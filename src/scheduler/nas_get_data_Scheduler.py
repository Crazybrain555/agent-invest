import schedule
import time
import traceback
import logging
from datetime import datetime
from src.tasks.nas_forbid_data_task import NASForbidDataTask
from src.utils.config_loader import ConfigLoader

# Configure logging for the scheduler
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# 默认NAS路径，可以被构造函数的nas_path覆盖
DEFAULT_NAS_PATH = r'\\space\forbid'

class NASDataScheduler:
    """
    Scheduler specifically for running the NAS data fetching tasks (e.g., Forbid Pool).
    Uses the 'schedule' library for simple time-based scheduling.
    """
    def __init__(self, nas_path=None):
        """
        Initializes the scheduler, loads configuration, and prepares the task.
        
        Args:
            nas_path (str, optional): 完整的NAS路径。如果提供，将直接使用此路径而不依赖配置文件中的base_path。
        """
        logger.info("Initializing NASDataScheduler...")
        try:
            # Load scheduler configuration
            config_loader = ConfigLoader(config_dir='configs')
            cfg = config_loader.load_config("nas_disk/nas_config.yaml")
            scheduler_cfg = cfg.get('scheduler', {})
            schedule_time_cfg = scheduler_cfg.get('schedule', {})

            self.run_hour = schedule_time_cfg.get('hour', 1) # Default to 1 AM
            self.run_minute = schedule_time_cfg.get('minute', 0) # Default to :00
            self.run_time_str = f"{self.run_hour:02d}:{self.run_minute:02d}"
            self.is_enabled = scheduler_cfg.get('enabled', False) # Default to disabled

            if not self.is_enabled:
                 logger.warning("NASDataScheduler is disabled in the configuration.")
                 return
            
            # 使用传入的NAS路径或默认路径
            self.nas_path = nas_path or DEFAULT_NAS_PATH

            # 实例化任务，传递NAS路径
            self.task = NASForbidDataTask(nas_path=self.nas_path)
            logger.info(f"NASDataScheduler initialized. Using NAS path: {self.nas_path}. Task will run daily at {self.run_time_str}.")

        except Exception as e:
            logger.error(f"Failed to initialize NASDataScheduler: {e}", exc_info=True)
            self.is_enabled = False # Disable if init fails
            raise

    def job(self):
        """
        The job function that the scheduler executes.
        It runs the NASForbidDataTask, defaulting to today's date as end_date.
        """
        logger.info(f"NAS Scheduler job starting at {datetime.now()}...")
        try:
            # Run the task, defaulting to today. The task itself handles finding the latest if None.
            success = self.task.run() # Task defaults end_date to latest available
            if success:
                logger.info("NAS Scheduler job completed successfully.")
            else:
                 logger.warning("NAS Scheduler job finished, but task reported failure (check task logs).")
        except Exception as e:
            logger.error(f"NAS Scheduler job failed with an exception: {e}", exc_info=True)
            # Depending on requirements, might add notifications here (e.g., email, Slack)

    def schedule_tasks(self):
        """
        Sets up the daily schedule using the configured run time.
        """
        if not self.is_enabled:
            logger.info("Scheduling skipped as NASDataScheduler is disabled.")
            return

        logger.info(f"Scheduling NAS forbid data task to run daily at {self.run_time_str}.")
        schedule.clear() # Clear any existing schedules from this instance
        schedule.every().day.at(self.run_time_str).do(self.job)
        logger.info("Task successfully scheduled.")

    def run_continuously(self, interval=30):
        """
        Starts the scheduling loop, checking for pending jobs periodically.

        Args:
            interval (int): How often (in seconds) to check for pending jobs.
        """
        if not self.is_enabled:
            logger.warning("Scheduler not starting as it is disabled.")
            return

        self.schedule_tasks()
        logger.info("Starting continuous scheduler loop...")
        while True:
            try:
                schedule.run_pending()
                time.sleep(interval)
            except KeyboardInterrupt:
                logger.info("Scheduler stopped manually.")
                break
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}", exc_info=True)
                # Avoid continuous crashing, wait longer before retrying loop
                time.sleep(interval * 2)

# Example of how to run the scheduler directly
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Running NASDataScheduler directly...")
    scheduler = NASDataScheduler()
    # Optional: Run the job once immediately for testing before starting the loop
    # logger.info("Running job once immediately for testing...")
    # scheduler.job()
    # logger.info("Test job run finished.")
    scheduler.run_continuously() 