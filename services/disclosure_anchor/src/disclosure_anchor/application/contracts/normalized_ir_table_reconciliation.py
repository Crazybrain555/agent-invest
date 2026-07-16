"""NormalizedIR table-reconciliation extension contract.

This is the shared contract between parser producers and unit-builder
consumers. Provider-specific algorithm generations live here rather than in
the parser-neutral port DTOs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
from typing import Any, NoReturn


CURRENT_TABLE_RECONCILIATION_ALGORITHM = "mineru-aggregate-table-locator.v4"
LEGACY_RESTORE_ALGORITHM = "mineru-aggregate-table-restore.v3"
# Backward-compatible import name used by the MinerU adapter.
TABLE_RECONCILIATION_ALGORITHM_VERSION = CURRENT_TABLE_RECONCILIATION_ALGORITHM
_TABLE_BUILDER_SEMANTICS_V2 = "table-builder-semantics.v2"
_MODEL_STATUSES = {
    "absent",
    "supported",
    "unreadable",
    "invalid_json",
    "unsupported_schema",
}
_BASE_COUNTERS = (
    "content_tables",
    "model_tables",
    "uniquely_matched_tables",
    "ambiguous_matches",
    "candidate_groups",
    "proven_groups",
    "unproven_groups",
    "restoration_rejected_groups",
    "unresolved_groups",
    "located_groups",
    "located_tables",
    "restored_groups",
    "restored_tables",
)
_V4_COUNTERS = (
    "content_tables",
    "model_tables",
    "uniquely_matched_tables",
    "ambiguous_matches",
    "candidate_groups",
    "proven_groups",
    "unproven_groups",
    "locator_only_groups",
    "locator_only_tables",
    "restoration_rejected_groups",
    "unresolved_groups",
    "located_groups",
    "located_tables",
    "restored_groups",
    "restored_tables",
)
_LOCATOR_FIELDS = {
    "page_span",
    "page_bboxes",
    "model_table_indices",
    "continuation_source_item_indices",
    "table_locator_algorithm",
}
_BBOX_MAX_DELTA = 3.0


class ReconciliationCompatibility(str, Enum):
    NONE = "none"
    CURRENT = "current"
    LEGACY_CARRIER_PRESERVING = "legacy_carrier_preserving"
    REPARSE_REQUIRED = "reparse_required"


@dataclass(frozen=True)
class TableReconciliationAssessment:
    algorithm_version: str | None
    compatibility: ReconciliationCompatibility


class TableReconciliationContractError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class UnsupportedTableReconciliationAlgorithm(TableReconciliationContractError):
    pass


@dataclass(frozen=True)
class _PayloadContext:
    elements: list[dict[str, Any]]
    locators: list[dict[str, Any]]
    reconciliation: dict[str, Any] | None


def _invalid(reason_code: str, message: str) -> NoReturn:
    raise TableReconciliationContractError(reason_code, message)


def validate_table_reconciliation_diagnostics(value: Any) -> None:
    """Validate the current locator-only diagnostics emitted by the parser."""

    diagnostics = _validated_current_diagnostics(value)
    if diagnostics["algorithm_version"] != (CURRENT_TABLE_RECONCILIATION_ALGORITHM):
        _invalid(
            "current_algorithm_required",
            "current table reconciliation diagnostics use an unexpected algorithm",
        )


def validate_table_reconciliation_payload(
    payload: Any,
) -> TableReconciliationAssessment:
    """Validate and classify the table-reconciliation extension."""

    return assess_normalized_ir_table_reconciliation(payload)


def assess_normalized_ir_table_reconciliation(
    payload: Any,
) -> TableReconciliationAssessment:
    context = _payload_context(payload)
    reconciliation = context.reconciliation
    if reconciliation is None:
        return TableReconciliationAssessment(
            algorithm_version=None,
            compatibility=ReconciliationCompatibility.NONE,
        )

    algorithm = reconciliation.get("algorithm_version")
    if algorithm == CURRENT_TABLE_RECONCILIATION_ALGORITHM:
        diagnostics = _validated_current_diagnostics(reconciliation)
        _validate_locator_payload(
            context,
            algorithm=algorithm,
            model_table_count=diagnostics["model_tables"],
            content_table_count=diagnostics["content_tables"],
            expected_locator_groups=diagnostics["locator_only_groups"],
            expected_locator_tables=diagnostics["locator_only_tables"],
        )
        return TableReconciliationAssessment(
            algorithm_version=algorithm,
            compatibility=ReconciliationCompatibility.CURRENT,
        )

    if algorithm == LEGACY_RESTORE_ALGORITHM:
        diagnostics = _validated_legacy_diagnostics(reconciliation)
        expected_locator_tables = (
            diagnostics["located_tables"] - diagnostics["restored_tables"]
        )
        _validate_locator_payload(
            context,
            algorithm=algorithm,
            model_table_count=diagnostics["model_tables"],
            content_table_count=diagnostics["content_tables"],
            expected_locator_groups=diagnostics["restoration_rejected_groups"],
            expected_locator_tables=expected_locator_tables,
        )
        restored = bool(
            diagnostics["restored_groups"] or diagnostics["restored_tables"]
        )
        return TableReconciliationAssessment(
            algorithm_version=algorithm,
            compatibility=(
                ReconciliationCompatibility.REPARSE_REQUIRED
                if restored
                else ReconciliationCompatibility.LEGACY_CARRIER_PRESERVING
            ),
        )

    if isinstance(algorithm, str):
        raise UnsupportedTableReconciliationAlgorithm(
            "unsupported_algorithm",
            f"unsupported table reconciliation algorithm: {algorithm!r}",
        )
    _invalid(
        "invalid_algorithm",
        "table reconciliation algorithm_version must be a string",
    )


def _payload_context(payload: Any) -> _PayloadContext:
    if not isinstance(payload, dict):
        _invalid("payload_not_object", "normalized IR must be an object")
    elements_value = payload.get("elements")
    if not isinstance(elements_value, list):
        _invalid(
            "elements_not_array",
            "normalized IR elements must be an array",
        )
    elements: list[dict[str, Any]] = []
    locators: list[dict[str, Any]] = []
    for element in elements_value:
        if not isinstance(element, dict):
            _invalid(
                "element_not_object",
                "normalized IR elements must be objects",
            )
        elements.append(element)
        present = _LOCATOR_FIELDS.intersection(element)
        if not present:
            continue
        if (
            present != _LOCATOR_FIELDS
            or element.get("kind") != "table"
            or element.get("raw_kind") != "table"
        ):
            _invalid(
                "locator_bundle_incomplete",
                "aggregate table locator bundle is incomplete",
            )
        locators.append(element)

    diagnostics_value = payload.get("parser_diagnostics")
    if diagnostics_value is None:
        if locators:
            _invalid(
                "locator_without_diagnostics",
                "aggregate table locators require reconciliation diagnostics",
            )
        return _PayloadContext(elements, locators, None)
    if not isinstance(diagnostics_value, dict):
        _invalid(
            "diagnostics_not_object",
            "normalized IR parser_diagnostics must be an object",
        )
    reconciliation_value = diagnostics_value.get("table_reconciliation")
    if reconciliation_value is None:
        if locators:
            _invalid(
                "locator_without_diagnostics",
                "aggregate table locators require reconciliation diagnostics",
            )
        return _PayloadContext(elements, locators, None)
    if not isinstance(reconciliation_value, dict):
        _invalid(
            "reconciliation_not_object",
            "table reconciliation diagnostics must be an object",
        )
    return _PayloadContext(elements, locators, reconciliation_value)


def _validated_current_diagnostics(value: Any) -> dict[str, Any]:
    required = {
        "algorithm_version",
        "model_status",
        "model_hash",
        *_V4_COUNTERS,
    }
    diagnostics = _validated_common_diagnostics(
        value,
        required=required,
        counters=_V4_COUNTERS,
    )
    if diagnostics["algorithm_version"] != (CURRENT_TABLE_RECONCILIATION_ALGORITHM):
        _invalid(
            "current_algorithm_required",
            "locator-only diagnostics require the current algorithm",
        )
    if diagnostics["model_status"] != "supported":
        return diagnostics

    candidate = diagnostics["candidate_groups"]
    proven = diagnostics["proven_groups"]
    unproven = diagnostics["unproven_groups"]
    locator_groups = diagnostics["locator_only_groups"]
    locator_tables = diagnostics["locator_only_tables"]
    located_groups = diagnostics["located_groups"]
    located_tables = diagnostics["located_tables"]
    if candidate != proven + unproven:
        _invalid(
            "candidate_formula",
            "candidate groups must equal proven plus unproven groups",
        )
    if any(
        diagnostics[name]
        for name in (
            "restoration_rejected_groups",
            "restored_groups",
            "restored_tables",
        )
    ):
        _invalid(
            "restoration_forbidden",
            "locator-only reconciliation forbids table restoration",
        )
    if proven != locator_groups or located_groups != proven:
        _invalid(
            "locator_group_formula",
            "proven, located, and locator-only group counters disagree",
        )
    if diagnostics["unresolved_groups"] != unproven:
        _invalid(
            "unresolved_formula",
            "unresolved group counter is inconsistent",
        )
    if located_tables != locator_tables:
        _invalid(
            "locator_table_formula",
            "located tables must equal locator-only tables",
        )
    _validate_group_table_counts(
        groups=located_groups,
        tables=located_tables,
        label="located",
    )
    _validate_group_table_counts(
        groups=locator_groups,
        tables=locator_tables,
        label="locator-only",
    )
    _validate_candidate_capacity(diagnostics)
    return diagnostics


def _validated_legacy_diagnostics(value: Any) -> dict[str, Any]:
    required = {
        "algorithm_version",
        "table_builder_semantics_version",
        "model_status",
        "model_hash",
        *_BASE_COUNTERS,
    }
    diagnostics = _validated_common_diagnostics(
        value,
        required=required,
        counters=_BASE_COUNTERS,
    )
    if diagnostics["algorithm_version"] != LEGACY_RESTORE_ALGORITHM:
        _invalid(
            "legacy_algorithm_required",
            "legacy restoration diagnostics use an unexpected algorithm",
        )
    if diagnostics["table_builder_semantics_version"] != (_TABLE_BUILDER_SEMANTICS_V2):
        _invalid(
            "legacy_semantics_invalid",
            "legacy table-builder semantics version is invalid",
        )
    if diagnostics["model_status"] != "supported":
        return diagnostics

    candidate = diagnostics["candidate_groups"]
    proven = diagnostics["proven_groups"]
    unproven = diagnostics["unproven_groups"]
    rejected = diagnostics["restoration_rejected_groups"]
    restored_groups = diagnostics["restored_groups"]
    restored_tables = diagnostics["restored_tables"]
    located_groups = diagnostics["located_groups"]
    located_tables = diagnostics["located_tables"]
    if candidate != proven + unproven:
        _invalid(
            "candidate_formula",
            "candidate groups must equal proven plus unproven groups",
        )
    if proven != restored_groups + rejected:
        _invalid(
            "legacy_proven_formula",
            "legacy proven groups must equal restored plus rejected groups",
        )
    if diagnostics["unresolved_groups"] != unproven + rejected:
        _invalid(
            "legacy_unresolved_formula",
            "legacy unresolved groups must equal unproven plus rejected groups",
        )
    if located_groups != proven:
        _invalid(
            "legacy_located_formula",
            "legacy located groups must equal proven groups",
        )
    _validate_group_table_counts(
        groups=located_groups,
        tables=located_tables,
        label="legacy located",
    )
    _validate_group_table_counts(
        groups=restored_groups,
        tables=restored_tables,
        label="legacy restored",
    )
    if restored_tables > located_tables:
        _invalid(
            "legacy_table_formula",
            "legacy restored tables exceed located tables",
        )
    locator_tables = located_tables - restored_tables
    _validate_group_table_counts(
        groups=rejected,
        tables=locator_tables,
        label="legacy rejected locator",
    )
    _validate_candidate_capacity(diagnostics)
    return diagnostics


def _validated_common_diagnostics(
    value: Any,
    *,
    required: set[str],
    counters: tuple[str, ...],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        _invalid(
            "diagnostic_fields",
            "table reconciliation diagnostics have invalid fields",
        )
    diagnostics = value
    if any(
        type(diagnostics[name]) is not int or diagnostics[name] < 0 for name in counters
    ):
        _invalid(
            "negative_counter",
            "table reconciliation counters must be non-negative integers",
        )
    status = diagnostics["model_status"]
    if not isinstance(status, str) or status not in _MODEL_STATUSES:
        _invalid(
            "model_status",
            "invalid table reconciliation model status",
        )
    model_hash = diagnostics["model_hash"]
    hash_required = status in {
        "supported",
        "invalid_json",
        "unsupported_schema",
    }
    if hash_required and (
        not isinstance(model_hash, str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", model_hash) is None
    ):
        _invalid(
            "model_hash",
            "table reconciliation model status and hash disagree",
        )
    if not hash_required and model_hash is not None:
        _invalid(
            "model_hash",
            "table reconciliation model status and hash disagree",
        )
    if status != "supported" and any(
        diagnostics[name] for name in counters if name != "content_tables"
    ):
        _invalid(
            "unsupported_status_counters",
            "non-supported model statuses require zero derived counters",
        )
    if status == "supported":
        unique = diagnostics["uniquely_matched_tables"]
        ambiguous = diagnostics["ambiguous_matches"]
        content_tables = diagnostics["content_tables"]
        model_tables = diagnostics["model_tables"]
        if unique > min(content_tables, model_tables):
            _invalid(
                "unique_match_count",
                "unique matches exceed content or model tables",
            )
        if unique + ambiguous > content_tables:
            _invalid(
                "ambiguous_match_count",
                "unique and ambiguous matches exceed content tables",
            )
        if (ambiguous and model_tables == 0) or (ambiguous == 1 and model_tables < 2):
            _invalid(
                "ambiguous_match_model_count",
                "ambiguous matches require at least two model tables",
            )
    return diagnostics


def _validate_candidate_capacity(diagnostics: dict[str, Any]) -> None:
    if (
        diagnostics["located_tables"] + 2 * diagnostics["unproven_groups"]
        > diagnostics["uniquely_matched_tables"]
    ):
        _invalid(
            "candidate_capacity",
            "candidate groups exceed their uniquely matched tables",
        )


def _validate_group_table_counts(*, groups: int, tables: int, label: str) -> None:
    if (groups == 0) != (tables == 0) or tables < 2 * groups:
        _invalid(
            "group_table_count",
            f"{label} group and table counters disagree",
        )


def _validate_locator_payload(
    context: _PayloadContext,
    *,
    algorithm: str,
    model_table_count: int,
    content_table_count: int,
    expected_locator_groups: int,
    expected_locator_tables: int,
) -> None:
    actual_content_tables = sum(
        element.get("raw_kind") == "table" for element in context.elements
    )
    if actual_content_tables != content_table_count:
        _invalid(
            "content_table_count",
            "content table count disagrees with normalized IR elements",
        )

    elements_by_source_index: dict[int, dict[str, Any]] = {}
    for element in context.elements:
        source_index = element.get("source_item_index")
        if type(source_index) is not int or source_index < 0:
            continue
        if source_index in elements_by_source_index:
            _invalid(
                "duplicate_source_index",
                "normalized IR source item indices are duplicated",
            )
        elements_by_source_index[source_index] = element

    model_indices: list[int] = []
    continuation_indices: list[int] = []
    root_indices: list[int] = []
    for element in context.locators:
        if element["table_locator_algorithm"] != algorithm:
            _invalid(
                "locator_algorithm",
                "table locator and diagnostics algorithms disagree",
            )
        indices = element["model_table_indices"]
        continuations = element["continuation_source_item_indices"]
        page_span = element["page_span"]
        page_bboxes = element["page_bboxes"]
        if (
            not isinstance(indices, list)
            or len(indices) < 2
            or any(type(index) is not int or index < 0 for index in indices)
            or not isinstance(continuations, list)
            or len(continuations) != len(indices) - 1
            or any(type(index) is not int or index < 0 for index in continuations)
            or continuations != sorted(continuations)
            or not isinstance(page_span, list)
            or len(page_span) != 2
            or any(type(page) is not int or page < 1 for page in page_span)
            or page_span[0] >= page_span[1]
            or not isinstance(page_bboxes, list)
            or len(page_bboxes) != len(indices)
        ):
            _invalid(
                "locator_indices",
                "aggregate table locator indices are invalid",
            )
        pages = _validate_page_bboxes(page_bboxes)
        if pages != list(range(page_span[0], page_span[1] + 1)):
            _invalid(
                "locator_page_sequence",
                "aggregate table locator page sequence is invalid",
            )

        root_index = element.get("source_item_index")
        if type(root_index) is not int or root_index < 0:
            _invalid(
                "locator_root_index",
                "aggregate table locator root lacks a source index",
            )
        table_html = element.get("table_html")
        if not isinstance(table_html, str) or not table_html.strip():
            _invalid(
                "locator_root_html",
                "aggregate table locator root must contain table HTML",
            )
        _validate_table_grid(element.get("table"), require_content=True, label="root")
        root_page = element.get("page_no")
        if type(root_page) is not int or root_page != pages[0] or any(
            continuation <= root_index for continuation in continuations
        ):
            _invalid(
                "locator_source_order",
                "aggregate table locator source order is invalid",
            )
        _validate_element_bbox(
            element,
            expected=page_bboxes[0]["bbox"],
            label="root",
        )
        root_indices.append(root_index)
        for continuation_index, expected_page, expected_page_bbox in zip(
            continuations,
            pages[1:],
            page_bboxes[1:],
            strict=True,
        ):
            continuation = elements_by_source_index.get(continuation_index)
            table = continuation.get("table") if continuation is not None else None
            continuation_html = (
                continuation.get("table_html") if continuation is not None else None
            )
            continuation_page = (
                continuation.get("page_no") if continuation is not None else None
            )
            if (
                continuation is None
                or continuation.get("kind") != "table"
                or continuation.get("raw_kind") != "table"
                or not isinstance(continuation_html, str)
                or continuation_html.strip()
                or type(continuation_page) is not int
                or continuation_page != expected_page
            ):
                _invalid(
                    "locator_ghost",
                    "continuation source index does not name an empty table carrier",
                )
            _validate_table_grid(table, require_content=False, label="continuation")
            _validate_element_bbox(
                continuation,
                expected=expected_page_bbox["bbox"],
                label="continuation",
            )
        model_indices.extend(indices)
        continuation_indices.extend(continuations)

    if len(context.locators) != expected_locator_groups:
        _invalid(
            "locator_root_count",
            "locator root count disagrees with diagnostics",
        )
    if len(model_indices) != expected_locator_tables:
        _invalid(
            "locator_table_count",
            "locator table count disagrees with diagnostics",
        )
    if len(model_indices) != len(set(model_indices)) or any(
        index >= model_table_count for index in model_indices
    ):
        _invalid(
            "locator_model_indices",
            "locator model indices are reused or out of range",
        )
    if len(continuation_indices) != len(set(continuation_indices)):
        _invalid(
            "locator_continuation_overlap",
            "continuation source indices overlap across locator groups",
        )
    if len(root_indices) != len(set(root_indices)) or set(root_indices).intersection(
        continuation_indices
    ):
        _invalid(
            "locator_root_overlap",
            "locator roots and continuation sources overlap",
        )


def _validate_table_grid(value: Any, *, require_content: bool, label: str) -> None:
    if not isinstance(value, dict):
        _invalid(
            "locator_table_grid",
            f"aggregate table locator {label} requires a parsed table grid",
        )
    headers = value.get("headers")
    rows = value.get("rows")
    if (
        not isinstance(headers, list)
        or any(not isinstance(cell, str) for cell in headers)
        or not isinstance(rows, list)
        or any(
            not isinstance(row, list)
            or any(not isinstance(cell, str) for cell in row)
            for row in rows
        )
    ):
        _invalid(
            "locator_table_grid",
            f"aggregate table locator {label} has an invalid table grid",
        )
    has_content = any(cell.strip() for cell in headers) or any(
        cell.strip() for row in rows for cell in row
    )
    if require_content != has_content:
        _invalid(
            "locator_table_grid",
            (
                f"aggregate table locator {label} must contain a non-empty grid"
                if require_content
                else f"aggregate table locator {label} must contain an empty grid"
            ),
        )


def _validate_page_bboxes(page_bboxes: list[Any]) -> list[int]:
    pages: list[int] = []
    for page_bbox in page_bboxes:
        bbox = page_bbox.get("bbox") if isinstance(page_bbox, dict) else None
        page_no = page_bbox.get("page_no") if isinstance(page_bbox, dict) else None
        if (
            not isinstance(page_bbox, dict)
            or set(page_bbox) != {"page_no", "bbox"}
            or type(page_no) is not int
            or page_no < 1
            or _normalized_bbox(bbox) is None
        ):
            _invalid(
                "locator_bbox",
                "aggregate table locator bbox is invalid",
            )
        pages.append(page_no)
    return pages


def _validate_element_bbox(
    element: dict[str, Any], *, expected: Any, label: str
) -> None:
    actual_bbox = _normalized_bbox(element.get("bbox"))
    expected_bbox = _normalized_bbox(expected)
    if actual_bbox is None or expected_bbox is None:
        _invalid(
            "locator_element_bbox",
            f"aggregate table locator {label} requires a valid element bbox",
        )
    if (
        max(
            abs(left - right)
            for left, right in zip(actual_bbox, expected_bbox, strict=True)
        )
        > _BBOX_MAX_DELTA
    ):
        _invalid(
            "locator_element_bbox",
            f"aggregate table locator {label} bbox disagrees with element bbox",
        )


def _normalized_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(
        not isinstance(part, (int, float))
        or isinstance(part, bool)
        or not math.isfinite(float(part))
        for part in value
    ):
        return None
    bbox = (
        float(value[0]),
        float(value[1]),
        float(value[2]),
        float(value[3]),
    )
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        return None
    return bbox
