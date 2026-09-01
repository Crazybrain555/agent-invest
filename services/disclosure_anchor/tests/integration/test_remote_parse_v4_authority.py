"""Scratch-PostgreSQL behavior tests for the append-only V4 authority."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import importlib
import subprocess
import unittest

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    FUTURE_L2_READER_ROLE,
    READER_ROLE,
)
from disclosure_anchor.adapters.security.provider_secret_cipher import (
    AesGcmProviderSecretCipher,
)
from disclosure_anchor.adapters.security.provider_secret_keyring import (
    StaticProviderSecretKeyring,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    EvidenceValueV4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    RemoteParseCheckpointV4,
)
from tests.integration._remote_parse_v4_factory import (
    EVIDENCE_FIELD_NAMES,
    HELD_CREDIT_NAMES,
    V4AuthorityFixture,
    append_remote_failed_tail,
    build_v4_authority_fixture,
    insert_checkpoint,
    insert_core_rows,
    insert_evidence,
    insert_legacy_head,
    insert_secret,
    insert_v4_head,
    insert_winner,
    install_acked_cycle,
    install_local_materialized_cycle,
    install_prepared_cycle,
    install_remote_failed_without_secret,
    install_resource_free_failure,
    install_success_ack_pending_cycle,
    install_submitted_cycle,
    sha256_bytes,
    update_v4_head,
)
from tests.integration._support import engine_or_skip, run_alembic


V4_TABLES = (
    "remote_parse_v4_evidence",
    "remote_parse_v4_checkpoint",
    "remote_parse_v4_secret",
    "atomic_publication_winner_v4",
)
PURGE_FUNCTION = (
    "disclosure_ops.purge_remote_parse_v4_secrets_final(text,text,bigint,text,bigint)"
)
V4_MIGRATION_MODULE = (
    "disclosure_anchor.adapters.db.postgres.migrations.versions."
    "0057_remote_parse_v4_authority"
)


class RemoteParseV4AuthorityIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _force_constraints(conn: Connection) -> None:
        conn.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")

    @staticmethod
    def _defer_constraints(conn: Connection) -> None:
        conn.exec_driver_sql("SET CONSTRAINTS ALL DEFERRED")

    def _alembic(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        # Never build the child environment here: run_alembic pins every
        # database URL variable to this test engine.
        return run_alembic(self.engine, *arguments)

    def _restore_migration_head(self) -> None:
        restored = self._alembic("upgrade", "head")
        self.assertEqual(
            restored.returncode,
            0,
            restored.stdout + restored.stderr,
        )

    def _assert_rejected(
        self,
        conn: Connection,
        action: Callable[[], None],
        *,
        message: str | None = None,
    ) -> None:
        savepoint = conn.begin_nested()
        try:
            with self.assertRaises(SQLAlchemyError) as caught:
                action()
                self._force_constraints(conn)
            if message is not None:
                self.assertIn(message, str(caught.exception))
        finally:
            if savepoint.is_active:
                savepoint.rollback()
        self._defer_constraints(conn)

    def _insert_forged_checkpoint_path(
        self,
        conn: Connection,
        fixture: V4AuthorityFixture,
        *,
        chain: tuple[RemoteParseCheckpointV4, ...],
        evidence_values: tuple[EvidenceValueV4, ...],
        target_state: str,
        held_credit_overrides: dict[str, int] | None = None,
        source_byte_count_override: int | None = None,
        source_page_count_override: int | None = None,
    ) -> None:
        insert_core_rows(conn, fixture)
        insert_v4_head(conn, fixture, fixture.prepared)
        evidence_by_hash = {item.sha256: item for item in evidence_values}
        inserted_evidence: set[str] = set()
        secret_inserted = False
        winner_inserted = False
        for checkpoint in chain:
            for field_name in EVIDENCE_FIELD_NAMES:
                evidence_sha256 = getattr(checkpoint, field_name)
                if (
                    evidence_sha256 is not None
                    and evidence_sha256 not in inserted_evidence
                ):
                    insert_evidence(
                        conn,
                        fixture,
                        evidence_by_hash[evidence_sha256],
                    )
                    inserted_evidence.add(evidence_sha256)
            if (
                checkpoint.publication_winner_sha256 is not None
                and not winner_inserted
            ):
                insert_winner(conn, fixture)
                winner_inserted = True
            if checkpoint.state == target_state:
                insert_checkpoint(
                    conn,
                    fixture,
                    checkpoint,
                    held_credit_overrides=held_credit_overrides,
                    source_byte_count_override=source_byte_count_override,
                    source_page_count_override=source_page_count_override,
                )
                return
            insert_checkpoint(conn, fixture, checkpoint)
            if checkpoint.state != "prepared":
                update_v4_head(conn, fixture, checkpoint)
            if checkpoint.state == "submitted" and not secret_inserted:
                insert_secret(conn, fixture)
                secret_inserted = True
        raise AssertionError(f"target checkpoint state is absent: {target_state}")

    def test_resource_free_initial_failure_is_a_legal_retained_head(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_resource_free_failure(conn, fixture)
                self._force_constraints(conn)
                head = conn.execute(
                    text(
                        "SELECT checkpoint_contract_version,state,is_current,"
                        "row_version,current_checkpoint_sha256,claim_generation,"
                        "claim_owner_identity,claim_lease_until "
                        "FROM disclosure_ops.remote_parse_attempt "
                        "WHERE attempt_id=:attempt_id"
                    ),
                    {"attempt_id": fixture.attempt_id},
                ).one()
                self.assertEqual(
                    tuple(head),
                    (
                        4,
                        "preparation_failed",
                        False,
                        0,
                        fixture.preparation_failed.sha256,
                        0,
                        None,
                        None,
                    ),
                )
                self.assertEqual(
                    conn.execute(
                        text(
                            "SELECT count(*) FROM "
                            "disclosure_ops.remote_parse_v4_secret "
                            "WHERE attempt_id=:attempt_id"
                        ),
                        {"attempt_id": fixture.attempt_id},
                    ).scalar_one(),
                    0,
                )
            finally:
                transaction.rollback()

    def test_each_nonfinal_checkpoint_credit_shape_is_durable_authority(
        self,
    ) -> None:
        cases = {
            "prepared": {"provider_tasks": 1},
            "reconciling": {"remote_waits": 0},
            "submitted": {"ack_items": 0},
            "remote_terminal": {"provider_result_bytes": 0},
            "materializing": {"materialization_items": 0},
            "local_materialized": {"output_pages": 3},
            "publish_committed": {"output_bytes": 0},
            "cleanup_pending": {"provider_tasks": 0},
            "ack_pending": {"provider_result_bytes": 1},
        }
        success_states = {
            "remote_terminal",
            "materializing",
            "local_materialized",
            "publish_committed",
        }
        for state, overrides in cases.items():
            with self.subTest(state=state):
                fixture = build_v4_authority_fixture()
                if state in success_states:
                    chain = fixture.success_checkpoints
                    evidence = fixture.success_evidence
                else:
                    chain = fixture.final_checkpoints
                    evidence = fixture.final_evidence
                with self.engine.connect() as conn:
                    transaction = conn.begin()
                    try:
                        self._assert_rejected(
                            conn,
                            lambda: self._insert_forged_checkpoint_path(
                                conn,
                                fixture,
                                chain=chain,
                                evidence_values=evidence,
                                target_state=state,
                                held_credit_overrides=overrides,
                            ),
                            message=(
                                "cleanup checkpoint changed held credit"
                                if state == "cleanup_pending"
                                else "ck_remote_parse_v4_checkpoint_credit_shape"
                            ),
                        )
                    finally:
                        transaction.rollback()

    def test_checkpoint_source_counts_are_immutable_across_successors(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                self._assert_rejected(
                    conn,
                    lambda: self._insert_forged_checkpoint_path(
                        conn,
                        fixture,
                        chain=fixture.final_checkpoints,
                        evidence_values=fixture.final_evidence,
                        target_state="reconciling",
                        source_page_count_override=(
                            fixture.reconciling.source_page_count + 1
                        ),
                    ),
                    message="checkpoint source counts drifted",
                )
            finally:
                transaction.rollback()

    def test_prepared_current_cycle_is_legal_and_exact(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_prepared_cycle(conn, fixture)
                self._force_constraints(conn)
                row = conn.execute(
                    text(
                        "SELECT a.state,a.row_version,a.current_checkpoint_sha256,"
                        "c.state,c.checkpoint_sha256,c.resource_reservation_sha256 "
                        "FROM disclosure_ops.remote_parse_attempt a JOIN "
                        "disclosure_ops.remote_parse_v4_checkpoint c ON "
                        "c.attempt_id=a.attempt_id AND "
                        "c.lifecycle_version=a.row_version AND "
                        "c.checkpoint_sha256=a.current_checkpoint_sha256 "
                        "WHERE a.attempt_id=:attempt_id"
                    ),
                    {"attempt_id": fixture.attempt_id},
                ).one()
                self.assertEqual(
                    tuple(row),
                    (
                        "prepared",
                        0,
                        fixture.prepared.sha256,
                        "prepared",
                        fixture.prepared.sha256,
                        fixture.reservation.sha256,
                    ),
                )
            finally:
                transaction.rollback()

    def test_legacy_parent_cannot_own_a_v4_child(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                insert_core_rows(conn, fixture)
                insert_legacy_head(conn, fixture)

                cases = (
                    (
                        "remote_parse_v4_evidence",
                        lambda: insert_evidence(
                            conn, fixture, fixture.preparation_failure
                        ),
                    ),
                    (
                        "remote_parse_v4_checkpoint",
                        lambda: (
                            insert_evidence(
                                conn, fixture, fixture.preparation_failure
                            ),
                            insert_checkpoint(
                                conn, fixture, fixture.preparation_failed
                            ),
                        ),
                    ),
                    (
                        "atomic_publication_winner_v4",
                        lambda: insert_winner(conn, fixture),
                    ),
                    (
                        "remote_parse_v4_secret",
                        lambda: (
                            insert_evidence(conn, fixture, fixture.accepted),
                            insert_secret(conn, fixture),
                        ),
                    ),
                )
                for table_name, insert_child in cases:
                    with self.subTest(table_name=table_name):

                        def insert_and_force_target() -> None:
                            insert_child()
                            conn.exec_driver_sql(
                                "SET CONSTRAINTS disclosure_ops."
                                f"ck_{table_name}_v4_parent IMMEDIATE"
                            )

                        self._assert_rejected(
                            conn,
                            insert_and_force_target,
                            message="remote parse v4 child lacks exact v4 parent",
                        )
                self.assertEqual(
                    conn.execute(
                        text(
                            "SELECT checkpoint_contract_version FROM "
                            "disclosure_ops.remote_parse_attempt "
                            "WHERE attempt_id=:attempt_id"
                        ),
                        {"attempt_id": fixture.attempt_id},
                    ).scalar_one(),
                    1,
                )
            finally:
                transaction.rollback()

    def test_v4_head_cannot_be_inserted_after_lifecycle_zero(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                insert_core_rows(conn, fixture)
                self._assert_rejected(
                    conn,
                    lambda: insert_v4_head(conn, fixture, fixture.submitted),
                    message=(
                        "remote parse v4 head must be inserted at lifecycle "
                        "version zero"
                    ),
                )
            finally:
                transaction.rollback()

    def test_v4_head_contract_and_identity_are_immutable(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_prepared_cycle(conn, fixture)
                self._force_constraints(conn)
                self._defer_constraints(conn)
                for column, value in (
                    ("checkpoint_contract_version", 3),
                    ("fence_identity", "different-fence"),
                ):
                    with self.subTest(column=column):

                        def drift() -> None:
                            conn.execute(
                                text(
                                    "UPDATE disclosure_ops.remote_parse_attempt "
                                    f"SET {column}=:value WHERE attempt_id=:attempt_id"
                                ),
                                {
                                    "value": value,
                                    "attempt_id": fixture.attempt_id,
                                },
                            )

                        self._assert_rejected(conn, drift)
                observed = conn.execute(
                    text(
                        "SELECT checkpoint_contract_version,fence_identity FROM "
                        "disclosure_ops.remote_parse_attempt "
                        "WHERE attempt_id=:attempt_id"
                    ),
                    {"attempt_id": fixture.attempt_id},
                ).one()
                self.assertEqual(tuple(observed), (4, fixture.fence_identity))
            finally:
                transaction.rollback()

    def test_checkpoint_chain_rejects_state_jump_and_evidence_loss(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_prepared_cycle(conn, fixture)
                self._force_constraints(conn)
                self._defer_constraints(conn)

                jumped = replace(
                    fixture.submitted,
                    lifecycle_version=1,
                    previous_checkpoint_sha256=fixture.prepared.sha256,
                )

                def insert_jumped_checkpoint() -> None:
                    insert_evidence(conn, fixture, fixture.submission)
                    insert_evidence(conn, fixture, fixture.accepted)
                    insert_checkpoint(conn, fixture, jumped)
                    insert_secret(conn, fixture)
                    update_v4_head(conn, fixture, jumped)

                self._assert_rejected(
                    conn,
                    insert_jumped_checkpoint,
                    message="state transition is invalid",
                )

                insert_evidence(conn, fixture, fixture.submission)
                insert_checkpoint(conn, fixture, fixture.reconciling)
                update_v4_head(conn, fixture, fixture.reconciling)
                self._force_constraints(conn)
                self._defer_constraints(conn)

                insert_evidence(conn, fixture, fixture.accepted)
                insert_checkpoint(conn, fixture, fixture.submitted)
                insert_secret(conn, fixture)
                update_v4_head(conn, fixture, fixture.submitted)
                self._force_constraints(conn)
                self._defer_constraints(conn)

                insert_evidence(conn, fixture, fixture.terminal)
                insert_checkpoint(conn, fixture, fixture.remote_terminal)
                update_v4_head(conn, fixture, fixture.remote_terminal)
                self._force_constraints(conn)
                self._defer_constraints(conn)

                def discard_inherited_submission_evidence() -> None:
                    insert_evidence(conn, fixture, fixture.materialization_intent)
                    insert_checkpoint(
                        conn,
                        fixture,
                        fixture.materializing,
                        column_overrides={"submission_intent_sha256": None},
                    )
                    update_v4_head(conn, fixture, fixture.materializing)

                self._assert_rejected(
                    conn,
                    discard_inherited_submission_evidence,
                    message="discarded immutable evidence",
                )
            finally:
                transaction.rollback()

    def test_checkpoint_successor_and_head_cas_cannot_split_or_skip(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_prepared_cycle(conn, fixture)
                self._force_constraints(conn)
                self._defer_constraints(conn)
                insert_evidence(conn, fixture, fixture.submission)
                self._assert_rejected(
                    conn,
                    lambda: insert_checkpoint(conn, fixture, fixture.reconciling),
                    message="remote parse v4 checkpoint is ahead of its head",
                )
                head = conn.execute(
                    text(
                        "SELECT row_version,state FROM "
                        "disclosure_ops.remote_parse_attempt "
                        "WHERE attempt_id=:attempt_id"
                    ),
                    {"attempt_id": fixture.attempt_id},
                ).one()
                self.assertEqual(tuple(head), (0, "prepared"))

                def skip_a_lifecycle_version() -> None:
                    insert_evidence(conn, fixture, fixture.accepted)
                    insert_checkpoint(conn, fixture, fixture.reconciling)
                    insert_checkpoint(conn, fixture, fixture.submitted)
                    insert_secret(conn, fixture)
                    update_v4_head(conn, fixture, fixture.submitted)

                self._assert_rejected(
                    conn,
                    skip_a_lifecycle_version,
                    message=(
                        "remote parse v4 head lifecycle version must advance "
                        "exactly once"
                    ),
                )
            finally:
                transaction.rollback()

    def test_submission_intent_evidence_cannot_commit_without_checkpoint(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_prepared_cycle(conn, fixture)
                self._force_constraints(conn)
                self._defer_constraints(conn)

                def insert_orphan_submission_intent() -> None:
                    insert_evidence(conn, fixture, fixture.submission)

                self._assert_rejected(
                    conn,
                    insert_orphan_submission_intent,
                    message=(
                        "remote parse v4 evidence is not closed by a checkpoint"
                    ),
                )
            finally:
                transaction.rollback()

    def test_publication_winner_requires_committed_successor(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_local_materialized_cycle(conn, fixture)
                self._force_constraints(conn)
                self._defer_constraints(conn)
                self._assert_rejected(
                    conn,
                    lambda: insert_winner(conn, fixture),
                    message=(
                        "remote parse v4 publication winner lacks its "
                        "committed checkpoint"
                    ),
                )
            finally:
                transaction.rollback()

    def test_winner_committed_checkpoint_and_head_close_atomically(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_local_materialized_cycle(conn, fixture)
                self._force_constraints(conn)
                self._defer_constraints(conn)
                insert_winner(conn, fixture)
                insert_checkpoint(conn, fixture, fixture.publish_committed)
                update_v4_head(conn, fixture, fixture.publish_committed)
                self._force_constraints(conn)
                observed = conn.execute(
                    text(
                        "SELECT a.state,a.row_version,a.current_checkpoint_sha256,"
                        "c.publication_winner_sha256,w.winner_sha256 FROM "
                        "disclosure_ops.remote_parse_attempt a JOIN "
                        "disclosure_ops.remote_parse_v4_checkpoint c ON "
                        "c.attempt_id=a.attempt_id AND "
                        "c.lifecycle_version=a.row_version AND "
                        "c.checkpoint_sha256=a.current_checkpoint_sha256 JOIN "
                        "disclosure_ops.atomic_publication_winner_v4 w ON "
                        "w.attempt_id=c.attempt_id AND "
                        "w.winner_sha256=c.publication_winner_sha256 "
                        "WHERE a.attempt_id=:attempt_id"
                    ),
                    {"attempt_id": fixture.attempt_id},
                ).one()
                self.assertEqual(
                    tuple(observed),
                    (
                        "publish_committed",
                        fixture.publish_committed.lifecycle_version,
                        fixture.publish_committed.sha256,
                        fixture.publication_winner.sha256,
                        fixture.publication_winner.sha256,
                    ),
                )
            finally:
                transaction.rollback()

    def test_complete_success_chain_reaches_acked_and_purges_secret(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_acked_cycle(conn, fixture)
                self._force_constraints(conn)
                head = conn.execute(
                    text(
                        "SELECT state,is_current,row_version,"
                        "current_checkpoint_sha256 FROM "
                        "disclosure_ops.remote_parse_attempt "
                        "WHERE attempt_id=:attempt_id"
                    ),
                    {"attempt_id": fixture.attempt_id},
                ).one()
                self.assertEqual(
                    tuple(head),
                    (
                        "acked",
                        False,
                        fixture.acked.lifecycle_version,
                        fixture.acked.sha256,
                    ),
                )
                self.assertEqual(
                    conn.execute(
                        text(
                            "SELECT count(*) FROM "
                            "disclosure_ops.remote_parse_v4_secret "
                            "WHERE attempt_id=:attempt_id"
                        ),
                        {"attempt_id": fixture.attempt_id},
                    ).scalar_one(),
                    0,
                )
            finally:
                transaction.rollback()

    def test_checkpoint_state_evidence_rejects_missing_winner_and_ack(self) -> None:
        cases = (
            ("publish_committed", {"publication_winner_sha256": None}),
            (
                "acked",
                {
                    "publication_winner_sha256": None,
                    "ack_receipt_sha256": None,
                },
            ),
        )
        for state, overrides in cases:
            with self.subTest(state=state), self.engine.connect() as conn:
                transaction = conn.begin()
                try:
                    fixture = build_v4_authority_fixture()
                    if state == "publish_committed":
                        install_local_materialized_cycle(conn, fixture)
                        checkpoint = fixture.publish_committed
                    else:
                        install_success_ack_pending_cycle(conn, fixture)
                        checkpoint = fixture.acked
                    self._force_constraints(conn)
                    self._defer_constraints(conn)
                    self._assert_rejected(
                        conn,
                        lambda: insert_checkpoint(
                            conn,
                            fixture,
                            checkpoint,
                            column_overrides=overrides,
                        ),
                        message=(
                            "ck_remote_parse_v4_checkpoint_state_evidence"
                        ),
                    )
                finally:
                    transaction.rollback()

    def test_checkpoint_state_evidence_rejects_terminal_shape_drift(self) -> None:
        fixture = build_v4_authority_fixture()
        cases = (
            (
                "cleanup_without_outcome",
                fixture.cleanup_pending,
                {"failure_receipt_sha256": None},
            ),
            (
                "ack_pending_without_outcome",
                fixture.ack_pending,
                {"failure_receipt_sha256": None},
            ),
            (
                "pre_submission_failure_with_ack",
                fixture.remote_failed,
                {
                    "state": "pre_submission_failed",
                    "accepted_submission_sha256": None,
                },
            ),
            (
                "unaccepted_supersession_with_ack",
                fixture.remote_failed,
                {
                    "state": "superseded",
                    "accepted_submission_sha256": None,
                    "failure_receipt_sha256": None,
                    "supersession_receipt_sha256": (
                        fixture.remote_failure.sha256
                    ),
                },
            ),
        )
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                for shape, checkpoint, overrides in cases:
                    with self.subTest(shape=shape):
                        self._assert_rejected(
                            conn,
                            lambda checkpoint=checkpoint, overrides=overrides: (
                                insert_checkpoint(
                                    conn,
                                    fixture,
                                    checkpoint,
                                    column_overrides=overrides,
                                )
                            ),
                            message=(
                                "ck_remote_parse_v4_checkpoint_state_evidence"
                            ),
                        )
            finally:
                transaction.rollback()

    def test_checkpoint_chain_rejects_evidence_introduced_on_wrong_edge(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_submitted_cycle(conn, fixture, include_secret=True)
                self._force_constraints(conn)
                self._defer_constraints(conn)

                def introduce_local_receipt_during_cleanup() -> None:
                    for evidence in (
                        fixture.remote_failure,
                        fixture.cleanup_plan,
                        fixture.local_materialization_receipt,
                    ):
                        insert_evidence(conn, fixture, evidence)
                    insert_checkpoint(
                        conn,
                        fixture,
                        fixture.cleanup_pending,
                        column_overrides={
                            "local_materialization_receipt_sha256": (
                                fixture.local_materialization_receipt.sha256
                            )
                        },
                    )
                    update_v4_head(conn, fixture, fixture.cleanup_pending)

                self._assert_rejected(
                    conn,
                    introduce_local_receipt_during_cleanup,
                    message="introduced unexpected evidence",
                )
            finally:
                transaction.rollback()

    def test_accepted_current_head_without_secret_is_rejected(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                with self.assertRaises(SQLAlchemyError):
                    install_submitted_cycle(
                        conn,
                        fixture,
                        include_secret=False,
                    )
                    self._force_constraints(conn)
            finally:
                transaction.rollback()

    def test_final_head_cannot_retain_or_revive_secret(self) -> None:
        retained = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_submitted_cycle(conn, retained, include_secret=True)
                self._force_constraints(conn)
                self._defer_constraints(conn)
                self._assert_rejected(
                    conn,
                    lambda: append_remote_failed_tail(conn, retained),
                )
            finally:
                transaction.rollback()

        revived = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_remote_failed_without_secret(conn, revived)
                self._force_constraints(conn)
                self._defer_constraints(conn)
                self._assert_rejected(
                    conn,
                    lambda: insert_secret(conn, revived),
                )
            finally:
                transaction.rollback()

    def test_secret_history_accepts_a_cryptographic_revision_two_rewrap(
        self,
    ) -> None:
        fixture = build_v4_authority_fixture()
        cipher = AesGcmProviderSecretCipher(
            keyring=StaticProviderSecretKeyring(
                primary_kek_id="kek-test",
                keks={"kek-test": b"k" * 32},
            ),
            rng=lambda count: b"r" * count,
        )
        rewrapped = cipher.rewrap(fixture.sealed_secret)
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_submitted_cycle(conn, fixture, include_secret=True)
                self._force_constraints(conn)
                self._defer_constraints(conn)
                insert_secret(conn, fixture, rewrapped)
                self._force_constraints(conn)
                observed = conn.execute(
                    text(
                        "SELECT count(*),min(encryption_revision),"
                        "max(encryption_revision) FROM "
                        "disclosure_ops.remote_parse_v4_secret "
                        "WHERE attempt_id=:attempt_id"
                    ),
                    {"attempt_id": fixture.attempt_id},
                ).one()
                self.assertEqual(tuple(observed), (2, 1, 2))
            finally:
                transaction.rollback()

    def test_secret_history_rejects_revision_gap_and_data_layer_drift(
        self,
    ) -> None:
        gap_fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_submitted_cycle(conn, gap_fixture, include_secret=True)
                self._force_constraints(conn)
                self._defer_constraints(conn)
                self._assert_rejected(
                    conn,
                    lambda: insert_secret(
                        conn,
                        gap_fixture,
                        replace(
                            gap_fixture.sealed_secret,
                            encryption_revision=3,
                        ),
                    ),
                    message=(
                        "remote parse v4 secret revisions are not contiguous"
                    ),
                )
            finally:
                transaction.rollback()

        drift_fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_submitted_cycle(conn, drift_fixture, include_secret=True)
                self._force_constraints(conn)
                self._defer_constraints(conn)
                self._assert_rejected(
                    conn,
                    lambda: insert_secret(
                        conn,
                        drift_fixture,
                        replace(
                            drift_fixture.sealed_secret,
                            encryption_revision=2,
                            wrap_nonce=b"r" * 12,
                            data_nonce=b"x" * 12,
                        ),
                    ),
                    message=(
                        "remote parse v4 secret immutable data layer drifted"
                    ),
                )
            finally:
                transaction.rollback()

    def test_v4_parent_rejects_both_legacy_plaintext_secret_tables(self) -> None:
        fixture = build_v4_authority_fixture()
        payload = b"forbidden-v4-plaintext-secret"
        parameters = {
            "attempt_id": fixture.attempt_id,
            "token_bytes": payload,
            "token_sha256": sha256_bytes(payload),
            "token_byte_count": len(payload),
        }
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_prepared_cycle(conn, fixture)
                self._force_constraints(conn)
                self._defer_constraints(conn)

                def insert_v2_plaintext_secret() -> None:
                    conn.execute(
                        text(
                            "INSERT INTO disclosure_ops.remote_parse_resume_secret "
                            "(attempt_id,secret_kind,secret_contract_version,"
                            "token_bytes,token_sha256,token_byte_count) VALUES "
                            "(:attempt_id,'prepared_reconcile',2,:token_bytes,"
                            ":token_sha256,:token_byte_count)"
                        ),
                        parameters,
                    )

                self._assert_rejected(
                    conn,
                    insert_v2_plaintext_secret,
                    message="legacy resume secret parent contract is invalid",
                )

                def insert_v3_plaintext_secret() -> None:
                    conn.execute(
                        text(
                            "INSERT INTO "
                            "disclosure_ops.remote_parse_v3_resume_secret "
                            "(attempt_id,secret_kind,token_bytes,token_sha256,"
                            "token_byte_count) VALUES "
                            "(:attempt_id,'prepared_reconcile',:token_bytes,"
                            ":token_sha256,:token_byte_count)"
                        ),
                        parameters,
                    )

                self._assert_rejected(
                    conn,
                    insert_v3_plaintext_secret,
                    message="v3 resume secret parent contract is invalid",
                )
            finally:
                transaction.rollback()

    def test_semantic_receipt_locator_accepts_only_supported_shapes(self) -> None:
        fixture = build_v4_authority_fixture()
        canonical_hash = "sha256:" + "a" * 64
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                insert_core_rows(conn, fixture)
                conn.execute(
                    text(
                        "UPDATE disclosure_core.processing_run SET "
                        "document_units_relpath='derived/document_units.v1.jsonl' "
                        "WHERE processing_run_id=:run_id"
                    ),
                    {"run_id": fixture.processing_run_id},
                )
                accepted = (
                    (None, None, None),
                    (None, None, canonical_hash),
                    (
                        "derived/semantic_route_receipts.v2.jsonl",
                        "semantic_route_receipt.v2",
                        canonical_hash,
                    ),
                    (
                        "derived/semantic_route_receipts.v3.jsonl",
                        "semantic_route_receipt.v3",
                        canonical_hash,
                    ),
                )
                update_locator = text(
                    "UPDATE disclosure_core.processing_run SET "
                    "semantic_route_receipts_relpath=:relpath,"
                    "semantic_route_receipts_contract_version=:version,"
                    "semantic_route_receipts_hash=:receipt_hash "
                    "WHERE processing_run_id=:run_id"
                )
                for relpath, version, receipt_hash in accepted:
                    conn.execute(
                        update_locator,
                        {
                            "run_id": fixture.processing_run_id,
                            "relpath": relpath,
                            "version": version,
                            "receipt_hash": receipt_hash,
                        },
                    )
                    self.assertEqual(
                        tuple(
                            conn.execute(
                                text(
                                    "SELECT semantic_route_receipts_relpath,"
                                    "semantic_route_receipts_contract_version,"
                                    "semantic_route_receipts_hash FROM "
                                    "disclosure_core.processing_run WHERE "
                                    "processing_run_id=:run_id"
                                ),
                                {"run_id": fixture.processing_run_id},
                            ).one()
                        ),
                        (relpath, version, receipt_hash),
                    )

                rejected_locators = (
                    (None, "semantic_route_receipt.v2", None),
                    ("derived/receipt.jsonl", None, None),
                    (None, "semantic_route_receipt.v2", canonical_hash),
                    ("derived/receipt.jsonl", None, canonical_hash),
                    (
                        "derived/receipt.jsonl",
                        "semantic_route_receipt.v2",
                        None,
                    ),
                    (
                        "derived/receipt.jsonl",
                        "semantic_route_receipt.v1",
                        canonical_hash,
                    ),
                    (
                        "derived/receipt.jsonl",
                        "semantic_route_receipt.v9",
                        canonical_hash,
                    ),
                )
                for relpath, version, receipt_hash in rejected_locators:
                    def update_rejected_locator(
                        relpath: str | None = relpath,
                        version: str | None = version,
                        receipt_hash: str | None = receipt_hash,
                    ) -> None:
                        conn.execute(
                            update_locator,
                            {
                                "run_id": fixture.processing_run_id,
                                "relpath": relpath,
                                "version": version,
                                "receipt_hash": receipt_hash,
                            },
                        )

                    self._assert_rejected(
                        conn,
                        update_rejected_locator,
                        message="ck_processing_run_semantic_receipt_locator",
                    )

                for relpath, version in (
                    (None, None),
                    ("derived/receipt.jsonl", "semantic_route_receipt.v2"),
                ):
                    def update_malformed_hash(
                        relpath: str | None = relpath,
                        version: str | None = version,
                    ) -> None:
                        conn.execute(
                            update_locator,
                            {
                                "run_id": fixture.processing_run_id,
                                "relpath": relpath,
                                "version": version,
                                "receipt_hash": "sha256:invalid",
                            },
                        )

                    self._assert_rejected(
                        conn,
                        update_malformed_hash,
                        message="ck_processing_run_semantic_receipt_hash",
                    )

                def remove_document_units_locator() -> None:
                    conn.execute(
                        text(
                            "UPDATE disclosure_core.processing_run SET "
                            "document_units_relpath=NULL,"
                            "semantic_route_receipts_relpath=NULL,"
                            "semantic_route_receipts_contract_version=NULL,"
                            "semantic_route_receipts_hash=:receipt_hash "
                            "WHERE processing_run_id=:run_id"
                        ),
                        {
                            "run_id": fixture.processing_run_id,
                            "receipt_hash": canonical_hash,
                        },
                    )

                self._assert_rejected(
                    conn,
                    remove_document_units_locator,
                    message="ck_processing_run_semantic_receipt_hash",
                )
            finally:
                transaction.rollback()

    def test_v1_hash_only_semantic_receipt_survives_0057_upgrade(self) -> None:
        self.addCleanup(self._restore_migration_head)
        downgraded = self._alembic(
            "downgrade",
            "0056_staged_credit_evidence",
        )
        self.assertEqual(
            downgraded.returncode,
            0,
            downgraded.stdout + downgraded.stderr,
        )
        fixture = build_v4_authority_fixture()
        canonical_hash = "sha256:" + "b" * 64

        def cleanup_core_fixture() -> None:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.processing_run "
                        "WHERE processing_run_id=:run_id"
                    ),
                    {"run_id": fixture.processing_run_id},
                )
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.document "
                        "WHERE document_id=:document_id"
                    ),
                    {"document_id": fixture.document_id},
                )

        self.addCleanup(cleanup_core_fixture)
        with self.engine.begin() as conn:
            insert_core_rows(conn, fixture)
            conn.execute(
                text(
                    "UPDATE disclosure_core.processing_run SET "
                    "document_units_relpath='derived/document_units.v1.jsonl',"
                    "semantic_route_receipts_hash=:receipt_hash "
                    "WHERE processing_run_id=:run_id"
                ),
                {
                    "run_id": fixture.processing_run_id,
                    "receipt_hash": canonical_hash,
                },
            )

        upgraded = self._alembic("upgrade", "head")
        self.assertEqual(
            upgraded.returncode,
            0,
            upgraded.stdout + upgraded.stderr,
        )
        with self.engine.begin() as conn:
            self.assertEqual(
                tuple(
                    conn.execute(
                        text(
                            "SELECT semantic_route_receipts_relpath,"
                            "semantic_route_receipts_contract_version,"
                            "semantic_route_receipts_hash FROM "
                            "disclosure_core.processing_run WHERE "
                            "processing_run_id=:run_id"
                        ),
                        {"run_id": fixture.processing_run_id},
                    ).one()
                ),
                (None, None, canonical_hash),
            )

    def test_clean_downgrade_round_trip_and_nonempty_guard(self) -> None:
        self.addCleanup(self._restore_migration_head)
        downgraded = self._alembic(
            "downgrade",
            "0056_staged_credit_evidence",
        )
        self.assertEqual(
            downgraded.returncode,
            0,
            downgraded.stdout + downgraded.stderr,
        )
        upgraded = self._alembic("upgrade", "head")
        self.assertEqual(
            upgraded.returncode,
            0,
            upgraded.stdout + upgraded.stderr,
        )

        migration = importlib.import_module(V4_MIGRATION_MODULE)
        downgrade = getattr(migration, "downgrade")
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_prepared_cycle(conn, fixture)
                self._force_constraints(conn)
                self._defer_constraints(conn)
                migration_context = MigrationContext.configure(conn)
                with Operations.context(migration_context):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "0057 downgrade would destroy v4 staged evidence",
                    ):
                        downgrade()
                self.assertEqual(
                    conn.execute(
                        text(
                            "SELECT version_num FROM "
                            "disclosure_ops.alembic_version"
                        )
                    ).scalar_one(),
                    "0057_remote_parse_v4_authority",
                )
                retained_tables = set(
                    conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema='disclosure_ops' AND "
                            "table_name=ANY(:names)"
                        ),
                        {"names": list(V4_TABLES)},
                    ).scalars()
                )
                self.assertEqual(retained_tables, set(V4_TABLES))
            finally:
                transaction.rollback()

    def test_exact_final_purge_closes_secret_history(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                install_submitted_cycle(conn, fixture, include_secret=True)
                self._force_constraints(conn)
                self._defer_constraints(conn)
                append_remote_failed_tail(conn, fixture)
                deleted = conn.execute(
                    text(
                        "SELECT disclosure_ops."
                        "purge_remote_parse_v4_secrets_final("
                        ":attempt_id,:fence,:version,:checkpoint_sha,:revision)"
                    ),
                    {
                        "attempt_id": fixture.attempt_id,
                        "fence": fixture.fence_identity,
                        "version": fixture.remote_failed.lifecycle_version,
                        "checkpoint_sha": fixture.remote_failed.sha256,
                        "revision": 1,
                    },
                ).scalar_one()
                self.assertEqual(deleted, 1)
                self._force_constraints(conn)
                self.assertEqual(
                    conn.execute(
                        text(
                            "SELECT count(*) FROM "
                            "disclosure_ops.remote_parse_v4_secret "
                            "WHERE attempt_id=:attempt_id"
                        ),
                        {"attempt_id": fixture.attempt_id},
                    ).scalar_one(),
                    0,
                )
                final = conn.execute(
                    text(
                        "SELECT state,is_current,row_version,"
                        "current_checkpoint_sha256 FROM "
                        "disclosure_ops.remote_parse_attempt "
                        "WHERE attempt_id=:attempt_id"
                    ),
                    {"attempt_id": fixture.attempt_id},
                ).one()
                self.assertEqual(
                    tuple(final),
                    (
                        "remote_failed",
                        False,
                        fixture.remote_failed.lifecycle_version,
                        fixture.remote_failed.sha256,
                    ),
                )
            finally:
                transaction.rollback()

    def test_catalog_and_acl_expose_only_the_v4_application_surface(self) -> None:
        with self.engine.connect() as conn:
            table_names = set(
                conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='disclosure_ops' AND "
                        "table_name=ANY(:names)"
                    ),
                    {"names": list(V4_TABLES)},
                ).scalars()
            )
            self.assertEqual(table_names, set(V4_TABLES))

            checkpoint_columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='disclosure_ops' AND "
                        "table_name='remote_parse_v4_checkpoint'"
                    )
                ).scalars()
            )
            self.assertLessEqual(
                {
                    "attempt_id",
                    "fence_identity",
                    "state",
                    "lifecycle_version",
                    "previous_checkpoint_sha256",
                    "checkpoint_sha256",
                    "checkpoint_bytes",
                    "resource_reservation_sha256",
                    "source_byte_count",
                    "source_page_count",
                    *(f"held_{name}" for name in HELD_CREDIT_NAMES),
                    *EVIDENCE_FIELD_NAMES,
                    "publication_winner_sha256",
                },
                checkpoint_columns,
            )
            attempt_columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='disclosure_ops' AND "
                        "table_name='remote_parse_attempt'"
                    )
                ).scalars()
            )
            self.assertIn("current_checkpoint_sha256", attempt_columns)

            critical_constraints = set(
                conn.execute(
                    text("SELECT conname FROM pg_constraint WHERE conname=ANY(:names)"),
                    {
                        "names": [
                            "fk_remote_parse_attempt_v4_current_checkpoint",
                            "fk_remote_parse_v4_checkpoint_predecessor",
                            "fk_remote_parse_v4_secret_accepted_evidence",
                            "uq_remote_parse_attempt_v4_parent_identity",
                            "ck_remote_parse_v4_checkpoint_state_evidence",
                            "ck_remote_parse_v4_checkpoint_credit_shape",
                        ]
                    },
                ).scalars()
            )
            self.assertEqual(
                critical_constraints,
                {
                    "fk_remote_parse_attempt_v4_current_checkpoint",
                    "fk_remote_parse_v4_checkpoint_predecessor",
                    "fk_remote_parse_v4_secret_accepted_evidence",
                    "uq_remote_parse_attempt_v4_parent_identity",
                    "ck_remote_parse_v4_checkpoint_state_evidence",
                    "ck_remote_parse_v4_checkpoint_credit_shape",
                },
            )
            head_fk = conn.execute(
                text(
                    "SELECT condeferrable,condeferred FROM pg_constraint "
                    "WHERE conname='fk_remote_parse_attempt_v4_current_checkpoint'"
                )
            ).one()
            self.assertEqual(tuple(head_fk), (True, True))

            for table_name in V4_TABLES:
                relation = "disclosure_ops." + table_name
                for privilege in ("SELECT", "INSERT"):
                    self.assertTrue(
                        conn.execute(
                            text(
                                "SELECT has_table_privilege(:role,:relation,:privilege)"
                            ),
                            {
                                "role": APP_ROLE,
                                "relation": relation,
                                "privilege": privilege,
                            },
                        ).scalar_one(),
                        (table_name, privilege),
                    )
                for privilege in ("UPDATE", "DELETE"):
                    self.assertFalse(
                        conn.execute(
                            text(
                                "SELECT has_table_privilege(:role,:relation,:privilege)"
                            ),
                            {
                                "role": APP_ROLE,
                                "relation": relation,
                                "privilege": privilege,
                            },
                        ).scalar_one(),
                        (table_name, privilege),
                    )
                for role in (READER_ROLE, FUTURE_L2_READER_ROLE):
                    self.assertFalse(
                        conn.execute(
                            text(
                                "SELECT has_table_privilege(:role,:relation,'SELECT')"
                            ),
                            {"role": role, "relation": relation},
                        ).scalar_one(),
                        (role, table_name),
                    )

            self.assertTrue(
                conn.execute(
                    text("SELECT has_function_privilege(:role,:function,'EXECUTE')"),
                    {"role": APP_ROLE, "function": PURGE_FUNCTION},
                ).scalar_one()
            )
            for role in (READER_ROLE, FUTURE_L2_READER_ROLE):
                self.assertFalse(
                    conn.execute(
                        text(
                            "SELECT has_function_privilege(:role,:function,'EXECUTE')"
                        ),
                        {"role": role, "function": PURGE_FUNCTION},
                    ).scalar_one(),
                    role,
                )
            function_shape = conn.execute(
                text(
                    "SELECT p.prosecdef,p.proconfig FROM pg_proc p JOIN "
                    "pg_namespace n ON n.oid=p.pronamespace WHERE "
                    "n.nspname='disclosure_ops' AND "
                    "p.proname='purge_remote_parse_v4_secrets_final'"
                )
            ).one()
            self.assertTrue(function_shape.prosecdef)
            self.assertIn("search_path=pg_catalog", function_shape.proconfig)


if __name__ == "__main__":
    unittest.main()
