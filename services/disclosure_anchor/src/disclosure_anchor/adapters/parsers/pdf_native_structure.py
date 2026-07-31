"""Read PDF logical structure and visible marked content without guessing text roles."""

from __future__ import annotations

from collections.abc import Mapping
import ctypes
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
from typing import Any, cast

from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import PDFObjRef, resolve1
from pdfminer.psparser import PSLiteral
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from disclosure_anchor.domain.errors import ParserOutputContractError
from disclosure_anchor.adapters.parsers.pdfium_geometry import (
    PageScreenGeometry,
    normalized_screen_bbox,
    normalized_screen_point,
    page_screen_geometry,
)
from disclosure_anchor.adapters.parsers.pdfium_runtime import PDFIUM_LOCK


NATIVE_PDF_STRUCTURE_VERSION = "native_pdf_structure.v2"
_MAX_DEPTH = 128
_OBJECT_TYPES = {
    pdfium_raw.FPDF_PAGEOBJ_UNKNOWN: "unknown",
    pdfium_raw.FPDF_PAGEOBJ_TEXT: "text",
    pdfium_raw.FPDF_PAGEOBJ_PATH: "path",
    pdfium_raw.FPDF_PAGEOBJ_IMAGE: "image",
    pdfium_raw.FPDF_PAGEOBJ_SHADING: "shading",
    pdfium_raw.FPDF_PAGEOBJ_FORM: "form",
}
_ROOT_FIELDS = frozenset(
    {
        "bookmarks",
        "contract_version",
        "diagnostics",
        "marked_content",
        "native_status",
        "nodes",
        "pdfium_tagged",
        "role_map",
        "segments",
        "source_pdf_page_count",
        "source_pdf_sha256",
    }
)
_MARKED_FIELDS = frozenset(
    {
        "bbox",
        "mcid",
        "mcid_marks",
        "object_depth",
        "object_order",
        "object_type",
        "page_idx",
        "stream_scope",
        "text",
    }
)
_STREAM_SCOPE_PAGE = "page_content"
_STREAM_SCOPE_UNRESOLVED_FORM = "nested_form_unresolved"
_STREAM_SCOPE_UNRESOLVED_MARKS = "multiple_mcid_marks_unresolved"
_MCID_MARK_FIELDS = frozenset({"mark_order", "mcid"})
_NODE_FIELDS = frozenset(
    {
        "ancestor_node_ids",
        "ancestor_roles",
        "declared_parent_object_id",
        "forward_parent_object_id",
        "mcid_refs",
        "node_id",
        "object_id",
        "parent_consistent",
        "raw_role",
        "segment_id",
        "standard_role",
    }
)
_SEGMENT_FIELDS = frozenset(
    {
        "node_id_span",
        "page_indices",
        "pages_contiguous",
        "segment_id",
        "top_object_id",
    }
)
_BOOKMARK_FIELDS = frozenset(
    {
        "bookmark_order",
        "destination_view",
        "destination_y",
        "level",
        "page_idx",
        "title",
    }
)
_OBJECT_ISSUE_FIELDS = frozenset({"mcid", "object_order", "page_idx", "reason"})
_OBJECT_ISSUE_REASONS = frozenset(
    {
        "bbox_unavailable",
        "multiple_mcid_marks_unresolved",
        "nested_stream_identity_unavailable",
        "text_unavailable",
    }
)
_DIAGNOSTIC_FIELDS = frozenset(
    {
        "marked_content_objects",
        "object_issues",
        "parent_conflicts",
        "referenced_mcid_refs",
        "resolved_mcid_refs",
        "root_reachable_nodes",
        "unresolved",
        "unresolved_mcid_refs",
        "visible_mcid_anchors",
    }
)


@dataclass(frozen=True, slots=True)
class NativeTableCell:
    """One page-local PDF structure cell and its visible object evidence."""

    cell_key: tuple[str, int, int]
    text: str
    bboxes: tuple[tuple[float, float, float, float], ...]


@dataclass(frozen=True, slots=True)
class NativeMarkedObject:
    """One page-content marked object and the visible evidence it carries."""

    page_idx: int
    mcid: int
    object_order: int
    object_type: str
    text: str | None
    bbox: tuple[float, float, float, float] | None


@dataclass(frozen=True, slots=True)
class NativeStructureNode:
    """One root-reachable StructTree element and its page-stream anchors."""

    node_id: int
    segment_id: str
    raw_role: str
    standard_role: str
    ancestor_roles: tuple[str, ...]
    ancestor_node_ids: tuple[int, ...]
    parent_consistent: bool | None
    mcid_refs: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class NativeBookmark:
    """One outline entry; an absent page keeps the destination unresolved."""

    bookmark_order: int
    level: int
    title: str
    page_idx: int | None
    destination_y: float | None


@dataclass(frozen=True, slots=True)
class NativeObjectIssue:
    """One marked object whose identity or content could not be read."""

    page_idx: int
    mcid: int
    object_order: int
    reason: str


@dataclass(frozen=True, slots=True)
class NativeStructureDiagnostics:
    """Closed counters of what the forward traversal could and could not bind."""

    parent_conflicts: int
    root_reachable_nodes: int
    visible_mcid_anchors: int
    marked_content_objects: int
    referenced_mcid_refs: int
    resolved_mcid_refs: int
    unresolved_reasons: tuple[str, ...]
    unresolved_mcid_refs: tuple[tuple[int, int], ...]
    object_issues: tuple[NativeObjectIssue, ...]


@dataclass(frozen=True, slots=True)
class NativeStructureIndex:
    """The single validated reading of one native PDF structure artifact.

    Every consumer takes its facts from here; the raw artifact mapping stays a
    persistence payload and is never interpreted a second time.
    """

    source_pdf_page_count: int
    native_status: str
    pdfium_tagged: bool
    nodes: tuple[NativeStructureNode, ...]
    bookmarks: tuple[NativeBookmark, ...]
    marked_objects: Mapping[tuple[int, int], tuple[NativeMarkedObject, ...]]
    diagnostics: NativeStructureDiagnostics
    table_cells: tuple[NativeTableCell, ...]
    table_guard_bboxes: tuple[
        tuple[int, tuple[float, float, float, float]],
        ...,
    ]


