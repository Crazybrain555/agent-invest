# train_dfzq_gru.py
# ==================================================
import logging, random, json
from pathlib import Path
from typing import Tuple, List, Dict, Optional, Mapping
from collections import defaultdict
import pickle
import time
import warnings

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
from src.models.rnn.gru.dfzq_gru.get_configs import DFZQGRUConfig
from src.train.Neural_networks.RNN.DFZQ_GRU.dfzq_Dataloader import (
    get_train_valid_test_loaders,
)
from src.train.Neural_networks.RNN.DFZQ_GRU.config import TrainingConfig

# --------------------------------------------------
# 0. Utilities
# --------------------------------------------------

class SimpleEarlyStop:                                     # 🔹[1]
    """Early stopping on a metric (maximize)."""
    def __init__(self, patience: int = 10, delta: float = 1e-6):
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


def orthogonality_penalty(fv: torch.Tensor, eps: float = 1e-3) -> torch.Tensor:
    """更稳定的正交惩罚实现，手工计算相关矩阵，避免torch.corrcoef潜在的数值问题
    
    Args:
        fv: 特征向量，形状为 [batch_size, hidden_size]
        eps: 数值稳定性epsilon，防止除零和方差极小导致的不稳定性
        
    Returns:
        torch.Tensor: 正交惩罚值，标量
    """
    # 安全检查
    if fv.size(0) <= 1:  # 如果批次大小为0或1，无法计算相关性
        return torch.tensor(0.0, device=fv.device)
        
    # 1. 批内零均值化
    x = fv - fv.mean(dim=0, keepdim=True)
    
    # 2. 计算方差并添加epsilon避免零方差
    var = x.var(dim=0, unbiased=False)
    var = var.clamp(min=eps)  # 确保方差不小于eps
    
    # 3. 标准化 (Z-score)
    std = torch.sqrt(var)
    x_norm = x / std.unsqueeze(0)  # 形状 [batch_size, hidden_size]
    
    # 4. 手动计算相关矩阵 (维度 [hidden_size, hidden_size])
    n = x.size(0)
    corr = (x_norm.T @ x_norm) / n
    
    # 5. 减去对角线 (只保留非对角元素)
    eye = torch.eye(corr.size(0), device=corr.device)
    corr = corr - eye
    
    # 6. 计算惩罚 (Frobenius范数的平方均值)
    penalty = (corr ** 2).mean()
    
    return penalty


def neg_pearson_loss(pred: torch.Tensor, label: torch.Tensor, eps: float = 1e-8) -> torch.Tensor:
    """计算负皮尔逊相关损失
    
    Args:
        pred: 预测值，形状为 [batch_size, 1]
        label: 标签值，形状为 [batch_size, 1]
        eps: 数值稳定性epsilon，防止除零
        
    Returns:
        torch.Tensor: 负皮尔逊相关损失，标量
    """
    pred_c = pred - pred.mean(0, keepdim=True)
    label_c = label - label.mean(0, keepdim=True)
    corr = (pred_c * label_c).mean() / (
        pred_c.std(unbiased=False) * label_c.std(unbiased=False) + eps
    )
    return -corr


def spearman_ic(pred: torch.Tensor, label: torch.Tensor) -> float:
    ic, _ = spearmanr(pred.detach().cpu().numpy(), label.detach().cpu().numpy(), nan_policy="omit")
    return float(ic)


def pearson_ic(pred: torch.Tensor, label: torch.Tensor) -> float:
    """计算皮尔逊相关系数（常规IC），测量线性相关性。"""
    pred_np = pred.detach().cpu().numpy().flatten()
    label_np = label.detach().cpu().numpy().flatten()
    valid_mask = ~(np.isnan(pred_np) | np.isnan(label_np))
    if not np.any(valid_mask) or np.sum(valid_mask) < 2:
        return 0.0  # 如果没有有效数据或数据不足，返回0
    ic, _ = pearsonr(pred_np[valid_mask], label_np[valid_mask])
    return float(ic)


