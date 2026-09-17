"""Explicit finite resident-session owner; no activation, deployment or retry.

This composes the real remote starters, independent control observations, a
dedicated Mac observer and exact closure/CPU replay. The caller still owns
fresh runtime/profile qualification and full-host-hour/worker acceptance.
Nothing in this module turns a matching caller hash into external attestation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
import importlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import time
from typing import Literal, cast
from types import MappingProxyType
import uuid

from disclosure_anchor.adapters.runtime.dedicated_mac_observer import (
    DedicatedMacObserver, DedicatedMacObserverRequest,
)
from disclosure_anchor.adapters.runtime.bounded_http import BoundedHTTPTransportError
from disclosure_anchor.adapters.runtime.mac_observer_identity import MacObserverIdentityReader
from disclosure_anchor.adapters.runtime.resident_owner_control import (
    BoundedOwnerCommand, OwnerCommandResult, owner_ssh_command, pinned_windows_script,
)
from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig, ResidentSSHHTTPClient
from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import (
    MAX_RECEIPT_BYTES, PLAN_FILENAME, SynchronizedObserverResult, sampling_duration_ns,
)
from disclosure_anchor.adapters.runtime.windows_resident_telemetry import windows_resident_collector_spec
from disclosure_anchor.application.contracts.resident_combined_cpu import check_combined_resident_cpu_v4
from disclosure_anchor.application.contracts.mineru_capacity_config import decode_mineru_capacity_config
from disclosure_anchor.application.contracts.mineru_capacity_health import assert_profile_matches_capacity
from disclosure_anchor.application.contracts.mineru_process_profile import decode_mineru_process_profile
from disclosure_anchor.application.contracts.resident_combined_cpu import CheckedCombinedResidentCpu
from disclosure_anchor.application.contracts.resident_session_evidence import (
    RESIDENT_WIRE_LIFETIME_CEILING_MS, CheckedExternalWindowsObservation, CheckedResidentReady, artifact_sha256,
    canonical_bytes, check_external_windows_observation, check_mac_observer_identity,
    check_resident_closure, check_resident_configuration, check_resident_observer_mapping_v4, check_resident_ready,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.contracts.windows_resident_telemetry import PULL_VERSION, HostQueueBinding
from disclosure_anchor.application.services.m6_launch_budget import EXTENDED_COMMAND_CEILING_SECONDS
from disclosure_anchor.application.services.resident_measurement_policy import (
    LANE_MAX_SECONDS, POST_SAMPLE_MAX_SECONDS, PRE_GO_MAX_SECONDS, SAMPLE_MAX_SECONDS, require_start_headroom,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    FrozenApiProcessProfile, ProcessProfileParameters, SynchronizedSamplingPlanV1, SynchronizedTelemetryFrameV3,
    SynchronizedTelemetryReceiptV4, SynchronizedTelemetrySealV4, parse_canonical_json_artifact,
)


_START = "start_mineru_resident_telemetry.ps1"
_OBSERVE = "observe_mineru_resident_session.ps1"
# The finite telemetry vector (R22) has one source, resident_measurement_policy:
# sampling 8500 s; native lane = sampling + 20 s pre-GO + 60 s tail = 8580 s;
# wire/config = lane + 10 s; primitive Job/finite command = wire + 10 s = 8600 s.
# The start transport is the lane lifetime + 20 s control allowance.
RESIDENT_TRANSPORT_CEILING_SECONDS = EXTENDED_COMMAND_CEILING_SECONDS
RESIDENT_START_CONTROL_ALLOWANCE_SECONDS = 20
RESIDENT_PRE_GO_SECONDS = PRE_GO_MAX_SECONDS
RESIDENT_SAMPLING_TAIL_SECONDS = POST_SAMPLE_MAX_SECONDS
RESIDENT_LANE_LIFETIME_CEILING_MS = LANE_MAX_SECONDS * 1000
RESIDENT_SAMPLING_CEILING_SECONDS = SAMPLE_MAX_SECONDS
RESIDENT_DRAIN_GRACE_SECONDS = 10
if (RESIDENT_LANE_LIFETIME_CEILING_MS > RESIDENT_WIRE_LIFETIME_CEILING_MS
        or RESIDENT_LANE_LIFETIME_CEILING_MS // 1000 + RESIDENT_START_CONTROL_ALLOWANCE_SECONDS > RESIDENT_TRANSPORT_CEILING_SECONDS
        or RESIDENT_SAMPLING_CEILING_SECONDS + RESIDENT_PRE_GO_SECONDS + RESIDENT_SAMPLING_TAIL_SECONDS != RESIDENT_LANE_LIFETIME_CEILING_MS // 1000):
    raise AssertionError("resident lifetime vector layers are inconsistent")


@dataclass(frozen=True, slots=True)
class ResidentLaneLaunch:
    config_bytes: bytes
    manifest_bytes: bytes
    remote_config_path: str


@dataclass(frozen=True, slots=True)
class ResidentTelemetryOwnerRequest:
    # Evidence directory must be new. Remote private directories/configs and
    # compiled source bundle must already exist; this owner never overwrites.
    evidence_directory: Path
    observer_artifact_root: Path
    run_id: str
    gpu: ResidentLaneLaunch
    host: ResidentLaneLaunch
    source_hashes: Mapping[str, str]
    process_profile_bytes: bytes
    windows_node_identity_sha256: str
    ssh: ResidentSSHConfig
    ssh_executable: Path
    ssh_executable_sha256: str
    duration_seconds: float
    # The release's frozen capacity (exact MineruCapacityConfig bytes): the one
    # authority the host lane's raw API health is validated against.
    capacity_config_bytes: bytes


@dataclass(frozen=True, slots=True)
class ResidentTelemetryOwnerResult:
    observer: SynchronizedObserverResult
    cpu: CheckedCombinedResidentCpu
    evidence_directory: Path


class _Journal:
    """New-only, owner-private files anchored to one original directory FD."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute() or path.parent.resolve() != path.parent:
            raise ValueError("owner evidence parent must be an absolute resolved directory")
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError("owner evidence parent must be owned and mode 0700")
        path.mkdir(mode=0o700, exist_ok=False)
        self._fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        actual = os.fstat(self._fd)
        if actual.st_uid != os.geteuid() or stat.S_IMODE(actual.st_mode) != 0o700:
            os.close(self._fd)
            raise ValueError("new owner evidence directory is not private")
        self._path, self._identity = path, (actual.st_dev, actual.st_ino)
        self.hashes: dict[str, str] = {}

    def put(self, name: str, payload: bytes) -> None:
        current = self._path.lstat()
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != self._identity or stat.S_IMODE(current.st_mode) != 0o700 or current.st_uid != os.geteuid():
            raise ValueError("owner evidence directory identity changed")
        if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,100}", name) is None or len(payload) > 1048576:
            raise ValueError("owner evidence name/byte bound invalid")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
        try:
            view = memoryview(payload)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("owner evidence short write")
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self._fd)
        self.hashes[name] = artifact_sha256(payload)

    def close(self) -> None:
        os.close(self._fd)


