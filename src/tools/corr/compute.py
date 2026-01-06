from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd


def normalize_long_df(df: pd.DataFrame) -> pd.DataFrame:
    if "field_name" not in df.columns and "factor_name" in df.columns:
        df = df.rename(columns={"factor_name": "field_name"})
    if "value" not in df.columns and "factor_value" in df.columns:
        df = df.rename(columns={"factor_value": "value"})
    return df


def build_factor_matrix(df_long: pd.DataFrame, factor_id_col: str = "factor_id") -> pd.DataFrame:
    if df_long.empty:
        return df_long
    pivot = df_long.pivot_table(
        index=["trade_date", "stock_code"],
        columns=factor_id_col,
        values="value",
        aggfunc="first",
        observed=True,
    ).reset_index()
    pivot.columns.name = None
    return pivot


def _filter_constant_columns(group: pd.DataFrame, factor_cols: List[str], min_periods: int) -> List[str]:
    """Filter out constant columns that would cause correlation warnings."""
    valid_cols = []
    for col in factor_cols:
        valid_data = group[col].dropna()
        if len(valid_data) >= min_periods and valid_data.nunique() > 1:
            valid_cols.append(col)
    return valid_cols


def compute_corr_stats(
    df_wide: pd.DataFrame,
    method: str,
    corr_type: str,
    min_periods: int,
) -> Tuple[pd.DataFrame, Dict[str, float]]:
    factor_cols = [c for c in df_wide.columns if c not in ("trade_date", "stock_code")]
    if len(factor_cols) < 2:
        raise ValueError("Need at least 2 factors to compute correlation")

    if method == "cross_sectional":
        group_key = "trade_date"
        group_unit = "trade_date"
        group_counts = df_wide.groupby("trade_date")["stock_code"].nunique()
    elif method == "time_series":
        group_key = "stock_code"
        group_unit = "stock_code"
        group_counts = df_wide.groupby("stock_code")["trade_date"].nunique()
    else:
        raise ValueError("method must be 'cross_sectional' or 'time_series'")

    # Compute correlation per group, filtering out constant columns
    corr_list = []
    for _, group in df_wide.groupby(group_key):
        valid_cols = _filter_constant_columns(group, factor_cols, min_periods)
        if len(valid_cols) >= 2:
            corr_matrix = group[valid_cols].corr(method=corr_type, min_periods=min_periods)
            corr_list.append(corr_matrix)
    
    if not corr_list:
        # Return empty stats if no valid groups
        empty_stats = pd.DataFrame(
            columns=["corr_mean", "corr_median", "corr_std", "n_groups"]
        )
        empty_stats.index = pd.MultiIndex.from_tuples([], names=[None, None])
        summary = {
            "n_groups": 0,
            "group_unit": group_unit,
            "avg_group_size": 0.0,
            "n_dates": 0,
            "avg_n_stocks": 0.0,
        }
        return empty_stats, summary

    # Stack all correlation matrices
    corr_slices = pd.concat(corr_list, keys=range(len(corr_list)))

    stacked = corr_slices.stack()
    stats = (
        stacked.groupby(level=[1, 2])
        .agg(["mean", "median", "std", "count"])
        .rename(
            columns={
                "mean": "corr_mean",
                "median": "corr_median",
                "std": "corr_std",
                "count": "n_groups",
            }
        )
    )

    n_groups = int(group_counts.shape[0])
    avg_group_size = float(group_counts.mean()) if not group_counts.empty else 0.0
    summary = {
        "n_groups": n_groups,
        "group_unit": group_unit,
        "avg_group_size": avg_group_size,
        "n_dates": n_groups,
        "avg_n_stocks": avg_group_size,
    }
    return stats, summary


def build_corr_matrix(stats: pd.DataFrame) -> pd.DataFrame:
    return stats["corr_mean"].unstack()


def build_high_corr_pairs(corr_matrix: pd.DataFrame, threshold: float) -> pd.DataFrame:
    if corr_matrix.empty:
        return corr_matrix
    mask = np.triu(np.ones_like(corr_matrix, dtype=bool), k=1)
    stacked = corr_matrix.where(mask).stack().reset_index()
    stacked.columns = ["factor_a", "factor_b", "corr_mean"]
    stacked["abs_corr"] = stacked["corr_mean"].abs()
    result = stacked[stacked["abs_corr"] >= threshold]
    return result.sort_values("abs_corr", ascending=False).reset_index(drop=True)


