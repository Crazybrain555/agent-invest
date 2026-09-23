"""Persistent, descendant-free adapter for the Windows resident exporter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
import secrets
import time
from typing import Literal, cast
from urllib.parse import urlsplit

from disclosure_anchor.adapters.runtime.bounded_http import (
    BoundedHTTPProtocolError,
    BoundedHTTPTransportError,
    ThreadOwnedPersistentHTTPClient,
)
from disclosure_anchor.application.contracts.windows_resident_telemetry import (
    PULL_VERSION,
    HostQueueBinding,
    ResidentIdentity,
    WindowsGpuResidentSample,
    WindowsHostResidentSample,
    WindowsResidentPullV1,
    decode_windows_resident_pull,
    decode_windows_resident_sample,
    project_queue_vllm,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    ResidentExporterPullProvenance,
    ResidentExporterSampleProvenance,
)
from disclosure_anchor.application.services.resident_measurement_policy import (
    PullTiming,
    pull_capture_bounds,
)
from disclosure_anchor.application.ports.synchronized_telemetry import (
    GpuLaneSnapshot,
    HostLaneSnapshot,
    ResidentTelemetryCollectorSpec,
    TelemetrySampleIdentity,
    TelemetrySnapshotContinuityLost,
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
    observer_clock_domain_identity_sha256: str
    expected_identity: ResidentIdentity
    ssh: dict[str, object] | None = None
    host_binding: HostQueueBinding | None = None
    # R22 fresh-per-request protocol; None keeps the historical /after/{n} path
    # readable for unit replay only. The live owner always selects the pull.
    pull_protocol: Literal["mineru.windows-resident-pull.v1"] | None = None


class WindowsResidentTelemetrySampler:
    """One owning collector process reuses one direct HTTP connection."""

    def __init__(self, config: _Config) -> None:
        if (config.lane == "host_slow") != (config.host_binding is not None):
            raise ValueError("the host lane requires exactly one frozen capacity binding")
        self._config = config
        self._client: ThreadOwnedPersistentHTTPClient
        if config.ssh is None:
            self._client = ThreadOwnedPersistentHTTPClient(
                config.base_url,
                maximum_response_bytes=config.maximum_response_bytes,
                user_agent="disclosure-anchor-resident-telemetry/1",
            )
        else:
            from disclosure_anchor.adapters.runtime.resident_ssh_http import (
                ResidentSSHConfig,
                ResidentSSHHTTPClient,
            )
            parsed = urlsplit(config.base_url)
            if (
                parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.port is None or parsed.path not in {"", "/"}
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
            ):
                raise ValueError("SSH resident HTTP destination must be one explicit loopback port")
            self._client = ResidentSSHHTTPClient(
                ResidentSSHConfig(**config.ssh),  # type: ignore[arg-type]
                remote_port=parsed.port,
                maximum_response_bytes=config.maximum_response_bytes,
            )
        self._last_sequence = 0
        self._last_wire_monotonic_ns: int | None = None
        self._last_wire_observed_at: datetime | None = None
        self._identity: ResidentIdentity | None = None
        self._last_native_reply_ns: int | None = None
        self._terminal_continuity_lost = False

    @property
    def collector_identity_sha256(self) -> str:
        return self._config.collector_identity_sha256

    def adopt_supervisor_interruption(self) -> None:
        """Called only by the spawned collector entry whose parent kills it at the deadline."""
        self._client.adopt_supervisor_interruption()

    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> GpuLaneSnapshot | HostLaneSnapshot:
        if self._terminal_continuity_lost:
            raise TelemetrySnapshotContinuityLost(
                "resident exporter sequence continuity was already lost"
            )
        remaining = (deadline.monotonic_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            raise TelemetrySnapshotDeadlineExceeded("snapshot deadline already expired")
        if self._config.pull_protocol is not None:
            return self._pull_snapshot(deadline)
        try:
            status, payload = self._client.get_bytes(
                f"{self._config.path}/after/{self._last_sequence}",
                timeout_seconds=remaining,
                transport_attempts=1,
                absolute_deadline=deadline.monotonic_ns / 1_000_000_000,
            )
        except BoundedHTTPTransportError as exc:
            raise TelemetrySnapshotTransportUnavailable(str(exc)) from exc
        except BoundedHTTPProtocolError:
            raise
        if status == 409:
            self._terminal_continuity_lost = True
            self.close()
            raise TelemetrySnapshotContinuityLost(
                "resident exporter no longer retains the exact next sequence"
            )
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
        if sample.sequence != self._last_sequence + 1:
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
            # The observer brackets this call with its own local monotonic clock.
            # The remote QPC identity is checked above, never relabelled as local.
            clock_domain_identity_sha256=self._config.observer_clock_domain_identity_sha256,
        )
        if isinstance(sample, WindowsGpuResidentSample):
            return GpuLaneSnapshot(
                identity=identity,
                gpu=sample.gpu,
                resident_exporter_provenance=_provenance(sample),
            )
        if not isinstance(sample, WindowsHostResidentSample) or self._config.host_binding is None:
            raise AssertionError("closed decoder returned an unknown sample")
        # The forwarded producer bytes pass the shared capacity validator here,
        # against the release's frozen capacity, before they become frame values.
        return HostLaneSnapshot(
            identity=identity,
            api_process=sample.api_process,
            host_cgroup=sample.host_cgroup,
            queue_vllm=project_queue_vllm(sample.queue_vllm, binding=self._config.host_binding),
            resident_exporter_provenance=_provenance(sample),
        )

    def _pull_snapshot(self, deadline: TelemetrySnapshotDeadline) -> GpuLaneSnapshot | HostLaneSnapshot:
        """Fresh-per-request: one nonce, one request bracket, one native capture inside it.

        The request bracket [s, f] is this collector's own monotonic clock; the
        exporter's QPC instants are checked only against each other. No wall
        clock, no cross-host difference and no native cadence rule take part.
        """
        nonce = secrets.token_hex(16)
        cursor = self._last_sequence
        request_ns = time.monotonic_ns()
        remaining = (deadline.monotonic_ns - request_ns) / 1_000_000_000
        if remaining <= 0:
            raise TelemetrySnapshotDeadlineExceeded("snapshot deadline expired before dispatch")
        try:
            status, payload = self._client.get_bytes(
                f"{self._config.path}/after/{cursor}/request/{nonce}",
                timeout_seconds=remaining,
                transport_attempts=1,
                absolute_deadline=deadline.monotonic_ns / 1_000_000_000,
            )
        except BoundedHTTPTransportError as exc:
            raise TelemetrySnapshotTransportUnavailable(str(exc)) from exc
        except BoundedHTTPProtocolError:
            raise
        response_ns = time.monotonic_ns()
        if status == 409:
            self._terminal_continuity_lost = True
            self.close()
            raise TelemetrySnapshotContinuityLost(
                "resident exporter no longer retains the exact next sequence"
            )
        if status != 200:
            raise TelemetrySnapshotTransportUnavailable(
                f"resident exporter returned HTTP {status}"
            )
        pull = decode_windows_resident_pull(
            payload, lane=self._config.lane, maximum_bytes=self._config.maximum_response_bytes,
        )
        if pull.request_nonce != nonce or pull.after_sequence != cursor:
            raise ValueError("resident exporter answered another request than this one")
        sample = pull.sample
        if sample.identity != self._config.expected_identity:
            raise ValueError("resident exporter identity drifted from the pinned identity")
        if self._identity is not None and sample.identity != self._identity:
            raise ValueError("resident exporter identity changed during the collector lifetime")
        if sample.sequence != cursor + 1:
            raise ValueError("resident exporter sequence has a gap or rollback")
        if self._last_wire_monotonic_ns is not None and sample.sampled_monotonic_ns <= self._last_wire_monotonic_ns:
            raise ValueError("resident exporter source clock did not advance between requests")
        if self._last_native_reply_ns is not None and pull.request_received_monotonic_ns < self._last_native_reply_ns:
            raise ValueError("resident exporter received this request before it replied to the previous one")
        timing = PullTiming(
            request_nonce=pull.request_nonce, after_sequence=pull.after_sequence, sample_sequence=sample.sequence,
            local_request_ns=request_ns, local_response_ns=response_ns,
            q_request_ns=pull.request_received_monotonic_ns, q_capture_start_ns=sample.sampled_monotonic_ns,
            q_capture_end_ns=pull.sample_capture_finished_monotonic_ns, q_reply_ns=pull.reply_started_monotonic_ns,
        )
        pull_capture_bounds(
            timing, expected_nonce=nonce, expected_after_sequence=cursor,
            maximum_age_ns=self._config.maximum_sample_age_ms * 1_000_000,
        )
        self._identity = sample.identity
        self._last_sequence = sample.sequence
        self._last_wire_monotonic_ns = sample.sampled_monotonic_ns
        self._last_wire_observed_at = sample.observed_at_utc
        self._last_native_reply_ns = pull.reply_started_monotonic_ns
        identity = TelemetrySampleIdentity(
            runtime_bundle_identity_sha256=sample.identity.runtime_bundle_identity_sha256,
            process_profile_sha256=sample.identity.process_profile_sha256,
            clock_domain_identity_sha256=self._config.observer_clock_domain_identity_sha256,
        )
        provenance = _pull_provenance(pull, request_ns=request_ns, response_ns=response_ns)
        if isinstance(sample, WindowsGpuResidentSample):
            return GpuLaneSnapshot(identity=identity, gpu=sample.gpu, resident_exporter_provenance=provenance)
        if not isinstance(sample, WindowsHostResidentSample) or self._config.host_binding is None:
            raise AssertionError("closed decoder returned an unknown sample")
        return HostLaneSnapshot(
            identity=identity, api_process=sample.api_process, host_cgroup=sample.host_cgroup,
            queue_vllm=project_queue_vllm(sample.queue_vllm, binding=self._config.host_binding),
            resident_exporter_provenance=provenance,
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
        "observer_clock_domain_identity_sha256",
        "expected_identity",
    }
    optional_keys = {"ssh", "host_binding", "pull_protocol"}
    if not expected_keys <= set(config) <= expected_keys | optional_keys:
        raise ValueError("resident telemetry collector config shape is invalid")
    if "pull_protocol" in config and config["pull_protocol"] != PULL_VERSION:
        raise ValueError("resident telemetry pull protocol is not the supported version")
    ssh = config.get("ssh")
    if "ssh" in config and (
        not isinstance(ssh, dict)
        or set(ssh) != {"address", "port", "username", "private_key_path", "known_hosts_path"}
        or any(not isinstance(ssh[name], str) for name in ssh if name != "port")
        or type(ssh["port"]) is not int
    ):
        raise ValueError("resident SSH private configuration shape is invalid")
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
    observer_clock = config["observer_clock_domain_identity_sha256"]
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
    if not isinstance(collector_identity, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", collector_identity) is None:
        raise ValueError("collector_identity_sha256 is invalid")
    if not isinstance(observer_clock, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", observer_clock) is None:
        raise ValueError("observer_clock_domain_identity_sha256 is invalid")
    base_url = config["base_url"]
    if not isinstance(base_url, str):
        raise ValueError("base_url is invalid")
    identity = ResidentIdentity.model_validate(config["expected_identity"])
    host_binding = HostQueueBinding.from_config(config["host_binding"]) if "host_binding" in config else None
    return WindowsResidentTelemetrySampler(
        _Config(
            cast(Literal["gpu_fast", "host_slow"], lane),
            base_url,
            path,
            maximum_bytes,
            maximum_age,
            nominal_interval,
            collector_identity,
            observer_clock,
            identity,
            cast(dict[str, object] | None, ssh),
            host_binding,
            PULL_VERSION if "pull_protocol" in config else None,
        )
    )


def canonical_collector_config(**values: object) -> bytes:
    return json.dumps(values, sort_keys=True, separators=(",", ":")).encode()


def windows_resident_collector_spec(
    *,
    collector_identity_sha256: str,
    observer_clock_domain_identity_sha256: str,
    lane: Literal["gpu_fast", "host_slow"],
    base_url: str,
    path: str,
    maximum_response_bytes: int,
    maximum_sample_age_ms: int,
    nominal_interval_ms: int,
    expected_identity: ResidentIdentity,
    ssh: dict[str, object] | None = None,
    host_binding: HostQueueBinding | None = None,
    pull_protocol: Literal["mineru.windows-resident-pull.v1"] | None = None,
) -> ResidentTelemetryCollectorSpec:
    """Build the closed default-off spec; the config contains no credential."""

    if (lane == "host_slow") != (host_binding is not None):
        raise ValueError("the host lane requires exactly one frozen capacity binding")
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
            observer_clock_domain_identity_sha256=observer_clock_domain_identity_sha256,
            expected_identity=expected_identity.model_dump(mode="json"),
            **({"ssh": ssh} if ssh is not None else {}),
            **({"host_binding": host_binding.as_config()} if host_binding is not None else {}),
            **({"pull_protocol": pull_protocol} if pull_protocol is not None else {}),
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


def _pull_provenance(pull: WindowsResidentPullV1, *, request_ns: int, response_ns: int) -> ResidentExporterPullProvenance:
    sample = pull.sample
    return ResidentExporterPullProvenance(
        exporter_source_sha256=sample.identity.exporter_source_sha256,
        host_assignment_identity_sha256=sample.identity.host_assignment_identity_sha256,
        boot_identity_sha256=sample.identity.boot_identity_sha256,
        exporter_process_epoch_sha256=sample.identity.exporter_process_epoch_sha256,
        wire_sequence=sample.sequence, wire_observed_at_utc=sample.observed_at_utc,
        wire_sampled_monotonic_ns=sample.sampled_monotonic_ns,
        request_nonce=pull.request_nonce, after_sequence=pull.after_sequence,
        local_request_monotonic_ns=request_ns, local_response_monotonic_ns=response_ns,
        native_request_received_monotonic_ns=pull.request_received_monotonic_ns,
        native_capture_finished_monotonic_ns=pull.sample_capture_finished_monotonic_ns,
        native_reply_started_monotonic_ns=pull.reply_started_monotonic_ns,
    )


__all__ = [
    "WindowsResidentTelemetrySampler",
    "build_windows_resident_telemetry_sampler",
    "canonical_collector_config",
    "windows_resident_collector_spec",
]