def extract_pdf_structure(
    input_pdf: Path,
    *,
    source_pdf_sha256: str,
) -> dict[str, Any]:
    """Extract the root-reachable forward StructTree and its visible MCID anchors."""

    actual_hash = _sha256(input_pdf)
    if actual_hash != source_pdf_sha256:
        raise ParserOutputContractError(
            "native PDF structure source hash differs from the registered raw PDF"
        )
    forward = _extract_forward_tree(input_pdf)
    visible, bookmarks, pdfium_tagged, object_issues = _extract_pdfium_evidence(
        input_pdf,
        page_count=forward["page_count"],
        wanted_pages=set(forward["referenced_page_indices"]),
    )
    parent_conflicts = sum(
        1 for node in forward["nodes"] if node["parent_consistent"] is False
    )
    unresolved = list(forward["unresolved"])
    referenced_refs = {
        (int(ref["page_idx"]), int(ref["mcid"]))
        for node in forward["nodes"]
        for ref in node["mcid_refs"]
    }
    resolved_refs = {
        (int(item["page_idx"]), int(item["mcid"]))
        for item in visible
        if item["stream_scope"] == _STREAM_SCOPE_PAGE
    }
    unresolved_refs = sorted(referenced_refs - resolved_refs)
    native_status = (
        "untagged"
        if not forward["has_struct_tree"]
        else "malformed"
        if not forward["segments"]
        else "partial"
        if unresolved or unresolved_refs or object_issues
        else "usable"
    )
    return {
        "contract_version": NATIVE_PDF_STRUCTURE_VERSION,
        "source_pdf_sha256": source_pdf_sha256,
        "source_pdf_page_count": forward["page_count"],
        "native_status": native_status,
        "pdfium_tagged": pdfium_tagged,
        "role_map": forward["role_map"],
        "segments": forward["segments"],
        "nodes": forward["nodes"],
        "marked_content": visible,
        "bookmarks": bookmarks,
        "diagnostics": {
            "parent_conflicts": parent_conflicts,
            "unresolved": unresolved,
            "root_reachable_nodes": forward["root_reachable_nodes"],
            "visible_mcid_anchors": len(visible),
            "marked_content_objects": len(visible),
            "referenced_mcid_refs": len(referenced_refs),
            "resolved_mcid_refs": len(referenced_refs & resolved_refs),
            "unresolved_mcid_refs": [
                {"page_idx": page_idx, "mcid": mcid}
                for page_idx, mcid in unresolved_refs
            ],
            "object_issues": object_issues,
        },
    }


