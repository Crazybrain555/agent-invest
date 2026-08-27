"""Pinned-SSH, content-free host sampler for the commissioned MinerU host."""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Literal

from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_WINDOWS_COLLECTOR_PATH,
)
from disclosure_anchor.application.contracts.capacity import HostSampleValues


HOST_CONTAINER_NAMES = frozenset(
    {"mineru-api", "mineru-api-proxy", "mineru-openai-server"}
)
_SSH_HOST_RE = re.compile(r"^(?!-)[A-Za-z0-9.-]+$")
_SSH_USER_RE = re.compile(r"^(?!-)[A-Za-z0-9_.-]+$")
_CONTAINER_ID_RE = re.compile(r"^[a-f0-9]{64}$")


def _private_observer_file(path: Path, *, label: str) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise ValueError(f"{label} must be an owner-only 0600 regular file")


def build_host_observer_ssh_command(
    *,
    host: str,
    user: str,
    port: int,
    identity_file: Path,
    known_hosts_file: Path,
    expected_host_key_sha256: str,
) -> list[str]:
    if (
        _SSH_HOST_RE.fullmatch(host) is None
        or _SSH_USER_RE.fullmatch(user) is None
        or port != 22
    ):
        raise ValueError("host observer SSH destination is invalid")
    _private_observer_file(identity_file, label="host observer SSH identity")
    _private_observer_file(known_hosts_file, label="host observer known_hosts")
    lines = [
        line.strip()
        for line in known_hosts_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(lines) != 1:
        raise ValueError("host observer known_hosts must contain one pinned key")
    fields = lines[0].split()
    if len(fields) != 3 or fields[0] != host or fields[1] != "ssh-ed25519":
        raise ValueError("host observer known_hosts does not pin the exact host")
    try:
        key_blob = base64.b64decode(fields[2], validate=True)
    except ValueError as exc:
        raise ValueError("host observer key is not canonical base64") from exc
    observed_host_key_sha256 = "sha256:" + hashlib.sha256(key_blob).hexdigest()
    if observed_host_key_sha256 != expected_host_key_sha256:
        raise ValueError("host observer SSH key drifted from runtime manifest")
    return [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-i",
        str(identity_file),
        "-p",
        str(port),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "CheckHostIP=no",
        "-o",
        "ConnectTimeout=15",
        "--",
        f"{user}@{host}",
    ]


def _integer(value: object, *, label: str, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"host capacity {label} is invalid")
    if value < (0 if allow_zero else 1):
        raise ValueError(f"host capacity {label} is invalid")
    return value


def project_host_capacity_sample(
    payload: object,
    *,
    expected_collector_sha256: str,
    expected_windows_node_identity_sha256: str,
    docker_memory_reserve_bytes: int,
) -> HostSampleValues:
    """Validate the collector contract and remove host/container identifiers."""

    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "observed_at_utc",
        "collector_path",
        "collector_sha256",
        "windows_node_identity_sha256",
        "containers",
    }:
        raise ValueError("host capacity sample fields drifted")
    if (
        payload.get("schema") != "mineru-host-capacity-sample.v1"
        or payload.get("collector_path") != MINERU_WINDOWS_COLLECTOR_PATH
        or payload.get("collector_sha256") != expected_collector_sha256
        or payload.get("windows_node_identity_sha256")
        != expected_windows_node_identity_sha256
        or docker_memory_reserve_bytes < 1
    ):
        raise ValueError("host capacity sample identity drifted")
    try:
        observed_at = datetime.fromisoformat(
            str(payload.get("observed_at_utc")).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("host capacity timestamp is invalid") from exc
    if observed_at.tzinfo is None:
        raise ValueError("host capacity timestamp is not aware")
    containers = payload.get("containers")
    if not isinstance(containers, list) or len(containers) != 3:
        raise ValueError("host capacity container set is incomplete")

    names: set[str] = set()
    epochs: list[dict[str, str]] = []
    restart_total = 0
    oom_killed_count = 0
    unsafe_count = 0
    cgroup_oom_total = 0
    cgroup_oom_kill_total = 0
    cgroup_high_total = 0
    vm_total: int | None = None
    vm_available: int | None = None
    api_rss: int | None = None
    api_rss_hwm: int | None = None
    violation_codes: set[
        Literal[
            "cgroup_oom_observed",
            "container_state_unsafe",
            "memory_reserve_crossed",
        ]
    ] = set()
    expected_fields = {
        "name",
        "id",
        "started_at_utc",
        "restart_count",
        "oom_killed",
        "exit_code",
        "running",
        "status",
        "health",
        "pid",
        "memory_current_bytes",
        "memory_max_bytes",
        "memory_events",
        "pid1_rss_bytes",
        "pid1_rss_hwm_bytes",
        "docker_vm_memory_total_bytes",
        "docker_vm_memory_available_bytes",
    }
    for item in containers:
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise ValueError("host capacity container fields drifted")
        name = item.get("name")
        container_id = item.get("id")
        if (
            not isinstance(name, str)
            or name in names
            or not isinstance(container_id, str)
            or _CONTAINER_ID_RE.fullmatch(container_id) is None
        ):
            raise ValueError("host capacity container identity is invalid")
        restart_count = _integer(
            item.get("restart_count"), label="restart_count", allow_zero=True
        )
        exit_code = _integer(item.get("exit_code"), label="exit_code", allow_zero=True)
        oom_killed = item.get("oom_killed")
        running = item.get("running")
        status = item.get("status")
        health = item.get("health")
        if (
            not isinstance(oom_killed, bool)
            or not isinstance(running, bool)
            or not isinstance(status, str)
            or not isinstance(health, str)
        ):
            raise ValueError("host capacity container state fields are invalid")
        unsafe = (
            restart_count != 0
            or oom_killed
            or exit_code != 0
            or not running
            or status != "running"
            or health != "healthy"
        )
        if unsafe:
            violation_codes.add("container_state_unsafe")
            unsafe_count += 1
        try:
            started_at = datetime.fromisoformat(
                str(item.get("started_at_utc")).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError("host capacity container epoch is invalid") from exc
        if started_at.tzinfo is None:
            raise ValueError("host capacity container epoch is not aware")
        memory_current = _integer(
            item.get("memory_current_bytes"),
            label="memory_current_bytes",
            allow_zero=True,
        )
        memory_max_value = item.get("memory_max_bytes")
        memory_max = (
            None
            if memory_max_value is None
            else _integer(memory_max_value, label="memory_max_bytes")
        )
        if memory_max is not None and memory_current > memory_max:
            raise ValueError("host capacity cgroup memory exceeds its limit")
        rss = _integer(
            item.get("pid1_rss_bytes"), label="pid1_rss_bytes", allow_zero=True
        )
        rss_hwm = _integer(
            item.get("pid1_rss_hwm_bytes"),
            label="pid1_rss_hwm_bytes",
            allow_zero=True,
        )
        if rss_hwm < rss:
            raise ValueError("host capacity RSS high-water mark is invalid")
        container_vm_total = _integer(
            item.get("docker_vm_memory_total_bytes"),
            label="docker_vm_memory_total_bytes",
        )
        container_vm_available = _integer(
            item.get("docker_vm_memory_available_bytes"),
            label="docker_vm_memory_available_bytes",
        )
        if container_vm_available > container_vm_total:
            raise ValueError("host capacity Docker VM memory values are invalid")
        if vm_total is None:
            vm_total = container_vm_total
            vm_available = container_vm_available
        elif vm_total != container_vm_total:
            raise ValueError("host capacity Docker VM total memory values disagree")
        else:
            assert vm_available is not None
            vm_available = min(vm_available, container_vm_available)
        if container_vm_available < docker_memory_reserve_bytes:
            violation_codes.add("memory_reserve_crossed")
        events = item.get("memory_events")
        if (
            not isinstance(events, dict)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in events.values()
            )
        ):
            raise ValueError("host capacity cgroup OOM evidence is invalid")
        oom = int(events.get("oom", 0))
        oom_kill = int(events.get("oom_kill", 0))
        high = int(events.get("high", 0))
        if oom != 0 or oom_kill != 0:
            violation_codes.add("cgroup_oom_observed")
        _integer(item.get("pid"), label="pid")
        names.add(name)
        restart_total += restart_count
        oom_killed_count += int(oom_killed)
        cgroup_oom_total += oom
        cgroup_oom_kill_total += oom_kill
        cgroup_high_total += high
        epochs.append(
            {
                "name": name,
                "id": container_id,
                "started_at_utc": started_at.isoformat(),
            }
        )
        if name == "mineru-api":
            api_rss = rss
            api_rss_hwm = rss_hwm
    if names != HOST_CONTAINER_NAMES or api_rss is None or api_rss_hwm is None:
        raise ValueError("host capacity container identities drifted")
    assert vm_total is not None and vm_available is not None
    epoch_bytes = json.dumps(
        sorted(epochs, key=lambda item: item["name"]),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return HostSampleValues(
        collector_sha256=expected_collector_sha256,
        windows_node_identity_sha256=expected_windows_node_identity_sha256,
        container_epoch_sha256=(
            "sha256:" + hashlib.sha256(epoch_bytes).hexdigest()
        ),
        container_count=len(containers),
        restart_count_total=restart_total,
        oom_killed_count=oom_killed_count,
        unsafe_container_count=unsafe_count,
        cgroup_oom_total=cgroup_oom_total,
        cgroup_oom_kill_total=cgroup_oom_kill_total,
        cgroup_high_total=cgroup_high_total,
        docker_vm_memory_total_bytes=vm_total,
        docker_vm_memory_available_bytes=vm_available,
        docker_memory_reserve_bytes=docker_memory_reserve_bytes,
        api_pid1_rss_bytes=api_rss,
        api_pid1_rss_hwm_bytes=api_rss_hwm,
        safety_violation_codes=tuple(sorted(violation_codes)),
    )


class MineruHostCapacitySampler:
    source = "host"
    cadence_seconds = 5.0

    def __init__(
        self,
        *,
        ssh_command: list[str],
        expected_collector_sha256: str,
        expected_windows_node_identity_sha256: str,
        docker_memory_reserve_bytes: int,
    ) -> None:
        self._ssh_command = list(ssh_command)
        self._collector_sha256 = expected_collector_sha256
        self._node_identity_sha256 = expected_windows_node_identity_sha256
        self._reserve = docker_memory_reserve_bytes

    def sample(self) -> HostSampleValues:
        completed = subprocess.run(
            [
                *self._ssh_command,
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                MINERU_WINDOWS_COLLECTOR_PATH,
                "-CapacitySample",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            payload: Any = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("host capacity sample is not JSON") from exc
        return project_host_capacity_sample(
            payload,
            expected_collector_sha256=self._collector_sha256,
            expected_windows_node_identity_sha256=self._node_identity_sha256,
            docker_memory_reserve_bytes=self._reserve,
        )


__all__ = [
    "MineruHostCapacitySampler",
    "build_host_observer_ssh_command",
    "project_host_capacity_sample",
]
