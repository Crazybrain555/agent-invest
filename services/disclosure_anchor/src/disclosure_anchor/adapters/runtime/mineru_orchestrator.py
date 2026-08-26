"""Strict health and drain fence for the dedicated MinerU API."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import threading
import time
from typing import Any
import urllib.error
import urllib.request


MINERU_API_VERSION = "3.4.4"
MINERU_API_PROTOCOL_VERSION = 2
MINERU_API_DEFAULT_TASK_SLOTS = 1
MINERU_API_MAX_SUPPORTED_TASK_SLOTS = 3
MINERU_API_INFERENCE_CONCURRENCY = 7
MINERU_API_PROCESSING_WINDOW_SIZE = 16
MINERU_API_TASK_RETENTION_SECONDS = 600
MINERU_API_CLEANUP_INTERVAL_SECONDS = 30
MAX_HEALTH_BYTES = 64 * 1024
_RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_INCIDENT_LOCK = threading.Lock()
_INCIDENT_GENERATION = 0
_INCIDENT_DRAINS_IN_PROGRESS: set[int] = set()


class MinerUOrchestratorError(RuntimeError):
    """The dedicated API cannot prove its fixed health contract."""


class MinerUOrchestratorUnavailableError(MinerUOrchestratorError):
    """The dedicated API endpoint was temporarily unreachable."""


@dataclass(frozen=True)
class MinerUOrchestratorIncidentState:
    generation: int
    drains_in_progress: int


def mark_mineru_orchestrator_incident() -> int:
    """Invalidate already-composed admission checkers before waiting to drain.

    The fixed API has no cancellation endpoint. A failing client must publish
    the incident before it blocks on natural drain; otherwise another completed
    Future can refill the shared API queue and prevent the drain from ever
    reaching zero. The generation is process-local coordination, not durable
    runtime evidence. A newly composed checker must still prove live idle state.
    """

    global _INCIDENT_GENERATION
    with _INCIDENT_LOCK:
        _INCIDENT_GENERATION += 1
        token = _INCIDENT_GENERATION
        _INCIDENT_DRAINS_IN_PROGRESS.add(token)
        return token


def finish_mineru_orchestrator_incident(token: int) -> None:
    """Release one drain owner without claiming that the API is healthy."""

    with _INCIDENT_LOCK:
        if token not in _INCIDENT_DRAINS_IN_PROGRESS:
            raise ValueError("unknown or already-finished MinerU incident token")
        _INCIDENT_DRAINS_IN_PROGRESS.remove(token)


def mineru_orchestrator_incident_generation() -> int:
    with _INCIDENT_LOCK:
        return _INCIDENT_GENERATION


def mineru_orchestrator_incident_state() -> MinerUOrchestratorIncidentState:
    with _INCIDENT_LOCK:
        return MinerUOrchestratorIncidentState(
            generation=_INCIDENT_GENERATION,
            drains_in_progress=len(_INCIDENT_DRAINS_IN_PROGRESS),
        )


@dataclass(frozen=True)
class MinerUOrchestratorHealth:
    status: str
    version: str
    protocol_version: int
    queued_tasks: int
    processing_tasks: int
    completed_tasks: int
    failed_tasks: int
    max_concurrent_requests: int
    processing_window_size: int
    task_retention_seconds: int
    task_cleanup_interval_seconds: int

    @property
    def active_tasks(self) -> int:
        return self.queued_tasks + self.processing_tasks

    def as_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


def fetch_mineru_orchestrator_health(
    api_url: str,
    *,
    timeout_seconds: float = 15.0,
    expected_task_slots: int | None = None,
    expected_task_retention_seconds: int | None = MINERU_API_TASK_RETENTION_SECONDS,
    expected_cleanup_interval_seconds: int | None = MINERU_API_CLEANUP_INTERVAL_SECONDS,
) -> MinerUOrchestratorHealth:
    """Fetch one bounded strict `/health` sample without proxy inheritance."""

    url = api_url.rstrip("/") + "/health"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "disclosure-anchor/1"},
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            payload = response.read(MAX_HEALTH_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in _RETRYABLE_HTTP_STATUS_CODES:
            raise MinerUOrchestratorUnavailableError(
                "MinerU API health endpoint temporarily rejected the probe"
            ) from exc
        raise MinerUOrchestratorError(
            f"MinerU API health endpoint returned non-retryable HTTP {exc.code}"
        ) from exc
    except (
        OSError,
        TimeoutError,
        http.client.HTTPException,
        urllib.error.URLError,
    ) as exc:
        raise MinerUOrchestratorUnavailableError(
            "MinerU API health endpoint unavailable"
        ) from exc
    if len(payload) > MAX_HEALTH_BYTES:
        raise MinerUOrchestratorError("MinerU API health response exceeds safety limit")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinerUOrchestratorError("MinerU API health response is not JSON") from exc
    if not isinstance(decoded, dict):
        raise MinerUOrchestratorError("MinerU API health root must be an object")
    required = {
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
    if set(decoded) != required:
        raise MinerUOrchestratorError("MinerU API health fields drifted")
    for field in required - {"status", "version"}:
        value = decoded[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MinerUOrchestratorError(f"MinerU API health {field} is invalid")
    health = MinerUOrchestratorHealth(**decoded)
    if health.status != "healthy":
        raise MinerUOrchestratorError("MinerU API reported unhealthy")
    if health.version != MINERU_API_VERSION:
        raise MinerUOrchestratorError("MinerU API version drifted")
    if health.protocol_version != MINERU_API_PROTOCOL_VERSION:
        raise MinerUOrchestratorError("MinerU API protocol drifted")
    if not (1 <= health.max_concurrent_requests <= MINERU_API_MAX_SUPPORTED_TASK_SLOTS):
        raise MinerUOrchestratorError("MinerU API task-slot limit is unsupported")
    if (
        expected_task_slots is not None
        and health.max_concurrent_requests != expected_task_slots
    ):
        raise MinerUOrchestratorError("MinerU API task-slot limit drifted")
    if health.processing_window_size != MINERU_API_PROCESSING_WINDOW_SIZE:
        raise MinerUOrchestratorError("MinerU API processing window drifted")
    if health.processing_tasks > health.max_concurrent_requests:
        raise MinerUOrchestratorError(
            "MinerU API processing count exceeds its declared limit"
        )
    if health.active_tasks > health.processing_window_size:
        raise MinerUOrchestratorError(
            "MinerU API active task count exceeds its declared window"
        )
    if (
        expected_task_retention_seconds is not None
        and health.task_retention_seconds != expected_task_retention_seconds
    ):
        raise MinerUOrchestratorError("MinerU API task retention drifted")
    if (
        expected_cleanup_interval_seconds is not None
        and health.task_cleanup_interval_seconds != expected_cleanup_interval_seconds
    ):
        raise MinerUOrchestratorError("MinerU API cleanup interval drifted")
    return health


def wait_for_mineru_orchestrator_idle(
    api_url: str,
    *,
    timeout_seconds: float,
    poll_seconds: float = 1.0,
    expected_task_slots: int | None = None,
    expected_task_retention_seconds: int | None = MINERU_API_TASK_RETENTION_SECONDS,
    expected_cleanup_interval_seconds: int | None = MINERU_API_CLEANUP_INTERVAL_SECONDS,
) -> tuple[MinerUOrchestratorHealth, float]:
    """Wait for natural drain; this is deliberately not called cancellation."""

    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("MinerU API drain timing must be positive")
    started = time.monotonic()
    deadline = started + timeout_seconds
    while True:
        health = fetch_mineru_orchestrator_health(
            api_url,
            timeout_seconds=min(15.0, max(0.1, deadline - time.monotonic())),
            expected_task_slots=expected_task_slots,
            expected_task_retention_seconds=expected_task_retention_seconds,
            expected_cleanup_interval_seconds=expected_cleanup_interval_seconds,
        )
        if health.active_tasks == 0:
            return health, time.monotonic() - started
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MinerUOrchestratorError(
                "MinerU API did not drain queued/processing tasks before deadline"
            )
        time.sleep(min(poll_seconds, remaining))


__all__ = [
    "MINERU_API_INFERENCE_CONCURRENCY",
    "MINERU_API_PROCESSING_WINDOW_SIZE",
    "MINERU_API_PROTOCOL_VERSION",
    "MINERU_API_DEFAULT_TASK_SLOTS",
    "MINERU_API_MAX_SUPPORTED_TASK_SLOTS",
    "MINERU_API_TASK_RETENTION_SECONDS",
    "MINERU_API_CLEANUP_INTERVAL_SECONDS",
    "MINERU_API_VERSION",
    "MinerUOrchestratorError",
    "MinerUOrchestratorHealth",
    "MinerUOrchestratorIncidentState",
    "MinerUOrchestratorUnavailableError",
    "fetch_mineru_orchestrator_health",
    "finish_mineru_orchestrator_incident",
    "mark_mineru_orchestrator_incident",
    "mineru_orchestrator_incident_generation",
    "mineru_orchestrator_incident_state",
    "wait_for_mineru_orchestrator_idle",
]
