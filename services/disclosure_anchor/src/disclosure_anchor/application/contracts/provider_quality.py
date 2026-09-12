"""Closed private reason occurrences for the existing provider quality policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, cast

from disclosure_anchor.application.contracts.document_outline import HeadingSourceFragment
from disclosure_anchor.application.contracts.provider_source_semantics import SourceQualityFinding
from disclosure_anchor.application.contracts.provider_table_projection import (
    ProviderTablePartRef, UnboundProviderTablePart, UnboundTablePartReason,
)
from disclosure_anchor.application.contracts.provider_unit import ProviderUnitSourceQualityFinding


@dataclass(frozen=True, slots=True)
class SourceFindingOccurrence:
    unit_index: int
    finding: ProviderUnitSourceQualityFinding

    @property
    def reason_id(self) -> str:
        return "source_finding:" + self.finding.source_kind + ":" + self.finding.reason


@dataclass(frozen=True, slots=True)
class UnboundTableOccurrence:
    unit_index: int | None
    part: UnboundProviderTablePart
    block_page_index: int | None
    raw_block_sha256: str | None
    segment_page_index: int | None
    raw_segment_sha256: str | None

    @property
    def reason_id(self) -> str:
        return "table_unbound:" + self.part.reason


@dataclass(frozen=True, slots=True)
class EncodedTextOccurrence:
    unit_index: int
    source_index: int
    raw_block_sha256: str

    @property
    def reason_id(self) -> str:
        return "suspected_encoded_text"


@dataclass(frozen=True, slots=True)
class TruncatedTitleOccurrence:
    unit_index: int
    heading_id: str
    source_fragments: tuple[HeadingSourceFragment, ...]

    @property
    def reason_id(self) -> str:
        return "suspected_truncated_markup_title"


ProviderQualityOccurrence = (
    SourceFindingOccurrence | UnboundTableOccurrence
    | EncodedTextOccurrence | TruncatedTitleOccurrence
)


@dataclass(frozen=True, slots=True)
class ProviderUnitQualityAssessment:
    quality_status: Literal["ok", "needs_review"]
    occurrences: tuple[ProviderQualityOccurrence, ...]


def quality_occurrence_to_payload(item: ProviderQualityOccurrence) -> dict[str, object]:
    kind = {
        SourceFindingOccurrence: "source_finding",
        UnboundTableOccurrence: "table_unbound",
        EncodedTextOccurrence: "encoded_text",
        TruncatedTitleOccurrence: "truncated_title",
    }.get(type(item))
    if kind is None:
        raise ValueError("quality occurrence type is unsupported")
    payload = asdict(item)
    if isinstance(item, TruncatedTitleOccurrence):
        payload["source_fragments"] = [asdict(fragment) for fragment in item.source_fragments]
    return {"kind": kind, "reason_id": item.reason_id, **payload}


def quality_occurrence_from_payload(value: object) -> ProviderQualityOccurrence:
    """Validate closed occurrence shapes, not their truth against a source."""
    if type(value) is not dict:
        raise ValueError("quality occurrence must be an object")
    obj = cast(dict[str, object], value)
    kind = obj.get("kind")
    common = {"kind", "reason_id", "unit_index"}
    item: ProviderQualityOccurrence
    if kind == "source_finding":
        _fields(obj, common | {"finding"})
        finding = _fields(obj["finding"], set(SourceQualityFinding.__dataclass_fields__))
        for name, field in finding.items():
            if name in {"source_index", "payload_ordinal"}:
                _index(field)
            else:
                _text(field)
        checked = SourceQualityFinding(**finding)  # type: ignore[arg-type]
        item = SourceFindingOccurrence(
            _index(obj["unit_index"]), ProviderUnitSourceQualityFinding(**asdict(checked)),
        )
    elif kind == "table_unbound":
        _fields(obj, common | {
            "part", "block_page_index", "raw_block_sha256",
            "segment_page_index", "raw_segment_sha256",
        })
        part = _fields(obj["part"], {"part", "reason"})
        ref = _fields(part["part"], {"block_source_index", "physical_segment_index"})
        block = _optional_index(ref["block_source_index"])
        segment = _optional_index(ref["physical_segment_index"])
        unit = _optional_index(obj["unit_index"])
        block_page = _optional_index(obj["block_page_index"])
        segment_page = _optional_index(obj["segment_page_index"])
        block_hash = _optional_sha(obj["raw_block_sha256"])
        segment_hash = _optional_sha(obj["raw_segment_sha256"])
        if (
            (block is None) != (block_page is None)
            or (block is None) != (block_hash is None)
            or (segment is None) != (segment_page is None)
            or (segment is None) != (segment_hash is None)
            or (unit is None) != (block is None)
        ):
            raise ValueError("quality table occurrence has incomplete ownership/preimages")
        item = UnboundTableOccurrence(
            unit,
            UnboundProviderTablePart(
                ProviderTablePartRef(block, segment),
                cast(UnboundTablePartReason, _text(part["reason"])),
            ),
            block_page, block_hash, segment_page, segment_hash,
        )
    elif kind == "encoded_text":
        _fields(obj, common | {"source_index", "raw_block_sha256"})
        item = EncodedTextOccurrence(
            _index(obj["unit_index"]), _index(obj["source_index"]),
            _sha(obj["raw_block_sha256"]),
        )
    elif kind == "truncated_title":
        _fields(obj, common | {"heading_id", "source_fragments"})
        fragments = obj["source_fragments"]
        if type(fragments) is not list or not fragments:
            raise ValueError("quality title occurrence requires source fragments")
        parsed = []
        for raw in fragments:
            fragment = _fields(raw, set(HeadingSourceFragment.__dataclass_fields__))
            parsed.append(HeadingSourceFragment(
                source_index=_index(fragment["source_index"]),
                payload_ordinal=_index(fragment["payload_ordinal"]),
                page_index=_index(fragment["page_index"]),
                text=_text(fragment["text"]),
                raw_block_sha256=_sha(fragment["raw_block_sha256"]),
            ))
        item = TruncatedTitleOccurrence(
            _index(obj["unit_index"]), _text(obj["heading_id"]), tuple(parsed),
        )
    else:
        raise ValueError("quality occurrence kind is unsupported")
    if _text(obj["reason_id"]) != item.reason_id:
        raise ValueError("quality occurrence reason differs from its evidence")
    return item


def ordered_quality_occurrences(
    items: tuple[ProviderQualityOccurrence, ...],
) -> tuple[ProviderQualityOccurrence, ...]:
    """Deduplicate identical occurrences, order numeric locations numerically."""
    return tuple(sorted(set(items), key=_order))


def _order(item: ProviderQualityOccurrence) -> tuple[object, ...]:
    unit = -1 if item.unit_index is None else item.unit_index
    if isinstance(item, SourceFindingOccurrence):
        identity: tuple[object, ...] = (
            item.finding.source_index, item.finding.payload_ordinal,
            item.finding.raw_block_sha256, item.finding.provider_text_sha256,
            item.finding.source_text_sha256,
        )
    elif isinstance(item, UnboundTableOccurrence):
        ref = item.part.part
        identity = (
            -1 if ref.block_source_index is None else ref.block_source_index,
            -1 if ref.physical_segment_index is None else ref.physical_segment_index,
            -1 if item.block_page_index is None else item.block_page_index,
            item.raw_block_sha256 or "",
            -1 if item.segment_page_index is None else item.segment_page_index,
            item.raw_segment_sha256 or "",
        )
    elif isinstance(item, EncodedTextOccurrence):
        identity = (item.source_index, item.raw_block_sha256)
    else:
        identity = (item.heading_id, tuple(
            (f.source_index, f.payload_ordinal, f.page_index, f.text, f.raw_block_sha256)
            for f in item.source_fragments
        ))
    return (item.reason_id, unit, *identity)


def _fields(value: object, expected: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise ValueError("quality occurrence fields are not closed")
    return cast(dict[str, object], value)


def _index(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("quality occurrence index must be a nonnegative integer")
    return value


def _optional_index(value: object) -> int | None:
    return None if value is None else _index(value)


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("quality occurrence text must be nonempty")
    return value


def _sha(value: object) -> str:
    text = _text(value)
    if len(text) != 71 or not text.startswith("sha256:") or any(
        char not in "0123456789abcdef" for char in text[7:]
    ):
        raise ValueError("quality occurrence hash must be canonical")
    return text


def _optional_sha(value: object) -> str | None:
    return None if value is None else _sha(value)
