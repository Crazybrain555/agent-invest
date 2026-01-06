"""
Step: 回测执行

职责：
- 对每个 AlphaExpression 只跑一次回测（不再按年重跑）
- 输出 StrategyBacktestResult（含 performance/portfolio_history/trade_log/ic_analysis/factor_return_analysis）
"""

import logging
from typing import TYPE_CHECKING, Dict, List, Optional

import pandas as pd

from backtest.backtester.backtest_runner import BacktestRunner
from backtest.configs.backtest_config import BacktestConfig
from backtest.expression.expression import AlphaExpression
from backtest.backtest_result_pipeline.types import StrategyBacktestResult, RunContext

if TYPE_CHECKING:
    from configs.backtest.model_backtest_config import ModelBacktestConfig

logger = logging.getLogger(__name__)

ALL_POOL_CODE = "ALL"


def _normalize_stock_code_series(series: pd.Series) -> pd.Series:
    """标准化股票代码（去后缀 + zfill(6)），用于 pool join key。"""
    codes = series.fillna("").astype(str).str.upper().str.strip()
    codes = codes.str.replace(r"\.(SZ|SH|BJ)$", "", regex=True)
    codes = codes.str.split(".").str[0]
    codes = codes.replace({"": pd.NA, "NAN": pd.NA})
    mask = codes.notna()
    codes.loc[mask] = codes.loc[mask].str.zfill(6)
    return codes


def _attach_join_keys(df: pd.DataFrame) -> pd.DataFrame:
    """添加 trade_date_key + stock_code_key 用于 pool 成份 join。"""
    df_keyed = df.copy()
    df_keyed["trade_date_key"] = pd.to_datetime(df_keyed["trade_date"]).dt.strftime("%Y%m%d")
    df_keyed["stock_code_key"] = _normalize_stock_code_series(df_keyed["stock_code"])
    return df_keyed


def _fetch_pool_members(
    cfg: "ModelBacktestConfig",
    pool_code: str,
    cache: Dict[str, pd.DataFrame]
) -> pd.DataFrame:
    """读取股票池成份（返回已带 join key 的 DataFrame）。"""
    if pool_code in cache:
        return cache[pool_code]

    try:
        from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
    except Exception as exc:  # pragma: no cover - import failure will surface at runtime
        logger.error(f"无法导入 LocalTestDBDataProvider: {exc}")
        raise

    pool_table = getattr(cfg, "pool_table", "ai_is.stk_pool_of_index")
    pool_signal_value = getattr(cfg, "pool_signal_value", None)
    column_filters = {"pool_code": [pool_code]}
    if pool_signal_value is not None:
        column_filters["signal"] = [pool_signal_value]

    provider = LocalTestDBDataProvider()
    pool_df = provider.fetch_data(
        table=pool_table,
        start_date=cfg.start_date,
        end_date=cfg.end_date,
        column_filters=column_filters
    )

    if pool_df.empty:
        logger.warning(f"股票池 {pool_code} 在 {cfg.start_date}-{cfg.end_date} 无成份数据")
        cache[pool_code] = pool_df
        return pool_df

    pool_df = pool_df.copy()
    if "trade_date" in pool_df.columns:
        pool_df["trade_date_key"] = pd.to_datetime(pool_df["trade_date"]).dt.strftime("%Y%m%d")
    if "stock_code" in pool_df.columns:
        pool_df["stock_code_key"] = _normalize_stock_code_series(pool_df["stock_code"])

    pool_df = pool_df.dropna(subset=["trade_date_key", "stock_code_key"])
    pool_df = pool_df[["trade_date_key", "stock_code_key"]].drop_duplicates()

    cache[pool_code] = pool_df
    return pool_df


def create_backtest_config(cfg: "ModelBacktestConfig", max_stocks_override: Optional[int] = None) -> BacktestConfig:
    """从 ModelBacktestConfig 创建 BacktestConfig"""
    return BacktestConfig(
        # 基础配置
        start_date=cfg.start_date,
        end_date=cfg.end_date,
        
        # 权重分配和中性化配置
        weight_method=cfg.weight_method,
        neutralize_method=cfg.neutralize_method,
        neutralize_industry_name=cfg.neutralize_industry_name,
        neutralize_algo=cfg.neutralize_algo,
        
        # 交易策略配置
        rebalance_frequency=cfg.rebalance_frequency,
        trade_at=cfg.trade_at,
        trade_cost_rate=cfg.trade_cost_rate,
        slippage_ratio=cfg.slippage_ratio,
        
        # IC计算配置
        ic_calculation_period=cfg.ic_calculation_period,
        ic_method=cfg.ic_method,
        
        # 因子收益率配置
        factor_return_period=cfg.factor_return_period,
        factor_return_calculation_frequency=cfg.factor_return_calculation_frequency,
        
        # 组合配置
        initial_capital=cfg.initial_capital,
        max_position_size=cfg.max_position_size,
        min_market_cap=cfg.min_market_cap,
        max_stocks=max_stocks_override if max_stocks_override is not None else cfg.max_stocks,
        
        # 详细交易记录输出配置
        enable_detailed_log=cfg.enable_detailed_log,
        detailed_log_path=cfg.detailed_log_path,
        log_holdings=cfg.log_holdings,
        log_trades=cfg.log_trades,
        log_costs=cfg.log_costs
    )


