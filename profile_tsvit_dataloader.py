
#!/usr/bin/env python
"""Standalone profiler for the TSViT parquet dataloader pipeline."""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import Any, Dict

import torch

from src.train.Transformer.TSVIT.config import TSViTConfig
import src.dataloader.DataLoader as dataloader_module
from src.dataset.DFZQ_GRU_PV_dataset import parquet_pv_dataset_debug as debug_dataset


def _build_dataloader_config(config: TSViTConfig, args: argparse.Namespace) -> Dict[str, Any]:
    dataset_path = args.dataset_path or config.dataset_path
    if dataset_path is None:
        raise ValueError("Dataset path must be provided either via YAML or --dataset-path")

    chunk_size = args.chunk_size
    if chunk_size is None:
        chunk_size = getattr(config, "chunk_size", None) or getattr(config, "batch_size", None)

    num_workers = args.num_workers
    if num_workers is None:
        num_workers = getattr(config, "num_workers", 0) or 0

    dl_cfg: Dict[str, Any] = {
        "dataset_path": str(dataset_path),
        "batch_size": getattr(config, "batch_size", None),
        "num_workers": num_workers,
        "shuffle": getattr(config, "shuffle", False),
        "seed": getattr(config, "seed", None),
        "chunk_size": chunk_size,
        "memory_limit": getattr(config, "memory_limit", "16GB"),
        "use_fixed_indices": getattr(config, "use_fixed_indices", True),
        "prefetch_factor": args.prefetch_factor
            if args.prefetch_factor is not None
            else getattr(config, "prefetch_factor", None),
        "duck_threads": getattr(config, "duck_threads", 8),
        "duck_memory": getattr(config, "duck_memory", "16GB"),
        "duck_cache": getattr(config, "duck_cache", "8GB"),
        "duck_materialize": getattr(config, "duck_materialize", True),
        "duck_persist_conn": getattr(config, "duck_persist_conn", True),
        "pin_memory": getattr(config, "pin_memory", True),
        "persistent_workers": getattr(config, "persistent_workers", True),
        "use_custom_splits": getattr(config, "use_custom_splits", False),
        "date_ranges": getattr(config, "date_ranges", None),
        "selected_factors": getattr(config, "selected_factors", None),
        "batch_by": args.batch_by or getattr(config, "batch_by", None) or "chunk",
        # Debug/monitoring knobs
        "debug_profile_io": True,
        "debug_profile_top_n": args.profile_top_n,
        "debug_profile_log_every_batches": args.profile_log_every,
        "debug_profile_history": args.profile_history,
    }

    return dl_cfg


def _describe_batch(batch: Any) -> str:
    if isinstance(batch, (list, tuple)) and batch:
        first = batch[0]
        if isinstance(first, torch.Tensor):
            return f"tensor{tuple(first.shape)} {first.dtype}"
    if isinstance(batch, torch.Tensor):
        return f"tensor{tuple(batch.shape)} {batch.dtype}"
    return type(batch).__name__


def _iterate_loader(loader, max_batches: int | None) -> None:
    profile_logger = logging.getLogger("profile")
    total_start = time.perf_counter()
    step_start = total_start
    processed = 0
    for idx, batch in enumerate(loader):
        now = time.perf_counter()
        load_time = now - step_start
        processed = idx + 1
        profile_logger.info(
            "step=%d load_time=%.3fs batch=%s",
            idx,
            load_time,
            _describe_batch(batch),
        )
        if max_batches is not None and processed >= max_batches:
            break
        step_start = time.perf_counter()
    total_elapsed = time.perf_counter() - total_start
    profile_logger.info("completed %d batches in %.3fs", processed, total_elapsed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile TSViT parquet dataloader")
    parser.add_argument("--config", type=Path, default=Path("configs/models/transformer/tsvit.yaml"),
                        help="Path to TSViT YAML config")
    parser.add_argument("--dataset-path", type=Path, default=None,
                        help="Override dataset path")
    parser.add_argument("--split", choices=["train", "valid", "test"], default="train",
                        help="Which split to iterate")
    parser.add_argument("--batch-by", choices=["chunk", "date"], default=None,
                        help="Override batch grouping mode")
    parser.add_argument("--chunk-size", type=int, default=None,
                        help="Override dataset chunk size")
    parser.add_argument("--num-workers", type=int, default=None,
                        help="Override dataloader workers")
    parser.add_argument("--prefetch-factor", type=int, default=None,
                        help="Override dataloader prefetch factor")
    parser.add_argument("--max-batches", type=int, default=8,
                        help="Number of batches to iterate (default: 8)")
    parser.add_argument("--log-level", default="INFO",
                        help="Root log level (default: INFO)")
    parser.add_argument("--profile-top-n", type=int, default=6,
                        help="How many timing entries to show per summary")
    parser.add_argument("--profile-log-every", type=int, default=0,
                        help="Log intermediate dataset profile every N DuckDB batches (0=only final)")
    parser.add_argument("--profile-history", type=int, default=8,
                        help="Profiling history size to retain in dataset (num_workers=0 mode)")
    parser.add_argument("--only-init", action="store_true",
                        help="Build dataloader and exit without iterating")

    args = parser.parse_args()

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    config = TSViTConfig.from_yaml(str(args.config))
    if args.dataset_path is not None:
        config.dataset_path = str(args.dataset_path)
    if args.batch_by is not None:
        config.batch_by = args.batch_by
    if args.chunk_size is not None:
        config.chunk_size = args.chunk_size
    if args.num_workers is not None:
        config.num_workers = args.num_workers
    if args.prefetch_factor is not None:
        config.prefetch_factor = args.prefetch_factor

    dl_cfg = _build_dataloader_config(config, args)
    date_grouping = dl_cfg["batch_by"] == "date"

    # Swap in the debug dataset implementation
    dataloader_module.ParquetPVDataset = debug_dataset.ParquetPVDataset

    train_loader, valid_loader, test_loader = dataloader_module.get_train_valid_test_loaders(
        dl_cfg,
        keep_meta_train=date_grouping,
        keep_meta_eval=False,
        max_samples_train=getattr(config, "max_samples_train", None),
        max_samples_valid=getattr(config, "max_samples_valid", None),
        max_samples_test=getattr(config, "max_samples_test", None),
        use_fixed_indices=dl_cfg.get("use_fixed_indices", True),
        selected_factors=getattr(config, "selected_factors", None),
    )

    loader_map = {"train": train_loader, "valid": valid_loader, "test": test_loader}
    target_loader = loader_map[args.split]

    logging.getLogger(__name__).info(
        "Profiling split=%s chunk_size=%s num_workers=%s batch_by=%s", 
        args.split,
        dl_cfg["chunk_size"],
        dl_cfg["num_workers"],
        dl_cfg["batch_by"],
    )

    if args.only_init:
        logging.getLogger(__name__).info("Dataset/Dataloader initialised (--only-init), skipping iteration")
        return

    _iterate_loader(target_loader, args.max_batches)

    dataset = target_loader.dataset
    last_stats = getattr(dataset, "_last_iter_stats", None)
    if last_stats:
        logging.getLogger(__name__).info(
            "Last iterator stats (single-worker runs): %s",
            last_stats,
        )


if __name__ == "__main__":
    main()
