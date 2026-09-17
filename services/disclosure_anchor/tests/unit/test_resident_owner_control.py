"""Outer local command bounds; no live SSH, Windows or shared runtime."""

from __future__ import annotations

import base64
import hashlib
import errno
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from disclosure_anchor.adapters.runtime.resident_owner_control import (
    BoundedOwnerCommand, owner_ssh_command, pinned_windows_script,
)
from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig


class ResidentOwnerControlTests(unittest.TestCase):
    def test_head_tail_keeps_pipe_edges_counts_loss_and_preserves_complete_evidence_file(self):
        stdout = bytes(range(256)) * 200
        stderr = b"stderr-start\n" + b"z" * 70000 + b"\nstderr-end"
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "complete.bin"
            code = (
                "import os,pathlib,sys; "
                "out=bytes(range(256))*200; err=b'stderr-start\\n'+b'z'*70000+b'\\nstderr-end'; "
                "pathlib.Path(sys.argv[1]).write_bytes(out+err); "
                "os.write(1,out); os.write(2,err); raise SystemExit(7)"
            )
            command = BoundedOwnerCommand([sys.executable, "-c", code, str(evidence)],
                                          timeout_seconds=5, maximum_bytes=257, retention="head_tail")
            self.addCleanup(command.abort)
            result = command.finish()
            self.assertEqual(result.exit_code, 7)
            self.assertEqual(result.stdout, stdout[:128] + stdout[-128:])
            self.assertEqual(result.stderr, stderr[:128] + stderr[-128:])
            self.assertEqual(evidence.read_bytes(), stdout + stderr)
            self.assertEqual(command.retention_report(), {
                "mode": "head_tail", "stdout_total_bytes": len(stdout), "stderr_total_bytes": len(stderr),
                "stdout_dropped_bytes": len(stdout) - 256, "stderr_dropped_bytes": len(stderr) - 256,
            })
            self.assertIs(command.poll(), result)

    def test_head_tail_small_output_is_not_duplicated_and_volume_does_not_renew_deadline(self):
        command = BoundedOwnerCommand([sys.executable, "-c", "import os; os.write(1,b'123456789')"],
                                      timeout_seconds=3, maximum_bytes=16, retention="head_tail")
        self.addCleanup(command.abort)
        result = command.finish()
        self.assertEqual((result.stdout, result.stderr), (b"123456789", b""))
        self.assertEqual(command.retention_report(), {
            "mode": "head_tail", "stdout_total_bytes": 9, "stderr_total_bytes": 0,
            "stdout_dropped_bytes": 0, "stderr_dropped_bytes": 0,
        })
        flood = BoundedOwnerCommand([sys.executable, "-c", "import os\nwhile True: os.write(1,b'x'*8192)"],
                                    timeout_seconds=0.3, maximum_bytes=32, retention="head_tail")
        self.addCleanup(flood.abort)
        with self.assertRaises(TimeoutError):
            flood.finish()
        self.assertIsNotNone(flood._process.poll())
        self.assertTrue(flood._closed)
        self.assertLessEqual(len(flood.captured_output[0]), 32)

    def test_invalid_retention_is_refused_before_child_launch(self):
        for options in ({"retention": "unbounded"}, {"retention": "head_tail", "maximum_bytes": 1}):
            with self.subTest(options=options), patch("disclosure_anchor.adapters.runtime.resident_owner_control.subprocess.Popen") as popen:
                with self.assertRaises(ValueError):
                    BoundedOwnerCommand([sys.executable, "-c", "pass"], timeout_seconds=3, **options)
                popen.assert_not_called()

    def test_explicit_child_environment_and_cwd_do_not_mutate_parent_or_peer(self):
        code = "import json,os; print(json.dumps([os.getcwd(),os.getenv('M6_TEST_CHILD'),os.getenv('M6_TEST_PARENT')]))"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"M6_TEST_PARENT": "parent-only"}):
            before = dict(os.environ)
            environment = {"M6_TEST_CHILD": "child-only"}
            explicit = BoundedOwnerCommand([sys.executable, "-c", code], timeout_seconds=3,
                                           environment=environment, cwd=str(Path(directory).resolve()))
            self.addCleanup(explicit.abort)
            environment["M6_TEST_CHILD"] = "caller-changed-after-launch"
            inherited = BoundedOwnerCommand([sys.executable, "-c", code], timeout_seconds=3)
            self.addCleanup(inherited.abort)
            first, peer = explicit.finish(), inherited.finish()
            self.assertEqual(first.exit_code, 0, first.stderr)
            self.assertEqual(peer.exit_code, 0, peer.stderr)
            self.assertEqual(json.loads(first.stdout), [str(Path(directory).resolve()), "child-only", None])
            self.assertEqual(json.loads(peer.stdout), [os.getcwd(), before.get("M6_TEST_CHILD"), "parent-only"])
            self.assertEqual(dict(os.environ), before)

    def test_invalid_explicit_child_context_does_not_start_a_process(self):
        for kwargs in ({"environment": {"X": 4}}, {"environment": {1: "X"}},
                       {"cwd": "relative"}, {"cwd": str(Path(__file__).resolve())}):
            with self.subTest(kwargs=kwargs), patch("disclosure_anchor.adapters.runtime.resident_owner_control.subprocess.Popen") as popen:
                with self.assertRaises(ValueError):
                    BoundedOwnerCommand([sys.executable, "-c", "pass"], timeout_seconds=2, **kwargs)
                popen.assert_not_called()

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
