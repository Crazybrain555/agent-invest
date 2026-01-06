from __future__ import annotations

import pandas as pd

from src.tools.corr.naming import short_factor_name


def build_excel_corr_table(corr_table: pd.DataFrame) -> pd.DataFrame:
    if corr_table.empty:
        return corr_table
    excel_df = corr_table.copy()
    excel_df["factor_a"] = excel_df["factor_a"].map(short_factor_name)
    excel_df["factor_b"] = excel_df["factor_b"].map(short_factor_name)
    drop_cols = [c for c in excel_df.columns if c.startswith("source_table_")]
    if drop_cols:
        excel_df = excel_df.drop(columns=drop_cols)
    preferred = [
        "factor_a",
        "factor_b",
        "corr_mean",
        "corr_median",
        "corr_std",
        "n_groups",
        "group_unit",
        "avg_group_size",
        "n_dates",
        "avg_n_stocks",
    ]
    ordered = [c for c in preferred if c in excel_df.columns] + [
        c for c in excel_df.columns if c not in preferred
    ]
    return excel_df[ordered]


def build_excel_corr_matrix(corr_matrix: pd.DataFrame) -> pd.DataFrame:
    if corr_matrix.empty:
        return corr_matrix
    excel_df = corr_matrix.copy()
    excel_df.columns = [short_factor_name(c) for c in excel_df.columns]
    excel_df.index = [short_factor_name(i) for i in excel_df.index]
    return excel_df.reset_index().rename(columns={"index": "factor"})
