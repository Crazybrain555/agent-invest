"""PostgreSQL transaction script for final resourceful V4 failures."""

from __future__ import annotations

from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import (
    RemoteParseV4Repository,
)
from disclosure_anchor.adapters.db.postgres.repositories import (
    DocumentRepository,
    OutboxRepository,
    ProcessingRunRepository,
)
from disclosure_anchor.application.ports.remote_parse_v4_failure_committer import (
    V4FinalFailureCommit,
    V4FinalFailureDrift,
    V4FinalFailureReconciliation,
    failure_receipt_from_authority_v4,
    processing_run_error_from_failure_v4,
    processing_run_failed_event_v4,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Authority,
)
from disclosure_anchor.application.worker.locks import acquire_document_xact_lock
from disclosure_anchor.domain.entities import Document, OutboxEvent, ProcessingRun


class RemoteParseV4FailureCommitter:
    def __init__(
        self,
        session: Session,
        *,
        remote_parse_v4: RemoteParseV4Repository,
    ) -> None:
        self._session = session
        self._remote_parse_v4 = remote_parse_v4
        self._documents = DocumentRepository(session)
        self._processing_runs = ProcessingRunRepository(session)
        self._outbox = OutboxRepository(session)

    def commit(
        self,
        command: V4FinalFailureCommit,
    ) -> RemoteParseV4Authority:
        if type(command) is not V4FinalFailureCommit:
            raise ValueError("v4 final-failure command must be exact")
        document, run = self._lock_roots(command)
        if self._failure_events(run.processing_run_id):
            raise V4FinalFailureDrift(
                "v4 final failure already has a semantic outbox event"
            )
        authority = self._remote_parse_v4.append_successor(command.append)
        receipt = failure_receipt_from_authority_v4(authority)
        error = processing_run_error_from_failure_v4(receipt)
        if run.status != "running":
            raise V4FinalFailureDrift(
                "v4 final failure processing run is not running"
            )
        run.status = "failed"
        run.finished_at = command.failed_at
        run.error = error
        self._processing_runs.update(run)

        if document.current_processing_run_id is None:
            document.status = "parse_failed"
            self._documents.update(document)

        expected_event = processing_run_failed_event_v4(command, receipt)
        self._outbox.add(expected_event)
        return authority

    def reconcile(
        self,
        command: V4FinalFailureCommit,
    ) -> V4FinalFailureReconciliation:
        if type(command) is not V4FinalFailureCommit:
            raise ValueError("v4 final-failure command must be exact")
        document, run = self._lock_roots(command)
        reconciled = self._remote_parse_v4.reconcile_successor(command.append)
        authority = reconciled.authority
        receipt = failure_receipt_from_authority_v4(authority)
        expected_error = processing_run_error_from_failure_v4(receipt)
        if (
            run.status != "failed"
            or run.finished_at != command.failed_at
            or run.error != expected_error
        ):
            raise V4FinalFailureDrift(
                "v4 final failure processing run differs from committed result"
            )
        if (
            document.current_processing_run_id is None
            and document.status != "parse_failed"
        ):
            raise V4FinalFailureDrift(
                "v4 final failure document differs from committed result"
            )
        expected_event = processing_run_failed_event_v4(command, receipt)
        observed_events = self._failure_events(run.processing_run_id)
        if len(observed_events) != 1 or not _same_outbox_event(
            observed_events[0], expected_event
        ):
            raise V4FinalFailureDrift(
                "v4 final failure outbox differs from committed result"
            )
        return V4FinalFailureReconciliation(
            authority=authority,
            document=document,
            processing_run=run,
            outbox_event=observed_events[0],
        )

    def _lock_roots(
        self,
        command: V4FinalFailureCommit,
    ) -> tuple[Document, ProcessingRun]:
        successor = command.append.successor
        acquire_document_xact_lock(self._session, successor.document_id)
        document_row = (
            self._session.query(models.Document)
            .filter(models.Document.document_id == successor.document_id)
            .with_for_update()
            .one_or_none()
        )
        if document_row is None:
            raise V4FinalFailureDrift("v4 final failure document is absent")
        run_row = (
            self._session.query(models.ProcessingRun)
            .filter(
                models.ProcessingRun.processing_run_id
                == successor.processing_run_id
            )
            .with_for_update()
            .one_or_none()
        )
        if run_row is None:
            raise V4FinalFailureDrift("v4 final failure processing run is absent")
        run = self._processing_runs.get(successor.processing_run_id)
        if run is None or run.document_id != successor.document_id:
            raise V4FinalFailureDrift(
                "v4 final failure processing run ownership drifted"
            )
        document = self._documents.get(successor.document_id)
        if document is None:
            raise V4FinalFailureDrift("v4 final failure document is absent")
        return document, run

    def _failure_events(self, processing_run_id: str) -> tuple[OutboxEvent, ...]:
        rows = (
            self._session.query(models.OutboxEvent)
            .filter(
                models.OutboxEvent.processing_run_id == processing_run_id,
                models.OutboxEvent.event_kind == "processing_run_failed",
            )
            .order_by(models.OutboxEvent.event_id)
            .with_for_update()
            .all()
        )
        events: list[OutboxEvent] = []
        for row in rows:
            event = self._outbox.get(row.event_id)
            if event is None:
                raise V4FinalFailureDrift("v4 final failure outbox disappeared")
            events.append(event)
        return tuple(events)


def _same_outbox_event(observed: OutboxEvent, expected: OutboxEvent) -> bool:
    return all(
        getattr(observed, field) == getattr(expected, field)
        for field in (
            "event_id",
            "event_kind",
            "change_kind",
            "subject_kind",
            "subject_ref",
            "document_id",
            "processing_run_id",
            "asset_id",
            "payload",
            "occurred_at",
        )
    )


__all__ = ["RemoteParseV4FailureCommitter"]
