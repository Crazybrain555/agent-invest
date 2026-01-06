# train_dfzq_gru.py - TensorBoard版本，移除wandb相关功能
import logging
import random
import time
import warnings
import os
from pathlib import Path
from typing import Tuple, List, Dict, Optional
from collections import defaultdict

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from scipy.stats import spearmanr, pearsonr
from torch.amp import GradScaler, autocast
from tqdm import tqdm
from torch.optim.lr_scheduler import LinearLR, CosineAnnealingLR, SequentialLR

from src.models.rnn.gru.dfzq_gru.dfzq_gru import DFZQGRU
# from src.models.rnn.gru.dfzq_gru.dfzq_gru_copy_0610base import DFZQGRU
# from src.models.rnn.gru.dfzq_gru.dfzq_gru_copy_0607 import DFZQGRU
# from src.models.rnn.gru.dfzq_gru.dfzq_gru_06072240 import DFZQGRU

from src.models.rnn.gru.dfzq_gru.get_configs import DFZQGRUConfig
from src.train.Neural_networks.RNN.DFZQ_GRU.dfzq_Dataloader import get_train_valid_test_loaders
from src.train.Neural_networks.RNN.DFZQ_GRU.config import TrainingConfig
from src.train.Neural_networks.RNN.DFZQ_GRU.training_monitor import TrainingMonitor
from src.utils.experiment_utils import get_experiment_summary, create_experiment_dirs


class SimpleEarlyStop:
    """简单的早停机制"""
    def __init__(self, patience: int = 10, delta: float = 1e-4):
        self.patience = patience
        self.delta = delta
        self.best = -float("inf")
        self.counter = 0
        self.should_stop = False

    def __call__(self, metric: float):
        if metric > self.best + self.delta:
            self.best = metric
            self.counter = 0
        else:
            self.counter += 1
            if self.counter >= self.patience:
                self.should_stop = True


# ===============================================
# 🎯 简洁优美的损失函数实现
# ===============================================

def pearson_ic_loss(preds: torch.Tensor, labels: torch.Tensor, eps: float = 1e-8) -> torch.Tensor:
    """
    🎯 简洁优美的Pearson IC损失：loss = 1 - ρ(pred, label)
    
    Args:
        preds: [B, 1] 预测值
        labels: [B, 1] 真实值  
        eps: 数值稳定性小量
    
    Returns:
        loss: 标量，1 - Pearson相关系数
    """
    # 去中心化
    p_centered = preds - preds.mean()
    l_centered = labels - labels.mean()
    
    # 计算协方差和标准差
    cov = (p_centered * l_centered).mean()
    p_std = p_centered.std() + eps
    l_std = l_centered.std() + eps
    
    # p_std = p_std.detach()
    # Pearson相关系数 → IC损失
    # corr = cov / (p_std * l_std)
    corr = cov
    logging.getLogger(__name__).debug(f"Pearson IC: {corr}")
    return 1.0 - corr


def variance_penalty(preds: torch.Tensor, target_std: float = 1.0, eps: float = 1e-3) -> torch.Tensor:
    """
    🎯 方差守门员正则：防止预测方差被过度压缩
    
    Args:
        preds: [B, 1] 预测值
        target_std: 目标标准差
        eps: 数值稳定性小量
    
    Returns:
        penalty: 标量，方差偏离目标的惩罚
    """
    pred_std = preds.std() + eps
    return ((pred_std / target_std - 1.0) ** 2)


