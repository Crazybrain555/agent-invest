"""Explicit, default-off Mac observer owner with an independently checked GO.

The socket carries only bounded identity/GO/cancel messages, not frame payloads.
The existing runner owns and reaps its collectors. Killing this parent alone
would not kill their separate process groups: failed cooperative cleanup is an
unresolved ownership error, never normal exit or an overhead qualification.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import multiprocessing
from pathlib import Path
import re
import socket
import struct
import threading
import time
from typing import Callable, Literal, cast
import uuid

from disclosure_anchor.adapters.runtime.mac_observer_identity import MacObserverIdentityReader
from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import (
    SynchronizedObserverResult, SynchronizedTelemetryCollectionFailed, SynchronizedTelemetryFailureResult,
    read_synchronized_telemetry_terminal, run_synchronized_telemetry_observer,
    verify_synchronized_telemetry_observer,
)
from disclosure_anchor.application.contracts.resident_session_evidence import check_mac_observer_identity
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    FrozenApiProcessProfile, SynchronizedSamplingPlanV1, SynchronizedTelemetryReceiptV3,
    SynchronizedTelemetryReceiptV4,
)
from disclosure_anchor.application.ports.synchronized_telemetry import ResidentTelemetryCollectorSpec
from disclosure_anchor.application.services.resident_measurement_policy import SAMPLE_MAX_SECONDS

_EVENT_MAX_BYTES = 4096
_EVENT_KINDS = ("plan_recorded", "sampling_drained")
# A started control record must complete within this bound. The child writes each
# record with one bounded sendall over a local socketpair; a longer gap is a broken
# channel, never an idle one.
_CONTROL_RECORD_SECONDS = 2.0
_SHA256_TEXT = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
# Closed record shapes: exact keys and exact scalar types per kind.
_EVENT_SHAPES: dict[str, dict[str, type]] = {
    "plan_recorded": {
        "kind": str, "run_id": str, "sampling_plan_sha256": str,
        "started_monotonic_ns": int, "planned_end_monotonic_ns": int,
    },
    "sampling_drained": {
        "kind": str, "run_id": str, "sampling_plan_sha256": str, "drained_monotonic_ns": int,
        "frames_jsonl_sha256": str, "frames_bytes": int, "frames_records": int,
    },
}


@dataclass(frozen=True, slots=True)
class DedicatedMacObserverRequest:
    artifact_root: Path
    process_profile: FrozenApiProcessProfile
    gpu_collector: ResidentTelemetryCollectorSpec
    host_collector: ResidentTelemetryCollectorSpec
    duration_seconds: float
    run_id: str
    gpu_interval_ms: int = 250
    # R22: version 4 freezes the sampling plan from the owner intent and reports
    # plan_recorded/sampling_drained over the existing control socket.
    receipt_version: Literal[3, 4] = 3
    owner_intent_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.process_profile, FrozenApiProcessProfile):
            raise ValueError("dedicated Mac observer requires a frozen v3 API profile")
        if (isinstance(self.duration_seconds, bool) or not isinstance(self.duration_seconds, (int, float))
                or not math.isfinite(self.duration_seconds) or not 0 < self.duration_seconds <= SAMPLE_MAX_SECONDS):
            raise ValueError("dedicated Mac observer duration is invalid")
        if self.gpu_interval_ms not in {250, 500} or str(uuid.UUID(self.run_id)) != self.run_id:
            raise ValueError("dedicated Mac observer cadence/run ID is invalid")
        if not self.artifact_root.is_absolute():
            raise ValueError("dedicated Mac observer requires an absolute private artifact root")
        if self.receipt_version not in (3, 4):
            raise ValueError("dedicated Mac observer receipt version is invalid")
        if (self.receipt_version == 4) != (self.owner_intent_sha256 is not None):
            raise ValueError("the v4 observer requires exactly the owner intent identity")


def _read_exact(channel: socket.socket, count: int, deadline: float) -> bytes:
    parts = bytearray()
    while len(parts) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Mac observer control message deadline")
        channel.settimeout(remaining)
        piece = channel.recv(count - len(parts))
        if not piece:
            raise EOFError("Mac observer control channel closed")
        parts.extend(piece)
    return bytes(parts)


def _read_identity(channel: socket.socket, *, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    size = struct.unpack("!I", _read_exact(channel, 4, deadline))[0]
    if not 0 < size <= 4096:
        raise ValueError("Mac observer identity message byte bound")
    payload = _read_exact(channel, size, deadline)
    check_mac_observer_identity(payload)
    return payload


def _send_identity(channel: socket.socket, payload: bytes) -> None:
    check_mac_observer_identity(payload)
    if len(payload) > 4096:
        raise ValueError("Mac observer identity message byte bound")
    channel.settimeout(2)
    channel.sendall(struct.pack("!I", len(payload)) + payload)


def _send_event(channel: socket.socket, event: dict[str, object]) -> None:
    payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > _EVENT_MAX_BYTES:
        raise ValueError("Mac observer control event byte bound")
    channel.settimeout(2)
    channel.sendall(struct.pack("!I", len(payload)) + payload)


class _ControlRecordReader:
    """Length-prefixed control records, consumed exactly once and never re-parsed.

    A record that arrives in pieces across idle polls keeps its buffer and the
    deadline set by its first byte; an idle poll that sees no byte of a new
    record returns None. A started record that misses its own deadline fails
    once, explicitly, and the reader stays failed. The reader never receives
    more bytes than the current record needs, so the child's final identity
    message stays intact for the exit path.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._record_deadline: float | None = None
        self._failed = False

    @property
    def partial(self) -> bool:
        return bool(self._buffer)

    def _needed(self) -> int:
        if len(self._buffer) < 4:
            return 4
        size = struct.unpack("!I", bytes(self._buffer[:4]))[0]
        if not 0 < size <= _EVENT_MAX_BYTES:
            self._failed = True
            raise ValueError("Mac observer control message byte bound")
        return 4 + size

    def poll(self, channel: socket.socket, *, timeout: float) -> bytes | None:
        if self._failed:
            raise RuntimeError("Mac observer control channel already failed")
        poll_deadline = time.monotonic() + timeout
        while True:
            needed = self._needed()
            if len(self._buffer) >= 4 and len(self._buffer) == needed:
                payload = bytes(self._buffer[4:])
                self._buffer.clear()
                self._record_deadline = None
                return payload
            now = time.monotonic()
            if self._record_deadline is not None and now >= self._record_deadline:
                self._failed = True
                raise TimeoutError("Mac observer control record did not complete within its deadline")
            if now >= poll_deadline:
                return None
            until = poll_deadline if self._record_deadline is None else min(poll_deadline, self._record_deadline)
            channel.settimeout(max(until - now, 0.001))
            try:
                piece = channel.recv(needed - len(self._buffer))
            except TimeoutError:
                continue
            if not piece:
                self._failed = True
                raise EOFError("Mac observer control channel closed")
            if not self._buffer:
                self._record_deadline = time.monotonic() + _CONTROL_RECORD_SECONDS
            self._buffer.extend(piece)


