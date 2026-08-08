"""Fail-closed OFCP terminals for provider observations and undecoded glyphs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias, Union
import unicodedata


PROVIDER_OBSERVATION_TERMINAL_VERSION = "provider-observation-terminal.v2"
GLYPH_TOKEN_CONTRACT_VERSION = "glyph-token.v1"
GLYPH_MAPPING_PROOF_VERSION = "glyph-mapping-proof.v1"
PUBLICATION_GATE_VERSION = "publication-gate.v1"
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_GLYPH_PLACEHOLDER_RE = re.compile(r"⟦未解码字形[^⟧]*⟧")
_CMAP_BFCHAR_BLOCK_RE = re.compile(
    rb"beginbfchar(?P<body>.*?)endbfchar", re.IGNORECASE | re.DOTALL
)
_CMAP_PAIR_RE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_GLYPH_PROOF_TOKEN = object()

BBox: TypeAlias = tuple[float, float, float, float]
SemanticPart: TypeAlias = Union[str, "GlyphToken"]


class ProviderObservationTerminalKind(StrEnum):
    SOURCE_BOUND_SUPPORT = "source_bound_support"
    ALIAS_OR_SUPPORT = "alias_or_support"
    REJECTED_OBSERVATION = "rejected_observation"
    NON_READER_VISIBLE = "non_reader_visible"
    ADVISORY_ONLY = "advisory_only"


class GlyphDecodeStatus(StrEnum):
    RESOLVED_FONT_BOUND = "resolved_font_bound"
    UNRESOLVED = "unresolved"


class GlyphMappingSource(StrEnum):
    PDF_TOUNICODE_BFCHAR = "pdf_tounicode_bfchar"


class PublicationSafetyError(ValueError):
    """Unsafe semantic text or a provider observation attempted publication."""


class AuditReceipt(Protocol):
    """Narrow input: the gate cannot inspect or rebuild source facts."""

    @property
    def ok(self) -> bool: ...

    @property
    def metrics(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PublicationGateEvaluation:
    decision: Literal["publish", "block"]
    checks: Mapping[str, bool]
    diagnostics: Mapping[str, int]
    contract_version: str = PUBLICATION_GATE_VERSION
    capability: str = "source-evidence-bounded-content-conservation"

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "capability": self.capability,
            "decision": self.decision,
            "checks": dict(self.checks),
            "diagnostics": dict(self.diagnostics),
        }


def evaluate_publication_gate_v1(
    report: AuditReceipt,
) -> PublicationGateEvaluation:
    """Turn one completed source audit into a strict publication decision."""

    metrics = report.metrics
    coverage = metrics.get("coverage")
    primary = metrics.get("primary_search")
    error_count = _metric_count(metrics.get("error_count"))
    uncovered = _metric_count(
        coverage.get("uncovered") if isinstance(coverage, Mapping) else None
    )
    primary_missing = _metric_count(
        primary.get("missing_carriers")
        if isinstance(primary, Mapping)
        else None
    )
    metric_shape = bool(
        isinstance(coverage, Mapping)
        and isinstance(primary, Mapping)
        and error_count >= 0
        and uncovered >= 0
        and primary_missing >= 0
    )
    diagnostics = {
        "error_count": error_count,
        "coverage_uncovered": uncovered,
        "primary_search_missing": primary_missing,
    }
    checks = {
        "metric_shape_closed": metric_shape,
        "audit_ok": report.ok is True,
        "error_count_zero": diagnostics["error_count"] == 0,
        "coverage_closed": diagnostics["coverage_uncovered"] == 0,
        "primary_search_closed": diagnostics["primary_search_missing"] == 0,
    }
    decision: Literal["publish", "block"] = (
        "publish" if all(checks.values()) else "block"
    )
    return PublicationGateEvaluation(
        decision=decision,
        checks=MappingProxyType(checks),
        diagnostics=MappingProxyType(diagnostics),
    )


def _metric_count(value: object) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else -1
    )


@dataclass(frozen=True, slots=True)
class GlyphMappingProof:
    proof_id: str
    source_pdf_sha256: str
    font_object_ref: str
    raw_code: str
    semantic_unicode: str
    mapping_source: GlyphMappingSource
    mapping_evidence_sha256: str
    matched_pair_sha256: str
    contract_version: str = GLYPH_MAPPING_PROOF_VERSION

    def __post_init__(self, _token: object = None) -> None:
        if _token is not _GLYPH_PROOF_TOKEN:
            raise PublicationSafetyError(
                "glyph mapping proof must be produced by a byte verifier"
            )

    @classmethod
    def _verified(
        cls,
        *,
        source_pdf_sha256: str,
        font_object_ref: str,
        raw_code: str,
        semantic_unicode: str,
        mapping_evidence_sha256: str,
        matched_pair_sha256: str,
    ) -> GlyphMappingProof:
        payload = {
            "contract_version": GLYPH_MAPPING_PROOF_VERSION,
            "font_object_ref": font_object_ref,
            "mapping_evidence_sha256": mapping_evidence_sha256,
            "mapping_source": GlyphMappingSource.PDF_TOUNICODE_BFCHAR.value,
            "matched_pair_sha256": matched_pair_sha256,
            "raw_code": raw_code,
            "semantic_unicode": semantic_unicode,
            "source_pdf_sha256": source_pdf_sha256,
        }
        proof = object.__new__(cls)
        object.__setattr__(proof, "proof_id", _canonical_sha256(payload))
        object.__setattr__(proof, "source_pdf_sha256", source_pdf_sha256)
        object.__setattr__(proof, "font_object_ref", font_object_ref)
        object.__setattr__(proof, "raw_code", raw_code)
        object.__setattr__(proof, "semantic_unicode", semantic_unicode)
        object.__setattr__(
            proof,
            "mapping_source",
            GlyphMappingSource.PDF_TOUNICODE_BFCHAR,
        )
        object.__setattr__(proof, "mapping_evidence_sha256", mapping_evidence_sha256)
        object.__setattr__(proof, "matched_pair_sha256", matched_pair_sha256)
        object.__setattr__(proof, "contract_version", GLYPH_MAPPING_PROOF_VERSION)
        proof.__post_init__(_GLYPH_PROOF_TOKEN)
        return proof

    def as_dict(self) -> dict[str, Any]:
        return {
            "proof_id": self.proof_id,
            "source_pdf_sha256": self.source_pdf_sha256,
            "font_object_ref": self.font_object_ref,
            "raw_code": self.raw_code,
            "semantic_unicode": self.semantic_unicode,
            "mapping_source": self.mapping_source.value,
            "mapping_evidence_sha256": self.mapping_evidence_sha256,
            "matched_pair_sha256": self.matched_pair_sha256,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True)
class ProviderObservationTerminal:
    observation_id: str
    artifact_bindings: Mapping[str, str]
    runtime_identity_sha256: str
    provenance_locator: Mapping[str, Any]
    terminal_kind: ProviderObservationTerminalKind
    bound_source_occurrence_id: str | None
    terminal_reason: str
    contract_version: str = PROVIDER_OBSERVATION_TERMINAL_VERSION
    semantic_authority: bool = False
    creates_reader_visible_occurrence: bool = False
    creates_placement_owner: bool = False
    creates_payload_leaf: bool = False
    search_policy: str = "none"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.terminal_kind, ProviderObservationTerminalKind)
            or _SHA256_RE.fullmatch(self.observation_id) is None
            or not self.artifact_bindings
            or any(
                not isinstance(role, str)
                or not role
                or _SHA256_RE.fullmatch(sha256) is None
                for role, sha256 in self.artifact_bindings.items()
            )
            or _SHA256_RE.fullmatch(self.runtime_identity_sha256) is None
            or not self.provenance_locator
            or not self.terminal_reason
            or self.contract_version != PROVIDER_OBSERVATION_TERMINAL_VERSION
            or self.semantic_authority
            or self.creates_reader_visible_occurrence
            or self.creates_placement_owner
            or self.creates_payload_leaf
            or self.search_policy != "none"
        ):
            raise PublicationSafetyError(
                "provider observation terminal claims publication authority"
            )
        object.__setattr__(
            self,
            "artifact_bindings",
            MappingProxyType(dict(sorted(self.artifact_bindings.items()))),
        )
        object.__setattr__(
            self,
            "provenance_locator",
            MappingProxyType(dict(self.provenance_locator)),
        )
        source_bound = self.terminal_kind in {
            ProviderObservationTerminalKind.SOURCE_BOUND_SUPPORT,
            ProviderObservationTerminalKind.ALIAS_OR_SUPPORT,
        }
        if source_bound != (self.bound_source_occurrence_id is not None):
            raise PublicationSafetyError(
                "provider terminal source binding is inconsistent"
            )
        if self.bound_source_occurrence_id is not None and (
            _SHA256_RE.fullmatch(self.bound_source_occurrence_id) is None
        ):
            raise PublicationSafetyError("source occurrence identity is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "artifact_bindings": dict(self.artifact_bindings),
            "runtime_identity_sha256": self.runtime_identity_sha256,
            "provenance_locator": dict(self.provenance_locator),
            "terminal_kind": self.terminal_kind.value,
            "bound_source_occurrence_id": self.bound_source_occurrence_id,
            "terminal_reason": self.terminal_reason,
            "contract_version": self.contract_version,
            "semantic_authority": self.semantic_authority,
            "creates_reader_visible_occurrence": (
                self.creates_reader_visible_occurrence
            ),
            "creates_placement_owner": self.creates_placement_owner,
            "creates_payload_leaf": self.creates_payload_leaf,
            "search_policy": self.search_policy,
        }


@dataclass(frozen=True, slots=True)
class GlyphToken:
    glyph_token_id: str
    source_pdf_sha256: str
    page_index: int
    bbox: BBox
    raster_ref: str
    raw_code: str | None
    cid: int | None
    gid: int | None
    font_object_ref: str | None
    semantic_unicode: str | None
    display_fallback: str
    decode_status: GlyphDecodeStatus
    mapping_proof: GlyphMappingProof | None
    contract_version: str = GLYPH_TOKEN_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.decode_status, GlyphDecodeStatus)
            or _SHA256_RE.fullmatch(self.glyph_token_id) is None
            or _SHA256_RE.fullmatch(self.source_pdf_sha256) is None
            or self.page_index < 0
            or not _bbox(self.bbox)
            or not self.raster_ref
            or self.raw_code is not None
            and not self.raw_code
            or self.cid is not None
            and self.cid < 0
            or self.gid is not None
            and self.gid < 0
            or self.font_object_ref is not None
            and not self.font_object_ref
            or not self.display_fallback
            or self.contract_version != GLYPH_TOKEN_CONTRACT_VERSION
        ):
            raise PublicationSafetyError("glyph token identity is invalid")
        expected_id = glyph_token_id(
            source_pdf_sha256=self.source_pdf_sha256,
            page_index=self.page_index,
            bbox=self.bbox,
            raster_ref=self.raster_ref,
            raw_code=self.raw_code,
            cid=self.cid,
            gid=self.gid,
            font_object_ref=self.font_object_ref,
        )
        if self.glyph_token_id != expected_id:
            raise PublicationSafetyError("glyph token stable id differs")
        if self.decode_status is GlyphDecodeStatus.UNRESOLVED:
            if (
                self.semantic_unicode is not None
                or self.mapping_proof is not None
                or self.display_fallback
                != unresolved_glyph_display_fallback(
                    raw_code=self.raw_code,
                    cid=self.cid,
                    gid=self.gid,
                    font_object_ref=self.font_object_ref,
                )
            ):
                raise PublicationSafetyError(
                    "unresolved glyph cannot claim semantic/display authority"
                )
        elif (
            self.semantic_unicode is None
            or not self.semantic_unicode
            or unsafe_semantic_characters(self.semantic_unicode)
            or not isinstance(self.mapping_proof, GlyphMappingProof)
            or not self.font_object_ref
            or self.display_fallback != self.semantic_unicode
        ):
            raise PublicationSafetyError(
                "resolved glyph lacks a font-bound semantic proof"
            )
        elif (
            self.mapping_proof.source_pdf_sha256 != self.source_pdf_sha256
            or self.mapping_proof.font_object_ref != self.font_object_ref
            or self.mapping_proof.raw_code != self.raw_code
            or self.mapping_proof.semantic_unicode != self.semantic_unicode
            or self.mapping_proof.proof_id
            != _glyph_mapping_proof_id(self.mapping_proof)
        ):
            raise PublicationSafetyError("glyph mapping proof binds other source facts")

    @property
    def searchable(self) -> bool:
        return self.decode_status is GlyphDecodeStatus.RESOLVED_FONT_BOUND

    def as_dict(self) -> dict[str, Any]:
        return {
            "glyph_token_id": self.glyph_token_id,
            "source_pdf_sha256": self.source_pdf_sha256,
            "page_index": self.page_index,
            "bbox": list(self.bbox),
            "raster_ref": self.raster_ref,
            "raw_code": self.raw_code,
            "cid": self.cid,
            "gid": self.gid,
            "font_object_ref": self.font_object_ref,
            "semantic_unicode": self.semantic_unicode,
            "display_fallback": self.display_fallback,
            "decode_status": self.decode_status.value,
            "mapping_proof": (
                self.mapping_proof.as_dict() if self.mapping_proof is not None else None
            ),
            "contract_version": self.contract_version,
            "searchable": self.searchable,
        }


def admit_pdf_tounicode_bfchar_mapping(
    *,
    source_pdf_sha256: str,
    font_object_ref: str,
    raw_code: str,
    semantic_unicode: str,
    mapping_evidence_bytes: bytes,
    expected_mapping_evidence_sha256: str,
) -> GlyphMappingProof:
    """Verify one exact ``bfchar`` mapping from hash-bound ToUnicode bytes."""

    if (
        _SHA256_RE.fullmatch(source_pdf_sha256) is None
        or not font_object_ref
        or not raw_code
        or not semantic_unicode
        or unsafe_semantic_characters(semantic_unicode)
        or not isinstance(mapping_evidence_bytes, bytes)
        or _SHA256_RE.fullmatch(expected_mapping_evidence_sha256) is None
        or _bytes_sha256(mapping_evidence_bytes) != expected_mapping_evidence_sha256
    ):
        raise PublicationSafetyError("glyph mapping admission inputs are invalid")
    source_hex = _raw_code_hex(raw_code)
    matches: list[tuple[bytes, bytes]] = []
    for block in _CMAP_BFCHAR_BLOCK_RE.finditer(mapping_evidence_bytes):
        for pair in _CMAP_PAIR_RE.finditer(block.group("body")):
            if pair.group(1).decode("ascii").lower() == source_hex:
                matches.append((pair.group(1), pair.group(2)))
    if len(matches) != 1:
        raise PublicationSafetyError("raw glyph code does not map exactly once")
    source_bytes, destination_bytes = matches[0]
    try:
        decoded = bytes.fromhex(destination_bytes.decode("ascii")).decode("utf-16-be")
    except (UnicodeError, ValueError) as exc:
        raise PublicationSafetyError("ToUnicode destination is invalid") from exc
    if decoded != semantic_unicode:
        raise PublicationSafetyError("ToUnicode mapping differs from semantic value")
    matched_pair = (
        b"<" + source_bytes.upper() + b"> <" + destination_bytes.upper() + b">"
    )
    return GlyphMappingProof._verified(
        source_pdf_sha256=source_pdf_sha256,
        font_object_ref=font_object_ref,
        raw_code=raw_code,
        semantic_unicode=semantic_unicode,
        mapping_evidence_sha256=expected_mapping_evidence_sha256,
        matched_pair_sha256=_bytes_sha256(matched_pair),
    )


def unresolved_glyph_display_fallback(
    *,
    raw_code: str | None,
    cid: int | None,
    gid: int | None,
    font_object_ref: str | None,
) -> str:
    """Create a non-semantic display label from bounded source identities."""

    identity = json.dumps(
        {
            "cid": cid,
            "font_object_ref": font_object_ref,
            "gid": gid,
            "raw_code": raw_code,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"⟦未解码字形 id={hashlib.sha256(identity).hexdigest()[:12]}⟧"


def semantic_search_segments(parts: tuple[SemanticPart, ...]) -> tuple[str, ...]:
    """Return safe leaves without concatenating across unresolved glyphs."""

    segments: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        value = "".join(buffer)
        buffer.clear()
        if value:
            segments.append(value)

    for part in parts:
        if isinstance(part, str):
            if unsafe_semantic_characters(part):
                raise PublicationSafetyError(
                    "unsafe glyph/control leaked into semantic text"
                )
            buffer.append(part)
            continue
        if part.searchable:
            assert part.semantic_unicode is not None
            buffer.append(part.semantic_unicode)
        else:
            flush()
    flush()
    return tuple(segments)


def semantic_text(parts: tuple[SemanticPart, ...]) -> str | None:
    """Return one canonical string only when no unresolved glyph splits it."""

    if any(isinstance(part, GlyphToken) and not part.searchable for part in parts):
        return None
    segments = semantic_search_segments(parts)
    return "".join(segments)


def display_text(parts: tuple[SemanticPart, ...]) -> str:
    """Render display fallbacks without granting them semantic/search authority."""

    output: list[str] = []
    for part in parts:
        if isinstance(part, str):
            output.append(part)
        elif part.searchable:
            assert part.semantic_unicode is not None
            output.append(part.semantic_unicode)
        else:
            output.append(part.display_fallback)
    return "".join(output)


def unsafe_semantic_characters(value: str) -> tuple[str, ...]:
    """Identify undecoded/private/replacement/control code points.

    Format characters are not blanket-rejected because valid emoji sequences
    may contain them.  The guard targets private-use/surrogate/replacement
    values and non-whitespace control characters.
    """

    unsafe = [
        char
        for char in value
        if char == "\ufffd"
        or unicodedata.category(char) in {"Co", "Cs"}
        or unicodedata.category(char) == "Cc"
        and char not in "\n\r\t"
    ]
    unsafe.extend(match.group(0) for match in _GLYPH_PLACEHOLDER_RE.finditer(value))
    return tuple(unsafe)


def conservative_semantic_segments(value: str) -> tuple[str, ...]:
    """Keep safe context as separate leaves around undecoded spans."""

    blocked_offsets = {
        index
        for index, char in enumerate(value)
        if char == "\ufffd"
        or unicodedata.category(char) in {"Co", "Cs"}
        or unicodedata.category(char) == "Cc"
        and char not in "\n\r\t"
    }
    for match in _GLYPH_PLACEHOLDER_RE.finditer(value):
        blocked_offsets.update(range(match.start(), match.end()))
    output: list[str] = []
    start = 0
    for index in sorted(blocked_offsets):
        if start < index:
            output.append(value[start:index])
        start = max(start, index + 1)
    if start < len(value):
        output.append(value[start:])
    return tuple(part for part in output if part)


def conservative_semantic_text(value: str) -> str:
    """Project safe context without joining across an undecoded glyph."""

    return "\n".join(conservative_semantic_segments(value))


def semantic_payload_without_unsafe_glyphs(value: Any) -> Any:
    """Recursively remove unsafe glyphs while preserving explicit boundaries."""

    if isinstance(value, str):
        return conservative_semantic_text(value)
    if isinstance(value, Mapping):
        return {
            key: semantic_payload_without_unsafe_glyphs(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [semantic_payload_without_unsafe_glyphs(item) for item in value]
    return value


def glyph_token_id(
    *,
    source_pdf_sha256: str,
    page_index: int,
    bbox: BBox,
    raster_ref: str,
    raw_code: str | None,
    cid: int | None,
    gid: int | None,
    font_object_ref: str | None,
) -> str:
    """Hash available source facts; unavailable capabilities remain null."""

    if _SHA256_RE.fullmatch(source_pdf_sha256) is None or not _bbox(bbox):
        raise PublicationSafetyError("glyph token id inputs are invalid")
    payload = repr(
        (
            GLYPH_TOKEN_CONTRACT_VERSION,
            source_pdf_sha256,
            page_index,
            tuple(float(value).hex() for value in bbox),
            raster_ref,
            raw_code,
            cid,
            gid,
            font_object_ref,
        )
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _raw_code_hex(value: str) -> str:
    normalized = value[2:] if value.lower().startswith("0x") else value
    if (
        not normalized
        or len(normalized) % 2 != 0
        or any(char not in "0123456789abcdefABCDEF" for char in normalized)
    ):
        raise PublicationSafetyError("raw glyph code is not even-length hex")
    return normalized.lower()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _bytes_sha256(payload)


def _glyph_mapping_proof_id(proof: GlyphMappingProof) -> str:
    return _canonical_sha256(
        {key: value for key, value in proof.as_dict().items() if key != "proof_id"}
    )


def _bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _bbox(value: object) -> bool:
    return bool(
        isinstance(value, tuple)
        and len(value) == 4
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
        and value[0] < value[2]
        and value[1] < value[3]
    )


__all__ = [
    "BBox",
    "GLYPH_TOKEN_CONTRACT_VERSION",
    "GLYPH_MAPPING_PROOF_VERSION",
    "GlyphDecodeStatus",
    "GlyphMappingProof",
    "GlyphMappingSource",
    "GlyphToken",
    "PROVIDER_OBSERVATION_TERMINAL_VERSION",
    "PUBLICATION_GATE_VERSION",
    "ProviderObservationTerminal",
    "ProviderObservationTerminalKind",
    "PublicationSafetyError",
    "PublicationGateEvaluation",
    "SemanticPart",
    "display_text",
    "admit_pdf_tounicode_bfchar_mapping",
    "conservative_semantic_segments",
    "conservative_semantic_text",
    "evaluate_publication_gate_v1",
    "glyph_token_id",
    "semantic_search_segments",
    "semantic_payload_without_unsafe_glyphs",
    "semantic_text",
    "unsafe_semantic_characters",
    "unresolved_glyph_display_fallback",
]
