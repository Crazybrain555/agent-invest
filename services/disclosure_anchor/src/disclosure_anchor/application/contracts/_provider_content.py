"""Shared structural provider-content codec; no business admission authority.

Decoding checks the typed shape. validate_provider_content additionally checks
profile, media and canonical raw preimages. Neither proves a source/bundle read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from typing import cast

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)

from disclosure_anchor.application.contracts.provider_document import (
    PhysicalTableLogicalStatus,
    ProviderArtifact,
    ProviderBBox,
    ProviderBlock,
    ProviderDocument,
    ProviderPage,
    ProviderPayload,
    ProviderPhysicalTableSegment,
)


_REQUIRED_ARTIFACT_ROLES = frozenset(
    {"content_list", "content_list_v2", "middle_json", "model_json"}
)
_PUBLIC_IMAGE_MEDIA_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)


class ProviderDocumentEnvelopeError(ValueError):
    """The provider-document record is structurally invalid or noncanonical."""


def validate_provider_content(
    document: ProviderDocument, target_identity: ParserTargetIdentity,
) -> None:
    """Apply exactly the envelope's content/profile/raw-preimage checks."""

    if (
        document.parser_version != "3.4.4"
        or document.backend != "hybrid"
        or document.effort != "medium"
    ):
        raise ProviderDocumentEnvelopeError(
            "provider document is not the pinned MinerU 3.4.4 Medium lane"
        )
    _validate_medium_target(
        target_identity,
        document=document,
    )
    artifacts_by_role = {
        artifact.role: artifact for artifact in document.artifacts
    }
    if not _REQUIRED_ARTIFACT_ROLES.issubset(artifacts_by_role):
        raise ProviderDocumentEnvelopeError(
            "provider document is missing a required MinerU artifact role"
        )
    if any(
        artifacts_by_role[role].media_type != "application/json"
        for role in _REQUIRED_ARTIFACT_ROLES
    ):
        raise ProviderDocumentEnvelopeError(
            "required MinerU JSON artifact has the wrong media type"
        )
    expected_optional_media = {
        "layout_pdf": "application/pdf",
        "markdown": "text/markdown",
        "origin_pdf": "application/pdf",
    }
    for role, media_type in expected_optional_media.items():
        artifact = artifacts_by_role.get(role)
        if artifact is not None and artifact.media_type != media_type:
            raise ProviderDocumentEnvelopeError(
                f"MinerU artifact role {role} has the wrong media type"
            )
    evidence_roles = {
        role
        for block in document.blocks
        for role in block.referenced_artifact_roles
    }
    evidence_roles.update(
        segment.crop_artifact_role
        for segment in document.physical_table_segments
        if segment.crop_artifact_role is not None
    )
    if any(
        artifacts_by_role[role].media_type not in _PUBLIC_IMAGE_MEDIA_TYPES
        for role in evidence_roles
    ):
        raise ProviderDocumentEnvelopeError(
            "provider evidence artifact is not a verified image"
        )
    _validate_raw_record_hashes(document)


def provider_document_to_payload(document: ProviderDocument) -> dict[str, object]:
    return {
        "artifacts": [
            {
                "media_type": artifact.media_type,
                "relative_path": artifact.relative_path,
                "role": artifact.role,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            }
            for artifact in document.artifacts
        ],
        "backend": document.backend,
        "bundle_sha256": document.bundle_sha256,
        "effort": document.effort,
        "ocr_enabled": document.ocr_enabled,
        "pages": [
            {
                "blocks": [_block_payload(block) for block in page.blocks],
                "page_index": page.page_index,
                "page_size": list(page.page_size),
            }
            for page in document.pages
        ],
        "parser_version": document.parser_version,
        "physical_table_segments": [
            _physical_table_segment_payload(segment)
            for segment in document.physical_table_segments
        ],
        "source_pdf_sha256": document.source_pdf_sha256,
    }


def _block_payload(block: ProviderBlock) -> dict[str, object]:
    return {
        "bbox": _bbox_payload(block.bbox),
        "order_in_page": block.order_in_page,
        "page_index": block.page_index,
        "payloads": [
            {
                "field": payload.field,
                "item_index": payload.item_index,
                "text": payload.text,
            }
            for payload in block.payloads
        ],
        "provider_level": block.provider_level,
        "provider_type": block.provider_type,
        "raw_item_json": block.raw_item_json,
        "raw_item_sha256": block.raw_item_sha256,
        "referenced_artifact_roles": list(block.referenced_artifact_roles),
        "source_index": block.source_index,
        "typed_annotation": block.typed_annotation,
    }


def _physical_table_segment_payload(
    segment: ProviderPhysicalTableSegment,
) -> dict[str, object]:
    return {
        "bbox": _bbox_payload(segment.bbox),
        "crop_artifact_role": segment.crop_artifact_role,
        "logical_stream_status": segment.logical_stream_status,
        "order_in_page": segment.order_in_page,
        "page_index": segment.page_index,
        "page_local_html": segment.page_local_html,
        "provider_index": segment.provider_index,
        "raw_segment_json": segment.raw_segment_json,
        "raw_segment_sha256": segment.raw_segment_sha256,
    }


