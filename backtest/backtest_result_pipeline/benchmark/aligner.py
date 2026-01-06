"""
基准对齐器

负责策略与基准的交易日对齐
"""

import logging
from typing import Tuple

import pandas as pd

logger = logging.getLogger(__name__)


def align_benchmark_to_strategy(
    strategy_dates: pd.DatetimeIndex,
    benchmark_df: pd.DataFrame,
    date_col: str = "trade_date"
) -> Tuple[pd.DataFrame, int]:
    """
    将基准数据对齐到策略交易日
    
    对齐策略：
    - 以策略交易日为主
    - 基准 reindex(strategy_dates) 后 ffill()
    - 仍缺失则丢弃该日并打印 warning
    
    Args:
        strategy_dates: 策略交易日序列
        benchmark_df: 基准数据（含 trade_date 列）
        date_col: 日期列名
    
    Returns:
        Tuple[aligned_df, missing_count]: 对齐后的 DataFrame 和丢弃的日期数
    """
    if benchmark_df.empty:
        return pd.DataFrame(), len(strategy_dates)
    
    # 转换日期
    df = benchmark_df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col)
    df = df.set_index(date_col)
    
    # 去重（保留第一条）
    df = df[~df.index.duplicated(keep='first')]
    
    # reindex 到策略日期
    aligned = df.reindex(strategy_dates)
    
    # ffill 填充
    missing_before = aligned.isna().any(axis=1).sum()
    aligned = aligned.ffill()
    
    # 仍缺失的日期
    missing_after = aligned.isna().any(axis=1).sum()
    
    if missing_after > 0:
        logger.warning(f"基准在策略日期中有 {missing_after} 天缺失（ffill 后仍无法填充），将丢弃这些日期")
        aligned = aligned.dropna()
    
    # 重置索引
    aligned = aligned.reset_index()
    aligned = aligned.rename(columns={"index": date_col})
    
    return aligned, missing_after


def calculate_nav_and_returns(
    strategy_values: pd.Series,
    benchmark_close: pd.Series,
    initial_capital: float = None
) -> pd.DataFrame:
    """
    计算净值和收益率
    
    Args:
        strategy_values: 策略组合市值序列（DatetimeIndex）
        benchmark_close: 基准收盘价序列（DatetimeIndex）
        initial_capital: 初始资金（可选，用于计算绝对净值）
    
    Returns:
        DataFrame 含:
        - trade_date
        - strategy_nav: 策略净值（首日=1）
        - strategy_nav_abs: 策略绝对净值（初始资金口径，可选）
        - benchmark_nav: 基准净值（首日=1）
        - excess_nav: 差值型超额净值（1 + (strategy_nav - benchmark_nav)）
        - excess_nav_diff: 差值本身（strategy_nav - benchmark_nav）
        - strategy_ret: 策略日收益率
        - benchmark_ret: 基准日收益率
        - active_ret: 主动收益率（strategy_ret - benchmark_ret）
    """
    if strategy_values.empty or benchmark_close.empty:
        return pd.DataFrame()
    
    # 对齐索引
    common_dates = strategy_values.index.intersection(benchmark_close.index)
    if len(common_dates) == 0:
        logger.warning("策略与基准没有重叠日期")
        return pd.DataFrame()
    
    strategy = strategy_values.loc[common_dates].sort_index()
    benchmark = benchmark_close.loc[common_dates].sort_index()
    
    # 计算净值（首日=1）
    strategy_nav = strategy / strategy.iloc[0]
    benchmark_nav = benchmark / benchmark.iloc[0]
    
    # 差值型超额净值
    excess_nav_diff = strategy_nav - benchmark_nav
    excess_nav = 1.0 + excess_nav_diff
    
    # 日收益率
    strategy_ret = strategy_nav.pct_change().fillna(0.0)
    benchmark_ret = benchmark_nav.pct_change().fillna(0.0)
    active_ret = strategy_ret - benchmark_ret
    
    # 构建 DataFrame
    result = pd.DataFrame({
        "trade_date": common_dates,
        "strategy_nav": strategy_nav.values,
        "benchmark_nav": benchmark_nav.values,
        "excess_nav": excess_nav.values,
        "excess_nav_diff": excess_nav_diff.values,
        "strategy_ret": strategy_ret.values,
        "benchmark_ret": benchmark_ret.values,
        "active_ret": active_ret.values
    })
    
    # 可选：绝对净值
    if initial_capital is not None:
        result["strategy_nav_abs"] = strategy.values / initial_capital
    
    return result
