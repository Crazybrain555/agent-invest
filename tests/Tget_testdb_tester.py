"""
测试本地测试数据库数据提供者功能
"""
import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# 添加项目根目录到Python路径
project_root = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, project_root)

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

def test_wide_table_format_conversion():
    """测试宽表格式转换（宽表↔长表）"""
    print("\n=== 测试1: 宽表格式转换（宽表↔长表）===")
    try:
        provider = LocalTestDBDataProvider()
        
        # 1. 以宽表格式读取数据
        table = 'ai_is.intermediate_training_factors_market_normalize_lag30_countday1'
        start_date = '20230101'
        end_date = '20230110'
        stock_codes = ['000001', '000002']
        fields = ['adj_close_lag_0', 'adj_close_lag_1', 'volume_lag_0']
        
        print("\n1.1 以宽表格式读取数据...")
        df_wide = provider.fetch_data(
            table=table,
            start_date=start_date,
            end_date=end_date,
            stock_codes=stock_codes,
            fields=fields,
            format='wide'
        )
        
        if df_wide.empty:
            print("警告: 查询返回空数据集，请检查数据库是否有符合条件的数据。")
            return
            
        print(f"宽表数据形状: {df_wide.shape}")
        print("\n宽表数据示例:")
        print(df_wide.head())
        
        # 2. 以长表格式读取相同的数据
        print("\n1.2 以长表格式读取相同的数据...")
        df_long = provider.fetch_data(
            table=table,
            start_date=start_date,
            end_date=end_date,
            stock_codes=stock_codes,
            fields=fields,
            format='long'
        )
        
        print(f"长表数据形状: {df_long.shape}")
        print("\n长表数据示例:")
        print(df_long.head())
        
        # 验证转换是否正确
        expected_long_rows = df_wide.shape[0] * (len(fields))
        print(f"\n预期长表行数: {expected_long_rows}, 实际长表行数: {df_long.shape[0]}")
        assert df_long.shape[0] <= expected_long_rows, "长表行数不符合预期"
        
        # 验证唯一日期数和股票数
        wide_dates = df_wide['trade_date'].nunique()
        wide_stocks = df_wide['stock_code'].nunique()
        long_dates = df_long['trade_date'].nunique()
        long_stocks = df_long['stock_code'].nunique()
        
        print(f"宽表: {wide_dates}个日期, {wide_stocks}只股票")
        print(f"长表: {long_dates}个日期, {long_stocks}只股票")
        
        assert wide_dates == long_dates, "宽表和长表的日期数不一致"
        assert wide_stocks == long_stocks, "宽表和长表的股票数不一致"
        
        print("测试通过: 宽表和长表转换正确。\n")
        
    except Exception as e:
        print(f"测试失败: {str(e)}")
        raise

def test_long_table_field_filtering():
    """测试长表字段过滤"""
    print("\n=== 测试2: 长表字段过滤 ===")
    try:
        provider = LocalTestDBDataProvider()
        
        # 1. 获取所有字段
        table = 'ai_is.training_label_ls10_adj_topcor_cr30_cw240'
        start_date = '20230101'
        end_date = '20230110'
        stock_codes = ['000001', '000002']
        
        print("\n2.1 获取所有标签字段...")
        df_all = provider.fetch_data(
            table=table,
            start_date=start_date,
            end_date=end_date,
            stock_codes=stock_codes
        )
        
        if df_all.empty:
            print("警告: 查询返回空数据集，请检查数据库是否有符合条件的数据。")
            return
            
        print(f"所有字段数据形状: {df_all.shape}")
        print("\n所有字段数据示例:")
        print(df_all.head())
        
        # 获取唯一的字段名
        field_names = df_all['field_name'].unique()
        print(f"\n可用的字段名: {field_names}")
        
        # 2. 过滤特定字段
        if len(field_names) > 0:
            filter_field = [field_names[0]]  # 选择第一个可用字段
            print(f"\n2.2 仅获取字段 {filter_field}...")
            
            df_filtered = provider.fetch_data(
                table=table,
                start_date=start_date,
                end_date=end_date,
                stock_codes=stock_codes,
                fields=filter_field
            )
            
            print(f"过滤后数据形状: {df_filtered.shape}")
            print("\n过滤后数据示例:")
            print(df_filtered.head())
            
            # 验证过滤是否有效
            filtered_fields = df_filtered['field_name'].unique()
            print(f"过滤后的字段名: {filtered_fields}")
            
            assert len(filtered_fields) == len(filter_field), "过滤后的字段数不符合预期"
            assert all(field in filter_field for field in filtered_fields), "过滤后的字段不在预期范围内"
            
            # 验证行数比例
            expected_ratio = len(filter_field) / len(field_names)
            actual_ratio = df_filtered.shape[0] / df_all.shape[0]
            print(f"预期行数比例: {expected_ratio:.2f}, 实际行数比例: {actual_ratio:.2f}")
            
            # 允许一定的误差（例如，由于某些字段可能有缺失值）
            assert abs(actual_ratio - expected_ratio) < 0.5, "过滤后的行数比例差异过大"
            
            print("测试通过: 长表字段过滤正确。\n")
        else:
            print("警告: 没有可用的字段名进行测试。")
        
    except Exception as e:
        print(f"测试失败: {str(e)}")
        raise

