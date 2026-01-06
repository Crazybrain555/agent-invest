"""
Step: 基准对齐

职责：
- 拉取基准数据
- 对齐策略与基准交易日
- 计算差值型超额 NAV 和 active_ret
- 计算超额收益率（算术相减口径）
"""

import logging
from typing import TYPE_CHECKING, Dict

import pandas as pd

from backtest.backtest_result_pipeline.types import StrategyBacktestResult, BenchmarkNavResult
from backtest.backtest_result_pipeline.benchmark.provider import fetch_benchmark_data
from backtest.backtest_result_pipeline.benchmark.aligner import align_benchmark_to_strategy, calculate_nav_and_returns

if TYPE_CHECKING:
    from configs.backtest.model_backtest_config import ModelBacktestConfig

logger = logging.getLogger(__name__)


def run_step_benchmark(
    cfg: "ModelBacktestConfig",
    backtest_results: Dict[str, Dict[str, StrategyBacktestResult]]
) -> Dict[str, Dict[str, BenchmarkNavResult]]:
    """
    基准对齐与超额计算
    
    Args:
        cfg: ModelBacktestConfig 配置对象
        backtest_results: 策略回测结果
    
    Returns:
        Dict[pool_code, Dict[strategy_name, BenchmarkNavResult]]: 基准对齐结果
    """
    default_benchmark = getattr(cfg, "benchmark_code", None)

    if not default_benchmark:
        logger.warning("未配置 benchmark_code，跳过基准对齐")
        return {}

    logger.info("Step Benchmark: 基准对齐...")

    results: Dict[str, Dict[str, BenchmarkNavResult]] = {}
    benchmark_cache: Dict[tuple, pd.DataFrame] = {}

    for pool_code, pool_results in backtest_results.items():
        benchmark_code = default_benchmark if pool_code == "ALL" else pool_code
        logger.info(f"   股票池 {pool_code} 使用基准: {benchmark_code}")

        for strategy_name, bt_result in pool_results.items():
            logger.info(f"      处理策略: {strategy_name}")

            portfolio_history = bt_result.portfolio_history
            if portfolio_history is None or portfolio_history.empty:
                logger.warning(f"         策略 {strategy_name} 的 portfolio_history 为空，跳过")
                continue

            portfolio_history = portfolio_history.sort_index()

            start_str = portfolio_history.index.min().strftime("%Y%m%d")
            end_str = portfolio_history.index.max().strftime("%Y%m%d")

            cache_key = (benchmark_code, start_str, end_str)
            if cache_key not in benchmark_cache:
                benchmark_df = fetch_benchmark_data(benchmark_code, start_str, end_str)
                benchmark_cache[cache_key] = benchmark_df
            else:
                benchmark_df = benchmark_cache[cache_key]

            if benchmark_df.empty:
                logger.warning(f"         基准 {benchmark_code} 无数据，跳过")
                continue

            benchmark_df = benchmark_df.copy()
            benchmark_df["trade_date"] = pd.to_datetime(benchmark_df["trade_date"])
            aligned_df, _missing = align_benchmark_to_strategy(
                portfolio_history.index,
                benchmark_df
            )

            if aligned_df.empty:
                logger.warning(f"         对齐后无重叠日期，跳过")
                continue

            aligned_df = aligned_df.set_index("trade_date")
            benchmark_close = aligned_df["index_close"]

            nav_df = calculate_nav_and_returns(
                portfolio_history,
                benchmark_close,
                initial_capital=getattr(cfg, "initial_capital", None)
            )

            if nav_df.empty:
                logger.warning(f"         NAV 计算失败，跳过")
                continue

            strategy_total_return = nav_df["strategy_nav"].iloc[-1] - 1.0
            benchmark_total_return = nav_df["benchmark_nav"].iloc[-1] - 1.0
            excess_total_return = strategy_total_return - benchmark_total_return

            logger.info(
                f"         策略收益: {strategy_total_return:.2%} | "
                f"基准收益: {benchmark_total_return:.2%} | "
                f"超额收益: {excess_total_return:.2%}"
            )

            results.setdefault(pool_code, {})[strategy_name] = BenchmarkNavResult(
                pool_code=pool_code,
                benchmark_code=benchmark_code,
                strategy_name=strategy_name,
                nav_df=nav_df,
                strategy_total_return=strategy_total_return,
                benchmark_total_return=benchmark_total_return,
                excess_total_return=excess_total_return
            )

    logger.info("Step Benchmark: 完成")

    return results
