"""
Data standardization utilities for financial time series data.
"""
import pandas as pd
import numpy as np
from typing import Union, List, Optional, Dict, Any, Tuple
from src.utils.logger import setup_logger
import logging
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DataStandardizer:
    """
    通用数据标准化器，支持对"长表"或"宽表"格式的数据进行标准化参数提取和应用。
    
    主要功能:
    1. 从归一化数据中提取标准化参数（基于MAD的异常值裁剪 + 标准化）
    2. 应用标准化参数到新的数据上
    
    标准化参数包括:
    - upper: 上界 (median + m * MAD)
    - lower: 下界 (median - m * MAD)
    - mean: 均值
    - std: 标准差
    """

    def __init__(self):
        """初始化"""
        self.logger = setup_logger(__name__)

    @staticmethod
    def _extract_base_field_and_lag(col_name: str) -> Tuple[str, Optional[int]]:
        """
        从列名中提取基础字段名和滞后值
        Args:
            col_name: 列名，如 'adj_close_lag_0', 'volume_lag_1' 等
        Returns:
            Tuple[str, Optional[int]]: (基础字段名, 滞后值)
        """
        # 匹配模式：xxx_lag_N 或 xxx
        pattern = r'(.+?)(?:_lag_(\d+))?$'
        match = re.match(pattern, col_name)
        if match:
            base_field = match.group(1)
            lag = int(match.group(2)) if match.group(2) is not None else None
            return base_field, lag
        return col_name, None

    @staticmethod
    def _get_base_field(col_name: str) -> str:
        """
        从列名中提取基础字段名
        Args:
            col_name: 列名，如 'adj_close_lag_0', 'volume_lag_1' 等
        Returns:
            str: 基础字段名
        """
        base_field, _ = DataStandardizer._extract_base_field_and_lag(col_name)
        return base_field

    def extract_standard_params(
        self,
        df: pd.DataFrame,
        mad_multiplier: float = 7.0,
        data_format: str = 'wide',
        fields: Optional[List[str]] = None,
        **kwargs
    ) -> pd.DataFrame:
        """
        从归一化数据中提取标准化参数
        
        Args:
            df: 输入数据框（长表或宽表）
            mad_multiplier: MAD异常值裁剪的倍数，默认为7.0
            data_format: 数据格式，'long' 或 'wide'
            fields: 要处理的字段列表，如果为None则处理所有字段
            kwargs: 其他可选参数
            
        Returns:
            pd.DataFrame: 标准化参数，包含 'upper', 'lower', 'mean', 'std' 列
        """
        if data_format == 'long':
            return self._extract_standard_params_long(df, mad_multiplier, fields, **kwargs)
        else:
            return self._extract_standard_params_wide(df, mad_multiplier, fields, **kwargs)

    def _extract_standard_params_wide(
        self,
        df: pd.DataFrame,
        mad_multiplier: float = 7.0,
        fields: Optional[List[str]] = None,
        **kwargs
    ) -> pd.DataFrame:
        """
        从宽表格式的归一化数据中提取标准化参数
        
        Args:
            df: 输入数据框（宽表）
            mad_multiplier: MAD异常值裁剪的倍数
            fields: 要处理的字段列表，如果为None则处理所有字段
            kwargs: 其他可选参数
            
        Returns:
            pd.DataFrame: 标准化参数，包含 'upper', 'lower', 'mean', 'std' 列
        """
        # 如果fields为None，从列名中提取所有基础字段名
        if fields is None:
            fields = set()
            for col in df.columns:
                if col not in ['stock_code', 'trade_date']:  # 排除非特征列
                    base_field = self._get_base_field(col)
                    fields.add(base_field)
            fields = list(fields)
        else:
            # 验证指定的字段是否存在
            valid_fields = []
            for field in fields:
                if any(col.startswith(field) for col in df.columns):
                    valid_fields.append(field)
                else:
                    self.logger.warning(f"字段 {field} 在数据中不存在，跳过。")
            
            if not valid_fields:
                self.logger.error("没有有效的字段可以提取标准化参数。返回空数据框。")
                return pd.DataFrame(columns=['upper', 'lower', 'mean', 'std'])
            fields = valid_fields

        # 初始化结果数据框
        result = pd.DataFrame(index=df.columns, columns=['upper', 'lower', 'mean', 'std'])
        
        # 排除非特征列
        feature_cols = [col for col in df.columns if col not in ['stock_code', 'trade_date']]
        
        # 计算每个列的统计量
        for col in feature_cols:
            # 计算中位数和MAD
            median_val = df[col].median()
            mad_val = (df[col] - median_val).abs().median()  # median absolute deviation
            
            # 计算上下界
            lower_bound = median_val - mad_multiplier * mad_val
            upper_bound = median_val + mad_multiplier * mad_val
            
            # 裁剪异常值
            clipped_series = df[col].clip(lower=lower_bound, upper=upper_bound)
            
            # 计算均值和标准差
            mean_val = clipped_series.mean()
            std_val = clipped_series.std()
            
            # 存储结果
            result.loc[col, 'upper'] = upper_bound
            result.loc[col, 'lower'] = lower_bound
            result.loc[col, 'mean'] = mean_val
            result.loc[col, 'std'] = std_val
        
        return result

    def _extract_standard_params_long(
        self,
        df: pd.DataFrame,
        mad_multiplier: float = 7.0,
        fields: Optional[List[str]] = None,
        **kwargs
    ) -> pd.DataFrame:
        """
        从长表格式的归一化数据中提取标准化参数
        
        Args:
            df: 输入数据框（长表）
            mad_multiplier: MAD异常值裁剪的倍数
            fields: 要处理的字段列表，如果为None则处理所有字段
            kwargs: 其他可选参数
            
        Returns:
            pd.DataFrame: 标准化参数，包含 'upper', 'lower', 'mean', 'std' 列
        """
        # 验证长表格式
        if not {"field_name", "lag", "value"}.issubset(df.columns):
            self.logger.error("长表格式必须包含 [field_name, lag, value] 列。返回空数据框。")
            return pd.DataFrame(columns=['upper', 'lower', 'mean', 'std'])
        
        # 如果fields为None，获取所有唯一字段名
        if fields is None:
            fields = df['field_name'].unique().tolist()
        
        # 初始化结果数据框
        result = pd.DataFrame(columns=['upper', 'lower', 'mean', 'std'])
        
        # 对每个字段计算统计量
        for field in fields:
            # 获取该字段的所有数据
            field_data = df[df['field_name'] == field]['value']
            
            if field_data.empty:
                self.logger.warning(f"字段 {field} 在数据中为空，跳过。")
                continue
            
            # 计算中位数和MAD
            median_val = field_data.median()
            mad_val = (field_data - median_val).abs().median()  # median absolute deviation
            
            # 计算上下界
            lower_bound = median_val - mad_multiplier * mad_val
            upper_bound = median_val + mad_multiplier * mad_val
            
            # 裁剪异常值
            clipped_series = field_data.clip(lower=lower_bound, upper=upper_bound)
            
            # 计算均值和标准差
            mean_val = clipped_series.mean()
            std_val = clipped_series.std()
            
            # 存储结果
            result.loc[field, 'upper'] = upper_bound
            result.loc[field, 'lower'] = lower_bound
            result.loc[field, 'mean'] = mean_val
            result.loc[field, 'std'] = std_val
        
        return result

    def extract_standard_params_long_table(
        self,
        df: pd.DataFrame,
        mad_multiplier: float = 7.0,
        min_samples: int = 1000,
        factor_name_col: str = 'factor_name',
        factor_value_col: str = 'factor_value',
        window_col: str = 'z_windows',
        **kwargs
    ) -> pd.DataFrame:
        """
        从长表格式数据中提取标准化参数，支持 (factor_name, window) 组合
        
        Args:
            df: 输入数据框（长表格式）
            mad_multiplier: MAD异常值裁剪的倍数
            min_samples: 每个特征组合的最小样本数
            factor_name_col: 因子名称列名，默认 'factor_name'
            factor_value_col: 因子值列名，默认 'factor_value'
            window_col: 窗口列名，默认 'z_windows'
            kwargs: 其他可选参数
            
        Returns:
            pd.DataFrame: 标准化参数，包含 'feature_name', 'window', 'upper', 'lower', 'mean', 'std', 'sample_count' 列
        """
        # 验证必需的列是否存在
        required_cols = [factor_name_col, factor_value_col, window_col]
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            self.logger.error(f"长表格式缺少必需列: {missing_cols}")
            return pd.DataFrame(columns=['feature_name', 'window', 'upper', 'lower', 'mean', 'std', 'sample_count'])
        
        # 获取所有特征组合
        feature_combinations = df[[factor_name_col, window_col]].drop_duplicates()
        self.logger.info(f"发现 {len(feature_combinations)} 个特征组合")
        
        # 初始化结果列表
        results = []
        insufficient_data_count = 0
        
        # 对每个特征组合计算统计参数
        for _, row in feature_combinations.iterrows():
            factor_name = row[factor_name_col]
            window = row[window_col]
            
            # 获取该特征组合的数据
            mask = (df[factor_name_col] == factor_name) & (df[window_col] == window)
            feature_data = df.loc[mask, factor_value_col].dropna()
            
            if feature_data.empty:
                self.logger.warning(f"特征组合 ({factor_name}, {window}) 没有有效数据，跳过")
                continue
            
            # 检查数据量是否足够
            sample_count = len(feature_data)
            if sample_count < min_samples:
                self.logger.warning(f"特征组合 ({factor_name}, {window}) 数据量不足: {sample_count} < {min_samples}")
                insufficient_data_count += 1
                
                # 添加NaN结果
                results.append({
                    'feature_name': factor_name,
                    'window': window,
                    'upper': np.nan,
                    'lower': np.nan,
                    'mean': np.nan,
                    'std': np.nan,
                    'sample_count': sample_count
                })
                continue
            
            # 计算统计参数
            try:
                # 计算中位数和MAD
                median_val = feature_data.median()
                mad_val = (feature_data - median_val).abs().median()
                
                # 计算上下界
                upper_bound = median_val + mad_multiplier * mad_val
                lower_bound = median_val - mad_multiplier * mad_val
                
                # 裁剪异常值
                clipped_data = feature_data.clip(lower=lower_bound, upper=upper_bound)
                
                # 计算均值和标准差
                mean_val = clipped_data.mean()
                std_val = clipped_data.std()
                
                # 添加结果
                results.append({
                    'feature_name': factor_name,
                    'window': window,
                    'upper': upper_bound,
                    'lower': lower_bound,
                    'mean': mean_val,
                    'std': std_val,
                    'sample_count': sample_count
                })
                
            except Exception as e:
                self.logger.error(f"计算特征组合 ({factor_name}, {window}) 的统计参数失败: {str(e)}")
                continue
        
        # 转换为DataFrame
        result_df = pd.DataFrame(results)
        
        self.logger.info(f"成功处理 {len(result_df)} 个特征组合")
        if insufficient_data_count > 0:
            self.logger.info(f"其中 {insufficient_data_count} 个组合数据量不足，已设为NaN")
        
        return result_df

    def apply_standardization(
        self,
        df: pd.DataFrame,
        standard_params: pd.DataFrame,
        data_format: str = 'wide',
        fields: Optional[List[str]] = None,
        **kwargs
    ) -> pd.DataFrame:
        """
        应用标准化参数到数据上
        
        Args:
            df: 输入数据框（长表或宽表）
            standard_params: 标准化参数，包含 'upper', 'lower', 'mean', 'std' 列
            data_format: 数据格式，'long' 或 'wide'
            fields: 要处理的字段列表，如果为None则处理所有字段
            kwargs: 其他可选参数
            
        Returns:
            pd.DataFrame: 标准化后的数据框
        """
        if data_format == 'long':
            return self._apply_standardization_long(df, standard_params, fields, **kwargs)
        else:
            return self._apply_standardization_wide(df, standard_params, fields, **kwargs)

    def _apply_standardization_wide(
        self,
        df: pd.DataFrame,
        standard_params: pd.DataFrame,
        fields: Optional[List[str]] = None,
        **kwargs
    ) -> pd.DataFrame:
        """
        将标准化参数应用到宽表格式的数据上
        
        Args:
            df: 输入数据框（宽表）
            standard_params: 标准化参数，包含 'upper', 'lower', 'mean', 'std' 列
            fields: 要处理的字段列表，如果为None则处理所有字段
            kwargs: 其他可选参数
            
        Returns:
            pd.DataFrame: 标准化后的数据框
        """
        df_result = df.copy()
        
        # 如果fields为None，从列名中提取所有基础字段名
        if fields is None:
            fields = set()
            for col in df.columns:
                if col not in ['stock_code', 'trade_date']:  # 排除非特征列
                    base_field = self._get_base_field(col)
                    fields.add(base_field)
            fields = list(fields)
        else:
            # 验证指定的字段是否存在
            valid_fields = []
            for field in fields:
                if any(col.startswith(field) for col in df.columns):
                    valid_fields.append(field)
                else:
                    self.logger.warning(f"字段 {field} 在数据中不存在，跳过。")
            
            if not valid_fields:
                self.logger.error("没有有效的字段可以标准化。返回原始数据。")
                return df_result
            fields = valid_fields
        
        # 对每个字段应用标准化
        for field in fields:
            # 找到该字段的所有相关列
            field_cols = [col for col in df_result.columns if col.startswith(field)]
            if not field_cols:
                continue
            
            for col in field_cols:
                # 检查是否有对应的标准化参数
                if col not in standard_params.index:
                    self.logger.warning(f"列 {col} 没有对应的标准化参数，跳过。")
                    continue
                
                # 获取标准化参数
                upper = standard_params.loc[col, 'upper']
                lower = standard_params.loc[col, 'lower']
                mean = standard_params.loc[col, 'mean']
                std = standard_params.loc[col, 'std']
                
                # 应用标准化
                df_result[col] = df_result[col].clip(lower=lower, upper=upper)
                df_result[col] = (df_result[col] - mean) / (std + 1e-12)  # 添加小量避免除零
        
        return df_result

    def _apply_standardization_long(
        self,
        df: pd.DataFrame,
        standard_params: pd.DataFrame,
        fields: Optional[List[str]] = None,
        **kwargs
    ) -> pd.DataFrame:
        """
        将标准化参数应用到长表格式的数据上
        
        Args:
            df: 输入数据框（长表）
            standard_params: 标准化参数，包含 'upper', 'lower', 'mean', 'std' 列
            fields: 要处理的字段列表，如果为None则处理所有字段
            kwargs: 其他可选参数
            
        Returns:
            pd.DataFrame: 标准化后的数据框
        """
        # 验证长表格式
        if not {"field_name", "lag", "value"}.issubset(df.columns):
            self.logger.error("长表格式必须包含 [field_name, lag, value] 列。返回原始数据。")
            return df
        
        df_result = df.copy()
        
        # 如果fields为None，获取所有唯一字段名
        if fields is None:
            fields = df['field_name'].unique().tolist()
        
        # 对每个字段应用标准化
        for field in fields:
            # 获取该字段的所有数据
            mask_field = (df_result['field_name'] == field)
            if not any(mask_field):
                self.logger.warning(f"字段 {field} 在数据中不存在，跳过。")
                continue
            
            # 检查是否有对应的标准化参数
            if field not in standard_params.index:
                self.logger.warning(f"字段 {field} 没有对应的标准化参数，跳过。")
                continue
            
            # 获取标准化参数
            upper = standard_params.loc[field, 'upper']
            lower = standard_params.loc[field, 'lower']
            mean = standard_params.loc[field, 'mean']
            std = standard_params.loc[field, 'std']
            
            # 应用标准化
            values = df_result.loc[mask_field, 'value']
            values = values.clip(lower=lower, upper=upper)
            values = (values - mean) / (std + 1e-12)  # 添加小量避免除零
            
            # 回写
            df_result.loc[mask_field, 'value'] = values
        
        return df_result

