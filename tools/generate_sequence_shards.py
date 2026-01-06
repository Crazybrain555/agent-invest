#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
CLI tool to precompute sequence shards (features_seq_lag={T}) for pv datasets.

Example:
    python tools/generate_sequence_shards.py \
        --dataset-root data/Dataset/pv_v6 \
        --lags 30,300 \
        --threads 4 \
        --memory-limit 12GB
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import List


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data_service.pipelines.Dataset_builder.lag_duckdb import (  # noqa: E402
    build_sequence_shards,
    update_sequence_schema,
)


def _parse_lags(value: str) -> List[int]:
    try:
        return [int(v.strip()) for v in value.split(",") if v.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid lag list: {value}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate precomputed LIST-sequence shards for pv datasets."
    )
    parser.add_argument(
        "--dataset-root",
        required=True,
        help="Path to dataset root directory (must contain shards/features_long).",
    )
    parser.add_argument(
        "--lags",
        type=_parse_lags,
        default=[30,300],
        help="Comma-separated lag values to build (default: 30).",
    )
    parser.add_argument(
        "--factors",
        default="auto",
        help="Factor specification: 'auto' or comma-separated names.",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="DuckDB worker threads (default: 4).",
    )
    parser.add_argument(
        "--memory-limit",
        default="32GB",
        help="DuckDB memory limit (default: 16GB).",
    )
    parser.add_argument(
        "--temp-dir",
        default=None,
        help="DuckDB temp directory (default: <dataset_root>/duck_tmp).",
    )
    parser.add_argument(
        "--join-with",
        choices=("labels", "full_indices"),
        default="labels",
        help="Which table to restrict output keys to (default: labels).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing shards if present.",
    )
    parser.add_argument(
        "--no-fill-zero",
        action="store_true",
        help="Do not replace NULL with 0 in LIST columns during build.",
    )
    parser.add_argument(
        "--hide-progress",
        action="store_true",
        help="Suppress year-level progress bar output.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    dataset_root = Path(args.dataset_root).resolve()
    if not dataset_root.exists():
        parser.error(f"dataset root {dataset_root} does not exist")

    temp_dir = Path(args.temp_dir).resolve() if args.temp_dir else dataset_root / "duck_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    if args.factors == "auto":
        factors = "auto"
    else:
        factors = [f.strip() for f in args.factors.split(",") if f.strip()]
        if not factors:
            parser.error("factors list is empty")

    entries = []
    for lag in args.lags:
        logging.info("Building sequence shard lag=%s ...", lag)
        info = build_sequence_shards(
            dataset_root=dataset_root,
            lag=int(lag),
            factors=factors,
            threads=args.threads,
            memory_limit=args.memory_limit,
            temp_dir=temp_dir,
            fill_missing_zero=not args.no_fill_zero,
            join_with=args.join_with,
            force=args.force,
            show_progress=not args.hide_progress,
        )
        entries.append(info)
        logging.info("Sequence shard lag=%s generated under %s", lag, info.get("path"))

    update_sequence_schema(dataset_root, entries)
    lag_list = ",".join(str(e["lag"]) for e in entries)
    logging.info(
        "schema.json updated with sequence shard metadata for lags: %s", lag_list
    )
    logging.info("Sequence shard roots are relative to dataset: %s", [e["path"] for e in entries])


if __name__ == "__main__":
    main()
