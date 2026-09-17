"""Closed wire contract for the default-off Windows resident telemetry exporter.

The host lane forwards the serving API's raw health and HTTP-snapshot bytes plus
its own vLLM metric digest. The closed capacity rules are evaluated here, once,
by the shared capacity validator against the release's frozen
``MineruCapacityConfig``; the Windows side binds only the capacity identity at
the source and never mirrors those rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruCapacityConfig,
    decode_mineru_capacity_config,
)
from disclosure_anchor.application.contracts.mineru_capacity_health import (
    parse_mineru_capacity_wire_health,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    ApiProcessObservation,
    GpuObservation,
    HostCgroupObservation,
    ObservationStatus,
    QueueVllmObservation,
    QueueVllmTelemetryValues,
    UnsupportedReason,
    parse_canonical_json_artifact,
)


WIRE_VERSION: Literal["mineru.windows-resident-telemetry.v2"] = (
    "mineru.windows-resident-telemetry.v2"
)
PULL_VERSION: Literal["mineru.windows-resident-pull.v1"] = "mineru.windows-resident-pull.v1"
_NONCE = re.compile(r"[0-9a-f]{32}")
API_HTTP_SNAPSHOT_CONTRACT = "mineru.api-http-request-snapshot.v1"
_MAX_HEALTH_BYTES = 8192
_MAX_HTTP_BYTES = 1024
_UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ResidentIdentity(_Closed):
    exporter_source_sha256: str
    host_assignment_identity_sha256: str
    boot_identity_sha256: str
    runtime_bundle_identity_sha256: str
    process_profile_sha256: str
    clock_domain_identity_sha256: str
    exporter_process_epoch_sha256: str

    @model_validator(mode="after")
    def _canonical_hashes(self) -> "ResidentIdentity":
        for name, value in self:
            digest = value.removeprefix("sha256:")
            if (
                not value.startswith("sha256:")
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{name} is not a canonical SHA-256")
        return self


class VllmWireValues(_Closed):
    vllm_requests_running: int = Field(ge=0)
    vllm_requests_waiting: int = Field(ge=0)
    vllm_kv_cache_usage_ratio: float = Field(ge=0, le=1)
    vllm_preemptions_total: int = Field(ge=0)


class QueueVllmWireValues(_Closed):
    """Raw producer bytes as forwarded, plus the exporter's exact vLLM digest."""

    api_health: str = Field(min_length=1, max_length=_MAX_HEALTH_BYTES)
    api_http: str = Field(min_length=1, max_length=_MAX_HTTP_BYTES)
    vllm: VllmWireValues


class QueueVllmWireObservation(_Closed):
    status: ObservationStatus
    reason: UnsupportedReason | None
    values: QueueVllmWireValues | None

    @model_validator(mode="after")
    def _check_values(self) -> "QueueVllmWireObservation":
        if self.status == "supported":
            if self.values is None or self.reason is not None:
                raise ValueError("supported observation requires values and no reason")
        elif self.values is not None or self.reason is None:
            raise ValueError("unsupported observation requires a reason and no values")
        return self


class WindowsGpuResidentSample(_Closed):
    contract_version: Literal["mineru.windows-resident-telemetry.v2"] = WIRE_VERSION
    lane: Literal["gpu_fast"]
    sequence: int = Field(ge=1)
    observed_at_utc: datetime
    sampled_monotonic_ns: int = Field(ge=0)
    identity: ResidentIdentity
    gpu: GpuObservation

    @model_validator(mode="after")
    def _clock(self) -> "WindowsGpuResidentSample":
        if self.observed_at_utc.tzinfo is None or self.observed_at_utc.utcoffset() != timezone.utc.utcoffset(self.observed_at_utc):
            raise ValueError("observed_at_utc must be UTC")
        return self


class WindowsHostResidentSample(_Closed):
    contract_version: Literal["mineru.windows-resident-telemetry.v2"] = WIRE_VERSION
    lane: Literal["host_slow"]
    sequence: int = Field(ge=1)
    observed_at_utc: datetime
    sampled_monotonic_ns: int = Field(ge=0)
    identity: ResidentIdentity
    api_process: ApiProcessObservation
    host_cgroup: HostCgroupObservation
    queue_vllm: QueueVllmWireObservation

    @model_validator(mode="after")
    def _clock(self) -> "WindowsHostResidentSample":
        if self.observed_at_utc.tzinfo is None or self.observed_at_utc.utcoffset() != timezone.utc.utcoffset(self.observed_at_utc):
            raise ValueError("observed_at_utc must be UTC")
        return self


