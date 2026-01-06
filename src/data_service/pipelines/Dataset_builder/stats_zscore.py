# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from typing import Dict, List, Optional, Set, Tuple
import numpy as np
import pandas as pd

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider


def _load_zscore_whitelist():
    """加载 zscore 白名单配置"""
    try:
        from src.utils.config_loader import ConfigLoader
        config_loader = ConfigLoader(config_dir='configs')
        whitelist_config = config_loader.load_config('dataset/zscore_whitelist.yaml')
        
        if not whitelist_config.get('rules', {}).get('enabled', True):
            return set()
            
        whitelist = set(whitelist_config.get('whitelist_factors', []))
        if whitelist:
            logger.info(f"加载 zscore 白名单：{len(whitelist)} 个因子")
        return whitelist
    except Exception as e:
        logger.debug(f"加载 zscore 白名单配置失败: {str(e)}，使用空白名单")
        return set()


def _load_stats_with_window(prov: LocalTestDBDataProvider, table: str, clip: bool):
    stats_df = prov.fetch_data(table=table)
    stats_df = stats_df.set_index(['feature_name', 'window'])
    cols = ["mean", "std", "lower", "upper"] if clip else ["mean", "std"]
    return stats_df[cols]


def _apply_zscore_with_window(
    df: pd.DataFrame,
    stats,
    clip: bool,
    factor_windows: Optional[Dict[str, List[int]]] = None,
) -> pd.DataFrame:
    """Apply z-score transformation with fallback handling when statistics are missing.

    This mirrors the behaviour used during backtest-time factor generation so both
    dataset construction and live inference share logging semantics.
    """
    feat_cols = [c for c in df.columns if "_lag_" in c]
    missing_features: Dict[Tuple[str, str], Set[str]] = {}
    skipped_columns: List[str] = []
    
    # 加载白名单
    whitelist = _load_zscore_whitelist()

    for col in feat_cols:
        parts = col.split('_lag_')
        if len(parts) != 2:
            skipped_columns.append(col)
            continue

        base_name = parts[0]
        factor_name = base_name
        window = None
        if '_w' in base_name:
            base_parts = base_name.rsplit('_w', 1)
            if len(base_parts) == 2:
                factor_name = base_parts[0]
                try:
                    window = int(base_parts[1])
                except ValueError:
                    window = None

        stat_key = None
        if window is not None:
            if (factor_name, window) in stats.index:
                stat_key = (factor_name, window)
            elif (base_name, window) in stats.index:
                stat_key = (base_name, window)

        if stat_key is None:
            available_windows = [
                key
                for key in stats.index
                if isinstance(key, tuple) and key[0] in (factor_name, base_name)
            ]
            if available_windows:
                stat_key = available_windows[0]
            else:
                # 检查是否在白名单中
                if factor_name in whitelist or base_name in whitelist:
                    logger.debug(f"因子 {factor_name}/{base_name} 在白名单中，跳过 zscore 处理")
                    continue
                window_label = str(window) if window is not None else 'any'
                missing_features.setdefault((factor_name, base_name), set()).add(window_label)
                skipped_columns.append(col)
                continue

        try:
            stat_row = stats.loc[stat_key]
        except (KeyError, IndexError):
            # 检查是否在白名单中
            if factor_name in whitelist or base_name in whitelist:
                logger.debug(f"因子 {factor_name}/{base_name} 在白名单中，跳过 zscore 处理")
                continue
            window_label = str(window) if window is not None else 'any'
            missing_features.setdefault((factor_name, base_name), set()).add(window_label)
            skipped_columns.append(col)
            continue

        mu = float(stat_row['mean'])
        sd = float(stat_row['std']) + 1e-12
        values = df[col].values.astype(float)
        values = (values - mu) / sd

        if clip and {'lower', 'upper'}.issubset(stats.columns):
            lo = float(stat_row['lower'])
            hi = float(stat_row['upper'])
            lower_bound = (lo - mu) / sd
            upper_bound = (hi - mu) / sd
            values = np.clip(values, lower_bound, upper_bound)

        df[col] = values.astype('float32')

    if missing_features:
        # 过滤掉白名单中的因子，只报告真正缺失统计的因子
        non_whitelist_missing = {}
        for (fact_name, base_name), windows in missing_features.items():
            if fact_name not in whitelist and base_name not in whitelist:
                non_whitelist_missing[(fact_name, base_name)] = windows
        
        if non_whitelist_missing:
            preview = []
            for (fact_name, base_name), windows in list(non_whitelist_missing.items())[:5]:
                window_repr = '[' + ','.join(sorted(windows)) + ']'
                preview.append(f"{fact_name} (base={base_name}, windows={window_repr})")
            if len(non_whitelist_missing) > 5:
                preview.append('...')
            logger.warning(
                "No statistics for %d feature/window groups; skipped zscore on %d lag columns. Examples: %s",
                len(non_whitelist_missing),
                len(skipped_columns),
                '; '.join(preview),
            )
        else:
            logger.debug(f"所有缺失统计的因子都在白名单中，已跳过 zscore 处理")

    return df