def validate_pdf_structure_artifact(
    value: object,
    *,
    expected_source_pdf_sha256: str,
    expected_page_count: int,
) -> NativeStructureIndex:
    """Validate the native artifact and expose every fact it proves."""

    if not isinstance(value, Mapping) or set(value) != _ROOT_FIELDS:
        raise ParserOutputContractError("native PDF structure fields are not closed")
    role_map = value.get("role_map")
    segments = value.get("segments")
    bookmarks = value.get("bookmarks")
    if (
        value.get("contract_version") != NATIVE_PDF_STRUCTURE_VERSION
        or value.get("source_pdf_sha256") != expected_source_pdf_sha256
        or value.get("source_pdf_page_count") != expected_page_count
        or value.get("native_status")
        not in {"untagged", "malformed", "partial", "usable"}
        or not isinstance(value.get("pdfium_tagged"), bool)
        or not isinstance(role_map, Mapping)
        or not isinstance(segments, list)
        or not isinstance(bookmarks, list)
    ):
        raise ParserOutputContractError("native PDF structure identity is invalid")
    if any(
        not isinstance(source, str)
        or not source
        or not isinstance(target, str)
        or not target
        for source, target in role_map.items()
    ):
        raise ParserOutputContractError("native PDF role map is invalid")
    segment_records = _validated_segments(
        segments,
        expected_page_count=expected_page_count,
    )
    bookmark_records = _validated_bookmarks(
        bookmarks,
        expected_page_count=expected_page_count,
    )
    marked = value.get("marked_content")
    nodes = value.get("nodes")
    diagnostics = value.get("diagnostics")
    if (
        not isinstance(marked, list)
        or not isinstance(nodes, list)
        or not isinstance(diagnostics, Mapping)
        or set(diagnostics) != _DIAGNOSTIC_FIELDS
    ):
        raise ParserOutputContractError("native PDF structure evidence is invalid")

    objects_by_ref: dict[tuple[int, int], list[NativeMarkedObject]] = {}
    object_orders: dict[int, list[int]] = {}
    object_keys: set[tuple[int, int, int]] = set()
    nested_object_keys: set[tuple[int, int, int]] = set()
    multiple_mark_keys: set[tuple[int, int, int]] = set()
    missing_bbox_keys: set[tuple[int, int, int]] = set()
    missing_text_keys: set[tuple[int, int, int]] = set()
    for item in marked:
        if not isinstance(item, Mapping) or set(item) != _MARKED_FIELDS:
            raise ParserOutputContractError("marked-content object is not closed")
        page_idx = _required_index(item.get("page_idx"), "marked page")
        mcid = _required_index(item.get("mcid"), "marked MCID")
        object_order = _required_index(item.get("object_order"), "marked object order")
        object_depth = _required_index(item.get("object_depth"), "marked object depth")
        mcid_marks = item.get("mcid_marks")
        if not isinstance(mcid_marks, list) or not mcid_marks:
            raise ParserOutputContractError("marked-content MCID marks are invalid")
        validated_marks: list[int] = []
        validated_mark_orders: list[int] = []
        for mark in mcid_marks:
            if not isinstance(mark, Mapping) or set(mark) != _MCID_MARK_FIELDS:
                raise ParserOutputContractError("marked-content MCID mark is invalid")
            validated_mark_orders.append(
                _required_index(
                    mark.get("mark_order"),
                    "marked-content mark order",
                )
            )
            validated_marks.append(
                _required_index(mark.get("mcid"), "marked-content mark MCID")
            )
        stream_scope = item.get("stream_scope")
        object_type = item.get("object_type")
        if (
            page_idx >= expected_page_count
            or not isinstance(object_type, str)
            or object_type not in _OBJECT_TYPES.values()
            or not isinstance(stream_scope, str)
            or stream_scope
            not in {
                _STREAM_SCOPE_PAGE,
                _STREAM_SCOPE_UNRESOLVED_FORM,
                _STREAM_SCOPE_UNRESOLVED_MARKS,
            }
            or validated_mark_orders != sorted(set(validated_mark_orders))
            or mcid != validated_marks[0]
            or stream_scope == _STREAM_SCOPE_PAGE
            and (object_depth != 0 or len(validated_marks) != 1)
            or stream_scope == _STREAM_SCOPE_UNRESOLVED_FORM
            and (object_depth == 0 or len(validated_marks) != 1)
            or stream_scope == _STREAM_SCOPE_UNRESOLVED_MARKS
            and len(validated_marks) < 2
            or item.get("text") is not None
            and not isinstance(item.get("text"), str)
        ):
            raise ParserOutputContractError("marked-content object is invalid")
        object_key = (page_idx, mcid, object_order)
        object_keys.add(object_key)
        bbox = None if item.get("bbox") is None else _validated_bbox(item["bbox"])
        if bbox is None:
            missing_bbox_keys.add(object_key)
        text = cast("str | None", item.get("text"))
        if object_type == "text" and text is None:
            missing_text_keys.add(object_key)
        if stream_scope == _STREAM_SCOPE_PAGE:
            objects_by_ref.setdefault((page_idx, mcid), []).append(
                NativeMarkedObject(
                    page_idx=page_idx,
                    mcid=mcid,
                    object_order=object_order,
                    object_type=object_type,
                    text=text,
                    bbox=bbox,
                )
            )
        if object_depth > 0:
            nested_object_keys.add(object_key)
        if stream_scope == _STREAM_SCOPE_UNRESOLVED_MARKS:
            multiple_mark_keys.add(object_key)
        object_orders.setdefault(page_idx, []).append(object_order)
    if any(orders != sorted(set(orders)) for orders in object_orders.values()):
        raise ParserOutputContractError("marked-content object order is not unique")
    object_issues = diagnostics.get("object_issues")
    issue_records, issue_keys = _validated_object_issues(
        object_issues,
        object_keys=object_keys,
        expected_page_count=expected_page_count,
    )
    any_issue_keys = set().union(*issue_keys.values())

    referenced_refs: set[tuple[int, int]] = set()
    cells: list[NativeTableCell] = []
    table_guards: set[tuple[int, tuple[float, float, float, float]]] = set()
    nodes_by_id: dict[int, NativeStructureNode] = {}
    object_id_by_node: dict[int, int | None] = {}
    node_records: list[NativeStructureNode] = []
    for item in nodes:
        if not isinstance(item, Mapping) or set(item) != _NODE_FIELDS:
            raise ParserOutputContractError("native structure node is not closed")
        node_id = _required_index(item.get("node_id"), "native node id")
        if node_id != len(nodes_by_id) + 1:
            raise ParserOutputContractError("native structure node order is invalid")
        segment_id = item.get("segment_id")
        ancestor_roles = item.get("ancestor_roles")
        ancestor_node_ids = item.get("ancestor_node_ids")
        refs = item.get("mcid_refs")
        raw_role = item.get("raw_role")
        standard_role = item.get("standard_role")
        object_id = _optional_object_id(item.get("object_id"), "native object id")
        forward_parent_id = _optional_object_id(
            item.get("forward_parent_object_id"),
            "native forward parent id",
        )
        declared_parent_id = _optional_object_id(
            item.get("declared_parent_object_id"),
            "native declared parent id",
        )
        expected_consistency = (
            None
            if forward_parent_id is None or declared_parent_id is None
            else forward_parent_id == declared_parent_id
        )
        if (
            not isinstance(segment_id, str)
            or not segment_id
            or segment_id not in segment_records
            or not isinstance(raw_role, str)
            or not raw_role
            or not isinstance(standard_role, str)
            or not standard_role
            or standard_role
            != _standard_role(
                raw_role,
                cast(Mapping[str, str], role_map),
            )
            or not isinstance(ancestor_roles, list)
            or any(not isinstance(role, str) or not role for role in ancestor_roles)
            or not isinstance(ancestor_node_ids, list)
            or len(ancestor_node_ids) != len(ancestor_roles)
            or not isinstance(refs, list)
            or item.get("parent_consistent") is not expected_consistency
        ):
            raise ParserOutputContractError("native structure node ancestry is invalid")
        ancestor_ids = [
            _required_index(value, "native ancestor node id")
            for value in ancestor_node_ids
        ]
        if any(node_id < 1 for node_id in ancestor_ids):
            raise ParserOutputContractError("native ancestor node id is invalid")
        ancestors = [nodes_by_id.get(ancestor_id) for ancestor_id in ancestor_ids]
        if any(ancestor is None for ancestor in ancestors) or any(
            ancestor.segment_id != segment_id or ancestor.standard_role != role
            for ancestor, role in zip(ancestors, ancestor_roles, strict=True)
            if ancestor is not None
        ):
            raise ParserOutputContractError("native structure ancestry is inconsistent")
        if ancestors:
            parent = cast(NativeStructureNode, ancestors[-1])
            if ancestor_ids != [
                *parent.ancestor_node_ids,
                parent.node_id,
            ] or forward_parent_id != object_id_by_node[parent.node_id]:
                raise ParserOutputContractError(
                    "native structure parent chain is inconsistent"
                )
        node_refs: list[tuple[int, int]] = []
        for ref in refs:
            if not isinstance(ref, Mapping) or set(ref) != {"page_idx", "mcid"}:
                raise ParserOutputContractError("native MCID reference is invalid")
            page_idx = _required_index(ref.get("page_idx"), "native MCID page")
            mcid = _required_index(ref.get("mcid"), "native MCID")
            if page_idx >= expected_page_count:
                raise ParserOutputContractError("native MCID page is outside the PDF")
            node_refs.append((page_idx, mcid))
        if len(node_refs) != len(set(node_refs)):
            raise ParserOutputContractError("native MCID reference is duplicated")
        referenced_refs.update(node_refs)
        record = NativeStructureNode(
            node_id=node_id,
            segment_id=segment_id,
            raw_role=raw_role,
            standard_role=standard_role,
            ancestor_roles=tuple(cast(list[str], ancestor_roles)),
            ancestor_node_ids=tuple(ancestor_ids),
            parent_consistent=expected_consistency,
            mcid_refs=tuple(node_refs),
        )
        nodes_by_id[node_id] = record
        object_id_by_node[node_id] = object_id
        node_records.append(record)

    _validate_segment_closure(segment_records, node_records)
    leaf_owners: dict[tuple[int, int], set[int]] = {}
    for node in node_records:
        if node.standard_role in {"TD", "TH"}:
            for ref in node.mcid_refs:
                leaf_owners.setdefault(ref, set()).add(node.node_id)
    ambiguous_leaf_refs = {
        ref for ref, owners in leaf_owners.items() if len(owners) > 1
    }
    for node in node_records:
        if (
            node.standard_role not in {"TD", "TH"}
            or "Table" not in node.ancestor_roles
            or node.parent_consistent is False
            or any(
                nodes_by_id[ancestor_id].parent_consistent is False
                for ancestor_id in node.ancestor_node_ids
            )
        ):
            continue
        for (page_idx, _), objects in (
            (ref, objects_by_ref.get(ref, ())) for ref in node.mcid_refs
        ):
            table_guards.update(
                (page_idx, obj.bbox) for obj in objects if obj.bbox is not None
            )
        for page_idx in sorted({page for page, _ in node.mcid_refs}):
            page_refs = [ref for ref in node.mcid_refs if ref[0] == page_idx]
            if any(
                ref not in objects_by_ref or ref in ambiguous_leaf_refs
                for ref in page_refs
            ):
                continue
            cell_objects = sorted(
                (obj for ref in page_refs for obj in objects_by_ref.get(ref, ())),
                key=lambda obj: obj.object_order,
            )
            object_keys_for_cell = {
                (obj.page_idx, obj.mcid, obj.object_order) for obj in cell_objects
            }
            if (
                object_keys_for_cell & any_issue_keys
                or any(obj.bbox is None for obj in cell_objects)
                or any(
                    obj.object_type == "text" and obj.text is None
                    for obj in cell_objects
                )
            ):
                continue
            cell_text = "".join(
                obj.text for obj in cell_objects if obj.text is not None
            )
            bboxes = tuple(
                obj.bbox for obj in cell_objects if obj.bbox is not None
            )
            if cell_text and bboxes:
                cells.append(
                    NativeTableCell(
                        cell_key=(node.segment_id, node.node_id, page_idx),
                        text=cell_text,
                        bboxes=bboxes,
                    )
                )

    resolved_refs = referenced_refs & set(objects_by_ref)
    unresolved_refs = sorted(referenced_refs - set(objects_by_ref))
    expected_unresolved = [
        {"page_idx": page_idx, "mcid": mcid} for page_idx, mcid in unresolved_refs
    ]
    unresolved = diagnostics.get("unresolved")
    if (
        diagnostics.get("visible_mcid_anchors") != len(marked)
        or diagnostics.get("marked_content_objects") != len(marked)
        or diagnostics.get("referenced_mcid_refs") != len(referenced_refs)
        or diagnostics.get("resolved_mcid_refs") != len(resolved_refs)
        or diagnostics.get("unresolved_mcid_refs") != expected_unresolved
        or diagnostics.get("parent_conflicts")
        != sum(record.parent_consistent is False for record in node_records)
        or not isinstance(diagnostics.get("root_reachable_nodes"), int)
        or isinstance(diagnostics.get("root_reachable_nodes"), bool)
        or cast(int, diagnostics["root_reachable_nodes"]) < len(nodes)
        or not isinstance(unresolved, list)
        or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("reason"), str)
            or not item.get("reason")
            for item in unresolved
        )
        or not nested_object_keys.issubset(
            issue_keys["nested_stream_identity_unavailable"]
        )
        or not multiple_mark_keys.issubset(issue_keys["multiple_mcid_marks_unresolved"])
        or not missing_bbox_keys.issubset(issue_keys["bbox_unavailable"])
        or not missing_text_keys.issubset(issue_keys["text_unavailable"])
    ):
        raise ParserOutputContractError("native structure diagnostics do not close")
    status = value["native_status"]
    has_incomplete_evidence = bool(
        unresolved or unresolved_refs or cast(list[object], object_issues)
    )
    if (
        status == "untagged"
        and (segments or nodes or marked or unresolved)
        or status == "malformed"
        and bool(segments)
        or status == "usable"
        and (not segments or has_incomplete_evidence)
        or status == "partial"
        and (not segments or not has_incomplete_evidence)
    ):
        raise ParserOutputContractError("native structure status is inconsistent")
    return NativeStructureIndex(
        source_pdf_page_count=expected_page_count,
        native_status=cast(str, status),
        pdfium_tagged=cast(bool, value["pdfium_tagged"]),
        nodes=tuple(node_records),
        bookmarks=bookmark_records,
        marked_objects={
            ref: tuple(objects) for ref, objects in objects_by_ref.items()
        },
        diagnostics=NativeStructureDiagnostics(
            parent_conflicts=sum(
                record.parent_consistent is False for record in node_records
            ),
            root_reachable_nodes=cast(int, diagnostics["root_reachable_nodes"]),
            visible_mcid_anchors=len(marked),
            marked_content_objects=len(marked),
            referenced_mcid_refs=len(referenced_refs),
            resolved_mcid_refs=len(resolved_refs),
            unresolved_reasons=tuple(
                cast(str, cast(Mapping[str, Any], item)["reason"])
                for item in unresolved
            ),
            unresolved_mcid_refs=tuple(unresolved_refs),
            object_issues=issue_records,
        ),
        table_cells=tuple(cells),
        table_guard_bboxes=tuple(sorted(table_guards)),
    )


