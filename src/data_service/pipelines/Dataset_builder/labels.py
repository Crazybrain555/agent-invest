# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from typing import Dict, List, Optional, Set, Tuple
import numpy as np
import pandas as pd

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider


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