def _configuration(plan: ResidentLaneLaunch) -> dict[str, object]:
    # Only used after check_resident_configuration has validated exact bytes.
    return cast(dict[str, object], json.loads(plan.config_bytes))


def _validate_request(request: ResidentTelemetryOwnerRequest) -> None:
    if (isinstance(request.duration_seconds, bool) or not isinstance(request.duration_seconds, (int, float))
            or not math.isfinite(request.duration_seconds) or not 0 < request.duration_seconds <= RESIDENT_SAMPLING_CEILING_SECONDS):
        raise ValueError("resident owner sampling duration invalid")
    if str(uuid.UUID(request.run_id)) != request.run_id:
        raise ValueError("resident owner run ID is not canonical")
    if not request.observer_artifact_root.is_absolute():
        raise ValueError("resident owner observer root must be absolute")
    profile = decode_mineru_process_profile(request.process_profile_bytes)
    capacity = decode_mineru_capacity_config(request.capacity_config_bytes)
    assert_profile_matches_capacity(profile, capacity)
    configurations = []
    for lane, plan in (("gpu_fast", request.gpu), ("host_slow", request.host)):
        check_resident_configuration(config_bytes=plan.config_bytes, manifest_bytes=plan.manifest_bytes, expected_source_hashes=request.source_hashes)
        config = _configuration(plan)
        if not PureWindowsPath(plan.remote_config_path).is_absolute() or config["lane"] != lane:
            raise ValueError("resident owner lane/config path differs")
        if cast(int, config["lease_ms"]) != 30000 or cast(int, config["lifetime_ms"]) < (request.duration_seconds + RESIDENT_PRE_GO_SECONDS + RESIDENT_SAMPLING_TAIL_SECONDS) * 1000:
            raise ValueError("resident owner needs 30s leases and pre-GO plus tail lifecycle headroom")
        if cast(int, config["lifetime_ms"]) > RESIDENT_LANE_LIFETIME_CEILING_MS:
            raise ValueError("resident owner lifetime plus control allowance exceeds the transport ceiling")
        owner = cast(dict[str, object], config["owner_identity"])
        if owner["runtime_bundle_identity_sha256"] != profile.runtime_bundle_identity_sha256 or owner["process_profile_sha256"] != artifact_sha256(request.process_profile_bytes):
            raise ValueError("resident owner exact process profile binding differs")
        if lane == "host_slow" and cast(dict[str, object], config["backend"])["capacity_config_sha256"] != capacity.sha256:
            raise ValueError("resident owner host lane names another capacity than the exact capacity config")
        configurations.append(config)
    gpu, host = configurations
    for field in ("session", "port"):
        if gpu[field] == host[field]:
            raise ValueError("resident owner lane resources collide")
    if PureWindowsPath(cast(str, gpu["run_directory"])) == PureWindowsPath(cast(str, host["run_directory"])) or PureWindowsPath(request.gpu.remote_config_path) == PureWindowsPath(request.host.remote_config_path):
        raise ValueError("resident owner remote paths collide")
    if canonical_bytes(gpu["owner_identity"]) != canonical_bytes(host["owner_identity"]):
        raise ValueError("resident owner host/boot/runtime/profile pairing differs")
    if profile.host_runtime_identity_sha256 != request.windows_node_identity_sha256:
        raise ValueError("resident owner Windows node differs from profile")
    host_assignment = artifact_sha256(canonical_bytes({
        "windows_node_identity_sha256": request.windows_node_identity_sha256,
        "gpu_uuid": cast(dict[str, object], gpu["backend"])["gpu_uuid"],
    }))
    if cast(dict[str, object], gpu["owner_identity"])["host_assignment_identity_sha256"] != host_assignment:
        raise ValueError("resident owner GPU host assignment differs")
    for name in (_START, _OBSERVE):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", request.source_hashes[name]) is None:
            raise ValueError("resident owner control source hash invalid")


