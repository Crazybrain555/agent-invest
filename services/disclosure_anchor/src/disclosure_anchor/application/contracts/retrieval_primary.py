"""Explicit DB-free search ownership for one provider-native outline."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

SearchTransform = Literal["identity.v1", "html_visible_text_segments.v1"]
BlockRetrievalDisposition = Literal["primary", "evidence_only"]
BlockRetrievalReason = Literal[
    "searchable_payload",
    "page_furniture",
    "empty_provider_carrier",
    "visual_without_text",
]


@dataclass(frozen=True, slots=True)
class RetrievalTarget:
    """One explicit ProviderPayload field selected for deterministic replay."""

    target_id: str
    source_index: int
    payload_ordinal: int
    field: str
    item_index: int | None
    transform: SearchTransform
    raw_block_sha256: str

    def __post_init__(self) -> None:
        if not self.target_id or self.source_index < 0 or self.payload_ordinal < 0:
            raise ValueError("retrieval target identity is invalid")
        if not self.field:
            raise ValueError("retrieval target field must be non-empty")
        if self.item_index is not None and self.item_index < 0:
            raise ValueError("retrieval target item index cannot be negative")
        if self.transform not in {
            "identity.v1",
            "html_visible_text_segments.v1",
        }:
            raise ValueError("retrieval target transform is unsupported")
        if not _SHA256_RE.fullmatch(self.raw_block_sha256):
            raise ValueError("retrieval target block hash must be canonical")


@dataclass(frozen=True, slots=True)
class BlockRetrievalSelection:
    """One complete decision for an immutable provider content-list block."""

    source_index: int
    raw_block_sha256: str
    disposition: BlockRetrievalDisposition
    reason: BlockRetrievalReason
    target_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.source_index < 0:
            raise ValueError("retrieval block source index cannot be negative")
        if not _SHA256_RE.fullmatch(self.raw_block_sha256):
            raise ValueError("retrieval block hash must be canonical")
        if self.disposition not in {"primary", "evidence_only"}:
            raise ValueError("retrieval block disposition is unsupported")
        if self.disposition == "primary":
            if self.reason != "searchable_payload" or not self.target_ids:
                raise ValueError("primary block must expose searchable payload")
        elif self.reason not in {
            "page_furniture",
            "empty_provider_carrier",
            "visual_without_text",
        }:
            raise ValueError("evidence-only block has an invalid reason")
        elif self.target_ids:
            raise ValueError("evidence-only block cannot expose search targets")
        if len(self.target_ids) != len(set(self.target_ids)):
            raise ValueError("retrieval block cannot repeat a target")


@dataclass(frozen=True, slots=True)
class UnitRetrievalSelection:
    """Search targets and logical tables owned by one existing coarse unit."""

    unit_index: int
    primary_block_source_indices: tuple[int, ...]
    target_ids: tuple[str, ...]
    logical_table_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.unit_index < 0:
            raise ValueError("retrieval unit index cannot be negative")
        for values, label in (
            (self.primary_block_source_indices, "primary block"),
            (self.logical_table_indices, "logical table"),
        ):
            if tuple(sorted(values)) != values or len(values) != len(set(values)):
                raise ValueError(f"retrieval unit {label} indices must be unique and ordered")
        if len(self.target_ids) != len(set(self.target_ids)):
            raise ValueError("retrieval unit cannot repeat a target")


@dataclass(frozen=True, slots=True)
class RetrievalPrimaryProjection:
    """Complete primary/evidence partition without a persistence claim."""

    source_pdf_sha256: str
    provider_bundle_sha256: str
    block_count: int
    logical_table_count: int
    blocks: tuple[BlockRetrievalSelection, ...]
    targets: tuple[RetrievalTarget, ...]
    units: tuple[UnitRetrievalSelection, ...]

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.source_pdf_sha256):
            raise ValueError("retrieval source PDF hash must be canonical")
        if not _SHA256_RE.fullmatch(self.provider_bundle_sha256):
            raise ValueError("retrieval provider bundle hash must be canonical")
        if self.block_count < 0 or self.logical_table_count < 0:
            raise ValueError("retrieval projection counts cannot be negative")
        if tuple(block.source_index for block in self.blocks) != tuple(
            range(self.block_count)
        ):
            raise ValueError("retrieval decisions must cover every block in order")

        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("retrieval target ids must be unique")
        target_keys = [
            (target.source_index, target.payload_ordinal)
            for target in self.targets
        ]
        if target_keys != sorted(target_keys):
            raise ValueError("retrieval targets must preserve provider order")
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("one provider payload cannot have multiple retrieval targets")
        target_by_id = {target.target_id: target for target in self.targets}
        block_target_ids: list[str] = []
        primary_blocks: list[int] = []
        for block in self.blocks:
            if block.disposition == "primary":
                primary_blocks.append(block.source_index)
            for target_id in block.target_ids:
                target = target_by_id.get(target_id)
                if target is None or target.source_index != block.source_index:
                    raise ValueError("retrieval block target does not bind its source")
                if target.raw_block_sha256 != block.raw_block_sha256:
                    raise ValueError("retrieval target block hash drifted")
                block_target_ids.append(target_id)
        if block_target_ids != target_ids:
            raise ValueError("retrieval blocks must own every target exactly once")

        if tuple(unit.unit_index for unit in self.units) != tuple(
            range(len(self.units))
        ):
            raise ValueError("retrieval unit indices must be contiguous")
        unit_targets = [target_id for unit in self.units for target_id in unit.target_ids]
        unit_blocks = [
            source_index
            for unit in self.units
            for source_index in unit.primary_block_source_indices
        ]
        unit_tables = [
            table_index
            for unit in self.units
            for table_index in unit.logical_table_indices
        ]
        if unit_targets != target_ids:
            raise ValueError("retrieval units must partition targets in order")
        if unit_blocks != primary_blocks:
            raise ValueError("retrieval units must partition primary blocks in order")
        if unit_tables != list(range(self.logical_table_count)):
            raise ValueError(
                "retrieval units must own every logical table once in provider order"
            )


__all__ = [
    "BlockRetrievalDisposition",
    "BlockRetrievalReason",
    "BlockRetrievalSelection",
    "RetrievalPrimaryProjection",
    "RetrievalTarget",
    "SearchTransform",
    "UnitRetrievalSelection",
]
