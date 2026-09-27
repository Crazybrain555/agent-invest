"""Independent F5 acceptance: resident planes latch first and exit truthfully.

Pro §3.1 exit matrix and §5.3/§6.1: a fatal maintenance or startup-recovery
error latches before cleanup and wakes every plane; a pure operator drain
exits 0 and writes nothing; a latched cause exits 78 even when a later close
or cleanup error escapes, and that error stays secondary; an unclassified
circuit is still a public stop; an unexpected failure without a cause stays a
visible nonzero exit, never 0 and never 78; ``once`` keeps its historical 0
when the singleton is busy.
"""

from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import threading
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime.worker_stop_control import StopRecord, WorkerControlStore
from disclosure_anchor.application.dto.worker_report import WorkerLimits
from disclosure_anchor.application.ports.worker_stop_control import PublicStopCause
from disclosure_anchor.application.services.staged_parse_coordinator import CoordinatorTerminal
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from disclosure_anchor.cli import worker as worker_cli
from tests.unit._f5_stop_fixture import cause, control_dir, durable_control, settings_for, trusted_root


_SECRET = "sk-live-F5SECRET /Volumes/AgentSSD/secret"
_DATABASE = {"DATABASE_URL": "postgresql+psycopg://app@127.0.0.1:9/f5_database_is_down"}


def _plane_settings() -> mock.MagicMock:
    return mock.MagicMock(
        worker_loop_interval_seconds=900,
        worker_loop_max_interval_seconds=1800,
        worker_report_interval_seconds=300,
        worker_wedge_timeout_seconds=0,
        worker_parse_execution_mode="legacy-sync",
    )


def _drain_reports(*, reports, **_kwargs):  # type: ignore[no-untyped-def]
    while reports.get() is not None:
        pass