def _decode_event(payload: bytes, *, run_id: str) -> dict[str, object]:
    """One closed control record: exact keys and scalar types per kind, this run only."""
    value = strict_json_loads(payload)
    if type(value) is not dict or value.get("kind") not in _EVENT_SHAPES:
        raise ValueError("Mac observer control event is invalid or names a foreign run")
    shape = _EVENT_SHAPES[cast(str, value["kind"])]
    if set(value) != set(shape) or any(type(value[key]) is not expected for key, expected in shape.items()):
        raise ValueError("Mac observer control event is not the closed record shape")
    if value["run_id"] != run_id:
        raise ValueError("Mac observer control event is invalid or names a foreign run")
    for key, item in value.items():
        if (isinstance(item, str) and key.endswith("_sha256") and not _SHA256_TEXT.match(item)) or (isinstance(item, int) and item < 0):
            raise ValueError("Mac observer control event carries an invalid identity or count")
    if value["kind"] == "plan_recorded" and cast(int, value["planned_end_monotonic_ns"]) <= cast(int, value["started_monotonic_ns"]):
        raise ValueError("Mac observer plan event has an empty or inverted window")
    return value


def _watch_owner(channel: socket.socket, stop: threading.Event, cancel: threading.Event, protocol_error: threading.Event) -> None:
    # EOF is also cancellation, including an abrupt owner exit. This thread is
    # joined before child exit. It never reads frames or starts another process.
    while not stop.is_set():
        try:
            channel.settimeout(0.1)
            command = channel.recv(1)
        except TimeoutError:
            continue
        except OSError:
            cancel.set()
            return
        cancel.set()
        if command not in {b"", b"C"}:
            # Invalid control is never accepted as a normal sampling completion.
            protocol_error.set()
        return


