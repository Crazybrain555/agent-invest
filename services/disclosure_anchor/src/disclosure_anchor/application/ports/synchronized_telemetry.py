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


class GpuTelemetrySamplerPort(Protocol):
    def sample(self) -> GpuLaneSnapshot: ...


class HostTelemetrySamplerPort(Protocol):
    def sample(self) -> HostLaneSnapshot: ...


__all__ = [
    "GpuLaneSnapshot",
    "GpuTelemetrySamplerPort",
    "HostLaneSnapshot",
    "HostTelemetrySamplerPort",
    "TelemetrySampleIdentity",
]
