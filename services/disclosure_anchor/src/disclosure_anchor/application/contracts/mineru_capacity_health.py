"""Explicit capacity health; legacy serial wire validators remain unchanged."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast
from uuid import UUID

from disclosure_anchor.application.contracts.mineru_api_health import (
    MINERU_API_HEALTH_FIELDS,
    MineruApiHealth,
    validate_mineru_task_admission,
)
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruCapacityConfig,
    encode_mineru_capacity_config,
)
from disclosure_anchor.application.contracts.mineru_process_profile import MineruProcessProfile
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


class MineruCapacityHealth(MineruApiHealth):
    task_protocol_schema: str
    task_protocol_runtime: dict[str, Any]
    task_admission: dict[str, Any]
    capacity_observation: dict[str, Any]


def _closed(value: object, fields: set[str] | frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError("MinerU capacity " + label + " fields are not closed")
    return value


def _integer(value: object, *, maximum: int = 2**63 - 1, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("MinerU capacity observation integer is invalid")
    return value


def _uuid(value: object) -> None:
    if type(value) is not str or len(value) != 36 or str(UUID(value)) != value:
        raise ValueError("MinerU capacity observation UUID is invalid")


def validate_mineru_capacity_wire_health(
    decoded: object, *, expected_capacity: MineruCapacityConfig,
    expected_task_retention_seconds: int = 600,
    expected_cleanup_interval_seconds: int = 30,
) -> MineruCapacityHealth:
    """Retain all serving evidence and compare it with external input authority."""
    encode_mineru_capacity_config(expected_capacity)
    config = expected_capacity
    health = _closed(decoded, MINERU_API_HEALTH_FIELDS | {
        "task_protocol_schema", "task_protocol_runtime", "task_admission", "capacity_observation",
    }, "wire health")
    for field in MINERU_API_HEALTH_FIELDS - {"status", "version"}:
        _integer(health[field])
    expected = {
        "status": "healthy", "version": "3.4.4", "protocol_version": 2,
        "max_concurrent_requests": config.parse_active_limit,
        "max_pending_tasks_requested": config.total_nonterminal_limit,
        "max_pending_tasks_effective": config.total_nonterminal_limit,
        "processing_window_size": config.processing_window_size,
        "task_retention_seconds": expected_task_retention_seconds,
        "task_cleanup_interval_seconds": expected_cleanup_interval_seconds,
        "task_protocol_schema": "mineru-task-protocol.v2",
    }
    if any(health[key] != value for key, value in expected.items()):
        raise ValueError("MinerU capacity health differs from expected startup identity")
    if health["queued_tasks"] + health["processing_tasks"] > config.total_nonterminal_limit:
        raise ValueError("MinerU accepted responsibility exceeds configured P")
    runtime_expected = {
        "schema": "mineru-task-runtime.v3", "enabled": True,
        "task_registry_max_records": 128,
        "task_result_reservation_bytes": config.result_reservation_bytes,
        "max_unacked_result_bytes": config.max_unacked_result_bytes,
        "registry_schema": "mineru-task-registry.v3",
        "admission_scope": "post_form_owned_upload",
        "capacity_config_sha256": config.sha256,
    }
    runtime = _closed(health["task_protocol_runtime"], set(runtime_expected), "task runtime")
    if any(type(runtime[key]) is not type(value) or runtime[key] != value
           for key, value in runtime_expected.items()):
        raise ValueError("MinerU serving task runtime differs from expected capacity")
    validate_mineru_task_admission(
        health["task_admission"], queued_tasks=health["queued_tasks"],
        processing_tasks=health["processing_tasks"], nonterminal_limit=config.total_nonterminal_limit,
    )
    observation = _closed(health["capacity_observation"], {
        "schema", "capacity_config_sha256", "owner", "resolved_limits", "http_limiter_state",
        "stage_counters", "http_counters", "owner_control", "framework_limits", "observed_at",
    }, "serving observation")
    if (observation["schema"] != "mineru.capacity-observation.v1"
            or observation["capacity_config_sha256"] != config.sha256):
        raise ValueError("MinerU capacity observation is not bound to the expected config")
    owner = _closed(observation["owner"], {
        "process_id", "process_start_ticks", "boot_id", "loop_epoch",
    }, "serving owner")
    _integer(owner["process_id"], minimum=1)
    _integer(owner["process_start_ticks"], minimum=1)
    _uuid(owner["boot_id"])
    _uuid(owner["loop_epoch"])
    observed_at = _closed(observation["observed_at"], {
        "clock", "implementation", "started_ns", "completed_ns",
    }, "sample clock")
    if (observed_at["clock"] != "python.monotonic_ns"
            or type(observed_at["implementation"]) is not str
            or not 1 <= len(observed_at["implementation"]) <= 128):
        raise ValueError("MinerU capacity sample clock domain is invalid")
    started = _integer(observed_at["started_ns"])
    _integer(observed_at["completed_ns"], minimum=started)
    limits = _closed(observation["resolved_limits"], {
        "parse_active_limit", "total_nonterminal_limit", "finalizer_active_limit",
        "result_reservation_bytes", "max_unacked_result_bytes", "final_http_limit_per_loop",
    }, "resolved limits")
    for field in limits.keys() - {"final_http_limit_per_loop"}:
        if _integer(limits[field], minimum=1) != getattr(config, field):
            raise ValueError("MinerU resolved capacity disagrees with the startup config")
    state = observation["http_limiter_state"]
    if state == "not_initialized":
        if limits["final_http_limit_per_loop"] is not None:
            raise ValueError("uninitialized HTTP limiter cannot report a resolved limit")
    elif state == "initialized":
        if (_integer(limits["final_http_limit_per_loop"], minimum=1)
                != config.final_http_limit_per_loop):
            raise ValueError("MinerU resolved final HTTP capacity disagrees")
    else:
        raise ValueError("MinerU HTTP limiter state is invalid")
    stages = _closed(observation["stage_counters"], {
        "result_capacity_waiting", "parse_waiting", "parse_active", "finalizer_waiting", "finalizer_active",
    }, "stage counters")
    for value in stages.values():
        _integer(value, maximum=config.total_nonterminal_limit)
    if (stages["parse_active"] > config.parse_active_limit
            or stages["finalizer_active"] > config.finalizer_active_limit):
        raise ValueError("MinerU actual stage owners exceed their capacities")
    admission = health["task_admission"]
    if (sum(stages.values()) > admission["durable_nonterminal_tasks"]
            or stages["result_capacity_waiting"] + stages["parse_waiting"] > admission["accepted_pending_tasks"]
            or stages["parse_active"] > admission["accepted_processing_tasks"]
            or stages["finalizer_waiting"] + stages["finalizer_active"] > admission["accepted_finalizing_tasks"]):
        raise ValueError("MinerU stage owners exceed the corresponding durable responsibility")
    http = _closed(observation["http_counters"], {"active_requests", "pending_requests"}, "HTTP counters")
    _integer(http["active_requests"], maximum=config.final_http_limit_per_loop)
    _integer(http["pending_requests"])
    if state == "not_initialized" and any(http.values()):
        raise ValueError("uninitialized HTTP limiter cannot own active or waiting POSTs")
    control = _closed(observation["owner_control"], {
        "foreign_loop_observed", "soft_drain_requested", "soft_drain_applied", "trigger",
    }, "owner control")
    if any(type(control[field]) is not bool for field in control.keys() - {"trigger"}):
        raise ValueError("MinerU capacity drain flags are invalid")
    if (control["soft_drain_requested"] != control["foreign_loop_observed"]
            or (control["soft_drain_applied"] and not control["soft_drain_requested"])
            or control["trigger"] != ("foreign_event_loop" if control["foreign_loop_observed"] else None)
            or (control["soft_drain_applied"] and health["task_admission"]["blocked_reason"] != "shutting_down")):
        raise ValueError("MinerU capacity owner control contradicts its admission state")
    framework = _closed(observation["framework_limits"], {
        "torch_intraop_threads", "pdf_render_pool_max_workers", "mkl_threads", "openblas_threads",
    }, "framework observations")
    for field, item in framework.items():
        item = _closed(item, {"state", "value", "reason"}, "framework observation")
        if item["state"] == "available":
            _integer(item["value"], minimum=1)
            if item["reason"] is not None or field in {"mkl_threads", "openblas_threads"}:
                raise ValueError("MinerU framework observation claims an unsupported getter")
        elif item["state"] == "unavailable":
            reasons = {
                "torch_intraop_threads": {"serving_getter_not_loaded"},
                "pdf_render_pool_max_workers": {"serving_pool_not_initialized", "serving_pool_lock_busy"},
                "mkl_threads": {"no_serving_getter"}, "openblas_threads": {"no_serving_getter"},
            }
            if (item["value"] is not None or type(item["reason"]) is not str
                    or item["reason"] not in reasons[field]):
                raise ValueError("MinerU unavailable framework value is not explicit")
        else:
            raise ValueError("MinerU framework observation state is invalid")
    return cast(MineruCapacityHealth, deepcopy(health))


def parse_mineru_capacity_wire_health(
    payload: bytes, *, expected_capacity: MineruCapacityConfig,
    expected_task_retention_seconds: int = 600,
    expected_cleanup_interval_seconds: int = 30,
) -> MineruCapacityHealth:
    if type(payload) is not bytes or not 0 < len(payload) <= 65536:
        raise ValueError("MinerU capacity health payload exceeds its byte bound")
    return validate_mineru_capacity_wire_health(
        strict_json_loads(payload), expected_capacity=expected_capacity,
        expected_task_retention_seconds=expected_task_retention_seconds,
        expected_cleanup_interval_seconds=expected_cleanup_interval_seconds,
    )


# Process-profile fields that are projections of the capacity config. The
# capacity is the one authority; a profile that disagrees is not this release's.
PROFILE_CAPACITY_PROJECTION = (
    ("api_task_slots", "parse_active_limit"),
    ("api_max_pending_tasks", "total_nonterminal_limit"),
    ("registry_nonterminal_cap", "total_nonterminal_limit"),
    ("finalizer_slots", "finalizer_active_limit"),
    ("inference_concurrency", "final_http_limit_per_loop"),
    ("gpu_request_slots", "final_http_limit_per_loop"),
    ("processing_window_size", "processing_window_size"),
    ("omp_thread_count", "omp_num_threads"),
    ("requested_hybrid_batch_ratio", "hybrid_batch_ratio_requested"),
    ("pipeline_inference_locks", "pipeline_inference_locks"),
    ("result_reservation_bytes", "result_reservation_bytes"),
    ("max_unacked_result_bytes", "max_unacked_result_bytes"),
)


def assert_profile_matches_capacity(profile: MineruProcessProfile, capacity: MineruCapacityConfig) -> None:
    """Refuse a process profile that is not the projection of this exact capacity config."""
    encode_mineru_capacity_config(capacity)
    differing = sorted(
        profile_field for profile_field, capacity_field in PROFILE_CAPACITY_PROJECTION
        if getattr(profile, profile_field) != getattr(capacity, capacity_field)
    )
    if differing:
        raise ValueError("process profile is not the projection of the capacity config: " + ", ".join(differing))
