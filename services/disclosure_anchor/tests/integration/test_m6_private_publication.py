"""Independent M6 private evidence checks, only in the managed scratch database.

Fixture writes use the existing atomic-publication fixture. The reader has a
separate app session and the real runtime identity gate; no UoW or SQL shim.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Event
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from disclosure_anchor.adapters.db.postgres import connection as db_connection, models
from disclosure_anchor.adapters.db.postgres.atomic_document_publisher_v4 import (
    PostgresAtomicWholeDocumentPublisherV4,
)
from disclosure_anchor.adapters.db.postgres.m6_publish_verifier import PostgresM6PublishVerifier
from disclosure_anchor.adapters.db.postgres.schema import APP_ROLE
from disclosure_anchor.application.contracts.m6_run_events import M6AttemptAdmitted
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import AtomicPublicationUniqueConflict
from disclosure_anchor.application.ports.remote_parse_v4_repository import V4HeadNotFound
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import ConfigurationError
import tests.integration.test_atomic_document_publisher_v4 as atomic_fixture


class M6PrivatePublicationIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Its engine_or_skip verifies all URL aliases AND the managed DB marker
        # before any fixture writes. Never construct a database or fall back here.
        self.atomic = atomic_fixture.AtomicDocumentPublisherV4IntegrationTests()
        self.atomic.setUp()
        self.addCleanup(self.atomic.tearDown)
        self.engine, self.fixture = self.atomic.engine, self.atomic.fixture
        self.extra_documents = []
        self.extra_events = []
        self.addCleanup(self._clean_extra_rows)
        with self.engine.connect() as conn:
            scratch_name = conn.exec_driver_sql("SELECT current_database()").scalar_one()
        # Only the already marker-verified disposable DB name is substituted.
        # The actual session/current role and superuser checks remain untouched.
        self.database_name = patch.object(db_connection, "DATABASE_NAME", scratch_name)
        self.database_name.start()
        self.addCleanup(self.database_name.stop)
        self.app_engine = sa.create_engine(self.engine.url, poolclass=NullPool)
        self.addCleanup(self.app_engine.dispose)

        @sa.event.listens_for(self.app_engine, "connect")
        def app_session(dbapi_connection, _record):
            previous = dbapi_connection.autocommit
            dbapi_connection.autocommit = True
            try:
                with dbapi_connection.cursor() as cursor:
                    # Scratch-only session authorization, not SET ROLE: both
                    # session_user/current_user are the real non-superuser app.
                    cursor.execute("SET SESSION AUTHORIZATION " + APP_ROLE)
            finally:
                dbapi_connection.autocommit = previous

        self.sql, self.rollbacks, self.commits = [], [], []
        sa.event.listen(self.app_engine, "before_cursor_execute", self._observe_sql)
        sa.event.listen(self.app_engine, "rollback", lambda conn: self.rollbacks.append(True))
        sa.event.listen(self.app_engine, "commit", lambda conn: self.commits.append(True))
        self.receipts = []
        self.reader = PostgresM6PublishVerifier(engine=self.app_engine, receipt_sink=self.receipts.append)
        checkpoint = self.fixture.local_materialized
        self.admission = M6AttemptAdmitted(
            attempt_id=checkpoint.attempt_id, fence_identity=checkpoint.fence_identity,
            document_id=checkpoint.document_id, processing_run_id=checkpoint.processing_run_id,
            source_pdf_sha256=checkpoint.source_pdf_sha256, source_byte_count=checkpoint.source_byte_count,
            source_page_count=checkpoint.source_page_count,
            process_profile_sha256=checkpoint.process_profile_sha256,
        )

    def _observe_sql(self, conn, cursor, statement, parameters, context, executemany):
        self.sql.append(" ".join(statement.lower().split()))

    def _clean_extra_rows(self):
        with self.engine.begin() as conn:
            for event in self.extra_events:
                conn.execute(sa.text("DELETE FROM disclosure_ops.outbox_event WHERE event_id=:event"),
                             {"event": event})
            for document in self.extra_documents:
                for table in ("disclosure_ops.durable_publish_base", "disclosure_ops.outbox_event",
                              "disclosure_core.processing_run", "disclosure_core.document"):
                    conn.execute(sa.text(f"DELETE FROM {table} WHERE document_id=:document"),
                                 {"document": document})

    def _publish(self):
        return PostgresAtomicWholeDocumentPublisherV4(engine=self.engine).commit_whole_document(
            self.atomic.request, claim=self.atomic._claim(), stage_guard=self.atomic.stage_guard,
            artifacts_ready=self.atomic._ready(),
        )

    def _legacy_run(self, *, source, pages=None, active=False):
        document, run = str(ids.new_document_id()), str(ids.new_processing_run_id())
        self.extra_documents.append(document)
        with self.engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO disclosure_core.document(document_id,status,raw_file_hash) "
                                 "VALUES (:doc,'registered',:source)"), {"doc": document, "source": source})
            conn.execute(sa.text(
                "INSERT INTO disclosure_core.processing_run(processing_run_id,document_id,"
                "artifact_owner_processing_run_id,run_kind,status,input_raw_file_hash,"
                "provider_document_relpath,is_active,unit_build_status) VALUES "
                "(:run,:doc,:run,'parse','succeeded',:source,'scratch/history.json',:active,'succeeded')"
            ), {"run": run, "doc": document, "source": source, "active": active})
            seq = None
            if pages is not None:
                seq = conn.execute(sa.text(
                    "INSERT INTO disclosure_ops.durable_publish_base(processing_run_id,document_id,"
                    "source_identity_sha256,source_page_count,publish_precommit_at) "
                    "VALUES (:run,:doc,:source,:pages,'2001-01-01T00:00:00Z') RETURNING ledger_seq"
                ), {"run": run, "doc": document, "source": source, "pages": pages}).scalar_one()
        return document, run, seq

    def _outbox(self, *, source=None, run=None, document=None):
        event = str(ids.new_outbox_event_id())
        self.extra_events.append(event)
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO disclosure_ops.outbox_event(event_id,event_kind,change_kind,subject_kind,"
                "subject_ref,document_id,processing_run_id,payload) VALUES "
                "(:event,'processing_run_published','materialized','processing_run',:subject,"
                ":doc,:run,CAST(:payload AS jsonb))"
            ), {"event": event, "subject": run or "unattributed-history", "doc": document,
                "run": run, "payload": json.dumps({} if source is None else {"source_identity": source})})
        return event

    def test_actual_app_snapshot_is_read_only_repeatable_and_never_reads_authority_secrets(self):
        self._publish()
        before = self._authority_rows()
        fact = self.reader.read_publication(self.admission)
        self.assertIsNotNone(fact)
        snapshot = json.loads(self.receipts[-1])["snapshot"]
        self.assertEqual((snapshot["read_only"], snapshot["isolation"]), ("on", "repeatable read"))
        identity = snapshot["identity"]
        self.assertEqual((identity["session_role"], identity["current_role"]), (APP_ROLE, APP_ROLE))
        self.assertFalse(identity["session_superuser"] or identity["current_superuser"])
        self.assertEqual(before, self._authority_rows())
        self.assertTrue(self.rollbacks)
        self.assertEqual(self.commits, [])
        observed = "\n".join(self.sql)
        for forbidden in ("for update", "for share", "pg_advisory", "remote_parse_v4_secret",
                          "remote_parse_secret", "ack_token", "cancel_token"):
            self.assertNotIn(forbidden, observed)
        for statement in self.sql:
            self.assertTrue(statement.startswith(("select ", "set ", "with ")), statement)
        sink = []
        with self.assertRaises(ConfigurationError):
            PostgresM6PublishVerifier(engine=self.engine, receipt_sink=sink.append).first_ledger_for(
                (self.fixture.source_pdf_sha256,))
        self.assertEqual(sink, [])

    def _authority_rows(self):
        with self.engine.connect() as conn:
            return tuple(conn.execute(sa.text(
                "SELECT row_to_json(a)::text FROM disclosure_ops.remote_parse_attempt a "
                "WHERE attempt_id=:attempt"
            ), {"attempt": self.fixture.attempt_id}).scalars())

    def test_server_rejects_a_write_and_reader_rolls_back_without_a_receipt(self):
        injected = False

        def write_during_snapshot(conn, cursor, statement, parameters, context, executemany):
            nonlocal injected
            if not injected and "pg_current_snapshot()" in statement:
                injected = True
                conn.execute(sa.text("UPDATE disclosure_core.document SET status='failed' WHERE document_id=:doc"),
                             {"doc": self.fixture.document_id})

        sa.event.listen(self.app_engine, "before_cursor_execute", write_during_snapshot)
        with self.assertRaises(DBAPIError) as caught:
            self.reader.first_ledger_for((self.fixture.source_pdf_sha256,))
        self.assertEqual(getattr(caught.exception.orig, "sqlstate", None), "25006")
        self.assertEqual(self.receipts, [])
        self.assertEqual(self.commits, [])
        self.assertTrue(self.rollbacks)

    def test_one_history_call_keeps_one_snapshot_across_concurrent_committed_witness(self):
        source = "sha256:" + "e" * 64
        entered, release = Event(), Event()
        intercepted = False

        def pause_after_base(conn, cursor, statement, parameters, context, executemany):
            nonlocal intercepted
            if not intercepted and "SELECT b.*" in statement:
                intercepted = True
                entered.set()
                if not release.wait(10):
                    raise AssertionError("scratch snapshot barrier was not released")

        sa.event.listen(self.app_engine, "after_cursor_execute", pause_after_base)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.reader.first_ledger_for, (source,))
            try:
                self.assertTrue(entered.wait(10), "reader never reached its real base SELECT")
                self._outbox()  # New unknown provenance, committed on a different connection.
            finally:
                release.set()
            first, = future.result(timeout=10)
        second, = self.reader.first_ledger_for((source,))
        self.assertTrue(first.scan_complete)
        self.assertIsNone(first.first_ledger_seq)
        self.assertFalse(second.scan_complete)
        self.assertNotEqual(first.audit_receipt_sha256, second.audit_receipt_sha256)

    def test_global_earliest_other_document_and_page_variants_survive_source_scope(self):
        source = self.fixture.source_pdf_sha256
        self._legacy_run(source="sha256:" + "d" * 64, pages=99)
        _, old_run, old_seq = self._legacy_run(source=source, pages=34)
        self._publish()
        self._legacy_run(source=source, pages=158)
        fact, = self.reader.first_ledger_for((source,))
        self.assertTrue(fact.scan_complete)
        self.assertEqual((fact.first_processing_run_id, fact.first_ledger_seq, fact.first_source_page_count),
                         (old_run, old_seq, 34))
        self.assertEqual(self.admission.source_page_count, 2)  # Existing literal atomic fixture.
        self.assertEqual(fact.source_page_variants, 3)
        self.assertEqual(len(json.loads(self.receipts[-1])["base_rows"]), 3)

    def test_missing_base_and_unattributable_legacy_rows_never_claim_fresh_history(self):
        source = "sha256:" + "e" * 64
        fact, = self.reader.first_ledger_for((source,))
        self.assertTrue(fact.scan_complete)
        self.assertIsNone(fact.first_ledger_seq)
        for raw_source in (source, None, "legacy-invalid-hash"):
            with self.subTest(raw_source=raw_source):
                document = str(ids.new_document_id())
                self.extra_documents.append(document)
                with self.engine.begin() as conn:
                    conn.execute(sa.text("INSERT INTO disclosure_core.document(document_id,status,raw_file_hash) "
                                         "VALUES (:doc,'published',:source)"),
                                 {"doc": document, "source": raw_source})
                fact, = self.reader.first_ledger_for((source,))
                self.assertFalse(fact.scan_complete)
                self.assertIsNone(fact.first_ledger_seq)
                with self.engine.begin() as conn:
                    conn.execute(sa.text("DELETE FROM disclosure_core.document WHERE document_id=:doc"),
                                 {"doc": document})
        self._legacy_run(source=source, active=True)
        fact, = self.reader.first_ledger_for((source,))
        self.assertFalse(fact.scan_complete)

    def test_mutable_current_document_hash_cannot_exclude_unknown_old_source(self):
        old_source, new_source = "sha256:" + "e" * 64, "sha256:" + "f" * 64
        for historical_source in (None, "old-invalid-hash"):
            with self.subTest(historical_source=historical_source):
                document, run, _ = self._legacy_run(source=historical_source)
                event = self._outbox(run=run, document=document)
                with self.engine.begin() as conn:
                    conn.execute(sa.text("UPDATE disclosure_core.document SET raw_file_hash=:source "
                                         "WHERE document_id=:doc"),
                                 {"source": new_source, "doc": document})
                fact, = self.reader.first_ledger_for((old_source,))
                self.assertFalse(fact.scan_complete)
                self.assertIsNone(fact.first_ledger_seq)
                with self.engine.begin() as conn:
                    conn.execute(sa.text("DELETE FROM disclosure_ops.outbox_event WHERE event_id=:event"),
                                 {"event": event})
                # No active/current/Unit/base/outbox witness remains; this merely
                # registered historical run alone does not imply publication.
                fact, = self.reader.first_ledger_for((old_source,))
                self.assertTrue(fact.scan_complete)

    def test_orphan_outbox_with_null_document_cannot_attribute_history_from_payload_alone(self):
        requested_source = "sha256:" + "e" * 64
        fact, = self.reader.first_ledger_for((requested_source,))
        self.assertTrue(fact.scan_complete)
        self.assertIsNone(fact.first_ledger_seq)
        event = self._outbox(
            source="sha256:" + "f" * 64,
            run=str(ids.new_processing_run_id()),
            document=None,
        )
        # The payload names another valid source, but has no run or document
        # owner that can bind it. SQL NULL/NULL equality cannot prove exclusion.
        fact, = self.reader.first_ledger_for((requested_source,))
        self.assertFalse(fact.scan_complete)
        self.assertIsNone(fact.first_ledger_seq)
        self.assertEqual(fact.source_page_variants, 0)
        with self.engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM disclosure_ops.outbox_event WHERE event_id=:event"),
                         {"event": event})
        fact, = self.reader.first_ledger_for((requested_source,))
        self.assertTrue(fact.scan_complete)

    def test_true_unpublished_then_exact_published_receipt_and_sink_failure(self):
        self.assertIsNone(self.reader.read_publication(self.admission))
        self.assertIsNone(json.loads(self.receipts[-1])["publication"])
        winner = self._publish()
        fact = self.reader.read_publication(self.admission)
        with self.engine.connect() as conn:
            seq = conn.execute(sa.text("SELECT ledger_seq FROM disclosure_ops.durable_publish_base "
                                       "WHERE processing_run_id=:run"),
                               {"run": self.fixture.processing_run_id}).scalar_one()
        self.assertIsNotNone(fact)
        self.assertEqual((fact.publication.ledger_seq, fact.publication.winner_sha256,
                          fact.publication.durable_base_sha256),
                         (seq, winner.sha256, winner.durable_base_commit.durable_base_sha256))
        self.assertEqual(fact.audit_receipt_sha256, "sha256:" + hashlib.sha256(self.receipts[-1]).hexdigest())
        self.assertEqual(json.loads(self.receipts[-1])["winner_canonical_json"].encode(), winner.canonical_bytes)
        failure = OSError("receipt persist failed")

        def broken_sink(raw):
            raise failure

        reader = PostgresM6PublishVerifier(engine=self.app_engine, receipt_sink=broken_sink)
        count = len(self.rollbacks)
        with self.assertRaises(OSError) as caught:
            reader.read_publication(self.admission)
        self.assertIs(caught.exception, failure)
        self.assertGreater(len(self.rollbacks), count)

    def test_each_admission_binding_and_committed_projection_drift_fail_closed(self):
        self._publish()
        # Handwritten dimensions, independent of the implementation's field loop.
        mutations = (
            {"attempt_id": "not-the-stored-attempt"}, {"fence_identity": "another-fence"},
            {"document_id": str(ids.new_document_id())}, {"processing_run_id": "another-run"},
            {"source_pdf_sha256": "sha256:" + "f" * 64},
            {"source_byte_count": self.admission.source_byte_count + 1},
            {"source_page_count": self.admission.source_page_count + 1},
            {"process_profile_sha256": "sha256:" + "f" * 64},
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                admission = M6AttemptAdmitted.model_validate(self.admission.model_dump() | changes)
                count = len(self.receipts)
                with self.assertRaises((ValueError, V4HeadNotFound)):
                    self.reader.read_publication(admission)
                self.assertEqual(len(self.receipts), count)
        # These are persisted rows, not fake closure returns. Restore each row
        # after its independently visible mutation so the next case is adjacent.
        run = self.fixture.processing_run_id
        rows = (
            (models.DocumentUnit.__table__, "title", "changed unit title"),
            (models.OutboxEvent.__table__, "payload", {"source_identity": "sha256:" + "f" * 64}),
            (models.ProcessingRun.__table__, "provider_document_relpath", "scratch/changed.json"),
            (models.DurablePublishBase.__table__, "source_page_count", 99),
        )
        for table, field, replacement in rows:
            with self.subTest(row_table=table.name, field=field):
                selector = table.c.processing_run_id == run
                if table.name == "outbox_event":
                    selector = sa.and_(selector, table.c.event_kind == "processing_run_published")
                with self.engine.begin() as conn:
                    old_value = conn.execute(sa.select(table.c[field]).where(selector)).scalar_one()
                    conn.execute(table.update().where(selector).values({field: replacement}))
                try:
                    count = len(self.receipts)
                    with self.assertRaises(AtomicPublicationUniqueConflict):
                        self.reader.read_publication(self.admission)
                    self.assertEqual(len(self.receipts), count)
                finally:
                    with self.engine.begin() as conn:
                        conn.execute(table.update().where(selector).values({field: old_value}))
        # Committed and later superseded is not a new None/unpublished result.
        with self.engine.begin() as conn:
            conn.execute(sa.text("UPDATE disclosure_core.processing_run SET is_active=false WHERE processing_run_id=:run"),
                         {"run": self.fixture.processing_run_id})
        with self.assertRaises(AtomicPublicationUniqueConflict):
            self.reader.read_publication(self.admission)

    def test_existing_base_without_checkpoint_winner_is_a_contradiction_not_none(self):
        self.assertIsNone(self.reader.read_publication(self.admission))
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO disclosure_ops.durable_publish_base(processing_run_id,document_id,"
                "source_identity_sha256,source_page_count,publish_precommit_at) "
                "VALUES (:run,:doc,:source,:pages,'2001-01-01T00:00:00Z')"
            ), {"run": self.fixture.processing_run_id, "doc": self.fixture.document_id,
                "source": self.fixture.source_pdf_sha256, "pages": self.admission.source_page_count})
        count = len(self.receipts)
        with self.assertRaises(ValueError):
            self.reader.read_publication(self.admission)
        self.assertEqual(len(self.receipts), count)

    def test_immutable_winner_rejects_changes_and_missing_base_fails_closed(self):
        winner = self._publish()
        table = models.AtomicPublicationWinnerV4.__table__
        mutations = (
            table.delete().where(table.c.attempt_id == self.fixture.attempt_id),
            table.update().where(table.c.attempt_id == self.fixture.attempt_id).values(
                winner_bytes=b"[" + winner.canonical_bytes[1:],
            ),
        )
        for mutation in mutations:
            with self.subTest(operation=type(mutation).__name__):
                # The real immutable trigger rejects both operations before a
                # missing/corrupt winner could become visible. Keep it enabled.
                with self.assertRaises(DBAPIError) as caught:
                    with self.engine.begin() as conn:
                        conn.execute(mutation)
                self.assertEqual(getattr(caught.exception.orig, "sqlstate", None), "P0001")
                self.assertIn("immutable row cannot be changed", str(caught.exception.orig))
                fact = self.reader.read_publication(self.admission)
                self.assertIsNotNone(fact)
                self.assertEqual(fact.publication.winner_sha256, winner.sha256)
                self.assertEqual(json.loads(self.receipts[-1])["winner_canonical_json"].encode(),
                                 winner.canonical_bytes)
        with self.engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM disclosure_ops.durable_publish_base WHERE processing_run_id=:run"),
                         {"run": self.fixture.processing_run_id})
        count = len(self.receipts)
        with self.assertRaises(AtomicPublicationUniqueConflict):
            self.reader.read_publication(self.admission)
        self.assertEqual(len(self.receipts), count)


if __name__ == "__main__":
    unittest.main()
