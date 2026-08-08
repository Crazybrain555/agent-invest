"""Hash-bound public reads for evidence artifacts referenced by a document unit."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, cast
from urllib.parse import quote

from disclosure_anchor.api.errors import evidence_integrity_error
from disclosure_anchor.api.schemas.public import EvidenceRefV1
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.contracts.normalized_ir import (
    NormalizedIRVersionError,
    validate_current_normalized_ir_for_write,
    validate_normalized_ir_identity,
    validate_normalized_ir_path_version,
)
from disclosure_anchor.domain.errors import PathSafetyError


_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_ARTIFACT_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_EVIDENCE_DESCRIPTOR_FIELDS = frozenset(
    {"artifact_role", "sha256", "size_bytes", "media_type"}
)
# Closed producer union: ordinary extracted images use the base fields; a
# fixed-render full-page visual additionally carries both pixel dimensions.
_VISUAL_DESCRIPTOR_FIELDS = frozenset({"pixel_width", "pixel_height"})
_IMAGE_MEDIA_TYPES = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})
_MAX_MIXED_DEPTH = 16


@dataclass(frozen=True, slots=True)
class EvidenceArtifactDescriptor:
    artifact_role: str
    sha256: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, slots=True)
class VerifiedUnitEvidence:
    content: bytes
    sha256: str
    media_type: str


def normalize_evidence_digest(value: str) -> str | None:
    """Return the API's canonical bare sha256 digest, or None if malformed."""

    return value if _DIGEST_RE.fullmatch(value) is not None else None


def unit_evidence_refs(
    *,
    asset_id: str,
    payload_kind: str,
    payload: Any,
    artifact_locator: Any,
) -> list[EvidenceRefV1]:
    """Derive stable request references without exposing storage roles or paths."""

    descriptors = _unit_evidence_descriptors(
        payload_kind=payload_kind,
        payload=_optional_mapping(payload, field="payload"),
        artifact_locator=_optional_mapping(
            artifact_locator,
            field="artifact_locator",
        ),
    )
    refs: list[EvidenceRefV1] = []
    seen: set[str] = set()
    escaped_asset_id = quote(asset_id, safe="")
    for descriptor in descriptors:
        if descriptor.sha256 in seen:
            continue
        seen.add(descriptor.sha256)
        digest = descriptor.sha256.removeprefix("sha256:")
        refs.append(
            EvidenceRefV1(
                uri=f"/v1/units/{escaped_asset_id}/evidence/{digest}",
                sha256=descriptor.sha256,
                size_bytes=descriptor.size_bytes,
                media_type=descriptor.media_type,
            )
        )
    return refs


