import duckdb
import json
import logging
import pathlib

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.data_service.pipelines.Dataset_builder import FACTOR_WINDOWS
from src.data_service.pipelines.Dataset_builder.main_builder import (
    build_pv_dataset_wide_daily,
)
from src.data_service.pipelines.Dataset_builder.indices import generate_indices


root = pathlib.Path("data/Dataset/pv_v7")

CHUNK_FREQ = "Q"
WARMUP_DAYS = 200
FILTER_FEATURES_RESTRICTED = True
EXCLUDE_CODE_PREFIXES = ['9']
EXCLUDE_CODES_REGEX = None
GENERATE_INDICES = True
INDICES_LAGS = (30, 300, 500)
MAX_FACTORS_PER_BATCH = 16  # 单批限制 pivot 的因子数，降低内存峰值

# 分割规则（提到外部，供后续使用）
SPLIT_RULES = [
    ("train", "20060101", "20181231"),
    ("valid", "20190101", "20201231"),
    ("test", "20220101", "20250831"),
]


def ensure_splits_file(root: pathlib.Path, split_rules=SPLIT_RULES):
    """若 meta/splits.parquet 不存在，则根据 full_indices 或 wide_daily 生成。"""
    meta_dir = root / "meta"
    splits_path = meta_dir / "splits.parquet"
    if splits_path.exists():
        logging.info("splits.parquet already exists at %s", splits_path)
        return splits_path

    logging.info("Generating splits.parquet from available data sources...")
    
    # 1) 取键集合
    full_idx = meta_dir / "full_indices.parquet"
    con = duckdb.connect()
    try:
        if full_idx.exists():
            logging.info("Using full_indices.parquet as source for splits")
            df = con.execute(
                f"SELECT trade_date, stock_code FROM read_parquet('{full_idx.as_posix()}')"
            ).fetchdf()
        else:
            logging.info("Using wide_daily shards as source for splits")
            wide_glob = (root / "shards" / "wide_daily" / "**" / "*.parquet").as_posix()
            df = con.execute(
                f"SELECT DISTINCT trade_date, stock_code FROM read_parquet('{wide_glob}', hive_partitioning=1, union_by_name=1)"
            ).fetchdf()
    finally:
        con.close()

    if df.empty:
        raise RuntimeError("无法生成 splits：没有 trade_date/stock_code 键")

    # 2) 应用规则
    def to_split(d: str):
        for sp, s, e in split_rules:
            if s <= d <= e:
                return sp
        return "unused"
    
    df["split"] = df["trade_date"].astype(str).map(to_split)
    df = df.sort_values(["trade_date", "stock_code"]).reset_index(drop=True)
    
    # 写入文件
    meta_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df), splits_path, compression="zstd")
    logging.info("Generated splits.parquet at %s with %d rows", splits_path, len(df))
    
    # 打印分割统计
    split_counts = df["split"].value_counts()
    logging.info("Split distribution: %s", split_counts.to_dict())
    
    return splits_path


