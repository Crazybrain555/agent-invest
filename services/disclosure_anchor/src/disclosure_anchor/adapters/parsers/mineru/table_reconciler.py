"""Restore page-local carriers for proven MinerU aggregate tables.

MinerU 3.4 can concatenate later pages' table HTML into the first page's
``content_list`` item and leave empty table carriers on the following pages.
The sibling ``*_model.json`` retains page-local table detections. This module
restores those page-local HTML fragments only when both the logical source
cells and the builder-visible expanded grid prove that re-merging them is
semantically identical. Proven groups that cannot be restored safely keep the
aggregate carrier and gain model-backed page provenance only.

Logical-cell comparison deliberately avoids rowspan/colspan expansion, which
can make equivalent source HTML appear to have different rectangular grids.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    TABLE_RECONCILIATION_ALGORITHM_VERSION,
    parse_table_html,
    table_html_logical_rows,
)
from disclosure_anchor.adapters.unit_builder import rules
from disclosure_anchor.adapters.unit_builder.table_grid import (
    drop_blank_table_rows,
    merge_table_grids,
)


MODEL_BBOX_MAX_DELTA = 3.0
_TABLE_LOCATOR_KEY = "_mineru_aggregate_table_locator"


@dataclass(frozen=True)
class TableReconciliationStats:
    model_status: str
    content_tables: int
    algorithm_version: str = TABLE_RECONCILIATION_ALGORITHM_VERSION
    model_hash: str | None = None
    model_tables: int = 0
    uniquely_matched_tables: int = 0
    ambiguous_matches: int = 0
    candidate_groups: int = 0
    proven_groups: int = 0
    unproven_groups: int = 0
    restoration_rejected_groups: int = 0
    located_groups: int = 0
    located_tables: int = 0
    restored_groups: int = 0
    restored_tables: int = 0
    table_builder_semantics_version: str = rules.TABLE_BUILDER_SEMANTICS_VERSION

    def __post_init__(self) -> None:
        if self.algorithm_version != TABLE_RECONCILIATION_ALGORITHM_VERSION:
            raise ValueError("unexpected table reconciliation algorithm version")
        if (
            self.table_builder_semantics_version
            != rules.TABLE_BUILDER_SEMANTICS_VERSION
        ):
            raise ValueError("unexpected table-builder semantics version")
        statuses = {
            "absent",
            "supported",
            "unreadable",
            "invalid_json",
            "unsupported_schema",
        }
        if self.model_status not in statuses:
            raise ValueError("invalid table reconciliation model status")
        counters = {
            name: getattr(self, name)
            for name in (
                "content_tables",
                "model_tables",
                "uniquely_matched_tables",
                "ambiguous_matches",
                "candidate_groups",
                "proven_groups",
                "unproven_groups",
                "restoration_rejected_groups",
                "located_groups",
                "located_tables",
                "restored_groups",
                "restored_tables",
            )
        }
        if any(type(value) is not int or value < 0 for value in counters.values()):
            raise ValueError("table reconciliation counters must be non-negative integers")
        hash_required = self.model_status in {
            "supported",
            "invalid_json",
            "unsupported_schema",
        }
        if hash_required != (self.model_hash is not None):
            raise ValueError(
                "table reconciliation model status and model hash disagree"
            )
        if self.model_hash is not None and not re.fullmatch(
            r"sha256:[a-f0-9]{64}", self.model_hash
        ):
            raise ValueError("invalid table reconciliation model hash")
        if self.model_status != "supported":
            non_supported = {
                name: value
                for name, value in counters.items()
                if name != "content_tables"
            }
            if any(non_supported.values()):
                raise ValueError(
                    "non-supported table reconciliation statuses require zero counters"
                )
            return
        if self.model_hash is None:
            raise ValueError("supported table reconciliation requires model_hash")
        if self.candidate_groups != self.proven_groups + self.unproven_groups:
            raise ValueError("candidate groups must equal proven plus unproven groups")
        if self.proven_groups != self.restored_groups + self.restoration_rejected_groups:
            raise ValueError(
                "proven groups must equal restored plus restoration-rejected groups"
            )
        if self.located_groups != self.proven_groups:
            raise ValueError("located groups must equal proven groups")
        if self.uniquely_matched_tables > min(self.content_tables, self.model_tables):
            raise ValueError("unique matches exceed content or model table count")
        if self.located_tables > self.uniquely_matched_tables:
            raise ValueError("located tables exceed uniquely matched tables")
        if self.restored_tables > self.located_tables:
            raise ValueError("restored tables exceed located tables")
        if (self.located_groups == 0) != (self.located_tables == 0):
            raise ValueError("located group and table counters disagree")
        if self.located_tables < self.located_groups * 2:
            raise ValueError("each located group must contain at least two tables")
        if (self.restored_groups == 0) != (self.restored_tables == 0):
            raise ValueError("restored group and table counters disagree")
        if self.restored_tables < self.restored_groups * 2:
            raise ValueError("each restored group must contain at least two tables")

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "algorithm_version": self.algorithm_version,
            "table_builder_semantics_version": (
                self.table_builder_semantics_version
            ),
            "model_status": self.model_status,
            "model_hash": self.model_hash,
            "content_tables": self.content_tables,
            "model_tables": self.model_tables,
            "uniquely_matched_tables": self.uniquely_matched_tables,
            "ambiguous_matches": self.ambiguous_matches,
            "candidate_groups": self.candidate_groups,
            "proven_groups": self.proven_groups,
            "unproven_groups": self.unproven_groups,
            "restoration_rejected_groups": self.restoration_rejected_groups,
            "unresolved_groups": (
                self.unproven_groups + self.restoration_rejected_groups
            ),
            "located_groups": self.located_groups,
            "located_tables": self.located_tables,
            "restored_groups": self.restored_groups,
            "restored_tables": self.restored_tables,
        }


@dataclass(frozen=True)
class TableReconciliationResult:
    content_list: list[dict[str, Any]]
    stats: TableReconciliationStats


@dataclass(frozen=True)
class _ModelTable:
    model_index: int
    page_idx: int
    bbox: tuple[float, float, float, float]
    html: str


def reconcile_content_list_tables(
    content_list: list[dict[str, Any]], *, model_path: Path | None
) -> TableReconciliationResult:
    """Locate geometrically unique, logically proven aggregate table groups."""

    content_table_count = sum(
        1 for item in content_list if str(item.get("type") or "") == "table"
    )
    if model_path is None:
        return TableReconciliationResult(
            content_list=list(content_list),
            stats=TableReconciliationStats(
                model_status="absent", content_tables=content_table_count
            ),
        )

    model_tables, model_status, model_hash = _read_model_tables(model_path)
    if model_status != "supported":
        return TableReconciliationResult(
            content_list=list(content_list),
            stats=TableReconciliationStats(
                model_status=model_status,
                content_tables=content_table_count,
                model_hash=model_hash,
            ),
        )

    matches, ambiguous_matches = _unique_geometry_matches(
        content_list, model_tables
    )
    reconciled = list(content_list)
    located_indices: set[int] = set()
    allowed_intervening_indices = _allowed_intervening_furniture_indices(content_list)
    candidate_groups = 0
    proven_groups = 0
    unproven_groups = 0
    restoration_rejected_groups = 0
    located_groups = 0
    restored_groups = 0
    restored_tables = 0

    for current_index, current_item in enumerate(content_list):
        if (
            current_index in located_indices
            or str(current_item.get("type") or "") != "table"
            or not _table_html(current_item).strip()
            or current_index not in matches
        ):
            continue
        group = _following_empty_table_group(
            content_list,
            root_index=current_index,
            matches=matches,
            located_indices=located_indices,
            allowed_intervening_indices=allowed_intervening_indices,
        )
        if len(group) < 2:
            continue
        candidate_groups += 1
        if not _is_proven_page_concatenation(
            aggregate_html=_table_html(current_item),
            page_htmls=[matches[index].html for index in group],
        ):
            unproven_groups += 1
            continue
        proven_groups += 1
        located_groups += 1
        located_indices.update(group)
        if _can_restore_page_local_tables(
            content_list=content_list,
            group=group,
            matches=matches,
            allowed_intervening_indices=allowed_intervening_indices,
        ):
            _restore_page_local_tables(
                reconciled=reconciled,
                content_list=content_list,
                group=group,
                matches=matches,
            )
            restored_groups += 1
            restored_tables += len(group)
        else:
            restoration_rejected_groups += 1
            reconciled[current_index] = _with_aggregate_table_locator(
                current_item,
                group=group,
                matches=matches,
            )

    return TableReconciliationResult(
        content_list=reconciled,
        stats=TableReconciliationStats(
            model_status="supported",
            content_tables=content_table_count,
            model_hash=model_hash,
            model_tables=len(model_tables),
            uniquely_matched_tables=len(matches),
            ambiguous_matches=ambiguous_matches,
            candidate_groups=candidate_groups,
            proven_groups=proven_groups,
            unproven_groups=unproven_groups,
            restoration_rejected_groups=restoration_rejected_groups,
            located_groups=located_groups,
            located_tables=len(located_indices),
            restored_groups=restored_groups,
            restored_tables=restored_tables,
        ),
    )


def _restore_page_local_tables(
    *,
    reconciled: list[dict[str, Any]],
    content_list: list[dict[str, Any]],
    group: list[int],
    matches: dict[int, _ModelTable],
) -> None:
    """Replace one proven aggregate/ghost group with page-local model HTML."""

    for position, index in enumerate(group):
        item = dict(content_list[index])
        if "table_body" in item or "table_html" not in item:
            item["table_body"] = matches[index].html
        else:
            item["table_html"] = matches[index].html
        item.pop(_TABLE_LOCATOR_KEY, None)
        if position:
            # Empty-string placeholder arrays become payload/hash drift after
            # the ghost is restored into a real table carrier. They were
            # already semantically empty and are normalized to the mapper's
            # canonical empty form.
            item["table_caption"] = []
            item["table_footnote"] = []
        reconciled[index] = item


def _can_restore_page_local_tables(
    *,
    content_list: list[dict[str, Any]],
    group: list[int],
    matches: dict[int, _ModelTable],
    allowed_intervening_indices: set[int],
) -> bool:
    """Prove page-local mapping will re-merge to the aggregate table payload."""

    continuation_items = [content_list[index] for index in group[1:]]
    if any(
        not _blank_string_list(item.get("table_caption"))
        or not _blank_string_list(item.get("table_footnote"))
        for item in continuation_items
    ):
        return False
    if any(
        _contains_header_cells(matches[index].html) for index in group[1:]
    ):
        return False
    if _has_visual_furniture_conflict(
        content_list,
        group=group,
        allowed_intervening_indices=allowed_intervening_indices,
    ):
        return False
    aggregate_grid = _builder_visible_grid([_table_html(content_list[group[0]])])
    page_grid = _builder_visible_grid([matches[index].html for index in group])
    return aggregate_grid is not None and page_grid == aggregate_grid


def _has_visual_furniture_conflict(
    content_list: list[dict[str, Any]],
    *,
    group: list[int],
    allowed_intervening_indices: set[int],
) -> bool:
    """Reject a restore when page furniture can alter a continuation caption.

    MinerU source order is not visual order. The builder therefore recovers a
    statutory statement caption from any same-page furniture visibly above a
    table, including furniture serialized after the table carrier. A restore
    would turn that caption into a new S5 boundary, so reconciliation must run
    the same geometry check before replacing an empty ghost.
    """

    for table_index in group[1:]:
        table_item = content_list[table_index]
        page_idx = _page_index(table_item.get("page_idx"))
        table_bbox = _bbox(table_item.get("bbox"))
        if page_idx is None or table_bbox is None:
            return True
        for furniture_index, item in enumerate(content_list):
            furniture_kind = str(item.get("type") or "")
            if furniture_kind not in {
                "header",
                "footer",
                "page_number",
                "aside_text",
            }:
                continue
            title = " ".join(str(item.get("text") or "").split())
            structural = rules.is_structural_page_furniture_title(title)
            if (
                furniture_kind == "page_number"
                and structural
                and group[0] < furniture_index < group[-1]
            ):
                # S1 promotes the first closed-set structural furniture title
                # without a geometry requirement. A caption misclassified as
                # page_number between the aggregate carrier and its ghost can
                # therefore split restored page-local tables even at the page
                # bottom or outside the table's horizontal span.
                return True
            if _page_index(item.get("page_idx")) != page_idx:
                continue
            if furniture_kind == "page_number" and structural:
                return True
            furniture_bbox = _bbox(item.get("bbox"))
            if furniture_bbox is None:
                continue
            if furniture_kind in {"page_number", "aside_text"}:
                compact_page_number = re.sub(r"\s+", "", title)
                if (
                    compact_page_number.isdigit()
                    and 1 <= int(compact_page_number) <= 200
                    and furniture_bbox[0] < 200
                ):
                    # This is exactly the numeric candidate family used by
                    # S1's split-note recovery. It can pair with a same-line
                    # exact note title regardless of its position relative to
                    # the table, so page-local restore must fail closed.
                    return True
            visibly_above = furniture_bbox[3] <= table_bbox[1]
            horizontally_overlaps = max(furniture_bbox[0], table_bbox[0]) <= min(
                furniture_bbox[2], table_bbox[2]
            )
            if furniture_kind == "page_number" and visibly_above:
                # The builder can pair a left-margin numeric page_number with
                # a same-line exact note label, so horizontal overlap with the
                # table is not required for it to create a structural split.
                return True
            if not visibly_above or not horizontally_overlaps:
                continue
            # MinerU sometimes labels a left-margin statement caption as a
            # page_number. The builder can recover that same-page, visibly
            # above label as structure, so restoring the ghost table would
            # create a new S5 boundary even though the HTML grids are equal.
            # A real bottom page number never reaches this above-table gate.
            if (
                furniture_kind == "page_number"
                or structural
                or furniture_index not in allowed_intervening_indices
            ):
                return True
    return False


def _blank_string_list(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, list) and all(
        isinstance(item, str) and not item.strip() for item in value
    )


def _contains_header_cells(html: str) -> bool:
    rows, failed = table_html_logical_rows(html)
    return failed or any(cell[1] for row in rows for cell in row)


def _builder_visible_grid(
    htmls: list[str],
) -> (
    tuple[
        tuple[str, ...],
        tuple[tuple[str, ...], ...],
        tuple[tuple[int, int, int, int], ...],
    ]
    | None
):
    """Project HTML through the mapper/S5 grid semantics without business rules."""

    tables: list[dict[str, Any]] = []
    widths: list[int] = []
    for html in htmls:
        table, failed = parse_table_html(html)
        if failed:
            return None
        headers = [str(value) for value in table.get("headers") or []]
        rows = [
            [str(value) for value in row] for row in table.get("rows") or []
        ]
        width = len(headers) if headers else (len(rows[0]) if rows else 0)
        if width == 0 or any(len(row) != width for row in rows):
            return None
        merged_cells = [dict(cell) for cell in table.get("merged_cells") or []]
        tables.append(
            {"headers": headers, "rows": rows, "merged_cells": merged_cells}
        )
        widths.append(width)
    if not tables or len(set(widths)) != 1:
        return None

    headers, rows, merged_cells = merge_table_grids(tables)
    kept_rows, merged_cells, _dropped = drop_blank_table_rows(
        headers=headers,
        rows=rows,
        merged_cells=merged_cells,
    )
    merged_projection = tuple(
        (
            int(cell["row"]),
            int(cell["col"]),
            int(cell["rowspan"]),
            int(cell["colspan"]),
        )
        for cell in merged_cells
    )
    return (
        tuple(headers),
        tuple(tuple(row) for row in kept_rows),
        merged_projection,
    )
def _with_aggregate_table_locator(
    item: dict[str, Any],
    *,
    group: list[int],
    matches: dict[int, _ModelTable],
) -> dict[str, Any]:
    """Annotate one aggregate carrier without changing any table HTML."""

    located = dict(item)
    pages = [matches[index].page_idx + 1 for index in group]
    located[_TABLE_LOCATOR_KEY] = {
        "algorithm_version": TABLE_RECONCILIATION_ALGORITHM_VERSION,
        "page_span": [min(pages), max(pages)],
        "page_bboxes": [
            {
                "page_no": matches[index].page_idx + 1,
                "bbox": list(matches[index].bbox),
            }
            for index in group
        ],
        "model_table_indices": [matches[index].model_index for index in group],
        "continuation_source_item_indices": group[1:],
    }
    return located


def _read_model_tables(
    path: Path,
) -> tuple[list[_ModelTable], str, str | None]:
    try:
        raw = path.read_bytes()
    except OSError:
        return [], "unreadable", None
    model_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return [], "invalid_json", model_hash
    if not isinstance(payload, list):
        return [], "unsupported_schema", model_hash
    if all(isinstance(page, dict) for page in payload):
        tables, status = _pipeline_model_tables(payload)
        return tables, status, model_hash
    if all(isinstance(page, list) for page in payload):
        tables, status = _vlm_model_tables(payload)
        return tables, status, model_hash
    return [], "unsupported_schema", model_hash


def _pipeline_model_tables(payload: list[Any]) -> tuple[list[_ModelTable], str]:
    """Read pipeline ``page_info``/``layout_dets`` model output."""

    tables: list[_ModelTable] = []
    for page in payload:
        if not isinstance(page, dict):
            return [], "unsupported_schema"
        page_info = page.get("page_info")
        detections = page.get("layout_dets")
        if not isinstance(page_info, dict) or not isinstance(detections, list):
            return [], "unsupported_schema"
        page_idx = _page_index(page_info.get("page_no"))
        width = _positive_float(page_info.get("width"))
        height = _positive_float(page_info.get("height"))
        if page_idx is None or width is None or height is None:
            return [], "unsupported_schema"
        for detection in detections:
            if not isinstance(detection, dict):
                return [], "unsupported_schema"
            if detection.get("label") != "table":
                continue
            html = detection.get("html")
            bbox = _bbox(detection.get("bbox"))
            if not isinstance(html, str) or not html.strip() or bbox is None:
                return [], "unsupported_schema"
            tables.append(
                _ModelTable(
                    model_index=len(tables),
                    page_idx=page_idx,
                    bbox=(
                        bbox[0] / width * 1000,
                        bbox[1] / height * 1000,
                        bbox[2] / width * 1000,
                        bbox[3] / height * 1000,
                    ),
                    html=html,
                )
            )
    return tables, "supported"


def _vlm_model_tables(payload: list[Any]) -> tuple[list[_ModelTable], str]:
    """Read VLM page lists whose table bboxes are normalized to 0..1."""

    tables: list[_ModelTable] = []
    for page_idx, page in enumerate(payload):
        if not isinstance(page, list):  # pragma: no cover - caller schema gate
            return [], "unsupported_schema"
        for item in page:
            if not isinstance(item, dict):
                return [], "unsupported_schema"
            if item.get("type") != "table":
                continue
            html = item.get("content")
            bbox = _bbox(item.get("bbox"))
            if not isinstance(html, str) or not html.strip() or bbox is None:
                return [], "unsupported_schema"
            if any(coordinate < 0 or coordinate > 1 for coordinate in bbox):
                return [], "unsupported_schema"
            tables.append(
                _ModelTable(
                    model_index=len(tables),
                    page_idx=page_idx,
                    bbox=(
                        bbox[0] * 1000,
                        bbox[1] * 1000,
                        bbox[2] * 1000,
                        bbox[3] * 1000,
                    ),
                    html=html,
                )
            )
    return tables, "supported"


def _unique_geometry_matches(
    content_list: list[dict[str, Any]], model_tables: list[_ModelTable]
) -> tuple[dict[int, _ModelTable], int]:
    provisional: dict[int, _ModelTable] = {}
    ambiguous = 0
    for index, item in enumerate(content_list):
        if str(item.get("type") or "") != "table":
            continue
        page_idx = _page_index(item.get("page_idx"))
        bbox = _bbox(item.get("bbox"))
        if page_idx is None or bbox is None:
            continue
        candidates = [
            table
            for table in model_tables
            if table.page_idx == page_idx
            and _bbox_delta(bbox, table.bbox) <= MODEL_BBOX_MAX_DELTA
        ]
        if len(candidates) == 1:
            provisional[index] = candidates[0]
        elif len(candidates) > 1:
            ambiguous += 1

    owners: dict[int, list[int]] = {}
    for content_index, table in provisional.items():
        owners.setdefault(table.model_index, []).append(content_index)
    for content_indices in owners.values():
        if len(content_indices) <= 1:
            continue
        ambiguous += len(content_indices)
        for content_index in content_indices:
            provisional.pop(content_index, None)
    return provisional, ambiguous


def _following_empty_table_group(
    content_list: list[dict[str, Any]],
    *,
    root_index: int,
    matches: dict[int, _ModelTable],
    located_indices: set[int],
    allowed_intervening_indices: set[int],
) -> list[int]:
    root_page = _page_index(content_list[root_index].get("page_idx"))
    if root_page is None:
        return [root_index]
    group = [root_index]
    expected_page = root_page + 1
    for index in range(root_index + 1, len(content_list)):
        item = content_list[index]
        if str(item.get("type") or "") != "table":
            if index in allowed_intervening_indices:
                continue
            break
        page_idx = _page_index(item.get("page_idx"))
        if (
            index in located_indices
            or index not in matches
            or page_idx != expected_page
            or _table_html(item).strip()
        ):
            break
        group.append(index)
        expected_page += 1
    return group


def _allowed_intervening_furniture_indices(
    content_list: list[dict[str, Any]],
) -> set[int]:
    """Allow only mapper-dropped page numbers or proven running furniture.

    MinerU-declared headers/footers are not sufficient by themselves: unique
    body titles are sometimes misclassified as furniture. Exact repetition on
    at least two pages plus a matching page-margin bbox proves a running item
    without looking at business words or table subjects.
    """

    pages_by_signature: dict[tuple[str, str], set[int]] = {}
    candidates: dict[int, tuple[str, str]] = {}
    allowed = {
        index
        for index, item in enumerate(content_list)
        if str(item.get("type") or "") == "page_number"
    }
    for index, item in enumerate(content_list):
        kind = str(item.get("type") or "")
        if kind not in {"header", "footer"}:
            continue
        text = " ".join(str(item.get("text") or "").split())
        structural_text = re.sub(r"[（(]\s*续\s*[）)]$", "", text)
        page_idx = _page_index(item.get("page_idx"))
        bbox = _bbox(item.get("bbox"))
        in_margin = bool(
            bbox is not None
            and (
                (kind == "header" and bbox[3] <= 180)
                or (kind == "footer" and bbox[1] >= 820)
            )
        )
        if (
            not text
            or rules.is_structural_page_furniture_title(structural_text)
            or page_idx is None
            or not in_margin
        ):
            continue
        signature = (kind, text)
        candidates[index] = signature
        pages_by_signature.setdefault(signature, set()).add(page_idx)
    allowed.update(
        index
        for index, signature in candidates.items()
        if len(pages_by_signature[signature]) >= 2
    )
    return allowed


def _is_proven_page_concatenation(
    *, aggregate_html: str, page_htmls: list[str]
) -> bool:
    aggregate_rows, aggregate_failed = table_html_logical_rows(aggregate_html)
    pages: list[list[list[tuple[str, bool, int, int]]]] = []
    for html in page_htmls:
        rows, failed = table_html_logical_rows(html)
        if failed or not rows:
            return False
        pages.append(rows)
    if aggregate_failed or not aggregate_rows or len(pages) < 2:
        return False

    aggregate = tuple(tuple(row) for row in aggregate_rows)
    page_rows = tuple(tuple(tuple(row) for row in rows) for rows in pages)
    first_header = page_rows[0][0]

    @lru_cache(maxsize=None)
    def matches_from(page_index: int, aggregate_index: int) -> bool:
        if page_index == len(page_rows):
            return aggregate_index == len(aggregate)
        rows = page_rows[page_index]
        candidates = [rows]
        if page_index > 0 and rows[0] == first_header:
            candidates.append(rows[1:])
        for candidate in candidates:
            end = aggregate_index + len(candidate)
            if aggregate[aggregate_index:end] == candidate and matches_from(
                page_index + 1, end
            ):
                return True
        return False

    return matches_from(0, 0)


def _table_html(item: dict[str, Any]) -> str:
    value = item.get("table_body")
    if value is None:
        value = item.get("table_html")
    return str(value) if value is not None else ""


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(isinstance(part, bool) for part in value):
        return None
    try:
        parsed = tuple(float(part) for part in value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(part) for part in parsed):
        return None
    return parsed  # type: ignore[return-value]


def _bbox_delta(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(abs(a - b) for a, b in zip(left, right, strict=True))


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _page_index(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )
