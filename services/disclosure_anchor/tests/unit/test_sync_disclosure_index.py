"""Unit tests for CNINFO index sync use case."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
import unittest

from disclosure_anchor.adapters.sources.cninfo.mapper import (
    map_filing_type,
    map_p_info3015_record,
    map_p_stock2100_record,
)
from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureWindow,
    SourceCompanyProfile,
    SourceSecurity,
)
from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    INDEX_INTERFACE,
    CompanyNotTrackedError,
    SyncDisclosureIndex,
    SyncDisclosureIndexCommand,
    compute_sync_window,
)
from disclosure_anchor.domain import entities as e
from tests.unit._fakes import FakeUnitOfWork, SourceAccessRepo


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "cninfo"


def _seed_tracked(uow: FakeUnitOfWork, *, status: str = "active") -> e.TrackedCompany:
    """Sync requires prior pool membership (round23); seed it like `make track`."""

    company = uow.companies.add(
        e.Company(company_id="comp_test000001", legal_name="平安银行股份有限公司")
    )
    security = uow.securities.add(
        e.Security(
            security_id="sec_test000001",
            company_id=company.company_id,
            security_code="000001",
            exchange="SZSE",
        )
    )
    return uow.tracked_companies.add(
        e.TrackedCompany(
            tracked_company_id="trk_test000001",
            company_id=company.company_id,
            security_id=security.security_id,
            status=status,
        )
    )


class SyncDisclosureIndexTests(unittest.TestCase):
    def test_persists_candidates_before_advancing_checkpoint(self) -> None:
        uow = FakeUnitOfWork()
        _seed_tracked(uow)
        use_case = _use_case(uow, _refs())

        result = use_case.execute(_command())

        index_access = uow.source_accesses.get(result.index_source_access_id)
        self.assertEqual(index_access.provider_interface, INDEX_INTERFACE)
        self.assertEqual(index_access.status, "ok")
        candidates = index_access.result_snapshot["candidates"]
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            candidates[0]["provider_document_id"],
            "cninfo-test-000001-20260701-annual",
        )
        self.assertEqual(candidates[0]["filing_type"], "annual_report")
        self.assertEqual(
            candidates[0]["file_signature_hint"]["file_size"],
            512,
        )
        checkpoint = uow.source_checkpoints.get(result.checkpoint_id)
        # Cursor gained audit fields (design/watchlist-operations.md §5.4);
        # readers only use window_end.
        self.assertEqual(checkpoint.cursor["window_end"], "2026-07-02")
        self.assertEqual(checkpoint.cursor["window_start"], "2026-07-01")
        self.assertIn("synced_at", checkpoint.cursor)
        self.assertTrue(checkpoint.scope_key.endswith(":p_info3015"))
        self.assertEqual(uow.commit_count, 1)

    def test_empty_index_writes_empty_snapshot_and_hash(self) -> None:
        uow = FakeUnitOfWork()
        _seed_tracked(uow)
        use_case = _use_case(uow, [])

        result = use_case.execute(_command())

        index_access = uow.source_accesses.get(result.index_source_access_id)
        self.assertTrue(result.empty)
        self.assertEqual(index_access.result_snapshot, {"result": "empty", "candidates": []})
        self.assertTrue(index_access.result_hash.startswith("sha256:"))

    def test_cninfo_org_id_and_tracked_company_are_recorded(self) -> None:
        uow = FakeUnitOfWork()
        _seed_tracked(uow)
        use_case = _use_case(uow, _refs())

        result = use_case.execute(_command())

        tracked = uow.tracked_companies.get_by_company_id(result.company_id)
        org_identifier = uow.company_identifiers.get_by_scheme_value(
            "cninfo_org_id", "cninfo-org-test-000001"
        )
        uscc_identifier = uow.company_identifiers.get_by_scheme_value(
            "uscc", _profile().uscc
        )
        self.assertEqual(tracked.security_id, result.security_id)
        self.assertIsNone(tracked.process_classes)
        self.assertEqual(org_identifier.company_id, result.company_id)
        self.assertEqual(
            org_identifier.source_access_id, result.profile_source_access_id
        )
        self.assertEqual(uscc_identifier.company_id, result.company_id)
        self.assertEqual(
            uscc_identifier.source_access_id, result.profile_source_access_id
        )

    def test_untracked_company_is_rejected_before_any_provider_call(self) -> None:
        uow = FakeUnitOfWork()
        profile_calls: list[str] = []
        source = FakeCninfoSource(_refs())

        def _recording_loader(code: str) -> object:
            profile_calls.append(code)
            return _profile()

        use_case = SyncDisclosureIndex(
            source=source,
            profile_loader=_recording_loader,
            uow_factory=lambda: uow,
        )

        with self.assertRaises(CompanyNotTrackedError):
            use_case.execute(_command())

        # No quota burned, no ledger rows created, nothing persisted.
        self.assertEqual(profile_calls, [])
        self.assertEqual(source.calls, [])
        self.assertEqual(uow.companies.all(), [])
        self.assertEqual(uow.securities.all(), [])
        self.assertEqual(uow.source_accesses.all(), [])
        self.assertEqual(uow.commit_count, 0)

    def test_sync_preserves_paused_status_and_never_resurrects(self) -> None:
        uow = FakeUnitOfWork()
        _seed_tracked(uow, status="paused")
        use_case = _use_case(uow, _refs())

        result = use_case.execute(_command())

        tracked = uow.tracked_companies.get_by_company_id(result.company_id)
        self.assertEqual(tracked.status, "paused")
        self.assertEqual(tracked.security_id, result.security_id)

    def test_checkpoint_does_not_advance_when_candidate_persistence_fails(self) -> None:
        uow = FakeUnitOfWork()
        _seed_tracked(uow)
        uow.source_accesses = FailingIndexSourceAccessRepo()
        use_case = _use_case(uow, _refs())

        with self.assertRaises(RuntimeError):
            use_case.execute(_command())

        self.assertEqual(uow.source_checkpoints.all(), [])

    def test_existing_checkpoint_refreshes_due_timestamp(self) -> None:
        uow = FakeUnitOfWork()
        _seed_tracked(uow)
        use_case = _use_case(uow, _refs())
        first = use_case.execute(_command())
        checkpoint = uow.source_checkpoints.get(first.checkpoint_id)
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        checkpoint.updated_at = old
        uow.source_checkpoints.update(checkpoint)

        second = use_case.execute(_command())

        refreshed = uow.source_checkpoints.get(second.checkpoint_id)
        self.assertEqual(second.checkpoint_id, first.checkpoint_id)
        self.assertIsNotNone(refreshed.updated_at)
        self.assertGreater(refreshed.updated_at, old)
        self.assertEqual(
            refreshed.cursor["synced_at"], refreshed.updated_at.isoformat()
        )

    def test_persisted_candidates_can_be_recovered_after_crash(self) -> None:
        uow = FakeUnitOfWork()
        _seed_tracked(uow)
        use_case = _use_case(uow, _refs())
        result = use_case.execute(_command())

        recovered = use_case.load_persisted_candidates(company_id=result.company_id)

        self.assertEqual(len(recovered), 2)
        self.assertEqual(
            recovered[0]["provider_document_id"],
            "cninfo-test-000001-20260701-annual",
        )


class ComputeSyncWindowTests(unittest.TestCase):
    TODAY = date(2026, 7, 14)

    def _window(self, **kwargs):
        uow = FakeUnitOfWork()
        return compute_sync_window(
            uow_factory=lambda: uow,
            company="000001",
            exchange="SZSE",
            today=self.TODAY,
            overlap_days=7,
            explicit_window_days=kwargs.pop("explicit_window_days", None),
            **kwargs,
        )

    def test_explicit_date_range_wins(self) -> None:
        self.assertEqual(
            self._window(
                explicit_window_start=date(2019, 1, 1),
                explicit_window_end=date(2019, 12, 31),
            ),
            (date(2019, 1, 1), date(2019, 12, 31)),
        )

    def test_range_and_window_days_are_mutually_exclusive(self) -> None:
        with self.assertRaises(ValueError):
            self._window(
                explicit_window_days=30,
                explicit_window_start=date(2019, 1, 1),
                explicit_window_end=date(2019, 12, 31),
            )

    def test_range_requires_both_ends_ordered_and_not_future(self) -> None:
        with self.assertRaises(ValueError):
            self._window(explicit_window_start=date(2019, 1, 1))
        with self.assertRaises(ValueError):
            self._window(
                explicit_window_start=date(2020, 1, 1),
                explicit_window_end=date(2019, 1, 1),
            )
        with self.assertRaises(ValueError):
            self._window(
                explicit_window_start=date(2026, 7, 1),
                explicit_window_end=date(2027, 1, 1),
            )


class CheckpointMonotonicTests(unittest.TestCase):
    def test_historical_backfill_does_not_regress_cursor(self) -> None:
        # A [2019, 2019] repair sync must not drag window_end back to 2019 —
        # the next worker round would re-sync years of index (round23).
        uow = FakeUnitOfWork()
        _seed_tracked(uow)
        use_case = _use_case(uow, _refs())
        first = use_case.execute(_command())  # window_end = 2026-07-02
        backfill = use_case.execute(
            SyncDisclosureIndexCommand(
                security_code="000001",
                exchange="SZSE",
                window_start=date(2019, 1, 1),
                window_end=date(2019, 12, 31),
            )
        )

        checkpoint = uow.source_checkpoints.get(backfill.checkpoint_id)
        self.assertEqual(backfill.checkpoint_id, first.checkpoint_id)
        self.assertEqual(checkpoint.cursor["window_end"], "2026-07-02")
        # Audit fields still record what the backfill actually covered.
        self.assertEqual(checkpoint.cursor["window_start"], "2019-01-01")


class FakeCninfoSource:
    def __init__(self, refs: list[AnnouncementRef]) -> None:
        self.refs = refs
        self.calls: list[tuple[SourceSecurity, DisclosureWindow, tuple[str, ...] | None]] = []

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
    ) -> list[AnnouncementRef]:
        self.calls.append((security, window))
        return self.refs

    def download_pdf(self, ref: AnnouncementRef) -> bytes:
        raise AssertionError("P4 sync must not download PDFs")


class FailingIndexSourceAccessRepo(SourceAccessRepo):
    def add(self, item: e.SourceAccess) -> e.SourceAccess:
        if item.provider_interface == INDEX_INTERFACE:
            raise RuntimeError("forced source_access persistence failure")
        return super().add(item)


def _use_case(uow: FakeUnitOfWork, refs: list[AnnouncementRef]) -> SyncDisclosureIndex:
    return SyncDisclosureIndex(
        source=FakeCninfoSource(refs),
        profile_loader=lambda _: _profile(),
        uow_factory=lambda: uow,
    )


def _command() -> SyncDisclosureIndexCommand:
    return SyncDisclosureIndexCommand(
        security_code="000001",
        exchange="SZSE",
        window_start=date(2026, 7, 1),
        window_end=date(2026, 7, 2),
    )


CATEGORY_NAMES = {
    "010301": "年度报告",
    "010112": "深市公司公告",
    "0120": "投资者关系",
    "012001": "投资者关系信息",
}


def _refs() -> list[AnnouncementRef]:
    payload = json.loads(
        (FIXTURE_ROOT / "p_info3015_sample.json").read_text(encoding="utf-8")
    )
    return [
        replace(
            ref,
            filing_type=map_filing_type(
                ref.raw_category, category_names_by_code=CATEGORY_NAMES
            ),
        )
        for ref in (map_p_info3015_record(record) for record in payload["records"])
    ]


def _profile() -> SourceCompanyProfile:
    payload = json.loads(
        (FIXTURE_ROOT / "p_stock2100_sample.json").read_text(encoding="utf-8")
    )
    return map_p_stock2100_record(payload["records"][0])


if __name__ == "__main__":
    unittest.main()
