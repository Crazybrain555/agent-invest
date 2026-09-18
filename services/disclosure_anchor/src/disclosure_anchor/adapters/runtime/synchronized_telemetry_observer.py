"""Default-off resident dual-lane synchronized telemetry observer.

This module owns only local scheduling and immutable evidence.  Concrete GPU and
host collectors are injected; no runtime service, database or worker is touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import importlib
import json
import math
import multiprocessing
from multiprocessing.connection import Connection
import os
from pathlib import Path
import queue
import signal
import stat
import subprocess
import sys
import threading
import time
import traceback
from typing import Callable, Literal, cast
import uuid

from pydantic import ValidationError

import disclosure_anchor.application.contracts.synchronized_telemetry as contract_module
import disclosure_anchor.application.ports.synchronized_telemetry as port_module
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    ApiProcessObservationV2,
    BoundedObserverErrorV1,
    CollectionFailureV1,
    RawFramesPrefixV1,
    SynchronizedTelemetryFailureReceiptV1,
    SynchronizedTelemetryFailureSealV1,
    bounded_observer_error,
    derive_prefix_coverage_v1,
    GpuObservationV2,
    HostCgroupObservationV2,
    QueueVllmObservationV2,
    SampleClock,
    SampleQuality,
    ResidentExporterPullProvenance,
    SynchronizedSamplingPlanV1,
    SynchronizedTelemetryFrameV2,
    SynchronizedTelemetryFrameV3,
    SynchronizedTelemetryReceiptV2,
    SynchronizedTelemetryReceiptV3,
    SynchronizedTelemetryReceiptV4,
    SynchronizedTelemetrySealV2,
    SynchronizedTelemetrySealV3,
    SynchronizedTelemetrySealV4,
    FrozenApiProcessProfile,
    TelemetryObserverIdentity,
    TelemetryArtifactsV2,
    canonical_jsonl_artifact_sha256,
    derive_frame_evidence,
    derive_frame_evidence_v4,
    parse_canonical_json_artifact,
    parse_canonical_jsonl_artifact,
    sampling_seal_denominator_ns,
)
from disclosure_anchor.application.services.resident_measurement_policy import (
    NS as _POLICY_NS,
    SAMPLE_MAX_SECONDS as _POLICY_SAMPLE_MAX_SECONDS,
)
from disclosure_anchor.application.ports.synchronized_telemetry import (
    GpuLaneSnapshot,
    HostLaneSnapshot,
    ResidentTelemetryCollectorSpec,
    TelemetrySampleIdentity,
    TelemetrySnapshotDeadline,
    TelemetrySnapshotDeadlineExceeded,
    TelemetrySnapshotContinuityLost,
    TelemetrySnapshotTransportUnavailable,
)


MAX_FRAME_RECORDS = 1_000_000
MAX_FRAME_FILE_BYTES = 256 * 1024 * 1024
MAX_FRAME_RECORD_BYTES = 256 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
FRAME_FILENAME = "frames.v2.jsonl"
RECEIPT_FILENAME = "receipt.v2.json"
SEAL_FILENAME = "seal.v2.json"
FRAME_V3_FILENAME = "frames.v3.jsonl"
PLAN_FILENAME = "sampling-plan.v1.json"
# R23 negative-only terminal of a v4 run (never selected for v2/v3).
FAILURE_RECEIPT_FILENAME = "receipt.failure.v1.json"
FAILURE_SEAL_FILENAME = "seal.failure.v1.json"
# Lane threads react to internal_stop within one poll (50 ms) or one bounded snapshot
# (at most one nominal interval); a longer join means the run cannot prove quiesce.
_LANE_JOIN_SECONDS = 5.0
_CLOCK_PAIR_ATTEMPTS = 3
_MAX_CLOCK_PAIR_BRACKET_NS = 10_000_000

TelemetryReceipt = SynchronizedTelemetryReceiptV2 | SynchronizedTelemetryReceiptV3 | SynchronizedTelemetryReceiptV4
TelemetrySeal = SynchronizedTelemetrySealV2 | SynchronizedTelemetrySealV3 | SynchronizedTelemetrySealV4
TelemetryFrame = SynchronizedTelemetryFrameV2 | SynchronizedTelemetryFrameV3
ApiProfile = contract_module.ProcessProfileLifecycle | FrozenApiProcessProfile


@dataclass(frozen=True, slots=True)
class _ArtifactProtocol:
    receipt_filename: str
    seal_filename: str
    receipt_model: type[SynchronizedTelemetryReceiptV2] | type[SynchronizedTelemetryReceiptV3] | type[SynchronizedTelemetryReceiptV4]
    seal_model: type[SynchronizedTelemetrySealV2] | type[SynchronizedTelemetrySealV3] | type[SynchronizedTelemetrySealV4]
    frames_filename: str = FRAME_FILENAME
    frame_model: type[SynchronizedTelemetryFrameV2] | type[SynchronizedTelemetryFrameV3] = SynchronizedTelemetryFrameV2
    # Only the R22 protocol freezes a sampling plan file before the first collect.
    plan_filename: str | None = None


_V2 = _ArtifactProtocol(RECEIPT_FILENAME, SEAL_FILENAME, SynchronizedTelemetryReceiptV2, SynchronizedTelemetrySealV2)
_V3 = _ArtifactProtocol("receipt.v3.json", "seal.v3.json", SynchronizedTelemetryReceiptV3, SynchronizedTelemetrySealV3)
_V4 = _ArtifactProtocol(
    "receipt.v4.json", "seal.v4.json", SynchronizedTelemetryReceiptV4, SynchronizedTelemetrySealV4,
    frames_filename=FRAME_V3_FILENAME, frame_model=SynchronizedTelemetryFrameV3, plan_filename=PLAN_FILENAME,
)


def _artifact_protocol(version: Literal[2, 3, 4]) -> _ArtifactProtocol:
    if type(version) is not int or version not in (2, 3, 4):
        raise ValueError("explicit supported observer receipt version required")
    return {2: _V2, 3: _V3, 4: _V4}[version]


class ObserverState(str, Enum):
    INIT = "INIT"
    PREFLIGHT = "PREFLIGHT"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    SEALED = "SEALED"
    FAILED_EVIDENCE = "FAILED_EVIDENCE"


class SynchronizedTelemetryTerminalAbsent(ValueError):
    """Neither a complete normal nor a complete failure terminal exists for the run."""


class SynchronizedTelemetryEvidenceError(RuntimeError):
    """Raised when exact observer evidence cannot be durably sealed."""


@dataclass(frozen=True, slots=True)
class SynchronizedObserverLimits:
    mailbox_records_per_lane: int = 32
    maximum_frame_records: int = MAX_FRAME_RECORDS
    maximum_frame_file_bytes: int = MAX_FRAME_FILE_BYTES
    maximum_frame_record_bytes: int = MAX_FRAME_RECORD_BYTES
    maximum_receipt_bytes: int = MAX_RECEIPT_BYTES

    def __post_init__(self) -> None:
        if self.mailbox_records_per_lane < 1:
            raise ValueError("telemetry mailbox bound must be positive")
        if self.maximum_frame_records < 2:
            raise ValueError("telemetry frame bound must cover both lanes")
        for value in (
            self.maximum_frame_file_bytes,
            self.maximum_frame_record_bytes,
            self.maximum_receipt_bytes,
        ):
            if value < 1:
                raise ValueError("telemetry byte bounds must be positive")


@dataclass(frozen=True, slots=True)
class SynchronizedObserverResult:
    state: Literal[ObserverState.SEALED]
    run_directory: Path
    receipt: TelemetryReceipt
    seal: TelemetrySeal
    frames: tuple[TelemetryFrame, ...]
    plan: SynchronizedSamplingPlanV1 | None = None

    @property
    def evidence_status(self) -> contract_module.RunStatus:
        """Authoritative final status, including observer-overhead attestation."""

        return self.seal.status


@dataclass(frozen=True, slots=True)
class SynchronizedTelemetryFailureResult:
    """A verified negative-only terminal: no measurement, no credit, original errors retained."""

    run_directory: Path
    receipt: SynchronizedTelemetryFailureReceiptV1
    seal: SynchronizedTelemetryFailureSealV1
    plan: SynchronizedSamplingPlanV1
    prefix_frames: tuple[SynchronizedTelemetryFrameV3, ...]

    @property
    def evidence_status(self) -> Literal["failed"]:
        return "failed"


class SynchronizedTelemetryCollectionFailed(SynchronizedTelemetryEvidenceError):
    """The v4 run ended on its negative terminal; the failure receipt/seal are written and replayed."""

    def __init__(self, result: SynchronizedTelemetryFailureResult) -> None:
        super().__init__(f"synchronized telemetry collection failed: {result.receipt.reason}")
        self.result = result

    @property
    def receipt(self) -> SynchronizedTelemetryFailureReceiptV1:
        return self.result.receipt

    @property
    def seal(self) -> SynchronizedTelemetryFailureSealV1:
        return self.result.seal

    @property
    def run_directory(self) -> Path:
        return self.result.run_directory


class _NegativeTerminal(Exception):
    """Internal: the merge ended with recorded collection failures; take the negative path."""


@dataclass(frozen=True, slots=True)
class _ClockPair:
    wall: datetime
    monotonic_ns: int
    bracket_ns: int


def _capture_clock_pair(
    *,
    monotonic_ns: Callable[[], int],
    utc_now: Callable[[], datetime],
) -> _ClockPair:
    """Capture the tightest bounded wall/monotonic pair without sleeping."""

    best: _ClockPair | None = None
    previous_after: int | None = None
    for _ in range(_CLOCK_PAIR_ATTEMPTS):
        before = monotonic_ns()
        if isinstance(before, bool) or not isinstance(before, int) or before < 0:
            raise ValueError("monotonic clock returned an invalid value")
        if previous_after is not None and before < previous_after:
            raise ValueError("monotonic clock regressed between pairing attempts")
        wall = utc_now()
        _require_utc(wall)
        after = monotonic_ns()
        if isinstance(after, bool) or not isinstance(after, int) or after < before:
            raise ValueError("monotonic clock regressed during clock pairing")
        previous_after = after
        bracket = after - before
        candidate = _ClockPair(
            wall=wall,
            monotonic_ns=before + bracket // 2,
            bracket_ns=bracket,
        )
        if best is None or candidate.bracket_ns < best.bracket_ns:
            best = candidate
    assert best is not None
    if best.bracket_ns > _MAX_CLOCK_PAIR_BRACKET_NS:
        raise SynchronizedTelemetryEvidenceError(
            "wall/monotonic clock pair exceeded its sampling bound"
        )
    return best


_RECEIPT_CLOCK_DIVERGENCE_ERROR = "wall and monotonic receipt clocks diverged"


def _is_receipt_clock_divergence_validation_error(exc: ValidationError) -> bool:
    """Match only the receipt validator's closed clock-divergence failure."""

    for error in exc.errors(include_url=False, include_input=False):
        if error.get("type") != "value_error":
            continue
        context = error.get("ctx")
        if not isinstance(context, dict):
            continue
        if str(context.get("error")) == _RECEIPT_CLOCK_DIVERGENCE_ERROR:
            return True
    return False


def _receipt_clock_diagnostic_note(
    *,
    receipt_model: type[SynchronizedTelemetryReceiptV2]
    | type[SynchronizedTelemetryReceiptV3]
    | type[SynchronizedTelemetryReceiptV4],
    start_clock: _ClockPair,
    finish_clock: _ClockPair,
) -> str | None:
    """Format only bounded clock facts using the selected receipt model limits."""

    fixed_field = receipt_model.model_fields.get("maximum_clock_divergence_fixed_ns")
    ppm_field = receipt_model.model_fields.get("maximum_clock_divergence_ppm")
    if fixed_field is None or ppm_field is None:
        return None
    fixed_value = fixed_field.default
    ppm_value = ppm_field.default
    if (
        isinstance(fixed_value, bool)
        or not isinstance(fixed_value, int)
        or isinstance(ppm_value, bool)
        or not isinstance(ppm_value, int)
    ):
        return None
    fixed_ns = cast(int, fixed_value)
    ppm = cast(int, ppm_value)
    wall_elapsed_ns = int(
        (finish_clock.wall - start_clock.wall).total_seconds() * 1_000_000_000
    )
    monotonic_elapsed_ns = finish_clock.monotonic_ns - start_clock.monotonic_ns
    clock_divergence_ns = abs(wall_elapsed_ns - monotonic_elapsed_ns)
    maximum_clock_divergence_ns = (
        fixed_ns + monotonic_elapsed_ns * ppm // 1_000_000
    )
    fields = (
        ("receipt_model", receipt_model.__name__),
        ("started_at_utc", start_clock.wall.isoformat()),
        ("finished_at_utc", finish_clock.wall.isoformat()),
        ("started_monotonic_ns", start_clock.monotonic_ns),
        ("finished_monotonic_ns", finish_clock.monotonic_ns),
        ("start_clock_bracket_ns", start_clock.bracket_ns),
        ("finish_clock_bracket_ns", finish_clock.bracket_ns),
        ("wall_elapsed_ns", wall_elapsed_ns),
        ("monotonic_elapsed_ns", monotonic_elapsed_ns),
        ("clock_divergence_ns", clock_divergence_ns),
        ("maximum_clock_divergence_fixed_ns", fixed_ns),
        ("maximum_clock_divergence_ppm", ppm),
        ("maximum_clock_divergence_ns", maximum_clock_divergence_ns),
    )
    return "synchronized telemetry receipt clock diagnostics: " + "; ".join(
        f"{name}={value}" for name, value in fields
    )


@dataclass(frozen=True, slots=True)
class _PendingSample:
    lane: Literal["gpu_fast", "host_slow"]
    scheduled_monotonic_ns: int
    started_monotonic_ns: int
    finished_monotonic_ns: int
    observed_at_utc: datetime
    resident_exporter_provenance: port_module.ResidentProvenance | None
    gpu: GpuObservationV2
    api_process: ApiProcessObservationV2
    host_cgroup: HostCgroupObservationV2
    queue_vllm: QueueVllmObservationV2


@dataclass(frozen=True, slots=True)
class _CollectionFailure:
    """A local, witness-less failure that stopped one lane (v4 only); never a sample."""

    lane: Literal["gpu_fast", "host_slow"]
    category: contract_module.CollectionFailureCategory
    scheduled_monotonic_ns: int
    started_monotonic_ns: int
    finished_monotonic_ns: int
    exception: BaseException
    traceback_text: str


_MailboxItem = _PendingSample | _CollectionFailure


@dataclass(slots=True)
class _Mailbox:
    values: queue.Queue[_MailboxItem]
    done: threading.Event
    fallback: _MailboxItem | None = None
    started: threading.Event = field(default_factory=threading.Event)
    watermark_monotonic_ns: int = -1
    lock: threading.Lock = field(default_factory=threading.Lock)
    failure: BaseException | None = None