def read_unit_evidence(
    *,
    row: Mapping[str, Any],
    digest: str,
    paths: FileStorePathBuilder,
) -> VerifiedUnitEvidence | None:
    """Resolve and verify one unit-authorized evidence artifact.

    ``None`` means the syntactically valid digest is not referenced by this
    unit. Every published-state or filesystem inconsistency fails explicitly
    as ``EVIDENCE_INTEGRITY_ERROR``.
    """

    requested_sha256 = f"sha256:{digest}"
    descriptors = _unit_evidence_descriptors(
        payload_kind=_required_text(row, "payload_kind"),
        payload=_optional_mapping(row.get("payload"), field="payload"),
        artifact_locator=_optional_mapping(
            row.get("artifact_locator"),
            field="artifact_locator",
        ),
    )
    matching = tuple(
        descriptor
        for descriptor in descriptors
        if descriptor.sha256 == requested_sha256
    )
    if not matching:
        return None

    document_id = _required_text(row, "document_id")
    _required_text(row, "processing_run_id")
    artifact_owner_id = row.get("artifact_owner_processing_run_id")
    resolved_owner_id = row.get("resolved_artifact_owner_processing_run_id")
    if (
        not isinstance(artifact_owner_id, str)
        or not artifact_owner_id
        or resolved_owner_id != artifact_owner_id
        or row.get("artifact_owner_document_id") != document_id
        or row.get("artifact_owner_run_kind") != "parse"
    ):
        evidence_integrity_error("artifact_owner_invalid")
    provider = _required_text(row, "provider")
    security_code = _required_text(row, "security_code")
    provider_document_id = _required_text(row, "provider_document_id")
    expected_ir_hash = _required_sha256(
        row.get("artifact_hash"),
        "artifact_owner_hash",
    )
    producer_ir_hash = _required_sha256(
        row.get("producer_artifact_hash"),
        "producer_artifact_hash",
    )
    if producer_ir_hash != expected_ir_hash:
        evidence_integrity_error("artifact_owner_hash_mismatch")
    try:
        ir_relpath = paths.normalized_ir_run_relpath(
            provider=provider,
            security_code=security_code,
            provider_document_id=provider_document_id,
            processing_run_id=artifact_owner_id,
        )
        ir_content = _read_data_bytes(
            paths,
            ir_relpath,
            missing_reason="normalized_ir_missing",
            unreadable_reason="normalized_ir_unreadable",
            path_invalid_reason="normalized_ir_path_invalid",
        )
    except PathSafetyError:
        evidence_integrity_error("normalized_ir_path_invalid")

    actual_ir_hash = _sha256(ir_content)
    if actual_ir_hash != expected_ir_hash:
        evidence_integrity_error("normalized_ir_hash_mismatch")
    try:
        decoded = json.loads(ir_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        evidence_integrity_error("normalized_ir_invalid")
    if not isinstance(decoded, Mapping):
        evidence_integrity_error("normalized_ir_invalid")
    normalized_ir = cast(Mapping[str, Any], decoded)
    try:
        # Evidence retrieval serves the stored artifact of the generation
        # that published the unit: integrity is anchored by the unit-row
        # hash equality above plus the closed write shape. The generational
        # gates (current structure algorithm, current parser target, parse
        # receipt) govern new publication, not serving already-published
        # evidence, so the frozen-generation authority applies here.
        version = validate_current_normalized_ir_for_write(
            normalized_ir,
            write_authority="frozen_generation",
        )
        validate_normalized_ir_identity(normalized_ir, document_id=document_id)
        validate_normalized_ir_path_version(ir_relpath, version=version)
    except NormalizedIRVersionError:
        evidence_integrity_error("normalized_ir_invalid")

    parser_artifacts = cast(Mapping[str, Any], normalized_ir["parser_artifacts"])
    files = cast(Mapping[str, Any], parser_artifacts["files"])
    selected_manifest: Mapping[str, Any] | None = None
    for descriptor in matching:
        manifest = files.get(descriptor.artifact_role)
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("availability") != "present"
            or manifest.get("sha256") != descriptor.sha256
            or manifest.get("size_bytes") != descriptor.size_bytes
        ):
            evidence_integrity_error("evidence_manifest_mismatch")
        if selected_manifest is None:
            selected_manifest = manifest
    assert selected_manifest is not None
    relpath_value = selected_manifest.get("relpath")
    if not isinstance(relpath_value, str):
        evidence_integrity_error("evidence_manifest_mismatch")
    try:
        artifact_content = _read_data_bytes(
            paths,
            Path(relpath_value),
            expected_size=matching[0].size_bytes,
            missing_reason="evidence_artifact_missing",
            unreadable_reason="evidence_artifact_unreadable",
            path_invalid_reason="evidence_artifact_path_invalid",
        )
    except PathSafetyError:
        evidence_integrity_error("evidence_artifact_path_invalid")
    if len(artifact_content) != matching[0].size_bytes:
        evidence_integrity_error("evidence_artifact_size_mismatch")
    if _sha256(artifact_content) != requested_sha256:
        evidence_integrity_error("evidence_artifact_hash_mismatch")
    actual_media_type = _image_media_type(artifact_content)
    if actual_media_type != matching[0].media_type:
        evidence_integrity_error("evidence_artifact_media_type_mismatch")
    return VerifiedUnitEvidence(
        content=artifact_content,
        sha256=requested_sha256,
        media_type=actual_media_type,
    )


def _unit_evidence_descriptors(
    *,
    payload_kind: str,
    payload: Mapping[str, Any] | None,
    artifact_locator: Mapping[str, Any] | None,
) -> tuple[EvidenceArtifactDescriptor, ...]:
    descriptors: list[EvidenceArtifactDescriptor] = []
    if artifact_locator is not None:
        descriptors.extend(_locator_descriptors(artifact_locator))
    if payload_kind == "mixed":
        if payload is None:
            evidence_integrity_error("unit_evidence_locator_invalid")
        assert payload is not None
        parts = payload.get("parts")
        if not isinstance(parts, list):
            evidence_integrity_error("unit_evidence_locator_invalid")
        for locator in _mixed_part_locators(parts, depth=0):
            descriptors.extend(_locator_descriptors(locator))

    by_digest: dict[str, tuple[int, str]] = {}
    deduplicated: list[EvidenceArtifactDescriptor] = []
    seen_descriptors: set[EvidenceArtifactDescriptor] = set()
    for descriptor in descriptors:
        identity = (descriptor.size_bytes, descriptor.media_type)
        previous = by_digest.setdefault(descriptor.sha256, identity)
        if previous != identity:
            evidence_integrity_error("unit_evidence_descriptor_conflict")
        if descriptor not in seen_descriptors:
            seen_descriptors.add(descriptor)
            deduplicated.append(descriptor)
    return tuple(deduplicated)


