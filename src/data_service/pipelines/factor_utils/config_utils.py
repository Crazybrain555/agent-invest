#!/usr/bin/env python3
"""
Utilities to resolve experiment/schema configuration and unify data fetching & column alignment.

Provides:
- resolve_experiment_and_schema: merge experiment_config.json and dataset schema.json
- detect_dataset_last_date: infer the last available date in the dataset
- align_df_to_factor_order: align wide+lag DataFrame to factor_order × lag_0..lag_{L-1}
- build_fetch_cfg: construct a lightweight cfg object for db fetcher
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any
import json
import pandas as pd
import numpy as np

from src.utils.experiment_utils import load_experiment_config


@dataclass
class ResolvedConfig:
    dataset_path: str
    selected_factors: Optional[List[str]] = None
    seq_len: int = 30
    features_tables: List[str] = field(default_factory=list)
    labels_table: Optional[str] = None
    label_name: Optional[str] = None
    restricted_table: Optional[str] = None
    stats_table: Optional[str] = None
    clip_std: bool = True
    factor_based_nan_handling: bool = True
    consecutive_nan_threshold: Optional[int] = None
    winsorise_labels: bool = True
    label_shift: int = 10


def _read_schema(dataset_path: str | Path) -> Dict[str, Any]:
    schema_path = Path(dataset_path) / "meta" / "schema.json"
    if not schema_path.exists():
        return {}
    with open(schema_path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def resolve_experiment_and_schema(model_path: str, fallback_dataset_path: Optional[str] = None) -> ResolvedConfig:
    exp = load_experiment_config(model_path) or {}

    training = exp.get("training_config", {})
    model_cfg = exp.get("model_config", {})
    exp_info = exp.get("experiment_info", {})

    dataset_path = (
        training.get("dataset_path")
        or exp_info.get("dataset_path")
        or fallback_dataset_path
        or "data/Dataset/pv_v5_pv_v5_pvhflow_v2"
    )

    schema = _read_schema(dataset_path)
    tables = schema.get("tables", {}) if isinstance(schema, dict) else {}

    # features tables: list or dict in schema
    features_tables: List[str] = []
    feats = tables.get("features") if isinstance(tables, dict) else None
    if isinstance(feats, list):
        features_tables = [d.get("name") for d in feats if isinstance(d, dict) and d.get("name")]
    elif isinstance(feats, dict) and feats.get("name"):
        features_tables = [feats.get("name")]

    # label and others
    labels_table = (tables.get("labels") or {}).get("name") if isinstance(tables, dict) else None
    restricted_table_tables = (tables.get("restricted") or {}).get("name") if isinstance(tables, dict) else None
    restricted_table_root = schema.get("restricted_table") if isinstance(schema, dict) else None
    stats_table_schema = (tables.get("stats") or {}).get("name") if isinstance(tables, dict) else None

    # selected factors from experiment
    selected_factors = training.get("selected_factors") or model_cfg.get("actual_features")

    # seq_len
    seq_len = None
    seq_len_candidates = [
        training.get("sequence_length"),
        training.get("seq_len"),
        training.get("T"),
        training.get("lag"),
        model_cfg.get("sequence_length"),
        model_cfg.get("seq_len"),
        model_cfg.get("T"),
        exp_info.get("T"),
        (schema.get("sequence_length") if isinstance(schema, dict) else None),
        training.get("feature_lag"),
        model_cfg.get("feature_lag"),
        (schema.get("feature_lag") if isinstance(schema, dict) else None),
        (schema.get("sequence_lag") if isinstance(schema, dict) else None),
    ]
    for candidate in seq_len_candidates:
        if candidate in (None, ""):
            continue
        try:
            seq_val = int(candidate)
        except (TypeError, ValueError):
            try:
                seq_val = int(float(candidate))
            except (TypeError, ValueError):
                continue
        if seq_val > 0:
            seq_len = seq_val
            break
    if seq_len is None:
        seq_len = 30

    resolved = ResolvedConfig(
        dataset_path=dataset_path,
        selected_factors=selected_factors,
        seq_len=int(seq_len),
        features_tables=features_tables or training.get("features_tables", []),
        labels_table=training.get("labels_table") or labels_table,
        label_name=(schema.get("label_col") if isinstance(schema, dict) else None)
        or training.get("label_name")
        or "tc_t10_n30_adj",
        restricted_table=(
            training.get("restricted_table")
            or restricted_table_tables
            or restricted_table_root
        ),
        stats_table=training.get("stats_table") or stats_table_schema,
        clip_std=bool(training.get("clip_std", True)),
        factor_based_nan_handling=bool(training.get("factor_based_nan_handling", True)),
        consecutive_nan_threshold=training.get("consecutive_nan_threshold"),
        winsorise_labels=bool(training.get("winsorise_labels", True)),
        label_shift=int(training.get("label_shift", 10)),
    )

    # fallback defaults for features_tables
    if not resolved.features_tables:
        resolved.features_tables = [
            "ai_is.inter_train_factors_mkt_processed_v3",
            "ai_is.quantitative_other_signals",
        ]

    return resolved


def detect_dataset_last_date(dataset_path: str | Path) -> Optional[str]:
    try:
        import pyarrow.parquet as pq
        dataset_path = Path(dataset_path)
        # prefer splits.parquet
        splits_file = dataset_path / "meta" / "splits.parquet"
        if splits_file.exists():
            df = pq.read_table(splits_file).to_pandas()
            if "trade_date" in df.columns:
                last_date = df["trade_date"].max()
                if isinstance(last_date, str):
                    return last_date.replace("-", "")
                return pd.to_datetime(last_date).strftime("%Y%m%d")
        # fallback: inspect any shard
        shards = list((dataset_path / "shards").rglob("*.parquet"))
        if shards:
            df0 = pq.read_table(shards[0]).to_pandas()
            if "trade_date" in df0.columns and not df0.empty:
                last_date = df0["trade_date"].max()
                if isinstance(last_date, str):
                    return last_date.replace("-", "")
                return pd.to_datetime(last_date).strftime("%Y%m%d")
    except Exception:
        return None
    return None


def align_df_to_factor_order(df: pd.DataFrame, factor_order: List[str], seq_len: int) -> pd.DataFrame:
    """Align columns to ['trade_date','stock_code'] + factor_order × lag_0..lag_{L-1}.
    Missing factor lag columns are filled with 0.0; extra lag columns are dropped.
    """
    if df.index.name == "trade_date" or "trade_date" not in df.columns:
        df = df.reset_index()
        if "index" in df.columns:
            df = df.rename(columns={"index": "trade_date"})
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["stock_code"] = df["stock_code"].astype(str)

    expected = ["trade_date", "stock_code"] + [
        f"{f}_lag_{i}" for f in factor_order for i in range(seq_len)
    ]
    # add missing (batch) to avoid fragmentation
    missing = [c for c in expected if c not in ("trade_date", "stock_code") and c not in df.columns]
    if missing:
        add_df = pd.DataFrame({c: np.float32(0.0) for c in missing}, index=df.index)
        df = pd.concat([df, add_df], axis=1)
    # drop extras
    extra = [c for c in df.columns if ("_lag_" in c and c not in expected)]
    if extra:
        df = df.drop(columns=extra)
    # order
    keep = [c for c in expected if c in df.columns]
    df = df[keep].sort_values(["trade_date", "stock_code"]).reset_index(drop=True)
    return df


def build_fetch_cfg(resolved: ResolvedConfig, align_to_dataset: bool = True):
    """Create a lightweight config object with attributes consumed by fetch_wide_lag."""
    class FetchCfg:
        def __init__(self, r: ResolvedConfig, align_flag: bool) -> None:
            self.seq_len = r.seq_len
            self.features_tables = r.features_tables
            self.stats_table = r.stats_table
            self.clip_std = r.clip_std
            self.factor_based_nan_handling = r.factor_based_nan_handling
            self.consecutive_nan_threshold = r.consecutive_nan_threshold
            self.labels_table = r.labels_table
            self.label_name = r.label_name
            # 保持与历史脚本一致，如果 schema/exp 未声明限制表则回退至默认禁买池
            self.restricted_table = r.restricted_table or "ai_is.forbid_pool_comprehensive"
            self.align_to_dataset = align_flag
            # 控制长表透视为宽表时的因子批大小，降低内存峰值
            self.max_factors_per_batch = 16
            # 添加默认股票过滤配置
            self.code_prefix_blacklist = ["9"]  # 默认过滤 9 开头的股票
            self.code_blacklist = []             # 完整股票代码黑名单
    return FetchCfg(resolved, align_to_dataset)
