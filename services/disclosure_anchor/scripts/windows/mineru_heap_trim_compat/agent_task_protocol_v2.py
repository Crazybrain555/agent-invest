"""Durable state machine for the sole MinerU staged-task protocol."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from functools import wraps
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


class TaskRegistryPersistenceError(OSError):
    """A content-free registry persistence outcome with an explicit commit boundary."""

    def __init__(
        self,
        *,
        operation: str,
        phase: str,
        outcome: str,
        committed: bool,
    ) -> None:
        self.operation = operation
        self.phase = phase
        self.outcome = outcome
        self.committed = committed
        super().__init__(
            "task registry persistence "
            f"{outcome} during {operation} at {phase}"
        )


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
        self._active_operation = "initial_load"
        self._last_persistence_event: dict[str, Any] | None = None
        self._last_persistence_cause: BaseException | None = None
        self._last_persistence_cleanup_cause: BaseException | None = None
        self._uncertain_records: dict[str, DurableTaskRecord] | None = None
        self._uncertain_watermark_bucket: int | None = None
        self._uncertain_payload: bytes | None = None
        self._persistence_generation = 0
        self._submission_watermark_bucket = -1
        self._records = self._load()
        self._durable_payload = self._read_current_registry_bytes()
        self._last_durable_records = self._clone_records(self._records)
        self._last_durable_watermark_bucket = self._submission_watermark_bucket

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
            self.assert_observation_safe()
            return copy.deepcopy(self._records.get(idempotency_key))



    def get_by_task_id(self, task_id: str) -> DurableTaskRecord | None:
        with self._lock:
            self.assert_observation_safe()
            matches = [
                record for record in self._records.values() if record.task_id == task_id
            ]
            if len(matches) > 1:
                raise TaskProtocolConflict("task id is not unique")
            return copy.deepcopy(matches[0]) if matches else None


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
        """Hydrate routes and durably prepare interrupted work for replay.

        Live reader counts are process-local barriers.  A cold constructor may
        clear persisted counts, but an in-process recovery must never discard a
        handle that is still open.  Interrupted-state changes are committed
        before any filesystem cleanup, so a cleanup failure remains retryable.
        """
        with self._lock:
            if any(record.active_readers for record in self._records.values()):
                raise TaskProtocolConflict(
                    "live result readers prevent in-process task recovery"
                )

            proposed_records = self._clone_records(self._records)
            changed = False
            replay_keys: list[str] = []
            for key, record in list(proposed_records.items()):
                if record.state == "pending" and record.task_payload is None:
                    del proposed_records[key]
                    changed = True
                    continue
                if record.state in {"pending", "processing", "finalizing"}:
                    if record.task_payload is None:
                        raise TaskProtocolConflict(
                            "nonterminal task has no durable replay payload"
                        )
                    if record.state in {"processing", "finalizing"}:
                        record.state = "pending"
                        record.reserved_result_bytes = 0
                        record.recovery_generation += 1
                        record.error = None
                        changed = True
                    ownership = record.task_payload.get("_agent_protocol")
                    if (
                        not isinstance(ownership, dict)
                        or ownership.get("schema")
                        != "mineru-task-payload-owner.v1"
                    ):
                        raise TaskProtocolConflict(
                            "recoverable task payload ownership receipt is invalid"
                        )
                    if ownership.get("generation") != record.recovery_generation:
                        ownership["generation"] = record.recovery_generation
                        changed = True
                    replay_keys.append(key)

            if changed:
                self._commit_registry_transition(
                    proposed_records,
                    self._submission_watermark_bucket,
                )

            # Filesystem replay cleanup is intentionally after the registry
            # transition.  The durable pending state and ownership receipt then
            # make a partial cleanup failure safe to retry on the next call.
            for key in replay_keys:
                current_record = self._records.get(key)
                if (
                    current_record is None
                    or current_record.state != "pending"
                    or current_record.task_payload is None
                ):
                    continue
                self._prepare_clean_replay(copy.deepcopy(current_record))

            hydrated: list[dict[str, Any]] = []
            for record in self._records.values():
                if record.task_payload is None or record.state == "consumed":
                    continue
                recovered = copy.deepcopy(record.task_payload)
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
                hydrated.append(recovered)
            return tuple(hydrated)


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

    @staticmethod
    def _fsync_namespace_directory(fd: int) -> None:
        """Commit directory-entry changes before durable ownership is released."""
        os.fsync(fd)

    @staticmethod
    def _close_namespace_descriptors(
        descriptors: tuple[tuple[int, str], ...],
        primary_error: BaseException | None,
    ) -> None:
        """Close every fd without replacing an earlier delete/fsync failure."""
        close_error: BaseException | None = None
        for descriptor, label in descriptors:
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except BaseException as exc:
                if primary_error is not None:
                    primary_error.add_note(
                        f"{label} close also failed: {type(exc).__name__}"
                    )
                elif close_error is None:
                    close_error = exc
                else:
                    close_error.add_note(
                        f"{label} close also failed: {type(exc).__name__}"
                    )
        if primary_error is None and close_error is not None:
            raise close_error

    @classmethod
    def _sync_namespace_path(cls, directory: Path) -> None:
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        primary_error: BaseException | None = None
        try:
            cls._fsync_namespace_directory(descriptor)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cls._close_namespace_descriptors(
                ((descriptor, "cleanup namespace directory"),),
                primary_error,
            )

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
            primary_error: BaseException | None = None
            try:
                for child in os.listdir(child_fd):
                    cls._remove_at(child_fd, child)
                cls._fsync_namespace_directory(child_fd)
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                cls._close_namespace_descriptors(
                    ((child_fd, "cleanup child directory"),),
                    primary_error,
                )
            os.rmdir(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)
        cls._fsync_namespace_directory(parent_fd)

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
        proposed = self._clone_records(self._records)
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
        with self._lock:
            durable = sum(
                record.reserved_result_bytes for record in self._records.values()
            )
            if self._uncertain_records is None:
                return durable
            candidate = sum(
                record.reserved_result_bytes
                for record in self._uncertain_records.values()
            )
            return max(durable, candidate)



    @property
    def unacked_result_bytes(self) -> int:
        def usage(records: dict[str, DurableTaskRecord]) -> int:
            return sum(
                record.result_bytes or 0
                for record in records.values()
                if record.state in {"completed", "cleanup_pending"}
            )

        with self._lock:
            durable = usage(self._records)
            if self._uncertain_records is None:
                return durable
            return max(durable, usage(self._uncertain_records))


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
        except BaseException as primary_error:
            try:
                self.release_result(idempotency_key)
            except BaseException as release_error:
                primary_error.add_note(
                    "result reader release also failed: "
                    f"{type(release_error).__name__}"
                )
                raise primary_error from release_error
            raise
        else:
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
            if current.active_readers < 1:
                raise RuntimeError("result reader count underflowed")
            current.active_readers -= 1
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
                    result_path = Path(record.result_path)
                    if before_unlink is None:
                        result_path.unlink(missing_ok=True)
                    else:
                        try:
                            before_unlink(result_path)
                        except FileNotFoundError:
                            pass
                    self._sync_namespace_path(result_path.parent)
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
            primary_error: BaseException | None = None
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
                # A prior attempt may have removed the task entry before its
                # output-parent fsync.  Syncing the still-pinned output root
                # makes the observed absence durable before consuming intent.
                self._fsync_namespace_directory(root_fd)
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                self._close_namespace_descriptors(
                    ((root_fd, "cleanup output root"),),
                    primary_error,
                )
            return
        cleanup_error: BaseException | None = None
        try:
            if self._directory_identity(os.fstat(task_fd)) != protocol.get(
                "task_root_identity"
            ):
                raise TaskProtocolConflict("cleanup task directory identity drifted")
            expected_upload_root = protocol.get("uploads_root_identity")
            if not isinstance(expected_upload_root, dict):
                raise TaskProtocolConflict("cleanup uploads identity is absent")
            try:
                upload_fd = os.open(
                    "uploads",
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=task_fd,
                )
            except FileNotFoundError:
                # A prior cleanup attempt may already have removed the owned
                # uploads tree.  Reject a rename of that same inode, but allow
                # exact intent replay to continue with the remaining children.
                entries = os.listdir(task_fd)
                if len(entries) > _MAX_RECORDS + _MAX_TOMBSTONES:
                    raise TaskProtocolConflict(
                        "cleanup task-root scan exceeded bound"
                    )
                for entry in entries:
                    metadata = os.stat(
                        entry, dir_fd=task_fd, follow_symlinks=False
                    )
                    if (
                        metadata.st_dev == expected_upload_root.get("device")
                        and metadata.st_ino == expected_upload_root.get("inode")
                    ):
                        raise TaskProtocolConflict(
                            "owned uploads directory was renamed during cleanup"
                        )
            else:
                upload_error: BaseException | None = None
                try:
                    if self._directory_identity(os.fstat(upload_fd)) != expected_upload_root:
                        raise TaskProtocolConflict(
                            "cleanup uploads directory identity drifted"
                        )
                except BaseException as exc:
                    upload_error = exc
                    raise
                finally:
                    self._close_namespace_descriptors(
                        ((upload_fd, "cleanup uploads directory"),),
                        upload_error,
                    )
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
            self._fsync_namespace_directory(task_fd)
            closing_task_fd = task_fd
            task_fd = -1
            self._close_namespace_descriptors(
                ((closing_task_fd, "cleanup task directory"),),
                None,
            )
            os.rmdir(record.task_id, dir_fd=root_fd)
            self._fsync_namespace_directory(root_fd)
        except BaseException as exc:
            cleanup_error = exc
            raise
        finally:
            self._close_namespace_descriptors(
                (
                    (task_fd, "cleanup task directory"),
                    (root_fd, "cleanup output root"),
                ),
                cleanup_error,
            )

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

        return self._decode_registry(raw)

    def _decode_registry(
        self, raw: bytes, *, require_quiescent: bool = False
    ) -> dict[str, DurableTaskRecord]:
        """Decode without repair when used by a read-only commissioning probe."""
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
        if require_quiescent and (
            not isinstance(payload.get("output_root"), dict)
            or any(type(payload["output_root"].get(key)) is not int
                   for key in ("device", "inode", "uid", "mode"))
            or json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() != raw
        ):
            raise TaskProtocolConflict("quiescent registry is not canonical")
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
            if require_quiescent and (
                record.state != "consumed"
                or record.active_readers != 0
                or record.reserved_result_bytes != 0
                or record.consumed_at_unix is None
                or record.consumed_at_unix < 0
                or any(value is not None for value in (
                    record.result_path, record.task_payload, record.lease_until_unix,
                    record.error, record.cleanup_kind,
                ))
            ):
                raise TaskProtocolConflict("output registry still owns task resources")
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
            if not require_quiescent:
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


    @staticmethod
    def _clone_records(
        records: dict[str, DurableTaskRecord],
    ) -> dict[str, DurableTaskRecord]:
        return copy.deepcopy(records)

    def _restore_last_durable_state(self) -> None:
        self._records = self._clone_records(self._last_durable_records)
        self._submission_watermark_bucket = (
            self._last_durable_watermark_bucket
        )

    def _current_state_differs_from_last_durable(self) -> bool:
        return (
            self._submission_watermark_bucket
            != self._last_durable_watermark_bucket
            or self._records != self._last_durable_records
        )

    def _record_persistence_event(
        self,
        *,
        outcome: str,
        phase: str,
        committed: bool,
        operation: str | None = None,
        cause: BaseException | None = None,
        cleanup_cause: BaseException | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "outcome": outcome,
            "phase": phase,
            "committed": committed,
            "operation": operation or self._active_operation,
        }
        if cause is not None:
            event["cause_type"] = type(cause).__name__
        if cleanup_cause is not None:
            event["cleanup_cause_type"] = type(cleanup_cause).__name__
        self._last_persistence_event = event
        self._last_persistence_cause = cause
        self._last_persistence_cleanup_cause = cleanup_cause

    def persistence_status(self) -> dict[str, Any]:
        with self._lock:
            if self._uncertain_records is not None:
                state = "durability_uncertain"
                event = self._last_persistence_event or {}
                if event.get("operation") in {"acquire_result", "release_result"}:
                    recovery_action = "restart_registry_process"
                else:
                    recovery_action = "call recover_persistence_uncertainty"
            elif self._last_persistence_event is not None:
                state = "degraded"
                event = self._last_persistence_event
                if bool(event.get("committed")):
                    recovery_action = "do_not_retry_committed_operation"
                else:
                    recovery_action = "retry_idempotent_operation"
            else:
                state = "healthy"
                recovery_action = None
            return {
                "state": state,
                "last_event": copy.deepcopy(self._last_persistence_event),
                "candidate_idempotency_keys": sorted(
                    self._uncertain_records or {}
                ),
                "recovery_action": recovery_action,
            }

    def recover_persistence_uncertainty(self) -> dict[str, Any]:
        """Durably reconcile an ambiguous replace without cold-decoding readers.

        Exact saved snapshots are selected only after a successful parent fsync
        and stable byte reread.  Reader-count mutations require a cold process
        restart because a failed acquire may not have returned a handle and a
        failed release may already have relinquished one.
        """
        with self._lock:
            if self._uncertain_records is None:
                return self.persistence_status()
            event = self._last_persistence_event or {}
            operation = str(event.get("operation", "unknown"))
            original_cause = self._last_persistence_cause
            original_cleanup_cause = self._last_persistence_cleanup_cause
            if operation in {"acquire_result", "release_result"}:
                error = TaskRegistryPersistenceError(
                    operation=operation,
                    phase="reader_mutation_requires_cold_restart",
                    outcome="durability_uncertain",
                    committed=False,
                )
                cause = original_cleanup_cause or original_cause
                if cause is None:
                    raise error
                raise error from cause
            candidate_payload = self._uncertain_payload
            candidate_records = self._uncertain_records
            candidate_watermark = self._uncertain_watermark_bucket
            if candidate_payload is None or candidate_watermark is None:
                raise TaskProtocolConflict(
                    "task registry uncertainty snapshot is incomplete"
                )
            try:
                visible = self._read_current_registry_bytes()
                if visible not in {candidate_payload, self._durable_payload}:
                    raise TaskProtocolConflict(
                        "task registry bytes changed during explicit recovery"
                    )
                close_error = self._sync_parent_directory()
                visible_after = self._read_current_registry_bytes()
                if visible_after != visible:
                    raise TaskProtocolConflict(
                        "task registry bytes changed during explicit recovery"
                    )
            except BaseException as exc:
                self._record_persistence_event(
                    outcome="durability_uncertain",
                    phase="explicit_parent_fsync",
                    committed=False,
                    operation=operation,
                    cause=exc,
                )
                raise TaskRegistryPersistenceError(
                    operation=operation,
                    phase="explicit_parent_fsync",
                    outcome="durability_uncertain",
                    committed=False,
                ) from exc

            if visible == candidate_payload:
                self._records = self._clone_records(candidate_records)
                self._submission_watermark_bucket = candidate_watermark
                if close_error is None:
                    self._mark_durable_commit(
                        candidate_payload,
                        outcome="committed_after_explicit_recovery",
                        phase="explicit_parent_fsync",
                        operation=operation,
                    )
                else:
                    self._mark_durable_commit(
                        candidate_payload,
                        outcome="committed_cleanup_failed",
                        phase="explicit_parent_close",
                        cause=original_cause,
                        cleanup_cause=close_error,
                        operation=operation,
                    )
            else:
                self._restore_last_durable_state()
                self._uncertain_records = None
                self._uncertain_watermark_bucket = None
                self._uncertain_payload = None
                self._persistence_generation += 1
                outcome = "not_committed_after_explicit_recovery"
                phase = "explicit_parent_fsync"
                if close_error is not None:
                    outcome = "not_committed_cleanup_failed"
                    phase = "explicit_parent_close"
                self._record_persistence_event(
                    outcome=outcome,
                    phase=phase,
                    committed=False,
                    operation=operation,
                    cause=original_cause,
                    cleanup_cause=close_error or original_cleanup_cause,
                )
            return self.persistence_status()

    def _raise_current_persistence_error(
        self,
        *,
        operation: str,
        phase: str,
        outcome: str,
        committed: bool,
    ) -> None:
        error = TaskRegistryPersistenceError(
            operation=operation,
            phase=phase,
            outcome=outcome,
            committed=committed,
        )
        cause = self._last_persistence_cleanup_cause or self._last_persistence_cause
        if cause is None:
            raise error
        raise error from cause

    def assert_observation_safe(self) -> None:
        if self._uncertain_records is None:
            return
        event = self._last_persistence_event or {}
        self._raise_current_persistence_error(
            operation=str(event.get("operation", self._active_operation)),
            phase=str(event.get("phase", "replace_reconciliation")),
            outcome="durability_uncertain",
            committed=False,
        )

    def assert_persistence_healthy(self) -> None:
        with self._lock:
            event = self._last_persistence_event
            if event is None:
                return
            self._raise_current_persistence_error(
                operation=str(event["operation"]),
                phase=str(event["phase"]),
                outcome=str(event["outcome"]),
                committed=bool(event["committed"]),
            )

    def _ensure_mutation_allowed(self, operation: str) -> None:
        if self._uncertain_records is None:
            return
        event = self._last_persistence_event or {}
        self._raise_current_persistence_error(
            operation=operation,
            phase=str(event.get("phase", "replace_reconciliation")),
            outcome="durability_uncertain",
            committed=False,
        )

    def _read_current_registry_bytes(self) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._path, flags)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or not 0 < before.st_size <= _MAX_REGISTRY_BYTES
            ):
                raise TaskProtocolConflict(
                    "task registry file identity is unsafe"
                )
            chunks: list[bytes] = []
            remaining = _MAX_REGISTRY_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)

            def identity(value: os.stat_result) -> tuple[int, ...]:
                return (
                    value.st_dev,
                    value.st_ino,
                    value.st_mode,
                    value.st_uid,
                    value.st_nlink,
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                )

            if len(raw) != before.st_size or identity(before) != identity(after):
                raise TaskProtocolConflict(
                    "task registry changed while reading"
                )
            return raw
        finally:
            os.close(fd)

    @staticmethod
    def _write_registry_stream(stream: Any, payload: bytes) -> None:
        written = stream.write(payload)
        if written != len(payload):
            raise OSError("short task registry write")

    @staticmethod
    def _flush_registry_stream(stream: Any) -> None:
        stream.flush()

    @staticmethod
    def _fsync_registry_file(fd: int) -> None:
        os.fsync(fd)

    @staticmethod
    def _close_registry_stream(stream: Any) -> None:
        stream.close()

    @staticmethod
    def _replace_registry_file(source: Path, destination: Path) -> None:
        os.replace(source, destination)

    @staticmethod
    def _fsync_parent_descriptor(fd: int) -> None:
        os.fsync(fd)

    @staticmethod
    def _close_parent_descriptor(fd: int) -> None:
        os.close(fd)

    @staticmethod
    def _cleanup_temp_path(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _sync_parent_directory(self) -> BaseException | None:
        directory_fd = os.open(
            self._path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        fsync_error: BaseException | None = None
        close_error: BaseException | None = None
        try:
            self._fsync_parent_descriptor(directory_fd)
        except BaseException as exc:
            fsync_error = exc
        try:
            self._close_parent_descriptor(directory_fd)
        except BaseException as exc:
            close_error = exc
        if fsync_error is not None:
            if close_error is not None:
                fsync_error.add_note(
                    "task registry parent close also failed: "
                    f"{type(close_error).__name__}"
                )
            raise fsync_error
        return close_error

    def _mark_durable_commit(
        self,
        payload: bytes,
        *,
        outcome: str | None = None,
        phase: str = "parent_fsync",
        cause: BaseException | None = None,
        cleanup_cause: BaseException | None = None,
        operation: str | None = None,
    ) -> None:
        self._durable_payload = payload
        self._last_durable_records = self._clone_records(self._records)
        self._last_durable_watermark_bucket = (
            self._submission_watermark_bucket
        )
        self._uncertain_records = None
        self._uncertain_watermark_bucket = None
        self._uncertain_payload = None
        self._persistence_generation += 1
        if outcome is None:
            self._last_persistence_event = None
            self._last_persistence_cause = None
            self._last_persistence_cleanup_cause = None
        else:
            self._record_persistence_event(
                outcome=outcome,
                phase=phase,
                committed=True,
                operation=operation,
                cause=cause,
                cleanup_cause=cleanup_cause,
            )

    def _raise_persistence_failure(
        self,
        *,
        outcome: str,
        phase: str,
        candidate_payload: bytes,
        cause: BaseException | None = None,
        cleanup_cause: BaseException | None = None,
    ) -> None:
        if outcome == "durability_uncertain":
            self._uncertain_records = self._clone_records(self._records)
            self._uncertain_watermark_bucket = (
                self._submission_watermark_bucket
            )
            self._uncertain_payload = candidate_payload
        self._record_persistence_event(
            outcome=outcome,
            phase=phase,
            committed=False,
            cause=cause,
            cleanup_cause=cleanup_cause,
        )
        error = TaskRegistryPersistenceError(
            operation=self._active_operation,
            phase=phase,
            outcome=outcome,
            committed=False,
        )
        if cause is None:
            raise error
        raise error from cause

    def _resolve_replace_outcome(
        self,
        *,
        candidate_payload: bytes,
        phase: str,
        primary_error: BaseException,
        cleanup_error: BaseException | None,
    ) -> None:
        try:
            visible = self._read_current_registry_bytes()
        except BaseException as exc:
            self._raise_persistence_failure(
                outcome="durability_uncertain",
                phase=f"{phase}_readback",
                candidate_payload=candidate_payload,
                cause=exc,
                cleanup_cause=cleanup_error,
            )
        previous_payload = self._durable_payload
        if visible == candidate_payload:
            visible_kind = "candidate"
        elif visible == previous_payload:
            visible_kind = "previous"
        else:
            self._raise_persistence_failure(
                outcome="durability_uncertain",
                phase=f"{phase}_ambiguous_bytes",
                candidate_payload=candidate_payload,
                cause=primary_error,
                cleanup_cause=cleanup_error,
            )
            raise AssertionError("unreachable")
        retry_phase = (
            "parent_fsync_retry"
            if phase == "parent_fsync"
            else f"{phase}_parent_fsync_retry"
        )
        try:
            close_error = self._sync_parent_directory()
            visible_after = self._read_current_registry_bytes()
        except BaseException as exc:
            self._raise_persistence_failure(
                outcome="durability_uncertain",
                phase=retry_phase,
                candidate_payload=candidate_payload,
                cause=exc,
                cleanup_cause=cleanup_error,
            )
            raise AssertionError("unreachable")
        if visible_after != visible:
            self._raise_persistence_failure(
                outcome="durability_uncertain",
                phase=f"{phase}_changed_during_reconciliation",
                candidate_payload=candidate_payload,
                cause=primary_error,
                cleanup_cause=cleanup_error,
            )
        if visible_kind == "previous":
            self._raise_persistence_failure(
                outcome="not_committed",
                phase=f"{phase}_previous_durable",
                candidate_payload=candidate_payload,
                cause=primary_error,
                cleanup_cause=cleanup_error or close_error,
            )
        warning = cleanup_error or close_error
        if warning is None:
            self._mark_durable_commit(
                candidate_payload,
                outcome="committed_after_recovery",
                phase=retry_phase,
                cause=primary_error,
            )
            return
        self._mark_durable_commit(
            candidate_payload,
            outcome="committed_cleanup_failed",
            phase=f"{phase}_cleanup_after_recovery",
            cause=primary_error,
            cleanup_cause=warning,
        )

    def _persist_serialized_payload(self, payload: bytes) -> None:
        temp_path: Path | None = None
        stream: Any | None = None
        raw_fd: int | None = None
        phase = "temp_create"
        replace_attempted = False
        durability_established = False
        primary_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        parent_close_error: BaseException | None = None
        try:
            raw_fd, temp_name = tempfile.mkstemp(
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                dir=self._path.parent,
            )
            temp_path = Path(temp_name)
            stream = os.fdopen(raw_fd, "wb")
            raw_fd = None
            phase = "write"
            self._write_registry_stream(stream, payload)
            phase = "flush"
            self._flush_registry_stream(stream)
            phase = "file_fsync"
            self._fsync_registry_file(stream.fileno())
            phase = "file_close"
            self._close_registry_stream(stream)
            stream = None
            phase = "replace"
            replace_attempted = True
            self._replace_registry_file(temp_path, self._path)
            phase = "parent_fsync"
            parent_close_error = self._sync_parent_directory()
            durability_established = True
        except BaseException as exc:
            primary_error = exc
        if stream is not None:
            try:
                self._close_registry_stream(stream)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if raw_fd is not None:
            try:
                os.close(raw_fd)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if temp_path is not None:
            try:
                self._cleanup_temp_path(temp_path)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc

        if durability_established:
            warning = cleanup_error or parent_close_error
            if warning is None:
                self._mark_durable_commit(payload)
                return
            self._mark_durable_commit(
                payload,
                outcome="committed_cleanup_failed",
                phase="post_commit_cleanup",
                cleanup_cause=warning,
            )
            return

        if primary_error is None:
            primary_error = cleanup_error or OSError(
                "registry persistence did not commit"
            )
        if replace_attempted:
            self._resolve_replace_outcome(
                candidate_payload=payload,
                phase=phase,
                primary_error=primary_error,
                cleanup_error=cleanup_error,
            )
            return
        if cleanup_error is not None and cleanup_error is not primary_error:
            primary_error.add_note(
                "task registry temp cleanup also failed: "
                f"{type(cleanup_error).__name__}"
            )
        # Before replace, the target name was never exchanged.
        self._raise_persistence_failure(
            outcome="not_committed",
            phase=phase if cleanup_error is None else f"{phase}_temp_cleanup",
            candidate_payload=payload,
            cause=primary_error,
            cleanup_cause=cleanup_error,
        )

    def _persist(self) -> None:
        try:
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
                raise TaskProtocolConflict(
                    "task registry exceeds the closed envelope"
                )
        except TaskProtocolConflict:
            raise
        except BaseException as exc:
            self._record_persistence_event(
                outcome="not_committed",
                phase="serialize_or_prepare",
                committed=False,
                cause=exc,
            )
            raise TaskRegistryPersistenceError(
                operation=self._active_operation,
                phase="serialize_or_prepare",
                outcome="not_committed",
                committed=False,
            ) from exc
        self._persist_serialized_payload(payload)



def inspect_quiescent_output_root(
    root: Path, *, allow_empty: bool = False
) -> dict[str, Any]:
    """Read-only point-in-time proof under operator writer exclusion, not a lock.

    Preserve physical inventory and consumed tombstones. Pin/recheck ancestors,
    both directories and registry; never repair, delete, or recursively scan.
    """
    if not root.is_absolute() or ".." in root.parts:
        raise TaskProtocolConflict("output root must be an absolute canonical path")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    opened: list[int] = []
    edges: list[tuple[int, str, int]] = []

    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    def entries(descriptor: int) -> set[str]:
        result: set[str] = set()
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                result.add(entry.name)
                if len(result) > 1:
                    raise TaskProtocolConflict("output directory has unexpected entries")
        return result

    try:
        current = os.open("/", directory_flags)
        opened.append(current)
        for component in root.parts[1:]:
            child = os.open(component, directory_flags, dir_fd=current)
            opened.append(child)
            edges.append((current, component, child))
            current = child
        root_fd = current
        root_meta = os.fstat(root_fd)
        if root_meta.st_uid != os.getuid():
            raise TaskProtocolConflict("output root owner drifted")
        root_identity = {
            "path": str(root), "device": root_meta.st_dev, "inode": root_meta.st_ino,
            "uid": root_meta.st_uid, "mode": root_meta.st_mode,
        }
        root_entries = entries(root_fd)
        control = ".agent-task-protocol-v2"
        proof: dict[str, Any] = {
            "schema": "mineru-output-quiescence.v1", "root_identity": root_identity,
            "registry_sha256": None, "record_count": 0,
            "submission_watermark_bucket": None,
        }
        file_count = total_bytes = 0
        pinned: list[tuple[int, tuple[int, ...]]] = [(root_fd, identity(root_meta))]
        if not root_entries:
            if not allow_empty:
                raise TaskProtocolConflict("commissioned task registry is absent")
        else:
            if root_entries != {control}:
                raise TaskProtocolConflict("output root has unknown or retained task entries")
            control_fd = os.open(control, directory_flags, dir_fd=root_fd)
            opened.append(control_fd)
            edges.append((root_fd, control, control_fd))
            control_meta = os.fstat(control_fd)
            if control_meta.st_uid != os.getuid() or entries(control_fd) != {"registry.json"}:
                raise TaskProtocolConflict("task control directory is not quiescent")
            pinned.append((control_fd, identity(control_meta)))
            descriptor = os.open(
                "registry.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=control_fd,
            )
            opened.append(descriptor)
            edges.append((control_fd, "registry.json", descriptor))
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600
                or not 0 < metadata.st_size <= _MAX_REGISTRY_BYTES
            ):
                raise TaskProtocolConflict("task registry file identity is unsafe")
            pinned.append((descriptor, identity(metadata)))
            chunks: list[bytes] = []
            remaining = metadata.st_size + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != metadata.st_size:
                raise TaskProtocolConflict("task registry changed while reading")
            reader = object.__new__(DurableTaskRegistry)
            reader._output_root = root
            reader._output_root_identity = (
                root_meta.st_dev, root_meta.st_ino, root_meta.st_uid, root_meta.st_mode
            )
            records = reader._decode_registry(raw, require_quiescent=True)
            proof.update(
                registry_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                record_count=len(records),
                submission_watermark_bucket=reader._submission_watermark_bucket,
            )
            file_count, total_bytes = 1, len(raw)
            if entries(control_fd) != {"registry.json"}:
                raise TaskProtocolConflict("task control inventory changed")
        if entries(root_fd) != root_entries:
            raise TaskProtocolConflict("output inventory changed")
        for descriptor, before in pinned:
            if identity(os.fstat(descriptor)) != before:
                raise TaskProtocolConflict("output evidence changed during inspection")
        for parent, name, descriptor in edges:
            linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(descriptor)
            if (linked.st_dev, linked.st_ino, linked.st_mode, linked.st_uid) != (
                held.st_dev, held.st_ino, held.st_mode, held.st_uid
            ):
                raise TaskProtocolConflict("output evidence path was replaced")
        return {"file_count": file_count, "total_bytes": total_bytes, "quiescence": proof}
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


_REGISTRY_MUTATOR_NAMES = (
    "abandon_unbound",
    "acknowledge",
    "acknowledge_failed",
    "acquire_result",
    "bind_task_payload",
    "cleanup_consumed",
    "complete",
    "fail",
    "lease",
    "reconcile_or_create",
    "recoverable_payloads",
    "release_result",
    "reserve_finalizer",
    "transition",
)


def _transactional_registry_mutator(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def wrapped(self: DurableTaskRegistry, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            operation = method.__name__
            self._ensure_mutation_allowed(operation)
            previous_operation = self._active_operation
            starting_generation = self._persistence_generation
            self._active_operation = operation
            try:
                result = method(self, *args, **kwargs)
            except BaseException as exc:
                changed_without_commit = (
                    self._persistence_generation == starting_generation
                    and self._current_state_differs_from_last_durable()
                )
                self._restore_last_durable_state()
                if (
                    changed_without_commit
                    and not isinstance(
                        exc,
                        (TaskProtocolConflict, TaskRegistryPersistenceError),
                    )
                ):
                    # Test/fault probes may replace _persist itself and thereby
                    # bypass phase classification.  Roll back safely and retain
                    # the original injected exception for compatibility.
                    self._record_persistence_event(
                        outcome="not_committed",
                        phase="persist_call",
                        committed=False,
                        operation=operation,
                    )
                raise
            finally:
                self._active_operation = previous_operation
            return copy.deepcopy(result)

    return wrapped


for _registry_mutator_name in _REGISTRY_MUTATOR_NAMES:
    setattr(
        DurableTaskRegistry,
        _registry_mutator_name,
        _transactional_registry_mutator(
            getattr(DurableTaskRegistry, _registry_mutator_name)
        ),
    )


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
        except TaskRegistryPersistenceError:
            raise
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


def task_protocol_runtime_status(
    registry: DurableTaskRegistry, executor: SplitTaskExecutor
) -> dict[str, Any]:
    """Content-free facts from the serving process's initialized objects."""
    if not isinstance(registry, DurableTaskRegistry) or not isinstance(executor, SplitTaskExecutor):
        raise TaskProtocolConflict("task protocol runtime is not initialized")
    limits = {
        "task_registry_max_records": _MAX_RECORDS,
        "task_result_reservation_bytes": executor._result_reservation_bytes,
        "max_unacked_result_bytes": registry._limit,
    }
    if any(type(value) is not int or value < 1 for value in limits.values()):
        raise TaskProtocolConflict("task protocol runtime limits are invalid")
    registry.assert_persistence_healthy()
    return {"schema": "mineru-task-runtime.v1", "enabled": True, **limits}


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
    "TaskRegistryPersistenceError",
    "DurableTaskRecord",
    "DurableTaskRegistry",
    "SplitTaskExecutor",
    "TaskProtocolConflict",
    "evict_consumed_routes",
    "inspect_quiescent_output_root",
    "task_protocol_runtime_status",
]
