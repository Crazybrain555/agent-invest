from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
import unittest

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE, FUTURE_L2_READER_ROLE, READER_ROLE,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.runtime.capacity_progress_relay import (
    ProgressRelayResume, encode_anchored_progress_relay_head,
)
from disclosure_anchor.application.contracts.publish_evidence_ledger import (
    DurablePublishBaseEvidence, DurablePublishSupplementEvidence,
    PublishEvidenceConflict,
)
from disclosure_anchor.application.services.full_host_hour_kpi import (
    reconcile_private_publish_ledger_rows,
)
from disclosure_anchor.application.worker.queries import durable_publish_ledger_rows
from disclosure_anchor.domain import ids
from tests.integration._support import engine_or_skip


def _sha(char: str) -> str:
    return "sha256:" + char * 64


class PublishEvidenceLedgerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.document_id = ids.new_document_id()
        self.run_id = ids.new_processing_run_id()
        self.committed = datetime.now(timezone.utc).replace(microsecond=0)
        self.relay_ids: set[str] = set()
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO disclosure_core.document (document_id,status) VALUES (:d,'registered')"
            ), {"d": self.document_id})
            conn.execute(text(
                "INSERT INTO disclosure_core.processing_run "
                "(processing_run_id,document_id,artifact_owner_processing_run_id,run_kind,status,input_raw_file_hash,normalized_ir_relpath) "
                "VALUES (:r,:d,:r,'parse','succeeded',:h,'normalized/test.json')"
            ), {"r": self.run_id, "d": self.document_id, "h": _sha("a")})

    def tearDown(self) -> None:
        with self.engine.begin() as conn:
            for relay_id in self.relay_ids:
                conn.execute(text("DELETE FROM disclosure_ops.progress_relay_head WHERE relay_id=:r"), {"r": relay_id})
            conn.execute(text("DELETE FROM disclosure_ops.durable_publish_supplement WHERE processing_run_id=:r"), {"r": self.run_id})
            conn.execute(text("DELETE FROM disclosure_ops.durable_publish_base WHERE processing_run_id=:r"), {"r": self.run_id})
            conn.execute(text("DELETE FROM disclosure_core.processing_run WHERE processing_run_id=:r"), {"r": self.run_id})
            conn.execute(text("DELETE FROM disclosure_core.document WHERE document_id=:d"), {"d": self.document_id})
        self.engine.dispose()

    def _base(self, **changes: object) -> DurablePublishBaseEvidence:
        values: dict[str, object] = {
            "processing_run_id": self.run_id, "document_id": self.document_id,
            "source_identity_sha256": _sha("a"), "source_page_count": 42,
            "publish_precommit_at": self.committed,
        }
        values.update(changes)
        return DurablePublishBaseEvidence.model_validate(values)

    def _supplement(self, supplement_id: str | None = None, **changes: object) -> DurablePublishSupplementEvidence:
        values: dict[str, object] = {
            "supplement_id": supplement_id or "pes_" + ids.new_ulid(),
            "processing_run_id": self.run_id,
            "source_identity_sha256": _sha("a"), "source_page_count": 42,
            "publish_precommit_at": self.committed,
            "host_assignment_identity_sha256": _sha("b"),
            "boot_identity_sha256": _sha("c"),
            "runtime_bundle_identity_sha256": _sha("d"),
            "process_profile_sha256": _sha("e"),
            "observer_run_id": "01890f3e-7b4a-7cc1-8c2a-1f0d3e5a7b9c",
            "observer_receipt_sha256": _sha("f"),
            "observer_seal_sha256": _sha("0"),
            "observer_contract_version": "mineru.synchronized-telemetry-receipt.v2",
            "publish_durable_observed_at": self.committed + timedelta(minutes=1),
        }
        values.update(changes)
        return DurablePublishSupplementEvidence.model_validate(values)

    def test_base_first_write_is_idempotent_conflict_visible_and_rollback_atomic(self) -> None:
        base = self._base()
        def append_base() -> DurablePublishBaseEvidence:
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                result = uow.publish_evidence.add_base(base)
                uow.commit()
                return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(tuple(pool.map(lambda _index: append_base(), range(2))), (base, base))
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            with self.assertRaises(PublishEvidenceConflict):
                uow.publish_evidence.add_base(self._base(source_page_count=43))
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                uow.publish_evidence.append_supplement(self._supplement())
                raise RuntimeError("rollback")
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_ops.durable_publish_supplement WHERE processing_run_id=:r"
            ), {"r": self.run_id}).scalar_one(), 0)

    def test_supplement_conflict_is_retained_and_replay_fails_closed(self) -> None:
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.publish_evidence.add_base(self._base())
            uow.publish_evidence.append_supplement(self._supplement())
            uow.publish_evidence.append_supplement(
                self._supplement(host_assignment_identity_sha256=_sha("9"))
            )
            uow.commit()
        with self.engine.connect() as conn:
            rows = durable_publish_ledger_rows(
                conn, started_at=self.committed - timedelta(seconds=1),
                finished_at=self.committed + timedelta(seconds=1),
            )
        evidence = reconcile_private_publish_ledger_rows(rows)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].status, "conflict")

    def test_cross_hour_interval_is_returned_for_both_affected_hours(self) -> None:
        hour = self.committed.replace(minute=0, second=0)
        lower = hour + timedelta(minutes=59, seconds=59)
        upper = hour + timedelta(hours=1, seconds=1)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.publish_evidence.add_base(self._base(publish_precommit_at=lower))
            uow.publish_evidence.append_supplement(self._supplement(
                publish_precommit_at=lower,
                publish_durable_observed_at=upper,
            ))
            uow.commit()
        with self.engine.connect() as conn:
            first = durable_publish_ledger_rows(
                conn, started_at=hour, finished_at=hour + timedelta(hours=1)
            )
            second = durable_publish_ledger_rows(
                conn,
                started_at=hour + timedelta(hours=1),
                finished_at=hour + timedelta(hours=2),
            )
        self.assertEqual({row["processing_run_id"] for row in first}, {self.run_id})
        self.assertEqual({row["processing_run_id"] for row in second}, {self.run_id})

    def test_append_only_relay_head_cas_serializes_first_row_race(self) -> None:
        resume = ProgressRelayResume(
            run_id="01890f3e-7b4a-7cc1-8c2a-1f0d3e5a7b9c",
            process_epoch_sha256=_sha("a"), runtime_bundle_identity_sha256=_sha("b"),
            process_profile_sha256=_sha("c"), clock_domain_identity_sha256=_sha("d"),
            next_sequence=0, cumulative_unique_source_pages=0, durable_sources=(),
        )
        relay_id = f"{resume.run_id}:{resume.process_epoch_sha256}"
        self.relay_ids.add(relay_id)
        head = encode_anchored_progress_relay_head(relay_id=relay_id, row_version=0, resume=resume)

        def append() -> str:
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                result = uow.publish_evidence.append_relay_head(head)
                uow.commit()
                return result.checkpoint_sha256

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(tuple(pool.map(lambda _index: append(), range(2))), (head.checkpoint_sha256,) * 2)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_ops.progress_relay_head WHERE relay_id=:r"
            ), {"r": relay_id}).scalar_one(), 1)

    def test_same_source_sequence_is_serialized_through_first_commit(self) -> None:
        second_document_id = ids.new_document_id()
        second_run_id = ids.new_processing_run_id()
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO disclosure_core.document (document_id,status) "
                "VALUES (:d,'registered')"
            ), {"d": second_document_id})
            conn.execute(text(
                "INSERT INTO disclosure_core.processing_run "
                "(processing_run_id,document_id,artifact_owner_processing_run_id,run_kind,status,input_raw_file_hash,normalized_ir_relpath) "
                "VALUES (:r,:d,:r,'parse','succeeded',:h,'normalized/test-2.json')"
            ), {"r": second_run_id, "d": second_document_id, "h": _sha("a")})
        first_locked = Event()
        permit_first_commit = Event()
        second_finished = Event()
        first = self._base()
        second = DurablePublishBaseEvidence(
            processing_run_id=second_run_id,
            document_id=second_document_id,
            source_identity_sha256=_sha("a"),
            source_page_count=42,
            publish_precommit_at=self.committed,
        )

        def append_first() -> None:
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                uow.publish_evidence.add_base(first)
                first_locked.set()
                if not permit_first_commit.wait(5):
                    raise RuntimeError("test did not release first source lock")
                uow.commit()

        def append_second() -> None:
            if not first_locked.wait(5):
                raise RuntimeError("first source lock was not acquired")
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                uow.publish_evidence.add_base(second)
                uow.commit()
            second_finished.set()

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(append_first)
                second_future = pool.submit(append_second)
                self.assertTrue(first_locked.wait(5))
                self.assertFalse(second_finished.wait(0.25))
                permit_first_commit.set()
                first_future.result(timeout=5)
                second_future.result(timeout=5)
            with self.engine.connect() as conn:
                ranked = conn.execute(text(
                    "SELECT processing_run_id FROM disclosure_ops.durable_publish_base "
                    "WHERE source_identity_sha256=:s ORDER BY ledger_seq"
                ), {"s": _sha("a")}).scalars().all()
            self.assertEqual(ranked, [self.run_id, second_run_id])
        finally:
            permit_first_commit.set()
            with self.engine.begin() as conn:
                conn.execute(text(
                    "DELETE FROM disclosure_ops.durable_publish_base "
                    "WHERE processing_run_id=:r"
                ), {"r": second_run_id})
                conn.execute(text(
                    "DELETE FROM disclosure_core.processing_run "
                    "WHERE processing_run_id=:r"
                ), {"r": second_run_id})
                conn.execute(text(
                    "DELETE FROM disclosure_core.document WHERE document_id=:d"
                ), {"d": second_document_id})

    def test_private_acl_and_legacy_outbox_are_not_promoted(self) -> None:
        with self.engine.connect() as conn:
            for role in (READER_ROLE, FUTURE_L2_READER_ROLE):
                for table in ("durable_publish_base", "durable_publish_supplement", "progress_relay_head"):
                    self.assertFalse(conn.execute(text(
                        "SELECT has_table_privilege(:role,:table,'SELECT')"
                    ), {"role": role, "table": f"disclosure_ops.{table}"}).scalar_one())
            self.assertTrue(conn.execute(text(
                "SELECT has_table_privilege(:role,'disclosure_ops.durable_publish_base','SELECT,INSERT')"
            ), {"role": APP_ROLE}).scalar_one())
            self.assertTrue(conn.execute(text(
                "SELECT has_sequence_privilege(:role,'disclosure_ops.durable_publish_ledger_seq','USAGE,SELECT')"
            ), {"role": APP_ROLE}).scalar_one())
            for role in (READER_ROLE, FUTURE_L2_READER_ROLE):
                self.assertFalse(conn.execute(text(
                    "SELECT has_sequence_privilege(:role,'disclosure_ops.durable_publish_ledger_seq','USAGE')"
                ), {"role": role}).scalar_one())
            rows = durable_publish_ledger_rows(
                conn, started_at=self.committed - timedelta(days=1),
                finished_at=self.committed + timedelta(days=1),
            )
            self.assertEqual(rows, [])

        # A privilege predicate is insufficient: prove the application role can
        # consume the explicit sequence while the owner transaction rolls back.
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                conn.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
                conn.execute(text(
                    "INSERT INTO disclosure_ops.durable_publish_base "
                    "(processing_run_id,document_id,source_identity_sha256,source_page_count,publish_precommit_at) "
                    "VALUES (:r,:d,:s,42,:t)"
                ), {"r": self.run_id, "d": self.document_id, "s": _sha("a"), "t": self.committed})
            finally:
                transaction.rollback()


if __name__ == "__main__":
    unittest.main()