def _required_index(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ParserOutputContractError(f"{label} is invalid")
    return value


def _optional_object_id(value: object, label: str) -> int | None:
    if value is None:
        return None
    result = _required_index(value, label)
    if result < 1:
        raise ParserOutputContractError(f"{label} is invalid")
    return result


def _validated_segments(
    values: list[object],
    *,
    expected_page_count: int,
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping) or set(value) != _SEGMENT_FIELDS:
            raise ParserOutputContractError("native structure segment is not closed")
        segment_id = value.get("segment_id")
        span = value.get("node_id_span")
        pages = value.get("page_indices")
        _optional_object_id(value.get("top_object_id"), "segment object id")
        if (
            not isinstance(segment_id, str)
            or not segment_id
            or segment_id in result
            or not isinstance(span, list)
            or len(span) != 2
            or not isinstance(pages, list)
        ):
            raise ParserOutputContractError("native structure segment is invalid")
        start = _required_index(span[0], "segment node span")
        end = _required_index(span[1], "segment node span")
        page_indices = [_required_index(page, "segment page index") for page in pages]
        if (
            start < 1
            or start > end + 1
            or page_indices != sorted(set(page_indices))
            or any(page >= expected_page_count for page in page_indices)
            or value.get("pages_contiguous") is not _consecutive(page_indices)
        ):
            raise ParserOutputContractError("native structure segment is invalid")
        result[segment_id] = value
    return result


def _validate_segment_closure(
    segments: Mapping[str, Mapping[str, Any]],
    nodes: list[NativeStructureNode],
) -> None:
    for segment_id, segment in segments.items():
        segment_nodes = [node for node in nodes if node.segment_id == segment_id]
        ids = [node.node_id for node in segment_nodes]
        span = cast(list[int], segment["node_id_span"])
        expected_ids = list(range(span[0], span[1] + 1))
        pages = sorted({page for node in segment_nodes for page, _ in node.mcid_refs})
        if ids != expected_ids or pages != segment["page_indices"]:
            raise ParserOutputContractError("native structure segment does not close")


def _validated_bookmarks(
    values: list[object],
    *,
    expected_page_count: int,
) -> tuple[NativeBookmark, ...]:
    records: list[NativeBookmark] = []
    for order, value in enumerate(values):
        if not isinstance(value, Mapping) or set(value) != _BOOKMARK_FIELDS:
            raise ParserOutputContractError("native PDF bookmark is not closed")
        page_idx = value.get("page_idx")
        destination_y = value.get("destination_y")
        if (
            value.get("bookmark_order") != order
            or not isinstance(value.get("level"), int)
            or isinstance(value.get("level"), bool)
            or cast(int, value["level"]) < 1
            or not isinstance(value.get("title"), str)
            or page_idx is not None
            and (
                not isinstance(page_idx, int)
                or isinstance(page_idx, bool)
                or page_idx < 0
                or page_idx >= expected_page_count
            )
            or destination_y is not None
            and (
                not isinstance(destination_y, (int, float))
                or isinstance(destination_y, bool)
                or not math.isfinite(float(destination_y))
            )
        ):
            # Real outlines place /XYZ targets slightly outside the crop box;
            # a finite out-of-page destination stays a valid artifact fact and
            # is arbitrated downstream (alignment tolerance or
            # bookmark_unaligned), while ill-typed values still reject.
            raise ParserOutputContractError("native PDF bookmark is invalid")
        records.append(
            NativeBookmark(
                bookmark_order=order,
                level=cast(int, value["level"]),
                title=cast(str, value["title"]),
                page_idx=cast("int | None", page_idx),
                destination_y=(
                    None if destination_y is None else float(cast(float, destination_y))
                ),
            )
        )
    return tuple(records)


def _validated_object_issues(
    value: object,
    *,
    object_keys: set[tuple[int, int, int]],
    expected_page_count: int,
) -> tuple[tuple[NativeObjectIssue, ...], dict[str, set[tuple[int, int, int]]]]:
    if not isinstance(value, list):
        raise ParserOutputContractError("native object issues are invalid")
    records: list[NativeObjectIssue] = []
    result: dict[str, set[tuple[int, int, int]]] = {
        reason: set() for reason in _OBJECT_ISSUE_REASONS
    }
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _OBJECT_ISSUE_FIELDS:
            raise ParserOutputContractError("native object issue is not closed")
        key = (
            _required_index(item.get("page_idx"), "object issue page"),
            _required_index(item.get("mcid"), "object issue MCID"),
            _required_index(item.get("object_order"), "object issue order"),
        )
        reason = item.get("reason")
        if (
            key[0] >= expected_page_count
            or key not in object_keys
            or not isinstance(reason, str)
            or reason not in _OBJECT_ISSUE_REASONS
            or key in result[reason]
        ):
            raise ParserOutputContractError("native object issue is invalid")
        result[reason].add(key)
        records.append(
            NativeObjectIssue(
                page_idx=key[0],
                mcid=key[1],
                object_order=key[2],
                reason=reason,
            )
        )
    return tuple(records), result


def _validated_bbox(value: object) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(float(item))
            for item in value
        )
    ):
        raise ParserOutputContractError("native marked-content bbox is invalid")
    bbox = tuple(float(item) for item in value)
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3] or min(bbox) < 0 or max(bbox) > 1000:
        raise ParserOutputContractError("native marked-content bbox is invalid")
    return cast(tuple[float, float, float, float], bbox)


