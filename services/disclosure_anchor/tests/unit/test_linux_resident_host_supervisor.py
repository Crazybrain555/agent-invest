from __future__ import annotations

import os
from pathlib import Path
import select
import subprocess
import sys
import unittest

from scripts.windows import linux_resident_host_sampler as sampler
from scripts.windows import linux_resident_host_supervisor as supervisor
from tests.unit import test_linux_resident_host_sampler as sampler_tests


class LinuxResidentHostSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reuse the existing kernel fixture, not a second metric oracle.
        self.fixture = sampler_tests.LinuxResidentHostSamplerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def _child(self, mode: str = "normal") -> subprocess.Popen[bytes]:
        code = (
            "import os,time\nfrom pathlib import Path\n"
            "from scripts.windows import linux_resident_host_sampler as m\n"
            "from scripts.windows import linux_resident_host_supervisor as s\n"
            "real=m.HostSampler\n"
            f"m.HostSampler=lambda c:real(c,proc_root=Path({str(self.fixture.proc)!r}),cgroup_root=Path({str(self.fixture.cg)!r}))\n"
            "m.host_namespaces=lambda:{'pid':'fixture-pid','cgroup':'fixture-cgroup'}\n"
            "m.self_process_stat=lambda:{'start_ticks':1}\n"
            # macOS has no /proc. The fresh child imports only stdlib/scripts;
            # the actual Linux single-thread guard is separately smoke-tested.
            "s.ensure_single_thread=lambda m:None\n"
        )
        if mode == "exit_cpu":
            code += (
                "original=m.run\n"
                "def with_exit_work(*args):\n original(*args)\n until=time.process_time()+0.08\n while time.process_time()<until: pass\n"
                "m.run=with_exit_work\n"
            )
        elif mode == "abnormal":
            code += "original=m.run\ndef abnormal(*args):\n original(*args)\n os._exit(7)\nm.run=abnormal\n"
        elif mode == "hang":
            code += "real.sample=lambda self:time.sleep(30)\n"
        elif mode == "backpressure":
            code += "real.sample=lambda self:{'bounded_backpressure_probe':'x'*60000}\n"
        code += f"s.run({self.fixture.config!r},m,'sha256:'+'a'*64,'sha256:'+'b'*64)\n"
        child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        def cleanup() -> None:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)
        self.addCleanup(cleanup)
        return child

    def _read(self, child: subprocess.Popen[bytes]) -> dict:
        assert child.stdout is not None
        self.assertTrue(select.select([child.stdout], [], [], 2)[0], "supervisor output timeout")
        body = child.stdout.readline()
        self.assertTrue(body, "unexpected supervisor EOF")
        return sampler.decode(body.removesuffix(b"\n"))

    def _send(self, child: subprocess.Popen[bytes], command: str, sequence: int) -> None:
        assert child.stdin is not None
        child.stdin.write(sampler.canonical({"command": command, "sequence": sequence}) + b"\n")
        child.stdin.flush()

    def _absent(self, pid: int) -> None:
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_exact_child_reap_includes_exit_cpu_without_double_count(self) -> None:
        child = self._child("exit_cpu")
        ready = self._read(child)
        inner = ready["sampler_ready"]
        self.assertEqual(ready["contract_version"], supervisor.VERSION)
        self.assertEqual(ready["identity"]["pid"], child.pid)
        self._send(child, "sample", 1)
        self.assertEqual(self._read(child)["epoch_sha256"], inner["epoch_sha256"])
        self._send(child, "close", 2)
        close = self._read(child)
        receipt = self._read(child)
        self.assertEqual(receipt["kind"], "closed")
        self.assertEqual(receipt["sampler_epoch_sha256"], inner["epoch_sha256"])
        self.assertEqual(receipt["sampler_pid"], inner["identity"]["pid"])
        self.assertEqual(receipt["sampler_wait_status"], 0)
        self.assertGreater(sum(receipt["sampler_exit_cpu"].values()) - sum(close["cpu"].values()), 60_000_000)
        self.assertGreaterEqual(sum(receipt["supervisor_pre_attestation_cpu"].values()), 0)
        self.assertEqual(child.wait(timeout=2), 0)
        self._absent(receipt["sampler_pid"])

    def test_eof_and_bad_commands_reap_exact_child_without_success_receipt(self) -> None:
        for payload in (None, b'{"command":"sample","sequence":2}\n', b'x'*1025,
                        b'{"command":"sample","sequence":1,"sequence":1}\n',
                        b'{"command":"sample","sequence":1}\n{}\n'):
            with self.subTest(payload=payload):
                child = self._child()
                pid = self._read(child)["sampler_ready"]["identity"]["pid"]
                assert child.stdin is not None
                if payload is not None:
                    child.stdin.write(payload)
                child.stdin.close()
                child.stdin = None
                stdout, stderr = child.communicate(timeout=3)
                self.assertNotIn(b'"kind":"closed"', stdout)
                self.assertEqual(child.returncode, 0 if payload is None else 1, stderr.decode())
                self._absent(pid)

    def test_abnormal_exit_after_close_never_emits_safe_accounting(self) -> None:
        child = self._child("abnormal")
        pid = self._read(child)["sampler_ready"]["identity"]["pid"]
        self._send(child, "close", 1)
        stdout, stderr = child.communicate(timeout=3)
        self.assertEqual(child.returncode, 1)
        self.assertIn(b'abnormal child exit', stderr)
        self.assertNotIn(b'"kind":"closed"', stdout)
        self._absent(pid)

    def test_lease_hang_and_output_backpressure_reap_without_closed_receipt(self) -> None:
        for mode in ("normal", "hang", "backpressure"):
            with self.subTest(mode=mode):
                child = self._child(mode)
                pid = self._read(child)["sampler_ready"]["identity"]["pid"]
                if mode != "normal":
                    self._send(child, "sample", 1)
                # Deliberately do not drain stdout until the finite lease ends.
                self.assertEqual(child.wait(timeout=3), 1)
                stdout, _ = child.communicate(timeout=3)
                self.assertNotIn(b'"kind":"closed"', stdout)
                self._absent(pid)

    def test_fixed_source_hash_and_input_byte_bounds(self) -> None:
        source = Path(sampler.__file__).read_bytes()
        loaded = supervisor.load_sampler(source, supervisor.sha256(source))
        self.assertEqual(loaded.VERSION, sampler.VERSION)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            supervisor.load_sampler(source + b"\n", supervisor.sha256(source))
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            supervisor.load_sampler(b"x" * 65537, "sha256:" + "a" * 64)


if __name__ == "__main__":
    unittest.main()
