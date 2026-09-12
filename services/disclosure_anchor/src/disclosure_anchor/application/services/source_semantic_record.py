"""Closed source-only evidence records with fresh pure semantic derivation.

Decoded data is not proof of a PDF read, process closure or production admission.
The separately typed candidate preserves divergent claims for later comparison.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from typing import TypeVar, cast

from disclosure_anchor.application.contracts._provider_content import (
    provider_document_from_payload, provider_document_to_payload,
    validate_provider_content,
)
from disclosure_anchor.application.contracts.diagnostic_json import (
    bounded_json_bytes, bounded_json_value, require_projection_budget,
)
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.contracts.provider_source_semantics import (
    ProviderSourceSemantics, SourcePdfObservation, SourcePdfTextObservation,
    SourceQualityFinding, SourceTextReconciliation,
)
from disclosure_anchor.application.services.provider_source_semantics import (
    derive_source_semantics,
)


SOURCE_SEMANTIC_RECORD_VERSION = "m6.source-semantic-record.v1"
SOURCE_SEMANTICS_VERSION = "provider_source_semantics.v1"


@dataclass(frozen=True, slots=True)
class _SourceSemanticData:
    source_observation: SourcePdfObservation
    target_identity: ParserTargetIdentity
    provider_document: ProviderDocument
    native_observations: tuple[SourcePdfTextObservation, ...]
    source_text_reconciliations: tuple[SourceTextReconciliation, ...]
    source_quality_findings: tuple[SourceQualityFinding, ...]
    record_sha256: str


@dataclass(frozen=True, slots=True)
class UntrustedSourceSemanticCandidate(_SourceSemanticData):
    """Well-formed claims only; cross-source/derivation drift remains visible."""


@dataclass(frozen=True, slots=True)
class DecodedSourceSemanticRecord(_SourceSemanticData):
    """Purely rederived and bound claims; no measured-read or IO capability."""

    @property
    def semantics(self) -> ProviderSourceSemantics:
        return ProviderSourceSemantics(
            self.provider_document, self.source_text_reconciliations,
            self.source_quality_findings,
        )


def encode_source_semantic_record(
    *, source_observation: SourcePdfObservation,
    target_identity: ParserTargetIdentity,
    provider_document: ProviderDocument,
    native_observations: tuple[SourcePdfTextObservation, ...],
    maximum_bytes: int,
) -> bytes:
    """Derive rather than accept caller-supplied repairs and quality findings."""
    if (
        type(source_observation) is not SourcePdfObservation
        or type(target_identity) is not ParserTargetIdentity
        or type(provider_document) is not ProviderDocument
        or type(native_observations) is not tuple
        or any(type(item) is not SourcePdfTextObservation for item in native_observations)
    ):
        raise ValueError("source semantic encoder requires exact source value types")
    require_projection_budget(
        (source_observation, target_identity, provider_document, native_observations),
        maximum_bytes=maximum_bytes,
    )
    semantics = derive_source_semantics(
        document=provider_document, observations=native_observations,
    )
    raw = bounded_json_bytes(
        {
            "contract_version": SOURCE_SEMANTIC_RECORD_VERSION,
            "mode": "service_diagnostic",
            "source_semantics_version": SOURCE_SEMANTICS_VERSION,
            "source_observation": asdict(source_observation),
            "target_identity": target_identity.to_payload(),
            "provider_document": provider_document_to_payload(provider_document),
            "native_observations": [asdict(item) for item in native_observations],
            "source_text_reconciliations": [
                asdict(item) for item in semantics.source_text_reconciliations
            ],
            "source_quality_findings": [
                asdict(item) for item in semantics.source_quality_findings
            ],
        },
        maximum_bytes=maximum_bytes,
    )
    # One validation path also rejects loose legacy DTO scalar constructors.
    decode_source_semantic_record(raw, maximum_bytes=maximum_bytes)
    return raw


def parse_source_semantic_candidate(
    raw: bytes, *, maximum_bytes: int,
) -> UntrustedSourceSemanticCandidate:
    """Parse immutable bytes without asserting their measured/semantic truth."""
    payload = _closed_object(
        bounded_json_value(raw, maximum_bytes=maximum_bytes),
        {
            "contract_version", "mode", "source_semantics_version",
            "source_observation", "target_identity", "provider_document",
            "native_observations", "source_text_reconciliations",
            "source_quality_findings",
        },
    )
    for name, expected in (
        ("contract_version", SOURCE_SEMANTIC_RECORD_VERSION),
        ("mode", "service_diagnostic"),
        ("source_semantics_version", SOURCE_SEMANTICS_VERSION),
    ):
        if type(payload[name]) is not str or payload[name] != expected:
            raise ValueError("source semantic record version/mode is unsupported")
    source = _source_value(SourcePdfObservation, payload["source_observation"])
    target = ParserTargetIdentity.from_payload(payload["target_identity"])
    document = provider_document_from_payload(payload["provider_document"])
    validate_provider_content(document, target)
    return UntrustedSourceSemanticCandidate(
        source, target, document,
        _source_values(SourcePdfTextObservation, payload["native_observations"]),
        _source_values(SourceTextReconciliation, payload["source_text_reconciliations"]),
        _source_values(SourceQualityFinding, payload["source_quality_findings"]),
        "sha256:" + hashlib.sha256(raw).hexdigest(),
    )


def decode_source_semantic_record(
    raw: bytes, *, maximum_bytes: int,
) -> DecodedSourceSemanticRecord:
    """Require exact source claims and fresh repair/finding rederivation."""
    candidate = parse_source_semantic_candidate(raw, maximum_bytes=maximum_bytes)
    source, document = candidate.source_observation, candidate.provider_document
    if (
        source.sha256 != document.source_pdf_sha256
        or source.page_count != len(document.pages)
    ):
        raise ValueError("source semantic record source/page claim differs from provider")
    derived = derive_source_semantics(
        document=document, observations=candidate.native_observations,
    )
    if (
        candidate.source_text_reconciliations != derived.source_text_reconciliations
        or candidate.source_quality_findings != derived.source_quality_findings
    ):
        raise ValueError("source semantic record repair/finding derivation differs")
    return DecodedSourceSemanticRecord(
        source, candidate.target_identity, document, candidate.native_observations,
        derived.source_text_reconciliations, derived.source_quality_findings,
        candidate.record_sha256,
    )


_SourceValue = TypeVar(
    "_SourceValue", SourcePdfObservation, SourcePdfTextObservation,
    SourceTextReconciliation, SourceQualityFinding,
)
_INTEGER_FIELDS = frozenset(
    {"byte_count", "page_count", "source_index", "page_index", "payload_ordinal"}
)


def _source_value(kind: type[_SourceValue], value: object) -> _SourceValue:
    fields = _closed_object(value, set(kind.__dataclass_fields__))
    for name, item in fields.items():
        if name in _INTEGER_FIELDS:
            if type(item) is not int or item < 0:
                raise ValueError("source semantic index/count must be a nonnegative integer")
        elif type(item) is not str:
            raise ValueError("source semantic text/hash/kind must be a string")
    # Each original value class validates its own hash/kind/reason vocabulary.
    return kind(**fields)  # type: ignore[arg-type]


def _source_values(kind: type[_SourceValue], value: object) -> tuple[_SourceValue, ...]:
    if type(value) is not list:
        raise ValueError("source semantic observations/claims must be arrays")
    return tuple(_source_value(kind, item) for item in value)


def _closed_object(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError("source semantic record fields are not closed")
    return cast(dict[str, object], value)
