# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from pathlib import Path
from typing import List, Sequence
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds


def _write_chunk(df: pd.DataFrame, shard_dir: Path, feature_cols: List[str], 
                 label: str, mask_cols: List[str]):
    df['trade_date'] = (
        pd.to_datetime(df['trade_date'], errors='coerce')
          .dt.strftime('%Y%m%d')
    )
    df = df.assign(
        year  = df['trade_date'].str.slice(0, 4),
        month = df['trade_date'].str.slice(4, 6)
    )
    if 'index' in df.columns:
        df.drop(columns=['index'], inplace=True)
    expected_cols = set(feature_cols + mask_cols + [label, 'trade_date', 'stock_code', 'year', 'month'])
    actual_cols = set(df.columns)
    missing_feature_cols = set(feature_cols) - actual_cols
    missing_mask_cols = set(mask_cols) - actual_cols
    if missing_feature_cols:
        feature_cols_data = {col: np.full(len(df), np.float32(0.0), dtype=np.float32) for col in missing_feature_cols}
        feature_df = pd.DataFrame(feature_cols_data, index=df.index)
        df = pd.concat([df, feature_df], axis=1)
    if missing_mask_cols:
        mask_cols_data = {col: np.full(len(df), np.uint8(0), dtype=np.uint8) for col in missing_mask_cols}
        mask_df = pd.DataFrame(mask_cols_data, index=df.index)
        df = pd.concat([df, mask_df], axis=1)
    for col in feature_cols:
        if col in df.columns:
            df[col] = df[col].astype(np.float32)
    for col in mask_cols:
        if col in df.columns:
            df[col] = df[col].fillna(0).astype("uint8")
    actual_cols = set(df.columns)
    extra_cols = actual_cols - expected_cols
    if extra_cols:
        df = df.drop(columns=list(extra_cols))
    schema_dict = {c: pa.float32() for c in feature_cols}
    schema_dict[label] = pa.float32()
    for c in mask_cols:
        schema_dict[c] = pa.uint8()
    for c in ['trade_date', 'stock_code', 'year', 'month']:
        schema_dict[c] = pa.string()
    schema = pa.schema([pa.field(k, v) for k, v in schema_dict.items()])
    tbl = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    parquet_fmt = ds.ParquetFileFormat()
    file_opts   = parquet_fmt.make_write_options(compression='zstd')
    ds.write_dataset(
        tbl,
        base_dir=str(shard_dir),
        format=parquet_fmt,
        file_options=file_opts,
        partitioning=['year', 'month'],
        existing_data_behavior='overwrite_or_ignore',
    )


def _write_chunk_simple(df: pd.DataFrame, shard_dir: Path, feature_cols: List[str], label: str):
    df['trade_date'] = (
        pd.to_datetime(df['trade_date'], errors='coerce')
          .dt.strftime('%Y%m%d')
    )
    df = df.assign(
        year  = df['trade_date'].str.slice(0, 4),
        month = df['trade_date'].str.slice(4, 6)
    )
    if 'index' in df.columns:
        df.drop(columns=['index'], inplace=True)
    expected_cols = set(feature_cols + [label, 'trade_date', 'stock_code', 'year', 'month'])
    actual_cols = set(df.columns)
    missing_feature_cols = set(feature_cols) - actual_cols
    if missing_feature_cols:
        missing_cols_data = {col: np.full(len(df), np.float32(0.0), dtype=np.float32) for col in missing_feature_cols}
        missing_df = pd.DataFrame(missing_cols_data, index=df.index)
        df = pd.concat([df, missing_df], axis=1)
    extra_cols = actual_cols - expected_cols
    if extra_cols:
        df = df.drop(columns=list(extra_cols))
    schema_dict = {c: pa.float32() for c in feature_cols}
    schema_dict[label] = pa.float32()
    for c in ['trade_date', 'stock_code', 'year', 'month']:
        schema_dict[c] = pa.string()
    schema = pa.schema([pa.field(k, v) for k, v in schema_dict.items()])
    tbl = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    parquet_fmt = ds.ParquetFileFormat()
    file_opts   = parquet_fmt.make_write_options(compression='zstd')
    ds.write_dataset(
        tbl,
        base_dir=str(shard_dir),
        format=parquet_fmt,
        file_options=file_opts,
        partitioning=['year', 'month'],
        existing_data_behavior='overwrite_or_ignore',
    )


def write_wide_daily(
    df: pd.DataFrame,
    base_dir: Path,
    feature_cols: Sequence[str],
    label_col: str,
) -> None:
    """
    Write a daily wide table (one row per date-stock) partitioned by year/month/day.
    """
    if df.empty:
        return

    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y%m%d")
    out["stock_code"] = out["stock_code"].astype(str)

    feature_cols = list(feature_cols)
    missing_features = [col for col in feature_cols if col not in out.columns]
    if missing_features:
        filler = pd.DataFrame(
            np.full((len(out), len(missing_features)), np.nan, dtype=np.float32),
            index=out.index,
            columns=missing_features,
        )
        out = pd.concat([out, filler], axis=1)
    existing_features = [col for col in feature_cols if col in out.columns]
    if existing_features:
        out[existing_features] = out[existing_features].astype("float32")

    label_present = label_col in out.columns
    if label_present:
        out[label_col] = out[label_col].astype("float32")

    base_cols = ["trade_date", "stock_code"] + feature_cols
    if label_present:
        base_cols.append(label_col)
    out = out.loc[:, [c for c in base_cols if c in out.columns]]

    ymd = pd.DataFrame(
        {
            "year": out["trade_date"].str.slice(0, 4),
            "month": out["trade_date"].str.slice(4, 6),
            "day": out["trade_date"].str.slice(6, 8),
        },
        index=out.index,
    )
    out = pd.concat([out, ymd], axis=1)

    table = pa.Table.from_pandas(out, preserve_index=False)
    partitioning = ds.partitioning(
        pa.schema(
            [
                pa.field("year", pa.string()),
                pa.field("month", pa.string()),
                pa.field("day", pa.string()),
            ]
        ),
        flavor="hive",
    )

    target_dir = base_dir / "shards" / "wide_daily"
    target_dir.mkdir(parents=True, exist_ok=True)
    ds.write_dataset(
        table,
        base_dir=str(target_dir),
        format="parquet",
        partitioning=partitioning,
        existing_data_behavior="overwrite_or_ignore",
    )


