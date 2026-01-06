#!/usr/bin/env python3
"""
模型回测脚本 - 触发器版本

职责：
1. CLI → cfg（ModelBacktestConfig）
2. cfg → BacktestResultPipeline(cfg).run()
3. 打印 pipeline 返回的 run_dir 与关键产物路径

改造说明：
- 业务逻辑已下沉到 backtest/backtest_result_pipeline/
- 本文件只负责参数解析和调用 pipeline
- 保留 ModelBacktester 和 run_full_backtest() 以兼容 run_tsvit.py
"""

import argparse
import warnings
from typing import Optional

# 导入配置层
from configs.backtest.model_backtest_config import ModelBacktestConfig

# 导入 Pipeline
from backtest.backtest_result_pipeline.pipeline import BacktestResultPipeline
from backtest.backtest_result_pipeline.types import PipelineResult


class ModelBacktester:
    """
    模型回测器类 - 兼容层
    
    保留此类名和 run_full_backtest() 方法以兼容 run_tsvit.py 的调用方式。
    内部直接调用 BacktestResultPipeline。
    """
    
    def __init__(self, cfg: ModelBacktestConfig):
        self.cfg = cfg
    
    def run_full_backtest(self) -> Optional[PipelineResult]:
        """
        运行完整回测，返回 PipelineResult
        
        注意：此方法内部调用 BacktestResultPipeline，
        不再走旧的 run_backtest_by_year() 流程。
        """
        try:
            pipeline = BacktestResultPipeline(self.cfg)
            result = pipeline.run()
            
            # 打印关键路径
            print(f"\n📂 回测结果目录: {result.run_dir}")
            print(f"📊 Excel 报告: {result.tables_excel_path}")
            print(f"📋 产物清单: {result.manifest_path}")
            
            if result.nav_csv_paths:
                nav_items = [
                    (pool_code, strategy_name, path)
                    for pool_code, pool_map in result.nav_csv_paths.items()
                    for strategy_name, path in pool_map.items()
                ]
                print(f"\n📈 NAV 文件 ({len(nav_items)} 个策略):")
                for pool_code, strategy_name, path in nav_items[:3]:
                    print(f"   - [{pool_code}] {strategy_name}: {path.name}")
                if len(nav_items) > 3:
                    print(f"   ... 还有 {len(nav_items) - 3} 个")
            
            return result
        
        except Exception as e:
            print(f"\n❌ 回测过程中出现错误: {str(e)}")
            import traceback
            traceback.print_exc()
            return None


# ==============================================================================
# DEPRECATED 函数 - 保留但不再被 pipeline 调用
# ==============================================================================

def create_backtest_config(cfg, start_date: str, end_date: str):
    """
    DEPRECATED: 已迁移到 backtest/backtest_result_pipeline/steps/step_backtest.py
    """
    warnings.warn(
        "create_backtest_config() 已废弃，请使用 BacktestResultPipeline",
        DeprecationWarning,
        stacklevel=2
    )
    from backtest.backtest_result_pipeline.steps.step_backtest import create_backtest_config as _create
    return _create(cfg)


def run_backtest_by_year(cfg, df_factor, factor_expressions):
    """
    DEPRECATED: 年度重跑模式已废弃，由"一次回测 + 切片聚合"替代
    """
    warnings.warn(
        "run_backtest_by_year() 已废弃。pipeline 使用一次回测 + 年度切片聚合，"
        "不再按年重跑引擎。请使用 BacktestResultPipeline。",
        DeprecationWarning,
        stacklevel=2
    )
    raise NotImplementedError("请使用 BacktestResultPipeline 替代 run_backtest_by_year()")


def attach_benchmark_and_export_nav(cfg, strategy_result, benchmark_code):
    """
    DEPRECATED: 已迁移到 backtest/backtest_result_pipeline/steps/step_benchmark.py
    """
    warnings.warn(
        "attach_benchmark_and_export_nav() 已废弃，请使用 BacktestResultPipeline",
        DeprecationWarning,
        stacklevel=2
    )
    raise NotImplementedError("请使用 BacktestResultPipeline 替代 attach_benchmark_and_export_nav()")


# ==============================================================================
# CLI 参数解析
# ==============================================================================

