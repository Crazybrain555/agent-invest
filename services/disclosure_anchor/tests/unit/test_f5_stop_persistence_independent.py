"""Independent F5 acceptance: first-cause latch and durable stop persistence.

Pro §5.1/§5.3/§5.4 and §7 group (1): the first public cause is latched once,
concurrently and before any slow work; a latched stop is persisted by native
disable first and then by one create-only record; every failure of either
channel (native command/readback, EACCES/ENOSPC/EIO/fsync/link, missing root)
leaves the other attempted, the cause unchanged and the result truthful.
Logging failures never skip persistence (root review R2).

The launchd channel is always a scripted runner bound to a disposable label;
file faults are injected at the ``os`` boundary for the temp control
directory only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime.worker_stop_control import (
    StopPersistenceOutcome,
    StopRecord,
    WorkerControlStore,
    require_worker_start_permitted,
)
from disclosure_anchor.application.ports.worker_stop_control import (
    PublicStopCause,
    WorkerOperationalStopError,
)
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from tests.unit._f5_stop_fixture import (
    ScriptedLaunchctl,
    cause,
    control_dir,
    disposable_label,
    durable_control,
    runtime_root,
    settings_for,
    trusted_root,
)


class _BrokenStream:
    """A stderr whose every write fails, like a closed pipe."""

    def write(self, _text: str) -> int:
        raise BrokenPipeError(errno.EPIPE, "stderr closed")

    def flush(self) -> None:
        raise BrokenPipeError(errno.EPIPE, "stderr closed")


class _Unprintable(RuntimeError):
    def __str__(self) -> str:
        raise ValueError("this exception cannot render itself")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class FirstCauseLatchTests(unittest.TestCase):
    def test_concurrent_trips_latch_one_first_cause_and_never_wait_for_persistence(self) -> None:
        hook_entered = threading.Event()
        hook_release = threading.Event()
        hooked: list[PublicStopCause] = []

        def hook(first: PublicStopCause) -> None:
            hooked.append(first)
            hook_entered.set()
            hook_release.wait(timeout=10)

        latch = InProcessWorkerStopLatch(on_first_trip=hook)
        woken: list[str] = []
        latch.add_wake_callback(lambda: woken.append("maintenance"))
        latch.add_wake_callback(lambda: woken.append("work_available"))
        causes = [cause(f"racer_{index:02d}") for index in range(16)]
        results: dict[int, bool] = {}
        barrier = threading.Barrier(len(causes))

        def race(index: int) -> None:
            barrier.wait(timeout=5)
            results[index] = latch.trip(causes[index])

        threads = [threading.Thread(target=race, args=(index,)) for index in range(len(causes))]
        for thread in threads:
            thread.start()
        self.assertTrue(hook_entered.wait(timeout=5))
        # The halt is visible while the first caller is still persisting ...
        self.assertTrue(latch.is_tripped())
        # ... and every later caller returns without waiting behind it.
        winners = []
        for index, thread in enumerate(threads):
            thread.join(timeout=0.5)
            if thread.is_alive():
                winners.append(index)
        self.assertEqual(len(winners), 1, "only the persisting first caller may still run")
        self.assertEqual(len(results), len(causes) - 1)
        self.assertFalse(any(results.values()))
        hook_release.set()
        threads[winners[0]].join(timeout=5)
        self.assertEqual(sum(results.values()), 1)
        self.assertTrue(results[winners[0]])
        self.assertIs(latch.first_cause(), causes[winners[0]])
        self.assertEqual(hooked, [causes[winners[0]]])
        self.assertEqual(sorted(woken), ["maintenance", "work_available"])
        # A plane that registers after the trip is woken immediately, once.
        latch.add_wake_callback(lambda: woken.append("late"))
        self.assertEqual(woken.count("late"), 1)
        self.assertFalse(latch.trip(cause("after_the_fact")))
        self.assertIs(latch.first_cause(), causes[winners[0]])
        self.assertEqual(len(hooked), 1)

    def test_a_non_cause_is_refused_and_latches_nothing(self) -> None:
        latch = InProcessWorkerStopLatch()
        for value in ("forbidden_tool_call", None, {"kind": "stage_fault"}):
            with self.subTest(value=value), self.assertRaises(TypeError):
                latch.trip(value)  # type: ignore[arg-type]
        self.assertFalse(latch.is_tripped())
        self.assertIsNone(latch.first_cause())
        self.assertTrue(latch.trip(cause()))

    def test_failing_callback_and_broken_stderr_never_skip_wake_or_persistence(self) -> None:
        for failure in (RuntimeError("wake failed"), _Unprintable()):
            with self.subTest(failure=type(failure).__name__):
                later: list[str] = []
                hooked: list[PublicStopCause] = []

                def failing(error: BaseException = failure) -> None:
                    raise error

                latch = InProcessWorkerStopLatch(on_first_trip=hooked.append)
                latch.add_wake_callback(failing)
                latch.add_wake_callback(lambda: later.append("woken"))
                first = cause("forbidden_tool_call")
                with mock.patch.object(sys, "stderr", _BrokenStream()):
                    self.assertTrue(latch.trip(first))
                    self.assertFalse(latch.trip(cause("second_fault")))
                self.assertEqual(later, ["woken"])
                self.assertEqual(hooked, [first])
                self.assertIs(latch.first_cause(), first)

    def test_base_exception_in_a_callback_still_persists_once_then_propagates(self) -> None:
        hooked: list[PublicStopCause] = []
        latch = InProcessWorkerStopLatch(on_first_trip=hooked.append)

        def interrupted() -> None:
            raise KeyboardInterrupt

        latch.add_wake_callback(interrupted)
        first = cause()
        with self.assertRaises(KeyboardInterrupt):
            latch.trip(first)
        self.assertEqual(hooked, [first])
        self.assertTrue(latch.is_tripped())
        self.assertFalse(latch.trip(cause("second_fault")))
        self.assertEqual(hooked, [first])

    def test_durable_control_persists_through_failing_callback_logger_and_stderr(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        launchctl = ScriptedLaunchctl(disposable_label())
        emitted: list[str] = []

        def emit(line: str) -> None:
            emitted.append(line)
            raise OSError(errno.EIO, "log device failed")

        def failing_wake() -> None:
            raise _Unprintable()

        control = durable_control(settings, launchctl=launchctl, emit=emit)
        control.add_wake_callback(failing_wake)
        first = cause("forbidden_tool_call")
        with mock.patch.object(sys, "stderr", _BrokenStream()):
            self.assertTrue(control.trip(first))
            self.assertFalse(control.trip(cause("second_fault")))
        outcome = control.persistence_outcome(timeout=5)
        assert outcome is not None
        self.assertEqual(outcome.native.status, "verified_disabled")
        self.assertEqual(outcome.marker.status, "written")
        self.assertTrue(outcome.durable)
        self.assertEqual(launchctl.verbs(), ["disable", "print-disabled"])
        self.assertIs(control.first_cause(), first)
        active = WorkerControlStore.for_settings(settings).read_active()
        assert active.record is not None
        self.assertEqual(active.record.cause, first)
        self.assertTrue(any("PUBLIC_STOP latched" in line for line in emitted))


@contextmanager
def _control_dir_faults(directory: Path, **faults: int) -> Iterator[None]:
    """Fail selected ``os`` calls only for files/directory under ``directory``.

    Keys: ``write`` (record bytes), ``fsync_file``, ``link`` (install into
    place), ``fsync_dir`` (the control directory entry). Values are errno.
    """

    real_open, real_close = os.open, os.close
    real_write, real_fsync, real_link = os.write, os.fsync, os.link
    tracked: dict[int, str] = {}

    def ours(path: object) -> bool:
        candidate = Path(os.fsdecode(path))  # type: ignore[arg-type]
        return candidate == directory or directory in candidate.parents

    def fail(key: str) -> None:
        number = faults[key]
        raise OSError(number, os.strerror(number))

    def fake_open(path: object, flags: int, mode: int = 0o777, **kwargs: object) -> int:
        fd = real_open(path, flags, mode, **kwargs)  # type: ignore[arg-type]
        if ours(path):
            tracked[fd] = "dir" if Path(os.fsdecode(path)) == directory else "file"  # type: ignore[arg-type]
        return fd

    def fake_close(fd: int) -> None:
        tracked.pop(fd, None)
        real_close(fd)

    def fake_write(fd: int, data: bytes) -> int:
        if tracked.get(fd) == "file" and "write" in faults:
            fail("write")
        return real_write(fd, data)

    def fake_fsync(fd: int) -> None:
        kind = tracked.get(fd)
        if kind == "file" and "fsync_file" in faults:
            fail("fsync_file")
        if kind == "dir" and "fsync_dir" in faults:
            fail("fsync_dir")
        real_fsync(fd)

    def fake_link(source: object, target: object, **kwargs: object) -> None:
        if ours(target) and "link" in faults:
            fail("link")
        real_link(source, target, **kwargs)  # type: ignore[arg-type]

    with (
        mock.patch("os.open", fake_open),
        mock.patch("os.close", fake_close),
        mock.patch("os.write", fake_write),
        mock.patch("os.fsync", fake_fsync),
        mock.patch("os.link", fake_link),
    ):
        yield


class DurablePersistenceMatrixTests(unittest.TestCase):
    def _trip(
        self,
        *,
        launchctl: ScriptedLaunchctl | None,
        root: Path | None = None,
        faults: dict[str, int] | None = None,
        reason: str = "forbidden_tool_call",
    ) -> tuple[StopPersistenceOutcome, list[str], PublicStopCause, Path]:
        root = trusted_root(self) if root is None else root
        settings = settings_for(root)
        lines: list[str] = []
        control = durable_control(settings, launchctl=launchctl, lines=lines)
        first = cause(reason)
        with _control_dir_faults(control_dir(root), **(faults or {})):
            self.assertTrue(control.trip(first))
        outcome = control.persistence_outcome(timeout=5)
        assert outcome is not None
        self.assertIs(control.first_cause(), first)
        return outcome, lines, first, root

    def _assert_no_partial(self, root: Path) -> None:
        directory = control_dir(root)
        if directory.exists():
            self.assertEqual([p.name for p in directory.iterdir() if p.name.endswith(".partial")], [])

    def test_native_first_then_create_only_record_with_owner_only_modes(self) -> None:
        label = disposable_label()
        seen_before_disable: list[bool] = []
        root = trusted_root(self)
        active_path = control_dir(root) / "worker-circuit-stop.json"

        def before(verb: str) -> None:
            if verb == "disable":
                seen_before_disable.append(active_path.exists())

        launchctl = ScriptedLaunchctl(label, before=before)
        outcome, lines, first, _ = self._trip(launchctl=launchctl, root=root)

        self.assertEqual(seen_before_disable, [False], "native disable precedes the record")
        self.assertEqual(launchctl.verbs(), ["disable", "print-disabled"])
        self.assertEqual(outcome.native.status, "verified_disabled")
        self.assertEqual(outcome.native.service_target, f"gui/{os.getuid()}/{label}")
        self.assertEqual((outcome.marker.status, outcome.marker.detail), ("written", "installed"))
        self.assertTrue(outcome.durable)
        raw = active_path.read_bytes()
        self.assertEqual(outcome.marker.sha256, _sha256(raw))
        self.assertEqual(raw, outcome.record_bytes)
        self.assertEqual(stat.S_IMODE(active_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(control_dir(root).stat().st_mode), 0o700)
        record = StopRecord.decode(raw)
        self.assertEqual(record.record_origin, "automatic_fault")
        self.assertEqual(record.cause, first)
        self.assertEqual(record.native_disable, outcome.native)
        self.assertEqual(sorted(p.name for p in control_dir(root).iterdir()), ["worker-circuit-stop.json"])
        self.assertFalse(any("STOP_PERSISTENCE_FAILED" in line for line in lines))
        self.assertTrue(any(f"active={outcome.marker.sha256}" in line for line in lines))
        # A persisted stop refuses the next start by its exact hash.
        with self.assertRaises(WorkerOperationalStopError) as refused:
            require_worker_start_permitted(settings_for(root))
        self.assertEqual((refused.exception.state, refused.exception.active_sha256),
                         ("PUBLIC_STOP", outcome.marker.sha256))

    def test_every_native_outcome_leaves_the_record_attempted_and_truthful(self) -> None:
        timeout = subprocess.TimeoutExpired(["launchctl"], 10)
        cases = (
            ("disable nonzero, readback enabled", dict(disabled="enabled", disable_takes_effect=False,
                                                       failures={"disable": 5}),
             ("failed", "readback_enabled")),
            ("disable ok, label still unlisted", dict(disable_takes_effect=False),
             ("failed", "readback_enabled")),
            ("readback times out", dict(failures={"print-disabled": timeout}),
             ("unknown", "print_disabled_timeout")),
            ("readback exits nonzero", dict(failures={"print-disabled": 1}),
             ("unknown", "print_disabled_failed")),
            ("readback without the override table", dict(table=False),
             ("unknown", "print_disabled_unrecognized")),
            ("readback with an unknown value", dict(disabled="maybe", disable_takes_effect=False),
             ("unknown", "print_disabled_unrecognized")),
            ("disable times out but readback proves disabled",
             dict(disabled="disabled", failures={"disable": timeout}),
             ("verified_disabled", "disable_timeout")),
            ("launchctl cannot be executed", dict(failures={
                "disable": FileNotFoundError(errno.ENOENT, "no launchctl"),
                "print-disabled": FileNotFoundError(errno.ENOENT, "no launchctl")}),
             ("unknown", "print_disabled_enoent")),
        )
        for name, script, (status, detail) in cases:
            with self.subTest(case=name):
                launchctl = ScriptedLaunchctl(disposable_label(), **script)  # type: ignore[arg-type]
                outcome, lines, first, root = self._trip(launchctl=launchctl)
                self.assertEqual((outcome.native.status, outcome.native.detail), (status, detail))
                self.assertEqual(launchctl.verbs(), ["disable", "print-disabled"])
                self.assertNotIn("enable", launchctl.verbs())
                self.assertEqual(outcome.marker.status, "written")
                self.assertTrue(outcome.durable)
                record = StopRecord.decode((control_dir(root) / "worker-circuit-stop.json").read_bytes())
                self.assertEqual(record.cause, first)
                self.assertEqual(record.native_disable, outcome.native)
                self.assertFalse(any("STOP_PERSISTENCE_FAILED" in line for line in lines))
        with self.subTest(case="no supervised launchd job"):
            outcome, _, first, root = self._trip(launchctl=None)
            self.assertEqual(outcome.native.status, "failed")
            self.assertEqual(outcome.native.service_target, None)
            self.assertEqual(outcome.marker.status, "written")

    def test_every_record_failure_keeps_the_cause_and_hash_bound_evidence(self) -> None:
        cases = (
            ("write ENOSPC", {"write": errno.ENOSPC}, "write_enospc", False),
            ("write EIO", {"write": errno.EIO}, "write_eio", False),
            ("file fsync EIO", {"fsync_file": errno.EIO}, "write_eio", False),
            ("install link EIO", {"link": errno.EIO}, "write_eio", False),
            ("directory fsync EIO after install", {"fsync_dir": errno.EIO},
             "installed_dir_fsync_eio", True),
        )
        for name, faults, detail, installed in cases:
            with self.subTest(case=name):
                launchctl = ScriptedLaunchctl(disposable_label())
                outcome, lines, first, root = self._trip(launchctl=launchctl, faults=faults)
                self.assertEqual((outcome.marker.status, outcome.marker.detail), ("failed", detail))
                self.assertEqual(outcome.native.status, "verified_disabled")
                self.assertTrue(outcome.durable, "the native channel alone is durable")
                self._assert_no_partial(root)
                active_path = control_dir(root) / "worker-circuit-stop.json"
                self.assertEqual(active_path.exists(), installed)
                assert outcome.record_bytes is not None
                if installed:
                    self.assertEqual(active_path.read_bytes(), outcome.record_bytes)
                    self.assertEqual(outcome.marker.sha256, _sha256(outcome.record_bytes))
                    with self.assertRaises(WorkerOperationalStopError) as refused:
                        require_worker_start_permitted(settings_for(root))
                    self.assertEqual(refused.exception.state, "PUBLIC_STOP")
                failed = [line for line in lines if "STOP_PERSISTENCE_FAILED" in line]
                self.assertEqual(len(failed), 1)
                self.assertIn("native-only", failed[0])
                evidence = [line for line in lines if line.startswith("[worker-control] STOP_RECORD ")]
                self.assertEqual(len(evidence), 1)
                printed = (evidence[0].removeprefix("[worker-control] STOP_RECORD ") + "\n").encode("ascii")
                self.assertEqual(printed, outcome.record_bytes)
                self.assertEqual(StopRecord.decode(printed).cause, first)

    def test_untrusted_or_missing_storage_is_never_created_or_trusted(self) -> None:
        with self.subTest(case="runtime root missing"):
            root = trusted_root(self)
            runtime_root(root).rmdir()
            outcome, lines, _, _ = self._trip(launchctl=ScriptedLaunchctl(disposable_label()), root=root)
            self.assertEqual((outcome.marker.status, outcome.marker.detail),
                             ("failed", "runtime_root_missing"))
            self.assertFalse(runtime_root(root).exists(), "a missing root is never created")
            self.assertEqual(outcome.native.status, "verified_disabled")
        if os.geteuid() != 0:
            with self.subTest(case="control directory cannot be created"):
                root = trusted_root(self)
                runtime_root(root).chmod(0o500)
                self.addCleanup(runtime_root(root).chmod, 0o700)
                outcome, _, _, _ = self._trip(launchctl=ScriptedLaunchctl(disposable_label()), root=root)
                self.assertEqual((outcome.marker.status, outcome.marker.detail),
                                 ("failed", "control_dir_create_eacces"))
        with self.subTest(case="both channels fail"):
            root = trusted_root(self)
            runtime_root(root).rmdir()
            outcome, lines, first, _ = self._trip(launchctl=None, root=root)
            self.assertFalse(outcome.durable)
            self.assertIn("no durable channel", "\n".join(lines))
            evidence = [line for line in lines if " STOP_RECORD " in line]
            self.assertEqual(len(evidence), 1)
            self.assertEqual(
                StopRecord.decode((evidence[0].split(" STOP_RECORD ", 1)[1] + "\n").encode()).cause,
                first,
            )

    def test_an_existing_first_cause_record_is_preserved_byte_for_byte(self) -> None:
        root = trusted_root(self)
        first_outcome, _, first, _ = self._trip(launchctl=ScriptedLaunchctl(disposable_label()),
                                                 root=root, reason="forbidden_tool_call")
        active_path = control_dir(root) / "worker-circuit-stop.json"
        original = active_path.read_bytes()
        # A later process (fresh latch) latches an unrelated fault.
        second_launchctl = ScriptedLaunchctl(disposable_label())
        second, _, _, _ = self._trip(launchctl=second_launchctl, root=root, reason="retry_stage_stuck")
        self.assertEqual((second.marker.status, second.marker.detail), ("existing", "first_cause_preserved"))
        self.assertEqual(second.marker.sha256, first_outcome.marker.sha256)
        self.assertTrue(second.durable)
        self.assertEqual(second_launchctl.verbs(), ["disable", "print-disabled"])
        self.assertEqual(active_path.read_bytes(), original)
        self.assertEqual(StopRecord.decode(original).cause, first)


if __name__ == "__main__":
    unittest.main()
