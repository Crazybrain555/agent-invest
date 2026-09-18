"""M6 line transport over one hash-pinned native OpenSSH stdio forward.

Python only owns a local socketpair. The pinned OpenSSH process owns the LAN
socket and forwards its stdio to the configured Windows loopback owner port.
No listener, SSH config, agent, ProxyCommand, firewall or owner-wire change is
introduced.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
from typing import IO, Any

from disclosure_anchor.adapters.runtime.m6_owner_protocol import M6_CONTROL_EXCHANGE_TIMEOUT_NS, M6LineOwnerTransport
from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig, _read_private_config


def validate_pinned_executable(path: Path, expected_sha256: str) -> None:
    if not path.is_absolute() or not path.is_file():
        raise ValueError("M6 SSH executable must be an existing absolute file")
    actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError("M6 SSH executable path/hash differs")


def _openssh_direct_argv(
    executable: Path, config: ResidentSSHConfig, remote_port: int, timeout: float,
) -> list[str]:
    if not 0 < timeout <= 30:
        raise ValueError("M6 OpenSSH startup timeout invalid")
    for path in (config.private_key_path, config.known_hosts_path):
        if any(ch.isspace() for ch in path):
            raise ValueError("M6 SSH file paths must not contain whitespace")
    connect_timeout = max(1, min(10, math.ceil(timeout)))
    return [
        str(executable), "-F", "/dev/null", "-p", str(config.port), "-i", config.private_key_path,
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile=" + config.known_hosts_path,
        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "IdentityFile=none", "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "IdentityAgent=none", "-o", "ClearAllForwardings=yes",
        "-o", "PreferredAuthentications=publickey", "-o", "LogLevel=ERROR", "-T",
        "-W", f"127.0.0.1:{remote_port}", config.username + "@" + config.address,
    ]


def _child_environment() -> dict[str, str]:
    """Minimal inherited surface: OpenSSH needs PATH and HOME only."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", str(Path.home()))}


def _child_report(returncode: int | None, stderr: IO[bytes] | None) -> str:
    """Exit status plus the first 2048 bytes of stderr; the temp file is always closed here."""
    head = b""
    if stderr is not None:
        try:
            stderr.seek(0)
            head = stderr.read(2048)
        finally:
            stderr.close()
    text = head.decode("utf-8", "replace").replace("\r", "").strip().replace("\n", " | ")
    report = "pinned OpenSSH exited " + ("unknown" if returncode is None else str(returncode))
    return report + (": " + text if text else "")


class _OwnedChannel:
    """Parent socketpair end; peer loss is reported with the child's exit status and stderr prefix."""

    def __init__(self, sock: socket.socket, session: "_OpenSSHDirectSession") -> None:
        self._sock, self._session = sock, session

    def settimeout(self, timeout: float | None) -> None:
        self._sock.settimeout(timeout)

    def gettimeout(self) -> float | None:
        return self._sock.gettimeout()

    def fileno(self) -> int:
        return self._sock.fileno()

    def close(self) -> None:
        self._sock.close()

    def sendall(self, data: bytes) -> None:
        try:
            self._sock.sendall(data)
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise EOFError(
                "M6 owner request write lost; remote outcome requires reconciliation; "
                + self._session.report_after_peer_loss()
            ) from exc

    def recv(self, size: int) -> bytes:
        part = self._sock.recv(size)
        if not part:
            raise EOFError(
                "M6 owner response EOF; remote outcome requires reconciliation; "
                + self._session.report_after_peer_loss()
            )
        return part


