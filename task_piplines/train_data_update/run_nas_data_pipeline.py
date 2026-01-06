import logging
import os
import sys
import argparse
import time
import schedule
from datetime import datetime, timedelta
import traceback

# Ensure the project root is in the path
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.append(project_root)

from src.tasks.nas_forbid_data_task import NASForbidDataTask
from src.scheduler.nas_get_data_Scheduler import NASDataScheduler
from src.utils.config_loader import ConfigLoader

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"nas_pipeline_{datetime.now().strftime('%Y%m%d')}.log")
    ]
)
logger = logging.getLogger("nas_pipeline")

# 直接定义完整的NAS路径，不依赖配置文件中的base_path
NAS_FORBID_PATH = r'\\space\forbid'  # 禁投池数据的完整UNC路径

class NASDataPipeline:
    """
    Pipeline for managing NAS data loading tasks.
    Supports initialization mode (loading all historical data) and scheduled mode.
    """
    
    def __init__(self):
        """Initialize the NAS data pipeline"""
        logger.info("Initializing NAS Data Pipeline...")
        try:
            # Load configuration
            self.config_loader = ConfigLoader(config_dir='configs')
            self.cfg = self.config_loader.load_config("nas_disk/nas_config.yaml")
            
            # Extract configuration values
            self.loader_cfg = self.cfg.get('loader', {})
            self.scheduler_cfg = self.cfg.get('scheduler', {})
            self.schedule_time = f"{self.scheduler_cfg.get('schedule', {}).get('hour', 1):02d}:{self.scheduler_cfg.get('schedule', {}).get('minute', 0):02d}"
            
            # 初始化任务，传入NAS路径
            self.task = NASForbidDataTask(nas_path=NAS_FORBID_PATH)
            logger.info("NAS Data Pipeline initialized successfully.")
            
        except Exception as e:
            logger.error(f"Failed to initialize NAS Data Pipeline: {e}", exc_info=True)
            raise
    
    def run_initialization(self, start_date=None, end_date=None, batch_size=None):
        """
        Run in initialization mode to load all historical data.
        
        Args:
            start_date (str): Optional start date in YYYYMMDD format. If None, uses earliest available.
            end_date (str): Optional end date in YYYYMMDD format. If None, uses latest available.
            batch_size (int): Optional batch size for loading data in chunks to avoid memory issues.
        """
        logger.info(f"Running NAS Data Pipeline in INITIALIZATION mode.")
        logger.info(f"Parameters: start_date={start_date}, end_date={end_date}, batch_size={batch_size}")
        
        try:
            # Get all available dates from the loader
            all_dates = self.task.loader.list_available_dates()
            if not all_dates:
                logger.warning("No data files found on NAS. Initialization aborted.")
                return False
            
            # Determine effective date range
            effective_start = start_date or min(all_dates)
            effective_end = end_date or max(all_dates)
            logger.info(f"Effective date range: {effective_start} to {effective_end}")
            
            # Filter dates within the range
            target_dates = [d for d in all_dates if d >= effective_start and d <= effective_end]
            target_dates.sort()
            logger.info(f"Found {len(target_dates)} dates to process.")
            
            if not target_dates:
                logger.warning("No dates within the specified range. Initialization aborted.")
                return False
            
            # Process all at once if batch_size is None or invalid
            if not batch_size or batch_size <= 0:
                logger.info("Processing all dates in a single batch...")
                return self.task.run(start_date_str=effective_start, end_date_str=effective_end, is_init_mode=True)
            
            # Process in batches
            logger.info(f"Processing in batches of {batch_size} days...")
            success = True
            
            for i in range(0, len(target_dates), batch_size):
                batch_dates = target_dates[i:i+batch_size]
                batch_start = batch_dates[0]
                batch_end = batch_dates[-1]
                logger.info(f"Processing batch {i//batch_size + 1}/{(len(target_dates) + batch_size - 1)//batch_size}: {batch_start} to {batch_end}")
                
                batch_success = self.task.run(
                    start_date_str=batch_start, 
                    end_date_str=batch_end,
                    is_init_mode=True
                )
                
                if not batch_success:
                    logger.error(f"Batch {i//batch_size + 1} failed. Continuing with next batch...")
                    success = False
            
            if success:
                logger.info("Initialization completed successfully.")
            else:
                logger.warning("Initialization completed with some errors.")
            
            return success
            
        except Exception as e:
            logger.error(f"Initialization failed: {e}", exc_info=True)
            return False
    
    def run_latest(self, overlap_days=None):
        """
        Run the pipeline for the latest data.
        
        Args:
            overlap_days (int): Optional override for the number of days to overlap.
                                If None, uses the value from configuration.
        """
        logger.info("Running NAS Data Pipeline for LATEST data.")
        
        try:
            # Use the configured task directly
            if overlap_days is not None:
                # If overlap_days is specified, save the original and restore it after
                original_overlap = self.task.overlap_days
                self.task.overlap_days = overlap_days
                logger.info(f"Temporarily overriding overlap_days to {overlap_days}")
                
                try:
                    result = self.task.run()  # Uses default params (latest end date)
                finally:
                    # Restore original overlap_days
                    self.task.overlap_days = original_overlap
                    logger.info(f"Restored overlap_days to {original_overlap}")
            else:
                # Use default overlap_days
                result = self.task.run()  # Uses default params (latest end date)
                
            return result
            
        except Exception as e:
            logger.error(f"Latest data processing failed: {e}", exc_info=True)
            return False
    
    def run_specific_date(self, date_str, is_end_date=True):
        """
        Run the pipeline for a specific date.
        
        Args:
            date_str (str): The date in YYYYMMDD format.
            is_end_date (bool): If True, use as end_date. If False, use as both start and end date.
        """
        logger.info(f"Running NAS Data Pipeline for specific date: {date_str} (as {'end' if is_end_date else 'exact'} date)")
        
        try:
            if is_end_date:
                # Use as end date with default overlap behavior
                return self.task.run(end_date_str=date_str)
            else:
                # Use as both start and end date to process exactly one day
                return self.task.run(start_date_str=date_str, end_date_str=date_str)
                
        except Exception as e:
            logger.error(f"Specific date processing failed: {e}", exc_info=True)
            return False
    
    def run_date_range(self, start_date, end_date, batch_size=None):
        """
        Run the pipeline for a specific date range.
        
        Args:
            start_date (str): Start date in YYYYMMDD format.
            end_date (str): End date in YYYYMMDD format.
            batch_size (int): Optional batch size for processing in chunks.
        """
        logger.info(f"Running NAS Data Pipeline for date range: {start_date} to {end_date}")
        
        if batch_size and batch_size > 0:
            return self.run_initialization(start_date, end_date, batch_size)
        else:
            return self.task.run(start_date_str=start_date, end_date_str=end_date)
    
    def run_scheduled(self, custom_time=None, run_once_now=False):
        """
        Run the pipeline in scheduled mode.
        
        Args:
            custom_time (str): Optional custom time in HH:MM format. If None, uses config.
            run_once_now (bool): If True, also runs the pipeline immediately.
        """
        logger.info("Starting NAS Data Pipeline in SCHEDULED mode.")
        
        # Use the scheduler implementation with NAS path
        scheduler = NASDataScheduler(nas_path=NAS_FORBID_PATH)
        
        if not scheduler.is_enabled:
            logger.warning("Scheduler is disabled in configuration. Enabling temporarily.")
            scheduler.is_enabled = True
        
        # Override schedule time if specified
        if custom_time:
            try:
                hour, minute = map(int, custom_time.split(':'))
                scheduler.run_hour = hour
                scheduler.run_minute = minute
                scheduler.run_time_str = f"{hour:02d}:{minute:02d}"
                logger.info(f"Overriding schedule time to {scheduler.run_time_str}")
            except (ValueError, AttributeError) as e:
                logger.error(f"Invalid custom time format '{custom_time}'. Using default: {scheduler.run_time_str}")
        
        # Run once immediately if requested
        if run_once_now:
            logger.info("Running job once immediately...")
            scheduler.job()
            logger.info("Immediate job execution completed.")
        
        # Start the scheduler
        logger.info(f"Scheduler will run daily at {scheduler.run_time_str}")
        scheduler.run_continuously()
        
        # This point is never reached unless run_continuously() is interrupted

