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
from disclosure_anchor.application.ports.parser import ParserOptions
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
        self.assertEqual(
            self.controller.observe(_report(downloaded=1), now=10.0), 0
        )
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

    def test_source_outage_cools_source_but_not_local_parse(self) -> None:
        report = _report(failed=1)
        report.failures.append(WorkerFailure("source", "cninfo", "ConfigurationError"))

        self.assertEqual(self.controller.observe(report, now=10.0), 60)
        cooled = self.controller.effective_limits(self.limits, now=11.0)
        self.assertEqual(cooled.sync, 0)
        self.assertEqual(cooled.download, 0)
        self.assertEqual(cooled.parse, 50)

    def test_wrapped_provider_outage_cools_acquisition_only(self) -> None:
        report = _report(failed=13)
        report.failures.extend(
            WorkerFailure("sync", f"company_{index}", "http_503", True)
            for index in range(13)
        )

        self.assertEqual(self.controller.observe(report, now=10.0), 60)
        cooled = self.controller.effective_limits(self.limits, now=11.0)
        self.assertEqual(cooled.sync, 0)
        self.assertEqual(cooled.download, 0)
        self.assertEqual(cooled.parse, 50)

    def test_system_errors_exponentially_back_off(self) -> None:
        self.assertEqual(self.controller.system_error_delay(), 60)
        self.assertEqual(self.controller.system_error_delay(), 120)

    def test_successful_idle_round_resets_system_error_backoff(self) -> None:
        self.controller.system_error_delay()
        self.controller.system_error_delay()

        self.controller.observe(_report(), now=100.0)

        self.assertEqual(self.controller.system_error_delay(), 60)


