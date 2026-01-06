# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

import pandas as pd

from src.utils.config_loader import ConfigLoader
from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider


def _load_table_configs() -> dict:
    """Load table configurations from local_db_configs.yaml"""
    config_loader = ConfigLoader(config_dir='configs')
    try:
        return config_loader.load_config('db/local_db_configs.yaml')['tables']
    except Exception as e:
        logger.error(f"Failed to load table configurations: {str(e)}")
        raise


def _load_restricted_set(prov: LocalTestDBDataProvider, start: str, end: str, table: str):
    df = prov.fetch_data(table=table, start_date=start, end_date=end,
                         fields=["trade_date", "stock_code", "signal"])
    df_restricted = df[df.signal == 1].copy()
    df_restricted['trade_date_formatted'] = pd.to_datetime(df_restricted['trade_date']).dt.strftime('%Y%m%d')
    return set(zip(df_restricted['trade_date_formatted'],
                   df_restricted['stock_code'].astype(str)))


