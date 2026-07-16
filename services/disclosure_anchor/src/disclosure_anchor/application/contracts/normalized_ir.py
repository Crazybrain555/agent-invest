"""Version authority for serialized NormalizedIR artifacts."""

from __future__ import annotations

from datetime import datetime
import math
from pathlib import PurePath
import re
from typing import Any, Mapping

from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    TableReconciliationContractError,
    assess_normalized_ir_table_reconciliation,
)


CURRENT_NORMALIZED_IR_VERSION = "normalized_ir.v3"
LEGACY_READABLE_NORMALIZED_IR_VERSIONS = frozenset({"normalized_ir.v2"})
READABLE_NORMALIZED_IR_VERSIONS = frozenset(
    {CURRENT_NORMALIZED_IR_VERSION, *LEGACY_READABLE_NORMALIZED_IR_VERSIONS}
)
_NORMALIZED_IR_VERSION_RE = re.compile(r"^normalized_ir\.v(?P<generation>[1-9][0-9]*)$")
_OLDEST_READABLE_GENERATION = min(
    int(version.rsplit("v", 1)[1]) for version in READABLE_NORMALIZED_IR_VERSIONS
)
_ROOT_REQUIRED = frozenset(
    {
        "contract_version",
        "created_at",
        "document_id",
        "elements",
        "parsed_pages",
        "parser",
        "parser_artifacts",
        "source_pdf",
        "title",
    }
)
_ELEMENT_REQUIRED = frozenset(
    {"ir_id", "kind", "raw_kind", "order_index", "source_item_index"}
)
_ELEMENT_KINDS = frozenset(
    {
        "text",
        "heading",
        "table",
        "image",
        "equation",
        "page_furniture",
        "unknown",
    }
)
_PARSER_BACKENDS = frozenset(
    {
        "pipeline",
        "vlm-engine",
        "vlm-http-client",
        "hybrid-engine",
        "hybrid-http-client",
    }
)
_PARSER_METHODS = frozenset({"auto", "txt", "ocr"})