# 全局变量存储每个日期的均值和标准差
DATE_STATS: Dict[str, Tuple[float, float]] = {}

def compute_and_save_date_stats(
    dataloaders: List[DataLoader],
    device: torch.device,
    cache_file: Optional[str] = None
) -> Dict[str, Tuple[float, float]]:
    """计算所有日期的标签均值和标准差
    
    Args:
        dataloaders: 数据加载器列表，应包含训练和验证集
        device: 计算设备
        cache_file: 可选的缓存文件路径，用于保存计算结果
    
    Returns:
        字典，将日期映射到(均值,标准差)元组
    """
    # 先检查是否有缓存文件
    if cache_file and Path(cache_file).exists():
        try:
            with open(cache_file, 'rb') as f:
                date_stats = pickle.load(f)
                print(f"已从缓存加载 {len(date_stats)} 个日期的统计信息")
                return date_stats
        except Exception as e:
            print(f"读取缓存失败: {e}，将重新计算")
    
    # 无缓存或读取失败，重新计算
    print("开始计算每个日期的标签统计信息...")
    start_time = time.time()
    
    # 存储每个日期的所有标签
    date_labels: Dict[str, List[float]] = defaultdict(list)
    
    # 收集所有日期的标签 (从所有提供的数据加载器)
    total_batches = 0
    for loader_idx, dataloader in enumerate(dataloaders):
        loader_name = f"数据集{loader_idx+1}" if len(dataloaders) > 1 else "数据集"
        pbar = tqdm(dataloader, desc=f'收集{loader_name}日期标签', leave=True)
        for batch_data in pbar:
            if len(batch_data) == 4:
                _, labels, dates, _ = batch_data
                labels = labels.to(device).float()
                
                # 将标签按日期分组
                for i, date in enumerate(dates):
                    date_labels[date].append(labels[i].item())
            else:
                raise ValueError("数据加载器必须返回日期信息(keep_meta=True)")
        total_batches += len(dataloader)
    
    # 计算每个日期的统计信息
    date_stats = {}
    for date, values in date_labels.items():
        # 如果该日期没有标签数据，打印警告并使用默认值
        if not values:
            print(f"警告: 日期 {date} 没有标签数据，使用默认 mean=0.0 和 std=1.0")
            date_stats[date] = (0.0, 1.0)
            continue
        # 构造张量并计算统计
        values_tensor = torch.tensor(values, device=device, dtype=torch.float32)
        date_mean = values_tensor.mean().item()
        date_std = values_tensor.std(unbiased=False).item()
        # 使用 numpy.isnan 检查浮点NaN
        date_std = max(date_std, 1e-3)  # 确保标准差不会太小
        date_stats[date] = (date_mean, date_std)
    
    print(f"计算完成，共 {len(date_stats)} 个日期，耗时 {time.time() - start_time:.2f}秒")
    print(f"✅ 日期标签收集完毕，总数据集数: {len(dataloaders)}, 总 batch 数: {total_batches}")
    
    # 保存缓存
    if cache_file:
        cache_dir = Path(cache_file).parent
        if not cache_dir.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_file, 'wb') as f:
            pickle.dump(date_stats, f)
        print(f"已将日期统计信息缓存到 {cache_file}")
    
    return date_stats

