"""Closed canonical manifest for one remote-parse v4 materialization tree."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, Literal, cast
import unicodedata

from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
)
from disclosure_anchor.application.contracts.staged_resource_paths import (
    validate_relative_resource_path_v4,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


LOCAL_MATERIALIZATION_MANIFEST_V4_SCHEMA = "local-materialization-manifest.v4"
LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME = (
    ".agent-materialization-manifest.v4.json"
)

_INFLIGHT_MARKER_FILENAMES = frozenset(
    {
        ".agent-materialization-inflight.v1.json",
        ".agent-materialization-inflight.v4.json",
    }
)
_RESERVED_MANAGEMENT_FILENAMES_CASEFOLD = frozenset(
    name.casefold()
    for name in {
        LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
        *_INFLIGHT_MARKER_FILENAMES,
    }
)
_MAX_CANONICAL_BYTES = 1024 * 1024
_MAX_INT = (1 << 63) - 1
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")

PayloadFileRoleV4 = Literal["provider_envelope", "parser_artifact"]


@dataclass(frozen=True, slots=True)
class LocalMaterializationPayloadFileV4:
    """One immutable payload file relative to the materialized output root."""

    role: PayloadFileRoleV4
    relpath: str
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        if self.role not in {"provider_envelope", "parser_artifact"}:
            raise ValueError("materialization payload file role is unsupported")
        _relpath(self.relpath, "materialization payload file")
        _sha(self.sha256, "materialization payload file")
        _nonnegative(self.byte_count, "materialization payload file byte count")
        if (
            PurePosixPath(self.relpath).name.casefold()
            in _RESERVED_MANAGEMENT_FILENAMES_CASEFOLD
        ):
            raise ValueError("materialization management file cannot be payload")


@dataclass(frozen=True, slots=True)
class LocalMaterializationObservationsV4:
    """Bounded resource observations made while producing the output tree."""

    member_count: int
    uncompressed_byte_count: int
    decoded_byte_count: int
    temporary_disk_peak_byte_count: int
    output_file_count: int
    output_byte_count: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.member_count, "archive member count"),
            (self.uncompressed_byte_count, "uncompressed byte count"),
            (self.decoded_byte_count, "decoded byte count"),
            (self.temporary_disk_peak_byte_count, "temporary disk peak byte count"),
            (self.output_file_count, "output file count"),
            (self.output_byte_count, "output byte count"),
        ):
            _positive(value, label)


@dataclass(frozen=True, slots=True)
class LocalMaterializationManifestV4:
    """Immutable, portable identity and payload closure for local materialization."""

    attempt_id: str
    fence_identity: str
    document_id: str
    processing_run_id: str
    materialization_intent_sha256: str
    terminal_receipt_sha256: str
    remote_task_identity: str
    artifact_owner_identity: str
    artifact_sha256: str
    artifact_byte_count: int
    source_pdf_sha256: str
    source_page_count: int
    parser_target_sha256: str
    spool_relpath: str
    output_relpath: str
    provider_envelope_relpath: str
    provider_envelope_sha256: str
    provider_envelope_byte_count: int
    observations: LocalMaterializationObservationsV4
    payload_files: tuple[LocalMaterializationPayloadFileV4, ...]
    schema: str = LOCAL_MATERIALIZATION_MANIFEST_V4_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != LOCAL_MATERIALIZATION_MANIFEST_V4_SCHEMA:
            raise ValueError("local materialization manifest schema is unsupported")
        for value, label in (
            (self.attempt_id, "attempt"),
            (self.fence_identity, "fence"),
            (self.document_id, "document"),
            (self.processing_run_id, "processing run"),
            (self.remote_task_identity, "remote task"),
            (self.artifact_owner_identity, "artifact owner"),
        ):
            _identity(value, label)
        for value, label in (
            (self.materialization_intent_sha256, "materialization intent"),
            (self.terminal_receipt_sha256, "terminal receipt"),
            (self.artifact_sha256, "terminal artifact"),
            (self.source_pdf_sha256, "source PDF"),
            (self.parser_target_sha256, "parser target"),
            (self.provider_envelope_sha256, "provider envelope"),
        ):
            _sha(value, label)
        _positive(self.artifact_byte_count, "terminal artifact byte count")
        _positive(self.source_page_count, "source page count")
        _positive(self.provider_envelope_byte_count, "provider envelope byte count")
        for value, label in (
            (self.spool_relpath, "retained spool"),
            (self.output_relpath, "materialized output"),
            (self.provider_envelope_relpath, "provider envelope"),
        ):
            _relpath(value, label)
        if self.spool_relpath == self.output_relpath:
            raise ValueError("materialization spool and output paths must differ")
        if type(self.observations) is not LocalMaterializationObservationsV4:
            raise ValueError("materialization observations are not exact")
        if type(self.payload_files) is not tuple or any(
            type(item) is not LocalMaterializationPayloadFileV4
            for item in self.payload_files
        ):
            raise ValueError("materialization payload files are not an exact tuple")
        if not self.payload_files:
            raise ValueError("materialization payload files are empty")
        ordered = tuple(sorted(self.payload_files, key=lambda item: item.relpath))
        if ordered != self.payload_files:
            raise ValueError("materialization payload files are not canonically ordered")
        paths = tuple(item.relpath for item in self.payload_files)
        canonical_path_keys = tuple(_canonical_path_key(path) for path in paths)
        if len(set(paths)) != len(paths) or len(set(canonical_path_keys)) != len(paths):
            raise ValueError("materialization payload files contain duplicate paths")
        if any(
            left != right
            and (
                left[: len(right)] == right
                or right[: len(left)] == left
            )
            for left in canonical_path_keys
            for right in canonical_path_keys
        ):
            raise ValueError(
                "materialization payload files contain ancestor path conflicts"
            )
        envelope_files = tuple(
            item for item in self.payload_files if item.role == "provider_envelope"
        )
        if len(envelope_files) != 1:
            raise ValueError("materialization payload lacks one provider envelope")
        envelope = envelope_files[0]
        if (
            PurePosixPath(self.provider_envelope_relpath).name
            != PROVIDER_DOCUMENT_FILENAME
            or envelope.relpath != self.provider_envelope_relpath
            or envelope.sha256 != self.provider_envelope_sha256
            or envelope.byte_count != self.provider_envelope_byte_count
        ):
            raise ValueError("provider envelope evidence triple drifted")
        if not any(item.role == "parser_artifact" for item in self.payload_files):
            raise ValueError("materialization payload lacks parser artifacts")
        if (
            self.observations.output_file_count != len(self.payload_files)
            or self.observations.output_byte_count
            != _checked_sum(item.byte_count for item in self.payload_files)
        ):
            raise ValueError("materialization payload observations do not close")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(asdict(self))

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes).hexdigest()


def seal_local_materialization_manifest_v4(
    *,
    attempt_id: str,
    fence_identity: str,
    document_id: str,
    processing_run_id: str,
    materialization_intent_sha256: str,
    terminal_receipt_sha256: str,
    remote_task_identity: str,
    artifact_owner_identity: str,
    artifact_sha256: str,
    artifact_byte_count: int,
    source_pdf_sha256: str,
    source_page_count: int,
    parser_target_sha256: str,
    spool_relpath: str,
    output_relpath: str,
    provider_envelope_relpath: str,
    provider_envelope_sha256: str,
    provider_envelope_byte_count: int,
    observations: LocalMaterializationObservationsV4,
    payload_files: tuple[LocalMaterializationPayloadFileV4, ...],
) -> LocalMaterializationManifestV4:
    """Seal one canonical manifest without claim, clock, or host-local facts."""

    return LocalMaterializationManifestV4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        document_id=document_id,
        processing_run_id=processing_run_id,
        materialization_intent_sha256=materialization_intent_sha256,
        terminal_receipt_sha256=terminal_receipt_sha256,
        remote_task_identity=remote_task_identity,
        artifact_owner_identity=artifact_owner_identity,
        artifact_sha256=artifact_sha256,
        artifact_byte_count=artifact_byte_count,
        source_pdf_sha256=source_pdf_sha256,
        source_page_count=source_page_count,
        parser_target_sha256=parser_target_sha256,
        spool_relpath=spool_relpath,
        output_relpath=output_relpath,
        provider_envelope_relpath=provider_envelope_relpath,
        provider_envelope_sha256=provider_envelope_sha256,
        provider_envelope_byte_count=provider_envelope_byte_count,
        observations=observations,
        payload_files=payload_files,
    )


def decode_local_materialization_manifest_v4(
    exact_bytes: bytes,
) -> LocalMaterializationManifestV4:
    """Strictly decode canonical bytes; reject aliases and unknown fields."""

    if type(exact_bytes) is not bytes or not 1 <= len(exact_bytes) <= _MAX_CANONICAL_BYTES:
        raise ValueError("local materialization manifest bytes are outside the envelope")
    decoded = strict_json_loads(exact_bytes)
    if not isinstance(decoded, dict):
        raise ValueError("local materialization manifest must be an object")
    root = cast(dict[str, Any], decoded)
    _closed(root, LocalMaterializationManifestV4)
    raw_observations = root["observations"]
    if not isinstance(raw_observations, dict):
        raise ValueError("materialization observations must be an object")
    observation_values = cast(dict[str, Any], raw_observations)
    _closed(observation_values, LocalMaterializationObservationsV4)
    raw_files = root["payload_files"]
    if not isinstance(raw_files, list):
        raise ValueError("materialization payload files must be an array")
    payload_files: list[LocalMaterializationPayloadFileV4] = []
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise ValueError("materialization payload file must be an object")
        file_values = cast(dict[str, Any], raw_file)
        _closed(file_values, LocalMaterializationPayloadFileV4)
        payload_files.append(LocalMaterializationPayloadFileV4(**file_values))
    value = LocalMaterializationManifestV4(
        **{
            **root,
            "observations": LocalMaterializationObservationsV4(
                **observation_values
            ),
            "payload_files": tuple(payload_files),
        }
    )
    if value.canonical_bytes != exact_bytes:
        raise ValueError("local materialization manifest JSON is not canonical")
    return value


def _closed(value: dict[str, Any], item_type: type[Any]) -> None:
    if set(value) != {item.name for item in fields(item_type)}:
        raise ValueError(f"{item_type.__name__} fields are not closed")


def _canonical_json(value: object) -> bytes:
    try:
        exact = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("local materialization manifest is not strict JSON") from exc
    if not 1 <= len(exact) <= _MAX_CANONICAL_BYTES:
        raise ValueError("local materialization manifest bytes are outside the envelope")
    return exact


def _identity(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 1024
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{label} identity is invalid")


def _sha(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} hash is not canonical")


def _positive(value: int, label: str) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_INT:
        raise ValueError(f"{label} must be a positive bounded integer")


def _nonnegative(value: int, label: str) -> None:
    if type(value) is not int or not 0 <= value <= _MAX_INT:
        raise ValueError(f"{label} must be a non-negative bounded integer")


def _checked_sum(values: Any) -> int:
    total = 0
    for value in values:
        _nonnegative(value, "materialization payload byte count")
        if total > _MAX_INT - value:
            raise ValueError("materialization payload byte count overflowed")
        total += value
    return total


def _relpath(value: str, label: str) -> None:
    validate_relative_resource_path_v4(value, label)


def _canonical_path_key(value: str) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFC", part).casefold()
        for part in PurePosixPath(value).parts
    )


__all__ = [
    "LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME",
    "LOCAL_MATERIALIZATION_MANIFEST_V4_SCHEMA",
    "LocalMaterializationManifestV4",
    "LocalMaterializationObservationsV4",
    "LocalMaterializationPayloadFileV4",
    "PayloadFileRoleV4",
    "decode_local_materialization_manifest_v4",
    "seal_local_materialization_manifest_v4",
]
