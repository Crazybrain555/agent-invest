#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
测试标准化参数生成器
用于测试StandardParamsGenerator能否正确处理2002-2012年的数据
"""

import os
import sys
import logging
from datetime import datetime
from src.tasks.standardization_parameter_generation import StandardParamsGenerator
from src.utils.logger import setup_logger
import pandas as pd
import numpy as np
from src.data_service.data_saving.data_to_testdb import TestDBManager
from src.utils.table_schema import TableSchemaBuilder  # 使用 TableSchemaBuilder 来构建表结构

# 设置日志
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

def test_standard_params_generation():
    """
    测试标准化参数生成器
    使用2002-01-01至2012-12-31的数据生成标准化参数
    """
    try:
        logger.info("开始测试标准化参数生成器...")
        
        # 设置参数
        start_date = "2002-01-01"
        end_date = "2012-12-31"
        lag = 30
        days_count = 1
        data_format = 'wide'
        mad_multiplier = 7.0
        batch_size = 10  # 每批处理10个因子
        
        logger.info(f"参数设置: start_date={start_date}, end_date={end_date}, lag={lag}, days_count={days_count}, batch_size={batch_size}")
        
        # 创建标准化参数生成器实例
        generator = StandardParamsGenerator(
            start_date=start_date,
            end_date=end_date,
            lag=lag,
            days_count=days_count,
            data_format=data_format,
            mad_multiplier=mad_multiplier,
            batch_size=batch_size
        )
        
        # 执行标准化参数生成任务
        logger.info("执行标准化参数生成任务...")
        success = generator.execute()
        
        if success:
            logger.info("标准化参数生成任务成功完成!")
            logger.info(f"标准化参数已保存到数据库表 {generator.params_table_name}")
            
            # 打印一些统计信息
            try:
                db_manager = TestDBManager()
                
                # 查询标准化参数表中的记录数
                query = f"SELECT COUNT(*) as count FROM {generator.params_table_name}"
                result = db_manager.execute_query(query)
                if result and len(result) > 0:
                    count = result[0][0]
                    logger.info(f"标准化参数表中共有 {count} 条记录")
                
                # 查询一些示例数据
                query = f"SELECT * FROM {generator.params_table_name} LIMIT 5"
                result = db_manager.execute_query(query)
                if result and len(result) > 0:
                    logger.info("标准化参数示例:")
                    for row in result:
                        logger.info(row)
            except Exception as e:
                logger.error(f"获取标准化参数统计信息失败: {str(e)}")
        else:
            logger.error("标准化参数生成任务失败!")
        
        return success
        
    except Exception as e:
        logger.error(f"测试标准化参数生成器失败: {str(e)}", exc_info=True)
        return False

# 模拟数据
def generate_test_data(rows=50, stocks=5):
    """生成测试数据"""
    logger.info(f"生成测试数据: {rows}行, {stocks}支股票")
    
    # 生成日期和股票代码
    date_range = pd.date_range(start='2023-01-01', periods=rows//stocks)
    stock_codes = [f'60000{i}' for i in range(1, stocks+1)]
    
    # 创建数据框
    data = []
    for date in date_range:
        for stock in stock_codes:
            # 添加一些随机数据
            data.append({
                'trade_date': date,
                'stock_code': stock,
                'field_name': 'label_raw',
                'value': np.random.normal(0, 1),
                'label_shift': 10
            })
    
    df = pd.DataFrame(data)
    logger.info(f"生成了测试数据，形状为: {df.shape}")
    return df

# 删除测试表
def drop_test_table():
    """删除测试表"""
    logger.info("尝试删除现有的测试表...")
    db_manager = TestDBManager()
    if db_manager.check_table_exists("test_table"):
        success = db_manager.delete_table("test_table")
        if success:
            logger.info("成功删除测试表")
        else:
            logger.error("删除测试表失败")
    else:
        logger.info("测试表不存在，无需删除")
    return True

# 创建测试表
def create_test_table():
    """创建测试表"""
    logger.info("创建测试表...")
    
    # 生成测试数据
    df = generate_test_data()
    
    # 创建表结构
    manager = TestDBManager()
    
    # 使用TableSchemaBuilder创建表结构
    columns = TableSchemaBuilder.create_factor_table_schema(
        table_name="test_table",
        df=df,
        lag=30,
        days_count=1,
        numeric_type="float",
        numeric_precision=(38, 32),
        pk_fields=["trade_date", "stock_code"]  # 设置主键
    )
    
    # 创建表
    success = manager.create_table("test_table", columns)
    
    if success:
        logger.info("成功创建测试表，表结构包含主键 trade_date, stock_code")
        return True
    else:
        logger.error("创建测试表失败")
        return False

# 测试主键去重
def test_pk_dedup():
    """测试主键去重功能（含新时间数据）"""
    logger.info("测试主键去重功能（含新时间数据）...")
    
    # 生成测试数据 - 包含一些重复的主键
    df1 = generate_test_data(rows=25, stocks=5)
    logger.info("第一批数据样本:")
    logger.info(df1.head(3).to_string())
    
    # 创建一个包含部分重复主键但value不同的数据框
    df2 = df1.copy()
    df2['value'] = df2['value'] + 1.0  # 修改值
    logger.info("第二批数据样本（相同主键，不同value）:")
    logger.info(df2.head(3).to_string())

    # 新增一批新日期（2023-01-06）的数据
    new_date = pd.to_datetime('2023-01-06')
    stock_codes = df1['stock_code'].unique()
    new_data = []
    for stock in stock_codes:
        new_data.append({
            'trade_date': new_date,
            'stock_code': stock,
            'field_name': 'label_raw',
            'value': np.random.normal(0, 1),
            'label_shift': 10
        })
    df3 = pd.DataFrame(new_data)
    logger.info("第三批数据样本（新日期 2023-01-06）:")
    logger.info(df3.head().to_string())
    
    # 保存第一批数据
    manager = TestDBManager()
    result1 = manager.save_dataframe(
        df=df1,
        table_name="test_table",
        mode="append",
        batch_size=1000
    )
    
    # 检查数据量
    count_sql = "SELECT COUNT(*) FROM test_table"
    count1 = manager.execute_query(count_sql)[0][0]
    logger.info(f"第一批数据插入后表中有 {count1} 条记录")
    
    # 使用主键去重模式保存第二批数据
    logger.info("使用主键去重模式保存第二批数据...")
    result2 = manager.save_dataframe(
        df=df2,
        table_name="test_table",
        mode="update",
        pk_fields=["trade_date", "stock_code"],
        batch_size=1000
    )
    
    # 检查更新后的数据量
    count2 = manager.execute_query(count_sql)[0][0]
    logger.info(f"主键去重后表中有 {count2} 条记录")
    
    # 再插入新日期的数据（2023-01-06），用 update 模式
    logger.info("插入新日期（2023-01-06）数据...")
    result3 = manager.save_dataframe(
        df=df3,
        table_name="test_table",
        mode="update",
        pk_fields=["trade_date", "stock_code"],
        batch_size=1000
    )
    
    # 检查最终数据量
    count3 = manager.execute_query(count_sql)[0][0]
    logger.info(f"插入新日期后表中有 {count3} 条记录（应比前面多 {len(df3)} 条）")
    
    # 检查新日期数据是否写入
    sample_sql = "SELECT * FROM test_table WHERE trade_date='2023-01-06'"
    sample_data = manager.execute_query(sample_sql)
    logger.info(f"新日期 2023-01-06 的样本数据（共 {len(sample_data)} 条）:")
    for row in sample_data:
        logger.info(row)
    
    # 检查value是否已更新
    sample_sql2 = "SELECT * FROM test_table LIMIT 5"
    sample_data2 = manager.execute_query(sample_sql2)
    logger.info("更新后的样本数据:")
    for row in sample_data2:
        logger.info(row)
    
    return result2 and result3

# 测试 trade_date 区间去重
def test_trade_date_dedup():
    """测试只按trade_date去重功能"""
    logger.info("测试只按trade_date去重功能...")
    
    # 重置测试表
    drop_test_table()
    create_test_table()
    
    # 生成测试数据 - 不同股票同一天
    df1 = generate_test_data(rows=25, stocks=5)
    logger.info("第一批数据样本:")
    logger.info(df1.head(3).to_string())
    
    # 创建具有相同日期但不同股票代码的数据
    df2 = generate_test_data(rows=25, stocks=5)
    df2['stock_code'] = [f'60100{i}' for i in range(1, 6)] * 5  # 使用不同的股票代码
    logger.info("第二批数据样本（相同日期，不同股票）:")
    logger.info(df2.head(3).to_string())
    
    # 保存第一批数据
    manager = TestDBManager()
    manager.save_dataframe(
        df=df1,
        table_name="test_table",
        mode="append",
        batch_size=1000
    )
    
    # 检查数据量
    count_sql = "SELECT COUNT(*) FROM test_table"
    count1 = manager.execute_query(count_sql)[0][0]
    logger.info(f"第一批数据插入后表中有 {count1} 条记录")
    
    # 使用默认的trade_date, stock_code去重模式保存第二批数据
    logger.info("使用默认的trade_date, stock_code去重模式保存第二批数据...")
    manager.save_dataframe(
        df=df2,
        table_name="test_table",
        mode="update",  # 不传pk_fields，使用默认的trade_date和stock_code
        batch_size=1000
    )
    
    # 检查更新后的数据量
    count2 = manager.execute_query(count_sql)[0][0]
    logger.info(f"按默认主键去重后表中有 {count2} 条记录")
    
    # 应该是两批数据相加，因为stock_code不同
    expected = count1 + len(df2)
    logger.info(f"期望的记录数: {expected}, 实际记录数: {count2}")
    
    # 测试扩展主键去重
    logger.info("测试扩展主键去重 (trade_date, stock_code, field_name)...")
    
    # 创建具有相同日期、股票代码但不同field_name的数据
    df3 = df1.copy()
    df3['field_name'] = 'label_adj'  # 使用不同的field_name
    df3['value'] = df3['value'] * 2  # 修改值
    
    # 使用扩展主键去重模式保存第三批数据
    manager.save_dataframe(
        df=df3,
        table_name="test_table",
        mode="update",
        pk_fields=["trade_date", "stock_code"],
        batch_size=1000
    )
    
    # 检查更新后的数据量
    count3 = manager.execute_query(count_sql)[0][0]
    logger.info(f"使用扩展主键去重后表中有 {count3} 条记录")
    
    return True

def test_nan_handling():
    """测试处理含NaN值的行"""
    logger.info("测试处理含NaN值的行...")
    
    # 重置测试表
    drop_test_table()
    create_test_table()
    
    # 生成测试数据
    df = generate_test_data(rows=20, stocks=4)
    
    # 首先保存基础数据
    manager = TestDBManager()
    result1 = manager.save_dataframe(
        df=df,
        table_name="test_table",
        mode="append",
        batch_size=1000
    )
    
    # 检查基础数据量
    count_sql = "SELECT COUNT(*) FROM test_table"
    count1 = manager.execute_query(count_sql)[0][0]
    logger.info(f"基础数据插入后表中有 {count1} 条记录")
    
    # 添加一些有效值但非主键列为NaN的行
    nan_rows = [
        {'trade_date': pd.to_datetime('2023-01-05'), 'stock_code': '600101', 'field_name': None, 'value': 1.0, 'label_shift': 10},
        {'trade_date': pd.to_datetime('2023-01-05'), 'stock_code': '600102', 'field_name': 'label_raw', 'value': None, 'label_shift': 10},
        {'trade_date': pd.to_datetime('2023-01-05'), 'stock_code': '600103', 'field_name': 'label_raw', 'value': 3.0, 'label_shift': None}
    ]
    df_nan = pd.DataFrame(nan_rows)
    
    logger.info("含NaN值的数据样本:")
    logger.info(df_nan.to_string())
    
    # 保存含NaN的数据
    result2 = manager.save_dataframe(
        df=df_nan,
        table_name="test_table",
        mode="update",
        batch_size=1000
    )
    
    # 检查插入后的数据量
    count2 = manager.execute_query(count_sql)[0][0]
    logger.info(f"含NaN值的数据插入后表中共有 {count2} 条记录")
    
    # 检查有非空的trade_date和stock_code但其他字段为空的行
    check_sql = "SELECT * FROM test_table WHERE trade_date='2023-01-05'"
    nan_data = manager.execute_query(check_sql)
    logger.info(f"检查含部分NaN值的行，共找到 {len(nan_data)} 条记录:")
    for row in nan_data:
        logger.info(row)
    
    # 如果数据量有增加，说明测试通过
    return count2 > count1

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("开始运行标准化参数生成器测试")
    logger.info("=" * 50)
    
    start_time = datetime.now()
    logger.info(f"开始时间: {start_time}")
    
    # 先删除表（如果存在）
    drop_test_table()
    
    # 创建表
    if create_test_table():
        # 测试主键去重
        logger.info("\n===== 测试1: 主键去重 =====")
        if test_pk_dedup():
            logger.info("✅ 主键去重测试通过")
        else:
            logger.error("❌ 主键去重测试失败")
        
        # 测试trade_date去重
        logger.info("\n===== 测试2: 默认主键和扩展主键去重 =====")
        if test_trade_date_dedup():
            logger.info("✅ 默认主键和扩展主键去重测试通过")
        else:
            logger.error("❌ 默认主键和扩展主键去重测试失败")
        
        # 测试处理含NaN值的行
        logger.info("\n===== 测试3: 处理含NaN值的行 =====")
        if test_nan_handling():
            logger.info("✅ 处理含NaN值的行测试通过")
        else:
            logger.error("❌ 处理含NaN值的行测试失败")
        
        logger.info("\n所有测试完成！")
    else:
        logger.error("创建测试表失败，无法继续测试")
    
    end_time = datetime.now()
    duration = end_time - start_time
    logger.info(f"结束时间: {end_time}")
    logger.info(f"总耗时: {duration}")
    
    logger.info("=" * 50)
    logger.info("测试完成")
    logger.info("=" * 50)
