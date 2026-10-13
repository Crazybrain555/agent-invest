"""Independent F5 acceptance (r4): semantic child cleanup never replaces the call's outcome.

F-1: ``codex_cli._run_process`` (shared by the Codex and Claude adapters) let a
cleanup ``PermissionError`` replace a shutdown cancellation, and both adapters
read it as ``executable_unavailable``, i.e. a fallback and a degraded
publication. On Darwin ``killpg`` answers EPERM, not ESRCH, for a group whose
leader has exited but is not reaped. Checked only through real disposable
children and the adapters' outcomes:

* the natural zombie-group race is proven gone (leader reaped, then ESRCH) and
  stays a silent, retry-neutral cancellation;
* a member that really cannot be signalled never replaces the original
  cancellation, timeout or guard fault. Cleanup is bounded, reported on stderr
  with pid and type names only, and noted ``closure=unproven``. A leader that
  is still running stays owned by the shutdown sweep;
* the leader and its same-group descendants are closed before the call
  returns, and a member that outlives its reaped leader is never claimed closed;
* a sweep racing registration still cancels before the prompt is handed over;
  the sweep never raises and reports only children that are still running.

Failures are injected only at ``os.killpg`` for one child's own group. Nothing
else is signalled, and every child and descendant is killed and reaped in
cleanup.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, suppress
import errno
import io
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from disclosure_anchor.adapters.semantics import codex_cli
from disclosure_anchor.adapters.semantics.claude_cli import ClaudeCliSemanticAdjudicator
from disclosure_anchor.application.ports.semantic_routes import SemanticRouteAdjudicatorError
from disclosure_anchor.application.ports.staged_execution import StageNote
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseGuard, StageLeaseLost
from tests.unit.test_semantic_adjudication import _batch


_REAL_KILLPG = os.killpg
# Reads the whole prompt, writes a truncated answer and keeps running.
_ANSWERING = ("import sys,time; sys.stdin.read(); "
              "sys.stdout.write('{\"type\":\"result\",\"structured_output\":{'); sys.stdout.flush(); "
              "time.sleep(60)")
_OWNER_DETAIL = re.compile(
    r"semantic child group (?P<pid>\d+) cleanup after (?P<failure>cancelled|timeout|error:[A-Za-z_]+) "
    r"failed \((?P<types>[A-Za-z_]+(?: from [A-Za-z_]+)?)\); not proven stopped"
)
_SWEEP_LINE = re.compile(
    r"\[semantic-process\] semantic child group (?P<pid>\d+) could not be signalled "
    r"\((?P<signal>SIGTERM|SIGKILL): (?P<type>[A-Za-z_]+)\) and is still running"
)


def _eperm() -> PermissionError:
    return PermissionError(errno.EPERM, os.strerror(errno.EPERM))


def _running(pid: int) -> bool:
    """Whether this process's unreaped child ``pid`` has not exited yet."""

    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None
    except ChildProcessError:
        return False


def _exited_unreaped(pid: int) -> None:
    deadline = time.monotonic() + 10
    while _running(pid):
        if time.monotonic() > deadline:
            raise AssertionError(f"child {pid} did not exit")
        time.sleep(0.005)


def _group_gone(pgid: int) -> bool:
    """Probe with signal 0 until the group no longer exists (zombies awaiting launchd answer EPERM)."""

    deadline = time.monotonic() + 5
    while True:
        try:
            _REAL_KILLPG(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)


def _guard(notes: _Notes) -> StageLeaseGuard:
    return StageLeaseGuard(time.monotonic() + 60, threading.Event(), time.monotonic, observer=notes)


class _Notes:
    def __init__(self) -> None:
        self.records: list[StageNote] = []
        self.failures: list[BaseException] = []

    def note(self, record: StageNote) -> None:
        self.records.append(record)

    def record_failure(self, error: BaseException) -> None:
        self.failures.append(error)

    def ended(self) -> dict[str, int | str | None]:
        (ended,) = [dict(record.scalars) for record in self.records if record.kind == "process_ended"]
        return ended


class _FullStream(io.StringIO):
    def write(self, text: str) -> int:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


