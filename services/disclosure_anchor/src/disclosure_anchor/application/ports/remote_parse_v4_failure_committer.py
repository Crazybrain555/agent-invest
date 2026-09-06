"""One-transaction closure for resourceful V4 parse failures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    FailureReceiptV4,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Authority,
    V4SuccessorAppend,
)
from disclosure_anchor.domain.entities import Document, OutboxEvent, ProcessingRun

_FINAL_FAILURE_STATES = frozenset(
    {"pre_submission_failed", "remote_failed", "local_failed"}
)
_STATE_OUTCOME = {
    "pre_submission_failed": "pre_submission_failure",
    "remote_failed": "remote_failure",
    "local_failed": "local_failure",
}


class V4FinalFailureCommitError(RuntimeError):
    """Base class for a V4 final-failure transaction violation."""


class V4FinalFailureDrift(V4FinalFailureCommitError):
    """The V4 head, processing run, or fixed outbox event disagrees."""


@dataclass(frozen=True, slots=True)
class V4FinalFailureCommit:
    append: V4SuccessorAppend
    failed_at: datetime
    outbox_event_id: str

    def __post_init__(self) -> None:
        if (
            type(self.append) is not V4SuccessorAppend
            or self.append.successor.state not in _FINAL_FAILURE_STATES
        ):
            raise ValueError("v4 final-failure append is invalid")
        if (
            not isinstance(self.failed_at, datetime)
            or self.failed_at.tzinfo is None
            or self.failed_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("v4 final-failure time must be UTC aware")
        if (
            type(self.outbox_event_id) is not str
            or not self.outbox_event_id.strip()
            or len(self.outbox_event_id.encode("utf-8")) > 64
        ):
            raise ValueError("v4 final-failure outbox identity is invalid")


@dataclass(frozen=True, slots=True)
class V4FinalFailureReconciliation:
    authority: RemoteParseV4Authority
    document: Document
    processing_run: ProcessingRun
    outbox_event: OutboxEvent

    def __post_init__(self) -> None:
        if (
            type(self.authority) is not RemoteParseV4Authority
            or type(self.document) is not Document
            or type(self.processing_run) is not ProcessingRun
            or type(self.outbox_event) is not OutboxEvent
        ):
            raise ValueError("v4 final-failure reconciliation is invalid")


def failure_receipt_from_authority_v4(
    authority: RemoteParseV4Authority,
) -> FailureReceiptV4:
    receipts = tuple(
        item.value
        for item in authority.evidence
        if item.kind == "failure_receipt"
    )
    if len(receipts) != 1 or type(receipts[0]) is not FailureReceiptV4:
        raise V4FinalFailureDrift("v4 final authority lacks one failure receipt")
    receipt = receipts[0]
    expected_outcome = _STATE_OUTCOME.get(authority.state)
    if expected_outcome is None or receipt.outcome != expected_outcome:
        raise V4FinalFailureDrift("v4 final state and failure outcome disagree")
    return receipt


def processing_run_error_from_failure_v4(
    receipt: FailureReceiptV4,
) -> dict[str, Any]:
    if type(receipt) is not FailureReceiptV4:
        raise ValueError("processing-run error requires an exact failure receipt")
    return {
        "stage": receipt.error_stage,
        "error_code": receipt.error_code,
        "error_class": receipt.error_class,
        "retryable": receipt.retryable,
        "retry_budget_class": receipt.retry_budget_class,
        "message": receipt.message,
    }


def processing_run_failed_event_v4(
    command: V4FinalFailureCommit,
    receipt: FailureReceiptV4,
) -> OutboxEvent:
    successor = command.append.successor
    error = processing_run_error_from_failure_v4(receipt)
    return OutboxEvent(
        event_id=command.outbox_event_id,
        event_kind="processing_run_failed",
        change_kind="observed",
        subject_kind="processing_run",
        subject_ref=successor.processing_run_id,
        document_id=successor.document_id,
        processing_run_id=successor.processing_run_id,
        payload={
            "document_id": successor.document_id,
            "status": "failed",
            "error": error,
        },
        occurred_at=command.failed_at,
    )


class RemoteParseV4FailureCommitter(Protocol):
    def commit(
        self,
        command: V4FinalFailureCommit,
    ) -> RemoteParseV4Authority: ...

    def reconcile(
        self,
        command: V4FinalFailureCommit,
    ) -> V4FinalFailureReconciliation: ...


__all__ = [
    "RemoteParseV4FailureCommitter",
    "V4FinalFailureCommit",
    "V4FinalFailureCommitError",
    "V4FinalFailureDrift",
    "V4FinalFailureReconciliation",
    "failure_receipt_from_authority_v4",
    "processing_run_error_from_failure_v4",
    "processing_run_failed_event_v4",
]
