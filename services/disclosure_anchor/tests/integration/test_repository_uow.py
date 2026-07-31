"""Repository and UnitOfWork integration tests."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from tests.integration._support import engine_or_skip


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RepositoryUnitOfWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        # Run-unique strong keys: crash residue must never collide with the
        # partial unique index on (scheme, normalized_value) or company uscc.
        self.uscc = f"913206{ids.new_ulid()[-12:].upper()}"
        self.pid = f"uow{ids.new_ulid()[-10:].lower()}"
        self.security_code = f"T{ids.new_ulid()[-6:]}"

    def tearDown(self) -> None:
        self.engine.dispose()

    def _delete(self, created: dict[str, str]) -> None:
        """Best-effort cleanup of rows a committing test created."""
        order = [
            ("disclosure_core.document_unit", "asset_id", "unit"),
            ("disclosure_ops.outbox_event", "event_id", "event"),
            ("disclosure_core.processing_run", "processing_run_id", "run"),
            ("disclosure_core.document", "document_id", "document"),
            ("disclosure_core.source_access", "source_access_id", "source_access"),
            ("disclosure_core.security", "security_id", "security"),
            ("disclosure_core.company_identifier", "identifier_id", "identifier"),
            ("disclosure_core.company", "company_id", "company"),
        ]
        with self.engine.begin() as conn:
            for table, column, key in order:
                if key in created:
                    conn.execute(
                        text(f"DELETE FROM {table} WHERE {column} = :v"),
                        {"v": created[key]},
                    )

    def test_create_all_entities_and_commit(self) -> None:
        created: dict[str, str] = {}
        try:
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                company = uow.companies.add(
                    e.Company(
                        company_id=ids.new_company_id(),
                        legal_name="江海股份",
                        unified_social_credit_code=self.uscc,
                    )
                )
                created["company"] = company.company_id
                identifier = uow.company_identifiers.add(
                    e.CompanyIdentifier(
                        identifier_id=ids.new_company_identifier_id(),
                        company_id=company.company_id,
                        scheme="uscc",
                        raw_value=self.uscc,
                        normalized_value=self.uscc,
                        jurisdiction="CN",
                        observed_at=_now(),
                    )
                )
                created["identifier"] = identifier.identifier_id

                security = uow.securities.add(
                    e.Security(
                        security_id=ids.new_security_id(),
                        company_id=company.company_id,
                        security_code=self.security_code,
                        exchange="LOCAL",
                    )
                )
                created["security"] = security.security_id

                source_access = uow.source_accesses.add(
                    e.SourceAccess(
                        source_access_id=ids.new_source_access_id(),
                        provider="cninfo",
                        accessed_at=_now(),
                        status="ok",
                        provider_interface="cninfo:p_info3015",
                    )
                )
                created["source_access"] = source_access.source_access_id

                document = uow.documents.add(
                    e.Document(
                        document_id=ids.new_document_id(),
                        status="registered",
                        company_id=company.company_id,
                        security_id=security.security_id,
                        source_access_id=source_access.source_access_id,
                        provider="cninfo",
                        provider_document_id=self.pid,
                        title="2025 年年度报告",
                        report_period="2025A",
                        raw_file_relpath=(
                            f"raw_documents/cninfo/{self.security_code}/2025/{self.pid}/"
                            "sha256_7c73.pdf"
                        ),
                        raw_file_hash="sha256:7c73103aa3c9",
                    )
                )
                created["document"] = document.document_id
                self.assertEqual(document.class_filing_type, "annual_report")
                self.assertIsNotNone(document.class_rules_version)

                run_id = ids.new_processing_run_id()
                run = uow.processing_runs.add(
                    e.ProcessingRun(
                        processing_run_id=run_id,
                        document_id=document.document_id,
                        artifact_owner_processing_run_id=run_id,
                        run_kind="full",
                        status="succeeded",
                        is_active=True,
                        parser_name="mineru",
                        parser_version="3.4.0",
                        parser_backend="pipeline",
                        parser_method="auto",
                        parser_language="ch",
                        input_raw_file_hash="sha256:7c73103aa3c9",
                        parser_artifact_relpath=(
                            f"parser_artifacts/cninfo/{self.security_code}/{self.pid}/"
                            "run_01K0000000000000000000000"
                        ),
                        normalized_ir_relpath=(
                            f"derived/normalized_ir/cninfo/{self.security_code}/{self.pid}/"
                            "run_01K0000000000000000000000/normalized_ir.v2.json"
                        ),
                        builder_rules_version="ub-2026.07-1",
                        error={
                            "stage": "parse",
                            "error_code": "noop",
                            "retryable": False,
                        },
                    )
                )
                created["run"] = run.processing_run_id

                unit = uow.document_units.add(
                    e.DocumentUnit(
                        asset_id=ids.new_asset_id(),
                        document_id=document.document_id,
                        processing_run_id=run.processing_run_id,
                        payload_kind="table",
                        order_index=0,
                        heading_path=["第八节 财务报告", "应收账款", "按账龄披露"],
                        title="应收账款按账龄披露",
                        semantic_key="receivable_aging",
                        payload={"unit": "元", "headers": ["账龄"], "rows": [["合计"]]},
                        content_hash="sha256:unit",
                        query_projection_hash="sha256:query",
                        artifact_locator={
                            "artifact_kind": "normalized_ir",
                            "order_index": 312,
                        },
                    )
                )
                created["unit"] = unit.asset_id

                event = uow.outbox.add(
                    e.OutboxEvent(
                        event_id=ids.new_outbox_event_id(),
                        event_kind="run_published",
                        change_kind="materialized",
                        subject_kind="processing_run",
                        subject_ref=run.processing_run_id,
                        document_id=document.document_id,
                        processing_run_id=run.processing_run_id,
                    )
                )
                created["event"] = event.event_id
                self.assertIsNotNone(event.seq)

                uow.commit()

            # Read back in a fresh UnitOfWork to confirm the commit persisted.
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                self.assertEqual(
                    uow.companies.get(created["company"]).legal_name, "江海股份"
                )
                self.assertEqual(
                    uow.documents.get(created["document"]).report_period, "2025A"
                )
                loaded_document = uow.documents.get(created["document"])
                self.assertIsNotNone(loaded_document)
                assert loaded_document is not None
                self.assertEqual(loaded_document.class_filing_type, "annual_report")
                self.assertIsNotNone(loaded_document.class_rules_version)
                self.assertEqual(
                    uow.company_identifiers.get(created["identifier"]).scheme, "uscc"
                )
                self.assertEqual(
                    uow.company_identifiers.get_by_scheme_value(
                        "uscc", self.uscc
                    ).company_id,
                    created["company"],
                )
                got_unit = uow.document_units.get(created["unit"])
                got_run = uow.processing_runs.get(created["run"])
                self.assertEqual(got_run.parser_backend, "pipeline")
                self.assertEqual(got_run.parser_method, "auto")
                self.assertEqual(got_run.parser_language, "ch")
                self.assertEqual(got_run.input_raw_file_hash, "sha256:7c73103aa3c9")
                self.assertTrue(
                    got_run.parser_artifact_relpath.startswith("parser_artifacts/")
                )
                self.assertEqual(got_run.unit_build_status, "not_started")
                self.assertEqual(got_run.unit_build_attempt_count, 0)
                self.assertEqual(got_run.builder_rules_version, "ub-2026.07-1")
                self.assertEqual(got_run.error["error_code"], "noop")
                self.assertEqual(got_unit.semantic_key, "receivable_aging")
                self.assertEqual(got_unit.heading_path[0], "第八节 财务报告")
                self.assertEqual(got_unit.query_projection_hash, "sha256:query")
                self.assertEqual(
                    uow.outbox.get(created["event"]).event_kind, "run_published"
                )
        finally:
            self._delete(created)

    def test_rollback_discards_writes(self) -> None:
        company_id = ids.new_company_id()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.companies.add(
                e.Company(company_id=company_id, legal_name="rollback-me")
            )
            self.assertIsNotNone(uow.companies.get(company_id))
            uow.rollback()
            self.assertIsNone(uow.companies.get(company_id))

        # A fresh UnitOfWork must not see the rolled-back row.
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            self.assertIsNone(uow.companies.get(company_id))

    def test_context_exit_without_commit_rolls_back(self) -> None:
        company_id = ids.new_company_id()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.companies.add(e.Company(company_id=company_id, legal_name="no-commit"))
            # no commit
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            self.assertIsNone(uow.companies.get(company_id))

    def test_one_active_run_per_document(self) -> None:
        document_id = ids.new_document_id()
        first_run_id = ids.new_processing_run_id()
        second_run_id = ids.new_processing_run_id()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.documents.add(e.Document(document_id=document_id, status="registered"))
            uow.processing_runs.add(
                e.ProcessingRun(
                    processing_run_id=first_run_id,
                    document_id=document_id,
                    artifact_owner_processing_run_id=first_run_id,
                    run_kind="full",
                    status="succeeded",
                    is_active=True,
                )
            )
            with self.assertRaises(IntegrityError):
                uow.processing_runs.add(
                    e.ProcessingRun(
                        processing_run_id=second_run_id,
                        document_id=document_id,
                        artifact_owner_processing_run_id=second_run_id,
                        run_kind="full",
                        status="succeeded",
                        is_active=True,
                    )
                )
            uow.rollback()


if __name__ == "__main__":
    unittest.main()
