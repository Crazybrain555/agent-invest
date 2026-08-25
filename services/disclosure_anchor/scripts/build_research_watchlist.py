"""Rebuild the reviewed research-priority watchlist from a frozen screen.

The input is a normalized, immutable public-data screen artifact.  Network
fetching deliberately stays outside this command: rebuilding the Git snapshot
must never silently replace the observation date or provider evidence.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import io
import json
import math
import os
import re
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

from disclosure_anchor.application.contracts.research_universe import (
    CNINFO_BOARD_BY_MARKET_CODE,
    EVIDENCE_LIMITATIONS,
    EXCLUSION_REASONS,
    EXPECTED_RULES,
    MANIFEST_SCHEMA,
    PURPOSE,
    RESEARCH_BOARDS,
    SCREEN_SCHEMA,
    SELECTION_RULE_VERSION,
)
from disclosure_anchor.domain.value_objects import canonical_security_identity


EXPECTED_YEARS = ("2023", "2024", "2025")
SUPPORTED_MARKETS = frozenset({"BSE", "SSE", "SZSE"})
ALLOWED_REASONS = EXCLUSION_REASONS
RISK_NAME_RE = re.compile(r"(?:^|[^A-Za-z])\*?ST|退")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
CNINFO_IDENTITY_FIELDS = (
    "cninfo_sectype",
    "cninfo_status",
    "cninfo_list_date",
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _subtract_calendar_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def identity_row_sha256(row: dict[str, Any]) -> str:
    """Hash the retained CNINFO identity projection for one screen row.

    Missing identities are represented by explicit null CNINFO fields.  The
    hash still binds that absence marker, the code, fallback display name, and
    the exact frozen source snapshot instead of inventing a provider row.
    """

    projection = {
        "security_code": row.get("security_code"),
        "canonical_orgname": row.get("canonical_orgname"),
        "board": row.get("board"),
        "cninfo_sectype": row.get("cninfo_sectype"),
        "cninfo_status": row.get("cninfo_status"),
        "cninfo_list_date": row.get("cninfo_list_date"),
        "cninfo_source_sha256": row.get("cninfo_source_sha256"),
    }
    payload = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def cninfo_raw_row_sha256(raw_row: dict[str, Any]) -> str:
    """Hash one full parsed CNINFO provider row using canonical JSON bytes."""

    if not isinstance(raw_row, dict):
        raise ValueError("CNINFO raw row must be an object")
    try:
        payload = json.dumps(
            raw_row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("CNINFO raw row is not canonical-JSON serializable") from exc
    return _sha256_bytes(payload)


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


def _json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read screen JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("screen root must be an object")
    return payload


def _write_new(path: Path, payload: bytes) -> None:
    """Create one immutable generated artifact without replacing reviewed bytes."""

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


def _evidence_relpath(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"{label} must be a safe relative path")
    if path.parts[0] != "watchlist":
        raise ValueError(f"{label} must be relative to the evidence/watchlist root")
    return path.as_posix()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _required_positive_number(values: dict[str, Any], key: str) -> float:
    number = _number(values.get(key))
    if number is None or number <= 0:
        raise ValueError(f"{key} must be a positive number")
    return number


def _annual_profits(row: dict[str, Any]) -> tuple[float | None, ...]:
    profits_raw = row.get("profits")
    profits = profits_raw if isinstance(profits_raw, dict) else {}
    return tuple(_number(profits.get(year)) for year in EXPECTED_YEARS)


def _annual_revenues(row: dict[str, Any]) -> tuple[float | None, ...]:
    revenues_raw = row.get("revenues")
    revenues = revenues_raw if isinstance(revenues_raw, dict) else {}
    return tuple(_number(revenues.get(year)) for year in EXPECTED_YEARS)


def _annual_roes(row: dict[str, Any]) -> tuple[float | None, ...]:
    roes_raw = row.get("roes")
    roes = roes_raw if isinstance(roes_raw, dict) else {}
    return tuple(_number(roes.get(year)) for year in EXPECTED_YEARS)


def _cagr_ratio(start: float, end: float, *, years: int) -> float | None:
    if start <= 0 or end <= 0:
        return None
    return (end / start) ** (1 / years) - 1


def _year_over_year_ratio(start: float, end: float) -> float | None:
    if start <= 0 or end <= 0:
        return None
    return end / start - 1


def _net_margin_ratio(profit: float | None, revenue: float | None) -> float | None:
    if profit is None or revenue is None or revenue <= 0:
        return None
    return profit / revenue


def _complete_average(values: tuple[float | None, ...]) -> float | None:
    if any(value is None for value in values):
        return None
    return math.fsum(float(value) for value in values if value is not None) / len(
        values
    )


def _future_potential_metrics(row: dict[str, Any]) -> dict[str, float | None]:
    profits = _annual_profits(row)
    revenues = _annual_revenues(row)
    roes = _annual_roes(row)
    if any(value is None for value in (*profits, *revenues)):
        return {
            "revenue_cagr_2023_to_2025": None,
            "parent_profit_cagr_2023_to_2025": None,
            "parent_profit_growth_2024_to_2025": None,
            "average_roe_2023_to_2025": _complete_average(roes),
        }
    profit_2023_raw, profit_2024_raw, profit_2025_raw = profits
    revenue_2023_raw, _, revenue_2025_raw = revenues
    assert profit_2023_raw is not None
    assert profit_2024_raw is not None
    assert profit_2025_raw is not None
    assert revenue_2023_raw is not None
    assert revenue_2025_raw is not None
    profit_2023 = float(profit_2023_raw)
    profit_2024 = float(profit_2024_raw)
    profit_2025 = float(profit_2025_raw)
    revenue_2023 = float(revenue_2023_raw)
    revenue_2025 = float(revenue_2025_raw)
    return {
        "revenue_cagr_2023_to_2025": _cagr_ratio(
            revenue_2023,
            revenue_2025,
            years=2,
        ),
        "parent_profit_cagr_2023_to_2025": _cagr_ratio(
            profit_2023,
            profit_2025,
            years=2,
        ),
        "parent_profit_growth_2024_to_2025": _year_over_year_ratio(
            profit_2024,
            profit_2025,
        ),
        "average_roe_2023_to_2025": _complete_average(roes),
    }


def _future_research_signals(
    row: dict[str, Any],
    *,
    revenue_cagr_min_ratio: float,
    parent_profit_cagr_min_ratio: float,
    latest_parent_profit_growth_min_ratio: float,
    profit_signal_prior_year_net_margin_min_ratio: float,
    durable_quality_average_roe_min_pct: float,
    durable_quality_revenue_cagr_min_ratio: float,
    durable_quality_parent_profit_cagr_min_ratio: float,
    profitable_turnaround_roe_min_pct: float,
) -> tuple[str, ...]:
    metrics = _future_potential_metrics(row)
    revenue_cagr = metrics["revenue_cagr_2023_to_2025"]
    profit_cagr = metrics["parent_profit_cagr_2023_to_2025"]
    latest_profit_growth = metrics["parent_profit_growth_2024_to_2025"]
    average_roe = metrics["average_roe_2023_to_2025"]
    profits = _annual_profits(row)
    revenues = _annual_revenues(row)
    profit_2023, profit_2024, profit_2025 = profits
    revenue_2023, revenue_2024, _ = revenues
    margin_2023 = _net_margin_ratio(profit_2023, revenue_2023)
    margin_2024 = _net_margin_ratio(profit_2024, revenue_2024)
    signals: list[str] = []
    if revenue_cagr is not None and revenue_cagr >= revenue_cagr_min_ratio:
        signals.append("revenue_cagr_meets_floor")
    if (
        profit_cagr is not None
        and profit_cagr >= parent_profit_cagr_min_ratio
        and margin_2023 is not None
        and margin_2023 >= profit_signal_prior_year_net_margin_min_ratio
    ):
        signals.append("parent_profit_cagr_meets_floor_and_base_quality")
    if (
        latest_profit_growth is not None
        and latest_profit_growth >= latest_parent_profit_growth_min_ratio
        and margin_2024 is not None
        and margin_2024 >= profit_signal_prior_year_net_margin_min_ratio
    ):
        signals.append("latest_parent_profit_growth_meets_floor_and_base_quality")
    if (
        average_roe is not None
        and average_roe >= durable_quality_average_roe_min_pct
        and revenue_cagr is not None
        and revenue_cagr >= durable_quality_revenue_cagr_min_ratio
        and profit_cagr is not None
        and profit_cagr >= durable_quality_parent_profit_cagr_min_ratio
    ):
        signals.append("durable_quality_compounder")
    latest_roe = _number(row.get("roe_2025"))
    latest_turnaround = (
        profit_2023 is not None
        and profit_2024 is not None
        and profit_2025 is not None
        and profit_2025 > 0
        and (
            profit_2024 <= 0
            or (profit_2023 <= 0 and profit_2025 >= profit_2024)
        )
    )
    if (
        latest_turnaround
        and latest_roe is not None
        and latest_roe >= profitable_turnaround_roe_min_pct
    ):
        signals.append("profitable_turnaround_with_quality_floor")
    return tuple(signals)


def _future_evidence_tier(signals: tuple[str, ...]) -> str | None:
    if len(signals) >= 3:
        return "A"
    if len(signals) == 2:
        return "B"
    if len(signals) == 1:
        return "C"
    return None


def _descending_metric(value: Any) -> tuple[bool, float]:
    number = _number(value)
    return number is None, -(number or 0.0)


def _future_evidence_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    signals = tuple(str(value) for value in row["future_research_signals"])
    metrics = row["future_potential_metrics"]
    tier = _future_evidence_tier(signals)
    tier_order = {"A": 0, "B": 1, "C": 2}
    return (
        tier_order.get(tier, 3) if tier is not None else 3,
        -len(signals),
        -int("durable_quality_compounder" in signals),
        _descending_metric(metrics["revenue_cagr_2023_to_2025"]),
        _descending_metric(metrics["parent_profit_cagr_2023_to_2025"]),
        _descending_metric(metrics["parent_profit_growth_2024_to_2025"]),
        _descending_metric(row.get("roe_2025")),
        str(row["security_code"]),
    )


def _latest_two_parent_losses(row: dict[str, Any]) -> bool | None:
    latest_two = _annual_profits(row)[-2:]
    if any(value is None for value in latest_two):
        return None
    return all(value is not None and value < 0 for value in latest_two)


def _validate_identity_provenance(
    row: dict[str, Any], *, identity_source_sha256: str
) -> None:
    code = row.get("security_code")
    if row.get("cninfo_source_sha256") != identity_source_sha256:
        raise ValueError(f"{code}: cninfo_source_sha256 does not match identity source")
    row_hash = row.get("cninfo_row_sha256")
    if not isinstance(row_hash, str) or not SHA256_RE.fullmatch(row_hash):
        raise ValueError(f"{code}: cninfo_row_sha256 must be a lowercase SHA-256")
    expected_row_hash = identity_row_sha256(row)
    if row_hash != expected_row_hash:
        raise ValueError(f"{code}: cninfo_row_sha256 does not match retained identity")

    canonical_orgname = row.get("canonical_orgname")
    if not isinstance(canonical_orgname, str) or not canonical_orgname.strip():
        raise ValueError(f"{code}: canonical_orgname must be non-empty text")
    board = row.get("board")
    if board is not None and board not in CNINFO_BOARD_BY_MARKET_CODE.values():
        raise ValueError(f"{code}: board must be a known CNINFO board or null")
    missing_fields = [field for field in CNINFO_IDENTITY_FIELDS if field not in row]
    if missing_fields:
        raise ValueError(
            f"{code}: retained CNINFO identity fields missing {missing_fields}"
        )
    identity_values = tuple(row.get(field) for field in CNINFO_IDENTITY_FIELDS)
    if any(value is not None and not isinstance(value, str) for value in identity_values):
        raise ValueError(f"{code}: CNINFO identity fields must be text or null")
    if any(value is None for value in identity_values) and not all(
        value is None for value in identity_values
    ):
        raise ValueError(f"{code}: CNINFO identity fields must be complete or all null")

    for field in ("cninfo_raw_row", "cninfo_raw_row_sha256"):
        if field not in row:
            raise ValueError(f"{code}: retained CNINFO raw identity field missing {field}")
    raw_row = row.get("cninfo_raw_row")
    raw_row_hash = row.get("cninfo_raw_row_sha256")
    if raw_row is None:
        if raw_row_hash is not None:
            raise ValueError(
                f"{code}: cninfo_raw_row_sha256 must be null without a raw row"
            )
        if not all(value is None for value in identity_values):
            raise ValueError(f"{code}: retained CNINFO identity has no raw provider row")
        if board is not None:
            raise ValueError(f"{code}: board must be null without a raw provider row")
    else:
        if not isinstance(raw_row, dict):
            raise ValueError(f"{code}: cninfo_raw_row must be an object or null")
        if not isinstance(raw_row_hash, str) or not SHA256_RE.fullmatch(raw_row_hash):
            raise ValueError(
                f"{code}: cninfo_raw_row_sha256 must be a lowercase SHA-256"
            )
        if raw_row_hash != cninfo_raw_row_sha256(raw_row):
            raise ValueError(f"{code}: cninfo_raw_row_sha256 does not match raw row")
        retained_from_raw = {
            "security_code": raw_row.get("SECCODE"),
            "canonical_orgname": raw_row.get("ORGNAME"),
            "cninfo_sectype": raw_row.get("F003V"),
            "cninfo_status": raw_row.get("F011V"),
            "cninfo_list_date": raw_row.get("F006D"),
            "board": CNINFO_BOARD_BY_MARKET_CODE.get(
                str(raw_row.get("F004V") or "")
            ),
        }
        retained = {
            field: row.get(field)
            for field in (
                "security_code",
                "canonical_orgname",
                *CNINFO_IDENTITY_FIELDS,
                "board",
            )
        }
        if retained != retained_from_raw:
            raise ValueError(f"{code}: retained CNINFO identity does not match raw row")

    cninfo_list_date = row.get("cninfo_list_date")
    if row.get("list_date") != cninfo_list_date:
        raise ValueError(f"{code}: list_date must exactly match cninfo_list_date")


def _validate_market_and_financial_provenance(row: dict[str, Any]) -> None:
    code = row.get("security_code")
    quote = row.get("quote_raw_row")
    quote_hash = row.get("quote_raw_row_sha256")
    if not isinstance(quote, dict):
        raise ValueError(f"{code}: quote_raw_row must be an object")
    if (
        not isinstance(quote_hash, str)
        or not SHA256_RE.fullmatch(quote_hash)
        or quote_hash != cninfo_raw_row_sha256(quote)
    ):
        raise ValueError(f"{code}: quote raw row hash does not match raw row")
    expected_quote = {
        "security_code": quote.get("code"),
        "name": str(quote.get("name") or ""),
        "market_cap_cny": _scaled_cny(quote.get("mktcap")),
        "float_market_cap_cny": _scaled_cny(quote.get("nmc")),
        "last_price": _number(quote.get("trade")),
        "daily_amount_cny": _number(quote.get("amount")),
        "turnover_pct": _number(quote.get("turnoverratio")),
    }
    retained_quote = {field: row.get(field) for field in expected_quote}
    if retained_quote != expected_quote:
        raise ValueError(f"{code}: retained quote projection does not match raw row")

    annual_rows = row.get("annual_raw_rows")
    annual_hashes = row.get("annual_raw_row_sha256")
    profits = row.get("profits")
    revenues = row.get("revenues")
    roes = row.get("roes")
    expected_keys = set(EXPECTED_YEARS)
    if not all(
        isinstance(value, dict) and set(value) == expected_keys
        for value in (annual_rows, annual_hashes, profits, revenues, roes)
    ):
        raise ValueError(f"{code}: annual rows/projections must cover exact years")
    assert isinstance(annual_rows, dict)
    assert isinstance(annual_hashes, dict)
    assert isinstance(profits, dict)
    assert isinstance(revenues, dict)
    assert isinstance(roes, dict)
    for year in EXPECTED_YEARS:
        raw = annual_rows[year]
        raw_hash = annual_hashes[year]
        if raw is None:
            if raw_hash is not None or any(
                projection[year] is not None
                for projection in (profits, revenues, roes)
            ):
                raise ValueError(
                    f"{code}: missing annual {year} row has retained values"
                )
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"{code}: annual {year} raw row must be an object")
        if (
            not isinstance(raw_hash, str)
            or not SHA256_RE.fullmatch(raw_hash)
            or raw_hash != cninfo_raw_row_sha256(raw)
        ):
            raise ValueError(
                f"{code}: annual {year} raw row hash does not match raw row"
            )
        expected_report_date = f"{year}-12-31 00:00:00"
        if (
            raw.get("SECURITY_CODE") != code
            or raw.get("REPORTDATE") != expected_report_date
        ):
            raise ValueError(f"{code}: annual {year} row identity is invalid")
        projected = {
            "profit": profits[year],
            "revenue": revenues[year],
            "roe": roes[year],
        }
        expected = {
            "profit": _number(raw.get("PARENT_NETPROFIT")),
            "revenue": _number(raw.get("TOTAL_OPERATE_INCOME")),
            "roe": _number(raw.get("WEIGHTAVG_ROE")),
        }
        if projected != expected:
            raise ValueError(
                f"{code}: annual {year} projection does not match raw row"
            )
    latest = annual_rows["2025"] or {}
    expected_latest = {
        "roe_2025": _number(latest.get("WEIGHTAVG_ROE")),
        "bps_2025": _number(latest.get("BPS")),
        "ocf_per_share_2025": _number(latest.get("MGJYXJJE")),
    }
    retained_latest = {field: row.get(field) for field in expected_latest}
    if retained_latest != expected_latest:
        raise ValueError(f"{code}: latest annual projection does not match raw row")


def _derived_reasons(
    row: dict[str, Any],
    *,
    listing_cutoff: date,
    market_cap_min_cny: float,
    minimum_positive_profit_years: int,
    roe_2025_min_pct: float,
    bps_2025_min_cny: float,
    revenue_cagr_min_ratio: float,
    parent_profit_cagr_min_ratio: float,
    latest_parent_profit_growth_min_ratio: float,
    profit_signal_prior_year_net_margin_min_ratio: float,
    durable_quality_average_roe_min_pct: float,
    durable_quality_revenue_cagr_min_ratio: float,
    durable_quality_parent_profit_cagr_min_ratio: float,
    profitable_turnaround_roe_min_pct: float,
) -> set[str]:
    reasons: set[str] = set()
    code = row.get("security_code")
    exchange = row.get("exchange")
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"invalid security_code: {code!r}")
    if not isinstance(exchange, str) or exchange not in SUPPORTED_MARKETS:
        reasons.add("unsupported_exchange")
    else:
        try:
            canonical_security_identity(code, exchange)
        except ValueError as exc:
            raise ValueError(f"{code}: invalid canonical exchange: {exc}") from exc
    if row.get("board") not in RESEARCH_BOARDS:
        reasons.add("outside_research_boards")

    identity_values = tuple(row.get(field) for field in CNINFO_IDENTITY_FIELDS)
    identity_missing = all(value is None for value in identity_values)
    if identity_missing:
        reasons.add("missing_cninfo_identity")
    else:
        if row.get("cninfo_sectype") != "A股":
            reasons.add("cninfo_not_a_share")
        if row.get("cninfo_status") != "正常上市":
            reasons.add("cninfo_not_normal_listed")

    name = row.get("name")
    if isinstance(name, str) and RISK_NAME_RE.search(name.upper()):
        reasons.add("risk_warning_name")

    list_date_raw = row.get("cninfo_list_date")
    if not isinstance(list_date_raw, str) or not list_date_raw:
        reasons.add("missing_listing_date")
    else:
        try:
            listed_on = date.fromisoformat(list_date_raw)
        except ValueError as exc:
            raise ValueError(f"{code}: invalid list_date {list_date_raw!r}") from exc
        if listed_on > listing_cutoff:
            reasons.add("listing_age_lt_3_5y")

    market_cap = _number(row.get("market_cap_cny"))
    if market_cap is None or market_cap < market_cap_min_cny:
        reasons.add("market_cap_lt_2bn")
    annual_profits = _annual_profits(row)
    annual_revenues = _annual_revenues(row)
    annual_roes = _annual_roes(row)
    if any(
        value is None for value in (*annual_profits, *annual_revenues, *annual_roes)
    ):
        reasons.add("missing_annual_financial")
    else:
        latest_profit = annual_profits[-1]
        if latest_profit is None or latest_profit <= 0:
            reasons.add("latest_parent_profit_nonpositive")
        if sum(
            value is not None and value > 0 for value in annual_profits
        ) < minimum_positive_profit_years:
            reasons.add("fewer_than_two_positive_parent_profit_years")

    bps = _number(row.get("bps_2025"))
    if bps is None or bps < bps_2025_min_cny:
        reasons.add("missing_or_low_2025_bps")
    latest_roe = _number(row.get("roe_2025"))
    if latest_roe is None or latest_roe < roe_2025_min_pct:
        reasons.add("missing_or_low_2025_roe")
    if not reasons and not _future_research_signals(
        row,
        revenue_cagr_min_ratio=revenue_cagr_min_ratio,
        parent_profit_cagr_min_ratio=parent_profit_cagr_min_ratio,
        latest_parent_profit_growth_min_ratio=(
            latest_parent_profit_growth_min_ratio
        ),
        profit_signal_prior_year_net_margin_min_ratio=(
            profit_signal_prior_year_net_margin_min_ratio
        ),
        durable_quality_average_roe_min_pct=(
            durable_quality_average_roe_min_pct
        ),
        durable_quality_revenue_cagr_min_ratio=(
            durable_quality_revenue_cagr_min_ratio
        ),
        durable_quality_parent_profit_cagr_min_ratio=(
            durable_quality_parent_profit_cagr_min_ratio
        ),
        profitable_turnaround_roe_min_pct=profitable_turnaround_roe_min_pct,
    ):
        reasons.add("no_future_research_signal")
    return reasons


def recompute_selection(screen: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if screen.get("schema") != SCREEN_SCHEMA:
        raise ValueError(
            f"screen schema must be {SCREEN_SCHEMA!r}; got {screen.get('schema')!r}"
        )
    rules = screen.get("rules_draft")
    if not isinstance(rules, dict):
        raise ValueError("screen rules_draft must be an object")
    if rules != EXPECTED_RULES:
        raise ValueError(
            "screen rules_draft must exactly match "
            f"{SELECTION_RULE_VERSION}: {EXPECTED_RULES!r}"
        )
    selection_count_min = rules.get("selection_count_min")
    selection_count_max = rules.get("selection_count_max")
    if (
        isinstance(selection_count_min, bool)
        or not isinstance(selection_count_min, int)
        or isinstance(selection_count_max, bool)
        or not isinstance(selection_count_max, int)
        or selection_count_min < 1
        or selection_count_max < selection_count_min
    ):
        raise ValueError("selection count band must contain positive integers")
    cutoff_raw = rules.get("listing_date_on_or_before")
    if not isinstance(cutoff_raw, str):
        raise ValueError("listing_date_on_or_before must be an ISO date")
    listing_cutoff = date.fromisoformat(cutoff_raw)
    minimum_listing_age_months = rules.get("minimum_listing_age_months")
    if (
        isinstance(minimum_listing_age_months, bool)
        or not isinstance(minimum_listing_age_months, int)
        or minimum_listing_age_months < 1
    ):
        raise ValueError("minimum_listing_age_months must be a positive integer")
    observed_at_raw = screen.get("observed_at_utc")
    if not isinstance(observed_at_raw, str):
        raise ValueError("observed_at_utc must be an aware UTC timestamp")
    try:
        observed_at = datetime.fromisoformat(observed_at_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at_utc must be an aware UTC timestamp") from exc
    if observed_at.tzinfo is None or observed_at.utcoffset() != timezone.utc.utcoffset(
        observed_at
    ):
        raise ValueError("observed_at_utc must be an aware UTC timestamp")
    expected_listing_cutoff = _subtract_calendar_months(
        observed_at.date(), minimum_listing_age_months
    )
    if listing_cutoff != expected_listing_cutoff:
        raise ValueError(
            "listing_date_on_or_before does not match observed_at_utc minus "
            f"minimum_listing_age_months; expected {expected_listing_cutoff}"
        )
    market_cap_min = _number(rules.get("market_cap_min_cny"))
    if market_cap_min is None or market_cap_min <= 0:
        raise ValueError("market_cap_min_cny must be positive")
    revenue_cagr_min = _required_positive_number(
        rules, "revenue_cagr_2023_to_2025_min_ratio"
    )
    profit_cagr_min = _required_positive_number(
        rules, "parent_profit_cagr_2023_to_2025_min_ratio"
    )
    latest_profit_growth_min = _required_positive_number(
        rules, "parent_profit_growth_2024_to_2025_min_ratio"
    )
    profit_signal_prior_margin_min = _required_positive_number(
        rules, "profit_signal_prior_year_net_margin_min_ratio"
    )
    quality_average_roe_min = _required_positive_number(
        rules, "durable_quality_average_roe_min_pct"
    )
    quality_revenue_cagr_min = _required_positive_number(
        rules, "durable_quality_revenue_cagr_min_ratio"
    )
    quality_profit_cagr_min = _required_positive_number(
        rules, "durable_quality_parent_profit_cagr_min_ratio"
    )
    turnaround_roe_min = _required_positive_number(
        rules, "profitable_turnaround_roe_2025_min_pct"
    )
    roe_2025_min = _required_positive_number(rules, "roe_2025_min_pct")
    bps_2025_min = _required_positive_number(rules, "bps_2025_min_cny")
    minimum_positive_profit_years = rules.get(
        "minimum_positive_parent_profit_years"
    )
    if (
        isinstance(minimum_positive_profit_years, bool)
        or not isinstance(minimum_positive_profit_years, int)
        or not 1 <= minimum_positive_profit_years <= len(EXPECTED_YEARS)
    ):
        raise ValueError(
            "minimum_positive_parent_profit_years must cover one to three years"
        )
    identity_source = screen.get("identity_source")
    if not isinstance(identity_source, dict):
        raise ValueError("identity_source must be an object")
    identity_source_sha256 = identity_source.get("sha256")
    if not isinstance(identity_source_sha256, str) or not SHA256_RE.fullmatch(
        identity_source_sha256
    ):
        raise ValueError("identity_source.sha256 must be a lowercase SHA-256")

    selected_raw = screen.get("selected")
    excluded_raw = screen.get("excluded")
    if not isinstance(selected_raw, list) or not isinstance(excluded_raw, list):
        raise ValueError("screen selected/excluded must be arrays")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in (*selected_raw, *excluded_raw):
        if not isinstance(item, dict):
            raise ValueError("every screen row must be an object")
        code = item.get("security_code")
        if not isinstance(code, str) or code in seen:
            raise ValueError(f"duplicate or invalid security_code: {code!r}")
        seen.add(code)
        _validate_identity_provenance(
            item,
            identity_source_sha256=identity_source_sha256,
        )
        _validate_market_and_financial_provenance(item)
        if "latest_two_parent_losses" not in item:
            raise ValueError(f"{code}: latest_two_parent_losses observation is required")
        expected_latest_two = _latest_two_parent_losses(item)
        if item.get("latest_two_parent_losses") is not expected_latest_two:
            raise ValueError(
                f"{code}: latest_two_parent_losses observation drift; "
                f"expected {expected_latest_two!r}"
            )
        expected_metrics = _future_potential_metrics(item)
        if item.get("future_potential_metrics") != expected_metrics:
            raise ValueError(
                f"{code}: future_potential_metrics drift; "
                f"expected {expected_metrics!r}"
            )
        expected_future_signals = list(
            _future_research_signals(
                item,
                revenue_cagr_min_ratio=revenue_cagr_min,
                parent_profit_cagr_min_ratio=profit_cagr_min,
                latest_parent_profit_growth_min_ratio=latest_profit_growth_min,
                profit_signal_prior_year_net_margin_min_ratio=(
                    profit_signal_prior_margin_min
                ),
                durable_quality_average_roe_min_pct=quality_average_roe_min,
                durable_quality_revenue_cagr_min_ratio=quality_revenue_cagr_min,
                durable_quality_parent_profit_cagr_min_ratio=quality_profit_cagr_min,
                profitable_turnaround_roe_min_pct=turnaround_roe_min,
            )
        )
        if item.get("future_research_signals") != expected_future_signals:
            raise ValueError(
                f"{code}: future_research_signals drift; "
                f"expected {expected_future_signals!r}"
            )
        expected_tier = _future_evidence_tier(tuple(expected_future_signals))
        if item.get("future_evidence_tier") != expected_tier:
            raise ValueError(
                f"{code}: future_evidence_tier drift; expected {expected_tier!r}"
            )
        recorded_raw = item.get("exclude_reasons")
        if not isinstance(recorded_raw, list) or not all(
            isinstance(reason, str) for reason in recorded_raw
        ):
            raise ValueError(f"{code}: exclude_reasons must be a string array")
        recorded = set(recorded_raw)
        if len(recorded) != len(recorded_raw):
            raise ValueError(f"{code}: exclude_reasons must not contain duplicates")
        unknown = recorded - ALLOWED_REASONS
        if unknown:
            raise ValueError(f"{code}: unknown exclusion reasons {sorted(unknown)}")
        expected = _derived_reasons(
            item,
            listing_cutoff=listing_cutoff,
            market_cap_min_cny=market_cap_min,
            minimum_positive_profit_years=minimum_positive_profit_years,
            roe_2025_min_pct=roe_2025_min,
            bps_2025_min_cny=bps_2025_min,
            revenue_cagr_min_ratio=revenue_cagr_min,
            parent_profit_cagr_min_ratio=profit_cagr_min,
            latest_parent_profit_growth_min_ratio=latest_profit_growth_min,
            profit_signal_prior_year_net_margin_min_ratio=(
                profit_signal_prior_margin_min
            ),
            durable_quality_average_roe_min_pct=quality_average_roe_min,
            durable_quality_revenue_cagr_min_ratio=quality_revenue_cagr_min,
            durable_quality_parent_profit_cagr_min_ratio=quality_profit_cagr_min,
            profitable_turnaround_roe_min_pct=turnaround_roe_min,
        )
        if recorded != expected:
            raise ValueError(
                f"{code}: exclusion reason drift; expected {sorted(expected)}, "
                f"got {sorted(recorded)}"
            )
        rows.append(item)

    selected = [row for row in rows if not row["exclude_reasons"]]
    selected.sort(key=_future_evidence_sort_key)
    if not selection_count_min <= len(selected) <= selection_count_max:
        raise ValueError(
            "future-potential threshold outcome is outside the reviewed "
            f"selection band: {len(selected)}"
        )
    recorded_selected_codes = [str(row["security_code"]) for row in selected_raw]
    recomputed_codes = [str(row["security_code"]) for row in selected]
    if recomputed_codes != recorded_selected_codes:
        raise ValueError("recorded selected rows/order do not match recomputed ranking")
    for item in excluded_raw:
        code = str(item["security_code"])
        if not item["exclude_reasons"]:
            raise ValueError(f"{code}: excluded row must carry an exclusion reason")

    counts = Counter(
        reason for row in rows for reason in row["exclude_reasons"]
    )
    return selected, dict(sorted(counts.items()))


def render_watchlist(selected: list[dict[str, Any]], *, joined_date: str) -> str:
    header = (
        "# disclosure_anchor research-priority universe — generated from a frozen public screen.\n"
        "# Selection purpose/rules/source hashes: config/watchlist-screen.v1.json; "
        "DB tracked_company remains runtime authority.\n"
        "# Restore/import with: make track [PRUNE_DRIFT=YES]; blank optional cells inherit global defaults.\n"
        "security_code,exchange,status,joined_date,lookback_days,sync_frequency,process_classes,note\n"
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in selected:
        writer.writerow(
            [
                row["security_code"],
                row["exchange"],
                "active",
                joined_date,
                "",
                "",
                "",
                row["canonical_orgname"],
            ]
        )
    return header + buffer.getvalue()


def build_manifest(
    *,
    screen_path: Path,
    screen: dict[str, Any],
    screen_bytes: bytes,
    selected: list[dict[str, Any]],
    exclusion_counts: dict[str, int],
    csv_bytes: bytes,
    receipt_path: Path | None,
    screen_evidence_relpath: str | None = None,
    fetch_receipt_evidence_relpath: str | None = None,
) -> dict[str, Any]:
    rules = screen["rules_draft"]
    all_rows = [*screen["selected"], *screen["excluded"]]
    screen_evidence_relpath = _evidence_relpath(
        screen_evidence_relpath,
        label="screen_evidence_relpath",
    )
    fetch_receipt_evidence_relpath = _evidence_relpath(
        fetch_receipt_evidence_relpath,
        label="fetch_receipt_evidence_relpath",
    )
    selected_identity = [
        {"security_code": row["security_code"], "exchange": row["exchange"]}
        for row in selected
    ]
    selected_identity_bytes = json.dumps(
        selected_identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    receipt: dict[str, Any] | None = None
    if receipt_path is not None:
        receipt_bytes = receipt_path.read_bytes()
        receipt = {
            "logical_name": receipt_path.name,
            "sha256": _sha256_bytes(receipt_bytes),
            "bytes": len(receipt_bytes),
        }
        if fetch_receipt_evidence_relpath is not None:
            receipt["evidence_relpath"] = fetch_receipt_evidence_relpath
    screen_evidence = {
        "logical_name": screen_path.name,
        "schema": screen["schema"],
        "sha256": _sha256_bytes(screen_bytes),
        "bytes": len(screen_bytes),
        "row_count": len(screen["selected"]) + len(screen["excluded"]),
    }
    if screen_evidence_relpath is not None:
        screen_evidence["evidence_relpath"] = screen_evidence_relpath
    closed_exclusion_counts = {
        reason: exclusion_counts.get(reason, 0)
        for reason in sorted(EXCLUSION_REASONS)
    }
    return {
        "schema": MANIFEST_SCHEMA,
        "purpose": PURPOSE,
        "selection_rule_version": SELECTION_RULE_VERSION,
        "observed_at_utc": screen["observed_at_utc"],
        "joined_date": str(screen["observed_at_utc"])[:10],
        "screen": screen_evidence,
        "fetch_receipt": receipt,
        "sources": {
            "identity": screen["identity_source"],
            "quotes": screen["quote_source"],
            "annual": screen["annual_source"],
        },
        "rules": rules,
        "evidence_limitations": EVIDENCE_LIMITATIONS,
        "result": {
            "selected_count": len(selected),
            "selected_exchange_counts": dict(
                sorted(Counter(str(row["exchange"]) for row in selected).items())
            ),
            "selected_board_counts": dict(
                sorted(Counter(str(row["board"]) for row in selected).items())
            ),
            "selected_min_market_cap_cny": min(
                float(row["market_cap_cny"]) for row in selected
            ),
            "selected_evidence_tier_counts": dict(
                sorted(
                    Counter(str(row["future_evidence_tier"]) for row in selected).items()
                )
            ),
            "eligible_after_hard_gates": len(selected)
            + closed_exclusion_counts["no_future_research_signal"],
            "exclusion_reason_counts": closed_exclusion_counts,
            "observation_counts": {
                "latest_two_parent_losses_all_rows": sum(
                    row["latest_two_parent_losses"] is True for row in all_rows
                ),
                "latest_two_parent_losses_selected": sum(
                    row["latest_two_parent_losses"] is True for row in selected
                ),
                "latest_parent_profit_nonpositive_all_rows": sum(
                    (value := _annual_profits(row)[-1]) is not None and value <= 0
                    for row in all_rows
                ),
                "latest_parent_profit_nonpositive_selected": sum(
                    (value := _annual_profits(row)[-1]) is not None and value <= 0
                    for row in selected
                ),
                "future_research_signal_all_rows": sum(
                    bool(row["future_research_signals"]) for row in all_rows
                ),
                "future_research_signal_selected": sum(
                    bool(row["future_research_signals"]) for row in selected
                ),
                "ocf_per_share_missing_all_rows": sum(
                    _number(row.get("ocf_per_share_2025")) is None
                    for row in all_rows
                ),
                "ocf_per_share_missing_selected": sum(
                    _number(row.get("ocf_per_share_2025")) is None
                    for row in selected
                ),
                "ocf_per_share_nonpositive_all_rows": sum(
                    (value := _number(row.get("ocf_per_share_2025"))) is not None
                    and value <= 0
                    for row in all_rows
                ),
                "ocf_per_share_nonpositive_selected": sum(
                    (value := _number(row.get("ocf_per_share_2025"))) is not None
                    and value <= 0
                    for row in selected
                ),
            },
            "selected_identity_sha256": _sha256_bytes(selected_identity_bytes),
            "watchlist_csv_sha256": _sha256_bytes(csv_bytes),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_research_watchlist", description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--manifest-out", type=Path, required=True)
    parser.add_argument("--fetch-receipt", type=Path)
    parser.add_argument("--screen-evidence-relpath")
    parser.add_argument("--fetch-receipt-evidence-relpath")
    args = parser.parse_args(argv)

    screen_bytes = args.input.read_bytes()
    screen = _json_object(args.input)
    selected, exclusion_counts = recompute_selection(screen)
    joined_date = str(screen["observed_at_utc"])[:10]
    csv_text = render_watchlist(selected, joined_date=joined_date)
    csv_bytes = csv_text.encode("utf-8")
    manifest = build_manifest(
        screen_path=args.input,
        screen=screen,
        screen_bytes=screen_bytes,
        selected=selected,
        exclusion_counts=exclusion_counts,
        csv_bytes=csv_bytes,
        receipt_path=args.fetch_receipt,
        screen_evidence_relpath=args.screen_evidence_relpath,
        fetch_receipt_evidence_relpath=args.fetch_receipt_evidence_relpath,
    )
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    if args.out.exists() or args.manifest_out.exists():
        raise FileExistsError("watchlist outputs are new-only and must not exist")
    _write_new(args.out, csv_bytes)
    try:
        _write_new(args.manifest_out, manifest_bytes)
    except BaseException:
        args.out.unlink(missing_ok=True)
        raise
    print(
        f"research watchlist: selected={len(selected)} "
        f"csv_sha256={manifest['result']['watchlist_csv_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
