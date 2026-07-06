"""Batch watchlist intake tests (offline, idempotent)."""

from __future__ import annotations

import unittest

from disclosure_anchor.application.services.subject_resolver import (
    PENDING_LEGAL_NAME_PREFIX,
    SubjectCandidate,
    SubjectResolver,
)
from disclosure_anchor.application.use_cases.track_companies import (
    TrackCompanies,
    TrackCompaniesCommand,
    TrackEntry,
)

from tests.unit._fakes import FakeUnitOfWork


class TrackCompaniesTests(unittest.TestCase):
    def test_offline_intake_creates_placeholder_subject_and_tracked_row(self) -> None:
        uow = FakeUnitOfWork()

        result = TrackCompanies(uow_factory=lambda: uow).execute(
            TrackCompaniesCommand(
                entries=(
                    TrackEntry(security_code="600519", exchange="SSE"),
                    TrackEntry(
                        security_code="000001",
                        exchange="SZSE",
                        lookback_days=30,
                        sync_frequency="hourly",
                    ),
                )
            )
        )

        self.assertEqual(result.created_count, 2)
        companies = list(uow.companies.items.values())
        self.assertTrue(
            all(c.legal_name.startswith(PENDING_LEGAL_NAME_PREFIX) for c in companies)
        )
        tracked = list(uow.tracked_companies.items.values())
        self.assertEqual(len(tracked), 2)
        override = next(t for t in tracked if t.lookback is not None)
        self.assertEqual(override.lookback, {"days": 30})
        self.assertEqual(override.sync_frequency, "hourly")

    def test_intake_is_idempotent_and_updates_overrides(self) -> None:
        uow = FakeUnitOfWork()
        use_case = TrackCompanies(uow_factory=lambda: uow)
        use_case.execute(
            TrackCompaniesCommand(
                entries=(TrackEntry(security_code="600519", exchange="SSE"),)
            )
        )

        result = use_case.execute(
            TrackCompaniesCommand(
                entries=(
                    TrackEntry(
                        security_code="600519", exchange="SSE", lookback_days=90
                    ),
                )
            )
        )

        self.assertEqual(result.created_count, 0)
        tracked = list(uow.tracked_companies.items.values())
        self.assertEqual(len(tracked), 1)
        self.assertEqual(tracked[0].lookback, {"days": 90})
        self.assertEqual(len(uow.companies.items), 1)

    def test_unknown_sync_frequency_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TrackCompanies(uow_factory=lambda: FakeUnitOfWork()).execute(
                TrackCompaniesCommand(
                    entries=(
                        TrackEntry(
                            security_code="600519",
                            exchange="SSE",
                            sync_frequency="fortnightly",
                        ),
                    )
                )
            )


class PlaceholderUpgradeTests(unittest.TestCase):
    def test_first_credentialed_sync_upgrades_placeholder_name(self) -> None:
        uow = FakeUnitOfWork()
        resolver = SubjectResolver()
        resolver.resolve(
            uow,
            SubjectCandidate(
                security_code="600519", exchange="SSE", legal_name=None
            ),
        )

        resolved = resolver.resolve(
            uow,
            SubjectCandidate(
                security_code="600519",
                exchange="SSE",
                legal_name="贵州茅台酒股份有限公司",
                credit_code="9152000071430580XT",
            ),
        )

        self.assertEqual(resolved.company.legal_name, "贵州茅台酒股份有限公司")
        self.assertEqual(len(uow.companies.items), 1)

    def test_real_name_conflict_still_contested(self) -> None:
        from disclosure_anchor.domain.errors import SubjectIdentityConflictError

        uow = FakeUnitOfWork()
        resolver = SubjectResolver()
        resolver.resolve(
            uow,
            SubjectCandidate(
                security_code="600519",
                exchange="SSE",
                legal_name="贵州茅台酒股份有限公司",
                credit_code="9152000071430580XT",
            ),
        )

        with self.assertRaises(SubjectIdentityConflictError):
            resolver.resolve(
                uow,
                SubjectCandidate(
                    security_code="600519",
                    exchange="SSE",
                    legal_name="另一家公司股份有限公司",
                    credit_code="9152000071430580XT",
                ),
            )


if __name__ == "__main__":
    unittest.main()
