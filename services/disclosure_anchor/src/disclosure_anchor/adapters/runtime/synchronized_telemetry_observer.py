"""Default-off resident dual-lane synchronized telemetry observer.

This module owns only local scheduling and immutable evidence.  Concrete GPU and
host collectors are injected; no runtime service, database or worker is touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import stat
import threading
import time
from typing import Callable, Literal, cast
import uuid

import disclosure_anchor.application.contracts.synchronized_telemetry as contract_module
import disclosure_anchor.application.ports.synchronized_telemetry as port_module
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    ApiProcessObservationV2,
    GpuObservationV2,
    HostCgroupObservationV2,
    QueueVllmObservationV2,
    SampleClock,
    SampleQuality,
    SynchronizedTelemetryFrameV2,
    SynchronizedTelemetryReceiptV2,
    SynchronizedTelemetrySealV2,
    TelemetryArtifactsV2,
    canonical_jsonl_artifact_sha256,
    derive_frame_evidence,
    parse_canonical_json_artifact,
    parse_canonical_jsonl_artifact,
)
from disclosure_anchor.application.ports.synchronized_telemetry import (
    GpuLaneSnapshot,
    GpuTelemetrySamplerPort,
    HostLaneSnapshot,
    HostTelemetrySamplerPort,
    TelemetrySampleIdentity,
    TelemetrySnapshotDeadline,
    TelemetrySnapshotDeadlineExceeded,
    TelemetrySnapshotTransportUnavailable,
)


MAX_FRAME_RECORDS = 1_000_000
MAX_FRAME_FILE_BYTES = 256 * 1024 * 1024
MAX_FRAME_RECORD_BYTES = 256 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
FRAME_FILENAME = "frames.v2.jsonl"
RECEIPT_FILENAME = "receipt.v2.json"
SEAL_FILENAME = "seal.v2.json"


class ObserverState(str, Enum):
    INIT = "INIT"
    PREFLIGHT = "PREFLIGHT"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    SEALED = "SEALED"
    FAILED_EVIDENCE = "FAILED_EVIDENCE"


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
    receipt: SynchronizedTelemetryReceiptV2
    seal: SynchronizedTelemetrySealV2
    frames: tuple[SynchronizedTelemetryFrameV2, ...]

    @property
    def evidence_status(self) -> contract_module.RunStatus:
        """Authoritative final status, including observer-overhead attestation."""

        return self.seal.status


@dataclass(frozen=True, slots=True)
class _PendingSample:
    lane: Literal["gpu_fast", "host_slow"]
    scheduled_monotonic_ns: int
    started_monotonic_ns: int
    finished_monotonic_ns: int
    observed_at_utc: datetime
    gpu: GpuObservationV2
    api_process: ApiProcessObservationV2
    host_cgroup: HostCgroupObservationV2
    queue_vllm: QueueVllmObservationV2


@dataclass(slots=True)
class _Mailbox:
    values: queue.Queue[_PendingSample]
    done: threading.Event
    fallback: _PendingSample | None = None
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


class _FrameWriter:
    def __init__(
        self,
        *,
        artifact_root: Path,
        run_id: str,
        limits: SynchronizedObserverLimits,
    ) -> None:
        self._limits = limits
        self._root_fd: int | None = _open_or_create_private_directory(artifact_root)
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
            self._root_fd = None
            raise
        self.run_directory = artifact_root / run_id
        if self._run_fd is None:
            raise SynchronizedTelemetryEvidenceError(
                "telemetry run descriptor is unavailable"
            )
        try:
            _validate_private_directory_fd(self._run_fd, label="telemetry run")
            self._frames_fd: int | None = _open_new_private_at(
                self._run_fd, FRAME_FILENAME
            )
        except Exception:
            _close_descriptor(self._run_fd)
            _close_descriptor(self._root_fd)
            self._run_fd = None
            self._root_fd = None
            raise
        self._frame_hash = hashlib.sha256()
        self._frame_count = 0
        self._frame_bytes = 0
        self._frames_closed = False
        self._closed = False

    def append(self, frame: SynchronizedTelemetryFrameV2) -> None:
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
        os.close(self._frames_fd)
        self._frames_fd = None
        self._frames_closed = True
        os.fsync(self._run_fd)
        return "sha256:" + self._frame_hash.hexdigest()

    def write_receipt(self, receipt: SynchronizedTelemetryReceiptV2) -> bytes:
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
        descriptor = _open_new_private_at(self._run_fd, RECEIPT_FILENAME)
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(self._run_fd)
        os.fsync(self._root_fd)
        return payload

    def replay_unsealed(
        self,
    ) -> tuple[tuple[SynchronizedTelemetryFrameV2, ...], SynchronizedTelemetryReceiptV2]:
        if self._run_fd is None:
            raise SynchronizedTelemetryEvidenceError("telemetry run descriptor is unavailable")
        return _replay_artifacts_at(self._run_fd, expect_seal=False)[:2]

    def write_seal(self, seal: SynchronizedTelemetrySealV2) -> None:
        if self._run_fd is None or self._root_fd is None:
            raise SynchronizedTelemetryEvidenceError("telemetry seal descriptors are unavailable")
        payload = _canonical_json_bytes(seal.model_dump(mode="json"))
        if len(payload) > self._limits.maximum_receipt_bytes:
            raise _ArtifactBoundExceeded("telemetry seal exceeds its bound")
        descriptor = _open_new_private_at(self._run_fd, SEAL_FILENAME)
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(self._run_fd)
        os.close(self._run_fd)
        self._run_fd = None
        try:
            os.fsync(self._root_fd)
        finally:
            os.close(self._root_fd)
            self._root_fd = None
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
        _close_descriptor(self._run_fd)
        _close_descriptor(self._root_fd)
        self._run_fd = None
        self._root_fd = None
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


def run_synchronized_telemetry_observer(
    *,
    artifact_root: Path,
    process_profile: contract_module.ProcessProfileLifecycle,
    gpu_sampler: GpuTelemetrySamplerPort,
    host_sampler: HostTelemetrySamplerPort,
    duration_seconds: float,
    gpu_interval_ms: int = 250,
    run_id: str | None = None,
    cancel_event: threading.Event | None = None,
    limits: SynchronizedObserverLimits | None = None,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    process_cpu_ns: Callable[[], int] = time.process_time_ns,
) -> SynchronizedObserverResult:
    """Run independent absolute-deadline lanes and seal replayable evidence."""

    state = ObserverState.INIT
    if not 250 <= gpu_interval_ms <= 500:
        raise ValueError("GPU telemetry cadence must be 250-500ms")
    if not math.isfinite(duration_seconds) or not duration_seconds > 0:
        raise ValueError("telemetry duration must be positive")
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
    try:
        start_wall = utc_now()
        _require_utc(start_wall)
        start_monotonic = monotonic_ns()
        process_cpu_started = process_cpu_ns()
        end_deadline = start_monotonic + int(duration_seconds * 1_000_000_000)
        observer_source = synchronized_observer_source_sha256()
    except BaseException as exc:
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
        clock_domain_identity_sha256=process_profile.clock_domain_identity_sha256,
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
                "sampler": gpu_sampler.snapshot,
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
            },
            name="synchronized-telemetry-gpu-fast",
            daemon=False,
        ),
        threading.Thread(
            target=_run_lane,
            kwargs={
                "lane": "host_slow",
                "interval_ns": 1_000_000_000,
                "sampler": host_sampler.snapshot,
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
            },
            name="synchronized-telemetry-host-slow",
            daemon=False,
        ),
    ]
    frames: list[SynchronizedTelemetryFrameV2] = []
    state = ObserverState.RUNNING
    started_threads: list[threading.Thread] = []
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
            observer_source_sha256=observer_source,
            termination=termination,
            internal_stop=internal_stop,
            gpu_interval_ms=gpu_interval_ms,
        )
        state = ObserverState.DRAINING
        for thread in started_threads:
            thread.join()
        finish_monotonic = max(
            monotonic_ns(),
            max(frame.clock.finished_monotonic_ns for frame in frames),
        )
        finish_wall = utc_now()
        _require_utc(finish_wall)
        frame_digest = writer.close_frames()
        frame_tuple = tuple(frames)
        lane_quality, unsupported_count = derive_frame_evidence(
            cast(tuple[contract_module.SynchronizedTelemetryFrame, ...], frame_tuple),
            started_monotonic_ns=start_monotonic,
            finished_monotonic_ns=finish_monotonic,
        )
        wall_ns = int((finish_wall - start_wall).total_seconds() * 1_000_000_000)
        monotonic_elapsed_ns = finish_monotonic - start_monotonic
        clock_divergence_ns = abs(wall_ns - monotonic_elapsed_ns)
        termination_reason = termination.value()
        receipt_payload = {
            "run_id": resolved_run_id,
            "runtime_bundle_identity_sha256": (
                process_profile.runtime_bundle_identity_sha256
            ),
            "process_profile": process_profile,
            "observer_source_sha256": observer_source,
            "clock_domain_identity_sha256": (
                process_profile.clock_domain_identity_sha256
            ),
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
        receipt = SynchronizedTelemetryReceiptV2.model_validate(receipt_payload)
        validate_synchronized_telemetry_v2(frame_tuple, receipt=receipt)
        receipt_bytes = writer.write_receipt(receipt)
        replay_frames, replay_receipt = writer.replay_unsealed()
        if replay_frames != frame_tuple or replay_receipt != receipt:
            raise SynchronizedTelemetryEvidenceError("mandatory pre-seal replay drifted")
        process_cpu_finished = process_cpu_ns()
        seal = SynchronizedTelemetrySealV2(
            run_id=resolved_run_id,
            receipt_sha256="sha256:" + hashlib.sha256(receipt_bytes).hexdigest(),
            frames_jsonl_sha256=frame_digest,
            observer_process_cpu_started_ns=process_cpu_started,
            observer_process_cpu_finished_ns=process_cpu_finished,
            observer_cpu_ns=process_cpu_finished - process_cpu_started,
            attested_elapsed_ns=monotonic_elapsed_ns,
            receipt_status=receipt.status,
            status=(
                "unsafe"
                if receipt.status == "unsafe"
                or (process_cpu_finished - process_cpu_started) / monotonic_elapsed_ns > 0.02
                else receipt.status
            ),
        )
        writer.write_seal(seal)
        replay = verify_synchronized_telemetry_observer(
            artifact_root=artifact_root, run_id=resolved_run_id
        )
        state = ObserverState.SEALED
        return replay
    except BaseException as exc:
        state = ObserverState.FAILED_EVIDENCE
        internal_stop.set()
        for thread in started_threads:
            thread.join()
        writer.abort()
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise SynchronizedTelemetryEvidenceError(
            f"synchronized telemetry evidence failed in {state.value}"
        ) from exc


def verify_synchronized_telemetry_observer(
    *, artifact_root: Path, run_id: str
) -> SynchronizedObserverResult:
    """Replay exact private files and recompute hashes, clocks, CPU and lanes."""

    canonical_run_id = str(uuid.UUID(run_id))
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
            frames, receipt, seal = _replay_artifacts_at(run_fd, expect_seal=True)
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
    )


def _replay_artifacts_at(
    run_fd: int, *, expect_seal: bool
) -> tuple[
    tuple[SynchronizedTelemetryFrameV2, ...],
    SynchronizedTelemetryReceiptV2,
    SynchronizedTelemetrySealV2 | None,
]:
    expected = [FRAME_FILENAME, RECEIPT_FILENAME]
    if expect_seal:
        expected.append(SEAL_FILENAME)
    if sorted(os.listdir(run_fd)) != sorted(expected):
        raise ValueError("telemetry run artifacts are incomplete or unexpected")
    frames_payload = _read_private_file_at(run_fd, FRAME_FILENAME, maximum_bytes=MAX_FRAME_FILE_BYTES)
    receipt_payload = _read_private_file_at(run_fd, RECEIPT_FILENAME, maximum_bytes=MAX_RECEIPT_BYTES)
    frame_values = parse_canonical_jsonl_artifact(
        frames_payload,
        label="frames",
        maximum_bytes=MAX_FRAME_FILE_BYTES,
        maximum_record_bytes=MAX_FRAME_RECORD_BYTES,
        maximum_records=MAX_FRAME_RECORDS,
    )
    frames = tuple(SynchronizedTelemetryFrameV2.model_validate(value) for value in frame_values)
    receipt = SynchronizedTelemetryReceiptV2.model_validate(
        parse_canonical_json_artifact(receipt_payload, label="receipt", maximum_bytes=MAX_RECEIPT_BYTES)
    )
    frames_hash = canonical_jsonl_artifact_sha256(frames_payload, label="frames")
    if frames_hash != receipt.artifacts.frames_jsonl_sha256:
        raise ValueError("telemetry frames artifact hash drifted")
    validate_synchronized_telemetry_v2(frames, receipt=receipt)
    seal: SynchronizedTelemetrySealV2 | None = None
    if expect_seal:
        seal_payload = _read_private_file_at(run_fd, SEAL_FILENAME, maximum_bytes=MAX_RECEIPT_BYTES)
        seal = SynchronizedTelemetrySealV2.model_validate(
            parse_canonical_json_artifact(seal_payload, label="seal", maximum_bytes=MAX_RECEIPT_BYTES)
        )
        receipt_hash = "sha256:" + hashlib.sha256(receipt_payload).hexdigest()
        if seal.run_id != receipt.run_id or seal.receipt_sha256 != receipt_hash or seal.frames_jsonl_sha256 != frames_hash:
            raise ValueError("telemetry seal artifact identity drifted")
        if seal.attested_elapsed_ns != receipt.finished_monotonic_ns - receipt.started_monotonic_ns:
            raise ValueError("telemetry seal elapsed interval drifted")
        expected_status = "unsafe" if receipt.status == "unsafe" or seal.observer_cpu_ns / seal.attested_elapsed_ns > 0.02 else receipt.status
        if seal.receipt_status != receipt.status or seal.status != expected_status:
            raise ValueError("telemetry seal status drifted")
    return frames, receipt, seal


def validate_synchronized_telemetry_v2(
    frames: tuple[SynchronizedTelemetryFrameV2, ...],
    *,
    receipt: SynchronizedTelemetryReceiptV2,
) -> None:
    if not frames:
        raise ValueError("telemetry frame sequence is empty")
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


def _run_lane(
    *,
    lane: Literal["gpu_fast", "host_slow"],
    interval_ns: int,
    sampler: Callable[..., GpuLaneSnapshot | HostLaneSnapshot],
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
) -> None:
    scheduled = start_monotonic
    emitted = False
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
            started = monotonic_ns()
            observed_at = utc_now()
            _require_utc(observed_at)
            try:
                snapshot_deadline = min(end_deadline, scheduled + interval_ns)
                snapshot = sampler(
                    deadline=TelemetrySnapshotDeadline(monotonic_ns=snapshot_deadline)
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
            except _SafetyDrift as exc:
                finished = monotonic_ns()
                pending = _unsupported_pending(
                    lane=lane,
                    scheduled=scheduled,
                    started=started,
                    finished=finished,
                    observed_at=observed_at,
                    reason=exc.reason,
                )
                safety_drifts.add(exc.reason)
                termination.mark("identity_drift")
                internal_stop.set()
            except TelemetrySnapshotDeadlineExceeded:
                finished = monotonic_ns()
                pending = _unsupported_pending(
                    lane=lane,
                    scheduled=scheduled,
                    started=started,
                    finished=finished,
                    observed_at=observed_at,
                    reason="deadline_exceeded",
                )
                termination.mark("sampler_or_transport_shutdown")
                internal_stop.set()
            except TelemetrySnapshotTransportUnavailable:
                finished = monotonic_ns()
                pending = _unsupported_pending(
                    lane=lane,
                    scheduled=scheduled,
                    started=started,
                    finished=finished,
                    observed_at=observed_at,
                    reason="endpoint_unreachable",
                )
                termination.mark("sampler_or_transport_shutdown")
                internal_stop.set()
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
            fallback = _unsupported_pending(
                lane=lane,
                scheduled=start_monotonic,
                started=now,
                finished=now,
                observed_at=utc_now(),
                reason=(safety_drifts.values()[0] if safety_drifts.values() else "collector_disabled"),
            )
            mailbox.fallback = fallback
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
    frames: list[SynchronizedTelemetryFrameV2],
    run_id: str,
    process_profile: contract_module.ProcessProfileLifecycle,
    observer_source_sha256: str,
    termination: _Termination,
    internal_stop: threading.Event,
    gpu_interval_ms: int,
) -> None:
    heads: dict[str, _PendingSample | None] = {
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
        frame = SynchronizedTelemetryFrameV2(
            run_id=run_id,
            sequence=len(frames),
            lane=pending.lane,
            runtime_bundle_identity_sha256=(
                process_profile.runtime_bundle_identity_sha256
            ),
            process_profile_sha256=process_profile.process_profile_sha256,
            observer_source_sha256=observer_source_sha256,
            clock=SampleClock(
                clock_domain_identity_sha256=(
                    process_profile.clock_domain_identity_sha256
                ),
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
        )
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
        gpu=_unsupported_gpu("not_due_at_this_tick"),
        api_process=ApiProcessObservationV2.model_validate(snapshot.api_process.model_dump()),
        host_cgroup=HostCgroupObservationV2.model_validate(snapshot.host_cgroup.model_dump()),
        queue_vllm=QueueVllmObservationV2.model_validate(snapshot.queue_vllm.model_dump()),
    )


class _SafetyDrift(ValueError):
    def __init__(self, reason: contract_module.SafetyDriftReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


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
    *, mailbox: _Mailbox, pending: _PendingSample, condition: threading.Condition
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


def _open_or_create_private_directory(path: Path) -> int:
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
    finally:
        os.close(parent_descriptor)
    return _open_existing_private_directory(path, label="telemetry root")


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


def _open_new_private_at(directory_fd: int, filename: str) -> int:
    descriptor = os.open(
        filename,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
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
    directory_fd: int, filename: str, *, maximum_bytes: int
) -> bytes:
    descriptor = os.open(
        filename,
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size > maximum_bytes
        ):
            raise ValueError("telemetry artifact is not a bounded private file")
        remaining = metadata.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("telemetry artifact changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("telemetry artifact changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


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
    "ObserverState",
    "SynchronizedObserverLimits",
    "SynchronizedObserverResult",
    "SynchronizedTelemetryEvidenceError",
    "run_synchronized_telemetry_observer",
    "synchronized_observer_source_sha256",
    "verify_synchronized_telemetry_observer",
    "validate_synchronized_telemetry_v2",
]
