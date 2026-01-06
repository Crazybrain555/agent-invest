import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
import yaml

# Ensure the repo root is in the path
repo_root = Path(__file__).resolve().parents[2]
repo_root_str = str(repo_root)
if repo_root_str not in sys.path:
    sys.path.append(repo_root_str)

from src.tasks.index_stk_pool_task import IndexStockPoolTask
from src.utils.config_loader import ConfigLoader
from src.utils.logger import setup_logger


logger = setup_logger(__name__)

DEFAULT_CONFIG_PATH = "pipelines/stk_pool.yaml"


def _load_config(config_path: str) -> dict:
    config_loader = ConfigLoader(config_dir="configs")
    path = config_path or DEFAULT_CONFIG_PATH
    if os.path.isabs(path) or os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            cfg = yaml.safe_load(handle) or {}
    else:
        cfg = config_loader.load_config(path)
    return cfg.get("index_pool", {})


def _normalize_pool_codes(pool_codes):
    if not pool_codes:
        return []
    normalized = []
    for code in pool_codes:
        if code is None:
            continue
        for part in str(code).split(","):
            part = part.strip().upper()
            if part:
                normalized.append(part)
    # Deduplicate while preserving order
    seen = set()
    unique_codes = []
    for code in normalized:
        if code not in seen:
            seen.add(code)
            unique_codes.append(code)
    return unique_codes


def main():
    parser = argparse.ArgumentParser(description="Run index stock pool pipeline")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--init", action="store_true", help="Initialize full history")
    mode_group.add_argument("--latest", action="store_true", help="Update latest dates (default)")
    mode_group.add_argument("--date", type=str, help="Run for a single trade date (YYYYMMDD)")
    mode_group.add_argument("--range", nargs=2, metavar=("START", "END"), help="Run for a date range (YYYYMMDD)")
    parser.add_argument("--start-date", type=str, help="Start date for init mode (YYYYMMDD)")
    parser.add_argument("--end-date", type=str, help="End date override (YYYYMMDD)")
    parser.add_argument("--pool-codes", nargs="*", help="Override pool codes (space or comma separated)")
    parser.add_argument("--overlap-days", type=int, help="Overlap days for latest mode")
    parser.add_argument("--write-batch-rows", type=int, help="Rows per DB write batch")
    parser.add_argument("--config", type=str, help="Config path (relative to configs/ or absolute)")
    args = parser.parse_args()

    logger.info("--- Index Stock Pool Pipeline ---")
    logger.info(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    cfg = _load_config(args.config)
    pool_codes = _normalize_pool_codes(args.pool_codes) or _normalize_pool_codes(cfg.get("pool_codes", []))

    if not pool_codes:
        logger.error("No pool_codes provided (use --pool-codes or config).")
        return 1

    task = IndexStockPoolTask(
        table_name=cfg.get("table_name", "stk_pool_of_index"),
        pool_codes=pool_codes,
        overlap_days=args.overlap_days if args.overlap_days is not None else cfg.get("overlap_days", 20),
        init_start_date=cfg.get("init_start_date", "20050104"),
        calendar_market=cfg.get("calendar_market", "SSE"),
        write_batch_rows=args.write_batch_rows or cfg.get("write_batch_rows", 200000),
    )

    if args.init:
        mode = "init"
        start_date = args.start_date
        end_date = args.end_date
    elif args.date:
        mode = "date"
        start_date = args.date
        end_date = args.date
    elif args.range:
        mode = "range"
        start_date, end_date = args.range
    else:
        mode = "latest"
        start_date = None
        end_date = args.end_date
        if args.start_date:
            logger.warning("--start-date is ignored in latest mode.")

    logger.info(
        f"Mode={mode}, start_date={start_date}, end_date={end_date}, "
        f"pool_codes={pool_codes}, overlap_days={args.overlap_days}"
    )

    success = task.run(
        mode=mode,
        start_date=start_date,
        end_date=end_date,
        pool_codes=pool_codes,
        overlap_days=args.overlap_days,
    )

    if success:
        logger.info("Index stock pool pipeline finished successfully.")
        return 0

    logger.error("Index stock pool pipeline failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
