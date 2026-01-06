
"""Debug version of the parquet PV dataset with detailed I/O profiling hooks."""

from __future__ import annotations

import logging
import time
from collections import deque
from contextlib import nullcontext
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Tuple

import duckdb
import numpy as np
import pandas as pd
import torch

from .parquet_pv_dataset import ParquetPVDataset as _BaseDataset


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class _TimingContext:
    """Context manager that records wall time into a collector."""

    __slots__ = ("_collector", "_name", "_start")

    def __init__(self, collector: "_TimingCollector", name: str) -> None:
        self._collector = collector
        self._name = name
        self._start: float | None = None

    def __enter__(self) -> None:
        self._start = time.perf_counter()
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._start is not None:
            duration = time.perf_counter() - self._start
            self._collector.add_duration(self._name, duration)
        return False


class _TimingCollector:
    """Accumulates timing statistics for labelled sections."""

    __slots__ = ("enabled", "label", "_totals", "_counts")

    def __init__(self, enabled: bool, label: str) -> None:
        self.enabled = enabled
        self.label = label
        self._totals: Dict[str, float] = {}
        self._counts: Dict[str, int] = {}

    def track(self, name: str):
        if not self.enabled:
            return nullcontext()
        return _TimingContext(self, name)

    def add_duration(self, name: str, duration: float) -> None:
        if not self.enabled:
            return
        self._totals[name] = self._totals.get(name, 0.0) + duration
        self._counts[name] = self._counts.get(name, 0) + 1

    def summary_items(self, top_n: Optional[int] = None) -> List[Tuple[str, float, int, float]]:
        if not self.enabled:
            return []
        items: List[Tuple[str, float, int, float]] = []
        for name, total in self._totals.items():
            count = self._counts.get(name, 0)
            avg = total / count if count else 0.0
            items.append((name, total, count, avg))
        items.sort(key=lambda x: x[1], reverse=True)
        if top_n is not None and top_n > 0:
            items = items[:top_n]
        return items

    def summary_lines(self, top_n: Optional[int] = None) -> List[str]:
        return [
            f"{name}: total={total:.3f}s avg={avg * 1000:.2f}ms ({count}x)"
            for name, total, count, avg in self.summary_items(top_n)
        ]


