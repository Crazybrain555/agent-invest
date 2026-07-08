"""Download persisted disclosure candidates and register archived PDFs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path

from disclosure_anchor.application.ports.disclosure_source import AnnouncementRef, DisclosureSourcePort
from disclosure_anchor.application.ports.file_store import (
    FileStorePathPort,
    RawDocumentStorePort,
    RawDocumentWriteResult,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.register_document import (
    DocumentRegistration,
    register_document,
)
from disclosure_anchor.application.services.subject_resolver import (
    SubjectCandidate,
    SubjectResolver,
)
from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    CNINFO_PROVIDER,
    INDEX_INTERFACE,
    WEB_INDEX_INTERFACE,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import (
    InvalidRawDocumentError,
    RegistrationMetadataError,
    SourceRequestError,
)
from disclosure_anchor.domain.value_objects import ReportPeriod


DOWNLOAD_INTERFACE = "cninfo:download_pdf"


@dataclass(frozen=True)
class DownloadDocumentCommand:
    candidate: Mapping[str, object]
    oversized_kb: int = 10240


@dataclass(frozen=True)
class DownloadDocumentResult:
    provider_document_id: str
    document_id: str | None
    source_access_id: str
    raw_file_hash: str | None
    reused_existing_document: bool = False
    quarantined_path: Path | None = None
    quarantine_reason: str | None = None


class DownloadDocument:
    """Download one candidate, archive raw bytes, and reuse register_document."""

    def __init__(
        self,
        *,
        source: DisclosureSourcePort,
        raw_store: RawDocumentStorePort,
        path_builder: FileStorePathPort,
        uow_factory: Callable[[], UnitOfWork],
        subject_resolver: SubjectResolver | None = None,
    ) -> None:
        self._source = source
        self._raw_store = raw_store
        self._paths = path_builder
        self._uow_factory = uow_factory
        self._subject_resolver = subject_resolver or SubjectResolver()

    def list_pending_candidates(
        self, *, max_retries: int, overlap_start: date
    ) -> list[dict[str, object]]:
        with self._uow_factory() as uow:
            return uow.source_accesses.list_pending_download_candidates(
                provider=CNINFO_PROVIDER,
                index_interfaces=(INDEX_INTERFACE, WEB_INDEX_INTERFACE),
                download_interface=DOWNLOAD_INTERFACE,
                max_retries=max_retries,
                overlap_start=overlap_start,
            )

    def execute(self, command: DownloadDocumentCommand) -> DownloadDocumentResult:
        candidate = command.candidate
        ref = _ref_from_candidate(candidate)
        tmp_path = self._paths.runtime_tmp_path(f"cninfo_{ids.new_ulid()}.pdf")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            try:
                payload = self._source.download_pdf(ref)
            except SourceRequestError as exc:
                source_access = self._record_failed_download(
                    candidate=candidate,
                    error=exc.to_error(
                        stage="download",
                        provider_document_id=ref.provider_document_id,
                    ),
                    snapshot={"reason": str(exc)},
                )
                return DownloadDocumentResult(
                    provider_document_id=ref.provider_document_id,
                    document_id=None,
                    source_access_id=source_access.source_access_id,
                    raw_file_hash=None,
                )
            tmp_path.write_bytes(payload)
            try:
                raw = self._raw_store.put_raw_document(
                    provider=CNINFO_PROVIDER,
                    security_code=ref.security_code,
                    year=ref.announcement_date.year,
                    provider_document_id=ref.provider_document_id,
                    input_file=tmp_path,
                )
            except InvalidRawDocumentError as exc:
                quarantine = self._raw_store.quarantine_raw_document(
                    provider=CNINFO_PROVIDER,
                    provider_document_id=ref.provider_document_id,
                    input_file=tmp_path,
                    reason="invalid_raw_document",
                )
                source_access = self._record_failed_download(
                    candidate=candidate,
                    error={
                        "stage": "download",
                        "error_code": "invalid_raw_document",
                        "retryable": False,
                        "provider_document_id": ref.provider_document_id,
                    },
                    snapshot={
                        "reason": str(exc),
                        "quarantine_filename": quarantine.path.name,
                        "byte_count": quarantine.byte_count,
                    },
                )
                return DownloadDocumentResult(
                    provider_document_id=ref.provider_document_id,
                    document_id=None,
                    source_access_id=source_access.source_access_id,
                    raw_file_hash=None,
                    quarantined_path=quarantine.path,
                    quarantine_reason=quarantine.reason,
                )
            return self._register(candidate=candidate, ref=ref, raw=raw, command=command)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _register(
        self,
        *,
        candidate: Mapping[str, object],
        ref: AnnouncementRef,
        raw: RawDocumentWriteResult,
        command: DownloadDocumentCommand,
    ) -> DownloadDocumentResult:
        with self._uow_factory() as uow:
            subject_candidate = _subject_candidate_from_existing_security(
                uow=uow,
                ref=ref,
                exchange=_candidate_optional_str(candidate.get("exchange")),
            )
            subject = self._subject_resolver.resolve(uow, subject_candidate)
            outcome = register_document(
                uow,
                subject=subject,
                doc_meta=DocumentRegistration(
                    provider=CNINFO_PROVIDER,
                    provider_document_id=ref.provider_document_id,
                    title=ref.title,
                    announcement_date=ref.announcement_date,
                    report_period=_candidate_report_period(candidate),
                    filename=f"{ref.provider_document_id}.pdf",
                    provider_metadata=_provider_metadata(
                        candidate, oversized_kb=command.oversized_kb
                    ),
                    provider_interface=DOWNLOAD_INTERFACE,
                    dataset_key="p_info3015",
                ),
                raw=raw,
            )
            uow.commit()
        return DownloadDocumentResult(
            provider_document_id=ref.provider_document_id,
            document_id=outcome.document.document_id,
            source_access_id=outcome.source_access.source_access_id,
            raw_file_hash=outcome.document.raw_file_hash,
            reused_existing_document=outcome.reused_existing_document,
        )

    def _record_failed_download(
        self,
        *,
        candidate: Mapping[str, object],
        error: Mapping[str, object],
        snapshot: Mapping[str, object],
    ) -> e.SourceAccess:
        with self._uow_factory() as uow:
            source_access = uow.source_accesses.add(
                e.SourceAccess(
                    source_access_id=ids.new_source_access_id(),
                    provider=CNINFO_PROVIDER,
                    provider_interface=DOWNLOAD_INTERFACE,
                    dataset_key="p_info3015",
                    query_params={
                        "provider_document_id": _candidate_str(
                            candidate, "provider_document_id"
                        ),
                        "download_url": _candidate_str(candidate, "download_url"),
                    },
                    accessed_at=datetime.now(timezone.utc),
                    status="failed",
                    error=_json(error),
                    result_snapshot=dict(snapshot),
                )
            )
            uow.commit()
        return source_access


def _ref_from_candidate(candidate: Mapping[str, object]) -> AnnouncementRef:
    signature = _candidate_mapping(candidate, "file_signature_hint")
    index_updated_at = signature.get("index_updated_at")
    return AnnouncementRef(
        provider=CNINFO_PROVIDER,
        provider_document_id=_candidate_str(candidate, "provider_document_id"),
        title=_candidate_str(candidate, "title"),
        download_url=_candidate_str(candidate, "download_url"),
        # Empty on the web fallback channel (no F006V categories there).
        raw_category=_candidate_optional_str(candidate.get("raw_category")) or "",
        announcement_date=date.fromisoformat(_candidate_str(candidate, "announcement_date")),
        security_code=_candidate_str(candidate, "security_code"),
        security_name=_candidate_optional_str(candidate.get("security_name")),
        file_size=_candidate_file_size(signature.get("file_size")),
        index_updated_at=(
            datetime.fromisoformat(index_updated_at)
            if isinstance(index_updated_at, str)
            else None
        ),
        object_id=_candidate_optional_int_or_str(candidate.get("object_id")),
        rec_id=_candidate_optional_str(candidate.get("rec_id")),
    )


def _subject_candidate_from_existing_security(
    *, uow: UnitOfWork, ref: AnnouncementRef, exchange: str | None
) -> SubjectCandidate:
    exchanges = [exchange] if exchange else []
    exchanges.extend(item for item in ("SZSE", "SSE", "LOCAL") if item not in exchanges)
    security = None
    for candidate_exchange in exchanges:
        if candidate_exchange is None:
            continue
        security = uow.securities.get_by_code_exchange(
            ref.security_code, candidate_exchange
        )
        if security is not None:
            break
    if security is None:
        raise RegistrationMetadataError(
            f"security must be synced before download: {ref.security_code}"
        )
    company = uow.companies.get(security.company_id)
    if company is None:
        raise RegistrationMetadataError(
            f"security {security.security_id} references missing company"
        )
    return SubjectCandidate(
        security_code=security.security_code,
        exchange=security.exchange,
        legal_name=company.legal_name,
        credit_code=company.unified_social_credit_code,
    )


def _provider_metadata(
    candidate: Mapping[str, object], *, oversized_kb: int
) -> dict[str, object]:
    signature = dict(_candidate_mapping(candidate, "file_signature_hint"))
    metadata: dict[str, object] = {
        "raw_category": _candidate_optional_str(candidate.get("raw_category")) or "",
        "category_names": candidate.get("category_names"),
        "provider_org_id": candidate.get("provider_org_id"),
        "object_id": candidate.get("object_id"),
        "rec_id": candidate.get("rec_id"),
        "file_signature": signature,
    }
    file_size = signature.get("file_size")
    if isinstance(file_size, (int, float)) and file_size > oversized_kb:
        metadata["oversized"] = True
    return metadata


def _candidate_report_period(candidate: Mapping[str, object]) -> ReportPeriod | None:
    value = candidate.get("report_period")
    if not isinstance(value, str) or not value:
        return None
    try:
        return ReportPeriod.parse(value)
    except ValueError:
        # Null report_period must never block registration (07 §3.2).
        return None


def _candidate_mapping(candidate: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = candidate.get(key)
    if not isinstance(value, Mapping):
        raise RegistrationMetadataError(f"candidate missing mapping {key}")
    return value


def _candidate_str(
    candidate: Mapping[str, object], key: str, *, default: str | None = None
) -> str:
    value = candidate.get(key, default)
    if value is None or value == "":
        raise RegistrationMetadataError(f"candidate missing field {key}")
    return str(value)


def _candidate_optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _candidate_optional_int_or_str(value: object) -> int | str | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    return str(value)


def _candidate_file_size(value: object) -> int | float | str | None:
    if value is None or isinstance(value, (int, float, str)):
        return value
    return str(value)


def _json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
