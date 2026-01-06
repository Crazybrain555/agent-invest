# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from typing import List
import pandas as pd


def pivot_long_to_wide_simple(
    df: pd.DataFrame,
    factor_names: List[str],
    factor_name_col: str,
    value_col: str,
    lag_filter: int = 0
) -> pd.DataFrame:
    logger.info(f"Starting simplified long-to-wide conversion, data shape: {df.shape}")
    if 'lag' in df.columns:
        df_filtered = df[df['lag'] == lag_filter].copy()
        logger.info(f"After lag filter ({lag_filter}): {df_filtered.shape}")
    else:
        df_filtered = df.copy()
    if df_filtered.empty:
        return pd.DataFrame()
    value_df = df_filtered[df_filtered[factor_name_col].isin(factor_names)].copy()
    if value_df.empty:
        return pd.DataFrame()
    value_df = value_df.drop_duplicates(
        subset=['trade_date', 'stock_code', factor_name_col], 
        keep='first'
    )
    wide = value_df.pivot_table(
        index=['trade_date', 'stock_code'],
        columns=factor_name_col,
        values=value_col,
        aggfunc='first'
    ).reset_index()
    wide.columns.name = None
    logger.info(f"Pivot completed, wide table shape: {wide.shape}")
    return wide


def _complete_date_reindex(df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    if df.empty:
        return df
    try:
        from src.utils.db_connection import db_config
        from sqlalchemy import text
        sql = text("""
        SELECT TRADE_DAYS
        FROM wind_quant.dbo.AShareCalendar
        WHERE S_INFO_EXCHMARKET='SSE'
        AND TRADE_DAYS >= :start_date
        AND TRADE_DAYS <= :end_date
        ORDER BY TRADE_DAYS ASC
        """)
        with db_config.get_wind_session() as session:
            result = session.execute(sql, {"start_date": start_date, "end_date": end_date})
            trading_dates = [str(row[0]) for row in result]
    except Exception as e:
        logger.warning(f"无法获取交易日历，使用现有日期: {str(e)}")
        trading_dates = sorted(df['trade_date'].unique())
    all_stocks = df['stock_code'].unique()
    full_index = pd.MultiIndex.from_product(
        [trading_dates, all_stocks], 
        names=['trade_date', 'stock_code']
    )
    df_indexed = df.set_index(['trade_date', 'stock_code'])
    df_reindexed = df_indexed.reindex(full_index)
    df_complete = df_reindexed.reset_index()
    return df_complete


