from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
import re

import pandas as pd
import yaml

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.tools.corr.compute import (
    build_corr_matrix,
    build_high_corr_pairs,
    build_factor_matrix,
    compute_corr_one_to_many,
    compute_corr_stats,
)
from src.tools.corr.config import load_include_tables, resolve_repo_root
from src.tools.corr.loading import fetch_forbid_pool, filter_forbid_pool, load_long_df
from src.tools.corr.recommend import recommend_by_cluster
from src.tools.corr.sampling import fetch_trading_calendar, sample_stocks_per_date, select_trade_dates, to_ymd
from src.tools.corr.sources import (
    build_factor_index,
    build_factor_meta,
    build_table_factor_map,
    flatten_table_factors,
    merge_table_maps,
    normalize_table_name,
    resolve_factor_specs,
)


@dataclass
class CorrResult:
    mode: str
    summary: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    missing: Dict[str, List[str]] = field(default_factory=dict)
    stats: pd.DataFrame = field(default_factory=pd.DataFrame)
    corr_table: pd.DataFrame = field(default_factory=pd.DataFrame)
    corr_matrix: pd.DataFrame = field(default_factory=pd.DataFrame)
    high_corr_pairs: pd.DataFrame = field(default_factory=pd.DataFrame)
    recommendation: Dict[str, Any] = field(default_factory=dict)


def _normalize_list(values: Optional[Iterable[str]]) -> List[str]:
    items: List[str] = []
    for value in values or []:
        if value is None:
            continue
        part = str(value).strip()
        if part:
            items.append(part)
    return items


def _load_suffix_pattern(repo_root: Path) -> Optional[str]:
    path = repo_root / "configs" / "db" / "table_config.yaml"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    rule = (
        config.get("code_format_rules", {})
        .get("output_format", {})
        .get("remove_all_suffix", {})
    )
    suffixes = rule.get("suffixes", [])
    if not suffixes:
        return None
    return "|".join(map(re.escape, suffixes))


def _normalize_stock_codes(series: pd.Series, suffix_pattern: Optional[str]) -> pd.Series:
    codes = series.astype(str).str.strip()
    if suffix_pattern:
        codes = codes.str.replace(f"({suffix_pattern})$", "", regex=True)
    return codes


def normalize_user_factor_df(
    df: pd.DataFrame,
    *,
    target_key: str,
    suffix_pattern: Optional[str],
    dup_policy: str,
) -> pd.DataFrame:
    if df is None or df.empty:
        raise ValueError("target_df is empty; provide a long-format DataFrame with data.")

    required = ["trade_date", "stock_code", "value"]
    missing_cols = [col for col in required if col not in df.columns]
    if missing_cols:
        example = (
            "trade_date,stock_code,value\n"
            "2020-01-02,000001,0.123\n"
            "2020-01-02,000002,-0.456"
        )
        raise ValueError(
            "target_df must be long format with columns: trade_date, stock_code, value. "
            f"Missing: {', '.join(missing_cols)}. Example:\n{example}"
        )

    work = df.copy()
    work = work[required].copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce")
    if work["trade_date"].isna().any():
        raise ValueError("target_df contains invalid trade_date values after parsing.")
    work["stock_code"] = _normalize_stock_codes(work["stock_code"], suffix_pattern)
    if work["stock_code"].isna().any() or (work["stock_code"].str.len() == 0).any():
        raise ValueError("target_df contains invalid stock_code values after normalization.")
    work["value"] = pd.to_numeric(work["value"], errors="coerce").astype("float32")
    work["factor_id"] = str(target_key)

    keys = ["trade_date", "stock_code", "factor_id"]
    if dup_policy == "error":
        if work.duplicated(subset=keys).any():
            dup = work[work.duplicated(subset=keys, keep=False)]
            sample = dup.head(3)[keys].to_dict(orient="records")
            raise ValueError(f"target_df has duplicate keys; sample: {sample}")
    elif dup_policy == "last":
        work = work.drop_duplicates(subset=keys, keep="last")
    elif dup_policy == "mean":
        work = work.groupby(keys, as_index=False)["value"].mean()
    else:
        raise ValueError(f"Unsupported dup_policy: {dup_policy}")

    return work[["trade_date", "stock_code", "factor_id", "value"]]


