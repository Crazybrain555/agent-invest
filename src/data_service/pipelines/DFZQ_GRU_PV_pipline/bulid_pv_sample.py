'''
Streaming builder for the Price‑Volume dataset (pv_v1).
Processes data month‑by‑month to keep <2 GB RAM, writes shards on the fly,
creates splits.parquet (optional) and shows a real‑time progress bar.

Usage (CLI):
    python -m src.data_service.pipelines.build_pv_dataset_streaming \
        --out data/Dataset/pv_v1 --start 20030101 --end 20131231 \
        --chunk M --lag 30 --label tc_t10_n30_adj \
        --splits train:20030101:20091231 valid:20100101:20131231
'''
from __future__ import annotations

import gc
import json
import logging
import sys  # 添加sys模块导入
from pathlib import Path
from typing import Sequence, Tuple, List, Set

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from tqdm import tqdm

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.utils.config_loader import ConfigLoader

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────
#                            Helpers
# ────────────────────────────────────────────────────────────────

def _ensure_dirs(out_dir: Path):
    meta = out_dir / "meta"
    shards = out_dir / "shards"
    meta.mkdir(parents=True, exist_ok=True)
    shards.mkdir(parents=True, exist_ok=True)
    return meta, shards, meta / "stats.parquet"


def _iter_ranges(start: str, end: str, freq: str = "M"):
    """
    生成 (起始日, 结束日) 序列。
    - 当 freq == "M" 时使用 MonthStart / MonthEnd，确保区间为 0101‒0131、0201‒0228…；
    - 其它 freq （例如 "Y"）保持原逻辑。
    """
    if freq == "M":
        idx = pd.date_range(start, end, freq="MS")  # Month **S**tart
        for d0 in idx:
            d1 = (d0 + pd.offsets.MonthEnd(0)).strftime("%Y%m%d")  # 当月最后一天
            yield d0.strftime("%Y%m%d"), d1
    else:
        idx = pd.date_range(start, end, freq=freq)
        for d0, d1 in zip(idx, idx[1:].append(pd.DatetimeIndex([end]))):
            yield d0.strftime("%Y%m%d"), d1.strftime("%Y%m%d")


def _load_stats(prov: LocalTestDBDataProvider, table: str, clip: bool):
    stats = prov.fetch_data(table=table).set_index("feature_name")
    cols = ["mean", "std", "lower", "upper"] if clip else ["mean", "std"]
    return stats[cols]


def _load_restricted_set(prov: LocalTestDBDataProvider, start: str, end: str, table: str):
    df = prov.fetch_data(table=table, start_date=start, end_date=end,
                         fields=["trade_date", "stock_code", "signal"])
    return set(zip(df[df.signal == 1].trade_date.astype(str),
                   df[df.signal == 1].stock_code.astype(str)))


def _apply_zscore(df: pd.DataFrame, stats, clip: bool):
    feat_cols = [c for c in df.columns if "_lag_" in c]
    mu = stats.loc[feat_cols, "mean"].values.astype(float)
    sd = stats.loc[feat_cols, "std"].values.astype(float) + 1e-12
    arr = df[feat_cols].values.astype(float)
    arr = (arr - mu) / sd
    
    # 只在 clip=True 时才获取并应用下界/上界
    if clip and "lower" in stats.columns and "upper" in stats.columns:
        lo = stats.loc[feat_cols, "lower"].values.astype(float)
        hi = stats.loc[feat_cols, "upper"].values.astype(float)
        lower = (lo - mu) / sd
        upper = (hi - mu) / sd
        np.clip(arr, lower, upper, out=arr)
    
    df[feat_cols] = arr.astype("float32")
    return df


