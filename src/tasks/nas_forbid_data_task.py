import logging
import pandas as pd
from datetime import datetime, timedelta
from src.tasks.base import BaseTask
from src.data_service.data_loading.forbid_data import ForbidDataLoader
from src.utils.table_schema import TableSchemaBuilder
from src.data_service.data_saving.data_to_testdb import TestDBManager
from src.utils.config_loader import ConfigLoader

# 默认NAS路径，可被构造函数参数覆盖
DEFAULT_NAS_PATH = r'\\space\forbid'

class NASForbidDataTask(BaseTask):
    """
    Task to load forbid pool data from NAS, process it, and save/update it in the database.
    """

    def __init__(self, nas_path=None):
        """
        Initializes the task, loads configuration, ensures the database table exists.
        
        Args:
            nas_path (str, optional): 完整的NAS路径。如果提供，将直接使用此路径而不依赖配置文件中的base_path。
        """
        super().__init__('nas_forbid_data_task') # Initialize BaseTask with task name
        self.logger.info("Initializing NASForbidDataTask...")

        # 使用传入的NAS路径或默认路径
        self.nas_path = nas_path or DEFAULT_NAS_PATH
        self.logger.info(f"Using NAS path: {self.nas_path}")

        # Load relevant configurations
        try:
            self.config_loader = ConfigLoader(config_dir='configs')
            self.cfg = self.config_loader.load_config("nas_disk/nas_config.yaml")
            self.loader_cfg = self.cfg.get('loader', {})
            self.db_cfg = self.cfg.get('database', {})

            self.overlap_days = self.loader_cfg.get('overlap_days', 3) # Default overlap if not in config
            self.table_name = self.db_cfg.get('table_name')
            self.pk_fields = self.db_cfg.get('pk_fields')

            if not self.table_name:
                raise ValueError("Database 'table_name' not found in configuration.")
            if not self.pk_fields or not isinstance(self.pk_fields, list):
                 raise ValueError("Database 'pk_fields' not found or not a list in configuration.")

        except Exception as e:
            self.logger.error(f"Failed to load configuration for NASForbidDataTask: {e}", exc_info=True)
            raise

        # Instantiate necessary components - 传递NAS路径
        self.loader = ForbidDataLoader(nas_path=self.nas_path)
        self.db = TestDBManager()

        # Ensure the target table exists in the database
        self._ensure_table_exists()

        self.logger.info(f"NASForbidDataTask initialized. Target table: '{self.table_name}', Overlap: {self.overlap_days} days.")

    def _ensure_table_exists(self):
        """
        Checks if the target table exists and creates it using the schema if it doesn't.
        """
        try:
            if not self.db.check_table_exists(self.table_name):
                self.logger.info(f"Table '{self.table_name}' does not exist. Creating...")
                schema_def = TableSchemaBuilder.create_forbid_table_schema()
                self.db.create_table(self.table_name, schema_def)
                self.logger.info(f"Table '{self.table_name}' created successfully.")
            else:
                 self.logger.info(f"Table '{self.table_name}' already exists.")
        except Exception as e:
            self.logger.error(f"Error ensuring table '{self.table_name}' exists: {e}", exc_info=True)
            # Depending on severity, might want to raise this
            raise

    def run(self, start_date_str: str = None, end_date_str: str = None, is_init_mode: bool = False) -> bool:
        """
        Executes the task: Load data for the relevant date range and save to DB.

        Args:
            start_date_str: The start date (YYYYMMDD string) for the data loading period.
                           If None and is_init_mode=True, loads from earliest available date.
                           If None and is_init_mode=False, calculates based on end_date and overlap_days.
            end_date_str: The end date (YYYYMMDD string) for the data loading period.
                          If None, defaults to the latest available date found on NAS.
            is_init_mode: If True, ignores overlap_days and loads all data from start_date_str
                          (or earliest available if start_date_str is None) to end_date_str.

        Returns:
            True if the task ran successfully, False otherwise.
        """
        self.logger.info(f"Running NASForbidDataTask for period: {start_date_str or 'Auto'} to {end_date_str or 'Latest'} (Init mode: {is_init_mode})")

        try:
            # 1. Determine date range
            all_dates = self.loader.list_available_dates()
            if not all_dates:
                self.logger.warning("No data files found on NAS. Task finished.")
                return True # No files is not an error in this context

            # Use provided end_date or default to the latest available
            effective_end_date_str = end_date_str or max(all_dates)
            self.logger.info(f"Effective end date for processing: {effective_end_date_str}")

            # Determine start date based on mode and parameters
            if is_init_mode:
                # In initialization mode, use provided start_date or earliest available
                effective_start_date_str = start_date_str or min(all_dates)
                self.logger.info(f"Initialization mode: Using start date {effective_start_date_str}")
            elif start_date_str:
                # Use explicitly provided start date
                effective_start_date_str = start_date_str
                self.logger.info(f"Using explicitly provided start date: {effective_start_date_str}")
            else:
                # Calculate start date based on end date and overlap
                try:
                    end_dt = pd.to_datetime(effective_end_date_str, format='%Y%m%d')
                    start_dt = end_dt - timedelta(days=self.overlap_days)
                    effective_start_date_str = start_dt.strftime('%Y%m%d')
                    self.logger.info(f"Calculated start date using overlap of {self.overlap_days} days: {effective_start_date_str}")
                except ValueError as e:
                    self.logger.error(f"Invalid end date format '{effective_end_date_str}': {e}")
                    return False

            # Filter available dates to get the target range
            target_dates = [d for d in all_dates if d >= effective_start_date_str and d <= effective_end_date_str]

            if not target_dates:
                self.logger.warning(f"No data files found within the target date range: {effective_start_date_str} to {effective_end_date_str}")
                return True

            self.logger.info(f"Target dates to load: {len(target_dates)} (from {min(target_dates)} to {max(target_dates)})")

            # 2. Load data
            df = self.loader.load_dates(target_dates)

            if df.empty:
                self.logger.warning("No data loaded for the target dates. Task finished.")
                return True

            # 3. Add insert timestamp
            # Convert signal to boolean if required by schema? create_forbid_table_schema uses SmallInt, so keep as int.
            # Ensure 'signal' column type is compatible with SmallInteger
            if 'signal' in df.columns:
                df['signal'] = pd.to_numeric(df['signal'], errors='coerce').fillna(0).astype(int) # Already done in loader, but good to ensure

            df["insert_time"] = datetime.utcnow()
            self.logger.info(f"Loaded {len(df)} rows. Adding insert_time.")

            # 4. Save to Database (using upsert logic via save_dataframe)
            self.logger.info(f"Attempting to save/update data to table '{self.table_name}' with PKs {self.pk_fields}")
            success = self.db.save_dataframe(
                df=df,
                table_name=self.table_name,
                mode='update', # 'update' mode with pk_fields performs upsert
                index=False,
                pk_fields=self.pk_fields,
                batch_size=self.loader_cfg.get('batch_size', 1000), # Get batch_size from loader config
                use_parallel=True # Consider using parallel for performance
            )

            if success:
                self.logger.info(f"Successfully saved/updated {len(df)} rows to '{self.table_name}'.")
                return True
            else:
                self.logger.error(f"Failed to save data to '{self.table_name}'. Check TestDBManager logs.")
                return False

        except Exception as e:
            self.logger.error(f"NASForbidDataTask failed during execution: {e}", exc_info=True)
            return False 