"""Pure replay of private resident-session mechanisms, not runtime activation.

Canonical config/READY/close/Job/Linux bytes prove their mutual binding. The
execution owner must additionally observe actual live processes, loaded sources,
host/runtime/profile and exact container absence. This module never infers those
observations from a self-reported hash or a caller-provided success boolean.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import PureWindowsPath
import re
from typing import cast
import uuid

from disclosure_anchor.application.contracts.synchronized_telemetry import (
    SynchronizedTelemetryFrameV2,
    SynchronizedTelemetryReceiptV3,
    TelemetryObserverIdentity,
    parse_canonical_json_artifact,
)
from disclosure_anchor.application.contracts.windows_resident_telemetry import ResidentIdentity
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


_PS_SOURCES = (
    "load_mineru_resident_session.ps1", "load_mineru_telemetry_assembly.ps1",
    "mineru_resident_telemetry_exporter.ps1", "start_mineru_resident_telemetry.ps1",
    "linux_resident_host_sampler.py", "linux_resident_host_supervisor.py",
)
_CS_SOURCES = {
    "mineru_nvml_backend.cs": "nvml_source_sha256",
    "mineru_resident_wire.cs": "wire_source_sha256",
    "mineru_telemetry_job_supervisor.cs": "supervisor_source_sha256",
}
_OWNER_FIELDS = (
    "host_assignment_identity_sha256", "boot_identity_sha256",
    "runtime_bundle_identity_sha256", "process_profile_sha256",
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def artifact_sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class _Object:
    def __init__(self, value: object, keys: str | tuple[str, ...]) -> None:
        expected = keys.split() if isinstance(keys, str) else keys
        if not isinstance(value, dict) or set(value) != set(expected):
            raise ValueError(f"resident evidence object shape differs: {expected}")
        self.value = cast(dict[str, object], value)

    def get(self, key: str) -> object:
        return self.value[key]

    def text(self, key: str) -> str:
        value = self.get(key)
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError(f"resident evidence string required: {key}")
        return value

    def integer(self, key: str, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
        value = self.get(key)
        if type(value) is not int or not minimum <= cast(int, value) <= maximum:
            raise ValueError(f"resident evidence bounded integer required: {key}")
        return cast(int, value)

    def sha(self, key: str) -> str:
        value = self.text(key)
        if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise ValueError(f"resident evidence canonical hash required: {key}")
        return value

    def expect(self, key: str, expected: object) -> None:
        if canonical_bytes(self.get(key)) != canonical_bytes(expected):
            raise ValueError(f"resident evidence binding differs: {key}")


def _decode(payload: bytes, keys: str | tuple[str, ...], maximum: int = 65536) -> _Object:
    return _Object(parse_canonical_json_artifact(payload, label="resident session evidence", maximum_bytes=maximum), keys)


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("resident timestamp must be UTC text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("resident timestamp must be UTC")
    return parsed


def _elapsed_ns(start: datetime, end: datetime) -> int:
    value = end - start
    return (value.days * 86400 + value.seconds) * 1_000_000_000 + value.microseconds * 1000


def _clock_delta(start_ns: int, finish_ns: int, start_wall: datetime, finish_wall: datetime) -> None:
    elapsed = finish_ns - start_ns
    if elapsed < 0 or abs(_elapsed_ns(start_wall, finish_wall) - elapsed) > 50_000_000 + elapsed * 50 // 1_000_000:
        raise ValueError("resident source wall/monotonic divergence")


def check_mac_observer_identity(payload: bytes) -> TelemetryObserverIdentity:
    """Recompute OS process/clock hashes; owner must independently read the PID."""
    value = _decode(payload, "contract_version process clock", 4096)
    value.expect("contract_version", "mineru.mac-observer-identity.v1")
    process = _Object(value.get("process"), "pid parent_pid uid start_time_unix_seconds start_time_microseconds boot_session_uuid")
    process.integer("pid", 1, 2**31 - 1)
    process.integer("parent_pid", 0, 2**31 - 1)
    process.integer("uid", 0, 2**32 - 1)
    process.integer("start_time_unix_seconds", 1)
    process.integer("start_time_microseconds", 0, 999999)
    boot = process.text("boot_session_uuid")
    if str(uuid.UUID(boot)).upper() != boot.upper():
        raise ValueError("Mac observer boot UUID is not canonical")
    clock = _Object(value.get("clock"), "boot_session_uuid kernel_release implementation monotonic adjustable resolution_seconds")
    clock.expect("boot_session_uuid", boot)
    clock.expect("implementation", "mach_absolute_time()")
    clock.expect("monotonic", True)
    clock.expect("adjustable", False)
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", clock.text("kernel_release")) is None:
        raise ValueError("Mac observer kernel release malformed")
    resolution = clock.get("resolution_seconds")
    if type(resolution) is not float or not math.isfinite(resolution) or not 0 < resolution <= 1:
        raise ValueError("Mac observer clock resolution invalid")
    return TelemetryObserverIdentity(
        process_epoch_sha256=artifact_sha256(canonical_bytes(process.value)),
        clock_domain_identity_sha256=artifact_sha256(canonical_bytes(clock.value)),
    )


@dataclass(frozen=True, slots=True)
class CheckedResidentReady:
    """Self-consistent raw evidence; no claim of independent live attestation."""

    config_bytes: bytes
    ready_bytes: bytes
    manifest_bytes: bytes
    session: str
    lane: str
    cadence_ms: int
    identity: ResidentIdentity
    pid: int
    creation_filetime_100ns: int


def check_resident_configuration(
    *, config_bytes: bytes, manifest_bytes: bytes,
    expected_source_hashes: Mapping[str, str],
) -> None:
    """Validate the complete launch input before any remote process exists."""
    config = _decode(config_bytes, "contract_version session lane cadence_ms port lease_ms lifetime_ms sampling_timeout_ms response_timeout_ms run_directory source_directory prepared sources powershell_executable_sha256 owner_identity backend", 32768)
    config.expect("contract_version", "mineru.windows-resident-session.v1")
    session = config.text("session")
    if re.fullmatch(r"[0-9a-f]{32}", session) is None or uuid.UUID(hex=session).variant != uuid.RFC_4122:
        raise ValueError("resident session must be a canonical GUID")
    lane = config.text("lane")
    cadence = config.integer("cadence_ms")
    if (lane, cadence) not in {("gpu_fast", 250), ("gpu_fast", 500), ("host_slow", 1000)}:
        raise ValueError("resident lane cadence differs")
    config.integer("port", 1024, 65535)
    config.integer("lifetime_ms", config.integer("lease_ms", 2000, 30000), 7190000)
    config.integer("sampling_timeout_ms", 1, cadence)
    config.integer("response_timeout_ms", 1, 1000)
    config.sha("powershell_executable_sha256")
    for field in ("run_directory", "source_directory"):
        if not PureWindowsPath(config.text(field)).is_absolute():
            raise ValueError("resident Windows absolute directory required")
    sources = _Object(config.get("sources"), _PS_SOURCES)
    for name in _PS_SOURCES:
        if sources.sha(name) != expected_source_hashes[name]:
            raise ValueError(f"resident source drift: {name}")
    owner = _Object(config.get("owner_identity"), _OWNER_FIELDS)
    for name in _OWNER_FIELDS:
        owner.sha(name)
    prepared = _Object(config.get("prepared"), "manifest_path manifest_sha256 nvml_source_sha256 wire_source_sha256 supervisor_source_sha256")
    prepared.expect("manifest_sha256", artifact_sha256(manifest_bytes))
    manifest = _decode(manifest_bytes, "assembly_name assembly_sha256 compiler_arguments compiler_path compiler_sha256 contract_version http_assembly_sha256 powershell_version preparation_recipe_sha256 runtime_version sources system_assembly_sha256", 32768)
    manifest.expect("contract_version", "mineru.telemetry-prepared-assembly.v2")
    manifest.expect("assembly_name", "mineru-telemetry.dll")
    for name in ("assembly_sha256", "compiler_sha256", "http_assembly_sha256", "system_assembly_sha256"):
        manifest.sha(name)
    manifest.expect("preparation_recipe_sha256", expected_source_hashes["build_mineru_telemetry_assembly.ps1"])
    manifest_sources = manifest.get("sources")
    if not isinstance(manifest_sources, list) or len(manifest_sources) != 3:
        raise ValueError("resident prepared source count differs")
    expected_prepared_sources = []
    for name, field in sorted(_CS_SOURCES.items()):
        prepared.expect(field, expected_source_hashes[name])
        expected_prepared_sources.append({"name": name, "sha256": expected_source_hashes[name]})
    manifest.expect("sources", expected_prepared_sources)
    compiler = PureWindowsPath(manifest.text("compiler_path"))
    manifest_path = PureWindowsPath(prepared.text("manifest_path"))
    source_root = PureWindowsPath(config.text("source_directory"))
    if not compiler.is_absolute() or compiler.name != "csc.exe" or not manifest_path.is_absolute():
        raise ValueError("resident compiler/manifest path differs")
    manifest.expect("compiler_arguments", [
        "/nologo", "/noconfig", "/target:library", "/warnaserror+",
        "/out:" + str(manifest_path.parent / "mineru-telemetry.dll"),
        "/reference:" + str(compiler.parent / "System.dll"),
        "/reference:" + str(compiler.parent / "System.Net.Http.dll"),
        *(str(source_root / name) for name in sorted(_CS_SOURCES)),
    ])
    if lane == "gpu_fast":
        backend = _Object(config.get("backend"), "nvml_dll_sha256 gpu_uuid")
        backend.sha("nvml_dll_sha256")
        if re.fullmatch(r"GPU-[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", backend.text("gpu_uuid")) is None:
            raise ValueError("resident GPU UUID malformed")
    else:
        _check_linux_configuration(config)


def check_resident_ready(
    *, config_bytes: bytes, ready_bytes: bytes, manifest_bytes: bytes,
    expected_source_hashes: Mapping[str, str],
) -> CheckedResidentReady:
    """Pin exact preparation/source bytes and recompute READY clock/epoch."""
    check_resident_configuration(config_bytes=config_bytes, manifest_bytes=manifest_bytes, expected_source_hashes=expected_source_hashes)
    # Shapes and launch-only semantics have already been checked above.
    config_value = cast(dict[str, object], json.loads(config_bytes))
    config = _Object(config_value, tuple(config_value))
    manifest_value = cast(dict[str, object], json.loads(manifest_bytes))
    manifest = _Object(manifest_value, tuple(manifest_value))
    owner = _Object(config.get("owner_identity"), _OWNER_FIELDS)
    sources = _Object(config.get("sources"), _PS_SOURCES)
    session, lane, cadence = config.text("session"), config.text("lane"), config.integer("cadence_ms")
    ready = _decode(ready_bytes, "backend cadence_ms clock config_sha256 contract_version identity lane port process session")
    ready.expect("contract_version", "mineru.windows-resident-ready.v1")
    for name in ("session", "lane", "port", "cadence_ms"):
        ready.expect(name, config.get(name))
    config_sha = artifact_sha256(config_bytes)
    ready.expect("config_sha256", config_sha)
    identity = ResidentIdentity.model_validate(ready.get("identity"))
    expected_identity = dict(owner.value)
    clock = _Object(ready.get("clock"), "boot_identity_sha256 clock_source frequency_hz")
    clock.expect("boot_identity_sha256", owner.get("boot_identity_sha256"))
    clock.expect("clock_source", "QueryPerformanceCounter")
    clock.integer("frequency_hz", 1)
    process = _Object(ready.get("process"), "assembly_sha256 config_sha256 creation_filetime_100ns manifest_sha256 pid session")
    for name, value in (("session", session), ("config_sha256", config_sha), ("manifest_sha256", artifact_sha256(manifest_bytes)), ("assembly_sha256", manifest.get("assembly_sha256"))):
        process.expect(name, value)
    pid = process.integer("pid", 1)
    creation = process.integer("creation_filetime_100ns", 1)
    expected_identity.update(
        clock_domain_identity_sha256=artifact_sha256(canonical_bytes(clock.value)),
        exporter_process_epoch_sha256=artifact_sha256(canonical_bytes(process.value)),
        exporter_source_sha256=sources.get("mineru_resident_telemetry_exporter.ps1"),
    )
    ready.expect("identity", expected_identity)
    if lane == "gpu_fast":
        backend = _Object(config.get("backend"), "nvml_dll_sha256 gpu_uuid")
        if re.fullmatch(r"GPU-[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", backend.text("gpu_uuid")) is None:
            raise ValueError("resident GPU UUID malformed")
        observed = _Object(ready.get("backend"), "device_identity_sha256 nvml_dll_sha256")
        observed.expect("nvml_dll_sha256", backend.sha("nvml_dll_sha256"))
        observed.expect("device_identity_sha256", artifact_sha256(("nvml.device-uuid.v1|" + backend.text("gpu_uuid")).encode()))
    else:
        _check_linux_ready(config, ready)
    return CheckedResidentReady(config_bytes, ready_bytes, manifest_bytes, session, lane, cadence, identity, pid, creation)


def _check_linux_configuration(config: _Object) -> _Object:
    backend = _Object(config.get("backend"), "docker_path docker_sha256 image_id linux_config api_port vllm_port api_namespace_pid model_name")
    backend.sha("image_id")
    backend.sha("docker_sha256")
    if not PureWindowsPath(backend.text("docker_path")).is_absolute():
        raise ValueError("resident Docker executable path must be absolute")
    backend.integer("api_port", 1024, 65535)
    backend.integer("vllm_port", 1024, 65535)
    backend.integer("api_namespace_pid", 1)
    backend.text("model_name")
    linux_config = _Object(backend.get("linux_config"), "boot_id members parent_device parent_inode lease_ms lifetime_ms")
    for name in ("lease_ms", "lifetime_ms"):
        linux_config.expect(name, config.get(name))
    if re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", linux_config.text("boot_id")) is None:
        raise ValueError("resident Linux boot identity malformed")
    members = _Object(linux_config.get("members"), "api inference proxy")
    member_pids: set[int] = set()
    member_cgroups: set[str] = set()
    for name in members.value:
        member = _Object(members.get(name), "cgroup pid start_ticks")
        member_pid = member.integer("pid", 1)
        member_cgroup = member.text("cgroup")
        if member_pid in member_pids or member_cgroup in member_cgroups:
            raise ValueError("resident Linux duplicate service member")
        member_pids.add(member_pid)
        member_cgroups.add(member_cgroup)
        member.integer("start_ticks", 1)
        if re.fullmatch(r"/docker/[0-9a-f]{64}", member.text("cgroup")) is None:
            raise ValueError("resident member cgroup is not the pinned Docker layout")
    linux_config.integer("parent_device")
    linux_config.integer("parent_inode", 1)
    return linux_config


def _check_linux_ready(config: _Object, ready: _Object) -> tuple[_Object, _Object]:
    linux_config = _check_linux_configuration(config)
    member_pids = {cast(dict[str, int], value)["pid"] for value in cast(dict[str, object], linux_config.get("members")).values()}
    backend = cast(dict[str, object], config.get("backend"))
    observed = _Object(ready.get("backend"), "container_name docker_creation_filetime_100ns docker_pid docker_sha256 linux_ready")
    observed.expect("container_name", "m6-resident-" + config.text("session"))
    observed.expect("docker_sha256", backend["docker_sha256"])
    exporter = _Object(ready.get("process"), "assembly_sha256 config_sha256 creation_filetime_100ns manifest_sha256 pid session")
    if observed.integer("docker_pid", 1) == exporter.integer("pid", 1):
        raise ValueError("resident Docker and exporter PID collide")
    observed.integer("docker_creation_filetime_100ns", exporter.integer("creation_filetime_100ns", 1))
    linux = _Object(observed.get("linux_ready"), "contract_version epoch_sha256 identity kind sampler_ready")
    linux.expect("contract_version", "mineru.linux-resident-supervisor.v1")
    linux.expect("kind", "ready")
    supervisor = _Object(linux.get("identity"), "boot_id namespaces pid sampler_source_sha256 source_sha256 start_ticks")
    sampler = _Object(linux.get("sampler_ready"), "contract_version cpu epoch_sha256 identity kind monotonic_ns")
    sampler.expect("contract_version", "mineru.linux-resident-host.v1")
    sampler.expect("kind", "ready")
    child = _Object(sampler.get("identity"), "boot_id members namespaces parent_device parent_inode parent_path pid source_sha256 start_ticks")
    for frame, identity, source in ((linux, supervisor, "linux_resident_host_supervisor.py"), (sampler, child, "linux_resident_host_sampler.py")):
        frame.expect("epoch_sha256", artifact_sha256(canonical_bytes(identity.value)))
        identity.expect("source_sha256", cast(dict[str, object], config.get("sources"))[source])
        identity.expect("boot_id", linux_config.get("boot_id"))
        identity.integer("pid", 1)
        identity.integer("start_ticks", 1)
    supervisor.expect("sampler_source_sha256", child.get("source_sha256"))
    if child.get("pid") == supervisor.get("pid"):
        raise ValueError("resident Linux sampler and supervisor PID collide")
    if child.get("pid") in member_pids or supervisor.get("pid") in member_pids:
        raise ValueError("resident Linux helper and service PID collide")
    child.integer("start_ticks", supervisor.integer("start_ticks", 1))
    for name in ("members", "parent_device", "parent_inode"):
        child.expect(name, linux_config.get(name))
    child.expect("parent_path", "/docker")
    child.expect("namespaces", supervisor.get("namespaces"))
    namespaces = _Object(supervisor.get("namespaces"), "cgroup pid")
    for name in namespaces.value:
        if re.fullmatch(name + r":\[[0-9]+\]", namespaces.text(name)) is None:
            raise ValueError("resident Linux namespace malformed")
    _cpu(sampler.get("cpu"))
    sampler.integer("monotonic_ns")
    return linux, sampler


def _cpu(value: object) -> tuple[int, int]:
    cpu = _Object(value, "user_ns_total system_ns_total")
    return cpu.integer("user_ns_total"), cpu.integer("system_ns_total")


@dataclass(frozen=True, slots=True)
class CheckedResidentClosure:
    session: str
    job_instance: str
    windows_supervisor_epoch: tuple[int, int]
    windows_job_cpu_ns: int
    windows_supervisor_pre_attestation_cpu_ns: int
    linux_sampler_exit_cpu_ns: int | None
    linux_supervisor_pre_attestation_cpu_ns: int | None
    source_sample_count: int
    source_last_sequence: int
    source_skipped_slots: int
    config_sha256: str
    ready_sha256: str


def check_resident_closure(
    *, ready: CheckedResidentReady, closed_bytes: bytes, job_bytes: bytes,
    linux_closed_bytes: bytes | None,
) -> CheckedResidentClosure:
    """Recompute normal-close CPU; absence/live identity remain separate gates."""
    closed = _decode(closed_bytes, "config_sha256 contract_version identity linux_closed_sha256 sampling session")
    closed.expect("contract_version", "mineru.windows-resident-closed.v2")
    closed.expect("identity", ready.identity.model_dump(mode="json"))
    job_receipt = _decode(job_bytes, "config_sha256 contract_version job session supervisor_process")
    job_receipt.expect("contract_version", "mineru.windows-resident-job-receipt.v1")
    for artifact in (closed, job_receipt):
        artifact.expect("session", ready.session)
        artifact.expect("config_sha256", artifact_sha256(ready.config_bytes))
    process = _Object(job_receipt.get("supervisor_process"), "assembly_sha256 config_sha256 creation_filetime_100ns manifest_sha256 pid session")
    observed = cast(dict[str, object], json.loads(ready.ready_bytes))
    child = _Object(observed["process"], tuple(process.value))
    for name in ("assembly_sha256", "config_sha256", "manifest_sha256", "session"):
        process.expect(name, child.get(name))
    supervisor_epoch = (process.integer("pid", 1), process.integer("creation_filetime_100ns", 1))
    if supervisor_epoch[0] == ready.pid:
        raise ValueError("resident Windows parent and child collide")
    if supervisor_epoch[1] > ready.creation_filetime_100ns:
        raise ValueError("resident Windows parent created after exporter")
    if ready.lane == "host_slow" and cast(dict[str, object], observed["backend"])["docker_pid"] == supervisor_epoch[0]:
        raise ValueError("resident Docker and Windows supervisor PID collide")
    job = _Object(job_receipt.get("job"), "child_creation_filetime_100ns child_exit_code child_pid contract_version forced_termination job_active_processes job_instance job_system_ns_total job_total_processes job_user_ns_total supervisor_creation_filetime_100ns supervisor_pid supervisor_pre_attestation_system_ns_total supervisor_pre_attestation_user_ns_total supervisor_source_sha256")
    job.expect("contract_version", "mineru.windows-job-accounting.v1")
    for name, value in (("child_pid", ready.pid), ("child_creation_filetime_100ns", ready.creation_filetime_100ns), ("supervisor_pid", supervisor_epoch[0]), ("supervisor_creation_filetime_100ns", supervisor_epoch[1]), ("child_exit_code", 0), ("forced_termination", False), ("job_active_processes", 0)):
        job.expect(name, value)
    config_value = cast(dict[str, object], json.loads(ready.config_bytes))
    prepared = cast(dict[str, object], config_value["prepared"])
    job.expect("supervisor_source_sha256", prepared["supervisor_source_sha256"])
    job_id = job.text("job_instance")
    if str(uuid.UUID(job_id)) != job_id or uuid.UUID(job_id).variant != uuid.RFC_4122:
        raise ValueError("resident Job instance malformed")
    job.integer("job_total_processes", 1 if ready.lane == "gpu_fast" else 2)
    sampling = _Object(closed.get("sampling"), "first_sampled_monotonic_ns first_observed_at_utc last_sampled_monotonic_ns last_observed_at_utc last_sequence sample_count skipped_slots closing_monotonic_ns closing_at_utc")
    count, sequence, skipped = (sampling.integer(name) for name in ("sample_count", "last_sequence", "skipped_slots"))
    if count + skipped != sequence:
        raise ValueError("resident source slot accounting differs")
    end_ns = sampling.integer("closing_monotonic_ns")
    end_wall = _utc(sampling.get("closing_at_utc"))
    if count == 0:
        for name in ("first_sampled_monotonic_ns", "last_sampled_monotonic_ns", "first_observed_at_utc", "last_observed_at_utc"):
            sampling.expect(name, None)
        if sequence:
            raise ValueError("unstarted resident source carries slots")
    else:
        first_ns, last_ns = (sampling.integer(name) for name in ("first_sampled_monotonic_ns", "last_sampled_monotonic_ns"))
        first_wall, last_wall = (_utc(sampling.get(name)) for name in ("first_observed_at_utc", "last_observed_at_utc"))
        _clock_delta(first_ns, last_ns, first_wall, last_wall)
        _clock_delta(last_ns, end_ns, last_wall, end_wall)
        if count == 1 and (first_ns != last_ns or first_wall != last_wall):
            raise ValueError("single resident source sample has two timestamps")
    linux_cpu = linux_parent_cpu = None
    if ready.lane == "gpu_fast":
        if linux_closed_bytes is not None:
            raise ValueError("GPU lane must not add Linux CPU")
        closed.expect("linux_closed_sha256", None)
    else:
        if linux_closed_bytes is None:
            raise ValueError("host lane requires Linux exit evidence")
        closed.expect("linux_closed_sha256", artifact_sha256(linux_closed_bytes))
        linux_cpu, linux_parent_cpu = _check_linux_closed(ready, linux_closed_bytes, count)
    return CheckedResidentClosure(
        ready.session, job_id, supervisor_epoch,
        job.integer("job_user_ns_total") + job.integer("job_system_ns_total"),
        job.integer("supervisor_pre_attestation_user_ns_total") + job.integer("supervisor_pre_attestation_system_ns_total"),
        linux_cpu, linux_parent_cpu, count, sequence, skipped,
        artifact_sha256(ready.config_bytes), artifact_sha256(ready.ready_bytes),
    )


def _check_linux_closed(ready: CheckedResidentReady, payload: bytes, sample_count: int) -> tuple[int, int]:
    receipt = _decode(payload, "backend_ready close closed config_sha256 contract_version session")
    receipt.expect("contract_version", "mineru.windows-linux-closed-receipt.v1")
    receipt.expect("session", ready.session)
    receipt.expect("config_sha256", artifact_sha256(ready.config_bytes))
    observed = cast(dict[str, object], json.loads(ready.ready_bytes))
    receipt.expect("backend_ready", observed["backend"])
    linux = _Object(cast(dict[str, object], observed["backend"])["linux_ready"], "contract_version epoch_sha256 identity kind sampler_ready")
    sampler = _Object(linux.get("sampler_ready"), "contract_version cpu epoch_sha256 identity kind monotonic_ns")
    child = cast(dict[str, object], sampler.get("identity"))
    close = _Object(receipt.get("close"), "contract_version cpu epoch_sha256 finished_monotonic_ns kind sequence started_monotonic_ns values")
    close.expect("contract_version", "mineru.linux-resident-host.v1")
    close.expect("kind", "close")
    close.expect("epoch_sha256", sampler.get("epoch_sha256"))
    close.expect("values", None)
    close.expect("sequence", sample_count + 1)
    close.integer("finished_monotonic_ns", close.integer("started_monotonic_ns", sampler.integer("monotonic_ns")))
    closed = _Object(receipt.get("closed"), "contract_version epoch_sha256 kind pre_attestation_monotonic_ns sampler_epoch_sha256 sampler_exit_cpu sampler_pid sampler_wait_status sequence supervisor_pre_attestation_cpu")
    closed.expect("contract_version", "mineru.linux-resident-supervisor.v1")
    closed.expect("kind", "closed")
    for name, value in (("epoch_sha256", linux.get("epoch_sha256")), ("sampler_epoch_sha256", sampler.get("epoch_sha256")), ("sampler_pid", child["pid"]), ("sampler_wait_status", 0), ("sequence", close.get("sequence"))):
        closed.expect(name, value)
    closed.integer("pre_attestation_monotonic_ns", close.integer("finished_monotonic_ns"))
    initial_cpu, close_cpu, exit_cpu = (_cpu(value) for value in (sampler.get("cpu"), close.get("cpu"), closed.get("sampler_exit_cpu")))
    if any(start > middle or middle > finish for start, middle, finish in zip(initial_cpu, close_cpu, exit_cpu, strict=True)):
        raise ValueError("resident Linux CPU counter rollback")
    return sum(exit_cpu), sum(_cpu(closed.get("supervisor_pre_attestation_cpu")))


@dataclass(frozen=True, slots=True)
class CheckedExternalWindowsObservation:
    """Replay of a separately executed pinned read-only control observation."""

    raw_bytes: bytes
    ready_sha256: str
    started_sha256: str
    phase: str
    windows_boot_utc: str
    observed_at_utc: datetime
    container_id: str | None


def check_external_windows_observation(
    *, payload: bytes, ready: CheckedResidentReady, windows_node_identity_sha256: str,
    previous_ready: CheckedExternalWindowsObservation | None = None,
) -> CheckedExternalWindowsObservation:
    """Validate actual control output, not caller-provided success booleans.

    The execution owner must retain the exact source-pinned command/result.
    This pure replay alone cannot prove that the command was actually executed.
    PowerShell's outer JSON need not be canonical; all embedded artifacts do.
    """
    if len(payload) > 131072 or re.fullmatch(r"sha256:[0-9a-f]{64}", windows_node_identity_sha256) is None:
        raise ValueError("external resident observation bound/node identity invalid")
    value = _Object(strict_json_loads(payload), "contract_version phase observed_at_utc windows_boot_utc config_sha256 ready_raw started_raw processes container container_absence closed_raw job_raw linux_closed_raw")
    value.expect("contract_version", "mineru.windows-resident-external-observation.v1")
    value.expect("config_sha256", artifact_sha256(ready.config_bytes))
    if value.text("ready_raw").encode() != ready.ready_bytes:
        raise ValueError("external resident READY bytes differ")
    boot = value.text("windows_boot_utc")
    _utc(boot)
    observed = _utc(value.get("observed_at_utc"))
    if artifact_sha256(canonical_bytes({"windows_node_identity_sha256": windows_node_identity_sha256, "boot_utc": boot})) != ready.identity.boot_identity_sha256:
        raise ValueError("external resident actual Windows boot differs")
    started_bytes = value.text("started_raw").encode()
    started = _decode(started_bytes, "contract_version session config_sha256 supervisor_process", 8192)
    started.expect("contract_version", "mineru.windows-resident-supervisor-started.v1")
    started.expect("session", ready.session)
    started.expect("config_sha256", artifact_sha256(ready.config_bytes))
    parent = _Object(started.get("supervisor_process"), "assembly_sha256 config_sha256 creation_filetime_100ns manifest_sha256 pid session")
    ready_value = cast(dict[str, object], json.loads(ready.ready_bytes))
    exporter = cast(dict[str, object], ready_value["process"])
    for name in ("assembly_sha256", "config_sha256", "manifest_sha256", "session"):
        parent.expect(name, exporter[name])
    parent_pid = parent.integer("pid", 1, 2**31 - 1)
    parent_birth = parent.integer("creation_filetime_100ns", 1, ready.creation_filetime_100ns)
    expected = {"supervisor": (parent_pid, parent_birth), "exporter": (ready.pid, ready.creation_filetime_100ns)}
    if ready.lane == "host_slow":
        backend = cast(dict[str, object], ready_value["backend"])
        expected["docker"] = (cast(int, backend["docker_pid"]), cast(int, backend["docker_creation_filetime_100ns"]))
    if len({pid for pid, _ in expected.values()}) != len(expected):
        raise ValueError("external resident process roles collide")
    phase = value.text("phase")
    if phase not in {"ready", "closed"} or (previous_ready is None) != (phase == "ready"):
        raise ValueError("external resident observation phase/previous evidence differs")
    processes = value.get("processes")
    if not isinstance(processes, list) or len(processes) != len(expected):
        raise ValueError("external resident process observation count differs")
    seen: set[str] = set()
    for item in processes:
        process = _Object(item, "role pid expected_creation_filetime_100ns actual_creation_filetime_100ns state")
        role = process.text("role")
        if role not in expected or role in seen:
            raise ValueError("external resident process observation role differs")
        seen.add(role)
        pid, birth = expected[role]
        process.expect("pid", pid)
        process.expect("expected_creation_filetime_100ns", birth)
        if phase == "ready":
            process.expect("state", "same-process")
            process.expect("actual_creation_filetime_100ns", birth)
        elif process.get("state") == "absent":
            process.expect("actual_creation_filetime_100ns", None)
        elif process.get("state") == "different-birth":
            if process.integer("actual_creation_filetime_100ns", 1) == birth:
                raise ValueError("external resident original process remains alive")
        else:
            raise ValueError("external resident original process remains alive")
    container_id: str | None = None
    if phase == "ready":
        for name in ("closed_raw", "job_raw", "linux_closed_raw", "container_absence"):
            value.expect(name, None)
        if ready.lane == "gpu_fast":
            value.expect("container", None)
        else:
            config = cast(dict[str, object], json.loads(ready.config_bytes))
            backend_config = cast(dict[str, object], config["backend"])
            linux = cast(dict[str, object], cast(dict[str, object], ready_value["backend"])["linux_ready"])
            container = _Object(value.get("container"), "id name image running pid started_at pid_mode cgroup_mode network read_only auto_remove privileged cap_add cap_drop security_opt mount_count entrypoint")
            container_id = container.text("id")
            if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
                raise ValueError("external resident helper ID malformed")
            _utc(container.get("started_at"))
            for name, required in (("name", "/m6-resident-" + ready.session), ("image", backend_config["image_id"]), ("running", True), ("pid", cast(dict[str, object], linux["identity"])["pid"]), ("pid_mode", "host"), ("cgroup_mode", "host"), ("network", "none"), ("read_only", True), ("auto_remove", True), ("privileged", False), ("mount_count", 0), ("cap_drop", ["ALL"]), ("entrypoint", ["/usr/bin/python3.12"])):
                container.expect(name, required)
            if container.get("cap_add") is not None and canonical_bytes(container.get("cap_add")) != b"[]":
                raise ValueError("external resident helper added capabilities")
            security = container.get("security_opt")
            if not isinstance(security, list) or len(security) != 2 or set(security) != {"no-new-privileges", "label=disable"}:
                raise ValueError("external resident helper security options differ")
    else:
        assert previous_ready is not None
        if previous_ready.phase != "ready" or previous_ready.ready_sha256 != artifact_sha256(ready.ready_bytes) or previous_ready.started_sha256 != artifact_sha256(started_bytes) or previous_ready.windows_boot_utc != boot or observed < previous_ready.observed_at_utc:
            raise ValueError("external resident closure previous observation differs")
        value.expect("container", None)
        for name in ("closed_raw", "job_raw"):
            value.text(name)
        if ready.lane == "gpu_fast":
            value.expect("container_absence", None)
            value.expect("linux_closed_raw", None)
        else:
            value.text("linux_closed_raw")
            container_id = previous_ready.container_id
            absence = _Object(value.get("container_absence"), "id exit_code stdout stderr")
            absence.expect("id", container_id)
            absence.expect("exit_code", 1)
            if absence.text("stdout").strip() != "[]" or absence.text("stderr").strip() != "error: no such object: " + str(container_id):
                raise ValueError("external resident exact helper absence differs")
        job_bytes = value.text("job_raw").encode()
        check_resident_closure(
            ready=ready, closed_bytes=value.text("closed_raw").encode(), job_bytes=job_bytes,
            linux_closed_bytes=value.text("linux_closed_raw").encode() if ready.lane == "host_slow" else None,
        )
        job = _decode(job_bytes, "config_sha256 contract_version job session supervisor_process")
        job.expect("supervisor_process", parent.value)
    return CheckedExternalWindowsObservation(payload, artifact_sha256(ready.ready_bytes), artifact_sha256(started_bytes), phase, boot, observed, container_id)


def check_resident_observer_mapping(
    *, ready: CheckedResidentReady, closed_bytes: bytes,
    frames: tuple[SynchronizedTelemetryFrameV2, ...], receipt: SynchronizedTelemetryReceiptV3,
) -> None:
    """Bind already-replayed observer frames to QPC without equating clocks.

