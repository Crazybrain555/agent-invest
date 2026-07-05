"""Phase 03 local PDF registration integration tests."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.runtime.doctor import run_raw_archive_checks
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.application.use_cases.register_local_pdf import (
    RegisterLocalPdf,
    RegisterLocalPdfCommand,
)
from disclosure_anchor.domain.errors import RegistrationMetadataError
from disclosure_anchor.domain.errors import SubjectIdentityConflictError
from disclosure_anchor.domain.ids import new_ulid
from disclosure_anchor.settings import Settings
from tests.integration._support import engine_or_skip


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


class RegisterLocalPdfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.settings = _settings(self.root)
        self.store = RawDocumentStore(FileStorePathBuilder(self.settings))
        self.use_case = RegisterLocalPdf(
            raw_store=self.store,
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=self.engine),
        )
        self.provider_document_ids: list[str] = []
        self.extra_security_ids: list[str] = []
        self.extra_company_ids: list[str] = []

    def tearDown(self) -> None:
        for provider_document_id in self.provider_document_ids:
            self._delete_by_provider_document_id(provider_document_id)
        self._delete_extra_security_company()
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _delete_extra_security_company(self) -> None:
        with self.engine.begin() as conn:
            for security_id in self.extra_security_ids:
                conn.execute(
                    text("DELETE FROM disclosure_core.security WHERE security_id = :id"),
                    {"id": security_id},
                )
            for company_id in self.extra_company_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.company_identifier "
                        "WHERE company_id = :id"
                    ),
                    {"id": company_id},
                )
            for company_id in self.extra_company_ids:
                conn.execute(
                    text("DELETE FROM disclosure_core.company WHERE company_id = :id"),
                    {"id": company_id},
                )

    def _delete_by_provider_document_id(self, provider_document_id: str) -> None:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT document_id, source_access_id, company_id, security_id "
                    "FROM disclosure_core.document "
                    "WHERE provider = 'cninfo' AND provider_document_id = :pid"
                ),
                {"pid": provider_document_id},
            ).all()
            document_ids = [row[0] for row in rows]
            source_access_ids = [row[1] for row in rows if row[1]]
            security_ids = [row[3] for row in rows if row[3]]
            company_ids = [row[2] for row in rows if row[2]]

            for document_id in document_ids:
                conn.execute(
                    text("DELETE FROM disclosure_ops.outbox_event WHERE document_id = :id"),
                    {"id": document_id},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.document WHERE document_id = :id"),
                    {"id": document_id},
                )
            for source_access_id in source_access_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.source_access "
                        "WHERE source_access_id = :id"
                    ),
                    {"id": source_access_id},
                )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.source_access "
                    "WHERE provider = 'cninfo' "
                    "AND query_params ->> 'provider_document_id' = :pid"
                ),
                {"pid": provider_document_id},
            )
            for security_id in security_ids:
                conn.execute(
                    text("DELETE FROM disclosure_core.security WHERE security_id = :id"),
                    {"id": security_id},
                )
            for company_id in company_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.company_identifier "
                        "WHERE company_id = :id"
                    ),
                    {"id": company_id},
                )
            for company_id in company_ids:
                conn.execute(
                    text("DELETE FROM disclosure_core.company WHERE company_id = :id"),
                    {"id": company_id},
                )

    def _pdf(self, name: str, payload: bytes = b"sample") -> Path:
        path = self.root / name
        path.write_bytes(b"%PDF-1.4\n" + payload + b"\n%%EOF\n")
        return path

    def _command(self, provider_document_id: str, file_path: Path) -> RegisterLocalPdfCommand:
        return RegisterLocalPdfCommand(
            file_path=file_path,
            company_legal_name=f"Phase03 Test Co {provider_document_id}",
            security_code="T03" + provider_document_id[-4:],
            exchange="LOCAL",
            filing_type="annual_report",
            title="Phase 03 local PDF",
            announcement_date=date(2026, 6, 29),
            report_period="2025A",
            provider_document_id=provider_document_id,
            provider="cninfo",
        )

    def test_register_local_pdf_writes_raw_and_db_metadata(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)

        result = self.use_case.execute(
            self._command(provider_document_id, self._pdf("one.pdf"))
        )

        self.assertIsNotNone(result.document_id)
        self.assertFalse(result.reused_existing_document)
        self.assertIsNone(result.quarantined_path)
        self.assertTrue(result.raw_file_relpath.startswith("raw_documents/cninfo/"))
        self.assertTrue(result.raw_file_hash.startswith("sha256:"))
        self.assertTrue(
            (self.settings.disclosure_data_root / "data" / result.raw_file_relpath).is_file()
        )

        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT raw_file_relpath, raw_file_hash, status "
                    "FROM disclosure_core.document WHERE document_id = :id"
                ),
                {"id": result.document_id},
            ).one()
            event_row = conn.execute(
                text(
                    "SELECT event_kind, change_kind, subject_kind, subject_ref "
                    "FROM disclosure_ops.outbox_event "
                    "WHERE document_id = :id"
                ),
                {"id": result.document_id},
            ).one()
        self.assertEqual(row.raw_file_relpath, result.raw_file_relpath)
        self.assertEqual(row.raw_file_hash, result.raw_file_hash)
        self.assertEqual(row.status, "registered")
        self.assertEqual(event_row.event_kind, "document_registered")
        self.assertEqual(event_row.change_kind, "materialized")
        self.assertEqual(event_row.subject_kind, "document")
        self.assertEqual(event_row.subject_ref, result.document_id)

    def test_duplicate_same_file_reuses_document(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)
        file_path = self._pdf("duplicate.pdf")

        first = self.use_case.execute(self._command(provider_document_id, file_path))
        second = self.use_case.execute(self._command(provider_document_id, file_path))

        self.assertTrue(second.reused_existing_document)
        self.assertEqual(second.document_id, first.document_id)
        with self.engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.document "
                    "WHERE provider = 'cninfo' AND provider_document_id = :pid"
                ),
                {"pid": provider_document_id},
            ).scalar_one()
        self.assertEqual(count, 1)

    def test_duplicate_import_records_observed_event_and_source_access(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)
        file_path = self._pdf("duplicate-observed.pdf")

        first = self.use_case.execute(self._command(provider_document_id, file_path))
        second = self.use_case.execute(self._command(provider_document_id, file_path))

        self.assertTrue(second.reused_existing_document)
        self.assertEqual(second.document_id, first.document_id)
        self.assertNotEqual(second.source_access_id, first.source_access_id)
        self.assertIsNotNone(second.outbox_event_id)
        with self.engine.connect() as conn:
            observed = conn.execute(
                text(
                    "SELECT event_kind, change_kind FROM disclosure_ops.outbox_event "
                    "WHERE event_id = :id"
                ),
                {"id": second.outbox_event_id},
            ).one()
            access_count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.source_access "
                    "WHERE provider = 'cninfo' "
                    "AND query_params ->> 'provider_document_id' = :pid"
                ),
                {"pid": provider_document_id},
            ).scalar_one()
        self.assertEqual(observed.event_kind, "document_observed")
        self.assertEqual(observed.change_kind, "observed")
        self.assertEqual(access_count, 2)

    def test_invalid_pdf_goes_to_quarantine_without_document(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)
        bad_file = self.root / "bad.pdf"
        bad_file.write_bytes(b"not pdf")

        result = self.use_case.execute(self._command(provider_document_id, bad_file))

        self.assertIsNone(result.document_id)
        self.assertIsNotNone(result.quarantined_path)
        self.assertTrue(result.quarantined_path.is_file())
        with self.engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.document "
                    "WHERE provider = 'cninfo' AND provider_document_id = :pid"
                ),
                {"pid": provider_document_id},
            ).scalar_one()
        self.assertEqual(count, 0)

    def test_quarantine_records_failed_source_access(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)
        bad_file = self.root / "quarantine-trace.pdf"
        bad_file.write_bytes(b"not pdf")

        result = self.use_case.execute(self._command(provider_document_id, bad_file))

        self.assertIsNone(result.document_id)
        self.assertIsNotNone(result.source_access_id)
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT status, error, query_params "
                    "FROM disclosure_core.source_access "
                    "WHERE source_access_id = :id"
                ),
                {"id": result.source_access_id},
            ).mappings().one()
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["error"])
        self.assertEqual(row["query_params"]["provider_document_id"], provider_document_id)

    def test_missing_pdf_goes_to_quarantine_without_document(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)
        missing_file = self.root / "missing.pdf"

        result = self.use_case.execute(self._command(provider_document_id, missing_file))

        self.assertIsNone(result.document_id)
        self.assertIsNotNone(result.quarantined_path)
        self.assertTrue(result.quarantined_path.is_file())
        self.assertEqual(result.quarantined_path.read_bytes(), b"")
        with self.engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.document "
                    "WHERE provider = 'cninfo' AND provider_document_id = :pid"
                ),
                {"pid": provider_document_id},
            ).scalar_one()
        self.assertEqual(count, 0)

    def test_existing_security_company_mismatch_fails_before_raw_write(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)
        command = self._command(provider_document_id, self._pdf("mismatch.pdf"))
        company_id = "co_" + new_ulid()
        security_id = "sec_" + new_ulid()
        self.extra_company_ids.append(company_id)
        self.extra_security_ids.append(security_id)

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company (company_id, legal_name) "
                    "VALUES (:company_id, :legal_name)"
                ),
                {"company_id": company_id, "legal_name": "Canonical Phase03 Co"},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.security "
                    "(security_id, company_id, security_code, exchange) "
                    "VALUES (:security_id, :company_id, :security_code, :exchange)"
                ),
                {
                    "security_id": security_id,
                    "company_id": company_id,
                    "security_code": command.security_code,
                    "exchange": command.exchange,
                },
            )

        with self.assertRaises(RegistrationMetadataError):
            self.use_case.execute(command)

        raw_root = self.settings.disclosure_data_root / "data" / "raw_documents"
        self.assertFalse(
            any(path.is_file() for path in raw_root.rglob("*"))
            if raw_root.exists()
            else False
        )
        with self.engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.document "
                    "WHERE provider = 'cninfo' AND provider_document_id = :pid"
                ),
                {"pid": provider_document_id},
            ).scalar_one()
        self.assertEqual(count, 0)

    def test_security_legal_name_mismatch_marks_identifier_contested(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)
        credit_code = "USCC" + new_ulid()
        company_id = "co_" + new_ulid()
        security_id = "sec_" + new_ulid()
        identifier_id = "ci_" + new_ulid()
        self.extra_company_ids.append(company_id)
        self.extra_security_ids.append(security_id)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company "
                    "(company_id, legal_name, unified_social_credit_code) "
                    "VALUES (:company_id, :legal_name, :credit_code)"
                ),
                {
                    "company_id": company_id,
                    "legal_name": "Canonical Security Co",
                    "credit_code": credit_code,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.security "
                    "(security_id, company_id, security_code, exchange) "
                    "VALUES (:security_id, :company_id, :security_code, :exchange)"
                ),
                {
                    "security_id": security_id,
                    "company_id": company_id,
                    "security_code": "SLM" + provider_document_id[-4:],
                    "exchange": "LOCAL",
                },
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company_identifier "
                    "(identifier_id, company_id, scheme, raw_value, normalized_value, "
                    "jurisdiction, status, observed_at) "
                    "VALUES (:identifier_id, :company_id, 'uscc', :raw_value, "
                    ":normalized_value, 'CN', 'active', now())"
                ),
                {
                    "identifier_id": identifier_id,
                    "company_id": company_id,
                    "raw_value": credit_code,
                    "normalized_value": credit_code,
                },
            )
        command = self._command(
            provider_document_id, self._pdf("security-legal-contested.pdf")
        )
        command = RegisterLocalPdfCommand(
            file_path=command.file_path,
            company_legal_name="Conflicting Security Co",
            security_code="SLM" + provider_document_id[-4:],
            exchange="LOCAL",
            filing_type=command.filing_type,
            title=command.title,
            announcement_date=command.announcement_date,
            provider_document_id=command.provider_document_id,
            provider=command.provider,
            report_period=command.report_period,
            company_credit_code=credit_code,
        )

        with self.assertRaises(SubjectIdentityConflictError):
            self.use_case.execute(command)

        with self.engine.connect() as conn:
            status = conn.execute(
                text(
                    "SELECT status FROM disclosure_core.company_identifier "
                    "WHERE identifier_id = :identifier_id"
                ),
                {"identifier_id": identifier_id},
            ).scalar_one()
            document_count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.document "
                    "WHERE provider = 'cninfo' AND provider_document_id = :pid"
                ),
                {"pid": provider_document_id},
            ).scalar_one()
        self.assertEqual(status, "contested")
        self.assertEqual(document_count, 0)

    def test_security_legal_name_mismatch_records_new_contested_identifier(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)
        credit_code = "USCC" + new_ulid()
        company_id = "co_" + new_ulid()
        security_id = "sec_" + new_ulid()
        self.extra_company_ids.append(company_id)
        self.extra_security_ids.append(security_id)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company "
                    "(company_id, legal_name) VALUES (:company_id, :legal_name)"
                ),
                {"company_id": company_id, "legal_name": "Canonical No USCC Co"},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.security "
                    "(security_id, company_id, security_code, exchange) "
                    "VALUES (:security_id, :company_id, :security_code, :exchange)"
                ),
                {
                    "security_id": security_id,
                    "company_id": company_id,
                    "security_code": "SNU" + provider_document_id[-4:],
                    "exchange": "LOCAL",
                },
            )
        command = self._command(
            provider_document_id, self._pdf("security-new-uscc-contested.pdf")
        )
        command = RegisterLocalPdfCommand(
            file_path=command.file_path,
            company_legal_name="Conflicting No USCC Co",
            security_code="SNU" + provider_document_id[-4:],
            exchange="LOCAL",
            filing_type=command.filing_type,
            title=command.title,
            announcement_date=command.announcement_date,
            provider_document_id=command.provider_document_id,
            provider=command.provider,
            report_period=command.report_period,
            company_credit_code=credit_code,
        )

        with self.assertRaises(SubjectIdentityConflictError):
            self.use_case.execute(command)

        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT status, raw_value, normalized_value "
                    "FROM disclosure_core.company_identifier "
                    "WHERE company_id = :company_id "
                    "AND normalized_value = :normalized_value"
                ),
                {"company_id": company_id, "normalized_value": credit_code},
            ).mappings().one()
            company_credit_code = conn.execute(
                text(
                    "SELECT unified_social_credit_code "
                    "FROM disclosure_core.company WHERE company_id = :company_id"
                ),
                {"company_id": company_id},
            ).scalar_one()
            document_count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.document "
                    "WHERE provider = 'cninfo' AND provider_document_id = :pid"
                ),
                {"pid": provider_document_id},
            ).scalar_one()
        self.assertEqual(row["status"], "contested")
        self.assertEqual(row["raw_value"], credit_code)
        self.assertIsNone(company_credit_code)
        self.assertEqual(document_count, 0)

    def test_identifier_legal_name_mismatch_marks_identifier_contested(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)
        credit_code = "USCC" + new_ulid()
        company_id = "co_" + new_ulid()
        identifier_id = "ci_" + new_ulid()
        self.extra_company_ids.append(company_id)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company "
                    "(company_id, legal_name, unified_social_credit_code) "
                    "VALUES (:company_id, :legal_name, :credit_code)"
                ),
                {
                    "company_id": company_id,
                    "legal_name": "Canonical Review Co",
                    "credit_code": credit_code,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company_identifier "
                    "(identifier_id, company_id, scheme, raw_value, normalized_value, "
                    "jurisdiction, status, observed_at) "
                    "VALUES (:identifier_id, :company_id, 'uscc', :raw_value, "
                    ":normalized_value, 'CN', 'active', now())"
                ),
                {
                    "identifier_id": identifier_id,
                    "company_id": company_id,
                    "raw_value": credit_code,
                    "normalized_value": credit_code,
                },
            )
        command = self._command(
            provider_document_id, self._pdf("identifier-contested.pdf")
        )
        command = RegisterLocalPdfCommand(
            file_path=command.file_path,
            company_legal_name="Conflicting Review Co",
            security_code=command.security_code,
            exchange=command.exchange,
            filing_type=command.filing_type,
            title=command.title,
            announcement_date=command.announcement_date,
            provider_document_id=command.provider_document_id,
            provider=command.provider,
            report_period=command.report_period,
            company_credit_code=credit_code,
        )

        with self.assertRaises(SubjectIdentityConflictError):
            self.use_case.execute(command)

        with self.engine.connect() as conn:
            status = conn.execute(
                text(
                    "SELECT status FROM disclosure_core.company_identifier "
                    "WHERE identifier_id = :identifier_id"
                ),
                {"identifier_id": identifier_id},
            ).scalar_one()
            document_count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.document "
                    "WHERE provider = 'cninfo' AND provider_document_id = :pid"
                ),
                {"pid": provider_document_id},
            ).scalar_one()
        self.assertEqual(status, "contested")
        self.assertEqual(document_count, 0)

    def test_security_uscc_owner_mismatch_marks_identifier_contested(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)
        credit_code = "USCC" + new_ulid()
        security_company_id = "co_" + new_ulid()
        identifier_company_id = "co_" + new_ulid()
        security_id = "sec_" + new_ulid()
        identifier_id = "ci_" + new_ulid()
        self.extra_company_ids.extend([security_company_id, identifier_company_id])
        self.extra_security_ids.append(security_id)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company (company_id, legal_name) "
                    "VALUES (:company_id, :legal_name)"
                ),
                {"company_id": security_company_id, "legal_name": "Shared Review Co"},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company (company_id, legal_name) "
                    "VALUES (:company_id, :legal_name)"
                ),
                {"company_id": identifier_company_id, "legal_name": "Shared Review Co"},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.security "
                    "(security_id, company_id, security_code, exchange) "
                    "VALUES (:security_id, :company_id, :security_code, :exchange)"
                ),
                {
                    "security_id": security_id,
                    "company_id": security_company_id,
                    "security_code": "RFX" + provider_document_id[-4:],
                    "exchange": "LOCAL",
                },
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company_identifier "
                    "(identifier_id, company_id, scheme, raw_value, normalized_value, "
                    "jurisdiction, status, observed_at) "
                    "VALUES (:identifier_id, :company_id, 'uscc', :raw_value, "
                    ":normalized_value, 'CN', 'active', now())"
                ),
                {
                    "identifier_id": identifier_id,
                    "company_id": identifier_company_id,
                    "raw_value": credit_code,
                    "normalized_value": credit_code,
                },
            )
        command = self._command(
            provider_document_id, self._pdf("identifier-owner-contested.pdf")
        )
        command = RegisterLocalPdfCommand(
            file_path=command.file_path,
            company_legal_name="Shared Review Co",
            security_code="RFX" + provider_document_id[-4:],
            exchange="LOCAL",
            filing_type=command.filing_type,
            title=command.title,
            announcement_date=command.announcement_date,
            provider_document_id=command.provider_document_id,
            provider=command.provider,
            report_period=command.report_period,
            company_credit_code=credit_code,
        )

        with self.assertRaises(SubjectIdentityConflictError):
            self.use_case.execute(command)

        with self.engine.connect() as conn:
            status = conn.execute(
                text(
                    "SELECT status FROM disclosure_core.company_identifier "
                    "WHERE identifier_id = :identifier_id"
                ),
                {"identifier_id": identifier_id},
            ).scalar_one()
            document_count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.document "
                    "WHERE provider = 'cninfo' AND provider_document_id = :pid"
                ),
                {"pid": provider_document_id},
            ).scalar_one()
        self.assertEqual(status, "contested")
        self.assertEqual(document_count, 0)

    def test_company_uscc_conflict_records_contested_identifier(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)
        old_credit_code = "OLD" + new_ulid()
        new_credit_code = "NEW" + new_ulid()
        company_id = "co_" + new_ulid()
        security_id = "sec_" + new_ulid()
        self.extra_company_ids.append(company_id)
        self.extra_security_ids.append(security_id)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company "
                    "(company_id, legal_name, unified_social_credit_code) "
                    "VALUES (:company_id, :legal_name, :credit_code)"
                ),
                {
                    "company_id": company_id,
                    "legal_name": "USCC Conflict Review Co",
                    "credit_code": old_credit_code,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.security "
                    "(security_id, company_id, security_code, exchange) "
                    "VALUES (:security_id, :company_id, :security_code, :exchange)"
                ),
                {
                    "security_id": security_id,
                    "company_id": company_id,
                    "security_code": "RCF" + provider_document_id[-4:],
                    "exchange": "LOCAL",
                },
            )
        command = self._command(
            provider_document_id, self._pdf("company-uscc-contested.pdf")
        )
        command = RegisterLocalPdfCommand(
            file_path=command.file_path,
            company_legal_name="USCC Conflict Review Co",
            security_code="RCF" + provider_document_id[-4:],
            exchange="LOCAL",
            filing_type=command.filing_type,
            title=command.title,
            announcement_date=command.announcement_date,
            provider_document_id=command.provider_document_id,
            provider=command.provider,
            report_period=command.report_period,
            company_credit_code=new_credit_code,
        )

        with self.assertRaises(SubjectIdentityConflictError):
            self.use_case.execute(command)

        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT status, raw_value, normalized_value "
                    "FROM disclosure_core.company_identifier "
                    "WHERE company_id = :company_id "
                    "AND normalized_value = :normalized_value"
                ),
                {"company_id": company_id, "normalized_value": new_credit_code},
            ).mappings().one()
            document_count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.document "
                    "WHERE provider = 'cninfo' AND provider_document_id = :pid"
                ),
                {"pid": provider_document_id},
            ).scalar_one()
        self.assertEqual(row["status"], "contested")
        self.assertEqual(row["raw_value"], new_credit_code)
        self.assertEqual(document_count, 0)

    def test_doctor_detects_raw_hash_mismatch(self) -> None:
        provider_document_id = "local-" + new_ulid()
        self.provider_document_ids.append(provider_document_id)
        result = self.use_case.execute(
            self._command(provider_document_id, self._pdf("doctor.pdf"))
        )

        raw_path = self.settings.disclosure_data_root / "data" / result.raw_file_relpath
        raw_path.write_bytes(b"%PDF-1.4\nchanged\n%%EOF\n")

        checks = run_raw_archive_checks(self.settings, self.engine)
        matching = [
            check
            for check in checks
            if result.document_id in check.message and check.name == "raw hash"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].status, "FAIL")
        self.assertIn("raw hash mismatch", matching[0].message)


if __name__ == "__main__":
    unittest.main()