def _extract_forward_tree(input_pdf: Path) -> dict[str, Any]:
    with input_pdf.open("rb") as stream:
        document = PDFDocument(PDFParser(stream))
        pages = list(PDFPage.create_pages(document))
        page_by_object_id = {
            cast(int, page.pageid): index for index, page in enumerate(pages)
        }
        root_ref = document.catalog.get("StructTreeRoot")
        if root_ref is None:
            return {
                "has_struct_tree": False,
                "nodes": [],
                "page_count": len(pages),
                "referenced_page_indices": [],
                "role_map": {},
                "root_reachable_nodes": 0,
                "segments": [],
                "unresolved": [],
            }
        root = _resolved_mapping(root_ref, label="StructTreeRoot")
        root_type = _name(root.get("Type"))
        if root_type and root_type != "StructTreeRoot":
            raise ParserOutputContractError(
                f"PDF StructTreeRoot has unexpected Type /{root_type}"
            )
        role_map = _role_map(root.get("RoleMap"))
        top_children = _children(root.get("K"))
        nodes: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        segments: list[dict[str, Any]] = []
        referenced_pages: set[int] = set()
        traversal_count = 0

        def walk(
            raw_value: object,
            *,
            inherited_page_ref: object | None,
            forward_parent_id: int | None,
            segment_id: str,
            depth: int,
            stack: frozenset[int],
            ancestor_roles: tuple[str, ...],
            ancestor_node_ids: tuple[int, ...],
        ) -> list[tuple[int, int]]:
            nonlocal traversal_count
            if depth > _MAX_DEPTH:
                unresolved.append({"reason": "depth_limit", "segment_id": segment_id})
                return []
            traversal_count += 1
            object_id = _object_id(raw_value)
            if object_id is not None and object_id in stack:
                unresolved.append(
                    {
                        "object_id": object_id,
                        "reason": "forward_cycle",
                        "segment_id": segment_id,
                    }
                )
                return []
            resolved = resolve1(raw_value)
            if isinstance(resolved, int) and not isinstance(resolved, bool):
                return _direct_mcid(
                    resolved,
                    inherited_page_ref,
                    page_by_object_id=page_by_object_id,
                    segment_id=segment_id,
                    unresolved=unresolved,
                )
            if isinstance(resolved, list):
                return [
                    ref
                    for child in resolved
                    for ref in walk(
                        child,
                        inherited_page_ref=inherited_page_ref,
                        forward_parent_id=forward_parent_id,
                        segment_id=segment_id,
                        depth=depth + 1,
                        stack=stack,
                        ancestor_roles=ancestor_roles,
                        ancestor_node_ids=ancestor_node_ids,
                    )
                ]
            if not isinstance(resolved, Mapping):
                unresolved.append(
                    {
                        "reason": "invalid_kid",
                        "segment_id": segment_id,
                        "value_type": type(resolved).__name__,
                    }
                )
                return []
            node = dict(resolved)
            if _name(node.get("Type")) == "OBJR":
                # An object reference is valid logical content, but it carries
                # no page-stream MCID that can anchor a text carrier.
                return []
            if _name(node.get("Type")) == "MCR" or ("MCID" in node and "S" not in node):
                mcid = node.get("MCID")
                if not isinstance(mcid, int) or isinstance(mcid, bool) or mcid < 0:
                    unresolved.append(
                        {
                            "object_id": object_id,
                            "reason": "invalid_mcid",
                            "segment_id": segment_id,
                        }
                    )
                    return []
                if node.get("Stm") is not None:
                    issue, page_index = _stream_scoped_mcid_issue(
                        node,
                        object_id=object_id,
                        inherited_page_ref=inherited_page_ref,
                        page_by_object_id=page_by_object_id,
                        segment_id=segment_id,
                    )
                    unresolved.append(issue)
                    if page_index is not None:
                        referenced_pages.add(page_index)
                    return []
                return _direct_mcid(
                    mcid,
                    node.get("Pg", inherited_page_ref),
                    page_by_object_id=page_by_object_id,
                    segment_id=segment_id,
                    unresolved=unresolved,
                )
            raw_role = _name(node.get("S"))
            if not raw_role:
                unresolved.append(
                    {
                        "object_id": object_id,
                        "reason": "structure_role_missing",
                        "segment_id": segment_id,
                    }
                )
                return []
            node_type = _name(node.get("Type"))
            if node_type and node_type != "StructElem":
                unresolved.append(
                    {
                        "object_id": object_id,
                        "reason": "structure_type_invalid",
                        "segment_id": segment_id,
                    }
                )
                return []
            page_ref = node.get("Pg", inherited_page_ref)
            declared_parent_id = _object_id(node.get("P"))
            next_stack = stack | {object_id} if object_id is not None else stack
            node_position = len(nodes)
            standard_role = _standard_role(raw_role, role_map)
            node_id = node_position + 1
            nodes.append({})
            refs = _deduplicated_refs(
                [
                    ref
                    for child in _children(node.get("K"))
                    for ref in walk(
                        child,
                        inherited_page_ref=page_ref,
                        forward_parent_id=object_id,
                        segment_id=segment_id,
                        depth=depth + 1,
                        stack=next_stack,
                        ancestor_roles=(*ancestor_roles, standard_role),
                        ancestor_node_ids=(*ancestor_node_ids, node_id),
                    )
                ]
            )
            referenced_pages.update(page for page, _ in refs)
            nodes[node_position] = {
                "node_id": node_id,
                "object_id": object_id,
                "raw_role": raw_role,
                "standard_role": standard_role,
                "segment_id": segment_id,
                "ancestor_roles": list(ancestor_roles),
                "ancestor_node_ids": list(ancestor_node_ids),
                "forward_parent_object_id": forward_parent_id,
                "declared_parent_object_id": declared_parent_id,
                "parent_consistent": (
                    None
                    if forward_parent_id is None or declared_parent_id is None
                    else forward_parent_id == declared_parent_id
                ),
                "mcid_refs": [{"page_idx": page, "mcid": mcid} for page, mcid in refs],
            }
            return refs

        root_id = _object_id(root_ref)
        for segment_number, top_child in enumerate(top_children):
            segment_id = f"native_{segment_number + 1}"
            before = len(nodes)
            refs = _deduplicated_refs(
                walk(
                    top_child,
                    inherited_page_ref=None,
                    forward_parent_id=root_id,
                    segment_id=segment_id,
                    depth=0,
                    stack=frozenset(),
                    ancestor_roles=(),
                    ancestor_node_ids=(),
                )
            )
            page_indices = sorted({page for page, _ in refs})
            segments.append(
                {
                    "segment_id": segment_id,
                    "top_object_id": _object_id(top_child),
                    "node_id_span": [before + 1, len(nodes)],
                    "page_indices": page_indices,
                    "pages_contiguous": _consecutive(page_indices),
                }
            )
        return {
            "has_struct_tree": True,
            "nodes": nodes,
            "page_count": len(pages),
            "referenced_page_indices": sorted(referenced_pages),
            "role_map": role_map,
            "root_reachable_nodes": traversal_count,
            "segments": segments,
            "unresolved": unresolved,
        }


