"""
因子缺失值处理和预处理模块 v2.2
基于配置文件的因子预处理，支持宽表和长表格式
新增keep_nan策略：保持NaN不填充，避免价格0填充导致的标准化异常
新增连续NaN检测：超过阈值的连续NaN区域保持不填充
"""

import pandas as pd
import numpy as np
import logging
from typing import Optional, Tuple, List, Dict, Any
from pathlib import Path
import sys

# 添加项目根目录到路径，确保能导入config_loader
project_root = Path(__file__).parent.parent.parent.parent
sys.path.append(str(project_root))

from src.utils.config_loader import ConfigLoader

logger = logging.getLogger(__name__)

class FactorPreprocessor:
    """
    因子预处理器 - 基于配置文件的统一因子处理
    
    支持两种数据格式：
    1. 宽表格式：每个因子是一列
    2. 长表格式：因子名和值在不同行
    
    支持的填充策略：
    - keep_nan: 保持NaN不填充，让后续代码自动drop
    - zero_fill: 直接填充0
    - ffill_then_zero: 前向填充后填充0
    - ffill_then_median: 前向填充后用当日中位数填充
    - ffill_then_industry_median: 前向填充后用行业中位数填充
    
    新增连续NaN检测：
    - consecutive_nan_threshold: 连续NaN超过阈值则保持不填充
    """
    
    def __init__(self, config_file: str = "data_processor/factors_to_dataset.yaml"):
        """
        初始化因子预处理器
        
        Args:
            config_file: 配置文件路径（相对于configs目录）
        """
        self.config_loader = ConfigLoader()
        self.config = self.config_loader.load_config(config_file)
        self.factor_categories = self.config.get('factor_categories', {})
        self.matching_config = self.config.get('matching', {})
        self.enable_prefix_matching = self.matching_config.get('enable_prefix_matching', True)
        
        # 构建因子到策略的映射
        self._build_factor_strategy_mapping()
        
        logger.info(f"✅ 因子预处理器初始化完成，加载了 {len(self.factor_categories)} 个因子分类")

    def _build_factor_strategy_mapping(self):
        """构建因子名称到填充策略的映射"""
        self.factor_to_strategy = {}
        
        for category, config in self.factor_categories.items():
            strategy = config.get('fill_strategy')
            fields = config.get('fields', [])
            
            for field in fields:
                self.factor_to_strategy[field] = strategy
        
        logger.info(f"构建了 {len(self.factor_to_strategy)} 个因子的策略映射")

    def _detect_consecutive_nan_mask(self, df: pd.DataFrame, threshold: int) -> pd.Series:
        """
        检测连续NaN并创建mask标记
        
        Args:
            df: 长表格式DataFrame，包含 ['trade_date', 'stock_code', 'factor_name', 'factor_value']
            threshold: 连续NaN的阈值天数
            
        Returns:
            pd.Series: 布尔mask，True表示该位置应该保持NaN
        """
        logger.info(f"🔍 开始检测连续NaN，阈值={threshold}天")
        
        # 确保数据按正确顺序排序
        df = df.sort_values(['stock_code', 'factor_name', 'trade_date']).reset_index(drop=True)
        
        # 创建mask Series，默认为False（不保持NaN）
        consecutive_nan_mask = pd.Series(False, index=df.index)
        
        # 按股票+因子分组处理
        groups = df.groupby(['stock_code', 'factor_name'])
        
        for (stock_code, factor_name), group in groups:
            # 获取该组的factor_value
            values = group['factor_value'].copy()
            group_indices = group.index
            
            if len(values) < threshold:
                # 如果总数据量少于阈值，跳过
                continue
            
            # 检测NaN位置
            is_nan = values.isna()
            
            if not is_nan.any():
                # 如果没有NaN，跳过
                continue
            
            # 🚀 高效算法：检测连续NaN长度
            # 方法：使用cumsum来标记连续NaN组，然后计算每组长度
            
            # 创建NaN组标识：每当从非NaN转为NaN时，组ID+1
            nan_group_ids = (~is_nan).cumsum()
            
            # 只保留NaN位置的组ID
            nan_positions = is_nan
            nan_group_ids_filtered = nan_group_ids[nan_positions]
            
            if len(nan_group_ids_filtered) == 0:
                continue
            
            # 计算每个NaN组的长度
            nan_group_lengths = nan_group_ids_filtered.groupby(nan_group_ids_filtered).size()
            
            # 找到长度>=阈值的组
            long_nan_groups = nan_group_lengths[nan_group_lengths >= threshold].index
            
            if len(long_nan_groups) == 0:
                continue
            
            # 标记这些长连续NaN位置
            mask_positions = nan_positions & nan_group_ids.isin(long_nan_groups)
            
            # 更新全局mask
            consecutive_nan_mask.loc[group_indices[mask_positions]] = True
            
            # 记录统计
            total_marked = mask_positions.sum()
            if total_marked > 0:
                logger.debug(f"  {stock_code}-{factor_name}: 标记了{total_marked}个连续NaN位置")
        
        total_marked = consecutive_nan_mask.sum()
        total_records = len(consecutive_nan_mask)
        logger.info(f"🎯 连续NaN检测完成: {total_marked}/{total_records} ({total_marked/total_records*100:.2f}%) 位置被标记保持NaN")
        
        return consecutive_nan_mask

    def _get_matching_cols_with_prefix(self, available_cols: List[str], target_factor: str) -> List[str]:
        """
        根据目标因子名匹配可用列，支持前缀匹配
        
        Args:
            available_cols: 可用的列名列表
            target_factor: 目标因子名
            
        Returns:
            匹配的列名列表
        """
        matching = []
        
        if self.enable_prefix_matching:
            # 前缀匹配逻辑
            for col in available_cols:
                # 精确匹配
                if col == target_factor:
                    matching.append(col)
                # 前缀匹配（支持分隔符）
                elif col.startswith(target_factor):
                    # 检查分隔符
                    separators = self.matching_config.get('prefix_separators', ['_', '-'])
                    next_char_idx = len(target_factor)
                    if next_char_idx < len(col):
                        next_char = col[next_char_idx]
                        if next_char in separators:
                            matching.append(col)
                    else:
                        # 完全匹配（长度相等的情况已在精确匹配处理）
                        pass
        else:
            # 只进行精确匹配
            if target_factor in available_cols:
                matching.append(target_factor)
        
        return matching

    def _get_fill_strategy(self, col_name: str) -> str:
        """
        获取列的填充策略
        
        Args:
            col_name: 列名
            
        Returns:
            填充策略名称
        """
        # 尝试直接匹配
        if col_name in self.factor_to_strategy:
            return self.factor_to_strategy[col_name]
        
        if self.enable_prefix_matching:
            # 前缀匹配
            for factor, strategy in self.factor_to_strategy.items():
                if col_name.startswith(factor):
                    # 检查分隔符
                    separators = self.matching_config.get('prefix_separators', ['_', '-'])
                    next_char_idx = len(factor)
                    if next_char_idx < len(col_name):
                        next_char = col_name[next_char_idx]
                        if next_char in separators:
                            return strategy
        
        # 如果没有匹配到，返回默认策略
        return 'zero_fill'

    def preprocess_factors_long(self, df: pd.DataFrame, factor_windows: dict = None, consecutive_nan_threshold: Optional[int] = None) -> pd.DataFrame:
        """
        长表阶段的因子预处理：在长表阶段就做好缺失值填充
        这样前向填充等操作更有优势
        
        Args:
            df: 长表格式的DataFrame
            factor_windows: 因子窗口配置
            consecutive_nan_threshold: 连续NaN阈值，超过则保持不填充。None表示不启用
            
        Returns:
            处理后的长表DataFrame
        """
        logger.info(f"开始长表阶段因子预处理，数据形状: {df.shape}")
        if consecutive_nan_threshold is not None:
            logger.info(f"🚀 启用连续NaN检测，阈值={consecutive_nan_threshold}天")
        else:
            logger.info("连续NaN检测未启用，按配置文件策略处理")
        
        df = df.copy()
        
        # 确保有必要的列
        required_base_cols = ['trade_date', 'stock_code']
        missing_base_cols = [col for col in required_base_cols if col not in df.columns]
        if missing_base_cols:
            raise ValueError(f"长表缺少基础列: {missing_base_cols}")
        
        # 检查因子名和值列
        factor_name_col = None
        factor_value_col = None
        
        for col in ['factor_name', 'field_name']:
            if col in df.columns:
                factor_name_col = col
                break
        
        for col in ['factor_value', 'value']:
            if col in df.columns:
                factor_value_col = col
                break
        
        if factor_name_col is None or factor_value_col is None:
            raise ValueError(f"长表缺少因子名列或值列。可用列: {list(df.columns)}")
        
        # 统一列名
        if factor_name_col != 'factor_name':
            df = df.rename(columns={factor_name_col: 'factor_name'})
        if factor_value_col != 'factor_value':
            df = df.rename(columns={factor_value_col: 'factor_value'})
        
        # 确保按时间排序，便于前向填充
        df = df.sort_values(['stock_code', 'trade_date', 'factor_name']).reset_index(drop=True)
        
        # 🚀 **方案2: Mask标记方案** - 第1步：检测连续NaN并创建标识（包含窗口信息）
        consecutive_nan_positions = set()
        if consecutive_nan_threshold is not None:
            consecutive_nan_mask = self._detect_consecutive_nan_mask(df, consecutive_nan_threshold)
            # 将mask转换为具体的数据标识 (trade_date, stock_code, factor_name, z_windows)
            masked_indices = df[consecutive_nan_mask].index
            for idx in masked_indices:
                row = df.iloc[idx]
                consecutive_nan_positions.add((
                    row['trade_date'], 
                    row['stock_code'], 
                    row['factor_name'],
                    row['z_windows'] if 'z_windows' in df.columns else 1
                ))
            logger.info(f"🎯 记录了{len(consecutive_nan_positions)}个连续NaN位置标识（含窗口信息）")
        
        # 按因子分组处理
        processed_groups = []
        
        for factor_name in df['factor_name'].unique():
            factor_data = df[df['factor_name'] == factor_name].copy()
            strategy = self._get_fill_strategy(factor_name)
            
            # 🚀 **方案2: 第2步** - 正常处理所有数据（按yaml配置）
            if strategy == 'keep_nan':
                # 保持NaN不填充
                pass
            
            elif strategy == 'zero_fill':
                # 直接填充0
                factor_data['factor_value'] = factor_data['factor_value'].fillna(0)
                
            elif strategy == 'ffill_then_zero':
                # 按股票前向填充，然后填充0
                factor_data['factor_value'] = (
                    factor_data.groupby('stock_code')['factor_value']
                    .ffill()
                    .fillna(0)
                )
                
            elif strategy == 'ffill_then_median':
                # 按股票前向填充，然后用当日中位数填充，最后填充0
                factor_data['factor_value'] = (
                    factor_data.groupby('stock_code')['factor_value']
                    .ffill()
                )
                
                # 用当日中位数填充剩余NaN
                if 'trade_date' in factor_data.columns:
                    daily_median = (
                        factor_data.groupby('trade_date')['factor_value']
                        .transform('median')
                    )
                    factor_data['factor_value'] = (
                        factor_data['factor_value'].fillna(daily_median).fillna(0)
                    )
                else:
                    factor_data['factor_value'] = factor_data['factor_value'].fillna(0)
                    
            elif strategy == 'ffill_then_industry_median':
                # 按股票前向填充，然后用当日行业中位数填充，最后填充0
                # 专门用于状态类因子，避免0/1不平衡导致的平均值问题
                factor_data['factor_value'] = (
                    factor_data.groupby('stock_code')['factor_value']
                    .ffill()
                )
                
                # 用当日行业中位数填充剩余NaN（如果有行业信息的话）
                if 'trade_date' in factor_data.columns:
                    if 'industry' in factor_data.columns:
                        # 如果有行业信息，用行业中位数填充
                        industry_median = (
                            factor_data.groupby(['trade_date', 'industry'])['factor_value']
                            .transform('median')
                        )
                        factor_data['factor_value'] = (
                            factor_data['factor_value'].fillna(industry_median).fillna(0)
                        )
                    else:
                        # 如果没有行业信息，降级为当日中位数填充
                        logger.warning(f"因子 {factor_name} 缺少行业信息，降级为当日中位数填充")
                        daily_median = (
                            factor_data.groupby('trade_date')['factor_value']
                            .transform('median')
                        )
                        factor_data['factor_value'] = (
                            factor_data['factor_value'].fillna(daily_median).fillna(0)
                        )
                else:
                    factor_data['factor_value'] = factor_data['factor_value'].fillna(0)
                    
            elif strategy == 'transform_then_zero':
                # 先进行数值转换，然后填充0
                logger.debug(f"对因子 {factor_name} 进行百分比转换: /100 - 1")
                factor_data['factor_value'] = (factor_data['factor_value'] / 100.0) - 1.0
                factor_data['factor_value'] = factor_data['factor_value'].fillna(0)
                
            else:
                # 默认策略：填充0
                logger.warning(f"未知策略 {strategy}，对因子 {factor_name} 使用默认的零填充")
                factor_data['factor_value'] = factor_data['factor_value'].fillna(0)
            
            processed_groups.append(factor_data)
        
        # 合并处理结果
        result_df = pd.concat(processed_groups, ignore_index=True)
        
        # 重命名因子列（添加窗口后缀）- 先重命名，再更新位置标识
        if 'z_windows' in result_df.columns:
            logger.info("重命名因子列，添加窗口后缀")
            result_df['factor_name'] = (
                result_df['factor_name'] + '_w' + result_df['z_windows'].astype(int).astype(str)
            )
            
            # 🚀 更新连续NaN位置标识中的因子名称（使用正确的窗口值）
            if consecutive_nan_positions:
                updated_positions = set()
                for trade_date, stock_code, old_factor_name, window_val in consecutive_nan_positions:
                    # 根据实际的z_windows值更新因子名称
                    new_factor_name = f"{old_factor_name}_w{int(window_val)}"
                    updated_positions.add((trade_date, stock_code, new_factor_name))
                consecutive_nan_positions = updated_positions
                logger.info(f"🔄 已更新{len(consecutive_nan_positions)}个位置标识的因子名称")
        
        # 🚀 **方案2: 第3步** - 基于位置标识恢复连续NaN（重命名后）
        if consecutive_nan_positions:
            logger.info("🔧 基于位置标识恢复连续NaN（重命名后）...")
            restore_count = 0
            
            # 创建布尔mask来标识需要恢复为NaN的位置
            restore_mask = pd.Series(False, index=result_df.index)
            
            for i, row in result_df.iterrows():
                position_key = (row['trade_date'], row['stock_code'], row['factor_name'])
                if position_key in consecutive_nan_positions:
                    restore_mask.iloc[i] = True
                    restore_count += 1
            
            # 批量恢复为NaN
            if restore_count > 0:
                result_df.loc[restore_mask, 'factor_value'] = np.nan
                logger.info(f"🎯 已将{restore_count}个连续NaN位置恢复为NaN（超过{consecutive_nan_threshold}天阈值）")
            else:
                logger.info("无需恢复连续NaN位置")
        
        final_nan_count = result_df['factor_value'].isna().sum()
        logger.info(f"长表因子预处理完成，最终形状: {result_df.shape}")
        logger.info(f"最终NaN数量: {final_nan_count}")
        
        return result_df

