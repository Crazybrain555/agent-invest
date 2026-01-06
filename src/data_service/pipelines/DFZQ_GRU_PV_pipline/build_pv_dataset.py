# ──────────────────────────────────────────────────────────────
# File: src/data_service/pipelines/build_pv_dataset.py
# Author: AIQuant Lab Assistant
# Created: 2025-04-29
# Purpose: Build an offline, sharded Price-Volume dataset (pv_v1)
# ──────────────────────────────────────────────────────────────

from __future__ import annotations

import os
from pathlib import Path
import json
import logging
from typing import Tuple, List, Optional, Dict, Sequence
import numpy as np

# import duckdb # Not used in builder, only loader
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.dataset as ds
import pandas as pd

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider

# Configure module-level logger
logger = logging.getLogger(__name__)

# =============================================================
#                          Helpers
# =============================================================

def _ensure_dirs(output_dir: Path) -> Tuple[Path, Path, Path]:
    """Create {output_dir}/meta and /shards directories if they don't exist."""
    meta_dir = output_dir / "meta"
    shard_dir = output_dir / "shards"
    meta_dir.mkdir(parents=True, exist_ok=True)
    shard_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Ensured directories exist: {meta_dir}, {shard_dir}")
    return meta_dir, shard_dir, output_dir / "stats.parquet"


