"""Dataset maintenance utilities: coverage diagnostics and incremental extension."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import text

from src.data_service.pipelines.Dataset_builder.main_builder import (
    build_pv_dataset_wide_daily,
    DEFAULT_LABELS_TABLE,
)
from src.data_service.pipelines.Dataset_builder.indices import generate_indices
from src.utils.db_connection import db_config


DEFAULT_MAX_FACTORS_PER_BATCH = 16
LABEL_TABLE_FALLBACKS = {
    "ai_is.training_label_ls10_adj_topcor_cr30_cw240": DEFAULT_LABELS_TABLE,
}


# ---------------------------------------------------------------------------
# Coverage model
# ---------------------------------------------------------------------------


@dataclass
class CoverageReport:
    dataset_start: Optional[str]
    dataset_end: Optional[str]
    target_start: str
    target_end: str
    missing_ranges: List[Tuple[str, str]] = field(default_factory=list)
    extended: bool = False

    @property
    def is_satisfied(self) -> bool:
        return not self.missing_ranges


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_dataframe(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pq.read_table(path).to_pandas()
    except Exception:
        return pd.DataFrame()


def _unique_sorted_dates(df: pd.DataFrame) -> List[str]:
    if df.empty or "trade_date" not in df.columns:
        return []
    ser = pd.to_datetime(df["trade_date"], errors="coerce").dropna().dt.strftime("%Y%m%d")
    return sorted(ser.unique())


def _ensure_directory(dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)


def _copy_partitioned_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    for file in src.rglob("*.parquet"):
        rel = file.relative_to(src)
        target = dst / rel
        _ensure_directory(target.parent)
        shutil.move(str(file), str(target))


def _discover_lags(indices_dir: Path) -> List[int]:
    lags: List[int] = []
    if not indices_dir.exists():
        return lags
    for file in indices_dir.glob("index_lag*.parquet"):
        name = file.stem  # index_lag300
        try:
            lag = int(name.replace("index_lag", ""))
            lags.append(lag)
        except ValueError:
            continue
    return sorted(set(lags))


def _load_trading_days(start: str, end: str) -> List[str]:
    sql = text(
        """
        SELECT TRADE_DAYS
        FROM wind_quant.dbo.AShareCalendar
        WHERE S_INFO_EXCHMARKET='SSE'
          AND TRADE_DAYS BETWEEN :start AND :end
        ORDER BY TRADE_DAYS
        """
    )
    try:
        with db_config.get_wind_session() as session:
            rows = session.execute(sql, {"start": start, "end": end}).fetchall()
        return [str(row[0]) for row in rows]
    except Exception:
        # Fallback: assume calendar days
        rng = pd.date_range(start=start, end=end, freq="D")
        return [d.strftime("%Y%m%d") for d in rng]


def _compute_missing_ranges(
    available: Sequence[str], required: Sequence[str]
) -> List[Tuple[str, str]]:
    avail_set = set(available)
    missing: List[Tuple[str, str]] = []
    current_start: Optional[str] = None

    for day in required:
        if day in avail_set:
            if current_start is not None:
                missing.append((current_start, prev_day))
                current_start = None
        else:
            if current_start is None:
                current_start = day
        prev_day = day

    if current_start is not None:
        missing.append((current_start, required[-1]))
    return missing


def _merge_metadata(
    dataset_meta: Path,
    temp_meta: Path,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    splits_final = _load_dataframe(dataset_meta / "splits.parquet")
    full_final = _load_dataframe(dataset_meta / "full_indices.parquet")

    temp_full = _load_dataframe(temp_meta / "full_indices.parquet")
    temp_splits = _load_dataframe(temp_meta / "splits.parquet")
    if temp_splits.empty and not temp_full.empty:
        temp_splits = temp_full.loc[:, ["trade_date", "stock_code", "split"]]

    if not temp_splits.empty:
        splits_final = pd.concat([splits_final, temp_splits], ignore_index=True)
        splits_final = (
            splits_final.drop_duplicates(subset=["trade_date", "stock_code"], keep="last")
            .sort_values(["trade_date", "stock_code"])
            .reset_index(drop=True)
        )

    if not temp_full.empty:
        full_final = pd.concat([full_final, temp_full], ignore_index=True)
        full_final = (
            full_final.drop_duplicates(subset=["trade_date", "stock_code"], keep="last")
            .sort_values(["trade_date", "stock_code"])
            .reset_index(drop=True)
        )
        if "index_id" in full_final.columns:
            full_final = full_final.drop(columns=["index_id"])

    return splits_final, full_final


# ---------------------------------------------------------------------------
# Coverage analyzer
# ---------------------------------------------------------------------------


class DatasetCoverageAnalyzer:
    def __init__(self, dataset_path: Path | str):
        self.dataset_path = Path(dataset_path)
        self.meta_dir = self.dataset_path / "meta"
        self.schema_path = self.meta_dir / "schema.json"
        self._schema_cache: Optional[dict] = None
        self._full_indices_df: Optional[pd.DataFrame] = None

    def load_schema(self) -> dict:
        if self._schema_cache is None:
            if not self.schema_path.exists():
                raise FileNotFoundError(f"schema.json not found at {self.schema_path}")
            # schema.json may be written with BOM, so tolerate utf-8-sig
            self._schema_cache = json.loads(self.schema_path.read_text(encoding="utf-8-sig"))
        return self._schema_cache

    def load_full_indices(self) -> pd.DataFrame:
        if self._full_indices_df is None:
            path = self.meta_dir / "full_indices.parquet"
            self._full_indices_df = _load_dataframe(path)
        return self._full_indices_df

    def dataset_bounds(self) -> Tuple[Optional[str], Optional[str]]:
        df = self.load_full_indices()
        if df.empty:
            return None, None
        dates = _unique_sorted_dates(df)
        if not dates:
            return None, None
        return dates[0], dates[-1]

    def coverage(self, start: str, end: str) -> CoverageReport:
        df = self.load_full_indices()
        available_dates = _unique_sorted_dates(df)
        ds_start = available_dates[0] if available_dates else None
        ds_end = available_dates[-1] if available_dates else None

        required = _load_trading_days(start, end)
        missing_ranges = _compute_missing_ranges(available_dates, required)

        return CoverageReport(
            dataset_start=ds_start,
            dataset_end=ds_end,
            target_start=start,
            target_end=end,
            missing_ranges=missing_ranges,
        )


# ---------------------------------------------------------------------------
# Dataset appender
# ---------------------------------------------------------------------------


class DatasetAppender:
    def __init__(self, dataset_path: Path | str):
        self.dataset_path = Path(dataset_path).resolve()
        self.meta_dir = self.dataset_path / "meta"
        self.schema_path = self.meta_dir / "schema.json"
        analyzer = DatasetCoverageAnalyzer(self.dataset_path)
        self.schema = analyzer.load_schema()

    # Public API -------------------------------------------------------------

    def append_range(
        self,
        start: str,
        end: str,
        *,
        threads: Optional[int] = None,
    ) -> None:
        if start > end:
            return
        with tempfile.TemporaryDirectory(prefix="dataset_extend_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            self._build_increment(tmp_dir, start, end)
            self._merge_increment(tmp_dir)
            self._regenerate_indices(threads=threads)
            self._update_schema(end)

    # Internal ---------------------------------------------------------------

    def _build_increment(self, temp_dir: Path, start: str, end: str) -> None:
        factor_windows = self.schema.get("factor_windows") or {}
        label_name = self.schema.get("label_col")
        if not label_name:
            raise ValueError("schema.json missing 'label_col'")

        features_sources = self.schema.get("feature_sources") or []
        labels_table_cfg = self.schema.get("labels_table")
        if not labels_table_cfg:
            labels_table = DEFAULT_LABELS_TABLE
        else:
            labels_table = LABEL_TABLE_FALLBACKS.get(labels_table_cfg, labels_table_cfg)
        self.schema["labels_table"] = labels_table
        restricted_table = self.schema.get("restricted_table")
        stats_table = self.schema.get("stats_table")
        warmup_days = int(self.schema.get("warmup_days", 200))
        filters_cfg = self.schema.get("filters", {}) or {}
        max_factors_per_batch = self.schema.get("max_factors_per_batch")

        if max_factors_per_batch is None:
            max_factors_per_batch = DEFAULT_MAX_FACTORS_PER_BATCH
        else:
            try:
                max_factors_per_batch = int(max_factors_per_batch)
            except (TypeError, ValueError):  # noqa: BLE001
                max_factors_per_batch = DEFAULT_MAX_FACTORS_PER_BATCH

        self.schema["max_factors_per_batch"] = max_factors_per_batch

        split_rules = self._extract_split_rules()

        build_pv_dataset_wide_daily(
            output_dir=temp_dir,
            start_date=start,
            end_date=end,
            label_name=label_name,
            factor_windows=factor_windows,
            features_table=features_sources,
            labels_table=labels_table,
            restricted_table=restricted_table,
            split_rules=split_rules if split_rules else None,
            warmup_days=warmup_days,
            stats_table=stats_table,
            clip_std=self.schema.get("clip_std", True),
            factor_based_nan_handling=self.schema.get("factor_based_nan_handling", True),
            consecutive_nan_threshold=self.schema.get("consecutive_nan_threshold"),
            dropna_factor_value=filters_cfg.get("dropna_factor_value", True),
            filter_features_restricted=filters_cfg.get("filter_features_restricted", True),
            exclude_code_prefixes=filters_cfg.get("exclude_code_prefixes"),
            exclude_codes_regex=filters_cfg.get("exclude_codes_regex"),
            feature_lag_hint=int(self.schema.get("feature_lag", 30)),
            max_factors_per_batch=max_factors_per_batch,
        )

    def _merge_increment(self, temp_dir: Path) -> None:
        shards_dir = temp_dir / "shards"
        if shards_dir.exists():
            for child in shards_dir.iterdir():
                target = self.dataset_path / "shards" / child.name
                _ensure_directory(target)
                _copy_partitioned_tree(child, target)

        splits_final, full_final = _merge_metadata(
            self.meta_dir,
            temp_dir / "meta",
        )

        if not splits_final.empty:
            pq.write_table(
                pa.Table.from_pandas(splits_final),
                self.meta_dir / "splits.parquet",
                compression="zstd",
            )
        if not full_final.empty:
            full_final = full_final.sort_values(["trade_date", "stock_code"]).reset_index(drop=True)
            full_final["index_id"] = range(len(full_final))
            pq.write_table(
                pa.Table.from_pandas(full_final),
                self.meta_dir / "full_indices.parquet",
                compression="zstd",
            )

    def _regenerate_indices(self, *, threads: Optional[int]) -> None:
        indices_dir = self.meta_dir / "indices"
        lags = _discover_lags(indices_dir)
        if not lags:
            feature_lag = int(self.schema.get("feature_lag", 30))
            lags = [feature_lag]
        generate_indices(
            self.dataset_path,
            lags=lags,
            factors="auto",
            threads=threads,
            with_splits=True,
            force=True,
            show_progress=False,
        )

    def _update_schema(self, end: str) -> None:
        schema = self.schema
        schema["dataset_last_date"] = end
        schema["build_end_date"] = end
        schema["build_updated_at"] = pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        self.schema_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")

    def _extract_split_rules(self) -> List[Tuple[str, str, str]]:
        splits_path = self.meta_dir / "splits.parquet"
        if not splits_path.exists():
            return []
        df = pq.read_table(splits_path).to_pandas()
        if df.empty or "split" not in df.columns:
            return []
        rules: List[Tuple[str, str, str]] = []
        for split, grp in df.groupby("split"):
            start = grp["trade_date"].min()
            end = grp["trade_date"].max()
            rules.append((split, start, end))
        return rules


# ---------------------------------------------------------------------------
# High-level maintenance façade
# ---------------------------------------------------------------------------


class DatasetMaintenance:
    def __init__(self, dataset_path: Path | str):
        self.dataset_path = Path(dataset_path).resolve()
        self.analyzer = DatasetCoverageAnalyzer(self.dataset_path)

    def ensure_coverage(
        self,
        start: str,
        end: str,
        *,
        auto_extend: bool = True,
        threads: Optional[int] = None,
    ) -> CoverageReport:
        report = self.analyzer.coverage(start, end)
        if report.is_satisfied or not auto_extend:
            return report

        gap_start, gap_end = report.missing_ranges[0][0], report.missing_ranges[-1][1]
        appender = DatasetAppender(self.dataset_path)
        appender.append_range(gap_start, gap_end, threads=threads)
        self.analyzer = DatasetCoverageAnalyzer(self.dataset_path)
        report = self.analyzer.coverage(start, end)
        report.extended = report.is_satisfied
        return report
