# -*- coding: utf-8 -*-
"""
统一索引生成模块 - 替代 ok_keys
功能：
  1. 特征完整性筛选（基于滑窗非空计数）
  2. 标签可用性标记（has_label）
  3. 产出训练/推理专用索引（含 index_id）
  4. 支持 wide_daily（主要）和 features_long（回退）
"""
from __future__ import annotations

import gc
import json
import logging
import math
import shutil
import time
from contextlib import suppress
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import duckdb
from tqdm import tqdm

LOGGER = logging.getLogger(__name__)


# ============================================================================
# 辅助函数（复用自 ok_keys.py）
# ============================================================================

def _robust_unlink(path: Path, retries: int = 6, base_delay: float = 0.2) -> None:
    """Windows 文件锁容忍的删除"""
    if not path.exists():
        return
    for attempt in range(retries):
        try:
            path.unlink()
            return
        except PermissionError:
            gc.collect()
            time.sleep(base_delay * (2**attempt))
        except FileNotFoundError:
            return
    with suppress(Exception):
        path.unlink()


def _load_schema(dataset_root: Path) -> dict:
    schema_path = dataset_root / "meta" / "schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.json not found at {schema_path}")
    with schema_path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def _resolve_factors(schema: dict, spec: str | Sequence[str]) -> List[str]:
    """解析因子列表：auto | list | file | csv"""
    if spec == "auto":
        factors = schema.get("expanded_factor_names") or []
        if not factors:
            raise ValueError("schema.json missing 'expanded_factor_names'")
        return sorted(set(factors))
    
    if isinstance(spec, (list, tuple)):
        factors = [x.strip() for x in spec if x and x.strip()]
        if not factors:
            raise ValueError("factor list is empty")
        return sorted(set(factors))
    
    path_obj = Path(str(spec))
    if path_obj.exists():
        factors = [ln.strip() for ln in path_obj.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not factors:
            raise ValueError(f"factor file {spec} is empty")
        return sorted(set(factors))
    
    factors = [x.strip() for x in str(spec).split(",") if x.strip()]
    if not factors:
        raise ValueError("factor spec is empty")
    return sorted(set(factors))


def _quote_ident(name: str) -> str:
    """DuckDB 标识符引用"""
    return '"' + name.replace('"', '""') + '"'


def _quote(items: Iterable[str]) -> str:
    """DuckDB 字符串列表引用"""
    return ", ".join("'" + i.replace("'", "''") + "'" for i in items)


def _chunks(seq: Sequence[str], size: int) -> Iterable[List[str]]:
    """Chunk a sequence into fixed-size batches"""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    length = len(seq)
    for start in range(0, length, size):
        yield list(seq[start : start + size])


def _configure_connection(
    con: duckdb.DuckDBPyConnection,
    threads: int | None,
    temp_dir: Path | None,
    memory_limit: str | None,
    *,
    show_progress: bool = False,
) -> None:
    """配置 DuckDB 连接"""
    if threads:
        con.execute(f"SET threads={threads}")
    if temp_dir:
        con.execute(f"SET temp_directory='{temp_dir.as_posix()}'")
    if memory_limit:
        con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute("SET parquet_metadata_cache=true")
    con.execute("SET enable_external_file_cache=true")
    con.execute("SET preserve_insertion_order=false")
    if show_progress:
        con.execute("PRAGMA enable_progress_bar=1")
        con.execute("PRAGMA progress_bar_time=1000")


def _discover_years(root: Path) -> List[int]:
    """发现 hive 分区年份"""
    years: List[int] = []
    for yd in root.glob("year=*"):
        try:
            years.append(int(yd.name.split("=", 1)[1]))
        except Exception:
            continue
    return sorted(set(years))


def _default_min_non_null(lag: int) -> int:
    """默认阈值：max(10, lag//10)"""
    return max(10, int(lag) // 10)


# ============================================================================
# 核心：计算特征完整性索引（ready_pairs）
# ============================================================================

def _compute_ready_pairs(
    con: duckdb.DuckDBPyConnection,
    dataset_root: Path,
    lag: int,
    factors: Sequence[str],
    out_path: Path,
    *,
    force: bool,
    min_non_null: Optional[int] = None,
) -> bool:
    """Compute (trade_date, stock_code) pairs that satisfy factor availability."""
    if out_path.exists() and not force:
        LOGGER.info("Skip lag=%s ready_pairs (exists): %s", lag, out_path.name)
        return False

    wide_root = dataset_root / "shards" / "wide_daily"
    long_root = dataset_root / "shards" / "features_long"

    use_wide = False
    if wide_root.exists() and any(wide_root.glob("year=*")):
        source_root = wide_root
        use_wide = True
    elif long_root.exists() and any(long_root.glob("year=*")):
        source_root = long_root
    else:
        raise RuntimeError("No feature shards found (expected wide_daily or features_long)")

    thr = int(min_non_null) if (min_non_null is not None) else _default_min_non_null(lag)
    if thr > lag:
        LOGGER.warning("min_non_null (%d) > lag (%d); capping to lag", thr, lag)
        thr = lag
    LOGGER.info(
        "Computing ready_pairs lag=%s, min_non_null=%s, source=%s",
        lag,
        thr,
        "wide_daily" if use_wide else "features_long",
    )

    years = _discover_years(source_root)
    if not years:
        raise RuntimeError(f"No year partitions under {source_root}")

    empty_sql = """
    SELECT CAST(NULL AS VARCHAR) AS trade_date,
           CAST(NULL AS VARCHAR) AS stock_code,
           CAST(NULL AS INTEGER) AS year,
           CAST(NULL AS VARCHAR) AS month,
           CAST(NULL AS VARCHAR) AS day
    WHERE 1=0
    """

    tmp_dir = out_path.parent / f"tmp_lag{lag}"
    if tmp_dir.exists() and force:
        with suppress(Exception):
            shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    def _write_empty_ready() -> bool:
        tmp_final = out_path.with_suffix(".tmp.parquet")
        try:
            con.execute(
                f"COPY ({empty_sql}) TO '{tmp_final.as_posix()}' "
                "(FORMAT 'parquet', COMPRESSION 'zstd')"
            )
            tmp_final.replace(out_path)
            return True
        finally:
            _robust_unlink(tmp_final)

    if use_wide:
        factor_list = list(factors)
        if not factor_list:
            raise ValueError("factor list is empty")

        sel_dirs = [
            (source_root / f"year={yy}").as_posix()
            for yy in years
            if (source_root / f"year={yy}").exists()
        ]
        if not sel_dirs:
            LOGGER.warning("No wide_daily parquet shards found; writing empty ready file")
            result = _write_empty_ready()
            with suppress(Exception):
                shutil.rmtree(tmp_dir)
            return result

        dirs_sql = ", ".join(f"'{d}/**/*.parquet'" for d in sel_dirs)
        batch_size = 16
        written_any = False

        try:
            for batch_idx, cols in enumerate(_chunks(factor_list, batch_size)):
                if not cols:
                    continue

                factor_cols_expr = ", " + ", ".join(_quote_ident(c) for c in cols)
                window_exprs = [
                    (
                        f"COUNT({_quote_ident(col)}) OVER ("
                        "PARTITION BY stock_code "
                        "ORDER BY tdi "
                        f"ROWS BETWEEN {lag - 1} PRECEDING AND CURRENT ROW)"
                    )
                    for col in cols
                ]
                if len(window_exprs) == 1:
                    ready_expr = window_exprs[0]
                else:
                    ready_expr = f"GREATEST({', '.join(window_exprs)})"

                sql = f"""
                WITH src AS (
                    SELECT trade_date,
                           CAST(trade_date AS BIGINT) AS tdi,
                           stock_code,
                           CAST(year AS INTEGER) AS year
                           {factor_cols_expr}
                    FROM read_parquet([{dirs_sql}], hive_partitioning=1, union_by_name=1)
                )
                SELECT DISTINCT
                    trade_date,
                    stock_code,
                    year,
                    SUBSTR(CAST(trade_date AS VARCHAR), 5, 2) AS month,
                    SUBSTR(CAST(trade_date AS VARCHAR), 7, 2) AS day
                FROM src
                QUALIFY {ready_expr} >= {thr}
                """

                LOGGER.info(
                    "Writing candidate pairs for lag=%s batch=%s (%s factors)",
                    lag,
                    batch_idx,
                    len(cols),
                )
                out_dir = tmp_dir / f"parts_b{batch_idx}"
                out_dir.mkdir(parents=True, exist_ok=True)
                con.execute(
                    f"COPY ({sql}) TO '{out_dir.as_posix()}' "
                    "(FORMAT 'parquet', PARTITION_BY (year), COMPRESSION 'zstd', OVERWRITE_OR_IGNORE)"
                )
                written_any = True

            if not written_any:
                LOGGER.warning("No ready candidates generated for lag=%s; writing empty file", lag)
                return _write_empty_ready()

            parts_glob = (tmp_dir / "parts_b*" / "year=*" / "*.parquet").as_posix()
            union_sql = f"""
            SELECT DISTINCT
                trade_date,
                stock_code,
                CAST(year AS INTEGER) AS year,
                SUBSTR(CAST(trade_date AS VARCHAR), 5, 2) AS month,
                SUBSTR(CAST(trade_date AS VARCHAR), 7, 2) AS day
            FROM read_parquet('{parts_glob}', hive_partitioning=1, union_by_name=1)
            """
            tmp_final = out_path.with_suffix(".tmp.parquet")
            try:
                con.execute(
                    f"COPY ({union_sql}) TO '{tmp_final.as_posix()}' "
                    "(FORMAT 'parquet', COMPRESSION 'zstd')"
                )
                tmp_final.replace(out_path)
                return True
            finally:
                _robust_unlink(tmp_final)
        finally:
            with suppress(Exception):
                shutil.rmtree(tmp_dir)

    min_year = years[0]
    warmup_years = max(1, int(math.ceil(lag / 220.0)))
    factor_list = _quote(factors)

    for y in tqdm(years, desc=f"ready_pairs lag={lag}"):
        y_start = max(min_year, y - warmup_years)

        sel_dirs = [
            (source_root / f"year={yy}").as_posix()
            for yy in range(y_start, y + 1)
            if (source_root / f"year={yy}").exists()
        ]
        if not sel_dirs:
            continue

        dirs_sql = ", ".join(f"'{d}/**/*.parquet'" for d in sel_dirs)

        sql = f"""
        WITH base AS (
            SELECT trade_date, CAST(trade_date AS BIGINT) AS tdi,
                   stock_code, factor_name, CAST(factor_value AS DOUBLE) AS v,
                   CAST(year AS INTEGER) AS year
            FROM read_parquet([{dirs_sql}], hive_partitioning=1, union_by_name=1)
            WHERE factor_name IN ({factor_list})
              AND CAST(year AS INTEGER) BETWEEN {y_start} AND {y}
        ),
        nonnull AS (
            SELECT trade_date, tdi, stock_code, factor_name, year
            FROM base WHERE v IS NOT NULL
        ),
        counts AS (
            SELECT trade_date, tdi, stock_code, factor_name, year,
                   COUNT(*) OVER (
                       PARTITION BY stock_code, factor_name
                       ORDER BY tdi
                       ROWS BETWEEN {lag - 1} PRECEDING AND CURRENT ROW
                   ) AS nnc
            FROM nonnull
        )
        SELECT DISTINCT
            trade_date, stock_code,
            CAST(year AS INTEGER) AS year,
            SUBSTR(trade_date, 5, 2) AS month,
            SUBSTR(trade_date, 7, 2) AS day
        FROM counts
        WHERE year = {y} AND nnc >= {thr}
        """

        out_y = tmp_dir / f"ready_{y}.parquet"
        tmp_y = out_y.with_suffix(".tmp.parquet")
        if out_y.exists() and not force:
            LOGGER.info("Skip yearly ready: %s", out_y.name)
            continue

        LOGGER.info("Writing yearly ready: %s", out_y.name)
        try:
            con.execute(f"COPY ({sql}) TO '{tmp_y.as_posix()}' (FORMAT 'parquet', COMPRESSION 'zstd')")
            tmp_y.replace(out_y)
        finally:
            _robust_unlink(tmp_y)

    tmp_files = sorted(tmp_dir.glob("ready_*.parquet"))
    if not tmp_files:
        LOGGER.warning("No yearly ready_pairs; writing empty file")
        result = _write_empty_ready()
        with suppress(Exception):
            shutil.rmtree(tmp_dir)
        return result

    tmp_glob = (tmp_dir / "ready_*.parquet").as_posix()
    union_sql = f"SELECT * FROM read_parquet('{tmp_glob}')"
    tmp_final = out_path.with_suffix(".tmp.parquet")
    try:
        con.execute(f"COPY ({union_sql}) TO '{tmp_final.as_posix()}' (FORMAT 'parquet', COMPRESSION 'zstd')")
        tmp_final.replace(out_path)
        return True
    finally:
        _robust_unlink(tmp_final)
        with suppress(Exception):
            shutil.rmtree(tmp_dir)

def _update_schema_indices(dataset_root: Path, entries: List[dict]) -> None:
    """更新 schema.json 的 indices 节点"""
    schema = _load_schema(dataset_root)
    exist = {int(it["lag"]): it for it in schema.get("indices", []) if "lag" in it}
    for e in entries:
        exist[int(e["lag"])] = e
    schema["indices"] = sorted(exist.values(), key=lambda x: int(x["lag"]))
    
    schema_path = dataset_root / "meta" / "schema.json"
    with schema_path.open("w", encoding="utf-8") as fp:
        json.dump(schema, fp, indent=2, ensure_ascii=False)


def generate_indices(
    dataset_root: Path | str,
    *,
    lags: Sequence[int] = (30, 300, 500),
    factors: str | Sequence[str] = "auto",
    threads: Optional[int] = None,
    temp_dir: Optional[Path] = None,
    memory_limit: Optional[str] = None,
    with_splits: bool = True,
    force: bool = False,
    min_non_null: Optional[int] = None,
    require_label_for_train: bool = True,
    show_progress: bool = False,
) -> List[dict]:
    """
    统一索引生成入口（替代 generate_ok_keys）
    
    产出：
      - meta/indices/ready_pairs/ready_lag{lag}.parquet  (内部中间产物)
      - meta/indices/index_lag{lag}.parquet              (全量索引：ok_factors + has_label)
      - meta/indices/{split}_index_lag{lag}_train.parquet (训练索引：ok_factors=1 & has_label=1)
      - meta/indices/{split}_index_lag{lag}_infer.parquet (推理索引：ok_factors=1)
    
    参数：
      lags: 滞后天数列表
      factors: 因子列表（auto | list | file | csv）
      require_label_for_train: 训练索引是否要求 has_label=1（默认 True）
      其他参数：同 ok_keys.py
    
    返回：索引元信息列表（会写入 schema.json）
    """
    root = Path(dataset_root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Dataset root {root} not found")

    schema = _load_schema(root)
    factor_list = _resolve_factors(schema, factors)
    label_col = schema.get("label_col")
    if not label_col:
        raise ValueError("schema.json missing 'label_col'")

    indices_dir = root / "meta" / "indices"
    indices_dir.mkdir(parents=True, exist_ok=True)
    ready_dir = indices_dir / "ready_pairs"
    ready_dir.mkdir(parents=True, exist_ok=True)

    # B1: labels 视图优先从 wide_daily 读取，确保单一真相源
    wide_dir = root / "shards" / "wide_daily"
    labels_dir = root / "shards" / "labels"
    wide_glob = (wide_dir / "**" / "*.parquet").as_posix()
    labels_glob = (labels_dir / "**" / "*.parquet").as_posix()
    splits_path = root / "meta" / "splits.parquet"

    entries: List[dict] = []

    con = duckdb.connect()
    try:
        _configure_connection(con, threads, temp_dir, memory_limit, show_progress=show_progress)

        # 创建 labels_has 视图：优先从 wide_daily，回退到 labels
        label_ident = _quote_ident(str(label_col))
        if wide_dir.exists():
            # 优先：从 wide_daily 直接判定（单一真相源）
            con.execute(f"""
                CREATE OR REPLACE VIEW labels_has AS
                SELECT DISTINCT trade_date, stock_code
                FROM read_parquet('{wide_glob}', hive_partitioning=1, union_by_name=1)
                WHERE {label_ident} IS NOT NULL
            """)
            LOGGER.info("labels_has view created from wide_daily (single source of truth)")
        elif labels_dir.exists():
            # 回退：从 shards/labels
            con.execute(f"""
                CREATE OR REPLACE VIEW labels_has AS
                SELECT DISTINCT trade_date, stock_code
                FROM read_parquet('{labels_glob}', hive_partitioning=1, union_by_name=1)
                WHERE {label_ident} IS NOT NULL
            """)
            LOGGER.info("labels_has view created from shards/labels (fallback)")
        else:
            # 无数据：创建空视图
            con.execute("""
                CREATE OR REPLACE VIEW labels_has AS
                SELECT CAST(NULL AS VARCHAR) AS trade_date, CAST(NULL AS VARCHAR) AS stock_code
                WHERE 1=0
            """)
            LOGGER.warning("No label data found; labels_has view is empty")

        # splits 视图
        have_splits = with_splits and splits_path.exists()
        if have_splits:
            con.execute(f"CREATE OR REPLACE VIEW splits_all AS SELECT * FROM read_parquet('{splits_path.as_posix()}')")
            splits = [row[0] for row in con.execute("SELECT DISTINCT split FROM splits_all").fetchall()]
        else:
            splits = []

        for lag in lags:
            # 1) 计算 ready_pairs（特征完整性）
            ready_path = ready_dir / f"ready_lag{lag}.parquet"
            _compute_ready_pairs(con, root, int(lag), factor_list, ready_path,
                                force=force, min_non_null=min_non_null)

            con.execute(f"CREATE OR REPLACE VIEW ready_v AS SELECT * FROM read_parquet('{ready_path.as_posix()}')")

            # 2) 生成全量索引（加入 split + has_label）
            if have_splits:
                all_sql = f"""
                SELECT r.trade_date, r.stock_code, r.year, r.month, r.day,
                       s.split,
                       CAST(1 AS UINT8) AS ok_factors,
                       CASE WHEN l.trade_date IS NULL THEN CAST(0 AS UINT8) ELSE CAST(1 AS UINT8) END AS has_label
                FROM splits_all s
                JOIN ready_v r USING (trade_date, stock_code)
                LEFT JOIN labels_has l USING (trade_date, stock_code)
                """
            else:
                all_sql = f"""
                SELECT r.trade_date, r.stock_code, r.year, r.month, r.day,
                       'unused' AS split,
                       CAST(1 AS UINT8) AS ok_factors,
                       CASE WHEN l.trade_date IS NULL THEN CAST(0 AS UINT8) ELSE CAST(1 AS UINT8) END AS has_label
                FROM ready_v r
                LEFT JOIN labels_has l USING (trade_date, stock_code)
                """

            all_path = indices_dir / f"index_lag{lag}.parquet"
            tmp_all = all_path.with_suffix(".tmp.parquet")
            try:
                con.execute(f"COPY ({all_sql}) TO '{tmp_all.as_posix()}' (FORMAT 'parquet', COMPRESSION 'zstd')")
                tmp_all.replace(all_path)
            finally:
                _robust_unlink(tmp_all)

            LOGGER.info("Generated index_lag%s.parquet", lag)

            # 3) 为每个 split 生成训练/推理索引
            per_split_paths: Dict[str, Dict[str, str]] = {}

            if have_splits:
                for split in splits:
                    per_split_paths[split] = {}

                    # 训练索引：ok_factors=1 & has_label=1
                    train_path = indices_dir / f"{split}_index_lag{lag}_train.parquet"
                    tmp_train = train_path.with_suffix(".tmp.parquet")
                    train_filter = "ok_factors=1 AND has_label=1" if require_label_for_train else "ok_factors=1"
                    train_sql = f"""
                    SELECT trade_date, stock_code, year, month, day, split,
                           ok_factors, has_label,
                           row_number() OVER (ORDER BY trade_date, stock_code) - 1 AS index_id
                    FROM read_parquet('{all_path.as_posix()}')
                    WHERE split='{split}' AND {train_filter}
                    """
                    try:
                        con.execute(f"COPY ({train_sql}) TO '{tmp_train.as_posix()}' (FORMAT 'parquet', COMPRESSION 'zstd')")
                        tmp_train.replace(train_path)
                    finally:
                        _robust_unlink(tmp_train)
                    per_split_paths[split]["train"] = train_path.as_posix()
                    LOGGER.info("Generated %s_index_lag%s_train.parquet", split, lag)

                    # 推理索引：ok_factors=1（不要求 has_label）
                    infer_path = indices_dir / f"{split}_index_lag{lag}_infer.parquet"
                    tmp_infer = infer_path.with_suffix(".tmp.parquet")
                    infer_sql = f"""
                    SELECT trade_date, stock_code, year, month, day, split,
                           ok_factors, has_label,
                           row_number() OVER (ORDER BY trade_date, stock_code) - 1 AS index_id
                    FROM read_parquet('{all_path.as_posix()}')
                    WHERE split='{split}' AND ok_factors=1
                    """
                    try:
                        con.execute(f"COPY ({infer_sql}) TO '{tmp_infer.as_posix()}' (FORMAT 'parquet', COMPRESSION 'zstd')")
                        tmp_infer.replace(infer_path)
                    finally:
                        _robust_unlink(tmp_infer)
                    per_split_paths[split]["infer"] = infer_path.as_posix()
                    LOGGER.info("Generated %s_index_lag%s_infer.parquet", split, lag)

            con.execute("DROP VIEW ready_v")

            eff_thr = int(min_non_null) if (min_non_null is not None) else _default_min_non_null(int(lag))
            entries.append({
                "lag": int(lag),
                "path_all": all_path.as_posix(),
                "per_split": per_split_paths,
                "min_non_null": eff_thr,
                "require_label_for_train": bool(require_label_for_train),
            })

        # 更新 schema.json
        _update_schema_indices(root, entries)
        LOGGER.info("Updated schema.json with indices metadata")
        return entries

    finally:
        con.close()