def _observer_child(
    channel: socket.socket, request: DedicatedMacObserverRequest,
    observe: Callable[[], bytes] | None = None,
) -> None:
    reader = MacObserverIdentityReader().observe if observe is None else observe
    stop, cancel, protocol_error = threading.Event(), threading.Event(), threading.Event()
    watcher: threading.Thread | None = None
    try:
        initial = reader()
        identity = check_mac_observer_identity(initial)
        _send_identity(channel, initial)
        # No collector or telemetry artifact exists before the independent GO.
        if _read_exact(channel, 2, time.monotonic() + 30) != b"GO":
            raise ValueError("Mac observer did not receive its start gate")
        if reader() != initial:
            raise ValueError("Mac observer kernel identity changed before sampling")
        for spec in (request.gpu_collector, request.host_collector):
            config = json.loads(spec.canonical_config_json)
            if config.get("observer_clock_domain_identity_sha256") != identity.clock_domain_identity_sha256:
                raise ValueError("Mac observer collector clock binding differs")
        watcher = threading.Thread(target=_watch_owner, args=(channel, stop, cancel, protocol_error), name="mac-observer-owner-watch", daemon=False)
        watcher.start()
        callbacks: dict[str, object] = {}
        if request.receipt_version == 4:
            def plan_recorded(plan: SynchronizedSamplingPlanV1, payload: bytes, sha256: str) -> None:
                _send_event(channel, {
                    "kind": "plan_recorded", "run_id": plan.run_id, "sampling_plan_sha256": sha256,
                    "started_monotonic_ns": plan.started_monotonic_ns,
                    "planned_end_monotonic_ns": plan.planned_end_monotonic_ns,
                })
            callbacks = {
                "receipt_version": 4, "owner_intent_sha256": request.owner_intent_sha256,
                "on_plan_recorded": plan_recorded, "on_sampling_drained": lambda event: _send_event(channel, event),
            }
        negative: SynchronizedTelemetryCollectionFailed | None = None
        try:
            run_synchronized_telemetry_observer(
                artifact_root=request.artifact_root, process_profile=request.process_profile,
                observer_identity=identity, gpu_collector=request.gpu_collector,
                host_collector=request.host_collector, duration_seconds=request.duration_seconds,
                gpu_interval_ms=request.gpu_interval_ms, run_id=request.run_id, cancel_event=cancel,
                **callbacks,  # type: ignore[arg-type]
            )
        except SynchronizedTelemetryCollectionFailed as exc:
            # The negative terminal is written and replayed; this child still exits non-zero.
            # Its final identity is sent so the owner can bind the failure to the same process.
            negative = exc
        stop.set()
        watcher.join(timeout=1)
        if watcher.is_alive():
            raise RuntimeError("Mac observer control watcher failed to stop")
        if protocol_error.is_set():
            raise ValueError("Mac observer received an invalid control message")
        if reader() != initial:
            raise ValueError("Mac observer kernel identity changed after sampling")
        _send_identity(channel, initial)
        if negative is not None:
            raise negative
    finally:
        stop.set()
        cancel.set()
        if watcher is not None:
            watcher.join(timeout=1)
            if watcher.is_alive():
                raise RuntimeError("Mac observer control watcher cleanup unresolved")
        channel.close()


