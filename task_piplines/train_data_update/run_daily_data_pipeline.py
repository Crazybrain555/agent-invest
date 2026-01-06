import logging
from datetime import datetime
import sys
import os
import argparse

# Ensure the src directory is in the Python path
# This is often needed when running scripts from a different directory
project_root = os.path.dirname(os.path.abspath(__file__)) 
if project_root not in sys.path:
    sys.path.append(project_root)

from src.scheduler.Dfzq_gru_scheduler import DfzqGruScheduler
from src.utils.logger import setup_logger
from src.data_service.preprocessing.methods import norm_config as nc

# Configure logging
logger = setup_logger(__name__) # The function already sets INFO level by default

# --- Configuration ---
DATA_NORM_CONFIG = {
    "processing_mode": "factor_engineering",  # 使用因子工程模式
    "lookback_periods": 100,                  # 回看期数，设置为较大值以覆盖最大窗口需求（最大窗口90天）
    "days_count": 1,                         # Daily data
    "start_date": "2025-08-11",              # 修改：从20250715年开始重新计算
    "overlap_days": 20,                     # 重叠天数，设置为较大值以确保数据连续性
    "force_update": True,                    #  修改：强制更新，确保重新处理数据
    "field_batch_size": 12,                  #  性能优化：增加字段批次大小，减少数据库操作次数
    "table_name": "inter_train_factors_mkt_processed_v3",  # 新表名称
    "field_window_config": "Z_WINDOW_MAP_FACTOR_ENGINEERING",  # 🎯 因子工程字段窗口配置
    # 🚀 新增性能优化参数
    "use_parallel": True,                    # 启用并行处理
    "upsert_batch_rows": 2_000_000,         #  修复：减少UPSERT批次大小到200万行，避免锁超时
    "copy_batch_size": 200_000,             # COPY批次大小：20万行
    "enable_trading_day_alignment": True,    #  新增：启用交易日对齐功能
}

# 标准化参数生成配置 - V3版本，支持多表合并处理
STANDARD_PARAMS_CONFIG = {
    "source_table": [
        "inter_train_factors_mkt_processed_v3",
        "quantitative_alternative_high_frequency_signals",
        "quantitative_alternative_institutional_patent_signals",
        "quantitative_analyst_coverage_rating_signals",
        "quantitative_analyst_earnings_revision_signals",
        "quantitative_growth_forecast_trend_signals",
        "quantitative_growth_profitability_signals",
        "quantitative_growth_revenue_asset_signals",
        "quantitative_quality_cashflow_safety_signals",
        "quantitative_quality_operating_efficiency_signals",
        "quantitative_quality_profit_quality_signals",
        "quantitative_sentiment_liquidity_signals",
        "quantitative_sentiment_momentum_signals",
        "quantitative_sentiment_price_return_signals",
        "quantitative_sentiment_value_reversal_signals",
        "quantitative_sentiment_volatility_signals",
        "quantitative_value_valuation_signals"
    ],  # 🚀 多表支持：源数据表名列表（不包含quantitative_other_signals）
    "start_date": "2008-01-01",     # Start date
    "end_date": "2018-12-31",       # End date
    "data_format": "long",          # Data format - 长表格式
    "mad_multiplier": 8.0,          # MAD multiplier
    "min_samples": 1000,            # 最小样本数，少于此数量将设为NaN
    "batch_size": 25,               # 每批处理的特征组合数量
    "save_format": "database",      # Save format
    "skip_if_exists": True,         # Skip if exists
    "table_name_prefix": "train_signals_std",  # 🚀 修复遗漏：自定义表名前缀
    "factors_per_batch": 10          # 修复内存溢出：大窗口内每批处理的因子数量，按表处理更安全
}

# 标签生成配置
LABEL_GENERATION_CONFIG = {
    "label_shift": [10, 20],     # 同时生成10日和20日未来收益率
    "corr_window": 240,          # 相关性计算窗口
    "corr_rank_num": 30,         # 相关性排名数量
    "min_rank_num": 20,          # 最小邻居数量
    "use_db_pct_change": False,  # 是否使用数据库中的百分比变化，改为False以使用价格数据计算收益率
    "correlation_type": "pearson", # 相关性类型
    "overlap_days": 20,          # 数据重叠天数
    "strategies": ["top_correlation", "rank"]  # 🎯 支持多策略：可以是单个字符串或列表
}

# 禁投池生成配置
FORBID_POOL_CONFIG = {
    "start_date": "2006-01-01",         # 禁投池数据开始日期
    "table_name": "forbid_pool_comprehensive",  # 禁投池表名
    "ipo_days": 122,                    # 新股上市天数阈值，小于此天数的为新股
    "st_lookback_days": 20,             # ST股票筛选回看天数
    "stock_code_prefixes": ["0", "3", "6"],  # 股票代码前缀筛选：主板、创业板、科创板
    "overlap_days": 20                   # 更新时的重叠天数
}