def _stream_scoped_mcid_issue(
    node: Mapping[str, Any],
    *,
    object_id: int | None,
    inherited_page_ref: object | None,
    page_by_object_id: Mapping[int, int],
    segment_id: str,
) -> tuple[dict[str, Any], int | None]:
    """Keep /Stm identity visible without pretending PDFium can bind it."""

    page_object_id = _object_id(node.get("Pg", inherited_page_ref))
    page_idx = (
        page_by_object_id.get(page_object_id) if page_object_id is not None else None
    )
    return (
        {
            "object_id": object_id,
            "reason": "stream_scoped_mcid",
            "segment_id": segment_id,
            "mcid": cast(int, node["MCID"]),
            "page_idx": page_idx,
            "stm_object_id": _object_id(node.get("Stm")),
            "stm_owner_object_id": _object_id(node.get("StmOwn")),
        },
        page_idx,
    )


def _extract_pdfium_evidence(
    input_pdf: Path,
    *,
    page_count: int,
    wanted_pages: set[int],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    bool,
    list[dict[str, Any]],
]:
    marked: list[dict[str, Any]] = []
    bookmarks: list[dict[str, Any]] = []
    object_issues: list[dict[str, Any]] = []
    destination_pages: dict[
        int,
        tuple[pdfium.PdfPage, PageScreenGeometry],
    ] = {}
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(input_pdf)
        try:
            if len(document) != page_count:
                raise ParserOutputContractError(
                    "PDF page count differs between structure and page-object readers"
                )
            tagged = bool(document.is_tagged())
            for page_index in sorted(wanted_pages):
                page = document[page_index]
                text_page = page.get_textpage()
                try:
                    geometry = page_screen_geometry(page)
                    for object_order, obj in enumerate(
                        page.get_objects(
                            filter=None,
                            max_depth=_MAX_DEPTH,
                            textpage=text_page,
                        )
                    ):
                        mcid_marks = _marked_content_ids(obj)
                        if not mcid_marks:
                            continue
                        mcid = mcid_marks[0]["mcid"]
                        text: str | None = None
                        if obj.type == pdfium_raw.FPDF_PAGEOBJ_TEXT:
                            try:
                                text = obj.extract() or None
                            except pdfium.PdfiumError:
                                pass
                            if text is None:
                                object_issues.append(
                                    _object_issue(
                                        page_index,
                                        mcid,
                                        object_order,
                                        "text_unavailable",
                                    )
                                )
                        try:
                            bbox = normalized_screen_bbox(
                                geometry,
                                obj.get_bounds(),
                            )
                        except pdfium.PdfiumError:
                            bbox = None
                        if bbox is None:
                            object_issues.append(
                                _object_issue(
                                    page_index,
                                    mcid,
                                    object_order,
                                    "bbox_unavailable",
                                )
                            )
                        stream_scope = (
                            _STREAM_SCOPE_UNRESOLVED_MARKS
                            if len(mcid_marks) > 1
                            else _STREAM_SCOPE_PAGE
                            if obj.level == 0
                            else _STREAM_SCOPE_UNRESOLVED_FORM
                        )
                        marked.append(
                            {
                                "page_idx": page_index,
                                "mcid": mcid,
                                "object_order": object_order,
                                "object_type": _OBJECT_TYPES.get(
                                    obj.type,
                                    "unknown",
                                ),
                                "object_depth": obj.level,
                                "stream_scope": stream_scope,
                                "mcid_marks": mcid_marks,
                                "text": text,
                                "bbox": list(bbox) if bbox is not None else None,
                            }
                        )
                        if obj.level > 0:
                            object_issues.append(
                                _object_issue(
                                    page_index,
                                    mcid,
                                    object_order,
                                    "nested_stream_identity_unavailable",
                                )
                            )
                        if len(mcid_marks) > 1:
                            object_issues.append(
                                _object_issue(
                                    page_index,
                                    mcid,
                                    object_order,
                                    "multiple_mcid_marks_unresolved",
                                )
                            )
                finally:
                    text_page.close()
                    page.close()
            for bookmark_order, bookmark in enumerate(
                document.get_toc(max_depth=_MAX_DEPTH)
            ):
                title = bookmark.get_title()
                destination = bookmark.get_dest()
                bookmark_page_index = destination.get_index() if destination else None
                view = destination.get_view() if destination else None
                destination_y = (
                    _destination_screen_y(
                        document,
                        destination,
                        page_idx=bookmark_page_index,
                        page_cache=destination_pages,
                    )
                    if destination is not None
                    else None
                )
                bookmarks.append(
                    {
                        "bookmark_order": bookmark_order,
                        "level": bookmark.level + 1,
                        "title": title,
                        "page_idx": bookmark_page_index,
                        "destination_y": destination_y,
                        "destination_view": (
                            [view[0], list(view[1])] if view is not None else None
                        ),
                    }
                )
        finally:
            for page, _ in destination_pages.values():
                page.close()
            document.close()
    return marked, bookmarks, tagged, object_issues


