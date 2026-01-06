#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
CLI tool to generate unified indices for dataset (replacing ok_keys).
"""
import argparse
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.data_service.pipelines.Dataset_builder.indices import generate_indices


def main():
    parser = argparse.ArgumentParser(description="Generate unified indices for dataset")
    parser.add_argument("--dataset-root", type=str, default="data/Dataset/pv_v7",
                        help="Path to dataset root directory (default: data/Dataset/pv_v7)")
    parser.add_argument("--lags", type=str, default="30,300,500",
                        help="Comma-separated lag values (default: 30,300,500)")
    parser.add_argument("--factors", type=str, default="auto",
                        help="Factor specification (default: auto)")
    parser.add_argument("--threads", type=int, default=6,
                        help="Number of threads for DuckDB (default: 6)")
    parser.add_argument("--temp-dir", type=str, default=None,
                        help="Temporary directory for DuckDB (default: {dataset_root}/duck_tmp)")
    parser.add_argument("--memory-limit", type=str, default="16GB",
                        help="Memory limit for DuckDB (default: 16GB)")
    parser.add_argument("--skip-splits", action="store_true",
                        help="Skip generating split indices (default: False)")
    parser.add_argument("--force", action="store_true",
                        help="Force regeneration even if files exist (default: False)")
    parser.add_argument("--min-non-null", type=int, default=None,
                        help="Minimum non-null count within the lag window for ANY factor to mark a key as ready. "
                             "Default per lag = max(10, lag//10) if not provided.")
    parser.add_argument("--no-require-label-train", action="store_true",
                        help="Do not require has_label=1 for training indices (default: require label)")
    parser.add_argument("--progress", dest="progress", action="store_true",
                        help="Enable DuckDB progress bar output (default: enabled)")
    parser.add_argument("--no-progress", dest="progress", action="store_false",
                        help="Disable DuckDB progress bar output")
    parser.set_defaults(progress=True)
    
    args = parser.parse_args()
    
    lags = [int(x.strip()) for x in args.lags.split(",") if x.strip()]
    if not lags:
        raise ValueError("At least one lag must be specified via --lags")

    dataset_root = Path(args.dataset_root).resolve()
    # 如果未指定 temp_dir，默认使用 {dataset_root}/duck_tmp
    temp_dir = Path(args.temp_dir).resolve() if args.temp_dir else (dataset_root / "duck_tmp")
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"📊 配置:")
    print(f"  数据集根目录: {dataset_root}")
    print(f"  滞后天数: {lags}")
    print(f"  因子: {args.factors}")
    print(f"  线程数: {args.threads}")
    print(f"  临时目录: {temp_dir}")
    print(f"  内存限制: {args.memory_limit}")
    print(f"  生成分割索引: {'否' if args.skip_splits else '是'}")
    print(f"  强制重建: {'是' if args.force else '否'}")
    print(f"  训练要求标签: {'否' if args.no_require_label_train else '是'}")
    print(f"  DuckDB 进度条: {'是' if args.progress else '否'}")
    print()

    generate_indices(
        dataset_root=str(dataset_root),
        lags=lags,
        factors=args.factors,
        threads=args.threads,
        temp_dir=temp_dir,
        memory_limit=args.memory_limit,
        with_splits=not args.skip_splits,
        force=args.force,
        min_non_null=args.min_non_null,
        require_label_for_train=not args.no_require_label_train,
        show_progress=args.progress,
    )


if __name__ == "__main__":
    main()
