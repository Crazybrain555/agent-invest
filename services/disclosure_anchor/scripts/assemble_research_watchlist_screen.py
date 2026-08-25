"""Assemble a normalized, self-auditing screen from a frozen source bundle."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any

if __package__:
    from scripts.build_research_watchlist import (
        _derived_reasons,
        _future_potential_metrics,
        _future_evidence_sort_key,
        _future_evidence_tier,
        _future_research_signals,
        identity_row_sha256,
        recompute_selection,
    )
else:
    from build_research_watchlist import (  # type: ignore[no-redef]
        _derived_reasons,
        _future_potential_metrics,
        _future_evidence_sort_key,
        _future_evidence_tier,
        _future_research_signals,
        identity_row_sha256,
        recompute_selection,
    )
from disclosure_anchor.application.contracts.research_universe import (
    CNINFO_BOARD_BY_MARKET_CODE,
    EXPECTED_RULES,
    SCREEN_SCHEMA,
)
from disclosure_anchor.domain.value_objects import infer_mainland_exchange


_SOURCE_SCHEMA = "research-watchlist-source-bundle.v1"
_MARKET_EXCHANGES = {
    "012001": "SSE",
    "012002": "SZSE",
    "012015": "SZSE",
    "012029": "SSE",
    "012046": "BSE",
}
_SHA256_RE = re.compile(r"^sha256:([a-f0-9]{64})$")
_YEARS = (2023, 2024, 2025)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_row_sha256(row: dict[str, Any] | None) -> str | None:
    if row is None:
        return None
    payload = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(payload)


def _number(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _scaled_cny(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        scaled = Decimal(str(value)) * Decimal(10_000)
    except (InvalidOperation, ValueError):
        return None
    if not scaled.is_finite():
        return None
    integral = scaled.to_integral_value()
    return int(integral) if scaled == integral else float(scaled)


def _exchange(code: str, identity: dict[str, Any] | None) -> str | None:
    if identity is not None:
        market = _MARKET_EXCHANGES.get(str(identity.get("F004V") or ""))
        if market is not None:
            return market
    try:
        return infer_mainland_exchange(code)
    except ValueError:
        return None


def _board(identity: dict[str, Any] | None) -> str | None:
    if identity is None:
        return None
    return CNINFO_BOARD_BY_MARKET_CODE.get(str(identity.get("F004V") or ""))


def _safe_evidence_relpath(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("source evidence path must be safe and relative")
    if path.parts[0] != "watchlist":
        raise ValueError("source evidence path must start with watchlist/")
    return path.as_posix()


def _load_verified_sources(
    source_root: Path,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    dict[int, list[dict[str, Any]]],
    bytes,
    list[dict[str, Any]],
]:
    receipt_path = source_root / "source-fetch-receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    if not isinstance(receipt, dict) or receipt.get("schema") != _SOURCE_SCHEMA:
        raise ValueError("source bundle receipt schema mismatch")
    identity_item = receipt.get("identity")
    requests = receipt.get("requests")
    if not isinstance(identity_item, dict) or not isinstance(requests, list):
        raise ValueError("source bundle receipt identity/requests are invalid")

    items = [identity_item, *requests]
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("source receipt item must be an object")
        relpath = item.get("relpath")
        expected_hash = item.get("sha256")
        if not isinstance(relpath, str) or not isinstance(expected_hash, str):
            raise ValueError("source receipt relpath/hash missing")
        candidate = (source_root / relpath).resolve(strict=True)
        if source_root.resolve() not in candidate.parents:
            raise ValueError(f"source receipt path escapes bundle: {relpath}")
        raw = candidate.read_bytes()
        match = _SHA256_RE.fullmatch(expected_hash)
        if match is None or _sha256(raw) != match.group(1):
            raise ValueError(f"source response hash mismatch: {relpath}")
        if item.get("bytes") != len(raw):
            raise ValueError(f"source response byte count mismatch: {relpath}")

    identity_path = source_root / str(identity_item["relpath"])
    identity_payload = json.loads(identity_path.read_bytes())
    identity_rows = identity_payload.get("records")
    if not isinstance(identity_rows, list) or any(
        not isinstance(row, dict) for row in identity_rows
    ):
        raise ValueError("CNINFO records must be a list of objects")
    if identity_item.get("rows") != len(identity_rows):
        raise ValueError("CNINFO receipt row count mismatch")

    quotes: list[dict[str, Any]] = []
    annual: dict[int, list[dict[str, Any]]] = {year: [] for year in _YEARS}
    for item in requests:
        path = source_root / str(item["relpath"])
        provider = item.get("provider")
        payload = json.loads(path.read_bytes())
        if provider == "Sina Market Center hs_a":
            if not isinstance(payload, list) or any(
                not isinstance(row, dict) for row in payload
            ):
                raise ValueError(f"Sina page must contain object rows: {path}")
            quotes.extend(payload)
            actual_rows = len(payload)
        elif provider == "Eastmoney RPT_LICO_FN_CPD":
            result = payload.get("result") if isinstance(payload, dict) else None
            rows = result.get("data") if isinstance(result, dict) else None
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) for row in rows
            ):
                raise ValueError(f"Eastmoney page must contain object rows: {path}")
            request = item.get("request")
            filter_value = request.get("filter") if isinstance(request, dict) else None
            year_match = re.fullmatch(
                r"\(REPORTDATE='(\d{4})-12-31'\)", str(filter_value)
            )
            if year_match is None or int(year_match.group(1)) not in annual:
                raise ValueError(f"Eastmoney receipt year is invalid: {filter_value!r}")
            annual[int(year_match.group(1))].extend(rows)
            actual_rows = len(rows)
        else:
            raise ValueError(f"unexpected source provider: {provider!r}")
        if item.get("rows") != actual_rows:
            raise ValueError(f"source receipt row count mismatch: {path}")
    return receipt, identity_rows, annual, receipt_bytes, quotes


def _unique_index(
    rows: list[dict[str, Any]],
    field: str,
    *,
    source: str,
    ignore_nonlisted_codes: bool = False,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = row.get(field)
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
            if ignore_nonlisted_codes:
                continue
            raise ValueError(f"{source} row has invalid {field}: {code!r}")
        if code in result:
            raise ValueError(f"{source} has duplicate security code: {code}")
        result[code] = row
    return result


def assemble_screen(
    *, source_root: Path, source_evidence_relpath: str
) -> dict[str, Any]:
    source_root = source_root.resolve(strict=True)
    source_evidence_relpath = _safe_evidence_relpath(source_evidence_relpath)
    receipt, identity_rows, annual_rows, receipt_bytes, quotes = _load_verified_sources(
        source_root
    )
    identity = _unique_index(
        identity_rows,
        "SECCODE",
        source="CNINFO",
        ignore_nonlisted_codes=True,
    )
    quote_index = _unique_index(quotes, "code", source="Sina")
    annual = {
        year: _unique_index(
            rows,
            "SECURITY_CODE",
            source=f"Eastmoney {year}",
            ignore_nonlisted_codes=True,
        )
        for year, rows in annual_rows.items()
    }
    identity_file = source_root / str(receipt["identity"]["relpath"])
    identity_sha = _sha256(identity_file.read_bytes())
    listing_cutoff = date.fromisoformat(
        str(EXPECTED_RULES["listing_date_on_or_before"])
    )
    minimum_cap = float(EXPECTED_RULES["market_cap_min_cny"])
    minimum_roe = float(EXPECTED_RULES["roe_2025_min_pct"])
    minimum_bps = float(EXPECTED_RULES["bps_2025_min_cny"])
    minimum_positive_profit_years = int(
        EXPECTED_RULES["minimum_positive_parent_profit_years"]
    )
    revenue_cagr_min = float(
        EXPECTED_RULES["revenue_cagr_2023_to_2025_min_ratio"]
    )
    profit_cagr_min = float(
        EXPECTED_RULES["parent_profit_cagr_2023_to_2025_min_ratio"]
    )
    latest_profit_growth_min = float(
        EXPECTED_RULES["parent_profit_growth_2024_to_2025_min_ratio"]
    )
    profit_signal_prior_margin_min = float(
        EXPECTED_RULES["profit_signal_prior_year_net_margin_min_ratio"]
    )
    quality_average_roe_min = float(
        EXPECTED_RULES["durable_quality_average_roe_min_pct"]
    )
    quality_revenue_cagr_min = float(
        EXPECTED_RULES["durable_quality_revenue_cagr_min_ratio"]
    )
    quality_profit_cagr_min = float(
        EXPECTED_RULES["durable_quality_parent_profit_cagr_min_ratio"]
    )
    turnaround_roe_min = float(
        EXPECTED_RULES["profitable_turnaround_roe_2025_min_pct"]
    )
    rows: list[dict[str, Any]] = []
    for code in sorted(quote_index):
        quote = quote_index[code]
        raw_identity = identity.get(code)
        yearly = {year: annual[year].get(code) for year in _YEARS}
        profits = {
            str(year): _number(row.get("PARENT_NETPROFIT")) if row is not None else None
            for year, row in yearly.items()
        }
        revenues = {
            str(year): _number(row.get("TOTAL_OPERATE_INCOME"))
            if row is not None
            else None
            for year, row in yearly.items()
        }
        roes = {
            str(year): _number(row.get("WEIGHTAVG_ROE")) if row is not None else None
            for year, row in yearly.items()
        }
        latest_two_values = (profits["2024"], profits["2025"])
        latest_two = (
            None
            if any(value is None for value in latest_two_values)
            else all(value < 0 for value in latest_two_values if value is not None)
        )
        cninfo_list_date = (
            str(raw_identity["F006D"])
            if raw_identity is not None and raw_identity.get("F006D") is not None
            else None
        )
        row: dict[str, Any] = {
            "security_code": code,
            "exchange": _exchange(code, raw_identity),
            "board": _board(raw_identity),
            "name": str(quote.get("name") or ""),
            "canonical_orgname": str(
                (raw_identity or {}).get("ORGNAME") or quote.get("name") or ""
            ),
            "list_date": cninfo_list_date,
            "cninfo_sectype": (raw_identity or {}).get("F003V"),
            "cninfo_status": (raw_identity or {}).get("F011V"),
            "cninfo_list_date": cninfo_list_date,
            "cninfo_source_sha256": identity_sha,
            "cninfo_raw_row": raw_identity,
            "cninfo_raw_row_sha256": _canonical_row_sha256(raw_identity),
            "market_cap_cny": _scaled_cny(quote.get("mktcap")),
            "float_market_cap_cny": _scaled_cny(quote.get("nmc")),
            "last_price": _number(quote.get("trade")),
            "daily_amount_cny": _number(quote.get("amount")),
            "turnover_pct": _number(quote.get("turnoverratio")),
            "profits": profits,
            "revenues": revenues,
            "roes": roes,
            "roe_2025": _number((yearly[2025] or {}).get("WEIGHTAVG_ROE")),
            "bps_2025": _number((yearly[2025] or {}).get("BPS")),
            "ocf_per_share_2025": _number((yearly[2025] or {}).get("MGJYXJJE")),
            "latest_two_parent_losses": latest_two,
            "quote_raw_row": quote,
            "quote_raw_row_sha256": _canonical_row_sha256(quote),
            "annual_raw_rows": {str(year): yearly[year] for year in _YEARS},
            "annual_raw_row_sha256": {
                str(year): _canonical_row_sha256(yearly[year]) for year in _YEARS
            },
        }
        row["future_potential_metrics"] = _future_potential_metrics(row)
        row["future_research_signals"] = list(
            _future_research_signals(
                row,
                revenue_cagr_min_ratio=revenue_cagr_min,
                parent_profit_cagr_min_ratio=profit_cagr_min,
                latest_parent_profit_growth_min_ratio=latest_profit_growth_min,
                profit_signal_prior_year_net_margin_min_ratio=(
                    profit_signal_prior_margin_min
                ),
                durable_quality_average_roe_min_pct=quality_average_roe_min,
                durable_quality_revenue_cagr_min_ratio=(
                    quality_revenue_cagr_min
                ),
                durable_quality_parent_profit_cagr_min_ratio=(
                    quality_profit_cagr_min
                ),
                profitable_turnaround_roe_min_pct=turnaround_roe_min,
            )
        )
        row["future_evidence_tier"] = _future_evidence_tier(
            tuple(row["future_research_signals"])
        )
        row["cninfo_row_sha256"] = identity_row_sha256(row)
        row["exclude_reasons"] = sorted(
            _derived_reasons(
                row,
                listing_cutoff=listing_cutoff,
                market_cap_min_cny=minimum_cap,
                minimum_positive_profit_years=minimum_positive_profit_years,
                roe_2025_min_pct=minimum_roe,
                bps_2025_min_cny=minimum_bps,
                revenue_cagr_min_ratio=revenue_cagr_min,
                parent_profit_cagr_min_ratio=profit_cagr_min,
                latest_parent_profit_growth_min_ratio=latest_profit_growth_min,
                profit_signal_prior_year_net_margin_min_ratio=(
                    profit_signal_prior_margin_min
                ),
                durable_quality_average_roe_min_pct=quality_average_roe_min,
                durable_quality_revenue_cagr_min_ratio=(
                    quality_revenue_cagr_min
                ),
                durable_quality_parent_profit_cagr_min_ratio=(
                    quality_profit_cagr_min
                ),
                profitable_turnaround_roe_min_pct=turnaround_roe_min,
            )
        )
        rows.append(row)

    selected = [row for row in rows if not row["exclude_reasons"]]
    selected.sort(key=_future_evidence_sort_key)
    selected_codes = {row["security_code"] for row in selected}
    excluded = sorted(
        (row for row in rows if row["security_code"] not in selected_codes),
        key=lambda row: row["security_code"],
    )
    source_receipt_sha = _sha256(receipt_bytes)
    screen: dict[str, Any] = {
        "schema": SCREEN_SCHEMA,
        "observed_at_utc": receipt["completed_at_utc"],
        "identity_source": {
            "provider": "CNINFO p_stock2101",
            "evidence_relpath": f"{source_evidence_relpath}/cninfo/p-stock2101.json",
            "rows": len(identity_rows),
            "sha256": identity_sha,
            "source_bundle_receipt_sha256": source_receipt_sha,
        },
        "quote_source": {
            "provider": "Sina Market Center hs_a",
            "evidence_relpath": f"{source_evidence_relpath}/sina",
            "rows": len(quotes),
            "source_bundle_receipt_sha256": source_receipt_sha,
            "unit_note": "mktcap/nmc multiplied by 10,000 to CNY",
        },
        "annual_source": {
            "provider": "Eastmoney data center RPT_LICO_FN_CPD",
            "evidence_relpath": f"{source_evidence_relpath}/eastmoney",
            "raw_rows": sum(len(value) for value in annual_rows.values()),
            "source_bundle_receipt_sha256": source_receipt_sha,
            "years": list(_YEARS),
        },
        "rules_draft": EXPECTED_RULES,
        "selected": selected,
        "excluded": excluded,
    }
    recomputed, _ = recompute_selection(screen)
    if [row["security_code"] for row in recomputed] != [
        row["security_code"] for row in selected
    ]:
        raise RuntimeError("assembled screen failed deterministic self-check")
    return screen


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-evidence-relpath", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    screen = assemble_screen(
        source_root=args.source_root,
        source_evidence_relpath=args.source_evidence_relpath,
    )
    encoded = (
        json.dumps(screen, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    _write_new(args.output, encoded)
    print(
        json.dumps(
            {
                "bytes": len(encoded),
                "excluded": len(screen["excluded"]),
                "output": str(args.output),
                "selected": len(screen["selected"]),
                "sha256": _sha256(encoded),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