def _fetch_join_filter_chunk(prov: LocalTestDBDataProvider, s: str, e: str, lag: int,
                              label: str, x_table: str, y_table: str,
                              restricted: Set[tuple[str, str]]):
    """
    Fetch data from features and labels table, join them, and filter out restricted stocks.
    Uses the correct format parameters for wide and long tables.
    """
    # Get features data (wide format)
    base_cols = ["adj_open", "adj_high", "adj_low", "adj_close", "vwap", "amount", "turnover_rate"]
    feature_cols = [f"{c}_lag_{i}" for c in base_cols for i in range(lag)]
    
    logger.info(f"Fetching features from {x_table} ({s} to {e})...")
    features_df = prov.fetch_data(
        table=x_table,
        start_date=s,
        end_date=e,
        fields=feature_cols,
        format="wide"  # Specify wide format for features table
    )
    
    if features_df.empty:
        logger.warning(f"No feature data found in {x_table} for date range {s} to {e}")
        return pd.DataFrame()
    
    # Reset index to get trade_date and stock_code as columns
    features_df = features_df.reset_index()
    
    # Convert column types for consistency
    features_df['trade_date'] = features_df['trade_date'].astype(str)
    features_df['stock_code'] = features_df['stock_code'].astype(str)
    
    # Get labels data (long format)
    logger.info(f"Fetching labels from {y_table} ({s} to {e})...")
    labels_df = prov.fetch_data(
        table=y_table,
        start_date=s,
        end_date=e,
        fields=[label],  # Specify the label field
        format="long"    # Specify long format for labels table
    )
    
    if labels_df.empty:
        logger.warning(f"No label data found in {y_table} for date range {s} to {e}")
        return pd.DataFrame()
    
    # Convert column types for consistency
    labels_df['trade_date'] = labels_df['trade_date'].astype(str)
    labels_df['stock_code'] = labels_df['stock_code'].astype(str)
    
    # Filter labels by field_name
    labels_df = labels_df[labels_df['field_name'] == label].copy()
    if labels_df.empty:
        logger.warning(f"No data found for label {label} in date range {s} to {e}")
        return pd.DataFrame()
    
    # Rename the value column to the label name
    labels_df = labels_df.rename(columns={'value': label})
    labels_df = labels_df.drop(columns=['field_name'])
    
    # Join features and labels
    logger.info("Joining features and labels...")
    df = pd.merge(
        features_df, 
        labels_df,
        on=['trade_date', 'stock_code'],
        how='inner'
    )
    
    if df.empty:
        logger.warning(f"No data after joining features and labels for date range {s} to {e}")
        return df
    
    # Filter out restricted stocks
    if restricted:
        mask = ~pd.MultiIndex.from_arrays([df.trade_date, df.stock_code]).isin(restricted)
        df = df[mask]
        
    logger.info(f"Final data shape: {df.shape}")
    return df


def _write_chunk(df: pd.DataFrame, shard_dir: Path, feature_cols: List[str], label: str):
    # -------- ①  保留原始精度 ----------
    # trade_date 原本是什么就保留什么（一般数据库读出来就是 str）
    # 如果它偶尔是 int/datetime，可以先统一成 str
    # 将 trade_date 统一格式化为 YYYYMMDD
    df['trade_date'] = (
        pd.to_datetime(df['trade_date'], errors='coerce')
          .dt.strftime('%Y%m%d')
    )

    # -------- ②  用临时时间戳提 year/month ----------
    # 一次性新增 year/month，避免碎片化
    df = df.assign(
        year  = df['trade_date'].str.slice(0, 4),
        month = df['trade_date'].str.slice(4, 6)
    )

    # -------- ③  （可选）删除多余列 ----------
    if 'index' in df.columns:           # reset_index 后残留的列
        df.drop(columns=['index'], inplace=True)

    # -------- ④  建 Arrow schema ----------
    schema_dict = {c: pa.float32() for c in feature_cols}
    schema_dict[label] = pa.float32()
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
    """计算每个日期的标签统计量（均值和标准差）
    
    Args:
        df: 包含日期和标签的DataFrame
        label_col: 标签列名
        
    Returns:
        字典，将日期映射到(均值,标准差)元组
    """
    date_stats = {}
    grouped = df.groupby('trade_date')
    
    for date, group in grouped:
        values = group[label_col].values
        if len(values) == 0:
            date_stats[date] = (0.0, 1.0)
            continue
            
        mean = np.mean(values)
        std = np.std(values, ddof=0)  # 使用无偏标准差
        
        # 处理标准差为0或NaN的情况
        if std == 0 or np.isnan(std):
            std = 1.0
            
        date_stats[date] = (mean, std)
    
    return date_stats


