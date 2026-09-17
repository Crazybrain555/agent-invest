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
from typing import Any, Literal, Mapping

from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig
from disclosure_anchor.application.services.resident_measurement_policy import FINITE_COMMAND_MAX_SECONDS


@dataclass(frozen=True, slots=True)
class OwnerCommandResult:
    exit_code: int
    stdout: bytes
    stderr: bytes


class _PipeCapture:
    """Bounded retention of one pipe: an exact head, then a sliding tail with the dropped count."""

    __slots__ = ("head", "tail", "total", "dropped", "head_limit", "tail_limit")

    def __init__(self, head_limit: int, tail_limit: int) -> None:
        self.head, self.tail = bytearray(), bytearray()
        self.total = self.dropped = 0
        self.head_limit, self.tail_limit = head_limit, tail_limit

    def extend(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.head_limit - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            excess = len(self.tail) - self.tail_limit
            if excess > 0:
                self.dropped += excess
                del self.tail[:excess]

    def __len__(self) -> int:
        return len(self.head) + len(self.tail)

    def __bytes__(self) -> bytes:
        return bytes(self.head) + bytes(self.tail)


OwnerCommandRetention = Literal["strict", "head_tail"]


class BoundedOwnerCommand:
    """One explicitly owned local command with a finite lifetime/output cap.

    ``strict`` retention (default) fails the command when the combined output
    exceeds ``maximum_bytes``. ``head_tail`` retention is for long-lived
    supervised children whose canonical evidence lives in their own files: it
    keeps the first and last ``maximum_bytes // 2`` of each pipe, counts what
    was dropped in between and never fails on volume.

    ``lifetime_ceiling_seconds`` is the absolute ceiling this one command may
    request; a caller raises it only for an explicitly longer finite session.
    """

    def __init__(
        self, command: list[str], *, timeout_seconds: float, maximum_bytes: int = 262144,
        environment: Mapping[str, str] | None = None, cwd: str | None = None,
        retention: OwnerCommandRetention = "strict", lifetime_ceiling_seconds: int = 7200,
    ) -> None:
        if type(lifetime_ceiling_seconds) is not int or not 1 <= lifetime_ceiling_seconds <= FINITE_COMMAND_MAX_SECONDS:
            raise ValueError("owner command lifetime ceiling invalid")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= lifetime_ceiling_seconds):
            raise ValueError("owner command timeout invalid")
        if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 1048576:
            raise ValueError("owner command output cap invalid")
        if retention not in ("strict", "head_tail") or (retention == "head_tail" and maximum_bytes < 2):
            raise ValueError("owner command retention mode invalid")
        # An explicit environment replaces inheritance for this child only; the
        # caller's own process environment is never mutated to launch it.
        if environment is not None and (
            type(environment) is not dict or any(type(k) is not str or type(v) is not str for k, v in environment.items())
        ):
            raise ValueError("owner command environment must map strings to strings")
        if cwd is not None and (type(cwd) is not str or not Path(cwd).is_absolute() or not Path(cwd).is_dir()):
            raise ValueError("owner command working directory must be an existing absolute directory")
        self._deadline = time.monotonic() + timeout_seconds
        self._maximum = maximum_bytes
        self._retention: OwnerCommandRetention = retention
        if retention == "strict":
            self._stdout, self._stderr = _PipeCapture(maximum_bytes + 1, 0), _PipeCapture(maximum_bytes + 1, 0)
        else:
            self._stdout, self._stderr = _PipeCapture(maximum_bytes // 2, maximum_bytes // 2), _PipeCapture(maximum_bytes // 2, maximum_bytes // 2)
        self._selector = selectors.DefaultSelector()
        self._closed = False
        self._result: OwnerCommandResult | None = None
        try:
            self._process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, env=None if environment is None else dict(environment), cwd=cwd,
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

    def retention_report(self) -> dict[str, Any]:
        """Exact totals and dropped counts; in strict mode nothing is ever dropped."""
        return {
            "mode": self._retention,
            "stdout_total_bytes": self._stdout.total, "stderr_total_bytes": self._stderr.total,
            "stdout_dropped_bytes": self._stdout.dropped, "stderr_dropped_bytes": self._stderr.dropped,
        }

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
                        if self._retention == "strict":
                            chunk = os.read(key.fd, min(8192, self._maximum + 1 - len(self._stdout) - len(self._stderr)))
                        else:
                            chunk = os.read(key.fd, 8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        self._selector.unregister(key.fileobj)
                    else:
                        key.data.extend(chunk)
                        if self._retention == "strict" and self._stdout.total + self._stderr.total > self._maximum:
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
    *, script_path: str, expected_sha256: str, arguments: Mapping[str, str | bool],
) -> str:
    if not PureWindowsPath(script_path).is_absolute() or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("owner Windows script path/hash invalid")
    if any(re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key) is None for key in arguments):
        raise ValueError("owner Windows parameter name invalid")
    if any(type(value) not in (str, bool) for value in arguments.values()):
        raise ValueError("owner Windows parameter value must be a string or a switch")
    path, digest = _quote_powershell(script_path), _quote_powershell(expected_sha256)
    # A bool selects a switch parameter: True emits the bare `-Name`, False omits it (the same form binds
    # under `& script` and `-File`); strings are always single-quoted values.
    args = " ".join(
        "-" + name if value is True else "-" + name + " " + _quote_powershell(value)
        for name, value in arguments.items() if value is not False
    )
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
