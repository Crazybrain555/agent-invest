"""Offline config validation (Home Assistant check_config pattern).

Validates operator-owned config before anything applies it, with no DB access:

* watchlist CSV columns, canonical identities, values, and duplicates;
* the research-universe sidecar's exact CSV byte/hash and rule binding;
* processing-policy coverage and disjoint assignments.

``make config-check`` runs this; ``make track`` also validates the exact
in-memory CSV snapshot that it applies.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from disclosure_anchor.adapters.sources.cninfo.mapper import load_class_map
from disclosure_anchor.adapters.watchlist_config import (
    DEFAULT_PROCESSING_POLICY,
    DEFAULT_SCREEN_MANIFEST,
    DEFAULT_WATCHLIST,
    WatchlistSnapshot,
    load_watchlist_snapshot,
    screen_manifest_for_snapshot,
    validate_screen_manifest,
    validate_watchlist_snapshot,
)


WATCHLIST = DEFAULT_WATCHLIST
SCREEN_MANIFEST = DEFAULT_SCREEN_MANIFEST
POLICY = DEFAULT_PROCESSING_POLICY


def check_watchlist(
    errors: list[str],
    known_classes: frozenset[str],
    *,
    watchlist: Path | None = None,
    snapshot: WatchlistSnapshot | None = None,
) -> None:
    path = watchlist or WATCHLIST
    try:
        resolved_snapshot = snapshot or load_watchlist_snapshot(path)
    except ValueError as exc:
        errors.append(str(exc))
        return
    errors.extend(validate_watchlist_snapshot(resolved_snapshot, known_classes))


def check_screen_manifest(
    errors: list[str],
    *,
    watchlist: Path,
    manifest_path: Path,
    snapshot: WatchlistSnapshot | None = None,
) -> None:
    try:
        resolved_snapshot = snapshot or load_watchlist_snapshot(watchlist)
    except ValueError as exc:
        errors.append(str(exc))
        return
    errors.extend(validate_screen_manifest(resolved_snapshot, manifest_path))


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="config_check", description=__doc__)
    parser.add_argument("--watchlist", type=Path)
    parser.add_argument("--screen-manifest", type=Path)
    args = parser.parse_args(argv)
    watchlist = args.watchlist or WATCHLIST
    known_classes = frozenset(load_class_map()["classes"])
    errors: list[str] = []
    try:
        snapshot = load_watchlist_snapshot(watchlist)
    except ValueError as exc:
        snapshot = None
        errors.append(str(exc))

    screen_manifest = args.screen_manifest
    if snapshot is not None:
        screen_manifest = screen_manifest_for_snapshot(
            snapshot,
            explicit_manifest=screen_manifest,
            default_watchlist=WATCHLIST,
            default_manifest=SCREEN_MANIFEST,
        )
        check_watchlist(
            errors,
            known_classes,
            watchlist=watchlist,
            snapshot=snapshot,
        )
        if screen_manifest is not None:
            check_screen_manifest(
                errors,
                watchlist=watchlist,
                manifest_path=screen_manifest,
                snapshot=snapshot,
            )
    check_policy(errors, known_classes)
    for line in errors:
        print(f"ERROR {line}", file=sys.stderr)
    if errors:
        print(f"config-check: {len(errors)} error(s)", file=sys.stderr)
        return 1
    checked = "watchlist + processing_policy"
    if screen_manifest is not None:
        checked = "watchlist + research-universe manifest + processing_policy"
    print(f"config-check: OK ({checked})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
