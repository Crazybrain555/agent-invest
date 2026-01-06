"""
Data normalization and standardization utilities v2024 - 因子工程模式.

基于窗口的因子工程数据归一化器（长表格式），实现系统性的因子构建：
- 价格字段: 分母统一用adj_close，结果log，生成MAR和ROC因子
- 体量/资金流字段: 分母用自身，结果log，生成MAR和ROC因子
- 比例字段: window=0保留原值，其他和体量一样处理
- 状态字段: 只允许window=0，保留原值

参考蓝图: docs/factor_processing_v2_blueprint.md
"""
import pandas as pd
import numpy as np
from typing import Union, List, Optional, Dict, Any, Tuple
from functools import lru_cache
from src.utils.logger import setup_logger
from .norm_config import (
    FieldCategoryFactorEng, get_field_category_factor_engineering, 
    validate_field_window_config_factor_engineering,
    Z_WINDOW_MAP_FACTOR_ENGINEERING, DEFAULT_Z_WINDOWS
)
import logging
import re
from tqdm import tqdm
import weakref

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 全局常量配置
DEFAULT_LOOKBACK_PERIODS = 120

# 全局弱引用字典，用于缓存columns列表
class _ColumnsWrapper:
    """包装类，使list可以被弱引用"""
    def __init__(self, columns):
        self.columns = list(columns)

_cols_registry = weakref.WeakValueDictionary()

