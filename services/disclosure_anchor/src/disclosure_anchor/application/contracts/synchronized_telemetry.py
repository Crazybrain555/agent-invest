"""Closed, content-free contracts for synchronized MinerU capacity telemetry."""

from __future__ import annotations

from datetime import datetime, timezone
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
PROGRESS_EVENT_VERSION: Literal["mineru.capacity-progress-event.v1"] = (
    "mineru.capacity-progress-event.v1"
)
VECTOR_CREDIT_VERSION: Literal["mineru.capacity-vector-credit-event.v1"] = (
    "mineru.capacity-vector-credit-event.v1"
)
PHASE_SUMMARY_VERSION: Literal["mineru.synchronized-phase-summary.v1"] = (
    "mineru.synchronized-phase-summary.v1"
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
RunStatus = Literal["complete", "incomplete", "unsafe"]
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
    blocked_duration_ns: int = Field(ge=0)


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

    @model_validator(mode="after")
    def _check_artifacts(self) -> "TelemetryArtifacts":
        _sha256(self.frames_sha256, label="frames_sha256")
        for label, value in (
            ("progress_events_sha256", self.progress_events_sha256),
            ("vector_events_sha256", self.vector_events_sha256),
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
            item.late_sample_count > 0 or item.supported_frame_count == 0
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
    """Attested mapping from one phase process epoch to one monotonic clock."""

    phase_process_epoch: str
    clock_domain_identity_sha256: str
    binding_artifact_sha256: str

    @model_validator(mode="after")
    def _check_binding(self) -> "PhaseClockBinding":
        if re.fullmatch(r"[a-f0-9]{32}", self.phase_process_epoch) is None:
            raise ValueError("phase process epoch is invalid")
        _sha256(
            self.clock_domain_identity_sha256,
            label="clock_domain_identity_sha256",
        )
        _sha256(self.binding_artifact_sha256, label="binding_artifact_sha256")
        return self


OPERATIONAL_TELEMETRY_SCHEMAS: dict[str, type[BaseModel]] = {
    "capacity-progress-event.v1.schema.json": CapacityProgressEventEnvelope,
    "capacity-vector-credit-event.v1.schema.json": CapacityVectorCreditEvent,
    "synchronized-telemetry-frame.v1.schema.json": SynchronizedTelemetryFrame,
    "synchronized-telemetry-receipt.v1.schema.json": SynchronizedTelemetryReceipt,
    "synchronized-phase-summary.v1.schema.json": SynchronizedPhaseSummary,
}


def operational_telemetry_schema_documents() -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for filename, model in OPERATIONAL_TELEMETRY_SCHEMAS.items():
        document = model.model_json_schema()
        document["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        documents[filename] = document
    return documents


def canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def validate_frame_sequence(
    frames: tuple[SynchronizedTelemetryFrame, ...],
    *,
    receipt: SynchronizedTelemetryReceipt,
) -> None:
    if not frames:
        raise ValueError("telemetry frame sequence is empty")
    previous_started = -1
    lane_previous: dict[TelemetryLane, SynchronizedTelemetryFrame] = {}
    lane_frames: dict[TelemetryLane, list[SynchronizedTelemetryFrame]] = {
        "gpu_fast": [],
        "host_slow": [],
    }
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
        if frame.clock.started_monotonic_ns < previous_started:
            raise ValueError("telemetry frames are not monotonic")
        previous = lane_previous.get(frame.lane)
        if previous is None:
            if frame.quality.status != "first":
                raise ValueError("first lane frame is not marked first")
        else:
            observed_ms = (
                frame.clock.started_monotonic_ns
                - previous.clock.started_monotonic_ns
            ) / 1_000_000
            if frame.quality.observed_interval_ms is None or not math.isclose(
                frame.quality.observed_interval_ms,
                observed_ms,
                abs_tol=0.001,
            ):
                raise ValueError("observed lane interval differs from clock")
        lane_previous[frame.lane] = frame
        lane_frames[frame.lane].append(frame)
        previous_started = frame.clock.started_monotonic_ns
    receipt_quality = {item.lane: item for item in receipt.lane_quality}
    unsupported_observation_count = 0
    for lane, frames_in_lane in lane_frames.items():
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
        gaps_ns = [starts[0] - receipt.started_monotonic_ns]
        gaps_ns.extend(right - left for left, right in zip(starts, starts[1:]))
        gaps_ns.append(receipt.finished_monotonic_ns - starts[-1])
        expected = LaneQualitySummary(
            lane=lane,
            nominal_interval_ms=frames_in_lane[0].quality.nominal_interval_ms,
            sample_count=len(frames_in_lane),
            maximum_gap_ms=max(gaps_ns) / 1_000_000,
            late_sample_count=sum(
                frame.quality.status == "late" for frame in frames_in_lane
            ),
            missed_deadline_count=sum(
                frame.quality.missed_deadlines for frame in frames_in_lane
            ),
            supported_frame_count=supported_frame_count,
        )
        if receipt_quality[lane] != expected:
            raise ValueError(f"telemetry {lane} lane quality receipt drifted")
    if receipt.unsupported_observation_count != unsupported_observation_count:
        raise ValueError("telemetry unsupported observation count drifted")


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
    "SynchronizedTelemetryReceipt",
    "TelemetryArtifacts",
    "canonical_json_sha256",
    "operational_telemetry_schema_documents",
    "validate_credit_event_chain",
    "validate_frame_sequence",
]