def standardize_labels_by_date_fn(
    labels: torch.Tensor, 
    dates: List[str],
    date_stats: Optional[Dict[str, Tuple[float, float]]] = None
) -> torch.Tensor:
    """按日期对标签进行标准化，相同日期的标签共享相同的均值和标准差。
    
    Args:
        labels: 形状为 [batch_size, 1] 的标签张量
        dates: 长度为 batch_size 的日期列表
        date_stats: 可选的日期统计字典，如果未提供则使用全局变量DATE_STATS
        
    Returns:
        标准化后的标签，形状与输入相同
    """
    # 使用提供的统计信息或全局变量
    stats_dict = date_stats if date_stats is not None else DATE_STATS
    if not stats_dict:
        raise ValueError("未提供日期统计信息且全局DATE_STATS为空，请先调用compute_and_save_date_stats")
    
    # 创建相同形状的输出张量
    normalized_labels = torch.zeros_like(labels)
    
    # 按日期应用预计算的均值和标准差
    for i, date in enumerate(dates):
        if date in stats_dict:
            date_mean, date_std = stats_dict[date]
            normalized_labels[i] = (labels[i] - date_mean) / date_std
        else:
            # 对于未出现在统计中的日期，使用全局均值和标准差
            all_means = [m for m, _ in stats_dict.values()]
            all_stds = [s for _, s in stats_dict.values()]
            global_mean = sum(all_means) / len(all_means) if all_means else 0.0
            global_std = sum(all_stds) / len(all_stds) if all_stds else 1.0
            normalized_labels[i] = (labels[i] - global_mean) / global_std
            print(f"警告: 日期 {date} 未在统计信息中找到，使用全局均值={global_mean:.4f}和标准差={global_std:.4f}")
    
    return normalized_labels

# --------------------------------------------------
# 1. Training / evaluation
# --------------------------------------------------

