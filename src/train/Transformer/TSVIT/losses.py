# -*- coding: utf-8 -*-
"""
TSViT 损失函数模块 - 简洁的损失函数实现
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Tuple, Optional


def pearson_ic_loss(preds: torch.Tensor, labels: torch.Tensor, eps: float = 1e-8) -> torch.Tensor:
    """
    Pearson IC损失：loss = 1 - ρ(pred, label)
    
    Args:
        preds: [B] 预测值
        labels: [B] 真实值  
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
    
    # Pearson相关系数 → IC损失
    corr = cov / (p_std * l_std)
    return 1.0 - corr


def rank_ic_loss(preds: torch.Tensor, labels: torch.Tensor, eps: float = 1e-8) -> torch.Tensor:
    """
    基于排序的IC损失：优化预测排序与真实排序的一致性
    
    Args:
        preds: [B] 预测值
        labels: [B] 真实值
        eps: 数值稳定性小量
    
    Returns:
        loss: 标量，排序IC损失
    """
    if preds.numel() <= 1:
        return torch.tensor(0.0, device=preds.device)
    
    # 计算排序
    pred_ranks = torch.argsort(torch.argsort(-preds)).float()
    label_ranks = torch.argsort(torch.argsort(-labels)).float()
    
    # 计算排序相关性
    pred_centered = pred_ranks - pred_ranks.mean()
    label_centered = label_ranks - label_ranks.mean()
    
    cov = (pred_centered * label_centered).mean()
    pred_std = pred_centered.std() + eps
    label_std = label_centered.std() + eps
    
    corr = cov / (pred_std * label_std)
    return 1.0 - corr


def get_loss_function(*args, **kwargs):
    raise NotImplementedError("loss函数由 combined_loss 固定组成，不再通过 name 选择。")


def _weighted_stats(x: torch.Tensor, w: torch.Tensor, eps: float = 1e-8):
    """返回加权均值与加权方差（带数值稳定项）"""
    ws = w.sum() + eps
    mx = (w * x).sum() / ws
    vx = (w * (x - mx)**2).sum() / ws + eps
    return mx, vx


def weighted_corr_loss(
    preds: torch.Tensor,
    labels: torch.Tensor,
    w: torch.Tensor = None,
    eps: float = 1e-8,
) -> torch.Tensor:
    """
    加权相关系数损失：1 - corr_w(preds, labels)
    相关系数具有尺度不变与[-1,1]有界的性质，可避免预测尺度无界增大。
    """
    preds = preds.squeeze(-1) if preds.ndim > 1 else preds
    labels = labels.squeeze(-1) if labels.ndim > 1 else labels
    if w is None:
        w = torch.ones_like(preds)

    mp, vp = _weighted_stats(preds, w, eps)
    ml, vl = _weighted_stats(labels, w, eps)
    cov = (w * (preds - mp) * (labels - ml)).sum() / (w.sum() + eps)
    denom = torch.sqrt(vp * vl).clamp_min(1e-12)
    corr = cov / denom
    return 1.0 - corr


def weighted_cov_loss(
    preds: torch.Tensor,
    labels: torch.Tensor,
    w: torch.Tensor = None,
    eps: float = 1e-8,
) -> torch.Tensor:
    """
    加权协方差损失：loss = - cov_w(preds, labels)
    提供可与 weighted_corr_loss 互换的协方差版本（用于测试尺度敏感目标）。
    注意：该损失对预测尺度敏感，可能推动预测幅度增大。
    """
    preds = preds.squeeze(-1) if preds.ndim > 1 else preds
    labels = labels.squeeze(-1) if labels.ndim > 1 else labels
    if w is None:
        w = torch.ones_like(preds)

    mp, _ = _weighted_stats(preds, w, eps)
    ml, _ = _weighted_stats(labels, w, eps)
    cov = (w * (preds - mp) * (labels - ml)).sum() / (w.sum() + eps)
    return -cov


