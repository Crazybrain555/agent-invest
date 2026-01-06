"""
测试数据归一化功能
"""
import os
import sys
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

# 添加项目根目录到Python路径
project_root = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, project_root)

from src.data_service.data_loading.market_data import MarketDataProvider
from src.data_service.preprocessing.methods.normalizer import DataNormalizer
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


def test_normalization():
    """测试数据归一化功能"""
    print("\n=== 测试数据归一化功能 ===")
    try:
        # 1. 加载数据
        provider = MarketDataProvider()
        fields = ['adj_close', 'volume']
        start_date = '20230101'
        end_date = '20230131'
        stock_codes = None
        feature_lag = 3

        print("\n1. 加载市场数据...")
        df_wide = provider.fetch_data(
            fields=fields,
            start_date=start_date,
            end_date=end_date,
            stock_codes=stock_codes,
            feature_lag=feature_lag,
            format='wide'
        )
        print(f"宽表数据形状: {df_wide.shape}")
        print("\n宽表数据示例:")
        print(df_wide.head())

        # 2. 转换为长表格式
        print("\n2. 转换为长表格式...")
        df_long = provider.fetch_data(
            fields=fields,
            start_date=start_date,
            end_date=end_date,
            stock_codes=stock_codes,
            feature_lag=feature_lag,
            format='long'
        )
        print(f"长表数据形状: {df_long.shape}")
        print("\n长表数据示例:")
        print(df_long.head())

        # 3. 测试归一化
        normalizer = DataNormalizer()

        # 3.1 测试 lag_0 归一化（宽表）
        print("\n3.1 测试 lag_0 归一化（宽表）...")
        df_wide_norm = normalizer.normalize_data(
            df=df_wide,
            fields=['adj_close', 'volume'],
            method='lag_0',
            data_format='wide',
            keep_original=False
        )
        print("\n归一化后的宽表数据示例:")
        print(df_wide_norm.head())

        # 3.2 测试 lag_0 归一化（长表）
        print("\n3.2 测试 lag_0 归一化（长表）...")
        df_long_norm = normalizer.normalize_data(
            df=df_long,
            fields=fields,
            method='lag_0',
            data_format='long',
            keep_original=False
        )
        print("\n归一化后的长表数据示例:")
        print(df_long_norm.head())


    except Exception as e:
        print(f"测试失败: {str(e)}")
        raise


if __name__ == "__main__":
    test_normalization()