def parse_cli_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Model backtest script - Pipeline version"
    )
    
    # === 基本日期 ===
    parser.add_argument("--start_date", default="20200101",
                        help="回测开始日期，YYYYMMDD")
    parser.add_argument("--end_date", default="20250731",
                        help="回测结束日期，YYYYMMDD")

    # === 路径相关 ===
    parser.add_argument("--model_path", 
                        default=r'outputs/TSViT_MODEL/use_symmetric_Sentiment_liquidty_h64_l6_lr4e-05_wd1e+00_attn_pv_v7_20251127_145731',
                        help="训练好的模型目录")
    parser.add_argument("--dataset_path", default=None,
                        help="数据集目录（可选，如果不提供则从experiment_config.json中自动读取）")
    parser.add_argument("--backtest_result_path", default=None,
                        help="结果输出目录，默认自动生成")

    # === 资金与仓位 ===
    parser.add_argument("--initial_capital", type=float, default=1_000_000.0,
                        help="初始资金")
    parser.add_argument("--max_position_size", type=float, default=0.1,
                        help="单只股票最大权重")
    parser.add_argument("--max_stocks", type=int, default=100,
                        help="最大持仓数（全市场口径，等同 --market-top-n）")
    parser.add_argument("--min_market_cap", type=float, default=1e8 / 10000,
                        help="最小市值过滤（万元）")

    # === 股票池配置 ===
    parser.add_argument("--pool-codes", nargs="+", default=ModelBacktestConfig.pool_codes,
                        help="股票池列表（默认沪深300/中证500/中证1000）")
    parser.add_argument("--pool-top-n", dest="pool_top_n", type=int, default=argparse.SUPPRESS,
                        help="股票池内选股数量")
    parser.add_argument("--top-n", dest="pool_top_n", type=int, default=argparse.SUPPRESS,
                        help="股票池内选股数量（pool-top-n 的别名）")
    parser.add_argument("--market-top-n", dest="market_top_n", type=int, default=argparse.SUPPRESS,
                        help="全市场选股数量（默认沿用 max_stocks）")

    # === 权重分配和中性化 ===
    parser.add_argument("--weight_method", default="equal",
                        help="权重分配方法: equal(等权重), factor_score(因子得分加权)")
    parser.add_argument("--neutralize_method", nargs="+", default=["industry", "market_cap"],
                        help="中性化方法: industry(行业), market_cap(市值)")
    parser.add_argument("--neutralize_industry_name", default="CSI",
                        help="行业分类标准")
    parser.add_argument("--neutralize_algo", default="ols",
                        help="中性化算法: ols(普通最小二乘), wls(加权最小二乘)")

    # === 交易策略配置 ===
    parser.add_argument("--rebalance_frequency", default="10D",
                        help="调仓频率 1D/5D/10D/20D/1M/1Q")
    parser.add_argument("--trade_at", default="vwap",
                        help="交易价格: close(收盘价), vwap(成交量加权平均价)")
    parser.add_argument("--trade_cost_rate", type=float, default=20 / 10000,
                        help="交易费率（单边）")
    parser.add_argument("--slippage_ratio", type=float, default=0.0001,
                        help="滑点比例")
    parser.add_argument(
        "--benchmark_code", "--banchmark_code",
        dest="benchmark_code",
        default="000852.SH",
        help="基准指数 Wind 代码，例如 000852.SH(中证1000)、000905.SH(中证500)"
    )

    # === 因子计算配置 ===
    parser.add_argument("--factor_return_period", type=int, default=20,
                        help="因子收益率的未来收益计算周期")
    parser.add_argument("--factor_return_calculation_frequency", type=int, default=20,
                        help="因子收益率的截面回归计算频率")
    parser.add_argument("--factor_shift", type=int, default=1,
                        help="因子滞后一期防止偷看历史")
    parser.add_argument("--ic_calculation_period", type=int, default=20,
                        help="IC计算周期")
    parser.add_argument("--ic_method", default="spearman",
                        help="IC计算方法: pearson, spearman")

    # === 信号处理配置 ===
    parser.add_argument("--signal_negative", action="store_true",
                        help="信号是否取反")

    # === 输出和日志配置 ===
    parser.add_argument("--save_excel", action="store_true", default=True,
                        help="是否保存Excel报告")
    parser.add_argument("--print_results", action="store_true", default=True,
                        help="是否打印详细结果")
    parser.add_argument("--enable_detailed_log", action="store_true",
                        help="是否启用详细交易记录输出")
    parser.add_argument("--detailed_log_path", default="logs/detailed_trading_log.csv",
                        help="详细交易记录文件路径")
    parser.add_argument("--log_holdings", action="store_true", default=True,
                        help="是否记录持仓详情")
    parser.add_argument("--log_trades", action="store_true", default=True,
                        help="是否记录交易详情")
    parser.add_argument("--log_costs", action="store_true", default=True,
                        help="是否记录费用详情")
    
    # === 因子输出和保存配置 ===
    parser.add_argument("--factor_target_format", default="backtest",
                        help="因子输出格式: backtest, wind, live")
    parser.add_argument("--factor_save_formats", nargs="+", default=["csv"],
                        help="因子保存格式: csv, parquet, database")
    parser.add_argument("--enable_factor_save", action="store_true", default=True,  
                        help="是否启用因子保存")
    
    # === DB补齐配置 ===
    parser.add_argument("--fetch.seq_len", type=int, default=ModelBacktestConfig.fetch.seq_len,
                        help="序列长度，与模型训练时保持一致")
    parser.add_argument("--fetch.features_tables", nargs="+", 
                        default=["ai_is.inter_train_factors_mkt_processed_v3", 
                                "ai_is.quantitative_other_signals"],
                        help="特征表列表，用于DB补齐")
    parser.add_argument("--fetch.stats_table", 
                        default=ModelBacktestConfig.fetch.stats_table,
                        help="统计表，用于标准化")
    parser.add_argument("--fetch.clip_std", action="store_true", default=True,
                        help="是否启用标准差截尾")
    parser.add_argument("--fetch.factor_based_nan_handling", action="store_true", default=True,
                        help="启用因子配置驱动的NaN处理")
    parser.add_argument("--fetch.consecutive_nan_threshold", type=int, default=None,
                        help="连续NaN阈值，超过则不填充")
    parser.add_argument("--fetch.duck_threads", type=int, default=8,
                        help="DuckDB线程数")
    parser.add_argument("--fetch.duck_memory", default="16GB",
                        help="DuckDB内存限制")
    parser.add_argument("--fetch.duck_cache", default="4GB",
                        help="DuckDB缓存大小")
    parser.add_argument("--fetch.max_factors_per_batch", type=int, default=16,
                        help="透视宽表时的因子分批大小（降低内存峰值），默认16")

    return parser.parse_args()


