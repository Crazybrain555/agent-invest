"""Capability record produced only after a provider document is source-admitted."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re

from disclosure_anchor.application.contracts.provider_document import (
    ProviderBlock,
    ProviderDocument,
    ProviderPage,
)
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
class SourcePdfTextObservation:
    """Native PDF text measured inside one provider-owned block rectangle."""

    source_index: int
    page_index: int
    payload_ordinal: int
    raw_block_sha256: str
    text: str

    def __post_init__(self) -> None:
        if min(self.source_index, self.page_index, self.payload_ordinal) < 0:
            raise ValueError("source PDF text observation indices cannot be negative")
        if not _SHA256_RE.fullmatch(self.raw_block_sha256) or not self.text.strip():
            raise ValueError("source PDF text observation is not source-bound")


@dataclass(frozen=True, slots=True)
class SourceTextReconciliation:
    """One source-bound MinerU text correction proven by native PDF text."""

    source_index: int
    payload_ordinal: int
    raw_block_sha256: str
    provider_text_sha256: str
    source_text_sha256: str
    source_text: str
    source_kind: str = "source_pdf_native_numeric.v1"

    def __post_init__(self) -> None:
        if min(self.source_index, self.payload_ordinal) < 0:
            raise ValueError("source text reconciliation indices cannot be negative")
        if self.source_kind not in {
            "source_pdf_native_numeric.v1",
            "source_pdf_native_identifier.v1",
            "source_pdf_native_identifier.v2",
        }:
            raise ValueError("source text reconciliation kind is unsupported")
        if not all(
            _SHA256_RE.fullmatch(value)
            for value in (
                self.raw_block_sha256,
                self.provider_text_sha256,
                self.source_text_sha256,
            )
        ):
            raise ValueError("source text reconciliation hashes must be canonical")
        if not self.source_text or _sha_text(self.source_text) != self.source_text_sha256:
            raise ValueError("source text reconciliation text hash drifted")


@dataclass(frozen=True, slots=True)
class SourceQualityFinding:
    """One source-bound native-PDF mismatch that forbids silent trust."""

    source_index: int
    payload_ordinal: int
    raw_block_sha256: str
    provider_text_sha256: str
    source_text_sha256: str
    reason: str
    source_kind: str = "source_pdf_native_table_quality.v1"

    def __post_init__(self) -> None:
        if min(self.source_index, self.payload_ordinal) < 0:
            raise ValueError("source quality finding indices cannot be negative")
        allowed_reasons = {
            "source_pdf_native_table_quality.v1": {
                "empty_table_tail",
                "malformed_numeric_grouping",
                "numeric_token_mismatch",
            },
            "source_pdf_native_text_quality.v1": {"native_text_omission"},
            "source_pdf_native_identifier_quality.v1": {
                "identifier_confusable_mismatch"
            },
            "source_pdf_native_text_quality.v2": {"cjk_bracket_omission"},
        }
        if self.reason not in allowed_reasons.get(self.source_kind, set()):
            raise ValueError("source quality finding reason is unsupported")
        if not all(
            _SHA256_RE.fullmatch(value)
            for value in (
                self.raw_block_sha256,
                self.provider_text_sha256,
                self.source_text_sha256,
            )
        ):
            raise ValueError("source quality finding hash is invalid")


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
        identities = [
            (item.source_index, item.payload_ordinal)
            for item in self.source_text_reconciliations
        ]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError(
                "source text reconciliations must be unique and source ordered"
            )
        finding_identities = [
            (item.source_index, item.payload_ordinal)
            for item in self.source_quality_findings
        ]
        if finding_identities != sorted(finding_identities) or len(
            finding_identities
        ) != len(set(finding_identities)):
            raise ValueError("source quality findings must be unique and source ordered")
        if set(identities) & set(finding_identities):
            raise ValueError("source reconciliation and quality finding cannot overlap")
        blocks = self.envelope.provider_document.blocks
        for item in self.source_text_reconciliations:
            if item.source_index >= len(blocks):
                raise ValueError("source text reconciliation block is out of range")
            block = blocks[item.source_index]
            if (
                block.raw_item_sha256 != item.raw_block_sha256
                or item.payload_ordinal >= len(block.payloads)
                or _sha_text(block.payloads[item.payload_ordinal].text)
                != item.provider_text_sha256
            ):
                raise ValueError("source text reconciliation differs from its provider")
        for finding in self.source_quality_findings:
            if finding.source_index >= len(blocks):
                raise ValueError("source quality finding block is out of range")
            block = blocks[finding.source_index]
            if (
                block.raw_item_sha256 != finding.raw_block_sha256
                or finding.payload_ordinal >= len(block.payloads)
                or _sha_text(block.payloads[finding.payload_ordinal].text)
                != finding.provider_text_sha256
            ):
                raise ValueError("source quality finding differs from its provider")

    @property
    def provider_document(self) -> ProviderDocument:
        return self.envelope.provider_document

    @property
    def effective_provider_document(self) -> ProviderDocument:
        """Return the admitted semantic view with source-bound numeric repairs."""

        if not self.source_text_reconciliations:
            return self.provider_document
        by_identity = {
            (item.source_index, item.payload_ordinal): item
            for item in self.source_text_reconciliations
        }
        pages: list[ProviderPage] = []
        for page in self.provider_document.pages:
            blocks: list[ProviderBlock] = []
            for block in page.blocks:
                payloads = tuple(
                    replace(
                        payload,
                        text=by_identity[(block.source_index, payload_ordinal)].source_text,
                    )
                    if (block.source_index, payload_ordinal) in by_identity
                    else payload
                    for payload_ordinal, payload in enumerate(block.payloads)
                )
                blocks.append(replace(block, payloads=payloads))
            pages.append(replace(page, blocks=tuple(blocks)))
        return replace(self.provider_document, pages=tuple(pages))


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


def _sha_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
