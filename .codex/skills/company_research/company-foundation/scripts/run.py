#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""company-foundation skill runner."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

SKILL_NAME = "company-foundation"
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


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _load_payload(path: Path | None, inline_json: str | None) -> dict[str, Any] | None:
    if inline_json:
        return json.loads(inline_json)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text) or {}


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


def _normalize_company_data(ticker: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    data = payload or {}
    info = data.get("company_info") or data
    cik_value = data.get("cik") or info.get("cik") or info.get("cik_number")
    if isinstance(cik_value, dict):
        cik_value = cik_value.get("cik")
    return {
        "ticker": ticker,
        "company_name": info.get("company_name") or info.get("name"),
        "cik": cik_value,
        "exchange": info.get("exchange"),
        "sic": info.get("sic"),
        "fiscal_year_end": info.get("fiscal_year_end") or info.get("fiscalYearEnd"),
        "currency": info.get("currency") or "USD",
    }


def _normalize_market_snapshot(payload: dict[str, Any] | None, as_of: str) -> dict[str, Any]:
    data = payload or {}
    snapshot = {
        "as_of": as_of,
        "price": data.get("price"),
        "shares_outstanding": data.get("shares_outstanding") or data.get("sharesOutstanding"),
        "shares_float": data.get("shares_float") or data.get("sharesFloat"),
        "market_cap": data.get("market_cap") or data.get("marketCap"),
        "enterprise_value": data.get("enterprise_value") or data.get("enterpriseValue"),
        "net_debt": data.get("net_debt"),
        "source": data.get("source") or "trading_mcp.get_fundamental_stock_metrics",
    }
    if snapshot["net_debt"] is None and snapshot["enterprise_value"] is not None and snapshot["market_cap"] is not None:
        snapshot["net_debt"] = snapshot["enterprise_value"] - snapshot["market_cap"]
    return snapshot


def _generate_evidence_id(prefix: str = "E") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


def _append_evidence_record(
    *,
    evidence_path: Path,
    ticker: str,
    claim: str,
    sources: list[dict[str, Any]],
    confidence: float = 0.9,
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


def run(
    ticker: str,
    *,
    as_of: date | str | None = None,
    force_refresh: bool = False,
    identity_payload: dict[str, Any] | None = None,
    market_payload: dict[str, Any] | None = None,
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
        force_refresh=force_refresh,
    )
    write_meta(run_dir, meta)

    warnings: list[str] = []
    missing: list[str] = []

    existing_company = _load_yaml(paths.company_yaml)
    identity_skipped = bool(existing_company.get("cik")) and not force_refresh
    if identity_skipped:
        company_data = existing_company
    elif demo:
        company_data = _normalize_company_data(
            ticker,
            {
                "cik": "0000000000",
                "company_info": {"name": f"{ticker} Demo Co", "exchange": "DEMO"},
            },
        )
    else:
        company_data = _normalize_company_data(ticker, identity_payload)

    existing_market = _load_yaml(paths.current_dir / "market_snapshot.yaml")
    market_skipped = (
        existing_market.get("as_of") == as_of_label
        and existing_market.get("price") is not None
        and existing_market.get("shares_outstanding") is not None
        and not force_refresh
    )
    if market_skipped:
        market_data = existing_market
    elif demo:
        market_data = _normalize_market_snapshot(
            {
                "price": 100.0,
                "sharesOutstanding": 100000000,
                "marketCap": 10000000000,
                "enterpriseValue": 11000000000,
            },
            as_of_label,
        )
    else:
        market_data = _normalize_market_snapshot(market_payload, as_of_label)

    identity_status = "skipped" if identity_skipped else "ok" if company_data.get("cik") else "blocked"
    market_complete = market_data.get("price") is not None and market_data.get("shares_outstanding") is not None
    market_status = (
        "skipped"
        if market_skipped
        else "ok"
        if market_complete
        else "partial"
        if market_data.get("price") is not None or market_data.get("shares_outstanding") is not None
        else "blocked"
    )

    if identity_skipped and market_skipped:
        status = "skipped"
    elif not company_data.get("cik"):
        status = "blocked"
    elif not market_complete:
        status = "partial"
    else:
        status = "ok"

    if not company_data.get("cik"):
        missing.append("company.yaml.cik")
    if market_data.get("price") is None:
        missing.append("market_snapshot.price")
        warnings.append("Market snapshot missing price")
    if market_data.get("shares_outstanding") is None:
        missing.append("market_snapshot.shares_outstanding")
        warnings.append("Market snapshot missing shares_outstanding")

    atomic_write_yaml(outputs_dir / "company.yaml", company_data)
    atomic_write_yaml(outputs_dir / "market_snapshot.yaml", market_data)

    if status in {"ok", "partial"}:
        if not identity_skipped:
            atomic_write_yaml(paths.company_yaml, company_data)
        if not market_skipped:
            atomic_write_yaml(paths.current_dir / "market_snapshot.yaml", market_data)

    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="company.yaml",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=paths.company_yaml if paths.company_yaml.exists() else None,
        extra={"status": identity_status},
    )
    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/market_snapshot.yaml",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=paths.current_dir / "market_snapshot.yaml"
        if (paths.current_dir / "market_snapshot.yaml").exists()
        else None,
        extra={"status": market_status},
    )

    if company_data.get("cik"):
        _append_evidence_record(
            evidence_path=paths.evidence_jsonl,
            ticker=ticker,
            claim=f"Resolved CIK {company_data.get('cik')} for {ticker}",
            sources=[{"type": "sec_edgar_mcp", "tool": "get_cik_by_ticker"}],
            confidence=0.95,
        )
    if market_complete:
        _append_evidence_record(
            evidence_path=paths.evidence_jsonl,
            ticker=ticker,
            claim=f"Captured market snapshot for {ticker} as of {as_of_label}",
            sources=[{"type": "trading_mcp", "tool": "get_fundamental_stock_metrics"}],
            confidence=0.9,
        )

    if status == "blocked":
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": "company.yaml",
                    "producer_skill": SKILL_NAME,
                    "reason": "Missing CIK from sec_edgar_mcp",
                }
            ],
            suggested_plan=[SKILL_NAME],
        )
        write_needs(run_dir, needs)

    result = build_run_result(
        skill=SKILL_NAME,
        ticker=ticker,
        run_id=run_id,
        status=status,
        as_of=as_of_value,
        timezone=timezone_name,
        warnings=warnings,
        missing=missing,
        outputs=["company.yaml", "current/market_snapshot.yaml"],
        identity_skipped=identity_skipped,
        market_skipped=market_skipped,
    )
    write_result(run_dir, result)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="company-foundation runner")
    parser.add_argument("ticker", help="Stock ticker symbol")
    parser.add_argument("--as-of", dest="as_of", help="Snapshot date YYYY-MM-DD")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--identity-json", help="Inline JSON from identity tool calls")
    parser.add_argument("--market-json", help="Inline JSON from trading_mcp tool call")
    parser.add_argument("--identity-path", type=Path, help="Path to identity payload (json/yaml)")
    parser.add_argument("--market-path", type=Path, help="Path to market payload (json/yaml)")
    parser.add_argument("--demo", action="store_true", help="Use demo data instead of MCP results")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    args = parser.parse_args()

    identity_payload = _load_payload(args.identity_path, args.identity_json)
    market_payload = _load_payload(args.market_path, args.market_json)
    as_of_value = _parse_as_of(args.as_of)

    result = run(
        args.ticker,
        as_of=as_of_value,
        force_refresh=args.force_refresh,
        identity_payload=identity_payload,
        market_payload=market_payload,
        demo=args.demo,
        timezone_name=args.timezone,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
