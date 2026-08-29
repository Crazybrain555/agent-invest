"""Closed, content-free contracts for synchronized MinerU capacity telemetry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator


TELEMETRY_FRAME_VERSION: Literal["mineru.synchronized-telemetry-frame.v1"] = (
    "mineru.synchronized-telemetry-frame.v1"
)
TELEMETRY_RECEIPT_VERSION: Literal[
    "mineru.synchronized-telemetry-receipt.v1"
] = "mineru.synchronized-telemetry-receipt.v1"
TELEMETRY_FRAME_V2_VERSION: Literal["mineru.synchronized-telemetry-frame.v2"] = (
    "mineru.synchronized-telemetry-frame.v2"
)
TELEMETRY_RECEIPT_V2_VERSION: Literal["mineru.synchronized-telemetry-receipt.v2"] = (
    "mineru.synchronized-telemetry-receipt.v2"
)
TELEMETRY_SEAL_V2_VERSION: Literal["mineru.synchronized-telemetry-seal.v2"] = (
    "mineru.synchronized-telemetry-seal.v2"
)
PROGRESS_EVENT_VERSION: Literal["mineru.capacity-progress-event.v1"] = (
    "mineru.capacity-progress-event.v1"
)
VECTOR_CREDIT_VERSION: Literal["mineru.capacity-vector-credit-event.v1"] = (
    "mineru.capacity-vector-credit-event.v1"
)
PHASE_SUMMARY_VERSION: Literal["mineru.synchronized-phase-summary.v1"] = (
    "mineru.synchronized-phase-summary.v1"
)
PHASE_CLOCK_BINDING_VERSION: Literal["mineru.phase-clock-binding.v1"] = (
    "mineru.phase-clock-binding.v1"
)

_SCHEMA_ROOT = "https://agent-invest.local/contracts/operational/"
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_UUID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-"
    r"[89ab][a-f0-9]{3}-[a-f0-9]{12}$"
)

TelemetryLane = Literal["gpu_fast", "host_slow"]
ObservationStatus = Literal["supported", "unsupported"]
UnsupportedReason = Literal[
    "collector_disabled",
    "collector_unsupported",
    "deadline_exceeded",
    "endpoint_unreachable",
    "epoch_drift",
    "not_due_at_this_tick",
]
UnsupportedReasonV2 = Literal[
    "collector_disabled",
    "collector_unsupported",
    "deadline_exceeded",
    "endpoint_unreachable",
    "epoch_drift",
    "identity_drift",
    "counter_regression",
    "oom_increment",
    "not_due_at_this_tick",
]
RunStatus = Literal["complete", "incomplete", "unsafe"]
ObserverTerminationReason = Literal[
    "duration_elapsed",
    "cancelled",
    "sampler_or_transport_shutdown",
    "queue_overflow",
    "artifact_bound_exceeded",
    "identity_drift",
]
SafetyDriftReason = Literal[
    "epoch_drift",
    "identity_drift",
    "counter_regression",
    "oom_increment",
]
ProfileOutcome = Literal["running", "succeeded", "failed", "drained"]
BlockedReason = Literal[
    "api_pending_limit",
    "cpu_stage_starved",
    "durable_publish_backpressure",
    "gpu_input_starved",
    "gpu_memory_pressure",
    "host_memory_pressure",
    "inference_unavailable",
    "profile_drain",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _sha256(value: str, *, label: str) -> None:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is not a canonical SHA-256")


def _run_id(value: str) -> None:
    if _UUID_RE.fullmatch(value) is None:
        raise ValueError("run_id is not a canonical UUID")


def _utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{label} must be UTC")


class PressureLine(_FrozenModel):
    avg10_pct: float = Field(ge=0, le=100)
    avg60_pct: float = Field(ge=0, le=100)
    avg300_pct: float = Field(ge=0, le=100)
    total_stall_us: int = Field(ge=0)


class PressureSample(_FrozenModel):
    some: PressureLine
    full_status: ObservationStatus
    full_reason: UnsupportedReason | None
    full: PressureLine | None

    @model_validator(mode="after")
    def _check_full(self) -> "PressureSample":
        if self.full_status == "supported":
            if self.full is None or self.full_reason is not None:
                raise ValueError("supported PSI full line requires values")
        elif self.full is not None or self.full_reason is None:
            raise ValueError("unsupported PSI full line requires one reason")
        return self


class GpuTelemetryValues(_FrozenModel):
    device_identity_sha256: str
    utilization_pct: float = Field(ge=0, le=100)
    framebuffer_used_bytes: int = Field(ge=0)
    framebuffer_free_bytes: int = Field(ge=0)
    framebuffer_total_bytes: int = Field(ge=1)
    power_usage_watts: float = Field(ge=0, le=1000)

    @model_validator(mode="after")
    def _check_gpu(self) -> "GpuTelemetryValues":
        _sha256(self.device_identity_sha256, label="device_identity_sha256")
        if (
            self.framebuffer_used_bytes > self.framebuffer_total_bytes
            or self.framebuffer_free_bytes > self.framebuffer_total_bytes
        ):
            raise ValueError("GPU framebuffer values exceed total")
        return self


class ApiProcessTelemetryValues(_FrozenModel):
    process_epoch_sha256: str
    cpu_user_ns_total: int = Field(ge=0)
    cpu_system_ns_total: int = Field(ge=0)
    rss_bytes: int = Field(ge=0)
    rss_hwm_bytes: int = Field(ge=0)
    thread_count: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_process(self) -> "ApiProcessTelemetryValues":
        _sha256(self.process_epoch_sha256, label="process_epoch_sha256")
        if self.rss_hwm_bytes < self.rss_bytes:
            raise ValueError("process RSS exceeds its high-water mark")
        return self


class CgroupMemoryStat(_FrozenModel):
    anon_bytes: int = Field(ge=0)
    file_bytes: int = Field(ge=0)
    shmem_bytes: int = Field(ge=0)
    slab_bytes: int = Field(ge=0)


class CgroupMemoryEvents(_FrozenModel):
    low_total: int = Field(ge=0)
    high_total: int = Field(ge=0)
    max_total: int = Field(ge=0)
    oom_total: int = Field(ge=0)
    oom_kill_total: int = Field(ge=0)
    oom_group_kill_total: int = Field(ge=0)


class CgroupCpuStat(_FrozenModel):
    usage_ns_total: int = Field(ge=0)
    user_ns_total: int = Field(ge=0)
    system_ns_total: int = Field(ge=0)
    throttled_ns_total: int = Field(ge=0)
    throttled_periods_total: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_cpu(self) -> "CgroupCpuStat":
        if self.user_ns_total + self.system_ns_total > self.usage_ns_total:
            raise ValueError("cgroup CPU components exceed total usage")
        return self


class HostCgroupTelemetryValues(_FrozenModel):
    parent_cgroup_epoch_sha256: str
    docker_vm_memory_total_bytes: int = Field(ge=1)
    docker_vm_memory_available_bytes: int = Field(ge=0)
    memory_current_bytes: int = Field(ge=0)
    memory_max_status: Literal["bounded", "unbounded"]
    memory_max_bytes: int | None
    memory_stat: CgroupMemoryStat
    memory_events: CgroupMemoryEvents
    memory_psi: PressureSample
    cpu_stat: CgroupCpuStat
    cpu_psi: PressureSample
    io_psi: PressureSample

    @model_validator(mode="after")
    def _check_host(self) -> "HostCgroupTelemetryValues":
        _sha256(
            self.parent_cgroup_epoch_sha256,
            label="parent_cgroup_epoch_sha256",
        )
        if self.docker_vm_memory_available_bytes > self.docker_vm_memory_total_bytes:
            raise ValueError("Docker VM available memory exceeds total")
        if self.memory_max_status == "bounded":
            if self.memory_max_bytes is None or self.memory_current_bytes > self.memory_max_bytes:
                raise ValueError("bounded cgroup memory limit is invalid")
        elif self.memory_max_bytes is not None:
            raise ValueError("unbounded cgroup cannot carry a numeric limit")
        return self


class QueueVllmTelemetryValues(_FrozenModel):
    api_queued_tasks: int = Field(ge=0)
    api_processing_tasks: int = Field(ge=0)
    api_nonterminal_tasks: int = Field(ge=0)
    api_http_active_requests: int = Field(ge=0)
    api_http_pending_requests: int = Field(ge=0)
    api_max_pending_tasks: int = Field(ge=1)
    vllm_requests_running: int = Field(ge=0)
    vllm_requests_waiting: int = Field(ge=0)
    vllm_kv_cache_usage_ratio: float = Field(ge=0, le=1)
    vllm_preemptions_total: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_queue(self) -> "QueueVllmTelemetryValues":
        if self.api_nonterminal_tasks != (
            self.api_queued_tasks + self.api_processing_tasks
        ):
            raise ValueError("API nonterminal task count does not reconcile")
        if self.api_nonterminal_tasks > self.api_max_pending_tasks:
            raise ValueError("API nonterminal task count exceeds admission limit")
        return self


class _Observation(_FrozenModel):
    status: ObservationStatus
    reason: UnsupportedReason | None


class GpuObservation(_Observation):
    values: GpuTelemetryValues | None

    @model_validator(mode="after")
    def _check_values(self) -> "GpuObservation":
        return _check_observation(self)


class ApiProcessObservation(_Observation):
    values: ApiProcessTelemetryValues | None

    @model_validator(mode="after")
    def _check_values(self) -> "ApiProcessObservation":
        return _check_observation(self)


class HostCgroupObservation(_Observation):
    values: HostCgroupTelemetryValues | None

    @model_validator(mode="after")
    def _check_values(self) -> "HostCgroupObservation":
        return _check_observation(self)


class QueueVllmObservation(_Observation):
    values: QueueVllmTelemetryValues | None

    @model_validator(mode="after")
    def _check_values(self) -> "QueueVllmObservation":
        return _check_observation(self)


def _check_observation(value: Any) -> Any:
    if value.status == "supported":
        if value.values is None or value.reason is not None:
            raise ValueError("supported observation requires values and no reason")
    elif value.values is not None or value.reason is None:
        raise ValueError("unsupported observation requires one reason and no values")
    return value


class SampleClock(_FrozenModel):
    clock_domain_identity_sha256: str
    observed_at_utc: datetime
    scheduled_monotonic_ns: int = Field(ge=0)
    started_monotonic_ns: int = Field(ge=0)
    finished_monotonic_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_clock(self) -> "SampleClock":
        _sha256(
            self.clock_domain_identity_sha256,
            label="clock_domain_identity_sha256",
        )
        _utc(self.observed_at_utc, label="observed_at_utc")
        if not (
            self.scheduled_monotonic_ns
            <= self.started_monotonic_ns
            <= self.finished_monotonic_ns
        ):
            raise ValueError("sample monotonic clock is invalid")
        return self


class SampleQuality(_FrozenModel):
    nominal_interval_ms: int = Field(ge=250, le=1000)
    observed_interval_ms: float | None = Field(default=None, ge=0)
    collection_duration_ms: float = Field(ge=0)
    missed_deadlines: int = Field(ge=0)
    status: Literal["on_time", "late", "first"]

    @model_validator(mode="after")
    def _check_quality(self) -> "SampleQuality":
        if self.status == "first":
            if self.observed_interval_ms is not None:
                raise ValueError("first sample cannot carry an observed interval")
        elif self.observed_interval_ms is None:
            raise ValueError("non-first sample requires an observed interval")
        if self.status == "on_time" and self.missed_deadlines != 0:
            raise ValueError("on-time sample cannot have missed deadlines")
        if self.status == "late" and self.missed_deadlines == 0:
            raise ValueError("late sample requires a missed deadline")
        return self


class SynchronizedTelemetryFrame(_FrozenModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_extra={
            "$id": _SCHEMA_ROOT + "synchronized-telemetry-frame.v1.schema.json"
        },
    )

    contract_version: Literal["mineru.synchronized-telemetry-frame.v1"] = (
        TELEMETRY_FRAME_VERSION
    )
    run_id: str
    sequence: int = Field(ge=0)
    lane: TelemetryLane
    runtime_bundle_identity_sha256: str
    process_profile_sha256: str
    observer_source_sha256: str
    clock: SampleClock
    quality: SampleQuality
    gpu: GpuObservation
    api_process: ApiProcessObservation
    host_cgroup: HostCgroupObservation
    queue_vllm: QueueVllmObservation

    @model_validator(mode="after")
    def _check_frame(self) -> "SynchronizedTelemetryFrame":
        _run_id(self.run_id)
        for label, value in (
            ("runtime_bundle_identity_sha256", self.runtime_bundle_identity_sha256),
            ("process_profile_sha256", self.process_profile_sha256),
            ("observer_source_sha256", self.observer_source_sha256),
        ):
            _sha256(value, label=label)
        if self.lane == "gpu_fast":
            if not 250 <= self.quality.nominal_interval_ms <= 500:
                raise ValueError("GPU telemetry cadence must be 250-500ms")
        else:
            if self.quality.nominal_interval_ms != 1000:
                raise ValueError("host telemetry cadence must be 1s")
        duration_ms = (
            self.clock.finished_monotonic_ns - self.clock.started_monotonic_ns
        ) / 1_000_000
        if abs(duration_ms - self.quality.collection_duration_ms) > 0.001:
            raise ValueError("sample duration differs from monotonic clock")
        return self


class ProcessProfileParameters(_FrozenModel):
    requested_hybrid_batch_ratio: int = Field(ge=1)
    effective_hybrid_batch_ratio: int = Field(ge=1)
    api_task_slots: int = Field(ge=1)
    api_max_pending_tasks: int = Field(ge=1)
    inference_concurrency: int = Field(ge=1)
    processing_window_size: int = Field(ge=1)
    vllm_max_num_seqs: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_profile(self) -> "ProcessProfileParameters":
        if self.api_task_slots > self.api_max_pending_tasks:
            raise ValueError("task slots exceed pending-task admission")
        if self.inference_concurrency > self.vllm_max_num_seqs:
            raise ValueError("inference concurrency exceeds vLLM sequence limit")
        return self


class ProcessProfileLifecycle(_FrozenModel):
    lifecycle: Literal["startup_only"] = "startup_only"
    runtime_bundle_identity_sha256: str
    process_epoch_sha256: str
    process_profile_sha256: str
    clock_domain_identity_sha256: str
    started_at_utc: datetime
    started_monotonic_ns: int = Field(ge=0)
    parameters: ProcessProfileParameters

    @model_validator(mode="after")
    def _check_lifecycle(self) -> "ProcessProfileLifecycle":
        _utc(self.started_at_utc, label="started_at_utc")
        for label, value in (
            ("runtime_bundle_identity_sha256", self.runtime_bundle_identity_sha256),
            ("process_epoch_sha256", self.process_epoch_sha256),
            ("process_profile_sha256", self.process_profile_sha256),
            ("clock_domain_identity_sha256", self.clock_domain_identity_sha256),
        ):
            _sha256(value, label=label)
        return self


class DocumentProfileLifecycle(_FrozenModel):
    lifecycle: Literal["document_frozen"] = "document_frozen"
    attempt_identity_sha256: str
    process_profile_sha256: str
    document_profile_sha256: str
    started_monotonic_ns: int = Field(ge=0)
    finished_monotonic_ns: int | None = Field(default=None, ge=0)
    window_size: int = Field(ge=1)
    pipeline_depth: int = Field(ge=0)
    max_resident_pages: int = Field(ge=1)
    outcome: ProfileOutcome

    @model_validator(mode="after")
    def _check_document(self) -> "DocumentProfileLifecycle":
        for label, value in (
            ("attempt_identity_sha256", self.attempt_identity_sha256),
            ("process_profile_sha256", self.process_profile_sha256),
            ("document_profile_sha256", self.document_profile_sha256),
        ):
            _sha256(value, label=label)
        if self.pipeline_depth > 0 and (
            self.window_size * (self.pipeline_depth + 1) > self.max_resident_pages
        ):
            raise ValueError("document profile exceeds resident-page credits")
        if self.outcome == "running":
            if self.finished_monotonic_ns is not None:
                raise ValueError("running document cannot be finished")
        elif (
            self.finished_monotonic_ns is None
            or self.finished_monotonic_ns < self.started_monotonic_ns
        ):
            raise ValueError("terminal document requires a valid finish")
        return self


class CapacityVector(_FrozenModel):
    source_disk_bytes: int = Field(ge=0)
    raster_cpu_bytes: int = Field(ge=0)
    tensor_cpu_bytes: int = Field(ge=0)
    tensor_gpu_bytes: int = Field(ge=0)
    model_cpu_bytes: int = Field(ge=0)
    model_gpu_bytes: int = Field(ge=0)
    document_owner_bytes: int = Field(ge=0)
    task_slots: int = Field(ge=0)
    native_owner_slots: int = Field(ge=0)
    vllm_sequence_slots: int = Field(ge=0)


_VECTOR_FIELDS = tuple(CapacityVector.model_fields)


def _vector_map(value: CapacityVector) -> dict[str, int]:
    return {name: int(getattr(value, name)) for name in _VECTOR_FIELDS}


def _vector_add(*values: CapacityVector) -> CapacityVector:
    return CapacityVector(
        **{name: sum(getattr(value, name) for value in values) for name in _VECTOR_FIELDS}
    )


def _vector_subtract(left: CapacityVector, right: CapacityVector) -> CapacityVector:
    payload = {
        name: getattr(left, name) - getattr(right, name) for name in _VECTOR_FIELDS
    }
    if any(value < 0 for value in payload.values()):
        raise ValueError("capacity vector would become negative")
    return CapacityVector(**payload)


class MeasuredSafetyMargin(_FrozenModel):
    method: Literal["p99-mad-positive-jump.v1"] = "p99-mad-positive-jump.v1"
    sample_count: int = Field(ge=1)
    capacity: CapacityVector
    model_baseline: CapacityVector
    uncertainty: CapacityVector
    safety_margin: CapacityVector

    @model_validator(mode="after")
    def _check_margin(self) -> "MeasuredSafetyMargin":
        uncertainty = _vector_map(self.uncertainty)
        margin = _vector_map(self.safety_margin)
        if any(margin[name] < uncertainty[name] for name in _VECTOR_FIELDS):
            raise ValueError("safety margin is below measured uncertainty")
        _vector_subtract(
            self.capacity,
            _vector_add(self.model_baseline, self.safety_margin),
        )
        return self


class CapacityVectorSnapshot(_FrozenModel):
    capacity: CapacityVector
    model_baseline: CapacityVector
    safety_margin: CapacityVector
    active_reserved: CapacityVector
    available: CapacityVector

    @model_validator(mode="after")
    def _check_conservation(self) -> "CapacityVectorSnapshot":
        expected = _vector_add(
            self.model_baseline,
            self.safety_margin,
            self.active_reserved,
            self.available,
        )
        if expected != self.capacity:
            raise ValueError("capacity vector does not conserve credits")
        return self


class CapacityVectorCreditEvent(_FrozenModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_extra={
            "$id": _SCHEMA_ROOT + "capacity-vector-credit-event.v1.schema.json"
        },
    )

    contract_version: Literal["mineru.capacity-vector-credit-event.v1"] = (
        VECTOR_CREDIT_VERSION
    )
    sequence: int = Field(ge=0)
    process_epoch_sha256: str
    lease_identity_sha256: str
    action: Literal["reserve", "borrow", "return"]
    delta: CapacityVector
    available_before: CapacityVector
    available_after: CapacityVector
    monotonic_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_event(self) -> "CapacityVectorCreditEvent":
        _sha256(self.process_epoch_sha256, label="process_epoch_sha256")
        _sha256(self.lease_identity_sha256, label="lease_identity_sha256")
        expected = (
            _vector_add(self.available_before, self.delta)
            if self.action == "return"
            else _vector_subtract(self.available_before, self.delta)
        )
        if expected != self.available_after:
            raise ValueError("credit event does not conserve its resource dimensions")
        return self


def validate_credit_event_chain(
    events: tuple[CapacityVectorCreditEvent, ...],
    *,
    require_closed: bool,
) -> None:
    if not events:
        raise ValueError("credit event chain is empty")
    epoch = events[0].process_epoch_sha256
    lease_balances: dict[str, CapacityVector] = {}
    zero = CapacityVector(**{name: 0 for name in _VECTOR_FIELDS})
    previous_after: CapacityVector | None = None
    previous_monotonic = -1
    for index, event in enumerate(events):
        if event.sequence != index or event.process_epoch_sha256 != epoch:
            raise ValueError("credit event sequence or epoch drifted")
        if previous_after is not None and event.available_before != previous_after:
            raise ValueError("credit event availability chain drifted")
        if event.monotonic_ns < previous_monotonic:
            raise ValueError("credit event monotonic order drifted")
        balance = lease_balances.get(event.lease_identity_sha256, zero)
        if event.action == "return":
            balance = _vector_subtract(balance, event.delta)
        else:
            balance = _vector_add(balance, event.delta)
        lease_balances[event.lease_identity_sha256] = balance
        previous_after = event.available_after
        previous_monotonic = event.monotonic_ns
    if require_closed and any(balance != zero for balance in lease_balances.values()):
        raise ValueError("credit event chain has an unreturned lease")


class _ProgressBase(_FrozenModel):
    contract_version: Literal["mineru.capacity-progress-event.v1"] = (
        PROGRESS_EVENT_VERSION
    )
    run_id: str
    sequence: int = Field(ge=0)
    process_epoch_sha256: str
    process_profile_sha256: str
    clock_domain_identity_sha256: str
    observed_at_utc: datetime
    monotonic_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_base(self) -> "_ProgressBase":
        _run_id(self.run_id)
        _utc(self.observed_at_utc, label="observed_at_utc")
        _sha256(self.process_epoch_sha256, label="process_epoch_sha256")
        _sha256(self.process_profile_sha256, label="process_profile_sha256")
        _sha256(
            self.clock_domain_identity_sha256,
            label="clock_domain_identity_sha256",
        )
        return self


class BlockedProgressEvent(_ProgressBase):
    event_type: Literal["blocked"] = "blocked"
    blocked_reason: BlockedReason
    blocked_interval_started_monotonic_ns: int = Field(ge=0)
    blocked_duration_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_interval(self) -> "BlockedProgressEvent":
        if (
            self.blocked_interval_started_monotonic_ns > self.monotonic_ns
            or self.blocked_duration_ns
            != self.monotonic_ns - self.blocked_interval_started_monotonic_ns
        ):
            raise ValueError("blocked interval duration is not closed")
        return self


class DurablePageCommitEvent(_ProgressBase):
    event_type: Literal["unique_durable_pages_committed"] = (
        "unique_durable_pages_committed"
    )
    source_identity_sha256: str
    committed_source_pages: int = Field(ge=1)
    cumulative_unique_source_pages: int = Field(ge=1)
    commit_latency_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_source(self) -> "DurablePageCommitEvent":
        _sha256(self.source_identity_sha256, label="source_identity_sha256")
        if self.committed_source_pages > self.cumulative_unique_source_pages:
            raise ValueError("durable page delta exceeds cumulative pages")
        return self


ProgressEvent = Annotated[
    Union[BlockedProgressEvent, DurablePageCommitEvent],
    Field(discriminator="event_type"),
]


class CapacityProgressEventEnvelope(RootModel[ProgressEvent]):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "$id": _SCHEMA_ROOT + "capacity-progress-event.v1.schema.json"
        },
    )


class LaneQualitySummary(_FrozenModel):
    lane: TelemetryLane
    nominal_interval_ms: int = Field(ge=250, le=1000)
    sample_count: int = Field(ge=1)
    maximum_gap_ms: float = Field(ge=0)
    late_sample_count: int = Field(ge=0)
    missed_deadline_count: int = Field(ge=0)
    supported_frame_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_counts(self) -> "LaneQualitySummary":
        if (
            self.late_sample_count > self.sample_count
            or self.supported_frame_count > self.sample_count
        ):
            raise ValueError("lane quality counts exceed sample count")
        return self


class TelemetryArtifacts(_FrozenModel):
    frames_sha256: str
    progress_events_sha256: str | None
    vector_events_sha256: str | None
    phase_capture_sha256: str | None = None
    phase_clock_binding_sha256: str | None = None

    @model_validator(mode="after")
    def _check_artifacts(self) -> "TelemetryArtifacts":
        _sha256(self.frames_sha256, label="frames_sha256")
        for label, value in (
            ("progress_events_sha256", self.progress_events_sha256),
            ("vector_events_sha256", self.vector_events_sha256),
            ("phase_capture_sha256", self.phase_capture_sha256),
            ("phase_clock_binding_sha256", self.phase_clock_binding_sha256),
        ):
            if value is not None:
                _sha256(value, label=label)
        return self


class SynchronizedTelemetryReceipt(_FrozenModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_extra={
            "$id": _SCHEMA_ROOT + "synchronized-telemetry-receipt.v1.schema.json"
        },
    )

    contract_version: Literal["mineru.synchronized-telemetry-receipt.v1"] = (
        TELEMETRY_RECEIPT_VERSION
    )
    run_id: str
    runtime_bundle_identity_sha256: str
    process_profile: ProcessProfileLifecycle
    observer_source_sha256: str
    clock_domain_identity_sha256: str
    started_at_utc: datetime
    finished_at_utc: datetime
    started_monotonic_ns: int = Field(ge=0)
    finished_monotonic_ns: int = Field(ge=0)
    status: RunStatus
    lane_quality: tuple[LaneQualitySummary, LaneQualitySummary]
    observer_cpu_ns: int = Field(ge=0)
    maximum_observer_overhead_ratio: float = Field(default=0.02, ge=0.02, le=0.02)
    maximum_clock_divergence_fixed_ns: Literal[50_000_000] = 50_000_000
    maximum_clock_divergence_ppm: Literal[50] = 50
    observed_clock_divergence_ns: int = Field(ge=0)
    epoch_changed: bool
    unsupported_observation_count: int = Field(ge=0)
    safety_margin: MeasuredSafetyMargin | None
    artifacts: TelemetryArtifacts
    activation_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _check_receipt(self) -> "SynchronizedTelemetryReceipt":
        _run_id(self.run_id)
        _utc(self.started_at_utc, label="started_at_utc")
        _utc(self.finished_at_utc, label="finished_at_utc")
        _sha256(
            self.runtime_bundle_identity_sha256,
            label="runtime_bundle_identity_sha256",
        )
        _sha256(self.observer_source_sha256, label="observer_source_sha256")
        _sha256(
            self.clock_domain_identity_sha256,
            label="clock_domain_identity_sha256",
        )
        if (
            self.finished_at_utc <= self.started_at_utc
            or self.finished_monotonic_ns <= self.started_monotonic_ns
        ):
            raise ValueError("telemetry receipt interval is invalid")
        wall_ns = int(
            (self.finished_at_utc - self.started_at_utc).total_seconds()
            * 1_000_000_000
        )
        monotonic_ns = self.finished_monotonic_ns - self.started_monotonic_ns
        clock_divergence_ns = abs(wall_ns - monotonic_ns)
        maximum_clock_divergence_ns = (
            self.maximum_clock_divergence_fixed_ns
            + monotonic_ns * self.maximum_clock_divergence_ppm // 1_000_000
        )
        if self.observed_clock_divergence_ns != clock_divergence_ns:
            raise ValueError("recorded clock divergence disagrees with clocks")
        if clock_divergence_ns > maximum_clock_divergence_ns:
            raise ValueError("wall and monotonic receipt clocks diverged")
        lanes = {item.lane for item in self.lane_quality}
        if lanes != {"gpu_fast", "host_slow"}:
            raise ValueError("receipt requires one quality summary per lane")
        overhead_ratio = self.observer_cpu_ns / monotonic_ns
        unsafe = self.epoch_changed or overhead_ratio > self.maximum_observer_overhead_ratio
        incomplete = self.unsupported_observation_count > 0 or any(
            item.late_sample_count > 0
            or item.missed_deadline_count > 0
            or item.supported_frame_count == 0
            for item in self.lane_quality
        )
        expected: RunStatus = "unsafe" if unsafe else "incomplete" if incomplete else "complete"
        if self.status != expected:
            raise ValueError("receipt status disagrees with quality evidence")
        if self.process_profile.runtime_bundle_identity_sha256 != self.runtime_bundle_identity_sha256:
            raise ValueError("process profile runtime identity drifted")
        if (
            self.process_profile.clock_domain_identity_sha256
            != self.clock_domain_identity_sha256
        ):
            raise ValueError("process profile clock domain drifted")
        return self


class _ObservationV2(_FrozenModel):
    status: ObservationStatus
    reason: UnsupportedReasonV2 | None


class GpuObservationV2(_ObservationV2):
    values: GpuTelemetryValues | None

    @model_validator(mode="after")
    def _check_values(self) -> "GpuObservationV2":
        return _check_observation(self)


class ApiProcessObservationV2(_ObservationV2):
    values: ApiProcessTelemetryValues | None

    @model_validator(mode="after")
    def _check_values(self) -> "ApiProcessObservationV2":
        return _check_observation(self)


class HostCgroupObservationV2(_ObservationV2):
    values: HostCgroupTelemetryValues | None

    @model_validator(mode="after")
    def _check_values(self) -> "HostCgroupObservationV2":
        return _check_observation(self)


class QueueVllmObservationV2(_ObservationV2):
    values: QueueVllmTelemetryValues | None

    @model_validator(mode="after")
    def _check_values(self) -> "QueueVllmObservationV2":
        return _check_observation(self)


class SynchronizedTelemetryFrameV2(_FrozenModel):
    """Streaming frame contract; v1 remains the original JSON-array contract."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_extra={
            "$id": _SCHEMA_ROOT + "synchronized-telemetry-frame.v2.schema.json"
        },
    )
    contract_version: Literal["mineru.synchronized-telemetry-frame.v2"] = (
        TELEMETRY_FRAME_V2_VERSION
    )
    run_id: str
    sequence: int = Field(ge=0)
    lane: TelemetryLane
    runtime_bundle_identity_sha256: str
    process_profile_sha256: str
    observer_source_sha256: str
    clock: SampleClock
    quality: SampleQuality
    gpu: GpuObservationV2
    api_process: ApiProcessObservationV2
    host_cgroup: HostCgroupObservationV2
    queue_vllm: QueueVllmObservationV2

    @model_validator(mode="after")
    def _check_frame(self) -> "SynchronizedTelemetryFrameV2":
        _run_id(self.run_id)
        for label, value in (
            ("runtime_bundle_identity_sha256", self.runtime_bundle_identity_sha256),
            ("process_profile_sha256", self.process_profile_sha256),
            ("observer_source_sha256", self.observer_source_sha256),
        ):
            _sha256(value, label=label)
        if self.lane == "gpu_fast":
            if not 250 <= self.quality.nominal_interval_ms <= 500:
                raise ValueError("GPU telemetry cadence must be 250-500ms")
        elif self.quality.nominal_interval_ms != 1000:
            raise ValueError("host telemetry cadence must be 1s")
        duration_ms = (self.clock.finished_monotonic_ns - self.clock.started_monotonic_ns) / 1_000_000
        if abs(duration_ms - self.quality.collection_duration_ms) > 0.001:
            raise ValueError("sample duration differs from monotonic clock")
        return self


