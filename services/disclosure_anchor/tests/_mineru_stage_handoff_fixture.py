"""Barriers for the durable-first stage handoff, over the real registry and service IO.

``DurableCommitHold`` parks one registry commit inside its own file fsync, so the
registry data lock is held, the temporary file is already written and the
published durable view still carries the previous committed state.  Nothing about
the protocol's ordering, locking or persistence is replaced; the barrier only
decides when the real commit is allowed to finish.

``StageExecutorHarness`` runs the real ``SplitTaskExecutor`` against the real
``DurableTaskRegistry`` and the real generated ``RegistryServiceIO`` drain, so
registry writes leave the serving loop exactly as they do in the generated API.
Parse and finalizer callbacks are barriers only: no PDF, model, GPU, network or
database is involved.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from tests._mineru_owned_drain_fixture import (
    generated_sources,
    load_definitions,
    load_owned,
)
from tests._mineru_result_reservation_fixture import ReservationLab

ZERO = {
    "result_capacity_waiting": 0,
    "parse_waiting": 0,
    "parse_active": 0,
    "finalizer_waiting": 0,
    "finalizer_active": 0,
}
RESERVATION = 31

_SETTLE: Any = None


def settle_service_operation() -> Any:
    """The real generated shielded drain the serving process gives RegistryServiceIO."""
    global _SETTLE
    if _SETTLE is None:
        sources = generated_sources()
        namespace = load_owned(sources["model"], sources["api"])
        namespace["anyio"] = anyio
        load_definitions(sources["api"], {"_settle_service_operation"}, namespace)
        _SETTLE = namespace["_settle_service_operation"]
    return _SETTLE


def durable_state(registry: Any, key: str) -> str | None:
    """Read the published durable view; a commit may be holding the data lock.

    Every lock-taking accessor (``get``, ``admission_status``, ``observe``) blocks
    or times out while a commit is in flight, so a test that observes a held
    commit must read the same lock-free projection ``/health`` reads.
    """
    for record in registry.durable_view().records:
        if record.idempotency_key == key:
            return record.state
    return None


class DurableCommitHold:
    """Park one registry transition inside its real file fsync until released."""

    def __init__(self, registry: Any, *, target: str, timeout: float = 20.0) -> None:
        self._registry = registry
        self._target = target
        self._timeout = timeout
        self._original_transition = registry.transition
        self._original_fsync = protocol.DurableTaskRegistry._fsync_registry_file
        self._armed = False
        self._wanted: str | None = None
        self._active = False
        self._stack: ExitStack | None = None
        self.held_key: str | None = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def __enter__(self) -> "DurableCommitHold":
        self._stack = ExitStack()
        self._stack.enter_context(
            patch.object(self._registry, "transition", side_effect=self._transition)
        )
        self._stack.enter_context(
            patch.object(
                protocol.DurableTaskRegistry,
                "_fsync_registry_file",
                staticmethod(self._fsync),
            )
        )
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.release.set()
        assert self._stack is not None
        self._stack.close()
        return False

    def arm(self, key: str | None = None) -> None:
        """Hold the next matching transition; later ones commit normally."""
        self._wanted = key
        self.entered.clear()
        self.release.clear()
        self._armed = True

    def _transition(self, key: str, target: str) -> None:
        selected = (
            self._armed
            and target == self._target
            and (self._wanted is None or key == self._wanted)
        )
        if not selected:
            return self._original_transition(key, target)
        self._active = True
        self.held_key = key
        try:
            return self._original_transition(key, target)
        finally:
            self._active = False

    def _fsync(self, descriptor: int) -> None:
        self._original_fsync(descriptor)
        if not self._active:
            return
        self._armed = False
        self._active = False
        self.entered.set()
        if not self.release.wait(self._timeout):
            raise AssertionError(
                "durable-commit hold on "
                + repr(self.held_key)
                + " was never released; the serving loop may have taken the data lock"
            )


class StageExecutorHarness:
    """Real registry, real generated service-IO drain, real SplitTaskExecutor."""

    def __init__(
        self,
        root: Path,
        *,
        parse_slots: int,
        finalizer_slots: int = 1,
        limit: int = 100000,
    ) -> None:
        self.lab = ReservationLab(root, limit=limit)
        self.registry = self.lab.registry
        self.io = protocol.RegistryServiceIO(
            drain=settle_service_operation(), max_pending=32
        )
        self.executor = protocol.SplitTaskExecutor(
            parse_slots=parse_slots,
            finalizer_slots=finalizer_slots,
            result_reservation_bytes=RESERVATION,
        )
        self.executor.start()
        self.parse_slots = parse_slots
        self.finalizer_slots = finalizer_slots
        self.entered: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.final_entered: dict[str, asyncio.Event] = {}
        self.final_release: dict[str, asyncio.Event] = {}
        self.parsed: list[str] = []

    def event(self, store: dict[str, asyncio.Event], key: str) -> asyncio.Event:
        return store.setdefault(key, asyncio.Event())

    def run(
        self,
        key: str,
        *,
        blocking_parse: bool = True,
        blocking_finalize: bool = False,
    ) -> asyncio.Task:
        """Start one accepted task through the real executor and service IO."""
        self.lab.pending(key)
        # Bind every barrier now, so ``release_all`` reaches a task that has not
        # entered its callback yet instead of leaving it blocked forever.
        for store in (self.entered, self.release, self.final_entered, self.final_release):
            self.event(store, key)

        async def parse() -> None:
            self.parsed.append(key)
            self.event(self.entered, key).set()
            if blocking_parse:
                await self.event(self.release, key).wait()

        async def finalize() -> tuple[Path, str, int, str]:
            self.event(self.final_entered, key).set()
            if blocking_finalize:
                await self.event(self.final_release, key).wait()
            result = self.lab.result(key, 1)
            return (
                result["result_path"],
                result["result_sha256"],
                result["result_bytes"],
                result["result_owner"],
            )

        return asyncio.create_task(
            self.executor.run(
                registry=self.registry,
                key=key,
                parse=parse,
                finalize=finalize,
                registry_io=self.io,
            ),
            name="stage-handoff-" + key,
        )

    def stages(self) -> dict[str, int]:
        return self.executor.stage_snapshot()

    def free_parse_permits(self) -> int:
        return self.executor._parse._value

    def free_finalizer_permits(self) -> int:
        return self.executor._finalize._value

    def release_all(self) -> None:
        for store in (self.release, self.final_release):
            for item in store.values():
                item.set()

    async def close(self) -> None:
        self.release_all()
        await asyncio.wait_for(self.io.close(), 10)
