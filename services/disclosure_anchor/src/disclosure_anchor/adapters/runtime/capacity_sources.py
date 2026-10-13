"""DB-free read-only API, vLLM and GPU capacity samplers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import math
import re
import time
import urllib.error
import urllib.request
from typing import Any

from disclosure_anchor.application.contracts.capacity import (
    ApiSampleValues,
    GpuSampleValues,
    VllmSampleValues,
)
from disclosure_anchor.application.contracts.mineru_api_health import (
    parse_mineru_api_health,
)
from disclosure_anchor.application.contracts.mineru_capacity_config import AnyMineruCapacityConfig
from disclosure_anchor.application.contracts.mineru_capacity_health import parse_mineru_capacity_wire_health
from disclosure_anchor.adapters.runtime.bounded_http import BoundedHTTPResponse
from disclosure_anchor.adapters.runtime.gpu_telemetry_freshness import (
    EXPORTER_DATE_HEADER,
    NVIDIA_SMI_MAX_SAMPLE_AGE_SECONDS,
    GpuCollectionUnavailableError,
    GpuSampleClockUnorderedError,
    GpuSampleStaleError,
    GpuTelemetryUnavailable,
    exporter_response_date_seconds,
    exporter_sample_age_bound_seconds,
)


MAX_API_HEALTH_BYTES = 64 * 1024
MAX_METRICS_BYTES = 4 * 1024 * 1024
_VLLM_ALIASES = {
    "running": ("vllm:num_requests_running", "vllm_num_requests_running"),
    "waiting": ("vllm:num_requests_waiting", "vllm_num_requests_waiting"),
    "preemptions": ("vllm:num_preemptions_total", "vllm_num_preemptions_total"),
    "kv": (
        "vllm:gpu_cache_usage_perc",
        "vllm_gpu_cache_usage_perc",
        "vllm:kv_cache_usage_perc",
        "vllm_kv_cache_usage_perc",
    ),
}
_NVIDIA_ALIASES = {
    "utilization": ("nvidia_smi_utilization_gpu_ratio",),
    "used_bytes": ("nvidia_smi_memory_used_bytes",),
    "free_bytes": ("nvidia_smi_memory_free_bytes",),
    "total_bytes": ("nvidia_smi_memory_total_bytes",),
    "power": ("nvidia_smi_power_draw_watts",),
    "temperature": ("nvidia_smi_temperature_gpu",),
    "success": ("nvidia_smi_last_collect_success",),
    "timestamp": ("nvidia_smi_last_collect_success_timestamp_seconds",),
}
_NVIDIA_DEVICE_METRICS = frozenset(
    {
        "nvidia_smi_gpu_info",
        "nvidia_smi_utilization_gpu_ratio",
        "nvidia_smi_memory_used_bytes",
        "nvidia_smi_memory_free_bytes",
        "nvidia_smi_memory_total_bytes",
        "nvidia_smi_power_draw_watts",
        "nvidia_smi_temperature_gpu",
    }
)
_UUID_LABEL_RE = re.compile(r'(?:^|,)uuid="([^"\\]+)"(?:,|$)')
_INDEX_LABEL_RE = re.compile(r'(?:^|,)index="([^"\\]+)"(?:,|$)')
_NAME_LABEL_RE = re.compile(r'(?:^|,)name="([^"\\]+)"(?:,|$)')


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _fetch_payload(
    url: str,
    *,
    timeout_seconds: float,
    accepted_content_types: frozenset[str],
    maximum_bytes: int,
) -> bytes:
    return _fetch_response(
        url,
        timeout_seconds=timeout_seconds,
        accepted_content_types=accepted_content_types,
        maximum_bytes=maximum_bytes,
        capture_headers=(),
    ).body


def _fetch_response(
    url: str,
    *,
    timeout_seconds: float,
    accepted_content_types: frozenset[str],
    maximum_bytes: int,
    capture_headers: tuple[str, ...],
) -> BoundedHTTPResponse:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": ", ".join(sorted(accepted_content_types)),
            "User-Agent": "disclosure-anchor-capacity-observer/1",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.geturl() != url:
                raise ValueError("telemetry endpoint redirected")
            if response.headers.get_content_type() not in accepted_content_types:
                raise ValueError("telemetry response content type is invalid")
            captured = {
                name: tuple(response.headers.get_all(name) or ())
                for name in capture_headers
            }
            payload = response.read(maximum_bytes + 1)
            received = time.monotonic()
            status = response.status
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError("telemetry endpoint unavailable") from exc
    if not isinstance(payload, bytes) or len(payload) > maximum_bytes:
        raise ValueError("telemetry response exceeds safety limit")
    return BoundedHTTPResponse(
        status=status, body=payload, headers=captured, elapsed_seconds=received - started,
    )


def _service_root(url: str, *, remove_v1: bool = False) -> str:
    root = url.rstrip("/")
    if remove_v1 and root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    return root


def _prometheus(payload: bytes) -> dict[str, tuple[float, ...]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("metrics payload is not UTF-8") from exc
    values: dict[str, list[float]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            metric_name, _, labelled = line.partition("{")
            label_end = labelled.find("}")
            if label_end < 0:
                continue
            sample_parts = labelled[label_end + 1 :].split()
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            metric_name = parts[0]
            sample_parts = parts[1:]
        try:
            value = float(sample_parts[0])
        except (IndexError, ValueError):
            continue
        if math.isfinite(value):
            values.setdefault(metric_name, []).append(value)
    return {name: tuple(items) for name, items in values.items()}


def _alias(
    samples: dict[str, tuple[float, ...]], aliases: tuple[str, ...]
) -> tuple[float, ...]:
    for alias in aliases:
        values = samples.get(alias, ())
        if values:
            return values
    return ()


def _api_values(
    payload: bytes, *, expected_task_slots: int | None = None,
    expected_capacity: AnyMineruCapacityConfig | None = None,
) -> ApiSampleValues:
    if expected_capacity is None:
        decoded = parse_mineru_api_health(
            payload, expected_task_slots=expected_task_slots,
        )
        active = decoded["processing_tasks"]
    else:
        if expected_task_slots is not None:
            raise ValueError("explicit capacity cannot also use legacy task slots")
        health = parse_mineru_capacity_wire_health(payload, expected_capacity=expected_capacity)
        control = health["capacity_observation"]["owner_control"]
        if (any(control[field] for field in (
                "foreign_loop_observed", "soft_drain_requested", "soft_drain_applied"))
                or health["task_admission"]["blocked_reason"] not in (None, "capacity_full")):
            raise ValueError("MinerU capacity owner is draining or admission is closed")
        # The wire aggregate includes finalizers, which do not occupy N.
        active = health["capacity_observation"]["stage_counters"]["parse_active"]
        decoded = health
    return ApiSampleValues(
        queued_tasks=decoded["queued_tasks"],
        processing_tasks=active,
        completed_tasks_gauge=decoded["completed_tasks"],
        failed_tasks_gauge=decoded["failed_tasks"],
        task_slots=decoded["max_concurrent_requests"],
        max_pending_tasks_requested=decoded["max_pending_tasks_requested"],
        max_pending_tasks_effective=decoded["max_pending_tasks_effective"],
        processing_window_size=decoded["processing_window_size"],
        task_retention_seconds=decoded["task_retention_seconds"],
        task_cleanup_interval_seconds=decoded["task_cleanup_interval_seconds"],
        protocol_version=decoded["protocol_version"],
    )


def _vllm_values(payload: bytes) -> VllmSampleValues:
    samples = _prometheus(payload)
    running = _alias(samples, _VLLM_ALIASES["running"])
    waiting = _alias(samples, _VLLM_ALIASES["waiting"])
    if not running or not waiting:
        raise ValueError("vLLM metrics are missing running/waiting gauges")
    preemptions = _alias(samples, _VLLM_ALIASES["preemptions"])
    kv = _alias(samples, _VLLM_ALIASES["kv"])
    return VllmSampleValues(
        requests_running=int(sum(running)),
        requests_waiting=int(sum(waiting)),
        preemptions_total=int(sum(preemptions)) if preemptions else None,
        kv_cache_usage_ratio=max(kv) if kv else None,
    )


def _nvidia_uuid(payload: bytes) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("metrics payload is not UTF-8") from exc
    identities: dict[str, list[str]] = {
        metric_name: [] for metric_name in _NVIDIA_DEVICE_METRICS
    }
    gpu_name: str | None = None
    gpu_index: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        metric_name, separator, remainder = line.partition("{")
        if metric_name not in identities:
            continue
        label_end = remainder.find("}")
        if not separator or label_end < 0:
            raise ValueError("nvidia-smi device metric labels are invalid")
        labels = remainder[:label_end]
        uuid_match = _UUID_LABEL_RE.search(labels)
        if uuid_match is None:
            raise ValueError("nvidia-smi device metric is missing UUID")
        identities[metric_name].append(
            uuid_match.group(1).lower().removeprefix("gpu-")
        )
        if metric_name == "nvidia_smi_gpu_info":
            index_match = _INDEX_LABEL_RE.search(labels)
            name_match = _NAME_LABEL_RE.search(labels)
            gpu_index = index_match.group(1) if index_match else None
            gpu_name = name_match.group(1) if name_match else None
    if any(len(items) != 1 for items in identities.values()):
        raise ValueError("nvidia-smi metrics do not identify exactly one GPU")
    uuids = {items[0] for items in identities.values()}
    if len(uuids) != 1 or gpu_index != "0" or not gpu_name:
        raise ValueError("nvidia-smi GPU identity is inconsistent")
    return uuids.pop()


def gpu_device_identity_sha256(device_uuid: str) -> str:
    normalized = device_uuid.lower().removeprefix("gpu-")
    return "sha256:" + hashlib.sha256(normalized.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class _NvidiaExporterReading:
    """One validated pinned-exporter response, classified on its own host clock.

    ``success_timestamp`` is the exporter's last-success token (``None`` before
    its first success) and ``response_date_seconds`` its response Date.
    ``age_bound_seconds`` bounds the sample's age at complete local receipt.
    ``values`` exists only for a successful collection whose bound is inside the
    strict limit; otherwise ``unavailable`` carries the typed transient reason.
    """

    response_date_seconds: int
    success_timestamp: float | None
    age_bound_seconds: float | None
    values: GpuSampleValues | None
    unavailable: GpuTelemetryUnavailable | None


def _nvidia_device_series_present(payload: bytes) -> bool:
    for raw_line in payload.decode("utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and re.split(r"[{\s]", line, maxsplit=1)[0] in _NVIDIA_DEVICE_METRICS:
            return True
    return False


def _nvidia_exporter_reading(
    payload: bytes, *, expected_device_uuid: str, response_date: Sequence[str],
    transport_elapsed_seconds: float,
) -> _NvidiaExporterReading:
    samples = _prometheus(payload)
    success = _alias(samples, _NVIDIA_ALIASES["success"])
    timestamps = _alias(samples, _NVIDIA_ALIASES["timestamp"])
    if not success and not _alias(samples, _NVIDIA_ALIASES["utilization"]):
        raise ValueError("capacity observation requires the pinned nvidia-smi exporter")
    # The response Date is transport evidence of every pinned response. When it
    # is absent, repeated or malformed, no local reading time replaces it.
    response_date_seconds = exporter_response_date_seconds(response_date)
    if success not in ((0.0,), (1.0,)) or len(timestamps) > 1 or (timestamps and timestamps[0] <= 0):
        raise ValueError("nvidia-smi exporter collection status is invalid")
    token = timestamps[0] if timestamps else None
    if success == (0.0,) and not _nvidia_device_series_present(payload):
        # Upstream 1.14.0 renders only its always-present health families after
        # a failed collection, and no timestamp before the first success.
        if len(_alias(samples, ("nvidia_smi_failed_scrapes_total",))) != 1:
            raise ValueError("nvidia-smi exporter collection status is invalid")
        return _NvidiaExporterReading(
            response_date_seconds, token, None, None,
            GpuCollectionUnavailableError("nvidia-smi exporter collection is unsuccessful"),
        )
    if not _alias(samples, _NVIDIA_ALIASES["utilization"]):
        raise ValueError("capacity observation requires the pinned nvidia-smi exporter")

    device_uuid = _nvidia_uuid(payload)
    normalized_expected = expected_device_uuid.lower().removeprefix("gpu-")
    if normalized_expected != device_uuid:
        raise ValueError("nvidia-smi GPU UUID differs from attestation")
    values = {
        name: _alias(samples, aliases) for name, aliases in _NVIDIA_ALIASES.items()
    }
    if token is None:
        raise ValueError("nvidia-smi exporter collection status is invalid")
    utilization = values["utilization"]
    used = values["used_bytes"]
    free = values["free_bytes"]
    total = values["total_bytes"]
    power = values["power"]
    temperature = values["temperature"]
    if (
        len(utilization) != 1
        or not 0 <= utilization[0] <= 1
        or len(used) != 1
        or len(free) != 1
        or len(total) != 1
        or used[0] < 0
        or free[0] < 0
        or total[0] < 1024 * 1024 * 1024
        or used[0] > total[0]
        or free[0] > total[0]
        or abs(total[0] - used[0] - free[0]) > total[0] * 0.1
        or len(power) != 1
        or not 0 <= power[0] <= 1000
        or len(temperature) != 1
        or not -50 <= temperature[0] <= 150
    ):
        raise ValueError("nvidia-smi GPU measurements are invalid")
    # Missing or old evidence cannot hide a separate identity, format or
    # measurement violation, so those are validated before any transient class.
    if success == (0.0,):
        return _NvidiaExporterReading(
            response_date_seconds, token, None, None,
            GpuCollectionUnavailableError("nvidia-smi exporter collection is unsuccessful"),
        )
    try:
        age_bound = exporter_sample_age_bound_seconds(
            response_date_seconds=response_date_seconds, success_timestamp=token,
            transport_elapsed_seconds=transport_elapsed_seconds,
        )
    except GpuSampleClockUnorderedError as error:
        return _NvidiaExporterReading(response_date_seconds, token, None, None, error)
    if age_bound > NVIDIA_SMI_MAX_SAMPLE_AGE_SECONDS:
        return _NvidiaExporterReading(
            response_date_seconds, token, age_bound, None,
            GpuSampleStaleError("nvidia-smi exporter sample is stale"),
        )
    return _NvidiaExporterReading(response_date_seconds, token, age_bound, GpuSampleValues(
        exporter_family="nvidia_smi",
        device_count=1,
        device_identity_sha256=gpu_device_identity_sha256(device_uuid),
        gpu_utilization_pct=100 * utilization[0],
        framebuffer_used_bytes=round(used[0]),
        framebuffer_free_bytes=round(free[0]),
        framebuffer_total_bytes=round(total[0]),
        power_usage_watts=power[0],
        temperature_celsius=temperature[0],
    ), None)


def _gpu_values(
    payload: bytes, *, expected_device_uuid: str, response_date: Sequence[str],
    transport_elapsed_seconds: float,
) -> GpuSampleValues:
    """Strict one-shot sampler: every non-current state raises its typed error."""

    reading = _nvidia_exporter_reading(
        payload, expected_device_uuid=expected_device_uuid, response_date=response_date,
        transport_elapsed_seconds=transport_elapsed_seconds,
    )
    if reading.unavailable is not None:
        raise reading.unavailable
    assert reading.values is not None
    return reading.values


class MineruApiCapacitySampler:
    source = "api"
    cadence_seconds = 1.0

    def __init__(
        self, *, url: str, timeout_seconds: float, task_slots: int | None = None,
        expected_capacity: AnyMineruCapacityConfig | None = None,
    ) -> None:
        if expected_capacity is not None and task_slots is not None:
            raise ValueError("explicit capacity cannot also use legacy task slots")
        self._url = _service_root(url) + "/health"
        self._timeout = timeout_seconds
        self._task_slots = task_slots
        self._expected_capacity = expected_capacity

    def sample(self) -> ApiSampleValues:
        payload = _fetch_payload(
            self._url,
            timeout_seconds=self._timeout,
            accepted_content_types=frozenset({"application/json"}),
            maximum_bytes=MAX_API_HEALTH_BYTES,
        )
        return _api_values(
            payload, expected_task_slots=self._task_slots, expected_capacity=self._expected_capacity,
        )


class VllmCapacitySampler:
    source = "vllm"
    cadence_seconds = 1.0

    def __init__(self, *, url: str, timeout_seconds: float) -> None:
        self._url = _service_root(url, remove_v1=True) + "/metrics"
        self._timeout = timeout_seconds

    def sample(self) -> VllmSampleValues:
        payload = _fetch_payload(
            self._url,
            timeout_seconds=self._timeout,
            accepted_content_types=frozenset(
                {"application/openmetrics-text", "text/plain"}
            ),
            maximum_bytes=MAX_METRICS_BYTES,
        )
        return _vllm_values(payload)


class GpuCapacitySampler:
    source = "gpu"
    cadence_seconds = 1.0

    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float,
        expected_device_uuid: str,
    ) -> None:
        self._url = url
        self._timeout = timeout_seconds
        self._expected_device_uuid = expected_device_uuid

    def sample(self) -> GpuSampleValues:
        response = _fetch_response(
            self._url,
            timeout_seconds=self._timeout,
            accepted_content_types=frozenset(
                {"application/openmetrics-text", "text/plain"}
            ),
            maximum_bytes=MAX_METRICS_BYTES,
            capture_headers=(EXPORTER_DATE_HEADER,),
        )
        return _gpu_values(
            response.body,
            expected_device_uuid=self._expected_device_uuid,
            response_date=response.headers[EXPORTER_DATE_HEADER],
            transport_elapsed_seconds=response.elapsed_seconds,
        )


__all__ = [
    "GpuCapacitySampler",
    "MineruApiCapacitySampler",
    "VllmCapacitySampler",
    "gpu_device_identity_sha256",
]
