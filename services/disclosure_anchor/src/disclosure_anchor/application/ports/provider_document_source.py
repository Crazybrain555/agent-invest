"""Read-only source operations used by provider-document admission."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.contracts.provider_document_admission import (
    SourcePdfObservation,
    SourcePdfTextObservation,
)


class ProviderDocumentSourceError(RuntimeError):
    """A controlled filesystem or provider-bundle read failed."""

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = retryable


class SourcePdfTextReaderPort(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        document: ProviderDocument,
    ) -> tuple[SourcePdfTextObservation, ...]:
        ...


class ProviderDocumentSourcePort(Protocol):
    def read_provider_document_record(self, relpath: Path) -> bytes:
        ...

    def observe_source_pdf(self, relpath: Path) -> SourcePdfObservation:
        ...

    def rebuild_provider_document(
        self,
        bundle_relpath: Path,
        *,
        source_pdf_sha256: str,
    ) -> ProviderDocument:
        ...

    def observe_source_pdf_text(
        self,
        relpath: Path,
        *,
        document: ProviderDocument,
        expected_sha256: str,
    ) -> tuple[SourcePdfTextObservation, ...]:
        ...


__all__ = [
    "ProviderDocumentSourceError",
    "ProviderDocumentSourcePort",
    "SourcePdfTextReaderPort",
]
