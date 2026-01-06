# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from pathlib import Path
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

logger = logging.getLogger(__name__)


def _normalize_long(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out['trade_date'] = pd.to_datetime(out['trade_date']).dt.strftime('%Y%m%d')
    out['stock_code'] = out['stock_code'].astype(str)

    if 'factor_value' not in out.columns and 'value' in out.columns:
        out = out.rename(columns={'value': 'factor_value'})
    if 'factor_name' not in out.columns and 'field_name' in out.columns:
        out = out.rename(columns={'field_name': 'factor_name'})

    if 'z_windows' in out.columns:
        has_suffix = out['factor_name'].astype(str).str.contains(r'_w\d+$')
        if not has_suffix.all():
            out['factor_name'] = (
                out['factor_name'].astype(str)
                + '_w'
                + out['z_windows'].astype(int).astype(str)
            )

    cols = ['trade_date', 'stock_code', 'factor_name', 'factor_value']
    missing = set(cols) - set(out.columns)
    if missing:
        raise KeyError(f"long writer missing columns: {sorted(missing)}")

    out['factor_value'] = out['factor_value'].astype('float32')
    return out[cols]


def write_features_long(
    df: pd.DataFrame,
    base_dir: Path,
    dropna_factor_value: bool = True,
) -> None:
    normalized = _normalize_long(df)

    removed = 0
    if dropna_factor_value:
        before = len(normalized)
        normalized = normalized.dropna(subset=['factor_value'])
        removed = before - len(normalized)
        if removed:
            logger.info(
                "Removed %d feature rows with NaN factor_value before writing",
                removed,
            )

    if normalized.empty:
        if removed:
            logger.info(
                "Skip writing features chunk because all rows were NaN after dropna",
            )
        return

    normalized['year'] = normalized['trade_date'].str.slice(0, 4)
    normalized['month'] = normalized['trade_date'].str.slice(4, 6)
    normalized['day'] = normalized['trade_date'].str.slice(6, 8)

    table = pa.Table.from_pandas(normalized, preserve_index=False)
    partitioning = ds.partitioning(
        pa.schema([
            pa.field('year', pa.string()),
            pa.field('month', pa.string()),
            pa.field('day', pa.string()),
        ]),
        flavor='hive',
    )
    ds.write_dataset(
        table,
        base_dir=str(base_dir / 'shards' / 'features_long'),
        format='parquet',
        partitioning=partitioning,
        existing_data_behavior='overwrite_or_ignore',
    )


def write_labels_long(df: pd.DataFrame, label_col: str, base_dir: Path) -> None:
    out = df.copy()
    out['trade_date'] = pd.to_datetime(out['trade_date']).dt.strftime('%Y%m%d')
    out['stock_code'] = out['stock_code'].astype(str)

    cols = ['trade_date', 'stock_code', label_col]
    missing = set(cols) - set(out.columns)
    if missing:
        raise KeyError(f"label writer missing columns: {sorted(missing)}")

    out = out[cols]
    out[label_col] = out[label_col].astype('float32')
    out['year'] = out['trade_date'].str.slice(0, 4)
    out['month'] = out['trade_date'].str.slice(4, 6)
    out['day'] = out['trade_date'].str.slice(6, 8)

    table = pa.Table.from_pandas(out, preserve_index=False)
    partitioning = ds.partitioning(
        pa.schema([
            pa.field('year', pa.string()),
            pa.field('month', pa.string()),
            pa.field('day', pa.string()),
        ]),
        flavor='hive',
    )
    ds.write_dataset(
        table,
        base_dir=str(base_dir / 'shards' / 'labels'),
        format='parquet',
        partitioning=partitioning,
        existing_data_behavior='overwrite_or_ignore',
    )
