# -*- coding: utf-8 -*-
import logging
import copy

logger = logging.getLogger(__name__)

from typing import Dict, List, Optional, Set, Tuple
import pandas as pd

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.data_service.preprocessing.methods.preprocess_factors import FactorPreprocessor

from .calendar_utils import _get_trading_days_before
from .pivoting import pivot_long_to_wide_simple, _complete_date_reindex
from .lag import generate_lag_features_simple
from .stats_zscore import _load_stats_with_window, _apply_zscore_with_window

_FEATURE_SOURCE_CACHE: Dict[
    Tuple[int, Tuple[str, ...], Tuple[Tuple[str, Tuple[int, ...]], ...], int],
    Dict[Tuple[str, int], Dict[str, str]]
] = {}



def _resolve_feature_sources(
    features_tables: List[str],
    factor_windows: Dict[str, List[int]],
    prov: LocalTestDBDataProvider,
    probe_days: int = 20,
) -> Dict[Tuple[str, int], Dict[str, str]]:
    """
    解析多个特征表，确定每个因子应该从哪个表获取数据

    Args:
        features_tables: 特征表列表
        factor_windows: 因子窗口配置
        prov: 数据提供器
        probe_days: 回溯用于抽样映射的交易日数量（>=0 表示启用窗口）

    Returns:
        Dict[Tuple[str, int], Dict[str, str]]: {(factor_name, window): {"table": table_name, ...}}
    """
    logger.info(f"🔍 开始解析 {len(features_tables)} 个特征表的字段分配..")

    normalized_tables = tuple(sorted(features_tables))
    try:
        normalized_factor_windows = tuple(
            sorted(
                (str(name), tuple(sorted(int(win) for win in wins)))
                for name, wins in factor_windows.items()
            )
        )
    except Exception:  # noqa: BLE001
        normalized_factor_windows = tuple(
            sorted(
                (str(name), tuple(sorted(wins)))
                for name, wins in factor_windows.items()
            )
        )
    try:
        normalized_probe_days = max(int(probe_days), 0)
    except (TypeError, ValueError):
        normalized_probe_days = 0

    cache_key = (
        id(prov),
        normalized_tables,
        normalized_factor_windows,
        normalized_probe_days,
    )
    cached = _FEATURE_SOURCE_CACHE.get(cache_key)
    if cached is not None:
        logger.info("🔁 使用缓存的因子路由映射 (probe_days=%s)", normalized_probe_days)
        return copy.deepcopy(cached)

    mapping: Dict[Tuple[str, int], Dict[str, str]] = {}
    duplicates = []

    # 🚀 第一步：获取每个表中实际存在的因子
    table_factors = {}  # {table_name: {factor_name: [windows]}}

    for table_name in features_tables:
        try:
            # 获取表的所有字段列表
            available_cols = prov.list_fields(table_name)
            logger.info(f"  📋 表 {table_name} 包含 {len(available_cols)} 个字段")

            # 自动探测列名
            factor_col = None
            win_col = None

            for possible_name in ['factor_name', 'field_name', 'feature_name']:
                if possible_name in available_cols:
                    factor_col = possible_name
                    break

            for possible_name in ['z_windows', 'z_window', 'window']:
                if possible_name in available_cols:
                    win_col = possible_name
                    break

            if factor_col is None:
                logger.warning(f"  ⚠️ 表 {table_name} 缺少因子名列，跳过")
                continue

            if win_col is None:
                logger.info(f"  ⚠️ 表 {table_name} 没有窗口列，所有因子默认窗口=0")

            logger.info(f"  ✅ 表 {table_name} 列名映射: 因子列={factor_col}, 窗口列={win_col}")

            sample_days = normalized_probe_days
            if sample_days > 0:
                logger.info(
                    "  🔍 抽样查询 %s 中的实际因子（近 %s 个交易日窗口，用于映射探测）..",
                    table_name,
                    sample_days,
                )
            else:
                logger.info(f"  🔍 抽样查询 {table_name} 中的实际因子（不限制日期窗口）..")

            date_filter = ''
            if sample_days > 0:
                recent_date_sql = f"SELECT MAX(trade_date) as max_date FROM {table_name}"
                try:
                    max_date_result = prov._read_sql_with_retry(recent_date_sql)
                except Exception as err:  # noqa: BLE001
                    logger.warning(
                        "  ⚠️ 获取 %s 最大 trade_date 失败：%s，使用 LIMIT 回退",
                        table_name,
                        err,
                    )
                    max_date_result = pd.DataFrame()

                if not max_date_result.empty:
                    max_raw = max_date_result['max_date'].iloc[0]
                else:
                    max_raw = None

                if max_raw is not None and not pd.isna(max_raw):
                    max_date = pd.to_datetime(max_raw)
                    max_date_str = max_date.strftime('%Y%m%d')
                    recent_date = None
                    try:
                        offset = max(sample_days - 1, 0)
                        recent_date = _get_trading_days_before(max_date_str, offset)
                    except Exception as cal_err:  # noqa: BLE001
                        logger.warning(
                            "  ⚠️ 基于交易日的回溯失败：%s，改用自然日回溯",
                            cal_err,
                        )
                        recent_date = None

                    if not recent_date:
                        recent_date = (max_date - pd.Timedelta(days=sample_days)).strftime('%Y%m%d')

                    date_filter = f" WHERE trade_date >= '{recent_date}'"
                    logger.info("    ⏱  使用 trade_date >= %s 作为抽样窗口", recent_date)
                else:
                    logger.warning(f"  ⚠️ 未获取 {table_name} 的最大交易日，使用 LIMIT 回退")

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

            result_df = prov._read_sql_with_retry(query_sql)

            if result_df.empty:
                logger.warning(f"  ⚠️ 表 {table_name} 中没有数据，跳过")
                continue

            table_factors[table_name] = {}
            actual_factors_count = 0

            for _, row in result_df.iterrows():
                factor_name = row[factor_col]
                window = int(row[win_col]) if win_col and win_col in row else 0

                if factor_name not in table_factors[table_name]:
                    table_factors[table_name][factor_name] = []
                table_factors[table_name][factor_name].append(window)
                actual_factors_count += 1

            logger.info(
                f"  📊 表 {table_name} 实际包含 {len(table_factors[table_name])} 个不同因子，"
                f"{actual_factors_count} 个因子窗口组合"
            )

            table_factors[table_name]['_meta'] = {
                'factor_col': factor_col,
                'win_col': win_col
            }

        except Exception as e:  # noqa: BLE001
            logger.error(f"  ❌ 解析表 {table_name} 时出现异常: {str(e)}")
            continue

    logger.info("🔧 创建因子路由映射...")

    for factor_name, windows in factor_windows.items():
        for window in windows:
            key = (factor_name, window)

            candidate_tables = []
            for table_name, factors_info in table_factors.items():
                if factor_name in factors_info and window in factors_info[factor_name]:
                    candidate_tables.append(table_name)

            if not candidate_tables:
                logger.debug(f"  ⚠️ 因子 {factor_name}_w{window} 在任何表中都不存在")
                continue

            if len(candidate_tables) > 1:
                duplicates.append((key, candidate_tables[0], candidate_tables[1:]))
                selected_table = candidate_tables[0]
                logger.warning(
                    f"  🚨 因子 {factor_name}_w{window} 在多个表中存在 {candidate_tables}，选择 {selected_table}"
                )
            else:
                selected_table = candidate_tables[0]

            table_meta = table_factors[selected_table]['_meta']
            mapping[key] = {
                'table': selected_table,
                'factor_col': table_meta['factor_col'],
                'win_col': table_meta['win_col']
            }

    if duplicates:
        logger.warning(f"🚨 发现 {len(duplicates)} 个真正的重复因子:")
        for key, first_table, other_tables in duplicates:
            logger.warning(f"  - 因子 {key[0]}_w{key[1]}: 选择 {first_table}，忽略 {other_tables}")

    logger.info(f"✅ 因子路由映射完成，共创建 {len(mapping)} 个有效映射")

    table_usage = {}
    for _, source_info in mapping.items():
        table = source_info['table']
        table_usage[table] = table_usage.get(table, 0) + 1

    logger.info("📊 各表实际使用统计:")
    for table, count in table_usage.items():
        logger.info(f"  - {table}: {count} 个因子")

    _FEATURE_SOURCE_CACHE[cache_key] = copy.deepcopy(mapping)
    return copy.deepcopy(mapping)


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
    from .skeleton import _create_complete_factor_skeleton  # local import to avoid cycle
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


