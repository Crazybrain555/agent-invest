# train_dfzq_gru.py - 简化版，保留核心诊断功能
import logging
import random
import time
import warnings
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
from src.models.rnn.gru.dfzq_gru.get_configs import DFZQGRUConfig
from src.train.Neural_networks.RNN.DFZQ_GRU.dfzq_Dataloader import get_train_valid_test_loaders
from src.train.Neural_networks.RNN.DFZQ_GRU.config import TrainingConfig


# --------------------------------------------------
# 监控和诊断工具 - 简化版
# --------------------------------------------------

class SimpleMonitor:
    """简化的训练监控器，专注于核心诊断功能"""
    
    def __init__(self, writer: SummaryWriter, logger: logging.Logger):
        self.writer = writer
        self.logger = logger
        self.reset_stats()
        
    def reset_stats(self):
        """重置统计数据"""
        self.grad_norms = []
        self.pred_stats = []
        self.overflow_count = 0
    
    def collect_batch_stats(self, preds: torch.Tensor, grad_norm: Optional[float] = None, overflow: bool = False):
        """收集批次统计数据"""
        # 预测值统计
        pred_mean = preds.abs().mean().item()
        pred_std = preds.std().item()
        pred_min = preds.min().item()
        pred_max = preds.max().item()
        self.pred_stats.append((pred_mean, pred_std, pred_min, pred_max))
        
        # 梯度统计
        if grad_norm is not None:
            self.grad_norms.append(grad_norm)
            
        # 溢出计数
        if overflow:
            self.overflow_count += 1
    
    def log_epoch_stats(self, epoch: int):
        """记录epoch统计信息"""
        if not self.writer:
            return
            
        # 预测值统计 - 检测过拟合和输出异常
        if self.pred_stats:
            pred_means, pred_stds, pred_mins, pred_maxs = zip(*self.pred_stats)
            avg_pred_mean = np.mean(pred_means)
            avg_pred_std = np.mean(pred_stds)
            pred_range = np.mean(pred_maxs) - np.mean(pred_mins)
            
            self.writer.add_scalar("Diagnostics/pred_abs_mean", avg_pred_mean, epoch)
            self.writer.add_scalar("Diagnostics/pred_std", avg_pred_std, epoch)
            self.writer.add_scalar("Diagnostics/pred_range", pred_range, epoch)
            
            # 诊断预测异常
            if avg_pred_mean > 2.0:
                self.logger.warning(f"Epoch {epoch}: 预测值过大 ({avg_pred_mean:.4f})，可能过拟合")
            if avg_pred_std < 0.05:
                self.logger.warning(f"Epoch {epoch}: 预测标准差过小 ({avg_pred_std:.4f})，可能输出饱和")
        
        # 梯度统计 - 检测梯度爆炸/消失
        if self.grad_norms:
            avg_grad_norm = np.mean(self.grad_norms)
            max_grad_norm = np.max(self.grad_norms)
            
            self.writer.add_scalar("Diagnostics/grad_norm_avg", avg_grad_norm, epoch)
            self.writer.add_scalar("Diagnostics/grad_norm_max", max_grad_norm, epoch)
            
            # 诊断梯度问题
            if avg_grad_norm > 10.0:
                self.logger.warning(f"Epoch {epoch}: 梯度爆炸风险 (平均={avg_grad_norm:.4f})")
            elif avg_grad_norm < 0.001:
                self.logger.warning(f"Epoch {epoch}: 梯度消失风险 (平均={avg_grad_norm:.4f})")
        
        # 溢出统计
        if self.overflow_count > 0:
            total_batches = len(self.pred_stats)
            overflow_rate = self.overflow_count / max(total_batches, 1)
            self.writer.add_scalar("Diagnostics/overflow_rate", overflow_rate, epoch)
            
            if overflow_rate > 0.05:
                self.logger.error(f"Epoch {epoch}: 严重数值不稳定 ({overflow_rate:.1%} batches overflow)")
        
        # 重置统计
        self.reset_stats()


