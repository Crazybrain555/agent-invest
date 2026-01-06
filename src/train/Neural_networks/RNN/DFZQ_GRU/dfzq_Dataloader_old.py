# utils/pv_dataloader.py
import logging
import random
from typing import Any, Mapping, Tuple, List

import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader, Dataset
from functools import partial
from src.dataset.DFZQ_GRU_PV_dataset.parquet_pv_dataset import ParquetPVDataset


# ========================================================================
# 🔧 可Picklable的推理Dataset类 (解决Windows多进程问题)
# ========================================================================
class WideInferDataset(Dataset):
    """
    可picklable的推理用Dataset（宽表 + lag）
    适用于Windows下的多进程DataLoader
    """
    def __init__(self, df: pd.DataFrame, seq_len: int = 30, 
                 feature_cols: List[str] = None):
        """
        Args:
            df: wide+lag格式的DataFrame，包含trade_date, stock_code和lag特征列
            seq_len: 序列长度，需要与训练时保持一致
            feature_cols: 特征列名列表，如果为None则自动检测
        """
        self.seq_len = seq_len
        self.df = df.copy()
        
        # 确保按日期和股票代码排序
        if 'trade_date' in df.columns and 'stock_code' in df.columns:
            self.df = self.df.sort_values(['stock_code', 'trade_date'])
        
        # 自动检测特征列
        if feature_cols is None:
            exclude_cols = {'trade_date', 'stock_code', 'label', 'mask'}
            detected_feature_cols = [col for col in df.columns if col not in exclude_cols]
            self.feature_cols = detected_feature_cols
        else:
            self.feature_cols = feature_cols
        
        print(f"📊 推理Dataset: 使用 {len(self.feature_cols)} 个特征列")
        
        # 按股票分组准备序列数据
        self.samples = []
        
        for stock_code, group in self.df.groupby('stock_code'):
            group = group.sort_values('trade_date')
            features = group[self.feature_cols].values.astype(np.float32)
            dates = group['trade_date'].values
            
            # 为每个时间点创建序列
            for i in range(seq_len - 1, len(group)):
                seq_features = features[i-seq_len+1:i+1]  # [seq_len, n_features]
                
                self.samples.append({
                    'features': seq_features,
                    'date': dates[i],
                    'code': stock_code,
                    'dummy_label': 0.0
                })
    
    def __len__(self):
        return len(self.samples)
    
    def __getitem__(self, idx):
        sample = self.samples[idx]
        
        features = torch.tensor(sample['features'], dtype=torch.float32)
        label = torch.tensor(sample['dummy_label'], dtype=torch.float32)
        # 🔧 转换日期为字符串，避免numpy.datetime64的collate问题
        date = str(sample['date']) if hasattr(sample['date'], 'strftime') else sample['date']
        code = sample['code']
        
        return features, label, date, code

logger = logging.getLogger(__name__)


# ---------- collate ----------------------------------------------------------
def _pv_collate_fn(
    batch: List[Tuple[torch.Tensor, torch.Tensor, str, str]],
    keep_meta: bool = False,
):
    """把单条数据打包成 (feats, labels, [date, code])."""
    feats, labels, t_dates, s_codes = zip(*batch)
    feats = torch.stack(feats, 0)          # (B, C, L)
    labels = torch.stack(labels, 0)        # (B,)
    if keep_meta:
        return feats, labels, list(t_dates), list(s_codes)
    return feats, labels


# ---------- worker-seed ------------------------------------------------------
def _worker_init_fn(worker_id: int) -> None:
    """为每个 dataloader worker 设置独立但可复现的随机种子。"""
    base_seed = torch.initial_seed() % 2**32
    np.random.seed(base_seed + worker_id)
    random.seed(base_seed + worker_id)