def _marked_content_ids(obj: pdfium.PdfObject) -> list[dict[str, int]]:
    """Enumerate every numeric MCID mark instead of trusting the first mark."""

    count = pdfium_raw.FPDFPageObj_CountMarks(obj)
    if count < 0:
        raise ParserOutputContractError("cannot enumerate PDF content marks")
    result: list[dict[str, int]] = []
    for mark_order in range(count):
        mark = pdfium_raw.FPDFPageObj_GetMark(obj, mark_order)
        if not mark:
            raise ParserOutputContractError("cannot read PDF content mark")
        mcid = ctypes.c_int()
        if pdfium_raw.FPDFPageObjMark_GetParamIntValue(
            mark,
            b"MCID",
            ctypes.byref(mcid),
        ):
            if mcid.value < 0:
                raise ParserOutputContractError("PDF content mark MCID is negative")
            result.append({"mark_order": mark_order, "mcid": mcid.value})
    return result


def _object_issue(
    page_idx: int,
    mcid: int,
    object_order: int,
    reason: str,
) -> dict[str, Any]:
    return {
        "page_idx": page_idx,
        "mcid": mcid,
        "object_order": object_order,
        "reason": reason,
    }


def _destination_screen_y(
    document: pdfium.PdfDocument,
    destination: pdfium.PdfDest,
    *,
    page_idx: int | None,
    page_cache: dict[int, tuple[pdfium.PdfPage, PageScreenGeometry]],
) -> float | None:
    """Return an absolute bookmark Y only for a complete PDF /XYZ point."""

    if (
        page_idx is None
        or isinstance(page_idx, bool)
        or not isinstance(page_idx, int)
        or page_idx < 0
        or page_idx >= len(document)
    ):
        return None
    has_x = pdfium_raw.FPDF_BOOL()
    has_y = pdfium_raw.FPDF_BOOL()
    has_zoom = pdfium_raw.FPDF_BOOL()
    x = pdfium_raw.FS_FLOAT()
    y = pdfium_raw.FS_FLOAT()
    zoom = pdfium_raw.FS_FLOAT()
    ok = pdfium_raw.FPDFDest_GetLocationInPage(
        destination,
        ctypes.byref(has_x),
        ctypes.byref(has_y),
        ctypes.byref(has_zoom),
        ctypes.byref(x),
        ctypes.byref(y),
        ctypes.byref(zoom),
    )
    if not ok or not has_x.value or not has_y.value:
        return None
    if not math.isfinite(x.value) or not math.isfinite(y.value):
        return None
    cached = page_cache.get(page_idx)
    if cached is None:
        page = document[page_idx]
        cached = (
            page,
            page_screen_geometry(page),
        )
        page_cache[page_idx] = cached
    return normalized_screen_point(cached[1], x.value, y.value)[1]


