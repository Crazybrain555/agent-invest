"""Scratch-PostgreSQL tests for the exact V4 repository/UOW authority."""

from __future__ import annotations

import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import (
    RemoteParseV4Repository,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from disclosure_anchor.application.contracts.provider_secret_envelope_v4 import (
    SealedProviderSecretV4,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    EvidenceValueV4,
    FailureReceiptV4,
    SubmissionIntentV4,
    SupersessionReceiptV4,
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    RemoteParseCheckpointV4,
    advance_remote_parse_checkpoint_v4,
    build_initial_remote_parse_checkpoint_v4,
    build_local_cleanup_plan_v4,
    build_local_cleanup_receipt_v4,
    build_resource_free_remote_parse_checkpoint_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationWinnerV4,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    LegacyCurrentRemoteParseAuthority,
    RemoteParseV4Authority,
    RemoteParseV4AuthorityViolation,
    V4AttemptFinal,
    V4ClaimWitness,
    V4ClaimGenerationExhausted,
    V4ClaimHeldByOther,
    V4ClaimLost,
    V4DifferentSuccessorCommitted,
    V4DocumentCurrentConflict,
    V4GenerationConflict,
    V4HeadExpectation,
    V4HeadStale,
    V4PreparedCreation,
    V4ResourceFreeFailureCreation,
    V4ResourceFreeSupersessionCreation,
    V4SecretRevisionConflict,
    V4SecretRewrap,
    V4SuccessorAppend,
    V4SuccessorNotCommitted,
)
from disclosure_anchor.domain import ids
from tests.integration._remote_parse_v4_factory import (
    V4AuthorityFixture,
    V4SupersessionStageFixture,
    build_v4_authority_fixture,
    build_v4_resource_free_supersession_fixture,
    build_v4_supersession_stage_fixture,
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
    install_submitted_cycle,
    install_success_ack_pending_cycle,
    install_v4_resource_free_supersession,
    install_v4_supersession_stage,
    insert_v4_supersession_link,
    sha256_bytes,
    update_v4_head,
)
from tests.integration._support import engine_or_skip


class RemoteParseV4RepositoryIntegrationTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        engine = engine_or_skip()
        try:
            with engine.begin() as conn:
                fixture_roots = tuple(
                    conn.execute(
                        sa.text(
                            "SELECT DISTINCT processing_run_id,document_id "
                            "FROM disclosure_core.processing_run "
                            "WHERE provider_document_relpath="
                            "'scratch/provider.json'"
                        )
                    ).mappings()
                )
                conn.exec_driver_sql(
                    "TRUNCATE TABLE "
                    "disclosure_ops.remote_parse_attempt CASCADE"
                )
                for root in fixture_roots:
                    conn.execute(
                        sa.text(
                            "DELETE FROM disclosure_core.processing_run "
                            "WHERE processing_run_id=:processing_run_id"
                        ),
                        {"processing_run_id": root["processing_run_id"]},
                    )
                for root in fixture_roots:
                    conn.execute(
                        sa.text(
                            "DELETE FROM disclosure_core.document "
                            "WHERE document_id=:document_id"
                        ),
                        {"document_id": root["document_id"]},
                    )
        finally:
            engine.dispose()

    def setUp(self) -> None:
        self.engine = engine_or_skip()

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _repository(conn: Connection) -> tuple[Session, RemoteParseV4Repository]:
        session = Session(bind=conn, expire_on_commit=False, future=True)
        return session, RemoteParseV4Repository(session)

    @staticmethod
    def _prepared_creation(fixture: V4AuthorityFixture) -> V4PreparedCreation:
        return V4PreparedCreation(
            checkpoint=fixture.prepared,
            reservation=fixture.reservation,
            preparation_intent=fixture.preparation,
            snapshot_receipt=fixture.snapshot,
            parser_target_sha256=fixture.parser_target_sha256,
            client_submit_key=fixture.client_submit_key,
        )

    @staticmethod
    def _successor_append(
        authority: RemoteParseV4Authority,
        checkpoint: RemoteParseCheckpointV4,
        *evidence: EvidenceValueV4,
        sealed_secret: SealedProviderSecretV4 | None = None,
        publication_winner: AtomicPublicationWinnerV4 | None = None,
    ) -> V4SuccessorAppend:
        return V4SuccessorAppend(
            claim=authority.claim_witness,
            successor=checkpoint,
            new_evidence=tuple(
                encode_remote_parse_evidence_v4(item) for item in evidence
            ),
            sealed_secret=sealed_secret,
            publication_winner=publication_winner,
        )

    @staticmethod
    def _insert_superseding_prepared_head(
        conn: Connection,
        stage: V4SupersessionStageFixture,
        *,
        is_current: bool,
        head_overrides: dict[str, object] | None = None,
    ) -> None:
        values: dict[str, object] = {
            "attempt_id": stage.attempt_id,
            "run_id": stage.reservation.processing_run_id,
            "document_id": stage.reservation.document_id,
            "generation": stage.reservation.attempt_generation,
            "fence": stage.fence_identity,
            "source_sha": stage.reservation.source_pdf_sha256,
            "target_sha": stage.parser_target_sha256,
            "request_sha": stage.request_sha256,
            "epoch_sha": stage.runtime_epoch_sha256,
            "submit_key": stage.client_submit_key,
            "is_current": is_current,
            "checkpoint_sha": stage.prepared.sha256,
        }
        values.update(head_overrides or {})
        conn.execute(
            sa.text(
                "INSERT INTO disclosure_ops.remote_parse_attempt "
                "(attempt_id,processing_run_id,document_id,attempt_generation,"
                "fence_identity,source_pdf_sha256,parser_target_sha256,"
                "request_sha256,runtime_epoch_sha256,client_submit_key,"
                "checkpoint_contract_version,state,is_current,row_version,"
                "current_checkpoint_sha256,claim_generation,"
                "claim_owner_identity,claim_lease_until) VALUES "
                "(:attempt_id,:run_id,:document_id,:generation,:fence,"
                ":source_sha,:target_sha,:request_sha,:epoch_sha,:submit_key,"
                "4,'prepared',:is_current,0,:checkpoint_sha,0,NULL,NULL)"
            ),
            values,
        )
        for evidence in stage.prepared_evidence:
            insert_evidence(conn, stage, evidence)
        insert_checkpoint(conn, stage, stage.prepared)

    def test_strict_reload_closes_prepared_submitted_and_acked_authority(
        self,
    ) -> None:
        prepared = build_v4_authority_fixture()
        submitted = build_v4_authority_fixture()
        acked = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, prepared)
            install_submitted_cycle(conn, submitted, include_secret=True)
            install_acked_cycle(conn, acked)
            session, repository = self._repository(conn)
            try:
                prepared_authority = repository.load(prepared.attempt_id)
                submitted_authority = repository.load(submitted.attempt_id)
                acked_authority = repository.load(acked.attempt_id)
            finally:
                session.close()

        self.assertEqual(prepared_authority.state, "prepared")
        self.assertEqual(len(prepared_authority.checkpoint_history), 1)
        self.assertIsNotNone(prepared_authority.reservation)
        self.assertEqual(submitted_authority.state, "submitted")
        self.assertEqual(len(submitted_authority.secret_history), 1)
        self.assertEqual(acked_authority.state, "acked")
        self.assertIsNotNone(acked_authority.publication_winner)
        self.assertEqual(acked_authority.secret_history, ())

    def test_legacy_current_is_loaded_truthfully_and_blocks_v4_create(self) -> None:
        legacy = build_v4_authority_fixture()
        successor = build_v4_supersession_stage_fixture(legacy)
        creation = V4PreparedCreation(
            checkpoint=successor.prepared,
            reservation=successor.reservation,
            preparation_intent=successor.preparation,
            snapshot_receipt=successor.snapshot,
            parser_target_sha256=successor.parser_target_sha256,
            client_submit_key=successor.client_submit_key,
        )
        with self.engine.begin() as conn:
            insert_core_rows(conn, legacy)
            insert_legacy_head(conn, legacy)
            session, repository = self._repository(conn)
            try:
                current = repository.load_current_for_document(legacy.document_id)
                with self.assertRaises(V4DocumentCurrentConflict):
                    repository.create_prepared(creation)
            finally:
                session.close()

        self.assertIsInstance(current, LegacyCurrentRemoteParseAuthority)
        assert isinstance(current, LegacyCurrentRemoteParseAuthority)
        self.assertEqual(current.attempt_id, legacy.attempt_id)
        self.assertEqual(current.checkpoint_contract_version, 1)
        self.assertEqual(current.state, "prepared")

    def test_current_lookup_returns_none_or_exact_v4_authority(self) -> None:
        empty = build_v4_authority_fixture()
        current_fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            insert_core_rows(conn, empty)
            insert_core_rows(conn, current_fixture)
            session, repository = self._repository(conn)
            try:
                absent = repository.load_current_for_document(empty.document_id)
                created = repository.create_prepared(
                    self._prepared_creation(current_fixture)
                )
                current = repository.load_current_for_document(
                    current_fixture.document_id
                )
            finally:
                session.close()

        self.assertIsNone(absent)
        self.assertEqual(current, created)

    def test_prepared_without_snapshot_receipt_round_trips_truthfully(self) -> None:
        fixture = build_v4_authority_fixture()
        checkpoint = build_initial_remote_parse_checkpoint_v4(
            reservation=fixture.reservation,
            preparation_intent_sha256=fixture.preparation.sha256,
            snapshot_receipt_sha256=None,
            held_resource_credit=ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=fixture.reservation.source_byte_count,
            ),
        )
        creation = V4PreparedCreation(
            checkpoint=checkpoint,
            reservation=fixture.reservation,
            preparation_intent=fixture.preparation,
            snapshot_receipt=None,
            parser_target_sha256=fixture.parser_target_sha256,
            client_submit_key=fixture.client_submit_key,
        )
        with self.engine.begin() as conn:
            insert_core_rows(conn, fixture)
            session, repository = self._repository(conn)
            try:
                created = repository.create_prepared(creation)
                replayed = repository.create_prepared(creation)
                reloaded = repository.load(fixture.attempt_id)
                claimed = repository.claim(
                    V4HeadExpectation.from_authority(reloaded),
                    owner_identity="worker-delayed-snapshot",
                    lease_seconds=120,
                )
                reconciling = advance_remote_parse_checkpoint_v4(
                    checkpoint,
                    state="reconciling",
                    held_resource_credit=replace(
                        checkpoint.held_resource_credit,
                        remote_waits=1,
                    ),
                    snapshot_receipt_sha256=fixture.snapshot.sha256,
                    submission_intent_sha256=fixture.submission.sha256,
                )
                progressed = repository.append_successor(
                    self._successor_append(
                        claimed,
                        reconciling,
                        fixture.snapshot,
                        fixture.submission,
                    )
                )
            finally:
                session.close()

        self.assertEqual(created, replayed)
        self.assertEqual(reloaded, created)
        self.assertEqual(reloaded.checkpoint.snapshot_receipt_sha256, None)
        self.assertEqual(
            tuple(item.kind for item in reloaded.evidence),
            ("preparation_intent",),
        )
        self.assertEqual(progressed.state, "reconciling")
        self.assertEqual(
            tuple(item.kind for item in progressed.evidence),
            ("preparation_intent", "snapshot_receipt", "submission_intent"),
        )

    def test_strict_reload_rejects_head_projection_drift(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            insert_core_rows(conn, fixture)
            insert_v4_head(
                conn,
                fixture,
                fixture.prepared,
                parser_target_sha256_override=sha256_bytes(
                    b"different-parser-target"
                ),
            )
            for evidence in fixture.prepared_evidence:
                insert_evidence(conn, fixture, evidence)
            insert_checkpoint(conn, fixture, fixture.prepared)
            session, repository = self._repository(conn)
            try:
                with self.assertRaisesRegex(
                    RemoteParseV4AuthorityViolation,
                    "parser target",
                ):
                    repository.load(fixture.attempt_id)
            finally:
                session.close()

    def test_strict_head_projection_independently_closes_claim_shapes(self) -> None:
        prepared = build_v4_authority_fixture()
        submitted = build_v4_authority_fixture()
        acked = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, prepared)
            install_submitted_cycle(conn, submitted, include_secret=True)
            install_acked_cycle(conn, acked)
            rows = {
                row["attempt_id"]: dict(row)
                for row in conn.execute(
                    sa.text(
                        "SELECT * FROM disclosure_ops.remote_parse_attempt "
                        "WHERE attempt_id IN (:prepared,:submitted,:acked)"
                    ),
                    {
                        "prepared": prepared.attempt_id,
                        "submitted": submitted.attempt_id,
                        "acked": acked.attempt_id,
                    },
                ).mappings()
            }

        drifted = (
            (
                {
                    **rows[prepared.attempt_id],
                    "claim_generation": 1,
                },
                prepared.prepared,
            ),
            (
                {
                    **rows[prepared.attempt_id],
                    "is_current": False,
                    "claim_generation": 1,
                    "claim_owner_identity": "staged-owner",
                    "claim_lease_until": datetime.now(UTC),
                },
                prepared.prepared,
            ),
            (
                {
                    **rows[submitted.attempt_id],
                    "claim_generation": 0,
                    "claim_owner_identity": None,
                    "claim_lease_until": None,
                },
                submitted.submitted,
            ),
            (
                {
                    **rows[submitted.attempt_id],
                    "claim_owner_identity": "   ",
                },
                submitted.submitted,
            ),
            (
                {
                    **rows[acked.attempt_id],
                    "claim_generation": 0,
                },
                acked.acked,
            ),
        )
        for head, checkpoint in drifted:
            with self.subTest(
                state=checkpoint.state,
                current=head["is_current"],
                generation=head["claim_generation"],
            ), self.assertRaises(ValueError):
                RemoteParseV4Repository._validate_head_projection(
                    head,
                    checkpoint,
                )

    def test_strict_reload_rejects_receipt_link_target_drift(self) -> None:
        source = build_v4_authority_fixture()
        fixture = build_v4_resource_free_supersession_fixture(source)
        forged_receipt = SupersessionReceiptV4(
            attempt_id=source.attempt_id,
            fence_identity=source.fence_identity,
            source_document_id=source.document_id,
            source_attempt_generation=source.reservation.attempt_generation,
            source_state="not_prepared",
            source_lifecycle_version=0,
            source_checkpoint_sha256=None,
            superseding_attempt_id="rpa_forged_target",
            superseding_attempt_generation=fixture.target.reservation.attempt_generation,
            superseding_document_id=source.document_id,
            superseding_checkpoint_sha256=sha256_bytes(b"forged-target-h0"),
            reason_code="newer_attempt",
        )
        forged_checkpoint = build_resource_free_remote_parse_checkpoint_v4(
            state="superseded",
            attempt_id=source.attempt_id,
            attempt_generation=source.reservation.attempt_generation,
            fence_identity=source.fence_identity,
            document_id=source.document_id,
            processing_run_id=source.processing_run_id,
            source_pdf_sha256=source.source_pdf_sha256,
            source_byte_count=source.reservation.source_byte_count,
            source_page_count=source.reservation.source_page_count,
            request_sha256=source.request_sha256,
            runtime_epoch_sha256=source.runtime_epoch_sha256,
            process_profile_sha256=source.process_profile_sha256,
            credit_policy_sha256=source.credit_policy_sha256,
            reservation_input_sha256=source.reservation_input.sha256,
            supersession_receipt_sha256=forged_receipt.sha256,
        )
        forged = replace(
            fixture,
            supersession=forged_receipt,
            source_superseded=forged_checkpoint,
        )
        with self.engine.begin() as conn:
            install_v4_resource_free_supersession(conn, forged)
            conn.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
            session, repository = self._repository(conn)
            try:
                with self.assertRaisesRegex(
                    RemoteParseV4AuthorityViolation,
                    "supersession receipt",
                ):
                    repository.load(forged.target.attempt_id)
            finally:
                session.close()

    def test_source_reload_rejects_linked_target_head_h0_projection_drift(
        self,
    ) -> None:
        source = build_v4_authority_fixture()
        pair = build_v4_resource_free_supersession_fixture(source)
        with self.engine.begin() as conn:
            insert_core_rows(conn, source)
            insert_v4_head(conn, source, pair.source_superseded)
            insert_evidence(conn, source, pair.supersession)
            insert_checkpoint(conn, source, pair.source_superseded)
            self._insert_superseding_prepared_head(
                conn,
                pair.target,
                is_current=True,
                head_overrides={
                    "generation": pair.target.reservation.attempt_generation + 1,
                },
            )
            insert_v4_supersession_link(
                conn,
                pair.target,
                source_receipt_sha256=pair.supersession.sha256,
            )
            conn.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
            session, repository = self._repository(conn)
            try:
                with self.assertRaisesRegex(
                    RemoteParseV4AuthorityViolation,
                    "supersession target head",
                ):
                    repository.load(source.attempt_id)
            finally:
                session.close()

    def test_superseding_target_projection_closes_every_immutable_h0_field(
        self,
    ) -> None:
        source = build_v4_authority_fixture()
        stage = build_v4_supersession_stage_fixture(source)
        head: dict[str, object] = {
            "attempt_id": stage.attempt_id,
            "processing_run_id": stage.reservation.processing_run_id,
            "document_id": stage.reservation.document_id,
            "attempt_generation": stage.reservation.attempt_generation,
            "fence_identity": stage.fence_identity,
            "source_pdf_sha256": stage.reservation.source_pdf_sha256,
            "parser_target_sha256": stage.parser_target_sha256,
            "request_sha256": stage.request_sha256,
            "runtime_epoch_sha256": stage.runtime_epoch_sha256,
        }
        drifts: dict[str, object] = {
            "attempt_id": "rpa_drifted_target",
            "processing_run_id": "run_drifted_target",
            "document_id": "doc_drifted_target",
            "attempt_generation": stage.reservation.attempt_generation + 1,
            "fence_identity": "fence-drifted-target",
            "source_pdf_sha256": sha256_bytes(b"drifted-source"),
            "parser_target_sha256": sha256_bytes(b"drifted-parser-target"),
            "request_sha256": sha256_bytes(b"drifted-request"),
            "runtime_epoch_sha256": sha256_bytes(b"drifted-runtime-epoch"),
        }
        RemoteParseV4Repository._validate_superseding_target_head_projection(
            head,
            stage.prepared,
            stage.preparation,
        )
        for field, drift in drifts.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                RemoteParseV4Repository._validate_superseding_target_head_projection(
                    {**head, field: drift},
                    stage.prepared,
                    stage.preparation,
                )

    def test_source_final_append_rolls_back_contradictory_target_activation(
        self,
    ) -> None:
        source = build_v4_authority_fixture()
        stage = build_v4_supersession_stage_fixture(source)
        unrelated = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            insert_core_rows(conn, source)
            insert_core_rows(conn, unrelated)
            insert_v4_head(conn, source, source.prepared)
            for evidence in (*source.prepared_evidence, source.submission):
                insert_evidence(conn, source, evidence)
            insert_checkpoint(conn, source, source.prepared)
            insert_checkpoint(conn, source, source.reconciling)
            update_v4_head(conn, source, source.reconciling)
            insert_evidence(conn, source, stage.supersession)
            insert_evidence(conn, source, stage.cleanup_plan)
            insert_checkpoint(conn, source, stage.source_cleanup_pending)
            update_v4_head(conn, source, stage.source_cleanup_pending)
            self._insert_superseding_prepared_head(
                conn,
                stage,
                is_current=False,
                head_overrides={
                    "generation": stage.reservation.attempt_generation + 1,
                },
            )
            insert_v4_supersession_link(conn, stage)
            conn.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
            session, repository = self._repository(conn)
            try:
                final_append = V4SuccessorAppend(
                    claim=V4ClaimWitness(
                        attempt_id=source.attempt_id,
                        fence_identity=source.fence_identity,
                        state=stage.source_cleanup_pending.state,
                        lifecycle_version=(
                            stage.source_cleanup_pending.lifecycle_version
                        ),
                        checkpoint_sha256=stage.source_cleanup_pending.sha256,
                        claim_owner_identity="worker-test",
                        claim_generation=1,
                    ),
                    successor=stage.source_superseded,
                    new_evidence=(
                        encode_remote_parse_evidence_v4(stage.cleanup_receipt),
                    ),
                )
                with self.assertRaisesRegex(
                    RemoteParseV4AuthorityViolation,
                    "supersession target head",
                ):
                    repository.append_successor(final_append)
                heads = {
                    row["attempt_id"]: dict(row)
                    for row in session.execute(
                        sa.text(
                            "SELECT attempt_id,state,is_current "
                            "FROM disclosure_ops.remote_parse_attempt "
                            "WHERE attempt_id IN (:source,:target)"
                        ),
                        {
                            "source": source.attempt_id,
                            "target": stage.attempt_id,
                        },
                    ).mappings()
                }
                self.assertEqual(
                    heads[source.attempt_id],
                    {
                        "attempt_id": source.attempt_id,
                        "state": "cleanup_pending",
                        "is_current": True,
                    },
                )
                self.assertEqual(
                    heads[stage.attempt_id],
                    {
                        "attempt_id": stage.attempt_id,
                        "state": "prepared",
                        "is_current": False,
                    },
                )
                self.assertEqual(
                    session.execute(sa.text("SELECT 1")).scalar_one(),
                    1,
                )
                residue = session.execute(
                    sa.text(
                        "SELECT "
                        "(SELECT count(*) FROM "
                        " disclosure_ops.remote_parse_v4_checkpoint "
                        " WHERE attempt_id=:attempt_id "
                        " AND lifecycle_version=:final_version) AS checkpoints,"
                        "(SELECT count(*) FROM "
                        " disclosure_ops.remote_parse_v4_evidence "
                        " WHERE attempt_id=:attempt_id "
                        " AND evidence_kind='cleanup_receipt') AS receipts"
                    ),
                    {
                        "attempt_id": source.attempt_id,
                        "final_version": stage.source_superseded.lifecycle_version,
                    },
                ).mappings().one()
                self.assertEqual(dict(residue), {"checkpoints": 0, "receipts": 0})
                session.execute(sa.text("SET CONSTRAINTS ALL DEFERRED"))
                unrelated_created = repository.create_prepared(
                    self._prepared_creation(unrelated)
                )
                self.assertEqual(unrelated_created.attempt_id, unrelated.attempt_id)
            finally:
                session.close()
        with self.engine.begin() as conn:
            persisted = {
                row["attempt_id"]: dict(row)
                for row in conn.execute(
                    sa.text(
                        "SELECT attempt_id,state,is_current "
                        "FROM disclosure_ops.remote_parse_attempt "
                        "WHERE attempt_id IN (:source,:target,:unrelated)"
                    ),
                    {
                        "source": source.attempt_id,
                        "target": stage.attempt_id,
                        "unrelated": unrelated.attempt_id,
                    },
                ).mappings()
            }
        self.assertEqual(persisted[source.attempt_id]["state"], "cleanup_pending")
        self.assertTrue(persisted[source.attempt_id]["is_current"])
        self.assertEqual(persisted[stage.attempt_id]["state"], "prepared")
        self.assertFalse(persisted[stage.attempt_id]["is_current"])
        self.assertEqual(persisted[unrelated.attempt_id]["state"], "prepared")
        self.assertTrue(persisted[unrelated.attempt_id]["is_current"])

    def test_target_reload_rejects_linked_source_lifecycle_witness_drift(
        self,
    ) -> None:
        source = build_v4_authority_fixture()
        pair = build_v4_resource_free_supersession_fixture(source)
        forged_receipt = replace(
            pair.supersession,
            source_state="prepared",
            source_checkpoint_sha256=sha256_bytes(b"forged-source-checkpoint"),
        )
        forged_source = build_resource_free_remote_parse_checkpoint_v4(
            state="superseded",
            attempt_id=source.attempt_id,
            attempt_generation=source.reservation.attempt_generation,
            fence_identity=source.fence_identity,
            document_id=source.document_id,
            processing_run_id=source.processing_run_id,
            source_pdf_sha256=source.source_pdf_sha256,
            source_byte_count=source.reservation.source_byte_count,
            source_page_count=source.reservation.source_page_count,
            request_sha256=source.request_sha256,
            runtime_epoch_sha256=source.runtime_epoch_sha256,
            process_profile_sha256=source.process_profile_sha256,
            credit_policy_sha256=source.credit_policy_sha256,
            reservation_input_sha256=source.reservation_input.sha256,
            supersession_receipt_sha256=forged_receipt.sha256,
        )
        forged_pair = replace(
            pair,
            supersession=forged_receipt,
            source_superseded=forged_source,
        )
        with self.engine.begin() as conn:
            install_v4_resource_free_supersession(conn, forged_pair)
            conn.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
            session, repository = self._repository(conn)
            try:
                with self.assertRaisesRegex(
                    RemoteParseV4AuthorityViolation,
                    "source lifecycle",
                ):
                    repository.load(pair.target.attempt_id)
                with self.assertRaisesRegex(
                    RemoteParseV4AuthorityViolation,
                    "source lifecycle",
                ):
                    repository.claim(
                        V4HeadExpectation(
                            attempt_id=pair.target.attempt_id,
                            fence_identity=pair.target.fence_identity,
                            state="prepared",
                            lifecycle_version=0,
                            checkpoint_sha256=pair.target.prepared.sha256,
                        ),
                        owner_identity="worker-forged-resource-free",
                        lease_seconds=30,
                    )
            finally:
                session.close()

    def test_source_witness_projection_closes_resource_free_and_resourceful(
        self,
    ) -> None:
        source = build_v4_authority_fixture()
        resource_free = build_v4_resource_free_supersession_fixture(source)
        resource_free_head: dict[str, object] = {
            "state": "superseded",
            "row_version": 0,
        }
        RemoteParseV4Repository._validate_supersession_source_witness_projection(
            source_head=resource_free_head,
            receipt=resource_free.supersession,
            checkpoint=None,
        )
        resource_free_drifts = (
            replace(
                resource_free.supersession,
                source_state="prepared",
                source_checkpoint_sha256=sha256_bytes(b"forged-source-h0"),
            ),
            replace(
                resource_free.supersession,
                source_checkpoint_sha256=sha256_bytes(b"forged-source-h0"),
            ),
            replace(
                resource_free.supersession,
                source_lifecycle_version=1,
            ),
        )
        for receipt in resource_free_drifts:
            with self.subTest(
                source_state=receipt.source_state,
                version=receipt.source_lifecycle_version,
                has_hash=receipt.source_checkpoint_sha256 is not None,
            ), self.assertRaises(RemoteParseV4AuthorityViolation):
                RemoteParseV4Repository._validate_supersession_source_witness_projection(
                    source_head=resource_free_head,
                    receipt=receipt,
                    checkpoint=None,
                )

        stage = build_v4_supersession_stage_fixture(source)
        resourceful_head: dict[str, object] = {
            "attempt_id": source.attempt_id,
            "processing_run_id": source.processing_run_id,
            "document_id": source.document_id,
            "attempt_generation": source.reservation.attempt_generation,
            "fence_identity": source.fence_identity,
            "source_pdf_sha256": source.source_pdf_sha256,
            "request_sha256": source.request_sha256,
            "runtime_epoch_sha256": source.runtime_epoch_sha256,
            "state": stage.source_cleanup_pending.state,
            "row_version": stage.source_cleanup_pending.lifecycle_version,
        }
        RemoteParseV4Repository._validate_supersession_source_witness_projection(
            source_head=resourceful_head,
            receipt=stage.supersession,
            checkpoint=source.reconciling,
            cleanup_pending=stage.source_cleanup_pending,
            cleanup_plan=stage.cleanup_plan,
        )
        with self.assertRaises(RemoteParseV4AuthorityViolation):
            RemoteParseV4Repository._validate_supersession_source_witness_projection(
                source_head=resourceful_head,
                receipt=stage.supersession,
                checkpoint=None,
            )
        with self.assertRaises(RemoteParseV4AuthorityViolation):
            RemoteParseV4Repository._validate_supersession_source_witness_projection(
                source_head=resourceful_head,
                receipt=replace(stage.supersession, source_state="submitted"),
                checkpoint=source.reconciling,
            )
        with self.assertRaises(RemoteParseV4AuthorityViolation):
            RemoteParseV4Repository._validate_supersession_source_witness_projection(
                source_head=resourceful_head,
                receipt=replace(
                    stage.supersession,
                    source_state=stage.source_cleanup_pending.state,
                    source_lifecycle_version=(
                        stage.source_cleanup_pending.lifecycle_version
                    ),
                    source_checkpoint_sha256=stage.source_cleanup_pending.sha256,
                ),
                checkpoint=stage.source_cleanup_pending,
            )

    def test_target_reload_and_claim_reject_resourceful_source_witness_drift(
        self,
    ) -> None:
        source = build_v4_authority_fixture()
        stage = build_v4_supersession_stage_fixture(source)
        forged_receipt = replace(stage.supersession, source_state="submitted")
        forged_plan = build_local_cleanup_plan_v4(
            reservation=source.reservation,
            source_checkpoint=source.reconciling,
            outcome="superseded",
            supersession_receipt_sha256=forged_receipt.sha256,
            resources=stage.cleanup_plan.resources,
        )
        forged_pending = advance_remote_parse_checkpoint_v4(
            source.reconciling,
            state="cleanup_pending",
            held_resource_credit=source.reconciling.held_resource_credit,
            supersession_receipt_sha256=forged_receipt.sha256,
            cleanup_plan_sha256=forged_plan.sha256,
        )
        forged_cleanup = build_local_cleanup_receipt_v4(
            plan=forged_plan,
            cleanup_pending_checkpoint=forged_pending,
            results=stage.cleanup_receipt.results,
        )
        forged_final = advance_remote_parse_checkpoint_v4(
            forged_pending,
            state="superseded",
            held_resource_credit=ResourceCreditVector(),
            cleanup_receipt_sha256=forged_cleanup.sha256,
        )
        forged_stage = replace(
            stage,
            supersession=forged_receipt,
            cleanup_plan=forged_plan,
            source_cleanup_pending=forged_pending,
            cleanup_receipt=forged_cleanup,
            source_superseded=forged_final,
        )
        with self.engine.begin() as conn:
            install_v4_supersession_stage(conn, forged_stage)
            insert_evidence(conn, source, forged_cleanup)
            insert_checkpoint(conn, source, forged_final)
            update_v4_head(conn, source, forged_final)
            conn.execute(
                sa.text(
                    "UPDATE disclosure_ops.remote_parse_attempt "
                    "SET is_current=true "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": stage.attempt_id},
            )
            conn.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
            session, repository = self._repository(conn)
            try:
                expectation = V4HeadExpectation(
                    attempt_id=stage.attempt_id,
                    fence_identity=stage.fence_identity,
                    state="prepared",
                    lifecycle_version=0,
                    checkpoint_sha256=stage.prepared.sha256,
                )
                with self.assertRaisesRegex(
                    RemoteParseV4AuthorityViolation,
                    "source lifecycle",
                ):
                    repository.load(stage.attempt_id)
                with self.assertRaisesRegex(
                    RemoteParseV4AuthorityViolation,
                    "source lifecycle",
                ):
                    repository.claim(
                        expectation,
                        owner_identity="worker-forged-resourceful",
                        lease_seconds=30,
                    )
            finally:
                session.close()

    def test_linked_reload_rejects_coherent_wrong_cleanup_predecessor(
        self,
    ) -> None:
        for plan_source_name in ("reconciling", "prepared"):
            with self.subTest(plan_source=plan_source_name):
                source = build_v4_authority_fixture()
                stage = build_v4_supersession_stage_fixture(source)
                forged_receipt = replace(
                    stage.supersession,
                    source_state=source.prepared.state,
                    source_lifecycle_version=source.prepared.lifecycle_version,
                    source_checkpoint_sha256=source.prepared.sha256,
                )
                plan_source = getattr(source, plan_source_name)
                forged_plan = build_local_cleanup_plan_v4(
                    reservation=source.reservation,
                    source_checkpoint=plan_source,
                    outcome="superseded",
                    supersession_receipt_sha256=forged_receipt.sha256,
                    resources=stage.cleanup_plan.resources,
                )
                forged_pending = advance_remote_parse_checkpoint_v4(
                    source.reconciling,
                    state="cleanup_pending",
                    held_resource_credit=source.reconciling.held_resource_credit,
                    supersession_receipt_sha256=forged_receipt.sha256,
                    cleanup_plan_sha256=forged_plan.sha256,
                )
                forged_cleanup = (
                    build_local_cleanup_receipt_v4(
                        plan=forged_plan,
                        cleanup_pending_checkpoint=forged_pending,
                        results=stage.cleanup_receipt.results,
                    )
                    if plan_source_name == "reconciling"
                    else replace(
                        stage.cleanup_receipt,
                        cleanup_plan_sha256=forged_plan.sha256,
                        cleanup_pending_checkpoint_sha256=forged_pending.sha256,
                        cleanup_pending_lifecycle_version=(
                            forged_pending.lifecycle_version
                        ),
                    )
                )
                forged_final = advance_remote_parse_checkpoint_v4(
                    forged_pending,
                    state="superseded",
                    held_resource_credit=ResourceCreditVector(),
                    cleanup_receipt_sha256=forged_cleanup.sha256,
                )
                forged_stage = replace(
                    stage,
                    supersession=forged_receipt,
                    cleanup_plan=forged_plan,
                    source_cleanup_pending=forged_pending,
                    cleanup_receipt=forged_cleanup,
                    source_superseded=forged_final,
                )
                with self.engine.begin() as conn:
                    install_v4_supersession_stage(conn, forged_stage)
                    insert_evidence(conn, source, forged_cleanup)
                    insert_checkpoint(conn, source, forged_final)
                    update_v4_head(conn, source, forged_final)
                    conn.execute(
                        sa.text(
                            "UPDATE disclosure_ops.remote_parse_attempt "
                            "SET is_current=true "
                            "WHERE attempt_id=:attempt_id"
                        ),
                        {"attempt_id": stage.attempt_id},
                    )
                    conn.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
                    session, repository = self._repository(conn)
                    try:
                        expectation = V4HeadExpectation(
                            attempt_id=stage.attempt_id,
                            fence_identity=stage.fence_identity,
                            state="prepared",
                            lifecycle_version=0,
                            checkpoint_sha256=stage.prepared.sha256,
                        )
                        with self.assertRaisesRegex(
                            RemoteParseV4AuthorityViolation,
                            "source cleanup transition",
                        ):
                            repository.load(stage.attempt_id)
                        with self.assertRaisesRegex(
                            RemoteParseV4AuthorityViolation,
                            "source cleanup transition",
                        ):
                            repository.claim(
                                expectation,
                                owner_identity="worker-wrong-predecessor",
                                lease_seconds=30,
                            )
                        with self.assertRaises(
                            RemoteParseV4AuthorityViolation
                        ):
                            repository.load(source.attempt_id)
                        target_claim = session.execute(
                            sa.text(
                                "SELECT claim_generation,claim_owner_identity,"
                                "claim_lease_until FROM "
                                "disclosure_ops.remote_parse_attempt "
                                "WHERE attempt_id=:attempt_id"
                            ),
                            {"attempt_id": stage.attempt_id},
                        ).mappings().one()
                        self.assertEqual(
                            dict(target_claim),
                            {
                                "claim_generation": 0,
                                "claim_owner_identity": None,
                                "claim_lease_until": None,
                            },
                        )
                        self.assertEqual(
                            session.execute(sa.text("SELECT 1")).scalar_one(),
                            1,
                        )
                    finally:
                        session.close()

    def test_strict_reload_rejects_canonical_byte_and_secret_binding_drift(
        self,
    ) -> None:
        evidence_drift = build_v4_authority_fixture()
        checkpoint_drift = build_v4_authority_fixture()
        reservation_drift = build_v4_authority_fixture()
        winner_drift = build_v4_authority_fixture()
        secret_drift = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            for fixture in (
                evidence_drift,
                checkpoint_drift,
                reservation_drift,
            ):
                insert_core_rows(conn, fixture)

            insert_v4_head(conn, evidence_drift, evidence_drift.prepared)
            insert_evidence(
                conn,
                evidence_drift,
                evidence_drift.preparation,
                exact_bytes_override=b"{}",
            )
            insert_evidence(conn, evidence_drift, evidence_drift.snapshot)
            insert_checkpoint(conn, evidence_drift, evidence_drift.prepared)

            insert_v4_head(conn, checkpoint_drift, checkpoint_drift.prepared)
            for evidence in checkpoint_drift.prepared_evidence:
                insert_evidence(conn, checkpoint_drift, evidence)
            insert_checkpoint(
                conn,
                checkpoint_drift,
                checkpoint_drift.prepared,
                checkpoint_bytes_override=b"{}",
            )

            insert_v4_head(conn, reservation_drift, reservation_drift.prepared)
            for evidence in reservation_drift.prepared_evidence:
                insert_evidence(conn, reservation_drift, evidence)
            insert_checkpoint(
                conn,
                reservation_drift,
                reservation_drift.prepared,
                reservation_bytes_override=b"{}",
            )

            install_local_materialized_cycle(conn, winner_drift)
            insert_winner(conn, winner_drift, winner_bytes_override=b"{}")
            insert_checkpoint(conn, winner_drift, winner_drift.publish_committed)
            update_v4_head(conn, winner_drift, winner_drift.publish_committed)

            install_submitted_cycle(conn, secret_drift, include_secret=False)
            insert_secret(
                conn,
                secret_drift,
                replace(
                    secret_drift.sealed_secret,
                    binding=replace(
                        secret_drift.sealed_secret.binding,
                        secret_kind="different-task-token.v1",
                    ),
                ),
            )
            conn.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
            conn.exec_driver_sql("SET CONSTRAINTS ALL DEFERRED")

            session, repository = self._repository(conn)
            try:
                for fixture in (
                    evidence_drift,
                    checkpoint_drift,
                    reservation_drift,
                    winner_drift,
                    secret_drift,
                ):
                    with self.subTest(
                        attempt_id=fixture.attempt_id
                    ), self.assertRaises(RemoteParseV4AuthorityViolation):
                        repository.load(fixture.attempt_id)
            finally:
                session.close()

    def test_claim_same_owner_retry_reclaim_renew_and_exhaustion(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
            session, repository = self._repository(conn)
            try:
                expectation = V4HeadExpectation.from_authority(
                    repository.load(fixture.attempt_id)
                )
                first = repository.claim(
                    expectation,
                    owner_identity="worker-a",
                    lease_seconds=30,
                )
                retry = repository.claim(
                    expectation,
                    owner_identity="worker-a",
                    lease_seconds=30,
                )
                self.assertEqual(first.claim_generation, 1)
                self.assertEqual(retry.claim_generation, 1)
                first_lease = first.claim_lease_until
                retry_lease = retry.claim_lease_until
                self.assertIsNotNone(first_lease)
                self.assertIsNotNone(retry_lease)
                assert first_lease is not None
                assert retry_lease is not None
                self.assertGreaterEqual(retry_lease, first_lease)
                with self.assertRaises(V4ClaimHeldByOther):
                    repository.claim(
                        expectation,
                        owner_identity="worker-b",
                        lease_seconds=30,
                    )

                renewed = repository.renew(
                    retry.claim_witness,
                    lease_seconds=60,
                )
                self.assertEqual(renewed.claim_generation, 1)
                renewed_lease = renewed.claim_lease_until
                self.assertIsNotNone(renewed_lease)
                assert renewed_lease is not None
                self.assertGreaterEqual(renewed_lease, retry_lease)
                with self.assertRaises(V4ClaimLost):
                    repository.renew(
                        replace(
                            renewed.claim_witness,
                            claim_generation=2,
                        ),
                        lease_seconds=30,
                    )

                session.execute(
                    sa.text(
                        "UPDATE disclosure_ops.remote_parse_attempt "
                        "SET claim_lease_until=clock_timestamp()-interval '1 second' "
                        "WHERE attempt_id=:attempt_id"
                    ),
                    {"attempt_id": fixture.attempt_id},
                )
                reclaimed = repository.claim(
                    expectation,
                    owner_identity="worker-b",
                    lease_seconds=30,
                )
                self.assertEqual(reclaimed.claim_generation, 2)
                self.assertEqual(reclaimed.claim_owner_identity, "worker-b")

                session.execute(
                    sa.text(
                        "UPDATE disclosure_ops.remote_parse_attempt "
                        "SET claim_generation=:maximum,"
                        "claim_lease_until=clock_timestamp()+interval '30 seconds' "
                        "WHERE attempt_id=:attempt_id"
                    ),
                    {
                        "attempt_id": fixture.attempt_id,
                        "maximum": (1 << 63) - 1,
                    },
                )
                at_max = repository.claim(
                    expectation,
                    owner_identity="worker-b",
                    lease_seconds=30,
                )
                self.assertEqual(at_max.claim_generation, (1 << 63) - 1)
                renewed_at_max = repository.renew(
                    at_max.claim_witness,
                    lease_seconds=30,
                )
                self.assertEqual(
                    renewed_at_max.claim_generation,
                    (1 << 63) - 1,
                )
                session.execute(
                    sa.text(
                        "UPDATE disclosure_ops.remote_parse_attempt SET "
                        "claim_lease_until=clock_timestamp()-interval '1 second' "
                        "WHERE attempt_id=:attempt_id"
                    ),
                    {"attempt_id": fixture.attempt_id},
                )
                with self.assertRaises(V4ClaimGenerationExhausted):
                    repository.claim(
                        expectation,
                        owner_identity="worker-c",
                        lease_seconds=30,
                    )
            finally:
                session.close()

    def test_two_owners_race_for_one_unclaimed_head(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
            session, repository = self._repository(conn)
            try:
                expectation = V4HeadExpectation.from_authority(
                    repository.load(fixture.attempt_id)
                )
            finally:
                session.close()

        barrier = Barrier(2)

        def compete(owner: str) -> str:
            with self.engine.begin() as conn:
                session, repository = self._repository(conn)
                try:
                    barrier.wait(timeout=5)
                    try:
                        repository.claim(
                            expectation,
                            owner_identity=owner,
                            lease_seconds=30,
                        )
                    except V4ClaimHeldByOther:
                        return "held"
                    return "acquired"
                finally:
                    session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(
                executor.map(compete, ("worker-race-a", "worker-race-b"))
            )
        self.assertEqual(sorted(outcomes), ["acquired", "held"])

    def test_post_lock_database_clock_rejects_blocked_expired_renew(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
            session, repository = self._repository(conn)
            try:
                claimed = repository.claim(
                    V4HeadExpectation.from_authority(
                        repository.load(fixture.attempt_id)
                    ),
                    owner_identity="worker-blocked-renew",
                    lease_seconds=2,
                )
            finally:
                session.close()

        locked = Event()

        def hold_head_lock() -> None:
            with self.engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "SELECT attempt_id FROM disclosure_ops.remote_parse_attempt "
                        "WHERE attempt_id=:attempt_id FOR UPDATE"
                    ),
                    {"attempt_id": fixture.attempt_id},
                ).one()
                locked.set()
                time.sleep(2.2)

        def blocked_renew() -> str:
            if not locked.wait(timeout=5):
                raise AssertionError("head lock was not acquired")
            with self.engine.begin() as conn:
                session, repository = self._repository(conn)
                try:
                    with self.assertRaises(V4ClaimLost):
                        repository.renew(
                            claimed.claim_witness,
                            lease_seconds=30,
                        )
                    return "expired"
                finally:
                    session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            holder = executor.submit(hold_head_lock)
            renewer = executor.submit(blocked_renew)
            holder.result(timeout=10)
            self.assertEqual(renewer.result(timeout=10), "expired")

    def test_expired_append_and_reclaim_race_cannot_both_win(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            insert_core_rows(conn, fixture)
            session, repository = self._repository(conn)
            try:
                claimed = repository.claim(
                    V4HeadExpectation.from_authority(
                        repository.create_prepared(
                            self._prepared_creation(fixture)
                        )
                    ),
                    owner_identity="worker-expiring-append",
                    lease_seconds=2,
                )
                append = self._successor_append(
                    claimed,
                    fixture.reconciling,
                    fixture.submission,
                )
                expectation = V4HeadExpectation.from_authority(claimed)
            finally:
                session.close()

        locked = Event()

        def hold_head_lock() -> None:
            with self.engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "SELECT attempt_id FROM disclosure_ops.remote_parse_attempt "
                        "WHERE attempt_id=:attempt_id FOR UPDATE"
                    ),
                    {"attempt_id": fixture.attempt_id},
                ).one()
                locked.set()
                time.sleep(2.2)

        def blocked_append() -> str:
            if not locked.wait(timeout=5):
                raise AssertionError("head lock was not acquired")
            with self.engine.begin() as conn:
                session, repository = self._repository(conn)
                try:
                    with self.assertRaises(V4ClaimLost):
                        repository.append_successor(append)
                    return "append-lost"
                finally:
                    session.close()

        def blocked_reclaim() -> str:
            if not locked.wait(timeout=5):
                raise AssertionError("head lock was not acquired")
            time.sleep(0.05)
            with self.engine.begin() as conn:
                session, repository = self._repository(conn)
                try:
                    reclaimed = repository.claim(
                        expectation,
                        owner_identity="worker-expired-reclaimer",
                        lease_seconds=30,
                    )
                    self.assertEqual(reclaimed.claim_generation, 2)
                    return "reclaimed"
                finally:
                    session.close()

        with ThreadPoolExecutor(max_workers=3) as executor:
            holder = executor.submit(hold_head_lock)
            appender = executor.submit(blocked_append)
            reclaimer = executor.submit(blocked_reclaim)
            holder.result(timeout=10)
            self.assertEqual(appender.result(timeout=10), "append-lost")
            self.assertEqual(reclaimer.result(timeout=10), "reclaimed")

        with self.engine.begin() as conn:
            session, repository = self._repository(conn)
            try:
                authority = repository.load(fixture.attempt_id)
            finally:
                session.close()
        self.assertEqual(authority.checkpoint, fixture.prepared)
        self.assertEqual(authority.claim_owner_identity, "worker-expired-reclaimer")
        self.assertEqual(authority.claim_generation, 2)

    def test_two_connections_append_same_successor_and_reconcile(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            insert_core_rows(conn, fixture)
            session, repository = self._repository(conn)
            try:
                claimed = repository.claim(
                    V4HeadExpectation.from_authority(
                        repository.create_prepared(
                            self._prepared_creation(fixture)
                        )
                    ),
                    owner_identity="worker-append-race",
                    lease_seconds=120,
                )
                append = self._successor_append(
                    claimed,
                    fixture.reconciling,
                    fixture.submission,
                )
            finally:
                session.close()

        barrier = Barrier(2)

        def compete() -> str:
            with self.engine.begin() as conn:
                session, repository = self._repository(conn)
                try:
                    barrier.wait(timeout=5)
                    try:
                        repository.append_successor(append)
                    except V4HeadStale:
                        reconciled = repository.reconcile_successor(append)
                        self.assertEqual(
                            reconciled.authority.checkpoint_sha256,
                            fixture.reconciling.sha256,
                        )
                        return "reconciled"
                    return "appended"
                finally:
                    session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(lambda _: compete(), range(2)))
        self.assertEqual(sorted(outcomes), ["appended", "reconciled"])

    def test_two_connections_proposing_different_successors_do_not_alias(
        self,
    ) -> None:
        fixture = build_v4_authority_fixture()
        alternate_submission = replace(
            fixture.submission,
            submission_epoch_unix=fixture.submission.submission_epoch_unix + 1,
        )
        alternate_checkpoint = advance_remote_parse_checkpoint_v4(
            fixture.prepared,
            state="reconciling",
            held_resource_credit=fixture.reconciling.held_resource_credit,
            submission_intent_sha256=alternate_submission.sha256,
        )
        with self.engine.begin() as conn:
            insert_core_rows(conn, fixture)
            session, repository = self._repository(conn)
            try:
                claimed = repository.claim(
                    V4HeadExpectation.from_authority(
                        repository.create_prepared(
                            self._prepared_creation(fixture)
                        )
                    ),
                    owner_identity="worker-different-race",
                    lease_seconds=120,
                )
                proposals = (
                    self._successor_append(
                        claimed,
                        fixture.reconciling,
                        fixture.submission,
                    ),
                    self._successor_append(
                        claimed,
                        alternate_checkpoint,
                        alternate_submission,
                    ),
                )
            finally:
                session.close()

        barrier = Barrier(2)

        def compete(append: V4SuccessorAppend) -> str:
            with self.engine.begin() as conn:
                session, repository = self._repository(conn)
                try:
                    barrier.wait(timeout=5)
                    try:
                        repository.append_successor(append)
                    except V4HeadStale:
                        with self.assertRaises(V4DifferentSuccessorCommitted):
                            repository.reconcile_successor(append)
                        return "different"
                    return "appended"
                finally:
                    session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(compete, proposals))
        self.assertEqual(sorted(outcomes), ["appended", "different"])

    def test_claim_generation_fences_aba_and_restarted_actor(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
            session, repository = self._repository(conn)
            try:
                expectation = V4HeadExpectation.from_authority(
                    repository.load(fixture.attempt_id)
                )
                generation_one = repository.claim(
                    expectation,
                    owner_identity="worker-aba-a",
                    lease_seconds=30,
                )
                for owner in ("worker-aba-b", "worker-aba-a"):
                    session.execute(
                        sa.text(
                            "UPDATE disclosure_ops.remote_parse_attempt SET "
                            "claim_lease_until=clock_timestamp()-interval '1 second' "
                            "WHERE attempt_id=:attempt_id"
                        ),
                        {"attempt_id": fixture.attempt_id},
                    )
                    latest = repository.claim(
                        expectation,
                        owner_identity=owner,
                        lease_seconds=30,
                    )
                self.assertEqual(latest.claim_generation, 3)
                self.assertEqual(latest.claim_owner_identity, "worker-aba-a")
                with self.assertRaises(V4ClaimHeldByOther):
                    repository.claim(
                        expectation,
                        owner_identity="worker-aba-a-restarted",
                        lease_seconds=30,
                    )
                with self.assertRaises(V4ClaimLost):
                    repository.reload_claimed(generation_one.claim_witness)
                with self.assertRaises(V4ClaimLost):
                    repository.renew(
                        generation_one.claim_witness,
                        lease_seconds=30,
                    )
                with self.assertRaises(V4ClaimLost):
                    repository.append_successor(
                        self._successor_append(
                            generation_one,
                            fixture.reconciling,
                            fixture.submission,
                        )
                    )
            finally:
                session.close()

    def test_reconcile_proves_commit_after_lease_reclaim(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            insert_core_rows(conn, fixture)
            session, repository = self._repository(conn)
            try:
                claimed = repository.claim(
                    V4HeadExpectation.from_authority(
                        repository.create_prepared(
                            self._prepared_creation(fixture)
                        )
                    ),
                    owner_identity="worker-old",
                    lease_seconds=30,
                )
                append = self._successor_append(
                    claimed,
                    fixture.reconciling,
                    fixture.submission,
                )
                committed = repository.append_successor(append)
            finally:
                session.close()

        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE disclosure_ops.remote_parse_attempt SET "
                    "claim_lease_until=clock_timestamp()-interval '1 second' "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": fixture.attempt_id},
            )
            session, repository = self._repository(conn)
            try:
                reclaimed = repository.claim(
                    V4HeadExpectation.from_authority(committed),
                    owner_identity="worker-new",
                    lease_seconds=30,
                )
                self.assertEqual(reclaimed.claim_generation, 2)
            finally:
                session.close()

        with self.engine.begin() as conn:
            session, repository = self._repository(conn)
            try:
                reconciliation = repository.reconcile_successor(append)
            finally:
                session.close()
        self.assertEqual(
            reconciliation.authority.checkpoint_sha256,
            fixture.reconciling.sha256,
        )
        self.assertFalse(reconciliation.authorization_still_live)

    def test_successor_outer_rollback_leaves_predecessor_authority(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            insert_core_rows(conn, fixture)
            session, repository = self._repository(conn)
            try:
                claimed = repository.claim(
                    V4HeadExpectation.from_authority(
                        repository.create_prepared(
                            self._prepared_creation(fixture)
                        )
                    ),
                    owner_identity="worker-rollback",
                    lease_seconds=120,
                )
            finally:
                session.close()
        append = self._successor_append(
            claimed,
            fixture.reconciling,
            fixture.submission,
        )

        conn = self.engine.connect()
        transaction = conn.begin()
        session, repository = self._repository(conn)
        try:
            repository.append_successor(append)
            transaction.rollback()
        finally:
            session.close()
            conn.close()

        with self.engine.begin() as verify_conn:
            session, repository = self._repository(verify_conn)
            try:
                persisted = repository.load(fixture.attempt_id)
                self.assertEqual(persisted.checkpoint, fixture.prepared)
                with self.assertRaises(V4SuccessorNotCommitted):
                    repository.reconcile_successor(append)
            finally:
                session.close()

    def test_create_methods_close_generation_currentness_and_links(self) -> None:
        prepared = build_v4_authority_fixture()
        failed = build_v4_authority_fixture()
        supersession_source = build_v4_authority_fixture()
        supersession = build_v4_resource_free_supersession_fixture(
            supersession_source
        )
        with self.engine.begin() as conn:
            for fixture in (prepared, failed, supersession_source):
                insert_core_rows(conn, fixture)
            session, repository = self._repository(conn)
            try:
                prepared_authority = repository.create_prepared(
                    self._prepared_creation(prepared)
                )
                failed_authority = repository.create_resource_free_failure(
                    V4ResourceFreeFailureCreation(
                        checkpoint=failed.preparation_failed,
                        failure_receipt=failed.preparation_failure,
                        parser_target_sha256=failed.parser_target_sha256,
                        client_submit_key=failed.client_submit_key,
                    )
                )
                source_authority, target_authority = (
                    repository.create_resource_free_supersession(
                        V4ResourceFreeSupersessionCreation(
                            source_checkpoint=supersession.source_superseded,
                            supersession_receipt=supersession.supersession,
                            source_parser_target_sha256=(
                                supersession_source.parser_target_sha256
                            ),
                            source_client_submit_key=(
                                supersession_source.client_submit_key
                            ),
                            superseding=V4PreparedCreation(
                                checkpoint=supersession.target.prepared,
                                reservation=supersession.target.reservation,
                                preparation_intent=(
                                    supersession.target.preparation
                                ),
                                snapshot_receipt=supersession.target.snapshot,
                                parser_target_sha256=(
                                    supersession.target.parser_target_sha256
                                ),
                                client_submit_key=(
                                    supersession.target.client_submit_key
                                ),
                            ),
                        )
                    )
                )
                self.assertTrue(prepared_authority.is_current)
                self.assertEqual(failed_authority.state, "preparation_failed")
                self.assertFalse(failed_authority.is_current)
                self.assertEqual(source_authority.state, "superseded")
                self.assertFalse(source_authority.is_current)
                self.assertTrue(target_authority.is_current)
                self.assertIsNotNone(source_authority.source_supersession_link)
                self.assertIsNotNone(target_authority.staged_by_link)
                replayed_pair = repository.create_resource_free_supersession(
                    V4ResourceFreeSupersessionCreation(
                        source_checkpoint=supersession.source_superseded,
                        supersession_receipt=supersession.supersession,
                        source_parser_target_sha256=(
                            supersession_source.parser_target_sha256
                        ),
                        source_client_submit_key=(
                            supersession_source.client_submit_key
                        ),
                        superseding=V4PreparedCreation(
                            checkpoint=supersession.target.prepared,
                            reservation=supersession.target.reservation,
                            preparation_intent=supersession.target.preparation,
                            snapshot_receipt=supersession.target.snapshot,
                            parser_target_sha256=(
                                supersession.target.parser_target_sha256
                            ),
                            client_submit_key=(
                                supersession.target.client_submit_key
                            ),
                        ),
                    )
                )
                self.assertEqual(
                    replayed_pair[0].checkpoint_sha256,
                    source_authority.checkpoint_sha256,
                )
                self.assertEqual(
                    replayed_pair[1].checkpoint_sha256,
                    target_authority.checkpoint_sha256,
                )
                replayed = repository.create_prepared(
                    self._prepared_creation(prepared)
                )
                self.assertEqual(replayed, prepared_authority)
                conflicting = build_v4_resource_free_supersession_fixture(
                    prepared
                ).target
                with self.assertRaises(V4DocumentCurrentConflict):
                    repository.create_prepared(
                        V4PreparedCreation(
                            checkpoint=conflicting.prepared,
                            reservation=conflicting.reservation,
                            preparation_intent=conflicting.preparation,
                            snapshot_receipt=conflicting.snapshot,
                            parser_target_sha256=conflicting.parser_target_sha256,
                            client_submit_key=conflicting.client_submit_key,
                        )
                    )
            finally:
                session.close()

    def test_resource_free_failure_coexists_with_current_and_replays_exactly(
        self,
    ) -> None:
        fixture = build_v4_authority_fixture()
        attempt_id = fixture.attempt_id + "-f"
        fence_identity = fixture.fence_identity + "-f"
        receipt = FailureReceiptV4(
            attempt_id=attempt_id,
            fence_identity=fence_identity,
            outcome="preparation_failure",
            source_state="not_prepared",
            source_lifecycle_version=0,
            source_checkpoint_sha256=None,
            submission_was_attempted=False,
            submission_absence_proof=None,
            accepted_submission_receipt_sha256=None,
            terminal_receipt_sha256=None,
            materialization_intent_sha256=None,
            local_materialization_receipt_sha256=None,
            error_code="snapshot_failed",
            error_stage="prepare",
            error_class="OSError",
            retryable=True,
            retry_budget_class="local_io",
            message="snapshot preparation failed",
        )
        checkpoint = build_resource_free_remote_parse_checkpoint_v4(
            state="preparation_failed",
            attempt_id=attempt_id,
            attempt_generation=2,
            fence_identity=fence_identity,
            document_id=fixture.document_id,
            processing_run_id=fixture.processing_run_id,
            source_pdf_sha256=fixture.source_pdf_sha256,
            source_byte_count=fixture.reservation.source_byte_count,
            source_page_count=fixture.reservation.source_page_count,
            request_sha256=fixture.request_sha256,
            runtime_epoch_sha256=fixture.runtime_epoch_sha256,
            process_profile_sha256=fixture.process_profile_sha256,
            credit_policy_sha256=fixture.credit_policy_sha256,
            reservation_input_sha256=fixture.reservation_input.sha256,
            failure_receipt_sha256=receipt.sha256,
        )
        creation = V4ResourceFreeFailureCreation(
            checkpoint=checkpoint,
            failure_receipt=receipt,
            parser_target_sha256=fixture.parser_target_sha256,
            client_submit_key=fixture.client_submit_key + "-f",
        )
        with self.engine.begin() as conn:
            insert_core_rows(conn, fixture)
            session, repository = self._repository(conn)
            try:
                current = repository.create_prepared(
                    self._prepared_creation(fixture)
                )
                failed = repository.create_resource_free_failure(creation)
                replayed = repository.create_resource_free_failure(creation)
                self.assertTrue(current.is_current)
                self.assertFalse(failed.is_current)
                self.assertEqual(replayed.checkpoint_sha256, failed.checkpoint_sha256)
                with self.assertRaises(V4GenerationConflict):
                    repository.create_resource_free_failure(
                        replace(
                            creation,
                            client_submit_key=creation.client_submit_key + "-other",
                        )
                    )
            finally:
                session.close()

    def test_successor_reconcile_rewrap_and_final_purge_are_atomic(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            insert_core_rows(conn, fixture)
            session, repository = self._repository(conn)
            try:
                authority = repository.create_prepared(
                    self._prepared_creation(fixture)
                )
                authority = repository.claim(
                    V4HeadExpectation.from_authority(authority),
                    owner_identity="worker-successor",
                    lease_seconds=120,
                )
                reloaded_claim = repository.reload_claimed(
                    authority.claim_witness
                )
                self.assertEqual(reloaded_claim.checkpoint, authority.checkpoint)
                self.assertEqual(
                    reloaded_claim.claim_witness,
                    authority.claim_witness,
                )
                self.assertIsNotNone(reloaded_claim.database_lease)
                reconciling_append = self._successor_append(
                    authority,
                    fixture.reconciling,
                    fixture.submission,
                )
                with self.assertRaises(V4SuccessorNotCommitted):
                    repository.reconcile_successor(reconciling_append)
                authority = repository.append_successor(reconciling_append)

                replayed_creation = repository.create_prepared(
                    self._prepared_creation(fixture)
                )
                self.assertEqual(
                    (
                        replayed_creation.state,
                        replayed_creation.checkpoint_sha256,
                        replayed_creation.claim_generation,
                    ),
                    (
                        authority.state,
                        authority.checkpoint_sha256,
                        authority.claim_generation,
                    ),
                )
                with self.assertRaises(V4GenerationConflict):
                    repository.create_prepared(
                        replace(
                            self._prepared_creation(fixture),
                            client_submit_key="different-response-loss-key",
                        )
                    )

                submitted_append = self._successor_append(
                    authority,
                    fixture.submitted,
                    fixture.accepted,
                    sealed_secret=fixture.sealed_secret,
                )
                authority = repository.append_successor(submitted_append)
                rewrapped = replace(
                    fixture.sealed_secret,
                    encryption_revision=2,
                    kek_id="test-kek-2",
                    wrap_nonce=b"r" * 12,
                    wrapped_dek=b"w" * 48,
                )
                history = repository.rewrap_secret(
                    V4SecretRewrap(
                        attempt_id=fixture.attempt_id,
                        fence_identity=fixture.fence_identity,
                        rewrapped=rewrapped,
                    )
                )
                self.assertEqual(history, (fixture.sealed_secret, rewrapped))
                self.assertEqual(
                    repository.rewrap_secret(
                        V4SecretRewrap(
                            attempt_id=fixture.attempt_id,
                            fence_identity=fixture.fence_identity,
                            rewrapped=rewrapped,
                        )
                    ),
                    history,
                )
                skipped_revision = replace(
                    rewrapped,
                    encryption_revision=4,
                    kek_id="test-kek-4",
                )
                with self.assertRaises(V4SecretRevisionConflict):
                    repository.rewrap_secret(
                        V4SecretRewrap(
                            attempt_id=fixture.attempt_id,
                            fence_identity=fixture.fence_identity,
                            rewrapped=skipped_revision,
                        )
                    )

                for checkpoint, cleanup_evidence in (
                    (fixture.remote_terminal, (fixture.terminal,)),
                    (fixture.materializing, (fixture.materialization_intent,)),
                    (
                        fixture.local_materialized,
                        (fixture.local_materialization_receipt,),
                    ),
                ):
                    authority = repository.append_successor(
                        self._successor_append(
                            authority,
                            checkpoint,
                            *cleanup_evidence,
                        )
                    )
                authority = repository.append_successor(
                    self._successor_append(
                        authority,
                        fixture.publish_committed,
                        publication_winner=fixture.publication_winner,
                    )
                )
                for checkpoint, finalization_evidence in (
                    (
                        fixture.success_cleanup_pending,
                        (fixture.success_cleanup_plan,),
                    ),
                    (
                        fixture.success_ack_pending,
                        (fixture.success_cleanup_receipt,),
                    ),
                ):
                    authority = repository.append_successor(
                        self._successor_append(
                            authority,
                            checkpoint,
                            *finalization_evidence,
                        )
                    )
                final_append = self._successor_append(
                    authority,
                    fixture.acked,
                    fixture.success_ack_receipt,
                )
                authority = repository.append_successor(final_append)
                self.assertEqual(authority.state, "acked")
                self.assertFalse(authority.is_current)
                self.assertEqual(authority.secret_history, ())
                reconciled = repository.reconcile_successor(final_append)
                self.assertEqual(reconciled.authority, authority)
                self.assertFalse(reconciled.authorization_still_live)
                with self.assertRaises(V4HeadStale):
                    repository.reconcile_successor(reconciling_append)
                final_rewrap = replace(
                    rewrapped,
                    encryption_revision=3,
                    kek_id="test-kek-final",
                )
                with self.assertRaises(V4AttemptFinal):
                    repository.rewrap_secret(
                        V4SecretRewrap(
                            attempt_id=fixture.attempt_id,
                            fence_identity=fixture.fence_identity,
                            rewrapped=final_rewrap,
                        )
                    )
            finally:
                session.close()

    def test_final_ack_purge_outer_rollback_restores_head_and_secret(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            install_success_ack_pending_cycle(conn, fixture)
            session, repository = self._repository(conn)
            try:
                authority = repository.load(fixture.attempt_id)
                final_append = self._successor_append(
                    authority,
                    fixture.acked,
                    fixture.success_ack_receipt,
                )
            finally:
                session.close()

        conn = self.engine.connect()
        transaction = conn.begin()
        session, repository = self._repository(conn)
        try:
            final = repository.append_successor(final_append)
            self.assertEqual(final.state, "acked")
            self.assertEqual(final.secret_history, ())
            transaction.rollback()
        finally:
            session.close()
            conn.close()

        with self.engine.begin() as conn:
            session, repository = self._repository(conn)
            try:
                restored = repository.load(fixture.attempt_id)
                residue = conn.execute(
                    sa.text(
                        "SELECT "
                        "(SELECT count(*) FROM disclosure_ops.remote_parse_v4_evidence "
                        " WHERE attempt_id=:attempt_id "
                        " AND evidence_kind='ack_receipt') AS ack_evidence_count,"
                        "(SELECT count(*) FROM disclosure_ops.remote_parse_v4_checkpoint "
                        " WHERE attempt_id=:attempt_id "
                        " AND lifecycle_version=:final_version) AS final_count,"
                        "(SELECT count(*) FROM disclosure_ops.remote_parse_v4_secret "
                        " WHERE attempt_id=:attempt_id) AS secret_count"
                    ),
                    {
                        "attempt_id": fixture.attempt_id,
                        "final_version": fixture.acked.lifecycle_version,
                    },
                ).mappings().one()
            finally:
                session.close()
        self.assertEqual(restored.state, "ack_pending")
        self.assertTrue(restored.is_current)
        self.assertEqual(restored.secret_history, (fixture.sealed_secret,))
        self.assertEqual(
            dict(residue),
            {
                "ack_evidence_count": 0,
                "final_count": 0,
                "secret_count": 1,
            },
        )

    def test_resourceful_supersession_stages_activates_and_progresses_h0(
        self,
    ) -> None:
        source = build_v4_authority_fixture()
        target_run_id = str(ids.new_processing_run_id())
        stage = build_v4_supersession_stage_fixture(
            source,
            processing_run_id=target_run_id,
        )
        staged_creation = V4PreparedCreation(
            checkpoint=stage.prepared,
            reservation=stage.reservation,
            preparation_intent=stage.preparation,
            snapshot_receipt=stage.snapshot,
            parser_target_sha256=stage.parser_target_sha256,
            client_submit_key=stage.client_submit_key,
        )
        with self.engine.begin() as conn:
            insert_core_rows(conn, source)
            conn.execute(
                sa.text(
                    "INSERT INTO disclosure_core.processing_run "
                    "(processing_run_id,document_id,"
                    "artifact_owner_processing_run_id,run_kind,status,"
                    "input_raw_file_hash,provider_document_relpath,"
                    "parser_target_identity) VALUES "
                    "(:run_id,:document_id,:run_id,'parse','running',"
                    ":source_sha,'scratch/provider.json',CAST(:target AS jsonb))"
                ),
                {
                    "run_id": target_run_id,
                    "document_id": source.document_id,
                    "source_sha": source.source_pdf_sha256,
                    "target": source.parser_target_identity_json,
                },
            )
            session, repository = self._repository(conn)
            try:
                authority = repository.claim(
                    V4HeadExpectation.from_authority(
                        repository.create_prepared(
                            self._prepared_creation(source)
                        )
                    ),
                    owner_identity="worker-supersession",
                    lease_seconds=120,
                )
                authority = repository.append_successor(
                    self._successor_append(
                        authority,
                        source.reconciling,
                        source.submission,
                    )
                )
                stage_append = V4SuccessorAppend(
                    claim=authority.claim_witness,
                    successor=stage.source_cleanup_pending,
                    new_evidence=tuple(
                        encode_remote_parse_evidence_v4(item)
                        for item in (stage.supersession, stage.cleanup_plan)
                    ),
                    staged_superseder=staged_creation,
                )
                authority = repository.append_successor(stage_append)
                staged_reconciliation = repository.reconcile_successor(
                    stage_append
                )
                self.assertEqual(
                    replace(
                        staged_reconciliation.authority,
                        database_lease=None,
                    ),
                    replace(authority, database_lease=None),
                )
                self.assertTrue(
                    staged_reconciliation.authorization_still_live
                )
                staged = repository.load(stage.attempt_id)
                self.assertFalse(staged.is_current)
                self.assertEqual(staged.state, "prepared")
                with self.assertRaises(V4HeadStale):
                    repository.claim(
                        V4HeadExpectation.from_authority(staged),
                        owner_identity="worker-too-early",
                        lease_seconds=30,
                    )
                final_append = self._successor_append(
                    authority,
                    stage.source_superseded,
                    stage.cleanup_receipt,
                )
                source_final = repository.append_successor(final_append)
                self.assertEqual(source_final.state, "superseded")
                with self.assertRaises(V4HeadStale):
                    repository.reconcile_successor(stage_append)
                activated = repository.load(stage.attempt_id)
                self.assertTrue(activated.is_current)

                activated = repository.claim(
                    V4HeadExpectation.from_authority(activated),
                    owner_identity="worker-target",
                    lease_seconds=120,
                )
                submission = SubmissionIntentV4(
                    attempt_id=stage.attempt_id,
                    fence_identity=stage.fence_identity,
                    snapshot_receipt_sha256=stage.snapshot.sha256,
                    source_pdf_sha256=source.source_pdf_sha256,
                    parser_target_sha256=stage.parser_target_sha256,
                    request_sha256=stage.request_sha256,
                    runtime_epoch_sha256=stage.runtime_epoch_sha256,
                    client_submit_key=stage.client_submit_key,
                    submission_epoch_unix=2,
                    provider_protocol_version="mineru-task-protocol.v2",
                )
                target_reconciling = advance_remote_parse_checkpoint_v4(
                    stage.prepared,
                    state="reconciling",
                    held_resource_credit=ResourceCreditVector(
                        documents=1,
                        snapshot_items=1,
                        snapshot_bytes=stage.reservation.source_byte_count,
                        remote_waits=1,
                    ),
                    submission_intent_sha256=submission.sha256,
                )
                progressed = repository.append_successor(
                    self._successor_append(
                        activated,
                        target_reconciling,
                        submission,
                    )
                )
                reloaded = repository.load(stage.attempt_id)
                self.assertEqual(progressed.state, "reconciling")
                self.assertEqual(reloaded.checkpoint_history[0], stage.prepared)
                self.assertEqual(reloaded.checkpoint, target_reconciling)
                self.assertEqual(reloaded.processing_run_id, target_run_id)
            finally:
                session.close()

        with self.engine.begin() as conn:
            session, repository = self._repository(conn)
            try:
                reconciled = repository.reconcile_successor(final_append)
            finally:
                session.close()
        self.assertEqual(reconciled.authority, source_final)
        self.assertFalse(reconciled.authorization_still_live)

    def test_staged_insert_failure_rolls_back_savepoint_and_outer_remains_usable(
        self,
    ) -> None:
        source = build_v4_authority_fixture()
        stage = build_v4_supersession_stage_fixture(source)
        valid_creation = V4PreparedCreation(
            checkpoint=stage.prepared,
            reservation=stage.reservation,
            preparation_intent=stage.preparation,
            snapshot_receipt=stage.snapshot,
            parser_target_sha256=stage.parser_target_sha256,
            client_submit_key=stage.client_submit_key,
        )
        invalid_creation = replace(
            valid_creation,
            client_submit_key=source.client_submit_key,
        )
        no_snapshot_checkpoint = build_initial_remote_parse_checkpoint_v4(
            reservation=stage.reservation,
            preparation_intent_sha256=stage.preparation.sha256,
            snapshot_receipt_sha256=None,
            held_resource_credit=stage.prepared.held_resource_credit,
        )
        no_snapshot_creation = V4PreparedCreation(
            checkpoint=no_snapshot_checkpoint,
            reservation=stage.reservation,
            preparation_intent=stage.preparation,
            snapshot_receipt=None,
            parser_target_sha256=stage.parser_target_sha256,
            client_submit_key=stage.client_submit_key,
        )
        with self.engine.begin() as conn:
            insert_core_rows(conn, source)
            session, repository = self._repository(conn)
            try:
                authority = repository.claim(
                    V4HeadExpectation.from_authority(
                        repository.create_prepared(
                            self._prepared_creation(source)
                        )
                    ),
                    owner_identity="worker-savepoint-recovery",
                    lease_seconds=120,
                )
                authority = repository.append_successor(
                    self._successor_append(
                        authority,
                        source.reconciling,
                        source.submission,
                    )
                )
                encoded_stage_evidence = tuple(
                    encode_remote_parse_evidence_v4(item)
                    for item in (stage.supersession, stage.cleanup_plan)
                )
                with self.assertRaises(V4GenerationConflict):
                    repository.append_successor(
                        V4SuccessorAppend(
                            claim=authority.claim_witness,
                            successor=stage.source_cleanup_pending,
                            new_evidence=encoded_stage_evidence,
                            staged_superseder=no_snapshot_creation,
                        )
                    )
                with self.assertRaises(V4GenerationConflict):
                    repository.append_successor(
                        V4SuccessorAppend(
                            claim=authority.claim_witness,
                            successor=stage.source_cleanup_pending,
                            new_evidence=encoded_stage_evidence,
                            staged_superseder=invalid_creation,
                        )
                    )
                unchanged = repository.load(source.attempt_id)
                self.assertEqual(unchanged.checkpoint, source.reconciling)
                target_count = session.execute(
                    sa.text(
                        "SELECT count(*) FROM disclosure_ops.remote_parse_attempt "
                        "WHERE attempt_id=:attempt_id"
                    ),
                    {"attempt_id": stage.attempt_id},
                ).scalar_one()
                self.assertEqual(target_count, 0)

                committed = repository.append_successor(
                    V4SuccessorAppend(
                        claim=authority.claim_witness,
                        successor=stage.source_cleanup_pending,
                        new_evidence=encoded_stage_evidence,
                        staged_superseder=valid_creation,
                    )
                )
                self.assertEqual(committed.state, "cleanup_pending")
                self.assertEqual(repository.load(stage.attempt_id).state, "prepared")
            finally:
                session.close()

    def test_deferred_trigger_failure_is_typed_and_outer_transaction_recovers(
        self,
    ) -> None:
        corrupt = build_v4_authority_fixture()
        creation = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, corrupt)
            insert_core_rows(conn, creation)
            session, repository = self._repository(conn)
            try:
                encoded = encode_remote_parse_evidence_v4(corrupt.submission)
                corrupt_scope = session.begin_nested()
                session.execute(
                    sa.text(
                        "INSERT INTO disclosure_ops.remote_parse_v4_evidence "
                        "(attempt_id,fence_identity,evidence_kind,evidence_sha256,"
                        "evidence_bytes,evidence_byte_count) VALUES "
                        "(:attempt_id,:fence,:kind,:sha256,:payload,:byte_count)"
                    ),
                    {
                        "attempt_id": corrupt.attempt_id,
                        "fence": corrupt.fence_identity,
                        "kind": encoded.kind,
                        "sha256": encoded.sha256,
                        "payload": encoded.exact_bytes,
                        "byte_count": encoded.byte_count,
                    },
                )
                with self.assertRaises(RemoteParseV4AuthorityViolation):
                    repository.create_prepared(
                        self._prepared_creation(creation)
                    )
                corrupt_scope.rollback()
                created = repository.create_prepared(
                    self._prepared_creation(creation)
                )
                self.assertEqual(created.checkpoint, creation.prepared)
            finally:
                session.close()

        with self.engine.begin() as conn:
            session, repository = self._repository(conn)
            try:
                self.assertEqual(
                    repository.load(corrupt.attempt_id).checkpoint,
                    corrupt.prepared,
                )
                self.assertEqual(
                    repository.load(creation.attempt_id).checkpoint,
                    creation.prepared,
                )
            finally:
                session.close()

    def test_infrastructure_dbapi_failure_is_not_mislabeled_as_corruption(
        self,
    ) -> None:
        class Disconnected(Exception):
            sqlstate = "08006"

        transient = DBAPIError(
            "SELECT 1",
            {},
            Disconnected("connection lost"),
            connection_invalidated=True,
        )
        with self.assertRaises(DBAPIError) as raised:
            RemoteParseV4Repository._raise_authority_dbapi(
                transient,
                "must not be used",
            )
        self.assertIs(raised.exception, transient)

    def test_concurrent_secret_rewrap_has_one_revision_and_exact_replay(
        self,
    ) -> None:
        fixture = build_v4_authority_fixture()
        proposals = (
            replace(
                fixture.sealed_secret,
                encryption_revision=2,
                kek_id="test-kek-race-a",
                wrap_nonce=b"a" * 12,
                wrapped_dek=b"A" * 48,
            ),
            replace(
                fixture.sealed_secret,
                encryption_revision=2,
                kek_id="test-kek-race-b",
                wrap_nonce=b"b" * 12,
                wrapped_dek=b"B" * 48,
            ),
        )
        with self.engine.begin() as conn:
            install_submitted_cycle(conn, fixture, include_secret=True)

        barrier = Barrier(2)

        def compete(proposed: SealedProviderSecretV4) -> str:
            with self.engine.begin() as conn:
                session, repository = self._repository(conn)
                try:
                    barrier.wait(timeout=5)
                    try:
                        repository.rewrap_secret(
                            V4SecretRewrap(
                                attempt_id=fixture.attempt_id,
                                fence_identity=fixture.fence_identity,
                                rewrapped=proposed,
                            )
                        )
                    except V4SecretRevisionConflict:
                        return "conflict"
                    return "rewrapped"
                finally:
                    session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(compete, proposals))
        self.assertEqual(sorted(outcomes), ["conflict", "rewrapped"])

        with self.engine.begin() as conn:
            session, repository = self._repository(conn)
            try:
                authority = repository.load(fixture.attempt_id)
                self.assertEqual(len(authority.secret_history), 2)
                winning_revision = authority.secret_history[-1]
                replayed = repository.rewrap_secret(
                    V4SecretRewrap(
                        attempt_id=fixture.attempt_id,
                        fence_identity=fixture.fence_identity,
                        rewrapped=winning_revision,
                    )
                )
            finally:
                session.close()
        self.assertEqual(replayed, authority.secret_history)

    def test_publication_request_is_not_parse_head_request(self) -> None:
        fixture = build_v4_authority_fixture()
        winner = replace(
            fixture.publication_winner,
            request_sha256=sha256_bytes(b"distinct-publication-request"),
        )
        successor = replace(
            fixture.publish_committed,
            publication_winner_sha256=winner.sha256,
        )
        with self.engine.begin() as conn:
            install_local_materialized_cycle(conn, fixture)
            session, repository = self._repository(conn)
            try:
                authority = repository.load(fixture.attempt_id)
                published = repository.append_successor(
                    self._successor_append(
                        authority,
                        successor,
                        publication_winner=winner,
                    )
                )
                self.assertEqual(published.publication_winner, winner)
                persisted_winner = published.publication_winner
                assert persisted_winner is not None
                self.assertNotEqual(
                    persisted_winner.request_sha256,
                    published.request_sha256,
                )
            finally:
                session.close()

    def test_successful_supersession_staging_outer_rollback_is_all_or_none(
        self,
    ) -> None:
        source = build_v4_authority_fixture()
        stage = build_v4_supersession_stage_fixture(source)
        staged_creation = V4PreparedCreation(
            checkpoint=stage.prepared,
            reservation=stage.reservation,
            preparation_intent=stage.preparation,
            snapshot_receipt=stage.snapshot,
            parser_target_sha256=stage.parser_target_sha256,
            client_submit_key=stage.client_submit_key,
        )
        with self.engine.begin() as conn:
            insert_core_rows(conn, source)
            session, repository = self._repository(conn)
            try:
                authority = repository.claim(
                    V4HeadExpectation.from_authority(
                        repository.create_prepared(
                            self._prepared_creation(source)
                        )
                    ),
                    owner_identity="worker-stage-outer-rollback",
                    lease_seconds=120,
                )
                authority = repository.append_successor(
                    self._successor_append(
                        authority,
                        source.reconciling,
                        source.submission,
                    )
                )
                stage_append = V4SuccessorAppend(
                    claim=authority.claim_witness,
                    successor=stage.source_cleanup_pending,
                    new_evidence=tuple(
                        encode_remote_parse_evidence_v4(item)
                        for item in (stage.supersession, stage.cleanup_plan)
                    ),
                    staged_superseder=staged_creation,
                )
            finally:
                session.close()

        conn = self.engine.connect()
        transaction = conn.begin()
        session, repository = self._repository(conn)
        try:
            staged_source = repository.append_successor(stage_append)
            self.assertEqual(staged_source.state, "cleanup_pending")
            self.assertEqual(repository.load(stage.attempt_id).state, "prepared")
            transaction.rollback()
        finally:
            session.close()
            conn.close()

        with self.engine.begin() as conn:
            session, repository = self._repository(conn)
            try:
                source_after = repository.load(source.attempt_id)
                residue = conn.execute(
                    sa.text(
                        "SELECT "
                        "(SELECT count(*) FROM disclosure_ops.remote_parse_attempt "
                        " WHERE attempt_id=:target_id) AS target_count,"
                        "(SELECT count(*) FROM disclosure_ops.remote_parse_v4_supersession_link "
                        " WHERE source_attempt_id=:source_id) AS link_count,"
                        "(SELECT count(*) FROM disclosure_ops.remote_parse_v4_checkpoint "
                        " WHERE attempt_id=:source_id AND lifecycle_version=2) "
                        "AS successor_count,"
                        "(SELECT count(*) FROM disclosure_ops.remote_parse_v4_evidence "
                        " WHERE attempt_id=:source_id AND evidence_kind IN "
                        " ('supersession_receipt','cleanup_plan')) AS evidence_count"
                    ),
                    {
                        "source_id": source.attempt_id,
                        "target_id": stage.attempt_id,
                    },
                ).mappings().one()
            finally:
                session.close()
        self.assertEqual(source_after.checkpoint, source.reconciling)
        self.assertEqual(dict(residue), {
            "target_count": 0,
            "link_count": 0,
            "successor_count": 0,
            "evidence_count": 0,
        })

    def test_final_supersession_outer_rollback_preserves_staged_pair(self) -> None:
        source = build_v4_authority_fixture()
        stage = build_v4_supersession_stage_fixture(source)
        staged_creation = V4PreparedCreation(
            checkpoint=stage.prepared,
            reservation=stage.reservation,
            preparation_intent=stage.preparation,
            snapshot_receipt=stage.snapshot,
            parser_target_sha256=stage.parser_target_sha256,
            client_submit_key=stage.client_submit_key,
        )
        with self.engine.begin() as conn:
            insert_core_rows(conn, source)
            session, repository = self._repository(conn)
            try:
                authority = repository.claim(
                    V4HeadExpectation.from_authority(
                        repository.create_prepared(
                            self._prepared_creation(source)
                        )
                    ),
                    owner_identity="worker-supersession-rollback",
                    lease_seconds=120,
                )
                authority = repository.append_successor(
                    self._successor_append(
                        authority,
                        source.reconciling,
                        source.submission,
                    )
                )
                authority = repository.append_successor(
                    V4SuccessorAppend(
                        claim=authority.claim_witness,
                        successor=stage.source_cleanup_pending,
                        new_evidence=tuple(
                            encode_remote_parse_evidence_v4(item)
                            for item in (stage.supersession, stage.cleanup_plan)
                        ),
                        staged_superseder=staged_creation,
                    )
                )
                final_append = self._successor_append(
                    authority,
                    stage.source_superseded,
                    stage.cleanup_receipt,
                )
            finally:
                session.close()

        conn = self.engine.connect()
        transaction = conn.begin()
        session, repository = self._repository(conn)
        try:
            repository.append_successor(final_append)
            transaction.rollback()
        finally:
            session.close()
            conn.close()

        with self.engine.begin() as conn:
            session, repository = self._repository(conn)
            try:
                source_after = repository.load(source.attempt_id)
                target_after = repository.load(stage.attempt_id)
            finally:
                session.close()
        self.assertEqual(source_after.state, "cleanup_pending")
        self.assertTrue(source_after.is_current)
        self.assertEqual(target_after.state, "prepared")
        self.assertFalse(target_after.is_current)

    def test_uow_binds_repository_and_rolls_back_without_explicit_commit(
        self,
    ) -> None:
        fixture = build_v4_authority_fixture()
        pair_source = build_v4_authority_fixture()
        pair = build_v4_resource_free_supersession_fixture(pair_source)
        with self.engine.begin() as conn:
            insert_core_rows(conn, fixture)
            insert_core_rows(conn, pair_source)

        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.remote_parse_v4.create_prepared(self._prepared_creation(fixture))

        with self.engine.connect() as conn:
            count = conn.execute(
                sa.text(
                    "SELECT count(*) FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": fixture.attempt_id},
            ).scalar_one()
        self.assertEqual(count, 0)

        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            created = uow.remote_parse_v4.create_prepared(
                self._prepared_creation(fixture)
            )
            self.assertEqual(created.attempt_id, fixture.attempt_id)
            uow.commit()

        with self.engine.connect() as conn:
            count = conn.execute(
                sa.text(
                    "SELECT count(*) FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": fixture.attempt_id},
            ).scalar_one()
        self.assertEqual(count, 1)

        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.remote_parse_v4.create_resource_free_supersession(
                V4ResourceFreeSupersessionCreation(
                    source_checkpoint=pair.source_superseded,
                    supersession_receipt=pair.supersession,
                    source_parser_target_sha256=pair_source.parser_target_sha256,
                    source_client_submit_key=pair_source.client_submit_key,
                    superseding=V4PreparedCreation(
                        checkpoint=pair.target.prepared,
                        reservation=pair.target.reservation,
                        preparation_intent=pair.target.preparation,
                        snapshot_receipt=pair.target.snapshot,
                        parser_target_sha256=pair.target.parser_target_sha256,
                        client_submit_key=pair.target.client_submit_key,
                    ),
                )
            )

        with self.engine.connect() as conn:
            pair_count = conn.execute(
                sa.text(
                    "SELECT count(*) FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id IN (:source_id,:target_id)"
                ),
                {
                    "source_id": pair.source_superseded.attempt_id,
                    "target_id": pair.target.attempt_id,
                },
            ).scalar_one()
        self.assertEqual(pair_count, 0)

    def test_claim_database_lease_uses_observed_database_time(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
            session, repository = self._repository(conn)
            try:
                expectation = V4HeadExpectation.from_authority(
                    repository.load(fixture.attempt_id)
                )
                claimed = repository.claim(
                    expectation,
                    owner_identity="worker-clock",
                    lease_seconds=15,
                )
            finally:
                session.close()
        self.assertIsNotNone(claimed.database_lease)
        assert claimed.database_lease is not None
        self.assertGreater(claimed.database_lease.remaining_microseconds, 0)
        self.assertLessEqual(
            claimed.database_lease.database_observed_at_utc,
            claimed.database_lease.lease_until_utc,
        )
        self.assertLess(
            claimed.database_lease.database_observed_at_utc,
            datetime.now(UTC) + timedelta(seconds=1),
        )


if __name__ == "__main__":
    unittest.main()
