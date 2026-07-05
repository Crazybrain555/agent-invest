"""Unit tests for CNINFO index sync use case."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
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
    SourceSecurity,
)
from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    INDEX_INTERFACE,
    SyncDisclosureIndex,
    SyncDisclosureIndexCommand,
)
from disclosure_anchor.domain import entities as e
from tests.unit._fakes import FakeUnitOfWork, SourceAccessRepo


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "cninfo"


class SyncDisclosureIndexTests(unittest.TestCase):
    def test_persists_candidates_before_advancing_checkpoint(self) -> None:
        uow = FakeUnitOfWork()
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
        self.assertEqual(checkpoint.cursor, {"window_end": "2026-07-02"})
        self.assertTrue(checkpoint.scope_key.endswith(":p_info3015"))
        self.assertEqual(uow.commit_count, 1)

    def test_empty_index_writes_empty_snapshot_and_hash(self) -> None:
        uow = FakeUnitOfWork()
        use_case = _use_case(uow, [])

        result = use_case.execute(_command())

        index_access = uow.source_accesses.get(result.index_source_access_id)
        self.assertTrue(result.empty)
        self.assertEqual(index_access.result_snapshot, {"result": "empty", "candidates": []})
        self.assertTrue(index_access.result_hash.startswith("sha256:"))

    def test_cninfo_org_id_and_tracked_company_are_recorded(self) -> None:
        uow = FakeUnitOfWork()
        use_case = _use_case(uow, _refs())

        result = use_case.execute(_command())

        tracked = uow.tracked_companies.get_by_company_id(result.company_id)
        identifier = uow.company_identifiers.get_by_scheme_value(
            "cninfo_org_id", "cninfo-org-test-000001"
        )
        self.assertEqual(tracked.security_id, result.security_id)
        self.assertEqual(tracked.filing_categories, ["0103", "0120"])
        self.assertEqual(identifier.company_id, result.company_id)
        self.assertEqual(identifier.source_access_id, result.profile_source_access_id)

    def test_checkpoint_does_not_advance_when_candidate_persistence_fails(self) -> None:
        uow = FakeUnitOfWork()
        uow.source_accesses = FailingIndexSourceAccessRepo()
        use_case = _use_case(uow, _refs())

        with self.assertRaises(RuntimeError):
            use_case.execute(_command())

        self.assertEqual(uow.source_checkpoints.all(), [])

    def test_persisted_candidates_can_be_recovered_after_crash(self) -> None:
        uow = FakeUnitOfWork()
        use_case = _use_case(uow, _refs())
        result = use_case.execute(_command())

        recovered = use_case.load_persisted_candidates(company_id=result.company_id)

        self.assertEqual(len(recovered), 2)
        self.assertEqual(
            recovered[0]["provider_document_id"],
            "cninfo-test-000001-20260701-annual",
        )


class FakeCninfoSource:
    def __init__(self, refs: list[AnnouncementRef]) -> None:
        self.refs = refs
        self.calls: list[tuple[SourceSecurity, DisclosureWindow, tuple[str, ...] | None]] = []

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
        categories: tuple[str, ...] | None = None,
    ) -> list[AnnouncementRef]:
        self.calls.append((security, window, categories))
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
        categories=("0103", "0120"),
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


def _profile() -> object:
    payload = json.loads(
        (FIXTURE_ROOT / "p_stock2100_sample.json").read_text(encoding="utf-8")
    )
    return map_p_stock2100_record(payload["records"][0])


if __name__ == "__main__":
    unittest.main()