class _Termination:
    _PRIORITY = {
        "duration_elapsed": 0,
        "cancelled": 1,
        "sampler_or_transport_shutdown": 2,
        "queue_overflow": 3,
        "artifact_bound_exceeded": 4,
        "identity_drift": 5,
    }

    def __init__(self) -> None:
        self._value = "duration_elapsed"
        self._lock = threading.Lock()

    def mark(self, value: str) -> None:
        with self._lock:
            if self._PRIORITY[value] > self._PRIORITY[self._value]:
                self._value = value

    def value(self) -> str:
        with self._lock:
            return self._value


class _SafetyDrifts:
    def __init__(self) -> None:
        self._values: set[contract_module.SafetyDriftReason] = set()
        self._lock = threading.Lock()

    def add(self, value: contract_module.SafetyDriftReason) -> None:
        with self._lock:
            self._values.add(value)

    def values(self) -> tuple[contract_module.SafetyDriftReason, ...]:
        with self._lock:
            return tuple(sorted(self._values))


@dataclass(slots=True)
class _CounterTracker:
    previous: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True, slots=True)
class _ArtifactStat:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int


class _InvocationCancelled(Exception):
    pass


class _ResidentSamplerProcess:
    """One owned resident sampler process with a killable request transport."""

    def __init__(self, spec: ResidentTelemetryCollectorSpec, *, label: str) -> None:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        self._connection: Connection | None = parent
        self._process_group_ready = False
        self._started = False
        self._process_closed = False
        self._process = context.Process(
            target=_resident_sampler_main,
            args=(
                child,
                spec.factory_module,
                spec.factory_qualname,
                spec.canonical_config_json,
            ),
            name=f"synchronized-telemetry-{label}-collector",
            daemon=False,
        )
        try:
            self._process.start()
            self._started = True
            child.close()
            if not parent.poll(5.0):
                raise TelemetrySnapshotTransportUnavailable(
                    "resident collector READY handshake timed out"
                )
            kind, payload = parent.recv()
            if kind != "ready":
                self._process_group_ready = os.name == "posix"
                raise TelemetrySnapshotTransportUnavailable(
                    f"resident collector bootstrap failed: {payload}"
                )
            identity, process_group_ready, descendants = payload
            self._process_group_ready = bool(process_group_ready)
            if identity != spec.expected_collector_identity_sha256:
                raise TelemetrySnapshotTransportUnavailable(
                    "resident collector identity handshake drifted"
                )
            if descendants or self._unexpected_process_group_members():
                raise TelemetrySnapshotTransportUnavailable(
                    "resident collector violated no-descendants capability"
                )
        except BaseException:
            child.close()
            self._terminate()
            parent.close()
            self._connection = None
            self._close_process_handle()
            raise

    def snapshot(
        self,
        *,
        deadline: TelemetrySnapshotDeadline,
        cancel_event: threading.Event,
        monotonic_ns: Callable[[], int],
    ) -> GpuLaneSnapshot | HostLaneSnapshot:
        connection = self._connection
        if connection is None or not self._started or not self._process.is_alive():
            raise TelemetrySnapshotTransportUnavailable("resident collector is unavailable")
        connection.send(("snapshot", deadline))
        while True:
            if cancel_event.is_set():
                self._terminate()
                raise _InvocationCancelled
            remaining_ns = deadline.monotonic_ns - monotonic_ns()
            if remaining_ns <= 0:
                self._terminate()
                raise TelemetrySnapshotDeadlineExceeded("resident snapshot deadline exceeded")
            if not connection.poll(min(remaining_ns / 1_000_000_000, 0.05)):
                continue
            try:
                kind, payload = connection.recv()
            except (EOFError, OSError) as exc:
                self._terminate()
                raise TelemetrySnapshotTransportUnavailable(
                    "resident collector transport closed"
                ) from exc
            if monotonic_ns() > deadline.monotonic_ns:
                self._terminate()
                raise TelemetrySnapshotDeadlineExceeded("resident snapshot returned late")
            if kind == "ok":
                return cast(GpuLaneSnapshot | HostLaneSnapshot, payload)
            if kind == "deadline":
                raise TelemetrySnapshotDeadlineExceeded(str(payload))
            if kind == "transport":
                raise TelemetrySnapshotTransportUnavailable(str(payload))
            if kind == "continuity":
                self._terminate()
                raise TelemetrySnapshotContinuityLost(str(payload))
            if kind == "assertion":
                raise AssertionError(str(payload))
            if kind == "capability":
                self._terminate()
                raise RuntimeError(f"resident collector capability violation: {payload}")
            raise RuntimeError(f"resident collector program error: {payload}")

    def close(self) -> None:
        if self._process_closed:
            return
        connection = self._connection
        if connection is not None and self._started and self._process.is_alive():
            try:
                connection.send(("close", None))
            except (BrokenPipeError, EOFError, OSError):
                pass
            self._process.join(timeout=0.25)
        if self._started and self._process.is_alive():
            self._terminate()
        else:
            unexpected = self._quiesce_process_group()
            if unexpected:
                raise RuntimeError(
                    f"resident collector capability violation: {unexpected}"
                )
        if connection is not None:
            connection.close()
            self._connection = None
        self._close_process_handle()

    def _terminate(self) -> None:
        if not self._started:
            return
        if os.name == "posix" and self._process_group_ready and self._process.pid:
            _signal_process_group(self._process.pid, signal.SIGTERM)
        elif self._process.is_alive():
            self._process.terminate()
        if self._process.is_alive():
            self._process.join(timeout=1)
        self._quiesce_process_group()
        if self._process.is_alive():
            if os.name == "posix" and self._process_group_ready and self._process.pid:
                _signal_process_group(self._process.pid, signal.SIGKILL)
            else:
                self._process.kill()
            self._process.join(timeout=1)
            self._quiesce_process_group()
        if self._process.is_alive():
            raise SynchronizedTelemetryEvidenceError("resident collector did not terminate")

    def _close_process_handle(self) -> None:
        if self._process_closed:
            return
        if self._started and self._process.is_alive():
            return
        self._process.close()
        self._process_closed = True

    def _unexpected_process_group_members(self) -> tuple[int, ...]:
        if os.name != "posix" or not self._process_group_ready or not self._process.pid:
            return ()
        return tuple(
            pid for pid in _posix_process_group_members(self._process.pid)
            if pid != self._process.pid
        )

    def _quiesce_process_group(self) -> tuple[int, ...]:
        if os.name != "posix" or not self._process_group_ready or not self._process.pid:
            return ()
        deadline = time.monotonic() + 1.0
        members = _posix_process_group_members(self._process.pid)
        unexpected = tuple(pid for pid in members if pid != self._process.pid)
        if members:
            _signal_process_group(self._process.pid, signal.SIGTERM)
        while members and time.monotonic() < deadline:
            time.sleep(0.02)
            members = _posix_process_group_members(self._process.pid)
        if members:
            _signal_process_group(self._process.pid, signal.SIGKILL)
            deadline = time.monotonic() + 1.0
            while members and time.monotonic() < deadline:
                time.sleep(0.02)
                members = _posix_process_group_members(self._process.pid)
        if members:
            raise SynchronizedTelemetryEvidenceError(
                f"resident collector process group did not quiesce: {members}"
            )
        return unexpected


def _signal_process_group(process_group_id: int, signal_number: int) -> None:
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        pass


def _posix_process_group_members(process_group_id: int) -> tuple[int, ...]:
    """Return live non-zombie members without creating a process in the target group."""

    completed = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,stat="],
        check=True,
        capture_output=True,
        text=True,
        timeout=1,
    )
    members: list[int] = []
    for line in completed.stdout.splitlines():
        columns = line.split()
        if len(columns) < 3:
            continue
        pid_text, pgid_text, state = columns[:3]
        if int(pgid_text) == process_group_id and not state.startswith("Z"):
            members.append(int(pid_text))
    return tuple(sorted(members))


def _resident_sampler_main(
    connection: Connection,
    factory_module: str,
    factory_qualname: str,
    canonical_config_json: bytes,
) -> None:
    try:
        process_group_ready = False
        if os.name == "posix":
            os.setsid()
            process_group_ready = True
        config = parse_canonical_json_artifact(
            canonical_config_json,
            label="collector config",
            maximum_bytes=64 * 1024,
        )
        if not isinstance(config, dict):
            raise ValueError("collector config must be an object")
        factory: object = importlib.import_module(factory_module)
        for component in factory_qualname.split("."):
            if component == "<locals>":
                raise ValueError("collector factory must be top-level")
            factory = getattr(factory, component)
        sampler = factory(config)  # type: ignore[operator]
        identity = getattr(sampler, "collector_identity_sha256", None)
        if not isinstance(identity, str):
            raise ValueError("collector factory omitted identity")
        descendants = tuple(child.pid for child in multiprocessing.active_children())
        connection.send(("ready", (identity, process_group_ready, descendants)))
        while True:
            command, payload = connection.recv()
            if command == "close":
                return
            try:
                value = sampler.snapshot(deadline=payload)
                descendants = tuple(child.pid for child in multiprocessing.active_children())
                if descendants:
                    connection.send(("capability", descendants))
                else:
                    connection.send(("ok", value))
            except TelemetrySnapshotDeadlineExceeded as exc:
                connection.send(("deadline", str(exc)))
            except TelemetrySnapshotTransportUnavailable as exc:
                connection.send(("transport", str(exc)))
            except TelemetrySnapshotContinuityLost as exc:
                connection.send(("continuity", str(exc)))
            except AssertionError as exc:
                connection.send(("assertion", str(exc)))
            except BaseException as exc:
                connection.send(("program", f"{type(exc).__name__}: {exc}"))
    except (EOFError, BrokenPipeError, OSError):
        return
    except BaseException as exc:
        try:
            connection.send(("bootstrap_error", f"{type(exc).__name__}: {exc}"))
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


