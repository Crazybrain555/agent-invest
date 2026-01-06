# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from pathlib import Path
from typing import List, Optional
import duckdb
import pandas as pd


def _ensure_duckdb(con: duckdb.DuckDBPyConnection,
                   temp_directory: Optional[str],
                   memory_limit: Optional[str],
                   threads: Optional[int]):
    if temp_directory:
        con.execute(f"SET temp_directory='{temp_directory}';")
    if memory_limit:
        con.execute(f"SET memory_limit='{memory_limit}';")
    if threads is not None:
        con.execute(f"SET threads={int(threads)};")
    # 写入大结果集时关闭保序，显著降低内存峰值
    con.execute("SET preserve_insertion_order=false;")
    # 限制分区写时同时打开的文件数，避免句柄与内存峰值
    con.execute("SET partitioned_write_max_open_files=16;")
    # 进度条有时有用；如不需要可去掉
    con.execute("SET enable_progress_bar=true;")


def build_sequence_lists_with_duckdb(
    long_df: pd.DataFrame,
    factor_names_expanded: List[str],
    lag: int,
    duckdb_path: Optional[str] = None,
    temp_directory: Optional[str] = None,
    memory_limit: Optional[str] = None,
    threads: Optional[int] = None,
) -> pd.DataFrame:
    """
    用 DuckDB 将长表聚合为：每因子一列（LIST<FLOAT>，长度=lag，左侧NULL填充）的宽表。
    输入列要求: ['trade_date','stock_code','factor_name','z_windows','factor_value']
    返回列: ['trade_date','stock_code', <each factor_name_wX as LIST<FLOAT>>]
    """
    if long_df.empty:
        return pd.DataFrame()

    df = long_df.copy()
    # 保持 datetime 类型，避免在 DuckDB 中由字符串解析日期
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df['stock_code'] = df['stock_code'].astype(str)

    db_path = duckdb_path or ":memory:"
    con = duckdb.connect(database=db_path)
    _ensure_duckdb(con, temp_directory, memory_limit, threads)

    con.register("f_long", df)

    # 显式列清单，确保列顺序与 schema 一致
    factor_list_sql = ",".join([f"'" + f.replace("'", "''") + "'" for f in factor_names_expanded])
    select_factor_cols = ", ".join([f'"{f}"' for f in factor_names_expanded])

    sql = f"""
    WITH base AS (
      SELECT
        CAST(trade_date AS DATE) AS trade_date,
        stock_code,
        factor_name,
        CAST(factor_value AS FLOAT) AS v
      FROM f_long
    ),
    seq_rows AS (
      SELECT
        trade_date,
        stock_code,
        factor_name,
        -- 左侧 NULL padding，并截取最近 {lag} 个（使用中括号切片，end 省略表示到末尾）
        (
          list_concat(
            list_resize([], {lag}, CAST(NULL AS FLOAT)),
            list(v) OVER (
              PARTITION BY stock_code, factor_name
              ORDER BY trade_date
              ROWS BETWEEN {lag-1} PRECEDING AND CURRENT ROW
            )
          )
        )[-{lag}:] AS seq
      FROM base
    ),
    wide AS (
      SELECT *
      FROM (
        PIVOT seq_rows
        ON factor_name IN ({factor_list_sql})
        USING first(seq)
      ) AS p
    )
    SELECT
      strftime(trade_date, '%Y%m%d') AS trade_date,
      stock_code,
      {select_factor_cols}
    FROM wide
    """

    out_df = con.execute(sql).fetchdf()
    con.close()
    return out_df


