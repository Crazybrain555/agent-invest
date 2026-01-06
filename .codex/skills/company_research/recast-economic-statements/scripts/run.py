#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recast-economic-statements skill runner (v0.1)."""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

SKILL_NAME = "recast-economic-statements"
DEFAULT_TIMEZONE = "America/New_York"


def _find_repo_root(start: Path) -> Path:
    for parent in [start] + list(start.parents):
        if (parent / "company_research_runtime").exists():
            return parent
    return start.parents[4]


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from company_research_runtime import (  # noqa: E402
    append_evidence,
    append_question,
    atomic_write_parquet,
    atomic_write_yaml,
    build_needs,
    build_run_meta,
    build_run_result,
    company_paths,
    default_run_id,
    fingerprint_data,
    hash_file,
    update_artifacts_state,
    write_meta,
    write_needs,
    write_result,
)
from recast import build_core_metrics, build_economic_statements, load_policy  # noqa: E402


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _parse_as_of(value: str | None) -> date | str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return value


def _as_of_str(value: date | str | None, *, tz: str) -> str:
    if value is None:
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _generate_record_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


def _append_question_record(*, questions_path: Path, ticker: str, question: str) -> None:
    record = {
        "id": _generate_record_id("Q"),
        "created_at": datetime.now(timezone.utc).date().isoformat(),
        "ticker": ticker,
        "skill": SKILL_NAME,
        "question": question,
    }
    append_question(questions_path, record)


def _append_evidence_record(
    *,
    evidence_path: Path,
    ticker: str,
    claim: str,
    sources: list[dict[str, Any]],
    confidence: float = 0.75,
) -> None:
    record = {
        "id": _generate_record_id("E"),
        "created_at": datetime.now(timezone.utc).date().isoformat(),
        "ticker": ticker,
        "skill": SKILL_NAME,
        "claim": claim,
        "confidence": confidence,
        "sources": sources,
    }
    append_evidence(evidence_path, record)


def _should_skip(
    *,
    force_refresh: bool,
    policy_version: str,
    policy_hash: str,
    facts_hash: str | None,
    periods_hash: str | None,
    outputs: list[Path],
    recast_policy_path: Path,
) -> bool:
    if force_refresh:
        return False
    if not all(path.exists() for path in outputs):
        return False
    if not recast_policy_path.exists():
        return False
    existing = _load_yaml(recast_policy_path)
    if existing.get("policy_version") != policy_version:
        return False
    inputs = existing.get("inputs") or {}
    if inputs.get("policy_hash") != policy_hash:
        return False
    if facts_hash and inputs.get("facts_hash") != facts_hash:
        return False
    if periods_hash and inputs.get("periods_hash") != periods_hash:
        return False
    return True


