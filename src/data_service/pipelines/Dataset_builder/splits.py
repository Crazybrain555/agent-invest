# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from pathlib import Path
from typing import Sequence, Tuple
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def _apply_splits(df: pd.DataFrame, rules: Sequence[Tuple[str, str, str]]):
    if not rules:
        return None
    ser = pd.to_datetime(df.trade_date, errors='coerce')
    split = pd.Series("unused", index=df.index, dtype="object")
    for name, s, e in rules:
        mask = (ser >= pd.Timestamp(s)) & (ser <= pd.Timestamp(e))
        split[mask] = name
    out = df[["trade_date", "stock_code"]].copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y%m%d")
    out["split"] = split
    return out


def _generate_fixed_indices(splits_df: pd.DataFrame, meta_dir: Path):
    logger.info("生成固定顺序的索引文件...")
    required_cols = ["trade_date", "stock_code", "split"]
    if not all(col in splits_df.columns for col in required_cols):
        logger.error(f"splits_df缺少必要的列: {required_cols}")
        return
    splits_df = splits_df.copy()
    splits_df["trade_date"] = pd.to_datetime(splits_df["trade_date"]).dt.strftime("%Y%m%d")
    all_indices = splits_df.sort_values(["trade_date", "stock_code"]).copy()
    all_indices["index_id"] = np.arange(len(all_indices))
    full_indices_path = meta_dir / "full_indices.parquet"
    pq.write_table(
        pa.Table.from_pandas(all_indices),
        full_indices_path,
        compression="zstd"
    )
    logger.info(f"全局固定索引已保存至 {full_indices_path}，包含 {len(all_indices)} 条记录")
    for split_name in splits_df["split"].unique():
        if split_name == "unused":
            continue
        split_indices = all_indices[all_indices["split"] == split_name].copy()
        split_indices = split_indices.sort_values(["trade_date", "stock_code"])
        split_indices["index_id"] = np.arange(len(split_indices))
        split_path = meta_dir / f"{split_name}_indices.parquet"
        pq.write_table(
            pa.Table.from_pandas(split_indices),
            split_path,
            compression="zstd"
        )
        logger.info(f"{split_name}分割集固定索引已保存至 {split_path}，包含 {len(split_indices)} 条记录")