if not root.exists():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    
    # 使用全局 SPLIT_RULES
    all_dates: list[pd.Timestamp] = []
    for _, s_date, e_date in SPLIT_RULES:
        all_dates.append(pd.to_datetime(s_date, format="%Y%m%d"))
        all_dates.append(pd.to_datetime(e_date, format="%Y%m%d"))
    effective_start_date = min(all_dates).strftime("%Y%m%d")
    effective_end_date = max(all_dates).strftime("%Y%m%d")
    logging.info(
        "Dataset root %s not found. Auto-initializing with data from %s to %s using defined splits...",
        root,
        effective_start_date,
        effective_end_date,
    )
    try:
        (root / "duck_tmp").mkdir(parents=True, exist_ok=True)

        build_pv_dataset_wide_daily(
            output_dir=str(root),
            start_date=effective_start_date,
            end_date=effective_end_date,
            label_name="tc_t10_n30_adj",
            factor_windows=FACTOR_WINDOWS,
            label_shift=10,
            winsorise_labels=True,
            label_winsor_q=(0.0005, 0.9995),
            split_rules=SPLIT_RULES,
            chunk_freq=CHUNK_FREQ,
            stats_table="ai_is.train_signals_std_std_2008_2018_mad8p0",
            features_table=[
                # 基础市场数据
                "ai_is.inter_train_factors_mkt_processed_v3",
                
                # Growth (成长类) - 3个表
                "ai_is.quantitative_growth_profitability_signals",
                "ai_is.quantitative_growth_revenue_asset_signals",
                "ai_is.quantitative_growth_forecast_trend_signals",
                
                # Analyst (分析师类) - 2个表
                "ai_is.quantitative_analyst_coverage_rating_signals",
                "ai_is.quantitative_analyst_earnings_revision_signals",
                
                # Value (价值类) - 1个表
                "ai_is.quantitative_value_valuation_signals",
                
                # Sentiment (情绪类) - 5个表
                "ai_is.quantitative_sentiment_volatility_signals",
                "ai_is.quantitative_sentiment_momentum_signals",
                "ai_is.quantitative_sentiment_price_return_signals",
                "ai_is.quantitative_sentiment_value_reversal_signals",
                "ai_is.quantitative_sentiment_liquidity_signals",
                
                # Quality (质量类) - 3个表
                "ai_is.quantitative_quality_cashflow_safety_signals",
                "ai_is.quantitative_quality_operating_efficiency_signals",
                "ai_is.quantitative_quality_profit_quality_signals",
                
                # Alternative (另类) - 2个表
                "ai_is.quantitative_alternative_high_frequency_signals",
                "ai_is.quantitative_alternative_institutional_patent_signals",
            ],
            labels_table="ai_is.training_label_v1",
            restricted_table="ai_is.forbid_pool_comprehensive",
            factor_based_nan_handling=True,
            consecutive_nan_threshold=None,
            warmup_days=WARMUP_DAYS,
            dropna_factor_value=True,
            filter_features_restricted=FILTER_FEATURES_RESTRICTED,
            exclude_code_prefixes=EXCLUDE_CODE_PREFIXES,
            exclude_codes_regex=EXCLUDE_CODES_REGEX,
            max_factors_per_batch=MAX_FACTORS_PER_BATCH,
        )
        logging.info("Dataset auto-initialization complete at %s", root)

        if GENERATE_INDICES:
            try:
                generate_indices(
                    dataset_root=root,
                    lags=INDICES_LAGS,
                    factors='auto',
                    threads=4,  # 限制线程数避免内存问题
                    temp_dir=root / 'duck_tmp',
                    memory_limit='16GB',  # 限制内存使用
                    with_splits=True,
                    force=False,
                    require_label_for_train=True,  # 训练模式要求有 label
                )
                logging.info("indices generation completed for %s", root)
            except Exception as idx_err:
                logging.error("Failed to generate indices for %s: %s", root, idx_err, exc_info=True)
    except Exception as e:
        logging.error("Failed to auto-initialize dataset at %s: %s", root, e, exc_info=True)
        raise
else:
    logging.info("Dataset root %s already exists; skip auto-initialization", root)