class ParquetPVDebugDataset(_BaseDataset):
    """Instrumentation wrapper around :class:`ParquetPVDataset` for profiling."""

    def __init__(
        self,
        root: str,
        config: Mapping[str, Any],
        split: Optional[str] = None,
        max_samples: Optional[int] = None,
    ) -> None:
        self.profile_enabled: bool = bool(config.get("debug_profile_io", True))
        top_n_cfg = config.get("debug_profile_top_n", 6)
        try:
            self.profile_top_n = max(1, int(top_n_cfg))
        except (TypeError, ValueError):
            self.profile_top_n = 6
        log_every_cfg = config.get("debug_profile_log_every_batches", 0)
        try:
            self._profile_batch_log_every = int(log_every_cfg)
        except (TypeError, ValueError):
            self._profile_batch_log_every = 0
        history_cfg = config.get("debug_profile_history", 8)
        try:
            history_cap = max(1, int(history_cfg))
        except (TypeError, ValueError):
            history_cap = 8
        self._profile_history: Deque[Dict[str, Any]] = deque(maxlen=history_cap)
        self._last_iter_stats: Optional[Dict[str, Any]] = None
        self._init_last_summary: Optional[Dict[str, Any]] = None
        self._init_profiler = _TimingCollector(self.profile_enabled, label=f"init[{split or 'all'}]")

        with self._init_profiler.track("init_total"):
            super().__init__(root, config, split=split, max_samples=max_samples)

        if self.profile_enabled:
            summary = self._init_profiler.summary_lines(top_n=self.profile_top_n)
            if summary:
                logger.info(
                    "⏱️ [DEBUG-INIT] split=%s %s",
                    self.split or 'all',
                    " | ".join(summary),
                )
            else:
                logger.info(
                    "⏱️ [DEBUG-INIT] split=%s init_total recorded but no detailed steps",
                    self.split or 'all',
                )
            self._init_last_summary = {
                "split": self.split,
                "summary": summary,
            }

        default_min_batch = 2 if self.batch_by == "date" else 1
        cfg_min = self.config.get("min_samples_per_batch", default_min_batch)
        try:
            self.min_samples_per_batch = max(1, int(cfg_min))
        except (TypeError, ValueError):
            logger.warning(
                "无效的 min_samples_per_batch 值 %r，回退到默认 %d",
                cfg_min,
                default_min_batch,
            )
            self.min_samples_per_batch = default_min_batch
        if self.batch_by == "date" and self.min_samples_per_batch < 2:
            logger.info("batch_by='date' 默认将 min_samples_per_batch 调整为 2，以跳过仅有单个样本的交易日")
            self.min_samples_per_batch = 2

    # ------------------------------------------------------------------
    # Init helpers with timing wrappers
    # ------------------------------------------------------------------
    def _load_fixed_indices(self) -> None:  # type: ignore[override]
        if not self.profile_enabled:
            return super()._load_fixed_indices()
        with self._init_profiler.track("load_fixed_indices"):
            return super()._load_fixed_indices()

    def _apply_date_filter(self) -> None:  # type: ignore[override]
        if not self.profile_enabled:
            return super()._apply_date_filter()
        with self._init_profiler.track("apply_date_filter"):
            return super()._apply_date_filter()

    # ------------------------------------------------------------------
    # Profiling utilities
    # ------------------------------------------------------------------
    def _log_profile_snapshot(
        self,
        profiler: _TimingCollector,
        prefix: str,
        batch_count: int,
        total_rows: int,
        stage: str,
    ) -> None:
        if not self.profile_enabled:
            return
        summary = profiler.summary_lines(top_n=self.profile_top_n)
        detail = " | ".join(summary) if summary else "no timing data yet"
        logger.info(
            "⏱️ [%s] %s -> %d batches, %d rows | %s",
            stage,
            prefix,
            batch_count,
            total_rows,
            detail,
        )

    # ------------------------------------------------------------------
    # Worker index preparation with timing
    # ------------------------------------------------------------------
    def _prepare_worker_indices(
        self,
        worker_id: int = 0,
        num_workers: int = 1,
        profiler: Optional[_TimingCollector] = None,
    ) -> pd.DataFrame:
        ctx = profiler.track("prepare_worker_indices") if profiler else nullcontext()
        with ctx:
            indices_df = self.fixed_indices.copy()
            if self.max_samples and self.max_samples < len(indices_df):
                indices_df = indices_df.head(self.max_samples)

            if self.batch_by != "date":
                if num_workers > 1:
                    total_samples = len(indices_df)
                    start_idx = (total_samples * worker_id) // num_workers
                    end_idx = (total_samples * (worker_id + 1)) // num_workers
                    indices_df = indices_df.iloc[start_idx:end_idx].reset_index(drop=True)
                    logger.info(
                        "🔄 Worker %d/%d: Processing %d samples (slice [%d:%d] of %d)",
                        worker_id,
                        num_workers,
                        len(indices_df),
                        start_idx,
                        end_idx,
                        total_samples,
                    )
                if self.shuffle:
                    epoch_seed = self.base_seed + self._epoch + worker_id
                    with (profiler.track("shuffle_indices") if profiler else nullcontext()):
                        np.random.seed(epoch_seed)
                        indices_df = indices_df.sample(frac=1, random_state=epoch_seed).reset_index(drop=True)
                    logger.debug("Worker %d: Applied shuffle with seed %d", worker_id, epoch_seed)
                return indices_df

            with (profiler.track("unique_days") if profiler else nullcontext()):
                unique_days = indices_df["trade_date"].drop_duplicates().sort_values().tolist()
            if self.shuffle:
                epoch_seed = self.base_seed + self._epoch
                with (profiler.track("shuffle_days") if profiler else nullcontext()):
                    rng = np.random.default_rng(epoch_seed)
                    rng.shuffle(unique_days)

            if num_workers > 1:
                n = len(unique_days)
                start_idx = (n * worker_id) // num_workers
                end_idx = (n * (worker_id + 1)) // num_workers
                days_for_worker = set(unique_days[start_idx:end_idx])
                logger.info(
                    "🗓️ Worker %d/%d: Processing %d days (slice [%d:%d] of %d)",
                    worker_id,
                    num_workers,
                    len(days_for_worker),
                    start_idx,
                    end_idx,
                    n,
                )
            else:
                days_for_worker = set(unique_days)

            with (profiler.track("filter_indices_by_days") if profiler else nullcontext()):
                indices_df = indices_df[indices_df["trade_date"].isin(days_for_worker)].reset_index(drop=True)
            logger.info(
                "Worker %d: Selected %d samples from %d unique days",
                worker_id,
                len(indices_df),
                len(days_for_worker),
            )
            return indices_df

    # ------------------------------------------------------------------
    # Iteration with detailed profiling
    # ------------------------------------------------------------------
    def __iter__(self) -> Iterable[Any]:  # type: ignore[override]
        worker_info = torch.utils.data.get_worker_info()
        worker_id = worker_info.id if worker_info else 0
        num_workers = worker_info.num_workers if worker_info else 1
        self._epoch += 1

        if self.fixed_indices is None:
            logger.error("No fixed indices loaded. Cannot produce data.")
            return iter(())

        profiler = _TimingCollector(
            self.profile_enabled,
            label=f"iter[{self.split or 'all'}|w{worker_id}|e{self._epoch}]",
        )
        iter_start = time.perf_counter()
        prefix = f"Worker {worker_id}/{num_workers} split={self.split or 'all'} epoch={self._epoch}"
        logger.info("🚀 %s: Starting", prefix)

        con: duckdb.DuckDBPyConnection
        new_connection = False
        with profiler.track("connection_setup"):
            if self.duck_persist_conn and (self._duck_con is not None):
                con = self._duck_con
            else:
                con = duckdb.connect(':memory:')
                new_connection = True

        with profiler.track("apply_duckdb_config"):
            if new_connection:
                try:
                    duck_threads = self.config.get('duck_threads', 8)
                    duck_memory = self.config.get('duck_memory', '16GB')
                    con.execute("PRAGMA enable_object_cache")
                    con.execute(f"SET threads={duck_threads}")
                    con.execute(f"SET memory_limit='{duck_memory}'")
                    con.execute("SET preserve_insertion_order=false")
                    con.execute("SET default_null_order='nulls_first'")
                    temp_dir = self.root.parent / ".duck_tmp"
                    temp_dir.mkdir(exist_ok=True)
                    con.execute(f"SET temp_directory='{temp_dir}'")
                    logger.info(
                        "Worker %d: DuckDB config applied (Mem: %s, Threads: %s)",
                        worker_id,
                        duck_memory,
                        duck_threads,
                    )
                except Exception as e:
                    logger.warning("Worker %d: Failed to apply DuckDB configs: %s", worker_id, e)

        if self.duck_persist_conn and new_connection:
            self._duck_con = con

        shard_glob = str(self.root / "shards" / "**" / "*.parquet")
        try:
            with profiler.track("register_parquet_view"):
                con.execute(
                    f"""
                    CREATE OR REPLACE VIEW pv_data AS
                    SELECT * FROM parquet_scan('{shard_glob}', binary_as_string=true)
                    """
                )
            logger.info("Worker %d: Created pv_data view", worker_id)
        except Exception as e:
            logger.warning("Worker %d: Failed with binary_as_string=true: %s", worker_id, e)
            with profiler.track("register_parquet_view_fallback"):
                con.execute(
                    f"""
                    CREATE OR REPLACE VIEW pv_data AS
                    SELECT * FROM parquet_scan('{shard_glob}')
                    """
                )
            logger.info("Worker %d: Created pv_data view with standard settings", worker_id)

        worker_indices = self._prepare_worker_indices(worker_id, num_workers, profiler=profiler)
        indices_table_name = f"idx_{worker_id}"
        with profiler.track("register_indices_table"):
            con.register(indices_table_name, worker_indices)
        logger.info(
            "Worker %d: Registered %d indices as '%s'",
            worker_id,
            len(worker_indices),
            indices_table_name,
        )

        if getattr(self, "sequence_mode", False):
            feature_exprs_sql = ", ".join(self._seq_feature_exprs)
            tail_cols_sql = f'p."{self.label_col}" AS "{self.label_col}", p.trade_date, p.stock_code'
            select_clause = f"{feature_exprs_sql}, {tail_cols_sql}"
        else:
            all_cols = self.feature_cols + [self.label_col, 'trade_date', 'stock_code']
            select_clause = ", ".join([f'p."{c}"' for c in all_cols])

        order_clause = 'ORDER BY idx.index_id'
        if self.batch_by == "date":
            order_clause = 'ORDER BY p.trade_date, idx.index_id'

        sql = f"""
            SELECT {select_clause}
            FROM pv_data p
            INNER JOIN {indices_table_name} idx ON 
                p.trade_date = idx.trade_date AND 
                p.stock_code = idx.stock_code
            {order_clause}
        """

        if self.duck_materialize:
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
                    with profiler.track("duck_materialize"):
                        con.execute(f"CREATE TEMP TABLE {self._cache_table_name} AS {sql}")
                    logger.info(
                        "Worker %d: Materialized cache table '%s' created",
                        worker_id,
                        self._cache_table_name,
                    )
                    sql = f"SELECT * FROM {self._cache_table_name}"
                except Exception as e:
                    logger.warning("Worker %d: Failed to materialize cache table: %s", worker_id, e)
            else:
                sql = f"SELECT * FROM {self._cache_table_name}"

        logger.info("Worker %d: Starting query execution with chunk_size=%s", worker_id, self.chunk_size)

        buf_feats: List[np.ndarray] = []
        buf_labels: List[np.ndarray] = []
        buf_dates: List[Any] = []
        buf_codes: List[Any] = []
        current_day: Any = None

        def _flush_day_batch():
            if not buf_labels:
                return None
            sample_count = sum(arr.shape[0] for arr in buf_labels)
            if sample_count < self.min_samples_per_batch:
                if current_day is not None:
                    logger.warning(
                        "Worker %d: 跳过交易日 %s，仅 %d 个样本 (<%d)",
                        worker_id,
                        current_day,
                        sample_count,
                        self.min_samples_per_batch,
                    )
                buf_feats.clear()
                buf_labels.clear()
                buf_dates.clear()
                buf_codes.clear()
                return None
            with profiler.track("date_concat"):
                feat_np = np.concatenate(buf_feats, axis=0)
                lab_np = np.concatenate(buf_labels, axis=0).astype(np.float32)
                num_samples = lab_np.shape[0]
                feat_np = feat_np.reshape(num_samples, self.n_base_features, self.feature_lag)
                feat_np = feat_np.transpose(0, 2, 1)
            with profiler.track("date_to_tensor"):
                feats = torch.from_numpy(feat_np).pin_memory()
                labels = torch.from_numpy(lab_np).pin_memory()
            if self.keep_meta:
                yield_feats = feats
                yield_labels = labels
                yield_meta_d = buf_dates.copy()
                yield_meta_c = buf_codes.copy()
                buf_feats.clear()
                buf_labels.clear()
                buf_dates.clear()
                buf_codes.clear()
                return (yield_feats, yield_labels, yield_meta_d, yield_meta_c)
            buf_feats.clear()
            buf_labels.clear()
            buf_dates.clear()
            buf_codes.clear()
            return (feats, labels)

        try:
            with profiler.track("duckdb_execute"):
                reader = con.execute(sql).fetch_record_batch(rows_per_batch=self.chunk_size)
            batch_count = 0
            total_rows = 0
            reader_iter = iter(reader)

            while True:
                with profiler.track("duckdb_fetch_batch"):
                    try:
                        record_batch = next(reader_iter)
                    except StopIteration:
                        record_batch = None
                if record_batch is None:
                    break
                if record_batch.num_rows == 0:
                    continue

                batch_count += 1
                total_rows += record_batch.num_rows

                with profiler.track("arrow_to_numpy"):
                    feat_block = np.stack(
                        [
                            col.to_numpy(zero_copy_only=False)
                            for col in record_batch.select(self.feature_cols).columns
                        ],
                        axis=1,
                        dtype=np.float32,
                    )
                    lab_block = (
                        record_batch.column(self.label_col)
                        .to_numpy(zero_copy_only=False)
                        .astype(np.float32)
                    )
                    date_block = record_batch.column("trade_date").to_numpy(zero_copy_only=False)
                    code_block = record_batch.column("stock_code").to_numpy(zero_copy_only=False)

                if self.batch_by != "date":
                    with profiler.track("chunk_to_tensor"):
                        num_samples = lab_block.shape[0]
                        if num_samples < self.min_samples_per_batch:
                            logger.debug(
                                "Worker %d: 跳过仅有 %d 个样本的小批次 (<%d)",
                                worker_id,
                                num_samples,
                                self.min_samples_per_batch,
                            )
                            continue
                        feat_np = (
                            feat_block.reshape(num_samples, self.n_base_features, self.feature_lag)
                            .transpose(0, 2, 1)
                        )
                        feats = torch.from_numpy(feat_np).pin_memory()
                        labels = torch.from_numpy(lab_block).pin_memory()
                    if self.keep_meta:
                        yield feats, labels, date_block.tolist(), code_block.tolist()
                    else:
                        yield feats, labels
                    continue

                pos = 0
                while pos < len(date_block):
                    day = date_block[pos]
                    if current_day is None:
                        current_day = day
                    if day != current_day:
                        out = _flush_day_batch()
                        if out is not None:
                            yield out
                        current_day = day

                    next_break = pos + 1
                    while next_break < len(date_block) and date_block[next_break] == current_day:
                        next_break += 1

                    with profiler.track("date_buffer_append"):
                        buf_feats.append(feat_block[pos:next_break])
                        buf_labels.append(lab_block[pos:next_break])
                        if self.keep_meta:
                            buf_dates.extend(date_block[pos:next_break].tolist())
                            buf_codes.extend(code_block[pos:next_break].tolist())
                    pos = next_break

                if self._profile_batch_log_every and batch_count % self._profile_batch_log_every == 0:
                    self._log_profile_snapshot(profiler, prefix, batch_count, total_rows, "DEBUG-STEP")

                if batch_count % 20 == 0:
                    logger.debug(
                        "Worker %d: Processed %d batches, %d rows",
                        worker_id,
                        batch_count,
                        total_rows,
                    )

            if self.batch_by == "date":
                out = _flush_day_batch()
                if out is not None:
                    yield out

            iter_total = time.perf_counter() - iter_start
            self._log_profile_snapshot(profiler, prefix, batch_count, total_rows, "DEBUG-FINAL")
            if self.profile_enabled:
                summary = profiler.summary_lines(top_n=self.profile_top_n)
                stats = {
                    "worker_id": worker_id,
                    "epoch": self._epoch,
                    "split": self.split,
                    "batches": batch_count,
                    "rows": total_rows,
                    "iter_time": iter_total,
                    "summary": summary,
                }
                self._profile_history.append(stats)
                self._last_iter_stats = stats
                logger.info(
                    "⏱️ %s: total %.3fs across %d record batches (%d rows)",
                    prefix,
                    iter_total,
                    batch_count,
                    total_rows,
                )
            else:
                logger.info(
                    "Worker %d: Completed. Total: %d batches, %d rows",
                    worker_id,
                    batch_count,
                    total_rows,
                )

        except Exception as e:
            logger.error("Worker %d: Query execution failed: %s", worker_id, e)
            raise

        return iter(())


ParquetPVDataset = ParquetPVDebugDataset

__all__ = ["ParquetPVDebugDataset", "ParquetPVDataset"]
