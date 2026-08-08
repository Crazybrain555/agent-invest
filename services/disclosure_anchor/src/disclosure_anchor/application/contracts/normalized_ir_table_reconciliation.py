"""NormalizedIR contract for strict page-local MinerU table closure."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, NoReturn


# v6 compared exact cell text plus media bytes/alt/title; v7 compares the
# reader-visible table projection and additionally commits to the matching
# via a hashed receipt root. v6 stays readable forever; only v7 may drive
# current writes and publication.
LEGACY_TABLE_RECONCILIATION_ALGORITHM = "mineru-page-local-table-closure.v6"
CURRENT_TABLE_RECONCILIATION_ALGORITHM = "mineru-page-local-table-closure.v7"
TABLE_RECONCILIATION_ALGORITHM_VERSION = CURRENT_TABLE_RECONCILIATION_ALGORITHM
TABLE_COMPARISON_CONTRACT = "reader-visible-table-projection.v1"
_LEGACY_DIAGNOSTIC_FIELDS = {
    "algorithm_version",
    "model_hash",
    "content_tables",
    "model_tables",
    "matched_tables",
    "page_local_closed",
}
_DIAGNOSTIC_FIELDS = _LEGACY_DIAGNOSTIC_FIELDS | {
    "comparison_contract",
    "projection_root",
}
_LEGACY_LOCATOR_FIELDS = frozenset(
    {
        "continuation_source_item_indices",
        "model_table_indices",
        "page_bboxes",
        "page_span",
        "table_locator_algorithm",
    }
)
_SHA256_RE = re.compile(r"sha256:[a-f0-9]{64}")


class ReconciliationCompatibility(str, Enum):
    NONE = "none"
    LEGACY = "legacy"
    CURRENT = "current"


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


def _invalid(reason_code: str, message: str) -> NoReturn:
    raise TableReconciliationContractError(reason_code, message)


def validate_table_reconciliation_diagnostics(value: Any) -> None:
    """Validate the exact current page-local closure diagnostic."""

    _validated_current_diagnostics(value)


def validate_table_reconciliation_payload(
    payload: Any,
) -> TableReconciliationAssessment:
    return assess_normalized_ir_table_reconciliation(payload)


def assess_normalized_ir_table_reconciliation(
    payload: Any,
) -> TableReconciliationAssessment:
    if not isinstance(payload, dict):
        _invalid("payload_not_object", "normalized IR must be an object")
    elements = payload.get("elements")
    if not isinstance(elements, list) or not all(
        isinstance(element, dict) for element in elements
    ):
        _invalid("elements_not_array", "normalized IR elements must be objects")
    parser_diagnostics = payload.get("parser_diagnostics")
    if parser_diagnostics is None:
        return TableReconciliationAssessment(
            algorithm_version=None,
            compatibility=ReconciliationCompatibility.NONE,
        )
    if not isinstance(parser_diagnostics, dict):
        _invalid(
            "diagnostics_not_object",
            "normalized IR parser_diagnostics must be an object",
        )
    reconciliation = parser_diagnostics.get("table_reconciliation")
    if reconciliation is None:
        return TableReconciliationAssessment(
            algorithm_version=None,
            compatibility=ReconciliationCompatibility.NONE,
        )
    if not isinstance(reconciliation, dict):
        _invalid(
            "reconciliation_not_object",
            "table reconciliation diagnostics must be an object",
        )
    algorithm = reconciliation.get("algorithm_version")
    if algorithm == CURRENT_TABLE_RECONCILIATION_ALGORITHM:
        diagnostics = _validated_current_diagnostics(reconciliation)
        _validate_current_elements(
            elements,
            expected_tables=diagnostics["content_tables"],
        )
        return TableReconciliationAssessment(
            algorithm_version=algorithm,
            compatibility=ReconciliationCompatibility.CURRENT,
        )
    if algorithm == LEGACY_TABLE_RECONCILIATION_ALGORITHM:
        diagnostics = _validated_legacy_diagnostics(reconciliation)
        _validate_current_elements(
            elements,
            expected_tables=diagnostics["content_tables"],
        )
        return TableReconciliationAssessment(
            algorithm_version=algorithm,
            compatibility=ReconciliationCompatibility.LEGACY,
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


def _validated_current_diagnostics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _DIAGNOSTIC_FIELDS:
        _invalid(
            "diagnostic_fields",
            "page-local table closure diagnostics have invalid fields",
        )
    if value.get("algorithm_version") != CURRENT_TABLE_RECONCILIATION_ALGORITHM:
        _invalid(
            "current_algorithm_required",
            "page-local table closure requires the current algorithm",
        )
    if value.get("comparison_contract") != TABLE_COMPARISON_CONTRACT:
        _invalid(
            "comparison_contract",
            "page-local table closure requires the reader-visible "
            "projection contract",
        )
    projection_root = value.get("projection_root")
    if (
        not isinstance(projection_root, str)
        or _SHA256_RE.fullmatch(projection_root) is None
    ):
        _invalid(
            "projection_root",
            "page-local table closure requires the projection receipt root",
        )
    _validated_shared_diagnostics(value)
    return value


def _validated_legacy_diagnostics(value: Any) -> dict[str, Any]:
    """Keep the frozen v6 shape readable without gaining v7 semantics."""

    if not isinstance(value, dict) or set(value) != _LEGACY_DIAGNOSTIC_FIELDS:
        _invalid(
            "diagnostic_fields",
            "legacy page-local table closure diagnostics have invalid fields",
        )
    _validated_shared_diagnostics(value)
    return value


def _validated_shared_diagnostics(value: dict[str, Any]) -> None:
    model_hash = value.get("model_hash")
    if not isinstance(model_hash, str) or _SHA256_RE.fullmatch(model_hash) is None:
        _invalid(
            "model_hash",
            "page-local table closure requires the exact model artifact hash",
        )
    counters = ("content_tables", "model_tables", "matched_tables")
    if any(type(value.get(name)) is not int or value[name] < 0 for name in counters):
        _invalid(
            "counter",
            "page-local table closure counters must be non-negative integers",
        )
    if not (
        value["content_tables"]
        == value["model_tables"]
        == value["matched_tables"]
    ):
        _invalid(
            "closure_count",
            "content, model, and matched table counts must be equal",
        )
    if value.get("page_local_closed") is not True:
        _invalid(
            "closure_flag",
            "page-local table closure must be proven",
        )


def _validate_current_elements(
    elements: list[dict[str, Any]],
    *,
    expected_tables: int,
) -> None:
    tables = [element for element in elements if element.get("raw_kind") == "table"]
    if len(tables) != expected_tables:
        _invalid(
            "content_table_count",
            "content table count disagrees with normalized IR elements",
        )
    for element in tables:
        if element.get("kind") != "table":
            _invalid(
                "table_kind",
                "page-local table closure requires table carriers",
            )
        if _LEGACY_LOCATOR_FIELDS.intersection(element):
            _invalid(
                "legacy_locator_forbidden",
                "page-local table closure forbids aggregate table locators",
            )
        if (
            not isinstance(element.get("table_html"), str)
            or not element["table_html"].strip()
            or not isinstance(element.get("image_path"), str)
            or not element["image_path"].strip()
            or element.get("table_parse_failed") is not None
        ):
            _invalid(
                "table_evidence",
                "each page-local table requires HTML, image crop, and a parsed grid",
            )
        table = element.get("table")
        if not isinstance(table, dict):
            _invalid("table_grid", "page-local table grid must be an object")
        headers = table.get("headers")
        rows = table.get("rows")
        cells = table.get("cells")
        embedded_media = table.get("embedded_media")
        if (
            not isinstance(headers, list)
            or any(not isinstance(cell, str) for cell in headers)
            or not isinstance(rows, list)
            or any(
                not isinstance(row, list)
                or any(not isinstance(cell, str) for cell in row)
                for row in rows
            )
            or not (headers or rows)
            or not isinstance(cells, list)
            or not cells
            or not isinstance(embedded_media, list)
        ):
            _invalid(
                "table_grid",
                "page-local table closure requires logical cells and media occurrences",
            )
