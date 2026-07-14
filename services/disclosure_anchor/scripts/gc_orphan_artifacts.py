"""Orphan parser-artifact inventory and (opt-in) cleanup.

Batch 4 (2026-07-14): doctor reports orphan parser artifacts as a WARN but
offers no disposal path — 8k+ files accumulated with no owner. This script
reuses doctor's exact orphan definition (files under parser_artifacts/ whose
relpath is not covered by any processing_run.parser_artifact_relpath prefix).

Default is DRY-RUN: print counts, total bytes, and the top offenders.
``--apply`` deletes the orphans and writes the full deletion manifest to the
audit directory first — evidence before removal, same discipline as the raw
archive. Raw documents are NEVER touched here (immutable archive).

Usage (env comes from worker.env, same as other DB scripts):
    PYTHONPATH=src .venv/bin/python scripts/gc_orphan_artifacts.py [--apply]
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.connection import create_db_engine
from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA
from disclosure_anchor.settings import load_settings


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
    artifact_root = data_root / "parser_artifacts"
    if not artifact_root.exists():
        print("[ok] no parser_artifacts directory; nothing to do")
        return 0

    # Scan the filesystem FIRST, snapshot DB references AFTER, and skip
    # young files: a parse run started during the scan writes files whose
    # run row may not carry parser_artifact_relpath until finish — without
    # the age guard --apply could delete a live run's artifacts (round23
    # review S3, TOCTOU).
    min_age_seconds = 24 * 3600
    now_ts = __import__("time").time()

    candidates: list[Path] = []
    skipped_young = 0
    for path in artifact_root.rglob("*"):
        if not path.is_file():
            continue
        if now_ts - path.stat().st_mtime < min_age_seconds:
            skipped_young += 1
            continue
        candidates.append(path)

    engine = create_db_engine(settings.database_url.get_secret_value())
    with engine.connect() as conn:
        expected = {
            str(row[0]).rstrip("/")
            for row in conn.execute(
                text(
                    f"SELECT parser_artifact_relpath FROM {CORE_SCHEMA}.processing_run "
                    "WHERE parser_artifact_relpath IS NOT NULL"
                )
            )
        }
    engine.dispose()

    orphans: list[Path] = []
    total_bytes = 0
    group_bytes: dict[str, int] = defaultdict(int)
    group_count: dict[str, int] = defaultdict(int)
    for path in candidates:
        relpath = str(path.relative_to(data_root))
        if any(
            relpath == exp or relpath.startswith(exp + "/") for exp in expected
        ):
            continue
        size = path.stat().st_size
        orphans.append(path)
        total_bytes += size
        # Group by the artifact run directory (parser_artifacts/<...>/<run>/).
        parts = Path(relpath).parts
        group = str(Path(*parts[: min(len(parts), 5)]))
        group_bytes[group] += size
        group_count[group] += 1

    print(
        f"[{'apply' if args.apply else 'dry-run'}] orphan parser artifacts: "
        f"{len(orphans)} files, {total_bytes / 1024 / 1024:.1f} MiB, "
        f"{len(group_bytes)} groups "
        f"(skipped {skipped_young} files younger than 24h)"
    )
    for group, size in sorted(group_bytes.items(), key=lambda kv: -kv[1])[: args.top]:
        print(f"  {size / 1024 / 1024:8.1f} MiB  {group_count[group]:6d} files  {group}")

    if not args.apply:
        print("[note] dry-run only; re-run with --apply to delete "
              "(a deletion manifest lands in the audit dir first)")
        return 0

    audit_dir = settings.disclosure_data_root / "audit" / "gc"
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = audit_dir / f"gc_orphan_artifacts_{stamp}.json"
    manifest_path.write_text(
        json.dumps(
            {
                "deleted_at": stamp,
                "total_files": len(orphans),
                "total_bytes": total_bytes,
                "files": [str(p.relative_to(data_root)) for p in orphans],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    deleted = 0
    for path in orphans:
        path.unlink(missing_ok=True)
        deleted += 1
    # Sweep now-empty directories bottom-up.
    for directory in sorted(
        (p for p in artifact_root.rglob("*") if p.is_dir()),
        key=lambda p: -len(p.parts),
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    print(f"[ok] deleted {deleted} files; manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
