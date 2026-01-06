"""
Space Signals Pipeline - 二级分类因子入库模块

提供：
- 二级分类映射加载
- 表名生成和建表工具
"""

from .mapping import FactorMapping
from .table_utils import (
    generate_table_name,
    get_schema_from_config,
    ensure_factor_table,
    validate_table_structure,
    get_table_info
)

__all__ = [
    'FactorMapping',
    'generate_table_name',
    'get_schema_from_config',
    'ensure_factor_table',
    'validate_table_structure',
    'get_table_info',
]