def decode_windows_resident_sample(
    payload: bytes,
    *,
    lane: Literal["gpu_fast", "host_slow"],
    maximum_bytes: int = 64 * 1024,
) -> WindowsGpuResidentSample | WindowsHostResidentSample:
    """Decode exact canonical bytes; duplicate/nonfinite/noncanonical input is rejected."""

    value = parse_canonical_json_artifact(
        payload,
        label="Windows resident telemetry sample",
        maximum_bytes=maximum_bytes,
    )
    if not isinstance(value, dict):
        raise ValueError("Windows resident telemetry sample must be an object")
    model = WindowsGpuResidentSample if lane == "gpu_fast" else WindowsHostResidentSample
    sample = model.model_validate(value)
    return sample


class WindowsResidentPullV1(_Closed):
    """One fresh-per-request reply: the exporter's own QPC instants around exactly one backend call.

    ``request_received`` (a) is taken after the request was validated,
    ``sample.sampled_monotonic_ns`` (c) immediately before the single backend
    call, ``sample_capture_finished`` (d) after it returned and
    ``reply_started`` (b) before the bounded reply. A cached sample would
    violate a <= c and is rejected here, on the wire, before any projection.
    """

    contract_version: Literal["mineru.windows-resident-pull.v1"] = PULL_VERSION
    request_nonce: str
    after_sequence: int = Field(ge=0)
    request_received_monotonic_ns: int = Field(ge=1)
    sample_capture_finished_monotonic_ns: int = Field(ge=1)
    reply_started_monotonic_ns: int = Field(ge=1)
    sample: WindowsGpuResidentSample | WindowsHostResidentSample

    @model_validator(mode="after")
    def _fresh(self) -> "WindowsResidentPullV1":
        if _NONCE.fullmatch(self.request_nonce) is None:
            raise ValueError("pull request nonce must be 32 lowercase hex characters")
        if self.sample.sequence != self.after_sequence + 1:
            raise ValueError("pull sample sequence must follow the requested cursor")
        if not (self.request_received_monotonic_ns <= self.sample.sampled_monotonic_ns
                <= self.sample_capture_finished_monotonic_ns <= self.reply_started_monotonic_ns):
            raise ValueError("pull reply carries a sample captured outside this request")
        return self


def decode_windows_resident_pull(
    payload: bytes, *, lane: Literal["gpu_fast", "host_slow"], maximum_bytes: int = 64 * 1024,
) -> WindowsResidentPullV1:
    """Decode exact canonical pull bytes; the inner sample must be this lane's closed sample."""

    value = parse_canonical_json_artifact(payload, label="Windows resident pull", maximum_bytes=maximum_bytes)
    if not isinstance(value, dict) or not isinstance(value.get("sample"), dict):
        raise ValueError("Windows resident pull must be an object carrying one sample object")
    sample_model = WindowsGpuResidentSample if lane == "gpu_fast" else WindowsHostResidentSample
    sample = sample_model.model_validate(value["sample"])
    return WindowsResidentPullV1.model_validate({**value, "sample": sample})


