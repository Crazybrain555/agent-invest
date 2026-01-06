# -*- coding: utf-8 -*-
"""
DuckDB-backed iterable dataset that streams (B, T, F) tensors for the pv6 wide table.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Iterator, List, Mapping, Optional, Sequence, Tuple

import duckdb
import numpy as np
import pyarrow.compute as pc
import torch
from torch.utils.data import IterableDataset, get_worker_info

from .wide_seq_view import create_wide_sequence_view


def _qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


class DuckWideIterableDataset(IterableDataset):
    """IterableDataset that reads sequences from DuckDB views and yields (features, labels)."""

    def __init__(
        self,
        root: str | Path,
        split: str,
        *,
        index_path: Optional[str | Path] = None,
        seq_len: int = 30,
        factors: Optional[Sequence[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        require_label_for_train: bool = True,
        chunk_size: int = 4096,
        duck_threads: int = 8,
        duck_memory: str = "16GB",
        duck_temp_dir: Optional[Path] = None,
        keep_meta: bool = False,
        persist_connection: bool = True,
        respect_split: str | bool = "auto",
    ) -> None:
        super().__init__()
        self.root = Path(root).resolve()
        self.split = str(split)
        self.index_path = Path(index_path) if index_path else None
        self.seq_len = int(seq_len)
        self.date_from = date_from
        self.date_to = date_to
        self.require_label_for_train = bool(require_label_for_train)
        self.chunk_size = max(1, int(chunk_size))
        self.duck_threads = int(duck_threads)
        self.duck_memory = str(duck_memory)
        self.duck_temp_dir = (duck_temp_dir or (self.root / "duck_tmp")).resolve()
        self.keep_meta = bool(keep_meta)
        self.persist_connection = bool(persist_connection)
        self.respect_split = respect_split

        self._requested_factors = (
            list(dict.fromkeys(str(x) for x in factors)) if factors else None
        )
        self._factors: List[str] = []
        self._feature_expr_sql: Optional[str] = None
        self._feature_cols: List[str] = []
        self._n_base_features: int = 0
        self._label_col: Optional[str] = None

        self._con: Optional[duckdb.DuckDBPyConnection] = None
        self._view_name: Optional[str] = None
        self._view_connection: Optional[duckdb.DuckDBPyConnection] = None
        self._len_batches: Optional[int] = None
        self._n_rows: Optional[int] = None

    # --- connection & view helpers ---------------------------------------
    def _ensure_connection(self) -> duckdb.DuckDBPyConnection:
        if self._con is not None:
            return self._con
        con = duckdb.connect(database=":memory:")
        con.execute("PRAGMA enable_object_cache")
        con.execute(f"SET threads={self.duck_threads}")
        con.execute(f"SET memory_limit='{self.duck_memory}'")
        con.execute("SET preserve_insertion_order=false")
        con.execute("SET default_null_order='nulls_first'")
        self.duck_temp_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory='{self.duck_temp_dir.as_posix()}'")
        self._con = con
        return con

    def _prepare_feature_projection(self) -> None:
        if not self._factors:
            raise ValueError("No factor columns available to build feature projection")
        self._feature_cols = list(self._factors)
        self._feature_expr_sql = ", ".join(_qident(f) for f in self._factors)
        self._n_base_features = len(self._factors)

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_con"] = None
        state["_view_name"] = None
        state["_view_connection"] = None
        state["_len_batches"] = None
        state["_n_rows"] = None
        return state

    def __del__(self):
        con = getattr(self, "_con", None)
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        self._con = None
        self._view_connection = None
        self._len_batches = None
        self._n_rows = None

    def _ensure_view(self, con: duckdb.DuckDBPyConnection) -> None:
        if self._view_name is not None and self._view_connection is con:
            if self._len_batches is None:
                try:
                    self._n_rows = int(con.execute("SELECT COUNT(*) FROM idx_v").fetchone()[0] or 0)
                except duckdb.Error:
                    self._n_rows = 0
                self._len_batches = (
                    (self._n_rows + self.chunk_size - 1) // self.chunk_size
                    if self._n_rows
                    else 0
                )
            return

        worker = get_worker_info()
        shard_mod = worker.num_workers if worker is not None and worker.num_workers > 1 else None
        shard_rem = worker.id if worker is not None and worker.num_workers > 1 else None

        view, used_factors, label_col = create_wide_sequence_view(
            con,
            self.root,
            index_path=self.index_path,
            lag=self.seq_len,
            split=self.split,
            factors=self._requested_factors,
            date_from=self.date_from,
            date_to=self.date_to,
            require_label_for_train=self.require_label_for_train,
            respect_split=self.respect_split,
            shard_mod=shard_mod,
            shard_rem=shard_rem,
        )
        self._view_name = view
        self._factors = list(used_factors)
        self._label_col = label_col
        self._prepare_feature_projection()
        self._view_connection = con
        try:
            self._n_rows = int(con.execute("SELECT COUNT(*) FROM idx_v").fetchone()[0] or 0)
        except duckdb.Error:
            self._n_rows = 0
        self._len_batches = (
            (self._n_rows + self.chunk_size - 1) // self.chunk_size if self._n_rows else 0
        )

    # --- IterableDataset interface ---------------------------------------
    def __iter__(self) -> Iterator[Tuple[torch.Tensor, torch.Tensor]]:
        con = self._ensure_connection()
        self._ensure_view(con)
        assert self._feature_expr_sql is not None

        select_tail = "label, trade_date, stock_code, index_id"
        sql = f"""
            SELECT {self._feature_expr_sql}, {select_tail}
            FROM {self._view_name}
        """

        reader = con.execute(sql).fetch_record_batch(rows_per_batch=self.chunk_size)
        for rb in reader:
            if rb.num_rows == 0:
                continue
            schema = rb.schema

            id_idx = schema.get_field_index("index_id")
            order = None
            if id_idx != -1:
                ids = rb.column(id_idx).to_numpy(zero_copy_only=False)
                order = np.argsort(ids, kind="stable")

            y_idx = schema.get_field_index("label")
            if y_idx == -1:
                raise KeyError("label column missing in RecordBatch")
            y = rb.column(y_idx).to_numpy(zero_copy_only=False).astype(np.float32, copy=False)

            B = rb.num_rows
            F = self._n_base_features
            T = self.seq_len
            feats_np = np.empty((B, T, F), dtype=np.float32)

            for j, col_name in enumerate(self._feature_cols):
                col_idx = schema.get_field_index(col_name)
                if col_idx == -1:
                    raise KeyError(f"feature list column {col_name} missing in RecordBatch")
                list_col = rb.column(col_idx)
                flat = pc.list_flatten(list_col)
                flat = pc.fill_null(flat, 0.0)
                vals = flat.to_numpy(zero_copy_only=False).astype(np.float32, copy=False)
                if vals.size != B * T:
                    raise ValueError(
                        f"Unexpected flattened size for {col_name}: got {vals.size}, expected {B*T}"
                    )
                feats_np[:, :, j] = vals.reshape(B, T)

            if order is not None:
                feats_np = feats_np[order]
                y = y[order]

            feats = torch.from_numpy(feats_np).pin_memory()
            labels = torch.from_numpy(y.copy()).pin_memory()

            if not self.keep_meta:
                yield feats, labels
                continue

            dt_idx = schema.get_field_index("trade_date")
            sc_idx = schema.get_field_index("stock_code")
            if dt_idx == -1 or sc_idx == -1:
                raise KeyError("trade_date or stock_code missing in RecordBatch")
            dates = rb.column(dt_idx).to_numpy(zero_copy_only=False)
            codes = rb.column(sc_idx).to_numpy(zero_copy_only=False)
            if order is not None:
                dates = dates[order]
                codes = codes[order]
            yield feats, labels, dates.tolist(), codes.tolist()

    def __len__(self) -> int:
        if self._len_batches is not None:
            return self._len_batches
        con = self._ensure_connection()
        self._ensure_view(con)
        return int(self._len_batches or 0)

    @property
    def feature_names(self) -> List[str]:
        return list(self._factors)

    @property
    def n_base_features(self) -> int:
        return self._n_base_features

    @property
    def label_column(self) -> Optional[str]:
        return self._label_col


def build_duckwide_dataset(
    split: str,
    config: Mapping[str, Any],
    *,
    keep_meta: bool = False,
) -> DuckWideIterableDataset:
    root = config["dataset_path"]
    seq_len = int(config.get("seq_len", config.get("feature_lag", 30)))

    use_custom_splits = config.get("use_custom_splits", False)
    respect_split = False if use_custom_splits else "auto"

    date_from = None
    date_to = None
    if use_custom_splits:
        date_ranges = config.get("date_ranges", {})
        if split in date_ranges:
            date_from, date_to = date_ranges[split]
    else:
        date_from = config.get("date_from")
        date_to = config.get("date_to")

    return DuckWideIterableDataset(
        root=root,
        split=split,
        index_path=config.get("index_path"),
        seq_len=seq_len,
        factors=config.get("factors") or config.get("selected_factors"),
        date_from=date_from,
        date_to=date_to,
        require_label_for_train=config.get("require_label_for_train", True),
        chunk_size=int(config.get("chunk_size", 4096)),
        duck_threads=int(config.get("duck_threads", 8)),
        duck_memory=str(config.get("duck_memory", "16GB")),
        duck_temp_dir=Path(config.get("duck_temp_dir", Path(root) / "duck_tmp")),
        keep_meta=keep_meta,
        persist_connection=bool(config.get("persist_connection", True)),
        respect_split=respect_split,
    )


def duckwide_worker_init_fn(worker_id: int) -> None:
    base_seed = torch.initial_seed() % 2**32
    np.random.seed(base_seed + worker_id)
    random.seed(base_seed + worker_id)