def apply_cli_overrides(cfg: ModelBacktestConfig, args) -> None:
    """将 CLI 参数覆盖到配置对象"""
    arg_dict = vars(args)
    for key, value in arg_dict.items():
        if key.startswith('fetch.'):
            # 处理 fetch 子配置参数
            fetch_key = key[6:]  # 移除 'fetch.' 前缀
            setattr(cfg.fetch, fetch_key, value)
        else:
            # 处理普通配置参数
            setattr(cfg, key, value)

    if "market_top_n" not in arg_dict:
        cfg.market_top_n = getattr(cfg, "max_stocks", cfg.market_top_n)


def main():
    """主函数 - 触发器逻辑"""
    print("=" * 60)
    print("🚀 模型回测评估 (Pipeline 版本)")
    print("=" * 60)

    # ① 解析命令行参数
    args = parse_cli_args()

    # ② 创建配置对象并使用命令行参数覆盖默认值
    cfg = ModelBacktestConfig()
    apply_cli_overrides(cfg, args)
    
    print(f"\n📊 配置概览:")
    print(f"   - 回测期间: {cfg.start_date} - {cfg.end_date}")
    print(f"   - 模型路径: {cfg.model_path}")
    print(f"   - 基准代码: {cfg.benchmark_code}")
    print(f"   - 数据集路径: {cfg.dataset_path or '自动检测'}")

    # ③ 调用 Pipeline（CLI 与训练脚本共用同一入口）
    result = ModelBacktester(cfg).run_full_backtest()
    
    if result is not None:
        print("\n✅ 回测评估完成！")
        return result
    else:
        print("\n❌ 回测评估失败")
        return None


if __name__ == "__main__":
    results = main()
