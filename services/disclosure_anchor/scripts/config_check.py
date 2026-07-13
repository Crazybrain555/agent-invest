"""Offline config validation (Home Assistant check_config pattern).

Validates the two operator-owned config files BEFORE anything applies them —
file/line-anchored errors, non-zero exit on any error, no DB access:

  config/watchlist.csv          columns, code/exchange/status/date shapes,
                                lookback int, sync_frequency vocabulary,
                                process_classes ⊆ class_map
  config/processing_policy.json process ∪ register_only == class_map classes,
                                disjoint, no unknown names

`make config-check` runs this; `make track` runs it first (validate → apply).
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

from disclosure_anchor.adapters.sources.cninfo.mapper import load_class_map
from disclosure_anchor.application.use_cases.track_companies import SYNC_FREQUENCIES
from disclosure_anchor.domain.value_objects import canonical_security_identity

WATCHLIST = Path("config/watchlist.csv")
POLICY = Path("config/processing_policy.json")
EXCHANGES = {"BSE", "SSE", "SZSE"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CODE_RE = re.compile(r"^\d{6}$")


def check_watchlist(errors: list[str], known_classes: frozenset[str]) -> None:
    if not WATCHLIST.exists():
        errors.append(f"{WATCHLIST}: missing")
        return
    with WATCHLIST.open(encoding="utf-8") as fh:
        lines = [line for line in fh]
    reader = csv.DictReader(
        (line for line in lines if not line.startswith("#")),
    )
    required = {"security_code", "exchange", "status", "joined_date"}
    if reader.fieldnames is None or not required <= set(reader.fieldnames):
        errors.append(
            f"{WATCHLIST}: header must contain {sorted(required)}; "
            f"got {reader.fieldnames}"
        )
        return
    if "process_classes" not in reader.fieldnames:
        errors.append(
            f"{WATCHLIST}: header missing process_classes (renamed from "
            "filing_categories in 0018)"
        )
    seen: dict[str, int] = {}
    for lineno, row in enumerate(reader, start=1):
        where = f"{WATCHLIST}:{lineno}"
        code = (row.get("security_code") or "").strip()
        if not code:
            continue
        if not CODE_RE.match(code):
            errors.append(f"{where}: security_code {code!r} is not 6 digits")
        if code in seen:
            errors.append(f"{where}: duplicate security_code {code} (first at row {seen[code]})")
        seen[code] = lineno
        exchange = (row.get("exchange") or "").strip()
        if exchange and exchange not in EXCHANGES:
            errors.append(f"{where}: exchange {exchange!r} not in {sorted(EXCHANGES)}")
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
        if lookback and (not lookback.isdigit()):
            errors.append(f"{where}: lookback_days {lookback!r} is not a non-negative int")
        frequency = (row.get("sync_frequency") or "").strip()
        if frequency and frequency not in SYNC_FREQUENCIES:
            errors.append(f"{where}: sync_frequency {frequency!r} not in {SYNC_FREQUENCIES}")
        classes_raw = (row.get("process_classes") or "").strip()
        for item in (seg.strip() for seg in classes_raw.split(";") if seg.strip()):
            if item not in known_classes:
                errors.append(
                    f"{where}: unknown process_classes value {item!r} (see class_map.json)"
                )


def check_policy(errors: list[str], known_classes: frozenset[str]) -> None:
    if not POLICY.exists():
        errors.append(f"{POLICY}: missing")
        return
    try:
        payload = json.loads(POLICY.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{POLICY}:{exc.lineno}: invalid JSON — {exc.msg}")
        return
    process = list(payload.get("process", []))
    register_only = list(payload.get("register_only", []))
    for name in (*process, *register_only):
        if name not in known_classes:
            errors.append(f"{POLICY}: unknown class {name!r}")
    overlap = set(process) & set(register_only)
    if overlap:
        errors.append(f"{POLICY}: classes in BOTH lists: {sorted(overlap)}")
    missing = known_classes - set(process) - set(register_only)
    if missing:
        errors.append(
            f"{POLICY}: classes missing an assignment: {sorted(missing)} "
            "(every class_map class needs process or register_only)"
        )


def main() -> int:
    known_classes = frozenset(load_class_map()["classes"])
    errors: list[str] = []
    check_watchlist(errors, known_classes)
    check_policy(errors, known_classes)
    for line in errors:
        print(f"ERROR {line}", file=sys.stderr)
    if errors:
        print(f"config-check: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print("config-check: OK (watchlist + processing_policy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