def _safe_mean(x: torch.Tensor) -> torch.Tensor:
    return x.mean() if x.numel() > 0 else x.new_zeros(())


def quantile_huber_loss(
    preds: torch.Tensor,
    labels: torch.Tensor,
    delta: float = 1.0,
    tau: float = 0.6,
    reduction: str = "mean",
) -> torch.Tensor:
    """
    分位（偏置）Huber：对“低估”与“高估”赋予不对称权重。
    残差 r = labels - preds：
      - r >= 0: 模型低估（preds < labels），权重 tau
      - r <  0: 模型高估（preds > labels），权重 (1 - tau)
    取 tau > 0.5 ⇨ 更重惩罚“低估”（偏多头）。
    """
    preds = preds.squeeze(-1) if preds.ndim > 1 else preds
    labels = labels.squeeze(-1) if labels.ndim > 1 else labels

    r = labels - preds
    abs_r = r.abs()

    hub = torch.where(
        abs_r <= delta,
        0.5 * r * r,
        delta * (abs_r - 0.5 * delta)
    )

    loss = torch.where(r >= 0, tau * hub, (1.0 - tau) * hub)

    if reduction == "mean":
        return _safe_mean(loss)
    elif reduction == "sum":
        return loss.sum()
    else:
        return loss


def linear_weighted_cov_loss(
    preds: torch.Tensor,
    labels: torch.Tensor,
    eps: float = 1e-8,
) -> torch.Tensor:
    """
    基线的线性加权协方差损失（临时添加，用于对齐基线）
    权重 wi = (n-1-rank)/(n-1)，按预测值降序排列
    返回负协方差以供最小化
    """
    preds = preds.squeeze(-1) if preds.ndim > 1 else preds
    labels = labels.squeeze(-1) if labels.ndim > 1 else labels

    n = preds.numel()
    if n <= 1:
        return preds.new_zeros(())

    order = torch.argsort(-preds)              # 预测值从大到小
    rank = torch.arange(n, device=preds.device).float()
    w = (n - 1 - rank) / (n - 1)               # 最好样本 w=1，最差 w=0
    w = w[order]                               # 权重对齐到排序后样本

    x, y = preds[order], labels[order]

    mu_x = (w * x).sum() / (w.sum() + eps)     # 加权均值
    mu_y = (w * y).sum() / (w.sum() + eps)

    cov_xy = (w * (x - mu_x) * (y - mu_y)).sum() / (w.sum() + eps)
    
    return -cov_xy  # 返回负值以供最小化


def orthogonality_penalty(fv: torch.Tensor, eps: float = 1e-6, alpha_diag: float = 1.0) -> torch.Tensor:
    """
    稳定的正交惩罚实现（去掉梯度死区，防止维度塌缩）
    
    Args:
        fv: [B, Dh] 特征向量
        eps: 数值稳定项（用+eps替代clamp避免梯度死区）
        alpha_diag: 对角项约束权重（防止维度塌缩）
    """
    if fv.size(0) <= 1:
        return torch.tensor(0.0, device=fv.device)
        
    # 中心化
    x = fv - fv.mean(dim=0, keepdim=True)
    
    # 标准化（用+eps避免clamp的梯度死区）
    std = torch.sqrt(x.var(dim=0, unbiased=False) + eps)
    x_norm = x / std.unsqueeze(0)
    
    # 相关矩阵
    n = x.size(0)
    corr = (x_norm.T @ x_norm) / n  # [Dh, Dh]
    
    # 只惩罚非对角元素
    off_diag = corr - torch.diag(torch.diag(corr))
    loss_off = (off_diag ** 2).mean()
    
    # 对角项约束（Barlow Twins风格）：防止维度塌缩
    loss_diag = ((torch.diag(corr) - 1.0) ** 2).mean()
    
    return loss_off + alpha_diag * loss_diag