# === 确保 splits 存在，并重建 indices（始终执行） ===========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
try:
    ensure_splits_file(root, SPLIT_RULES)
    
    # Sanity check: 打印 splits 分布
    splits_path = root / "meta" / "splits.parquet"
    if splits_path.exists():
        with duckdb.connect() as con:
            df_dist = con.execute(
                f"SELECT split, COUNT(*) AS n FROM read_parquet('{splits_path.as_posix()}') GROUP BY 1 ORDER BY 1"
            ).fetchdf()
            print("\n📊 Splits distribution:")
            print(df_dist.to_string(index=False))
            print()
    
    logging.info("Regenerating indices with splits...")
    generate_indices(
        dataset_root=root,
        lags=INDICES_LAGS,
        factors='auto',
        threads=4,
        temp_dir=root / 'duck_tmp',
        memory_limit='16GB',
        with_splits=True,                 # ✅ 现在肯定有 splits 了
        force=False,                      # ready_pairs 复用；index_lag* 会覆盖
        require_label_for_train=True,
    )
    logging.info("indices regeneration (with splits) completed for %s", root)
    
    # Sanity check: 打印 index_lag300 的 split 分布
    idx_path = root / "meta" / "indices" / "index_lag300.parquet"
    if idx_path.exists():
        with duckdb.connect() as con:
            df_idx_dist = con.execute(
                f"SELECT split, COUNT(*) AS n FROM read_parquet('{idx_path.as_posix()}') GROUP BY 1 ORDER BY 1"
            ).fetchdf()
            print("\n📊 Index_lag300 split distribution:")
            print(df_idx_dist.to_string(index=False))
            
            # 训练集可用样本（ok_factors=1 & has_label=1）
            train_count = con.execute(
                f"""
                SELECT COUNT(*) FROM read_parquet('{idx_path.as_posix()}')
                WHERE ok_factors=1 AND has_label=1 AND trade_date BETWEEN '20080101' AND '20181231'
                """
            ).fetchone()[0]
            print(f"\n✅ 训练集可用样本（20080101-20181231, ok_factors=1 & has_label=1）: {train_count:,}")
            print()
            
except Exception as e:
    logging.error("Failed to ensure splits / regenerate indices: %s", e, exc_info=True)
    raise

# --- Inspect wide_daily and labels shards ------------------------------------
wide_dir = root / "shards" / "wide_daily"
labels_dir = root / "shards" / "labels"

if wide_dir.exists() and list(wide_dir.rglob("*.parquet")):
    with duckdb.connect() as con:
        wide_glob = (wide_dir / "**" / "*.parquet").as_posix()
        con.execute(
            f"""
            CREATE OR REPLACE VIEW wide_daily AS
            SELECT * FROM parquet_scan('{wide_glob}')
        """
        )
        print("wide_daily column info:")
        print(con.execute("PRAGMA table_info('wide_daily')").fetchdf())
        print("wide_daily samples:")
        print(
            con.execute(
                "SELECT * FROM wide_daily LIMIT 5"
            ).fetchdf()
        )
else:
    print("wide_daily shards are not available, skip view creation")

if labels_dir.exists() and list(labels_dir.rglob("*.parquet")):
    with duckdb.connect() as con:
        labels_glob = (labels_dir / "**" / "*.parquet").as_posix()
        con.execute(
            f"""
            CREATE OR REPLACE VIEW labels_long AS
            SELECT * FROM parquet_scan('{labels_glob}')
        """
        )
        print("labels_long column info:")
        print(con.execute("PRAGMA table_info('labels_long')").fetchdf())
        print("labels_long samples:")
        print(
            con.execute(
                "SELECT trade_date, stock_code, * FROM labels_long LIMIT 5"
            ).fetchdf()
        )
else:
    print("labels shards are not available, skip view creation")


# --- Inspect splits.parquet --------------------------------------------------
splits_path = root / "meta" / "splits.parquet"
if splits_path.exists():
    splits = pq.read_table(splits_path).to_pandas()
    print("splits.dtypes:\n", splits.dtypes)
    print("splits samples:\n", splits.iloc[:5, :3])
else:
    print("splits.parquet not found")

# --- Inspect schema.json -----------------------------------------------------
schema_path = root / "meta" / "schema.json"
if schema_path.exists():
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)
    tables_info = schema.get("tables", {})
    print("schema info:")
    print(f"dynamic_window: {schema.get('dynamic_window', False)}")
    print(f"default feature lag: {schema.get('feature_lag', 'unknown')}")
    print(f"base factor count: {schema.get('n_base_features', 'unknown')}")
    print(f"label column: {schema.get('label_col', 'unknown')}")
    print(f"features_long table spec: {tables_info.get('features_long', {})}")
    print(f"labels table spec: {tables_info.get('labels', {})}")
else:
    print("schema.json not found")
