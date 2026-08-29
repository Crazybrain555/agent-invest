"""DB-free read-only API, vLLM and GPU capacity samplers."""

from __future__ import annotations

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
from disclosure_anchor.adapters.runtime.gpu_telemetry_freshness import (
    nvidia_smi_sample_age_seconds,
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
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.geturl() != url:
                raise ValueError("telemetry endpoint redirected")
            if response.headers.get_content_type() not in accepted_content_types:
                raise ValueError("telemetry response content type is invalid")
            payload = response.read(maximum_bytes + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError("telemetry endpoint unavailable") from exc
    if not isinstance(payload, bytes) or len(payload) > maximum_bytes:
        raise ValueError("telemetry response exceeds safety limit")
    return payload


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


def _api_values(payload: bytes, *, expected_task_slots: int) -> ApiSampleValues:
    decoded = parse_mineru_api_health(
        payload, expected_task_slots=expected_task_slots
    )
    return ApiSampleValues(
        queued_tasks=decoded["queued_tasks"],
        processing_tasks=decoded["processing_tasks"],
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


def _gpu_values(payload: bytes, *, expected_device_uuid: str) -> GpuSampleValues:
    samples = _prometheus(payload)
    has_nvidia = bool(_alias(samples, _NVIDIA_ALIASES["utilization"]))
    if not has_nvidia:
        raise ValueError("capacity observation requires the pinned nvidia-smi exporter")

    device_uuid = _nvidia_uuid(payload)
    normalized_expected = expected_device_uuid.lower().removeprefix("gpu-")
    if normalized_expected != device_uuid:
        raise ValueError("nvidia-smi GPU UUID differs from attestation")
    values = {
        name: _alias(samples, aliases) for name, aliases in _NVIDIA_ALIASES.items()
    }
    if values["success"] != (1.0,) or len(values["timestamp"]) != 1:
        raise ValueError("nvidia-smi exporter collection is unsuccessful")
    nvidia_smi_sample_age_seconds(
        now_timestamp=time.time(),
        success_timestamp=values["timestamp"][0],
    )
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
    return GpuSampleValues(
        exporter_family="nvidia_smi",
        device_count=1,
        device_identity_sha256=gpu_device_identity_sha256(device_uuid),
        gpu_utilization_pct=100 * utilization[0],
        framebuffer_used_bytes=round(used[0]),
        framebuffer_free_bytes=round(free[0]),
        framebuffer_total_bytes=round(total[0]),
        power_usage_watts=power[0],
        temperature_celsius=temperature[0],
    )


class MineruApiCapacitySampler:
    source = "api"
    cadence_seconds = 1.0

    def __init__(self, *, url: str, timeout_seconds: float, task_slots: int) -> None:
        self._url = _service_root(url) + "/health"
        self._timeout = timeout_seconds
        self._task_slots = task_slots

    def sample(self) -> ApiSampleValues:
        payload = _fetch_payload(
            self._url,
            timeout_seconds=self._timeout,
            accepted_content_types=frozenset({"application/json"}),
            maximum_bytes=MAX_API_HEALTH_BYTES,
        )
        return _api_values(payload, expected_task_slots=self._task_slots)


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
        payload = _fetch_payload(
            self._url,
            timeout_seconds=self._timeout,
            accepted_content_types=frozenset(
                {"application/openmetrics-text", "text/plain"}
            ),
            maximum_bytes=MAX_METRICS_BYTES,
        )
        return _gpu_values(
            payload,
            expected_device_uuid=self._expected_device_uuid,
        )


__all__ = [
    "GpuCapacitySampler",
    "MineruApiCapacitySampler",
    "VllmCapacitySampler",
    "gpu_device_identity_sha256",
]