class TelemetryArtifactsV2(_FrozenModel):
    frames_jsonl_sha256: str

    @model_validator(mode="after")
    def _check_hash(self) -> "TelemetryArtifactsV2":
        _sha256(self.frames_jsonl_sha256, label="frames_jsonl_sha256")
        return self


class SynchronizedTelemetryReceiptV2(_FrozenModel):
    """Closed receipt for the default-off resident JSONL observer."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_extra={
            "$id": _SCHEMA_ROOT + "synchronized-telemetry-receipt.v2.schema.json"
        },
    )
    contract_version: Literal["mineru.synchronized-telemetry-receipt.v2"] = (
        TELEMETRY_RECEIPT_V2_VERSION
    )
    run_id: str
    runtime_bundle_identity_sha256: str
    process_profile: ProcessProfileLifecycle
    observer_source_sha256: str
    clock_domain_identity_sha256: str
    started_at_utc: datetime
    finished_at_utc: datetime
    started_monotonic_ns: int = Field(ge=0)
    finished_monotonic_ns: int = Field(ge=0)
    status: RunStatus
    lane_quality: tuple[LaneQualitySummary, LaneQualitySummary]
    termination_reason: ObserverTerminationReason
    observed_clock_divergence_ns: int = Field(ge=0)
    epoch_changed: bool
    safety_drift_reasons: tuple[SafetyDriftReason, ...]
    unsupported_observation_count: int = Field(ge=0)
    artifacts: TelemetryArtifactsV2
    activation_authorized: Literal[False] = False
    maximum_clock_divergence_fixed_ns: Literal[50_000_000] = 50_000_000
    maximum_clock_divergence_ppm: Literal[50] = 50

    @model_validator(mode="after")
    def _check_receipt(self) -> "SynchronizedTelemetryReceiptV2":
        _run_id(self.run_id)
        _utc(self.started_at_utc, label="started_at_utc")
        _utc(self.finished_at_utc, label="finished_at_utc")
        for label, value in (
            ("runtime_bundle_identity_sha256", self.runtime_bundle_identity_sha256),
            ("observer_source_sha256", self.observer_source_sha256),
            ("clock_domain_identity_sha256", self.clock_domain_identity_sha256),
        ):
            _sha256(value, label=label)
        if self.finished_at_utc <= self.started_at_utc or self.finished_monotonic_ns <= self.started_monotonic_ns:
            raise ValueError("telemetry receipt interval is invalid")
        elapsed = self.finished_monotonic_ns - self.started_monotonic_ns
        wall = int((self.finished_at_utc - self.started_at_utc).total_seconds() * 1_000_000_000)
        divergence = abs(wall - elapsed)
        if divergence != self.observed_clock_divergence_ns:
            raise ValueError("recorded clock divergence disagrees with clocks")
        if divergence > self.maximum_clock_divergence_fixed_ns + elapsed * self.maximum_clock_divergence_ppm // 1_000_000:
            raise ValueError("wall and monotonic receipt clocks diverged")
        if {item.lane for item in self.lane_quality} != {"gpu_fast", "host_slow"}:
            raise ValueError("receipt requires one quality summary per lane")
        if len(set(self.safety_drift_reasons)) != len(self.safety_drift_reasons):
            raise ValueError("safety drift reasons are duplicated")
        if self.epoch_changed != ("epoch_drift" in self.safety_drift_reasons):
            raise ValueError("epoch_changed must describe only epoch drift")
        if bool(self.safety_drift_reasons) != (self.termination_reason == "identity_drift"):
            raise ValueError("safety drift and termination reason disagree")
        unsafe = bool(self.safety_drift_reasons)
        incomplete = self.termination_reason != "duration_elapsed" or self.unsupported_observation_count > 0 or any(
            item.late_sample_count > 0 or item.missed_deadline_count > 0 or item.supported_frame_count == 0
            for item in self.lane_quality
        )
        expected: RunStatus = "unsafe" if unsafe else "incomplete" if incomplete else "complete"
        if self.status != expected:
            raise ValueError("receipt status disagrees with quality evidence")
        if self.process_profile.runtime_bundle_identity_sha256 != self.runtime_bundle_identity_sha256:
            raise ValueError("process profile runtime identity drifted")
        if self.process_profile.clock_domain_identity_sha256 != self.clock_domain_identity_sha256:
            raise ValueError("process profile clock domain drifted")
        return self


class SynchronizedTelemetrySealV2(_FrozenModel):
    """Non-self-referential attestation created after mandatory receipt replay."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_extra={
            "$id": _SCHEMA_ROOT + "synchronized-telemetry-seal.v2.schema.json"
        },
    )
    contract_version: Literal["mineru.synchronized-telemetry-seal.v2"] = TELEMETRY_SEAL_V2_VERSION
    run_id: str
    receipt_sha256: str
    frames_jsonl_sha256: str
    preseal_observer_process_cpu_started_ns: int = Field(ge=0)
    preseal_observer_process_cpu_finished_ns: int = Field(ge=0)
    preseal_observer_cpu_ns: int = Field(ge=0)
    sampling_elapsed_ns_denominator: int = Field(gt=0)
    maximum_observer_overhead_ratio: float = Field(default=0.02, ge=0.02, le=0.02)
    receipt_status: RunStatus
    status: RunStatus

    @model_validator(mode="after")
    def _check_seal(self) -> "SynchronizedTelemetrySealV2":
        _run_id(self.run_id)
        _sha256(self.receipt_sha256, label="receipt_sha256")
        _sha256(self.frames_jsonl_sha256, label="frames_jsonl_sha256")
        if self.preseal_observer_process_cpu_finished_ns < self.preseal_observer_process_cpu_started_ns:
            raise ValueError("observer CPU interval is invalid")
        if self.preseal_observer_cpu_ns != self.preseal_observer_process_cpu_finished_ns - self.preseal_observer_process_cpu_started_ns:
            raise ValueError("observer CPU duration disagrees with counters")
        overhead_unsafe = self.preseal_observer_cpu_ns / self.sampling_elapsed_ns_denominator > self.maximum_observer_overhead_ratio
        expected: RunStatus = "unsafe" if overhead_unsafe or self.receipt_status == "unsafe" else self.receipt_status
        if self.status != expected:
            raise ValueError("seal status disagrees with observer overhead")
        return self


