"""
Job runner for executing scheduled tasks.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
import importlib
import logging
from typing import Any, Dict

from .job_definitions import JobDefinitions
from src.tasks.etl import ETLTask
from src.tasks.training import TrainingTask

logger = logging.getLogger(__name__)

class JobRunner:
    """Handles the execution of scheduled jobs."""
    
    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.jobs = JobDefinitions.get_jobs()
        self.task_instances = {
            'daily_etl': ETLTask(),
            'weekly_training': TrainingTask()
        }
    
    def _create_trigger(self, job_config: Dict[str, Any]):
        """Create the appropriate trigger based on job configuration."""
        trigger_type = job_config['trigger']
        
        if trigger_type == 'cron':
            return CronTrigger(**{k: v for k, v in job_config.items() 
                                if k not in ['name', 'func', 'trigger', 'args', 'kwargs']})
        elif trigger_type == 'interval':
            return IntervalTrigger(**job_config.get('kwargs', {}))
        elif trigger_type == 'date':
            return DateTrigger(**job_config.get('kwargs', {}))
        else:
            raise ValueError(f"Unsupported trigger type: {trigger_type}")
    
    def _task_wrapper(self, task_name: str, *args, **kwargs):
        """Wrapper function to execute a task."""
        task = self.task_instances.get(task_name)
        if task:
            return task.execute(*args, **kwargs)
        else:
            raise ValueError(f"Task not found: {task_name}")
    
    def start(self):
        """Start the scheduler and add all jobs."""
        for job_id, job_config in self.jobs.items():
            try:
                trigger = self._create_trigger(job_config)
                
                self.scheduler.add_job(
                    self._task_wrapper,
                    trigger=trigger,
                    id=job_id,
                    name=job_config['name'],
                    args=[job_id] + list(job_config.get('args', ())),
                    kwargs=job_config.get('kwargs', {})
                )
                logger.info(f"Added job: {job_id}")
            except Exception as e:
                logger.error(f"Failed to add job {job_id}: {str(e)}")
        
        self.scheduler.start()
        logger.info("Scheduler started")
    
    def stop(self):
        """Stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Scheduler stopped") 