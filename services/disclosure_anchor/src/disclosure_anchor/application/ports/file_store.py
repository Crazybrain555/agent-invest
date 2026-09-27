"""File-store port contracts."""

from __future__ import annotations

from collections.abc import Sequence

from pathlib import Path
from typing import Protocol

from dataclasses import dataclass

from disclosure_anchor.application.ports.disclosure_source import (
    CompletedPdfTransfer,
    PdfDownloadSink,
)
from disclosure_anchor.domain.value_objects import QuarantineReason


@dataclass(frozen=True)
class RawDocumentWriteResult:
    relpath: Path
    raw_file_hash: str
    byte_count: int
    created: bool


@dataclass(frozen=True)
class RetainedRawDocument:
    """One already archived raw file, located and hashed without any write."""

    relpath: Path
    raw_file_hash: str
    byte_count: int


@dataclass(frozen=True)
class RawDocumentVerification:
    relpath: Path
    expected_hash: str
    actual_hash: str | None
    ok: bool
    message: str


@dataclass(frozen=True)
class QuarantineResult:
    """Outcome of quarantining one input file.

    ``transfer_complete`` means every input byte was copied, fsynced and
    verified against an unchanged input, so the caller may remove its input.
    Otherwise ``path`` is an explicit empty marker described by its manifest;
    ``input_missing`` separates an absent input from one that could not be
    copied, which the caller must keep.
    """

    path: Path
    reason: QuarantineReason
    byte_count: int
    transfer_complete: bool = False
    input_missing: bool = False
    payload_sha256: str | None = None


class AcquisitionCapacityError(Exception):
    """A raw-acquisition write would leave its volume below the free floor.

    A physical resource verdict, not a document-size limit: the same
    document may fit once space is released.
    """

    error_code = "local_space_shortfall"
    retryable = True

    def __init__(
        self,
        *,
        phase: str,
        required_bytes: int,
        available_bytes: int,
        floor_bytes: int,
    ) -> None:
        self.phase = phase
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        self.floor_bytes = floor_bytes
        super().__init__(
            f"{phase} needs {required_bytes} bytes; {available_bytes} bytes are "
            f"free against a {floor_bytes}-byte free floor"
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "required_bytes": self.required_bytes,
            "available_bytes": self.available_bytes,
            "floor_bytes": self.floor_bytes,
        }


class IncompletePdfDownloadError(Exception):
    """The staged bytes do not match the transfer that claimed completion."""

    error_code = "incomplete_transfer"
    retryable = True


def storage_failure_text(exc: OSError) -> str:
    """Describe a storage ``OSError`` without the absolute paths it may carry."""

    if exc.errno is not None and exc.strerror:
        return f"{type(exc).__name__}: [Errno {exc.errno}] {exc.strerror}"
    return f"{type(exc).__name__}: {exc}"


@dataclass(frozen=True)
class SealedPdfDownload:
    """A complete, fsynced download file and the hash of exactly its bytes."""

    path: Path
    raw_file_hash: str
    byte_count: int


class PdfDownloadStaging(PdfDownloadSink, Protocol):
    """One owned, uncommitted download file under the runtime tmp root.

    Until ``seal`` the file is a partial that ``close`` removes. A sealed file
    is durable, complete material: only ``discard`` removes it, once the
    archive or a complete quarantine copy holds its bytes; otherwise ``close``
    retains it and returns its path.
    """

    @property
    def attempts(self) -> int:
        ...

    @property
    def byte_count(self) -> int:
        """Bytes written by the current attempt."""
        ...

    @property
    def declared_byte_count(self) -> int | None:
        ...

    def seal(self, transfer: CompletedPdfTransfer) -> SealedPdfDownload:
        ...

    def discard(self) -> None:
        ...

    def close(self) -> Path | None:
        ...


@dataclass(frozen=True)
class ArtifactWriteResult:
    relpath: Path
    artifact_hash: str
    byte_count: int


