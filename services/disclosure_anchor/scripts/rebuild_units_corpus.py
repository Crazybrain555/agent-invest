"""Corpus-wide unit rebuild driver for unit-builder rules upgrades.

Re-slices published documents from their existing succeeded parse runs
(RebuildUnits → BuildUnits → PublishRun; no MinerU re-parse, no GPU) so a
RULES_VERSION bump reaches the already-published corpus.  The worklist is
read from the live DB: every document whose active run was built under a
different builder_rules_version than the current code's RULES_VERSION.

Safe beside the resident worker: published documents are outside the
worker's parse/download queues, and each publish is one atomic supersede.

Resume: rerun with the same ``--ledger`` path — documents already recorded
as rebuilt are skipped.  Empty publishes fail loudly and stay in the
ledger for explicit review, mirroring reparse_corpus.

Usage:
  .venv/bin/python scripts/rebuild_units_corpus.py --ledger <path>.jsonl
      [--concurrency N] [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from disclosure_anchor.adapters.db.postgres.connection import create_db_engine
from disclosure_anchor.adapters.unit_builder.rules import RULES_VERSION
from disclosure_anchor.application.use_cases.build_units import BuildUnitsCommand
from disclosure_anchor.application.use_cases.publish_run import PublishRunCommand
from disclosure_anchor.application.use_cases.rebuild_units import (
    RebuildUnitsCommand,
)
from disclosure_anchor.cli.pipeline import _Deps, _database_url
from disclosure_anchor.settings import load_settings


def _worklist(engine, limit: int | None) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT d.document_id
                  FROM disclosure_core.document d
                  JOIN disclosure_core.processing_run pr
                    ON pr.processing_run_id = d.current_processing_run_id
                 WHERE pr.is_active
                   AND pr.builder_rules_version IS DISTINCT FROM :version
                 ORDER BY d.document_id
                """
            ),
            {"version": RULES_VERSION},
        ).scalars()
        items = list(rows)
    return items[:limit] if limit else items


def _load_done(ledger_path: Path) -> set[str]:
    done: set[str] = set()
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("status") == "rebuilt":
                done.add(record["document_id"])
    return done


def main() -> int:
    parser = argparse.ArgumentParser(prog="rebuild_units_corpus")
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    engine = create_db_engine(_database_url(settings))
    try:
        worklist = _worklist(engine, args.limit)
    finally:
        engine.dispose()
    done = _load_done(args.ledger)
    pending = [doc_id for doc_id in worklist if doc_id not in done]
    print(
        f"target rules_version={RULES_VERSION} worklist={len(worklist)} "
        f"already_done={len(worklist) - len(pending)} pending={len(pending)}"
    )
    if args.dry_run or not pending:
        return 0

    deps = _Deps(settings)
    ledger_lock = threading.Lock()
    counters = {"rebuilt": 0, "failed": 0}

    def _record(record: dict) -> None:
        record["at"] = datetime.now(timezone.utc).isoformat()
        with ledger_lock:
            with args.ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            counters["rebuilt" if record["status"] == "rebuilt" else "failed"] += 1
            total = counters["rebuilt"] + counters["failed"]
            if total % 100 == 0:
                print(
                    f"progress {total}/{len(pending)} "
                    f"(failed={counters['failed']})",
                    flush=True,
                )

    def _one(document_id: str) -> None:
        try:
            rebuilt = deps.rebuild_units().execute(
                RebuildUnitsCommand(document_id=document_id)
            )
            deps.build_units().execute(
                BuildUnitsCommand(processing_run_id=rebuilt.processing_run_id)
            )
            deps.publish().execute(
                PublishRunCommand(
                    processing_run_id=rebuilt.processing_run_id,
                    reason=f"rules-upgrade:{RULES_VERSION}",
                )
            )
            _record({"document_id": document_id, "status": "rebuilt"})
        except Exception as error:  # ledger keeps the failure for review
            _record(
                {
                    "document_id": document_id,
                    "status": "failed",
                    "error": repr(error)[:500],
                }
            )

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(_one, doc_id) for doc_id in pending]
        for future in as_completed(futures):
            future.result()

    print(
        f"done rebuilt={counters['rebuilt']} failed={counters['failed']} "
        f"ledger={args.ledger}"
    )
    return 1 if counters["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
