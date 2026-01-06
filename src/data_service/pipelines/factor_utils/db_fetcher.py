#!/usr/bin/env python3
"""
数据库补齐工具：从本地/测试数据库按因子与窗口配置获取长表数据，构造完整骨架并透视为宽表，
在不依赖旧版 DFZQ 预处理器的前提下，生成宽表 + 滞后特征（支持分块流式）。

主要功能：
- fetch_wide_lag: 入口函数，返回整表 DataFrame 或 LagChunkStream（长序列自动启用分块）
- LagChunkStream: 以“股票分组”为单位分块，逐块生成 lag 特征、可选 z-score、对齐 schema/特征顺序
- validate_db_connection / get_available_date_range: 简易连通性与表日期范围工具

注意：
- DB 补齐阶段使用与 dataset 构建相同的因子预处理策略（factor_based_nan_handling=True）
- 当序列较长（例如 ≥120）时，优先使用分块流式，显著降低内存峰值
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple, Union
from pathlib import Path
import pandas as pd
import numpy as np

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider

from src.data_service.pipelines.Dataset_builder.calendar_utils import _get_trading_days_before
from src.data_service.pipelines.Dataset_builder.io_tables import _load_restricted_set
from src.data_service.pipelines.Dataset_builder.fetch_multi import _resolve_feature_sources
from src.data_service.pipelines.Dataset_builder.skeleton import _create_complete_factor_skeleton
from src.data_service.pipelines.Dataset_builder.pivoting import pivot_long_to_wide_simple, _complete_date_reindex
from src.data_service.pipelines.Dataset_builder.lag import generate_lag_features_simple
from src.data_service.pipelines.Dataset_builder.stats_zscore import _load_stats_with_window, _apply_zscore_with_window
from src.data_service.pipelines.DFZQ_GRU_PV_pipline.factor_windows import FACTOR_WINDOWS

from .align import align_wide_to_schema
from .config_utils import align_df_to_factor_order

__all__ = ["fetch_wide_lag", "LagChunkStream", "validate_db_connection", "get_available_date_range"]


def _apply_stock_filters(df: pd.DataFrame, code_prefix_blacklist: Optional[List[str]] = None, code_blacklist: Optional[List[str]] = None) -> pd.DataFrame:
    """应用股票代码过滤，剔除黑名单股票。
    
    Args:
        df: 包含 stock_code 列的 DataFrame
        code_prefix_blacklist: 股票代码前缀黑名单（如 ["9"] 过滤 B 股）
        code_blacklist: 完整股票代码黑名单
        
    Returns:
        过滤后的 DataFrame
    """
    if df.empty or 'stock_code' not in df.columns:
        return df
    
    initial_count = len(df)
    
    # 前缀黑名单过滤
    if code_prefix_blacklist:
        for prefix in code_prefix_blacklist:
            df = df[~df['stock_code'].str.startswith(str(prefix))]
    
    # 完整代码黑名单过滤
    if code_blacklist:
        df = df[~df['stock_code'].isin(code_blacklist)]
    
    final_count = len(df)
    if initial_count > final_count:
        print(f"   股票过滤: {initial_count} → {final_count} 行")
    
    return df


class LagChunkStream:
    """按“股票分组”分块输出已对齐的“宽表+滞后特征”DataFrame。

    每块处理流程：生成 lag → 目标日期截断 → 受限池过滤 → 可选 z-score → schema/特征顺序对齐。
    适用于长序列（如 T 很大）以显著降低内存峰值。
    """

    def __init__(
        self,
        features_wide: pd.DataFrame,
        factor_names: List[str],
        seq_len: int,
        target_start: str,
        target_end: str,
        *,
        stats_df: Optional[pd.DataFrame] = None,
        clip_std: bool = True,
        factor_windows: Optional[Dict[str, List[int]]] = None,
        align_to_schema_path: Optional[str] = None,
        dataset_feature_order: Optional[List[str]] = None,
        selected_factors: Optional[List[str]] = None,
        chunk_size: int = 25,
        restricted_set: Optional[Set[Tuple[str, str]]] = None,
        code_prefix_blacklist: Optional[List[str]] = None,
        code_blacklist: Optional[List[str]] = None,
    ) -> None:
        self.features_wide = features_wide
        self.factor_names = factor_names
        self.seq_len = int(seq_len)
        self.target_start = pd.to_datetime(target_start)
        self.target_end = pd.to_datetime(target_end)
        self.stats_df = stats_df
        self.clip_std = clip_std
        self.factor_windows = factor_windows or {}
        self.align_to_schema_path = align_to_schema_path
        self.dataset_feature_order = dataset_feature_order
        self.selected_factors = selected_factors
        self.chunk_size = max(1, int(chunk_size))
        self.reference_order = dataset_feature_order or selected_factors or factor_names
        self.restricted_set = set()
        if restricted_set:
            self.restricted_set = {(str(d), str(c)) for d, c in restricted_set}
        self.code_prefix_blacklist = code_prefix_blacklist
        self.code_blacklist = code_blacklist

    def __iter__(self):
        return self.iter_chunks()

    def iter_chunks(self):
        if self.features_wide is None or self.features_wide.empty:
            return

        chunk_frames: List[pd.DataFrame] = []
        grouped = self.features_wide.groupby('stock_code', sort=False)
        for _, group in grouped:
            chunk_frames.append(group)
            if len(chunk_frames) >= self.chunk_size:
                prepared = self._prepare_chunk(chunk_frames)
                if prepared is not None and not prepared.empty:
                    yield prepared
                chunk_frames = []

        if chunk_frames:
            prepared = self._prepare_chunk(chunk_frames)
            if prepared is not None and not prepared.empty:
                yield prepared

        self.features_wide = None

    def _prepare_chunk(self, frames: List[pd.DataFrame]) -> Optional[pd.DataFrame]:
        chunk_wide = pd.concat(frames, ignore_index=True)
        if chunk_wide.empty:
            return None

        chunk_lag = generate_lag_features_simple(chunk_wide, self.factor_names, self.seq_len)
        if chunk_lag.empty:
            return None

        chunk_lag['trade_date'] = pd.to_datetime(chunk_lag['trade_date'], errors='coerce')
        mask = (chunk_lag['trade_date'] >= self.target_start) & (chunk_lag['trade_date'] <= self.target_end)
        chunk_lag = chunk_lag.loc[mask].copy()
        if chunk_lag.empty:
            return None

        chunk_lag['trade_date'] = chunk_lag['trade_date'].dt.strftime('%Y%m%d')
        chunk_lag['stock_code'] = chunk_lag['stock_code'].astype(str)

        if self.restricted_set:
            mi = pd.MultiIndex.from_arrays([chunk_lag['trade_date'], chunk_lag['stock_code']])
            chunk_lag = chunk_lag.loc[~mi.isin(self.restricted_set)]
            if chunk_lag.empty:
                return None

        # 应用股票代码过滤
        chunk_lag = _apply_stock_filters(chunk_lag, self.code_prefix_blacklist, self.code_blacklist)
        if chunk_lag is None or chunk_lag.empty:
            return None

        if self.stats_df is not None:
            chunk_lag = _apply_zscore_with_window(chunk_lag, self.stats_df, self.clip_std, self.factor_windows)

        if self.align_to_schema_path:
            try:
                chunk_lag = align_wide_to_schema(
                    chunk_lag,
                    self.align_to_schema_path,
                    fallback_order=self.reference_order,
                    seq_len=self.seq_len,
                )
            except Exception as ex:
                print(f"   警告：分块的 schema 对齐失败，已跳过此步骤。错误: {ex}")

        reference_order = self.dataset_feature_order or self.selected_factors or self.reference_order
        if reference_order:
            chunk_lag = align_df_to_factor_order(chunk_lag, reference_order, self.seq_len)

        chunk_lag = chunk_lag.sort_values(['trade_date', 'stock_code']).reset_index(drop=True)
        return chunk_lag


def _build_windows_map(selected_factors: Optional[List[str]], windows_override: Optional[Dict[str, List[int]]]) -> Dict[str, List[int]]:
    """根据 selected_factors 的后缀 *_w{window} 与默认 FACTOR_WINDOWS 合并得到 {因子: [窗口]} 映射。

    优先级：显式 windows_override > 从 selected_factors 解析的窗口 > FACTOR_WINDOWS 默认窗口。
    """
    if windows_override:
        return {k: sorted({int(v) for v in vals}) for k, vals in windows_override.items()}

    windows_map: Dict[str, Set[int]] = {}
    if selected_factors:
        for factor in selected_factors:
            if '_w' not in factor:
                continue
            base, win = factor.rsplit('_w', 1)
            try:
                win_int = int(win)
            except ValueError:
                continue
            windows_map.setdefault(base, set()).add(win_int)
    for base, defaults in FACTOR_WINDOWS.items():
        windows_map.setdefault(base, set(defaults))
    return {k: sorted(v) for k, v in windows_map.items()}


def _fetch_features_long(
    prov: LocalTestDBDataProvider,
    start: str,
    end: str,
    seq_len: int,
    routing: Dict[Tuple[str, int], Dict[str, str]],
) -> pd.DataFrame:
    """按路由（因子、窗口 → 表、列）批量获取“长表”因子数据，并统一列名/类型。

    - 重命名为标准列：factor_col → factor_name，value → factor_value，win_col → z_windows（缺省置 0）
    - 统一格式：['trade_date','stock_code','factor_name','factor_value','z_windows']
    - 日期统一为 'YYYYMMDD' 字符串，股票代码统一为字符串
    """
    fetch_start = _get_trading_days_before(start, seq_len - 1)

    table_groups: Dict[str, Dict[str, Union[str, List[str]]]] = {}
    for (factor_name, window), source in routing.items():
        table = source['table']
        entry = table_groups.setdefault(
            table,
            {'factors': [], 'windows': [], 'factor_col': source['factor_col'], 'win_col': source['win_col']},
        )
        entry['factors'].append(factor_name)
        entry['windows'].append(window)

    long_parts: List[pd.DataFrame] = []

    for table_name, info in table_groups.items():
        factor_col = info['factor_col']
        win_col = info['win_col']
        unique_factors = sorted(set(info['factors']))
        filters = {factor_col: unique_factors}
        if win_col:
            unique_windows = sorted({w for w in info['windows'] if w is not None})
            if unique_windows:
                filters[win_col] = unique_windows

        df_part = prov.fetch_data(
            table=table_name,
            start_date=fetch_start,
            end_date=end,
            format="long",
            column_filters=filters,
        )

        if df_part.empty:
            continue

        rename_map = {}
        if factor_col != 'factor_name':
            rename_map[factor_col] = 'factor_name'
        if 'value' in df_part.columns and 'factor_value' not in df_part.columns:
            rename_map['value'] = 'factor_value'
        if 'field_name' in df_part.columns and 'factor_name' not in rename_map.values():
            rename_map['field_name'] = 'factor_name'
        if rename_map:
            df_part = df_part.rename(columns=rename_map)

        if win_col and win_col in df_part.columns:
            if win_col != 'z_windows':
                df_part = df_part.rename(columns={win_col: 'z_windows'})
        else:
            df_part['z_windows'] = 0

        df_part = df_part[['trade_date', 'stock_code', 'factor_name', 'factor_value', 'z_windows']].copy()
        df_part['trade_date'] = pd.to_datetime(df_part['trade_date']).dt.strftime('%Y%m%d')
        df_part['stock_code'] = df_part['stock_code'].astype(str)
        df_part['factor_name'] = df_part['factor_name'].astype(str)
        df_part['z_windows'] = df_part['z_windows'].astype(int)
        long_parts.append(df_part)

    if not long_parts:
        return pd.DataFrame()

    return pd.concat(long_parts, ignore_index=True)


def _build_wide_frame(
    features_long: pd.DataFrame,
    windows_map: Dict[str, List[int]],
    seq_len: int,
    start: str,
    end: str,
    factor_based_nan_handling: bool,
    consecutive_nan_threshold: Optional[int],
    max_factors_per_batch: Optional[int] = None,
) -> Tuple[pd.DataFrame, List[str]]:
    """将标准长表构造成完整骨架 → 因子预处理 → 透视为宽表 → 完整日期重建。

    说明：
    - DB 补齐阶段使用与 dataset 构建相同的因子预处理策略
    - 透视到宽表后，对日期区间做完整 reindex，保证日期连续性
    - 最终按 ['stock_code','trade_date'] 排序
    返回：宽表 DataFrame 与特征名列表（含窗口后缀）
    """
    if features_long.empty:
        return pd.DataFrame(), []

    date_range = sorted(features_long['trade_date'].unique())
    stock_list = sorted(features_long['stock_code'].unique())

    features_df = _create_complete_factor_skeleton(date_range, stock_list, windows_map, features_long)
    if features_df.empty:
        return pd.DataFrame(), []

    if factor_based_nan_handling:
        from src.data_service.preprocessing.methods.preprocess_factors import FactorPreprocessor

        preprocessor = FactorPreprocessor()
        features_df = preprocessor.preprocess_factors_long(features_df, windows_map, consecutive_nan_threshold)
    else:
        features_df['factor_name'] = (
            features_df['factor_name'] + '_w' + features_df['z_windows'].astype(int).astype(str)
        )

    factor_names = features_df['factor_name'].unique().tolist()

    # 批量透视，降低内存峰值
    batch_n = None
    try:
        if max_factors_per_batch is not None:
            batch_n = int(max_factors_per_batch)
    except Exception:
        batch_n = None

    if batch_n and batch_n > 0 and len(factor_names) > batch_n:
        print(f"   透视宽表采用分批模式，每批 {batch_n} 个因子，共 { (len(factor_names)+batch_n-1)//batch_n } 批")
        # 基础索引（避免每批重复生成完整索引）
        base = features_df[['trade_date','stock_code']].drop_duplicates().copy()
        features_wide = base
        for i in range(0, len(factor_names), batch_n):
            sub_names = factor_names[i:i+batch_n]
            sub_wide = pivot_long_to_wide_simple(
                features_df,
                sub_names,
                factor_name_col='factor_name',
                value_col='factor_value',
                lag_filter=0,
            )
            if sub_wide is None or sub_wide.empty:
                continue
            features_wide = features_wide.merge(sub_wide, on=['trade_date','stock_code'], how='left')
    else:
        features_wide = pivot_long_to_wide_simple(
            features_df,
            factor_names,
            factor_name_col='factor_name',
            value_col='factor_value',
            lag_filter=0,
        )

    if features_wide.empty:
        return pd.DataFrame(), []

    features_wide = _complete_date_reindex(features_wide, start, end)
    features_wide['trade_date'] = pd.to_datetime(features_wide['trade_date'], errors='coerce')
    features_wide['stock_code'] = features_wide['stock_code'].astype(str)
    features_wide = features_wide.sort_values(['stock_code', 'trade_date']).reset_index(drop=True)

    return features_wide, factor_names


def fetch_wide_lag(
    start: str,
    end: str,
    selected_factors: List[str],
    windows: Optional[Dict[str, List[int]]] = None,
    cfg=None,
    align_to_schema: Optional[str] = None,
    dataset_path_for_order: Optional[str] = None,
    dataset_feature_order: Optional[List[str]] = None,
    chunk_size: int = 25,
    code_prefix_blacklist: Optional[List[str]] = None,
    code_blacklist: Optional[List[str]] = None,
) -> Union[pd.DataFrame, LagChunkStream]:
    print(f"DB补齐器：获取 {start} - {end} 的因子数据...")

    prov = LocalTestDBDataProvider()

    seq_len = int(getattr(cfg, 'seq_len', 30)) if cfg is not None else 30
    clip_std = bool(getattr(cfg, 'clip_std', True)) if cfg is not None else True
    stats_table = getattr(cfg, 'stats_table', None) if cfg is not None else None
    
    # 从 cfg 读取黑名单配置（如果调用方未指定）
    if code_prefix_blacklist is None and cfg is not None:
        code_prefix_blacklist = getattr(cfg, 'code_prefix_blacklist', ['9'])
    elif code_prefix_blacklist is None:
        code_prefix_blacklist = ['9']  # 默认过滤 9 开头
        
    if code_blacklist is None and cfg is not None:
        code_blacklist = getattr(cfg, 'code_blacklist', [])
    elif code_blacklist is None:
        code_blacklist = []
    factor_based_nan_handling = bool(getattr(cfg, 'factor_based_nan_handling', True)) if cfg is not None else True
    if factor_based_nan_handling:
        print("   启用因子配置驱动的 NaN 处理策略（与 dataset 构建保持一致）")
    else:
        print("   使用简单的窗口后缀重命名策略")
    consecutive_nan_threshold = getattr(cfg, 'consecutive_nan_threshold', None) if cfg is not None else None
    chunk_stock_size = getattr(cfg, 'db_chunk_stock_size', chunk_size) if cfg is not None else chunk_size

    windows_map = _build_windows_map(selected_factors, windows)

    feature_tables = getattr(cfg, 'features_tables', [
        "ai_is.inter_train_factors_mkt_processed_v3",
        "ai_is.quantitative_other_signals",
    ]) if cfg is not None else [
        "ai_is.inter_train_factors_mkt_processed_v3",
        "ai_is.quantitative_other_signals",
    ]
    print(f"   使用特征表: {feature_tables}")

    routing = _resolve_feature_sources(feature_tables, windows_map, prov)
    print(f"   已解析的因子-表路由数量: {len(routing)}")

    align_to_dataset = getattr(cfg, 'align_to_dataset', True) if cfg is not None else True
    restricted_set: Set[Tuple[str, str]] = set()
    if align_to_dataset:
        restricted_table = (
            getattr(cfg, 'restricted_table', 'ai_is.forbid_pool_comprehensive')
            if cfg is not None else 'ai_is.forbid_pool_comprehensive'
        )
        if restricted_table:
            restricted_set = _load_restricted_set(prov, start, end, restricted_table)
        else:
            print("   未提供限制股票池表，跳过禁买池对齐逻辑")

    features_long = _fetch_features_long(prov, start, end, seq_len, routing)
    if features_long.empty:
        raise RuntimeError(f"DB补齐 {start}-{end} 未获取到有效因子数据")

    print(f"   长表数据行数: {len(features_long)}")

    features_wide, factor_names = _build_wide_frame(
        features_long,
        windows_map,
        seq_len,
        start,
        end,
        factor_based_nan_handling,
        consecutive_nan_threshold,
        getattr(cfg, 'max_factors_per_batch', 16) if cfg is not None else 16,
    )
    if features_wide.empty:
        raise RuntimeError("DB补齐：宽表为空")

    print(f"   生成宽表：{features_wide.shape[0]} 行 x {features_wide.shape[1]} 列")

    stats_df = None
    if stats_table:
        try:
            stats_df = _load_stats_with_window(prov, stats_table, clip_std)
        except Exception as ex:
            print(f"   z-score 统计表加载失败，跳过标准化: {ex}")

    if seq_len > 120:
        stream = LagChunkStream(
            features_wide=features_wide,
            factor_names=factor_names,
            seq_len=seq_len,
            target_start=start,
            target_end=end,
            stats_df=stats_df,
            clip_std=clip_std,
            factor_windows=windows_map,
            align_to_schema_path=align_to_schema,
            dataset_feature_order=dataset_feature_order,
            selected_factors=selected_factors,
            chunk_size=chunk_stock_size,
            restricted_set=restricted_set,
            code_prefix_blacklist=code_prefix_blacklist,
            code_blacklist=code_blacklist,
        )
        print(f"   已启用分块 lag 生成（每块约 {stream.chunk_size} 只股票）")
        return stream

    lagged = generate_lag_features_simple(features_wide, factor_names, lag=seq_len)
    if lagged.empty:
        raise RuntimeError("DB补齐：滞后特征生成失败")

    mask_target = (lagged['trade_date'] >= start) & (lagged['trade_date'] <= end)
    lagged = lagged.loc[mask_target].sort_values(['trade_date', 'stock_code']).reset_index(drop=True)

    if restricted_set:
        mi = pd.MultiIndex.from_arrays([lagged['trade_date'], lagged['stock_code']])
        lagged = lagged.loc[~mi.isin(restricted_set)]

    # 应用股票代码过滤
    lagged = _apply_stock_filters(lagged, code_prefix_blacklist, code_blacklist)
    if lagged is None or lagged.empty:
        return pd.DataFrame()

    if stats_df is not None:
        lagged = _apply_zscore_with_window(lagged, stats_df, clip_std, windows_map)

    if align_to_schema:
        fallback_order = dataset_feature_order or selected_factors or factor_names
        seq_len_hint = None
        if cfg is not None and hasattr(cfg, 'seq_len'):
            seq_len_hint = getattr(cfg, 'seq_len')
        lagged = align_wide_to_schema(
            lagged,
            align_to_schema,
            fallback_order=fallback_order,
            seq_len=seq_len_hint,
        )

    reference_order = dataset_feature_order or selected_factors
    if reference_order:
        lagged = align_df_to_factor_order(lagged, reference_order, seq_len)

    return lagged


def validate_db_connection() -> bool:
    """快速检查数据库连接是否可用（返回 True/False）。"""
    try:
        prov = LocalTestDBDataProvider()
        prov.fetch_data("SELECT 1 AS test")
        return True
    except Exception as e:
        print(f"数据库连接检查失败: {str(e)}")
        return False


def get_available_date_range(table_name: str) -> Tuple[str, str]:
    """返回指定表的最小与最大交易日期，格式为 (YYYYMMDD, YYYYMMDD)。"""
    prov = LocalTestDBDataProvider()
    query = f"""
        SELECT
            MIN(trade_date) AS min_date,
            MAX(trade_date) AS max_date
        FROM {table_name}
        WHERE trade_date IS NOT NULL
    """
    result = prov._read_sql_with_retry(query)
    if result.empty:
        raise ValueError(f"表 {table_name} 无有效 trade_date 数据")
    min_date = pd.to_datetime(result['min_date'].iloc[0]).strftime('%Y%m%d')
    max_date = pd.to_datetime(result['max_date'].iloc[0]).strftime('%Y%m%d')
    return min_date, max_date
