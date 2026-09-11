"""Disposable registry laboratory shared by the M6 P1 durability tests.

Everything here observes the task registry only through its public surface or
through the bytes it leaves on disk.  Fault injection replaces the registry's
narrow storage hooks with synthetic failures; nothing reimplements persistence
logic to predict an outcome.  Roots are temporary directories created by the
caller; no production PDF, PostgreSQL, credential or shared runtime path is
touched.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts.windows.mineru_heap_trim_compat.agent_task_protocol_v2 import (
    DurableTaskRegistry,
)

UNACKED_LIMIT = 2 * 1024 * 1024 * 1024
FINALIZER_BUDGET = 1024 * 1024
KEY = "key-primary"
TASK = "task-primary"
OTHER_KEY = "key-other"
OTHER_TASK = "task-other"

# Storage phases that fail before the registry pathname is ever exchanged.
PRE_COMMIT_PHASES = (
    "temp_create",
    "write",
    "short_write",
    "flush",
    "file_fsync",
    "file_close",
    "replace_before_rename",
)

# Every public mutator, paired with the durable stage it must start from.
MUTATOR_PRECONDITIONS: dict[str, str] = {
    "reconcile_or_create": "pending_unbound",
    "abandon_unbound": "pending_unbound",
    "bind_task_payload": "pending_unbound",
    "transition": "bound",
    "fail": "processing",
    "recoverable_payloads": "processing",
    "reserve_finalizer": "finalizing",
    "complete": "reserved",
    "lease": "completed",
    "acknowledge": "completed",
    "acquire_result": "leased",
    "release_result": "acquired",
    "acknowledge_failed": "failed",
    "cleanup_consumed": "acknowledged",
}


class SyntheticStorageFault(OSError):
    """Marker exception for every injected storage failure."""


def result_owner(task_id: str, sha256_hex: str, size: int) -> str:
    """Wire identity of a retained result: task id, content hash and byte count."""
    return hashlib.sha256(f"{task_id}\0{sha256_hex}\0{size}".encode()).hexdigest()


class RegistryLab:
    """One disposable output root with its private registry pathname."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.output_root = self.root / "output"
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.registry_path = (
            self.output_root / ".agent-task-protocol-v2" / "registry.json"
        )

    def open(self, **overrides: Any) -> DurableTaskRegistry:
        options: dict[str, Any] = {
            "max_unacked_result_bytes": UNACKED_LIMIT,
            "output_root": self.output_root,
        }
        options.update(overrides)
        return DurableTaskRegistry(self.registry_path, **options)

    # -- on-disk observation -------------------------------------------------

    def disk_bytes(self) -> bytes | None:
        try:
            return self.registry_path.read_bytes()
        except FileNotFoundError:
            return None

    def disk_records(self) -> dict[str, dict[str, Any]]:
        raw = self.disk_bytes()
        if raw is None:
            return {}
        payload = json.loads(raw)
        return {item["idempotency_key"]: item for item in payload["records"]}

    def stray_names(self) -> list[str]:
        """Names other than the registry file inside its private directory."""
        directory = self.registry_path.parent
        if not directory.exists():
            return []
        return sorted(
            name for name in os.listdir(directory) if name != self.registry_path.name
        )

    # -- owned task trees ------------------------------------------------------

    def task_dir(self, task_id: str) -> Path:
        return self.output_root / task_id

    def make_task_tree(
        self, task_id: str, *, upload: bytes = b"%PDF-synthetic-placeholder"
    ) -> dict[str, Any]:
        task_root = self.task_dir(task_id)
        uploads = task_root / "uploads"
        uploads.mkdir(parents=True)
        upload_path = uploads / "source.pdf"
        upload_path.write_bytes(upload)
        return {
            "task_id": task_id,
            "output_dir": str(task_root),
            "uploads": [str(upload_path)],
            "backend": "hybrid",
            "options": {"nested": {"list": [1, 2, 3]}},
        }

    def make_result(
        self, task_id: str, content: bytes = b"PK\x05\x06synthetic-empty-zip"
    ) -> tuple[Path, str, int, str]:
        path = self.task_dir(task_id) / "result.zip"
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        return path, digest, len(content), result_owner(task_id, digest, len(content))