class NormalizedIRVersionError(ValueError):
    """The serialized artifact version is absent, unknown, or inconsistent."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def normalized_ir_filename(version: str = CURRENT_NORMALIZED_IR_VERSION) -> str:
    if version not in READABLE_NORMALIZED_IR_VERSIONS:
        raise NormalizedIRVersionError(
            "unsupported_contract_version",
            f"unsupported NormalizedIR contract version: {version!r}",
        )
    return f"{version}.json"


def normalized_ir_schema_filename(version: str) -> str:
    return normalized_ir_filename(version)


def validate_normalized_ir_path_version(
    path: str | PurePath, *, version: str
) -> None:
    expected = normalized_ir_filename(version)
    actual = PurePath(path).name
    if actual != expected:
        raise NormalizedIRVersionError(
            "contract_filename_mismatch",
            f"NormalizedIR {version} must be stored as {expected}, got {actual}",
        )


def read_normalized_ir_version(payload: Mapping[str, Any]) -> str:
    version = payload.get("contract_version")
    if not isinstance(version, str) or not version:
        raise NormalizedIRVersionError(
            "contract_version_missing",
            "NormalizedIR contract_version must be non-empty text",
        )
    if version not in READABLE_NORMALIZED_IR_VERSIONS:
        match = _NORMALIZED_IR_VERSION_RE.fullmatch(version)
        if match is not None and int(match.group("generation")) < (
            _OLDEST_READABLE_GENERATION
        ):
            raise NormalizedIRVersionError(
                "contract_version_too_old",
                f"NormalizedIR contract version requires re-parse: {version!r}",
            )
        raise NormalizedIRVersionError(
            "unsupported_contract_version",
            f"unsupported NormalizedIR contract version: {version!r}",
        )
    return version


def require_current_normalized_ir(payload: Mapping[str, Any]) -> None:
    version = read_normalized_ir_version(payload)
    if version != CURRENT_NORMALIZED_IR_VERSION:
        raise NormalizedIRVersionError(
            "current_contract_required",
            "new parser artifacts must use " + CURRENT_NORMALIZED_IR_VERSION,
        )


def validate_normalized_ir_contract(
    payload: Mapping[str, Any], *, require_current: bool = False
) -> str:
    """Validate the production-critical, versioned IR envelope.

    JSON Schema remains the exported exhaustive contract.  Runtime ingress
    repeats the invariants that protect publication so a renamed or corrupted
    derived artifact cannot bypass validation merely by carrying a supported
    ``contract_version`` string.
    """

    version = read_normalized_ir_version(payload)
    if require_current and version != CURRENT_NORMALIZED_IR_VERSION:
        raise NormalizedIRVersionError(
            "current_contract_required",
            "new parser artifacts must use " + CURRENT_NORMALIZED_IR_VERSION,
        )
    missing = sorted(_ROOT_REQUIRED - payload.keys())
    if missing:
        raise NormalizedIRVersionError(
            "required_root_field_missing",
            "NormalizedIR is missing required fields: " + ", ".join(missing),
        )
    created_at = _require_text(payload, "created_at")
    try:
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NormalizedIRVersionError(
            "created_at_invalid", "NormalizedIR created_at must be RFC3339 date-time"
        ) from exc
    if parsed_created_at.tzinfo is None:
        raise NormalizedIRVersionError(
            "created_at_invalid", "NormalizedIR created_at must include a timezone"
        )
    _require_text(payload, "document_id")
    _require_text(payload, "source_pdf")
    title = payload.get("title")
    if title is not None and not isinstance(title, str):
        raise NormalizedIRVersionError(
            "title_invalid", "NormalizedIR title must be text or null"
        )
    _validate_parser(payload.get("parser"))
    _validate_parser_artifacts(payload.get("parser_artifacts"))
    _validate_parsed_pages(payload.get("parsed_pages"))

    diagnostics = payload.get("parser_diagnostics")
    if diagnostics is not None and not isinstance(diagnostics, Mapping):
        raise NormalizedIRVersionError(
            "parser_diagnostics_invalid",
            "NormalizedIR parser_diagnostics must be an object",
        )
    if version == CURRENT_NORMALIZED_IR_VERSION:
        if "native_text" in payload:
            raise NormalizedIRVersionError(
                "v3_native_text_forbidden",
                "normalized_ir.v3 cannot carry the retired native_text shadow",
            )
        if isinstance(diagnostics, Mapping) and "native_text_shadow" in diagnostics:
            raise NormalizedIRVersionError(
                "v3_native_text_diagnostic_forbidden",
                "normalized_ir.v3 cannot carry native_text_shadow diagnostics",
            )

    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise NormalizedIRVersionError(
            "elements_invalid", "NormalizedIR elements must be an array"
        )
    seen_ir_ids: set[str] = set()
    seen_source_indices: set[int] = set()
    seen_order_indices: set[int] = set()
    previous_order: int | None = None
    for position, element in enumerate(elements):
        if not isinstance(element, Mapping):
            raise NormalizedIRVersionError(
                "element_invalid", f"NormalizedIR element {position} must be an object"
            )
        missing_element = sorted(_ELEMENT_REQUIRED - element.keys())
        if missing_element:
            raise NormalizedIRVersionError(
                "element_required_field_missing",
                f"NormalizedIR element {position} is missing: "
                + ", ".join(missing_element),
            )
        ir_id = element.get("ir_id")
        if not isinstance(ir_id, str) or not ir_id or ir_id in seen_ir_ids:
            raise NormalizedIRVersionError(
                "element_ir_id_invalid",
                f"NormalizedIR element {position} ir_id must be unique text",
            )
        seen_ir_ids.add(ir_id)
        kind = element.get("kind")
        raw_kind = element.get("raw_kind")
        if kind not in _ELEMENT_KINDS or not isinstance(raw_kind, str) or not raw_kind:
            raise NormalizedIRVersionError(
                "element_kind_invalid",
                f"NormalizedIR element {position} has an invalid kind/raw_kind",
            )
        order_index = _require_unique_integer(
            element,
            "order_index",
            position=position,
            seen=seen_order_indices,
        )
        _require_unique_integer(
            element,
            "source_item_index",
            position=position,
            seen=seen_source_indices,
        )
        if previous_order is not None and order_index <= previous_order:
            raise NormalizedIRVersionError(
                "element_order_invalid",
                "NormalizedIR element order_index values must be strictly increasing",
            )
        previous_order = order_index
        _validate_element_optional_fields(element, position=position)
    return version


def validate_current_normalized_ir_for_write(payload: Mapping[str, Any]) -> str:
    """Validate a new producer artifact at the parser-port boundary."""

    version = validate_normalized_ir_contract(payload, require_current=True)
    try:
        assessment = assess_normalized_ir_table_reconciliation(payload)
    except TableReconciliationContractError as exc:
        raise NormalizedIRVersionError(
            f"table_reconciliation_{exc.reason_code}",
            f"invalid table reconciliation payload: {exc}",
        ) from exc
    validate_reconciliation_generation(
        version=version,
        algorithm_version=assessment.algorithm_version,
    )
    return version


def validate_normalized_ir_identity(
    payload: Mapping[str, Any],
    *,
    document_id: str,
    source_pdf: str | None = None,
) -> None:
    if payload.get("document_id") != document_id:
        raise NormalizedIRVersionError(
            "document_id_mismatch",
            "NormalizedIR document_id differs from the processing run",
        )
    if source_pdf is not None and payload.get("source_pdf") != source_pdf:
        raise NormalizedIRVersionError(
            "source_pdf_mismatch",
            "NormalizedIR source_pdf differs from the registered raw artifact",
        )


def _require_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise NormalizedIRVersionError(
            f"{field}_invalid", f"NormalizedIR {field} must be non-empty text"
        )
    return value


def _validate_parser(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise NormalizedIRVersionError(
            "parser_invalid", "NormalizedIR parser must be an object"
        )
    for field in ("name", "package_version", "language"):
        _require_text(value, field)
    if value.get("backend") not in _PARSER_BACKENDS:
        raise NormalizedIRVersionError(
            "parser_backend_invalid", "NormalizedIR parser backend is unsupported"
        )
    if value.get("method") not in _PARSER_METHODS:
        raise NormalizedIRVersionError(
            "parser_method_invalid", "NormalizedIR parser method is unsupported"
        )
    for field in ("formula", "table"):
        if not isinstance(value.get(field), bool):
            raise NormalizedIRVersionError(
                f"parser_{field}_invalid",
                f"NormalizedIR parser {field} must be boolean",
            )


def _validate_parser_artifacts(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise NormalizedIRVersionError(
            "parser_artifacts_invalid",
            "NormalizedIR parser_artifacts must be an object",
        )
    for field in ("artifact_root_relpath", "content_list_relpath"):
        if field not in value:
            raise NormalizedIRVersionError(
                "parser_artifact_required_field_missing",
                f"NormalizedIR parser_artifacts is missing {field}",
            )
    for field, path in value.items():
        if not isinstance(field, str) or not isinstance(path, str) or not path:
            raise NormalizedIRVersionError(
                "parser_artifact_path_invalid",
                "NormalizedIR parser artifact paths must be non-empty text",
            )
        pure = PurePath(path)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or path.startswith("file:")
            or re.match(r"^[A-Za-z]:[\\/]", path)
        ):
            raise NormalizedIRVersionError(
                "parser_artifact_path_invalid",
                "NormalizedIR parser artifact paths must be relative",
            )


def _validate_parsed_pages(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise NormalizedIRVersionError(
            "parsed_pages_invalid", "NormalizedIR parsed_pages must be an object"
        )
    required = {"start_page_no", "end_page_no", "full_pdf"}
    if set(value) != required:
        raise NormalizedIRVersionError(
            "parsed_pages_shape_invalid",
            "NormalizedIR parsed_pages must contain only start_page_no, "
            "end_page_no, and full_pdf",
        )
    for field in ("start_page_no", "end_page_no"):
        page = value.get(field)
        if page is not None and (
            isinstance(page, bool) or not isinstance(page, int) or page < 1
        ):
            raise NormalizedIRVersionError(
                f"parsed_pages_{field}_invalid",
                f"NormalizedIR parsed_pages {field} must be a positive integer/null",
            )
    if not isinstance(value.get("full_pdf"), bool):
        raise NormalizedIRVersionError(
            "parsed_pages_full_pdf_invalid",
            "NormalizedIR parsed_pages full_pdf must be boolean",
        )
    start_page_no = value.get("start_page_no")
    end_page_no = value.get("end_page_no")
    if (
        isinstance(start_page_no, int)
        and not isinstance(start_page_no, bool)
        and isinstance(end_page_no, int)
        and not isinstance(end_page_no, bool)
        and start_page_no > end_page_no
    ):
        raise NormalizedIRVersionError(
            "parsed_pages_order_invalid",
            "NormalizedIR parsed_pages start_page_no must not exceed end_page_no",
        )


def _validate_element_optional_fields(
    element: Mapping[str, Any], *, position: int
) -> None:
    for field in ("text", "table_html", "image_path", "visual_subtype"):
        if field in element and not isinstance(element[field], str):
            raise NormalizedIRVersionError(
                f"element_{field}_invalid",
                f"NormalizedIR element {position} {field} must be text",
            )
    for field, minimum in (("page_idx", 0), ("page_no", 1)):
        value = element.get(field)
        if field in element and (
            isinstance(value, bool) or not isinstance(value, int) or value < minimum
        ):
            raise NormalizedIRVersionError(
                f"element_{field}_invalid",
                f"NormalizedIR element {position} {field} is invalid",
            )
    heading_level = element.get("heading_level")
    if "heading_level" in element and heading_level is not None and (
        isinstance(heading_level, bool) or not isinstance(heading_level, int)
    ):
        raise NormalizedIRVersionError(
            "element_heading_level_invalid",
            f"NormalizedIR element {position} heading_level is invalid",
        )
    if "bbox" in element:
        bbox = element["bbox"]
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in bbox
            )
            or float(bbox[0]) >= float(bbox[2])
            or float(bbox[1]) >= float(bbox[3])
            or min(float(value) for value in bbox) < 0
            or max(float(value) for value in bbox) > 1000
        ):
            raise NormalizedIRVersionError(
                "element_bbox_invalid",
                f"NormalizedIR element {position} bbox is invalid",
            )
    for field in (
        "table_caption",
        "table_footnote",
        "image_caption",
        "image_footnote",
    ):
        if field in element and (
            not isinstance(element[field], list)
            or not all(isinstance(item, str) for item in element[field])
        ):
            raise NormalizedIRVersionError(
                f"element_{field}_invalid",
                f"NormalizedIR element {position} {field} must be a text array",
            )
    if "table_parse_failed" in element and not isinstance(
        element["table_parse_failed"], bool
    ):
        raise NormalizedIRVersionError(
            "element_table_parse_failed_invalid",
            f"NormalizedIR element {position} table_parse_failed must be boolean",
        )
    if "table" in element:
        _validate_table_grid(element["table"], position=position)


def _validate_table_grid(value: Any, *, position: int) -> None:
    if not isinstance(value, Mapping) or set(value) - {
        "headers",
        "rows",
        "merged_cells",
    }:
        raise NormalizedIRVersionError(
            "element_table_invalid",
            f"NormalizedIR element {position} table grid is invalid",
        )
    headers = value.get("headers")
    rows = value.get("rows")
    if not isinstance(headers, list) or not all(
        isinstance(item, str) for item in headers
    ):
        raise NormalizedIRVersionError(
            "element_table_headers_invalid",
            f"NormalizedIR element {position} table headers must be text array",
        )
    if not isinstance(rows, list) or not all(
        isinstance(row, list) and all(isinstance(item, str) for item in row)
        for row in rows
    ):
        raise NormalizedIRVersionError(
            "element_table_rows_invalid",
            f"NormalizedIR element {position} table rows must be text arrays",
        )


def _require_unique_integer(
    payload: Mapping[str, Any],
    field: str,
    *,
    position: int,
    seen: set[int],
) -> int:
    value = payload.get(field)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value in seen
    ):
        raise NormalizedIRVersionError(
            f"element_{field}_invalid",
            f"NormalizedIR element {position} {field} must be a unique integer",
        )
    seen.add(value)
    return value


def validate_reconciliation_generation(
    *, version: str, algorithm_version: str | None
) -> None:
    """Bind table-reconciliation generations to an IR contract generation."""

    if version == "normalized_ir.v2" and algorithm_version == (
        "mineru-aggregate-table-locator.v4"
    ):
        raise NormalizedIRVersionError(
            "v2_locator_v4_forbidden",
            "locator-only table reconciliation requires normalized_ir.v3",
        )
    if version == CURRENT_NORMALIZED_IR_VERSION and algorithm_version == (
        "mineru-aggregate-table-restore.v3"
    ):
        raise NormalizedIRVersionError(
            "v3_restore_v3_forbidden",
            "normalized_ir.v3 cannot carry legacy physical table restoration",
        )
