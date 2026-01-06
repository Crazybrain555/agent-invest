#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""extract-xbrl-timeseries skill runner (v0.1 shallow)."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

SKILL_NAME = "extract-xbrl-timeseries"
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
    update_artifacts_state,
    write_meta,
    write_needs,
    write_result,
)
from build_atlas_minimal import build_minimal_atlas  # noqa: E402


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _load_payload(path: Path | None, inline_json: str | None) -> Any | None:
    if inline_json:
        return json.loads(inline_json)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _parse_as_of(value: str | None) -> date | str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return value


def _as_of_str(value: date | str | None) -> str:
    if value is None:
        return date.today().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _generate_record_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


def _append_question_record(
    *,
    questions_path: Path,
    ticker: str,
    question: str,
) -> None:
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
    confidence: float = 0.8,
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


def _demo_payload(ticker: str, period_end: str) -> dict[str, Any]:
    accession = "0000000000-00-000000"
    return {
        "income_statement": [
            {
                "label": "Revenue",
                "value": 1000000,
                "concept": "us-gaap:Revenues",
                "period_end": period_end,
                "fiscal_period": "FY",
                "accession": accession,
            },
            {
                "label": "Net Income",
                "value": 120000,
                "concept": "us-gaap:NetIncomeLoss",
                "period_end": period_end,
                "fiscal_period": "FY",
                "accession": accession,
            },
        ],
        "balance_sheet": [
            {
                "label": "Total Assets",
                "value": 2500000,
                "concept": "us-gaap:Assets",
                "period_end": period_end,
                "fiscal_period": "FY",
                "accession": accession,
            }
        ],
        "cash_flow": [
            {
                "label": "Net Cash Provided by Operating Activities",
                "value": 180000,
                "concept": "us-gaap:NetCashProvidedByUsedInOperatingActivities",
                "period_end": period_end,
                "fiscal_period": "FY",
                "accession": accession,
            }
        ],
    }


def _collect_payloads(payloads: Iterable[Any | None]) -> list[Any]:
    collected: list[Any] = []
    for payload in payloads:
        if payload is None:
            continue
        collected.append(payload)
    return collected