# -- durable stage driver --------------------------------------------------------


def drive(
    lab: RegistryLab,
    registry: DurableTaskRegistry,
    stage: str,
    *,
    key: str = KEY,
    task_id: str = TASK,
) -> dict[str, Any]:
    """Bring one record to a named durable stage through public mutators only."""
    facts: dict[str, Any] = {}
    registry.reconcile_or_create(
        idempotency_key=key,
        task_id=task_id,
        attempt_identity=f"attempt-{key}",
        fence_identity=f"fence-{key}",
    )
    if stage == "pending_unbound":
        return facts
    facts["payload"] = lab.make_task_tree(task_id)
    registry.bind_task_payload(key, facts["payload"])
    if stage == "bound":
        return facts
    registry.transition(key, "processing")
    if stage == "processing":
        return facts
    if stage == "failed":
        registry.fail(key, error="synthetic parse failure")
        return facts
    registry.transition(key, "finalizing")
    if stage == "finalizing":
        return facts
    registry.reserve_finalizer(key, byte_budget=FINALIZER_BUDGET)
    if stage == "reserved":
        return facts
    facts["result"] = lab.make_result(task_id)
    path, digest, size, owner = facts["result"]
    registry.complete(
        key,
        result_path=path,
        result_sha256=digest,
        result_bytes=size,
        result_owner=owner,
    )
    if stage == "completed":
        return facts
    registry.lease(key, seconds=600)
    if stage == "leased":
        return facts
    if stage == "acquired":
        facts["reader_path"] = registry.acquire_result(key)
        return facts
    registry.acknowledge(key)
    if stage == "acknowledged":
        return facts
    raise ValueError(f"unknown registry stage {stage!r}")


def prepare_mutation(
    lab: RegistryLab, registry: DurableTaskRegistry, name: str
) -> Callable[[], object]:
    """Reach the mutator's precondition and return the exact call to perform."""
    stage = MUTATOR_PRECONDITIONS[name]
    facts = drive(lab, registry, stage)
    if name == "reconcile_or_create":
        return functools.partial(
            registry.reconcile_or_create,
            idempotency_key=OTHER_KEY,
            task_id=OTHER_TASK,
            attempt_identity="attempt-other",
            fence_identity="fence-other",
        )
    if name == "abandon_unbound":
        return functools.partial(registry.abandon_unbound, KEY)
    if name == "bind_task_payload":
        payload = lab.make_task_tree(TASK)
        return functools.partial(registry.bind_task_payload, KEY, payload)
    if name == "transition":
        return functools.partial(registry.transition, KEY, "processing")
    if name == "fail":
        return functools.partial(registry.fail, KEY, error="synthetic failure")
    if name == "recoverable_payloads":
        return registry.recoverable_payloads
    if name == "reserve_finalizer":
        return functools.partial(
            registry.reserve_finalizer, KEY, byte_budget=FINALIZER_BUDGET
        )
    if name == "complete":
        path, digest, size, owner = lab.make_result(TASK)
        return functools.partial(
            registry.complete,
            KEY,
            result_path=path,
            result_sha256=digest,
            result_bytes=size,
            result_owner=owner,
        )
    if name == "lease":
        return functools.partial(registry.lease, KEY, seconds=600)
    if name == "acknowledge":
        return functools.partial(registry.acknowledge, KEY)
    if name == "acquire_result":
        return functools.partial(registry.acquire_result, KEY)
    if name == "release_result":
        return functools.partial(registry.release_result, KEY)
    if name == "acknowledge_failed":
        return functools.partial(registry.acknowledge_failed, KEY)
    if name == "cleanup_consumed":
        return registry.cleanup_consumed
    raise ValueError(f"unknown mutator {name!r}: facts={sorted(facts)}")


# -- public state snapshots --------------------------------------------------------


def snapshot(
    registry: DurableTaskRegistry, keys: tuple[str, ...] = (KEY, OTHER_KEY)
) -> dict[str, Any]:
    """Public in-memory state: records by key plus both capacity charges."""
    records: dict[str, Any] = {}
    for key in keys:
        record = registry.get(key)
        records[key] = None if record is None else asdict(record)
    return {
        "records": records,
        "unacked": registry.unacked_result_bytes,
        "reserved": registry.reserved_result_bytes,
    }


