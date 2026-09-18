"""Content-free observations of existing serving objects; never creates pools."""

from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import re
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


def _kernel_text(path: Path, maximum: int = 65536) -> str:
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if not raw or len(raw) > maximum:
        raise RuntimeError("pressure kernel input exceeds its byte bound")
    return raw.decode("ascii")


def _linux_pressure_memory() -> dict:
    """Read this container's cgroup and the VM, without claiming hidden ancestors.

    The supported deployment has a private cgroup namespace mounted at its
    root. Other topologies need their own qualified mapping, never path guesses.
    These are pressure signals, not an allocation or reservation guarantee.
    """
    membership = _kernel_text(Path("/proc/self/cgroup"), 4096)
    if membership != "0::/\n":
        raise RuntimeError("pressure requires the qualified private cgroup root")
    def mounted_root() -> str:
        matches = []
        for line in _kernel_text(Path("/proc/self/mountinfo")).splitlines():
            left, separator, right = line.partition(" - ")
            filesystem = right.split()
            if separator and filesystem and filesystem[0] == "cgroup2":
                fields = left.split()
                if len(fields) >= 6 and fields[3:5] == ["/", "/sys/fs/cgroup"]:
                    matches.append(line)
        if len(matches) != 1:
            raise RuntimeError("pressure cgroup mount does not identify one namespace root")
        return matches[0]
    mount = mounted_root()
    root = Path("/sys/fs/cgroup")
    birth = root.stat()
    namespace = os.readlink("/proc/self/ns/cgroup")
    if re.fullmatch(r"cgroup:\[[0-9]+\]", namespace) is None:
        raise RuntimeError("pressure cgroup namespace identity is invalid")
    identity = {"membership": membership, "mount": mount, "namespace": namespace,
                "device": birth.st_dev, "inode": birth.st_ino}
    identity_sha = "sha256:" + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    def number(path: Path, *, unlimited: bool = False) -> int | None:
        value = _kernel_text(path, 128).strip()
        if unlimited and value == "max":
            return None
        if re.fullmatch(r"[0-9]+", value) is None:
            raise RuntimeError("pressure kernel counter is malformed")
        return int(value)
    current = number(root / "memory.current")
    maximum = number(root / "memory.max", unlimited=True)
    events = {}
    for line in _kernel_text(root / "memory.events", 4096).splitlines():
        pieces = line.split()
        if len(pieces) != 2 or re.fullmatch(r"[a-z_]+", pieces[0]) is None or re.fullmatch(r"[0-9]+", pieces[1]) is None or pieces[0] in events:
            raise RuntimeError("pressure memory event is malformed")
        events[pieces[0]] = int(pieces[1])
    if not {"low", "high", "max", "oom", "oom_kill"} <= events.keys():
        raise RuntimeError("pressure memory event counters are incomplete")
    meminfo = {}
    for line in _kernel_text(Path("/proc/meminfo")).splitlines():
        name, _, tail = line.partition(":")
        if name in {"MemTotal", "MemAvailable"}:
            if name in meminfo or re.fullmatch(r"\s*[0-9]+ kB", tail) is None:
                raise RuntimeError("pressure VM memory field is malformed")
            meminfo[name] = int(tail.split()[0]) * 1024
    if set(meminfo) != {"MemTotal", "MemAvailable"} or not 0 <= meminfo["MemAvailable"] <= meminfo["MemTotal"] or meminfo["MemTotal"] <= 0:
        raise RuntimeError("pressure VM memory fields are incomplete or invalid")
    after = root.stat()
    if (birth.st_dev, birth.st_ino) != (after.st_dev, after.st_ino) or os.readlink("/proc/self/ns/cgroup") != namespace or _kernel_text(Path("/proc/self/cgroup"), 4096) != membership or mounted_root() != mount:
        raise RuntimeError("pressure cgroup identity changed during read")
    return {"scope": "self_cgroup_and_vm", "ancestor_visibility": "not_observed",
            "cgroup_identity_sha256": identity_sha,
            "cgroup_current_bytes": current, "cgroup_max_bytes": maximum,
            "memory_events": events, "vm_total_bytes": meminfo["MemTotal"],
            "vm_available_bytes": meminfo["MemAvailable"]}


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
        self._pressure_cgroup_identity: str | None = None

    def _assert_process_config(self) -> None:
        from mineru.cli.agent_capacity_bootstrap import get_process_capacity
        if os.getpid() != self.process_id or get_process_capacity() is not self.config:
            raise RuntimeError("capacity observation process or config changed")

    def pressure_begin(self) -> tuple[int, dict]:
        """Capture all serving-loop-owned state before blocking kernel IO."""
        started = time.monotonic_ns()
        self._assert_process_config()
        return started, self.snapshot()

    @staticmethod
    def pressure_kernel_memory() -> dict:
        """Only blocking proc/cgroup reads. Safe to run on the owned IO executor."""
        return _linux_pressure_memory()

    def pressure_finish(self, started: int, observation: dict, memory: dict) -> dict:
        """Revalidate owner/config on the serving loop and bind one fresh kernel read."""
        if type(started) is not int or started < 0 or type(observation) is not dict or type(memory) is not dict:
            raise ValueError("pressure observation components are invalid")
        self._assert_process_config()
        if observation.get("owner") != self.snapshot().get("owner"):
            raise RuntimeError("pressure serving owner changed during kernel read")
        identity = memory.get("cgroup_identity_sha256")
        if not isinstance(identity, str):
            raise RuntimeError("pressure cgroup identity is absent")
        if self._pressure_cgroup_identity is not None and self._pressure_cgroup_identity != identity:
            raise RuntimeError("pressure cgroup identity changed during serving lifetime")
        self._pressure_cgroup_identity = identity
        return {"schema": "mineru.process-pressure.v1",
                "capacity_config_sha256": self.config.sha256,
                "owner": observation["owner"], "memory": memory,
                "observed_at": {"clock": "python.monotonic_ns", "started_ns": started,
                                "completed_ns": time.monotonic_ns()}}

    def pressure_snapshot(self) -> dict:
        """Synchronous compatibility path for offline/unit callers only."""
        started, observation = self.pressure_begin()
        return self.pressure_finish(started, observation, self.pressure_kernel_memory())

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
