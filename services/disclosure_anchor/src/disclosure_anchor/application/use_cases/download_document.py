"""Download persisted disclosure candidates and register archived PDFs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path

from disclosure_anchor.application.contracts.historical_security_registration import (
    ContractViolation,
    candidate_sha256,
)
from disclosure_anchor.application.ports.disclosure_source import AnnouncementRef, DisclosureSourcePort
from disclosure_anchor.application.ports.file_store import (
    AcquisitionCapacityError,
    FileStorePathPort,
    IncompletePdfDownloadError,
    PdfDownloadStaging,
    QuarantineResult,
    RawDocumentStorePort,
    RawDocumentWriteResult,
    SealedPdfDownload,
    storage_failure_text,
)
from disclosure_anchor.application.ports.repositories import PendingDownloadCandidate
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.register_document import (
    DocumentRegistration,
    register_document,
)
from disclosure_anchor.application.services.source_security_resolution import (
    RegistrationProvenance,
    resolve_acquisition_subject,
)
from disclosure_anchor.application.services.subject_resolver import SubjectResolver
from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    CNINFO_PROVIDER,
    INDEX_INTERFACE,
    WEB_INDEX_INTERFACE,
)
from disclosure_anchor.application.worker.locks import shared_corpus_writer
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import (
    DocumentIdentityConflictError,
    InvalidRawDocumentError,
    RawDocumentError,
    RegistrationMetadataError,
    SourceRequestError,
    SubjectIdentityConflictError,
    SubjectIdentityRaceError,
)
from disclosure_anchor.domain.value_objects import QuarantineReason, ReportPeriod


DOWNLOAD_INTERFACE = "cninfo:download_pdf"


@dataclass(frozen=True)
class DownloadDocumentCommand:
    candidate: Mapping[str, object]
    # The index SourceAccess that carried this candidate. Current securities
    # register without it; a historical security refuses to register without
    # it, because only that access proves where the candidate came from.
    index_source_access_id: str | None = None

    def __post_init__(self) -> None:
        candidate = self.candidate
        if isinstance(candidate, PendingDownloadCandidate):
            if (
                self.index_source_access_id is not None
                and self.index_source_access_id != candidate.index_source_access_id
            ):
                raise ValueError("index_source_access_id contradicts the pending candidate")
            object.__setattr__(
                self, "index_source_access_id", candidate.index_source_access_id
            )
            object.__setattr__(self, "candidate", candidate.candidate)


@dataclass(frozen=True)
class DownloadDocumentResult:
    provider_document_id: str
    document_id: str | None
    source_access_id: str
    raw_file_hash: str | None
    reused_existing_document: bool = False
    quarantined_path: Path | None = None
    quarantine_reason: str | None = None
    error_code: str | None = None
    retryable: bool | None = None


class DownloadDocument:
    """Stream one candidate into owned staging, archive it, reuse register_document."""

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
    ) -> list[PendingDownloadCandidate]:
        with self._uow_factory() as uow:
            return uow.source_accesses.list_pending_download_candidates(
                provider=CNINFO_PROVIDER,
                index_interfaces=(INDEX_INTERFACE, WEB_INDEX_INTERFACE),
                download_interface=DOWNLOAD_INTERFACE,
                max_retries=max_retries,
                overlap_start=overlap_start,
            )

    def execute(self, command: DownloadDocumentCommand) -> DownloadDocumentResult:
        # Provider bytes, tmp/quarantine/raw files, and their database
        # ownership record are admitted as one corpus-write lifecycle.
        with shared_corpus_writer(self._uow_factory):
            return self._execute_admitted(command)

    def _execute_admitted(
        self, command: DownloadDocumentCommand
    ) -> DownloadDocumentResult:
        candidate = command.candidate
        # Every failure below must land as a status='failed' download
        # source_access: the retry budget and the dead-letter cutoff in
        # ops.pending_download_v1 count exactly those rows — an escaping
        # exception means an uncounted, endlessly re-downloaded candidate
        # (round23). Only environment-level failures may escape: the
        # database is down, or the volume is already below its free floor
        # before any provider contact.
        try:
            ref = _ref_from_candidate(candidate)
        except (RegistrationMetadataError, ValueError) as exc:
            return self._failed_result(
                candidate=candidate,
                error_code="invalid_candidate_snapshot",
                retryable=False,
                reason=str(exc),
                failure_phase="candidate",
                index_source_access_id=command.index_source_access_id,
            )
        tmp_path = self._paths.runtime_tmp_path(f"cninfo_{ids.new_ulid()}.pdf")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        # Opening checks the free floor first; that refusal escapes rather
        # than spend this candidate's retry budget on local disk state.
        download = self._raw_store.open_pdf_download(tmp_path)
        try:
            return self._download_and_register(
                candidate=candidate, ref=ref, command=command, download=download
            )
        finally:
            # A partial is removed; a sealed file nobody else holds is kept.
            download.close()

    def _download_and_register(
        self,
        *,
        candidate: Mapping[str, object],
        ref: AnnouncementRef,
        command: DownloadDocumentCommand,
        download: PdfDownloadStaging,
    ) -> DownloadDocumentResult:
        try:
            transfer = self._source.download_pdf_to(ref, download)
            sealed = download.seal(transfer)
        except SourceRequestError as exc:
            source_access = self._record_failed_download(
                candidate=candidate,
                error={
                    **exc.to_error(
                        stage="download",
                        provider_document_id=ref.provider_document_id,
                    ),
                    "failure_phase": "download",
                },
                snapshot={"reason": str(exc), "transfer": _transfer_progress(download)},
                index_source_access_id=command.index_source_access_id,
            )
            return DownloadDocumentResult(
                provider_document_id=ref.provider_document_id,
                document_id=None,
                source_access_id=source_access.source_access_id,
                raw_file_hash=None,
                error_code=exc.error_code,
                retryable=exc.retryable,
            )
        except (AcquisitionCapacityError, IncompletePdfDownloadError) as exc:
            detail: dict[str, object] = {"transfer": _transfer_progress(download)}
            if isinstance(exc, AcquisitionCapacityError):
                detail["capacity"] = exc.snapshot()
            return self._failed_result(
                candidate=candidate,
                error_code=exc.error_code,
                retryable=exc.retryable,
                reason=str(exc),
                extra_snapshot=detail,
                failure_phase="download",
                index_source_access_id=command.index_source_access_id,
            )
        except OSError as exc:
            return self._failed_result(
                candidate=candidate,
                error_code="io_error",
                retryable=True,
                reason=storage_failure_text(exc),
                extra_snapshot={"transfer": _transfer_progress(download)},
                failure_phase="download",
                index_source_access_id=command.index_source_access_id,
            )
        try:
            # The archive applies the free floor to its own copy, so an address
            # it already holds is reused without needing any space.
            raw = self._raw_store.put_raw_document(
                provider=CNINFO_PROVIDER,
                security_code=ref.security_code,
                year=ref.announcement_date.year,
                provider_document_id=ref.provider_document_id,
                input_file=sealed.path,
                expected_raw_file_hash=sealed.raw_file_hash,
            )
        except AcquisitionCapacityError as exc:
            # Nothing else holds these bytes yet: the sealed file is retained
            # (a later download is not a durable copy now).
            return self._failed_result(
                candidate=candidate,
                error_code=exc.error_code,
                retryable=exc.retryable,
                reason=str(exc),
                extra_snapshot={"capacity": exc.snapshot(), **_retained(sealed)},
                failure_phase="archive",
                index_source_access_id=command.index_source_access_id,
            )
        except InvalidRawDocumentError as exc:
            quarantine, detail = self._quarantine(
                ref=ref, sealed=sealed, download=download, reason="invalid_raw_document"
            )
            source_access = self._record_failed_download(
                candidate=candidate,
                error={
                    "stage": "download",
                    "error_code": "invalid_raw_document",
                    "retryable": False,
                    "provider_document_id": ref.provider_document_id,
                    "failure_phase": "archive",
                },
                snapshot={"reason": str(exc), **detail},
                index_source_access_id=command.index_source_access_id,
            )
            return DownloadDocumentResult(
                provider_document_id=ref.provider_document_id,
                document_id=None,
                source_access_id=source_access.source_access_id,
                raw_file_hash=None,
                quarantined_path=quarantine.path if quarantine else None,
                quarantine_reason=quarantine.reason if quarantine else None,
                error_code="invalid_raw_document",
                retryable=False,
            )
        except RawDocumentError as exc:
            # Archive-level inconsistency (e.g. existing file at the hash
            # path with different content): keep the fresh bytes as
            # evidence, needs operator action — terminal.
            quarantine, detail = self._quarantine(
                ref=ref, sealed=sealed, download=download, reason="raw_archive_conflict"
            )
            return self._failed_result(
                candidate=candidate,
                error_code="raw_archive_error",
                retryable=False,
                reason=str(exc),
                extra_snapshot=detail,
                quarantine=quarantine,
                failure_phase="archive",
                index_source_access_id=command.index_source_access_id,
            )
        except OSError as exc:
            # Local I/O failed reading the seal or writing the archive; the
            # bytes may be held nowhere else, so keep the seal.
            return self._failed_result(
                candidate=candidate,
                error_code="io_error",
                retryable=True,
                reason=storage_failure_text(exc),
                extra_snapshot=_retained(sealed),
                failure_phase="archive",
                index_source_access_id=command.index_source_access_id,
            )
        # The archive holds these exact bytes now; staging is redundant.
        download.discard()
        return self._register_with_retry(
            candidate=candidate, ref=ref, raw=raw, command=command
        )

    def _quarantine(
        self,
        *,
        ref: AnnouncementRef,
        sealed: SealedPdfDownload,
        download: PdfDownloadStaging,
        reason: QuarantineReason,
    ) -> tuple[QuarantineResult | None, dict[str, object]]:
        """Quarantine a sealed download; drop it only once a full copy exists."""

        try:
            quarantine = self._raw_store.quarantine_raw_document(
                provider=CNINFO_PROVIDER,
                provider_document_id=ref.provider_document_id,
                input_file=sealed.path,
                reason=reason,
            )
        except OSError as exc:
            # The quarantine destination failed; the staged file stays the
            # only copy and is retained with the failure record.
            return None, {
                "quarantine_complete": False,
                "quarantine_error": storage_failure_text(exc),
                **_retained(sealed),
            }
        detail: dict[str, object] = {
            "quarantine_filename": quarantine.path.name,
            "byte_count": quarantine.byte_count,
            "quarantine_complete": quarantine.transfer_complete,
            "input_missing": quarantine.input_missing,
        }
        if quarantine.transfer_complete or quarantine.input_missing:
            download.discard()
        else:
            detail.update(_retained(sealed))
        return quarantine, detail

    def _register_with_retry(
        self,
        *,
        candidate: Mapping[str, object],
        ref: AnnouncementRef,
        raw: RawDocumentWriteResult,
        command: DownloadDocumentCommand,
    ) -> DownloadDocumentResult:
        try:
            try:
                return self._register(
                    candidate=candidate, ref=ref, raw=raw, command=command
                )
            except (DocumentIdentityConflictError, SubjectIdentityRaceError):
                # Same single-retry policy as RegisterLocalPdf: unique-index
                # races with a concurrent CLI/admin register resolve on reread.
                return self._register(
                    candidate=candidate, ref=ref, raw=raw, command=command
                )
        except (DocumentIdentityConflictError, SubjectIdentityRaceError) as exc:
            return self._failed_result(
                candidate=candidate,
                error_code=type(exc).__name__,
                retryable=True,
                reason=str(exc),
                failure_phase="registration",
                index_source_access_id=command.index_source_access_id,
                raw=raw,
            )
        except SubjectIdentityConflictError as exc:
            return self._failed_result(
                candidate=candidate,
                error_code="subject_identity_conflict",
                retryable=False,
                reason=str(exc),
                failure_phase="registration",
                index_source_access_id=command.index_source_access_id,
                raw=raw,
            )
        except RegistrationMetadataError as exc:
            return self._failed_result(
                candidate=candidate,
                error_code="registration_metadata_error",
                retryable=False,
                reason=str(exc),
                failure_phase="registration",
                index_source_access_id=command.index_source_access_id,
                raw=raw,
            )

    def _failed_result(
        self,
        *,
        candidate: Mapping[str, object],
        error_code: str,
        retryable: bool,
        reason: str,
        failure_phase: str,
        index_source_access_id: str | None,
        extra_snapshot: Mapping[str, object] | None = None,
        quarantine: QuarantineResult | None = None,
        raw: RawDocumentWriteResult | None = None,
    ) -> DownloadDocumentResult:
        provider_document_id = _candidate_lenient_str(candidate, "provider_document_id")
        snapshot: dict[str, object] = {"reason": reason}
        if extra_snapshot:
            snapshot.update(extra_snapshot)
        source_access = self._record_failed_download(
            candidate=candidate,
            error={
                "stage": "download",
                "error_code": error_code,
                "retryable": retryable,
                "provider_document_id": provider_document_id,
                "failure_phase": failure_phase,
            },
            snapshot=snapshot,
            index_source_access_id=index_source_access_id,
            raw=raw,
        )
        return DownloadDocumentResult(
            provider_document_id=provider_document_id,
            document_id=None,
            source_access_id=source_access.source_access_id,
            raw_file_hash=None,
            quarantined_path=quarantine.path if quarantine else None,
            quarantine_reason=quarantine.reason if quarantine else None,
            error_code=error_code,
            retryable=retryable,
        )

    def _register(
        self,
        *,
        candidate: Mapping[str, object],
        ref: AnnouncementRef,
        raw: RawDocumentWriteResult,
        command: DownloadDocumentCommand,
    ) -> DownloadDocumentResult:
        with self._uow_factory() as uow:
            verified = resolve_acquisition_subject(
                uow,
                candidate=candidate,
                index_source_access_id=command.index_source_access_id,
                subject_resolver=self._subject_resolver,
            )
            outcome = register_document(
                uow,
                subject=verified.subject,
                doc_meta=candidate_registration(
                    candidate,
                    ref=ref,
                    provider_interface=DOWNLOAD_INTERFACE,
                    dataset_key="p_info3015",
                    provenance=verified.provenance,
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
        index_source_access_id: str | None,
        raw: RawDocumentWriteResult | None = None,
    ) -> e.SourceAccess:
        query_params: dict[str, object] = {
            # Lenient lookups: the failure record itself must never
            # raise on a malformed candidate snapshot.
            "provider_document_id": _candidate_lenient_str(
                candidate, "provider_document_id"
            ),
            "download_url": _candidate_lenient_str(candidate, "download_url"),
        }
        if index_source_access_id is not None:
            query_params["index_source_access_id"] = index_source_access_id
        result_snapshot = dict(snapshot)
        # What was archived before the failure. A registration failure keeps
        # the known raw identity, so a later retained registration binds to
        # this record instead of to an after-the-fact directory inventory.
        result_snapshot["archive"] = (
            {
                "archive_completed": True,
                "raw_file_relpath": str(raw.relpath),
                "raw_file_hash": raw.raw_file_hash,
                "byte_count": raw.byte_count,
                "raw_created": raw.created,
            }
            if raw is not None
            else {"archive_completed": False}
        )
        identity = _candidate_identity(candidate)
        if identity is not None:
            result_snapshot["candidate_sha256"] = identity
        with self._uow_factory() as uow:
            source_access = uow.source_accesses.add(
                e.SourceAccess(
                    source_access_id=ids.new_source_access_id(),
                    provider=CNINFO_PROVIDER,
                    provider_interface=DOWNLOAD_INTERFACE,
                    dataset_key="p_info3015",
                    query_params=query_params,
                    accessed_at=datetime.now(timezone.utc),
                    status="failed",
                    error=_json(error),
                    result_snapshot=result_snapshot,
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


def candidate_registration(
    candidate: Mapping[str, object],
    *,
    provider_interface: str,
    dataset_key: str,
    provenance: RegistrationProvenance | None = None,
    ref: AnnouncementRef | None = None,
) -> DocumentRegistration:
    """Project one stored index candidate onto the shared register core.

    Downloads and retained registrations use this single projection, so
    title, dates, report period, provider metadata and signature hints are
    never re-derived or "corrected" on a recovery path.
    """

    ref = ref if ref is not None else _ref_from_candidate(candidate)
    return DocumentRegistration(
        provider=CNINFO_PROVIDER,
        provider_document_id=ref.provider_document_id,
        title=ref.title,
        announcement_date=ref.announcement_date,
        report_period=_candidate_report_period(candidate),
        filename=f"{ref.provider_document_id}.pdf",
        provider_metadata=_provider_metadata(candidate),
        provider_interface=provider_interface,
        dataset_key=dataset_key,
        provenance=provenance,
    )


def _transfer_progress(download: PdfDownloadStaging) -> dict[str, object]:
    """What an unfinished download had staged; never registered as raw."""

    return {
        "complete": False,
        "attempts": download.attempts,
        "attempt_byte_count": download.byte_count,
        "declared_byte_count": download.declared_byte_count,
    }


def _retained(sealed: SealedPdfDownload) -> dict[str, object]:
    """Identity of a sealed download kept because nothing else holds it."""

    return {
        "retained_filename": sealed.path.name,
        "retained_raw_file_hash": sealed.raw_file_hash,
        "retained_byte_count": sealed.byte_count,
    }


def _candidate_identity(candidate: Mapping[str, object]) -> str | None:
    """Canonical candidate hash for a failure record, when one exists."""

    try:
        return candidate_sha256(candidate)
    except (ContractViolation, TypeError, ValueError):
        # A malformed snapshot still gets its failure record; it simply has
        # no stable identity to carry.
        return None


def _provider_metadata(candidate: Mapping[str, object]) -> dict[str, object]:
    signature = dict(_candidate_mapping(candidate, "file_signature_hint"))
    return {
        "raw_category": _candidate_optional_str(candidate.get("raw_category")) or "",
        "category_names": candidate.get("category_names"),
        "provider_org_id": candidate.get("provider_org_id"),
        "object_id": candidate.get("object_id"),
        "rec_id": candidate.get("rec_id"),
        "security_name": candidate.get("security_name"),
        "file_signature": signature,
    }


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


def _candidate_lenient_str(candidate: Mapping[str, object], key: str) -> str:
    value = candidate.get(key)
    if value is None or value == "":
        return "unknown"
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
