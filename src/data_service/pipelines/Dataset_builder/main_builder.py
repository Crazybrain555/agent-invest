# -*- coding: utf-8 -*-
from __future__ import annotations
import logging
logger = logging.getLogger(__name__)

from pathlib import Path
from typing import Sequence, Tuple, List, Set, Optional, Dict, Union

import re
# 第三方库的 import 按原文件保留
import gc, json, sys, os
from datetime import datetime, timedelta
import duckdb, numpy as np, pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from tqdm import tqdm

# 业务依赖（保留原绝对路径）
from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.data_service.preprocessing.methods.preprocess_factors import (
    preprocess_factors, preprocess_factors_long, pivot_long_to_wide,
    winsorize_labels_by_date, generate_lag_features, FactorPreprocessor
)
from src.utils.config_loader import ConfigLoader

# 同包内的功能拆分（新导入）
from .factor_windows import FACTOR_WINDOWS, get_all_factor_names, get_base_windows
from .helpers import _ensure_dirs, _iter_ranges
from .io_tables import _load_table_configs, _load_restricted_set
from .calendar_utils import _get_trading_days_before
from .stats_zscore import _load_stats_with_window, _apply_zscore_with_window
from .skeleton import _create_complete_factor_skeleton
from .pivoting import pivot_long_to_wide_simple, _complete_date_reindex
from .lag import generate_lag_features_simple
from .long_stage import build_long_preprocessed_with_zscore
from .lag_duckdb import build_sequence_lists_with_duckdb, build_seq_lists_and_write
from .writer_list import _write_chunk_list
from .labels import _fetch_labels_chunk, _compute_date_label_stats, _standardize_labels_by_date
from .fetch_long import _fetch_join_filter_chunk_long
from .fetch_multi import _resolve_feature_sources, _fetch_join_filter_chunk_multi
from .writer import _write_chunk, _write_chunk_simple, write_wide_daily
from .writer_long import write_features_long, write_labels_long
from .splits import _apply_splits, _generate_fixed_indices

DEFAULT_LABELS_TABLE = "ai_is.training_label_v1"