class _FrameWriter:
    def __init__(
        self,
        *,
        artifact_root: Path,
        run_id: str,
        limits: SynchronizedObserverLimits,
        protocol: _ArtifactProtocol = _V2,
    ) -> None:
        self._limits = limits
        self._protocol = protocol
        parent_fd, root_fd = _open_or_create_private_directory(artifact_root)
        self._parent_fd: int | None = parent_fd
        self._root_fd: int | None = root_fd
        self._root_name = artifact_root.name
        self._run_id = run_id
        self._root_stat = _directory_stat_fd(root_fd)
        try:
            os.mkdir(run_id, mode=0o700, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
            self._run_fd: int | None = os.open(
                run_id,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self._root_fd,
            )
        except Exception:
            _close_descriptor(self._root_fd)
            _close_descriptor(self._parent_fd)
            self._root_fd = None
            self._parent_fd = None
            raise
        self.run_directory = artifact_root / run_id
        if self._run_fd is None:
            raise SynchronizedTelemetryEvidenceError(
                "telemetry run descriptor is unavailable"
            )
        try:
            _validate_private_directory_fd(self._run_fd, label="telemetry run")
            self._run_stat = _directory_stat_fd(self._run_fd)
            self._frames_fd: int | None = _open_new_private_at(
                self._run_fd, protocol.frames_filename
            )
        except Exception:
            _close_descriptor(self._run_fd)
            _close_descriptor(self._root_fd)
            _close_descriptor(self._parent_fd)
            self._run_fd = None
            self._root_fd = None
            self._parent_fd = None
            raise
        self._frame_hash = hashlib.sha256()
        self._frame_count = 0
        self._frame_bytes = 0
        self._frames_closed = False
        self._closed = False
        self._artifact_stats: dict[str, _ArtifactStat] = {}
        self._artifact_fds: dict[str, int] = {protocol.frames_filename: self._frames_fd}

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def frame_bytes(self) -> int:
        return self._frame_bytes

    @property
    def frames_sha256(self) -> str:
        return "sha256:" + self._frame_hash.hexdigest()

    def write_plan(self, plan: SynchronizedSamplingPlanV1) -> tuple[bytes, str]:
        """Freeze the sampling plan as a new-only private file before any collect."""
        if self._protocol.plan_filename is None:
            raise SynchronizedTelemetryEvidenceError("this observer protocol has no sampling plan file")
        if self._frame_count or self._frames_closed or self._run_fd is None or self._root_fd is None:
            raise SynchronizedTelemetryEvidenceError("sampling plan must be frozen before the first frame")
        payload = _canonical_json_bytes(plan.model_dump(mode="json"))
        if len(payload) > self._limits.maximum_receipt_bytes:
            raise _ArtifactBoundExceeded("sampling plan exceeds its bound")
        descriptor = _open_new_private_at(self._run_fd, self._protocol.plan_filename)
        self._artifact_fds[self._protocol.plan_filename] = descriptor
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.fsync(self._run_fd)
        os.fsync(self._root_fd)
        observed, metadata = self._bind_written_descriptor(
            self._protocol.plan_filename, descriptor, maximum_bytes=self._limits.maximum_receipt_bytes,
        )
        if observed != payload:
            raise SynchronizedTelemetryEvidenceError("sampling plan write bytes drifted")
        self._artifact_stats[self._protocol.plan_filename] = metadata
        return payload, "sha256:" + hashlib.sha256(payload).hexdigest()

    def append(self, frame: TelemetryFrame) -> None:
        if self._frames_closed:
            raise SynchronizedTelemetryEvidenceError("telemetry frame stream is closed")
        if self._frames_fd is None:
            raise SynchronizedTelemetryEvidenceError(
                "telemetry frame descriptor is unavailable"
            )
        payload = _canonical_json_bytes(frame.model_dump(mode="json")) + b"\n"
        if (
            len(payload) > self._limits.maximum_frame_record_bytes
            or self._frame_count + 1 > self._limits.maximum_frame_records
            or self._frame_bytes + len(payload)
            > self._limits.maximum_frame_file_bytes
        ):
            raise _ArtifactBoundExceeded("telemetry frame stream exceeds its bound")
        _write_all(self._frames_fd, payload)
        self._frame_hash.update(payload)
        self._frame_count += 1
        self._frame_bytes += len(payload)

    def close_frames(self) -> str:
        if self._frames_closed:
            raise SynchronizedTelemetryEvidenceError("telemetry frame stream already closed")
        if self._frames_fd is None or self._run_fd is None:
            raise SynchronizedTelemetryEvidenceError(
                "telemetry frame seal descriptors are unavailable"
            )
        os.fsync(self._frames_fd)
        self._frames_closed = True
        os.fsync(self._run_fd)
        payload, metadata = self._bind_written_descriptor(
            self._protocol.frames_filename,
            self._frames_fd,
            maximum_bytes=self._limits.maximum_frame_file_bytes,
        )
        if "sha256:" + hashlib.sha256(payload).hexdigest() != "sha256:" + self._frame_hash.hexdigest():
            raise SynchronizedTelemetryEvidenceError("telemetry frame write bytes drifted")
        self._artifact_stats[self._protocol.frames_filename] = metadata
        return "sha256:" + self._frame_hash.hexdigest()

    def write_receipt(self, receipt: TelemetryReceipt) -> bytes:
        if not self._frames_closed:
            raise SynchronizedTelemetryEvidenceError(
                "telemetry receipt cannot precede the frame seal"
            )
        if self._run_fd is None or self._root_fd is None:
            raise SynchronizedTelemetryEvidenceError(
                "telemetry receipt descriptors are unavailable"
            )
        payload = _canonical_json_bytes(receipt.model_dump(mode="json"))
        if len(payload) > self._limits.maximum_receipt_bytes:
            raise _ArtifactBoundExceeded("telemetry receipt exceeds its bound")
        descriptor = _open_new_private_at(self._run_fd, self._protocol.receipt_filename)
        self._artifact_fds[self._protocol.receipt_filename] = descriptor
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.fsync(self._run_fd)
        os.fsync(self._root_fd)
        observed, metadata = self._bind_written_descriptor(
            self._protocol.receipt_filename,
            descriptor,
            maximum_bytes=self._limits.maximum_receipt_bytes,
        )
        if observed != payload:
            raise SynchronizedTelemetryEvidenceError("telemetry receipt write bytes drifted")
        self._artifact_stats[self._protocol.receipt_filename] = metadata
        return payload

    def replay_unsealed(
        self,
    ) -> tuple[tuple[TelemetryFrame, ...], TelemetryReceipt]:
        if self._run_fd is None:
            raise SynchronizedTelemetryEvidenceError("telemetry run descriptor is unavailable")
        self._validate_anchors()
        return self._replay_open_descriptors(expect_seal=False)[:2]

    # --- R23 negative terminal ---------------------------------------------------

    def close_frames_physical(self) -> bytes:
        """Fsync and read back whatever bytes the frame stream physically holds.

        Unlike ``close_frames`` this does not require the bytes to equal the
        running hash: a failed run keeps its physical file, including any
        partial trailing record, and the caller accounts for the complete prefix.
        """
        if self._frames_fd is None or self._run_fd is None:
            raise SynchronizedTelemetryEvidenceError("telemetry frame descriptors are unavailable")
        if not self._frames_closed:
            os.fsync(self._frames_fd)
            self._frames_closed = True
            os.fsync(self._run_fd)
        payload, metadata = self._bind_written_descriptor(
            self._protocol.frames_filename, self._frames_fd, maximum_bytes=self._limits.maximum_frame_file_bytes,
        )
        self._artifact_stats[self._protocol.frames_filename] = metadata
        return payload

    def unsealed_positive_artifacts(self) -> dict[str, str]:
        """A positive receipt written before the run turned negative, by exact digest; never a seal."""
        retained: dict[str, str] = {}
        if self._protocol.seal_filename in self._artifact_fds:
            raise SynchronizedTelemetryEvidenceError("a sealed positive terminal cannot be joined by a failure terminal")
        descriptor = self._artifact_fds.get(self._protocol.receipt_filename)
        if descriptor is not None:
            payload, _metadata = _read_private_descriptor(descriptor, maximum_bytes=self._limits.maximum_receipt_bytes)
            retained[self._protocol.receipt_filename] = "sha256:" + hashlib.sha256(payload).hexdigest()
        return retained

    def write_failure_receipt(self, receipt: SynchronizedTelemetryFailureReceiptV1) -> bytes:
        if not self._frames_closed:
            raise SynchronizedTelemetryEvidenceError("telemetry failure receipt cannot precede the physical frame close")
        return self._write_named(FAILURE_RECEIPT_FILENAME, _canonical_json_bytes(receipt.model_dump(mode="json")))

    def write_failure_seal(self, seal: SynchronizedTelemetryFailureSealV1) -> None:
        if FAILURE_RECEIPT_FILENAME not in self._artifact_fds:
            raise SynchronizedTelemetryEvidenceError("telemetry failure seal cannot precede its receipt")
        observed = self._write_named(FAILURE_SEAL_FILENAME, _canonical_json_bytes(seal.model_dump(mode="json")))
        parsed = SynchronizedTelemetryFailureSealV1.model_validate(
            parse_canonical_json_artifact(observed, label="failure seal", maximum_bytes=MAX_RECEIPT_BYTES)
        )
        if parsed != seal:
            raise SynchronizedTelemetryEvidenceError("telemetry failure seal parsed bytes drifted")

    def replay_failure_terminal(self) -> SynchronizedTelemetryFailureResult:
        """Read every negative-terminal file back through its own write descriptor and re-verify."""
        if self._run_fd is None or self._protocol.plan_filename is None:
            raise SynchronizedTelemetryEvidenceError("telemetry run descriptor is unavailable")
        self._validate_anchors()
        expected = [self._protocol.frames_filename, self._protocol.plan_filename, FAILURE_RECEIPT_FILENAME, FAILURE_SEAL_FILENAME]
        if self._protocol.receipt_filename in self._artifact_fds:
            expected.append(self._protocol.receipt_filename)
        names_before = sorted(os.listdir(self._run_fd))
        if names_before != sorted(expected):
            raise SynchronizedTelemetryEvidenceError("telemetry failure terminal artifact set drifted")
        payloads: dict[str, bytes] = {}
        for filename in expected:
            maximum = self._limits.maximum_frame_file_bytes if filename == self._protocol.frames_filename else self._limits.maximum_receipt_bytes
            payload, metadata = _read_private_descriptor(
                self._artifact_fds[filename], maximum_bytes=maximum, expected_stat=self._artifact_stats[filename],
            )
            if _private_file_stat_at(self._run_fd, filename, maximum_bytes=maximum) != metadata:
                raise SynchronizedTelemetryEvidenceError("telemetry artifact name drifted from its write descriptor")
            payloads[filename] = payload
        if sorted(os.listdir(self._run_fd)) != names_before:
            raise SynchronizedTelemetryEvidenceError("telemetry run directory changed during descriptor replay")
        result = _parse_failure_payloads(
            frames_payload=payloads[self._protocol.frames_filename], plan_payload=payloads[self._protocol.plan_filename],
            receipt_payload=payloads[FAILURE_RECEIPT_FILENAME], seal_payload=payloads[FAILURE_SEAL_FILENAME],
            unsealed={name: payloads[name] for name in expected if name == self._protocol.receipt_filename},
            run_directory=self.run_directory,
        )
        self._validate_anchors()
        self._close_success()
        return result

    def _write_named(self, filename: str, payload: bytes) -> bytes:
        if self._run_fd is None or self._root_fd is None:
            raise SynchronizedTelemetryEvidenceError("telemetry artifact descriptors are unavailable")
        if len(payload) > self._limits.maximum_receipt_bytes:
            raise _ArtifactBoundExceeded(f"{filename} exceeds its bound")
        descriptor = _open_new_private_at(self._run_fd, filename)
        self._artifact_fds[filename] = descriptor
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.fsync(self._run_fd)
        os.fsync(self._root_fd)
        observed, metadata = self._bind_written_descriptor(filename, descriptor, maximum_bytes=self._limits.maximum_receipt_bytes)
        if observed != payload:
            raise SynchronizedTelemetryEvidenceError(f"{filename} write bytes drifted")
        self._artifact_stats[filename] = metadata
        return observed

    def write_seal(self, seal: TelemetrySeal) -> None:
        if self._run_fd is None or self._root_fd is None:
            raise SynchronizedTelemetryEvidenceError("telemetry seal descriptors are unavailable")
        payload = _canonical_json_bytes(seal.model_dump(mode="json"))
        if len(payload) > self._limits.maximum_receipt_bytes:
            raise _ArtifactBoundExceeded("telemetry seal exceeds its bound")
        descriptor = _open_new_private_at(self._run_fd, self._protocol.seal_filename)
        self._artifact_fds[self._protocol.seal_filename] = descriptor
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.fsync(self._run_fd)
        os.fsync(self._root_fd)
        observed, metadata = self._bind_written_descriptor(
            self._protocol.seal_filename,
            descriptor,
            maximum_bytes=self._limits.maximum_receipt_bytes,
        )
        if observed != payload:
            raise SynchronizedTelemetryEvidenceError("telemetry seal write bytes drifted")
        parsed = self._protocol.seal_model.model_validate(
            parse_canonical_json_artifact(observed, label="seal", maximum_bytes=MAX_RECEIPT_BYTES)
        )
        if parsed != seal:
            raise SynchronizedTelemetryEvidenceError("telemetry seal parsed bytes drifted")
        self._artifact_stats[self._protocol.seal_filename] = metadata

    def replay_sealed(self) -> SynchronizedObserverResult:
        if self._run_fd is None:
            raise SynchronizedTelemetryEvidenceError("telemetry run descriptor is unavailable")
        self._validate_anchors()
        frames, receipt, seal, plan = self._replay_open_descriptors(expect_seal=True)
        self._validate_anchors()
        if seal is None:
            raise SynchronizedTelemetryEvidenceError("telemetry seal is missing")
        if receipt.run_id != self._run_id:
            raise SynchronizedTelemetryEvidenceError("telemetry receipt run identity drifted")
        if receipt.observer_source_sha256 != synchronized_observer_source_sha256():
            raise SynchronizedTelemetryEvidenceError("telemetry observer source identity drifted")
        result = SynchronizedObserverResult(
            state=ObserverState.SEALED,
            run_directory=self.run_directory,
            receipt=receipt,
            seal=seal,
            frames=frames,
            plan=plan,
        )
        self._close_success()
        return result

    def _bind_written_descriptor(
        self, filename: str, descriptor: int, *, maximum_bytes: int
    ) -> tuple[bytes, _ArtifactStat]:
        payload, metadata = _read_private_descriptor(
            descriptor, maximum_bytes=maximum_bytes
        )
        if self._run_fd is None:
            raise SynchronizedTelemetryEvidenceError("telemetry run descriptor is unavailable")
        named = _private_file_stat_at(
            self._run_fd, filename, maximum_bytes=maximum_bytes
        )
        if named != metadata:
            raise SynchronizedTelemetryEvidenceError(
                "telemetry artifact name no longer identifies its write descriptor"
            )
        return payload, metadata

    def _replay_open_descriptors(
        self, *, expect_seal: bool
    ) -> tuple[
        tuple[TelemetryFrame, ...],
        TelemetryReceipt,
        TelemetrySeal | None,
        SynchronizedSamplingPlanV1 | None,
    ]:
        if self._run_fd is None:
            raise SynchronizedTelemetryEvidenceError("telemetry run descriptor is unavailable")
        expected_names = [self._protocol.frames_filename, self._protocol.receipt_filename]
        if self._protocol.plan_filename is not None:
            expected_names.append(self._protocol.plan_filename)
        if expect_seal:
            expected_names.append(self._protocol.seal_filename)
        names_before = sorted(os.listdir(self._run_fd))
        if names_before != sorted(expected_names):
            raise SynchronizedTelemetryEvidenceError("telemetry run artifact set drifted")
        payloads: dict[str, bytes] = {}
        for filename in expected_names:
            descriptor = self._artifact_fds[filename]
            maximum = (
                self._limits.maximum_frame_file_bytes
                if filename == self._protocol.frames_filename
                else self._limits.maximum_receipt_bytes
            )
            payload, metadata = _read_private_descriptor(
                descriptor,
                maximum_bytes=maximum,
                expected_stat=self._artifact_stats[filename],
            )
            named = _private_file_stat_at(
                self._run_fd, filename, maximum_bytes=maximum
            )
            if named != metadata:
                raise SynchronizedTelemetryEvidenceError(
                    "telemetry artifact name drifted from its write descriptor"
                )
            payloads[filename] = payload
        if sorted(os.listdir(self._run_fd)) != names_before:
            raise SynchronizedTelemetryEvidenceError(
                "telemetry run directory changed during descriptor replay"
            )
        return _parse_replay_payloads(
            payloads[self._protocol.frames_filename],
            payloads[self._protocol.receipt_filename],
            payloads.get(self._protocol.seal_filename),
            protocol=self._protocol,
            plan_payload=None if self._protocol.plan_filename is None else payloads[self._protocol.plan_filename],
        )

    def _validate_anchors(self) -> None:
        if self._parent_fd is None or self._root_fd is None or self._run_fd is None:
            raise SynchronizedTelemetryEvidenceError("telemetry anchor descriptors are unavailable")
        root = _directory_stat_at(self._parent_fd, self._root_name, label="telemetry root")
        run = _directory_stat_at(self._root_fd, self._run_id, label="telemetry run")
        if root != self._root_stat or run != self._run_stat:
            raise SynchronizedTelemetryEvidenceError("telemetry directory anchor was replaced")

    def _close_success(self) -> None:
        for descriptor in self._artifact_fds.values():
            _close_descriptor(descriptor)
        self._artifact_fds.clear()
        self._frames_fd = None
        _close_descriptor(self._run_fd)
        _close_descriptor(self._root_fd)
        _close_descriptor(self._parent_fd)
        self._run_fd = None
        self._root_fd = None
        self._parent_fd = None
        self._closed = True

    def abort(self) -> None:
        if self._closed:
            return
        if not self._frames_closed and self._frames_fd is not None:
            try:
                try:
                    os.fsync(self._frames_fd)
                except OSError:
                    pass
            finally:
                _close_descriptor(self._frames_fd)
                self._frames_fd = None
            self._frames_closed = True
        for descriptor in self._artifact_fds.values():
            _close_descriptor(descriptor)
        self._artifact_fds.clear()
        self._frames_fd = None
        _close_descriptor(self._run_fd)
        _close_descriptor(self._root_fd)
        _close_descriptor(self._parent_fd)
        self._run_fd = None
        self._root_fd = None
        self._parent_fd = None
        self._closed = True


class _ArtifactBoundExceeded(ValueError):
    pass


def synchronized_observer_source_sha256() -> str:
    """Bind evidence to the exact runner, port and closed-contract sources."""

    digest = hashlib.sha256()
    paths = (
        Path(__file__),
        Path(cast(str, contract_module.__file__)),
        Path(cast(str, port_module.__file__)),
    )
    for path in sorted(paths, key=lambda item: str(item)):
        payload = path.read_bytes()
        name = path.name.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def sampling_duration_ns(duration_seconds: float) -> int:
    """Exact integer nanoseconds from the owner's declared seconds; no float accumulation."""
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)) or not math.isfinite(duration_seconds):
        raise ValueError("telemetry duration must be a finite number")
    try:
        scaled = Decimal(str(duration_seconds)) * _POLICY_NS
    except InvalidOperation as exc:
        raise ValueError("telemetry duration is not representable") from exc
    if scaled != scaled.to_integral_value() or scaled <= 0:
        raise ValueError("telemetry duration must be a positive whole number of nanoseconds")
    duration_ns = int(scaled)
    if duration_ns > _POLICY_SAMPLE_MAX_SECONDS * _POLICY_NS:
        raise ValueError("telemetry duration exceeds the finite measurement ceiling")
    return duration_ns


def _observer_process_tree_cpu_ns() -> int:
    """Cumulative parent plus reaped collector CPU for the pre-seal interval."""

    total = time.process_time_ns()
    if os.name == "posix":
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        total += int((usage.ru_utime + usage.ru_stime) * 1_000_000_000)
    return total


