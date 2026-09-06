from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import unittest

from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import (
    RemoteParseV4Repository,
    recovery_candidate_from_head_row,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RecoveryCandidate,
    RemoteParseV4AuthorityViolation,
    V4GenerationConflict,
    V4PreparedProposal,
    bind_v4_prepared_proposal,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
    ResourceReservationInput,
    StagedResourceCreditEnvelope,
    encode_resource_reservation_input,
)
from tests.integration._remote_parse_v4_factory import _execution_spec
from disclosure_anchor.application.services.staged_parse_coordinator import (
    RecoveryCandidate as CoordinatorRecoveryCandidate,
)


class RemoteParseV4RecoveryProjectionTests(unittest.TestCase):
    observed_at = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)

    @classmethod
    def _claimed_row(cls, **overrides: object) -> dict[str, object]:
        row: dict[str, object] = {
            "attempt_id": "rpa_recovery-projection",
            "checkpoint_contract_version": 4,
            "state": "submitted",
            "is_current": True,
            "row_version": 2,
            "claim_generation": 3,
            "claim_owner_identity": "worker-recovery",
            "claim_lease_until": cls.observed_at + timedelta(seconds=10),
        }
        row.update(overrides)
        return row

    def test_unclaimed_prepared_head_projects_and_is_the_coordinator_type(self) -> None:
        candidate = recovery_candidate_from_head_row(
            self._claimed_row(
                state="prepared",
                row_version=0,
                claim_generation=0,
                claim_owner_identity=None,
                claim_lease_until=None,
            ),
            database_observed_at=self.observed_at,
        )

        self.assertIs(CoordinatorRecoveryCandidate, RecoveryCandidate)
        self.assertEqual(
            candidate,
            RecoveryCandidate(
                attempt_id="rpa_recovery-projection",
                state="prepared",
                lifecycle_version=0,
                claim_generation=0,
                claim_owner_identity=None,
                lease_remaining_seconds=None,
            ),
        )

    def test_creation_guard_rejects_a_historical_generation_gap(self) -> None:
        with self.assertRaisesRegex(
            V4GenerationConflict,
            "not contiguous",
        ):
            RemoteParseV4Repository._guard_creation_chain(
                rows=(
                    {
                        "attempt_generation": 2,
                        "is_current": False,
                        "checkpoint_contract_version": 4,
                        "state": "acked",
                    },
                ),
                generations=(3,),
                allow_existing_current=False,
            )

    def test_owned_lease_projection_preserves_negative_zero_and_positive_signs(
        self,
    ) -> None:
        for seconds in (-1.25, 0.0, 10.5):
            with self.subTest(seconds=seconds):
                candidate = recovery_candidate_from_head_row(
                    self._claimed_row(
                        claim_lease_until=self.observed_at
                        + timedelta(seconds=seconds)
                    ),
                    database_observed_at=self.observed_at,
                )
                self.assertEqual(candidate.lease_remaining_seconds, seconds)

    def test_naive_database_datetimes_are_interpreted_as_utc(self) -> None:
        observed = self.observed_at.replace(tzinfo=None)
        candidate = recovery_candidate_from_head_row(
            self._claimed_row(
                claim_lease_until=observed + timedelta(microseconds=1)
            ),
            database_observed_at=observed,
        )

        self.assertEqual(candidate.lease_remaining_seconds, 0.000001)

    def test_incomplete_projection_fails_closed(self) -> None:
        row = self._claimed_row()
        del row["attempt_id"]

        with self.assertRaisesRegex(
            RemoteParseV4AuthorityViolation,
            "projection is incomplete",
        ):
            recovery_candidate_from_head_row(
                row,
                database_observed_at=self.observed_at,
            )

    def test_authority_and_claim_shape_drift_fail_closed(self) -> None:
        invalid_rows = (
            {"checkpoint_contract_version": 3},
            {"is_current": False},
            {"state": "acked"},
            {"state": "unknown"},
            {"row_version": True},
            {"row_version": -1},
            {"claim_generation": True},
            {"claim_generation": -1},
            {"claim_generation": 0},
            {"claim_owner_identity": ""},
            {"claim_lease_until": None},
            {
                "state": "prepared",
                "row_version": 0,
                "claim_generation": 0,
                "claim_owner_identity": None,
                "claim_lease_until": self.observed_at,
            },
            {
                "state": "prepared",
                "row_version": 1,
                "claim_generation": 0,
                "claim_owner_identity": None,
                "claim_lease_until": None,
            },
            {
                "state": "submitted",
                "claim_generation": 0,
                "claim_owner_identity": None,
                "claim_lease_until": None,
            },
            {"attempt_id": ""},
        )
        for overrides in invalid_rows:
            with self.subTest(overrides=overrides):
                with self.assertRaises(RemoteParseV4AuthorityViolation):
                    recovery_candidate_from_head_row(
                        self._claimed_row(**overrides),
                        database_observed_at=self.observed_at,
                    )

        with self.assertRaises(RemoteParseV4AuthorityViolation):
            recovery_candidate_from_head_row(
                self._claimed_row(),
                database_observed_at="not-a-clock",  # type: ignore[arg-type]
            )

    def test_prepared_proposal_binds_generation_into_exact_h0(self) -> None:
        proposal = _prepared_proposal()

        creation = bind_v4_prepared_proposal(
            proposal,
            attempt_generation=4,
        )

        self.assertEqual(creation.checkpoint.attempt_generation, 4)
        self.assertEqual(creation.reservation.attempt_generation, 4)
        self.assertEqual(creation.checkpoint.state, "prepared")
        self.assertEqual(creation.checkpoint.lifecycle_version, 0)
        self.assertIsNone(creation.snapshot_receipt)
        self.assertIsNone(creation.checkpoint.snapshot_receipt_sha256)
        self.assertEqual(
            creation.checkpoint.held_resource_credit,
            ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=100,
            ),
        )

    def test_prepared_proposal_generation_binding_is_pure_and_distinct(self) -> None:
        proposal = _prepared_proposal()

        first = bind_v4_prepared_proposal(proposal, attempt_generation=1)
        second = bind_v4_prepared_proposal(proposal, attempt_generation=2)

        self.assertEqual(proposal, _prepared_proposal())
        self.assertNotEqual(first.reservation.sha256, second.reservation.sha256)
        self.assertNotEqual(first.checkpoint.sha256, second.checkpoint.sha256)
        self.assertEqual(first.preparation_intent.parser_target_sha256, proposal.parser_target_sha256)


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _prepared_proposal() -> V4PreparedProposal:
    spec = _execution_spec("rpa_prepared-proposal", "fence-prepared-proposal", _sha("source"))
    reserved = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=100,
        remote_waits=1,
        provider_tasks=1,
        provider_result_bytes=200,
        materialization_items=1,
        compressed_bytes=200,
        decoded_bytes=300,
        temp_disk_bytes=400,
        output_items=1,
        output_bytes=300,
        output_pages=2,
        ack_items=1,
    )
    encoded = encode_resource_reservation_input(
        ResourceReservationInput(
            source_pdf_sha256=_sha("source"),
            source_byte_count=100,
            source_page_count=2,
            process_profile_sha256=spec.process_profile_sha256,
            credit_policy_sha256=_sha("policy"),
            bucket="regular",
            reservation=reserved,
        )
    )
    return V4PreparedProposal(
        document_id="doc_prepared-proposal",
        processing_run_id="run_prepared-proposal",
        prepared_submission=spec.prepared_submission,
        credit_envelope=StagedResourceCreditEnvelope(
            process_profile_sha256=spec.process_profile_sha256,
            credit_policy_sha256=_sha("policy"),
            reservation_input=encoded,
            reservation=reserved,
        ),
        execution_spec=spec,
    )


if __name__ == "__main__":
    unittest.main()