def build_pv_dataset_wide_daily(
    output_dir: str | Path,
    start_date: str,
    end_date: str,
    label_name: str,
    factor_windows: Dict[str, List[int]] | None = None,
    features_table: Union[str, Sequence[str]] = "ai_is.inter_train_factors_mkt_processed_v3",
    labels_table: str = DEFAULT_LABELS_TABLE,
    restricted_table: str = "ai_is.forbid_pool_comprehensive",
    split_rules: Sequence[Tuple[str, str, str]] | None = None,
    chunk_freq: str = "Q",
    label_shift: int = 10,
    winsorise_labels: bool = True,
    label_winsor_q: Tuple[float, float] = (0.0005, 0.9995),
    stats_table: Optional[str] = None,
    clip_std: bool = True,
    factor_based_nan_handling: bool = True,
    consecutive_nan_threshold: Optional[int] = None,
    warmup_days: int = 200,
    dropna_factor_value: bool = True,
    filter_features_restricted: bool = True,
    exclude_code_prefixes: Optional[List[str]] = None,
    exclude_codes_regex: Optional[str] = None,
    feature_lag_hint: int = 300,
    max_factors_per_batch: Optional[int] = None,
) -> None:
    out = Path(output_dir)
    meta_dir, shard_dir = _ensure_dirs(out)
    (shard_dir / "wide_daily").mkdir(parents=True, exist_ok=True)
    prov = LocalTestDBDataProvider()

    if factor_windows is None:
        factor_windows = FACTOR_WINDOWS.copy()

    features_tables = [features_table] if isinstance(features_table, str) else list(features_table)
    if not features_tables:
        raise ValueError("features_table must not be empty")

    prefix_tuple: Tuple[str, ...] = tuple(exclude_code_prefixes or [])
    code_pattern = re.compile(exclude_codes_regex) if exclude_codes_regex else None
    ranges = list(_iter_ranges(start_date, end_date, chunk_freq))

    def _apply_code_filters(df: pd.DataFrame, origin: str) -> Tuple[pd.DataFrame, int]:
        if df.empty or (not prefix_tuple and code_pattern is None):
            return df, 0
        working = df.copy()
        codes = working["stock_code"].astype(str)
        mask = pd.Series(True, index=working.index)
        if prefix_tuple:
            mask &= ~codes.str.startswith(prefix_tuple)
        if code_pattern is not None:
            mask &= ~codes.str.match(code_pattern)
        removed = int((~mask).sum())
        if removed:
            logger.info("Filtered %d rows in %s via stock code filters", removed, origin)
            working = working.loc[mask].reset_index(drop=True)
        return working, removed

    expanded_factor_names = sorted(
        {f"{name}_w{int(win)}" for name, wins in factor_windows.items() for win in wins}
    )

    def _filter_restricted_rows(
        df: pd.DataFrame,
        restricted_keys: Set[tuple[str, str]],
        log_template: str,
    ) -> pd.DataFrame:
        if (
            df.empty
            or not filter_features_restricted
            or not restricted_keys
        ):
            return df
        rk_df = pd.DataFrame(list(restricted_keys), columns=["trade_date", "stock_code"])
        if rk_df.empty:
            return df
        rk_df["__rk__"] = 1
        merged = df.merge(rk_df, on=["trade_date", "stock_code"], how="left")
        removed = int(merged["__rk__"].notna().sum())
        if removed:
            logger.info(log_template, removed)
        merged = merged[merged["__rk__"].isna()].drop(columns="__rk__").reset_index(drop=True)
        return merged

    use_factor_batches = False
    batch_size = 0
    factor_batches_defs: List[List[Tuple[str, int]]] = []
    base_factor_source_map: Optional[Dict[Tuple[str, int], Dict[str, str]]] = None
    schema_max_factors_per_batch: Optional[int] = None
    if max_factors_per_batch is not None:
        try:
            batch_size = int(max_factors_per_batch)
        except (TypeError, ValueError) as err:  # noqa: BLE001
            raise ValueError("max_factors_per_batch must be an integer") from err
        schema_max_factors_per_batch = batch_size
        if batch_size > 0:
            use_factor_batches = True

    if use_factor_batches:
        def _split_factor_column(col: str) -> Tuple[str, int]:
            base, sep, win_str = col.rpartition("_w")
            if not sep or not win_str:
                raise ValueError(f"Invalid factor column name without window suffix: {col}")
            try:
                return base, int(win_str)
            except ValueError as err:  # noqa: BLE001
                raise ValueError(f"Invalid window value in factor column: {col}") from err

        ordered_pairs: List[Tuple[str, int]] = [_split_factor_column(name) for name in expanded_factor_names]
        current_batch: List[Tuple[str, int]] = []
        for pair in ordered_pairs:
            current_batch.append(pair)
            if len(current_batch) >= batch_size:
                factor_batches_defs.append(current_batch)
                current_batch = []
        if current_batch:
            factor_batches_defs.append(current_batch)
        logger.info(
            "Using factor batching: total %d factors, batch size %d -> %d batches",
            len(expanded_factor_names),
            batch_size,
            len(factor_batches_defs),
        )

    splits_accum: List[pd.DataFrame] = []
    written_any = False

    for s, e in tqdm(ranges, desc=f"wide-daily shards -> {out}"):
        pad_start = s
        try:
            if warmup_days and int(warmup_days) > 0:
                pad_start = _get_trading_days_before(s, int(warmup_days))
        except Exception:
            pad_start = (
                pd.to_datetime(s) - pd.Timedelta(days=int(1.4 * int(warmup_days)))
            ).strftime("%Y%m%d")

        if not use_factor_batches:
            feats_long = build_long_preprocessed_with_zscore(
                prov,
                pad_start,
                e,
                factor_windows,
                features_tables,
                factor_based_nan_handling=factor_based_nan_handling,
                consecutive_nan_threshold=consecutive_nan_threshold,
                stats_table=stats_table,
                clip_std=clip_std,
            )
            if feats_long.empty:
                continue

            try:
                feats_long = feats_long[feats_long["trade_date"] >= s]
            except Exception:
                logger.debug("warmup trim fallback failed; leaving dataframe unchanged")

            feats_long, _ = _apply_code_filters(feats_long, "features")
            if feats_long.empty:
                logger.info("Skip %s-%s: no feature rows after code filters", s, e)
                continue

            restricted_keys: Set[tuple[str, str]] = (
                _load_restricted_set(prov, s, e, restricted_table) if restricted_table else set()
            )
            labels_df = _fetch_labels_chunk(
                prov,
                s,
                e,
                label_name,
                labels_table,
                restricted_keys,
                label_shift=label_shift,
            )
            if labels_df.empty:
                continue

            labels_df, _ = _apply_code_filters(labels_df, "labels")
            if labels_df.empty:
                logger.info("Skip %s-%s: no label rows after code filters", s, e)
                continue

            if winsorise_labels:
                labels_df = winsorize_labels_by_date(labels_df, label_name, label_winsor_q)

            feats_long = _filter_restricted_rows(
                feats_long,
                restricted_keys,
                "Filtered %d rows in features via restricted set",
            )
            if feats_long.empty:
                logger.info("Skip %s-%s: no feature rows after restricted filter", s, e)
                continue

            wide = pivot_long_to_wide_simple(
                feats_long,
                expanded_factor_names,
                "factor_name",
                "factor_value",
                0,
            )
            if wide.empty:
                logger.info("Skip %s-%s: pivot produced empty wide table", s, e)
                continue

            wide = _complete_date_reindex(wide, s, e)
            wide = _filter_restricted_rows(
                wide,
                restricted_keys,
                "Removed %d rows in wide table via restricted set",
            )

            wide = wide.merge(labels_df, on=["trade_date", "stock_code"], how="left")

            missing_factor_cols = [col for col in expanded_factor_names if col not in wide.columns]
            for col in missing_factor_cols:
                wide[col] = np.nan

            if dropna_factor_value:
                factor_subset = [col for col in expanded_factor_names if col in wide.columns]
                before = len(wide)
                wide = wide.dropna(axis=0, how="all", subset=factor_subset)
                dropped = before - len(wide)
                if dropped:
                    logger.info("Dropped %d all-null factor rows before writing", dropped)

            keys_final = wide.loc[:, ["trade_date", "stock_code"]].drop_duplicates()
            labels_on_wide = labels_df.merge(keys_final, on=["trade_date", "stock_code"], how="inner")
            logger.info(
                "Label sync: original %d, on_wide %d (%.1f%%)",
                len(labels_df),
                len(labels_on_wide),
                len(labels_on_wide) / len(labels_df) * 100 if len(labels_df) > 0 else 0,
            )

            write_wide_daily(wide, out, expanded_factor_names, label_name)
            write_labels_long(labels_on_wide, label_name, out)
            written_any = True

            splits_accum.append(wide[["trade_date", "stock_code"]].drop_duplicates())

            del feats_long, labels_df, wide, labels_on_wide
            gc.collect()
            continue

        if base_factor_source_map is None:
            base_factor_source_map = _resolve_feature_sources(
                features_tables,
                factor_windows,
                prov,
            )

        restricted_keys = (
            _load_restricted_set(prov, s, e, restricted_table) if restricted_table else set()
        )
        labels_df: Optional[pd.DataFrame] = None
        wide_acc: Optional[pd.DataFrame] = None

        for batch_pairs in factor_batches_defs:
            batch_factor_windows: Dict[str, List[int]] = {}
            for base, window in batch_pairs:
                bucket = batch_factor_windows.setdefault(base, [])
                bucket.append(window)
            for key in batch_factor_windows:
                batch_factor_windows[key] = sorted(set(batch_factor_windows[key]))
            batch_factor_names = [f"{base}_w{window}" for base, window in batch_pairs]
            batch_mapping: Dict[Tuple[str, int], Dict[str, str]] = {}
            if base_factor_source_map:
                for pair in batch_pairs:
                    info = base_factor_source_map.get(pair)
                    if info is not None:
                        batch_mapping[pair] = info

            feats_long_batch = build_long_preprocessed_with_zscore(
                prov,
                pad_start,
                e,
                batch_factor_windows,
                features_tables,
                factor_based_nan_handling=factor_based_nan_handling,
                consecutive_nan_threshold=consecutive_nan_threshold,
                stats_table=stats_table,
                clip_std=clip_std,
                feature_probe_days=0,
                factor_source_map=batch_mapping if batch_mapping else None,
            )
            if feats_long_batch.empty:
                continue

            try:
                feats_long_batch = feats_long_batch[feats_long_batch["trade_date"] >= s]
            except Exception:
                logger.debug("warmup trim fallback failed; leaving dataframe unchanged")

            feats_long_batch, _ = _apply_code_filters(feats_long_batch, "features")
            if feats_long_batch.empty:
                continue

            feats_long_batch = _filter_restricted_rows(
                feats_long_batch,
                restricted_keys,
                "Filtered %d rows in features via restricted set",
            )
            if feats_long_batch.empty:
                continue

            if labels_df is None:
                labels_candidate = _fetch_labels_chunk(
                    prov,
                    s,
                    e,
                    label_name,
                    labels_table,
                    restricted_keys,
                    label_shift=label_shift,
                )
                if labels_candidate.empty:
                    labels_df = pd.DataFrame()
                    break
                labels_candidate, _ = _apply_code_filters(labels_candidate, "labels")
                if labels_candidate.empty:
                    labels_df = pd.DataFrame()
                    break
                if winsorise_labels:
                    labels_candidate = winsorize_labels_by_date(
                        labels_candidate,
                        label_name,
                        label_winsor_q,
                    )
                labels_df = labels_candidate

            wide_batch = pivot_long_to_wide_simple(
                feats_long_batch,
                batch_factor_names,
                "factor_name",
                "factor_value",
                0,
            )
            if wide_batch.empty:
                continue

            if wide_acc is None:
                wide_acc = wide_batch
            else:
                wide_acc = wide_acc.merge(
                    wide_batch,
                    on=["trade_date", "stock_code"],
                    how="outer",
                )

            del feats_long_batch, wide_batch
            gc.collect()

        if (
            labels_df is None
            or labels_df.empty
            or wide_acc is None
            or wide_acc.empty
        ):
            labels_df = None
            wide_acc = None
            gc.collect()
            continue

        wide = _complete_date_reindex(wide_acc, s, e)
        wide = _filter_restricted_rows(
            wide,
            restricted_keys,
            "Removed %d rows in wide table via restricted set",
        )

        wide = wide.merge(labels_df, on=["trade_date", "stock_code"], how="left")

        missing_factor_cols = [col for col in expanded_factor_names if col not in wide.columns]
        for col in missing_factor_cols:
            wide[col] = np.nan

        if dropna_factor_value:
            factor_subset = [col for col in expanded_factor_names if col in wide.columns]
            before = len(wide)
            wide = wide.dropna(axis=0, how="all", subset=factor_subset)
            dropped = before - len(wide)
            if dropped:
                logger.info("Dropped %d all-null factor rows before writing", dropped)

        keys_final = wide.loc[:, ["trade_date", "stock_code"]].drop_duplicates()
        labels_on_wide = labels_df.merge(keys_final, on=["trade_date", "stock_code"], how="inner")
        logger.info(
            "Label sync: original %d, on_wide %d (%.1f%%)",
            len(labels_df),
            len(labels_on_wide),
            len(labels_on_wide) / len(labels_df) * 100 if len(labels_df) > 0 else 0,
        )

        write_wide_daily(wide, out, expanded_factor_names, label_name)
        write_labels_long(labels_on_wide, label_name, out)
        written_any = True

        splits_accum.append(wide[["trade_date", "stock_code"]].drop_duplicates())

        del labels_df, wide_acc, wide, labels_on_wide
        gc.collect()

    if splits_accum:
        keys_df = pd.concat(splits_accum, ignore_index=True).drop_duplicates()
    else:
        keys_df = pd.DataFrame(columns=["trade_date", "stock_code"])

    splits_df = None
    if split_rules and not keys_df.empty:
        splits_df = _apply_splits(keys_df.copy(), split_rules)
        if splits_df is not None:
            pq.write_table(
                pa.Table.from_pandas(splits_df),
                meta_dir / "splits.parquet",
                compression="zstd",
            )
            _generate_fixed_indices(splits_df, meta_dir)

    if not keys_df.empty:
        if splits_df is not None:
            full_indices = splits_df.copy()
        else:
            full_indices = keys_df.copy()
            full_indices["split"] = "unused"
        full_indices["trade_date"] = pd.to_datetime(full_indices["trade_date"]).dt.strftime("%Y%m%d")
        full_indices = full_indices.sort_values(["trade_date", "stock_code"]).reset_index(drop=True)
        full_indices["index_id"] = np.arange(len(full_indices))
        pq.write_table(
            pa.Table.from_pandas(full_indices),
            meta_dir / "full_indices.parquet",
            compression="zstd",
        )

    factor_names = sorted(factor_windows.keys())

    schema = {
        "sequence_mode": False,
        "dynamic_window": False,
        "feature_lag": feature_lag_hint,
        "warmup_days": warmup_days,
        "filters": {
            "dropna_factor_value": dropna_factor_value,
            "filter_features_restricted": filter_features_restricted,
            "exclude_code_prefixes": list(prefix_tuple),
            "exclude_codes_regex": exclude_codes_regex,
        },
        "label_col": label_name,
        "index_cols": ["trade_date", "stock_code"],
        "factor_names": factor_names,
        "expanded_factor_names": expanded_factor_names,
        "n_base_features": len(expanded_factor_names),
        "n_total_features": len(expanded_factor_names),
        "winsorise_labels": winsorise_labels,
        "label_winsor_q": list(label_winsor_q),
        "factor_based_nan_handling": factor_based_nan_handling,
        "consecutive_nan_threshold": consecutive_nan_threshold,
        "clip_std": clip_std,
        "factor_windows": factor_windows,
        "max_factors_per_batch": schema_max_factors_per_batch,
        "feature_sources": features_tables,
        "labels_table": labels_table,
        "restricted_table": restricted_table,
        "stats_table": stats_table,
        "build_start_date": start_date,
        "build_end_date": end_date,
        "tables": {
            "wide_daily": {"layout": "wide_daily", "path": "shards/wide_daily"},
            "labels": {"layout": "long", "path": "shards/labels"},
        },
        "written_any": written_any,
    }

    with open(meta_dir / "schema.json", "w", encoding="utf-8") as fp:
        json.dump(schema, fp, indent=2, ensure_ascii=False)

    logger.info("Wide-daily dataset ready at %s", out)


