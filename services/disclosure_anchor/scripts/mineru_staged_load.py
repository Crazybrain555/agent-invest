"""Run the fixed DB-free 4/8/16 MinerU deployment load envelope.

This is an explicit operator gate, never a resident worker. It uses the exact
official Hybrid-medium writer path against frozen PDF copies, samples vLLM
metrics throughout each stage, stops on the first unsafe signal, removes all
temporary state, and writes one new-only private receipt.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any
import uuid

from disclosure_anchor.adapters.parsers.mineru_medium import (
    MinerUMediumDocumentParser,
    MinerUProcess,
)
from disclosure_anchor.adapters.parsers.mineru_medium.process import (
    terminate_active_mineru_processes,
)
from disclosure_anchor.adapters.parsers.pdf_page_probe import count_pdf_pages
from disclosure_anchor.adapters.runtime.mineru_canary import (
    canary_request_sha256,
    probe_mineru_served_model,
    run_mineru_multimodal_canary,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_API_INFERENCE_MAX_CONCURRENCY,
    MINERU_PROCESSING_WINDOW_SIZE,
    MINERU_STAGED_LOAD_MINIMUM_RUNAWAY_TIMEOUT_SECONDS,
    MINERU_WINDOWS_COLLECTOR_PATH,
    client_bundle_identity,
    verify_runtime_manifest_payload,
    writer_code_digest,
)
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorHealth,
    MinerUOrchestratorHealthClient,
    MinerUOrchestratorUnavailableError,
    wait_for_mineru_orchestrator_idle,
)
from disclosure_anchor.adapters.runtime.bounded_http import (
    BoundedHTTPProtocolError,
    BoundedHTTPTransportError,
    ThreadOwnedPersistentHTTPClient,
)
from disclosure_anchor.adapters.runtime.mineru_process_isolation import (
    active_disclosure_producers,
    mineru_api_temp_dirs,
    mineru_processes,
    process_snapshot,
)
from disclosure_anchor.application.ports.parser import ParserOptions


RECEIPT_SCHEMA = "mineru_staged_load_receipt.v7"
RECEIPT_SCHEMA_VERSION = 7
ADMISSION_ORDER_PROFILE = "copy-index-fifo.v1"
NOT_ADMITTED_ATOMIC_ABORT = "not_admitted_atomic_abort"
METRICS_OBSERVER_PROFILE = "metrics-observer.v1"
ORCHESTRATOR_OBSERVER_PROFILE = "orchestrator-observer.v1"
TASK_REGISTRY_SEMANTICS = "retained-terminal-gauges.v1"
STAGE_DOCUMENT_COUNTS = (4, 8, 16)
ORCHESTRATOR_INFERENCE_CONCURRENCY = MINERU_API_INFERENCE_MAX_CONCURRENCY
SAFETY_LIMITS_PROFILE = "whole-document-runaway-and-drain.v1"
ARM_INFERENCE_LIVENESS_PROFILE = "epoch-bound-multimodal-canary.v1"
ARM_INFERENCE_LIVENESS_SCHEMA = "mineru-arm-inference-liveness.v1"
ARM_BOUNDARY_CANARY_SCHEMA = "mineru-arm-boundary-canary.v1"
CAMPAIGN_EPOCH_SCHEMA = "mineru-campaign-service-epoch.v1"
MINIMUM_INPUT_PAGES = 7
MINIMUM_CORPUS_DOCUMENTS = 16
CORPUS_SCHEMA = "mineru_staged_corpus.v1"
WAITING_ABORT_THRESHOLD = 64.0
WAITING_ABORT_SECONDS = 30.0
METRICS_SAMPLE_INTERVAL_SECONDS = 1.0
ORCHESTRATOR_SAMPLE_INTERVAL_SECONDS = 0.25
HOST_CAPACITY_SAMPLE_INTERVAL_SECONDS = 5.0
HOST_CAPACITY_MAX_SAMPLE_GAP_SECONDS = 15.0
HOST_CAPACITY_TRANSPORT_TIMEOUT_SECONDS = 4.5
METRICS_LOGICAL_SAMPLE_TIMEOUT_SECONDS = 10.0
METRICS_TRANSPORT_ATTEMPT_TIMEOUT_SECONDS = 4.5
METRICS_TRANSPORT_ATTEMPTS = 2
MAX_TOLERATED_METRICS_SAMPLE_FAILURES_PER_STAGE = 1
_MAX_METRICS_BYTES = 2 * 1024 * 1024
_MAX_SAFE_DETAIL_CHARS = 500
_SAFE_DETAIL_TRUNCATION_MARKER = " ...[middle truncated]... "
_HOST_CONTAINER_NAMES = {
    "mineru-api",
    "mineru-api-proxy",
    "mineru-openai-server",
}
_SSH_HOST_RE = re.compile(r"^(?!-)[A-Za-z0-9.-]+$")
_SSH_USER_RE = re.compile(r"^(?!-)[A-Za-z0-9_.-]+$")
_METRIC_ALIASES = {
    "running": {
        "vllm:num_requests_running",
        "vllm_num_requests_running",
    },
    "waiting": {
        "vllm:num_requests_waiting",
        "vllm_num_requests_waiting",
    },
    "preemptions": {
        "vllm:num_preemptions_total",
        "vllm_num_preemptions_total",
    },
    "kv_cache": {
        "vllm:gpu_cache_usage_perc",
        "vllm_gpu_cache_usage_perc",
        "vllm:kv_cache_usage_perc",
        "vllm_kv_cache_usage_perc",
    },
}


@dataclass(frozen=True)
class MetricsSample:
    observed_seconds: float
    running: float
    waiting: float
    preemptions: float
    kv_cache: float

    def to_payload(self) -> dict[str, float]:
        return {
            "observed_seconds": round(self.observed_seconds, 3),
            "running": self.running,
            "waiting": self.waiting,
            "preemptions": self.preemptions,
            "kv_cache": self.kv_cache,
        }


@dataclass(frozen=True)
class FrozenCorpusInput:
    logical_name: str
    payload: bytes
    digest: str
    page_count: int
    workload_class: str

    def evidence(self) -> dict[str, object]:
        return {
            "logical_name": self.logical_name,
            "sha256": f"sha256:{self.digest}",
            "bytes": len(self.payload),
            "page_count": self.page_count,
            "workload_class": self.workload_class,
        }


class _StageAbortLatch:
    """Linearize monitor failure publication with API admission grants."""

    def __init__(self) -> None:
        self.condition = threading.Condition(threading.RLock())
        self._reason: str | None = None

    @property
    def reason(self) -> str | None:
        with self.condition:
            return self._reason

    def reason_locked(self) -> str | None:
        """Return the reason while the shared condition is already held."""

        return self._reason

    def publish_locked(self, reason: str) -> None:
        """Publish one sticky reason while the shared condition is held."""

        self._reason = self._reason or reason
        self.condition.notify_all()

    def publish(self, reason: str) -> None:
        with self.condition:
            self.publish_locked(reason)


class _StageAdmission:
    """Own one deterministic FIFO admission order and atomic abort closure."""

    def __init__(
        self,
        *,
        outstanding_window: int,
        copy_indices: tuple[int, ...],
        abort_latch: _StageAbortLatch | None = None,
    ) -> None:
        if outstanding_window < 1:
            raise ValueError("client outstanding window must be positive")
        if copy_indices != tuple(range(1, len(copy_indices) + 1)):
            raise ValueError("stage copy indices must be contiguous and one-based")
        if not copy_indices:
            raise ValueError("stage admission requires at least one copy index")
        self._window = outstanding_window
        self._copy_indices = copy_indices
        self._abort_latch = abort_latch or _StageAbortLatch()
        self._condition = self._abort_latch.condition
        self._pending = list(copy_indices)
        self._states = {copy_index: "pending" for copy_index in copy_indices}
        self._ordinals: dict[int, int] = {}
        self._admission_order: list[int] = []
        self._active = 0
        self._huge_active = False
        self._closed = False
        self._peak = 0

    @property
    def peak(self) -> int:
        with self._condition:
            return self._peak

    def evidence(self) -> dict[str, object]:
        with self._condition:
            return {
                "profile": ADMISSION_ORDER_PROFILE,
                "expected_copy_indices": list(self._copy_indices),
                "admission_order_copy_indices": list(self._admission_order),
                "records": [
                    {
                        "copy_index": copy_index,
                        "admission_ordinal": self._ordinals.get(copy_index),
                        "state": self._states[copy_index],
                    }
                    for copy_index in self._copy_indices
                ],
                "closed": self._closed,
                "abort_reason": self._abort_latch.reason_locked(),
            }

    def _close_locked(self) -> None:
        self._closed = True
        for copy_index in self._pending:
            self._states[copy_index] = NOT_ADMITTED_ATOMIC_ABORT
        self._pending.clear()
        self._condition.notify_all()

    def run(
        self,
        *,
        copy_index: int,
        workload_class: str,
        operation: Callable[[], dict[str, Any]],
        abort_reason: Callable[[], str | None] = lambda: None,
    ) -> dict[str, Any]:
        if copy_index not in self._states:
            raise ValueError("copy index is outside the frozen stage order")
        exclusive = workload_class == "huge"
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed
                or self._abort_latch.reason_locked() is not None
                or (
                    self._pending
                    and self._pending[0] == copy_index
                    and (
                        self._active == 0
                        if exclusive
                        else not self._huge_active and self._active < self._window
                    )
                )
            )
            latched_failure = self._abort_latch.reason_locked()
            if self._closed or latched_failure is not None:
                self._close_locked()
                raise _StageAdmissionClosedError(
                    "stage admission closed after an earlier failure"
                    + (
                        f": {_safe_detail(latched_failure)}"
                        if latched_failure is not None
                        else ""
                    ),
                    copy_index=copy_index,
                )
            # A monitor can latch failure while this caller is waiting for the
            # prior document to release the only local slot.  Re-check that
            # latch while still holding the admission condition so the waiter
            # cannot turn a newly freed token into another remote submission
            # before the coordinator's polling loop calls ``close()``.
            failure = abort_reason()
            if failure is not None:
                self._abort_latch.publish_locked(failure)
                self._close_locked()
                raise _StageAdmissionClosedError(
                    "stage admission closed by a latched monitor failure: "
                    f"{_safe_detail(failure)}",
                    copy_index=copy_index,
                )
            if not self._pending or self._pending[0] != copy_index:
                raise RuntimeError("stage admission FIFO ownership drifted")
            self._pending.pop(0)
            self._states[copy_index] = "admitted"
            self._ordinals[copy_index] = len(self._admission_order)
            self._admission_order.append(copy_index)
            self._active += 1
            self._huge_active = exclusive
            self._peak = max(self._peak, self._active)
        operation_error: Exception | None = None
        outcome: dict[str, Any] | None = None
        try:
            outcome = operation()
        except Exception as exc:
            operation_error = exc
        with self._condition:
            self._active -= 1
            if exclusive:
                self._huge_active = False
            if operation_error is not None:
                self._states[copy_index] = "failed"
                self._abort_latch.publish_locked(
                    f"document_operation_exception:{copy_index}:"
                    f"{type(operation_error).__name__}"
                )
                self._close_locked()
            elif outcome is None or outcome.get("status") != "pass":
                self._states[copy_index] = "failed"
                failure_class = (
                    outcome.get("failure_class", "unknown")
                    if outcome is not None
                    else "missing_outcome"
                )
                self._abort_latch.publish_locked(
                    f"document_failed:{copy_index}:{_safe_detail(str(failure_class))}"
                )
                self._close_locked()
            else:
                self._states[copy_index] = "completed"
                self._condition.notify_all()
        if operation_error is not None:
            raise operation_error
        assert outcome is not None
        return outcome

    def close(self) -> None:
        with self._condition:
            self._close_locked()


class _StageAdmissionClosedError(RuntimeError):
    """A queued stage document was cancelled after a peer failed."""

    def __init__(self, message: str, *, copy_index: int) -> None:
        super().__init__(message)
        self.copy_index = copy_index


def _private_host_observer_file(path: Path, *, label: str) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise ValueError(f"{label} must be an owner-only 0600 regular file")


def _host_observer_ssh_base(
    *,
    host: str,
    user: str,
    port: int,
    identity_file: Path,
    known_hosts_file: Path,
    control_path: Path | None = None,
) -> list[str]:
    if (
        _SSH_HOST_RE.fullmatch(host) is None
        or _SSH_USER_RE.fullmatch(user) is None
        or port != 22
    ):
        raise ValueError("host observer SSH destination is invalid")
    _private_host_observer_file(identity_file, label="host observer SSH identity")
    _private_host_observer_file(
        known_hosts_file,
        label="host observer known_hosts",
    )
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
        base64.b64decode(fields[2], validate=True)
    except ValueError as exc:
        raise ValueError("host observer key is not canonical base64") from exc
    command = [
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
    ]
    if control_path is not None:
        encoded = os.fsencode(control_path)
        if len(encoded) > 90:
            raise ValueError("host observer SSH ControlPath is too long")
        if control_path.exists() or control_path.is_symlink():
            raise ValueError("host observer SSH ControlPath must be new")
        parent = control_path.parent
        metadata = parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.getuid()
            or parent.is_symlink()
        ):
            raise ValueError("host observer SSH control directory is not private")
        command.extend(["-S", str(control_path)])
    command.extend(["--", f"{user}@{host}"])
    return command


class _HostObserverControlMaster:
    """Own one foreground pinned OpenSSH connection for the whole campaign."""

    def __init__(
        self,
        *,
        host: str,
        user: str,
        port: int,
        identity_file: Path,
        known_hosts_file: Path,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._host = host
        self._user = user
        self._port = port
        self._identity_file = identity_file
        self._known_hosts_file = known_hosts_file
        self._clock = monotonic_clock
        self._control_dir: Path | None = None
        self._control_path: Path | None = None
        self._base: list[str] | None = None
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self._process is not None or self._control_dir is not None:
            raise RuntimeError("host observer SSH ControlMaster already started")
        control_dir = Path(tempfile.mkdtemp(prefix="da-ssh-", dir="/tmp"))
        control_dir.chmod(0o700)
        control_path = control_dir / "m"
        self._control_dir = control_dir
        self._control_path = control_path
        try:
            base = _host_observer_ssh_base(
                host=self._host,
                user=self._user,
                port=self._port,
                identity_file=self._identity_file,
                known_hosts_file=self._known_hosts_file,
                control_path=control_path,
            )
            self._base = base
            master_command = [
                *base[:-2],
                "-M",
                "-N",
                "-T",
                "-o",
                "ControlPersist=no",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=3",
                *base[-2:],
            ]
            self._process = subprocess.Popen(
                master_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = self._clock() + 15.0
            while True:
                if self._process.poll() is not None:
                    raise RuntimeError(
                        "host observer SSH ControlMaster exited during startup"
                    )
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise RuntimeError(
                        "host observer SSH ControlMaster startup timed out"
                    )
                checked = subprocess.run(
                    [*base[:-2], "-O", "check", *base[-2:]],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=min(HOST_CAPACITY_TRANSPORT_TIMEOUT_SECONDS, remaining),
                )
                if checked.returncode == 0:
                    return
                time.sleep(min(0.1, max(0.0, remaining)))
        except Exception:
            self.close()
            raise

    def session_command(self) -> list[str]:
        if self._process is None or self._base is None or self._control_path is None:
            raise _HostCapacityTransportUnavailableError(
                "pinned Windows host-capacity ControlMaster is unavailable"
            )
        try:
            metadata = self._control_path.stat(follow_symlinks=False)
        except OSError as exc:
            raise _HostCapacityTransportUnavailableError(
                "pinned Windows host-capacity ControlMaster is unavailable"
            ) from exc
        if (
            self._process.poll() is not None
            or not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or self._control_path.is_symlink()
        ):
            raise _HostCapacityTransportUnavailableError(
                "pinned Windows host-capacity ControlMaster is unavailable"
            )
        return [
            *self._base[:-2],
            "-o",
            "ControlMaster=no",
            "-o",
            "ProxyCommand=/usr/bin/false",
            *self._base[-2:],
        ]

    def close(self) -> None:
        process = self._process
        base = self._base
        if process is not None and base is not None and process.poll() is None:
            try:
                subprocess.run(
                    [*base[:-2], "-O", "exit", *base[-2:]],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=HOST_CAPACITY_TRANSPORT_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
        control_dir = self._control_dir
        if control_dir is not None and control_dir.exists():
            metadata = control_dir.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.getuid()
                or control_dir.is_symlink()
            ):
                raise RuntimeError("host observer SSH control directory drifted")
            for child in control_dir.iterdir():
                if child.is_dir() and not child.is_symlink():
                    raise RuntimeError(
                        "host observer SSH control directory contains a directory"
                    )
                child.unlink(missing_ok=True)
            control_dir.rmdir()
        self._process = None
        self._base = None
        self._control_path = None
        self._control_dir = None


def _positive_int(value: object, *, label: str, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"host capacity {label} is invalid")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ValueError(f"host capacity {label} is invalid")
    return value


class _TrustedHostCapacityViolation(ValueError):
    """Safety violation carried with a structurally trusted collector sample."""

    def __init__(self, reason: str, *, sample: dict[str, Any]) -> None:
        super().__init__(reason)
        self.sample = sample


class _HostCapacityTransportUnavailableError(RuntimeError):
    """The pinned Windows collector could not be reached for one sample."""


def _validate_host_capacity_sample(
    payload: object,
    *,
    expected_collector_sha256: str,
    expected_windows_node_identity_sha256: str,
    docker_memory_reserve_bytes: int,
) -> dict[str, Any]:
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
    ):
        raise ValueError("host capacity sample identity drifted")
    observed_at = payload.get("observed_at_utc")
    try:
        parsed_observed_at = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("host capacity timestamp is invalid") from exc
    if parsed_observed_at.tzinfo is None:
        raise ValueError("host capacity timestamp is not aware")
    containers = payload.get("containers")
    if not isinstance(containers, list) or len(containers) != 3:
        raise ValueError("host capacity container set is incomplete")
    normalized: list[dict[str, Any]] = []
    safety_violations: list[str] = []
    names: set[str] = set()
    for item in containers:
        if not isinstance(item, dict) or set(item) != {
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
        }:
            raise ValueError("host capacity container fields drifted")
        name = item.get("name")
        container_id = item.get("id")
        if (
            not isinstance(name, str)
            or name in names
            or not isinstance(container_id, str)
            or re.fullmatch(r"[a-f0-9]{64}", container_id) is None
        ):
            raise ValueError("host capacity container identity is invalid")
        restart_count = _positive_int(
            item.get("restart_count"),
            label="restart_count",
            allow_zero=True,
        )
        exit_code = _positive_int(
            item.get("exit_code"),
            label="exit_code",
            allow_zero=True,
        )
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
        if (
            restart_count != 0
            or oom_killed
            or exit_code != 0
            or not running
            or status != "running"
            or health != "healthy"
        ):
            safety_violations.append(f"{name}:container_state_unsafe")
        try:
            started_at = datetime.fromisoformat(
                str(item.get("started_at_utc")).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError("host capacity container epoch is invalid") from exc
        if started_at.tzinfo is None:
            raise ValueError("host capacity container epoch is not aware")
        memory_current = _positive_int(
            item.get("memory_current_bytes"),
            label="memory_current_bytes",
            allow_zero=True,
        )
        memory_max_value = item.get("memory_max_bytes")
        memory_max = (
            None
            if memory_max_value is None
            else _positive_int(memory_max_value, label="memory_max_bytes")
        )
        if memory_max is not None and memory_current > memory_max:
            raise ValueError("host capacity cgroup memory exceeds its limit")
        rss = _positive_int(
            item.get("pid1_rss_bytes"),
            label="pid1_rss_bytes",
            allow_zero=True,
        )
        rss_hwm = _positive_int(
            item.get("pid1_rss_hwm_bytes"),
            label="pid1_rss_hwm_bytes",
            allow_zero=True,
        )
        if rss_hwm < rss:
            raise ValueError("host capacity RSS high-water mark is invalid")
        vm_total = _positive_int(
            item.get("docker_vm_memory_total_bytes"),
            label="docker_vm_memory_total_bytes",
        )
        vm_available = _positive_int(
            item.get("docker_vm_memory_available_bytes"),
            label="docker_vm_memory_available_bytes",
        )
        if vm_available > vm_total:
            raise ValueError("host capacity Docker VM memory values are invalid")
        if vm_available < docker_memory_reserve_bytes:
            safety_violations.append(f"{name}:docker_vm_memory_reserve_crossed")
        events = item.get("memory_events")
        if (
            not isinstance(events, dict)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in events.values()
            )
        ):
            raise ValueError("host capacity cgroup OOM evidence is invalid")
        if events.get("oom") != 0 or events.get("oom_kill") != 0:
            safety_violations.append(f"{name}:cgroup_oom_observed")
        _positive_int(item.get("pid"), label="pid")
        names.add(name)
        normalized.append(dict(item))
    if names != _HOST_CONTAINER_NAMES:
        raise ValueError("host capacity container identities drifted")
    result = dict(payload)
    result["containers"] = sorted(normalized, key=lambda item: str(item["name"]))
    if safety_violations:
        raise _TrustedHostCapacityViolation(
            ";".join(sorted(set(safety_violations))),
            sample=result,
        )
    return result


def _fetch_host_capacity_sample(
    ssh_command: list[str],
    *,
    expected_collector_sha256: str,
    expected_windows_node_identity_sha256: str,
    docker_memory_reserve_bytes: int,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [
                *ssh_command,
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
            timeout=HOST_CAPACITY_TRANSPORT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise _HostCapacityTransportUnavailableError(
            "pinned Windows host-capacity collector transport failed"
        ) from exc
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 255:
            raise _HostCapacityTransportUnavailableError(
                "pinned Windows host-capacity collector transport failed"
            ) from exc
        raise RuntimeError(
            "pinned Windows host-capacity collector command failed"
        ) from exc
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("host capacity sample is not JSON") from exc
    return _validate_host_capacity_sample(
        payload,
        expected_collector_sha256=expected_collector_sha256,
        expected_windows_node_identity_sha256=(
            expected_windows_node_identity_sha256
        ),
        docker_memory_reserve_bytes=docker_memory_reserve_bytes,
    )


class _HostCapacityMonitor:
    """Process-external Docker epoch/OOM/RSS observer for one whole replay."""

    def __init__(
        self,
        *,
        sampler: Callable[[], dict[str, Any]],
        collector_sha256: str,
        windows_node_identity_sha256: str,
        docker_memory_reserve_bytes: int,
        abort_latch: _StageAbortLatch | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sample_interval_seconds: float = HOST_CAPACITY_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        self._sampler = sampler
        self._collector_sha256 = collector_sha256
        self._node_identity = windows_node_identity_sha256
        self._reserve = docker_memory_reserve_bytes
        self._abort_latch = abort_latch or _StageAbortLatch()
        self._clock = monotonic_clock
        self._interval = sample_interval_seconds
        self._max_gap = max(
            HOST_CAPACITY_MAX_SAMPLE_GAP_SECONDS,
            sample_interval_seconds * 3,
        )
        self._started = monotonic_clock()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._samples: list[dict[str, Any]] = []
        self._violations: list[dict[str, Any]] = []
        self._sampling_failures: list[dict[str, Any]] = []
        self._epochs: dict[str, tuple[str, str]] | None = None
        self._failure: str | None = None
        self._admission_stop_reason: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    @property
    def admission_stop_reason(self) -> str | None:
        with self._lock:
            return self._failure or self._admission_stop_reason

    @property
    def observation_stop_reason(self) -> str | None:
        with self._lock:
            return self._admission_stop_reason

    def start(self) -> None:
        self._sample_once()
        with self._lock:
            initial_failure = self._failure or (
                "host_capacity_initial_sample_unavailable"
                if not self._samples
                else None
            )
        if initial_failure is not None:
            self._record_failure(initial_failure)
            raise RuntimeError(initial_failure)
        self._thread = threading.Thread(
            target=self._run,
            name="mineru-host-capacity-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=35)
            if self._thread.is_alive():
                self._record_failure("host_capacity_monitor_did_not_stop")
                return
        self._sample_once()

    def _run(self) -> None:
        next_sample = self._clock() + self._interval
        while not self._stop.wait(max(0.0, next_sample - self._clock())):
            self._sample_once()
            next_sample += self._interval
            now = self._clock()
            while next_sample <= now:
                next_sample += self._interval

    def _record_failure(self, failure: str) -> None:
        with self._abort_latch.condition:
            with self._lock:
                self._failure = self._failure or failure
                published = self._failure
            self._abort_latch.publish_locked(published)

    def _sample_once(self) -> None:
        try:
            sample = dict(self._sampler())
        except _TrustedHostCapacityViolation as exc:
            observed_seconds = max(0.0, self._clock() - self._started)
            failure = f"host_capacity_violation:{_safe_detail(str(exc))}"
            with self._abort_latch.condition:
                with self._lock:
                    sample_index = self._append_trusted_sample_locked(
                        dict(exc.sample),
                        observed_seconds=observed_seconds,
                    )
                    self._violations.append(
                        {
                            "observed_seconds": round(observed_seconds, 6),
                            "sample_index": sample_index,
                            "failure": failure,
                        }
                    )
                    self._failure = self._failure or failure
                    published = self._failure
                self._abort_latch.publish_locked(published)
        except _HostCapacityTransportUnavailableError as exc:
            observed_seconds = max(0.0, self._clock() - self._started)
            failure = (
                "host_capacity_transport_unavailable:"
                f"{type(exc).__name__}:{_safe_detail(str(exc))}"
            )
            with self._abort_latch.condition:
                with self._lock:
                    self._sampling_failures.append(
                        {
                            "observed_seconds": round(observed_seconds, 6),
                            "failure": failure,
                        }
                    )
                    self._admission_stop_reason = (
                        self._admission_stop_reason
                        or "host_capacity_observation_incomplete"
                    )
                    last_trusted_seconds = (
                        float(self._samples[-1]["observed_seconds"])
                        if self._samples
                        else None
                    )
                    if (
                        last_trusted_seconds is not None
                        and observed_seconds - last_trusted_seconds > self._max_gap
                    ):
                        self._failure = self._failure or "host_capacity_sample_gap"
                    published = self._failure or self._admission_stop_reason
                self._abort_latch.publish_locked(published)
        except Exception as exc:
            observed_seconds = max(0.0, self._clock() - self._started)
            failure = (
                "host_capacity_sample_failed:"
                f"{type(exc).__name__}:{_safe_detail(str(exc))}"
            )
            with self._abort_latch.condition:
                with self._lock:
                    self._sampling_failures.append(
                        {
                            "observed_seconds": round(observed_seconds, 6),
                            "failure": failure,
                        }
                    )
                    self._failure = self._failure or failure
                    published = self._failure
                self._abort_latch.publish_locked(published)
        else:
            observed_seconds = max(0.0, self._clock() - self._started)
            self._append_trusted_sample(
                sample,
                observed_seconds=observed_seconds,
            )

    def _append_trusted_sample(
        self,
        sample: dict[str, Any],
        *,
        observed_seconds: float,
    ) -> int:
        with self._abort_latch.condition:
            with self._lock:
                sample_index = self._append_trusted_sample_locked(
                    sample,
                    observed_seconds=observed_seconds,
                )
                failure = self._failure
            if failure is not None:
                self._abort_latch.publish_locked(failure)
            return sample_index

    def _append_trusted_sample_locked(
        self,
        sample: dict[str, Any],
        *,
        observed_seconds: float,
    ) -> int:
        sample["observed_seconds"] = round(observed_seconds, 6)
        epochs = {
            str(item["name"]): (
                str(item["id"]),
                str(item["started_at_utc"]),
            )
            for item in sample["containers"]
        }
        if self._samples:
            gap = observed_seconds - float(self._samples[-1]["observed_seconds"])
            if gap > self._max_gap:
                self._failure = self._failure or "host_capacity_sample_gap"
        if self._epochs is None:
            self._epochs = epochs
        elif epochs != self._epochs:
            self._failure = self._failure or "host_capacity_epoch_changed"
        self._samples.append(sample)
        return len(self._samples) - 1

    def evidence(self) -> dict[str, Any]:
        with self._lock:
            samples = [dict(item) for item in self._samples]
            violations = [dict(item) for item in self._violations]
            sampling_failures = [dict(item) for item in self._sampling_failures]
            failure = self._failure
            admission_stop_reason = self._admission_stop_reason
        max_api_rss = 0
        min_vm_available: int | None = None
        for sample in samples:
            for container in sample["containers"]:
                available = int(container["docker_vm_memory_available_bytes"])
                min_vm_available = (
                    available
                    if min_vm_available is None
                    else min(min_vm_available, available)
                )
                if container["name"] == "mineru-api":
                    max_api_rss = max(max_api_rss, int(container["pid1_rss_hwm_bytes"]))
        return {
            "schema": "mineru-host-capacity-evidence.v2",
            "status": (
                "pass"
                if failure is None
                and admission_stop_reason is None
                and not sampling_failures
                and len(samples) >= 2
                else "fail"
            ),
            "failure": failure
            or admission_stop_reason
            or (None if len(samples) >= 2 else "too_few_samples"),
            "sample_interval_seconds": self._interval,
            "max_sample_gap_seconds": self._max_gap,
            "docker_memory_reserve_bytes": self._reserve,
            "collector_path": MINERU_WINDOWS_COLLECTOR_PATH,
            "collector_sha256": self._collector_sha256,
            "windows_node_identity_sha256": self._node_identity,
            "samples": samples,
            "violations": violations,
            "sampling_failures": sampling_failures,
            "summary": {
                "sample_count": len(samples),
                "max_api_pid1_rss_hwm_bytes": max_api_rss,
                "min_docker_vm_memory_available_bytes": min_vm_available,
            },
        }

    def stable_epochs(self) -> dict[str, tuple[str, str]]:
        """Return the trusted current epochs only while admission is safe."""

        with self._lock:
            if (
                self._epochs is None
                or self._failure is not None
                or self._admission_stop_reason is not None
            ):
                raise RuntimeError("host capacity epoch is not currently trusted")
            return dict(self._epochs)

    def observed_seconds(self) -> float:
        return max(0.0, self._clock() - self._started)

    def campaign_epoch_source(
        self,
    ) -> tuple[str, str, dict[str, tuple[str, str]]]:
        return (
            self._collector_sha256,
            self._node_identity,
            self.stable_epochs(),
        )


def _campaign_epoch_payload(
    *,
    collector_sha256: str,
    windows_node_identity_sha256: str,
    epochs: dict[str, tuple[str, str]],
) -> dict[str, object]:
    services: dict[str, dict[str, str]] = {}
    for role, name in (
        ("proxy", "mineru-api-proxy"),
        ("inference", "mineru-openai-server"),
    ):
        try:
            container_id, started_at_utc = epochs[name]
        except KeyError as exc:
            raise RuntimeError(f"campaign epoch is missing {name}") from exc
        services[role] = {
            "name": name,
            "container_id": container_id,
            "started_at_utc": started_at_utc,
        }
    canonical = {
        "schema": CAMPAIGN_EPOCH_SCHEMA,
        "windows_node_identity_sha256": windows_node_identity_sha256,
        "collector_sha256": collector_sha256,
        "services": services,
    }
    observed_sha256 = "sha256:" + hashlib.sha256(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        **canonical,
        "observed_sha256": observed_sha256,
    }


def _campaign_epoch_evidence(
    host_monitor: _HostCapacityMonitor,
    *,
    expected_sha256: str,
) -> dict[str, object]:
    collector_sha256, windows_node_identity_sha256, epochs = (
        host_monitor.campaign_epoch_source()
    )
    return {
        **_campaign_epoch_payload(
            collector_sha256=collector_sha256,
            windows_node_identity_sha256=windows_node_identity_sha256,
            epochs=epochs,
        ),
        "expected_sha256": expected_sha256,
    }


def _campaign_inference_epoch(
    campaign_epoch: dict[str, object],
) -> dict[str, str]:
    services = campaign_epoch.get("services")
    inference = services.get("inference") if isinstance(services, dict) else None
    if (
        not isinstance(inference, dict)
        or set(inference) != {"name", "container_id", "started_at_utc"}
        or inference.get("name") != "mineru-openai-server"
        or not isinstance(inference.get("container_id"), str)
        or not isinstance(inference.get("started_at_utc"), str)
    ):
        raise RuntimeError("campaign inference epoch is invalid")
    return {
        "name": "mineru-openai-server",
        "container_id": str(inference["container_id"]),
        "started_at_utc": str(inference["started_at_utc"]),
    }


def _run_arm_boundary_canary(
    observability_url: str,
    *,
    expected_model_id: str,
    runtime_identity_sha256: str,
    campaign_epoch: dict[str, object],
    phase: str,
    host_monitor: _HostCapacityMonitor,
) -> tuple[dict[str, object], Exception | None]:
    campaign_epoch_sha256 = str(campaign_epoch["observed_sha256"])
    inference_epoch = _campaign_inference_epoch(campaign_epoch)
    started_at_utc = datetime.now(UTC).isoformat()
    started_observed_seconds = host_monitor.observed_seconds()
    record: dict[str, object] = {
        "schema": ARM_BOUNDARY_CANARY_SCHEMA,
        "phase": phase,
        "status": "not_run",
        "started_at_utc": started_at_utc,
        "finished_at_utc": None,
        "started_observed_seconds": round(started_observed_seconds, 6),
        "finished_observed_seconds": None,
        "elapsed_seconds": None,
        "model_id": expected_model_id,
        "attempts": 1,
        "observability_endpoint_sha256": (
            "sha256:" + _endpoint_sha256(observability_url)
        ),
        "request_sha256": "sha256:" + canary_request_sha256(expected_model_id),
        "response_sha256": [],
        "runtime_manifest_identity_sha256": runtime_identity_sha256,
        "campaign_epoch_sha256": campaign_epoch_sha256,
        "inference_epoch": inference_epoch,
        "failure": "not_run",
    }
    try:
        evidence = run_mineru_multimodal_canary(
            observability_url,
            attempts=1,
            expected_model_id=expected_model_id,
        )
    except Exception as exc:
        finished_observed_seconds = host_monitor.observed_seconds()
        record.update(
            {
                "status": "fail",
                "finished_at_utc": datetime.now(UTC).isoformat(),
                "finished_observed_seconds": round(
                    finished_observed_seconds, 6
                ),
                "elapsed_seconds": round(
                    finished_observed_seconds - started_observed_seconds, 6
                ),
                "failure": f"{type(exc).__name__}:{_safe_detail(str(exc))}",
            }
        )
        return record, exc
    finished_observed_seconds = host_monitor.observed_seconds()
    record.update(
        {
            "status": "pass",
            "finished_at_utc": datetime.now(UTC).isoformat(),
            "finished_observed_seconds": round(finished_observed_seconds, 6),
            "elapsed_seconds": round(
                finished_observed_seconds - started_observed_seconds, 6
            ),
            "model_id": evidence.model_id,
            "attempts": evidence.attempts,
            "request_sha256": "sha256:" + evidence.request_sha256,
            "response_sha256": [
                "sha256:" + digest for digest in evidence.response_sha256
            ],
            "failure": None,
        }
    )
    return record, None


def _not_run_arm_boundary_canary(
    *,
    observability_url: str,
    expected_model_id: str,
    runtime_identity_sha256: str,
    campaign_epoch: dict[str, object],
    phase: str,
) -> dict[str, object]:
    inference_epoch = _campaign_inference_epoch(campaign_epoch)
    return {
        "schema": ARM_BOUNDARY_CANARY_SCHEMA,
        "phase": phase,
        "status": "not_run",
        "started_at_utc": None,
        "finished_at_utc": None,
        "started_observed_seconds": None,
        "finished_observed_seconds": None,
        "elapsed_seconds": None,
        "model_id": expected_model_id,
        "attempts": 0,
        "observability_endpoint_sha256": (
            "sha256:" + _endpoint_sha256(observability_url)
        ),
        "request_sha256": "sha256:" + canary_request_sha256(expected_model_id),
        "response_sha256": [],
        "runtime_manifest_identity_sha256": runtime_identity_sha256,
        "campaign_epoch_sha256": campaign_epoch.get("observed_sha256"),
        "inference_epoch": inference_epoch,
        "failure": "not_run",
    }


def _select_stage_inputs(
    corpus: tuple[FrozenCorpusInput, ...],
    *,
    document_count: int,
) -> tuple[FrozenCorpusInput, ...]:
    if document_count not in STAGE_DOCUMENT_COUNTS:
        raise ValueError("MinerU staged document count is not approved")
    if len(corpus) < document_count:
        raise ValueError("MinerU staged corpus is smaller than the requested stage")
    selected: list[FrozenCorpusInput] = []
    selected_hashes: set[str] = set()
    for workload_class in ("regular", "heavy", "huge"):
        item = next(
            (
                candidate
                for candidate in corpus
                if candidate.workload_class == workload_class
            ),
            None,
        )
        if item is None:
            raise ValueError(f"staged corpus has no {workload_class} PDF")
        selected.append(item)
        selected_hashes.add(item.digest)
    for item in corpus:
        if len(selected) >= document_count:
            break
        if item.digest not in selected_hashes:
            selected.append(item)
            selected_hashes.add(item.digest)
    if len(selected) != document_count:
        raise ValueError("staged corpus cannot fill the exact selected set")
    return tuple(selected)


@dataclass(frozen=True)
class MetricsSamplingFailure:
    observed_seconds: float
    duration_seconds: float
    failure: str

    def to_payload(self) -> dict[str, float | str]:
        return {
            "observed_seconds": round(self.observed_seconds, 6),
            "duration_seconds": round(self.duration_seconds, 6),
            "failure": self.failure,
        }


class MetricsTransportUnavailableError(RuntimeError):
    """One logical metrics sample exhausted only its transport budget."""


@dataclass(frozen=True)
class OrchestratorSamplingFailure:
    observed_seconds: float
    duration_seconds: float
    failure: str

    def to_payload(self) -> dict[str, float | str]:
        return {
            "observed_seconds": round(self.observed_seconds, 6),
            "duration_seconds": round(self.duration_seconds, 6),
            "failure": self.failure,
        }


@dataclass(frozen=True)
class OrchestratorSample:
    observed_seconds: float
    queued_tasks: int
    processing_tasks: int
    completed_tasks: int
    failed_tasks: int

    @classmethod
    def from_health(
        cls,
        health: MinerUOrchestratorHealth,
        *,
        observed_seconds: float,
    ) -> "OrchestratorSample":
        return cls(
            observed_seconds=observed_seconds,
            queued_tasks=health.queued_tasks,
            processing_tasks=health.processing_tasks,
            completed_tasks=health.completed_tasks,
            failed_tasks=health.failed_tasks,
        )

    def to_payload(self) -> dict[str, float | int]:
        return {
            "observed_seconds": round(self.observed_seconds, 6),
            "queued_tasks": self.queued_tasks,
            "processing_tasks": self.processing_tasks,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
        }


class _OrchestratorMonitor:
    """Continuously sample the fixed API queue while one stage is active."""

    def __init__(
        self,
        *,
        sampler: Callable[[], MinerUOrchestratorHealth],
        task_slots: int,
        client_outstanding_window: int,
        abort_latch: _StageAbortLatch | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sample_interval_seconds: float = ORCHESTRATOR_SAMPLE_INTERVAL_SECONDS,
        sampler_close: Callable[[], None] | None = None,
    ) -> None:
        self._sampler = sampler
        self._task_slots = task_slots
        self._client_outstanding_window = client_outstanding_window
        self._abort_latch = abort_latch or _StageAbortLatch()
        self._monotonic_clock = monotonic_clock
        self._sample_interval_seconds = sample_interval_seconds
        self._sampler_close = sampler_close
        self._started = monotonic_clock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="mineru-staged-load-orchestrator",
            daemon=True,
        )
        self._lock = threading.Lock()
        self._samples: list[OrchestratorSample] = []
        self._sampling_failures: list[OrchestratorSamplingFailure] = []
        self._failure: str | None = None
        self._admission_stop_reason: str | None = None
        self._state = "STARTING"
        self._transitions: list[dict[str, object]] = []
        self._thread_started = False
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread.start()
        self._thread_started = True
        if not self._ready.wait(timeout=max(16.0, self._sample_interval_seconds * 2)):
            self._publish_hard_failure("orchestrator_monitor_initial_sample_timeout")
            raise RuntimeError("orchestrator monitor initial sample timed out")
        with self._lock:
            initial_failure = self._failure
            initial_transport_failure = bool(self._sampling_failures)
            has_sample = bool(self._samples)
        if initial_failure is not None:
            raise RuntimeError(initial_failure)
        if initial_transport_failure or not has_sample:
            raise RuntimeError("orchestrator initial transport sample unavailable")

    def stop(self) -> None:
        if not self._thread_started:
            return
        self._stop.set()
        self._thread.join(timeout=max(16.0, self._sample_interval_seconds * 2))
        if self._thread.is_alive():
            self._publish_hard_failure("orchestrator_monitor_did_not_stop")
            return
        with self._lock:
            self._transition_locked(
                "CLOSED",
                reason="monitor_stopped",
                observed_seconds=max(
                    0.0,
                    self._monotonic_clock() - self._started,
                ),
            )

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    @property
    def samples(self) -> tuple[OrchestratorSample, ...]:
        with self._lock:
            return tuple(self._samples)

    @property
    def sampling_failures(self) -> tuple[OrchestratorSamplingFailure, ...]:
        with self._lock:
            return tuple(self._sampling_failures)

    @property
    def admission_stop_reason(self) -> str | None:
        with self._lock:
            return self._failure or self._admission_stop_reason

    @property
    def observation_stop_reason(self) -> str | None:
        with self._lock:
            return self._admission_stop_reason

    @property
    def evidence_failure(self) -> str | None:
        with self._lock:
            if self._failure is not None:
                return None
            if self._sampling_failures:
                return "orchestrator_observation_incomplete"
            return None

    def evidence(self) -> dict[str, object]:
        with self._lock:
            complete = bool(
                self._state == "CLOSED"
                and self._failure is None
                and not self._sampling_failures
            )
            return {
                "profile": ORCHESTRATOR_OBSERVER_PROFILE,
                "state": self._state,
                "observation_complete": complete,
                "hard_failure": self._failure,
                "admission_stop_reason": self._admission_stop_reason,
                "transitions": [dict(item) for item in self._transitions],
            }

    def _transition_locked(
        self,
        state: str,
        *,
        reason: str,
        observed_seconds: float,
    ) -> None:
        if state == self._state:
            return
        previous = self._state
        self._state = state
        self._transitions.append(
            {
                "from": previous,
                "to": state,
                "reason": reason,
                "observed_seconds": round(observed_seconds, 6),
            }
        )

    def _publish_hard_failure(self, failure: str) -> None:
        with self._abort_latch.condition:
            with self._lock:
                self._failure = self._failure or failure
                self._transition_locked(
                    "CLOSED",
                    reason=self._failure,
                    observed_seconds=max(
                        0.0,
                        self._monotonic_clock() - self._started,
                    ),
                )
                published = self._failure
            self._abort_latch.publish_locked(published)

    def _record_transport_failure(
        self,
        exc: MinerUOrchestratorUnavailableError,
        *,
        sample_started: float,
    ) -> None:
        now = self._monotonic_clock()
        observed = OrchestratorSamplingFailure(
            observed_seconds=max(0.0, now - self._started),
            duration_seconds=max(0.0, now - sample_started),
            failure=f"{type(exc).__name__}:{_safe_detail(str(exc))}",
        )
        with self._abort_latch.condition:
            with self._lock:
                self._sampling_failures.append(observed)
                self._admission_stop_reason = (
                    self._admission_stop_reason
                    or "orchestrator_observation_incomplete"
                )
                self._transition_locked(
                    "DEGRADED_TRANSPORT",
                    reason="orchestrator_transport_unavailable",
                    observed_seconds=observed.observed_seconds,
                )
                published = self._admission_stop_reason
            self._abort_latch.publish_locked(published)

    def _run(self) -> None:
        try:
            keep_sampling = self._observe_once()
            self._ready.set()
            next_sample = self._monotonic_clock() + self._sample_interval_seconds
            while keep_sampling and not self._stop.is_set():
                if self._stop.wait(
                    max(0.0, next_sample - self._monotonic_clock())
                ):
                    break
                keep_sampling = self._observe_once()
                next_sample += self._sample_interval_seconds
                now = self._monotonic_clock()
                while next_sample <= now:
                    next_sample += self._sample_interval_seconds
        finally:
            self._ready.set()
            if self._sampler_close is not None:
                try:
                    self._sampler_close()
                except Exception:
                    self._publish_hard_failure(
                        "orchestrator_monitor_transport_close_failed"
                    )

    def _observe_once(self) -> bool:
        sample_started = self._monotonic_clock()
        try:
            health = self._sampler()
            sample = OrchestratorSample.from_health(
                health,
                observed_seconds=max(
                    0.0,
                    self._monotonic_clock() - self._started,
                ),
            )
            if sample.processing_tasks > self._task_slots:
                failure = "orchestrator_processing_exceeded_attested_slots"
            elif (
                sample.queued_tasks + sample.processing_tasks
                > self._client_outstanding_window
            ):
                failure = "orchestrator_active_exceeded_client_window"
            else:
                failure = None
            with self._lock:
                self._samples.append(sample)
                self._transition_locked(
                    "HEALTHY" if failure is None else "CLOSED",
                    reason=(failure or "valid_orchestrator_sample"),
                    observed_seconds=sample.observed_seconds,
                )
            if failure is not None:
                self._publish_hard_failure(failure)
                return False
            return True
        except MinerUOrchestratorUnavailableError as exc:
            self._record_transport_failure(exc, sample_started=sample_started)
            return True
        except Exception as exc:
            self._publish_hard_failure(
                "orchestrator_health_invalid:"
                f"{type(exc).__name__}:{_safe_detail(str(exc))}"
            )
            return False


class _MetricsMonitor:
    def __init__(
        self,
        *,
        sampler: Callable[[], MetricsSample],
        expected_preemptions: float,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sample_interval_seconds: float = METRICS_SAMPLE_INTERVAL_SECONDS,
        sampler_close: Callable[[], None] | None = None,
    ) -> None:
        self._sampler = sampler
        self._expected_preemptions = expected_preemptions
        self._monotonic_clock = monotonic_clock
        self._sample_interval_seconds = sample_interval_seconds
        self._sampler_close = sampler_close
        self._started = monotonic_clock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="mineru-staged-load-metrics",
            daemon=True,
        )
        self._lock = threading.Lock()
        self._samples: list[MetricsSample] = []
        self._sampling_failures: list[MetricsSamplingFailure] = []
        self._failure: str | None = None
        self._state = "STARTING"
        self._transitions: list[dict[str, object]] = []
        self._waiting_since: float | None = None
        self._terminal_sample_observed_seconds: float | None = None
        self._thread_started = False
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread.start()
        self._thread_started = True
        if not self._ready.wait(timeout=max(12.0, self._sample_interval_seconds * 2)):
            with self._lock:
                self._failure = self._failure or "metrics_monitor_initial_sample_timeout"
            raise RuntimeError("metrics monitor initial sample timed out")
        with self._lock:
            initial_failure = self._failure
            initial_transport_failure = bool(self._sampling_failures)
            has_sample = bool(self._samples)
        if initial_failure is not None:
            raise RuntimeError(initial_failure)
        if initial_transport_failure or not has_sample:
            raise RuntimeError("metrics initial transport sample unavailable")

    def stop(self) -> None:
        if not self._thread_started:
            return
        self._stop.set()
        self._thread.join(timeout=max(12.0, self._sample_interval_seconds * 2))
        if self._thread.is_alive():
            with self._lock:
                self._failure = self._failure or "metrics_monitor_did_not_stop"
                self._transition_locked(
                    "CLOSED",
                    reason="monitor_thread_did_not_stop",
                    observed_seconds=max(
                        0.0,
                        self._monotonic_clock() - self._started,
                    ),
                )
            return
        with self._lock:
            self._transition_locked(
                "CLOSED",
                reason="monitor_stopped",
                observed_seconds=max(
                    0.0,
                    self._monotonic_clock() - self._started,
                ),
            )

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    @property
    def samples(self) -> tuple[MetricsSample, ...]:
        with self._lock:
            return tuple(self._samples)

    @property
    def sampling_failures(self) -> tuple[MetricsSamplingFailure, ...]:
        with self._lock:
            return tuple(self._sampling_failures)

    @property
    def terminal_sample_observed_seconds(self) -> float | None:
        with self._lock:
            return self._terminal_sample_observed_seconds

    @property
    def evidence_failure(self) -> str | None:
        with self._lock:
            if self._failure is not None:
                return None
            if self._sampling_failures or self._terminal_sample_observed_seconds is None:
                return "metrics_observation_incomplete"
            return None

    def evidence(self) -> dict[str, object]:
        with self._lock:
            complete = bool(
                self._state == "CLOSED"
                and self._failure is None
                and not self._sampling_failures
                and self._terminal_sample_observed_seconds is not None
            )
            return {
                "profile": METRICS_OBSERVER_PROFILE,
                "state": self._state,
                "observation_complete": complete,
                "hard_failure": self._failure,
                "transitions": [dict(item) for item in self._transitions],
            }

    def _transition_locked(
        self,
        state: str,
        *,
        reason: str,
        observed_seconds: float,
    ) -> None:
        if state == self._state:
            return
        previous = self._state
        self._state = state
        self._transitions.append(
            {
                "from": previous,
                "to": state,
                "reason": reason,
                "observed_seconds": round(observed_seconds, 6),
            }
        )

    def _run(self) -> None:
        try:
            keep_sampling = self._observe_once()
            self._ready.set()
            next_sample = self._monotonic_clock() + self._sample_interval_seconds
            while keep_sampling and not self._stop.is_set():
                if self._stop.wait(
                    max(0.0, next_sample - self._monotonic_clock())
                ):
                    break
                keep_sampling = self._observe_once()
                next_sample += self._sample_interval_seconds
                now = self._monotonic_clock()
                while next_sample <= now:
                    next_sample += self._sample_interval_seconds
            if self._stop.is_set() and self.failure is None:
                self._observe_once(terminal=True)
        finally:
            self._ready.set()
            if self._sampler_close is not None:
                try:
                    self._sampler_close()
                except Exception:
                    with self._lock:
                        self._failure = (
                            self._failure or "metrics_monitor_transport_close_failed"
                        )

    def _observe_once(
        self,
        *,
        terminal: bool = False,
    ) -> bool:
        sample_started = self._monotonic_clock()
        try:
            raw = self._sampler()
            now = self._monotonic_clock()
            sample = MetricsSample(
                observed_seconds=max(0.0, now - self._started),
                running=raw.running,
                waiting=raw.waiting,
                preemptions=raw.preemptions,
                kv_cache=raw.kv_cache,
            )
            failure, waiting_since = metric_abort_reason(
                sample,
                baseline_preemptions=self._expected_preemptions,
                waiting_since=self._waiting_since,
                observed_at=now,
            )
            with self._lock:
                self._waiting_since = waiting_since
                self._samples.append(sample)
                self._transition_locked(
                    "HEALTHY",
                    reason="valid_metrics_sample",
                    observed_seconds=sample.observed_seconds,
                )
                if terminal:
                    self._terminal_sample_observed_seconds = sample.observed_seconds
                if failure is not None:
                    self._failure = failure
            return failure is None
        except MetricsTransportUnavailableError as exc:
            now = self._monotonic_clock()
            observed = MetricsSamplingFailure(
                observed_seconds=max(0.0, now - self._started),
                duration_seconds=max(0.0, now - sample_started),
                failure=f"{type(exc).__name__}:{_safe_detail(str(exc))}",
            )
            with self._lock:
                self._sampling_failures.append(observed)
                self._transition_locked(
                    "DEGRADED_TRANSPORT",
                    reason="metrics_transport_unavailable",
                    observed_seconds=observed.observed_seconds,
                )
            return True
        except Exception as exc:
            now = self._monotonic_clock()
            observed = MetricsSamplingFailure(
                observed_seconds=max(0.0, now - self._started),
                duration_seconds=max(0.0, now - sample_started),
                failure=f"{type(exc).__name__}:{_safe_detail(str(exc))}",
            )
            with self._lock:
                self._sampling_failures.append(observed)
                self._transition_locked(
                    "DEGRADED_TRANSPORT",
                    reason="metrics_payload_invalid",
                    observed_seconds=observed.observed_seconds,
                )
            return True


def parse_vllm_metrics(payload: bytes) -> MetricsSample:
    """Parse the exact four safety signals from Prometheus text."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("vLLM metrics are not UTF-8") from exc
    values: dict[str, list[float]] = {name: [] for name in _METRIC_ALIASES}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        metric_name = fields[0].partition("{")[0]
        matched = next(
            (
                name
                for name, aliases in _METRIC_ALIASES.items()
                if metric_name in aliases
            ),
            None,
        )
        if matched is None:
            continue
        try:
            value = float(fields[1])
        except ValueError as exc:
            raise ValueError(f"vLLM metric {metric_name} is not numeric") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"vLLM metric {metric_name} is invalid")
        values[matched].append(value)
    missing = sorted(name for name, samples in values.items() if not samples)
    if missing:
        raise ValueError(f"vLLM metrics missing required signals: {','.join(missing)}")
    return MetricsSample(
        observed_seconds=0.0,
        running=sum(values["running"]),
        waiting=sum(values["waiting"]),
        preemptions=sum(values["preemptions"]),
        kv_cache=max(values["kv_cache"]),
    )


