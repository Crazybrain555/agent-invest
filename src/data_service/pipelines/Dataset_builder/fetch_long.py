# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from typing import Dict, List, Optional, Set
import pandas as pd
import numpy as np

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.data_service.preprocessing.methods.preprocess_factors import FactorPreprocessor

from .calendar_utils import _get_trading_days_before
from .pivoting import pivot_long_to_wide_simple, _complete_date_reindex
from .lag import generate_lag_features_simple
from .stats_zscore import _load_stats_with_window, _apply_zscore_with_window


def _fetch_join_filter_chunk_long(prov: LocalTestDBDataProvider, s: str, e: str, lag: int,
                                  label: str, factor_windows: Dict[str, List[int]], 
                                  x_table: str, y_table: str,
                                  restricted: Set[tuple[str, str]],
                                  label_shift: int = 10,
                                  stats_table: str = None,
                                  clip_std: bool = True,
                                  factor_based_nan_handling: bool = False,
                                  consecutive_nan_threshold: Optional[int] = None):
    """
    Fetch data from long-format features table and labels table, 
    pivot to wide format, generate lag features, apply zscore processing.
    🚀 修复：为所有定义的因子创建完整骨架，避免写入阶段填充0的问题
    
    Args:
        prov: Data provider
        s, e: Start and end dates
        lag: Lag window size
        label: Label name
        factor_windows: Factor windows configuration
        x_table: Features table name (long format)
        y_table: Labels table name
        restricted: Restricted stocks set
        label_shift: Label shift parameter
        stats_table: Statistics table name for zscore
        clip_std: Whether to apply clipping
        factor_based_nan_handling: Whether to apply advanced NaN handling based on factor categories
        consecutive_nan_threshold: Consecutive NaN threshold
        
    Returns:
        Processed DataFrame
    """
    # Calculate extended start date for lag features
    fetch_start = _get_trading_days_before(s, lag - 1)
    logger.info(f"Fetching features from {x_table} with buffer ({fetch_start} to {e}, target: {s} to {e})...")
    
    # 🚀 动态探测表结构，避免硬编码列名导致的no-data问题
    try:
        # 方法1: 尝试使用 list_fields 获取列名（更高效）
        try:
            available_cols = prov.list_fields(x_table)
            logger.info(f"通过 list_fields 探测到表 {x_table} 的列名: {available_cols}")
        except:
            # 方法2: 获取一小部分数据来探测列名
            sample_data = prov.fetch_data(table=x_table, start_date=fetch_start, end_date=fetch_start)
            available_cols = list(sample_data.columns) if not sample_data.empty else []
            logger.info(f"通过样本数据探测到表 {x_table} 的列名: {available_cols}")
        
        # 自动识别窗口列名（按优先级尝试）
        possible_win_cols = ['z_windows', 'z_window', 'window']
        win_col = next((c for c in possible_win_cols if c in available_cols), None)
        
        # 自动识别因子名列
        possible_factor_cols = ['factor_name', 'field_name', 'feature_name']
        factor_col = next((c for c in possible_factor_cols if c in available_cols), None)
        
        logger.info(f"识别列名: 窗口列={win_col}, 因子列={factor_col}")
        
    except Exception as e:
        logger.warning(f"探测表结构失败: {str(e)}，使用默认列名")
        win_col = 'z_windows'
        factor_col = 'factor_name'
    
    # 🚀 第1步：获取所有有数据的因子（部分数据）
    features_accum = []
    for fac, ws in factor_windows.items():
        # 动态构建过滤条件，只在列存在时才添加过滤
        filters = {}
        if factor_col:
            filters[factor_col] = [fac]
        if win_col and ws:  # 只有窗口列存在且有窗口值时才过滤
            filters[win_col] = ws
            
        logger.debug(f"获取因子 {fac} 数据，过滤条件: {filters}")
        
        df_part = prov.fetch_data(
            table=x_table,
            start_date=fetch_start,
            end_date=e,
            format="long",
            column_filters=filters if filters else None  # 如果没有过滤条件就不传
        )
        if not df_part.empty:
            features_accum.append(df_part)
    
    # 🚀 第2步：获取数据范围和股票列表
    # 即使没有任何因子数据，也需要获取基本的日期和股票信息来创建骨架
    if features_accum:
        existing_data = pd.concat(features_accum, ignore_index=True)
        logger.info(f"从数据库获取到部分因子数据: {len(existing_data)} 条记录")
        
        # 从现有数据中提取日期和股票列表
        existing_data['trade_date'] = pd.to_datetime(existing_data['trade_date']).dt.strftime('%Y%m%d')
        existing_data['stock_code'] = existing_data['stock_code'].astype(str)
        
        date_range = sorted(existing_data['trade_date'].unique())
        stock_list = sorted(existing_data['stock_code'].unique())
    else:
        logger.warning(f"❌ No feature data found in {x_table} for any factor")
        existing_data = pd.DataFrame()
        
        # 尝试从任何数据中获取基本的日期和股票信息
        try:
            # 尝试从标签表获取日期和股票列表
            labels_sample = prov.fetch_data(
                table=y_table,
                start_date=fetch_start,
                end_date=e,
                fields=["trade_date", "stock_code"],
                format="long"
            )
            
            if not labels_sample.empty:
                labels_sample['trade_date'] = pd.to_datetime(labels_sample['trade_date']).dt.strftime('%Y%m%d')
                labels_sample['stock_code'] = labels_sample['stock_code'].astype(str)
                date_range = sorted(labels_sample['trade_date'].unique())
                stock_list = sorted(labels_sample['stock_code'].unique())
                logger.info(f"从标签表获取基础信息: {len(date_range)}个日期, {len(stock_list)}个股票")
            else:
                logger.error("无法从标签表获取基础信息，返回空DataFrame")
                return pd.DataFrame()
                
        except Exception as e:
            logger.error(f"无法获取基础日期和股票信息: {str(e)}")
            return pd.DataFrame()
    
    # 🚀 第3步：创建完整的因子骨架（关键修复）
    logger.info("🔧 创建完整因子骨架，确保所有定义的因子都有记录...")
    from .skeleton import _create_complete_factor_skeleton  # local import to avoid cycle
    features_df = _create_complete_factor_skeleton(
        date_range, stock_list, factor_windows, existing_data
    )
    
    if features_df.empty:
        logger.warning(f"创建的因子骨架为空，无法继续处理")
        return pd.DataFrame()
    
    # 🚀 第4步：数据已经是统一格式，直接进行统计
    logger.info(f"完整因子骨架创建完成: {features_df.shape}")
    
    # 统计各因子的NaN情况
    for factor_name in factor_windows.keys():
        factor_data = features_df[features_df['factor_name'] == factor_name]
        if not factor_data.empty:
            nan_count = factor_data['factor_value'].isna().sum()
            total_count = len(factor_data)
            nan_pct = (nan_count / total_count * 100) if total_count > 0 else 0
            logger.info(f"  {factor_name}: {total_count}条记录, {nan_count}个NaN ({nan_pct:.1f}%)")
    
    # 数据格式已经是标准的，列名已经统一为：
    # ['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']

    # 🚀 第5步：关键修复 - 在长表阶段进行因子预处理
    logger.info("🎯 对完整因子骨架进行预处理，包括缺失因子的NaN处理...")
    if factor_based_nan_handling:
        preprocessor = FactorPreprocessor()
        features_df = preprocessor.preprocess_factors_long(features_df, factor_windows, consecutive_nan_threshold)
        logger.info("✅ 长表阶段因子预处理完成")
        logger.info("✅ 所有因子（包括缺失的）都按配置策略处理")
        
        # 统计预处理后的NaN情况
        final_nan_count = features_df['factor_value'].isna().sum()
        total_records = len(features_df)
        logger.info(f"预处理后NaN统计: {final_nan_count}/{total_records} ({final_nan_count/total_records*100:.2f}%)")
    else:
        # 如果不启用高级NaN处理，手动重命名因子列
        logger.info("未启用高级NaN处理，手动添加窗口后缀...")
        features_df['factor_name'] = (
            features_df['factor_name'] + '_w' + features_df['z_windows'].astype(int).astype(str)
        )

    # 🚀 第6步：获取所有因子名称（现在包含窗口后缀）
    factor_names = features_df['factor_name'].unique().tolist()
    logger.info(f"处理后的因子数量: {len(factor_names)} (包含所有定义的因子)")

    # 🚀 第7步：将完整的长表转换为宽表
    logger.info("将完整预处理后的长表转换为宽表...")
    features_wide = pivot_long_to_wide_simple(features_df, factor_names, 
                                           factor_name_col='factor_name', 
                                           value_col='factor_value', 
                                           lag_filter=0)

    if features_wide.empty:
        logger.warning(f"No data after pivoting for date range {s} to {e}")
        return pd.DataFrame()

    # 🚀 修复2：在pivot后进行完整的日期reindex (使用扩展的日期范围)
    logger.info("Performing complete date reindexing with extended range...")
    features_wide = _complete_date_reindex(features_wide, fetch_start, e)

    # Generate lag features (现在在完整数据上生成)
    logger.info(f"Generating lag features with lag={lag}...")
    features_lagged = generate_lag_features_simple(features_wide, factor_names, lag)
    
    # 🚀 关键修复：lag生成后立即截断到目标日期范围，避免历史缓冲数据的NaN污染
    if not features_lagged.empty:
        initial_rows = len(features_lagged)
        
        # 🔍 调试lag生成后的NaN情况
        feature_cols_temp = [col for col in features_lagged.columns if '_lag_' in col]
        if feature_cols_temp:
            lag_nan_counts = features_lagged[feature_cols_temp].isnull().sum()
            total_lag_nans = lag_nan_counts.sum()
            logger.info(f"🔍 Lag生成后NaN统计: {total_lag_nans} 个NaN值")
            if total_lag_nans > 0:
                top_lag_nan_cols = lag_nan_counts[lag_nan_counts > 0].sort_values(ascending=False).head(5)
                logger.info(f"  📊 NaN最多的lag特征列:")
                for col, count in top_lag_nan_cols.items():
                    logger.info(f"    {col}: {count} NaN")
        
        mask_target_range = (features_lagged['trade_date'] >= s) & (features_lagged['trade_date'] <= e)
        features_lagged = features_lagged[mask_target_range]
        final_rows = len(features_lagged)
        logger.info(f"截断到目标日期范围: {initial_rows} → {final_rows} rows (目标: {s} to {e})")
        logger.info("已移除历史缓冲期的NaN数据，保留纯净的目标范围数据")
        
        # 🔍 调试截断后的NaN情况  
        if feature_cols_temp:
            truncate_nan_counts = features_lagged[feature_cols_temp].isnull().sum()
            total_truncate_nans = truncate_nan_counts.sum()
            logger.info(f"🔍 截断后NaN统计: {total_truncate_nans} 个NaN值")

    # Apply zscore transformation if stats_table is provided
    if stats_table:
        logger.info(f"Applying zscore transformation using {stats_table}...")
        stats = _load_stats_with_window(prov, stats_table, clip_std)
        features_lagged = _apply_zscore_with_window(features_lagged, stats, clip_std, factor_windows)
    
    # Get labels data
    logger.info(f"Fetching labels from {y_table} ({s} to {e})...")
    labels_df = prov.fetch_data(
        table=y_table,
        start_date=s,
        end_date=e,
        fields=["trade_date", "stock_code", "field_name", "value", "label_shift"],
        format="long"
    )
    
    if labels_df.empty:
        logger.warning(f"No label data found in {y_table} for date range {s} to {e}")
        return pd.DataFrame()
    
    # Convert column types for consistency
    labels_df['trade_date'] = pd.to_datetime(labels_df['trade_date']).dt.strftime('%Y%m%d')
    labels_df['stock_code'] = labels_df['stock_code'].astype(str)
    
    # Filter labels by field_name and label_shift
    labels_df = labels_df[
        (labels_df['field_name'] == label) & 
        (labels_df['label_shift'] == label_shift)
    ].copy()
    
    if labels_df.empty:
        logger.warning(f"No data found for label {label} with label_shift={label_shift} in date range {s} to {e}")
        return pd.DataFrame()
    
    # Rename the value column to the label name
    labels_df = labels_df.rename(columns={'value': label})
    labels_df = labels_df.drop(columns=['field_name', 'label_shift'])
    
    # Join features and labels
    logger.info("Joining features and labels...")
    df = pd.merge(
        features_lagged, 
        labels_df,
        on=['trade_date', 'stock_code'],
        how='inner'
    )
    
    if df.empty:
        logger.warning(f"No data after joining features and labels for date range {s} to {e}")
        return df
    
    
    
    # 最终NaN清理
    if not df.empty:
        initial_rows = len(df)
        
        # 先移除标签为NaN的行
        if label in df.columns:
            df = df.dropna(subset=[label])
            after_label_filter = len(df)
            logger.info(f"Dropped {initial_rows - after_label_filter} rows with NaN labels")
            
            # 移除特征列中的NaN行
            feature_cols = [col for col in df.columns if '_lag_' in col]
            if feature_cols:
                # 检查特征列的NaN情况
                nan_counts = df[feature_cols].isnull().sum()
                total_nans = nan_counts.sum()
                
                if total_nans > 0:
                    # 移除包含NaN的行
                    before_dropna = len(df)
                    df = df.dropna(subset=feature_cols)
                    after_dropna = len(df)
                    logger.info(f"移除包含NaN特征的行: {before_dropna} → {after_dropna}")

        else:
            logger.warning(f"Label column {label} not found, applying general dropna")
            df = df.dropna()
            
        final_rows = len(df)
        logger.info(f"数据清理完成: {initial_rows} → {final_rows} rows")
    
    # Filter out restricted stocks (IMPORTANT: keep this logic!)
    if restricted and not df.empty:
        initial_rows = len(df)
        mask = ~pd.MultiIndex.from_arrays([df.trade_date, df.stock_code]).isin(restricted)
        df = df[mask]
        final_rows = len(df)
        logger.info(f"Filtered out restricted stocks: {initial_rows} → {final_rows} rows")
    
    logger.info(f"Final data shape: {df.shape}")
    return df


