"""Pure deterministic paths shared by v4 staged-resource adapters and evidence."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
import re
import unicodedata

_SHA = re.compile(r"sha256:([0-9a-f]{64})\Z")


def validate_relative_resource_path_v4(value: str, label: str) -> None:
    """Require one canonical, platform-neutral relative resource path."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value != unicodedata.normalize("NFC", value)
        or "\\" in value
        or ":" in value
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{label} path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or str(path) != value
    ):
        raise ValueError(f"{label} path is not a canonical safe relative path")


def staged_snapshot_relpaths(*, attempt_id: str, fence_identity: str, source_pdf_sha256: str) -> dict[str, str]:
    source = _sha_hex(source_pdf_sha256, "source PDF")
    token = _digest(attempt_id, fence_identity, source)
    return {
        "snapshot": f"spool/.upload-{token}.pdf",
        "snapshot_part": f"spool/.upload-{token}.pdf.part",
        "snapshot_part_owner": f"spool/.upload-{token}.pdf.part.owner.json",
        "snapshot_lock": f"spool/.upload-{token}.lock",
    }


def staged_retained_relpaths(
    *, attempt_id: str, fence_identity: str, artifact_owner_identity: str,
    artifact_sha256: str,
) -> dict[str, str]:
    artifact = _sha_hex(artifact_sha256, "retained artifact")
    token = _digest(attempt_id, fence_identity, artifact_owner_identity, artifact)
    root = f"spool/.retained-{token}.zip"
    return {
        "spool": root,
        "spool_part": f"{root}.part",
        "spool_part_owner": f"{root}.part.owner.json",
        "spool_lock": f"{root}.lock",
    }


def staged_materialization_relpaths(
    *, output_dir_name: str, attempt_id: str, fence_identity: str,
    artifact_sha256: str,
) -> dict[str, str]:
    output_dir_name = _path_component(output_dir_name, "materialization output")
    artifact = _sha_hex(artifact_sha256, "materialization artifact")
    token = _digest(output_dir_name, attempt_id, fence_identity, artifact)
    staging = f"materialization/.{output_dir_name}.materializing-{token}"
    return {
        "staging": staging,
        "staging_marker": f"{staging}/.agent-materialization-inflight.v1.json",
        "staging_lock": f"spool/.materialization-locks/.{output_dir_name}.lock",
        "output": f"materialization/{output_dir_name}",
    }


def _digest(*values: str) -> str:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("staged resource path identity is empty")
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def _sha_hex(value: str, label: str) -> str:
    match = _SHA.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"{label} hash is not canonical")
    return match.group(1)


def _path_component(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value in {".", ".."}
        or value != unicodedata.normalize("NFC", value)
        or "/" in value
        or "\\" in value
        or ":" in value
        or len(value.encode("utf-8")) > 255
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"{label} path component is unsafe")
    return value


__all__ = [
    "staged_materialization_relpaths",
    "staged_retained_relpaths",
    "staged_snapshot_relpaths",
    "validate_relative_resource_path_v4",
]
