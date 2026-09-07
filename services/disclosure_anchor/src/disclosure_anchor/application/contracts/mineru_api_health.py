"""Closed MinerU API health contract shared by every observer."""

from __future__ import annotations

from typing import TypedDict

from disclosure_anchor.application.contracts.strict_json import strict_json_loads

MINERU_API_TASK_REGISTRY_MAX_RECORDS = 128
MINERU_API_RESULT_RESERVATION_BYTES = 256 * 1024 * 1024
MINERU_API_MAX_UNACKED_RESULT_BYTES = 2 * 1024 * 1024 * 1024


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
        "max_pending_tasks_requested",
        "max_pending_tasks_effective",
        "processing_window_size",
        "task_retention_seconds",
        "task_cleanup_interval_seconds",
    }
)


class MineruApiHealth(TypedDict):
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


def validate_mineru_api_health(
    decoded: object,
    *,
    expected_task_slots: int | None,
    expected_task_retention_seconds: int | None = 600,
    expected_cleanup_interval_seconds: int | None = 30,
) -> MineruApiHealth:
    if not isinstance(decoded, dict) or set(decoded) != MINERU_API_HEALTH_FIELDS:
        raise ValueError("MinerU API health fields are not closed")
    if expected_task_slots is not None and (
        expected_task_slots != 1
        or decoded.get("max_concurrent_requests") != 1
        or decoded.get("max_pending_tasks_requested") != 1
        or decoded.get("max_pending_tasks_effective") != 1
    ):
        raise ValueError("MinerU API task-slot/pending limit drifted")
    if (
        decoded.get("status") != "healthy"
        or decoded.get("version") != "3.4.4"
        or decoded.get("protocol_version") != 2
        or decoded.get("processing_window_size") != 16
        or (
            expected_task_retention_seconds is not None
            and decoded.get("task_retention_seconds")
            != expected_task_retention_seconds
        )
        or (
            expected_cleanup_interval_seconds is not None
            and decoded.get("task_cleanup_interval_seconds")
            != expected_cleanup_interval_seconds
        )
    ):
        raise ValueError("MinerU API identity or health drifted")
    integer_fields = MINERU_API_HEALTH_FIELDS - {"status", "version"}
    positive = {
        "protocol_version",
        "max_concurrent_requests",
        "max_pending_tasks_requested",
        "max_pending_tasks_effective",
        "processing_window_size",
        "task_cleanup_interval_seconds",
    }
    if any(
        isinstance(decoded.get(name), bool)
        or not isinstance(decoded.get(name), int)
        or decoded[name] < (1 if name in positive else 0)
        for name in integer_fields
    ):
        raise ValueError("MinerU API health numbers are invalid")
    if (
        decoded["processing_tasks"] > decoded["max_concurrent_requests"]
        or decoded["queued_tasks"] + decoded["processing_tasks"]
        > decoded["max_pending_tasks_effective"]
        or decoded["queued_tasks"] + decoded["processing_tasks"]
        > decoded["processing_window_size"]
        or decoded["max_pending_tasks_effective"]
        < decoded["max_pending_tasks_requested"]
        or decoded["max_pending_tasks_effective"]
        < decoded["max_concurrent_requests"]
    ):
        raise ValueError("MinerU API pending/task counters exceed declared limits")
    return MineruApiHealth(
        status=decoded["status"],
        version=decoded["version"],
        protocol_version=decoded["protocol_version"],
        queued_tasks=decoded["queued_tasks"],
        processing_tasks=decoded["processing_tasks"],
        completed_tasks=decoded["completed_tasks"],
        failed_tasks=decoded["failed_tasks"],
        max_concurrent_requests=decoded["max_concurrent_requests"],
        max_pending_tasks_requested=decoded["max_pending_tasks_requested"],
        max_pending_tasks_effective=decoded["max_pending_tasks_effective"],
        processing_window_size=decoded["processing_window_size"],
        task_retention_seconds=decoded["task_retention_seconds"],
        task_cleanup_interval_seconds=decoded["task_cleanup_interval_seconds"],
    )


def parse_mineru_api_health(
    payload: bytes,
    *,
    expected_task_slots: int | None,
    expected_task_retention_seconds: int | None = 600,
    expected_cleanup_interval_seconds: int | None = 30,
) -> MineruApiHealth:
    try:
        decoded = strict_json_loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("MinerU API health is not valid strict UTF-8 JSON") from exc
    return validate_mineru_api_wire_health(
        decoded,
        expected_task_slots=expected_task_slots,
        expected_task_retention_seconds=expected_task_retention_seconds,
        expected_cleanup_interval_seconds=expected_cleanup_interval_seconds,
    )


def validate_mineru_task_runtime(decoded: object) -> None:
    """The exact serving-process capacity subproof, never configured guesses."""
    limits = {
        "task_registry_max_records": MINERU_API_TASK_REGISTRY_MAX_RECORDS,
        "task_result_reservation_bytes": MINERU_API_RESULT_RESERVATION_BYTES,
        "max_unacked_result_bytes": MINERU_API_MAX_UNACKED_RESULT_BYTES,
    }
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"schema", "enabled", *limits}
        or decoded.get("schema") != "mineru-task-runtime.v1"
        or decoded.get("enabled") is not True
        or any(type(decoded.get(key)) is not int or decoded[key] != value
               for key, value in limits.items())
    ):
        raise ValueError("MinerU serving task runtime identity or limits drifted")


def validate_mineru_api_wire_health(
    decoded: object,
    *,
    expected_task_slots: int | None,
    expected_task_retention_seconds: int | None = 600,
    expected_cleanup_interval_seconds: int | None = 30,
) -> MineruApiHealth:
    """Validate the versioned wire shape before projecting existing receipt fields.

    validate_mineru_api_health remains the closed normalized receipt contract;
    no wire caller may silently drop unknown fields or accept that projection.
    """
    if (
        not isinstance(decoded, dict)
        or set(decoded) != MINERU_API_HEALTH_FIELDS | {
            "task_protocol_schema", "task_protocol_runtime"
        }
        or decoded.get("task_protocol_schema") != "mineru-task-protocol.v2"
    ):
        raise ValueError("MinerU API wire health fields are not closed")
    validate_mineru_task_runtime(decoded["task_protocol_runtime"])
    return validate_mineru_api_health(
        {name: decoded[name] for name in MINERU_API_HEALTH_FIELDS},
        expected_task_slots=expected_task_slots,
        expected_task_retention_seconds=expected_task_retention_seconds,
        expected_cleanup_interval_seconds=expected_cleanup_interval_seconds,
    )


__all__ = [
    "MINERU_API_HEALTH_FIELDS",
    "parse_mineru_api_health",
    "validate_mineru_api_health",
    "validate_mineru_api_wire_health",
    "validate_mineru_task_runtime",
]
