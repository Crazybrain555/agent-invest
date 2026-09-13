"""Handwritten wire contract examples, never a serving or quality receipt."""

from copy import deepcopy
import hashlib

from tests._mineru_capacity_config_fixture import CAPACITY_BYTES


_CAPACITY_SHA = "sha256:" + hashlib.sha256(CAPACITY_BYTES).hexdigest()
_WIRE = {
    "status": "healthy",
    "version": "3.4.4",
    "protocol_version": 2,
    "queued_tasks": 0,
    "processing_tasks": 3,
    "completed_tasks": 1,
    "failed_tasks": 2,
    "max_concurrent_requests": 2,
    "max_pending_tasks_requested": 3,
    "max_pending_tasks_effective": 3,
    "processing_window_size": 16,
    "task_retention_seconds": 600,
    "task_cleanup_interval_seconds": 30,
    "task_protocol_schema": "mineru-task-protocol.v2",
    "task_protocol_runtime": {
        "schema": "mineru-task-runtime.v3",
        "enabled": True,
        "task_registry_max_records": 128,
        "task_result_reservation_bytes": 31,
        "max_unacked_result_bytes": 73,
        "registry_schema": "mineru-task-registry.v3",
        "admission_scope": "post_form_owned_upload",
        "capacity_config_sha256": _CAPACITY_SHA,
    },
    "task_admission": {
        "schema": "mineru-task-admission.v1",
        "registry_schema": "mineru-task-registry.v3",
        "nonterminal_limit": 3,
        "ingress_tasks": 0,
        "accepted_pending_tasks": 0,
        "accepted_processing_tasks": 1,
        "accepted_finalizing_tasks": 2,
        "durable_nonterminal_tasks": 3,
        "routeless_accepted_tasks": 0,
        "ingress_cleanup_tasks": 0,
        "unowned_ingress_tasks": 0,
        "scheduled_tasks": 3,
        "queue_depth": 0,
        "active_processors": 3,
        "recovery_overcommitted": False,
        "admission_open": False,
        "blocked_reason": "capacity_full",
    },
    "capacity_observation": {
        "schema": "mineru.capacity-observation.v1",
        "capacity_config_sha256": _CAPACITY_SHA,
        "owner": {
            "process_id": 123,
            "process_start_ticks": 456,
            "boot_id": "11111111-2222-4333-8444-555555555555",
            "loop_epoch": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        },
        "resolved_limits": {
            "parse_active_limit": 2,
            "total_nonterminal_limit": 3,
            "finalizer_active_limit": 1,
            "result_reservation_bytes": 31,
            "max_unacked_result_bytes": 73,
            "final_http_limit_per_loop": 7,
        },
        "http_limiter_state": "initialized",
        "stage_counters": {
            "result_capacity_waiting": 0,
            "parse_waiting": 0,
            "parse_active": 1,
            "finalizer_waiting": 1,
            "finalizer_active": 1,
        },
        "http_counters": {"active_requests": 6, "pending_requests": 5},
        "owner_control": {
            "foreign_loop_observed": False,
            "soft_drain_requested": False,
            "soft_drain_applied": False,
            "trigger": None,
        },
        "framework_limits": {
            "torch_intraop_threads": {"state": "available", "value": 6, "reason": None},
            "pdf_render_pool_max_workers": {
                "state": "available",
                "value": 5,
                "reason": None,
            },
            "mkl_threads": {
                "state": "unavailable",
                "value": None,
                "reason": "no_serving_getter",
            },
            "openblas_threads": {
                "state": "unavailable",
                "value": None,
                "reason": "no_serving_getter",
            },
        },
        "observed_at": {
            "clock": "python.monotonic_ns",
            "implementation": "clock_gettime(CLOCK_MONOTONIC)",
            "started_ns": 100,
            "completed_ns": 120,
        },
    },
}


def capacity_health_payload():
    return deepcopy(_WIRE)
