"""run_once scheduling/aggregation with faked queues (08 §5 unit scope)."""

from __future__ import annotations

from datetime import date, timedelta, datetime, timezone
import unittest
from unittest import mock

from disclosure_anchor.application.dto.worker_report import WorkerLimits
from disclosure_anchor.application.worker import worker as worker_module
from disclosure_anchor.application.worker.locks import stable_document_hash
from disclosure_anchor.application.worker.worker import (
    WorkerConfig,
    WorkerDeps,
    _sync_window_start,
    render_report_section,
    run_once,
)
from disclosure_anchor.domain.errors import (
    ParserVersionProbeError,
    PublishRunError,
    SourceRequestError,
)


def _config() -> WorkerConfig:
    return WorkerConfig(
        max_parse_retries=3,
        max_build_retries=3,
        stale_run_threshold_seconds=3600,
        sync_interval_seconds=86400,
        cninfo_overlap_days=7,
        cninfo_max_retries=3,
        cninfo_oversized_kb=10240,
    )


class _FakeEngineContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *args: object) -> None:
        return None


class _FakeEngine:
    def begin(self) -> _FakeEngineContext:
        return _FakeEngineContext()

    def connect(self) -> _FakeEngineContext:
        return _FakeEngineContext()


def _deps() -> WorkerDeps:
    return WorkerDeps(
        engine=_FakeEngine(),  # type: ignore[arg-type]
        uow_factory=lambda: mock.MagicMock(),
        path_builder=mock.MagicMock(),
        raw_store=mock.MagicMock(),
        artifact_store=mock.MagicMock(),
        source_factory=lambda: mock.MagicMock(),
        profile_loader_factory=lambda source: (lambda code: None),
        parser_factory=lambda: mock.MagicMock(),
        parse_timeout_seconds=1800,
        config=_config(),
        clock=lambda: datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc),
    )


class StableHashTests(unittest.TestCase):
    def test_folds_unsigned_crc32_into_signed_int4(self) -> None:
        import zlib

        high = next(
            f"doc_{i}" for i in range(10000) if zlib.crc32(f"doc_{i}".encode()) >= 2**31
        )
        low = next(
            f"doc_{i}" for i in range(10000) if zlib.crc32(f"doc_{i}".encode()) < 2**31
        )
        self.assertEqual(
            stable_document_hash(high), zlib.crc32(high.encode()) - 2**32
        )
        self.assertLess(stable_document_hash(high), 0)
        self.assertEqual(stable_document_hash(low), zlib.crc32(low.encode()))
        for value in (high, low, "doc_01KWSGSEQQ23R18VG6ER9ZPSGG"):
            self.assertTrue(-(2**31) <= stable_document_hash(value) < 2**31)


class SyncWindowStartTests(unittest.TestCase):
    def test_existing_cursor_looks_back_overlap_days(self) -> None:
        self.assertEqual(
            _sync_window_start("2026-07-01", today=date(2026, 7, 6), overlap_days=7),
            date(2026, 6, 24),
        )

    def test_missing_cursor_defaults_to_initial_backfill(self) -> None:
        # User decision 2026-07-06: 三年是底线 — a never-synced company backfills
        # the full initial window, not the incremental overlap.
        self.assertEqual(
            _sync_window_start(
                None, today=date(2026, 7, 6), overlap_days=7,
                initial_lookback_days=1095,
            ),
            date(2026, 7, 6) - timedelta(days=1095),
        )

    def test_missing_cursor_honors_per_company_lookback_override(self) -> None:
        self.assertEqual(
            _sync_window_start(
                None, today=date(2026, 7, 6), overlap_days=7,
                initial_lookback_days=1095, lookback={"days": 30},
            ),
            date(2026, 6, 6),
        )
        # Malformed override falls back to the default, never crashes.
        self.assertEqual(
            _sync_window_start(
                None, today=date(2026, 7, 6), overlap_days=7,
                initial_lookback_days=10, lookback={"days": "soon"},
            ),
            date(2026, 6, 26),
        )


