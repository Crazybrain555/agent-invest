"""Scratch-PostgreSQL tests for the V4 coordinator persistence slice."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
import json
import time
from threading import Event
from types import TracebackType
import unittest

import sqlalchemy as sa
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import (
    RemoteParseV4Repository,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import (
    SqlAlchemyUnitOfWork,
    unit_of_work_factory,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.provider_document_admission import (
    SourcePdfObservation,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
    StagedResourceCreditEnvelope,
)
from disclosure_anchor.application.ports.remote_parse_v4_ingress import (
    V4InitialIngressCommit,
    V4InitialIngressDrift,
    V4InitialIngressNotEligible,
)
from disclosure_anchor.application.ports.remote_parse_v4_source_rejection import V4SourceRejectionCommit
from disclosure_anchor.application.ports.staged_new_work_v4 import V4OrdinaryParseCandidate, V4RejectedSourcePdf
from disclosure_anchor.application.worker.queries import pending_parse
from disclosure_anchor.application.ports.remote_parse_v4_failure_committer import (
    V4FinalFailureCommit,
    V4FinalFailureDrift,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    V4PreparedProposal,
    V4SuccessorAppend,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
)
from disclosure_anchor.application.services.staged_ingress_v4 import (
    DurableStagedIngressV4,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorLimits,
    RecoveryDeferred,
    StageLeaseGuard,
)
from disclosure_anchor.domain import ids
from tests.integration._remote_parse_v4_factory import (
    V4AuthorityFixture,
    build_v4_authority_fixture,
    build_v4_resource_free_supersession_fixture,
    insert_checkpoint,
    insert_evidence,
    install_prepared_cycle,
    install_submitted_cycle,
    install_v4_resource_free_supersession,
    update_v4_head,
)
from tests.integration._support import engine_or_skip


def _limits() -> CoordinatorLimits:
    return CoordinatorLimits(
        credits=ResourceCreditVector(
            documents=8,
            snapshot_items=8,
            snapshot_bytes=10_000,
            remote_waits=8,
            provider_tasks=8,
            provider_result_bytes=10_000,
            materialization_items=8,
            compressed_bytes=10_000,
            decoded_bytes=2_000_000,
            temp_disk_bytes=4_000_000,
            output_items=8,
            output_bytes=2_000_000,
            output_pages=1_000,
            ack_items=8,
        ),
        recovery_page_size=2,
        poll_seconds=0.01,
    )


class _CommitResponseLostUnitOfWork:
    def __init__(
        self,
        delegate: SqlAlchemyUnitOfWork,
        lose_once: list[bool],
    ) -> None:
        self._delegate = delegate
        self._lose_once = lose_once

    @property
    def remote_parse_v4(self) -> object:
        return self._delegate.remote_parse_v4

    @property
    def remote_parse_v4_failures(self) -> object:
        return self._delegate.remote_parse_v4_failures

    @property
    def remote_parse_v4_ingress(self) -> object:
        return self._delegate.remote_parse_v4_ingress

    def __enter__(self) -> _CommitResponseLostUnitOfWork:
        self._delegate.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._delegate.__exit__(exc_type, exc, tb)

    def commit(self) -> None:
        self._delegate.commit()
        if self._lose_once[0]:
            self._lose_once[0] = False
            raise RuntimeError("simulated commit response loss")


class StagedCoordinatorPersistenceV4IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self._clean_scratch_rows()

    def tearDown(self) -> None:
        try:
            self._clean_scratch_rows()
        finally:
            self.engine.dispose()

    def _clean_scratch_rows(self) -> None:
        with self.engine.begin() as conn:
            roots = tuple(
                conn.execute(
                    sa.text(
                        "SELECT DISTINCT processing_run_id,document_id "
                        "FROM disclosure_core.processing_run "
                        "WHERE provider_document_relpath='scratch/provider.json'"
                    )
                ).mappings()
            )
            conn.exec_driver_sql(
                "TRUNCATE TABLE disclosure_ops.remote_parse_attempt CASCADE"
            )
            for root in roots:
                conn.execute(
                    sa.text(
                        "DELETE FROM disclosure_ops.outbox_event "
                        "WHERE processing_run_id=:processing_run_id"
                    ),
                    {"processing_run_id": root["processing_run_id"]},
                )
                conn.execute(
                    sa.text(
                        "DELETE FROM disclosure_core.processing_run "
                        "WHERE processing_run_id=:processing_run_id"
                    ),
                    {"processing_run_id": root["processing_run_id"]},
                )
            for root in roots:
                conn.execute(
                    sa.text(
                        "DELETE FROM disclosure_core.document "
                        "WHERE document_id=:document_id"
                    ),
                    {"document_id": root["document_id"]},
                )
            conn.execute(
                sa.text(
                    "DELETE FROM disclosure_core.document "
                    "WHERE provider='cninfo' AND provider_document_id='ingress-doc'"
                )
            )
            conn.execute(
                sa.text(
                    "DELETE FROM disclosure_core.security "
                    "WHERE security_id='sec_ingress-atomic'"
                )
            )
            conn.execute(
                sa.text(
                    "DELETE FROM disclosure_core.company "
                    "WHERE company_id='co_ingress-atomic'"
                )
            )

    def _backend(
        self,
        *,
        owner: str = "worker-integration-boot",
        factory: Callable[[], UnitOfWork] | None = None,
        utc_now: Callable[[], datetime] | None = None,
        outbox_event_id_factory: Callable[[], str] | None = None,
    ) -> DurableStagedCoordinatorPersistenceV4:
        return DurableStagedCoordinatorPersistenceV4(
            uow_factory=factory or unit_of_work_factory(self.engine),
            limits=_limits(),
            owner_identity=owner,
            utc_now=utc_now,
            outbox_event_id_factory=(
                outbox_event_id_factory or ids.new_outbox_event_id
            ),
        )

    @staticmethod
    def _install_remote_failure_ack_pending(
        conn: sa.Connection,
        fixture: V4AuthorityFixture,
    ) -> None:
        install_submitted_cycle(conn, fixture, include_secret=True)
        for evidence, checkpoint in (
            (fixture.remote_failure, fixture.cleanup_pending),
            (fixture.cleanup_plan, None),
            (fixture.cleanup_receipt, fixture.ack_pending),
        ):
            insert_evidence(conn, fixture, evidence)
            if checkpoint is not None:
                insert_checkpoint(conn, fixture, checkpoint)
                update_v4_head(conn, fixture, checkpoint)

    @staticmethod
    def _insert_ingress_document(
        conn: sa.Connection,
        fixture: V4AuthorityFixture,
    ) -> str:
        company_id = "co_ingress-atomic"
        security_id = "sec_ingress-atomic"
        conn.execute(
            sa.text(
                "INSERT INTO disclosure_core.company (company_id,legal_name) "
                "VALUES (:company,'Ingress Atomic Fixture')"
            ),
            {"company": company_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO disclosure_core.security "
                "(security_id,company_id,security_code,exchange) "
                "VALUES (:security,:company,'000001','SZSE')"
            ),
            {"security": security_id, "company": company_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO disclosure_core.document "
                "(document_id,security_id,provider,provider_document_id,"
                "raw_file_relpath,raw_file_hash,status) VALUES "
                "(:document,:security,'cninfo','ingress-doc',"
                "'raw/ingress.pdf',:source,'registered')"
            ),
            {
                "document": fixture.document_id,
                "security": security_id,
                "source": fixture.source_pdf_sha256,
            },
        )
        return security_id

    @staticmethod
    def _ingress_command(
        fixture: V4AuthorityFixture,
        *,
        security_id: str,
        started_at: datetime,
        event_id: str,
    ) -> V4InitialIngressCommit:
        target = ParserTargetIdentity.from_payload(
            json.loads(fixture.parser_target_identity_json)
        )
        return V4InitialIngressCommit(
            proposal=V4PreparedProposal(
                document_id=fixture.document_id,
                processing_run_id=fixture.processing_run_id,
                prepared_submission=fixture.prepared_submission,
                credit_envelope=StagedResourceCreditEnvelope(
                    process_profile_sha256=fixture.process_profile_sha256,
                    credit_policy_sha256=fixture.credit_policy_sha256,
                    reservation_input=fixture.reservation_input,
                    reservation=fixture.reservation.reserved_credit,
                ),
                execution_spec=fixture.execution_spec,
            ),
            source_observation=SourcePdfObservation(
                sha256=fixture.source_pdf_sha256,
                byte_count=100,
                page_count=2,
            ),
            expected_provider="cninfo",
            expected_provider_document_id="ingress-doc",
            expected_security_id=security_id,
            expected_raw_file_relpath="raw/ingress.pdf",
            expected_raw_file_hash=fixture.source_pdf_sha256,
            parser_target=target,
            parser_artifact_relpath="scratch/artifacts",
            provider_document_relpath="scratch/provider.json",
            started_at=started_at,
            created_outbox_event_id=event_id,
            max_retries=3,
            scope_classes=None,
        )

    def _source_rejection_command(self) -> V4SourceRejectionCommit:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            security_id = self._insert_ingress_document(conn, fixture)
        return V4SourceRejectionCommit(
            candidate=V4OrdinaryParseCandidate(
                document_id=fixture.document_id, provider="cninfo", provider_document_id="ingress-doc",
                security_id=security_id, security_code="000001", raw_file_relpath="raw/ingress.pdf",
                raw_file_hash=fixture.source_pdf_sha256, archived_raw_byte_count=100,
            ), rejection=V4RejectedSourcePdf(
                sha256=fixture.source_pdf_sha256, byte_count=100, reason_code="source_pdf_invalid_format",
            ), parser_target=ParserTargetIdentity.from_payload(json.loads(fixture.parser_target_identity_json)),
            processing_run_id=fixture.processing_run_id, parser_artifact_relpath="scratch/artifacts",
            provider_document_relpath="scratch/provider.json", failed_at=datetime(2026, 9, 6, tzinfo=UTC),
            created_outbox_event_id=ids.new_id("evt"), failed_outbox_event_id=ids.new_id("evt"),
            max_retries=3, scope_classes=None,
        )

    def test_source_rejection_response_loss_restart_and_duplicate_episode_are_atomic(self) -> None:
        command = self._source_rejection_command()
        lose_once = [True]
        probes = []

        def guard() -> None:
            probes.append(True)
            if len(probes) > 2:
                raise RuntimeError("ownership lost after commit")

        def factory() -> UnitOfWork:
            return _CommitResponseLostUnitOfWork(
                SqlAlchemyUnitOfWork(engine=self.engine), lose_once,
            )  # type: ignore[return-value]

        DurableStagedIngressV4(uow_factory=factory).reject_source(command, write_guard=guard)
        self.assertFalse(lose_once[0])
        self.assertEqual(len(probes), 2)  # exact read-only winner, no fresh write probe
        restarted = DurableStagedIngressV4(uow_factory=unit_of_work_factory(self.engine))
        restarted.reject_source(command, write_guard=lambda: None)
        with self.assertRaises(V4InitialIngressNotEligible):
            restarted.reject_source(replace(
                command, processing_run_id=ids.new_id("run"),
                created_outbox_event_id=ids.new_id("evt"), failed_outbox_event_id=ids.new_id("evt"),
            ), write_guard=lambda: None)
        with self.engine.connect() as conn:
            self.assertEqual(pending_parse(
                conn, max_retries=3, limit=10, document_ids=(command.candidate.document_id,),
            ), [])
            row = conn.execute(sa.text(
                "SELECT status,error,input_raw_file_hash FROM disclosure_core.processing_run "
                "WHERE processing_run_id=:run"
            ), {"run": command.processing_run_id}).mappings().one()
            self.assertEqual(row["status"], "failed")
            self.assertFalse(row["error"]["retryable"])
            self.assertEqual(row["input_raw_file_hash"], command.rejection.sha256)
            self.assertEqual(conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_ops.outbox_event WHERE processing_run_id=:run"
            ), {"run": command.processing_run_id}).scalar_one(), 2)
            self.assertEqual(conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_ops.remote_parse_attempt WHERE processing_run_id=:run"
            ), {"run": command.processing_run_id}).scalar_one(), 0)

    def test_source_rejection_revocation_before_commit_rolls_back_every_effect(self) -> None:
        command = self._source_rejection_command()
        probes = []

        def guard() -> None:
            probes.append(True)
            if len(probes) >= 2:
                raise RuntimeError("singleton lost")

        with self.assertRaisesRegex(RuntimeError, "singleton lost"):
            DurableStagedIngressV4(uow_factory=unit_of_work_factory(self.engine)).reject_source(
                command, write_guard=guard,
            )
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_core.processing_run WHERE processing_run_id=:run"
            ), {"run": command.processing_run_id}).scalar_one(), 0)
            self.assertEqual(conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_ops.outbox_event WHERE processing_run_id=:run"
            ), {"run": command.processing_run_id}).scalar_one(), 0)
            self.assertEqual(conn.execute(sa.text(
                "SELECT status FROM disclosure_core.document WHERE document_id=:document"
            ), {"document": command.candidate.document_id}).scalar_one(), "registered")
            self.assertEqual(len(pending_parse(
                conn, max_retries=3, limit=10, document_ids=(command.candidate.document_id,),
            )), 1)

    def test_source_rejection_reconcile_rejects_semantic_episode_drift(self) -> None:
        command = self._source_rejection_command()
        ingress = DurableStagedIngressV4(uow_factory=unit_of_work_factory(self.engine))
        ingress.reject_source(command, write_guard=lambda: None)
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "UPDATE disclosure_core.processing_run SET error=jsonb_set(error,'{retryable}','true') "
                "WHERE processing_run_id=:run"
            ), {"run": command.processing_run_id})
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            with self.assertRaisesRegex(V4InitialIngressDrift, "processing run differs"):
                uow.remote_parse_v4_ingress.reconcile_source_rejection(command)

    def test_source_rejection_cannot_bypass_current_source_or_company_scope(self) -> None:
        command = self._source_rejection_command()
        ingress = DurableStagedIngressV4(uow_factory=unit_of_work_factory(self.engine))
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "UPDATE disclosure_core.document SET raw_file_hash=:sha WHERE document_id=:document"
            ), {"sha": "sha256:" + "f" * 64, "document": command.candidate.document_id})
        with self.assertRaisesRegex(V4InitialIngressDrift, "current raw identity"):
            ingress.reject_source(command, write_guard=lambda: None)
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "UPDATE disclosure_core.document SET raw_file_hash=:sha,company_id='co_ingress-atomic' "
                "WHERE document_id=:document"
            ), {"sha": command.candidate.raw_file_hash, "document": command.candidate.document_id})
        with self.assertRaises(V4InitialIngressNotEligible):
            ingress.reject_source(command, write_guard=lambda: None)

    def test_claim_renew_and_one_step_reload_use_durable_authority(self) -> None:
        fixture = build_v4_authority_fixture(attempt_id="rpa_backend_claim")
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
        backend = self._backend()

        candidate = backend.list_recoverable(
            after_attempt_id=None,
            limit=10,
        )[0]
        claimed = backend.claim_recovery(candidate)
        time.sleep(0.01)
        renewed = backend.renew_claim(claimed, lease_seconds=120)

        self.assertEqual(claimed.claim_generation, 1)
        self.assertEqual(claimed.claim_owner_identity, "worker-integration-boot")
        self.assertGreater(
            renewed.lease_expires_monotonic or 0,
            claimed.lease_expires_monotonic or 0,
        )
        with self.engine.begin() as conn:
            session = Session(bind=conn, expire_on_commit=False, future=True)
            try:
                repository = RemoteParseV4Repository(session)
                authority = repository.load(fixture.attempt_id)
                repository.append_successor(
                    V4SuccessorAppend(
                        claim=authority.claim_witness,
                        successor=fixture.reconciling,
                        new_evidence=(
                            encode_remote_parse_evidence_v4(fixture.submission),
                        ),
                    )
                )
            finally:
                session.close()

        reloaded = backend.reload_claim(renewed)
        self.assertEqual(reloaded.state, "reconciling")
        self.assertEqual(reloaded.lifecycle_version, 1)
        self.assertEqual(reloaded.claim_generation, 1)

    def test_live_foreign_claim_is_deferred_without_writing(self) -> None:
        fixture = build_v4_authority_fixture(attempt_id="rpa_backend_foreign")
        with self.engine.begin() as conn:
            install_submitted_cycle(conn, fixture, include_secret=True)
            before = conn.execute(
                sa.text(
                    "SELECT claim_generation,claim_owner_identity,claim_lease_until "
                    "FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": fixture.attempt_id},
            ).one()
        backend = self._backend()
        candidate = backend.list_recoverable(
            after_attempt_id=None,
            limit=10,
        )[0]

        with self.assertRaises(RecoveryDeferred) as raised:
            backend.claim_recovery(candidate)

        self.assertEqual(
            raised.exception.durable_work.claim_owner_identity,
            "worker-test",
        )
        with self.engine.begin() as conn:
            after = conn.execute(
                sa.text(
                    "SELECT claim_generation,claim_owner_identity,claim_lease_until "
                    "FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": fixture.attempt_id},
            ).one()
        self.assertEqual(after, before)

    def test_claim_response_loss_is_closed_by_fresh_database_reload(self) -> None:
        fixture = build_v4_authority_fixture(attempt_id="rpa_backend_loss")
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
        lose_once = [True]

        def response_loss_factory() -> UnitOfWork:
            return _CommitResponseLostUnitOfWork(
                SqlAlchemyUnitOfWork(engine=self.engine),
                lose_once,
            )  # type: ignore[return-value]

        backend = self._backend(factory=response_loss_factory)
        candidate = backend.list_recoverable(
            after_attempt_id=None,
            limit=10,
        )[0]

        claimed = backend.claim_recovery(candidate)

        self.assertFalse(lose_once[0])
        self.assertEqual(claimed.claim_generation, 1)
        self.assertEqual(claimed.claim_owner_identity, "worker-integration-boot")
        with self.engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT claim_generation,claim_owner_identity "
                    "FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": fixture.attempt_id},
            ).one()
        self.assertEqual(tuple(row), (1, "worker-integration-boot"))

    def test_renew_response_loss_is_closed_by_fresh_database_reload(self) -> None:
        fixture = build_v4_authority_fixture(attempt_id="rpa_backend_renew_loss")
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
        backend = self._backend()
        candidate = backend.list_recoverable(
            after_attempt_id=None,
            limit=10,
        )[0]
        claimed = backend.claim_recovery(candidate)
        time.sleep(0.01)
        lose_once = [True]

        def response_loss_factory() -> UnitOfWork:
            return _CommitResponseLostUnitOfWork(
                SqlAlchemyUnitOfWork(engine=self.engine),
                lose_once,
            )  # type: ignore[return-value]

        renewed = self._backend(factory=response_loss_factory).renew_claim(
            claimed,
            lease_seconds=120,
        )

        self.assertFalse(lose_once[0])
        self.assertGreater(
            renewed.lease_expires_monotonic or 0,
            claimed.lease_expires_monotonic or 0,
        )

    def test_final_failure_response_loss_closes_run_document_and_outbox_once(
        self,
    ) -> None:
        fixture = build_v4_authority_fixture(
            attempt_id="rpa_backend_final_failure_loss"
        )
        with self.engine.begin() as conn:
            self._install_remote_failure_ack_pending(conn, fixture)
        ordinary = self._backend(owner="worker-test")
        candidate = ordinary.list_recoverable(
            after_attempt_id=None,
            limit=10,
        )[0]
        work = ordinary.claim_recovery(candidate)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            authority = uow.remote_parse_v4.load(fixture.attempt_id)
        append = V4SuccessorAppend(
            claim=authority.claim_witness,
            successor=fixture.remote_failed,
            new_evidence=(encode_remote_parse_evidence_v4(fixture.ack_receipt),),
        )
        fixed_at = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
        event_id = "evt_v4-final-failure-loss"
        lose_once = [True]

        def response_loss_factory() -> UnitOfWork:
            return _CommitResponseLostUnitOfWork(
                SqlAlchemyUnitOfWork(engine=self.engine),
                lose_once,
            )  # type: ignore[return-value]

        final = self._backend(
            owner="worker-test",
            factory=response_loss_factory,
            utc_now=lambda: fixed_at,
            outbox_event_id_factory=lambda: event_id,
        ).append_successor(
            work, append,
            stage_guard=StageLeaseGuard(
                deadline_monotonic=time.monotonic() + 60, _revoked=Event(), _monotonic=time.monotonic,
            ),
        )

        self.assertFalse(lose_once[0])
        self.assertEqual(final.state, "remote_failed")
        with self.engine.begin() as conn:
            run = conn.execute(
                sa.text(
                    "SELECT status,finished_at,error FROM "
                    "disclosure_core.processing_run WHERE processing_run_id=:run"
                ),
                {"run": fixture.processing_run_id},
            ).mappings().one()
            document_status = conn.execute(
                sa.text(
                    "SELECT status FROM disclosure_core.document "
                    "WHERE document_id=:document"
                ),
                {"document": fixture.document_id},
            ).scalar_one()
            events = conn.execute(
                sa.text(
                    "SELECT event_id,payload FROM disclosure_ops.outbox_event "
                    "WHERE event_id=:event"
                ),
                {"event": event_id},
            ).mappings().all()
            secret_count = conn.execute(
                sa.text(
                    "SELECT count(*) FROM disclosure_ops.remote_parse_v4_secret "
                    "WHERE attempt_id=:attempt"
                ),
                {"attempt": fixture.attempt_id},
            ).scalar_one()

        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["finished_at"], fixed_at)
        self.assertEqual(run["error"]["error_code"], fixture.remote_failure.error_code)
        self.assertEqual(document_status, "parse_failed")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["status"], "failed")
        self.assertEqual(secret_count, 0)

    def test_initial_ingress_response_loss_commits_run_outbox_and_h0_once(
        self,
    ) -> None:
        fixture = build_v4_authority_fixture(
            attempt_id="rpa_backend_initial_ingress_loss"
        )
        with self.engine.begin() as conn:
            security_id = self._insert_ingress_document(conn, fixture)
        started_at = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)
        event_id = "evt_v4-initial-ingress-loss"
        command = self._ingress_command(
            fixture,
            security_id=security_id,
            started_at=started_at,
            event_id=event_id,
        )
        lose_once = [True]

        def response_loss_factory() -> UnitOfWork:
            return _CommitResponseLostUnitOfWork(
                SqlAlchemyUnitOfWork(engine=self.engine),
                lose_once,
            )  # type: ignore[return-value]

        authority = DurableStagedIngressV4(
            uow_factory=response_loss_factory
        ).execute(command, write_guard=lambda: None)
        replayed = DurableStagedIngressV4(
            uow_factory=unit_of_work_factory(self.engine)
        ).execute(command, write_guard=lambda: None)

        self.assertFalse(lose_once[0])
        self.assertEqual(replayed, authority)
        self.assertEqual(authority.state, "prepared")
        self.assertEqual(authority.attempt_generation, 1)
        self.assertIsNone(authority.checkpoint.snapshot_receipt_sha256)
        with self.engine.begin() as conn:
            runs = conn.execute(
                sa.text(
                    "SELECT status,started_at FROM "
                    "disclosure_core.processing_run WHERE processing_run_id=:run"
                ),
                {"run": fixture.processing_run_id},
            ).mappings().all()
            events = conn.execute(
                sa.text(
                    "SELECT event_id,payload FROM disclosure_ops.outbox_event "
                    "WHERE event_id=:event"
                ),
                {"event": event_id},
            ).mappings().all()
            remaining = conn.execute(
                sa.text(
                    "SELECT count(*) FROM disclosure_ops.pending_parse_v1 "
                    "WHERE document_id=:document"
                ),
                {"document": fixture.document_id},
            ).scalar_one()

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "running")
        self.assertEqual(runs[0]["started_at"], started_at)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["status"], "running")
        self.assertEqual(remaining, 0)

    def test_initial_ingress_rejects_source_observation_drift_before_write(
        self,
    ) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            security_id = self._insert_ingress_document(conn, fixture)
        command = self._ingress_command(
            fixture,
            security_id=security_id,
            started_at=datetime(2026, 9, 4, 9, 5, tzinfo=UTC),
            event_id="evt_v4-source-observation-drift",
        )

        with self.assertRaisesRegex(ValueError, "immutable identities"):
            replace(
                command,
                source_observation=replace(
                    command.source_observation,
                    byte_count=command.source_observation.byte_count + 1,
                ),
            )

    def test_initial_ingress_cannot_bypass_inactive_company_scope(self) -> None:
        fixture = build_v4_authority_fixture()
        with self.engine.begin() as conn:
            security_id = self._insert_ingress_document(conn, fixture)
            conn.execute(
                sa.text(
                    "UPDATE disclosure_core.document SET company_id="
                    "'co_ingress-atomic' WHERE document_id=:document"
                ),
                {"document": fixture.document_id},
            )
        command = self._ingress_command(
            fixture,
            security_id=security_id,
            started_at=datetime(2026, 9, 4, 9, 10, tzinfo=UTC),
            event_id="evt_v4-inactive-company",
        )

        with self.assertRaises(V4InitialIngressNotEligible):
            DurableStagedIngressV4(
                uow_factory=unit_of_work_factory(self.engine)
            ).execute(command, write_guard=lambda: None)

        with self.engine.begin() as conn:
            self.assertEqual(
                conn.execute(
                    sa.text(
                        "SELECT count(*) FROM disclosure_core.processing_run "
                        "WHERE processing_run_id=:run"
                    ),
                    {"run": fixture.processing_run_id},
                ).scalar_one(),
                0,
            )

    def test_final_failure_reconcile_rejects_missing_document_projection(
        self,
    ) -> None:
        fixture = build_v4_authority_fixture(
            attempt_id="rpa_backend_final_failure_document_drift"
        )
        with self.engine.begin() as conn:
            self._install_remote_failure_ack_pending(conn, fixture)
        backend = self._backend(owner="worker-test")
        candidate = backend.list_recoverable(
            after_attempt_id=None,
            limit=10,
        )[0]
        backend.claim_recovery(candidate)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            authority = uow.remote_parse_v4.load(fixture.attempt_id)
        command = V4FinalFailureCommit(
            append=V4SuccessorAppend(
                claim=authority.claim_witness,
                successor=fixture.remote_failed,
                new_evidence=(
                    encode_remote_parse_evidence_v4(fixture.ack_receipt),
                ),
            ),
            failed_at=datetime(2026, 9, 4, 10, 30, tzinfo=UTC),
            outbox_event_id="evt_v4-final-failure-document-drift",
        )
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.remote_parse_v4_failures.commit(command)
            uow.commit()
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE disclosure_core.document SET status='registered' "
                    "WHERE document_id=:document"
                ),
                {"document": fixture.document_id},
            )

        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            with self.assertRaisesRegex(
                V4FinalFailureDrift,
                "document differs",
            ):
                uow.remote_parse_v4_failures.reconcile(command)

    def test_admission_claims_runtime_activated_generation_zero_superseder(self) -> None:
        source = build_v4_authority_fixture(attempt_id="rpa_backend_source")
        supersession = build_v4_resource_free_supersession_fixture(source)
        with self.engine.begin() as conn:
            install_v4_resource_free_supersession(conn, supersession)
        backend = self._backend()

        blocked = backend.admit_new(
            limit=2,
            available_credits=ResourceCreditVector(),
        )
        self.assertEqual(blocked.work, ())
        self.assertTrue(blocked.backlog_exists)
        self.assertIn("documents", blocked.blocked_dimensions)
        with self.engine.begin() as conn:
            untouched_generation = conn.execute(
                sa.text(
                    "SELECT claim_generation FROM "
                    "disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": supersession.target.attempt_id},
            ).scalar_one()
        self.assertEqual(untouched_generation, 0)

        admitted = backend.admit_new(
            limit=2,
            available_credits=_limits().credits,
        )

        self.assertEqual(
            tuple(item.attempt_id for item in admitted.work),
            (supersession.target.attempt_id,),
        )
        self.assertFalse(admitted.backlog_exists)
        self.assertEqual(admitted.work[0].claim_generation, 1)
        self.assertEqual(
            admitted.work[0].claim_owner_identity,
            "worker-integration-boot",
        )
        second = backend.admit_new(
            limit=2,
            available_credits=_limits().credits,
        )
        self.assertEqual(second.work, ())
        self.assertFalse(second.backlog_exists)


if __name__ == "__main__":
    unittest.main()