def train_one_epoch(
    model: nn.Module,
    loader: DataLoader,
    optimizer: torch.optim.Optimizer,
    scaler: GradScaler | None,
    device: torch.device,
    alpha_corr: float,
    use_amp: bool,
    grad_clip_norm: float,
    writer: SummaryWriter = None,
    epoch: int = None,
    standardize_labels_by_date: bool = False,
    date_to_idx: Dict[str, int] = None,
    means_tensor: torch.Tensor = None,
    stds_tensor: torch.Tensor = None,
    var_reg_beta: float = 0.0,
    target_std: float = 1.0,
) -> Tuple[float, float, float, float, float, float, float, float, float]:
    model.train()
    running_loss = 0.0
    running_loss_corr = 0.0
    running_loss_ortho = 0.0
    running_loss_var = 0.0
    running_preds_std = 0.0
    running_preds_mean = 0.0
    running_grad_norm = 0.0
    running_ic_pearson = 0.0
    running_ic_spearman = 0.0
    pbar = tqdm(loader, desc='Train', leave=False)
    for batch_idx, batch_data in enumerate(pbar):
        # 使用元数据解包 (feats, labels, dates, codes)
        if len(batch_data) == 4:
            feats, labels, dates, _ = batch_data
        else:
            feats, labels = batch_data
            dates = None  # 如果没有日期信息，就用None标记
        feats = feats.permute(0, 2, 1).to(device).float()
        labels = labels.unsqueeze(1).to(device).float()
        
        # 如果启用了标签日期标准化，则进行标准化
        if standardize_labels_by_date:
            if dates is None or date_to_idx is None or means_tensor is None or stds_tensor is None:
                raise ValueError("缺少日期信息或标准化映射！")
            idxs = torch.tensor([date_to_idx[d] for d in dates], dtype=torch.long, device=device)
            m = means_tensor[idxs]  # [batch_size, 1]
            s = stds_tensor[idxs]
            labels = (labels - m) / (s + 1e-3)  # 添加epsilon避免除零问题

        optimizer.zero_grad(set_to_none=True)
        with autocast('cuda', enabled=use_amp):
            preds, fv = model(feats)
            # 计算每个batch的指标
            std_tensor = torch.std(preds)
            preds_std = std_tensor.item()
            # 使用预测值绝对值的均值作为预测幅度监控
            preds_mean = preds.abs().mean().item()
            running_preds_std += preds_std
            running_preds_mean += preds_mean
            # 基于 Pearson 相关的损失
            # loss_corr = -(preds * labels).mean()  #尝试改一下
            loss_corr=-((preds-preds.mean(0))*(labels-labels.mean(0))).mean()
            
            # 基于 Pearson 相关的损失
            # loss_corr = neg_pearson_loss(preds, labels)

            # 正交惩罚
            loss_ortho = alpha_corr * orthogonality_penalty(fv)
            # 方差正则项: (std(preds) - target_std)^2
            loss_var = var_reg_beta * (std_tensor - target_std) ** 2
            # 总损失
            loss = loss_corr + loss_ortho + loss_var
            
            # 记录每个batch的指标到TensorBoard
            if writer is not None and epoch is not None:
                global_step = (epoch - 1) * len(loader) + batch_idx
                writer.add_scalar("Batch/preds_std", preds_std, global_step)
                writer.add_scalar("Batch/preds_mean", preds_mean, global_step)
                writer.add_scalar("Batch/loss_corr", loss_corr.item(), global_step)
                writer.add_scalar("Batch/loss_ortho", loss_ortho.item(), global_step)
                writer.add_scalar("Batch/loss_var", loss_var.item(), global_step)

            # Compute batch Pearson and Spearman IC
            batch_pearson_ic = pearson_ic(preds, labels)
            batch_spearman_ic = spearman_ic(preds, labels)
            running_ic_pearson += batch_pearson_ic
            running_ic_spearman += batch_spearman_ic

        if scaler is not None:
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            # 检测梯度是否含有Inf/NaN
            overflow = False
            for p in model.parameters():
                if p.grad is not None and not torch.isfinite(p.grad).all():
                    overflow = True
                    warnings.warn("Gradient contains Inf/NaN, skipping this step")
                    break
                    
            if overflow:
                # 发现overflow，立即下调scale并跳过本步
                warnings.warn("Gradient overflow detected, skipping this step")
                if epoch is not None and hasattr(pbar, 'set_postfix'):
                    pbar.set_postfix(loss=loss.item(), overflow="True")
                scaler.update()
                optimizer.zero_grad(set_to_none=True)
                continue
                
            # 先剪裁梯度，再统计范数
            nn.utils.clip_grad_norm_(model.parameters(), grad_clip_norm)
            total_norm = nn.utils.clip_grad_norm_(model.parameters(), float('inf'))
            if torch.isfinite(total_norm):
                running_grad_norm += total_norm.item()
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            # 先剪裁梯度，再统计范数
            nn.utils.clip_grad_norm_(model.parameters(), grad_clip_norm)
            total_norm = nn.utils.clip_grad_norm_(model.parameters(), float('inf'))
            if torch.isfinite(total_norm):
                running_grad_norm += total_norm.item()
            optimizer.step()

        running_loss += loss.item()
        running_loss_corr += loss_corr.item()
        running_loss_ortho += loss_ortho.item()
        running_loss_var += loss_var.item()
        
        pbar.set_postfix(loss=loss.item(), preds_std=preds_std)
    # 注：preds_std 和 grad_norm 只在 epoch 级别返回平均值，不再记录每个 step
    n_batches = max(len(loader), 1)
    avg_ic_pearson = running_ic_pearson / n_batches
    avg_ic_spearman = running_ic_spearman / n_batches
    return (
        running_loss / n_batches, 
        running_loss_corr / n_batches, 
        running_loss_ortho / n_batches, 
        running_loss_var / n_batches,
        running_preds_std / n_batches,
        running_preds_mean / n_batches,
        running_grad_norm / n_batches,
        avg_ic_pearson,
        avg_ic_spearman
    )