def main():
    """Main entry point for the NAS data pipeline script."""
    
    parser = argparse.ArgumentParser(description='NAS Data Pipeline Runner')
    
    # Mode selection
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument('--init', action='store_true', help='Run in initialization mode (load all historical data)')
    mode_group.add_argument('--latest', action='store_true', help='Run for latest data only')
    mode_group.add_argument('--date', type=str, help='Run for specific date (YYYYMMDD format)')
    mode_group.add_argument('--range', nargs=2, metavar=('START_DATE', 'END_DATE'), help='Run for specific date range (YYYYMMDD format)')
    mode_group.add_argument('--schedule', action='store_true', help='Run in scheduled mode')
    
    # Additional options
    parser.add_argument('--batch-size', type=int, help='Batch size for processing large datasets (number of days per batch)')
    parser.add_argument('--overlap', type=int, help='Override the default overlap days for latest mode')
    parser.add_argument('--schedule-time', type=str, help='Custom schedule time (HH:MM format) for scheduled mode')
    parser.add_argument('--run-now', action='store_true', help='Run once immediately when in scheduled mode')
    parser.add_argument('--exact-date', action='store_true', help='For --date mode, use date as both start and end (no overlap)')
    
    args = parser.parse_args()
    
    try:
        # Initialize the pipeline
        pipeline = NASDataPipeline()
        
        # Run in the appropriate mode
        if args.init:
            logger.info("=== Running in INITIALIZATION mode ===")
            result = pipeline.run_initialization(batch_size=args.batch_size)
        
        elif args.latest:
            logger.info("=== Running for LATEST data ===")
            result = pipeline.run_latest(overlap_days=args.overlap)
        
        elif args.date:
            logger.info(f"=== Running for SPECIFIC DATE: {args.date} ===")
            result = pipeline.run_specific_date(args.date, not args.exact_date)
        
        elif args.range:
            logger.info(f"=== Running for DATE RANGE: {args.range[0]} to {args.range[1]} ===")
            result = pipeline.run_date_range(args.range[0], args.range[1], args.batch_size)
        
        elif args.schedule:
            logger.info("=== Running in SCHEDULED mode ===")
            pipeline.run_scheduled(args.schedule_time, args.run_now)
            # run_scheduled() will run continuously and won't return unless interrupted
            result = True
        
        # Report results
        if result:
            logger.info("Pipeline execution SUCCEEDED")
            return 0
        else:
            logger.error("Pipeline execution FAILED")
            return 1
            
    except Exception as e:
        logger.error(f"Pipeline execution failed with exception: {e}", exc_info=True)
        return 1

if __name__ == "__main__":
    sys.exit(main()) 