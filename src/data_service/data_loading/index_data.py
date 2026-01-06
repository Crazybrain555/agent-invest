import pandas as pd
from typing import List, Optional

from src.data_service.data_loading.market_data import MarketDataProvider


class IndexDataProvider:
    """指数行情数据提供者（包装 MarketDataProvider）。"""

    def __init__(self):
        # 仅加载 index_data 映射，避免与股票字段冲突
        self._provider = MarketDataProvider(sections=['index_data'])

    def fetch_index(
        self,
        fields: List[str],
        start_date: str,
        end_date: str,
        index_codes: Optional[List[str]] = None,
        feature_lag: Optional[int] = None,
        days_counted: int = 1,
        format: str = 'wide',
    ) -> pd.DataFrame:
        """
        获取指数行情数据（AIndexEODPrices），内部直接转发到 MarketDataProvider.fetch_data。

        Args:
            fields: 指数字段列表，例如 ['index_close', 'index_pct_change']。
            start_date: 开始日期，格式 YYYYMMDD。
            end_date: 结束日期，格式 YYYYMMDD。
            index_codes: Wind 指数代码列表，例如 ['000300.SH', '000905.SH']。
            feature_lag: 若需要自动生成滞后特征，传入整数；无需则传 None。
            days_counted: 累积天数参数，保持与基础 Provider 一致。
            format: 输出格式，'wide' 或 'long'。

        Returns:
            pd.DataFrame: 含 trade_date / stock_code 以及请求字段的 DataFrame。
        """
        return self._provider.fetch_data(
            fields=fields,
            start_date=start_date,
            end_date=end_date,
            stock_codes=index_codes,
            feature_lag=feature_lag,
            days_counted=days_counted,
            format=format,
            stock_code_prefixes=None,
        )