@dataclass(frozen=True, slots=True)
class HostQueueBinding:
    """What the host lane's raw API evidence is bound to before it becomes frame values.

    The capacity is the release's frozen config (one authority for N/P/F/H/B/L
    and the window); the serving PID, boot and start ticks are the same API
    process the Linux sampler measures in the same frame (from the host READY);
    the retention/cleanup seconds come from the exact process profile.
    """

    expected_capacity: MineruCapacityConfig
    serving_namespace_pid: int
    api_boot_id: str
    api_start_ticks: int
    task_retention_seconds: int
    task_cleanup_interval_seconds: int

    def __post_init__(self) -> None:
        if type(self.expected_capacity) is not MineruCapacityConfig:
            raise ValueError("host queue binding requires the exact capacity config type")
        for name in ("serving_namespace_pid", "api_start_ticks", "task_retention_seconds", "task_cleanup_interval_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"host queue binding {name} must be a positive integer")
        if type(self.api_boot_id) is not str or _UUID.fullmatch(self.api_boot_id) is None:
            raise ValueError("host queue binding boot identity must be a canonical UUID")

    def as_config(self) -> dict[str, object]:
        return {
            "capacity_config": cast(dict[str, object], json.loads(self.expected_capacity.exact_bytes)),
            "serving_namespace_pid": self.serving_namespace_pid,
            "api_boot_id": self.api_boot_id,
            "api_start_ticks": self.api_start_ticks,
            "task_retention_seconds": self.task_retention_seconds,
            "task_cleanup_interval_seconds": self.task_cleanup_interval_seconds,
        }

    @classmethod
    def from_config(cls, value: object) -> "HostQueueBinding":
        fields = {
            "capacity_config", "serving_namespace_pid", "api_boot_id", "api_start_ticks",
            "task_retention_seconds", "task_cleanup_interval_seconds",
        }
        if type(value) is not dict or set(value) != fields or type(value["capacity_config"]) is not dict:
            raise ValueError("host queue binding config shape is invalid")
        capacity = decode_mineru_capacity_config(json.dumps(
            value["capacity_config"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8"))
        return cls(
            expected_capacity=capacity,
            serving_namespace_pid=cast(int, value["serving_namespace_pid"]),
            api_boot_id=cast(str, value["api_boot_id"]),
            api_start_ticks=cast(int, value["api_start_ticks"]),
            task_retention_seconds=cast(int, value["task_retention_seconds"]),
            task_cleanup_interval_seconds=cast(int, value["task_cleanup_interval_seconds"]),
        )


def _api_http_snapshot(raw: str, *, expected_process_id: int) -> dict[str, int]:
    payload = raw.encode("utf-8")
    if len(payload) > _MAX_HTTP_BYTES:
        raise ValueError("resident API HTTP snapshot exceeds its byte bound")
    decoded = strict_json_loads(payload)
    if (
        type(decoded) is not dict
        or set(decoded) != {"contract_version", "process_id", "active_requests", "pending_requests"}
        or decoded["contract_version"] != API_HTTP_SNAPSHOT_CONTRACT
        or any(type(decoded[name]) is not int or decoded[name] < 0
               for name in ("process_id", "active_requests", "pending_requests"))
    ):
        raise ValueError("resident API HTTP snapshot fields are not closed")
    if decoded["process_id"] != expected_process_id:
        raise ValueError("resident API HTTP snapshot names another serving process")
    return cast(dict[str, int], decoded)


def project_queue_vllm(wire: QueueVllmWireObservation, *, binding: HostQueueBinding) -> QueueVllmObservation:
    """Validate the forwarded producer bytes against the frozen capacity, then project the closed frame values.

    The health passes the same closed validator every capacity consumer uses;
    its owner must be the API process the Linux sampler measures; a draining
    owner or closed admission is a drift, not a sample.
    """
    if wire.status != "supported":
        return QueueVllmObservation(status="unsupported", reason=wire.reason, values=None)
    values = wire.values
    if values is None:
        raise ValueError("supported queue observation lacks values")
    health_bytes = values.api_health.encode("utf-8")
    if len(health_bytes) > _MAX_HEALTH_BYTES:
        raise ValueError("resident API health exceeds its byte bound")
    health = parse_mineru_capacity_wire_health(
        health_bytes, expected_capacity=binding.expected_capacity,
        expected_task_retention_seconds=binding.task_retention_seconds,
        expected_cleanup_interval_seconds=binding.task_cleanup_interval_seconds,
    )
    observation = cast(dict[str, Any], health["capacity_observation"])
    owner = cast(dict[str, Any], observation["owner"])
    if (
        owner["process_id"] != binding.serving_namespace_pid
        or owner["boot_id"] != binding.api_boot_id
        or owner["process_start_ticks"] != binding.api_start_ticks
    ):
        raise ValueError("resident API health owner differs from the sampled API process")
    control = cast(dict[str, Any], observation["owner_control"])
    admission = cast(dict[str, Any], health["task_admission"])
    if (
        any(control[field] for field in ("foreign_loop_observed", "soft_drain_requested", "soft_drain_applied"))
        or admission["blocked_reason"] not in (None, "capacity_full")
    ):
        raise ValueError("resident API owner is draining or admission is closed")
    http = _api_http_snapshot(values.api_http, expected_process_id=binding.serving_namespace_pid)
    return QueueVllmObservation(
        status="supported", reason=None,
        values=QueueVllmTelemetryValues(
            api_queued_tasks=health["queued_tasks"],
            api_processing_tasks=health["processing_tasks"],
            api_nonterminal_tasks=health["queued_tasks"] + health["processing_tasks"],
            api_http_active_requests=http["active_requests"],
            api_http_pending_requests=http["pending_requests"],
            api_max_pending_tasks=health["max_pending_tasks_effective"],
            vllm_requests_running=values.vllm.vllm_requests_running,
            vllm_requests_waiting=values.vllm.vllm_requests_waiting,
            vllm_kv_cache_usage_ratio=values.vllm.vllm_kv_cache_usage_ratio,
            vllm_preemptions_total=values.vllm.vllm_preemptions_total,
        ),
    )


__all__ = [
    "API_HTTP_SNAPSHOT_CONTRACT",
    "HostQueueBinding",
    "PULL_VERSION",
    "QueueVllmWireObservation",
    "QueueVllmWireValues",
    "ResidentIdentity",
    "VllmWireValues",
    "WIRE_VERSION",
    "WindowsGpuResidentSample",
    "WindowsHostResidentSample",
    "WindowsResidentPullV1",
    "decode_windows_resident_pull",
    "decode_windows_resident_sample",
    "project_queue_vllm",
]
