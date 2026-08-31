from __future__ import annotations

from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.staged_resource_credit import (
    PerAttemptResourceAllowance,
    ResourceCreditFacts,
    ResourceCreditVector,
    STAGED_RESOURCE_STATE_TRANSITIONS,
    resource_credit_shape,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    advance_remote_parse_checkpoint_v4,
)
from tests.unit.test_remote_parse_lifecycle_v4 import (
    _happy_path,
    _reservation_input,
)


class StagedResourceCreditV4Tests(unittest.TestCase):
    def test_allowance_cannot_enlarge_an_exact_reservation_input(self) -> None:
        reservation_input = _reservation_input()
        with self.assertRaisesRegex(ValueError, "drifted from reservation input"):
            PerAttemptResourceAllowance(
                reservation_input_sha256=reservation_input.sha256,
                reservation_input=reservation_input,
                limits=replace(
                    reservation_input.value.reservation,
                    output_bytes=(
                        reservation_input.value.reservation.output_bytes + 1
                    ),
                ),
            )

    def test_state_table_has_no_staged_or_cleanup_committed_state(self) -> None:
        self.assertNotIn("materialization_staged", STAGED_RESOURCE_STATE_TRANSITIONS)
        self.assertNotIn("cleanup_committed", STAGED_RESOURCE_STATE_TRANSITIONS)
        self.assertEqual(
            STAGED_RESOURCE_STATE_TRANSITIONS["materializing"],
            frozenset({"local_materialized", "cleanup_pending"}),
        )
        self.assertEqual(
            STAGED_RESOURCE_STATE_TRANSITIONS["cleanup_pending"],
            frozenset({"ack_pending", "pre_submission_failed", "superseded"}),
        )

    def test_spool_and_output_remain_held_until_cleanup(self) -> None:
        facts = _local_facts()
        expected = ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=70,
            provider_tasks=1,
            provider_result_bytes=10,
            compressed_bytes=10,
            output_items=1,
            output_bytes=33,
            output_pages=4,
            ack_items=1,
        )
        self.assertEqual(resource_credit_shape("local_materialized", facts), expected)
        published = replace(facts, publication_committed=True)
        self.assertEqual(resource_credit_shape("publish_committed", published), expected)
        self.assertEqual(
            resource_credit_shape(
                "cleanup_pending",
                replace(published, cleanup_outcome="success"),
            ),
            expected,
        )

    def test_partial_cleanup_cannot_release_credit(self) -> None:
        facts = replace(
            _local_facts(),
            publication_committed=True,
            cleanup_outcome="success",
        )
        before = resource_credit_shape("cleanup_pending", facts)
        self.assertGreater(before.output_bytes, 0)
        with self.assertRaisesRegex(ValueError, "uncommitted cleanup plan"):
            resource_credit_shape(
                "cleanup_pending", replace(facts, resources_cleaned=True)
            )

    def test_ack_pending_keeps_only_remote_and_ack_obligations(self) -> None:
        facts = replace(
            _local_facts(),
            publication_committed=True,
            cleanup_outcome="success",
            resources_cleaned=True,
        )
        self.assertEqual(
            resource_credit_shape("ack_pending", facts),
            ResourceCreditVector(
                documents=1,
                provider_tasks=1,
                provider_result_bytes=10,
                ack_items=1,
            ),
        )
        with self.assertRaisesRegex(ValueError, "cannot enter ack_pending"):
            resource_credit_shape(
                "ack_pending",
                ResourceCreditFacts(
                    snapshot_byte_count=70,
                    cleanup_outcome="pre_submission_failure",
                    resources_cleaned=True,
                ),
            )

    def test_provider_owned_states_include_the_ack_obligation(self) -> None:
        submitted = ResourceCreditFacts(
            snapshot_byte_count=70,
            provider_task_retained=True,
        )
        self.assertEqual(
            resource_credit_shape("submitted", submitted).ack_items,
            1,
        )
        terminal = replace(submitted, provider_result_byte_count=10)
        self.assertEqual(
            resource_credit_shape("remote_terminal", terminal).ack_items,
            1,
        )

    def test_projected_materializing_and_local_credit_advance_exactly(self) -> None:
        fixture = _happy_path()
        chain = fixture["chain"]
        intent = fixture["intent"]
        materialization = fixture["materialization"]
        materializing_facts = ResourceCreditFacts(
            snapshot_byte_count=100,
            provider_task_retained=True,
            provider_result_byte_count=20,
            compressed_byte_count=20,
            uncompressed_byte_count=30,
            decoded_byte_count=20,
            temporary_disk_byte_count=50,
            source_page_count=2,
            materialization_prepared=True,
            reservation_input=_reservation_input(),
        )
        projected_materializing = resource_credit_shape(
            "materializing",
            materializing_facts,
        )
        self.assertEqual(projected_materializing, intent.held_resource_credit)
        materializing = advance_remote_parse_checkpoint_v4(
            chain[3],
            state="materializing",
            held_resource_credit=projected_materializing,
            materialization_intent_sha256=intent.sha256,
        )
        local_facts = ResourceCreditFacts(
            snapshot_byte_count=100,
            provider_task_retained=True,
            provider_result_byte_count=20,
            compressed_byte_count=20,
            output_artifact_byte_count=materialization.output_byte_count,
            source_page_count=2,
            materialization_prepared=True,
            local_materialization_completed=True,
        )
        projected_local = resource_credit_shape("local_materialized", local_facts)
        local = advance_remote_parse_checkpoint_v4(
            materializing,
            state="local_materialized",
            held_resource_credit=projected_local,
            local_materialization_receipt_sha256=materialization.sha256,
        )
        self.assertEqual(local.held_resource_credit, projected_local)

    def test_resourceful_supersession_covers_pre_and_post_terminal_shapes(self) -> None:
        pre = ResourceCreditFacts(
            snapshot_byte_count=70,
            cleanup_outcome="superseded",
        )
        self.assertEqual(
            resource_credit_shape("cleanup_pending", pre),
            ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=70,
            ),
        )
        post = replace(
            _local_facts(),
            cleanup_outcome="superseded",
        )
        self.assertGreater(
            resource_credit_shape("cleanup_pending", post).output_bytes, 0
        )
        with self.assertRaisesRegex(ValueError, "committed publication"):
            resource_credit_shape(
                "cleanup_pending",
                replace(post, publication_committed=True),
            )

    def test_local_failure_rejects_committed_or_stray_materialization_facts(self) -> None:
        with self.assertRaisesRegex(ValueError, "committed publication"):
            resource_credit_shape(
                "cleanup_pending",
                replace(
                    _local_facts(),
                    cleanup_outcome="local_failure",
                    publication_committed=True,
                ),
            )
        with self.assertRaisesRegex(ValueError, "materialization facts"):
            resource_credit_shape(
                "cleanup_pending",
                ResourceCreditFacts(
                    snapshot_byte_count=70,
                    provider_task_retained=True,
                    provider_result_byte_count=10,
                    compressed_byte_count=1,
                    cleanup_outcome="local_failure",
                ),
            )

    def test_all_named_finals_release_every_credit(self) -> None:
        final_facts = {
            "acked": replace(
                _local_facts(),
                cleanup_outcome="success",
                resources_cleaned=True,
                publication_committed=True,
            ),
            "remote_failed": replace(
                ResourceCreditFacts(
                    snapshot_byte_count=70,
                    provider_task_retained=True,
                ),
                cleanup_outcome="remote_failure",
                resources_cleaned=True,
            ),
            "local_failed": replace(
                _local_facts(),
                cleanup_outcome="local_failure",
                resources_cleaned=True,
            ),
            "pre_submission_failed": ResourceCreditFacts(
                snapshot_byte_count=70,
                cleanup_outcome="pre_submission_failure",
                resources_cleaned=True,
            ),
            "superseded": replace(
                _local_facts(),
                cleanup_outcome="superseded",
                resources_cleaned=True,
            ),
        }
        for state, facts in final_facts.items():
            with self.subTest(state=state, facts=facts):
                self.assertEqual(
                    resource_credit_shape(state, facts), ResourceCreditVector()
                )
        self.assertEqual(
            resource_credit_shape("preparation_failed", ResourceCreditFacts()),
            ResourceCreditVector(),
        )
        self.assertEqual(
            resource_credit_shape("superseded", ResourceCreditFacts()),
            ResourceCreditVector(),
        )

    def test_named_finals_reject_outcome_incompatible_historical_facts(self) -> None:
        incompatible = replace(
            _local_facts(),
            cleanup_outcome="remote_failure",
            resources_cleaned=True,
        )
        with self.assertRaisesRegex(ValueError, "remote failure"):
            resource_credit_shape("remote_failed", incompatible)
        with self.assertRaisesRegex(ValueError, "provider result bytes"):
            resource_credit_shape(
                "acked",
                ResourceCreditFacts(
                    snapshot_byte_count=70,
                    provider_task_retained=True,
                    cleanup_outcome="success",
                    resources_cleaned=True,
                ),
            )

    def test_resourceful_final_requires_matching_completed_cleanup(self) -> None:
        facts = _local_facts()
        for state, outcome in (
            ("acked", "success"),
            ("remote_failed", "remote_failure"),
            ("local_failed", "local_failure"),
            ("superseded", "superseded"),
        ):
            with self.subTest(state=state, case="missing outcome"):
                with self.assertRaisesRegex(ValueError, "cleanup outcome drifted"):
                    resource_credit_shape(state, facts)
            with self.subTest(state=state, case="wrong outcome"):
                with self.assertRaisesRegex(ValueError, "cleanup outcome drifted"):
                    resource_credit_shape(
                        state,
                        replace(facts, cleanup_outcome="pre_submission_failure"),
                    )
            with self.subTest(state=state, case="not cleaned"):
                with self.assertRaisesRegex(ValueError, "still owns local resources"):
                    resource_credit_shape(
                        state,
                        replace(facts, cleanup_outcome=outcome),
                    )

        pre_submission = ResourceCreditFacts(snapshot_byte_count=70)
        with self.assertRaisesRegex(ValueError, "cleanup outcome drifted"):
            resource_credit_shape("pre_submission_failed", pre_submission)
        with self.assertRaisesRegex(ValueError, "still owns local resources"):
            resource_credit_shape(
                "pre_submission_failed",
                replace(
                    pre_submission,
                    cleanup_outcome="pre_submission_failure",
                ),
            )


def _local_facts() -> ResourceCreditFacts:
    return ResourceCreditFacts(
        snapshot_byte_count=70,
        provider_task_retained=True,
        provider_result_byte_count=10,
        compressed_byte_count=10,
        source_page_count=4,
        materialization_prepared=True,
        local_materialization_completed=True,
        output_artifact_byte_count=33,
    )


if __name__ == "__main__":
    unittest.main()
