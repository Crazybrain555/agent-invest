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

# Configure logging
logger = setup_logger(__name__) # The function already sets INFO level by default

# --- Configuration ---
DATA_NORM_CONFIG = {
    "lag": 30,                 # Lag features
    "days_count": 1,           # Daily data
    "start_date": "2002-01-01", # Start from the beginning
    "overlap_days": 20,         # Restored to normal value after fixing duplication issue
    "force_update": False       # Whether to force update even if data appears to be up-to-date
}

# 标准化参数生成配置 - V2版本，支持长表处理
STANDARD_PARAMS_CONFIG = {
    "source_table": "inter_train_factors_mkt_processed_v3",  # 源数据表名（长表）
    # 🚀 新增：支持多个表的列表形式
    # "source_table": ["inter_train_factors_mkt_processed_v3", "inter_train_factors_mkt_processed_v2"],  # 支持多个表
    "start_date": "2010-01-01",     # Start date
    "end_date": "2018-12-31",       # End date
    "data_format": "long",          # Data format - 长表格式
    "mad_multiplier": 8.0,          # MAD multiplier
    "min_samples": 1000,            # 最小样本数，少于此数量将设为NaN
    "batch_size": 25,               # 每批处理的特征组合数量
    "save_format": "database",      # Save format
    "skip_if_exists": True          # Skip if exists
}

# 标签生成配置
LABEL_GENERATION_CONFIG = {
    "label_shift": 10,           # 未来收益率的天数
    "corr_window": 240,          # 相关性计算窗口
    "corr_rank_num": 30,         # 相关性排名数量
    "min_rank_num": 20,          # 最小邻居数量
    "use_db_pct_change": True,   # 是否使用数据库中的百分比变化
    "correlation_type": "pearson", # 相关性类型
    "overlap_days": 20           # 数据重叠天数
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
    parser.add_argument('--step', type=str, choices=['all', 'normalize', 'standardize', 'label'], default='all',
                      help='Which step to run: all, normalize (Step 1), standardize (Step 2), or label (Step 4)')
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
                logger.info(f"执行Step 1: 市场行情数据标准化任务，参数: {DATA_NORM_CONFIG}")
                success_step1 = scheduler.data_manager.run_market_data_normalization(
                    lag=DATA_NORM_CONFIG["lag"], 
                    days_count=DATA_NORM_CONFIG["days_count"],
                    start_date=DATA_NORM_CONFIG["start_date"],
                    overlap_days=DATA_NORM_CONFIG["overlap_days"],
                    force_update=DATA_NORM_CONFIG["force_update"]
                )
                
                if success_step1:
                    logger.info("Step 1: 市场数据归一化任务执行成功。")
                else:
                    logger.error("Step 1: 市场数据归一化任务执行失败。")
                    if args.step == 'normalize':
                        logger.info("由于只执行归一化步骤，程序退出。")
                        return
            
            if args.step == 'all' or args.step == 'standardize':
                logger.info(f"执行Step 2: 标准化参数生成任务，参数: {STANDARD_PARAMS_CONFIG}")
                
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
                    skip_if_exists=skip_if_exists
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
                    # start_date="2002-01-01",   # 指定开始日期
                    start_date=None,             # 使用当前日期作为开始日期
                    end_date=None,             # 使用当前日期作为结束日期
                    batch_freq="YE"            # 年度分批处理，改为"Q"为季度
                )
                
                if success_step4:
                    logger.info("Step 4: 标签生成任务执行成功。")
                else:
                    logger.error("Step 4: 标签生成任务执行失败。")
            
            logger.info("任务执行完成，程序退出。")

    except Exception as e:
        logger.error(f"调度器主流程发生严重错误: {str(e)}", exc_info=True)

    logger.info("--- 调度器运行结束 ---")

if __name__ == "__main__":
    main() 