@torch.no_grad()
def evaluate(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
    alpha_corr: float,
    use_amp: bool,
    standardize_labels_by_date: bool = False,
    date_to_idx: Dict[str, int] = None,
    means_tensor: torch.Tensor = None,
    stds_tensor: torch.Tensor = None,
) -> Tuple[float, float, float]:  # 返回 (pearson_ic, spearman_ic, corr_penalty)
    model.eval()
    ic_list: List[float] = []
    pearson_ic_list: List[float] = []
    corr_list: List[float] = []
    pbar = tqdm(loader, desc='Valid/Test', leave=False)
    for batch_data in pbar:
        # 使用元数据解包 (feats, labels, dates, codes)
        if len(batch_data) == 4:
            feats, labels, dates, _ = batch_data
        else:
            feats, labels = batch_data
            dates = None  # 如果没有日期信息，就用None标记
        feats = feats.permute(0, 2, 1).to(device).float()
        labels = labels.unsqueeze(1).to(device).float()
        
        with autocast('cuda', enabled=use_amp):
            preds, fv = model(feats)
        
        ic_list.append(spearman_ic(preds, labels))
        pearson_ic_list.append(pearson_ic(preds, labels))
        corr_list.append(orthogonality_penalty(fv, eps=1e-3).item())

    return float(np.mean(pearson_ic_list)), float(np.mean(ic_list)), float(np.mean(corr_list))

# --------------------------------------------------
# 2. Main
# --------------------------------------------------

