"""
Tests for the scheduler module.
"""
import pytest
from src.scheduler.job_runner import JobRunner
from src.scheduler.job_definitions import JobDefinitions

def test_job_definitions():
    """Test that job definitions are properly structured."""
    jobs = JobDefinitions.get_jobs()
    
    assert isinstance(jobs, dict)
    assert 'daily_etl' in jobs
    assert 'weekly_training' in jobs
    
    # Test daily_etl job structure
    daily_etl = jobs['daily_etl']
    assert daily_etl['trigger'] == 'cron'
    assert daily_etl['hour'] == 2
    assert daily_etl['minute'] == 0

def test_job_runner():
    """Test job runner initialization and basic functionality."""
    runner = JobRunner()
    assert runner.scheduler is not None
    assert isinstance(runner.jobs, dict) 