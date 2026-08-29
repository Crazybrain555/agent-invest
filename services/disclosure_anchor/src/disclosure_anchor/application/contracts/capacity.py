"""Closed, content-free contracts for passive MinerU capacity observation."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import math
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


CAPACITY_RAW_SAMPLE_VERSION = "capacity_observation.raw_sample.v1"
CAPACITY_INTERVAL_VERSION = "capacity_observation.interval.v1"
CAPACITY_RUN_VERSION = "capacity_observation.run.v1"
CAPACITY_INTERVAL_SCHEMA_ID = (
    "https://agent-invest.local/contracts/operational/"
    "capacity-observation-interval.v1.schema.json"
)
CAPACITY_RUN_SCHEMA_ID = (
    "https://agent-invest.local/contracts/operational/"
    "capacity-observation-run.v1.schema.json"
)

_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_RUN_ID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"
)

CapacitySource = Literal["api", "gpu", "host", "vllm"]
ObservationStatus = Literal["available", "unavailable"]
IntervalStatus = Literal["complete", "incomplete", "unsafe"]
SamplerFailureReason = Literal[
    "contract_unsatisfied",
    "endpoint_unreachable",
    "host_sample_failed",
    "not_configured",
    "unexpected_sampler_failure",
]
UnavailableReason = SamplerFailureReason | Literal["sample_completed_after_deadline"]
CoverageReason = Literal[
    "boundary_sample_unavailable",
    "coverage_below_threshold",
    "max_gap_exceeded",
    "no_samples",
]
SafetyViolation = Literal[
    "api_identity_drift",
    "host_cgroup_oom_observed",
    "host_container_epoch_changed",
    "host_container_state_unsafe",
    "host_counter_reset",
    "host_memory_reserve_crossed",
    "vllm_counter_reset",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ApiSampleValues(_FrozenModel):
    kind: Literal["api"] = "api"
    queued_tasks: int = Field(ge=0)
    processing_tasks: int = Field(ge=0)
    completed_tasks_gauge: int = Field(ge=0)
    failed_tasks_gauge: int = Field(ge=0)
    task_slots: int = Field(ge=1)
    max_pending_tasks_requested: int = Field(ge=1)
    max_pending_tasks_effective: int = Field(ge=1)
    processing_window_size: int = Field(ge=1)
    task_retention_seconds: int = Field(ge=1)
    task_cleanup_interval_seconds: int = Field(ge=1)
    protocol_version: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_declared_limits(self) -> "ApiSampleValues":
        if self.processing_tasks > self.task_slots:
            raise ValueError("processing_tasks exceeds task_slots")
        if (
            self.max_pending_tasks_effective < self.task_slots
            or self.max_pending_tasks_effective < self.max_pending_tasks_requested
        ):
            raise ValueError("effective pending capacity is below its declared limits")
        if (
            self.queued_tasks + self.processing_tasks
            > self.max_pending_tasks_effective
        ):
            raise ValueError("API population exceeds effective pending capacity")
        if self.queued_tasks + self.processing_tasks > self.processing_window_size:
            raise ValueError("API population exceeds processing window")
        return self


class VllmSampleValues(_FrozenModel):
    kind: Literal["vllm"] = "vllm"
    requests_running: int = Field(ge=0)
    requests_waiting: int = Field(ge=0)
    preemptions_total: int | None = Field(default=None, ge=0)
    kv_cache_usage_ratio: float | None = Field(default=None, ge=0, le=1)


class GpuSampleValues(_FrozenModel):
    kind: Literal["gpu"] = "gpu"
    exporter_family: Literal["nvidia_smi"]
    device_count: int = Field(ge=1)
    device_identity_sha256: str
    gpu_utilization_pct: float = Field(ge=0, le=100)
    framebuffer_used_bytes: int | None = Field(default=None, ge=0)
    framebuffer_free_bytes: int | None = Field(default=None, ge=0)
    framebuffer_total_bytes: int | None = Field(default=None, ge=1)
    power_usage_watts: float | None = Field(default=None, ge=0, le=1000)
    temperature_celsius: float | None = Field(default=None, ge=-50, le=150)

    @model_validator(mode="after")
    def _check_gpu_identity_and_memory(self) -> "GpuSampleValues":
        _sha256(self.device_identity_sha256, label="device_identity_sha256")
        if self.framebuffer_total_bytes is not None:
            if (
                self.framebuffer_used_bytes is None
                or self.framebuffer_free_bytes is None
                or self.framebuffer_used_bytes > self.framebuffer_total_bytes
                or self.framebuffer_free_bytes > self.framebuffer_total_bytes
            ):
                raise ValueError("GPU memory measurements are incomplete")
        return self


class HostSampleValues(_FrozenModel):
    kind: Literal["host"] = "host"
    collector_sha256: str
    windows_node_identity_sha256: str
    container_epoch_sha256: str
    container_count: int = Field(ge=1)
    restart_count_total: int = Field(ge=0)
    oom_killed_count: int = Field(ge=0)
    unsafe_container_count: int = Field(ge=0)
    cgroup_oom_total: int = Field(ge=0)
    cgroup_oom_kill_total: int = Field(ge=0)
    cgroup_high_total: int = Field(ge=0)
    docker_vm_memory_total_bytes: int = Field(ge=1)
    docker_vm_memory_available_bytes: int = Field(ge=0)
    docker_memory_reserve_bytes: int = Field(ge=1)
    api_pid1_rss_bytes: int = Field(ge=0)
    api_pid1_rss_hwm_bytes: int = Field(ge=0)
    safety_violation_codes: tuple[
        Literal[
            "cgroup_oom_observed",
            "container_state_unsafe",
            "memory_reserve_crossed",
        ],
        ...,
    ] = ()

    @model_validator(mode="after")
    def _check_host_identity_and_memory(self) -> "HostSampleValues":
        for label, value in (
            ("collector_sha256", self.collector_sha256),
            ("windows_node_identity_sha256", self.windows_node_identity_sha256),
            ("container_epoch_sha256", self.container_epoch_sha256),
        ):
            _sha256(value, label=label)
        if self.docker_vm_memory_available_bytes > self.docker_vm_memory_total_bytes:
            raise ValueError("Docker available memory exceeds total memory")
        if self.api_pid1_rss_hwm_bytes < self.api_pid1_rss_bytes:
            raise ValueError("API RSS high-water mark is below current RSS")
        if self.unsafe_container_count > self.container_count:
            raise ValueError("unsafe container count exceeds container count")
        if tuple(sorted(set(self.safety_violation_codes))) != self.safety_violation_codes:
            raise ValueError("host safety violation codes must be sorted and unique")
        return self


RawSampleValues = Annotated[
    Union[ApiSampleValues, GpuSampleValues, HostSampleValues, VllmSampleValues],
    Field(discriminator="kind"),
]


class CapacityRawSample(_FrozenModel):
    contract_version: Literal["capacity_observation.raw_sample.v1"] = (
        "capacity_observation.raw_sample.v1"
    )
    run_id: str
    sequence: int = Field(ge=0)
    previous_record_sha256: str | None
    record_sha256: str
    runtime_bundle_identity_sha256: str
    observer_source_sha256: str
    observed_at_utc: datetime
    monotonic_offset_seconds: float = Field(ge=0)
    sample_duration_seconds: float = Field(ge=0)
    source: CapacitySource
    status: ObservationStatus
    values: RawSampleValues | None
    reason_code: UnavailableReason | None
    underlying_reason_code: SamplerFailureReason | None = None

    @model_validator(mode="after")
    def _check_closed_state(self) -> "CapacityRawSample":
        _run_id(self.run_id)
        _optional_previous_hash(self.previous_record_sha256, sequence=self.sequence)
        _sha256(self.record_sha256, label="record_sha256")
        _sha256(
            self.runtime_bundle_identity_sha256,
            label="runtime_bundle_identity_sha256",
        )
        _sha256(self.observer_source_sha256, label="observer_source_sha256")
        _aware(self.observed_at_utc, label="observed_at_utc")
        if self.status == "available":
            if (
                self.values is None
                or self.reason_code is not None
                or self.underlying_reason_code is not None
            ):
                raise ValueError("available samples require values and no reason")
            if self.values.kind != self.source:
                raise ValueError("sample values do not match source")
        elif self.values is not None or self.reason_code is None:
            raise ValueError("unavailable samples require one closed reason")
        elif (
            self.underlying_reason_code is not None
            and self.reason_code != "sample_completed_after_deadline"
        ):
            raise ValueError("underlying reason requires a deadline reason")
        return self


class CoverageEvidence(_FrozenModel):
    status: Literal["complete", "incomplete"]
    valid_samples: int = Field(ge=0)
    expected_samples: int = Field(ge=1)
    covered_seconds: float = Field(ge=0)
    coverage_ratio: float = Field(ge=0, le=1)
    required_ratio: float = Field(gt=0, le=1)
    max_gap_seconds: float = Field(ge=0)
    allowed_max_gap_seconds: float = Field(gt=0)
    reason_code: CoverageReason | None

    @model_validator(mode="after")
    def _check_coverage_state(self) -> "CoverageEvidence":
        if self.status == "complete" and self.reason_code is not None:
            raise ValueError("complete coverage cannot carry a reason")
        if self.status == "incomplete" and self.reason_code is None:
            raise ValueError("incomplete coverage requires a reason")
        return self


class CapacityCoverageSet(_FrozenModel):
    api: CoverageEvidence
    gpu: CoverageEvidence
    host: CoverageEvidence
    vllm: CoverageEvidence


class MetricAggregate(_FrozenModel):
    current: float | None
    minimum: float | None
    maximum: float | None
    time_weighted_mean: float | None
    time_weighted_p50: float | None
    time_weighted_p95: float | None
    observed_seconds: float = Field(ge=0)


class ApiIntervalMetrics(_FrozenModel):
    queued_tasks: MetricAggregate
    processing_tasks: MetricAggregate
    completed_tasks_gauge: MetricAggregate
    failed_tasks_gauge: MetricAggregate
    processing_task_seconds: float = Field(ge=0)
    queued_task_seconds: float = Field(ge=0)
    idle_task_seconds: float = Field(ge=0)
    task_slots: int | None = Field(default=None, ge=1)


class VllmIntervalMetrics(_FrozenModel):
    requests_running: MetricAggregate
    requests_waiting: MetricAggregate
    kv_cache_usage_ratio: MetricAggregate
    preemptions_delta: int | None = Field(default=None, ge=0)
    counter_epoch_valid: bool


class GpuIntervalMetrics(_FrozenModel):
    gpu_utilization_pct: MetricAggregate
    framebuffer_used_bytes: MetricAggregate
    framebuffer_free_bytes: MetricAggregate
    power_usage_watts: MetricAggregate
    temperature_celsius: MetricAggregate


class HostIntervalMetrics(_FrozenModel):
    docker_vm_memory_available_bytes: MetricAggregate
    api_pid1_rss_bytes: MetricAggregate
    api_pid1_rss_hwm_bytes: MetricAggregate
    memory_reserve_crossed_seconds: float = Field(ge=0)
    restart_delta: int | None = Field(default=None, ge=0)
    cgroup_oom_delta: int | None = Field(default=None, ge=0)
    cgroup_oom_kill_delta: int | None = Field(default=None, ge=0)
    cgroup_high_delta: int | None = Field(default=None, ge=0)
    counter_epoch_valid: bool
    container_epoch_changed: bool


class CapacityObservationInterval(_FrozenModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"$id": CAPACITY_INTERVAL_SCHEMA_ID},
    )

    contract_version: Literal["capacity_observation.interval.v1"] = (
        "capacity_observation.interval.v1"
    )
    run_id: str
    interval_index: int = Field(ge=0)
    previous_record_sha256: str | None
    record_sha256: str
    runtime_bundle_identity_sha256: str
    observer_source_sha256: str
    observed_at_utc: datetime
    monotonic_start_seconds: float = Field(ge=0)
    monotonic_end_seconds: float = Field(gt=0)
    wall_seconds: float = Field(gt=0)
    status: IntervalStatus
    coverage: CapacityCoverageSet
    api: ApiIntervalMetrics
    gpu: GpuIntervalMetrics
    host: HostIntervalMetrics
    vllm: VllmIntervalMetrics
    safety_violations: tuple[SafetyViolation, ...]

    @model_validator(mode="after")
    def _check_interval(self) -> "CapacityObservationInterval":
        _run_id(self.run_id)
        _optional_previous_hash(
            self.previous_record_sha256,
            sequence=self.interval_index,
        )
        for label, value in (
            ("record_sha256", self.record_sha256),
            ("runtime_bundle_identity_sha256", self.runtime_bundle_identity_sha256),
            ("observer_source_sha256", self.observer_source_sha256),
        ):
            _sha256(value, label=label)
        _aware(self.observed_at_utc, label="observed_at_utc")
        if self.monotonic_end_seconds <= self.monotonic_start_seconds:
            raise ValueError("interval monotonic range is invalid")
        if abs(
            self.wall_seconds
            - (self.monotonic_end_seconds - self.monotonic_start_seconds)
        ) > 1e-6:
            raise ValueError("interval wall seconds differ from monotonic range")
        if tuple(sorted(set(self.safety_violations))) != self.safety_violations:
            raise ValueError("safety violations must be sorted and unique")
        coverage_complete = all(
            item.status == "complete"
            for item in (
                self.coverage.api,
                self.coverage.gpu,
                self.coverage.host,
                self.coverage.vllm,
            )
        )
        expected_status: IntervalStatus = (
            "unsafe"
            if self.safety_violations
            else "complete" if coverage_complete else "incomplete"
        )
        if self.status != expected_status:
            raise ValueError("interval status disagrees with evidence")
        return self


class CapacitySourceCounts(_FrozenModel):
    api: int = Field(ge=0)
    gpu: int = Field(ge=0)
    host: int = Field(ge=0)
    vllm: int = Field(ge=0)


class CapacityArtifactDigests(_FrozenModel):
    raw_samples_sha256: str
    intervals_sha256: str
    raw_chain_head_sha256: str | None
    interval_chain_head_sha256: str | None

    @model_validator(mode="after")
    def _check_hashes(self) -> "CapacityArtifactDigests":
        for label, value in (
            ("raw_samples_sha256", self.raw_samples_sha256),
            ("intervals_sha256", self.intervals_sha256),
        ):
            _sha256(value, label=label)
        for label, optional_value in (
            ("raw_chain_head_sha256", self.raw_chain_head_sha256),
            ("interval_chain_head_sha256", self.interval_chain_head_sha256),
        ):
            if optional_value is not None:
                _sha256(optional_value, label=label)
        return self


class CapacityObservationRun(_FrozenModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"$id": CAPACITY_RUN_SCHEMA_ID},
    )

    contract_version: Literal["capacity_observation.run.v1"] = (
        "capacity_observation.run.v1"
    )
    run_id: str
    runtime_bundle_identity_sha256: str
    observer_source_sha256: str
    started_at_utc: datetime
    finished_at_utc: datetime
    duration_seconds: float = Field(gt=0)
    interval_seconds: float = Field(gt=0)
    status: IntervalStatus
    raw_sample_count: int = Field(ge=0)
    interval_count: int = Field(ge=1)
    complete_interval_count: int = Field(ge=0)
    incomplete_interval_count: int = Field(ge=0)
    unsafe_interval_count: int = Field(ge=0)
    source_sample_counts: CapacitySourceCounts
    safety_violations: tuple[SafetyViolation, ...]
    artifacts: CapacityArtifactDigests
    activation_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _check_run(self) -> "CapacityObservationRun":
        _run_id(self.run_id)
        _sha256(
            self.runtime_bundle_identity_sha256,
            label="runtime_bundle_identity_sha256",
        )
        _sha256(self.observer_source_sha256, label="observer_source_sha256")
        _aware(self.started_at_utc, label="started_at_utc")
        _aware(self.finished_at_utc, label="finished_at_utc")
        if self.finished_at_utc <= self.started_at_utc:
            raise ValueError("run timestamps are invalid")
        if abs(
            (self.finished_at_utc - self.started_at_utc).total_seconds()
            - self.duration_seconds
        ) > 1e-6:
            raise ValueError("run wall timestamps differ from duration")
        if (
            self.complete_interval_count
            + self.incomplete_interval_count
            + self.unsafe_interval_count
            != self.interval_count
        ):
            raise ValueError("run interval counts do not reconcile")
        ratio = self.duration_seconds / self.interval_seconds
        nearest = round(ratio)
        expected_interval_count = (
            max(1, int(nearest))
            if abs(ratio - nearest) <= 1e-9
            else max(1, math.ceil(ratio))
        )
        if self.interval_count != expected_interval_count:
            raise ValueError("run interval count differs from duration geometry")
        if tuple(sorted(set(self.safety_violations))) != self.safety_violations:
            raise ValueError("run safety violations must be sorted and unique")
        expected_status: IntervalStatus = (
            "unsafe"
            if self.unsafe_interval_count
            else "incomplete" if self.incomplete_interval_count else "complete"
        )
        if self.status != expected_status:
            raise ValueError("run status disagrees with interval evidence")
        return self


OPERATIONAL_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "capacity-observation-interval.v1.schema.json": CapacityObservationInterval,
    "capacity-observation-run.v1.schema.json": CapacityObservationRun,
}


def operational_schema_documents() -> dict[str, dict[str, Any]]:
    """Return the canonical tracked operational JSON Schema documents."""

    documents: dict[str, dict[str, Any]] = {}
    for filename, model in OPERATIONAL_SCHEMA_MODELS.items():
        document = model.model_json_schema()
        document["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        documents[filename] = document
    return documents


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def chained_record_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return one canonical record with its self-hash filled in."""

    if payload.get("record_sha256") not in {None, ""}:
        raise ValueError("record_sha256 must be empty before chaining")
    unhashed = dict(payload)
    unhashed.pop("record_sha256", None)
    digest = "sha256:" + hashlib.sha256(canonical_json_bytes(unhashed)).hexdigest()
    return {**unhashed, "record_sha256": digest}


