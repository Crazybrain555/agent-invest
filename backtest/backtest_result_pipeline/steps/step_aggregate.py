"""
Step: 聚合统计

职责：
- 从一次回测结果切片产出"总体+年度"指标
- 年度指标不再重跑引擎，而从序列切片计算
- portfolio 类指标调用公共 performance_calc.py
"""

import logging
from typing import TYPE_CHECKING, Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.backtest_result_pipeline.types import (
    StrategyBacktestResult,
    BenchmarkNavResult,
    OverallMetrics,
    YearlyMetrics,
    AggregatedTables
)
from backtest.metrics.performance_calc import calculate_portfolio_metrics, slice_portfolio_for_period

if TYPE_CHECKING:
    from configs.backtest.model_backtest_config import ModelBacktestConfig

logger = logging.getLogger(__name__)


def _calculate_ic_metrics_for_period(
    ic_series: pd.Series,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp
) -> Dict[str, float]:
    """计算指定区间的 IC 指标"""
    if ic_series is None or ic_series.empty:
        return {"mean_ic": 0.0, "ic_std": 0.0, "ic_hit_rate": 0.0}
    
    mask = (ic_series.index >= start_date) & (ic_series.index <= end_date)
    period_ic = ic_series[mask]
    
    if period_ic.empty:
        return {"mean_ic": 0.0, "ic_std": 0.0, "ic_hit_rate": 0.0}
    
    return {
        "mean_ic": float(period_ic.mean()),
        "ic_std": float(period_ic.std()),
        "ic_hit_rate": float((period_ic > 0).mean())
    }


def _calculate_factor_return_metrics_for_period(
    factor_return_series: pd.Series,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp
) -> Dict[str, float]:
    """计算指定区间的因子收益率指标"""
    if factor_return_series is None or factor_return_series.empty:
        return {"factor_return_total": 0.0, "factor_return_mean": 0.0, "factor_return_t_stat": 0.0}
    
    mask = (factor_return_series.index >= start_date) & (factor_return_series.index <= end_date)
    period_fr = factor_return_series[mask]
    
    if period_fr.empty:
        return {"factor_return_total": 0.0, "factor_return_mean": 0.0, "factor_return_t_stat": 0.0}
    
    total = float(period_fr.sum())
    mean = float(period_fr.mean())
    std = float(period_fr.std())
    n = len(period_fr)
    
    t_stat = mean / (std / np.sqrt(n)) if std > 0 and n > 0 else 0.0
    
    return {
        "factor_return_total": total,
        "factor_return_mean": mean,
        "factor_return_t_stat": t_stat
    }


def _calculate_turnover_metrics_for_period(
    trade_log_df: Optional[pd.DataFrame],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp
) -> Dict[str, float]:
    """计算指定区间的换手率指标（基于 trade_log 的 rebalance 记录）"""
    if trade_log_df is None or trade_log_df.empty:
        return {"turnover_mean": 0.0, "turnover_total": 0.0}

    if "date" not in trade_log_df.columns or "turnover" not in trade_log_df.columns:
        return {"turnover_mean": 0.0, "turnover_total": 0.0}

    df = trade_log_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]

    if df.empty:
        return {"turnover_mean": 0.0, "turnover_total": 0.0}

    turnover = pd.to_numeric(df["turnover"], errors="coerce").dropna()
    if turnover.empty:
        return {"turnover_mean": 0.0, "turnover_total": 0.0}

    return {
        "turnover_mean": float(turnover.mean()),
        "turnover_total": float(turnover.sum())
    }


def _get_yearly_ranges(portfolio_history: pd.Series) -> List[Dict]:
    """获取年度区间列表"""
    if portfolio_history is None or portfolio_history.empty:
        return []
    
    years = sorted(portfolio_history.index.year.unique())
    ranges = []
    
    for year in years:
        year_start = pd.Timestamp(f"{year}-01-01")
        year_end = pd.Timestamp(f"{year}-12-31")
        
        # 实际的年度起止日期
        year_data = portfolio_history[(portfolio_history.index.year == year)]
        if year_data.empty:
            continue
        
        actual_start = year_data.index.min()
        actual_end = year_data.index.max()
        
        ranges.append({
            "year": year,
            "start_date": actual_start,
            "end_date": actual_end
        })
    
    return ranges


