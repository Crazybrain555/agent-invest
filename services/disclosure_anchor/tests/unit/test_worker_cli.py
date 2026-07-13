"""Adaptive resident worker-loop tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.application.dto.worker_report import (
    WorkerFailure,
    WorkerLimits,
    WorkerReport,
)
from disclosure_anchor.cli import worker as worker_cli
from disclosure_anchor.settings import Settings


def _report(**values: int | bool) -> WorkerReport:
    report = WorkerReport(started_at=datetime(2026, 7, 13, tzinfo=timezone.utc))
    for name, value in values.items():
        setattr(report, name, value)
    return report


class AdaptiveLoopControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = worker_cli._AdaptiveLoopController(900, 1800)
        self.limits = WorkerLimits(sync=13, download=300, parse=50, build=10, publish=10)

    def test_progress_runs_next_round_without_wait(self) -> None:
        self.assertEqual(self.controller.observe(_report(parsed=1), now=10.0), 0)
        self.assertEqual(self.controller.effective_limits(self.limits, now=10.0), self.limits)

    def test_idle_backoff_is_fifteen_then_thirty_minutes(self) -> None:
        self.assertEqual(self.controller.observe(_report(), now=0.0), 900)
        self.assertEqual(self.controller.observe(_report(), now=900.0), 1800)
        self.assertEqual(self.controller.observe(_report(), now=2700.0), 1800)

    def test_quota_break_cools_only_sync_stage(self) -> None:
        report = _report(failed=1, sync_quota_break=True)
        report.failures.append(WorkerFailure("sync", "000001", "quota_exhausted"))

        self.assertEqual(self.controller.observe(report, now=100.0), 0)
        cooled = self.controller.effective_limits(self.limits, now=101.0)
        self.assertEqual(cooled.sync, 0)
        self.assertEqual(cooled.download, 300)
        self.assertEqual(cooled.parse, 50)
        self.assertEqual(
            self.controller.effective_limits(self.limits, now=1900.0).sync,
            13,
        )

    def test_local_download_progress_does_not_reset_quota_backoff(self) -> None:
        first = _report(failed=1, sync_quota_break=True)
        first.failures.append(WorkerFailure("sync", "000001", "quota_exhausted"))
        self.controller.observe(first, now=0.0)

        self.controller.observe(_report(downloaded=1), now=1.0)
        second = _report(failed=1, sync_quota_break=True)
        second.failures.append(WorkerFailure("sync", "000001", "quota_exhausted"))
        self.controller.observe(second, now=1801.0)

        self.assertEqual(
            self.controller.effective_limits(self.limits, now=3602.0).sync,
            0,
        )
        self.assertEqual(
            self.controller.effective_limits(self.limits, now=5402.0).sync,
            13,
        )

    def test_gpu_outage_cools_only_parse_stage(self) -> None:
        report = _report(failed=8)
        report.failures.extend(
            WorkerFailure("parse", f"doc_{index}", "parser_invocation_failed")
            for index in range(8)
        )

        self.controller.observe(report, now=10.0)
        cooled = self.controller.effective_limits(self.limits, now=11.0)
        self.assertEqual(cooled.parse, 0)
        self.assertEqual(cooled.sync, 13)
        self.assertEqual(
            self.controller.effective_limits(self.limits, now=130.0).parse,
            50,
        )

    def test_actual_parse_timeout_code_activates_gpu_cooldown(self) -> None:
        report = _report(failed=1)
        report.failures.append(WorkerFailure("parse", "doc_timeout", "parse_timeout"))

        self.controller.observe(report, now=10.0)

        self.assertEqual(
            self.controller.effective_limits(self.limits, now=11.0).parse,
            0,
        )

    def test_partial_batch_gpu_outage_still_activates_cooldown(self) -> None:
        report = _report(failed=7, parsed=1)
        report.failures.extend(
            WorkerFailure("parse", f"doc_{index}", "parser_invocation_failed")
            for index in range(7)
        )

        self.assertEqual(self.controller.observe(report, now=10.0), 0)
        self.assertEqual(
            self.controller.effective_limits(self.limits, now=11.0).parse,
            0,
        )

    def test_source_outage_cools_source_but_not_local_parse(self) -> None:
        report = _report(failed=1, parsed=1)
        report.failures.append(WorkerFailure("source", "cninfo", "ConfigurationError"))

        self.assertEqual(self.controller.observe(report, now=10.0), 0)
        cooled = self.controller.effective_limits(self.limits, now=11.0)
        self.assertEqual(cooled.sync, 0)
        self.assertEqual(cooled.download, 0)
        self.assertEqual(cooled.parse, 50)

    def test_wrapped_provider_outage_cools_source_even_with_parse_progress(self) -> None:
        report = _report(failed=13, parsed=1)
        report.failures.extend(
            WorkerFailure("sync", f"company_{index}", "http_503", True)
            for index in range(13)
        )

        self.assertEqual(self.controller.observe(report, now=10.0), 0)
        cooled = self.controller.effective_limits(self.limits, now=11.0)
        self.assertEqual(cooled.sync, 0)
        self.assertEqual(cooled.download, 0)
        self.assertEqual(cooled.parse, 50)

    def test_publish_failure_cools_publish_and_parse_even_with_other_progress(self) -> None:
        report = _report(failed=1, downloaded=1, parsed=1, built=1)
        report.failures.append(WorkerFailure("publish", "run_1", "database_down"))

        self.assertEqual(self.controller.observe(report, now=10.0), 0)
        cooled = self.controller.effective_limits(self.limits, now=11.0)
        self.assertEqual(cooled.parse, 0)
        self.assertEqual(cooled.publish, 0)
        self.assertEqual(cooled.download, 300)

    def test_build_failure_cools_build_and_parse_even_with_parse_progress(self) -> None:
        report = _report(failed=1, parsed=1)
        report.failures.append(WorkerFailure("build", "run_1", "DB_WRITE_FAILED"))

        self.assertEqual(self.controller.observe(report, now=10.0), 0)
        cooled = self.controller.effective_limits(self.limits, now=11.0)
        self.assertEqual(cooled.parse, 0)
        self.assertEqual(cooled.build, 0)
        self.assertEqual(cooled.download, 300)
        # When cooldown expires, one build-only round proves recovery before
        # new PDFs may consume GPU and leave the admission watermark.
        probe = self.controller.effective_limits(self.limits, now=130.0)
        self.assertEqual(probe.parse, 0)
        self.assertEqual(probe.build, 10)
        self.assertEqual(self.controller.observe(_report(), now=130.0), 0)
        self.assertEqual(
            self.controller.effective_limits(self.limits, now=131.0).parse,
            50,
        )

    def test_item_local_build_poison_does_not_cool_global_stages(self) -> None:
        report = _report(failed=1, parsed=1)
        report.failures.append(WorkerFailure("build", "run_1", "IR_MISSING"))

        self.assertEqual(self.controller.observe(report, now=10.0), 0)
        self.assertEqual(
            self.controller.effective_limits(self.limits, now=11.0),
            self.limits,
        )

    def test_repeated_unknown_build_failure_activates_global_cooldown(self) -> None:
        report = _report(failed=2, parsed=2)
        report.failures.extend(
            (
                WorkerFailure("build", "run_1", "RuntimeError"),
                WorkerFailure("build", "run_2", "RuntimeError"),
            )
        )

        self.assertEqual(self.controller.observe(report, now=10.0), 0)
        cooled = self.controller.effective_limits(self.limits, now=11.0)
        self.assertEqual(cooled.parse, 0)
        self.assertEqual(cooled.build, 0)

    def test_system_errors_exponentially_back_off(self) -> None:
        self.assertEqual(self.controller.system_error_delay(), 60)
        self.assertEqual(self.controller.system_error_delay(), 120)

    def test_successful_idle_round_resets_system_error_backoff(self) -> None:
        self.controller.system_error_delay()
        self.controller.system_error_delay()

        self.controller.observe(_report(), now=100.0)

        self.assertEqual(self.controller.system_error_delay(), 60)


class ResidentLoopBoundaryTests(unittest.TestCase):
    def test_system_error_is_reported_and_next_round_runs(self) -> None:
        stop = mock.MagicMock()
        engine = mock.MagicMock()
        success = _report(downloaded=1)
        emitted: list[WorkerReport] = []
        sleeps: list[float] = []
        deps = mock.MagicMock()

        with (
            mock.patch.object(worker_cli, "_StopFlag", return_value=stop),
            mock.patch.object(worker_cli, "create_db_engine", return_value=engine),
            mock.patch.object(worker_cli, "_deps", return_value=deps),
            mock.patch.object(worker_cli, "_assert_singleton_lock"),
            mock.patch.object(
                worker_cli, "run_once", side_effect=[RuntimeError("db down"), success]
            ) as run_once,
            mock.patch.object(
                worker_cli, "_append_reports", side_effect=lambda _settings, report: emitted.append(report)
            ),
            mock.patch.object(worker_cli, "_sleep_interruptible", side_effect=lambda seconds, **_: sleeps.append(seconds)),
            mock.patch.object(worker_cli.traceback, "print_exc"),
            mock.patch("builtins.print"),
        ):
            stop.is_set.side_effect = lambda: run_once.call_count >= 2
            result = worker_cli._run_loop(
                mock.MagicMock(
                    worker_loop_interval_seconds=900,
                    worker_loop_max_interval_seconds=1800,
                ),
                lock_conn=mock.MagicMock(),
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(emitted), 2)
        self.assertEqual(emitted[0].failures[0].stage, "system")
        self.assertEqual(emitted[0].failures[0].error_code, "RuntimeError")
        self.assertEqual(emitted[1].downloaded, 1)
        self.assertEqual(sleeps, [60.0])
        deps.close_source.assert_called_once_with()
        engine.dispose.assert_called_once()

    def test_report_io_failure_backs_off_without_exiting(self) -> None:
        stop = mock.MagicMock()
        engine = mock.MagicMock()
        deps = mock.MagicMock()
        success = _report(downloaded=1)
        sleeps: list[float] = []

        with (
            mock.patch.object(worker_cli, "_StopFlag", return_value=stop),
            mock.patch.object(worker_cli, "create_db_engine", return_value=engine),
            mock.patch.object(worker_cli, "_deps", return_value=deps),
            mock.patch.object(worker_cli, "_assert_singleton_lock"),
            mock.patch.object(worker_cli, "run_once", return_value=success) as run_once,
            mock.patch.object(
                worker_cli, "_append_reports", side_effect=OSError("disk full")
            ),
            mock.patch.object(
                worker_cli,
                "_sleep_interruptible",
                side_effect=lambda seconds, **_: sleeps.append(seconds),
            ),
            mock.patch.object(worker_cli.traceback, "print_exc"),
            mock.patch("builtins.print"),
        ):
            stop.is_set.side_effect = lambda: run_once.call_count >= 2
            result = worker_cli._run_loop(
                mock.MagicMock(
                    worker_loop_interval_seconds=900,
                    worker_loop_max_interval_seconds=1800,
                ),
                lock_conn=mock.MagicMock(),
            )

        self.assertEqual(result, 0)
        self.assertEqual(sleeps, [60.0])

    def test_production_deps_reuses_one_cninfo_client_across_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                disclosure_data_root=root / "service",
                disclosure_shared_root=root / "shared",
                disclosure_runtime_root=root / "service" / "runtime",
                mineru_model_cache=root / "shared" / "mineru",
                hf_home=root / "shared" / "hf",
                modelscope_cache=root / "shared" / "modelscope",
                cninfo_access_token="test-token",
            )
            client = mock.MagicMock()
            with mock.patch.object(
                worker_cli.CninfoClient, "from_settings", return_value=client
            ) as from_settings:
                deps = worker_cli._deps(settings, mock.MagicMock())
                first = deps.source_factory()
                second = deps.source_factory()
                deps.close_source()

        self.assertIs(first, second)
        from_settings.assert_called_once_with(settings)
        client.close.assert_called_once_with()

    def test_production_deps_reuses_parser_version_across_fresh_parsers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                disclosure_data_root=root / "service",
                disclosure_shared_root=root / "shared",
                disclosure_runtime_root=root / "service" / "runtime",
                mineru_model_cache=root / "shared" / "mineru",
                hf_home=root / "shared" / "hf",
                modelscope_cache=root / "shared" / "modelscope",
            )
            with mock.patch.object(
                worker_cli.MinerUProcess, "version", return_value="3.4.0"
            ) as version:
                deps = worker_cli._deps(settings, mock.MagicMock())
                first = deps.parser_factory()
                second = deps.parser_factory()

        self.assertEqual(first.identity().version, "3.4.0")
        self.assertEqual(second.identity().version, "3.4.0")
        version.assert_called_once_with()

    def test_lost_singleton_lock_fails_closed(self) -> None:
        lock_conn = mock.MagicMock()
        lock_conn.execute.return_value.scalar_one.return_value = False

        with self.assertRaisesRegex(RuntimeError, "singleton advisory lock was lost"):
            worker_cli._assert_singleton_lock(lock_conn)

    def test_signal_stops_refill_and_terminates_active_mineru_groups(self) -> None:
        stop = worker_cli._StopFlag()
        with mock.patch.object(
            worker_cli, "terminate_active_mineru_processes"
        ) as terminate:
            stop._handle(15, None)

        self.assertTrue(stop.is_set())
        terminate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
