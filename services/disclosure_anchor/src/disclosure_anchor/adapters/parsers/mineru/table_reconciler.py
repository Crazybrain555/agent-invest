"""Locate every page of proven MinerU aggregate tables without splitting them.

MinerU 3.4 can concatenate later pages' table HTML into the first page's
``content_list`` item and leave empty table carriers on the following pages.
The sibling ``*_model.json`` retains page-local table detections. This module
proves which empty carriers belong to one aggregate and attaches complete
page/model provenance to that logical table. It deliberately never replaces
physical carriers: native-text/form recovery and unit boundaries can depend on
carrier shape even when expanded grids compare equal.

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
from typing import Any

from disclosure_anchor.adapters.parsers.mineru.geometry import (
    PAGE_BOTTOM_BAND_MIN,
    PAGE_TOP_BAND_MAX,
    bbox_delta,
    is_page_index,
)
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    resolved_table_html,
    table_html_logical_rows,
)
from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    TABLE_RECONCILIATION_ALGORITHM_VERSION,
    validate_table_reconciliation_diagnostics,
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
    locator_only_groups: int = 0
    locator_only_tables: int = 0
    restoration_rejected_groups: int = 0
    located_groups: int = 0
    located_tables: int = 0
    restored_groups: int = 0
    restored_tables: int = 0

    def __post_init__(self) -> None:
        if self.algorithm_version != TABLE_RECONCILIATION_ALGORITHM_VERSION:
            raise ValueError("unexpected table reconciliation algorithm version")
        validate_table_reconciliation_diagnostics(self.as_dict())

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "algorithm_version": self.algorithm_version,
            "model_status": self.model_status,
            "model_hash": self.model_hash,
            "content_tables": self.content_tables,
            "model_tables": self.model_tables,
            "uniquely_matched_tables": self.uniquely_matched_tables,
            "ambiguous_matches": self.ambiguous_matches,
            "candidate_groups": self.candidate_groups,
            "proven_groups": self.proven_groups,
            "unproven_groups": self.unproven_groups,
            "locator_only_groups": self.locator_only_groups,
            "locator_only_tables": self.locator_only_tables,
            "restoration_rejected_groups": self.restoration_rejected_groups,
            "unresolved_groups": self.unproven_groups,
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
    locator_only_groups = 0
    locator_only_tables = 0
    located_groups = 0

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
        locator_only_groups += 1
        locator_only_tables += len(group)
        # Never replace MinerU's physical carriers. Builder behavior includes
        # carrier-sensitive native-text/form recovery, so equal expanded grids
        # are not sufficient proof that page-local restoration preserves unit
        # boundaries or hashes. Keep one logical aggregate table and attach
        # complete page/model provenance instead.
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
            locator_only_groups=locator_only_groups,
            locator_only_tables=locator_only_tables,
            located_groups=located_groups,
            located_tables=len(located_indices),
        ),
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
            and bbox_delta(bbox, table.bbox) <= MODEL_BBOX_MAX_DELTA
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
            or _has_any_table_html(item)
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
        page_idx = _page_index(item.get("page_idx"))
        bbox = _bbox(item.get("bbox"))
        in_margin = bool(
            bbox is not None
            and (
                (kind == "header" and bbox[3] <= PAGE_TOP_BAND_MAX)
                or (kind == "footer" and bbox[1] >= PAGE_BOTTOM_BAND_MIN)
            )
        )
        if (
            not text
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
    return resolved_table_html(item) or ""


def _has_any_table_html(item: dict[str, Any]) -> bool:
    """Treat a carrier as empty only when every supported HTML field is empty."""

    return any(
        str(item[key]).strip()
        for key in ("table_body", "table_html")
        if key in item and item[key] is not None
    )


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
        if is_page_index(value)
        else None
    )