def run_synchronized_telemetry_observer(
    *,
    artifact_root: Path,
    process_profile: ApiProfile,
    observer_identity: TelemetryObserverIdentity | None = None,
    gpu_collector: ResidentTelemetryCollectorSpec,
    host_collector: ResidentTelemetryCollectorSpec,
    duration_seconds: float,
    gpu_interval_ms: int = 250,
    run_id: str | None = None,
    cancel_event: threading.Event | None = None,
    limits: SynchronizedObserverLimits | None = None,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    process_cpu_ns: Callable[[], int] = lambda: _observer_process_tree_cpu_ns(),
    receipt_version: Literal[3, 4] | None = None,
    owner_intent_sha256: str | None = None,
    on_plan_recorded: Callable[[SynchronizedSamplingPlanV1, bytes, str], None] | None = None,
    on_sampling_drained: Callable[[dict[str, object]], None] | None = None,
) -> SynchronizedObserverResult:
    """Run independent lanes; explicit separate identities select receipt v3.

    Legacy v2 keeps its original model and interpretation for deterministic
    replay. A real cross-host owner must provide both a FrozenApiProcessProfile
    and an independently bound TelemetryObserverIdentity; no mixed fallback.
    ``receipt_version=4`` (R22) additionally freezes the sampling plan from the
    owner intent before the first collect, writes frame v3 with fresh-pull
    witnesses, scores slots against the plan and announces ``sampling_drained``
    before the expensive replay; it is never selected implicitly.
    """

    state = ObserverState.INIT
    if receipt_version not in (None, 3, 4):
        raise ValueError("explicit supported observer receipt version required")
    if observer_identity is None:
        if receipt_version is not None:
            raise ValueError("an explicit receipt version requires a separate observer identity")
        if not isinstance(process_profile, contract_module.ProcessProfileLifecycle):
            raise ValueError("v3 API profile requires a separate observer identity")
        protocol = _V2
        observer_clock_domain = process_profile.clock_domain_identity_sha256
    else:
        if not isinstance(process_profile, FrozenApiProcessProfile):
            raise ValueError("v3 observer identity requires a clock-free API profile")
        protocol = _V4 if receipt_version == 4 else _V3
        observer_clock_domain = observer_identity.clock_domain_identity_sha256
    if not 250 <= gpu_interval_ms <= 500:
        raise ValueError("GPU telemetry cadence must be 250-500ms")
    if not math.isfinite(duration_seconds) or not duration_seconds > 0:
        raise ValueError("telemetry duration must be positive")
    duration_ns: int | None = None
    if protocol is _V4:
        if owner_intent_sha256 is None or not contract_module._SHA256_RE.fullmatch(owner_intent_sha256):
            raise ValueError("the v4 observer requires the owner intent identity")
        if gpu_interval_ms not in (250, 500):
            raise ValueError("the v4 plan freezes a 250 or 500 ms GPU cadence")
        duration_ns = sampling_duration_ns(duration_seconds)
    elif owner_intent_sha256 is not None or on_plan_recorded is not None or on_sampling_drained is not None:
        raise ValueError("plan/drain callbacks belong to the v4 observer only")
    resolved_run_id = str(uuid.UUID(run_id)) if run_id is not None else str(uuid.uuid4())
    if resolved_run_id != (run_id or resolved_run_id):
        raise ValueError("run_id is not canonical")
    resolved_limits = limits or SynchronizedObserverLimits()
    state = ObserverState.PREFLIGHT
    try:
        writer = _FrameWriter(
            artifact_root=artifact_root,
            run_id=resolved_run_id,
            limits=resolved_limits,
            protocol=protocol,
        )
    except Exception as exc:
        raise SynchronizedTelemetryEvidenceError(
            "synchronized telemetry evidence failed in FAILED_EVIDENCE"
        ) from exc
    internal_stop = threading.Event()
    external_cancel = cancel_event or threading.Event()
    termination = _Termination()
    safety_drifts = _SafetyDrifts()
    counter_tracker = _CounterTracker()
    condition = threading.Condition()
    mailboxes = {
        "gpu_fast": _Mailbox(
            values=queue.Queue(resolved_limits.mailbox_records_per_lane),
            done=threading.Event(),
        ),
        "host_slow": _Mailbox(
            values=queue.Queue(resolved_limits.mailbox_records_per_lane),
            done=threading.Event(),
        ),
    }
    invocations: list[_ResidentSamplerProcess] = []
    try:
        gpu_invocation = _ResidentSamplerProcess(gpu_collector, label="gpu-fast")
        invocations.append(gpu_invocation)
        host_invocation = _ResidentSamplerProcess(host_collector, label="host-slow")
        invocations.append(host_invocation)
    except BaseException as exc:
        for invocation in invocations:
            invocation.close()
        writer.abort()
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise SynchronizedTelemetryEvidenceError(
            "synchronized telemetry evidence failed in FAILED_EVIDENCE"
        ) from exc
    try:
        start_clock = _capture_clock_pair(
            monotonic_ns=monotonic_ns,
            utc_now=utc_now,
        )
        start_wall = start_clock.wall
        start_monotonic = start_clock.monotonic_ns
        process_cpu_started = process_cpu_ns()
        plan: SynchronizedSamplingPlanV1 | None = None
        plan_sha256: str | None = None
        if duration_ns is not None:
            assert owner_intent_sha256 is not None
            plan = SynchronizedSamplingPlanV1(
                run_id=resolved_run_id, owner_intent_sha256=owner_intent_sha256,
                observer_clock_domain_identity_sha256=observer_clock_domain,
                started_monotonic_ns=start_monotonic, duration_ns=duration_ns,
                planned_end_monotonic_ns=start_monotonic + duration_ns,
                gpu_nominal_interval_ms=cast(Literal[250, 500], gpu_interval_ms),
            )
            plan_bytes, plan_sha256 = writer.write_plan(plan)
            if on_plan_recorded is not None:
                on_plan_recorded(plan, plan_bytes, plan_sha256)
            end_deadline = plan.planned_end_monotonic_ns
        else:
            end_deadline = start_monotonic + int(duration_seconds * 1_000_000_000)
        observer_source = synchronized_observer_source_sha256()
    except BaseException as exc:
        for invocation in invocations:
            invocation.close()
        writer.abort()
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise SynchronizedTelemetryEvidenceError(
            "synchronized telemetry evidence failed in FAILED_EVIDENCE"
        ) from exc
    expected_identity = TelemetrySampleIdentity(
        runtime_bundle_identity_sha256=(
            process_profile.runtime_bundle_identity_sha256
        ),
        process_profile_sha256=process_profile.process_profile_sha256,
        clock_domain_identity_sha256=observer_clock_domain,
    )
    frozen_gpu_identity: list[str | None] = [None]
    frozen_cgroup_identity: list[str | None] = [None]
    identity_lock = threading.Lock()
    threads = [
        threading.Thread(
            target=_run_lane,
            kwargs={
                "lane": "gpu_fast",
                "interval_ns": gpu_interval_ms * 1_000_000,
                "sampler": gpu_invocation,
                "mailbox": mailboxes["gpu_fast"],
                "condition": condition,
                "expected_identity": expected_identity,
                "expected_process_epoch": process_profile.process_epoch_sha256,
                "frozen_gpu_identity": frozen_gpu_identity,
                "frozen_cgroup_identity": frozen_cgroup_identity,
                "identity_lock": identity_lock,
                "start_monotonic": start_monotonic,
                "end_deadline": end_deadline,
                "external_cancel": external_cancel,
                "internal_stop": internal_stop,
                "termination": termination,
                "safety_drifts": safety_drifts,
                "counter_tracker": counter_tracker,
                "monotonic_ns": monotonic_ns,
                "utc_now": utc_now,
                "negative_outcomes": protocol is _V4,
            },
            name="synchronized-telemetry-gpu-fast",
            daemon=False,
        ),
        threading.Thread(
            target=_run_lane,
            kwargs={
                "lane": "host_slow",
                "interval_ns": 1_000_000_000,
                "sampler": host_invocation,
                "mailbox": mailboxes["host_slow"],
                "condition": condition,
                "expected_identity": expected_identity,
                "expected_process_epoch": process_profile.process_epoch_sha256,
                "frozen_gpu_identity": frozen_gpu_identity,
                "frozen_cgroup_identity": frozen_cgroup_identity,
                "identity_lock": identity_lock,
                "start_monotonic": start_monotonic,
                "end_deadline": end_deadline,
                "external_cancel": external_cancel,
                "internal_stop": internal_stop,
                "termination": termination,
                "safety_drifts": safety_drifts,
                "counter_tracker": counter_tracker,
                "monotonic_ns": monotonic_ns,
                "utc_now": utc_now,
                "negative_outcomes": protocol is _V4,
            },
            name="synchronized-telemetry-host-slow",
            daemon=False,
        ),
    ]
    frames: list[TelemetryFrame] = []
    collection_failures: list[_CollectionFailure] = []
    state = ObserverState.RUNNING
    started_threads: list[threading.Thread] = []
    negative = protocol is _V4
    drained_sent_ns: int | None = None
    try:
        for thread in threads:
            thread.start()
            started_threads.append(thread)
        _merge_lane_mailboxes(
            mailboxes=mailboxes,
            condition=condition,
            writer=writer,
            frames=frames,
            run_id=resolved_run_id,
            process_profile=process_profile,
            observer_clock_domain=observer_clock_domain,
            observer_source_sha256=observer_source,
            termination=termination,
            internal_stop=internal_stop,
            gpu_interval_ms=gpu_interval_ms,
            protocol=protocol,
            collection_failures=collection_failures if negative else None,
        )
        state = ObserverState.DRAINING
        for thread in started_threads:
            # Lanes leave within one poll or one bounded snapshot; a v4 lane that does not is a
            # quiesce failure the negative terminal records, never an open-ended wait here.
            thread.join(timeout=_LANE_JOIN_SECONDS if negative else None)
            if thread.is_alive():
                raise SynchronizedTelemetryEvidenceError(f"{thread.name} did not stop within {_LANE_JOIN_SECONDS}s")
        for invocation in invocations:
            invocation.close()
        if collection_failures:
            raise _NegativeTerminal
        finish_clock = _capture_clock_pair(
            monotonic_ns=monotonic_ns,
            utc_now=utc_now,
        )
        finish_monotonic = finish_clock.monotonic_ns
        if finish_monotonic < max(
            frame.clock.finished_monotonic_ns for frame in frames
        ):
            raise SynchronizedTelemetryEvidenceError(
                "finish clock pair precedes collected telemetry"
            )
        finish_wall = finish_clock.wall
        frame_digest = writer.close_frames()
        frame_tuple = tuple(frames)
        if plan is not None and on_sampling_drained is not None:
            # Collectors are joined, closed and the JSONL is fsynced: the owner
            # may now close the native sources; the replay below is not waited for.
            # Sent at most once per run: a later failure of this normal path reuses it.
            on_sampling_drained({
                "kind": "sampling_drained", "run_id": resolved_run_id, "sampling_plan_sha256": plan_sha256,
                "drained_monotonic_ns": monotonic_ns(), "frames_jsonl_sha256": frame_digest,
                "frames_bytes": writer.frame_bytes, "frames_records": writer.frame_count,
            })
            drained_sent_ns = monotonic_ns()
        slot_coverage_v4: tuple[contract_module.LaneSlotCoverageV4, contract_module.LaneSlotCoverageV4] | None = None
        if plan is not None:
            lane_quality, slot_coverage_v4, unsupported_count = derive_frame_evidence_v4(
                cast(tuple[SynchronizedTelemetryFrameV3, ...], frame_tuple), plan=plan,
            )
        else:
            lane_quality, unsupported_count = derive_frame_evidence(
                cast(tuple[contract_module.SynchronizedTelemetryFrame, ...], frame_tuple),
                started_monotonic_ns=start_monotonic,
                finished_monotonic_ns=finish_monotonic,
            )
        wall_ns = int((finish_wall - start_wall).total_seconds() * 1_000_000_000)
        monotonic_elapsed_ns = finish_monotonic - start_monotonic
        clock_divergence_ns = abs(wall_ns - monotonic_elapsed_ns)
        termination_reason = termination.value()
        receipt_payload: dict[str, object] = {
            "run_id": resolved_run_id,
            "runtime_bundle_identity_sha256": (
                process_profile.runtime_bundle_identity_sha256
            ),
            "process_profile": process_profile,
            "observer_source_sha256": observer_source,
            "clock_domain_identity_sha256": observer_clock_domain,
            "started_at_utc": start_wall,
            "finished_at_utc": finish_wall,
            "started_monotonic_ns": start_monotonic,
            "finished_monotonic_ns": finish_monotonic,
            "status": (
                "unsafe"
                if termination_reason == "identity_drift"
                else "incomplete"
                if termination_reason != "duration_elapsed"
                or unsupported_count > 0
                or any(
                    quality.late_sample_count > 0
                    or quality.missed_deadline_count > 0
                    or quality.supported_frame_count == 0
                    for quality in lane_quality
                )
                or (slot_coverage_v4 is not None and any(item.missing_slots > 0 for item in slot_coverage_v4))
                else "complete"
            ),
            "lane_quality": lane_quality,
            "termination_reason": termination_reason,
            "observed_clock_divergence_ns": clock_divergence_ns,
            "epoch_changed": "epoch_drift" in safety_drifts.values(),
            "safety_drift_reasons": safety_drifts.values(),
            "unsupported_observation_count": unsupported_count,
            "artifacts": TelemetryArtifactsV2(
                frames_jsonl_sha256=frame_digest,
            ),
        }
        if observer_identity is not None:
            receipt_payload["observer_identity"] = observer_identity
        if plan is not None:
            receipt_payload.update({
                "sampling_plan_sha256": plan_sha256, "duration_ns": plan.duration_ns,
                "planned_end_monotonic_ns": plan.planned_end_monotonic_ns, "slot_coverage": slot_coverage_v4,
            })
        try:
            receipt = protocol.receipt_model.model_validate(receipt_payload)
        except ValidationError as exc:
            if _is_receipt_clock_divergence_validation_error(exc):
                diagnostic_note = _receipt_clock_diagnostic_note(
                    receipt_model=protocol.receipt_model,
                    start_clock=start_clock,
                    finish_clock=finish_clock,
                )
                if diagnostic_note is not None:
                    exc.add_note(diagnostic_note)
            raise
        if isinstance(receipt, SynchronizedTelemetryReceiptV4):
            assert plan is not None
            validate_synchronized_telemetry_v4(cast(tuple[SynchronizedTelemetryFrameV3, ...], frame_tuple), receipt=receipt, plan=plan)
            denominator_ns = sampling_seal_denominator_ns(receipt)
        else:
            validate_synchronized_telemetry_v2(cast(tuple[SynchronizedTelemetryFrameV2, ...], frame_tuple), receipt=receipt)
            denominator_ns = monotonic_elapsed_ns
        receipt_bytes = writer.write_receipt(receipt)
        replay_frames, replay_receipt = writer.replay_unsealed()
        if replay_frames != frame_tuple or replay_receipt != receipt:
            raise SynchronizedTelemetryEvidenceError("mandatory pre-seal replay drifted")
        process_cpu_finished = process_cpu_ns()
        seal_payload: dict[str, object] = {
            "run_id": resolved_run_id,
            "receipt_sha256": "sha256:" + hashlib.sha256(receipt_bytes).hexdigest(),
            "frames_jsonl_sha256": frame_digest,
            "preseal_observer_process_cpu_started_ns": process_cpu_started,
            "preseal_observer_process_cpu_finished_ns": process_cpu_finished,
            "preseal_observer_cpu_ns": process_cpu_finished - process_cpu_started,
            "sampling_elapsed_ns_denominator": denominator_ns,
            "receipt_status": receipt.status,
            "status": (
                "unsafe"
                if receipt.status == "unsafe"
                or (process_cpu_finished - process_cpu_started) / denominator_ns > 0.02
                else receipt.status
            ),
        }
        if plan is not None:
            seal_payload.update({
                "sampling_plan_sha256": plan_sha256, "lifecycle_elapsed_ns": monotonic_elapsed_ns,
                "frames_records": writer.frame_count, "frames_bytes": writer.frame_bytes,
            })
        seal = protocol.seal_model.model_validate(seal_payload)
        writer.write_seal(seal)
        replay = writer.replay_sealed()
        state = ObserverState.SEALED
        return replay
    except BaseException as exc:
        internal_stop.set()
        if isinstance(exc, (KeyboardInterrupt, SystemExit)) or not negative:
            state = ObserverState.FAILED_EVIDENCE
            for thread in started_threads:
                thread.join()
            writer.abort()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise SynchronizedTelemetryEvidenceError(
                f"synchronized telemetry evidence failed in {state.value}"
            ) from exc
        assert plan is not None and plan_sha256 is not None and observer_identity is not None
        raise _negative_terminal(
            primary=None if isinstance(exc, _NegativeTerminal) else exc,
            collection_failures=collection_failures, started_threads=started_threads, invocations=invocations,
            writer=writer, plan=plan, plan_sha256=plan_sha256, process_profile=process_profile,
            observer_identity=observer_identity, observer_source=observer_source,
            observer_clock_domain=observer_clock_domain, run_id=resolved_run_id,
            monotonic_ns=monotonic_ns, on_sampling_drained=on_sampling_drained, drained_sent_ns=drained_sent_ns,
        ) from exc
    finally:
        for invocation in invocations:
            try:
                invocation.close()
            except BaseException as close_error:  # noqa: BLE001 - a repeated close never replaces the terminal error
                if isinstance(close_error, (KeyboardInterrupt, SystemExit)):
                    raise
                in_flight = sys.exception()
                if in_flight is None:
                    raise
                in_flight.add_note(f"collector close after the terminal: {type(close_error).__name__}: {close_error}")


