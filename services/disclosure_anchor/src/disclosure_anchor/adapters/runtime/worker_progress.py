"""Versioned worker progress snapshots for terminal, Agent, and future UI use."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine

from disclosure_anchor.application.dto.worker_report import WorkerReport
from disclosure_anchor.application.worker.queries import (
    worker_progress_database_snapshot,
)
from disclosure_anchor.settings import Settings


WORKER_PROGRESS_CONTRACT_VERSION = "worker_progress.v2"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MAX_METRICS_BYTES = 4 * 1024 * 1024
MAX_API_HEALTH_BYTES = 64 * 1024
MAX_GPU_METRICS_BYTES = 64 * 1024
MINERU_API_HEALTH_FIELDS = frozenset(
    {
        "status",
        "version",
        "protocol_version",
        "queued_tasks",
        "processing_tasks",
        "completed_tasks",
        "failed_tasks",
        "max_concurrent_requests",
        "processing_window_size",
        "task_retention_seconds",
        "task_cleanup_interval_seconds",
    }
)
VLLM_METRIC_NAMES = {
    "requests_running": ("vllm:num_requests_running", "vllm_num_requests_running"),
    "requests_waiting": ("vllm:num_requests_waiting", "vllm_num_requests_waiting"),
    "preemptions_total": ("vllm:num_preemptions_total", "vllm_num_preemptions_total"),
    "kv_cache_usage_ratio": (
        "vllm:gpu_cache_usage_perc",
        "vllm_gpu_cache_usage_perc",
        "vllm:kv_cache_usage_perc",
        "vllm_kv_cache_usage_perc",
    ),
}
DCGM_METRIC_NAMES = {
    "gpu_utilization_pct": ("DCGM_FI_DEV_GPU_UTIL",),
    "framebuffer_used_mib": ("DCGM_FI_DEV_FB_USED",),
    "framebuffer_free_mib": ("DCGM_FI_DEV_FB_FREE",),
    "power_usage_watts": ("DCGM_FI_DEV_POWER_USAGE",),
}
NVIDIA_SMI_METRIC_NAMES = {
    "gpu_utilization_ratio": ("nvidia_smi_utilization_gpu_ratio",),
    "framebuffer_used_bytes": ("nvidia_smi_memory_used_bytes",),
    "framebuffer_free_bytes": ("nvidia_smi_memory_free_bytes",),
    "framebuffer_total_bytes": ("nvidia_smi_memory_total_bytes",),
    "power_usage_watts": ("nvidia_smi_power_draw_watts",),
    "temperature_celsius": ("nvidia_smi_temperature_gpu",),
    "last_collect_success": ("nvidia_smi_last_collect_success",),
    "last_collect_success_timestamp": (
        "nvidia_smi_last_collect_success_timestamp_seconds",
    ),
}
NVIDIA_SMI_MAX_SAMPLE_AGE_SECONDS = 30
NVIDIA_SMI_DEVICE_METRICS = (
    "nvidia_smi_gpu_info",
    "nvidia_smi_utilization_gpu_ratio",
    "nvidia_smi_memory_used_bytes",
    "nvidia_smi_memory_free_bytes",
    "nvidia_smi_memory_total_bytes",
    "nvidia_smi_power_draw_watts",
    "nvidia_smi_temperature_gpu",
)
PROMETHEUS_UUID_LABEL_RE = re.compile(r'(?:^|,)uuid="([^"\\]+)"(?:,|$)')
PROMETHEUS_INDEX_LABEL_RE = re.compile(r'(?:^|,)index="([^"\\]+)"(?:,|$)')
PROMETHEUS_NAME_LABEL_RE = re.compile(r'(?:^|,)name="([^"\\]+)"(?:,|$)')
WORKER_PROGRESS_PRODUCER_ID = uuid.uuid4().hex
_SEQUENCE_LOCK = threading.Lock()
_SEQUENCE = 0


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep loopback telemetry probes on their configured endpoint."""

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
    accept: str,
    maximum_bytes: int,
) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"Accept": accept, "User-Agent": "disclosure-anchor-progress/2"},
    )
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.geturl() != url:
                raise RuntimeError("telemetry endpoint redirected")
            content_type = response.headers.get_content_type()
            accepted_types = (
                {"application/json"}
                if accept == "application/json"
                else {"text/plain", "application/openmetrics-text"}
            )
            if content_type not in accepted_types:
                raise RuntimeError("telemetry response content type is invalid")
            payload = response.read(maximum_bytes + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError("telemetry endpoint unavailable") from exc
    if not isinstance(payload, bytes) or len(payload) > maximum_bytes:
        raise RuntimeError("telemetry response exceeds safety limit")
    return payload


def _fetch_metrics(url: str, *, timeout_seconds: float) -> bytes:
    return _fetch_payload(
        url,
        timeout_seconds=timeout_seconds,
        accept="text/plain",
        maximum_bytes=MAX_METRICS_BYTES,
    )


def _fetch_api_health(url: str, *, timeout_seconds: float) -> bytes:
    return _fetch_payload(
        url,
        timeout_seconds=timeout_seconds,
        accept="application/json",
        maximum_bytes=MAX_API_HEALTH_BYTES,
    )


def _fetch_gpu_metrics(url: str, *, timeout_seconds: float) -> bytes:
    return _fetch_payload(
        url,
        timeout_seconds=timeout_seconds,
        accept="text/plain",
        maximum_bytes=MAX_GPU_METRICS_BYTES,
    )


def _normalized_service_root(url: str, *, remove_v1: bool = False) -> str:
    root = url.rstrip("/")
    if remove_v1 and root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    return root


def _mineru_api_health_url(url: str) -> str:
    return _normalized_service_root(url) + "/health"


def _vllm_metrics_url(url: str) -> str:
    return _normalized_service_root(url, remove_v1=True) + "/metrics"


def parse_prometheus_metrics(payload: bytes) -> dict[str, tuple[float, ...]]:
    """Parse finite numeric samples without depending on a Prometheus client."""

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
            metric_name, _, labelled_remainder = line.partition("{")
            label_end = labelled_remainder.find("}")
            if label_end < 0:
                continue
            sample_parts = labelled_remainder[label_end + 1 :].split()
        else:
            sample_parts = line.split()[1:]
            metric_name = line.split(None, 1)[0]
        if not metric_name or not sample_parts:
            continue
        try:
            value = float(sample_parts[0])
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        values.setdefault(metric_name, []).append(value)
    return {name: tuple(samples) for name, samples in values.items()}


def _alias_values(
    samples: dict[str, tuple[float, ...]], aliases: tuple[str, ...]
) -> tuple[float, ...]:
    for alias in aliases:
        values = samples.get(alias, ())
        if values:
            return values
    return ()


def _nvidia_smi_device_identity(payload: bytes) -> tuple[str, str]:
    """Require every device metric to bind to one identical UUID."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("metrics payload is not UTF-8") from exc
    identities: dict[str, list[str]] = {
        metric_name: [] for metric_name in NVIDIA_SMI_DEVICE_METRICS
    }
    gpu_name: str | None = None
    gpu_index: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        metric_name, separator, remainder = line.partition("{")
        if metric_name not in identities:
            continue
        label_end = remainder.find("}")
        if not separator or label_end < 0 or not remainder[label_end + 1 :].lstrip():
            raise ValueError("nvidia-smi device metric is missing closed labels")
        label_text = remainder[:label_end]
        uuid_match = PROMETHEUS_UUID_LABEL_RE.search(label_text)
        if uuid_match is None:
            raise ValueError("nvidia-smi device metric is missing UUID label")
        identities[metric_name].append(
            uuid_match.group(1).lower().removeprefix("gpu-")
        )
        if metric_name == "nvidia_smi_gpu_info":
            name_match = PROMETHEUS_NAME_LABEL_RE.search(label_text)
            index_match = PROMETHEUS_INDEX_LABEL_RE.search(label_text)
            if name_match is None or index_match is None:
                raise ValueError("nvidia-smi GPU identity labels are incomplete")
            gpu_name = name_match.group(1)
            gpu_index = index_match.group(1)
    if any(len(values) != 1 for values in identities.values()):
        raise ValueError("nvidia-smi device metrics do not identify exactly one GPU")
    uuids = {values[0] for values in identities.values()}
    if len(uuids) != 1 or gpu_index != "0" or not gpu_name:
        raise ValueError("nvidia-smi device identity is inconsistent")
    return uuids.pop(), gpu_name


def _next_sequence() -> int:
    global _SEQUENCE
    with _SEQUENCE_LOCK:
        _SEQUENCE += 1
        return _SEQUENCE


def vllm_metrics_snapshot(payload: bytes) -> dict[str, Any]:
    samples = parse_prometheus_metrics(payload)
    running = _alias_values(samples, VLLM_METRIC_NAMES["requests_running"])
    waiting = _alias_values(samples, VLLM_METRIC_NAMES["requests_waiting"])
    if not running or not waiting:
        raise ValueError("vLLM metrics are missing running/waiting gauges")
    preemptions = _alias_values(samples, VLLM_METRIC_NAMES["preemptions_total"])
    kv_usage = _alias_values(samples, VLLM_METRIC_NAMES["kv_cache_usage_ratio"])
    return {
        "status": "available",
        "source": "vllm_metrics",
        "requests_running": int(sum(running)),
        "requests_waiting": int(sum(waiting)),
        "preemptions_total": int(sum(preemptions)) if preemptions else None,
        "kv_cache_usage_ratio_max": max(kv_usage) if kv_usage else None,
    }


def mineru_api_health_snapshot(payload: bytes) -> dict[str, Any]:
    """Parse the exact MinerU 3.4.4 orchestration health contract."""

    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("MinerU API health payload is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict) or set(decoded) != MINERU_API_HEALTH_FIELDS:
        raise ValueError("MinerU API health fields are not closed")
    if (
        decoded.get("status") != "healthy"
        or decoded.get("version") != "3.4.4"
        or decoded.get("protocol_version") != 2
        or decoded.get("max_concurrent_requests") != 3
        or decoded.get("processing_window_size") != 16
        or decoded.get("task_retention_seconds") != 600
        or decoded.get("task_cleanup_interval_seconds") != 30
    ):
        raise ValueError("MinerU API identity or health status drifted")

    nonnegative_fields = (
        "queued_tasks",
        "processing_tasks",
        "completed_tasks",
        "failed_tasks",
        "task_retention_seconds",
    )
    positive_fields = (
        "max_concurrent_requests",
        "processing_window_size",
        "task_cleanup_interval_seconds",
    )
    if any(
        isinstance(decoded.get(name), bool)
        or not isinstance(decoded.get(name), int)
        or decoded[name] < 0
        for name in nonnegative_fields
    ) or any(
        isinstance(decoded.get(name), bool)
        or not isinstance(decoded.get(name), int)
        or decoded[name] < 1
        for name in positive_fields
    ):
        raise ValueError("MinerU API health counters or limits are invalid")
    if (
        decoded["processing_tasks"] > decoded["max_concurrent_requests"]
        or decoded["queued_tasks"] + decoded["processing_tasks"]
        > decoded["processing_window_size"]
    ):
        raise ValueError("MinerU API health counters exceed declared limits")
    return {
        "status": "available",
        "source": "mineru_api_health",
        "health_status": decoded["status"],
        "version": decoded["version"],
        "protocol_version": decoded["protocol_version"],
        **{name: decoded[name] for name in (*nonnegative_fields, *positive_fields)},
    }


def dcgm_metrics_snapshot(payload: bytes) -> dict[str, Any]:
    samples = parse_prometheus_metrics(payload)
    values = {
        name: _alias_values(samples, aliases)
        for name, aliases in DCGM_METRIC_NAMES.items()
    }
    utilization = values["gpu_utilization_pct"]
    if not utilization:
        raise ValueError("DCGM metrics are missing GPU utilization")
    result: dict[str, Any] = {
        "status": "available",
        "source": "nvidia_dcgm_exporter",
        "device_count": len(utilization),
        "gpu_utilization_pct_mean": round(sum(utilization) / len(utilization), 3),
        "gpu_utilization_pct_max": max(utilization),
    }
    for name in ("framebuffer_used_mib", "framebuffer_free_mib", "power_usage_watts"):
        metric_values = values[name]
        result[f"{name}_total"] = sum(metric_values) if metric_values else None
    return result


def nvidia_smi_metrics_snapshot(
    payload: bytes,
    *,
    now_timestamp: float | None = None,
    expected_device_uuid: str | None = None,
) -> dict[str, Any]:
    """Parse one fresh, successful, single-GPU Windows exporter snapshot."""

    samples = parse_prometheus_metrics(payload)
    device_uuid, device_name = _nvidia_smi_device_identity(payload)
    normalized_expected_uuid = (
        expected_device_uuid.lower().removeprefix("gpu-")
        if expected_device_uuid is not None
        else None
    )
    if normalized_expected_uuid is not None and device_uuid != normalized_expected_uuid:
        raise ValueError("nvidia-smi exporter GPU UUID differs from attestation")
    values = {
        name: _alias_values(samples, aliases)
        for name, aliases in NVIDIA_SMI_METRIC_NAMES.items()
    }
    success = values["last_collect_success"]
    timestamps = values["last_collect_success_timestamp"]
    utilization = values["gpu_utilization_ratio"]
    if success != (1.0,):
        raise ValueError("nvidia-smi exporter collection is not successful")
    if len(timestamps) != 1:
        raise ValueError("nvidia-smi exporter collection timestamp is missing")
    sample_age = (now_timestamp if now_timestamp is not None else time.time()) - timestamps[0]
    if sample_age < 0 or sample_age > NVIDIA_SMI_MAX_SAMPLE_AGE_SECONDS:
        raise ValueError("nvidia-smi exporter sample is stale")
    if len(utilization) != 1 or not 0 <= utilization[0] <= 1:
        raise ValueError("nvidia-smi exporter GPU identity or utilization is invalid")

    used = values["framebuffer_used_bytes"]
    free = values["framebuffer_free_bytes"]
    total = values["framebuffer_total_bytes"]
    power = values["power_usage_watts"]
    temperature = values["temperature_celsius"]
    if (
        len(used) != 1
        or len(free) != 1
        or used[0] < 0
        or free[0] < 0
        or used[0] + free[0] <= 0
        or len(total) != 1
        or total[0] < 1024 * 1024 * 1024
        or used[0] > total[0]
        or free[0] > total[0]
        or abs(total[0] - used[0] - free[0]) > total[0] * 0.1
        or len(power) != 1
        or not 0 <= power[0] <= 1000
        or len(temperature) != 1
        or not -50 <= temperature[0] <= 150
    ):
        raise ValueError("nvidia-smi exporter GPU measurements are invalid")
    return {
        "status": "available",
        "source": "nvidia_smi_exporter",
        "device_count": 1,
        "device_uuid": device_uuid,
        "device_name": device_name,
        "sample_age_seconds": round(sample_age, 3),
        "gpu_utilization_pct_mean": round(100 * utilization[0], 3),
        "gpu_utilization_pct_max": 100 * utilization[0],
        "framebuffer_used_mib_total": round(used[0] / (1024 * 1024), 3),
        "framebuffer_free_mib_total": round(free[0] / (1024 * 1024), 3),
        "framebuffer_total_mib": round(total[0] / (1024 * 1024), 3),
        "power_usage_watts_total": power[0],
        "temperature_celsius_max": temperature[0],
    }


def gpu_metrics_snapshot(
    payload: bytes,
    *,
    expected_device_uuid: str | None = None,
) -> dict[str, Any]:
    """Detect exactly one supported GPU exporter family."""

    samples = parse_prometheus_metrics(payload)
    has_dcgm = bool(_alias_values(samples, DCGM_METRIC_NAMES["gpu_utilization_pct"]))
    has_nvidia_smi = bool(
        _alias_values(
            samples,
            NVIDIA_SMI_METRIC_NAMES["gpu_utilization_ratio"],
        )
    )
    if has_dcgm == has_nvidia_smi:
        raise ValueError("GPU metrics identify zero or multiple exporter families")
    if has_dcgm:
        return dcgm_metrics_snapshot(payload)
    return nvidia_smi_metrics_snapshot(
        payload,
        expected_device_uuid=expected_device_uuid,
    )


def _probe_snapshot(
    *,
    url: str | None,
    parser: Callable[[bytes], dict[str, Any]],
    timeout_seconds: float,
    source: str,
    contract_failure_reason: str = "metric_contract_unsatisfied",
    fetcher: Callable[..., bytes] | None = None,
) -> dict[str, Any]:
    if url is None:
        return {"status": "unavailable", "source": source, "reason": "not_configured"}
    resolved_fetcher = fetcher or _fetch_metrics
    try:
        payload = resolved_fetcher(url, timeout_seconds=timeout_seconds)
    except RuntimeError:
        return {
            "status": "unavailable",
            "source": source,
            "reason": "endpoint_unreachable",
        }
    try:
        return parser(payload)
    except ValueError:
        return {
            "status": "unavailable",
            "source": source,
            "reason": contract_failure_reason,
        }


def collect_worker_progress(
    *,
    settings: Settings,
    engine: Engine,
    scope_classes: tuple[str, ...] | None,
    report: WorkerReport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Collect one exact DB snapshot plus best-effort labelled telemetry."""

    observed_at = now()
    with engine.connect() as raw_connection:
        connection = raw_connection.execution_options(
            isolation_level="REPEATABLE READ"
        )
        with connection.begin():
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            database = worker_progress_database_snapshot(
                connection,
                max_download_retries=settings.cninfo_max_retries,
                max_parse_retries=settings.disclosure_max_parse_retries,
                max_build_retries=settings.disclosure_max_build_retries,
                scope_classes=scope_classes,
            )
    api_url = getattr(settings, "disclosure_mineru_api_url", None)
    observability_url = getattr(
        settings,
        "disclosure_mineru_observability_url",
        None,
    )
    api_health_url = _mineru_api_health_url(api_url) if api_url else None
    vllm_url = _vllm_metrics_url(observability_url) if observability_url else None
    sequence = _next_sequence()
    gpu_metrics_url = (
        settings.disclosure_gpu_metrics_url
        or settings.disclosure_dcgm_metrics_url
    )
    return {
        "contract_version": WORKER_PROGRESS_CONTRACT_VERSION,
        "event_id": f"{WORKER_PROGRESS_PRODUCER_ID}:{sequence}",
        "producer_instance_id": WORKER_PROGRESS_PRODUCER_ID,
        "sequence": sequence,
        "observed_at": observed_at.isoformat(),
        **database,
        "latest_interval": report.as_dict() if report is not None else None,
        "orchestration": _probe_snapshot(
            url=api_health_url,
            parser=mineru_api_health_snapshot,
            timeout_seconds=settings.worker_progress_metrics_timeout_seconds,
            source="mineru_api_health",
            contract_failure_reason="api_contract_unsatisfied",
            fetcher=_fetch_api_health,
        ),
        "inference": _probe_snapshot(
            url=vllm_url,
            parser=vllm_metrics_snapshot,
            timeout_seconds=settings.worker_progress_metrics_timeout_seconds,
            source="vllm_metrics",
        ),
        "gpu": _probe_snapshot(
            url=gpu_metrics_url,
            parser=lambda payload: gpu_metrics_snapshot(
                payload,
                expected_device_uuid=settings.disclosure_gpu_expected_uuid,
            ),
            timeout_seconds=settings.worker_progress_metrics_timeout_seconds,
            source="nvidia_gpu_metrics",
            fetcher=_fetch_gpu_metrics,
        ),
    }


def worker_progress_path(settings: Settings, observed_at: datetime) -> Path:
    day = observed_at.astimezone(SHANGHAI_TZ).date().isoformat()
    return settings.disclosure_runtime_root / "reports" / "progress" / f"{day}.jsonl"


def append_worker_progress(settings: Settings, event: dict[str, Any]) -> Path:
    observed_at = datetime.fromisoformat(str(event["observed_at"]))
    path = worker_progress_path(settings, observed_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        # The creation mode is private from the first byte. Tighten an existing
        # file too, before appending any new operational evidence.
        os.fchmod(descriptor, 0o600)
        with os.fdopen(
            descriptor,
            "a",
            encoding="utf-8",
            closefd=False,
        ) as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def _progress_bar(done: int, total: int, *, width: int = 20) -> str:
    if total <= 0:
        return "[" + "-" * width + "]"
    filled = round(width * min(1.0, max(0.0, done / total)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def render_worker_progress(event: dict[str, Any]) -> str:
    universe = event["universe"]
    documents = event["documents"]
    queues = event["queues"]
    synced = int(universe["synced_companies"])
    active = int(universe["active_companies"])
    published = int(documents["published_documents"])
    known = int(documents["known_process_documents"])
    lines = [
        f"[{event['observed_at']}] companies {_progress_bar(synced, active)} "
        f"{synced}/{active} synced | documents {_progress_bar(published, known)} "
        f"{published}/{known} published (dynamic total)",
        "queues "
        f"download={queues['pending_download']} parse={queues['pending_parse']} "
        f"build={queues['pending_build']} publish={queues['pending_publish']} | "
        "dead letters "
        f"download={queues['download_dead_letters']} "
        f"parse={queues['parse_dead_letters']} build={queues['build_dead_letters']}",
    ]
    orchestration = event["orchestration"]
    if orchestration["status"] == "available":
        orchestration_label = (
            f"MinerU API queued={orchestration['queued_tasks']} "
            f"processing={orchestration['processing_tasks']} "
            f"completed={orchestration['completed_tasks']} "
            f"failed={orchestration['failed_tasks']} "
            f"cap={orchestration['max_concurrent_requests']} "
            f"window={orchestration['processing_window_size']}"
        )
    else:
        orchestration_label = (
            f"MinerU API unavailable ({orchestration['reason']})"
        )
    inference = event["inference"]
    if inference["status"] == "available":
        kv = inference.get("kv_cache_usage_ratio_max")
        kv_label = f"{100 * float(kv):.1f}%" if kv is not None else "n/a"
        inference_label = (
            f"vLLM running={inference['requests_running']} "
            f"waiting={inference['requests_waiting']} KV={kv_label}"
        )
    else:
        inference_label = f"vLLM unavailable ({inference['reason']})"
    gpu = event["gpu"]
    if gpu["status"] == "available":
        source_label = {
            "nvidia_dcgm_exporter": "DCGM",
            "nvidia_smi_exporter": "nvidia-smi",
        }.get(str(gpu.get("source")), "exporter")
        used = gpu.get("framebuffer_used_mib_total")
        free = gpu.get("framebuffer_free_mib_total")
        total = gpu.get("framebuffer_total_mib")
        memory_label = ""
        if used is not None and free is not None:
            display_total = (
                float(total)
                if total is not None
                else float(used) + float(free)
            )
            memory_label = (
                f" VRAM={float(used):.0f}/{display_total:.0f}MiB"
            )
        power = gpu.get("power_usage_watts_total")
        temperature = gpu.get("temperature_celsius_max")
        power_label = f" power={float(power):.1f}W" if power is not None else ""
        temperature_label = (
            f" temp={float(temperature):.0f}C" if temperature is not None else ""
        )
        gpu_label = (
            f"GPU/{source_label} util="
            f"{float(gpu['gpu_utilization_pct_mean']):.1f}% mean/"
            f"{float(gpu['gpu_utilization_pct_max']):.1f}% max"
            + memory_label
            + power_label
            + temperature_label
        )
    else:
        gpu_label = f"GPU exporter unavailable ({gpu['reason']})"
    lines.append(orchestration_label + " | " + inference_label + " | " + gpu_label)
    current = event["current_work"]
    if current:
        labels = [
            f"{item['stage']}:{item['security_code'] or item['document_id']}"
            for item in current[:8]
        ]
        suffix = f" +{len(current) - 8}" if len(current) > 8 else ""
        lines.append("current " + ", ".join(labels) + suffix)
    interval = event.get("latest_interval")
    if interval is not None:
        lines.append(
            "last interval "
            f"sync={interval['synced_companies']} download={interval['downloaded']} "
            f"parse={interval['parsed']} build={interval['built']} "
            f"publish={interval['published']} failed={interval['failed']}"
        )
        admission = interval.get("admission")
        if admission is not None:
            admission_label = f"admission={admission['status']}"
            if admission["consecutive_failures"]:
                admission_label += (
                    " consecutive_failures="
                    f"{admission['consecutive_failures']}"
                )
            if admission["next_probe_at"] is not None:
                admission_label += f" next_probe={admission['next_probe_at']}"
            if admission["reason"] is not None:
                admission_label += f" reason={admission['reason']}"
            lines.append(admission_label)
    return "\n".join(lines)


def render_worker_progress_json(event: dict[str, Any]) -> str:
    return json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
