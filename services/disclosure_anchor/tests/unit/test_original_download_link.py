"""A recorded link is an external reference, not proof of current PDF bytes."""

from dataclasses import replace
from datetime import datetime, timezone
import unittest

from disclosure_anchor.api.schemas.original_download_link import OriginalDownloadLinkOut
from disclosure_anchor.application.ports.original_download_link import OriginalDownloadLinkRecord
from disclosure_anchor.application.use_cases.read_original_download_link import (
    DownloadLinkDocumentNotFound, ReadOriginalDownloadLink, supported_publisher_url,
)


class Reader:
    def __init__(self, record: OriginalDownloadLinkRecord | None) -> None:
        self.record = record
        self.calls: list[str] = []

    def read(self, document_id: str) -> OriginalDownloadLinkRecord | None:
        self.calls.append(document_id)
        return self.record


class OriginalDownloadLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = OriginalDownloadLinkRecord(
            document_id="doc_01M49JD4FDZT6GERD01X7BV499", provider="cninfo",
            provider_document_id="synthetic", raw_file_hash="sha256:" + "a" * 64,
            source_ref="source://synthetic", recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            recorded_url="http://static.cninfo.com.cn/finalpage/2026-01-01/synthetic.PDF",
        )

    def test_stored_url_and_archive_identity_preserved_without_live_claim(self) -> None:
        reader = Reader(self.record)
        result = ReadOriginalDownloadLink(reader).execute(self.record.document_id)
        OriginalDownloadLinkOut.model_validate(result)
        self.assertEqual(result["url"], self.record.recorded_url)
        self.assertEqual(result["raw_file_hash"], self.record.raw_file_hash)
        self.assertFalse(result["link_live_checked"])
        self.assertTrue(result["hash_verification_required"])
        self.assertEqual(reader.calls, [self.record.document_id])

    def test_absent_recorded_url_does_not_synthesize_from_provider_id(self) -> None:
        result = ReadOriginalDownloadLink(Reader(replace(
            self.record, recorded_url=None, recorded_at=None,
        ))).execute(self.record.document_id)
        self.assertIsNone(result["url"])
        self.assertIsNone(result["url_source"])
        self.assertEqual(result["unavailable_reason"], "no_matching_recorded_url")

    def test_unsupported_sources_are_not_exposed(self) -> None:
        for url in (
            "file:///private/original.pdf", "http://127.0.0.1/private.pdf",
            "https://static.cninfo.com.cn.evil.example/file.pdf",
            "https://name:secret@static.cninfo.com.cn/file.pdf",
            "https://static.cninfo.com.cn/file.pdf?token=secret",
            "https://static.cninfo.com.cn:8080/file.pdf", "\nhttps://static.cninfo.com.cn/file.pdf",
            "https://static.cninfo.com.cn/file.pdf#secret", "https://static.cninfo.com.cn/not-pdf",
        ):
            with self.subTest(url=url):
                result = ReadOriginalDownloadLink(Reader(replace(
                    self.record, recorded_url=url,
                ))).execute(self.record.document_id)
                self.assertIsNone(result["url"])
                self.assertEqual(result["unavailable_reason"], "unsupported_publisher_url")
        self.assertFalse(supported_publisher_url("unknown", self.record.recorded_url or ""))

    def test_unknown_document_remains_missing_and_db_failure_remains_failure(self) -> None:
        with self.assertRaises(DownloadLinkDocumentNotFound):
            ReadOriginalDownloadLink(Reader(None)).execute(self.record.document_id)

        class BrokenReader(Reader):
            def read(self, document_id: str) -> OriginalDownloadLinkRecord | None:
                raise RuntimeError("synthetic database outage")

        with self.assertRaisesRegex(RuntimeError, "database outage"):
            ReadOriginalDownloadLink(BrokenReader(None)).execute(self.record.document_id)
