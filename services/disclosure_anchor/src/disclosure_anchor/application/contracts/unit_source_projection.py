"""Versioned source-to-unit projection contract.

Artifact locators remain useful navigation hints.  This contract separately
states which concrete NormalizedIR field owns each public unit field, so an
auditor never has to infer ownership by recursively walking arbitrary JSON.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


UNIT_SOURCE_PROJECTION_VERSION = "unit-source-projection.v1"

SOURCE_IDENTITY_FIELDS = ("ir_id", "source_item_index", "order_index")
SOURCE_GEOMETRY_FIELDS = ("page_no", "bbox")
TABLE_LOCATOR_FIELDS = (
    "page_span",
    "page_bboxes",
    "model_table_indices",
    "continuation_source_item_indices",
    "table_locator_algorithm",
)

SOURCE_FIELD_KINDS = frozenset(
    {
        "text",
        "table",
        "table_caption",
        "table_header",
        "table_cell",
        "table_rows",
        "table_note",
        "table_html",
        "image",
        "image_caption",
        "image_footnote",
    }
)

PAYLOAD_PROJECTION_KINDS = frozenset(
    {
        "text_identity",
        "text_concat",
        "text_partition",
        "exact_duplicate_text",
        "table_identity",
        "table_partition",
        "image_identity",
        "container",
    }
)

HEADING_PROJECTION_KINDS = frozenset(
    {
        "document_metadata",
        "source_field",
        "source_concat",
    }
)


def source_ref_from_locator(locator: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return one strict source reference, or ``None`` for legacy/incomplete data."""

    ir_id = locator.get("ir_id")
    source_item_index = locator.get("source_item_index")
    order_index = locator.get("order_index")
    if (
        not isinstance(ir_id, str)
        or not ir_id
        or not isinstance(source_item_index, int)
        or isinstance(source_item_index, bool)
        or not isinstance(order_index, int)
        or isinstance(order_index, bool)
    ):
        return None
    ref: dict[str, Any] = {
        "ir_id": ir_id,
        "source_item_index": source_item_index,
        "order_index": order_index,
    }
    for field in SOURCE_GEOMETRY_FIELDS:
        if field in locator:
            ref[field] = locator[field]
    table_fields = [field for field in TABLE_LOCATOR_FIELDS if field in locator]
    if table_fields:
        for field in TABLE_LOCATOR_FIELDS:
            if field not in locator:
                return None
            ref[field] = locator[field]
    return ref


def source_selector(
    locator: Mapping[str, Any],
    *,
    field: str,
    index: int | None = None,
    row: int | None = None,
    column: int | None = None,
    row_indices: list[int] | None = None,
    char_span: list[int] | None = None,
    value_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Build a closed source-field selector from a concrete locator."""

    if field not in SOURCE_FIELD_KINDS:
        raise ValueError(f"unsupported source field: {field}")
    source = source_ref_from_locator(locator)
    if source is None:
        return None
    field_selector: dict[str, Any] = {"kind": field}
    if index is not None:
        field_selector["index"] = index
    if row is not None:
        field_selector["row"] = row
    if column is not None:
        field_selector["column"] = column
    if row_indices is not None:
        field_selector["row_indices"] = list(row_indices)
    if char_span is not None:
        field_selector["char_span"] = list(char_span)
    if value_sha256 is not None:
        field_selector["value_sha256"] = value_sha256
    return {"source": source, "field": field_selector}


def source_value_sha256(value: object) -> str:
    """Hash a selected source value using the projection contract encoding."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def empty_projection_graph() -> dict[str, Any]:
    """Return a fresh graph with every role present and no implicit edges."""

    return {
        "version": UNIT_SOURCE_PROJECTION_VERSION,
        "payload": None,
        "heading_path": [],
        "structured": [],
        "provenance": [],
    }
