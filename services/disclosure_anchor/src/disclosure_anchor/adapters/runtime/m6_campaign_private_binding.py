"""Private (0600) campaign binding: transport, workspace and environment references for one campaign.

Nothing here enters the tracked intent, a spec or a summary. Paths are
references; token/key bytes are never loaded by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re

from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig
from disclosure_anchor.application.contracts.closed_document import (
    load_closed_object, require_fields, require_int, require_sha256, require_str,
)

CAMPAIGN_PRIVATE_BINDING_CONTRACT = "m6.campaign-private-binding.v1"
_FIELDS = frozenset({
    "contract_version", "env_dir", "service_root", "python_executable", "runtime_root", "ssh", "windows",
    "mac_exclusive_lock_path",
})
_SSH_FIELDS = frozenset({
    "address", "port", "username", "private_key_path", "known_hosts_path", "executable_path", "executable_sha256",
    "sftp_executable_path", "sftp_executable_sha256",
})
_WINDOWS_FIELDS = frozenset({
    "hostname", "workspace_root", "owner_executable_path", "owner_executable_sha256", "owner_source_sha256",
    "launcher_path", "launcher_sha256", "node_sha256", "gpu_uuid", "nvml_dll_sha256", "port",
    "maximum_lease_ticks", "propagation_reserve_ticks", "max_artifacts", "max_artifact_bytes",
})
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
_GPU_UUID_RE = re.compile(r"^GPU-[0-9a-fA-F-]{36}$")
_ENV_FILES = ("worker.env", "cninfo.env")


class CampaignBindingError(ValueError):
    """The private binding is malformed or references something that does not exist."""


@dataclass(frozen=True, slots=True)
class CampaignWindowsTarget:
    hostname: str
    workspace_root: PureWindowsPath
    owner_executable_path: PureWindowsPath
    owner_executable_sha256: str
    owner_source_sha256: str
    launcher_path: PureWindowsPath
    launcher_sha256: str
    node_sha256: str
    gpu_uuid: str
    nvml_dll_sha256: str
    port: int
    maximum_lease_ticks: int
    propagation_reserve_ticks: int
    max_artifacts: int
    max_artifact_bytes: int


@dataclass(frozen=True, slots=True)
class CampaignPrivateBinding:
    env_dir: Path
    service_root: Path
    python_executable: Path
    runtime_root: Path
    ssh: ResidentSSHConfig
    ssh_executable: Path
    ssh_executable_sha256: str
    sftp_executable: Path
    sftp_executable_sha256: str
    windows: CampaignWindowsTarget
    mac_exclusive_lock_path: Path

    def env_files(self) -> tuple[Path, ...]:
        return tuple(self.env_dir / name for name in _ENV_FILES)


def _windows_path(value: object, *, label: str) -> PureWindowsPath:
    path = PureWindowsPath(require_str(value, label=label))
    if not path.is_absolute() or path.drive == "" or any(part in ("..", ".") for part in path.parts):
        raise ValueError(f"{label} must be an absolute drive-rooted Windows path")
    return path


def _executable(path_value: object, digest_value: object, *, label: str) -> tuple[Path, str]:
    path = Path(require_str(path_value, label=f"{label} path"))
    digest = require_sha256(digest_value, label=f"{label} sha256")
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"{label} must be an existing absolute executable")
    if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError(f"{label} bytes differ from the pinned sha256")
    return path, digest


def _private_file(path: Path, *, label: str) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be an existing absolute regular file")
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError(f"{label} must be owner-only (0600) and owned by the caller")


def load_campaign_private_binding(path: Path) -> CampaignPrivateBinding:
    try:
        _private_file(path, label="campaign private binding")
        value = load_closed_object(path.read_bytes(), label="campaign private binding", maximum_bytes=65536)
        require_fields(value, _FIELDS, label="campaign private binding")
        if value["contract_version"] != CAMPAIGN_PRIVATE_BINDING_CONTRACT:
            raise ValueError("campaign private binding contract is unsupported")
        env_dir = Path(require_str(value["env_dir"], label="env_dir"))
        if not env_dir.is_absolute() or not env_dir.is_dir():
            raise ValueError("env_dir must be an existing absolute directory")
        for name in _ENV_FILES:
            _private_file(env_dir / name, label=f"env_dir/{name}")
        service_root = Path(require_str(value["service_root"], label="service_root"))
        if not service_root.is_absolute() or not (service_root / "src" / "disclosure_anchor").is_dir():
            raise ValueError("service_root must be the absolute disclosure_anchor service checkout")
        python_executable = Path(require_str(value["python_executable"], label="python_executable"))
        if not python_executable.is_absolute() or not os.access(python_executable, os.X_OK):
            raise ValueError("python_executable must be an absolute executable")
        runtime_root = Path(require_str(value["runtime_root"], label="runtime_root"))
        if not runtime_root.is_absolute() or not runtime_root.is_dir():
            raise ValueError("runtime_root must be an existing absolute directory")
        ssh_value = value["ssh"]
        if type(ssh_value) is not dict:
            raise ValueError("ssh must be an object")
        require_fields(ssh_value, _SSH_FIELDS, label="campaign private binding ssh")
        ssh = ResidentSSHConfig(
            address=ssh_value["address"], port=ssh_value["port"], username=ssh_value["username"],
            private_key_path=ssh_value["private_key_path"], known_hosts_path=ssh_value["known_hosts_path"],
        )
        _private_file(Path(ssh.private_key_path), label="ssh private_key_path")
        if not Path(ssh.known_hosts_path).is_file():
            raise ValueError("ssh known_hosts_path does not exist")
        ssh_executable, ssh_sha = _executable(ssh_value["executable_path"], ssh_value["executable_sha256"], label="ssh executable")
        sftp_executable, sftp_sha = _executable(ssh_value["sftp_executable_path"], ssh_value["sftp_executable_sha256"], label="sftp executable")
        windows_value = value["windows"]
        if type(windows_value) is not dict:
            raise ValueError("windows must be an object")
        require_fields(windows_value, _WINDOWS_FIELDS, label="campaign private binding windows")
        hostname = require_str(windows_value["hostname"], label="windows hostname", maximum=63)
        if _HOSTNAME_RE.fullmatch(hostname) is None:
            raise ValueError("windows hostname is invalid")
        gpu_uuid = require_str(windows_value["gpu_uuid"], label="gpu_uuid", maximum=64)
        if _GPU_UUID_RE.fullmatch(gpu_uuid) is None:
            raise ValueError("gpu_uuid is not a canonical NVIDIA device UUID")
        windows = CampaignWindowsTarget(
            hostname=hostname,
            workspace_root=_windows_path(windows_value["workspace_root"], label="workspace_root"),
            owner_executable_path=_windows_path(windows_value["owner_executable_path"], label="owner_executable_path"),
            owner_executable_sha256=require_sha256(windows_value["owner_executable_sha256"], label="owner_executable_sha256"),
            owner_source_sha256=require_sha256(windows_value["owner_source_sha256"], label="owner_source_sha256"),
            launcher_path=_windows_path(windows_value["launcher_path"], label="launcher_path"),
            launcher_sha256=require_sha256(windows_value["launcher_sha256"], label="launcher_sha256"),
            node_sha256=require_sha256(windows_value["node_sha256"], label="node_sha256"),
            gpu_uuid=gpu_uuid,
            nvml_dll_sha256=require_sha256(windows_value["nvml_dll_sha256"], label="nvml_dll_sha256"),
            port=require_int(windows_value["port"], label="port", minimum=1024, maximum=65535),
            maximum_lease_ticks=require_int(windows_value["maximum_lease_ticks"], label="maximum_lease_ticks"),
            propagation_reserve_ticks=require_int(windows_value["propagation_reserve_ticks"], label="propagation_reserve_ticks"),
            max_artifacts=require_int(windows_value["max_artifacts"], label="max_artifacts", minimum=128, maximum=16384),
            max_artifact_bytes=require_int(windows_value["max_artifact_bytes"], label="max_artifact_bytes", minimum=8_388_608, maximum=268_435_456),
        )
        lock = Path(require_str(value["mac_exclusive_lock_path"], label="mac_exclusive_lock_path"))
        if not lock.is_absolute() or not lock.parent.is_dir():
            raise ValueError("mac_exclusive_lock_path must be absolute under an existing directory")
    except ValueError as exc:
        raise CampaignBindingError(str(exc)) from exc
    return CampaignPrivateBinding(
        env_dir=env_dir, service_root=service_root, python_executable=python_executable, runtime_root=runtime_root,
        ssh=ssh, ssh_executable=ssh_executable, ssh_executable_sha256=ssh_sha, sftp_executable=sftp_executable,
        sftp_executable_sha256=sftp_sha, windows=windows, mac_exclusive_lock_path=lock,
    )


def assert_known_hosts_pins_address(ssh: ResidentSSHConfig) -> None:
    """The pinned known_hosts must name the transport address itself (no alias mapping)."""
    entries = []
    for line in Path(ssh.known_hosts_path).read_text(encoding="utf-8").splitlines():
        fields = line.strip().split()
        if len(fields) >= 3 and not fields[0].startswith("#"):
            entries.append(fields[0])
    accepted = {ssh.address, f"[{ssh.address}]:{ssh.port}"}
    if not any(any(name in accepted for name in entry.split(",")) for entry in entries):
        raise CampaignBindingError("known_hosts does not pin the SSH transport address; use the address-keyed pinned file")


__all__ = [
    "CAMPAIGN_PRIVATE_BINDING_CONTRACT", "CampaignBindingError", "CampaignPrivateBinding", "CampaignWindowsTarget",
    "assert_known_hosts_pins_address", "load_campaign_private_binding",
]