class FileStorePathPort(Protocol):
    def raw_document_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        year: int | str,
        provider_document_id: str,
        raw_file_hash: str,
        extension: str = ".pdf",
    ) -> Path:
        ...

    def raw_document_dir_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        year: int | str,
        provider_document_id: str,
    ) -> Path:
        ...

    def data_path(self, relpath: Path) -> Path:
        ...

    def parser_artifacts_root_relpath(self, *, document_id: str, processing_run_id: str) -> Path:
        ...

    def parser_run_artifacts_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        ...

    def parser_run_artifacts_v4_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
        source_pdf_sha256: str,
        parser_backend: str,
        parser_method: str,
    ) -> Path:
        ...

    def normalized_ir_relpath(self, *, document_id: str, processing_run_id: str) -> Path:
        ...

    def normalized_ir_run_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        ...

    def provider_document_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        artifact_owner_processing_run_id: str,
    ) -> Path:
        ...

    def document_units_snapshot_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        ...

    def semantic_route_receipts_v3_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        ...

    def atomic_publication_preparation_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        ...

    def atomic_publication_readiness_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        ...

    def runtime_tmp_path(self, name: str | None = None) -> Path:
        ...

    def runtime_quarantine_path(
        self,
        *,
        provider: str,
        provider_document_id: str,
        name: str,
    ) -> Path:
        ...


class RawDocumentStorePort(Protocol):
    def put_raw_document(
        self,
        *,
        provider: str,
        security_code: str,
        year: int | str,
        provider_document_id: str,
        input_file: Path,
        expected_raw_file_hash: str | None = None,
    ) -> RawDocumentWriteResult:
        """Archive ``input_file`` under its content address (no-replace).

        ``InvalidRawDocumentError``: the input is absent or not a regular file,
        has the wrong hash or is not a PDF. ``RawDocumentError``: the archive
        holds other bytes at that address or the input changed while copied.
        ``AcquisitionCapacityError`` (phase ``archive``) and ``OSError``: local
        storage refused or failed, reading the input or writing the archive;
        nothing is said about the input's bytes.
        """
        ...

    def verify_raw_document(
        self, *, relpath: Path, expected_hash: str
    ) -> RawDocumentVerification:
        ...

    def quarantine_raw_document(
        self,
        *,
        provider: str,
        provider_document_id: str,
        input_file: Path,
        reason: QuarantineReason,
    ) -> QuarantineResult:
        ...

    def open_pdf_download(self, path: Path) -> PdfDownloadStaging:
        """Create ``path`` exclusively as a download staging file.

        Raises ``AcquisitionCapacityError`` before creating anything when the
        volume is already below its free floor.
        """
        ...


class RetainedRawArchivePort(Protocol):
    """Read-only access to raw PDFs archived before a failed registration.

    Nothing here writes, moves or deletes: an existing archive file is the
    input, so a registration from it always reports ``created=False``.
    """

    def locate_retained_raw_document(
        self,
        *,
        provider: str,
        security_code: str,
        year: int | str,
        provider_document_id: str,
    ) -> RetainedRawDocument:
        """The single archived version in one provider document directory.

        Raises ``RawDocumentError`` for a missing or unsafe directory, a
        symlink or non-regular entry, any unexpected name, or anything other
        than exactly one ``sha256_<hex>.pdf`` whose bytes match its name.
        """
        ...

    def verify_retained_raw_document(
        self, *, relpath: Path, expected_hash: str, expected_byte_count: int
    ) -> RawDocumentWriteResult:
        """Re-hash one archive file read-only; the result has created=False."""
        ...


class ArtifactStorePort(Protocol):
    def write_json_atomic(self, *, relpath: Path, payload: object) -> ArtifactWriteResult:
        ...

    def write_jsonl_atomic(
        self, *, relpath: Path, rows: Sequence[object]
    ) -> ArtifactWriteResult:
        ...

    def write_text_atomic(self, *, relpath: Path, text: str) -> ArtifactWriteResult:
        ...
