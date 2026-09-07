from __future__ import annotations

import json
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.windows import linux_resident_host_sampler as sampler
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    ApiProcessObservation, HostCgroupObservation,
)


def _stat(pid: int, *, start: int = 100, user: int = 10) -> str:
    fields = ["0"] * 50
    fields[0] = "S"
    fields[11] = str(user)
    fields[12] = "3"
    fields[19] = str(start)
    return f"{pid} (worker ) with spaces)) " + " ".join(fields) + "\n"


class LinuxResidentHostSamplerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        self.cg = self.root / "cgroup"
        self.parent = self.cg / "docker"
        self.parent.mkdir(parents=True)
        boot = self.proc / "sys/kernel/random"
        boot.mkdir(parents=True)
        (boot / "boot_id").write_text("00000000-0000-4000-8000-000000000001\n")
        (self.proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 600 kB\nMemFree: 20 kB\n")
        members = {}
        for pid, role in enumerate(("api", "inference", "proxy"), 10):
            path = self.proc / str(pid)
            path.mkdir()
            (path / "stat").write_text(_stat(pid))
            (path / "cgroup").write_text(f"0::/docker/{role}\n")
            (path / "status").write_text("VmRSS: 100 kB\nVmHWM: 130 kB\nThreads: 4\n")
            members[role] = {"pid": pid, "start_ticks": 100, "cgroup": f"/docker/{role}"}
        for name, text in {
            "memory.current": "24000\n", "memory.max": "max\n",
            "memory.stat": "anon 100\nfile 200\nshmem 30\nslab 40\nnew_counter 9\n",
            "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n",
            "cpu.stat": "usage_usec 100\nuser_usec 60\nsystem_usec 40\nthrottled_usec 2\nnr_throttled 1\n",
        }.items():
            (self.parent / name).write_text(text)
        for kind in ("memory", "cpu", "io"):
            (self.parent / (kind + ".pressure")).write_text("some avg10=0.00 avg60=1.00 avg300=2.00 total=10\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=2\n")
        stat = self.parent.stat()
        self.config = {
            "boot_id": "00000000-0000-4000-8000-000000000001", "members": members,
            "parent_device": stat.st_dev, "parent_inode": stat.st_ino,
            "lease_ms": 1000, "lifetime_ms": 3000,
        }

    def _open(self) -> sampler.HostSampler:
        value = sampler.HostSampler(self.config, proc_root=self.proc, cgroup_root=self.cg)
        self.addCleanup(value.close)
        return value

    def test_real_contract_projection_units_unknown_keys_and_roundtrip(self) -> None:
        value = self._open()
        value.tick_hz = 250
        result = value.sample()
        api = ApiProcessObservation.model_validate(result["api_process"]).values
        host = HostCgroupObservation.model_validate(result["host_cgroup"]).values
        assert api is not None and host is not None
        self.assertEqual(api.cpu_user_ns_total, 40_000_000)
        self.assertEqual(api.rss_bytes, 102400)
        self.assertEqual(api.rss_hwm_bytes, 133120)
        self.assertEqual(host.docker_vm_memory_available_bytes, 614400)
        self.assertEqual(host.cpu_stat.throttled_ns_total, 2000)
        self.assertIsNone(host.memory_max_bytes)
        self.assertEqual(sampler.decode(sampler.canonical(result)), result)
        self.assertEqual(value.sample(), result)

    def test_missing_metrics_never_default_zero_and_pressure_missing_full_explicit(self) -> None:
        value = self._open()
        (self.parent / "cpu.stat").write_text("usage_usec 100\nuser_usec 60\nsystem_usec 40\n")
        observed = value.sample()
        self.assertEqual(observed["host_cgroup"]["status"], "unsupported")
        self.assertIsNone(observed["host_cgroup"]["values"])
        self.assertEqual(observed["api_process"]["status"], "supported")
        parsed = sampler.pressure("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        self.assertEqual(parsed["full_status"], "unsupported")
        self.assertIsNone(parsed["full"])

    def test_pid_reuse_cgroup_migration_and_boot_drift_reject(self) -> None:
        value = self._open()
        changes = (
            (self.proc / "10/stat", _stat(10, start=101)),
            (self.proc / "10/cgroup", "0::/docker/other\n"),
            (self.proc / "sys/kernel/random/boot_id", "another-boot\n"),
        )
        for path, changed in changes:
            original = path.read_text()
            path.write_text(changed)
            with self.subTest(path=path.name), self.assertRaisesRegex(ValueError, "identity drift"):
                value.sample()
            path.write_text(original)

    def test_threads_missing_is_unsupported_but_malformed_or_duplicate_is_visible(self) -> None:
        value = self._open()
        status = self.proc / "10/status"
        body = "VmRSS: 100 kB\nVmHWM: 130 kB\n"
        status.write_text(body)
        self.assertEqual(value.sample()["api_process"]["status"], "unsupported")
        for field in ("Threads:\n", "Threads: 1\nThreads: 1\n", "Threads: nope\n"):
            status.write_text(body + field)
            with self.assertRaises(ValueError):
                value.sample()

    def test_parent_replacement_and_symlink_file_reject(self) -> None:
        value = self._open()
        original = self.parent / "memory.max"
        original.unlink()
        original.symlink_to(self.parent / "memory.current")
        with self.assertRaises(OSError):
            value.sample()
        self.parent.rename(self.cg / "old-parent")
        self.parent.mkdir()
        with self.assertRaisesRegex(ValueError, "directory identity drift"):
            value.sample()

    def test_counter_regression_and_invalid_kernel_numbers_reject(self) -> None:
        value = self._open()
        value.sample()
        (self.proc / "10/stat").write_text(_stat(10, user=9))
        with self.assertRaisesRegex(ValueError, "counter regression"):
            value.sample()
        for text in ("a -1\n", "a 1\na 1\n", "a 0.0\n"):
            with self.assertRaises(ValueError):
                sampler.counters(text)
        with self.assertRaises(ValueError):
            sampler.pressure("some avg10=nan avg60=0 avg300=0 total=0\n")
        with self.assertRaises(ValueError):
            sampler.decode(b'{"command":"sample","sequence":1,"sequence":1}')

    def test_kernel_file_byte_bound_and_process_terminal_reject(self) -> None:
        value = self._open()
        (self.parent / "memory.stat").write_text("a" * (sampler.MAX_FILE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "exceeded bound"):
            value.sample()
        with self.assertRaisesRegex(ValueError, "terminal"):
            sampler.proc_stat(_stat(10).replace(") S ", ") Z "), 10)
        with self.assertRaisesRegex(ValueError, "exceeded bound"):
            sampler.read_kernel_file(self.parent / "memory.stat")

    def _child(self, *, lease: int = 1000, lifetime: int = 3000, large_sample: bool = False,
               blocking_sample: bool = False, delayed_fuse: bool = False) -> subprocess.Popen[bytes]:
        # Inject only the kernel source for deterministic protocol/lifetime
        # tests. The production main has no fake mode or configurable roots.
        config = {**self.config, "lease_ms": lease, "lifetime_ms": lifetime}
        code = (
            "from scripts.windows import linux_resident_host_sampler as m\n"
            "from pathlib import Path\n"
            "real=m.HostSampler\n"
            + ("real.sample=lambda s:{'bounded_backpressure_probe':'x'*60000}\n" if large_sample else "") +
            ("real.sample=lambda s:__import__('time').sleep(30)\n" if blocking_sample else "") +
            ("original_timer=m.signal.setitimer\n"
             "m.signal.setitimer=lambda kind,seconds:original_timer(kind,seconds+0.05)\n"
             if delayed_fuse else "") +
            f"m.HostSampler=lambda c:real(c,proc_root=Path({str(self.proc)!r}),cgroup_root=Path({str(self.cg)!r}))\n"
            # /proc is unavailable on macOS; replace only startup identity
            # queries while keeping real timers, process and nonblocking stdio.
            "m.host_namespaces=lambda:{'pid':'fixture-pid','cgroup':'fixture-cgroup'}\n"
            "m.self_process_stat=lambda:{'start_ticks':1}\n"
            f"m.run({config!r},'sha256:'+'a'*64)\n"
        )
        child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup() -> None:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)
        self.addCleanup(cleanup)
        return child

    def _read(self, child: subprocess.Popen[bytes]) -> dict:
        assert child.stdout is not None
        self.assertTrue(select.select([child.stdout], [], [], 2)[0], "child output timeout")
        line = child.stdout.readline()
        if not line:
            assert child.stderr is not None
            self.fail(child.stderr.read().decode())
        return json.loads(line)

    def test_resident_real_stdio_fragment_sequence_close_and_eof(self) -> None:
        child = self._child()
        ready = self._read(child)
        self.assertEqual(ready["kind"], "ready")
        assert child.stdin is not None
        child.stdin.write(b'{"command":"sam')
        child.stdin.flush()
        child.stdin.write(b'ple","sequence":1}\n')
        child.stdin.flush()
        result = self._read(child)
        self.assertEqual(result["epoch_sha256"], ready["epoch_sha256"])
        self.assertEqual(result["sequence"], 1)
        child.stdin.write(b'{"command":"close","sequence":2}\n')
        child.stdin.flush()
        self.assertEqual(self._read(child)["kind"], "close")
        self.assertEqual(child.wait(timeout=2), 0)
        second = self._child()
        self._read(second)
        assert second.stdin is not None
        second.stdin.close()
        second.stdin = None
        self.assertEqual(second.wait(timeout=2), 0)

    def test_real_lease_and_hard_lifetime_are_terminal(self) -> None:
        child = self._child()
        self._read(child)
        self._assert_deadline_exit(child, timeout=2)
        child = self._child(lease=1000, lifetime=1500)
        self._read(child)
        assert child.stdin is not None
        start = time.monotonic()
        for sequence in range(1, 4):
            child.stdin.write(sampler.canonical({"command": "sample", "sequence": sequence}) + b"\n")
            child.stdin.flush()
            self._read(child)
            time.sleep(0.4)
        self._assert_deadline_exit(child, timeout=1)
        self.assertLess(time.monotonic() - start, 2)

    def _assert_deadline_exit(self, child: subprocess.Popen[bytes], *, timeout: float) -> None:
        code = child.wait(timeout=timeout)
        assert child.stderr is not None
        stderr = child.stderr.read().decode()
        if code == -signal.SIGALRM:
            return
        self.assertEqual(code, 1, stderr)
        self.assertTrue(stderr.startswith("Traceback (most recent call last):"), stderr)
        self.assertEqual(stderr.splitlines()[-1], "TimeoutError: lease expired", stderr)

    def test_explicit_timeout_and_native_blocking_fuse_both_remain_terminal(self) -> None:
        # Test-only timer offset deterministically chooses the legal select
        # deadline path. Production uses the original simultaneous deadline.
        explicit = self._child(delayed_fuse=True)
        self._read(explicit)
        self.assertEqual(explicit.wait(timeout=2), 1)
        self._assert_deadline_exit(explicit, timeout=1)
        blocked = self._child(blocking_sample=True)
        self._read(blocked)
        assert blocked.stdin is not None
        blocked.stdin.write(b'{"command":"sample","sequence":1}\n')
        blocked.stdin.flush()
        self.assertEqual(blocked.wait(timeout=2), -signal.SIGALRM)

    def test_stdout_backpressure_cannot_hold_linux_process_past_lease(self) -> None:
        child = self._child(large_sample=True)
        self._read(child)
        assert child.stdin is not None
        child.stdin.write(b'{"command":"sample","sequence":1}\n')
        child.stdin.flush()
        # Do not drain stdout: a large, bounded frame fills the kernel pipe.
        # Either default-action alarm or explicit output deadline is terminal.
        self.assertNotEqual(child.wait(timeout=2), 0)

    def test_real_protocol_overlong_duplicate_and_pipelining_fail_visible(self) -> None:
        for payload in (
            b"x" * 1025,
            b'{"command":"sample","sequence":1,"sequence":1}\n',
            b'{"command":"sample","sequence":1}\n{}\n',
            b'{"command":"sample","sequence":2}\n',
        ):
            child = self._child()
            self._read(child)
            assert child.stdin is not None
            child.stdin.write(payload)
            child.stdin.flush()
            self.assertNotEqual(child.wait(timeout=2), 0)
            assert child.stderr is not None
            self.assertIn(b"ValueError", child.stderr.read())


if __name__ == "__main__":
    unittest.main()
