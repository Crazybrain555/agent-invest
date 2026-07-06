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
