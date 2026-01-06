"""
表名生成和建表工具模块

负责：
1. 根据一级/二级分类生成标准化表名
2. 确保因子表存在（使用正确的 SQLAlchemy 类型）
3. 从配置获取 schema
"""

from __future__ import annotations
import re
import logging
from typing import Optional

from sqlalchemy import Date, String, Float
from src.utils.config_loader import ConfigLoader
from src.data_service.data_saving.data_to_testdb import TestDBManager

logger = logging.getLogger(__name__)


def _sanitize(s: str) -> str:
    """
    清理字符串，用于生成表名
    
    规则：
    - 转小写
    - 非字母数字转下划线
    - 多个下划线合并
    - 去除首尾下划线
    
    Args:
        s: 输入字符串
    
    Returns:
        清理后的字符串
    """
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    s = re.sub(r'_+', '_', s).strip('_')
    return s


def generate_table_name(level1: str, level2: Optional[str] = None) -> str:
    """
    根据分类生成表名
    
    命名规则：
    - 二级分类: quantitative_{level1}_{level2}_signals
    - 仅一级分类: quantitative_{level1}_signals
    - other 类别: quantitative_other_signals
    
    Args:
        level1: 一级分类名称
        level2: 二级分类名称（可选）
    
    Returns:
        标准化的表名
    
    Examples:
        >>> generate_table_name('growth', 'efficiency')
        'quantitative_growth_efficiency_signals'
        >>> generate_table_name('value', None)
        'quantitative_value_signals'
        >>> generate_table_name('other', None)
        'quantitative_other_signals'
    """
    l1 = _sanitize(level1) if level1 else 'other'
    
    # 特殊处理 other 类别或无二级分类的情况
    if l1 == 'other' or not level2:
        table_name = f"quantitative_{l1}_signals"
        logger.debug(f"Generated flat table name: {table_name}")
        return table_name
    
    # 二级分类
    l2 = _sanitize(level2)
    table_name = f"quantitative_{l1}_{l2}_signals"
    logger.debug(f"Generated hierarchical table name: {table_name}")
    return table_name


def get_schema_from_config() -> Optional[str]:
    """
    从配置文件获取数据库 schema
    
    读取 configs/space_disk/space_config.yaml 中的 database.schema
    
    Returns:
        schema 名称（如 'ai_is'），未配置则返回 None
    """
    try:
        cfg = ConfigLoader(config_dir='configs').load_config("space_disk/space_config.yaml")
        schema = (cfg.get('database') or {}).get('schema')
        
        if schema:
            logger.debug(f"Loaded schema from config: {schema}")
        else:
            logger.debug("No schema configured, will use database default")
        
        return schema
    
    except Exception as e:
        logger.warning(f"Failed to load schema from config: {e}, using default")
        return None


def ensure_factor_table(
    db: TestDBManager, 
    table_name: str, 
    schema: Optional[str] = None
) -> bool:
    """
    确保因子表存在，若不存在则创建
    
    表结构：
    - trade_date: DATE, NOT NULL
    - stock_code: VARCHAR(10), NOT NULL
    - factor_name: VARCHAR(128), NOT NULL
    - factor_value: FLOAT, NULL
    
    唯一约束：(trade_date, stock_code, factor_name)
    注：唯一索引由 TestDBManager 的 update 模式自动创建和维护
    
    Args:
        db: 数据库管理器实例
        table_name: 表名
        schema: 数据库 schema（可选）
    
    Returns:
        True 表存在或创建成功，False 创建失败
    """
    # 检查表是否已存在
    if db.check_table_exists(table_name, schema=schema):
        logger.debug(f"Table already exists: {schema}.{table_name}" if schema else table_name)
        return True
    
    logger.info(f"Creating factor table: {schema}.{table_name}" if schema else table_name)
    
    # 定义表结构（使用 SQLAlchemy 类型对象，不是字符串）
    columns = [
        {
            'name': 'trade_date',
            'type': Date(),           # SQLAlchemy Date 类型
            'nullable': False
        },
        {
            'name': 'stock_code',
            'type': String(10),       # SQLAlchemy String 类型
            'nullable': False
        },
        {
            'name': 'factor_name',
            'type': String(128),      # SQLAlchemy String 类型
            'nullable': False
        },
        {
            'name': 'factor_value',
            'type': Float(),          # SQLAlchemy Float 类型
            'nullable': True
        },
    ]
    
    # 创建表
    try:
        success = db.create_table(
            table_name=table_name,
            columns=columns,
            schema=schema
        )
        
        if success:
            logger.info(f"Successfully created table: {schema}.{table_name}" if schema else table_name)
        else:
            logger.error(f"Failed to create table: {schema}.{table_name}" if schema else table_name)
        
        return success
    
    except Exception as e:
        logger.error(f"Error creating table {table_name}: {e}", exc_info=True)
        return False


def validate_table_structure(
    db: TestDBManager,
    table_name: str,
    schema: Optional[str] = None
) -> bool:
    """
    验证表结构是否符合预期（可选的健康检查功能）
    
    Args:
        db: 数据库管理器实例
        table_name: 表名
        schema: 数据库 schema（可选）
    
    Returns:
        True 结构正确，False 结构不符或验证失败
    """
    try:
        # 使用 SQLAlchemy inspector 检查列
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        
        columns = inspector.get_columns(table_name, schema=schema)
        column_names = {col['name'] for col in columns}
        
        required_columns = {'trade_date', 'stock_code', 'factor_name', 'factor_value'}
        
        if not required_columns.issubset(column_names):
            missing = required_columns - column_names
            logger.warning(f"Table {table_name} missing required columns: {missing}")
            return False
        
        logger.debug(f"Table {table_name} structure validated successfully")
        return True
    
    except Exception as e:
        logger.warning(f"Failed to validate table structure for {table_name}: {e}")
        return False


def get_table_info(
    db: TestDBManager,
    table_name: str,
    schema: Optional[str] = None
) -> Optional[dict]:
    """
    获取表的详细信息（用于调试和监控）
    
    Args:
        db: 数据库管理器实例
        table_name: 表名
        schema: 数据库 schema（可选）
    
    Returns:
        表信息字典，失败返回 None
    """
    try:
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        
        if not db.check_table_exists(table_name, schema=schema):
            return None
        
        columns = inspector.get_columns(table_name, schema=schema)
        indexes = inspector.get_indexes(table_name, schema=schema)
        pk = inspector.get_pk_constraint(table_name, schema=schema)
        
        return {
            'name': table_name,
            'schema': schema,
            'columns': columns,
            'indexes': indexes,
            'primary_key': pk
        }
    
    except Exception as e:
        logger.warning(f"Failed to get table info for {table_name}: {e}")
        return None

