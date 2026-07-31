"""DB-gated contract tests for corpus write admission."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import unittest
from unittest.mock import MagicMock

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from disclosure_anchor.application.worker.locks import (
    CORPUS_WRITE_NS,
    DOC_NS,
    DOC_PRODUCER_NS,
    CorpusWriteBusyError,
    exclusive_corpus_mutation,
    exclusive_document_producer,
    shared_corpus_writer,
    stable_document_hash,
)
from disclosure_anchor.application.use_cases.build_search_projection import (
    BuildSearchProjection,
    BuildSearchProjectionCommand,
)
from disclosure_anchor.application.use_cases.register_local_pdf import (
    RegisterLocalPdf,
    RegisterLocalPdfCommand,
)
from tests.integration._support import engine_or_skip


class CorpusWriteAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_exclusive_maintenance_and_shared_producers_conflict_both_ways(
        self,
    ) -> None:
        with self.engine.connect() as maintenance, self.engine.connect() as producer:
            self.assertTrue(
                maintenance.execute(
                    text("SELECT pg_try_advisory_lock(:ns, 0)"),
                    {"ns": CORPUS_WRITE_NS},
                ).scalar_one()
            )
            try:
                self.assertFalse(
                    producer.execute(
                        text(
                            "SELECT pg_try_advisory_lock_shared(:ns, 0)"
                        ),
                        {"ns": CORPUS_WRITE_NS},
                    ).scalar_one()
                )
            finally:
                maintenance.execute(
                    text("SELECT pg_advisory_unlock(:ns, 0)"),
                    {"ns": CORPUS_WRITE_NS},
                )

            try:
                self.assertTrue(
                    producer.execute(
                        text(
                            "SELECT pg_try_advisory_lock_shared(:ns, 0)"
                        ),
                        {"ns": CORPUS_WRITE_NS},
                    ).scalar_one()
                )
                self.assertFalse(
                    maintenance.execute(
                        text("SELECT pg_try_advisory_lock(:ns, 0)"),
                        {"ns": CORPUS_WRITE_NS},
                    ).scalar_one()
                )
            finally:
                producer.execute(
                    text("SELECT pg_advisory_unlock_shared(:ns, 0)"),
                    {"ns": CORPUS_WRITE_NS},
                )

    def test_uow_and_lifecycle_helper_hold_one_session_lease_across_commits(
        self,
    ) -> None:
        def factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(engine=self.engine)

        with self.engine.connect() as maintenance:
            with shared_corpus_writer(factory):
                self.assertFalse(
                    maintenance.execute(
                        text("SELECT pg_try_advisory_lock(:ns, 0)"),
                        {"ns": CORPUS_WRITE_NS},
                    ).scalar_one()
                )
                # PostgreSQL deliberately separates bigint and (int,int)
                # advisory-key spaces. This negative control prevents a future
                # refactor from making only one side look numerically equal.
                self.assertTrue(
                    maintenance.execute(
                        text(
                            "SELECT pg_try_advisory_lock("
                            "CAST(:ns AS bigint))"
                        ),
                        {"ns": CORPUS_WRITE_NS},
                    ).scalar_one()
                )
                maintenance.execute(
                    text(
                        "SELECT pg_advisory_unlock(CAST(:ns AS bigint))"
                    ),
                    {"ns": CORPUS_WRITE_NS},
                )

            with SqlAlchemyUnitOfWork(engine=self.engine) as producer:
                producer.commit()
                self.assertFalse(
                    maintenance.execute(
                        text("SELECT pg_try_advisory_lock(:ns, 0)"),
                        {"ns": CORPUS_WRITE_NS},
                    ).scalar_one()
                )
            self.assertTrue(
                maintenance.execute(
                    text("SELECT pg_try_advisory_lock(:ns, 0)"),
                    {"ns": CORPUS_WRITE_NS},
                ).scalar_one()
            )
            maintenance.execute(
                text("SELECT pg_advisory_unlock(:ns, 0)"),
                {"ns": CORPUS_WRITE_NS},
            )

    def test_helpers_fail_closed_in_both_directions(self) -> None:
        def factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(engine=self.engine)

        with exclusive_corpus_mutation(self.engine):
            with self.assertRaises(CorpusWriteBusyError):
                with factory():
                    self.fail("UoW entered while exclusive maintenance held")
        with factory():
            with self.assertRaises(CorpusWriteBusyError):
                with exclusive_corpus_mutation(self.engine):
                    self.fail("maintenance entered while a UoW was active")

    def test_exclusive_gate_stops_raw_archive_before_first_file_write(self) -> None:
        raw_store = MagicMock()
        use_case = RegisterLocalPdf(
            raw_store=raw_store,
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=self.engine),
        )
        command = RegisterLocalPdfCommand(
            file_path=Path("/does/not/matter.pdf"),
            company_legal_name="Corpus Gate Test",
            security_code="TLOCK1",
            exchange="LOCAL",
            filing_type="annual_report",
            title="Corpus gate",
            announcement_date=date(2026, 7, 27),
            report_period="2025A",
            provider_document_id="1",
            provider="cninfo",
        )
        with exclusive_corpus_mutation(self.engine):
            with self.assertRaises(CorpusWriteBusyError):
                use_case.execute(command)
        raw_store.put_raw_document.assert_not_called()
        raw_store.quarantine_raw_document.assert_not_called()

    def test_direct_search_projection_joins_the_same_gate(self) -> None:
        use_case = BuildSearchProjection(engine=self.engine)
        with exclusive_corpus_mutation(self.engine):
            with self.assertRaises(CorpusWriteBusyError):
                use_case.execute(BuildSearchProjectionCommand(full=False))

    def test_document_producer_lease_serializes_without_self_deadlock(
        self,
    ) -> None:
        def factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(engine=self.engine)

        document_id = "doc_lock_contract"
        document_hash = stable_document_hash(document_id)
        with self.engine.connect() as contender:
            with exclusive_document_producer(factory, document_id):
                self.assertFalse(
                    contender.execute(
                        text("SELECT pg_try_advisory_lock(:ns, :h)"),
                        {"ns": DOC_PRODUCER_NS, "h": document_hash},
                    ).scalar_one()
                )
                contender.rollback()
                transaction = contender.begin()
                try:
                    self.assertTrue(
                        contender.execute(
                            text("SELECT pg_try_advisory_xact_lock(:ns, :h)"),
                            {"ns": DOC_NS, "h": document_hash},
                        ).scalar_one()
                    )
                finally:
                    transaction.rollback()


if __name__ == "__main__":
    unittest.main()