def _launch_command(
    request: ResidentTelemetryOwnerRequest, plan: ResidentLaneLaunch, *, phase: Literal["start", "ready", "closed"],
    journal: _Journal, container_id: str | None = None,
) -> BoundedOwnerCommand:
    config = _configuration(plan)
    script_name = _START if phase == "start" else _OBSERVE
    arguments = {"ConfigJsonPath": plan.remote_config_path, "ExpectedConfigSha256": artifact_sha256(plan.config_bytes)}
    if phase != "start":
        arguments["Phase"] = phase
    if container_id is not None:
        arguments["ExpectedContainerId"] = container_id
    script = pinned_windows_script(
        script_path=str(PureWindowsPath(cast(str, config["source_directory"])) / script_name),
        expected_sha256=request.source_hashes[script_name], arguments=arguments,
    )
    command = owner_ssh_command(executable=request.ssh_executable, executable_sha256=request.ssh_executable_sha256, ssh=request.ssh, script=script)
    # Persist before Popen: loss of the launch reply cannot justify a retry.
    journal.put(f"{config['lane']}-{phase}-intent.json", canonical_bytes({"command": command, "script_sha256": artifact_sha256(script.encode()), "config_sha256": artifact_sha256(plan.config_bytes)}))
    timeout = cast(int, config["lifetime_ms"]) / 1000 + RESIDENT_START_CONTROL_ALLOWANCE_SECONDS if phase == "start" else 25
    # Only the start transport may span the extended finite lane lifetime;
    # the ready/closed controls keep the default outer-command ceiling.
    if phase == "start":
        return BoundedOwnerCommand(command, timeout_seconds=timeout, lifetime_ceiling_seconds=RESIDENT_TRANSPORT_CEILING_SECONDS)
    return BoundedOwnerCommand(command, timeout_seconds=timeout)


def _retain_command(journal: _Journal, label: str, result: OwnerCommandResult) -> None:
    journal.put(label + ".stdout", result.stdout)
    journal.put(label + ".stderr", result.stderr)
    journal.put(label + ".exit.json", canonical_bytes({"exit_code": result.exit_code}))


def _failed_names(results: dict[int, OwnerCommandResult], labels: list[str]) -> list[str]:
    return [f"{labels[index]} exit {result.exit_code}" for index, result in sorted(results.items()) if result.exit_code != 0]


def _finish_controls(
    commands: list[BoundedOwnerCommand], journal: _Journal, labels: list[str], *, deadline_ns: int | None = None,
) -> list[OwnerCommandResult]:
    """Wait for every command, each within min(its own bound, the caller's absolute deadline)."""
    results: dict[int, OwnerCommandResult] = {}
    while len(results) != len(commands):
        if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
            pending = [labels[index] for index in range(len(commands)) if index not in results]
            raise TimeoutError("telemetry_post_sampling_deadline: " + ", ".join(pending) + " did not end by the planned end plus tail")
        for index, command in enumerate(commands):
            if index in results:
                continue
            result = command.poll(timeout=0.05)
            if result is not None:
                _retain_command(journal, labels[index], result)
                results[index] = result
    failed = _failed_names(results, labels)
    if failed:
        raise RuntimeError("resident owner command failed: " + ", ".join(failed) + "; exact raw outcome retained, no retry")
    return [results[index] for index in range(len(commands))]


