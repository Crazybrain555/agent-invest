"""Optional, in-process SSH transport for one pinned loopback HTTP endpoint.

No SSH config, agents, commands, interactive authentication or listening socket.
The collector owner must call close before its preseal CPU accounting boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hmac
import http.client
import io
import ipaddress
import os
from pathlib import Path
import socket
import stat
import threading
import time
from typing import Any

from disclosure_anchor.adapters.runtime.bounded_http import (
    BoundedHTTPTransportError,
    ThreadOwnedPersistentHTTPClient,
)


@dataclass(frozen=True, slots=True)
class ResidentSSHConfig:
    """Private configuration references, never private key bytes in a spec."""

    address: str
    port: int
    username: str
    private_key_path: str
    known_hosts_path: str

    def __post_init__(self) -> None:
        # Literal addresses avoid an unbounded DNS lookup during collector READY.
        if str(ipaddress.ip_address(self.address)) != self.address:
            raise ValueError("SSH address must be a canonical IP literal")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("SSH port is invalid")
        if not self.username or not self.username.isascii() or any(
            ch.isspace() or ord(ch) < 32 for ch in self.username
        ):
            raise ValueError("SSH username is invalid")
        for path in (self.private_key_path, self.known_hosts_path):
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise ValueError("SSH configuration file must be an explicit absolute path")


def _read_private_config(path: str, *, secret: bool) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        forbidden = 0o077 if secret else 0o022
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & forbidden
            or before.st_nlink != 1
            or not 0 < before.st_size <= 65536
        ):
            raise ValueError("SSH configuration file ownership, mode or size is invalid")
        data = os.read(fd, 65537)
        after = os.fstat(fd)
        def identity(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
                value.st_mode, value.st_uid, value.st_nlink,
            )
        if identity(before) != identity(after) or len(data) != before.st_size:
            raise ValueError("SSH configuration file changed during read")
        return data.decode("utf-8", errors="strict")
    finally:
        os.close(fd)


class _Session:
    def __init__(self, config: ResidentSSHConfig, *, remote_port: int, timeout: float) -> None:
        if type(remote_port) is not int or not 1024 <= remote_port <= 65535:
            raise ValueError("SSH loopback destination port is invalid")
        if not 0 < timeout <= 30:
            raise ValueError("SSH startup timeout is invalid")
        import paramiko  # optional dependency; never imported by ordinary workers

        if paramiko.__version__ != "5.0.0":
            raise RuntimeError("resident SSH requires the reviewed Paramiko 5.0.0")
        self._owner = threading.get_ident()
        self._remote_port = remote_port
        self._socket: socket.socket | None = None
        self._transport: Any = None
        self._closed = False
        lookup = config.address if config.port == 22 else f"[{config.address}]:{config.port}"
        expected: dict[str, Any] = {}
        for line in _read_private_config(config.known_hosts_path, secret=False).splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
            if entry is None or entry.key is None:
                raise ValueError("SSH known-host entry is invalid")
            # Resolve every record against the actual configured host. Comparing
            # record names misses aliases with different OpenSSH hash salts.
            record = paramiko.HostKeys()
            for hostname in entry.hostnames:
                record.add(hostname, entry.key.get_name(), entry.key)
            if record.lookup(lookup):
                kind = entry.key.get_name()
                if kind in expected and not hmac.compare_digest(
                    expected[kind].asbytes(), entry.key.asbytes()
                ):
                    raise ValueError("SSH known-hosts contains conflicting keys")
                expected[kind] = entry.key
        if not expected:
            raise ValueError("SSH host is absent from the specified known-hosts file")
        # Exact key kind: do not discover adjacent certificate files or other keys.
        private_key = paramiko.Ed25519Key.from_private_key(
            io.StringIO(_read_private_config(config.private_key_path, secret=True))
        )
        deadline = time.monotonic() + timeout
        try:
            self._socket = socket.create_connection((config.address, config.port), timeout=timeout)
            self._transport = paramiko.Transport(self._socket)
            transport = self._transport
            transport.banner_timeout = self._remaining(deadline)
            transport.handshake_timeout = self._remaining(deadline)
            transport.start_client(timeout=self._remaining(deadline))
            remote_key = transport.get_remote_server_key()
            expected_key = expected.get(remote_key.get_name())
            if expected_key is None or not hmac.compare_digest(
                expected_key.asbytes(), remote_key.asbytes()
            ):
                raise ValueError("SSH server key does not match the specified known-hosts file")
            transport.auth_timeout = self._remaining(deadline)
            remaining_methods = transport.auth_publickey(config.username, private_key)
            if remaining_methods or not transport.is_authenticated():
                raise ValueError("SSH public-key authentication was incomplete; no fallback allowed")
            self._remaining(deadline)
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup("SSH startup and cleanup failed", [primary, cleanup])
            raise

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BoundedHTTPTransportError("SSH operation deadline expired")
        return remaining

    def _assert_owner(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError("resident SSH crossed collector thread ownership")

    def open_channel(self, timeout: float) -> Any:
        self._assert_owner()
        if self._closed or not self._transport.is_authenticated():
            raise BoundedHTTPTransportError("resident SSH session is closed or unauthenticated")
        channel = self._transport.open_channel(
            "direct-tcpip",
            dest_addr=("127.0.0.1", self._remote_port),
            src_addr=("127.0.0.1", 0),
            timeout=timeout,
        )
        channel.settimeout(timeout)
        return channel

    def close(self) -> None:
        self._assert_owner()
        if self._closed:
            return
        errors: list[BaseException] = []
        # Shut the OS socket first: no protocol send/rekey can prolong close.
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError as exc:
                if exc.errno not in {errno.ENOTCONN, errno.EBADF}:
                    errors.append(exc)
        if self._transport is not None:
            try:
                self._transport.close()
            except BaseException as exc:
                errors.append(exc)
            try:
                if self._transport.ident is not None:
                    self._transport.join(timeout=2.0)
                if self._transport.is_alive():
                    raise RuntimeError("resident SSH transport thread did not exit")
            except BaseException as exc:
                errors.append(exc)
        if self._socket is not None:
            try:
                self._socket.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("resident SSH cleanup was not verified", errors)
        self._closed = True


class _ForwardedConnection(http.client.HTTPConnection):
    def __init__(self, session: _Session, *, port: int, timeout: float) -> None:
        super().__init__("127.0.0.1", port=port, timeout=timeout)
        self._session = session

    def connect(self) -> None:
        if self.timeout is None:
            raise ValueError("SSH HTTP requires a finite operation timeout")
        self.sock = self._session.open_channel(self.timeout)


class ResidentSSHHTTPClient(ThreadOwnedPersistentHTTPClient):
    """One authenticated SSH lifetime, one destination, bounded persistent HTTP."""

    def __init__(
        self,
        config: ResidentSSHConfig,
        *,
        remote_port: int,
        maximum_response_bytes: int,
        startup_timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            f"http://127.0.0.1:{remote_port}",
            maximum_response_bytes=maximum_response_bytes,
            user_agent="disclosure-anchor-resident-ssh/1",
        )
        self._session = _Session(config, remote_port=remote_port, timeout=startup_timeout_seconds)
        self._remote_port = remote_port

    def _new_connection(self, timeout_seconds: float) -> http.client.HTTPConnection:
        return _ForwardedConnection(self._session, port=self._remote_port, timeout=timeout_seconds)

    def close(self) -> None:
        self._session._assert_owner()
        errors: list[BaseException] = []
        for close in (self._session.close, super().close):
            try:
                close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("resident SSH HTTP cleanup failed", errors)
