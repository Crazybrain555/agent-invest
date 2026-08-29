"""SQLAlchemy repository implementations.

Each repository adds domain entities into the active session (mapping them to ORM
models) and loads them back as entities. They never commit; the UnitOfWork owns
the transaction boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
import json
from typing import Any, Optional, cast

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres import mappers, models
from disclosure_anchor.application.contracts.remote_parse_checkpoint import (
    ALLOWED_TRANSITIONS,
    AcceptedSubmissionReceipt,
    EncodedCheckpointReceipt,
    EncodedTerminalReceipt,
    RemoteParseAttempt,
    RemoteParseCheckpointConflict,
    RemoteParseResumeSecret,
    FailureReceipt,
    LocalMaterializationReceipt,
    PreparedReconcileReceipt,
    decode_checkpoint_receipt,
    decode_terminal_receipt,
)
from disclosure_anchor.application.contracts.publish_evidence_ledger import (
    DurablePublishBaseEvidence,
    DurablePublishSupplementEvidence,
    EncodedProgressRelayCheckpoint,
    PublishEvidenceConflict,
)
from disclosure_anchor.adapters.db.postgres.classification_refresh import (
    refresh_document_classification,
)
from disclosure_anchor.application.worker.locks import (
    OUTBOX_NS,
    acquire_document_xact_lock,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.entities import outbox_events
from disclosure_anchor.domain.value_objects import canonical_security_identity
from disclosure_anchor.domain.errors import (
    DocumentIdentityConflictError,
    SubjectIdentityRaceError,
)


class CompanyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, company: e.Company) -> e.Company:
        row = mappers.company_to_model(company)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            detail = str(getattr(exc, "orig", exc))
            if "unified_social_credit_code" in detail:
                raise SubjectIdentityRaceError(
                    "company unified_social_credit_code already exists"
                ) from exc
            raise
        return mappers.company_to_entity(row)

    def get(self, company_id: str) -> Optional[e.Company]:
        row = self._session.get(models.Company, company_id)
        return mappers.company_to_entity(row) if row is not None else None

    def get_by_legal_name(self, legal_name: str) -> Optional[e.Company]:
        row = (
            self._session.query(models.Company)
            .filter(models.Company.legal_name == legal_name)
            .order_by(
                models.Company.created_at.desc(), models.Company.company_id.desc()
            )
            .first()
        )
        return mappers.company_to_entity(row) if row is not None else None

    def get_by_credit_code(self, uscc: str) -> Optional[e.Company]:
        row = (
            self._session.query(models.Company)
            .filter(models.Company.unified_social_credit_code == uscc)
            .one_or_none()
        )
        return mappers.company_to_entity(row) if row is not None else None

    def update(self, company: e.Company) -> e.Company:
        row = self._session.get(models.Company, company.company_id)
        if row is None:
            raise KeyError(f"company not found: {company.company_id}")
        updated = mappers.company_to_model(company)
        for column in ("legal_name", "unified_social_credit_code"):
            setattr(row, column, getattr(updated, column))
        self._session.flush()
        return mappers.company_to_entity(row)


class CompanyIdentifierRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, identifier: e.CompanyIdentifier) -> e.CompanyIdentifier:
        row = mappers.company_identifier_to_model(identifier)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            detail = str(getattr(exc, "orig", exc))
            if "uq_company_identifier_strong_key" in detail:
                raise SubjectIdentityRaceError(
                    "company identifier strong key already exists"
                ) from exc
            raise
        return mappers.company_identifier_to_entity(row)

    def get(self, identifier_id: str) -> Optional[e.CompanyIdentifier]:
        row = self._session.get(models.CompanyIdentifier, identifier_id)
        return mappers.company_identifier_to_entity(row) if row is not None else None

    def get_by_scheme_value(
        self, scheme: str, normalized_value: str
    ) -> Optional[e.CompanyIdentifier]:
        row = (
            self._session.query(models.CompanyIdentifier)
            .filter(
                models.CompanyIdentifier.scheme == scheme,
                models.CompanyIdentifier.normalized_value == normalized_value,
                models.CompanyIdentifier.status == "active",
            )
            .order_by(
                models.CompanyIdentifier.created_at.desc(),
                models.CompanyIdentifier.identifier_id.desc(),
            )
            .first()
        )
        return mappers.company_identifier_to_entity(row) if row is not None else None

    def update(self, identifier: e.CompanyIdentifier) -> e.CompanyIdentifier:
        row = self._session.get(models.CompanyIdentifier, identifier.identifier_id)
        if row is None:
            raise KeyError(f"company identifier not found: {identifier.identifier_id}")
        updated = mappers.company_identifier_to_model(identifier)
        for column in (
            "company_id",
            "scheme",
            "raw_value",
            "normalized_value",
            "jurisdiction",
            "source_access_id",
            "status",
            "valid_from",
            "valid_to",
            "observed_at",
        ):
            setattr(row, column, getattr(updated, column))
        self._session.flush()
        return mappers.company_identifier_to_entity(row)


class SecurityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, security: e.Security) -> e.Security:
        row = mappers.security_to_model(security)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            detail = str(getattr(exc, "orig", exc))
            if "uq_security_code_exchange" in detail:
                raise SubjectIdentityRaceError(
                    "security code/exchange already exists"
                ) from exc
            raise
        return mappers.security_to_entity(row)

    def get(self, security_id: str) -> Optional[e.Security]:
        row = self._session.get(models.Security, security_id)
        return mappers.security_to_entity(row) if row is not None else None

    def get_by_code_exchange(
        self, security_code: str, exchange: str
    ) -> Optional[e.Security]:
        security_code, exchange = canonical_security_identity(security_code, exchange)
        row = (
            self._session.query(models.Security)
            .filter(
                models.Security.security_code == security_code,
                models.Security.exchange == exchange,
            )
            .one_or_none()
        )
        return mappers.security_to_entity(row) if row is not None else None


class TrackedCompanyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, tracked_company: e.TrackedCompany) -> e.TrackedCompany:
        row = mappers.tracked_company_to_model(tracked_company)
        self._session.add(row)
        self._session.flush()
        return mappers.tracked_company_to_entity(row)

    def get(self, tracked_company_id: str) -> Optional[e.TrackedCompany]:
        row = self._session.get(models.TrackedCompany, tracked_company_id)
        return mappers.tracked_company_to_entity(row) if row is not None else None

    def get_by_company_id(self, company_id: str) -> Optional[e.TrackedCompany]:
        row = (
            self._session.query(models.TrackedCompany)
            .filter(models.TrackedCompany.company_id == company_id)
            .one_or_none()
        )
        return mappers.tracked_company_to_entity(row) if row is not None else None

    def list_all(self) -> list[e.TrackedCompany]:
        rows = (
            self._session.query(models.TrackedCompany)
            .order_by(models.TrackedCompany.tracked_company_id)
            .all()
        )
        return [mappers.tracked_company_to_entity(row) for row in rows]

    def update(self, tracked_company: e.TrackedCompany) -> e.TrackedCompany:
        row = self._session.get(
            models.TrackedCompany, tracked_company.tracked_company_id
        )
        if row is None:
            raise KeyError(
                f"tracked company not found: {tracked_company.tracked_company_id}"
            )
        updated = mappers.tracked_company_to_model(tracked_company)
        for column in (
            "company_id",
            "security_id",
            "status",
            "lookback",
            "process_classes",
            "sync_frequency",
        ):
            setattr(row, column, getattr(updated, column))
        self._session.flush()
        return mappers.tracked_company_to_entity(row)

    def delete(self, tracked_company_id: str) -> None:
        row = self._session.get(models.TrackedCompany, tracked_company_id)
        if row is None:
            raise KeyError(f"tracked company not found: {tracked_company_id}")
        self._session.delete(row)
        self._session.flush()


class SourceAccessRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, source_access: e.SourceAccess) -> e.SourceAccess:
        row = mappers.source_access_to_model(source_access)
        self._session.add(row)
        self._session.flush()
        return mappers.source_access_to_entity(row)

    def get(self, source_access_id: str) -> Optional[e.SourceAccess]:
        row = self._session.get(models.SourceAccess, source_access_id)
        return mappers.source_access_to_entity(row) if row is not None else None

    def list_candidate_snapshots(
        self, *, provider: str, provider_interface: str, company_id: str
    ) -> list[dict[str, object]]:
        rows = (
            self._session.query(models.SourceAccess)
            .filter(
                models.SourceAccess.provider == provider,
                models.SourceAccess.provider_interface == provider_interface,
                models.SourceAccess.company_id == company_id,
                models.SourceAccess.status == "ok",
            )
            .order_by(models.SourceAccess.accessed_at.asc())
            .all()
        )
        snapshots: list[dict[str, object]] = []
        for row in rows:
            snapshot = row.result_snapshot
            if isinstance(snapshot, dict) and isinstance(
                snapshot.get("candidates"), list
            ):
                snapshots.append(snapshot)
        return snapshots

    def list_pending_download_candidates(
        self,
        *,
        provider: str,
        index_interfaces: Sequence[str],
        download_interface: str,
        max_retries: int,
        overlap_start: object,
    ) -> list[dict[str, object]]:
        overlap_date = _coerce_date(overlap_start)
        candidates = _candidate_rows(
            self._session.query(models.SourceAccess)
            .filter(
                models.SourceAccess.provider == provider,
                models.SourceAccess.provider_interface.in_(list(index_interfaces)),
                models.SourceAccess.status == "ok",
            )
            .order_by(models.SourceAccess.accessed_at.asc())
            .all()
        )
        pending: list[dict[str, object]] = []
        seen: set[str] = set()
        for candidate in candidates:
            provider_document_id = candidate.get("provider_document_id")
            if (
                not isinstance(provider_document_id, str)
                or provider_document_id in seen
            ):
                continue
            seen.add(provider_document_id)
            if self._terminal_download_failure(
                provider=provider,
                download_interface=download_interface,
                provider_document_id=provider_document_id,
                max_retries=max_retries,
            ):
                continue
            document = self._latest_document(
                provider=provider, provider_document_id=provider_document_id
            )
            if _should_download_candidate(
                candidate=candidate,
                document=document,
                overlap_start=overlap_date,
            ):
                pending.append(candidate)
        return pending

    def _terminal_download_failure(
        self,
        *,
        provider: str,
        download_interface: str,
        provider_document_id: str,
        max_retries: int,
    ) -> bool:
        rows = (
            self._session.query(models.SourceAccess)
            .filter(
                models.SourceAccess.provider == provider,
                models.SourceAccess.provider_interface == download_interface,
                models.SourceAccess.status == "failed",
                models.SourceAccess.query_params.op("->>")("provider_document_id")
                == provider_document_id,
            )
            .all()
        )
        if len(rows) >= max_retries:
            return True
        return any(_error_retryable(row.error) is False for row in rows)

    def _latest_document(
        self, *, provider: str, provider_document_id: str
    ) -> Optional[e.Document]:
        row = (
            self._session.query(models.Document)
            .filter(
                models.Document.provider == provider,
                models.Document.provider_document_id == provider_document_id,
            )
            .order_by(
                models.Document.created_at.desc(), models.Document.document_id.desc()
            )
            .first()
        )
        return mappers.document_to_entity(row) if row is not None else None


def _candidate_rows(rows: list[models.SourceAccess]) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for row in rows:
        snapshot = row.result_snapshot
        if not isinstance(snapshot, dict):
            continue
        raw_candidates = snapshot.get("candidates")
        if not isinstance(raw_candidates, list):
            continue
        candidates.extend(item for item in raw_candidates if isinstance(item, dict))
    return candidates


def _coerce_date(value: object) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(
        f"overlap_start must be date or ISO string, got {type(value).__name__}"
    )


def _error_retryable(error: str | None) -> bool | None:
    if not error:
        return None
    try:
        payload = json.loads(error)
    except json.JSONDecodeError:
        return None
    retryable = payload.get("retryable")
    return retryable if isinstance(retryable, bool) else None


def _should_download_candidate(
    *,
    candidate: dict[str, object],
    document: e.Document | None,
    overlap_start: date,
) -> bool:
    if document is None:
        return True
    if _has_correction_signal(candidate.get("title")):
        return True
    signature_state = _signature_state(
        candidate.get("file_signature_hint"),
        document.provider_metadata.get("file_signature"),
    )
    if signature_state == "different":
        return True
    # Inside the overlap verification window a matching provider signature is
    # not trusted (its size hint is not unit-stable); re-download and let the
    # raw_file_hash decide.
    announcement_date = _candidate_announcement_date(candidate)
    if announcement_date is not None and announcement_date >= overlap_start:
        return True
    return False


def _has_correction_signal(title: object) -> bool:
    if not isinstance(title, str):
        return False
    return any(signal in title for signal in ("更正", "修订", "更新后", "补充", "取消"))


def _candidate_announcement_date(candidate: Mapping[str, object]) -> date | None:
    value = candidate.get("announcement_date")
    if not isinstance(value, str):
        return None
    return date.fromisoformat(value)


def _signature_state(left: object, right: object) -> str:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return "unreliable"
    comparable = False
    for field in ("file_size", "etag", "last_modified"):
        left_value = left.get(field)
        right_value = right.get(field)
        if left_value is None or right_value is None:
            continue
        comparable = True
        if left_value != right_value:
            return "different"
    return "same" if comparable else "unreliable"


class SourceCheckpointRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, checkpoint: e.SourceCheckpoint) -> e.SourceCheckpoint:
        row = mappers.source_checkpoint_to_model(checkpoint)
        self._session.add(row)
        self._session.flush()
        return mappers.source_checkpoint_to_entity(row)

    def get(self, source_checkpoint_id: str) -> Optional[e.SourceCheckpoint]:
        row = self._session.get(models.SourceCheckpoint, source_checkpoint_id)
        return mappers.source_checkpoint_to_entity(row) if row is not None else None

    def get_by_scope(
        self, provider: str, scope_key: str
    ) -> Optional[e.SourceCheckpoint]:
        row = (
            self._session.query(models.SourceCheckpoint)
            .filter(
                models.SourceCheckpoint.provider == provider,
                models.SourceCheckpoint.scope_key == scope_key,
            )
            .one_or_none()
        )
        return mappers.source_checkpoint_to_entity(row) if row is not None else None

    def update(self, checkpoint: e.SourceCheckpoint) -> e.SourceCheckpoint:
        row = self._session.get(
            models.SourceCheckpoint, checkpoint.source_checkpoint_id
        )
        if row is None:
            raise KeyError(
                f"source checkpoint not found: {checkpoint.source_checkpoint_id}"
            )
        updated = mappers.source_checkpoint_to_model(checkpoint)
        row.provider = updated.provider
        row.scope_key = updated.scope_key
        row.cursor = updated.cursor
        row.updated_at = updated.updated_at
        self._session.flush()
        return mappers.source_checkpoint_to_entity(row)


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, document: e.Document) -> e.Document:
        row = mappers.document_to_model(document)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            detail = str(getattr(exc, "orig", exc))
            if "uq_document_provider_doc_hash" in detail:
                raise DocumentIdentityConflictError(
                    "document provider/document/hash already exists"
                ) from exc
            raise
        # Materialized classification stamps at insert, in the same
        # transaction; rules reloads refresh the rest by stamp mismatch
        # (design: retrieval-scale-hardening.md §3).
        refresh_document_classification(
            self._session.connection(), document_id=row.document_id
        )
        # The refresh runs as explicit SQL, outside ORM state tracking. Reload
        # before mapping so callers receive the materialized classification
        # written in this transaction rather than the pre-refresh NULLs.
        self._session.refresh(row)
        return mappers.document_to_entity(row)

    def get(self, document_id: str) -> Optional[e.Document]:
        row = self._session.get(models.Document, document_id)
        return mappers.document_to_entity(row) if row is not None else None

    def get_for_update(self, document_id: str) -> Optional[e.Document]:
        row = (
            self._session.query(models.Document)
            .filter(models.Document.document_id == document_id)
            .with_for_update()
            .one_or_none()
        )
        return mappers.document_to_entity(row) if row is not None else None

    def update(self, document: e.Document) -> e.Document:
        row = self._session.get(models.Document, document.document_id)
        if row is None:
            raise KeyError(f"document not found: {document.document_id}")
        updated = mappers.document_to_model(document)
        classification_inputs_changed = (
            row.title != updated.title
            or row.provider != updated.provider
            or row.provider_metadata != updated.provider_metadata
        )
        for column in (
            "company_id",
            "security_id",
            "source_access_id",
            "provider",
            "provider_document_id",
            "title",
            "announcement_date",
            "report_period",
            "raw_file_relpath",
            "raw_file_hash",
            "status",
            "provider_metadata",
            "current_processing_run_id",
            "supersedes_document_id",
            "correction_of_document_id",
        ):
            setattr(row, column, getattr(updated, column))
        self._session.flush()
        if classification_inputs_changed:
            # Materialized classification derives from title/provider/
            # metadata; a mutation without a re-stamp would survive under a
            # still-matching stamp forever (the loader only refreshes on
            # stamp mismatch).
            refresh_document_classification(
                self._session.connection(), document_id=row.document_id
            )
            self._session.refresh(row)
        return mappers.document_to_entity(row)

    def get_by_provider_document_and_hash(
        self, *, provider: str, provider_document_id: str, raw_file_hash: str
    ) -> Optional[e.Document]:
        row = (
            self._session.query(models.Document)
            .filter(
                models.Document.provider == provider,
                models.Document.provider_document_id == provider_document_id,
                models.Document.raw_file_hash == raw_file_hash,
            )
            .order_by(
                models.Document.created_at.desc(), models.Document.document_id.desc()
            )
            .first()
        )
        return mappers.document_to_entity(row) if row is not None else None

    def latest_by_provider_document(
        self, *, provider: str, provider_document_id: str
    ) -> Optional[e.Document]:
        row = (
            self._session.query(models.Document)
            .filter(
                models.Document.provider == provider,
                models.Document.provider_document_id == provider_document_id,
            )
            .order_by(
                models.Document.created_at.desc(), models.Document.document_id.desc()
            )
            .first()
        )
        return mappers.document_to_entity(row) if row is not None else None


class _ProcessingRunRepositoryBase:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run: e.ProcessingRun) -> e.ProcessingRun:
        row = mappers.processing_run_to_model(run)
        self._session.add(row)
        self._session.flush()
        return mappers.processing_run_to_entity(row)


def _remote_attempt_entity(row: models.RemoteParseAttempt) -> RemoteParseAttempt:
    if row.submitted_receipt_bytes is not None:
        encoded_submission = decode_checkpoint_receipt(
            bytes(row.submitted_receipt_bytes)
        )
        submission = encoded_submission.receipt
        if not isinstance(submission, AcceptedSubmissionReceipt) or (
            encoded_submission.sha256 != row.submitted_receipt_sha256
            or encoded_submission.byte_count != row.submitted_receipt_byte_count
            or submission.attempt_identity != row.attempt_id
            or submission.fence_identity != row.fence_identity
            or submission.source_pdf_sha256 != row.source_pdf_sha256
            or submission.client_submit_key != row.client_submit_key
            or submission.remote_task_identity != row.remote_task_identity
        ):
            raise RemoteParseCheckpointConflict(
                "stored accepted submission receipt identity drifted"
            )
    if row.terminal_receipt_bytes is not None:
        encoded = decode_terminal_receipt(bytes(row.terminal_receipt_bytes))
        terminal = encoded.receipt
        if (
            encoded.sha256 != row.terminal_receipt_sha256
            or encoded.byte_count != row.terminal_receipt_byte_count
            or terminal.attempt_identity != row.attempt_id
            or terminal.fence_identity != row.fence_identity
            or terminal.source_pdf_sha256 != row.source_pdf_sha256
            or terminal.artifact_owner_identity != row.result_owner_identity
            or terminal.artifact_sha256 != row.result_artifact_sha256
            or terminal.artifact_byte_count != row.result_artifact_bytes
        ):
            raise RemoteParseCheckpointConflict("stored terminal receipt identity drifted")
    if row.local_receipt_bytes is not None:
        encoded_local = decode_checkpoint_receipt(bytes(row.local_receipt_bytes))
        local = encoded_local.receipt
        if not isinstance(local, LocalMaterializationReceipt) or (
            encoded_local.sha256 != row.local_receipt_sha256
            or encoded_local.byte_count != row.local_receipt_byte_count
            or local.attempt_identity != row.attempt_id
            or local.fence_identity != row.fence_identity
            or not 1 <= local.claim_generation <= row.claim_generation
            or local.source_pdf_sha256 != row.source_pdf_sha256
            or local.parser_target_sha256 != row.parser_target_sha256
            or local.terminal_receipt_sha256 != row.terminal_receipt_sha256
            or local.artifact_owner_identity != row.result_owner_identity
            or local.artifact_sha256 != row.result_artifact_sha256
            or local.artifact_byte_count != row.result_artifact_bytes
        ):
            raise RemoteParseCheckpointConflict("stored local receipt identity drifted")
    if row.failure_receipt_bytes is not None:
        encoded_failure = decode_checkpoint_receipt(bytes(row.failure_receipt_bytes))
        failure = encoded_failure.receipt
        if not isinstance(failure, FailureReceipt) or (
            encoded_failure.sha256 != row.failure_receipt_sha256
            or encoded_failure.byte_count != row.failure_receipt_byte_count
            or failure.attempt_identity != row.attempt_id
            or failure.fence_identity != row.fence_identity
            or failure.stage != row.failure_stage
            or failure.remote_task_identity != row.remote_task_identity
            or not 1 <= failure.claim_generation <= row.claim_generation
            or (
                failure.stage == "local"
                and failure.terminal_receipt_sha256 != row.terminal_receipt_sha256
            )
        ):
            raise RemoteParseCheckpointConflict("stored failure receipt identity drifted")
    return RemoteParseAttempt(
        attempt_id=row.attempt_id, processing_run_id=row.processing_run_id,
        document_id=row.document_id, attempt_generation=row.attempt_generation,
        fence_identity=row.fence_identity, source_pdf_sha256=row.source_pdf_sha256,
        parser_target_sha256=row.parser_target_sha256, request_sha256=row.request_sha256,
        runtime_epoch_sha256=row.runtime_epoch_sha256, client_submit_key=row.client_submit_key,
        checkpoint_contract_version=row.checkpoint_contract_version,
        state=cast(Any, row.state), is_current=row.is_current, row_version=row.row_version,
        remote_task_identity=row.remote_task_identity,
        submitted_receipt_sha256=row.submitted_receipt_sha256,
        submitted_receipt_bytes=(
            None if row.submitted_receipt_bytes is None
            else bytes(row.submitted_receipt_bytes)
        ),
        submitted_receipt_byte_count=row.submitted_receipt_byte_count,
        terminal_receipt_sha256=row.terminal_receipt_sha256,
        terminal_receipt_bytes=None if row.terminal_receipt_bytes is None else bytes(row.terminal_receipt_bytes),
        terminal_receipt_byte_count=row.terminal_receipt_byte_count,
        result_owner_identity=row.result_owner_identity,
        result_artifact_sha256=row.result_artifact_sha256,
        result_artifact_bytes=row.result_artifact_bytes,
        claim_generation=row.claim_generation,
        claim_owner_identity=row.claim_owner_identity,
        claim_lease_until=row.claim_lease_until,
        local_receipt_sha256=row.local_receipt_sha256,
        local_receipt_bytes=None if row.local_receipt_bytes is None else bytes(row.local_receipt_bytes),
        local_receipt_byte_count=row.local_receipt_byte_count,
        failure_receipt_sha256=row.failure_receipt_sha256,
        failure_receipt_bytes=None if row.failure_receipt_bytes is None else bytes(row.failure_receipt_bytes),
        failure_receipt_byte_count=row.failure_receipt_byte_count,
        failure_stage=cast(Any, row.failure_stage),
        created_at=row.created_at, updated_at=row.updated_at,
    )


class RemoteParseAttemptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self, attempt: RemoteParseAttempt,
        submission_secret: RemoteParseResumeSecret,
    ) -> RemoteParseAttempt:
        if not (
            attempt.checkpoint_contract_version == 2
            and
            attempt.state == "prepared"
            and attempt.is_current
            and attempt.row_version == 0
            and attempt.remote_task_identity is None
            and attempt.submitted_receipt_bytes is None
            and attempt.terminal_receipt_sha256 is None
            and attempt.terminal_receipt_bytes is None
            and attempt.terminal_receipt_byte_count is None
            and attempt.result_owner_identity is None
            and attempt.result_artifact_sha256 is None
            and attempt.result_artifact_bytes is None
            and attempt.claim_generation == 0
            and attempt.claim_owner_identity is None
            and attempt.claim_lease_until is None
            and attempt.local_receipt_bytes is None
            and attempt.failure_receipt_bytes is None
        ):
            raise ValueError("new remote parse attempt must have canonical prepared shape")
        if (
            not isinstance(submission_secret, RemoteParseResumeSecret)
            or submission_secret.attempt_id != attempt.attempt_id
            or submission_secret.secret_kind != "prepared_reconcile"
            or submission_secret.secret_contract_version != 2
        ):
            raise ValueError("prepared attempt requires its exact submission secret")
        prepared = decode_checkpoint_receipt(submission_secret.token_bytes).receipt
        if not isinstance(prepared, PreparedReconcileReceipt) or (
            prepared.attempt_identity != attempt.attempt_id
            or prepared.fence_identity != attempt.fence_identity
            or prepared.source_pdf_sha256 != attempt.source_pdf_sha256
            or prepared.client_submit_key != attempt.client_submit_key
            or prepared.parser_target_sha256 != attempt.parser_target_sha256
            or prepared.request_sha256 != attempt.request_sha256
            or prepared.runtime_epoch_sha256 != attempt.runtime_epoch_sha256
        ):
            raise ValueError("prepared reconcile receipt drifted from attempt")
        row = models.RemoteParseAttempt(**{
            name: getattr(attempt, name) for name in (
                "attempt_id", "processing_run_id", "document_id", "attempt_generation",
                "fence_identity", "source_pdf_sha256", "parser_target_sha256",
                "request_sha256", "runtime_epoch_sha256", "client_submit_key", "state",
                "checkpoint_contract_version",
                "is_current", "row_version", "remote_task_identity", "terminal_receipt_sha256",
                "submitted_receipt_sha256", "submitted_receipt_bytes",
                "submitted_receipt_byte_count",
                "terminal_receipt_bytes", "terminal_receipt_byte_count", "result_owner_identity",
                "result_artifact_sha256", "result_artifact_bytes",
            )
        })
        with self._session.begin_nested():
            self._session.add(row)
            self._session.flush()
            self._put_secret(submission_secret)
        return _remote_attempt_entity(row)

    def get(self, attempt_id: str) -> Optional[RemoteParseAttempt]:
        row = self._session.get(models.RemoteParseAttempt, attempt_id)
        return None if row is None else _remote_attempt_entity(row)

    def get_current_for_document(self, document_id: str) -> Optional[RemoteParseAttempt]:
        row = self._session.query(models.RemoteParseAttempt).filter(
            models.RemoteParseAttempt.document_id == document_id,
            models.RemoteParseAttempt.is_current.is_(True),
        ).one_or_none()
        return None if row is None else _remote_attempt_entity(row)

    def list_recoverable(
        self, *, after_attempt_id: str | None, limit: int
    ) -> list[RemoteParseAttempt]:
        if limit < 1 or limit > 1000:
            raise ValueError("recovery page limit is outside 1..1000")
        unsupported = self._session.query(models.RemoteParseAttempt).filter(
            models.RemoteParseAttempt.is_current.is_(True),
            models.RemoteParseAttempt.checkpoint_contract_version != 2,
        ).order_by(models.RemoteParseAttempt.attempt_id).first()
        if unsupported is not None:
            raise RemoteParseCheckpointConflict(
                "unsupported current v1 checkpoint blocks staged admission: "
                f"{unsupported.attempt_id}/{unsupported.state}"
            )
        query = self._session.query(models.RemoteParseAttempt).filter(
            models.RemoteParseAttempt.is_current.is_(True)
        )
        if after_attempt_id is not None:
            query = query.filter(models.RemoteParseAttempt.attempt_id > after_attempt_id)
        rows = query.order_by(models.RemoteParseAttempt.attempt_id).limit(limit).all()
        return [_remote_attempt_entity(row) for row in rows]

    def claim_recovery(
        self, *, attempt_id: str, fence_identity: str, expected_version: int,
        owner_identity: str, lease_seconds: int,
    ) -> RemoteParseAttempt:
        if (
            not owner_identity
            or len(owner_identity) > 128
            or isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 300
        ):
            raise ValueError("recovery claim identity/lease is invalid")
        row = self._session.execute(
            sa.select(models.RemoteParseAttempt, sa.func.now())
            .where(models.RemoteParseAttempt.attempt_id == attempt_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise RemoteParseCheckpointConflict("recovery claim attempt is absent")
        current, database_now = row
        if current.fence_identity != fence_identity or not current.is_current:
            raise RemoteParseCheckpointConflict("recovery claim lost fence/version/current CAS")
        if (
            current.claim_owner_identity == owner_identity
            and current.claim_lease_until is not None
            and current.claim_lease_until > database_now
        ):
            return _remote_attempt_entity(current)
        if current.row_version != expected_version:
            raise RemoteParseCheckpointConflict("recovery claim lost fence/version/current CAS")
        if current.checkpoint_contract_version != 2:
            raise RemoteParseCheckpointConflict("v1 checkpoint cannot acquire a v2 recovery claim")
        if (
            current.claim_owner_identity is not None
            and current.claim_lease_until is not None
            and current.claim_lease_until > database_now
        ):
            raise RemoteParseCheckpointConflict("recovery attempt has a live foreign lease")
        won = self._session.execute(
            sa.update(models.RemoteParseAttempt).where(
                models.RemoteParseAttempt.attempt_id == attempt_id,
                models.RemoteParseAttempt.fence_identity == fence_identity,
                models.RemoteParseAttempt.row_version == expected_version,
                models.RemoteParseAttempt.is_current.is_(True),
                models.RemoteParseAttempt.checkpoint_contract_version == 2,
            ).values(
                claim_generation=models.RemoteParseAttempt.claim_generation + 1,
                claim_owner_identity=owner_identity,
                claim_lease_until=sa.func.now() + sa.func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds),
                row_version=expected_version + 1,
                updated_at=sa.func.now(),
            ).returning(models.RemoteParseAttempt.attempt_id)
        ).scalar_one_or_none()
        if won is None:
            raise RemoteParseCheckpointConflict("recovery claim lost fence/version/live lease CAS")
        claimed_row = self._session.get(
            models.RemoteParseAttempt, attempt_id, populate_existing=True
        )
        assert claimed_row is not None
        return _remote_attempt_entity(claimed_row)

    def renew_recovery_claim(
        self, *, attempt_id: str, fence_identity: str, owner_identity: str,
        claim_generation: int, lease_seconds: int,
    ) -> RemoteParseAttempt:
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 300:
            raise ValueError("recovery renewal lease is outside 1..300 seconds")
        won = self._session.execute(
            sa.update(models.RemoteParseAttempt).where(
                models.RemoteParseAttempt.attempt_id == attempt_id,
                models.RemoteParseAttempt.fence_identity == fence_identity,
                models.RemoteParseAttempt.is_current.is_(True),
                models.RemoteParseAttempt.claim_owner_identity == owner_identity,
                models.RemoteParseAttempt.claim_generation == claim_generation,
                models.RemoteParseAttempt.claim_lease_until > sa.func.now(),
            ).values(
                claim_lease_until=sa.func.now()
                + sa.func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds),
                updated_at=sa.func.now(),
            ).returning(models.RemoteParseAttempt.attempt_id)
        ).scalar_one_or_none()
        if won is None:
            raise RemoteParseCheckpointConflict("recovery claim renewal lost live claim CAS")
        row = self._session.get(models.RemoteParseAttempt, attempt_id, populate_existing=True)
        assert row is not None
        return _remote_attempt_entity(row)

    def checkpoint_submitted(self, *, attempt_id: str, fence_identity: str,
                             expected_version: int,
                             remote_task_identity: str,
                             receipt: EncodedCheckpointReceipt,
                             accepted_secret: RemoteParseResumeSecret,
                             claim_owner_identity: str,
                             claim_generation: int) -> RemoteParseAttempt:
        if not remote_task_identity or len(remote_task_identity) > 1024:
            raise ValueError("invalid remote task identity")
        validated = decode_checkpoint_receipt(receipt.exact_bytes)
        submission = validated.receipt
        if not isinstance(submission, AcceptedSubmissionReceipt) or validated != receipt:
            raise RemoteParseCheckpointConflict(
                "accepted submission receipt is not self-consistent"
            )
        if (
            submission.attempt_identity != attempt_id
            or submission.fence_identity != fence_identity
            or submission.remote_task_identity != remote_task_identity
        ):
            raise RemoteParseCheckpointConflict(
                "accepted submission receipt lost attempt/task fence"
            )
        if (
            not isinstance(accepted_secret, RemoteParseResumeSecret)
            or accepted_secret.attempt_id != attempt_id
            or accepted_secret.secret_kind != "accepted_submission"
            or accepted_secret.secret_contract_version != 2
            or accepted_secret.token_sha256 != submission.resume_token_sha256
        ):
            raise ValueError("submitted checkpoint requires accepted-task secret")
        with self._session.begin_nested():
            prepared_secret = self.get_secret(attempt_id, "prepared_reconcile")
            if prepared_secret is None:
                raise RemoteParseCheckpointConflict(
                    "prepared reconcile evidence is absent"
                )
            prepared = decode_checkpoint_receipt(prepared_secret.token_bytes).receipt
            if not isinstance(prepared, PreparedReconcileReceipt) or (
                prepared.submission_epoch_unix != submission.submission_epoch_unix
                or prepared.client_submit_key != submission.client_submit_key
                or prepared.source_pdf_sha256 != submission.source_pdf_sha256
                or prepared.attempt_identity != submission.attempt_identity
                or prepared.fence_identity != submission.fence_identity
            ):
                raise RemoteParseCheckpointConflict(
                    "accepted submission drifted from prepared reconcile evidence"
                )
            self._put_secret(accepted_secret)
            won = self._session.execute(
                sa.update(models.RemoteParseAttempt).where(
                models.RemoteParseAttempt.attempt_id == attempt_id,
                models.RemoteParseAttempt.fence_identity == fence_identity,
                models.RemoteParseAttempt.state == "reconciling",
                models.RemoteParseAttempt.row_version == expected_version,
                models.RemoteParseAttempt.remote_task_identity.is_(None),
                models.RemoteParseAttempt.claim_owner_identity == claim_owner_identity,
                models.RemoteParseAttempt.claim_generation == claim_generation,
                models.RemoteParseAttempt.claim_lease_until > sa.func.now(),
                models.RemoteParseAttempt.source_pdf_sha256
                == submission.source_pdf_sha256,
                models.RemoteParseAttempt.client_submit_key
                == submission.client_submit_key,
                ).values(
                    state="submitted",
                    row_version=expected_version + 1,
                    remote_task_identity=remote_task_identity,
                    submitted_receipt_sha256=validated.sha256,
                    submitted_receipt_bytes=validated.exact_bytes,
                    submitted_receipt_byte_count=validated.byte_count,
                    updated_at=sa.func.now(),
                ).returning(models.RemoteParseAttempt.attempt_id)
            ).scalar_one_or_none()
            if won is None:
                raise RemoteParseCheckpointConflict(
                    "remote submit checkpoint lost fence/version CAS"
                )
        row = self._session.get(models.RemoteParseAttempt, attempt_id, populate_existing=True)
        assert row is not None
        return _remote_attempt_entity(row)

    def transition(self, *, attempt_id: str, fence_identity: str, expected_state: str,
                   expected_version: int, next_state: str,
                   claim_owner_identity: str, claim_generation: int) -> RemoteParseAttempt:
        if (expected_state, next_state) not in ALLOWED_TRANSITIONS:
            raise ValueError("remote parse state transition is not allowed")
        values: dict[str, object] = {
            "state": next_state, "row_version": expected_version + 1,
            "updated_at": sa.func.now(),
        }
        if next_state in {"acked", "remote_failed", "local_failed", "superseded"}:
            values["is_current"] = False
            values["claim_owner_identity"] = None
            values["claim_lease_until"] = None
        result = self._session.execute(
            sa.update(models.RemoteParseAttempt).where(
                models.RemoteParseAttempt.attempt_id == attempt_id,
                models.RemoteParseAttempt.fence_identity == fence_identity,
                models.RemoteParseAttempt.state == expected_state,
                models.RemoteParseAttempt.row_version == expected_version,
                models.RemoteParseAttempt.claim_owner_identity == claim_owner_identity,
                models.RemoteParseAttempt.claim_generation == claim_generation,
                models.RemoteParseAttempt.claim_lease_until > sa.func.now(),
            ).values(**values).returning(models.RemoteParseAttempt.attempt_id)
        ).scalar_one_or_none()
        if result is None:
            raise RemoteParseCheckpointConflict("remote parse transition lost fence/version CAS")
        row = self._session.get(models.RemoteParseAttempt, attempt_id, populate_existing=True)
        assert row is not None
        return _remote_attempt_entity(row)

    def checkpoint_terminal(self, *, attempt_id: str, fence_identity: str,
                            expected_version: int, remote_task_identity: str,
                            receipt: EncodedTerminalReceipt,
                            terminal_secret: RemoteParseResumeSecret,
                            claim_owner_identity: str,
                            claim_generation: int) -> RemoteParseAttempt:
        validated = decode_terminal_receipt(receipt.exact_bytes)
        if validated != receipt:
            raise RemoteParseCheckpointConflict("terminal receipt envelope is not self-consistent")
        terminal = validated.receipt
        if terminal.attempt_identity != attempt_id or terminal.fence_identity != fence_identity:
            raise RemoteParseCheckpointConflict("terminal receipt attempt/fence drifted")
        if (
            not isinstance(terminal_secret, RemoteParseResumeSecret)
            or terminal_secret.attempt_id != attempt_id
            or terminal_secret.secret_kind != "terminal"
            or terminal_secret.secret_contract_version != 2
            or terminal_secret.token_sha256 != terminal.resume_token_sha256
        ):
            raise RemoteParseCheckpointConflict(
                "terminal receipt/private resume token identity drifted"
            )
        with self._session.begin_nested():
            self._put_secret(terminal_secret)
            values = {
                "state": "remote_terminal", "row_version": expected_version + 1,
                "terminal_receipt_sha256": validated.sha256,
                "terminal_receipt_bytes": validated.exact_bytes,
                "terminal_receipt_byte_count": validated.byte_count,
                "result_owner_identity": terminal.artifact_owner_identity,
                "result_artifact_sha256": terminal.artifact_sha256,
                "result_artifact_bytes": terminal.artifact_byte_count,
                "updated_at": sa.func.now(),
            }
            won = self._session.execute(
                sa.update(models.RemoteParseAttempt).where(
                    models.RemoteParseAttempt.attempt_id == attempt_id,
                    models.RemoteParseAttempt.fence_identity == fence_identity,
                    models.RemoteParseAttempt.state == "submitted",
                    models.RemoteParseAttempt.row_version == expected_version,
                    models.RemoteParseAttempt.remote_task_identity == remote_task_identity,
                    models.RemoteParseAttempt.source_pdf_sha256 == terminal.source_pdf_sha256,
                    models.RemoteParseAttempt.claim_owner_identity == claim_owner_identity,
                    models.RemoteParseAttempt.claim_generation == claim_generation,
                    models.RemoteParseAttempt.claim_lease_until > sa.func.now(),
                ).values(**values).returning(models.RemoteParseAttempt.attempt_id)
            ).scalar_one_or_none()
            row = self._session.get(
                models.RemoteParseAttempt, attempt_id, populate_existing=True
            )
            if row is None:
                raise RemoteParseCheckpointConflict("terminal checkpoint attempt is absent")
            if won is None and not (
                row.fence_identity == fence_identity
                and row.state in {
                    "remote_terminal", "materializing", "local_materialized",
                    "finish_committed", "acked", "local_failed", "superseded",
                }
                and row.remote_task_identity == remote_task_identity
                and row.claim_owner_identity == claim_owner_identity
                and row.claim_generation == claim_generation
                and row.terminal_receipt_sha256 == validated.sha256
                and bytes(row.terminal_receipt_bytes or b"") == validated.exact_bytes
            ):
                raise RemoteParseCheckpointConflict(
                    "conflicting terminal checkpoint lost first-terminal-wins"
                )
        return _remote_attempt_entity(row)

    def checkpoint_local(
        self, *, attempt_id: str, fence_identity: str, expected_version: int,
        claim_owner_identity: str, claim_generation: int,
        receipt: EncodedCheckpointReceipt,
    ) -> RemoteParseAttempt:
        validated = decode_checkpoint_receipt(receipt.exact_bytes)
        local = validated.receipt
        if not isinstance(local, LocalMaterializationReceipt) or validated != receipt:
            raise RemoteParseCheckpointConflict("local receipt is not self-consistent")
        if (
            local.attempt_identity != attempt_id
            or local.fence_identity != fence_identity
            or local.claim_generation != claim_generation
        ):
            raise RemoteParseCheckpointConflict("local receipt lost attempt/claim fence")
        won = self._session.execute(
            sa.update(models.RemoteParseAttempt).where(
                models.RemoteParseAttempt.attempt_id == attempt_id,
                models.RemoteParseAttempt.fence_identity == fence_identity,
                models.RemoteParseAttempt.state == "materializing",
                models.RemoteParseAttempt.row_version == expected_version,
                models.RemoteParseAttempt.claim_owner_identity == claim_owner_identity,
                models.RemoteParseAttempt.claim_generation == claim_generation,
                models.RemoteParseAttempt.claim_lease_until > sa.func.now(),
                models.RemoteParseAttempt.source_pdf_sha256 == local.source_pdf_sha256,
                models.RemoteParseAttempt.parser_target_sha256 == local.parser_target_sha256,
                models.RemoteParseAttempt.terminal_receipt_sha256
                == local.terminal_receipt_sha256,
                models.RemoteParseAttempt.result_owner_identity
                == local.artifact_owner_identity,
                models.RemoteParseAttempt.result_artifact_sha256 == local.artifact_sha256,
                models.RemoteParseAttempt.result_artifact_bytes == local.artifact_byte_count,
            ).values(
                state="local_materialized",
                row_version=expected_version + 1,
                local_receipt_sha256=validated.sha256,
                local_receipt_bytes=validated.exact_bytes,
                local_receipt_byte_count=validated.byte_count,
                updated_at=sa.func.now(),
            ).returning(models.RemoteParseAttempt.attempt_id)
        ).scalar_one_or_none()
        if won is None:
            raise RemoteParseCheckpointConflict("local checkpoint lost attempt/claim CAS")
        row = self._session.get(models.RemoteParseAttempt, attempt_id, populate_existing=True)
        assert row is not None
        return _remote_attempt_entity(row)

    def fail_run_and_checkpoint(
        self, *, document_id: str, processing_run_id: str,
        attempt_id: str, fence_identity: str, expected_state: str,
        expected_version: int, claim_owner_identity: str, claim_generation: int,
        receipt: EncodedCheckpointReceipt,
    ) -> RemoteParseAttempt:
        validated = decode_checkpoint_receipt(receipt.exact_bytes)
        failure = validated.receipt
        if not isinstance(failure, FailureReceipt) or validated != receipt:
            raise RemoteParseCheckpointConflict("failure receipt is not self-consistent")
        if (
            failure.attempt_identity != attempt_id
            or failure.fence_identity != fence_identity
            or failure.claim_generation != claim_generation
        ):
            raise RemoteParseCheckpointConflict("failure receipt lost attempt/claim fence")
        expected_by_class = {
            # This edge is application-owned and may only consume the typed
            # local preflight failure emitted before any remote IO.  A failed
            # lookup/POST/reconcile remains prepared and is not routed here.
            "pre_submission": {"prepared"},
            "remote_terminal": {"submitted"},
            "local_materialization": {"remote_terminal", "materializing"},
        }
        if expected_state not in expected_by_class[failure.error_class]:
            raise ValueError("failure receipt class disagrees with source state")
        next_state = {
            "pre_submission": "pre_submission_failed",
            "remote_terminal": "remote_failure_committed",
            "local_materialization": "local_failure_committed",
        }[failure.error_class]
        values: dict[str, object] = {
            "state": next_state,
            "row_version": expected_version + 1,
            "failure_receipt_sha256": validated.sha256,
            "failure_receipt_bytes": validated.exact_bytes,
            "failure_receipt_byte_count": validated.byte_count,
            "failure_stage": failure.stage,
            "updated_at": sa.func.now(),
        }
        if next_state == "pre_submission_failed":
            values.update(
                is_current=False,
                claim_owner_identity=None,
                claim_lease_until=None,
            )
        acquire_document_xact_lock(self._session, document_id)
        document_row = self._session.execute(
            sa.select(models.Document).where(
                models.Document.document_id == document_id,
            ).with_for_update()
        ).scalar_one_or_none()
        run_row = self._session.execute(
            sa.select(models.ProcessingRun).where(
                models.ProcessingRun.processing_run_id == processing_run_id,
                models.ProcessingRun.document_id == document_id,
            ).with_for_update()
        ).scalar_one_or_none()
        if document_row is None or run_row is None or run_row.status != "running":
            raise RemoteParseCheckpointConflict(
                "failure checkpoint lost document/running-run first-terminal-wins"
            )
        error = {
            "stage": failure.error_stage,
            "error_code": failure.error_code,
            "retryable": failure.retryable,
            "retry_budget_class": failure.retry_budget_class,
            "message": failure.message,
        }
        finished_at = datetime.now(timezone.utc)
        with self._session.begin_nested():
            won = self._session.execute(
                sa.update(models.RemoteParseAttempt).where(
                models.RemoteParseAttempt.attempt_id == attempt_id,
                models.RemoteParseAttempt.processing_run_id == processing_run_id,
                models.RemoteParseAttempt.document_id == document_id,
                models.RemoteParseAttempt.fence_identity == fence_identity,
                models.RemoteParseAttempt.state == expected_state,
                models.RemoteParseAttempt.row_version == expected_version,
                models.RemoteParseAttempt.claim_owner_identity == claim_owner_identity,
                models.RemoteParseAttempt.claim_generation == claim_generation,
                models.RemoteParseAttempt.claim_lease_until > sa.func.now(),
                models.RemoteParseAttempt.remote_task_identity
                == failure.remote_task_identity,
                sa.or_(
                    sa.true() if failure.stage == "remote" else sa.false(),
                    models.RemoteParseAttempt.terminal_receipt_sha256
                    == failure.terminal_receipt_sha256,
                ),
                ).values(**values).returning(models.RemoteParseAttempt.attempt_id)
            ).scalar_one_or_none()
            if won is None:
                raise RemoteParseCheckpointConflict(
                    "failure checkpoint lost attempt/claim CAS"
                )
            run_row.status = "failed"
            run_row.finished_at = finished_at
            run_row.error = error
            if document_row.current_processing_run_id is None:
                document_row.status = "parse_failed"
            OutboxRepository(self._session).add(
                outbox_events.processing_run_failed(
                    document_id=document_id,
                    processing_run_id=processing_run_id,
                    error=error,
                    occurred_at=finished_at,
                )
            )
        row = self._session.get(models.RemoteParseAttempt, attempt_id, populate_existing=True)
        assert row is not None
        return _remote_attempt_entity(row)

    def finish_run_and_checkpoint(
        self, *, finished_run: e.ProcessingRun, attempt_id: str,
        fence_identity: str, expected_version: int, claim_owner_identity: str,
        claim_generation: int,
    ) -> RemoteParseAttempt:
        if finished_run.status != "succeeded":
            raise ValueError("finish checkpoint requires a succeeded processing run")
        acquire_document_xact_lock(self._session, finished_run.document_id)
        document_row = self._session.execute(
            sa.select(models.Document).where(
                models.Document.document_id == finished_run.document_id,
            ).with_for_update()
        ).scalar_one_or_none()
        run_row = self._session.execute(
            sa.select(models.ProcessingRun).where(
                models.ProcessingRun.processing_run_id == finished_run.processing_run_id,
                models.ProcessingRun.document_id == finished_run.document_id,
            ).with_for_update()
        ).scalar_one_or_none()
        if document_row is None or run_row is None or run_row.status != "running":
            raise RemoteParseCheckpointConflict(
                "finish checkpoint lost document/running-run first-terminal-wins"
            )
        current = self._session.execute(
            sa.select(models.RemoteParseAttempt).where(
                models.RemoteParseAttempt.attempt_id == attempt_id,
            ).with_for_update()
        ).scalar_one_or_none()
        if current is None or current.processing_run_id != finished_run.processing_run_id:
            raise RemoteParseCheckpointConflict("finish checkpoint run ownership drifted")
        with self._session.begin_nested():
            ProcessingRunRepository(self._session).update(finished_run)
            if document_row.current_processing_run_id is None:
                document_row.status = "parsed"
            won = self._session.execute(
                sa.update(models.RemoteParseAttempt).where(
                models.RemoteParseAttempt.attempt_id == attempt_id,
                models.RemoteParseAttempt.fence_identity == fence_identity,
                models.RemoteParseAttempt.state == "local_materialized",
                models.RemoteParseAttempt.row_version == expected_version,
                models.RemoteParseAttempt.claim_owner_identity == claim_owner_identity,
                models.RemoteParseAttempt.claim_generation == claim_generation,
                models.RemoteParseAttempt.claim_lease_until > sa.func.now(),
                ).values(
                    state="finish_committed",
                    row_version=expected_version + 1,
                    updated_at=sa.func.now(),
                ).returning(models.RemoteParseAttempt.attempt_id)
            ).scalar_one_or_none()
            if won is None:
                raise RemoteParseCheckpointConflict(
                    "finish checkpoint lost attempt/claim CAS"
                )
        row = self._session.get(models.RemoteParseAttempt, attempt_id, populate_existing=True)
        assert row is not None
        return _remote_attempt_entity(row)

    def _put_secret(self, secret: RemoteParseResumeSecret) -> None:
        inserted = self._session.execute(
            pg_insert(models.RemoteParseResumeSecret)
            .values(
                attempt_id=secret.attempt_id,
                secret_kind=secret.secret_kind,
                token_bytes=secret.token_bytes,
                token_sha256=secret.token_sha256,
                token_byte_count=secret.token_byte_count,
                secret_contract_version=secret.secret_contract_version,
            )
            .on_conflict_do_nothing(
                index_elements=["attempt_id", "secret_kind"]
            )
            .returning(models.RemoteParseResumeSecret.attempt_id)
        ).scalar_one_or_none()
        if inserted is None:
            existing = self._session.get(
                models.RemoteParseResumeSecret,
                (secret.attempt_id, secret.secret_kind),
                populate_existing=True,
            )
            if existing is None or (
                bytes(existing.token_bytes) != secret.token_bytes
                or existing.token_sha256 != secret.token_sha256
                or existing.token_byte_count != secret.token_byte_count
                or existing.secret_contract_version != secret.secret_contract_version
            ):
                raise RemoteParseCheckpointConflict(
                    "conflicting private resume token lost first-write-wins"
                )

    def get_secret(self, attempt_id: str, secret_kind: str) -> Optional[RemoteParseResumeSecret]:
        row = self._session.get(models.RemoteParseResumeSecret, (attempt_id, secret_kind))
        if row is None:
            return None
        return RemoteParseResumeSecret(
            attempt_id=row.attempt_id, secret_kind=cast(Any, row.secret_kind),
            token_bytes=bytes(row.token_bytes), token_sha256=row.token_sha256,
            token_byte_count=row.token_byte_count,
            secret_contract_version=row.secret_contract_version,
        )


class ProcessingRunRepository(_ProcessingRunRepositoryBase):
    """Complete the existing repository after the private checkpoint adapter."""

    def get(self, processing_run_id: str) -> Optional[e.ProcessingRun]:
        row = self._session.get(models.ProcessingRun, processing_run_id)
        return mappers.processing_run_to_entity(row) if row is not None else None

    def latest_succeeded_provider_run_for_document(
        self, document_id: str
    ) -> Optional[e.ProcessingRun]:
        row = (
            self._session.query(models.ProcessingRun)
            .filter(
                models.ProcessingRun.document_id == document_id,
                # Provider rebuild runs alias one self-owned parse artifact
                # and remain valid deterministic build candidates.
                models.ProcessingRun.run_kind.in_(("parse", "rebuild_units")),
                models.ProcessingRun.status == "succeeded",
                models.ProcessingRun.provider_document_relpath.isnot(None),
                models.ProcessingRun.normalized_ir_relpath.is_(None),
            )
            .order_by(
                models.ProcessingRun.started_at.desc().nullslast(),
                models.ProcessingRun.processing_run_id.desc(),
            )
            .first()
        )
        return mappers.processing_run_to_entity(row) if row is not None else None

    def update(self, run: e.ProcessingRun) -> e.ProcessingRun:
        row = self._session.get(models.ProcessingRun, run.processing_run_id)
        if row is None:
            raise KeyError(f"processing_run not found: {run.processing_run_id}")
        updated = mappers.processing_run_to_model(run)
        for column in (
            "document_id",
            "artifact_owner_processing_run_id",
            "run_kind",
            "status",
            "parser_name",
            "parser_version",
            "parser_backend",
            "parser_method",
            "parser_language",
            "parser_target_identity",
            "search_projection_error",
            "input_raw_file_hash",
            "parser_artifact_relpath",
            "artifact_hash",
            "normalized_ir_relpath",
            "provider_document_relpath",
            "document_units_relpath",
            "semantic_route_receipts_hash",
            "semantic_route_receipts_relpath",
            "semantic_route_receipts_contract_version",
            "semantic_adjudication_status",
            "semantic_degraded_unit_count",
            "semantic_failover_group_count",
            "semantic_adjudication_summary",
            "content_hash_aggregate",
            "structure_hash",
            "builder_rules_version",
            "is_active",
            "unit_build_status",
            "unit_build_error",
            "unit_build_attempt_count",
            "unit_built_at",
            "started_at",
            "finished_at",
            "error",
        ):
            setattr(row, column, getattr(updated, column))
        self._session.flush()
        return mappers.processing_run_to_entity(row)


class DocumentUnitRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, unit: e.DocumentUnit) -> e.DocumentUnit:
        row = mappers.document_unit_to_model(unit)
        self._session.add(row)
        self._session.flush()
        return mappers.document_unit_to_entity(row)

    def add_many(self, units: list[e.DocumentUnit]) -> list[e.DocumentUnit]:
        rows = [mappers.document_unit_to_model(unit) for unit in units]
        self._session.add_all(rows)
        self._session.flush()
        return [mappers.document_unit_to_entity(row) for row in rows]

    def get(self, asset_id: str) -> Optional[e.DocumentUnit]:
        row = self._session.get(models.DocumentUnit, asset_id)
        return mappers.document_unit_to_entity(row) if row is not None else None

    def list_by_processing_run(self, processing_run_id: str) -> list[e.DocumentUnit]:
        rows = (
            self._session.query(models.DocumentUnit)
            .filter(models.DocumentUnit.processing_run_id == processing_run_id)
            .order_by(models.DocumentUnit.order_index, models.DocumentUnit.asset_id)
            .all()
        )
        return [mappers.document_unit_to_entity(row) for row in rows]

    def list_by_document_active(self, document_id: str) -> list[e.DocumentUnit]:
        rows = (
            self._session.query(models.DocumentUnit)
            .join(
                models.ProcessingRun,
                models.DocumentUnit.processing_run_id
                == models.ProcessingRun.processing_run_id,
            )
            .filter(
                models.DocumentUnit.document_id == document_id,
                models.ProcessingRun.is_active.is_(True),
            )
            .order_by(models.DocumentUnit.order_index, models.DocumentUnit.asset_id)
            .all()
        )
        return [mappers.document_unit_to_entity(row) for row in rows]


class OutboxRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, event: e.OutboxEvent) -> e.OutboxEvent:
        # BIGSERIAL assigns seq at INSERT, not at commit: two concurrent
        # publishers can commit out of seq order, and a `seq > cursor`
        # consumer that saw the later seq first would skip the earlier one
        # forever. This xact-scoped advisory lock serializes [first outbox
        # insert .. commit], making committed seq order == commit order
        # (holes come only from rolled-back transactions). Outbox-writing
        # transactions are short DB-only writes, so the serialization cost
        # is negligible at this deployment's write rate (round23).
        self._session.execute(
            sa.text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": OUTBOX_NS}
        )
        row = mappers.outbox_event_to_model(event)
        self._session.add(row)
        self._session.flush()
        return mappers.outbox_event_to_entity(row)

    def get(self, event_id: str) -> Optional[e.OutboxEvent]:
        row = (
            self._session.query(models.OutboxEvent)
            .filter(models.OutboxEvent.event_id == event_id)
            .one_or_none()
        )
        return mappers.outbox_event_to_entity(row) if row is not None else None


class PublishEvidenceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_base(self, evidence: DurablePublishBaseEvidence) -> DurablePublishBaseEvidence:
        # Source order is the numerator order.  Hold this xact-scoped lock
        # through commit so ledger_seq allocation cannot race ahead of the
        # first durable commit for the same raw source.  Every caller acquires
        # source before run to keep the lock order deterministic.
        self._session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"durable-publish-source:{evidence.source_identity_sha256}"},
        )
        self._session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"durable-publish-base:{evidence.processing_run_id}"},
        )
        existing = self._session.get(models.DurablePublishBase, evidence.processing_run_id)
        if existing is not None:
            loaded = self._base(existing)
            if loaded != evidence:
                raise PublishEvidenceConflict("durable publish base conflicts with first write")
            return loaded
        self._session.add(models.DurablePublishBase(**evidence.model_dump()))
        self._session.flush()
        return evidence

    def append_supplement(
        self, evidence: DurablePublishSupplementEvidence
    ) -> DurablePublishSupplementEvidence:
        self._session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"durable-publish-supplement:{evidence.supplement_id}"},
        )
        existing = self._session.get(models.DurablePublishSupplement, evidence.supplement_id)
        if existing is not None:
            loaded = self._supplement(existing)
            if loaded != evidence:
                raise PublishEvidenceConflict("publish supplement id conflicts with first write")
            return loaded
        base = self._session.get(models.DurablePublishBase, evidence.processing_run_id)
        if base is None:
            raise PublishEvidenceConflict("publish supplement has no durable base")
        self._session.add(models.DurablePublishSupplement(**evidence.model_dump()))
        self._session.flush()
        return evidence

    def latest_relay_head(self, relay_id: str) -> Optional[EncodedProgressRelayCheckpoint]:
        row = (
            self._session.query(models.ProgressRelayHead)
            .filter(models.ProgressRelayHead.relay_id == relay_id)
            .order_by(models.ProgressRelayHead.row_version.desc())
            .first()
        )
        return self._head(row) if row is not None else None

    def append_relay_head(
        self, checkpoint: EncodedProgressRelayCheckpoint
    ) -> EncodedProgressRelayCheckpoint:
        self._session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:relay_id, 0))"),
            {"relay_id": checkpoint.relay_id},
        )
        latest = (
            self._session.query(models.ProgressRelayHead)
            .filter(models.ProgressRelayHead.relay_id == checkpoint.relay_id)
            .order_by(models.ProgressRelayHead.row_version.desc())
            .with_for_update()
            .first()
        )
        existing = self._session.get(
            models.ProgressRelayHead, (checkpoint.relay_id, checkpoint.row_version)
        )
        if existing is not None:
            loaded = self._head(existing)
            if loaded != checkpoint:
                raise PublishEvidenceConflict("relay head version conflicts with first write")
            return loaded
        expected_version = 0 if latest is None else latest.row_version + 1
        expected_previous = None if latest is None else latest.checkpoint_sha256
        if (
            checkpoint.row_version != expected_version
            or checkpoint.previous_checkpoint_sha256 != expected_previous
        ):
            raise PublishEvidenceConflict("relay head CAS predecessor mismatch")
        self._session.add(models.ProgressRelayHead(**checkpoint.model_dump()))
        self._session.flush()
        return checkpoint

    @staticmethod
    def _base(row: models.DurablePublishBase) -> DurablePublishBaseEvidence:
        values = {
            name: getattr(row, name) for name in DurablePublishBaseEvidence.model_fields
        }
        values["publish_precommit_at"] = values["publish_precommit_at"].astimezone(
            timezone.utc
        )
        return DurablePublishBaseEvidence.model_validate(values)

    @staticmethod
    def _supplement(row: models.DurablePublishSupplement) -> DurablePublishSupplementEvidence:
        values = {
            name: getattr(row, name) for name in DurablePublishSupplementEvidence.model_fields
        }
        for name in ("publish_precommit_at", "publish_durable_observed_at"):
            values[name] = values[name].astimezone(timezone.utc)
        return DurablePublishSupplementEvidence.model_validate(values)

    @staticmethod
    def _head(row: models.ProgressRelayHead) -> EncodedProgressRelayCheckpoint:
        return EncodedProgressRelayCheckpoint.model_validate({
            name: getattr(row, name) for name in EncodedProgressRelayCheckpoint.model_fields
        })
