# -*- coding: utf-8 -*-
"""
TSViT 训练器 - 简洁的训练循环实现
"""

import logging
import time
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from torch.amp import GradScaler, autocast
from tqdm import tqdm
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

from src.train.utils.cuda_prefetch import TwoStagePrefetcher

from .config import TSViTConfig
from .losses import compute_total_loss
from .metrics import compute_metrics
from .monitor import TSViTMonitor


class TSViTTrainer:
    """TSViT 训练器"""
    
    def __init__(
        self, 
        model: nn.Module, 
        config: TSViTConfig,
        device: torch.device,
        output_dir: str
    ):
        self.model = model
        self.config = config
        self.device = device
        self.output_dir = Path(output_dir)

        if torch.cuda.is_available():
            try:
                torch.set_float32_matmul_precision("high")
                torch.backends.cuda.matmul.allow_tf32 = True  # type: ignore[attr-defined]
            except Exception:
                pass
        
        # 创建输出目录
        self.ckpt_dir = self.output_dir / "ckpt"
        self.log_dir = self.output_dir / "logs"
        self.ckpt_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # 设置日志
        self.logger = logging.getLogger("tsvit_trainer")
        
        # 训练组件
        self.optimizer = self._create_optimizer()
        self.scheduler = self._create_scheduler()
        # 训练使用组合损失（wic + huber），评估亦保持一致
        self.scaler = GradScaler(enabled=bool(config.use_amp) and device.type == 'cuda')
        
        # TensorBoard
        self.writer = SummaryWriter(log_dir=str(self.log_dir), flush_secs=30, max_queue=1000)
        
        # 监控器
        monitor_config = {
            'detailed_log_freq': 100,
            'grad_explosion_threshold': 10.0,
            'grad_vanishing_threshold': 1e-4,
        }
        self.monitor = TSViTMonitor(self.writer, monitor_config)
        # Higher log frequency to reduce CPU overhead (500-1000 recommended for fast training)
        self.opt_log_freq = getattr(config, 'opt_log_freq', 500)
        self.log_trust_ratio_like = getattr(config, 'log_trust_ratio_like', False)
        
        # 早停
        self.best_metric = -float('inf')
        self.patience_counter = 0
        
        self.logger.info(f"训练器初始化完成，输出目录: {output_dir}")
    
    def _create_optimizer(self) -> torch.optim.Optimizer:
        """创建优化器，四级参数分组策略（参考DFZQ_GRU）"""
        # 四级分组：主干(full wd) → attention(wd*factor) → patch_embed(wd*factor) → head(wd*factor) → no_decay(0)
        decay_params = []           # 主干参数：正常权重衰减
        attention_params = []       # 注意力参数：轻度衰减（可配）
        patch_params = []           # patch_embed 参数：中等衰减（可配）
        head_params = []            # 输出头参数：轻度/中度衰减（可配）
        no_decay_params = []        # 无衰减参数：bias/norm/1D等
        
        # 记录参数名用于日志
        decay_param_names = []
        attention_param_names = []
        patch_param_names = []
        head_param_names = []
        no_decay_param_names = []
        
        for name, param in self.model.named_parameters():
            if not param.requires_grad:
                continue
                
            lname = name.lower()
            is_bias = ('bias' in lname)
            is_pos_emb = ('pos_emb' in lname or 'cls_token' in lname)
            is_norm = any(tok in lname for tok in ["ln", "layernorm", "batchnorm", "bn", "norm"])
            is_1d = (param.ndim == 1)
            is_attention = any(tok in lname for tok in ["attn", "attention", "self_attention", "multiheadattention"])
            # 头部判定：放宽至包含 head / output_projection / out_proj / classifier 任一
            is_head = any(tok in lname for tok in ["head", "output_projection", "out_proj", "classifier"])
            is_patch_embed = any(tok in lname for tok in ["patch_embed", "depth", "point"])
            
            # 四级分组策略
            if is_bias or is_norm or (is_1d and "weight" in lname) or is_pos_emb:
                # 1D参数、bias、norm、位置嵌入：不衰减
                no_decay_params.append(param)
                no_decay_param_names.append(name)
            elif is_attention:
                # 注意力参数：轻度衰减 (wd/10)
                attention_params.append(param)
                attention_param_names.append(name)
            elif is_head:
                # 输出头参数：中度衰减 (wd/2)
                head_params.append(param)
                head_param_names.append(name)
            elif is_patch_embed:
                # patch_embed 参数：中等衰减（介于 attention 与 backbone 之间）
                patch_params.append(param)
                patch_param_names.append(name)
            else:
                # 主干参数（patch_embed、encoder主体）：正常衰减
                decay_params.append(param)
                decay_param_names.append(name)
        
        # 计算参数统计
        decay_param_count = sum(p.numel() for p in decay_params)
        attention_param_count = sum(p.numel() for p in attention_params)
        patch_param_count = sum(p.numel() for p in patch_params)
        head_param_count = sum(p.numel() for p in head_params)
        no_decay_param_count = sum(p.numel() for p in no_decay_params)
        total_param_count = decay_param_count + attention_param_count + patch_param_count + head_param_count + no_decay_param_count
        
        # 动态权重衰减比例设置
        main_wd = self.config.weight_decay
        attention_wd = main_wd * float(getattr(self.config, 'wd_factor_attention', 0.2))  # 可由YAML配置
        head_wd = main_wd * float(getattr(self.config, 'wd_factor_head', 0.5))            # 可由YAML配置
        patch_wd = main_wd * float(getattr(self.config, 'wd_factor_patch_embed', 0.5))    # 可由YAML配置
        
        param_groups = [
            {'params': decay_params, 'weight_decay': main_wd, 'group_name': 'backbone'},
            {'params': attention_params, 'weight_decay': attention_wd, 'group_name': 'attention'},
            {'params': patch_params, 'weight_decay': patch_wd, 'group_name': 'patch_embed'},
            {'params': head_params, 'weight_decay': head_wd, 'group_name': 'head'},
            {'params': no_decay_params, 'weight_decay': 0.0, 'group_name': 'no_decay'}
        ]
        
        # 存储分组信息供监控使用
        self.param_group_info = {
            'backbone': {'params': decay_params, 'names': decay_param_names, 'count': decay_param_count, 'wd': main_wd},
            'attention': {'params': attention_params, 'names': attention_param_names, 'count': attention_param_count, 'wd': attention_wd},
            'patch_embed': {'params': patch_params, 'names': patch_param_names, 'count': patch_param_count, 'wd': patch_wd},
            'head': {'params': head_params, 'names': head_param_names, 'count': head_param_count, 'wd': head_wd},
            'no_decay': {'params': no_decay_params, 'names': no_decay_param_names, 'count': no_decay_param_count, 'wd': 0.0}
        }
        
        # 详细参数分组报告
        self.logger.info(f"🔍 TSViT四级参数分组分析:")
        self.logger.info(f"  ✅ 主干权重衰减组: {len(decay_params)}个参数层, {decay_param_count:,}个参数 ({decay_param_count/total_param_count:.1%}), wd={main_wd:.2e}")
        self.logger.info(f"  🎯 Attention轻度衰减组: {len(attention_params)}个参数层, {attention_param_count:,}个参数 ({attention_param_count/total_param_count:.1%}), wd={attention_wd:.2e}")
        self.logger.info(f"  🧩 PatchEmbed中度衰减组: {len(patch_params)}个参数层, {patch_param_count:,}个参数 ({patch_param_count/total_param_count:.1%}), wd={patch_wd:.2e}")
        self.logger.info(f"  🏗️ Head轻度衰减组: {len(head_params)}个参数层, {head_param_count:,}个参数 ({head_param_count/total_param_count:.1%}), wd={head_wd:.2e}")
        self.logger.info(f"  ❌ 无衰减组: {len(no_decay_params)}个参数层, {no_decay_param_count:,}个参数 ({no_decay_param_count/total_param_count:.1%}), wd=0.0")
        self.logger.info(f"  📊 总参数量: {total_param_count:,}")
        
        # 打印关键层的分组情况（前5个）
        for group_name, info in self.param_group_info.items():
            if info['names']:
                sample_names = info['names'][:5]
                self.logger.info(f"🎯 {group_name}组关键参数: {', '.join(sample_names)}{'...' if len(info['names']) > 5 else ''}")
        
        if self.config.optimizer == 'adamw':
            adamw_kwargs = {}
            if torch.cuda.is_available():
                try:
                    adamw_kwargs['fused'] = True
                except TypeError:
                    pass
            return torch.optim.AdamW(param_groups, lr=self.config.lr, **adamw_kwargs)
        elif self.config.optimizer == 'adam':
            return torch.optim.Adam(param_groups, lr=self.config.lr)
        else:
            raise ValueError(f"未支持的优化器: {self.config.optimizer}")
    
    def _create_scheduler(self) -> Optional[torch.optim.lr_scheduler._LRScheduler]:
        """创建学习率调度器"""
        name = (self.config.scheduler_name or 'cosine').lower()
        if name in ('cosine', 'warm_cos', 'warmcos', 'warmup_cosine'):
            # warmup + cosine (与GRU类似)
            from torch.optim.lr_scheduler import LinearLR, CosineAnnealingLR, SequentialLR
            warmup_epochs = max(0, int(self.config.warmup_epochs or 0))
            total_epochs = max(1, int(self.config.epochs or 1))
            main_epochs = max(1, total_epochs - warmup_epochs)
            warmup = LinearLR(
                self.optimizer,
                start_factor=float(self.config.scheduler_warmup_start_factor if self.config.scheduler_warmup_start_factor is not None else 0.1),
                end_factor=1.0,
                total_iters=warmup_epochs if warmup_epochs > 0 else 1,
            )
            cosine = CosineAnnealingLR(
                self.optimizer,
                T_max=main_epochs,
                eta_min=float(self.config.scheduler_min_lr if self.config.scheduler_min_lr is not None else max(1e-6, float(self.config.lr or 1e-3) * 0.01)),
            )
            return SequentialLR(
                self.optimizer,
                schedulers=[warmup, cosine] if warmup_epochs > 0 else [cosine],
                milestones=[warmup_epochs] if warmup_epochs > 0 else [0],
            )
        elif name in ('none', 'off'):
            return None
        else:
            raise ValueError(f"未支持的调度器: {self.config.scheduler_name}")
    
    def train_epoch(self, train_loader: DataLoader, epoch: int) -> Dict[str, float]:
        """训练一个 epoch"""
        self.model.train()

        epoch_losses: List[float] = []
        epoch_metrics = {'pearson_ic': [], 'spearman_ic': [], 'pred_std': []}
        metric_weight_sums = {'pearson_ic': 0.0, 'spearman_ic': 0.0, 'pred_std': 0.0}
        metric_weight_counts = {'pearson_ic': 0, 'spearman_ic': 0, 'pred_std': 0}
        sum_wic = 0.0
        sum_huber = 0.0
        sum_ortho = 0.0
        num_batches = 0

        grad_accum_steps = max(1, int(getattr(self.config, 'grad_accum_steps', 1)))
        total_loader_len = len(train_loader)
        pbar = tqdm(total=total_loader_len, desc=f'Epoch {epoch}', leave=False)
        start_wall = time.time()
        seen_samples = 0

        self.optimizer.zero_grad(set_to_none=True)

        # Use TwoStagePrefetcher for better CPU-GPU overlap
        cpu_queue_size = getattr(self.config, 'cpu_queue_size', 2)
        io_half = getattr(self.config, 'io_half', False)
        target_dtype = torch.float16 if (self.config.use_amp and io_half) else None
        
        prefetcher = TwoStagePrefetcher(
            train_loader, 
            self.device,
            cpu_queue_size=cpu_queue_size,
            target_dtype=target_dtype
        )
        batch = prefetcher.next()
        batch_idx = 0
        last_preclip = 0.0
        last_postclip = 0.0
        last_amp_scale = float(self.scaler.get_scale()) if (self.config.use_amp and self.device.type == 'cuda') else 0.0

        def _global_norm(params):
            sq = 0.0
            for p in params:
                if p.grad is not None:
                    sq += p.grad.detach().float().pow(2).sum().item()
            return sq ** 0.5

        while batch is not None:
            feats, labels = batch
            # Only convert dtype if not using io_half (already converted by prefetcher)
            if target_dtype is None:
                feats = feats.float()
                labels = labels.float()

            batch_size = feats.size(0)
            seen_samples += batch_size
            num_batches += 1
            pbar.update(1)

            with autocast(device_type='cuda', enabled=self.config.use_amp):
                use_ortho = getattr(self.config, 'use_orthogonality_penalty', False)
                if use_ortho:
                    preds, fv = self.model(feats, return_fv=True)
                else:
                    preds = self.model(feats, return_fv=False)
                    fv = None
                preds = preds.squeeze(-1) if preds.ndim > 1 else preds
                total, wic, huber, ortho = compute_total_loss(
                    preds, labels,
                    wic_mode=getattr(self.config, 'wic_mode', 'corr'),
                    lambda_wic=getattr(self.config, 'lambda_wic', 0.7),
                    huber_delta=getattr(self.config, 'loss_delta', 1.0),
                    huber_tau=getattr(self.config, 'huber_tau', 0.6),
                    focus=getattr(self.config, 'loss_focus', 'long_top'),
                    topk=getattr(self.config, 'loss_topk', 0.2),
                    fv=fv,
                    use_ortho=use_ortho,
                    alpha_corr=getattr(self.config, 'alpha_corr', 0.01),
                )
                loss = total / grad_accum_steps

            if self.config.use_amp:
                self.scaler.scale(loss).backward()
            else:
                loss.backward()

            epoch_losses.append(float(total.item()))
            batch_metrics = compute_metrics(preds.detach(), labels)
            epoch_metrics['pearson_ic'].append(batch_metrics['pearson_ic'])
            epoch_metrics['spearman_ic'].append(batch_metrics['spearman_ic'])
            epoch_metrics['pred_std'].append(batch_metrics['pred_std'])
            # Weight IC/pred std by the effective number of samples so that
            # tiny/warm-up batches do not dominate the epoch average.
            metric_weight_sums['pearson_ic'] += float(batch_metrics['pearson_ic']) * batch_size
            metric_weight_sums['spearman_ic'] += float(batch_metrics['spearman_ic']) * batch_size
            metric_weight_sums['pred_std'] += float(batch_metrics['pred_std']) * batch_size
            metric_weight_counts['pearson_ic'] += batch_size
            metric_weight_counts['spearman_ic'] += batch_size
            metric_weight_counts['pred_std'] += batch_size
            sum_wic += float(wic.item())
            sum_huber += float(huber.item())
            sum_ortho += float(ortho.item())

            next_batch = prefetcher.next()
            is_last = next_batch is None
            global_step = (epoch - 1) * total_loader_len + batch_idx
            step_now = ((batch_idx + 1) % grad_accum_steps == 0) or is_last
            need_opt_log = False
            param_snapshot = None

            if step_now:
                if self.config.use_amp:
                    self.scaler.unscale_(self.optimizer)
                preclip = _global_norm(self.model.parameters())
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.config.grad_clip)
                postclip = _global_norm(self.model.parameters())
                need_opt_log = (global_step % self.opt_log_freq) == 0
                if need_opt_log:
                    # Snapshot weights BEFORE step for update-norm later
                    param_snapshot = [p.detach().clone() for p in self.model.parameters() if p.requires_grad]

                if self.config.use_amp:
                    self.scaler.step(self.optimizer)
                    self.scaler.update()
                    last_amp_scale = float(self.scaler.get_scale())
                else:
                    self.optimizer.step()

                if need_opt_log:
                    import torch.optim as optim
                    is_adamw = isinstance(self.optimizer, optim.AdamW)
                    use_ratio = is_adamw
                    beta1 = beta2 = eps = None
                    group_stats = {}
                    if isinstance(self.optimizer, (optim.Adam, optim.AdamW)):
                        for group_idx, group in enumerate(self.optimizer.param_groups):
                            if beta1 is None:
                                beta1, beta2 = group.get('betas', (0.9, 0.999))
                                eps = group.get('eps', 1e-8)
                            group_name = group.get('group_name', f'group_{group_idx}')
                            lr = group['lr']
                            wd = float(group.get('weight_decay', 0.0))
                            amsgrad = bool(group.get('amsgrad', False))
                            group_w_sq = 0.0
                            group_grad_sq = 0.0
                            group_wd_step_sq = 0.0
                            group_grad_step_sq = 0.0
                            group_param_count = 0
                            for p in group['params']:
                                if not getattr(p, 'requires_grad', False):
                                    continue
                                w = p.detach()
                                group_param_count += p.numel()
                                group_w_sq += w.float().pow(2).sum().item()
                                if p.grad is not None:
                                    g = p.grad.detach()
                                    group_grad_sq += g.float().pow(2).sum().item()
                                state = self.optimizer.state.get(p, None)
                                if state and ('exp_avg' in state):
                                    m = state['exp_avg']
                                    v = state['max_exp_avg_sq'] if (amsgrad and 'max_exp_avg_sq' in state) else state['exp_avg_sq']
                                    step_i = int(state.get('step', 1))
                                    m_hat = m / (1.0 - (beta1 ** step_i))
                                    v_hat = v / (1.0 - (beta2 ** step_i))
                                    eff = m_hat / (v_hat.sqrt() + eps)
                                    grad_step = lr * eff
                                    group_grad_step_sq += grad_step.float().pow(2).sum().item()
                                if use_ratio and wd > 0.0:
                                    wd_step = (lr * wd) * w
                                    group_wd_step_sq += wd_step.float().pow(2).sum().item()
                            group_w_norm = (group_w_sq ** 0.5) if group_w_sq > 0 else 0.0
                            group_grad_norm = (group_grad_sq ** 0.5) if group_grad_sq > 0 else 0.0
                            group_wd_step_norm = (group_wd_step_sq ** 0.5) if group_wd_step_sq > 0 else 0.0
                            group_grad_step_norm = (group_grad_step_sq ** 0.5) if group_grad_step_sq > 0 else 0.0
                            if use_ratio and group_grad_step_norm > 0:
                                group_wd_over_grad = group_wd_step_norm / group_grad_step_norm
                            elif use_ratio:
                                group_wd_over_grad = float('nan')
                            else:
                                group_wd_over_grad = float('nan')
                            group_stats[group_name] = {
                                'w_norm': group_w_norm,
                                'grad_norm': group_grad_norm,
                                'wd_step_norm': group_wd_step_norm,
                                'grad_step_norm': group_grad_step_norm,
                                'wd_over_grad': group_wd_over_grad,
                                'param_count': group_param_count,
                                'weight_decay': wd,
                            }
                    self.monitor.log_layerwise_wd_stats(global_step, group_stats)

                self.optimizer.zero_grad(set_to_none=True)
                last_preclip = preclip
                last_postclip = postclip
            else:
                preclip = last_preclip
                postclip = last_postclip

            iter_elapsed = max(time.time() - start_wall, 1e-6)
            samples_per_sec = float(seen_samples) / iter_elapsed

            if step_now and need_opt_log and param_snapshot is not None:
                w_sq = 0.0
                dw_sq = 0.0
                i = 0
                for p in self.model.parameters():
                    if not p.requires_grad:
                        continue
                    w = p.detach()
                    w_sq += w.float().pow(2).sum().item()
                    prev = param_snapshot[i]
                    dw = (w - prev)
                    dw_sq += dw.float().pow(2).sum().item()
                    i += 1
                w_norm = (w_sq ** 0.5)
                update_norm = (dw_sq ** 0.5) if dw_sq > 0 else None
                rel_update = (update_norm / max(w_norm, 1e-12)) if update_norm is not None else None
                g_over_w = (postclip / max(w_norm, 1e-12)) if w_norm > 0 else None

                import torch.optim as optim
                is_adamw = isinstance(self.optimizer, optim.AdamW)
                use_ratio = is_adamw
                adam_m_sq = adam_v_sum = eff_step_sq = 0.0
                wd_step_sq = grad_step_sq = 0.0
                beta1 = beta2 = eps = None
                if isinstance(self.optimizer, (optim.Adam, optim.AdamW)):
                    for group_idx, group in enumerate(self.optimizer.param_groups):
                        if beta1 is None:
                            beta1, beta2 = group.get('betas', (0.9, 0.999))
                            eps = group.get('eps', 1e-8)
                        group_name = group.get('group_name', f'group_{group_idx}')
                        lr = group['lr']
                        wd = float(group.get('weight_decay', 0.0))
                        amsgrad = bool(group.get('amsgrad', False))
                        group_w_sq = 0.0
                        group_grad_sq = 0.0
                        group_wd_step_sq = 0.0
                        group_grad_step_sq = 0.0
                        group_param_count = 0
                        for p in group['params']:
                            if not getattr(p, 'requires_grad', False):
                                continue
                            w = p.detach()
                            group_param_count += p.numel()
                            group_w_sq += w.float().pow(2).sum().item()
                            state = self.optimizer.state.get(p, None)
                            if state and ('exp_avg' in state):
                                m = state['exp_avg']
                                v = state['max_exp_avg_sq'] if (amsgrad and 'max_exp_avg_sq' in state) else state['exp_avg_sq']
                                step_i = int(state.get('step', 1))
                                adam_m_sq += m.float().pow(2).sum().item()
                                adam_v_sum += v.float().sum().item()
                                m_hat = m / (1.0 - (beta1 ** step_i))
                                v_hat = v / (1.0 - (beta2 ** step_i))
                                eff = m_hat / (v_hat.sqrt() + eps)
                                eff_step_sq += eff.float().pow(2).sum().item()
                                grad_step = lr * eff
                                group_grad_step_sq += grad_step.float().pow(2).sum().item()
                            if use_ratio and wd > 0.0:
                                wd_step = (lr * wd) * w
                                group_wd_step_sq += wd_step.float().pow(2).sum().item()
                        wd_step_sq += group_wd_step_sq
                        grad_step_sq += group_grad_step_sq
                w_norm = (w_sq ** 0.5)
                eff_step_norm = (eff_step_sq ** 0.5) if eff_step_sq > 0 else None
                adam_m_norm = (adam_m_sq ** 0.5) if adam_m_sq > 0 else None
                adam_rms_v = (adam_v_sum / max(1e-12, w_sq)) ** 0.5 if (adam_v_sum > 0 and w_sq > 0) else None
                if use_ratio and grad_step_sq > 0:
                    wd_over_grad = ((wd_step_sq ** 0.5) / (grad_step_sq ** 0.5)) if wd_step_sq > 0 else 0.0
                elif use_ratio:
                    wd_over_grad = float('nan')
                else:
                    wd_over_grad = None
                trust_ratio_like = (w_norm / max(eff_step_norm, 1e-12)) if (self.log_trust_ratio_like and eff_step_norm is not None) else None
                self.monitor.log_optim_step(
                    step=global_step,
                    grad_pre=last_preclip,
                    grad_post=last_postclip,
                    clip_threshold=self.config.grad_clip,
                    w_norm=w_norm,
                    update_norm=update_norm,
                    rel_update=rel_update,
                    g_over_w=g_over_w,
                    adam_m_norm=adam_m_norm,
                    adam_rms_v=adam_rms_v,
                    eff_step_norm=eff_step_norm,
                    wd_over_grad=wd_over_grad,
                    trust_ratio_like=trust_ratio_like,
                )

            # Reduce logging frequency to minimize CPU overhead
            log_freq = getattr(self.config, 'step_log_freq', 100)
            if batch_idx % log_freq == 0:
                amp_scale = last_amp_scale if self.config.use_amp else 0.0
                self.monitor.log_step_metrics(
                    step=global_step,
                    loss=float(total.item()),
                    lr=self.optimizer.param_groups[0]['lr'],
                    wic_loss=float(wic.item()),
                    huber_loss=float(huber.item()),
                    ortho_loss=float(ortho.item()),
                    pearson_ic=batch_metrics['pearson_ic'],
                    spearman_ic=batch_metrics['spearman_ic'],
                    predictions=preds.detach(),
                    labels=labels,
                    grad_norm_preclip=last_preclip,
                    grad_norm_postclip=last_postclip,
                    grad_clip_threshold=float(self.config.grad_clip or 0.0),
                    amp_scale=amp_scale,
                    samples_per_sec=samples_per_sec,
                    prefix="Train",
                )
                pbar.set_postfix(
                    loss=float(total.item()),
                    pearson=batch_metrics['pearson_ic'],
                    spearman=batch_metrics['spearman_ic']
                )

            batch = next_batch
            batch_idx += 1

        pbar.close()

        results = {
            'loss': float(np.mean(epoch_losses)) if epoch_losses else 0.0,
            'pearson_ic': (
                metric_weight_sums['pearson_ic'] / metric_weight_counts['pearson_ic']
                if metric_weight_counts['pearson_ic'] > 0
                else (float(np.mean(epoch_metrics['pearson_ic'])) if epoch_metrics['pearson_ic'] else 0.0)
            ),
            'spearman_ic': (
                metric_weight_sums['spearman_ic'] / metric_weight_counts['spearman_ic']
                if metric_weight_counts['spearman_ic'] > 0
                else (float(np.mean(epoch_metrics['spearman_ic'])) if epoch_metrics['spearman_ic'] else 0.0)
            ),
        }
        if num_batches > 0:
            results['wic'] = sum_wic / num_batches
            results['huber'] = sum_huber / num_batches
            results['ortho'] = sum_ortho / num_batches
        if metric_weight_counts['pred_std'] > 0:
            results['pred_std'] = metric_weight_sums['pred_std'] / metric_weight_counts['pred_std']
        elif epoch_metrics['pred_std']:
            results['pred_std'] = float(np.mean(epoch_metrics['pred_std']))
        results['combined_ic'] = (results['pearson_ic'] + results['spearman_ic']) / 2

        return results
    @torch.no_grad()
    def evaluate(self, eval_loader: DataLoader, split: str = 'val') -> Dict[str, float]:
        """评估模型"""
        self.model.eval()
        
        all_preds = []
        all_labels = []
        epoch_losses = []
        
        pbar = tqdm(eval_loader, desc=f'{split.title()}', leave=False)
        sum_wic = 0.0
        sum_huber = 0.0
        sum_ortho = 0.0
        count = 0
        
        for batch_idx, batch_data in enumerate(pbar):
            # 解包数据
            if len(batch_data) >= 2:
                feats, labels = batch_data[0], batch_data[1]
            else:
                raise ValueError("数据格式错误")
            
            # 移到设备
            feats = feats.to(self.device, non_blocking=True).float()
            labels = labels.to(self.device, non_blocking=True).float()
            
            # 前向传播
            with autocast(device_type='cuda', enabled=self.config.use_amp):
                # 根据是否启用正交惩罚决定是否返回fv
                use_ortho = getattr(self.config, 'use_orthogonality_penalty', False)
                if use_ortho:
                    preds, fv = self.model(feats, return_fv=True)  # [B], [B, Dh]
                else:
                    preds = self.model(feats, return_fv=False)  # [B]
                    fv = None
                
                preds = preds.squeeze(-1) if preds.ndim > 1 else preds
                
                # 使用新的完整损失计算
                total, wic, huber, ortho = compute_total_loss(
                    preds, labels,
                    wic_mode=getattr(self.config, 'wic_mode', 'corr'),
                    lambda_wic=getattr(self.config, 'lambda_wic', 0.7),
                    huber_delta=getattr(self.config, 'loss_delta', 1.0),
                    huber_tau=getattr(self.config, 'huber_tau', 0.6),
                    focus=getattr(self.config, 'loss_focus', 'long_top'),
                    topk=getattr(self.config, 'loss_topk', 0.2),
                    fv=fv,
                    use_ortho=use_ortho,
                    alpha_corr=getattr(self.config, 'alpha_corr', 0.01),
                )
                loss = total
            
            epoch_losses.append(loss.item())
            sum_wic += float(wic.item())
            sum_huber += float(huber.item())
            sum_ortho += float(ortho.item())
            count += 1
            all_preds.append(preds.cpu())
            all_labels.append(labels.cpu())
        
        # 合并所有预测和标签
        all_preds = torch.cat(all_preds, dim=0)
        all_labels = torch.cat(all_labels, dim=0)
        
        # 计算整体指标
        metrics = compute_metrics(all_preds, all_labels)
        metrics['loss'] = float(np.mean(epoch_losses))
        metrics['wic'] = sum_wic / max(count, 1)
        metrics['huber'] = sum_huber / max(count, 1)
        metrics['ortho'] = sum_ortho / max(count, 1)
        metrics['combined_ic'] = 0.5 * (metrics['pearson_ic'] + metrics['spearman_ic'])
        # 统一写 epoch 指标交由 monitor.log_epoch_metrics 调用时写入，避免重复
        
        return metrics
    
    def save_checkpoint(self, epoch: int, metrics: Dict[str, float], is_best: bool = False):
        """保存检查点"""
        ckpt_data = {
            'epoch': epoch,
            'model_state_dict': self.model.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'scaler_state_dict': self.scaler.state_dict() if self.config.use_amp else None,
            'config': self.config.__dict__,
            'metrics': metrics
        }
        
        # 保存最新检查点
        latest_path = self.ckpt_dir / "latest.pth"
        torch.save(ckpt_data, latest_path)
        
        # 保存最佳模型
        if is_best:
            best_path = self.ckpt_dir / "best.pth"
            torch.save(ckpt_data, best_path)
            self.logger.info(f"保存最佳模型: {best_path}, Combined IC: {metrics.get('combined_ic', 0):.6f}")
    
    def should_stop(self, metric: float) -> bool:
        """早停检查"""
        if metric > self.best_metric + 1e-6:
            self.best_metric = metric
            self.patience_counter = 0
            return False
        else:
            self.patience_counter += 1
            if self.patience_counter >= self.config.patience:
                self.logger.info(f"早停触发，patience: {self.config.patience}")
                return True
            return False
    
    def train(
        self, 
        train_loader: DataLoader, 
        valid_loader: DataLoader,
        test_loader: Optional[DataLoader] = None
    ) -> str:
        """完整训练流程"""
        self.logger.info(f"开始训练 {self.config.epochs} 个epoch")
        
        for epoch in range(1, self.config.epochs + 1):
            # 训练
            train_metrics = self.train_epoch(train_loader, epoch)
            
            # 验证
            val_metrics = self.evaluate(valid_loader, 'val')
            
            # 使用监控器记录epoch指标
            self.monitor.log_epoch_metrics(epoch, train_metrics, val_metrics)
            
            # 记录模型统计
            self.monitor.log_model_stats(self.model, epoch)
            
            # 学习率调度
            if self.scheduler:
                self.scheduler.step()
                self.writer.add_scalar('LR', self.optimizer.param_groups[0]['lr'], epoch)
            
            # 日志
            ortho_info = f" | Ortho: {train_metrics.get('ortho', 0.0):.6f}" if getattr(self.config, 'use_orthogonality_penalty', False) else ""
            self.logger.info(
                f"Epoch {epoch:03d}/{self.config.epochs} | "
                f"Train Loss: {train_metrics['loss']:.6f} | "
                f"Train IC: {train_metrics['combined_ic']:.6f} "
                f"(P: {train_metrics.get('pearson_ic', 0.0):.6f}, S: {train_metrics.get('spearman_ic', 0.0):.6f}) | "
                f"Val IC: {val_metrics['combined_ic']:.6f} "
                f"(P: {val_metrics.get('pearson_ic', 0.0):.6f}, S: {val_metrics.get('spearman_ic', 0.0):.6f}) | "
                f"LR: {self.optimizer.param_groups[0]['lr']:.2e}{ortho_info}"
            )
            
            # 保存检查点
            is_best = val_metrics['combined_ic'] > self.best_metric
            self.save_checkpoint(epoch, val_metrics, is_best)
            
            # 早停检查
            if self.should_stop(val_metrics['combined_ic']):
                break
        
        # 测试最佳模型
        if test_loader is not None:
            best_path = self.ckpt_dir / "best.pth"
            if best_path.exists():
                checkpoint = torch.load(best_path, map_location=self.device, weights_only=False)
                self.model.load_state_dict(checkpoint['model_state_dict'])
                self.logger.info(f"加载最佳模型进行测试")
                
                test_metrics = self.evaluate(test_loader, 'test')
                
                # 记录测试结果
                for key, value in test_metrics.items():
                    self.writer.add_scalar(f'Test/{key}', value, epoch)
                
                self.logger.info(
                    f"测试结果 | IC: {test_metrics['combined_ic']:.6f} | "
                    f"Pearson: {test_metrics['pearson_ic']:.6f} | "
                    f"Spearman: {test_metrics['spearman_ic']:.6f}"
                )
        
        self.writer.close()
        self.logger.info(f"训练完成，输出目录: {self.output_dir}")
        
        return str(self.output_dir)