def run(
    ticker: str,
    *,
    as_of: date | str | None = None,
    policy_version: str = "v0.1",
    policy_path: Path | None = None,
    force_refresh: bool = False,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    ticker = ticker.upper()
    as_of_value = as_of or date.today()
    as_of_label = _as_of_str(as_of_value, tz=timezone_name)

    paths = company_paths(ticker)
    paths.ensure_base_dirs()

    run_id = default_run_id(timezone=timezone_name)
    run_dir = paths.run_dir(run_id)
    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    meta = build_run_meta(
        skill=SKILL_NAME,
        ticker=ticker,
        run_id=run_id,
        as_of=as_of_value,
        timezone=timezone_name,
        policy_version=policy_version,
        force_refresh=force_refresh,
    )
    write_meta(run_dir, meta)

    warnings: list[str] = []
    missing: list[str] = []

    atlas_dir = paths.current_dir / "xbrl_atlas"
    economic_dir = paths.current_dir / "economic"
    economic_dir.mkdir(parents=True, exist_ok=True)

    facts_path = atlas_dir / "facts.parquet"
    periods_path = atlas_dir / "periods.yaml"
    nodes_path = atlas_dir / "nodes.parquet"
    edges_path = atlas_dir / "edges.parquet"

    required_missing = [
        path
        for path in [facts_path, periods_path, nodes_path, edges_path]
        if not path.exists()
    ]

    output_paths = [
        economic_dir / "recast_policy.yaml",
        economic_dir / "economic_statements.parquet",
        economic_dir / "core_metrics.parquet",
    ]
    output_labels = [
        "current/economic/recast_policy.yaml",
        "current/economic/economic_statements.parquet",
        "current/economic/core_metrics.parquet",
    ]

    if required_missing:
        missing = [
            str(path.relative_to(paths.current_dir))
            if path.is_absolute() and paths.current_dir in path.parents
            else str(path)
            for path in required_missing
        ]
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": str(path.relative_to(paths.current_dir)),
                    "producer_skill": "extract-xbrl-timeseries",
                    "reason": "Required atlas artifact missing",
                }
                for path in required_missing
            ],
            suggested_plan=["extract-xbrl-timeseries", SKILL_NAME],
            priority="high",
        )
        write_needs(run_dir, needs)
        status = "blocked"
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status=status,
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=output_labels,
        )
        write_result(run_dir, result)
        return result

    policy_path = policy_path or Path(__file__).with_name("recast_policy_default.yaml")
    policy = load_policy(policy_path)
    policy["policy_version"] = policy_version
    policy_hash = fingerprint_data(policy)

    facts_hash = hash_file(facts_path) if facts_path.exists() else None
    periods_hash = hash_file(periods_path) if periods_path.exists() else None

    if _should_skip(
        force_refresh=force_refresh,
        policy_version=policy_version,
        policy_hash=policy_hash,
        facts_hash=facts_hash,
        periods_hash=periods_hash,
        outputs=output_paths,
        recast_policy_path=output_paths[0],
    ):
        status = "skipped"
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status=status,
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=output_labels,
        )
        write_result(run_dir, result)
        return result

    facts_df = pd.read_parquet(facts_path)
    periods_payload = _load_yaml(periods_path)

    if facts_df.empty or not periods_payload.get("periods"):
        status = "blocked"
        warnings.append("facts.parquet empty or periods.yaml missing entries")
        missing.append("xbrl_atlas/facts.parquet or periods.yaml")
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": "current/xbrl_atlas/facts.parquet",
                    "producer_skill": "extract-xbrl-timeseries",
                    "reason": "Facts or periods missing",
                }
            ],
            suggested_plan=["extract-xbrl-timeseries", SKILL_NAME],
            priority="high",
        )
        write_needs(run_dir, needs)
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status=status,
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=output_labels,
        )
        write_result(run_dir, result)
        return result

    economic_df, mapping_summary = build_economic_statements(facts_df, periods_payload, policy)
    core_df = build_core_metrics(economic_df, policy)

    output_policy = {
        "policy_version": policy_version,
        "created_at": date.today().isoformat(),
        "inputs": {
            "facts_path": str(facts_path),
            "periods_path": str(periods_path),
            "policy_path": str(policy_path),
            "facts_hash": facts_hash,
            "periods_hash": periods_hash,
            "policy_hash": policy_hash,
        },
        "defaults": policy.get("defaults") or {},
        "maintenance_capex_method": policy.get("maintenance_capex_method") or {},
        "owner_earnings_definition": policy.get("owner_earnings_definition"),
        "invested_capital_definition": policy.get("invested_capital_definition"),
        "mapping_rules": [],
    }

    for rule in policy.get("mapping_rules") or []:
        target = rule.get("target")
        if not target:
            continue
        summary = mapping_summary.get(target, {})
        output_policy["mapping_rules"].append(
            {
                "target": target,
                "selector": {
                    "statement_type": rule.get("statement_type"),
                    "concept_matches": rule.get("concept_matches") or [],
                    "label_matches": rule.get("label_matches") or [],
                },
                "chosen_labels": summary.get("chosen_labels") or [],
                "chosen_concepts": summary.get("chosen_concepts") or [],
                "match_types": summary.get("match_types") or {},
                "matched_periods": summary.get("matched_periods"),
                "total_periods": summary.get("total_periods"),
                "fallback_used": summary.get("fallback_used", False),
                "rationale": rule.get("rationale"),
            }
        )

    atomic_write_yaml(outputs_dir / "recast_policy.yaml", output_policy)
    atomic_write_parquet(outputs_dir / "economic_statements.parquet", economic_df)
    atomic_write_parquet(outputs_dir / "core_metrics.parquet", core_df)

    atomic_write_yaml(output_paths[0], output_policy)
    atomic_write_parquet(output_paths[1], economic_df)
    atomic_write_parquet(output_paths[2], core_df)

    status = "ok"
    if economic_df.empty or core_df.empty:
        status = "blocked"
        warnings.append("No economic statements generated")
    else:
        latest = core_df.iloc[-1]
        if pd.isna(latest.get("cfo")) or pd.isna(latest.get("capex")):
            status = "partial"
            warnings.append("CFO or capex missing for latest period")
            _append_question_record(
                questions_path=paths.questions_jsonl,
                ticker=ticker,
                question="CFO or capex missing in latest period; check CF labels or policy",
            )
        if pd.isna(latest.get("owner_earnings")):
            status = "partial"
            warnings.append("Owner earnings could not be calculated for latest period")

    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/economic/recast_policy.yaml",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=output_paths[0],
        extra={"status": status, "count": len(output_policy["mapping_rules"])},
    )
    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/economic/economic_statements.parquet",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=output_paths[1],
        extra={"status": status, "count": len(economic_df)},
    )
    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/economic/core_metrics.parquet",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=output_paths[2],
        extra={"status": status, "count": len(core_df)},
    )

    if status in {"ok", "partial"}:
        _append_evidence_record(
            evidence_path=paths.evidence_jsonl,
            ticker=ticker,
            claim=f"Recasted economic statements with policy {policy_version}",
            sources=[{"type": "xbrl_atlas", "path": str(facts_path)}],
            confidence=0.8 if status == "ok" else 0.6,
        )

    result = build_run_result(
        skill=SKILL_NAME,
        ticker=ticker,
        run_id=run_id,
        status=status,
        as_of=as_of_value,
        timezone=timezone_name,
        missing=missing,
        warnings=warnings,
        outputs=output_labels,
    )
    write_result(run_dir, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="recast-economic-statements runner")
    parser.add_argument("ticker", help="Stock ticker symbol")
    parser.add_argument("--as-of", dest="as_of", help="Snapshot date YYYY-MM-DD")
    parser.add_argument("--policy-version", default="v0.1")
    parser.add_argument("--policy-path", type=Path)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    args = parser.parse_args()

    run(
        args.ticker,
        as_of=_parse_as_of(args.as_of),
        policy_version=args.policy_version,
        policy_path=args.policy_path,
        force_refresh=args.force_refresh,
        timezone_name=args.timezone,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