def _standardize_labels_by_date(df: pd.DataFrame, label_col: str, date_stats: dict):
    """按日期对标签进行标准化
    
    Args:
        df: 包含日期和标签的DataFrame
        label_col: 标签列名
        date_stats: 日期统计字典，从日期映射到(均值,标准差)元组
        
    Returns:
        标准化后的DataFrame
    """
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
    """生成固定顺序的索引文件，确保数据加载顺序一致性。
    
    为每个split（train/valid/test）生成一个排序好的索引文件，
    存储于meta/full_indices.parquet和meta/{split}_indices.parquet。
    
    Args:
        splits_df: 包含split信息的DataFrame
        meta_dir: 元数据目录路径
    """
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

# ────────────────────────────────────────────────────────────────
#                         Main entry
# ────────────────────────────────────────────────────────────────

def build_pv_dataset_streaming(
    output_dir: str | Path = "data/Dataset/pv_v1",
    start_date: str = "20030101",
    end_date: str = "20131231",
    lag: int = 30,
    label_name: str = "tc_t10_n30_adj",
    clip_std: bool = True,
    standardize_labels_by_date: bool = False,
    split_rules: Sequence[Tuple[str, str, str]] | None = None,
    chunk_freq: str = "M",
    stats_table: str = "ai_is.inter_train_factors_std_l30_d1_2002_2012",
    features_table: str = "ai_is.intermediate_training_factors_market_normalize_lag30_countday1",
    labels_table: str = "ai_is.training_label_ls10_adj_topcor_cr30_cw240",
    restricted_table: str = "ai_is.forbid_pool_comprehensive",
):
    out = Path(output_dir)
    meta_dir, shard_dir, stats_path = _ensure_dirs(out)
    prov = LocalTestDBDataProvider()

    # Load table configurations
    try:
        table_configs = _load_table_configs()
        
        # Validate table names and their configurations
        for table in [stats_table, features_table, labels_table, restricted_table]:
            if table not in table_configs:
                raise ValueError(f"Table {table} not found in configuration")
            
            # Validate required fields for each table type
            config = table_configs[table]
            if config['table_type'] == 'stat' and 'feature_name_field' not in config:
                raise ValueError(f"Table {table} missing required field 'feature_name_field'")
            elif config['table_type'] in ['wide', 'long', 'flag']:
                if 'date_field' not in config or 'code_field' not in config:
                    raise ValueError(f"Table {table} missing required fields 'date_field' or 'code_field'")
            
            logger.info(f"Using table {table} with type {config['table_type']}")
    except Exception as e:
        logger.error(f"Error loading or validating table configurations: {str(e)}")
        raise

    stats = _load_stats(prov, stats_table, clip_std)
    restricted = _load_restricted_set(prov, start_date, end_date, restricted_table)

    feature_cols = [f"{c}_lag_{i}" for c in [
        "adj_open", "adj_high", "adj_low", "adj_close", "vwap", "amount", "turnover_rate"] for i in range(lag)]

    pq.write_table(pa.Table.from_pandas(stats.reset_index()), stats_path, compression="zstd")

    # 用于存储所有日期的标签统计信息
    date_label_stats = {}
    # 如果启用了日期标签标准化，先进行一次数据采集，计算每个日期的标签统计量
    if standardize_labels_by_date:
        logger.info("开始执行日期标签标准化，计算每个日期的标签统计信息...")
        all_date_labels = []
        
        # 如果需要日期标准化，需要先收集所有日期的标签信息
        ranges_for_stats = list(_iter_ranges(start_date, end_date, chunk_freq))
        pbar_stats = tqdm(ranges_for_stats, desc="采集日期标签统计")
        for s, e in pbar_stats:
            df = _fetch_join_filter_chunk(prov, s, e, lag, label_name,
                                           features_table,
                                           labels_table,
                                           restricted)
            if not df.empty:
                # 只保留需要的列以节省内存
                all_date_labels.append(df[['trade_date', label_name]])
            pbar_stats.set_postfix(date=f"{s}-{e}")
            
        if all_date_labels:
            # 合并所有日期的标签数据
            all_labels_df = pd.concat(all_date_labels, ignore_index=True)
            # 计算每个日期的标签统计量
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
        df = _fetch_join_filter_chunk(prov, s, e, lag, label_name,
                                       features_table,
                                       labels_table,
                                       restricted)
        if df.empty:
            continue
        df = _apply_zscore(df, stats, clip_std)
        
        # 应用日期标准化（如果启用）
        if standardize_labels_by_date and date_label_stats:
            logger.debug(f"对日期范围 {s}-{e} 的标签按日期进行标准化")
            df = _standardize_labels_by_date(df, label_name, date_label_stats)
            
        df.dropna(inplace=True)
        if split_rules:
            splits_accum.append(df[["trade_date", "stock_code"]])
        _write_chunk(df, shard_dir, feature_cols, label_name)
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
            # 导入生成固定索引的函数
            # 如果在同一Python进程中，可以直接从包导入
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

    schema_dict = {
        "feature_cols": feature_cols,
        "label_col": label_name,
        "index_cols": ["trade_date", "stock_code"],
        "feature_lag": lag,
        "n_base_features": 7,
        "n_total_features": len(feature_cols),
        "clip_std": clip_std,
        "standardize_labels_by_date": standardize_labels_by_date,
        "build_start_date": start_date,
        "build_end_date": end_date,
        "tables": {
            "stats": {
                "name": stats_table,
                "type": table_configs[stats_table]['table_type'],
                "description": table_configs[stats_table].get('description', '')
            },
            "features": {
                "name": features_table,
                "type": table_configs[features_table]['table_type'],
                "description": table_configs[features_table].get('description', '')
            },
            "labels": {
                "name": labels_table,
                "type": table_configs[labels_table]['table_type'],
                "description": table_configs[labels_table].get('description', '')
            },
            "restricted": {
                "name": restricted_table,
                "type": table_configs[restricted_table]['table_type'],
                "description": table_configs[restricted_table].get('description', '')
            }
        }
    }
    with open(meta_dir / "schema.json", "w", encoding="utf-8") as fp:
        json.dump(schema_dict, fp, indent=2, ensure_ascii=False)

    logger.info("Dataset build completed → %s", out)

