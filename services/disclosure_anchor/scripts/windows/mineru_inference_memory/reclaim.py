"""Idle-cadenced allocator-cache release in the MinerU vLLM inference owner.

Loaded only by the ``mineru.owner-memory-reclaim.v1`` inference mode:

* The API server loads ``OwnerMemoryReclaimMiddleware`` through vLLM's
  ``--middleware``. It passes every HTTP and websocket request through untouched
  and, for the server's lifespan, owns one asyncio heartbeat that issues
  ``collective_rpc("mineru_owner_memory_reclaim")``. The API process never
  touches CUDA.
* vLLM 0.21 runs that utility on the EngineCore busy-loop thread between
  scheduling iterations, and ``UniProcExecutor`` calls the worker on that same
  thread, the one that initialized the device. ``OwnerMemoryWorker`` binds an
  ``OwnerMemoryReclaimer`` on the first call and delegates every call to it.

Safety of a release does not rest on the engine being idle. The release is
``torch.cuda.synchronize(device)`` then ``torch.cuda.empty_cache()`` on the owner
thread with no stream capture underway. The native allocator takes its mutex,
completes outstanding cross-stream block events, and returns to the driver only
whole free segments of its default pools and the private pools of graphs that
no longer exist. Allocated blocks (weights, KV cache, workspace, encoder-cache
entries, pending outputs) and the pools of live graphs are never free blocks,
so they are untouched whatever the engine is doing. Owner-hook quiet decides
only the cadence: it is not evidence that the engine is globally idle.

Importing this module loads only the standard library and ``.core``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from .core import (
    MAX_RECORD_BYTES,
    RECLAIM_METHOD,
    IdleEvidence,
    OwnerMemoryMonitor,
    OwnerMemoryPreconditionError,
    allocator_section,
    device_memory_section,
    error_type,
    file_sha256,
    require_loaded_file,
    token,
)

RECLAIM_SCHEMA = "mineru.gpu-owner-reclaim.v1"
RECLAIM_LOG_MARKER = "MINERU_GPU_OWNER_RECLAIM"
RECLAIM_MIDDLEWARE = "mineru_inference_memory.reclaim.OwnerMemoryReclaimMiddleware"
# Candidate operational limits; qualification measures their effect.
# Owner-hook silence before a release. Inter-document gaps seen in production
# were seconds, so this normally separates a pause in work from a gap inside a
# batch; it guarantees nothing about future workloads.
RECLAIM_IDLE_NS = 60_000_000_000
# One utility call per interval; bounds the delay after the idle threshold.
RECLAIM_HEARTBEAT_SECONDS = 15.0
# After this long without an answer the outcome is unknown and the heartbeat
# stops for good; an answered call waits only for the engine step in progress
# and the release itself.
RECLAIM_CALL_TIMEOUT_SECONDS = 120.0
# Byte-exact vLLM v0.21.0 files that define the utility dispatch between engine
# steps and the executor's same-thread worker call; checked before any release.
RECLAIM_ENGINE_SOURCES: dict[str, tuple[str, str]] = {
    "v1/engine/core.py": (
        "vllm.v1.engine.core",
        "53ff4df24745e3ac4ac09028ddeb310f6627b90f6374dbfa82548e97c9bc65bc",
    ),
    "v1/executor/uniproc_executor.py": (
        "vllm.v1.executor.uniproc_executor",
        "93fac0cbecffb9800b1a90746fd179bfdc7c61bf22c6c1337ebe0d917157f247",
    ),
}

LOGGER = logging.getLogger("vllm.mineru_inference_memory")

JsonDict = dict[str, Any]
_COMPACT_STATS = (
    "reserved_bytes.all.current",
    "allocated_bytes.all.current",
    "active_bytes.all.current",
    "inactive_split_bytes.all.current",
    "segment.all.current",
    "num_device_alloc",
    "num_device_free",
    "num_alloc_retries",
    "num_ooms",
    "num_sync_all_streams",
)
_OUTCOMES = frozenset({"refused", "released", "no_benefit", "failed", "disabled"})
_TERMINAL_OUTCOMES = frozenset({"failed", "disabled"})


class ReclaimCuda(Protocol):
    """The owner's CUDA entry points; read-only ones match ``core.CudaRuntime``."""

    def is_initialized(self) -> bool: ...

    def current_device(self) -> int: ...

    def memory_stats(self, device_index: int) -> Mapping[str, object]: ...

    def allocator_backend(self) -> str: ...

    def mem_get_info(self, device_index: int) -> tuple[int, int]: ...

    def is_current_stream_capturing(self) -> bool: ...

    def synchronize(self, device_index: int) -> None: ...

    def empty_cache(self) -> None: ...