def _direct_mcid(
    mcid: int,
    page_ref: object | None,
    *,
    page_by_object_id: Mapping[int, int],
    segment_id: str,
    unresolved: list[dict[str, Any]],
) -> list[tuple[int, int]]:
    page_object_id = _object_id(page_ref)
    page_index = (
        page_by_object_id.get(page_object_id) if page_object_id is not None else None
    )
    if page_index is None or mcid < 0:
        unresolved.append(
            {
                "mcid": mcid,
                "reason": "mcid_page_unresolved",
                "segment_id": segment_id,
            }
        )
        return []
    return [(page_index, mcid)]


def _role_map(value: object) -> dict[str, str]:
    if value is None:
        return {}
    mapping = _resolved_mapping(value, label="RoleMap")
    return {_name(key): _name(resolve1(target)) for key, target in mapping.items()}


def _standard_role(role: str, role_map: Mapping[str, str]) -> str:
    seen: set[str] = set()
    while role in role_map and role not in seen:
        seen.add(role)
        role = role_map[role]
    return role


def _children(value: object) -> list[object]:
    if value is None:
        return []
    resolved = resolve1(value)
    return list(resolved) if isinstance(resolved, list) else [value]


def _resolved_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    resolved = resolve1(value)
    if not isinstance(resolved, Mapping):
        raise ParserOutputContractError(f"PDF {label} must be an object")
    return resolved


def _object_id(value: object) -> int | None:
    return value.objid if isinstance(value, PDFObjRef) else None


def _name(value: object) -> str:
    if isinstance(value, PSLiteral):
        name = value.name
        return name.decode("latin-1") if isinstance(name, bytes) else name
    return value if isinstance(value, str) else ""


def _deduplicated_refs(values: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return list(dict.fromkeys(values))


def _consecutive(values: list[int]) -> bool:
    return not values or values == list(range(values[0], values[-1] + 1))


def _sha256(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot read raw PDF for native structure: {path}"
        ) from exc


__all__ = [
    "NATIVE_PDF_STRUCTURE_VERSION",
    "NativeBookmark",
    "NativeMarkedObject",
    "NativeObjectIssue",
    "NativeStructureDiagnostics",
    "NativeStructureIndex",
    "NativeStructureNode",
    "NativeTableCell",
    "extract_pdf_structure",
    "validate_pdf_structure_artifact",
]
