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
    ConfigurationError,
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
    def test_parse_stage_overlaps_acquisition(self) -> None:
        # The acquisition thread (sync/download) must run beside the parse
        # stage: here sync blocks until parse has started, which deadlocks
        # under sequential stage order and completes only when pipelined.
        import threading

        deps = _deps()
        parse_started = threading.Event()
        acquisition_saw_parse: list[bool] = []

        def _sync_due(*args: object, **kwargs: object) -> list[dict[str, str]]:
            acquisition_saw_parse.append(parse_started.wait(timeout=5.0))
            return []

        parsed = mock.MagicMock(status="succeeded", processing_run_id="run_x")
        built = mock.MagicMock(status="succeeded", build_stats=None)
        published = mock.MagicMock(status="published")

        def _parse_execute(command: object) -> object:
            parse_started.set()
            return parsed

        # One-shot batch: the continuous-feed loop re-dequeues while the
        # acquisition thread lives, and the real queue excludes documents
        # with runs — a constant mock would replay the same document.
        batches = iter([[{"document_id": "doc_x", "oversized": False}]])

        with (
            mock.patch.object(
                worker_module.queries, "reclaim_stale_runs", return_value=0
            ),
            mock.patch.object(
                worker_module.queries, "sync_due", side_effect=_sync_due
            ),
            mock.patch.object(
                worker_module.queries,
                "pending_parse",
                side_effect=lambda *a, **k: next(batches, []),
            ),
            mock.patch.object(worker_module, "ParseDocument") as parse_cls,
            mock.patch.object(worker_module, "BuildUnits") as build_cls,
            mock.patch.object(worker_module, "PublishRun") as publish_cls,
        ):
            parse_cls.return_value.execute.side_effect = _parse_execute
            build_cls.return_value.execute.return_value = built
            publish_cls.return_value.execute.return_value = published
            report = run_once(
                WorkerLimits(sync=1, download=0, parse=1, build=0, publish=0),
                deps,
            )

        self.assertEqual(acquisition_saw_parse, [True])
        self.assertEqual(report.parsed, 1)

    def test_source_factory_failure_does_not_block_local_parse_chain(self) -> None:
        deps = _deps()
        object.__setattr__(
            deps,
            "source_factory",
            # Provider-family error (credentials/config) → "source" outage;
            # a local RuntimeError is covered by the test below (round23).
            mock.Mock(side_effect=ConfigurationError("credentials unavailable")),
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
        self.assertTrue(report.source_outage_break)

    def test_local_stage_crash_is_not_disguised_as_provider_outage(self) -> None:
        # A queue-read SQL/programming error is a LOCAL fault: it must be
        # stage-isolated and reported, but never flagged as a CNINFO outage
        # (round23 — wrong classification triggered provider backoff).
        deps = _deps()
        object.__setattr__(
            deps,
            "source_factory",
            mock.Mock(side_effect=RuntimeError("queue view exploded")),
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
        stages = [failure.stage for failure in report.failures]
        self.assertIn("source_local", stages)
        self.assertNotIn("source", stages)
        self.assertFalse(report.source_outage_break)
        local = next(f for f in report.failures if f.stage == "source_local")
        self.assertIn("queue view exploded", local.message or "")

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


class AcquisitionPumpTests(unittest.TestCase):
    """Round-internal sync+download pump (progress-gated, window-bounded)."""

    @staticmethod
    def _ok_download() -> mock.MagicMock:
        return mock.MagicMock(
            document_id="doc_pump",
            error_code=None,
            retryable=None,
            quarantine_reason=None,
        )

    @staticmethod
    def _pending_row(pdid: str) -> dict:
        return {
            "provider_document_id": pdid,
            "candidate": {"provider_document_id": pdid},
        }

    def test_pump_repeats_download_batches_until_queue_drains(self) -> None:
        deps = _deps()
        batches = [
            [self._pending_row("1")],
            [self._pending_row("2")],
            [],
        ]
        with (
            mock.patch.object(
                worker_module.queries, "reclaim_stale_runs", return_value=0
            ),
            mock.patch.object(
                worker_module.queries, "pending_downloads", side_effect=batches
            ) as pending,
            mock.patch.object(worker_module, "DownloadDocument") as download_cls,
        ):
            download_cls.return_value.execute.return_value = self._ok_download()
            report = run_once(
                WorkerLimits(
                    sync=0,
                    download=1,
                    parse=0,
                    build=0,
                    publish=0,
                    acquisition_seconds=3600,
                ),
                deps,
            )

        # Two productive passes, then the empty pass ends the pump early —
        # the window deadline is a bound, not a hold-open.
        self.assertEqual(report.downloaded, 2)
        self.assertEqual(pending.call_count, 3)

    def test_zero_window_keeps_legacy_single_pass(self) -> None:
        deps = _deps()
        with (
            mock.patch.object(
                worker_module.queries, "reclaim_stale_runs", return_value=0
            ),
            mock.patch.object(
                worker_module.queries,
                "pending_downloads",
                return_value=[self._pending_row("1")],
            ) as pending,
            mock.patch.object(worker_module, "DownloadDocument") as download_cls,
        ):
            download_cls.return_value.execute.return_value = self._ok_download()
            report = run_once(
                WorkerLimits(sync=0, download=1, parse=0, build=0, publish=0),
                deps,
            )

        self.assertEqual(report.downloaded, 1)
        self.assertEqual(pending.call_count, 1)

    def test_rate_limited_sync_skipped_while_download_pump_continues(self) -> None:
        class _Rate(Exception):
            error_code = "rate_limited"

        deps = _deps()
        due = [
            {
                "tracked_company_id": "tc_pump",
                "company_id": "co_pump",
                "security_id": "sec_pump",
                "security_code": "000001",
                "exchange": "SZSE",
                "window_end": "2026-07-01",
            }
        ]
        batches = [[self._pending_row("1")], []]
        with (
            mock.patch.object(
                worker_module.queries, "reclaim_stale_runs", return_value=0
            ),
            mock.patch.object(
                worker_module.queries, "sync_due", return_value=due
            ) as sync_due,
            mock.patch.object(
                worker_module.queries, "pending_downloads", side_effect=batches
            ) as pending,
            mock.patch.object(worker_module, "SyncDisclosureIndex") as sync_cls,
            mock.patch.object(worker_module, "DownloadDocument") as download_cls,
        ):
            sync_cls.return_value.execute.side_effect = _Rate("slow down")
            download_cls.return_value.execute.return_value = self._ok_download()
            report = run_once(
                WorkerLimits(
                    sync=1,
                    download=1,
                    parse=0,
                    build=0,
                    publish=0,
                    acquisition_seconds=3600,
                ),
                deps,
            )

        # Pass 1 trips the rate limit; pass 2 must not touch CNINFO sync
        # again this round, while the download pump keeps going.
        self.assertTrue(report.sync_rate_limited)
        self.assertEqual(sync_due.call_count, 1)
        sync_cls.return_value.execute.assert_called_once()
        self.assertEqual(report.downloaded, 1)
        self.assertEqual(pending.call_count, 2)

    def test_window_deadline_bounds_pump_despite_endless_supply(self) -> None:
        deps = _deps()
        counter = iter(range(0, 10**9, 100))
        with (
            mock.patch.object(
                worker_module.queries, "reclaim_stale_runs", return_value=0
            ),
            mock.patch.object(
                worker_module.queries,
                "pending_downloads",
                side_effect=lambda *a, **k: [self._pending_row("again")],
            ) as pending,
            mock.patch.object(worker_module, "DownloadDocument") as download_cls,
            mock.patch.object(
                worker_module.time,
                "monotonic",
                side_effect=lambda: float(next(counter)),
            ),
        ):
            download_cls.return_value.execute.return_value = self._ok_download()
            report = run_once(
                WorkerLimits(
                    sync=0,
                    download=1,
                    parse=0,
                    build=0,
                    publish=0,
                    acquisition_seconds=150,
                ),
                deps,
            )

        # monotonic advances 100s per call and the window is 150s: the pump
        # must stop after very few passes even though the queue never dries.
        self.assertLess(pending.call_count, 4)
        self.assertGreaterEqual(report.downloaded, 1)

    def test_failures_only_download_pass_ends_pump(self) -> None:
        deps = _deps()
        failed = mock.MagicMock(
            document_id=None,
            error_code="download_failed",
            retryable=False,
            quarantine_reason=None,
        )
        with (
            mock.patch.object(
                worker_module.queries, "reclaim_stale_runs", return_value=0
            ),
            mock.patch.object(
                worker_module.queries,
                "pending_downloads",
                side_effect=lambda *a, **k: [self._pending_row("stuck")],
            ) as pending,
            mock.patch.object(worker_module, "DownloadDocument") as download_cls,
        ):
            download_cls.return_value.execute.return_value = failed
            report = run_once(
                WorkerLimits(
                    sync=0,
                    download=1,
                    parse=0,
                    build=0,
                    publish=0,
                    acquisition_seconds=3600,
                ),
                deps,
            )

        # Failures are not progress: one pass, then the item stays pending
        # for the next round with its retry accounting intact.
        self.assertEqual(report.downloaded, 0)
        self.assertEqual(report.failed, 1)
        self.assertEqual(pending.call_count, 1)

    def test_outage_after_same_pass_progress_ends_pump(self) -> None:
        deps = _deps()
        outage = mock.MagicMock(
            document_id=None,
            error_code="transport_error",
            retryable=True,
            quarantine_reason=None,
        )
        with (
            mock.patch.object(
                worker_module.queries, "reclaim_stale_runs", return_value=0
            ),
            mock.patch.object(
                worker_module.queries,
                "pending_downloads",
                side_effect=lambda *a, **k: [
                    self._pending_row("ok"),
                    self._pending_row("boom"),
                ],
            ) as pending,
            mock.patch.object(worker_module, "DownloadDocument") as download_cls,
        ):
            download_cls.return_value.execute.side_effect = [
                self._ok_download(),
                outage,
            ]
            report = run_once(
                WorkerLimits(
                    sync=0,
                    download=2,
                    parse=0,
                    build=0,
                    publish=0,
                    acquisition_seconds=3600,
                ),
                deps,
            )

        # Progress in the same pass must not outvote the outage breaker.
        self.assertTrue(report.source_outage_break)
        self.assertEqual(report.downloaded, 1)
        self.assertEqual(pending.call_count, 1)

    def test_sync_progress_alone_keeps_pumping(self) -> None:
        deps = _deps()
        due_batches = [
            [
                {
                    "tracked_company_id": "tc_only",
                    "company_id": "co_only",
                    "security_id": "sec_only",
                    "security_code": "000001",
                    "exchange": "SZSE",
                    "window_end": "2026-07-01",
                }
            ],
            [],
        ]
        with (
            mock.patch.object(
                worker_module.queries, "reclaim_stale_runs", return_value=0
            ),
            mock.patch.object(
                worker_module.queries, "sync_due", side_effect=due_batches
            ) as sync_due,
            mock.patch.object(
                worker_module.queries, "pending_downloads", return_value=[]
            ),
            mock.patch.object(worker_module, "SyncDisclosureIndex") as sync_cls,
        ):
            sync_cls.return_value.execute.return_value = mock.MagicMock(
                candidate_count=1
            )
            report = run_once(
                WorkerLimits(
                    sync=1,
                    download=1,
                    parse=0,
                    build=0,
                    publish=0,
                    acquisition_seconds=3600,
                ),
                deps,
            )

        # Sync-only progress keeps the pump alive for another pass.
        self.assertEqual(report.synced_companies, 1)
        self.assertEqual(sync_due.call_count, 2)

    def test_parse_exit_ends_pump_promptly(self) -> None:
        import threading

        deps = _deps()
        counter = iter(range(0, 10**9))
        first_pass_started = threading.Event()

        def _slow_pending(*a: object, **k: object) -> list[dict]:
            # Real passes take minutes; the mocked pump would otherwise spin
            # hundreds of passes inside one GIL slice before the main thread
            # ever gets to set the parse-exited event. Ordering is pinned:
            # the fake parse below returns only after pass 1 has started, so
            # parse_exited is set while an early pass is in flight, and the
            # 25ms pass length gives the main thread scheduling room even on
            # a loaded machine. The fake-monotonic deadline bounds the test
            # if the coupling regresses.
            import time as _time

            first_pass_started.set()
            _time.sleep(0.025)
            return [self._pending_row("again")]

        def _fake_parse(*a: object, **k: object) -> None:
            first_pass_started.wait(timeout=5.0)

        with (
            mock.patch.object(
                worker_module.queries, "reclaim_stale_runs", return_value=0
            ),
            mock.patch.object(
                worker_module.queries, "pending_downloads", side_effect=_slow_pending
            ) as pending,
            mock.patch.object(worker_module, "DownloadDocument") as download_cls,
            mock.patch.object(worker_module, "_parse_stage", side_effect=_fake_parse),
            mock.patch.object(
                worker_module.time,
                "monotonic",
                side_effect=lambda: float(next(counter)),
            ),
        ):
            download_cls.return_value.execute.return_value = self._ok_download()
            report = run_once(
                WorkerLimits(
                    sync=0,
                    download=1,
                    parse=1,
                    build=0,
                    publish=0,
                    acquisition_seconds=60,
                ),
                deps,
            )

        # Parse returning while the pump is alive (halt path) must end the
        # pump within a few passes, not hold the round open for the whole
        # window: report/alert/controller reaction stay prompt.
        self.assertLessEqual(pending.call_count, 6)
        self.assertLessEqual(report.downloaded, 6)

    def test_failed_sync_company_attempted_once_per_round(self) -> None:
        deps = _deps()
        row = {
            "tracked_company_id": "tc_flaky",
            "company_id": "co_flaky",
            "security_id": "sec_flaky",
            "security_code": "000001",
            "exchange": "SZSE",
            "window_end": "2026-07-01",
        }
        download_batches = [
            [self._pending_row("1")],
            [self._pending_row("2")],
            [],
        ]
        with (
            mock.patch.object(
                worker_module.queries, "reclaim_stale_runs", return_value=0
            ),
            mock.patch.object(
                worker_module.queries,
                "sync_due",
                side_effect=lambda *a, **k: [dict(row)],
            ) as sync_due,
            mock.patch.object(
                worker_module.queries,
                "pending_downloads",
                side_effect=download_batches,
            ),
            mock.patch.object(worker_module, "SyncDisclosureIndex") as sync_cls,
            mock.patch.object(worker_module, "DownloadDocument") as download_cls,
        ):
            sync_cls.return_value.execute.side_effect = RuntimeError("boom")
            download_cls.return_value.execute.return_value = self._ok_download()
            report = run_once(
                WorkerLimits(
                    sync=1,
                    download=1,
                    parse=0,
                    build=0,
                    publish=0,
                    acquisition_seconds=3600,
                ),
                deps,
            )

        # The 60s failure cooldown re-lists the company while downloads keep
        # the pump alive; the per-round attempted set caps provider-facing
        # retries at one per round.
        sync_cls.return_value.execute.assert_called_once()
        self.assertGreaterEqual(sync_due.call_count, 2)
        self.assertEqual(report.downloaded, 2)

    def test_deferred_backfill_counted_once_per_round(self) -> None:
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
        row = {
            "tracked_company_id": "tc_defer",
            "company_id": "co_defer",
            "security_id": "sec_defer",
            "security_code": "000002",
            "exchange": "SZSE",
            "window_end": None,
            "last_synced_at": None,
        }
        download_batches = [
            [self._pending_row("1")],
            [self._pending_row("2")],
            [],
        ]
        with (
            mock.patch.object(
                worker_module.queries, "reclaim_stale_runs", return_value=0
            ),
            mock.patch.object(
                worker_module.queries,
                "sync_due",
                side_effect=lambda *a, **k: [dict(row)],
            ),
            mock.patch.object(
                worker_module.queries,
                "pending_processing_backlog_count",
                return_value=5000,
            ),
            mock.patch.object(
                worker_module.queries,
                "pending_downloads",
                side_effect=download_batches,
            ),
            mock.patch.object(worker_module, "SyncDisclosureIndex"),
            mock.patch.object(worker_module, "DownloadDocument") as download_cls,
        ):
            download_cls.return_value.execute.return_value = self._ok_download()
            report = run_once(
                WorkerLimits(
                    sync=1,
                    download=1,
                    parse=0,
                    build=0,
                    publish=0,
                    acquisition_seconds=3600,
                ),
                deps,
            )

        # Saturated-watermark deferral is one fact per round, not one per
        # pump pass.
        self.assertEqual(report.deferred_backfill, 1)
        self.assertEqual(report.downloaded, 2)


class ProjectStageTests(unittest.TestCase):
    def test_projection_delta_drains_unbounded(self) -> None:
        # The projection must never be capped by an unrelated batch constant:
        # the borrowed publish limit (document-scale, 10) once starved this
        # unit-scale rebuild to 48% search coverage while rounds kept
        # reporting success.
        deps = _deps()
        with (
            mock.patch.object(
                worker_module.queries, "reclaim_stale_runs", return_value=0
            ),
            mock.patch.object(
                worker_module.queries, "pending_publish", return_value=[]
            ),
            mock.patch.object(worker_module, "BuildSearchProjection") as project_cls,
        ):
            project_cls.return_value.execute.return_value = mock.MagicMock(
                projected=7, deleted=0, skipped=0
            )
            report = run_once(
                WorkerLimits(sync=0, download=0, parse=0, build=0, publish=10),
                deps,
            )

        (command,) = project_cls.return_value.execute.call_args.args
        self.assertFalse(command.full)
        self.assertIsNone(command.limit)
        self.assertEqual(report.projected, 7)
