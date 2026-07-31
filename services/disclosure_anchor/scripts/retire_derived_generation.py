"""Manifest-driven retirement of superseded derived generations.

Retirement deletes only DB ownership metadata in one guarded transaction.
The independent orphan collector subsequently removes files that have no
remaining owner.  This DB-first design is crash-safe: interruption can leave a
harmless orphan, never a live row pointing at a file already deleted by a
separate phase.

Modes:
  (no flags)  build + print the retirement manifest (dry-run)
  --apply     atomically delete the reviewed manifest's derived DB metadata
  --auto      keep the newest superseded run per document and retire older ones

Guards:
  * only runs with NOT is_active AND status <> 'running' AND created_at <
    --before enter the manifest; runs referenced by any
    document.current_processing_run_id are excluded;
  * apply re-verifies the complete manifest under exclusive corpus admission
    before deleting any row;
  * not-started/running builds are never retirement candidates;
  * U5 historical replay for retired runs is intentionally given up
    (authorized in HANDOFF corpus-reparse-audit-r1).

Usage:
  .venv/bin/python scripts/retire_derived_generation.py --before <ISO8601>
      [--manifest <path>] [--apply]
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from disclosure_anchor.adapters.db.postgres.connection import create_db_engine
from disclosure_anchor.application.worker.locks import (
    exclusive_corpus_mutation,
)
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
       AND pr.unit_build_status IN ('succeeded', 'failed')
       AND pr.created_at < :before
       AND pr.processing_run_id NOT IN (
           SELECT current_processing_run_id FROM disclosure_core.document
            WHERE current_processing_run_id IS NOT NULL)
       AND NOT EXISTS (
           SELECT 1
             FROM disclosure_core.processing_run dependent
            WHERE dependent.artifact_owner_processing_run_id =
                  pr.processing_run_id
              AND dependent.processing_run_id <> pr.processing_run_id)
     ORDER BY pr.processing_run_id
    """
)

# Auto mode: per document keep the newest SUCCEEDED superseded run as
# rollback insurance (a newer failed run is not a rollback target) and retire
# everything else superseded. New/active/current runs are excluded by the
# same predicates as the manual path.
_SELECT_AUTO_RETIREMENT = text(
    """
    SELECT pr.processing_run_id, pr.document_id, pr.status, pr.is_active,
           pr.created_at, pr.parser_artifact_relpath, pr.normalized_ir_relpath,
           pr.document_units_relpath,
           (SELECT count(*) FROM disclosure_core.document_unit du
             WHERE du.processing_run_id = pr.processing_run_id) AS unit_count
      FROM disclosure_core.processing_run pr
     WHERE NOT pr.is_active
       AND pr.status <> 'running'
       AND pr.unit_build_status IN ('succeeded', 'failed')
       AND pr.processing_run_id NOT IN (
           SELECT current_processing_run_id FROM disclosure_core.document
            WHERE current_processing_run_id IS NOT NULL)
       AND NOT EXISTS (
           SELECT 1
             FROM disclosure_core.processing_run dependent
            WHERE dependent.artifact_owner_processing_run_id =
                  pr.processing_run_id
              AND dependent.processing_run_id <> pr.processing_run_id)
       AND pr.processing_run_id NOT IN (
           SELECT DISTINCT ON (pr2.document_id) pr2.processing_run_id
             FROM disclosure_core.processing_run pr2
            WHERE NOT pr2.is_active
              AND pr2.status <> 'running'
              AND pr2.unit_build_status IN ('succeeded', 'failed')
              AND pr2.processing_run_id NOT IN (
                  SELECT current_processing_run_id FROM disclosure_core.document
                   WHERE current_processing_run_id IS NOT NULL)
            ORDER BY pr2.document_id,
                     (pr2.status = 'succeeded') DESC,
                     pr2.created_at DESC,
                     pr2.processing_run_id DESC)
     ORDER BY pr.processing_run_id
    """
)


