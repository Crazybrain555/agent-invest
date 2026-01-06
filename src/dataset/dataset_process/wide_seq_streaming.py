# -*- coding: utf-8 -*-
"""
Streaming IterableDataset backed by DuckDB + per-stock mirror ring buffers.

Key ideas
---------
* DuckDB only filters daily rows; no SQL window functions.
* Maintain a mirror ring buffer of shape (N, 2T, F) for N stocks, seq len T.
  Each write populates position p and p+T so the latest sequence is a single
  contiguous slice -> vectorised gather via np.take_along_axis.
* Assemble whole batches in NumPy; a single copy to torch tensors keeps CPU
  overhead minimal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import duckdb
import numpy as np
import torch
from torch.utils.data import IterableDataset, get_worker_info
import pyarrow as pa
import glob

from src.utils.path_helpers import normalize_storage_path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_posix(path: Path | str) -> str:
    return Path(path).resolve().as_posix()


def _load_schema(root: Path) -> dict:
    schema_path = root / "meta" / "schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.json not found at {schema_path}")
    import json

    return json.loads(schema_path.read_text(encoding="utf-8-sig"))


def _pick_factors(schema: dict, factors: Optional[Sequence[str]]) -> List[str]:
    if factors:
        return list(dict.fromkeys(str(x) for x in factors))
    candidates = schema.get("expanded_factor_names") or schema.get("factor_names") or []
    return list(dict.fromkeys(str(x) for x in candidates))


def _resolve_index_path(
    schema: dict,
    lag: int,
    split: Optional[str],
    require_label_for_train: bool,
    dataset_root: Optional[Path] = None,
) -> Path:
    entries = schema.get("indices", [])
    for item in entries:
        try:
            if int(item.get("lag")) != int(lag):
                continue
        except Exception:
            continue

        per_split = item.get("per_split") or {}
        if split:
            if split in per_split:
                order: List[str] = []
                if split == "train" and require_label_for_train:
                    order.append("train")
                else:
                    order.extend(["infer", "train"])
                order.extend(k for k in per_split[split].keys() if k not in order)
                for key in order:
                    candidate = per_split[split].get(key)
                    if candidate:
                        return normalize_storage_path(candidate, base_dir=dataset_root)
        else:
            # split 未指定时直接使用全量索引
            path_all = item.get("path_all")
            if path_all:
                return normalize_storage_path(path_all, base_dir=dataset_root)

        path_all = item.get("path_all")
        if path_all:
            return normalize_storage_path(path_all, base_dir=dataset_root)

    raise FileNotFoundError(f"schema.indices 未找到 lag={lag} 的索引信息")


class DuckWideSlidingDataset(IterableDataset):
    """
    IterableDataset streaming daily rows into mirror ring buffers, yielding (B,T,F).
    """

    def __init__(
        self,
        root: str | Path,
        split: Optional[str],
        *,
        seq_len: int,
        factors: Sequence[str],
        chunk_size: int = 2048,
        require_label_for_train: bool = True,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        duck_threads: int = 8,
        duck_memory: str = "8GB",
        duck_temp_dir: Optional[Path] = None,
        keep_meta: bool = False,
        days_per_fetch: int = 10,
        part_pad: str = "auto",
    ) -> None:
        super().__init__()
        self.root = Path(root).resolve()
        self.split = str(split) if split is not None else None
        self.T = int(seq_len)
        self.factors = list(factors)
        self.F = len(self.factors)
        self.chunk_size = max(1, int(chunk_size))
        self.require_label_for_train = bool(require_label_for_train)
        self.date_from = date_from
        self.date_to = date_to

        self.duck_threads = int(duck_threads)
        self.duck_memory = str(duck_memory)
        self.duck_temp_dir = (duck_temp_dir or (self.root / "duck_tmp")).resolve()
        self.keep_meta = bool(keep_meta)
        self.days_per_fetch = max(1, int(days_per_fetch))
        self.part_pad = str(part_pad).lower()
        if self.part_pad not in ("auto", "padded", "unpadded"):
            raise ValueError("part_pad must be 'auto', 'padded', or 'unpadded'")

        # Runtime state (not pickled)
        self._con: Optional[duckdb.DuckDBPyConnection] = None
        self._prepared = False

        # Metadata populated during prepare
        self.codes: List[str] = []
        self.code2i: Dict[str, int] = {}
        self.num_codes: int = 0
        self.days_all: List[str] = []
        self.day_to_codes_map: Dict[str, List[str]] = {}
        self.first_index_day: Optional[str] = None
        self.label_column: Optional[str] = None

        self._n_rows: Optional[int] = None

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_con"] = None
        state["_prepared"] = False
        return state

    def __del__(self):
        con = getattr(self, "_con", None)
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        self._con = None

    def _ensure_con(self) -> duckdb.DuckDBPyConnection:
        if self._con is not None:
            return self._con
        con = duckdb.connect(database=":memory:")
        con.execute("PRAGMA enable_object_cache")
        con.execute(f"SET threads={self.duck_threads}")
        con.execute(f"SET memory_limit='{self.duck_memory}'")
        con.execute("SET preserve_insertion_order=false")
        con.execute("SET default_null_order='nulls_first'")
        self.duck_temp_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory='{_as_posix(self.duck_temp_dir)}'")
        self._con = con
        return con

    def _prepare(self, con: duckdb.DuckDBPyConnection) -> None:
        if self._prepared:
            return

        schema = _load_schema(self.root)
        label_col = str(schema.get("label_col"))
        if not label_col:
            raise ValueError("schema.json 缺少 label_col")
        self.label_column = label_col

        used_factors = _pick_factors(schema, self.factors)
        if not used_factors:
            raise ValueError("未找到可用因子列")
        self.factors = used_factors
        self.F = len(self.factors)
        self._factor_cols_sql = ", ".join(f'"{f}"' for f in self.factors)

        index_path = _resolve_index_path(
            schema,
            self.T,
            self.split,
            self.require_label_for_train,
            self.root,
        )
        if not index_path.exists():
            raise FileNotFoundError(f"index parquet not found: {index_path}")

        preview = con.execute(
            f"SELECT * FROM read_parquet('{_as_posix(index_path)}') LIMIT 0"
        ).fetch_arrow_table()
        idx_cols = set(preview.column_names)
        has_split = "split" in idx_cols
        has_label_flag = "has_label" in idx_cols

        where_parts: List[str] = ["ok_factors=1"]
        if has_split and self.split is not None:
            where_parts.append(f"split = '{self.split}'")
        if self.date_from:
            where_parts.append(f"trade_date >= '{self.date_from}'")
        if self.date_to:
            where_parts.append(f"trade_date <= '{self.date_to}'")
        if self.require_label_for_train and self.split == "train" and has_label_flag:
            where_parts.append("has_label = 1")
        where_sql = " AND ".join(where_parts)

        con.execute(
            f"""
            CREATE OR REPLACE VIEW idx_v AS
            SELECT trade_date, stock_code
            FROM read_parquet('{_as_posix(index_path)}')
            WHERE {where_sql}
            """
        )

        codes_tbl = con.execute("SELECT DISTINCT stock_code FROM idx_v").fetch_arrow_table()
        self.codes = [str(c.as_py()) for c in codes_tbl["stock_code"]]
        self.num_codes = len(self.codes)
        
        # Register in-memory codes_map for SQL JOIN (stock_code -> code_id)
        # This eliminates Python dict lookup overhead
        codes_map_table = pa.Table.from_arrays(
            [pa.array(self.codes), pa.array(np.arange(self.num_codes, dtype=np.int32))],
            names=["stock_code", "code_id"]
        )
        con.register("codes_map", codes_map_table)

        self._n_rows = int(con.execute("SELECT COUNT(*) FROM idx_v").fetchone()[0] or 0)

        miny, maxy = con.execute(
            """
            SELECT MIN(CAST(substr(trade_date,1,4) AS INTEGER)),
                   MAX(CAST(substr(trade_date,1,4) AS INTEGER))
            FROM idx_v
            """
        ).fetchone()

        if miny is None or maxy is None:
            self.days_all = []
            self.day_to_codes_map = {}
            self.day_sql = None
            self.first_index_day = None
            self._prepared = True
            return

        warmup_years = max(1, int((self.T + 219) // 220))
        y_start = max(int(miny) - warmup_years, 1900)
        y_end = int(maxy)

        wide_glob = (self.root / "shards" / "wide_daily" / "**" / "*.parquet").as_posix()
        factor_cols = ", ".join(f'"{f}"' for f in self.factors)

        con.execute(
            f"""
            CREATE OR REPLACE VIEW wide_src AS
            SELECT
                trade_date,
                stock_code,
                "{label_col}" AS label,
                {factor_cols}
            FROM read_parquet('{wide_glob}', hive_partitioning=1, union_by_name=1)
            WHERE CAST(year AS INTEGER) BETWEEN {y_start} AND {y_end}
              AND stock_code IN (SELECT stock_code FROM idx_v)
            """
        )

        days_tbl = con.execute(
            "SELECT DISTINCT trade_date FROM idx_v ORDER BY trade_date"
        ).fetch_arrow_table()
        self.days_all = [str(d.as_py()) for d in days_tbl["trade_date"]]

        byday_tbl = con.execute(
            """
            SELECT trade_date, list(stock_code) AS codes
            FROM idx_v
            GROUP BY trade_date
            ORDER BY trade_date
            """
        ).fetch_arrow_table()
        day_keys = [str(d.as_py()) for d in byday_tbl["trade_date"]]
        codes_by_day = [[str(c.as_py()) for c in arr] for arr in byday_tbl["codes"]]
        self.day_to_codes_map = dict(zip(day_keys, codes_by_day))
        self.first_index_day = min(self.day_to_codes_map.keys()) if self.day_to_codes_map else None
        self.day_sql = None  # not used in streaming mode
        self._wide_base = (self.root / "shards" / "wide_daily").resolve()

        self._prepared = True

    # ------------------------------------------------------------------
    # IterableDataset interface
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[Tuple[torch.Tensor, torch.Tensor]]:
        worker = get_worker_info()
        if worker is not None:
            raise RuntimeError("DuckWideSlidingDataset 目前仅支持 num_workers=0（单进程）模式。")

        con = self._ensure_con()
        self._prepared = False
        self._prepare(con)
        if not self.days_all:
            return

        N = self.num_codes
        T = self.T
        F = self.F
        # Use Torch tensors for time indexing (int64 required for gather)
        time_idx_t = torch.arange(T, dtype=torch.int64)
        base_dir = self._wide_base.as_posix()
        label_col = self.label_column or "label"
        factor_cols_sql = self._factor_cols_sql

        def files_for_day(day: str) -> List[str]:
            year = day[0:4]
            month_i = int(day[4:6])
            day_i = int(day[6:8])
            unpadded = f"{base_dir}/year={year}/month={month_i}/day={day_i}/*.parquet"
            padded = f"{base_dir}/year={year}/month={month_i:02d}/day={day_i:02d}/*.parquet"
            patterns: List[str]
            if self.part_pad == "auto":
                patterns = [unpadded, padded]
            elif self.part_pad == "unpadded":
                patterns = [unpadded]
            else:
                patterns = [padded]
            files: List[str] = []
            for pat in patterns:
                files.extend(glob.glob(pat))
                if files:
                    break
            return [Path(p).as_posix() for p in files]

        # Use Torch CPU pinned tensors to avoid NumPy→Torch copy overhead
        ring2_t = torch.zeros((N, 2 * T, F), dtype=torch.float32, pin_memory=True)
        pos_t = torch.full((N,), T - 1, dtype=torch.int32)  # int32 is fine for position tracking

        Bcap = self.chunk_size
        buf_X_t = torch.empty((Bcap, T, F), dtype=torch.float32, pin_memory=True)
        buf_y_t = torch.empty((Bcap,), dtype=torch.float32, pin_memory=True)
        meta_dates: List[str] = []
        meta_codes: List[str] = []
        bcur = 0

        days = self.days_all

        # Process day-by-day without ORDER BY for better performance
        for day in days:
            files = files_for_day(day)
            if not files:
                continue
            
            files_sql = ", ".join("'" + f.replace("'", "''") + "'" for f in files)
            # JOIN codes_map to get code_id directly, avoiding Python dict lookup
            select_cols = (
                f'ws.stock_code, "{label_col}" AS label'
                + (", " + factor_cols_sql if factor_cols_sql else "")
            )
            # No ORDER BY - we process day-by-day, order within day doesn't matter
            sql = (
                f"SELECT {select_cols}, cm.code_id "
                f"FROM read_parquet([{files_sql}], union_by_name=1) AS ws "
                f"JOIN codes_map AS cm ON ws.stock_code = cm.stock_code"
            )

            for rb in con.execute(sql).fetch_record_batch():
                if rb.num_rows == 0:
                    continue
                schema_rb = rb.schema
                
                # Extract code_id (from JOIN), stock_code, label
                code_idx = schema_rb.get_field_index("code_id")
                sc_idx = schema_rb.get_field_index("stock_code")
                lb_idx = schema_rb.get_field_index("label")
                
                # Convert to numpy first, then to torch
                # Arrow arrays are read-only, use copy=True to make them writable
                ids_np = rb.column(code_idx).to_numpy(zero_copy_only=False).astype(np.int64, copy=True)
                codes_all = np.array([str(v) for v in rb.column(sc_idx).to_pylist()], dtype=object)
                labels_np = rb.column(lb_idx).to_numpy(zero_copy_only=False).astype(np.float32, copy=True)
                
                # Extract factors and convert to torch tensors
                feat_cols = []
                for f in self.factors:
                    idx = schema_rb.get_field_index(f)
                    if idx == -1:
                        raise KeyError(f"Missing factor column {f} in wide_src result")
                    arr = rb.column(idx).to_numpy(zero_copy_only=False).astype(np.float32, copy=True)
                    # Arrow arrays are read-only, need copy=True for nan_to_num
                    arr_clean = np.nan_to_num(arr, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
                    feat_cols.append(torch.from_numpy(arr_clean))
                
                feats_t = torch.stack(feat_cols, dim=1).contiguous()
                ids_t = torch.from_numpy(ids_np)
                labels_t = torch.from_numpy(labels_np)
                
                if ids_t.size(0) == 0:
                    continue
                
                # Update ring buffer using Torch operations
                # newpos = (pos[ids] + 1) % T
                newpos_t = (pos_t.index_select(0, ids_t) + 1) % T
                
                # Update ring buffer at position and mirror position (for contiguous slicing)
                # Use advanced indexing for better performance
                ids_list = ids_t.tolist()
                pos_list = newpos_t.tolist()
                for i, (stock_id, pos_new) in enumerate(zip(ids_list, pos_list)):
                    ring2_t[stock_id, pos_new, :] = feats_t[i]
                    ring2_t[stock_id, pos_new + T, :] = feats_t[i]
                
                # Update position tracker
                pos_t.index_copy_(0, ids_t, newpos_t)
                
                # Get stocks to process for this day (from index)
                codes_today_raw = self.day_to_codes_map.get(day)
                if not codes_today_raw:
                    continue
                
                # Build label map for this batch
                label_per_id = torch.full((N,), float('nan'), dtype=torch.float32)
                label_per_id[ids_t] = labels_t
                
                # Get code_ids for today's stocks (from day_to_codes_map)
                codes_today_list = [str(code) for code in codes_today_raw]
                # Map to code_ids using our registered codes_map
                ids_today_list = []
                for code in codes_today_list:
                    try:
                        idx = self.codes.index(code)
                        ids_today_list.append(idx)
                    except ValueError:
                        continue
                
                if not ids_today_list:
                    continue
                
                ids_today_t = torch.tensor(ids_today_list, dtype=torch.int64)
                labels_today_t = label_per_id[ids_today_t]
                
                # Filter out NaN labels
                keep_label = ~torch.isnan(labels_today_t)
                if not keep_label.any():
                    continue
                    
                ids_today_t = ids_today_t[keep_label]
                labels_today_t = labels_today_t[keep_label]
                codes_today_arr = [codes_today_list[i] for i, k in enumerate(keep_label.tolist()) if k]
                
                # Gather sequences using Torch operations
                # starts_idx = (pos[ids_today] + 1) % (2*T), then slice [start:start+T]
                starts_idx_t = (pos_t.index_select(0, ids_today_t) + 1).to(torch.int64)
                
                # Build gather indices: [B, T] where each row is [start, start+1, ..., start+T-1]
                # gather() requires int64 indices
                gather_idx_t = (starts_idx_t.view(-1, 1, 1) + time_idx_t.view(1, -1, 1)).expand(-1, T, F)
                
                # Gather from ring buffer: X_block[i,j,k] = ring2[ids_today[i], gather_idx[i,j,k], k]
                X_block_t = ring2_t.index_select(0, ids_today_t).gather(1, gather_idx_t)
                
                # Fill batch buffer using Torch operations
                m = X_block_t.shape[0]
                off = 0
                while off < m:
                    take = min(Bcap - bcur, m - off)
                    buf_X_t[bcur : bcur + take].copy_(X_block_t[off : off + take], non_blocking=False)
                    buf_y_t[bcur : bcur + take].copy_(labels_today_t[off : off + take], non_blocking=False)
                    if self.keep_meta:
                        meta_dates.extend([day] * take)
                        meta_codes.extend(codes_today_arr[off : off + take])
                    bcur += take
                    off += take
                    
                    # Yield full batch (already pinned, no copy needed)
                    if bcur >= Bcap:
                        if self.keep_meta:
                            yield buf_X_t[:bcur], buf_y_t[:bcur], list(meta_dates), list(meta_codes)
                            meta_dates.clear()
                            meta_codes.clear()
                        else:
                            yield buf_X_t[:bcur], buf_y_t[:bcur]
                        bcur = 0

        # Yield remaining batch
        if bcur > 0:
            if self.keep_meta:
                yield buf_X_t[:bcur], buf_y_t[:bcur], list(meta_dates), list(meta_codes)
            else:
                yield buf_X_t[:bcur], buf_y_t[:bcur]
    def __len__(self) -> int:
        if self._n_rows is not None:
            n = int(self._n_rows)
        else:
            con = self._ensure_con()
            self._prepared = False
            self._prepare(con)
            n = int(self._n_rows or 0)
        if n == 0:
            return 0
        return (n + self.chunk_size - 1) // self.chunk_size


def build_duckwide_streaming_dataset(
    split: Optional[str],
    config: Mapping[str, Any],
    *,
    keep_meta: bool = False,
) -> DuckWideSlidingDataset:
    root = config["dataset_path"]
    seq_len = int(config.get("seq_len") or config.get("feature_lag", 30))

    factors = config.get("factors") or config.get("selected_factors")
    if not factors:
        raise ValueError("请在 config 中提供 factors 或 selected_factors")

    use_custom_splits = config.get("use_custom_splits", False)
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    days_per_fetch = int(config.get("days_per_fetch", 10))
    part_pad = str(config.get("part_pad", "auto"))

    if use_custom_splits:
        date_ranges = config.get("date_ranges", {})
        if split in date_ranges:
            date_from, date_to = date_ranges[split]
    else:
        date_from = config.get("date_from")
        date_to = config.get("date_to")

    # 使用统一的 chunk_size 参数
    chunk_size = int(config.get("chunk_size", 2048))
    
    return DuckWideSlidingDataset(
        root=root,
        split=split,
        seq_len=seq_len,
        factors=factors,
        chunk_size=chunk_size,
        require_label_for_train=bool(config.get("require_label_for_train", True)),
        date_from=date_from,
        date_to=date_to,
        duck_threads=int(config.get("duck_threads", 8)),
        duck_memory=str(config.get("duck_memory", "8GB")),
        duck_temp_dir=Path(config.get("duck_temp_dir", Path(root) / "duck_tmp")),
        keep_meta=keep_meta,
        days_per_fetch=days_per_fetch,
        part_pad=part_pad,
    )
