"""Shared document registration core for local and provider download paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from disclosure_anchor.application.contracts.historical_security_registration import (
    RETAINED_REGISTRATION_INTERFACE,
)
from disclosure_anchor.application.ports.file_store import RawDocumentWriteResult
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.source_security_resolution import (
    HistoricalAcquisitionProvenance,
    RegistrationProvenance,
    RetainedRecoveryProvenance,
)
from disclosure_anchor.application.services.subject_resolver import ResolvedSubject
from disclosure_anchor.application.worker.locks import maybe_lock_document
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.entities import outbox_events
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import SourceRecoveryError
from disclosure_anchor.domain.value_objects import ReportPeriod


@dataclass(frozen=True)
class DocumentRegistration:
    provider: str
    provider_document_id: str
    title: str
    announcement_date: date
    report_period: ReportPeriod | None
    filename: str
    provider_metadata: dict[str, object] = field(default_factory=dict)
    provider_interface: str = "local:register_pdf"
    dataset_key: str = "local_pdf"
    # Verified lineage from source_security_resolution; None keeps the
    # ordinary registration byte-for-byte unchanged.
    provenance: RegistrationProvenance | None = None


@dataclass(frozen=True)
class RegisterDocumentOutcome:
    document: e.Document
    source_access: e.SourceAccess
    outbox_event: e.OutboxEvent
    reused_existing_document: bool


def register_document(
    uow: UnitOfWork,
    *,
    subject: ResolvedSubject,
    doc_meta: DocumentRegistration,
    raw: RawDocumentWriteResult,
) -> RegisterDocumentOutcome:
    """Register archived raw bytes and emit the appropriate document event."""

    now = datetime.now(timezone.utc)
    recovery = _recovery_provenance(doc_meta=doc_meta, subject=subject, raw=raw)
    existing = uow.documents.get_by_provider_document_and_hash(
        provider=doc_meta.provider,
        provider_document_id=doc_meta.provider_document_id,
        raw_file_hash=raw.raw_file_hash,
    )
    if recovery is not None:
        # A recovery registers only into its own subject and never ahead of
        # a different version. Both checks precede every write, so a refusal
        # leaves no SourceAccess, receipt or event behind.
        _require_recovery_target(
            uow, existing=existing, subject=subject, doc_meta=doc_meta, raw=raw
        )
    source_access = _add_source_access(
        uow=uow,
        subject=subject,
        doc_meta=doc_meta,
        raw=raw,
        now=now,
        recovery=recovery,
    )
    if existing is not None:
        maybe_lock_document(uow, existing.document_id)
        # Same bytes, fresher provider signature hint: refresh the stored
        # file_signature so the pending_download_v1 signature_differs
        # re-fetch trigger self-limits — a spurious size-hint drift must
        # not re-download the same PDF every round (round23).
        fresh_signature = (doc_meta.provider_metadata or {}).get("file_signature")
        if isinstance(fresh_signature, dict) and isinstance(
            existing.provider_metadata, dict
        ):
            stored = existing.provider_metadata.get("file_signature")
            if stored != fresh_signature:
                existing.provider_metadata = {
                    **existing.provider_metadata,
                    "file_signature": fresh_signature,
                }
                uow.documents.update(existing)
        event = uow.outbox.add(
            outbox_events.document_observed(
                document_id=existing.document_id,
                provider=doc_meta.provider,
                provider_document_id=doc_meta.provider_document_id,
                raw_file_hash=raw.raw_file_hash,
                source_access_id=source_access.source_access_id,
                occurred_at=now,
            )
        )
        return RegisterDocumentOutcome(
            document=existing,
            source_access=source_access,
            outbox_event=event,
            reused_existing_document=True,
        )

    latest = uow.documents.latest_by_provider_document(
        provider=doc_meta.provider,
        provider_document_id=doc_meta.provider_document_id,
    )
    document = uow.documents.add(
        e.Document(
            document_id=ids.new_document_id(),
            status="registered",
            company_id=subject.company.company_id,
            security_id=subject.security.security_id,
            source_access_id=source_access.source_access_id,
            provider=doc_meta.provider,
            provider_document_id=doc_meta.provider_document_id,
            title=doc_meta.title,
            announcement_date=doc_meta.announcement_date,
            report_period=str(doc_meta.report_period) if doc_meta.report_period else None,
            raw_file_relpath=str(raw.relpath),
            raw_file_hash=raw.raw_file_hash,
            provider_metadata=doc_meta.provider_metadata,
            supersedes_document_id=latest.document_id if latest else None,
        )
    )
    event = uow.outbox.add(
        outbox_events.document_registered(
            document_id=document.document_id,
            provider=doc_meta.provider,
            provider_document_id=doc_meta.provider_document_id,
            raw_file_hash=raw.raw_file_hash,
            occurred_at=now,
        )
    )
    return RegisterDocumentOutcome(
        document=document,
        source_access=source_access,
        outbox_event=event,
        reused_existing_document=False,
    )


def _recovery_provenance(
    *,
    doc_meta: DocumentRegistration,
    subject: ResolvedSubject,
    raw: RawDocumentWriteResult,
) -> RetainedRecoveryProvenance | None:
    provenance = doc_meta.provenance
    is_recovery_interface = doc_meta.provider_interface == RETAINED_REGISTRATION_INTERFACE
    if not isinstance(provenance, RetainedRecoveryProvenance):
        if is_recovery_interface:
            raise SourceRecoveryError(
                "RECOVERY_PROVENANCE_REQUIRED",
                "a retained registration receipt needs its verified recovery provenance",
            )
        return None
    if not is_recovery_interface:
        raise SourceRecoveryError(
            "RECOVERY_INTERFACE_REQUIRED",
            f"retained recovery must register through {RETAINED_REGISTRATION_INTERFACE}",
        )
    if (
        raw.created
        or raw.raw_file_hash != provenance.raw_file_hash
        or str(raw.relpath) != provenance.raw_file_relpath
        or raw.byte_count != provenance.byte_count
    ):
        raise SourceRecoveryError(
            "RECOVERY_RAW_MISMATCH",
            "retained raw differs from the verified archive of "
            f"{provenance.recovery_of_source_access_id}",
        )
    if (
        subject.security.security_id == provenance.acquisition.acquisition_scope_security_id
        or subject.security.security_code != provenance.acquisition.original_candidate_code
    ):
        raise SourceRecoveryError(
            "RECOVERY_SUBJECT_MISMATCH",
            "retained recovery subject is not the verified historical security",
        )
    return provenance


def _require_recovery_target(
    uow: UnitOfWork,
    *,
    existing: e.Document | None,
    subject: ResolvedSubject,
    doc_meta: DocumentRegistration,
    raw: RawDocumentWriteResult,
) -> None:
    if existing is not None:
        if (
            existing.company_id != subject.company.company_id
            or existing.security_id != subject.security.security_id
            or existing.raw_file_relpath != str(raw.relpath)
        ):
            raise SourceRecoveryError(
                "RECOVERY_DOCUMENT_SUBJECT_CONFLICT",
                f"document {existing.document_id} already registers these bytes "
                "under another subject or archive path",
            )
        return
    latest = uow.documents.latest_by_provider_document(
        provider=doc_meta.provider,
        provider_document_id=doc_meta.provider_document_id,
    )
    if latest is not None:
        # A retained archive must not supersede a different registered
        # version by insertion time.
        raise SourceRecoveryError(
            "RECOVERY_NEWER_VERSION_EXISTS",
            f"document {latest.document_id} already registers another raw "
            f"version of {doc_meta.provider_document_id}",
        )


def _add_source_access(
    *,
    uow: UnitOfWork,
    subject: ResolvedSubject,
    doc_meta: DocumentRegistration,
    raw: RawDocumentWriteResult,
    now: datetime,
    recovery: RetainedRecoveryProvenance | None,
) -> e.SourceAccess:
    query_params: dict[str, object] = {
        "provider_document_id": doc_meta.provider_document_id,
        "filename": doc_meta.filename,
    }
    result_snapshot: dict[str, object] = {
        "byte_count": raw.byte_count,
        "raw_created": raw.created,
    }
    provenance = doc_meta.provenance
    acquisition = (
        provenance.acquisition
        if isinstance(provenance, RetainedRecoveryProvenance)
        else provenance
    )
    if isinstance(acquisition, HistoricalAcquisitionProvenance):
        # Lineage lives under its own keys; the reserved keys above are
        # never taken from a caller-supplied mapping.
        query_params["index_source_access_id"] = acquisition.index_source_access_id
        result_snapshot["acquisition_provenance"] = acquisition.snapshot()
    if recovery is not None:
        result_snapshot["retained_registration"] = recovery.snapshot()
    return uow.source_accesses.add(
        e.SourceAccess(
            source_access_id=ids.new_source_access_id(),
            provider=doc_meta.provider,
            provider_interface=doc_meta.provider_interface,
            dataset_key=doc_meta.dataset_key,
            query_params=query_params,
            accessed_at=now,
            status="ok",
            result_hash=raw.raw_file_hash,
            result_snapshot=result_snapshot,
            company_id=subject.company.company_id,
            security_id=subject.security.security_id,
            recovery_of_source_access_id=(
                recovery.recovery_of_source_access_id if recovery is not None else None
            ),
        )
    )
