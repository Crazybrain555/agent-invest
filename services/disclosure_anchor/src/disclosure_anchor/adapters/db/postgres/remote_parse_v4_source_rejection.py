"""Atomic ordinary-backlog source rejection without a synthetic V4 attempt."""

from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.db.postgres.repositories import (
    DocumentRepository, OutboxRepository, ProcessingRunRepository,
)
from disclosure_anchor.application.ports.remote_parse_v4_ingress import (
    V4InitialIngressDrift, V4InitialIngressNotCommitted, V4InitialIngressNotEligible,
)
from disclosure_anchor.application.ports.remote_parse_v4_source_rejection import (
    V4SourceRejectionCommit, rejected_source_events, rejected_source_processing_run,
)
from disclosure_anchor.application.worker.locks import acquire_document_xact_lock
from disclosure_anchor.application.worker.queries import pending_parse
from disclosure_anchor.domain.entities import OutboxEvent, ProcessingRun


class RemoteParseV4SourceRejector:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._documents = DocumentRepository(session)
        self._runs = ProcessingRunRepository(session)
        self._outbox = OutboxRepository(session)

    def commit(self, command: V4SourceRejectionCommit) -> None:
        self._lock_document(command)
        if self._existing(command):
            return
        candidate = command.candidate
        rows = pending_parse(
            self._session.connection(), max_retries=command.max_retries, limit=1,
            scope_classes=command.scope_classes, require_active_company_scope=True,
            document_ids=(candidate.document_id,),
        )
        if len(rows) != 1:
            raise V4InitialIngressNotEligible("rejected source is no longer ordinary parse work")
        archived_count = rows[0].get("raw_byte_count")
        if archived_count is not None and archived_count != command.rejection.byte_count:
            raise V4InitialIngressDrift("rejected source archived length changed")
        self._runs.add(rejected_source_processing_run(command))
        for event in rejected_source_events(command):
            self._outbox.add(event)
        document = self._documents.get(candidate.document_id)
        if document is None:
            raise V4InitialIngressDrift("source rejection document disappeared under lock")
        if document.current_processing_run_id is None:
            document.status = "parse_failed"
            self._documents.update(document)

    def reconcile(self, command: V4SourceRejectionCommit) -> None:
        self._lock_document(command)
        if not self._existing(command):
            raise V4InitialIngressNotCommitted("source rejection episode is absent")

    def _lock_document(self, command: V4SourceRejectionCommit) -> None:
        if type(command) is not V4SourceRejectionCommit:
            raise ValueError("source rejection requires an exact command")
        candidate = command.candidate
        acquire_document_xact_lock(self._session, candidate.document_id)
        row = (self._session.query(models.Document)
               .filter(models.Document.document_id == candidate.document_id)
               .with_for_update().one_or_none())
        if row is None:
            raise V4InitialIngressNotEligible("source rejection document is absent")
        if any(getattr(row, name) != getattr(candidate, name) for name in (
            "provider", "provider_document_id", "security_id", "raw_file_relpath", "raw_file_hash",
        )):
            raise V4InitialIngressDrift("source rejection current raw identity changed")

    def _existing(self, command: V4SourceRejectionCommit) -> bool:
        run = self._runs.get(command.processing_run_id)
        expected_events = rejected_source_events(command)
        events = tuple(self._outbox.get(event.event_id) for event in expected_events)
        if run is None and all(event is None for event in events):
            return False
        if run is None or any(event is None for event in events):
            raise V4InitialIngressDrift("source rejection episode is partially present")
        if any(getattr(run, name) != getattr(rejected_source_processing_run(command), name)
               for name in ProcessingRun.__dataclass_fields__ if name != "created_at"):
            raise V4InitialIngressDrift("source rejection processing run differs")
        for actual, expected in zip(events, expected_events, strict=True):
            assert actual is not None
            if any(getattr(actual, name) != getattr(expected, name)
                   for name in OutboxEvent.__dataclass_fields__ if name not in {"seq", "created_at"}):
                raise V4InitialIngressDrift("source rejection semantic event differs")
        event_count = (self._session.query(models.OutboxEvent)
                       .filter(models.OutboxEvent.processing_run_id == command.processing_run_id).count())
        attempt = (self._session.query(models.RemoteParseAttempt)
                   .filter(models.RemoteParseAttempt.processing_run_id == command.processing_run_id).first())
        if event_count != 2 or attempt is not None:
            raise V4InitialIngressDrift("source rejection has extra events or a fabricated H0")
        document = self._documents.get(command.candidate.document_id)
        if document is None or (document.current_processing_run_id is None and document.status != "parse_failed"):
            raise V4InitialIngressDrift("source rejection document disposition differs")
        return True


__all__ = ["RemoteParseV4SourceRejector"]