def metric_abort_reason(
    sample: MetricsSample,
    *,
    baseline_preemptions: float,
    waiting_since: float | None,
    observed_at: float,
) -> tuple[str | None, float | None]:
    if sample.preemptions != baseline_preemptions:
        return "preemption_counter_changed", waiting_since
    if sample.waiting < WAITING_ABORT_THRESHOLD:
        return None, None
    if waiting_since is None:
        return None, observed_at
    if observed_at - waiting_since >= WAITING_ABORT_SECONDS:
        return "waiting_gte_64_for_30_seconds", waiting_since
    return None, waiting_since


class _PersistentVLLMMetricsClient:
    """Thread-owned persistent transport for bounded vLLM metrics samples."""

    def __init__(
        self,
        server_url: str,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        root = server_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3].rstrip("/")
        self._clock = monotonic_clock
        self._transport = ThreadOwnedPersistentHTTPClient(
            root,
            maximum_response_bytes=_MAX_METRICS_BYTES,
            monotonic_clock=monotonic_clock,
        )

    def fetch(self) -> MetricsSample:
        try:
            status, payload = self._transport.get_bytes(
                "/metrics",
                timeout_seconds=METRICS_LOGICAL_SAMPLE_TIMEOUT_SECONDS,
                transport_attempts=METRICS_TRANSPORT_ATTEMPTS,
                maximum_attempt_timeout_seconds=(
                    METRICS_TRANSPORT_ATTEMPT_TIMEOUT_SECONDS
                ),
            )
        except BoundedHTTPTransportError as exc:
            raise MetricsTransportUnavailableError(
                "cannot read vLLM metrics inside the logical transport budget"
            ) from exc
        except BoundedHTTPProtocolError as exc:
            raise RuntimeError("vLLM metrics response violates safety limits") from exc
        if not 200 <= status < 300:
            raise RuntimeError(f"cannot read vLLM metrics: HTTP {status}")
        sample = parse_vllm_metrics(payload)
        return sample

    def close(self) -> None:
        self._transport.close()