def _starter_ended(
    journal: _Journal, starters: list[BoundedOwnerCommand], labels: list[str], retained: set[str], *, phase: str,
) -> None:
    """Retain and name any starter that ended while its remote session was still owed.

    A starter is the local transport of one remote Job. Its end before READY or
    before the observer completed is a genuine startup/lifecycle failure: the
    exact exit code, stdout and stderr are retained immediately, before any abort
    could discard bytes the local pipe still holds. Nothing is waited out or retried.
    """
    ended: list[str] = []
    for starter, label in zip(starters, labels, strict=True):
        if label in retained:
            continue
        result = starter.poll()
        if result is not None:
            _retain_command(journal, label, result)
            retained.add(label)
            ended.append(f"{label} exit {result.exit_code}")
    if ended:
        raise RuntimeError(f"resident starter ended before {phase}: " + ", ".join(ended) + "; exact raw outcome retained, no retry")


def _await_ready_controls(
    controls: list[BoundedOwnerCommand], journal: _Journal, labels: list[str], *,
    starters: list[BoundedOwnerCommand], starter_labels: list[str], retained: set[str],
) -> list[OwnerCommandResult]:
    """Wait for the READY controls while watching the starters they observe.

    The controls have their own 10 s remote deadline; a starter that dies at
    bootstrap ends within milliseconds with the real reason on its stderr, so
    it is polled in the same loop and reported first, by name, instead of being
    discovered later as an empty aborted transport.
    """
    results: dict[int, OwnerCommandResult] = {}
    while len(results) != len(controls):
        _starter_ended(journal, starters, starter_labels, retained, phase="READY")
        for index, control in enumerate(controls):
            if index in results:
                continue
            result = control.poll(timeout=0.05)
            if result is not None:
                _retain_command(journal, labels[index], result)
                results[index] = result
    failed = _failed_names(results, labels)
    if failed:
        # A lane whose control timed out has usually ended its starter already;
        # retain that exact outcome before the generic abort path runs.
        _starter_ended(journal, starters, starter_labels, retained, phase="READY")
        raise RuntimeError("resident owner READY control failed: " + ", ".join(failed) + "; exact raw outcome retained, no retry")
    return [results[index] for index in range(len(controls))]


def _external_ready(request: ResidentTelemetryOwnerRequest, plan: ResidentLaneLaunch, result: OwnerCommandResult) -> tuple[CheckedResidentReady, CheckedExternalWindowsObservation]:
    outer = strict_json_loads(result.stdout)
    if not isinstance(outer, dict) or not isinstance(outer.get("ready_raw"), str):
        raise ValueError("resident owner READY response lacks exact artifact")
    ready = check_resident_ready(config_bytes=plan.config_bytes, ready_bytes=outer["ready_raw"].encode(), manifest_bytes=plan.manifest_bytes, expected_source_hashes=request.source_hashes)
    checked = check_external_windows_observation(payload=result.stdout, ready=ready, windows_node_identity_sha256=request.windows_node_identity_sha256)
    return ready, checked


def _close_lane(ssh: ResidentSSHConfig, ready: CheckedResidentReady, *, deadline_ns: int | None = None) -> bytes:
    # Construct/use/close on this same thread; no shared HTTP client ownership.
    # The close keeps its own 5 s cap and never runs past the caller's absolute deadline.
    timeout = 5.0 if deadline_ns is None else min(5.0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)
    if timeout <= 0:
        raise TimeoutError(f"telemetry_post_sampling_deadline: no time left to close {ready.lane}")
    client = ResidentSSHHTTPClient(ssh, remote_port=cast(int, json.loads(ready.config_bytes)["port"]), maximum_response_bytes=65536)
    try:
        status, payload = client.get_bytes(f"/v1/{ready.session}/{ready.lane}/close", timeout_seconds=timeout, transport_attempts=1)
        if status != 200:
            raise ValueError(f"resident close returned HTTP {status}")
        return payload
    finally:
        client.close()


