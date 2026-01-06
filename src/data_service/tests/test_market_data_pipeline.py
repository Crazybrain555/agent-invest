import unittest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from src.data_service.data_loading.market_data import MarketDataProvider
from src.data_service.preprocessing.methods.normalizer import MarketDataNormalizer
from src.data_service.data_saving.data_to_testdb import DataSaver
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

class TestMarketDataPipeline(unittest.TestCase):
    """测试市场数据处理流程"""
    
    def setUp(self):
        """测试前的准备工作"""
        self.provider = MarketDataProvider()
        self.normalizer = MarketDataNormalizer()
        self.data_saver = DataSaver()
        # 设置测试时间范围（使用较小的范围以加快测试）
        self.start_date = '2023-01-01'
        self.end_date = '2023-01-10'
        # 设置测试股票代码
        self.test_stocks = ['600463.SH', '000001.SZ']
        # 设置测试字段
        self.test_fields = ['adj_close', 'volume', 'amount']
        
        # 创建测试表
        self.table_schema = """
            CREATE TABLE IF NOT EXISTS normalized_market_data (
                code VARCHAR(20),
                date VARCHAR(8),
                field_name VARCHAR(50),
                lag INTEGER,
                value FLOAT,
                PRIMARY KEY (code, date, field_name, lag)
            )
        """
        self.data_saver.create_table_if_not_exists('normalized_market_data', self.table_schema)
        
    def test_basic_data_fetch(self):
        """测试基本数据获取"""
        logger.info("测试基本数据获取...")
        
        # 获取基本数据
        df = self.provider.fetch_basic_data(
            fields=self.test_fields,
            start_date=self.start_date,
            end_date=self.end_date,
            stock_codes=self.test_stocks
        )
        
        # 验证数据格式
        self.assertIsInstance(df, pd.DataFrame)
        self.assertTrue(all(col in df.columns for col in ['stock_code', 'trade_date', 'field_name', 'value']))
        self.assertEqual(len(df['field_name'].unique()), len(self.test_fields))
        
        # 验证数据内容
        self.assertTrue(len(df) > 0)
        self.assertTrue(all(stock in df['stock_code'].unique() for stock in self.test_stocks))
        
        # 打印数据样例
        logger.info("\n基本数据样例:")
        logger.info(df.head())
        
    def test_data_with_lag(self):
        """测试带滞后特征的数据获取"""
        logger.info("测试带滞后特征的数据获取...")
        
        # 设置较小的 lag 值以加快测试
        feature_lag = 3
        
        # 获取带滞后特征的数据
        df = self.provider.fetch_data_with_lag(
            fields=self.test_fields,
            start_date=self.start_date,
            end_date=self.end_date,
            stock_codes=self.test_stocks,
            feature_lag=feature_lag
        )
        
        # 验证数据格式
        self.assertIsInstance(df, pd.DataFrame)
        self.assertTrue(all(col in df.columns for col in ['stock_code', 'trade_date', 'field_name', 'lag', 'value']))
        
        # 验证 lag 特征
        lag_values = df['lag'].unique()
        self.assertEqual(len(lag_values), feature_lag)
        self.assertTrue(all(lag in range(feature_lag) for lag in lag_values))
        
        # 验证每个字段都有对应的 lag 特征
        for field in self.test_fields:
            field_data = df[df['field_name'] == field]
            self.assertEqual(len(field_data['lag'].unique()), feature_lag)
            
        # 打印数据样例
        logger.info("\n带滞后特征的数据样例:")
        logger.info(df.head())
        
    def test_data_normalization(self):
        """测试数据归一化"""
        logger.info("测试数据归一化...")
        
        # 获取带滞后特征的数据
        df = self.provider.fetch_data_with_lag(
            fields=['adj_close'],  # 只测试价格数据
            start_date=self.start_date,
            end_date=self.end_date,
            stock_codes=self.test_stocks,
            feature_lag=3
        )
        
        # 按股票代码和字段名分组，计算每个 lag 的归一化值
        normalized_df = df.copy()
        for (stock, field), group in df.groupby(['stock_code', 'field_name']):
            # 获取 lag0 作为基准值
            base_values = group[group['lag'] == 0]['value']
            # 计算归一化值
            for lag in range(1, 4):  # 1-3
                mask = (normalized_df['stock_code'] == stock) & \
                       (normalized_df['field_name'] == field) & \
                       (normalized_df['lag'] == lag)
                normalized_df.loc[mask, 'value'] = normalized_df.loc[mask, 'value'] / base_values.values
        
        # 验证归一化结果
        self.assertTrue(len(normalized_df) == len(df))
        
        # 打印归一化后的数据样例
        logger.info("\n归一化后的数据样例:")
        logger.info(normalized_df.head())
        
    def test_pipeline_step1(self):
        """测试完整的数据处理流程（Step 1）"""
        logger.info("开始测试完整的数据处理流程...")
        
        # 1. 获取带滞后特征的数据
        df = self.provider.fetch_data_with_lag(
            fields=self.test_fields,
            start_date=self.start_date,
            end_date=self.end_date,
            stock_codes=self.test_stocks,
            feature_lag=3
        )
        
        # 2. 数据归一化
        normalized_df = df.copy()
        for (stock, field), group in df.groupby(['stock_code', 'field_name']):
            # 获取 lag0 作为基准值
            base_values = group[group['lag'] == 0]['value']
            # 计算归一化值
            for lag in range(1, 4):  # 1-3
                mask = (normalized_df['stock_code'] == stock) & \
                       (normalized_df['field_name'] == field) & \
                       (normalized_df['lag'] == lag)
                normalized_df.loc[mask, 'value'] = normalized_df.loc[mask, 'value'] / base_values.values
        
        # 3. 验证最终结果
        self.assertTrue(len(normalized_df) > 0)
        self.assertTrue(all(col in normalized_df.columns for col in ['stock_code', 'trade_date', 'field_name', 'lag', 'value']))
        
        # 打印最终结果样例
        logger.info("\n数据处理流程最终结果样例:")
        logger.info(normalized_df.head())
        
        # 4. 保存结果（可选）
        # normalized_df.to_csv('test_normalized_data.csv', index=False)
        
    def test_market_data_pipeline(self):
        """测试完整的数据处理流程"""
        # 1. 获取市场数据
        fields = ['adj_close', 'volume']  # 使用配置文件中定义的字段名
        start_date = '20240101'
        end_date = '20240131'
        lag = 5  # 为了测试，使用较小的lag值
        
        df = self.provider.fetch_data(
            fields=fields,
            start_date=start_date,
            end_date=end_date,
            lag=lag
        )
        
        # 验证数据格式
        self.assertFalse(df.empty)
        self.assertIn('code', df.columns)
        self.assertIn('date', df.columns)
        self.assertIn('field_name', df.columns)
        self.assertIn('lag', df.columns)
        self.assertIn('value', df.columns)
        
        # 2. 归一化数据
        normalized_df = self.normalizer.normalize_data(
            df=df,
            fields=fields,
            method='lag_0'
        )
        
        # 验证归一化结果
        self.assertFalse(normalized_df.empty)
        self.assertEqual(len(normalized_df), len(df))
        
        # 3. 保存数据
        self.data_saver.save_to_testdb(
            df=normalized_df,
            table_name='normalized_market_data',
            mode='replace'
        )
        
        # 4. 验证保存的数据
        with self.data_saver.db.get_test_session() as session:
            result = session.execute("""
                SELECT COUNT(*) 
                FROM normalized_market_data
            """)
            count = result.scalar()
            self.assertGreater(count, 0)
            
    def test_lag_feature_generation(self):
        """测试lag特征生成"""
        fields = ['adj_close']  # 使用配置文件中定义的字段名
        start_date = '20240101'
        end_date = '20240110'
        lag = 2
        
        df = self.provider.fetch_data(
            fields=fields,
            start_date=start_date,
            end_date=end_date,
            lag=lag
        )
        
        # 验证lag特征
        for code in df['code'].unique():
            code_data = df[df['code'] == code]
            for date in code_data['date'].unique():
                date_data = code_data[code_data['date'] == date]
                self.assertEqual(len(date_data), lag + 1)  # 应该有lag+1个值（包括lag_0）
                
    def test_normalization(self):
        """测试归一化处理"""
        fields = ['adj_close']  # 使用配置文件中定义的字段名
        start_date = '20240101'
        end_date = '20240110'
        lag = 2
        
        df = self.provider.fetch_data(
            fields=fields,
            start_date=start_date,
            end_date=end_date,
            lag=lag
        )
        
        normalized_df = self.normalizer.normalize_data(
            df=df,
            fields=fields,
            method='lag_0'
        )
        
        # 验证lag_0的值是否为1
        lag_0_values = normalized_df[normalized_df['lag'] == 0]['value']
        self.assertTrue(all(lag_0_values == 1.0))
        
    def test_invalid_field(self):
        """测试无效字段名"""
        fields = ['invalid_field']
        start_date = '20240101'
        end_date = '20240110'
        
        with self.assertRaises(ValueError):
            self.provider.fetch_data(
                fields=fields,
                start_date=start_date,
                end_date=end_date
            )
        
if __name__ == '__main__':
    unittest.main() 