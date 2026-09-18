"""Independent checks of the pinned-OpenSSH M6 owner transport session.

No real ssh binary, network, or owner is used: the executable is a temp file
whose bytes are hashed, and subprocess.Popen is replaced by a recorder that
holds the child end of the local socketpair exactly like a spawned child would.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import m6_owner_ssh
from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig

CONFIG = ResidentSSHConfig("192.0.2.1", 2222, "frozen", "/private/m6/key", "/private/m6/known_hosts")
OWNER_PORT = 39840
EXPECTED_ARGV_TAIL = [
    "-F", "/dev/null", "-p", "2222", "-i", "/private/m6/key",
    "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
    "-o", "UserKnownHostsFile=/private/m6/known_hosts",
    "-o", "GlobalKnownHostsFile=/dev/null", "-o", "IdentityFile=none", "-o", "ConnectTimeout=5",
    "-o", "IdentityAgent=none", "-o", "ClearAllForwardings=yes",
    "-o", "PreferredAuthentications=publickey", "-o", "LogLevel=ERROR", "-T",
    "-W", f"127.0.0.1:{OWNER_PORT}", "frozen@192.0.2.1",
]


class _RecordedChild:
    """Duck-typed Popen result holding a dup of the child fd like a real child would.

    behavior: exits_on_eof (real ssh exits when its stdin closes) | needs_terminate |
    needs_kill | immortal.
    """

    def __init__(self, argv: list[str], kwargs: dict[str, object], *, behavior: str = "exits_on_eof") -> None:
        self.argv, self.kwargs, self.behavior = argv, kwargs, behavior
        self.child_fd = os.dup(int(kwargs["stdin"]))  # type: ignore[call-overload]
        self.stderr_fd = int(kwargs["stderr"])  # type: ignore[call-overload]
        self.returncode: int | None = None
        self.calls: list[str] = []

    def _exit(self, code: int) -> None:
        if self.child_fd >= 0:
            os.close(self.child_fd)
            self.child_fd = -1
        self.returncode = code

    def die(self, code: int, stderr: bytes) -> None:
        os.write(self.stderr_fd, stderr)
        self._exit(code)

    def poll(self) -> int | None:
        self.calls.append("poll")
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait")
        if self.returncode is None and self.behavior == "exits_on_eof":
            self._exit(0)
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        return self.returncode

    def terminate(self) -> None:
        self.calls.append("terminate")
        if self.behavior == "needs_terminate":
            self._exit(0)

    def kill(self) -> None:
        self.calls.append("kill")
        if self.behavior in ("needs_terminate", "needs_kill"):
            self._exit(-9)


class PinnedOpenSSHSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="m6-openssh-")
        self.addCleanup(self.tmp.cleanup)
        self.exe = Path(self.tmp.name) / "ssh"
        self.exe.write_bytes(b"synthetic-openssh-bytes")
        self.sha = "sha256:" + hashlib.sha256(self.exe.read_bytes()).hexdigest()
        self.children: list[_RecordedChild] = []

    def _popen(self, behavior: str = "exits_on_eof"):
        def popen(argv: list[str], **kwargs: object) -> _RecordedChild:
            child = _RecordedChild(argv, kwargs, behavior=behavior)
            self.children.append(child)
            return child
        return popen

    def _session(self) -> m6_owner_ssh._OpenSSHDirectSession:
        return m6_owner_ssh._OpenSSHDirectSession(CONFIG, executable=self.exe, executable_sha256=self.sha, remote_port=OWNER_PORT)

    def test_argv_is_exact_shell_free_and_pins_every_hardening_option(self) -> None:
        argv = m6_owner_ssh._openssh_direct_argv(self.exe, CONFIG, OWNER_PORT, 5.0)
        self.assertEqual(argv, [str(self.exe)] + EXPECTED_ARGV_TAIL)
        self.assertTrue(all(type(item) is str for item in argv))
        self.assertNotIn("ProxyCommand", " ".join(argv))

        def connect_timeout(timeout: float) -> str:
            argv_ = m6_owner_ssh._openssh_direct_argv(self.exe, CONFIG, OWNER_PORT, timeout)
            return [item for item in argv_ if item.startswith("ConnectTimeout=")][0]
        self.assertEqual(connect_timeout(0.4), "ConnectTimeout=1")
        self.assertEqual(connect_timeout(5.0), "ConnectTimeout=5")
        self.assertEqual(connect_timeout(30.0), "ConnectTimeout=10")
        for bad in (0.0, -1.0, 30.5):
            with self.assertRaises(ValueError, msg=str(bad)):
                m6_owner_ssh._openssh_direct_argv(self.exe, CONFIG, OWNER_PORT, bad)
        spaced = ResidentSSHConfig("192.0.2.1", 2222, "frozen", "/private/m6/key", "/private/m6/known hosts")
        with self.assertRaises(ValueError):
            m6_owner_ssh._openssh_direct_argv(self.exe, spaced, OWNER_PORT, 5.0)

    def test_hash_pin_and_port_refusals_happen_before_any_spawn(self) -> None:
        def never(*args: object, **kwargs: object) -> None:
            raise AssertionError("OpenSSH must not be spawned when the pin is refused")
        with mock.patch.object(m6_owner_ssh.subprocess, "Popen", never):
            wrong = "sha256:" + "0" * 64
            with self.assertRaises(ValueError):
                m6_owner_ssh._OpenSSHDirectSession(CONFIG, executable=self.exe, executable_sha256=wrong, remote_port=OWNER_PORT)
            with self.assertRaises(ValueError):
                m6_owner_ssh._OpenSSHDirectSession(CONFIG, executable=Path("ssh"), executable_sha256=self.sha, remote_port=OWNER_PORT)
            with self.assertRaises(ValueError):
                m6_owner_ssh._OpenSSHDirectSession(
                    CONFIG, executable=self.exe.with_name("absent"), executable_sha256=self.sha, remote_port=OWNER_PORT,
                )
            for port in (0, 1023, 65536):
                with self.assertRaises(ValueError, msg=str(port)):
                    m6_owner_ssh._OpenSSHDirectSession(CONFIG, executable=self.exe, executable_sha256=self.sha, remote_port=port)
            session = self._session()
            # The pin is re-checked on every spawn, so a file swapped after construction is refused too.
            self.exe.write_bytes(b"replaced-bytes")
            with self.assertRaises(ValueError):
                session.open_channel(1.0)
            with self.assertRaises(ValueError):
                self._session()

    def test_open_channel_spawns_pinned_child_on_the_local_socketpair_and_returns_the_parent_end(self) -> None:
        session = self._session()
        with mock.patch.object(m6_owner_ssh.subprocess, "Popen", self._popen()):
            channel = session.open_channel(5.0)
            self.assertEqual(len(self.children), 1)
            child = self.children[0]
            self.assertEqual(child.argv, [str(self.exe)] + EXPECTED_ARGV_TAIL)
            self.assertEqual(child.kwargs["stdin"], child.kwargs["stdout"], "stdio is one socketpair end")
            self.assertTrue(stat.S_ISREG(os.fstat(child.stderr_fd).st_mode), "stderr is a bounded regular temp file")
            self.assertIs(child.kwargs["close_fds"], True)
            self.assertIs(child.kwargs["start_new_session"], True)
            self.assertNotIn("shell", child.kwargs)
            self.assertEqual(set(child.kwargs["env"]), {"PATH", "HOME"})  # type: ignore[call-overload]
            self.assertEqual(channel.gettimeout(), 5.0)
            with self.assertRaises(OSError, msg="the parent must close its copy of the child end"):
                os.fstat(int(child.kwargs["stdin"]))  # type: ignore[call-overload]
            remote = socket.socket(fileno=os.dup(child.child_fd))
            try:
                channel.sendall(b"M6-AUTH/1 token\n{}\n")
                self.assertEqual(remote.recv(64), b"M6-AUTH/1 token\n{}\n")
                remote.sendall(b'{"reply":1}\n')
                self.assertEqual(channel.recv(64), b'{"reply":1}\n')
            finally:
                remote.close()
            # A second open reaps the first child, which exits on stdin EOF: no terminate, no kill.
            replacement = session.open_channel(2.0)
            self.assertEqual(len(self.children), 2)
            self.assertEqual(child.calls[:2], ["poll", "wait"])
            self.assertNotIn("terminate", child.calls)
            self.assertNotIn("kill", child.calls)
            self.assertEqual(child.returncode, 0)
            self.assertEqual(replacement.gettimeout(), 2.0)
            self.assertEqual(channel.fileno(), -1, "the retired parent end is closed")
            session.close()
            session.close()  # idempotent
            self.assertEqual(self.children[1].returncode, 0)
            with self.assertRaises(OSError, msg="the last stderr temp file is closed with the session"):
                os.fstat(self.children[1].stderr_fd)
            with self.assertRaises(RuntimeError):
                session.open_channel(1.0)

    def test_peer_eof_reports_child_exit_status_and_bounded_stderr_prefix_then_allows_a_fresh_child(self) -> None:
        session = self._session()
        with mock.patch.object(m6_owner_ssh.subprocess, "Popen", self._popen()):
            channel = session.open_channel(5.0)
            child = self.children[0]
            child.die(255, b"x" * 3000 + b"\nssh: connect to host 192.0.2.1 port 2222: No route to host\n")
            with self.assertRaises(EOFError) as caught:
                channel.recv(64)
            message = str(caught.exception)
            self.assertIn("remote outcome requires reconciliation", message)
            self.assertIn("pinned OpenSSH exited 255", message)
            self.assertLess(len(message), 2300, "the stderr prefix is bounded")
            self.assertEqual(child.calls.count("terminate"), 0)
            replacement = session.open_channel(1.0)
            self.assertEqual(len(self.children), 2)
            self.assertEqual(replacement.gettimeout(), 1.0)
            session.close()
        with mock.patch.object(m6_owner_ssh.subprocess, "Popen", self._popen()):
            session = self._session()
            channel = session.open_channel(5.0)
            self.children[-1].die(255, b"ssh: connect to host 192.0.2.1 port 2222: No route to host\n")
            with self.assertRaises(EOFError) as caught:
                channel.recv(64)
            self.assertIn("No route to host", str(caught.exception))
            session.close()

    def test_retire_escalates_to_terminate_then_kill_and_fails_loudly_if_the_child_never_exits(self) -> None:
        session = self._session()
        with mock.patch.object(m6_owner_ssh.subprocess, "Popen", self._popen("needs_terminate")):
            session.open_channel(1.0)
            session.close()
        stubborn = self.children[-1]
        self.assertEqual((stubborn.calls.count("terminate"), stubborn.calls.count("kill"), stubborn.returncode), (1, 0, 0))
        session = self._session()
        with mock.patch.object(m6_owner_ssh.subprocess, "Popen", self._popen("needs_kill")):
            session.open_channel(1.0)
            session.close()
        killed = self.children[-1]
        self.assertEqual((killed.calls.count("terminate"), killed.calls.count("kill"), killed.returncode), (1, 1, -9))
        session = self._session()
        with mock.patch.object(m6_owner_ssh.subprocess, "Popen", self._popen("immortal")):
            session.open_channel(1.0)
            with self.assertRaises(RuntimeError):
                session.close()
        immortal = self.children[-1]
        self.assertEqual(immortal.calls.count("kill"), 1)
        os.close(immortal.child_fd)
        # A peer loss whose child cannot be reaped still carries the child report in the RuntimeError.
        session = self._session()
        with mock.patch.object(m6_owner_ssh.subprocess, "Popen", self._popen("immortal")):
            channel = session.open_channel(1.0)
            stuck = self.children[-1]
            os.write(stuck.stderr_fd, b"ssh: hung after connect\n")
            os.close(stuck.child_fd)
            stuck.child_fd = -1
            with self.assertRaises(RuntimeError) as caught:
                channel.recv(64)
        self.assertIn("did not exit", str(caught.exception))
        self.assertIn("hung after connect", str(caught.exception))

    def test_spawn_failure_leaks_neither_socketpair_end_nor_the_stderr_file(self) -> None:
        session = self._session()
        seen: dict[str, int] = {}
        def failing(argv: list[str], **kwargs: object) -> None:
            seen["stdin"] = int(kwargs["stdin"])  # type: ignore[call-overload]
            seen["stderr"] = int(kwargs["stderr"])  # type: ignore[call-overload]
            raise OSError("controlled spawn failure")
        with mock.patch.object(m6_owner_ssh.subprocess, "Popen", failing):
            with self.assertRaises(OSError):
                session.open_channel(1.0)
        for name in ("stdin", "stderr"):
            with self.assertRaises(OSError, msg=name):
                os.fstat(seen[name])
        session.close()

    def test_session_is_bound_to_its_owner_thread(self) -> None:
        session = self._session()
        errors: list[BaseException] = []
        def cross() -> None:
            try:
                session.open_channel(1.0)
            except BaseException as exc:  # noqa: BLE001 - the test records the exact class
                errors.append(exc)
        thread = threading.Thread(target=cross)
        thread.start()
        thread.join(5)
        self.assertEqual([type(exc) for exc in errors], [RuntimeError])
        session.close()


if __name__ == "__main__":
    unittest.main()