def _read_recorded_plan(artifact_root: Path, run_id: str) -> bytes:
    """Bounded read of the child's original plan file: no symlink, no oversize, no guess."""
    if not artifact_root.is_absolute():
        raise ValueError("resident observer artifact root must be absolute")
    directory_fd = os.open(artifact_root / run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(PLAN_FILENAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_RECEIPT_BYTES:
            raise ValueError("resident observer plan file is not a bounded regular file")
        payload = os.read(descriptor, MAX_RECEIPT_BYTES + 1)
    finally:
        os.close(descriptor)
    if not payload or len(payload) != metadata.st_size:
        raise ValueError("resident observer plan file is empty or changed while it was read")
    return payload


def _local_sources() -> dict[str, bytes]:
    """Snapshot the concrete local composition, not a caller-selected label."""
    modules = (
        "adapters.runtime.resident_telemetry_owner", "adapters.runtime.resident_owner_control",
        "adapters.runtime.dedicated_mac_observer", "adapters.runtime.mac_observer_identity",
        "adapters.runtime.synchronized_telemetry_observer", "adapters.runtime.windows_resident_telemetry",
        "adapters.runtime.resident_ssh_http", "adapters.runtime.bounded_http",
        "application.contracts.resident_session_evidence", "application.contracts.resident_combined_cpu",
        "application.contracts.windows_resident_telemetry", "application.contracts.synchronized_telemetry",
        "application.contracts.strict_json", "application.contracts.mineru_process_profile",
        "application.contracts.mineru_capacity_config", "application.contracts.mineru_capacity_health",
        "application.contracts.mineru_api_health", "application.ports.synchronized_telemetry",
        "application.services.resident_measurement_policy",
    )
    return {name: Path(cast(str, importlib.import_module("disclosure_anchor." + name).__file__)).read_bytes() for name in modules}


def run_resident_telemetry_session(
    request: ResidentTelemetryOwnerRequest, *, progress: Callable[[str], None] | None = None,
) -> ResidentTelemetryOwnerResult:
    """One finite explicit invocation; retain failures without blind relaunch.

    Remote start/close and external observations are outside the Mac observer's
    sampling cost. No per-tick CLI, compilation, snapshot probe or queue write.
    Even a successful result is not runtime qualification or activation.
    """
    request = replace(request, source_hashes=MappingProxyType(dict(request.source_hashes)))
    _validate_request(request)
    journal = _Journal(request.evidence_directory)
    commands: list[BoundedOwnerCommand] = []
    starters: list[BoundedOwnerCommand] = []
    starter_labels = ["gpu_fast-start", "host_slow-start"]
    retained_starters: set[str] = set()
    ready_pairs: list[tuple[CheckedResidentReady, CheckedExternalWindowsObservation]] = []
    observer: DedicatedMacObserver | None = None
    close_attempted = False
    notify = progress if progress is not None else lambda _: None
    try:
        plans = (request.gpu, request.host)
        local_sources = _local_sources()
        for index, (name, source) in enumerate(local_sources.items()):
            journal.put(f"local-source-{index}.py", source)
        journal.put("local-sources.json", canonical_bytes({name: {"file": f"local-source-{index}.py", "sha256": artifact_sha256(source)} for index, (name, source) in enumerate(local_sources.items())}))
        owner_intent = canonical_bytes({"run_id": request.run_id, "duration_seconds": request.duration_seconds, "source_hashes": dict(request.source_hashes), "ssh_executable_sha256": request.ssh_executable_sha256, "qualification": "diagnostic-only; external runtime qualification and full-hour acceptance remain separate"})
        journal.put("owner-intent.json", owner_intent)
        owner_intent_sha256 = artifact_sha256(owner_intent)
        journal.put("process-profile.json", request.process_profile_bytes)
        journal.put("capacity-config.json", request.capacity_config_bytes)
        for lane, plan in zip(("gpu_fast", "host_slow"), plans, strict=True):
            journal.put(lane + "-config.json", plan.config_bytes)
            journal.put(lane + "-manifest.json", plan.manifest_bytes)
        notify("launching two finite resident sessions")
        # The earliest native starter's local spawn instant bounds the pre-GO
        # reserve; it is the same Mac monotonic domain the observer plans in.
        earliest_starter_spawn_ns = time.monotonic_ns()
        for plan in plans:
            command = _launch_command(request, plan, phase="start", journal=journal)
            commands.append(command)
            starters.append(command)
        controls = []
        for plan in plans:
            command = _launch_command(request, plan, phase="ready", journal=journal)
            commands.append(command)
            controls.append(command)
        results = _await_ready_controls(
            controls, journal, ["gpu_fast-ready", "host_slow-ready"],
            starters=starters, starter_labels=starter_labels, retained=retained_starters,
        )
        for plan, control_result in zip(plans, results, strict=True):
            ready_pairs.append(_external_ready(request, plan, control_result))
        gpu_ready, host_ready = (pair[0] for pair in ready_pairs)
        host_value = json.loads(host_ready.ready_bytes)["backend"]["linux_ready"]["sampler_ready"]["identity"]
        profile = decode_mineru_process_profile(request.process_profile_bytes)
        frozen = FrozenApiProcessProfile(
            process_epoch_sha256=artifact_sha256(canonical_bytes({"boot_id": host_value["boot_id"], **host_value["members"]["api"]})),
            runtime_bundle_identity_sha256=profile.runtime_bundle_identity_sha256,
            process_profile_sha256=artifact_sha256(request.process_profile_bytes),
            parameters=ProcessProfileParameters.model_validate({key: getattr(profile, key) for key in ProcessProfileParameters.model_fields}),
        )
        clock = check_mac_observer_identity(MacObserverIdentityReader().observe()).clock_domain_identity_sha256
        collector_source = artifact_sha256(canonical_bytes({name: artifact_sha256(source) for name, source in local_sources.items()}))
        # The host lane's raw API health is bound to the frozen capacity and to
        # the very API process the Linux sampler measures (from this READY).
        host_binding = HostQueueBinding(
            expected_capacity=decode_mineru_capacity_config(request.capacity_config_bytes),
            serving_namespace_pid=cast(int, cast(dict[str, object], _configuration(request.host)["backend"])["api_namespace_pid"]),
            api_boot_id=cast(str, host_value["boot_id"]),
            api_start_ticks=cast(int, cast(dict[str, object], cast(dict[str, object], host_value["members"])["api"])["start_ticks"]),
            task_retention_seconds=profile.task_retention_seconds, task_cleanup_interval_seconds=profile.task_cleanup_interval_seconds,
        )
        specs = [windows_resident_collector_spec(
            collector_identity_sha256=collector_source, observer_clock_domain_identity_sha256=clock,
            lane=cast(Literal["gpu_fast", "host_slow"], ready.lane), base_url=f"http://127.0.0.1:{_configuration(plan)['port']}",
            path=f"/v1/{ready.session}/{ready.lane}", maximum_response_bytes=65536,
            maximum_sample_age_ms=1000, nominal_interval_ms=ready.cadence_ms, expected_identity=ready.identity,
            ssh=asdict(request.ssh), host_binding=host_binding if ready.lane == "host_slow" else None,
            pull_protocol=PULL_VERSION,
        ) for plan, (ready, _) in zip(plans, ready_pairs, strict=True)]
        observer = DedicatedMacObserver(DedicatedMacObserverRequest(
            request.observer_artifact_root, frozen, specs[0], specs[1], request.duration_seconds,
            request.run_id, gpu_ready.cadence_ms, receipt_version=4, owner_intent_sha256=owner_intent_sha256,
        ))
        journal.put("mac-observer-identity.json", observer.identity_bytes)
        observer.start()
        notify("independent Mac GO; combined resident sampling started")
        # PLAN_RECORDED: the child froze the plan before its first collect.
        plan_deadline = time.monotonic() + 30
        plan_event: dict[str, object] | None = None
        while plan_event is None:
            _starter_ended(journal, starters, starter_labels, retained_starters, phase="plan recording")
            if time.monotonic() >= plan_deadline:
                raise TimeoutError("resident observer did not record its sampling plan in time")
            plan_event = observer.poll_event(timeout=0.2)
        journal.put("observer-plan-recorded.json", canonical_bytes(plan_event))
        # The event only announces the plan; the child's original plan bytes are the
        # authority. They are read now, bounded, hash-bound to the event, and checked
        # against this owner's intent and the observer's independently read identity
        # before any deadline is derived from them. A foreign plan ends the session here.
        plan_bytes = _read_recorded_plan(request.observer_artifact_root, request.run_id)
        if artifact_sha256(plan_bytes) != plan_event["sampling_plan_sha256"]:
            raise ValueError("resident observer plan bytes differ from its plan_recorded event")
        sampling_plan = SynchronizedSamplingPlanV1.model_validate(
            parse_canonical_json_artifact(plan_bytes, label="sampling plan", maximum_bytes=MAX_RECEIPT_BYTES),
        )
        observer_identity = check_mac_observer_identity(observer.identity_bytes)
        if (sampling_plan.run_id != request.run_id or sampling_plan.owner_intent_sha256 != owner_intent_sha256
                or sampling_plan.duration_ns != sampling_duration_ns(request.duration_seconds)
                or sampling_plan.observer_clock_domain_identity_sha256 != observer_identity.clock_domain_identity_sha256
                or sampling_plan.gpu_nominal_interval_ms != gpu_ready.cadence_ms
                or sampling_plan.host_nominal_interval_ms != host_ready.cadence_ms
                or sampling_plan.started_monotonic_ns != plan_event["started_monotonic_ns"]
                or sampling_plan.planned_end_monotonic_ns != plan_event["planned_end_monotonic_ns"]):
            raise ValueError("resident observer plan differs from the owner intent, the observer identity or its event")
        journal.put("sampling-plan.v1.json", plan_bytes)
        planned_start_ns, planned_end_ns = sampling_plan.started_monotonic_ns, sampling_plan.planned_end_monotonic_ns
        # Pre-GO reserve: native starters spawned at most 20 s before sampling started,
        # in the same Mac monotonic domain. Exceeding it ends the session before any admission.
        require_start_headroom(earliest_starter_spawn_ns=earliest_starter_spawn_ns, sampling_start_ns=planned_start_ns)
        # SAMPLING → SAMPLING_DRAINED, watched with the starters; no replay yet.
        drain_deadline_ns = planned_end_ns + RESIDENT_DRAIN_GRACE_SECONDS * 1_000_000_000
        post_deadline_ns = planned_end_ns + RESIDENT_SAMPLING_TAIL_SECONDS * 1_000_000_000
        drained_event: dict[str, object] | None = None
        while drained_event is None:
            _starter_ended(journal, starters, starter_labels, retained_starters, phase="sampling")
            if time.monotonic_ns() >= drain_deadline_ns:
                journal.put("telemetry-drain-timeout.json", canonical_bytes({"run_id": request.run_id, "planned_end_monotonic_ns": planned_end_ns, "observed_monotonic_ns": time.monotonic_ns()}))
                raise TimeoutError("telemetry_drain_timeout: the observer did not announce its sampling drain by the planned end plus grace")
            drained_event = observer.poll_event(timeout=0.2)
        journal.put("observer-sampling-drained.json", canonical_bytes(drained_event))
        notify("sampling drained; closing both remote sessions before the observer replay")
        close_attempted = True
        close_payloads: dict[str, bytes] = {}
        close_errors: list[Exception] = []
        # Loss of the close reply is not repeated; the independent on-disk
        # closure and original session owner determine the outcome below.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="resident-outer-close") as pool:
            closing = [pool.submit(_close_lane, request.ssh, ready, deadline_ns=post_deadline_ns) for ready, _ in ready_pairs]
            for (ready, _), future in zip(ready_pairs, closing, strict=True):
                try:
                    payload = future.result()
                except Exception as exc:
                    journal.put(ready.lane + "-close-response-error.json", canonical_bytes({"error_type": type(exc).__name__, "message": str(exc)}))
                    if not isinstance(exc, BoundedHTTPTransportError):
                        close_errors.append(exc)
                else:
                    journal.put(ready.lane + "-close-response.json", payload)
                    close_payloads[ready.lane] = payload
        starter_results = _finish_controls(starters, journal, starter_labels, deadline_ns=post_deadline_ns)
        controls = []
        for plan, (_, previous) in zip(plans, ready_pairs, strict=True):
            command = _launch_command(request, plan, phase="closed", journal=journal, container_id=previous.container_id)
            controls.append(command)
            commands.append(command)
        closed_results = _finish_controls(controls, journal, ["gpu_fast-closed", "host_slow-closed"], deadline_ns=post_deadline_ns)
        # NATIVE_CLOSING done: now the child's real exit, then the expensive replay, all
        # inside the absolute post-sampling deadline (plan end + 60 s).
        while not observer.wait_exit(timeout=min(60.0, max(0.0, (post_deadline_ns - time.monotonic_ns()) / 1_000_000_000))):
            if time.monotonic_ns() >= post_deadline_ns:
                raise TimeoutError("telemetry_post_sampling_deadline: the observer child did not exit by the planned end plus tail")
        if time.monotonic_ns() >= post_deadline_ns:
            raise TimeoutError("telemetry_post_sampling_deadline: no time left for the observer replay")
        result = observer.replay_result(deadline_ns=post_deadline_ns)
        if time.monotonic_ns() > post_deadline_ns:
            raise TimeoutError("telemetry_post_sampling_deadline: the observer replay ended past the planned end plus tail")
        if not isinstance(result.receipt, SynchronizedTelemetryReceiptV4) or not isinstance(result.seal, SynchronizedTelemetrySealV4) or result.plan is None:
            raise ValueError("resident owner requires exact v4 observer replay")
        # The replay binds the receipt to the plan file bytes; those must be the exact
        # plan this owner validated at plan_recorded, not merely a plan with the same hash claim.
        if result.receipt.sampling_plan_sha256 != plan_event["sampling_plan_sha256"] or result.plan != sampling_plan:
            raise ValueError("resident owner replayed plan differs from the plan validated at plan_recorded")
        frames = cast(tuple[SynchronizedTelemetryFrameV3, ...], result.frames)
        closures = []
        for (ready, previous), control, starter_result in zip(ready_pairs, closed_results, starter_results, strict=True):
            check_external_windows_observation(payload=control.stdout, ready=ready, windows_node_identity_sha256=request.windows_node_identity_sha256, previous_ready=previous)
            external = json.loads(control.stdout)
            closed_bytes, job_bytes = external["closed_raw"].encode(), external["job_raw"].encode()
            if ready.lane in close_payloads and close_payloads[ready.lane] != closed_bytes:
                raise ValueError("resident close HTTP bytes differ from original disk artifact")
            if starter_result.stdout.rstrip(b"\r\n") != job_bytes:
                raise ValueError("resident starter stdout differs from independently reread original Job")
            closures.append(check_resident_closure(ready=ready, closed_bytes=closed_bytes, job_bytes=job_bytes, linux_closed_bytes=external["linux_closed_raw"].encode() if ready.lane == "host_slow" else None))
            check_resident_observer_mapping_v4(ready=ready, closed_bytes=closed_bytes, frames=frames, receipt=result.receipt, plan=result.plan)
        if close_errors:
            raise ExceptionGroup("resident close has non-transport failures", close_errors)
        if _local_sources() != local_sources:
            raise ValueError("resident owner local composition source changed during execution")
        cpu = check_combined_resident_cpu_v4(gpu_ready=gpu_ready, host_ready=host_ready, gpu_closure=closures[0], host_closure=closures[1], receipt=result.receipt, seal=result.seal)
        journal.put("owner-result.json", canonical_bytes({
            "contract_version": "mineru.resident-owner-diagnostic.v2", "run_id": request.run_id, "receipt_version": 4,
            "sampling_plan_sha256": result.receipt.sampling_plan_sha256, "owner_intent_sha256": owner_intent_sha256,
            "observer_status": result.evidence_status, "observer_receipt_sha256": artifact_sha256(canonical_bytes(result.receipt.model_dump(mode="json"))),
            "observer_seal_sha256": artifact_sha256(canonical_bytes(result.seal.model_dump(mode="json"))),
            "cpu": asdict(cpu), "total_cpu_ns": cpu.total_cpu_ns, "within_two_percent": cpu.within_two_percent,
            "evidence_sha256": dict(journal.hashes), "activation_authorized": False,
        }))
        notify("exact closure, source mapping and seven-role CPU replay completed")
        return ResidentTelemetryOwnerResult(result, cpu, request.evidence_directory)
    except BaseException as exc:
        failures: list[BaseException] = [exc]
        if observer is not None:
            try:
                observer.close()
            except BaseException as cleanup_error:
                failures.append(cleanup_error)
        if not close_attempted and ready_pairs:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="resident-failed-close") as pool:
                futures = [pool.submit(_close_lane, request.ssh, ready) for ready, _ in ready_pairs]
                for future in futures:
                    try:
                        future.result()
                    except BaseException as cleanup_error:
                        failures.append(cleanup_error)
        # These are local transports only. Finite remote leases/Jobs persist
        # independently; an abort never constitutes remote absence evidence.
        for index, command in enumerate(commands):
            drained: OwnerCommandResult | None = None
            try:
                # Bounded drain before the abort closes the pipes: read what the
                # local pipe already holds and the exit code if the transport has
                # ended. This waits at most the poll bound, never a deadline.
                drained = command.poll(timeout=0.25)
            except BaseException as cleanup_error:
                failures.append(cleanup_error)
            try:
                command.abort()
            except BaseException as cleanup_error:
                failures.append(cleanup_error)
            try:
                stdout, stderr = command.captured_output
                journal.put(f"failure-command-{index}.stdout", stdout)
                journal.put(f"failure-command-{index}.stderr", stderr)
                journal.put(f"failure-command-{index}.state.json", canonical_bytes({
                    "exit_code": None if drained is None else drained.exit_code,
                    "ended_before_abort": drained is not None, **command.retention_report(),
                }))
            except BaseException as cleanup_error:
                failures.append(cleanup_error)
        journal.put("owner-failure.json", canonical_bytes({"run_id": request.run_id, "errors": [{"type": type(error).__name__, "message": str(error)} for error in failures], "remote_outcome": "requires exact session reconciliation; never automatically relaunched"}))
        raise BaseExceptionGroup("resident telemetry owner failed; original evidence retained", failures)
    finally:
        journal.close()
