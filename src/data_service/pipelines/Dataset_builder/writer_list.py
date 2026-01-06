# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from pathlib import Path
from typing import List
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds


def _write_chunk_list(df: pd.DataFrame, shard_dir: Path, list_feature_cols: List[str], label_col: str):
    """
    将每个因子列作为 pa.list_(pa.float32()) 落盘，分区 year/month。
    期望 df 列: trade_date(str 'YYYYMMDD'), stock_code(str), label_col(float32), list_feature_cols(each: python list of float)
    """
    if df.empty:
        return

    df = df.copy()
    df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce').dt.strftime('%Y%m%d')
    df['year'] = df['trade_date'].str.slice(0, 4)
    df['month'] = df['trade_date'].str.slice(4, 6)

    # Schema: list<float32> for features, float32 for label, string for keys
    schema_fields = []
    for c in list_feature_cols:
        if c in df.columns:
            schema_fields.append(pa.field(c, pa.list_(pa.float32())))
    if label_col in df.columns:
        schema_fields.append(pa.field(label_col, pa.float32()))
    for c in ['trade_date', 'stock_code', 'year', 'month']:
        schema_fields.append(pa.field(c, pa.string()))
    schema = pa.schema(schema_fields)

    cols = ['trade_date', 'stock_code'] + list_feature_cols + ([label_col] if label_col in df.columns else []) + ['year', 'month']
    tbl = pa.Table.from_pandas(df[cols], schema=schema, preserve_index=False)

    parquet_fmt = ds.ParquetFileFormat()
    file_opts = parquet_fmt.make_write_options(compression='zstd')

    ds.write_dataset(
        tbl,
        base_dir=str(shard_dir),
        format=parquet_fmt,
        file_options=file_opts,
        partitioning=['year', 'month'],
        existing_data_behavior='overwrite_or_ignore',
    )