def main():
    """
    Main function to initialize the scheduler and run the data pipeline task.
    
    Supports two modes:
    1. Run immediately (default): Processes data once and exits
    2. Run as schedule: Sets up scheduled tasks and runs continuously
    """
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description='Run daily data pipeline')
    parser.add_argument('--schedule', action='store_true', 
                      help='Run as a scheduled task instead of immediate execution')
    parser.add_argument('--schedule-time', type=str, default="23:00",
                      help='Time to run scheduled task (HH:MM format, 24-hour clock)')
    parser.add_argument('--step', type=str, choices=['all', 'normalize', 'standardize', 'label', 'forbid'], default='all',
                      help='Which step to run: all, normalize (Step 1), standardize (Step 2), label (Step 4), or forbid (Step 5: Forbid Pool)')
    parser.add_argument('--skip-if-exists', action='store_true',
                      help='Skip standardization parameter generation if the target table already exists')
    parser.add_argument('--force-update', action='store_true',
                      help='Force update even if data appears to be up-to-date')
    args = parser.parse_args()
    
    logger.info("--- Daily Data Pipeline Scheduler ---")
    logger.info(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        # 1. Initialize the Scheduler
        logger.info("初始化调度器...")
        scheduler = DfzqGruScheduler()
        
        # 如果命令行提供了force_update参数，覆盖配置中的值
        if args.force_update:
            DATA_NORM_CONFIG["force_update"] = True
            logger.info("从命令行参数启用强制更新模式")
        
        # 🎯 解析因子工程字段窗口配置
        map_name = DATA_NORM_CONFIG.get("field_window_config", None)
        if isinstance(map_name, str):
            field_window_config = getattr(nc, map_name, None)
            if field_window_config is None:
                logger.warning(f"未找到字段窗口配置 '{map_name}'，使用默认 Z_WINDOW_MAP_FACTOR_ENGINEERING")
                field_window_config = nc.Z_WINDOW_MAP_FACTOR_ENGINEERING
        else:
            field_window_config = nc.Z_WINDOW_MAP_FACTOR_ENGINEERING
        
        logger.info(f"使用因子工程字段窗口配置: {map_name or 'Z_WINDOW_MAP_FACTOR_ENGINEERING'}")
        
        logger.info("调度器初始化完成。")
        
        if args.schedule:
            # Run in scheduled mode
            logger.info(f"设置计划任务，将在每天 {args.schedule_time} 运行...")
            
            # Override the default schedule time in DfzqGruScheduler
            # This requires a small modification to schedule_tasks method to accept a parameter
            if hasattr(scheduler, 'schedule_tasks') and callable(getattr(scheduler, 'schedule_tasks')):
                # Try to set the custom schedule time
                try:
                    original_schedule_tasks = scheduler.schedule_tasks
                    
                    def modified_schedule_tasks():
                        logger.info(f"使用自定义计划时间: {args.schedule_time}")
                        # Clear any existing schedules
                        import schedule
                        schedule.clear()
                        # 使用配置文件中的force_update设置来替代硬编码的默认值
                        scheduler.schedule_tasks(force_update=DATA_NORM_CONFIG["force_update"])
                        # 修改调度时间
                        for job in schedule.jobs:
                            job.at(args.schedule_time)
                        logger.info(f"已设置每日数据流水线任务在 {args.schedule_time} 运行，force_update={DATA_NORM_CONFIG['force_update']}")
                    
                    # Replace the method
                    scheduler.schedule_tasks = modified_schedule_tasks
                    
                except Exception as e:
                    logger.warning(f"无法设置自定义计划时间: {str(e)}, 将使用默认值")
            
            # Start the scheduler
            logger.info("启动计划任务调度器...")
            try:
                scheduler.run_continuously(force_update=DATA_NORM_CONFIG["force_update"])
            except KeyboardInterrupt:
                logger.info("收到中断信号，正在结束调度器...")
        else:
            # Run immediately
            logger.info("立即执行数据处理任务...")
            
            # 根据参数选择执行哪个步骤
            if args.step == 'all' or args.step == 'normalize':
                logger.info(f"执行Step 1: 市场行情数据因子工程任务，参数: {DATA_NORM_CONFIG}")
                success_step1 = scheduler.data_manager.run_market_data_factor_engineering(
                    processing_mode=DATA_NORM_CONFIG["processing_mode"],
                    lookback_periods=DATA_NORM_CONFIG["lookback_periods"], 
                    days_count=DATA_NORM_CONFIG["days_count"],
                    start_date=DATA_NORM_CONFIG["start_date"],
                    overlap_days=DATA_NORM_CONFIG["overlap_days"],
                    force_update=DATA_NORM_CONFIG["force_update"],
                    field_batch_size=DATA_NORM_CONFIG["field_batch_size"],
                    table_name=DATA_NORM_CONFIG["table_name"],
                    field_window_config=field_window_config,
                    # 🚀 传递性能优化参数
                    use_parallel=DATA_NORM_CONFIG["use_parallel"],
                    upsert_batch_rows=DATA_NORM_CONFIG["upsert_batch_rows"],
                    copy_batch_size=DATA_NORM_CONFIG["copy_batch_size"],
                    enable_trading_day_alignment=DATA_NORM_CONFIG["enable_trading_day_alignment"]
                )
                
                if success_step1:
                    logger.info("Step 1: 市场数据因子工程任务执行成功。")
                else:
                    logger.error("Step 1: 市场数据因子工程任务执行失败。")
                    if args.step == 'normalize':
                        logger.info("由于只执行因子工程步骤，程序退出。")
                        return
            
            if args.step == 'all' or args.step == 'standardize':
                logger.info(f"执行Step 2: 标准化参数生成任务（V3版本，多表合并处理），参数: {STANDARD_PARAMS_CONFIG}")
                
                # 使用命令行参数覆盖默认的skip_if_exists设置
                skip_if_exists = args.skip_if_exists if args.skip_if_exists else STANDARD_PARAMS_CONFIG["skip_if_exists"]
                
                success_step2 = scheduler.data_manager.run_standardization_parameter_generation(
                    source_table=STANDARD_PARAMS_CONFIG["source_table"],
                    start_date=STANDARD_PARAMS_CONFIG["start_date"],
                    end_date=STANDARD_PARAMS_CONFIG["end_date"],
                    data_format=STANDARD_PARAMS_CONFIG["data_format"],
                    mad_multiplier=STANDARD_PARAMS_CONFIG["mad_multiplier"],
                    min_samples=STANDARD_PARAMS_CONFIG["min_samples"],
                    batch_size=STANDARD_PARAMS_CONFIG["batch_size"],
                    save_format=STANDARD_PARAMS_CONFIG["save_format"],
                    skip_if_exists=skip_if_exists,
                    table_name_prefix=STANDARD_PARAMS_CONFIG.get("table_name_prefix"),  # 🚀 V3新增：自定义表名前缀
                    factors_per_batch=STANDARD_PARAMS_CONFIG.get("factors_per_batch", 10)  # 🚀 修复内存溢出：控制因子批次大小
                )
                
                if success_step2:
                    logger.info("Step 2: 标准化参数生成任务执行成功。")
                else:
                    logger.error("Step 2: 标准化参数生成任务执行失败。")
            
            if args.step == 'all' or args.step == 'label':
                logger.info(f"执行Step 4: 标签生成任务，参数: {LABEL_GENERATION_CONFIG}")
                
                # 支持年度分批处理
                success_step4 = scheduler.data_manager.run_label_generation(
                    label_shift=LABEL_GENERATION_CONFIG["label_shift"],
                    corr_window=LABEL_GENERATION_CONFIG["corr_window"],
                    corr_rank_num=LABEL_GENERATION_CONFIG["corr_rank_num"],
                    min_rank_num=LABEL_GENERATION_CONFIG["min_rank_num"],
                    use_db_pct_change=LABEL_GENERATION_CONFIG["use_db_pct_change"],
                    correlation_type=LABEL_GENERATION_CONFIG["correlation_type"],
                    overlap_days=LABEL_GENERATION_CONFIG["overlap_days"],
                    start_date="2024-01-01",   # 指定开始日期
                    # start_date=None,             # 使用当前日期作为开始日期
                    end_date=None,             # 使用当前日期作为结束日期
                    batch_freq="YE",            # 年度分批处理，改为"Q"为季度
                    strategies=LABEL_GENERATION_CONFIG["strategies"]
                )
                
                if success_step4:
                    logger.info("Step 4: 标签生成任务执行成功。")
                else:
                    logger.error("Step 4: 标签生成任务执行失败。")
            
            if args.step == 'all' or args.step == 'forbid':
                logger.info(f"执行Step 5: 禁投池生成任务，参数: {FORBID_POOL_CONFIG}")
                
                success_step5 = scheduler.data_manager.run_forbid_pool_generation(
                    start_date=FORBID_POOL_CONFIG["start_date"],
                    end_date=None,  # 默认到今天
                    table_name=FORBID_POOL_CONFIG["table_name"],
                    ipo_days=FORBID_POOL_CONFIG["ipo_days"],
                    st_lookback_days=FORBID_POOL_CONFIG["st_lookback_days"],
                    stock_code_prefixes=FORBID_POOL_CONFIG["stock_code_prefixes"],
                    overlap_days=FORBID_POOL_CONFIG["overlap_days"]
                )
                
                if success_step5:
                    logger.info("Step 5: 禁投池生成任务执行成功。")
                else:
                    logger.error("Step 5: 禁投池生成任务执行失败。")
            
            logger.info("任务执行完成，程序退出。")

    except Exception as e:
        logger.error(f"调度器主流程发生严重错误: {str(e)}", exc_info=True)

    logger.info("--- 调度器运行结束 ---")

if __name__ == "__main__":
    main() 