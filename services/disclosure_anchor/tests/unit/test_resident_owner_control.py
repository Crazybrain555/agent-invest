"""Outer local command bounds; no live SSH, Windows or shared runtime."""

from __future__ import annotations

import base64
import hashlib
import errno
from pathlib import Path
import signal
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from disclosure_anchor.adapters.runtime.resident_owner_control import (
    BoundedOwnerCommand, owner_ssh_command, pinned_windows_script,
)
from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig


class ResidentOwnerControlTests(unittest.TestCase):
    def test_darwin_zombie_eperm_requires_reaped_leader_and_explicit_group_absence(self):
        cases = ((0, ProcessLookupError(errno.ESRCH, "no group"), False),
                 (None, None, True), (0, None, True),
                 (0, PermissionError(errno.EPERM, "still inaccessible"), True))
        for code, probe, fails in cases:
            command = BoundedOwnerCommand.__new__(BoundedOwnerCommand)
            command._closed = False
            command._process = SimpleNamespace(pid=43210, poll=Mock(return_value=code), wait=Mock())
            command._dispose = Mock()
            first = PermissionError(errno.EPERM, "owned group signal denied")
            with self.subTest(code=code, probe=probe), patch("disclosure_anchor.adapters.runtime.resident_owner_control.os.killpg", side_effect=[first, probe]) as kill:
                if fails:
                    with self.assertRaises(PermissionError):
                        command.abort()
                    command._dispose.assert_not_called()
                else:
                    command.abort()
                    command._dispose.assert_called_once_with()
                self.assertEqual(kill.call_args_list[0].args, (43210, signal.SIGKILL))
                if code is None:
                    self.assertEqual(kill.call_count, 1)
                else:
                    self.assertEqual(kill.call_args_list[1].args, (43210, 0))
                command._process.wait.assert_not_called()

    def test_concurrent_streams_preserve_bytes_and_nonzero_exit(self):
        command = BoundedOwnerCommand([sys.executable, "-c", "import os;os.write(1,b'raw\\n');os.write(2,b'warning\\n');raise SystemExit(7)"], timeout_seconds=2)
        result = command.finish()
        self.assertEqual((result.exit_code, result.stdout, result.stderr), (7, b"raw\n", b"warning\n"))
        self.assertTrue(command._closed)
        self.assertIs(command.poll(), result)

    def test_deadline_and_output_overflow_kill_and_reap_only_owned_local_command(self):
        for script, timeout, cap, error in (("import time;time.sleep(60)", 0.1, 1024, TimeoutError), ("import os,time;os.write(1,b'x'*4096);time.sleep(60)", 2, 1024, ValueError)):
            with self.subTest(error=error):
                command = BoundedOwnerCommand([sys.executable, "-c", script], timeout_seconds=timeout, maximum_bytes=cap)
                with self.assertRaises(error):
                    command.finish()
                self.assertIsNotNone(command._process.poll())
                self.assertTrue(command._closed)
                self.assertLessEqual(sum(map(len, command.captured_output)), cap + 1)

    def test_pinned_script_quoting_and_explicit_ssh_trust_arguments(self):
        executable = Path(sys.executable)
        sha = "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest()
        script = pinned_windows_script(script_path="C:\\owned\\script's.ps1", expected_sha256="sha256:" + "a" * 64, arguments={"ConfigJsonPath": "C:\\owned\\config.json"})
        self.assertIn("script''s.ps1", script)
        self.assertLess(script.index("source drift"), script.index(";& "))
        ssh = ResidentSSHConfig("192.0.2.1", 22, "fixture", "/fixture/key", "/fixture/known-hosts")
        args = owner_ssh_command(executable=executable, executable_sha256=sha, ssh=ssh, script=script)
        self.assertIn("IdentityAgent=none", args)
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("UserKnownHostsFile=/fixture/known-hosts", args)
        self.assertIn("GlobalKnownHostsFile=/dev/null", args)
        self.assertIn("IdentityFile=none", args)
        self.assertIn("PreferredAuthentications=publickey", args)
        self.assertEqual(base64.b64decode(args[-1]).decode("utf-16-le"), script)
        with self.assertRaises(ValueError):
            owner_ssh_command(executable=executable, executable_sha256="sha256:" + "0" * 64, ssh=ssh, script=script)
        for arguments in ({"Bad;Key": "x"}, {"ConfigJsonPath": "a\nb"}):
            with self.assertRaises(ValueError):
                pinned_windows_script(script_path="C:\\owned\\s.ps1", expected_sha256="sha256:" + "a" * 64, arguments=arguments)