def resolve_selection(
    *,
    mapping: Dict[str, Any],
    tables: Optional[Iterable[str]] = None,
    groups: Optional[Iterable[str]] = None,
    factors: Optional[Iterable[str]] = None,
    include_tables: Optional[Iterable[str]] = None,
    exclude_tables: Optional[Iterable[str]] = None,
    strict_name: bool = True,
    allow_all_if_empty: bool = False,
) -> Tuple[Dict[str, List[str]], List[str], List[str], Dict[str, List[str]], List[str]]:
    warnings: List[str] = []
    missing_tables: List[str] = []
    missing_specs: List[str] = []
    skipped_tables: List[str] = []

    table_specs = [normalize_table_name(t) for t in _normalize_list(tables)]
    group_specs = _normalize_list(groups)
    factor_specs = _normalize_list(factors)
    include_norm = [normalize_table_name(t) for t in _normalize_list(include_tables)]
    exclude_norm = [normalize_table_name(t) for t in _normalize_list(exclude_tables)]

    if not table_specs and not group_specs and not factor_specs:
        if not allow_all_if_empty:
            raise ValueError("No candidate factors provided; use tables/groups/factors to select candidates.")

    scope_tables = table_specs or include_norm
    if table_specs and include_norm:
        filtered = [t for t in table_specs if t in include_norm]
        missing_tables.extend([t for t in table_specs if t not in include_norm])
        scope_tables = filtered
    if table_specs and not scope_tables:
        raise ValueError(
            f"Explicit tables resolved to none (input={table_specs}); "
            "abort to avoid running a full scan."
        )

    table_factors: Dict[str, List[str]] = {}

    expand_tables = table_specs or (not table_specs and not group_specs and not factor_specs and allow_all_if_empty)
    if expand_tables:
        table_map, skipped = build_table_factor_map(mapping, scope_tables, exclude_norm)
        table_factors = merge_table_maps(table_factors, table_map)
        skipped_tables.extend(skipped)
        if table_specs:
            missing_tables.extend([t for t in scope_tables if t not in table_map])

    if group_specs:
        group_map, skipped = build_table_factor_map(mapping, scope_tables, exclude_norm, group_specs)
        table_factors = merge_table_maps(table_factors, group_map)
        skipped_tables.extend(skipped)

    factor_index = build_factor_index(mapping, scope_tables, exclude_norm)
    if factor_specs:
        if not strict_name:
            for spec in factor_specs:
                if "::" in spec:
                    continue
                tables_for_spec = factor_index.get(spec, [])
                if len(tables_for_spec) > 1:
                    warnings.append(f"factor '{spec}' expands to tables: {sorted(set(tables_for_spec))}")

        factor_map, missing, _ = resolve_factor_specs(
            factor_specs, factor_index, strict_name, scope_tables, exclude_norm
        )
        missing_specs.extend(missing)

        expanded = {f"{table}::{field}" for table, field in flatten_table_factors(table_factors)}
        for table, fields in factor_map.items():
            for field in fields:
                key = f"{table}::{field}"
                if key in expanded:
                    warnings.append(f"factor '{key}' already included from tables/groups; skipped duplicate.")
                    continue
                table_factors = merge_table_maps(table_factors, {table: [field]})
                expanded.add(key)

    missing = {
        "missing_tables": sorted(set(missing_tables)),
        "missing_specs": sorted(set(missing_specs)),
        "missing_factors": [],
    }

    return table_factors, group_specs, factor_specs, warnings, missing