def run_training(cfg: TrainingConfig):
    """Main training and evaluation logic, takes a TrainingConfig object."""
    # ---------- seeds 🔹[5] ----------
    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)
    random.seed(cfg.seed)

    # ---------- logging ----------
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("train_dfzq_gru")
    logger.info(f"Starting training with config: {cfg}")

    device = torch.device("cuda" if torch.cuda.is_available() and not cfg.force_cpu else "cpu")
    logger.info(f"Using device: {device}")

    # ---------- Data ----------
    dl_cfg = {
        "dataset_path": cfg.dataset_path,
        "batch_size": cfg.batch_size,
        "num_workers": cfg.num_workers,
        "shuffle": cfg.shuffle, # This is shuffle for ParquetPVDataset internal index shuffling
        "seed": cfg.seed,
        "chunk_size": cfg.chunk_size,
        "memory_limit": cfg.memory_limit,
        "use_fixed_indices": cfg.use_fixed_indices, # 添加固定索引参数
    }
    # 为了标签按日期标准化，需要保留元数据
    train_loader, valid_loader, test_loader = get_train_valid_test_loaders(
        dl_cfg, 
        keep_meta_train=True,  # 修改为True，确保我们可以访问日期信息
        keep_meta_eval=True,   # 修改为True，确保我们可以访问日期信息
        max_samples_train=cfg.max_samples_train,
        max_samples_valid=cfg.max_samples_valid,
        max_samples_test=cfg.max_samples_test,
        use_fixed_indices=cfg.use_fixed_indices  # 添加固定索引参数
    )
    
    # ---------- 预计算日期标签统计量 ----------
    global DATE_STATS
    date_to_idx = None
    means_tensor = None
    stds_tensor = None
    
    # 只有在启用标签日期标准化时才计算日期统计量
    if cfg.standardize_labels_by_date:
        stats_path = Path(cfg.output_root) / "date_label_stats.pkl"
        try:
            logger.info("预计算每个日期的标签统计信息...")
            # 同时使用训练集和验证集计算日期统计信息
            DATE_STATS = compute_and_save_date_stats(
                [train_loader, valid_loader], 
                device,
                cache_file=str(stats_path)
            )
            logger.info(f"已完成 {len(DATE_STATS)} 个日期的标签统计")
            
            # 记录一些日期的样例统计量到日志
            sample_dates = list(DATE_STATS.keys())[:5]
            for date in sample_dates:
                mean, std = DATE_STATS[date]
                logger.info(f"日期 {date} 标签统计: 均值={mean:.6f}, 标准差={std:.6f}")
                
            # 构建按日期矢量化标准化映射
            date_list = list(DATE_STATS.keys())
            date_to_idx = {d: i for i, d in enumerate(date_list)}
            means_tensor = torch.tensor([DATE_STATS[d][0] for d in date_list], device=device).unsqueeze(1)
            stds_tensor = torch.tensor([DATE_STATS[d][1] for d in date_list], device=device).unsqueeze(1)
        except Exception as e:
            logger.error(f"计算日期标签统计信息失败: {e}")
            raise
    else:
        logger.info("标签日期标准化已禁用，将使用原始标签")

    # ---------- 数据顺序一致性测试 ----------
    # 添加数据顺序一致性日志信息
    if cfg.use_fixed_indices:
        logger.info("已启用固定索引，确保数据加载顺序一致性")
    else:
        if cfg.shuffle:
            logger.info("使用随机打乱模式，数据顺序将受随机种子控制")
        else:
            logger.warning("未启用固定索引且未打乱数据，可能导致训练结果不一致！建议启用固定索引")
    # ---------- label mean/std (缓存到文件) 🔹[2] ----------
    # Compute stats only if train_loader is not empty
    label_mean, label_std = 0.0, 1.0 # 保留定义以避免函数调用错误，但实际不使用

    # ---------- Model ----------
    model_cfg = DFZQGRUConfig()
    model_cfg.input_size = cfg.input_size
    model_cfg.hidden_size = cfg.hidden_size
    model_cfg.num_layers = cfg.num_layers
    model_cfg.dropout = cfg.dropout
    model_cfg.output_size = cfg.output_size
    model = DFZQGRU(model_cfg).to(device)

    logger.info(f"Model created: input_size={model_cfg.input_size}, hidden_size={model_cfg.hidden_size}, "
               f"num_layers={model_cfg.num_layers}, dropout={model_cfg.dropout}")

    #改一下优化器
    # optimizer = torch.optim.Adam(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    # logger.info(f"Optimizer: Adam with lr={cfg.lr}, weight_decay={cfg.weight_decay}")
    
    optimizer = torch.optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    logger.info(f"Optimizer: AdamW with lr={cfg.lr}, weight_decay={cfg.weight_decay}")
    scaler = GradScaler(enabled=cfg.use_amp and device.type == 'cuda')

    # ---------- Scheduler (configurable) ----------
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
            min_lr=cfg.lr_scheduler_min_lr)
        scheduler_step = lambda metric: scheduler.step(metric)
    else:
        raise ValueError(f"未知 lr_scheduler_type: {cfg.lr_scheduler_type}")

    # ---------- I/O ----------
    root = Path(cfg.output_root).expanduser()
    ckpt_dir = root / "ckpt"; log_dir = root / "logs"; bt_dir = root / "bt_results"
    for d_path in (ckpt_dir, log_dir, bt_dir): d_path.mkdir(parents=True, exist_ok=True)
    best_ckpt_path = ckpt_dir / "best_model.pth" # Renamed
    log_csv_path = log_dir / "training_log.csv" # Renamed
    
    
    
    # flush_secs=100：每隔 100s 自动将缓存刷到磁盘
    writer = SummaryWriter(log_dir=str(log_dir),    flush_secs=100, max_queue=10)



    # ---------- Early-Stopping 🔹[1] ----------
    stopper = SimpleEarlyStop(patience=cfg.patience)

    # ---------- Loop ----------
    history = []
    best_ic = -np.inf
    logger.info(f"Starting training for {cfg.max_epochs} epochs.")
    
    # 正交惩罚权重调度策略
    initial_alpha_corr = 0.1  # 初始较小权重
    target_alpha_corr = cfg.alpha_corr  # 原始配置中的目标权重
    alpha_growth_rate = 0.02  # 每个epoch的增长率
    alpha_growth_start = 20   # 开始增长的epoch
    
    for epoch in tqdm(range(1, cfg.max_epochs + 1), desc="Training", leave=False):
        # # 计算当前epoch的alpha_corr权重
        # if epoch < alpha_growth_start:
        #     current_alpha_corr = initial_alpha_corr
        # else:
        #     # 线性增长，但不超过目标值
        #     epochs_past_start = epoch - alpha_growth_start
        #     current_alpha_corr = min(
        #         initial_alpha_corr + epochs_past_start * alpha_growth_rate,
        #         target_alpha_corr
        #     )
        
        #先这么做测试
        current_alpha_corr = cfg.alpha_corr
        
        
        (
            train_loss, train_loss_corr, train_loss_ortho, train_loss_var,
            train_preds_std, train_preds_mean, train_grad_norm,
            train_pearson_ic, train_spearman_ic
        ) = train_one_epoch(
            model, train_loader, optimizer, scaler, device,
            current_alpha_corr, cfg.use_amp and device.type == 'cuda',  # 使用当前epoch的alpha_corr
            cfg.grad_clip_norm,
            writer, epoch,
            cfg.standardize_labels_by_date,
            date_to_idx, means_tensor, stds_tensor,
            cfg.var_reg_beta, cfg.target_std
        )
        val_pearson_ic, val_spearman_ic, val_corr = evaluate(
            model, valid_loader, device, current_alpha_corr,
            cfg.use_amp and device.type == 'cuda',
            cfg.standardize_labels_by_date,
            date_to_idx, means_tensor, stds_tensor
        )

        # ---- log (console / csv / tensorboard) ----
        history.append((
            epoch, train_loss, train_loss_corr, train_loss_ortho, train_loss_var,
            train_pearson_ic, train_spearman_ic,
            val_pearson_ic, val_spearman_ic, val_corr,
            train_preds_std, train_preds_mean, train_grad_norm
        ))
        log_msg = (
            f"Epoch {epoch:03d}/{cfg.max_epochs} | Train Loss: {train_loss:.6f} | " 
            f"Train Var: {train_loss_var:.6f} | Train Pearson IC: {train_pearson_ic:.6f} | Train Spearman IC: {train_spearman_ic:.6f} | "
            f"Val Pearson IC: {val_pearson_ic:.6f} | Val Spearman IC: {val_spearman_ic:.6f} | " 
            f"Val OrthoPenalty: {val_corr:.6f} | Best Val IC: {best_ic:.6f} | " 
            f"Preds Mean: {train_preds_mean:.6f} | Preds Std: {train_preds_std:.6f} | Grad Norm: {train_grad_norm:.6f} | " 
            f"LR: {optimizer.param_groups[0]['lr']:.2e}"
        )
        logger.info(log_msg)
        print(log_msg)
        
        safe_preds_std = train_preds_std if np.isfinite(train_preds_std) else 0.0
        safe_preds_mean = train_preds_mean if np.isfinite(train_preds_mean) else 0.0
        safe_grad_norm = train_grad_norm if np.isfinite(train_grad_norm) else 0.0
        
        
        writer.add_scalar("Loss/train", train_loss, epoch)
        writer.add_scalar("Loss/train_corr", train_loss_corr, epoch)
        writer.add_scalar("Loss/train_ortho", train_loss_ortho, epoch)
        writer.add_scalar("Loss/train_var", train_loss_var, epoch)
        writer.add_scalar("IC/train_pearson", train_pearson_ic, epoch)
        writer.add_scalar("IC/train_spearman", train_spearman_ic, epoch)
        writer.add_scalar("Diagnostics/preds_std", safe_preds_std, epoch)
        writer.add_scalar("Diagnostics/preds_mean", safe_preds_mean, epoch)
        writer.add_scalar("Diagnostics/grad_norm", safe_grad_norm, epoch)
        # 记录混合精度训练的缩放因子
        if scaler is not None and scaler.is_enabled():
            writer.add_scalar("Diagnostics/scaler_scale", scaler.get_scale(), epoch)
        # 记录当前epoch的正交惩罚权重
        writer.add_scalar("Hyperparams/alpha_corr", current_alpha_corr, epoch)
        
        
        # 计算并记录模型权重的L2范数
        with torch.no_grad():
            total_weight_norm = torch.sqrt(
                sum(p.pow(2).sum() for p in model.parameters() if p.requires_grad)
            )
            writer.add_scalar("Diagnostics/weight_norm", total_weight_norm.item(), epoch)
            
        writer.add_scalar("IC/val_pearson", val_pearson_ic, epoch)
        writer.add_scalar("IC/val_spearman", val_spearman_ic, epoch)
        writer.add_scalar("CorrPenalty/val", val_corr, epoch)
        writer.add_scalar("LR", optimizer.param_groups[0]["lr"], epoch)


        # ---- early stop ----
        # 使用 Pearson IC 和 Spearman IC 的综合指标，更好地捕捉线性关系和单调关系
        combined_ic = val_pearson_ic * 0.5 + val_spearman_ic * 0.5  # 可根据需要调整权重
        stopper(combined_ic)
        if combined_ic > best_ic + 1e-6: # Check against a small delta for float comparisons
            best_ic = combined_ic
            torch.save({
                "epoch": epoch,
                "model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "scaler_state_dict": scaler.state_dict() if scaler.is_enabled() else None,
                "best_ic": best_ic,
                "training_config": cfg.__dict__ # Save config for reproducibility
            }, best_ckpt_path)
            logger.info(f"New best model saved to {best_ckpt_path} at epoch {epoch} with Combined Val IC: {best_ic:.6f}")
        
        if stopper.should_stop:
            logger.info(f"Early stopping triggered at epoch {epoch}.")
            break

        # 立即写入磁盘，保证 TensorBoard 能马上读到
        writer.flush()
        
        
        # ---- scheduler step (type-safe) ----
        scheduler_step(combined_ic)
        
        # scheduler.step()
        
        # # 同步让 weight_decay 按 lr 比例缩放
        # base_lr, base_wd = cfg.lr, cfg.weight_decay
        # for g in optimizer.param_groups:
        #     g["weight_decay"] = base_wd * (g["lr"] / base_lr)

    writer.close()
    
    # 添加数据加载配置摘要信息
    logger.info("训练循环完成。")
    logger.info(f"数据顺序配置摘要：固定索引={cfg.use_fixed_indices}，随机打乱={cfg.shuffle}，随机种子={cfg.seed}")

    # ---------- Save CSV ----------
    pd.DataFrame(history, columns=[
        "epoch", "train_loss", "train_loss_corr", "train_loss_ortho", "train_loss_var",
        "train_pearson_ic", "train_spearman_ic",
        "val_pearson_ic", "val_spearman_ic", "val_corr",
        "preds_std", "preds_mean", "grad_norm"
    ]).to_csv(log_csv_path, index=False)
    logger.info(f"Training log saved to {log_csv_path}")


    # ---------- Test ----------
    if best_ckpt_path.exists():
        logger.info(f"Loading best model from {best_ckpt_path} for testing.")
        checkpoint = torch.load(best_ckpt_path, map_location=device)

        model.load_state_dict(checkpoint["model_state_dict"])

        logger.info(f"Best model loaded. Epoch: {checkpoint.get('epoch', 'N/A')}, Val IC: {checkpoint.get('best_ic', 'N/A'):.6f}")
    else:
        logger.warning("No best model checkpoint found for testing. Using last model state.")
        saved_label_mean, saved_label_std = 0.0, 1.0 # 不再使用

    test_pearson_ic, test_spearman_ic, test_corr = evaluate(
        model, test_loader, device, current_alpha_corr,
        cfg.use_amp and device.type == 'cuda',
        cfg.standardize_labels_by_date,
        date_to_idx, means_tensor, stds_tensor
    )
    test_combined_ic = test_pearson_ic * 0.5 + test_spearman_ic * 0.5
    logger.info(f"Test results | Pearson IC={test_pearson_ic:.6f} | Spearman IC={test_spearman_ic:.6f} | Combined IC={test_combined_ic:.6f} | OrthoPenalty={test_corr:.6f}")
    print(f"Test results | Pearson IC={test_pearson_ic:.6f} | Spearman IC={test_spearman_ic:.6f} | Combined IC={test_combined_ic:.6f} | OrthoPenalty={test_corr:.6f}")


def main():
    """Entry point for running training with default configuration."""
    cfg = TrainingConfig()
    run_training(cfg)

if __name__ == "__main__":
    main()
# wandb.init(project="dfzq_gru", name="dfzq_gru")