def fetch_vllm_metrics(
    server_url: str,
    *,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> MetricsSample:
    client = _PersistentVLLMMetricsClient(
        server_url,
        monotonic_clock=monotonic_clock,
    )
    try:
        return client.fetch()
    finally:
        client.close()


def execute_fixed_stage_sequence(
    stage_runner: Callable[[int], dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for document_count in STAGE_DOCUMENT_COUNTS:
        result = stage_runner(document_count)
        results.append(result)
        if result.get("status") != "pass":
            break
    return results


def _reclassify_cancelled_after_termination(
    outcome: dict[str, Any],
    *,
    termination_requested: bool,
) -> None:
    """Attach stage-abort causality only after the coordinator terminated."""

    if termination_requested and outcome.get("failure_detail", "").startswith(
        "ParserCancelledError:"
    ):
        outcome["status"] = "cancelled_after_stage_abort"
        outcome["failure_class"] = "stage_abort"


def _run_stage(
    *,
    document_count: int,
    run_root: Path,
    corpus: tuple[FrozenCorpusInput, ...],
    mineru_bin: Path,
    api_url: str,
    inference_upstream_url: str,
    runtime_identity: str,
    document_runaway_timeout_seconds: int,
    api_drain_timeout_seconds: int,
    expected_preemptions: float,
    metrics_sampler: Callable[[], MetricsSample],
    orchestrator_sampler: Callable[[], MinerUOrchestratorHealth],
    orchestrator_idle_waiter: Callable[[], tuple[MinerUOrchestratorHealth, float]],
    task_slots: int,
    metrics_monitor_sampler: Callable[[], MetricsSample] | None = None,
    metrics_monitor_close: Callable[[], None] | None = None,
    orchestrator_monitor_close: Callable[[], None] | None = None,
    host_failure: Callable[[], str | None] = lambda: None,
    host_observation_stop: Callable[[], str | None] = lambda: None,
    abort_latch: _StageAbortLatch | None = None,
) -> dict[str, Any]:
    if document_count not in STAGE_DOCUMENT_COUNTS:
        raise ValueError("MinerU staged load concurrency is not an approved stage")
    stage_inputs = _select_stage_inputs(corpus, document_count=document_count)
    # The MinerU async API has no remote cancellation endpoint.  Keep any
    # backlog in local durable/admission state so a host-safety failure cannot
    # leave a queued remote document starting after admission closes.
    client_outstanding_window = min(document_count, task_slots)
    stage_abort_latch = abort_latch or _StageAbortLatch()
    admission = _StageAdmission(
        outstanding_window=client_outstanding_window,
        copy_indices=tuple(range(1, document_count + 1)),
        abort_latch=stage_abort_latch,
    )
    stage_started = time.monotonic()
    orchestrator_baseline, preflight_drain_seconds = orchestrator_idle_waiter()
    if orchestrator_baseline.active_tasks != 0:
        raise RuntimeError("MinerU orchestrator idle waiter returned active tasks")
    metrics_baseline = metrics_sampler()
    if metrics_baseline.preemptions != expected_preemptions:
        return _stage_preflight_failure(
            concurrency=document_count,
            started=stage_started,
            metrics_baseline=metrics_baseline,
            orchestrator_baseline=orchestrator_baseline,
            preflight_drain_seconds=preflight_drain_seconds,
            failure="preemption_counter_changed_between_stages",
            task_slots=task_slots,
        )
    if metrics_baseline.running != 0 or metrics_baseline.waiting != 0:
        return _stage_preflight_failure(
            concurrency=document_count,
            started=stage_started,
            metrics_baseline=metrics_baseline,
            orchestrator_baseline=orchestrator_baseline,
            preflight_drain_seconds=preflight_drain_seconds,
            failure="stage_remote_baseline_not_idle",
            task_slots=task_slots,
        )
    metrics_monitor = _MetricsMonitor(
        sampler=metrics_monitor_sampler or metrics_sampler,
        expected_preemptions=expected_preemptions,
        sampler_close=metrics_monitor_close,
    )
    orchestrator_monitor = _OrchestratorMonitor(
        sampler=orchestrator_sampler,
        task_slots=task_slots,
        client_outstanding_window=client_outstanding_window,
        abort_latch=stage_abort_latch,
        sampler_close=orchestrator_monitor_close,
    )

    def current_monitor_stop() -> tuple[str | None, bool]:
        hard_failure = (
            metrics_monitor.failure
            or orchestrator_monitor.failure
            or host_failure()
        )
        if hard_failure is not None:
            return hard_failure, True
        observation_stop = (
            orchestrator_monitor.observation_stop_reason
            or host_observation_stop()
        )
        return observation_stop, False

    def current_abort_reason() -> str | None:
        monitor_reason, _monitor_is_hard = current_monitor_stop()
        failure = stage_abort_latch.reason or monitor_reason
        if failure is not None:
            stage_abort_latch.publish(failure)
        return stage_abort_latch.reason

    outcomes: dict[int, dict[str, Any]] = {
        index: {
            "copy_index": index,
            "logical_name": stage_input.logical_name,
            "input_sha256": f"sha256:{stage_input.digest}",
            "workload_class": stage_input.workload_class,
            "status": "not_started",
        }
        for index, stage_input in enumerate(stage_inputs, start=1)
    }
    failure: str | None = None
    failure_origin: str | None = None
    orchestrator_terminal: MinerUOrchestratorHealth | None = None
    terminal_drain_seconds: float | None = None
    stage_tree: Path | None = None
    api_temp_before = mineru_api_temp_dirs()
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"stage-{document_count:02d}-",
            dir=run_root,
        ) as tmp:
            stage_tree = Path(tmp)
            metrics_monitor.start()
            orchestrator_monitor.start()
            with ThreadPoolExecutor(
                max_workers=document_count,
                thread_name_prefix=f"mineru-stage-{document_count}",
            ) as executor:
                futures: dict[Future[dict[str, Any]], int] = {
                    executor.submit(
                        _parse_admitted_copy,
                        admission=admission,
                        abort_reason=current_abort_reason,
                        workload_class=stage_input.workload_class,
                        copy_index=index,
                        stage_root=stage_tree,
                        input_bytes=stage_input.payload,
                        input_digest=stage_input.digest,
                        input_logical_name=stage_input.logical_name,
                        mineru_bin=mineru_bin,
                        api_url=api_url,
                        inference_upstream_url=inference_upstream_url,
                        runtime_identity=runtime_identity,
                        document_runaway_timeout_seconds=(
                            document_runaway_timeout_seconds
                        ),
                        api_drain_timeout_seconds=api_drain_timeout_seconds,
                    ): index
                    for index, stage_input in enumerate(stage_inputs, start=1)
                }
                pending = set(futures)
                admission_closed = False
                local_termination_requested = False
                while pending:
                    monitor_failure, monitor_is_hard = current_monitor_stop()
                    if monitor_failure is not None:
                        stage_abort_latch.publish(monitor_failure)
                        if monitor_is_hard:
                            failure = monitor_failure
                            failure_origin = "monitor_hard"
                        elif failure is None:
                            failure = monitor_failure
                            failure_origin = "monitor_observer"
                        if not admission_closed:
                            admission.close()
                            admission_closed = True
                            for future in pending:
                                future.cancel()
                        if monitor_is_hard and not local_termination_requested:
                            terminate_active_mineru_processes()
                            local_termination_requested = True
                    done, pending = wait(
                        pending,
                        timeout=0.25,
                        return_when=FIRST_COMPLETED,
                    )
                    termination_requested_before_batch = local_termination_requested
                    batch_requires_termination = False
                    for future in sorted(done, key=lambda item: futures[item]):
                        index = futures[future]
                        try:
                            outcome = future.result()
                        except _StageAdmissionClosedError as exc:
                            outcome = _failed_document_outcome(
                                index,
                                stage_inputs[index - 1].digest,
                                exc,
                                input_logical_name=(
                                    stage_inputs[index - 1].logical_name
                                ),
                                workload_class=(
                                    stage_inputs[index - 1].workload_class
                                ),
                            )
                            outcome["status"] = NOT_ADMITTED_ATOMIC_ABORT
                            outcome["failure_class"] = "stage_abort"
                        except Exception as exc:
                            outcome = _failed_document_outcome(
                                index,
                                stage_inputs[index - 1].digest,
                                exc,
                                input_logical_name=(
                                    stage_inputs[index - 1].logical_name
                                ),
                                workload_class=(
                                    stage_inputs[index - 1].workload_class
                                ),
                            )
                        _reclassify_cancelled_after_termination(
                            outcome,
                            termination_requested=(
                                termination_requested_before_batch
                            ),
                        )
                        outcomes[index] = outcome
                        if outcome["status"] == "fail" and failure_origin in {
                            None,
                            "monitor_observer",
                        }:
                            failure = str(outcome["failure_class"])
                            failure_origin = "document"
                            batch_requires_termination = True
                    if batch_requires_termination and not local_termination_requested:
                        terminate_active_mineru_processes()
                        local_termination_requested = True
                    if failure is not None and not admission_closed:
                        admission.close()
                        admission_closed = True
                        for future in pending:
                            future.cancel()
            admission.close()
            # ThreadPoolExecutor.__exit__ waits for every running future.  Read
            # terminal states only after that boundary; sampling them inside
            # the context can leave a completed/cancelled document recorded as
            # ``not_started`` in the immutable receipt.
            for future, index in futures.items():
                if future.cancelled():
                    outcomes[index]["status"] = NOT_ADMITTED_ATOMIC_ABORT
                    outcomes[index]["failure_class"] = "stage_abort"
                    continue
                if not future.done():
                    outcomes[index] = _failed_document_outcome(
                        index,
                        stage_inputs[index - 1].digest,
                        RuntimeError("stage future unresolved after executor shutdown"),
                        input_logical_name=stage_inputs[index - 1].logical_name,
                        workload_class=stage_inputs[index - 1].workload_class,
                    )
                    failure = failure or "stage_future_unresolved"
                    continue
                if outcomes[index]["status"] != "not_started":
                    continue
                try:
                    outcome = future.result()
                except _StageAdmissionClosedError as exc:
                    outcome = _failed_document_outcome(
                        index,
                        stage_inputs[index - 1].digest,
                        exc,
                        input_logical_name=stage_inputs[index - 1].logical_name,
                        workload_class=stage_inputs[index - 1].workload_class,
                    )
                    outcome["status"] = NOT_ADMITTED_ATOMIC_ABORT
                    outcome["failure_class"] = "stage_abort"
                except Exception as exc:
                    outcome = _failed_document_outcome(
                        index,
                        stage_inputs[index - 1].digest,
                        exc,
                        input_logical_name=stage_inputs[index - 1].logical_name,
                        workload_class=stage_inputs[index - 1].workload_class,
                    )
                _reclassify_cancelled_after_termination(
                    outcome,
                    termination_requested=local_termination_requested,
                )
                outcomes[index] = outcome
    finally:
        try:
            orchestrator_terminal, terminal_drain_seconds = orchestrator_idle_waiter()
        except Exception as exc:
            failure = failure or (
                "orchestrator_terminal_drain_failed:"
                f"{type(exc).__name__}:{_safe_detail(str(exc))}"
            )
        for monitor_name, monitor_stop in (
            ("orchestrator", orchestrator_monitor.stop),
            ("metrics", metrics_monitor.stop),
        ):
            try:
                monitor_stop()
            except Exception as exc:
                failure = failure or (
                    f"{monitor_name}_monitor_cleanup_failed:"
                    f"{type(exc).__name__}:{_safe_detail(str(exc))}"
                )
    cleanup_ok = stage_tree is not None and not stage_tree.exists()
    cleanup_observation_error: str | None = None
    api_temp_dirs_created = 0
    api_temp_cleanup_errors: list[str] = []
    try:
        remaining_processes = _wait_for_process_cleanup()
        if remaining_processes:
            terminate_active_mineru_processes()
            remaining_processes = _wait_for_process_cleanup()
        created_api_temp_dirs = mineru_api_temp_dirs() - api_temp_before
        api_temp_dirs_created = len(created_api_temp_dirs)
        if not remaining_processes:
            api_temp_cleanup_errors = _remove_api_temp_dirs(created_api_temp_dirs)
        new_api_temp_dirs = mineru_api_temp_dirs() - api_temp_before
    except Exception as exc:
        remaining_processes = {}
        new_api_temp_dirs = set()
        cleanup_observation_error = (
            f"cleanup_observation_failed:{type(exc).__name__}:{_safe_detail(str(exc))}"
        )
    metrics_samples = metrics_monitor.samples
    if metrics_monitor.failure is not None and failure is None:
        failure = metrics_monitor.failure
    if orchestrator_monitor.failure is not None and failure is None:
        failure = orchestrator_monitor.failure
    if not cleanup_ok and failure is None:
        failure = "stage_temporary_tree_not_removed"
    if cleanup_observation_error is not None and failure is None:
        failure = cleanup_observation_error
    if api_temp_cleanup_errors and failure is None:
        failure = "stage_api_temp_cleanup_failed"
    if (remaining_processes or new_api_temp_dirs) and failure is None:
        failure = (
            f"stage_cleanup_failed:pids={sorted(remaining_processes)}:"
            f"temp_dirs={len(new_api_temp_dirs)}"
        )
    if failure is None and any(item["status"] != "pass" for item in outcomes.values()):
        failure = "stage_document_incomplete"
    orchestrator_failure = _orchestrator_evidence_failure(
        baseline=orchestrator_baseline,
        samples=orchestrator_monitor.samples,
        sampling_failures=orchestrator_monitor.sampling_failures,
        terminal=orchestrator_terminal,
        task_slots=task_slots,
        client_outstanding_window=client_outstanding_window,
    )
    if failure is None and orchestrator_failure is not None:
        failure = orchestrator_failure
    operational_failure = failure
    orchestrator_evidence_failure = orchestrator_monitor.evidence_failure
    metrics_evidence_failure = metrics_monitor.evidence_failure
    if (
        metrics_evidence_failure is None
        and not _metrics_prove_staged_activity(metrics_baseline, metrics_samples)
    ):
        metrics_evidence_failure = "stage_metrics_observed_no_load_activity"
    if operational_failure is None and orchestrator_evidence_failure is not None:
        failure = orchestrator_evidence_failure
    elif operational_failure is None and metrics_evidence_failure is not None:
        # Observation evidence is intentionally not a data-plane actuator.
        # The complete stage still drains normally, but this receipt cannot
        # commission or activate a profile.
        failure = metrics_evidence_failure
    return {
        "stage_document_count": document_count,
        "client_outstanding_window": client_outstanding_window,
        "peak_client_outstanding": admission.peak,
        "admission_order_profile": ADMISSION_ORDER_PROFILE,
        "admission_order_copy_indices": admission.evidence()[
            "admission_order_copy_indices"
        ],
        "admission": admission.evidence(),
        "selection_profile": "per_stage_regular_heavy_huge.v1",
        "orchestrator_task_concurrency": task_slots,
        "orchestrator_inference_concurrency": ORCHESTRATOR_INFERENCE_CONCURRENCY,
        "effective_inference_request_upper_bound": (
            task_slots * ORCHESTRATOR_INFERENCE_CONCURRENCY
            if task_slots is not None
            else None
        ),
        "status": "pass" if failure is None else "fail",
        "failure": failure,
        "elapsed_seconds": round(time.monotonic() - stage_started, 3),
        "documents": [outcomes[index] for index in sorted(outcomes)],
        "metrics": _metrics_summary(
            metrics_baseline,
            metrics_samples,
            metrics_monitor.sampling_failures,
            terminal_sample_observed_seconds=(
                metrics_monitor.terminal_sample_observed_seconds
            ),
            observer=metrics_monitor.evidence(),
        ),
        "orchestrator": _orchestrator_summary(
            baseline=orchestrator_baseline,
            samples=orchestrator_monitor.samples,
            sampling_failures=orchestrator_monitor.sampling_failures,
            terminal=orchestrator_terminal,
            preflight_drain_seconds=preflight_drain_seconds,
            terminal_drain_seconds=terminal_drain_seconds,
            observer=orchestrator_monitor.evidence(),
        ),
        "cleanup": {
            "temporary_tree_removed": cleanup_ok,
            "external_api_temp_dirs_created": api_temp_dirs_created,
            "external_api_temp_dirs_after": len(new_api_temp_dirs),
            "api_temp_cleanup_errors": api_temp_cleanup_errors,
            "external_mineru_processes_after": len(remaining_processes),
            "observation_error": cleanup_observation_error,
        },
    }


def _stage_preflight_failure(
    *,
    concurrency: int,
    started: float,
    metrics_baseline: MetricsSample,
    orchestrator_baseline: MinerUOrchestratorHealth,
    preflight_drain_seconds: float,
    failure: str,
    task_slots: int,
) -> dict[str, Any]:
    return {
        "stage_document_count": concurrency,
        "client_outstanding_window": min(concurrency, task_slots),
        "peak_client_outstanding": 0,
        "admission_order_profile": ADMISSION_ORDER_PROFILE,
        "admission_order_copy_indices": [],
        "admission": {
            "profile": ADMISSION_ORDER_PROFILE,
            "expected_copy_indices": list(range(1, concurrency + 1)),
            "admission_order_copy_indices": [],
            "records": [
                {
                    "copy_index": copy_index,
                    "admission_ordinal": None,
                    "state": NOT_ADMITTED_ATOMIC_ABORT,
                }
                for copy_index in range(1, concurrency + 1)
            ],
            "closed": True,
            "abort_reason": failure,
        },
        "selection_profile": "per_stage_regular_heavy_huge.v1",
        "orchestrator_task_concurrency": task_slots,
        "orchestrator_inference_concurrency": ORCHESTRATOR_INFERENCE_CONCURRENCY,
        "effective_inference_request_upper_bound": (
            task_slots * ORCHESTRATOR_INFERENCE_CONCURRENCY
        ),
        "status": "fail",
        "failure": failure,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "documents": [],
        "metrics": _metrics_summary(
            metrics_baseline,
            (),
            observer={
                "profile": METRICS_OBSERVER_PROFILE,
                "state": "CLOSED",
                "observation_complete": False,
                "hard_failure": None,
                "transitions": [],
            },
        ),
        "orchestrator": _orchestrator_summary(
            baseline=orchestrator_baseline,
            samples=(),
            terminal=orchestrator_baseline,
            preflight_drain_seconds=preflight_drain_seconds,
            terminal_drain_seconds=0.0,
        ),
        "cleanup": {
            "temporary_tree_removed": True,
            "external_api_temp_dirs_created": 0,
            "external_api_temp_dirs_after": 0,
            "api_temp_cleanup_errors": [],
            "external_mineru_processes_after": 0,
            "observation_error": None,
        },
    }


def _parse_admitted_copy(
    *,
    admission: _StageAdmission,
    abort_reason: Callable[[], str | None],
    workload_class: str,
    copy_index: int,
    stage_root: Path,
    input_bytes: bytes,
    input_digest: str,
    input_logical_name: str,
    mineru_bin: Path,
    api_url: str,
    inference_upstream_url: str,
    runtime_identity: str,
    document_runaway_timeout_seconds: int,
    api_drain_timeout_seconds: int,
) -> dict[str, Any]:
    outcome = admission.run(
        copy_index=copy_index,
        workload_class=workload_class,
        abort_reason=abort_reason,
        operation=lambda: _parse_frozen_copy(
            copy_index=copy_index,
            stage_root=stage_root,
            input_bytes=input_bytes,
            input_digest=input_digest,
            input_logical_name=input_logical_name,
            mineru_bin=mineru_bin,
            api_url=api_url,
            inference_upstream_url=inference_upstream_url,
            runtime_identity=runtime_identity,
            document_runaway_timeout_seconds=(
                document_runaway_timeout_seconds
            ),
            api_drain_timeout_seconds=api_drain_timeout_seconds,
        ),
    )
    outcome["workload_class"] = workload_class
    return outcome


def _parse_frozen_copy(
    *,
    copy_index: int,
    stage_root: Path,
    input_bytes: bytes,
    input_digest: str,
    input_logical_name: str,
    mineru_bin: Path,
    api_url: str,
    inference_upstream_url: str,
    runtime_identity: str,
    document_runaway_timeout_seconds: int,
    api_drain_timeout_seconds: int,
) -> dict[str, Any]:
    started = time.monotonic()
    document_root = stage_root / f"copy-{copy_index:02d}"
    private_tmp = document_root / "tmp"
    private_tmp.mkdir(parents=True)
    source = document_root / f"sha256_{input_digest}.pdf"
    source.write_bytes(input_bytes)
    if hashlib.sha256(source.read_bytes()).hexdigest() != input_digest:
        raise RuntimeError("frozen staged-load copy hash drifted")
    process = MinerUProcess(
        executable=mineru_bin,
        extra_env={
            "TEMP": str(private_tmp),
            "TMP": str(private_tmp),
            "TMPDIR": str(private_tmp),
        },
    )
    parser = MinerUMediumDocumentParser(
        process=process,
        api_url=api_url,
        server_url=inference_upstream_url,
    )
    try:
        result = parser.parse(
            input_pdf=source,
            output_dir=document_root / "output",
            options=ParserOptions(
                timeout_seconds=document_runaway_timeout_seconds,
                api_url=api_url,
                api_drain_timeout_seconds=api_drain_timeout_seconds,
                server_url=inference_upstream_url,
                http_request_concurrency=None,
                runtime_bundle_identity_sha256=runtime_identity,
            ),
            source_pdf_sha256=f"sha256:{input_digest}",
        )
        provider = result.provider_document
        if len(provider.pages) < MINIMUM_INPUT_PAGES:
            raise ValueError(
                f"staged-load input must produce at least {MINIMUM_INPUT_PAGES} pages"
            )
        if provider.parser_version != "3.4.4":
            raise ValueError("staged-load parser version drifted")
        return {
            "copy_index": copy_index,
            "logical_name": input_logical_name,
            "input_sha256": f"sha256:{input_digest}",
            "status": "pass",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "page_count": len(provider.pages),
            "block_count": len(provider.blocks),
            "provider_bundle_sha256": provider.bundle_sha256,
        }
    except Exception as exc:
        return _failed_document_outcome(
            copy_index,
            input_digest,
            exc,
            input_logical_name=input_logical_name,
            started=started,
        )


def _failed_document_outcome(
    copy_index: int,
    input_digest: str,
    exc: Exception,
    *,
    input_logical_name: str = "frozen-input.pdf",
    workload_class: str | None = None,
    started: float | None = None,
) -> dict[str, Any]:
    raw_detail = f"{type(exc).__name__}:{' '.join(str(exc).split())}"
    detail = _safe_detail(raw_detail)
    outcome = {
        "copy_index": copy_index,
        "logical_name": input_logical_name,
        "input_sha256": f"sha256:{input_digest}",
        "status": "fail",
        # Classify the complete normalized diagnostic before rendering the
        # bounded receipt excerpt.  A marker in the omitted middle must not be
        # silently downgraded to a generic parse failure.
        "failure_class": _classify_failure(raw_detail),
        "failure_detail": detail,
        "failure_detail_chars": len(raw_detail),
        "failure_detail_sha256": (
            "sha256:" + hashlib.sha256(raw_detail.encode("utf-8")).hexdigest()
        ),
        "elapsed_seconds": (
            round(time.monotonic() - started, 3) if started is not None else None
        ),
    }
    if workload_class is not None:
        outcome["workload_class"] = workload_class
    return outcome


def _classify_failure(detail: str) -> str:
    lowered = detail.lower()
    markers = (
        ("parsertaskdeadlineerror", "task_deadline"),
        ("task deadline exceeded", "task_deadline"),
        ("429", "overload_429"),
        ("resource_exhausted", "overload_resource_exhausted"),
        ("overload", "overload"),
        ("out of memory", "gpu_oom"),
        ("cuda oom", "gpu_oom"),
        ("enginecore", "engine_core_failure"),
        ("engine core", "engine_core_failure"),
        ("http 5", "remote_5xx"),
        ("http error 5", "remote_5xx"),
        ("status code: [5", "remote_5xx"),
    )
    return next(
        (label for marker, label in markers if marker in lowered), "parse_failure"
    )


def _metrics_summary(
    baseline: MetricsSample,
    samples: tuple[MetricsSample, ...],
    sampling_failures: tuple[MetricsSamplingFailure, ...] = (),
    *,
    terminal_sample_observed_seconds: float | None = None,
    observer: dict[str, object] | None = None,
) -> dict[str, Any]:
    observed = (baseline, *samples)
    distribution = samples or (baseline,)

    def p95(name: str) -> float:
        values = sorted(float(getattr(item, name)) for item in distribution)
        index = max(0, math.ceil(0.95 * len(values)) - 1)
        return values[index]

    return {
        "baseline": baseline.to_payload(),
        "sample_count": len(samples),
        "sampling_failures": [item.to_payload() for item in sampling_failures],
        "terminal_sample_observed_seconds": (
            round(terminal_sample_observed_seconds, 6)
            if terminal_sample_observed_seconds is not None
            else None
        ),
        "observer": observer,
        "range": {
            name: {
                "min": min(getattr(item, name) for item in observed),
                "max": max(getattr(item, name) for item in observed),
            }
            for name in ("running", "waiting", "preemptions", "kv_cache")
        },
        "percentiles": {
            f"{name}_p95": p95(name) for name in ("running", "waiting", "kv_cache")
        },
    }


def _orchestrator_evidence_failure(
    *,
    baseline: MinerUOrchestratorHealth,
    samples: tuple[OrchestratorSample, ...],
    sampling_failures: tuple[OrchestratorSamplingFailure, ...] = (),
    terminal: MinerUOrchestratorHealth | None,
    task_slots: int,
    client_outstanding_window: int,
) -> str | None:
    if sampling_failures:
        return "orchestrator_observation_incomplete"
    if baseline.active_tasks != 0:
        return "orchestrator_baseline_not_idle"
    if terminal is None:
        return "orchestrator_terminal_health_missing"
    if terminal.active_tasks != 0:
        return "orchestrator_terminal_not_idle"
    observed = (
        OrchestratorSample.from_health(baseline, observed_seconds=0.0),
        *samples,
        OrchestratorSample.from_health(terminal, observed_seconds=0.0),
    )
    if max(sample.processing_tasks for sample in observed) > task_slots:
        return "orchestrator_processing_exceeded_attested_slots"
    if (
        max(sample.queued_tasks + sample.processing_tasks for sample in observed)
        > client_outstanding_window
    ):
        return "orchestrator_active_exceeded_client_window"
    if not samples or max(sample.processing_tasks for sample in samples) == 0:
        return "orchestrator_observed_no_processing_activity"
    return None


def _orchestrator_summary(
    *,
    baseline: MinerUOrchestratorHealth,
    samples: tuple[OrchestratorSample, ...],
    sampling_failures: tuple[OrchestratorSamplingFailure, ...] = (),
    terminal: MinerUOrchestratorHealth | None,
    preflight_drain_seconds: float,
    terminal_drain_seconds: float | None,
    observer: dict[str, object] | None = None,
) -> dict[str, Any]:
    observed = (
        OrchestratorSample.from_health(baseline, observed_seconds=0.0),
        *samples,
    )
    if terminal is not None:
        observed = (
            *observed,
            OrchestratorSample.from_health(
                terminal,
                observed_seconds=(samples[-1].observed_seconds if samples else 0.0),
            ),
        )
    return {
        "task_registry_semantics": TASK_REGISTRY_SEMANTICS,
        "baseline": baseline.as_dict(),
        "samples": [sample.to_payload() for sample in samples],
        "sample_count": len(samples),
        "sampling_failures": [item.to_payload() for item in sampling_failures],
        "observer": observer,
        "terminal": terminal.as_dict() if terminal is not None else None,
        "terminal_active_tasks": (
            terminal.active_tasks if terminal is not None else None
        ),
        "preflight_drain_seconds": round(preflight_drain_seconds, 6),
        "terminal_drain_seconds": (
            round(terminal_drain_seconds, 6)
            if terminal_drain_seconds is not None
            else None
        ),
        "stop_semantics": "drain-not-cancel.v1",
        "range": {
            name: {
                "min": min(getattr(sample, name) for sample in observed),
                "max": max(getattr(sample, name) for sample in observed),
            }
            for name in (
                "queued_tasks",
                "processing_tasks",
                "completed_tasks",
                "failed_tasks",
            )
        },
    }


def _metrics_prove_staged_activity(
    baseline: MetricsSample,
    samples: tuple[MetricsSample, ...],
) -> bool:
    return bool(
        samples
        and (
            max(sample.running for sample in samples) > baseline.running
            or max(sample.kv_cache for sample in samples) > baseline.kv_cache
        )
    )


def _wait_for_process_cleanup() -> dict[int, str]:
    deadline = time.monotonic() + 5
    while True:
        remaining = mineru_processes(process_snapshot())
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(0.25)


def _remove_api_temp_dirs(paths: set[Path]) -> list[str]:
    errors: list[str] = []
    for path in sorted(paths):
        if not path.name.startswith("mineru-api-client-"):
            errors.append(f"refused_unexpected_path:{path.name}")
            continue
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{path.name}:{type(exc).__name__}:{_safe_detail(str(exc))}")
    return errors


def _write_new_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _safe_detail(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= _MAX_SAFE_DETAIL_CHARS:
        return normalized
    remaining = _MAX_SAFE_DETAIL_CHARS - len(_SAFE_DETAIL_TRUNCATION_MARKER)
    head_chars = remaining // 2
    tail_chars = remaining - head_chars
    return (
        normalized[:head_chars]
        + _SAFE_DETAIL_TRUNCATION_MARKER
        + normalized[-tail_chars:]
    )


def _endpoint_sha256(url: str) -> str:
    return hashlib.sha256(url.rstrip("/").encode("utf-8")).hexdigest()


def _verify_endpoint_identity(
    topology: object,
    *,
    field: str,
    url: str,
) -> None:
    if not isinstance(topology, dict):
        raise ValueError("runtime manifest topology must be an object")
    if topology.get(field) != "sha256:" + _endpoint_sha256(url):
        raise ValueError(f"runtime manifest topology {field} drifted")


def _workload_class(page_count: int) -> str:
    if page_count >= 500:
        return "huge"
    if page_count >= 80:
        return "heavy"
    return "regular"


def _load_frozen_corpus(
    manifest_path: Path,
    *,
    expected_identity_sha256: str,
) -> tuple[tuple[FrozenCorpusInput, ...], dict[str, object]]:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("frozen corpus manifest is missing or unsafe")
    payload = json.loads(manifest_path.read_bytes())
    if not isinstance(payload, dict) or set(payload) != {"schema", "documents"}:
        raise ValueError("frozen corpus manifest fields are not closed")
    if payload.get("schema") != CORPUS_SCHEMA:
        raise ValueError(f"frozen corpus schema must be {CORPUS_SCHEMA}")
    documents = payload.get("documents")
    if not isinstance(documents, list) or len(documents) < MINIMUM_CORPUS_DOCUMENTS:
        raise ValueError(
            f"frozen corpus requires at least {MINIMUM_CORPUS_DOCUMENTS} documents"
        )
    frozen: list[FrozenCorpusInput] = []
    seen_names: set[str] = set()
    seen_hashes: set[str] = set()
    for index, item in enumerate(documents, start=1):
        if not isinstance(item, dict) or set(item) != {
            "logical_name",
            "path",
            "sha256",
        }:
            raise ValueError(f"frozen corpus document {index} fields drifted")
        logical_name = item.get("logical_name")
        path_value = item.get("path")
        sha256_value = item.get("sha256")
        if (
            not isinstance(logical_name, str)
            or not logical_name
            or "/" in logical_name
            or "\\" in logical_name
            or not isinstance(path_value, str)
            or not path_value
            or not isinstance(sha256_value, str)
        ):
            raise ValueError(f"frozen corpus document {index} is invalid")
        path = Path(path_value)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"frozen corpus document {index} is missing or unsafe")
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if _normalized_sha256(sha256_value) != digest:
            raise ValueError(f"frozen corpus document {index} hash drifted")
        if logical_name in seen_names or digest in seen_hashes:
            raise ValueError("frozen corpus names and hashes must be unique")
        page_count = count_pdf_pages(path)
        if page_count < 1:
            raise ValueError(f"frozen corpus document {index} has no pages")
        seen_names.add(logical_name)
        seen_hashes.add(digest)
        frozen.append(
            FrozenCorpusInput(
                logical_name=logical_name,
                payload=content,
                digest=digest,
                page_count=page_count,
                workload_class=_workload_class(page_count),
            )
        )
    classes = {item.workload_class for item in frozen}
    if not {"regular", "heavy", "huge"}.issubset(classes):
        raise ValueError("frozen corpus must include regular, heavy and huge PDFs")
    canonical_documents = [item.evidence() for item in frozen]
    canonical = {"schema": CORPUS_SCHEMA, "documents": canonical_documents}
    identity = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    if _normalized_sha256(expected_identity_sha256) != identity.removeprefix("sha256:"):
        raise ValueError("frozen corpus identity drifted")
    return tuple(frozen), {
        "profile": "operator_frozen_heterogeneous_v2",
        "logical_name": manifest_path.name,
        "sha256": identity,
        "bytes": sum(len(item.payload) for item in frozen),
        "minimum_required_pages": MINIMUM_INPUT_PAGES,
        "documents": canonical_documents,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mineru_staged_load", description=__doc__)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--receipt-out", type=Path, required=True)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--expected-corpus-sha256", required=True)
    parser.add_argument("--mineru-bin", type=Path)
    parser.add_argument("--api-url")
    parser.add_argument("--observability-url")
    parser.add_argument("--inference-upstream-url")
    parser.add_argument("--runtime-bundle-identity")
    parser.add_argument("--work-root", type=Path)
    parser.add_argument(
        "--document-runaway-timeout-seconds",
        type=int,
        default=MINERU_STAGED_LOAD_MINIMUM_RUNAWAY_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--api-drain-timeout-seconds",
        type=int,
        default=MINERU_STAGED_LOAD_MINIMUM_RUNAWAY_TIMEOUT_SECONDS,
    )
    parser.add_argument("--host-observer-ssh-host")
    parser.add_argument("--host-observer-ssh-user")
    parser.add_argument("--host-observer-ssh-port", type=int, default=22)
    parser.add_argument("--host-observer-identity-file", type=Path)
    parser.add_argument("--host-observer-known-hosts-file", type=Path)
    parser.add_argument("--docker-memory-reserve-bytes", type=int)
    parser.add_argument("--expected-campaign-epoch-sha256", required=True)
    return parser.parse_args(argv)


def _resolve_staged_preflight(
    args: argparse.Namespace,
) -> tuple[
    Path,
    str,
    str,
    str,
    str,
    tuple[FrozenCorpusInput, ...],
    dict[str, object],
]:
    mineru_bin = args.mineru_bin or (
        Path(value) if (value := os.environ.get("DISCLOSURE_MINERU_BIN")) else None
    )
    api_url = args.api_url or os.environ.get("DISCLOSURE_MINERU_API_URL")
    observability_url = args.observability_url or os.environ.get(
        "DISCLOSURE_MINERU_OBSERVABILITY_URL"
    )
    inference_upstream_url = args.inference_upstream_url or os.environ.get(
        "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL"
    )
    runtime_identity = args.runtime_bundle_identity or os.environ.get(
        "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256"
    )
    if mineru_bin is None or not mineru_bin.is_file():
        raise ValueError("DISCLOSURE_MINERU_BIN is missing or not a file")
    if not api_url or not observability_url or not inference_upstream_url:
        raise ValueError("complete MinerU fixed-API topology is required")
    if not _is_prefixed_sha256(runtime_identity):
        raise ValueError("runtime bundle identity is missing or invalid")
    if args.work_root is not None and not args.work_root.is_dir():
        raise ValueError(f"work-root is not a directory: {args.work_root}")
    if (
        args.expected_campaign_epoch_sha256 is not None
        and not _is_prefixed_sha256(args.expected_campaign_epoch_sha256)
    ):
        raise ValueError("expected campaign epoch SHA-256 is invalid")
    if (
        args.document_runaway_timeout_seconds
        < MINERU_STAGED_LOAD_MINIMUM_RUNAWAY_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "document-runaway-timeout-seconds is below the formal safety minimum"
        )
    if (
        args.api_drain_timeout_seconds
        < MINERU_STAGED_LOAD_MINIMUM_RUNAWAY_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "api-drain-timeout-seconds is below the formal safety minimum"
        )
    if (
        not args.host_observer_ssh_host
        or not args.host_observer_ssh_user
        or args.host_observer_identity_file is None
        or args.host_observer_known_hosts_file is None
        or isinstance(args.docker_memory_reserve_bytes, bool)
        or not isinstance(args.docker_memory_reserve_bytes, int)
        or args.docker_memory_reserve_bytes < 1
    ):
        raise ValueError(
            "pinned host observer SSH and a positive Docker memory reserve are required"
        )
    if os.environ.get("MINERU_PROCESSING_WINDOW_SIZE") != str(
        MINERU_PROCESSING_WINDOW_SIZE
    ):
        raise ValueError("MINERU_PROCESSING_WINDOW_SIZE must be 16")
    try:
        corpus, corpus_evidence = _load_frozen_corpus(
            args.corpus_manifest,
            expected_identity_sha256=args.expected_corpus_sha256,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"frozen heterogeneous corpus is invalid: {exc}") from exc
    assert isinstance(api_url, str)
    assert isinstance(observability_url, str)
    assert isinstance(inference_upstream_url, str)
    assert isinstance(runtime_identity, str)
    return (
        mineru_bin,
        api_url,
        observability_url,
        inference_upstream_url,
        runtime_identity,
        corpus,
        corpus_evidence,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.receipt_out.exists() or args.receipt_out.is_symlink():
        raise SystemExit(
            f"[abort] output already exists; stale evidence: {args.receipt_out}"
        )
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    stage_results: list[dict[str, Any]] = []
    failure: str | None = None
    identity_payload: dict[str, Any] = {}
    run_tree: Path | None = None
    api_temp_before: set[Path] = set()
    remaining_processes: dict[int, str] = {}
    new_api_temp_dirs: set[Path] = set()
    temporary_tree_removed = True
    cleanup_observation_error: str | None = None
    api_temp_dirs_created = 0
    api_temp_cleanup_errors: list[str] = []
    task_slots: int | None = None
    mineru_bin: Path | None = None
    api_url: str | None = None
    observability_url: str | None = None
    inference_upstream_url: str | None = None
    runtime_identity: str | None = None
    corpus: tuple[FrozenCorpusInput, ...] = ()
    corpus_evidence: dict[str, object] = {}
    failure_phase = "preflight_configuration"
    runtime_cleanup_armed = False
    host_monitor: _HostCapacityMonitor | None = None
    host_transport: _HostObserverControlMaster | None = None
    run_abort_latch = _StageAbortLatch()
    host_capacity_evidence: dict[str, Any] = {}
    secondary_failures: list[dict[str, str]] = []
    campaign_epoch_evidence: dict[str, object] = {}
    inference_liveness_evidence: dict[str, object] = {
        "schema": ARM_INFERENCE_LIVENESS_SCHEMA,
        "profile": ARM_INFERENCE_LIVENESS_PROFILE,
        "pre_arm": None,
        "workload_started_at_utc": None,
        "workload_finished_at_utc": None,
        "workload_started_observed_seconds": None,
        "workload_finished_observed_seconds": None,
        "post_arm": None,
    }
    try:
        (
            mineru_bin,
            api_url,
            observability_url,
            inference_upstream_url,
            runtime_identity,
            corpus,
            corpus_evidence,
        ) = _resolve_staged_preflight(args)
        failure_phase = "runtime_preflight"
        api_temp_before = mineru_api_temp_dirs()
        if api_temp_before:
            raise RuntimeError(
                "pre-existing MinerU API temporary directories require cleanup"
            )
        before = process_snapshot()
        if producers := active_disclosure_producers(before):
            raise RuntimeError(
                f"disclosure producer processes are active: {sorted(producers)}"
            )
        if existing_mineru := mineru_processes(before):
            raise RuntimeError(
                f"pre-existing MinerU processes require cleanup: {sorted(existing_mineru)}"
            )
        runtime_cleanup_armed = True
        failure_phase = "runtime_identity"
        local_client = client_bundle_identity(mineru_bin)
        code_digest = writer_code_digest()
        manifest_payload = json.loads(args.runtime_manifest.read_bytes())
        verified = verify_runtime_manifest_payload(
            manifest_payload,
            configured_identity=str(runtime_identity),
            local_client_identity=local_client,
            local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
            local_writer_code_digest=code_digest,
        )
        topology = verified.manifest["topology"]
        _verify_endpoint_identity(
            topology,
            field="api_endpoint_sha256",
            url=api_url,
        )
        _verify_endpoint_identity(
            topology,
            field="observability_endpoint_sha256",
            url=observability_url,
        )
        _verify_endpoint_identity(
            topology,
            field="inference_upstream_sha256",
            url=inference_upstream_url,
        )
        collector_sha256 = topology["windows_collector_sha256"]
        windows_node_identity_sha256 = topology[
            "windows_node_identity_sha256"
        ]
        failure_phase = "host_capacity_observer"
        host_transport = _HostObserverControlMaster(
            host=args.host_observer_ssh_host,
            user=args.host_observer_ssh_user,
            port=args.host_observer_ssh_port,
            identity_file=args.host_observer_identity_file,
            known_hosts_file=args.host_observer_known_hosts_file,
        )
        host_transport.start()
        host_monitor = _HostCapacityMonitor(
            sampler=lambda: _fetch_host_capacity_sample(
                host_transport.session_command(),
                expected_collector_sha256=collector_sha256,
                expected_windows_node_identity_sha256=(
                    windows_node_identity_sha256
                ),
                docker_memory_reserve_bytes=args.docker_memory_reserve_bytes,
            ),
            collector_sha256=collector_sha256,
            windows_node_identity_sha256=windows_node_identity_sha256,
            docker_memory_reserve_bytes=args.docker_memory_reserve_bytes,
            abort_latch=run_abort_latch,
        )
        host_monitor.start()
        orchestrator_manifest = verified.manifest["orchestrator"]
        task_slots = verified.max_concurrent_requests
        expected_task_retention_seconds = int(
            orchestrator_manifest["task_retention_seconds"]
        )
        expected_cleanup_interval_seconds = int(
            orchestrator_manifest["task_cleanup_interval_seconds"]
        )
        failure_phase = "campaign_epoch_preflight"
        campaign_epoch_evidence = _campaign_epoch_evidence(
            host_monitor,
            expected_sha256=args.expected_campaign_epoch_sha256,
        )
        observed_campaign_epoch_sha256 = str(
            campaign_epoch_evidence["observed_sha256"]
        )
        if (
            args.expected_campaign_epoch_sha256 is not None
            and args.expected_campaign_epoch_sha256
            != observed_campaign_epoch_sha256
        ):
            raise RuntimeError("expected campaign service epoch drifted")
        inference_liveness_evidence["pre_arm"] = _not_run_arm_boundary_canary(
            observability_url=observability_url,
            expected_model_id=verified.served_model_id,
            runtime_identity_sha256=verified.identity_sha256,
            campaign_epoch=campaign_epoch_evidence,
            phase="pre_arm",
        )
        inference_liveness_evidence["post_arm"] = _not_run_arm_boundary_canary(
            observability_url=observability_url,
            expected_model_id=verified.served_model_id,
            runtime_identity_sha256=verified.identity_sha256,
            campaign_epoch=campaign_epoch_evidence,
            phase="post_arm",
        )
        failure_phase = "pre_arm_inference_liveness"
        pre_arm_canary, pre_arm_failure = _run_arm_boundary_canary(
            observability_url,
            expected_model_id=verified.served_model_id,
            runtime_identity_sha256=verified.identity_sha256,
            campaign_epoch=campaign_epoch_evidence,
            phase="pre_arm",
            host_monitor=host_monitor,
        )
        inference_liveness_evidence["pre_arm"] = pre_arm_canary
        if pre_arm_failure is not None:
            raise pre_arm_failure
        failure_phase = "runtime_preflight"
        probe_mineru_served_model(
            observability_url,
            expected_model_id=verified.served_model_id,
        )
        global_metrics_baseline = fetch_vllm_metrics(observability_url)
        identity_payload = {
            "local_client_identity_sha256": local_client.package_set_sha256,
            "local_content_package_versions": dict(
                local_client.content_package_versions
            ),
            "local_processing_window_size": MINERU_PROCESSING_WINDOW_SIZE,
            "local_writer_code_sha256": code_digest,
            "runtime_manifest_identity_sha256": verified.identity_sha256,
            "orchestrator_runtime_identity_sha256": (
                verified.orchestrator_identity_sha256
            ),
            "provider_runtime_identity_sha256": verified.provider_identity_sha256,
            "served_model_id": verified.served_model_id,
            "orchestrator_task_slots": task_slots,
        }
        failure_phase = "staged_load"
        with tempfile.TemporaryDirectory(
            prefix="disclosure-mineru-staged-load-",
            dir=args.work_root,
        ) as tmp:
            run_tree = Path(tmp)
            inference_liveness_evidence["workload_started_at_utc"] = (
                datetime.now(UTC).isoformat()
            )
            inference_liveness_evidence["workload_started_observed_seconds"] = (
                round(host_monitor.observed_seconds(), 6)
            )
            def run_persistent_stage(document_count: int) -> dict[str, Any]:
                metrics_monitor_client = _PersistentVLLMMetricsClient(
                    observability_url
                )
                orchestrator_monitor_client = MinerUOrchestratorHealthClient(
                    api_url
                )
                try:
                    return _run_stage(
                    document_count=document_count,
                    run_root=run_tree,
                    corpus=corpus,
                    mineru_bin=mineru_bin,
                    api_url=api_url,
                    inference_upstream_url=inference_upstream_url,
                    runtime_identity=str(runtime_identity),
                    document_runaway_timeout_seconds=(
                        args.document_runaway_timeout_seconds
                    ),
                    api_drain_timeout_seconds=args.api_drain_timeout_seconds,
                    task_slots=task_slots,
                    expected_preemptions=global_metrics_baseline.preemptions,
                    metrics_sampler=lambda: fetch_vllm_metrics(observability_url),
                    metrics_monitor_sampler=metrics_monitor_client.fetch,
                    metrics_monitor_close=metrics_monitor_client.close,
                    orchestrator_sampler=lambda: orchestrator_monitor_client.fetch(
                        expected_task_slots=task_slots,
                        expected_task_retention_seconds=(
                            expected_task_retention_seconds
                        ),
                        expected_cleanup_interval_seconds=(
                            expected_cleanup_interval_seconds
                        ),
                    ),
                    orchestrator_monitor_close=(
                        orchestrator_monitor_client.close
                    ),
                    orchestrator_idle_waiter=lambda: wait_for_mineru_orchestrator_idle(
                        api_url,
                        timeout_seconds=args.api_drain_timeout_seconds,
                        expected_task_slots=task_slots,
                        expected_task_retention_seconds=(
                            expected_task_retention_seconds
                        ),
                        expected_cleanup_interval_seconds=(
                            expected_cleanup_interval_seconds
                        ),
                    ),
                    host_failure=lambda: host_monitor.failure,
                    host_observation_stop=lambda: (
                        host_monitor.observation_stop_reason
                    ),
                    abort_latch=run_abort_latch,
                    )
                finally:
                    close_error: Exception | None = None
                    for close_client in (
                        metrics_monitor_client.close,
                        orchestrator_monitor_client.close,
                    ):
                        try:
                            close_client()
                        except Exception as exc:
                            close_error = close_error or exc
                    if close_error is not None:
                        raise RuntimeError(
                            "persistent observer transport cleanup failed"
                        ) from close_error

            stage_results = execute_fixed_stage_sequence(run_persistent_stage)
        inference_liveness_evidence["workload_finished_at_utc"] = (
            datetime.now(UTC).isoformat()
        )
        inference_liveness_evidence["workload_finished_observed_seconds"] = (
            round(host_monitor.observed_seconds(), 6)
        )
        if len(stage_results) != len(STAGE_DOCUMENT_COUNTS) or any(
            result.get("status") != "pass" for result in stage_results
        ):
            failure = "staged_load_stopped_before_all_fixed_stages_passed"
        else:
            failure_phase = "post_arm_inference_liveness"
            current_campaign_epoch = _campaign_epoch_evidence(
                host_monitor,
                expected_sha256=args.expected_campaign_epoch_sha256,
            )
            if (
                current_campaign_epoch["observed_sha256"]
                != observed_campaign_epoch_sha256
            ):
                raise RuntimeError("campaign service epoch drifted during arm")
            post_arm_canary, post_arm_failure = _run_arm_boundary_canary(
                observability_url,
                expected_model_id=verified.served_model_id,
                runtime_identity_sha256=verified.identity_sha256,
                campaign_epoch=campaign_epoch_evidence,
                phase="post_arm",
                host_monitor=host_monitor,
            )
            inference_liveness_evidence["post_arm"] = post_arm_canary
            if post_arm_failure is not None:
                raise post_arm_failure
            failure_phase = "complete"
    except Exception as exc:
        failure = f"{type(exc).__name__}:{_safe_detail(str(exc))}"
    finally:
        if host_monitor is not None:
            host_monitor.stop()
            host_capacity_evidence = host_monitor.evidence()
            if host_capacity_evidence.get("status") != "pass":
                if failure is None:
                    failure_phase = "host_capacity_observer"
                    failure = str(host_capacity_evidence.get("failure"))
        if host_transport is not None:
            try:
                host_transport.close()
            except Exception as exc:
                transport_cleanup_failure = (
                    "host_capacity_transport_cleanup_failed:"
                    f"{type(exc).__name__}:{_safe_detail(str(exc))}"
                )
                secondary_failures.append(
                    {
                        "phase": "host_capacity_transport_cleanup",
                        "failure": transport_cleanup_failure,
                    }
                )
                if failure is None:
                    failure_phase = "host_capacity_observer"
                    failure = transport_cleanup_failure
        if runtime_cleanup_armed:
            try:
                remaining_processes = _wait_for_process_cleanup()
                if remaining_processes:
                    terminate_active_mineru_processes()
                    remaining_processes = _wait_for_process_cleanup()
                created_api_temp_dirs = mineru_api_temp_dirs() - api_temp_before
                api_temp_dirs_created = len(created_api_temp_dirs)
                if not remaining_processes:
                    api_temp_cleanup_errors = _remove_api_temp_dirs(
                        created_api_temp_dirs
                    )
                new_api_temp_dirs = mineru_api_temp_dirs() - api_temp_before
                temporary_tree_removed = run_tree is None or not run_tree.exists()
            except Exception as exc:
                cleanup_observation_error = (
                    "cleanup_observation_failed:"
                    f"{type(exc).__name__}:{_safe_detail(str(exc))}"
                )
                if failure is None:
                    failure_phase = "cleanup"
                    failure = cleanup_observation_error
            if (
                remaining_processes
                or new_api_temp_dirs
                or api_temp_cleanup_errors
                or not temporary_tree_removed
            ):
                cleanup_failure = (
                    f"cleanup_failed:pids={sorted(remaining_processes)}:"
                    f"temp_dirs={len(new_api_temp_dirs)}:"
                    f"temp_cleanup_errors={len(api_temp_cleanup_errors)}:"
                    f"tree={temporary_tree_removed}"
                )
                if failure is None:
                    failure_phase = "cleanup"
                    failure = cleanup_failure

    receipt_status = "pass" if failure is None else "fail"
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
        "execution_id": str(uuid.uuid4()),
        "status": receipt_status,
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "topology": {
            "api_endpoint_sha256": (
                "sha256:" + _endpoint_sha256(api_url) if api_url else None
            ),
            "observability_endpoint_sha256": (
                "sha256:" + _endpoint_sha256(observability_url)
                if observability_url
                else None
            ),
            "inference_upstream_sha256": (
                "sha256:" + _endpoint_sha256(inference_upstream_url)
                if inference_upstream_url
                else None
            ),
        },
        "database_access": "none",
        "queue_access": "none",
        "fixed_stage_document_counts": list(STAGE_DOCUMENT_COUNTS),
        "orchestrator_task_concurrency": task_slots,
        "orchestrator_inference_concurrency": ORCHESTRATOR_INFERENCE_CONCURRENCY,
        "effective_inference_request_upper_bound": (
            task_slots * ORCHESTRATOR_INFERENCE_CONCURRENCY
            if task_slots is not None
            else None
        ),
        "safety_limits": {
            "profile": SAFETY_LIMITS_PROFILE,
            "document_runaway_timeout_seconds": (
                args.document_runaway_timeout_seconds
            ),
            "api_drain_timeout_seconds": args.api_drain_timeout_seconds,
        },
        "input": corpus_evidence,
        "identity": identity_payload,
        "campaign_epoch": campaign_epoch_evidence,
        "inference_liveness": inference_liveness_evidence,
        "host_capacity": host_capacity_evidence,
        "stages": stage_results,
        "failure": failure,
        "failure_phase": failure_phase if failure is not None else None,
        "secondary_failures": secondary_failures,
        "cleanup": {
            "external_api_temp_dirs_created": api_temp_dirs_created,
            "external_api_temp_dirs_after": len(new_api_temp_dirs),
            "api_temp_cleanup_errors": api_temp_cleanup_errors,
            "external_mineru_processes_after": len(remaining_processes),
            "temporary_tree_removed": temporary_tree_removed,
            "observation_error": cleanup_observation_error,
        },
    }
    _write_new_json(args.receipt_out, receipt)
    print(
        f"mineru-staged-load: {receipt_status.upper()} "
        f"stages={len(stage_results)}/{len(STAGE_DOCUMENT_COUNTS)} "
        f"receipt={args.receipt_out}"
    )
    return 0 if failure is None else 2


def _is_prefixed_sha256(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


def _normalized_sha256(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected input SHA-256 is invalid")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("expected input SHA-256 is invalid")
    return digest


if __name__ == "__main__":
    raise SystemExit(main())
