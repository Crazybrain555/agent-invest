# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from typing import Dict, List, Optional, Sequence, Tuple
import pandas as pd

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.data_service.preprocessing.methods.preprocess_factors import FactorPreprocessor

from .stats_zscore import _load_stats_with_window


def _standardize_long_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Normalize column names to required set
    rename_map = {}
    if 'field_name' in df.columns and 'factor_name' not in df.columns:
        rename_map['field_name'] = 'factor_name'
    if 'value' in df.columns and 'factor_value' not in df.columns:
        rename_map['value'] = 'factor_value'
    if rename_map:
        df = df.rename(columns=rename_map)
    # Ensure essential columns
    for c in ['trade_date', 'stock_code', 'factor_name', 'factor_value']:
        if c not in df.columns:
            raise ValueError(f"long-stage input missing required column: {c}")
    # Normalize types
    df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y%m%d')
    df['stock_code'] = df['stock_code'].astype(str)
    return df



# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.data_service.preprocessing.methods.preprocess_factors import FactorPreprocessor

from .fetch_multi import _resolve_feature_sources
from .skeleton import _create_complete_factor_skeleton
from .stats_zscore import _load_stats_with_window
from src.utils.config_loader import ConfigLoader


def _load_zscore_whitelist():
    """加载 zscore 白名单配置"""
    try:
        config_loader = ConfigLoader(config_dir='configs')
        whitelist_config = config_loader.load_config('dataset/zscore_whitelist.yaml')
        
        if not whitelist_config.get('rules', {}).get('enabled', True):
            return set()
            
        whitelist = set(whitelist_config.get('whitelist_factors', []))
        if whitelist:
            logger.info(f"加载 zscore 白名单：{len(whitelist)} 个因子")
        return whitelist
    except Exception as e:
        logger.warning(f"加载 zscore 白名单配置失败: {str(e)}，使用空白名单")
        return set()