def _negative_terminal(
    *, primary: BaseException | None, collection_failures: list[_CollectionFailure],
    started_threads: list[threading.Thread], invocations: list[_ResidentSamplerProcess], writer: _FrameWriter,
    plan: SynchronizedSamplingPlanV1, plan_sha256: str, process_profile: ApiProfile,
    observer_identity: TelemetryObserverIdentity, observer_source: str, observer_clock_domain: str, run_id: str,
    monotonic_ns: Callable[[], int], on_sampling_drained: Callable[[dict[str, object]], None] | None,
    drained_sent_ns: int | None = None,
) -> SynchronizedTelemetryEvidenceError:
    """Close a failed v4 run on its negative terminal; return the exception the caller raises.

    Order: request lane stop → bounded join → close collectors → fsync the physical
    raw file → announce ``sampling_drained`` (at most once per run, only when
    quiesce and durability are proven, from the writer's own durable byte/record
    facts) → validate the complete prefix → write the failure receipt and seal →
    replay them through their own descriptors. The native close may therefore
    begin before the prefix parse. The planned window is never shortened and
    nothing is invented. If the terminal itself cannot be written, the writer is
    aborted and the original error is reported as FAILED_EVIDENCE.
    """
    secondary: list[BaseException] = []
    quiesced = True
    for thread in started_threads:
        thread.join(timeout=_LANE_JOIN_SECONDS)
        if thread.is_alive():
            quiesced = False
            secondary.append(RuntimeError(f"{thread.name} did not stop within {_LANE_JOIN_SECONDS}s"))
    for invocation in invocations:
        try:
            invocation.close()
        except BaseException as exc:  # noqa: BLE001 - every cleanup outcome is retained beside the primary
            quiesced = False
            secondary.append(exc)
    stopped_ns = monotonic_ns()
    try:
        physical = writer.close_frames_physical()
        physical_sha256 = "sha256:" + hashlib.sha256(physical).hexdigest()
        drained_ns: int | None = drained_sent_ns
        if drained_ns is None and quiesced and on_sampling_drained is not None:
            # The writer's completed-append count is a durable fact, not a guess; the prefix
            # parse below confirms it and any disagreement is recorded as a writer problem.
            on_sampling_drained({
                "kind": "sampling_drained", "run_id": run_id, "sampling_plan_sha256": plan_sha256,
                "drained_monotonic_ns": monotonic_ns(), "frames_jsonl_sha256": physical_sha256,
                "frames_bytes": len(physical), "frames_records": writer.frame_count,
            })
            drained_ns = monotonic_ns()
        prefix_frames, prefix_bytes = _complete_frame_prefix(
            physical, frame_model=SynchronizedTelemetryFrameV3, run_id=run_id,
            runtime_bundle_identity_sha256=process_profile.runtime_bundle_identity_sha256,
            process_profile_sha256=process_profile.process_profile_sha256,
            observer_source_sha256=observer_source, clock_domain_identity_sha256=observer_clock_domain,
        )
        if len(prefix_frames) != writer.frame_count:
            secondary.append(SynchronizedTelemetryEvidenceError(
                f"complete prefix holds {len(prefix_frames)} records but the writer completed {writer.frame_count} appends"
            ))
        failures: list[CollectionFailureV1] = []
        for lane in ("gpu_fast", "host_slow"):
            first = next((item for item in collection_failures if item.lane == lane), None)
            if first is None:
                continue
            bounded = bounded_observer_error(first.exception, text=first.traceback_text)
            failures.append(CollectionFailureV1(
                **bounded.model_dump(), lane=lane, category=first.category,
                scheduled_monotonic_ns=first.scheduled_monotonic_ns, started_monotonic_ns=first.started_monotonic_ns,
                finished_monotonic_ns=first.finished_monotonic_ns, observer_clock_domain_identity_sha256=observer_clock_domain,
            ))
        observer_error = None
        if primary is not None:
            observer_error = bounded_observer_error(primary, text="".join(traceback.format_exception(primary, limit=20)))
        writer_problem: BoundedObserverErrorV1 | None = None
        if secondary:
            writer_problem = bounded_observer_error(
                secondary[0], text="\n".join(f"{type(item).__name__}: {item}" for item in secondary),
            )
        reason: contract_module.FailureReceiptReason
        if failures and all(item.exception_type == _LaneWithoutSample.__name__ for item in failures):
            reason = "no_sample_before_stop"
        elif failures:
            reason = "lane_collection_failure"
        else:
            reason = "observer_error"
        receipt = SynchronizedTelemetryFailureReceiptV1(
            run_id=run_id, reason=reason,
            runtime_bundle_identity_sha256=process_profile.runtime_bundle_identity_sha256,
            process_profile_sha256=process_profile.process_profile_sha256, observer_source_sha256=observer_source,
            observer_identity=observer_identity, clock_domain_identity_sha256=observer_clock_domain,
            sampling_plan_sha256=plan_sha256, planned_start_monotonic_ns=plan.started_monotonic_ns,
            planned_end_monotonic_ns=plan.planned_end_monotonic_ns, stopped_monotonic_ns=stopped_ns,
            drained_monotonic_ns=drained_ns, collectors_quiesced=quiesced,
            raw_frames=RawFramesPrefixV1(
                sha256=physical_sha256, physical_bytes=len(physical), complete_prefix_bytes=prefix_bytes,
                complete_records=len(prefix_frames), trailing_bytes=len(physical) - prefix_bytes,
            ),
            lane_coverage=derive_prefix_coverage_v1(prefix_frames, plan=plan),
            failures=tuple(failures), observer_error=observer_error, writer_problem=writer_problem,
            unsealed_positive_artifacts=writer.unsealed_positive_artifacts(),
        )
        receipt_bytes = writer.write_failure_receipt(receipt)
        seal = SynchronizedTelemetryFailureSealV1(
            run_id=run_id, receipt_sha256="sha256:" + hashlib.sha256(receipt_bytes).hexdigest(),
            sampling_plan_sha256=plan_sha256, raw_frames_sha256=physical_sha256, physical_bytes=len(physical),
            complete_prefix_bytes=prefix_bytes, complete_records=len(prefix_frames), collectors_quiesced=quiesced,
            sealed_monotonic_ns=monotonic_ns(),
        )
        writer.write_failure_seal(seal)
        result = writer.replay_failure_terminal()
    except BaseException as terminal_error:
        writer.abort()
        if isinstance(terminal_error, (KeyboardInterrupt, SystemExit)):
            raise
        failed = SynchronizedTelemetryEvidenceError(
            f"synchronized telemetry evidence failed in {ObserverState.FAILED_EVIDENCE.value}: negative terminal could not be written"
        )
        failed.__cause__ = terminal_error
        if primary is not None:
            failed.add_note(f"original error: {type(primary).__name__}: {primary}")
        for item in collection_failures:
            failed.add_note(f"{item.lane} {item.category}: {type(item.exception).__name__}: {item.exception}")
        return failed
    return SynchronizedTelemetryCollectionFailed(result)


def _complete_frame_prefix(
    payload: bytes, *, frame_model: type[SynchronizedTelemetryFrameV3], run_id: str,
    runtime_bundle_identity_sha256: str, process_profile_sha256: str, observer_source_sha256: str,
    clock_domain_identity_sha256: str, deadline_monotonic_ns: int | None = None,
) -> tuple[tuple[SynchronizedTelemetryFrameV3, ...], int]:
    """The longest prefix of complete, canonical, sequential, identity-bound frame lines.

    Parsing stops at the first line that is incomplete, non-canonical, invalid or
    out of sequence; that line and everything after it are the physical tail and
    are never skipped over to reach later frames.
    """
    frames: list[SynchronizedTelemetryFrameV3] = []
    offset = 0
    while True:
        if deadline_monotonic_ns is not None and len(frames) % 64 == 0 and time.monotonic_ns() >= deadline_monotonic_ns:
            raise TimeoutError("telemetry prefix replay deadline passed while parsing frames")
        newline = payload.find(b"\n", offset)
        if newline < 0 or len(frames) >= MAX_FRAME_RECORDS:
            break
        line = payload[offset:newline]
        try:
            frame = frame_model.model_validate(
                parse_canonical_json_artifact(line, label="frame", maximum_bytes=MAX_FRAME_RECORD_BYTES)
            )
        except (ValueError, ValidationError):
            break
        if (frame.sequence != len(frames) or frame.run_id != run_id
                or frame.runtime_bundle_identity_sha256 != runtime_bundle_identity_sha256
                or frame.process_profile_sha256 != process_profile_sha256
                or frame.observer_source_sha256 != observer_source_sha256
                or frame.clock.clock_domain_identity_sha256 != clock_domain_identity_sha256):
            break
        frames.append(frame)
        offset = newline + 1
    return tuple(frames), offset


