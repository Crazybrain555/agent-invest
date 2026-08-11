"""Small DB-free Unit drafts projected from one admitted provider document."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from disclosure_anchor.application.contracts.provider_table_projection import (
    UnboundProviderTablePart,
)
from disclosure_anchor.application.contracts.document_outline import (
    HeadingPlacementSource,
)
from disclosure_anchor.application.contracts.retrieval_primary import RetrievalTarget


PROVIDER_UNIT_LOCATOR_VERSION = "provider_unit_locator.v1"
PROVIDER_UNIT_SEMANTIC_KEY = "document_content"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

ProviderUnitPayloadKind = Literal["text", "table", "mixed"]
ProviderUnitPartKind = Literal["text", "table", "visual"]
ProviderSearchDestinationKind = Literal[
    "unit_title",
    "unit_payload",
    "mixed_part",
]


@dataclass(frozen=True, slots=True)
class ProviderUnitHeadingRef:
    """One source heading in a Unit's root-to-leaf path."""

    heading_id: str
    source_index: int
    placement_source: HeadingPlacementSource

    def __post_init__(self) -> None:
        if not self.heading_id or self.source_index < 0 or not self.placement_source:
            raise ValueError("provider unit heading reference is invalid")


@dataclass(frozen=True, slots=True)
class ProviderUnitPartRef:
    """One payload part and the provider occurrences it owns."""

    part_index: int
    kind: ProviderUnitPartKind
    block_source_indices: tuple[int, ...]
    physical_table_segment_indices: tuple[int, ...] = ()
    logical_table_index: int | None = None

    def __post_init__(self) -> None:
        if self.part_index < 0 or self.kind not in {"text", "table", "visual"}:
            raise ValueError("provider unit part identity is invalid")
        if not self.block_source_indices:
            raise ValueError("provider unit part must own a provider block")
        for values, label in (
            (self.block_source_indices, "block"),
            (self.physical_table_segment_indices, "physical table segment"),
        ):
            if tuple(sorted(values)) != values or len(values) != len(set(values)):
                raise ValueError(
                    f"provider unit part {label} indices must be unique and ordered"
                )
        if self.logical_table_index is not None:
            if self.kind != "table" or self.logical_table_index < 0:
                raise ValueError("logical table reference must belong to a table part")
            if len(self.block_source_indices) != len(
                self.physical_table_segment_indices
            ):
                raise ValueError(
                    "logical table part must pair every block and physical segment"
                )


@dataclass(frozen=True, slots=True)
class ProviderSearchDestination:
    """Closed destination for one provider payload retrieval target."""

    kind: ProviderSearchDestinationKind
    part_index: int | None = None
    field: str | None = None
    item_index: int | None = None

    def __post_init__(self) -> None:
        if self.kind == "unit_title":
            if any(
                value is not None
                for value in (self.part_index, self.field, self.item_index)
            ):
                raise ValueError("unit title destination cannot claim payload fields")
            return
        if not self.field:
            raise ValueError("provider search payload destination needs a field")
        if self.item_index is not None and self.item_index < 0:
            raise ValueError("provider search destination item index cannot be negative")
        if self.kind == "unit_payload":
            if self.part_index is not None:
                raise ValueError("unit payload destination cannot claim a mixed part")
        elif self.kind == "mixed_part":
            if self.part_index is None or self.part_index < 0:
                raise ValueError("mixed destination requires a part index")
        else:
            raise ValueError("provider search destination kind is unsupported")


@dataclass(frozen=True, slots=True)
class ProviderUnitSearchBinding:
    """One exact source target bound to one raw Unit payload destination."""

    source: RetrievalTarget
    destination: ProviderSearchDestination


@dataclass(frozen=True, slots=True)
class ProviderUnitLocator:
    """One thin locator; the hash-bound ProviderDocument supplies all detail."""

    provider_document_sha256: str
    unit_index: int
    heading_chain: tuple[ProviderUnitHeadingRef, ...]
    parts: tuple[ProviderUnitPartRef, ...]
    evidence_only_block_source_indices: tuple[int, ...]
    unbound_table_parts: tuple[UnboundProviderTablePart, ...]
    search_targets: tuple[ProviderUnitSearchBinding, ...]
    contract_version: str = PROVIDER_UNIT_LOCATOR_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != PROVIDER_UNIT_LOCATOR_VERSION:
            raise ValueError("provider unit locator version is unsupported")
        if not _SHA256_RE.fullmatch(self.provider_document_sha256):
            raise ValueError("provider unit locator hash must be canonical")
        if self.unit_index < 0:
            raise ValueError("provider unit locator index cannot be negative")
        if tuple(part.part_index for part in self.parts) != tuple(
            range(len(self.parts))
        ):
            raise ValueError("provider unit part indices must be contiguous")
        evidence = self.evidence_only_block_source_indices
        if tuple(sorted(evidence)) != evidence or len(evidence) != len(set(evidence)):
            raise ValueError("evidence-only block indices must be unique and ordered")
        search_ids = [binding.source.target_id for binding in self.search_targets]
        if len(search_ids) != len(set(search_ids)):
            raise ValueError("provider unit locator cannot repeat a search target")
        if any(
            part.part.block_source_index is None
            for part in self.unbound_table_parts
        ):
            raise ValueError(
                "provider unit locator cannot claim a segment-only unbound table part"
            )