def _bbox_payload(bbox: ProviderBBox | None) -> list[float] | None:
    if bbox is None:
        return None
    return list(bbox.as_tuple())


def provider_document_from_payload(value: object) -> ProviderDocument:
    payload = _exact_mapping(
        value,
        keys={
            "artifacts",
            "backend",
            "bundle_sha256",
            "effort",
            "ocr_enabled",
            "pages",
            "parser_version",
            "physical_table_segments",
            "source_pdf_sha256",
        },
        label="provider document",
    )
    artifacts = tuple(
        _artifact_from_payload(item)
        for item in _sequence(payload["artifacts"], "artifacts")
    )
    pages = tuple(
        _page_from_payload(item) for item in _sequence(payload["pages"], "pages")
    )
    segments = tuple(
        _physical_table_segment_from_payload(item)
        for item in _sequence(
            payload["physical_table_segments"], "physical_table_segments"
        )
    )
    return ProviderDocument(
        source_pdf_sha256=_text(payload["source_pdf_sha256"], "source_pdf_sha256"),
        parser_version=_text(payload["parser_version"], "parser_version"),
        backend=_text(payload["backend"], "backend"),
        effort=_text(payload["effort"], "effort"),
        ocr_enabled=_boolean(payload["ocr_enabled"], "ocr_enabled"),
        pages=pages,
        physical_table_segments=segments,
        artifacts=artifacts,
        bundle_sha256=_text(payload["bundle_sha256"], "bundle_sha256"),
    )


def _artifact_from_payload(value: object) -> ProviderArtifact:
    payload = _exact_mapping(
        value,
        keys={"media_type", "relative_path", "role", "sha256", "size_bytes"},
        label="provider artifact",
    )
    return ProviderArtifact(
        role=_text(payload["role"], "artifact.role"),
        relative_path=_text(payload["relative_path"], "artifact.relative_path"),
        sha256=_text(payload["sha256"], "artifact.sha256"),
        size_bytes=_integer(payload["size_bytes"], "artifact.size_bytes"),
        media_type=_text(payload["media_type"], "artifact.media_type"),
    )


def _page_from_payload(value: object) -> ProviderPage:
    payload = _exact_mapping(
        value,
        keys={"blocks", "page_index", "page_size"},
        label="provider page",
    )
    page_size = _sequence(payload["page_size"], "page_size")
    if len(page_size) != 2:
        raise ProviderDocumentEnvelopeError("provider page size must have two values")
    return ProviderPage(
        page_index=_integer(payload["page_index"], "page_index"),
        page_size=(
            _number(page_size[0], "page_size[0]"),
            _number(page_size[1], "page_size[1]"),
        ),
        blocks=tuple(
            _block_from_payload(item) for item in _sequence(payload["blocks"], "blocks")
        ),
    )


def _block_from_payload(value: object) -> ProviderBlock:
    payload = _exact_mapping(
        value,
        keys={
            "bbox",
            "order_in_page",
            "page_index",
            "payloads",
            "provider_level",
            "provider_type",
            "raw_item_json",
            "raw_item_sha256",
            "referenced_artifact_roles",
            "source_index",
            "typed_annotation",
        },
        label="provider block",
    )
    provider_level_value = payload["provider_level"]
    typed_annotation_value = payload["typed_annotation"]
    return ProviderBlock(
        source_index=_integer(payload["source_index"], "source_index"),
        page_index=_integer(payload["page_index"], "page_index"),
        order_in_page=_integer(payload["order_in_page"], "order_in_page"),
        provider_type=_text(payload["provider_type"], "provider_type"),
        typed_annotation=(
            None
            if typed_annotation_value is None
            else _text(typed_annotation_value, "typed_annotation")
        ),
        provider_level=(
            None
            if provider_level_value is None
            else _integer(provider_level_value, "provider_level")
        ),
        bbox=_bbox_from_payload(payload["bbox"], "block.bbox"),
        payloads=tuple(
            _provider_payload_from_payload(item)
            for item in _sequence(payload["payloads"], "payloads")
        ),
        referenced_artifact_roles=tuple(
            _text(item, "referenced_artifact_role")
            for item in _sequence(
                payload["referenced_artifact_roles"],
                "referenced_artifact_roles",
            )
        ),
        raw_item_json=_text(payload["raw_item_json"], "raw_item_json"),
        raw_item_sha256=_text(payload["raw_item_sha256"], "raw_item_sha256"),
    )


def _provider_payload_from_payload(value: object) -> ProviderPayload:
    payload = _exact_mapping(
        value,
        keys={"field", "item_index", "text"},
        label="provider payload",
    )
    item_index = payload["item_index"]
    return ProviderPayload(
        field=_text(payload["field"], "payload.field"),
        item_index=(
            None if item_index is None else _integer(item_index, "payload.item_index")
        ),
        text=_text_allow_empty(payload["text"], "payload.text"),
    )