class RunOnceSchedulingTests(unittest.TestCase):
    def test_source_factory_failure_does_not_block_local_parse_chain(self) -> None:
        deps = _deps()
        object.__setattr__(
            deps,
            "source_factory",
            mock.Mock(side_effect=RuntimeError("credentials unavailable")),
        )
        parsed = mock.MagicMock(status="succeeded", processing_run_id="run_local")
        built = mock.MagicMock(status="succeeded", build_stats=None)
        published = mock.MagicMock(status="published")
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(
                worker_module.queries,
                "sync_due",
                return_value=[
                    {
                        "tracked_company_id": "tc_local",
                        "company_id": "co_local",
                        "security_id": "sec_local",
                        "security_code": "000001",
                        "exchange": "SZSE",
                    }
                ],
            ),
            mock.patch.object(
                worker_module.queries,
                "pending_parse",
                return_value=[{"document_id": "doc_local", "oversized": False}],
            ),
            mock.patch.object(worker_module, "ParseDocument") as parse_cls,
            mock.patch.object(worker_module, "BuildUnits") as build_cls,
            mock.patch.object(worker_module, "PublishRun") as publish_cls,
        ):
            parse_cls.return_value.execute.return_value = parsed
            build_cls.return_value.execute.return_value = built
            publish_cls.return_value.execute.return_value = published
            report = run_once(
                WorkerLimits(sync=1, download=1, parse=1, build=0, publish=0),
                deps,
            )

        self.assertEqual(report.parsed, 1)
        self.assertEqual(report.published, 1)
        self.assertIn("source", [failure.stage for failure in report.failures])

    def test_oversized_documents_are_skipped_and_counted(self) -> None:
        deps = _deps()
        pending = [
            {"document_id": "doc_big", "oversized": True},
            {"document_id": "doc_ok", "oversized": False},
        ]
        parse_result = mock.MagicMock(status="succeeded", processing_run_id="run_1")
        build_result = mock.MagicMock(
            status="succeeded", build_stats={"generated_by_kind": {"text": 1}}
        )
        publish_result = mock.MagicMock(status="published")
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "pending_parse", return_value=pending),
            mock.patch.object(worker_module, "ParseDocument") as parse_cls,
            mock.patch.object(worker_module, "BuildUnits") as build_cls,
            mock.patch.object(worker_module, "PublishRun") as publish_cls,
        ):
            parse_cls.return_value.execute.return_value = parse_result
            build_cls.return_value.execute.return_value = build_result
            publish_cls.return_value.execute.return_value = publish_result
            report = run_once(
                WorkerLimits(sync=0, download=0, parse=5, build=0, publish=0), deps
            )

        self.assertEqual(report.skipped_oversized, 1)
        self.assertEqual(report.parsed, 1)
        self.assertEqual(report.built, 1)
        self.assertEqual(report.published, 1)
        self.assertEqual(report.failed, 0)
        parse_cls.return_value.execute.assert_called_once()

    def test_one_bad_document_does_not_kill_the_round(self) -> None:
        deps = _deps()
        pending = [
            {"document_id": "doc_bad", "oversized": False},
            {"document_id": "doc_good", "oversized": False},
        ]
        good = mock.MagicMock(status="succeeded", processing_run_id="run_good")
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=2),
            mock.patch.object(worker_module.queries, "pending_parse", return_value=pending),
            mock.patch.object(worker_module, "ParseDocument") as parse_cls,
            mock.patch.object(worker_module, "BuildUnits") as build_cls,
            mock.patch.object(worker_module, "PublishRun") as publish_cls,
        ):
            parse_cls.return_value.execute.side_effect = [
                RuntimeError("boom"),
                good,
            ]
            build_cls.return_value.execute.return_value = mock.MagicMock(
                status="succeeded", build_stats=None
            )
            publish_cls.return_value.execute.return_value = mock.MagicMock(
                status="published"
            )
            report = run_once(
                WorkerLimits(sync=0, download=0, parse=5, build=0, publish=0), deps
            )

        self.assertEqual(report.stale_reclaimed, 2)
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.parsed, 1)
        self.assertEqual(report.failures[0].stage, "parse")
        self.assertEqual(report.failures[0].item_ref, "doc_bad")
        self.assertEqual(report.failures[0].error_code, "RuntimeError")

    def test_publish_error_is_attributed_to_publish_stage(self) -> None:
        deps = _deps()
        parsed = mock.MagicMock(status="succeeded", processing_run_id="run_empty")
        built = mock.MagicMock(status="succeeded", build_stats=None)
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(
                worker_module.queries,
                "pending_parse",
                return_value=[{"document_id": "doc_empty", "oversized": False}],
            ),
            mock.patch.object(worker_module, "ParseDocument") as parse_cls,
            mock.patch.object(worker_module, "BuildUnits") as build_cls,
            mock.patch.object(worker_module, "PublishRun") as publish_cls,
        ):
            parse_cls.return_value.execute.return_value = parsed
            build_cls.return_value.execute.return_value = built
            publish_cls.return_value.execute.side_effect = PublishRunError(
                {"error_code": "EMPTY_RUN", "retryable": False}
            )
            report = run_once(
                WorkerLimits(sync=0, download=0, parse=1, build=0, publish=0), deps
            )

        self.assertEqual(report.parsed, 1)
        self.assertEqual(report.built, 1)
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.failures[0].stage, "publish")
        self.assertEqual(report.failures[0].error_code, "EMPTY_RUN")

    def test_stop_submits_at_most_parse_concurrency(self) -> None:
        import threading

        deps = _deps()
        object.__setattr__(
            deps,
            "config",
            WorkerConfig(
                max_parse_retries=3,
                max_build_retries=3,
                stale_run_threshold_seconds=3600,
                sync_interval_seconds=86400,
                cninfo_overlap_days=7,
                cninfo_max_retries=3,
                cninfo_oversized_kb=10240,
                parse_concurrency=2,
            ),
        )
        stop = threading.Event()
        pending = [
            {"document_id": f"doc_{index}", "oversized": False}
            for index in range(20)
        ]

        def stop_after_start(_deps, _document_id):  # noqa: ANN001, ANN202
            stop.set()
            return worker_module._DocOutcome()

        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "pending_parse", return_value=pending),
            mock.patch.object(
                worker_module, "_process_one_document", side_effect=stop_after_start
            ) as process_one,
        ):
            run_once(
                WorkerLimits(sync=0, download=0, parse=20, build=0, publish=0),
                deps,
                should_stop=stop.is_set,
            )

        self.assertLessEqual(process_one.call_count, 2)

    def test_parser_identity_outage_consumes_no_document_attempts(self) -> None:
        deps = _deps()
        object.__setattr__(
            deps,
            "config",
            WorkerConfig(
                max_parse_retries=3,
                max_build_retries=3,
                stale_run_threshold_seconds=3600,
                sync_interval_seconds=86400,
                cninfo_overlap_days=7,
                cninfo_max_retries=3,
                cninfo_oversized_kb=10240,
                parse_concurrency=2,
            ),
        )
        pending = [
            {"document_id": f"doc_{index}", "oversized": False}
            for index in range(20)
        ]
        parser = mock.MagicMock()
        parser.identity.side_effect = ParserVersionProbeError("mineru missing")
        object.__setattr__(deps, "parser_factory", lambda: parser)
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "pending_parse", return_value=pending),
            mock.patch.object(worker_module, "ParseDocument") as parse_cls,
        ):
            report = run_once(
                WorkerLimits(sync=0, download=0, parse=20, build=0, publish=0),
                deps,
            )

        parse_cls.assert_not_called()
        parser.identity.assert_called_once_with()
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.failures[0].item_ref, "parser")

    def test_build_failure_stops_parse_refill(self) -> None:
        deps = _deps()
        pending = [
            {"document_id": f"doc_{index}", "oversized": False}
            for index in range(3)
        ]
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "pending_parse", return_value=pending),
            mock.patch.object(worker_module, "ParseDocument") as parse_cls,
            mock.patch.object(worker_module, "BuildUnits") as build_cls,
        ):
            parse_cls.return_value.execute.return_value = mock.MagicMock(
                status="succeeded", processing_run_id="run_1"
            )
            build_cls.return_value.execute.return_value = mock.MagicMock(
                status="failed",
                error={"error_code": "DB_WRITE_FAILED", "retryable": False},
            )
            report = run_once(
                WorkerLimits(sync=0, download=0, parse=3, build=0, publish=0),
                deps,
            )

        parse_cls.return_value.execute.assert_called_once()
        self.assertEqual(report.parsed, 1)
        self.assertEqual(report.failures[0].stage, "build")

    def test_item_local_build_failure_does_not_stop_parse_refill(self) -> None:
        deps = _deps()
        pending = [
            {"document_id": f"doc_{index}", "oversized": False}
            for index in range(3)
        ]
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "pending_parse", return_value=pending),
            mock.patch.object(worker_module, "ParseDocument") as parse_cls,
            mock.patch.object(worker_module, "BuildUnits") as build_cls,
        ):
            parse_cls.return_value.execute.return_value = mock.MagicMock(
                status="succeeded", processing_run_id="run_local_poison"
            )
            build_cls.return_value.execute.return_value = mock.MagicMock(
                status="failed",
                error={"error_code": "IR_MISSING", "retryable": False},
            )
            report = run_once(
                WorkerLimits(sync=0, download=0, parse=3, build=0, publish=0),
                deps,
            )

        self.assertEqual(parse_cls.return_value.execute.call_count, 3)
        self.assertEqual(report.parsed, 3)
        self.assertEqual(report.failed, 3)

    def test_repeated_unknown_build_failure_stops_after_batch_evidence(self) -> None:
        deps = _deps()
        pending = [
            {"document_id": f"doc_{index}", "oversized": False}
            for index in range(3)
        ]
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "pending_parse", return_value=pending),
            mock.patch.object(worker_module, "ParseDocument") as parse_cls,
            mock.patch.object(worker_module, "BuildUnits") as build_cls,
        ):
            parse_cls.return_value.execute.return_value = mock.MagicMock(
                status="succeeded", processing_run_id="run_unknown"
            )
            build_cls.return_value.execute.side_effect = RuntimeError("regression")
            report = run_once(
                WorkerLimits(sync=0, download=0, parse=3, build=0, publish=0),
                deps,
            )

        self.assertEqual(parse_cls.return_value.execute.call_count, 2)
        self.assertEqual(build_cls.return_value.execute.call_count, 2)
        self.assertEqual([failure.error_code for failure in report.failures], [
            "RuntimeError",
            "RuntimeError",
        ])

    def test_parse_concurrency_runs_document_chains_in_parallel(self) -> None:
        import threading

        deps = _deps()
        object.__setattr__(
            deps,
            "config",
            WorkerConfig(
                max_parse_retries=3,
                max_build_retries=3,
                stale_run_threshold_seconds=3600,
                sync_interval_seconds=86400,
                cninfo_overlap_days=7,
                cninfo_max_retries=3,
                cninfo_oversized_kb=10240,
                parse_concurrency=3,
            ),
        )
        pending = [
            {"document_id": f"doc_{i}", "oversized": False} for i in range(3)
        ]
        # The barrier only releases when all 3 chains are inside execute()
        # simultaneously — proof of parallelism, not just completion.
        barrier = threading.Barrier(3, timeout=10)

        def blocking_execute(command):  # noqa: ANN001, ANN202
            barrier.wait()
            return mock.MagicMock(
                status="succeeded", processing_run_id=f"run_{command.document_id}"
            )

        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "pending_parse", return_value=pending),
            mock.patch.object(worker_module, "ParseDocument") as parse_cls,
            mock.patch.object(worker_module, "BuildUnits") as build_cls,
            mock.patch.object(worker_module, "PublishRun") as publish_cls,
        ):
            parse_cls.return_value.execute.side_effect = blocking_execute
            build_cls.return_value.execute.return_value = mock.MagicMock(
                status="succeeded", build_stats=None
            )
            publish_cls.return_value.execute.return_value = mock.MagicMock(
                status="published"
            )
            report = run_once(
                WorkerLimits(sync=0, download=0, parse=5, build=0, publish=0), deps
            )

        self.assertEqual(report.parsed, 3)
        self.assertEqual(report.built, 3)
        self.assertEqual(report.published, 3)
        self.assertEqual(report.failed, 0)

    def test_retryable_false_parse_failure_recorded_with_error_code(self) -> None:
        deps = _deps()
        failed = mock.MagicMock(
            status="failed",
            processing_run_id="run_x",
            error={"error_code": "OUTPUT_CONTRACT", "retryable": False},
        )
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(
                worker_module.queries,
                "pending_parse",
                return_value=[{"document_id": "doc_x", "oversized": False}],
            ),
            mock.patch.object(worker_module, "ParseDocument") as parse_cls,
        ):
            parse_cls.return_value.execute.return_value = failed
            report = run_once(
                WorkerLimits(sync=0, download=0, parse=1, build=0, publish=0), deps
            )

        self.assertEqual(report.failed, 1)
        self.assertEqual(report.failures[0].error_code, "OUTPUT_CONTRACT")

    def test_report_section_renders_all_counters(self) -> None:
        deps = _deps()
        with mock.patch.object(
            worker_module.queries, "reclaim_stale_runs", return_value=1
        ):
            report = run_once(
                WorkerLimits(sync=0, download=0, parse=0, build=0, publish=0), deps
            )
        section = render_report_section(report)
        self.assertIn("## run 2026-07-06T00:00:00+00:00", section)
        self.assertIn("- stale_reclaimed: 1", section)
        self.assertIn("- skipped_oversized: 0", section)


