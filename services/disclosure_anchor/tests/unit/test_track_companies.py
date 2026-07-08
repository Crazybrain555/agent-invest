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

    def test_blank_csv_cells_clear_stale_overrides(self) -> None:
        # CSV is the single source of truth: a blank cell means "use the
        # default" and must clear a previously-set override (acceptance P1).
        uow = FakeUnitOfWork()
        use_case = TrackCompanies(uow_factory=lambda: uow)
        use_case.execute(
            TrackCompaniesCommand(
                entries=(
                    TrackEntry(
                        security_code="600519",
                        exchange="SSE",
                        lookback_days=30,
                        sync_frequency="hourly",
                        process_classes=("dividend",),
                    ),
                )
            )
        )

        use_case.execute(
            TrackCompaniesCommand(
                entries=(TrackEntry(security_code="600519", exchange="SSE"),)
            )
        )

        tracked = list(uow.tracked_companies.items.values())
        self.assertEqual(len(tracked), 1)
        self.assertIsNone(tracked[0].lookback)
        self.assertIsNone(tracked[0].sync_frequency)
        self.assertIsNone(tracked[0].process_classes)

    def test_unknown_process_classes_raise(self) -> None:
        uow = FakeUnitOfWork()
        use_case = TrackCompanies(uow_factory=lambda: uow)
        with self.assertRaises(ValueError):
            use_case.execute(
                TrackCompaniesCommand(
                    entries=(
                        TrackEntry(
                            security_code="600519",
                            exchange="SSE",
                            process_classes=("divident",),
                        ),
                    )
                )
            )

    def test_dry_run_computes_plan_without_writes(self) -> None:
        uow = FakeUnitOfWork()
        use_case = TrackCompanies(uow_factory=lambda: uow)
        result = use_case.execute(
            TrackCompaniesCommand(
                entries=(TrackEntry(security_code="600519", exchange="SSE"),),
                dry_run=True,
            )
        )
        self.assertTrue(result.dry_run)
        self.assertEqual(result.created_count, 1)
        self.assertEqual(uow.commit_count, 0)

    def test_reconcile_reports_and_prunes_drift(self) -> None:
        uow = FakeUnitOfWork()
        use_case = TrackCompanies(uow_factory=lambda: uow)
        use_case.execute(
            TrackCompaniesCommand(
                entries=(
                    TrackEntry(security_code="600519", exchange="SSE"),
                    TrackEntry(security_code="000001", exchange="SZSE"),
                )
            )
        )

        # Reconcile against a watchlist that no longer contains 000001.
        result = use_case.execute(
            TrackCompaniesCommand(
                entries=(TrackEntry(security_code="600519", exchange="SSE"),),
                reconcile=True,
            )
        )
        self.assertEqual(len(result.drift), 1)
        self.assertEqual(result.drift[0].security_code, "000001")
        self.assertEqual(result.drift[0].action, "reported")

        pruned = use_case.execute(
            TrackCompaniesCommand(
                entries=(TrackEntry(security_code="600519", exchange="SSE"),),
                reconcile=True,
                prune_drift=True,
            )
        )
        self.assertEqual(pruned.drift[0].action, "paused")
        drifted = next(
            t for t in uow.tracked_companies.items.values()
            if t.tracked_company_id == pruned.drift[0].tracked_company_id
        )
        self.assertEqual(drifted.status, "paused")

    def test_paused_status_and_categories_from_entry(self) -> None:
        uow = FakeUnitOfWork()
        TrackCompanies(uow_factory=lambda: uow).execute(
            TrackCompaniesCommand(
                entries=(
                    TrackEntry(
                        security_code="600519",
                        exchange="SSE",
                        status="paused",
                        process_classes=("dividend", "meeting_resolution"),
                    ),
                )
            )
        )
        tracked = next(iter(uow.tracked_companies.items.values()))
        self.assertEqual(tracked.status, "paused")
        self.assertEqual(tracked.process_classes, ["dividend", "meeting_resolution"])

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