class ResidentPlaneLatchTests(unittest.TestCase):
    def _run_loop(self, control: InProcessWorkerStopLatch, events: list[tuple[object, ...]],
                  **planes: object) -> BaseException | int:
        deps = mock.MagicMock()
        deps.config.process_scope_classes = None
        with ExitStack() as stack:
            for name, value in {
                "_StopFlag": mock.MagicMock(return_value=mock.MagicMock(**{"is_set.return_value": False})),
                "_create_worker_db_engine": mock.MagicMock(return_value=mock.MagicMock()),
                "require_runtime_app_engine": mock.MagicMock(),
                "_deps": mock.MagicMock(return_value=deps),
                "_limits": mock.MagicMock(return_value=WorkerLimits(sync=1, download=1, parse=1, build=1, publish=1)),
                "_emit_progress_snapshot": mock.MagicMock(),
                "_report_writer": _drain_reports,
                "terminate_active_mineru_processes": mock.MagicMock(
                    side_effect=lambda: events.append(("terminate_mineru", control.is_tripped()))),
                "terminate_active_semantic_processes": mock.MagicMock(
                    side_effect=lambda: events.append(("terminate_semantic", control.is_tripped()))),
                **planes,
            }.items():
                stack.enter_context(mock.patch.object(worker_cli, name, value))
            stack.enter_context(redirect_stderr(io.StringIO()))
            try:
                return worker_cli._run_loop(_plane_settings(), lock_conn=mock.MagicMock(), stop_control=control)
            except BaseException as exc:  # noqa: BLE001 - the test inspects it
                return exc

    def test_fatal_maintenance_latches_before_cleanup_and_wakes_the_parse_plane(self) -> None:
        events: list[tuple[object, ...]] = []
        control = InProcessWorkerStopLatch(on_first_trip=lambda first: events.append(("trip", first.kind)))
        boom = RuntimeError("maintenance failed " + _SECRET)

        def parse_plane(_deps, *, should_stop, work_available, **_kwargs):  # type: ignore[no-untyped-def]
            woke = work_available.wait(timeout=5)
            events.append(("parse_woken", woke, should_stop(), control.is_tripped()))

        outcome = self._run_loop(
            control, events,
            _run_startup_recovery=mock.MagicMock(),
            _run_maintenance_loop=mock.MagicMock(side_effect=boom),
            run_resident_parse=parse_plane,
        )

        self.assertIs(outcome, boom)
        first = control.first_cause()
        assert first is not None
        self.assertEqual((first.kind, first.reason_code, first.origin, first.exception_class),
                         ("maintenance_fatal", "maintenance_loop_failed", "maintenance", "builtins.RuntimeError"))
        self.assertNotIn(_SECRET, json.dumps(first.to_payload(), ensure_ascii=False))
        self.assertEqual(events[0], ("trip", "maintenance_fatal"))
        self.assertIn(("parse_woken", True, True, True), events)
        terminations = [event for event in events if event[0].startswith("terminate")]
        self.assertTrue(terminations)
        self.assertTrue(all(tripped for _, tripped in terminations), "cleanup never precedes the latch")

    def test_fatal_startup_recovery_latches_before_cleanup_but_interrupts_do_not(self) -> None:
        cases = (
            (RuntimeError("recovery failed " + _SECRET), ("startup_fatal", "startup_recovery_failed")),
            (worker_cli.WorkerSingletonGuardError("singleton advisory lock was lost"),
             ("ownership_lost", "singleton_lost")),
            (KeyboardInterrupt(), None),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                events: list[tuple[object, ...]] = []
                control = InProcessWorkerStopLatch(on_first_trip=lambda first: events.append(("trip", first.kind)))
                parse = mock.MagicMock(side_effect=AssertionError("parse admitted after a fatal recovery"))
                outcome = self._run_loop(
                    control, events,
                    _run_startup_recovery=mock.MagicMock(side_effect=error),
                    _run_maintenance_loop=mock.MagicMock(side_effect=AssertionError("maintenance started")),
                    run_resident_parse=parse,
                )
                self.assertIs(outcome, error)
                parse.assert_not_called()
                first = control.first_cause()
                if expected is None:
                    self.assertIsNone(first)
                    continue
                assert first is not None
                self.assertEqual((first.kind, first.reason_code, first.origin),
                                 (*expected, "startup_recovery"))
                self.assertEqual(events[0], ("trip", expected[0]))
                self.assertTrue(all(event[1] for event in events if event[0].startswith("terminate")))


def _result(**values: object) -> mock.MagicMock:
    return mock.MagicMock(**{"terminal": CoordinatorTerminal.STUCK_OPEN_CIRCUIT, "errors": (), **values})


class StagedResidentEndingTests(unittest.TestCase):
    def test_only_a_pure_operator_drain_returns_normally(self) -> None:
        with self.subTest(case="operator drain, operator requested, no cause"):
            control = InProcessWorkerStopLatch()
            with redirect_stdout(io.StringIO()):
                worker_cli._end_staged_resident(_result(termination_kind="operator_drain"), control, lambda: True)
            self.assertFalse(control.is_tripped())
        with self.subTest(case="drain-shaped result without an operator request"):
            control = InProcessWorkerStopLatch()
            with self.assertRaises(worker_cli.WorkerPublicStopError) as raised:
                worker_cli._end_staged_resident(_result(termination_kind="operator_drain"), control, lambda: False)
            self.assertEqual(raised.exception.cause.reason_code, "unclassified_circuit")  # type: ignore[union-attr]
        with self.subTest(case="latched fault racing the operator request"):
            control = InProcessWorkerStopLatch()
            fault = cause("forbidden_tool_call")
            control.trip(fault)
            with self.assertRaises(worker_cli.WorkerPublicStopError) as raised:
                worker_cli._end_staged_resident(_result(termination_kind="public_stop"), control, lambda: True)
            self.assertIs(raised.exception.cause, fault)
        with self.subTest(case="circuit with no latched cause"):
            control = InProcessWorkerStopLatch()
            with self.assertRaises(worker_cli.WorkerPublicStopError):
                worker_cli._end_staged_resident(_result(termination_kind="circuit",
                                                        errors=("a:commit:boom",)), control, lambda: False)
            first = control.first_cause()
            assert first is not None
            self.assertEqual((first.kind, first.reason_code, first.origin),
                             ("coordinator_circuit", "unclassified_circuit", "resident"))

    def test_staged_startup_and_controller_failures_latch_and_always_close_the_runtime(self) -> None:
        cases = (
            ("startup verification", "verify_startup", ("startup_fatal", "staged_startup_verification_failed")),
            ("coordinator run", "coordinator.run", ("coordinator_fault", "coordinator_run_failed")),
        )
        for name, failing, expected in cases:
            with self.subTest(case=name):
                runtime = mock.MagicMock()
                boom = RuntimeError(f"{name} failed {_SECRET}")
                if failing == "verify_startup":
                    runtime.verify_startup.side_effect = boom
                else:
                    runtime.coordinator.run.side_effect = boom
                control = InProcessWorkerStopLatch()
                with (
                    mock.patch("disclosure_anchor.adapters.runtime.staged_worker_v4.build_staged_worker_v4_runtime",
                               return_value=runtime),
                    self.assertRaises(RuntimeError) as raised,
                ):
                    self._resident(control)
                self.assertIs(raised.exception, boom)
                runtime.close.assert_called_once_with()
                first = control.first_cause()
                assert first is not None
                self.assertEqual((first.kind, first.reason_code), expected)

    def _resident(self, control: InProcessWorkerStopLatch, *, operator: bool = False) -> None:
        deps = mock.MagicMock()
        deps.config.process_scope_classes = None
        worker_cli._run_staged_v4_resident(
            mock.MagicMock(worker_loop_interval_seconds=900,
                           disclosure_mineru_stream_pressure_config=None,
                           disclosure_mineru_stream_pressure_config_sha256=None),
            engine=mock.MagicMock(), deps=deps, should_stop=lambda: False,
            ownership_guard=lambda: None, admission_guard=lambda: None,
            work_available=threading.Event(), prune_tracker=worker_cli._ProjectionPruneTracker(),
            progress_output="off", stop_control=control, operator_stop_requested=lambda: operator,
        )


class ResidentExitCodeTests(unittest.TestCase):
    def _resident(self, settings, control, run_loop):  # type: ignore[no-untyped-def]
        lock_conn = mock.MagicMock()
        lock_conn.execute.return_value.scalar_one.return_value = True
        lock_engine = mock.MagicMock()
        lock_engine.connect.return_value = lock_conn
        stderr = io.StringIO()
        with (
            mock.patch.object(worker_cli, "MinerUDeploymentChecker"),
            mock.patch.object(worker_cli, "_print_version_banner"),
            mock.patch.object(worker_cli.sqlalchemy, "create_engine", return_value=lock_engine),
            mock.patch.object(worker_cli, "require_runtime_app_connection"),
            mock.patch.object(worker_cli, "_run_loop", side_effect=run_loop),
            redirect_stderr(stderr),
        ):
            try:
                code: object = worker_cli.run_resident_worker(settings, stop_control=control)
            except BaseException as exc:  # noqa: BLE001 - inspected by the caller
                code = exc
        lock_conn.close.assert_called_once_with()
        return code, stderr.getvalue()

    def test_exit_codes_follow_the_first_cause_not_the_last_error(self) -> None:
        fault = PublicStopCause(kind="stage_fault", reason_code="commit_unexpected_failure",
                                origin="stage_call", attempt_id="rpa_f5_exit")
        with self.subTest(case="pure operator stop"):
            root = trusted_root(self)
            settings = settings_for(root, extra=_DATABASE)
            control = durable_control(settings, launchctl=None)
            code, _ = self._resident(settings, control, lambda *a, **k: 0)
            self.assertEqual(code, 0)
            self.assertFalse(control_dir(root).exists(), "an operator stop writes no record")
        with self.subTest(case="public stop raised by the resident"):
            root = trusted_root(self)
            settings = settings_for(root, extra=_DATABASE)
            control = durable_control(settings, launchctl=None)

            def public_stop(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                control.trip(fault)
                raise worker_cli.WorkerPublicStopError(fault)

            code, stderr = self._resident(settings, control, public_stop)
            self.assertEqual(code, 78)
            self.assertIn("PUBLIC_STOP exit 78: kind=stage_fault", stderr)
            self.assertIn("marker=written", stderr)
            record = WorkerControlStore.for_settings(settings).read_active().record
            assert record is not None
            self.assertEqual(record.cause, fault)
        with self.subTest(case="close error after the latch stays secondary"):
            root = trusted_root(self)
            settings = settings_for(root, extra=_DATABASE)
            control = durable_control(settings, launchctl=None)
            close_error = RuntimeError("runtime close failed")

            def close_fails(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                control.trip(fault)
                raise close_error

            code, stderr = self._resident(settings, control, close_fails)
            self.assertEqual(code, 78)
            self.assertIs(control.first_cause(), fault)
            self.assertIn("runtime close failed", stderr)
            self.assertIn("PUBLIC_STOP exit 78: kind=stage_fault", stderr)
            active = (control_dir(root) / "worker-circuit-stop.json").read_bytes()
            self.assertEqual(StopRecord.decode(active).cause, fault)
        with self.subTest(case="a plane latched while the loop returned normally"):
            root = trusted_root(self)
            settings = settings_for(root, extra=_DATABASE)
            control = durable_control(settings, launchctl=None)

            def returns_after_trip(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                control.trip(fault)
                return 0

            code, _ = self._resident(settings, control, returns_after_trip)
            self.assertEqual(code, 78)
        with self.subTest(case="unexpected failure without a cause"):
            root = trusted_root(self)
            settings = settings_for(root, extra=_DATABASE)
            control = durable_control(settings, launchctl=None)
            unexpected = RuntimeError("unexpected")
            code, _ = self._resident(settings, control, mock.MagicMock(side_effect=unexpected))
            self.assertIs(code, unexpected, "visible nonzero failure, neither 0 nor 78")
            self.assertFalse(control.is_tripped())
            self.assertFalse(control_dir(root).exists())

    def test_once_keeps_zero_when_the_singleton_is_busy(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root, extra=_DATABASE)
        lock_conn = mock.MagicMock()
        lock_conn.execute.return_value.scalar_one.return_value = False
        lock_engine = mock.MagicMock()
        lock_engine.connect.return_value = lock_conn
        with (
            mock.patch.object(worker_cli, "load_settings", return_value=settings),
            mock.patch.object(worker_cli, "MinerUDeploymentChecker"),
            mock.patch.object(worker_cli, "_print_version_banner"),
            mock.patch.object(worker_cli.sqlalchemy, "create_engine", return_value=lock_engine),
            mock.patch.object(worker_cli, "require_runtime_app_connection"),
            mock.patch.object(worker_cli, "_run_rounds", side_effect=AssertionError("round ran while busy")),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(worker_cli.main(["once"]), 0)
        self.assertFalse(control_dir(root).exists())


if __name__ == "__main__":
    unittest.main()