def _build_manifest(
    engine: sqlalchemy.Engine, before: str, *, auto: bool = False
) -> dict[str, Any]:
    with engine.connect() as conn:
        if auto:
            rows = [
                dict(row) for row in conn.execute(_SELECT_AUTO_RETIREMENT).mappings()
            ]
        else:
            rows = [
                dict(row)
                for row in conn.execute(
                    _SELECT_RETIREMENT, {"before": before}
                ).mappings()
            ]
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
    return {
        "manifest_schema": _MANIFEST_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "before": before,
        "mode": "auto-keep-latest-superseded" if auto else "manual",
        "run_count": len(rows),
        "unit_count": sum(int(row["unit_count"]) for row in rows),
        "runs": rows,
    }


def _write_manifest(
    manifest: dict[str, Any],
    settings: Settings,
    *,
    prefix: str,
    override: Path | None = None,
) -> Path:
    out_dir = Path(settings.disclosure_data_root) / "audit" / "gc"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = override or (out_dir / f"{prefix}_{stamp}.json")
    out_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out_path


def _apply_metadata(
    engine: sqlalchemy.Engine,
    manifest: dict[str, Any],
) -> int:
    before = manifest.get("before")
    if not isinstance(before, str) or not before:
        raise SystemExit("[abort] manifest has no valid before cutoff")
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
                   AND pr.unit_build_status IN ('succeeded', 'failed')
                   AND pr.created_at < :before
                   AND pr.processing_run_id NOT IN (
                       SELECT current_processing_run_id FROM disclosure_core.document
                        WHERE current_processing_run_id IS NOT NULL)
                   AND NOT EXISTS (
                       SELECT 1
                         FROM disclosure_core.processing_run dependent
                        WHERE dependent.artifact_owner_processing_run_id =
                              pr.processing_run_id
                          AND dependent.processing_run_id <>
                              pr.processing_run_id)
                """
            ),
            {"run_ids": run_ids, "before": before},
        ).scalar_one()
        present = conn.execute(
            text(
                "SELECT count(*) FROM disclosure_core.processing_run"
                " WHERE processing_run_id = ANY(:run_ids)"
            ),
            {"run_ids": run_ids},
        ).scalar_one()
        if present != len(run_ids) or retirable != present:
            raise SystemExit(
                "[abort] manifest membership or retirement guards drifted "
                f"(expected={len(run_ids)}, present={present}, "
                f"retirable={retirable}) — regenerate the manifest"
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
    parser.add_argument(
        "--before", help="ISO8601 cutoff: only runs created before this retire"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="existing manifest to apply / output path override",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument(
        "--auto",
        action="store_true",
        help="unattended mode: per document keep the newest superseded run, "
        "build the manifest for everything older and apply both phases",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --auto: build and print the manifest, apply nothing",
    )
    args = parser.parse_args(argv)
    if not args.auto and not args.before:
        parser.error("--before is required unless --auto is given")

    settings = load_settings()
    engine = create_db_engine(_database_url(settings))
    try:
        destructive = bool((args.auto and not args.dry_run) or args.apply)
        mutation_gate = (
            exclusive_corpus_mutation(engine) if destructive else nullcontext()
        )
        with mutation_gate:
            if args.auto:
                now = datetime.now(timezone.utc)
                manifest = _build_manifest(engine, now.isoformat(), auto=True)
                out_path = _write_manifest(manifest, settings, prefix="retire_auto")
                print(
                    f"[auto] superseded-beyond-rollback runs={manifest['run_count']}"
                    f" units={manifest['unit_count']} manifest={out_path}"
                )
                if manifest["run_count"] == 0 or args.dry_run:
                    return 0
                return _apply_metadata(engine, manifest)

            if args.apply:
                if not args.manifest or not args.manifest.is_file():
                    raise SystemExit(
                        "[abort] --apply requires --manifest pointing "
                        "at a reviewed manifest file"
                    )
                manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
                if manifest.get("manifest_schema") != _MANIFEST_SCHEMA:
                    raise SystemExit("[abort] manifest schema mismatch")
                if manifest.get("before") != args.before:
                    raise SystemExit(
                        "[abort] --before does not match the manifest cutoff"
                    )
                return _apply_metadata(engine, manifest)

            manifest = _build_manifest(engine, args.before)
            out_path = _write_manifest(
                manifest, settings, prefix="retire", override=args.manifest
            )
            print(
                f"[manifest] runs={manifest['run_count']} "
                f"units={manifest['unit_count']} before={args.before} "
                f"-> {out_path}"
            )
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
