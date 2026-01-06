import duckdb, pandas as pd, pyarrow.parquet as pq, pathlib, json
import logging
from src.data_service.pipelines.DFZQ_GRU_PV_pipline.build_pv_dataset_streaming import build_pv_dataset_streaming

root = pathlib.Path("data/Dataset/pv_v2")

# # 自动初始化数据集（如目录不存在）
# if not root.exists():
#     logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
#     current_year = pd.Timestamp.now().year
#     test_end_year = min(current_year, 2024)
#     split_rules = [
#         ("train", "20030101", "20181231"),
#         ("valid", "20190101", "20201231"),
#         ("test", "20210101", f"{test_end_year}1231"),
#     ]
#     all_dates = []
#     for _, s_date, e_date in split_rules:
#         all_dates.append(pd.to_datetime(s_date, format="%Y%m%d"))
#         all_dates.append(pd.to_datetime(e_date, format="%Y%m%d"))
#     effective_start_date = min(all_dates).strftime("%Y%m%d")
#     effective_end_date = max(all_dates).strftime("%Y%m%d")
#     logging.info(f"Dataset root {root} not found. Auto-initializing with data from {effective_start_date} to {effective_end_date} using defined splits...")
#     try:
#         build_pv_dataset_streaming(
#             output_dir=str(root),
#             start_date=effective_start_date,
#             end_date=effective_end_date,
#             lag=30,
#             label_name="tc_t10_n30_adj",
#             clip_std=True,
#             split_rules=split_rules,
#             chunk_freq="M",
#             stats_table="ai_is.inter_train_factors_std_l30_d1_2002_2012",
#             features_table="ai_is.intermediate_training_factors_market_normalize_lag30_countday1",
#             labels_table="ai_is.training_label_ls10_adj_topcor_cr30_cw240",
#             restricted_table="ai_is.restricted_stock_pool",
#         )
#         logging.info(f"Dataset auto-initialization complete at {root}")
#     except Exception as e:
#         logging.error(f"Failed to auto-initialize dataset at {root}: {e}", exc_info=True)
#         raise

# --- pv_data 里的类型 ---------------------------------
con = duckdb.connect()
con.execute(f"""
    CREATE OR REPLACE VIEW pv_data AS
    SELECT * FROM parquet_scan('{root / "shards"}/**/*.parquet')
""")
print("pv_data 字段类型：")
print(con.execute("PRAGMA table_info('pv_data')").fetchdf())
print("pv_data 样例：")

sample_pv_data = con.execute("SELECT trade_date, stock_code FROM pv_data LIMIT 5").fetchdf()
print(sample_pv_data)






# --- splits.parquet 里的类型 ---------------------------
splits = pq.read_table(root / "meta" / "splits.parquet").to_pandas()
print("splits.dtypes:\n", splits.dtypes)
print("splits样例：\n", splits.iloc[:5, :3])



###确保这两边的数据能merge trade_date格式看看是不是一致的

# 读取 pv_data 和 splits 数据
pv_data = con.execute("SELECT trade_date, stock_code FROM pv_data").fetchdf()
splits = pq.read_table(root / "meta" / "splits.parquet").to_pandas()

# 确保 trade_date 格式一致
pv_data['trade_date'] = pd.to_datetime(pv_data['trade_date']).dt.strftime('%Y%m%d')
splits['trade_date'] = pd.to_datetime(splits['trade_date']).dt.strftime('%Y%m%d')

# 合并数据
merged_data = pd.merge(pv_data, splits, on='trade_date', how='inner')

print(merged_data)

