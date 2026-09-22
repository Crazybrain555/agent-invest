"""Record one append-only decision that releases a failed parse run.

Contract-class parse failures stay out of the queue until an operator states
what was fixed. This command writes that decision; it never edits the failed
run, never re-parses, and refuses anything it cannot prove from the stored
failure. ``--dry-run`` evaluates every guardrail and writes nothing.

The receipt reports three separate facts: whether the decision was recorded,
whether the document is eligible for parse right now (asked of the queue's own
predicate), and which blockers remain.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url, create_db_engine, require_runtime_app_engine,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.application.use_cases.parse_requeue import (
    ParseRequeue, ParseRequeueCommand, ParseRequeueResult,
)
from disclosure_anchor.application.worker.queries import parse_admission_diagnosis
from disclosure_anchor.domain.errors import ParseRequeueError
from disclosure_anchor.settings import load_settings

_REQUIRED = ("document_id", "processing_run_id", "fixed_by", "reason", "decided_by")
_ADMISSION_NOTE = (
    "recording a decision admits nothing by itself; the document is parsed "
    "only when a normal worker scan or an authorized campaign picks it up"
)


def _receipt(result: ParseRequeueResult, diagnosis: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_recorded": result.decision_id is not None,
        "decision_id": result.decision_id,
        "document_id": result.document_id,
        "processing_run_id": result.processing_run_id,
        "failure_error_code": result.failure_error_code,
        "failure_retry_budget_class": result.failure_retry_budget_class,
        "fixed_by": result.fixed_by,
        "decided_by": result.decided_by,
        "decided_at": (
            None if result.decided_at is None else result.decided_at.isoformat()
        ),
        "dry_run": result.dry_run,
        "currently_eligible": diagnosis["currently_eligible"],
        "remaining_blockers": diagnosis["remaining_blockers"],
        "note": _ADMISSION_NOTE,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--processing-run-id", required=True)
    parser.add_argument(
        "--fixed-by", required=True,
        help="what fixed the cause, e.g. 'semantic_router.v102 4548ecaa'",
    )
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--decided-by", required=True, help="operator identity; never inferred",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="evaluate every guardrail and print the would-be receipt",
    )
    args = parser.parse_args(argv)
    for name in _REQUIRED:
        if not str(getattr(args, name)).strip():
            parser.error(f"--{name.replace('_', '-')} must not be blank")
    command = ParseRequeueCommand(
        document_id=args.document_id,
        processing_run_id=args.processing_run_id,
        fixed_by=args.fixed_by,
        reason=args.reason,
        decided_by=args.decided_by,
    )
    settings = load_settings()
    engine = create_db_engine(app_database_url(settings))
    try:
        require_runtime_app_engine(engine)
        try:
            result = ParseRequeue(
                uow_factory=unit_of_work_factory(engine)
            ).execute(command, dry_run=args.dry_run)
        except ParseRequeueError as exc:
            print(json.dumps(exc.error, ensure_ascii=False, sort_keys=True), file=sys.stderr)
            return 1
        # Admission is read after the write, through the queue predicate the
        # worker uses; the receipt never claims more than that answer.
        with engine.connect() as connection:
            diagnosis = parse_admission_diagnosis(
                connection,
                document_id=result.document_id,
                max_retries=settings.disclosure_max_parse_retries,
            )
        print(
            json.dumps(_receipt(result, diagnosis), ensure_ascii=False, sort_keys=True, default=str)
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
