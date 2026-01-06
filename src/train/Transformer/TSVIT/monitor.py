# -*- coding: utf-8 -*-
"""
TSViT 训练监控 - 精简版本，参考GRU监控
"""

import torch
import numpy as np
from torch.utils.tensorboard import SummaryWriter
from typing import Dict, Any, Optional
import logging


class TSViTMonitor:
    """TSViT 精简监控器"""
    
    def __init__(self, writer: SummaryWriter, config: Optional[Dict] = None):
        self.writer = writer
        self.config = config or {}
        self.logger = logging.getLogger("tsvit_monitor")
        
        # 监控配置
        self.detailed_log_freq = self.config.get('detailed_log_freq', 100)
        self.grad_explosion_threshold = self.config.get('grad_explosion_threshold', 10.0)
        self.grad_vanishing_threshold = self.config.get('grad_vanishing_threshold', 1e-4)
        
        self.logger.info("TSViT监控器初始化完成")
    
    def log_step_metrics(
        self,
        step: int,
        loss: float,
        lr: float,
        wic_loss: Optional[float] = None,
        huber_loss: Optional[float] = None,
        ortho_loss: Optional[float] = None,
        pearson_ic: Optional[float] = None,
        spearman_ic: Optional[float] = None,
        predictions: Optional[torch.Tensor] = None,
        labels: Optional[torch.Tensor] = None,
        grad_norm_preclip: Optional[float] = None,
        grad_norm_postclip: Optional[float] = None,
        grad_clip_threshold: Optional[float] = None,
        amp_scale: Optional[float] = None,
        samples_per_sec: Optional[float] = None,
        prefix: str = "Train",
    ):
        """记录步级指标（统一分组/命名）"""

        # Loss（步级）
        self.writer.add_scalar(f"Loss/step_total_{prefix}", loss, step)
        if wic_loss is not None:
            self.writer.add_scalar(f"Loss/step_wic_{prefix}", float(wic_loss), step)
        if huber_loss is not None:
            self.writer.add_scalar(f"Loss/step_huber_{prefix}", float(huber_loss), step)
        if ortho_loss is not None:
            self.writer.add_scalar(f"Loss/step_ortho_{prefix}", float(ortho_loss), step)

        # IC（步级）
        if pearson_ic is not None:
            self.writer.add_scalar(f"IC/step_pearson_{prefix}", float(pearson_ic), step)
        if spearman_ic is not None:
            self.writer.add_scalar(f"IC/step_spearman_{prefix}", float(spearman_ic), step)

        # Optim / LR
        self.writer.add_scalar(f"Optim/lr_{prefix}", lr, step)

        # Grad（裁剪前/后）与 Alerts
        if grad_norm_preclip is not None:
            self.writer.add_scalar(f"Optim/grad_norm_preclip_{prefix}", float(grad_norm_preclip), step)
        if grad_norm_postclip is not None:
            self.writer.add_scalar(f"Optim/grad_norm_postclip_{prefix}", float(grad_norm_postclip), step)
            if grad_norm_postclip > self.grad_explosion_threshold:
                self.writer.add_scalar("Alerts/grad_explosion", float(grad_norm_postclip), step)
            if grad_norm_postclip < self.grad_vanishing_threshold:
                self.writer.add_scalar("Alerts/grad_vanishing", float(grad_norm_postclip), step)
        if (grad_norm_preclip is not None) and (grad_clip_threshold is not None) and grad_clip_threshold > 0:
            ratio = float(grad_norm_preclip) / float(grad_clip_threshold)
            self.writer.add_scalar(f"Optim/grad_clip_ratio_{prefix}", ratio, step)

        # 分布/偏置
        if predictions is not None:
            pred_mean = predictions.mean().item()
            pred_std = predictions.std().item()
            self.writer.add_scalar(f"Dist/pred_mean_{prefix}", pred_mean, step)
            self.writer.add_scalar(f"Dist/pred_std_{prefix}", pred_std, step)
            if torch.isnan(predictions).any():
                self.writer.add_scalar("Alerts/pred_nan", 1.0, step)
            if torch.isinf(predictions).any():
                self.writer.add_scalar("Alerts/pred_inf", 1.0, step)
        if labels is not None:
            self.writer.add_scalar(f"Dist/label_std_{prefix}", labels.std().item(), step)
            if predictions is not None:
                bias = (predictions - labels).mean().item()
                self.writer.add_scalar(f"Dist/pred_bias_{prefix}", bias, step)

        # AMP / Runtime
        if amp_scale is not None:
            self.writer.add_scalar(f"AMP/scale_{prefix}", float(amp_scale), step)
            if amp_scale < 2.0:
                self.writer.add_scalar("Alerts/amp_scale_drop", 1.0, step)
        if samples_per_sec is not None:
            self.writer.add_scalar(f"Runtime/samples_per_sec_{prefix}", float(samples_per_sec), step)
    
    def log_epoch_metrics(self, epoch: int, train_metrics: Dict[str, float], val_metrics: Dict[str, float]):
        """记录 epoch 级指标（统一命名）"""
        for split, metrics in (("train", train_metrics), ("val", val_metrics)):
            if 'loss' in metrics:
                self.writer.add_scalar(f"Loss/epoch_total_{split}", metrics['loss'], epoch)
            if 'wic' in metrics:
                self.writer.add_scalar(f"Loss/epoch_wic_{split}", metrics['wic'], epoch)
            if 'huber' in metrics:
                self.writer.add_scalar(f"Loss/epoch_huber_{split}", metrics['huber'], epoch)
            if 'ortho' in metrics:
                self.writer.add_scalar(f"Loss/epoch_ortho_{split}", metrics['ortho'], epoch)
            if 'pearson_ic' in metrics:
                self.writer.add_scalar(f"IC/epoch_pearson_{split}", metrics['pearson_ic'], epoch)
            if 'spearman_ic' in metrics:
                self.writer.add_scalar(f"IC/epoch_spearman_{split}", metrics['spearman_ic'], epoch)
            if 'combined_ic' in metrics:
                self.writer.add_scalar(f"IC/epoch_combined_{split}", metrics['combined_ic'], epoch)

    def log_optim_step(
        self,
        step: int,
        *,
        grad_pre: Optional[float] = None,
        grad_post: Optional[float] = None,
        clip_threshold: Optional[float] = None,
        w_norm: Optional[float] = None,
        update_norm: Optional[float] = None,
        rel_update: Optional[float] = None,
        g_over_w: Optional[float] = None,
        adam_m_norm: Optional[float] = None,
        adam_rms_v: Optional[float] = None,
        eff_step_norm: Optional[float] = None,
        wd_over_grad: Optional[float] = None,
        trust_ratio_like: Optional[float] = None,
        prefix: str = "Optim",
    ):
        if grad_pre is not None:
            self.writer.add_scalar(f"{prefix}/grad_norm_preclip", grad_pre, step)
        if grad_post is not None:
            self.writer.add_scalar(f"{prefix}/grad_norm_postclip", grad_post, step)
        if (clip_threshold is not None) and (grad_pre is not None) and clip_threshold > 0:
            self.writer.add_scalar(f"{prefix}/clip_ratio", float(grad_pre) / float(clip_threshold), step)
        if w_norm is not None:
            self.writer.add_scalar(f"{prefix}/weight_norm_total", w_norm, step)
        if update_norm is not None:
            self.writer.add_scalar(f"{prefix}/update_norm", update_norm, step)
        if rel_update is not None:
            self.writer.add_scalar(f"{prefix}/update_over_weight", rel_update, step)
        if g_over_w is not None:
            self.writer.add_scalar(f"{prefix}/grad_over_weight", g_over_w, step)
        if adam_m_norm is not None:
            self.writer.add_scalar(f"{prefix}/adam_m_norm", adam_m_norm, step)
        if adam_rms_v is not None:
            self.writer.add_scalar(f"{prefix}/adam_rms_v", adam_rms_v, step)
        if eff_step_norm is not None:
            self.writer.add_scalar(f"{prefix}/adam_eff_step_norm", eff_step_norm, step)
        if wd_over_grad is not None:
            self.writer.add_scalar(f"{prefix}/wd_step_over_grad_step", wd_over_grad, step)
        if trust_ratio_like is not None:
            self.writer.add_scalar(f"{prefix}/trust_ratio_like", trust_ratio_like, step)
    
    def log_model_stats(self, model: torch.nn.Module, epoch: int):
        """记录模型统计信息"""
        if epoch % self.detailed_log_freq == 0:
            # 参数统计
            total_params = sum(p.numel() for p in model.parameters())
            trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
            
            self.writer.add_scalar("Model/TotalParams", total_params, epoch)
            self.writer.add_scalar("Model/TrainableParams", trainable_params, epoch)
            
            # 权重统计
            for name, param in model.named_parameters():
                if param.requires_grad and len(param.shape) >= 2:  # 只监控权重矩阵
                    weight_norm = param.norm().item()
                    self.writer.add_scalar(f"Weights/{name}_norm", weight_norm, epoch)
                    
                    if param.grad is not None:
                        grad_norm = param.grad.norm().item()
                        self.writer.add_scalar(f"Grads/{name}_norm", grad_norm, epoch)
    
    def log_layerwise_wd_stats(self, step: int, group_stats: dict):
        """记录分层权重衰减统计（关键指标）"""
        for group_name, stats in group_stats.items():
            # 每个参数组的关键指标
            self.writer.add_scalar(f"LayerWD/{group_name}_w_norm", stats['w_norm'], step)
            self.writer.add_scalar(f"LayerWD/{group_name}_grad_norm", stats['grad_norm'], step)
            self.writer.add_scalar(f"LayerWD/{group_name}_wd_step_norm", stats['wd_step_norm'], step)
            self.writer.add_scalar(f"LayerWD/{group_name}_grad_step_norm", stats['grad_step_norm'], step)
            
            # 最关键指标：wd_step / grad_step 比例
            self.writer.add_scalar(f"LayerWD/{group_name}_wd_over_grad", stats['wd_over_grad'], step)
            
            # 相对权重（此层参数占总参数的比例）
            # 这个可以在初始化时计算一次，这里简化
            
        # 记录关键报警：某些组的 wd_over_grad 过大
        for group_name, stats in group_stats.items():
            if stats['wd_over_grad'] > 2.0:  # WD 步长超过梯度步长2倍
                self.writer.add_scalar(f"Alerts/wd_dominance_{group_name}", stats['wd_over_grad'], step)
                if step % 100 == 0:  # 低频报警
                    self.logger.warning(f"⚠️ {group_name}层 WD 主导过度: wd/grad = {stats['wd_over_grad']:.3f}")
    
    def log_attention_stats(self, model: torch.nn.Module, step: int):
        """记录注意力统计（可选）"""
        # 这里可以添加注意力权重的可视化
        # 由于TSViT可能使用不同的编码器实现，这里保持简单
        pass
    
    def flush(self):
        """刷新TensorBoard"""
        self.writer.flush()