class SimpleEarlyStop:
    """简单的早停机制"""
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
# 标签日期标准化功能 - 保留重要功能
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
    writer: Optional[SummaryWriter] = None,
    epoch: Optional[int] = None,
    standardize_labels_by_date: bool = False,
    date_to_idx: Optional[Dict[str, int]] = None,
    means_tensor: Optional[torch.Tensor] = None,
    stds_tensor: Optional[torch.Tensor] = None,
    monitor: Optional[SimpleMonitor] = None,
) -> Tuple[float, float, float, float, float, float, float, float]:
    """训练一个epoch"""
    model.train()
    running_loss = 0.0
    running_loss_main = 0.0
    running_loss_ortho = 0.0
    running_preds_std = 0.0
    running_preds_mean = 0.0
    running_grad_norm = 0.0
    running_ic_pearson = 0.0
    running_ic_spearman = 0.0
    
    # 选择损失函数
    criterion = nn.HuberLoss(delta=0.5)  # 保留Huber损失，更鲁棒
    
    pbar = tqdm(loader, desc='Train', leave=False)
    for batch_idx, batch_data in enumerate(pbar):
        # 解包数据
        if len(batch_data) == 4:
            feats, labels, dates, _ = batch_data
        else:
            feats, labels = batch_data
            dates = None
            
        feats = feats.to(device).float()
        labels = labels.unsqueeze(1).to(device).float()
        
        # 标签日期标准化
        if standardize_labels_by_date and dates is not None:
            if date_to_idx is None or means_tensor is None or stds_tensor is None:
                raise ValueError("缺少日期标准化参数")
            idxs = torch.tensor([date_to_idx[d] for d in dates], dtype=torch.long, device=device)
            m = means_tensor[idxs]
            s = stds_tensor[idxs]
            labels = (labels - m) / (s + 1e-3)

        optimizer.zero_grad(set_to_none=True)
        
        with autocast('cuda', enabled=use_amp):
            preds, fv = model(feats)
            
            # 主损失
            loss_main = criterion(preds, labels)
            
            # 正交惩罚
            loss_ortho = alpha_corr * orthogonality_penalty(fv)
            
            # 总损失
            loss = loss_main + loss_ortho
            
            # 统计
            preds_std = preds.std().item()
            preds_mean = preds.abs().mean().item()
            
            # 计算IC
            batch_pearson_ic = pearson_ic(preds, labels)
            batch_spearman_ic = spearman_ic(preds, labels)

        # 反向传播
        if scaler is not None:
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            
            # 梯度裁剪和监控
            total_norm = nn.utils.clip_grad_norm_(model.parameters(), grad_clip_norm)
            overflow = not torch.isfinite(total_norm)
            
            if monitor:
                monitor.collect_batch_stats(preds.detach(), total_norm.item(), overflow)
            
            if overflow:
                warnings.warn(f"梯度溢出检测到，跳过此步骤")
                scaler.update()
                continue
            
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            total_norm = nn.utils.clip_grad_norm_(model.parameters(), grad_clip_norm)
            
            if monitor:
                monitor.collect_batch_stats(preds.detach(), total_norm.item(), False)
            
            if torch.isfinite(total_norm):
                optimizer.step()
            else:
                warnings.warn(f"梯度NaN/Inf检测到，跳过此步骤")

        # 累积统计
        running_loss += loss.item()
        running_loss_main += loss_main.item()
        running_loss_ortho += loss_ortho.item()
        running_preds_std += preds_std
        running_preds_mean += preds_mean
        running_grad_norm += total_norm.item()
        running_ic_pearson += batch_pearson_ic
        running_ic_spearman += batch_spearman_ic
        
        pbar.set_postfix(loss=loss.item(), preds_std=preds_std)

    n_batches = max(len(loader), 1)
    return (
        running_loss / n_batches,
        running_loss_main / n_batches,
        running_loss_ortho / n_batches,
        running_preds_std / n_batches,
        running_preds_mean / n_batches,
        running_grad_norm / n_batches,
        running_ic_pearson / n_batches,
        running_ic_spearman / n_batches
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
) -> Tuple[float, float, float, float, float]:
    """评估模型"""
    model.eval()
    pearson_ic_list = []
    spearman_ic_list = []
    corr_list = []
    preds_mean_list = []
    preds_std_list = []
    
    pbar = tqdm(loader, desc='Eval', leave=False)
    for batch_data in pbar:
        if len(batch_data) == 4:
            feats, labels, dates, _ = batch_data
        else:
            feats, labels = batch_data
            
        feats = feats.to(device).float()
        labels = labels.unsqueeze(1).to(device).float()
        
        with autocast('cuda', enabled=use_amp):
            preds, fv = model(feats)
        
        pearson_ic_list.append(pearson_ic(preds, labels))
        spearman_ic_list.append(spearman_ic(preds, labels))
        corr_list.append(orthogonality_penalty(fv, eps=1e-3).item())
        preds_mean_list.append(preds.abs().mean().item())
        preds_std_list.append(preds.std().item())

    return (
        float(np.mean(pearson_ic_list)),
        float(np.mean(spearman_ic_list)),
        float(np.mean(corr_list)),
        float(np.mean(preds_mean_list)),
        float(np.mean(preds_std_list)),
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

    # 日志设置
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("train_dfzq_gru")
    logger.info(f"开始训练，配置: {cfg}")

    device = torch.device("cuda" if torch.cuda.is_available() and not cfg.force_cpu else "cpu")
    logger.info(f"使用设备: {device}")

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
        use_fixed_indices=cfg.use_fixed_indices
    )

    # 日期标准化设置
    date_to_idx = None
    means_tensor = None
    stds_tensor = None
    
    if cfg.standardize_labels_by_date:
        stats_path = Path(cfg.output_root) / "date_label_stats.pkl"
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

    # 优化器
    optimizer = torch.optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    
    # 混合精度
    scaler = GradScaler(enabled=cfg.use_amp and device.type == 'cuda') if cfg.use_amp else None

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
            min_lr=cfg.lr_scheduler_min_lr
        )
        scheduler_step = lambda metric: scheduler.step(metric)
    else:
        raise ValueError(f"未知调度器类型: {cfg.lr_scheduler_type}")

    # 输出目录
    root = Path(cfg.output_root).expanduser()
    ckpt_dir = root / "ckpt"
    log_dir = root / "logs"
    bt_dir = root / "bt_results"
    for d_path in (ckpt_dir, log_dir, bt_dir): 
        d_path.mkdir(parents=True, exist_ok=True)
    
    best_ckpt_path = ckpt_dir / "best_model.pth"
    log_csv_path = log_dir / "training_log.csv"
    
    writer = SummaryWriter(log_dir=str(log_dir), flush_secs=30)
    monitor = SimpleMonitor(writer, logger)
    stopper = SimpleEarlyStop(patience=cfg.patience)

    # 训练循环
    history = []
    best_ic = -np.inf
    
    logger.info(f"开始训练，总轮数: {cfg.max_epochs}")
    
    for epoch in tqdm(range(1, cfg.max_epochs + 1), desc="Training"):
        # 训练
        (train_loss, train_loss_main, train_loss_ortho,
         train_preds_std, train_preds_mean, train_grad_norm,
         train_pearson_ic, train_spearman_ic) = train_one_epoch(
            model, train_loader, optimizer, scaler, device,
            cfg.alpha_corr, cfg.use_amp and device.type == 'cuda',
            cfg.grad_clip_norm, writer, epoch,
            cfg.standardize_labels_by_date,
            date_to_idx, means_tensor, stds_tensor, monitor
        )
        
        # 验证
        val_pearson_ic, val_spearman_ic, val_corr, val_preds_mean, val_preds_std = evaluate(
            model, valid_loader, device, cfg.alpha_corr,
            cfg.use_amp and device.type == 'cuda',
            cfg.standardize_labels_by_date,
            date_to_idx, means_tensor, stds_tensor
        )

        # 记录日志
        history.append((
            epoch, train_loss, train_loss_main, train_loss_ortho,
            train_pearson_ic, train_spearman_ic,
            val_pearson_ic, val_spearman_ic, val_corr,
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
        
        # TensorBoard记录
        writer.add_scalar("Loss/train", train_loss, epoch)
        writer.add_scalar("Loss/train_main", train_loss_main, epoch)
        writer.add_scalar("Loss/train_ortho", train_loss_ortho, epoch)
        writer.add_scalar("IC/train_pearson", train_pearson_ic, epoch)
        writer.add_scalar("IC/train_spearman", train_spearman_ic, epoch)
        writer.add_scalar("IC/val_pearson", val_pearson_ic, epoch)
        writer.add_scalar("IC/val_spearman", val_spearman_ic, epoch)
        writer.add_scalar("LR", optimizer.param_groups[0]["lr"], epoch)
        
        # 监控记录
        monitor.log_epoch_stats(epoch)

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
        
        if stopper.should_stop:
            logger.info(f"早停触发，Epoch {epoch}")
            break

        writer.flush()
        scheduler_step(combined_ic)

    writer.close()

    # 保存训练日志
    pd.DataFrame(history, columns=[
        "epoch", "train_loss", "train_loss_main", "train_loss_ortho",
        "train_pearson_ic", "train_spearman_ic",
        "val_pearson_ic", "val_spearman_ic", "val_corr",
        "preds_std", "preds_mean", "grad_norm"
    ]).to_csv(log_csv_path, index=False)
    logger.info(f"训练日志保存到 {log_csv_path}")

    # 测试最佳模型
    if best_ckpt_path.exists():
        logger.info(f"加载最佳模型进行测试: {best_ckpt_path}")
        checkpoint = torch.load(best_ckpt_path, map_location=device)
        model.load_state_dict(checkpoint["model_state_dict"])
        logger.info(f"最佳模型加载完成，Epoch: {checkpoint.get('epoch', 'N/A')}, Val IC: {checkpoint.get('best_ic', 'N/A'):.6f}")

    test_pearson_ic, test_spearman_ic, test_corr, test_preds_mean, test_preds_std = evaluate(
        model, test_loader, device, cfg.alpha_corr,
        cfg.use_amp and device.type == 'cuda',
        cfg.standardize_labels_by_date,
        date_to_idx, means_tensor, stds_tensor
    )
    
    test_combined_ic = test_pearson_ic * 0.5 + test_spearman_ic * 0.5
    
    logger.info(f"测试结果 | Pearson IC={test_pearson_ic:.6f} | "
               f"Spearman IC={test_spearman_ic:.6f} | "
               f"Combined IC={test_combined_ic:.6f} | "
               f"OrthoPenalty={test_corr:.6f}")
    print(f"测试结果 | Pearson IC={test_pearson_ic:.6f} | "
          f"Spearman IC={test_spearman_ic:.6f} | "
          f"Combined IC={test_combined_ic:.6f} | "
          f"OrthoPenalty={test_corr:.6f}")


def main():
    """训练入口点"""
    cfg = TrainingConfig()
    run_training(cfg)


if __name__ == "__main__":
    main()
