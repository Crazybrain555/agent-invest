"""Exact-byte loading and offline validation for tracked-company imports."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any

from disclosure_anchor.application.contracts.research_universe import (
    EVIDENCE_LIMITATIONS,
    EXCLUSION_REASONS,
    EXPECTED_RULES,
    MANIFEST_SCHEMA,
    PURPOSE,
    SCREEN_SCHEMA,
    SELECTION_RULE_VERSION,
)
from disclosure_anchor.application.use_cases.track_companies import SYNC_FREQUENCIES
from disclosure_anchor.domain.value_objects import canonical_security_identity


EXCHANGES = {"BSE", "SSE", "SZSE"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CODE_RE = re.compile(r"^\d{6}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SERVICE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WATCHLIST = SERVICE_ROOT / "config" / "watchlist.csv"
DEFAULT_SCREEN_MANIFEST = SERVICE_ROOT / "config" / "watchlist-screen.v1.json"
DEFAULT_PROCESSING_POLICY = SERVICE_ROOT / "config" / "processing_policy.json"
WATCHLIST_FIELDS = frozenset(
    {
        "security_code",
        "exchange",
        "status",
        "joined_date",
        "lookback_days",
        "sync_frequency",
        "process_classes",
        "note",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "purpose",
        "selection_rule_version",
        "observed_at_utc",
        "joined_date",
        "screen",
        "fetch_receipt",
        "sources",
        "rules",
        "evidence_limitations",
        "result",
    }
)
_SCREEN_EVIDENCE_FIELDS = frozenset(
    {"logical_name", "schema", "sha256", "bytes", "row_count", "evidence_relpath"}
)
_FETCH_RECEIPT_FIELDS = frozenset(
    {"logical_name", "sha256", "bytes", "evidence_relpath"}
)
_SOURCE_FIELDS = frozenset({"identity", "quotes", "annual"})
_IDENTITY_SOURCE_FIELDS = frozenset(
    {"provider", "evidence_relpath", "rows", "sha256", "source_bundle_receipt_sha256"}
)
_QUOTE_SOURCE_FIELDS = frozenset(
    {"provider", "evidence_relpath", "rows", "source_bundle_receipt_sha256", "unit_note"}
)
_ANNUAL_SOURCE_FIELDS = frozenset(
    {"provider", "evidence_relpath", "raw_rows", "source_bundle_receipt_sha256", "years"}
)
_RESULT_FIELDS = frozenset(
    {
        "selected_count",
        "selected_board_counts",
        "selected_exchange_counts",
        "selected_min_market_cap_cny",
        "selected_evidence_tier_counts",
        "eligible_after_hard_gates",
        "exclusion_reason_counts",
        "observation_counts",
        "selected_identity_sha256",
        "watchlist_csv_sha256",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "latest_two_parent_losses_all_rows",
        "latest_two_parent_losses_selected",
        "latest_parent_profit_nonpositive_all_rows",
        "latest_parent_profit_nonpositive_selected",
        "future_research_signal_all_rows",
        "future_research_signal_selected",
        "ocf_per_share_missing_all_rows",
        "ocf_per_share_missing_selected",
        "ocf_per_share_nonpositive_all_rows",
        "ocf_per_share_nonpositive_selected",
    }
)
_EXCLUSION_REASON_FIELDS = EXCLUSION_REASONS


@dataclass(frozen=True)
class WatchlistSnapshot:
    """One immutable in-memory view used for validation and import parsing."""

    requested_path: Path
    resolved_path: Path
    content: bytes
    sha256: str
    fieldnames: tuple[str, ...] | None
    rows: tuple[dict[str, str | None], ...]
    structural_errors: tuple[str, ...] = ()


def load_watchlist_snapshot(path: Path) -> WatchlistSnapshot:
    """Resolve and read one regular UTF-8 CSV exactly once."""

    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{path}: unavailable: {exc}") from exc
    with resolved.open("rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"{path}: must resolve to a regular file")
        content = handle.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: not valid UTF-8: {exc}") from exc
    reader = csv.DictReader(
        (line for line in io.StringIO(text) if not line.lstrip().startswith("#")),
        strict=True,
    )
    try:
        raw_rows = tuple(reader)
    except csv.Error as exc:
        raise ValueError(f"{path}: invalid CSV: {exc}") from exc
    structural_errors: list[str] = []
    rows: list[dict[str, str | None]] = []
    for row_number, raw_row in enumerate(raw_rows, start=1):
        overflow = raw_row.get(None)
        if overflow:
            structural_errors.append(
                f"{path}:{row_number}: row has fields beyond the header"
            )
        rows.append(
            {
                str(key): value if isinstance(value, str) else None
                for key, value in raw_row.items()
                if key is not None
            }
        )
    return WatchlistSnapshot(
        requested_path=path,
        resolved_path=resolved,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        fieldnames=tuple(reader.fieldnames) if reader.fieldnames is not None else None,
        rows=tuple(rows),
        structural_errors=tuple(structural_errors),
    )


def screen_manifest_for_snapshot(
    snapshot: WatchlistSnapshot,
    *,
    explicit_manifest: Path | None,
    default_watchlist: Path,
    default_manifest: Path,
) -> Path | None:
    """Select the default sidecar by resolved identity, never lexical spelling."""

    if explicit_manifest is not None:
        return explicit_manifest
    try:
        default_resolved = default_watchlist.resolve(strict=True)
    except OSError:
        return None
    if snapshot.resolved_path == default_resolved:
        return default_manifest
    return None


def validate_watchlist_snapshot(
    snapshot: WatchlistSnapshot,
    known_classes: frozenset[str],
) -> list[str]:
    errors: list[str] = []
    path = snapshot.requested_path
    required = {"security_code", "exchange", "status", "joined_date"}
    if snapshot.fieldnames is None or not required <= set(snapshot.fieldnames):
        errors.append(
            f"{path}: header must contain {sorted(required)}; "
            f"got {snapshot.fieldnames}"
        )
        return errors
    duplicate_fields = sorted(
        {
            field
            for field in snapshot.fieldnames
            if snapshot.fieldnames.count(field) > 1
        }
    )
    if duplicate_fields:
        errors.append(f"{path}: duplicate header fields: {duplicate_fields}")
    unknown_fields = sorted(set(snapshot.fieldnames) - WATCHLIST_FIELDS)
    if unknown_fields:
        errors.append(f"{path}: unknown header fields: {unknown_fields}")
    errors.extend(snapshot.structural_errors)
    if "process_classes" not in snapshot.fieldnames:
        errors.append(
            f"{path}: header missing process_classes (renamed from "
            "filing_categories in 0018)"
        )
    seen: dict[str, int] = {}
    for lineno, row in enumerate(snapshot.rows, start=1):
        where = f"{path}:{lineno}"
        code = (row.get("security_code") or "").strip()
        if not code:
            continue
        if not CODE_RE.match(code):
            errors.append(f"{where}: security_code {code!r} is not 6 digits")
        if code in seen:
            errors.append(
                f"{where}: duplicate security_code {code} "
                f"(first at row {seen[code]})"
            )
        seen[code] = lineno
        exchange = (row.get("exchange") or "").strip()
        if exchange and exchange not in EXCHANGES:
            errors.append(
                f"{where}: exchange {exchange!r} not in {sorted(EXCHANGES)}"
            )
        elif exchange and CODE_RE.match(code):
            try:
                canonical_security_identity(code, exchange)
            except ValueError as exc:
                errors.append(f"{where}: {exc}")
        status = (row.get("status") or "").strip() or "active"
        if status not in ("active", "paused"):
            errors.append(f"{where}: status {status!r} must be active|paused")
        joined = (row.get("joined_date") or "").strip()
        if joined and not DATE_RE.match(joined):
            errors.append(f"{where}: joined_date {joined!r} is not YYYY-MM-DD")
        lookback = (row.get("lookback_days") or "").strip()
        if lookback and not lookback.isdigit():
            errors.append(
                f"{where}: lookback_days {lookback!r} is not a non-negative int"
            )
        frequency = (row.get("sync_frequency") or "").strip()
        if frequency and frequency not in SYNC_FREQUENCIES:
            errors.append(
                f"{where}: sync_frequency {frequency!r} not in {SYNC_FREQUENCIES}"
            )
        classes_raw = (row.get("process_classes") or "").strip()
        for item in (seg.strip() for seg in classes_raw.split(";") if seg.strip()):
            if item not in known_classes:
                errors.append(
                    f"{where}: unknown process_classes value {item!r} "
                    "(see class_map.json)"
                )
    return errors


def validate_screen_manifest(
    snapshot: WatchlistSnapshot,
    manifest_path: Path,
) -> list[str]:
    errors: list[str] = []
    try:
        manifest_bytes = manifest_path.resolve(strict=True).read_bytes()
    except OSError as exc:
        return [f"{manifest_path}: unavailable: {exc}"]
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"{manifest_path}: invalid JSON: {exc}"]
    if not isinstance(manifest, dict):
        return [f"{manifest_path}: root must be an object"]
    _validate_closed_shape(
        errors,
        manifest_path=manifest_path,
        label="manifest",
        value=manifest,
        expected=_MANIFEST_FIELDS,
    )
    if manifest.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"{manifest_path}: schema must be {MANIFEST_SCHEMA}")
    if manifest.get("purpose") != PURPOSE:
        errors.append(f"{manifest_path}: purpose must be {PURPOSE}")
    if manifest.get("selection_rule_version") != SELECTION_RULE_VERSION:
        errors.append(
            f"{manifest_path}: selection_rule_version must be "
            f"{SELECTION_RULE_VERSION}"
        )
    observed_at = manifest.get("observed_at_utc")
    joined_date = manifest.get("joined_date")
    try:
        parsed_observed_at = (
            datetime.fromisoformat(observed_at)
            if isinstance(observed_at, str)
            else None
        )
    except ValueError:
        parsed_observed_at = None
    if parsed_observed_at is None or parsed_observed_at.tzinfo is None:
        errors.append(f"{manifest_path}: observed_at_utc must be an aware timestamp")
    elif (
        not isinstance(joined_date, str)
        or not DATE_RE.fullmatch(joined_date)
        or joined_date != parsed_observed_at.astimezone(UTC).date().isoformat()
    ):
        errors.append(
            f"{manifest_path}: joined_date must equal observed_at_utc UTC date"
        )
    result = manifest.get("result")
    rules = manifest.get("rules")
    if not isinstance(result, dict) or not isinstance(rules, dict):
        errors.append(f"{manifest_path}: result and rules must be objects")
        return errors
    _validate_closed_shape(
        errors,
        manifest_path=manifest_path,
        label="result",
        value=result,
        expected=_RESULT_FIELDS,
    )
    if rules != EXPECTED_RULES:
        errors.append(
            f"{manifest_path}: rules must exactly match {SELECTION_RULE_VERSION}"
        )
    if manifest.get("evidence_limitations") != EVIDENCE_LIMITATIONS:
        errors.append(
            f"{manifest_path}: evidence_limitations drifted from the reviewed set"
        )
    screen = manifest.get("screen")
    receipt = manifest.get("fetch_receipt")
    for label, evidence in (("screen", screen), ("fetch_receipt", receipt)):
        if not isinstance(evidence, dict):
            errors.append(f"{manifest_path}: {label} must be an evidence object")
            continue
        _validate_closed_shape(
            errors,
            manifest_path=manifest_path,
            label=label,
            value=evidence,
            expected=(
                _SCREEN_EVIDENCE_FIELDS
                if label == "screen"
                else _FETCH_RECEIPT_FIELDS
            ),
        )
        if not SHA256_RE.fullmatch(str(evidence.get("sha256", ""))):
            errors.append(f"{manifest_path}: {label}.sha256 is invalid")
        size = evidence.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            errors.append(f"{manifest_path}: {label}.bytes must be positive")
        if not _safe_evidence_relpath(evidence.get("evidence_relpath")):
            errors.append(
                f"{manifest_path}: {label}.evidence_relpath must be a safe "
                "watchlist-relative archive path"
            )
        logical_name = evidence.get("logical_name")
        evidence_relpath = evidence.get("evidence_relpath")
        if (
            not isinstance(logical_name, str)
            or not logical_name
            or Path(logical_name).name != logical_name
            or not isinstance(evidence_relpath, str)
            or Path(evidence_relpath).name != logical_name
        ):
            errors.append(
                f"{manifest_path}: {label}.logical_name must be a basename "
                "matching evidence_relpath"
            )
        if (
            isinstance(joined_date, str)
            and isinstance(evidence_relpath, str)
            and joined_date not in Path(evidence_relpath).parts
        ):
            errors.append(
                f"{manifest_path}: {label}.evidence_relpath must bind joined_date"
            )
    if isinstance(screen, dict):
        if screen.get("schema") != SCREEN_SCHEMA:
            errors.append(f"{manifest_path}: screen.schema must be {SCREEN_SCHEMA}")
        row_count = screen.get("row_count")
        required_rows = rules.get("selection_count_min")
        if (
            isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or isinstance(required_rows, bool)
            or not isinstance(required_rows, int)
            or row_count < required_rows
        ):
            errors.append(
                f"{manifest_path}: screen.row_count must cover selection_count_min"
            )
    _validate_manifest_sources(
        errors,
        manifest_path=manifest_path,
        sources=manifest.get("sources"),
        screen=screen,
        receipt=receipt,
        joined_date=joined_date,
    )
    if result.get("watchlist_csv_sha256") != snapshot.sha256:
        errors.append(
            f"{manifest_path}: watchlist_csv_sha256 does not match "
            f"{snapshot.requested_path}"
        )
    rows = [
        row
        for row in snapshot.rows
        if (row.get("security_code") or "").strip()
    ]
    _validate_manifest_result(
        errors,
        manifest_path=manifest_path,
        watchlist_path=snapshot.requested_path,
        manifest=manifest,
        rules=rules,
        result=result,
        rows=rows,
    )
    return errors


def _validate_manifest_result(
    errors: list[str],
    *,
    manifest_path: Path,
    watchlist_path: Path,
    manifest: dict[str, Any],
    rules: dict[str, Any],
    result: dict[str, Any],
    rows: list[dict[str, str | None]],
) -> None:
    selection_count_min = rules.get("selection_count_min")
    selection_count_max = rules.get("selection_count_max")
    selected_count = result.get("selected_count")
    if (
        isinstance(selection_count_min, bool)
        or not isinstance(selection_count_min, int)
        or isinstance(selection_count_max, bool)
        or not isinstance(selection_count_max, int)
        or isinstance(selected_count, bool)
        or not isinstance(selected_count, int)
        or not selection_count_min <= selected_count <= selection_count_max
        or selected_count != len(rows)
    ):
        errors.append(
            f"{manifest_path}: selection band/result/CSV counts differ: "
            f"{selection_count_min!r}..{selection_count_max!r}/"
            f"{selected_count!r}/{len(rows)}"
        )
    exchange_counts = result.get("selected_exchange_counts")
    configured_markets = rules.get("markets")
    markets = (
        configured_markets
        if isinstance(configured_markets, list)
        and all(isinstance(item, str) for item in configured_markets)
        else []
    )
    actual_exchange_counts = {
        exchange: sum(
            (row.get("exchange") or "").strip() == exchange for row in rows
        )
        for exchange in markets
    }
    actual_exchange_counts = {
        key: value for key, value in actual_exchange_counts.items() if value
    }
    if (
        exchange_counts != actual_exchange_counts
        or not isinstance(selected_count, int)
        or sum(actual_exchange_counts.values()) != selected_count
    ):
        errors.append(
            f"{manifest_path}: selected_exchange_counts does not match "
            f"{watchlist_path}"
        )
    board_counts = result.get("selected_board_counts")
    configured_boards = rules.get("boards")
    boards = (
        configured_boards
        if isinstance(configured_boards, list)
        and all(isinstance(item, str) for item in configured_boards)
        else []
    )
    if (
        not isinstance(board_counts, dict)
        or not set(board_counts).issubset(boards)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in board_counts.values()
        )
        or not isinstance(selected_count, int)
        or sum(board_counts.values()) != selected_count
    ):
        errors.append(f"{manifest_path}: selected_board_counts are invalid")
    floor = result.get("selected_min_market_cap_cny")
    if (
        isinstance(floor, bool)
        or not isinstance(floor, (int, float))
        or not math.isfinite(float(floor))
        or floor < 2_000_000_000
    ):
        errors.append(
            f"{manifest_path}: selected_min_market_cap_cny is invalid"
        )
    tier_counts = result.get("selected_evidence_tier_counts")
    if (
        not isinstance(tier_counts, dict)
        or not set(tier_counts).issubset({"A", "B", "C"})
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in tier_counts.values()
        )
        or not isinstance(selected_count, int)
        or sum(tier_counts.values()) != selected_count
    ):
        errors.append(f"{manifest_path}: selected_evidence_tier_counts are invalid")
    eligible = result.get("eligible_after_hard_gates")
    exclusion_counts = result.get("exclusion_reason_counts")
    screen = manifest.get("screen")
    screen_row_count = screen.get("row_count") if isinstance(screen, dict) else None
    if (
        isinstance(eligible, bool)
        or not isinstance(eligible, int)
        or not isinstance(selected_count, int)
        or eligible < selected_count
        or not isinstance(exclusion_counts, dict)
        or set(exclusion_counts) != _EXCLUSION_REASON_FIELDS
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or not isinstance(screen_row_count, int)
            or value > screen_row_count
            for value in exclusion_counts.values()
        )
        or not isinstance(screen_row_count, int)
        or eligible > screen_row_count
        or exclusion_counts.get("no_future_research_signal")
        != eligible - selected_count
    ):
        errors.append(f"{manifest_path}: eligibility/exclusion counts are invalid")
    observation_counts = result.get("observation_counts")
    if not isinstance(observation_counts, dict) or set(
        observation_counts
    ) != _OBSERVATION_FIELDS:
        errors.append(f"{manifest_path}: observation_counts shape is invalid")
        observation_counts = {}
    all_latest_two = observation_counts.get("latest_two_parent_losses_all_rows")
    selected_latest_two = observation_counts.get(
        "latest_two_parent_losses_selected"
    )
    all_latest_nonpositive = observation_counts.get(
        "latest_parent_profit_nonpositive_all_rows"
    )
    selected_latest_nonpositive = observation_counts.get(
        "latest_parent_profit_nonpositive_selected"
    )
    all_future_signal = observation_counts.get("future_research_signal_all_rows")
    selected_future_signal = observation_counts.get(
        "future_research_signal_selected"
    )
    all_ocf_missing = observation_counts.get("ocf_per_share_missing_all_rows")
    selected_ocf_missing = observation_counts.get(
        "ocf_per_share_missing_selected"
    )
    all_ocf_nonpositive = observation_counts.get(
        "ocf_per_share_nonpositive_all_rows"
    )
    selected_ocf_nonpositive = observation_counts.get(
        "ocf_per_share_nonpositive_selected"
    )
    observation_values = (
        all_latest_two,
        selected_latest_two,
        all_latest_nonpositive,
        selected_latest_nonpositive,
        all_future_signal,
        selected_future_signal,
        all_ocf_missing,
        selected_ocf_missing,
        all_ocf_nonpositive,
        selected_ocf_nonpositive,
    )
    observation_shape_valid = (
        all(not isinstance(value, bool) and isinstance(value, int) for value in observation_values)
        and not isinstance(screen_row_count, bool)
        and isinstance(screen_row_count, int)
        and not isinstance(selected_count, bool)
        and isinstance(selected_count, int)
    )
    if not observation_shape_valid:
        errors.append(f"{manifest_path}: quality/growth observations are invalid")
    else:
        assert isinstance(all_latest_two, int)
        assert isinstance(selected_latest_two, int)
        assert isinstance(all_latest_nonpositive, int)
        assert isinstance(selected_latest_nonpositive, int)
        assert isinstance(all_future_signal, int)
        assert isinstance(selected_future_signal, int)
        assert isinstance(all_ocf_missing, int)
        assert isinstance(selected_ocf_missing, int)
        assert isinstance(all_ocf_nonpositive, int)
        assert isinstance(selected_ocf_nonpositive, int)
        assert isinstance(screen_row_count, int)
        assert isinstance(selected_count, int)
        if (
            not 0 <= selected_latest_two <= all_latest_two <= screen_row_count
            or not 0
            <= selected_latest_nonpositive
            <= all_latest_nonpositive
            <= screen_row_count
            or not 0
            <= selected_future_signal
            <= all_future_signal
            <= screen_row_count
            or selected_latest_two != 0
            or selected_latest_nonpositive != 0
            or selected_future_signal != selected_count
            or not 0 <= selected_ocf_missing <= all_ocf_missing <= screen_row_count
            or not 0
            <= selected_ocf_nonpositive
            <= all_ocf_nonpositive
            <= screen_row_count
        ):
            errors.append(
                f"{manifest_path}: quality/growth observations are invalid"
            )
    identities = [
        {
            "security_code": (row.get("security_code") or "").strip(),
            "exchange": (row.get("exchange") or "").strip(),
        }
        for row in rows
    ]
    identity_bytes = json.dumps(
        identities,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    actual_identity_sha = hashlib.sha256(identity_bytes).hexdigest()
    if result.get("selected_identity_sha256") != actual_identity_sha:
        errors.append(
            f"{manifest_path}: selected_identity_sha256 does not match "
            f"{watchlist_path}"
        )
    joined_date = manifest.get("joined_date")
    if not isinstance(joined_date, str) or any(
        (row.get("joined_date") or "").strip() != joined_date for row in rows
    ):
        errors.append(
            f"{manifest_path}: every default watchlist row must use joined_date "
            f"{joined_date!r}"
        )


def _safe_evidence_relpath(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/"):
        return False
    parts = Path(value).parts
    return ".." not in parts and parts[0] == "watchlist"


def _validate_closed_shape(
    errors: list[str],
    *,
    manifest_path: Path,
    label: str,
    value: dict[str, Any],
    expected: frozenset[str],
) -> None:
    actual = set(value)
    if actual != expected:
        errors.append(
            f"{manifest_path}: {label} fields are not closed; "
            f"missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}"
        )


def _validate_manifest_sources(
    errors: list[str],
    *,
    manifest_path: Path,
    sources: object,
    screen: object,
    receipt: object,
    joined_date: object,
) -> None:
    if not isinstance(sources, dict):
        errors.append(f"{manifest_path}: sources must be an object")
        return
    _validate_closed_shape(
        errors,
        manifest_path=manifest_path,
        label="sources",
        value=sources,
        expected=_SOURCE_FIELDS,
    )
    source_objects = {
        "identity": (_IDENTITY_SOURCE_FIELDS, "CNINFO p_stock2101"),
        "quotes": (_QUOTE_SOURCE_FIELDS, "Sina Market Center hs_a"),
        "annual": (_ANNUAL_SOURCE_FIELDS, "Eastmoney data center RPT_LICO_FN_CPD"),
    }
    for label, (fields, provider) in source_objects.items():
        source = sources.get(label)
        if not isinstance(source, dict):
            errors.append(f"{manifest_path}: sources.{label} must be an object")
            continue
        _validate_closed_shape(
            errors,
            manifest_path=manifest_path,
            label=f"sources.{label}",
            value=source,
            expected=fields,
        )
        if source.get("provider") != provider:
            errors.append(f"{manifest_path}: sources.{label}.provider drifted")
        if not _safe_evidence_relpath(source.get("evidence_relpath")):
            errors.append(
                f"{manifest_path}: sources.{label}.evidence_relpath is invalid"
            )
        elif (
            isinstance(joined_date, str)
            and joined_date not in Path(str(source["evidence_relpath"])).parts
        ):
            errors.append(
                f"{manifest_path}: sources.{label}.evidence_relpath must bind "
                "joined_date"
            )

    if not isinstance(receipt, dict):
        return
    receipt_sha = receipt.get("sha256")
    receipt_relpath = receipt.get("evidence_relpath")
    if not isinstance(receipt_relpath, str) or not _safe_evidence_relpath(
        receipt_relpath
    ):
        return
    source_root = str(Path(receipt_relpath).parent)
    expected_relpaths = {
        "identity": f"{source_root}/cninfo/p-stock2101.json",
        "quotes": f"{source_root}/sina",
        "annual": f"{source_root}/eastmoney",
    }
    screen_rows = screen.get("row_count") if isinstance(screen, dict) else None
    for label, expected_relpath in expected_relpaths.items():
        source = sources.get(label)
        if not isinstance(source, dict):
            continue
        if source.get("source_bundle_receipt_sha256") != receipt_sha:
            errors.append(
                f"{manifest_path}: sources.{label} receipt hash does not bind "
                "fetch_receipt"
            )
        if source.get("evidence_relpath") != expected_relpath:
            errors.append(
                f"{manifest_path}: sources.{label}.evidence_relpath does not bind "
                "the fetch receipt root"
            )

    identity = sources.get("identity")
    if isinstance(identity, dict):
        rows = identity.get("rows")
        if (
            isinstance(rows, bool)
            or not isinstance(rows, int)
            or rows < 1
            or not isinstance(screen_rows, int)
            or rows < screen_rows
            or not SHA256_RE.fullmatch(str(identity.get("sha256", "")))
        ):
            errors.append(f"{manifest_path}: sources.identity evidence is invalid")
    quotes = sources.get("quotes")
    if isinstance(quotes, dict):
        rows = quotes.get("rows")
        if (
            isinstance(rows, bool)
            or not isinstance(rows, int)
            or rows != screen_rows
            or quotes.get("unit_note") != "mktcap/nmc multiplied by 10,000 to CNY"
        ):
            errors.append(f"{manifest_path}: sources.quotes evidence is invalid")
    annual = sources.get("annual")
    if isinstance(annual, dict):
        raw_rows = annual.get("raw_rows")
        if (
            isinstance(raw_rows, bool)
            or not isinstance(raw_rows, int)
            or raw_rows < 1
            or annual.get("years") != EXPECTED_RULES["annual_years"]
        ):
            errors.append(f"{manifest_path}: sources.annual evidence is invalid")
