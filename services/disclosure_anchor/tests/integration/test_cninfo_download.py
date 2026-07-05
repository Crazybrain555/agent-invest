"""DB-gated CNINFO sync -> download -> register integration test."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.sources.cninfo.mapper import (
    map_p_info3015_record,
    map_p_stock2100_record,
)
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureWindow,
    SourceSecurity,
)
from disclosure_anchor.application.use_cases.download_document import (
    DownloadDocument,
    DownloadDocumentCommand,
)
from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    SyncDisclosureIndex,
    SyncDisclosureIndexCommand,
)
from disclosure_anchor.settings import Settings
from tests.integration._support import engine_or_skip


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "cninfo"


class CninfoDownloadIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmpdir.name))
        self._cleanup()

    def tearDown(self) -> None:
        self._cleanup()
        self.engine.dispose()
        self.tmpdir.cleanup()

    def test_sync_download_register_full_chain_with_fake_source(self) -> None:
        source = FakeCninfoSource(_refs(), _pdf_bytes())
        sync = SyncDisclosureIndex(
            source=source,
            profile_loader=lambda _: _profile(),
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=self.engine),
        )
        sync_result = sync.execute(_command())
        downloader = DownloadDocument(
            source=source,
            raw_store=RawDocumentStore(FileStorePathBuilder(self.settings)),
            path_builder=FileStorePathBuilder(self.settings),
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=self.engine),
        )
        pending = [
            candidate
            for candidate in downloader.list_pending_candidates(
                max_retries=3, overlap_start=date(2026, 6, 25)
            )
            # The shared live DB may hold real cninfo candidates; only this
            # test's namespaced fixtures may be driven through the fake source.
            if str(candidate.get("provider_document_id", "")).startswith("cninfo-test-")
        ]
        self.assertTrue(pending, "expected the test fixture candidate to be pending")

        result = downloader.execute(DownloadDocumentCommand(candidate=pending[0]))

        self.assertIsNotNone(result.document_id)
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT provider_document_id, raw_file_relpath, provider_metadata "
                    "FROM disclosure_core.document WHERE document_id = :id"
                ),
                {"id": result.document_id},
            ).one()
            event_count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_ops.outbox_event "
                    "WHERE document_id = :id AND event_kind = 'document_registered'"
                ),
                {"id": result.document_id},
            ).scalar_one()

        self.assertEqual(row.provider_document_id, pending[0]["provider_document_id"])
        self.assertTrue(
            (self.settings.disclosure_data_root / "data" / row.raw_file_relpath).is_file()
        )
        self.assertEqual(row.provider_metadata["file_signature"]["file_size"], 512)
        self.assertEqual(event_count, 1)
        self.assertEqual(sync_result.candidate_count, 1)

    def _cleanup(self) -> None:
        with self.engine.begin() as conn:
            document_ids = [
                row.document_id
                for row in conn.execute(
                    text(
                        "SELECT document_id FROM disclosure_core.document "
                        "WHERE provider = 'cninfo' "
                        "AND provider_document_id LIKE 'cninfo-test-000001-%'"
                    )
                )
            ]
            for document_id in document_ids:
                conn.execute(
                    text("DELETE FROM disclosure_ops.outbox_event WHERE document_id = :id"),
                    {"id": document_id},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.document WHERE document_id = :id"),
                    {"id": document_id},
                )
            rows = conn.execute(
                text(
                    "SELECT company_id, security_id FROM disclosure_core.security "
                    "WHERE security_code = 'T07DOWN' AND exchange = 'LOCAL'"
                )
            ).all()
            company_ids = [row.company_id for row in rows]
            security_ids = [row.security_id for row in rows]
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
                    "AND query_params ->> 'scode' = 'T07DOWN'"
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


class FakeCninfoSource:
    def __init__(self, refs: list[AnnouncementRef], pdf_bytes: bytes) -> None:
        self.refs = refs
        self.pdf_bytes = pdf_bytes

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
        categories: tuple[str, ...] | None = None,
    ) -> list[AnnouncementRef]:
        return self.refs

    def download_pdf(self, ref: AnnouncementRef) -> bytes:
        owned = {item.provider_document_id for item in self.refs}
        if ref.provider_document_id not in owned:
            raise AssertionError(
                f"fake source asked to download foreign ref {ref.provider_document_id}"
            )
        return self.pdf_bytes


def _settings(root: Path) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        mineru_model_cache=shared_root / "model_cache" / "mineru",
        hf_home=shared_root / "model_cache" / "huggingface",
        modelscope_cache=shared_root / "model_cache" / "modelscope",
    )


def _command() -> SyncDisclosureIndexCommand:
    return SyncDisclosureIndexCommand(
        security_code="T07DOWN",
        exchange="LOCAL",
        window_start=date(2026, 7, 1),
        window_end=date(2026, 7, 2),
        categories=("0103",),
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
            security_code="T07DOWN",
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
        for ref in refs[:1]
    ]


def _profile() -> object:
    payload = json.loads(
        (FIXTURE_ROOT / "p_stock2100_sample.json").read_text(encoding="utf-8")
    )
    base = map_p_stock2100_record(payload["records"][0])
    return type(base)(
        security_code="T07DOWN",
        security_name="P5下载证券",
        legal_name="P5 CNINFO Download Integration Co",
        provider_org_id="cninfo-org-test-t07down",
        uscc=None,
    )


def _pdf_bytes() -> bytes:
    return (FIXTURE_ROOT / "sample_announcement.pdf").read_bytes()


if __name__ == "__main__":
    unittest.main()
