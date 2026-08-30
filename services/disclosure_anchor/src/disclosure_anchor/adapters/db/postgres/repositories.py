"""SQLAlchemy repository implementations.

Each repository adds domain entities into the active session (mapping them to ORM
models) and loads them back as entities. They never commit; the UnitOfWork owns
the transaction boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
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
    LocalMaterializationReceiptV2,
    PreparedMaterializationReceiptV2,
    PreparedReconcileReceipt,
    decode_checkpoint_receipt,
    decode_terminal_receipt,
)
from disclosure_anchor.application.contracts.staged_credit import (
    CreditShapeFacts,
    CreditVector,
    DatabaseLeaseSnapshot,
    STAGED_STATE_TRANSITIONS,
    credit_shape,
)
from disclosure_anchor.application.ports.repositories import (
    ClaimedAttemptSnapshot,
    CreditTransitionGrant,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    DurableCheckpointWitness,
    encode_durable_checkpoint_witness,
    prepared_submission_identity_from_reconcile,
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

_MAX_SIGNED_BIGINT = (1 << 63) - 1


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
    materialization: PreparedMaterializationReceiptV2 | None = None
    if row.materialization_receipt_bytes is not None:
        encoded_materialization = decode_checkpoint_receipt(
            bytes(row.materialization_receipt_bytes)
        )
        raw_materialization = encoded_materialization.receipt
        if not isinstance(raw_materialization, PreparedMaterializationReceiptV2):
            raise RemoteParseCheckpointConflict(
                "stored prepared materialization projection drifted"
            )
        materialization = raw_materialization
        if (
            encoded_materialization.sha256 != row.materialization_receipt_sha256
            or encoded_materialization.byte_count != row.materialization_receipt_byte_count
            or materialization.attempt_identity != row.attempt_id
            or materialization.fence_identity != row.fence_identity
            or materialization.source_pdf_sha256 != row.source_pdf_sha256
            or materialization.source_page_count != row.materialization_source_page_count
            or materialization.source_page_count != row.reservation_source_page_count
            or materialization.terminal_receipt_sha256 != row.terminal_receipt_sha256
            or materialization.process_profile_sha256 != row.process_profile_sha256
            or materialization.credit_policy_sha256 != row.credit_policy_sha256
            or materialization.reservation_input_sha256 != row.reservation_input_sha256
            or materialization.spool_relpath != row.materialization_spool_relpath
            or materialization.spool_sha256 != row.materialization_spool_sha256
            or materialization.spool_byte_count != row.materialization_spool_byte_count
            or materialization.spool_byte_count != row.result_artifact_bytes
            or materialization.compressed_byte_count
            != row.materialization_compressed_byte_count
            or materialization.compressed_byte_count != row.result_artifact_bytes
            or materialization.uncompressed_byte_count
            != row.materialization_uncompressed_byte_count
            or materialization.temporary_disk_byte_count
            != row.materialization_temp_disk_byte_count
            or materialization.decoded_byte_count
            != row.materialization_decoded_byte_count
            or materialization.member_count != row.materialization_member_count
            or materialization.private_token_sha256
            != row.materialization_token_sha256
        ):
            raise RemoteParseCheckpointConflict(
                "stored prepared materialization projection drifted"
            )
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
        if not isinstance(local, (LocalMaterializationReceipt, LocalMaterializationReceiptV2)) or (
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
        if isinstance(local, LocalMaterializationReceiptV2) and (
            local.db_staged_byte_count != row.local_db_staged_byte_count
            or local.process_profile_sha256 != row.process_profile_sha256
            or local.credit_policy_sha256 != row.credit_policy_sha256
            or local.reservation_input_sha256 != row.reservation_input_sha256
            or local.prepared_materialization_sha256
            != row.materialization_receipt_sha256
            or materialization is None
            or local.source_page_count != materialization.source_page_count
            or local.compressed_byte_count != materialization.compressed_byte_count
            or local.uncompressed_byte_count != materialization.uncompressed_byte_count
            or local.member_count != materialization.member_count
            or local.temporary_disk_byte_count
            != materialization.temporary_disk_byte_count
            or local.decoded_byte_count != materialization.decoded_byte_count
        ):
            raise RemoteParseCheckpointConflict("stored v2 local credit evidence drifted")
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
    credit_names = (
        "documents", "remote_waits", "retained_results", "retained_bytes",
        "local_items", "compressed_bytes", "decoded_bytes", "temp_disk_bytes",
        "db_stage_items", "db_staged_bytes", "ack_items", "unpublished_pages",
    )
    reservation = (
        None if row.checkpoint_contract_version < 3 else CreditVector(
            **{name: getattr(row, f"reservation_{name}") for name in credit_names}
        )
    )
    current_credits = (
        None if row.checkpoint_contract_version < 3 else CreditVector(
            **{name: getattr(row, f"current_{name}") for name in credit_names}
        )
    )
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
        process_profile_sha256=row.process_profile_sha256,
        credit_policy_sha256=row.credit_policy_sha256,
        reservation_input_sha256=row.reservation_input_sha256,
        reservation_input_bytes=(
            None if row.reservation_input_bytes is None
            else bytes(row.reservation_input_bytes)
        ),
        reservation_input_byte_count=row.reservation_input_byte_count,
        reservation_source_byte_count=row.reservation_source_byte_count,
        reservation_source_page_count=row.reservation_source_page_count,
        reservation_bucket=row.reservation_bucket,
        reservation=reservation,
        current_credits=current_credits,
        materialization_receipt_sha256=row.materialization_receipt_sha256,
        materialization_receipt_bytes=(
            None if row.materialization_receipt_bytes is None
            else bytes(row.materialization_receipt_bytes)
        ),
        materialization_receipt_byte_count=row.materialization_receipt_byte_count,
        local_db_staged_byte_count=row.local_db_staged_byte_count,
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

    def add_v3_prepared(
        self,
        attempt: RemoteParseAttempt,
        prepared_secret: RemoteParseResumeSecret,
    ) -> RemoteParseAttempt:
        if not (
            attempt.checkpoint_contract_version == 3
            and attempt.state == "prepared"
            and attempt.is_current
            and attempt.row_version == 0
            and attempt.claim_generation == 0
            and attempt.claim_owner_identity is None
            and attempt.claim_lease_until is None
            and attempt.remote_task_identity is None
            and attempt.submitted_receipt_sha256 is None
            and attempt.submitted_receipt_bytes is None
            and attempt.submitted_receipt_byte_count is None
            and attempt.terminal_receipt_sha256 is None
            and attempt.terminal_receipt_bytes is None
            and attempt.terminal_receipt_byte_count is None
            and attempt.result_owner_identity is None
            and attempt.result_artifact_sha256 is None
            and attempt.result_artifact_bytes is None
            and attempt.local_receipt_sha256 is None
            and attempt.local_receipt_bytes is None
            and attempt.local_receipt_byte_count is None
            and attempt.local_db_staged_byte_count is None
            and attempt.failure_receipt_sha256 is None
            and attempt.failure_receipt_bytes is None
            and attempt.failure_receipt_byte_count is None
            and attempt.failure_stage is None
            and attempt.materialization_receipt_sha256 is None
            and attempt.materialization_receipt_bytes is None
            and attempt.materialization_receipt_byte_count is None
            and type(attempt.reservation) is CreditVector
            and type(attempt.current_credits) is CreditVector
        ):
            raise ValueError("new v3 attempt must have canonical unclaimed prepared shape")
        if not (
            isinstance(prepared_secret, RemoteParseResumeSecret)
            and prepared_secret.secret_contract_version == 3
            and prepared_secret.secret_kind == "prepared_reconcile"
            and prepared_secret.attempt_id == attempt.attempt_id
        ):
            raise ValueError("v3 prepared attempt requires its exact private secret")
        encoded = decode_checkpoint_receipt(prepared_secret.token_bytes)
        prepared = encoded.receipt
        if not isinstance(prepared, PreparedReconcileReceipt) or (
            prepared_secret.token_sha256 != encoded.sha256
            or prepared_secret.token_byte_count != encoded.byte_count
            or prepared.attempt_identity != attempt.attempt_id
            or prepared.fence_identity != attempt.fence_identity
            or prepared.source_pdf_sha256 != attempt.source_pdf_sha256
            or prepared.client_submit_key != attempt.client_submit_key
            or prepared.parser_target_sha256 != attempt.parser_target_sha256
            or prepared.request_sha256 != attempt.request_sha256
            or prepared.runtime_epoch_sha256 != attempt.runtime_epoch_sha256
        ):
            raise ValueError("v3 prepared reconcile evidence drifted from attempt")
        values = {
            name: getattr(attempt, name)
            for name in (
                "attempt_id", "processing_run_id", "document_id",
                "attempt_generation", "fence_identity", "source_pdf_sha256",
                "parser_target_sha256", "request_sha256", "runtime_epoch_sha256",
                "client_submit_key", "checkpoint_contract_version", "state",
                "is_current", "row_version", "remote_task_identity",
                "submitted_receipt_sha256", "submitted_receipt_bytes",
                "submitted_receipt_byte_count", "terminal_receipt_sha256",
                "terminal_receipt_bytes", "terminal_receipt_byte_count",
                "result_owner_identity", "result_artifact_sha256",
                "result_artifact_bytes", "claim_generation",
                "claim_owner_identity", "claim_lease_until",
                "local_receipt_sha256", "local_receipt_bytes",
                "local_receipt_byte_count", "failure_receipt_sha256",
                "failure_receipt_bytes", "failure_receipt_byte_count",
                "failure_stage", "process_profile_sha256", "credit_policy_sha256",
                "reservation_input_sha256", "reservation_input_bytes",
                "reservation_input_byte_count", "reservation_source_byte_count",
                "reservation_source_page_count", "reservation_bucket",
                "materialization_receipt_sha256", "materialization_receipt_bytes",
                "materialization_receipt_byte_count", "local_db_staged_byte_count",
            )
        }
        for prefix, vector in (
            ("reservation", attempt.reservation),
            ("current", attempt.current_credits),
        ):
            assert isinstance(vector, CreditVector)
            for name in vector.__dataclass_fields__:
                values[f"{prefix}_{name}"] = getattr(vector, name)
        row = models.RemoteParseAttempt(**values)
        secret_row = models.RemoteParseV3ResumeSecret(
            attempt_id=prepared_secret.attempt_id,
            secret_kind=prepared_secret.secret_kind,
            token_bytes=prepared_secret.token_bytes,
            token_sha256=prepared_secret.token_sha256,
            token_byte_count=prepared_secret.token_byte_count,
        )
        with self._session.begin_nested():
            self._session.add(row)
            self._session.add(secret_row)
            self._session.flush()
        return _remote_attempt_entity(row)

    def get(self, attempt_id: str) -> Optional[RemoteParseAttempt]:
        row = self._session.get(models.RemoteParseAttempt, attempt_id)
        return None if row is None else _remote_attempt_entity(row)

    def durable_checkpoint_witness(
        self, attempt_id: str
    ) -> DurableCheckpointWitness:
        row = self._session.get(models.RemoteParseAttempt, attempt_id)
        if row is None or row.checkpoint_contract_version != 2:
            raise RemoteParseCheckpointConflict(
                "durable witness requires an existing v2 attempt"
            )
        # Decode and cross-bind every state-owned receipt before projecting a
        # destructive-operation witness. This is the single repository trust
        # boundary; callers cannot manufacture receipt hashes.
        _remote_attempt_entity(row)
        prepared_secret = self._session.get(
            models.RemoteParseResumeSecret,
            (attempt_id, "prepared_reconcile"),
        )
        if prepared_secret is None:
            raise RemoteParseCheckpointConflict(
                "durable witness lacks prepared reconcile evidence"
            )
        prepared_encoded = decode_checkpoint_receipt(
            bytes(prepared_secret.token_bytes)
        )
        prepared = prepared_encoded.receipt
        if not isinstance(prepared, PreparedReconcileReceipt) or (
            prepared.attempt_identity != row.attempt_id
            or prepared.fence_identity != row.fence_identity
            or prepared.source_pdf_sha256 != row.source_pdf_sha256
            or prepared.parser_target_sha256 != row.parser_target_sha256
            or prepared.request_sha256 != row.request_sha256
            or prepared.runtime_epoch_sha256 != row.runtime_epoch_sha256
            or prepared.client_submit_key != row.client_submit_key
        ):
            raise RemoteParseCheckpointConflict(
                "prepared reconcile evidence drifted from attempt"
            )
        prepared_identity = prepared_submission_identity_from_reconcile(prepared)

        if row.submitted_receipt_bytes is not None:
            submitted = decode_checkpoint_receipt(
                bytes(row.submitted_receipt_bytes)
            )
            if (
                not isinstance(submitted.receipt, AcceptedSubmissionReceipt)
                or submitted.sha256 != row.submitted_receipt_sha256
                or submitted.receipt.attempt_identity != row.attempt_id
                or submitted.receipt.fence_identity != row.fence_identity
                or submitted.receipt.source_pdf_sha256
                != prepared.source_pdf_sha256
                or submitted.receipt.client_submit_key
                != prepared.client_submit_key
                or submitted.receipt.submission_epoch_unix
                != prepared.submission_epoch_unix
                or submitted.receipt.remote_task_identity
                != row.remote_task_identity
            ):
                raise RemoteParseCheckpointConflict(
                    "accepted submission evidence drifted from prepared attempt"
                )
        if row.failure_receipt_bytes is not None:
            failure = decode_checkpoint_receipt(bytes(row.failure_receipt_bytes))
            if (
                not isinstance(failure.receipt, FailureReceipt)
                or failure.sha256 != row.failure_receipt_sha256
                or failure.receipt.attempt_identity != row.attempt_id
                or failure.receipt.fence_identity != row.fence_identity
                or failure.receipt.remote_task_identity
                != row.remote_task_identity
            ):
                raise RemoteParseCheckpointConflict(
                    "failure evidence drifted from durable attempt"
                )
        return encode_durable_checkpoint_witness(
            attempt_identity=row.attempt_id,
            fence_identity=row.fence_identity,
            checkpoint_contract_version=row.checkpoint_contract_version,
            row_version=row.row_version,
            claim_generation=row.claim_generation,
            state=row.state,
            prepared_identity=prepared_identity,
            accepted_submission_receipt_sha256=row.submitted_receipt_sha256,
            terminal_receipt_sha256=row.terminal_receipt_sha256,
            failure_receipt_sha256=row.failure_receipt_sha256,
            remote_task_identity=row.remote_task_identity,
        )

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

    def list_v3_recoverable(
        self, *, after_attempt_id: str | None, limit: int
    ) -> list[RemoteParseAttempt]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("v3 recovery page limit is outside 1..1000")
        unsupported = self._session.query(models.RemoteParseAttempt).filter(
            models.RemoteParseAttempt.is_current.is_(True),
            models.RemoteParseAttempt.checkpoint_contract_version != 3,
        ).order_by(models.RemoteParseAttempt.attempt_id).first()
        if unsupported is not None:
            raise RemoteParseCheckpointConflict(
                "non-v3 current checkpoint blocks v3 staged activation: "
                f"{unsupported.attempt_id}/{unsupported.state}"
            )
        query = self._session.query(models.RemoteParseAttempt).filter(
            models.RemoteParseAttempt.checkpoint_contract_version == 3,
            models.RemoteParseAttempt.is_current.is_(True),
        )
        if after_attempt_id is not None:
            query = query.filter(models.RemoteParseAttempt.attempt_id > after_attempt_id)
        rows = query.order_by(models.RemoteParseAttempt.attempt_id).limit(limit).all()
        return [_remote_attempt_entity(row) for row in rows]

    @staticmethod
    def _validate_v3_claim_request(
        *, owner_identity: str, lease_seconds: int, expected_current: CreditVector,
        expected_version: int, allow_version_increment: bool,
        claim_generation: int | None = None,
    ) -> None:
        maximum_version = _MAX_SIGNED_BIGINT - int(allow_version_increment)
        if (
            not isinstance(owner_identity, str)
            or not owner_identity.strip()
            or len(owner_identity) > 128
            or isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 300
            or type(expected_current) is not CreditVector
            or isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or not 0 <= expected_version <= maximum_version
            or (
                claim_generation is not None
                and (
                    isinstance(claim_generation, bool)
                    or not isinstance(claim_generation, int)
                    or not 1 <= claim_generation <= _MAX_SIGNED_BIGINT
                )
            )
        ):
            raise ValueError("v3 recovery claim request is invalid")

    @staticmethod
    def _v3_current_predicates(expected_current: CreditVector) -> tuple[Any, ...]:
        return tuple(
            getattr(models.RemoteParseAttempt, f"current_{name}")
            == getattr(expected_current, name)
            for name in expected_current.__dataclass_fields__
        )

    @staticmethod
    def _database_clock() -> Any:
        return sa.select(
            sa.cast(
                sa.func.clock_timestamp(), sa.DateTime(timezone=True)
            ).label("database_observed_at")
        ).cte("database_clock").prefix_with("MATERIALIZED")

    @staticmethod
    def _claimed_snapshot(
        attempt: models.RemoteParseAttempt,
        database_observed_at: datetime,
        remaining_microseconds: int,
    ) -> ClaimedAttemptSnapshot:
        entity = _remote_attempt_entity(attempt)
        if entity.claim_lease_until is None:
            raise RemoteParseCheckpointConflict("v3 claim returned without a lease")
        observed_utc = (
            database_observed_at.replace(tzinfo=timezone.utc)
            if database_observed_at.tzinfo is None
            else database_observed_at.astimezone(timezone.utc)
        )
        lease_utc = (
            entity.claim_lease_until.replace(tzinfo=timezone.utc)
            if entity.claim_lease_until.tzinfo is None
            else entity.claim_lease_until.astimezone(timezone.utc)
        )
        entity = replace(entity, claim_lease_until=lease_utc)
        return ClaimedAttemptSnapshot(
            attempt=entity,
            database_lease=DatabaseLeaseSnapshot(
                database_observed_at_utc=observed_utc,
                lease_until_utc=lease_utc,
                remaining_microseconds=remaining_microseconds,
            ),
        )

    def claim_v3_recovery(
        self, *, attempt_id: str, fence_identity: str, expected_state: str,
        expected_version: int, expected_current: CreditVector,
        owner_identity: str, lease_seconds: int,
    ) -> ClaimedAttemptSnapshot:
        self._validate_v3_claim_request(
            owner_identity=owner_identity,
            lease_seconds=lease_seconds,
            expected_current=expected_current,
            expected_version=expected_version,
            allow_version_increment=True,
        )
        clock = self._database_clock()
        live_same_owner = sa.and_(
            models.RemoteParseAttempt.claim_owner_identity == owner_identity,
            models.RemoteParseAttempt.claim_lease_until > clock.c.database_observed_at,
            models.RemoteParseAttempt.row_version == expected_version + 1,
        )
        acquirable = sa.and_(
            models.RemoteParseAttempt.row_version == expected_version,
            models.RemoteParseAttempt.claim_generation < _MAX_SIGNED_BIGINT,
            sa.or_(
                models.RemoteParseAttempt.claim_owner_identity.is_(None),
                models.RemoteParseAttempt.claim_lease_until
                <= clock.c.database_observed_at,
            ),
        )
        lease_until = clock.c.database_observed_at + sa.func.make_interval(
            0, 0, 0, 0, 0, 0, lease_seconds
        )
        statement = (
            sa.update(models.RemoteParseAttempt)
            .where(
                models.RemoteParseAttempt.attempt_id == attempt_id,
                models.RemoteParseAttempt.fence_identity == fence_identity,
                models.RemoteParseAttempt.checkpoint_contract_version == 3,
                models.RemoteParseAttempt.is_current.is_(True),
                models.RemoteParseAttempt.state == expected_state,
                *self._v3_current_predicates(expected_current),
                sa.or_(live_same_owner, acquirable),
            )
            .values(
                claim_generation=sa.case(
                    (live_same_owner, models.RemoteParseAttempt.claim_generation),
                    else_=models.RemoteParseAttempt.claim_generation + 1,
                ),
                claim_owner_identity=owner_identity,
                claim_lease_until=lease_until,
                row_version=sa.case(
                    (live_same_owner, models.RemoteParseAttempt.row_version),
                    else_=models.RemoteParseAttempt.row_version + 1,
                ),
                updated_at=clock.c.database_observed_at,
            )
            .returning(
                models.RemoteParseAttempt,
                clock.c.database_observed_at,
                sa.cast(
                    sa.func.floor(
                        sa.extract("epoch", lease_until - clock.c.database_observed_at)
                        * 1_000_000
                    ),
                    sa.BigInteger,
                ).label("remaining_microseconds"),
            )
        )
        result = self._session.execute(statement).one_or_none()
        if result is None:
            raise RemoteParseCheckpointConflict("v3 recovery claim lost exact CAS")
        return self._claimed_snapshot(result[0], result[1], result[2])

    def renew_v3_claim(
        self, *, attempt_id: str, fence_identity: str, expected_state: str,
        expected_version: int, expected_current: CreditVector,
        owner_identity: str, claim_generation: int, lease_seconds: int,
    ) -> ClaimedAttemptSnapshot:
        self._validate_v3_claim_request(
            owner_identity=owner_identity,
            lease_seconds=lease_seconds,
            expected_current=expected_current,
            expected_version=expected_version,
            allow_version_increment=False,
            claim_generation=claim_generation,
        )
        clock = self._database_clock()
        lease_until = clock.c.database_observed_at + sa.func.make_interval(
            0, 0, 0, 0, 0, 0, lease_seconds
        )
        statement = (
            sa.update(models.RemoteParseAttempt)
            .where(
                models.RemoteParseAttempt.attempt_id == attempt_id,
                models.RemoteParseAttempt.fence_identity == fence_identity,
                models.RemoteParseAttempt.checkpoint_contract_version == 3,
                models.RemoteParseAttempt.is_current.is_(True),
                models.RemoteParseAttempt.state == expected_state,
                models.RemoteParseAttempt.row_version == expected_version,
                models.RemoteParseAttempt.claim_owner_identity == owner_identity,
                models.RemoteParseAttempt.claim_generation == claim_generation,
                models.RemoteParseAttempt.claim_lease_until
                > clock.c.database_observed_at,
                *self._v3_current_predicates(expected_current),
            )
            .values(
                claim_lease_until=lease_until,
                updated_at=clock.c.database_observed_at,
            )
            .returning(
                models.RemoteParseAttempt,
                clock.c.database_observed_at,
                sa.cast(
                    sa.func.floor(
                        sa.extract("epoch", lease_until - clock.c.database_observed_at)
                        * 1_000_000
                    ),
                    sa.BigInteger,
                ).label("remaining_microseconds"),
            )
        )
        result = self._session.execute(statement).one_or_none()
        if result is None:
            raise RemoteParseCheckpointConflict("v3 recovery renewal lost exact live claim")
        return self._claimed_snapshot(result[0], result[1], result[2])

    def reload_v3_claim(
        self, *, attempt_id: str, fence_identity: str, expected_state: str,
        expected_version: int, expected_current: CreditVector,
        owner_identity: str, claim_generation: int,
    ) -> ClaimedAttemptSnapshot:
        self._validate_v3_claim_request(
            owner_identity=owner_identity,
            lease_seconds=1,
            expected_current=expected_current,
            expected_version=expected_version,
            allow_version_increment=False,
            claim_generation=claim_generation,
        )
        clock = self._database_clock()
        remaining = sa.cast(
            sa.func.floor(
                sa.extract(
                    "epoch",
                    models.RemoteParseAttempt.claim_lease_until
                    - clock.c.database_observed_at,
                ) * 1_000_000
            ),
            sa.BigInteger,
        ).label("remaining_microseconds")
        statement = sa.select(
            models.RemoteParseAttempt,
            clock.c.database_observed_at,
            remaining,
        ).where(
            models.RemoteParseAttempt.attempt_id == attempt_id,
            models.RemoteParseAttempt.fence_identity == fence_identity,
            models.RemoteParseAttempt.checkpoint_contract_version == 3,
            models.RemoteParseAttempt.is_current.is_(True),
            models.RemoteParseAttempt.state == expected_state,
            models.RemoteParseAttempt.row_version == expected_version,
            models.RemoteParseAttempt.claim_owner_identity == owner_identity,
            models.RemoteParseAttempt.claim_generation == claim_generation,
            models.RemoteParseAttempt.claim_lease_until
            > clock.c.database_observed_at,
            *self._v3_current_predicates(expected_current),
        )
        result = self._session.execute(statement).one_or_none()
        if result is None:
            raise RemoteParseCheckpointConflict("v3 recovery reload lost exact live claim")
        return self._claimed_snapshot(result[0], result[1], result[2])

    @staticmethod
    def _validate_expected_v3_attempt(attempt: RemoteParseAttempt) -> None:
        if not (
            type(attempt) is RemoteParseAttempt
            and attempt.checkpoint_contract_version == 3
            and attempt.is_current
            and 0 <= attempt.row_version < _MAX_SIGNED_BIGINT
            and attempt.claim_generation >= 1
            and attempt.claim_owner_identity is not None
            and attempt.claim_lease_until is not None
            and type(attempt.current_credits) is CreditVector
            and type(attempt.reservation) is CreditVector
        ):
            raise ValueError("v3 lifecycle CAS requires an exact claimed attempt")

    @staticmethod
    def _v3_secret_row(
        secret: RemoteParseResumeSecret,
        *, attempt_id: str,
        kind: str,
        token_sha256: str,
    ) -> models.RemoteParseV3ResumeSecret:
        if not (
            isinstance(secret, RemoteParseResumeSecret)
            and secret.secret_contract_version == 3
            and secret.attempt_id == attempt_id
            and secret.secret_kind == kind
            and secret.token_sha256 == token_sha256
        ):
            raise ValueError(f"v3 {kind} secret drifted from lifecycle receipt")
        return models.RemoteParseV3ResumeSecret(
            attempt_id=secret.attempt_id,
            secret_kind=secret.secret_kind,
            token_bytes=secret.token_bytes,
            token_sha256=secret.token_sha256,
            token_byte_count=secret.token_byte_count,
        )

    def _transition_v3_lifecycle(
        self,
        *,
        expected: RemoteParseAttempt,
        next_state: str,
        candidate_current: CreditVector,
        grant: CreditTransitionGrant,
        extra_values: Mapping[str, Any] | None = None,
        secret_row: models.RemoteParseV3ResumeSecret | None = None,
    ) -> ClaimedAttemptSnapshot:
        self._validate_expected_v3_attempt(expected)
        assert isinstance(expected.current_credits, CreditVector)
        assert isinstance(expected.reservation, CreditVector)
        if next_state not in STAGED_STATE_TRANSITIONS.get(expected.state, frozenset()):
            raise ValueError("v3 lifecycle transition is not in the closed state graph")
        if type(grant) is not CreditTransitionGrant or (
            grant.expected_current != expected.current_credits
        ):
            raise ValueError("v3 lifecycle credit grant drifted from expected attempt")
        if type(candidate_current) is not CreditVector or not candidate_current.fits(
            expected.reservation
        ):
            raise ValueError("v3 lifecycle candidate exceeds immutable reservation")
        if not grant.permits(candidate_current):
            raise ValueError("v3 lifecycle candidate exceeds positive credit grant")
        clock = self._database_clock()
        remaining = sa.cast(
            sa.func.floor(
                sa.extract(
                    "epoch",
                    models.RemoteParseAttempt.claim_lease_until
                    - clock.c.database_observed_at,
                ) * 1_000_000
            ),
            sa.BigInteger,
        ).label("remaining_microseconds")
        values: dict[str, Any] = {
            "state": next_state,
            "row_version": expected.row_version + 1,
            "updated_at": clock.c.database_observed_at,
            **{
                f"current_{name}": getattr(candidate_current, name)
                for name in candidate_current.__dataclass_fields__
            },
        }
        if extra_values:
            values.update(extra_values)
        statement = (
            sa.update(models.RemoteParseAttempt)
            .where(
                models.RemoteParseAttempt.attempt_id == expected.attempt_id,
                models.RemoteParseAttempt.fence_identity == expected.fence_identity,
                models.RemoteParseAttempt.checkpoint_contract_version == 3,
                models.RemoteParseAttempt.is_current.is_(True),
                models.RemoteParseAttempt.state == expected.state,
                models.RemoteParseAttempt.row_version == expected.row_version,
                models.RemoteParseAttempt.claim_owner_identity
                == expected.claim_owner_identity,
                models.RemoteParseAttempt.claim_generation
                == expected.claim_generation,
                models.RemoteParseAttempt.claim_lease_until
                > clock.c.database_observed_at,
                *self._v3_current_predicates(expected.current_credits),
            )
            .values(**values)
            .returning(
                models.RemoteParseAttempt,
                clock.c.database_observed_at,
                remaining,
            )
        )
        with self._session.begin_nested():
            if secret_row is not None:
                self._session.execute(
                    pg_insert(models.RemoteParseV3ResumeSecret)
                    .values(
                        attempt_id=secret_row.attempt_id,
                        secret_kind=secret_row.secret_kind,
                        token_bytes=secret_row.token_bytes,
                        token_sha256=secret_row.token_sha256,
                        token_byte_count=secret_row.token_byte_count,
                    )
                    .on_conflict_do_nothing()
                )
            result = self._session.execute(statement).one_or_none()
            if result is None:
                raise RemoteParseCheckpointConflict("v3 lifecycle transition lost exact CAS")
            snapshot = self._claimed_snapshot(result[0], result[1], result[2])
        return snapshot

    def transition_v3_reconciling(
        self, *, expected_attempt: RemoteParseAttempt,
        grant: CreditTransitionGrant,
    ) -> ClaimedAttemptSnapshot:
        if expected_attempt.state != "prepared":
            raise ValueError("v3 reconciling transition requires prepared state")
        return self._transition_v3_lifecycle(
            expected=expected_attempt,
            next_state="reconciling",
            candidate_current=credit_shape("reconciling", CreditShapeFacts()),
            grant=grant,
        )

    def checkpoint_v3_submitted(
        self, *, expected_attempt: RemoteParseAttempt,
        grant: CreditTransitionGrant, receipt: EncodedCheckpointReceipt,
        accepted_secret: RemoteParseResumeSecret,
    ) -> ClaimedAttemptSnapshot:
        validated = decode_checkpoint_receipt(receipt.exact_bytes)
        accepted = validated.receipt
        if validated != receipt or not isinstance(accepted, AcceptedSubmissionReceipt):
            raise ValueError("v3 submitted receipt is not canonical accepted evidence")
        if not (
            expected_attempt.state == "reconciling"
            and accepted.attempt_identity == expected_attempt.attempt_id
            and accepted.fence_identity == expected_attempt.fence_identity
            and accepted.source_pdf_sha256 == expected_attempt.source_pdf_sha256
            and accepted.client_submit_key == expected_attempt.client_submit_key
        ):
            raise ValueError("v3 accepted receipt drifted from expected attempt")
        prepared_secret = self._session.get(
            models.RemoteParseV3ResumeSecret,
            (expected_attempt.attempt_id, "prepared_reconcile"),
        )
        if prepared_secret is None:
            raise RemoteParseCheckpointConflict("v3 prepared secret is absent")
        prepared = decode_checkpoint_receipt(bytes(prepared_secret.token_bytes)).receipt
        if not isinstance(prepared, PreparedReconcileReceipt) or (
            prepared.submission_epoch_unix != accepted.submission_epoch_unix
            or prepared.attempt_identity != accepted.attempt_identity
            or prepared.fence_identity != accepted.fence_identity
            or prepared.source_pdf_sha256 != accepted.source_pdf_sha256
            or prepared.client_submit_key != accepted.client_submit_key
        ):
            raise ValueError("v3 accepted receipt drifted from prepared evidence")
        secret_row = self._v3_secret_row(
            accepted_secret,
            attempt_id=expected_attempt.attempt_id,
            kind="accepted_submission",
            token_sha256=accepted.resume_token_sha256,
        )
        return self._transition_v3_lifecycle(
            expected=expected_attempt,
            next_state="submitted",
            candidate_current=credit_shape("submitted", CreditShapeFacts()),
            grant=grant,
            extra_values={
                "remote_task_identity": accepted.remote_task_identity,
                "submitted_receipt_sha256": validated.sha256,
                "submitted_receipt_bytes": validated.exact_bytes,
                "submitted_receipt_byte_count": validated.byte_count,
            },
            secret_row=secret_row,
        )

    def checkpoint_v3_terminal(
        self, *, expected_attempt: RemoteParseAttempt,
        grant: CreditTransitionGrant, receipt: EncodedTerminalReceipt,
        terminal_secret: RemoteParseResumeSecret,
    ) -> ClaimedAttemptSnapshot:
        validated = decode_terminal_receipt(receipt.exact_bytes)
        terminal = validated.receipt
        if validated != receipt or not (
            expected_attempt.state == "submitted"
            and terminal.attempt_identity == expected_attempt.attempt_id
            and terminal.fence_identity == expected_attempt.fence_identity
            and terminal.source_pdf_sha256 == expected_attempt.source_pdf_sha256
        ):
            raise ValueError("v3 terminal receipt drifted from expected attempt")
        secret_row = self._v3_secret_row(
            terminal_secret,
            attempt_id=expected_attempt.attempt_id,
            kind="terminal",
            token_sha256=terminal.resume_token_sha256,
        )
        candidate = credit_shape(
            "remote_terminal",
            CreditShapeFacts(terminal_byte_count=terminal.artifact_byte_count),
        )
        return self._transition_v3_lifecycle(
            expected=expected_attempt,
            next_state="remote_terminal",
            candidate_current=candidate,
            grant=grant,
            extra_values={
                "terminal_receipt_sha256": validated.sha256,
                "terminal_receipt_bytes": validated.exact_bytes,
                "terminal_receipt_byte_count": validated.byte_count,
                "result_owner_identity": terminal.artifact_owner_identity,
                "result_artifact_sha256": terminal.artifact_sha256,
                "result_artifact_bytes": terminal.artifact_byte_count,
            },
            secret_row=secret_row,
        )

    def prepare_v3_materialization(
        self, *, expected_attempt: RemoteParseAttempt,
        grant: CreditTransitionGrant, receipt: EncodedCheckpointReceipt,
        materialization_secret: RemoteParseResumeSecret,
    ) -> ClaimedAttemptSnapshot:
        validated = decode_checkpoint_receipt(receipt.exact_bytes)
        materialization = validated.receipt
        if validated != receipt or not isinstance(
            materialization, PreparedMaterializationReceiptV2
        ):
            raise ValueError("v3 materialization receipt is not canonical")
        if not (
            expected_attempt.state == "remote_terminal"
            and materialization.attempt_identity == expected_attempt.attempt_id
            and materialization.fence_identity == expected_attempt.fence_identity
            and materialization.source_pdf_sha256 == expected_attempt.source_pdf_sha256
            and materialization.source_page_count
            == expected_attempt.reservation_source_page_count
            and materialization.terminal_receipt_sha256
            == expected_attempt.terminal_receipt_sha256
            and materialization.process_profile_sha256
            == expected_attempt.process_profile_sha256
            and materialization.credit_policy_sha256
            == expected_attempt.credit_policy_sha256
            and materialization.reservation_input_sha256
            == expected_attempt.reservation_input_sha256
            and materialization.spool_byte_count
            == expected_attempt.result_artifact_bytes
            and materialization.spool_sha256
            == expected_attempt.result_artifact_sha256
            and materialization.compressed_byte_count
            == expected_attempt.result_artifact_bytes
        ):
            raise ValueError("v3 materialization receipt drifted from terminal attempt")
        secret_row = self._v3_secret_row(
            materialization_secret,
            attempt_id=expected_attempt.attempt_id,
            kind="materialization",
            token_sha256=materialization.private_token_sha256,
        )
        candidate = credit_shape(
            "materializing",
            CreditShapeFacts(
                terminal_byte_count=expected_attempt.result_artifact_bytes or 0,
                compressed_byte_count=materialization.compressed_byte_count,
                uncompressed_byte_count=materialization.uncompressed_byte_count,
                decoded_byte_count=materialization.decoded_byte_count,
                temporary_disk_byte_count=materialization.temporary_disk_byte_count,
                source_page_count=materialization.source_page_count,
                materialization_prepared=True,
            ),
        )
        return self._transition_v3_lifecycle(
            expected=expected_attempt,
            next_state="materializing",
            candidate_current=candidate,
            grant=grant,
            extra_values={
                "materialization_receipt_sha256": validated.sha256,
                "materialization_receipt_bytes": validated.exact_bytes,
                "materialization_receipt_byte_count": validated.byte_count,
                "materialization_source_page_count": materialization.source_page_count,
                "materialization_spool_relpath": materialization.spool_relpath,
                "materialization_spool_sha256": materialization.spool_sha256,
                "materialization_spool_byte_count": materialization.spool_byte_count,
                "materialization_compressed_byte_count": materialization.compressed_byte_count,
                "materialization_uncompressed_byte_count": materialization.uncompressed_byte_count,
                "materialization_temp_disk_byte_count": materialization.temporary_disk_byte_count,
                "materialization_decoded_byte_count": materialization.decoded_byte_count,
                "materialization_member_count": materialization.member_count,
                "materialization_token_sha256": materialization.private_token_sha256,
            },
            secret_row=secret_row,
        )

    def reconcile_v3_claim_after_race(
        self, *, expected_attempt: RemoteParseAttempt,
        next_state: str, next_current: CreditVector,
    ) -> ClaimedAttemptSnapshot:
        self._validate_expected_v3_attempt(expected_attempt)
        assert isinstance(expected_attempt.current_credits, CreditVector)
        if next_state not in STAGED_STATE_TRANSITIONS.get(
            expected_attempt.state, frozenset()
        ) or type(next_current) is not CreditVector:
            raise ValueError("v3 race reconciliation next projection is invalid")
        clock = self._database_clock()
        remaining = sa.cast(sa.func.floor(sa.extract(
            "epoch", models.RemoteParseAttempt.claim_lease_until
            - clock.c.database_observed_at
        ) * 1_000_000), sa.BigInteger).label("remaining_microseconds")
        old_projection = sa.and_(
            models.RemoteParseAttempt.state == expected_attempt.state,
            models.RemoteParseAttempt.row_version == expected_attempt.row_version,
            *self._v3_current_predicates(expected_attempt.current_credits),
        )
        next_projection = sa.and_(
            models.RemoteParseAttempt.state == next_state,
            models.RemoteParseAttempt.row_version == expected_attempt.row_version + 1,
            *self._v3_current_predicates(next_current),
        )
        statement = sa.select(
            models.RemoteParseAttempt, clock.c.database_observed_at, remaining,
        ).where(
            models.RemoteParseAttempt.attempt_id == expected_attempt.attempt_id,
            models.RemoteParseAttempt.fence_identity == expected_attempt.fence_identity,
            models.RemoteParseAttempt.checkpoint_contract_version == 3,
            models.RemoteParseAttempt.is_current.is_(True),
            models.RemoteParseAttempt.claim_owner_identity
            == expected_attempt.claim_owner_identity,
            models.RemoteParseAttempt.claim_generation
            == expected_attempt.claim_generation,
            models.RemoteParseAttempt.claim_lease_until
            > clock.c.database_observed_at,
            sa.or_(old_projection, next_projection),
        )
        result = self._session.execute(statement).one_or_none()
        if result is None:
            raise RemoteParseCheckpointConflict("v3 race reconciliation lost exact projection")
        return self._claimed_snapshot(result[0], result[1], result[2])

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
        if current.local_receipt_bytes is None:
            raise RemoteParseCheckpointConflict(
                "finish checkpoint lacks local materialization evidence"
            )
        encoded_local = decode_checkpoint_receipt(bytes(current.local_receipt_bytes))
        local = encoded_local.receipt
        target_identity = finished_run.parser_target_identity
        if target_identity is None:
            target_sha256 = None
        else:
            target_exact = json.dumps(
                target_identity, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            target_sha256 = "sha256:" + hashlib.sha256(target_exact).hexdigest()
        if not isinstance(local, LocalMaterializationReceipt) or (
            encoded_local.sha256 != current.local_receipt_sha256
            or encoded_local.byte_count != current.local_receipt_byte_count
            or local.attempt_identity != current.attempt_id
            or local.fence_identity != current.fence_identity
            or local.source_pdf_sha256 != finished_run.input_raw_file_hash
            or local.parser_target_sha256 != target_sha256
            or local.provider_envelope_relpath
            != finished_run.provider_document_relpath
            or local.provider_envelope_sha256 != finished_run.artifact_hash
            or local.artifact_root_relpath != finished_run.parser_artifact_relpath
        ):
            raise RemoteParseCheckpointConflict(
                "succeeded processing run drifted from local receipt"
            )
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