class ResidentLoopBoundaryTests(unittest.TestCase):
    def test_startup_recovery_retries_before_first_admission(self) -> None:
        settings = mock.MagicMock(worker_loop_max_interval_seconds=1800)
        projection_failed = _report(failed=1)
        projection_failed.failures.append(
            WorkerFailure(
                "project",
                "search_projection",
                "RuntimeError",
                True,
            )
        )
        recovered = _report(built=1)
        drained = _report()
        deps = mock.MagicMock()
        reports: worker_cli.queue.SimpleQueue[WorkerReport | None] = (
            worker_cli.queue.SimpleQueue()
        )

        with (
            mock.patch.object(worker_cli, "_assert_singleton_or_cancel"),
            mock.patch.object(
                worker_cli,
                "run_once",
                side_effect=[
                    RuntimeError("db down"),
                    projection_failed,
                    recovered,
                    drained,
                ],
            ) as run_once,
            mock.patch.object(worker_cli, "_wait_while") as wait,
            mock.patch.object(worker_cli.traceback, "print_exc"),
        ):
            worker_cli._run_startup_recovery(
                settings,
                lock_conn=mock.MagicMock(),
                deps=deps,
                base_limits=WorkerLimits(
                    sync=1, download=1, parse=2, build=3, publish=4
                ),
                should_stop=lambda: False,
                reports=reports,
            )

        first = reports.get()
        second = reports.get()
        third = reports.get()
        fourth = reports.get()
        assert (
            first is not None
            and second is not None
            and third is not None
            and fourth is not None
        )
        self.assertEqual(first.failures[0].stage, "system")
        self.assertIs(second, projection_failed)
        self.assertIs(third, recovered)
        self.assertIs(fourth, drained)
        self.assertEqual(run_once.call_count, 4)
        self.assertEqual(
            [
                call.kwargs["reclaim_stale"]
                for call in run_once.call_args_list
            ],
            [True, True, False, False],
        )
        self.assertEqual(
            [
                call.kwargs["stale_threshold_seconds"]
                for call in run_once.call_args_list
            ],
            [0, 0, None, None],
        )
        self.assertEqual(
            [
                call.kwargs["run_projection"]
                for call in run_once.call_args_list
            ],
            [True, True, True, True],
        )
        self.assertEqual(
            [
                call.kwargs["projection_prune"]
                for call in run_once.call_args_list
            ],
            [True, True, True, False],
        )
        self.assertEqual(wait.call_count, 2)

    def test_report_io_failure_does_not_stop_later_reports(self) -> None:
        reports: worker_cli.queue.SimpleQueue[WorkerReport | None] = (
            worker_cli.queue.SimpleQueue()
        )
        first = _report()
        first.runs_deactivated = 2
        second = _report(downloaded=1)
        reports.put(first)
        reports.put(second)
        reports.put(None)
        tracker = worker_cli._ProjectionPruneTracker()

        with (
            mock.patch.object(
                worker_cli,
                "_append_reports",
                side_effect=[OSError("disk full"), None],
            ) as append,
            mock.patch.object(worker_cli, "_maybe_alert"),
            mock.patch.object(worker_cli.traceback, "print_exc"),
            mock.patch("builtins.print"),
        ):
            worker_cli._report_writer(
                settings=mock.MagicMock(),
                reports=reports,
                prune_tracker=tracker,
            )

        self.assertEqual(append.call_count, 2)
        self.assertEqual(tracker.pending_generation(), 2)

    def test_maintenance_projects_without_reclaim_or_finalize_ownership(
        self,
    ) -> None:
        reports: worker_cli.queue.SimpleQueue[WorkerReport | None] = (
            worker_cli.queue.SimpleQueue()
        )
        tracker = worker_cli._ProjectionPruneTracker()
        tracker.mark(3)
        work_available = worker_cli.threading.Event()
        deps = mock.MagicMock()
        report = _report(downloaded=1)
        should_stop = mock.Mock(side_effect=[False, False, True])

        with mock.patch.object(
            worker_cli, "run_once", return_value=report
        ) as run_once:
            worker_cli._run_maintenance_loop(
                mock.MagicMock(
                    worker_loop_interval_seconds=1,
                    worker_loop_max_interval_seconds=2,
                ),
                deps=deps,
                base_limits=WorkerLimits(
                    sync=1, download=2, parse=3, build=4, publish=5
                ),
                should_stop=should_stop,
                reports=reports,
                prune_tracker=tracker,
                work_available=work_available,
            )

        limits = run_once.call_args.args[0]
        self.assertEqual((limits.parse, limits.build, limits.publish), (0, 0, 0))
        self.assertFalse(run_once.call_args.kwargs["reclaim_stale"])
        self.assertTrue(run_once.call_args.kwargs["run_projection"])
        self.assertTrue(run_once.call_args.kwargs["projection_prune"])
        self.assertIs(reports.get(), report)
        self.assertTrue(work_available.is_set())
        self.assertEqual(tracker.pending_generation(), 0)

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
                # The test pins the API-client lifecycle; the ambient
                # DISCLOSURE_SYNC_CHANNEL=web of a backfill deployment would
                # otherwise route source_factory away from CninfoClient.
                disclosure_sync_channel="api",
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
                disclosure_mineru_backend="vlm-http-client",
                disclosure_mineru_server_url="http://127.0.0.1:30000",
                worker_parse_concurrency=16,
                worker_gpu_request_budget=112,
                worker_gpu_max_sequences=128,
            )
            with mock.patch.object(
                worker_cli.MinerUProcess, "version", return_value="3.4.0"
            ) as version:
                deps = worker_cli._deps(settings, mock.MagicMock())
                first = deps.parser_factory()
                second = deps.parser_factory()

        self.assertEqual(first.identity().version, "3.4.0")
        self.assertEqual(second.identity().version, "3.4.0")
        self.assertEqual(deps.parser_options.http_request_concurrency, 7)
        self.assertEqual(deps.config.parse_runaway_timeout_seconds, 86400)
        with mock.patch.object(worker_cli, "_exit_wedged_worker") as exit_worker:
            deps.on_parse_runaway("doc_wedged")
        exit_worker.assert_called_once_with()
        version.assert_called_once_with()

    def test_lost_singleton_lock_fails_closed(self) -> None:
        lock_conn = mock.MagicMock()
        lock_conn.execute.return_value.scalar_one.return_value = False

        with self.assertRaisesRegex(RuntimeError, "singleton advisory lock was lost"):
            worker_cli._assert_singleton_lock(lock_conn)

    def test_lost_singleton_lock_cancels_active_mineru_before_raising(self) -> None:
        lock_conn = mock.MagicMock()
        lock_conn.execute.return_value.scalar_one.return_value = False

        with (
            mock.patch.object(
                worker_cli, "terminate_active_mineru_processes"
            ) as terminate,
            self.assertRaisesRegex(
                RuntimeError, "singleton advisory lock was lost"
            ),
        ):
            worker_cli._assert_singleton_or_cancel(lock_conn)

        terminate.assert_called_once_with()

    def test_resident_exits_after_process_lifetime_shutdown_latch(self) -> None:
        stop = mock.MagicMock()
        stop.is_set.return_value = False
        engine = mock.MagicMock()
        deps = mock.MagicMock()
        settings = mock.MagicMock(
            worker_loop_interval_seconds=900,
            worker_loop_max_interval_seconds=1800,
            worker_wedge_timeout_seconds=0,
        )
        with (
            mock.patch.object(worker_cli, "_StopFlag", return_value=stop),
            mock.patch.object(
                worker_cli, "create_db_engine", return_value=engine
            ),
            mock.patch.object(worker_cli, "_deps", return_value=deps),
            mock.patch.object(
                worker_cli,
                "_run_startup_recovery",
                side_effect=worker_cli.WorkerSingletonGuardError(
                    "singleton advisory lock was lost"
                ),
            ),
            self.assertRaises(worker_cli.WorkerSingletonGuardError),
        ):
            worker_cli._run_loop(settings, lock_conn=mock.MagicMock())

        deps.close_source.assert_called_once_with()
        engine.dispose.assert_called_once_with()

    def test_maintenance_heartbeat_cannot_mask_parse_plane(self) -> None:
        import itertools
        import threading

        engine = mock.MagicMock()
        close_source = mock.Mock()
        deps = worker_cli.WorkerDeps(
            engine=engine,
            uow_factory=lambda: mock.MagicMock(),
            path_builder=mock.MagicMock(),
            raw_store=mock.MagicMock(),
            artifact_store=mock.MagicMock(),
            source_evidence_validator=mock.MagicMock(),
            source_factory=lambda: mock.MagicMock(),
            profile_loader_factory=lambda _source: lambda _code: None,
            parser_factory=lambda: mock.MagicMock(),
            parse_expected_seconds=1,
            config=mock.MagicMock(),
            parser_options=ParserOptions(
                runtime_bundle_identity_sha256="sha256:" + "b" * 64
            ),
            close_source=close_source,
        )
        stop = mock.MagicMock()
        stop.is_set.return_value = False
        settings = mock.MagicMock(
            worker_loop_interval_seconds=900,
            worker_loop_max_interval_seconds=1800,
            worker_report_interval_seconds=300,
            worker_wedge_timeout_seconds=2700,
        )
        limits = WorkerLimits(
            sync=1, download=1, parse=1, build=1, publish=1
        )
        progress: dict[str, list[float]] = {}
        initial_parse_progress: list[float] = []
        maintenance_pulsed = threading.Event()
        clocks = itertools.count(100)

        def watchdog(
            *,
            plane: str,
            last_progress: list[float],
            **_kwargs: object,
        ) -> mock.MagicMock:
            progress[plane] = last_progress
            if plane == "parse":
                initial_parse_progress.append(last_progress[0])
            return mock.MagicMock()

        def maintenance(
            *_args: object,
            deps: worker_cli.WorkerDeps,
            **_kwargs: object,
        ) -> None:
            deps.heartbeat()
            maintenance_pulsed.set()

        def resident(
            parse_deps: worker_cli.WorkerDeps,
            **_kwargs: object,
        ) -> None:
            self.assertTrue(maintenance_pulsed.wait(timeout=1))
            self.assertEqual(
                progress["parse"][0],
                initial_parse_progress[0],
                "maintenance progress must not renew parse ownership",
            )
            self.assertGreater(
                progress["maintenance"][0],
                initial_parse_progress[0],
            )
            parse_deps.heartbeat()
            self.assertGreater(
                progress["parse"][0], initial_parse_progress[0]
            )

        with (
            mock.patch.object(worker_cli, "_StopFlag", return_value=stop),
            mock.patch.object(
                worker_cli, "create_db_engine", return_value=engine
            ),
            mock.patch.object(worker_cli, "_deps", return_value=deps),
            mock.patch.object(worker_cli, "_limits", return_value=limits),
            mock.patch.object(worker_cli, "_run_startup_recovery"),
            mock.patch.object(
                worker_cli,
                "_run_maintenance_loop",
                side_effect=maintenance,
            ),
            mock.patch.object(
                worker_cli, "run_resident_parse", side_effect=resident
            ),
            mock.patch.object(
                worker_cli, "_wedge_watchdog", side_effect=watchdog
            ),
            mock.patch.object(
                worker_cli.time,
                "monotonic",
                side_effect=lambda: float(next(clocks)),
            ),
        ):
            result = worker_cli._run_loop(
                settings, lock_conn=mock.MagicMock()
            )

        self.assertEqual(result, 0)
        self.assertEqual(set(progress), {"parse", "maintenance"})
        close_source.assert_called_once_with()
        engine.dispose.assert_called_once_with()

    def test_alert_message_triggers_on_outage_and_failure_burst(self) -> None:
        # Single-operator alert channel (batch 4): fire on source outage or
        # >=5 failures in a round; stay quiet on ordinary rounds.
        quiet = WorkerReport(started_at=datetime.now(timezone.utc))
        quiet.failed = 4
        self.assertIsNone(worker_cli._alert_message(quiet))

        outage = WorkerReport(started_at=datetime.now(timezone.utc))
        outage.source_outage_break = True
        self.assertIn("outage", worker_cli._alert_message(outage))

        burst = WorkerReport(started_at=datetime.now(timezone.utc))
        burst.failed = 5
        burst.failures = [
            WorkerFailure(stage="parse", item_ref="doc_x", error_code="boom")
        ]
        message = worker_cli._alert_message(burst)
        self.assertIn("5 failures", message)
        self.assertIn("parse", message)

    def test_rate_limit_cooldown_decays_on_progress_instead_of_resetting(
        self,
    ) -> None:
        controller = worker_cli._AdaptiveLoopController(900, 1800)
        # Consecutive 429 trips escalate 90 -> 180 -> 360 -> 600 (cap).
        for now in (0.0, 200.0, 600.0):
            controller.observe(_report(sync_rate_limited=True), now=now)
        self.assertEqual(controller.rate_limit_cooldown_seconds, 600)

        # A trickle of synced companies inside a long throttle window must
        # not collapse the ladder back to base (it would hammer the wall
        # every ~90s); it decays by half instead.
        controller.observe(_report(synced_companies=2), now=1300.0)
        self.assertEqual(controller.rate_limit_cooldown_seconds, 300)
        controller.observe(_report(synced_companies=2), now=1400.0)
        self.assertEqual(controller.rate_limit_cooldown_seconds, 150)
        controller.observe(_report(synced_companies=2), now=1500.0)
        controller.observe(_report(synced_companies=2), now=1600.0)
        self.assertEqual(
            controller.rate_limit_cooldown_seconds,
            worker_cli.RATE_LIMIT_COOLDOWN_BASE_SECONDS,
        )

    def test_signal_stops_refill_and_terminates_active_mineru_groups(self) -> None:
        stop = worker_cli._StopFlag()
        with mock.patch.object(
            worker_cli, "terminate_active_mineru_processes"
        ) as terminate:
            stop._handle(15, None)

        self.assertTrue(stop.is_set())
        terminate.assert_called_once_with()

    def test_wedge_exit_terminates_parser_groups_before_hard_exit(self) -> None:
        events: list[object] = []
        with (
            mock.patch.object(
                worker_cli,
                "terminate_active_mineru_processes",
                side_effect=lambda: events.append("terminated") or 2,
            ),
            mock.patch(
                "os._exit",
                side_effect=lambda code: events.append(("exit", code)),
            ),
        ):
            worker_cli._exit_wedged_worker()

        self.assertEqual(events, ["terminated", ("exit", 70)])


if __name__ == "__main__":
    unittest.main()
