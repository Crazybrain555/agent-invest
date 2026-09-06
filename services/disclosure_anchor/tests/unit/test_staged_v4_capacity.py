from __future__ import annotations

from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4

from dataclasses import replace
from dataclasses import fields
import unittest

from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
)
from disclosure_anchor.application.services.staged_v4_capacity import (
    staged_v4_coordinator_limits,
    staged_v4_database_concurrency,
)
from tests.unit.test_mineru_process_profile import _profile


class StagedV4CapacityTests(unittest.TestCase):
    def test_every_credit_and_worker_limit_is_profile_derived(self) -> None:
        profile = _profile()
        limits = staged_v4_coordinator_limits(
            profile,
            worker_profile=StagedWorkerProfileV4(profile.sha256, 3, 5),
        )
        registry_items = (
            profile.registry_nonterminal_cap + profile.registry_terminal_cap
        )

        self.assertEqual(
            limits.credits,
            ResourceCreditVector(
                documents=registry_items,
                snapshot_items=registry_items,
                snapshot_bytes=profile.source_pdf_bytes_limit,
                remote_waits=profile.api_max_pending_tasks,
                provider_tasks=registry_items,
                provider_result_bytes=profile.max_unacked_result_bytes,
                materialization_items=5,
                compressed_bytes=profile.max_unacked_result_bytes,
                decoded_bytes=profile.decoded_payload_bytes_limit,
                temp_disk_bytes=profile.temporary_disk_bytes_limit,
                output_items=profile.registry_terminal_cap,
                output_bytes=profile.terminal_output_bytes_limit,
                output_pages=profile.unpublished_pages_limit,
                ack_items=registry_items,
            ),
        )
        self.assertEqual(
            {
                limits.local_prepare_workers,
                limits.local_workers,
                limits.commit_workers,
                limits.cleanup_workers,
                limits.ack_workers,
            },
            {5},
        )
        self.assertEqual(limits.remote_workers, profile.api_max_pending_tasks)
        self.assertEqual(limits.preflight_workers, 3)
        self.assertEqual(limits.recovery_page_size, registry_items)

    def test_profile_projection_populates_all_credit_dimensions(self) -> None:
        credits = staged_v4_coordinator_limits(
            _profile(),
            worker_profile=StagedWorkerProfileV4(_profile().sha256, 3, 5),
        ).credits
        self.assertEqual(
            {field.name for field in fields(ResourceCreditVector)},
            set(credits.nonzero()),
        )

    def test_database_concurrency_covers_all_lanes_and_nested_commit(self) -> None:
        profile = _profile()
        concurrency = staged_v4_database_concurrency(
            profile,
            worker_profile=StagedWorkerProfileV4(profile.sha256, 3, 5),
        )

        self.assertEqual(
            concurrency.primary_stage_checkouts,
            3 + profile.api_max_pending_tasks + (5 * 5),
        )
        self.assertEqual(
            concurrency.nested_commit_checkouts,
            5,
        )

    def test_terminal_document_does_not_block_next_remote_submission(self) -> None:
        profile = replace(
            _profile(),
            api_task_slots=1,
            api_max_pending_tasks=1,
            registry_nonterminal_cap=4,
            finalizer_slots=1,
        )
        limits = staged_v4_coordinator_limits(
            profile,
            worker_profile=StagedWorkerProfileV4(profile.sha256, 2, 2),
        )
        terminal_a = ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            provider_tasks=1,
            ack_items=1,
        )
        submitted_b = ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            remote_waits=1,
            provider_tasks=1,
            ack_items=1,
        )

        self.assertTrue((terminal_a + submitted_b).fits(limits.credits))
        self.assertEqual(limits.credits.remote_waits, 1)
        self.assertGreaterEqual(limits.credits.provider_tasks, 2)

    def test_requires_exact_profile_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "exact process profile"):
            staged_v4_coordinator_limits(
                object(),  # type: ignore[arg-type]
                worker_profile=StagedWorkerProfileV4(_profile().sha256, 1, 1),
            )

    def test_rejects_invalid_mac_lane_limits(self) -> None:
        for preflight, finalize in ((0, 1), (1, 0), (True, 1), (1, False)):
            with self.subTest(preflight=preflight, finalize=finalize):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    staged_v4_coordinator_limits(
                        _profile(),
                        worker_profile=StagedWorkerProfileV4(
                            _profile().sha256, preflight, finalize,
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