# ────────────────────────────────────────────────────────────────
#                            CLI
# ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse, sys
    parser = argparse.ArgumentParser("Streaming builder for pv_v1",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--out", default="data/Dataset/pv_v1")
    parser.add_argument("--start", default="20030101")
    parser.add_argument("--end", default="20131231")
    parser.add_argument("--lag", type=int, default=30)
    parser.add_argument("--label", default="tc_t10_n30_adj")
    parser.add_argument("--no-clip", action="store_true")
    parser.add_argument("--standardize-labels-by-date", action="store_true", help="按日期对标签进行标准化")
    parser.add_argument("--chunk", default="M", help="Chunk frequency: M or Y")
    parser.add_argument("--splits", nargs="*", metavar="NAME:START:END")
    parser.add_argument("--stats-table", default="ai_is.inter_train_factors_std_l30_d1_2002_2012")
    parser.add_argument("--features-table", default="ai_is.intermediate_training_factors_market_normalize_lag30_countday1")
    parser.add_argument("--labels-table", default="ai_is.training_label_ls10_adj_topcor_cr30_cw240")
    parser.add_argument("--restricted-table", default="ai_is.forbid_pool_comprehensive")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    def _parse_splits(items):
        rules = []
        for it in items or []:
            name, s, e = it.split(":")
            rules.append((name, s, e))
        return rules or None

    build_pv_dataset_streaming(
        output_dir=args.out,
        start_date=args.start,
        end_date=args.end,
        lag=args.lag,
        label_name=args.label,
        clip_std=not args.no_clip,
        standardize_labels_by_date=args.standardize_labels_by_date,
        split_rules=_parse_splits(args.splits),
        chunk_freq=args.chunk,
        stats_table=args.stats_table,
        features_table=args.features_table,
        labels_table=args.labels_table,
        restricted_table=args.restricted_table,
    )
