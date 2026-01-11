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
from typing import Any, Iterable

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
    atomic_write_json,
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


def _load_payload(path: Path | None, inline_json: str | None) -> dict[str, Any] | list[Any] | None:
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

def _parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "").replace("$", "")
    if not text or text.lower() in {"nan", "none", "null", "n/a"}:
        return None
    multiplier = 1.0
    suffix = text[-1].upper()
    if suffix in {"K", "M", "B", "T"}:
        multiplier = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[suffix]
        text = text[:-1].strip()
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _get_nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _coerce_payload_list(payloads: Any) -> list[dict[str, Any]]:
    if payloads is None:
        return []
    if isinstance(payloads, list):
        merged: list[dict[str, Any]] = []
        for item in payloads:
            if isinstance(item, dict):
                merged.append(item)
            elif isinstance(item, list):
                merged.extend(entry for entry in item if isinstance(entry, dict))
        return merged
    if isinstance(payloads, dict):
        return [payloads]
    return []


def _infer_source_label(payload: dict[str, Any]) -> str | None:
    source = payload.get("source")
    if source:
        return str(source)
    if "fundamentals" in payload:
        return "trading_mcp.get_fundamental_stock_metrics"
    if any(key in payload for key in ("currentPrice", "regularMarketPrice", "quoteSourceName", "financialCurrency")):
        return "yfinance.get_stock_info"
    if any(key in payload for key in ("latest_trade", "latestTrade", "latest_quote", "latestQuote")):
        return "alpaca.get_stock_snapshot"
    if "facts" in payload:
        return "sec_edgar_mcp.get_company_facts"
    return None


def _extract_price(payload: dict[str, Any]) -> Any:
    fundamentals = payload.get("fundamentals", {}) if isinstance(payload.get("fundamentals"), dict) else {}
    price = (
        payload.get("price")
        or payload.get("currentPrice")
        or payload.get("regularMarketPrice")
        or fundamentals.get("price")
        or fundamentals.get("currentPrice")
    )
    if price is not None:
        return price
    trade = payload.get("latest_trade") or payload.get("latestTrade") or payload.get("trade")
    if isinstance(trade, dict):
        return trade.get("p") or trade.get("price")
    quote = payload.get("latest_quote") or payload.get("latestQuote") or payload.get("quote")
    if isinstance(quote, dict):
        bid = quote.get("bp") or quote.get("bid_price") or quote.get("bidPrice")
        ask = quote.get("ap") or quote.get("ask_price") or quote.get("askPrice")
        bid_val = _parse_number(bid)
        ask_val = _parse_number(ask)
        if bid_val is not None and ask_val is not None:
            return (bid_val + ask_val) / 2
        return bid or ask
    bar = payload.get("minute_bar") or payload.get("daily_bar")
    if isinstance(bar, dict):
        return bar.get("c") or bar.get("close")
    return None


def _extract_shares_outstanding(payload: dict[str, Any]) -> Any:
    fundamentals = payload.get("fundamentals", {}) if isinstance(payload.get("fundamentals"), dict) else {}
    return (
        payload.get("shares_outstanding")
        or payload.get("sharesOutstanding")
        or payload.get("impliedSharesOutstanding")
        or fundamentals.get("shares_outstanding")
        or fundamentals.get("sharesOutstanding")
    )


def _extract_shares_float(payload: dict[str, Any]) -> Any:
    fundamentals = payload.get("fundamentals", {}) if isinstance(payload.get("fundamentals"), dict) else {}
    return (
        payload.get("shares_float")
        or payload.get("sharesFloat")
        or payload.get("floatShares")
        or fundamentals.get("shares_float")
        or fundamentals.get("sharesFloat")
    )


def _extract_market_cap(payload: dict[str, Any]) -> Any:
    fundamentals = payload.get("fundamentals", {}) if isinstance(payload.get("fundamentals"), dict) else {}
    return payload.get("market_cap") or payload.get("marketCap") or fundamentals.get("marketCap")


def _extract_enterprise_value(payload: dict[str, Any]) -> Any:
    fundamentals = payload.get("fundamentals", {}) if isinstance(payload.get("fundamentals"), dict) else {}
    return payload.get("enterprise_value") or payload.get("enterpriseValue") or fundamentals.get("enterpriseValue")


def _extract_net_debt(payload: dict[str, Any]) -> Any:
    fundamentals = payload.get("fundamentals", {}) if isinstance(payload.get("fundamentals"), dict) else {}
    return payload.get("net_debt") or fundamentals.get("net_debt")


def _build_source_label(sources: Iterable[str], primary: str | None) -> str:
    unique = [value for value in dict.fromkeys([s for s in sources if s])]
    if not unique and primary:
        return primary
    if not unique:
        return "unknown"
    if len(unique) == 1:
        return unique[0]
    return "mixed:" + "+".join(sorted(unique))


