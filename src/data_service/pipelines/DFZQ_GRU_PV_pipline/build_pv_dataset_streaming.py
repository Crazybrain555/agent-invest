'''
Streaming builder for the Price‑Volume dataset (pv_v2).
Processes data month‑by‑month to keep <2 GB RAM, writes shards on the fly,
creates splits.parquet (optional) and shows a real‑time progress bar.

Usage (CLI):
    python -m src.data_service.pipelines.build_pv_dataset_streaming \
        --out data/Dataset/pv_v2 --start 20120101 --end 20241231 \
        --chunk M --lag 30 --label label_raw \
        --splits train:20120101:20241231
'''
from __future__ import annotations

import gc
import json
import logging
import sys  # 添加sys模块导入
from pathlib import Path
from typing import Sequence, Tuple, List, Set, Optional, Dict, Union
import os
from datetime import datetime, timedelta

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from tqdm import tqdm

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.data_service.preprocessing.methods.preprocess_factors import (
    preprocess_factors, preprocess_factors_long, pivot_long_to_wide, 
    winsorize_labels_by_date, generate_lag_features, FactorPreprocessor
)
# 移除norm_config依赖，使用新的配置文件系统
# from src.data_service.preprocessing.methods.norm_config import (
#     PRICE_FIELDS_FACTOR_ENG, VOLUME_FIELDS_FACTOR_ENG, RATIO_FIELDS_FACTOR_ENG,
#     VALUE_FIELDS_FACTOR_ENG, FORECAST_FIELDS_FACTOR_ENG, STATUS_FIELDS_FACTOR_ENG,
#     TECHNICAL_FIELDS_FACTOR_ENG
# )
from src.utils.config_loader import ConfigLoader
from .factor_windows import FACTOR_WINDOWS, get_all_factor_names, get_base_windows

logger = logging.getLogger(__name__)

# 定义mask基础列名 - 改为函数动态生成
# MASK_BASE = ["fundflow_mask", "vwap_mask"]  # 删除这行

# 交易日历缓存
_TRADING_CALENDAR_CACHE = None

# 保持向后兼容的默认因子名称（已弃用，使用 FACTOR_WINDOWS）
DEFAULT_FACTOR_NAMES = get_all_factor_names()

# ────────────────────────────────────────────────────────────────
#                            Helpers
# ────────────────────────────────────────────────────────────────

def _ensure_dirs(out_dir: Path):
    meta = out_dir / "meta"
    shards = out_dir / "shards"
    meta.mkdir(parents=True, exist_ok=True)
    shards.mkdir(parents=True, exist_ok=True)
    return meta, shards


def _iter_ranges(start: str, end: str, freq: str = "M"):
    """
    生成 (起始日, 结束日) 序列。
    - 当 freq == "M" 时使用 MonthStart / MonthEnd，确保区间为 0101‒0131、0201‒0228…；
    - 其它 freq （例如 "Y"）保持原逻辑。
    """
    if freq == "M":
        idx = pd.date_range(start, end, freq="MS")  # Month **S**tart
        for d0 in idx:
            d1_raw = (d0 + pd.offsets.MonthEnd(0)).strftime("%Y%m%d")  # 当月最后一天
            d1 = min(d1_raw, end)  # 截断：不能超过指定的end日期
            yield d0.strftime("%Y%m%d"), d1
    else:
        idx = pd.date_range(start, end, freq=freq)
        for d0, d1 in zip(idx, idx[1:].append(pd.DatetimeIndex([end]))):
            yield d0.strftime("%Y%m%d"), d1.strftime("%Y%m%d")


def _load_restricted_set(prov: LocalTestDBDataProvider, start: str, end: str, table: str):
    df = prov.fetch_data(table=table, start_date=start, end_date=end,
                         fields=["trade_date", "stock_code", "signal"])
    # 🚀 修复日期格式不匹配问题：统一为YYYYMMDD格式
    df_restricted = df[df.signal == 1].copy()
    df_restricted['trade_date_formatted'] = pd.to_datetime(df_restricted['trade_date']).dt.strftime('%Y%m%d')
    return set(zip(df_restricted['trade_date_formatted'],
                   df_restricted['stock_code'].astype(str)))


def _fetch_labels_chunk(prov: LocalTestDBDataProvider, s: str, e: str, 
                       label: str, y_table: str, restricted: Set[tuple[str, str]], 
                       label_shift: int = 10):
    """
    简化版本：只获取标签数据，用于统计阶段
    """
    logger.info(f"Fetching labels only from {y_table} ({s} to {e})...")
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
    
    # Drop rows where label is NaN
    labels_df = labels_df.dropna(subset=[label])
    
    # Filter out restricted stocks
    if restricted:
        mask = ~pd.MultiIndex.from_arrays([labels_df.trade_date, labels_df.stock_code]).isin(restricted)
        labels_df = labels_df[mask]
    
    return labels_df


def _get_trading_days_before(start_date: str, periods: int) -> str:
    """
    获取指定日期之前N个交易日的日期（带缓存优化）
    
    Args:
        start_date: 基准日期 (YYYYMMDD格式)
        periods: 向前查找的交易日数量
        
    Returns:
        str: N个交易日之前的日期 (YYYYMMDD格式)
    """
    global _TRADING_CALENDAR_CACHE
    
    try:
        # 如果缓存为空，一次性加载完整交易日历
        if _TRADING_CALENDAR_CACHE is None:
            logger.info("首次加载交易日历缓存...")
            from src.utils.db_connection import db_config
            from sqlalchemy import text
            
            # 加载足够长的历史交易日历（比如2000年至今）
            sql = text("""
            SELECT TRADE_DAYS
            FROM wind_quant.dbo.AShareCalendar
            WHERE S_INFO_EXCHMARKET='SSE'
            AND TRADE_DAYS >= '20000101'
            ORDER BY TRADE_DAYS ASC
            """)
            
            with db_config.get_wind_session() as session:
                result = session.execute(sql)
                _TRADING_CALENDAR_CACHE = [str(row[0]) for row in result]
            
            logger.info(f"交易日历缓存加载完成，包含 {len(_TRADING_CALENDAR_CACHE)} 个交易日")
        
        # 从缓存中查找
        trading_dates = _TRADING_CALENDAR_CACHE
        
        if not trading_dates:
            # 如果无法获取交易日历，使用估算方法
            logger.warning(f"无法获取交易日历，使用估算方法：向前推{int(periods * 1.4)}个自然日")
            end_date = datetime.strptime(start_date, '%Y%m%d')
            estimated_date = end_date - timedelta(days=int(periods * 1.4))
            return estimated_date.strftime('%Y%m%d')
        
        # 找到基准日期在交易日历中的位置
        if start_date in trading_dates:
            target_index = trading_dates.index(start_date)
        else:
            # 如果基准日期不是交易日，找到最近的前一个交易日
            target_index = len([d for d in trading_dates if d < start_date]) - 1
        
        # 计算目标日期的索引
        target_date_index = max(0, target_index - periods)
        
        return trading_dates[target_date_index]
        
    except Exception as e:
        logger.error(f"获取交易日失败: {str(e)}")
        # 降级处理：使用估算方法
        end_date = datetime.strptime(start_date, '%Y%m%d')
        estimated_date = end_date - timedelta(days=int(periods * 1.4))
        return estimated_date.strftime('%Y%m%d')


def _load_stats_with_window(prov: LocalTestDBDataProvider, table: str, clip: bool):
    """
    Load statistics table with window support for new format
    
    Args:
        prov: Data provider
        table: Statistics table name
        clip: Whether to include clipping bounds
        
    Returns:
        Dictionary mapping (feature_name, window) to stats
    """
    stats_df = prov.fetch_data(table=table)
    
    # Create a multi-index with (feature_name, window)
    stats_df = stats_df.set_index(['feature_name', 'window'])
    
    cols = ["mean", "std", "lower", "upper"] if clip else ["mean", "std"]
    return stats_df[cols]


