import os
from pathlib import Path
import logging
import json
import math
from typing import Optional, Sequence, Tuple, Iterator, Union, List, Mapping, Any


import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.dataset as ds

import torch
from torch.utils.data import IterableDataset
import duckdb



# Set logging level to DEBUG during development/testing
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO) # Use INFO for production

# -----------------------------------------------------------------------------
# Helper utils
# -----------------------------------------------------------------------------

def _arrow_to_numpy(batch: pa.RecordBatch, cols: List[str]) -> np.ndarray:
    """Stack selected Arrow columns to a single NumPy array (N, C)."""
    arrays = [batch.column(c).to_numpy(zero_copy_only=False) for c in cols]
    return np.stack(arrays, axis=1, dtype=np.float32)

# -----------------------------------------------------------------------------
# Dataset implementation
# -----------------------------------------------------------------------------

class ParquetPVDataset(IterableDataset):
    """
    Iterable dataset for streaming Parquet data with DuckDB backend.
    Optimized for large-scale financial time series with minimal memory footprint.
    """

    def __init__(
        self,
        root: Union[str, Path],
        config: Mapping[str, Any],
        split: Optional[str] = None,
        max_samples: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.root = Path(root)
        self.config = config
        self.split = split
        self.max_samples = max_samples

        # Extract params from config, providing defaults.
        self.shuffle: bool = self.config.get("shuffle", False)
        # 🚀 关键优化：动态调整chunk_size，优化大数据集性能
        default_chunk = self.config.get("chunk_size", None)
        if default_chunk is None:
            # 根据worker数量动态调整chunk_size
            worker_info = torch.utils.data.get_worker_info()
            num_workers = worker_info.num_workers if worker_info else 1
            
            # 对于大数据集，使用更大的chunk_size提高效率
            if num_workers <= 1:
                default_chunk = 32768 # 32K行≈110MB，单worker时使用大块
            else:
                default_chunk = 16384  # 16K行≈55MB，多worker时平衡
                
        self.chunk_size: int = default_chunk
        self.use_fixed_indices: bool = self.config.get("use_fixed_indices", True)
        self.base_seed: int = self.config.get("seed", 0)
        self.keep_meta: bool = self.config.get("keep_meta", False)  # ⭐ 新增：元数据开关
        # DuckDB 首批/跨epoch性能优化：是否物化一次查询、是否复用连接
        self.duck_materialize: bool = bool(self.config.get("duck_materialize", True))
        self.duck_persist_conn: bool = bool(self.config.get("duck_persist_conn", True))
        self._duck_con = None
        self._cache_table_name: Optional[str] = None
        
        self._epoch: int = 0
        
        # --- batch分组方式 ---------------------------------------------------
        self.batch_by = self.config.get("batch_by", "chunk")  # 默认为chunk模式
        
        if self.batch_by == "date":
            logger.info("🗓️ 日期分组模式已启用：每个batch包含同一交易日的所有股票")
        elif self.batch_by == "chunk":
            logger.info("📦 样本分组模式（默认）：按样本数量分组batch")
        else:
            logger.warning(f"⚠️ 未知的batch_by值: {self.batch_by}，回退到chunk模式")
            self.batch_by = "chunk"

        default_min_batch = 2 if self.batch_by == "date" else 1
        cfg_min = self.config.get("min_samples_per_batch", default_min_batch)
        try:
            self.min_samples_per_batch = max(1, int(cfg_min))
        except (TypeError, ValueError):
            logger.warning(f"无效的 min_samples_per_batch 值 {cfg_min!r}，回退到默认 {default_min_batch}")
            self.min_samples_per_batch = default_min_batch
        if self.batch_by == "date" and self.min_samples_per_batch < 2:
            logger.info("batch_by='date' 默认将 min_samples_per_batch 调整为 2，以跳过仅有单个样本的交易日")
            self.min_samples_per_batch = 2

        # --- load schema -------------------------------------------------------
        schema_path = self.root / "meta" / "schema.json"
        if not schema_path.exists():
            raise FileNotFoundError(f"Schema JSON not found: {schema_path}")
        with schema_path.open("r", encoding="utf-8-sig") as fp:
            schema_json = json.load(fp)
        
        # 🚀 新增：特征选择功能 / 序列模式支持
        self.expanded_factor_names: List[str] = schema_json["expanded_factor_names"]
        self.all_feature_cols: List[str] = schema_json.get("feature_cols", [])
        self.label_col: str = schema_json["label_col"]
        self.feature_lag: int = schema_json["feature_lag"]
        self.original_n_base_features: int = schema_json["n_base_features"]
        
        # === Sequence 模式判定 ===
        self.sequence_mode: bool = schema_json.get("sequence_mode", False)
        if self.sequence_mode:
            # 读取 sequence 专属配置
            self.sequence_lag: int = int(schema_json["sequence_lag"])  # 序列长度
            self.list_feature_cols: List[str] = schema_json["list_feature_cols"]  # 每因子一列（LIST）
            
            # 选择使用的因子（带窗口后缀）
            selected_factors_seq = self.config.get("selected_factors", None)
            self.selected_factors_seq: List[str] = (
                selected_factors_seq if selected_factors_seq is not None else self.list_feature_cols
            )
            # 校验选择的因子是否存在
            invalid = [f for f in self.selected_factors_seq if f not in self.list_feature_cols]
            if invalid:
                raise ValueError(f"Invalid selected_factors for sequence_mode: {invalid}.\n"
                                 f"Available: {self.list_feature_cols}")
            
            # 基于 DuckDB 在 SQL 里把 LIST 列展开为标量列（旧命名：{factor}_lag_{k}，k=lag-1..0）
            self.feature_cols = []        # 展开后的别名列名
            self._seq_feature_exprs = []  # 对应的 DuckDB SELECT 表达式
            for f in self.selected_factors_seq:
                for k in range(self.sequence_lag - 1, -1, -1):
                    alias = f"{f}_lag_{k}"
                    # DuckDB list_extract 是 1-based；index=1 是最早元素
                    idx = self.sequence_lag - k
                    expr = f'CAST(list_extract(p."{f}", {idx}) AS FLOAT) AS "{alias}"'
                    self.feature_cols.append(alias)
                    self._seq_feature_exprs.append(expr)
            
            # 训练侧约定的形状参数
            self.n_base_features = len(self.selected_factors_seq)
            self.feature_lag = self.sequence_lag
            logger.info(
                f"🧱 sequence_mode: {self.n_base_features} 因子, lag={self.feature_lag}, 展开列={len(self.feature_cols)}"
            )
        else:
            # 兼容旧（平铺）模式：沿用原逻辑（按平铺列名做特征选择）
            selected_factors = self.config.get("selected_factors", None)
            if selected_factors is not None:
                logger.info(f"🎯 特征选择模式：从{len(self.expanded_factor_names)}个特征中选择{len(selected_factors)}个")
                self._apply_feature_selection(selected_factors)
            else:
                logger.info(f"📊 使用全部特征：{len(self.expanded_factor_names)}个特征")
                self.feature_cols = self.all_feature_cols
                self.n_base_features = self.original_n_base_features
        
        logger.info(f"✅ 最终特征配置：{self.n_base_features}个基础特征，{len(self.feature_cols)}个特征列")

        # --- Load indices and calculate total count -----------------------------
        self.fixed_indices = None
        if self.use_fixed_indices:
            self._load_fixed_indices()
        
        if self.fixed_indices is not None:
            split_size = len(self.fixed_indices)
            self.total_count = min(split_size, self.max_samples or split_size)
        else:
            logger.warning("No fixed indices found. Performance will be suboptimal. Consider generating index files.")
            self.total_count = 0
    
    def _apply_feature_selection(self, selected_factors: List[str]) -> None:
        """
        根据选择的特征名筛选特征列
        
        Args:
            selected_factors: 选择的特征名列表，应该是expanded_factor_names的子集
        """
        # 验证选择的特征是否存在
        invalid_factors = [f for f in selected_factors if f not in self.expanded_factor_names]
        if invalid_factors:
            raise ValueError(f"Invalid factor names: {invalid_factors}. "
                           f"Available factors: {self.expanded_factor_names}")
        
        # 构建选择的特征列
        selected_feature_cols = []
        for factor_name in selected_factors:
            # 找到该特征的所有滞后期列
            factor_cols = [col for col in self.all_feature_cols if col.startswith(f"{factor_name}_")]
            if not factor_cols:
                # 尝试不同的匹配模式
                factor_cols = [col for col in self.all_feature_cols if factor_name in col]
            
            if factor_cols:
                selected_feature_cols.extend(factor_cols)
                logger.debug(f"  {factor_name}: {len(factor_cols)} 列")
            else:
                logger.warning(f"  {factor_name}: 未找到匹配的特征列")
        
        if not selected_feature_cols:
            raise ValueError(f"No feature columns found for selected factors: {selected_factors}")
        
        # 更新特征配置
        self.feature_cols = selected_feature_cols
        self.n_base_features = len(selected_factors)
        
        # 验证特征列数量是否正确
        expected_cols = self.n_base_features * self.feature_lag
        if len(self.feature_cols) != expected_cols:
            logger.warning(f"特征列数量不匹配：期望{expected_cols}，实际{len(self.feature_cols)}")
        
        logger.info(f"🎯 特征选择完成：")
        logger.info(f"  选择特征：{selected_factors}")
        logger.info(f"  基础特征数：{self.n_base_features}")
        logger.info(f"  特征列数：{len(self.feature_cols)}")
        logger.info(f"  滞后期：{self.feature_lag}")
    
    def get_available_factors(self) -> List[str]:
        """返回可用的特征名列表"""
        # sequence_mode 下，因子名即 LIST 列的列名（与 expanded_factor_names 等价，但更直观）
        if getattr(self, "sequence_mode", False):
            return list(self.list_feature_cols)
        return self.expanded_factor_names.copy()
    
    def get_selected_factors(self) -> List[str]:
        """返回当前选择的特征名列表"""
        selected_factors = self.config.get("selected_factors", None)
        if selected_factors is not None:
            return selected_factors.copy()
        # 默认：sequence 模式返回 list_feature_cols，否则返回 expanded_factor_names
        if getattr(self, "sequence_mode", False):
            return list(self.list_feature_cols)
        return self.expanded_factor_names.copy()


    
    def _load_fixed_indices(self) -> None:
        """Try to load fixed indices for the given split to ensure consistent ordering."""
        if self.split is None:
            logger.warning("`use_fixed_indices` is True but no split was provided.")
            return

        # 🚀 如果使用自定义日期范围，强制使用全局索引以支持灵活的日期分割
        use_global_for_custom_dates = (
            self.config.get("use_custom_splits", False) and 
            self.config.get("date_ranges") is not None
        )
        
        if use_global_for_custom_dates:
            logger.info(f"🔄 Using global indices for custom date range splitting for split '{self.split}'")
            full_path = self.root / "meta" / "full_indices.parquet"
            if full_path.exists():
                try:
                    self.fixed_indices = pq.read_table(full_path).to_pandas()
                    logger.info(f"✅ Loaded {len(self.fixed_indices)} global indices for custom date filtering.")
                    # 🚀 应用日期过滤
                    self._apply_date_filter()
                    return
                except Exception as e:
                    logger.error(f"❌ Failed to load global indices {full_path}: {e}")
                    raise RuntimeError(f"Cannot load global indices for custom date splitting: {e}")
            else:
                logger.error(f"❌ Global indices file not found: {full_path}")
                raise FileNotFoundError(f"Global indices file required for custom date splitting: {full_path}")
        
        # 🚀 标准模式：使用原始分割特定的索引
        split_path = self.root / "meta" / f"{self.split}_indices.parquet"
        if split_path.exists():
            try:
                self.fixed_indices = pq.read_table(split_path).to_pandas()
                logger.info(f"Loaded {len(self.fixed_indices)} fixed indices for split '{self.split}'.")
                # 🚀 应用日期过滤
                self._apply_date_filter()
                return
            except Exception as e:
                logger.warning(f"Failed to load {split_path}: {e}")
        
        full_path = self.root / "meta" / "full_indices.parquet"
        if full_path.exists():
            logger.warning(f"Split-specific index not found. Falling back to filtering '{full_path.name}'.")
            try:
                all_indices = pq.read_table(full_path).to_pandas()
                if "split" in all_indices.columns:
                    split_indices = all_indices[all_indices["split"] == self.split]
                    if not split_indices.empty:
                        self.fixed_indices = split_indices.reset_index(drop=True)
                        logger.info(f"Filtered {len(self.fixed_indices)} indices for split '{self.split}' from global index.")
                        # 🚀 应用日期过滤
                        self._apply_date_filter()
                        return
            except Exception as e:
                logger.warning(f"Failed to load or filter {full_path}: {e}")
        
        logger.error("Failed to load any fixed indices. Dataset will be empty.")
        self.fixed_indices = None
    
    def _apply_date_filter(self) -> None:
        """Apply date range filter to fixed indices if date_from/date_to are specified."""
        if self.fixed_indices is None:
            return
            
        date_from = self.config.get("date_from")
        date_to = self.config.get("date_to")
        
        if date_from is None and date_to is None:
            return  # No date filtering needed
            
        initial_count = len(self.fixed_indices)
        
        if date_from is not None and date_to is not None:
            # Both start and end dates specified
            mask = (self.fixed_indices["trade_date"] >= date_from) & (self.fixed_indices["trade_date"] <= date_to)
            self.fixed_indices = self.fixed_indices[mask].reset_index(drop=True)
            logger.info(f"Applied date range filter [{date_from} to {date_to}]: {initial_count} → {len(self.fixed_indices)} indices")
        elif date_from is not None:
            # Only start date specified
            mask = self.fixed_indices["trade_date"] >= date_from
            self.fixed_indices = self.fixed_indices[mask].reset_index(drop=True)
            logger.info(f"Applied date filter [>= {date_from}]: {initial_count} → {len(self.fixed_indices)} indices")
        elif date_to is not None:
            # Only end date specified
            mask = self.fixed_indices["trade_date"] <= date_to
            self.fixed_indices = self.fixed_indices[mask].reset_index(drop=True)
            logger.info(f"Applied date filter [<= {date_to}]: {initial_count} → {len(self.fixed_indices)} indices")
        
        if len(self.fixed_indices) == 0:
            logger.warning(f"Date filtering resulted in empty dataset for split '{self.split}'")
            self.fixed_indices = None
            self.total_count = 0
        else:
            # 重新计算总计数
            split_size = len(self.fixed_indices)
            self.total_count = min(split_size, self.max_samples or split_size)


    
    def _prepare_worker_indices(self, worker_id: int = 0, num_workers: int = 1) -> pd.DataFrame:
        """准备worker特定的索引数据，支持分片和shuffle
        
        Args:
            worker_id: 当前worker ID
            num_workers: 总worker数
        
        Returns:
            worker特定的索引DataFrame
        """
        # 获取基础索引数据
        indices_df = self.fixed_indices.copy()
        if self.max_samples and self.max_samples < len(indices_df):
            indices_df = indices_df.head(self.max_samples)

        if self.batch_by != "date":
            # 旧逻辑（按样本切片）
            if num_workers > 1:
                total_samples = len(indices_df)
                start_idx = (total_samples * worker_id) // num_workers
                end_idx = (total_samples * (worker_id + 1)) // num_workers
                indices_df = indices_df.iloc[start_idx:end_idx].reset_index(drop=True)
                logger.info(f"🔄 Worker {worker_id}/{num_workers}: Processing {len(indices_df)} samples "
                           f"(slice [{start_idx}:{end_idx}] of {total_samples})")
            if self.shuffle:
                # 每个worker使用不同的随机种子，避免重复
                epoch_seed = self.base_seed + self._epoch + worker_id
                np.random.seed(epoch_seed)
                indices_df = indices_df.sample(frac=1, random_state=epoch_seed).reset_index(drop=True)
                logger.debug(f"Worker {worker_id}: Applied shuffle with seed {epoch_seed}")
            return indices_df

        # ✅ 单日 batch：按日期切分
        # ——1. 拿到所有交易日并按需洗牌（shuffle 作用在"天"维度）
        unique_days = indices_df["trade_date"].drop_duplicates().sort_values().tolist()
        if self.shuffle:
            epoch_seed = self.base_seed + self._epoch
            rng = np.random.default_rng(epoch_seed)
            rng.shuffle(unique_days)

        # ——2. 把交易日平均分配给各个 worker
        if num_workers > 1:
            n = len(unique_days)
            start_idx = (n * worker_id) // num_workers
            end_idx = (n * (worker_id + 1)) // num_workers
            days_for_worker = set(unique_days[start_idx:end_idx])
            logger.info(f"🗓️ Worker {worker_id}/{num_workers}: Processing {len(days_for_worker)} days "
                       f"(slice [{start_idx}:{end_idx}] of {n} total days)")
        else:
            days_for_worker = set(unique_days)

        # ——2.1 预过滤：剔除当日样本数 < min_samples_per_batch 的交易日，避免后续 flush 时出现“仅1个样本”的警告
        if self.min_samples_per_batch > 1 and len(days_for_worker) > 0:
            per_day_counts = indices_df["trade_date"].value_counts()
            valid_days = {d for d in days_for_worker if int(per_day_counts.get(d, 0)) >= self.min_samples_per_batch}
            removed_days = len(days_for_worker) - len(valid_days)
            if removed_days > 0:
                logger.info(
                    f"Worker {worker_id}: Filtering out {removed_days} low-sample days (<{self.min_samples_per_batch})"
                )
            days_for_worker = valid_days

        # ——3. 过滤出属于本 worker 的样本
        indices_df = indices_df[indices_df["trade_date"].isin(days_for_worker)].reset_index(drop=True)
        logger.info(f"Worker {worker_id}: Selected {len(indices_df)} samples from {len(days_for_worker)} days")
        return indices_df
    

    
    def __iter__(self):
        """🚀 简化版：每个worker独享DuckDB连接，直接批量产出数据"""
        # ——1. 获取worker信息——
        worker_info = torch.utils.data.get_worker_info()
        worker_id = worker_info.id if worker_info else 0
        num_workers = worker_info.num_workers if worker_info else 1
        self._epoch += 1
        
        if self.fixed_indices is None:
            logger.error("No fixed indices loaded. Cannot produce data.")
            return

        logger.info(f"🚀 Worker {worker_id}/{num_workers}: Starting epoch {self._epoch}")
        
        # ——2. 建立/复用 每-worker 独享DuckDB连接——
        new_connection = False
        if self.duck_persist_conn and (self._duck_con is not None):
            con = self._duck_con
        else:
            con = duckdb.connect(':memory:')
            new_connection = True
        
        # 应用性能配置
        try:
            if new_connection:
                duck_threads = self.config.get('duck_threads', 8)
                duck_memory = self.config.get('duck_memory', '16GB')
                duck_max_temp = self.config.get('duck_max_temp', None)
                
                con.execute("PRAGMA enable_object_cache")
                con.execute(f"SET threads={duck_threads}")
                con.execute(f"SET memory_limit='{duck_memory}'")
                con.execute("SET preserve_insertion_order=false")
                con.execute("SET default_null_order='nulls_first'")
                if duck_max_temp is not None:
                    # 允许配置更高的临时目录使用上限，避免大查询排序/物化时 OOM
                    con.execute(f"PRAGMA max_temp_directory_size='{duck_max_temp}'")
                
                # 临时目录配置
                temp_dir = self.root.parent / ".duck_tmp"
                temp_dir.mkdir(exist_ok=True)
                con.execute(f"SET temp_directory='{temp_dir}'")
                
                logger.info(f"Worker {worker_id}: DuckDB config applied (Mem: {duck_memory}, Threads: {duck_threads})")
        except Exception as e:
            logger.warning(f"Worker {worker_id}: Failed to apply DuckDB configs: {e}")
        
        if self.duck_persist_conn and new_connection:
            self._duck_con = con
        
        # ——3. 注册数据视图和索引表——
        try:
            shard_glob = str(self.root / "shards" / "**" / "*.parquet")
            con.execute(f"""
                CREATE OR REPLACE VIEW pv_data AS
                SELECT * FROM parquet_scan('{shard_glob}', binary_as_string=true)
            """)
            logger.info(f"Worker {worker_id}: Created pv_data view")
        except Exception as e:
            logger.warning(f"Worker {worker_id}: Failed with binary_as_string=true: {e}")
            # 回退到标准方式
            con.execute(f"""
                CREATE OR REPLACE VIEW pv_data AS
                SELECT * FROM parquet_scan('{shard_glob}')
            """)
            logger.info(f"Worker {worker_id}: Created pv_data view with standard settings")
        
        # 准备worker特定的索引数据
        worker_indices = self._prepare_worker_indices(worker_id, num_workers)
        indices_table_name = f"idx_{worker_id}"
        con.register(indices_table_name, worker_indices)
        logger.info(f"Worker {worker_id}: Registered {len(worker_indices)} indices as '{indices_table_name}'")
        
        # ——4. 构建查询SQL——
        # 构造 SELECT 子句：sequence 模式下需要把 LIST 列展开成标量别名列
        if getattr(self, "sequence_mode", False):
            feature_exprs_sql = ", ".join(self._seq_feature_exprs)
            tail_cols_sql = f'p."{self.label_col}" AS "{self.label_col}", p.trade_date, p.stock_code, idx.index_id AS index_id'
            select_clause = f"{feature_exprs_sql}, {tail_cols_sql}"
        else:
            all_cols = self.feature_cols + [self.label_col, 'trade_date', 'stock_code']
            select_clause = ", ".join([f'p."{c}"' for c in all_cols])
            select_clause = f"{select_clause}, idx.index_id AS index_id"
        
        # 根据配置/模式决定是否跳过全局排序（可显著降低内存占用）
        # 当存在稳定的 index_id 且 shuffle=False 且按 chunk 分组时，可跳过 ORDER BY 以实现纯流式处理
        skip_order_by_cfg = self.config.get("skip_order_by", None)
        dynamic_skip = (self.use_fixed_indices and not self.shuffle and self.batch_by != "date")
        skip_order_by = dynamic_skip if skip_order_by_cfg is None else bool(skip_order_by_cfg)

        # 根据batch分组方式选择排序方式（或跳过）
        if skip_order_by:
            order_clause = ''
            logger.info("⚡ Streaming without ORDER BY (stable indices + no shuffle)")
        else:
            order_clause = 'ORDER BY idx.index_id'
            if self.batch_by == "date":
                order_clause = 'ORDER BY p.trade_date, idx.index_id'  # 保证同日行连续
        
        sql = f"""
            SELECT {select_clause}
            FROM pv_data p
            INNER JOIN {indices_table_name} idx ON 
                p.trade_date = idx.trade_date AND 
                p.stock_code = idx.stock_code
            {order_clause}
        """

        # ——4.1 首次可选物化（TEMP TABLE），后续 epoch 直接 SELECT *
        # 大序列/跳过排序时禁用物化，避免巨大的内存与临时磁盘占用
        large_seq = bool(getattr(self, "sequence_mode", False) and int(self.feature_lag) >= 200)
        do_materialize = bool(self.duck_materialize and (not skip_order_by) and (not large_seq))
        if do_materialize:
            def _cache_exists() -> bool:
                if self._cache_table_name is None:
                    return False
                try:
                    con.execute(f"SELECT 1 FROM {self._cache_table_name} LIMIT 1")
                    return True
                except Exception:
                    return False

            if not _cache_exists():
                self._cache_table_name = f"cache_{self.split or 'all'}_{worker_id}"
                try:
                    con.execute(f"CREATE TEMP TABLE {self._cache_table_name} AS {sql}")
                    logger.info(f"Worker {worker_id}: Materialized cache table '{self._cache_table_name}' created")
                    sql = f"SELECT * FROM {self._cache_table_name}"
                except Exception as e:
                    logger.warning(f"Worker {worker_id}: Failed to materialize cache table: {e}")
                    # 失败则回退到在线查询
            else:
                sql = f"SELECT * FROM {self._cache_table_name}"
        
        # ——5. 流式拿批数据——
        logger.info(f"Worker {worker_id}: Starting query execution with chunk_size={self.chunk_size}")
        
        # 用于拼接"同一天"的缓冲
        buf_feats = []
        buf_labels = []
        buf_dates = []
        buf_codes = []
        current_day = None


        def _flush_day_batch():
            if not buf_labels:
                return None
            sample_count = sum(arr.shape[0] for arr in buf_labels)
            if sample_count < self.min_samples_per_batch:
                if current_day is not None:
                    logger.warning(
                        f"Worker {worker_id}: 跳过交易日 {current_day}，仅 {sample_count} 个样本 (<{self.min_samples_per_batch})"
                    )
                buf_feats.clear()
                buf_labels.clear()
                buf_dates.clear()
                buf_codes.clear()
                return None
            feat_np = np.concatenate(buf_feats, axis=0)             # (B, F_total)
            lab_np = np.concatenate(buf_labels, axis=0).astype(np.float32)  # (B,)
            # 还原形状 -> (B, L, C)
            num_samples = lab_np.shape[0]
            feat_np = feat_np.reshape(num_samples, self.n_base_features, self.feature_lag)
            feat_np = feat_np.transpose(0, 2, 1)  # (B, L, C)

            feats = torch.from_numpy(feat_np).pin_memory()
            labels = torch.from_numpy(lab_np).pin_memory()
            
            if self.keep_meta:
                yield_feats = feats
                yield_labels = labels
                yield_meta_d = buf_dates.copy()
                yield_meta_c = buf_codes.copy()
                # 清空缓冲
                buf_feats.clear()
                buf_labels.clear() 
                buf_dates.clear()
                buf_codes.clear()
                return (yield_feats, yield_labels, yield_meta_d, yield_meta_c)
            else:
                buf_feats.clear()
                buf_labels.clear()
                buf_dates.clear()
                buf_codes.clear()
                return (feats, labels)
        
        try:
            reader = con.execute(sql).fetch_record_batch(rows_per_batch=self.chunk_size)
            batch_count = 0
            total_rows = 0
            
            for record_batch in reader:
                if record_batch.num_rows == 0:
                    continue
                
                batch_count += 1
                total_rows += record_batch.num_rows
                
                # 取出当批的列
                # 注意：一次性取出以便随后按"日"切片
                feat_block = np.stack(
                    [col.to_numpy(zero_copy_only=False) for col in record_batch.select(self.feature_cols).columns],
                    axis=1, dtype=np.float32
                )  # (N, F_total)
                lab_block = record_batch.column(self.label_col).to_numpy(zero_copy_only=False).astype(np.float32)
                date_block = record_batch.column("trade_date").to_numpy(zero_copy_only=False)
                code_block = record_batch.column("stock_code").to_numpy(zero_copy_only=False)
                # 轻量级稳定排序：按 index_id 升序，避免全局 ORDER BY 带来的内存压力
                try:
                    index_block = record_batch.column("index_id").to_numpy(zero_copy_only=False)
                    order_idx = np.argsort(index_block, kind="stable")
                    if order_idx is not None and order_idx.shape[0] == feat_block.shape[0]:
                        feat_block = feat_block[order_idx]
                        lab_block = lab_block[order_idx]
                        date_block = date_block[order_idx]
                        code_block = code_block[order_idx]
                except Exception as _e:
                    # 不影响主流程，必要时可以降级为未排序的块
                    pass

                if self.batch_by != "date":
                    # 原来的整块 yield 路径（保持兼容）
                    num_samples = lab_block.shape[0]
                    if num_samples < self.min_samples_per_batch:
                        logger.debug(
                            f"Worker {worker_id}: 跳过仅有 {num_samples} 个样本的小批次 (<{self.min_samples_per_batch})"
                        )
                        continue
                    feat_np = feat_block.reshape(num_samples, self.n_base_features, self.feature_lag).transpose(0, 2, 1)
                    feats = torch.from_numpy(feat_np).pin_memory()
                    labels = torch.from_numpy(lab_block).pin_memory()
                    if self.keep_meta:
                        yield feats, labels, date_block.tolist(), code_block.tolist()
                    else:
                        yield feats, labels
                    continue

                # ✅ 单日 batch：遍历这个 block，按日期切片并写入缓冲
                pos = 0
                while pos < len(date_block):
                    day = date_block[pos]
                    if current_day is None:
                        current_day = day
                    if day != current_day:
                        # 日期变了，先把之前那天 flush
                        out = _flush_day_batch()
                        if out is not None:
                            yield out
                        current_day = day

                    # 找到这一连段同日的末尾（run-length）
                    next_break = pos + 1
                    while next_break < len(date_block) and date_block[next_break] == current_day:
                        next_break += 1

                    # 追加到缓冲
                    buf_feats.append(feat_block[pos:next_break])
                    buf_labels.append(lab_block[pos:next_break])
                    if self.keep_meta:
                        buf_dates.extend(date_block[pos:next_break].tolist())
                        buf_codes.extend(code_block[pos:next_break].tolist())

                    pos = next_break
                
                if batch_count % 20 == 0:
                    logger.debug(f"Worker {worker_id}: Processed {batch_count} batches, {total_rows} rows")

            # reader 结束后，flush 最后一天
            if self.batch_by == "date":
                out = _flush_day_batch()
                if out is not None:
                    yield out
            
            logger.info(f"Worker {worker_id}: Completed. Total: {batch_count} batches, {total_rows} rows")
            
        except Exception as e:
            logger.error(f"Worker {worker_id}: Query execution failed: {e}")
            raise

    def __len__(self):
        """返回 DataLoader 将看到的 *总步数*（全体 worker 合起来）"""
        if self.fixed_indices is None:
            return 0

        if self.batch_by == "date":
            # 全局交易日数（一个交易日产出一个 batch）
            return int(self.fixed_indices["trade_date"].nunique())

        # chunk 模式：全局样本数 / chunk_size
        n = len(self.fixed_indices)
        if self.max_samples is not None:
            n = min(n, self.max_samples)
        return math.ceil(n / self.chunk_size)+1
    