def build_corr_table(
    stats: pd.DataFrame,
    pairs: List[Tuple[str, str]],
    summary: Dict[str, float],
) -> pd.DataFrame:
    rows = []
    for factor_a, factor_b in pairs:
        if (factor_a, factor_b) in stats.index:
            row = stats.loc[(factor_a, factor_b)].to_dict()
        elif (factor_b, factor_a) in stats.index:
            row = stats.loc[(factor_b, factor_a)].to_dict()
        else:
            row = {
                "corr_mean": np.nan,
                "corr_median": np.nan,
                "corr_std": np.nan,
                "n_groups": 0,
            }
        row.update({"factor_a": factor_a, "factor_b": factor_b})
        row.setdefault("n_groups", summary.get("n_groups", 0))
        row["group_unit"] = summary.get("group_unit")
        row["avg_group_size"] = summary.get("avg_group_size")
        rows.append(row)
    return pd.DataFrame(rows)


def _has_variance(series: pd.Series, min_periods: int = 2) -> bool:
    """Check if series has variance (not constant)."""
    valid = series.dropna()
    if len(valid) < min_periods:
        return False
    return valid.nunique() > 1


def compute_corr_one_to_many(
    df_wide: pd.DataFrame,
    target_col: str,
    candidate_cols: List[str],
    method: str,
    corr_type: str,
    min_periods: int,
) -> Tuple[pd.DataFrame, Dict[str, float]]:
    if target_col not in df_wide.columns:
        raise ValueError(f"Target column missing: {target_col}")
    if not candidate_cols:
        raise ValueError("No candidate columns provided for one-to-many correlation.")

    if method == "cross_sectional":
        group_key = "trade_date"
        group_counts = df_wide.groupby("trade_date")["stock_code"].nunique()
        group_unit = "trade_date"
    elif method == "time_series":
        group_key = "stock_code"
        group_counts = df_wide.groupby("stock_code")["trade_date"].nunique()
        group_unit = "stock_code"
    else:
        raise ValueError("method must be 'cross_sectional' or 'time_series'")

    per_group: List[pd.Series] = []
    for _, group in df_wide.groupby(group_key):
        target = group[target_col]
        candidates = group[candidate_cols]
        valid = candidates.notna().to_numpy() & target.notna().to_numpy()[:, None]
        counts = pd.Series(valid.sum(axis=0), index=candidate_cols)
        
        # Initialize correlations with NaN
        corrs = pd.Series(np.nan, index=candidate_cols)
        
        # Skip if target is constant (no variance)
        if not _has_variance(target, min_periods):
            per_group.append(corrs)
            continue
        
        # Find candidates with variance
        cols_with_variance = [c for c in candidate_cols if _has_variance(candidates[c], min_periods)]
        
        if cols_with_variance:
            # Only compute correlation for non-constant columns
            valid_corrs = candidates[cols_with_variance].corrwith(target, method=corr_type)
            corrs.update(valid_corrs)
        
        corrs[counts < min_periods] = np.nan
        per_group.append(corrs)

    if not per_group:
        return pd.DataFrame(), {
            "n_groups": 0,
            "group_unit": group_unit,
            "avg_group_size": 0.0,
            "n_dates": 0,
            "avg_n_stocks": 0.0,
        }

    stacked = pd.DataFrame(per_group)
    stats = (
        stacked.agg(["mean", "median", "std", "count"])
        .T.rename(
            columns={
                "mean": "corr_mean",
                "median": "corr_median",
                "std": "corr_std",
                "count": "n_groups",
            }
        )
    )

    n_groups = int(group_counts.shape[0])
    avg_group_size = float(group_counts.mean()) if not group_counts.empty else 0.0
    summary = {
        "n_groups": n_groups,
        "group_unit": group_unit,
        "avg_group_size": avg_group_size,
        "n_dates": n_groups,
        "avg_n_stocks": avg_group_size,
    }
    return stats, summary


def attach_factor_meta(
    df: pd.DataFrame,
    factor_meta: Dict[str, Dict[str, str]],
    prefix_a: str = "source_table_a",
    prefix_b: str = "source_table_b",
) -> pd.DataFrame:
    if df.empty:
        return df
    updated = df.copy()
    updated[prefix_a] = updated["factor_a"].map(
        lambda key: factor_meta.get(key, {}).get("source_table")
    )
    updated[prefix_b] = updated["factor_b"].map(
        lambda key: factor_meta.get(key, {}).get("source_table")
    )
    return updated