# 保持向后兼容的便捷函数
def preprocess_factors(df: pd.DataFrame) -> pd.DataFrame:
    """
    宽表因子预处理（保持向后兼容）
    
    Args:
        df: 包含trade_date, stock_code和因子列的DataFrame
        
    Returns:
        处理后的DataFrame
    """
    logger.info(f"开始宽表因子预处理，数据形状: {df.shape}")
    
    preprocessor = FactorPreprocessor()
    df = df.copy()
    
    # 获取所有因子列（排除基础列）
    base_cols = ['trade_date', 'stock_code', 'year', 'month']
    factor_cols = [col for col in df.columns if col not in base_cols]
    
    logger.info(f"发现 {len(factor_cols)} 个因子列需要处理")
    
    for col in factor_cols:
        strategy = preprocessor._get_fill_strategy(col)
        
        if strategy == 'keep_nan':
            # 保持NaN不填充
            pass
            
        elif strategy == 'zero_fill':
            df[col] = df[col].fillna(0)
            
        elif strategy == 'ffill_then_zero':
            if 'stock_code' in df.columns:
                df[col] = df.groupby('stock_code')[col].ffill().fillna(0)
            else:
                df[col] = df[col].fillna(0)
                
        elif strategy == 'ffill_then_median':
            if 'stock_code' in df.columns and 'trade_date' in df.columns:
                # 按股票前向填充
                df[col] = df.groupby('stock_code')[col].ffill()
                # 用当日中位数填充剩余NaN
                median_values = df.groupby('trade_date')[col].transform('median')
                df[col] = df[col].fillna(median_values).fillna(0)
            else:
                df[col] = df[col].fillna(0)
                
        elif strategy == 'ffill_then_industry_median':
            if 'stock_code' in df.columns and 'trade_date' in df.columns:
                # 按股票前向填充
                df[col] = df.groupby('stock_code')[col].ffill()
                # 用当日行业中位数填充剩余NaN
                if 'industry' in df.columns:
                    industry_median = df.groupby(['trade_date', 'industry'])[col].transform('median')
                    df[col] = df[col].fillna(industry_median).fillna(0)
                else:
                    # 降级为当日中位数填充
                    median_values = df.groupby('trade_date')[col].transform('median')
                    df[col] = df[col].fillna(median_values).fillna(0)
            else:
                df[col] = df[col].fillna(0)
                
        elif strategy == 'transform_then_zero':
            # 百分比转换
            df[col] = (df[col] / 100.0) - 1.0
            df[col] = df[col].fillna(0)
        else:
            # 默认填充0
            df[col] = df[col].fillna(0)
    
    logger.info(f"宽表因子预处理完成，最终形状: {df.shape}")
    return df


