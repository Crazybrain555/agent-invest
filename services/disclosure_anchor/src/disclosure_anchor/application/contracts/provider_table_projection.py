"""Small DB-free records for MinerU's explicit logical-table assertion."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

UnboundTablePartReason = Literal[
    "page_table_count_mismatch",
    "retained_without_payload",
    "deleted_with_payload",
    "provider_status_unbound",
    "continuation_without_owner",
    "continuation_not_next_page",
    "continuation_not_page_boundary",
]


@dataclass(frozen=True, slots=True)
class ProviderTablePartRef:
    """A table block and/or physical segment retained by ProviderDocument."""

    block_source_index: int | None
    physical_segment_index: int | None

    def __post_init__(self) -> None:
        if self.block_source_index is None and self.physical_segment_index is None:
            raise ValueError("provider table part must reference a block or segment")
        if self.block_source_index is not None and self.block_source_index < 0:
            raise ValueError("provider table block index cannot be negative")
        if self.physical_segment_index is not None and self.physical_segment_index < 0:
            raise ValueError("provider table segment index cannot be negative")


@dataclass(frozen=True, slots=True)
class ProviderLogicalTable:
    """One provider-owned aggregate payload and its page-local continuations."""

    owner: ProviderTablePartRef
    continuations: tuple[ProviderTablePartRef, ...]

    def __post_init__(self) -> None:
        if (
            self.owner.block_source_index is None
            or self.owner.physical_segment_index is None
        ):
            raise ValueError("logical table owner must bind a block and segment")
        previous_block = self.owner.block_source_index
        previous_segment = self.owner.physical_segment_index
        for continuation in self.continuations:
            if (
                continuation.block_source_index is None
                or continuation.physical_segment_index is None
            ):
                raise ValueError("logical table continuation must bind a block and segment")
            if (
                continuation.block_source_index <= previous_block
                or continuation.physical_segment_index <= previous_segment
            ):
                raise ValueError("logical table parts must preserve provider order")
            previous_block = continuation.block_source_index
            previous_segment = continuation.physical_segment_index


@dataclass(frozen=True, slots=True)
class UnboundProviderTablePart:
    """One table occurrence retained without inventing a logical owner."""

    part: ProviderTablePartRef
    reason: UnboundTablePartReason

    def __post_init__(self) -> None:
        if self.reason not in {
            "page_table_count_mismatch",
            "retained_without_payload",
            "deleted_with_payload",
            "provider_status_unbound",
            "continuation_without_owner",
            "continuation_not_next_page",
            "continuation_not_page_boundary",
        }:
            raise ValueError("provider table unbound reason is unsupported")


@dataclass(frozen=True, slots=True)
class ProviderTableProjection:
    """Complete table-part partition derived from one exact provider document."""

    source_pdf_sha256: str
    provider_bundle_sha256: str
    table_block_source_indices: tuple[int, ...]
    physical_segment_count: int
    logical_tables: tuple[ProviderLogicalTable, ...]
    unbound_parts: tuple[UnboundProviderTablePart, ...]

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.source_pdf_sha256):
            raise ValueError("table projection source PDF hash must be canonical")
        if not _SHA256_RE.fullmatch(self.provider_bundle_sha256):
            raise ValueError("table projection bundle hash must be canonical")
        if self.physical_segment_count < 0:
            raise ValueError("table projection segment count cannot be negative")
        if (
            tuple(sorted(self.table_block_source_indices))
            != self.table_block_source_indices
            or len(set(self.table_block_source_indices))
            != len(self.table_block_source_indices)
        ):
            raise ValueError("table block indices must be unique and ordered")

        block_refs: list[int] = []
        segment_refs: list[int] = []
        previous_owner = -1
        for logical_table in self.logical_tables:
            owner_index = logical_table.owner.block_source_index
            assert owner_index is not None
            if owner_index <= previous_owner:
                raise ValueError("logical table owners must preserve source order")
            previous_owner = owner_index
            for part in (logical_table.owner, *logical_table.continuations):
                assert part.block_source_index is not None
                assert part.physical_segment_index is not None
                block_refs.append(part.block_source_index)
                segment_refs.append(part.physical_segment_index)
        for unbound in self.unbound_parts:
            if unbound.part.block_source_index is not None:
                block_refs.append(unbound.part.block_source_index)
            if unbound.part.physical_segment_index is not None:
                segment_refs.append(unbound.part.physical_segment_index)

        if sorted(block_refs) != list(self.table_block_source_indices):
            raise ValueError("table projection must reference every table block once")
        if len(block_refs) != len(set(block_refs)):
            raise ValueError("table projection repeats a table block")
        if sorted(segment_refs) != list(range(self.physical_segment_count)):
            raise ValueError("table projection must reference every segment once")
        if len(segment_refs) != len(set(segment_refs)):
            raise ValueError("table projection repeats a physical segment")


__all__ = [
    "ProviderLogicalTable",
    "ProviderTablePartRef",
    "ProviderTableProjection",
    "UnboundProviderTablePart",
    "UnboundTablePartReason",
]
