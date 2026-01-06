"""
因子生成和处理工具包
用于模型回测中的因子相关功能
"""

from .factor_generator import FactorGenerator
from .factor_signal import build_signals
from .result_formatter import ResultFormatter
from .factor_saver import (
    FactorSaverFactory,
    FactorSaverManager,
    CSVFactorSaver,
    ParquetFactorSaver,
    DatabaseFactorSaver,
    save_factor_csv,
    save_factor_multi_format
)
from .factor_converter import (
    convert,
    convert_to_backtest,
    convert_to_wind,
    convert_to_live,
    get_supported_formats,
    validate_df_pred
)

__all__ = [
    "FactorGenerator",
    "build_signals", 
    "ResultFormatter",
    "FactorSaverFactory",
    "FactorSaverManager",
    "CSVFactorSaver",
    "ParquetFactorSaver", 
    "DatabaseFactorSaver",
    "save_factor_csv",
    "save_factor_multi_format",
    "convert",
    "convert_to_backtest",
    "convert_to_wind",
    "convert_to_live",
    "get_supported_formats",
    "validate_df_pred"
]