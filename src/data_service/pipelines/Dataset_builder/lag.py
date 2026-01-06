# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from typing import List
import pandas as pd


def generate_lag_features_simple(
    df: pd.DataFrame, 
    factor_cols: List[str], 
    lag: int = 30
) -> pd.DataFrame:
    logger.info(f"Starting simplified lag feature generation, lag={lag}, factors={len(factor_cols)}")
    if 'stock_code' not in df.columns or 'trade_date' not in df.columns:
        logger.error("DataFrame must contain stock_code and trade_date columns")
        return df
    df = df.sort_values(['stock_code', 'trade_date']).reset_index(drop=True)
    factors_present = [f for f in factor_cols if f in df.columns]
    df = df.set_index(['stock_code', 'trade_date'])
    out_dfs = []
    non_factor_cols = [col for col in df.columns if col not in factors_present]
    if non_factor_cols:
        base_df = df[non_factor_cols].copy()
        out_dfs.append(base_df)
    for factor in factors_present:
        factor_series = df[factor]
        factor_lag_dfs = []
        for i in range(lag-1, -1, -1):
            if i == 0:
                lag_df = factor_series.to_frame(f"{factor}_lag_{i}")
            else:
                shifted = (
                    factor_series.groupby('stock_code', sort=False)
                    .shift(i)
                    .to_frame(f"{factor}_lag_{i}")
                )
                lag_df = shifted
            factor_lag_dfs.append(lag_df)
        factor_all_lags = pd.concat(factor_lag_dfs, axis=1)
        out_dfs.append(factor_all_lags)
    if out_dfs:
        result_df = pd.concat(out_dfs, axis=1).reset_index()
    else:
        result_df = df.reset_index()
    return result_df


