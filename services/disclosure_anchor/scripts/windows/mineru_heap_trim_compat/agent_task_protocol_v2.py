"""Durable state machine for the sole MinerU staged-task protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from threading import RLock
from typing import Any, Literal

TaskState = Literal[
    "pending", "processing", "finalizing", "completed", "failed",
    "cleanup_pending", "consumed"
]
CleanupKind = Literal["result", "task_tree"]
_MAX_REGISTRY_BYTES = 16 * 1024 * 1024
_MAX_RECORDS = 128
_MAX_TOMBSTONES = 8192
_MAX_TASK_PAYLOAD_BYTES = 64 * 1024
_MAX_CLOCK_SKEW_SECONDS = 300


class TaskProtocolConflict(RuntimeError):
    pass


@dataclass(slots=True)
class DurableTaskRecord:
    idempotency_key: str
    task_id: str
    attempt_identity: str
    fence_identity: str
    state: TaskState = "pending"
    result_path: str | None = None
    result_sha256: str | None = None
    result_bytes: int | None = None
    result_owner: str | None = None
    lease_until_unix: float | None = None
    active_readers: int = 0
    error: str | None = None
    task_payload: dict[str, Any] | None = None
    reserved_result_bytes: int = 0
    recovery_generation: int = 1
    consumed_at_unix: float | None = None
    cleanup_kind: CleanupKind | None = None

    def __post_init__(self) -> None:
        for value in (
            self.idempotency_key,
            self.task_id,
            self.attempt_identity,
            self.fence_identity,
        ):
            if not isinstance(value, str) or not value or len(value) > 256:
                raise TaskProtocolConflict("task registry identity is invalid")
        if (
            self.task_id in {".", ".."}
            or "/" in self.task_id
            or "\\" in self.task_id
        ):
            raise TaskProtocolConflict("task id is not one safe path component")
        if (
            not isinstance(self.active_readers, int)
            or isinstance(self.active_readers, bool)
            or self.active_readers < 0
        ):
            raise TaskProtocolConflict("task registry reader count is invalid")
        if (
            not isinstance(self.reserved_result_bytes, int)
            or isinstance(self.reserved_result_bytes, bool)
            or self.reserved_result_bytes < 0
        ):
            raise TaskProtocolConflict("task registry reservation is invalid")
        if self.result_bytes is not None and (
            not isinstance(self.result_bytes, int)
            or isinstance(self.result_bytes, bool)
            or self.result_bytes < 1
        ):
            raise TaskProtocolConflict("task registry result bytes are invalid")
        if (
            not isinstance(self.recovery_generation, int)
            or isinstance(self.recovery_generation, bool)
            or self.recovery_generation < 1
        ):
            raise TaskProtocolConflict("task recovery generation is invalid")
        if self.lease_until_unix is not None and (
            isinstance(self.lease_until_unix, bool)
            or not isinstance(self.lease_until_unix, (int, float))
        ):
            raise TaskProtocolConflict("task registry lease is invalid")
        if self.consumed_at_unix is not None and (
            isinstance(self.consumed_at_unix, bool)
            or not isinstance(self.consumed_at_unix, (int, float))
        ):
            raise TaskProtocolConflict("task registry consumed time is invalid")
        if (self.state == "consumed") != (self.consumed_at_unix is not None):
            raise TaskProtocolConflict("task registry consumed lifecycle is invalid")
        if self.error is not None and not isinstance(self.error, str):
            raise TaskProtocolConflict("task registry error is invalid")
        if self.task_payload is not None and not isinstance(self.task_payload, dict):
            raise TaskProtocolConflict("task registry payload is invalid")
        identities = (self.result_sha256, self.result_owner)
        if any(value is not None for value in identities) and not all(
            isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
            for value in identities
        ):
            raise TaskProtocolConflict("task registry result identity is invalid")
        has_result_identity = all(
            value is not None
            for value in (
                self.result_sha256,
                self.result_bytes,
                self.result_owner,
            )
        )
        if self.state == "completed" and not has_result_identity:
            raise TaskProtocolConflict("terminal task result identity is incomplete")
        if self.state == "cleanup_pending" and self.cleanup_kind not in {
            "result", "task_tree"
        }:
            raise TaskProtocolConflict("cleanup intent kind is absent")
        if self.state != "cleanup_pending" and self.cleanup_kind is not None:
            raise TaskProtocolConflict("cleanup intent escaped pending state")
        if self.cleanup_kind == "result" and not has_result_identity:
            raise TaskProtocolConflict("result cleanup identity is incomplete")
        if self.cleanup_kind == "task_tree" and (
            has_result_identity or self.result_path is not None
        ):
            raise TaskProtocolConflict("task-tree cleanup carried result identity")
        if self.state == "consumed" and any(value is not None for value in identities) != has_result_identity:
            raise TaskProtocolConflict("consumed result identity is incomplete")
        if (
            self.state == "completed" or self.cleanup_kind == "result"
        ) and not isinstance(self.result_path, str):
            raise TaskProtocolConflict("completed task result path is absent")
        if self.state not in {"completed", "cleanup_pending", "consumed"} and any(
            value is not None
            for value in (
                self.result_path,
                self.result_sha256,
                self.result_bytes,
                self.result_owner,
            )
        ):
            raise TaskProtocolConflict("non-result task contains result identity")


class DurableTaskRegistry:
    """Atomic registry with reconcile, leases, ACK and reader-safe cleanup."""

    def __init__(
        self,
        path: Path,
        *,
        max_unacked_result_bytes: int,
        output_root: Path | None = None,
        tombstone_retention_seconds: int = 86400,
        enforce_key_lifecycle: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_unacked_result_bytes < 1:
            raise ValueError("unacked result byte limit must be positive")
        if not 3600 <= tombstone_retention_seconds <= 30 * 86400:
            raise ValueError("tombstone retention must be between one hour and 30 days")
        self._path = path
        self._limit = max_unacked_result_bytes
        self._clock = clock
        self._retention = tombstone_retention_seconds
        self._enforce_key_lifecycle = enforce_key_lifecycle
        self._output_root = (output_root or path.parent).resolve()
        root_fd = os.open(
            self._output_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            root_meta = os.fstat(root_fd)
            if not stat.S_ISDIR(root_meta.st_mode) or root_meta.st_uid != os.getuid():
                raise TaskProtocolConflict("configured output root is unsafe")
            self._output_root_identity = (
                root_meta.st_dev, root_meta.st_ino, root_meta.st_uid, root_meta.st_mode
            )
        finally:
            os.close(root_fd)
        self._lock = RLock()
        self._submission_watermark_bucket = -1
        self._records = self._load()

    def reconcile_or_create(
        self,
        *,
        idempotency_key: str,
        task_id: str,
        attempt_identity: str,
        fence_identity: str,
    ) -> tuple[DurableTaskRecord, bool]:
        values = (idempotency_key, task_id, attempt_identity, fence_identity)
        if not all(value.strip() for value in values):
            raise ValueError("task protocol identities must be non-empty")
        with self._lock:
            observed_server_epoch = self._validate_key_lifecycle(idempotency_key)
            proposed_records = self._records_without_expired_tombstones()
            proposed_watermark = self._submission_watermark_bucket
            if observed_server_epoch is not None:
                proposed_watermark = max(proposed_watermark, observed_server_epoch)
            existing = proposed_records.get(idempotency_key)
            if existing is not None:
                if (
                    existing.attempt_identity != attempt_identity
                    or existing.fence_identity != fence_identity
                ):
                    raise TaskProtocolConflict(
                        "idempotency key was reused with different attempt/fence"
                    )
                if (
                    proposed_records != self._records
                    or proposed_watermark != self._submission_watermark_bucket
                ):
                    self._commit_registry_transition(
                        proposed_records, proposed_watermark
                    )
                return existing, False
            if sum(record.state != "consumed" for record in proposed_records.values()) >= _MAX_RECORDS:
                raise TaskProtocolConflict("active task registry capacity exhausted")
            if sum(record.state == "consumed" for record in proposed_records.values()) >= _MAX_TOMBSTONES:
                raise TaskProtocolConflict("task tombstone retention capacity exhausted")
            record = DurableTaskRecord(
                idempotency_key=idempotency_key,
                task_id=task_id,
                attempt_identity=attempt_identity,
                fence_identity=fence_identity,
            )
            proposed_records[idempotency_key] = record
            self._commit_registry_transition(proposed_records, proposed_watermark)
            return record, True

    def get(self, idempotency_key: str) -> DurableTaskRecord | None:
        with self._lock:
            return self._records.get(idempotency_key)

    def get_by_task_id(self, task_id: str) -> DurableTaskRecord | None:
        with self._lock:
            matches = [
                record for record in self._records.values() if record.task_id == task_id
            ]
            if len(matches) > 1:
                raise TaskProtocolConflict("task id is not unique")
            return matches[0] if matches else None

    def bind_task_payload(
        self,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> None:
        # Round-trip now so restart cannot discover a non-JSON task payload.
        normalized = json.loads(json.dumps(payload, sort_keys=True))
        if not isinstance(normalized, dict):
            raise TypeError("task payload must be one JSON object")
        with self._lock:
            record = self._required(idempotency_key)
            if normalized.get("task_id") != record.task_id:
                raise TaskProtocolConflict("task payload identity drifted")
            output_value = normalized.get("output_dir")
            uploads_value = normalized.get("uploads")
            if not isinstance(output_value, str) or not isinstance(uploads_value, list):
                raise TaskProtocolConflict("task payload ownership paths are absent")
            output = Path(output_value)
            if output.parent.resolve() != self._output_root:
                raise TaskProtocolConflict("task output root escaped configured parent")
            root_fd, task_fd = self._open_task_dir(record.task_id)
            output_meta = os.fstat(task_fd)
            if (
                output.name != record.task_id
                or not stat.S_ISDIR(output_meta.st_mode)
                or output_meta.st_uid != os.getuid()
            ):
                os.close(task_fd)
                os.close(root_fd)
                raise TaskProtocolConflict("task output root identity is unsafe")
            upload_root = output / "uploads"
            try:
                if set(os.listdir(task_fd)) != {"uploads"}:
                    raise TaskProtocolConflict(
                        "task root contained data before generation ownership began"
                    )
                upload_fd = os.open(
                    "uploads", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0), dir_fd=task_fd,
                )
                try:
                    upload_meta = os.fstat(upload_fd)
                    upload_identities = [
                        self._stable_file_identity_at(
                            upload_fd, Path(value), expected_parent=upload_root
                        )
                        for value in uploads_value if isinstance(value, str)
                    ]
                finally:
                    os.close(upload_fd)
            finally:
                os.close(task_fd)
                os.close(root_fd)
            if len(upload_identities) != len(uploads_value):
                raise TaskProtocolConflict("task upload paths are invalid")
            normalized["_agent_protocol"] = {
                "schema": "mineru-task-payload-owner.v1",
                "task_root": str(output.resolve()),
                "task_root_identity": self._directory_identity(output_meta),
                "uploads_root_identity": self._directory_identity(upload_meta),
                "generation": record.recovery_generation,
                "uploads": upload_identities,
            }
            if len(json.dumps(normalized, sort_keys=True).encode()) > _MAX_TASK_PAYLOAD_BYTES:
                raise TaskProtocolConflict("task payload exceeds the closed envelope")
            if record.task_payload is not None and record.task_payload != normalized:
                raise TaskProtocolConflict("task payload drifted after allocation")
            record.task_payload = normalized
            self._persist()

    def recoverable_payloads(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            recoverable = []
            abandoned = [
                key
                for key, record in self._records.items()
                if record.state == "pending" and record.task_payload is None
            ]
            for key in abandoned:
                del self._records[key]
            for record in self._records.values():
                if record.state in {"pending", "processing", "finalizing"}:
                    # A partially allocated request without a bound upload/output
                    # description cannot be safely replayed.
                    if record.task_payload is None:
                        raise TaskProtocolConflict(
                            "nonterminal task has no durable replay payload"
                        )
                    if record.state in {"processing", "finalizing"}:
                        record.state = "pending"
                        record.reserved_result_bytes = 0
                        record.recovery_generation += 1
                        self._prepare_clean_replay(record)
                if record.task_payload is not None and record.state != "consumed":
                    recovered = dict(record.task_payload)
                    recovered.pop("_agent_protocol", None)
                    recovered["status"] = (
                        "pending"
                        if record.state in {"pending", "processing", "finalizing"}
                        else record.state
                    )
                    recovered["result_artifact_path"] = record.result_path
                    recovered["result_artifact_sha256"] = record.result_sha256
                    recovered["result_artifact_bytes"] = record.result_bytes
                    recovered["result_artifact_owner"] = record.result_owner
                    recovered["error"] = record.error
                    recoverable.append(recovered)
            self._persist()
            return tuple(recoverable)

    def _prepare_clean_replay(self, record: DurableTaskRecord) -> None:
        payload = record.task_payload or {}
        output_value = payload.get("output_dir")
        uploads_value = payload.get("uploads")
        if not isinstance(output_value, str) or not isinstance(uploads_value, list):
            raise TaskProtocolConflict("restart replay paths are absent")
        protocol = payload.get("_agent_protocol")
        if not isinstance(protocol, dict) or protocol.get("schema") != "mineru-task-payload-owner.v1":
            raise TaskProtocolConflict("restart task ownership receipt is absent")
        output = Path(output_value)
        if output.parent.resolve() != self._output_root:
            raise TaskProtocolConflict("restart task root escaped configured parent")
        root_fd, task_fd = self._open_task_dir(record.task_id)
        output_meta = os.fstat(task_fd)
        if (
            output.name != record.task_id
            or str(output.resolve()) != protocol.get("task_root")
            or not stat.S_ISDIR(output_meta.st_mode)
            or output_meta.st_uid != os.getuid()
        ):
            raise TaskProtocolConflict("restart task root identity drifted")
        upload_root = output / "uploads"
        expected_uploads = protocol.get("uploads")
        if not isinstance(expected_uploads, list) or len(expected_uploads) != len(uploads_value):
            raise TaskProtocolConflict("restart upload receipt drifted")
        try:
            upload_fd = os.open(
                "uploads", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0), dir_fd=task_fd,
            )
            try:
                if self._directory_identity(output_meta) != protocol.get(
                    "task_root_identity"
                ) or self._directory_identity(os.fstat(upload_fd)) != protocol.get(
                    "uploads_root_identity"
                ):
                    raise TaskProtocolConflict("restart task directory identity drifted")
                observed = [
                    self._stable_file_identity_at(
                        upload_fd, Path(value), expected_parent=upload_root
                    ) for value in uploads_value if isinstance(value, str)
                ]
            finally:
                os.close(upload_fd)
            if observed != expected_uploads:
                raise TaskProtocolConflict("restart upload snapshot identity drifted")
            for child in os.listdir(task_fd):
                if child == "uploads":
                    continue
                self._remove_at(task_fd, child)
            protocol["generation"] = record.recovery_generation
        finally:
            os.close(task_fd)
            os.close(root_fd)

    @staticmethod
    def _directory_identity(metadata: os.stat_result) -> dict[str, int]:
        return {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "uid": metadata.st_uid,
            "mode": metadata.st_mode,
        }

    @staticmethod
    def _stable_file_identity_at(
        parent_fd: int, path: Path, *, expected_parent: Path
    ) -> dict[str, Any]:
        if path.parent.resolve() != expected_parent.resolve() or path.name in {"", ".", ".."}:
            raise TaskProtocolConflict("task upload escaped configured task root")
        descriptor = os.open(
            path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd
        )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or path.resolve().parent != expected_parent.resolve()
            ):
                raise TaskProtocolConflict("task upload snapshot identity is unsafe")
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
                total += len(chunk)
            after = os.fstat(descriptor)
            def identity(value: os.stat_result) -> tuple[int, ...]:
                return (
                    value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                    value.st_nlink, value.st_size, value.st_mtime_ns,
                    value.st_ctime_ns,
                )
            if total != before.st_size or identity(before) != identity(after):
                raise TaskProtocolConflict("task upload changed while hashing")
            return {"path": str(path.resolve()), "bytes": total, "sha256": digest.hexdigest()}
        finally:
            os.close(descriptor)

    def _open_task_dir(self, task_id: str) -> tuple[int, int]:
        root_fd = os.open(
            self._output_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        root_meta = os.fstat(root_fd)
        if (
            root_meta.st_dev, root_meta.st_ino, root_meta.st_uid, root_meta.st_mode
        ) != self._output_root_identity:
            os.close(root_fd)
            raise TaskProtocolConflict("configured output root identity drifted")
        try:
            task_fd = os.open(
                task_id,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd,
            )
        except BaseException:
            os.close(root_fd)
            raise
        return root_fd, task_fd

    @classmethod
    def _remove_at(cls, parent_fd: int, name: str) -> None:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise TaskProtocolConflict("restart cleanup target is a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(
                name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd,
            )
            try:
                for child in os.listdir(child_fd):
                    cls._remove_at(child_fd, child)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)

    def _validate_key_lifecycle(self, key: str) -> int | None:
        if not self._enforce_key_lifecycle:
            return None
        try:
            bucket_text, digest = key.split(".", 1)
            bucket = int(bucket_text, 16)
        except (ValueError, TypeError) as exc:
            raise TaskProtocolConflict("idempotency key lifecycle is invalid") from exc
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise TaskProtocolConflict("idempotency key digest is invalid")
        now = self._clock()
        oldest = int(now - self._retention)
        if bucket < oldest or bucket > int(now + _MAX_CLOCK_SKEW_SECONDS):
            raise TaskProtocolConflict("idempotency key lifecycle expired")
        if int(now) < self._submission_watermark_bucket - _MAX_CLOCK_SKEW_SECONDS:
            raise TaskProtocolConflict("server clock rolled back behind watermark")
        return int(now)

    def _records_without_expired_tombstones(
        self,
    ) -> dict[str, DurableTaskRecord]:
        proposed = dict(self._records)
        if not self._enforce_key_lifecycle:
            return proposed
        cutoff = self._clock() - self._retention
        expired = [
            key for key, record in proposed.items()
            if record.state == "consumed"
            and record.consumed_at_unix is not None
            and record.consumed_at_unix < cutoff
        ]
        for key in expired:
            del proposed[key]
        return proposed

    def _commit_registry_transition(
        self,
        proposed_records: dict[str, DurableTaskRecord],
        proposed_watermark: int,
    ) -> None:
        previous_records = self._records
        previous_watermark = self._submission_watermark_bucket
        self._records = proposed_records
        self._submission_watermark_bucket = proposed_watermark
        try:
            self._persist()
        except BaseException:
            self._records = previous_records
            self._submission_watermark_bucket = previous_watermark
            raise

    def abandon_unbound(self, idempotency_key: str) -> None:
        """Remove only a reservation that never acquired durable task ownership."""
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "pending" or record.task_payload is not None:
                raise TaskProtocolConflict(
                    "only an unbound pending task may be abandoned"
                )
            del self._records[idempotency_key]
            self._persist()

    def fail(self, idempotency_key: str, *, error: str) -> None:
        if not error.strip():
            raise ValueError("task failure must be visible")
        with self._lock:
            record = self._required(idempotency_key)
            if record.state not in {"pending", "processing", "finalizing"}:
                raise TaskProtocolConflict("terminal task cannot fail again")
            record.state = "failed"
            record.error = error
            record.reserved_result_bytes = 0
            self._persist()

    def acknowledge_failed(self, idempotency_key: str) -> None:
        """Compact one observed failed terminal without losing idempotency history."""
        with self._lock:
            record = self._required(idempotency_key)
            if record.state == "consumed":
                return
            if record.state != "failed":
                raise TaskProtocolConflict("only failed tasks can use failed ACK")
            record.state = "cleanup_pending"
            record.cleanup_kind = "task_tree"
            try:
                self._persist()
            except BaseException:
                record.state = "failed"
                record.cleanup_kind = None
                raise
            self.cleanup_consumed()

    def transition(self, idempotency_key: str, target: TaskState) -> None:
        allowed: dict[TaskState, frozenset[TaskState]] = {
            "pending": frozenset({"processing", "failed"}),
            "processing": frozenset({"finalizing", "failed"}),
            "finalizing": frozenset({"completed", "failed"}),
            "completed": frozenset({"cleanup_pending"}),
            "failed": frozenset(),
            "cleanup_pending": frozenset({"consumed"}),
            "consumed": frozenset(),
        }
        with self._lock:
            record = self._required(idempotency_key)
            if target not in allowed[record.state]:
                raise TaskProtocolConflict(
                    f"invalid task transition {record.state}->{target}"
                )
            record.state = target
            if target == "failed":
                record.reserved_result_bytes = 0
            self._persist()

    def complete(
        self,
        idempotency_key: str,
        *,
        result_path: Path,
        result_sha256: str,
        result_bytes: int,
        result_owner: str,
    ) -> None:
        if (
            isinstance(result_bytes, bool)
            or not isinstance(result_bytes, int)
            or result_bytes < 1
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
                for value in (result_sha256, result_owner)
            )
        ):
            raise ValueError("result identity is invalid")
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "finalizing":
                raise TaskProtocolConflict("only finalizing tasks may complete")
            if result_bytes > record.reserved_result_bytes:
                raise TaskProtocolConflict("result exceeded its reserved byte envelope")
            record.result_path = str(result_path)
            record.result_sha256 = result_sha256
            record.result_bytes = result_bytes
            record.result_owner = result_owner
            record.state = "completed"
            record.reserved_result_bytes = 0
            self._persist()

    def reserve_finalizer(self, idempotency_key: str, *, byte_budget: int) -> None:
        if (
            isinstance(byte_budget, bool)
            or not isinstance(byte_budget, int)
            or byte_budget < 1
        ):
            raise ValueError("finalizer byte reservation must be a positive integer")
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "finalizing" or record.reserved_result_bytes:
                raise TaskProtocolConflict("finalizer reservation state is invalid")
            if (
                self.unacked_result_bytes + self.reserved_result_bytes + byte_budget
                > self._limit
            ):
                raise TaskProtocolConflict("unacked result byte capacity exhausted")
            record.reserved_result_bytes = byte_budget
            self._persist()

    @property
    def reserved_result_bytes(self) -> int:
        return sum(record.reserved_result_bytes for record in self._records.values())

    @property
    def unacked_result_bytes(self) -> int:
        return sum(
            record.result_bytes or 0
            for record in self._records.values()
            if record.state in {"completed", "cleanup_pending", "consumed"} and record.result_path
        )

    def lease(self, idempotency_key: str, *, seconds: float) -> float:
        if seconds <= 0:
            raise ValueError("lease duration must be positive")
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "completed":
                raise TaskProtocolConflict("only completed results can be leased")
            record.lease_until_unix = time.time() + seconds
            self._persist()
            return record.lease_until_unix

    @contextmanager
    def open_result(self, idempotency_key: str) -> Iterator[Path]:
        path = self.acquire_result(idempotency_key)
        try:
            yield path
        finally:
            self.release_result(idempotency_key)

    def acquire_result(self, idempotency_key: str) -> Path:
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "completed" or not record.result_path:
                raise TaskProtocolConflict("result is unavailable")
            if not record.lease_until_unix or record.lease_until_unix <= time.time():
                raise TaskProtocolConflict("result lease is absent or expired")
            record.active_readers += 1
            self._persist()
            return Path(record.result_path)

    def release_result(self, idempotency_key: str) -> None:
        with self._lock:
            current = self._required(idempotency_key)
            current.active_readers -= 1
            if current.active_readers < 0:
                raise RuntimeError("result reader count underflowed")
            self._persist()

    def acknowledge(self, idempotency_key: str) -> None:
        with self._lock:
            record = self._required(idempotency_key)
            if record.state in {"cleanup_pending", "consumed"}:
                return
            if record.state != "completed" or record.active_readers:
                raise TaskProtocolConflict(
                    "result cannot be ACKed while unavailable/in use"
                )
            record.state = "cleanup_pending"
            record.cleanup_kind = "result"
            try:
                self._persist()
            except BaseException:
                record.state = "completed"
                record.cleanup_kind = None
                raise

    def cleanup_consumed(
        self, unlink: Callable[[Path], None] | None = None
    ) -> int:
        with self._lock:
            removable = [
                key
                for key, record in self._records.items()
                if record.state == "cleanup_pending" and record.active_readers == 0
            ]
            cleaned = 0
            for key in removable:
                record = self._records[key]
                previous = (
                    record.result_path,
                    record.task_payload,
                    record.lease_until_unix,
                    record.error,
                    record.reserved_result_bytes,
                    record.state,
                    record.consumed_at_unix,
                    record.cleanup_kind,
                )
                self._unlink_owned_result(record, before_unlink=unlink)
                record.result_path = None
                record.task_payload = None
                record.lease_until_unix = None
                record.error = None
                record.reserved_result_bytes = 0
                record.state = "consumed"
                record.consumed_at_unix = self._clock()
                record.cleanup_kind = None
                try:
                    self._persist()
                except BaseException:
                    (
                        record.result_path,
                        record.task_payload,
                        record.lease_until_unix,
                        record.error,
                        record.reserved_result_bytes,
                        record.state,
                        record.consumed_at_unix,
                        record.cleanup_kind,
                    ) = previous
                    raise
                cleaned += 1
            return cleaned

    def _unlink_owned_result(
        self,
        record: DurableTaskRecord,
        *,
        before_unlink: Callable[[Path], None] | None,
    ) -> None:
        payload = record.task_payload or {}
        protocol = payload.get("_agent_protocol")
        output_value = payload.get("output_dir")
        if not isinstance(protocol, dict) or not isinstance(output_value, str):
            if record.task_payload is None and record.result_path is None:
                return
            if not self._enforce_key_lifecycle:
                if record.result_path:
                    if before_unlink is None:
                        Path(record.result_path).unlink(missing_ok=True)
                    else:
                        try:
                            before_unlink(Path(record.result_path))
                        except FileNotFoundError:
                            pass
                return
            raise TaskProtocolConflict("cleanup task ownership receipt is absent")
        output = Path(output_value)
        result = Path(record.result_path or "")
        if record.cleanup_kind == "result" and (
            result.parent.resolve() != output.resolve()
            or result.name in {"", ".", ".."}
        ):
            raise TaskProtocolConflict("cleanup result escaped task root")
        try:
            root_fd, task_fd = self._open_task_dir(record.task_id)
        except FileNotFoundError:
            root_fd = os.open(
                self._output_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                root_meta = os.fstat(root_fd)
                if (
                    root_meta.st_dev,
                    root_meta.st_ino,
                    root_meta.st_uid,
                    root_meta.st_mode,
                ) != self._output_root_identity:
                    raise TaskProtocolConflict("configured output root identity drifted")
                expected = protocol.get("task_root_identity")
                if not isinstance(expected, dict):
                    raise TaskProtocolConflict("cleanup task identity is absent")
                entries = os.listdir(root_fd)
                if len(entries) > _MAX_RECORDS + _MAX_TOMBSTONES:
                    raise TaskProtocolConflict("cleanup output-root scan exceeded bound")
                for entry in entries:
                    metadata = os.stat(
                        entry, dir_fd=root_fd, follow_symlinks=False
                    )
                    if (
                        metadata.st_dev == expected.get("device")
                        and metadata.st_ino == expected.get("inode")
                    ):
                        raise TaskProtocolConflict(
                            "owned task directory was renamed during cleanup"
                        )
            finally:
                os.close(root_fd)
            return
        try:
            if self._directory_identity(os.fstat(task_fd)) != protocol.get(
                "task_root_identity"
            ):
                raise TaskProtocolConflict("cleanup task directory identity drifted")
            upload_fd = os.open(
                "uploads",
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=task_fd,
            )
            try:
                if self._directory_identity(os.fstat(upload_fd)) != protocol.get(
                    "uploads_root_identity"
                ):
                    raise TaskProtocolConflict(
                        "cleanup uploads directory identity drifted"
                    )
            finally:
                os.close(upload_fd)
            if record.cleanup_kind == "result":
                result_metadata: os.stat_result | None
                try:
                    result_metadata = os.stat(
                        result.name, dir_fd=task_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    result_metadata = None
                if result_metadata is not None and (
                    not stat.S_ISREG(result_metadata.st_mode)
                    or result_metadata.st_nlink != 1
                ):
                    raise TaskProtocolConflict("cleanup result identity is unsafe")
                if before_unlink is not None and result_metadata is not None:
                    before_unlink(result)
            for child in os.listdir(task_fd):
                self._remove_at(task_fd, child)
            os.close(task_fd)
            task_fd = -1
            os.rmdir(record.task_id, dir_fd=root_fd)
        finally:
            if task_fd >= 0:
                os.close(task_fd)
            os.close(root_fd)

    def _required(self, key: str) -> DurableTaskRecord:
        try:
            return self._records[key]
        except KeyError as exc:
            raise TaskProtocolConflict("task is unknown") from exc

    def _load(self) -> dict[str, DurableTaskRecord]:
        try:
            descriptor = os.open(
                self._path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        except FileNotFoundError:
            return {}
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or not 0 < metadata.st_size <= _MAX_REGISTRY_BYTES
            ):
                raise TaskProtocolConflict("task registry file identity is unsafe")
            chunks = []
            remaining = _MAX_REGISTRY_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
            def identity(value: os.stat_result) -> tuple[int, ...]:
                return (
                    value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                    value.st_nlink, value.st_size, value.st_mtime_ns,
                    value.st_ctime_ns,
                )
            if len(raw) != metadata.st_size or identity(metadata) != identity(after):
                raise TaskProtocolConflict("task registry changed while reading")
        finally:
            os.close(descriptor)

        def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise TaskProtocolConflict(
                        "task registry contains duplicate fields"
                    )
                value[key] = item
            return value

        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=closed_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                TaskProtocolConflict(f"non-finite registry value: {value}")
            ),
        )
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema", "output_root", "submission_watermark_bucket", "records"}
            or payload.get("schema") != "mineru-task-registry.v2"
        ):
            raise TaskProtocolConflict("task registry schema is invalid")
        expected_root = {
            "path": str(self._output_root),
            "device": self._output_root_identity[0],
            "inode": self._output_root_identity[1],
            "uid": self._output_root_identity[2],
            "mode": self._output_root_identity[3],
        }
        if payload.get("output_root") != expected_root:
            raise TaskProtocolConflict("configured output root identity drifted")
        watermark = payload.get("submission_watermark_bucket")
        if not isinstance(watermark, int) or isinstance(watermark, bool) or watermark < -1:
            raise TaskProtocolConflict("submission watermark is invalid")
        self._submission_watermark_bucket = watermark
        records = payload.get("records")
        if not isinstance(records, list) or len(records) > _MAX_RECORDS + _MAX_TOMBSTONES:
            raise TaskProtocolConflict("task registry records are invalid")
        expected = {item.name for item in fields(DurableTaskRecord)}
        if any(not isinstance(item, dict) or set(item) != expected for item in records):
            raise TaskProtocolConflict("task registry record fields are not closed")
        loaded = {}
        task_ids = set()
        for item in records:
            record = DurableTaskRecord(**item)
            if record.idempotency_key in loaded or record.task_id in task_ids:
                raise TaskProtocolConflict("task registry identities are not unique")
            if record.state not in {
                "pending",
                "processing",
                "finalizing",
                "completed",
                "failed",
                "cleanup_pending",
                "consumed",
            }:
                raise TaskProtocolConflict("task registry state is invalid")
            loaded[record.idempotency_key] = record
            task_ids.add(record.task_id)
        for record in loaded.values():
            record.active_readers = 0
            if record.state in {"completed", "cleanup_pending", "consumed"} and record.result_path:
                result_path = Path(record.result_path or "")
                try:
                    metadata = result_path.lstat()
                except FileNotFoundError:
                    if record.state == "cleanup_pending":
                        continue
                    raise TaskProtocolConflict(
                        "retained result disappeared outside cleanup intent"
                    ) from None
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise TaskProtocolConflict("retained result file identity is unsafe")
                digest = hashlib.sha256()
                total = 0
                with result_path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        total += len(chunk)
                expected_owner = hashlib.sha256(
                    f"{record.task_id}\0{digest.hexdigest()}\0{total}".encode()
                ).hexdigest()
                if (
                    digest.hexdigest() != record.result_sha256
                    or total != record.result_bytes
                    or expected_owner != record.result_owner
                ):
                    raise TaskProtocolConflict("retained result identity drifted")
        return loaded

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "schema": "mineru-task-registry.v2",
                "output_root": {
                    "path": str(self._output_root),
                    "device": self._output_root_identity[0],
                    "inode": self._output_root_identity[1],
                    "uid": self._output_root_identity[2],
                    "mode": self._output_root_identity[3],
                },
                "submission_watermark_bucket": self._submission_watermark_bucket,
                "records": [
                    asdict(record)
                    for record in sorted(
                        self._records.values(), key=lambda item: item.idempotency_key
                    )
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(payload) > _MAX_REGISTRY_BYTES:
            raise TaskProtocolConflict("task registry exceeds the closed envelope")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}-",
            suffix=".tmp",
            dir=self._path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


class SplitTaskExecutor:
    """Separate parse and finalizer credits with explicit state transitions."""

    def __init__(
        self,
        *,
        parse_slots: int,
        finalizer_slots: int,
        result_reservation_bytes: int = 268435456,
    ) -> None:
        if parse_slots < 1 or finalizer_slots < 1:
            raise ValueError("executor slots must be positive")
        self._parse = asyncio.Semaphore(parse_slots)
        self._finalize = asyncio.Semaphore(finalizer_slots)
        self._result_reservation_bytes = result_reservation_bytes

    async def run(
        self,
        *,
        registry: DurableTaskRegistry,
        key: str,
        parse: Callable[[], Awaitable[None]],
        finalize: Callable[[], Awaitable[tuple[Path, str, int, str]]],
    ) -> None:
        registry.transition(key, "processing")
        try:
            async with self._parse:
                await parse()
            registry.transition(key, "finalizing")
            registry.reserve_finalizer(key, byte_budget=self._result_reservation_bytes)
            async with self._finalize:
                path, digest, byte_count, owner = await finalize()
            registry.complete(
                key,
                result_path=path,
                result_sha256=digest,
                result_bytes=byte_count,
                result_owner=owner,
            )
        except BaseException as exc:
            record = registry.get(key)
            if record is not None and record.state in {"processing", "finalizing"}:
                failure = json.dumps(
                    {
                        "code": "parse_or_finalize_failed",
                        "detail": type(exc).__name__[:64],
                        "schema": "mineru-task-failure.v1",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                registry.fail(key, error=failure)
            raise


def evict_consumed_routes(
    registry: DurableTaskRegistry,
    tasks: dict[str, Any],
    task_events: dict[str, Any],
) -> int:
    """Remove only routes whose durable terminal was compacted to consumed."""
    evicted = 0
    for task_id in tuple(tasks):
        record = registry.get_by_task_id(task_id)
        if record is not None and record.state == "consumed":
            tasks.pop(task_id, None)
            task_events.pop(task_id, None)
            evicted += 1
    return evicted


__all__ = [
    "DurableTaskRecord",
    "DurableTaskRegistry",
    "SplitTaskExecutor",
    "TaskProtocolConflict",
    "evict_consumed_routes",
]