class SynchronizedPhaseSummary(_FrozenModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_extra={
            "$id": _SCHEMA_ROOT + "synchronized-phase-summary.v1.schema.json"
        },
    )

    contract_version: Literal["mineru.synchronized-phase-summary.v1"] = (
        PHASE_SUMMARY_VERSION
    )
    runtime_bundle_identity_sha256: str
    process_profile_sha256: str
    clock_domain_identity_sha256: str
    phase_capture_sha256: str
    telemetry_receipt_sha256: str
    phase_started_monotonic_ns: int = Field(ge=0)
    phase_finished_monotonic_ns: int = Field(ge=0)
    telemetry_started_monotonic_ns: int = Field(ge=0)
    telemetry_finished_monotonic_ns: int = Field(ge=0)
    gpu_fast_samples_in_phase: int = Field(ge=1)
    host_slow_samples_in_phase: int = Field(ge=1)
    blocked_duration_ns: int = Field(ge=0)
    unique_durable_source_pages_committed: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_summary(self) -> "SynchronizedPhaseSummary":
        for label, value in (
            ("runtime_bundle_identity_sha256", self.runtime_bundle_identity_sha256),
            ("process_profile_sha256", self.process_profile_sha256),
            ("clock_domain_identity_sha256", self.clock_domain_identity_sha256),
            ("phase_capture_sha256", self.phase_capture_sha256),
            ("telemetry_receipt_sha256", self.telemetry_receipt_sha256),
        ):
            _sha256(value, label=label)
        if not (
            self.telemetry_started_monotonic_ns
            <= self.phase_started_monotonic_ns
            < self.phase_finished_monotonic_ns
            <= self.telemetry_finished_monotonic_ns
        ):
            raise ValueError("phase interval is not covered by telemetry")
        return self


