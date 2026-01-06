# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from typing import Dict, List, Optional
import numpy as np
import pandas as pd


def _create_complete_factor_skeleton(date_range: List[str], stock_list: List[str], 
                                   factor_windows: Dict[str, List[int]], 
                                   existing_data: pd.DataFrame = None) -> pd.DataFrame:
    logger.info("🔧 为所有定义因子创建完整数据骨架...")
    skeleton_records = []
    for factor_name, windows in factor_windows.items():
        for window in windows:
            for date in date_range:
                for stock in stock_list:
                    skeleton_records.append({
                        'trade_date': date,
                        'stock_code': stock,
                        'factor_name': factor_name,
                        'factor_value': np.nan,
                        'z_windows': window
                    })
    skeleton_df = pd.DataFrame(skeleton_records)
    logger.info(f"创建骨架数据: {len(skeleton_df)} 条记录")
    if existing_data is not None and not existing_data.empty:
        logger.info("用真实数据填充骨架...")
        existing_data = existing_data.copy()
        if 'field_name' in existing_data.columns:
            existing_data = existing_data.rename(columns={'field_name': 'factor_name'})
        if 'value' in existing_data.columns:
            existing_data = existing_data.rename(columns={'value': 'factor_value'})
        merge_cols = ['trade_date', 'stock_code', 'factor_name', 'z_windows']
        result_df = skeleton_df.merge(
            existing_data[merge_cols + ['factor_value']], 
            on=merge_cols, 
            how='left', 
            suffixes=('_skeleton', '_real')
        )
        mask_has_real_data = result_df['factor_value_real'].notna()
        result_df.loc[mask_has_real_data, 'factor_value_skeleton'] = result_df.loc[mask_has_real_data, 'factor_value_real']
        final_df = result_df[['trade_date', 'stock_code', 'factor_name', 'factor_value_skeleton', 'z_windows']].copy()
        final_df = final_df.rename(columns={'factor_value_skeleton': 'factor_value'})
        return final_df
    else:
        logger.info("没有真实数据，返回全NaN骨架")
        return skeleton_df


