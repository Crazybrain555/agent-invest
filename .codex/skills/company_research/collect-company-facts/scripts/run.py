#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collect-company-facts skill runner (SEC-only for Phase 1)."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

SKILL_NAME = "collect-company-facts"
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_FORMS = ["10-K", "10-Q", "8-K", "DEF14A", "20-F", "6-K"]


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
    atomic_write_json,
    atomic_write_parquet,
    atomic_write_text,
    atomic_write_yaml,
    build_needs,
    build_run_meta,
    build_run_result,
    company_paths,
    default_run_id,
    ensure_jsonl,
    update_artifacts_state,
    write_meta,
    write_needs,
    write_result,
)


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


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    candidate = str(value)
    if len(candidate) >= 10 and candidate[4] == "-":
        try:
            return date.fromisoformat(candidate[:10])
        except ValueError:
            return None
    if len(candidate) == 8 and candidate.isdigit():
        try:
            return date(int(candidate[:4]), int(candidate[4:6]), int(candidate[6:]))
        except ValueError:
            return None
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_accession(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("accession") or value.get("accession_number")
    text = str(value).strip()
    return text or None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    if isinstance(value, int):
        return value != 0
    return False


def _normalize_filing(filing: Mapping[str, Any]) -> dict[str, Any]:
    accession = _normalize_accession(
        filing.get("accession")
        or filing.get("accession_number")
        or filing.get("accessionNumber")
        or filing.get("accessionNo")
    )
    form = (
        filing.get("form")
        or filing.get("form_type")
        or filing.get("formType")
        or filing.get("filing_type")
        or filing.get("type")
    )
    form = form.upper() if isinstance(form, str) else form
    filed_at = (
        filing.get("filed_at")
        or filing.get("filedAt")
        or filing.get("filing_date")
        or filing.get("filingDate")
        or filing.get("date")
    )
    period_end = (
        filing.get("period_end")
        or filing.get("period_of_report")
        or filing.get("periodOfReport")
        or filing.get("report_date")
        or filing.get("reportDate")
    )
    has_xbrl = _coerce_bool(
        filing.get("has_xbrl")
        or filing.get("hasXbrl")
        or filing.get("is_xbrl")
        or filing.get("xbrl")
    )
    return {
        "form": form,
        "filed_at": filed_at,
        "period_end": period_end,
        "accession": accession,
        "has_xbrl": has_xbrl,
    }


def _extract_filings(payload: Any | None) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("filings", "results", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if any(key in payload for key in ("accession", "accession_number", "form")):
            return [payload]
        collected: list[dict[str, Any]] = []
        for value in payload.values():
            if isinstance(value, list):
                collected.extend([item for item in value if isinstance(item, dict)])
        if collected:
            return collected
    return []


def _merge_filings(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for filing in existing + incoming:
        accession = filing.get("accession")
        if not accession:
            continue
        merged[accession] = filing
    return list(merged.values())


def _filter_filings_by_lookback(
    filings: list[dict[str, Any]],
    *,
    as_of: date,
    lookback_years: int,
) -> list[dict[str, Any]]:
    cutoff = as_of - timedelta(days=365 * lookback_years)
    filtered: list[dict[str, Any]] = []
    for filing in filings:
        filed_date = _parse_date(str(filing.get("filed_at") or ""))
        period_date = _parse_date(str(filing.get("period_end") or ""))
        compare_date = filed_date or period_date
        if compare_date is None or compare_date >= cutoff:
            filtered.append(filing)
    return filtered


def _filing_sort_key(filing: dict[str, Any]) -> str:
    return str(filing.get("filed_at") or filing.get("period_end") or "")


def _persist_inputs(
    run_dir: Path,
    *,
    filings_payloads: list[Any],
    filing_content_payload: Any | None,
) -> list[str]:
    inputs_dir = run_dir / "inputs"
    persisted: list[str] = []
    if filings_payloads:
        atomic_write_json(
            inputs_dir / "filings_payloads.json",
            filings_payloads,
            ensure_ascii=False,
            default=str,
        )
        persisted.append("inputs/filings_payloads.json")
    if filing_content_payload is not None:
        atomic_write_json(
            inputs_dir / "filing_content_payload.json",
            filing_content_payload,
            ensure_ascii=False,
            default=str,
        )
        persisted.append("inputs/filing_content_payload.json")
    return persisted


def _resolve_filing_payload_map(payload: Any | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _resolve_filing_content(payload_map: dict[str, Any], accession: str) -> Any | None:
    if accession in payload_map:
        return payload_map[accession]
    alt = accession.replace("-", "")
    return payload_map.get(alt)


def _write_filing_raw(
    *,
    raw_sec_dir: Path,
    filing: dict[str, Any],
    content_payload: Any | None,
) -> None:
    accession = filing.get("accession")
    if not accession:
        return
    target_dir = raw_sec_dir / accession
    target_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_yaml(target_dir / "meta.yaml", filing)

    if isinstance(content_payload, dict):
        if "sections" in content_payload:
            atomic_write_json(target_dir / "sections.json", content_payload["sections"], ensure_ascii=False)
        if "content" in content_payload:
            atomic_write_text(target_dir / "content.txt", str(content_payload["content"]))
        if "html" in content_payload:
            atomic_write_text(target_dir / "content.html", str(content_payload["html"]))
        return

    if isinstance(content_payload, str):
        atomic_write_text(target_dir / "content.txt", content_payload)


def _generate_evidence_id(prefix: str = "E") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


def _append_evidence_record(
    *,
    evidence_path: Path,
    ticker: str,
    claim: str,
    sources: list[dict[str, Any]],
    confidence: float = 0.85,
) -> None:
    record = {
        "id": _generate_evidence_id("E"),
        "created_at": datetime.now(timezone.utc).date().isoformat(),
        "ticker": ticker,
        "skill": SKILL_NAME,
        "claim": claim,
        "confidence": confidence,
        "sources": sources,
    }
    append_evidence(evidence_path, record)


def _build_events_index(
    filings: Iterable[dict[str, Any]],
    *,
    ticker: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for filing in filings:
        accession = filing.get("accession")
        form = filing.get("form")
        if not accession or not isinstance(form, str):
            continue
        if not (form.startswith("8-K") or form.startswith("6-K")):
            continue
        events.append(
            {
                "event_id": f"sec:{accession}",
                "event_type": "sec",
                "occurred_at": filing.get("filed_at"),
                "ticker": ticker,
                "headline": f"{form} {accession}",
                "tags": [],
                "materiality_hint": None,
                "score_hint": None,
                "impact_score": None,  # Phase2 placeholder
                "source_ref_json": json.dumps(
                    {"local_dir": filing.get("local_dir")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "anchors_json": json.dumps({}, ensure_ascii=False, sort_keys=True),
            }
        )
    return events


def _write_parquet_pair(
    *,
    run_path: Path,
    current_path: Path,
    records: list[dict[str, Any]],
    timestamp_cols: list[str] | None,
    warnings: list[str],
    label: str,
) -> bool:
    if not records:
        return False
    try:
        import pandas as pd
    except ImportError:
        warnings.append(f"Skipped {label} parquet: pandas not available")
        return False

    frame = pd.DataFrame(records)
    for col in timestamp_cols or []:
        if col in frame.columns:
            frame[col] = pd.to_datetime(frame[col], utc=True, errors="coerce")

    atomic_write_parquet(run_path, frame)
    atomic_write_parquet(current_path, frame)
    return True


def run(
    ticker: str,
    *,
    as_of: date | str | None = None,
    lookback_years: int = 10,
    force_refresh: bool = False,
    filings_payloads: list[Any] | None = None,
    filing_content_payload: Any | None = None,
    demo: bool = False,
    timezone_name: str = DEFAULT_TIMEZONE,
    persist_inputs: bool = False,
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

    filings_payloads = filings_payloads or []
    persisted_inputs: list[str] = []
    if persist_inputs and not demo:
        persisted_inputs = _persist_inputs(
            run_dir,
            filings_payloads=filings_payloads,
            filing_content_payload=filing_content_payload,
        )

    meta = build_run_meta(
        skill=SKILL_NAME,
        ticker=ticker,
        run_id=run_id,
        as_of=as_of_value,
        timezone=timezone_name,
        lookback_years=lookback_years,
        force_refresh=force_refresh,
        forms=DEFAULT_FORMS,
        inputs_persisted=persisted_inputs,
    )
    write_meta(run_dir, meta)

    warnings: list[str] = []
    missing: list[str] = []

    company = _load_yaml(paths.company_yaml)
    if not company.get("cik"):
        missing.append("company.yaml.cik")
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": "company.yaml",
                    "producer_skill": "company-foundation",
                    "reason": "Missing CIK needed to query SEC filings",
                }
            ],
            suggested_plan=["company-foundation", SKILL_NAME],
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

    cik = str(company.get("cik"))
    filing_content_map = _resolve_filing_payload_map(filing_content_payload)

    current_filings_path = paths.current_dir / "filings_index.yaml"
    existing_index = _load_yaml(current_filings_path)
    existing_filings = existing_index.get("filings") if isinstance(existing_index, dict) else []
    existing_filings = existing_filings if isinstance(existing_filings, list) else []
    existing_accessions = {
        filing.get("accession") for filing in existing_filings if isinstance(filing, dict) and filing.get("accession")
    }

    raw_filings: list[dict[str, Any]] = []
    for payload in filings_payloads:
        raw_filings.extend(_extract_filings(payload))

    if demo and not raw_filings:
        raw_filings = [
            {
                "form": "10-K",
                "filed_at": as_of_label,
                "period_end": as_of_label,
                "accession": "0000000000-00-000000",
                "has_xbrl": True,
            }
        ]

    normalized_filings = [_normalize_filing(filing) for filing in raw_filings]
    normalized_filings = [filing for filing in normalized_filings if filing.get("accession")]

    filings_skipped = False
    if not normalized_filings and existing_filings and not force_refresh:
        filings_index: dict[str, Any] = existing_index
        filings_skipped = True
    else:
        merged_filings = _merge_filings(existing_filings, normalized_filings)
        merged_filings = _filter_filings_by_lookback(
            merged_filings,
            as_of=as_of_value if isinstance(as_of_value, date) else date.today(),
            lookback_years=lookback_years,
        )
        new_accessions_count = len({f.get("accession") for f in normalized_filings if f.get("accession")} - existing_accessions)
        filings_index = {
            "as_of": as_of_label,
            "cik": cik,
            "totals": {
                "fetched": len(raw_filings),
                "deduped_new": new_accessions_count,
                "stored_total": len(merged_filings),
            },
            "filings": merged_filings,
        }

    filings_list = filings_index.get("filings") if isinstance(filings_index, dict) else []
    filings_list = filings_list if isinstance(filings_list, list) else []

    new_accessions: set[str] = set()
    for filing in filings_list:
        if not isinstance(filing, dict):
            continue
        accession = filing.get("accession")
        if accession and accession not in existing_accessions:
            new_accessions.add(accession)

    raw_sec_dir = paths.raw_dir / "sec"
    raw_sec_dir.mkdir(parents=True, exist_ok=True)
    if not filings_skipped:
        for filing in normalized_filings:
            accession = filing.get("accession")
            if not accession:
                continue
            if accession in existing_accessions and not force_refresh:
                continue
            content_payload = _resolve_filing_content(filing_content_map, accession)
            _write_filing_raw(raw_sec_dir=raw_sec_dir, filing=filing, content_payload=content_payload)

    # Attach local_dir and sort.
    enriched_filings: list[dict[str, Any]] = []
    for filing in filings_list:
        if not isinstance(filing, dict):
            continue
        enriched_filings.append(
            {
                **filing,
                "local_dir": f"raw/sec/{filing.get('accession')}/" if filing.get("accession") else None,
            }
        )
    enriched_filings.sort(key=_filing_sort_key, reverse=True)
    filings_index["filings"] = enriched_filings

    ensure_jsonl(paths.evidence_jsonl)
    ensure_jsonl(paths.questions_jsonl)

    filings_status = "skipped" if filings_skipped else "ok"
    if not enriched_filings:
        filings_status = "blocked"
        warnings.append("SEC filings list unavailable or empty")
        missing.append("current/filings_index.yaml")

    # Persist outputs (runs → current).
    run_filings_yaml = outputs_dir / "filings_index.yaml"
    atomic_write_yaml(run_filings_yaml, filings_index)
    if (not filings_skipped) or not current_filings_path.exists():
        atomic_write_yaml(current_filings_path, filings_index)

    filings_parquet_written = _write_parquet_pair(
        run_path=outputs_dir / "filings_index.parquet",
        current_path=paths.current_dir / "filings_index.parquet",
        records=enriched_filings,
        timestamp_cols=None,
        warnings=warnings,
        label="filings_index",
    )

    events = _build_events_index(enriched_filings, ticker=ticker)
    events_parquet_written = _write_parquet_pair(
        run_path=outputs_dir / "events_index.parquet",
        current_path=paths.current_dir / "events_index.parquet",
        records=events,
        timestamp_cols=["occurred_at"],
        warnings=warnings,
        label="events_index",
    )

    if filings_status == "blocked":
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": "current/filings_index.yaml",
                    "producer_skill": SKILL_NAME,
                    "reason": "SEC filings list unavailable",
                }
            ],
            suggested_plan=[SKILL_NAME],
            priority="high",
        )
        write_needs(run_dir, needs)

    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/filings_index.yaml",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=current_filings_path if current_filings_path.exists() else None,
        extra={"status": filings_status, "count": len(enriched_filings)},
    )
    if filings_parquet_written:
        update_artifacts_state(
            paths.artifacts_state_yaml,
            artifact="current/filings_index.parquet",
            run_id=run_id,
            skill=SKILL_NAME,
            file_path=paths.current_dir / "filings_index.parquet",
            extra={"status": filings_status, "count": len(enriched_filings)},
        )
    if events_parquet_written:
        update_artifacts_state(
            paths.artifacts_state_yaml,
            artifact="current/events_index.parquet",
            run_id=run_id,
            skill=SKILL_NAME,
            file_path=paths.current_dir / "events_index.parquet",
            extra={"status": "ok", "count": len(events)},
        )

    if new_accessions:
        _append_evidence_record(
            evidence_path=paths.evidence_jsonl,
            ticker=ticker,
            claim=f"Collected {len(new_accessions)} new SEC filings for {ticker}",
            sources=[{"type": "sec_edgar_mcp", "tool": "get_recent_filings", "count": len(new_accessions)}],
            confidence=0.9,
        )

    status: str
    if filings_status == "blocked":
        status = "blocked"
    elif filings_status == "skipped":
        status = "skipped"
    elif warnings:
        status = "partial"
    else:
        status = "ok"

    outputs: list[str] = ["current/filings_index.yaml"]
    if filings_parquet_written:
        outputs.append("current/filings_index.parquet")
    if events_parquet_written:
        outputs.append("current/events_index.parquet")

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
        filings_skipped=filings_skipped,
        cik=cik,
    )
    write_result(run_dir, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="collect-company-facts runner (SEC-only)")
    parser.add_argument("ticker", help="Stock ticker symbol")
    parser.add_argument("--as-of", dest="as_of", help="Snapshot date YYYY-MM-DD")
    parser.add_argument("--lookback-years", type=int, default=10)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--filings-json", action="append", help="Inline JSON payload for filings")
    parser.add_argument("--filings-path", action="append", type=Path, help="Path to filings payload")
    parser.add_argument("--filing-content-json", help="Inline JSON map accession->content")
    parser.add_argument("--filing-content-path", type=Path, help="Path to filing content map")
    parser.add_argument("--demo", action="store_true", help="Use demo data instead of MCP results")
    parser.add_argument(
        "--persist-inputs",
        action="store_true",
        help="Persist input payloads under runs/{run_id}/inputs",
    )
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    args = parser.parse_args()

    filings_payloads: list[Any] = []
    if args.filings_json:
        for payload in args.filings_json:
            filings_payloads.append(json.loads(payload))
    if args.filings_path:
        for payload_path in args.filings_path:
            payload = _load_payload(payload_path, None)
            if payload is not None:
                filings_payloads.append(payload)

    filing_content_payload = _load_payload(args.filing_content_path, args.filing_content_json)
    as_of_value = _parse_as_of(args.as_of)

    result = run(
        args.ticker,
        as_of=as_of_value,
        lookback_years=args.lookback_years,
        force_refresh=args.force_refresh,
        filings_payloads=filings_payloads,
        filing_content_payload=filing_content_payload,
        demo=args.demo,
        timezone_name=args.timezone,
        persist_inputs=args.persist_inputs,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