def preprocess_factors_long(df: pd.DataFrame, factor_windows: dict = None) -> pd.DataFrame:
    """
    长表因子预处理（保持向后兼容）
    """
    preprocessor = FactorPreprocessor()
    return preprocessor.preprocess_factors_long(df, factor_windows)


def winsorize_labels_by_date(
    df: pd.DataFrame, 
    label_col: str,
    winsor_q: Tuple[float, float] = (0.0005, 0.9995)
) -> pd.DataFrame:
    """
    按日期对标签进行Winsorization处理
    
    Args:
        df: 包含trade_date和标签列的DataFrame
        label_col: 标签列名
        winsor_q: Winsorization的分位数范围
        
    Returns:
        处理后的DataFrame
    """
    logger.info(f"开始按日期对标签 {label_col} 进行Winsorization，分位数范围: {winsor_q}")

    if 'trade_date' not in df.columns:
        raise KeyError("DataFrame 必须包含 trade_date 列")
    if label_col not in df.columns:
        raise KeyError(f"DataFrame 中不存在标签列 {label_col}")

    df = df.copy()

    # 使用 transform 计算每个 trade_date 的上下分位并裁剪，仅修改标签列，保留其他列
    g = df.groupby('trade_date')[label_col]

    def _q(s: pd.Series, q: float):
        return np.nanquantile(s, q) if s.notna().any() else np.nan

    lo = g.transform(lambda s: _q(s, winsor_q[0]))
    hi = g.transform(lambda s: _q(s, winsor_q[1]))

    mask = df[label_col].notna() & lo.notna() & hi.notna()
    if mask.any():
        clipped = np.clip(df.loc[mask, label_col].to_numpy(), lo.loc[mask].to_numpy(), hi.loc[mask].to_numpy())
        df.loc[mask, label_col] = clipped

    logger.info("标签按日期Winsorization完成")
    return df


