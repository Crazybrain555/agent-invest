# -*- coding: utf-8 -*-
"""
Utilities for constructing DuckDB views that convert the pv6 wide table into sequence
record batches suitable for the iterable dataset pipeline.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Optional, Sequence, Tuple

import duckdb

from src.utils.path_helpers import normalize_storage_path

logger = logging.getLogger(__name__)

# Log each filter/sample/warning combination once to avoid noisy repetition when workers spin up.
_LOGGED_FILTER_KEYS: set[tuple] = set()
_LOGGED_SAMPLE_KEYS: set[tuple] = set()
_LOGGED_WARNING_KEYS: set[tuple] = set()


def _load_schema(root: Path) -> dict:
    schema_path = root / "meta" / "schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.json not found at {schema_path}")
    return json.loads(schema_path.read_text(encoding="utf-8-sig"))


def _qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _as_posix(p: Path | str) -> str:
    return Path(p).resolve().as_posix()


def _pick_factors(schema: dict, factors: Optional[Sequence[str]]) -> list[str]:
    if factors:
        return list(dict.fromkeys([str(x) for x in factors]))
    candidates = schema.get("expanded_factor_names") or schema.get("factor_names") or []
    return list(dict.fromkeys([str(x) for x in candidates]))


def _resolve_index_path(
    schema: dict,
    lag: int,
    split: str,
    require_label_for_train: bool,
    dataset_root: Path | None = None,
) -> Path:
    """
    Prefer per-split index files that already contain index_id, then fall back to path_all.
    """
    entries = schema.get("indices", [])
    for item in entries:
        try:
            if int(item.get("lag")) != int(lag):
                continue
        except Exception:
            continue
        per_split = item.get("per_split") or {}
        if split in per_split:
            target_order: list[str] = []
            if split == "train" and require_label_for_train:
                target_order.append("train")
            else:
                target_order.append("infer")
                target_order.append("train")
            target_order.extend([k for k in per_split[split].keys() if k not in target_order])
            for key in target_order:
                candidate = per_split[split].get(key)
                if candidate:
                    return normalize_storage_path(candidate, base_dir=dataset_root)
        path_all = item.get("path_all")
        if path_all:
            return normalize_storage_path(path_all, base_dir=dataset_root)
    raise FileNotFoundError(f"schema.indices 未找到 lag={lag} 的索引信息")


def create_wide_sequence_view(
    con: duckdb.DuckDBPyConnection,
    root: str | Path,
    *,
    index_path: Optional[str | Path] = None,
    lag: int = 30,
    split: str = "train",
    factors: Optional[Sequence[str]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    require_label_for_train: bool = True,
    respect_split: str | bool = "auto",
    shard_mod: Optional[int] = None,
    shard_rem: Optional[int] = None,
) -> Tuple[str, list[str], str]:
    """
    Build the DuckDB view that feeds the iterable dataloader.

    shard_mod / shard_rem allow worker-level sharding (index_id % shard_mod == shard_rem).
    """
    root = Path(root).resolve()
    schema = _load_schema(root)
    label_col = str(schema.get("label_col"))
    if not label_col:
        raise ValueError("schema.json 缺少 label_col")

    used_factors = _pick_factors(schema, factors)
    if not used_factors:
        raise ValueError("没有可用的因子列（factors / expanded_factor_names 为空）")

    if index_path is None:
        index_path = _resolve_index_path(schema, lag, split, require_label_for_train, root)
    else:
        index_path = normalize_storage_path(index_path, base_dir=root)

    index_path = Path(index_path).resolve()
    if not index_path.exists():
        raise FileNotFoundError(f"index parquet not found: {index_path}")

    preview = con.execute(f"SELECT * FROM read_parquet('{_as_posix(index_path)}') LIMIT 0")
    index_cols = [desc[0] for desc in (preview.description or [])]
    has_split_col = "split" in index_cols
    has_ok_flag = "ok_factors" in index_cols
    has_label_flag = "has_label" in index_cols
    has_index_id = "index_id" in index_cols

    if has_split_col:
        distinct_splits = [
            (row[0] if row and row[0] is not None else "unused")
            for row in con.execute(
                f"SELECT DISTINCT split FROM read_parquet('{_as_posix(index_path)}')"
            ).fetchall()
        ]
        norm = {("unused" if (s in (None, "", "unused")) else str(s)) for s in distinct_splits}
        only_unused = (len(norm) == 1 and "unused" in norm)
    else:
        distinct_splits = [split]
        norm = {split}
        only_unused = False

    if respect_split is True:
        apply_split = has_split_col
    elif respect_split is False:
        apply_split = False
    else:
        apply_split = has_split_col and (not only_unused) and (split in norm)

    filter_key = (
        str(index_path),
        apply_split,
        split,
        date_from,
        date_to,
        require_label_for_train,
        tuple(sorted(norm)),
        has_label_flag,
        has_ok_flag,
    )
    if filter_key not in _LOGGED_FILTER_KEYS:
        if apply_split:
            logger.info(
                "索引过滤: 使用 split='%s' + ok_factors + [可选日期] 选样；索引包含: %s",
                split,
                sorted(norm),
            )
        else:
            base = "索引过滤: 不使用 split" + ("，仅 ok_factors + [可选日期]" if has_ok_flag else "，仅 [可选日期]")
            if require_label_for_train and split == "train":
                if has_label_flag:
                    base += " + has_label"
                else:
                    base += "（索引缺少 has_label 列）"
            logger.info("%s；索引包含: %s", base, sorted(norm))
        _LOGGED_FILTER_KEYS.add(filter_key)

    where_parts: list[str] = []
    if has_ok_flag:
        where_parts.append("ok_factors=1")
    if apply_split and has_split_col:
        where_parts.append(f"split = '{split}'")
    if date_from:
        where_parts.append(f"trade_date >= '{date_from}'")
    if date_to:
        where_parts.append(f"trade_date <= '{date_to}'")
    if require_label_for_train and split == "train":
        if has_label_flag:
            where_parts.append("has_label=1")
        else:
            warn_key = (str(index_path), split, date_from, date_to, require_label_for_train)
            if warn_key not in _LOGGED_WARNING_KEYS:
                logger.warning("索引缺少 has_label 列，无法在训练 split 上过滤 has_label=1")
                _LOGGED_WARNING_KEYS.add(warn_key)

    where_sql = " AND ".join(where_parts) if where_parts else "1=1"
    idx_select_cols = ["trade_date", "stock_code"]
    if has_index_id:
        idx_select_cols.append("index_id")

    shard_pred = ""
    if shard_mod is not None and shard_rem is not None:
        if has_index_id:
            shard_pred = f" AND ((index_id %% {int(shard_mod)}) = {int(shard_rem)})"
        else:
            warn_key = ("no_index_id_for_shard", str(index_path))
            if warn_key not in _LOGGED_WARNING_KEYS:
                logger.warning("索引缺少 index_id，无法按 worker 分片，将忽略 shard 参数")
                _LOGGED_WARNING_KEYS.add(warn_key)

    con.execute(
        f"""
        CREATE OR REPLACE VIEW idx_v AS
        SELECT {', '.join(idx_select_cols)}
        FROM read_parquet('{_as_posix(index_path)}')
        WHERE {where_sql}{shard_pred}
        """
    )

    n_idx = con.execute("SELECT COUNT(*) FROM idx_v").fetchone()[0] or 0
    sample_key = (
        str(index_path),
        split,
        date_from,
        date_to,
        require_label_for_train,
        shard_mod,
        shard_rem,
        n_idx,
    )
    if sample_key not in _LOGGED_SAMPLE_KEYS:
        logger.info(f"idx_v 样本数: {n_idx:,}")
        _LOGGED_SAMPLE_KEYS.add(sample_key)

    if n_idx == 0:
        empty_name = f"wv_{uuid.uuid4().hex[:8]}"
        con.execute(f"CREATE OR REPLACE VIEW {empty_name} AS SELECT * FROM (SELECT 1) WHERE 1=0")
        return empty_name, used_factors, label_col

    min_max_row = con.execute(
        "SELECT MIN(CAST(substr(trade_date, 1, 4) AS INTEGER)), "
        "MAX(CAST(substr(trade_date, 1, 4) AS INTEGER)) FROM idx_v"
    ).fetchone()
    miny, maxy = min_max_row if min_max_row else (None, None)

    if miny is None or maxy is None:
        empty_name = f"wv_{uuid.uuid4().hex[:8]}"
        logger.warning("索引视图 idx_v 为空（split=%s, apply_split=%s），返回空序列视图", split, apply_split)
        con.execute(f"CREATE OR REPLACE VIEW {empty_name} AS SELECT * FROM (SELECT 1) WHERE 1=0")
        return empty_name, used_factors, label_col

    warmup_years = max(1, int((lag + 219) // 220))
    y_start = max(miny - warmup_years, 1900)
    y_end = maxy

    con.execute(
        """
        CREATE OR REPLACE VIEW idx_codes AS
        SELECT DISTINCT stock_code FROM idx_v
        """
    )

    wide_glob = (root / "shards" / "wide_daily" / "**" / "*.parquet").as_posix()
    factor_cols = ", " + ", ".join(_qident(c) for c in used_factors) if used_factors else ""
    con.execute(
        f"""
        CREATE OR REPLACE VIEW wide_src AS
        SELECT
            trade_date,
            CAST(trade_date AS BIGINT) AS tdi,
            stock_code,
            CAST(year AS INTEGER) AS year,
            CAST(month AS VARCHAR) AS month,
            CAST(day AS VARCHAR) AS day,
            {_qident(label_col)} AS __label__
            {factor_cols}
        FROM read_parquet('{wide_glob}', hive_partitioning=1, union_by_name=1)
        WHERE CAST(year AS INTEGER) BETWEEN {y_start} AND {y_end}
          AND stock_code IN (SELECT stock_code FROM idx_codes)
        """
    )

    list_exprs = [
        f'LIST(CAST({_qident(f)} AS DOUBLE)) OVER (PARTITION BY stock_code ORDER BY tdi '
        f'ROWS BETWEEN {lag - 1} PRECEDING AND CURRENT ROW) AS {_qident(f)}'
        for f in used_factors
    ]
    list_sql = ",\n       ".join(list_exprs)

    # Detect list_transform availability (DuckDB 1.3+).
    try:
        con.execute("SELECT list_transform([1, NULL], lambda x: coalesce(x, 0))")
        has_list_transform = True
    except duckdb.Error:
        has_list_transform = False

    if has_list_transform:
        transform_sql = ",\n            ".join(
            [
                f"""list_reverse(
                    list_resize(
                        list_reverse(
                            list_transform({_qident(f)}, lambda x: coalesce(x, 0.0))
                        ),
                        {lag}, 0.0
                    )
                ) AS {_qident(f)}"""
                for f in used_factors
            ]
        )
    else:
        # Fallback: rely on downstream fill_null; still ensure list length。
        transform_sql = ",\n            ".join(
            [
                f"""list_reverse(
                    list_resize(
                        list_reverse({_qident(f)}),
                        {lag}, 0.0
                    )
                ) AS {_qident(f)}"""
                for f in used_factors
            ]
        )

    index_select_sql = (
        "i.index_id AS index_id" if has_index_id else "CAST(NULL AS BIGINT) AS index_id"
    )
    order_clause = "ORDER BY index_id" if has_index_id else "ORDER BY trade_date, stock_code"

    view_name = f"wv_{uuid.uuid4().hex[:8]}"
    con.execute(
        f"""
        CREATE OR REPLACE VIEW {view_name} AS
        WITH win AS (
            SELECT
                trade_date,
                stock_code,
                __label__ AS label,
                {list_sql}
            FROM wide_src
        )
        SELECT
            trade_date,
            stock_code,
            label,
            {transform_sql},
            {index_select_sql}
        FROM win
        JOIN idx_v AS i USING (trade_date, stock_code)
        {order_clause}
        """
    )
    return view_name, used_factors, label_col