def _load_raw_data(
    prov: LocalTestDBDataProvider,
    start: str,
    end: str,
    lag: int,
    label_name: str,
    # TODO: Confirm table names are accurate
    x_table: str = "ai_is.intermediate_training_factors_market_normalize_lag30_countday1",
    y_table: str = "ai_is.training_label_ls10_adj_topcor_cr30_cw240"
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch X (wide) & y (long) DataFrames."""
    logger.info(f"Fetching X data from {x_table} ({start}-{end})...")
    # ------------- X -------------
    base_cols = [
        "adj_open", "adj_high", "adj_low",
        "adj_close", "vwap", "amount", "turnover_rate"
    ]
    
    x_fields = [f"{c}_lag_{i}" for c in base_cols for i in range(lag)]
    x_df = prov.fetch_data(
        table=x_table,
        start_date=start, end_date=end,
        fields=x_fields, format="wide" # Assuming format='wide' returns df with ['trade_date', 'stock_code'] as index
    )
    # Always reset_index since we expect trade_date/stock_code columns
    x_df = x_df.reset_index()
    logger.info(f"Fetched {len(x_df)} rows for X.")

    # ------------- y -------------
    logger.info(f"Fetching y data from {y_table} ({start}-{end}, label={label_name})...")
    y_long = prov.fetch_data(
        table=y_table,
        start_date=start, end_date=end,
        fields=[label_name], format="long" # Assuming format='long' returns df with ['trade_date', 'stock_code', 'field_name', 'value']
    )
    y_long = y_long[y_long["field_name"] == label_name]
    logger.info(f"Filtered {len(y_long)} label rows for '{label_name}'.")

    if y_long.empty:
        logger.error(f"No label data found for '{label_name}' in the specified date range.")
        raise ValueError(f"No label data available for {label_name}")

    # Pivot y data from long to wide format matching X's index columns
    y_df = (
        y_long.pivot_table(index=["trade_date", "stock_code"],
                           columns="field_name", values="value")
        .reset_index()
    )
    logger.info(f"Pivoted {len(y_df)} rows for y.")

    return x_df, y_df


def _zscore_clip(
    df: pd.DataFrame,
    prov: LocalTestDBDataProvider,
    clip: bool = True,
    # TODO: Confirm stats table name
    stats_table: str = "ai_is.inter_train_factors_std_l30_d1_2002_2012"
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Apply second-stage Z-score (with optional clipping)."""
    logger.info(f"Fetching standardization stats from {stats_table}...")
    # Fetch only needed stats once
    stats = prov.fetch_data(table=stats_table)
    if 'feature_name' not in stats.columns:
        raise ValueError(f"Stats table {stats_table} missing 'feature_name' column.")
    stats = stats.set_index("feature_name")
    logger.info(f"Loaded {len(stats)} stats entries.")

    feat_cols: List[str] = sorted([c for c in df.columns if "_lag_" in c]) # Sort for consistency
    valid_stats_cols = [] # Keep track of columns actually standardized

    # Check which feature columns from df exist in the stats table
    stats_available_for = stats.index.intersection(feat_cols)
    stats_missing_for = list(set(feat_cols) - set(stats_available_for))

    if stats_missing_for:
         logger.warning(f"Missing std stats for {len(stats_missing_for)} columns: {stats_missing_for[:5]}...") # Log first 5 missing

    logger.info(f"Applying Z-score{' and clipping' if clip else ''} to {len(stats_available_for)} columns...")
    skipped_count = 0
    for col in stats_available_for:
        # Check if stats are valid (not NaN/Infinite)
        try:
            mu, sigma, lo, hi = stats.loc[col, ["mean", "std", "lower", "upper"]]
            if not np.isfinite([mu, sigma, lo, hi]).all() or sigma == 0:
                 logger.warning(f"Invalid stats for {col} (mu={mu}, sigma={sigma}, lo={lo}, hi={hi}). Skipping standardization.")
                 skipped_count += 1
                 continue

            # Apply standardization
            z = (df[col].astype(float) - mu) / (sigma + 1e-12) # Ensure float type for division

            # Apply clipping if enabled
            if clip:
                # Calculate clip bounds in standardized space
                lower_bound = (lo - mu) / (sigma + 1e-12)
                upper_bound = (hi - mu) / (sigma + 1e-12)
                z = z.clip(lower_bound, upper_bound)

            df[col] = z.astype("float32") # Store as float32 to save memory
            valid_stats_cols.append(col)
        except KeyError:
             logger.warning(f"KeyError accessing stats for {col}. Should not happen if check above works.")
             skipped_count += 1
        except Exception as e:
             logger.error(f"Error standardizing column {col}: {e}")
             skipped_count += 1

    if skipped_count > 0:
        logger.warning(f"Skipped standardization for {skipped_count} columns due to invalid stats or errors.")

    # Prepare the DataFrame of used statistics
    if not valid_stats_cols:
         logger.warning("No columns were standardized. Returning empty stats DataFrame.")
         used_stats_df = pd.DataFrame(columns=['feature_name', 'mean', 'std', 'lower', 'upper'])
    else:
         used_stats_df = stats.loc[valid_stats_cols].reset_index()
         # Select and rename columns for clarity if needed, ensure correct order
         used_stats_df = used_stats_df[['feature_name', 'mean', 'std', 'lower', 'upper']]

    logger.info(f"Standardization complete. Used stats for {len(used_stats_df)} features.")
    return df, used_stats_df


def _filter_restricted(
    df: pd.DataFrame,
    prov: LocalTestDBDataProvider,
    # 默认使用综合禁投池表
    restricted_table: str = "ai_is.forbid_pool_comprehensive"
) -> pd.DataFrame:
    """Remove restricted stocks based on the signal=1 flag."""
    min_date = df['trade_date'].min()
    max_date = df['trade_date'].max()
    logger.info(f"Fetching restricted stock pool from {restricted_table} ({min_date}-{max_date})...")
    try:
        rest = prov.fetch_data(
            table=restricted_table,
            start_date=min_date,
            end_date=max_date,
            fields=["trade_date", "stock_code", "signal"]
        )
    except Exception as e:
        logger.error(f"Failed to fetch restricted stock data: {e}. Proceeding without filtering.")
        return df

    rest = rest[rest["signal"] == 1][["trade_date", "stock_code"]]
    logger.info(f"Found {len(rest)} restricted stock instances.")

    if rest.empty:
        logger.info("No restricted stocks to filter in the given period.")
        return df

    initial_rows = len(df)
    # Use a left merge with indicator to find rows in df that have a match in rest
    df = df.merge(rest, on=["trade_date", "stock_code"], how="left", indicator=True)
    # Keep only rows that were only in the left DataFrame (df)
    df_filtered = df[df["_merge"] == "left_only"].drop(columns="_merge")
    filtered_rows = initial_rows - len(df_filtered)
    logger.info(f"Filtered out {filtered_rows} rows corresponding to restricted stocks.")
    
    # Sort by date and stock code for consistency
    return df_filtered.sort_values(["trade_date", "stock_code"])


def _apply_splits(df: pd.DataFrame,
                  split_rules: Sequence[Tuple[str, str, str]] | None
                 ) -> pd.DataFrame | None:
    """
    Parameters
    ----------
    split_rules : list of (split_name, start_date, end_date) tuples
        *end_date* inclusive.  Pass None / empty list to skip creating splits.

    Returns  DataFrame[trade_date, stock_code, split]  or  None
    """
    if not split_rules:
        logger.info("No split rules supplied – skip writing splits.parquet.")
        return None

    date_ser = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    split_col = pd.Series("unused", index=df.index, dtype="object")

    for name, s, e in split_rules:
        mask = (date_ser >= pd.Timestamp(s)) & (date_ser <= pd.Timestamp(e))
        split_col[mask] = name
        logger.info("  • %-8s  %s – %s  →  %d rows",
                    name, s, e, mask.sum())

    out = df[["trade_date", "stock_code"]].copy()
    out["split"] = split_col
    return out


# =============================================================
#               Main Builder API - importable function
# =============================================================

def build_pv_dataset(
    output_dir: str | Path = "data/Dataset/pv_v1",
    start_date: str = "20030101",
    end_date: str = "20131231",
    lag: int = 30,
    label_name: str = "tc_t10_n30_adj",
    clip_std: bool = True,
    split_rules: Sequence[Tuple[str,str,str]] | None = None,
) -> None:
    """
    Offline builder: fetch raw -> preprocess -> shard to Parquet.

    Creates the dataset structure under output_dir.

    Parameters
    ----------
    output_dir : str | Path
        Root directory for the dataset (e.g., data/Dataset/pv_v1).
    start_date : str
        Start date for fetching data (YYYYMMDD).
    end_date : str
        End date for fetching data (YYYYMMDD).
    lag : int
        Feature look-back length (e.g., 30 for *_lag_0 to *_lag_29).
    label_name : str
        The specific label field name to use from the label table.
    clip_std : bool
        Whether to apply clipping during Z-score standardization.
    split_rules : list of (split_name, start_date, end_date) tuples
        Custom date splits. Pass None to skip creating splits.parquet.
    """
    output_dir = Path(output_dir)
    logger.info(f"Starting dataset build process for {output_dir} ({start_date} - {end_date})")
    meta_dir, shard_dir, stats_path = _ensure_dirs(output_dir)

    try:
        prov = LocalTestDBDataProvider() # TODO: Consider passing db config if needed
    except Exception as e:
        logger.error(f"Failed to initialize LocalTestDBDataProvider: {e}")
        raise

    # --- Step 1: Load Data ---
    logger.info("[Step 1/6] Fetching raw data...")
    try:
        x_df, y_df = _load_raw_data(prov, start_date, end_date, lag, label_name)
        if x_df.empty:
             logger.error("Fetched X data is empty. Aborting build.")
             return
        # Merge requires 'trade_date', 'stock_code' in both
        required_cols = ["trade_date", "stock_code"]
        if not all(c in x_df.columns for c in required_cols) or not all(c in y_df.columns for c in required_cols):
             logger.error(f"Missing required merge columns {required_cols} in X or Y DataFrames.")
             logger.error(f"X columns: {x_df.columns.tolist()}")
             logger.error(f"Y columns: {y_df.columns.tolist()}")
             return # Abort
        df = pd.merge(x_df, y_df, on=["trade_date", "stock_code"], how="inner")
        logger.info(f"Merged X and Y. Shape before filtering: {df.shape}")

    except Exception as e:
        logger.error(f"Failed during data loading or initial merge: {e}", exc_info=True)
        return # Abort

    # --- Step 2: Filter & Standardize ---
    logger.info("[Step 2/6] Filtering restricted stocks and NaNs...")
    df = _filter_restricted(df, prov)
    initial_rows = len(df)
    df = df.dropna() # Drop rows with any NaNs after merge and filtering
    nan_filtered_rows = initial_rows - len(df)
    logger.info(f"Dropped {nan_filtered_rows} rows containing NaNs. Shape before std: {df.shape}")

    if df.empty:
        logger.error("DataFrame is empty after filtering restricted stocks and NaNs. Aborting.")
        return

    logger.info("[Step 2/6] Applying standardization...")
    try:
        df, used_stats = _zscore_clip(df, prov, clip=clip_std)
        if used_stats.empty:
            logger.warning("Standardization did not use any stats. Check stats table and feature names.")
        # Save standardization parameters
        logger.info(f"Writing standardization stats to {stats_path}")
        pq.write_table(pa.Table.from_pandas(used_stats), stats_path, compression='zstd')
    except Exception as e:
        logger.error(f"Failed during standardization or saving stats: {e}", exc_info=True)
        return # Abort

    if df.empty:
        logger.error("DataFrame is empty after standardization. Aborting.")
        return

    # --- Step 3: Build Split Index (Optional) ---
    logger.info("[Step 3/6] Building split index...")
    try:
        splits = _apply_splits(df, split_rules)
        if splits is not None:
            split_path = meta_dir / "splits.parquet"
            pq.write_table(pa.Table.from_pandas(splits), split_path, compression='zstd')
            logger.info(f"Split index saved to {split_path}")
        else:
            logger.info("Skipping split index creation as requested.")
    except Exception as e:
        logger.error(f"Failed during split index generation or saving: {e}", exc_info=True)
        return # Abort

    # --- Step 4: Save Schema ---
    logger.info("[Step 4/6] Saving schema metadata...")
    try:
        # Dynamically determine feature columns and number of base features
        feature_cols = sorted([c for c in df.columns if "_lag_" in c])
        # Infer n_features by counting unique prefixes before '_lag_'
        base_feature_names = set(col.split('_lag_')[0] for col in feature_cols)
        n_base_features = len(base_feature_names)

        schema_dict = {
            "feature_cols": feature_cols, # List all feature columns
            "label_col": label_name,
            "index_cols": ["trade_date", "stock_code"],
            "feature_lag": lag,
            "n_base_features": n_base_features, # e.g., 7 for adj_open..turnover_rate
            "n_total_features": len(feature_cols), # e.g., 7 * 30 = 210
            "clip_std": clip_std,
            "build_start_date": start_date,
            "build_end_date": end_date,
        }
        schema_path = meta_dir / "schema.json"
        with open(schema_path, "w", encoding="utf-8") as fp:
            json.dump(schema_dict, fp, indent=2, ensure_ascii=False)
        logger.info(f"Schema saved to {schema_path}")
    except Exception as e:
        logger.error(f"Failed during schema generation or saving: {e}", exc_info=True)
        # Continue build even if schema fails, but log error

    # --- Step 5: Write Sharded Data ---
    logger.info("[Step 5/6] Writing data shards...")
    try:
        # Add year and month for partitioning
        df["year"] = df["trade_date"].str.slice(0, 4) # Keep as string for pyarrow dataset partitioning
        df["month"] = df["trade_date"].str.slice(4, 6) # Keep as string

        # Define the schema for the PyArrow table explicitly for better control
        # This ensures consistent types across shards
        field_types = {col: pa.float32() for col in feature_cols}
        field_types[label_name] = pa.float32() # Assuming label is float
        field_types["trade_date"] = pa.string()
        field_types["stock_code"] = pa.string()
        field_types["year"] = pa.string()
        field_types["month"] = pa.string()
        arrow_schema = pa.schema([pa.field(name, dtype) for name, dtype in field_types.items() if name in df.columns])

        # Use pyarrow.dataset.write_dataset for efficient sharding with hive partitioning
        ds.write_dataset(
            pa.Table.from_pandas(df, schema=arrow_schema, preserve_index=False),
            base_dir=shard_dir,
            partitioning=ds.partitioning(pa.schema([('year', pa.string()), ('month', pa.string())]), flavor='hive'),
            format="parquet",
            compression="zstd",
            existing_data_behavior='overwrite_or_ignore' # Be careful with this in production
        )
        logger.info(f"Finished writing shards to {shard_dir}")

    except Exception as e:
        logger.error(f"Failed during sharded data writing: {e}", exc_info=True)
        return # Abort if sharding fails

    # --- Step 6: Completion ---
    logger.info(f"[Step 6/6] Dataset build completed successfully! Output: {output_dir}")


# =============================================================
#                       CLI thin wrapper
# =============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Build Price-Volume dataset shards (pv_v1).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter) # Show defaults
    parser.add_argument("--out", default="data/Dataset/pv_v1",
                        help="Output root directory for the dataset.")
    parser.add_argument("--start", default="20030101",
                        help="Start date (YYYYMMDD).")
    parser.add_argument("--end",   default="20131231",
                        help="End date (YYYYMMDD).")
    parser.add_argument("--lag",   default=30, type=int,
                        help="Feature look-back length.")
    parser.add_argument("--label", default="tc_t10_n30_adj",
                        help="Label column name to use.")
    parser.add_argument("--no-clip", action="store_true",
                        help="Disable lower/upper clipping during standardization.")
    parser.add_argument(
        "--splits",
        nargs="+",
        metavar=("NAME:START:END"),
        help="Custom date splits, e.g.  train:20030101:20171231  valid:20180101:20191231",
    )
    # TODO: Add arguments for table names if they need to be configurable

    args = parser.parse_args()

    # Setup logging for CLI execution
    log_file = Path(args.out) / "build_log.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Add file handler to module logger instead of root logger
    file_handler = logging.FileHandler(log_file, mode='w')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)
    
    # Add stream handler for console output
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    logger.addHandler(stream_handler)
    
    # Set logging level
    logger.setLevel(logging.INFO)

    # Parse split rules
    def _parse_splits(args_list):
        rules = []
        for item in args_list or []:
            try:
                name, s, e = item.split(":")
                rules.append((name, s, e))
            except ValueError:
                raise ValueError(f"Bad --splits item '{item}', need NAME:START:END")
        return rules or None

    split_rules = _parse_splits(args.splits)

    build_pv_dataset(
        output_dir=args.out,
        start_date=args.start,
        end_date=args.end,
        lag=args.lag,
        label_name=args.label,
        clip_std=not args.no_clip,
        split_rules=split_rules,
    )
