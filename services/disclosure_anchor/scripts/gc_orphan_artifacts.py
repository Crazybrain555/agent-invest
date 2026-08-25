"""Inventory and optionally delete unowned derived artifacts.

The database is the ownership authority.  Parser artifacts use directory
ownership (a processing-run relpath owns every descendant); normalized IR and
document-unit snapshots and their hash-bound semantic-route receipts use
exact-file ownership. This collector removes only files that no active or
historical processing run owns.

Default is DRY-RUN. ``--apply`` holds the derived-state mutation lock from the
ownership snapshot through deletion, rechecks the 24-hour age guard, and writes
a durable deletion manifest before unlinking anything. Raw documents are never
in scope.

Usage (env comes from worker.env, same as other DB scripts):
    PYTHONPATH=src .venv/bin/python scripts/gc_orphan_artifacts.py [--apply]
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import time

from sqlalchemy import text
from sqlalchemy.engine import Connection

from disclosure_anchor.adapters.db.postgres.connection import create_db_engine
from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPTS_V1_FILENAME,
)
from disclosure_anchor.application.worker.locks import (
    exclusive_corpus_mutation,
)
from disclosure_anchor.settings import load_settings

_MIN_AGE_SECONDS = 24 * 3600
_MANIFEST_SCHEMA = "orphan-derived-artifacts.v3"
_FAMILY_ROOTS = {
    "parser_artifacts": Path("parser_artifacts"),
    "normalized_ir": Path("derived/normalized_ir"),
    "provider_documents": Path("derived/provider_documents"),
    "document_unit_snapshots": Path("derived/document_unit_snapshots"),
}
_PREFIX_OWNERSHIP_FAMILIES = frozenset({"parser_artifacts"})


@dataclass(frozen=True)
class _Candidate:
    family: str
    path: Path


@dataclass(frozen=True)
class _Orphan:
    family: str
    path: Path
    relpath: str
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int


def _scan_old_candidates(
    data_root: Path,
    *,
    now_ts: float,
    min_age_seconds: int = _MIN_AGE_SECONDS,
) -> tuple[list[_Candidate], dict[str, int]]:
    candidates: list[_Candidate] = []
    skipped_young = {family: 0 for family in _FAMILY_ROOTS}
    for family, root_relpath in _FAMILY_ROOTS.items():
        root = data_root / root_relpath
        if not root.exists():
            continue
        for path in root.rglob("*"):
            try:
                file_stat = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            if now_ts - file_stat.st_mtime < min_age_seconds:
                skipped_young[family] += 1
                continue
            candidates.append(_Candidate(family=family, path=path))
    candidates.sort(key=lambda candidate: (candidate.family, str(candidate.path)))
    return candidates, skipped_young


def _snapshot_expected_owners(conn: Connection) -> dict[str, set[str]]:
    expected: dict[str, set[str]] = {
        family: set() for family in _FAMILY_ROOTS
    }
    rows = conn.execute(
        text(
            f"""
            SELECT parser_artifact_relpath, normalized_ir_relpath,
                   provider_document_relpath, document_units_relpath,
                   semantic_route_receipts_hash,
                   semantic_route_receipts_relpath,
                   semantic_route_receipts_contract_version
              FROM {CORE_SCHEMA}.processing_run
            """
        )
    )
    for (
        parser_relpath,
        normalized_relpath,
        provider_relpath,
        units_relpath,
        receipt_hash,
        receipt_relpath,
        receipt_version,
    ) in rows:
        values = {
            "parser_artifacts": parser_relpath,
            "normalized_ir": normalized_relpath,
            "provider_documents": provider_relpath,
            "document_unit_snapshots": units_relpath,
        }
        for family, value in values.items():
            if value is not None:
                expected[family].add(str(value).rstrip("/"))
        if units_relpath is not None and receipt_hash is not None:
            if receipt_relpath is not None:
                if receipt_version != "semantic_route_receipt.v2":
                    raise RuntimeError(
                        "processing_run semantic receipt version is unsupported"
                    )
                expected["document_unit_snapshots"].add(str(receipt_relpath))
            else:
                if receipt_version is not None:
                    raise RuntimeError(
                        "processing_run semantic receipt identity is incomplete"
                    )
                snapshot = PurePosixPath(str(units_relpath))
                expected["document_unit_snapshots"].add(
                    (snapshot.parent / SEMANTIC_ROUTE_RECEIPTS_V1_FILENAME).as_posix()
                )
    return expected


def _is_owned(
    *,
    family: str,
    relpath: str,
    expected: dict[str, set[str]],
) -> bool:
    owners = expected[family]
    if family in _PREFIX_OWNERSHIP_FAMILIES:
        path = PurePosixPath(relpath)
        return any(candidate.as_posix() in owners for candidate in (path, *path.parents))
    return relpath in owners


def _collect_orphans(
    candidates: list[_Candidate],
    *,
    data_root: Path,
    expected: dict[str, set[str]],
    now_ts: float,
    min_age_seconds: int = _MIN_AGE_SECONDS,
) -> tuple[list[_Orphan], dict[str, int]]:
    """Revalidate candidates after the ownership snapshot.

    The second age check prevents a path that was replaced or rewritten during
    the initial filesystem walk from being treated as an old orphan.
    """

    orphans: list[_Orphan] = []
    skipped_young = {family: 0 for family in _FAMILY_ROOTS}
    for candidate in candidates:
        try:
            file_stat = candidate.path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(file_stat.st_mode):
            continue
        if now_ts - file_stat.st_mtime < min_age_seconds:
            skipped_young[candidate.family] += 1
            continue
        relpath = candidate.path.relative_to(data_root).as_posix()
        if _is_owned(
            family=candidate.family,
            relpath=relpath,
            expected=expected,
        ):
            continue
        orphans.append(
            _Orphan(
                family=candidate.family,
                path=candidate.path,
                relpath=relpath,
                size_bytes=file_stat.st_size,
                device=file_stat.st_dev,
                inode=file_stat.st_ino,
                mtime_ns=file_stat.st_mtime_ns,
            )
        )
    return orphans, skipped_young


def _build_manifest(orphans: list[_Orphan], *, planned_at: str) -> dict[str, object]:
    family_totals = {
        family: {
            "files": sum(orphan.family == family for orphan in orphans),
            "bytes": sum(
                orphan.size_bytes
                for orphan in orphans
                if orphan.family == family
            ),
        }
        for family in _FAMILY_ROOTS
    }
    return {
        "manifest_schema": _MANIFEST_SCHEMA,
        "planned_at": planned_at,
        "minimum_age_seconds": _MIN_AGE_SECONDS,
        "total_files": len(orphans),
        "total_bytes": sum(orphan.size_bytes for orphan in orphans),
        "families": family_totals,
        "files": [
            {
                "family": orphan.family,
                "relpath": orphan.relpath,
                "size_bytes": orphan.size_bytes,
                "device": orphan.device,
                "inode": orphan.inode,
                "mtime_ns": orphan.mtime_ns,
            }
            for orphan in orphans
        ],
    }


def _write_manifest_before_delete(path: Path, manifest: dict[str, object]) -> None:
    payload = json.dumps(manifest, ensure_ascii=False, indent=1) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _same_file(orphan: _Orphan) -> bool:
    try:
        current = orphan.path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_dev == orphan.device
        and current.st_ino == orphan.inode
        and current.st_size == orphan.size_bytes
        and current.st_mtime_ns == orphan.mtime_ns
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete the orphans (writes an audit manifest first)",
    )
    parser.add_argument(
        "--top", type=int, default=20, help="show the N largest orphan groups"
    )
    args = parser.parse_args()

    settings = load_settings()
    if settings.database_url is None:
        print("[FAIL] DATABASE_URL missing (source worker.env first)", file=sys.stderr)
        return 2
    data_root = settings.disclosure_data_root / "data"
    if not any((data_root / root).exists() for root in _FAMILY_ROOTS.values()):
        print("[ok] no derived artifact directories; nothing to do")
        return 0

    # Scan the filesystem FIRST, snapshot DB references AFTER, and skip
    # young files. A producer can create files before committing the run row,
    # so age is an independent fail-closed guard, not an ownership substitute.
    candidates, initially_skipped_young = _scan_old_candidates(
        data_root,
        now_ts=time.time(),
    )

    engine = create_db_engine(settings.database_url.get_secret_value())
    mutation_gate = (
        exclusive_corpus_mutation(engine) if args.apply else nullcontext()
    )
    try:
        # Apply mode takes the cross-tool lock before its DB reference
        # snapshot and holds it through the last unlink.  A full reset can
        # therefore never expose its just-truncated DB to this orphan scan.
        with mutation_gate:
            with engine.connect() as conn:
                expected = _snapshot_expected_owners(conn)

            orphans, recheck_skipped_young = _collect_orphans(
                candidates,
                data_root=data_root,
                expected=expected,
                now_ts=time.time(),
            )
            total_bytes = sum(orphan.size_bytes for orphan in orphans)
            group_bytes: dict[str, int] = defaultdict(int)
            group_count: dict[str, int] = defaultdict(int)
            for orphan in orphans:
                parts = Path(orphan.relpath).parts
                group = str(Path(*parts[: min(len(parts), 5)]))
                group_bytes[group] += orphan.size_bytes
                group_count[group] += 1
            skipped_young = {
                family: (
                    initially_skipped_young[family]
                    + recheck_skipped_young[family]
                )
                for family in _FAMILY_ROOTS
            }

            print(
                f"[{'apply' if args.apply else 'dry-run'}] orphan derived "
                f"artifacts: {len(orphans)} files, "
                f"{total_bytes / 1024 / 1024:.1f} MiB, "
                f"{len(group_bytes)} groups"
            )
            for family in _FAMILY_ROOTS:
                family_orphans = [
                    orphan for orphan in orphans if orphan.family == family
                ]
                family_bytes = sum(
                    orphan.size_bytes for orphan in family_orphans
                )
                print(
                    f"  {family}: {len(family_orphans)} files, "
                    f"{family_bytes / 1024 / 1024:.1f} MiB; "
                    f"skipped_young={skipped_young[family]}"
                )
            for group, size in sorted(
                group_bytes.items(), key=lambda item: -item[1]
            )[: args.top]:
                print(
                    f"  {size / 1024 / 1024:8.1f} MiB  "
                    f"{group_count[group]:6d} files  {group}"
                )

            if not args.apply:
                print(
                    "[note] dry-run only; re-run with --apply to delete "
                    "(a deletion manifest lands in the audit dir first)"
                )
                return 0

            audit_dir = settings.disclosure_data_root / "audit" / "gc"
            audit_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            manifest_path = audit_dir / f"gc_orphan_artifacts_{stamp}.json"
            _write_manifest_before_delete(
                manifest_path,
                _build_manifest(orphans, planned_at=stamp),
            )
            deleted = 0
            for orphan in orphans:
                if not _same_file(orphan):
                    raise RuntimeError(
                        "artifact changed after the deletion manifest was "
                        f"written: {orphan.relpath}"
                    )
                orphan.path.unlink()
                deleted += 1
            # Sweep now-empty directories bottom-up.
            for root_relpath in _FAMILY_ROOTS.values():
                root = data_root / root_relpath
                if not root.exists():
                    continue
                for directory in sorted(
                    (path for path in root.rglob("*") if path.is_dir()),
                    key=lambda path: -len(path.parts),
                ):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
            print(f"[ok] deleted {deleted} files; manifest: {manifest_path}")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
