"""Persistent, descendant-free adapter for the Windows resident exporter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import time
from typing import Literal, cast

from disclosure_anchor.adapters.runtime.bounded_http import (
    BoundedHTTPProtocolError,
    BoundedHTTPTransportError,
    ThreadOwnedPersistentHTTPClient,
)
from disclosure_anchor.application.contracts.windows_resident_telemetry import (
    ResidentIdentity,
    WindowsGpuResidentSample,
    WindowsHostResidentSample,
    decode_windows_resident_sample,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    ResidentExporterSampleProvenance,
)
from disclosure_anchor.application.ports.synchronized_telemetry import (
    GpuLaneSnapshot,
    HostLaneSnapshot,
    ResidentTelemetryCollectorSpec,
    TelemetrySampleIdentity,
    TelemetrySnapshotDeadline,
    TelemetrySnapshotDeadlineExceeded,
    TelemetrySnapshotTransportUnavailable,
)


@dataclass(frozen=True, slots=True)
class _Config:
    lane: Literal["gpu_fast", "host_slow"]
    base_url: str
    path: str
    maximum_response_bytes: int
    maximum_sample_age_ms: int
    nominal_interval_ms: int
    collector_identity_sha256: str
    expected_identity: ResidentIdentity


class WindowsResidentTelemetrySampler:
    """One owning collector process reuses one direct HTTP connection."""

    def __init__(self, config: _Config) -> None:
        self._config = config
        self._client = ThreadOwnedPersistentHTTPClient(
            config.base_url,
            maximum_response_bytes=config.maximum_response_bytes,
            user_agent="disclosure-anchor-resident-telemetry/1",
        )
        self._last_sequence = 0
        self._last_wire_monotonic_ns: int | None = None
        self._last_wire_observed_at: datetime | None = None
        self._identity: ResidentIdentity | None = None

    @property
    def collector_identity_sha256(self) -> str:
        return self._config.collector_identity_sha256

    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> GpuLaneSnapshot | HostLaneSnapshot:
        remaining = (deadline.monotonic_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            raise TelemetrySnapshotDeadlineExceeded("snapshot deadline already expired")
        try:
            status, payload = self._client.get_bytes(
                f"{self._config.path}/after/{self._last_sequence}",
                timeout_seconds=remaining,
                transport_attempts=1,
            )
        except BoundedHTTPTransportError as exc:
            raise TelemetrySnapshotTransportUnavailable(str(exc)) from exc
        except BoundedHTTPProtocolError:
            raise
        if status != 200:
            raise TelemetrySnapshotTransportUnavailable(
                f"resident exporter returned HTTP {status}"
            )
        sample = decode_windows_resident_sample(
            payload,
            lane=self._config.lane,
            maximum_bytes=self._config.maximum_response_bytes,
        )
        if sample.identity != self._config.expected_identity:
            raise ValueError("resident exporter identity drifted from the pinned identity")
        if self._identity is not None and sample.identity != self._identity:
            raise ValueError("resident exporter identity changed during the collector lifetime")
        if self._last_sequence and sample.sequence != self._last_sequence + 1:
            raise ValueError("resident exporter sequence has a gap or rollback")
        age_ms = (time.time() - sample.observed_at_utc.timestamp()) * 1000
        if age_ms < -1000 or age_ms > self._config.maximum_sample_age_ms:
            raise ValueError("resident exporter sample is stale or from the future")
        if self._last_wire_monotonic_ns is not None:
            assert self._last_wire_observed_at is not None
            monotonic_delta = sample.sampled_monotonic_ns - self._last_wire_monotonic_ns
            wall_delta = int(
                (sample.observed_at_utc - self._last_wire_observed_at).total_seconds()
                * 1_000_000_000
            )
            nominal_ns = self._config.nominal_interval_ms * 1_000_000
            if not nominal_ns * 9 // 10 <= monotonic_delta <= nominal_ns * 11 // 10:
                raise ValueError("resident exporter cadence drifted")
            if abs(wall_delta - monotonic_delta) > 50_000_000 + monotonic_delta * 50 // 1_000_000:
                raise ValueError("resident exporter wall and monotonic clocks diverged")
        self._identity = sample.identity
        self._last_sequence = sample.sequence
        self._last_wire_monotonic_ns = sample.sampled_monotonic_ns
        self._last_wire_observed_at = sample.observed_at_utc
        identity = TelemetrySampleIdentity(
            runtime_bundle_identity_sha256=sample.identity.runtime_bundle_identity_sha256,
            process_profile_sha256=sample.identity.process_profile_sha256,
            clock_domain_identity_sha256=sample.identity.clock_domain_identity_sha256,
        )
        if isinstance(sample, WindowsGpuResidentSample):
            return GpuLaneSnapshot(
                identity=identity,
                gpu=sample.gpu,
                resident_exporter_provenance=_provenance(sample),
            )
        if not isinstance(sample, WindowsHostResidentSample):
            raise AssertionError("closed decoder returned an unknown sample")
        return HostLaneSnapshot(
            identity=identity,
            api_process=sample.api_process,
            host_cgroup=sample.host_cgroup,
            queue_vllm=sample.queue_vllm,
            resident_exporter_provenance=_provenance(sample),
        )

    def close(self) -> None:
        self._client.close()


def build_windows_resident_telemetry_sampler(config: dict[str, object]) -> WindowsResidentTelemetrySampler:
    """Spawn-safe top-level factory used by ResidentTelemetryCollectorSpec."""

    expected_keys = {
        "lane",
        "base_url",
        "path",
        "maximum_response_bytes",
        "maximum_sample_age_ms",
        "nominal_interval_ms",
        "collector_identity_sha256",
        "expected_identity",
    }
    if set(config) != expected_keys:
        raise ValueError("resident telemetry collector config shape is invalid")
    lane = config["lane"]
    if lane not in {"gpu_fast", "host_slow"}:
        raise ValueError("resident telemetry lane is invalid")
    path = config["path"]
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("resident telemetry path is invalid")
    maximum_bytes = config["maximum_response_bytes"]
    maximum_age = config["maximum_sample_age_ms"]
    nominal_interval = config["nominal_interval_ms"]
    collector_identity = config["collector_identity_sha256"]
    if isinstance(maximum_bytes, bool) or not isinstance(maximum_bytes, int) or maximum_bytes < 1:
        raise ValueError("maximum_response_bytes is invalid")
    if isinstance(maximum_age, bool) or not isinstance(maximum_age, int) or maximum_age < 250:
        raise ValueError("maximum_sample_age_ms is invalid")
    if (
        isinstance(nominal_interval, bool)
        or not isinstance(nominal_interval, int)
        or nominal_interval not in {250, 500, 1000}
        or (lane == "host_slow" and nominal_interval != 1000)
        or (lane == "gpu_fast" and nominal_interval not in {250, 500})
    ):
        raise ValueError("nominal_interval_ms is invalid")
    if not isinstance(collector_identity, str):
        raise ValueError("collector_identity_sha256 is invalid")
    base_url = config["base_url"]
    if not isinstance(base_url, str):
        raise ValueError("base_url is invalid")
    identity = ResidentIdentity.model_validate(config["expected_identity"])
    return WindowsResidentTelemetrySampler(
        _Config(
            cast(Literal["gpu_fast", "host_slow"], lane),
            base_url,
            path,
            maximum_bytes,
            maximum_age,
            nominal_interval,
            collector_identity,
            identity,
        )
    )


def canonical_collector_config(**values: object) -> bytes:
    return json.dumps(values, sort_keys=True, separators=(",", ":")).encode()


def windows_resident_collector_spec(
    *,
    collector_identity_sha256: str,
    lane: Literal["gpu_fast", "host_slow"],
    base_url: str,
    path: str,
    maximum_response_bytes: int,
    maximum_sample_age_ms: int,
    nominal_interval_ms: int,
    expected_identity: ResidentIdentity,
) -> ResidentTelemetryCollectorSpec:
    """Build the closed default-off spec; the config contains no credential."""

    return ResidentTelemetryCollectorSpec(
        factory_module=__name__,
        factory_qualname="build_windows_resident_telemetry_sampler",
        canonical_config_json=canonical_collector_config(
            lane=lane,
            base_url=base_url,
            path=path,
            maximum_response_bytes=maximum_response_bytes,
            maximum_sample_age_ms=maximum_sample_age_ms,
            nominal_interval_ms=nominal_interval_ms,
            collector_identity_sha256=collector_identity_sha256,
            expected_identity=expected_identity.model_dump(mode="json"),
        ),
        expected_collector_identity_sha256=collector_identity_sha256,
        descendants_capability="forbidden",
    )


def _provenance(
    sample: WindowsGpuResidentSample | WindowsHostResidentSample,
) -> ResidentExporterSampleProvenance:
    return ResidentExporterSampleProvenance(
        exporter_source_sha256=sample.identity.exporter_source_sha256,
        host_assignment_identity_sha256=sample.identity.host_assignment_identity_sha256,
        boot_identity_sha256=sample.identity.boot_identity_sha256,
        exporter_process_epoch_sha256=sample.identity.exporter_process_epoch_sha256,
        wire_sequence=sample.sequence,
        wire_observed_at_utc=sample.observed_at_utc,
        wire_sampled_monotonic_ns=sample.sampled_monotonic_ns,
    )


__all__ = [
    "WindowsResidentTelemetrySampler",
    "build_windows_resident_telemetry_sampler",
    "canonical_collector_config",
    "windows_resident_collector_spec",
]
