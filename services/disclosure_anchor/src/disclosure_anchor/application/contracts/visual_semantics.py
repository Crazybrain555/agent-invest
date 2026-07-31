"""Closed, source-bound semantics for visual evidence occurrences.

The artifact records what happened to each physical visual occurrence.  It
does not classify document structure and it cannot promote visual text into a
heading, title, or boundary.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Literal, NoReturn, cast


VISUAL_SEMANTICS_CONTRACT_VERSION = "visual-semantics.v1"
VISUAL_SEMANTICS_ARTIFACT_ROLE = "visual_semantics"
MINERU_VL_UTILS_PACKAGE_VERSION = "1.0.5"

VisualSemanticStatus = Literal["semantic_text", "guard_only", "unresolved"]
VisualOccurrenceKind = Literal[
    "image",
    "chart",
    "equation",
    "table_media",
    "visual_page",
    "carrier_guard",
]
VisualSemanticOrigin = Literal[
    "provider_visual_text",
    "provider_media_metadata",
    "mineru_content_extract",
    "source_carrier",
]

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_ROLE = re.compile(r"^[a-z][a-z0-9_]*$")
_INDIVIDUAL_VISUAL_KINDS = frozenset(
    {"image", "chart", "equation", "table_media", "visual_page"}
)


class VisualSemanticContractError(ValueError):
    """A visual semantic artifact is open, unbound, or contradictory."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TableMediaLocator:
    occurrence_index: int
    cell_media_index: int
    row: int
    col: int
    rowspan: int
    colspan: int

    def __post_init__(self) -> None:
        if (
            any(
                not _index(value)
                for value in (
                    self.occurrence_index,
                    self.cell_media_index,
                    self.row,
                    self.col,
                )
            )
            or not _positive_int(self.rowspan)
            or not _positive_int(self.colspan)
        ):
            _fail(
                "visual_table_media_locator_invalid",
                "table-media cell and occurrence indices are invalid",
            )

    def to_payload(self) -> dict[str, int]:
        return {
            "occurrence_index": self.occurrence_index,
            "cell_media_index": self.cell_media_index,
            "row": self.row,
            "col": self.col,
            "rowspan": self.rowspan,
            "colspan": self.colspan,
        }

    @classmethod
    def from_payload(cls, value: object) -> "TableMediaLocator":
        if not isinstance(value, Mapping) or set(value) != {
            "occurrence_index",
            "cell_media_index",
            "row",
            "col",
            "rowspan",
            "colspan",
        }:
            _fail(
                "visual_table_media_locator_invalid",
                "table-media locator fields are not closed",
            )
        return cls(
            occurrence_index=cast(int, value["occurrence_index"]),
            cell_media_index=cast(int, value["cell_media_index"]),
            row=cast(int, value["row"]),
            col=cast(int, value["col"]),
            rowspan=cast(int, value["rowspan"]),
            colspan=cast(int, value["colspan"]),
        )


