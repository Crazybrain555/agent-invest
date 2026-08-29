from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from disclosure_anchor.application.contracts.full_host_hour_kpi import (
    DurableProfilePageEvidence,
    VerifiedTelemetryCoverage,
)
from disclosure_anchor.application.services.full_host_hour_kpi import (
    aggregate_full_gpu_host_hour,
    project_publish_payload_for_host_hour,
)


START = datetime(2026, 8, 30, 8, tzinfo=timezone.utc)
HASHES = ["sha256:" + character * 64 for character in "abcdef"]


def _coverage(start: datetime, finish: datetime, *, profile: str = HASHES[2], complete: bool = True):
    return VerifiedTelemetryCoverage(
        started_at_utc=start,
        finished_at_utc=finish,
        host_assignment_identity_sha256=HASHES[0],
        boot_identity_sha256=HASHES[5],
        gpu_exporter_process_epoch_sha256=HASHES[3],
        host_exporter_process_epoch_sha256=HASHES[4],
        runtime_bundle_identity_sha256=HASHES[1],
        process_profile_sha256=profile,
        observer_process_epoch_sha256=HASHES[3],
        receipt_sha256=HASHES[4],
        seal_sha256=HASHES[5],
        status="complete" if complete else "incomplete",
        gpu_lane_complete=complete,
        host_lane_complete=complete,
        observer_overhead_safe=complete,
        exporter_overhead_safe=complete,
        exporter_overhead_attestation_sha256=HASHES[4] if complete else None,
    )


def _publish(*, source: str, profile: str = HASHES[2], pages: int | None = 5, committed: datetime = START, status: str = "complete"):
    return DurableProfilePageEvidence(
        source_identity_sha256=source,
        host_assignment_identity_sha256=HASHES[0] if status == "complete" else None,
        boot_identity_sha256=HASHES[5] if status == "complete" else None,
        runtime_bundle_identity_sha256=HASHES[1] if status == "complete" else None,
        process_profile_sha256=profile if status == "complete" else None,
        source_page_count=pages if status == "complete" else None,
        publish_committed_at_utc=committed,
        evidence_observed_at_utc=committed + timedelta(hours=3),
        status=status,
        first_durable_publish=True if status == "complete" else None,
    )


class FullHostHourKpiTests(unittest.TestCase):
    def test_exact_hour_counts_source_profile_and_late_backfill(self) -> None:
        result = aggregate_full_gpu_host_hour(
            hour_started_at_utc=START,
            coverage=(
                _coverage(START, START + timedelta(minutes=30)),
                _coverage(
                    START + timedelta(minutes=30),
                    START + timedelta(hours=1),
                    profile=HASHES[3],
                ),
            ),
            publish_evidence=(
                _publish(source="sha256:" + "7" * 64, pages=5),
                _publish(
                    source="sha256:" + "8" * 64,
                    profile=HASHES[3],
                    pages=5,
                    committed=START + timedelta(minutes=30),
                ),
            ),
            publish_history_scan_complete=True,
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.host_unique_durable_pages, 10)
        self.assertIsNone(result.profile_eligible_unique_durable_pages)

    def test_partial_gap_unsafe_and_missing_profile_never_extrapolate(self) -> None:
        cases = (
            ((_coverage(START, START + timedelta(minutes=59)),), (), "coverage"),
            ((_coverage(START, START + timedelta(hours=1), complete=False),), (), "telemetry"),
            ((_coverage(START, START + timedelta(hours=1)),), (_publish(source="sha256:" + "7" * 64, status="incomplete"),), "publish"),
        )
        for coverage, evidence, reason in cases:
            result = aggregate_full_gpu_host_hour(
                hour_started_at_utc=START,
                coverage=coverage,
                publish_evidence=evidence,
                publish_history_scan_complete=True,
            )
            self.assertFalse(result.complete)
            self.assertIsNone(result.host_unique_durable_pages)
            self.assertTrue(any(reason in item for item in result.incomplete_reasons))

    def test_profile_change_keeps_host_goodput_but_disables_profile_comparison(self) -> None:
        result = aggregate_full_gpu_host_hour(
            hour_started_at_utc=START,
            coverage=(
                _coverage(START, START + timedelta(minutes=30)),
                _coverage(START + timedelta(minutes=30), START + timedelta(hours=1), profile=HASHES[3]),
            ),
            publish_evidence=(),
            publish_history_scan_complete=True,
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.host_unique_durable_pages, 0)
        self.assertIsNone(result.profile_eligible_unique_durable_pages)

    def test_same_source_across_profiles_counts_once_for_host(self) -> None:
        source = "sha256:" + "7" * 64
        result = aggregate_full_gpu_host_hour(
            hour_started_at_utc=START,
            coverage=(
                _coverage(START, START + timedelta(minutes=30)),
                _coverage(START + timedelta(minutes=30), START + timedelta(hours=1), profile=HASHES[3]),
            ),
            publish_evidence=(
                _publish(source=source, pages=5),
                _publish(source=source, profile=HASHES[3], pages=5, committed=START + timedelta(minutes=30)),
            ),
            publish_history_scan_complete=True,
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.host_unique_durable_pages, 5)
        self.assertIsNone(result.profile_eligible_unique_durable_pages)

    def test_late_supplement_is_attributed_to_original_commit_hour(self) -> None:
        evidence = project_publish_payload_for_host_hour(
            {
                "source_identity": "sha256:" + "7" * 64,
                "source_page_count": 5,
                "publish_committed_at": START.isoformat(),
                "is_first_durable_publish": True,
                "runtime_bundle_identity_sha256": HASHES[1],
                "process_profile_sha256": HASHES[2],
                "host_assignment_identity_sha256": HASHES[0],
                "boot_identity_sha256": HASHES[5],
            },
            event_kind="processing_run_publish_evidence_backfilled",
            occurred_at_utc=START + timedelta(hours=3),
            evidence_observed_at_utc=START + timedelta(hours=3),
        )
        result = aggregate_full_gpu_host_hour(
            hour_started_at_utc=START,
            coverage=(_coverage(START, START + timedelta(hours=1)),),
            publish_evidence=(evidence,),
            publish_history_scan_complete=True,
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.host_unique_durable_pages, 5)

    def test_incomplete_publish_scan_cannot_claim_zero_goodput(self) -> None:
        result = aggregate_full_gpu_host_hour(
            hour_started_at_utc=START,
            coverage=(_coverage(START, START + timedelta(hours=1)),),
            publish_evidence=(),
            publish_history_scan_complete=False,
        )
        self.assertFalse(result.complete)
        self.assertIn("publish_history_scan_incomplete", result.incomplete_reasons)

    def test_legacy_outbox_without_runtime_profile_is_explicitly_incomplete(self) -> None:
        evidence = project_publish_payload_for_host_hour(
            {
                "source_identity": "sha256:" + "7" * 64,
                "source_page_count": 5,
                "publish_committed_at": START.isoformat(),
                "is_first_durable_publish": True,
            },
            event_kind="processing_run_published",
            occurred_at_utc=START,
            evidence_observed_at_utc=START + timedelta(hours=2),
        )
        self.assertEqual(evidence.status, "incomplete")
        result = aggregate_full_gpu_host_hour(
            hour_started_at_utc=START,
            coverage=(_coverage(START, START + timedelta(hours=1)),),
            publish_evidence=(evidence,),
            publish_history_scan_complete=True,
        )
        self.assertFalse(result.complete)


if __name__ == "__main__":
    unittest.main()
