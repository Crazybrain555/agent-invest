"""Select explicit provider payloads once; preserve all other occurrences."""

from __future__ import annotations

from disclosure_anchor.application.contracts.document_outline import DocumentOutline
from disclosure_anchor.application.contracts.html_visible_text import (
    html_visible_text_segments,
)
from disclosure_anchor.application.contracts.provider_document import (
    ProviderBlock,
    ProviderDocument,
)
from disclosure_anchor.application.contracts.provider_table_projection import (
    ProviderTableProjection,
)
from disclosure_anchor.application.contracts.retrieval_primary import (
    BlockRetrievalReason,
    BlockRetrievalSelection,
    RetrievalPrimaryProjection,
    RetrievalTarget,
    SearchTransform,
    UnitRetrievalSelection,
)
from disclosure_anchor.application.services.provider_table_projection import (
    is_provider_page_furniture,
)


_HTML_FIELDS = frozenset({"table_body"})
_IDENTITY_FIELDS = frozenset(
    {
        "text",
        "content",
        "code_body",
        "table_caption",
        "table_footnote",
        "image_caption",
        "image_footnote",
        "chart_caption",
        "chart_footnote",
        "code_caption",
        "list_items",
    }
)


def build_retrieval_primary_projection(
    document: ProviderDocument,
    outline: DocumentOutline,
    table_projection: ProviderTableProjection,
) -> RetrievalPrimaryProjection:
    """Build a complete DB-free selection with no inferred text or alias dedupe."""

    _validate_inputs(document, outline, table_projection)
    targets: list[RetrievalTarget] = []
    blocks: list[BlockRetrievalSelection] = []
    target_ids_by_source: dict[int, tuple[str, ...]] = {}

    for block in document.blocks:
        if is_provider_page_furniture(block):
            selection = BlockRetrievalSelection(
                source_index=block.source_index,
                raw_block_sha256=block.raw_item_sha256,
                disposition="evidence_only",
                reason="page_furniture",
                target_ids=(),
            )
            blocks.append(selection)
            target_ids_by_source[block.source_index] = ()
            continue
        block_targets = _block_targets(block)
        targets.extend(block_targets)
        target_ids = tuple(target.target_id for target in block_targets)
        target_ids_by_source[block.source_index] = target_ids
        evidence_reason: BlockRetrievalReason = (
            "visual_without_text"
            if block.referenced_artifact_roles and not target_ids
            else "empty_provider_carrier"
        )
        blocks.append(
            BlockRetrievalSelection(
                source_index=block.source_index,
                raw_block_sha256=block.raw_item_sha256,
                disposition="primary" if target_ids else "evidence_only",
                reason="searchable_payload" if target_ids else evidence_reason,
                target_ids=target_ids,
            )
        )

    unit_by_source = {
        source_index: unit.unit_index
        for unit in outline.units
        for source_index in unit.block_source_indices
    }
    table_indices_by_unit: dict[int, list[int]] = {
        unit.unit_index: [] for unit in outline.units
    }
    for table_index, logical_table in enumerate(table_projection.logical_tables):
        raw_member_sources = (
            logical_table.owner.block_source_index,
            *(part.block_source_index for part in logical_table.continuations),
        )
        if any(source_index is None for source_index in raw_member_sources):
            raise ValueError("logical table contains an unbound block")
        member_sources = tuple(
            source_index
            for source_index in raw_member_sources
            if source_index is not None
        )
        resolved_units = {unit_by_source[source_index] for source_index in member_sources}
        if len(resolved_units) != 1:
            raise ValueError("one logical table crosses coarse-unit boundaries")
        table_indices_by_unit[resolved_units.pop()].append(table_index)

    units: list[UnitRetrievalSelection] = []
    primary_sources = {
        block.source_index for block in blocks if block.disposition == "primary"
    }
    for unit in outline.units:
        unit_sources = tuple(
            source_index
            for source_index in unit.block_source_indices
            if source_index in primary_sources
        )
        units.append(
            UnitRetrievalSelection(
                unit_index=unit.unit_index,
                primary_block_source_indices=unit_sources,
                target_ids=tuple(
                    target_id
                    for source_index in unit.block_source_indices
                    for target_id in target_ids_by_source[source_index]
                ),
                logical_table_indices=tuple(table_indices_by_unit[unit.unit_index]),
            )
        )
    return RetrievalPrimaryProjection(
        source_pdf_sha256=document.source_pdf_sha256,
        provider_bundle_sha256=document.bundle_sha256,
        block_count=len(document.blocks),
        logical_table_count=len(table_projection.logical_tables),
        blocks=tuple(blocks),
        targets=tuple(targets),
        units=tuple(units),
    )


