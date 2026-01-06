# -*- coding: utf-8 -*-
"""
DuckwideDataloader 集成入口：提供统一的 DataLoader 构建接口。
支持两种 IterableDataset 实现：
  - window：沿用旧版 SQL 窗口 + list_extract 的实现
  - ring  ：全新的环形缓冲 streaming 实现（默认）
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from torch.utils.data import DataLoader

from src.dataset.dataset_process.wide_seq_dataset import (
    build_duckwide_dataset as build_window_dataset,
    duckwide_worker_init_fn,
)
from src.dataset.dataset_process.wide_seq_streaming import (
    build_duckwide_streaming_dataset as build_ring_dataset,
)


def _resolve_prefetch(n_workers: int, override: Optional[int]) -> Optional[int]:
    if override is not None:
        return int(override)
    if n_workers >= 4:
        return 1
    if n_workers > 0:
        return 2
    return None


def get_duckwide_dataloader(
    split: Optional[str],
    config: Mapping[str, Any],
    *,
    batch_size: Optional[int] = None,
    num_workers: Optional[int] = None,
    prefetch_factor: Optional[int] = None,
    keep_meta: bool = False,
) -> DataLoader:
    """
    构造基于 wide_daily + indices 的 IterableDataset DataLoader。
    通过 config["dataset_impl"] 选择实现：
      - "ring"/"stream"/"streaming": 使用环形缓冲 streaming 实现
      - 其它/默认值 : 使用旧的 SQL 窗口实现
    """
    _ = batch_size  # IterableDataset 自行控制批次

    impl = str(config.get("dataset_impl", "ring")).lower()
    use_ring = impl in ("ring", "stream", "streaming")

    cfg = dict(config)

    if num_workers is not None:
        n_workers = int(num_workers)
    else:
        n_workers = int(config.get("num_workers", 0))

    if use_ring:
        # 环形缓冲实现建议单进程，避免多 worker 重复构建 ring
        if n_workers != 0:
            n_workers = 0
        dataset = build_ring_dataset(split, cfg, keep_meta=keep_meta)
    else:
        if split is None:
            raise ValueError("window 模式需要显式指定 split")
        if n_workers > 0:
            cfg["persist_connection"] = False
        else:
            cfg["persist_connection"] = bool(config.get("persist_connection", True))
        dataset = build_window_dataset(split, cfg, keep_meta=keep_meta)

    effective_prefetch = _resolve_prefetch(n_workers, prefetch_factor)

    loader_kwargs: dict[str, Any] = {
        "batch_size": None,
        "num_workers": n_workers,
        "pin_memory": bool(config.get("pin_memory", True)),
        "drop_last": False,
        "shuffle": False,
        "persistent_workers": n_workers > 0,
        "collate_fn": None,
    }

    if n_workers > 0 and not use_ring:
        loader_kwargs["worker_init_fn"] = duckwide_worker_init_fn
        loader_kwargs["timeout"] = int(config.get("loader_timeout", 300))
        if effective_prefetch is not None:
            loader_kwargs["prefetch_factor"] = int(effective_prefetch)

    return DataLoader(dataset, **loader_kwargs)


def get_duckwide_train_valid_test_loaders(
    config: Mapping[str, Any],
    *,
    keep_meta_train: bool = False,
    keep_meta_eval: bool = False,
) -> Tuple[DataLoader, DataLoader, DataLoader]:
    """一次性返回 train/valid/test 三个 Loader。"""
    train_loader = get_duckwide_dataloader("train", config, keep_meta=keep_meta_train)
    valid_loader = get_duckwide_dataloader("valid", config, keep_meta=keep_meta_eval)
    test_loader = get_duckwide_dataloader("test", config, keep_meta=keep_meta_eval)
    return train_loader, valid_loader, test_loader
