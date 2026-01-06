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
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
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
        stock_codes = ['000001.SZ', '000002.SZ', '600519.SH']
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

def test_valuation_indicators():
    """测试新增的AShareValuationIndicator表的估值指标"""
    print("\n=== 测试AShareValuationIndicator估值指标 ===")
    try:
        # 初始化数据提供者
        provider = MarketDataProvider()
        
        # 测试估值指标字段
        valuation_fields = [
            'pe_ttm',           # 市盈率(TTM) 
            'pe_ratio',         # 市盈率(LYR)
            'pb_ratio',         # 市净率(LF)
            'ps_ttm',           # 市销率(TTM)
            'pe_deducted_ttm',  # 市盈率(TTM,扣非)
            'dividend_yield_12m' # 股息率(近12个月)
        ]
        
        # 测试参数
        start_date = '20231201'
        end_date = '20231210'
        stock_codes = ['000001.SZ', '600519.SH', '000002.SZ']
        
        print(f"\n1. 测试估值指标字段映射...")
        for field in valuation_fields:
            try:
                field_info = provider._get_field_info(field)
                print(f"  ✅ {field}: {field_info['value_name']} (来源: {field_info['table']})")
            except Exception as e:
                print(f"  ❌ {field}: {str(e)}")
        
        print(f"\n2. 获取估值指标数据...")
        df = provider.fetch_data(
            fields=valuation_fields,
            start_date=start_date,
            end_date=end_date,
            stock_codes=stock_codes,
            format='wide'
        )
        
        print(f"数据形状: {df.shape}")
        print(f"字段列: {[col for col in df.columns if col not in ['trade_date', 'stock_code']]}")
        
        # 检查数据质量
        print(f"\n3. 数据质量检查...")
        print(f"数据行数: {len(df)}")
        print(f"股票数量: {df['stock_code'].nunique()}")
        print(f"交易日数量: {df['trade_date'].nunique()}")
        
        # 检查负值情况（估值指标的重要特性）
        print(f"\n4. 检查负值情况（估值指标应该包含负值）...")
        for field in valuation_fields:
            if field in df.columns:
                negative_count = (df[field] < 0).sum()
                total_count = df[field].notna().sum()
                if negative_count > 0:
                    print(f"  {field}: {negative_count}/{total_count} 个负值 ({negative_count/total_count*100:.1f}%)")
                else:
                    print(f"  {field}: 无负值")
        
        # 显示统计信息
        print(f"\n5. 统计信息...")
        stats = df[valuation_fields].describe()
        print(stats)
        
        print(f"\n✅ AShareValuationIndicator表测试完成!")
        return True
        
    except Exception as e:
        print(f"\n❌ AShareValuationIndicator表测试失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_multiple_tables():
    """测试多表数据整合"""
    print("\n=== 测试多表数据整合 ===")
    try:
        provider = MarketDataProvider()
        
        # 混合字段：来自不同表的字段
        mixed_fields = [
            'adj_close',        # 来自AShareEODPrices
            'volume',           # 来自AShareEODPrices
            'market_cap',       # 来自AShareEODDerivativeIndicator
            'pe_ttm',           # 来自AShareValuationIndicator
            'pb_ratio',         # 来自AShareValuationIndicator
            'consensus_np'      # 来自con_forecast_roll_stk
        ]
        
        start_date = '20231201'
        end_date = '20231205'
        stock_codes = ['000001.SZ', '600519.SH']
        
        print(f"\n1. 测试多表字段整合...")
        for field in mixed_fields:
            try:
                field_info = provider._get_field_info(field)
                print(f"  {field}: {field_info['table']} ({field_info['data_source']})")
            except Exception as e:
                print(f"  {field}: 字段配置错误 - {str(e)}")
        
        print(f"\n2. 获取多表数据...")
        df = provider.fetch_data(
            fields=mixed_fields,
            start_date=start_date,
            end_date=end_date,
            stock_codes=stock_codes,
            format='wide'
        )
        
        print(f"多表整合数据形状: {df.shape}")
        print(f"包含的字段: {[col for col in df.columns if col not in ['trade_date', 'stock_code']]}")
        
        # 检查数据完整性
        print(f"\n3. 多表数据完整性检查...")
        missing_data = df.isnull().sum()
        print("缺失值统计:")
        for col, missing in missing_data.items():
            if missing > 0:
                print(f"  {col}: {missing} 个缺失值")
        
        print(f"\n✅ 多表数据整合测试完成!")
        return True
        
    except Exception as e:
        print(f"\n❌ 多表数据整合测试失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("开始运行数据库管理器综合测试...")
    
    # 运行所有测试
    tests = [
        ("数据归一化测试", test_normalization),
        ("估值指标表测试", test_valuation_indicators), 
        ("多表整合测试", test_multiple_tables)
    ]
    
    passed = 0
    failed = 0
    
    for test_name, test_func in tests:
        try:
            print(f"\n{'='*60}")
            print(f"执行测试: {test_name}")
            print('='*60)
            
            result = test_func()
            if result != False:
                print(f"✅ {test_name} 通过")
                passed += 1
            else:
                print(f"❌ {test_name} 失败")
                failed += 1
        except Exception as e:
            print(f"❌ {test_name} 异常: {str(e)}")
            failed += 1
    
    print(f"\n{'='*60}")
    print(f"测试结果汇总: 通过 {passed} 个, 失败 {failed} 个")
    print('='*60)
    
    if failed == 0:
        print("🎉 所有测试通过！数据库配置和功能正常！")
    else:
        print("⚠️  部分测试失败，请检查配置和连接")
