"""Shared document registration core for local and provider download paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from disclosure_anchor.application.ports.file_store import RawDocumentWriteResult
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.subject_resolver import ResolvedSubject
from disclosure_anchor.application.worker.locks import maybe_lock_document
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.entities import outbox_events
from disclosure_anchor.domain import ids
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
    existing = uow.documents.get_by_provider_document_and_hash(
        provider=doc_meta.provider,
        provider_document_id=doc_meta.provider_document_id,
        raw_file_hash=raw.raw_file_hash,
    )
    source_access = _add_source_access(
        uow=uow,
        subject=subject,
        doc_meta=doc_meta,
        raw=raw,
        now=now,
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


def _add_source_access(
    *,
    uow: UnitOfWork,
    subject: ResolvedSubject,
    doc_meta: DocumentRegistration,
    raw: RawDocumentWriteResult,
    now: datetime,
) -> e.SourceAccess:
    return uow.source_accesses.add(
        e.SourceAccess(
            source_access_id=ids.new_source_access_id(),
            provider=doc_meta.provider,
            provider_interface=doc_meta.provider_interface,
            dataset_key=doc_meta.dataset_key,
            query_params={
                "provider_document_id": doc_meta.provider_document_id,
                "filename": doc_meta.filename,
            },
            accessed_at=now,
            status="ok",
            result_hash=raw.raw_file_hash,
            result_snapshot={
                "byte_count": raw.byte_count,
                "raw_created": raw.created,
            },
            company_id=subject.company.company_id,
            security_id=subject.security.security_id,
        )
    )
