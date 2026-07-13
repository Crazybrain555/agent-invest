"""Tests for pending CNINFO download and register-document reuse."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
import unittest

from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureWindow,
    SourceSecurity,
)
from disclosure_anchor.application.ports.file_store import (
    QuarantineResult,
    RawDocumentVerification,
    RawDocumentWriteResult,
)
from disclosure_anchor.application.use_cases.download_document import (
    DOWNLOAD_INTERFACE,
    DownloadDocument,
    DownloadDocumentCommand,
)
from disclosure_anchor.application.use_cases.sync_disclosure_index import INDEX_INTERFACE
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import InvalidRawDocumentError, SourceRequestError
from tests.unit._fakes import FakeUnitOfWork


class DownloadDocumentTests(unittest.TestCase):
    def test_lists_pending_candidates_from_persisted_source_access(self) -> None:
        uow = _uow_with_subject()
        candidate = _candidate()
        uow.source_accesses.add(
            e.SourceAccess(
                source_access_id="sa_index",
                provider="cninfo",
                provider_interface=INDEX_INTERFACE,
                accessed_at=datetime.now(timezone.utc),
                status="ok",
                result_snapshot={"result": "ok", "candidates": [candidate]},
                company_id="co_1",
                security_id="sec_1",
            )
        )
        use_case = _use_case(uow, [b"%PDF-1.4\nsame\n%%EOF\n"])

        pending = use_case.list_pending_candidates(
            max_retries=3, overlap_start=date(2026, 6, 25)
        )

        self.assertEqual(pending[0]["provider_document_id"], "pid-1")

    def test_download_archives_and_registers_document(self) -> None:
        uow = _uow_with_subject()
        use_case = _use_case(uow, [b"%PDF-1.4\none\n%%EOF\n"])

        result = use_case.execute(
            DownloadDocumentCommand(candidate=_candidate(), oversized_kb=1024)
        )

        self.assertIsNotNone(result.document_id)
        document = uow.documents.get(result.document_id)
        self.assertEqual(document.provider_document_id, "pid-1")
        self.assertEqual(document.provider_metadata["raw_category"], "010301")
        self.assertEqual(document.provider_metadata["file_signature"]["file_size"], 2048)
        self.assertEqual(document.provider_metadata["oversized"], True)
        source_access = uow.source_accesses.get(result.source_access_id)
        self.assertEqual(source_access.provider_interface, DOWNLOAD_INTERFACE)
        self.assertEqual(uow.commit_count, 1)

    def test_same_provider_document_changed_file_supersedes_via_register_core(self) -> None:
        uow = _uow_with_subject()
        use_case = _use_case(
            uow,
            [
                b"%PDF-1.4\nold\n%%EOF\n",
                b"%PDF-1.4\nnew\n%%EOF\n",
            ],
        )

        first = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))
        second = use_case.execute(DownloadDocumentCommand(candidate=_candidate(file_size=4096)))

        self.assertNotEqual(second.document_id, first.document_id)
        self.assertEqual(
            uow.documents.get(second.document_id).supersedes_document_id,
            first.document_id,
        )

    def test_same_provider_document_same_hash_reuses_existing_document(self) -> None:
        uow = _uow_with_subject()
        payload = b"%PDF-1.4\nsame\n%%EOF\n"
        use_case = _use_case(uow, [payload, payload])

        first = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))
        second = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))

        self.assertTrue(second.reused_existing_document)
        self.assertEqual(second.document_id, first.document_id)

    def test_non_pdf_is_quarantined_and_records_failed_source_access(self) -> None:
        uow = _uow_with_subject()
        raw_store = FakeRawStore()
        use_case = DownloadDocument(
            source=FakeDownloadSource([b"not a pdf"]),
            raw_store=raw_store,
            path_builder=FakePathBuilder(),
            uow_factory=lambda: uow,
        )

        result = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))

        self.assertIsNone(result.document_id)
        self.assertEqual(result.quarantine_reason, "invalid_raw_document")
        source_access = uow.source_accesses.get(result.source_access_id)
        self.assertEqual(source_access.status, "failed")
        self.assertEqual(source_access.query_params["provider_document_id"], "pid-1")
        self.assertIn('"retryable":false', source_access.error)

    def test_download_http_failure_records_failed_source_access(self) -> None:
        uow = _uow_with_subject()
        use_case = DownloadDocument(
            source=FailingDownloadSource(
                SourceRequestError(
                    "CNINFO download request failed",
                    error_code="http_404",
                    retryable=True,
                )
            ),
            raw_store=FakeRawStore(),
            path_builder=FakePathBuilder(),
            uow_factory=lambda: uow,
        )

        result = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))

        self.assertIsNone(result.document_id)
        self.assertIsNone(result.quarantined_path)
        self.assertEqual(result.error_code, "http_404")
        self.assertTrue(result.retryable)
        source_access = uow.source_accesses.get(result.source_access_id)
        self.assertEqual(source_access.status, "failed")
        self.assertIn('"error_code":"http_404"', source_access.error)
        self.assertIn('"retryable":true', source_access.error)
        self.assertIn('"stage":"download"', source_access.error)

    def test_missing_exchange_uses_inferred_mainland_identity(self) -> None:
        for code, exchange in (("600519", "SSE"), ("830001", "BSE")):
            with self.subTest(code=code, exchange=exchange):
                uow = _uow_with_listed_subject(code, exchange)
                candidate = _candidate()
                candidate["security_code"] = code
                candidate.pop("exchange")

                result = _use_case(
                    uow, [b"%PDF-1.4\nlisted\n%%EOF\n"]
                ).execute(DownloadDocumentCommand(candidate=candidate))

                self.assertIsNotNone(result.document_id)
                document = uow.documents.get(result.document_id)
                self.assertEqual(document.company_id, "co_listed")


class FailingDownloadSource:
    def __init__(self, error: SourceRequestError) -> None:
        self._error = error

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
        categories: tuple[str, ...] | None = None,
    ) -> list[AnnouncementRef]:
        raise AssertionError("download use case must not search")

    def download_pdf(self, ref: AnnouncementRef) -> bytes:
        raise self._error


class FakeDownloadSource:
    def __init__(self, payloads: list[bytes]) -> None:
        self.payloads = payloads

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
        categories: tuple[str, ...] | None = None,
    ) -> list[AnnouncementRef]:
        raise AssertionError("download use case must not search")

    def download_pdf(self, ref: AnnouncementRef) -> bytes:
        return self.payloads.pop(0)


class FakeRawStore:
    def put_raw_document(
        self,
        *,
        provider: str,
        security_code: str,
        year: int | str,
        provider_document_id: str,
        input_file: Path,
        expected_raw_file_hash: str | None = None,
    ) -> RawDocumentWriteResult:
        payload = input_file.read_bytes()
        if not payload.startswith(b"%PDF-"):
            raise InvalidRawDocumentError("input file is not a PDF")
        raw_hash = "sha256:" + hashlib.sha256(payload).hexdigest()
        return RawDocumentWriteResult(
            relpath=Path("raw_documents") / provider / security_code / str(year) / provider_document_id / "sample.pdf",
            raw_file_hash=raw_hash,
            byte_count=len(payload),
            created=True,
        )

    def verify_raw_document(
        self, *, relpath: Path, expected_hash: str
    ) -> RawDocumentVerification:
        raise AssertionError("not used")

    def quarantine_raw_document(
        self,
        *,
        provider: str,
        provider_document_id: str,
        input_file: Path,
        reason: str,
    ) -> QuarantineResult:
        return QuarantineResult(
            path=Path("quarantine") / f"{provider_document_id}.bin",
            reason="invalid_raw_document",
            byte_count=len(input_file.read_bytes()),
        )


class FakePathBuilder:
    def __init__(self) -> None:
        self.root = Path("/private/tmp") / f"download-doc-{ids.new_ulid()}"

    def runtime_tmp_path(self, name: str | None = None) -> Path:
        return self.root / (name or "")

    def raw_document_relpath(self, **_) -> Path:
        raise AssertionError("not used")

    def data_path(self, relpath: Path) -> Path:
        raise AssertionError("not used")

    def parser_artifacts_root_relpath(self, **_) -> Path:
        raise AssertionError("not used")

    def parser_run_artifacts_relpath(self, **_) -> Path:
        raise AssertionError("not used")

    def normalized_ir_relpath(self, **_) -> Path:
        raise AssertionError("not used")

    def normalized_ir_run_relpath(self, **_) -> Path:
        raise AssertionError("not used")

    def document_units_snapshot_relpath(self, **_) -> Path:
        raise AssertionError("not used")

    def runtime_quarantine_path(self, **_) -> Path:
        raise AssertionError("not used")


def _use_case(uow: FakeUnitOfWork, payloads: list[bytes]) -> DownloadDocument:
    return DownloadDocument(
        source=FakeDownloadSource(payloads),
        raw_store=FakeRawStore(),
        path_builder=FakePathBuilder(),
        uow_factory=lambda: uow,
    )


def _uow_with_subject() -> FakeUnitOfWork:
    uow = FakeUnitOfWork()
    company = uow.companies.add(
        e.Company(company_id="co_1", legal_name="P5 Test Co")
    )
    uow.securities.add(
        e.Security(
            security_id="sec_1",
            company_id=company.company_id,
            security_code="T07SYNC",
            exchange="LOCAL",
            status="active",
        )
    )
    return uow


def _uow_with_listed_subject(code: str, exchange: str) -> FakeUnitOfWork:
    uow = FakeUnitOfWork()
    company = uow.companies.add(
        e.Company(company_id="co_listed", legal_name="Listed Test Co")
    )
    uow.securities.add(
        e.Security(
            security_id="sec_listed",
            company_id=company.company_id,
            security_code=code,
            exchange=exchange,
            status="active",
        )
    )
    return uow


def _candidate(*, file_size: int = 2048) -> dict[str, object]:
    return {
        "provider_document_id": "pid-1",
        "title": "P5 Test Annual Report",
        "download_url": "https://static.cninfo.example/pid-1.PDF",
        "raw_category": "010301",
        "filing_type": "other",
        "announcement_date": "2026-07-01",
        "security_code": "T07SYNC",
        "exchange": "LOCAL",
        "security_name": "P5 Test",
        "provider_org_id": "org-p5",
        "object_id": 123,
        "rec_id": "rec-p5",
        "file_signature_hint": {
            "file_size": file_size,
            "etag": None,
            "last_modified": None,
            "index_updated_at": "2026-07-01T12:00:00+08:00",
        },
    }


if __name__ == "__main__":
    unittest.main()