def without_live_readers(state: dict[str, Any]) -> dict[str, Any]:
    """A cold restart resets process-local reader counts; compare the rest."""
    copied = json.loads(json.dumps(state))
    for record in copied["records"].values():
        if record is not None:
            record["active_readers"] = 0
    return copied


# -- fault injection -------------------------------------------------------------


def fail_with(message: str) -> Callable[..., Any]:
    def raise_fault(*_args: Any, **_kwargs: Any) -> Any:
        raise SyntheticStorageFault(message)

    return raise_fault


class CountedFault:
    """Raise for the first ``failures`` calls, then delegate (or no-op)."""

    def __init__(
        self,
        failures: int,
        delegate: Callable[..., Any] | None = None,
        *,
        message: str = "synthetic storage fault",
        delegate_first: bool = False,
    ) -> None:
        self.remaining = failures
        self.delegate = delegate
        self.message = message
        self.delegate_first = delegate_first
        self.calls = 0

    def __call__(self, *args: Any) -> Any:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            if self.delegate_first and self.delegate is not None:
                self.delegate(*args)
            raise SyntheticStorageFault(self.message)
        if self.delegate is not None:
            return self.delegate(*args)
        return None


PERMANENT = 10**9


@contextlib.contextmanager
def instance_hook(
    registry: DurableTaskRegistry, name: str, replacement: Callable[..., Any]
) -> Iterator[None]:
    """Shadow one static storage hook on a single registry instance."""
    setattr(registry, name, replacement)
    try:
        yield
    finally:
        delattr(registry, name)


class _ShortStream:
    """Write half of every payload and report the short count honestly."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, payload: bytes) -> int:
        return self._stream.write(payload[: max(1, len(payload) // 2)])


def short_write(stream: Any, payload: bytes) -> None:
    DurableTaskRegistry._write_registry_stream(_ShortStream(stream), payload)


def close_then_fail(stream: Any) -> None:
    stream.close()
    raise SyntheticStorageFault("synthetic close failure after real close")


def replace_then_fail(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    raise SyntheticStorageFault("replace reported failure after exchanging names")


def write_foreign_then_fail(source: Path, destination: Path) -> None:
    """Leave bytes that are neither the previous nor the candidate payload."""
    if destination.exists():
        destination.write_bytes(b'{"schema":"foreign-bytes"}')
    else:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, b'{"schema":"foreign-bytes"}')
        finally:
            os.close(descriptor)
    raise SyntheticStorageFault("replace failed after foreign bytes appeared")


def close_descriptor_then_fail(descriptor: int) -> None:
    os.close(descriptor)
    raise SyntheticStorageFault("parent descriptor close reported failure")


@contextlib.contextmanager
def pre_commit_fault(registry: DurableTaskRegistry, phase: str) -> Iterator[None]:
    """Inject one storage failure that happens before the name exchange."""
    if phase == "temp_create":
        import tempfile
        from unittest import mock

        with mock.patch.object(
            tempfile, "mkstemp", side_effect=SyntheticStorageFault("temp create failed")
        ):
            yield
        return
    hooks = {
        "write": ("_write_registry_stream", fail_with("write failed")),
        "short_write": ("_write_registry_stream", short_write),
        "flush": ("_flush_registry_stream", fail_with("flush failed")),
        "file_fsync": ("_fsync_registry_file", fail_with("file fsync failed")),
        "file_close": ("_close_registry_stream", close_then_fail),
        "replace_before_rename": (
            "_replace_registry_file",
            fail_with("replace failed before exchanging names"),
        ),
    }
    name, replacement = hooks[phase]
    with instance_hook(registry, name, replacement):
        yield


@contextlib.contextmanager
def persist_override(registry: DurableTaskRegistry) -> Iterator[None]:
    """Replace the whole persistence call, bypassing phase classification."""
    with instance_hook(registry, "_persist", fail_with("persist call failed")):
        yield
