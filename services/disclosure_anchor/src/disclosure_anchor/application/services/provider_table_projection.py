"""Project only MinerU's explicit merge result; never repair table content."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re

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
_PAGE_FURNITURE_ANNOTATIONS = frozenset({"page_header", "page_footer", "page_number"})
_HEADER_VARIANT_GEOMETRY_TOLERANCE = 8.0
_HEADER_VARIANT_MIN_FINGERPRINT_CHARS = 4
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
        unbound.append(UnboundProviderTablePart(part=part.ref, reason=mismatch_reason))
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
        table_block_source_indices=tuple(block.source_index for block in table_blocks),
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
    if not _only_previous_page_boundary_furniture(
        previous_page.blocks[previous.block.order_in_page + 1 :]
    ) or not _only_page_furniture(current_page.blocks[: current.block.order_in_page]):
        return "continuation_not_page_boundary"
    return None


def _only_page_furniture(blocks: tuple[ProviderBlock, ...]) -> bool:
    return all(is_provider_page_frame(block) for block in blocks)


def _only_previous_page_boundary_furniture(
    blocks: tuple[ProviderBlock, ...],
) -> bool:
    return all(
        is_provider_page_frame(block)
        or block.provider_type.casefold() == "page_footnote"
        or (block.typed_annotation or "").casefold() == "page_footnote"
        for block in blocks
    )


def is_provider_page_frame(block: ProviderBlock) -> bool:
    """Return the provider's broad page-frame classification.

    This is deliberately suitable only for physical page-boundary checks.  A
    unique header/footer can still be substantive source content.
    """

    return (
        block.provider_type.casefold() in _PAGE_FURNITURE_TYPES
        or (block.typed_annotation or "").casefold() in _PAGE_FURNITURE_ANNOTATIONS
    )


def semantic_page_furniture_source_indices(
    document: ProviderDocument,
) -> frozenset[int]:
    """Resolve semantic furniture from source-bound cross-page repetition.

    Page numbers and empty typed page frames are always evidence-only.  A
    non-empty header/footer is evidence-only only when the same normalized text
    appears in the same frame role on at least two distinct pages.  Once exact
    repetition proves one top header, a same-geometry typed-header variant may
    join that cluster only when it preserves that complete normalized header as
    an exact prefix and its CJK/digit fingerprint is identical.  This covers a
    Latin suffix from bilingual rendering without demoting a different Latin
    issuer, unique announcement metadata, or terminal notices.
    """

    repeated_pages: dict[tuple[str, str], set[int]] = defaultdict(set)
    classified: list[tuple[ProviderBlock, str, str]] = []
    for block in document.blocks:
        frame_kind = _page_frame_kind(block)
        if frame_kind is None:
            continue
        text = _normalized_frame_text(block)
        classified.append((block, frame_kind, text))
        if text and frame_kind != "page_number":
            repeated_pages[(frame_kind, text)].add(block.page_index)

    repeated_or_empty = {
        block.source_index
        for block, frame_kind, text in classified
        if frame_kind == "page_number"
        or not text
        or len(repeated_pages[(frame_kind, text)]) >= 2
    }
    repeated_headers = tuple(
        (block, text)
        for block, frame_kind, text in classified
        if frame_kind == "header"
        and text
        and len(repeated_pages[(frame_kind, text)]) >= 2
    )
    repeated_header_variants = {
        block.source_index
        for block, frame_kind, text in classified
        if frame_kind == "header"
        and block.source_index not in repeated_or_empty
        and _matches_repeated_header_variant(
            block,
            text=text,
            repeated_headers=repeated_headers,
        )
    }
    exact_identity_headers = _exact_identity_header_source_indices(
        document,
        excluded=frozenset((*repeated_or_empty, *repeated_header_variants)),
    )
    return frozenset(
        (*repeated_or_empty, *repeated_header_variants, *exact_identity_headers)
    )


def _matches_repeated_header_variant(
    block: ProviderBlock,
    *,
    text: str,
    repeated_headers: tuple[tuple[ProviderBlock, str], ...],
) -> bool:
    if (
        block.provider_type.casefold() != "header"
        or (block.typed_annotation or "").casefold() != "page_header"
        or block.bbox is None
    ):
        return False
    fingerprint = _header_variant_fingerprint(text)
    if len(fingerprint) < _HEADER_VARIANT_MIN_FINGERPRINT_CHARS:
        return False
    return any(
        repeated.page_index != block.page_index
        and repeated.bbox is not None
        and _same_header_geometry(block, repeated)
        and text.startswith(repeated_text)
        and _header_variant_fingerprint(repeated_text) == fingerprint
        for repeated, repeated_text in repeated_headers
    )


def _header_variant_fingerprint(text: str) -> str:
    return "".join(
        char
        for char in text
        if char.isdecimal() or "\u3400" <= char <= "\u9fff"
    )


def _same_header_geometry(left: ProviderBlock, right: ProviderBlock) -> bool:
    assert left.bbox is not None
    assert right.bbox is not None
    return all(
        abs(left_value - right_value) <= _HEADER_VARIANT_GEOMETRY_TOLERANCE
        for left_value, right_value in zip(
            left.bbox.as_tuple(),
            right.bbox.as_tuple(),
            strict=True,
        )
    )


def _exact_identity_header_source_indices(
    document: ProviderDocument,
    *,
    excluded: frozenset[int],
) -> frozenset[int]:
    """Demote a unique header only when a same-page typed field replays it."""

    result: set[int] = set()
    identity_field = re.compile(
        r"^(?:公司|证券|股票|债券|可转债)?(?:简称|代码)\s*[:：]\s*(?P<value>.+)$"
    )
    for page in document.pages:
        values: dict[str, set[int]] = defaultdict(set)
        for block in page.blocks:
            if is_provider_page_frame(block):
                continue
            for payload in block.payloads:
                match = identity_field.fullmatch(payload.text.strip())
                if match is not None and match.group("value").strip():
                    values[match.group("value").strip()].add(block.source_index)
        for block in page.blocks:
            if (
                block.source_index in excluded
                or block.provider_type.casefold() != "header"
                or (block.typed_annotation or "").casefold() != "page_header"
                or block.bbox is None
                or block.bbox.y1 > 200.0
            ):
                continue
            text = _normalized_frame_text(block)
            if text and len(values.get(text, ())) == 1:
                result.add(block.source_index)
    return frozenset(result)


def _page_frame_kind(block: ProviderBlock) -> str | None:
    provider_type = block.provider_type.casefold()
    annotation = (block.typed_annotation or "").casefold()
    if provider_type == "page_number" or annotation == "page_number":
        return "page_number"
    if provider_type == "header" or annotation == "page_header":
        return "header"
    if provider_type == "footer" or annotation == "page_footer":
        return "footer"
    return None


def _normalized_frame_text(block: ProviderBlock) -> str:
    return " ".join(
        re.sub(r"\s+", " ", payload.text).strip()
        for payload in block.payloads
        if payload.text.strip()
    )


__all__ = [
    "build_provider_table_projection",
    "is_provider_page_frame",
    "semantic_page_furniture_source_indices",
]
