# -*- coding: utf-8 -*-
import logging

logger = logging.getLogger(__name__)

from pathlib import Path
from typing import Sequence, Tuple, List, Set, Optional, Dict, Union
import pandas as pd


def _ensure_dirs(out_dir: Path):
    meta = out_dir / "meta"
    shards = out_dir / "shards"
    features_long = shards / "features_long"
    labels = shards / "labels"

    meta.mkdir(parents=True, exist_ok=True)
    shards.mkdir(parents=True, exist_ok=True)
    features_long.mkdir(parents=True, exist_ok=True)
    labels.mkdir(parents=True, exist_ok=True)
    return meta, shards


def _iter_ranges(start: str, end: str, freq: str = "M"):
    """Generate (start, end) pairs for the requested frequency.

    - freq="M": monthly slices, inclusive end-of-month;
    - freq="Q": quarterly slices, inclusive end-of-quarter;
    - freq="Y": yearly slices, inclusive end-of-year;
    - otherwise fall back to pandas date_range behaviour.
    """

    if freq == "M":
        idx = pd.date_range(start, end, freq="MS")
        for d0 in idx:
            d1_raw = (d0 + pd.offsets.MonthEnd(0)).strftime("%Y%m%d")
            d1 = min(d1_raw, end)
            yield d0.strftime("%Y%m%d"), d1
        return

    if freq == "Q":
        idx = pd.date_range(start, end, freq="QS")
        for d0 in idx:
            d1_raw = (d0 + pd.offsets.QuarterEnd(0)).strftime("%Y%m%d")
            d1 = min(d1_raw, end)
            yield d0.strftime("%Y%m%d"), d1
        return

    if freq == "Y":
        idx = pd.date_range(start, end, freq="YS")
        for d0 in idx:
            d1_raw = (d0 + pd.offsets.YearEnd(0)).strftime("%Y%m%d")
            d1 = min(d1_raw, end)
            yield d0.strftime("%Y%m%d"), d1
        return

    idx = pd.date_range(start, end, freq=freq)
    if len(idx) == 0:
        return
    for d0, d1 in zip(idx, idx[1:].append(pd.DatetimeIndex([end]))):
        yield d0.strftime("%Y%m%d"), d1.strftime("%Y%m%d")