# ---------- factory ----------------------------------------------------------
def get_dataloader(
    split: str,
    config: Mapping[str, Any],
    *,
    batch_size: int | None = None,
    num_workers: int | None = None,
    shuffle: bool | None = None,
    seed: int | None = None,
    pin_memory: bool = True,
    drop_last: bool = False,
    max_samples: int | None = None,
    chunk_size: int | None = None,
    memory_limit: str | None = None,
    prefetch_factor: int | None = None,
    keep_meta: bool = False,
    use_fixed_indices: bool = True,  # 默认启用固定索引
    selected_factors: List[str] | None = None,  # 🚀 新增：选择的特征列表
) -> DataLoader:

    # ---------------- 参数解析 ----------------
    dataset_path  = config["dataset_path"]
    batch_size    = batch_size    or config.get("batch_size", 256)
    num_workers   = num_workers   or config.get("num_workers", 4)
    shuffle       = shuffle if shuffle is not None else (split == "train")
    seed          = seed          or config.get("seed", 0)
    chunk_size    = chunk_size    or config.get("chunk_size", 32768)
    memory_limit  = memory_limit  or config.get("memory_limit", "4GB")
    # 从配置中获取 use_fixed_indices 参数，如果未提供则使用函数参数
    use_fixed_indices = config.get("use_fixed_indices", use_fixed_indices)
    # 🚀 新增：从配置中获取 selected_factors 参数
    selected_factors = selected_factors if selected_factors is not None else config.get("selected_factors", None)

    # ---------------- 数据集 -------------------
    # 将config中的参数合并，以便Dataloader的参数可以覆盖全局config
    dataset_config = {
        **config,
        "shuffle": shuffle,
        "seed": seed,
        "chunk_size": chunk_size,
        "memory_limit": memory_limit,
        "use_fixed_indices": use_fixed_indices,
        "selected_factors": selected_factors,  # �� 新增：传递特征选择参数
        "keep_meta": keep_meta,               # ⭐ 新增：传递元数据开关
    }

    # 🚀 添加日期范围支持
    # 优先使用直接指定的date_from和date_to参数
    if config.get("date_from") is not None and config.get("date_to") is not None:
        dataset_config["date_from"] = config.get("date_from")
        dataset_config["date_to"] = config.get("date_to")
        print(f"🗓️ 使用直接指定的日期范围: {dataset_config['date_from']} 至 {dataset_config['date_to']}")
    elif config.get("use_custom_splits", False) and config.get("date_ranges"):
        date_ranges = config.get("date_ranges", {})
        if split in date_ranges:
            dataset_config["date_from"], dataset_config["date_to"] = date_ranges[split]
            print(f"🗓️ 使用自定义日期范围 {split}: {date_ranges[split][0]} 至 {date_ranges[split][1]}")
        else:
            print(f"⚠️ 未找到split '{split}' 的日期范围配置，使用默认分割")
            dataset_config["date_from"] = None
            dataset_config["date_to"] = None
    else:
        dataset_config["date_from"] = None
        dataset_config["date_to"] = None

    dataset = ParquetPVDataset(
        root=dataset_path,
        split=split,
        max_samples=max_samples,
        config=dataset_config,
    )

    # ---------------- 数据集信息 ----------
    # 获取数据集特征维度信息用于日志输出
    dataset_base_features = dataset.n_base_features if hasattr(dataset, 'n_base_features') else config.get('base_input_size', 28)
    print(f"📊 数据集特征维度: {dataset_base_features} 个基础特征")

    # ---------------- DataLoader --------------
    # 根据配置动态决定预取批次数；允许用户在CLI中控制
    if prefetch_factor is None:
        # 如果函数参数未提供，则尝试从config读取；否则降级为2/1策略
        prefetch_factor_cfg = config.get("prefetch_factor", None)
        if prefetch_factor_cfg is not None:
            effective_prefetch = prefetch_factor_cfg
        else:
            effective_prefetch = 2 if num_workers > 0 else None
            if num_workers >= 4:
                effective_prefetch = 1  # 多worker时减少预取，避免内存竞争
    else:
        effective_prefetch = prefetch_factor
    
    # 🚀 多worker模式：每个worker独享DuckDB连接
    logger.info("🚀 Using multi-worker optimization: each worker has independent DuckDB connection")
    
    # 🚀 批级优化：Dataset已产出batch，DataLoader无需再打包
    dataloader_kwargs = {
        "batch_size": None,        # Dataset已分好batch
        "num_workers": num_workers,
        "pin_memory": True,        # 数据已在Dataset中pin_memory，这里保持True
        "drop_last": drop_last,
        "shuffle": False,
        "persistent_workers": num_workers > 0,
        "collate_fn": None,        # 不再需要collate
    }
    
    if num_workers > 0:
        dataloader_kwargs["worker_init_fn"] = _worker_init_fn
        dataloader_kwargs["timeout"] = 300
    
    if num_workers > 0 and effective_prefetch is not None:
        dataloader_kwargs["prefetch_factor"] = effective_prefetch
    
    logger.info(f"📊 DataLoader config: batch_size=None (批级yield), num_workers={num_workers}, pin_memory={dataloader_kwargs['pin_memory']}")
    
    loader = DataLoader(dataset, **dataloader_kwargs)
    return loader


