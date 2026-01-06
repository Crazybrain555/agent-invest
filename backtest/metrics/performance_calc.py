"""
绩效计算公共模块 - 单一真源

提供 portfolio 类绩效指标的计算函数，由引擎（Backtester._calculate_performance）
与 pipeline（step_aggregate）共同调用，保证"总体 vs 年度切片"口径一致。

注意：
- 本模块只负责 portfolio 类指标（收益、波动、Sharpe、回撤、胜率、VaR/CVaR、盈亏比等）
- IC/因子收益率由 step_aggregate.py 基于 ic_series/factor_return_series 计算
- 换手率由 trade_log 切片聚合
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class PortfolioMetrics:
    """Portfolio 类绩效指标（不含 IC/因子收益/换手）"""
    total_return: float = 0.0
    annual_return: float = 0.0
    volatility: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    calmar_ratio: float = 0.0
    hit_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    var_95: float = 0.0
    cvar_95: float = 0.0
    # 回撤相关
    drawdown_series: Optional[pd.Series] = None  # 回撤序列（可选，用于画图）


def calculate_portfolio_metrics(
    portfolio_values: pd.Series,
    trading_days_per_year: int = 252,
    return_drawdown_series: bool = False
) -> PortfolioMetrics:
    """
    计算 portfolio 类绩效指标
    
    Args:
        portfolio_values: 组合市值序列 (DatetimeIndex)
        trading_days_per_year: 每年交易日数，默认 252
        return_drawdown_series: 是否返回回撤序列
    
    Returns:
        PortfolioMetrics 实例
    """
    if portfolio_values is None or portfolio_values.empty or len(portfolio_values) < 2:
        return PortfolioMetrics()
    
    # 计算日收益率
    returns = portfolio_values.pct_change().dropna()
    
    if returns.empty:
        return PortfolioMetrics()
    
    # ========== 基础指标 ==========
    # 总收益率
    total_return = portfolio_values.iloc[-1] / portfolio_values.iloc[0] - 1
    
    # 计算年数（基于实际交易天数）
    n_years = len(portfolio_values) / float(trading_days_per_year)
    if n_years < 1.0 / trading_days_per_year:
        n_years = 1.0 / trading_days_per_year
    
    # 年化收益率
    annual_return = (1 + total_return) ** (1 / n_years) - 1 if n_years > 0 else 0
    
    # 波动率（年化）
    volatility = returns.std() * np.sqrt(trading_days_per_year)
    
    # 夏普比率
    sharpe_ratio = annual_return / volatility if volatility > 0 else 0
    
    # ========== 回撤相关 ==========
    peak = portfolio_values.expanding().max()
    drawdown = (portfolio_values - peak) / peak
    max_drawdown = drawdown.min()
    
    # Calmar 比率
    calmar_ratio = annual_return / abs(max_drawdown) if max_drawdown < 0 else 0
    
    # ========== 胜率和盈亏比 ==========
    hit_rate = (returns > 0).mean()
    
    positive_returns = returns[returns > 0]
    negative_returns = returns[returns < 0]
    if len(negative_returns) > 0 and len(positive_returns) > 0:
        profit_loss_ratio = positive_returns.mean() / abs(negative_returns.mean())
    else:
        profit_loss_ratio = np.inf if len(negative_returns) == 0 else 0.0
    
    # ========== 风险指标 ==========
    var_95 = returns.quantile(0.05)
    cvar_95 = returns[returns <= var_95].mean() if len(returns[returns <= var_95]) > 0 else var_95
    
    return PortfolioMetrics(
        total_return=float(total_return),
        annual_return=float(annual_return),
        volatility=float(volatility),
        sharpe_ratio=float(sharpe_ratio),
        max_drawdown=float(max_drawdown),
        calmar_ratio=float(calmar_ratio),
        hit_rate=float(hit_rate),
        profit_loss_ratio=float(profit_loss_ratio),
        var_95=float(var_95),
        cvar_95=float(cvar_95),
        drawdown_series=drawdown if return_drawdown_series else None
    )


def calculate_period_return(
    nav_series: pd.Series,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp
) -> float:
    """
    计算指定区间的收益率（用于年度切片）
    
    Args:
        nav_series: 净值序列
        start_date: 区间开始日期
        end_date: 区间结束日期
    
    Returns:
        区间收益率
    
    说明：
        - 年度收益率 = nav[year_end] / nav[year_start_prev] - 1
        - 其中 year_start_prev 为当年第一交易日的上一交易日（若存在）
    """
    if nav_series is None or nav_series.empty:
        return 0.0
    
    # 筛选区间内的数据
    mask = (nav_series.index >= start_date) & (nav_series.index <= end_date)
    period_nav = nav_series[mask]
    
    if period_nav.empty or len(period_nav) < 1:
        return 0.0
    
    # 尝试获取 start_date 前一交易日的净值作为基准
    prior_mask = nav_series.index < start_date
    prior_nav = nav_series[prior_mask]
    
    if not prior_nav.empty:
        base_value = prior_nav.iloc[-1]
    else:
        base_value = period_nav.iloc[0]
    
    end_value = period_nav.iloc[-1]
    
    return (end_value / base_value - 1) if base_value != 0 else 0.0


def slice_portfolio_for_period(
    portfolio_values: pd.Series,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    include_lookback: bool = True
) -> pd.Series:
    """
    按时间区间切片 portfolio_values
    
    Args:
        portfolio_values: 完整的组合市值序列
        start_date: 区间开始日期
        end_date: 区间结束日期
        include_lookback: 是否包含 start_date 前一交易日（用于计算首日收益）
    
    Returns:
        切片后的 portfolio_values
    """
    if portfolio_values is None or portfolio_values.empty:
        return pd.Series(dtype=float)
    
    if include_lookback:
        # 找到 start_date 前一交易日
        prior_mask = portfolio_values.index < start_date
        prior_dates = portfolio_values.index[prior_mask]
        
        if len(prior_dates) > 0:
            lookback_date = prior_dates[-1]
            mask = (portfolio_values.index >= lookback_date) & (portfolio_values.index <= end_date)
        else:
            mask = (portfolio_values.index >= start_date) & (portfolio_values.index <= end_date)
    else:
        mask = (portfolio_values.index >= start_date) & (portfolio_values.index <= end_date)
    
    return portfolio_values[mask].copy()