def _physical_table_segment_from_payload(
    value: object,
) -> ProviderPhysicalTableSegment:
    payload = _exact_mapping(
        value,
        keys={
            "bbox",
            "crop_artifact_role",
            "logical_stream_status",
            "order_in_page",
            "page_index",
            "page_local_html",
            "provider_index",
            "raw_segment_json",
            "raw_segment_sha256",
        },
        label="provider physical table segment",
    )
    crop_role = payload["crop_artifact_role"]
    status = _text(payload["logical_stream_status"], "logical_stream_status")
    if status not in {"retained", "deleted", "unbound"}:
        raise ProviderDocumentEnvelopeError("unsupported table segment status")
    return ProviderPhysicalTableSegment(
        page_index=_integer(payload["page_index"], "segment.page_index"),
        order_in_page=_integer(payload["order_in_page"], "segment.order_in_page"),
        provider_index=_integer(payload["provider_index"], "segment.provider_index"),
        bbox=_bbox_from_payload(payload["bbox"], "segment.bbox"),
        page_local_html=_text_allow_empty(
            payload["page_local_html"], "page_local_html"
        ),
        crop_artifact_role=(
            None if crop_role is None else _text(crop_role, "crop_artifact_role")
        ),
        logical_stream_status=cast(PhysicalTableLogicalStatus, status),
        raw_segment_json=_text(payload["raw_segment_json"], "raw_segment_json"),
        raw_segment_sha256=_text(payload["raw_segment_sha256"], "raw_segment_sha256"),
    )


def _bbox_from_payload(value: object, label: str) -> ProviderBBox | None:
    if value is None:
        return None
    values = _sequence(value, label)
    if len(values) != 4:
        raise ProviderDocumentEnvelopeError(f"{label} must have four values")
    return ProviderBBox(*(_number(item, label) for item in values))


def _validate_medium_target(
    target: ParserTargetIdentity,
    *,
    document: ProviderDocument,
) -> None:
    expected: dict[str, object] = {
        "name": "MinerU",
        "package_version": document.parser_version,
        "backend": "hybrid-http-client",
        "method": "auto",
        "language": "ch",
        "formula": True,
        "table": True,
        "effort": "medium",
        "image_analysis": False,
        "full_pdf": True,
        "start_page": None,
        "end_page": None,
    }
    for key, expected_value in expected.items():
        if getattr(target, key) != expected_value:
            raise ProviderDocumentEnvelopeError(
                f"parser target field {key} is not the pinned Medium profile"
            )


def _validate_raw_record_hashes(document: ProviderDocument) -> None:
    for block in document.blocks:
        _require_canonical_json(block.raw_item_json, label="provider block raw JSON")
        if block.raw_item_sha256 != _sha256(block.raw_item_json.encode("utf-8")):
            raise ProviderDocumentEnvelopeError(
                "provider block hash does not match its raw JSON"
            )
    for segment in document.physical_table_segments:
        _require_canonical_json(
            segment.raw_segment_json,
            label="provider table segment raw JSON",
        )
        if segment.raw_segment_sha256 != _sha256(
            segment.raw_segment_json.encode("utf-8")
        ):
            raise ProviderDocumentEnvelopeError(
                "provider table segment hash does not match its raw JSON"
            )


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProviderDocumentEnvelopeError(
            "value cannot be represented as canonical JSON"
        ) from exc


def _canonical_json_text(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProviderDocumentEnvelopeError(
            "value cannot be represented as canonical JSON"
        ) from exc


def _require_canonical_json(value: str, *, label: str) -> None:
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderDocumentEnvelopeError(f"{label} is invalid") from exc
    if _canonical_json_text(decoded) != value:
        raise ProviderDocumentEnvelopeError(f"{label} is not canonical")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _exact_object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not value:
        raise ProviderDocumentEnvelopeError(f"{label} must be a non-empty object")
    if not all(isinstance(key, str) for key in value):
        raise ProviderDocumentEnvelopeError(f"{label} keys must be strings")
    return cast(Mapping[str, object], value)


def _exact_mapping(
    value: object,
    *,
    keys: set[str],
    label: str,
) -> Mapping[str, object]:
    payload = _exact_object(value, label)
    if set(payload) != keys:
        raise ProviderDocumentEnvelopeError(f"{label} fields are not closed")
    return payload


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ProviderDocumentEnvelopeError(f"{label} must be an array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProviderDocumentEnvelopeError(f"{label} must be non-empty text")
    return value


def _text_allow_empty(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ProviderDocumentEnvelopeError(f"{label} must be text")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderDocumentEnvelopeError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderDocumentEnvelopeError(f"{label} must be a number")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ProviderDocumentEnvelopeError(f"{label} must be boolean")
    return value


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()
