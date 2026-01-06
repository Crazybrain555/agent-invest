#!/usr/bin/env python3
"""
因子保存器使用示例
展示如何使用不同的保存格式和策略
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# 导入因子保存相关组件
from src.data_service.pipelines.factor_utils import (
    FactorSaverFactory,
    FactorSaverManager,
    save_factor_csv,
    save_factor_multi_format
)
from configs.backtest.model_backtest_config import ModelBacktestConfig


def create_sample_factor_data():
    """创建示例因子数据"""
    # 生成样本数据
    dates = pd.date_range('2023-01-01', '2023-12-31', freq='D')
    stock_codes = ['000001', '000002', '600000', '600036', '300015']
    
    data = []
    for date in dates:
        for stock in stock_codes:
            data.append({
                'trade_date': date,
                'stock_code': stock,
                'model_pred': np.random.normal(0, 1)  # 随机因子值
            })
    
    return pd.DataFrame(data)


def example_1_single_csv_saver():
    """示例1：使用单个CSV保存器"""
    print("=" * 60)
    print("示例1：使用单个CSV保存器")
    print("=" * 60)
    
    cfg = ModelBacktestConfig()
    cfg.backtest_result_path = "examples/output"
    
    # 创建示例数据
    df_factor = create_sample_factor_data()
    print(f"生成示例数据: {len(df_factor)} 条记录")
    
    # 使用便捷函数保存
    result = save_factor_csv(df_factor, cfg, "examples/output")
    print(f"保存结果: {result}")


def example_2_factory_pattern():
    """示例2：使用工厂模式创建保存器"""
    print("\n" + "=" * 60)
    print("示例2：使用工厂模式创建保存器")
    print("=" * 60)
    
    cfg = ModelBacktestConfig()
    df_factor = create_sample_factor_data()
    
    # 创建CSV保存器
    csv_saver = FactorSaverFactory.create_saver('csv', cfg)
    result_csv = csv_saver.save(df_factor, "examples/output")
    print(f"CSV保存结果: {result_csv}")
    
    # 创建Parquet保存器（如果有pyarrow的话）
    try:
        parquet_saver = FactorSaverFactory.create_saver('parquet', cfg)
        result_parquet = parquet_saver.save(df_factor, "examples/output")
        print(f"Parquet保存结果: {result_parquet}")
    except Exception as e:
        print(f"Parquet保存跳过（可能缺少pyarrow）: {e}")


def example_3_multi_format_manager():
    """示例3：使用管理器同时保存多种格式"""
    print("\n" + "=" * 60)
    print("示例3：使用管理器同时保存多种格式")
    print("=" * 60)
    
    cfg = ModelBacktestConfig()
    df_factor = create_sample_factor_data()
    
    # 创建多格式保存管理器
    manager = FactorSaverManager(cfg)
    manager.add_saver('csv').add_saver('parquet')
    
    # 同时保存多种格式
    results = manager.save_all(df_factor, "examples/output")
    print(f"多格式保存结果:")
    for saver_type, result in results.items():
        print(f"  {saver_type}: {result.get('status', 'unknown')}")


def example_4_config_driven():
    """示例4：配置驱动的保存方式"""
    print("\n" + "=" * 60)
    print("示例4：配置驱动的保存方式")
    print("=" * 60)
    
    cfg = ModelBacktestConfig()
    cfg.factor_save_formats = ['csv', 'parquet']  # 配置保存格式
    cfg.enable_factor_save = True
    
    df_factor = create_sample_factor_data()
    
    # 使用配置驱动的便捷函数
    results = save_factor_multi_format(
        df_factor, 
        cfg, 
        "examples/output",
        formats=cfg.factor_save_formats
    )
    
    print(f"配置驱动保存结果:")
    for saver_type, result in results.items():
        print(f"  {saver_type}: {result.get('status', 'unknown')}")


def example_5_available_formats():
    """示例5：查看支持的保存格式"""
    print("\n" + "=" * 60)
    print("示例5：查看支持的保存格式")
    print("=" * 60)
    
    formats = FactorSaverFactory.get_available_formats()
    print(f"支持的保存格式: {formats}")
    
    for fmt in formats:
        try:
            cfg = ModelBacktestConfig()
            saver = FactorSaverFactory.create_saver(fmt, cfg)
            print(f"  ✅ {fmt}: {saver.__class__.__name__}")
        except Exception as e:
            print(f"  ❌ {fmt}: {e}")


if __name__ == "__main__":
    print("因子保存器功能演示")
    
    # 运行所有示例
    example_1_single_csv_saver()
    example_2_factory_pattern()
    example_3_multi_format_manager()
    example_4_config_driven()
    example_5_available_formats()
    
    print("\n" + "=" * 60)
    print("演示完成！")
    print("=" * 60)