if __name__ == "__main__":
    unittest.main()


class SyncStageProtectionTests(unittest.TestCase):
    def _run_sync(self, due, *, processing_backlog=0, sync_side_effect=None,
                  backfill_cap=2000):
        for index, row in enumerate(due):
            row.setdefault("tracked_company_id", f"tc_{index}")
            row.setdefault("company_id", f"co_{index}")
            row.setdefault("security_id", f"sec_{index}")
        deps = _deps()
        object.__setattr__(deps, "config", WorkerConfig(
            max_parse_retries=3, max_build_retries=3,
            stale_run_threshold_seconds=3600, sync_interval_seconds=86400,
            cninfo_overlap_days=7, cninfo_max_retries=3, cninfo_oversized_kb=10240,
            backfill_max_pending_downloads=backfill_cap,
        ))
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "sync_due", return_value=due),
            mock.patch.object(
                worker_module.queries,
                "pending_processing_backlog_count",
                return_value=processing_backlog,
            ),
            mock.patch.object(worker_module, "SyncDisclosureIndex") as sync_cls,
        ):
            if sync_side_effect is not None:
                sync_cls.return_value.execute.side_effect = sync_side_effect
            else:
                sync_cls.return_value.execute.return_value = mock.MagicMock(
                    candidate_count=1
                )
            report = run_once(
                WorkerLimits(sync=10, download=0, parse=0, build=0, publish=0), deps
            )
        return report, sync_cls

    def test_quota_error_trips_round_breaker(self) -> None:
        # edgartools guidance: on quota exhaustion stop the round instead of
        # burning quota on the remaining companies.
        class _Quota(Exception):
            error_code = "quota_exhausted"

        due = [
            {"security_code": "000001", "exchange": "SZSE", "window_end": "2026-07-01"},
            {"security_code": "000002", "exchange": "SZSE", "window_end": "2026-07-01"},
        ]
        report, sync_cls = self._run_sync(due, sync_side_effect=_Quota("429"))

        self.assertTrue(report.sync_quota_break)
        self.assertEqual(report.failed, 1)
        sync_cls.return_value.execute.assert_called_once()

    def test_wrapped_retryable_provider_error_is_preserved_for_controller(self) -> None:
        cause = SourceRequestError(
            "gateway down", error_code="http_503", retryable=True
        )
        wrapped = RuntimeError("sync failed")
        wrapped.__cause__ = cause
        due = [
            {
                "security_code": "000001",
                "exchange": "SZSE",
                "window_end": "2026-07-01",
            }
        ]

        report, _ = self._run_sync(due, sync_side_effect=wrapped)

        self.assertEqual(report.failures[0].error_code, "http_503")
        self.assertTrue(report.failures[0].retryable)
        self.assertTrue(report.source_outage_break)

    def test_provider_outage_stops_sync_batch_and_skips_download(self) -> None:
        cause = SourceRequestError(
            "gateway down", error_code="http_503", retryable=True
        )
        wrapped = RuntimeError("sync failed")
        wrapped.__cause__ = cause
        deps = _deps()
        due = [
            {
                "tracked_company_id": f"tc_{index}",
                "company_id": f"co_{index}",
                "security_id": f"sec_{index}",
                "security_code": f"00000{index}",
                "exchange": "SZSE",
                "window_end": "2026-07-01",
            }
            for index in range(1, 4)
        ]
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "sync_due", return_value=due),
            mock.patch.object(worker_module.queries, "pending_downloads") as downloads,
            mock.patch.object(worker_module, "SyncDisclosureIndex") as sync_cls,
        ):
            sync_cls.return_value.execute.side_effect = wrapped
            report = run_once(
                WorkerLimits(sync=3, download=50, parse=0, build=0, publish=0),
                deps,
            )

        self.assertTrue(report.source_outage_break)
        sync_cls.return_value.execute.assert_called_once()
        downloads.assert_not_called()

    def test_provider_outage_stops_download_batch(self) -> None:
        deps = _deps()
        pending = [
            {
                "provider_document_id": str(index),
                "candidate": {"provider_document_id": str(index)},
            }
            for index in range(3)
        ]
        failed = mock.MagicMock(
            document_id=None,
            error_code="transport_error",
            retryable=True,
            quarantine_reason=None,
        )
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "pending_downloads", return_value=pending),
            mock.patch.object(worker_module, "DownloadDocument") as download_cls,
        ):
            download_cls.return_value.execute.return_value = failed
            report = run_once(
                WorkerLimits(sync=0, download=3, parse=0, build=0, publish=0),
                deps,
            )

        self.assertTrue(report.source_outage_break)
        download_cls.return_value.execute.assert_called_once()

    def test_quota_break_skips_download_api_for_current_round(self) -> None:
        class _Quota(Exception):
            error_code = "quota_exhausted"

        deps = _deps()
        due = [
            {
                "tracked_company_id": "tc_quota",
                "company_id": "co_quota",
                "security_id": "sec_quota",
                "security_code": "000001",
                "exchange": "SZSE",
                "window_end": "2026-07-01",
            }
        ]
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "sync_due", return_value=due),
            mock.patch.object(worker_module.queries, "pending_downloads") as downloads,
            mock.patch.object(worker_module, "SyncDisclosureIndex") as sync_cls,
        ):
            sync_cls.return_value.execute.side_effect = _Quota("429")
            report = run_once(
                WorkerLimits(sync=1, download=50, parse=0, build=0, publish=0),
                deps,
            )

        self.assertTrue(report.sync_quota_break)
        downloads.assert_not_called()

    def test_never_synced_company_deferred_when_processing_backlog_saturated(self) -> None:
        due = [
            {"security_code": "000001", "exchange": "SZSE",
             "window_end": None, "last_synced_at": None},
            {"security_code": "600519", "exchange": "SSE",
             "window_end": "2026-07-01", "last_synced_at": "2026-07-01"},
        ]
        report, sync_cls = self._run_sync(due, processing_backlog=5000)

        # The backfill candidate defers; the incremental company still syncs.
        self.assertEqual(report.deferred_backfill, 1)
        self.assertEqual(report.synced_companies, 1)
        sync_cls.return_value.execute.assert_called_once()

    def test_backfill_watermark_scans_once_and_caches_conservative_growth(self) -> None:
        due = [
            {
                "security_code": f"00000{index}",
                "exchange": "SZSE",
                "window_end": None,
                "last_synced_at": None,
            }
            for index in range(1, 4)
        ]
        deps = _deps()
        object.__setattr__(
            deps,
            "config",
            WorkerConfig(
                max_parse_retries=3,
                max_build_retries=3,
                stale_run_threshold_seconds=3600,
                sync_interval_seconds=86400,
                cninfo_overlap_days=7,
                cninfo_max_retries=3,
                cninfo_oversized_kb=10240,
                backfill_max_pending_downloads=2000,
            ),
        )
        for index, row in enumerate(due):
            row.update(
                tracked_company_id=f"tc_refresh_{index}",
                company_id=f"co_refresh_{index}",
                security_id=f"sec_refresh_{index}",
            )
        with (
            mock.patch.object(worker_module.queries, "reclaim_stale_runs", return_value=0),
            mock.patch.object(worker_module.queries, "sync_due", return_value=due),
            mock.patch.object(
                worker_module.queries,
                "pending_processing_backlog_count",
                return_value=1999,
            ) as pending_count,
            mock.patch.object(worker_module, "SyncDisclosureIndex") as sync_cls,
        ):
            sync_cls.return_value.execute.return_value = mock.MagicMock(
                candidate_count=201
            )
            report = run_once(
                WorkerLimits(sync=3, download=0, parse=0, build=0, publish=0),
                deps,
            )

        self.assertEqual(report.synced_companies, 1)
        self.assertEqual(report.deferred_backfill, 2)
        self.assertEqual(pending_count.call_count, 1)
