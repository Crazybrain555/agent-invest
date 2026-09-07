"""Read-only, exact owner guard for a private accepted-result recovery grant."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from disclosure_anchor.application.contracts.staged_resource_credit import (
    STAGED_RESOURCE_STATE_TRANSITIONS,
)
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    validate_admission_document_ids,
)


def validate_recovery_scope_rows(
    rows: list[dict[str, Any]],
    *,
    attempts: tuple[dict[str, str], ...],
    runtime_sha256: str,
) -> None:
    expected = {item["attempt_id"]: item for item in attempts}
    if len(rows) != len(expected) or {row["attempt_id"] for row in rows} != set(
        expected
    ):
        raise RuntimeError(
            "recovery scope contains a missing, replaced, or outside owner"
        )
    for row in rows:
        pinned = expected[row["attempt_id"]]
        if (
            any(row[key] != value for key, value in pinned.items())
            or row["runtime_epoch_sha256"] != runtime_sha256
            or (
                row["is_current"] is not True
                and row["state"] not in {"acked", "local_failed", "remote_failed"}
            )
            or row["state"]
            not in {
                "submitted",
                "remote_terminal",
                "materializing",
                "local_materialized",
                "publish_committed",
                "cleanup_pending",
                "ack_pending",
                "acked",
                "local_failed",
                "remote_failed",
            }
        ):
            raise RuntimeError(
                "recovery owner/H0/spec/accepted receipt drifted or lacks acceptance"
            )


def require_accepted_recovery_scope(
    engine: Engine,
    *,
    attempts: tuple[dict[str, str], ...],
    runtime_sha256: str,
) -> None:
    rows = inspect_accepted_recovery_scope(
        engine,
        document_ids=tuple(item["document_id"] for item in attempts),
        accepted_attempt_ids=tuple(item["attempt_id"] for item in attempts),
    )
    validate_recovery_scope_rows(rows, attempts=attempts, runtime_sha256=runtime_sha256)


def inspect_accepted_recovery_scope(
    engine: Engine,
    *,
    document_ids: tuple[str, ...],
    accepted_attempt_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    validate_admission_document_ids(document_ids)
    if document_ids is None:
        raise ValueError("recovery inspection requires explicit document scope")
    if (
        type(accepted_attempt_ids) is not tuple
        or len(accepted_attempt_ids) > 8
        or len(set(accepted_attempt_ids)) != len(accepted_attempt_ids)
        or any(
            type(item) is not str or not 1 <= len(item) <= 128
            for item in accepted_attempt_ids
        )
    ):
        raise ValueError("recovery inspection attempt scope is invalid")
    # The OR deliberately includes every unresolved current owner globally.
    # Never filter away another responsibility to make a recovery scope fit.
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT a.document_id,a.attempt_id,a.processing_run_id,a.fence_identity,"
                "a.source_pdf_sha256,a.runtime_epoch_sha256,a.state,a.is_current,"
                "s.execution_spec_sha256,h.checkpoint_sha256 AS h0_sha256,"
                "c.accepted_submission_sha256 "
                "FROM disclosure_ops.remote_parse_attempt a "
                "LEFT JOIN disclosure_ops.remote_parse_v4_execution_spec s "
                "ON s.attempt_id=a.attempt_id AND s.fence_identity=a.fence_identity "
                "LEFT JOIN disclosure_ops.remote_parse_v4_checkpoint h "
                "ON h.attempt_id=a.attempt_id AND h.fence_identity=a.fence_identity AND h.lifecycle_version=0 "
                "LEFT JOIN disclosure_ops.remote_parse_v4_checkpoint c "
                "ON c.attempt_id=a.attempt_id AND c.checkpoint_sha256=a.current_checkpoint_sha256 "
                "WHERE a.checkpoint_contract_version=4 AND "
                "((a.is_current AND (a.state=ANY(:resource_states) OR a.document_id=ANY(:document_ids))) "
                "OR a.attempt_id=ANY(:accepted_attempt_ids))"
            ),
            {
                "resource_states": list(STAGED_RESOURCE_STATE_TRANSITIONS),
                "document_ids": list(document_ids),
                "accepted_attempt_ids": list(accepted_attempt_ids),
            },
        ).mappings()
        return [dict(row) for row in rows]
