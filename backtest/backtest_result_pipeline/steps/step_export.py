"""
Step: 统一导出

职责：
- 导出 NAV CSV
- 导出 NAV PNG
- 导出 signals CSV（当 enable_detailed_log=True）
- 导出 config JSON
- 生成 manifest.json
"""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List

import pandas as pd

from backtest.backtest_result_pipeline.types import (
    RunContext,
    StrategyBacktestResult,
    BenchmarkNavResult,
    AggregatedTables,
    PipelineResult
)
from backtest.backtest_result_pipeline.io.naming import (
    deduplicate_names,
    get_nav_csv_filename,
    get_nav_png_filename,
    get_signals_csv_filename,
    get_excel_filename,
    get_detailed_log_filename
)
from backtest.backtest_result_pipeline.io.atomic_write import atomic_write_df, atomic_write_json
from backtest.backtest_result_pipeline.io.manifest import ManifestBuilder
from backtest.backtest_result_pipeline.io.factor_cache import FactorCacheManager
from backtest.backtest_result_pipeline.report.excel_report import write_excel_report
from backtest.backtest_result_pipeline.report.plot_report import plot_nav_curve

if TYPE_CHECKING:
    from configs.backtest.model_backtest_config import ModelBacktestConfig

logger = logging.getLogger(__name__)