def _apply_zscore_with_window(df: pd.DataFrame, stats, clip: bool, factor_windows: Dict[str, List[int]] = None):
    """
    Apply zscore transformation with window-based statistics
    
    Args:
        df: DataFrame with feature columns
        stats: Statistics DataFrame with (feature_name, window) index
        clip: Whether to apply clipping
        factor_windows: Factor windows configuration to match exact windows
        
    Returns:
        DataFrame with zscore applied
    """
    feat_cols = [c for c in df.columns if "_lag_" in c]
    
    for col in feat_cols:
        # Extract base feature name and lag
        parts = col.split('_lag_')
        if len(parts) != 2:
            continue
            
        base_name = parts[0]
        lag_num = int(parts[1])
        
        # Extract window from feature name if it has _w suffix
        if '_w' in base_name:
            base_parts = base_name.split('_w')
            if len(base_parts) == 2:
                factor_name = base_parts[0]
                window = int(base_parts[1])
            else:
                factor_name = base_name
                window = None
        else:
            factor_name = base_name
            window = None
        
        # Try to find matching statistics
        stat_key = None
        if window is not None:
            # Try exact match first
            if (factor_name, window) in stats.index:
                stat_key = (factor_name, window)
            elif (base_name, window) in stats.index:
                stat_key = (base_name, window)
        
        # If no exact match found, try to find any available window for this factor
        if stat_key is None:
            available_windows = []
            for (feature_name, stat_window), row in stats.iterrows():
                if feature_name == factor_name or feature_name == base_name:
                    available_windows.append((feature_name, stat_window))
            
            if available_windows:
                # Use the first available window
                stat_key = available_windows[0]
            else:
                logger.warning(f"No statistics found for feature {factor_name}/{base_name}, skipping zscore")
                continue
        
        try:
            stat_row = stats.loc[stat_key]
            mu = float(stat_row["mean"])
            sd = float(stat_row["std"]) + 1e-12
            
            # Apply zscore transformation
            values = df[col].values.astype(float)
            values = (values - mu) / sd
            
            # Apply clipping if requested
            if clip and "lower" in stats.columns and "upper" in stats.columns:
                lo = float(stat_row["lower"])
                hi = float(stat_row["upper"])
                lower_bound = (lo - mu) / sd
                upper_bound = (hi - mu) / sd
                values = np.clip(values, lower_bound, upper_bound)
            
            df[col] = values.astype("float32")
            
        except (KeyError, IndexError) as e:
            logger.warning(f"Statistics not found for {stat_key}: {e}")
            continue
    
    return df


# 移除旧的_apply_factor_based_nan_handling函数，使用新的预处理器
# 这个函数已经被新的FactorPreprocessor替代，不再需要


def _create_complete_factor_skeleton(date_range: List[str], stock_list: List[str], 
                                   factor_windows: Dict[str, List[int]], 
                                   existing_data: pd.DataFrame = None) -> pd.DataFrame:
    """
    为所有定义的因子创建完整的数据骨架，缺失的因子值为NaN
    
    Args:
        date_range: 日期范围列表
        stock_list: 股票列表  
        factor_windows: 因子窗口配置
        existing_data: 已有的数据（长表格式）
        
    Returns:
        完整的因子数据骨架，缺失的因子值为NaN
    """
    logger.info("🔧 为所有定义因子创建完整数据骨架...")
    
    # 创建所有可能的因子-窗口组合的骨架
    skeleton_records = []
    for factor_name, windows in factor_windows.items():
        for window in windows:
            for date in date_range:
                for stock in stock_list:
                    skeleton_records.append({
                        'trade_date': date,
                        'stock_code': stock,
                        'factor_name': factor_name,
                        'factor_value': np.nan,  # 👈 默认为NaN，让预处理器处理
                        'z_windows': window
                    })
    
    skeleton_df = pd.DataFrame(skeleton_records)
    logger.info(f"创建骨架数据: {len(skeleton_df)} 条记录")
    
    # 如果有已存在的数据，则用真实数据覆盖骨架中的NaN
    if existing_data is not None and not existing_data.empty:
        logger.info("用真实数据填充骨架...")
        
        # 确保列名一致
        existing_data = existing_data.copy()
        if 'field_name' in existing_data.columns:
            existing_data = existing_data.rename(columns={'field_name': 'factor_name'})
        if 'value' in existing_data.columns:
            existing_data = existing_data.rename(columns={'value': 'factor_value'})
        
        # 创建合并键
        merge_cols = ['trade_date', 'stock_code', 'factor_name', 'z_windows']
        
        # 合并数据：真实数据覆盖骨架中的NaN
        result_df = skeleton_df.merge(
            existing_data[merge_cols + ['factor_value']], 
            on=merge_cols, 
            how='left', 
            suffixes=('_skeleton', '_real')
        )
        
        # 用真实数据覆盖骨架数据
        mask_has_real_data = result_df['factor_value_real'].notna()
        result_df.loc[mask_has_real_data, 'factor_value_skeleton'] = result_df.loc[mask_has_real_data, 'factor_value_real']
        
        # 保留最终数据
        final_df = result_df[['trade_date', 'stock_code', 'factor_name', 'factor_value_skeleton', 'z_windows']].copy()
        final_df = final_df.rename(columns={'factor_value_skeleton': 'factor_value'})
        
        # 统计覆盖情况
        real_data_count = mask_has_real_data.sum()
        total_count = len(skeleton_df)
        logger.info(f"数据覆盖完成: {real_data_count}/{total_count} ({real_data_count/total_count*100:.1f}%) 有真实数据")
        
        return final_df
    else:
        logger.info("没有真实数据，返回全NaN骨架")
        return skeleton_df


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


def pivot_long_to_wide_simple(
    df: pd.DataFrame,
    factor_names: List[str],
    factor_name_col: str,
    value_col: str,
    lag_filter: int = 0
) -> pd.DataFrame:
    """
    Simplified long table to wide table conversion without mask generation
    
    Args:
        df: Long table format DataFrame
        factor_names: List of factor names to keep
        factor_name_col: Column name for factor_name
        value_col: Column name for factor_value
        lag_filter: Only keep specified lag value data
        
    Returns:
        Wide table format DataFrame
    """
    logger.info(f"Starting simplified long-to-wide conversion, data shape: {df.shape}")
    
    # Filter by lag if lag column exists
    if 'lag' in df.columns:
        df_filtered = df[df['lag'] == lag_filter].copy()
        logger.info(f"After lag filter ({lag_filter}): {df_filtered.shape}")
    else:
        df_filtered = df.copy()
    
    if df_filtered.empty:
        logger.warning("No data after lag filtering")
        return pd.DataFrame()
    
    # Filter to only include specified factors
    value_df = df_filtered[df_filtered[factor_name_col].isin(factor_names)].copy()
    
    if value_df.empty:
        logger.warning("No matching factors found")
        return pd.DataFrame()
    
    try:
        # Remove duplicates
        value_df = value_df.drop_duplicates(
            subset=['trade_date', 'stock_code', factor_name_col], 
            keep='first'
        )
        
        # Pivot to wide format
        wide = value_df.pivot_table(
            index=['trade_date', 'stock_code'],
            columns=factor_name_col,
            values=value_col,
            aggfunc='first'
        ).reset_index()
        
        # Flatten column names
        wide.columns.name = None
        
        logger.info(f"Pivot completed, wide table shape: {wide.shape}")
        logger.info(f"Factor columns: {[col for col in wide.columns if col in factor_names]}")
        
        return wide
        
    except Exception as e:
        logger.error(f"Pivot table creation failed: {str(e)}")
        logger.error(f"Available {factor_name_col} values: {df_filtered[factor_name_col].unique()}")
        raise