class _OpenSSHDirectSession:
    """One owner-thread OpenSSH child and its local socketpair endpoint."""

    def __init__(
        self, config: ResidentSSHConfig, *, executable: Path, executable_sha256: str, remote_port: int,
    ) -> None:
        if type(remote_port) is not int or not 1024 <= remote_port <= 65535:
            raise ValueError("M6 loopback owner port invalid")
        validate_pinned_executable(executable, executable_sha256)
        self._owner = threading.get_ident()
        self._config = config
        self._executable = executable
        self._executable_sha256 = executable_sha256
        self._remote_port = remote_port
        self._process: subprocess.Popen[bytes] | None = None
        self._channel: socket.socket | None = None
        self._stderr: IO[bytes] | None = None
        self._last_child_report: str | None = None
        self._closed = False

    def _assert_owner(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError("M6 OpenSSH session crossed owner thread")

    def _retire(self) -> None:
        channel, self._channel = self._channel, None
        if channel is not None:
            channel.close()
        process, self._process = self._process, None
        stderr, self._stderr = self._stderr, None
        if process is None:
            if stderr is not None:
                stderr.close()
            return
        try:
            if process.poll() is None:
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=2.0)
                        except subprocess.TimeoutExpired as exc:
                            raise RuntimeError("pinned OpenSSH process did not exit") from exc
            if process.poll() is None:
                raise RuntimeError("pinned OpenSSH process did not exit")
        finally:
            self._last_child_report = _child_report(process.returncode, stderr)

    def open_channel(self, timeout: float) -> _OwnedChannel:
        self._assert_owner()
        if self._closed:
            raise RuntimeError("M6 OpenSSH session is closed")
        self._retire()
        validate_pinned_executable(self._executable, self._executable_sha256)
        argv = _openssh_direct_argv(self._executable, self._config, self._remote_port, timeout)
        stderr: IO[bytes] = tempfile.TemporaryFile()
        parent, child = socket.socketpair()
        try:
            process = subprocess.Popen(
                argv, stdin=child.fileno(), stdout=child.fileno(), stderr=stderr.fileno(),
                close_fds=True, start_new_session=True, env=_child_environment(),
            )
        except BaseException:
            parent.close()
            child.close()
            stderr.close()
            raise
        child.close()
        parent.settimeout(timeout)
        self._process = process
        self._channel = parent
        self._stderr = stderr
        return _OwnedChannel(parent, self)

    def report_after_peer_loss(self) -> str:
        """Reap the child whose stdio just closed and describe it within the bounded report."""
        self._assert_owner()
        try:
            self._retire()
        except RuntimeError as exc:
            raise RuntimeError(f"{exc}; {self._last_child_report or 'pinned OpenSSH child state unknown'}") from exc
        return self._last_child_report or "pinned OpenSSH child state unknown"

    def close(self) -> None:
        self._assert_owner()
        if self._closed:
            return
        self._retire()
        self._closed = True


def m6_ssh_owner_transport(
    *, config: ResidentSSHConfig, token_path: str, remote_port: int,
    ssh_executable: Path, ssh_executable_sha256: str,
    continuous_ns: Callable[[], int], timeout_ns: int = M6_CONTROL_EXCHANGE_TIMEOUT_NS,
) -> M6LineOwnerTransport:
    """Use the run's pinned native OpenSSH bytes for the private owner channel."""
    if not Path(token_path).is_absolute():
        raise ValueError("M6 private token path must be absolute")
    token = _read_private_config(token_path, secret=True).removesuffix("\n")
    if re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise ValueError("M6 private token file is not canonical")
    if type(remote_port) is not int or not 1024 <= remote_port <= 65535:
        raise ValueError("M6 loopback owner port invalid")
    validate_pinned_executable(ssh_executable, ssh_executable_sha256)
    session: _OpenSSHDirectSession | None = None

    def open_channel(timeout: float) -> Any:
        nonlocal session
        if session is None:
            session = _OpenSSHDirectSession(
                config, executable=ssh_executable, executable_sha256=ssh_executable_sha256,
                remote_port=remote_port,
            )
        return session.open_channel(timeout)

    def close_session() -> None:
        nonlocal session
        if session is not None:
            session.close()
            session = None

    return M6LineOwnerTransport(
        token=token, open_channel=open_channel, close_session=close_session,
        continuous_ns=continuous_ns, timeout_ns=timeout_ns,
    )


__all__ = ["m6_ssh_owner_transport", "validate_pinned_executable"]
