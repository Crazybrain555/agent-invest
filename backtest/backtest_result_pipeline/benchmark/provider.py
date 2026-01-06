"""
基准数据提供者

包装 IndexDataProvider.fetch_index()
"""

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def fetch_benchmark_data(
    benchmark_code: str,
    start_date: str,
    end_date: str,
    fields: Optional[list] = None
) -> pd.DataFrame:
    """
    获取基准指数数据
    
    Args:
        benchmark_code: 基准代码，如 "000852.SH"
        start_date: 开始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD
        fields: 需要的字段，默认 ["index_close", "index_pct_change"]
    
    Returns:
        DataFrame 含 trade_date, index_close 等字段
    """
    if fields is None:
        fields = ["index_close", "index_pct_change"]
    
    logger.info(f"获取基准数据: {benchmark_code} ({start_date} - {end_date})")
    
    try:
        from src.data_service.data_loading.index_data import IndexDataProvider
        
        provider = IndexDataProvider()
        df = provider.fetch_index(
            fields=fields,
            start_date=start_date,
            end_date=end_date,
            index_codes=[benchmark_code],
            feature_lag=None,
            format="wide"
        )
        
        if df.empty:
            logger.warning(f"基准 {benchmark_code} 在 {start_date}-{end_date} 未取到数据")
            return pd.DataFrame()
        
        logger.info(f"   获取到 {len(df)} 条基准数据")
        return df
    
    except Exception as e:
        logger.error(f"获取基准数据失败: {str(e)}")
        return pd.DataFrame()
