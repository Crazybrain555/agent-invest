"""Persist one source-bound MinerU Medium provider document without legacy NIR."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import cast
import unicodedata

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


PROVIDER_DOCUMENT_CONTRACT_VERSION = "provider_document.v1"
PROVIDER_DOCUMENT_FILENAME = "provider_document.v1.json"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REQUIRED_ARTIFACT_ROLES = frozenset(
    {"content_list", "content_list_v2", "middle_json", "model_json"}
)
_PUBLIC_IMAGE_MEDIA_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)


class ProviderDocumentEnvelopeError(ValueError):
    """The persisted provider document is incomplete or contradictory."""


@dataclass(frozen=True, slots=True)
class ProviderDocumentEnvelope:
    """One immutable parse-owner projection used by later deterministic builds."""

    document_id: str
    artifact_owner_processing_run_id: str
    provider: str
    provider_document_id: str
    source_pdf_relpath: str
    input_raw_file_hash: str
    source_pdf_page_count: int
    parser_artifact_root_relpath: str
    parser_target_identity: ParserTargetIdentity
    provider_document: ProviderDocument
    contract_version: str = PROVIDER_DOCUMENT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != PROVIDER_DOCUMENT_CONTRACT_VERSION:
            raise ProviderDocumentEnvelopeError(
                "provider document contract version is unsupported"
            )
        _identifier(self.document_id, label="document_id")
        _identifier(
            self.artifact_owner_processing_run_id,
            label="artifact_owner_processing_run_id",
        )
        _identifier(self.provider, label="provider")
        _safe_provider_document_id(self.provider_document_id)
        _safe_relpath(
            self.source_pdf_relpath,
            label="source_pdf_relpath",
            root="raw_documents",
        )
        _safe_relpath(
            self.parser_artifact_root_relpath,
            label="parser_artifact_root_relpath",
            root="parser_artifacts",
        )
        source_parts = PurePosixPath(self.source_pdf_relpath).parts
        if (
            len(source_parts) != 6
            or source_parts[1] != self.provider
            or source_parts[4] != self.provider_document_id
        ):
            raise ProviderDocumentEnvelopeError(
                "source PDF path does not bind provider and provider document"
            )
        _identifier(source_parts[2], label="source security_code")
        _identifier(source_parts[3], label="source year")
        parser_parts = PurePosixPath(self.parser_artifact_root_relpath).parts
        source_digest_name = "sha256_" + self.input_raw_file_hash.removeprefix(
            "sha256:"
        )
        if (
            len(parser_parts) != 7
            or parser_parts[1] != self.provider
            or parser_parts[2] != source_parts[2]
            or parser_parts[3] != self.provider_document_id
            or parser_parts[4] != self.artifact_owner_processing_run_id
            or parser_parts[5] != source_digest_name
            or parser_parts[6] != "hybrid_auto"
        ):
            raise ProviderDocumentEnvelopeError(
                "parser artifact root does not bind provider and artifact owner"
            )
        _identifier(parser_parts[2], label="parser security_code")
        if not _SHA256_RE.fullmatch(self.input_raw_file_hash):
            raise ProviderDocumentEnvelopeError("input raw file hash is invalid")
        if self.input_raw_file_hash != self.provider_document.source_pdf_sha256:
            raise ProviderDocumentEnvelopeError(
                "input raw file hash differs from provider source identity"
            )
        source_name = PurePosixPath(self.source_pdf_relpath).name
        if source_name != source_digest_name + ".pdf":
            raise ProviderDocumentEnvelopeError(
                "source PDF path does not match its registered hash"
            )
        if (
            isinstance(self.source_pdf_page_count, bool)
            or not isinstance(self.source_pdf_page_count, int)
            or self.source_pdf_page_count < 1
        ):
            raise ProviderDocumentEnvelopeError(
                "source PDF page count must be a positive integer"
            )
        if self.source_pdf_page_count != len(self.provider_document.pages):
            raise ProviderDocumentEnvelopeError(
                "source PDF page count differs from provider pages"
            )
        if (
            self.provider_document.parser_version != "3.4.4"
            or self.provider_document.backend != "hybrid"
            or self.provider_document.effort != "medium"
        ):
            raise ProviderDocumentEnvelopeError(
                "provider document is not the pinned MinerU 3.4.4 Medium lane"
            )
        _validate_medium_target(
            self.parser_target_identity,
            document=self.provider_document,
        )
        artifacts_by_role = {
            artifact.role: artifact for artifact in self.provider_document.artifacts
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
            for block in self.provider_document.blocks
            for role in block.referenced_artifact_roles
        }
        evidence_roles.update(
            segment.crop_artifact_role
            for segment in self.provider_document.physical_table_segments
            if segment.crop_artifact_role is not None
        )
        if any(
            artifacts_by_role[role].media_type not in _PUBLIC_IMAGE_MEDIA_TYPES
            for role in evidence_roles
        ):
            raise ProviderDocumentEnvelopeError(
                "provider evidence artifact is not a verified image"
            )
        _validate_raw_record_hashes(self.provider_document)

    @classmethod
    def build(
        cls,
        *,
        document_id: str,
        artifact_owner_processing_run_id: str,
        provider: str,
        provider_document_id: str,
        source_pdf_relpath: str,
        source_pdf_page_count: int,
        parser_artifact_root_relpath: str,
        parser_target_identity: ParserTargetIdentity,
        provider_document: ProviderDocument,
    ) -> "ProviderDocumentEnvelope":
        return cls(
            document_id=document_id,
            artifact_owner_processing_run_id=artifact_owner_processing_run_id,
            provider=provider,
            provider_document_id=provider_document_id,
            source_pdf_relpath=source_pdf_relpath,
            input_raw_file_hash=provider_document.source_pdf_sha256,
            source_pdf_page_count=source_pdf_page_count,
            parser_artifact_root_relpath=parser_artifact_root_relpath,
            parser_target_identity=parser_target_identity,
            provider_document=provider_document,
        )


def provider_document_envelope_to_payload(
    envelope: ProviderDocumentEnvelope,
) -> dict[str, object]:
    """Return the closed JSON value written by the artifact store."""

    payload = _provider_document_envelope_payload(envelope)
    provider_document_envelope_from_payload(payload)
    return payload


def _provider_document_envelope_payload(
    envelope: ProviderDocumentEnvelope,
) -> dict[str, object]:
    return {
        "artifact_owner_processing_run_id": (envelope.artifact_owner_processing_run_id),
        "contract_version": envelope.contract_version,
        "document_id": envelope.document_id,
        "input_raw_file_hash": envelope.input_raw_file_hash,
        "parser_artifact_root_relpath": envelope.parser_artifact_root_relpath,
        "parser_target_identity": envelope.parser_target_identity.to_payload(),
        "provider": envelope.provider,
        "provider_document": _provider_document_payload(envelope.provider_document),
        "provider_document_id": envelope.provider_document_id,
        "source_pdf_page_count": envelope.source_pdf_page_count,
        "source_pdf_relpath": envelope.source_pdf_relpath,
    }


def provider_document_envelope_from_payload(value: object) -> ProviderDocumentEnvelope:
    """Load and validate one exact ``provider_document.v1`` JSON value."""

    payload = _exact_mapping(
        value,
        keys={
            "artifact_owner_processing_run_id",
            "contract_version",
            "document_id",
            "input_raw_file_hash",
            "parser_artifact_root_relpath",
            "parser_target_identity",
            "provider",
            "provider_document",
            "provider_document_id",
            "source_pdf_page_count",
            "source_pdf_relpath",
        },
        label="provider document envelope",
    )
    try:
        target = ParserTargetIdentity.from_payload(payload["parser_target_identity"])
    except ValueError as exc:
        raise ProviderDocumentEnvelopeError(
            "parser target identity is invalid"
        ) from exc
    return ProviderDocumentEnvelope(
        contract_version=_text(payload["contract_version"], "contract_version"),
        document_id=_text(payload["document_id"], "document_id"),
        artifact_owner_processing_run_id=_text(
            payload["artifact_owner_processing_run_id"],
            "artifact_owner_processing_run_id",
        ),
        provider=_text(payload["provider"], "provider"),
        provider_document_id=_text(
            payload["provider_document_id"], "provider_document_id"
        ),
        source_pdf_relpath=_text(payload["source_pdf_relpath"], "source_pdf_relpath"),
        input_raw_file_hash=_text(
            payload["input_raw_file_hash"], "input_raw_file_hash"
        ),
        source_pdf_page_count=_integer(
            payload["source_pdf_page_count"], "source_pdf_page_count"
        ),
        parser_artifact_root_relpath=_text(
            payload["parser_artifact_root_relpath"],
            "parser_artifact_root_relpath",
        ),
        parser_target_identity=target,
        provider_document=_provider_document_from_payload(payload["provider_document"]),
    )


def provider_document_envelope_to_bytes(
    envelope: ProviderDocumentEnvelope,
) -> bytes:
    """Encode the one canonical byte representation bound by artifact_hash."""

    return _canonical_json_bytes(provider_document_envelope_to_payload(envelope))


def provider_document_envelope_from_bytes(
    value: bytes,
) -> ProviderDocumentEnvelope:
    """Decode only the canonical byte representation of one envelope."""

    try:
        decoded = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProviderDocumentEnvelopeError(
            "provider document envelope is not valid canonical JSON"
        ) from exc
    envelope = provider_document_envelope_from_payload(decoded)
    if provider_document_envelope_to_bytes(envelope) != value:
        raise ProviderDocumentEnvelopeError(
            "provider document envelope bytes are not canonical"
        )
    return envelope


def _provider_document_payload(document: ProviderDocument) -> dict[str, object]:
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
        "cell_merge_json": segment.cell_merge_json,
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


def _provider_document_from_payload(value: object) -> ProviderDocument:
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
            "cell_merge_json",
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
    cell_merge = payload["cell_merge_json"]
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
        cell_merge_json=(
            None if cell_merge is None else _text(cell_merge, "cell_merge_json")
        ),
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


def _identifier(value: str, *, label: str) -> None:
    if not _IDENTIFIER_RE.fullmatch(value) or value in {".", ".."}:
        raise ProviderDocumentEnvelopeError(f"{label} is unsafe")


def _safe_relpath(value: str, *, label: str, root: str) -> None:
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ProviderDocumentEnvelopeError(f"{label} is unsafe")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or path.parts[0] != root
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProviderDocumentEnvelopeError(f"{label} is unsafe")


def _safe_provider_document_id(value: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or value[0] == "."
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ProviderDocumentEnvelopeError("provider_document_id is unsafe")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = [
    "PROVIDER_DOCUMENT_CONTRACT_VERSION",
    "PROVIDER_DOCUMENT_FILENAME",
    "ProviderDocumentEnvelope",
    "ProviderDocumentEnvelopeError",
    "provider_document_envelope_from_bytes",
    "provider_document_envelope_from_payload",
    "provider_document_envelope_to_bytes",
    "provider_document_envelope_to_payload",
]