def _complete_date_reindex(df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    """
    在pivot后进行完整的日期reindex，确保所有股票在所有交易日都有记录
    
    Args:
        df: pivot后的wide格式DataFrame
        start_date: 开始日期 (YYYYMMDD)
        end_date: 结束日期 (YYYYMMDD)
        
    Returns:
        经过完整reindex的DataFrame，缺失的数据填充为NaN
    """
    if df.empty:
        return df
        
    # 获取实际的交易日历
    try:
        from src.utils.db_connection import db_config
        from sqlalchemy import text
        
        # 查询交易日历
        sql = text("""
        SELECT TRADE_DAYS
        FROM wind_quant.dbo.AShareCalendar
        WHERE S_INFO_EXCHMARKET='SSE'
        AND TRADE_DAYS >= :start_date
        AND TRADE_DAYS <= :end_date
        ORDER BY TRADE_DAYS ASC
        """)
        
        with db_config.get_wind_session() as session:
            result = session.execute(sql, {"start_date": start_date, "end_date": end_date})
            trading_dates = [str(row[0]) for row in result]
        
    except Exception as e:
        logger.warning(f"无法获取交易日历，使用现有日期: {str(e)}")
        # 降级处理：使用现有数据中的日期
        trading_dates = sorted(df['trade_date'].unique())
    
    # 获取所有股票
    all_stocks = df['stock_code'].unique()
    
    logger.info(f"完整reindex: {len(trading_dates)}个交易日 × {len(all_stocks)}个股票")
    
    # 创建完整的日期-股票索引
    full_index = pd.MultiIndex.from_product(
        [trading_dates, all_stocks], 
        names=['trade_date', 'stock_code']
    )
    
    # 设置原DataFrame的索引
    df_indexed = df.set_index(['trade_date', 'stock_code'])
    
    # Reindex到完整索引，缺失数据自动填充NaN
    df_reindexed = df_indexed.reindex(full_index)
    
    # 重置索引
    df_complete = df_reindexed.reset_index()
    
    original_rows = len(df)
    final_rows = len(df_complete)
    added_rows = final_rows - original_rows
    
    logger.info(f"完整reindex完成: {original_rows} → {final_rows} 行 (+{added_rows}行NaN记录)")
    
    return df_complete


def generate_lag_features_simple(
    df: pd.DataFrame, 
    factor_cols: List[str], 
    lag: int = 30
) -> pd.DataFrame:
    """
    Simplified lag feature generation without mask handling
    
    重要：为了匹配RNN/GRU的时间序列处理逻辑，生成的lag特征顺序为：
    - lag_29 (最早的历史数据) → 时间步 0
    - lag_28
    - ...
    - lag_1
    - lag_0 (最近的数据) → 时间步 29
    
    这样在最终的时间序列中，时间步从早到晚排列，符合时间序列模型的预期。
    
    Args:
        df: DataFrame with factors
        factor_cols: List of factor columns
        lag: Lag window size
        
    Returns:
        DataFrame with lag features in reverse chronological order
    """
    logger.info(f"Starting simplified lag feature generation, lag={lag}, factors={len(factor_cols)}")
    logger.info("生成逆序lag特征：从lag_29(最早)到lag_0(最近)，匹配RNN时间序列处理逻辑")
    
    if 'stock_code' not in df.columns or 'trade_date' not in df.columns:
        logger.error("DataFrame must contain stock_code and trade_date columns")
        return df
    
    # Sort by time to ensure correct lag calculation
    df = df.sort_values(['stock_code', 'trade_date']).reset_index(drop=True)
    logger.info("Data sorted by ['stock_code', 'trade_date']")
    
    # Check which factors actually exist
    factors_present = [f for f in factor_cols if f in df.columns]
    factors_missing = [f for f in factor_cols if f not in df.columns]
    
    if factors_missing:
        logger.warning(f"Missing factors (will be skipped): {factors_missing}")
    
    logger.info(f"Processing {len(factors_present)} existing factors")
    
    # Use MultiIndex for efficient groupby operations
    df = df.set_index(['stock_code', 'trade_date'])
    
    # Prepare output DataFrames
    out_dfs = []
    
    # Keep non-factor columns
    non_factor_cols = [col for col in df.columns if col not in factors_present]
    if non_factor_cols:
        base_df = df[non_factor_cols].copy()
        out_dfs.append(base_df)
    
    # Generate lag features for each factor in REVERSE order
    for factor in factors_present:
        factor_series = df[factor]
        factor_lag_dfs = []
        
        # 🚀 关键修改：逆序生成lag特征，从lag_29到lag_0
        # 这样时间序列的顺序就是：earliest → latest
        for i in range(lag-1, -1, -1):  # lag-1, lag-2, ..., 1, 0
            if i == 0:
                # lag-0 (current value)
                lag_df = factor_series.to_frame(f"{factor}_lag_{i}")
            else:
                # lag i (shifted value)
                shifted = (
                    factor_series.groupby('stock_code', sort=False)
                    .shift(i)
                    .to_frame(f"{factor}_lag_{i}")
                )
                lag_df = shifted
            
            factor_lag_dfs.append(lag_df)
        
        # Combine all lags for this factor
        factor_all_lags = pd.concat(factor_lag_dfs, axis=1)
        out_dfs.append(factor_all_lags)
    
    # Combine all factors
    if out_dfs:
        result_df = pd.concat(out_dfs, axis=1).reset_index()
    else:
        result_df = df.reset_index()
    
    logger.info(f"Lag feature generation completed, final shape: {result_df.shape}")
    logger.info(f"Generated lag feature columns: {len([col for col in result_df.columns if '_lag_' in col])}")
    logger.info("特征列顺序：lag_29(最早) → lag_0(最近)，适配RNN时间序列处理")
    
    return result_df


def _write_chunk(df: pd.DataFrame, shard_dir: Path, feature_cols: List[str], 
                 label: str, mask_cols: List[str]):
    # -------- ①  保留原始精度 ----------
    # 将 trade_date 统一格式化为 YYYYMMDD
    df['trade_date'] = (
        pd.to_datetime(df['trade_date'], errors='coerce')
          .dt.strftime('%Y%m%d')
    )

    # -------- ②  用临时时间戳提 year/month ----------
    df = df.assign(
        year  = df['trade_date'].str.slice(0, 4),
        month = df['trade_date'].str.slice(4, 6)
    )

    # -------- ③  （可选）删除多余列 ----------
    if 'index' in df.columns:
        df.drop(columns=['index'], inplace=True)

    # -------- ④  检查并补充缺失列（避免schema不匹配错误）----------
    expected_cols = set(feature_cols + mask_cols + [label, 'trade_date', 'stock_code', 'year', 'month'])
    actual_cols = set(df.columns)
    
    # 🚀 批量补充缺失的特征列和mask列，避免碎片化
    missing_feature_cols = set(feature_cols) - actual_cols
    missing_mask_cols = set(mask_cols) - actual_cols
    
    if missing_feature_cols:
        logger.warning(f"发现缺失的特征列: {missing_feature_cols}，将填充为0")
        # 批量创建缺失的特征列
        feature_cols_data = {col: np.full(len(df), np.float32(0.0), dtype=np.float32) for col in missing_feature_cols}
        feature_df = pd.DataFrame(feature_cols_data, index=df.index)
        df = pd.concat([df, feature_df], axis=1)
        logger.debug(f"批量添加了 {len(missing_feature_cols)} 个缺失特征列")
    
    if missing_mask_cols:
        logger.warning(f"发现缺失的mask列: {missing_mask_cols}，将填充为0")
        # 批量创建缺失的mask列
        mask_cols_data = {col: np.full(len(df), np.uint8(0), dtype=np.uint8) for col in missing_mask_cols}
        mask_df = pd.DataFrame(mask_cols_data, index=df.index)
        df = pd.concat([df, mask_df], axis=1)
        logger.debug(f"批量添加了 {len(missing_mask_cols)} 个缺失mask列")
    
    # 统一特征列数据类型为float32（避免Arrow转换警告）
    for col in feature_cols:
        if col in df.columns:
            df[col] = df[col].astype(np.float32)
    
    # 统一 mask dtype
    for col in mask_cols:
        if col in df.columns:
            df[col] = df[col].fillna(0).astype("uint8")
    
    # 重新计算actual_cols（因为前面可能补充了缺失列）
    actual_cols = set(df.columns)
    
    # 移除多余的列（除了必要列之外）
    extra_cols = actual_cols - expected_cols
    if extra_cols:
        logger.warning(f"发现多余列: {extra_cols}，将被移除")
        df = df.drop(columns=list(extra_cols))

    # -------- ⑤  建 Arrow schema ----------
    schema_dict = {c: pa.float32() for c in feature_cols}
    schema_dict[label] = pa.float32()
    for c in mask_cols:
        schema_dict[c] = pa.uint8()  # 使用uint8节省存储
    for c in ['trade_date', 'stock_code', 'year', 'month']:
        schema_dict[c] = pa.string()
    schema = pa.schema([pa.field(k, v) for k, v in schema_dict.items()])

    # -------- ⑤  写分区 ----------
    tbl = pa.Table.from_pandas(df, schema=schema, preserve_index=False)

    parquet_fmt = ds.ParquetFileFormat()
    file_opts   = parquet_fmt.make_write_options(compression='zstd')

    ds.write_dataset(
        tbl,
        base_dir=str(shard_dir),
        format=parquet_fmt,
        file_options=file_opts,
        partitioning=['year', 'month'],
        existing_data_behavior='overwrite_or_ignore',
    )


def _apply_splits(df: pd.DataFrame, rules: Sequence[Tuple[str, str, str]]):
    if not rules:
        return None
    # 不再指定 format="%Y%m%d"，允许自动识别日期格式
    ser = pd.to_datetime(df.trade_date, errors='coerce')
    split = pd.Series("unused", index=df.index, dtype="object")
    for name, s, e in rules:
        mask = (ser >= pd.Timestamp(s)) & (ser <= pd.Timestamp(e))
        split[mask] = name
    out = df[["trade_date", "stock_code"]].copy()
    #指定trade_date格式为YYYYMMDD，最好有通用性
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y%m%d")
    out["split"] = split
    return out


def _load_table_configs() -> dict:
    """Load table configurations from local_db_configs.yaml"""
    config_loader = ConfigLoader(config_dir='configs')
    try:
        return config_loader.load_config('db/local_db_configs.yaml')['tables']
    except Exception as e:
        logger.error(f"Failed to load table configurations: {str(e)}")
        raise


def _compute_date_label_stats(df: pd.DataFrame, label_col: str):
    """计算每个日期的标签统计量（均值和标准差）"""
    date_stats = {}
    grouped = df.groupby('trade_date')
    
    for date, group in grouped:
        values = group[label_col].values
        if len(values) == 0:
            date_stats[date] = (0.0, 1.0)
            continue
            
        mean = np.mean(values)
        std = np.std(values, ddof=0)
        
        if std == 0 or np.isnan(std):
            std = 1.0
            
        date_stats[date] = (mean, std)
    
    return date_stats


def _standardize_labels_by_date(df: pd.DataFrame, label_col: str, date_stats: dict):
    """按日期对标签进行标准化"""
    result_df = df.copy()
    
    # 计算全局均值和标准差，用于处理未见过的日期
    all_means = [m for m, _ in date_stats.values()]
    all_stds = [s for _, s in date_stats.values()]
    global_mean = sum(all_means) / len(all_means) if all_means else 0.0
    global_std = sum(all_stds) / len(all_stds) if all_stds else 1.0
    
    # 按日期应用标准化
    for date, group_indices in df.groupby('trade_date').groups.items():
        if date in date_stats:
            date_mean, date_std = date_stats[date]
        else:
            logger.warning(f"日期 {date} 未在统计信息中找到，使用全局均值={global_mean:.4f}和标准差={global_std:.4f}")
            date_mean, date_std = global_mean, global_std
            
        result_df.loc[group_indices, label_col] = (df.loc[group_indices, label_col] - date_mean) / date_std
    
    return result_df


def _generate_fixed_indices(splits_df: pd.DataFrame, meta_dir: Path):
    """生成固定顺序的索引文件，确保数据加载顺序一致性。"""
    logger.info("生成固定顺序的索引文件...")
    
    # 确保splits_df包含必要的列
    required_cols = ["trade_date", "stock_code", "split"]
    if not all(col in splits_df.columns for col in required_cols):
        logger.error(f"splits_df缺少必要的列: {required_cols}")
        return
    
    # 确保日期格式一致
    splits_df = splits_df.copy()
    splits_df["trade_date"] = pd.to_datetime(splits_df["trade_date"]).dt.strftime("%Y%m%d")
    
    # 1. 按照日期和股票代码排序，创建全局索引
    all_indices = splits_df.sort_values(["trade_date", "stock_code"]).copy()
    all_indices["index_id"] = np.arange(len(all_indices))
    
    # 保存全局索引文件
    full_indices_path = meta_dir / "full_indices.parquet"
    pq.write_table(
        pa.Table.from_pandas(all_indices),
        full_indices_path,
        compression="zstd"
    )
    logger.info(f"全局固定索引已保存至 {full_indices_path}，包含 {len(all_indices)} 条记录")
    
    # 2. 为每个split生成专用索引文件
    for split_name in splits_df["split"].unique():
        if split_name == "unused":
            continue
            
        split_indices = all_indices[all_indices["split"] == split_name].copy()
        # 重新排序并编号
        split_indices = split_indices.sort_values(["trade_date", "stock_code"])
        split_indices["index_id"] = np.arange(len(split_indices))
        
        # 保存到文件
        split_path = meta_dir / f"{split_name}_indices.parquet"
        pq.write_table(
            pa.Table.from_pandas(split_indices),
            split_path,
            compression="zstd"
        )
        logger.info(f"{split_name}分割集固定索引已保存至 {split_path}，包含 {len(split_indices)} 条记录")


def _resolve_feature_sources(features_tables: List[str], factor_windows: Dict[str, List[int]], 
                           prov: LocalTestDBDataProvider) -> Dict[Tuple[str, int], Dict[str, str]]:
    """
    解析多个特征表，确定每个因子应该从哪个表获取数据
    
    Args:
        features_tables: 特征表列表
        factor_windows: 因子窗口配置
        prov: 数据提供者
        
    Returns:
        Dict[Tuple[str, int], Dict[str, str]]: 
        {(factor_name, window): {'table': table_name, 'factor_col': col_name, 'win_col': col_name}}
    """
    logger.info(f"🔍 开始解析 {len(features_tables)} 个特征表的字段分布...")
    
    mapping = {}
    duplicates = []
    
    # 🚀 第一步：获取每个表中实际存在的因子
    table_factors = {}  # {table_name: {factor_name: [windows]}}
    
    for table_name in features_tables:
        try:
            # 获取表的所有字段
            available_cols = prov.list_fields(table_name)
            logger.info(f"  📋 表 {table_name} 包含 {len(available_cols)} 个字段")
            
            # 自动探测列名
            factor_col = None  
            win_col = None
            
            # 按优先级尝试因子名列
            for possible_name in ['factor_name', 'field_name', 'feature_name']:
                if possible_name in available_cols:
                    factor_col = possible_name
                    break
            
            # 按优先级尝试窗口列
            for possible_name in ['z_windows', 'z_window', 'window']:
                if possible_name in available_cols:
                    win_col = possible_name
                    break
                    
            if factor_col is None:
                logger.warning(f"  ❌ 表 {table_name} 缺少因子名列，跳过")
                continue
                
            if win_col is None:
                logger.info(f"  ⚠️  表 {table_name} 没有窗口列，所有因子将使用默认窗口=0")
            
            logger.info(f"  ✅ 表 {table_name} 列名映射: 因子列={factor_col}, 窗口列={win_col}")
            
            # 🚀 关键修复：查询表中实际存在的因子（优化：只查询最近一个月数据）
            logger.info(f"  🔍 正在查询表 {table_name} 中的实际因子（最近30天数据）...")
            
            # 获取最近日期用于优化查询
            recent_date_sql = f"SELECT MAX(trade_date) as max_date FROM {table_name}"
            with prov._get_engine().connect() as conn:
                max_date_result = pd.read_sql(recent_date_sql, conn)
                if not max_date_result.empty and max_date_result['max_date'].iloc[0] is not None:
                    max_date = pd.to_datetime(max_date_result['max_date'].iloc[0])
                    # 计算30天前的日期
                    recent_date = (max_date - pd.Timedelta(days=30)).strftime('%Y%m%d')
                    date_filter = f" WHERE trade_date >= '{recent_date}'"
                else:
                    # 如果无法获取最大日期，使用LIMIT限制
                    date_filter = ""
            
            # 构建优化后的查询：只查最近数据 + LIMIT
            if win_col:
                if date_filter:
                    query_sql = f"SELECT DISTINCT {factor_col}, {win_col} FROM {table_name}{date_filter} LIMIT 10000"
                else:
                    query_sql = f"SELECT DISTINCT {factor_col}, {win_col} FROM {table_name} LIMIT 10000"
            else:
                if date_filter:
                    query_sql = f"SELECT DISTINCT {factor_col} FROM {table_name}{date_filter} LIMIT 5000"
                else:
                    query_sql = f"SELECT DISTINCT {factor_col} FROM {table_name} LIMIT 5000"
            
            logger.debug(f"    SQL: {query_sql}")
            
            # 执行查询
            with prov._get_engine().connect() as conn:
                result_df = pd.read_sql(query_sql, conn)
            
            if result_df.empty:
                logger.warning(f"  ⚠️  表 {table_name} 中没有数据，跳过")
                continue
            
            # 解析结果
            table_factors[table_name] = {}
            actual_factors_count = 0
            
            for _, row in result_df.iterrows():
                factor_name = row[factor_col]
                window = int(row[win_col]) if win_col and win_col in row else 0
                
                if factor_name not in table_factors[table_name]:
                    table_factors[table_name][factor_name] = []
                table_factors[table_name][factor_name].append(window)
                actual_factors_count += 1
            
            logger.info(f"  📊 表 {table_name} 实际包含 {len(table_factors[table_name])} 个不同因子，{actual_factors_count} 个因子-窗口组合")
            
            # 存储表结构信息
            table_factors[table_name]['_meta'] = {
                'factor_col': factor_col,
                'win_col': win_col
            }
                    
        except Exception as e:
            logger.error(f"  ❌ 解析表 {table_name} 时出错: {str(e)}")
            continue
    
    # 🚀 第二步：只为实际存在的因子创建映射
    logger.info("🔧 创建因子路由映射...")
    
    for factor_name, windows in factor_windows.items():
        for window in windows:
            key = (factor_name, window)
            
            # 查找哪些表包含这个因子-窗口组合
            candidate_tables = []
            for table_name, factors_info in table_factors.items():
                if factor_name in factors_info and window in factors_info[factor_name]:
                    candidate_tables.append(table_name)
            
            if not candidate_tables:
                logger.debug(f"  ⚠️  因子 {factor_name}_w{window} 在任何表中都不存在")
                continue
            
            if len(candidate_tables) > 1:
                # 真正的重复：同一个因子-窗口组合在多个表中存在
                duplicates.append((key, candidate_tables[0], candidate_tables[1:]))
                selected_table = candidate_tables[0]
                logger.warning(f"  🚨 因子 {factor_name}_w{window} 在多个表中存在: {candidate_tables}，选择 {selected_table}")
            else:
                selected_table = candidate_tables[0]
            
            # 创建映射
            table_meta = table_factors[selected_table]['_meta']
            mapping[key] = {
                'table': selected_table,
                'factor_col': table_meta['factor_col'],
                'win_col': table_meta['win_col']
            }
    
    # 报告重复情况（真正的重复）
    if duplicates:
        logger.warning(f"🚨 发现 {len(duplicates)} 个真正的重复因子:")
        for key, first_table, other_tables in duplicates:
            logger.warning(f"  - 因子 {key[0]}_w{key[1]}: 选择 {first_table}，忽略 {other_tables}")
    
    logger.info(f"✅ 因子路由映射完成，共创建 {len(mapping)} 个有效映射")
    
    # 统计每个表将被使用的因子数量
    table_usage = {}
    for key, source_info in mapping.items():
        table = source_info['table']
        table_usage[table] = table_usage.get(table, 0) + 1
        
    logger.info("📊 各表实际使用统计:")
    for table, count in table_usage.items():
        logger.info(f"  - {table}: {count} 个因子")
    
    return mapping


def _fetch_join_filter_chunk_multi(prov: LocalTestDBDataProvider, s: str, e: str, lag: int,
                                  label: str, factor_windows: Dict[str, List[int]], 
                                  factor_source_map: Dict[Tuple[str, int], Dict[str, str]],
                                  y_table: str, restricted: Set[tuple[str, str]],
                                  label_shift: int = 10,
                                  stats_table: str = None,
                                  clip_std: bool = True,
                                  factor_based_nan_handling: bool = False,
                                  consecutive_nan_threshold: Optional[int] = None):
    """
    多表模式的数据获取和处理函数
    
    Args:
        prov: 数据提供者
        s, e: 开始和结束日期
        lag: lag窗口大小
        label: 标签名称
        factor_windows: 因子窗口配置
        factor_source_map: 因子源映射表
        y_table: 标签表名称
        restricted: 受限股票集合
        label_shift: 标签偏移参数
        stats_table: 统计表名称
        clip_std: 是否应用截断
        factor_based_nan_handling: 是否应用基于因子的NaN处理
        consecutive_nan_threshold: 连续NaN阈值
        
    Returns:
        处理后的DataFrame
    """
    # 计算扩展的开始日期用于lag特征
    fetch_start = _get_trading_days_before(s, lag - 1)
    logger.info(f"🔄 多表模式数据获取 ({fetch_start} to {e}, 目标: {s} to {e})...")
    
    # 按表分组获取数据
    table_factor_groups = {}
    for (factor_name, window), source_info in factor_source_map.items():
        table = source_info['table']
        if table not in table_factor_groups:
            table_factor_groups[table] = {'factors': [], 'windows': [], 'source_info': source_info}
        table_factor_groups[table]['factors'].append(factor_name)
        table_factor_groups[table]['windows'].append(window)
    
    logger.info(f"📦 将从 {len(table_factor_groups)} 个表获取数据")
    
    # 从每个表获取数据
    long_dfs = []
    for table_name, group_info in table_factor_groups.items():
        factors = group_info['factors']
        windows = group_info['windows']
        source_info = group_info['source_info']
        
        factor_col = source_info['factor_col']
        win_col = source_info['win_col']
        
        logger.info(f"  📊 从表 {table_name} 获取 {len(set(factors))} 个不同因子的数据...")
        
        try:
            # 构建过滤条件
            filters = {factor_col: list(set(factors))}  # 去重
            if win_col:
                filters[win_col] = list(set(windows))  # 去重
                
            # 获取数据
            df_part = prov.fetch_data(
                table=table_name,
                start_date=fetch_start,
                end_date=e,
                format="long",
                column_filters=filters
            )
            
            if df_part.empty:
                logger.warning(f"    ⚠️  表 {table_name} 返回空数据")
                continue
            
            # 标准化列名
            rename_map = {}
            if factor_col != 'factor_name':
                rename_map[factor_col] = 'factor_name'
            if 'value' in df_part.columns and 'factor_value' not in df_part.columns:
                rename_map['value'] = 'factor_value'
            if 'field_name' in df_part.columns and 'factor_name' not in rename_map.values():
                rename_map['field_name'] = 'factor_name'
                
            if rename_map:
                df_part = df_part.rename(columns=rename_map)
            
            # 处理窗口列
            if win_col and win_col in df_part.columns:
                if win_col != 'z_windows':
                    df_part = df_part.rename(columns={win_col: 'z_windows'})
            else:
                # 没有窗口列，默认设置为0
                df_part['z_windows'] = 0
                logger.info(f"    🔧 表 {table_name} 无窗口列，设置默认窗口=0")
            
            # 确保数据类型正确
            df_part['trade_date'] = pd.to_datetime(df_part['trade_date']).dt.strftime('%Y%m%d')
            df_part['stock_code'] = df_part['stock_code'].astype(str)
            
            # 只保留需要的列
            required_cols = ['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']
            available_cols = [col for col in required_cols if col in df_part.columns]
            df_part = df_part[available_cols]
            
            long_dfs.append(df_part)
            logger.info(f"    ✅ 成功获取 {len(df_part)} 条记录")
            
        except Exception as e:
            logger.error(f"    ❌ 从表 {table_name} 获取数据失败: {str(e)}")
            continue
    
    if not long_dfs:
        logger.warning("❌ 所有表都没有返回有效数据")
        return pd.DataFrame()
    
    # 合并所有长表数据
    features_long = pd.concat(long_dfs, ignore_index=True)
    logger.info(f"📋 合并后长表数据: {features_long.shape}")
    
    # 获取数据范围和股票列表
    date_range = sorted(features_long['trade_date'].unique())
    stock_list = sorted(features_long['stock_code'].unique())
    
    # 创建完整的因子骨架
    logger.info("🔧 创建完整因子骨架...")
    features_df = _create_complete_factor_skeleton(
        date_range, stock_list, factor_windows, features_long
    )
    
    if features_df.empty:
        logger.warning("创建的因子骨架为空，无法继续处理")
        return pd.DataFrame()
    
    # 长表阶段的因子预处理
    logger.info("🎯 长表阶段因子预处理...")
    if factor_based_nan_handling:
        preprocessor = FactorPreprocessor()
        features_df = preprocessor.preprocess_factors_long(features_df, factor_windows, consecutive_nan_threshold)
        logger.info("✅ 长表阶段因子预处理完成")
    else:
        logger.info("未启用高级NaN处理，手动添加窗口后缀...")
        features_df['factor_name'] = (
            features_df['factor_name'] + '_w' + features_df['z_windows'].astype(int).astype(str)
        )
    
    # 获取所有因子名称（现在包含窗口后缀）
    factor_names = features_df['factor_name'].unique().tolist()
    logger.info(f"处理后的因子数量: {len(factor_names)}")
    
    # 将长表转换为宽表
    logger.info("将长表转换为宽表...")
    features_wide = pivot_long_to_wide_simple(features_df, factor_names, 
                                           factor_name_col='factor_name', 
                                           value_col='factor_value', 
                                           lag_filter=0)
    
    if features_wide.empty:
        logger.warning(f"转换为宽表后数据为空")
        return pd.DataFrame()
    
    # 完整的日期reindex
    logger.info("执行完整的日期reindex...")
    features_wide = _complete_date_reindex(features_wide, fetch_start, e)
    
    # 生成lag特征
    logger.info(f"生成lag特征，lag={lag}...")
    features_lagged = generate_lag_features_simple(features_wide, factor_names, lag)
    
    # 截断到目标日期范围
    if not features_lagged.empty:
        mask_target_range = (features_lagged['trade_date'] >= s) & (features_lagged['trade_date'] <= e)
        features_lagged = features_lagged[mask_target_range]
        logger.info(f"截断到目标日期范围: {s} to {e}")
    
    # 应用zscore转换
    if stats_table:
        logger.info(f"应用zscore转换，使用统计表 {stats_table}...")
        stats = _load_stats_with_window(prov, stats_table, clip_std)
        features_lagged = _apply_zscore_with_window(features_lagged, stats, clip_std, factor_windows)
    
    # ──────────────────────────────────────────────
    # 🆕 允许 "不取 label" 以加速实时推理
    #     只要 y_table 传 None / ""，就直接跳过下面所有 label-join 逻辑
    #     并在返回前应用受限股票过滤（如有）
    # ──────────────────────────────────────────────
    if (y_table is None) or (y_table == ""):
        logger.info("⚡ 跳过标签获取与 join（实时推理不需要 label）")
        out_df = features_lagged.copy()
        if restricted and not out_df.empty:
            mi = pd.MultiIndex.from_arrays([
                pd.to_datetime(out_df['trade_date']).dt.strftime('%Y%m%d'),
                out_df['stock_code'].astype(str),
            ])
            mask = ~mi.isin(restricted)
            before_rows = len(out_df)
            out_df = out_df[mask]
            logger.info(f"受限股票过滤（无label路径）：{before_rows} → {len(out_df)} 行")
        return out_df
    
    # 获取标签数据
    logger.info(f"获取标签数据从 {y_table} ({s} to {e})...")
    labels_df = prov.fetch_data(
        table=y_table,
        start_date=s,
        end_date=e,
        fields=["trade_date", "stock_code", "field_name", "value", "label_shift"],
        format="long"
    )
    
    if labels_df.empty:
        logger.warning(f"标签表 {y_table} 返回空数据")
        return pd.DataFrame()
    
    # 处理标签数据
    labels_df['trade_date'] = pd.to_datetime(labels_df['trade_date']).dt.strftime('%Y%m%d')
    labels_df['stock_code'] = labels_df['stock_code'].astype(str)
    
    # 过滤标签
    labels_df = labels_df[
        (labels_df['field_name'] == label) & 
        (labels_df['label_shift'] == label_shift)
    ].copy()
    
    if labels_df.empty:
        logger.warning(f"没有找到标签 {label} 与 label_shift={label_shift} 的数据")
        return pd.DataFrame()
    
    # 重命名标签列
    labels_df = labels_df.rename(columns={'value': label})
    labels_df = labels_df.drop(columns=['field_name', 'label_shift'])
    
    # 合并特征和标签
    logger.info("合并特征和标签...")
    df = pd.merge(
        features_lagged, 
        labels_df,
        on=['trade_date', 'stock_code'],
        how='inner'
    )
    
    if df.empty:
        logger.warning("合并特征和标签后数据为空")
        return df
    
    # 清理NaN数据
    if not df.empty:
        initial_rows = len(df)
        
        # 移除标签为NaN的行
        if label in df.columns:
            df = df.dropna(subset=[label])
            
        # 移除特征列中的NaN行
        feature_cols = [col for col in df.columns if '_lag_' in col]
        if feature_cols:
            df = df.dropna(subset=feature_cols)
            
        final_rows = len(df)
        logger.info(f"数据清理完成: {initial_rows} → {final_rows} 行")
    
    # 过滤受限股票
    if restricted and not df.empty:
        initial_rows = len(df)
        mask = ~pd.MultiIndex.from_arrays([df.trade_date, df.stock_code]).isin(restricted)
        df = df[mask]
        final_rows = len(df)
        logger.info(f"过滤受限股票: {initial_rows} → {final_rows} 行")
    
    logger.info(f"多表模式数据处理完成，最终数据形状: {df.shape}")
    return df

# ────────────────────────────────────────────────────────────────
#                         Main entry
# ────────────────────────────────────────────────────────────────

def build_pv_dataset_streaming(
    output_dir: str | Path = "data/Dataset/pv_v2",
    start_date: str = "20120101",
    end_date: str = "20241231",
    lag: int = 30,
    label_name: str = "label_raw",
    factor_windows: Dict[str, List[int]] = None,
    label_shift: int = 10,
    winsorise_labels: bool = True,
    label_winsor_q: Tuple[float, float] = (0.0005, 0.9995),
    standardize_labels_by_date: bool = False,
    split_rules: Sequence[Tuple[str, str, str]] | None = None,
    chunk_freq: str = "M",
    stats_table: str = None,  # New parameter for statistics table
    clip_std: bool = True,    # New parameter for clipping
    features_table: Union[str, Sequence[str]] = "ai_is.inter_train_factors_mkt_processed_v1",  # 支持多表
    labels_table: str = "ai_is.training_label_ls10_adj_topcor_cr30_cw240",
    restricted_table: str = "ai_is.forbid_pool_comprehensive",
    enable_masks: bool = True,
    factor_based_nan_handling: bool = False,  # New parameter for factor-based NaN handling
    consecutive_nan_threshold: Optional[int] = None,  # 🚀 连续NaN阈值：超过N天连续NaN则保持不填充
):
    out = Path(output_dir)
    meta_dir, shard_dir = _ensure_dirs(out)
    prov = LocalTestDBDataProvider()

    # 使用默认因子窗口配置如果未提供
    if factor_windows is None:
        factor_windows = FACTOR_WINDOWS.copy()
    
    # 🚀 标准化 features_table 参数：支持单表或多表
    if isinstance(features_table, str):
        features_tables = [features_table]
        logger.info(f"🔧 单表模式: {features_table}")
    else:
        features_tables = list(features_table)
        logger.info(f"🔧 多表模式: {len(features_tables)} 个表")
        for i, table in enumerate(features_tables, 1):
            logger.info(f"  {i}. {table}")
    
    # 生成所有因子名称（带窗口后缀）
    factor_names = get_all_factor_names()
    
    # Simplified: no mask columns in simplified processing
    mask_cols = []  # Empty mask list for simplified processing
    logger.info(f"Simplified processing: mask generation disabled")

    # Load table configurations and validate
    try:
        table_configs = _load_table_configs()
        
        # 验证所有特征表 + 其他表
        tables_to_check = features_tables + [labels_table, restricted_table]
        if stats_table:
            tables_to_check.append(stats_table)
        
        for table in tables_to_check:
            if table not in table_configs:
                logger.warning(f"Table {table} not found in configuration, proceeding anyway")
            else:
                config = table_configs[table]
                logger.info(f"Using table {table} with type {config.get('table_type', 'unknown')}")
    except Exception as e:
        logger.warning(f"Could not load table configurations: {str(e)}, proceeding anyway")

    restricted = _load_restricted_set(prov, start_date, end_date, restricted_table)
    
    # 🚀 多表路由：解析每个因子应该从哪个表获取
    factor_source_map = None
    if len(features_tables) > 1:
        logger.info("🚀 启用多表路由模式...")
        factor_source_map = _resolve_feature_sources(features_tables, factor_windows, prov)
        if not factor_source_map:
            raise ValueError("多表路由失败：没有找到任何可用的因子映射")
    else:
        logger.info("使用单表模式，无需路由")

    # Generate feature column names based on lag (only factor columns, no masks)
    # 🚀 关键修改：与generate_lag_features_simple保持一致，使用逆序生成特征列名
    # 从lag_29到lag_0，确保时间序列顺序正确
    factor_lag_cols = [f"{c}_lag_{i}" for c in factor_names for i in range(lag-1, -1, -1)]
    feature_cols = factor_lag_cols  # No mask columns in simplified version

    # 用于存储所有日期的标签统计信息
    date_label_stats = {}
    # 如果启用了日期标签标准化，先进行一次数据采集，计算每个日期的标签统计量
    if standardize_labels_by_date:
        logger.info("开始执行日期标签标准化，计算每个日期的标签统计信息...")
        all_date_labels = []
        
        ranges_for_stats = list(_iter_ranges(start_date, end_date, chunk_freq))
        pbar_stats = tqdm(ranges_for_stats, desc="采集日期标签统计")
        for s, e in pbar_stats:
            df = _fetch_labels_chunk(prov, s, e, label_name, labels_table, restricted, label_shift)
            if not df.empty:
                all_date_labels.append(df[['trade_date', label_name]])
            pbar_stats.set_postfix(date=f"{s}-{e}")
            
        if all_date_labels:
            all_labels_df = pd.concat(all_date_labels, ignore_index=True)
            date_label_stats = _compute_date_label_stats(all_labels_df, label_name)
            logger.info(f"已完成 {len(date_label_stats)} 个日期的标签统计")
            
            # 保存日期标签统计信息
            with open(meta_dir / "date_label_stats.json", "w", encoding="utf-8") as fp:
                json.dump({k: [float(v[0]), float(v[1])] for k, v in date_label_stats.items()}, 
                          fp, indent=2, ensure_ascii=False)
        else:
            logger.warning("未能采集到任何日期的标签数据，日期标准化将被禁用")
            standardize_labels_by_date = False

    splits_accum = []  # collect for optional split index
    ranges = list(_iter_ranges(start_date, end_date, chunk_freq))
    pbar = tqdm(ranges, desc=" building {}".format(output_dir))
    for s, e in pbar:
        # 🚀 根据表数量选择处理函数
        if factor_source_map is None:
            # 单表模式：使用原有函数
            df = _fetch_join_filter_chunk_long(prov, s, e, lag, label_name, factor_windows,
                                            features_tables[0], labels_table, restricted,
                                            label_shift, stats_table=stats_table, clip_std=clip_std,
                                            factor_based_nan_handling=factor_based_nan_handling,
                                            consecutive_nan_threshold=consecutive_nan_threshold)
        else:
            # 多表模式：使用新的多表处理函数
            df = _fetch_join_filter_chunk_multi(prov, s, e, lag, label_name, factor_windows,
                                              factor_source_map, labels_table, restricted,
                                              label_shift, stats_table=stats_table, clip_std=clip_std,
                                              factor_based_nan_handling=factor_based_nan_handling,
                                              consecutive_nan_threshold=consecutive_nan_threshold)
        if df.empty:
            continue
        
        # 应用日期标准化（如果启用）
        if standardize_labels_by_date and date_label_stats:
            logger.debug(f"对日期范围 {s}-{e} 的标签按日期进行标准化")
            df = _standardize_labels_by_date(df, label_name, date_label_stats)
            
        # Apply label winsorization by date if enabled
        if not df.empty and winsorise_labels and label_name in df.columns:
            logger.info("Applying label winsorization by date...")
            df = winsorize_labels_by_date(df, label_name, label_winsor_q)
            
        # Final check - should not have NaN at this point due to dropna()
        if not df.empty:
            nan_count = df.isnull().sum().sum()
            if nan_count > 0:
                logger.warning(f"Found {nan_count} NaN values after processing, dropping...")
                df = df.dropna()
            
        if split_rules:
            splits_accum.append(df[["trade_date", "stock_code"]])
        _write_chunk_simple(df, shard_dir, feature_cols, label_name)
        rows = len(df)
        del df
        gc.collect()
        pbar.set_postfix(rows=f"{rows:,}")

    # write split index if needed
    if split_rules and splits_accum:
        df_all = pd.concat(splits_accum, ignore_index=True)
        splits = _apply_splits(df_all, split_rules)
        if splits is not None:
            pq.write_table(pa.Table.from_pandas(splits), meta_dir / "splits.parquet", compression="zstd")
            # 生成固定索引文件
            _generate_fixed_indices(splits, meta_dir)
    
    # 如果未生成固定索引文件（无分割规则或分割生成失败），则直接从shards创建
    full_indices_path = meta_dir / "full_indices.parquet"
    if not full_indices_path.exists():
        logger.info("未发现固定索引文件，从shards目录直接创建全局索引")
        try:
            # 说明：优先尝试从当前工作目录导入 generate_fixed_indices.py，若不存在则退化为以子进程方式执行同名脚本。
            # 这样可以避免 IDE 对相对导入的静态告警，同时在脚本缺失时优雅降级，不阻塞主流程。
            try:
                from generate_fixed_indices import generate_fixed_indices
                generate_fixed_indices(output_dir)
            except ImportError:
                # 如果找不到模块，尝试执行命令
                import subprocess
                subprocess.run([sys.executable, "generate_fixed_indices.py", output_dir], check=True)
            
            if full_indices_path.exists():
                logger.info(f"成功创建固定索引文件: {full_indices_path}")
            else:
                logger.warning("尝试创建固定索引文件失败")
        except Exception as e:
            logger.error(f"创建固定索引时出错: {e}")

    # Save schema metadata (simplified version without masks)
    schema_dict = {
        "feature_cols": feature_cols,  # Only factor lag columns
        "factor_lag_cols": factor_lag_cols,  # Same as feature_cols in simplified version
        "label_col": label_name,
        "index_cols": ["trade_date", "stock_code"],
        "mask_cols": mask_cols,  # Empty in simplified version
        "factor_names": list(factor_windows.keys()),  # 原始因子名称
        "factor_windows": factor_windows,  # 因子窗口配置
        "expanded_factor_names": factor_names,  # 带窗口后缀的因子名称
        "feature_lag": lag,
        "n_base_features": len(factor_names),  # Only factors, no masks
        "n_total_features": len(feature_cols),
        "n_masks": 0,  # No masks in simplified version
        "winsorise_labels": winsorise_labels,
        "label_winsor_q": list(label_winsor_q),
        "standardize_labels_by_date": standardize_labels_by_date,
        "clip_std": clip_std,
        "factor_based_nan_handling": factor_based_nan_handling,  # Record NaN handling strategy
        "consecutive_nan_threshold": consecutive_nan_threshold,  # Record consecutive NaN threshold
        "simplified_nan_handling": True,  # Mark as simplified processing
        "multi_table_mode": len(features_tables) > 1,  # Record whether multi-table mode was used
        "num_feature_tables": len(features_tables),  # Record number of feature tables
        "build_start_date": start_date,
        "build_end_date": end_date,
        "tables": {
            "features": [{"name": table, "type": "long", "description": f"Long format factor table with processed values: {table}"} for table in features_tables] if len(features_tables) > 1 else {
                "name": features_tables[0],
                "type": "long",
                "description": "Long format factor table with processed values"
            },
            "labels": {
                "name": labels_table,
                "type": "long", 
                "description": "Label table in long format"
            },
            "restricted": {
                "name": restricted_table,
                "type": "flag",
                "description": "Restricted stock pool flag table"
            }
        }
    }
    
    # Add stats table info if provided
    if stats_table:
        schema_dict["tables"]["stats"] = {
            "name": stats_table,
            "type": "stat",
            "description": "Statistics table for zscore transformation"
        }
    
    # Add multi-table routing info if applicable
    if factor_source_map:
        # Convert the routing map to a serializable format
        routing_info = {}
        for (factor_name, window), source_info in factor_source_map.items():
            key = f"{factor_name}_w{window}"
            routing_info[key] = {
                "source_table": source_info["table"],
                "factor_column": source_info["factor_col"],
                "window_column": source_info["win_col"]
            }
        schema_dict["factor_routing"] = routing_info
        
        # Add routing summary
        table_factor_counts = {}
        for source_info in factor_source_map.values():
            table = source_info["table"]
            table_factor_counts[table] = table_factor_counts.get(table, 0) + 1
        schema_dict["routing_summary"] = table_factor_counts
    
    with open(meta_dir / "schema.json", "w", encoding="utf-8") as fp:
        json.dump(schema_dict, fp, indent=2, ensure_ascii=False)

    logger.info("Dataset build completed → %s", out)


def _write_chunk_simple(df: pd.DataFrame, shard_dir: Path, feature_cols: List[str], label: str):
    """
    Simplified chunk writing without mask columns
    """
    # Format trade_date consistently
    df['trade_date'] = (
        pd.to_datetime(df['trade_date'], errors='coerce')
          .dt.strftime('%Y%m%d')
    )

    # Add year/month for partitioning
    df = df.assign(
        year  = df['trade_date'].str.slice(0, 4),
        month = df['trade_date'].str.slice(4, 6)
    )

    # Remove index column if present
    if 'index' in df.columns:
        df.drop(columns=['index'], inplace=True)

    # Check and supplement missing feature columns
    expected_cols = set(feature_cols + [label, 'trade_date', 'stock_code', 'year', 'month'])
    actual_cols = set(df.columns)
    
    missing_feature_cols = set(feature_cols) - actual_cols
    
    # 🚀 批量添加缺失列，避免DataFrame碎片化
    if missing_feature_cols:
        logger.warning(f"Missing feature columns: {missing_feature_cols}, filling with 0")
        # 一次性创建所有缺失列的数据
        missing_cols_data = {col: np.full(len(df), np.float32(0.0), dtype=np.float32) for col in missing_feature_cols}
        missing_df = pd.DataFrame(missing_cols_data, index=df.index)
        df = pd.concat([df, missing_df], axis=1)
        logger.debug(f"批量添加了 {len(missing_feature_cols)} 个缺失特征列")
    
    # Remove extra columns
    extra_cols = actual_cols - expected_cols
    if extra_cols:
        logger.warning(f"Extra columns found: {extra_cols}, will be removed")
        df = df.drop(columns=list(extra_cols))

    # Create Arrow schema
    schema_dict = {c: pa.float32() for c in feature_cols}
    schema_dict[label] = pa.float32()
    for c in ['trade_date', 'stock_code', 'year', 'month']:
        schema_dict[c] = pa.string()
    schema = pa.schema([pa.field(k, v) for k, v in schema_dict.items()])

    # Write partitioned data
    tbl = pa.Table.from_pandas(df, schema=schema, preserve_index=False)

    parquet_fmt = ds.ParquetFileFormat()
    file_opts   = parquet_fmt.make_write_options(compression='zstd')

    ds.write_dataset(
        tbl,
        base_dir=str(shard_dir),
        format=parquet_fmt,
        file_options=file_opts,
        partitioning=['year', 'month'],
        existing_data_behavior='overwrite_or_ignore',
    )

