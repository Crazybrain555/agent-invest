"""Mac side of one official installation: exclusivity, staging, owned remote wrapper.

The Mac holds the deployment exclusivity lock, proves the resident worker and
GC are not loaded and the API is idle, stages the verified package into a new
Windows directory, runs the Windows installation owner inside one bounded SSH
session it owns, fetches the operation records and judges only from them. A
local SSH exit never proves the remote outcome; an unfetchable or unknown
record leaves the result ``unknown`` with write permission closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
from typing import Any

from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
from disclosure_anchor.adapters.runtime.mineru_release_binding import fetch_json
from disclosure_anchor.adapters.runtime.mineru_release_package import (
    MANIFEST_NAME,
    ReleaseIdentityError,
    ReleaseInputError,
    VerifyReport,
    write_new_json,
)
from disclosure_anchor.adapters.runtime.mineru_release_private_binding import ReleasePrivateBinding
from disclosure_anchor.application.contracts.mineru_capacity_config import MineruCapacityConfig
from disclosure_anchor.application.contracts.mineru_capacity_health import parse_mineru_capacity_wire_health
from disclosure_anchor.adapters.runtime.resident_owner_control import (
    BoundedOwnerCommand,
    OwnerCommandResult,
    owner_ssh_command,
    pinned_windows_script,
)
from disclosure_anchor.application.contracts.closed_document import canonical_bytes, sha256_of
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


INSTALLATION_BINDING_CONTRACT = "m6.installation-binding.v1"
INSTALL_RESULT_CONTRACT = "m6.release-install-result.v1"
_STAGE_TIMEOUT_SECONDS = 600
_FETCH_TIMEOUT_SECONDS = 300
_LAUNCHCTL_TIMEOUT_SECONDS = 30


class InstallationOutcomeUnknown(RuntimeError):
    """The remote outcome could not be determined; write permission stays closed."""


_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9 _./:\\-]+$")


def _sftp_path(path: PureWindowsPath) -> str:
    text = str(path)
    if _SAFE_PATH_RE.fullmatch(text) is None:
        raise ReleaseInputError(f"remote path contains characters outside the transfer allowlist: {text!r}")
    return "/" + text.replace("\\", "/")


def _quoted(value: str) -> str:
    if _SAFE_PATH_RE.fullmatch(value) is None:
        raise ReleaseInputError(f"transfer path contains characters outside the allowlist: {value!r}")
    return '"' + value + '"'


def assert_known_hosts_pins_address(binding: ReleasePrivateBinding) -> None:
    """The pinned known_hosts must name the transport address itself (no alias mapping)."""

    ssh = binding.ssh
    entries = []
    for line in Path(ssh.known_hosts_path).read_text(encoding="utf-8").splitlines():
        fields = line.strip().split()
        if len(fields) >= 3 and not fields[0].startswith("#"):
            entries.append(fields[0])
    accepted = {ssh.address, f"[{ssh.address}]:{ssh.port}"}
    if not any(any(name in accepted for name in entry.split(",")) for entry in entries):
        raise ReleaseInputError("known_hosts does not pin the SSH transport address; use the address-keyed pinned file")


def _sftp_argv(binding: ReleasePrivateBinding, batch: Path) -> list[str]:
    ssh = binding.ssh
    return [
        str(binding.sftp_executable), "-b", str(batch), "-F", "/dev/null", "-P", str(ssh.port), "-i", ssh.private_key_path,
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile=" + ssh.known_hosts_path, "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10", "-o", "IdentityAgent=none", "-o", "ClearAllForwardings=yes",
        "-o", "PreferredAuthentications=publickey", f"{ssh.username}@{ssh.address}",
    ]


def _run_owned(argv: list[str], *, timeout_seconds: float, output: Path, label: str, maximum_bytes: int = 262144) -> OwnerCommandResult:
    write_new_json(output / f"{label}-command.json", {"argv": argv, "timeout_seconds": timeout_seconds,
                                                      "started_utc": datetime.now(UTC).isoformat(timespec="seconds")})
    command = BoundedOwnerCommand(argv, timeout_seconds=timeout_seconds, maximum_bytes=maximum_bytes)
    try:
        result = command.finish()
    except (TimeoutError, ValueError) as exc:
        stdout, stderr = command.captured_output
        write_new_exact(output / f"{label}-stdout.raw", stdout)
        write_new_exact(output / f"{label}-stderr.raw", stderr)
        write_new_json(output / f"{label}-execution.json", {"exit_code": None, "failure": str(exc),
                                                            "finished_utc": datetime.now(UTC).isoformat(timespec="seconds")})
        raise InstallationOutcomeUnknown(f"{label}: {exc}") from exc
    write_new_exact(output / f"{label}-stdout.raw", result.stdout)
    write_new_exact(output / f"{label}-stderr.raw", result.stderr)
    write_new_json(output / f"{label}-execution.json", {"exit_code": result.exit_code,
                                                        "finished_utc": datetime.now(UTC).isoformat(timespec="seconds")})
    return result


@dataclass(frozen=True, slots=True)
class ExclusivityLock:
    path: Path
    descriptor: int

    def release(self) -> None:
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)


def acquire_exclusivity(lock_path: Path, *, holder: dict[str, Any]) -> ExclusivityLock:
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise ReleaseIdentityError(f"deployment exclusivity lock is held: {lock_path}") from exc
    os.ftruncate(descriptor, 0)
    os.write(descriptor, json.dumps(holder, sort_keys=True).encode("utf-8") + b"\n")
    os.fsync(descriptor)
    return ExclusivityLock(path=lock_path, descriptor=descriptor)


# Known shared-runtime producers. They must always be checked; a binding cannot
# omit them by supplying a shorter list.
MANDATORY_LAUNCHD_LABELS = ("com.agentinvest.disclosure-worker", "com.agentinvest.disclosure-gc")
# launchctl print: 0 = loaded; 113 (ENOENT-style "Could not find service") = absent.
# Every other outcome is undeterminable and never counts as absence.
_LAUNCHCTL_MISSING_SERVICE = 113


def assert_launchd_jobs_unloaded(labels: tuple[str, ...]) -> dict[str, str]:
    """A loaded worker or GC job means shared-runtime writers may exist; refuse.

    Only the recognized missing-service outcome counts as unloaded; permission,
    transport or usage failures keep the state unknown and block installation.
    """

    states: dict[str, str] = {}
    domain = f"gui/{os.getuid()}"
    for label in tuple(dict.fromkeys(MANDATORY_LAUNCHD_LABELS + tuple(labels))):
        try:
            completed = subprocess.run(
                ["launchctl", "print", f"{domain}/{label}"], stdin=subprocess.DEVNULL, capture_output=True,
                timeout=_LAUNCHCTL_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseIdentityError(f"launchctl state of {label} is undeterminable: {exc}") from exc
        if completed.returncode == 0:
            states[label] = "loaded"
        elif completed.returncode == _LAUNCHCTL_MISSING_SERVICE and b"Could not find service" in completed.stderr:
            states[label] = "not_loaded"
        else:
            detail = completed.stderr.decode("utf-8", "replace").strip()[:200]
            raise ReleaseIdentityError(f"launchctl state of {label} is undeterminable (exit {completed.returncode}): {detail}")
    loaded = [label for label, state in states.items() if state == "loaded"]
    if loaded:
        raise ReleaseIdentityError("launchd jobs must be unloaded before installation: " + ", ".join(loaded))
    return states


def assert_idle_capacity_health(
    payload: bytes, *, expected_capacity: MineruCapacityConfig, task_retention_seconds: int, cleanup_interval_seconds: int,
) -> dict[str, Any]:
    """Closed idle proof: the full explicit-capacity wire health with zero durable responsibility.

    Legacy queued/processing gauges alone are not proof; ingress, scheduled,
    finalizing, stage owners and HTTP counters must all be zero and admission open.
    """

    health = parse_mineru_capacity_wire_health(
        payload, expected_capacity=expected_capacity, expected_task_retention_seconds=task_retention_seconds,
        expected_cleanup_interval_seconds=cleanup_interval_seconds,
    )
    admission = health["task_admission"]
    observation = health["capacity_observation"]
    busy = {
        **{f"task_admission.{k}": admission[k] for k in (
            "ingress_tasks", "accepted_pending_tasks", "accepted_processing_tasks", "accepted_finalizing_tasks",
            "durable_nonterminal_tasks", "scheduled_tasks", "queue_depth", "active_processors",
        )},
        **{f"stage_counters.{k}": v for k, v in observation["stage_counters"].items()},
        **{f"http_counters.{k}": v for k, v in observation["http_counters"].items()},
    }
    nonzero = sorted(name for name, value in busy.items() if value != 0)
    if nonzero or admission["admission_open"] is not True or observation["owner_control"]["soft_drain_requested"]:
        raise ReleaseIdentityError("API is not idle: " + (", ".join(nonzero) or "admission closed or draining"))
    return dict(health)


def read_idle_health(
    api_url: str, *, expected_capacity: MineruCapacityConfig | None, task_retention_seconds: int, cleanup_interval_seconds: int,
) -> tuple[dict[str, Any], bytes, str]:
    """Return the health sample and which idle proof applied.

    With a known expected capacity the closed explicit-capacity proof is
    required. Without one (a first explicit installation over a legacy runtime)
    only the legacy gauges exist; that weaker proof is named, never hidden.
    """

    health, raw = fetch_json(api_url.rstrip("/") + "/health")
    if expected_capacity is not None:
        assert_idle_capacity_health(
            raw, expected_capacity=expected_capacity, task_retention_seconds=task_retention_seconds,
            cleanup_interval_seconds=cleanup_interval_seconds,
        )
        return health, raw, "explicit_capacity_closed"
    if health.get("status") != "healthy" or health.get("queued_tasks") != 0 or health.get("processing_tasks") != 0:
        raise ReleaseIdentityError("API is not healthy and idle")
    if "capacity_observation" in health:
        raise ReleaseIdentityError("API already reports explicit capacity; the previous capacity identity must be supplied")
    return health, raw, "legacy_gauges_only"


def installation_binding_document(binding: ReleasePrivateBinding, *, api_device_profile: str, exclusivity_sha256: str) -> dict[str, Any]:
    windows = binding.windows
    return {
        "contract_version": INSTALLATION_BINDING_CONTRACT,
        "expected_hostname": windows.hostname,
        "api_device_profile": api_device_profile,
        "expected_active_compose_sha256": windows.expected_active_compose_sha256,
        "expected_previous_capacity_sha256": windows.expected_previous_capacity_sha256,
        "job_lifetime_milliseconds": windows.job_lifetime_milliseconds,
        "job_cleanup_milliseconds": windows.job_cleanup_milliseconds,
        "mac_exclusivity_receipt_sha256": exclusivity_sha256,
    }


def stage_batch(report: VerifyReport, package: Path, remote_root: PureWindowsPath, binding_local: Path, binding_remote_dir: PureWindowsPath) -> str:
    directories = sorted({str(PureWindowsPath(entry.path).parent) for entry in report.manifest.files if "/" in entry.path})
    lines = [f"mkdir {_quoted(_sftp_path(remote_root))}"]
    for directory in directories:
        lines.append(f"mkdir {_quoted(_sftp_path(remote_root / directory))}")
    for entry in report.manifest.files:
        lines.append(f"put {_quoted(str(package / entry.path))} {_quoted(_sftp_path(remote_root / entry.path))}")
    lines.append(f"put {_quoted(str(package / MANIFEST_NAME))} {_quoted(_sftp_path(remote_root / MANIFEST_NAME))}")
    lines.append(f"mkdir {_quoted(_sftp_path(binding_remote_dir))}")
    lines.append(f"put {_quoted(str(binding_local))} {_quoted(_sftp_path(binding_remote_dir / 'installation-binding.json'))}")
    return "\n".join(lines) + "\n"


def install_release(*, report: VerifyReport, package: Path, binding: ReleasePrivateBinding, output: Path) -> dict[str, Any]:
    if not output.is_absolute() or output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ReleaseInputError("install output must be a new absolute directory under an existing parent")
    if not report.passed:
        raise ReleaseIdentityError("release package did not verify; refusing to install")
    manifest = report.manifest
    output.mkdir(mode=0o700)
    started = datetime.now(UTC).isoformat(timespec="seconds")
    lock = acquire_exclusivity(binding.mac_exclusive_lock_path, holder={
        "pid": os.getpid(), "started_utc": started, "release_manifest_sha256": manifest.sha256, "purpose": "installation",
    })
    status = "failed"
    failure: str | None = None
    daemon_side = "unknown"
    remote: dict[str, Any] = {}
    try:
        assert_known_hosts_pins_address(binding)
        launchd = assert_launchd_jobs_unloaded(binding.launchd_labels)
        previous_capacity = None
        if binding.windows.expected_previous_capacity_sha256 is not None:
            if binding.windows.expected_previous_capacity_sha256 == report.inputs.capacity.sha256:
                previous_capacity = report.inputs.capacity
            else:
                previous_capacity = binding.previous_capacity
                if previous_capacity is None or previous_capacity.sha256 != binding.windows.expected_previous_capacity_sha256:
                    raise ReleaseInputError("previous capacity bytes are required to prove idle under the previous explicit capacity")
        deployment = report.inputs.deployment_profile
        health_before, health_raw, idle_proof = read_idle_health(
            binding.api_url, expected_capacity=previous_capacity,
            task_retention_seconds=deployment.api_task_retention_seconds,
            cleanup_interval_seconds=deployment.api_task_cleanup_interval_seconds,
        )
        write_new_exact(output / "health-before.json", health_raw)
        exclusivity = {
            "contract_version": "m6.installation-mac-exclusivity.v1",
            "lock_path": str(binding.mac_exclusive_lock_path), "pid": os.getpid(), "started_utc": started,
            "launchd": launchd, "api_health_before_sha256": sha256_of(health_raw), "idle_proof": idle_proof,
            "release_manifest_sha256": manifest.sha256,
        }
        exclusivity_raw = write_new_json(output / "mac-exclusivity.json", exclusivity)
        short = manifest.sha256[7:19]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        remote_root = binding.windows.workspace_root / f"mineru-release-{short}"
        remote_binding_dir = binding.windows.workspace_root / f"mineru-install-{short}-{stamp}-binding"
        remote_operation = binding.windows.workspace_root / f"mineru-install-{short}-{stamp}"
        remote = {"release_root": str(remote_root), "binding_dir": str(remote_binding_dir), "operation_dir": str(remote_operation)}
        install_binding = installation_binding_document(
            binding, api_device_profile=report.inputs.deployment_profile.api_device_profile,
            exclusivity_sha256=sha256_of(exclusivity_raw),
        )
        binding_local = output / "installation-binding.json"
        write_new_exact(binding_local, canonical_bytes(install_binding) + b"\n")
        batch = output / "stage.batch"
        write_new_exact(batch, stage_batch(report, package, remote_root, binding_local, remote_binding_dir).encode("utf-8"))
        staged = _run_owned(_sftp_argv(binding, batch), timeout_seconds=_STAGE_TIMEOUT_SECONDS, output=output, label="stage")
        if staged.exit_code != 0:
            raise ReleaseIdentityError(f"staging failed with sftp exit {staged.exit_code}; the remote directory may be partial and must not be reused")
        script = pinned_windows_script(
            script_path=str(remote_root / "windows" / "run_mineru_installation.ps1"),
            expected_sha256=manifest.installation["wrapper_sha256"],
            arguments={
                "ReleaseRoot": str(remote_root), "ExpectedManifestSha256": manifest.sha256,
                "OperationDirectory": str(remote_operation),
                "InstallationBinding": str(remote_binding_dir / "installation-binding.json"),
            },
        )
        argv = owner_ssh_command(
            executable=binding.ssh_executable, executable_sha256=binding.ssh_executable_sha256, ssh=binding.ssh, script=script,
        )
        timeout = min(7200.0, binding.windows.job_lifetime_milliseconds / 1000 + 300.0)
        try:
            wrapper = _run_owned(argv, timeout_seconds=timeout, output=output, label="wrapper", maximum_bytes=1048576)
            wrapper_exit: int | None = wrapper.exit_code
        except InstallationOutcomeUnknown as exc:
            wrapper_exit = None
            failure = str(exc)
        fetch_batch = output / "fetch.batch"
        write_new_exact(fetch_batch, f'get -r "{_sftp_path(remote_operation)}" "{output / "windows-operation"}"\n'.encode("utf-8"))
        try:
            fetched = _run_owned(_sftp_argv(binding, fetch_batch), timeout_seconds=_FETCH_TIMEOUT_SECONDS, output=output, label="fetch", maximum_bytes=1048576)
            fetched_ok = fetched.exit_code == 0
        except InstallationOutcomeUnknown as exc:
            fetched_ok = False
            failure = failure or str(exc)
        result_path = output / "windows-operation" / "operation-result.json"
        operation: dict[str, Any] | None = None
        if fetched_ok and result_path.is_file():
            value = strict_json_loads(result_path.read_bytes())
            if type(value) is dict:
                operation = value
        if operation is None:
            status = "unknown"
            failure = failure or "Windows operation result was not fetched; remote outcome unknown"
        else:
            daemon_side = str(operation.get("daemon_side_outcome", "unknown"))
            remote_status = operation.get("status")
            if remote_status == "pass" and wrapper_exit == 0 and daemon_side == "verified":
                health_after, health_after_raw, _ = read_idle_health(
                    binding.api_url, expected_capacity=report.inputs.capacity,
                    task_retention_seconds=deployment.api_task_retention_seconds,
                    cleanup_interval_seconds=deployment.api_task_cleanup_interval_seconds,
                )
                write_new_exact(output / "health-after.json", health_after_raw)
                status = "pass"
            elif remote_status == "unknown" or daemon_side == "unknown":
                status = "unknown"
                failure = failure or str(operation.get("first_error") or "remote outcome unknown")
            else:
                status = "failed"
                failure = failure or str(operation.get("first_error") or f"remote status {remote_status}")
    except (ReleaseIdentityError, ReleaseInputError, InstallationOutcomeUnknown, OSError, ValueError) as exc:
        failure = failure or str(exc)
        if status == "failed" and isinstance(exc, InstallationOutcomeUnknown):
            status = "unknown"
    finally:
        lock.release()
    summary = {
        "contract_version": INSTALL_RESULT_CONTRACT,
        "status": status,
        "first_error": failure,
        "daemon_side_outcome": daemon_side,
        # Installation never authorizes producers: the worker stays stopped until
        # this release is qualified and bound and the stale binding is replaced.
        "write_permission": "closed",
        "installation_verified": status == "pass",
        "next_required": ["qualify", "bind"] if status == "pass" else ["read_only_recovery_judgement"],
        "release_manifest_sha256": manifest.sha256,
        "capacity_config_sha256": report.inputs.capacity.sha256,
        "compose_sha256": manifest.projection["compose_sha256"],
        "remote": remote,
        "started_utc": started,
        "finished_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    write_new_json(output / "install-result.json", summary)
    return summary


__all__ = [
    "INSTALLATION_BINDING_CONTRACT",
    "INSTALL_RESULT_CONTRACT",
    "InstallationOutcomeUnknown",
    "acquire_exclusivity",
    "assert_launchd_jobs_unloaded",
    "install_release",
    "installation_binding_document",
    "read_idle_health",
    "stage_batch",
]
