"""Private release binding: explicit operator references for live release steps.

The file is owner-only JSON with paths and identities; it never carries key
bytes or credentials. ``qualify`` and ``install`` refuse to run without it and
never fall back to environment variables, previous reports or temporary paths.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re

from disclosure_anchor.adapters.runtime.mineru_release_package import ReleaseInputError
from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruCapacityConfig,
    decode_mineru_capacity_config,
)
from disclosure_anchor.application.contracts.closed_document import (
    SHA256_RE,
    load_closed_object,
    require_fields,
    require_int,
    require_str,
)


PRIVATE_BINDING_CONTRACT = "m6.release-private-binding.v1"
_FIELDS = frozenset({
    "contract_version", "mineru_bin", "api_url", "observability_url", "inference_upstream_url", "ssh_host",
    "ssh", "ssh_executable_path", "ssh_executable_sha256", "sftp_executable_path", "sftp_executable_sha256",
    "windows", "mac_exclusive_lock_path", "launchd_labels",
})
_SSH_FIELDS = frozenset({"address", "port", "username", "private_key_path", "known_hosts_path"})
_WINDOWS_FIELDS = frozenset({
    "hostname", "workspace_root", "expected_active_compose_sha256", "expected_previous_capacity_sha256",
    "previous_capacity_path", "job_lifetime_milliseconds", "job_cleanup_milliseconds",
})
_SSH_HOST_RE = re.compile(r"^(?!-)[A-Za-z0-9.-]+$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")


@dataclass(frozen=True, slots=True)
class WindowsInstallationTarget:
    hostname: str
    workspace_root: PureWindowsPath
    expected_active_compose_sha256: str | None
    expected_previous_capacity_sha256: str | None
    job_lifetime_milliseconds: int
    job_cleanup_milliseconds: int


@dataclass(frozen=True, slots=True)
class ReleasePrivateBinding:
    mineru_bin: Path
    api_url: str
    observability_url: str
    inference_upstream_url: str
    ssh_host: str
    ssh: ResidentSSHConfig
    ssh_executable: Path
    ssh_executable_sha256: str
    sftp_executable: Path
    sftp_executable_sha256: str
    windows: WindowsInstallationTarget
    mac_exclusive_lock_path: Path
    launchd_labels: tuple[str, ...]
    previous_capacity: MineruCapacityConfig | None


def _optional_sha(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or SHA256_RE.fullmatch(value) is None:
        raise ReleaseInputError(f"{label} must be null or a canonical sha256")
    return value


def _executable(path_value: object, digest_value: object, *, label: str) -> tuple[Path, str]:
    path = Path(require_str(path_value, label=f"{label} path"))
    digest = require_str(digest_value, label=f"{label} sha256")
    if not path.is_absolute() or not path.is_file():
        raise ReleaseInputError(f"{label} must be an existing absolute executable")
    if SHA256_RE.fullmatch(digest) is None:
        raise ReleaseInputError(f"{label} sha256 is not canonical")
    actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != digest:
        raise ReleaseInputError(f"{label} bytes differ from the pinned sha256")
    return path, digest


def load_release_private_binding(path: Path) -> ReleasePrivateBinding:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ReleaseInputError("private binding must be an existing absolute regular file")
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ReleaseInputError("private binding must be owner-only (0600) and owned by the caller")
    raw = path.read_bytes()
    try:
        value = load_closed_object(raw, label="release private binding", maximum_bytes=65536)
        require_fields(value, _FIELDS, label="release private binding")
        if value["contract_version"] != PRIVATE_BINDING_CONTRACT:
            raise ValueError("release private binding contract is unsupported")
        mineru_bin = Path(require_str(value["mineru_bin"], label="mineru_bin"))
        if not mineru_bin.is_absolute() or not mineru_bin.is_file():
            raise ValueError("mineru_bin must be an existing absolute file")
        for name in ("api_url", "observability_url", "inference_upstream_url"):
            url = require_str(value[name], label=name)
            if not url.startswith("http://"):
                raise ValueError(f"{name} must be an explicit http URL")
        ssh_host = require_str(value["ssh_host"], label="ssh_host", maximum=253)
        if _SSH_HOST_RE.fullmatch(ssh_host) is None:
            raise ValueError("ssh_host is invalid")
        ssh_value = value["ssh"]
        if type(ssh_value) is not dict:
            raise ValueError("ssh must be an object")
        require_fields(ssh_value, _SSH_FIELDS, label="private binding ssh")
        ssh = ResidentSSHConfig(**ssh_value)
        for name in ("private_key_path", "known_hosts_path"):
            candidate = Path(getattr(ssh, name))
            if not candidate.is_file():
                raise ValueError(f"ssh {name} does not exist")
        ssh_executable, ssh_sha = _executable(value["ssh_executable_path"], value["ssh_executable_sha256"], label="ssh executable")
        sftp_executable, sftp_sha = _executable(value["sftp_executable_path"], value["sftp_executable_sha256"], label="sftp executable")
        windows_value = value["windows"]
        if type(windows_value) is not dict:
            raise ValueError("windows must be an object")
        require_fields(windows_value, _WINDOWS_FIELDS, label="private binding windows")
        hostname = require_str(windows_value["hostname"], label="windows hostname", maximum=63)
        if _HOSTNAME_RE.fullmatch(hostname) is None:
            raise ValueError("windows hostname is invalid")
        workspace = PureWindowsPath(require_str(windows_value["workspace_root"], label="windows workspace_root"))
        if not workspace.is_absolute() or any(part in ("..", ".") for part in workspace.parts):
            raise ValueError("windows workspace_root must be an absolute Windows path")
        previous_capacity = None
        previous_path = windows_value["previous_capacity_path"]
        if previous_path is not None:
            candidate = Path(require_str(previous_path, label="previous_capacity_path"))
            if not candidate.is_absolute() or not candidate.is_file():
                raise ValueError("previous_capacity_path must be an existing absolute file")
            previous_capacity = decode_mineru_capacity_config(candidate.read_bytes())
            if previous_capacity.sha256 != windows_value["expected_previous_capacity_sha256"]:
                raise ValueError("previous capacity bytes differ from expected_previous_capacity_sha256")
        windows = WindowsInstallationTarget(
            hostname=hostname, workspace_root=workspace,
            expected_active_compose_sha256=_optional_sha(windows_value["expected_active_compose_sha256"], label="expected_active_compose_sha256"),
            expected_previous_capacity_sha256=_optional_sha(windows_value["expected_previous_capacity_sha256"], label="expected_previous_capacity_sha256"),
            job_lifetime_milliseconds=require_int(windows_value["job_lifetime_milliseconds"], label="job_lifetime_milliseconds", minimum=60_000, maximum=7_200_000),
            job_cleanup_milliseconds=require_int(windows_value["job_cleanup_milliseconds"], label="job_cleanup_milliseconds", minimum=1_000, maximum=10_000),
        )
        lock = Path(require_str(value["mac_exclusive_lock_path"], label="mac_exclusive_lock_path"))
        if not lock.is_absolute() or not lock.parent.is_dir():
            raise ValueError("mac_exclusive_lock_path must be absolute under an existing directory")
        labels_value = value["launchd_labels"]
        if type(labels_value) is not list or any(type(item) is not str or _LABEL_RE.fullmatch(item) is None for item in labels_value):
            raise ValueError("launchd_labels must be a list of label strings")
    except ValueError as exc:
        raise ReleaseInputError(str(exc)) from exc
    return ReleasePrivateBinding(
        mineru_bin=mineru_bin, api_url=value["api_url"], observability_url=value["observability_url"],
        inference_upstream_url=value["inference_upstream_url"], ssh_host=ssh_host, ssh=ssh,
        ssh_executable=ssh_executable, ssh_executable_sha256=ssh_sha, sftp_executable=sftp_executable,
        sftp_executable_sha256=sftp_sha, windows=windows, mac_exclusive_lock_path=lock,
        launchd_labels=tuple(labels_value), previous_capacity=previous_capacity,
    )


__all__ = [
    "PRIVATE_BINDING_CONTRACT",
    "ReleasePrivateBinding",
    "WindowsInstallationTarget",
    "load_release_private_binding",
]
