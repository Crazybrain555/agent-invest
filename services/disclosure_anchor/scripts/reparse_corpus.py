"""Full-corpus reparse driver (corpus-reparse-audit-r1).

Reparses existing raw PDFs through the production parse→build→publish chain,
one atomic publish per document, with a resumable JSONL ledger.  Run only
while the resident launchd worker is stopped; the driver additionally takes
the worker singleton advisory lock so a respawned worker exits with its
normal "[skip]" instead of racing the queues.

Worklist (read from the live DB, never a hand-rolled scope predicate):
  published  every document whose current run is active (source-identity
             reparse of the published corpus, unconditional)
  pending    documents the worker's own scope-filtered parse queue admits
             (first-time parses via ``queries.pending_parse``)
  parsed     documents stuck at status ``parsed`` (e.g. empty-publish dead
             letters); a fresh parse run supersedes their stalled run

Resume: rerun with the same ``--ledger`` path — documents already recorded
as published are skipped.  Failures stay in the ledger for explicit review;
empty-unit publishes fail loudly (EMPTY_RUN) and are never auto-allowed.

Usage:
  .venv/bin/python scripts/reparse_corpus.py --ledger <path>.jsonl
      [--concurrency N] [--limit N] [--only published|pending|parsed]
      [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from disclosure_anchor.adapters.db.postgres.connection import create_db_engine
from disclosure_anchor.application.worker import queries
from disclosure_anchor.application.worker.locks import WORKER_NS
from disclosure_anchor.application.worker.worker import _process_one_document
from disclosure_anchor.cli.worker import (
    PARSER_INFRASTRUCTURE_ERRORS,
    _database_url,
    _deps,
    _process_scope_classes,
)
from disclosure_anchor.settings import Settings, load_settings

# Consecutive infrastructure failures before the driver stops feeding work.
INFRA_BREAKER_THRESHOLD = 5


def _worklist(
    engine: sqlalchemy.Engine,
    settings: Settings,
    only: str | None,
) -> list[tuple[str, str]]:
    """Return ordered (document_id, bucket) pairs without duplicates."""

    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    with engine.connect() as conn:
        if only in (None, "published"):
            rows = conn.execute(
                text(
                    """
                    SELECT d.document_id
                      FROM disclosure_core.document d
                      JOIN disclosure_core.processing_run pr
                        ON pr.processing_run_id = d.current_processing_run_id
                     WHERE pr.is_active
                     ORDER BY d.document_id
                    """
                )
            ).scalars()
            for document_id in rows:
                if document_id not in seen:
                    seen.add(document_id)
                    items.append((document_id, "published"))
        if only in (None, "pending"):
            pending = queries.pending_parse(
                conn,
                max_retries=settings.disclosure_max_parse_retries,
                limit=1_000_000,
                scope_classes=_process_scope_classes(settings),
            )
            for row in pending:
                document_id = str(row["document_id"])
                if bool(row.get("oversized")) or document_id in seen:
                    continue
                seen.add(document_id)
                items.append((document_id, "pending"))
        if only in (None, "parsed"):
            rows = conn.execute(
                text(
                    "SELECT document_id FROM disclosure_core.document"
                    " WHERE status = 'parsed' ORDER BY document_id"
                )
            ).scalars()
            for document_id in rows:
                if document_id not in seen:
                    seen.add(document_id)
                    items.append((document_id, "parsed"))
    return items


def _raw_sizes(
    engine: sqlalchemy.Engine, settings: Settings, document_ids: list[str]
) -> dict[str, float]:
    """Raw PDF byte sizes for scheduling; missing files sort last."""

    if not document_ids:
        return {}
    data_root = Path(settings.disclosure_data_root) / "data"
    sizes: dict[str, float] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT document_id, raw_file_relpath"
                " FROM disclosure_core.document"
                " WHERE document_id = ANY(:ids) AND raw_file_relpath IS NOT NULL"
            ),
            {"ids": document_ids},
        )
        for document_id, relpath in rows:
            try:
                sizes[document_id] = (data_root / relpath).stat().st_size
            except OSError:
                sizes[document_id] = float("inf")
    return sizes


def _load_done(ledger_path: Path) -> set[str]:
    done: set[str] = set()
    if not ledger_path.exists():
        return done
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("published"):
            done.add(str(record["document_id"]))
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reparse_corpus")
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", choices=("published", "pending", "parsed"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings()
    engine = create_db_engine(_database_url(settings))

    # Same NullPool/AUTOCOMMIT singleton pattern as cli/worker.py so a
    # KeepAlive-respawned worker sees the lock and exits "[skip]".
    lock_engine = sqlalchemy.create_engine(
        _database_url(settings), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    lock_conn = lock_engine.connect()
    try:
        items = _worklist(engine, settings, args.only)
        done = _load_done(args.ledger)
        todo = [(d, bucket) for d, bucket in items if d not in done]
        # Small-first scheduling: giant filings otherwise monopolise every
        # slot for 30-60 minutes each (observed 2026-07-17 annual-report
        # cluster). Unknown sizes sort last, with the giants, fail-closed.
        sizes = _raw_sizes(engine, settings, [d for d, _ in todo])
        todo.sort(key=lambda item: sizes.get(item[0], float("inf")))
        if args.limit is not None:
            todo = todo[: args.limit]
        by_bucket: dict[str, int] = {}
        for _, bucket in todo:
            by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
        print(
            f"[worklist] total={len(items)} done={len(done)} todo={len(todo)} "
            f"buckets={by_bucket}"
        )
        if args.dry_run:
            return 0

        acquired = lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:ns, 0)"), {"ns": WORKER_NS}
        ).scalar_one()
        if not acquired:
            print("[abort] worker singleton lock is held — stop the worker first")
            return 2

        deps = _deps(settings, engine)
        # Fail fast on parser identity before consuming any document.
        deps.parser_factory().identity()

        concurrency = max(
            1, args.concurrency or settings.worker_parse_concurrency
        )
        ledger_lock = threading.Lock()
        args.ledger.parent.mkdir(parents=True, exist_ok=True)
        counters = {"published": 0, "failed": 0, "consecutive_infra": 0}
        stop_event = threading.Event()

        def run_one(document_id: str, bucket: str) -> None:
            if stop_event.is_set():
                return
            outcome = _process_one_document(deps, document_id)
            record = {
                "document_id": document_id,
                "bucket": bucket,
                "parsed": outcome.parsed,
                "built": outcome.built,
                "published": outcome.published,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            if outcome.failure is not None:
                record["failure"] = {
                    "stage": outcome.failure.stage,
                    "error_code": outcome.failure.error_code,
                }
            with ledger_lock:
                with args.ledger.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                counters["published" if outcome.published else "failed"] += 1
                # Circuit breaker (worker _halts_parse_refill analogue): a run
                # of consecutive parser-infrastructure failures means the GPU
                # server is down — stop feeding documents instead of burning a
                # failed run per document (observed 2026-07-17 outage).
                if (
                    outcome.failure is not None
                    and outcome.failure.error_code
                    in PARSER_INFRASTRUCTURE_ERRORS
                ):
                    counters["consecutive_infra"] += 1
                    if counters["consecutive_infra"] >= INFRA_BREAKER_THRESHOLD:
                        if not stop_event.is_set():
                            print(
                                "[breaker] "
                                f"{counters['consecutive_infra']} consecutive "
                                "parser-infrastructure failures — halting; fix "
                                "the parser backend and rerun with the same "
                                "ledger"
                            )
                        stop_event.set()
                else:
                    counters["consecutive_infra"] = 0
                total_done = counters["published"] + counters["failed"]
                if total_done % 25 == 0 or not outcome.published:
                    print(
                        f"[{total_done}/{len(todo)}] {document_id} "
                        f"published={outcome.published} "
                        f"failure={record.get('failure')}"
                    )

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [
                pool.submit(run_one, document_id, bucket)
                for document_id, bucket in todo
            ]
            for future in as_completed(futures):
                future.result()

        print(
            f"[done] published={counters['published']} failed={counters['failed']}"
        )
        if stop_event.is_set():
            return 2
        return 0 if counters["failed"] == 0 else 1
    finally:
        lock_conn.close()
        lock_engine.dispose()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
