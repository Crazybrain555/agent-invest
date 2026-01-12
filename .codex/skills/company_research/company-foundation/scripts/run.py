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


def _normalize_exchange(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if "AssetExchange." in text:
        text = text.split("AssetExchange.", 1)[1]
    upper = text.upper()
    code_map = {
        "NYQ": "NYSE",
        "NMS": "NASDAQ",
        "NAS": "NASDAQ",
        "NGS": "NASDAQ",
        "NGM": "NASDAQ",
        "NCM": "NASDAQ",
        "ASE": "NYSEAMERICAN",
    }
    if upper in code_map:
        return code_map[upper]
    if upper.startswith("NASDAQ"):
        return "NASDAQ"
    if upper.startswith("NYSE"):
        return "NYSE"
    return upper


def _parse_mm_dd(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if len(text) == 5 and text[2] == "-" and text[:2].isdigit() and text[3:].isdigit():
            return text
        iso_part = text.split("T", 1)[0].split(" ", 1)[0]
        try:
            parsed = date.fromisoformat(iso_part)
            return parsed.strftime("%m-%d")
        except ValueError:
            pass
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                parsed_dt = datetime.strptime(text, fmt)
                return parsed_dt.date().strftime("%m-%d")
            except ValueError:
                continue
    return None


def _extract_period_of_report_mm_dd(payload: dict[str, Any]) -> str | None:
    direct = payload.get("period_of_report") or payload.get("periodOfReport")
    mm_dd = _parse_mm_dd(direct)
    if mm_dd:
        return mm_dd
    filings = payload.get("filings")
    if isinstance(filings, list):
        for item in filings:
            if not isinstance(item, dict):
                continue
            mm_dd = _parse_mm_dd(item.get("period_of_report") or item.get("periodOfReport"))
            if mm_dd:
                return mm_dd
    return None


def _extract_document_period_end_mm_dd(payload: dict[str, Any]) -> str | None:
    concepts = payload.get("concepts")
    if isinstance(payload.get("xbrl_concepts"), dict) and not isinstance(concepts, dict):
        concepts = payload["xbrl_concepts"].get("concepts")
    if not isinstance(concepts, dict):
        return None
    entry = concepts.get("DocumentPeriodEndDate")
    if isinstance(entry, dict):
        for key in ("period", "raw_value", "value"):
            mm_dd = _parse_mm_dd(entry.get(key))
            if mm_dd:
                return mm_dd
    return _parse_mm_dd(entry)


def _normalize_company_data(
    ticker: str,
    payload: dict[str, Any] | None,
    *,
    supplemental_payloads: Any = None,
) -> dict[str, Any]:
    data = payload or {}
    info = data.get("company_info") or data
    cik_value = data.get("cik") or info.get("cik") or info.get("cik_number")
    if isinstance(cik_value, dict):
        cik_value = cik_value.get("cik")
    if isinstance(cik_value, int):
        cik_value = f"{cik_value:010d}"
    elif isinstance(cik_value, str) and cik_value.isdigit() and len(cik_value) < 10:
        cik_value = cik_value.zfill(10)
    supplemental_list = _coerce_payload_list(supplemental_payloads)

    exchange = _normalize_exchange(info.get("exchange"))
    if not exchange:
        best: tuple[int, str] | None = None
        for item in supplemental_list:
            source_label = _infer_source_label(item) or ""
            candidate = _normalize_exchange(item.get("exchange")) or _normalize_exchange(item.get("fullExchangeName"))
            if not candidate:
                continue
            priority = 0 if source_label.startswith("alpaca") else 1
            choice = (priority, candidate)
            if best is None or choice < best:
                best = choice
        if best is not None:
            exchange = best[1]

    fiscal_year_end = _parse_mm_dd(info.get("fiscal_year_end") or info.get("fiscalYearEnd"))
    if fiscal_year_end is None:
        best_mm_dd: str | None = None
        for item in supplemental_list:
            source_label = _infer_source_label(item) or ""
            if not source_label.startswith("sec_edgar_mcp.get_recent_filings"):
                continue
            best_mm_dd = _extract_period_of_report_mm_dd(item)
            if best_mm_dd:
                break
        if best_mm_dd is None:
            for item in supplemental_list:
                best_mm_dd = _extract_period_of_report_mm_dd(item)
                if best_mm_dd:
                    break
        if best_mm_dd is None:
            for item in supplemental_list:
                best_mm_dd = _extract_document_period_end_mm_dd(item)
                if best_mm_dd:
                    break
        fiscal_year_end = best_mm_dd

    return {
        "ticker": ticker,
        "company_name": info.get("company_name") or info.get("name"),
        "cik": cik_value,
        "exchange": exchange,
        "sic": info.get("sic"),
        "fiscal_year_end": fiscal_year_end,
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


def _extract_enterprise_value(payload: dict[str, Any]) -> tuple[Any, str]:
    fundamentals = payload.get("fundamentals", {}) if isinstance(payload.get("fundamentals"), dict) else {}
    if "enterprise_value" in payload:
        return payload.get("enterprise_value"), "normalized"
    if "enterpriseValue" in payload:
        return payload.get("enterpriseValue"), "raw"
    if "enterpriseValue" in fundamentals:
        return fundamentals.get("enterpriseValue"), "raw"
    return None, "missing"

def _build_source_label(sources: Iterable[str], primary: str | None) -> str:
    unique = [value for value in dict.fromkeys([s for s in sources if s])]
    if not unique and primary:
        return primary
    if not unique:
        return "unknown"
    if len(unique) == 1:
        return unique[0]
    return "mixed:" + "+".join(sorted(unique))


def _extract_fx_rate(payload: dict[str, Any]) -> tuple[str, str, float] | None:
    fx_from = payload.get("fx_from")
    fx_to = payload.get("fx_to")
    fx_rate = payload.get("fx_rate")
    if isinstance(payload.get("fx"), dict):
        fx_from = fx_from or payload["fx"].get("from")
        fx_to = fx_to or payload["fx"].get("to")
        fx_rate = fx_rate or payload["fx"].get("rate")
    if not fx_from or not fx_to:
        return None
    rate_value = _parse_number(fx_rate)
    if rate_value is None or rate_value == 0:
        return None
    return str(fx_from).upper(), str(fx_to).upper(), float(rate_value)


def _build_fx_table(payloads: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], str]]:
    table: dict[tuple[str, str], float] = {}
    sources: dict[tuple[str, str], str] = {}
    for payload in payloads:
        extracted = _extract_fx_rate(payload)
        if extracted is None:
            continue
        fx_from, fx_to, rate = extracted
        source_label = payload.get("source") or _infer_source_label(payload) or "fx"
        table[(fx_from, fx_to)] = rate
        sources[(fx_from, fx_to)] = str(source_label)
        if rate != 0:
            table[(fx_to, fx_from)] = 1.0 / rate
            sources[(fx_to, fx_from)] = str(source_label)
    return table, sources


def _normalize_market_snapshot(
    payloads: dict[str, Any] | list[dict[str, Any]] | None,
    as_of: str,
) -> dict[str, Any]:
    market_cap_rel_diff_threshold = 0.10
    data_sources = _coerce_payload_list(payloads)
    fx_table, fx_sources = _build_fx_table(data_sources)
    sources_used: list[str] = []
    price_source: str | None = None
    desired_currency = "USD"

    price: float | None = None
    shares_outstanding: float | None = None
    shares_float: float | None = None
    market_cap_extracted: float | None = None
    enterprise_value: float | None = None

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
        if market_cap_extracted is None:
            market_value = _parse_number(_extract_market_cap(payload))
            if market_value is not None:
                market_currency = payload.get("currency") or payload.get("quoteCurrency")
                if market_currency and str(market_currency).upper() != desired_currency:
                    rate = fx_table.get((str(market_currency).upper(), desired_currency))
                    if rate is None:
                        continue
                    market_value = market_value * rate
                    fx_source = fx_sources.get((str(market_currency).upper(), desired_currency))
                    if fx_source:
                        sources_used.append(fx_source)
                market_cap_extracted = market_value
                if source_label:
                    sources_used.append(source_label)
        if enterprise_value is None:
            ev_raw, ev_kind = _extract_enterprise_value(payload)
            ev_value = _parse_number(ev_raw)
            if ev_value is not None:
                if ev_kind == "normalized":
                    enterprise_value = ev_value
                    if source_label:
                        sources_used.append(source_label)
                else:
                    financial_currency = payload.get("financialCurrency") or payload.get("financial_currency")
                    listing_currency = payload.get("currency") or payload.get("quoteCurrency")
                    value_currency = (str(financial_currency).upper() if financial_currency else None) or (
                        str(listing_currency).upper() if listing_currency else None
                    )
                    if value_currency and value_currency != desired_currency:
                        rate = fx_table.get((value_currency, desired_currency))
                        if rate is not None:
                            enterprise_value = ev_value * rate
                            if source_label:
                                sources_used.append(source_label)
                            fx_source = fx_sources.get((value_currency, desired_currency))
                            if fx_source:
                                sources_used.append(fx_source)
                        else:
                            continue
                    else:
                        enterprise_value = ev_value
                        if source_label:
                            sources_used.append(source_label)

    market_cap_derived: float | None = None
    if price is not None and shares_outstanding is not None:
        market_cap_derived = price * shares_outstanding
    market_cap = market_cap_extracted
    if market_cap is None:
        market_cap = market_cap_derived
    elif market_cap_derived is not None and market_cap not in (0, None):
        rel_diff = abs(market_cap - market_cap_derived) / abs(market_cap)
        if rel_diff >= market_cap_rel_diff_threshold:
            market_cap = market_cap_derived

    if shares_float is not None and shares_outstanding is not None:
        if shares_float <= 0:
            shares_float = None
        elif shares_float > shares_outstanding * 1.1:
            shares_float = None
    # Do not auto-derive enterprise_value here; currency alignment (especially ADRs) can be ambiguous.

    snapshot = {
        "as_of": as_of,
        "currency": desired_currency,
        "price": price,
        "shares_outstanding": shares_outstanding,
        "shares_float": shares_float,
        "market_cap": market_cap,
        "enterprise_value": enterprise_value,
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
        company_data = _normalize_company_data(ticker, identity_payload, supplemental_payloads=market_payload)

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
        market_data = _normalize_market_snapshot(
            market_payload,
            as_of_label,
        )

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
