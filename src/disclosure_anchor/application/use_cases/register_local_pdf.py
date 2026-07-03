"""Register a local PDF into the raw archive and document table."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from disclosure_anchor.application.ports.file_store import (
    RawDocumentStorePort,
    RawDocumentWriteResult,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import (
    DocumentIdentityConflictError,
    InvalidRawDocumentError,
    RegistrationMetadataError,
)


@dataclass(frozen=True)
class RegisterLocalPdfCommand:
    file_path: Path
    company_legal_name: str
    security_code: str
    exchange: str
    filing_type: str
    title: str
    announcement_date: date
    report_period: str
    provider_document_id: str
    provider: str = "local"
    board: str | None = None
    company_credit_code: str | None = None
    expected_raw_file_hash: str | None = None


@dataclass(frozen=True)
class RegisterLocalPdfResult:
    document_id: str | None
    raw_file_relpath: str | None
    raw_file_hash: str | None
    source_access_id: str | None
    outbox_event_id: str | None
    reused_existing_document: bool = False
    quarantined_path: Path | None = None
    quarantine_reason: str | None = None


class RegisterLocalPdf:
    """Use case for Phase 03 local PDF registration."""

    def __init__(
        self,
        *,
        raw_store: RawDocumentStorePort,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        self._raw_store = raw_store
        self._uow_factory = uow_factory

    def execute(self, command: RegisterLocalPdfCommand) -> RegisterLocalPdfResult:
        self._preflight_existing_security(command)

        try:
            raw = self._raw_store.put_raw_document(
                provider=command.provider,
                security_code=command.security_code,
                year=command.announcement_date.year,
                provider_document_id=command.provider_document_id,
                input_file=command.file_path,
                expected_raw_file_hash=command.expected_raw_file_hash,
            )
        except InvalidRawDocumentError as exc:
            quarantine = self._raw_store.quarantine_raw_document(
                provider=command.provider,
                provider_document_id=command.provider_document_id,
                input_file=command.file_path,
                reason="invalid_raw_document",
            )
            return RegisterLocalPdfResult(
                document_id=None,
                raw_file_relpath=None,
                raw_file_hash=None,
                source_access_id=None,
                outbox_event_id=None,
                quarantined_path=quarantine.path,
                quarantine_reason=str(exc),
            )

        try:
            return self._register_after_raw_archive(command=command, raw=raw)
        except DocumentIdentityConflictError:
            with self._uow_factory() as uow:
                existing = uow.documents.get_by_provider_document_and_hash(
                    provider=command.provider,
                    provider_document_id=command.provider_document_id,
                    raw_file_hash=raw.raw_file_hash,
                )
                if existing is not None:
                    return self._reused_existing_result(existing)
            raise

    def _register_after_raw_archive(
        self, *, command: RegisterLocalPdfCommand, raw: RawDocumentWriteResult
    ) -> RegisterLocalPdfResult:
        now = datetime.now(timezone.utc)
        with self._uow_factory() as uow:
            existing = uow.documents.get_by_provider_document_and_hash(
                provider=command.provider,
                provider_document_id=command.provider_document_id,
                raw_file_hash=raw.raw_file_hash,
            )
            if existing is not None:
                return self._reused_existing_result(existing)

            company = uow.companies.get_by_legal_name(command.company_legal_name)
            security = uow.securities.get_by_code_exchange(
                command.security_code, command.exchange
            )
            if security is not None:
                company = self._company_for_existing_security(
                    uow=uow, command=command, security=security
                )
            else:
                if company is None:
                    company = uow.companies.add(
                        e.Company(
                            company_id=ids.new_company_id(),
                            legal_name=command.company_legal_name,
                            unified_social_credit_code=command.company_credit_code,
                        )
                    )
                security = uow.securities.add(
                    e.Security(
                        security_id=ids.new_security_id(),
                        company_id=company.company_id,
                        security_code=command.security_code,
                        exchange=command.exchange,
                        board=command.board,
                        status="active",
                    )
                )

            source_access = uow.source_accesses.add(
                e.SourceAccess(
                    source_access_id=ids.new_source_access_id(),
                    provider=command.provider,
                    provider_interface="local:register_pdf",
                    dataset_key="local_pdf",
                    query_params={
                        "provider_document_id": command.provider_document_id,
                        "filename": command.file_path.name,
                    },
                    accessed_at=now,
                    status="ok",
                    result_hash=raw.raw_file_hash,
                    result_snapshot={
                        "byte_count": raw.byte_count,
                        "raw_created": raw.created,
                    },
                    company_id=company.company_id,
                    security_id=security.security_id,
                )
            )

            latest = uow.documents.latest_by_provider_document(
                provider=command.provider,
                provider_document_id=command.provider_document_id,
            )
            document = uow.documents.add(
                e.Document(
                    document_id=ids.new_document_id(),
                    status="registered",
                    company_id=company.company_id,
                    security_id=security.security_id,
                    source_access_id=source_access.source_access_id,
                    provider=command.provider,
                    provider_document_id=command.provider_document_id,
                    title=command.title,
                    filing_type=command.filing_type,
                    announcement_date=command.announcement_date,
                    report_period=command.report_period,
                    raw_file_relpath=str(raw.relpath),
                    raw_file_hash=raw.raw_file_hash,
                    supersedes_document_id=latest.document_id if latest else None,
                )
            )

            event = uow.outbox.add(
                e.OutboxEvent(
                    event_id=ids.new_outbox_event_id(),
                    event_kind="document_registered",
                    document_id=document.document_id,
                    payload={
                        "provider": command.provider,
                        "provider_document_id": command.provider_document_id,
                        "raw_file_hash": raw.raw_file_hash,
                    },
                )
            )
            uow.commit()

        return RegisterLocalPdfResult(
            document_id=document.document_id,
            raw_file_relpath=document.raw_file_relpath,
            raw_file_hash=document.raw_file_hash,
            source_access_id=source_access.source_access_id,
            outbox_event_id=event.event_id,
            reused_existing_document=False,
        )

    def _preflight_existing_security(self, command: RegisterLocalPdfCommand) -> None:
        with self._uow_factory() as uow:
            security = uow.securities.get_by_code_exchange(
                command.security_code, command.exchange
            )
            if security is not None:
                self._company_for_existing_security(
                    uow=uow, command=command, security=security
                )

    @staticmethod
    def _reused_existing_result(existing: e.Document) -> RegisterLocalPdfResult:
        return RegisterLocalPdfResult(
            document_id=existing.document_id,
            raw_file_relpath=existing.raw_file_relpath,
            raw_file_hash=existing.raw_file_hash,
            source_access_id=existing.source_access_id,
            outbox_event_id=None,
            reused_existing_document=True,
        )

    @staticmethod
    def _company_for_existing_security(
        *,
        uow: UnitOfWork,
        command: RegisterLocalPdfCommand,
        security: e.Security,
    ) -> e.Company:
        company = uow.companies.get(security.company_id)
        if company is None:
            raise RegistrationMetadataError(
                f"security {security.security_id} references missing company "
                f"{security.company_id}"
            )
        if company.legal_name != command.company_legal_name:
            raise RegistrationMetadataError(
                "security/company mismatch: "
                f"{command.security_code}.{command.exchange} belongs to "
                f"{company.legal_name!r}, got {command.company_legal_name!r}"
            )
        return company
