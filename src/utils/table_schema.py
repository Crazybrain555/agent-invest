"""
TableSchemaBuilder 模块用于构建数据库表结构。

用法示例:
    from src.utils.table_schema import TableSchemaBuilder

    # 创建表结构
    columns = TableSchemaBuilder.create_factor_table_schema(
        table_name='my_table',
        df=my_dataframe,
        lag=30,
        days_count=1,
        numeric_type='float',
        numeric_precision=(38, 32)
    )

    # 使用 TestDBManager 创建表
    from src.data_service.data_saving.data_to_testdb import TestDBManager
    manager = TestDBManager()
    manager.create_table('my_table', columns)
"""

from typing import Dict, List, Any, Optional, Tuple, Union
from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime, Boolean, MetaData, Float, SmallInteger # Import SmallInteger
from datetime import datetime
import pandas as pd

class TableSchemaBuilder:
    """表结构构建器"""
    
    # 默认字段类型映射
    DEFAULT_TYPE_MAPPING = {
        'int': Integer,
        'str': String(50),
        'float': Float,
        'datetime': DateTime,
        'date': Date,
        'bool': Boolean
    }
    
    @classmethod
    def create_factor_table_schema(cls, 
                                 table_name: str,
                                 df: pd.DataFrame,
                                 lag: int = 30,
                                 days_count: int = 1,
                                 numeric_type: str = 'float',
                                 numeric_precision: Optional[Tuple[int, int]] = None,
                                 pk_fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """创建因子表的结构定义
        
        Args:
            table_name: 表名
            df: 数据框，如果提供则根据数据框结构生成表结构
            lag: 滞后特征数量
            days_count: 时间粒度（天数）
            numeric_type: 数值类型，'float'或'numeric'，默认为'float'
            numeric_precision: 当numeric_type为'numeric'时，指定精度和标度(precision, scale)，例如(10, 4)
            pk_fields: 主键字段列表，这些字段将设置 primary_key=True
            
        Returns:
            List[Dict[str, Any]]: 列定义列表
            
        Raises:
            TypeError: 如果 df 不是 pandas DataFrame 类型
        """
        if not isinstance(df, pd.DataFrame):
            raise TypeError(f"Expected pandas DataFrame, got {type(df)}")
            
        columns = []
        
        # 默认主键是 trade_date 和 stock_code
        if pk_fields is None:
            pk_fields = ['trade_date', 'stock_code']
        
        # 添加基础字段
        columns.extend([
            {'name': 'trade_date', 'type': Date, 'primary_key': 'trade_date' in pk_fields, 'nullable': False},
            {'name': 'stock_code', 'type': String(15), 'primary_key': 'stock_code' in pk_fields, 'nullable': False}
        ])
        
        # 确定数值列的类型
        numeric_column_type = Float
        if numeric_type.lower() == 'numeric' and numeric_precision:
            precision, scale = numeric_precision
            numeric_column_type = Numeric(precision, scale)
        
        # 添加因子字段
        for col in df.columns:
            if col not in ['trade_date', 'stock_code']:
                # 默认使用String类型
                col_type = String(20)
                
                # 尝试判断是否为数值类型
                try:
                    # 强制转换为数值类型，如果失败则保持String
                    pd.to_numeric(df[col], errors='raise')
                    col_type = numeric_column_type
                except (ValueError, TypeError):
                    # 如果转换失败，检查是否为其他已知类型
                    if pd.api.types.is_datetime64_any_dtype(df[col]):
                        col_type = DateTime
                    elif pd.api.types.is_bool_dtype(df[col]):
                        col_type = Boolean
                    # else: 保留String(20)作为默认值
                    else:
                        col_type = String(20)

                columns.append({
                    'name': col,
                    'type': col_type,
                    'primary_key': col in pk_fields
                })
        
        # 添加元数据字段
        columns.extend([
            {'name': 'model_version', 'type': String(15), 'primary_key': 'model_version' in pk_fields},
            {'name': 'insert_time', 'type': DateTime, 'primary_key': 'insert_time' in pk_fields},
            {'name': 'is_temporary', 'type': Boolean, 'primary_key': 'is_temporary' in pk_fields}
        ])
        
        return columns
    
    @classmethod
    def create_table_schema(cls, 
                          table_name: str,
                          df: Optional[Any] = None,
                          custom_columns: Optional[List[Dict[str, Any]]] = None,
                          numeric_type: str = 'float',
                          numeric_precision: Optional[Tuple[int, int]] = None) -> List[Dict[str, Any]]:
        """创建通用表结构
        
        Args:
            table_name: 表名
            df: 数据框，如果提供则根据数据框结构生成表结构
            custom_columns: 自定义列定义列表
            numeric_type: 数值类型，'float'或'numeric'，默认为'float'
            numeric_precision: 当numeric_type为'numeric'时，指定精度和标度(precision, scale)，例如(10, 4)
            
        Returns:
            List[Dict[str, Any]]: 列定义列表
        """
        if custom_columns:
            return custom_columns
            
        if df is None:
            raise ValueError("Either df or custom_columns must be provided")
        
        # 确定数值列的类型
        float_type = Float
        if numeric_type.lower() == 'numeric' and numeric_precision:
            precision, scale = numeric_precision
            float_type = Numeric(precision, scale)
            
        # 更新类型映射
        type_mapping = cls.DEFAULT_TYPE_MAPPING.copy()
        type_mapping['float'] = float_type
            
        columns = []
        for col in df.columns:
            # 根据列名和数据类型推断SQL类型
            dtype = str(df[col].dtype)
            if 'int' in dtype:
                col_type = type_mapping['int']
            elif 'float' in dtype:
                col_type = type_mapping['float']
            elif 'datetime' in dtype:
                col_type = type_mapping['datetime']
            elif 'date' in dtype:
                col_type = type_mapping['date']
            elif 'bool' in dtype:
                col_type = type_mapping['bool']
            else:
                col_type = type_mapping['str']
                
            columns.append({
                'name': col,
                'type': col_type
            })
            
        return columns 

    @classmethod
    def create_forbid_table_schema(cls) -> List[dict]:
        """创建禁投池表的 schema 定义 (Milestone 4)."""
        return [
            {"name": "trade_date",  "type": Date,       "primary_key": True,  "nullable": False},
            {"name": "stock_code",  "type": String(15), "primary_key": True,  "nullable": False},
            # Using SmallInteger for signal as it maps well to 0/1 and uses less space than Boolean in some DBs
            {"name": "signal",      "type": SmallInteger,"primary_key": False, "nullable": False},
            # Adding insert_time for tracking when the record was last updated/inserted
            {"name": "insert_time", "type": DateTime,   "primary_key": False, "nullable": False}
        ]

    @classmethod
    def create_stk_pool_table_schema(cls) -> List[dict]:
        """Create index stock pool table schema."""
        return [
            {"name": "trade_date",  "type": Date,        "primary_key": True,  "nullable": False},
            {"name": "pool_code",   "type": String(40),  "primary_key": True,  "nullable": False},
            {"name": "stock_code",  "type": String(15),  "primary_key": True,  "nullable": False},
            {"name": "signal",      "type": SmallInteger,"primary_key": False, "nullable": False},
            {"name": "insert_time", "type": DateTime,    "primary_key": False, "nullable": False},
        ]

    @classmethod
    def create_long_factor_table_schema(cls,
                                        numeric_type: str = 'numeric',
                                        numeric_precision: Optional[Tuple[int, int]] = (15, 6)) -> List[Dict[str, Any]]:
        """
        创建长表格式的因子表schema定义 (用于 inter_train_factors 表)
        
        Args:
            numeric_type: 数值类型，'float'或'numeric'，默认为'numeric'
            numeric_precision: 当numeric_type为'numeric'时，指定精度和标度(precision, scale)
            
        Returns:
            List[Dict[str, Any]]: 列定义列表
        """
        # 确定数值列的类型
        numeric_column_type = Float
        if numeric_type.lower() == 'numeric' and numeric_precision:
            precision, scale = numeric_precision
            numeric_column_type = Numeric(precision, scale)
        
        columns = [
            {'name': 'trade_date', 'type': Date, 'primary_key': True, 'nullable': False},
            {'name': 'stock_code', 'type': String(20), 'primary_key': True, 'nullable': False},
            {'name': 'factor_name', 'type': String(50), 'primary_key': True, 'nullable': False},
            {'name': 'factor_value', 'type': numeric_column_type, 'primary_key': False, 'nullable': True},
            {'name': 'lag', 'type': Integer, 'primary_key': True, 'nullable': False, 'default': 0},
            {'name': 'z_windows', 'type': SmallInteger, 'primary_key': True, 'nullable': False, 'default': 1}
            # 删除model_version和is_temporary字段以节省空间
        ]
        
        return columns 
