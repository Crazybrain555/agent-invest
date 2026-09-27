"""Independent acceptance: per-attempt retry windows and safe exhaustion diagnostics.

Root-adopted MIN fix of the 06:38 remote-poll episode (T1–T10 of the Fable
report): a provider-witnessed wait (``StageProviderWaiting``) ends only that
attempt's failure episode and restarts both its count and its time window;
local waits and other attempts' answers restart nothing; genuinely consecutive
failures still exhaust by count and by time; the global soft counter is
unchanged. The exhaustion diagnostic names its bounds and the last failure
without echoing unknown text (T11 and root's secret-safety rule).

The real coordinator runs on a fake monotonic clock advanced by its own
process guard and by each scripted remote episode (production-shaped limits:
300 s lease, 240 s stage step, 8 attempts, 300 s window).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import re
import threading
import unittest
from unittest import mock

import httpx

from disclosure_anchor.application.ports.remote_provider_v4 import (
    RemoteProviderUnavailableV4,
    RemoteProviderWaitingV4,
)
from disclosure_anchor.application.ports.worker_stop_control import exception_fingerprint
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorResult,
    CoordinatorSnapshot,
    CoordinatorTerminal,
    CoordinatorWork,
    ResourceCreditVector,
    RetryStage,
    StageLeaseGuard,
    StageProviderWaiting,
    StageWaiting,
    StagedParseCoordinator,
)
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from disclosure_anchor.cli import worker as worker_cli
from tests.unit._f5_stop_fixture import cause as stop_cause
from tests.unit.test_staged_coordinator_backend_v4 import (
    _authority,
    _backend,
    _guard,
    _work as _v4_work,
)
from tests.unit.test_staged_parse_coordinator import _Backend, _Clock, _limits, _work


_POLL = "provider poll episode was unavailable"
_SUBMIT = "provider submission episode was unavailable"
_SECRETS = ("sk-live-F5RETRY", "https://", "mineru.example", "/Volumes/AgentSSD")
_EXHAUSTED = re.compile(
    r"^(?P<attempt>[^:]+):(?P<lane>[a-z_]+):retry budget exhausted \(attempts=(?P<count>\d+)/(?P<max>\d+), "
    r"elapsed=(?P<elapsed>[0-9.]+)s/(?P<window>[0-9.e+]+)s, last=(?P<last>[^,]+), causes=(?P<causes>[^,)]+)"
    r"(?:, http_status=(?P<status>\d{3}))?\)$"
)

Step = str | tuple[str, float] | Callable[[], BaseException]


class _ScriptedRemote(_Backend):
    """The shared fake backend with a scripted remote lane on a fake clock.

    Steps: ``"fail"`` (the backend's RetryStage literal), ``"wait"`` (a
    provider-witnessed ``StageProviderWaiting``), ``"pause"`` (a local plain
    ``StageWaiting``), ``"complete"``, a ``(step, seconds)`` pair for a long
    episode, or a callable returning the exact exception to raise.
    """

    def __init__(self, clock: _Clock, scripts: dict[str, list[Step]], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.clock = clock
        self.advance_clock = clock.advance
        self.scripts = {attempt: list(steps) for attempt, steps in scripts.items()}
        self.outcomes: list[tuple[str, str, float]] = []
        self.on_outcome: Callable[[str, str], None] = lambda _attempt, _step: None

    def run_remote(self, work: CoordinatorWork, *, credit_allowance: ResourceCreditVector,
                   stage_guard: StageLeaseGuard) -> CoordinatorWork:
        script = self.scripts.get(work.attempt_id)
        if work.state in ("submitted", "reconciling") and script:
            stage_guard.checkpoint()
            step = script.pop(0)
            name, seconds = (step, 0.2) if not isinstance(step, tuple) else step
            self.clock.advance(seconds)
            stage_guard.checkpoint()
            label = name if isinstance(name, str) else "raise"
            self.outcomes.append((work.attempt_id, label, self.clock()))
            self.on_outcome(work.attempt_id, label)
            if callable(name):
                raise name()
            if name == "fail":
                raise RetryStage(_POLL if work.state == "submitted" else _SUBMIT, retry_after_seconds=1.0)
            if name == "wait":
                raise StageProviderWaiting("provider task is processing", retry_after_seconds=1.0)
            if name == "pause":
                raise StageWaiting("provider submission paused by stream pressure", retry_after_seconds=1.0)
            if name != "complete":
                raise AssertionError(f"unknown scripted step {name!r}")
            self.calls.append(f"complete:{work.attempt_id}")
        return super().run_remote(work, credit_allowance=credit_allowance, stage_guard=stage_guard)


def _run(scripts: dict[str, list[Step]], *, states: dict[str, str] | None = None,
         new: tuple[CoordinatorWork, ...] = (), on_outcome: Callable[[_ScriptedRemote, str, str], None] | None = None,
         **limit_changes: object) -> tuple[CoordinatorResult, _ScriptedRemote, list[CoordinatorSnapshot],
                                           InProcessWorkerStopLatch]:
    clock = _Clock(1_000.0)
    works = tuple(_work(attempt, (states or {}).get(attempt, "submitted"), 2) for attempt in scripts)
    backend = _ScriptedRemote(clock, scripts, recoverable=works, new=new)
    if on_outcome is not None:
        backend.on_outcome = lambda attempt, step: on_outcome(backend, attempt, step)
    snapshots: list[CoordinatorSnapshot] = []
    latch = InProcessWorkerStopLatch()
    limits = _limits(**{"claim_lease_seconds": 300, "max_stage_step_seconds": 240,
                        "claim_renew_margin_seconds": 30, "retry_max_attempts": 8,
                        "retry_stuck_seconds": 300, **limit_changes})
    box: list[CoordinatorResult] = []

    def run() -> None:
        box.append(StagedParseCoordinator(
            backend=backend, limits=limits, progress=snapshots.append, monotonic=clock,
            process_guard=lambda: clock.advance(0.25), stop_control=latch,
        ).run())

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=60)
    if thread.is_alive():
        raise AssertionError("coordinator did not finish within 60 s of real time")
    return box[0], backend, snapshots, latch


def _exhaustion(result: CoordinatorResult) -> re.Match[str]:
    entries = [error for error in result.errors if "retry budget exhausted" in error]
    if len(entries) != 1:
        raise AssertionError(f"expected exactly one exhaustion entry, got {result.errors!r}")
    match = _EXHAUSTED.fullmatch(entries[0])
    if match is None:
        raise AssertionError(f"exhaustion entry has an unexpected shape: {entries[0]!r}")
    return match


class WitnessedWaitWindowTests(unittest.TestCase):
    def assert_completed(self, result: CoordinatorResult, snapshots: list[CoordinatorSnapshot],
                         latch: InProcessWorkerStopLatch, attempt: str = "rpa_a") -> None:
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result.errors)
        self.assertEqual(result.errors, ())
        self.assertEqual(dict(result.final_states), {attempt: "acked"})
        self.assertFalse(any(s.blocked_reason == "retry_stage_stuck" for s in snapshots))
        self.assertFalse(latch.is_tripped())

    def assert_stuck(self, result: CoordinatorResult, latch: InProcessWorkerStopLatch,
                     attempt: str = "rpa_a") -> re.Match[str]:
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        match = _exhaustion(result)
        self.assertEqual(match["attempt"], attempt)
        cause = latch.first_cause()
        assert cause is not None
        self.assertEqual((cause.kind, cause.reason_code, cause.attempt_id),
                         ("coordinator_circuit", "retry_stage_stuck", attempt))
        self.assertEqual(result.termination_kind, "public_stop")
        return match

    def test_t1_witnessed_waits_restart_the_time_window(self) -> None:
        separated = ["fail", *[("wait", 20.0)] * 20, "fail", "wait", "wait", "complete"]
        result, backend, snapshots, latch = _run({"rpa_a": separated})
        self.assert_completed(result, snapshots, latch)
        fails = [at for _, step, at in backend.outcomes if step == "fail"]
        self.assertGreaterEqual(fails[1] - fails[0], 300, "the two failures are a window apart")
        with self.subTest(adjacent="the same spacing with only local waits exhausts by time"):
            local = ["fail", *[("pause", 20.0)] * 20, "fail", "complete"]
            result, _, _, latch = _run({"rpa_a": local})
            match = self.assert_stuck(result, latch)
            self.assertLess(int(match["count"]), 9, "exhausted by the time window, not the count")
            self.assertGreaterEqual(float(match["elapsed"]), 300)

    def test_t2_witnessed_waits_restart_the_count(self) -> None:
        flapping = [*["fail", "wait"] * 9, "complete"]
        result, _, snapshots, latch = _run({"rpa_a": flapping}, retry_stuck_seconds=1e6)
        self.assert_completed(result, snapshots, latch)
        with self.subTest(adjacent="T6b: plain waits between poll failures restart nothing"):
            result, _, _, latch = _run({"rpa_a": [*["fail", "pause"] * 9, "complete"]}, retry_stuck_seconds=1e6)
            match = self.assert_stuck(result, latch)
            self.assertEqual((match["count"], match["max"]), ("9", "8"))

    def test_t3_consecutive_failures_still_exhaust_by_count_and_keep_the_reservation(self) -> None:
        result, _, _, latch = _run({"rpa_a": ["fail"] * 9 + ["complete"]})
        match = self.assert_stuck(result, latch)
        self.assertEqual((match["lane"], match["count"], match["max"], match["last"]),
                         ("remote", "9", "8", _POLL))
        self.assertEqual(result.credits_in_use.documents, 1)
        self.assertEqual(result.completed, 0)

    def test_t4_consecutive_hung_failures_still_exhaust_by_time(self) -> None:
        result, backend, _, latch = _run({"rpa_a": [("fail", 216.0)] * 9 + ["complete"]})
        match = self.assert_stuck(result, latch)
        self.assertLess(int(match["count"]), 9)
        self.assertGreaterEqual(float(match["elapsed"]), 300)
        self.assertEqual(sum(step == "fail" for _, step, _ in backend.outcomes), int(match["count"]))

    def test_t5_an_outage_after_a_long_healthy_wait_still_exhausts(self) -> None:
        result, _, _, latch = _run({"rpa_a": [*[("wait", 20.0)] * 20, *["fail"] * 9, "complete"]})
        match = self.assert_stuck(result, latch)
        self.assertEqual(match["count"], "9")

    def test_t6a_submit_stream_pauses_do_not_restart_the_window(self) -> None:
        result, _, _, latch = _run({"rpa_a": [*["fail", "pause"] * 9, "complete"]},
                                   states={"rpa_a": "reconciling"}, retry_stuck_seconds=1e6)
        match = self.assert_stuck(result, latch)
        self.assertEqual((match["count"], match["last"]), ("9", _SUBMIT))

    def test_t7_another_attempts_witnessed_waits_restart_nothing_here(self) -> None:
        result, _, _, latch = _run({"rpa_a": ["fail"] * 9 + ["complete"],
                                    "rpa_b": ["wait"] * 60 + ["complete"]})
        self.assert_stuck(result, latch)
        self.assertFalse(any(error.startswith("rpa_b:") for error in result.errors))

    def test_t8_a_flapping_but_alive_provider_is_bounded_only_by_its_runaway(self) -> None:
        flapping = [*["fail", ("wait", 20.0)] * 20, "complete"]
        result, backend, snapshots, latch = _run({"rpa_a": flapping})
        self.assert_completed(result, snapshots, latch)
        fails = [at for _, step, at in backend.outcomes if step == "fail"]
        self.assertEqual(len(fails), 20)
        self.assertGreater(fails[-1] - fails[0], 300)

    def test_t10_the_global_soft_counter_still_clears_only_on_a_durable_transition(self) -> None:
        arrived: list[bool] = []

        def on_outcome(backend: _ScriptedRemote, _attempt: str, step: str) -> None:
            # A new document arrives at the first witnessed wait after three
            # attempts have each failed once (admission is soft-closed).
            fails = sum(item[1] == "fail" for item in backend.outcomes)
            if step == "wait" and fails >= 3 and not arrived:
                arrived.append(True)
                backend.new.append(replace(_work("rpa_new", "prepared"),
                                           lease_expires_monotonic=backend.clock() + 300))

        result, backend, snapshots, latch = _run(
            {"rpa_a": ["fail", "wait", "wait", "complete"],
             "rpa_b": ["fail", *["wait"] * 40, "complete"],
             "rpa_c": ["fail", *["wait"] * 40, "complete"]},
            on_outcome=on_outcome,
        )
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result.errors)
        self.assertFalse(latch.is_tripped())
        self.assertTrue(arrived)
        first_transition = backend.calls.index("complete:rpa_a")
        admitted = backend.calls.index("preflight:rpa_new")
        self.assertGreater(admitted, first_transition,
                           "witnessed waits alone never reopen admission (MIN keeps the global counter)")
        self.assertTrue(any(s.blocked_reason == "retry_degraded" for s in snapshots))


class BackendWitnessContractTests(unittest.TestCase):
    """T9: only an authoritative pending/processing poll is a witnessed wait."""

    def _poll(self, outcome: object, *, wall_clock: float, runaway: int = 3_600) -> tuple[object, object]:
        authority = _authority("submitted")
        inputs = mock.Mock()
        inputs.poll_command.return_value = mock.sentinel.poll
        inputs.remote_runaway_seconds.return_value = runaway
        remote = mock.Mock()
        if isinstance(outcome, BaseException):
            remote.poll_once.side_effect = outcome
        else:
            remote.poll_once.return_value = outcome
        backend, persistence, _, _ = _backend(authority, inputs=inputs, remote=remote,
                                              wall_clock=lambda: wall_clock)
        with mock.patch.object(backend, "_capability", return_value=mock.sentinel.capability):
            try:
                result: object = backend.run_remote(
                    _v4_work(authority), credit_allowance=ResourceCreditVector(provider_result_bytes=20),
                    stage_guard=_guard(),
                )
            except BaseException as exc:  # noqa: BLE001 - inspected by the caller
                result = exc
        return result, persistence

    def test_pending_or_processing_answer_is_the_only_witnessed_wait(self) -> None:
        for status in ("pending", "processing"):
            with self.subTest(status=status):
                waiting = RemoteProviderWaitingV4(remote_task_identity="task-1", status=status,
                                                  response_sha256="sha256:" + "1" * 64, response_byte_count=2)
                raised, persistence = self._poll(waiting, wall_clock=2.0)
                self.assertIs(type(raised), StageProviderWaiting)
                self.assertEqual(persistence.appends, [])
        with self.subTest(case="the runaway check runs first"):
            waiting = RemoteProviderWaitingV4(remote_task_identity="task-1", status="processing",
                                              response_sha256="sha256:" + "1" * 64, response_byte_count=2)
            updated, persistence = self._poll(waiting, wall_clock=12.0, runaway=10)
            self.assertEqual(updated.state, "cleanup_pending")  # type: ignore[attr-defined]
        with self.subTest(case="an unavailable poll is a failure, never a wait"):
            raised, persistence = self._poll(
                RemoteProviderUnavailableV4("MinerU V4 status returned HTTP 503"), wall_clock=2.0)
            self.assertIs(type(raised), RetryStage)
            self.assertEqual(str(raised), _POLL)
            self.assertIsInstance(raised.__cause__, RemoteProviderUnavailableV4)  # type: ignore[union-attr]
            self.assertEqual(persistence.appends, [])


def _chained(message: str, *causes: BaseException) -> Callable[[], BaseException]:
    """A RetryStage raised ``from`` a chain of causes (outermost first)."""

    def build() -> BaseException:
        inner: BaseException | None = None
        for cause in reversed(causes):
            if inner is not None:
                cause.__cause__ = inner
            inner = cause
        retry = RetryStage(message, retry_after_seconds=1.0)
        retry.__cause__ = inner
        return retry

    return build


class SafeExhaustionDiagnosticTests(unittest.TestCase):
    def _exhaust(self, step: Callable[[], BaseException]) -> tuple[CoordinatorResult, InProcessWorkerStopLatch, str]:
        result, _, _, latch = _run({"rpa_a": [step] * 9 + ["complete"]}, retry_stuck_seconds=1e6)
        entry = _exhaustion(result).string
        for secret in _SECRETS:
            self.assertNotIn(secret, " ".join(result.errors))
            self.assertNotIn(secret, json.dumps(latch.first_cause().to_payload()))  # type: ignore[union-attr]
        return result, latch, entry

    def test_t11_a_recognized_literal_and_its_cause_types_and_status_are_named(self) -> None:
        step = _chained(
            _POLL,
            RemoteProviderUnavailableV4("MinerU V4 status returned HTTP 503"),
            httpx.ReadTimeout("read timed out for https://user:sk-live-F5RETRY@mineru.example/tasks/t1"),
        )
        _, _, entry = self._exhaust(step)
        match = _EXHAUSTED.fullmatch(entry)
        assert match is not None
        self.assertEqual((match["count"], match["max"], match["window"], match["last"], match["causes"],
                          match["status"]),
                         ("9", "8", "1e+06", _POLL, "RemoteProviderUnavailableV4<-ReadTimeout", "503"))

    def test_unknown_retry_text_is_fingerprinted_and_matches_the_stop_record(self) -> None:
        secret_text = "poll of https://token:sk-live-F5RETRY@mineru.example/x failed at /Volumes/AgentSSD/y"
        captured: list[BaseException] = []

        def step() -> BaseException:
            error = RetryStage(secret_text, retry_after_seconds=1.0)
            captured.append(error)
            return error

        _, latch, entry = self._exhaust(step)
        match = _EXHAUSTED.fullmatch(entry)
        assert match is not None
        cause = latch.first_cause()
        assert cause is not None
        self.assertEqual(match["last"], "unrecognized " + exception_fingerprint(captured[-1]))
        self.assertEqual(match["last"], "unrecognized " + str(cause.exception_fingerprint))
        self.assertEqual(match["causes"], "none")
        self.assertIsNone(match["status"])

    def test_near_miss_status_text_long_chains_and_odd_type_names_leak_nothing(self) -> None:
        odd = type("bad name!", (RuntimeError,), {})
        step = _chained(
            _POLL,
            RemoteProviderUnavailableV4("GET https://mineru.example/tasks returned HTTP 503 sk-live-F5RETRY"),
            odd("sk-live-F5RETRY"),
            OSError("/Volumes/AgentSSD/private"),
            ValueError("sk-live-F5RETRY"),
            KeyError("sk-live-F5RETRY"),
        )
        _, _, entry = self._exhaust(step)
        match = _EXHAUSTED.fullmatch(entry)
        assert match is not None
        self.assertEqual(match["causes"], "RemoteProviderUnavailableV4<-unnamed<-OSError<-ValueError")
        self.assertIsNone(match["status"], "a status is parsed only from the exact fixed pattern")

    def test_resident_logs_only_exhaustion_lines_and_never_the_error_text(self) -> None:
        result, latch, entry = self._exhaust(_chained(_POLL, RemoteProviderUnavailableV4("x returned HTTP 502")))
        planted = "rpa_b:remote:retry budget exhausted sk-live-F5RETRY https://mineru.example"
        raw = "rpa_c:commit:RuntimeError:boom sk-live-F5RETRY /Volumes/AgentSSD/secret"
        noisy = mock.MagicMock(terminal=result.terminal, termination_kind=result.termination_kind,
                               errors=(raw, entry, planted))
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(worker_cli.WorkerPublicStopError):
            worker_cli._end_staged_resident(noisy, latch, lambda: False)
        self.assertEqual(stderr.getvalue().splitlines(), [f"[staged-v4] {entry}"])

    def test_the_loop_exit_never_prints_the_public_stop_error_text(self) -> None:
        latch = InProcessWorkerStopLatch()
        latch.trip(stop_cause("retry_stage_stuck", kind="coordinator_circuit", origin="coordinator"))
        secret_error = "rpa_c:commit:RuntimeError:boom sk-live-F5RETRY /Volumes/AgentSSD/secret"
        lock_conn = mock.MagicMock()
        lock_conn.execute.return_value.scalar_one.return_value = True
        lock_engine = mock.MagicMock()
        lock_engine.connect.return_value = lock_conn
        stderr, stdout = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(worker_cli, "MinerUDeploymentChecker"),
            mock.patch.object(worker_cli, "_print_version_banner"),
            mock.patch.object(worker_cli.sqlalchemy, "create_engine", return_value=lock_engine),
            mock.patch.object(worker_cli, "require_runtime_app_connection"),
            mock.patch.object(worker_cli, "_refuse_stopped_start", return_value=None),
            mock.patch.object(worker_cli, "_run_loop", side_effect=worker_cli.WorkerPublicStopError(
                latch.first_cause(), (secret_error,))),
            redirect_stderr(stderr), redirect_stdout(stdout),
        ):
            code = worker_cli.run_resident_worker(mock.MagicMock(worker_parse_execution_mode="legacy-sync"),
                                                  stop_control=latch)  # type: ignore[arg-type]
        self.assertEqual(code, 78)
        for secret in _SECRETS:
            self.assertNotIn(secret, stderr.getvalue() + stdout.getvalue())

    def test_progress_line_is_timestamped_and_printed_only_on_change(self) -> None:
        captured: dict[str, Callable[[CoordinatorSnapshot], None]] = {}
        snapshot_a = mock.MagicMock(spec=CoordinatorSnapshot, recovery_complete=True, admission_open=True,
                                    circuit_open=False, in_flight=(), queued=(), completed=0,
                                    blocked_reason=None)
        snapshot_b = mock.MagicMock(spec=CoordinatorSnapshot, recovery_complete=True, admission_open=False,
                                    circuit_open=True, in_flight=(), queued=(), completed=0,
                                    blocked_reason="retry_stage_stuck")
        runtime = mock.MagicMock()

        def run(**_kwargs: object) -> object:
            for snapshot in (snapshot_a, snapshot_a, snapshot_b):
                captured["progress"](snapshot)
            return mock.MagicMock(terminal=CoordinatorTerminal.QUIESCENT)

        runtime.coordinator.run.side_effect = run

        def build(**kwargs: object) -> object:
            captured["progress"] = kwargs["progress"]  # type: ignore[assignment]
            return runtime

        stdout = io.StringIO()
        with (
            mock.patch("disclosure_anchor.adapters.runtime.staged_worker_v4.build_staged_worker_v4_runtime",
                       side_effect=build),
            redirect_stdout(stdout),
        ):
            worker_cli._run_staged_v4_resident(
                mock.MagicMock(worker_loop_interval_seconds=900, disclosure_mineru_stream_pressure_config=None,
                               disclosure_mineru_stream_pressure_config_sha256=None),
                engine=mock.MagicMock(), deps=mock.MagicMock(), should_stop=mock.Mock(side_effect=[False, True]),
                ownership_guard=lambda: None, admission_guard=lambda: None, work_available=threading.Event(),
                prune_tracker=worker_cli._ProjectionPruneTracker(), progress_output="terminal",
            )
        lines = [line for line in stdout.getvalue().splitlines() if line.startswith("[staged-v4]")]
        self.assertEqual(len(lines), 2, "an unchanged snapshot is not printed again")
        pattern = r"^\[staged-v4\] \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 recovery=done admission=(open|closed) "
        for line in lines:
            self.assertRegex(line, pattern)
        self.assertTrue(lines[1].endswith("blocked=retry_stage_stuck"))


if __name__ == "__main__":
    unittest.main()