class TorchReclaimCuda:
    """``ReclaimCuda`` over ``torch.cuda``; built on the owner thread at binding."""

    def __init__(self) -> None:
        import torch

        cuda = torch.cuda
        for name in ("is_initialized", "current_device", "memory_stats", "get_allocator_backend",
                     "mem_get_info", "is_current_stream_capturing", "synchronize", "empty_cache"):
            if not callable(getattr(cuda, name, None)):
                raise OwnerMemoryPreconditionError("torch.cuda lacks the reviewed " + name)
        self._cuda = cuda

    def is_initialized(self) -> bool:
        return self._cuda.is_initialized() is True

    def current_device(self) -> int:
        return self._cuda.current_device()

    def memory_stats(self, device_index: int) -> Any:
        return self._cuda.memory_stats(device_index)

    def allocator_backend(self) -> Any:
        return self._cuda.get_allocator_backend()

    def mem_get_info(self, device_index: int) -> Any:
        return self._cuda.mem_get_info(device_index)

    def is_current_stream_capturing(self) -> bool:
        return self._cuda.is_current_stream_capturing() is not False

    def synchronize(self, device_index: int) -> None:
        self._cuda.synchronize(device_index)

    def empty_cache(self) -> None:
        self._cuda.empty_cache()


def engine_binding(vllm_root: Path, parallel_config: object) -> tuple[str | None, JsonDict]:
    """The decisive engine facts of the same-thread argument: (failure, facts)."""
    sources: dict[str, str | None] = {}
    failure: str | None = None
    for relative, (module_name, expected) in RECLAIM_ENGINE_SOURCES.items():
        path = vllm_root / relative
        try:
            digest: str | None = file_sha256(path)
            require_loaded_file(getattr(sys.modules.get(module_name), "__file__", None), path)
        except OwnerMemoryPreconditionError:
            digest = None
        sources[relative] = None if digest is None else "sha256:" + digest
        if digest != expected and failure is None:
            failure = "engine_source"
    backend = getattr(parallel_config, "distributed_executor_backend", None)
    topology = {
        "executor_backend": token(backend) if isinstance(backend, str) else None,
        "world_size": getattr(parallel_config, "world_size", None),
        "data_parallel_size": getattr(parallel_config, "data_parallel_size", None),
    }
    if failure is None and topology != {"executor_backend": "uni", "world_size": 1,
                                        "data_parallel_size": 1}:
        failure = "executor_topology"
    return failure, {"engine_sources": sources, "topology": topology}


def _measure(cuda: ReclaimCuda, device_index: int) -> JsonDict:
    allocator = allocator_section(cuda, device_index)
    stats = allocator["stats"] if isinstance(allocator["stats"], dict) else {}
    return {
        "allocator": {
            "state": allocator["state"],
            "reason": allocator["reason"],
            "backend": allocator["backend"],
            "stats": {key: stats.get(key) for key in _COMPACT_STATS},
        },
        "device_memory": device_memory_section(cuda, device_index),
    }


def _stat(measurement: JsonDict | None, key: str) -> int | None:
    if measurement is None:
        return None
    value = measurement["allocator"]["stats"].get(key)
    return value if type(value) is int else None


def _device_free(measurement: JsonDict | None) -> int | None:
    if measurement is None:
        return None
    value = measurement["device_memory"].get("free_bytes")
    return value if type(value) is int else None


def _difference(minuend: int | None, subtrahend: int | None) -> int | None:
    return None if minuend is None or subtrahend is None else minuend - subtrahend


