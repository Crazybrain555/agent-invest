"""Content-free canonical relay projection for capacity progress v1."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from disclosure_anchor.application.contracts.synchronized_telemetry import ProgressEvent
from disclosure_anchor.application.services.capacity_progress_replay import replay_capacity_progress


_FORBIDDEN_KEYS = frozenset(
    {
        "company",
        "company_id",
        "security_code",
        "document_id",
        "task_id",
        "url",
        "path",
        "filename",
        "prompt",
        "host_id",
        "hostname",
    }
)
_MAX_RELAY_SOURCES = 8192


class ProgressRelayResume(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    contract_version: Literal["mineru.capacity-progress-relay-resume.v1"] = (
        "mineru.capacity-progress-relay-resume.v1"
    )
    run_id: str
    process_epoch_sha256: str
    runtime_bundle_identity_sha256: str
    process_profile_sha256: str
    clock_domain_identity_sha256: str
    next_sequence: int
    cumulative_unique_source_pages: int
    durable_sources: tuple[tuple[str, str, int], ...]
    previous_checkpoint_sha256: str | None = None

    @model_validator(mode="after")
    def _closed_resume(self) -> "ProgressRelayResume":
        if self.next_sequence < 0 or self.cumulative_unique_source_pages < 0:
            raise ValueError("progress resume counters are negative")
        for name in (
            "process_epoch_sha256",
            "runtime_bundle_identity_sha256",
            "process_profile_sha256",
            "clock_domain_identity_sha256",
        ):
            value = getattr(self, name)
            if len(value) != 71 or not value.startswith("sha256:") or any(character not in "0123456789abcdef" for character in value[7:]):
                raise ValueError(f"{name} is invalid")
        if self.previous_checkpoint_sha256 is not None:
            value = self.previous_checkpoint_sha256
            if len(value) != 71 or not value.startswith("sha256:") or any(
                character not in "0123456789abcdef" for character in value[7:]
            ):
                raise ValueError("previous checkpoint hash is invalid")
        for source, profile, pages in self.durable_sources:
            if (
                len(source) != 71
                or not source.startswith("sha256:")
                or any(character not in "0123456789abcdef" for character in source[7:])
                or isinstance(pages, bool)
                or pages < 1
            ):
                raise ValueError("durable relay source evidence is invalid")
            if profile != self.process_profile_sha256:
                raise ValueError("durable relay source profile drifted")
        return self


class ContentFreeProgressSnapshot(BaseModel):
    """A closed UI/agent projection with counts only, never work identities."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    contract_version: Literal["mineru.content-free-progress-snapshot.v1"] = (
        "mineru.content-free-progress-snapshot.v1"
    )
    observed_at_utc: datetime
    process_epoch_sha256: str
    process_profile_sha256: str
    clock_domain_identity_sha256: str
    queue_documents: int = Field(ge=0)
    queue_estimated_pages: int = Field(ge=0)
    active_remote_owners: int = Field(ge=0)
    active_finalize_owners: int = Field(ge=0)
    pending_finalize_documents: int = Field(ge=0)
    pending_finalize_bytes: int = Field(ge=0)
    credits_in_use: int = Field(ge=0)
    credits_available: int = Field(ge=0)

    @model_validator(mode="after")
    def _closed(self) -> "ContentFreeProgressSnapshot":
        if self.observed_at_utc.tzinfo is None or self.observed_at_utc.utcoffset() != timezone.utc.utcoffset(self.observed_at_utc):
            raise ValueError("progress snapshot time must be UTC")
        for name in (
            "process_epoch_sha256",
            "process_profile_sha256",
            "clock_domain_identity_sha256",
        ):
            value = getattr(self, name)
            digest = value.removeprefix("sha256:")
            if not value.startswith("sha256:") or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError(f"{name} is not a canonical SHA-256")
        return self


