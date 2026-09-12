"""Capability record produced only after a provider document is source-admitted."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from disclosure_anchor.application.contracts.provider_document import (
    ProviderDocument,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
)

from disclosure_anchor.application.contracts.provider_source_semantics import (
    SourcePdfObservation,
    SourcePdfTextObservation,
    SourceQualityFinding,
    SourceTextReconciliation,
    effective_provider_document,
    validate_source_semantics,
)


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class AdmittedProviderDocument:
    """A canonical record whose typed projection was rebuilt from its bundle."""

    provider_document_relpath: Path
    provider_document_sha256: str
    envelope: ProviderDocumentEnvelope
    source_text_reconciliations: tuple[SourceTextReconciliation, ...] = ()
    source_quality_findings: tuple[SourceQualityFinding, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.provider_document_relpath.is_absolute()
            or ".." in self.provider_document_relpath.parts
        ):
            raise ValueError("admitted provider document path must be relative")
        if not _SHA256_RE.fullmatch(self.provider_document_sha256):
            raise ValueError("admitted provider document hash must be canonical")
        validate_source_semantics(
            self.envelope.provider_document,
            self.source_text_reconciliations,
            self.source_quality_findings,
        )

    @property
    def provider_document(self) -> ProviderDocument:
        return self.envelope.provider_document

    @property
    def effective_provider_document(self) -> ProviderDocument:
        """Return the admitted semantic view with source-bound numeric repairs."""

        return effective_provider_document(
            self.provider_document, self.source_text_reconciliations
        )


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
    "SourcePdfTextObservation",
    "SourceQualityFinding",
    "SourceTextReconciliation",
]
