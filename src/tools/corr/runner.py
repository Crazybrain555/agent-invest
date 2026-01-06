from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

from src.data_service.data_loading.local_testdb_data import LocalTestDBDataProvider
from src.tools.corr.api import CorrEngine
from src.tools.corr.cache import build_signature, update_edges_cache, update_factor_registry
from src.tools.corr.config import load_corr_config, load_include_tables, resolve_repo_root, snapshot_config
from src.tools.corr.formats import build_excel_corr_matrix, build_excel_corr_table
from src.tools.corr.naming import build_focus_tag, build_group_tag, build_run_dir_name
from src.tools.corr.report import write_excel, write_parquet, write_recommendation, write_summary
from src.tools.corr.sampling import build_sample_tag, now_ts
from src.tools.corr.sources import load_factor_mapping, normalize_table_name
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

MODE_ALIASES = {
    "train-precheck": "many_to_many",
    "check-new-factor": "one_to_many",
    "adhoc": "many_to_many",
}


def _split_list(values: Optional[List[str]]) -> List[str]:
    items: List[str] = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if part:
                items.append(part)
    return items


def _apply_sampling_overrides(cfg: dict, args: argparse.Namespace) -> None:
    sampling = cfg["corr_tool"]["sampling"]
    if args.years:
        years = [int(x) for x in args.years.split(",") if x.strip()]
        sampling["mode"] = "fixed_years"
        sampling["years"] = years
    if args.start_date or args.end_date:
        sampling["mode"] = "date_range"
        if args.start_date:
            sampling["start_date"] = args.start_date
        if args.end_date:
            sampling["end_date"] = args.end_date
    if args.random_days_per_year is not None:
        sampling["random_days_per_year"] = args.random_days_per_year
    if args.random_stocks_per_date is not None:
        sampling["random_stocks_per_date"] = args.random_stocks_per_date
    if args.random_seed is not None:
        sampling["random_seed"] = args.random_seed


def _apply_compute_overrides(cfg: dict, args: argparse.Namespace) -> None:
    compute = cfg["corr_tool"]["compute"]
    if args.method:
        compute["method"] = args.method
    if args.corr_type:
        compute["corr_type"] = args.corr_type
    if args.min_periods is not None:
        compute["min_periods"] = args.min_periods
    if args.threshold is not None:
        compute["high_corr_threshold_abs"] = args.threshold


def _apply_output_overrides(cfg: dict, args: argparse.Namespace) -> None:
    output = cfg["corr_tool"]["output"]
    if args.out_root:
        output["root"] = args.out_root


def _apply_universe_overrides(cfg: dict, args: argparse.Namespace) -> None:
    universe = cfg["corr_tool"]["universe"]
    if args.no_forbid_pool:
        universe["exclude_forbid_pool"] = False


def _apply_cache_overrides(cfg: dict, args: argparse.Namespace) -> None:
    cache_cfg = cfg["corr_tool"]["cache"]
    if args.no_cache:
        cache_cfg["enable"] = False


def _apply_naming_overrides(cfg: dict, args: argparse.Namespace) -> None:
    naming = cfg["corr_tool"].setdefault("naming", {})
    if args.strict_name:
        naming["strict_name"] = True


def _resolve_tables(cfg: dict, args: argparse.Namespace, repo_root: Path) -> Tuple[List[str], List[str]]:
    include_tables = load_include_tables(cfg, repo_root)
    exclude_tables = list(cfg["corr_tool"].get("sources", {}).get("exclude_tables", []))

    include_tables.extend(_split_list(args.include_table))
    exclude_tables.extend(_split_list(args.exclude_table))

    include_tables = [normalize_table_name(t) for t in include_tables]
    exclude_tables = [normalize_table_name(t) for t in exclude_tables]

    if args.tables:
        table_filter = {normalize_table_name(t) for t in args.tables.split(",") if t.strip()}
        if include_tables:
            include_tables = [t for t in include_tables if t in table_filter]
        else:
            include_tables = list(table_filter)

    return include_tables, exclude_tables


def _load_factor_mapping(repo_root: Path) -> dict:
    mapping_path = repo_root / "configs" / "field_mappings" / "factor_mapping.yaml"
    return load_factor_mapping(mapping_path)