def generate_lag_features(
    df: pd.DataFrame, 
    factor_cols: List[str], 
    lag: int = 30
) -> pd.DataFrame:
    """
    为指定的因子列生成lag特征（简化版本，去掉mask处理）
    
    Args:
        df: 包含trade_date, stock_code和因子列的DataFrame  
        factor_cols: 需要生成lag特征的因子列名列表
        lag: lag窗口大小
        
    Returns:
        包含lag特征的DataFrame（原始列被重命名为*_lag_0）
    """
    logger.info(f"开始生成lag特征，lag={lag}, 因子数={len(factor_cols)}")
    
    if 'stock_code' not in df.columns or 'trade_date' not in df.columns:
        logger.error("DataFrame必须包含stock_code和trade_date列")
        return df
    
    # 确保按时间排序
    df = df.sort_values(['stock_code', 'trade_date']).reset_index(drop=True)
    
    # 检查哪些因子实际存在于数据中
    factors_present = [f for f in factor_cols if f in df.columns]
    factors_missing = [f for f in factor_cols if f not in df.columns]
    
    if factors_missing:
        logger.warning(f"以下因子在数据中不存在，将跳过: {factors_missing}")
    
    logger.info(f"将为 {len(factors_present)} 个现有因子生成lag特征")
    
    # 使用MultiIndex提高效率
    df = df.set_index(['stock_code', 'trade_date'])
    
    out_dfs = []
    
    # 保留非因子列
    non_factor_cols = [col for col in df.columns if col not in factors_present]
    if non_factor_cols:
        base_df = df[non_factor_cols].copy()
        out_dfs.append(base_df)
    
    # 为每个因子生成lag特征（逆序：从lag_29到lag_0）
    for factor in factors_present:
        factor_series = df[factor]
        factor_lag_dfs = []
        
        # 生成逆序lag特征：lag_29 (最早) 到 lag_0 (最近)
        for i in range(lag-1, -1, -1):  # 29, 28, ..., 1, 0
            if i == 0:
                # lag_0 是原始值
                lag_df = factor_series.to_frame(f"{factor}_lag_{i}")
            else:
                # lag_i 是移位值
                shifted = (
                    factor_series.groupby('stock_code', sort=False)
                    .shift(i)
                    .to_frame(f"{factor}_lag_{i}")
                )
                lag_df = shifted
            
            factor_lag_dfs.append(lag_df)
        
        # 合并该因子的所有lag特征
        factor_all_lags = pd.concat(factor_lag_dfs, axis=1)
        out_dfs.append(factor_all_lags)
    
    # 合并所有结果
    if out_dfs:
        result_df = pd.concat(out_dfs, axis=1).reset_index()
    else:
        result_df = df.reset_index()
    
    logger.info(f"lag特征生成完成，最终形状: {result_df.shape}")
    logger.info(f"生成的lag特征列数: {len([col for col in result_df.columns if '_lag_' in col])}")
    
    return result_df


