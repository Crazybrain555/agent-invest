"""Typed native-structure fixtures for consumers of ``NativeStructureIndex``.

``build_mineru_structure_proof`` takes the validated index, so tests build the
typed facts directly instead of restating a raw artifact mapping.  Fixtures
that already carry a complete artifact keep using
``validate_pdf_structure_artifact`` so the production reading stays exercised.
"""

from __future__ import annotations

from collections.abc import Sequence

from disclosure_anchor.adapters.parsers.pdf_native_structure import (
    NativeBookmark,
    NativeMarkedObject,
    NativeStructureDiagnostics,
    NativeStructureIndex,
    NativeStructureNode,
)


EMPTY_DIAGNOSTICS = NativeStructureDiagnostics(
    parent_conflicts=0,
    root_reachable_nodes=0,
    visible_mcid_anchors=0,
    marked_content_objects=0,
    referenced_mcid_refs=0,
    resolved_mcid_refs=0,
    unresolved_reasons=(),
    unresolved_mcid_refs=(),
    object_issues=(),
)


def marked_object(
    page_idx: int,
    mcid: int,
    object_order: int,
    *,
    text: str | None,
    bbox: Sequence[float] | None,
    object_type: str = "text",
) -> NativeMarkedObject:
    return NativeMarkedObject(
        page_idx=page_idx,
        mcid=mcid,
        object_order=object_order,
        object_type=object_type,
        text=text,
        bbox=(
            None
            if bbox is None
            else (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        ),
    )


def native_node(
    node_id: int,
    standard_role: str,
    mcid_refs: Sequence[tuple[int, int]] = (),
    *,
    raw_role: str | None = None,
    segment_id: str = "native_1",
    ancestor_roles: tuple[str, ...] = (),
    ancestor_node_ids: tuple[int, ...] = (),
    parent_consistent: bool | None = True,
) -> NativeStructureNode:
    return NativeStructureNode(
        node_id=node_id,
        segment_id=segment_id,
        raw_role=standard_role if raw_role is None else raw_role,
        standard_role=standard_role,
        ancestor_roles=ancestor_roles,
        ancestor_node_ids=ancestor_node_ids,
        parent_consistent=parent_consistent,
        mcid_refs=tuple(mcid_refs),
    )


def native_bookmark(
    bookmark_order: int,
    level: int,
    title: str,
    *,
    page_idx: int | None = 0,
    destination_y: float | None = None,
) -> NativeBookmark:
    return NativeBookmark(
        bookmark_order=bookmark_order,
        level=level,
        title=title,
        page_idx=page_idx,
        destination_y=destination_y,
    )


def native_index(
    *,
    page_count: int = 1,
    native_status: str | None = None,
    pdfium_tagged: bool = False,
    nodes: Sequence[NativeStructureNode] = (),
    bookmarks: Sequence[NativeBookmark] = (),
    marked_objects: Sequence[NativeMarkedObject] = (),
    diagnostics: NativeStructureDiagnostics = EMPTY_DIAGNOSTICS,
) -> NativeStructureIndex:
    """Group page-content objects by MCID exactly as the validator does."""

    objects_by_ref: dict[tuple[int, int], tuple[NativeMarkedObject, ...]] = {}
    for item in marked_objects:
        key = (item.page_idx, item.mcid)
        objects_by_ref[key] = (*objects_by_ref.get(key, ()), item)
    return NativeStructureIndex(
        source_pdf_page_count=page_count,
        native_status=(
            native_status
            if native_status is not None
            else "usable"
            if nodes
            else "untagged"
        ),
        pdfium_tagged=pdfium_tagged,
        nodes=tuple(nodes),
        bookmarks=tuple(bookmarks),
        marked_objects=objects_by_ref,
        diagnostics=diagnostics,
        table_cells=(),
        table_guard_bboxes=(),
    )
