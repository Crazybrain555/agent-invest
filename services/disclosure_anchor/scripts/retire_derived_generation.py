"""Manifest-driven two-phase retirement of superseded derived generations.

Replaces blanket ``prune_history.sh`` for the corpus-reparse cleanup: retire
exactly the runs enumerated in a reviewed manifest, artifacts first, DB rows
second, with raw PDFs and source lineage untouched by construction (only the
three derived relpath families are ever deleted).

Phases:
  (no flags)          build + print the retirement manifest (dry-run; writes
                      the manifest JSON under <data_root>/audit/gc/)
  --apply-artifacts   delete the manifest runs' parser artifact trees,
                      normalized IR files, and unit snapshot files
  --apply-metadata    delete the manifest runs' outbox events, document
                      units, and processing_run rows in ONE transaction
                      (same predicates as prune_history.sh, scoped to the
                      manifest run ids)

Guards:
  * only runs with NOT is_active AND status <> 'running' AND created_at <
    --before enter the manifest; runs referenced by any
    document.current_processing_run_id are excluded;
  * both apply phases re-verify each guard against the live DB and abort on
    any drift;
  * artifact deletion refuses relpaths shared with any run outside the
    manifest;
  * U5 historical replay for retired runs is intentionally given up
    (HANDOFF corpus-reparse-audit-r1 authorization, user 2026-07-16).

Usage:
  .venv/bin/python scripts/retire_derived_generation.py --before <ISO8601>
      [--manifest <path>] [--apply-artifacts | --apply-metadata]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.engine import Connection

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from disclosure_anchor.adapters.db.postgres.connection import create_db_engine
from disclosure_anchor.cli.worker import _database_url
from disclosure_anchor.settings import Settings, load_settings

_MANIFEST_SCHEMA = "retire-derived-generation-manifest.v1"

_SELECT_RETIREMENT = text(
    """
    SELECT pr.processing_run_id, pr.document_id, pr.status, pr.is_active,
           pr.created_at, pr.parser_artifact_relpath, pr.normalized_ir_relpath,
           pr.document_units_relpath,
           (SELECT count(*) FROM disclosure_core.document_unit du
             WHERE du.processing_run_id = pr.processing_run_id) AS unit_count
      FROM disclosure_core.processing_run pr
     WHERE NOT pr.is_active
       AND pr.status <> 'running'
       AND pr.created_at < :before
       AND pr.processing_run_id NOT IN (
           SELECT current_processing_run_id FROM disclosure_core.document
            WHERE current_processing_run_id IS NOT NULL)
     ORDER BY pr.processing_run_id
    """
)

_VERIFY_ONE = text(
    """
    SELECT NOT pr.is_active
           AND pr.status <> 'running'
           AND pr.processing_run_id NOT IN (
               SELECT current_processing_run_id FROM disclosure_core.document
                WHERE current_processing_run_id IS NOT NULL) AS retirable
      FROM disclosure_core.processing_run pr
     WHERE pr.processing_run_id = :run_id
    """
)


def _build_manifest(engine: sqlalchemy.Engine, before: str) -> dict:
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(_SELECT_RETIREMENT, {"before": before}).mappings()]
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
    return {
        "manifest_schema": _MANIFEST_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "before": before,
        "run_count": len(rows),
        "unit_count": sum(int(row["unit_count"]) for row in rows),
        "runs": rows,
    }


def _shared_relpath_owners(
    conn: Connection, column: str, relpath: str, manifest_ids: set[str]
) -> list[str]:
    rows = conn.execute(
        text(
            f"SELECT processing_run_id FROM disclosure_core.processing_run"
            f" WHERE {column} = :relpath"
        ),
        {"relpath": relpath},
    ).scalars()
    return [run_id for run_id in rows if run_id not in manifest_ids]


def _apply_artifacts(
    engine: sqlalchemy.Engine, settings: Settings, manifest: dict, manifest_path: Path
) -> int:
    data_root = Path(settings.disclosure_data_root) / "data"
    manifest_ids = {run["processing_run_id"] for run in manifest["runs"]}
    deleted_log = manifest_path.with_suffix(".artifacts-deleted.jsonl")
    failures = 0
    with engine.connect() as conn, deleted_log.open("a", encoding="utf-8") as log:
        for run in manifest["runs"]:
            run_id = run["processing_run_id"]
            retirable = conn.execute(_VERIFY_ONE, {"run_id": run_id}).scalar()
            if retirable is not True:
                print(f"[abort-run] {run_id}: guard drifted (retirable={retirable})")
                failures += 1
                continue
            for column, kind in (
                ("parser_artifact_relpath", "tree"),
                ("normalized_ir_relpath", "file"),
                ("document_units_relpath", "file"),
            ):
                relpath = run.get(column)
                if not relpath:
                    continue
                owners = _shared_relpath_owners(conn, column, relpath, manifest_ids)
                if owners:
                    print(f"[skip-shared] {run_id} {column}={relpath} also owned by {owners}")
                    failures += 1
                    continue
                if Path(relpath).is_absolute() or ".." in Path(relpath).parts:
                    print(f"[skip-unsafe] {run_id} {column}={relpath}")
                    failures += 1
                    continue
                target = data_root / relpath
                if not target.exists():
                    log.write(json.dumps({"run_id": run_id, "relpath": relpath, "result": "absent"}) + "\n")
                    continue
                if kind == "tree" and target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                log.write(json.dumps({"run_id": run_id, "relpath": relpath, "result": "deleted"}) + "\n")
    print(f"[artifacts] processed {len(manifest['runs'])} runs, failures={failures}, log={deleted_log}")
    return 1 if failures else 0


def _apply_metadata(engine: sqlalchemy.Engine, manifest: dict) -> int:
    run_ids = [run["processing_run_id"] for run in manifest["runs"]]
    if not run_ids:
        print("[metadata] manifest empty — nothing to delete")
        return 0
    with engine.begin() as conn:
        retirable = conn.execute(
            text(
                """
                SELECT count(*) FROM disclosure_core.processing_run pr
                 WHERE pr.processing_run_id = ANY(:run_ids)
                   AND NOT pr.is_active AND pr.status <> 'running'
                   AND pr.processing_run_id NOT IN (
                       SELECT current_processing_run_id FROM disclosure_core.document
                        WHERE current_processing_run_id IS NOT NULL)
                """
            ),
            {"run_ids": run_ids},
        ).scalar_one()
        present = conn.execute(
            text(
                "SELECT count(*) FROM disclosure_core.processing_run"
                " WHERE processing_run_id = ANY(:run_ids)"
            ),
            {"run_ids": run_ids},
        ).scalar_one()
        if retirable != present:
            raise SystemExit(
                f"[abort] {present - retirable} manifest runs no longer satisfy the"
                " retirement guards — regenerate the manifest"
            )
        events = conn.execute(
            text(
                """
                DELETE FROM disclosure_ops.outbox_event
                 WHERE (subject_kind = 'processing_run' AND subject_ref = ANY(:run_ids))
                    OR (subject_kind = 'document_unit' AND subject_ref IN (
                        SELECT asset_id FROM disclosure_core.document_unit
                         WHERE processing_run_id = ANY(:run_ids)))
                    OR (processing_run_id IS NOT NULL
                        AND processing_run_id = ANY(:run_ids))
                """
            ),
            {"run_ids": run_ids},
        ).rowcount
        units = conn.execute(
            text(
                "DELETE FROM disclosure_core.document_unit"
                " WHERE processing_run_id = ANY(:run_ids)"
            ),
            {"run_ids": run_ids},
        ).rowcount
        runs = conn.execute(
            text(
                "DELETE FROM disclosure_core.processing_run"
                " WHERE processing_run_id = ANY(:run_ids)"
            ),
            {"run_ids": run_ids},
        ).rowcount
    print(f"[metadata] deleted runs={runs} units={units} outbox_events={events}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="retire_derived_generation")
    parser.add_argument("--before", required=True, help="ISO8601 cutoff: only runs created before this retire")
    parser.add_argument("--manifest", type=Path, help="existing manifest to apply / output path override")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply-artifacts", action="store_true")
    group.add_argument("--apply-metadata", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings()
    engine = create_db_engine(_database_url(settings))
    try:
        if args.apply_artifacts or args.apply_metadata:
            if not args.manifest or not args.manifest.is_file():
                raise SystemExit("[abort] apply phases require --manifest pointing at a reviewed manifest file")
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            if manifest.get("manifest_schema") != _MANIFEST_SCHEMA:
                raise SystemExit("[abort] manifest schema mismatch")
            if manifest.get("before") != args.before:
                raise SystemExit("[abort] --before does not match the manifest cutoff")
            if args.apply_artifacts:
                return _apply_artifacts(engine, settings, manifest, args.manifest)
            return _apply_metadata(engine, manifest)

        manifest = _build_manifest(engine, args.before)
        out_dir = Path(settings.disclosure_data_root) / "audit" / "gc"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = args.manifest or (
            out_dir
            / f"retire_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        out_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"[manifest] runs={manifest['run_count']} units={manifest['unit_count']}"
            f" before={args.before} -> {out_path}"
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
