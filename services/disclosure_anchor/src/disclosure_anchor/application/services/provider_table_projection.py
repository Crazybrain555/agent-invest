"""Project only MinerU's explicit merge result; never repair table content."""

from __future__ import annotations

from dataclasses import dataclass

from disclosure_anchor.application.contracts.provider_document import (
    ProviderBlock,
    ProviderDocument,
    ProviderPhysicalTableSegment,
)
from disclosure_anchor.application.contracts.provider_table_projection import (
    ProviderLogicalTable,
    ProviderTablePartRef,
    ProviderTableProjection,
    UnboundProviderTablePart,
    UnboundTablePartReason,
)


_PAGE_FURNITURE_TYPES = frozenset({"header", "footer", "page_number"})
_PAGE_FURNITURE_ANNOTATIONS = frozenset(
    {"page_header", "page_footer", "page_number"}
)
_TABLE_BODY_FIELD = "table_body"


@dataclass(frozen=True, slots=True)
class _BoundPart:
    ref: ProviderTablePartRef
    block: ProviderBlock
    segment: ProviderPhysicalTableSegment


def build_provider_table_projection(
    document: ProviderDocument,
) -> ProviderTableProjection:
    """Bind table ordinals and replay only provider-marked retained/deleted state."""

    table_blocks = tuple(
        block for block in document.blocks if block.provider_type == "table"
    )
    segment_index_by_identity = {
        id(segment): index
        for index, segment in enumerate(document.physical_table_segments)
    }
    bound_parts: list[_BoundPart] = []
    unbound: list[UnboundProviderTablePart] = []

    for page in document.pages:
        page_blocks = tuple(
            block for block in page.blocks if block.provider_type == "table"
        )
        page_segments = tuple(
            segment
            for segment in document.physical_table_segments
            if segment.page_index == page.page_index
        )
        if len(page_blocks) != len(page_segments):
            unbound.extend(
                UnboundProviderTablePart(
                    part=ProviderTablePartRef(block.source_index, None),
                    reason="page_table_count_mismatch",
                )
                for block in page_blocks
            )
            unbound.extend(
                UnboundProviderTablePart(
                    part=ProviderTablePartRef(
                        None,
                        segment_index_by_identity[id(segment)],
                    ),
                    reason="page_table_count_mismatch",
                )
                for segment in page_segments
            )
            continue
        bound_parts.extend(
            _BoundPart(
                ref=ProviderTablePartRef(
                    block.source_index,
                    segment_index_by_identity[id(segment)],
                ),
                block=block,
                segment=segment,
            )
            for block, segment in zip(page_blocks, page_segments, strict=True)
        )

    logical_tables: list[ProviderLogicalTable] = []
    active_parts: list[ProviderTablePartRef] = []
    previous_part: _BoundPart | None = None

    def flush_active() -> None:
        nonlocal active_parts
        if not active_parts:
            return
        logical_tables.append(
            ProviderLogicalTable(
                owner=active_parts[0],
                continuations=tuple(active_parts[1:]),
            )
        )
        active_parts = []

    for part in bound_parts:
        has_table_body = _has_table_body(part.block)
        status = part.segment.logical_stream_status
        if status == "retained" and has_table_body:
            flush_active()
            active_parts = [part.ref]
            previous_part = part
            continue

        if status == "deleted" and not _has_searchable_payload(part.block):
            continuation_reason = _continuation_failure_reason(
                document=document,
                previous=previous_part,
                current=part,
                has_active_owner=bool(active_parts),
            )
            if continuation_reason is None:
                active_parts.append(part.ref)
                previous_part = part
                continue
            flush_active()
            unbound.append(
                UnboundProviderTablePart(
                    part=part.ref,
                    reason=continuation_reason,
                )
            )
            previous_part = part
            continue

        flush_active()
        if status == "retained":
            mismatch_reason: UnboundTablePartReason = "retained_without_payload"
        elif status == "deleted":
            mismatch_reason = "deleted_with_payload"
        else:
            mismatch_reason = "provider_status_unbound"
        unbound.append(
            UnboundProviderTablePart(part=part.ref, reason=mismatch_reason)
        )
        previous_part = part

    flush_active()
    unbound.sort(
        key=lambda item: (
            item.part.block_source_index
            if item.part.block_source_index is not None
            else len(document.blocks) + (item.part.physical_segment_index or 0)
        )
    )
    return ProviderTableProjection(
        source_pdf_sha256=document.source_pdf_sha256,
        provider_bundle_sha256=document.bundle_sha256,
        table_block_source_indices=tuple(
            block.source_index for block in table_blocks
        ),
        physical_segment_count=len(document.physical_table_segments),
        logical_tables=tuple(logical_tables),
        unbound_parts=tuple(unbound),
    )


def _has_table_body(block: ProviderBlock) -> bool:
    return any(
        payload.field == _TABLE_BODY_FIELD and bool(payload.text.strip())
        for payload in block.payloads
    )


def _has_searchable_payload(block: ProviderBlock) -> bool:
    return any(payload.text.strip() for payload in block.payloads)


def _continuation_failure_reason(
    *,
    document: ProviderDocument,
    previous: _BoundPart | None,
    current: _BoundPart,
    has_active_owner: bool,
) -> UnboundTablePartReason | None:
    if previous is None or not has_active_owner:
        return "continuation_without_owner"
    if current.block.page_index != previous.block.page_index + 1:
        return "continuation_not_next_page"
    previous_page = document.pages[previous.block.page_index]
    current_page = document.pages[current.block.page_index]
    if not _only_page_furniture(
        previous_page.blocks[previous.block.order_in_page + 1 :]
    ) or not _only_page_furniture(
        current_page.blocks[: current.block.order_in_page]
    ):
        return "continuation_not_page_boundary"
    return None


def _only_page_furniture(blocks: tuple[ProviderBlock, ...]) -> bool:
    return all(is_provider_page_furniture(block) for block in blocks)


def is_provider_page_furniture(block: ProviderBlock) -> bool:
    """Return only the provider's typed page-frame classifications."""

    return (
        block.provider_type in _PAGE_FURNITURE_TYPES
        or block.typed_annotation in _PAGE_FURNITURE_ANNOTATIONS
    )


__all__ = ["build_provider_table_projection", "is_provider_page_furniture"]