def _filter_by_keys(df: pd.DataFrame, keys_df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or keys_df.empty:
        return df.iloc[0:0]
    key_index = pd.MultiIndex.from_frame(keys_df[["trade_date", "stock_code"]])
    mask = pd.MultiIndex.from_frame(df[["trade_date", "stock_code"]]).isin(key_index)
    return df.loc[mask].reset_index(drop=True)


class CorrEngine:
    def __init__(
        self,
        *,
        mapping: Dict[str, Any],
        provider: Optional[LocalTestDBDataProvider] = None,
        include_tables: Optional[Iterable[str]] = None,
        exclude_tables: Optional[Iterable[str]] = None,
        strict_name: bool = True,
        sampling_cfg: Optional[Dict[str, Any]] = None,
        compute_cfg: Optional[Dict[str, Any]] = None,
        universe_cfg: Optional[Dict[str, Any]] = None,
        repo_root: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.mapping = mapping
        self.provider = provider
        self.include_tables = [normalize_table_name(t) for t in _normalize_list(include_tables)]
        self.exclude_tables = [normalize_table_name(t) for t in _normalize_list(exclude_tables)]
        self.strict_name = strict_name
        self.sampling_cfg = sampling_cfg or {}
        self.compute_cfg = compute_cfg or {}
        self.universe_cfg = universe_cfg or {}
        self.repo_root = repo_root or resolve_repo_root()
        self._suffix_pattern = _load_suffix_pattern(self.repo_root)
        self.logger = logger

    @classmethod
    def from_config(
        cls,
        cfg: Dict[str, Any],
        *,
        provider: Optional[LocalTestDBDataProvider] = None,
        mapping: Optional[Dict[str, Any]] = None,
        repo_root: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
    ) -> "CorrEngine":
        repo_root = repo_root or resolve_repo_root()
        mapping = mapping or cls._load_mapping(repo_root)
        include_tables = load_include_tables(cfg, repo_root)
        exclude_tables = cfg.get("corr_tool", {}).get("sources", {}).get("exclude_tables", [])
        strict_name = bool(cfg.get("corr_tool", {}).get("naming", {}).get("strict_name", True))
        return cls(
            mapping=mapping,
            provider=provider,
            include_tables=include_tables,
            exclude_tables=exclude_tables,
            strict_name=strict_name,
            sampling_cfg=cfg.get("corr_tool", {}).get("sampling", {}),
            compute_cfg=cfg.get("corr_tool", {}).get("compute", {}),
            universe_cfg=cfg.get("corr_tool", {}).get("universe", {}),
            repo_root=repo_root,
            logger=logger,
        )

    @staticmethod
    def _load_mapping(repo_root: Path) -> Dict[str, Any]:
        mapping_path = repo_root / "configs" / "field_mappings" / "factor_mapping.yaml"
        with mapping_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        return data or {}

    def pair(
        self,
        *,
        factor_a: str,
        factor_b: str,
        sampling_cfg: Optional[Dict[str, Any]] = None,
        compute_cfg: Optional[Dict[str, Any]] = None,
        universe_cfg: Optional[Dict[str, Any]] = None,
        use_progress: bool = False,
    ) -> CorrResult:
        target_key, target_map = self._resolve_single_factor(factor_a)
        other_key, other_map = self._resolve_single_factor(factor_b)
        table_factors = merge_table_maps(target_map, other_map)

        result = self._run_one_to_many(
            mode="pair",
            target_key=target_key,
            candidate_keys=[other_key],
            table_factors=table_factors,
            target_df=None,
            target_df_key=None,
            sampling_cfg=sampling_cfg,
            compute_cfg=compute_cfg,
            universe_cfg=universe_cfg,
            use_progress=use_progress,
        )
        return result

    def one_to_many(
        self,
        *,
        target: Optional[str] = None,
        target_df: Optional[pd.DataFrame] = None,
        target_key: str = "user::target",
        factors: Optional[List[str]] = None,
        tables: Optional[List[str]] = None,
        groups: Optional[List[str]] = None,
        allow_all_if_empty: bool = False,
        strict_name: Optional[bool] = None,
        dup_policy: str = "error",
        sampling_cfg: Optional[Dict[str, Any]] = None,
        compute_cfg: Optional[Dict[str, Any]] = None,
        universe_cfg: Optional[Dict[str, Any]] = None,
        use_progress: bool = False,
    ) -> CorrResult:
        if target_df is not None and target:
            raise ValueError("Provide either target or target_df, not both.")
        if target_df is None and not target:
            raise ValueError("one_to_many requires target or target_df.")

        target_key_resolved: Optional[str] = None
        target_map: Dict[str, List[str]] = {}
        if target_df is None:
            target_key_resolved, target_map = self._resolve_single_factor(target, strict_name=strict_name)
        else:
            target_key_resolved = target_key

        strict_name = self.strict_name if strict_name is None else strict_name

        table_factors, group_specs, factor_specs, warnings, missing = resolve_selection(
            mapping=self.mapping,
            tables=tables,
            groups=groups,
            factors=factors,
            include_tables=self.include_tables,
            exclude_tables=self.exclude_tables,
            strict_name=strict_name,
            allow_all_if_empty=allow_all_if_empty,
        )

        if target_map:
            table_factors = merge_table_maps(table_factors, target_map)

        factor_meta = build_factor_meta(table_factors)
        target_key_value = str(target_key_resolved)
        if target_key_value not in factor_meta:
            factor_meta[target_key_value] = self._build_user_meta(target_key_value)

        candidate_keys = sorted([k for k in factor_meta.keys() if k != target_key_value])
        if not candidate_keys:
            raise ValueError("No candidate factors resolved for one_to_many.")

        result = self._run_one_to_many(
            mode="one_to_many",
            target_key=target_key_value,
            candidate_keys=candidate_keys,
            table_factors=table_factors,
            target_df=target_df,
            target_df_key=target_key_value if target_df is not None else None,
            sampling_cfg=sampling_cfg,
            compute_cfg=compute_cfg,
            universe_cfg=universe_cfg,
            use_progress=use_progress,
            dup_policy=dup_policy,
            warnings=warnings,
            missing=missing,
            group_specs=group_specs,
            factor_specs=factor_specs,
        )
        return result

    def many_to_many(
        self,
        *,
        factors: Optional[List[str]] = None,
        tables: Optional[List[str]] = None,
        groups: Optional[List[str]] = None,
        allow_all_if_empty: bool = False,
        strict_name: Optional[bool] = None,
        sampling_cfg: Optional[Dict[str, Any]] = None,
        compute_cfg: Optional[Dict[str, Any]] = None,
        universe_cfg: Optional[Dict[str, Any]] = None,
        use_progress: bool = False,
    ) -> CorrResult:
        strict_name = self.strict_name if strict_name is None else strict_name
        table_factors, group_specs, factor_specs, warnings, missing = resolve_selection(
            mapping=self.mapping,
            tables=tables,
            groups=groups,
            factors=factors,
            include_tables=self.include_tables,
            exclude_tables=self.exclude_tables,
            strict_name=strict_name,
            allow_all_if_empty=allow_all_if_empty,
        )

        table_factors = {k: v for k, v in table_factors.items() if v}
        if not table_factors:
            raise ValueError("No factors resolved for analysis.")

        sampling_cfg = self._resolve_sampling_cfg(sampling_cfg)
        compute_cfg = self._resolve_compute_cfg(compute_cfg)
        universe_cfg = self._resolve_universe_cfg(universe_cfg)

        trade_dates = self._resolve_trade_dates(sampling_cfg)
        if not trade_dates:
            raise ValueError("No trade dates selected.")

        long_df = self._load_db_long_df(
            table_factors=table_factors,
            trade_dates=trade_dates,
            sampling_cfg=sampling_cfg,
            universe_cfg=universe_cfg,
            use_progress=use_progress,
        )
        if long_df.empty:
            raise ValueError("No factor data loaded after filtering.")

        long_df = self._prepare_long_df(long_df)
        long_df = self._apply_universe_and_sampling(
            long_df=long_df,
            target_df=None,
            target_key=None,
            sampling_cfg=sampling_cfg,
            universe_cfg=universe_cfg,
            trade_dates=trade_dates,
        )

        if long_df.empty:
            raise ValueError("No factor data left after universe/sampling filters.")

        if compute_cfg.get("debug_check_duplicates"):
            self._raise_on_duplicates(long_df, "factor long_df")

        wide_df = build_factor_matrix(long_df)
        stats, coverage = compute_corr_stats(
            wide_df,
            compute_cfg.get("method", "cross_sectional"),
            compute_cfg.get("corr_type", "spearman"),
            int(compute_cfg.get("min_periods", 30)),
        )

        corr_matrix = build_corr_matrix(stats)
        corr_matrix.index.name = "factor"

        available_keys = list(corr_matrix.index)
        missing_factors = sorted(set(build_factor_meta(table_factors).keys()) - set(available_keys))
        missing["missing_factors"] = missing_factors

        factor_meta = build_factor_meta(table_factors)
        high_corr_pairs = build_high_corr_pairs(
            corr_matrix, float(compute_cfg.get("high_corr_threshold_abs", 0.7))
        )
        high_corr_pairs = self._attach_factor_meta_safe(high_corr_pairs, factor_meta)

        if len(available_keys) > 1:
            recommended, clusters = recommend_by_cluster(
                corr_matrix, float(compute_cfg.get("high_corr_threshold_abs", 0.7))
            )
        else:
            recommended = available_keys
            clusters = []

        summary = {
            "mode": "many_to_many",
            "trade_dates": [to_ymd(d) for d in trade_dates],
            "selection": {
                "target_key": None,
                "candidate_keys": available_keys,
                "factor_keys": available_keys,
                "tables": sorted(table_factors.keys()),
            },
            "coverage": coverage,
            "group_specs": group_specs,
            "factor_specs": factor_specs,
        }

        recommendation = {
            "threshold": float(compute_cfg.get("high_corr_threshold_abs", 0.7)),
            "strategy": "cluster_min_avg_abs_corr",
            "recommended_factors": recommended,
            "clusters": clusters,
        }

        return CorrResult(
            mode="many_to_many",
            summary=summary,
            warnings=warnings,
            missing=missing,
            stats=stats,
            corr_table=pd.DataFrame(),
            corr_matrix=corr_matrix,
            high_corr_pairs=high_corr_pairs,
            recommendation=recommendation,
        )

    def _run_one_to_many(
        self,
        *,
        mode: str,
        target_key: str,
        candidate_keys: List[str],
        table_factors: Dict[str, List[str]],
        target_df: Optional[pd.DataFrame],
        target_df_key: Optional[str],
        sampling_cfg: Optional[Dict[str, Any]],
        compute_cfg: Optional[Dict[str, Any]],
        universe_cfg: Optional[Dict[str, Any]],
        use_progress: bool,
        dup_policy: str = "error",
        warnings: Optional[List[str]] = None,
        missing: Optional[Dict[str, List[str]]] = None,
        group_specs: Optional[List[str]] = None,
        factor_specs: Optional[List[str]] = None,
    ) -> CorrResult:
        warnings = warnings or []
        missing = missing or {"missing_tables": [], "missing_specs": [], "missing_factors": []}

        sampling_cfg = self._resolve_sampling_cfg(sampling_cfg)
        compute_cfg = self._resolve_compute_cfg(compute_cfg)
        universe_cfg = self._resolve_universe_cfg(universe_cfg)

        normalized_target_df: Optional[pd.DataFrame] = None
        trade_dates: List[pd.Timestamp]

        if target_df is not None:
            normalized_target_df = normalize_user_factor_df(
                target_df,
                target_key=target_df_key or target_key,
                suffix_pattern=self._suffix_pattern,
                dup_policy=dup_policy,
            )
            apply_sampling = bool(sampling_cfg.get("apply_sampling_on_target_df", True))
            trade_dates = self._resolve_trade_dates_from_target(
                normalized_target_df,
                sampling_cfg,
                apply_sampling=apply_sampling,
            )
        else:
            trade_dates = self._resolve_trade_dates(sampling_cfg)

        if not trade_dates:
            raise ValueError("No trade dates selected.")

        long_df = self._load_db_long_df(
            table_factors=table_factors,
            trade_dates=trade_dates,
            sampling_cfg=sampling_cfg,
            universe_cfg=universe_cfg,
            use_progress=use_progress,
        )
        if long_df.empty and normalized_target_df is None:
            raise ValueError("No factor data loaded after filtering.")

        if not long_df.empty:
            long_df = self._prepare_long_df(long_df)
        if normalized_target_df is not None:
            normalized_target_df = self._prepare_long_df(normalized_target_df)

        combined = long_df
        if normalized_target_df is not None:
            combined = pd.concat([combined, normalized_target_df], ignore_index=True)

        combined = self._apply_universe_and_sampling(
            long_df=combined,
            target_df=normalized_target_df,
            target_key=target_key,
            sampling_cfg=sampling_cfg,
            universe_cfg=universe_cfg,
            trade_dates=trade_dates,
        )

        if combined.empty:
            raise ValueError("No data left after universe/sampling filters.")

        if compute_cfg.get("debug_check_duplicates"):
            self._raise_on_duplicates(combined, "combined long_df")

        wide_df = build_factor_matrix(combined)
        available_cols = [c for c in wide_df.columns if c not in ("trade_date", "stock_code")]
        if target_key not in available_cols:
            raise ValueError(f"Target factor not available in data: {target_key}")
        candidate_cols = [k for k in candidate_keys if k in available_cols and k != target_key]

        missing["missing_factors"] = sorted(set(candidate_keys) - set(candidate_cols))
        if not candidate_cols:
            raise ValueError("No candidate factors available in data.")

        stats, coverage = compute_corr_one_to_many(
            wide_df,
            target_key,
            candidate_cols,
            compute_cfg.get("method", "cross_sectional"),
            compute_cfg.get("corr_type", "spearman"),
            int(compute_cfg.get("min_periods", 30)),
        )

        corr_table = self._build_corr_table_for_target(stats, target_key, coverage)
        factor_meta = build_factor_meta(table_factors)
        if target_key not in factor_meta:
            factor_meta[target_key] = self._build_user_meta(target_key)
        corr_table = self._attach_factor_meta_safe(corr_table, factor_meta)

        threshold = float(compute_cfg.get("high_corr_threshold_abs", 0.7))
        high_corr_pairs = self._build_high_corr_pairs_from_table(corr_table, threshold)

        summary_candidate_keys = candidate_cols if mode != "pair" else [target_key] + candidate_cols
        summary = {
            "mode": mode,
            "trade_dates": [to_ymd(d) for d in trade_dates],
            "selection": {
                "target_key": target_key,
                "candidate_keys": summary_candidate_keys,
                "factor_keys": [target_key] + candidate_cols,
                "tables": sorted(table_factors.keys()),
            },
            "coverage": coverage,
            "group_specs": group_specs or [],
            "factor_specs": factor_specs or [],
        }

        return CorrResult(
            mode=mode,
            summary=summary,
            warnings=warnings,
            missing=missing,
            stats=stats,
            corr_table=corr_table,
            corr_matrix=pd.DataFrame(),
            high_corr_pairs=high_corr_pairs,
            recommendation={},
        )

    def _load_db_long_df(
        self,
        *,
        table_factors: Dict[str, List[str]],
        trade_dates: List[pd.Timestamp],
        sampling_cfg: Dict[str, Any],
        universe_cfg: Dict[str, Any],
        use_progress: bool,
    ) -> pd.DataFrame:
        if not table_factors:
            return pd.DataFrame()
        if self.provider is None:
            raise ValueError("DB provider is required to load factor data.")
        return load_long_df(
            self.provider,
            table_factors,
            trade_dates,
            sampling_cfg,
            universe_cfg,
            use_progress,
            apply_forbid_pool=False,
            random_stocks_per_date=0,
            logger=self.logger,
        )

    def _prepare_long_df(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
        if out["trade_date"].isna().any():
            raise ValueError("trade_date conversion failed after data load.")
        out["stock_code"] = _normalize_stock_codes(out["stock_code"], self._suffix_pattern)
        return out

    def _resolve_trade_dates(self, sampling_cfg: Dict[str, Any]) -> List[pd.Timestamp]:
        calendar = fetch_trading_calendar()
        return select_trade_dates(calendar, sampling_cfg)

    def _resolve_trade_dates_from_target(
        self,
        target_df: pd.DataFrame,
        sampling_cfg: Dict[str, Any],
        *,
        apply_sampling: bool,
    ) -> List[pd.Timestamp]:
        dates = sorted(target_df["trade_date"].dropna().unique().tolist())
        if not dates:
            return []
        if not apply_sampling:
            return dates
        local_cfg = dict(sampling_cfg)
        mode = local_cfg.get("mode", "fixed_years")
        if mode == "fixed_years":
            years = local_cfg.get("years") or []
            if not years:
                years = sorted({pd.Timestamp(d).year for d in dates})
                local_cfg["years"] = years
        elif mode == "date_range":
            start_date = local_cfg.get("start_date")
            end_date = local_cfg.get("end_date")
            if not start_date or not end_date:
                local_cfg["start_date"] = pd.Timestamp(min(dates)).strftime("%Y%m%d")
                local_cfg["end_date"] = pd.Timestamp(max(dates)).strftime("%Y%m%d")
        calendar = pd.Series(dates)
        return select_trade_dates(calendar, local_cfg)

    def _resolve_sampling_cfg(self, override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if override is None:
            return dict(self.sampling_cfg)
        merged = dict(self.sampling_cfg)
        merged.update(override)
        return merged

    def _resolve_compute_cfg(self, override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if override is None:
            return dict(self.compute_cfg)
        merged = dict(self.compute_cfg)
        merged.update(override)
        return merged

    def _resolve_universe_cfg(self, override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if override is None:
            return dict(self.universe_cfg)
        merged = dict(self.universe_cfg)
        merged.update(override)
        return merged

    def _apply_universe_and_sampling(
        self,
        *,
        long_df: pd.DataFrame,
        target_df: Optional[pd.DataFrame],
        target_key: Optional[str],
        sampling_cfg: Dict[str, Any],
        universe_cfg: Dict[str, Any],
        trade_dates: List[pd.Timestamp],
    ) -> pd.DataFrame:
        if long_df.empty:
            return long_df

        forbid_df = pd.DataFrame()
        if universe_cfg.get("exclude_forbid_pool"):
            if self.provider is None:
                raise ValueError("DB provider is required to fetch forbid pool data.")
            forbid_df = fetch_forbid_pool(
                self.provider,
                universe_cfg.get("forbid_pool_table"),
                trade_dates,
            )

        if not forbid_df.empty:
            long_df = filter_forbid_pool(long_df, forbid_df)
            if target_df is not None:
                target_df = filter_forbid_pool(target_df, forbid_df)

        random_stocks = sampling_cfg.get("random_stocks_per_date")
        random_seed = int(sampling_cfg.get("random_seed", 42))

        if target_df is not None:
            base_df = target_df
            if random_stocks:
                base_df = sample_stocks_per_date(base_df, random_stocks, random_seed)
            base_keys = base_df[["trade_date", "stock_code"]].drop_duplicates()
            long_df = _filter_by_keys(long_df, base_keys)
        elif target_key:
            base_df = long_df[long_df["factor_id"] == target_key]
            if base_df.empty:
                raise ValueError("Target factor has no data after filters.")
            if random_stocks:
                base_df = sample_stocks_per_date(base_df, random_stocks, random_seed)
            base_keys = base_df[["trade_date", "stock_code"]].drop_duplicates()
            long_df = _filter_by_keys(long_df, base_keys)
        else:
            long_df = sample_stocks_per_date(long_df, random_stocks, random_seed)

        return long_df

    def _resolve_single_factor(
        self,
        spec: str,
        *,
        strict_name: Optional[bool] = None,
    ) -> Tuple[str, Dict[str, List[str]]]:
        factor_index = build_factor_index(self.mapping, self.include_tables, self.exclude_tables)
        resolved_strict = self.strict_name if strict_name is None else strict_name
        table_map, missing, _ = resolve_factor_specs(
            [spec], factor_index, resolved_strict, self.include_tables, self.exclude_tables
        )
        if missing:
            raise ValueError(f"Factor not found or excluded: {', '.join(missing)}")
        pairs = flatten_table_factors(table_map)
        if len(pairs) != 1:
            raise ValueError(f"Factor spec must resolve to a single factor: {spec}")
        table, field = pairs[0]
        return f"{table}::{field}", table_map

    @staticmethod
    def _build_user_meta(factor_key: str) -> Dict[str, str]:
        if "::" in factor_key:
            table, field = factor_key.split("::", 1)
        else:
            table, field = "user", factor_key
        return {"source_table": table, "field_name": field}

    @staticmethod
    def _build_corr_table_for_target(
        stats: pd.DataFrame,
        target_key: str,
        coverage: Dict[str, Any],
    ) -> pd.DataFrame:
        if stats.empty:
            return pd.DataFrame()
        table = stats.reset_index().rename(columns={"index": "factor_b"})
        table.insert(0, "factor_a", target_key)
        table["n_groups"] = table.get("n_groups", coverage.get("n_groups", 0))
        table["group_unit"] = coverage.get("group_unit")
        table["avg_group_size"] = coverage.get("avg_group_size")
        return table

    @staticmethod
    def _build_high_corr_pairs_from_table(
        corr_table: pd.DataFrame,
        threshold: float,
    ) -> pd.DataFrame:
        if corr_table.empty:
            return corr_table
        result = corr_table.copy()
        result["abs_corr"] = result["corr_mean"].abs()
        result = result[result["abs_corr"] >= threshold]
        keep_cols = ["factor_a", "factor_b", "corr_mean", "abs_corr"]
        for col in ("source_table_a", "source_table_b"):
            if col in result.columns:
                keep_cols.append(col)
        result = result[keep_cols]
        return result.sort_values("abs_corr", ascending=False).reset_index(drop=True)

    @staticmethod
    def _raise_on_duplicates(df: pd.DataFrame, label: str) -> None:
        dup_mask = df.duplicated(subset=["trade_date", "stock_code", "factor_id"], keep=False)
        if not dup_mask.any():
            return
        sample = (
            df.loc[dup_mask, ["trade_date", "stock_code", "factor_id"]]
            .head(5)
            .to_dict(orient="records")
        )
        raise ValueError(f"{label} has duplicate keys; sample: {sample}")

    @staticmethod
    def _attach_factor_meta_safe(
        df: pd.DataFrame,
        factor_meta: Dict[str, Dict[str, str]],
    ) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        df["source_table_a"] = df["factor_a"].map(
            lambda key: factor_meta.get(key, {}).get("source_table")
        )
        df["source_table_b"] = df["factor_b"].map(
            lambda key: factor_meta.get(key, {}).get("source_table")
        )
        return df