def encode_content_free_progress_snapshot(snapshot: ContentFreeProgressSnapshot) -> bytes:
    return json.dumps(
        snapshot.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def encode_capacity_progress_jsonl(
    events: tuple[ProgressEvent, ...],
    *,
    runtime_bundle_identity_sha256: str,
    resume: ProgressRelayResume | None = None,
) -> tuple[bytes, ProgressRelayResume]:
    """Encode a closed batch; restart requires explicit prior durable relay state."""

    if not events:
        raise ValueError("capacity progress relay batch is empty")
    first, last = events[0], events[-1]
    if resume is not None:
        if (
            first.run_id != resume.run_id
            or first.process_epoch_sha256 != resume.process_epoch_sha256
            or first.process_profile_sha256 != resume.process_profile_sha256
            or first.clock_domain_identity_sha256
            != resume.clock_domain_identity_sha256
            or runtime_bundle_identity_sha256
            != resume.runtime_bundle_identity_sha256
            or first.sequence != resume.next_sequence
        ):
            raise ValueError("capacity progress resume state does not continue exactly")
        preceding = resume.cumulative_unique_source_pages
        prior_sources = resume.durable_sources
    else:
        if first.sequence != 0:
            raise ValueError("capacity progress restart continuity is unproven")
        preceding = 0
        prior_sources = ()
    replay = replay_capacity_progress(
        events,
        initial_cumulative_unique_pages=preceding,
        prior_durable_sources=prior_sources,
    )
    if len(replay.durable_sources) > _MAX_RELAY_SOURCES:
        raise ValueError("capacity progress relay source bound exceeded")
    dumped = [event.model_dump(mode="json") for event in events]
    for value in dumped:
        leaked = _FORBIDDEN_KEYS.intersection(value)
        if leaked:
            raise ValueError("capacity progress contains forbidden content keys")
    payload = b"".join(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        for value in dumped
    )
    return payload, ProgressRelayResume(
        run_id=last.run_id,
        process_epoch_sha256=last.process_epoch_sha256,
        runtime_bundle_identity_sha256=runtime_bundle_identity_sha256,
        process_profile_sha256=last.process_profile_sha256,
        clock_domain_identity_sha256=last.clock_domain_identity_sha256,
        next_sequence=last.sequence + 1,
        cumulative_unique_source_pages=replay.durable_unique_pages,
        durable_sources=replay.durable_sources,
        previous_checkpoint_sha256=(
            _resume_hash(resume) if resume is not None else None
        ),
    )


def _resume_bytes(resume: ProgressRelayResume) -> bytes:
    return json.dumps(
        resume.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


def _resume_hash(resume: ProgressRelayResume) -> str:
    return "sha256:" + hashlib.sha256(_resume_bytes(resume)).hexdigest()


def write_progress_relay_checkpoint(path: Path, resume: ProgressRelayResume) -> str:
    """Persist a private cache; an external durable head is required for restart proof."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = _resume_bytes(resume)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return _resume_hash(resume)


def read_progress_relay_checkpoint(path: Path) -> ProgressRelayResume:
    """Read one private checkpoint once and reject link/type/permission drift."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_mode & 0o777 != 0o600:
            raise ValueError("progress relay checkpoint is not a private single-link file")
        if before.st_size > 1_048_576:
            raise ValueError("progress relay checkpoint exceeds its byte bound")
        payload = b""
        while len(payload) <= before.st_size:
            chunk = os.read(descriptor, min(65536, before.st_size + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or len(payload) != before.st_size:
            raise ValueError("progress relay checkpoint changed while read")
        named = os.lstat(path)
        if (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("progress relay checkpoint name changed while read")
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=lambda pairs: _reject_duplicate_pairs(pairs),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("progress relay checkpoint is invalid JSON") from error
    resume = ProgressRelayResume.model_validate(decoded)
    if payload != _resume_bytes(resume):
        raise ValueError("progress relay checkpoint is non-canonical")
    return resume


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key}")
        result[key] = value
    return result


__all__ = [
    "ContentFreeProgressSnapshot",
    "ProgressRelayResume",
    "encode_capacity_progress_jsonl",
    "encode_content_free_progress_snapshot",
    "read_progress_relay_checkpoint",
    "write_progress_relay_checkpoint",
]
