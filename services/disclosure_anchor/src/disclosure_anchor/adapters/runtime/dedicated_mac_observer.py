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
import socket
import struct
import threading
import time
from typing import Callable
import uuid

from disclosure_anchor.adapters.runtime.mac_observer_identity import MacObserverIdentityReader
from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import (
    SynchronizedObserverResult, run_synchronized_telemetry_observer,
    verify_synchronized_telemetry_observer,
)
from disclosure_anchor.application.contracts.resident_session_evidence import check_mac_observer_identity
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    FrozenApiProcessProfile, SynchronizedTelemetryReceiptV3,
)
from disclosure_anchor.application.ports.synchronized_telemetry import ResidentTelemetryCollectorSpec


@dataclass(frozen=True, slots=True)
class DedicatedMacObserverRequest:
    artifact_root: Path
    process_profile: FrozenApiProcessProfile
    gpu_collector: ResidentTelemetryCollectorSpec
    host_collector: ResidentTelemetryCollectorSpec
    duration_seconds: float
    run_id: str
    gpu_interval_ms: int = 250

    def __post_init__(self) -> None:
        if not isinstance(self.process_profile, FrozenApiProcessProfile):
            raise ValueError("dedicated Mac observer requires a frozen v3 API profile")
        if not math.isfinite(self.duration_seconds) or not 0 < self.duration_seconds <= 7100:
            raise ValueError("dedicated Mac observer duration is invalid")
        if self.gpu_interval_ms not in {250, 500} or str(uuid.UUID(self.run_id)) != self.run_id:
            raise ValueError("dedicated Mac observer cadence/run ID is invalid")
        if not self.artifact_root.is_absolute():
            raise ValueError("dedicated Mac observer requires an absolute private artifact root")


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
        run_synchronized_telemetry_observer(
            artifact_root=request.artifact_root, process_profile=request.process_profile,
            observer_identity=identity, gpu_collector=request.gpu_collector,
            host_collector=request.host_collector, duration_seconds=request.duration_seconds,
            gpu_interval_ms=request.gpu_interval_ms, run_id=request.run_id, cancel_event=cancel,
        )
        stop.set()
        watcher.join(timeout=1)
        if watcher.is_alive():
            raise RuntimeError("Mac observer control watcher failed to stop")
        if protocol_error.is_set():
            raise ValueError("Mac observer received an invalid control message")
        if reader() != initial:
            raise ValueError("Mac observer kernel identity changed after sampling")
        _send_identity(channel, initial)
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
        if not self._closed and self.poll(timeout=15) is None:
            # Never kill only the observer and silently orphan separate groups.
            raise RuntimeError(f"Mac observer cooperative cleanup unresolved; owned PID {self.pid}, run {self.request.run_id}")
