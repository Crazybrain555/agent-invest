"""Durable state machine for the default-off MinerU task protocol v2."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Literal

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
        if result_bytes < 1 or len(result_sha256) != 64 or len(result_owner) != 64:
            raise ValueError("result identity is invalid")
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "finalizing":
                raise TaskProtocolConflict("only finalizing tasks may complete")
            projected = self.unacked_result_bytes + result_bytes
            if projected > self._limit:
                raise TaskProtocolConflict("unacked result byte capacity exhausted")
            record.result_path = str(result_path)
            record.result_sha256 = result_sha256
            record.result_bytes = result_bytes
            record.result_owner = result_owner
            record.state = "completed"
            self._persist()

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
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "completed" or not record.result_path:
                raise TaskProtocolConflict("result is unavailable")
            if not record.lease_until_unix or record.lease_until_unix <= time.time():
                raise TaskProtocolConflict("result lease is absent or expired")
            record.active_readers += 1
            self._persist()
            path = Path(record.result_path)
        try:
            yield path
        finally:
            with self._lock:
                current = self._required(idempotency_key)
                current.active_readers -= 1
                if current.active_readers < 0:
                    raise RuntimeError("result reader count underflowed")
                self._persist()

    def acknowledge(self, idempotency_key: str) -> None:
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "completed" or record.active_readers:
                raise TaskProtocolConflict("result cannot be ACKed while unavailable/in use")
            record.state = "consumed"
            self._persist()

    def cleanup_consumed(self, unlink: Callable[[Path], None]) -> int:
        with self._lock:
            removable = [
                key
                for key, record in self._records.items()
                if record.state == "consumed" and record.active_readers == 0
            ]
            for key in removable:
                record = self._records.pop(key)
                if record.result_path:
                    unlink(Path(record.result_path))
            if removable:
                self._persist()
            return len(removable)

    def _required(self, key: str) -> DurableTaskRecord:
        try:
            return self._records[key]
        except KeyError as exc:
            raise TaskProtocolConflict("task is unknown") from exc

    def _load(self) -> dict[str, DurableTaskRecord]:
        if not self._path.exists():
            return {}
        payload = json.loads(self._path.read_text())
        if not isinstance(payload, dict) or payload.get("schema") != "mineru-task-registry.v2":
            raise TaskProtocolConflict("task registry schema is invalid")
        records = payload.get("records")
        if not isinstance(records, list):
            raise TaskProtocolConflict("task registry records are invalid")
        loaded = {item["idempotency_key"]: DurableTaskRecord(**item) for item in records}
        for record in loaded.values():
            record.active_readers = 0
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

    def __init__(self, *, parse_slots: int, finalizer_slots: int) -> None:
        if parse_slots < 1 or finalizer_slots < 1:
            raise ValueError("executor slots must be positive")
        self._parse = asyncio.Semaphore(parse_slots)
        self._finalize = asyncio.Semaphore(finalizer_slots)

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
