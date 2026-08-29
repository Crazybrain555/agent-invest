"""Durable state machine for the default-off MinerU task protocol v2."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
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
    "pending", "processing", "finalizing", "completed", "failed", "consumed"
]


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

    def __post_init__(self) -> None:
        for value in (
            self.idempotency_key,
            self.task_id,
            self.attempt_identity,
            self.fence_identity,
        ):
            if not isinstance(value, str) or not value or len(value) > 1024:
                raise TaskProtocolConflict("task registry identity is invalid")
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
        if self.state in {"completed", "consumed"} and not has_result_identity:
            raise TaskProtocolConflict("terminal task result identity is incomplete")
        if self.state == "completed" and not isinstance(self.result_path, str):
            raise TaskProtocolConflict("completed task result path is absent")
        if self.state not in {"completed", "consumed"} and any(
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

    def __init__(self, path: Path, *, max_unacked_result_bytes: int) -> None:
        if max_unacked_result_bytes < 1:
            raise ValueError("unacked result byte limit must be positive")
        self._path = path
        self._limit = max_unacked_result_bytes
        self._lock = RLock()
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
            existing = self._records.get(idempotency_key)
            if existing is not None:
                if (
                    existing.attempt_identity != attempt_identity
                    or existing.fence_identity != fence_identity
                ):
                    raise TaskProtocolConflict(
                        "idempotency key was reused with different attempt/fence"
                    )
                return existing, False
            record = DurableTaskRecord(
                idempotency_key=idempotency_key,
                task_id=task_id,
                attempt_identity=attempt_identity,
                fence_identity=fence_identity,
            )
            self._records[idempotency_key] = record
            self._persist()
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
        output = Path(output_value).resolve()
        upload_root = (output / "uploads").resolve()
        for value in uploads_value:
            if not isinstance(value, str):
                raise TaskProtocolConflict("restart upload path is invalid")
            path = Path(value)
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or path.resolve().parent != upload_root
            ):
                raise TaskProtocolConflict("restart upload snapshot identity drifted")
        for child in output.iterdir():
            if child.resolve() == upload_root:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

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

    def transition(self, idempotency_key: str, target: TaskState) -> None:
        allowed: dict[TaskState, frozenset[TaskState]] = {
            "pending": frozenset({"processing", "failed"}),
            "processing": frozenset({"finalizing", "failed"}),
            "finalizing": frozenset({"completed", "failed"}),
            "completed": frozenset({"consumed"}),
            "failed": frozenset(),
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
            if record.state == "completed"
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
            if record.state == "consumed":
                return
            if record.state != "completed" or record.active_readers:
                raise TaskProtocolConflict(
                    "result cannot be ACKed while unavailable/in use"
                )
            record.state = "consumed"
            self._persist()

    def cleanup_consumed(self, unlink: Callable[[Path], None]) -> int:
        with self._lock:
            removable = [
                key
                for key, record in self._records.items()
                if record.state == "consumed" and record.active_readers == 0
            ]
            cleaned = 0
            for key in removable:
                record = self._records[key]
                if record.result_path:
                    unlink(Path(record.result_path))
                    record.result_path = None
                    cleaned += 1
            if cleaned:
                self._persist()
            return cleaned

    def _required(self, key: str) -> DurableTaskRecord:
        try:
            return self._records[key]
        except KeyError as exc:
            raise TaskProtocolConflict("task is unknown") from exc

    def _load(self) -> dict[str, DurableTaskRecord]:
        if not self._path.exists():
            return {}
        metadata = self._path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 16 * 1024 * 1024
        ):
            raise TaskProtocolConflict("task registry file identity is unsafe")
        raw = self._path.read_bytes()

        def closed_object(pairs):
            value = {}
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
            or set(payload) != {"schema", "records"}
            or payload.get("schema") != "mineru-task-registry.v2"
        ):
            raise TaskProtocolConflict("task registry schema is invalid")
        records = payload.get("records")
        if not isinstance(records, list):
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
                "consumed",
            }:
                raise TaskProtocolConflict("task registry state is invalid")
            loaded[record.idempotency_key] = record
            task_ids.add(record.task_id)
        for record in loaded.values():
            record.active_readers = 0
            if record.state == "completed":
                result_path = Path(record.result_path or "")
                metadata = result_path.lstat()
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
        except BaseException:
            record = registry.get(key)
            if record is not None and record.state in {"processing", "finalizing"}:
                registry.transition(key, "failed")
            raise


__all__ = [
    "DurableTaskRecord",
    "DurableTaskRegistry",
    "SplitTaskExecutor",
    "TaskProtocolConflict",
]
