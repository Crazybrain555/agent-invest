"""Bounded ordinary-worker history catch-up with source receipt progress."""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
import unittest
from unittest import mock

from disclosure_anchor.application.worker import worker
from disclosure_anchor.application.worker.history import oldest_missing_sync_slice
from tests.unit.test_worker_run_once import _deps


class HistorySliceTests(unittest.TestCase):
    def test_oldest_gap_is_bounded_and_overlap_does_not_hide_holes(self) -> None:
        start = date(2021, 10, 7)
        end = date(2026, 10, 8)
        self.assertEqual(
            oldest_missing_sync_slice(window_start=start, window_end=end, covered=[]),
            (start, start + timedelta(days=89)),
        )
        self.assertEqual(
            oldest_missing_sync_slice(
                window_start=start, window_end=end,
                covered=[(date(2022, 1, 1), end), (start, date(2021, 12, 29))],
            ),
            (date(2021, 12, 30), date(2021, 12, 31)),
        )
        self.assertIsNone(oldest_missing_sync_slice(
            window_start=start, window_end=end,
            covered=[(date(2022, 1, 1), end), (start, date(2022, 1, 2))],
        ))

    def test_restart_replays_only_uncovered_days_including_leap_day(self) -> None:
        start, end = date(2024, 2, 28), date(2024, 3, 2)
        covered = [(start, date(2024, 2, 28))]
        first = oldest_missing_sync_slice(
            window_start=start, window_end=end, covered=covered, max_days=2,
        )
        self.assertEqual(first, (date(2024, 2, 29), date(2024, 3, 1)))
        assert first is not None
        # A successful empty source response also records these queried dates.
        covered.append(first)
        second = oldest_missing_sync_slice(
            window_start=start, window_end=end, covered=covered, max_days=2,
        )
        self.assertEqual(second, (date(2024, 3, 2), date(2024, 3, 2)))
        assert second is not None
        covered.append(second)
        self.assertIsNone(oldest_missing_sync_slice(
            window_start=start, window_end=end, covered=covered,
        ))

    def test_invalid_coverage_or_slice_is_visible(self) -> None:
        with self.assertRaises(ValueError):
            oldest_missing_sync_slice(
                window_start=date(2024, 1, 1), window_end=date(2024, 2, 1),
                covered=[(date(2024, 1, 3), date(2024, 1, 2))],
            )
        with self.assertRaises(ValueError):
            oldest_missing_sync_slice(
                window_start=date(2024, 1, 1), window_end=date(2024, 2, 1),
                covered=[], max_days=0,
            )


