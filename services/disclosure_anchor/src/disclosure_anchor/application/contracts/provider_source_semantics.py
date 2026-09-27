"""Source-bound semantic values shared by admission and non-publishing diagnostics.

These values validate source references; they are not admission capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re

from disclosure_anchor.application.contracts.provider_document import (
    ProviderBlock, ProviderDocument, ProviderPage,
)

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

NUL_SUBSTITUTION_POLICY = "provider_text_nul_substitution.v1"
_NUL = "\x00"
_REPLACEMENT_CHARACTER = "\ufffd"


def substitute_nul_characters(text: str) -> str:
    """Replace each U+0000 with one U+FFFD and keep every other character.

    PostgreSQL TEXT/JSONB cannot store U+0000, and the character that the
    provider lost is unknown, so the replacement marks the position without
    guessing it. The literal six-character escape ``\\u0000`` is ordinary text.
    """

    return text.replace(_NUL, _REPLACEMENT_CHARACTER)


@dataclass(frozen=True, slots=True)
class SourcePdfObservation:
    """Independent facts measured from one immutable source PDF."""

    sha256: str
    byte_count: int
    page_count: int

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("source PDF observation hash must be canonical")
        for value, label in (
            (self.byte_count, "byte count"),
            (self.page_count, "page count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    f"source PDF observation {label} must be positive"
                )


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
            "source_pdf_native_text_quality.v3": {
                "native_text_omission",
                "numeric_token_truncation",
            },
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
class ProviderTextSubstitution:
    """One provider payload whose U+0000 characters were marked, not repaired.

    Unlike ``SourceTextReconciliation`` this is not proven by the native PDF:
    it records that the stored text differs from the provider text only by
    ``occurrence_count`` U+0000 -> U+FFFD replacements.
    """

    source_index: int
    payload_ordinal: int
    raw_block_sha256: str
    provider_text_sha256: str
    substituted_text_sha256: str
    substituted_text: str
    occurrence_count: int
    policy: str = NUL_SUBSTITUTION_POLICY

    def __post_init__(self) -> None:
        for value in (self.source_index, self.payload_ordinal, self.occurrence_count):
            if type(value) is not int:
                raise ValueError("provider text substitution counts must be integers")
        if min(self.source_index, self.payload_ordinal) < 0:
            raise ValueError("provider text substitution indices cannot be negative")
        if self.policy != NUL_SUBSTITUTION_POLICY:
            raise ValueError("provider text substitution policy is unsupported")
        if not all(
            type(value) is str and _SHA256_RE.fullmatch(value)
            for value in (
                self.raw_block_sha256,
                self.provider_text_sha256,
                self.substituted_text_sha256,
            )
        ):
            raise ValueError("provider text substitution hashes must be canonical")
        if (
            type(self.substituted_text) is not str
            or not self.substituted_text
            or _NUL in self.substituted_text
            or _sha_text(self.substituted_text) != self.substituted_text_sha256
        ):
            raise ValueError("provider text substitution text hash drifted")
        if (
            self.occurrence_count < 1
            or self.substituted_text.count(_REPLACEMENT_CHARACTER)
            < self.occurrence_count
        ):
            raise ValueError("provider text substitution count is invalid")


@dataclass(frozen=True, slots=True)
class ProviderSourceSemantics:
    """Original provider projection and its validated source-derived semantics."""

    provider_document: ProviderDocument
    source_text_reconciliations: tuple[SourceTextReconciliation, ...] = ()
    source_quality_findings: tuple[SourceQualityFinding, ...] = ()
    text_substitutions: tuple[ProviderTextSubstitution, ...] = ()

    def __post_init__(self) -> None:
        validate_source_semantics(
            self.provider_document,
            self.source_text_reconciliations,
            self.source_quality_findings,
            self.text_substitutions,
        )

    @property
    def effective_provider_document(self) -> ProviderDocument:
        return effective_provider_document(
            self.provider_document,
            self.source_text_reconciliations,
            self.text_substitutions,
        )


def validate_source_semantics(
    document: ProviderDocument,
    reconciliations: tuple[SourceTextReconciliation, ...],
    findings: tuple[SourceQualityFinding, ...],
    substitutions: tuple[ProviderTextSubstitution, ...] = (),
) -> None:
    """Validate the exact ordered repair/finding/substitution preimage contract.

    Every provider payload holding U+0000 has exactly one substitution unless a
    native repair replaced that payload; a repaired text that still holds U+0000
    stays visible to the publication representability gate.
    """

    identities = [
        (item.source_index, item.payload_ordinal)
        for item in reconciliations
    ]
    if identities != sorted(identities) or len(identities) != len(set(identities)):
        raise ValueError(
            "source text reconciliations must be unique and source ordered"
        )
    finding_identities = [
        (item.source_index, item.payload_ordinal)
        for item in findings
    ]
    if finding_identities != sorted(finding_identities) or len(
        finding_identities
    ) != len(set(finding_identities)):
        raise ValueError("source quality findings must be unique and source ordered")
    if set(identities) & set(finding_identities):
        raise ValueError("source reconciliation and quality finding cannot overlap")
    blocks = document.blocks
    for item in reconciliations:
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
    for finding in findings:
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
    substitution_identities = [
        (item.source_index, item.payload_ordinal)
        for item in substitutions
    ]
    if substitution_identities != sorted(substitution_identities) or len(
        substitution_identities
    ) != len(set(substitution_identities)):
        raise ValueError("provider text substitutions must be unique and source ordered")
    if set(substitution_identities) & set(identities):
        raise ValueError("provider text substitution cannot overlap a source repair")
    for substitution in substitutions:
        if substitution.source_index >= len(blocks):
            raise ValueError("provider text substitution block is out of range")
        block = blocks[substitution.source_index]
        if (
            block.raw_item_sha256 != substitution.raw_block_sha256
            or substitution.payload_ordinal >= len(block.payloads)
        ):
            raise ValueError("provider text substitution differs from its provider")
        text = block.payloads[substitution.payload_ordinal].text
        if (
            _sha_text(text) != substitution.provider_text_sha256
            or text.count(_NUL) != substitution.occurrence_count
            or substitute_nul_characters(text) != substitution.substituted_text
        ):
            raise ValueError("provider text substitution differs from its provider")
    required = {
        (block.source_index, payload_ordinal)
        for block in blocks
        for payload_ordinal, payload in enumerate(block.payloads)
        if _NUL in payload.text
    } - set(identities)
    if required != set(substitution_identities):
        raise ValueError("provider text substitutions must cover every unrepaired U+0000")


def effective_provider_document(
    document: ProviderDocument,
    reconciliations: tuple[SourceTextReconciliation, ...],
    substitutions: tuple[ProviderTextSubstitution, ...] = (),
) -> ProviderDocument:
    """Apply validated repairs, then U+0000 markers, keeping original artifacts."""

    if not reconciliations and not substitutions:
        return document
    by_identity = {
        (item.source_index, item.payload_ordinal): item.source_text
        for item in reconciliations
    }
    for substitution in substitutions:
        identity = (substitution.source_index, substitution.payload_ordinal)
        if identity in by_identity:
            raise ValueError("provider text substitution cannot overlap a source repair")
        by_identity[identity] = substitution.substituted_text
    pages: list[ProviderPage] = []
    for page in document.pages:
        blocks: list[ProviderBlock] = []
        for block in page.blocks:
            payloads = tuple(
                replace(
                    payload,
                    text=by_identity[(block.source_index, payload_ordinal)],
                )
                if (block.source_index, payload_ordinal) in by_identity
                else payload
                for payload_ordinal, payload in enumerate(block.payloads)
            )
            blocks.append(replace(block, payloads=payloads))
        pages.append(replace(page, blocks=tuple(blocks)))
    return replace(document, pages=tuple(pages))


def _sha_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
