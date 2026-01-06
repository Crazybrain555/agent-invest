#!/usr/bin/env python3
"""
Utilities for aligning wide+lag factor DataFrames to a dataset schema.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, List
import json
import pandas as pd
import numpy as np


def _load_schema_factor_lag_cols(dataset_path: str | Path) -> List[str]:
    dataset_path = Path(dataset_path)
    schema_path = dataset_path / "meta" / "schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.json 不存在: {schema_path}")
    with open(schema_path, "r", encoding="utf-8-sig") as f:
        schema = json.load(f)
    cols = schema.get("factor_lag_cols") or schema.get("feature_cols")
    if not cols:
        raise KeyError("schema.json 缺少 factor_lag_cols/feature_cols 字段")
    return list(cols)


def _infer_seq_len_from_columns(df: pd.DataFrame, factor_names: List[str]) -> Optional[int]:
    if df is None or df.empty:
        return None
    max_lag = -1
    columns = [str(c) for c in df.columns]
    for factor in factor_names:
        prefix = f"{factor}_lag_"
        for col in columns:
            if col.startswith(prefix):
                suffix = col[len(prefix):]
                if suffix.isdigit():
                    max_lag = max(max_lag, int(suffix))
    if max_lag < 0:
        return None
    return max_lag + 1


def _build_expected_from_fallback(
    df: pd.DataFrame, fallback_order: List[str], seq_len: Optional[int]
) -> List[str]:
    if not fallback_order:
        raise ValueError("fallback_order 不能为空")
    if seq_len is None:
        seq_len = _infer_seq_len_from_columns(df, fallback_order)
    if seq_len is None or seq_len <= 0:
        raise ValueError("无法从数据中推断有效的 seq_len")
    expected: List[str] = []
    for factor in fallback_order:
        for lag in range(seq_len):
            expected.append(f"{factor}_lag_{lag}")
    return expected


def align_wide_to_schema(
    df_wide_lag: pd.DataFrame,
    dataset_path: str | Path,
    allow_extra: bool = False,
    fill_value: float = 0.0,
    fallback_order: Optional[List[str]] = None,
    seq_len: Optional[int] = None,
) -> pd.DataFrame:
    """对齐宽表滞后特征的列集合和顺序。"""
    if df_wide_lag is None or len(df_wide_lag) == 0:
        return df_wide_lag

    out = df_wide_lag.copy()

    try:
        expected = _load_schema_factor_lag_cols(dataset_path)
    except (FileNotFoundError, KeyError) as schema_err:
        expected = None
        schema_error = schema_err
    else:
        schema_error = None

    if (not expected) and fallback_order:
        try:
            expected = _build_expected_from_fallback(out, fallback_order, seq_len)
        except ValueError as fallback_err:
            if schema_error is not None:
                raise schema_error
            raise fallback_err

    if not expected:
        if schema_error is not None:
            raise schema_error
        raise KeyError("无法从 schema.json 或 fallback_order 推断 factor lag 列")

    if 'trade_date' not in out.columns:
        out = out.reset_index()
        if 'index' in out.columns and 'trade_date' not in out.columns:
            out = out.rename(columns={'index': 'trade_date'})

    out['trade_date'] = pd.to_datetime(out['trade_date'], errors='coerce').dt.strftime('%Y%m%d')
    if 'stock_code' in out.columns:
        out['stock_code'] = out['stock_code'].astype(str)

    missing = [c for c in expected if c not in out.columns]
    if missing:
        add_df = pd.DataFrame({c: np.float32(fill_value) for c in missing}, index=out.index)
        out = pd.concat([out, add_df], axis=1)

    if not allow_extra:
        extra = [c for c in out.columns if ("_lag_" in str(c) and c not in expected)]
        if extra:
            out = out.drop(columns=extra)

    cast_cols = [c for c in expected if c in out.columns]
    if cast_cols:
        out[cast_cols] = out[cast_cols].astype(np.float32, copy=False)

    ordered = ['trade_date', 'stock_code'] + expected
    seen = set()
    keep = []
    for col in ordered:
        if col in out.columns and col not in seen:
            keep.append(col)
            seen.add(col)
    out = out[keep]

    out = out.sort_values(['trade_date', 'stock_code']).reset_index(drop=True)
    return out