def _normalize_market_snapshot(payloads: dict[str, Any] | list[dict[str, Any]] | None, as_of: str) -> dict[str, Any]:
    data_sources = _coerce_payload_list(payloads)
    sources_used: list[str] = []
    price_source: str | None = None

    price: float | None = None
    shares_outstanding: float | None = None
    shares_float: float | None = None
    market_cap: float | None = None
    enterprise_value: float | None = None
    net_debt: float | None = None

    for payload in data_sources:
        source_label = _infer_source_label(payload)
        if price is None:
            price_value = _parse_number(_extract_price(payload))
            if price_value is not None:
                price = price_value
                price_source = source_label
                if source_label:
                    sources_used.append(source_label)
        if shares_outstanding is None:
            shares_value = _parse_number(_extract_shares_outstanding(payload))
            if shares_value is not None:
                shares_outstanding = shares_value
                if source_label:
                    sources_used.append(source_label)
        if shares_float is None:
            float_value = _parse_number(_extract_shares_float(payload))
            if float_value is not None:
                shares_float = float_value
                if source_label:
                    sources_used.append(source_label)
        if market_cap is None:
            market_value = _parse_number(_extract_market_cap(payload))
            if market_value is not None:
                market_cap = market_value
                if source_label:
                    sources_used.append(source_label)
        if enterprise_value is None:
            ev_value = _parse_number(_extract_enterprise_value(payload))
            if ev_value is not None:
                enterprise_value = ev_value
                if source_label:
                    sources_used.append(source_label)
        if net_debt is None:
            net_debt_value = _parse_number(_extract_net_debt(payload))
            if net_debt_value is not None:
                net_debt = net_debt_value
                if source_label:
                    sources_used.append(source_label)

    if market_cap is None and price is not None and shares_outstanding is not None:
        market_cap = price * shares_outstanding
    if enterprise_value is None and market_cap is not None and net_debt is not None:
        enterprise_value = market_cap + net_debt
    if net_debt is None and enterprise_value is not None and market_cap is not None:
        net_debt = enterprise_value - market_cap

    snapshot = {
        "as_of": as_of,
        "price": price,
        "shares_outstanding": shares_outstanding,
        "shares_float": shares_float,
        "market_cap": market_cap,
        "enterprise_value": enterprise_value,
        "net_debt": net_debt,
        "source": _build_source_label(sources_used, price_source),
    }
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


def _source_label_to_evidence_sources(source: str | None) -> list[dict[str, Any]]:
    if not source:
        return [{"type": "unknown", "tool": "unknown"}]
    if source.startswith("mixed:"):
        items = source.replace("mixed:", "", 1).split("+")
        sources: list[dict[str, Any]] = []
        for item in items:
            if "." in item:
                namespace, tool = item.split(".", 1)
                sources.append({"type": namespace, "tool": tool})
            else:
                sources.append({"type": "unknown", "tool": item})
        return sources or [{"type": "unknown", "tool": "unknown"}]
    if "." in source:
        namespace, tool = source.split(".", 1)
        return [{"type": namespace, "tool": tool}]
    return [{"type": "unknown", "tool": source}]


def _persist_inputs(
    run_dir: Path,
    *,
    identity_payload: dict[str, Any] | None,
    market_payload: dict[str, Any] | list[Any] | None,
) -> list[str]:
    inputs_dir = run_dir / "inputs"
    persisted: list[str] = []
    if identity_payload is not None:
        atomic_write_json(inputs_dir / "identity_payload.json", identity_payload, ensure_ascii=False)
        persisted.append("inputs/identity_payload.json")
    if market_payload is not None:
        atomic_write_json(inputs_dir / "market_payload.json", market_payload, ensure_ascii=False)
        persisted.append("inputs/market_payload.json")
    return persisted


def run(
    ticker: str,
    *,
    as_of: date | str | None = None,
    force_refresh: bool = False,
    identity_payload: dict[str, Any] | None = None,
    market_payload: dict[str, Any] | list[dict[str, Any]] | None = None,
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

    persisted_inputs: list[str] = []
    if persist_inputs and not demo:
        persisted_inputs = _persist_inputs(
            run_dir,
            identity_payload=identity_payload,
            market_payload=market_payload,
        )

    meta = build_run_meta(
        skill=SKILL_NAME,
        ticker=ticker,
        run_id=run_id,
        as_of=as_of_value,
        timezone=timezone_name,
        force_refresh=force_refresh,
        inputs_persisted=persisted_inputs,
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
            sources=_source_label_to_evidence_sources(market_data.get("source")),
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
    parser.add_argument(
        "--market-json",
        action="append",
        help="Inline JSON payload (repeat to merge multiple sources in priority order)",
    )
    parser.add_argument("--identity-path", type=Path, help="Path to identity payload (json/yaml)")
    parser.add_argument(
        "--market-path",
        action="append",
        type=Path,
        help="Path to market payload (json/yaml); repeat to merge multiple sources",
    )
    parser.add_argument("--demo", action="store_true", help="Use demo data instead of MCP results")
    parser.add_argument(
        "--persist-inputs",
        action="store_true",
        help="Persist input payloads under runs/{run_id}/inputs",
    )
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    args = parser.parse_args()

    identity_payload = _load_payload(args.identity_path, args.identity_json)
    market_payloads: list[Any] = []
    if args.market_json:
        market_payloads.extend(_load_payload(None, item) for item in args.market_json)
    if args.market_path:
        market_payloads.extend(_load_payload(item, None) for item in args.market_path)
    market_payload: dict[str, Any] | list[dict[str, Any]] | None
    if market_payloads:
        market_payload = market_payloads
    else:
        market_payload = None
    as_of_value = _parse_as_of(args.as_of)

    result = run(
        args.ticker,
        as_of=as_of_value,
        force_refresh=args.force_refresh,
        identity_payload=identity_payload,
        market_payload=market_payload,
        demo=args.demo,
        timezone_name=args.timezone,
        persist_inputs=args.persist_inputs,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
