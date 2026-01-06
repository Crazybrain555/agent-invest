from __future__ import annotations

import logging
import sys
from typing import Dict, List, Optional

import pandas as pd
from tqdm.auto import tqdm

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.tools.corr.compute import normalize_long_df
from src.tools.corr.sampling import sample_stocks_per_date, to_ymd


def _chunk_list(values: List[pd.Timestamp], chunk_size: int) -> List[List[pd.Timestamp]]:
    if not values:
        return []
    if chunk_size <= 0 or chunk_size >= len(values):
        return [list(values)]
    return [values[i : i + chunk_size] for i in range(0, len(values), chunk_size)]


def _format_trade_dates(trade_dates: List[pd.Timestamp]) -> List[str]:
    return [to_ymd(d) for d in trade_dates]


def fetch_forbid_pool(
    provider: LocalTestDBDataProvider,
    table: str,
    trade_dates: List[pd.Timestamp],
) -> pd.DataFrame:
    if not table or not trade_dates:
        return pd.DataFrame()
    start_date = to_ymd(min(trade_dates))
    end_date = to_ymd(max(trade_dates))
    date_filters = _format_trade_dates(trade_dates)
    forbid_df = provider.fetch_data(
        table=table,
        start_date=start_date,
        end_date=end_date,
        column_filters={"trade_date": date_filters},
        format="wide",
    )
    if forbid_df.empty:
        return forbid_df
    forbid_df = forbid_df[forbid_df.get("signal", False)]
    return forbid_df[["trade_date", "stock_code"]]


def filter_forbid_pool(df: pd.DataFrame, forbid_df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or forbid_df.empty:
        return df
    forbid_keys = pd.MultiIndex.from_frame(forbid_df[["trade_date", "stock_code"]])
    keep_mask = ~pd.MultiIndex.from_frame(df[["trade_date", "stock_code"]]).isin(forbid_keys)
    return df.loc[keep_mask].reset_index(drop=True)


def load_long_df(
    provider: LocalTestDBDataProvider,
    table_factors: Dict[str, List[str]],
    trade_dates: List[pd.Timestamp],
    sampling_cfg: Dict,
    universe_cfg: Dict,
    use_progress: bool,
    logger: Optional[logging.Logger] = None,
    apply_forbid_pool: Optional[bool] = None,
    random_stocks_per_date: Optional[int] = None,
) -> pd.DataFrame:
    chunk_size = int(sampling_cfg.get("trade_date_chunk_size", 30) or 0)
    trade_date_chunks = _chunk_list(trade_dates, chunk_size)
    total_chunks = len(trade_date_chunks)

    if use_progress:
        logging.getLogger("src.data_service.data_loading.local_testdb_data").setLevel(logging.WARNING)

    frames: List[pd.DataFrame] = []
    chunk_bar = tqdm(
        trade_date_chunks,
        desc="trade_date chunks",
        total=total_chunks,
        unit="chunk",
        file=sys.stdout,
        disable=not use_progress,
        dynamic_ncols=True,
    )
    for chunk_idx, date_chunk in enumerate(chunk_bar, start=1):
        chunk_start = to_ymd(min(date_chunk))
        chunk_end = to_ymd(max(date_chunk))
        date_filters = _format_trade_dates(date_chunk)
        if use_progress:
            chunk_bar.set_postfix_str(f"{chunk_start}..{chunk_end} ({len(date_chunk)} dates)")
        elif logger:
            logger.info(
                "Loading trade_date chunk %d/%d (%s..%s, %d dates)",
                chunk_idx,
                total_chunks,
                chunk_start,
                chunk_end,
                len(date_chunk),
            )

        forbid_df = pd.DataFrame()
        use_forbid_pool = universe_cfg.get("exclude_forbid_pool") if apply_forbid_pool is None else apply_forbid_pool
        if use_forbid_pool:
            forbid_df = fetch_forbid_pool(provider, universe_cfg.get("forbid_pool_table"), date_chunk)

        chunk_frames: List[pd.DataFrame] = []
        table_items = list(table_factors.items())
        table_bar = tqdm(
            table_items,
            desc=f"tables (chunk {chunk_idx}/{total_chunks})",
            unit="table",
            leave=False,
            file=sys.stdout,
            disable=not use_progress,
            dynamic_ncols=True,
        )
        for table, factors in table_bar:
            if use_progress:
                table_bar.set_postfix_str(f"{table} ({len(factors)} factors)")
            elif logger:
                logger.info("Loading %s (%d factors) chunk %d/%d", table, len(factors), chunk_idx, total_chunks)
            df = provider.fetch_data(
                table=table,
                start_date=chunk_start,
                end_date=chunk_end,
                fields=factors,
                format="long",
                column_filters={"trade_date": date_filters},
            )
            if df.empty:
                continue
            df = normalize_long_df(df)
            df = df[df["trade_date"].isin(date_chunk)]
            if df.empty:
                continue
            df["factor_id"] = df["field_name"].astype(str).radd(f"{table}::")
            df["value"] = pd.to_numeric(df["value"], errors="coerce").astype("float32")
            chunk_frames.append(df[["trade_date", "stock_code", "factor_id", "value"]])

        if not chunk_frames:
            continue
        chunk_df = pd.concat(chunk_frames, ignore_index=True)
        if not forbid_df.empty:
            chunk_df = filter_forbid_pool(chunk_df, forbid_df)
            if chunk_df.empty:
                continue
        effective_random = (
            sampling_cfg.get("random_stocks_per_date") if random_stocks_per_date is None else random_stocks_per_date
        )
        chunk_df = sample_stocks_per_date(chunk_df, effective_random, int(sampling_cfg.get("random_seed", 42)))
        if chunk_df.empty:
            continue
        frames.append(chunk_df)

    if not frames:
        return pd.DataFrame()

    long_df = pd.concat(frames, ignore_index=True)
    long_df["stock_code"] = long_df["stock_code"].astype("category")
    long_df["factor_id"] = long_df["factor_id"].astype("category")
    return long_df