def run(
    ticker: str,
    *,
    as_of: date | str | None = None,
    lookback_years: int = 10,
    force_refresh: bool = False,
    financials_payloads: Iterable[Any] | None = None,
    demo: bool = False,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    ticker = ticker.upper()
    as_of_value = as_of or date.today()
    as_of_label = _as_of_str(as_of_value)

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
        lookback_years=lookback_years,
        force_refresh=force_refresh,
    )
    write_meta(run_dir, meta)

    warnings: list[str] = []
    missing: list[str] = []

    filings_index = _load_yaml(paths.current_dir / "filings_index.yaml")
    if not filings_index:
        warnings.append("current/filings_index.yaml missing; period_end fallback limited")

    atlas_dir = paths.current_dir / "xbrl_atlas"
    atlas_dir.mkdir(parents=True, exist_ok=True)

    facts_path = atlas_dir / "facts.parquet"
    nodes_path = atlas_dir / "nodes.parquet"
    edges_path = atlas_dir / "edges.parquet"
    paths_path = atlas_dir / "paths.parquet"
    periods_path = atlas_dir / "periods.yaml"

    payloads = _collect_payloads(financials_payloads or [])
    if demo and not payloads:
        payloads = [_demo_payload(ticker, as_of_label)]

    if not payloads and facts_path.exists() and not force_refresh:
        status = "skipped"
        outputs = [
            "current/xbrl_atlas/periods.yaml",
            "current/xbrl_atlas/nodes.parquet",
            "current/xbrl_atlas/edges.parquet",
            "current/xbrl_atlas/facts.parquet",
            "current/xbrl_atlas/paths.parquet",
        ]
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status=status,
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=outputs,
        )
        write_result(run_dir, result)
        update_artifacts_state(
            paths.artifacts_state_yaml,
            artifact="current/xbrl_atlas/facts.parquet",
            run_id=run_id,
            skill=SKILL_NAME,
            file_path=facts_path,
            extra={"status": status},
        )
        return result

    if not payloads:
        missing.append("financials_payloads")
        blocked_by = []
        if not filings_index:
            blocked_by.append(
                {
                    "artifact": "current/filings_index.yaml",
                    "producer_skill": "collect-company-facts",
                    "reason": "Missing filings_index needed for period mapping",
                }
            )
        blocked_by.append(
            {
                "artifact": "sec_edgar_mcp.get_financials",
                "producer_skill": SKILL_NAME,
                "reason": "Missing financial statement payloads",
            }
        )
        needs = build_needs(
            blocked_by=blocked_by,
            suggested_plan=["collect-company-facts", SKILL_NAME],
            priority="high",
        )
        write_needs(run_dir, needs)
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status="blocked",
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=[],
        )
        write_result(run_dir, result)
        return result

    (
        facts_df,
        nodes_df,
        edges_df,
        paths_df,
        periods_payload,
        build_warnings,
    ) = build_minimal_atlas(
        payloads,
        filings_index=filings_index,
        lookback_years=lookback_years,
        as_of=as_of_value,
    )
    warnings.extend(build_warnings)

    atomic_write_yaml(outputs_dir / "periods.yaml", periods_payload)
    atomic_write_parquet(outputs_dir / "nodes.parquet", nodes_df)
    atomic_write_parquet(outputs_dir / "edges.parquet", edges_df)
    atomic_write_parquet(outputs_dir / "facts.parquet", facts_df)
    atomic_write_parquet(outputs_dir / "paths.parquet", paths_df)

    atomic_write_yaml(periods_path, periods_payload)
    atomic_write_parquet(nodes_path, nodes_df)
    atomic_write_parquet(edges_path, edges_df)
    atomic_write_parquet(facts_path, facts_df)
    atomic_write_parquet(paths_path, paths_df)

    outputs = [
        "current/xbrl_atlas/periods.yaml",
        "current/xbrl_atlas/nodes.parquet",
        "current/xbrl_atlas/edges.parquet",
        "current/xbrl_atlas/facts.parquet",
        "current/xbrl_atlas/paths.parquet",
    ]

    status = "ok"
    if facts_df.empty:
        status = "blocked"
        warnings.append("facts.parquet is empty; no financial facts found")
    else:
        expected = {"IS", "BS", "CF"}
        observed = set(facts_df["statement_type"].dropna().unique())
        missing_types = sorted(expected - observed)
        if missing_types:
            status = "partial"
            warnings.append(f"Missing statement types: {', '.join(missing_types)}")
            _append_question_record(
                questions_path=paths.questions_jsonl,
                ticker=ticker,
                question=f"Missing statement types in XBRL atlas: {', '.join(missing_types)}",
            )
        if facts_df["period_end"].isna().any():
            status = "partial" if status == "ok" else status
            warnings.append("Some facts missing period_end")
            _append_question_record(
                questions_path=paths.questions_jsonl,
                ticker=ticker,
                question="Some facts missing period_end; check filings_index or payload",
            )

    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/xbrl_atlas/periods.yaml",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=periods_path if periods_path.exists() else None,
        extra={"status": status, "count": len(periods_payload.get("periods", []))},
    )
    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/xbrl_atlas/nodes.parquet",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=nodes_path if nodes_path.exists() else None,
        extra={"status": status, "count": len(nodes_df)},
    )
    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/xbrl_atlas/edges.parquet",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=edges_path if edges_path.exists() else None,
        extra={"status": status, "count": len(edges_df)},
    )
    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/xbrl_atlas/facts.parquet",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=facts_path if facts_path.exists() else None,
        extra={"status": status, "count": len(facts_df)},
    )
    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/xbrl_atlas/paths.parquet",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=paths_path if paths_path.exists() else None,
        extra={"status": status, "count": len(paths_df)},
    )

    if not facts_df.empty:
        _append_evidence_record(
            evidence_path=paths.evidence_jsonl,
            ticker=ticker,
            claim=f"Built Statement Atlas with {len(facts_df)} facts",
            sources=[{"type": "sec_edgar_mcp", "tool": "get_financials", "count": len(facts_df)}],
            confidence=0.85,
        )

    if status == "blocked":
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": "sec_edgar_mcp.get_financials",
                    "producer_skill": SKILL_NAME,
                    "reason": "No financial facts produced",
                }
            ],
            suggested_plan=[SKILL_NAME],
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
        outputs=outputs,
    )
    write_result(run_dir, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="extract-xbrl-timeseries runner")
    parser.add_argument("ticker", help="Stock ticker symbol")
    parser.add_argument("--as-of", dest="as_of", help="Snapshot date YYYY-MM-DD")
    parser.add_argument("--lookback-years", type=int, default=10)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--financials-json", action="append", help="Inline JSON payload for financials")
    parser.add_argument("--financials-path", action="append", type=Path, help="Path to financials payload")
    parser.add_argument("--demo", action="store_true", help="Use demo data instead of MCP results")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    args = parser.parse_args()

    payloads: list[Any] = []
    if args.financials_json:
        for payload in args.financials_json:
            payloads.append(json.loads(payload))
    if args.financials_path:
        for payload_path in args.financials_path:
            payload = _load_payload(payload_path, None)
            if payload is not None:
                payloads.append(payload)

    as_of_value = _parse_as_of(args.as_of)

    run(
        args.ticker,
        as_of=as_of_value,
        lookback_years=args.lookback_years,
        force_refresh=args.force_refresh,
        financials_payloads=payloads,
        demo=args.demo,
        timezone_name=args.timezone,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
