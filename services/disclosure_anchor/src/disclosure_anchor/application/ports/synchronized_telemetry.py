"""Injected sampling ports for the content-free synchronized observer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from disclosure_anchor.application.contracts.synchronized_telemetry import (
    ApiProcessObservation,
    GpuObservation,
    HostCgroupObservation,
    QueueVllmObservation,
    ResidentExporterSampleProvenance,
    parse_canonical_json_artifact,
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
    resident_exporter_provenance: ResidentExporterSampleProvenance | None = None


@dataclass(frozen=True, slots=True)
class HostLaneSnapshot:
    identity: TelemetrySampleIdentity
    api_process: ApiProcessObservation
    host_cgroup: HostCgroupObservation
    queue_vllm: QueueVllmObservation
    resident_exporter_provenance: ResidentExporterSampleProvenance | None = None


@dataclass(frozen=True, slots=True)
class TelemetrySnapshotDeadline:
    """Absolute local deadline for a non-blocking resident snapshot read."""

    monotonic_ns: int


class TelemetrySnapshotDeadlineExceeded(TimeoutError):
    """The resident collector could not return a snapshot by the deadline."""


class TelemetrySnapshotTransportUnavailable(ConnectionError):
    """The already-resident collector transport is unavailable."""


class TelemetrySnapshotContinuityLost(RuntimeError):
    """The exporter no longer retains the exact next sequence; restart the run."""


@dataclass(frozen=True, slots=True)
class ResidentTelemetryCollectorSpec:
    """Spawn-safe closed factory contract; no live sampler object crosses spawn."""

    factory_module: str
    factory_qualname: str
    canonical_config_json: bytes
    expected_collector_identity_sha256: str
    descendants_capability: Literal["forbidden"] = "forbidden"

    def __post_init__(self) -> None:
        if not self.factory_module or not self.factory_qualname:
            raise ValueError("collector factory reference is empty")
        parsed = parse_canonical_json_artifact(
            self.canonical_config_json,
            label="collector config",
            maximum_bytes=64 * 1024,
        )
        if not isinstance(parsed, dict):
            raise ValueError("collector config must be a canonical JSON object")
        digest = self.expected_collector_identity_sha256.removeprefix("sha256:")
        if (
            not self.expected_collector_identity_sha256.startswith("sha256:")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("collector identity is not a canonical SHA-256")


class GpuTelemetrySamplerPort(Protocol):
    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> GpuLaneSnapshot: ...


class HostTelemetrySamplerPort(Protocol):
    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> HostLaneSnapshot: ...


__all__ = [
    "GpuLaneSnapshot",
    "GpuTelemetrySamplerPort",
    "HostLaneSnapshot",
    "HostTelemetrySamplerPort",
    "ResidentTelemetryCollectorSpec",
    "TelemetrySampleIdentity",
    "TelemetrySnapshotContinuityLost",
    "TelemetrySnapshotDeadline",
    "TelemetrySnapshotDeadlineExceeded",
    "TelemetrySnapshotTransportUnavailable",
]
