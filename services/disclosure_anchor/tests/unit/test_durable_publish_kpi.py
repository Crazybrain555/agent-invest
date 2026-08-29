from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from disclosure_anchor.application.contracts.durable_publish_kpi import (
    replay_durable_publish_kpi,
)


NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)
FINISHED = NOW + timedelta(hours=1)


def _row(
    kind: str,
    run_id: str,
    identity: str | None,
    pages: int | None,
    *,
    occurred_at: datetime = NOW,
    publish_committed_at: datetime = NOW,
) -> dict[str, object]:
    payload = {} if identity is None else {
        "source_identity": identity,
        "source_page_count": pages,
        "publish_committed_at": publish_committed_at.isoformat(),
    }
    return {
        "event_kind": kind,
        "processing_run_id": run_id,
        "payload": payload,
        "occurred_at": occurred_at,
    }


class DurablePublishKpiTests(unittest.TestCase):
    def test_replay_deduplicates_across_intervals_and_restart_rows(self) -> None:
        identity = "sha256:" + "a" * 64
        snapshot = replay_durable_publish_kpi(
            (
                _row("processing_run_published", "run_1", identity, 10),
                _row("processing_run_published", "run_2", identity, 10),
            ),
            started_at=NOW,
            finished_at=FINISHED,
        )
        self.assertEqual(snapshot.unique_source_pages, 10)
        self.assertEqual(snapshot.unique_source_count, 1)
        self.assertEqual(snapshot.conflict_count, 0)

    def test_replay_closes_incomplete_with_supplement_and_exposes_conflict(self) -> None:
        identity = "sha256:" + "b" * 64
        snapshot = replay_durable_publish_kpi(
            (
                _row("processing_run_published", "run_1", None, None),
                _row(
                    "processing_run_publish_evidence_backfilled",
                    "run_1",
                    identity,
                    20,
                ),
                _row("processing_run_published", "run_2", identity, 21),
                _row("processing_run_published", "run_3", None, None),
            ),
            started_at=NOW,
            finished_at=FINISHED,
        )
        self.assertEqual(snapshot.unique_source_pages, 20)
        self.assertEqual(snapshot.incomplete_publish_count, 1)
        self.assertEqual(snapshot.conflict_count, 1)

    def test_replay_exposes_base_supplement_and_duplicate_base_conflicts(self) -> None:
        identity = "sha256:" + "c" * 64
        other = "sha256:" + "d" * 64
        snapshot = replay_durable_publish_kpi(
            (
                _row("processing_run_published", "run_1", identity, 10),
                _row("processing_run_published", "run_1", identity, 11),
                _row(
                    "processing_run_publish_evidence_backfilled",
                    "run_1",
                    other,
                    11,
                ),
            ),
            started_at=NOW,
            finished_at=FINISHED,
        )
        self.assertEqual(snapshot.conflict_count, 2)

    def test_replay_rejects_noncanonical_source_identity(self) -> None:
        snapshot = replay_durable_publish_kpi(
            (_row("processing_run_published", "run_1", "sha256:short", 10),),
            started_at=NOW,
            finished_at=FINISHED,
        )
        self.assertEqual(snapshot.unique_source_pages, 0)
        self.assertEqual(snapshot.incomplete_publish_count, 1)

    def test_late_backfill_closes_original_host_hour(self) -> None:
        identity = "sha256:" + "e" * 64
        snapshot = replay_durable_publish_kpi(
            (
                _row("processing_run_published", "run_1", None, None),
                _row(
                    "processing_run_publish_evidence_backfilled",
                    "run_1",
                    identity,
                    31,
                    occurred_at=NOW + timedelta(hours=3),
                    publish_committed_at=NOW,
                ),
            ),
            started_at=NOW,
            finished_at=FINISHED,
        )
        self.assertEqual(snapshot.unique_source_pages, 31)
        self.assertEqual(snapshot.incomplete_publish_count, 0)


if __name__ == "__main__":
    unittest.main()