def test_stat_table():
    """测试统计表查询"""
    print("\n=== 测试3: 统计表查询 ===")
    try:
        provider = LocalTestDBDataProvider()
        
        # 查询统计表
        table = 'ai_is.inter_train_factors_std_l30_d1_2002_2012'
        
        print("\n3.1 获取所有特征的统计参数...")
        df_stats = provider.fetch_data(
            table=table
        )
        
        if df_stats.empty:
            print("警告: 查询返回空数据集，请检查数据库是否有符合条件的数据。")
            return
            
        print(f"统计参数数据形状: {df_stats.shape}")
        print("\n统计参数数据示例:")
        print(df_stats.head())
        
        # 获取唯一的特征名
        feature_names = df_stats['feature_name'].unique()
        print(f"\n发现 {len(feature_names)} 个特征的统计参数")
        
        # 过滤特定特征
        if len(feature_names) > 0:
            filter_features = [feature_names[0]]  # 选择第一个特征
            print(f"\n3.2 仅获取特征 {filter_features} 的统计参数...")
            
            df_filtered = provider.fetch_data(
                table=table,
                fields=filter_features
            )
            
            print(f"过滤后数据形状: {df_filtered.shape}")
            print("\n过滤后数据示例:")
            print(df_filtered.head())
            
            # 验证过滤是否有效
            filtered_features = df_filtered['feature_name'].unique()
            print(f"过滤后的特征名: {filtered_features}")
            
            assert len(filtered_features) == len(filter_features), "过滤后的特征数不符合预期"
            assert all(feature in filter_features for feature in filtered_features), "过滤后的特征不在预期范围内"
            
            print("测试通过: 统计表查询正确。\n")
        else:
            print("警告: 没有可用的特征名进行测试。")
        
    except Exception as e:
        print(f"测试失败: {str(e)}")
        raise

def test_flag_table():
    """测试标志表查询（禁投池）"""
    print("\n=== 测试4: 标志表查询（禁投池）===")
    try:
        provider = LocalTestDBDataProvider()
        
        # 查询禁投池
        table = 'ai_is.restricted_stock_pool'
        start_date = '20230101'
        end_date = '20230110'
        
        print("\n4.1 获取指定日期范围的禁投池数据...")
        df_restricted = provider.fetch_data(
            table=table,
            start_date=start_date,
            end_date=end_date
        )
        
        if df_restricted.empty:
            print("警告: 查询返回空数据集，请检查数据库是否有符合条件的数据。")
            return
            
        print(f"禁投池数据形状: {df_restricted.shape}")
        print("\n禁投池数据示例:")
        print(df_restricted.head())
        
        # 验证信号字段
        print("\n验证信号字段类型...")
        if 'signal' in df_restricted.columns:
            signal_type = df_restricted['signal'].dtype
            print(f"signal字段的数据类型: {signal_type}")
            assert signal_type == bool or signal_type == np.bool_, "signal字段不是布尔类型"
        else:
            print("警告: 禁投池数据中没有signal字段")
        
        # 获取唯一日期数
        dates = df_restricted['trade_date'].unique()
        print(f"\n发现 {len(dates)} 个交易日的禁投数据")
        
        # 过滤特定股票
        if df_restricted.shape[0] > 0:
            sample_stock = df_restricted['stock_code'].iloc[0]
            print(f"\n4.2 仅获取股票 {sample_stock} 的禁投数据...")
            
            df_filtered = provider.fetch_data(
                table=table,
                start_date=start_date,
                end_date=end_date,
                stock_codes=[sample_stock]
            )
            
            print(f"过滤后数据形状: {df_filtered.shape}")
            print("\n过滤后数据示例:")
            print(df_filtered.head())
            
            # 验证过滤是否有效
            filtered_stocks = df_filtered['stock_code'].unique()
            print(f"过滤后的股票代码: {filtered_stocks}")
            
            assert len(filtered_stocks) == 1, "过滤后的股票数不符合预期"
            assert filtered_stocks[0] == sample_stock, "过滤后的股票不是预期的样本股票"
            
            print("测试通过: 标志表查询正确。\n")
        else:
            print("警告: 没有可用的禁投数据进行测试。")
        
    except Exception as e:
        print(f"测试失败: {str(e)}")
        raise

def main():
    """主函数"""
    print("=== 本地测试数据库数据提供者测试 ===\n")
    
    # 运行所有测试
    try:
        # 测试1: 宽表格式转换
        test_wide_table_format_conversion()
        
        # 测试2: 长表字段过滤
        test_long_table_field_filtering()
        
        # 测试3: 统计表查询
        test_stat_table()
        
        # 测试4: 标志表查询
        test_flag_table()
        
        print("\n所有测试完成！")
        
    except Exception as e:
        print(f"\n测试过程中出现错误: {str(e)}")
        raise

if __name__ == "__main__":
    main() 