def verify_synchronized_telemetry_observer(
    *, artifact_root: Path, run_id: str, receipt_version: Literal[2, 3, 4] = 2,
    deadline_monotonic_ns: int | None = None,
) -> SynchronizedObserverResult:
    """Replay exact private files and recompute hashes, clocks, CPU and lanes.

    ``deadline_monotonic_ns`` is the caller's absolute local deadline for this
    expensive step; the frame parse checks it every 64 rows rather than only
    after unbounded work.
    """

    canonical_run_id = str(uuid.UUID(run_id))
    protocol = _artifact_protocol(receipt_version)
    if canonical_run_id != run_id:
        raise ValueError("run_id is not canonical")
    root_fd = _open_existing_private_directory(artifact_root, label="telemetry root")
    try:
        run_fd = os.open(
            run_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            _validate_private_directory_fd(run_fd, label="telemetry run")
            frames, receipt, seal, plan = _replay_artifacts_at(
                run_fd, expect_seal=True, protocol=protocol, deadline_monotonic_ns=deadline_monotonic_ns,
            )
        finally:
            os.close(run_fd)
    finally:
        os.close(root_fd)
    if receipt.run_id != run_id:
        raise ValueError("telemetry receipt run identity drifted")
    if receipt.observer_source_sha256 != synchronized_observer_source_sha256():
        raise ValueError("telemetry observer source identity drifted")
    assert seal is not None
    return SynchronizedObserverResult(
        state=ObserverState.SEALED,
        run_directory=artifact_root / run_id,
        receipt=receipt,
        seal=seal,
        frames=frames,
        plan=plan,
    )


def read_synchronized_telemetry_terminal(
    *, artifact_root: Path, run_id: str, deadline_monotonic_ns: int | None = None,
) -> SynchronizedObserverResult | SynchronizedTelemetryFailureResult:
    """Read the one terminal a v4 run directory holds: normal or negative, never both.

    The normal branch is exactly ``verify_synchronized_telemetry_observer`` for
    receipt version 4. The failure branch verifies only the negative contract:
    plan bytes, physical raw bytes, complete prefix re-parse and coverage, and
    the seal's bindings. A normal verifier never accepts a failure terminal as
    success, and a directory holding two complete terminals is refused.
    """
    canonical_run_id = str(uuid.UUID(run_id))
    if canonical_run_id != run_id:
        raise ValueError("run_id is not canonical")
    protocol = _V4
    assert protocol.plan_filename is not None
    root_fd = _open_existing_private_directory(artifact_root, label="telemetry root")
    try:
        run_fd = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            _validate_private_directory_fd(run_fd, label="telemetry run")
            names = set(os.listdir(run_fd))
            base = {protocol.frames_filename, protocol.plan_filename}
            normal = base | {protocol.receipt_filename, protocol.seal_filename}
            failure = base | {FAILURE_RECEIPT_FILENAME, FAILURE_SEAL_FILENAME}
            normal_complete = normal <= names
            failure_complete = failure <= names
            if normal_complete and failure_complete:
                raise ValueError("telemetry run holds two complete terminals; neither is trusted")
            if not normal_complete and not failure_complete:
                raise SynchronizedTelemetryTerminalAbsent(
                    "telemetry run holds no complete terminal: " + ", ".join(sorted(names)) if names else "telemetry run is empty"
                )
            if normal_complete:
                frames, receipt, seal, plan = _replay_artifacts_at(
                    run_fd, expect_seal=True, protocol=protocol, deadline_monotonic_ns=deadline_monotonic_ns,
                )
                if receipt.run_id != run_id:
                    raise ValueError("telemetry receipt run identity drifted")
                if receipt.observer_source_sha256 != synchronized_observer_source_sha256():
                    raise ValueError("telemetry observer source identity drifted")
                assert seal is not None
                return SynchronizedObserverResult(
                    state=ObserverState.SEALED, run_directory=artifact_root / run_id, receipt=receipt, seal=seal,
                    frames=frames, plan=plan,
                )
            expected = sorted(failure | ({protocol.receipt_filename} if protocol.receipt_filename in names else set()))
            if sorted(names) != expected:
                raise ValueError("telemetry failure terminal artifact set is unexpected")
            payloads: dict[str, bytes] = {}
            for filename in expected:
                maximum = MAX_FRAME_FILE_BYTES if filename == protocol.frames_filename else MAX_RECEIPT_BYTES
                payloads[filename] = _read_private_file_at(run_fd, filename, maximum_bytes=maximum)
            if sorted(os.listdir(run_fd)) != expected:
                raise ValueError("telemetry run directory changed during replay")
            result = _parse_failure_payloads(
                frames_payload=payloads[protocol.frames_filename], plan_payload=payloads[protocol.plan_filename],
                receipt_payload=payloads[FAILURE_RECEIPT_FILENAME], seal_payload=payloads[FAILURE_SEAL_FILENAME],
                unsealed={name: payloads[name] for name in expected if name == protocol.receipt_filename},
                run_directory=artifact_root / run_id, deadline_monotonic_ns=deadline_monotonic_ns,
            )
            if result.receipt.run_id != run_id:
                raise ValueError("telemetry failure receipt run identity drifted")
            if result.receipt.observer_source_sha256 != synchronized_observer_source_sha256():
                raise ValueError("telemetry observer source identity drifted")
            return result
        finally:
            os.close(run_fd)
    finally:
        os.close(root_fd)


def _parse_failure_payloads(
    *, frames_payload: bytes, plan_payload: bytes, receipt_payload: bytes, seal_payload: bytes,
    unsealed: dict[str, bytes], run_directory: Path, deadline_monotonic_ns: int | None = None,
) -> SynchronizedTelemetryFailureResult:
    """Verify the negative contract from exact bytes; no measurement is derived and nothing is credited."""
    plan = SynchronizedSamplingPlanV1.model_validate(
        parse_canonical_json_artifact(plan_payload, label="sampling plan", maximum_bytes=MAX_RECEIPT_BYTES)
    )
    receipt = SynchronizedTelemetryFailureReceiptV1.model_validate(
        parse_canonical_json_artifact(receipt_payload, label="failure receipt", maximum_bytes=MAX_RECEIPT_BYTES)
    )
    plan_sha256 = "sha256:" + hashlib.sha256(plan_payload).hexdigest()
    if (receipt.sampling_plan_sha256 != plan_sha256 or receipt.run_id != plan.run_id
            or receipt.planned_start_monotonic_ns != plan.started_monotonic_ns
            or receipt.planned_end_monotonic_ns != plan.planned_end_monotonic_ns
            or receipt.clock_domain_identity_sha256 != plan.observer_clock_domain_identity_sha256):
        raise ValueError("telemetry failure receipt is not bound to its sampling plan")
    physical_sha256 = "sha256:" + hashlib.sha256(frames_payload).hexdigest()
    if receipt.raw_frames.sha256 != physical_sha256 or receipt.raw_frames.physical_bytes != len(frames_payload):
        raise ValueError("telemetry failure receipt raw frames hash or size drifted")
    prefix_frames, prefix_bytes = _complete_frame_prefix(
        frames_payload, frame_model=SynchronizedTelemetryFrameV3, run_id=receipt.run_id,
        runtime_bundle_identity_sha256=receipt.runtime_bundle_identity_sha256,
        process_profile_sha256=receipt.process_profile_sha256, observer_source_sha256=receipt.observer_source_sha256,
        clock_domain_identity_sha256=receipt.clock_domain_identity_sha256, deadline_monotonic_ns=deadline_monotonic_ns,
    )
    if receipt.raw_frames.complete_prefix_bytes != prefix_bytes or receipt.raw_frames.complete_records != len(prefix_frames):
        raise ValueError("telemetry failure receipt complete prefix drifted from the raw bytes")
    if derive_prefix_coverage_v1(prefix_frames, plan=plan) != receipt.lane_coverage:
        raise ValueError("telemetry failure receipt lane coverage drifted")
    seal = SynchronizedTelemetryFailureSealV1.model_validate(
        parse_canonical_json_artifact(seal_payload, label="failure seal", maximum_bytes=MAX_RECEIPT_BYTES)
    )
    receipt_sha256 = "sha256:" + hashlib.sha256(receipt_payload).hexdigest()
    if (seal.run_id != receipt.run_id or seal.receipt_sha256 != receipt_sha256 or seal.sampling_plan_sha256 != plan_sha256
            or seal.raw_frames_sha256 != physical_sha256 or seal.physical_bytes != len(frames_payload)
            or seal.complete_prefix_bytes != prefix_bytes or seal.complete_records != len(prefix_frames)
            or seal.collectors_quiesced != receipt.collectors_quiesced):
        raise ValueError("telemetry failure seal identity drifted")
    if {name: "sha256:" + hashlib.sha256(payload).hexdigest() for name, payload in unsealed.items()} != dict(receipt.unsealed_positive_artifacts):
        raise ValueError("telemetry failure receipt unsealed positive artifacts drifted")
    return SynchronizedTelemetryFailureResult(
        run_directory=run_directory, receipt=receipt, seal=seal, plan=plan, prefix_frames=prefix_frames,
    )


def _replay_artifacts_at(
    run_fd: int,
    *,
    expect_seal: bool,
    expected_stats: dict[str, _ArtifactStat] | None = None,
    protocol: _ArtifactProtocol = _V2,
    deadline_monotonic_ns: int | None = None,
) -> tuple[
    tuple[TelemetryFrame, ...],
    TelemetryReceipt,
    TelemetrySeal | None,
    SynchronizedSamplingPlanV1 | None,
]:
    expected = [protocol.frames_filename, protocol.receipt_filename]
    if protocol.plan_filename is not None:
        expected.append(protocol.plan_filename)
    if expect_seal:
        expected.append(protocol.seal_filename)
    names_before = sorted(os.listdir(run_fd))
    if names_before != sorted(expected):
        raise ValueError("telemetry run artifacts are incomplete or unexpected")
    frames_payload = _read_private_file_at(
        run_fd,
        protocol.frames_filename,
        maximum_bytes=MAX_FRAME_FILE_BYTES,
        expected_stat=(expected_stats or {}).get(protocol.frames_filename),
    )
    plan_payload = None
    if protocol.plan_filename is not None:
        plan_payload = _read_private_file_at(
            run_fd,
            protocol.plan_filename,
            maximum_bytes=MAX_RECEIPT_BYTES,
            expected_stat=(expected_stats or {}).get(protocol.plan_filename),
        )
    receipt_payload = _read_private_file_at(
        run_fd,
        protocol.receipt_filename,
        maximum_bytes=MAX_RECEIPT_BYTES,
        expected_stat=(expected_stats or {}).get(protocol.receipt_filename),
    )
    seal_payload = None
    if expect_seal:
        seal_payload = _read_private_file_at(
            run_fd,
            protocol.seal_filename,
            maximum_bytes=MAX_RECEIPT_BYTES,
            expected_stat=(expected_stats or {}).get(protocol.seal_filename),
        )
    result = _parse_replay_payloads(
        frames_payload, receipt_payload, seal_payload, protocol=protocol, plan_payload=plan_payload,
        deadline_monotonic_ns=deadline_monotonic_ns,
    )
    if sorted(os.listdir(run_fd)) != names_before:
        raise ValueError("telemetry run directory changed during replay")
    return result


def _parse_replay_payloads(
    frames_payload: bytes,
    receipt_payload: bytes,
    seal_payload: bytes | None,
    *, protocol: _ArtifactProtocol = _V2,
    plan_payload: bytes | None = None,
    deadline_monotonic_ns: int | None = None,
) -> tuple[
    tuple[TelemetryFrame, ...],
    TelemetryReceipt,
    TelemetrySeal | None,
    SynchronizedSamplingPlanV1 | None,
]:
    frame_values = parse_canonical_jsonl_artifact(
        frames_payload,
        label="frames",
        maximum_bytes=MAX_FRAME_FILE_BYTES,
        maximum_record_bytes=MAX_FRAME_RECORD_BYTES,
        maximum_records=MAX_FRAME_RECORDS,
    )
    parsed: list[TelemetryFrame] = []
    for index, value in enumerate(frame_values):
        if deadline_monotonic_ns is not None and index % 64 == 0 and time.monotonic_ns() >= deadline_monotonic_ns:
            raise TimeoutError("telemetry replay deadline passed while parsing frames")
        parsed.append(protocol.frame_model.model_validate(value))
    frames: tuple[TelemetryFrame, ...] = tuple(parsed)
    receipt = protocol.receipt_model.model_validate(
        parse_canonical_json_artifact(receipt_payload, label="receipt", maximum_bytes=MAX_RECEIPT_BYTES)
    )
    frames_hash = canonical_jsonl_artifact_sha256(frames_payload, label="frames")
    if frames_hash != receipt.artifacts.frames_jsonl_sha256:
        raise ValueError("telemetry frames artifact hash drifted")
    plan: SynchronizedSamplingPlanV1 | None = None
    if (plan_payload is None) != (protocol.plan_filename is None):
        raise ValueError("telemetry sampling plan presence disagrees with the protocol")
    if isinstance(receipt, SynchronizedTelemetryReceiptV4):
        assert plan_payload is not None
        plan = SynchronizedSamplingPlanV1.model_validate(
            parse_canonical_json_artifact(plan_payload, label="sampling plan", maximum_bytes=MAX_RECEIPT_BYTES)
        )
        if receipt.sampling_plan_sha256 != "sha256:" + hashlib.sha256(plan_payload).hexdigest():
            raise ValueError("telemetry receipt is not bound to its sampling plan bytes")
        validate_synchronized_telemetry_v4(
            cast(tuple[SynchronizedTelemetryFrameV3, ...], frames), receipt=receipt, plan=plan,
        )
    else:
        validate_synchronized_telemetry_v2(cast(tuple[SynchronizedTelemetryFrameV2, ...], frames), receipt=receipt)
    seal: TelemetrySeal | None = None
    if seal_payload is not None:
        seal = protocol.seal_model.model_validate(
            parse_canonical_json_artifact(seal_payload, label="seal", maximum_bytes=MAX_RECEIPT_BYTES)
        )
        receipt_hash = "sha256:" + hashlib.sha256(receipt_payload).hexdigest()
        if seal.run_id != receipt.run_id or seal.receipt_sha256 != receipt_hash or seal.frames_jsonl_sha256 != frames_hash:
            raise ValueError("telemetry seal artifact identity drifted")
        if isinstance(seal, SynchronizedTelemetrySealV4):
            assert isinstance(receipt, SynchronizedTelemetryReceiptV4)
            if (seal.sampling_elapsed_ns_denominator != sampling_seal_denominator_ns(receipt)
                    or seal.lifecycle_elapsed_ns != receipt.finished_monotonic_ns - receipt.started_monotonic_ns
                    or seal.sampling_plan_sha256 != receipt.sampling_plan_sha256
                    or seal.frames_records != len(frames) or seal.frames_bytes != len(frames_payload)):
                raise ValueError("telemetry seal v4 plan/denominator/raw counts drifted")
        elif seal.sampling_elapsed_ns_denominator != receipt.finished_monotonic_ns - receipt.started_monotonic_ns:
            raise ValueError("telemetry seal elapsed interval drifted")
        expected_status = "unsafe" if receipt.status == "unsafe" or seal.preseal_observer_cpu_ns / seal.sampling_elapsed_ns_denominator > 0.02 else receipt.status
        if seal.receipt_status != receipt.status or seal.status != expected_status:
            raise ValueError("telemetry seal status drifted")
    return frames, receipt, seal, plan


def validate_synchronized_telemetry_v2(
    frames: tuple[SynchronizedTelemetryFrameV2, ...],
    *,
    receipt: TelemetryReceipt,
) -> None:
    if not frames:
        raise ValueError("telemetry frame sequence is empty")
    resident_previous: dict[
        str, contract_module.ResidentExporterSampleProvenance
    ] = {}
    resident_missing_lanes: set[str] = set()
    resident_host_identity: tuple[str, str] | None = None
    for sequence, frame in enumerate(frames):
        if frame.sequence != sequence or frame.run_id != receipt.run_id:
            raise ValueError("telemetry frame sequence or run identity drifted")
        if (
            frame.runtime_bundle_identity_sha256 != receipt.runtime_bundle_identity_sha256
            or frame.process_profile_sha256 != receipt.process_profile.process_profile_sha256
            or frame.observer_source_sha256 != receipt.observer_source_sha256
            or frame.clock.clock_domain_identity_sha256 != receipt.clock_domain_identity_sha256
        ):
            raise ValueError("telemetry frame identity drifted")
        if frame.lane == "gpu_fast":
            if any(
                observation.status != "unsupported" or observation.reason != "not_due_at_this_tick"
                for observation in (frame.api_process, frame.host_cgroup, frame.queue_vllm)
            ):
                raise ValueError("GPU lane carries observations owned by the host lane")
        elif frame.gpu.status != "unsupported" or frame.gpu.reason != "not_due_at_this_tick":
            raise ValueError("host lane carries an observation owned by the GPU lane")
        if isinstance(receipt, SynchronizedTelemetryReceiptV3) and frame.api_process.values is not None:
            if frame.api_process.values.process_epoch_sha256 != receipt.process_profile.process_epoch_sha256:
                raise ValueError("v3 API process epoch differs from the observed profile")
        if isinstance(receipt, SynchronizedTelemetryReceiptV3) and frame.queue_vllm.values is not None:
            # The admission limit a frame reports is the profile's, not a
            # collector constant; a serial-1 or foreign-capacity frame fails here.
            if frame.queue_vllm.values.api_max_pending_tasks != receipt.process_profile.parameters.api_max_pending_tasks:
                raise ValueError("v3 queue admission limit differs from the observed profile")
        provenance = frame.resident_exporter_provenance
        if provenance is None:
            resident_missing_lanes.add(frame.lane)
        if provenance is not None:
            host_identity = (
                provenance.host_assignment_identity_sha256,
                provenance.boot_identity_sha256,
            )
            if resident_host_identity is None:
                resident_host_identity = host_identity
            elif resident_host_identity != host_identity:
                raise ValueError("resident exporter host or boot identity drifted")
            previous = resident_previous.get(frame.lane)
            if previous is not None:
                if (
                    provenance.exporter_source_sha256
                    != previous.exporter_source_sha256
                    or provenance.exporter_process_epoch_sha256
                    != previous.exporter_process_epoch_sha256
                    or provenance.wire_sequence != previous.wire_sequence + 1
                    or provenance.wire_sampled_monotonic_ns
                    <= previous.wire_sampled_monotonic_ns
                ):
                    raise ValueError("resident exporter sequence or identity drifted")
            resident_previous[frame.lane] = provenance
    if resident_previous and set(resident_previous) != {"gpu_fast", "host_slow"}:
        raise ValueError("resident exporter provenance is missing one lane")
    if resident_previous and resident_missing_lanes:
        raise ValueError("resident exporter provenance is missing from a frame")
    expected_quality, unsupported = derive_frame_evidence(
        cast(tuple[contract_module.SynchronizedTelemetryFrame, ...], frames),
        started_monotonic_ns=receipt.started_monotonic_ns,
        finished_monotonic_ns=receipt.finished_monotonic_ns,
    )
    if expected_quality != receipt.lane_quality or unsupported != receipt.unsupported_observation_count:
        raise ValueError("telemetry receipt evidence drifted")
    required_reasons = {
        observation.reason
        for frame in frames
        for observation in (
            (frame.gpu,)
            if frame.lane == "gpu_fast"
            else (frame.api_process, frame.host_cgroup, frame.queue_vllm)
        )
        if observation.status == "unsupported"
    }
    if not set(receipt.safety_drift_reasons).issubset(required_reasons):
        raise ValueError("telemetry safety drift lacks required unsupported evidence")


def validate_synchronized_telemetry_v4(
    frames: tuple[SynchronizedTelemetryFrameV3, ...],
    *,
    receipt: SynchronizedTelemetryReceiptV4,
    plan: SynchronizedSamplingPlanV1,
) -> None:
    """Frame v3 identity/continuity binding and plan-bounded evidence recomputation."""
    if not frames:
        raise ValueError("telemetry frame sequence is empty")
    if (receipt.run_id != plan.run_id or receipt.started_monotonic_ns != plan.started_monotonic_ns
            or receipt.duration_ns != plan.duration_ns or receipt.planned_end_monotonic_ns != plan.planned_end_monotonic_ns
            or receipt.clock_domain_identity_sha256 != plan.observer_clock_domain_identity_sha256):
        raise ValueError("telemetry receipt window differs from its sampling plan")
    resident_previous: dict[str, contract_module.ResidentExporterPullProvenance] = {}
    resident_host_identity: tuple[str, str] | None = None
    for sequence, frame in enumerate(frames):
        if frame.sequence != sequence or frame.run_id != receipt.run_id:
            raise ValueError("telemetry frame sequence or run identity drifted")
        if (
            frame.runtime_bundle_identity_sha256 != receipt.runtime_bundle_identity_sha256
            or frame.process_profile_sha256 != receipt.process_profile.process_profile_sha256
            or frame.observer_source_sha256 != receipt.observer_source_sha256
            or frame.clock.clock_domain_identity_sha256 != receipt.clock_domain_identity_sha256
        ):
            raise ValueError("telemetry frame identity drifted")
        if frame.lane == "gpu_fast":
            if any(
                observation.status != "unsupported" or observation.reason != "not_due_at_this_tick"
                for observation in (frame.api_process, frame.host_cgroup, frame.queue_vllm)
            ):
                raise ValueError("GPU lane carries observations owned by the host lane")
        elif frame.gpu.status != "unsupported" or frame.gpu.reason != "not_due_at_this_tick":
            raise ValueError("host lane carries an observation owned by the GPU lane")
        if frame.api_process.values is not None and frame.api_process.values.process_epoch_sha256 != receipt.process_profile.process_epoch_sha256:
            raise ValueError("v4 API process epoch differs from the observed profile")
        if frame.queue_vllm.values is not None and frame.queue_vllm.values.api_max_pending_tasks != receipt.process_profile.parameters.api_max_pending_tasks:
            raise ValueError("v4 queue admission limit differs from the observed profile")
        provenance = frame.resident_exporter_provenance
        host_identity = (provenance.host_assignment_identity_sha256, provenance.boot_identity_sha256)
        if resident_host_identity is None:
            resident_host_identity = host_identity
        elif resident_host_identity != host_identity:
            raise ValueError("resident exporter host or boot identity drifted")
        previous = resident_previous.get(frame.lane)
        if previous is not None and (
            provenance.exporter_source_sha256 != previous.exporter_source_sha256
            or provenance.exporter_process_epoch_sha256 != previous.exporter_process_epoch_sha256
            or provenance.wire_sequence != previous.wire_sequence + 1
            or provenance.wire_sampled_monotonic_ns <= previous.wire_sampled_monotonic_ns
            or provenance.native_request_received_monotonic_ns < previous.native_reply_started_monotonic_ns
        ):
            raise ValueError("resident exporter sequence or identity drifted")
        resident_previous[frame.lane] = provenance
    if set(resident_previous) != {"gpu_fast", "host_slow"}:
        raise ValueError("resident exporter provenance is missing one lane")
    lane_quality, slot_coverage, unsupported = derive_frame_evidence_v4(frames, plan=plan)
    if lane_quality != receipt.lane_quality or slot_coverage != receipt.slot_coverage or unsupported != receipt.unsupported_observation_count:
        raise ValueError("telemetry receipt evidence drifted")
    required_reasons = {
        observation.reason
        for frame in frames
        for observation in ((frame.gpu,) if frame.lane == "gpu_fast" else (frame.api_process, frame.host_cgroup, frame.queue_vllm))
        if observation.status == "unsupported"
    }
    if not set(receipt.safety_drift_reasons).issubset(required_reasons):
        raise ValueError("telemetry safety drift lacks required unsupported evidence")


def _run_lane(
    *,
    lane: Literal["gpu_fast", "host_slow"],
    interval_ns: int,
    sampler: _ResidentSamplerProcess,
    mailbox: _Mailbox,
    condition: threading.Condition,
    expected_identity: TelemetrySampleIdentity,
    expected_process_epoch: str,
    frozen_gpu_identity: list[str | None],
    frozen_cgroup_identity: list[str | None],
    identity_lock: threading.Lock,
    start_monotonic: int,
    end_deadline: int,
    external_cancel: threading.Event,
    internal_stop: threading.Event,
    termination: _Termination,
    safety_drifts: _SafetyDrifts,
    counter_tracker: _CounterTracker,
    monotonic_ns: Callable[[], int],
    utc_now: Callable[[], datetime],
    negative_outcomes: bool = False,
) -> None:
    """One lane's schedule loop.

    ``negative_outcomes`` (the v4 protocol) turns a deadline, transport, continuity
    or safety-drift failure into a ``_CollectionFailure`` record that stops the
    lane: no witness-less sample is published. Legacy protocols keep projecting
    such failures into unsupported samples for deterministic replay of old data.
    """
    scheduled = start_monotonic
    emitted = False
    pending: _MailboxItem

    def failure(category: contract_module.CollectionFailureCategory, exc: BaseException,
                *, scheduled_ns: int, started_ns: int) -> _CollectionFailure:
        finished_ns = monotonic_ns()
        return _CollectionFailure(
            lane=lane, category=category, scheduled_monotonic_ns=min(scheduled_ns, started_ns),
            started_monotonic_ns=started_ns, finished_monotonic_ns=max(started_ns, finished_ns),
            exception=exc, traceback_text="".join(traceback.format_exception(exc, limit=20)),
        )

    try:
        while scheduled < end_deadline and not internal_stop.is_set():
            if external_cancel.is_set():
                termination.mark("cancelled")
                break
            if _wait_until(
                scheduled,
                external_cancel=external_cancel,
                internal_stop=internal_stop,
                monotonic_ns=monotonic_ns,
            ):
                termination.mark("cancelled")
                break
            if internal_stop.is_set():
                break
            sample_clock = _capture_clock_pair(
                monotonic_ns=monotonic_ns,
                utc_now=utc_now,
            )
            started = sample_clock.monotonic_ns
            observed_at = sample_clock.wall
            try:
                snapshot_deadline = min(end_deadline, scheduled + interval_ns)
                snapshot = sampler.snapshot(
                    deadline=TelemetrySnapshotDeadline(monotonic_ns=snapshot_deadline),
                    cancel_event=external_cancel,
                    monotonic_ns=monotonic_ns,
                )
                finished = monotonic_ns()
                pending = _project_snapshot(
                    lane=lane,
                    scheduled=scheduled,
                    started=started,
                    finished=finished,
                    observed_at=observed_at,
                    snapshot=snapshot,
                    expected_identity=expected_identity,
                    expected_process_epoch=expected_process_epoch,
                    frozen_gpu_identity=frozen_gpu_identity,
                    frozen_cgroup_identity=frozen_cgroup_identity,
                    identity_lock=identity_lock,
                    counter_tracker=counter_tracker,
                )
            except _InvocationCancelled:
                termination.mark("cancelled")
                break
            except _SafetyDrift as exc:
                safety_drifts.add(exc.reason)
                termination.mark("identity_drift")
                internal_stop.set()
                if negative_outcomes:
                    pending = failure("continuity", exc, scheduled_ns=scheduled, started_ns=started)
                else:
                    finished = monotonic_ns()
                    pending = _unsupported_pending(
                        lane=lane, scheduled=scheduled, started=started, finished=finished,
                        observed_at=observed_at, reason=exc.reason,
                    )
            except TelemetrySnapshotDeadlineExceeded as exc:
                termination.mark("sampler_or_transport_shutdown")
                internal_stop.set()
                if negative_outcomes:
                    pending = failure("deadline", exc, scheduled_ns=scheduled, started_ns=started)
                else:
                    finished = monotonic_ns()
                    pending = _unsupported_pending(
                        lane=lane, scheduled=scheduled, started=started, finished=finished,
                        observed_at=observed_at, reason="deadline_exceeded",
                    )
            except TelemetrySnapshotTransportUnavailable as exc:
                termination.mark("sampler_or_transport_shutdown")
                internal_stop.set()
                if negative_outcomes:
                    pending = failure("transport", exc, scheduled_ns=scheduled, started_ns=started)
                else:
                    finished = monotonic_ns()
                    pending = _unsupported_pending(
                        lane=lane, scheduled=scheduled, started=started, finished=finished,
                        observed_at=observed_at, reason="endpoint_unreachable",
                    )
            except TelemetrySnapshotContinuityLost as exc:
                safety_drifts.add("identity_drift")
                termination.mark("identity_drift")
                internal_stop.set()
                if negative_outcomes:
                    pending = failure("continuity", exc, scheduled_ns=scheduled, started_ns=started)
                else:
                    finished = monotonic_ns()
                    pending = _unsupported_pending(
                        lane=lane, scheduled=scheduled, started=started, finished=finished,
                        observed_at=observed_at, reason="identity_drift",
                    )
            if not _publish_pending(
                mailbox=mailbox,
                pending=pending,
                condition=condition,
            ):
                termination.mark("queue_overflow")
            else:
                emitted = True
            with mailbox.lock:
                mailbox.watermark_monotonic_ns = max(
                    mailbox.watermark_monotonic_ns, pending.finished_monotonic_ns
                )
                mailbox.started.set()
            next_deadline = scheduled + interval_ns
            now = monotonic_ns()
            if now >= next_deadline:
                next_deadline += ((now - next_deadline) // interval_ns + 1) * interval_ns
            scheduled = next_deadline
        if (
            not external_cancel.is_set()
            and not internal_stop.is_set()
            and scheduled >= end_deadline
        ):
            _wait_until(
                end_deadline,
                external_cancel=external_cancel,
                internal_stop=internal_stop,
                monotonic_ns=monotonic_ns,
            )
        if external_cancel.is_set():
            termination.mark("cancelled")
        if not emitted:
            now = monotonic_ns()
            if negative_outcomes:
                # A lane that never produced a sample or a failure record before it stopped is
                # a negative fact of its own, not an unsupported frame without a witness.
                mailbox.fallback = failure(
                    "internal", _LaneWithoutSample(f"{lane} lane stopped ({termination.value()}) before its first sample"),
                    scheduled_ns=start_monotonic, started_ns=now,
                )
            else:
                mailbox.fallback = _unsupported_pending(
                    lane=lane,
                    scheduled=start_monotonic,
                    started=now,
                    finished=now,
                    observed_at=utc_now(),
                    reason=(safety_drifts.values()[0] if safety_drifts.values() else "collector_disabled"),
                )
    except BaseException as exc:
        mailbox.failure = exc
        internal_stop.set()
    finally:
        mailbox.done.set()
        with condition:
            condition.notify_all()


def _merge_lane_mailboxes(
    *,
    mailboxes: dict[str, _Mailbox],
    condition: threading.Condition,
    writer: _FrameWriter,
    frames: list[TelemetryFrame],
    run_id: str,
    process_profile: ApiProfile,
    observer_clock_domain: str,
    observer_source_sha256: str,
    termination: _Termination,
    internal_stop: threading.Event,
    gpu_interval_ms: int,
    protocol: _ArtifactProtocol = _V2,
    collection_failures: list[_CollectionFailure] | None = None,
) -> None:
    """Merge both lanes in start order into frames.

    A ``_CollectionFailure`` item (v4) is appended to ``collection_failures`` and
    writes no frame, advances no sequence and credits nothing; a v4 sample without
    its fresh-pull witness is recorded the same way as an internal failure. Lane
    thread exceptions are re-raised as before.
    """
    heads: dict[str, _MailboxItem | None] = {
        "gpu_fast": None,
        "host_slow": None,
    }
    previous_by_lane: dict[str, _PendingSample] = {}
    while True:
        for mailbox in mailboxes.values():
            if mailbox.failure is not None:
                raise mailbox.failure
        for lane, mailbox in mailboxes.items():
            if heads[lane] is None:
                try:
                    heads[lane] = mailbox.values.get_nowait()
                except queue.Empty:
                    if mailbox.done.is_set() and mailbox.fallback is not None:
                        heads[lane] = mailbox.fallback
                        mailbox.fallback = None
        if all(
            heads[lane] is None
            and mailbox.done.is_set()
            and mailbox.values.empty()
            and mailbox.fallback is None
            for lane, mailbox in mailboxes.items()
        ):
            break
        candidates = [value for value in heads.values() if value is not None]
        if not candidates:
            with condition:
                condition.wait(timeout=0.05)
            continue
        candidate = min(
            candidates,
            key=lambda item: (
                item.started_monotonic_ns,
                0 if item.lane == "gpu_fast" else 1,
            ),
        )
        ready = True
        for lane, mailbox in mailboxes.items():
            if heads[lane] is not None or mailbox.done.is_set():
                continue
            with mailbox.lock:
                if not mailbox.started.is_set() or mailbox.watermark_monotonic_ns < candidate.started_monotonic_ns:
                    ready = False
                    break
        if not ready:
            with condition:
                condition.wait(timeout=0.05)
            continue
        if isinstance(candidate, _CollectionFailure):
            if collection_failures is None:
                raise SynchronizedTelemetryEvidenceError("collection failure records belong to the v4 observer only")
            collection_failures.append(candidate)
            heads[candidate.lane] = None
            continue
        if protocol.frame_model is SynchronizedTelemetryFrameV3 and not isinstance(
            candidate.resident_exporter_provenance, ResidentExporterPullProvenance
        ):
            # A normally returned sample without its fresh-per-request witness is a bug, and a
            # bug is a negative fact: it closes the run without ever becoming a frame.
            if collection_failures is None:
                raise SynchronizedTelemetryEvidenceError("collection failure records belong to the v4 observer only")
            witness_error = SynchronizedTelemetryEvidenceError(
                "missing_pull_witness: fresh-per-request witness missing; the v4 observer accepts only pull provenance"
            )
            collection_failures.append(_CollectionFailure(
                lane=candidate.lane, category="internal", scheduled_monotonic_ns=candidate.scheduled_monotonic_ns,
                started_monotonic_ns=candidate.started_monotonic_ns, finished_monotonic_ns=candidate.finished_monotonic_ns,
                exception=witness_error, traceback_text=f"{type(witness_error).__name__}: {witness_error}",
            ))
            termination.mark("sampler_or_transport_shutdown")
            internal_stop.set()
            heads[candidate.lane] = None
            continue
        pending = candidate
        previous = previous_by_lane.get(pending.lane)
        if previous is None:
            status: Literal["first", "on_time", "late"] = "first"
            observed_interval_ms = None
            missed = 0
        else:
            observed_ns = (
                pending.started_monotonic_ns - previous.started_monotonic_ns
            )
            nominal_ms = gpu_interval_ms if pending.lane == "gpu_fast" else 1000
            nominal_ns = nominal_ms * 1_000_000
            scheduled_ns = (
                pending.scheduled_monotonic_ns
                - previous.scheduled_monotonic_ns
            )
            if scheduled_ns < nominal_ns or scheduled_ns % nominal_ns:
                raise SynchronizedTelemetryEvidenceError(
                    "telemetry lane absolute schedule drifted"
                )
            missed = scheduled_ns // nominal_ns - 1
            status = "late" if missed else "on_time"
            observed_interval_ms = observed_ns / 1_000_000
        frame: TelemetryFrame = protocol.frame_model.model_validate(dict(
            run_id=run_id,
            sequence=len(frames),
            lane=pending.lane,
            runtime_bundle_identity_sha256=(
                process_profile.runtime_bundle_identity_sha256
            ),
            process_profile_sha256=process_profile.process_profile_sha256,
            observer_source_sha256=observer_source_sha256,
            resident_exporter_provenance=pending.resident_exporter_provenance,
            clock=SampleClock(
                clock_domain_identity_sha256=observer_clock_domain,
                observed_at_utc=pending.observed_at_utc,
                scheduled_monotonic_ns=pending.scheduled_monotonic_ns,
                started_monotonic_ns=pending.started_monotonic_ns,
                finished_monotonic_ns=pending.finished_monotonic_ns,
            ),
            quality=SampleQuality(
                nominal_interval_ms=(
                    gpu_interval_ms if pending.lane == "gpu_fast" else 1000
                ),
                observed_interval_ms=observed_interval_ms,
                collection_duration_ms=(
                    pending.finished_monotonic_ns - pending.started_monotonic_ns
                )
                / 1_000_000,
                missed_deadlines=missed,
                status=status,
            ),
            gpu=pending.gpu,
            api_process=pending.api_process,
            host_cgroup=pending.host_cgroup,
            queue_vllm=pending.queue_vllm,
        ))
        try:
            writer.append(frame)
        except _ArtifactBoundExceeded:
            termination.mark("artifact_bound_exceeded")
            internal_stop.set()
            if not frames or {item.lane for item in frames} != {
                "gpu_fast",
                "host_slow",
            }:
                raise
            break
        frames.append(frame)
        previous_by_lane[pending.lane] = pending
        heads[pending.lane] = None


def _project_snapshot(
    *,
    lane: Literal["gpu_fast", "host_slow"],
    scheduled: int,
    started: int,
    finished: int,
    observed_at: datetime,
    snapshot: GpuLaneSnapshot | HostLaneSnapshot,
    expected_identity: TelemetrySampleIdentity,
    expected_process_epoch: str,
    frozen_gpu_identity: list[str | None],
    frozen_cgroup_identity: list[str | None],
    identity_lock: threading.Lock,
    counter_tracker: _CounterTracker,
) -> _PendingSample:
    if snapshot.identity != expected_identity:
        raise _SafetyDrift("identity_drift", "sample runtime identity drifted")
    if lane == "gpu_fast":
        if not isinstance(snapshot, GpuLaneSnapshot):
            raise AssertionError("GPU sampler returned the wrong lane")
        if snapshot.gpu.status == "supported":
            assert snapshot.gpu.values is not None
            observed_identity = snapshot.gpu.values.device_identity_sha256
            with identity_lock:
                if frozen_gpu_identity[0] is None:
                    frozen_gpu_identity[0] = observed_identity
                elif frozen_gpu_identity[0] != observed_identity:
                    raise _SafetyDrift("identity_drift", "GPU device identity drifted")
        return _PendingSample(
            lane=lane,
            scheduled_monotonic_ns=scheduled,
            started_monotonic_ns=started,
            finished_monotonic_ns=finished,
            observed_at_utc=observed_at,
            resident_exporter_provenance=snapshot.resident_exporter_provenance,
            gpu=GpuObservationV2.model_validate(snapshot.gpu.model_dump()),
            api_process=_unsupported_process("not_due_at_this_tick"),
            host_cgroup=_unsupported_host("not_due_at_this_tick"),
            queue_vllm=_unsupported_queue("not_due_at_this_tick"),
        )
    if not isinstance(snapshot, HostLaneSnapshot):
        raise AssertionError("host sampler returned the wrong lane")
    if snapshot.api_process.status == "supported":
        assert snapshot.api_process.values is not None
        if snapshot.api_process.values.process_epoch_sha256 != expected_process_epoch:
            raise _SafetyDrift("epoch_drift", "API process epoch drifted")
    if snapshot.host_cgroup.status == "supported":
        assert snapshot.host_cgroup.values is not None
        observed_identity = snapshot.host_cgroup.values.parent_cgroup_epoch_sha256
        with identity_lock:
            if frozen_cgroup_identity[0] is None:
                frozen_cgroup_identity[0] = observed_identity
            elif frozen_cgroup_identity[0] != observed_identity:
                raise _SafetyDrift("epoch_drift", "parent cgroup epoch drifted")
    _check_cumulative_counters(snapshot, counter_tracker)
    return _PendingSample(
        lane=lane,
        scheduled_monotonic_ns=scheduled,
        started_monotonic_ns=started,
        finished_monotonic_ns=finished,
        observed_at_utc=observed_at,
        resident_exporter_provenance=snapshot.resident_exporter_provenance,
        gpu=_unsupported_gpu("not_due_at_this_tick"),
        api_process=ApiProcessObservationV2.model_validate(snapshot.api_process.model_dump()),
        host_cgroup=HostCgroupObservationV2.model_validate(snapshot.host_cgroup.model_dump()),
        queue_vllm=QueueVllmObservationV2.model_validate(snapshot.queue_vllm.model_dump()),
    )


class _SafetyDrift(ValueError):
    def __init__(self, reason: contract_module.SafetyDriftReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class _LaneWithoutSample(RuntimeError):
    """A v4 lane stopped before publishing any sample or failure record."""


def _check_cumulative_counters(
    snapshot: HostLaneSnapshot, tracker: _CounterTracker
) -> None:
    counters: dict[str, int] = {}
    if snapshot.api_process.status == "supported":
        api_values = snapshot.api_process.values
        assert api_values is not None
        counters.update(api_cpu_user=api_values.cpu_user_ns_total, api_cpu_system=api_values.cpu_system_ns_total)
    if snapshot.host_cgroup.status == "supported":
        host_values = snapshot.host_cgroup.values
        assert host_values is not None
        counters.update(
            memory_low=host_values.memory_events.low_total,
            memory_high=host_values.memory_events.high_total,
            memory_max=host_values.memory_events.max_total,
            memory_oom=host_values.memory_events.oom_total,
            memory_oom_kill=host_values.memory_events.oom_kill_total,
            memory_oom_group_kill=host_values.memory_events.oom_group_kill_total,
            cpu_usage=host_values.cpu_stat.usage_ns_total,
            cpu_user=host_values.cpu_stat.user_ns_total,
            cpu_system=host_values.cpu_stat.system_ns_total,
            cpu_throttled=host_values.cpu_stat.throttled_ns_total,
            cpu_throttled_periods=host_values.cpu_stat.throttled_periods_total,
        )
    if snapshot.queue_vllm.status == "supported":
        queue_values = snapshot.queue_vllm.values
        assert queue_values is not None
        counters["vllm_preemptions"] = queue_values.vllm_preemptions_total
    with tracker.lock:
        for key, value in counters.items():
            previous = tracker.previous.get(key)
            if previous is not None and value < previous:
                raise _SafetyDrift("counter_regression", f"cumulative counter {key} regressed")
            if key in {"memory_oom", "memory_oom_kill", "memory_oom_group_kill"} and previous is not None and value > previous:
                raise _SafetyDrift("oom_increment", f"OOM counter {key} increased")
        tracker.previous.update(counters)


def _unsupported_pending(
    *,
    lane: Literal["gpu_fast", "host_slow"],
    scheduled: int,
    started: int,
    finished: int,
    observed_at: datetime,
    reason: contract_module.UnsupportedReasonV2,
) -> _PendingSample:
    return _PendingSample(
        lane=lane,
        scheduled_monotonic_ns=min(scheduled, started),
        started_monotonic_ns=started,
        finished_monotonic_ns=max(started, finished),
        observed_at_utc=observed_at,
        resident_exporter_provenance=None,
        gpu=(
            _unsupported_gpu(reason)
            if lane == "gpu_fast"
            else _unsupported_gpu("not_due_at_this_tick")
        ),
        api_process=(
            _unsupported_process(reason)
            if lane == "host_slow"
            else _unsupported_process("not_due_at_this_tick")
        ),
        host_cgroup=(
            _unsupported_host(reason)
            if lane == "host_slow"
            else _unsupported_host("not_due_at_this_tick")
        ),
        queue_vllm=(
            _unsupported_queue(reason)
            if lane == "host_slow"
            else _unsupported_queue("not_due_at_this_tick")
        ),
    )


def _unsupported_gpu(reason: contract_module.UnsupportedReasonV2) -> GpuObservationV2:
    return GpuObservationV2(status="unsupported", reason=reason, values=None)


def _unsupported_process(
    reason: contract_module.UnsupportedReasonV2,
) -> ApiProcessObservationV2:
    return ApiProcessObservationV2(status="unsupported", reason=reason, values=None)


def _unsupported_host(
    reason: contract_module.UnsupportedReasonV2,
) -> HostCgroupObservationV2:
    return HostCgroupObservationV2(status="unsupported", reason=reason, values=None)


def _unsupported_queue(
    reason: contract_module.UnsupportedReasonV2,
) -> QueueVllmObservationV2:
    return QueueVllmObservationV2(status="unsupported", reason=reason, values=None)


def _publish_pending(
    *, mailbox: _Mailbox, pending: _MailboxItem, condition: threading.Condition
) -> bool:
    try:
        mailbox.values.put_nowait(pending)
    except queue.Full:
        return False
    with condition:
        condition.notify_all()
    return True


def _wait_until(
    deadline_ns: int,
    *,
    external_cancel: threading.Event,
    internal_stop: threading.Event,
    monotonic_ns: Callable[[], int],
) -> bool:
    while True:
        if external_cancel.is_set() or internal_stop.is_set():
            return external_cancel.is_set()
        remaining_ns = deadline_ns - monotonic_ns()
        if remaining_ns <= 0:
            return False
        external_cancel.wait(min(remaining_ns / 1_000_000_000, 0.05))


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("telemetry wall clock must be UTC")


def _open_or_create_private_directory(path: Path) -> tuple[int, int]:
    created = False
    parent_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        if created:
            os.fsync(parent_descriptor)
        root_descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        _validate_private_directory_fd(root_descriptor, label="telemetry root")
        return parent_descriptor, root_descriptor
    except Exception:
        os.close(parent_descriptor)
        raise


def _open_existing_private_directory(path: Path, *, label: str) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _validate_private_directory_fd(descriptor, label=label)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _validate_private_directory_fd(descriptor: int, *, label: str) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise ValueError(f"{label} is not a private directory")


def _directory_stat_fd(descriptor: int) -> tuple[int, int, int, int]:
    metadata = os.fstat(descriptor)
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
    )


def _directory_stat_at(
    directory_fd: int, filename: str, *, label: str
) -> tuple[int, int, int, int]:
    metadata = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} anchor is not a directory")
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
    )


