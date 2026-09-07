"""Explicit trust, one destination and full SSH-thread shutdown boundaries."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from disclosure_anchor.adapters.runtime.resident_ssh_http import (
    ResidentSSHConfig,
    ResidentSSHHTTPClient,
    _read_private_config,
)
from disclosure_anchor.adapters.runtime.bounded_http import BoundedHTTPTransportError


@unittest.skipUnless(importlib.util.find_spec("paramiko"), "optional resident-ssh dependency not installed")
class ResidentSSHHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        import paramiko
        self.paramiko = paramiko
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.key_path = root / "synthetic-key"
        private = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        self.key_path.write_bytes(private)
        self.key_path.chmod(0o600)
        self.key = paramiko.Ed25519Key.from_private_key(io.StringIO(private.decode()))
        self.host_key = paramiko.RSAKey.generate(2048)
        self.known_path = root / "synthetic-known-hosts"
        self.config = self.configure(22)

    def configure(self, port: int) -> ResidentSSHConfig:
        host = "127.0.0.1" if port == 22 else f"[127.0.0.1]:{port}"
        self.known_path.write_text(f"{host} {self.host_key.get_name()} {self.host_key.get_base64()}\n")
        return ResidentSSHConfig("127.0.0.1", port, "synthetic-user", str(self.key_path), str(self.known_path))

    def transport(self) -> MagicMock:
        transport = MagicMock()
        transport.get_remote_server_key.return_value = self.host_key
        transport.auth_publickey.return_value = []
        transport.is_authenticated.return_value = True
        transport.is_alive.return_value = False
        transport.ident = 123
        return transport

    def test_unknown_and_mismatched_host_never_authenticate(self) -> None:
        transport = self.transport()
        transport.get_remote_server_key.return_value = self.key
        with patch("socket.create_connection"), patch("paramiko.Transport", return_value=transport):
            with self.assertRaisesRegex(ValueError, "server key"):
                ResidentSSHHTTPClient(self.config, remote_port=9835, maximum_response_bytes=1024)
        transport.auth_publickey.assert_not_called()
        transport.join.assert_called_once_with(timeout=2.0)
        self.known_path.write_text(self.known_path.read_text().replace("127.0.0.1", "127.0.0.2"))
        with patch("socket.create_connection") as connect:
            with self.assertRaisesRegex(ValueError, "absent"):
                ResidentSSHHTTPClient(self.config, remote_port=9835, maximum_response_bytes=1024)
            connect.assert_not_called()

    def test_partial_authentication_never_falls_back(self) -> None:
        for remaining, authenticated in ((["keyboard-interactive"], False), ([], False)):
            with self.subTest(remaining=remaining):
                transport = self.transport()
                transport.auth_publickey.return_value = remaining
                transport.is_authenticated.return_value = authenticated
                with patch("socket.create_connection"), patch("paramiko.Transport", return_value=transport):
                    with self.assertRaisesRegex(ValueError, "incomplete"):
                        ResidentSSHHTTPClient(self.config, remote_port=9835, maximum_response_bytes=1024)
                transport.auth_publickey.assert_called_once()
                transport.auth_password.assert_not_called()
                transport.auth_interactive.assert_not_called()
                transport.auth_interactive_dumb.assert_not_called()
                transport.open_channel.assert_not_called()

    def test_hashed_and_plain_alias_conflicts_are_rejected_before_connect(self) -> None:
        other = self.paramiko.RSAKey.generate(2048)
        for port in (22, 2222):
            config = self.configure(port)
            hostname = "127.0.0.1" if port == 22 else f"[127.0.0.1]:{port}"
            for first in (hostname, self.paramiko.HostKeys.hash_host(hostname)):
                second = self.paramiko.HostKeys.hash_host(hostname)
                self.known_path.write_text(
                    f"{first} ssh-rsa {self.host_key.get_base64()}\n"
                    f"{second} ssh-rsa {other.get_base64()}\n"
                )
                with patch("socket.create_connection") as connect:
                    with self.assertRaisesRegex(ValueError, "conflicting"):
                        ResidentSSHHTTPClient(config, remote_port=9835, maximum_response_bytes=1024)
                    connect.assert_not_called()
            # Different aliases for the same trusted key are not a conflict.
            self.known_path.write_text(
                f"{hostname} ssh-rsa {self.host_key.get_base64()}\n"
                f"{second} ssh-rsa {self.host_key.get_base64()}\n"
            )
            transport = self.transport()
            with patch("socket.create_connection"), patch("paramiko.Transport", return_value=transport):
                client = ResidentSSHHTTPClient(config, remote_port=9835, maximum_response_bytes=1024)
                client.close()

    def test_fixed_destination_and_no_subprocess_or_command_and_joined_close(self) -> None:
        transport = self.transport()
        with patch("socket.create_connection"), patch("paramiko.Transport", return_value=transport), patch(
            "subprocess.Popen", side_effect=AssertionError("no descendant allowed")
        ):
            client = ResidentSSHHTTPClient(self.config, remote_port=9835, maximum_response_bytes=1024)
            connection = client._new_connection(0.5)
            connection.connect()
            transport.open_channel.assert_called_once_with(
                "direct-tcpip", dest_addr=("127.0.0.1", 9835), src_addr=("127.0.0.1", 0), timeout=0.5
            )
            client.close()
            client.close()
        transport.open_session.assert_not_called()
        transport.join.assert_called_once_with(timeout=2.0)
        transport.is_alive.assert_called_once()

    def test_lingering_thread_and_cleanup_errors_remain_visible(self) -> None:
        transport = self.transport()
        with patch("socket.create_connection"), patch("paramiko.Transport", return_value=transport):
            client = ResidentSSHHTTPClient(self.config, remote_port=9835, maximum_response_bytes=1024)
            transport.is_alive.return_value = True
            with self.assertRaises(BaseExceptionGroup) as result:
                client.close()
            self.assertIn("cleanup", str(result.exception))
            transport.is_alive.return_value = False
            client.close()

    def test_config_file_bounds_modes_symlinks_and_host_conflicts(self) -> None:
        self.key_path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "mode"):
            _read_private_config(str(self.key_path), secret=True)
        self.key_path.chmod(0o600)
        link = self.key_path.with_name("symlink")
        link.symlink_to(self.key_path)
        with self.assertRaises(OSError):
            _read_private_config(str(link), secret=True)
        self.key_path.write_bytes(b"x" * 65537)
        with self.assertRaisesRegex(ValueError, "size"):
            _read_private_config(str(self.key_path), secret=True)
        other = self.paramiko.RSAKey.generate(2048)
        self.known_path.write_text(self.known_path.read_text() + f"127.0.0.1 ssh-rsa {other.get_base64()}\n")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            ResidentSSHHTTPClient(self.config, remote_port=9835, maximum_response_bytes=1024)

    def test_real_ssh_framing_reuse_timeout_and_shutdown(self) -> None:
        paramiko = self.paramiko
        for stall in (False, True):
            with self.subTest(stall=stall):
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                listener.settimeout(3)
                config = self.configure(listener.getsockname()[1])
                authorized_key = self.key
                destinations = []
                failures = []
                stop = threading.Event()

                class Server(paramiko.ServerInterface):
                    def check_auth_publickey(self, username, key):
                        return paramiko.AUTH_SUCCESSFUL if username == "synthetic-user" and key == authorized_key else paramiko.AUTH_FAILED

                    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
                        destinations.append(destination)
                        return paramiko.OPEN_SUCCEEDED if destination == ("127.0.0.1", 9835) else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

                def serve():
                    transport = None
                    sock = None
                    try:
                        sock, _ = listener.accept()
                        transport = paramiko.Transport(sock)
                        transport.add_server_key(self.host_key)
                        transport.start_server(server=Server())
                        channel = transport.accept(3)
                        if channel is None:
                            raise AssertionError("missing forwarded channel")
                        channel.settimeout(3)
                        for _ in range(1 if stall else 2):
                            request = b""
                            while not request.endswith(b"\r\n\r\n"):
                                part = channel.recv(1)
                                if not part:
                                    raise AssertionError("incomplete request")
                                request += part
                                if len(request) > 4096:
                                    raise AssertionError("request unbounded")
                            if stall:
                                stop.wait(3)
                            else:
                                channel.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                        stop.wait(3)
                    except BaseException as exc:
                        failures.append(exc)
                    finally:
                        if sock is not None:
                            try:
                                sock.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                        if transport is not None:
                            transport.close()
                            transport.join(2)
                            if transport.is_alive():
                                failures.append(AssertionError("server transport survived"))
                        if sock is not None:
                            sock.close()
                        listener.close()

                thread = threading.Thread(target=serve)
                baseline = set(threading.enumerate())
                thread.start()
                client = None
                try:
                    with patch("subprocess.Popen", side_effect=AssertionError("no subprocess")):
                        client = ResidentSSHHTTPClient(config, remote_port=9835, maximum_response_bytes=1024)
                        if stall:
                            started = time.monotonic()
                            with self.assertRaises(BoundedHTTPTransportError):
                                client.get_bytes("/health", timeout_seconds=0.15)
                            self.assertLess(time.monotonic() - started, 1.5)
                        else:
                            for _ in range(2):
                                self.assertEqual(client.get_bytes("/health", timeout_seconds=2), (200, b"{}"))
                finally:
                    if client is not None:
                        client.close()
                    stop.set()
                    thread.join(4)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(destinations, [("127.0.0.1", 9835)])
                self.assertEqual(set(threading.enumerate()) - baseline, set())


if __name__ == "__main__":
    unittest.main()