def combined_loss(preds: torch.Tensor, labels: torch.Tensor, 
                 lambda_ic: float = 0.6, huber_delta: float = 1.0, 
                 lambda_var: float = 0.05, target_std: float = 1.0) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    🎯 组合损失：λ_ic * IC_loss + (1-λ_ic) * Huber_loss + λ_var * Variance_penalty
    
    Args:
        preds: [B, 1] 预测值
        labels: [B, 1] 真实值
        lambda_ic: IC损失权重 ∈ [0,1]
        huber_delta: Huber损失δ参数
        lambda_var: 方差惩罚权重
        target_std: 目标标准差
    
    Returns:
        total_loss: 组合损失
        ic_loss: IC损失分量  
        huber_loss: Huber损失分量
        var_penalty: 方差惩罚分量
    """
    # IC损失：优化排序能力
    ic_loss = pearson_ic_loss(preds, labels)
    
    # Huber损失：保持数值尺度
    huber_loss = nn.functional.huber_loss(preds, labels, delta=huber_delta)
    
    # 方差守门员：防止预测方差过度压缩
    var_penalty = variance_penalty(preds, target_std)
    
    # 加权组合
    total_loss = lambda_ic * ic_loss + (1.0 - lambda_ic) * huber_loss + lambda_var * var_penalty
    
    return total_loss, ic_loss, huber_loss, var_penalty


# --------------------------------------------------
# 核心损失和评估函数
# --------------------------------------------------

def orthogonality_penalty(fv: torch.Tensor, eps: float = 1e-3) -> torch.Tensor:
    """稳定的正交惩罚实现"""
    if fv.size(0) <= 1:
        return torch.tensor(0.0, device=fv.device)
        
    # 中心化
    x = fv - fv.mean(dim=0, keepdim=True)
    
    # 标准化
    var = x.var(dim=0, unbiased=False).clamp(min=eps)
    std = torch.sqrt(var)
    x_norm = x / std.unsqueeze(0)
    
    # 相关矩阵
    n = x.size(0)
    corr = (x_norm.T @ x_norm) / n
    
    # 去除对角线
    eye = torch.eye(corr.size(0), device=corr.device)
    corr = corr - eye
    
    return (corr ** 2).mean()


def spearman_ic(pred: torch.Tensor, label: torch.Tensor) -> float:
    """计算Spearman相关系数"""
    try:
        ic, _ = spearmanr(pred.detach().cpu().numpy(), label.detach().cpu().numpy(), nan_policy="omit")
        return float(ic) if not np.isnan(ic) else 0.0
    except:
        return 0.0


def pearson_ic(pred: torch.Tensor, label: torch.Tensor) -> float:
    """计算Pearson相关系数"""
    try:
        pred_np = pred.detach().cpu().numpy().flatten()
        label_np = label.detach().cpu().numpy().flatten()
        valid_mask = ~(np.isnan(pred_np) | np.isnan(label_np))
        if np.sum(valid_mask) < 2:
            return 0.0
        ic, _ = pearsonr(pred_np[valid_mask], label_np[valid_mask])
        return float(ic) if not np.isnan(ic) else 0.0
    except:
        return 0.0


# --------------------------------------------------
# 标签日期标准化功能
# --------------------------------------------------

def compute_and_save_date_stats(
    dataloaders: List[DataLoader],
    device: torch.device,
    cache_file: Optional[str] = None
) -> Dict[str, Tuple[float, float]]:
    """计算所有日期的标签均值和标准差"""
    # 检查缓存
    if cache_file and Path(cache_file).exists():
        try:
            import pickle
            with open(cache_file, 'rb') as f:
                date_stats = pickle.load(f)
                print(f"从缓存加载 {len(date_stats)} 个日期的统计信息")
                return date_stats
        except Exception as e:
            print(f"读取缓存失败: {e}，重新计算")
    
    print("计算每个日期的标签统计信息...")
    start_time = time.time()
    
    # 收集日期标签
    date_labels: Dict[str, List[float]] = defaultdict(list)
    
    for loader_idx, dataloader in enumerate(dataloaders):
        pbar = tqdm(dataloader, desc=f'收集数据集{loader_idx+1}日期标签', leave=True)
        for batch_data in pbar:
            if len(batch_data) == 4:
                _, labels, dates, _ = batch_data
                labels = labels.to(device).float()
                
                for i, date in enumerate(dates):
                    date_labels[date].append(labels[i].item())
            else:
                raise ValueError("数据加载器必须返回日期信息(keep_meta=True)")
    
    # 计算统计信息
    date_stats = {}
    for date, values in date_labels.items():
        if not values:
            print(f"警告: 日期 {date} 没有标签数据，使用默认值")
            date_stats[date] = (0.0, 1.0)
            continue
            
        values_tensor = torch.tensor(values, device=device, dtype=torch.float32)
        date_mean = values_tensor.mean().item()
        date_std = max(values_tensor.std(unbiased=False).item(), 1e-3)
        date_stats[date] = (date_mean, date_std)
    
    print(f"计算完成，共 {len(date_stats)} 个日期，耗时 {time.time() - start_time:.2f}秒")
    
    # 保存缓存
    if cache_file:
        import pickle
        cache_dir = Path(cache_file).parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_file, 'wb') as f:
            pickle.dump(date_stats, f)
        print(f"缓存保存到 {cache_file}")
    
    return date_stats


def standardize_labels_by_date_fn(
    labels: torch.Tensor, 
    dates: List[str],
    date_stats: Dict[str, Tuple[float, float]]
) -> torch.Tensor:
    """按日期标准化标签"""
    normalized_labels = torch.zeros_like(labels)
    
    # 计算全局统计作为备用
    all_means = [m for m, _ in date_stats.values()]
    all_stds = [s for _, s in date_stats.values()]
    global_mean = sum(all_means) / len(all_means) if all_means else 0.0
    global_std = sum(all_stds) / len(all_stds) if all_stds else 1.0
    
    for i, date in enumerate(dates):
        if date in date_stats:
            date_mean, date_std = date_stats[date]
        else:
            date_mean, date_std = global_mean, global_std
            print(f"警告: 日期 {date} 未找到，使用全局统计")
        
        normalized_labels[i] = (labels[i] - date_mean) / date_std
    
    return normalized_labels


# --------------------------------------------------
# 训练和评估函数
# --------------------------------------------------

def train_one_epoch(
    model: nn.Module,
    loader: DataLoader,
    optimizer: torch.optim.Optimizer,
    scaler: Optional[GradScaler],
    device: torch.device,
    alpha_corr: float,
    use_amp: bool,
    grad_clip_norm: float,
    monitor: Optional[TrainingMonitor] = None,
    epoch: Optional[int] = None,
    standardize_labels_by_date: bool = False,
    date_to_idx: Optional[Dict[str, int]] = None,
    means_tensor: Optional[torch.Tensor] = None,
    stds_tensor: Optional[torch.Tensor] = None,
    gradient_explosion_threshold: float = 10.0,
    gradient_vanishing_threshold: float = 1e-4,
    enable_nan_handling: bool = False,  # 是否启用NaN处理
    cfg: Optional['TrainingConfig'] = None,  # 添加配置参数
) -> Tuple[float, float, float, float, float, float, float, float]:
    """训练一个epoch"""
    model.train()
    
    # 训练统计累积器
    epoch_losses = []
    epoch_losses_main = []
    epoch_losses_ortho = []
    epoch_losses_var = []  # 🎯 方差惩罚统计
    epoch_grad_norms = []
    epoch_pearson_ics = []
    epoch_spearman_ics = []
    epoch_pred_means = []  # 预测真实均值，不是绝对值均值
    epoch_pred_stds = []
    
    # 数值稳定监控
    overflow_count = 0
    total_batches = 0
    skipped_batches = 0  # 因为标签全NaN而跳过的batch数（仅在启用NaN处理时使用）
    
    pbar = tqdm(loader, desc='Train', leave=False)
    for batch_idx, batch_data in enumerate(pbar):
        total_batches += 1
        
        # 计算全局步数
        global_step = (epoch-1) * len(loader) + batch_idx if epoch is not None else batch_idx
        
        # 解包数据
        if len(batch_data) == 4:
            feats, labels, dates, _ = batch_data
        else:
            feats, labels = batch_data
            dates = None
            
        feats = feats.to(device).float()
        labels = labels.unsqueeze(1).to(device).float()
        
        # ========== 条件性的标签NaN处理 ==========
        if enable_nan_handling:
            # 启用NaN处理：屏蔽NaN标签
            label_mask = ~torch.isnan(labels)
            valid_labels_count = label_mask.sum().item()
            
            if valid_labels_count == 0:
                # 整个batch标签全是NaN，跳过本batch
                skipped_batches += 1
                continue
        else:
            # 不启用NaN处理：假设所有标签都有效
            label_mask = torch.ones_like(labels, dtype=torch.bool)
            valid_labels_count = labels.numel()
        # =======================================
        
        # 标签日期标准化
        if standardize_labels_by_date and dates is not None:
            if date_to_idx is None or means_tensor is None or stds_tensor is None:
                raise ValueError("缺少日期标准化参数")
            
            # 处理未知日期：使用全局统计作为默认值
            global_mean = means_tensor.mean().item()
            global_std = stds_tensor.mean().item()
            
            batch_means = []
            batch_stds = []
            
            for date in dates:
                if date in date_to_idx:
                    idx = date_to_idx[date]
                    batch_means.append(means_tensor[idx].item())
                    batch_stds.append(stds_tensor[idx].item())
                else:
                    # 遇到未知日期，使用全局统计
                    batch_means.append(global_mean)
                    batch_stds.append(global_std)
            
            # 转换为tensor
            m = torch.tensor(batch_means, device=device).unsqueeze(1)
            s = torch.tensor(batch_stds, device=device).unsqueeze(1)
            labels = (labels - m) / (s + 1e-3)

        optimizer.zero_grad(set_to_none=True)
        
        with autocast(device_type='cuda', enabled=use_amp):
            preds, fv = model(feats)
            
            # 🎯 组合损失：与训练保持一致
            lambda_ic = cfg.lambda_ic if cfg is not None else 0.6
            
            if enable_nan_handling:
                # NaN处理：只对有效标签计算损失
                valid_preds = preds[label_mask]
                valid_labels = labels[label_mask]
                
                if valid_preds.numel() == 0:  # 该batch所有标签都是NaN
                    skipped_batches += 1
                    continue
                    
                loss_main, loss_ic_part, loss_huber_part, loss_var_part = combined_loss(
                    valid_preds, valid_labels, lambda_ic, huber_delta=1.0,
                    lambda_var=getattr(cfg, 'lambda_var', 0.05),
                    target_std=getattr(cfg, 'target_std', 1.0)
                )
            else:
                # 标准情况：直接计算组合损失
                loss_main, loss_ic_part, loss_huber_part, loss_var_part = combined_loss(
                    preds, labels, lambda_ic, huber_delta=1.0,
                    lambda_var=getattr(cfg, 'lambda_var', 0.05),
                    target_std=getattr(cfg, 'target_std', 1.0)
                )
            
            # 正交惩罚（保持原有逻辑）
            loss_ortho = alpha_corr * orthogonality_penalty(fv, eps=1e-3)
            
            # 总损失 = 主损失(IC+Huber) + 正交惩罚
            loss = loss_main + loss_ortho

        # 反向传播
        if scaler is not None:
            # 🎯 检查loss是否为inf/nan，及时跳过坏step
            if not torch.isfinite(loss):
                warnings.warn(f"Loss为inf/nan，跳过此步骤: loss={loss.item()}")
                overflow_count += 1
                scaler.update()
                continue
                
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            
            # 梯度裁剪和监控
            total_norm = nn.utils.clip_grad_norm_(model.parameters(), grad_clip_norm)
            overflow = not torch.isfinite(total_norm)
            
            if overflow:
                warnings.warn(f"梯度溢出检测到，跳过此步骤")
                overflow_count += 1
                scaler.update()
                continue
            
            scaler.step(optimizer)
            scaler.update()
        else:
            # 🎯 检查loss是否为inf/nan，及时跳过坏step  
            if not torch.isfinite(loss):
                warnings.warn(f"Loss为inf/nan，跳过此步骤: loss={loss.item()}")
                overflow_count += 1
                continue
                
            loss.backward()
            total_norm = nn.utils.clip_grad_norm_(model.parameters(), grad_clip_norm)
            
            if torch.isfinite(total_norm):
                optimizer.step()
            else:
                warnings.warn(f"梯度NaN/Inf检测到，跳过此步骤")
                overflow_count += 1
                continue
            
        # 累积统计（用于epoch级别汇总）
        epoch_losses.append(loss.item())
        epoch_losses_main.append(loss_main.item())
        epoch_losses_ortho.append(loss_ortho.item())
        epoch_losses_var.append(loss_var_part.item())  # 🎯 方差惩罚统计（原始值）
        epoch_grad_norms.append(total_norm.item())
        
        # 计算batch级别的IC指标 - 根据是否启用NaN处理选择数据
        if enable_nan_handling:
            valid_preds = preds[label_mask].detach()
            valid_labels = labels[label_mask]
        else:
            valid_preds = preds.detach()
            valid_labels = labels
        
        batch_pearson_ic = pearson_ic(valid_preds, valid_labels)
        batch_spearman_ic = spearman_ic(valid_preds, valid_labels)
        epoch_pearson_ics.append(batch_pearson_ic)
        epoch_spearman_ics.append(batch_spearman_ic)
        
        # 计算batch级别的预测分布指标
        epoch_pred_means.append(preds.mean().item())  # 真正的均值，不是绝对值均值
        epoch_pred_stds.append(preds.std().item())
        
        # 🚀 使用精简高效的TrainingMonitor进行监控
        if monitor:
            # 全面监控：Core + LayerDiag + Alerts
            monitor.monitor_comprehensive(
                model=model,
                loss=loss.item(),
                grad_norm=total_norm.item(), 
                lr=optimizer.param_groups[0]['lr'],
                predictions=valid_preds,
                labels=valid_labels,
                step=global_step,
                batch_idx=batch_idx,
                scaler=scaler
            )
        
        # 进度条显示
        if enable_nan_handling:
            pbar.set_postfix(
                loss=loss.item(), 
                grad_norm=total_norm.item(),
                valid_labels=f"{valid_labels_count}/{labels.size(0)}"
            )
        else:
            pbar.set_postfix(
                loss=loss.item(), 
                grad_norm=total_norm.item()
            )

    # 计算epoch级别的汇总统计
    avg_loss = np.mean(epoch_losses)
    avg_loss_main = np.mean(epoch_losses_main)
    avg_loss_ortho = np.mean(epoch_losses_ortho)
    avg_loss_var = np.mean(epoch_losses_var)  # 🎯 方差惩罚均值
    avg_grad_norm = np.mean(epoch_grad_norms)
    avg_pearson_ic = np.mean(epoch_pearson_ics)
    avg_spearman_ic = np.mean(epoch_spearman_ics)
    avg_pred_mean = np.mean(epoch_pred_means)
    avg_pred_std = np.mean(epoch_pred_stds)
    
    # 数值稳定性检查
    overflow_rate = overflow_count / max(total_batches, 1)
    
    if overflow_rate > 0.05:
        warnings.warn(f"Epoch {epoch}: 严重数值不稳定 ({overflow_rate:.1%} batches overflow)")
    
    if enable_nan_handling:
        skip_rate = skipped_batches / max(total_batches, 1)
        if skip_rate > 0.01:
            warnings.warn(f"Epoch {epoch}: 标签NaN较多 ({skip_rate:.1%} batches skipped, {skipped_batches}/{total_batches})")

    return (
        avg_loss, avg_loss_main, avg_loss_ortho, avg_loss_var,
        avg_pred_std, avg_pred_mean, avg_grad_norm,
        avg_pearson_ic, avg_spearman_ic
    )


@torch.no_grad()
def evaluate(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
    alpha_corr: float,
    use_amp: bool,
    standardize_labels_by_date: bool = False,
    date_to_idx: Optional[Dict[str, int]] = None,
    means_tensor: Optional[torch.Tensor] = None,
    stds_tensor: Optional[torch.Tensor] = None,
    split: str = 'val',  # 'val' or 'test'
    epoch: Optional[int] = None,
    enable_nan_handling: bool = False,  # 是否启用NaN处理
    cfg: Optional['TrainingConfig'] = None,
) -> Tuple[float, float, float, float, float]:
    """评估模型"""
    model.eval()
    
    # 评估统计累积器
    epoch_losses_main = []
    epoch_losses_ortho = []
    epoch_losses_var = []  # 🎯 方差惩罚统计
    epoch_losses_total = []
    epoch_pearson_ics = []
    epoch_spearman_ics = []
    epoch_pred_means = []
    epoch_pred_stds = []
    
    # 标签NaN统计（仅在启用NaN处理时使用）
    total_batches = 0
    skipped_batches = 0
    
    pbar = tqdm(loader, desc=f'{split.title()}', leave=False)
    for batch_data in pbar:
        total_batches += 1
        
        if len(batch_data) == 4:
            feats, labels, dates, _ = batch_data
        else:
            feats, labels = batch_data
            
        feats = feats.to(device).float()
        labels = labels.unsqueeze(1).to(device).float()
        
        # ========== 条件性的标签NaN处理 ==========
        if enable_nan_handling:
            # 启用NaN处理：屏蔽NaN标签
            label_mask = ~torch.isnan(labels)
            valid_labels_count = label_mask.sum().item()
            
            if valid_labels_count == 0:
                # 整个batch标签全是NaN，跳过本batch
                skipped_batches += 1
                continue
        else:
            # 不启用NaN处理：假设所有标签都有效
            label_mask = torch.ones_like(labels, dtype=torch.bool)
            valid_labels_count = labels.numel()
        # =======================================
        
        # 标签日期标准化
        if standardize_labels_by_date and dates is not None:
            if date_to_idx is None or means_tensor is None or stds_tensor is None:
                raise ValueError("缺少日期标准化参数")
            
            # 处理未知日期：使用全局统计作为默认值
            global_mean = means_tensor.mean().item()
            global_std = stds_tensor.mean().item()
            
            batch_means = []
            batch_stds = []
            
            for date in dates:
                if date in date_to_idx:
                    idx = date_to_idx[date]
                    batch_means.append(means_tensor[idx].item())
                    batch_stds.append(stds_tensor[idx].item())
                else:
                    # 遇到未知日期，使用全局统计
                    batch_means.append(global_mean)
                    batch_stds.append(global_std)
            
            # 转换为tensor
            m = torch.tensor(batch_means, device=device).unsqueeze(1)
            s = torch.tensor(batch_stds, device=device).unsqueeze(1)
            labels = (labels - m) / (s + 1e-3)
        
        with autocast(device_type='cuda', enabled=use_amp):
            preds, fv = model(feats)
        
            # 🎯 组合损失：与训练保持一致
            lambda_ic = cfg.lambda_ic if cfg is not None else 0.6
            
            if enable_nan_handling:
                # NaN处理：只对有效标签计算损失
                valid_preds = preds[label_mask]
                valid_labels = labels[label_mask]
                
                if valid_preds.numel() == 0:  # 该batch所有标签都是NaN
                    skipped_batches += 1
                    continue
                    
                loss_main, loss_ic_part, loss_huber_part, loss_var_part = combined_loss(
                    valid_preds, valid_labels, lambda_ic, huber_delta=1.0,
                    lambda_var=getattr(cfg, 'lambda_var', 0.05),
                    target_std=getattr(cfg, 'target_std', 1.0)
                )
            else:
                # 标准情况：直接计算组合损失
                loss_main, loss_ic_part, loss_huber_part, loss_var_part = combined_loss(
                    preds, labels, lambda_ic, huber_delta=1.0,
                    lambda_var=getattr(cfg, 'lambda_var', 0.05),
                    target_std=getattr(cfg, 'target_std', 1.0)
                )
                
            loss_ortho = alpha_corr * orthogonality_penalty(fv, eps=1e-3)
            loss_total = loss_main + loss_ortho
        
        # 累积统计
        epoch_losses_main.append(loss_main.item())
        epoch_losses_ortho.append(loss_ortho.item())
        epoch_losses_var.append(loss_var_part.item())  # 🎯 方差惩罚统计
        epoch_losses_total.append(loss_total.item())
        
        # 计算IC指标 - 根据是否启用NaN处理选择数据
        if enable_nan_handling:
            valid_preds = preds[label_mask].detach()
            valid_labels = labels[label_mask]
        else:
            valid_preds = preds.detach()
            valid_labels = labels
        
        batch_pearson_ic = pearson_ic(valid_preds, valid_labels)
        batch_spearman_ic = spearman_ic(valid_preds, valid_labels)
        epoch_pearson_ics.append(batch_pearson_ic)
        epoch_spearman_ics.append(batch_spearman_ic)
        
        # 计算预测分布指标
        epoch_pred_means.append(preds.mean().item())  # 真正的均值，不是绝对值均值
        epoch_pred_stds.append(preds.std().item())
        
        # 进度条显示
        if enable_nan_handling:
            pbar.set_postfix(
                loss=loss_total.item(),
                valid_labels=f"{valid_labels_count}/{labels.size(0)}"
            )
        else:
            pbar.set_postfix(loss=loss_total.item())

    # 计算epoch级别汇总
    avg_loss_main = np.mean(epoch_losses_main)
    avg_loss_ortho = np.mean(epoch_losses_ortho)
    avg_loss_total = np.mean(epoch_losses_total)
    avg_pearson_ic = np.mean(epoch_pearson_ics)
    avg_spearman_ic = np.mean(epoch_spearman_ics)
    avg_pred_mean = np.mean(epoch_pred_means)  # 真正的预测均值
    avg_pred_std = np.mean(epoch_pred_stds)
    
    # 标签NaN统计（仅在启用NaN处理时报告）
    if enable_nan_handling:
        skip_rate = skipped_batches / max(total_batches, 1)
        if skip_rate > 0.01:
            warnings.warn(f"{split.title()} 评估: 标签NaN较多 ({skip_rate:.1%} batches skipped, {skipped_batches}/{total_batches})")

    return (
        avg_pearson_ic, avg_spearman_ic, avg_loss_ortho,  # avg_corr使用正交损失
        avg_pred_mean, avg_pred_std,
    )


# --------------------------------------------------
# 主训练函数
# --------------------------------------------------

def run_training(cfg: TrainingConfig):
    """主训练逻辑"""
    # 设置随机种子
    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)
    random.seed(cfg.seed)
    
    # 自适应梯度爆炸阈值：根据隐藏层大小动态调整
    cfg.gradient_explosion_threshold = max(
        cfg.gradient_explosion_threshold,
        10.0 * (cfg.hidden_size ** 0.5)
    )

    # 日志设置
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("train_dfzq_gru")
    logger.info(f"开始训练，配置: {cfg}")
    logger.info(f"自适应梯度爆炸阈值: {cfg.gradient_explosion_threshold:.2f} (基于hidden_size={cfg.hidden_size})")

    device = torch.device("cuda" if torch.cuda.is_available() and not cfg.force_cpu else "cpu")
    logger.info(f"使用设备: {device}")

    # 输出目录设置
    experiment_info = get_experiment_summary(cfg)
    output_dir = experiment_info['output_dir']
    run_name = experiment_info['run_name']
    
    # 创建实验目录
    dirs = create_experiment_dirs(output_dir)
    ckpt_dir = dirs['ckpt']
    log_dir = dirs['logs']
    bt_dir = dirs['bt_results']
    
    logger.info(f"实验输出目录: {output_dir}")
    print(f"📁 实验目录: {output_dir}")

    # 数据加载
    dl_cfg = {
        "dataset_path": cfg.dataset_path,
        "batch_size": cfg.batch_size,
        "num_workers": cfg.num_workers,
        "shuffle": cfg.shuffle,
        "seed": cfg.seed,
        "chunk_size": cfg.chunk_size,
        "memory_limit": cfg.memory_limit,
        "use_fixed_indices": cfg.use_fixed_indices,
    }
    
    train_loader, valid_loader, test_loader = get_train_valid_test_loaders(
        dl_cfg, 
        keep_meta_train=cfg.standardize_labels_by_date,
        keep_meta_eval=cfg.standardize_labels_by_date,
        max_samples_train=cfg.max_samples_train,
        max_samples_valid=cfg.max_samples_valid,
        max_samples_test=cfg.max_samples_test,
        use_fixed_indices=cfg.use_fixed_indices,
        enable_nan_handling=cfg.enable_nan_handling
    )
    
    # 日期标准化设置
    date_to_idx = None
    means_tensor = None
    stds_tensor = None
    
    if cfg.standardize_labels_by_date:
        stats_path = Path(dirs['root']) / "date_label_stats.pkl"
        logger.info("计算日期标签统计信息...")
        
        date_stats = compute_and_save_date_stats(
                [train_loader, valid_loader], 
                device,
                cache_file=str(stats_path)
            )
                
        # 构建映射
        date_list = list(date_stats.keys())
        date_to_idx = {d: i for i, d in enumerate(date_list)}
        means_tensor = torch.tensor([date_stats[d][0] for d in date_list], device=device).unsqueeze(1)
        stds_tensor = torch.tensor([date_stats[d][1] for d in date_list], device=device).unsqueeze(1)
        
        logger.info(f"完成 {len(date_stats)} 个日期的标签统计")

    # 模型初始化
    model_cfg = DFZQGRUConfig()
    model_cfg.input_size = cfg.input_size
    model_cfg.hidden_size = cfg.hidden_size
    model_cfg.num_layers = cfg.num_layers
    model_cfg.dropout = cfg.dropout
    model_cfg.output_size = cfg.output_size
    model_cfg.bidirectional = cfg.bidirectional
    model_cfg.attention = cfg.attention
    model_cfg.input_hidden_dim = cfg.input_hidden_dim
    model_cfg.head_hidden_dim = cfg.head_hidden_dim
    
    model = DFZQGRU(model_cfg).to(device)
    
    logger.info(f"模型创建完成: {model_cfg}")

    # 记录模型架构信息
    total_params = sum(p.numel() for p in model.parameters())
    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info(f"模型参数: 总计={total_params:,}, 可训练={trainable_params:,}")
    

    
    # —— 构建优化器：对所有一维参数（bias、LN/BN γ,β）不做衰减，且使用半 LR —— 
    decay_params = []
    no_decay_params = []
    decay_param_names = []
    no_decay_param_names = []
    
    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue
            
        # 更精确的参数分类逻辑
        is_bias = "bias" in name.lower()
        is_norm = any(norm_type in name.lower() for norm_type in ["ln", "bn", "layernorm", "batchnorm", "norm"])
        is_1d = param.ndim == 1
        
        # 对于1D参数，如果不是明确的bias，都假设是norm layer的scale参数
        # 这样更安全，因为大部分重要的权重都是2D的
        if is_bias or is_norm or (is_1d and "weight" in name):
            no_decay_params.append(param)
            no_decay_param_names.append(name)
        else:
            # 2D及以上参数（权重矩阵）以及其他参数应用weight decay
            decay_params.append(param)
            decay_param_names.append(name)

    # 计算参数统计
    decay_param_count = sum(p.numel() for p in decay_params)
    no_decay_param_count = sum(p.numel() for p in no_decay_params)
    total_param_count = decay_param_count + no_decay_param_count
    
    optimizer = torch.optim.AdamW(
        [
            {'params': decay_params, 'lr': cfg.lr, 'weight_decay': cfg.weight_decay},
            {'params': no_decay_params, 'lr': cfg.lr * 0.5, 'weight_decay': 0.0}
        ],
        betas=(0.9, 0.999),
        eps=1e-8
    )
    
    # 详细参数分组报告
    logger.info(f"🔍 参数分组分析:")
    logger.info(f"  ✅ Weight Decay组: {len(decay_params)}个参数层, {decay_param_count:,}个参数 ({decay_param_count/total_param_count:.1%})")
    logger.info(f"  ❌ No Decay组: {len(no_decay_params)}个参数层, {no_decay_param_count:,}个参数 ({no_decay_param_count/total_param_count:.1%})")
    logger.info(f"  📊 总参数量: {total_param_count:,}")
    
    # 打印关键层的分组情况
    logger.info(f"🎯 Weight Decay组关键参数:")
    for name in decay_param_names:
        if any(key in name.lower() for key in ["weight", "gru", "linear", "attention", "attn"]):
            param_shape = dict(model.named_parameters())[name].shape
            logger.info(f"    ✅ {name}: {param_shape}")
    
    logger.info(f"🚫 No Decay组参数:")
    for name in no_decay_param_names:
        param_shape = dict(model.named_parameters())[name].shape
        logger.info(f"    ❌ {name}: {param_shape}")
    
    logger.info(f"学习率设置: 主参数={cfg.lr:.2e}, bias/norm参数={cfg.lr * 0.5:.2e}")
    logger.info(f"权重衰减设置: 主参数={cfg.weight_decay:.2e}, bias/norm参数=0.0")
    
    # —— 其他训练组件（如学习率调度器）可在此之后构建 —— 
    # 例如：warmup + cosine decay
    # total_steps = ...  # 根据训练集大小、batch_size、max_epochs 计算
    # warmup_steps = int(total_steps * 0.05)
    # scheduler = get_cosine_schedule_with_warmup(
    #     optimizer,
    #     num_warmup_steps=warmup_steps,
    #     num_training_steps=total_steps
    # )

    # —— 梯度裁剪阈值：从配置文件读取 —— 
    grad_clip_norm = cfg.grad_clip_norm

    # 混合精度
    scaler = GradScaler(device="cuda", enabled=cfg.use_amp and device.type == 'cuda') if cfg.use_amp else None

    # 学习率调度器
    if cfg.lr_scheduler_type == "warm_cos":
        warmup_scheduler = LinearLR(
            optimizer,
            start_factor=cfg.lr_scheduler_warmup_start_factor,
            end_factor=1.0,
            total_iters=cfg.lr_scheduler_warmup_epochs
        )
        cosine_scheduler = CosineAnnealingLR(
            optimizer,
            T_max=cfg.max_epochs - cfg.lr_scheduler_warmup_epochs,
            eta_min=cfg.lr_scheduler_min_lr
        )
        scheduler = SequentialLR(
            optimizer,
            schedulers=[warmup_scheduler, cosine_scheduler],
            milestones=[cfg.lr_scheduler_warmup_epochs]
        )
        scheduler_step = lambda _: scheduler.step()
    elif cfg.lr_scheduler_type == "plateau":
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, mode="max",
            patience=cfg.lr_scheduler_patience,
            factor=cfg.lr_scheduler_factor,
            min_lr=cfg.lr_scheduler_min_lr,
            threshold_mode="rel",  # 使用相对阈值模式，更稳定
            threshold=0.01,        # 相对改善阈值1%
            cooldown=2            # 降低学习率后等待2个epoch再监控
        )
        scheduler_step = lambda metric: scheduler.step(metric)
    else:
        raise ValueError(f"未知调度器类型: {cfg.lr_scheduler_type}")

    best_ckpt_path = Path(ckpt_dir) / "best_model.pth"
    log_csv_path = Path(log_dir) / "training_log.csv"
    
    writer = SummaryWriter(log_dir=str(log_dir), flush_secs=30, max_queue=1000)
    stopper = SimpleEarlyStop(patience=cfg.patience)

    # 🚀 精简高效监控配置 - 三大页签架构
    monitor_config = {
        "detailed_log_freq": 500,              # 🔥 大幅减少LayerDiag监控频率  
        "full_monitor_batches": 3,             # 🔥 减少前期完全监控batch数
        "grad_explosion_threshold": 10.0,      # 梯度爆炸阈值
        "grad_vanishing_threshold": 1e-4,      # 梯度消失阈值
        "loss_explosion_threshold": 1000.0,    # 损失爆炸阈值
        "amp_scale_min_threshold": 2.0,        # AMP缩放最小阈值
        "activation_to_cpu": False,            # 🔥 关闭GPU->CPU传输，提升性能  
        "activation_sample_size": 500,         # 🔥 减少采样数量，提升性能
    }
    
    monitor = TrainingMonitor(writer, monitor_config)
    monitor.setup_model_monitoring(model)
    
    logger.info(f"📊 精简高效监控器设置完成")
    logger.info(f"   🎯 三大页签: Core + LayerDiag + Alerts")
    logger.info(f"   ⚡ 性能目标: <2ms/step, <100个写盘点位")

    # 训练循环
    history = []
    best_ic = -np.inf
    
    logger.info(f"开始训练，总轮数: {cfg.max_epochs}")
    
    try:
        for epoch in tqdm(range(1, cfg.max_epochs + 1), desc="Training"):
            # 训练
            (train_loss, train_loss_main, train_loss_ortho, train_loss_var,
             train_preds_std, train_preds_mean, train_grad_norm,
             train_pearson_ic, train_spearman_ic) = train_one_epoch(
                model, train_loader, optimizer, scaler, device,
                cfg.alpha_corr, cfg.use_amp and device.type == 'cuda',
                grad_clip_norm, monitor, epoch,
                cfg.standardize_labels_by_date,
                date_to_idx, means_tensor, stds_tensor,
                cfg.gradient_explosion_threshold,
                cfg.gradient_vanishing_threshold,
                cfg.enable_nan_handling,
                cfg
            )
            
            # 验证
            val_pearson_ic, val_spearman_ic, val_loss_ortho, val_preds_mean, val_preds_std = evaluate(
                model, valid_loader, device, cfg.alpha_corr,
                cfg.use_amp and device.type == 'cuda',
                cfg.standardize_labels_by_date,
                date_to_idx, means_tensor, stds_tensor,
                'val', epoch, cfg.enable_nan_handling,
                cfg
            )

            # 记录日志
            history.append((
                epoch, train_loss, train_loss_main, train_loss_ortho, train_loss_var,
                train_pearson_ic, train_spearman_ic,
                val_pearson_ic, val_spearman_ic, val_loss_ortho,
                train_preds_std, train_preds_mean, train_grad_norm
            ))
            
            log_msg = (
                f"Epoch {epoch:03d}/{cfg.max_epochs} | "
                f"Train Loss: {train_loss:.6f} | "
                f"Train Pearson IC: {train_pearson_ic:.6f} | "
                f"Train Spearman IC: {train_spearman_ic:.6f} | "
                f"Val Pearson IC: {val_pearson_ic:.6f} | "
                f"Val Spearman IC: {val_spearman_ic:.6f} | "
                f"LR: {optimizer.param_groups[0]['lr']:.2e}"
            )
            logger.info(log_msg)
            print(log_msg)
            
            # 使用monitor记录epoch级别指标
            train_metrics = {
                "loss": train_loss,
                "loss_main": train_loss_main,
                "loss_ortho": train_loss_ortho,
                "loss_var": train_loss_var,  # 🎯 方差惩罚指标
                "pearson_ic": train_pearson_ic,
                "spearman_ic": train_spearman_ic,
                "pred_std": train_preds_std,
                "pred_mean": train_preds_mean,
                "grad_norm": train_grad_norm,
            }
            
            val_metrics = {
                "pearson_ic": val_pearson_ic,
                "spearman_ic": val_spearman_ic,
                "ortho_penalty": val_loss_ortho,
                "pred_std": val_preds_std,
                "pred_mean": val_preds_mean,
            }
            
            monitor.log_epoch_summary(epoch, train_metrics, val_metrics)
            
            # 同时保持原有的TensorBoard记录（向后兼容）
            writer.add_scalar("Loss/train", train_loss, epoch)
            writer.add_scalar("Loss/train_main", train_loss_main, epoch)
            writer.add_scalar("Loss/train_ortho", train_loss_ortho, epoch)
            writer.add_scalar("Loss/train_var_raw", train_loss_var, epoch)  # 🎯 原始方差惩罚
            writer.add_scalar("Loss/train_var_weighted", train_loss_var * getattr(cfg, 'lambda_var', 0.05), epoch)  # 🎯 加权方差惩罚
            writer.add_scalar("Variance/train_pred_std", train_preds_std, epoch)  # 🎯 预测标准差监控
            writer.add_scalar("Variance/val_pred_std", val_preds_std, epoch)  # 🎯 验证预测标准差
            writer.add_scalar("IC/train_pearson", train_pearson_ic, epoch)
            writer.add_scalar("IC/train_spearman", train_spearman_ic, epoch)
            writer.add_scalar("IC/val_pearson", val_pearson_ic, epoch)
            writer.add_scalar("IC/val_spearman", val_spearman_ic, epoch)
            writer.add_scalar("LR", optimizer.param_groups[0]["lr"], epoch)
            
            # 早停和最佳模型保存
            combined_ic = val_pearson_ic * 0.5 + val_spearman_ic * 0.5
            stopper(combined_ic)
                
            if combined_ic > best_ic + 1e-6:
                best_ic = combined_ic
                torch.save({
                    "epoch": epoch,
                    "model_state_dict": model.state_dict(),
                    "optimizer_state_dict": optimizer.state_dict(),
                    "scaler_state_dict": scaler.state_dict() if scaler and scaler.is_enabled() else None,
                    "best_ic": best_ic,
                    "training_config": cfg.__dict__
                }, best_ckpt_path)
                logger.info(f"保存最佳模型到 {best_ckpt_path}，Epoch {epoch}，Combined Val IC: {best_ic:.6f}")
                
                # 记录最佳模型到TensorBoard
                writer.add_scalar("BestModel/epoch", epoch, epoch)
                writer.add_scalar("BestModel/combined_ic", best_ic, epoch)
            
            if stopper.should_stop:
                logger.info(f"早停触发，Epoch {epoch}")
                writer.add_scalar("EarlyStop/epoch", epoch, epoch)
                break
                        
            writer.flush()
            scheduler_step(combined_ic)

        writer.close()
        
        # 保存训练日志
        pd.DataFrame(history, columns=[
            "epoch", "train_loss", "train_loss_main", "train_loss_ortho", "train_loss_var",
            "train_pearson_ic", "train_spearman_ic",
            "val_pearson_ic", "val_spearman_ic", "val_loss_ortho",
            "preds_std", "preds_mean", "grad_norm"
        ]).to_csv(log_csv_path, index=False)
        logger.info(f"训练日志保存到 {log_csv_path}")

        # 测试最佳模型
        if best_ckpt_path.exists():
            logger.info(f"加载最佳模型进行测试: {best_ckpt_path}")
            checkpoint = torch.load(best_ckpt_path, map_location=device, weights_only=False)
            model.load_state_dict(checkpoint["model_state_dict"])
            logger.info(f"最佳模型加载完成，Epoch: {checkpoint.get('epoch', 'N/A')}, Val IC: {checkpoint.get('best_ic', 'N/A'):.6f}")

        test_pearson_ic, test_spearman_ic, test_loss_ortho, test_preds_mean, test_preds_std = evaluate(
            model, test_loader, device, cfg.alpha_corr,
            cfg.use_amp and device.type == 'cuda',
            cfg.standardize_labels_by_date,
            date_to_idx, means_tensor, stds_tensor,
            'test', epoch, cfg.enable_nan_handling,
            cfg
        )
            
        test_combined_ic = test_pearson_ic * 0.5 + test_spearman_ic * 0.5
            
        # 记录最终测试结果到TensorBoard
        writer.add_scalar("Test/pearson_ic", test_pearson_ic, epoch)
        writer.add_scalar("Test/spearman_ic", test_spearman_ic, epoch)
        writer.add_scalar("Test/combined_ic", test_combined_ic, epoch)
        writer.add_scalar("Test/ortho_penalty", test_loss_ortho, epoch)
        writer.add_scalar("Test/pred_mean", test_preds_mean, epoch)
        writer.add_scalar("Test/pred_std", test_preds_std, epoch)
            
        logger.info(f"测试结果 | Pearson IC={test_pearson_ic:.6f} | "
                   f"Spearman IC={test_spearman_ic:.6f} | "
                   f"Combined IC={test_combined_ic:.6f} | "
                   f"OrthoPenalty={test_loss_ortho:.6f}")
        print(f"测试结果 | Pearson IC={test_pearson_ic:.6f} | "
              f"Spearman IC={test_spearman_ic:.6f} | "
              f"Combined IC={test_combined_ic:.6f} | "
              f"OrthoPenalty={test_loss_ortho:.6f}")

        print("\n" + "="*60)
        print("🎉 训练完成！")
        print(f"📊 TensorBoard: tensorboard --logdir {log_dir}")
        print("="*60)

    except KeyboardInterrupt:
        logger.info("训练被用户中断")
        print("\n⚠️ 训练被用户中断")
    except Exception as e:
        logger.error(f"训练过程中出现错误: {e}")
        print(f"\n❌ 训练出错: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        # 清理监控资源
        if 'monitor' in locals():
            monitor.cleanup_hooks()
            logger.info("监控资源清理完成")


def main():
    """训练入口点"""
    cfg = TrainingConfig()
    run_training(cfg)


if __name__ == "__main__":
    main()