def run_step_backtest(
    cfg: "ModelBacktestConfig",
    df_factor: pd.DataFrame,
    alpha_expressions: List[AlphaExpression],
    run_ctx: RunContext
) -> Dict[str, Dict[str, StrategyBacktestResult]]:
    """
    执行回测（每个策略只跑一次）
    
    Args:
        cfg: ModelBacktestConfig 配置对象
        df_factor: 因子 DataFrame
        alpha_expressions: 策略表达式列表
        run_ctx: 运行上下文
    
    Returns:
        Dict[pool_code, Dict[strategy_name, StrategyBacktestResult]]: 股票池维度到策略结果的映射
    """
    logger.info(f"Step Backtest: 执行回测 ({cfg.start_date} - {cfg.end_date})...")
    logger.info(f"   共 {len(alpha_expressions)} 个策略")

    if df_factor["stock_code"].astype(str).str.contains(r"\.").any():
        logger.info("   stock_code 包含后缀，将在 pool join 时统一去后缀+zfill(6)")

    pool_codes = [code for code in getattr(cfg, "pool_codes", []) if code and code != ALL_POOL_CODE]
    market_top_n = getattr(cfg, "market_top_n", None) or cfg.max_stocks
    pool_top_n = getattr(cfg, "pool_top_n", None) or cfg.max_stocks

    df_factor_keyed = _attach_join_keys(df_factor)
    df_factor_keyed = df_factor_keyed.dropna(subset=["trade_date_key", "stock_code_key"])

    pool_cache: Dict[str, pd.DataFrame] = {}
    results: Dict[str, Dict[str, StrategyBacktestResult]] = {}

    pools_to_run = [(ALL_POOL_CODE, df_factor, market_top_n)]

    for pool_code in pool_codes:
        pool_members = _fetch_pool_members(cfg, pool_code, pool_cache)
        if pool_members.empty:
            logger.warning(f"   股票池 {pool_code} 成份为空，跳过")
            continue

        df_pool = df_factor_keyed.merge(
            pool_members,
            on=["trade_date_key", "stock_code_key"],
            how="inner"
        )
        if df_pool.empty:
            logger.warning(f"   股票池 {pool_code} 过滤后因子为空，跳过")
            continue

        df_pool = df_pool.drop(columns=["trade_date_key", "stock_code_key"])
        pools_to_run.append((pool_code, df_pool, pool_top_n))

    for pool_code, df_pool_factor, top_n in pools_to_run:
        logger.info(f"   回测股票池: {pool_code} (top_n={top_n})")

        backtest_config = create_backtest_config(cfg, max_stocks_override=top_n)
        if cfg.enable_detailed_log:
            backtest_config.detailed_log_path = str(run_ctx.logs_dir / f"detailed_trading_log_{pool_code}.csv")

        runner = BacktestRunner(backtest_config)
        pool_results: Dict[str, StrategyBacktestResult] = {}

        for i, alpha in enumerate(alpha_expressions):
            logger.info(f"      [{i+1}/{len(alpha_expressions)}] 回测策略: {alpha.name}")

            try:
                raw_result = runner.run_single_backtest(
                    alpha,
                    data_source=df_pool_factor,
                    plot_results=False
                )

                result = StrategyBacktestResult(
                    pool_code=pool_code,
                    alpha=raw_result['alpha'],
                    performance=raw_result['performance'],
                    portfolio_history=raw_result.get('portfolio_history', pd.Series(dtype=float)),
                    trade_log=raw_result.get('trade_log', pd.DataFrame()),
                    risk_log=raw_result.get('risk_log', pd.DataFrame()),
                    ic_analysis=raw_result.get('ic_analysis'),
                    factor_return_analysis=raw_result.get('factor_return_analysis')
                )

                pool_results[alpha.name] = result

                perf = result.performance
                logger.info(f"         总收益率: {perf.total_return:.2%}, 夏普比率: {perf.sharpe_ratio:.3f}")

            except Exception as e:
                logger.error(f"         策略 {alpha.name} 回测失败: {str(e)}")
                import traceback
                traceback.print_exc()
                continue

        results[pool_code] = pool_results
        logger.info(f"   股票池 {pool_code} 完成 {len(pool_results)}/{len(alpha_expressions)} 个策略")

    return results
