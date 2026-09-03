"""Scratch-PostgreSQL tests for the shared V3/V4 document generation gate."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest

import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from disclosure_anchor.application.contracts.remote_parse_checkpoint import (
    PreparedReconcileReceipt,
    RemoteParseAttempt,
    RemoteParseCheckpointConflict,
    RemoteParseResumeSecret,
    encode_checkpoint_receipt,
)
from disclosure_anchor.application.contracts.staged_credit import (
    CreditShapeFacts,
    build_staged_credit_envelope,
    credit_shape,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    V4DocumentCurrentConflict,
    V4PreparedCreation,
)
from disclosure_anchor.domain import ids
from tests.integration._remote_parse_v4_factory import (
    V4AuthorityFixture,
    build_v4_authority_fixture,
    insert_core_rows,
    install_prepared_cycle,
)
from tests.integration._support import engine_or_skip
from tests.unit.test_mineru_process_profile import _profile


class RemoteParseCrossVersionLockIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.fixture = build_v4_authority_fixture()

    def tearDown(self) -> None:
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                "TRUNCATE TABLE disclosure_ops.remote_parse_attempt CASCADE"
            )
            conn.execute(
                sa.text(
                    "DELETE FROM disclosure_core.processing_run "
                    "WHERE document_id=:document_id"
                ),
                {"document_id": self.fixture.document_id},
            )
            conn.execute(
                sa.text(
                    "DELETE FROM disclosure_core.document "
                    "WHERE document_id=:document_id"
                ),
                {"document_id": self.fixture.document_id},
            )
        self.engine.dispose()

    @staticmethod
    def _v4_creation(fixture: V4AuthorityFixture) -> V4PreparedCreation:
        return V4PreparedCreation(
            checkpoint=fixture.prepared,
            reservation=fixture.reservation,
            preparation_intent=fixture.preparation,
            snapshot_receipt=fixture.snapshot,
            parser_target_sha256=fixture.parser_target_sha256,
            client_submit_key=fixture.client_submit_key,
        )

    @staticmethod
    def _v3_creation(
        fixture: V4AuthorityFixture,
        *,
        attempt_id: str,
    ) -> tuple[RemoteParseAttempt, RemoteParseResumeSecret]:
        envelope = build_staged_credit_envelope(
            profile=_profile(),
            source_pdf_sha256=fixture.source_pdf_sha256,
            source_byte_count=fixture.reservation.source_byte_count,
            source_page_count=fixture.reservation.source_page_count,
        )
        fence_identity = "fence-v3-" + ids.new_ulid()
        client_submit_key = "submit-v3-" + ids.new_ulid()
        request_sha256 = "sha256:" + hashlib.sha256(
            (attempt_id + ":v3-request").encode()
        ).hexdigest()
        runtime_epoch_sha256 = "sha256:" + hashlib.sha256(
            (attempt_id + ":v3-epoch").encode()
        ).hexdigest()
        attempt = RemoteParseAttempt(
            attempt_id=attempt_id,
            processing_run_id=fixture.processing_run_id,
            document_id=fixture.document_id,
            attempt_generation=1,
            fence_identity=fence_identity,
            source_pdf_sha256=fixture.source_pdf_sha256,
            parser_target_sha256=fixture.parser_target_sha256,
            request_sha256=request_sha256,
            runtime_epoch_sha256=runtime_epoch_sha256,
            client_submit_key=client_submit_key,
            checkpoint_contract_version=3,
            process_profile_sha256=envelope.process_profile_sha256,
            credit_policy_sha256=envelope.credit_policy_sha256,
            reservation_input_bytes=envelope.reservation_input.exact_bytes,
            reservation_input_sha256=envelope.reservation_input.sha256,
            reservation_input_byte_count=envelope.reservation_input.byte_count,
            reservation_source_byte_count=fixture.reservation.source_byte_count,
            reservation_source_page_count=fixture.reservation.source_page_count,
            reservation_bucket=envelope.reservation_input.value.bucket,
            reservation=envelope.reservation,
            current_credits=credit_shape("prepared", CreditShapeFacts()),
        )
        encoded = encode_checkpoint_receipt(
            PreparedReconcileReceipt(
                attempt_identity=attempt_id,
                fence_identity=fence_identity,
                source_pdf_sha256=fixture.source_pdf_sha256,
                client_submit_key=client_submit_key,
                submission_epoch_unix=100,
                parser_target_sha256=fixture.parser_target_sha256,
                request_sha256=request_sha256,
                runtime_epoch_sha256=runtime_epoch_sha256,
            )
        )
        secret = RemoteParseResumeSecret(
            attempt_id=attempt_id,
            secret_kind="prepared_reconcile",
            token_bytes=encoded.exact_bytes,
            token_sha256=encoded.sha256,
            token_byte_count=encoded.byte_count,
            secret_contract_version=3,
        )
        return attempt, secret

    def test_same_attempt_id_v4_is_a_typed_v3_conflict(self) -> None:
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, self.fixture)
        attempt, secret = self._v3_creation(
            self.fixture,
            attempt_id=self.fixture.attempt_id,
        )

        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
            RemoteParseCheckpointConflict,
            "cross-version",
        ):
            uow.remote_parse_attempts.add_v3_prepared(attempt, secret)

    def test_concurrent_v3_v4_initial_create_has_one_typed_winner(self) -> None:
        with self.engine.begin() as conn:
            insert_core_rows(conn, self.fixture)
        v3_attempt, v3_secret = self._v3_creation(
            self.fixture,
            attempt_id="rpa_" + ids.new_ulid(),
        )
        barrier = Barrier(2)

        def create_v3() -> str | Exception:
            try:
                with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                    barrier.wait(timeout=5)
                    uow.remote_parse_attempts.add_v3_prepared(
                        v3_attempt,
                        v3_secret,
                    )
                    uow.commit()
                return "v3"
            except Exception as exc:  # outcome classification is asserted below
                return exc

        def create_v4() -> str | Exception:
            try:
                with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                    barrier.wait(timeout=5)
                    uow.remote_parse_v4.create_prepared(
                        self._v4_creation(self.fixture)
                    )
                    uow.commit()
                return "v4"
            except Exception as exc:  # outcome classification is asserted below
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            v3_future = pool.submit(create_v3)
            v4_future = pool.submit(create_v4)
            outcomes = (v3_future.result(), v4_future.result())

        winners = tuple(item for item in outcomes if isinstance(item, str))
        losers = tuple(item for item in outcomes if isinstance(item, Exception))
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(len(losers), 1, outcomes)
        self.assertIsInstance(
            losers[0],
            (RemoteParseCheckpointConflict, V4DocumentCurrentConflict),
        )
        with self.engine.connect() as conn:
            rows = tuple(
                conn.execute(
                    sa.text(
                        "SELECT attempt_id,attempt_generation,is_current,"
                        "checkpoint_contract_version FROM "
                        "disclosure_ops.remote_parse_attempt "
                        "WHERE document_id=:document_id"
                    ),
                    {"document_id": self.fixture.document_id},
                ).mappings()
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempt_generation"], 1)
        self.assertTrue(rows[0]["is_current"])
        self.assertEqual(
            rows[0]["checkpoint_contract_version"],
            3 if winners == ("v3",) else 4,
        )


if __name__ == "__main__":
    unittest.main()