def _mixed_part_locators(
    parts: list[Any],
    *,
    depth: int,
) -> Iterator[Mapping[str, Any]]:
    if depth >= _MAX_MIXED_DEPTH:
        evidence_integrity_error("unit_evidence_locator_invalid")
    for part in parts:
        if not isinstance(part, Mapping):
            evidence_integrity_error("unit_evidence_locator_invalid")
        locator = part.get("artifact_locator")
        if locator is not None:
            if not isinstance(locator, Mapping):
                evidence_integrity_error("unit_evidence_locator_invalid")
            yield cast(Mapping[str, Any], locator)
        nested = part.get("parts")
        if part.get("kind") == "mixed" or nested is not None:
            if not isinstance(nested, list):
                evidence_integrity_error("unit_evidence_locator_invalid")
            yield from _mixed_part_locators(nested, depth=depth + 1)


def _locator_descriptors(
    locator: Mapping[str, Any],
) -> tuple[EvidenceArtifactDescriptor, ...]:
    raw = locator.get("evidence_artifacts")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        evidence_integrity_error("unit_evidence_locator_invalid")
    output: list[EvidenceArtifactDescriptor] = []
    for item in raw:
        if (
            not isinstance(item, Mapping)
            or not _EVIDENCE_DESCRIPTOR_FIELDS.issubset(item)
            or not set(item).issubset(
                _EVIDENCE_DESCRIPTOR_FIELDS | _VISUAL_DESCRIPTOR_FIELDS
            )
        ):
            evidence_integrity_error("unit_evidence_locator_invalid")
        role = item.get("artifact_role")
        sha256 = item.get("sha256")
        size_bytes = item.get("size_bytes")
        media_type = item.get("media_type")
        if (
            not isinstance(role, str)
            or _ARTIFACT_ROLE_RE.fullmatch(role) is None
            or not isinstance(sha256, str)
            or _SHA256_RE.fullmatch(sha256) is None
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or media_type not in _IMAGE_MEDIA_TYPES
        ):
            evidence_integrity_error("unit_evidence_locator_invalid")
        visual_fields = _VISUAL_DESCRIPTOR_FIELDS & item.keys()
        if visual_fields and (
            visual_fields != _VISUAL_DESCRIPTOR_FIELDS
            or any(
                isinstance(item[field], bool)
                or not isinstance(item[field], int)
                or item[field] < 1
                for field in _VISUAL_DESCRIPTOR_FIELDS
            )
        ):
            evidence_integrity_error("unit_evidence_locator_invalid")
        output.append(
            EvidenceArtifactDescriptor(
                artifact_role=role,
                sha256=sha256,
                size_bytes=size_bytes,
                media_type=cast(str, media_type),
            )
        )
    return tuple(output)


def _read_data_bytes(
    paths: FileStorePathBuilder,
    relpath: Path,
    *,
    missing_reason: str,
    unreadable_reason: str,
    path_invalid_reason: str,
    expected_size: int | None = None,
) -> bytes:
    configured_path = paths.data_path(relpath)
    try:
        data_root = paths.data_path(Path()).resolve(strict=True)
        resolved = configured_path.resolve(strict=True)
        resolved.relative_to(data_root)
        if not resolved.is_file():
            evidence_integrity_error(unreadable_reason)
        if expected_size is not None and resolved.stat().st_size != expected_size:
            evidence_integrity_error("evidence_artifact_size_mismatch")
        return resolved.read_bytes()
    except FileNotFoundError:
        evidence_integrity_error(missing_reason)
    except ValueError:
        evidence_integrity_error(path_invalid_reason)
    except OSError:
        evidence_integrity_error(unreadable_reason)


def _image_media_type(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    evidence_integrity_error("evidence_artifact_media_type_mismatch")


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        evidence_integrity_error("published_evidence_identity_invalid")
    return value


def _required_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        evidence_integrity_error(f"{field}_invalid")
    return value


def _optional_mapping(
    value: Any,
    *,
    field: str,
) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        evidence_integrity_error(f"{field}_invalid")
    return cast(Mapping[str, Any], value)


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()