def _apply_legacy_coverage(df: pd.DataFrame, coverage: dict) -> pd.DataFrame:
    if df.empty:
        return df
    updated = df.copy()
    if "n_groups" not in updated.columns:
        updated["n_groups"] = coverage.get("n_groups", coverage.get("n_dates", 0))
    if "group_unit" not in updated.columns:
        updated["group_unit"] = coverage.get("group_unit")
    if "avg_group_size" not in updated.columns:
        updated["avg_group_size"] = coverage.get("avg_group_size", coverage.get("avg_n_stocks"))
    if "n_dates" not in updated.columns and "n_groups" in updated.columns:
        updated["n_dates"] = updated["n_groups"]
    if "avg_n_stocks" not in updated.columns and "avg_group_size" in updated.columns:
        updated["avg_n_stocks"] = updated["avg_group_size"]
    return updated


def run(args: argparse.Namespace) -> None:
    repo_root = resolve_repo_root()
    cfg = load_corr_config(args.config)

    _apply_sampling_overrides(cfg, args)
    _apply_compute_overrides(cfg, args)
    _apply_output_overrides(cfg, args)
    _apply_universe_overrides(cfg, args)
    _apply_cache_overrides(cfg, args)
    _apply_naming_overrides(cfg, args)

    corr_cfg = cfg["corr_tool"]
    include_tables, exclude_tables = _resolve_tables(cfg, args, repo_root)
    if args.tables and not include_tables:
        raise ValueError(
            f"--tables={args.tables} did not match any include tables; abort to avoid running a full scan."
        )

    mapping = _load_factor_mapping(repo_root)
    strict_name = bool(corr_cfg.get("naming", {}).get("strict_name", False))

    mode = MODE_ALIASES.get(args.mode, args.mode)
    if mode not in {"pair", "one_to_many", "many_to_many"}:
        raise ValueError(f"Unsupported mode: {mode}")

    factor_specs = _split_list(args.factor)
    group_specs = _split_list(args.group)

    provider = LocalTestDBDataProvider()
    engine = CorrEngine(
        mapping=mapping,
        provider=provider,
        include_tables=include_tables,
        exclude_tables=exclude_tables,
        strict_name=strict_name,
        sampling_cfg=corr_cfg.get("sampling", {}),
        compute_cfg=corr_cfg.get("compute", {}),
        universe_cfg=corr_cfg.get("universe", {}),
        repo_root=repo_root,
        logger=logger,
    )

    use_progress = bool(corr_cfg.get("output", {}).get("progress_bar", True)) and not args.no_progress

    if mode == "pair":
        spec_a = args.factor_a or (factor_specs[0] if factor_specs else None)
        spec_b = args.factor_b or (factor_specs[1] if len(factor_specs) > 1 else None)
        if not spec_a or not spec_b:
            raise ValueError("pair mode requires --factor-a and --factor-b (or two --factor)")
        result = engine.pair(factor_a=spec_a, factor_b=spec_b, use_progress=use_progress)
    elif mode == "one_to_many":
        target_spec = args.target or (factor_specs[0] if factor_specs else None)
        if not target_spec:
            raise ValueError("one_to_many mode requires --target or --factor")
        candidate_specs = []
        if args.candidates:
            candidate_specs.extend([c.strip() for c in args.candidates.split(",") if c.strip()])
        candidate_specs.extend(factor_specs[1:])
        result = engine.one_to_many(
            target=target_spec,
            factors=candidate_specs or None,
            groups=group_specs or None,
            allow_all_if_empty=True,
            use_progress=use_progress,
        )
    else:
        result = engine.many_to_many(
            factors=factor_specs or None,
            groups=group_specs or None,
            allow_all_if_empty=True,
            use_progress=use_progress,
        )

    selection = result.summary.get("selection", {})
    factor_keys = selection.get("factor_keys", [])
    candidate_keys = selection.get("candidate_keys", [])
    target_factor_key = selection.get("target_key")
    used_tables = selection.get("tables", [])

    if mode == "pair" and len(candidate_keys) < 2:
        candidate_keys = factor_keys

    sample_tag = build_sample_tag(corr_cfg.get("sampling", {}))
    ts = now_ts()
    focus_tag = build_focus_tag(
        mode,
        group_specs,
        factor_specs,
        target_factor_key,
        candidate_keys,
        factor_keys,
    )
    group_tag = build_group_tag(group_specs)
    run_dir_name = build_run_dir_name(focus_tag, mode, ts)

    output_root = Path(corr_cfg["output"]["root"])
    if not output_root.is_absolute():
        output_root = repo_root / output_root
    run_dir = output_root / run_dir_name

    skipped_tables = sorted(set(include_tables) - set(used_tables)) if include_tables else []
    coverage = result.summary.get("coverage", {})

    summary = {
        "mode": mode,
        "config": snapshot_config(cfg),
        "sampling": {
            "selected_dates": result.summary.get("trade_dates", []),
            "sample_tag": sample_tag,
        },
        "tables": {
            "include_tables": include_tables,
            "exclude_tables": exclude_tables,
            "used_tables": used_tables,
            "skipped_tables": skipped_tables,
            "missing_tables": sorted(set(result.missing.get("missing_tables", []))),
        },
        "factors": {
            "count": len(factor_keys),
            "factor_keys": factor_keys,
            "missing_specs": sorted(set(result.missing.get("missing_specs", []))),
            "missing_factors": sorted(set(result.missing.get("missing_factors", []))),
        },
        "output": {
            "focus_tag": focus_tag,
            "group_tag": group_tag or None,
            "run_dir_name": run_dir_name,
        },
        "coverage": coverage,
        "warnings": result.warnings,
    }

    summary_path = run_dir / "summary.json"
    write_summary(summary_path, summary)

    if mode in {"pair", "one_to_many"}:
        corr_table = _apply_legacy_coverage(result.corr_table, coverage)
        write_parquet(run_dir / "corr_table.parquet", corr_table)
        excel_df = build_excel_corr_table(corr_table)
        try:
            write_excel(
                run_dir / "corr_table.xlsx",
                excel_df,
                sheet_name="corr_table",
                index=False,
                freeze_panes="A2",
                color_scale_range=(2, 3, len(excel_df) + 1, 3),
            )
        except RuntimeError as exc:
            logger.warning("Excel output skipped: %s", exc)
        write_parquet(run_dir / "high_corr_pairs.parquet", result.high_corr_pairs)
    else:
        max_matrix = int(corr_cfg.get("compute", {}).get("max_factors_full_matrix", 400))
        corr_matrix = result.corr_matrix
        if len(factor_keys) <= max_matrix:
            write_parquet(run_dir / "corr_matrix.parquet", corr_matrix.reset_index())
            excel_df = build_excel_corr_matrix(corr_matrix)
            n_rows, n_cols = excel_df.shape
            if n_rows > 0 and n_cols > 1:
                color_range = (2, 2, n_rows + 1, n_cols)
            else:
                color_range = None
            try:
                write_excel(
                    run_dir / "corr_matrix.xlsx",
                    excel_df,
                    sheet_name="corr_matrix",
                    index=False,
                    freeze_panes="B2",
                    color_scale_range=color_range,
                )
            except RuntimeError as exc:
                logger.warning("Excel output skipped: %s", exc)
        write_parquet(run_dir / "high_corr_pairs.parquet", result.high_corr_pairs)
        if result.recommendation:
            write_recommendation(run_dir / "recommendation.yaml", result.recommendation)

    cache_cfg = corr_cfg.get("cache", {})
    if cache_cfg.get("enable"):
        signature_info = build_signature(
            corr_cfg.get("version"),
            include_tables,
            corr_cfg.get("sampling", {}),
            corr_cfg.get("universe", {}),
            corr_cfg.get("compute", {}).get("method"),
            corr_cfg.get("compute", {}).get("corr_type"),
            int(corr_cfg.get("compute", {}).get("min_periods", 30)),
            factor_keys,
        )
        summary["signature"] = signature_info
        write_summary(summary_path, summary)

        registry_path = Path(cache_cfg["registry_path"])
        if not registry_path.is_absolute():
            registry_path = repo_root / registry_path
        update_factor_registry(registry_path, factor_keys)

        max_matrix = int(corr_cfg.get("compute", {}).get("max_factors_full_matrix", 400))
        if mode == "many_to_many" and not result.stats.empty and len(factor_keys) <= max_matrix:
            edges_df = result.stats.reset_index()
            edges_df.columns = ["factor_a", "factor_b", "corr_mean", "corr_median", "corr_std", "n_groups"]
            edges_df = edges_df[edges_df["factor_a"] < edges_df["factor_b"]]
            edges_df = edges_df[["factor_a", "factor_b", "corr_mean", "corr_median", "n_groups"]]
            edges_df = edges_df.rename(columns={"n_groups": "n_dates"})
        else:
            edges_df = result.high_corr_pairs[["factor_a", "factor_b", "corr_mean"]].copy()
            edges_df["corr_median"] = None
            edges_df["n_dates"] = coverage.get("n_dates", coverage.get("n_groups"))

        edges_df["avg_n_stocks"] = coverage.get("avg_n_stocks", coverage.get("avg_group_size"))
        edges_df["signature"] = signature_info["signature"]
        edges_df["created_at"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
        edges_path = Path(cache_cfg["edges_cache_path"])
        if not edges_path.is_absolute():
            edges_path = repo_root / edges_path
        update_edges_cache(edges_path, edges_df)

    logger.info("Corr tool run completed: %s", run_dir)