Remote source continuity is recomputed independently of the Mac frame schedule.
Only UTC brackets connect the domains; no QPC number is subtracted from Mac
monotonic. The explicit owner artifact must retain both original clock objects.
    """
    closed = _decode(closed_bytes, "config_sha256 contract_version identity linux_closed_sha256 sampling session")
    if not isinstance(receipt, SynchronizedTelemetryReceiptV3):
        raise ValueError("resident cross-host mapping requires explicit observer v3")
    closed.expect("contract_version", "mineru.windows-resident-closed.v2")
    closed.expect("config_sha256", artifact_sha256(ready.config_bytes))
    closed.expect("identity", ready.identity.model_dump(mode="json"))
    closed.expect("session", ready.session)
    sampling = _Object(closed.get("sampling"), "first_sampled_monotonic_ns first_observed_at_utc last_sampled_monotonic_ns last_observed_at_utc last_sequence sample_count skipped_slots closing_monotonic_ns closing_at_utc")
    sampling.expect("skipped_slots", 0)
    lane_frames = tuple(frame for frame in frames if frame.lane == ready.lane)
    if not lane_frames:
        raise ValueError("resident observer source lane missing")
    ready_value = cast(dict[str, object], json.loads(ready.ready_bytes))
    backend = cast(dict[str, object], ready_value["backend"])
    api_epoch: str | None = None
    cgroup_epoch: str | None = None
    if ready.lane == "host_slow":
        linux = cast(dict[str, object], backend["linux_ready"])
        sampler = cast(dict[str, object], linux["sampler_ready"])
        child = cast(dict[str, object], sampler["identity"])
        members = cast(dict[str, object], child["members"])
        api_epoch = artifact_sha256(canonical_bytes({
            "boot_id": child["boot_id"], **cast(dict[str, object], members["api"]),
        }))
        # HostSampler.identity excludes helper process/source/namespaces. Its
        # cgroup projection is not the supervisor protocol's sampler epoch.
        cgroup_epoch = artifact_sha256(canonical_bytes({
            name: child[name] for name in ("boot_id", "members", "parent_path", "parent_device", "parent_inode")
        }))
        if receipt.process_profile.process_epoch_sha256 != api_epoch:
            raise ValueError("resident observer receipt API epoch differs")
    nominal_ns = ready.cadence_ms * 1_000_000
    elapsed = receipt.finished_monotonic_ns - receipt.started_monotonic_ns
    tolerance_ns = 50_000_000 + elapsed * 50 // 1_000_000
    previous_ns: int | None = None
    previous_wall: datetime | None = None
    first_wall: datetime | None = None
    last_wire_ns = 0
    for sequence, frame in enumerate(lane_frames, 1):
        provenance = frame.resident_exporter_provenance
        if provenance is None:
            raise ValueError("resident observer source provenance missing")
        for name in ("exporter_source_sha256", "host_assignment_identity_sha256", "boot_identity_sha256", "exporter_process_epoch_sha256"):
            if getattr(provenance, name) != getattr(ready.identity, name):
                raise ValueError(f"resident observer source identity drift: {name}")
        if frame.runtime_bundle_identity_sha256 != ready.identity.runtime_bundle_identity_sha256 or frame.process_profile_sha256 != ready.identity.process_profile_sha256:
            raise ValueError("resident observer source runtime/profile differs")
        if ready.lane == "gpu_fast":
            if frame.gpu.status != "supported" or frame.gpu.values is None or frame.gpu.values.device_identity_sha256 != backend["device_identity_sha256"]:
                raise ValueError("resident observer GPU device binding differs")
        else:
            if frame.api_process.status != "supported" or frame.api_process.values is None or frame.api_process.values.process_epoch_sha256 != api_epoch:
                raise ValueError("resident observer API epoch binding differs")
            if frame.host_cgroup.status != "supported" or frame.host_cgroup.values is None or frame.host_cgroup.values.parent_cgroup_epoch_sha256 != cgroup_epoch:
                raise ValueError("resident observer parent cgroup binding differs")
        if frame.clock.clock_domain_identity_sha256 != receipt.clock_domain_identity_sha256:
            raise ValueError("resident observer local clock differs")
        if provenance.wire_sequence != sequence or frame.quality.nominal_interval_ms != ready.cadence_ms:
            raise ValueError("resident observer source sequence/cadence differs")
        wire_ns, wire_wall = provenance.wire_sampled_monotonic_ns, provenance.wire_observed_at_utc
        if previous_ns is not None:
            assert previous_wall is not None
            if not nominal_ns * 9 // 10 <= wire_ns - previous_ns <= nominal_ns * 11 // 10:
                raise ValueError("resident observer source cadence drift")
            _clock_delta(previous_ns, wire_ns, previous_wall, wire_wall)
        else:
            first_wall = wire_wall
            sampling.expect("first_sampled_monotonic_ns", wire_ns)
            if _utc(sampling.get("first_observed_at_utc")) != wire_wall:
                raise ValueError("resident observer first source wall differs")
            if not -tolerance_ns <= _elapsed_ns(receipt.started_at_utc, wire_wall) <= nominal_ns + tolerance_ns:
                raise ValueError("resident observer source start boundary gap")
        local_start = frame.clock.started_monotonic_ns - receipt.started_monotonic_ns
        local_finish = frame.clock.finished_monotonic_ns - receipt.started_monotonic_ns
        wire_wall_offset = _elapsed_ns(receipt.started_at_utc, wire_wall)
        if not local_start - nominal_ns - tolerance_ns <= wire_wall_offset <= local_finish + tolerance_ns:
            raise ValueError("resident source UTC outside observer collection bracket")
        previous_ns, previous_wall, last_wire_ns = wire_ns, wire_wall, wire_ns
    assert previous_wall is not None and first_wall is not None
    if not -tolerance_ns <= _elapsed_ns(previous_wall, receipt.finished_at_utc) <= nominal_ns + tolerance_ns:
        raise ValueError("resident observer source end boundary gap")
    count = sampling.integer("sample_count", len(lane_frames))
    sampling.expect("last_sequence", count)
    last_source = sampling.integer("last_sampled_monotonic_ns", last_wire_ns)
    last_wall = _utc(sampling.get("last_observed_at_utc"))
    _clock_delta(sampling.integer("first_sampled_monotonic_ns"), last_source, first_wall, last_wall)
    _clock_delta(last_wire_ns, last_source, previous_wall, last_wall)
    if count == len(lane_frames):
        if last_source != last_wire_ns or last_wall != previous_wall:
            raise ValueError("resident observer last source frame differs")
    else:
        # Only a contiguous, cadence-consistent tail after the consumed prefix
        # is allowed. It cannot extend the observer denominator or fill a gap.
        tail_slots = count - len(lane_frames)
        if not tail_slots * nominal_ns * 9 // 10 <= last_source - last_wire_ns <= tail_slots * nominal_ns * 11 // 10:
            raise ValueError("resident source shutdown tail cadence differs")
    closing_wall = _utc(sampling.get("closing_at_utc"))
    _clock_delta(last_source, sampling.integer("closing_monotonic_ns"), last_wall, closing_wall)
    if _elapsed_ns(receipt.finished_at_utc, closing_wall) < -tolerance_ns:
        raise ValueError("resident source closed before observer finished")
