"""Capability record produced only after a provider document is source-admitted."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
)


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SourcePdfObservation:
    """Independent facts measured from one immutable source PDF."""

    sha256: str
    page_count: int

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("source PDF observation hash must be canonical")
        if (
            isinstance(self.page_count, bool)
            or not isinstance(self.page_count, int)
            or self.page_count < 1
        ):
            raise ValueError("source PDF observation page count must be positive")


@dataclass(frozen=True, slots=True)
class AdmittedProviderDocument:
    """A canonical record whose typed projection was rebuilt from its bundle."""

    provider_document_relpath: Path
    provider_document_sha256: str
    envelope: ProviderDocumentEnvelope

    def __post_init__(self) -> None:
        if (
            self.provider_document_relpath.is_absolute()
            or ".." in self.provider_document_relpath.parts
        ):
            raise ValueError("admitted provider document path must be relative")
        if not _SHA256_RE.fullmatch(self.provider_document_sha256):
            raise ValueError("admitted provider document hash must be canonical")

    @property
    def provider_document(self) -> ProviderDocument:
        return self.envelope.provider_document


class ProviderDocumentAdmissionError(ValueError):
    """A parse-owned provider document failed the sole source-admission path."""

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = retryable


__all__ = [
    "AdmittedProviderDocument",
    "ProviderDocumentAdmissionError",
    "SourcePdfObservation",
]
