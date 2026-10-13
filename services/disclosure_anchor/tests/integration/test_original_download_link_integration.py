"""Recorded source binding, using only the marker-verified scratch database."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.original_download_link import (
    PostgresOriginalDownloadLinkReader,
)
from disclosure_anchor.application.use_cases.read_original_download_link import (
    DownloadLinkDocumentNotFound, ReadOriginalDownloadLink,
)
from disclosure_anchor.domain.ids import new_ulid
from tests.integration._support import engine_or_skip


class OriginalDownloadLinkIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        # This validates runner DB name/comment and agreeing environment before
        # any mutation. No runtime DSN fallback is permitted by this helper.
        self.engine = engine_or_skip()
        self.addCleanup(self.engine.dispose)
        self.suffix = new_ulid()
        self.company = "co_link_" + self.suffix
        self.other_company = "co_other_link_" + self.suffix
        self.security = "sec_link_" + self.suffix
        self.document = "doc_" + new_ulid()
        self.pid = "link-" + self.suffix
        self.code = "99" + str(int(hashlib.sha256(self.suffix.encode()).hexdigest()[:8], 16) % 10000).zfill(4)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sources: list[str] = []
        self.addCleanup(self._clean_fixture)
        # The actual adapter opens a separate read-only transaction. Commit only
        # run-unique synthetic rows inside the already verified scratch DB.
        with self.engine.begin() as conn:
            for company in (self.company, self.other_company):
                conn.execute(text("INSERT INTO disclosure_core.company(company_id,legal_name) VALUES (:id,'Synthetic link fixture')"), {"id": company})
            conn.execute(text("INSERT INTO disclosure_core.security(security_id,company_id,security_code,exchange) VALUES (:id,:company,:code,'SSE')"),
                         {"id": self.security, "company": self.company, "code": self.code})
            conn.execute(text(
                "INSERT INTO disclosure_core.document(document_id,provider,provider_document_id,"
                "security_id,company_id,title,announcement_date,raw_file_relpath,raw_file_hash,status) "
                "VALUES (:id,'cninfo',:pid,:security,:company,'Synthetic recorded link','2026-01-01',"
                "'raw/synthetic/recorded.pdf',:hash,'registered')"
            ), {"id": self.document, "pid": self.pid, "security": self.security,
                "company": self.company, "hash": "sha256:" + "a" * 64})
        self.reader = PostgresOriginalDownloadLinkReader(self.engine)
        self.use_case = ReadOriginalDownloadLink(self.reader)

    def _clean_fixture(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM disclosure_core.document WHERE document_id=:id"), {"id": self.document})
            for source in self.sources:
                conn.execute(text("DELETE FROM disclosure_core.source_access WHERE source_access_id=:id"), {"id": source})
            conn.execute(text("DELETE FROM disclosure_core.security WHERE security_id=:id"), {"id": self.security})
            for company in (self.company, self.other_company):
                conn.execute(text("DELETE FROM disclosure_core.company WHERE company_id=:id"), {"id": company})

    def _index(self, *, delta=0, company=None, provider="cninfo", status="ok",
               interface="cninfo:p_info3015", candidate_overrides=None, snapshot=None):
        source = "sa_" + new_ulid()
        candidate = {"provider_document_id": self.pid, "security_code": self.code,
                     "exchange": "SSE", "announcement_date": "2026-01-01",
                     "download_url": "https://static.cninfo.com.cn/finalpage/synthetic-old.pdf"}
        candidate.update(candidate_overrides or {})
        snapshot = {"candidates": [candidate]} if snapshot is None else snapshot
        self.sources.append(source)
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO disclosure_core.source_access(source_access_id,provider,provider_interface,"
                "status,accessed_at,result_snapshot,company_id) "
                "VALUES (:id,:provider,:interface,:status,:at,CAST(:snapshot AS jsonb),:company)"
            ), {"id": source, "provider": provider, "interface": interface, "status": status,
                "at": self.now + timedelta(seconds=delta), "snapshot": json.dumps(snapshot),
                "company": self.company if company is None else company})
        return source

    def test_exact_binding_ignores_newer_adjacent_negative_source_records(self) -> None:
        self._index()
        adjacent = [
            {"company": self.other_company}, {"provider": "other-provider"},
            {"status": "failed"}, {"interface": "cninfo:unrelated"},
            {"candidate_overrides": {"provider_document_id": self.pid + "-other"}},
            {"candidate_overrides": {"security_code": "000001"}},
            {"candidate_overrides": {"exchange": "SZSE"}},
            {"candidate_overrides": {"announcement_date": "2026-01-02"}},
            {"candidate_overrides": {"download_url": None}},
            {"candidate_overrides": {"download_url": ""}},
            {"snapshot": {"candidates": {"download_url": "wrong-shape"}}},
        ]
        for index, changes in enumerate(adjacent, 1):
            with self.subTest(changes=changes):
                self._index(delta=index, **changes)
                result = self.use_case.execute(self.document)
                self.assertEqual(result["url"], "https://static.cninfo.com.cn/finalpage/synthetic-old.pdf")
                self.assertEqual(datetime.fromisoformat(result["recorded_at"]), self.now)
                self.assertEqual(result["raw_file_hash"], "sha256:" + "a" * 64)
                self.assertFalse(result["link_live_checked"])

    def test_latest_successful_matching_url_is_preserved_and_unsupported_is_honest(self) -> None:
        self._index()
        latest = "http://static.cninfo.com.cn/finalpage/Synthetic-New.PDF"
        self._index(delta=10, interface="cninfo:hisAnnouncement", candidate_overrides={"download_url": latest})
        result = self.use_case.execute(self.document)
        self.assertEqual(result["url"], latest)
        self.assertEqual(datetime.fromisoformat(result["recorded_at"]), self.now + timedelta(seconds=10))
        self.assertEqual(result["url_source"], "recorded_official_index")
        self.assertTrue(result["hash_verification_required"])
        # Latest observation wins; no guessed URL or silent older-link fallback.
        self._index(delta=20, candidate_overrides={"download_url": "https://untrusted.invalid/private.pdf"})
        result = self.use_case.execute(self.document)
        self.assertEqual(result["unavailable_reason"], "unsupported_publisher_url")
        for key in ("url", "url_source", "recorded_at"):
            self.assertIsNone(result[key])

    def test_missing_document_is_distinct_from_existing_document_without_recorded_link(self) -> None:
        result = self.use_case.execute(self.document)
        self.assertEqual(result["document_id"], self.document)
        self.assertEqual(result["unavailable_reason"], "no_matching_recorded_url")
        self.assertIsNone(result["url"])
        self.assertIsNone(self.reader.read("doc_" + new_ulid()))
        with self.assertRaises(DownloadLinkDocumentNotFound):
            self.use_case.execute("doc_" + new_ulid())


if __name__ == "__main__":
    unittest.main()