class PhaseClockBinding(_FrozenModel):
    """Closed observer attestation that both producers use one kernel clock."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        json_schema_extra={"$id": _SCHEMA_ROOT + "phase-clock-binding.v1.schema.json"},
    )

    contract_version: Literal["mineru.phase-clock-binding.v1"] = (
        PHASE_CLOCK_BINDING_VERSION
    )
    phase_process_epoch: str
    runtime_bundle_identity_sha256: str
    container_id: str
    container_started_at_utc: datetime
    phase_node_identity_sha256: str
    observer_node_identity_sha256: str
    phase_boot_identity_sha256: str
    observer_boot_identity_sha256: str
    phase_clock_source: Literal["linux.clock_gettime.CLOCK_MONOTONIC"]
    observer_process_epoch_sha256: str
    clock_domain_identity_sha256: str
    observer_clock_source: Literal["linux.clock_gettime.CLOCK_MONOTONIC"]
    attestor_source_sha256: str

    @model_validator(mode="after")
    def _check_binding(self) -> "PhaseClockBinding":
        if re.fullmatch(r"[a-f0-9]{32}", self.phase_process_epoch) is None:
            raise ValueError("phase process epoch is invalid")
        if re.fullmatch(r"[a-f0-9]{64}", self.container_id) is None:
            raise ValueError("phase container identity is invalid")
        _utc(self.container_started_at_utc, label="container_started_at_utc")
        for label, value in (
            ("runtime_bundle_identity_sha256", self.runtime_bundle_identity_sha256),
            ("phase_node_identity_sha256", self.phase_node_identity_sha256),
            ("observer_node_identity_sha256", self.observer_node_identity_sha256),
            ("phase_boot_identity_sha256", self.phase_boot_identity_sha256),
            ("observer_boot_identity_sha256", self.observer_boot_identity_sha256),
            ("observer_process_epoch_sha256", self.observer_process_epoch_sha256),
            ("clock_domain_identity_sha256", self.clock_domain_identity_sha256),
            ("attestor_source_sha256", self.attestor_source_sha256),
        ):
            _sha256(value, label=label)
        if self.phase_clock_source != self.observer_clock_source:
            raise ValueError("phase and observer clock sources differ")
        if (
            self.phase_node_identity_sha256 != self.observer_node_identity_sha256
            or self.phase_boot_identity_sha256
            != self.observer_boot_identity_sha256
        ):
            raise ValueError("phase and observer kernel clock domains differ")
        return self


OPERATIONAL_TELEMETRY_SCHEMAS: dict[str, type[BaseModel]] = {
    "capacity-progress-event.v1.schema.json": CapacityProgressEventEnvelope,
    "capacity-vector-credit-event.v1.schema.json": CapacityVectorCreditEvent,
    "synchronized-telemetry-frame.v1.schema.json": SynchronizedTelemetryFrame,
    "synchronized-telemetry-receipt.v1.schema.json": SynchronizedTelemetryReceipt,
    "synchronized-telemetry-frame.v2.schema.json": SynchronizedTelemetryFrameV2,
    "synchronized-telemetry-receipt.v2.schema.json": SynchronizedTelemetryReceiptV2,
    "synchronized-telemetry-seal.v2.schema.json": SynchronizedTelemetrySealV2,
    "synchronized-phase-summary.v1.schema.json": SynchronizedPhaseSummary,
    "phase-clock-binding.v1.schema.json": PhaseClockBinding,
}


def operational_telemetry_schema_documents() -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for filename, model in OPERATIONAL_TELEMETRY_SCHEMAS.items():
        document = model.model_json_schema()
        document["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        documents[filename] = document
    return documents


def operational_schema_documents() -> dict[str, dict[str, Any]]:
    """Return the complete operational schema registry for public export.

    Capacity observation schemas predate synchronized telemetry, but both are
    one tracked ``contracts/operational`` namespace.  Merge them here so every
    exporter has one exhaustive registry and duplicate filenames fail closed.
    """

    from disclosure_anchor.application.contracts.capacity import (
        operational_schema_documents as capacity_schema_documents,
    )

    documents = capacity_schema_documents()
    telemetry = operational_telemetry_schema_documents()
    duplicates = set(documents) & set(telemetry)
    if duplicates:
        raise ValueError(
            "operational schema filenames overlap: " + ", ".join(sorted(duplicates))
        )
    return {**documents, **telemetry}


def canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def parse_canonical_json_artifact(
    payload: bytes, *, label: str, maximum_bytes: int = 268_435_456
) -> object:
    """Parse exact canonical JSON bytes, rejecting duplicates and alternate encodings."""

    if not isinstance(payload, bytes) or not payload or len(payload) > maximum_bytes:
        raise ValueError(f"{label} artifact bytes are invalid")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} artifact contains a duplicate field")
            result[key] = value
        return result

    def reject_nonfinite_constant(value: str) -> object:
        raise ValueError(f"{label} artifact contains non-finite JSON number: {value}")

    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_nonfinite_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} artifact is not canonical UTF-8 JSON") from exc
    canonical = json.dumps(
        decoded,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if payload != canonical:
        raise ValueError(f"{label} artifact bytes are not canonical")
    return decoded


def parse_canonical_jsonl_artifact(
    payload: bytes,
    *,
    label: str,
    maximum_bytes: int = 268_435_456,
    maximum_record_bytes: int = 262_144,
    maximum_records: int = 1_000_000,
) -> tuple[object, ...]:
    """Parse bounded newline-terminated canonical JSON records.

    JSONL is the durable streaming representation for synchronized telemetry
    frames.  Each record independently retains the duplicate-key, finite-number,
    UTF-8 and canonical-byte guarantees of :func:`parse_canonical_json_artifact`.
    """

    if not isinstance(payload, bytes) or not payload or len(payload) > maximum_bytes:
        raise ValueError(f"{label} JSONL artifact bytes are invalid")
    if not payload.endswith(b"\n"):
        raise ValueError(f"{label} JSONL artifact is not newline terminated")
    if b"\r" in payload:
        raise ValueError(f"{label} JSONL artifact must use LF only")
    lines = payload[:-1].split(b"\n")
    if not lines or len(lines) > maximum_records:
        raise ValueError(f"{label} JSONL record count is invalid")
    records: list[object] = []
    for line in lines:
        if not line or len(line) > maximum_record_bytes:
            raise ValueError(f"{label} JSONL record bytes are invalid")
        records.append(
            parse_canonical_json_artifact(
                line,
                label=f"{label} record",
                maximum_bytes=maximum_record_bytes,
            )
        )
    return tuple(records)


def canonical_jsonl_artifact_sha256(payload: bytes, *, label: str) -> str:
    """Hash only bytes proven to be bounded canonical JSONL."""

    parse_canonical_jsonl_artifact(payload, label=label)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_json_artifact_sha256(payload: bytes, *, label: str) -> str:
    """Hash only bytes proven to be the canonical encoding of one JSON value."""

    parse_canonical_json_artifact(payload, label=label)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def validate_telemetry_artifact_hashes(
    receipt: SynchronizedTelemetryReceipt,
    *,
    frames_artifact: bytes,
    progress_events_artifact: bytes | None,
    vector_events_artifact: bytes | None,
    phase_capture_artifact: bytes | None = None,
    phase_clock_binding_artifact: bytes | None = None,
) -> None:
    """Reconcile every receipt digest with its exact canonical artifact bytes."""

    supplied = {
        "frames_sha256": frames_artifact,
        "progress_events_sha256": progress_events_artifact,
        "vector_events_sha256": vector_events_artifact,
        "phase_capture_sha256": phase_capture_artifact,
        "phase_clock_binding_sha256": phase_clock_binding_artifact,
    }
    for field, payload in supplied.items():
        recorded = getattr(receipt.artifacts, field)
        if recorded is None:
            if payload is not None:
                raise ValueError(f"unexpected {field} artifact bytes")
            continue
        if payload is None:
            raise ValueError(f"missing {field} artifact bytes")
        observed = canonical_json_artifact_sha256(
            payload, label=field.removesuffix("_sha256")
        )
        if observed != recorded:
            raise ValueError(f"{field} artifact hash drifted")


def validate_frame_sequence(
    frames: tuple[SynchronizedTelemetryFrame, ...],
    *,
    receipt: SynchronizedTelemetryReceipt,
) -> None:
    if not frames:
        raise ValueError("telemetry frame sequence is empty")
    for sequence, frame in enumerate(frames):
        if frame.sequence != sequence or frame.run_id != receipt.run_id:
            raise ValueError("telemetry frame sequence or run identity drifted")
        if (
            frame.runtime_bundle_identity_sha256
            != receipt.runtime_bundle_identity_sha256
            or frame.process_profile_sha256
            != receipt.process_profile.process_profile_sha256
            or frame.observer_source_sha256 != receipt.observer_source_sha256
            or frame.clock.clock_domain_identity_sha256
            != receipt.clock_domain_identity_sha256
        ):
            raise ValueError("telemetry frame identity drifted")
        if not (
            receipt.started_monotonic_ns
            <= frame.clock.started_monotonic_ns
            <= frame.clock.finished_monotonic_ns
            <= receipt.finished_monotonic_ns
        ):
            raise ValueError("telemetry frame lies outside receipt bounds")
        expected_wall = receipt.started_at_utc + timedelta(
            microseconds=(
                frame.clock.started_monotonic_ns - receipt.started_monotonic_ns
            )
            / 1000
        )
        wall_error_ns = abs(
            int((frame.clock.observed_at_utc - expected_wall).total_seconds() * 1e9)
        )
        elapsed_ns = frame.clock.started_monotonic_ns - receipt.started_monotonic_ns
        allowed_wall_error_ns = (
            receipt.maximum_clock_divergence_fixed_ns
            + elapsed_ns * receipt.maximum_clock_divergence_ppm // 1_000_000
        )
        if wall_error_ns > allowed_wall_error_ns:
            raise ValueError("telemetry frame wall and monotonic clocks diverged")
    expected_quality, unsupported_observation_count = derive_frame_evidence(
        frames,
        started_monotonic_ns=receipt.started_monotonic_ns,
        finished_monotonic_ns=receipt.finished_monotonic_ns,
    )
    receipt_quality = {item.lane: item for item in receipt.lane_quality}
    for expected in expected_quality:
        lane = expected.lane
        if receipt_quality[lane] != expected:
            raise ValueError(f"telemetry {lane} lane quality receipt drifted")
    if receipt.unsupported_observation_count != unsupported_observation_count:
        raise ValueError("telemetry unsupported observation count drifted")


def derive_frame_evidence(
    frames: tuple[SynchronizedTelemetryFrame, ...],
    *,
    started_monotonic_ns: int,
    finished_monotonic_ns: int,
) -> tuple[tuple[LaneQualitySummary, LaneQualitySummary], int]:
    """Mechanically derive receipt lane quality and required missing evidence."""

    if not frames:
        raise ValueError("telemetry frame sequence is empty")
    if finished_monotonic_ns <= started_monotonic_ns:
        raise ValueError("telemetry evidence interval is invalid")
    lane_previous: dict[TelemetryLane, SynchronizedTelemetryFrame] = {}
    lane_frames: dict[TelemetryLane, list[SynchronizedTelemetryFrame]] = {
        "gpu_fast": [],
        "host_slow": [],
    }
    previous_started = -1
    for sequence, frame in enumerate(frames):
        if frame.sequence != sequence:
            raise ValueError("telemetry frame sequence is not contiguous")
        if not (
            started_monotonic_ns
            <= frame.clock.started_monotonic_ns
            <= frame.clock.finished_monotonic_ns
            <= finished_monotonic_ns
        ):
            raise ValueError("telemetry frame lies outside evidence bounds")
        if frame.clock.started_monotonic_ns < previous_started:
            raise ValueError("telemetry frames are not monotonic")
        previous = lane_previous.get(frame.lane)
        if previous is None:
            if frame.quality.status != "first":
                raise ValueError("first lane frame is not marked first")
        else:
            if (
                frame.quality.nominal_interval_ms
                != previous.quality.nominal_interval_ms
            ):
                raise ValueError("telemetry lane cadence drifted")
            observed_ns = (
                frame.clock.started_monotonic_ns
                - previous.clock.started_monotonic_ns
            )
            expected_observed_ms = observed_ns / 1_000_000
            if frame.quality.observed_interval_ms is None or not math.isclose(
                frame.quality.observed_interval_ms,
                expected_observed_ms,
                abs_tol=0.001,
            ):
                raise ValueError("observed lane interval differs from clock")
            nominal_ns = frame.quality.nominal_interval_ms * 1_000_000
            scheduled_ns = (
                frame.clock.scheduled_monotonic_ns
                - previous.clock.scheduled_monotonic_ns
            )
            if scheduled_ns < nominal_ns or scheduled_ns % nominal_ns:
                raise ValueError("telemetry lane absolute schedule drifted")
            expected_missed = scheduled_ns // nominal_ns - 1
            expected_status = "late" if expected_missed else "on_time"
            if (
                frame.quality.missed_deadlines != expected_missed
                or frame.quality.status != expected_status
            ):
                raise ValueError("telemetry lane deadline evidence differs from clock")
        lane_previous[frame.lane] = frame
        lane_frames[frame.lane].append(frame)
        previous_started = frame.clock.started_monotonic_ns

    unsupported_observation_count = 0
    summaries: list[LaneQualitySummary] = []
    for lane in ("gpu_fast", "host_slow"):
        frames_in_lane = lane_frames[lane]
        if not frames_in_lane:
            raise ValueError(f"telemetry {lane} lane is empty")
        required_statuses = (
            [(frame.gpu.status,) for frame in frames_in_lane]
            if lane == "gpu_fast"
            else [
                (
                    frame.api_process.status,
                    frame.host_cgroup.status,
                    frame.queue_vllm.status,
                )
                for frame in frames_in_lane
            ]
        )
        unsupported_observation_count += sum(
            status == "unsupported"
            for statuses in required_statuses
            for status in statuses
        )
        supported_frame_count = sum(
            all(status == "supported" for status in statuses)
            for statuses in required_statuses
        )
        starts = [frame.clock.started_monotonic_ns for frame in frames_in_lane]
        gaps_ns = [starts[0] - started_monotonic_ns]
        gaps_ns.extend(right - left for left, right in zip(starts, starts[1:]))
        gaps_ns.append(finished_monotonic_ns - starts[-1])
        scheduled = [
            frame.clock.scheduled_monotonic_ns for frame in frames_in_lane
        ]
        schedule_gaps_ns = [scheduled[0] - started_monotonic_ns]
        schedule_gaps_ns.extend(
            right - left for left, right in zip(scheduled, scheduled[1:])
        )
        schedule_gaps_ns.append(finished_monotonic_ns - scheduled[-1])
        nominal_ns = frames_in_lane[0].quality.nominal_interval_ms * 1_000_000
        summaries.append(
            LaneQualitySummary(
                lane=lane,
                nominal_interval_ms=frames_in_lane[0].quality.nominal_interval_ms,
                sample_count=len(frames_in_lane),
                maximum_gap_ms=max(gaps_ns) / 1_000_000,
                late_sample_count=sum(
                    frame.quality.status == "late" for frame in frames_in_lane
                ),
                missed_deadline_count=sum(
                    max(0, (gap_ns - 1) // nominal_ns)
                    for gap_ns in schedule_gaps_ns
                ),
                supported_frame_count=supported_frame_count,
            )
        )
    return (summaries[0], summaries[1]), unsupported_observation_count


__all__ = [
    "ApiProcessObservation",
    "ApiProcessTelemetryValues",
    "BlockedProgressEvent",
    "CapacityVector",
    "CapacityVectorCreditEvent",
    "CapacityProgressEventEnvelope",
    "CapacityVectorSnapshot",
    "CgroupCpuStat",
    "CgroupMemoryEvents",
    "CgroupMemoryStat",
    "DocumentProfileLifecycle",
    "DurablePageCommitEvent",
    "GpuObservation",
    "GpuTelemetryValues",
    "HostCgroupObservation",
    "HostCgroupTelemetryValues",
    "LaneQualitySummary",
    "MeasuredSafetyMargin",
    "ObserverTerminationReason",
    "PhaseClockBinding",
    "PressureLine",
    "PressureSample",
    "ProcessProfileLifecycle",
    "ProcessProfileParameters",
    "ProgressEvent",
    "QueueVllmObservation",
    "QueueVllmTelemetryValues",
    "SampleClock",
    "SampleQuality",
    "SynchronizedPhaseSummary",
    "SynchronizedTelemetryFrame",
    "SynchronizedTelemetryFrameV2",
    "SynchronizedTelemetryReceipt",
    "SynchronizedTelemetryReceiptV2",
    "SynchronizedTelemetrySealV2",
    "TelemetryArtifacts",
    "TelemetryArtifactsV2",
    "SafetyDriftReason",
    "UnsupportedReasonV2",
    "canonical_jsonl_artifact_sha256",
    "canonical_json_artifact_sha256",
    "canonical_json_sha256",
    "derive_frame_evidence",
    "operational_telemetry_schema_documents",
    "operational_schema_documents",
    "parse_canonical_json_artifact",
    "parse_canonical_jsonl_artifact",
    "validate_credit_event_chain",
    "validate_frame_sequence",
    "validate_telemetry_artifact_hashes",
]
