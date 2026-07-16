"""Evidence-preserving projections of proven physical source carriers.

The parser owns physical carriers.  This module may expose independently
addressable slices of one carrier, but it never rewrites or discards the
original identity.  A projection is therefore useful only when its selector
can be replayed against NormalizedIR and audited without builder heuristics.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable
import unicodedata

from disclosure_anchor.adapters.unit_builder import rules
from disclosure_anchor.application.contracts.unit_source_projection import (
    source_selector,
    source_value_sha256,
)


PROJECTION_VERSION = "source-projection.v1"
OFFICIAL_IR_FORM_PROJECTION = "official-ir-form.v1"

_IR_FILING_TYPES = frozenset({"investor_relations", "performance_briefing"})
_METADATA_LABELS: dict[str, str] = {
    "投资者关系活动类别": "activity_category",
    "参与单位名称": "participants",
    "参与单位名称及人员姓名": "participants",
    "参与单位及人员姓名": "participants",
    "时间": "time",
    "地点": "location",
    "上市公司接待人员姓名": "company_participants",
}
_FOOTER_LABELS: dict[str, str] = {
    "附件清单如有": "attachment_list",
    "日期": "date",
}
_MIN_SIGNATURE_FIELDS = 4


@dataclass(frozen=True)
class SourceProjectionResult:
    elements: tuple[dict[str, Any], ...]
    status: str
    projected_carriers: int = 0
    reason_code: str | None = None


@dataclass(frozen=True)
class _FormAnchor:
    element_index: int
    narrative_row: int
    region_id: str


def project_official_ir_form(
    elements: Iterable[dict[str, Any]],
    *,
    filing_type: str | None,
) -> SourceProjectionResult:
    """Project a uniquely proven exchange-style IR form into source slices.

    The gate is deliberately structural: one simple two-column table must
    contain at least four distinct official metadata fields and exactly one
    official narrative field.  Partial label matches, merged grids, and
    multiple candidates are not interpreted.
    """

    source = [dict(element) for element in elements]
    if filing_type not in _IR_FILING_TYPES:
        return SourceProjectionResult(tuple(source), "not_applicable")

    anchors = [
        anchor
        for index, element in enumerate(source)
        if (anchor := _official_form_anchor(element, element_index=index)) is not None
    ]
    if not anchors:
        return SourceProjectionResult(
            tuple(source),
            "not_applicable",
            reason_code="official_ir_form_signature_absent",
        )
    if len(anchors) != 1:
        return SourceProjectionResult(
            tuple(source),
            "ambiguous",
            reason_code="official_ir_form_signature_not_unique",
        )

    anchor = anchors[0]
    output: list[dict[str, Any]] = []
    projected = 0
    phase = "before"
    for index, element in enumerate(source):
        if index < anchor.element_index:
            output.append(element)
            continue
        if index == anchor.element_index:
            pieces = _project_anchor_table(element, anchor=anchor)
            if pieces is None:
                return SourceProjectionResult(
                    tuple(source),
                    "ambiguous",
                    reason_code="official_ir_anchor_partition_ambiguous",
                )
            output.extend(pieces)
            projected += 1
            phase = (
                "footer"
                if any(
                    piece.get("projection_region_role") == "footer"
                    for piece in pieces
                )
                else "narrative"
            )
            continue

        caption = _first_caption(element)
        text = str(element.get("text") or "").strip()
        if rules.ATTACHMENT_CAPTION_RE.match(caption or text):
            phase = "attachment"
            output.append(_with_region(element, role="attachment", anchor=anchor))
            continue

        if phase == "narrative" and element.get("kind") == "table":
            pieces, next_phase = _project_narrative_table(element, anchor=anchor)
            if pieces is not None:
                output.extend(pieces)
                projected += 1
                phase = next_phase
                continue

        role = phase if phase in {"narrative", "footer", "attachment"} else None
        output.append(_with_region(element, role=role, anchor=anchor))

    return SourceProjectionResult(
        tuple(output),
        "proven",
        projected_carriers=projected,
        reason_code="official_ir_form_signature_unique",
    )


def _official_form_anchor(
    element: dict[str, Any], *, element_index: int
) -> _FormAnchor | None:
    rows = _simple_two_column_rows(element)
    if rows is None:
        return None
    metadata: set[str] = set()
    narrative_rows: list[int] = []
    for row_index, row in enumerate(rows):
        label = _label(row[0])
        canonical = _METADATA_LABELS.get(label)
        if canonical is not None:
            metadata.add(canonical)
        if rules.QA_FORM_NARRATIVE_LABEL_RE.fullmatch(row[0].strip()):
            narrative_rows.append(row_index)
    if len(metadata) < _MIN_SIGNATURE_FIELDS or len(narrative_rows) != 1:
        return None
    narrative_row = narrative_rows[0]
    if any(
        _METADATA_LABELS.get(_label(rows[index][0])) is None
        for index in range(narrative_row)
    ):
        return None
    identity = (
        str(element.get("ir_id") or "")
        or f"source:{element.get('source_item_index', element_index)}"
    )
    return _FormAnchor(
        element_index=element_index,
        narrative_row=narrative_row,
        region_id=f"official-ir-form:{identity}",
    )


def _project_anchor_table(
    element: dict[str, Any], *, anchor: _FormAnchor
) -> list[dict[str, Any]] | None:
    rows = _simple_two_column_rows(element)
    if rows is None:
        return None
    narrative_row = anchor.narrative_row
    before = list(range(narrative_row))
    after = list(range(narrative_row + 1, len(rows)))
    if any(_METADATA_LABELS.get(_label(rows[index][0])) is None for index in before):
        return None

    pieces: list[dict[str, Any]] = []
    if before:
        pieces.append(
            _table_rows_projection(
                element,
                row_indices=before,
                role="metadata",
                anchor=anchor,
                intra_order=len(pieces),
            )
        )
    pieces.append(
        _table_cell_projection(
            element,
            row=narrative_row,
            column=0,
            kind="heading",
            role="narrative",
            anchor=anchor,
            intra_order=len(pieces),
            heading_level=1,
        )
    )
    narrative_value = rows[narrative_row][1]
    if narrative_value:
        pieces.append(
            _table_cell_projection(
                element,
                row=narrative_row,
                column=1,
                kind="text",
                role="narrative",
                anchor=anchor,
                intra_order=len(pieces),
            )
        )
    if after:
        remainder = _partition_following_rows(rows, after)
        if remainder is None:
            return None
        narrative_indices, footer_indices = remainder
        for row_index in narrative_indices:
            pieces.append(
                _table_cell_projection(
                    element,
                    row=row_index,
                    column=1,
                    kind="text",
                    role="narrative",
                    anchor=anchor,
                    intra_order=len(pieces),
                )
            )
        if footer_indices:
            pieces.append(
                _table_rows_projection(
                    element,
                    row_indices=footer_indices,
                    role="footer",
                    anchor=anchor,
                    intra_order=len(pieces),
                )
            )
    _assign_table_annotations(element, pieces)
    return pieces


def _project_narrative_table(
    element: dict[str, Any], *, anchor: _FormAnchor
) -> tuple[list[dict[str, Any]] | None, str]:
    rows = _simple_two_column_rows(element)
    if rows is None:
        return None, "narrative"
    partition = _partition_following_rows(rows, list(range(len(rows))))
    if partition is None:
        return None, "narrative"
    narrative_indices, footer_indices = partition
    if not narrative_indices and not footer_indices:
        return None, "narrative"

    # A cell-only projection has no carrier for table caption/notes.  Keeping
    # the physical table is safer than silently losing those annotations.
    if not footer_indices and (
        _string_list(element.get("table_caption"))
        or _string_list(element.get("table_footnote"))
    ):
        return None, "narrative"

    pieces: list[dict[str, Any]] = []
    for row_index in narrative_indices:
        pieces.append(
            _table_cell_projection(
                element,
                row=row_index,
                column=1,
                kind="text",
                role="narrative",
                anchor=anchor,
                intra_order=len(pieces),
            )
        )
    if footer_indices:
        pieces.append(
            _table_rows_projection(
                element,
                row_indices=footer_indices,
                role="footer",
                anchor=anchor,
                intra_order=len(pieces),
            )
        )
    _assign_table_annotations(element, pieces)
    return pieces, "footer" if footer_indices else "narrative"


def _partition_following_rows(
    rows: list[list[str]], indices: list[int]
) -> tuple[list[int], list[int]] | None:
    narrative: list[int] = []
    footer: list[int] = []
    in_footer = False
    for index in indices:
        label, value = rows[index]
        canonical_footer = _FOOTER_LABELS.get(_label(label))
        if canonical_footer is not None:
            in_footer = True
            footer.append(index)
            continue
        if not in_footer and not label.strip() and value.strip():
            narrative.append(index)
            continue
        # Blank rows are source-empty and may stay inside a table-row slice;
        # every other label/value pattern is outside the proven form grammar.
        if in_footer and not label.strip() and not value.strip():
            footer.append(index)
            continue
        return None
    return narrative, footer


def _table_rows_projection(
    element: dict[str, Any],
    *,
    row_indices: list[int],
    role: str,
    anchor: _FormAnchor,
    intra_order: int,
) -> dict[str, Any]:
    rows = _simple_two_column_rows(element)
    assert rows is not None
    selected = [list(rows[index]) for index in row_indices]
    selector = {"kind": "table_rows", "row_indices": list(row_indices)}
    projected = _projection_base(
        element,
        selector=selector,
        source_value=selected,
        role=role,
        anchor=anchor,
        intra_order=intra_order,
    )
    projected.update(
        {
            "kind": "table",
            "raw_kind": "table",
            "table": {"headers": [], "rows": selected, "merged_cells": []},
            "table_caption": [],
            "table_footnote": [],
            "table_html": None,
            "table_parse_failed": False,
            "projection_inherits_section": role not in {"metadata", "footer"},
        }
    )
    return projected


def _table_cell_projection(
    element: dict[str, Any],
    *,
    row: int,
    column: int,
    kind: str,
    role: str,
    anchor: _FormAnchor,
    intra_order: int,
    heading_level: int | None = None,
) -> dict[str, Any]:
    rows = _simple_two_column_rows(element)
    assert rows is not None
    value = rows[row][column]
    selector = {
        "kind": "table_cell",
        "row": row,
        "column": column,
        "char_span": [0, len(value)],
    }
    projected = _projection_base(
        element,
        selector=selector,
        source_value=value,
        role=role,
        anchor=anchor,
        intra_order=intra_order,
    )
    projected.update(
        {
            "kind": kind,
            "raw_kind": "table_cell",
            "text": value,
            "projection_inherits_section": True,
        }
    )
    if heading_level is not None:
        projected["heading_level"] = heading_level
    return projected


def _projection_base(
    element: dict[str, Any],
    *,
    selector: dict[str, Any],
    source_value: object,
    role: str,
    anchor: _FormAnchor,
    intra_order: int,
) -> dict[str, Any]:
    projected = {
        key: value
        for key, value in element.items()
        if key
        not in {
            "kind",
            "raw_kind",
            "text",
            "heading_level",
            "table",
            "table_caption",
            "table_footnote",
            "table_html",
            "table_parse_failed",
            "derivation",
        }
    }
    projected.update(
        {
            "projection_intra_order": intra_order,
            "projection_region_role": role,
            "projection_region_id": anchor.region_id,
            "source_slice": _typed_source_slice(
                element,
                selector,
                source_value=source_value,
            ),
            "derivation": {
                "kind": "source_projection",
                "version": PROJECTION_VERSION,
                "projection": OFFICIAL_IR_FORM_PROJECTION,
                "region_id": anchor.region_id,
            },
        }
    )
    return projected


def _assign_table_annotations(
    source: dict[str, Any], pieces: list[dict[str, Any]]
) -> None:
    table_pieces = [piece for piece in pieces if piece.get("kind") == "table"]
    captions = _string_list(source.get("table_caption"))
    notes = _string_list(source.get("table_footnote"))
    if captions:
        assert table_pieces
        table_pieces[0]["table_caption"] = captions
        table_pieces[0].setdefault("annotation_source_slices", []).extend(
            _annotation_slice(
                source,
                field="table_caption",
                index=index,
                value=value,
            )
            for index, value in enumerate(captions)
        )
    if notes:
        assert table_pieces
        table_pieces[-1]["table_footnote"] = notes
        table_pieces[-1].setdefault("annotation_source_slices", []).extend(
            _annotation_slice(
                source,
                field="table_note",
                index=index,
                value=value,
            )
            for index, value in enumerate(notes)
        )


def _annotation_slice(
    source: dict[str, Any], *, field: str, index: int, value: str
) -> dict[str, Any]:
    selector = source_selector(
        source,
        field=field,
        index=index,
        value_sha256=source_value_sha256(value),
    )
    if selector is None:
        raise ValueError("source projection annotation requires source identity")
    return selector


def _with_region(
    element: dict[str, Any], *, role: str | None, anchor: _FormAnchor
) -> dict[str, Any]:
    if role is None:
        return dict(element)
    output = dict(element)
    output["projection_region_role"] = role
    output["projection_region_id"] = anchor.region_id
    if role == "footer":
        output["projection_inherits_section"] = False
    return output


def _simple_two_column_rows(element: dict[str, Any]) -> list[list[str]] | None:
    if element.get("kind") != "table" or element.get("table_parse_failed"):
        return None
    table = element.get("table")
    if not isinstance(table, dict):
        return None
    if table.get("headers") or table.get("merged_cells"):
        return None
    raw_rows = table.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        return None
    rows: list[list[str]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, list) or len(raw_row) != 2:
            return None
        rows.append([str(raw_row[0]), str(raw_row[1])])
    return rows


def _label(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"[\s:：()（）]", "", normalized).strip()


def _first_caption(element: dict[str, Any]) -> str:
    captions = _string_list(element.get("table_caption"))
    return captions[0].strip() if captions else ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _typed_source_slice(
    element: dict[str, Any],
    selector: dict[str, Any],
    *,
    source_value: object,
) -> dict[str, Any]:
    kind = selector.get("kind")
    if kind == "table_cell":
        typed = source_selector(
            element,
            field="table_cell",
            row=int(selector["row"]),
            column=int(selector["column"]),
            char_span=[int(value) for value in selector["char_span"]],
            value_sha256=source_value_sha256(source_value),
        )
    elif kind == "table_rows":
        typed = source_selector(
            element,
            field="table_rows",
            row_indices=[int(value) for value in selector["row_indices"]],
            value_sha256=source_value_sha256(source_value),
        )
    else:
        typed = None
    if typed is None:
        raise ValueError("source projection requires complete source identity")
    return typed
