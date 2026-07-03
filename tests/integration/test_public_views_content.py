"""Public views return committed data with the expected projection."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from tests.integration._support import engine_or_skip


class PublicViewContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.company_id = ids.new_company_id()
        self.security_id = ids.new_security_id()
        self.security_code = f"T{self.security_id[-6:]}"
        self.source_access_id = ids.new_source_access_id()
        self.document_id = ids.new_document_id()
        self.run_id = ids.new_processing_run_id()
        self.unit_id = ids.new_asset_id()
        self.event_id = ids.new_outbox_event_id()
        self.observed_event_id = ids.new_outbox_event_id()

    def tearDown(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM disclosure_ops.outbox_event "
                    "WHERE event_id IN (:event_id, :observed_event_id)"
                ),
                {
                    "event_id": self.event_id,
                    "observed_event_id": self.observed_event_id,
                },
            )
            conn.execute(
                text("DELETE FROM disclosure_core.document_unit WHERE asset_id = :v"),
                {"v": self.unit_id},
            )
            conn.execute(
                text("DELETE FROM disclosure_core.processing_run WHERE processing_run_id = :v"),
                {"v": self.run_id},
            )
            conn.execute(
                text("DELETE FROM disclosure_core.document WHERE document_id = :v"),
                {"v": self.document_id},
            )
            conn.execute(
                text("DELETE FROM disclosure_core.source_access WHERE source_access_id = :v"),
                {"v": self.source_access_id},
            )
            conn.execute(
                text("DELETE FROM disclosure_core.security WHERE security_id = :v"),
                {"v": self.security_id},
            )
            conn.execute(
                text("DELETE FROM disclosure_core.company WHERE company_id = :v"),
                {"v": self.company_id},
            )
        self.engine.dispose()

    def _seed(self) -> None:
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.companies.add(
                e.Company(company_id=self.company_id, legal_name="江海股份")
            )
            uow.securities.add(
                e.Security(
                    security_id=self.security_id,
                    company_id=self.company_id,
                    security_code=self.security_code,
                    exchange="SZSE",
                )
            )
            uow.source_accesses.add(
                e.SourceAccess(
                    source_access_id=self.source_access_id,
                    provider="cninfo",
                    provider_interface="local_pdf",
                    accessed_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
                    status="succeeded",
                    company_id=self.company_id,
                    security_id=self.security_id,
                )
            )
            uow.documents.add(
                e.Document(
                    document_id=self.document_id,
                    status="published",
                    company_id=self.company_id,
                    security_id=self.security_id,
                    source_access_id=self.source_access_id,
                    provider="cninfo",
                    provider_document_id="1225087169",
                    filing_type="annual_report",
                    report_period="2025A",
                    raw_file_hash="sha256:abc",
                    raw_file_relpath=(
                        "raw_documents/cninfo/002484/2025/1225087169/"
                        "sha256_abcdef.pdf"
                    ),
                )
            )
            uow.processing_runs.add(
                e.ProcessingRun(
                    processing_run_id=self.run_id,
                    document_id=self.document_id,
                    run_kind="full",
                    status="succeeded",
                    is_active=True,
                )
            )
            uow.document_units.add(
                e.DocumentUnit(
                    asset_id=self.unit_id,
                    document_id=self.document_id,
                    processing_run_id=self.run_id,
                    payload_kind="table",
                    order_index=0,
                    heading_path=["第八节 财务报告", "应收账款"],
                    semantic_key="receivable_aging",
                    payload={"unit": "元", "rows": [["合计", "1"]]},
                    content_hash="sha256:unit",
                )
            )
            uow.outbox.add(
                e.OutboxEvent(
                    event_id=self.event_id,
                    event_kind="document_registered",
                    document_id=self.document_id,
                    payload={"change_kind": "materialized"},
                )
            )
            uow.outbox.add(
                e.OutboxEvent(
                    event_id=self.observed_event_id,
                    event_kind="document_observed",
                    document_id=self.document_id,
                )
            )
            uow.commit()

    def test_document_units_and_source_refs_views(self) -> None:
        self._seed()
        with self.engine.connect() as conn:
            unit_row = conn.execute(
                text(
                    "SELECT payload_kind, contract_version, company_ref, "
                    "security_ref, security_code, filing_type, report_period, "
                    "source_ref, producer_action_ref, parent_ref, semantic_key, payload "
                    "FROM disclosure_public.document_units_v1 "
                    "WHERE asset_id = :v"
                ),
                {"v": self.unit_id},
            ).mappings().one()
            self.assertEqual(unit_row["payload_kind"], "table")
            self.assertEqual(unit_row["contract_version"], "document_unit.v1")
            self.assertEqual(unit_row["company_ref"], self.company_id)
            self.assertEqual(unit_row["security_ref"], self.security_id)
            self.assertEqual(unit_row["security_code"], self.security_code)
            self.assertEqual(unit_row["filing_type"], "annual_report")
            self.assertEqual(unit_row["report_period"], "2025A")
            self.assertEqual(unit_row["source_ref"], self.source_access_id)
            self.assertEqual(unit_row["producer_action_ref"], self.run_id)
            self.assertEqual(unit_row["parent_ref"], self.document_id)
            self.assertEqual(unit_row["semantic_key"], "receivable_aging")
            self.assertEqual(unit_row["payload"], {"unit": "元", "rows": [["合计", "1"]]})

            ref_row = conn.execute(
                text(
                    "SELECT service, contract_version, provider, provider_document_id, raw_file_hash, "
                    "unit_content_hash FROM disclosure_public.source_refs_v1 "
                    "WHERE asset_id = :v"
                ),
                {"v": self.unit_id},
            ).mappings().one()
            self.assertEqual(ref_row["service"], "disclosure_anchor")
            self.assertEqual(ref_row["contract_version"], "source_ref.v1")
            self.assertEqual(ref_row["provider"], "cninfo")
            self.assertEqual(ref_row["provider_document_id"], "1225087169")
            self.assertEqual(ref_row["raw_file_hash"], "sha256:abc")
            self.assertEqual(ref_row["unit_content_hash"], "sha256:unit")

            doc_row = conn.execute(
                text(
                    "SELECT status, raw_file_hash FROM disclosure_public.documents_v1 "
                    "WHERE document_id = :v"
                ),
                {"v": self.document_id},
            ).mappings().one()
            self.assertEqual(doc_row["status"], "published")
            # raw_file_relpath must not be a column in the view.
            self.assertNotIn("raw_file_relpath", doc_row)

            change_rows = conn.execute(
                text(
                    "SELECT event_id, event_kind, change_kind "
                    "FROM disclosure_public.change_events_v1 "
                    "WHERE event_id IN (:event_id, :observed_event_id)"
                ),
                {
                    "event_id": self.event_id,
                    "observed_event_id": self.observed_event_id,
                },
            ).mappings().all()
            change_by_id = {row["event_id"]: row for row in change_rows}
            self.assertEqual(
                change_by_id[self.event_id]["event_kind"], "document_registered"
            )
            self.assertEqual(change_by_id[self.event_id]["change_kind"], "materialized")
            self.assertEqual(
                change_by_id[self.observed_event_id]["event_kind"], "document_observed"
            )
            self.assertEqual(change_by_id[self.observed_event_id]["change_kind"], "observed")


if __name__ == "__main__":
    unittest.main()