def build_pv_dataset_streaming(
    *args,
    **kwargs,
):
    raise NotImplementedError(
        "build_pv_dataset_streaming is deprecated; use build_pv_dataset_long_dynamic instead."
    )
def build_pv_dataset_long_dynamic(
    output_dir: str | Path,
    start_date: str,
    end_date: str,
    label_name: str,
    factor_windows: Dict[str, List[int]] | None = None,
    features_table: Union[str, Sequence[str]] = "ai_is.inter_train_factors_mkt_processed_v3",
    labels_table: str = DEFAULT_LABELS_TABLE,
    restricted_table: str = "ai_is.forbid_pool_comprehensive",
    split_rules: Sequence[Tuple[str, str, str]] | None = None,
    chunk_freq: str = "M",
    label_shift: int = 10,
    winsorise_labels: bool = True,
    label_winsor_q: Tuple[float, float] = (0.0005, 0.9995),
    stats_table: Optional[str] = None,
    clip_std: bool = True,
    factor_based_nan_handling: bool = True,
    consecutive_nan_threshold: Optional[int] = None,
    feature_lag: int = 300,
    warmup_days: int = 200,
    dropna_factor_value: bool = True,
    filter_features_restricted: bool = False,
    exclude_code_prefixes: Optional[List[str]] = None,
    exclude_codes_regex: Optional[str] = None,
) -> None:
    """Build long-format shards for dynamic DuckDB windowing."""
    out = Path(output_dir)
    meta_dir, shard_dir = _ensure_dirs(out)
    prov = LocalTestDBDataProvider()

    if factor_windows is None:
        factor_windows = FACTOR_WINDOWS.copy()

    features_tables = [features_table] if isinstance(features_table, str) else list(features_table)
    if not features_tables:
        raise ValueError("features_table must not be empty")

    ranges = list(_iter_ranges(start_date, end_date, chunk_freq))
    prefix_tuple: Tuple[str, ...] = tuple(exclude_code_prefixes or [])
    code_pattern = re.compile(exclude_codes_regex) if exclude_codes_regex else None

    def _apply_code_filters(df: pd.DataFrame, origin: str) -> Tuple[pd.DataFrame, int]:
        if df.empty or (not prefix_tuple and code_pattern is None):
            return df, 0
        working = df.copy()
        codes = working["stock_code"].astype(str)
        mask = pd.Series(True, index=working.index)
        if prefix_tuple:
            mask &= ~codes.str.startswith(prefix_tuple)
        if code_pattern is not None:
            mask &= ~codes.str.match(code_pattern)
        removed = int((~mask).sum())
        if removed:
            logger.info("Filtered %d rows in %s via stock code filters", removed, origin)
            working = working.loc[mask].reset_index(drop=True)
        return working, removed

    splits_accum: List[pd.DataFrame] = []
    written_any = False

    for s, e in tqdm(ranges, desc=f"long-dynamic shards -> {out}"):
        pad_start = s
        try:
            if warmup_days and int(warmup_days) > 0:
                pad_start = _get_trading_days_before(s, int(warmup_days))
        except Exception:
            pad_start = (pd.to_datetime(s) - pd.Timedelta(days=int(1.4 * int(warmup_days)))).strftime("%Y%m%d")

        logger.debug(
            "warmup window: %s -> %s (target %s -> %s)",
            pad_start,
            s,
            s,
            e,
        )

        feats_long = build_long_preprocessed_with_zscore(
            prov,
            pad_start,
            e,
            factor_windows,
            features_tables,
            factor_based_nan_handling=factor_based_nan_handling,
            consecutive_nan_threshold=consecutive_nan_threshold,
            stats_table=stats_table,
            clip_std=clip_std,
        )
        if feats_long.empty:
            continue

        try:
            feats_long = feats_long[feats_long["trade_date"] >= s]
        except Exception:
            logger.debug("warmup trim fallback failed; leaving dataframe unchanged")

        feats_long, _ = _apply_code_filters(feats_long, "features")
        if feats_long.empty:
            logger.info("Skip %s-%s: no feature rows after code filters", s, e)
            continue

        restricted_keys = (
            _load_restricted_set(prov, s, e, restricted_table)
            if restricted_table else set()
        )
        labels_df = _fetch_labels_chunk(
            prov,
            s,
            e,
            label_name,
            labels_table,
            restricted_keys,
            label_shift=label_shift,
        )
        if labels_df.empty:
            continue

        labels_df, _ = _apply_code_filters(labels_df, "labels")
        if labels_df.empty:
            logger.info("Skip %s-%s: no label rows after code filters", s, e)
            continue

        if filter_features_restricted and restricted_keys:
            rk_df = pd.DataFrame(list(restricted_keys), columns=["trade_date", "stock_code"])
            if not rk_df.empty:
                rk_df["__rk__"] = 1
                feats_long = feats_long.merge(rk_df, on=["trade_date", "stock_code"], how="left")
                removed_restricted = int(feats_long["__rk__"].notna().sum())
                if removed_restricted:
                    logger.info("Filtered %d rows in features via restricted set", removed_restricted)
                feats_long = feats_long[feats_long["__rk__"].isna()].drop(columns="__rk__").reset_index(drop=True)
                if feats_long.empty:
                    logger.info("Skip %s-%s: no feature rows after restricted filter", s, e)
                    continue

        if winsorise_labels:
            labels_df = winsorize_labels_by_date(labels_df, label_name, label_winsor_q)

        # A1: 写入同步 - 只写入feats_long中实际存在的labels（强一致性）
        keys_final = feats_long.loc[:, ["trade_date", "stock_code"]].drop_duplicates()
        labels_on_feats = labels_df.merge(keys_final, on=["trade_date", "stock_code"], how="inner")
        logger.info("Label sync: original %d, on_feats %d (%.1f%%)", 
                    len(labels_df), len(labels_on_feats), 
                    len(labels_on_feats)/len(labels_df)*100 if len(labels_df) > 0 else 0)
        
        write_features_long(feats_long, out, dropna_factor_value=dropna_factor_value)
        write_labels_long(labels_on_feats, label_name, out)
        written_any = True

        if split_rules:
            splits_accum.append(labels_df[["trade_date", "stock_code"]].copy())

        del feats_long, labels_df
        gc.collect()

    if split_rules and splits_accum:
        keys_df = pd.concat(splits_accum, ignore_index=True).drop_duplicates()
        splits_df = _apply_splits(keys_df, split_rules)
        if splits_df is not None:
            pq.write_table(
                pa.Table.from_pandas(splits_df),
                meta_dir / "splits.parquet",
                compression="zstd",
            )
            _generate_fixed_indices(splits_df, meta_dir)

    full_indices_path = meta_dir / "full_indices.parquet"
    if not full_indices_path.exists():
        labels_path = shard_dir / "labels"
        if labels_path.exists():
            try:
                labels_ds = ds.dataset(labels_path.as_posix(), format="parquet", partitioning="hive")
                tbl = labels_ds.to_table(columns=["trade_date", "stock_code"])
            except Exception as exc:
                logger.warning("Unable to collect full indices from labels shards: %s", exc)
            else:
                if tbl.num_rows:
                    idx_df = tbl.to_pandas().drop_duplicates()
                    idx_df = idx_df.sort_values(["trade_date", "stock_code"]).reset_index(drop=True)
                    idx_df["split"] = "unused"
                    pq.write_table(
                        pa.Table.from_pandas(idx_df),
                        full_indices_path,
                        compression="zstd",
                    )

    factor_names = list(factor_windows.keys())
    expanded = []
    for name, wins in factor_windows.items():
        for win in wins:
            expanded.append(f"{name}_w{int(win)}")
    expanded = list(dict.fromkeys(expanded))

    schema = {
        "dynamic_window": True,
        "feature_lag": feature_lag,
        "warmup_days": warmup_days,
        "filters": {
            "dropna_factor_value": dropna_factor_value,
            "filter_features_restricted": filter_features_restricted,
            "exclude_code_prefixes": list(prefix_tuple),
            "exclude_codes_regex": exclude_codes_regex,
        },
        "label_col": label_name,
        "index_cols": ["trade_date", "stock_code"],
        "mask_cols": [],
        "factor_names": factor_names,
        "expanded_factor_names": expanded,
        "n_base_features": len(expanded),
        "n_total_features": len(expanded),
        "winsorise_labels": winsorise_labels,
        "label_winsor_q": list(label_winsor_q),
        "clip_std": clip_std,
        "factor_based_nan_handling": factor_based_nan_handling,
        "consecutive_nan_threshold": consecutive_nan_threshold,
        "feature_lag_hint": feature_lag,
        "factor_windows": factor_windows,
        "feature_sources": features_tables,
        "labels_table": labels_table,
        "restricted_table": restricted_table,
        "stats_table": stats_table,
        "build_start_date": start_date,
        "build_end_date": end_date,
        "tables": {
            "features_long": {"layout": "long", "path": "shards/features_long"},
            "labels": {"layout": "long", "path": "shards/labels"},
        },
        "written_any": written_any,
    }

    with open(meta_dir / "schema.json", "w", encoding="utf-8") as fp:
        json.dump(schema, fp, indent=2, ensure_ascii=False)

    logger.info("Long-format dataset ready at %s", out)