class WorkerHistorySyncTests(unittest.TestCase):
    def _run(self, *, coverage=(), backlog=0, lookback=None, side_effect=None,
             should_stop=lambda: False, stage_seconds=0, monotonic=None,
             rows=None, heartbeat=None):
        deps = _deps()
        if heartbeat is not None:
            object.__setattr__(deps, 'heartbeat', heartbeat)
        due = rows or [{
            'company_id': 'co_history', 'security_id': 'sec_history',
            'security_code': '000001', 'exchange': 'SZSE',
            'window_end': '2026-07-01', 'lookback': lookback,
        }]
        report = worker.WorkerReport(started_at=deps.clock())
        with (
            mock.patch.object(worker.queries, 'sync_due', return_value=due),
            mock.patch.object(worker.queries, 'successful_sync_ranges', return_value=coverage) as query,
            mock.patch.object(worker.queries, 'pending_processing_backlog_count', return_value=backlog) as waterline,
            mock.patch.object(worker, 'SyncDisclosureIndex') as use_case,
            mock.patch.object(worker.time, 'monotonic', side_effect=monotonic or [0]*20),
        ):
            use_case.return_value.execute.side_effect = side_effect
            use_case.return_value.execute.return_value = SimpleNamespace(candidate_count=1)
            worker._sync_stage(
                report, deps, None, limit=10, stage_seconds=stage_seconds,
                should_stop=should_stop, attempted=set(),
            )
        return report, use_case.return_value.execute, query, waterline

    def test_forward_always_precedes_one_history_slice_and_counts_company_once(self) -> None:
        heartbeat = mock.Mock()
        report, execute, query, _ = self._run(heartbeat=heartbeat)
        forward, history = [call.args[0] for call in execute.call_args_list]
        self.assertEqual((forward.window_start, forward.window_end),
                         (date(2026, 6, 24), date(2026, 7, 6)))
        self.assertEqual(history.window_start, date(2026, 7, 6)-timedelta(days=1827))
        self.assertEqual((history.window_end-history.window_start).days+1, 90)
        self.assertEqual(report.synced_companies, 1)
        self.assertEqual(report.candidates_discovered, 2)
        self.assertEqual(heartbeat.call_count, 2)
        self.assertEqual(query.call_args.kwargs['company_id'], 'co_history')

    def test_covered_or_no_longer_eligible_company_gets_forward_only(self) -> None:
        for coverage in (None, [(date(2021, 1, 1), date(2026, 7, 6))]):
            with self.subTest(coverage=coverage):
                report, execute, _, waterline = self._run(coverage=coverage)
                execute.assert_called_once()
                waterline.assert_not_called()
                self.assertEqual(report.synced_companies, 1)

    def test_successful_empty_history_receipt_is_skipped_after_restart(self) -> None:
        report, execute, _, _ = self._run(side_effect=[
            SimpleNamespace(candidate_count=1), SimpleNamespace(candidate_count=0),
        ])
        accepted = execute.call_args_list[1].args[0]
        self.assertEqual(report.candidates_discovered, 1)
        # The next ordinary scheduled pass loads the durable empty receipt.
        # Its forward sync still runs first, and history starts after coverage.
        restarted, next_execute, _, _ = self._run(coverage=[
            (accepted.window_start, accepted.window_end),
        ])
        next_history = next_execute.call_args_list[1].args[0]
        self.assertEqual(next_history.window_start, accepted.window_end+timedelta(days=1))
        self.assertEqual(restarted.synced_companies, 1)

    def test_explicit_company_override_and_first_sync_skip_extra_history(self) -> None:
        for rows in (
            None,
            [{'company_id': 'co_history', 'security_id': 'sec_history',
              'security_code': '000001', 'exchange': 'SZSE', 'window_end': None}],
        ):
            with self.subTest(rows=rows):
                _, execute, query, _ = self._run(lookback={'days': 30}, rows=rows)
                execute.assert_called_once()
                query.assert_not_called()

    def test_waterline_blocks_history_but_preserves_incremental_progress(self) -> None:
        report, execute, _, waterline = self._run(backlog=2000)
        execute.assert_called_once()
        waterline.assert_called_once()
        self.assertEqual(report.synced_companies, 1)
        self.assertEqual(report.deferred_backfill, 1)

    def test_history_outage_rate_and_quota_use_existing_breakers(self) -> None:
        for code, flag in (
            ('http_503', 'source_outage_break'),
            ('rate_limited', 'sync_rate_limited'),
            ('quota_exhausted', 'sync_quota_break'),
        ):
            with self.subTest(code=code):
                error = RuntimeError('history unavailable')
                error.error_code = code
                error.retryable = True
                rows = [
                    {'company_id': 'co_history', 'security_id': 'sec_history',
                     'security_code': '000001', 'exchange': 'SZSE', 'window_end': '2026-07-01'},
                    {'company_id': 'co_later', 'security_id': 'sec_later',
                     'security_code': '000002', 'exchange': 'SZSE', 'window_end': '2026-07-01'},
                ]
                heartbeat = mock.Mock()
                report, execute, _, _ = self._run(
                    rows=rows, heartbeat=heartbeat,
                    side_effect=[SimpleNamespace(candidate_count=1), error],
                )
                self.assertEqual(execute.call_count, 2)
                self.assertEqual((report.synced_companies, report.failed), (1, 1))
                self.assertTrue(getattr(report, flag))
                self.assertEqual(heartbeat.call_count, 2)

    def test_failed_forward_never_admits_history(self) -> None:
        report, execute, query, _ = self._run(side_effect=RuntimeError('forward failed'))
        execute.assert_called_once()
        query.assert_not_called()
        self.assertEqual((report.synced_companies, report.failed), (0, 1))

    def test_stop_or_timebox_after_forward_preserves_success_and_defers_history(self) -> None:
        report, execute, query, _ = self._run(should_stop=mock.Mock(side_effect=[False, True]))
        execute.assert_called_once()
        query.assert_not_called()
        self.assertEqual(report.synced_companies, 1)
        report, execute, query, _ = self._run(stage_seconds=10, monotonic=[0, 1, 11])
        execute.assert_called_once()
        query.assert_not_called()
        self.assertEqual(report.synced_companies, 1)

    def test_cached_history_growth_defers_next_history_without_starving_forward(self) -> None:
        rows = [
            {'company_id': f'co_{i}', 'security_id': f'sec_{i}',
             'security_code': f'00000{i}', 'exchange': 'SZSE', 'window_end': '2026-07-01'}
            for i in (1, 2)
        ]
        report, execute, _, waterline = self._run(rows=rows, backlog=1998)
        # first forward, first history, second forward; its history defers.
        self.assertEqual(execute.call_count, 3)
        self.assertEqual((report.synced_companies, report.deferred_backfill), (2, 1))
        waterline.assert_called_once()
        self.assertEqual(execute.call_args_list[2].args[0].window_end, date(2026, 7, 6))
