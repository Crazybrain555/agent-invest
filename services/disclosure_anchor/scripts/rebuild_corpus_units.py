"""Bulk rules-only unit rebuild over every active document (no MinerU).

The production loop after a RULES_VERSION / note_key_map bump: per document,
RebuildUnits (new run reusing the frozen parse artifacts) → BuildUnits →
PublishRun, exactly the ``pipeline rebuild-units`` chain, threaded with a
resumable JSONL ledger. Safe alongside the resident worker (per-document
advisory locks; publish is idempotent), but pausing it avoids duplicate runs.

Usage:
  .venv/bin/python scripts/rebuild_corpus_units.py --ledger <path>.jsonl
      [--concurrency N] [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from disclosure_anchor.adapters.db.postgres.connection import create_db_engine
from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.parsers.mineru.source_evidence_validator import (
    MinerUSourceEvidenceValidator,
)
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.use_cases.build_units import (
    BuildUnits,
    BuildUnitsCommand,
)
from disclosure_anchor.application.use_cases.publish_run import (
    NormalizedIRPublicationGuard,
    PublishRun,
    PublishRunCommand,
)
from disclosure_anchor.application.use_cases.rebuild_units import (
    RebuildUnits,
    RebuildUnitsCommand,
)
from disclosure_anchor.application.services.unit_builder import rules
from disclosure_anchor.cli.worker import _database_url
from disclosure_anchor.settings import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rebuild_corpus_units")
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings()
    engine = create_db_engine(_database_url(settings))
    paths = FileStorePathBuilder(settings)
    try:
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
                {"version": rules.RULES_VERSION},
            ).scalars()
            todo = list(rows)
        done: set[str] = set()
        if args.ledger.exists():
            for line in args.ledger.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("published"):
                    done.add(str(record["document_id"]))
        todo = [d for d in todo if d not in done]
        if args.limit is not None:
            todo = todo[: args.limit]
        print(
            f"[worklist] stale-rules docs={len(todo)} target={rules.RULES_VERSION}"
        )
        if args.dry_run:
            return 0

        def uow_factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(engine=engine)

        ledger_lock = threading.Lock()
        args.ledger.parent.mkdir(parents=True, exist_ok=True)
        counters = {"published": 0, "failed": 0}

        def run_one(document_id: str) -> None:
            record: dict[str, object] = {
                "document_id": document_id,
                "published": False,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            try:
                rebuilt = RebuildUnits(uow_factory=uow_factory).execute(
                    RebuildUnitsCommand(document_id=document_id)
                )
                run_id = rebuilt.processing_run_id
                build = BuildUnits(
                    path_builder=paths,
                    artifact_store=ArtifactStore(paths),
                    uow_factory=uow_factory,
                    source_evidence_validator=MinerUSourceEvidenceValidator(),
                ).execute(BuildUnitsCommand(processing_run_id=run_id))
                if build.status != "succeeded":
                    record["failure"] = build.error
                else:
                    publish = PublishRun(
                        uow_factory=uow_factory,
                        publication_guard=NormalizedIRPublicationGuard(paths),
                    ).execute(
                        PublishRunCommand(processing_run_id=run_id)
                    )
                    record["published"] = publish.status == "published"
                    if not record["published"]:
                        record["failure"] = {"stage": "publish", "status": publish.status}
            except Exception as exc:  # noqa: BLE001 — ledgered, rerun retries
                record["failure"] = {"stage": "chain", "error": str(exc)[:200]}
            with ledger_lock:
                with args.ledger.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                counters["published" if record["published"] else "failed"] += 1
                total = counters["published"] + counters["failed"]
                if total % 100 == 0 or record.get("failure"):
                    print(f"[{total}/{len(todo)}] {document_id} {record.get('failure') or 'ok'}")

        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            futures = [pool.submit(run_one, document_id) for document_id in todo]
            for future in as_completed(futures):
                future.result()
        print(f"[done] published={counters['published']} failed={counters['failed']}")
        return 0 if counters["failed"] == 0 else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
