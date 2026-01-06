"""
Main entry point for the quant framework.
"""
import os
import sys
from src.utils.logger import setup_logger
from src.scheduler.job_runner import JobRunner
from src.tasks.etl import ETLTask
from src.tasks.training import TrainingTask

def main():
    """Main entry point."""
    # Setup logging
    logger = setup_logger("main")
    logger.info("Starting quant framework...")
    
    try:
        # Initialize tasks
        etl_task = ETLTask()
        training_task = TrainingTask()
        
        # Initialize and start scheduler
        runner = JobRunner()
        runner.start()
        
        # Keep the main thread alive
        try:
            while True:
                pass
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            runner.stop()
            
    except Exception as e:
        logger.error(f"Application error: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main() 