def build_seq_lists_and_write(
    features_long_z: pd.DataFrame,
    labels_df: pd.DataFrame,
    shard_dir: str,
    factor_names: List[str],
    lag: int,
    duckdb_path: Optional[str] = None,
    temp_directory: Optional[str] = None,
    memory_limit: Optional[str] = None,
    threads: Optional[int] = None,
    label_col: Optional[str] = None,
) -> pd.DataFrame:
    """
    直接在 DuckDB 中完成：窗口聚合为 LIST → PIVOT → 与标签 JOIN → COPY 到分区化 Parquet。
    避免将巨大的"列表列宽表"回传到 Python，规避内存峰值。

    返回值：
        pandas.DataFrame，仅包含实际写入到分区 Parquet 的 (trade_date, stock_code) 键。
    
    Args:
        features_long_z: 长表特征数据（已预处理+zscore）
        labels_df: 标签数据
        shard_dir: 输出分片目录
        factor_names: 因子名称列表（含窗口后缀，如 xxx_w1）
        lag: 序列长度
        其他参数: DuckDB 配置选项
    """
    if features_long_z.empty or labels_df.empty:
        logger.warning("输入数据为空，跳过 DuckDB 处理")
        return pd.DataFrame(columns=["trade_date", "stock_code"])

    # 列名鲁棒规范化：确保存在 trade_date / stock_code 列
    def _ensure_cols(df: pd.DataFrame, role: str) -> pd.DataFrame:
        df = df.copy()
        # trade_date
        if 'trade_date' not in df.columns:
            if getattr(df.index, 'name', None) == 'trade_date':
                df = df.reset_index()
            elif 'TRADE_DATE' in df.columns:
                df = df.rename(columns={'TRADE_DATE': 'trade_date'})
            elif 'date' in df.columns:
                df = df.rename(columns={'date': 'trade_date'})
            else:
                raise KeyError(f"{role} 缺少 trade_date 列")
        # stock_code
        if 'stock_code' not in df.columns:
            if getattr(df.index, 'name', None) == 'stock_code':
                df = df.reset_index()
            elif 'STOCK_CODE' in df.columns:
                df = df.rename(columns={'STOCK_CODE': 'stock_code'})
            elif 'sec_code' in df.columns:
                df = df.rename(columns={'sec_code': 'stock_code'})
            else:
                raise KeyError(f"{role} 缺少 stock_code 列")
        return df

    feats = _ensure_cols(features_long_z, "features_long_z")
    labs  = _ensure_cols(labels_df, "labels_df")

    # 规范化类型
    feats['trade_date'] = pd.to_datetime(feats['trade_date'])
    feats['stock_code'] = feats['stock_code'].astype(str)
    labs['trade_date']  = pd.to_datetime(labs['trade_date'])
    labs['stock_code']  = labs['stock_code'].astype(str)

    # 自动推断 label 列
    if label_col is None:
        label_candidates = [c for c in labs.columns if c not in ('trade_date', 'stock_code')]
        if not label_candidates:
            raise ValueError("labels_df 缺少标签列")
        label_col = label_candidates[0]
        logger.debug(f"自动推断标签列: {label_col}")

    factor_list_sql = ",".join([f"'" + f.replace("'", "''") + "'" for f in factor_names])
    select_factor_cols = ", ".join([f'"{f}"' for f in factor_names])
    logger.debug(f"处理 {len(factor_names)} 个因子，序列长度 {lag}")

    db_path = duckdb_path or ":memory:"
    con = duckdb.connect(database=db_path)
    _ensure_duckdb(con, temp_directory, memory_limit, threads)

    con.register("feat_long", feats)
    con.register("labels", labs)

    # COPY 选项：分区写 + 控制行组大小（注意：不能与文件轮换选项同用）
    base_options = (
        "FORMAT 'parquet', "
        "PARTITION_BY (year, month), "
        "COMPRESSION 'zstd', "
        "OVERWRITE_OR_IGNORE true, "
        "ROW_GROUP_SIZE 65536"
    )

    # 因子个数，用于判满窗口
    n_factors = len(factor_names)
    logger.debug(f"DuckDB 严格满窗判定使用因子数: {n_factors}")

    core_select = f"""
    WITH base AS (
      SELECT
        CAST(trade_date AS DATE) AS trade_date,
        stock_code,
        factor_name,
        CAST(factor_value AS FLOAT) AS v
      FROM feat_long
    ),
    seq_rows AS (
      SELECT
        trade_date,
        stock_code,
        factor_name,
        -- 直接窗口聚合最近 {lag} 个取值
        list(v) OVER (
          PARTITION BY stock_code, factor_name
          ORDER BY trade_date
          ROWS BETWEEN {lag-1} PRECEDING AND CURRENT ROW
        ) AS seq,
        -- 非空计数是否等于 {lag}
        count(v) FILTER (WHERE v IS NOT NULL) OVER (
          PARTITION BY stock_code, factor_name
          ORDER BY trade_date
          ROWS BETWEEN {lag-1} PRECEDING AND CURRENT ROW
        ) = {lag} AS is_full
      FROM base
    ),
    ok_dates AS (
      -- 仅保留所有因子都满 {lag} 的 (date, stock)
      SELECT trade_date, stock_code
      FROM seq_rows
      GROUP BY trade_date, stock_code
      HAVING sum(CASE WHEN is_full THEN 1 ELSE 0 END) = {n_factors}
    ),
    seq_ok AS (
      SELECT s.*
      FROM seq_rows s
      JOIN ok_dates k USING (trade_date, stock_code)
    ),
    wide AS (
      SELECT *
      FROM (
        PIVOT seq_ok
        ON factor_name IN ({factor_list_sql})
        USING first(seq)
      ) AS p
    )
    SELECT
      strftime(w.trade_date, '%Y%m%d') AS trade_date,
      w.stock_code,
      {select_factor_cols},
      l."{label_col}" AS "{label_col}",
      substr(strftime(w.trade_date, '%Y%m%d'),1,4) AS year,
      substr(strftime(w.trade_date, '%Y%m%d'),5,2) AS month
    FROM wide AS w
    JOIN (
      SELECT CAST(trade_date AS DATE) AS trade_date, stock_code, "{label_col}"
      FROM labels
    ) AS l
    USING (trade_date, stock_code)
    """
    sql = f"COPY ({core_select}) TO '{Path(shard_dir).as_posix()}' ({base_options});"

    written_keys = pd.DataFrame(columns=["trade_date", "stock_code"])
    try:
        logger.debug("开始执行 DuckDB 窗口聚合和写入...")
        con.execute(sql)
        logger.debug(f"✅ 成功写入分区 Parquet 到 {shard_dir}")

        # 关键：获取本次将被写入的数据集对应的 (trade_date, stock_code) 键
        # 直接基于 core_select 做一次轻量级 SELECT，避免全目录回扫
        keys_sql = f"SELECT trade_date, stock_code FROM ({core_select})"
        try:
            keys_df = con.execute(keys_sql).fetchdf()
            # 类型规范
            if not keys_df.empty:
                keys_df["trade_date"] = keys_df["trade_date"].astype(str)
                keys_df["stock_code"] = keys_df["stock_code"].astype(str)
                written_keys = keys_df[["trade_date", "stock_code"]]
            logger.debug(f"用于索引的已写入键：{len(written_keys):,}")
        except Exception as ex:
            logger.warning(f"无法提取已写入键（将返回空）：{ex}")
    except Exception as e:
        logger.error(f"❌ DuckDB 执行失败: {str(e)}")
        raise
    finally:
        con.close()

    return written_keys


