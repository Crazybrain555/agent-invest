from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

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
    LocalMaterializationReceiptV2,
    PreparedMaterializationReceiptV2,
    PreparedReconcileReceipt,
    RemoteParseAttempt,
    RemoteParseCheckpointConflict,
    RemoteParseResumeSecret,
    TerminalReceipt,
    encode_checkpoint_receipt,
    encode_terminal_receipt,
)
from disclosure_anchor.application.contracts.staged_credit import (
    CreditVector,
    CreditShapeFacts,
    build_staged_credit_envelope,
    credit_shape,
)
from disclosure_anchor.application.ports.repositories import CreditTransitionGrant
from disclosure_anchor.application.ports.staged_provider_parser import (
    encode_provider_ack_completion_witness,
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
        token = encode_checkpoint_receipt(PreparedReconcileReceipt(
            attempt_identity=self.attempt_id,
            fence_identity="fence-1",
            source_pdf_sha256=_sha("a"),
            client_submit_key="submit-" + self.attempt_id,
            submission_epoch_unix=100,
            parser_target_sha256=_PARSER_TARGET_SHA,
            request_sha256=_sha("c"),
            runtime_epoch_sha256=_sha("d"),
        )).exact_bytes
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
        for mutation in (
            "missing_secret", "partial_current", "extra_secret",
            "arbitrary_prepared", "wrong_prepared_field", "noncanonical_prepared",
        ):
            parent, secret = self._v3_rows()
            if mutation == "partial_current":
                parent.current_documents = None
            if mutation == "arbitrary_prepared":
                secret.token_bytes = b"{}"
                secret.token_sha256 = "sha256:" + hashlib.sha256(b"{}").hexdigest()
                secret.token_byte_count = 2
            elif mutation == "wrong_prepared_field":
                wrong = encode_checkpoint_receipt(PreparedReconcileReceipt(
                    attempt_identity=self.attempt_id, fence_identity="wrong-fence",
                    source_pdf_sha256=_sha("a"),
                    client_submit_key="submit-" + self.attempt_id,
                    submission_epoch_unix=100, parser_target_sha256=_PARSER_TARGET_SHA,
                    request_sha256=_sha("c"), runtime_epoch_sha256=_sha("d"),
                )).exact_bytes
                secret.token_bytes = wrong
                secret.token_sha256 = "sha256:" + hashlib.sha256(wrong).hexdigest()
                secret.token_byte_count = len(wrong)
            elif mutation == "noncanonical_prepared":
                noncanonical = b" " + bytes(secret.token_bytes)
                secret.token_bytes = noncanonical
                secret.token_sha256 = "sha256:" + hashlib.sha256(noncanonical).hexdigest()
                secret.token_byte_count = len(noncanonical)
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

    def test_v3_accepted_and_terminal_secrets_bind_receipt_token_sha(self) -> None:
        for stage in ("submitted", "remote_terminal"):
            parent, prepared_secret = self._v3_rows()
            accepted_token = b"accepted-v3-token"
            accepted = encode_checkpoint_receipt(AcceptedSubmissionReceipt(
                attempt_identity=self.attempt_id,
                fence_identity="fence-1",
                source_pdf_sha256=_sha("a"),
                client_submit_key="submit-" + self.attempt_id,
                submission_epoch_unix=100,
                remote_task_identity="task-v3",
                status_url="http://private/tasks/task-v3",
                result_url="http://private/tasks/task-v3/result",
                resume_token_sha256="sha256:" + hashlib.sha256(accepted_token).hexdigest(),
            ))
            parent.state = stage
            parent.row_version = 2 if stage == "remote_terminal" else 1
            parent.claim_generation = 1
            parent.claim_owner_identity = "worker-v3"
            parent.claim_lease_until = datetime.now(timezone.utc) + timedelta(minutes=1)
            parent.remote_task_identity = "task-v3"
            parent.submitted_receipt_sha256 = accepted.sha256
            parent.submitted_receipt_bytes = accepted.exact_bytes
            parent.submitted_receipt_byte_count = accepted.byte_count
            parent.current_remote_waits = 1 if stage == "submitted" else 0
            accepted_secret = models.RemoteParseV3ResumeSecret(
                attempt_id=self.attempt_id,
                secret_kind="accepted_submission",
                token_bytes=b"wrong-accepted-token",
                token_sha256="sha256:" + hashlib.sha256(b"wrong-accepted-token").hexdigest(),
                token_byte_count=len(b"wrong-accepted-token"),
            )
            secrets = [prepared_secret, accepted_secret]
            if stage == "remote_terminal":
                terminal_token = b"terminal-v3-token"
                terminal = encode_terminal_receipt(TerminalReceipt(
                    attempt_identity=self.attempt_id,
                    fence_identity="fence-1",
                    source_pdf_sha256=_sha("a"),
                    artifact_owner_identity="owner-v3",
                    artifact_byte_count=10,
                    artifact_sha256=_sha("e"),
                    resume_token_sha256="sha256:" + hashlib.sha256(terminal_token).hexdigest(),
                ))
                parent.terminal_receipt_sha256 = terminal.sha256
                parent.terminal_receipt_bytes = terminal.exact_bytes
                parent.terminal_receipt_byte_count = terminal.byte_count
                parent.result_owner_identity = "owner-v3"
                parent.result_artifact_sha256 = _sha("e")
                parent.result_artifact_bytes = 10
                parent.current_retained_results = 1
                parent.current_retained_bytes = 10
                secrets.append(models.RemoteParseV3ResumeSecret(
                    attempt_id=self.attempt_id,
                    secret_kind="terminal",
                    token_bytes=b"wrong-terminal-token",
                    token_sha256="sha256:" + hashlib.sha256(b"wrong-terminal-token").hexdigest(),
                    token_byte_count=len(b"wrong-terminal-token"),
                ))
            with self.subTest(stage=stage), Session(self.engine) as session:
                session.add(parent)
                session.add_all(secrets)
                with self.assertRaises(SQLAlchemyError):
                    session.commit()

    def test_v3_materialization_partial_null_is_rejected_by_database(self) -> None:
        parent, prepared_secret = self._v3_rows()
        accepted_token = b"accepted-v3-token"
        terminal_token = b"terminal-v3-token"
        materialization_token = b"materialization-v3-token"
        accepted = encode_checkpoint_receipt(AcceptedSubmissionReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"),
            client_submit_key="submit-" + self.attempt_id,
            submission_epoch_unix=100, remote_task_identity="task-v3",
            status_url="http://private/tasks/task-v3",
            result_url="http://private/tasks/task-v3/result",
            resume_token_sha256="sha256:" + hashlib.sha256(accepted_token).hexdigest(),
        ))
        terminal = encode_terminal_receipt(TerminalReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"), artifact_owner_identity="owner-v3",
            artifact_byte_count=10, artifact_sha256=_sha("e"),
            resume_token_sha256="sha256:" + hashlib.sha256(terminal_token).hexdigest(),
        ))
        materialization = encode_checkpoint_receipt(PreparedMaterializationReceiptV2(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"), source_page_count=2,
            terminal_receipt_sha256=terminal.sha256,
            process_profile_sha256=parent.process_profile_sha256 or "",
            credit_policy_sha256=parent.credit_policy_sha256 or "",
            reservation_input_sha256=parent.reservation_input_sha256 or "",
            spool_relpath="attempt/result.zip", spool_sha256=_sha("e"),
            spool_byte_count=10, compressed_byte_count=10,
            uncompressed_byte_count=20, member_count=1,
            temporary_disk_byte_count=30, decoded_byte_count=20,
            private_token_sha256="sha256:" + hashlib.sha256(materialization_token).hexdigest(),
        ))
        parent.state = "materializing"
        parent.row_version = 3
        parent.claim_generation = 1
        parent.claim_owner_identity = "worker-v3"
        parent.claim_lease_until = datetime.now(timezone.utc) + timedelta(minutes=1)
        parent.remote_task_identity = "task-v3"
        parent.submitted_receipt_sha256 = accepted.sha256
        parent.submitted_receipt_bytes = accepted.exact_bytes
        parent.submitted_receipt_byte_count = accepted.byte_count
        parent.terminal_receipt_sha256 = terminal.sha256
        parent.terminal_receipt_bytes = terminal.exact_bytes
        parent.terminal_receipt_byte_count = terminal.byte_count
        parent.result_owner_identity = "owner-v3"
        parent.result_artifact_sha256 = _sha("e")
        parent.result_artifact_bytes = 10
        parent.materialization_receipt_sha256 = materialization.sha256
        parent.materialization_receipt_bytes = materialization.exact_bytes
        parent.materialization_receipt_byte_count = materialization.byte_count
        parent.materialization_source_page_count = 2
        parent.materialization_spool_relpath = "attempt/result.zip"
        parent.materialization_spool_sha256 = _sha("e")
        parent.materialization_spool_byte_count = 10
        parent.materialization_compressed_byte_count = 10
        parent.materialization_uncompressed_byte_count = 20
        parent.materialization_temp_disk_byte_count = 30
        parent.materialization_decoded_byte_count = 20
        parent.materialization_member_count = 1
        parent.materialization_token_sha256 = "sha256:" + hashlib.sha256(materialization_token).hexdigest()
        shape = credit_shape("materializing", CreditShapeFacts(
            terminal_byte_count=10, compressed_byte_count=10,
            uncompressed_byte_count=20, decoded_byte_count=20,
            temporary_disk_byte_count=30, source_page_count=2,
            materialization_prepared=True,
        ))
        for name in shape.__dataclass_fields__:
            setattr(parent, f"current_{name}", getattr(shape, name))
        secrets = [prepared_secret]
        for kind, token in (
            ("accepted_submission", accepted_token),
            ("terminal", terminal_token),
            ("materialization", materialization_token),
        ):
            secrets.append(models.RemoteParseV3ResumeSecret(
                attempt_id=self.attempt_id, secret_kind=kind,
                token_bytes=token,
                token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
                token_byte_count=len(token),
            ))
        with Session(self.engine) as session:
            session.add(parent)
            session.add_all(secrets)
            session.commit()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            self.assertEqual(
                uow.remote_parse_attempts.get(self.attempt_id).state,
                "materializing",
            )
        with self.assertRaises(SQLAlchemyError), self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE disclosure_ops.remote_parse_attempt SET materialization_receipt_sha256=NULL WHERE attempt_id=:a"
            ), {"a": self.attempt_id})
        drift_updates = ("materialization_source_page_count=3",)
        for update in drift_updates:
            with self.subTest(update=update), self.engine.begin() as conn:
                conn.execute(text(
                    f"UPDATE disclosure_ops.remote_parse_attempt SET {update} WHERE attempt_id=:a"
                ), {"a": self.attempt_id})
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
                RemoteParseCheckpointConflict, "prepared materialization projection"
            ):
                uow.remote_parse_attempts.get(self.attempt_id)
            with self.engine.begin() as conn:
                conn.execute(text(
                    "UPDATE disclosure_ops.remote_parse_attempt SET materialization_source_page_count=2, materialization_spool_byte_count=10, materialization_compressed_byte_count=10, current_compressed_bytes=10 WHERE attempt_id=:a"
                ), {"a": self.attempt_id})

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

    def _v3_attempt_and_secret(
        self,
    ) -> tuple[RemoteParseAttempt, RemoteParseResumeSecret]:
        envelope = build_staged_credit_envelope(
            profile=_profile(),
            source_pdf_sha256=_sha("a"),
            source_byte_count=1024,
            source_page_count=2,
        )
        attempt = RemoteParseAttempt(
            attempt_id=self.attempt_id,
            processing_run_id=self.run_id,
            document_id=self.document_id,
            attempt_generation=1,
            fence_identity="fence-1",
            source_pdf_sha256=_sha("a"),
            parser_target_sha256=_PARSER_TARGET_SHA,
            request_sha256=_sha("c"),
            runtime_epoch_sha256=_sha("d"),
            client_submit_key="submit-" + self.attempt_id,
            checkpoint_contract_version=3,
            process_profile_sha256=envelope.process_profile_sha256,
            credit_policy_sha256=envelope.credit_policy_sha256,
            reservation_input_bytes=envelope.reservation_input.exact_bytes,
            reservation_input_sha256=envelope.reservation_input.sha256,
            reservation_input_byte_count=envelope.reservation_input.byte_count,
            reservation_source_byte_count=1024,
            reservation_source_page_count=2,
            reservation_bucket=envelope.reservation_input.value.bucket,
            reservation=envelope.reservation,
            current_credits=credit_shape("prepared", CreditShapeFacts()),
        )
        encoded = encode_checkpoint_receipt(PreparedReconcileReceipt(
            attempt_identity=self.attempt_id,
            fence_identity="fence-1",
            source_pdf_sha256=_sha("a"),
            client_submit_key="submit-" + self.attempt_id,
            submission_epoch_unix=100,
            parser_target_sha256=_PARSER_TARGET_SHA,
            request_sha256=_sha("c"),
            runtime_epoch_sha256=_sha("d"),
        ))
        return attempt, RemoteParseResumeSecret(
            attempt_id=self.attempt_id,
            secret_kind="prepared_reconcile",
            token_bytes=encoded.exact_bytes,
            token_sha256=encoded.sha256,
            token_byte_count=encoded.byte_count,
            secret_contract_version=3,
        )

    def _v3_claimed_attempt(self) -> RemoteParseAttempt:
        attempt, secret = self._v3_attempt_and_secret()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            created = uow.remote_parse_attempts.add_v3_prepared(attempt, secret)
            assert created.current_credits is not None
            claimed = uow.remote_parse_attempts.claim_v3_recovery(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_state="prepared", expected_version=0,
                expected_current=created.current_credits,
                owner_identity="worker-v3-a", lease_seconds=60,
            ).attempt
            uow.commit()
            return claimed

    def _v3_submitted_attempt(self) -> RemoteParseAttempt:
        claimed = self._v3_claimed_attempt()
        assert claimed.reservation is not None
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            reconciling = uow.remote_parse_attempts.transition_v3_reconciling(
                expected_attempt=claimed,
                grant=CreditTransitionGrant(
                    expected_current=claimed.current_credits,
                    maximum_positive_delta=claimed.reservation,
                ),
            ).attempt
            uow.commit()
        token = b"v3-accepted-helper"
        receipt = encode_checkpoint_receipt(AcceptedSubmissionReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"),
            client_submit_key="submit-" + self.attempt_id,
            submission_epoch_unix=100, remote_task_identity="task-v3",
            status_url="http://private/tasks/task-v3",
            result_url="http://private/tasks/task-v3/result",
            resume_token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
        ))
        secret = RemoteParseResumeSecret(
            attempt_id=self.attempt_id, secret_kind="accepted_submission",
            token_bytes=token,
            token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
            token_byte_count=len(token), secret_contract_version=3,
        )
        assert reconciling.reservation is not None
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            submitted = uow.remote_parse_attempts.checkpoint_v3_submitted(
                expected_attempt=reconciling,
                grant=CreditTransitionGrant(
                    expected_current=reconciling.current_credits,
                    maximum_positive_delta=reconciling.reservation,
                ), receipt=receipt, accepted_secret=secret,
            ).attempt
            uow.commit()
            return submitted

    def _v3_remote_terminal_attempt(self) -> RemoteParseAttempt:
        submitted = self._v3_submitted_attempt()
        token = b"v3-terminal-helper"
        receipt = encode_terminal_receipt(TerminalReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"), artifact_owner_identity="owner-v3",
            artifact_byte_count=10, artifact_sha256=_sha("e"),
            resume_token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
        ))
        secret = RemoteParseResumeSecret(
            attempt_id=self.attempt_id, secret_kind="terminal",
            token_bytes=token,
            token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
            token_byte_count=len(token), secret_contract_version=3,
        )
        assert submitted.reservation is not None
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            terminal = uow.remote_parse_attempts.checkpoint_v3_terminal(
                expected_attempt=submitted,
                grant=CreditTransitionGrant(
                    expected_current=submitted.current_credits,
                    maximum_positive_delta=submitted.reservation,
                ), receipt=receipt, terminal_secret=secret,
            ).attempt
            uow.commit()
            return terminal

    def _v3_materializing_attempt(self) -> RemoteParseAttempt:
        terminal = self._v3_remote_terminal_attempt()
        token = b"v3-materialization-helper"
        receipt = encode_checkpoint_receipt(PreparedMaterializationReceiptV2(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"), source_page_count=2,
            terminal_receipt_sha256=terminal.terminal_receipt_sha256 or "",
            process_profile_sha256=terminal.process_profile_sha256 or "",
            credit_policy_sha256=terminal.credit_policy_sha256 or "",
            reservation_input_sha256=terminal.reservation_input_sha256 or "",
            spool_relpath="attempt/result.zip", spool_sha256=_sha("e"),
            spool_byte_count=10, compressed_byte_count=10,
            uncompressed_byte_count=20, member_count=1,
            temporary_disk_byte_count=30, decoded_byte_count=20,
            private_token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
        ))
        secret = RemoteParseResumeSecret(
            attempt_id=self.attempt_id, secret_kind="materialization",
            token_bytes=token,
            token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
            token_byte_count=len(token), secret_contract_version=3,
        )
        assert terminal.reservation is not None
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            result = uow.remote_parse_attempts.prepare_v3_materialization(
                expected_attempt=terminal,
                grant=CreditTransitionGrant(
                    expected_current=terminal.current_credits,
                    maximum_positive_delta=terminal.reservation,
                ), receipt=receipt, materialization_secret=secret,
            ).attempt
            uow.commit()
            return result

    def test_v3_repository_add_list_claim_renew_reload_and_response_loss(self) -> None:
        attempt, secret = self._v3_attempt_and_secret()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            created = uow.remote_parse_attempts.add_v3_prepared(attempt, secret)
            uow.commit()
        self.assertEqual(created.checkpoint_contract_version, 3)
        assert created.current_credits is not None

        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            page = uow.remote_parse_attempts.list_v3_recoverable(
                after_attempt_id=None, limit=10
            )
            self.assertEqual([item.attempt_id for item in page], [self.attempt_id])
            claimed = uow.remote_parse_attempts.claim_v3_recovery(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_state="prepared",
                expected_version=0,
                expected_current=created.current_credits,
                owner_identity="worker-v3-a",
                lease_seconds=30,
            )
            uow.commit()
        self.assertEqual(claimed.attempt.row_version, 1)
        self.assertEqual(claimed.attempt.claim_generation, 1)
        self.assertEqual(claimed.database_lease.remaining_microseconds, 30_000_000)

        with self.engine.connect() as conn:
            lease_before_rejections = conn.execute(text(
                "SELECT claim_lease_until FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=:a"
            ), {"a": self.attempt_id}).scalar_one()
        for rejected_version, error_type in (
            (1, RemoteParseCheckpointConflict),
            (2, RemoteParseCheckpointConflict),
            (-1, ValueError),
            (True, ValueError),
            ((1 << 63) - 1, ValueError),
        ):
            with self.subTest(rejected_version=rejected_version), SqlAlchemyUnitOfWork(
                engine=self.engine
            ) as uow, self.assertRaises(error_type):
                uow.remote_parse_attempts.claim_v3_recovery(
                    attempt_id=self.attempt_id,
                    fence_identity="fence-1",
                    expected_state="prepared",
                    expected_version=rejected_version,
                    expected_current=created.current_credits,
                    owner_identity="worker-v3-a",
                    lease_seconds=300,
                )
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT claim_lease_until FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=:a"
            ), {"a": self.attempt_id}).scalar_one(), lease_before_rejections)

        # A committed claim whose response was lost is replayable with the old
        # expected version without advancing either semantic fence.
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            replayed = uow.remote_parse_attempts.claim_v3_recovery(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_state="prepared",
                expected_version=0,
                expected_current=created.current_credits,
                owner_identity="worker-v3-a",
                lease_seconds=30,
            )
            renewed = uow.remote_parse_attempts.renew_v3_claim(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_state="prepared",
                expected_version=1,
                expected_current=created.current_credits,
                owner_identity="worker-v3-a",
                claim_generation=1,
                lease_seconds=40,
            )
            reloaded = uow.remote_parse_attempts.reload_v3_claim(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_state="prepared",
                expected_version=1,
                expected_current=created.current_credits,
                owner_identity="worker-v3-a",
                claim_generation=1,
            )
            uow.commit()
        self.assertEqual((replayed.attempt.row_version, replayed.attempt.claim_generation), (1, 1))
        self.assertEqual(renewed.attempt.row_version, 1)
        self.assertGreater(renewed.database_lease.remaining_microseconds, 39_000_000)
        self.assertGreater(reloaded.database_lease.remaining_microseconds, 0)

        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE disclosure_ops.remote_parse_attempt SET row_version=3 WHERE attempt_id=:a"
            ), {"a": self.attempt_id})
        with self.engine.connect() as conn:
            lease_before_stale = conn.execute(text(
                "SELECT claim_lease_until FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=:a"
            ), {"a": self.attempt_id}).scalar_one()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
            RemoteParseCheckpointConflict, "exact CAS"
        ):
            uow.remote_parse_attempts.claim_v3_recovery(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_state="prepared", expected_version=0,
                expected_current=created.current_credits,
                owner_identity="worker-v3-a", lease_seconds=300,
            )
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT claim_lease_until FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=:a"
            ), {"a": self.attempt_id}).scalar_one(), lease_before_stale)

    def test_v3_claim_integer_inputs_fail_before_sql(self) -> None:
        attempt, secret = self._v3_attempt_and_secret()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            created = uow.remote_parse_attempts.add_v3_prepared(attempt, secret)
            assert created.current_credits is not None
            claimed = uow.remote_parse_attempts.claim_v3_recovery(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_state="prepared", expected_version=0,
                expected_current=created.current_credits,
                owner_identity="worker-v3-a", lease_seconds=30,
            )
            uow.commit()
        invalid_calls = (
            ("renew-version", lambda repo: repo.renew_v3_claim(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_state="prepared", expected_version=-1,
                expected_current=created.current_credits,
                owner_identity="worker-v3-a", claim_generation=1, lease_seconds=30,
            )),
            ("renew-generation", lambda repo: repo.renew_v3_claim(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_state="prepared", expected_version=claimed.attempt.row_version,
                expected_current=created.current_credits,
                owner_identity="worker-v3-a", claim_generation=True, lease_seconds=30,
            )),
            ("renew-lease", lambda repo: repo.renew_v3_claim(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_state="prepared", expected_version=claimed.attempt.row_version,
                expected_current=created.current_credits,
                owner_identity="worker-v3-a", claim_generation=1,
                lease_seconds=(1 << 63),
            )),
            ("reload-version", lambda repo: repo.reload_v3_claim(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_state="prepared", expected_version=(1 << 63),
                expected_current=created.current_credits,
                owner_identity="worker-v3-a", claim_generation=1,
            )),
            ("reload-generation", lambda repo: repo.reload_v3_claim(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_state="prepared", expected_version=claimed.attempt.row_version,
                expected_current=created.current_credits,
                owner_identity="worker-v3-a", claim_generation=-1,
            )),
        )
        for label, call in invalid_calls:
            with self.subTest(label=label), SqlAlchemyUnitOfWork(
                engine=self.engine
            ) as uow, self.assertRaises(ValueError):
                call(uow.remote_parse_attempts)

    def test_v3_lifecycle_receipts_grants_and_race_reconciliation(self) -> None:
        attempt, secret = self._v3_attempt_and_secret()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            created = uow.remote_parse_attempts.add_v3_prepared(attempt, secret)
            assert created.current_credits is not None
            claimed = uow.remote_parse_attempts.claim_v3_recovery(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_state="prepared", expected_version=0,
                expected_current=created.current_credits,
                owner_identity="worker-v3-a", lease_seconds=60,
            ).attempt
            assert claimed.reservation is not None
            reconciling = uow.remote_parse_attempts.transition_v3_reconciling(
                expected_attempt=claimed,
                grant=CreditTransitionGrant(
                    expected_current=claimed.current_credits,
                    maximum_positive_delta=claimed.reservation,
                ),
            ).attempt
            uow.commit()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            replay = uow.remote_parse_attempts.reconcile_v3_reconciling_after_race(
                expected_attempt=claimed,
            )
        self.assertEqual(replay.attempt, reconciling)
        for wrong_operation_name in (
            "reconcile_v3_submitted_after_race",
            "reconcile_v3_terminal_after_race",
            "reconcile_v3_materialization_after_race",
        ):
            with SqlAlchemyUnitOfWork(engine=self.engine) as wrong_uow, self.assertRaisesRegex(
                ValueError, "expected state"
            ):
                getattr(wrong_uow.remote_parse_attempts, wrong_operation_name)(
                    expected_attempt=claimed
                )
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE disclosure_ops.remote_parse_attempt SET row_version=row_version+1 WHERE attempt_id=:a"
            ), {"a": self.attempt_id})
        with SqlAlchemyUnitOfWork(engine=self.engine) as gap_uow, self.assertRaisesRegex(
            RemoteParseCheckpointConflict, "lost exact projection"
        ):
            gap_uow.remote_parse_attempts.reconcile_v3_reconciling_after_race(
                expected_attempt=claimed
            )
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE disclosure_ops.remote_parse_attempt SET row_version=:v WHERE attempt_id=:a"
            ), {"a": self.attempt_id, "v": reconciling.row_version})

        accepted_token = b"v3-accepted-token"
        accepted_receipt = encode_checkpoint_receipt(AcceptedSubmissionReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"),
            client_submit_key="submit-" + self.attempt_id,
            submission_epoch_unix=100, remote_task_identity="task-v3",
            status_url="http://private/tasks/task-v3",
            result_url="http://private/tasks/task-v3/result",
            resume_token_sha256="sha256:" + hashlib.sha256(accepted_token).hexdigest(),
        ))
        accepted_secret = RemoteParseResumeSecret(
            attempt_id=self.attempt_id, secret_kind="accepted_submission",
            token_bytes=accepted_token,
            token_sha256="sha256:" + hashlib.sha256(accepted_token).hexdigest(),
            token_byte_count=len(accepted_token), secret_contract_version=3,
        )
        assert reconciling.reservation is not None
        wrong_accepted = encode_checkpoint_receipt(replace(
            accepted_receipt.receipt, source_pdf_sha256=_sha("f")
        ))
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
            ValueError, "drifted from expected attempt"
        ):
            uow.remote_parse_attempts.checkpoint_v3_submitted(
                expected_attempt=reconciling,
                grant=CreditTransitionGrant(
                    expected_current=reconciling.current_credits,
                    maximum_positive_delta=reconciling.reservation,
                ), receipt=wrong_accepted, accepted_secret=accepted_secret,
            )
        race_barrier = threading.Barrier(2)

        def renew_or_submit(action: str) -> str:
            race_barrier.wait()
            try:
                with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                    if action == "renew":
                        result = uow.remote_parse_attempts.renew_v3_claim(
                            attempt_id=self.attempt_id, fence_identity="fence-1",
                            expected_state="reconciling",
                            expected_version=reconciling.row_version,
                            expected_current=reconciling.current_credits,
                            owner_identity="worker-v3-a",
                            claim_generation=reconciling.claim_generation,
                            lease_seconds=60,
                        ).attempt
                    else:
                        result = uow.remote_parse_attempts.checkpoint_v3_submitted(
                            expected_attempt=reconciling,
                            grant=CreditTransitionGrant(
                                expected_current=reconciling.current_credits,
                                maximum_positive_delta=reconciling.reservation,
                            ), receipt=accepted_receipt,
                            accepted_secret=accepted_secret,
                        ).attempt
                    uow.commit()
                    return result.state
            except RemoteParseCheckpointConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            race_outcomes = tuple(pool.map(renew_or_submit, ("renew", "submit")))
        self.assertIn("submitted", race_outcomes)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            submitted = uow.remote_parse_attempts.reconcile_v3_submitted_after_race(
                expected_attempt=reconciling,
            ).attempt

        terminal_token = b"v3-terminal-token"
        terminal_receipt = encode_terminal_receipt(TerminalReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"), artifact_owner_identity="owner-v3",
            artifact_byte_count=10, artifact_sha256=_sha("e"),
            resume_token_sha256="sha256:" + hashlib.sha256(terminal_token).hexdigest(),
        ))
        terminal_secret = RemoteParseResumeSecret(
            attempt_id=self.attempt_id, secret_kind="terminal",
            token_bytes=terminal_token,
            token_sha256="sha256:" + hashlib.sha256(terminal_token).hexdigest(),
            token_byte_count=len(terminal_token), secret_contract_version=3,
        )
        wrong_terminal = encode_terminal_receipt(replace(
            terminal_receipt.receipt, source_pdf_sha256=_sha("f")
        ))
        assert submitted.reservation is not None
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
            ValueError, "drifted from expected attempt"
        ):
            uow.remote_parse_attempts.checkpoint_v3_terminal(
                expected_attempt=submitted,
                grant=CreditTransitionGrant(
                    expected_current=submitted.current_credits,
                    maximum_positive_delta=submitted.reservation,
                ), receipt=wrong_terminal, terminal_secret=terminal_secret,
            )
        too_small = CreditTransitionGrant(
            expected_current=submitted.current_credits,
            maximum_positive_delta=CreditVector(
                retained_results=1, retained_bytes=9
            ),
        )
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
            ValueError, "positive credit grant"
        ):
            uow.remote_parse_attempts.checkpoint_v3_terminal(
                expected_attempt=submitted, grant=too_small,
                receipt=terminal_receipt, terminal_secret=terminal_secret,
            )
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT state FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=:a"
            ), {"a": self.attempt_id}).scalar_one(), "submitted")
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_ops.remote_parse_v3_resume_secret WHERE attempt_id=:a AND secret_kind='terminal'"
            ), {"a": self.attempt_id}).scalar_one(), 0)

        assert submitted.reservation is not None
        terminal_grant = CreditTransitionGrant(
            expected_current=submitted.current_credits,
            maximum_positive_delta=submitted.reservation,
        )
        stale_submitted = replace(submitted, row_version=submitted.row_version - 1)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
            RemoteParseCheckpointConflict, "lost exact CAS"
        ):
            uow.remote_parse_attempts.checkpoint_v3_terminal(
                expected_attempt=stale_submitted, grant=CreditTransitionGrant(
                    expected_current=stale_submitted.current_credits,
                    maximum_positive_delta=submitted.reservation,
                ), receipt=terminal_receipt, terminal_secret=terminal_secret,
            )
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_ops.remote_parse_v3_resume_secret WHERE attempt_id=:a AND secret_kind='terminal'"
            ), {"a": self.attempt_id}).scalar_one(), 0)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            remote_terminal = uow.remote_parse_attempts.checkpoint_v3_terminal(
                expected_attempt=submitted, grant=terminal_grant,
                receipt=terminal_receipt, terminal_secret=terminal_secret,
            ).attempt
            uow.commit()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            self.assertEqual(
                uow.remote_parse_attempts.reconcile_v3_terminal_after_race(
                    expected_attempt=submitted,
                ).attempt,
                remote_terminal,
            )
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
            RemoteParseCheckpointConflict, "lost exact CAS"
        ):
            uow.remote_parse_attempts.checkpoint_v3_terminal(
                expected_attempt=submitted, grant=terminal_grant,
                receipt=terminal_receipt, terminal_secret=terminal_secret,
            )

        materialization_token = b"v3-materialization-token"
        materialization_receipt = encode_checkpoint_receipt(
            PreparedMaterializationReceiptV2(
                attempt_identity=self.attempt_id, fence_identity="fence-1",
                source_pdf_sha256=_sha("a"), source_page_count=2,
                terminal_receipt_sha256=terminal_receipt.sha256,
                process_profile_sha256=remote_terminal.process_profile_sha256,
                credit_policy_sha256=remote_terminal.credit_policy_sha256,
                reservation_input_sha256=remote_terminal.reservation_input_sha256,
                spool_relpath="attempt/spool.zip", spool_sha256=_sha("e"),
                spool_byte_count=10, compressed_byte_count=10,
                uncompressed_byte_count=20, member_count=1,
                temporary_disk_byte_count=30, decoded_byte_count=20,
                private_token_sha256="sha256:" + hashlib.sha256(materialization_token).hexdigest(),
            )
        )
        materialization_secret = RemoteParseResumeSecret(
            attempt_id=self.attempt_id, secret_kind="materialization",
            token_bytes=materialization_token,
            token_sha256="sha256:" + hashlib.sha256(materialization_token).hexdigest(),
            token_byte_count=len(materialization_token), secret_contract_version=3,
        )
        assert remote_terminal.reservation is not None
        wrong_materialization = encode_checkpoint_receipt(replace(
            materialization_receipt.receipt, spool_sha256=_sha("f")
        ))
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
            ValueError, "drifted from terminal attempt"
        ):
            uow.remote_parse_attempts.prepare_v3_materialization(
                expected_attempt=remote_terminal,
                grant=CreditTransitionGrant(
                    expected_current=remote_terminal.current_credits,
                    maximum_positive_delta=remote_terminal.reservation,
                ), receipt=wrong_materialization,
                materialization_secret=materialization_secret,
            )
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            materializing = uow.remote_parse_attempts.prepare_v3_materialization(
                expected_attempt=remote_terminal,
                grant=CreditTransitionGrant(
                    expected_current=remote_terminal.current_credits,
                    maximum_positive_delta=remote_terminal.reservation,
                ), receipt=materialization_receipt,
                materialization_secret=materialization_secret,
            ).attempt
            uow.commit()
        self.assertEqual(materializing.state, "materializing")
        self.assertEqual(materializing.materialization_receipt_sha256, materialization_receipt.sha256)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            self.assertEqual(
                uow.remote_parse_attempts.reconcile_v3_materialization_after_race(
                    expected_attempt=remote_terminal,
                ).attempt,
                materializing,
            )

        local_receipt = encode_checkpoint_receipt(LocalMaterializationReceiptV2(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            claim_generation=materializing.claim_generation,
            source_pdf_sha256=_sha("a"), source_page_count=2,
            parser_target_sha256=_PARSER_TARGET_SHA,
            terminal_receipt_sha256=terminal_receipt.sha256,
            process_profile_sha256=materializing.process_profile_sha256,
            credit_policy_sha256=materializing.credit_policy_sha256,
            reservation_input_sha256=materializing.reservation_input_sha256,
            prepared_materialization_sha256=materialization_receipt.sha256,
            artifact_owner_identity="owner-v3", artifact_sha256=_sha("e"),
            artifact_byte_count=10, output_manifest_sha256=_sha("f"),
            output_manifest_relpath="run/manifest.json",
            output_manifest_byte_count=20, artifact_root_relpath="run/artifacts",
            provider_envelope_relpath="run/provider.json",
            provider_envelope_sha256=_sha("1"), provider_envelope_byte_count=30,
            compressed_byte_count=10, uncompressed_byte_count=20,
            member_count=1, temporary_disk_byte_count=30,
            decoded_byte_count=20, db_staged_byte_count=40,
        ))
        assert materializing.reservation is not None
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            local_materialized = uow.remote_parse_attempts.checkpoint_v3_local(
                expected_attempt=materializing,
                grant=CreditTransitionGrant(
                    expected_current=materializing.current_credits,
                    maximum_positive_delta=materializing.reservation,
                ), receipt=local_receipt,
            ).attempt
            uow.commit()
        self.assertEqual(local_materialized.local_db_staged_byte_count, 40)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            self.assertEqual(
                uow.remote_parse_attempts.reconcile_v3_local_after_race(
                    expected_attempt=materializing,
                ).attempt,
                local_materialized,
            )
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            run = uow.processing_runs.get(self.run_id)
            assert run is not None
            run.status = "succeeded"
            run.finished_at = datetime.now(timezone.utc)
            run.parser_artifact_relpath = "run/artifacts"
            run.provider_document_relpath = "run/provider.json"
            run.artifact_hash = _sha("1")
            assert local_materialized.reservation is not None
            finish_committed = uow.remote_parse_attempts.finish_v3_run_and_checkpoint(
                expected_attempt=local_materialized,
                grant=CreditTransitionGrant(
                    expected_current=local_materialized.current_credits,
                    maximum_positive_delta=local_materialized.reservation,
                ), finished_run=run,
            ).attempt
            uow.commit()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            self.assertEqual(
                uow.remote_parse_attempts.reconcile_v3_finish_after_race(
                    expected_attempt=local_materialized,
                ).attempt,
                finish_committed,
            )
        ack = encode_provider_ack_completion_witness(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            remote_task_identity="task-v3", source_pdf_sha256=_sha("a"),
            committed_state="finish_committed",
            terminal_receipt_sha256=terminal_receipt.sha256,
            failure_receipt_sha256=None, http_status=204,
        )
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
            ValueError, "drifted"
        ):
            uow.remote_parse_attempts.finalize_v3_ack(
                expected_attempt=finish_committed,
                witness=replace(ack, remote_task_identity="wrong-task"),
            )
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            final = uow.remote_parse_attempts.finalize_v3_ack(
                expected_attempt=finish_committed, witness=ack,
            )
            uow.commit()
        self.assertEqual(final.state, "acked")
        self.assertFalse(final.is_current)
        self.assertEqual(final.current_credits, CreditVector())
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            replay_final = uow.remote_parse_attempts.finalize_v3_ack(
                expected_attempt=finish_committed, witness=ack,
            )
        self.assertEqual(replay_final, final)

    def test_v3_pre_submission_and_remote_failure_are_atomic_and_ack_bound(self) -> None:
        claimed = self._v3_claimed_attempt()
        pre_receipt = encode_checkpoint_receipt(FailureReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            stage="remote", accepted=False, ack_required=False,
            submission_was_attempted=False, remote_task_identity=None,
            claim_generation=claimed.claim_generation,
            terminal_receipt_sha256=None, error_code="pre_submission_failed",
            error_stage="pre_submit", error_class="pre_submission",
            retryable=True, retry_budget_class="infrastructure",
            message="local preflight failed before remote IO",
        ))
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            final = uow.remote_parse_attempts.fail_v3_pre_submission(
                expected_attempt=claimed, receipt=pre_receipt,
            )
            uow.commit()
        self.assertEqual(final.state, "pre_submission_failed")
        self.assertEqual(final.current_credits, CreditVector())
        self.assertIsNone(final.claim_owner_identity)
        with self.engine.connect() as conn:
            status = conn.execute(text(
                "SELECT r.status,d.status,COUNT(o.event_id) "
                "FROM disclosure_core.processing_run r "
                "JOIN disclosure_core.document d ON d.document_id=r.document_id "
                "LEFT JOIN disclosure_ops.outbox_event o "
                "ON o.processing_run_id=r.processing_run_id "
                "AND o.event_kind='processing_run_failed' "
                "WHERE r.processing_run_id=:r GROUP BY r.status,d.status"
            ), {"r": self.run_id}).one()
        self.assertEqual(tuple(status), ("failed", "parse_failed", 1))

    def test_v3_remote_failure_retains_ack_credit_until_typed_ack(self) -> None:
        submitted = self._v3_submitted_attempt()
        receipt = encode_checkpoint_receipt(FailureReceipt(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            stage="remote", accepted=True, ack_required=True,
            submission_was_attempted=True, remote_task_identity="task-v3",
            claim_generation=submitted.claim_generation,
            terminal_receipt_sha256=None, error_code="remote_terminal",
            error_stage="remote_terminal", error_class="remote_terminal",
            retryable=False, retry_budget_class="item",
            message="provider returned a durable terminal failure",
        ))
        assert submitted.reservation is not None
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            committed = uow.remote_parse_attempts.fail_v3_remote(
                expected_attempt=submitted,
                grant=CreditTransitionGrant(
                    expected_current=submitted.current_credits,
                    maximum_positive_delta=submitted.reservation,
                ), receipt=receipt,
            ).attempt
            uow.commit()
        self.assertEqual(committed.state, "remote_failure_committed")
        assert committed.current_credits is not None
        self.assertEqual(committed.current_credits.ack_items, 1)
        witness = encode_provider_ack_completion_witness(
            attempt_identity=self.attempt_id, fence_identity="fence-1",
            remote_task_identity="task-v3", source_pdf_sha256=_sha("a"),
            committed_state="remote_failure_committed",
            terminal_receipt_sha256=None,
            failure_receipt_sha256=receipt.sha256, http_status=204,
        )
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            final = uow.remote_parse_attempts.finalize_v3_ack(
                expected_attempt=committed, witness=witness,
            )
            uow.commit()
        self.assertEqual(final.state, "remote_failed")
        self.assertEqual(final.current_credits, CreditVector())


    def test_v3_claim_foreign_live_expiry_takeover_and_atomic_add_rollback(self) -> None:
        attempt, secret = self._v3_attempt_and_secret()
        malformed = self._v3_attempt_and_secret()[0]
        object.__setattr__(malformed, "terminal_receipt_sha256", _sha("f"))
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
            ValueError, "canonical unclaimed prepared shape"
        ):
            uow.remote_parse_attempts.add_v3_prepared(malformed, secret)
        wrong_encoded = encode_checkpoint_receipt(PreparedReconcileReceipt(
            attempt_identity=self.attempt_id, fence_identity="wrong-fence",
            source_pdf_sha256=_sha("a"),
            client_submit_key="submit-" + self.attempt_id,
            submission_epoch_unix=100, parser_target_sha256=_PARSER_TARGET_SHA,
            request_sha256=_sha("c"), runtime_epoch_sha256=_sha("d"),
        ))
        wrong = RemoteParseResumeSecret(
            attempt_id=secret.attempt_id,
            secret_kind=secret.secret_kind,
            token_bytes=wrong_encoded.exact_bytes,
            token_sha256=wrong_encoded.sha256,
            token_byte_count=wrong_encoded.byte_count,
            secret_contract_version=3,
        )
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaises(ValueError):
            uow.remote_parse_attempts.add_v3_prepared(attempt, wrong)
        with self.engine.connect() as conn:
            self.assertIsNone(conn.execute(text(
                "SELECT attempt_id FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=:a"
            ), {"a": self.attempt_id}).scalar_one_or_none())

        with self.assertRaisesRegex(RuntimeError, "rollback seam"):
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                uow.remote_parse_attempts.add_v3_prepared(attempt, secret)
                raise RuntimeError("rollback seam")
        with self.engine.connect() as conn:
            self.assertIsNone(conn.execute(text(
                "SELECT attempt_id FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=:a"
            ), {"a": self.attempt_id}).scalar_one_or_none())

        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            created = uow.remote_parse_attempts.add_v3_prepared(attempt, secret)
            assert created.current_credits is not None
            uow.commit()
        barrier = threading.Barrier(2)

        def competing_claim(owner: str) -> object:
            barrier.wait()
            try:
                with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                    result = uow.remote_parse_attempts.claim_v3_recovery(
                        attempt_id=self.attempt_id, fence_identity="fence-1",
                        expected_state="prepared", expected_version=0,
                        expected_current=created.current_credits,
                        owner_identity=owner, lease_seconds=30,
                    )
                    uow.commit()
                    return result
            except RemoteParseCheckpointConflict as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(competing_claim, ("worker-v3-a", "worker-v3-b")))
        winners = [item for item in outcomes if not isinstance(item, Exception)]
        self.assertEqual(len(winners), 1)
        first = winners[0]
        assert hasattr(first, "attempt")
        first_owner = first.attempt.claim_owner_identity
        foreign_owner = "worker-v3-b" if first_owner == "worker-v3-a" else "worker-v3-a"
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
            RemoteParseCheckpointConflict, "exact CAS"
        ):
            uow.remote_parse_attempts.claim_v3_recovery(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_state="prepared", expected_version=first.attempt.row_version,
                expected_current=created.current_credits,
                owner_identity=foreign_owner, lease_seconds=30,
            )
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE disclosure_ops.remote_parse_attempt SET claim_lease_until=clock_timestamp()-interval '1 second' WHERE attempt_id=:a"
            ), {"a": self.attempt_id})
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            takeover = uow.remote_parse_attempts.claim_v3_recovery(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_state="prepared", expected_version=first.attempt.row_version,
                expected_current=created.current_credits,
                owner_identity=foreign_owner, lease_seconds=30,
            )
            uow.commit()
        self.assertEqual(takeover.attempt.claim_generation, 2)
        self.assertEqual(takeover.attempt.row_version, 2)

    def test_non_v3_current_checkpoint_blocks_v3_activation_listing(self) -> None:
        attempt, secret = self._v3_attempt_and_secret()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.remote_parse_attempts.add_v3_prepared(attempt, secret)
            uow.commit()
        extra_document = ids.new_document_id()
        extra_run = ids.new_processing_run_id()
        extra_attempt = "rpa_" + ids.new_ulid()
        try:
            with self.engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO disclosure_core.document (document_id,status) VALUES (:d,'registered')"
                ), {"d": extra_document})
                conn.execute(text(
                    "INSERT INTO disclosure_core.processing_run (processing_run_id,document_id,artifact_owner_processing_run_id,run_kind,status,input_raw_file_hash,provider_document_relpath,parser_target_identity) VALUES (:r,:d,:r,'parse','running',:h,:p,CAST(:t AS jsonb))"
                ), {"r": extra_run, "d": extra_document, "h": _sha("a"),
                    "p": "run/provider.json", "t": json.dumps(_PARSER_TARGET)})
                conn.execute(text(
                    "INSERT INTO disclosure_ops.remote_parse_attempt (attempt_id,processing_run_id,document_id,attempt_generation,fence_identity,source_pdf_sha256,parser_target_sha256,request_sha256,runtime_epoch_sha256,client_submit_key,checkpoint_contract_version,state,is_current,row_version) VALUES (:a,:r,:d,1,'legacy-fence',:s,:p,:q,:e,:k,1,'prepared',true,0)"
                ), {"a": extra_attempt, "r": extra_run, "d": extra_document,
                    "s": _sha("a"), "p": _sha("b"), "q": _sha("c"),
                    "e": _sha("d"), "k": "legacy-" + extra_attempt})
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
                RemoteParseCheckpointConflict, "blocks v3 staged activation"
            ):
                uow.remote_parse_attempts.list_v3_recoverable(
                    after_attempt_id=None, limit=10
                )
            with self.engine.begin() as conn:
                conn.execute(text(
                    "UPDATE disclosure_ops.remote_parse_attempt SET checkpoint_contract_version=2 WHERE attempt_id=:a"
                ), {"a": extra_attempt})
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow, self.assertRaisesRegex(
                RemoteParseCheckpointConflict, "blocks v3 staged activation"
            ):
                uow.remote_parse_attempts.list_v3_recoverable(
                    after_attempt_id=None, limit=10
                )
        finally:
            with self.engine.begin() as conn:
                conn.execute(text(
                    "DELETE FROM disclosure_core.processing_run WHERE processing_run_id=:r"
                ), {"r": extra_run})
                conn.execute(text(
                    "DELETE FROM disclosure_core.document WHERE document_id=:d"
                ), {"d": extra_document})

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
