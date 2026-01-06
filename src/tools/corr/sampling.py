from __future__ import annotations

import hashlib
from functools import lru_cache
from datetime import datetime
from typing import List, Optional

import numpy as np
import pandas as pd

from src.utils.db_connection import db_config


@lru_cache(maxsize=1)
def fetch_trading_calendar() -> pd.Series:
    engine = db_config.get_wind_engine()
    sql = (
        "SELECT TRADE_DAYS FROM wind_quant.dbo.AShareCalendar "
        "WHERE S_INFO_EXCHMARKET='SSE' ORDER BY TRADE_DAYS"
    )
    df = pd.read_sql(sql, engine)
    if df.empty:
        raise ValueError("Trading calendar query returned empty result")
    col = df.columns[0]
    dates = pd.to_datetime(df[col])
    return dates.dropna().drop_duplicates().sort_values()


def _seed_from_date(seed: int, date_value: pd.Timestamp) -> int:
    payload = f"{seed}-{date_value.strftime('%Y%m%d')}".encode("utf-8")
    digest = hashlib.md5(payload).hexdigest()[:8]
    return int(digest, 16)


def select_trade_dates(calendar: pd.Series, sampling_cfg: dict) -> List[pd.Timestamp]:
    mode = sampling_cfg.get("mode", "fixed_years")
    day_picker = sampling_cfg.get("day_picker", "random_k_per_year")
    random_seed = int(sampling_cfg.get("random_seed", 42))
    random_days_per_year = sampling_cfg.get("random_days_per_year")

    if mode == "fixed_years":
        years = sampling_cfg.get("years") or []
    elif mode == "recent_n_years":
        n_years = int(sampling_cfg.get("n_years", 1))
        latest_year = int(calendar.dt.year.max())
        years = list(range(latest_year - n_years + 1, latest_year + 1))
    elif mode == "date_range":
        start_date = sampling_cfg.get("start_date")
        end_date = sampling_cfg.get("end_date")
        if not start_date or not end_date:
            raise ValueError("date_range mode requires start_date and end_date")
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        dates = calendar[(calendar >= start_dt) & (calendar <= end_dt)]
        return dates.tolist()
    else:
        raise ValueError(f"Unsupported sampling mode: {mode}")

    if day_picker != "random_k_per_year":
        raise ValueError(f"Unsupported day_picker: {day_picker}")
    if not years:
        raise ValueError("No years provided for sampling")

    random_days_per_year = _resolve_random_days_per_year(sampling_cfg, years, random_days_per_year)

    rng = np.random.default_rng(random_seed)
    sampled_dates: List[pd.Timestamp] = []

    for year in sorted(years):
        year_dates = calendar[calendar.dt.year == int(year)]
        if year_dates.empty:
            continue
        if not random_days_per_year:
            sampled = year_dates
        else:
            k = min(int(random_days_per_year), len(year_dates))
            idx = rng.choice(len(year_dates), size=k, replace=False)
            sampled = year_dates.iloc[idx]
        sampled_dates.extend(sampled.tolist())

    return sorted(set(sampled_dates))


def _resolve_random_days_per_year(
    sampling_cfg: dict,
    years: List[int],
    random_days_per_year: Optional[int],
) -> Optional[int]:
    auto = sampling_cfg.get("auto_days_per_year", True)
    if auto and years and len(years) <= 5:
        return min(200, int(400 / len(years)))
    return random_days_per_year


def build_sample_tag(sampling_cfg: dict) -> str:
    mode = sampling_cfg.get("mode", "fixed_years")
    seed = sampling_cfg.get("random_seed", 42)
    years = sampling_cfg.get("years") or []
    k = _resolve_random_days_per_year(sampling_cfg, years, sampling_cfg.get("random_days_per_year"))
    k = k if k is not None else "all"
    if mode == "fixed_years":
        years_part = "_".join(str(y) for y in years)
        return f"fy{years_part}_k{k}_seed{seed}"
    if mode == "recent_n_years":
        n_years = sampling_cfg.get("n_years", 1)
        return f"ry{n_years}_k{k}_seed{seed}"
    if mode == "date_range":
        start = sampling_cfg.get("start_date", "na")
        end = sampling_cfg.get("end_date", "na")
        return f"dr{start}_{end}_k{k}_seed{seed}"
    return f"{mode}_seed{seed}"


def sample_stocks_per_date(
    df: pd.DataFrame,
    random_stocks_per_date: Optional[int],
    random_seed: int,
) -> pd.DataFrame:
    if random_stocks_per_date is None:
        return df
    if random_stocks_per_date <= 0:
        return df

    groups = []
    for trade_date, group in df.groupby("trade_date"):
        stocks = group["stock_code"].unique().tolist()
        if len(stocks) <= random_stocks_per_date:
            groups.append(group)
            continue
        seed = _seed_from_date(random_seed, pd.Timestamp(trade_date))
        rng = np.random.default_rng(seed)
        sampled = rng.choice(stocks, size=random_stocks_per_date, replace=False)
        groups.append(group[group["stock_code"].isin(sampled)])

    if not groups:
        return df.iloc[0:0]
    return pd.concat(groups, ignore_index=True)


def to_ymd(date_value: pd.Timestamp) -> str:
    return pd.Timestamp(date_value).strftime("%Y%m%d")


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")