class DataNormalizer:
    """
    基于因子工程的数据归一化器，支持对"长表"格式的数据进行窗口化因子构建。
    
    支持的处理方法:
        - factor_engineering: 新的因子工程方法，基于窗口生成MAR和ROC因子
        - academic: 原有的学术标准方法（向后兼容）
    """

    def __init__(
        self,
        lookback_periods: int = DEFAULT_LOOKBACK_PERIODS,
        preserve_precision: bool = True,
        enable_parallel: bool = True,
        logger: Optional[logging.Logger] = None,
        provider = None  # MarketDataProvider实例，用于自动拉取辅助数据
    ):
        """
        初始化数据归一化器
        
        Args:
            lookback_periods: 历史数据回看期数（交易日）
            preserve_precision: 是否保持float64精度（用于数据库存储）
            enable_parallel: 是否启用并行处理
            logger: 日志记录器
            provider: MarketDataProvider实例，用于自动拉取辅助数据
        """
        self.lookback_periods = lookback_periods
        self.preserve_precision = preserve_precision
        self.enable_parallel = enable_parallel
        
        # 日志配置
        self.logger = logger or logging.getLogger(__name__)
        
        # 警告标志，避免重复日志刷屏
        self._warning_flags = set()
        
        # 辅助数据缓存（解决分批处理时缺少adj_close数据的问题）
        self.provider = provider
        self._adj_close_cache_long = pd.DataFrame()

    def normalize_data_factor_engineering(
        self,
        df: pd.DataFrame,
        field_window_config: Optional[Dict[str, List[int]]] = None,
        enable_validation: bool = True,
        **kwargs
    ) -> pd.DataFrame:
        """
        因子工程数据处理方法 - 基于窗口的因子工程
        
        Args:
            df: 输入数据 [trade_date, stock_code, field_name, value]
            field_window_config: 字段窗口配置 {field_name: [window1, window2, ...]}
            enable_validation: 是否启用配置验证
            
        Returns:
            处理后的数据 [trade_date, stock_code, factor_name, factor_value, z_windows]
        """
        # 验证输入
        required_cols = {'trade_date', 'stock_code', 'field_name', 'value'}
        if not required_cols.issubset(df.columns):
            raise ValueError(f"输入数据必须包含列: {required_cols}")
        
        # 使用默认配置如果没有提供
        if field_window_config is None:
            field_window_config = Z_WINDOW_MAP_FACTOR_ENGINEERING.copy()
        
        # 验证配置
        if enable_validation:
            for field_name, windows in field_window_config.items():
                validate_field_window_config_factor_engineering(field_name, windows)
        
        # 主处理逻辑
        return self._process_factors_engineering(df, field_window_config)

    def _process_factors_engineering(self, df: pd.DataFrame, field_window_config: Dict[str, List[int]]) -> pd.DataFrame:
        """因子工程主处理逻辑实现"""
        results = []
        
        # 按字段分组处理
        unique_fields = df['field_name'].unique()
        self.logger.info(f"开始处理 {len(unique_fields)} 个字段的因子工程")
        
        for field_name in tqdm(unique_fields, desc="处理字段", unit="field"):
            field_data = df[df['field_name'] == field_name].copy()
            category = get_field_category_factor_engineering(field_name)
            windows = field_window_config.get(field_name, DEFAULT_Z_WINDOWS)
            
            self.logger.debug(f"处理字段 {field_name}，分类: {category.value}，窗口: {windows}")
            
            # 显示更详细的进度信息
            if len(windows) > 1:
                window_desc = f"[{','.join(map(str, windows))}]"
            else:
                window_desc = f"[{windows[0]}]"
            tqdm.write(f"   {field_name} ({category.value}) 窗口: {window_desc}")
            
            for window in windows:
                try:
                    if category == FieldCategoryFactorEng.PRICE:
                        result = self._process_price_field_engineering(field_data, field_name, window)
                    elif category in [FieldCategoryFactorEng.VOLUME, FieldCategoryFactorEng.VALUE, FieldCategoryFactorEng.FORECAST]:
                        result = self._process_volume_like_field_engineering(field_data, field_name, window)
                    elif category == FieldCategoryFactorEng.RATIO:
                        result = self._process_ratio_field_engineering(field_data, field_name, window)
                    elif category == FieldCategoryFactorEng.STATUS:
                        result = self._process_status_field_engineering(field_data, field_name, window)
                    elif category == FieldCategoryFactorEng.TECHNICAL:
                        result = self._process_technical_field_engineering(field_data, field_name, window)
                    else:
                        self.logger.warning(f"未知分类 {category} 对字段 {field_name}")
                        continue
                        
                    results.append(result)
                    
                except Exception as e:
                    self.logger.error(f"处理字段 {field_name} 窗口 {window} 时发生错误: {str(e)}")
                    continue
        
        if not results:
            self.logger.warning("没有生成任何因子结果")
            return pd.DataFrame()
        
        self.logger.info(f"合并 {len(results)} 个因子结果...")
        final_result = pd.concat(results, ignore_index=True)
        
        # 确保列的顺序
        column_order = ['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']
        final_result = final_result[column_order]
        
        # 统一数据类型
        if self.preserve_precision:
            final_result['factor_value'] = final_result['factor_value'].astype('float64')
        else:
            final_result['factor_value'] = final_result['factor_value'].astype('float32')
        
        self.logger.info(f"因子工程处理完成，生成 {len(final_result)} 行因子数据")
        return final_result

    def _process_price_field_engineering(self, df: pd.DataFrame, field_name: str, window: int) -> pd.DataFrame:
        """
        价格字段处理逻辑
        特点：分母统一使用 adj_close，结果都要log
        
        NaN处理规则：
        1. 分子为nan直接传播
        2. 分母滑动平均：数据点数小于min(10, window//2)就是nan，window=1时分母不是nan就ok
        3. ROC分母：如果是nan，从前值找最近的非nan值填充
        """
        df = df.sort_values(['stock_code', 'trade_date']).copy()
        results = []
        
        if window == 0:
            # window=0: log1p(price), 保留所有行（包括nan）
            df_out = df.copy()
            df_out['factor_value'] = df_out['value']  # 先复制原值
            
            # 只对有效且大于0的数值进行log1p变换，其他保持原值（包括nan）
            valid_positive_mask = (pd.to_numeric(df_out['value'], errors='coerce').notna() & 
                                  (pd.to_numeric(df_out['value'], errors='coerce') > 0))
            df_out.loc[valid_positive_mask, 'factor_value'] = np.log1p(df_out.loc[valid_positive_mask, 'value'])
            
            # 对于0值，设为nan（因为log1p(0)=0，但价格为0通常是异常情况）
            zero_mask = (pd.to_numeric(df_out['value'], errors='coerce') == 0)
            df_out.loc[zero_mask, 'factor_value'] = np.nan
            
            df_out['factor_name'] = field_name
            df_out['z_windows'] = 0
            results.append(df_out[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
        
        else:
            # 需要获取adj_close数据作为分母
            adj_close_data = self._get_adj_close_for_batch(df)
            if adj_close_data.empty:
                raise ValueError(f"缺少adj_close数据用于处理价格字段 {field_name}，这是必需的分母数据！")
            
            merged = df.merge(adj_close_data, on=['trade_date', 'stock_code'], how='left')
            
            # 分子有效性检查（分子为nan直接传播）
            numerator_valid = pd.to_numeric(merged['value'], errors='coerce').notna() & (merged['value'] > 0)
            
            # 计算移动平均分母，使用智能min_periods
            if window == 1:
                min_periods_ma = 1  # window=1时分母不是nan就ok
            else:
                min_periods_ma = min(10, window // 2)  # 数据点数小于min(10, window//2)就是nan
            
            merged['adj_close_ma'] = (merged.groupby('stock_code')['adj_close']
                                     .transform(lambda x: x.shift(1).rolling(window, min_periods=min_periods_ma).mean()))
            
            # 计算ROC分母，并进行前向填充（只填充分母的nan，不影响分子）
            merged['adj_close_roc'] = merged.groupby('stock_code')['adj_close'].shift(window)
            # 对ROC分母进行前向填充：从前值找最近的非nan值填充
            merged['adj_close_roc'] = merged.groupby('stock_code')['adj_close_roc'].ffill()
            
            # 生成MAR因子: log(adj_xxxx / adj_close_ma)
            denominator_ma_valid = pd.to_numeric(merged['adj_close_ma'], errors='coerce').notna() & (merged['adj_close_ma'] > 0)
            valid_mar_mask = numerator_valid & denominator_ma_valid
            
            df_mar = merged.copy()
            df_mar['factor_value'] = np.nan  # 默认为nan
            if valid_mar_mask.sum() > 0:
                df_mar.loc[valid_mar_mask, 'factor_value'] = np.log(df_mar.loc[valid_mar_mask, 'value'] / df_mar.loc[valid_mar_mask, 'adj_close_ma'])
            df_mar['factor_name'] = f"{field_name}_mar"
            df_mar['z_windows'] = window
            results.append(df_mar[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
            
            # 生成ROC因子: log(adj_xxxx / adj_close_roc)
            denominator_roc_valid = pd.to_numeric(merged['adj_close_roc'], errors='coerce').notna() & (merged['adj_close_roc'] > 0)
            valid_roc_mask = numerator_valid & denominator_roc_valid
            
            df_roc = merged.copy()
            df_roc['factor_value'] = np.nan  # 默认为nan
            if valid_roc_mask.sum() > 0:
                df_roc.loc[valid_roc_mask, 'factor_value'] = np.log(df_roc.loc[valid_roc_mask, 'value'] / df_roc.loc[valid_roc_mask, 'adj_close_roc'])
            df_roc['factor_name'] = f"{field_name}_roc"
            df_roc['z_windows'] = window
            results.append(df_roc[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
        
        if not results:
            return pd.DataFrame()

        return pd.concat(results, ignore_index=True)

    def _process_volume_like_field_engineering(self, df: pd.DataFrame, field_name: str, window: int) -> pd.DataFrame:
        """
        体量/资金流字段处理逻辑
        特点：分母使用自身的历史值，结果都使用signed-log变换处理负值
        
        NaN处理规则：
        1. 分子为nan直接传播
        2. 分母滑动平均：数据点数小于min(10, window//2)就是nan，window=1时分母不是nan就ok
        3. ROC分母：如果是nan，从前值找最近的非nan值填充
        """
        df = df.sort_values(['stock_code', 'trade_date']).copy()
        results = []
        
        if window == 0:
            # window=0: signed_log(volume), 保留所有行（包括nan）
            df_out = df.copy()
            df_out['factor_value'] = df_out['value']  # 先复制原值
            
            # 只对有效数值进行signed-log变换（允许负值），其他保持原值（包括nan）
            valid_mask = pd.to_numeric(df_out['value'], errors='coerce').notna()
            df_out.loc[valid_mask, 'factor_value'] = self._apply_signed_log(df_out.loc[valid_mask, 'value'])
            
            df_out['factor_name'] = field_name
            df_out['z_windows'] = 0
            results.append(df_out[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
        
        else:
            # 分子有效性检查（分子为nan直接传播，不要求正数）
            numerator_valid = pd.to_numeric(df['value'], errors='coerce').notna()
            
            # 计算移动平均分母，使用智能min_periods
            if window == 1:
                min_periods_ma = 1  # window=1时分母不是nan就ok
            else:
                min_periods_ma = min(10, window // 2)  # 数据点数小于min(10, window//2)就是nan
            
            df['value_ma'] = (df.groupby('stock_code')['value']
                             .transform(lambda x: x.shift(1).rolling(window, min_periods=min_periods_ma).mean()))
            
            # 计算ROC分母，并进行前向填充（只填充分母的nan，不影响分子）
            df['value_roc'] = df.groupby('stock_code')['value'].shift(window)
            # 对ROC分母进行前向填充：从前值找最近的非nan值填充
            df['value_roc'] = df.groupby('stock_code')['value_roc'].ffill()
            
            # 生成MAR因子: signed_log(value / value_ma)
            denominator_ma_valid = pd.to_numeric(df['value_ma'], errors='coerce').notna()
            valid_mar_mask = numerator_valid & denominator_ma_valid
            
            df_mar = df.copy()
            df_mar['factor_value'] = np.nan  # 默认为nan
            if valid_mar_mask.sum() > 0:
                # 计算比率，然后应用signed-log变换（signed_log会安全处理inf情况）
                ratio_mar = df_mar.loc[valid_mar_mask, 'value'] / df_mar.loc[valid_mar_mask, 'value_ma']
                df_mar.loc[valid_mar_mask, 'factor_value'] = self._apply_signed_log(ratio_mar)
            df_mar['factor_name'] = f"{field_name}_mar"
            df_mar['z_windows'] = window
            results.append(df_mar[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
            
            # 生成ROC因子: signed_log(value / value_roc)
            denominator_roc_valid = pd.to_numeric(df['value_roc'], errors='coerce').notna()
            valid_roc_mask = numerator_valid & denominator_roc_valid
            
            df_roc = df.copy()
            df_roc['factor_value'] = np.nan  # 默认为nan
            if valid_roc_mask.sum() > 0:
                # 计算比率，然后应用signed-log变换（signed_log会安全处理inf情况）
                ratio_roc = df_roc.loc[valid_roc_mask, 'value'] / df_roc.loc[valid_roc_mask, 'value_roc']
                df_roc.loc[valid_roc_mask, 'factor_value'] = self._apply_signed_log(ratio_roc)
            df_roc['factor_name'] = f"{field_name}_roc"
            df_roc['z_windows'] = window
            results.append(df_roc[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
        
        if not results:
            return pd.DataFrame()

        return pd.concat(results, ignore_index=True)

    def _process_ratio_field_engineering(self, df: pd.DataFrame, field_name: str, window: int) -> pd.DataFrame:
        """
        比例字段处理逻辑
        特点：window=0时保留原值，其他时候使用signed-log变换处理可能包含负值的比率字段
        """
        df = df.sort_values(['stock_code', 'trade_date']).copy()
        
        if window == 0:
            # window=0: 保留原值（不做log1p），保留所有行（包括nan）
            df_out = df.copy()
            df_out['factor_value'] = df_out['value']  # 直接复制原值（包括nan）
            df_out['factor_name'] = field_name
            df_out['z_windows'] = 0
            return df_out[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']]
        
        else:
            # window>=1: 使用signed-log变换处理可能包含负值的比率字段
            return self._process_ratio_field_with_signed_log(df, field_name, window)
    
    def _apply_signed_log(self, series: pd.Series, eps: float = 1e-12) -> pd.Series:
        """
        应用signed-log变换：sgn(x) · log(|x| + 1)
        可容纳负数的变换，保持符号、压缩尾部，又不牺牲0附近的线性可解释性
        
        Args:
            series: 输入序列
            eps: 极小值阈值，小于该值的绝对值会被视为0
            
        Returns:
            signed-log变换后的序列
        """
        # 处理inf和nan
        series_clean = series.copy()
        
        # 将inf和-inf替换为nan（这样后续处理会自动跳过）
        series_clean = series_clean.replace([np.inf, -np.inf], np.nan)
        
        # 极小值归0，防止浮点误差
        series_clean = np.where(np.abs(series_clean) < eps, 0.0, series_clean)
        
        # 应用signed-log变换
        return np.sign(series_clean) * np.log1p(np.abs(series_clean))
    
    def _process_ratio_field_with_signed_log(self, df: pd.DataFrame, field_name: str, window: int) -> pd.DataFrame:
        """
        使用signed-log变换处理比率字段
        特点：可以处理负值，使用signed-log变换
        
        NaN处理规则：
        1. 分子为nan直接传播
        2. 分母滑动平均：数据点数小于min(10, window//2)就是nan，window=1时分母不是nan就ok
        3. ROC分母：如果是nan，从前值找最近的非nan值填充
        """
        df = df.sort_values(['stock_code', 'trade_date']).copy()
        results = []
        
        # 分子有效性检查（分子为nan直接传播，但不要求正数）
        numerator_valid = pd.to_numeric(df['value'], errors='coerce').notna()
        
        # 计算移动平均分母，使用智能min_periods
        if window == 1:
            min_periods_ma = 1  # window=1时分母不是nan就ok
        else:
            min_periods_ma = min(10, window // 2)  # 数据点数小于min(10, window//2)就是nan
        
        df['value_ma'] = (df.groupby('stock_code')['value']
                         .transform(lambda x: x.shift(1).rolling(window, min_periods=min_periods_ma).mean()))
        
        # 计算ROC分母，并进行前向填充（只填充分母的nan，不影响分子）
        df['value_roc'] = df.groupby('stock_code')['value'].shift(window)
        # 对ROC分母进行前向填充：从前值找最近的非nan值填充
        df['value_roc'] = df.groupby('stock_code')['value_roc'].ffill()
        
        # 生成MAR因子: signed_log(value / value_ma)
        denominator_ma_valid = pd.to_numeric(df['value_ma'], errors='coerce').notna()
        valid_mar_mask = numerator_valid & denominator_ma_valid
        
        df_mar = df.copy()
        df_mar['factor_value'] = np.nan  # 默认为nan
        if valid_mar_mask.sum() > 0:
            # 计算比率，然后应用signed-log变换（signed_log会安全处理inf情况）
            ratio_mar = df_mar.loc[valid_mar_mask, 'value'] / df_mar.loc[valid_mar_mask, 'value_ma']
            df_mar.loc[valid_mar_mask, 'factor_value'] = self._apply_signed_log(ratio_mar)
        df_mar['factor_name'] = f"{field_name}_mar"
        df_mar['z_windows'] = window
        results.append(df_mar[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
        
        # 生成ROC因子: signed_log(value / value_roc)
        denominator_roc_valid = pd.to_numeric(df['value_roc'], errors='coerce').notna()
        valid_roc_mask = numerator_valid & denominator_roc_valid
        
        df_roc = df.copy()
        df_roc['factor_value'] = np.nan  # 默认为nan
        if valid_roc_mask.sum() > 0:
            # 计算比率，然后应用signed-log变换（signed_log会安全处理inf情况）
            ratio_roc = df_roc.loc[valid_roc_mask, 'value'] / df_roc.loc[valid_roc_mask, 'value_roc']
            df_roc.loc[valid_roc_mask, 'factor_value'] = self._apply_signed_log(ratio_roc)
        df_roc['factor_name'] = f"{field_name}_roc"
        df_roc['z_windows'] = window
        results.append(df_roc[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']])
        
        if not results:
            return pd.DataFrame()

        return pd.concat(results, ignore_index=True)

    def _process_status_field_engineering(self, df: pd.DataFrame, field_name: str, window: int) -> pd.DataFrame:
        """
        状态字段处理逻辑
        特点：只允许window=0，保留原值
        """
        if window != 0:
            raise ValueError(f"状态字段 {field_name} 只能设置 z_windows=0，当前值: {window}")
        
        df_out = df.copy()
        # 状态字段保留原值，包括nan
        df_out['factor_value'] = df_out['value']  # 直接复制原值（包括nan）
        df_out['factor_name'] = field_name
        df_out['z_windows'] = 0
        
        return df_out[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']]

    def _process_technical_field_engineering(self, df: pd.DataFrame, field_name: str, window: int) -> pd.DataFrame:
        """
        技术指标字段处理逻辑
        特点：优先window=0，和比例字段类似的处理
        """
        return self._process_ratio_field_engineering(df, field_name, window)

    def _get_adj_close_for_batch(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        获取adj_close数据用于价格字段处理
        
        如果获取不到adj_close数据，会抛出异常，因为这是处理价格字段的必需数据
        """
        # 1) 检查当前批次是否包含adj_close
        if 'adj_close' in df['field_name'].unique():
            adj_close_data = df[df['field_name'] == 'adj_close'][['trade_date', 'stock_code', 'value']].copy()
            adj_close_data = adj_close_data.rename(columns={'value': 'adj_close'})
                
            # 缓存结果
            if not adj_close_data.empty:
                self._adj_close_cache_long = adj_close_data.copy()
                self.logger.debug(f"缓存adj_close数据: {len(adj_close_data)} 行")
                
            return adj_close_data

        # 2) 使用缓存的adj_close数据
        if not self._adj_close_cache_long.empty:
            self.logger.debug("使用缓存的adj_close数据")
            return self._adj_close_cache_long

        # 3) 主动拉取adj_close数据
        self.logger.info("当前批次缺adj_close，尝试自动拉取...")
        if self.provider is None:
            try:
                from src.data_service.data_loading.market_data import MarketDataProvider
                self.provider = MarketDataProvider()
            except ImportError as e:
                raise ValueError(f"无法导入MarketDataProvider且缺少adj_close数据: {e}")

        start_date, end_date = self._get_batch_range(df)
        stock_codes = df['stock_code'].unique().tolist() if 'stock_code' in df.columns else None
        
        # 添加更多的回看天数以获取足够的历史数据
        try:
            # 获取更大范围的数据以确保有足够的历史数据
            extended_start = pd.to_datetime(start_date) - pd.DateOffset(days=180)  # 向前推6个月
            extended_start_str = extended_start.strftime("%Y%m%d")
            
            fetch_df = self.provider.fetch_data(
                fields=['adj_close'],
                start_date=extended_start_str,
                end_date=end_date,
                stock_codes=stock_codes,
                feature_lag=None,
                days_counted=1,
                format='long'
            )
            
            if fetch_df is None or fetch_df.empty:
                raise ValueError("自动拉取adj_close数据失败，无法处理价格字段")

            # 递归调用自己处理拉取到的数据
            result = self._get_adj_close_for_batch(fetch_df)
            if result.empty:
                raise ValueError("自动拉取的adj_close数据为空，无法处理价格字段")
            
            self.logger.info(f"成功自动拉取adj_close数据: {len(result)} 行")
            return result
            
        except Exception as e:
            # 如果所有方法都失败，抛出异常而不是返回空DataFrame
            raise ValueError(f"获取adj_close数据失败，无法处理价格字段: {e}")
    
    def _get_batch_range(self, df: pd.DataFrame) -> Tuple[str, str]:
        """获取批次日期范围（YYYYMMDD格式）"""
        try:
            min_dt = pd.to_datetime(df['trade_date'].min()).strftime("%Y%m%d")
            max_dt = pd.to_datetime(df['trade_date'].max()).strftime("%Y%m%d")
            return min_dt, max_dt
        except Exception as e:
            self.logger.warning(f"获取批次日期范围失败: {e}")
            return "20080101", "20250101"

    def clear_cache(self):
        """清空所有辅助数据缓存"""
        self._adj_close_cache_long = pd.DataFrame()
        self.logger.debug("已清空所有Normalizer缓存")

    def get_cache_status(self) -> Dict[str, int]:
        """获取缓存状态信息"""
        return {
            'adj_close_cache_long_rows': len(self._adj_close_cache_long)
        }

    # ========= 向后兼容方法 =========
    def normalize_data(
        self,
        df: pd.DataFrame,
        fields: Optional[List[str]] = None,
        method: str = 'factor_engineering',
        data_format: str = 'long',
        **kwargs
    ) -> pd.DataFrame:
        """
        统一归一化调用接口（向后兼容）
        """
        if method == 'factor_engineering':
            field_window_config = kwargs.get('z_windows', None)
            if isinstance(field_window_config, dict):
                return self.normalize_data_factor_engineering(df, field_window_config, **kwargs)
            else:
                return self.normalize_data_factor_engineering(df, **kwargs)
        elif method == 'academic':
            # 调用原有的学术标准化方法（简化版本）
            self.logger.warning("academic方法已简化，建议使用factor_engineering方法")
            return self._normalize_academic_simplified(df, fields, **kwargs)
        else:
            self.logger.error(f"未知的归一化方法: {method}")
            return df

    def _normalize_academic_simplified(self, df: pd.DataFrame, fields: List[str], **kwargs) -> pd.DataFrame:
        """简化的学术标准化方法，用于向后兼容"""
        # 简化实现，主要保持接口兼容
        self.logger.info("使用简化的学术标准化方法")
        
        # 基本数据验证
        if not {'field_name', 'value', 'trade_date', 'stock_code'}.issubset(df.columns):
            self.logger.error("数据格式不符合要求")
            return df
        
        # 简单的log1p处理
        df_result = df.copy()
        numeric_mask = pd.to_numeric(df_result['value'], errors='coerce').notna()
        df_result.loc[numeric_mask, 'value'] = np.log1p(np.abs(df_result.loc[numeric_mask, 'value']))
        
        # 添加z_windows列以保持兼容性
        if 'z_windows' not in df_result.columns:
            df_result['z_windows'] = 0
        
        return df_result

    @staticmethod
    def _get_base_field(col_name: str) -> str:
        """从列名中提取基础字段名"""
        pattern = r'(.+?)(?:_lag_(\d+))?$'
        match = re.match(pattern, col_name)
        if match:
            return match.group(1)
        return col_name