@dataclass(frozen=True, slots=True)
class ProviderUnitDraft:
    """One persistence-ready draft without a DB asset identity."""

    unit_index: int
    payload_kind: ProviderUnitPayloadKind
    payload: dict[str, object]
    title: str | None
    heading_path: tuple[str, ...]
    semantic_key: str
    semantic_keys: tuple[str, ...]
    quality_status: str
    page_no: int
    locator: ProviderUnitLocator
    content_hash: str
    query_projection_hash: str
    structure_hash: str

    def __post_init__(self) -> None:
        if self.unit_index < 0 or self.locator.unit_index != self.unit_index:
            raise ValueError("provider unit draft index is invalid")
        if self.payload_kind not in {"text", "table", "mixed"}:
            raise ValueError("provider unit payload kind is unsupported")
        if self.title is None:
            if self.heading_path or self.locator.heading_chain:
                raise ValueError("unheaded provider unit cannot claim a heading path")
        elif not self.heading_path or self.heading_path[-1] != self.title:
            raise ValueError("provider unit title must end its heading path")
        if len(self.heading_path) != len(self.locator.heading_chain):
            raise ValueError("provider unit heading chain differs from its path")
        if (
            self.semantic_key != PROVIDER_UNIT_SEMANTIC_KEY
            or self.semantic_keys != (PROVIDER_UNIT_SEMANTIC_KEY,)
        ):
            raise ValueError("provider unit must use the generic L1 retrieval key")
        if not self.quality_status or self.page_no < 1:
            raise ValueError("provider unit quality and page must be complete")
        for value in (
            self.content_hash,
            self.query_projection_hash,
            self.structure_hash,
        ):
            if not _SHA256_RE.fullmatch(value):
                raise ValueError("provider unit hashes must be canonical")


@dataclass(frozen=True, slots=True)
class ProviderUnitBuildResult:
    """Complete DB-free Unit result and table evidence without a semantic owner."""

    provider_document_sha256: str
    units: tuple[ProviderUnitDraft, ...]
    unassigned_table_parts: tuple[UnboundProviderTablePart, ...]

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.provider_document_sha256):
            raise ValueError("provider Unit build hash must be canonical")
        if tuple(unit.unit_index for unit in self.units) != tuple(
            range(len(self.units))
        ):
            raise ValueError("provider Unit build indices must be contiguous")
        segment_indices = [
            part.part.physical_segment_index
            for part in self.unassigned_table_parts
        ]
        if any(
            part.part.block_source_index is not None
            or part.part.physical_segment_index is None
            for part in self.unassigned_table_parts
        ):
            raise ValueError("unassigned table parts must be segment-only")
        if len(segment_indices) != len(set(segment_indices)):
            raise ValueError("unassigned table parts cannot repeat a segment")


def provider_unit_locator_to_payload(
    locator: ProviderUnitLocator,
) -> dict[str, object]:
    """Serialize the one closed locator without provider paths or raw JSON."""

    return {
        "contract_version": locator.contract_version,
        "provider_document_sha256": locator.provider_document_sha256,
        "unit_index": locator.unit_index,
        "heading_chain": [
            {
                "heading_id": heading.heading_id,
                "placement_source": heading.placement_source,
                "source_index": heading.source_index,
            }
            for heading in locator.heading_chain
        ],
        "parts": [
            {
                "block_source_indices": list(part.block_source_indices),
                "kind": part.kind,
                "logical_table_index": part.logical_table_index,
                "part_index": part.part_index,
                "physical_table_segment_indices": list(
                    part.physical_table_segment_indices
                ),
            }
            for part in locator.parts
        ],
        "evidence_only_block_source_indices": list(
            locator.evidence_only_block_source_indices
        ),
        "unbound_table_parts": [
            {
                "block_source_index": part.part.block_source_index,
                "physical_table_segment_index": (
                    part.part.physical_segment_index
                ),
                "reason": part.reason,
            }
            for part in locator.unbound_table_parts
        ],
        "search_targets": [
            {
                "destination": {
                    "field": binding.destination.field,
                    "item_index": binding.destination.item_index,
                    "kind": binding.destination.kind,
                    "part_index": binding.destination.part_index,
                },
                "field": binding.source.field,
                "item_index": binding.source.item_index,
                "payload_ordinal": binding.source.payload_ordinal,
                "raw_block_sha256": binding.source.raw_block_sha256,
                "source_index": binding.source.source_index,
                "target_id": binding.source.target_id,
                "transform": binding.source.transform,
            }
            for binding in locator.search_targets
        ],
    }


__all__ = [
    "PROVIDER_UNIT_LOCATOR_VERSION",
    "PROVIDER_UNIT_SEMANTIC_KEY",
    "ProviderSearchDestination",
    "ProviderSearchDestinationKind",
    "ProviderUnitBuildResult",
    "ProviderUnitDraft",
    "ProviderUnitHeadingRef",
    "ProviderUnitLocator",
    "ProviderUnitPartKind",
    "ProviderUnitPartRef",
    "ProviderUnitPayloadKind",
    "ProviderUnitSearchBinding",
    "provider_unit_locator_to_payload",
]