def aggregate_overall_metrics(
    bt_result: StrategyBacktestResult,
    benchmark_result: Optional[BenchmarkNavResult],
    benchmark_code: str
) -> OverallMetrics:
    """聚合单个策略的总体指标"""
    perf = bt_result.performance
    
    # 基准相关
    benchmark_return = 0.0
    excess_return = 0.0
    if benchmark_result is not None:
        benchmark_return = benchmark_result.benchmark_total_return
        excess_return = benchmark_result.excess_total_return
    
    return OverallMetrics(
        strategy_name=bt_result.alpha.name,
        pool_code=bt_result.pool_code,
        total_return=perf.total_return,
        annual_return=perf.annual_return,
        volatility=perf.volatility,
        sharpe_ratio=perf.sharpe_ratio,
        max_drawdown=perf.max_drawdown,
        calmar_ratio=perf.calmar_ratio,
        hit_rate=perf.hit_rate,
        profit_loss_ratio=perf.profit_loss_ratio,
        var_95=perf.var_95,
        cvar_95=perf.cvar_95,
        benchmark_code=benchmark_code,
        benchmark_return=benchmark_return,
        excess_return=excess_return,
        mean_ic=perf.mean_ic,
        ic_std=perf.ic_std,
        ic_hit_rate=perf.ic_hit_rate,
        factor_return_total=perf.factor_return_total,
        factor_return_mean=perf.factor_return_mean,
        factor_return_t_stat=perf.factor_return_t_stat,
        turnover_mean=perf.turnover_mean,
        turnover_total=perf.turnover_total
    )


def aggregate_yearly_metrics(
    bt_result: StrategyBacktestResult,
    benchmark_result: Optional[BenchmarkNavResult],
    benchmark_code: str
) -> List[YearlyMetrics]:
    """聚合单个策略的年度指标（从序列切片计算）"""
    yearly_metrics_list = []
    
    portfolio_history = bt_result.portfolio_history
    if portfolio_history is None or portfolio_history.empty:
        return yearly_metrics_list
    
    # 获取 IC 和因子收益序列
    ic_series = None
    factor_return_series = None
    
    if bt_result.ic_analysis is not None:
        ic_series = bt_result.ic_analysis.get("ic_series")
    
    if bt_result.factor_return_analysis is not None:
        factor_return_series = bt_result.factor_return_analysis.get("factor_return_series")
    
    # 获取 trade_log 用于计算年度换手
    trade_log_df = bt_result.trade_log if hasattr(bt_result, "trade_log") else None
    
    # 获取基准 NAV 用于计算年度超额
    benchmark_nav_series = None
    if benchmark_result is not None and not benchmark_result.nav_df.empty:
        nav_df = benchmark_result.nav_df.copy()
        nav_df["trade_date"] = pd.to_datetime(nav_df["trade_date"])
        benchmark_nav_series = nav_df.set_index("trade_date")["benchmark_nav"]
    
    # 年度切片
    yearly_ranges = _get_yearly_ranges(portfolio_history)
    
    for yr in yearly_ranges:
        year = yr["year"]
        start_date = yr["start_date"]
        end_date = yr["end_date"]
        
        # 切片 portfolio_history
        period_portfolio = slice_portfolio_for_period(portfolio_history, start_date, end_date)
        
        if period_portfolio.empty or len(period_portfolio) < 2:
            continue
        
        # 使用公共模块计算 portfolio 类指标
        pm = calculate_portfolio_metrics(period_portfolio)
        
        # 计算年度基准/超额收益
        benchmark_return = 0.0
        excess_return = 0.0
        if benchmark_nav_series is not None:
            # 基准也按与 portfolio 相同的区间切片（含 lookback），保证口径一致
            bench_start = period_portfolio.index.min()
            bench_end = period_portfolio.index.max()

            period_benchmark = benchmark_nav_series[
                (benchmark_nav_series.index >= bench_start) &
                (benchmark_nav_series.index <= bench_end)
            ]
            if not period_benchmark.empty and len(period_benchmark) >= 2:
                benchmark_return = period_benchmark.iloc[-1] / period_benchmark.iloc[0] - 1
                strategy_return = period_portfolio.iloc[-1] / period_portfolio.iloc[0] - 1
                excess_return = strategy_return - benchmark_return
        
        # 计算年度 IC 指标
        ic_metrics = _calculate_ic_metrics_for_period(ic_series, start_date, end_date)
        
        # 计算年度因子收益率指标
        fr_metrics = _calculate_factor_return_metrics_for_period(factor_return_series, start_date, end_date)

        # 计算年度换手率指标
        turnover_metrics = _calculate_turnover_metrics_for_period(trade_log_df, start_date, end_date)
        
        yearly_metrics_list.append(YearlyMetrics(
            year=year,
            strategy_name=bt_result.alpha.name,
            pool_code=bt_result.pool_code,
            total_return=pm.total_return,
            annual_return=pm.annual_return,
            volatility=pm.volatility,
            sharpe_ratio=pm.sharpe_ratio,
            max_drawdown=pm.max_drawdown,
            calmar_ratio=pm.calmar_ratio,
            hit_rate=pm.hit_rate,
            profit_loss_ratio=pm.profit_loss_ratio,
            var_95=pm.var_95,
            cvar_95=pm.cvar_95,
            benchmark_return=benchmark_return,
            excess_return=excess_return,
            mean_ic=ic_metrics["mean_ic"],
            ic_std=ic_metrics["ic_std"],
            ic_hit_rate=ic_metrics["ic_hit_rate"],
            factor_return_total=fr_metrics["factor_return_total"],
            factor_return_mean=fr_metrics["factor_return_mean"],
            factor_return_t_stat=fr_metrics["factor_return_t_stat"],
            turnover_mean=turnover_metrics["turnover_mean"],
            turnover_total=turnover_metrics["turnover_total"]
        ))
    
    return yearly_metrics_list


