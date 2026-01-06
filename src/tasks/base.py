"""
Base task class for all scheduled tasks.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from src.utils.logger import setup_logger

class BaseTask(ABC):
    """Base class for all tasks in the system."""
    
    def __init__(self, name: str):
        self.name = name
        self.logger = setup_logger(f"task.{name}")
    
    @abstractmethod
    def run(self, *args, **kwargs) -> Any:
        """
        Execute the task.
        
        Args:
            *args: Positional arguments
            **kwargs: Keyword arguments
            
        Returns:
            Any: Task result
        """
        pass
    
    def pre_run(self) -> None:
        """Setup before task execution."""
        self.logger.info(f"Starting task: {self.name}")
    
    def post_run(self) -> None:
        """Cleanup after task execution."""
        self.logger.info(f"Completed task: {self.name}")
    
    def execute(self, *args, **kwargs) -> Any:
        """
        Execute the task with pre and post processing.
        
        Args:
            *args: Positional arguments
            **kwargs: Keyword arguments
            
        Returns:
            Any: Task result
        """
        try:
            self.pre_run()
            result = self.run(*args, **kwargs)
            self.post_run()
            return result
        except Exception as e:
            self.logger.error(f"Task {self.name} failed: {str(e)}")
            raise 