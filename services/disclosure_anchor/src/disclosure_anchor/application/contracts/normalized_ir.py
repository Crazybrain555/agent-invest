"""Version authority for serialized NormalizedIR artifacts."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import math
from pathlib import PurePath, PurePosixPath
import re
from typing import Any, Mapping, cast

from disclosure_anchor.application.contracts.document_structure import (
    DOCUMENT_STRUCTURE_ALGORITHM,
    DocumentStructureContractError,
    validate_document_structure,
)
from disclosure_anchor.application.contracts.html_visible_text import (
    html_visible_text,
)
from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    CURRENT_TABLE_RECONCILIATION_ALGORITHM,
    ReconciliationCompatibility,
    TableReconciliationContractError,
    assess_normalized_ir_table_reconciliation,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
    ParserTargetIdentityError,
)
from disclosure_anchor.application.contracts.visual_semantics import (
    VISUAL_SEMANTICS_ARTIFACT_ROLE,
)


CURRENT_NORMALIZED_IR_VERSION = "normalized_ir.v4"
LEGACY_READABLE_NORMALIZED_IR_VERSIONS = frozenset(
    {"normalized_ir.v2", "normalized_ir.v3"}
)
READABLE_NORMALIZED_IR_VERSIONS = frozenset(
    {CURRENT_NORMALIZED_IR_VERSION, *LEGACY_READABLE_NORMALIZED_IR_VERSIONS}
)
_NO_NATIVE_TEXT_VERSIONS = frozenset(
    {"normalized_ir.v3", CURRENT_NORMALIZED_IR_VERSION}
)
_NORMALIZED_IR_VERSION_RE = re.compile(r"^normalized_ir\.v(?P<generation>[1-9][0-9]*)$")
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_ARTIFACT_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_CURRENT_REQUIRED_ARTIFACT_ROLES = frozenset(
    {
        "content_list",
        "content_list_v2",
        "middle",
        "model",
        "pdf_structure",
        "source_evidence",
        VISUAL_SEMANTICS_ARTIFACT_ROLE,
    }
)
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
_CURRENT_ROOT_REQUIRED = frozenset(
    {
        "parser_diagnostics",
        "source_pdf_page_count",
        "source_pdf_sha256",
        "structure_proof",
    }
)
_CURRENT_ROOT_ALLOWED = frozenset(
    {
        *_ROOT_REQUIRED,
        *_CURRENT_ROOT_REQUIRED,
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
# New writers emit one closed, parser-neutral carrier shape for each preserved
# provider role.  This is a source-format contract: it prevents a mapper from
# changing an element's semantic kind or silently attaching fields owned by a
# different carrier.  Read validation remains deliberately looser for frozen
# v2/v3 artifacts.
_CURRENT_BASE_ELEMENT_FIELDS = frozenset(
    {
        "bbox",
        "ir_id",
        "kind",
        "order_index",
        "page_idx",
        "page_no",
        "raw_kind",
        "source_item_index",
        "source_item_sha256",
    }
)
_CURRENT_CARRIER_KINDS = {
    "text": frozenset({"text"}),
    "ref_text": frozenset({"text"}),
    "phonetic": frozenset({"text"}),
    "aside_text": frozenset({"text"}),
    "page_footnote": frozenset({"text"}),
    "header": frozenset({"page_furniture"}),
    "footer": frozenset({"page_furniture"}),
    "page_number": frozenset({"page_furniture"}),
    "table": frozenset({"table"}),
    "image": frozenset({"image"}),
    "chart": frozenset({"image"}),
    "equation": frozenset({"equation"}),
    "code": frozenset({"text"}),
    "list": frozenset({"text"}),
}
_CURRENT_CARRIER_FIELDS = {
    "text": frozenset({"text", "text_level"}),
    "ref_text": frozenset({"text"}),
    "phonetic": frozenset({"text"}),
    "aside_text": frozenset({"text"}),
    "page_footnote": frozenset({"text"}),
    "header": frozenset({"text"}),
    "footer": frozenset({"text"}),
    "page_number": frozenset({"text"}),
    "table": frozenset(
        {
            "image_path",
            "table",
            "table_caption",
            "table_footnote",
            "table_html",
        }
    ),
    "image": frozenset(
        {
            "image_caption",
            "image_footnote",
            "image_path",
            "text",
            "text_provenance",
            "visual_subtype",
            "visual_semantic_text",
        }
    ),
    "chart": frozenset(
        {
            "image_caption",
            "image_footnote",
            "image_path",
            "text",
            "text_provenance",
            "visual_subtype",
            "visual_semantic_text",
        }
    ),
    "equation": frozenset(
        {
            "image_path",
            "text",
            "text_format",
            "visual_semantic_text",
        }
    ),
    "code": frozenset(
        {
            "code_body",
            "code_caption",
            "code_footnote",
            "code_subtype",
            "text",
        }
    ),
    "list": frozenset({"list_items", "list_subtype", "text"}),
}
_CURRENT_CARRIER_REQUIRED_FIELDS = {
    "text": frozenset(),
    "ref_text": frozenset(),
    "phonetic": frozenset(),
    "aside_text": frozenset(),
    "page_footnote": frozenset(),
    "header": frozenset(),
    "footer": frozenset(),
    "page_number": frozenset(),
    "table": frozenset(
        {"image_path", "table", "table_caption", "table_footnote", "table_html"}
    ),
    "image": frozenset({"image_caption", "image_footnote"}),
    "chart": frozenset({"image_caption", "image_footnote"}),
    "equation": frozenset(),
    "code": frozenset({"text", "code_body", "code_caption", "code_footnote"}),
    "list": frozenset({"list_items"}),
}
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


def table_has_visible_text_evidence(
    table: Mapping[str, Any] | None,
    *,
    captions: Any = (),
    notes: Any = (),
    html: Any = "",
    unit: Any = None,
) -> bool:
    """Return whether a table carrier has reader-visible textual evidence."""

    grid = table or {}
    values = [
        *(str(value) for value in grid.get("headers") or []),
        *(
            str(value)
            for row in grid.get("rows") or []
            if isinstance(row, list)
            for value in row
        ),
        *(str(value) for value in captions if isinstance(captions, (list, tuple))),
        *(str(value) for value in notes if isinstance(notes, (list, tuple))),
        str(unit or ""),
    ]
    return any(value.strip() for value in values) or bool(
        html_visible_text(str(html or ""))
    )


def is_visual_only_table_element(element: Mapping[str, Any]) -> bool:
    """Return whether a table carrier's only surviving evidence is its image."""

    if (
        element.get("kind") != "table"
        or not str(element.get("image_path") or "").strip()
    ):
        return False
    raw_table = element.get("table")
    table = raw_table if isinstance(raw_table, Mapping) else {}
    return not table_has_visible_text_evidence(
        table,
        captions=element.get("table_caption") or [],
        notes=element.get("table_footnote") or [],
        html=element.get("table_html") or "",
    )


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