def run_step_aggregate(
    cfg: "ModelBacktestConfig",
    backtest_results: Dict[str, Dict[str, StrategyBacktestResult]],
    benchmark_results: Dict[str, Dict[str, BenchmarkNavResult]]
) -> AggregatedTables:
    """
    聚合统计（总体 + 年度）
    
    Args:
        cfg: 配置对象
        backtest_results: 回测结果
        benchmark_results: 基准对齐结果
    
    Returns:
        AggregatedTables: 聚合后的表格
    """
    logger.info("Step Aggregate: 聚合统计...")
    
    overall_list: List[OverallMetrics] = []
    yearly_list: List[YearlyMetrics] = []

    for pool_code, pool_results in backtest_results.items():
        pool_benchmarks = benchmark_results.get(pool_code, {})
        for strategy_name, bt_result in pool_results.items():
            benchmark_result = pool_benchmarks.get(strategy_name)
            benchmark_code = benchmark_result.benchmark_code if benchmark_result else ""

            overall = aggregate_overall_metrics(bt_result, benchmark_result, benchmark_code)
            overall_list.append(overall)

            yearly = aggregate_yearly_metrics(bt_result, benchmark_result, benchmark_code)
            yearly_list.extend(yearly)
    
    # 转换为 DataFrame
    overall_df = pd.DataFrame([vars(o) for o in overall_list])
    yearly_df = pd.DataFrame([vars(y) for y in yearly_list])
    
    # 构建核心指标汇总（总体 + 年度）
    summary_rows = []
    
    # 添加总体行
    for o in overall_list:
        summary_rows.append({
            "股票池": o.pool_code,
            "时期": "总体",
            "策略": o.strategy_name,
            "总收益率": o.total_return,
            "基准收益率": o.benchmark_return,
            "超额收益率": o.excess_return,
            "夏普比率": o.sharpe_ratio,
            "最大回撤": o.max_drawdown,
            "Calmar比率": o.calmar_ratio,
            "胜率": o.hit_rate,
            "IC均值": o.mean_ic,
            "IC胜率": o.ic_hit_rate
        })
    
    # 添加年度行
    for y in yearly_list:
        summary_rows.append({
            "股票池": y.pool_code,
            "时期": str(y.year),
            "策略": y.strategy_name,
            "总收益率": y.total_return,
            "基准收益率": y.benchmark_return,
            "超额收益率": y.excess_return,
            "夏普比率": y.sharpe_ratio,
            "最大回撤": y.max_drawdown,
            "Calmar比率": y.calmar_ratio,
            "胜率": y.hit_rate,
            "IC均值": y.mean_ic,
            "IC胜率": y.ic_hit_rate
        })
    
    summary_df = pd.DataFrame(summary_rows)

    if not summary_df.empty:
        ordered_cols = ["股票池", "时期", "策略"]
        remaining_cols = [c for c in summary_df.columns if c not in ordered_cols]
        summary_df = summary_df.reindex(columns=ordered_cols + remaining_cols)

    logger.info(f"Step Aggregate: 完成 {len(overall_list)} 个策略，{len(yearly_list)} 个年度记录")
    
    return AggregatedTables(
        summary=summary_df,
        overall=overall_df,
        yearly=yearly_df
    )