def _fetch_features_long_multi(
    prov: LocalTestDBDataProvider,
    s: str,
    e: str,
    factor_windows: Dict[str, List[int]],
    features_tables: List[str],
    probe_days: int = 20,
    factor_source_map: Optional[Dict[Tuple[str, int], Dict[str, str]]] = None,
) -> pd.DataFrame:
    """
    多表取数 + 统一列名 -> 合并成长表（仅做轻量处理），不做 pivot/lag。
    输出列：trade_date, stock_code, factor_name, factor_value, z_windows。

    Args:
        prov: 数据提供器
        s: 开始日期 (YYYYMMDD)
        e: 结束日期 (YYYYMMDD)
        factor_windows: 因子窗口配置
        features_tables: 用于取数的特征表列表
        probe_days: 利用最近多少个交易日抽样探测因子路由（>=0 表示启用窗口）
    """
    if factor_source_map is None:
        mapping = _resolve_feature_sources(
            features_tables,
            factor_windows,
            prov,
            probe_days=probe_days,
        )
    else:
        mapping = {
            (fname, win): factor_source_map[(fname, win)]
            for fname, wins in factor_windows.items()
            for win in wins
            if (fname, win) in factor_source_map
        }
    if not mapping:
        return pd.DataFrame()

    # 聚合 (table, factor_col, win_col) 组，减少往返调用
    tbl_groups = {}
    for (fname, win), meta in mapping.items():
        t = meta['table']
        fc = meta['factor_col']
        wc = meta['win_col']
        key = (t, fc, wc)
        if key not in tbl_groups:
            tbl_groups[key] = {'factors': set(), 'wins': set()}
        tbl_groups[key]['factors'].add(fname)
        if wc:
            tbl_groups[key]['wins'].add(win)

    parts = []
    for (table, factor_col, win_col), g in tbl_groups.items():
        filters = {factor_col: list(g['factors'])}
        if win_col:
            filters[win_col] = list(g['wins'])
        dfp = prov.fetch_data(
            table=table, start_date=s, end_date=e,
            format="long", column_filters=filters
        )
        if dfp.empty:
            continue
        ren = {}
        if factor_col != 'factor_name':
            ren[factor_col] = 'factor_name'
        if 'field_name' in dfp.columns and 'factor_name' not in ren.values():
            ren['field_name'] = 'factor_name'
        if 'value' in dfp.columns and 'factor_value' not in dfp.columns:
            ren['value'] = 'factor_value'
        dfp = dfp.rename(columns=ren)
        if win_col and win_col in dfp.columns:
            if win_col != 'z_windows':
                dfp = dfp.rename(columns={win_col: 'z_windows'})
        else:
            dfp['z_windows'] = 0
        dfp['trade_date'] = pd.to_datetime(dfp['trade_date']).dt.strftime('%Y%m%d')
        dfp['stock_code'] = dfp['stock_code'].astype(str)
        dfp = dfp[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']]
        parts.append(dfp)

    if not parts:
        return pd.DataFrame()
    features_long = pd.concat(parts, ignore_index=True)
    return features_long


def build_long_preprocessed_with_zscore(
    prov: LocalTestDBDataProvider,
    s: str,
    e: str,
    factor_windows: Dict[str, List[int]],
    features_tables: List[str],
    factor_based_nan_handling: bool = True,
    consecutive_nan_threshold: Optional[int] = None,
    stats_table: Optional[str] = None,
    clip_std: bool = True,
    strict_zscore: bool = True,
    drop_uncovered: bool = False,
    feature_probe_days: int = 20,
    factor_source_map: Optional[Dict[Tuple[str, int], Dict[str, str]]] = None,
) -> pd.DataFrame:
    """
    构造完整长表骨架 -> 长表预处理(缺失/前缀匹配策略) -> 在长表阶段完成 zscore。
    返回列: trade_date, stock_code, factor_name(with _w{win}), z_windows, factor_value
    """
    raw_long = _fetch_features_long_multi(
        prov,
        s,
        e,
        factor_windows,
        features_tables,
        probe_days=feature_probe_days,
        factor_source_map=factor_source_map,
    )
    if raw_long.empty:
        logger.debug(f"长表阶段：区间 {s}-{e} 没有取到任何特征数据")
        return pd.DataFrame()

    date_range = sorted(raw_long['trade_date'].unique())
    stock_list = sorted(raw_long['stock_code'].unique())
    skel = _create_complete_factor_skeleton(date_range, stock_list, factor_windows, raw_long)
    if skel.empty:
        return pd.DataFrame()

    if factor_based_nan_handling:
        pre = FactorPreprocessor()
        prep = pre.preprocess_factors_long(
            skel, factor_windows, consecutive_nan_threshold=consecutive_nan_threshold
        )
        # 确保因子名带窗口后缀
        fn_series = prep['factor_name'].astype(str)
        has_suffix = fn_series.str.contains(r"_w\d+$", regex=True)
        if not has_suffix.all():
            prep['factor_name'] = fn_series + '_w' + prep['z_windows'].astype(int).astype(str)
    else:
        prep = skel.copy()
        prep['factor_name'] = prep['factor_name'] + '_w' + prep['z_windows'].astype(int).astype(str)

    if stats_table:
        stats = _load_stats_with_window(prov, stats_table, clip_std)
        stats = stats.reset_index().rename(columns={'feature_name': 'factor_base', 'window': 'z_windows'})
        # 兼容旧版 pandas：不用 regex 参数，且更鲁棒的切分
        tmp = prep['factor_name'].astype(str).str.rpartition('_w')
        # tmp 返回三列: [left, sep, right]；sep 等于 '_w' 时表示成功切分
        prep['factor_base'] = np.where(tmp[1].eq('_w'), tmp[0], prep['factor_name'])
        # 覆盖率/有效性严格校验（支持白名单）
        whitelist = _load_zscore_whitelist()
        needed_keys = prep[['factor_base', 'z_windows']].drop_duplicates()
        chk = needed_keys.merge(
            stats[['factor_base', 'z_windows', 'std']],
            on=['factor_base', 'z_windows'],
            how='left'
        )
        missing_mask = chk['std'].isna()
        zero_mask = (~missing_mask) & (chk['std'].astype(float) <= 0.0)
        
        # 应用白名单过滤
        if whitelist:
            whitelist_mask = chk['factor_base'].isin(whitelist)
            missing_mask = missing_mask & (~whitelist_mask)
            zero_mask = zero_mask & (~whitelist_mask)
            
            # 记录白名单因子
            whitelisted_missing = chk[chk['factor_base'].isin(whitelist) & chk['std'].isna()]
            if not whitelisted_missing.empty:
                logger.info(f"白名单因子将保持原值：{whitelisted_missing['factor_base'].unique().tolist()}")
        
        n_missing = int(missing_mask.sum())
        n_zero = int(zero_mask.sum())
        if strict_zscore and (n_missing > 0 or n_zero > 0):
            def _top(df, mask, k=10):
                return (df.loc[mask, ['factor_base', 'z_windows']]
                          .head(k)
                          .astype({'z_windows': int})
                          .to_dict('records'))
            top_missing = _top(chk, missing_mask)
            top_zero = _top(chk, zero_mask)
            raise ValueError(
                f"[zscore] 统计检查不通过：缺失={n_missing}, std<=0={n_zero}. "
                f"示例缺失={top_missing}, 示例std<=0={top_zero}. "
                f"请补齐统计或修正统计表。"
            )
        if (not strict_zscore) and (n_missing > 0 or n_zero > 0):
            msg = f"[zscore] 严格模式关闭：缺失={n_missing}, std<=0={n_zero}."
            if drop_uncovered:
                bad = chk[(missing_mask | zero_mask)][['factor_base', 'z_windows']]
                prep = prep.merge(bad.assign(_bad=1), on=['factor_base', 'z_windows'], how='left')
                before = len(prep)
                prep = prep[prep['_bad'].isna()].drop(columns=['_bad'])
                after = len(prep)
                logger.warning(msg + f" 已删除未覆盖行：{before}→{after}")
            else:
                logger.warning(msg + " 将保留原值（跳过 zscore）")

        # 执行 zscore（仅对有统计且 std>0 的行）
        merged = prep.merge(
            stats,
            on=['factor_base', 'z_windows'],
            how='left'
        )
        ok = merged['std'].notna() & (merged['std'].astype(float) > 0.0)
        if ok.any():
            mu = merged.loc[ok, 'mean'].astype(float)
            sd = merged.loc[ok, 'std'].astype(float)
            z = (merged.loc[ok, 'factor_value'].astype(float) - mu) / sd
            if clip_std and {'lower', 'upper'}.issubset(merged.columns):
                lo = ((merged.loc[ok, 'lower'].astype(float) - mu) / sd)
                hi = ((merged.loc[ok, 'upper'].astype(float) - mu) / sd)
                z = z.clip(lower=lo, upper=hi)
            merged.loc[ok, 'factor_value'] = z.astype(np.float32)
        prep = merged[['trade_date', 'stock_code', 'factor_name', 'z_windows', 'factor_value']]

    # 最终强制保证因子名带窗口后缀，确保与 get_all_factor_names() 完全一致
    fn_series = prep['factor_name'].astype(str)
    has_suffix = fn_series.str.contains(r"_w\d+$", regex=True)
    if not has_suffix.all():
        prep['factor_name'] = fn_series + '_w' + prep['z_windows'].astype(int).astype(str)

    # 规范返回列；DuckDB PIVOT 只需要 factor_name/factor_value
    prep = prep[['trade_date', 'stock_code', 'factor_name', 'factor_value']]
    return prep