def replay_retrieval_target(
    document: ProviderDocument,
    target: RetrievalTarget,
) -> tuple[str, ...]:
    """Replay one target from its exact block and payload ordinal."""

    if target.source_index >= len(document.blocks):
        raise ValueError("retrieval target source index is out of range")
    block = document.blocks[target.source_index]
    if block.raw_item_sha256 != target.raw_block_sha256:
        raise ValueError("retrieval target block hash drifted")
    if target.payload_ordinal >= len(block.payloads):
        raise ValueError("retrieval target payload ordinal is out of range")
    payload = block.payloads[target.payload_ordinal]
    if payload.field != target.field or payload.item_index != target.item_index:
        raise ValueError("retrieval target payload identity drifted")
    expected_transform = _transform_for_field(payload.field)
    if expected_transform != target.transform:
        raise ValueError("retrieval target transform drifted")
    if target.transform == "identity.v1":
        return (payload.text,) if payload.text.strip() else ()
    return html_visible_text_segments(payload.text)


def _validate_inputs(
    document: ProviderDocument,
    outline: DocumentOutline,
    table_projection: ProviderTableProjection,
) -> None:
    if (
        outline.source_pdf_sha256 != document.source_pdf_sha256
        or outline.provider_bundle_sha256 != document.bundle_sha256
        or outline.block_count != len(document.blocks)
    ):
        raise ValueError("outline does not bind the exact provider document")
    if (
        table_projection.source_pdf_sha256 != document.source_pdf_sha256
        or table_projection.provider_bundle_sha256 != document.bundle_sha256
        or table_projection.table_block_source_indices
        != tuple(
            block.source_index
            for block in document.blocks
            if block.provider_type == "table"
        )
        or table_projection.physical_segment_count
        != len(document.physical_table_segments)
    ):
        raise ValueError("table projection does not bind the exact provider document")
    partition = tuple(
        source_index
        for unit in outline.units
        for source_index in unit.block_source_indices
    )
    if partition != tuple(range(len(document.blocks))):
        raise ValueError("outline units do not partition provider blocks")


def _block_targets(block: ProviderBlock) -> tuple[RetrievalTarget, ...]:
    targets: list[RetrievalTarget] = []
    for payload_ordinal, payload in enumerate(block.payloads):
        transform = _transform_for_field(payload.field)
        values: tuple[str, ...]
        if transform == "identity.v1":
            values = (payload.text,) if payload.text.strip() else ()
        else:
            values = html_visible_text_segments(payload.text)
        if not values:
            continue
        targets.append(
            RetrievalTarget(
                target_id=(
                    f"target:{block.source_index:08d}:{payload_ordinal:04d}"
                ),
                source_index=block.source_index,
                payload_ordinal=payload_ordinal,
                field=payload.field,
                item_index=payload.item_index,
                transform=transform,
                raw_block_sha256=block.raw_item_sha256,
            )
        )
    return tuple(targets)


def _transform_for_field(field: str) -> SearchTransform:
    if field in _HTML_FIELDS:
        return "html_visible_text_segments.v1"
    if field in _IDENTITY_FIELDS:
        return "identity.v1"
    raise ValueError(f"provider payload field is not registered for retrieval: {field}")


__all__ = [
    "build_retrieval_primary_projection",
    "replay_retrieval_target",
]