class DedicatedMacObserver:
    """Outer owner; one fresh child, one independent kernel identity, one GO.

    Construct after the finite remote lanes' READY/actual-source checks, then
    invoke start() promptly within their leases. poll() bounds the process wait;
    normal exit then requires a bounded identity read and anchored file replay.
    The v4 protocol splits that into poll_event() (plan_recorded /
    sampling_drained, no replay), wait_exit() (real exit + final identity) and
    replay_result() (the expensive replay, after the native sources are closed).
    """

    def __init__(self, request: DedicatedMacObserverRequest) -> None:
        self.request = request
        self._reader = MacObserverIdentityReader()
        self._channel, child = socket.socketpair()
        self._process = multiprocessing.get_context("spawn").Process(
            target=_observer_child, args=(child, request), name="dedicated-mac-telemetry-observer", daemon=False,
        )
        self._started = False
        self._closed = False
        self._launched = False
        self._result: SynchronizedObserverResult | None = None
        self._events: list[dict[str, object]] = []
        self._control = _ControlRecordReader()
        self._exit_verified = False
        self._exit_code: int | None = None
        self._terminal: SynchronizedObserverResult | SynchronizedTelemetryFailureResult | None = None
        try:
            self._process.start()
            self._launched = True
            child.close()
            self.identity_bytes = _read_identity(self._channel, timeout=10)
            pid = self._process.pid
            if pid is None or self._reader.observe(pid) != self.identity_bytes:
                raise ValueError("Mac observer child identity differs from independent kernel read")
            process = json.loads(self.identity_bytes)["process"]
            if process["pid"] != pid or process["parent_pid"] != multiprocessing.current_process().pid:
                raise ValueError("Mac observer child PID/parent differs")
        except BaseException:
            child.close()
            self._channel.close()  # child cannot pass GO and create collectors
            if self._launched:
                self._process.join(timeout=3)
                if self._process.is_alive():
                    # Safe only here: our GO has never been sent, so no nested
                    # collector can yet exist, including on bootstrap failure.
                    self._process.kill()
                    self._process.join(timeout=2)
                if self._process.is_alive():
                    raise RuntimeError("Mac observer pre-GO process ownership unresolved")
            self._process.close()
            raise

    @property
    def pid(self) -> int:
        return int(json.loads(self.identity_bytes)["process"]["pid"])

    def start(self) -> None:
        if self._started or self._closed:
            raise RuntimeError("Mac observer start gate is new-only")
        if self._reader.observe(self.pid) != self.identity_bytes:
            raise ValueError("Mac observer changed before independent GO")
        self._started = True  # uncertain send must never cause a second GO
        self._channel.settimeout(2)
        self._channel.sendall(b"GO")

    def poll(self, *, timeout: float = 0) -> SynchronizedObserverResult | None:
        if not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ValueError("Mac observer poll bound is invalid")
        if self.request.receipt_version == 4:
            raise RuntimeError("the v4 observer is driven by poll_event/wait_exit/replay_result")
        if self._closed:
            return self._result
        self._process.join(timeout=timeout)
        if self._process.is_alive():
            return None
        exit_code = self._process.exitcode
        try:
            if exit_code != 0 or not self._started:
                raise RuntimeError(f"Mac observer exited without normal completion: {exit_code}")
            if _read_identity(self._channel, timeout=2) != self.identity_bytes:
                raise ValueError("Mac observer final identity differs")
            result = verify_synchronized_telemetry_observer(
                artifact_root=self.request.artifact_root, run_id=self.request.run_id, receipt_version=3,
            )
            if not isinstance(result.receipt, SynchronizedTelemetryReceiptV3) or result.receipt.observer_identity != check_mac_observer_identity(self.identity_bytes):
                raise ValueError("Mac observer sealed identity differs")
            self._result = result
            return result
        finally:
            self._channel.close()
            self._process.close()
            self._closed = True

    @property
    def events(self) -> tuple[dict[str, object], ...]:
        return tuple(self._events)

    def poll_event(self, *, timeout: float) -> dict[str, object] | None:
        """Read the next bounded control event (v4); never replays the JSONL here.

        Returns None when the deadline passes without a complete message. An
        exited child, a foreign run, an unknown kind or an out-of-order event is
        an error, never a silent completion.
        """
        if not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ValueError("Mac observer poll bound is invalid")
        if self.request.receipt_version != 4 or not self._started or self._closed:
            raise RuntimeError("Mac observer control events require a started v4 observer")
        if len(self._events) >= len(_EVENT_KINDS):
            raise RuntimeError("Mac observer already delivered every control event")
        payload = self._control.poll(self._channel, timeout=timeout)
        if payload is None:
            if not self._process.is_alive():
                raise RuntimeError(f"Mac observer exited before its next control event: {self._process.exitcode}")
            return None
        event = _decode_event(payload, run_id=self.request.run_id)
        expected_kind = _EVENT_KINDS[len(self._events)]
        if event["kind"] != expected_kind:
            raise ValueError(f"Mac observer control event out of order: expected {expected_kind}")
        if self._events and event["sampling_plan_sha256"] != self._events[0]["sampling_plan_sha256"]:
            raise ValueError("Mac observer control events name different sampling plans")
        self._events.append(event)
        return event

    @property
    def exit_code(self) -> int | None:
        """The child's real exit code once it has been reaped; None while it runs."""
        return self._exit_code

    def wait_exit(self, *, timeout: float) -> bool:
        """Bounded wait for the real child exit and its final identity (v4); no replay.

        A non-zero exit is recorded, not raised: the child keeps exit 1 on its
        negative terminal and still sends its final identity, so the terminal
        reader (``replay_terminal``) decides what the run directory proves.
        Exiting before the drain announcement, an unconsumed partial control
        record or a differing final identity remain protocol errors.
        """
        if not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ValueError("Mac observer poll bound is invalid")
        if self.request.receipt_version != 4 or self._closed:
            raise RuntimeError("Mac observer exit wait requires a live v4 observer")
        self._process.join(timeout=timeout)
        if self._process.is_alive():
            return False
        exit_code = self._process.exitcode
        self._exit_code = exit_code
        try:
            if not self._started:
                raise RuntimeError(f"Mac observer exited before GO: {exit_code}")
            if len(self._events) != len(_EVENT_KINDS):
                raise RuntimeError(f"Mac observer exited ({exit_code}) before announcing its sampling drain")
            if self._control.partial:
                raise ValueError("Mac observer exited with an unconsumed partial control record")
            if _read_identity(self._channel, timeout=2) != self.identity_bytes:
                raise ValueError("Mac observer final identity differs")
            self._exit_verified = True
            return True
        finally:
            self._channel.close()
            self._process.close()
            self._closed = True

    def replay_terminal(self, *, deadline_ns: int | None = None) -> SynchronizedObserverResult | SynchronizedTelemetryFailureResult:
        """The one terminal the run directory holds, bound to this child's exit and drain.

        Exit 0 must pair with the normal sealed terminal and a non-zero exit with
        the negative one; either other combination is a contradiction, never a
        pass. The normal branch is exactly ``replay_result``.
        """
        if self.request.receipt_version != 4 or not self._exit_verified:
            raise RuntimeError("Mac observer replay requires a verified v4 exit")
        if self._terminal is not None:
            return self._terminal
        if self._exit_code == 0:
            self._terminal = self.replay_result(deadline_ns=deadline_ns)
            return self._terminal
        result = read_synchronized_telemetry_terminal(
            artifact_root=self.request.artifact_root, run_id=self.request.run_id, deadline_monotonic_ns=deadline_ns,
        )
        if not isinstance(result, SynchronizedTelemetryFailureResult):
            raise ValueError(f"Mac observer exited {self._exit_code} but its run directory holds a sealed normal terminal")
        if result.receipt.observer_identity != check_mac_observer_identity(self.identity_bytes):
            raise ValueError("Mac observer failure terminal identity differs")
        drained = self._events[1]
        # The drain named the physical bytes at the time of the announcement. A run whose
        # normal path drained first and then failed keeps that first announcement, so its
        # frame hash describes the same physical file only when no bytes were appended after
        # it (the writer had already closed the stream); a negative drain names the physical
        # file directly. Either way the physical hash and byte count must agree.
        if (result.receipt.sampling_plan_sha256 != self._events[0]["sampling_plan_sha256"]
                or result.receipt.raw_frames.sha256 != drained["frames_jsonl_sha256"]
                or result.receipt.raw_frames.physical_bytes != drained["frames_bytes"]):
            raise ValueError("Mac observer failure terminal differs from its announced drain")
        if result.receipt.raw_frames.complete_records != drained["frames_records"] and result.receipt.writer_problem is None:
            raise ValueError("Mac observer failure terminal record count differs from its announced drain without a recorded writer problem")
        self._terminal = result
        return result

    def replay_result(self, *, deadline_ns: int | None = None) -> SynchronizedObserverResult:
        """The expensive anchored replay, after the native sources were closed.

        ``deadline_ns`` is the owner's absolute local deadline (plan end plus
        tail); the frame parse checks it every 64 rows.
        """
        if self.request.receipt_version != 4 or not self._exit_verified:
            raise RuntimeError("Mac observer replay requires a verified v4 exit")
        if self._exit_code != 0:
            raise RuntimeError(f"Mac observer exited {self._exit_code}; only replay_terminal may read its negative terminal")
        if self._result is not None:
            return self._result
        result = verify_synchronized_telemetry_observer(
            artifact_root=self.request.artifact_root, run_id=self.request.run_id, receipt_version=4,
            deadline_monotonic_ns=deadline_ns,
        )
        receipt = result.receipt
        if not isinstance(receipt, SynchronizedTelemetryReceiptV4) or receipt.observer_identity != check_mac_observer_identity(self.identity_bytes):
            raise ValueError("Mac observer sealed identity differs")
        drained = self._events[1]
        if (receipt.sampling_plan_sha256 != self._events[0]["sampling_plan_sha256"]
                or receipt.artifacts.frames_jsonl_sha256 != drained["frames_jsonl_sha256"]):
            raise ValueError("Mac observer sealed evidence differs from its announced drain")
        self._result = result
        return result

    def cancel(self) -> None:
        if self._closed:
            return
        # A half-close communicates EOF even when sending C is impossible. The
        # reader direction remains open for the child's final bounded identity.
        try:
            self._channel.shutdown(socket.SHUT_WR)
        except OSError:
            if self._process.is_alive():
                raise

    def close(self) -> None:
        self.cancel()
        if self._closed:
            return
        if self.request.receipt_version == 4:
            self._process.join(timeout=15)
            alive = self._process.is_alive()
            if not alive:
                exit_code = self._process.exitcode
                self._exit_code = exit_code
                self._channel.close()
                self._process.close()
                self._closed = True
                if exit_code != 0:
                    raise RuntimeError(f"Mac observer exited without normal completion: {exit_code}")
                return
        elif self.poll(timeout=15) is not None:
            return
        # Never kill only the observer and silently orphan separate groups.
        raise RuntimeError(f"Mac observer cooperative cleanup unresolved; owned PID {self.pid}, run {self.request.run_id}")
