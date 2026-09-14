"""Two persistent read lanes and a non-blocking cache for staged admission.

The API lane reads its self cgroup/VM, never hidden ancestors. The already
qualified document envelope remains the upper bound; these signals only govern
pressure reduction/recovery within it. No shell, SSH process or GPU work runs
in the scheduler or in these readers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from math import ceil, isfinite
import threading
import time
from typing import Literal
from urllib.parse import urlsplit

from disclosure_anchor.adapters.runtime.bounded_http import (
    BoundedHTTPTransportError, ThreadOwnedPersistentHTTPClient,
)
from disclosure_anchor.adapters.runtime.capacity_sources import _gpu_values, _prometheus
from disclosure_anchor.adapters.runtime.gpu_telemetry_freshness import (
    GpuCollectionUnavailableError, GpuSampleStaleError,
)
from disclosure_anchor.application.contracts.mineru_capacity_config import MineruCapacityConfig
from disclosure_anchor.application.contracts.mineru_capacity_health import parse_mineru_capacity_wire_health
from disclosure_anchor.application.contracts.mineru_process_pressure import parse_mineru_process_pressure
from disclosure_anchor.application.ports.mineru_stream_pressure import StreamPressureSample


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True, slots=True)
class PressureBinding:
    runtime_identity_sha256: str
    capacity: MineruCapacityConfig
    owner_json: bytes
    cgroup_identity_sha256: str
    cgroup_max_bytes: int | None
    gpu_uuid: str
    api_max_age_seconds: float = 3.0
    gpu_max_age_seconds: float = 8.0

    @property
    def owner(self) -> dict[str, object]:
        return json.loads(self.owner_json)

    @property
    def owner_sha256(self) -> str:
        return _sha(self.owner_json)

    def __post_init__(self) -> None:
        from disclosure_anchor.application.contracts.mineru_process_pressure import ProcessPressureOwner
        from uuid import UUID
        owner = ProcessPressureOwner.model_validate(json.loads(self.owner_json))
        if self.owner_json != _canonical(owner.model_dump()):
            raise ValueError("pressure owner binding must use canonical bytes")
        for value in (owner.boot_id, owner.loop_epoch):
            if str(UUID(value)) != value:
                raise ValueError("pressure owner UUID is not canonical")
        for value in (self.runtime_identity_sha256, self.cgroup_identity_sha256):
            if type(value) is not str or len(value) != 71 or not value.startswith("sha256:") or any(c not in "0123456789abcdef" for c in value[7:]):
                raise ValueError("pressure binding hash is invalid")
        if type(self.capacity) is not MineruCapacityConfig or not self.gpu_uuid:
            raise ValueError("pressure capacity/device binding is invalid")
        if self.cgroup_max_bytes is not None and (type(self.cgroup_max_bytes) is not int or self.cgroup_max_bytes <= 0):
            raise ValueError("pressure cgroup ceiling is invalid")
        for duration in (self.api_max_age_seconds, self.gpu_max_age_seconds):
            if isinstance(duration, bool) or not isfinite(duration) or duration <= 0:
                raise ValueError("pressure source age is invalid")


class StreamPressureCache:
    def __init__(self, binding: PressureBinding, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.binding, self._clock = binding, monotonic
        self._lock = threading.Lock()
        self._api: tuple[float, int, int, int, str] | None = None
        self._gpu: tuple[float, int, str] | None = None
        self._events: dict[str, int] | None = None
        self._gpu_timestamp: float | None = None
        self._api_remote_finish = -1
        self._failures: dict[str, str] = {}
        self._unsafe: str | None = None
        self._sequence = 0

    def publish_api(self, health_payload: bytes, pressure_payload: bytes, *, started: float, finished: float) -> None:
        self._bracket(started, finished)
        b = self.binding
        health = parse_mineru_capacity_wire_health(health_payload, expected_capacity=b.capacity)
        observation = health["capacity_observation"]
        if observation["owner"] != b.owner:
            raise ValueError("pressure health owner drift")
        pressure = parse_mineru_process_pressure(pressure_payload, expected_capacity_sha256=b.capacity.sha256,
            expected_owner=b.owner, expected_cgroup_identity_sha256=b.cgroup_identity_sha256,
            expected_cgroup_max_bytes=b.cgroup_max_bytes)
        digest = _sha(_canonical({"health": _sha(health_payload), "pressure": _sha(pressure_payload), "started": started, "finished": finished}))
        events = pressure.memory.memory_events
        with self._lock:
            if pressure.observed_at.started_ns < self._api_remote_finish:
                self._unsafe = "api_pressure_clock_regressed"
            self._api_remote_finish = pressure.observed_at.completed_ns
            if self._events is not None:
                if set(events) != set(self._events) or any(events[k] < self._events[k] for k in self._events):
                    self._unsafe = "cgroup_events_regressed"
                elif any(events[k] > self._events[k] for k in ("oom", "oom_kill")):
                    self._unsafe = "new_cgroup_oom"
            self._events = dict(events)
            if any(observation["owner_control"][k] for k in ("foreign_loop_observed", "soft_drain_requested", "soft_drain_applied")):
                self._unsafe = "api_owner_control_not_open"
            self._api = (started, pressure.memory.observed_headroom_bytes,
                         observation["http_counters"]["active_requests"], observation["http_counters"]["pending_requests"], digest)
            self._failures.pop("api", None)

    def publish_gpu(self, payload: bytes, *, started: float, finished: float, received_wall: float) -> None:
        self._bracket(started, finished)
        unavailable = None
        try:
            values = _gpu_values(payload, expected_device_uuid=self.binding.gpu_uuid)
        except GpuCollectionUnavailableError as error:
            # Failed collections make no new successful-observation claim.
            # Retain the last valid value, timestamp and age; keep reading.
            self.record_failure("gpu", error)
            return
        except GpuSampleStaleError as error:
            unavailable = error
            values = None
        if values is not None and values.framebuffer_free_bytes is None:
            raise ValueError("GPU pressure free memory is unavailable")
        timestamps = _prometheus(payload).get("nvidia_smi_last_collect_success_timestamp_seconds", ())
        if len(timestamps) != 1:
            raise ValueError("GPU pressure timestamp missing")
        timestamp = timestamps[0]
        age = received_wall - timestamp
        if not isfinite(age) or age < -1:
            raise ValueError("GPU pressure timestamp is from the future")
        with self._lock:
            if self._gpu_timestamp is not None and timestamp < self._gpu_timestamp:
                self._unsafe = "gpu_sample_clock_regressed"
            if unavailable is not None:
                # A stale successful sample still cannot regress its clock.
                # It never replaces or rejuvenates the last valid GPU value.
                self._failures["gpu"] = f"{type(unavailable).__name__}:{unavailable}"[:200]
                return
            assert values is not None
            assert values.framebuffer_free_bytes is not None
            # Repeated reads of one exporter sample retain its original local
            # bracket; wall-clock jitter cannot rejuvenate cached GPU bytes.
            observed = (self._gpu[0] if self._gpu is not None and timestamp == self._gpu_timestamp
                        else max(0.0, started - max(0.0, age)))
            self._gpu_timestamp = timestamp
            self._gpu = (observed, values.framebuffer_free_bytes, _sha(payload))
            self._failures.pop("gpu", None)

    def record_failure(self, lane: Literal["api", "gpu"], error: BaseException, *, fatal: bool = False) -> None:
        with self._lock:
            self._failures[lane] = f"{type(error).__name__}:{error}"[:200]
            if fatal:
                self._unsafe = lane + "_reader_failed"

    def latest(self) -> StreamPressureSample:
        now = self._clock()
        with self._lock:
            api, gpu = self._api, self._gpu
            reasons = list(sorted(self._failures))
            if api is None or now - api[0] > self.binding.api_max_age_seconds:
                reasons.append("api_unavailable_or_stale")
            if gpu is None or now - gpu[0] > self.binding.gpu_max_age_seconds:
                reasons.append("gpu_unavailable_or_stale")
            self._sequence += 1
            # Partial startup has no joined observation time. Zero cannot
            # masquerade as fresh, and the first older GPU sample cannot make
            # a previously reported partial API timestamp regress.
            times = [api[0], gpu[0]] if api is not None and gpu is not None else []
            return StreamPressureSample(
                sequence=self._sequence, observed_monotonic=min(times, default=0.0),
                runtime_identity_sha256=self.binding.runtime_identity_sha256,
                owner_identity_sha256=self.binding.owner_sha256,
                evidence_sha256=_sha(_canonical({"api": None if api is None else api[-1], "gpu": None if gpu is None else gpu[-1], "failures": self._failures})),
                gpu_free_bytes=None if gpu is None else gpu[1],
                host_available_bytes=None if api is None else api[1],
                http_active=None if api is None else api[2], http_pending=None if api is None else api[3],
                unknown_reason=",".join(reasons) or None, unsafe_reason=self._unsafe,
            )

    @staticmethod
    def _bracket(started: float, finished: float) -> None:
        if not isfinite(started) or not isfinite(finished) or not 0 <= started <= finished:
            raise ValueError("pressure local collection bracket is invalid")


class StreamPressureSession:
    """Ordinary worker owns this context; a finite campaign may lend its cache."""
    def __init__(self, binding: PressureBinding, *, api_url: str, gpu_url: str,
                 evidence_sink: Callable[[Mapping[str, object]], None],
                 wakeup: Callable[[], None] = lambda: None,
                 request_timeout_seconds: float = 1.0,
                 client_factory: Callable[..., ThreadOwnedPersistentHTTPClient] = ThreadOwnedPersistentHTTPClient) -> None:
        if not 0 < request_timeout_seconds <= 2 or not callable(evidence_sink) or not callable(wakeup):
            raise ValueError("pressure reader bounds/callback invalid")
        self.cache = StreamPressureCache(binding)
        self._api_url, self._gpu_url = api_url, gpu_url
        self._sink, self._wakeup, self._timeout, self._factory = evidence_sink, wakeup, request_timeout_seconds, client_factory
        self._stop = threading.Event()
        self._updated = threading.Event()
        self._threads: list[threading.Thread] = []
        self._errors: list[BaseException] = []
        self._started = False

    def start(self) -> None:
        if self._started:
            raise RuntimeError("pressure readers are new-only")
        self._started = True
        try:
            for lane in ("api", "gpu"):
                thread = threading.Thread(target=self._read, args=(lane,), name="mineru-pressure-" + lane, daemon=False)
                thread.start()
                self._threads.append(thread)
        except BaseException as error:
            try:
                self.close()
            except BaseException as close_error:
                raise BaseExceptionGroup("pressure startup and cleanup failed", [error, close_error]) from error
            raise

    def _read(self, lane: Literal["api", "gpu"]) -> None:
        client = None
        try:
            url = self._api_url if lane == "api" else self._gpu_url
            parsed = urlsplit(url)
            if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                raise ValueError("pressure source URL is invalid")
            client = self._factory(f"{parsed.scheme}://{parsed.netloc}", maximum_response_bytes=65536)
            next_at = time.monotonic()
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    def read(path: str) -> bytes:
                        status, raw = client.get_bytes(path, timeout_seconds=self._timeout, transport_attempts=1)
                        if status != 200:
                            raise BoundedHTTPTransportError(f"pressure {lane} HTTP {status}")
                        return raw
                    if lane == "api":
                        health, pressure = read("/health"), read("/agent/telemetry/pressure/v1")
                        finished = time.monotonic()
                        self._sink({"lane": lane, "started": started, "finished": finished, "health": health.decode(), "pressure": pressure.decode()})
                        self.cache.publish_api(health, pressure, started=started, finished=finished)
                    else:
                        raw = read(parsed.path or "/")
                        finished = time.monotonic()
                        self._sink({"lane": lane, "started": started, "finished": finished, "metrics": raw.decode()})
                        self.cache.publish_gpu(raw, started=started, finished=finished, received_wall=time.time())
                except BoundedHTTPTransportError as error:
                    self._sink({"lane": lane, "started": started, "finished": time.monotonic(), "error": str(error)})
                    self.cache.record_failure(lane, error)
                self._updated.set()
                self._wakeup()
                now = time.monotonic()
                next_at += max(1, ceil(now - next_at))
                self._stop.wait(max(0.0, next_at - time.monotonic()))
        except BaseException as error:
            self.cache.record_failure(lane, error, fatal=True)
            self._errors.append(error)
            self._updated.set()
            self._wakeup()
        finally:
            if client is not None:
                try:
                    client.close()
                except BaseException as error:
                    self.cache.record_failure(lane, error, fatal=True)
                    self._errors.append(error)

    def wait_initial_sample(self, *, timeout_seconds: float = 10.0) -> None:
        """Bounded startup qualification, before any ordinary work is claimed."""
        if not self._started or not 0 < timeout_seconds <= 30:
            raise ValueError("pressure startup wait is invalid")
        deadline = time.monotonic() + timeout_seconds
        while True:
            self._updated.clear()
            sample = self.cache.latest()
            if sample.unsafe_reason is not None:
                raise RuntimeError("pressure startup failed: " + sample.unsafe_reason)
            if sample.unknown_reason is None:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("initial pressure sample unavailable: " + sample.unknown_reason)
            self._updated.wait(remaining)

    def close(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2 * self._timeout + 2)
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError("pressure reader cleanup unresolved")
        if self._errors:
            raise BaseExceptionGroup("pressure reader failures", self._errors)
