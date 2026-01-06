"""
Shared factor cache manager.

Keeps factor predictions under <model_path>/bt_results/factors and supports
range-based loading and incremental backfill.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .atomic_write import atomic_write_df, atomic_write_json

logger = logging.getLogger(__name__)

YEAR_PATTERN = re.compile(r"model_factor_(\d{4})\.csv$")
META_FILENAME = "factor_cache_meta.json"
LOCK_FILENAME = ".lock"
META_VERSION = 1


def _to_timestamp(value: str) -> pd.Timestamp:
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid date value: {value}")
    return ts


def _to_date_str(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d")


def _to_yyyymmdd(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y%m%d")


@dataclass
class YearStats:
    year: int
    path: Path
    min_date: Optional[str] = None
    max_date: Optional[str] = None
    rows: Optional[int] = None


class FactorCacheLock:
    """Simple file lock to protect cache writes."""

    def __init__(self, lock_path: Path, timeout_sec: int = 600, poll_sec: float = 1.0):
        self.lock_path = lock_path
        self.timeout_sec = timeout_sec
        self.poll_sec = poll_sec
        self._fd: Optional[int] = None

    def __enter__(self) -> None:
        deadline = time.time() + self.timeout_sec
        while True:
            try:
                self._fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                payload = f"pid={os.getpid()} time={datetime.now().isoformat()}\n"
                os.write(self._fd, payload.encode("utf-8"))
                return
            except FileExistsError:
                if time.time() >= deadline:
                    raise RuntimeError(f"Factor cache is locked: {self.lock_path}")
                time.sleep(self.poll_sec)

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._fd is not None:
                os.close(self._fd)
            if self.lock_path.exists():
                self.lock_path.unlink()
        except Exception:
            logger.warning("Failed to release factor cache lock", exc_info=True)


class FactorCacheManager:
    """Manage shared factor cache under a single directory."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.cache_dir / META_FILENAME
        self.lock_path = self.cache_dir / LOCK_FILENAME
        self._year_stats: Optional[Dict[int, YearStats]] = None

    def _list_year_files(self) -> Dict[int, Path]:
        files: Dict[int, Path] = {}
        for path in self.cache_dir.glob("model_factor_*.csv"):
            match = YEAR_PATTERN.match(path.name)
            if not match:
                continue
            year = int(match.group(1))
            files[year] = path
        return files

    def _read_meta(self) -> Optional[Dict]:
        if not self.meta_path.exists():
            return None
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("version") != META_VERSION:
                return None
            return meta
        except Exception:
            logger.warning("Failed to read factor cache meta", exc_info=True)
            return None

    def _write_meta(self, stats: Dict[int, YearStats]) -> None:
        meta = {
            "version": META_VERSION,
            "generated_at": datetime.now().isoformat(),
            "years": {
                str(year): {
                    "file": stats[year].path.name,
                    "min_date": stats[year].min_date,
                    "max_date": stats[year].max_date,
                    "rows": stats[year].rows,
                }
                for year in sorted(stats.keys())
            },
        }
        atomic_write_json(meta, self.meta_path, no_overwrite=False)

    def _compute_year_stats(self, path: Path, year: int) -> YearStats:
        df = pd.read_csv(path, usecols=["trade_date"], dtype={"trade_date": str})
        if df.empty:
            return YearStats(year=year, path=path, min_date=None, max_date=None, rows=0)
        dates = pd.to_datetime(df["trade_date"], errors="coerce").dropna()
        if dates.empty:
            return YearStats(year=year, path=path, min_date=None, max_date=None, rows=len(df))
        min_date = _to_date_str(dates.min())
        max_date = _to_date_str(dates.max())
        return YearStats(year=year, path=path, min_date=min_date, max_date=max_date, rows=len(df))

    def _load_year_stats(self) -> Dict[int, YearStats]:
        if self._year_stats is not None:
            return self._year_stats

        stats: Dict[int, YearStats] = {}
        meta = self._read_meta()
        year_files = self._list_year_files()

        updated = False
        if meta and "years" in meta:
            for year_str, info in meta["years"].items():
                year = int(year_str)
                path = year_files.get(year, self.cache_dir / info["file"])
                if not path.exists():
                    continue
                stats[year] = YearStats(
                    year=year,
                    path=path,
                    min_date=info.get("min_date"),
                    max_date=info.get("max_date"),
                    rows=info.get("rows"),
                )
            loaded_years = set(stats.keys())
            for year, path in year_files.items():
                if year in loaded_years:
                    continue
                stats[year] = self._compute_year_stats(path, year)
                updated = True
        else:
            for year, path in year_files.items():
                stats[year] = self._compute_year_stats(path, year)
            updated = bool(stats)

        if updated and stats:
            self._write_meta(stats)

        self._year_stats = stats
        return stats

    def get_coverage(self) -> Tuple[Optional[str], Optional[str]]:
        stats = self._load_year_stats()
        if not stats:
            return None, None
        min_dates = [s.min_date for s in stats.values() if s.min_date]
        max_dates = [s.max_date for s in stats.values() if s.max_date]
        if not min_dates or not max_dates:
            return None, None
        return min(min_dates), max(max_dates)

    def compute_missing_ranges(self, start_date: str, end_date: str) -> List[Tuple[str, str]]:
        start_ts = _to_timestamp(start_date)
        end_ts = _to_timestamp(end_date)
        cache_min, cache_max = self.get_coverage()
        if cache_min is None or cache_max is None:
            return [(_to_yyyymmdd(start_ts), _to_yyyymmdd(end_ts))]
        min_ts = _to_timestamp(cache_min)
        max_ts = _to_timestamp(cache_max)
        missing: List[Tuple[str, str]] = []
        if start_ts < min_ts:
            missing.append((_to_yyyymmdd(start_ts), _to_yyyymmdd(min_ts)))
        if end_ts > max_ts:
            missing.append((_to_yyyymmdd(max_ts), _to_yyyymmdd(end_ts)))
        return missing

    def list_year_files_for_range(self, start_date: str, end_date: str) -> List[Path]:
        start_ts = _to_timestamp(start_date)
        end_ts = _to_timestamp(end_date)
        year_files = self._list_year_files()
        files = []
        for year in range(start_ts.year, end_ts.year + 1):
            path = year_files.get(year)
            if path:
                files.append(path)
        return files

    def load_pred(self, start_date: str, end_date: str) -> pd.DataFrame:
        start_ts = _to_timestamp(start_date)
        end_ts = _to_timestamp(end_date)
        dfs: List[pd.DataFrame] = []

        for path in self.list_year_files_for_range(start_date, end_date):
            df = pd.read_csv(
                path,
                dtype={"trade_date": str, "stock_code": str, "model_pred": float},
            )
            if df.empty:
                continue
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            df = df.dropna(subset=["trade_date"])
            mask = (df["trade_date"] >= start_ts) & (df["trade_date"] <= end_ts)
            df = df.loc[mask, ["trade_date", "stock_code", "model_pred"]]
            if df.empty:
                continue
            df["trade_date"] = df["trade_date"].dt.strftime("%Y-%m-%d")
            dfs.append(df)

        if not dfs:
            return pd.DataFrame(columns=["trade_date", "stock_code", "model_pred"])

        combined = pd.concat(dfs, ignore_index=True)
        combined = combined.drop_duplicates(subset=["trade_date", "stock_code"], keep="last")
        combined = combined.sort_values(["trade_date", "stock_code"]).reset_index(drop=True)
        return combined

    def merge_write_pred(self, df_pred: pd.DataFrame) -> None:
        if df_pred.empty:
            return
        required = {"trade_date", "stock_code", "model_pred"}
        if not required.issubset(df_pred.columns):
            raise ValueError(f"df_pred missing columns: {required - set(df_pred.columns)}")

        df_work = df_pred.copy()
        df_work["trade_date"] = pd.to_datetime(df_work["trade_date"], errors="coerce")
        df_work = df_work.dropna(subset=["trade_date"])
        df_work["year"] = df_work["trade_date"].dt.year

        stats = self._load_year_stats()

        def _write_csv(df: pd.DataFrame, path: Path) -> None:
            df.to_csv(path, index=False, encoding="utf-8")

        with FactorCacheLock(self.lock_path):
            for year, df_year in df_work.groupby("year"):
                year = int(year)
                path = self.cache_dir / f"model_factor_{year}.csv"
                df_year = df_year[["trade_date", "stock_code", "model_pred"]].copy()

                if path.exists():
                    df_existing = pd.read_csv(
                        path,
                        dtype={"trade_date": str, "stock_code": str, "model_pred": float},
                    )
                    df_existing["trade_date"] = pd.to_datetime(df_existing["trade_date"], errors="coerce")
                    df_existing = df_existing.dropna(subset=["trade_date"])
                    merged = pd.concat([df_existing, df_year], ignore_index=True)
                else:
                    merged = df_year

                merged = merged.drop_duplicates(subset=["trade_date", "stock_code"], keep="last")
                merged = merged.sort_values(["trade_date", "stock_code"]).reset_index(drop=True)
                merged["trade_date"] = merged["trade_date"].dt.strftime("%Y-%m-%d")

                atomic_write_df(merged, path, write_func=_write_csv, no_overwrite=False)

                min_date = _to_date_str(pd.to_datetime(merged["trade_date"]).min())
                max_date = _to_date_str(pd.to_datetime(merged["trade_date"]).max())
                stats[year] = YearStats(
                    year=year,
                    path=path,
                    min_date=min_date,
                    max_date=max_date,
                    rows=len(merged),
                )

            self._write_meta(stats)