def pivot_long_to_wide(
    df: pd.DataFrame,
    factor_names: List[str],
    lag_filter: int = 0
) -> pd.DataFrame:
    """
    长表转宽表（简化版本，去掉mask生成）
    
    Args:
        df: 长表格式的DataFrame
        factor_names: 需要保留的因子名称列表
        lag_filter: 只保留指定lag值的数据
        
    Returns:
        宽表格式的DataFrame
    """
    logger.info(f"开始长表转宽表，数据形状: {df.shape}, lag_filter: {lag_filter}")
    
    # 动态检测列名
    factor_name_col = None
    value_col = None
    
    for col in ['factor_name', 'field_name']:
        if col in df.columns:
            factor_name_col = col
            break
    
    for col in ['factor_value', 'value']:
        if col in df.columns:
            value_col = col
            break
    
    if factor_name_col is None or value_col is None:
        logger.error(f"无法找到因子名列或值列。可用列: {list(df.columns)}")
        return pd.DataFrame()
    
    # lag过滤
    if 'lag' in df.columns:
        df_filtered = df[df['lag'] == lag_filter].copy()
    else:
        df_filtered = df.copy()
    
    if df_filtered.empty:
        logger.warning("过滤后数据为空")
        return pd.DataFrame()
    
    # 只保留指定的因子
    value_df = df_filtered[df_filtered[factor_name_col].isin(factor_names)].copy()
    
    if value_df.empty:
        logger.warning("没有匹配的因子数据")
        return pd.DataFrame()
    
    try:
        # 去重
        value_df = value_df.drop_duplicates(
            subset=['trade_date', 'stock_code', factor_name_col], 
            keep='first'
        )
        
        # 透视
        wide = value_df.pivot_table(
            index=['trade_date', 'stock_code'],
            columns=factor_name_col,
            values=value_col,
            aggfunc='first'
        ).reset_index()
        
        # 扁平化列名
        wide.columns.name = None
        
        logger.info(f"透视完成，宽表形状: {wide.shape}")
        
        return wide
        
    except Exception as e:
        logger.error(f"透视表创建失败: {str(e)}")
        raise 