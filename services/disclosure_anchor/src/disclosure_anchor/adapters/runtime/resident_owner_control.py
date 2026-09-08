"""Bounded outer-owner commands, deliberately separate from sampling SSH HTTP.

No command runs per telemetry tick. Source pinning and separate stdout/stderr
preserve raw observations across terminal truncation and CLIXML diagnostics.
Killing the local control transport does not prove a remote Job has ended.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path, PureWindowsPath
import re
import selectors
import signal
import subprocess
import time
from typing import Mapping

from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig


@dataclass(frozen=True, slots=True)
class OwnerCommandResult:
    exit_code: int
    stdout: bytes
    stderr: bytes


class BoundedOwnerCommand:
    """One explicitly owned local command with a finite lifetime/output cap."""

    def __init__(self, command: list[str], *, timeout_seconds: float, maximum_bytes: int = 262144) -> None:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 7200:
            raise ValueError("owner command timeout invalid")
        if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 1048576:
            raise ValueError("owner command output cap invalid")
        self._deadline = time.monotonic() + timeout_seconds
        self._maximum = maximum_bytes
        self._stdout, self._stderr = bytearray(), bytearray()
        self._selector = selectors.DefaultSelector()
        self._closed = False
        self._result: OwnerCommandResult | None = None
        try:
            self._process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except BaseException:
            self._selector.close()
            raise
        assert self._process.stdout is not None and self._process.stderr is not None
        try:
            for stream, output in ((self._process.stdout, self._stdout), (self._process.stderr, self._stderr)):
                os.set_blocking(stream.fileno(), False)
                self._selector.register(stream, selectors.EVENT_READ, output)
        except BaseException:
            self.abort()
            raise

    @property
    def captured_output(self) -> tuple[bytes, bytes]:
        return bytes(self._stdout), bytes(self._stderr)

    def poll(self, *, timeout: float = 0) -> OwnerCommandResult | None:
        if not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ValueError("owner command poll bound invalid")
        if self._closed:
            return self._result
        until = min(self._deadline, time.monotonic() + timeout)
        try:
            while True:
                now = time.monotonic()
                if now >= self._deadline:
                    raise TimeoutError("owner command deadline; remote outcome requires reconciliation")
                for key, _ in self._selector.select(max(0, min(0.05, until - now))):
                    try:
                        chunk = os.read(key.fd, min(8192, self._maximum + 1 - len(self._stdout) - len(self._stderr)))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        self._selector.unregister(key.fileobj)
                    else:
                        key.data.extend(chunk)
                        if len(self._stdout) + len(self._stderr) > self._maximum:
                            raise ValueError("owner command output byte bound exceeded")
                code = self._process.poll()
                if code is not None and not self._selector.get_map():
                    self._result = OwnerCommandResult(code, bytes(self._stdout), bytes(self._stderr))
                    self._dispose()
                    return self._result
                if time.monotonic() >= until:
                    return None
        except BaseException:
            self.abort()
            raise

    def finish(self) -> OwnerCommandResult:
        while True:
            result = self.poll(timeout=min(1, max(0, self._deadline - time.monotonic())))
            if result is not None:
                return result
            if self._closed:
                raise RuntimeError("owner command was aborted without a normal result")

    def abort(self) -> None:
        if self._closed:
            return
        # This PG belongs to our local CLI transport, never the remote Job.
        # Include inherited-pipe descendants even if the group leader exited.
        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin may report EPERM for an exited, unreaped zombie-only
            # group. Reap only this owned leader, then require explicit ESRCH.
            # A live leader, live descendant or still-inaccessible group stays
            # unresolved; never generally suppress permission failures.
            if self._process.poll() is None:
                raise
            try:
                os.killpg(self._process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise
        if self._process.poll() is None:
            self._process.wait(timeout=2)
        self._dispose()

    def _dispose(self) -> None:
        self._selector.close()
        assert self._process.stdout is not None and self._process.stderr is not None
        self._process.stdout.close()
        self._process.stderr.close()
        self._closed = True


def _quote_powershell(value: str) -> str:
    if not value or any(char in value for char in "\0\r\n"):
        raise ValueError("owner PowerShell argument contains a forbidden character")
    return "'" + value.replace("'", "''") + "'"


def pinned_windows_script(
    *, script_path: str, expected_sha256: str, arguments: Mapping[str, str],
) -> str:
    if not PureWindowsPath(script_path).is_absolute() or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("owner Windows script path/hash invalid")
    if any(re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key) is None for key in arguments):
        raise ValueError("owner Windows parameter name invalid")
    path, digest = _quote_powershell(script_path), _quote_powershell(expected_sha256)
    args = " ".join("-" + name + " " + _quote_powershell(value) for name, value in arguments.items())
    return (
        "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
        f"$pin=[IO.File]::OpenRead({path});$hash=[Security.Cryptography.SHA256]::Create();"
        "try{$actual='sha256:'+([BitConverter]::ToString($hash.ComputeHash($pin))).Replace('-','').ToLowerInvariant();"
        f"if($actual -cne {digest}){{throw 'owner entrypoint source drift'}};& {path} {args}"
        "}finally{$hash.Dispose();$pin.Dispose()}"
    )


def owner_ssh_command(*, executable: Path, executable_sha256: str, ssh: ResidentSSHConfig, script: str) -> list[str]:
    if not executable.is_absolute() or "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest() != executable_sha256:
        raise ValueError("owner SSH executable path/hash differs")
    # All trust references are explicit, matching the separately reviewed HTTP
    # collector. Ignore config/agents/ProxyCommand/implicit key discovery.
    return [
        str(executable), "-F", "/dev/null", "-p", str(ssh.port), "-i", ssh.private_key_path,
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile=" + ssh.known_hosts_path, "-o", "ConnectTimeout=10",
        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "IdentityFile=none",
        "-o", "PreferredAuthentications=publickey",
        "-o", "IdentityAgent=none", "-o", "ClearAllForwardings=yes",
        ssh.username + "@" + ssh.address, "powershell", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
    ]
