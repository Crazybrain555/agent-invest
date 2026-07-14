"""DB-gated CNINFO sync integration tests using FakeCninfoSource."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import unittest

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.sources.cninfo.mapper import (
    map_p_info3015_record,
    map_p_stock2100_record,
)
from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureWindow,
    SourceSecurity,
)
from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    SyncDisclosureIndex,
    SyncDisclosureIndexCommand,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from tests.integration._support import engine_or_skip


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "cninfo"


class CninfoSyncIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self._cleanup_by_security_code()
        self.created_company_ids: list[str] = []
        self.created_security_ids: list[str] = []
        self.created_source_access_ids: list[str] = []
        self.created_checkpoint_ids: list[str] = []

    def tearDown(self) -> None:
        self._cleanup_created_rows()
        self.engine.dispose()

    def _cleanup_by_security_code(self) -> None:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT company_id, security_id FROM disclosure_core.security "
                    "WHERE security_code = 'T07SYNC' AND exchange = 'LOCAL'"
                )
            ).all()
            self._cleanup_company_security_rows(
                conn,
                company_ids=[row.company_id for row in rows],
                security_ids=[row.security_id for row in rows],
            )

    def _cleanup_created_rows(self) -> None:
        with self.engine.begin() as conn:
            for checkpoint_id in self.created_checkpoint_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.source_checkpoint "
                        "WHERE source_checkpoint_id = :id"
                    ),
                    {"id": checkpoint_id},
                )
            for company_id in self.created_company_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.tracked_company "
                        "WHERE company_id = :id"
                    ),
                    {"id": company_id},
                )
            for company_id in self.created_company_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.company_identifier "
                        "WHERE company_id = :id"
                    ),
                    {"id": company_id},
                )
            for source_access_id in self.created_source_access_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.source_access "
                        "WHERE source_access_id = :id"
                    ),
                    {"id": source_access_id},
                )
            for security_id in self.created_security_ids:
                conn.execute(
                    text("DELETE FROM disclosure_core.security WHERE security_id = :id"),
                    {"id": security_id},
                )
            for company_id in self.created_company_ids:
                conn.execute(
                    text("DELETE FROM disclosure_core.company WHERE company_id = :id"),
                    {"id": company_id},
                )

    def _cleanup_company_security_rows(
        self,
        conn: object,
        *,
        company_ids: list[str],
        security_ids: list[str],
    ) -> None:
        for company_id in company_ids:
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.source_checkpoint "
                    "WHERE scope_key = :scope"
                ),
                {"scope": f"{company_id}:p_info3015"},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.tracked_company "
                    "WHERE company_id = :id"
                ),
                {"id": company_id},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.company_identifier "
                    "WHERE company_id = :id"
                ),
                {"id": company_id},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.source_access "
                    "WHERE company_id = :id"
                ),
                {"id": company_id},
            )
        conn.execute(
            text(
                "DELETE FROM disclosure_core.source_access "
                "WHERE provider = 'cninfo' "
                "AND provider_interface = 'cninfo:p_stock2100' "
                "AND query_params ->> 'scode' = 'T07SYNC'"
            )
        )
        for security_id in security_ids:
            conn.execute(
                text("DELETE FROM disclosure_core.security WHERE security_id = :id"),
                {"id": security_id},
            )
        for company_id in company_ids:
            conn.execute(
                text("DELETE FROM disclosure_core.company WHERE company_id = :id"),
                {"id": company_id},
            )

    def _seed_tracked(self) -> None:
        """Sync requires prior pool membership (round23); seed like `make track`."""

        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            company = uow.companies.add(
                e.Company(
                    company_id=ids.new_company_id(),
                    legal_name="P4 CNINFO Sync Integration Co",
                )
            )
            security = uow.securities.add(
                e.Security(
                    security_id=ids.new_security_id(),
                    company_id=company.company_id,
                    security_code="T07SYNC",
                    exchange="LOCAL",
                )
            )
            uow.tracked_companies.add(
                e.TrackedCompany(
                    tracked_company_id=ids.new_tracked_company_id(),
                    company_id=company.company_id,
                    security_id=security.security_id,
                    status="active",
                )
            )
            uow.commit()
        self.created_company_ids.append(company.company_id)
        self.created_security_ids.append(security.security_id)

    def test_sync_persists_candidates_and_checkpoint(self) -> None:
        self._seed_tracked()
        use_case = SyncDisclosureIndex(
            source=FakeCninfoSource(_refs()),
            profile_loader=lambda _: _profile(),
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=self.engine),
        )

        result = use_case.execute(_command())
        self.created_source_access_ids.extend(
            [result.profile_source_access_id, result.index_source_access_id]
        )
        self.created_checkpoint_ids.append(result.checkpoint_id)

        with self.engine.connect() as conn:
            index_row = conn.execute(
                text(
                    "SELECT result_snapshot, result_hash, query_params "
                    "FROM disclosure_core.source_access "
                    "WHERE source_access_id = :id"
                ),
                {"id": result.index_source_access_id},
            ).one()
            checkpoint_row = conn.execute(
                text(
                    "SELECT provider, scope_key, cursor "
                    "FROM disclosure_core.source_checkpoint "
                    "WHERE source_checkpoint_id = :id"
                ),
                {"id": result.checkpoint_id},
            ).one()
            tracked_count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.tracked_company "
                    "WHERE company_id = :id"
                ),
                {"id": result.company_id},
            ).scalar_one()

        self.assertEqual(len(index_row.result_snapshot["candidates"]), 2)
        self.assertTrue(index_row.result_hash.startswith("sha256:"))
        self.assertNotIn("access_token", index_row.query_params)
        self.assertEqual(checkpoint_row.provider, "cninfo")
        self.assertTrue(checkpoint_row.scope_key.endswith(":p_info3015"))
        self.assertEqual(checkpoint_row.cursor["window_end"], "2026-07-02")
        self.assertIn("window_start", checkpoint_row.cursor)
        self.assertIn("synced_at", checkpoint_row.cursor)
        self.assertEqual(tracked_count, 1)


class FakeCninfoSource:
    def __init__(self, refs: list[AnnouncementRef]) -> None:
        self.refs = refs

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
    ) -> list[AnnouncementRef]:
        return self.refs

    def download_pdf(self, ref: AnnouncementRef) -> bytes:
        raise AssertionError("sync integration should not download PDFs")


def _command() -> SyncDisclosureIndexCommand:
    return SyncDisclosureIndexCommand(
        security_code="T07SYNC",
        exchange="LOCAL",
        window_start=date(2026, 7, 1),
        window_end=date(2026, 7, 2),
    )


def _refs() -> list[AnnouncementRef]:
    payload = json.loads(
        (FIXTURE_ROOT / "p_info3015_sample.json").read_text(encoding="utf-8")
    )
    refs = [map_p_info3015_record(record) for record in payload["records"]]
    return [
        AnnouncementRef(
            provider=ref.provider,
            provider_document_id=ref.provider_document_id,
            title=ref.title,
            download_url=ref.download_url,
            raw_category=ref.raw_category,
            announcement_date=ref.announcement_date,
            security_code="T07SYNC",
            security_name=ref.security_name,
            file_size=ref.file_size,
            index_updated_at=ref.index_updated_at,
            object_id=ref.object_id,
            rec_id=ref.rec_id,
            format=ref.format,
            market_code=ref.market_code,
            market_name=ref.market_name,
            raw_record=ref.raw_record,
        )
        for ref in refs
    ]


def _profile() -> object:
    payload = json.loads(
        (FIXTURE_ROOT / "p_stock2100_sample.json").read_text(encoding="utf-8")
    )
    base = map_p_stock2100_record(payload["records"][0])
    return type(base)(
        security_code="T07SYNC",
        security_name="P4测试证券",
        legal_name="P4 CNINFO Sync Integration Co",
        provider_org_id="cninfo-org-test-t07sync",
        uscc=None,
    )


if __name__ == "__main__":
    unittest.main()