# ---------- convenience wrapper ---------------------------------------------
def get_train_valid_test_loaders(
    config: Mapping[str, Any],
    keep_meta_train: bool = False,
    keep_meta_eval: bool = False,
    max_samples_train: int | None = None,
    max_samples_valid: int | None = None,
    max_samples_test: int | None = None,
    use_fixed_indices: bool = True,  # 默认启用固定索引
    selected_factors: List[str] | None = None,  # 🚀 新增：选择的特征列表
) -> tuple[DataLoader, DataLoader, DataLoader]:
    """一次性返回 (train, valid, test) 三个 DataLoader。"""
    # 🚀 新增：从配置中获取特征选择参数
    selected_factors = selected_factors if selected_factors is not None else config.get("selected_factors", None)
    
    train_loader = get_dataloader(
        "train", 
        config, 
        keep_meta=keep_meta_train, 
        max_samples=max_samples_train,
        use_fixed_indices=use_fixed_indices,
        selected_factors=selected_factors  # 🚀 新增：传递特征选择参数
    )
    valid_loader = get_dataloader(
        "valid", 
        config, 
        keep_meta=keep_meta_eval, 
        max_samples=max_samples_valid,
        use_fixed_indices=use_fixed_indices,
        selected_factors=selected_factors  # 🚀 新增：传递特征选择参数
    )
    test_loader  = get_dataloader(
        "test",  
        config, 
        keep_meta=keep_meta_eval, 
        max_samples=max_samples_test,
        use_fixed_indices=use_fixed_indices,
        selected_factors=selected_factors  # 🚀 新增：传递特征选择参数
    )
    return train_loader, valid_loader, test_loader


def build_infer_dataset(wide_lag_df: pd.DataFrame, seq_len: int = 30) -> "torch.utils.data.Dataset":
    """
    将从DB获取的 wide+lag 格式数据转换为PyTorch Dataset，用于模型推理
    🔧 现在使用全局WideInferDataset类，解决Windows多进程问题
    
    Args:
        wide_lag_df: wide+lag格式的DataFrame，索引是日期，列是各种lag特征
        seq_len: 序列长度，需要与训练时保持一致
        
    Returns:
        WideInferDataset: 可用于推理的Dataset
    """
    # ✅ 使用全局类，避免pickle错误
    return WideInferDataset(wide_lag_df, seq_len)


def build_infer_dataset_from_wide(wide_df: pd.DataFrame, 
                                  seq_len: int = 30,
                                  feature_cols: List[str] = None) -> "torch.utils.data.Dataset":
    """
    从宽格式DataFrame构建推理用Dataset的便捷函数（工厂函数）
    🔧 现在使用全局WideInferDataset类，解决Windows多进程问题
    
    Args:
        wide_df: 宽格式DataFrame，包含trade_date, stock_code和各种特征列
        seq_len: 序列长度
        feature_cols: 特征列名列表，如果为None则自动检测
        
    Returns:
        WideInferDataset: 推理用Dataset
    """
    # ✅ 使用全局类，避免pickle错误，保持旧接口不变
    return WideInferDataset(wide_df, seq_len, feature_cols)

