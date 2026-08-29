"""Injected sampling ports for the content-free synchronized observer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from disclosure_anchor.application.contracts.synchronized_telemetry import (
    ApiProcessObservation,
    GpuObservation,
    HostCgroupObservation,
    QueueVllmObservation,
)


@dataclass(frozen=True, slots=True)
class TelemetrySampleIdentity:
    """Runtime identities observed atomically with one lane sample."""

    runtime_bundle_identity_sha256: str
    process_profile_sha256: str
    clock_domain_identity_sha256: str


@dataclass(frozen=True, slots=True)
class GpuLaneSnapshot:
    identity: TelemetrySampleIdentity
    gpu: GpuObservation


@dataclass(frozen=True, slots=True)
class HostLaneSnapshot:
    identity: TelemetrySampleIdentity
    api_process: ApiProcessObservation
    host_cgroup: HostCgroupObservation
    queue_vllm: QueueVllmObservation


@dataclass(frozen=True, slots=True)
class TelemetrySnapshotDeadline:
    """Absolute local deadline for a non-blocking resident snapshot read."""

    monotonic_ns: int


class TelemetrySnapshotDeadlineExceeded(TimeoutError):
    """The resident collector could not return a snapshot by the deadline."""


class TelemetrySnapshotTransportUnavailable(ConnectionError):
    """The already-resident collector transport is unavailable."""


class GpuTelemetrySamplerPort(Protocol):
    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> GpuLaneSnapshot: ...


class HostTelemetrySamplerPort(Protocol):
    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> HostLaneSnapshot: ...


__all__ = [
    "GpuLaneSnapshot",
    "GpuTelemetrySamplerPort",
    "HostLaneSnapshot",
    "HostTelemetrySamplerPort",
    "TelemetrySampleIdentity",
    "TelemetrySnapshotDeadline",
    "TelemetrySnapshotDeadlineExceeded",
    "TelemetrySnapshotTransportUnavailable",
]