@dataclass(frozen=True, slots=True)
class VisualSemanticDisposition:
    occurrence_id: str
    occurrence_kind: VisualOccurrenceKind
    status: VisualSemanticStatus
    source_item_index: int | None
    source_item_sha256: str | None
    page_idx: int
    bbox: tuple[float, float, float, float]
    table_media: TableMediaLocator | None
    artifact_role: str
    artifact_sha256: str
    semantic_text: str | None = None
    semantic_text_sha256: str | None = None
    semantic_origin: VisualSemanticOrigin | None = None

    def __post_init__(self) -> None:
        if (
            not self.occurrence_id
            or self.occurrence_kind
            not in {
                "image",
                "chart",
                "equation",
                "table_media",
                "visual_page",
                "carrier_guard",
            }
            or self.status not in {"semantic_text", "guard_only", "unresolved"}
            or not _index(self.page_idx)
            or not _bbox(self.bbox)
            or _ARTIFACT_ROLE.fullmatch(self.artifact_role) is None
            or not _sha(self.artifact_sha256)
        ):
            _fail(
                "visual_disposition_identity_invalid",
                "visual disposition identity is invalid",
            )
        if self.occurrence_kind == "visual_page":
            if self.source_item_index is not None or self.source_item_sha256 is not None:
                _fail(
                    "visual_source_identity_invalid",
                    "visual-page fallback cannot invent a provider source item",
                )
        elif (
            not _index(self.source_item_index)
            or not _sha(self.source_item_sha256)
        ):
            _fail(
                "visual_source_identity_invalid",
                "visual occurrence requires its exact provider item and hash",
            )
        if (self.occurrence_kind == "table_media") != (
            self.table_media is not None
        ):
            _fail(
                "visual_table_media_locator_invalid",
                "only table media can carry a logical cell locator",
            )
        if self.status == "guard_only":
            if self.occurrence_kind != "carrier_guard":
                _fail(
                    "visual_guard_scope_invalid",
                    "an individual visual occurrence cannot be guard_only",
                )
            if self.semantic_origin != "source_carrier":
                _fail(
                    "visual_guard_carrier_invalid",
                    "guard_only requires a typed searchable source carrier",
                )
        elif self.occurrence_kind == "carrier_guard":
            _fail(
                "visual_guard_scope_invalid",
                "carrier_guard must use the guard_only disposition",
            )
        if self.status in {"semantic_text", "guard_only"}:
            if (
                not isinstance(self.semantic_text, str)
                or not self.semantic_text.strip()
                or self.semantic_text_sha256 != text_sha256(self.semantic_text)
                or self.semantic_origin
                not in {
                    "provider_visual_text",
                    "provider_media_metadata",
                    "mineru_content_extract",
                    "source_carrier",
                }
            ):
                _fail(
                    "visual_semantic_text_invalid",
                    "searchable visual text and its exact digest are required",
                )
        elif any(
            value is not None
            for value in (
                self.semantic_text,
                self.semantic_text_sha256,
                self.semantic_origin,
            )
        ):
            _fail(
                "visual_unresolved_payload_invalid",
                "unresolved visual disposition cannot carry guessed semantics",
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "occurrence_id": self.occurrence_id,
            "occurrence_kind": self.occurrence_kind,
            "status": self.status,
            "source_item_index": self.source_item_index,
            "source_item_sha256": self.source_item_sha256,
            "page_idx": self.page_idx,
            "bbox": list(self.bbox),
            "table_media": (
                self.table_media.to_payload()
                if self.table_media is not None
                else None
            ),
            "artifact_role": self.artifact_role,
            "artifact_sha256": self.artifact_sha256,
            "semantic_text": self.semantic_text,
            "semantic_text_sha256": self.semantic_text_sha256,
            "semantic_origin": self.semantic_origin,
        }

    @classmethod
    def from_payload(cls, value: object) -> "VisualSemanticDisposition":
        expected = {
            "occurrence_id",
            "occurrence_kind",
            "status",
            "source_item_index",
            "source_item_sha256",
            "page_idx",
            "bbox",
            "table_media",
            "artifact_role",
            "artifact_sha256",
            "semantic_text",
            "semantic_text_sha256",
            "semantic_origin",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail(
                "visual_disposition_shape_invalid",
                "visual disposition fields are not closed",
            )
        bbox = value["bbox"]
        if not isinstance(bbox, list) or len(bbox) != 4:
            _fail(
                "visual_disposition_identity_invalid",
                "visual disposition bbox must have four coordinates",
            )
        table_media = value["table_media"]
        return cls(
            occurrence_id=cast(str, value["occurrence_id"]),
            occurrence_kind=cast(VisualOccurrenceKind, value["occurrence_kind"]),
            status=cast(VisualSemanticStatus, value["status"]),
            source_item_index=cast(int | None, value["source_item_index"]),
            source_item_sha256=cast(str | None, value["source_item_sha256"]),
            page_idx=cast(int, value["page_idx"]),
            bbox=cast(
                tuple[float, float, float, float],
                tuple(float(item) for item in bbox),
            ),
            table_media=(
                TableMediaLocator.from_payload(table_media)
                if table_media is not None
                else None
            ),
            artifact_role=cast(str, value["artifact_role"]),
            artifact_sha256=cast(str, value["artifact_sha256"]),
            semantic_text=cast(str | None, value["semantic_text"]),
            semantic_text_sha256=cast(str | None, value["semantic_text_sha256"]),
            semantic_origin=cast(
                VisualSemanticOrigin | None,
                value["semantic_origin"],
            ),
        )


@dataclass(frozen=True, slots=True)
class VisualSemanticClosure:
    source_pdf_sha256: str
    source_pdf_page_count: int
    source_evidence_sha256: str
    content_list_sha256: str
    content_list_v2_sha256: str
    middle_sha256: str
    model_sha256: str
    parser_target_sha256: str
    runtime_bundle_identity_sha256: str
    mineru_package_version: str
    mineru_vl_utils_version: str
    enrichment_backend: Literal["http-client"]
    enrichment_image_analysis: Literal[True]
    server_url_sha256: str
    formula_enabled: Literal[True]
    dispositions: tuple[VisualSemanticDisposition, ...]
    contract_version: str = VISUAL_SEMANTICS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != VISUAL_SEMANTICS_CONTRACT_VERSION:
            _fail(
                "visual_contract_version_unsupported",
                "visual semantic contract version is unsupported",
            )
        if (
            not all(
                _sha(value)
                for value in (
                self.source_pdf_sha256,
                self.source_evidence_sha256,
                self.content_list_sha256,
                self.content_list_v2_sha256,
                self.middle_sha256,
                self.model_sha256,
                self.parser_target_sha256,
                self.runtime_bundle_identity_sha256,
                self.server_url_sha256,
                )
            )
            or not _positive_int(self.source_pdf_page_count)
        ):
            _fail(
                "visual_artifact_attestation_invalid",
                "visual semantic source/runtime digests are invalid",
            )
        if (
            not self.mineru_package_version
            or self.mineru_vl_utils_version != MINERU_VL_UTILS_PACKAGE_VERSION
            or self.enrichment_backend != "http-client"
            or self.enrichment_image_analysis is not True
            or self.formula_enabled is not True
        ):
            _fail(
                "visual_runtime_attestation_invalid",
                "visual semantic extractor runtime is not the deployed contract",
            )
        identities = [item.occurrence_id for item in self.dispositions]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            _fail(
                "visual_occurrence_closure_invalid",
                "visual semantic occurrence identities must be unique and sorted",
            )

    @property
    def unresolved(self) -> tuple[VisualSemanticDisposition, ...]:
        return tuple(
            item for item in self.dispositions if item.status == "unresolved"
        )

    def status_counts(self) -> dict[str, int]:
        counts = Counter(item.status for item in self.dispositions)
        return {
            "semantic_text": counts["semantic_text"],
            "guard_only": counts["guard_only"],
            "unresolved": counts["unresolved"],
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "source_pdf_sha256": self.source_pdf_sha256,
            "source_pdf_page_count": self.source_pdf_page_count,
            "source_evidence_sha256": self.source_evidence_sha256,
            "source_artifacts": {
                "content_list_sha256": self.content_list_sha256,
                "content_list_v2_sha256": self.content_list_v2_sha256,
                "middle_sha256": self.middle_sha256,
                "model_sha256": self.model_sha256,
            },
            "runtime": {
                "parser_target_sha256": self.parser_target_sha256,
                "runtime_bundle_identity_sha256": (
                    self.runtime_bundle_identity_sha256
                ),
                "mineru_package_version": self.mineru_package_version,
                "mineru_vl_utils_version": self.mineru_vl_utils_version,
                "enrichment_backend": self.enrichment_backend,
                "enrichment_image_analysis": self.enrichment_image_analysis,
                "server_url_sha256": self.server_url_sha256,
                "formula_enabled": self.formula_enabled,
            },
            "dispositions": [item.to_payload() for item in self.dispositions],
        }

    @classmethod
    def from_payload(cls, value: object) -> "VisualSemanticClosure":
        if not isinstance(value, Mapping) or set(value) != {
            "contract_version",
            "source_pdf_sha256",
            "source_pdf_page_count",
            "source_evidence_sha256",
            "source_artifacts",
            "runtime",
            "dispositions",
        }:
            _fail(
                "visual_artifact_shape_invalid",
                "visual semantic artifact root fields are not closed",
            )
        artifacts = value["source_artifacts"]
        runtime = value["runtime"]
        dispositions = value["dispositions"]
        if not isinstance(artifacts, Mapping) or set(artifacts) != {
            "content_list_sha256",
            "content_list_v2_sha256",
            "middle_sha256",
            "model_sha256",
        }:
            _fail(
                "visual_artifact_attestation_invalid",
                "visual semantic source artifact fields are not closed",
            )
        if not isinstance(runtime, Mapping) or set(runtime) != {
            "parser_target_sha256",
            "runtime_bundle_identity_sha256",
            "mineru_package_version",
            "mineru_vl_utils_version",
            "enrichment_backend",
            "enrichment_image_analysis",
            "server_url_sha256",
            "formula_enabled",
        }:
            _fail(
                "visual_runtime_attestation_invalid",
                "visual semantic runtime fields are not closed",
            )
        if not isinstance(dispositions, list):
            _fail(
                "visual_disposition_shape_invalid",
                "visual semantic dispositions must be an array",
            )
        return cls(
            contract_version=cast(str, value["contract_version"]),
            source_pdf_sha256=cast(str, value["source_pdf_sha256"]),
            source_pdf_page_count=cast(int, value["source_pdf_page_count"]),
            source_evidence_sha256=cast(str, value["source_evidence_sha256"]),
            content_list_sha256=cast(str, artifacts["content_list_sha256"]),
            content_list_v2_sha256=cast(
                str,
                artifacts["content_list_v2_sha256"],
            ),
            middle_sha256=cast(str, artifacts["middle_sha256"]),
            model_sha256=cast(str, artifacts["model_sha256"]),
            parser_target_sha256=cast(str, runtime["parser_target_sha256"]),
            runtime_bundle_identity_sha256=cast(
                str,
                runtime["runtime_bundle_identity_sha256"],
            ),
            mineru_package_version=cast(str, runtime["mineru_package_version"]),
            mineru_vl_utils_version=cast(
                str,
                runtime["mineru_vl_utils_version"],
            ),
            enrichment_backend=cast(
                Literal["http-client"],
                runtime["enrichment_backend"],
            ),
            enrichment_image_analysis=cast(
                Literal[True],
                runtime["enrichment_image_analysis"],
            ),
            server_url_sha256=cast(str, runtime["server_url_sha256"]),
            formula_enabled=cast(Literal[True], runtime["formula_enabled"]),
            dispositions=tuple(
                VisualSemanticDisposition.from_payload(item)
                for item in dispositions
            ),
        )


def visual_semantic_bytes(closure: VisualSemanticClosure) -> bytes:
    return canonical_json_bytes(closure.to_payload())


def visual_semantic_diagnostics(
    closure: VisualSemanticClosure,
) -> dict[str, Any]:
    payload = visual_semantic_bytes(closure)
    return {
        "contract_version": closure.contract_version,
        "artifact_role": VISUAL_SEMANTICS_ARTIFACT_ROLE,
        "artifact_sha256": bytes_sha256(payload),
        "disposition_count": len(closure.dispositions),
        "status_counts": closure.status_counts(),
    }


def validate_visual_semantic_diagnostics(
    normalized_ir: Mapping[str, Any],
    closure: VisualSemanticClosure,
    *,
    artifact_sha256: str,
) -> None:
    diagnostics = normalized_ir.get("parser_diagnostics")
    visual = diagnostics.get("visual_semantics") if isinstance(
        diagnostics, Mapping
    ) else None
    expected = visual_semantic_diagnostics(closure)
    if artifact_sha256 != expected["artifact_sha256"] or visual != expected:
        _fail(
            "visual_artifact_binding_invalid",
            "NormalizedIR visual diagnostics differ from the exact artifact",
        )


def validate_visual_semantic_ir(
    normalized_ir: Mapping[str, Any],
    closure: VisualSemanticClosure,
    *,
    artifact_sha256: str,
) -> None:
    """Verify that IR carries only the artifact's exact searchable overlay."""

    validate_visual_semantic_diagnostics(
        normalized_ir,
        closure,
        artifact_sha256=artifact_sha256,
    )
    raw_elements = normalized_ir.get("elements")
    if not isinstance(raw_elements, list):
        _fail("visual_ir_projection_invalid", "NormalizedIR elements are absent")
    elements = {
        item.get("source_item_index"): item
        for item in raw_elements
        if isinstance(item, Mapping)
    }
    for disposition in closure.dispositions:
        if disposition.occurrence_kind == "visual_page":
            continue
        element = elements.get(disposition.source_item_index)
        if not isinstance(element, Mapping):
            _fail(
                "visual_ir_projection_invalid",
                f"visual source item is absent: {disposition.occurrence_id}",
            )
        if disposition.occurrence_kind == "table_media":
            table = element.get("table")
            media_values = (
                table.get("embedded_media")
                if isinstance(table, Mapping)
                else None
            )
            locator = disposition.table_media
            assert locator is not None
            if (
                not isinstance(media_values, list)
                or locator.occurrence_index >= len(media_values)
                or not isinstance(
                    media := media_values[locator.occurrence_index],
                    Mapping,
                )
                or any(
                    media.get(field) != getattr(locator, field)
                    for field in (
                        "occurrence_index",
                        "cell_media_index",
                        "row",
                        "col",
                        "rowspan",
                        "colspan",
                    )
                )
            ):
                _fail(
                    "visual_ir_projection_invalid",
                    f"table media identity differs: {disposition.occurrence_id}",
                )
            _require_projected_text(
                media,
                field="semantic_text",
                disposition=disposition,
            )
            continue
        _require_projected_text(
            element,
            field="visual_semantic_text",
            disposition=disposition,
        )
        if (
            disposition.semantic_origin == "provider_visual_text"
            and element.get("text") != disposition.semantic_text
        ):
            _fail(
                "visual_ir_projection_invalid",
                f"provider visual text differs: {disposition.occurrence_id}",
        )


def validate_visual_semantic_manifest(
    normalized_ir: Mapping[str, Any],
    closure: VisualSemanticClosure,
    *,
    artifact_sha256: str,
    load_artifact_sha256: Callable[[str], str],
) -> None:
    """Close a decoded artifact over IR, manifest identities, and real bytes."""

    validate_visual_semantic_ir(
        normalized_ir,
        closure,
        artifact_sha256=artifact_sha256,
    )
    parser = normalized_ir.get("parser")
    parser_artifacts = normalized_ir.get("parser_artifacts")
    files = (
        parser_artifacts.get("files")
        if isinstance(parser_artifacts, Mapping)
        else None
    )
    if not isinstance(parser, Mapping) or not isinstance(files, Mapping):
        _fail("visual_artifact_attestation_invalid", "IR attestation is absent")

    def manifest_sha(role: str) -> object:
        descriptor = files.get(role)
        return (
            descriptor.get("sha256")
            if isinstance(descriptor, Mapping)
            and descriptor.get("availability") == "present"
            else None
        )

    expected = (
        normalized_ir.get("source_pdf_sha256"),
        normalized_ir.get("source_pdf_page_count"),
        manifest_sha("source_evidence"),
        manifest_sha("content_list"),
        manifest_sha("content_list_v2"),
        manifest_sha("middle"),
        manifest_sha("model"),
        parser_target_sha256(parser),
        parser.get("runtime_bundle_identity_sha256"),
        parser.get("package_version"),
        parser.get("formula"),
    )
    actual = (
        closure.source_pdf_sha256,
        closure.source_pdf_page_count,
        closure.source_evidence_sha256,
        closure.content_list_sha256,
        closure.content_list_v2_sha256,
        closure.middle_sha256,
        closure.model_sha256,
        closure.parser_target_sha256,
        closure.runtime_bundle_identity_sha256,
        closure.mineru_package_version,
        closure.formula_enabled,
    )
    if actual != expected:
        _fail(
            "visual_artifact_attestation_invalid",
            "visual semantic attestation differs from IR/manifest",
        )
    for disposition in closure.dispositions:
        if load_artifact_sha256(disposition.artifact_role) != (
            disposition.artifact_sha256
        ):
            _fail(
                "visual_artifact_binding_invalid",
                f"visual input bytes differ: {disposition.artifact_role}",
            )


def _require_projected_text(
    carrier: Mapping[str, Any],
    *,
    field: str,
    disposition: VisualSemanticDisposition,
) -> None:
    expected = (
        disposition.semantic_text
        if disposition.status == "semantic_text"
        and disposition.semantic_origin == "mineru_content_extract"
        else None
    )
    actual = carrier.get(field)
    if actual != expected or (expected is None and field in carrier):
        _fail(
            "visual_ir_projection_invalid",
            f"visual semantic projection differs: {disposition.occurrence_id}",
        )


def parser_target_sha256(value: Mapping[str, Any]) -> str:
    return bytes_sha256(canonical_json_bytes(value))


def text_sha256(value: str) -> str:
    return bytes_sha256(value.encode("utf-8"))


def bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail(
            "visual_artifact_json_invalid",
            "visual semantic value is not canonical JSON",
        )
        raise AssertionError("unreachable") from exc


def ensure_no_unresolved_visuals(closure: VisualSemanticClosure) -> None:
    if closure.unresolved:
        sample = ", ".join(item.occurrence_id for item in closure.unresolved[:8])
        _fail(
            "visual_semantics_unresolved",
            f"{len(closure.unresolved)} visual occurrence(s) remain unresolved: {sample}",
        )


def _sha(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_int(value: object) -> bool:
    return _index(value) and cast(int, value) > 0


def _bbox(value: Sequence[object]) -> bool:
    if len(value) != 4:
        return False
    coordinates: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            return False
        coordinate = float(item)
        if not math.isfinite(coordinate):
            return False
        coordinates.append(coordinate)
    return (
        coordinates[0] < coordinates[2]
        and coordinates[1] < coordinates[3]
    )


def _fail(reason_code: str, message: str) -> NoReturn:
    raise VisualSemanticContractError(reason_code, message)


__all__ = [
    "MINERU_VL_UTILS_PACKAGE_VERSION",
    "TableMediaLocator",
    "VISUAL_SEMANTICS_ARTIFACT_ROLE",
    "VISUAL_SEMANTICS_CONTRACT_VERSION",
    "VisualOccurrenceKind",
    "VisualSemanticClosure",
    "VisualSemanticContractError",
    "VisualSemanticDisposition",
    "VisualSemanticOrigin",
    "VisualSemanticStatus",
    "bytes_sha256",
    "canonical_json_bytes",
    "ensure_no_unresolved_visuals",
    "parser_target_sha256",
    "text_sha256",
    "validate_visual_semantic_diagnostics",
    "validate_visual_semantic_ir",
    "validate_visual_semantic_manifest",
    "visual_semantic_bytes",
    "visual_semantic_diagnostics",
]
