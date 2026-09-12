"""Complete bounded source-only builds, preserving divergent candidate data.

The decoder proves shape and serialized Unit hashes, not conservation, semantic
truth, process independence or production admission. Comparison is a later step.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from typing import cast

from disclosure_anchor.application.contracts.diagnostic_json import (
    bounded_json_bytes, bounded_json_value, require_projection_budget,
)
from disclosure_anchor.application.contracts.provider_quality import (
    ProviderQualityOccurrence, ordered_quality_occurrences,
    quality_occurrence_from_payload, quality_occurrence_to_payload,
)
from disclosure_anchor.application.contracts.provider_table_projection import (
    ProviderTablePartRef, UnboundProviderTablePart, UnboundTablePartReason,
)
from disclosure_anchor.application.contracts.provider_unit import (
    PROVIDER_UNIT_BUILDER_VERSION, PROVIDER_UNIT_LOCATOR_VERSION,
    ProviderUnitApplicability, ProviderUnitBuildResult, ProviderUnitDraft,
    ProviderUnitPayloadKind, provider_unit_locator_from_payload,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.domain.services.unit_hashing import compute_unit_hashes


SOURCE_SEMANTIC_BUILD_VERSION = "m6.source-semantic-build.v1"


@dataclass(frozen=True, slots=True)
class SourceSemanticBuildCandidate:
    semantic_record_sha256: str
    build: ProviderUnitBuildResult
    quality_occurrences: tuple[ProviderQualityOccurrence, ...]
    record_sha256: str


def encode_source_semantic_build(
    *, semantic_record_sha256: str, build: ProviderUnitBuildResult,
    quality_occurrences: tuple[ProviderQualityOccurrence, ...], maximum_bytes: int,
) -> bytes:
    if type(build) is not ProviderUnitBuildResult or type(quality_occurrences) is not tuple:
        raise ValueError("source build encoder requires exact build/occurrence types")
    require_projection_budget(
        (build, quality_occurrences), maximum_bytes=maximum_bytes,
    )
    raw = bounded_json_bytes(
        {
            "contract_version": SOURCE_SEMANTIC_BUILD_VERSION,
            "mode": "service_diagnostic",
            "semantic_record_sha256": _sha(semantic_record_sha256),
            "builder_version": PROVIDER_UNIT_BUILDER_VERSION,
            "level_hints": [], "negative_hints": [],
            "build": {
                "provider_document_sha256": build.provider_document_sha256,
                "units": [source_unit_to_payload(unit) for unit in build.units],
                "unassigned_table_parts": [asdict(part) for part in build.unassigned_table_parts],
            },
            "quality_occurrences": [quality_occurrence_to_payload(item) for item in quality_occurrences],
        }, maximum_bytes=maximum_bytes,
    )
    decode_source_semantic_build(raw, maximum_bytes=maximum_bytes)
    return raw


def decode_source_semantic_build(
    raw: bytes, *, maximum_bytes: int,
) -> SourceSemanticBuildCandidate:
    obj = _fields(bounded_json_value(raw, maximum_bytes=maximum_bytes), {
        "contract_version", "mode", "semantic_record_sha256", "builder_version",
        "level_hints", "negative_hints", "build", "quality_occurrences",
    })
    if (
        obj["contract_version"] != SOURCE_SEMANTIC_BUILD_VERSION
        or obj["mode"] != "service_diagnostic"
        or obj["builder_version"] != PROVIDER_UNIT_BUILDER_VERSION
    ):
        raise ValueError("source build version/mode is unsupported")
    if _array(obj["level_hints"]) or _array(obj["negative_hints"]):
        raise ValueError("source build runtime requires explicitly empty hints")
    build = _fields(obj["build"], {"provider_document_sha256", "units", "unassigned_table_parts"})
    result = ProviderUnitBuildResult(
        _sha(build["provider_document_sha256"]),
        tuple(_unit_from_payload(unit) for unit in _array(build["units"])),
        tuple(_unassigned_from_payload(part) for part in _array(build["unassigned_table_parts"])),
    )
    occurrences = tuple(
        quality_occurrence_from_payload(item) for item in _array(obj["quality_occurrences"])
    )
    if occurrences != ordered_quality_occurrences(occurrences):
        raise ValueError("source quality occurrences must be ordered and unique")
    return SourceSemanticBuildCandidate(
        _sha(obj["semantic_record_sha256"]), result, occurrences,
        "sha256:" + hashlib.sha256(raw).hexdigest(),
    )


def source_unit_to_payload(unit: ProviderUnitDraft) -> dict[str, object]:
    """All Unit fields, including complete payload and current versioned locator."""
    if type(unit) is not ProviderUnitDraft:
        raise ValueError("source build requires exact Unit drafts")
    return {
        "unit_index": unit.unit_index, "payload_kind": unit.payload_kind,
        "payload": unit.payload, "title": unit.title,
        "heading_path": list(unit.heading_path),
        "section_keys": None if unit.section_keys is None else list(unit.section_keys),
        "semantic_keys": None if unit.semantic_keys is None else list(unit.semantic_keys),
        "applicability": unit.applicability, "quality_status": unit.quality_status,
        "page_no": unit.page_no, "locator": provider_unit_locator_to_payload(unit.locator),
        "content_hash": unit.content_hash, "query_projection_hash": unit.query_projection_hash,
        "structure_hash": unit.structure_hash,
    }


def _unit_from_payload(value: object) -> ProviderUnitDraft:
    obj = _fields(value, set(ProviderUnitDraft.__dataclass_fields__))
    payload = obj["payload"]
    if type(payload) is not dict:
        raise ValueError("source Unit payload must be an object")
    locator = provider_unit_locator_from_payload(obj["locator"])
    if locator.contract_version != PROVIDER_UNIT_LOCATOR_VERSION:
        raise ValueError("source build requires the current Unit locator")
    unit = ProviderUnitDraft(
        unit_index=_index(obj["unit_index"]),
        payload_kind=cast(ProviderUnitPayloadKind, _text(obj["payload_kind"])),
        payload=payload,
        title=None if obj["title"] is None else _text(obj["title"]),
        heading_path=tuple(_text(item) for item in _array(obj["heading_path"])),
        section_keys=_optional_texts(obj["section_keys"]),
        semantic_keys=_optional_texts(obj["semantic_keys"]),
        applicability=None if obj["applicability"] is None else cast(
            ProviderUnitApplicability, _text(obj["applicability"])
        ),
        quality_status=_text(obj["quality_status"]), page_no=_index(obj["page_no"]),
        locator=locator, content_hash=_sha(obj["content_hash"]),
        query_projection_hash=_sha(obj["query_projection_hash"]),
        structure_hash=_sha(obj["structure_hash"]),
    )
    hashes = compute_unit_hashes(
        payload_kind=unit.payload_kind, payload=unit.payload, title=unit.title,
        heading_path=list(unit.heading_path), quality_status=unit.quality_status,
        order_index=unit.unit_index + 1,
        section_keys=None if unit.section_keys is None else list(unit.section_keys),
        semantic_keys=None if unit.semantic_keys is None else list(unit.semantic_keys),
        applicability=unit.applicability,
    )
    if (
        unit.content_hash != hashes.content_hash
        or unit.query_projection_hash != hashes.query_projection_hash
        or unit.structure_hash != hashes.structure_hash
    ):
        raise ValueError("source Unit serialized hashes differ from recomputed content")
    return unit


def _unassigned_from_payload(value: object) -> UnboundProviderTablePart:
    obj = _fields(value, {"part", "reason"})
    part = _fields(obj["part"], {"block_source_index", "physical_segment_index"})
    return UnboundProviderTablePart(
        ProviderTablePartRef(
            None if part["block_source_index"] is None else _index(part["block_source_index"]),
            None if part["physical_segment_index"] is None else _index(part["physical_segment_index"]),
        ), cast(UnboundTablePartReason, _text(obj["reason"])),
    )


def _fields(value: object, expected: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise ValueError("source build fields are not closed")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if type(value) is not list:
        raise ValueError("source build arrays must be lists")
    return value


def _index(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("source build index must be a nonnegative integer")
    return value


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("source build text must be nonempty")
    return value


def _optional_texts(value: object) -> tuple[str, ...] | None:
    return None if value is None else tuple(_text(item) for item in _array(value))


def _sha(value: object) -> str:
    text = _text(value)
    if len(text) != 71 or not text.startswith("sha256:") or any(
        char not in "0123456789abcdef" for char in text[7:]
    ):
        raise ValueError("source build hash must be canonical")
    return text
