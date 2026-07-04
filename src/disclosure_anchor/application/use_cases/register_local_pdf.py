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
from disclosure_anchor.application.services.register_document import (
    DocumentRegistration,
    register_document,
)
from disclosure_anchor.application.services.subject_resolver import (
    SubjectCandidate,
    SubjectResolver,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import (
    DocumentIdentityConflictError,
    InvalidRawDocumentError,
    RegistrationMetadataError,
    SubjectIdentityConflictError,
    SubjectIdentityRaceError,
)
from disclosure_anchor.domain.value_objects import (
    ReportPeriod,
    validate_filing_type,
    validate_official_provider,
    validate_report_period_for_filing_type,
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
    provider_document_id: str
    provider: str
    report_period: ReportPeriod | None = None
    board: str | None = None
    company_credit_code: str | None = None
    expected_raw_file_hash: str | None = None

    def __post_init__(self) -> None:
        validate_official_provider(self.provider)
        validate_filing_type(self.filing_type)
        report_period = self.report_period
        if isinstance(report_period, str):
            report_period = ReportPeriod.parse(report_period)
            object.__setattr__(self, "report_period", report_period)
        validate_report_period_for_filing_type(
            filing_type=self.filing_type, report_period=report_period
        )


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
        subject_resolver: SubjectResolver | None = None,
    ) -> None:
        self._raw_store = raw_store
        self._uow_factory = uow_factory
        self._subject_resolver = subject_resolver or SubjectResolver()

    def execute(self, command: RegisterLocalPdfCommand) -> RegisterLocalPdfResult:
        self._preflight_existing_subject(command)

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
            source_access = self._record_quarantine_source_access(
                command=command,
                reason=str(exc),
                quarantine=quarantine,
            )
            return RegisterLocalPdfResult(
                document_id=None,
                raw_file_relpath=None,
                raw_file_hash=None,
                source_access_id=source_access.source_access_id,
                outbox_event_id=None,
                quarantined_path=quarantine.path,
                quarantine_reason=quarantine.reason,
            )

        return self._register_after_raw_archive_with_retry(command=command, raw=raw)

    def _register_after_raw_archive_with_retry(
        self, *, command: RegisterLocalPdfCommand, raw: RawDocumentWriteResult
    ) -> RegisterLocalPdfResult:
        try:
            return self._register_after_raw_archive(command=command, raw=raw)
        except (DocumentIdentityConflictError, SubjectIdentityRaceError):
            return self._register_after_raw_archive(command=command, raw=raw)

    def _register_after_raw_archive(
        self, *, command: RegisterLocalPdfCommand, raw: RawDocumentWriteResult
    ) -> RegisterLocalPdfResult:
        with self._uow_factory() as uow:
            subject = self._subject_resolver.resolve(
                uow,
                SubjectCandidate(
                    security_code=command.security_code,
                    exchange=command.exchange,
                    board=command.board,
                    legal_name=command.company_legal_name,
                    credit_code=command.company_credit_code,
                ),
            )
            outcome = register_document(
                uow,
                subject=subject,
                doc_meta=DocumentRegistration(
                    provider=command.provider,
                    provider_document_id=command.provider_document_id,
                    title=command.title,
                    filing_type=command.filing_type,
                    announcement_date=command.announcement_date,
                    report_period=command.report_period,
                    filename=command.file_path.name,
                ),
                raw=raw,
            )
            uow.commit()

        return RegisterLocalPdfResult(
            document_id=outcome.document.document_id,
            raw_file_relpath=outcome.document.raw_file_relpath,
            raw_file_hash=outcome.document.raw_file_hash,
            source_access_id=outcome.source_access.source_access_id,
            outbox_event_id=outcome.outbox_event.event_id,
            reused_existing_document=outcome.reused_existing_document,
        )

    def _preflight_existing_subject(self, command: RegisterLocalPdfCommand) -> None:
        with self._uow_factory() as uow:
            security_company: e.Company | None = None
            security = uow.securities.get_by_code_exchange(
                command.security_code, command.exchange
            )
            if security is not None:
                security_company = self._company_for_existing_security(
                    uow=uow, security=security
                )
            normalized_credit_code = _normalize_credit_code(command.company_credit_code)
            if normalized_credit_code:
                identifier = uow.company_identifiers.get_by_scheme_value(
                    "uscc", normalized_credit_code
                )
                if identifier is not None:
                    company = uow.companies.get(identifier.company_id)
                    if company is None:
                        raise RegistrationMetadataError(
                            "company identifier references missing company "
                            f"{identifier.company_id}"
                        )
                    if (
                        security_company is not None
                        and identifier.company_id != security_company.company_id
                    ):
                        self._contest_identifier_and_raise(
                            uow,
                            identifier=identifier,
                            message=(
                                "uscc strong identifier belongs to a different company"
                            ),
                        )
                    if company.legal_name != command.company_legal_name:
                        self._contest_identifier_and_raise(
                            uow,
                            identifier=identifier,
                            message=(
                                "subject legal_name mismatch: "
                                f"uscc belongs to {company.legal_name!r}, "
                                f"got {command.company_legal_name!r}"
                            ),
                        )
                if (
                    security_company is not None
                    and security_company.unified_social_credit_code
                    and _normalize_credit_code(
                        security_company.unified_social_credit_code
                    )
                    != normalized_credit_code
                ):
                    self._add_contested_identifier_and_raise(
                        uow,
                        company=security_company,
                        credit_code=command.company_credit_code,
                        message=(
                            "company unified_social_credit_code conflicts with "
                            "candidate uscc"
                        ),
                    )
                if (
                    security_company is not None
                    and security_company.legal_name != command.company_legal_name
                ):
                    self._add_contested_identifier_and_raise(
                        uow,
                        company=security_company,
                        credit_code=command.company_credit_code,
                        message=(
                            "security/company mismatch: "
                            f"{command.security_code}.{command.exchange} belongs to "
                            f"{security_company.legal_name!r}, "
                            f"got {command.company_legal_name!r}"
                        ),
                    )
            if (
                security_company is not None
                and security_company.legal_name != command.company_legal_name
            ):
                raise SubjectIdentityConflictError(
                    "security/company mismatch: "
                    f"{command.security_code}.{command.exchange} belongs to "
                    f"{security_company.legal_name!r}, "
                    f"got {command.company_legal_name!r}"
                )

    @staticmethod
    def _contest_identifier_and_raise(
        uow: UnitOfWork, *, identifier: e.CompanyIdentifier, message: str
    ) -> None:
        identifier.status = "contested"
        uow.company_identifiers.update(identifier)
        uow.commit()
        raise SubjectIdentityConflictError(message)

    @staticmethod
    def _add_contested_identifier_and_raise(
        uow: UnitOfWork, *, company: e.Company, credit_code: str, message: str
    ) -> None:
        uow.company_identifiers.add(
            e.CompanyIdentifier(
                identifier_id=ids.new_company_identifier_id(),
                company_id=company.company_id,
                scheme="uscc",
                raw_value=credit_code,
                normalized_value=_normalize_credit_code(credit_code) or credit_code,
                jurisdiction="CN",
                status="contested",
                observed_at=company.created_at or datetime.now(timezone.utc),
            )
        )
        uow.commit()
        raise SubjectIdentityConflictError(message)

    def _record_quarantine_source_access(
        self, *, command: RegisterLocalPdfCommand, reason: str, quarantine
    ):
        now = datetime.now(timezone.utc)
        with self._uow_factory() as uow:
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
                    status="failed",
                    error=reason,
                    result_snapshot={
                        "quarantine_reason": quarantine.reason,
                        "quarantine_path": str(quarantine.path),
                        "byte_count": quarantine.byte_count,
                    },
                )
            )
            uow.commit()
            return source_access

    @staticmethod
    def _company_for_existing_security(
        *,
        uow: UnitOfWork,
        security: e.Security,
    ) -> e.Company:
        company = uow.companies.get(security.company_id)
        if company is None:
            raise RegistrationMetadataError(
                f"security {security.security_id} references missing company "
                f"{security.company_id}"
            )
        return company


def _normalize_credit_code(value: str | None) -> str | None:
    return value.strip().upper() if value else None
