import schedule
import time
import logging
from datetime import datetime, timedelta
from typing import Union, List
from src.tasks.market_price_norm_data_initialization import MarketPriceNormDataTask
from src.tasks.standardization_parameter_generation import StandardParamsGenerator
from src.tasks.label_generation_task import LabelGenerationTask
from src.tasks.forbid_pool_generation_task import ForbidPoolGenerationTask
from src.utils.logger import setup_logger # Assuming this utility exists
from tqdm import tqdm
import pandas as pd

logger = setup_logger(__name__)

class DataPipelineManager:
    """Manages data processing tasks for the strategy."""
    def __init__(self):
        logger.info("DataPipelineManager initialized.")
        # Future: Initialize DB connections or load configurations if needed

    def run_market_data_factor_engineering(self, processing_mode="factor_engineering", lookback_periods=100, days_count=1, start_date="2002-01-01", overlap_days=100, force_update=False, table_name="inter_train_factors_mkt_processed_v1", field_batch_size=12, field_window_config=None, use_parallel=True, upsert_batch_rows=10_000_000, copy_batch_size=200_000, enable_trading_day_alignment=True):
        """
        Runs Step 1: Fetch, process via factor engineering, and store market data.
        Handles both initialization and daily updates automatically via MarketPriceNormDataTask.
        
        Args:
            processing_mode: 处理模式，默认"factor_engineering"
            lookback_periods: 回看期数（交易日），用于计算历史数据获取，默认100个交易日
            days_count: 时间粒度（天数），默认1
            start_date: 开始日期（仅在初始化时使用），默认"2002-01-01"
            overlap_days: 更新时需要重叠的天数，默认100天
            force_update: 是否强制更新，即使数据看起来是最新的，默认为False
            table_name: 目标表名，默认"inter_train_factors_mkt_processed_v1"
            field_batch_size: 字段批次大小，用于减少内存占用，默认为12（🚀 性能优化）
            field_window_config: 字段窗口配置，字典类型，默认None（使用默认配置）
            use_parallel: 是否使用并行处理，默认True（🚀 性能优化）
            upsert_batch_rows: UPSERT批次行数，默认1000万行（🚀 性能优化）
            copy_batch_size: COPY批次大小，默认20万行（🚀 性能优化）
            enable_trading_day_alignment: 是否启用交易日对齐功能，默认True
        """
        logger.info(f"Starting Step 1: Market data factor engineering task (processing_mode={processing_mode}, lookback_periods={lookback_periods}, days_count={days_count}, table_name='{table_name}')")
        logger.info(f"Performance optimization config: field_batch_size={field_batch_size}, use_parallel={use_parallel}, upsert_batch_rows={upsert_batch_rows}, copy_batch_size={copy_batch_size}")
        
        try:
            task = MarketPriceNormDataTask(
                start_date=start_date,
                processing_mode=processing_mode,
                field_window_config=field_window_config,
                lookback_periods=lookback_periods,
                days_count=days_count,
                table_name=table_name,
                field_batch_size=field_batch_size,
                use_parallel=use_parallel,
                enable_trading_day_alignment=enable_trading_day_alignment,
                # 🚀 传递优化的批次配置
                batch_size=copy_batch_size
            )
            
            # 🚀 设置UPSERT批次大小
            if hasattr(task, 'upsert_batch_rows'):
                task.upsert_batch_rows = upsert_batch_rows
            if hasattr(task, 'copy_batch_size'):
                task.copy_batch_size = copy_batch_size
            
            success = task.execute(overlap_days=overlap_days, force_update=force_update)
            
            if success:
                logger.info("Step 1: Market data factor engineering completed successfully")
            else:
                logger.error("Step 1: Market data factor engineering failed")
                
            return success
            
        except Exception as e:
            logger.error(f"Step 1: Market data factor engineering failed with exception: {str(e)}", exc_info=True)
            return False

    def run_market_data_normalization(self, lag=30, days_count=1, start_date="2002-01-01", overlap_days=20, force_update=False, table_suffix="", field_batch_size=5, z_window_map=None, clip_window=None, clip_number=5.0):
        """
        Runs Step 1: Fetch, normalize (academic method), and store market data.
        [DEPRECATED] This method is kept for backward compatibility. Use run_market_data_factor_engineering instead.
        """
        logger.warning("run_market_data_normalization is deprecated. Use run_market_data_factor_engineering instead.")
        return self.run_market_data_factor_engineering(
            processing_mode="academic",
            lookback_periods=lag,
            days_count=days_count,
            start_date=start_date,
            overlap_days=overlap_days,
            force_update=force_update,
            table_name=f"inter_train_factors_mkt_norm_academic{table_suffix}",
            field_batch_size=field_batch_size,
            field_window_config=z_window_map
        )

    def run_standardization_parameter_generation(self, source_table: Union[str, List[str]] = "inter_train_factors_mkt_processed_v1", 
                                               start_date: str = "2002-01-01", end_date: str = "2012-12-31", 
                                               data_format: str = 'long', mad_multiplier: float = 7.0, min_samples: int = 1000, 
                                               batch_size: int = 50, save_format: str = 'database', skip_if_exists: bool = False,
                                               table_name_prefix: str = None, factors_per_batch: int = 10):
        """
        Runs Step 2: Generate standardization parameters from long table data - V3版本
        Uses data from the specified date range (default 2002-01-01 to 2012-12-31) to compute parameters.
        🚀 新版本：将多个源表的数据合并，生成一个统一的标准化参数表
        
        Args:
            source_table: 源数据表名或表名列表，默认"inter_train_factors_mkt_processed_v1"
            start_date: 开始日期，格式为YYYY-MM-DD，默认"2002-01-01"
            end_date: 结束日期，格式为YYYY-MM-DD，默认"2012-12-31"
            data_format: 数据格式，'long'（长表）或'wide'（宽表），默认'long'
            mad_multiplier: MAD乘数，用于异常值检测，默认7.0
            min_samples: 每个特征组合的最小样本数，少于此数量将设为NaN，默认1000
            batch_size: 每批处理的特征组合数量，默认50
            save_format: 保存格式，'database'或'csv'，默认'database'
            skip_if_exists: 如果标准化参数表已存在，是否跳过处理，默认为False
            table_name_prefix: 自定义表名前缀，如果为None则自动生成，默认None
            factors_per_batch: 大窗口内因子分批处理时每批的因子数量，避免内存溢出，默认10
            
        Returns:
            bool: 任务是否成功执行
        """
        # 🚀 重构：无论单表还是多表，都统一处理
        logger.info(f"Starting Step 2: Standardization parameter generation task (V3版本，多表合并处理) "
                    f"(date range={start_date} to {end_date}, source_table={source_table})...")
        logger.info(f"配置参数: min_samples={min_samples}, batch_size={batch_size}, data_format={data_format}")
        
        try:
            # 创建标准化参数生成器实例 - V3版本，支持多表
            generator = StandardParamsGenerator(
                start_date=start_date,
                end_date=end_date,
                source_table=source_table,  # 可以是单个表名或列表
                data_format=data_format,
                mad_multiplier=mad_multiplier,
                min_samples=min_samples,
                batch_size=batch_size,
                save_format=save_format,
                skip_if_exists=skip_if_exists,
                table_name_prefix=table_name_prefix,  # 🚀 新增：自定义表名前缀
                factors_per_batch=factors_per_batch  # 🚀 修复内存溢出：控制每批处理的因子数量
            )

            # 执行任务
            success = generator.execute()

            if success:
                if save_format == 'database':
                    logger.info(f"Step 2: Standardization parameter generation task completed successfully. "
                               f"Parameters saved to database table {generator.params_table_name}.")
                else:
                    logger.info(f"Step 2: Standardization parameter generation task completed successfully. "
                               f"Parameters saved to CSV file.")
            else:
                logger.error("Step 2: Standardization parameter generation task failed.")
            return success
        except Exception as e:
            logger.exception(f"An error occurred during Step 2 (Standardization parameter generation): {e}")
            return False
    


    def run_final_data_standardization(self):
        """Placeholder for Step 3: Apply standardization to get training data."""
        logger.info("Executing Step 3: Final data standardization (placeholder)...")
        print("Step 3 executed (stub).")
        # TODO: Implement logic using normalized data and standardization parameters
        return True

    def run_label_generation(self, label_shift=10, corr_window=240, corr_rank_num=30, min_rank_num=20, 
                            use_db_pct_change=True, correlation_type="pearson", overlap_days=20,
                            start_date=None, end_date=None, batch_freq=None, strategies=None):
        """
        支持分批处理和多策略的标签生成任务。
        
        Args:
            label_shift: 标签偏移天数，默认10
            corr_window: 相关性窗口大小，默认240
            corr_rank_num: 相关性排名数量，默认30
            min_rank_num: 最小排名数量，默认20
            use_db_pct_change: 是否使用数据库中的收益率数据，默认True
            correlation_type: 相关性类型，默认"pearson"
            overlap_days: 重叠天数，用于确保数据连续性，默认20
            start_date: 开始日期，默认None（自动推断为当前日期）
            end_date: 结束日期，默认None（自动推断为今天）
            batch_freq: 批处理频率，默认None（全区间处理）
            strategies: 策略列表，默认None（使用top_correlation）
        """
        logger.info(f"Starting Step 4: Label generation task (label_shift={label_shift}, corr_window={corr_window}, overlap_days={overlap_days})...")

        from src.data_service.data_loading.market_data import MarketDataProvider
        from src.tasks.label_generation_task import LabelGenerationConfig, LabelGenerationTask
        market_data_provider = MarketDataProvider()

        # 🎯 支持多策略配置
        if strategies is None:
            strategies = "top_correlation"  # 默认策略

        logger.info(f"Using strategies: {strategies}")

        # ------------------------------------------------------------------
        # 支持 label_shift 为列表，自动按顺序生成多种 shift 标签
        # ------------------------------------------------------------------
        if isinstance(label_shift, (list, tuple)):
            ok_all = True
            for shift in label_shift:
                logger.info(f"Processing shift: {shift}")
                ok = self.run_label_generation(
                    label_shift=shift,
                    corr_window=corr_window,
                    corr_rank_num=corr_rank_num,
                    min_rank_num=min_rank_num,
                    use_db_pct_change=use_db_pct_change,
                    correlation_type=correlation_type,
                    overlap_days=overlap_days,
                    start_date=start_date,
                    end_date=end_date,
                    batch_freq=batch_freq,
                    strategies=strategies,
                )
                ok_all = ok_all and ok
            return ok_all

        # 构建任务
        task = LabelGenerationTask(
            market_data_provider=market_data_provider,
            config=LabelGenerationConfig(
                strategy=strategies,  # 🎯 支持单个字符串或列表
                label_shift=label_shift,
                corr_window=corr_window,
                corr_rank_num=corr_rank_num,
                min_rank_num=min_rank_num,
                correlation_type=correlation_type,
                use_db_pct_change=use_db_pct_change,
                save_intermediate=True,
                batch_size=10000,
                overlap_days=overlap_days  # 确保传递 overlap_days 参数
            )
        )

        # 自动推断区间
        if start_date is None:
            # 如果未指定开始日期，使用当前日期
            start_date = datetime.now().strftime('%Y-%m-%d')
            logger.info(f"未指定开始日期，使用当前日期: {start_date}")
            
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')
            logger.info(f"未指定结束日期，使用当前日期: {end_date}")

        # 只有在同时指定了开始日期和结束日期，且batch_freq不为None时才进行分批处理
        if batch_freq and start_date != end_date:
            logger.info(f"使用{batch_freq}频率进行分批处理，区间: {start_date} ~ {end_date}")
            dates = pd.date_range(start=start_date, end=end_date, freq=batch_freq)
            if dates[-1].strftime('%Y-%m-%d') < end_date:
                dates = dates.append(pd.DatetimeIndex([pd.to_datetime(end_date)]))
            for i in tqdm(range(len(dates) - 1), desc="标签分批处理"):
                batch_start = dates[i].strftime('%Y-%m-%d')
                batch_end = (dates[i+1] - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
                logger.info(f"处理区间: {batch_start} ~ {batch_end}")
                try:
                    df = task.execute(start_date=batch_start, end_date=batch_end)
                    if not df.empty:
                        logger.info(f"区间 {batch_start} ~ {batch_end} 生成 {len(df)} 条标签")
                    else:
                        logger.warning(f"区间 {batch_start} ~ {batch_end} 未生成标签")
                    del df
                except Exception as e:
                    logger.error(f"区间 {batch_start} ~ {batch_end} 处理失败: {str(e)}", exc_info=True)
            logger.info("所有区间处理完成！")
            return True
        else:
            # 单区间处理
            logger.info(f"单区间处理: {start_date} ~ {end_date}")
            try:
                df = task.execute(start_date=start_date, end_date=end_date)
                if not df.empty:
                    logger.info(f"生成 {len(df)} 条标签")
                else:
                    logger.warning("未生成标签")
                return True
            except Exception as e:
                logger.error(f"处理失败: {str(e)}", exc_info=True)
                return False

    def run_forbid_pool_generation(self, 
                                 start_date: str,
                                 end_date: str = None,
                                 table_name: str = "forbid_pool_comprehensive",
                                 ipo_days: int = 122,
                                 st_lookback_days: int = 20,
                                 stock_code_prefixes: List[str] = None,
                                 overlap_days: int = 3) -> bool:
        """
        执行禁投池生成任务
        
        Args:
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期，默认为今天 (YYYY-MM-DD)
            table_name: 禁投池表名
            ipo_days: 新股上市天数阈值，小于此天数的为新股
            st_lookback_days: ST股票筛选回看天数
            stock_code_prefixes: 股票代码前缀筛选
            overlap_days: 重叠天数，用于更新模式
            
        Returns:
            bool: 执行是否成功
        """
        logger.info(f"开始执行禁投池生成任务：{start_date} 至 {end_date or '今天'}")
        
        try:
            # 初始化禁投池生成任务
            task = ForbidPoolGenerationTask(
                table_name=table_name,
                ipo_days=ipo_days,
                st_lookback_days=st_lookback_days,
                stock_code_prefixes=stock_code_prefixes
            )
            
            # 执行任务
            success = task.run(
                start_date=start_date,
                end_date=end_date,
                overlap_days=overlap_days
            )
            
            if success:
                logger.info("禁投池生成任务执行成功")
                return True
            else:
                logger.error("禁投池生成任务执行失败")
                return False
                
        except Exception as e:
            logger.error(f"执行禁投池生成任务时发生异常: {str(e)}", exc_info=True)
            return False


class TrainingManager:
    """Manages model training tasks."""
    def __init__(self):
        logger.info("TrainingManager initialized.")
        # Future: Initialize training configurations, load model architecture etc.

    def run_training_pipeline(self):
        """Placeholder for running the model training pipeline."""
        logger.info("Executing training pipeline (placeholder)...")
        # TODO: Implement loading data (from Step 3), training GRU model, saving checkpoints
        print("Training pipeline executed (stub).")
        return True


class BacktestingManager:
    """Manages backtesting and evaluation tasks."""
    def __init__(self):
        logger.info("BacktestingManager initialized.")
        # Future: Initialize backtesting configurations, load trained models etc.

    def run_backtest(self):
        """Placeholder for running the backtesting process."""
        logger.info("Executing backtest (placeholder)...")
        # TODO: Implement loading model, loading prediction data, running simulation, generating reports
        print("Backtest executed (stub).")
        return True


class DfzqGruScheduler:
    """
    Main scheduler to orchestrate data, training, and backtesting tasks
    for the Dfzq GRU strategy.
    """
    def __init__(self):
        logger.info("Initializing DfzqGruScheduler...")
        self.data_manager = DataPipelineManager()
        self.training_manager = TrainingManager()
        self.backtesting_manager = BacktestingManager()
        logger.info("DfzqGruScheduler initialized.")

    def run_daily_data_pipeline(self, skip_if_exists=False, force_update=False):
        """Runs the daily data pipeline tasks in sequence."""
        logger.info(f"Starting daily data pipeline job at {datetime.now()}...")

        # Step 1: Market Data Factor Engineering (🚀 使用优化的性能参数)
        # This step needs to run daily to keep the base factor engineering data up-to-date.
        success_step1 = self.data_manager.run_market_data_factor_engineering(
            processing_mode="factor_engineering",
            lookback_periods=100, 
            days_count=1,
            force_update=force_update,
            # 🚀 使用优化的性能参数
            field_batch_size=12,
            use_parallel=True,
            upsert_batch_rows=10_000_000,
            copy_batch_size=200_000
        )
        if not success_step1:
            logger.error("Daily data pipeline failed at Step 1: Market data factor engineering.")
            return # Stop pipeline if a critical step fails

        # Step 2: Standardization Parameter Generation (V3版本，多表合并处理)
        # Note: In production, you might want to run this less frequently, perhaps monthly or quarterly
        # For now, we'll add it to the daily pipeline but it could be moved to a separate schedule
        success_step2 = self.data_manager.run_standardization_parameter_generation(
            source_table="inter_train_factors_mkt_processed_v3",  # 🚀 更新为新的表名
            start_date="2010-01-01",  # 🚀 更新时间范围
            end_date="2018-12-31",
            data_format="long",
            mad_multiplier=8.0,  # 🚀 更新MAD乘数
            min_samples=1000,
            batch_size=25,  # 🚀 调整批次大小
            skip_if_exists=skip_if_exists,
            table_name_prefix=None  # 🚀 V3新增：使用默认表名前缀
        )
        if not success_step2:
            logger.error("Daily data pipeline failed at Step 2: Standardization parameter generation.")
            return
            
        # Step 4: Label Generation
        # This step needs to run daily to keep the labels up-to-date
        success_step4 = self.data_manager.run_label_generation(
            label_shift=10,
            corr_window=240,
            corr_rank_num=30,
            min_rank_num=20,
            use_db_pct_change=True,
            correlation_type="pearson",
            overlap_days=20,
            strategies="top_correlation"  # 🎯 默认使用top_correlation策略
        )
        if not success_step4:
            logger.error("Daily data pipeline failed at Step 4: Label generation.")
            return

        # --- Add subsequent data steps here when implemented ---
        # success_step3 = self.data_manager.run_final_data_standardization()
        # if not success_step3:
        #     logger.error("Daily data pipeline failed at Step 3.")
        #     return

        logger.info(f"Daily data pipeline job finished successfully at {datetime.now()}.")

    def schedule_tasks(self, force_update=False):
        """
        Schedules the recurring tasks.
        
        Args:
            force_update: 是否强制更新数据，即使数据看起来是最新的，默认为False
        """
        logger.info("Scheduling daily tasks...")

        # Schedule the daily data pipeline to run at 11 PM (23:00)
        schedule.every().day.at("23:00").do(self.run_daily_data_pipeline, force_update=force_update)
        logger.info(f"Daily data pipeline scheduled for 23:00 (force_update={force_update}).")

        # --- Add schedules for training, backtesting etc. later if needed ---
        # Example: Run training weekly on Mondays at 1 AM
        # schedule.every().monday.at("01:00").do(self.training_manager.run_training_pipeline)
        # logger.info("Weekly training pipeline scheduled for Mondays at 01:00.")

    def run_continuously(self, force_update=False):
        """
        Starts the scheduler and runs it continuously.
        
        Args:
            force_update: 是否强制更新数据，即使数据看起来是最新的，默认为False
        """
        self.schedule_tasks(force_update=force_update)
        logger.info("Scheduler started. Running pending jobs and waiting...")
        # Run once immediately if scheduled time has passed for today? Optional.
        # schedule.run_all() # Uncomment to run all jobs once at startup

        while True:
            schedule.run_pending()
            time.sleep(60) # Check for pending jobs every 60 seconds

# This block allows running the scheduler directly for testing or as the main entry point.
# In production, you might instantiate DfzqGruScheduler and call run_continuously()
# from a main script like init_data.py or a service manager.
if __name__ == "__main__":
    print("Starting DfzqGruScheduler directly...")
    logger.info("Instantiating DfzqGruScheduler for standalone execution.")
    scheduler = DfzqGruScheduler()

    # --- Option 1: Run the daily data task once immediately for testing ---
    print("Running the daily data pipeline once for testing...")
    scheduler.run_daily_data_pipeline()
    print("Test run finished.")

    # --- Option 2: Run the scheduler continuously ---
    # print("Starting the scheduler to run continuously...")
    # scheduler.run_continuously()