def _open_new_private_at(directory_fd: int, filename: str) -> int:
    descriptor = os.open(
        filename,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise ValueError("telemetry artifact file creation is unsafe")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _read_private_file_at(
    directory_fd: int,
    filename: str,
    *,
    maximum_bytes: int,
    expected_stat: _ArtifactStat | None = None,
) -> bytes:
    descriptor = os.open(
        filename,
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        before = _artifact_stat_fd(descriptor, maximum_bytes=maximum_bytes)
        if expected_stat is not None and before != expected_stat:
            raise ValueError("telemetry artifact identity or metadata drifted")
        remaining = before.size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("telemetry artifact changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("telemetry artifact changed while being read")
        after = _artifact_stat_fd(descriptor, maximum_bytes=maximum_bytes)
        if after != before or (expected_stat is not None and after != expected_stat):
            raise ValueError("telemetry artifact changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_private_descriptor(
    descriptor: int,
    *,
    maximum_bytes: int,
    expected_stat: _ArtifactStat | None = None,
) -> tuple[bytes, _ArtifactStat]:
    before = _artifact_stat_fd(descriptor, maximum_bytes=maximum_bytes)
    if expected_stat is not None and before != expected_stat:
        raise ValueError("telemetry write descriptor metadata drifted")
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = before.size
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            raise ValueError("telemetry write descriptor changed while being read")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise ValueError("telemetry write descriptor grew while being read")
    after = _artifact_stat_fd(descriptor, maximum_bytes=maximum_bytes)
    if after != before or (expected_stat is not None and after != expected_stat):
        raise ValueError("telemetry write descriptor changed while being read")
    return b"".join(chunks), after


def _private_file_stat_at(
    directory_fd: int, filename: str, *, maximum_bytes: int
) -> _ArtifactStat:
    descriptor = os.open(
        filename,
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        return _artifact_stat_fd(descriptor, maximum_bytes=maximum_bytes)
    finally:
        os.close(descriptor)


def _artifact_stat_fd(descriptor: int, *, maximum_bytes: int) -> _ArtifactStat:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or metadata.st_size > maximum_bytes
    ):
        raise ValueError("telemetry artifact is not a bounded private file")
    return _ArtifactStat(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        links=metadata.st_nlink,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("telemetry artifact write made no progress")
        view = view[written:]


def _close_descriptor(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


__all__ = [
    "sampling_duration_ns",
    "ObserverState",
    "SynchronizedObserverLimits",
    "SynchronizedObserverResult",
    "SynchronizedTelemetryEvidenceError",
    "run_synchronized_telemetry_observer",
    "synchronized_observer_source_sha256",
    "verify_synchronized_telemetry_observer",
    "validate_synchronized_telemetry_v2",
]