class _ChildBoundary(unittest.TestCase):
    """Real children through the real adapter; ``os.killpg`` recorded, per-group policies."""

    def setUp(self) -> None:
        codex_cli._SEMANTIC_SHUTDOWN_REQUESTED.clear()
        self.addCleanup(codex_cli._SEMANTIC_SHUTDOWN_REQUESTED.clear)
        self.children: list[subprocess.Popen[str]] = []
        self.descendants: list[tuple[int, int]] = []
        self.spawned = threading.Event()
        self.on_spawn: Callable[[subprocess.Popen[str]], None] | None = None
        self.sigterm_blocked_at_spawn = False
        self.policies: dict[int, Callable[[int, int], None]] = {}
        self.signals: list[tuple[int, int, str]] = []
        self.addCleanup(self._release)
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):  # type: ignore[no-untyped-def]
            # A child spawned while this thread blocks SIGTERM starts with it blocked.
            mask = {signal.SIGTERM} if self.sigterm_blocked_at_spawn else set()
            previous = signal.pthread_sigmask(signal.SIG_BLOCK, mask)
            try:
                process = real_popen(*args, **kwargs)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            self.children.append(process)
            if self.on_spawn is not None:
                self.on_spawn(process)
            self.spawned.set()
            return process

        def killpg(pgid: int, signum: int) -> None:
            try:
                self.policies.get(pgid, _REAL_KILLPG)(pgid, signum)
            except OSError as exc:
                self.signals.append((pgid, signum, errno.errorcode.get(exc.errno or 0, "?")))
                raise
            self.signals.append((pgid, signum, "ok"))

        for patcher in (mock.patch.object(codex_cli.subprocess, "Popen", side_effect=popen),
                        mock.patch.object(codex_cli.os, "killpg", side_effect=killpg)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _release(self) -> None:
        for pid, pgid in self.descendants:
            with suppress(OSError):
                if os.getpgid(pid) == pgid:
                    os.kill(pid, signal.SIGKILL)
        for process in self.children:
            if process.poll() is None:
                with suppress(OSError):
                    _REAL_KILLPG(process.pid, signal.SIGKILL)
            with suppress(Exception):
                process.communicate(timeout=5)
            codex_cli._unregister_process(process)

    def _unsignalable(self, process: subprocess.Popen[str]) -> None:
        def refuse(_pgid: int, _signum: int) -> None:
            raise _eperm()

        self.policies[process.pid] = refuse

    def _after_spawn(self, delay: float, action: Callable[[], object]) -> list[object]:
        results: list[object] = []

        def run() -> None:
            if self.spawned.wait(timeout=10):
                time.sleep(delay)
                try:
                    results.append(action())
                except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                    results.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        return results

    def _adjudicate(self, child: str, guard: StageLeaseGuard | None, *,
                    timeout_seconds: int = 30) -> tuple[BaseException, float]:
        real_run = codex_cli._run_process

        def run_child(*, args, prompt, env, timeout_seconds, stage_guard=None):  # type: ignore[no-untyped-def]
            del args, env
            return real_run(args=[sys.executable, "-c", child], prompt=prompt, env={},
                            timeout_seconds=timeout_seconds,
                            **({} if stage_guard is None else {"stage_guard": stage_guard}))

        adapter = ClaudeCliSemanticAdjudicator(executable=Path("/not-run/claude"), timeout_seconds=timeout_seconds)
        outcome: list[tuple[BaseException | None, float]] = []

        def call() -> None:
            started = time.monotonic()
            try:
                if guard is None:
                    adapter.adjudicate_with_result(_batch())
                else:
                    adapter.adjudicate_with_result(_batch(), stage_guard=guard)
            except BaseException as exc:  # noqa: BLE001 - the outcome under test
                outcome.append((exc, time.monotonic() - started))
            else:
                outcome.append((None, time.monotonic() - started))

        with mock.patch.object(codex_cli, "_run_process", side_effect=run_child):
            caller = threading.Thread(target=call, daemon=True)
            caller.start()
            caller.join(25)
        self.assertFalse(caller.is_alive(), "the model call did not return within its bound")
        error, elapsed = outcome[0]
        assert error is not None, "the model call unexpectedly succeeded"
        return error, elapsed

    def _assert_retry_neutral_cancel(self, error: BaseException) -> BaseException:
        assert isinstance(error, SemanticRouteAdjudicatorError), repr(error)
        self.assertEqual((error.reason_code, error.retryable), ("cancelled", True),
                         "never executable_unavailable, a fallback or a degraded publication")
        original = error.__cause__
        self.assertIsInstance(original, codex_cli._SemanticProcessCancelled)
        assert original is not None
        return original

    def _unproven_note(self, original: BaseException, *, pid: int, failure: str) -> str:
        (note,) = getattr(original, "__notes__", ())
        match = _OWNER_DETAIL.fullmatch(note)
        assert match is not None, note
        self.assertEqual((int(match["pid"]), match["failure"]), (pid, failure))
        self.assertIn("PermissionError", match["types"])
        return note


class NaturalZombieRaceTests(_ChildBoundary):
    def test_a_zombie_only_group_is_proven_gone_and_the_cancellation_stays_silent(self) -> None:
        natural: list[str] = []

        def zombie_first(pgid: int, signum: int) -> None:
            # The TERM handler's kill won the race: when the stage's cleanup
            # signals, the leader has exited and nobody has reaped it yet.
            if not natural:
                os.kill(pgid, signal.SIGKILL)
                _exited_unreaped(pgid)
                try:
                    _REAL_KILLPG(pgid, signum)
                except OSError as exc:
                    natural.append(errno.errorcode.get(exc.errno or 0, "?"))
                    raise
                natural.append("ok")
                return
            _REAL_KILLPG(pgid, signum)

        self.on_spawn = lambda process: self.policies.__setitem__(process.pid, zombie_first)
        notes, stderr = _Notes(), io.StringIO()
        with redirect_stderr(stderr):
            self._after_spawn(0.3, codex_cli._SEMANTIC_SHUTDOWN_REQUESTED.set)
            error, _elapsed = self._adjudicate(_ANSWERING, _guard(notes))
        if natural != ["EPERM"]:
            self.skipTest(f"this host answers {natural} for a zombie-only group; the race needs EPERM")
        original = self._assert_retry_neutral_cancel(error)
        self.assertFalse(getattr(original, "__notes__", None), "no cleanup failure to note")
        self.assertEqual(stderr.getvalue(), "")
        ended = notes.ended()
        self.assertEqual(ended.get("reason"), "cancelled")
        self.assertNotIn("closure", ended)
        (process,) = self.children
        self.assertEqual(process.returncode, -signal.SIGKILL, "the proof reaped this call's own leader")
        later = [outcome for pgid, _signum, outcome in self.signals if pgid == process.pid][1:]
        self.assertIn("ESRCH", later, "a probe answered ESRCH after the leader was reaped")
        self.assertNotIn("ok", later, "nothing was delivered to the group after it ended")
        self.assertTrue(_group_gone(process.pid))


class UnsignalableChildTests(_ChildBoundary):
    def test_a_live_child_that_cannot_be_signalled_keeps_the_cancellation_bounded_visible_and_owned(self) -> None:
        self.on_spawn = self._unsignalable
        notes, stderr = _Notes(), io.StringIO()
        with redirect_stderr(stderr):
            # The worker's TERM handler runs the real sweep while the stage waits on the model.
            swept = self._after_spawn(
                0.3, lambda: codex_cli.terminate_active_semantic_processes(grace_seconds=0.2))
            error, elapsed = self._adjudicate(_ANSWERING, _guard(notes))
            # After the call gave up, the exit path's sweep still owns the child.
            again = codex_cli.terminate_active_semantic_processes(grace_seconds=0.2)
        (process,) = self.children
        self.assertEqual(swept, [1], "the sweep never raises and counts the owned child")
        original = self._assert_retry_neutral_cancel(error)
        note = self._unproven_note(original, pid=process.pid, failure="cancelled")
        self.assertLess(elapsed, 10.0, "cleanup of a child it cannot stop is bounded")
        lines = stderr.getvalue().splitlines()
        self.assertEqual(lines.count(f"[semantic-process] {note}"), 1)
        sweeps = [match for match in map(_SWEEP_LINE.fullmatch, lines) if match]
        self.assertEqual([(int(match["pid"]), match["type"]) for match in sweeps],
                         [(process.pid, "PermissionError")] * 2)
        self.assertEqual(len(lines), 3, lines)
        ended = notes.ended()
        self.assertEqual((ended.get("reason"), ended.get("closure"), ended.get("returncode")),
                         ("cancelled", "unproven", None))
        self.assertIsNone(process.poll(), "the report is truthful: the child is still running")
        self.assertEqual(again, 1, "the still-running child stays owned by the shutdown sweep")

    def test_a_provider_timeout_with_an_unstoppable_child_stays_a_timeout(self) -> None:
        self.on_spawn = self._unsignalable
        notes, stderr = _Notes(), io.StringIO()
        with redirect_stderr(stderr):
            error, elapsed = self._adjudicate(_ANSWERING, _guard(notes), timeout_seconds=1)
            owned = codex_cli.terminate_active_semantic_processes(grace_seconds=0.2)
        (process,) = self.children
        assert isinstance(error, SemanticRouteAdjudicatorError), repr(error)
        self.assertEqual((error.reason_code, error.retryable), ("timeout", True),
                         "the provider's own timeout stays the cause, never executable_unavailable")
        self.assertIsInstance(error.__cause__, subprocess.TimeoutExpired)
        assert error.__cause__ is not None
        note = self._unproven_note(error.__cause__, pid=process.pid, failure="timeout")
        self.assertLess(elapsed, 12.0)
        lines = stderr.getvalue().splitlines()
        self.assertEqual(lines[0], f"[semantic-process] {note}")
        self.assertEqual([int(match["pid"]) for match in map(_SWEEP_LINE.fullmatch, lines[1:]) if match],
                         [process.pid])
        ended = notes.ended()
        self.assertEqual((ended.get("reason"), ended.get("closure")), ("timeout", "unproven"))
        self.assertIsNone(process.poll())
        self.assertEqual(owned, 1, "a live child the call could not stop stays owned")

    def test_a_guard_fault_keeps_its_type_and_provenance_even_when_stderr_is_unwritable(self) -> None:
        self.on_spawn = self._unsignalable
        notes = _Notes()
        guard = _guard(notes)
        with redirect_stderr(_FullStream()):
            self._after_spawn(0.3, lambda: guard.revoke("public_stop"))
            error, _elapsed = self._adjudicate(_ANSWERING, guard)
        (process,) = self.children
        self.assertIsInstance(error, StageLeaseLost)
        assert isinstance(error, StageLeaseLost)
        self.assertEqual(error.provenance, "public_stop")
        self._unproven_note(error, pid=process.pid, failure="error:StageLeaseLost")
        ended = notes.ended()
        self.assertEqual((ended.get("reason"), ended.get("closure")), ("error:StageLeaseLost", "unproven"))
        self.assertEqual(notes.failures, [])


class RegistrationRaceTests(_ChildBoundary):
    def _sweep_before_registration(self, *, guarded: bool) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        evidence = directory / "stdin-bytes"
        child = ("import sys; data = sys.stdin.buffer.read(); "
                 f"open({str(evidence)!r}, 'w').write(str(len(data)))")
        window: list[int] = []
        # The child outlives SIGTERM so it can report what it was handed.
        self.sigterm_blocked_at_spawn = True
        self.on_spawn = lambda _process: window.append(
            codex_cli.terminate_active_semantic_processes(grace_seconds=0.1))
        notes, stderr = _Notes(), io.StringIO()
        with redirect_stderr(stderr):
            error, _elapsed = self._adjudicate(child, _guard(notes) if guarded else None)
        self.assertEqual(window, [0], "the sweep ran after the spawn, before registration")
        original = self._assert_retry_neutral_cancel(error)
        self.assertFalse(getattr(original, "__notes__", None))
        self.assertEqual(evidence.read_text(encoding="utf-8"), "0", "the prompt was never handed over")
        self.assertEqual(stderr.getvalue(), "")
        (process,) = self.children
        self.assertTrue(_group_gone(process.pid))
        if guarded:
            ended = notes.ended()
            self.assertEqual(ended.get("reason"), "cancelled")
            self.assertNotIn("closure", ended)

    def test_a_sweep_between_spawn_and_registration_cancels_a_guarded_call_before_the_prompt(self) -> None:
        self._sweep_before_registration(guarded=True)

    def test_a_sweep_between_spawn_and_registration_cancels_an_unguarded_call_before_the_prompt(self) -> None:
        self._sweep_before_registration(guarded=False)


class GroupClosureTests(_ChildBoundary):
    def _leader(self, evidence: Path, *, keeps_pipes: bool) -> str:
        stdio = "" if keeps_pipes else ", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
        return ("import subprocess,sys,time; "
                "d = subprocess.Popen([sys.executable, '-c', "
                "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']"
                f"{stdio}); "
                f"open({str(evidence)!r}, 'w').write(str(d.pid)); "
                "sys.stdin.read(); sys.stdout.write('{'); sys.stdout.flush(); time.sleep(60)")

    def _descendant_then(self, evidence: Path, action: Callable[[int], None]) -> None:
        def run() -> None:
            deadline = time.monotonic() + 10
            descendant: int | None = None
            while descendant is None and time.monotonic() < deadline:
                with suppress(OSError, ValueError):
                    descendant = int(evidence.read_text(encoding="utf-8"))
                if descendant is None:
                    time.sleep(0.02)
            if descendant is not None:
                self.descendants.append((descendant, self.children[0].pid))
                action(descendant)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)

    def _closed_with_descendant(self, *, keeps_pipes: bool) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        evidence = directory / "descendant-pid"
        members: list[int] = []

        def request(descendant: int) -> None:
            members.append(os.getpgid(descendant))
            codex_cli._SEMANTIC_SHUTDOWN_REQUESTED.set()

        self._descendant_then(evidence, request)
        notes, stderr = _Notes(), io.StringIO()
        with redirect_stderr(stderr):
            error, _elapsed = self._adjudicate(self._leader(evidence, keeps_pipes=keeps_pipes), _guard(notes))
        (process,) = self.children
        self.assertEqual(members, [process.pid], "the descendant is in the leader's group")
        original = self._assert_retry_neutral_cancel(error)
        self.assertFalse(getattr(original, "__notes__", None))
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("closure", notes.ended())
        self.assertIsNotNone(process.returncode, "the leader was reaped before the call returned")
        self.assertTrue(_group_gone(process.pid), "no member of the group survived the call")

    def test_a_descendant_holding_the_pipes_and_ignoring_sigterm_is_closed_with_its_leader(self) -> None:
        self._closed_with_descendant(keeps_pipes=True)

    def test_a_detached_stdio_descendant_ignoring_sigterm_is_closed_with_its_leader(self) -> None:
        self._closed_with_descendant(keeps_pipes=False)

    def test_a_member_that_outlives_its_reaped_leader_is_never_claimed_closed(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        evidence = directory / "descendant-pid"
        members: dict[str, int] = {}

        def leader_only(pgid: int, signum: int) -> None:
            # A member this worker may not signal: only the leader ever receives
            # anything, and once it has exited nothing in the group can be signalled.
            if _running(pgid):
                os.kill(pgid, signum)
                return
            with suppress(ProcessLookupError):
                os.kill(members["descendant"], 0)
                raise _eperm()
            raise ProcessLookupError(errno.ESRCH, os.strerror(errno.ESRCH))

        def request(descendant: int) -> None:
            members["descendant"] = descendant
            codex_cli._SEMANTIC_SHUTDOWN_REQUESTED.set()

        self.on_spawn = lambda process: self.policies.__setitem__(process.pid, leader_only)
        self._descendant_then(evidence, request)
        notes, stderr = _Notes(), io.StringIO()
        with redirect_stderr(stderr):
            error, _elapsed = self._adjudicate(self._leader(evidence, keeps_pipes=False), _guard(notes))
        (process,) = self.children
        original = self._assert_retry_neutral_cancel(error)
        note = self._unproven_note(original, pid=process.pid, failure="cancelled")
        self.assertEqual(stderr.getvalue().splitlines(), [f"[semantic-process] {note}"])
        self.assertEqual(notes.ended().get("closure"), "unproven")
        self.assertIsNotNone(process.returncode, "the leader itself was reaped")
        self.assertEqual(os.getpgid(members["descendant"]), process.pid,
                         "the report is truthful: a member of the group is still running")


class ShutdownSweepTests(_ChildBoundary):
    def test_the_sweep_never_raises_signals_every_child_and_reports_only_live_unsignalled_ones(self) -> None:
        natural: list[str] = []

        def exits_first(pgid: int, signum: int) -> None:
            # This child exits on its own just before the sweep reaches it.
            if not natural:
                os.kill(pgid, signal.SIGKILL)
                _exited_unreaped(pgid)
                try:
                    _REAL_KILLPG(pgid, signum)
                except OSError as exc:
                    natural.append(errno.errorcode.get(exc.errno or 0, "?"))
                    raise
                natural.append("ok")
                return
            _REAL_KILLPG(pgid, signum)

        spawned = {
            role: subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   start_new_session=True, text=True)
            for role in ("unsignalable", "ordinary", "exiting")
        }
        self._unsignalable(spawned["unsignalable"])
        self.policies[spawned["exiting"].pid] = exits_first
        for process in spawned.values():
            self.assertFalse(codex_cli._register_process(process))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            count = codex_cli.terminate_active_semantic_processes(grace_seconds=0.3)
        self.assertEqual(count, 3)
        if sys.platform == "darwin":
            self.assertEqual(natural, ["EPERM"], "the exiting child's group answered EPERM naturally")
        (line,) = stderr.getvalue().splitlines()
        match = _SWEEP_LINE.fullmatch(line)
        assert match is not None, line
        self.assertEqual((int(match["pid"]), match["type"]), (spawned["unsignalable"].pid, "PermissionError"))
        self.assertTrue(_group_gone(spawned["ordinary"].pid), "signalled despite another child's failure")
        self.assertIsNotNone(spawned["exiting"].poll())
        self.assertIsNone(spawned["unsignalable"].poll())
        with redirect_stderr(_FullStream()):
            self.assertEqual(codex_cli.terminate_active_semantic_processes(grace_seconds=0.1), 1,
                             "an unwritable stderr never makes the sweep raise")


if __name__ == "__main__":
    unittest.main()
