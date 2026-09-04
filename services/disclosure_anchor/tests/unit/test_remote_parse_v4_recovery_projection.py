from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import (
    recovery_candidate_from_head_row,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RecoveryCandidate,
    RemoteParseV4AuthorityViolation,
)
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


if __name__ == "__main__":
    unittest.main()
