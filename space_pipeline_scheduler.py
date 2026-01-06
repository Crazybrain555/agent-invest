#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
space_pipeline_scheduler.py
===========================
Schedule the Space data pipeline to refresh quantitative signal tables only.
"""
import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

try:
    import schedule  # type: ignore
except ImportError:  # pragma: no cover - schedule should be available
    schedule = None

BASE_DIR = Path(__file__).resolve().parent
PIPELINE_SCRIPT = BASE_DIR / "run_space_data_pipeline.py"
LOG_FILE = BASE_DIR / "space_pipeline_scheduler.log"

CATEGORY_JOBS: List[Dict[str, str]] = [
    {"category": "analyst", "table": "quantitative_analyst_signals"},
    {"category": "growth", "table": "quantitative_growth_signals"},
    {"category": "other", "table": "quantitative_other_signals"},
    {"category": "quality", "table": "quantitative_quality_signals"},
    {"category": "sentiment", "table": "quantitative_sentiment_signals"},
    {"category": "value", "table": "quantitative_value_signals"},
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8")
    ],
)
logger = logging.getLogger("space_pipeline_scheduler")


def check_virtual_env() -> bool:
    """Detect whether the script runs inside a virtual environment."""
    if hasattr(sys, "real_prefix"):
        logger.info("Detected virtual environment: %s", sys.prefix)
        return True
    if hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix:
        logger.info("Detected virtual environment: %s", sys.prefix)
        return True
    logger.warning("Virtual environment not detected. Ensure dependencies are installed.")
    return False


def build_command(job: Dict[str, str], range_days: int | None, extra_args: List[str]) -> List[str]:
    """Assemble the command used to invoke the pipeline for a single category."""
    cmd: List[str] = [
        sys.executable,
        str(PIPELINE_SCRIPT),
        "--category",
        job["category"],
        "--custom-table",
        job["table"],
    ]

    if range_days is not None:
        cmd.extend(["--range-days", str(range_days)])

    if extra_args:
        cmd.extend(extra_args)

    return cmd


def run_job(job: Dict[str, str], range_days: int | None, extra_args: List[str]) -> bool:
    """Execute the pipeline script for a single category job."""
    cmd = build_command(job, range_days, extra_args)
    start_ts = datetime.now()

    logger.info("-" * 60)
    logger.info(
        "Running Space data pipeline for category '%s' -> table '%s'",
        job["category"],
        job["table"],
    )
    logger.info("Command: %s", " ".join(cmd))

    try:
        subprocess.run(cmd, check=True, cwd=BASE_DIR)
        duration = datetime.now() - start_ts
        logger.info(
            "Finished category '%s' in %s",
            job["category"],
            duration,
        )
        return True
    except subprocess.CalledProcessError as exc:
        logger.error(
            "Category '%s' failed with exit code %s",
            job["category"],
            exc.returncode,
        )
        return False
    except FileNotFoundError:
        logger.error("run_space_data_pipeline.py not found at %s", PIPELINE_SCRIPT)
        return False
    except Exception as exc:  # pragma: no cover - catch unexpected errors
        logger.exception(
            "Unexpected error while running category '%s': %s",
            job["category"],
            exc,
        )
        return False


def run_all_jobs(range_days: int | None, extra_args: List[str]) -> bool:
    """Execute all configured category jobs sequentially."""
    overall_success = True
    for job in CATEGORY_JOBS:
        success = run_job(job, range_days, extra_args)
        if not success:
            overall_success = False
    return overall_success


def scheduled_job(range_days: int | None, extra_args: List[str]) -> bool:
    """Wrapper used by schedule to trigger the nightly job batch."""
    logger.info(
        "Scheduled run triggered at %s",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    return run_all_jobs(range_days, extra_args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Space data pipeline scheduler for quantitative signal tables"
    )
    parser.add_argument(
        "--schedule-time",
        default="03:00",
        help="Daily trigger time in 24h HH:MM format (default 03:00)",
    )
    parser.add_argument(
        "--daily-range-days",
        type=int,
        default=5,
        help="Rolling window in trading days covered by the nightly job (default 5)",
    )
    parser.add_argument(
        "--initial-range-days",
        type=int,
        default=30,
        help="Historical catch-up window executed on startup (default 30)",
    )
    parser.add_argument(
        "--skip-initial-run",
        action="store_true",
        help="Do not run the catch-up execution on startup",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run a single execution with the daily parameters and exit",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=60,
        help="Scheduler polling interval in seconds (default 60)",
    )
    parser.add_argument(
        "pipeline_args",
        nargs=argparse.REMAINDER,
        help="Additional arguments forwarded to run_space_data_pipeline.py (prefix with --)",
    )

    args = parser.parse_args()

    if args.pipeline_args and args.pipeline_args[0] == "--":
        args.pipeline_args = args.pipeline_args[1:]

    return args


def main() -> int:
    if schedule is None:
        logger.error("Missing dependency: schedule. Install it via 'pip install schedule'.")
        return 1

    args = parse_args()

    logger.info("=" * 60)
    logger.info("Space Pipeline Scheduler started")
    logger.info("Working directory: %s", BASE_DIR)
    logger.info("Daily trigger time: %s", args.schedule_time)
    logger.info("Target tables: %s", ", ".join(job["table"] for job in CATEGORY_JOBS))
    logger.info("Additional pipeline args: %s", args.pipeline_args if args.pipeline_args else "none")

    if not PIPELINE_SCRIPT.exists():
        logger.error("Pipeline script not found: %s", PIPELINE_SCRIPT)
        return 1

    check_virtual_env()

    extra_args = args.pipeline_args or []

    if args.run_once:
        logger.info("Single run requested; executing once and exiting.")
        success = run_all_jobs(args.daily_range_days, extra_args)
        return 0 if success else 1

    if not args.skip_initial_run:
        logger.info(
            "Running initial catch-up window to backfill recent trading days (%s).",
            args.initial_range_days,
        )
        run_all_jobs(args.initial_range_days, extra_args)
    else:
        logger.info("Skipping initial catch-up run; waiting for the scheduled trigger.")

    schedule.every().day.at(args.schedule_time).do(
        scheduled_job,
        range_days=args.daily_range_days,
        extra_args=extra_args,
    )

    logger.info("Scheduler is running; press Ctrl+C to stop.")

    try:
        while True:
            schedule.run_pending()
            time.sleep(max(1, args.poll_interval))
    except KeyboardInterrupt:
        logger.info("Termination requested; shutting down scheduler.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
