from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    FUTURE_L2_READER_ROLE,
    READER_ROLE,
)
from disclosure_anchor.application.contracts.remote_parse_checkpoint import (
    AcceptedSubmissionReceipt,
    FailureReceipt,
    LocalMaterializationReceipt,
    PreparedReconcileReceipt,
    RemoteParseAttempt,
    RemoteParseCheckpointConflict,
    RemoteParseResumeSecret,
    TerminalReceipt,
    encode_checkpoint_receipt,
    encode_terminal_receipt,
)
from disclosure_anchor.application.contracts.staged_credit import (
    CreditShapeFacts,
    build_staged_credit_envelope,
    credit_shape,
)
from disclosure_anchor.domain import ids
from tests.integration._support import engine_or_skip
from tests.unit.test_mineru_process_profile import _profile


def _sha(char: str) -> str:
    return "sha256:" + char * 64


_PARSER_TARGET = {"name": "MinerU", "schema": "test-parser-target.v1"}
_PARSER_TARGET_SHA = "sha256:" + hashlib.sha256(
    json.dumps(_PARSER_TARGET, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


class RemoteParseCheckpointIntegrationTests(unittest.TestCase):
    def _v3_rows(self) -> tuple[models.RemoteParseAttempt, models.RemoteParseV3ResumeSecret]:
        envelope = build_staged_credit_envelope(
            profile=_profile(), source_pdf_sha256=_sha("a"),
            source_byte_count=1024, source_page_count=2,
        )
        current = credit_shape("prepared", CreditShapeFacts())
        credits = tuple(current.__dataclass_fields__)
        values = {
            "attempt_id": self.attempt_id,
            "processing_run_id": self.run_id,
            "document_id": self.document_id,
            "attempt_generation": 1,
            "fence_identity": "fence-1",
            "source_pdf_sha256": _sha("a"),
            "parser_target_sha256": _PARSER_TARGET_SHA,
            "request_sha256": _sha("c"),
            "runtime_epoch_sha256": _sha("d"),
            "client_submit_key": "submit-" + self.attempt_id,
            "checkpoint_contract_version": 3,
            "state": "prepared",
            "is_current": True,
            "process_profile_sha256": envelope.process_profile_sha256,
            "credit_policy_sha256": envelope.credit_policy_sha256,
            "reservation_input_sha256": envelope.reservation_input.sha256,
            "reservation_input_bytes": envelope.reservation_input.exact_bytes,
            "reservation_input_byte_count": envelope.reservation_input.byte_count,
            "reservation_source_byte_count": 1024,
            "reservation_source_page_count": 2,
            "reservation_bucket": envelope.reservation_input.value.bucket,
            **{f"reservation_{name}": getattr(envelope.reservation, name) for name in credits},
            **{f"current_{name}": getattr(current, name) for name in credits},
        }
        token = b"v3-prepared-secret"
        return models.RemoteParseAttempt(**values), models.RemoteParseV3ResumeSecret(
            attempt_id=self.attempt_id,
            secret_kind="prepared_reconcile",
            token_bytes=token,
            token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
            token_byte_count=len(token),
        )

    def test_v3_parent_requires_exact_secret_and_closed_credit_shape(self) -> None:
        parent, secret = self._v3_rows()
        with Session(self.engine) as session:
            session.add_all((parent, secret))
            session.commit()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            loaded = uow.remote_parse_attempts.get(self.attempt_id)
            self.assertEqual(loaded.checkpoint_contract_version, 3)
            self.assertEqual(loaded.current_credits.documents, 1)
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM disclosure_ops.remote_parse_v3_resume_secret WHERE attempt_id=:a"), {"a": self.attempt_id})
            conn.execute(text("DELETE FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=:a"), {"a": self.attempt_id})
        for mutation in ("missing_secret", "partial_current", "extra_secret"):
            parent, secret = self._v3_rows()
            if mutation == "partial_current":
                parent.current_documents = None
            with self.subTest(mutation=mutation), Session(self.engine) as session:
                session.add(parent)
                if mutation != "missing_secret":
                    session.add(secret)
                if mutation == "extra_secret":
                    token = b"unexpected-accepted-secret"
                    session.add(models.RemoteParseV3ResumeSecret(
                        attempt_id=self.attempt_id,
                        secret_kind="accepted_submission",
                        token_bytes=token,
                        token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
                        token_byte_count=len(token),
                    ))
                with self.assertRaises(SQLAlchemyError):
                    session.commit()

    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.document_id = ids.new_document_id()
        self.run_id = ids.new_processing_run_id()
        self.attempt_id = "rpa_" + ids.new_ulid()
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO disclosure_core.document (document_id,status) VALUES (:d,'registered')"), {"d": self.document_id})
            conn.execute(text("INSERT INTO disclosure_core.processing_run (processing_run_id,document_id,artifact_owner_processing_run_id,run_kind,status,input_raw_file_hash,provider_document_relpath,parser_target_identity) VALUES (:r,:d,:r,'parse','running',:h,:p,CAST(:t AS jsonb))"), {"r": self.run_id, "d": self.document_id, "h": _sha("a"), "p": "run/provider.json", "t": json.dumps(_PARSER_TARGET)})

    def tearDown(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM disclosure_ops.outbox_event WHERE processing_run_id=:r"), {"r": self.run_id})
            conn.execute(text("DELETE FROM disclosure_core.processing_run WHERE processing_run_id=:r"), {"r": self.run_id})
            conn.execute(text("DELETE FROM disclosure_core.document WHERE document_id=:d"), {"d": self.document_id})
        self.engine.dispose()

    def _attempt(self) -> RemoteParseAttempt:
        return RemoteParseAttempt(
            attempt_id=self.attempt_id, processing_run_id=self.run_id,
            document_id=self.document_id, attempt_generation=1,
            fence_identity="fence-1", source_pdf_sha256=_sha("a"),
            parser_target_sha256=_PARSER_TARGET_SHA, request_sha256=_sha("c"),
            runtime_epoch_sha256=_sha("d"),
            client_submit_key="submit-" + self.attempt_id,
        )

    def _secret(self, kind: str, token: bytes) -> RemoteParseResumeSecret:
        return RemoteParseResumeSecret(
            attempt_id=self.attempt_id, secret_kind=kind,  # type: ignore[arg-type]
            token_bytes=token,
            token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
            token_byte_count=len(token),
        )

    def _prepared_secret(self) -> RemoteParseResumeSecret:
        encoded = encode_checkpoint_receipt(PreparedReconcileReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"),
            client_submit_key="submit-" + self.attempt_id,
            submission_epoch_unix=100, parser_target_sha256=_PARSER_TARGET_SHA,
            request_sha256=_sha("c"), runtime_epoch_sha256=_sha("d"),
        ))
        return self._secret("prepared_reconcile", encoded.exact_bytes)

    def _add_claim(self) -> RemoteParseAttempt:
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            created = uow.remote_parse_attempts.add(self._attempt(), self._prepared_secret())
            claimed = uow.remote_parse_attempts.claim_recovery(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_version=created.row_version,
                owner_identity="worker-boot-1", lease_seconds=120,
            )
            uow.commit()
            return claimed

    def _submitted(self, current: RemoteParseAttempt) -> RemoteParseAttempt:
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            current = uow.remote_parse_attempts.transition(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_state="prepared",
                expected_version=current.row_version,
                next_state="reconciling",
                claim_owner_identity="worker-boot-1",
                claim_generation=current.claim_generation,
            )
            uow.commit()
        token = b"accepted-private-token"
        accepted = encode_checkpoint_receipt(AcceptedSubmissionReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"),
            client_submit_key="submit-" + self.attempt_id,
            submission_epoch_unix=100, remote_task_identity="task-1",
            status_url="http://private/tasks/task-1",
            result_url="http://private/tasks/task-1/result",
            resume_token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
        ))
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            result = uow.remote_parse_attempts.checkpoint_submitted(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_version=current.row_version, remote_task_identity="task-1",
                receipt=accepted,
                accepted_secret=self._secret("accepted_submission", token),
                claim_owner_identity="worker-boot-1",
                claim_generation=current.claim_generation,
            )
            uow.commit()
            return result

    def _terminal(self, current: RemoteParseAttempt) -> RemoteParseAttempt:
        token = b"terminal-private-token"
        receipt = encode_terminal_receipt(TerminalReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"), artifact_owner_identity="owner-1",
            artifact_byte_count=10, artifact_sha256=_sha("e"),
            resume_token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
        ))
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            result = uow.remote_parse_attempts.checkpoint_terminal(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_version=current.row_version, remote_task_identity="task-1",
                receipt=receipt, terminal_secret=self._secret("terminal", token),
                claim_owner_identity="worker-boot-1",
                claim_generation=current.claim_generation,
            )
            uow.commit()
            return result

    def _local_materialized(self) -> RemoteParseAttempt:
        current = self._terminal(self._submitted(self._add_claim()))
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            current = uow.remote_parse_attempts.transition(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_state="remote_terminal",
                expected_version=current.row_version,
                next_state="materializing",
                claim_owner_identity="worker-boot-1",
                claim_generation=current.claim_generation,
            )
            uow.commit()
        local = encode_checkpoint_receipt(LocalMaterializationReceipt(
            attempt_identity=self.attempt_id,
            fence_identity="fence-1",
            claim_generation=current.claim_generation,
            source_pdf_sha256=_sha("a"),
            parser_target_sha256=_PARSER_TARGET_SHA,
            terminal_receipt_sha256=current.terminal_receipt_sha256 or "",
            artifact_owner_identity="owner-1",
            artifact_sha256=_sha("e"),
            artifact_byte_count=10,
            output_manifest_sha256=_sha("f"),
            output_manifest_relpath="run/manifest.json",
            output_manifest_byte_count=20,
            artifact_root_relpath="run/artifacts",
            provider_envelope_relpath="run/provider.json",
            provider_envelope_sha256=_sha("1"),
            provider_envelope_byte_count=30,
            compressed_byte_count=10,
            uncompressed_byte_count=40,
            member_count=2,
            disk_byte_count=50,
            decoded_byte_count=60,
        ))
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            current = uow.remote_parse_attempts.checkpoint_local(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_version=current.row_version,
                claim_owner_identity="worker-boot-1",
                claim_generation=current.claim_generation,
                receipt=local,
            )
            uow.commit()
            return current

    def test_claim_response_loss_and_renewal_are_idempotent(self) -> None:
        claimed = self._add_claim()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            replay = uow.remote_parse_attempts.claim_recovery(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_version=0, owner_identity="worker-boot-1",
                lease_seconds=120,
            )
            self.assertEqual(replay.row_version, claimed.row_version)
            renewed = uow.remote_parse_attempts.renew_recovery_claim(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                owner_identity="worker-boot-1",
                claim_generation=claimed.claim_generation, lease_seconds=120,
            )
            self.assertEqual(renewed.row_version, claimed.row_version)

    def test_expired_claim_takeover_advances_generation_once(self) -> None:
        claimed = self._add_claim()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE disclosure_ops.remote_parse_attempt "
                    "SET claim_lease_until=now() - interval '1 second' "
                    "WHERE attempt_id=:a"
                ),
                {"a": self.attempt_id},
            )
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            taken = uow.remote_parse_attempts.claim_recovery(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_version=claimed.row_version,
                owner_identity="worker-boot-2",
                lease_seconds=120,
            )
            uow.commit()
        self.assertEqual(taken.claim_generation, claimed.claim_generation + 1)
        self.assertEqual(taken.claim_owner_identity, "worker-boot-2")
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaises(
            RemoteParseCheckpointConflict
        ):
            uow.remote_parse_attempts.renew_recovery_claim(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                owner_identity="worker-boot-1",
                claim_generation=claimed.claim_generation,
                lease_seconds=120,
            )

    def test_accepted_receipt_and_secret_roll_back_together(self) -> None:
        current = self._add_claim()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            current = uow.remote_parse_attempts.transition(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_state="prepared",
                expected_version=current.row_version,
                next_state="reconciling",
                claim_owner_identity="worker-boot-1",
                claim_generation=current.claim_generation,
            )
            uow.commit()
        token = b"accepted-private-token"
        accepted = encode_checkpoint_receipt(AcceptedSubmissionReceipt(
            attempt_identity=self.attempt_id,
            fence_identity="fence-1",
            source_pdf_sha256=_sha("a"),
            client_submit_key="submit-" + self.attempt_id,
            submission_epoch_unix=100,
            remote_task_identity="task-1",
            status_url="http://private/tasks/task-1",
            result_url="http://private/tasks/task-1/result",
            resume_token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
        ))
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                uow.remote_parse_attempts.checkpoint_submitted(
                    attempt_id=self.attempt_id,
                    fence_identity="fence-1",
                    expected_version=current.row_version,
                    remote_task_identity="task-1",
                    receipt=accepted,
                    accepted_secret=self._secret("accepted_submission", token),
                    claim_owner_identity="worker-boot-1",
                    claim_generation=current.claim_generation,
                )
                raise RuntimeError("rollback")
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            replay = uow.remote_parse_attempts.get(self.attempt_id)
            assert replay is not None
            self.assertEqual(replay.state, "reconciling")
            self.assertIsNone(replay.submitted_receipt_sha256)
            self.assertIsNone(
                uow.remote_parse_attempts.get_secret(
                    self.attempt_id, "accepted_submission"
                )
            )

    def test_repository_witness_maps_prepared_and_accepted_evidence_exactly(self) -> None:
        claimed = self._add_claim()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            prepared = uow.remote_parse_attempts.durable_checkpoint_witness(
                self.attempt_id
            )
        self.assertEqual(prepared.state, "prepared")
        self.assertEqual(prepared.source_pdf_sha256, _sha("a"))
        self.assertEqual(prepared.parser_target_identity_sha256, _PARSER_TARGET_SHA)
        self.assertEqual(prepared.runtime_bundle_identity_sha256, _sha("d"))
        self.assertEqual(prepared.request_sha256, _sha("c"))
        self.assertEqual(prepared.client_submit_key, "submit-" + self.attempt_id)
        self.assertEqual(prepared.submission_epoch_unix, 100)

        submitted = self._submitted(claimed)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            witness = uow.remote_parse_attempts.durable_checkpoint_witness(
                self.attempt_id
            )
        self.assertEqual(witness.state, "submitted")
        self.assertEqual(
            witness.accepted_submission_receipt_sha256,
            submitted.submitted_receipt_sha256,
        )
        self.assertEqual(witness.remote_task_identity, "task-1")

    def test_success_finish_updates_run_document_and_checkpoint_atomically(self) -> None:
        current = self._local_materialized()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            run = uow.processing_runs.get(self.run_id)
            assert run is not None
            run.status = "succeeded"
            run.finished_at = datetime.now(timezone.utc)
            run.parser_artifact_relpath = "run/artifacts"
            run.provider_document_relpath = "run/provider.json"
            run.artifact_hash = _sha("1")
            result = uow.remote_parse_attempts.finish_run_and_checkpoint(
                finished_run=run, attempt_id=self.attempt_id,
                fence_identity="fence-1", expected_version=current.row_version,
                claim_owner_identity="worker-boot-1",
                claim_generation=current.claim_generation,
            )
            uow.commit()
        self.assertEqual(result.state, "finish_committed")
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            witness = uow.remote_parse_attempts.durable_checkpoint_witness(
                self.attempt_id
            )
        self.assertEqual(witness.state, "finish_committed")
        self.assertIsNotNone(witness.accepted_submission_receipt_sha256)
        self.assertIsNotNone(witness.terminal_receipt_sha256)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT status FROM disclosure_core.document WHERE document_id=:d"), {"d": self.document_id}).scalar_one(), "parsed")

    def test_finish_rejects_processing_run_drift_from_local_receipt(self) -> None:
        current = self._local_materialized()
        mutations = {
            "source": ("input_raw_file_hash", _sha("2")),
            "parser": ("parser_target_identity", {"name": "drift"}),
            "provider_path": ("provider_document_relpath", "run/other.json"),
            "provider_hash": ("artifact_hash", _sha("2")),
            "artifact_root": ("parser_artifact_relpath", "run/other-artifacts"),
        }
        for label, (field, value) in mutations.items():
            with self.subTest(label=label), SqlAlchemyUnitOfWork(
                engine=self.engine
            ) as uow:
                run = uow.processing_runs.get(self.run_id)
                assert run is not None
                run.status = "succeeded"
                run.finished_at = datetime.now(timezone.utc)
                run.parser_artifact_relpath = "run/artifacts"
                run.provider_document_relpath = "run/provider.json"
                run.artifact_hash = _sha("1")
                setattr(run, field, value)
                with self.assertRaisesRegex(
                    RemoteParseCheckpointConflict, "local receipt"
                ):
                    uow.remote_parse_attempts.finish_run_and_checkpoint(
                        finished_run=run,
                        attempt_id=self.attempt_id,
                        fence_identity="fence-1",
                        expected_version=current.row_version,
                        claim_owner_identity="worker-boot-1",
                        claim_generation=current.claim_generation,
                    )
        with self.engine.connect() as conn:
            run_status = conn.execute(
                text(
                    "SELECT status FROM disclosure_core.processing_run "
                    "WHERE processing_run_id=:r"
                ),
                {"r": self.run_id},
            ).scalar_one()
            attempt_state = conn.execute(
                text(
                    "SELECT state FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:a"
                ),
                {"a": self.attempt_id},
            ).scalar_one()
        self.assertEqual(run_status, "running")
        self.assertEqual(attempt_state, "local_materialized")

    def test_pre_submission_not_attempted_closes_run_document_and_outbox(self) -> None:
        current = self._add_claim()
        receipt = encode_checkpoint_receipt(FailureReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            stage="remote", accepted=False, ack_required=False,
            submission_was_attempted=False,
            remote_task_identity=None, claim_generation=current.claim_generation,
            terminal_receipt_sha256=None, error_code="pre_submission_failed",
            error_stage="pre_submit", error_class="pre_submission",
            retryable=True, retry_budget_class="infrastructure",
            message="local preflight failed before the POST call was invoked",
        ))
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            result = uow.remote_parse_attempts.fail_run_and_checkpoint(
                document_id=self.document_id, processing_run_id=self.run_id,
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_state="prepared", expected_version=current.row_version,
                claim_owner_identity="worker-boot-1",
                claim_generation=current.claim_generation, receipt=receipt,
            )
            uow.commit()
        self.assertEqual(result.state, "pre_submission_failed")
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT r.status,d.status,COUNT(o.event_id) FROM disclosure_core.processing_run r JOIN disclosure_core.document d ON d.document_id=r.document_id LEFT JOIN disclosure_ops.outbox_event o ON o.processing_run_id=r.processing_run_id AND o.event_kind='processing_run_failed' WHERE r.processing_run_id=:r GROUP BY r.status,d.status"), {"r": self.run_id}).one()
        self.assertEqual(tuple(row), ("failed", "parse_failed", 1))

    def test_ambiguous_remote_io_remains_recoverable_reconciling(self) -> None:
        current = self._add_claim()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            current = uow.remote_parse_attempts.transition(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_state="prepared",
                expected_version=current.row_version,
                next_state="reconciling",
                claim_owner_identity="worker-boot-1",
                claim_generation=current.claim_generation,
            )
            uow.commit()
        # Simulate process loss immediately after the durable marker, followed
        # by an unavailable prelookup. No final failure method is legal.
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            recovered = uow.remote_parse_attempts.list_recoverable(
                after_attempt_id=None, limit=10
            )
            self.assertEqual([row.attempt_id for row in recovered], [self.attempt_id])
            self.assertEqual(recovered[0].state, "reconciling")
            self.assertEqual(recovered[0].row_version, current.row_version)

    def test_accepted_remote_failure_commits_then_acks_without_budget_drift(self) -> None:
        submitted = self._submitted(self._add_claim())
        receipt = encode_checkpoint_receipt(FailureReceipt(
            attempt_identity=self.attempt_id,
            fence_identity="fence-1",
            stage="remote",
            accepted=True,
            ack_required=True,
            submission_was_attempted=True,
            remote_task_identity="task-1",
            claim_generation=submitted.claim_generation,
            terminal_receipt_sha256=None,
            error_code="cancelled_during_drain",
            error_stage="remote_terminal",
            error_class="remote_terminal",
            retryable=True,
            retry_budget_class="neutral",
            message="shutdown drain observed a terminal cancellation",
        ))
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            committed = uow.remote_parse_attempts.fail_run_and_checkpoint(
                document_id=self.document_id,
                processing_run_id=self.run_id,
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_state="submitted",
                expected_version=submitted.row_version,
                claim_owner_identity="worker-boot-1",
                claim_generation=submitted.claim_generation,
                receipt=receipt,
            )
            uow.commit()
        self.assertEqual(committed.state, "remote_failure_committed")
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            witness = uow.remote_parse_attempts.durable_checkpoint_witness(
                self.attempt_id
            )
        self.assertEqual(witness.state, "remote_failure_committed")
        self.assertIsNotNone(witness.accepted_submission_receipt_sha256)
        self.assertIsNotNone(witness.failure_receipt_sha256)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            final = uow.remote_parse_attempts.transition(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_state="remote_failure_committed",
                expected_version=committed.row_version,
                next_state="remote_failed",
                claim_owner_identity="worker-boot-1",
                claim_generation=committed.claim_generation,
            )
            uow.commit()
        self.assertEqual(final.state, "remote_failed")
        self.assertFalse(final.is_current)
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT r.status,d.status,COUNT(o.event_id) "
                    "FROM disclosure_core.processing_run r "
                    "JOIN disclosure_core.document d ON d.document_id=r.document_id "
                    "LEFT JOIN disclosure_ops.outbox_event o "
                    "ON o.processing_run_id=r.processing_run_id "
                    "AND o.event_kind='processing_run_failed' "
                    "WHERE r.processing_run_id=:r GROUP BY r.status,d.status"
                ),
                {"r": self.run_id},
            ).one()
        self.assertEqual(tuple(row), ("failed", "parse_failed", 1))

    def test_local_failure_committed_witness_retains_all_append_only_receipts(self) -> None:
        terminal = self._terminal(self._submitted(self._add_claim()))
        receipt = encode_checkpoint_receipt(FailureReceipt(
            attempt_identity=self.attempt_id,
            fence_identity="fence-1",
            stage="local",
            accepted=True,
            ack_required=True,
            submission_was_attempted=True,
            remote_task_identity="task-1",
            claim_generation=terminal.claim_generation,
            terminal_receipt_sha256=terminal.terminal_receipt_sha256,
            error_code="materialization_contract",
            error_stage="local_materialization",
            error_class="local_materialization",
            retryable=False,
            retry_budget_class="item",
            message="deterministic local materialization failure",
        ))
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            committed = uow.remote_parse_attempts.fail_run_and_checkpoint(
                document_id=self.document_id,
                processing_run_id=self.run_id,
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_state="remote_terminal",
                expected_version=terminal.row_version,
                claim_owner_identity="worker-boot-1",
                claim_generation=terminal.claim_generation,
                receipt=receipt,
            )
            uow.commit()
        self.assertEqual(committed.state, "local_failure_committed")
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            witness = uow.remote_parse_attempts.durable_checkpoint_witness(
                self.attempt_id
            )
        self.assertEqual(witness.state, "local_failure_committed")
        self.assertIsNotNone(witness.accepted_submission_receipt_sha256)
        self.assertEqual(witness.terminal_receipt_sha256, terminal.terminal_receipt_sha256)
        self.assertEqual(witness.failure_receipt_sha256, receipt.sha256)

    def test_current_v1_checkpoint_blocks_recovery(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO disclosure_ops.remote_parse_attempt (attempt_id,processing_run_id,document_id,attempt_generation,fence_identity,source_pdf_sha256,parser_target_sha256,request_sha256,runtime_epoch_sha256,client_submit_key,checkpoint_contract_version,state,is_current,row_version) VALUES (:a,:r,:d,1,'fence-v1',:s,:p,:q,:e,:k,1,'prepared',true,0)"), {"a": self.attempt_id, "r": self.run_id, "d": self.document_id, "s": _sha("a"), "p": _sha("b"), "q": _sha("c"), "e": _sha("d"), "k": "legacy-" + self.attempt_id})
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(RemoteParseCheckpointConflict, "unsupported current v1"):
            uow.remote_parse_attempts.list_recoverable(after_attempt_id=None, limit=10)

    def test_concurrent_terminal_is_idempotent_and_first_receipt_wins(self) -> None:
        submitted = self._submitted(self._add_claim())
        token = b"terminal-private-token"
        receipt = encode_terminal_receipt(TerminalReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"), artifact_owner_identity="owner-1",
            artifact_byte_count=10, artifact_sha256=_sha("e"),
            resume_token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
        ))

        def checkpoint() -> str:
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                result = uow.remote_parse_attempts.checkpoint_terminal(
                    attempt_id=self.attempt_id, fence_identity="fence-1",
                    expected_version=submitted.row_version,
                    remote_task_identity="task-1", receipt=receipt,
                    terminal_secret=self._secret("terminal", token),
                    claim_owner_identity="worker-boot-1",
                    claim_generation=submitted.claim_generation,
                )
                uow.commit()
                return result.terminal_receipt_sha256 or ""

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(tuple(pool.map(lambda _: checkpoint(), range(2))), (receipt.sha256, receipt.sha256))

    def test_terminal_receipt_and_secret_roll_back_together(self) -> None:
        submitted = self._submitted(self._add_claim())
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                token = b"terminal-private-token"
                uow.remote_parse_attempts.checkpoint_terminal(
                    attempt_id=self.attempt_id, fence_identity="fence-1",
                    expected_version=submitted.row_version,
                    remote_task_identity="task-1",
                    receipt=encode_terminal_receipt(TerminalReceipt(
                        attempt_identity=self.attempt_id, fence_identity="fence-1",
                        source_pdf_sha256=_sha("a"),
                        artifact_owner_identity="owner-1", artifact_byte_count=10,
                        artifact_sha256=_sha("e"),
                        resume_token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
                    )),
                    terminal_secret=self._secret("terminal", token),
                    claim_owner_identity="worker-boot-1",
                    claim_generation=submitted.claim_generation,
                )
                raise RuntimeError("rollback")
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            current = uow.remote_parse_attempts.get(self.attempt_id)
            assert current is not None
            self.assertEqual(current.state, "submitted")
            self.assertIsNone(uow.remote_parse_attempts.get_secret(self.attempt_id, "terminal"))

    def test_private_acl_partial_current_and_projection_drift_fail_closed(self) -> None:
        terminal = self._terminal(self._submitted(self._add_claim()))
        with self.engine.connect() as conn:
            for role in (READER_ROLE, FUTURE_L2_READER_ROLE):
                self.assertFalse(conn.execute(text("SELECT has_table_privilege(:r,'disclosure_ops.remote_parse_attempt','SELECT')"), {"r": role}).scalar_one())
                self.assertFalse(conn.execute(text("SELECT has_table_privilege(:r,'disclosure_ops.remote_parse_v3_resume_secret','SELECT')"), {"r": role}).scalar_one())
            self.assertTrue(conn.execute(text("SELECT has_table_privilege(:r,'disclosure_ops.remote_parse_attempt','SELECT,INSERT,UPDATE,DELETE')"), {"r": APP_ROLE}).scalar_one())
            self.assertTrue(conn.execute(text("SELECT has_table_privilege(:r,'disclosure_ops.remote_parse_v3_resume_secret','SELECT,INSERT,UPDATE,DELETE')"), {"r": APP_ROLE}).scalar_one())
        with self.assertRaises(IntegrityError), self.engine.begin() as conn:
            conn.execute(text("INSERT INTO disclosure_ops.remote_parse_attempt (attempt_id,processing_run_id,document_id,attempt_generation,fence_identity,source_pdf_sha256,parser_target_sha256,request_sha256,runtime_epoch_sha256,client_submit_key,checkpoint_contract_version,state,is_current,row_version) VALUES (:a,:r,:d,2,'fence-2',:s,:p,:q,:e,:k,2,'prepared',true,0)"), {"a": "rpa_" + ids.new_ulid(), "r": self.run_id, "d": self.document_id, "s": _sha("a"), "p": _sha("b"), "q": _sha("c"), "e": _sha("d"), "k": "duplicate-current"})
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE disclosure_ops.remote_parse_attempt SET result_owner_identity='drifted' WHERE attempt_id=:a"), {"a": self.attempt_id})
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(RemoteParseCheckpointConflict, "terminal receipt identity drifted"):
            uow.remote_parse_attempts.get(terminal.attempt_id)


if __name__ == "__main__":
    unittest.main()