def weighted_wic_qhuber_loss(
    preds: torch.Tensor,
    labels: torch.Tensor,
    wic_mode: str = "corr",  # "corr" | "cov"
    lambda_wic: float = 0.7,
    huber_delta: float = 1.0,
    huber_tau: float = 0.6,
    focus: str = "long_top",  # "symmetric" | "long_top" | "topk" | "label_pos" | "baseline"
    topk: float = 0.2,
    debug_weights: bool = False,  # 是否打印权重调试信息
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    加权wIC + 分位Huber组合损失：λ_wic * (1 - weighted_corr) + (1-λ_wic) * Quantile-Huber
    - wIC: 加权相关系数（尺度不变，有界），可配合基于预测排序的权重以偏向"多头/top"
    - Huber: 分位（不对称）Huber，tau>0.5 时更重惩罚低估（多头友好）
    """
    preds = preds.squeeze(-1) if preds.ndim > 1 else preds
    labels = labels.squeeze(-1) if labels.ndim > 1 else labels

    # 基于预测排序/标签的权重（不让梯度穿过权重构造）
    # 🎯 focus策略详解：
    if focus == "long_top":
        # 预测驱动线性权重：按预测值排序，高预测值→高权重（易产生自循环偏差）
        with torch.no_grad():
            n = preds.numel()
            order = torch.argsort(-preds)
            inv_order = torch.argsort(order)
            rank = torch.arange(n, device=preds.device, dtype=preds.dtype)
            lin = (n - rank) / max(n, 1)  # 线性递减：[1.0, ..., 0.0]
            floor = 0.15                   # 最低权重15%，避免完全忽略bottom样本
            rank_weight = floor + (1.0 - floor) * lin
            w = rank_weight[inv_order]
    elif focus == "topk":
        # 硬截断Top-K：只关注预测值最高的K个样本，其余权重为0
        with torch.no_grad():
            n = preds.numel()
            if isinstance(topk, float) and 0 < topk < 1:
                k = int(max(1, round(n * topk)))  # 比例模式：如topk=0.2表示前20%
            else:
                k = int(max(1, min(n, int(topk))))  # 绝对数量模式
            vals, idx = torch.topk(preds, k)
            w = torch.zeros_like(preds)
            w.scatter_(0, idx, 1.0)  # 选中样本权重1.0，其余0.0
    elif focus == "label_pos":
        # 标签驱动正向权重：修复版本（解决负样本权重为0的问题）
        with torch.no_grad():
            n = labels.numel()
            if n <= 1:
                w = torch.ones_like(labels)
            else:
                # 1) 横截面标准化，去掉尺度影响
                l = labels - labels.mean()
                l_std = labels.std().clamp_min(1e-6)
                l_norm = l / l_std
                
                # 2) 修复权重分配策略：使用平滑的多头偏向函数
                # 不再使用F.relu，而是给正负样本都分配合理权重，但偏向正样本
                base_weight = 0.3  # 基础权重，所有样本都有
                pos_bonus = 0.7    # 正样本额外权重
                
                # 使用sigmoid函数实现平滑的权重分配：
                # - 正样本得到更高权重 (base_weight + pos_bonus * sigmoid)
                # - 负样本也有合理权重 (base_weight + pos_bonus * sigmoid)
                # - 避免任何样本权重为0
                w_raw = base_weight + pos_bonus * torch.sigmoid(l_norm)
                
                # 3) 压尾，防止极少数样本主导
                if w_raw.numel() > 1:
                    hi = w_raw.quantile(0.95)
                    w = torch.clamp(w_raw, max=hi)
                else:
                    w = w_raw
                
                # 4) 检查有效样本数（现在应该接近100%）
                n_eff = (w.sum() ** 2) / ((w ** 2).sum() + 1e-8)
                # 不再需要混合策略，因为所有样本都有合理权重
                
                # 5) 归一化为 sum(w)=n
                w = w * (n / (w.sum() + 1e-8))
    elif focus == "baseline":
        # 基线模式：使用传统线性加权协方差，与学术基线对齐
        if wic_mode == "cov":
            # 对于 cov 模式，使用基线的线性加权协方差
            baseline_loss = linear_weighted_cov_loss(preds, labels, eps=1e-8)
            huber_part = torch.tensor(0.0, device=preds.device)
            return baseline_loss, baseline_loss, huber_part
        else:
            # 对于 corr 模式，使用基线权重但走正常 corr 流程
            with torch.no_grad():
                n = preds.numel()
                if n <= 1:
                    w = torch.ones_like(preds)
                else:
                    order = torch.argsort(-preds)
                    inv_order = torch.argsort(order)
                    rank = torch.arange(n, device=preds.device).float()
                    w_baseline = (n - 1 - rank) / (n - 1)
                    w = w_baseline[inv_order]  # 修复：用inv_order正确回写权重索引
    else:
        # symmetric（默认）: 均匀权重，所有样本等权重（传统无偏方式）
        w = torch.ones_like(preds)

    # 可选：打印权重调试信息（用于监控修复效果）
    if debug_weights and focus in ["label_pos", "long_top", "topk"]:
        n = w.numel()
        n_eff = (w.sum() ** 2) / ((w ** 2).sum() + 1e-8)
        w_min, w_max = w.min().item(), w.max().item()
        w_ratio = w_max / max(w_min, 1e-8)
        nonzero_rate = (w > 0.001).float().mean().item()  # 实际参与的样本比例
        
        print(f"[{focus}] n={n}, n_eff={n_eff:.1f}({n_eff/n:.3f}), "
              f"w_range=[{w_min:.4f}, {w_max:.4f}], ratio={w_ratio:.2f}, "
              f"active_rate={nonzero_rate:.3f}")
        
        # 特殊情况警告
        if n_eff < 0.1 * n:
            print(f"⚠️  有效样本数过低: {n_eff:.1f} < {0.1*n:.1f}")
        if focus == "label_pos":
            pos_labels = (labels > 0).float().mean().item()
            print(f"   正样本比例: {pos_labels:.3f}")

    if wic_mode == "cov":
        wic = weighted_cov_loss(preds, labels, w=w, eps=1e-8)
    else:
        # 默认 corr（尺度不变，更稳健）
        wic = weighted_corr_loss(preds, labels, w=w, eps=1e-8)
        
    preds_h = preds - preds.mean().detach()   # 只改Huber项的输入    

    qhuber = quantile_huber_loss(
        preds_h, labels,
        delta=huber_delta,
        tau=huber_tau,
        reduction="mean",
    )

    total = lambda_wic * wic + (1.0 - lambda_wic) * qhuber
    return total, wic, qhuber


def compute_total_loss(
    preds: torch.Tensor,
    labels: torch.Tensor,
    wic_mode: str = "corr",
    lambda_wic: float = 0.7,
    huber_delta: float = 1.0,
    huber_tau: float = 0.6,
    focus: str = "long_top",
    topk: float = 0.2,
    fv: Optional[torch.Tensor] = None,
    use_ortho: bool = False,
    alpha_corr: float = 0.01,
    debug_weights: bool = False,  # 是否打印权重调试信息
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    完整损失计算：主损失（wic + quantile-huber）+ 可选正交惩罚
    
    Returns:
        total_loss: 总损失
        wic_loss: 加权相关损失分量
        qhuber_loss: 分位Huber损失分量  
        ortho_loss: 正交惩罚分量（若未启用则为0）
    """
    # 主损失：wic + quantile-huber
    main_loss, wic_loss, qhuber_loss = weighted_wic_qhuber_loss(
        preds, labels,
        wic_mode,
        lambda_wic, huber_delta, huber_tau, focus, topk,
        debug_weights
    )
    
    # 正交惩罚（可选）
    if use_ortho and fv is not None:
        ortho_loss = alpha_corr * orthogonality_penalty(fv, eps=1e-3)
        total_loss = main_loss + ortho_loss
    else:
        ortho_loss = torch.tensor(0.0, device=preds.device)
        total_loss = main_loss
    
    return total_loss, wic_loss, qhuber_loss, ortho_loss