def validate_normalized_ir_path_version(path: str | PurePath, *, version: str) -> None:
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
    required_root = (
        _ROOT_REQUIRED | _CURRENT_ROOT_REQUIRED
        if version == CURRENT_NORMALIZED_IR_VERSION
        else _ROOT_REQUIRED
    )
    missing = sorted(required_root - payload.keys())
    if missing:
        raise NormalizedIRVersionError(
            "required_root_field_missing",
            "NormalizedIR is missing required fields: " + ", ".join(missing),
        )
    if version == CURRENT_NORMALIZED_IR_VERSION:
        unexpected_root = sorted(set(payload) - _CURRENT_ROOT_ALLOWED)
        if unexpected_root:
            raise NormalizedIRVersionError(
                "root_fields_invalid",
                "normalized_ir.v4 carries unsupported root fields: "
                + ", ".join(unexpected_root),
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
    parser_target = _validate_parser(
        payload.get("parser"),
        current=version == CURRENT_NORMALIZED_IR_VERSION,
    )
    _validate_parser_artifacts(
        payload.get("parser_artifacts"),
        version=version,
    )
    parsed_pages = _validate_parsed_pages(payload.get("parsed_pages"))
    if parser_target is not None:
        if parsed_pages["full_pdf"] != parser_target.full_pdf:
            raise NormalizedIRVersionError(
                "parser_page_range_mismatch",
                "NormalizedIR parser target differs from parsed_pages",
            )
        for target_field, parsed_field in (
            ("start_page", "start_page_no"),
            ("end_page", "end_page_no"),
        ):
            target_page = getattr(parser_target, target_field)
            if (
                target_page is not None
                and parsed_pages[parsed_field] != target_page + 1
            ):
                raise NormalizedIRVersionError(
                    "parser_page_range_mismatch",
                    "NormalizedIR parser target differs from parsed_pages",
                )

    diagnostics = payload.get("parser_diagnostics")
    if diagnostics is not None and not isinstance(diagnostics, Mapping):
        raise NormalizedIRVersionError(
            "parser_diagnostics_invalid",
            "NormalizedIR parser_diagnostics must be an object",
        )
    if version in _NO_NATIVE_TEXT_VERSIONS:
        if "native_text" in payload:
            raise NormalizedIRVersionError(
                "v3_native_text_forbidden",
                f"{version} cannot carry the retired native_text shadow",
            )
        if isinstance(diagnostics, Mapping) and "native_text_shadow" in diagnostics:
            raise NormalizedIRVersionError(
                "v3_native_text_diagnostic_forbidden",
                f"{version} cannot carry native_text_shadow diagnostics",
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
        _validate_element_optional_fields(
            element,
            position=position,
            current=version == CURRENT_NORMALIZED_IR_VERSION,
        )
    if version == CURRENT_NORMALIZED_IR_VERSION:
        source_hash = payload.get("source_pdf_sha256")
        page_count = payload.get("source_pdf_page_count")
        if (
            not isinstance(source_hash, str)
            or _SHA256_RE.fullmatch(source_hash) is None
            or isinstance(page_count, bool)
            or not isinstance(page_count, int)
            or page_count < 1
        ):
            raise NormalizedIRVersionError(
                "source_pdf_identity_invalid",
                "NormalizedIR requires source PDF hash and page count",
            )
        assert isinstance(page_count, int)
        for position, element in enumerate(elements):
            page_idx = element.get("page_idx")
            if (
                isinstance(page_idx, int)
                and not isinstance(page_idx, bool)
                and page_idx >= page_count
            ):
                raise NormalizedIRVersionError(
                    "element_page_out_of_range",
                    f"NormalizedIR element {position} exceeds the source PDF",
                )
        parser_artifacts = cast(Mapping[str, Any], payload["parser_artifacts"])
        files = cast(Mapping[str, Any], parser_artifacts["files"])
        present_roles = {
            str(role)
            for role, descriptor in files.items()
            if isinstance(descriptor, Mapping)
            and descriptor.get("availability") == "present"
        }
        missing_artifact_roles = sorted(
            _CURRENT_REQUIRED_ARTIFACT_ROLES - present_roles
        )
        if missing_artifact_roles:
            raise NormalizedIRVersionError(
                "parser_artifact_role_missing",
                "normalized_ir.v4 requires present parser artifacts: "
                + ", ".join(missing_artifact_roles),
            )
        _validate_element_artifact_bindings(
            payload,
            elements=cast(list[Mapping[str, Any]], elements),
            files=files,
        )
        try:
            validate_document_structure(
                payload.get("structure_proof"),
                elements=cast(list[Mapping[str, Any]], elements),
                expected_source_pdf_sha256=source_hash,
                available_artifact_roles=present_roles,
            )
        except DocumentStructureContractError as exc:
            raise NormalizedIRVersionError(
                exc.reason_code,
                f"invalid document structure proof: {exc}",
            ) from exc
    return version


def _validate_element_artifact_bindings(
    payload: Mapping[str, Any],
    *,
    elements: list[Mapping[str, Any]],
    files: Mapping[str, Any],
) -> None:
    parser_artifacts = cast(Mapping[str, Any], payload["parser_artifacts"])
    root = _relative_artifact_path(
        parser_artifacts.get("artifact_root_relpath"),
        field="artifact_root_relpath",
    )
    expected: dict[str, str] = {}
    for element in elements:
        source_item_index = element.get("source_item_index")
        if isinstance(source_item_index, bool) or not isinstance(
            source_item_index, int
        ):
            continue
        image_path = element.get("image_path")
        if isinstance(image_path, str) and image_path:
            expected[f"evidence_image_{source_item_index:06d}"] = image_path
        table = element.get("table")
        media_values = (
            table.get("embedded_media") if isinstance(table, Mapping) else None
        )
        if not isinstance(media_values, list):
            continue
        for media in media_values:
            assert isinstance(media, Mapping)
            role = cast(str, media["artifact_role"])
            expected[role] = cast(str, media["image_path"])
    for role, image_path in expected.items():
        descriptor = files.get(role)
        expected_relpath = str(root / _relative_artifact_path(
            image_path,
            field=f"elements.{role}.image_path",
        ))
        if (
            not isinstance(descriptor, Mapping)
            or descriptor.get("availability") != "present"
            or descriptor.get("relpath") != expected_relpath
        ):
            raise NormalizedIRVersionError(
                "element_image_artifact_binding_invalid",
                f"NormalizedIR image occurrence is not manifest-bound: {role}",
            )


def validate_current_normalized_ir_for_write(payload: Mapping[str, Any]) -> str:
    """Validate a new producer artifact at the parser-port boundary."""

    version = validate_normalized_ir_contract(payload, require_current=True)
    structure_proof = payload.get("structure_proof")
    if (
        isinstance(structure_proof, Mapping)
        and structure_proof.get("algorithm_version")
        != DOCUMENT_STRUCTURE_ALGORITHM
    ):
        # Historical algorithms stay readable for diagnostics, but a NEW
        # artifact must never persist a legacy structure authority.
        raise NormalizedIRVersionError(
            "structure_proof_current_required",
            "new NormalizedIR writes require the current structure algorithm",
        )
    elements = payload.get("elements")
    assert isinstance(elements, list)
    for position, element in enumerate(elements):
        assert isinstance(element, Mapping)
        _validate_current_write_element(
            element,
            position=position,
        )
    try:
        assessment = assess_normalized_ir_table_reconciliation(payload)
    except TableReconciliationContractError as exc:
        raise NormalizedIRVersionError(
            f"table_reconciliation_{exc.reason_code}",
            f"invalid table reconciliation payload: {exc}",
        ) from exc
    if assessment.compatibility is not ReconciliationCompatibility.CURRENT:
        raise NormalizedIRVersionError(
            "table_reconciliation_current_required",
            "new NormalizedIR writes require current page-local table closure",
        )
    validate_reconciliation_generation(
        version=version,
        algorithm_version=assessment.algorithm_version,
    )
    _validate_reconciliation_artifact_binding(payload)
    _validate_visual_semantic_artifact_binding(payload)
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


def _validate_parser(
    value: Any, *, current: bool
) -> ParserTargetIdentity | None:
    if not isinstance(value, Mapping):
        raise NormalizedIRVersionError(
            "parser_invalid", "NormalizedIR parser must be an object"
        )
    if current:
        try:
            return ParserTargetIdentity.from_payload(value)
        except ParserTargetIdentityError as exc:
            raise NormalizedIRVersionError(
                "parser_target_identity_invalid",
                f"NormalizedIR parser target is invalid: {exc}",
            ) from exc
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
    return None


def _validate_parser_artifacts(value: Any, *, version: str) -> None:
    if not isinstance(value, Mapping):
        raise NormalizedIRVersionError(
            "parser_artifacts_invalid",
            "NormalizedIR parser_artifacts must be an object",
        )
    if version == CURRENT_NORMALIZED_IR_VERSION:
        _validate_hashed_parser_artifacts(value)
        return
    _validate_legacy_parser_artifacts(value)


def _validate_legacy_parser_artifacts(value: Mapping[str, Any]) -> None:
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


def _validate_hashed_parser_artifacts(value: Mapping[str, Any]) -> None:
    if set(value) != {"artifact_root_relpath", "files"}:
        raise NormalizedIRVersionError(
            "parser_artifacts_shape_invalid",
            "normalized_ir.v4 parser_artifacts must contain only "
            "artifact_root_relpath and files",
        )
    root = _relative_artifact_path(
        value.get("artifact_root_relpath"),
        field="artifact_root_relpath",
    )
    files = value.get("files")
    if not isinstance(files, Mapping) or not files:
        raise NormalizedIRVersionError(
            "parser_artifact_files_invalid",
            "normalized_ir.v4 parser_artifacts files must be a non-empty object",
        )
    for role, descriptor in files.items():
        if not isinstance(role, str) or _ARTIFACT_ROLE_RE.fullmatch(role) is None:
            raise NormalizedIRVersionError(
                "parser_artifact_role_invalid",
                f"normalized_ir.v4 parser artifact role is unsafe: {role!r}",
            )
        if not isinstance(descriptor, Mapping):
            raise NormalizedIRVersionError(
                "parser_artifact_descriptor_invalid",
                f"normalized_ir.v4 parser artifact {role!r} must be an object",
            )
        availability = descriptor.get("availability")
        if availability == "not_emitted":
            if set(descriptor) != {"availability"}:
                raise NormalizedIRVersionError(
                    "parser_artifact_descriptor_invalid",
                    f"not_emitted parser artifact {role!r} cannot carry descriptors",
                )
            continue
        if availability != "present" or set(descriptor) != {
            "availability",
            "relpath",
            "sha256",
            "size_bytes",
        }:
            raise NormalizedIRVersionError(
                "parser_artifact_descriptor_invalid",
                f"present parser artifact {role!r} requires relpath/sha256/size_bytes",
            )
        relpath = _relative_artifact_path(
            descriptor.get("relpath"),
            field=f"files.{role}.relpath",
        )
        try:
            relative = relpath.relative_to(root)
        except ValueError as exc:
            raise NormalizedIRVersionError(
                "parser_artifact_root_escape",
                f"parser artifact {role!r} is outside artifact_root_relpath",
            ) from exc
        if not relative.parts:
            raise NormalizedIRVersionError(
                "parser_artifact_root_escape",
                f"parser artifact {role!r} must be below artifact_root_relpath",
            )
        sha256 = descriptor.get("sha256")
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            raise NormalizedIRVersionError(
                "parser_artifact_hash_invalid",
                f"parser artifact {role!r} sha256 is invalid",
            )
        size_bytes = descriptor.get("size_bytes")
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
        ):
            raise NormalizedIRVersionError(
                "parser_artifact_size_invalid",
                f"parser artifact {role!r} size_bytes is invalid",
            )


def _relative_artifact_path(value: Any, *, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise NormalizedIRVersionError(
            "parser_artifact_path_invalid",
            f"NormalizedIR parser artifact {field} must be a POSIX relative path",
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or str(path) != value
        or value.startswith("file:")
        or re.match(r"^[A-Za-z]:/", value)
    ):
        raise NormalizedIRVersionError(
            "parser_artifact_path_invalid",
            f"NormalizedIR parser artifact {field} must be a safe relative path",
        )
    return path


def _validate_reconciliation_artifact_binding(
    payload: Mapping[str, Any],
) -> None:
    diagnostics = payload.get("parser_diagnostics")
    if not isinstance(diagnostics, Mapping):
        return
    reconciliation = diagnostics.get("table_reconciliation")
    if not isinstance(reconciliation, Mapping):
        return
    model_hash = reconciliation.get("model_hash")
    parser_artifacts = payload.get("parser_artifacts")
    assert isinstance(parser_artifacts, Mapping)
    files = parser_artifacts.get("files")
    assert isinstance(files, Mapping)
    model = files.get("model")
    if not isinstance(model, Mapping):
        raise NormalizedIRVersionError(
            "table_reconciliation_model_artifact_missing",
            "table reconciliation diagnostics require the model artifact role",
        )
    if model.get("availability") != "present" or model.get("sha256") != model_hash:
        raise NormalizedIRVersionError(
            "table_reconciliation_model_binding_invalid",
            "page-local table closure must bind the exact model artifact hash",
        )


def _validate_parsed_pages(value: Any) -> Mapping[str, Any]:
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
    return value


def _validate_element_optional_fields(
    element: Mapping[str, Any], *, position: int, current: bool
) -> None:
    text_fields = ["text", "table_html", "image_path", "visual_subtype"]
    if current:
        text_fields.extend(["code_body", "code_subtype", "list_subtype", "text_format"])
    for field in text_fields:
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
    if (
        "heading_level" in element
        and heading_level is not None
        and (isinstance(heading_level, bool) or not isinstance(heading_level, int))
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
    text_array_fields = [
        "table_caption",
        "table_footnote",
        "image_caption",
        "image_footnote",
    ]
    if current:
        text_array_fields.extend(["code_caption", "code_footnote", "list_items"])
    for field in text_array_fields:
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
    if current:
        source_item_sha256 = element.get("source_item_sha256")
        if "source_item_sha256" in element and (
            not isinstance(source_item_sha256, str)
            or _SHA256_RE.fullmatch(source_item_sha256) is None
        ):
            raise NormalizedIRVersionError(
                "element_source_item_sha256_invalid",
                f"NormalizedIR element {position} source_item_sha256 is invalid",
            )
    if "table" in element:
        _validate_table_grid(
            element["table"],
            position=position,
            source_item_index=element.get("source_item_index"),
            current=current,
        )


def _validate_current_write_element(
    element: Mapping[str, Any],
    *,
    position: int,
) -> None:
    raw_kind = element.get("raw_kind")
    kind = element.get("kind")
    allowed_kinds = _CURRENT_CARRIER_KINDS.get(str(raw_kind))
    if allowed_kinds is None or kind not in allowed_kinds:
        raise NormalizedIRVersionError(
            "element_carrier_kind_invalid",
            "NormalizedIR write element "
            f"{position} has no supported raw_kind/kind carrier mapping",
        )
    allowed_fields = (
        _CURRENT_BASE_ELEMENT_FIELDS | _CURRENT_CARRIER_FIELDS[str(raw_kind)]
    )
    unexpected_fields = sorted(set(element) - allowed_fields)
    if unexpected_fields:
        raise NormalizedIRVersionError(
            "element_carrier_fields_invalid",
            "NormalizedIR write element "
            f"{position} carries fields outside its source carrier: "
            + ", ".join(unexpected_fields),
        )
    missing_fields = sorted(
        _CURRENT_CARRIER_REQUIRED_FIELDS[str(raw_kind)] - element.keys()
    )
    if missing_fields:
        raise NormalizedIRVersionError(
            "element_carrier_fields_missing",
            "NormalizedIR write element "
            f"{position} is missing required carrier fields: "
            + ", ".join(missing_fields),
        )
    if (
        element.get("source_item_index") != position
        or element.get("order_index") != position
    ):
        raise NormalizedIRVersionError(
            "element_source_identity_required",
            "NormalizedIR write element "
            f"{position} must preserve provider item identity",
        )
    source_item_sha256 = element.get("source_item_sha256")
    if (
        not isinstance(source_item_sha256, str)
        or _SHA256_RE.fullmatch(source_item_sha256) is None
    ):
        raise NormalizedIRVersionError(
            "element_source_item_sha256_required",
            f"NormalizedIR write element {position} requires source_item_sha256",
        )
    page_idx = element.get("page_idx")
    page_no = element.get("page_no")
    bbox = element.get("bbox")
    if (
        isinstance(page_idx, bool)
        or not isinstance(page_idx, int)
        or page_idx < 0
        or isinstance(page_no, bool)
        or not isinstance(page_no, int)
        or page_no != page_idx + 1
        or not isinstance(bbox, list)
        or len(bbox) != 4
    ):
        raise NormalizedIRVersionError(
            "element_source_location_required",
            "NormalizedIR write element "
            f"{position} requires exact page_idx/page_no/bbox source location",
        )
    if raw_kind == "text" and "text_level" in element:
        text_level = element.get("text_level")
        if (
            isinstance(text_level, bool)
            or not isinstance(text_level, int)
            or text_level < 0
        ):
            raise NormalizedIRVersionError(
                "element_text_level_projection_invalid",
                f"NormalizedIR write element {position} has invalid text_level",
            )
    if kind == "equation" and not (
        str(element.get("image_path") or "").strip()
        or str(element.get("text") or "").strip()
    ):
        raise NormalizedIRVersionError(
            "element_equation_evidence_missing",
            f"NormalizedIR element {position} equation has no image or text evidence",
        )
    if kind == "image" and not str(element.get("image_path") or "").strip():
        raise NormalizedIRVersionError(
            "element_visual_artifact_missing",
            f"NormalizedIR element {position} visual has no image artifact",
        )
    _validate_visual_semantic_text(
        element,
        position=position,
        label="element",
        field="visual_semantic_text",
    )
    visual_text_present = bool(str(element.get("text") or "").strip())
    expected_text_provenance = {
        "image": "generated_annotation",
        "chart": "visual_recognition",
    }.get(str(raw_kind))
    if expected_text_provenance is not None and (
        (
            visual_text_present
            and (
                element.get("text_provenance") != expected_text_provenance
                or not str(element.get("image_path") or "").strip()
            )
        )
        or (not visual_text_present and "text_provenance" in element)
    ):
        raise NormalizedIRVersionError(
            "element_image_text_provenance_invalid",
            "NormalizedIR visual element "
            f"{position} must bind and mark its non-native text provenance",
        )
    if raw_kind == "code":
        body = element.get("code_body")
        captions = element.get("code_caption")
        footnotes = element.get("code_footnote")
        if (
            element.get("kind") != "text"
            or not isinstance(body, str)
            or not body.strip()
            or not isinstance(captions, list)
            or not all(isinstance(value, str) for value in captions)
            or not isinstance(footnotes, list)
            or not all(isinstance(value, str) for value in footnotes)
        ):
            raise NormalizedIRVersionError(
                "element_code_contract_invalid",
                f"NormalizedIR element {position} has an invalid typed code carrier",
            )
        expected_text = "\n".join(
            value for value in [*captions, body, *footnotes] if value.strip()
        )
        if element.get("text") != expected_text:
            raise NormalizedIRVersionError(
                "element_code_projection_invalid",
                f"NormalizedIR element {position} code fields do not reproduce text",
            )
    elif raw_kind == "list":
        list_items = element.get("list_items")
        expected_list_text = (
            "\n".join(list_items)
            if isinstance(list_items, list)
            and any(isinstance(value, str) and value.strip() for value in list_items)
            else None
        )
        if (
            element.get("kind") != "text"
            or not isinstance(list_items, list)
            or not all(isinstance(value, str) for value in list_items)
            or (element.get("text") if "text" in element else None)
            != expected_list_text
        ):
            raise NormalizedIRVersionError(
                "element_list_contract_invalid",
                f"NormalizedIR element {position} has an invalid typed list carrier",
            )
    elif raw_kind == "equation":
        text_format = element.get("text_format")
        if text_format is not None and (
            not isinstance(text_format, str) or not text_format
        ):
            raise NormalizedIRVersionError(
                "element_equation_format_invalid",
                f"NormalizedIR element {position} equation format is invalid",
            )


def _validate_table_grid(
    value: Any,
    *,
    position: int,
    source_item_index: object,
    current: bool,
) -> None:
    allowed_fields = (
        {"headers", "rows", "cells", "embedded_media", "merged_cells"}
        if current
        else {"headers", "rows", "merged_cells"}
    )
    if not isinstance(value, Mapping) or set(value) - allowed_fields:
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
    if not current:
        return
    grid = ([headers] if headers else []) + rows
    if (
        not grid
        or not grid[0]
        or any(len(row) != len(grid[0]) for row in grid)
    ):
        raise NormalizedIRVersionError(
            "element_table_grid_shape_invalid",
            f"NormalizedIR element {position} table grid must be rectangular",
        )
    cells = value.get("cells")
    required_cell_fields = {
        "row",
        "col",
        "rowspan",
        "colspan",
        "text",
        "is_header",
    }
    if not isinstance(cells, list) or not cells:
        raise NormalizedIRVersionError(
            "element_table_cells_invalid",
            f"NormalizedIR element {position} table cells must be non-empty",
        )
    occupied: set[tuple[int, int]] = set()
    anchors: dict[tuple[int, int], Mapping[str, Any]] = {}
    for cell in cells:
        if (
            not isinstance(cell, Mapping)
            or set(cell) != required_cell_fields
            or any(
                isinstance(cell[field], bool)
                or not isinstance(cell[field], int)
                or cell[field] < minimum
                for field, minimum in (
                    ("row", 0),
                    ("col", 0),
                    ("rowspan", 1),
                    ("colspan", 1),
                )
            )
            or not isinstance(cell["text"], str)
            or not isinstance(cell["is_header"], bool)
        ):
            raise NormalizedIRVersionError(
                "element_table_cells_invalid",
                f"NormalizedIR element {position} table cell is invalid",
            )
        row = cast(int, cell["row"])
        col = cast(int, cell["col"])
        rowspan = cast(int, cell["rowspan"])
        colspan = cast(int, cell["colspan"])
        if (
            row + rowspan > len(grid)
            or col + colspan > len(grid[0])
            or (row, col) in anchors
        ):
            raise NormalizedIRVersionError(
                "element_table_cells_invalid",
                f"NormalizedIR element {position} table cell exceeds its grid",
            )
        targets = {
            (row + row_offset, col + col_offset)
            for row_offset in range(rowspan)
            for col_offset in range(colspan)
        }
        if targets & occupied or any(
            grid[target_row][target_col] != cell["text"]
            for target_row, target_col in targets
        ):
            raise NormalizedIRVersionError(
                "element_table_cell_projection_invalid",
                f"NormalizedIR element {position} table cells do not reproduce its grid",
            )
        occupied.update(targets)
        anchors[(row, col)] = cell
    for row_index, grid_row in enumerate(grid):
        for col_index, item in enumerate(grid_row):
            if item and (row_index, col_index) not in occupied:
                raise NormalizedIRVersionError(
                    "element_table_cell_projection_invalid",
                    f"NormalizedIR element {position} non-empty grid cell is unowned",
                )
    if headers and (
        any(
            not cast(bool, cell["is_header"])
            for cell in cells
            if cell["row"] == 0
        )
        or any(
            cast(bool, cell["is_header"])
            for cell in cells
            if cell["row"] != 0
        )
    ):
        raise NormalizedIRVersionError(
            "element_table_header_projection_invalid",
            f"NormalizedIR element {position} headers misstate source cell roles",
        )

    embedded_media = value.get("embedded_media")
    required_media_fields = {
        "occurrence_index",
        "cell_media_index",
        "row",
        "col",
        "rowspan",
        "colspan",
        "image_path",
        "artifact_role",
    }
    optional_media_fields = {
        "alt_text",
        "title_text",
        "semantic_text",
    }
    if not isinstance(embedded_media, list):
        raise NormalizedIRVersionError(
            "element_table_embedded_media_invalid",
            f"NormalizedIR element {position} embedded_media must be an array",
        )
    media_by_cell: Counter[tuple[int, int]] = Counter()
    for occurrence_index, media in enumerate(embedded_media):
        if (
            not isinstance(media, Mapping)
            or set(media) - required_media_fields - optional_media_fields
            or not required_media_fields <= set(media)
            or any(
                isinstance(media[field], bool)
                or not isinstance(media[field], int)
                or media[field] < minimum
                for field, minimum in (
                    ("occurrence_index", 0),
                    ("cell_media_index", 0),
                    ("row", 0),
                    ("col", 0),
                    ("rowspan", 1),
                    ("colspan", 1),
                )
            )
            or media["occurrence_index"] != occurrence_index
            or any(
                field in media and not isinstance(media[field], str)
                for field in optional_media_fields
            )
        ):
            raise NormalizedIRVersionError(
                "element_table_embedded_media_invalid",
                f"NormalizedIR element {position} embedded media is invalid",
            )
        row = cast(int, media["row"])
        col = cast(int, media["col"])
        cell = anchors.get((row, col))
        if (
            cell is None
            or media["rowspan"] != cell["rowspan"]
            or media["colspan"] != cell["colspan"]
            or media["cell_media_index"] != media_by_cell[(row, col)]
        ):
            raise NormalizedIRVersionError(
                "element_table_embedded_media_cell_invalid",
                f"NormalizedIR element {position} embedded media cell is invalid",
            )
        media_by_cell[(row, col)] += 1
        _validate_visual_semantic_text(
            media,
            position=position,
            label=f"embedded media {occurrence_index}",
            field="semantic_text",
        )
        _relative_artifact_path(
            media.get("image_path"),
            field=f"elements[{position}].table.embedded_media.image_path",
        )
        if (
            isinstance(source_item_index, bool)
            or not isinstance(source_item_index, int)
            or source_item_index < 0
            or media.get("artifact_role")
            != (
                f"evidence_table_media_{source_item_index:06d}_"
                f"{occurrence_index:06d}"
            )
        ):
            raise NormalizedIRVersionError(
                "element_table_embedded_media_role_invalid",
                f"NormalizedIR element {position} embedded media role is invalid",
            )
    merged_cells = value.get("merged_cells")
    if "merged_cells" in value and (
        not isinstance(merged_cells, list)
        or not all(
            isinstance(cell, Mapping)
            and set(cell) == {"row", "col", "rowspan", "colspan"}
            and all(
                not isinstance(cell[field], bool)
                and isinstance(cell[field], int)
                and cell[field] >= minimum
                for field, minimum in (
                    ("row", 0),
                    ("col", 0),
                    ("rowspan", 1),
                    ("colspan", 1),
                )
            )
            for cell in merged_cells
        )
    ):
        raise NormalizedIRVersionError(
            "element_table_merged_cells_invalid",
            f"NormalizedIR element {position} merged_cells are invalid",
        )
    expected_merged = [
        {
            "row": cast(int, cell["row"]),
            "col": cast(int, cell["col"]),
            "rowspan": cast(int, cell["rowspan"]),
            "colspan": cast(int, cell["colspan"]),
        }
        for cell in cells
        if cell["rowspan"] != 1 or cell["colspan"] != 1
    ]
    if (merged_cells or []) != expected_merged:
        raise NormalizedIRVersionError(
            "element_table_merged_cells_projection_invalid",
            f"NormalizedIR element {position} merged_cells differ from logical cells",
        )


def _validate_visual_semantic_text(
    value: Mapping[str, Any],
    *,
    position: int,
    label: str,
    field: str,
) -> None:
    text = value.get(field)
    if text is None:
        return
    if not isinstance(text, str) or not text.strip():
        raise NormalizedIRVersionError(
            "visual_semantic_text_invalid",
            f"NormalizedIR element {position} {label} visual semantics are invalid",
        )


def _validate_visual_semantic_artifact_binding(
    payload: Mapping[str, Any],
) -> None:
    parser_artifacts = payload.get("parser_artifacts")
    diagnostics = payload.get("parser_diagnostics")
    assert isinstance(parser_artifacts, Mapping)
    assert isinstance(diagnostics, Mapping)
    files = parser_artifacts.get("files")
    visual = diagnostics.get("visual_semantics")
    descriptor = (
        files.get(VISUAL_SEMANTICS_ARTIFACT_ROLE)
        if isinstance(files, Mapping)
        else None
    )
    if (
        not isinstance(visual, Mapping)
        or visual.get("artifact_role") != VISUAL_SEMANTICS_ARTIFACT_ROLE
        or not isinstance(descriptor, Mapping)
        or descriptor.get("availability") != "present"
        or descriptor.get("sha256") != visual.get("artifact_sha256")
    ):
        raise NormalizedIRVersionError(
            "visual_artifact_binding_invalid",
            "NormalizedIR visual diagnostics are not bound to the artifact manifest",
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

    if (
        version != CURRENT_NORMALIZED_IR_VERSION
        and algorithm_version == CURRENT_TABLE_RECONCILIATION_ALGORITHM
    ):
        raise NormalizedIRVersionError(
            "table_reconciliation_generation_invalid",
            "page-local table closure requires normalized_ir.v4",
        )