def run_step_export(
    cfg: "ModelBacktestConfig",
    run_ctx: RunContext,
    backtest_results: Dict[str, Dict[str, StrategyBacktestResult]],
    benchmark_results: Dict[str, Dict[str, BenchmarkNavResult]],
    aggregated_tables: AggregatedTables
) -> PipelineResult:
    """
    统一导出
    
    Args:
        cfg: 配置对象
        run_ctx: 运行上下文
        backtest_results: 回测结果
        benchmark_results: 基准对齐结果
        aggregated_tables: 聚合表格
    
    Returns:
        PipelineResult: 产物路径汇总
    """
    logger.info("Step Export: 统一导出...")
    
    benchmark_code = getattr(cfg, "benchmark_code", "")

    # 策略名称映射（原始 -> safe）
    original_names = sorted({name for pool in backtest_results.values() for name in pool.keys()})
    name_mapping = deduplicate_names(original_names)
    
    # 初始化 manifest 构建器
    bt_results_dir = run_ctx.run_dir.parent
    manifest = ManifestBuilder(run_ctx.run_id, run_ctx.run_dir, factor_base_dir=bt_results_dir)
    manifest.set_strategy_name_mapping(name_mapping)
    
    # 产物路径
    nav_csv_paths: Dict[str, Dict[str, Path]] = {}
    nav_png_paths: Dict[str, Dict[str, Path]] = {}
    signals_csv_paths: Dict[str, Dict[str, Path]] = {}
    factor_paths: List[Path] = []
    
    # ========== 1. 导出 config JSON ==========
    logger.info("   导出配置快照...")
    
    # run_config.json
    run_config = {
        "start_date": cfg.start_date,
        "end_date": cfg.end_date,
        "benchmark_code": benchmark_code,
        "model_path": str(cfg.model_path),
        "initial_capital": cfg.initial_capital,
        "rebalance_frequency": cfg.rebalance_frequency,
        "trade_cost_rate": cfg.trade_cost_rate,
        "max_stocks": cfg.max_stocks,
        "max_position_size": cfg.max_position_size,
        "pool_codes": getattr(cfg, "pool_codes", []),
        "pool_table": getattr(cfg, "pool_table", ""),
        "pool_signal_value": getattr(cfg, "pool_signal_value", None),
        "pool_top_n": getattr(cfg, "pool_top_n", None),
        "market_top_n": getattr(cfg, "market_top_n", None),
        "factor_cache_dir": str(bt_results_dir / "factors"),
        "factor_cache_mode": "shared",
        "factor_total_default": False
    }
    run_config_path = run_ctx.config_dir / "run_config.json"
    atomic_write_json(run_config, run_config_path, no_overwrite=False)
    manifest.add_config_file("run_config", run_config_path)
    manifest.set_run_config_summary(run_config)
    
    # ========== 2. 导出 NAV CSV 和 PNG ==========
    logger.info("   导出 NAV 文件...")
    
    for pool_code, pool_benchmarks in benchmark_results.items():
        pool_nav_dir = run_ctx.nav_dir / pool_code
        pool_plots_dir = run_ctx.plots_dir / pool_code

        for strategy_name, benchmark_result in pool_benchmarks.items():
            safe_name = name_mapping.get(strategy_name, strategy_name)

            csv_filename = get_nav_csv_filename(benchmark_result.benchmark_code, safe_name)
            csv_path = pool_nav_dir / csv_filename

            nav_df = benchmark_result.nav_df
            atomic_write_df(nav_df, csv_path, no_overwrite=False)

            nav_csv_paths.setdefault(pool_code, {})[strategy_name] = csv_path
            manifest.add_nav_csv(pool_code, strategy_name, csv_path)

            png_filename = get_nav_png_filename(benchmark_result.benchmark_code, safe_name)
            png_path = pool_plots_dir / png_filename

            try:
                result_path = plot_nav_curve(benchmark_result, png_path, no_overwrite=False)
                if result_path:
                    nav_png_paths.setdefault(pool_code, {})[strategy_name] = result_path
                    manifest.add_nav_png(pool_code, strategy_name, result_path)
            except Exception as e:
                logger.warning(f"      股票池 {pool_code} 策略 {strategy_name} 图表生成失败: {e}")
    
    # ========== 3. 导出 signals CSV（如果启用详细日志） ==========
    if getattr(cfg, "enable_detailed_log", False):
        logger.info("   导出 signals 文件...")
        
        for pool_code, pool_results in backtest_results.items():
            pool_signals_dir = run_ctx.signals_dir / pool_code
            for strategy_name, bt_result in pool_results.items():
                safe_name = name_mapping.get(strategy_name, strategy_name)

                trade_log = bt_result.trade_log
                if trade_log is not None and not trade_log.empty:
                    csv_filename = get_signals_csv_filename(safe_name)
                    csv_path = pool_signals_dir / csv_filename

                    atomic_write_df(trade_log, csv_path, no_overwrite=False)

                    signals_csv_paths.setdefault(pool_code, {})[strategy_name] = csv_path
                    manifest.add_signals_csv(pool_code, strategy_name, csv_path)
    
    # ========== 4. 导出 Excel 报告 ==========
    logger.info("   导出 Excel 报告...")
    
    excel_filename = get_excel_filename(run_ctx.run_id)
    excel_path = run_ctx.tables_dir / excel_filename
    
    write_excel_report(aggregated_tables, excel_path, no_overwrite=False)
    manifest.add_tables_file(excel_path)
    
    # ========== 5. 收集因子文件 ==========
    cache_dir = bt_results_dir / "factors"
    if cache_dir.exists():
        cache = FactorCacheManager(cache_dir)
        for factor_file in cache.list_year_files_for_range(cfg.start_date, cfg.end_date):
            factor_paths.append(factor_file)
            manifest.add_factor_file(factor_file)
    
    # ========== 6. 保存 manifest.json ==========
    logger.info("   生成 manifest.json...")
    manifest_path = manifest.save(no_overwrite=False)
    
    logger.info(f"Step Export: 完成")
    logger.info(f"   run_dir: {run_ctx.run_dir}")
    logger.info(f"   Excel: {excel_path}")
    logger.info(f"   manifest: {manifest_path}")
    
    return PipelineResult(
        run_dir=run_ctx.run_dir,
        tables_excel_path=excel_path,
        manifest_path=manifest_path,
        nav_csv_paths=nav_csv_paths,
        nav_png_paths=nav_png_paths,
        signals_csv_paths=signals_csv_paths,
        factor_paths=factor_paths,
        strategy_name_mapping=name_mapping
    )
