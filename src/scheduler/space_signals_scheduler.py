"""
Space Signals 定时调度器

功能：
- 每天定时运行 signals 入库任务
- 从配置文件读取调度时间
- 支持回溯天数配置
- 错误重试机制
"""

import logging
import schedule
import time
from datetime import datetime, timedelta
from typing import Optional

from src.utils.config_loader import ConfigLoader
from src.tasks.space_signals_ingest import SpaceSignalsIngest

logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _dateint_today() -> int:
    """获取今天的日期（YYYYMMDD 格式）"""
    return int(datetime.now().strftime("%Y%m%d"))


def _dateint_from_range_days(n: int) -> int:
    """
    计算回溯日期
    
    Args:
        n: 回溯天数
    
    Returns:
        日期（YYYYMMDD 格式）
    """
    d = datetime.now() - timedelta(days=n)
    return int(d.strftime("%Y%m%d"))


class SpaceSignalsScheduler:
    """
    Space Signals 定时调度器
    
    功能：
    - 定时执行 signals 入库任务
    - 从配置文件读取调度参数
    - 支持错误重试
    """
    
    def __init__(self, config_path: str = "configs/space_disk/space_config.yaml"):
        """
        初始化调度器
        
        Args:
            config_path: 配置文件路径
        """
        self.config_loader = ConfigLoader(config_dir='configs')
        self.config = self.config_loader.load_config("space_disk/space_config.yaml")
        
        # 调度器配置
        self.scheduler_config = self.config.get('scheduler') or {}
        self.enabled = self.scheduler_config.get('enabled', True)
        
        # 调度时间配置
        schedule_time = self.scheduler_config.get('schedule') or {}
        self.hour = schedule_time.get('hour', 1)
        self.minute = schedule_time.get('minute', 0)
        
        # 数据加载配置
        loader_config = self.config.get('loader') or {}
        self.overlap_days = loader_config.get('overlap_days', 20)
        
        # 重试配置
        self.max_retries = self.scheduler_config.get('max_job_retries', 3)
        self.retry_delay = self.scheduler_config.get('job_retry_delay', 300)  # 秒
        
        # 因子映射路径
        self.mapping_path = 'configs/field_mappings/factor_mapping.yaml'
        
        logger.info(f"Scheduler initialized: enabled={self.enabled}, "
                   f"schedule={self.hour:02d}:{self.minute:02d}, "
                   f"overlap_days={self.overlap_days}")
    
    def run_daily_task(self):
        """
        运行每日任务
        
        处理最近 overlap_days 天的 signals 数据
        """
        start_time = datetime.now()
        logger.info(f"{'='*80}")
        logger.info(f"Starting daily Space signals ingestion task at {start_time}")
        logger.info(f"{'='*80}")
        
        # 计算日期范围
        start_date = _dateint_from_range_days(self.overlap_days)
        end_date = _dateint_today()
        
        logger.info(f"Processing date range: {start_date} ~ {end_date} "
                   f"(overlap_days={self.overlap_days})")
        
        # 执行任务（带重试）
        success = self._run_with_retry(start_date, end_date)
        
        # 记录结果
        elapsed = datetime.now() - start_time
        if success:
            logger.info(f"{'='*80}")
            logger.info(f"Daily task COMPLETED successfully in {elapsed}")
            logger.info(f"{'='*80}")
        else:
            logger.error(f"{'='*80}")
            logger.error(f"Daily task FAILED after {elapsed}")
            logger.error(f"{'='*80}")
    
    def _run_with_retry(self, start_date: int, end_date: int) -> bool:
        """
        带重试机制的任务执行
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
        
        Returns:
            True 成功，False 失败
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(f"Attempt {attempt}/{self.max_retries}")
                
                # 创建任务实例
                task = SpaceSignalsIngest(mapping_path=self.mapping_path)
                
                # 执行任务
                success = task.run_latest(start_date=start_date, end_date=end_date)
                
                if success:
                    if attempt > 1:
                        logger.info(f"Task succeeded on attempt {attempt}")
                    return True
                else:
                    logger.warning(f"Task returned failure on attempt {attempt}")
                    
            except Exception as e:
                logger.exception(f"Exception on attempt {attempt}: {e}")
            
            # 如果还有重试机会，等待后重试
            if attempt < self.max_retries:
                logger.info(f"Retrying in {self.retry_delay} seconds...")
                time.sleep(self.retry_delay)
        
        logger.error(f"Task failed after {self.max_retries} attempts")
        return False
    
    def start(self):
        """
        启动调度器
        
        如果配置中 enabled=False，则不启动
        """
        if not self.enabled:
            logger.info("Scheduler is disabled in configuration, not starting")
            return
        
        # 格式化调度时间
        schedule_time = f"{self.hour:02d}:{self.minute:02d}"
        
        # 设置定时任务
        schedule.every().day.at(schedule_time).do(self.run_daily_task)
        
        logger.info(f"{'='*80}")
        logger.info(f"Space Signals Scheduler STARTED")
        logger.info(f"Schedule: Daily at {schedule_time}")
        logger.info(f"Overlap days: {self.overlap_days}")
        logger.info(f"Max retries: {self.max_retries}")
        logger.info(f"{'='*80}")
        
        # 主循环
        try:
            while True:
                schedule.run_pending()
                time.sleep(60)  # 每分钟检查一次
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user (Ctrl+C)")
        except Exception as e:
            logger.exception(f"Scheduler stopped due to error: {e}")
    
    def run_once(self):
        """
        立即执行一次任务（用于测试）
        """
        logger.info("Running task immediately (test mode)")
        self.run_daily_task()


def main():
    """
    主函数：启动调度器
    
    使用方式：
        python -m src.scheduler.space_signals_scheduler
    """
    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                f"space_scheduler_{datetime.now().strftime('%Y%m%d')}.log",
                encoding='utf-8'
            )
        ]
    )
    
    # 创建并启动调度器
    scheduler = SpaceSignalsScheduler()
    scheduler.start()


if __name__ == "__main__":
    main()

