"""PostgreSQL implementation of the atomic initial V4 ingress."""

from __future__ import annotations

from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import (
    RemoteParseV4Repository,
)
from disclosure_anchor.adapters.db.postgres.remote_parse_v4_source_rejection import RemoteParseV4SourceRejector
from disclosure_anchor.application.ports.remote_parse_v4_source_rejection import V4SourceRejectionCommit
from disclosure_anchor.adapters.db.postgres.repositories import (
    DocumentRepository,
    OutboxRepository,
    ProcessingRunRepository,
)
from disclosure_anchor.application.ports.remote_parse_v4_ingress import (
    V4InitialIngressCommit,
    V4InitialIngressDrift,
    V4InitialIngressNotCommitted,
    V4InitialIngressNotEligible,
    V4InitialIngressReconciliation,
    initial_prepared_authority_matches_v4,
    initial_processing_run_created_event_v4,
    initial_processing_run_v4,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Authority,
    V4HeadNotFound,
)
from disclosure_anchor.application.worker.locks import acquire_document_xact_lock
from disclosure_anchor.application.worker.queries import pending_parse
from disclosure_anchor.domain.entities import OutboxEvent, ProcessingRun


class RemoteParseV4IngressCommitter:
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
        self._source_rejections = RemoteParseV4SourceRejector(session)

    def reject_source(self, command: V4SourceRejectionCommit) -> None:
        self._source_rejections.commit(command)

    def reconcile_source_rejection(self, command: V4SourceRejectionCommit) -> None:
        self._source_rejections.reconcile(command)

    def commit(
        self,
        command: V4InitialIngressCommit,
    ) -> RemoteParseV4Authority:
        if type(command) is not V4InitialIngressCommit:
            raise ValueError("v4 ingress command must be exact")
        document_row = self._lock_document(command)
        existing = self._existing_packet(command)
        if existing is not None:
            return existing.authority
        self._require_eligible(command, document_row=document_row)
        run = self._processing_runs.add(initial_processing_run_v4(command))
        if not _same_processing_run(run, initial_processing_run_v4(command)):
            raise V4InitialIngressDrift("v4 ingress processing run write drifted")
        event = self._outbox.add(initial_processing_run_created_event_v4(command))
        if not _same_outbox_event(
            event,
            initial_processing_run_created_event_v4(command),
        ):
            raise V4InitialIngressDrift("v4 ingress outbox write drifted")
        return self._remote_parse_v4.create_next_prepared(command.proposal)

    def reconcile(
        self,
        command: V4InitialIngressCommit,
    ) -> V4InitialIngressReconciliation:
        if type(command) is not V4InitialIngressCommit:
            raise ValueError("v4 ingress command must be exact")
        self._lock_document(command)
        existing = self._existing_packet(command)
        if existing is None:
            raise V4InitialIngressNotCommitted(
                "v4 initial ingress packet is absent"
            )
        return existing

    def _existing_packet(
        self,
        command: V4InitialIngressCommit,
    ) -> V4InitialIngressReconciliation | None:
        proposal = command.proposal
        observed_run = self._processing_runs.get(proposal.processing_run_id)
        observed_event = self._outbox.get(command.created_outbox_event_id)
        try:
            authority = self._remote_parse_v4.load(proposal.attempt_id)
        except V4HeadNotFound:
            authority = None
        present = (
            observed_run is not None,
            observed_event is not None,
            authority is not None,
        )
        if present == (False, False, False):
            return None
        if present != (True, True, True):
            raise V4InitialIngressDrift(
                "v4 initial ingress packet is only partially present"
            )
        assert observed_run is not None
        assert observed_event is not None
        assert authority is not None
        if (
            not _same_processing_run(observed_run, initial_processing_run_v4(command))
            or not _same_outbox_event(
                observed_event,
                initial_processing_run_created_event_v4(command),
            )
            or not initial_prepared_authority_matches_v4(
                authority,
                proposal,
            )
        ):
            raise V4InitialIngressDrift(
                "v4 initial ingress packet differs from committed result"
            )
        return V4InitialIngressReconciliation(
            authority=authority,
            processing_run=observed_run,
            outbox_event=observed_event,
        )

    def _lock_document(self, command: V4InitialIngressCommit) -> models.Document:
        proposal = command.proposal
        acquire_document_xact_lock(self._session, proposal.document_id)
        document_row = (
            self._session.query(models.Document)
            .filter(models.Document.document_id == proposal.document_id)
            .with_for_update()
            .one_or_none()
        )
        if document_row is None:
            raise V4InitialIngressNotEligible("v4 ingress document is absent")
        return document_row

    def _require_eligible(
        self,
        command: V4InitialIngressCommit,
        *,
        document_row: models.Document,
    ) -> None:
        proposal = command.proposal
        candidates = pending_parse(
            self._session.connection(),
            max_retries=command.max_retries,
            limit=1,
            scope_classes=command.scope_classes,
            require_active_company_scope=True,
            document_ids=(proposal.document_id,),
        )
        if len(candidates) != 1:
            raise V4InitialIngressNotEligible(
                "v4 ingress document is no longer parse-eligible"
            )
        row = candidates[0]
        expected_document = {
            "provider": command.expected_provider,
            "provider_document_id": command.expected_provider_document_id,
            "security_id": command.expected_security_id,
            "raw_file_relpath": command.expected_raw_file_relpath,
            "raw_file_hash": command.expected_raw_file_hash,
        }
        if any(
            getattr(document_row, field) != expected
            for field, expected in expected_document.items()
        ) or any(
            row[field] != expected_document[field]
            for field in ("raw_file_relpath", "raw_file_hash")
        ):
            raise V4InitialIngressDrift(
                "v4 ingress document facts changed after source observation"
            )
        raw_byte_count = row.get("raw_byte_count")
        if (
            raw_byte_count is not None
            and raw_byte_count != command.source_observation.byte_count
        ):
            raise V4InitialIngressDrift(
                "v4 ingress archived source byte count changed after observation"
            )


def _same_processing_run(observed: ProcessingRun, expected: ProcessingRun) -> bool:
    return all(
        getattr(observed, field) == getattr(expected, field)
        for field in ProcessingRun.__dataclass_fields__
        if field != "created_at"
    )


def _same_outbox_event(observed: OutboxEvent, expected: OutboxEvent) -> bool:
    return all(
        getattr(observed, field) == getattr(expected, field)
        for field in OutboxEvent.__dataclass_fields__
        if field not in {"seq", "created_at"}
    )


__all__ = ["RemoteParseV4IngressCommitter"]
