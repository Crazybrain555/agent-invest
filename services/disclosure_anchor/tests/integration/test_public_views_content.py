"""Public views return committed data with the expected projection."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from tests.integration._support import engine_or_skip


class PublicViewContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        # Dedup-key values are run-unique so crash residue from a killed test
        # process can never collide with a later run (uq_document_provider_doc_hash).
        self.pid = f"pvc{ids.new_ulid()[-10:].lower()}"
        self.hash_a = f"sha256:abc{ids.new_ulid()[-8:].lower()}"
        self.hash_b = f"sha256:def{ids.new_ulid()[-8:].lower()}"
        self.company_id = ids.new_company_id()
        self.security_id = ids.new_security_id()
        self.security_code = f"T{self.security_id[-6:]}"
        self.source_access_id = ids.new_source_access_id()
        self.document_id = ids.new_document_id()
        self.run_id = ids.new_processing_run_id()
        self.unit_id = ids.new_asset_id()
        self.event_id = ids.new_outbox_event_id()
        self.observed_event_id = ids.new_outbox_event_id()
        self.counterexample_event_id = ids.new_outbox_event_id()
        self.superseding_document_id = ids.new_document_id()
        self.extra_document_ids: list[str] = []
        self.extra_run_ids: list[str] = []
        self.extra_unit_ids: list[str] = []
        self.extra_event_ids: list[str] = []

    def tearDown(self) -> None:
        with self.engine.begin() as conn:
            if self.extra_event_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_ops.outbox_event "
                        "WHERE event_id = ANY(:ids)"
                    ),
                    {"ids": self.extra_event_ids},
                )
            conn.execute(
                text(
                    "DELETE FROM disclosure_ops.outbox_event "
                    "WHERE event_id IN "
                    "(:event_id, :observed_event_id, :counterexample_event_id)"
                ),
                {
                    "event_id": self.event_id,
                    "observed_event_id": self.observed_event_id,
                    "counterexample_event_id": self.counterexample_event_id,
                },
            )
            if self.extra_unit_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.document_unit "
                        "WHERE asset_id = ANY(:ids)"
                    ),
                    {"ids": self.extra_unit_ids},
                )
            if self.extra_run_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.processing_run "
                        "WHERE processing_run_id = ANY(:ids)"
                    ),
                    {"ids": self.extra_run_ids},
                )
            if self.extra_document_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.document "
                        "WHERE document_id = ANY(:ids)"
                    ),
                    {"ids": self.extra_document_ids},
                )
            conn.execute(
                text("DELETE FROM disclosure_core.document WHERE document_id = :v"),
                {"v": self.superseding_document_id},
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

    def _seed_extra_document_unit(self, filing_type: str) -> tuple[str, str]:
        document_id = ids.new_document_id()
        run_id = ids.new_processing_run_id()
        unit_id = ids.new_asset_id()
        provider_document_id = f"pid-{document_id}"
        self.extra_document_ids.append(document_id)
        self.extra_run_ids.append(run_id)
        self.extra_unit_ids.append(unit_id)

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document "
                    "(document_id, status, company_id, security_id, source_access_id, "
                    "provider, provider_document_id, filing_type, report_period, "
                    "raw_file_hash, raw_file_relpath) "
                    "VALUES (:document_id, 'published', :company_id, :security_id, "
                    ":source_access_id, 'cninfo', :provider_document_id, :filing_type, "
                    "NULL, :raw_file_hash, :raw_file_relpath)"
                ),
                {
                    "document_id": document_id,
                    "company_id": self.company_id,
                    "security_id": self.security_id,
                    "source_access_id": self.source_access_id,
                    "provider_document_id": provider_document_id,
                    "filing_type": filing_type,
                    "raw_file_hash": f"sha256:{document_id}",
                    "raw_file_relpath": f"raw_documents/cninfo/{document_id}.pdf",
                },
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.processing_run "
                    "(processing_run_id, document_id, run_kind, status) "
                    "VALUES (:run_id, :document_id, 'full', 'succeeded')"
                ),
                {"run_id": run_id, "document_id": document_id},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document_unit "
                    "(asset_id, document_id, processing_run_id, payload_kind, "
                    "order_index, payload, content_hash) "
                    "VALUES (:unit_id, :document_id, :run_id, 'text', 0, "
                    "'{}'::jsonb, :content_hash)"
                ),
                {
                    "unit_id": unit_id,
                    "document_id": document_id,
                    "run_id": run_id,
                    "content_hash": f"sha256:{unit_id}",
                },
            )
        return document_id, unit_id

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
                    provider_document_id=self.pid,
                    filing_type="annual_report",
                    report_period="2025A",
                    raw_file_hash=self.hash_a,
                    raw_file_relpath=(
                        f"raw_documents/cninfo/002484/2025/{self.pid}/"
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
                    query_projection_hash="sha256:query",
                )
            )
            uow.outbox.add(
                e.OutboxEvent(
                    event_id=self.event_id,
                    event_kind="document_registered",
                    change_kind="materialized",
                    subject_kind="document",
                    subject_ref=self.document_id,
                    document_id=self.document_id,
                    payload={"change_kind": "materialized"},
                )
            )
            uow.outbox.add(
                e.OutboxEvent(
                    event_id=self.observed_event_id,
                    event_kind="document_observed",
                    change_kind="observed",
                    subject_kind="document",
                    subject_ref=self.document_id,
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
                    "source_ref, producer_action_ref, parent_ref, semantic_key, payload, "
                    "asset_kind, source_tier, trace_level, raw_file_hash, query_projection_hash "
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
            self.assertEqual(unit_row["asset_kind"], "document_unit")
            self.assertEqual(unit_row["source_tier"], "tier_0a")
            self.assertEqual(unit_row["trace_level"], "G0")
            self.assertEqual(unit_row["raw_file_hash"], self.hash_a)
            self.assertEqual(unit_row["query_projection_hash"], "sha256:query")

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
            self.assertEqual(ref_row["provider_document_id"], self.pid)
            self.assertEqual(ref_row["raw_file_hash"], self.hash_a)
            self.assertEqual(ref_row["unit_content_hash"], "sha256:unit")

            doc_row = conn.execute(
                text(
                    "SELECT status, raw_file_hash, contract_version, company_ref, "
                    "security_ref, source_ref, provider_metadata "
                    "FROM disclosure_public.documents_v1 "
                    "WHERE document_id = :v"
                ),
                {"v": self.document_id},
            ).mappings().one()
            self.assertEqual(doc_row["status"], "published")
            self.assertEqual(doc_row["contract_version"], "document.v1")
            self.assertEqual(doc_row["company_ref"], self.company_id)
            self.assertEqual(doc_row["security_ref"], self.security_id)
            self.assertEqual(doc_row["source_ref"], self.source_access_id)
            self.assertEqual(doc_row["provider_metadata"], {})
            # raw_file_relpath must not be a column in the view.
            self.assertNotIn("raw_file_relpath", doc_row)

            change_rows = conn.execute(
                text(
                    "SELECT event_id, event_kind, change_kind, subject_kind, subject_ref, "
                    "source, contract_version "
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
            self.assertEqual(change_by_id[self.event_id]["subject_kind"], "document")
            self.assertEqual(change_by_id[self.event_id]["subject_ref"], self.document_id)
            self.assertEqual(change_by_id[self.event_id]["source"], "disclosure_anchor")
            self.assertEqual(change_by_id[self.event_id]["contract_version"], "change_event.v1")
            self.assertEqual(
                change_by_id[self.observed_event_id]["event_kind"], "document_observed"
            )
            self.assertEqual(change_by_id[self.observed_event_id]["change_kind"], "observed")

    def test_document_units_view_column_contract(self) -> None:
        expected = {
            "asset_id",
            "document_id",
            "processing_run_id",
            "is_active_run",
            "provider_document_id",
            "payload_kind",
            "heading_path",
            "title",
            "order_index",
            "semantic_key",
            "payload",
            "content_hash",
            "structure_hash",
            "quality_status",
            "applicability",
            "page_no",
            "artifact_locator",
            "created_at",
            "contract_version",
            "company_ref",
            "security_ref",
            "security_code",
            "exchange",
            "filing_type",
            "report_period",
            "announcement_date",
            "producer_action_ref",
            "source_ref",
            "parent_ref",
            "asset_kind",
            "observed_at",
            "source_tier",
            "trace_level",
            "raw_file_hash",
            "query_projection_hash",
        }
        with self.engine.connect() as conn:
            columns = {
                row.column_name
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_public' "
                        "AND table_name = 'document_units_v1'"
                    )
                )
            }

        self.assertEqual(columns, expected)
        self.assertEqual(len(columns), 35)

    def test_source_tier_mapping_contract(self) -> None:
        self._seed()
        _, ir_unit_id = self._seed_extra_document_unit("investor_relations")
        _, briefing_unit_id = self._seed_extra_document_unit("performance_briefing")

        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT asset_id, filing_type, source_tier "
                    "FROM disclosure_public.document_units_v1 "
                    "WHERE asset_id = ANY(:ids)"
                ),
                {"ids": [self.unit_id, ir_unit_id, briefing_unit_id]},
            ).mappings().all()

        tier_by_unit = {row["asset_id"]: row["source_tier"] for row in rows}
        self.assertEqual(tier_by_unit[self.unit_id], "tier_0a")
        self.assertEqual(tier_by_unit[ir_unit_id], "tier_0b")
        self.assertEqual(tier_by_unit[briefing_unit_id], "tier_0b")

    def test_documents_view_derives_superseded_by_latest_document(self) -> None:
        self._seed()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document "
                    "(document_id, status, company_id, security_id, source_access_id, "
                    "provider, provider_document_id, filing_type, report_period, "
                    "raw_file_hash, raw_file_relpath, supersedes_document_id) "
                    "VALUES (:document_id, 'registered', :company_id, :security_id, "
                    ":source_access_id, 'cninfo', :pid, 'annual_report', "
                    "'2025A', :hash_b, "
                    ":raw_relpath, "
                    ":supersedes_document_id)"
                ),
                {
                    "document_id": self.superseding_document_id,
                    "company_id": self.company_id,
                    "security_id": self.security_id,
                    "source_access_id": self.source_access_id,
                    "supersedes_document_id": self.document_id,
                    "pid": self.pid,
                    "hash_b": self.hash_b,
                    "raw_relpath": f"raw_documents/cninfo/002484/2025/{self.pid}/sha256_def.pdf",
                },
            )

        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT superseded_by_document_id "
                    "FROM disclosure_public.documents_v1 "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": self.document_id},
            ).mappings().one()

        self.assertEqual(
            row["superseded_by_document_id"], self.superseding_document_id
        )

    def test_change_kind_is_required_and_not_inferred_from_event_name(self) -> None:
        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                with self.assertRaises(IntegrityError):
                    conn.execute(
                        text(
                            "INSERT INTO disclosure_ops.outbox_event "
                            "(event_id, event_kind, subject_kind, subject_ref) "
                            "VALUES (:event_id, 'document_observed_missing_kind', "
                            "'document', 'doc_missing_kind')"
                        ),
                        {"event_id": ids.new_outbox_event_id()},
                    )
            finally:
                trans.rollback()

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_ops.outbox_event "
                    "(event_id, event_kind, change_kind, subject_kind, subject_ref) "
                    "VALUES (:event_id, 'document_observed_counterexample', "
                    "'materialized', 'document', 'doc_observed_named')"
                ),
                {"event_id": self.counterexample_event_id},
            )

        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT event_kind, change_kind "
                    "FROM disclosure_public.change_events_v1 "
                    "WHERE event_id = :event_id"
                ),
                {"event_id": self.counterexample_event_id},
            ).mappings().one()

        self.assertEqual(row["event_kind"], "document_observed_counterexample")
        self.assertEqual(row["change_kind"], "materialized")


if __name__ == "__main__":
    unittest.main()
