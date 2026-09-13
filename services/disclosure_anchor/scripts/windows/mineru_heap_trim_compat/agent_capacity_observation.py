"""Content-free observations of existing serving objects; never creates pools."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
from typing import Any
import uuid

from mineru.cli.agent_capacity_config import MineruCapacityConfig


def _unavailable(reason: str) -> dict:
    return {"state": "unavailable", "value": None, "reason": reason}


def _resolved_integer(value: object) -> dict:
    if type(value) is not int or value < 1:
        raise RuntimeError("serving capacity getter returned an invalid integer")
    return {"state": "available", "value": value, "reason": None}


def _framework_limits() -> dict:
    torch = sys.modules.get("torch")
    getter = getattr(torch, "get_num_threads", None)
    intraop = (
        _unavailable("serving_getter_not_loaded") if not callable(getter)
        else _resolved_integer(getter())
    )
    render = sys.modules.get("mineru.utils.pdf_image_tools")
    pool_lock = getattr(render, "_pdf_render_executor_lock", None)
    pool_capacity = _unavailable("serving_pool_not_initialized")
    if pool_lock is not None:
        if not pool_lock.acquire(blocking=False):
            pool_capacity = _unavailable("serving_pool_lock_busy")
        else:
            try:
                pool = getattr(render, "_pdf_render_executor", None)
                if pool is not None:
                    pool_capacity = _resolved_integer(pool._max_workers)
            finally:
                pool_lock.release()
    return {
        "torch_intraop_threads": intraop,
        "pdf_render_pool_max_workers": pool_capacity,
        "mkl_threads": _unavailable("no_serving_getter"),
        "openblas_threads": _unavailable("no_serving_getter"),
    }


class CapacityServingObservation:
    """Process birth is sampled at manager startup; stages remain live samples."""

    def __init__(self, config: MineruCapacityConfig, manager: Any) -> None:
        self.config = config
        self.manager = manager
        self.process_id = os.getpid()
        stat = Path("/proc/self/stat").read_text()
        self.process_start_ticks = int(stat[stat.rfind(")") + 2:].split()[19])
        self.boot_id = str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))
        if self.process_start_ticks <= 0:
            raise RuntimeError("serving process birth is invalid")

    def snapshot(self) -> dict:
        from mineru.cli.agent_capacity_bootstrap import get_process_capacity
        from mineru_vl_utils.vlm_client.http_client import _capacity_http_snapshot

        if os.getpid() != self.process_id or get_process_capacity() is not self.config:
            raise RuntimeError("capacity observation process or config changed")
        started = time.monotonic_ns()
        http = _capacity_http_snapshot(self.config.sha256)
        executor = self.manager.task_protocol_executor
        registry = self.manager.task_protocol_v2
        limits = {
            "parse_active_limit": executor.parse_slots,
            "total_nonterminal_limit": self.manager.max_nonterminal_tasks,
            "finalizer_active_limit": executor.finalizer_slots,
            "result_reservation_bytes": executor.result_reservation_bytes,
            "max_unacked_result_bytes": registry._limit,
        }
        for field, actual in limits.items():
            if type(actual) is not int or actual != getattr(self.config, field):
                raise RuntimeError("serving object capacity drifted: " + field)
        resolved_h = http["final_http_limit_per_loop"]
        if resolved_h is not None and resolved_h != self.config.final_http_limit_per_loop:
            raise RuntimeError("serving HTTP capacity drifted")
        if http["owner_control"]["soft_drain_applied"] and not self.manager.is_shutting_down:
            raise RuntimeError("capacity owner drain was silently reset")
        result = {
            "schema": "mineru.capacity-observation.v1",
            "capacity_config_sha256": self.config.sha256,
            "owner": {
                "process_id": self.process_id,
                "process_start_ticks": self.process_start_ticks,
                "boot_id": self.boot_id,
                "loop_epoch": http["loop_epoch"],
            },
            "resolved_limits": {**limits, "final_http_limit_per_loop": resolved_h},
            "http_limiter_state": http["http_limiter_state"],
            "stage_counters": executor.stage_snapshot(),
            "http_counters": http["http_counters"],
            "owner_control": http["owner_control"],
            "framework_limits": _framework_limits(),
        }
        result["observed_at"] = {
            "clock": "python.monotonic_ns",
            "implementation": time.get_clock_info("monotonic").implementation,
            "started_ns": started, "completed_ns": time.monotonic_ns(),
        }
        return result
