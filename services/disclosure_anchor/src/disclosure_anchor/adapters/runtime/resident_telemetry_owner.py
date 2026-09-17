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
from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import SynchronizedObserverResult
from disclosure_anchor.adapters.runtime.windows_resident_telemetry import windows_resident_collector_spec
from disclosure_anchor.application.contracts.mineru_capacity_config import decode_mineru_capacity_config
from disclosure_anchor.application.contracts.mineru_capacity_health import assert_profile_matches_capacity
from disclosure_anchor.application.contracts.mineru_process_profile import decode_mineru_process_profile
from disclosure_anchor.application.contracts.resident_combined_cpu import (
    CheckedCombinedResidentCpu, check_combined_resident_cpu,
)
from disclosure_anchor.application.contracts.resident_session_evidence import (
    RESIDENT_WIRE_LIFETIME_CEILING_MS, CheckedExternalWindowsObservation, CheckedResidentReady, artifact_sha256,
    canonical_bytes, check_external_windows_observation, check_mac_observer_identity,
    check_resident_closure, check_resident_configuration, check_resident_observer_mapping, check_resident_ready,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.contracts.windows_resident_telemetry import HostQueueBinding
from disclosure_anchor.application.services.m6_launch_budget import EXTENDED_COMMAND_CEILING_SECONDS
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    FrozenApiProcessProfile, ProcessProfileParameters, SynchronizedTelemetryReceiptV3,
    SynchronizedTelemetrySealV3,
)


_START = "start_mineru_resident_telemetry.ps1"
_OBSERVE = "observe_mineru_resident_session.ps1"
# The finite telemetry lifetime vector (F10), defined once from the primitive
# 8400 s transport ceiling: start transport = lane lifetime + 20 s control;
# lane lifetime = sampling + 60 s tail + 20 s control. Wire/PS/C#/Linux mirror
# the primitive and the 8390 s wire bound; the independent upper-bound vector
# test pins all of them (8300 / 8380 / 8390 / 8400).
RESIDENT_TRANSPORT_CEILING_SECONDS = EXTENDED_COMMAND_CEILING_SECONDS
RESIDENT_START_CONTROL_ALLOWANCE_SECONDS = 20
RESIDENT_SAMPLING_TAIL_SECONDS = 60
RESIDENT_LANE_LIFETIME_CEILING_MS = (RESIDENT_TRANSPORT_CEILING_SECONDS - RESIDENT_START_CONTROL_ALLOWANCE_SECONDS) * 1000
RESIDENT_SAMPLING_CEILING_SECONDS = (RESIDENT_LANE_LIFETIME_CEILING_MS // 1000
                                     - RESIDENT_SAMPLING_TAIL_SECONDS - RESIDENT_START_CONTROL_ALLOWANCE_SECONDS)
if RESIDENT_LANE_LIFETIME_CEILING_MS > RESIDENT_WIRE_LIFETIME_CEILING_MS:
    raise AssertionError("resident lane lifetime ceiling exceeds the wire configuration bound")


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
        if cast(int, config["lease_ms"]) != 30000 or cast(int, config["lifetime_ms"]) < (request.duration_seconds + RESIDENT_SAMPLING_TAIL_SECONDS) * 1000:
            raise ValueError("resident owner needs 30s leases and 60s lifecycle headroom")
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


def _finish_controls(commands: list[BoundedOwnerCommand], journal: _Journal, labels: list[str]) -> list[OwnerCommandResult]:
    results: dict[int, OwnerCommandResult] = {}
    while len(results) != len(commands):
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


def _close_lane(ssh: ResidentSSHConfig, ready: CheckedResidentReady) -> bytes:
    # Construct/use/close on this same thread; no shared HTTP client ownership.
    client = ResidentSSHHTTPClient(ssh, remote_port=cast(int, json.loads(ready.config_bytes)["port"]), maximum_response_bytes=65536)
    try:
        status, payload = client.get_bytes(f"/v1/{ready.session}/{ready.lane}/close", timeout_seconds=5, transport_attempts=1)
        if status != 200:
            raise ValueError(f"resident close returned HTTP {status}")
        return payload
    finally:
        client.close()


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
        journal.put("owner-intent.json", canonical_bytes({"run_id": request.run_id, "duration_seconds": request.duration_seconds, "source_hashes": dict(request.source_hashes), "ssh_executable_sha256": request.ssh_executable_sha256, "qualification": "diagnostic-only; external runtime qualification and full-hour acceptance remain separate"}))
        journal.put("process-profile.json", request.process_profile_bytes)
        journal.put("capacity-config.json", request.capacity_config_bytes)
        for lane, plan in zip(("gpu_fast", "host_slow"), plans, strict=True):
            journal.put(lane + "-config.json", plan.config_bytes)
            journal.put(lane + "-manifest.json", plan.manifest_bytes)
        notify("launching two finite resident sessions")
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
        ) for plan, (ready, _) in zip(plans, ready_pairs, strict=True)]
        observer = DedicatedMacObserver(DedicatedMacObserverRequest(
            request.observer_artifact_root, frozen, specs[0], specs[1], request.duration_seconds,
            request.run_id, gpu_ready.cadence_ms,
        ))
        journal.put("mac-observer-identity.json", observer.identity_bytes)
        observer.start()
        notify("independent Mac GO; combined resident sampling started")
        deadline = time.monotonic() + request.duration_seconds + 30
        result = None
        while result is None:
            _starter_ended(journal, starters, starter_labels, retained_starters, phase="observer completion")
            if time.monotonic() >= deadline:
                raise TimeoutError("resident observer finite lifecycle deadline")
            result = observer.poll(timeout=0.2)
        notify("Mac observer reaped; closing both remote sessions")
        close_attempted = True
        close_payloads: dict[str, bytes] = {}
        close_errors: list[Exception] = []
        # Loss of the close reply is not repeated; the independent on-disk
        # closure and original session owner determine the outcome below.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="resident-outer-close") as pool:
            closing = [pool.submit(_close_lane, request.ssh, ready) for ready, _ in ready_pairs]
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
        starter_results = _finish_controls(starters, journal, starter_labels)
        controls = []
        for plan, (_, previous) in zip(plans, ready_pairs, strict=True):
            command = _launch_command(request, plan, phase="closed", journal=journal, container_id=previous.container_id)
            controls.append(command)
            commands.append(command)
        closed_results = _finish_controls(controls, journal, ["gpu_fast-closed", "host_slow-closed"])
        if not isinstance(result.receipt, SynchronizedTelemetryReceiptV3) or not isinstance(result.seal, SynchronizedTelemetrySealV3):
            raise ValueError("resident owner requires exact v3 observer replay")
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
            check_resident_observer_mapping(ready=ready, closed_bytes=closed_bytes, frames=result.frames, receipt=result.receipt)
        if close_errors:
            raise ExceptionGroup("resident close has non-transport failures", close_errors)
        if _local_sources() != local_sources:
            raise ValueError("resident owner local composition source changed during execution")
        cpu = check_combined_resident_cpu(gpu_ready=gpu_ready, host_ready=host_ready, gpu_closure=closures[0], host_closure=closures[1], receipt=result.receipt, seal=result.seal)
        journal.put("owner-result.json", canonical_bytes({
            "contract_version": "mineru.resident-owner-diagnostic.v1", "run_id": request.run_id,
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