def verify_chained_record(payload: dict[str, Any]) -> None:
    actual = payload.get("record_sha256")
    if not isinstance(actual, str):
        raise ValueError("record_sha256 is missing")
    unhashed = dict(payload)
    unhashed.pop("record_sha256")
    expected = "sha256:" + hashlib.sha256(canonical_json_bytes(unhashed)).hexdigest()
    if actual != expected:
        raise ValueError("record self-hash is invalid")


def _run_id(value: str) -> None:
    if not _RUN_ID_RE.fullmatch(value):
        raise ValueError("run_id is not a canonical UUID")


def _sha256(value: str, *, label: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} is not a canonical SHA-256")


def _optional_previous_hash(value: str | None, *, sequence: int) -> None:
    if sequence == 0:
        if value is not None:
            raise ValueError("first record must not have a previous hash")
    elif value is None:
        raise ValueError("non-first record requires a previous hash")
    else:
        _sha256(value, label="previous_record_sha256")


def _aware(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must use UTC offset +00:00")


__all__ = [
    "CAPACITY_INTERVAL_VERSION",
    "CAPACITY_RAW_SAMPLE_VERSION",
    "CAPACITY_RUN_VERSION",
    "ApiIntervalMetrics",
    "ApiSampleValues",
    "CapacityArtifactDigests",
    "CapacityCoverageSet",
    "CapacityObservationInterval",
    "CapacityObservationRun",
    "CapacityRawSample",
    "CapacitySourceCounts",
    "CoverageEvidence",
    "GpuIntervalMetrics",
    "GpuSampleValues",
    "HostIntervalMetrics",
    "HostSampleValues",
    "MetricAggregate",
    "OPERATIONAL_SCHEMA_MODELS",
    "VllmIntervalMetrics",
    "VllmSampleValues",
    "RawSampleValues",
    "SafetyViolation",
    "canonical_json_bytes",
    "chained_record_payload",
    "operational_schema_documents",
    "verify_chained_record",
]
