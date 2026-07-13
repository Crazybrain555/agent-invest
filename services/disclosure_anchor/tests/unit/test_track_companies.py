"""Batch watchlist intake tests (offline, idempotent)."""

from __future__ import annotations

import unittest

from disclosure_anchor.application.services.subject_resolver import (
    PENDING_LEGAL_NAME_PREFIX,
    SubjectCandidate,
    SubjectResolver,
)
from disclosure_anchor.application.ports.disclosure_source import SourceCompanyProfile
from disclosure_anchor.application.use_cases.track_companies import (
    ResolveTrackedProfiles,
    TrackCompanies,
    TrackCompaniesCommand,
    TrackEntry,
    UntrackCompanies,
)
from disclosure_anchor.domain.errors import DisclosureAnchorError, SourceRequestError

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

    def test_untrack_removes_pool_row_but_keeps_company_and_security(self) -> None:
        uow = FakeUnitOfWork()
        TrackCompanies(uow_factory=lambda: uow).execute(
            TrackCompaniesCommand(
                entries=(
                    TrackEntry(security_code="600519", exchange="SSE"),
                    TrackEntry(security_code="000001", exchange="SZSE"),
                )
            )
        )

        result = UntrackCompanies(uow_factory=lambda: uow).execute(
            (("600519", "SSE"), ("999999", "SSE"))
        )

        self.assertEqual(len(result.removed), 1)
        self.assertEqual(result.removed[0].security_code, "600519")
        self.assertEqual(result.not_tracked, ("999999.SSE",))
        # Pool row gone; ledger rows (company + security) stay.
        self.assertEqual(len(uow.tracked_companies.items), 1)
        self.assertEqual(len(uow.companies.items), 2)
        self.assertEqual(len(uow.securities.items), 2)

    def test_resolve_profiles_upgrades_pending_names_and_fails_open(self) -> None:
        uow = FakeUnitOfWork()
        TrackCompanies(uow_factory=lambda: uow).execute(
            TrackCompaniesCommand(
                entries=(
                    TrackEntry(security_code="300012", exchange="SZSE"),
                    TrackEntry(security_code="301046", exchange="SZSE"),
                )
            )
        )

        def loader(code: str) -> SourceCompanyProfile | None:
            if code == "300012":
                return SourceCompanyProfile(
                    security_code=code,
                    security_name="华测检测",
                    legal_name="华测检测认证集团股份有限公司",
                    provider_org_id=None,
                    uscc=None,
                )
            raise DisclosureAnchorError("quota exhausted")

        results = ResolveTrackedProfiles(
            uow_factory=lambda: uow, profile_loader=loader
        ).execute((("300012", "SZSE"), ("301046", "SZSE")))

        by_code = {r.security_code: r for r in results}
        self.assertTrue(by_code["300012"].resolved)
        self.assertFalse(by_code["301046"].resolved)
        names = {c.legal_name for c in uow.companies.items.values()}
        self.assertIn("华测检测认证集团股份有限公司", names)
        # Failure keeps the placeholder (first sync heals it later).
        self.assertTrue(
            any(n.startswith(PENDING_LEGAL_NAME_PREFIX) for n in names)
        )

        # Second pass: resolved companies are skipped entirely.
        second = ResolveTrackedProfiles(
            uow_factory=lambda: uow, profile_loader=loader
        ).execute((("300012", "SZSE"),))
        self.assertEqual(second, ())

    def test_profile_resolution_stops_batch_on_quota_exhaustion(self) -> None:
        uow = FakeUnitOfWork()
        codes = (("300012", "SZSE"), ("301046", "SZSE"), ("600519", "SSE"))
        TrackCompanies(uow_factory=lambda: uow).execute(
            TrackCompaniesCommand(
                entries=tuple(
                    TrackEntry(security_code=code, exchange=exchange)
                    for code, exchange in codes
                )
            )
        )
        calls: list[str] = []

        def loader(code: str) -> SourceCompanyProfile | None:
            calls.append(code)
            raise SourceRequestError(
                "quota exhausted", error_code="quota_exhausted", retryable=True
            )

        results = ResolveTrackedProfiles(
            uow_factory=lambda: uow, profile_loader=loader
        ).execute(codes)

        self.assertEqual(calls, ["300012"])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].resolved)

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