class OwnerMemoryReclaimer:
    """Engine-thread state behind ``OwnerMemoryWorker.mineru_owner_memory_reclaim``.

    Gates (cadence only): the bound owner pid/thread, a warm monitor, owner-hook
    silence of at least ``RECLAIM_IDLE_NS`` and one decision per such silence.
    The last step's classification and the runner's request count are recorded
    facts, never gates. A routine refusal makes no CUDA call and no record.

    Records: one ``bound`` record when the first owner call verified the
    engine binding, then one ``reclaim`` record per release (outcome
    ``released`` or ``no_benefit``) and for the one failure (``failed``). The
    first failure latches the reclaimer off. An exception raised by a CUDA or
    measurement call is recorded and re-raised unchanged, so vLLM returns it as
    the utility's failure and the next engine step meets any device fault.
    """

    def __init__(
        self,
        *,
        monitor: OwnerMemoryMonitor,
        runner: object,
        cuda: ReclaimCuda,
        logger: logging.Logger,
        binding: Callable[[], tuple[str | None, JsonDict]] | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        wall_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._monitor = monitor
        self._runner = runner
        self._cuda = cuda
        self._binding = binding
        self._logger = logger
        self._clock_ns = clock_ns
        self._wall_ns = wall_ns
        self._bound = False
        self._seq = 0
        self._disabled = False
        # Step-return time of the owner silence already decided; one decision each.
        self._decided_return_ns: int | None = None
        self._counts = {"calls": 0, "refused": 0, "released": 0, "no_benefit": 0, "failed": 0}

    @staticmethod
    def policy() -> JsonDict:
        return {
            "method": RECLAIM_METHOD,
            "idle_threshold_ns": RECLAIM_IDLE_NS,
            "heartbeat_seconds": RECLAIM_HEARTBEAT_SECONDS,
            "call_timeout_seconds": RECLAIM_CALL_TIMEOUT_SECONDS,
            "releases_per_owner_silence": 1,
        }

    def attempt(self) -> JsonDict:
        self._counts["calls"] += 1
        if self._disabled:
            return self._reply("disabled")
        evidence = self._monitor.idle_evidence()
        if not evidence.owner_call:
            return self._fail(evidence, "owner_thread", None)
        if not self._bound:
            failed = self._bind(evidence)
            if failed is not None:
                return failed
        if not evidence.warm:
            return self._refuse("not_warm")
        if evidence.quiet_ns is None or evidence.quiet_ns < RECLAIM_IDLE_NS:
            return self._refuse("busy")
        if evidence.last_return_ns == self._decided_return_ns:
            return self._refuse("already_decided")
        self._decided_return_ns = evidence.last_return_ns
        return self._release(evidence)

    def _bind(self, evidence: IdleEvidence) -> JsonDict | None:
        failure, facts = (None, {}) if self._binding is None else self._binding()
        if failure is not None:
            return self._fail(evidence, failure, None, binding=facts)
        try:
            source = file_sha256(Path(__file__))
        except OwnerMemoryPreconditionError:
            return self._fail(evidence, "reclaim_source", None, binding=facts)
        self._bound = True
        self._record("bound", evidence, binding={
            **facts, "policy": self.policy(), "source": "sha256:" + source,
        })
        return None

    def _release(self, evidence: IdleEvidence) -> JsonDict:
        cuda, device = self._cuda, evidence.device_index
        if device is None:
            return self._fail(evidence, "owner_device", None)
        facts = self._facts(evidence)
        before: JsonDict | None = None
        after: JsonDict | None = None
        timing: dict[str, int | None] = {"started_ns": self._clock_ns()}
        stage = "device_check"
        try:
            # current_device() would create a context if CUDA were uninitialized.
            if not cuda.is_initialized():
                return self._fail(evidence, "cuda_not_initialized", None, facts=facts, timing=timing)
            if cuda.current_device() != device:
                return self._fail(evidence, "device_mismatch", None, facts=facts, timing=timing)
            if cuda.is_current_stream_capturing():
                return self._fail(evidence, "stream_capturing", None, facts=facts, timing=timing)
            stage = "measure_before"
            before = _measure(cuda, device)
            if before["allocator"]["state"] != "available":
                return self._fail(evidence, "allocator_unavailable", None, facts=facts,
                                  before=before, timing=timing)
            stage = "synchronize"
            cuda.synchronize(device)
            timing["synchronized_ns"] = self._clock_ns()
            stage = "empty_cache"
            cuda.empty_cache()
            timing["emptied_ns"] = self._clock_ns()
            stage = "measure_after"
            after = _measure(cuda, device)
        except Exception as error:
            self._fail(evidence, stage, error_type(error), facts=facts, before=before, after=after,
                       timing=timing)
            raise
        timing["completed_ns"] = self._clock_ns()
        if after["allocator"]["state"] != "available":
            # The release ran but its effect is unknown: never report an outcome.
            return self._fail(evidence, "measure_after_unavailable", None, facts=facts,
                              before=before, after=after, timing=timing)
        released = _difference(_stat(before, "reserved_bytes.all.current"),
                               _stat(after, "reserved_bytes.all.current"))
        outcome = "released" if released is not None and released > 0 else "no_benefit"
        self._counts[outcome] += 1
        effect: JsonDict = {
            # Observed owner-allocator change across the bracket, not a prediction.
            "released_reserved_bytes": released,
            "device_free_calls": _difference(_stat(after, "num_device_free"),
                                             _stat(before, "num_device_free")),
            # cudaMemGetInfo covers every process on the device.
            "device_wide_free_bytes_change": _difference(_device_free(after), _device_free(before)),
        }
        self._record("reclaim", evidence, outcome=outcome, facts=facts, before=before, after=after,
                     effect=effect, timing=timing)
        return self._reply(outcome, released_reserved_bytes=released)

    def _facts(self, evidence: IdleEvidence) -> JsonDict:
        requests = getattr(self._runner, "requests", None)
        return {
            "quiet_ns": evidence.quiet_ns,
            "last_return_ns": evidence.last_return_ns,
            "idle_threshold_ns": RECLAIM_IDLE_NS,
            "last_step": evidence.last_step,
            "runner_requests": len(requests) if type(requests) is dict else None,
        }

    def _refuse(self, reason: str) -> JsonDict:
        self._counts["refused"] += 1
        return self._reply("refused", reason=reason)

    def _fail(
        self, evidence: IdleEvidence, stage: str, failure: str | None, *,
        facts: JsonDict | None = None, binding: JsonDict | None = None,
        before: JsonDict | None = None, after: JsonDict | None = None,
        timing: Mapping[str, int | None] | None = None,
    ) -> JsonDict:
        self._disabled = True
        self._counts["failed"] += 1
        self._record("reclaim", evidence, outcome="failed", reason=stage, error_type=failure,
                     facts=facts, binding=binding, before=before, after=after, timing=timing)
        return self._reply("failed", reason=stage)

    @staticmethod
    def _reply(outcome: str, **fields: object) -> JsonDict:
        return {"schema": RECLAIM_SCHEMA, "outcome": outcome, **fields}

    def _record(
        self, kind: str, evidence: IdleEvidence, *, outcome: str | None = None,
        reason: str | None = None, error_type: str | None = None, facts: JsonDict | None = None,
        binding: JsonDict | None = None, before: JsonDict | None = None,
        after: JsonDict | None = None, effect: JsonDict | None = None,
        timing: Mapping[str, int | None] | None = None,
    ) -> None:
        payload = json.dumps(
            {
                "schema": RECLAIM_SCHEMA,
                "kind": kind,
                "outcome": outcome,
                "reason": reason,
                "error_type": error_type,
                "seq": self._seq,
                "instance": evidence.instance,
                "pid": evidence.pid,
                "device_index": evidence.device_index,
                "gate": facts,
                "binding": binding,
                "device_work": ("synchronized" if timing and timing.get("synchronized_ns") is not None
                                else "not_synchronized"),
                "before": before,
                "after": after,
                "effect": effect,
                "timing": {**(timing or {}), "emitted_unix_ns": self._wall_ns()},
                "counts": dict(self._counts),
            },
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        if len(payload.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("owner reclaim record exceeds its byte bound")
        self._seq += 1
        self._logger.log(logging.WARNING if outcome == "failed" else logging.INFO,
                         "%s %s", RECLAIM_LOG_MARKER, payload)


def _heartbeat_line(logger: logging.Logger, level: int, kind: str, **fields: object) -> None:
    payload = json.dumps(
        {"schema": RECLAIM_SCHEMA, "kind": kind, "pid": os.getpid(),
         "observed_at": {"monotonic_ns": time.monotonic_ns(), "emitted_unix_ns": time.time_ns()},
         **fields},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    logger.log(level, "%s %s", RECLAIM_LOG_MARKER, payload)


def _reply_outcome(replies: object) -> str | None:
    """The uni executor answers ``[reply]``; anything else is malformed."""
    if type(replies) is not list or len(replies) != 1 or type(replies[0]) is not dict:
        return None
    reply = replies[0]
    outcome = reply.get("outcome")
    if reply.get("schema") != RECLAIM_SCHEMA or outcome not in _OUTCOMES:
        return None
    return str(outcome)


def _abandon(call: asyncio.Future[Any], logger: logging.Logger, number: int, started: float) -> None:
    """Stop waiting for ``call`` locally, and still observe how it ends.

    Cancellation closes only the API-side await; the owner may still run or
    have run the call. Whatever the abandoned call finally yields is retrieved
    and logged once, so no outcome or late exception goes unobserved.
    """

    def settled(done: asyncio.Future[Any]) -> None:
        if done.cancelled():
            outcome = "cancelled"
        elif (error := done.exception()) is not None:
            outcome = "error:" + error_type(error)
        else:
            outcome = _reply_outcome(done.result()) or "malformed_reply"
        try:
            _heartbeat_line(logger, logging.WARNING, "heartbeat_late_outcome", call=number,
                            outcome=outcome, elapsed_seconds=round(time.monotonic() - started, 3))
        except Exception:  # noqa: BLE001
            # The outcome is already retrieved; a failed log sink has no one to tell.
            return

    call.add_done_callback(settled)
    call.cancel()


async def run_heartbeat(
    engine_client: Any,
    *,
    logger: logging.Logger = LOGGER,
    interval: float = RECLAIM_HEARTBEAT_SECONDS,
    timeout: float = RECLAIM_CALL_TIMEOUT_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Issue the owner's utility call until a terminal condition; return its reason.

    At most one call is ever outstanding. A call unanswered within ``timeout``
    has an unknown outcome: vLLM's uni executor ignores utility timeouts, so the
    owner may still be running it, and abandoning the API-side wait neither
    stops nor undoes that execution (the owner records its own result; the
    abandoned call's local ending is logged when it comes). The heartbeat then
    stops for good, as it does on any failed call, malformed reply or terminal
    owner outcome. A stopped heartbeat is a declared state; only a new API
    process starts another.
    """
    _heartbeat_line(logger, logging.INFO, "heartbeat_start", interval_seconds=interval,
                    call_timeout_seconds=timeout, idle_threshold_ns=RECLAIM_IDLE_NS)
    calls = 0
    while True:
        await sleep(interval)
        if engine_client.errored:
            reason = "engine_errored"
            break
        calls += 1
        started = time.monotonic()
        call = asyncio.ensure_future(engine_client.collective_rpc(RECLAIM_METHOD))
        try:
            await asyncio.wait({call}, timeout=timeout)
        except asyncio.CancelledError:
            # Even a call that finished in the same tick still has its outcome observed.
            _abandon(call, logger, calls, started)
            raise
        if not call.done():
            _heartbeat_line(logger, logging.WARNING, "heartbeat_outcome_unknown", call=calls,
                            elapsed_seconds=round(time.monotonic() - started, 3))
            _abandon(call, logger, calls, started)
            reason = "outcome_unknown"
            break
        error = call.exception()
        if error is not None:
            _heartbeat_line(logger, logging.WARNING, "heartbeat_call_failed", call=calls,
                            error_type=error_type(error))
            reason = "call_failed"
            break
        outcome = _reply_outcome(call.result())
        if outcome is None:
            reason = "malformed_reply"
            break
        if outcome in _TERMINAL_OUTCOMES:
            reason = "owner_" + outcome
            break
    _heartbeat_line(logger, logging.WARNING, "heartbeat_stop", reason=reason, calls=calls)
    return reason


async def _supervised_heartbeat(engine_client: Any) -> None:
    """An unexpected heartbeat error ends the heartbeat visibly, never the server."""
    try:
        await run_heartbeat(engine_client)
    except Exception as error:
        _heartbeat_line(LOGGER, logging.WARNING, "heartbeat_stop", reason="internal",
                        error_type=error_type(error))


class OwnerMemoryReclaimMiddleware:
    """Pass-through ASGI middleware that owns the lifespan-scoped heartbeat.

    The heartbeat starts after the application completed lifespan startup, when
    vLLM has set ``app.state.engine_client``, and is cancelled before the
    application sees the lifespan shutdown message.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app
        self._task: asyncio.Task[None] | None = None

    async def __call__(self, scope: Mapping[str, Any], receive: Callable[[], Awaitable[Any]],
                       send: Callable[[Any], Awaitable[None]]) -> None:
        if scope.get("type") != "lifespan":
            await self.app(scope, receive, send)
            return

        async def lifespan_send(message: Any) -> None:
            await send(message)
            if isinstance(message, Mapping) and message.get("type") == "lifespan.startup.complete":
                self._start(scope)

        async def lifespan_receive() -> Any:
            message = await receive()
            if isinstance(message, Mapping) and message.get("type") == "lifespan.shutdown":
                await self._stop()
            return message

        try:
            await self.app(scope, lifespan_receive, lifespan_send)
        finally:
            # A failed or cancelled lifespan never leaves the heartbeat running.
            await self._stop()

    def _start(self, scope: Mapping[str, Any]) -> None:
        if self._task is not None:
            return
        state = getattr(scope.get("app"), "state", None)
        engine_client = getattr(state, "engine_client", None)
        if engine_client is None or not callable(getattr(engine_client, "collective_rpc", None)):
            _heartbeat_line(LOGGER, logging.WARNING, "heartbeat_unavailable", reason="engine_client")
            return
        self._task = asyncio.get_running_loop().create_task(
            _supervised_heartbeat(engine_client), name="mineru-owner-memory-reclaim-heartbeat")

    async def _stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        # Waiting does not re-raise the heartbeat's cancellation into the
        # lifespan, while a cancellation of the lifespan itself still propagates.
        await asyncio.wait({task})
