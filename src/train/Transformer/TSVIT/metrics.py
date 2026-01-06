# -*- coding: utf-8 -*-
"""
TSViT 评估指标模块
"""

import torch
import torch.nn.functional as F
import numpy as np
from scipy.stats import spearmanr, pearsonr
from typing import Dict, Any


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


def spearman_ic(pred: torch.Tensor, label: torch.Tensor) -> float:
    """计算Spearman相关系数"""
    try:
        ic, _ = spearmanr(pred.detach().cpu().numpy(), label.detach().cpu().numpy(), nan_policy="omit")
        return float(ic) if not np.isnan(ic) else 0.0
    except:
        return 0.0


def compute_metrics(preds: torch.Tensor, labels: torch.Tensor) -> Dict[str, float]:
    """计算所有评估指标"""
    metrics = {}
    
    # IC 指标
    metrics['pearson_ic'] = pearson_ic(preds, labels)
    metrics['spearman_ic'] = spearman_ic(preds, labels)
    metrics['combined_ic'] = (metrics['pearson_ic'] + metrics['spearman_ic']) / 2
    
    # 预测分布指标
    metrics['pred_mean'] = preds.mean().item()
    metrics['pred_std'] = preds.std().item()
    
    # 基础回归指标
    mse = F.mse_loss(preds, labels).item()
    mae = F.l1_loss(preds, labels).item()
    metrics['mse'] = mse
    metrics['mae'] = mae
    metrics['rmse'] = np.sqrt(mse)
    
    return metrics
