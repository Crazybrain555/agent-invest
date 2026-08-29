"""Strict health and drain fence for the dedicated MinerU API."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any

from disclosure_anchor.adapters.runtime.bounded_http import (
    BoundedHTTPProtocolError,
    BoundedHTTPTransportError,
    ThreadOwnedPersistentHTTPClient,
)
from disclosure_anchor.application.contracts.mineru_api_health import (
    parse_mineru_api_health,
    validate_mineru_api_health,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_PROCESSING_WINDOW_SIZE,
)


MINERU_API_VERSION = "3.4.4"
MINERU_API_PROTOCOL_VERSION = 2
MINERU_API_DEFAULT_TASK_SLOTS = 1
MINERU_API_INFERENCE_CONCURRENCY = 7
MINERU_API_PROCESSING_WINDOW_SIZE = MINERU_PROCESSING_WINDOW_SIZE
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


class MinerUOrchestratorHealthClient:
    """Thread-owned persistent client for strict MinerU health samples."""

    def __init__(self, api_url: str) -> None:
        self._transport = ThreadOwnedPersistentHTTPClient(
            api_url.rstrip("/"),
            maximum_response_bytes=MAX_HEALTH_BYTES,
        )

    def fetch(
        self,
        *,
        timeout_seconds: float = 15.0,
        expected_task_slots: int | None = MINERU_API_DEFAULT_TASK_SLOTS,
        expected_task_retention_seconds: int | None = (
            MINERU_API_TASK_RETENTION_SECONDS
        ),
        expected_cleanup_interval_seconds: int | None = (
            MINERU_API_CLEANUP_INTERVAL_SECONDS
        ),
    ) -> "MinerUOrchestratorHealth":
        try:
            status, payload = self._transport.get_bytes(
                "/health",
                timeout_seconds=timeout_seconds,
                transport_attempts=1,
                maximum_attempt_timeout_seconds=min(4.5, timeout_seconds),
            )
        except BoundedHTTPTransportError as exc:
            raise MinerUOrchestratorUnavailableError(
                "MinerU API health endpoint unavailable"
            ) from exc
        except BoundedHTTPProtocolError as exc:
            raise MinerUOrchestratorError(
                "MinerU API health response violates the transport contract"
            ) from exc
        if status in _RETRYABLE_HTTP_STATUS_CODES:
            raise MinerUOrchestratorUnavailableError(
                "MinerU API health endpoint temporarily rejected the probe"
            )
        if not 200 <= status < 300:
            raise MinerUOrchestratorError(
                f"MinerU API health endpoint returned non-retryable HTTP {status}"
            )
        return _decode_mineru_orchestrator_health(
            payload,
            expected_task_slots=expected_task_slots,
            expected_task_retention_seconds=expected_task_retention_seconds,
            expected_cleanup_interval_seconds=expected_cleanup_interval_seconds,
        )

    def close(self) -> None:
        self._transport.close()


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
    max_pending_tasks_requested: int
    max_pending_tasks_effective: int
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
    expected_task_slots: int | None = MINERU_API_DEFAULT_TASK_SLOTS,
    expected_task_retention_seconds: int | None = MINERU_API_TASK_RETENTION_SECONDS,
    expected_cleanup_interval_seconds: int | None = MINERU_API_CLEANUP_INTERVAL_SECONDS,
) -> MinerUOrchestratorHealth:
    """Fetch one bounded strict `/health` sample without proxy inheritance."""
    client = MinerUOrchestratorHealthClient(api_url)
    try:
        return client.fetch(
            timeout_seconds=timeout_seconds,
            expected_task_slots=expected_task_slots,
            expected_task_retention_seconds=expected_task_retention_seconds,
            expected_cleanup_interval_seconds=expected_cleanup_interval_seconds,
        )
    finally:
        client.close()


def _decode_mineru_orchestrator_health(
    payload: bytes,
    *,
    expected_task_slots: int | None,
    expected_task_retention_seconds: int | None,
    expected_cleanup_interval_seconds: int | None,
) -> MinerUOrchestratorHealth:
    try:
        decoded = parse_mineru_api_health(
            payload,
            expected_task_slots=expected_task_slots,
            expected_task_retention_seconds=expected_task_retention_seconds,
            expected_cleanup_interval_seconds=expected_cleanup_interval_seconds,
        )
    except ValueError as exc:
        raise MinerUOrchestratorError(str(exc)) from exc
    return MinerUOrchestratorHealth(**decoded)


def parse_mineru_orchestrator_health_payload(
    decoded: object,
    *,
    expected_task_slots: int | None = MINERU_API_DEFAULT_TASK_SLOTS,
    expected_task_retention_seconds: int | None = MINERU_API_TASK_RETENTION_SECONDS,
    expected_cleanup_interval_seconds: int | None = MINERU_API_CLEANUP_INTERVAL_SECONDS,
) -> MinerUOrchestratorHealth:
    """Validate one already-decoded API health object under the shared contract."""

    try:
        health = validate_mineru_api_health(
            decoded,
            expected_task_slots=expected_task_slots,
            expected_task_retention_seconds=expected_task_retention_seconds,
            expected_cleanup_interval_seconds=expected_cleanup_interval_seconds,
        )
    except ValueError as exc:
        raise MinerUOrchestratorError(str(exc)) from exc
    return MinerUOrchestratorHealth(**health)


def wait_for_mineru_orchestrator_idle(
    api_url: str,
    *,
    timeout_seconds: float,
    poll_seconds: float = 1.0,
    expected_task_slots: int | None = MINERU_API_DEFAULT_TASK_SLOTS,
    expected_task_retention_seconds: int | None = MINERU_API_TASK_RETENTION_SECONDS,
    expected_cleanup_interval_seconds: int | None = MINERU_API_CLEANUP_INTERVAL_SECONDS,
) -> tuple[MinerUOrchestratorHealth, float]:
    """Wait for natural drain; this is deliberately not called cancellation."""

    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("MinerU API drain timing must be positive")
    started = time.monotonic()
    deadline = started + timeout_seconds
    client = MinerUOrchestratorHealthClient(api_url)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MinerUOrchestratorError(
                    "MinerU API did not prove queued/processing drain before deadline"
                )
            try:
                health = client.fetch(
                    timeout_seconds=min(15.0, max(0.1, remaining)),
                    expected_task_slots=expected_task_slots,
                    expected_task_retention_seconds=(
                        expected_task_retention_seconds
                    ),
                    expected_cleanup_interval_seconds=(
                        expected_cleanup_interval_seconds
                    ),
                )
            except MinerUOrchestratorUnavailableError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MinerUOrchestratorError(
                        "MinerU API drain remained transport-unproved before deadline"
                    ) from exc
                time.sleep(min(poll_seconds, remaining))
                continue
            if health.active_tasks == 0:
                return health, time.monotonic() - started
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MinerUOrchestratorError(
                    "MinerU API did not drain queued/processing tasks before deadline"
                )
            time.sleep(min(poll_seconds, remaining))
    finally:
        client.close()


__all__ = [
    "MINERU_API_INFERENCE_CONCURRENCY",
    "MINERU_API_PROCESSING_WINDOW_SIZE",
    "MINERU_API_PROTOCOL_VERSION",
    "MINERU_API_DEFAULT_TASK_SLOTS",
    "MINERU_API_TASK_RETENTION_SECONDS",
    "MINERU_API_CLEANUP_INTERVAL_SECONDS",
    "MINERU_API_VERSION",
    "MinerUOrchestratorError",
    "MinerUOrchestratorHealth",
    "MinerUOrchestratorHealthClient",
    "MinerUOrchestratorIncidentState",
    "MinerUOrchestratorUnavailableError",
    "fetch_mineru_orchestrator_health",
    "finish_mineru_orchestrator_incident",
    "mark_mineru_orchestrator_incident",
    "mineru_orchestrator_incident_generation",
    "mineru_orchestrator_incident_state",
    "parse_mineru_orchestrator_health_payload",
    "wait_for_mineru_orchestrator_idle",
]
