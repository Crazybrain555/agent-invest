# Comprehensive Test Script for NAS Data Pipeline
import logging
import os
import pandas as pd
from datetime import datetime
import traceback

# Setup basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Imports --- (Ensure these paths are correct)
try:
    from src.utils.config_loader import ConfigLoader
    from src.utils.nas_connection import NASConnection
    from src.data_service.data_loading.forbid_data import ForbidDataLoader
    from src.tasks.nas_forbid_data_task import NASForbidDataTask
    # Use the renamed scheduler file
    from src.scheduler.nas_get_data_Scheduler import NASDataScheduler
except ImportError as e:
    logger.error(f"Failed to import necessary modules: {e}")
    logger.error("Please ensure the script is run from the project root directory and all modules exist.")
    exit(1)

def run_tests():
    logger.info("=== Starting Comprehensive NAS Data Pipeline Test ===")

    # --- 1. Test Configuration Loading ---
    logger.info("--- Testing Configuration Loading ---")
    cfg = None
    try:
        cfg_loader = ConfigLoader(config_dir='configs')
        cfg = cfg_loader.load_config('nas_disk/nas_config.yaml')
        logger.info("Config loaded successfully.")
        # Optionally print parts of the config for verification
        logger.debug(f"NAS Base Path from config: {cfg.get('nas', {}).get('base_path')}")
        logger.debug(f"DB Table from config: {cfg.get('database', {}).get('table_name')}")
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}", exc_info=True)
        logger.error("Aborting tests.")
        return False

    # --- 2. Test NASConnection Utility ---
    logger.info("--- Testing NASConnection --- ")
    nas = None
    nas_files = []
    try:
        nas = NASConnection() # Initializes based on loaded config
        logger.info(f"NASConnection initialized for base path: {nas.base_path}")

        logger.info("Listing files...")
        nas_files = nas.list_files()
        logger.info(f"Successfully listed {len(nas_files)} files. Sample: {nas_files[:5]}...{nas_files[-5:]}")

        if nas_files:
            first_file_relative = nas_files[0]
            logger.info(f"Attempting to read first file: {first_file_relative}")
            buffer = nas.read_file_to_buffer(first_file_relative)
            logger.info(f"Successfully read file '{first_file_relative}', buffer size: {buffer.getbuffer().nbytes} bytes.")
        else:
            logger.warning("No files found in NAS base path to test reading.")

    except Exception as e:
        logger.error(f"NASConnection test failed: {e}", exc_info=True)
        # Allow continuing if listing/reading failed, but task test will likely fail

    # --- 3. Test ForbidDataLoader ---
    logger.info("--- Testing ForbidDataLoader --- ")
    loader = None
    available_dates = []
    try:
        loader = ForbidDataLoader() # Initializes using config
        logger.info("ForbidDataLoader initialized.")

        available_dates = loader.list_available_dates()
        logger.info(f"Found {len(available_dates)} available dates. Sample: {available_dates[:5]}...{available_dates[-5:]}")

        if available_dates:
            # Load data for the most recent 2 dates (adjust number if needed)
            overlap_test_days = 2
            dates_to_load = available_dates[-overlap_test_days:] # Get last N dates
            if not dates_to_load:
                 logger.warning("Could not determine recent dates to load.")
            else:
                 logger.info(f"Attempting to load data for recent dates: {dates_to_load}")
                 df_loaded = loader.load_dates(dates_to_load)

                 # Log DataFrame info without printing to stdout directly
                 buffer = io.StringIO()
                 df_loaded.info(buf=buffer)
                 info_str = buffer.getvalue()
                 logger.info(f"Loaded DataFrame Info:\n{info_str}")
                 logger.info(f"Loaded DataFrame shape: {df_loaded.shape}")
                 logger.info(f"Loaded DataFrame Head:\n{df_loaded.head().to_string()}")

                 if not df_loaded.empty:
                     logger.info(f"Stock code example: {df_loaded['stock_code'].iloc[0]}")
                     logger.info(f"Signal type: {df_loaded['signal'].dtype}")
                     logger.info(f"Trade date type: {df_loaded['trade_date'].dtype}")
                 else:
                     logger.warning("Loaded DataFrame is empty.")
        else:
            logger.warning("No available dates found to test loading.")

    except Exception as e:
        logger.error(f"ForbidDataLoader test failed: {e}", exc_info=True)

    # --- 4. Test NASForbidDataTask ---
    logger.info("--- Testing NASForbidDataTask (includes DB interaction) --- ")
    task = None
    task_test_success = True
    try:
        logger.info("Initializing NASForbidDataTask...")
        task = NASForbidDataTask()
        # Initialization handles _ensure_table_exists
        logger.info(f"Task initialized successfully. Target table: '{task.table_name}'")

        # Test 1: Run with latest date
        logger.info("Running task for latest available date...")
        success_latest = task.run()
        logger.info(f"Task run (latest date) completed. Success: {success_latest}")
        if not success_latest: task_test_success = False

        # Test 2: Run with a specific end date (use second to last date if available)
        if len(available_dates) >= 2:
            specific_date = available_dates[-2]
            logger.info(f"Running task for specific end date: {specific_date}...")
            success_specific = task.run(end_date_str=specific_date)
            logger.info(f"Task run (specific date: {specific_date}) completed. Success: {success_specific}")
            if not success_specific: task_test_success = False

            # Test 3: Run again with the same specific date (test upsert)
            logger.info(f"Running task AGAIN for specific end date: {specific_date} (testing upsert)...")
            success_repeat = task.run(end_date_str=specific_date)
            logger.info(f"Task run (repeat specific date: {specific_date}) completed. Success: {success_repeat}")
            if not success_repeat: task_test_success = False
        else:
            logger.warning("Skipping specific date tests as not enough available dates found.")

        logger.info("NASForbidDataTask tests finished. Please manually verify data in database table 'restricted_stock_pool'.")

    except Exception as e:
        logger.error(f"NASForbidDataTask test failed during setup or execution: {e}", exc_info=True)
        task_test_success = False

    # --- 5. Test NASDataScheduler (Run Job Once) ---
    logger.info("--- Testing NASDataScheduler (running job once) --- ")
    scheduler = None
    scheduler_test_success = True
    try:
        # Need to import io here if not imported globally
        import io
        scheduler = NASDataScheduler()
        if scheduler.is_enabled:
            logger.info("Scheduler initialized and enabled. Running the job once...")
            scheduler.job() # Execute the scheduled job function directly
            logger.info("Scheduler job() method executed. Check logs for task output.")
        else:
            logger.warning("NASDataScheduler is disabled in config, skipping job run test.")
            scheduler_test_success = True # Not a failure if disabled

    except Exception as e:
        logger.error(f"NASDataScheduler test failed: {e}", exc_info=True)
        scheduler_test_success = False

    # --- Summary ---
    logger.info("=== Comprehensive Test Summary ===")
    if cfg: logger.info("Config Loading: OK")
    else: logger.error("Config Loading: FAILED")
    if nas and nas_files: logger.info("NAS Connection & Listing: OK")
    else: logger.warning("NAS Connection & Listing: FAILED or No Files")
    if loader and available_dates: logger.info("Data Loader: OK")
    else: logger.warning("Data Loader: FAILED or No Dates")
    if task and task_test_success: logger.info("Task Execution (DB Save/Upsert): OK (Verify DB Manually)")
    else: logger.error("Task Execution (DB Save/Upsert): FAILED")
    if scheduler and scheduler_test_success: logger.info("Scheduler Job Execution (Single Run): OK")
    else: logger.error("Scheduler Job Execution (Single Run): FAILED or Disabled")

    logger.info("=== Test Script Finished ===")

if __name__ == "__main__":
    run_tests()



