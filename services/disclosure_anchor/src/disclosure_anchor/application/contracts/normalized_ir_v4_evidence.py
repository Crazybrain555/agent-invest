"""Frozen read-only evidence manifest projection for historical NormalizedIR v4.

This module deliberately does not validate the old writer contract. Published
units already authorize a specific evidence role, digest, and size; the read
path only proves that those claims still point into the hash-bound v4 parser
artifact manifest. Writer semantics, elements, structure, and diagnostics are
outside this compatibility boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import PurePath, PurePosixPath
import re
from typing import Any, Literal, NoReturn, cast


NORMALIZED_IR_V4_CONTRACT_VERSION = "normalized_ir.v4"
NORMALIZED_IR_V4_FILENAME = "normalized_ir.v4.json"

_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_ARTIFACT_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:/")
_PARSER_ARTIFACT_FIELDS = frozenset({"artifact_root_relpath", "files"})
_PRESENT_DESCRIPTOR_FIELDS = frozenset(
    {"availability", "relpath", "sha256", "size_bytes"}
)

HistoricalEvidenceFailureReason = Literal[
    "normalized_ir_invalid",
    "evidence_manifest_mismatch",
]


class HistoricalNormalizedIRV4EvidenceError(ValueError):
    """The frozen v4 evidence projection is malformed or contradicts a unit."""

    def __init__(
        self,
        reason: HistoricalEvidenceFailureReason,
        message: str,
    ) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceClaim:
    artifact_role: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceArtifact:
    relpath: PurePosixPath
    sha256: str
    size_bytes: int


def resolve_historical_normalized_ir_v4_evidence(
    content: bytes,
    *,
    ir_relpath: PurePath,
    expected_document_id: str,
    expected_source_pdf_sha256: str,
    claims: Sequence[HistoricalEvidenceClaim],
) -> HistoricalEvidenceArtifact:
    """Resolve unit-authorized evidence from one exact historical v4 file."""

    if ir_relpath.name != NORMALIZED_IR_V4_FILENAME:
        _invalid("historical NormalizedIR filename is not normalized_ir.v4.json")
    if not isinstance(expected_document_id, str) or not expected_document_id:
        _invalid("expected document identity is missing")
    if _SHA256_RE.fullmatch(expected_source_pdf_sha256) is None:
        _invalid("expected source PDF hash is invalid")
    if not claims:
        _mismatch("no evidence claims were supplied")

    payload = _decode_object(content)
    if payload.get("contract_version") != NORMALIZED_IR_V4_CONTRACT_VERSION:
        _invalid("historical NormalizedIR contract version is unsupported")
    if payload.get("document_id") != expected_document_id:
        _invalid("historical NormalizedIR document identity differs")
    if payload.get("source_pdf_sha256") != expected_source_pdf_sha256:
        _invalid("historical NormalizedIR source PDF identity differs")

    parser_artifacts = payload.get("parser_artifacts")
    if not isinstance(parser_artifacts, Mapping):
        _invalid("parser_artifacts must be an object")
    if frozenset(parser_artifacts) != _PARSER_ARTIFACT_FIELDS:
        _invalid("parser_artifacts fields are not the frozen v4 subset")
    artifact_root = _safe_relpath(
        parser_artifacts.get("artifact_root_relpath"),
        label="artifact_root_relpath",
    )
    files = parser_artifacts.get("files")
    if not isinstance(files, Mapping) or not files:
        _invalid("parser_artifacts.files must be a non-empty object")

    selected: HistoricalEvidenceArtifact | None = None
    for claim in claims:
        _validate_claim(claim)
        descriptor = files.get(claim.artifact_role)
        if not isinstance(descriptor, Mapping):
            _mismatch("authorized evidence role is absent from the v4 manifest")
        if descriptor.get("availability") != "present":
            _mismatch("authorized evidence role was not emitted")
        if frozenset(descriptor) != _PRESENT_DESCRIPTOR_FIELDS:
            _invalid("selected evidence descriptor fields are invalid")
        manifest_sha256 = descriptor.get("sha256")
        manifest_size = descriptor.get("size_bytes")
        if (
            not isinstance(manifest_sha256, str)
            or _SHA256_RE.fullmatch(manifest_sha256) is None
            or isinstance(manifest_size, bool)
            or not isinstance(manifest_size, int)
            or manifest_size < 0
        ):
            _invalid("selected evidence descriptor identity is invalid")
        if (
            manifest_sha256 != claim.sha256
            or manifest_size != claim.size_bytes
        ):
            _mismatch("authorized evidence identity differs from the v4 manifest")
        relpath = _safe_relpath(
            descriptor.get("relpath"),
            label="selected evidence relpath",
        )
        try:
            suffix = relpath.relative_to(artifact_root)
        except ValueError:
            _invalid("selected evidence relpath escapes its artifact root")
        if not suffix.parts:
            _invalid("selected evidence relpath must name a file below its root")
        artifact = HistoricalEvidenceArtifact(
            relpath=relpath,
            sha256=claim.sha256,
            size_bytes=claim.size_bytes,
        )
        if selected is None:
            selected = artifact

    assert selected is not None
    return selected


def _decode_object(content: bytes) -> Mapping[str, Any]:
    try:
        text = content.decode("utf-8")
        decoded = json.loads(
            text,
            object_pairs_hook=_closed_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _invalid("historical NormalizedIR is not strict UTF-8 JSON")
    if not isinstance(decoded, Mapping):
        _invalid("historical NormalizedIR root must be an object")
    return cast(Mapping[str, Any], decoded)


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_claim(claim: HistoricalEvidenceClaim) -> None:
    if (
        not isinstance(claim.artifact_role, str)
        or _ARTIFACT_ROLE_RE.fullmatch(claim.artifact_role) is None
    ):
        _mismatch("authorized evidence role is invalid")
    if (
        not isinstance(claim.sha256, str)
        or _SHA256_RE.fullmatch(claim.sha256) is None
    ):
        _mismatch("authorized evidence hash is invalid")
    if (
        isinstance(claim.size_bytes, bool)
        or not isinstance(claim.size_bytes, int)
        or claim.size_bytes < 0
    ):
        _mismatch("authorized evidence size is invalid")


def _safe_relpath(value: object, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        _invalid(f"{label} must be non-empty text")
    if (
        "\\" in value
        or "\x00" in value
        or value.startswith("file:")
        or _WINDOWS_DRIVE_RE.match(value) is not None
    ):
        _invalid(f"{label} is not a canonical POSIX relative path")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        _invalid(f"{label} contains an unsafe path component")
    path = PurePosixPath(value)
    if path.is_absolute():
        _invalid(f"{label} must be relative")
    return path


def _invalid(message: str) -> NoReturn:
    raise HistoricalNormalizedIRV4EvidenceError(
        "normalized_ir_invalid",
        message,
    )


def _mismatch(message: str) -> NoReturn:
    raise HistoricalNormalizedIRV4EvidenceError(
        "evidence_manifest_mismatch",
        message,
    )
