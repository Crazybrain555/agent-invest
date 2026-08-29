from __future__ import annotations

import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    FUTURE_L2_READER_ROLE,
    READER_ROLE,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.application.contracts.remote_parse_checkpoint import (
    ALLOWED_TRANSITIONS,
    EncodedTerminalReceipt,
    RemoteParseAttempt,
    RemoteParseCheckpointConflict,
    RemoteParseResumeSecret,
    TerminalReceipt,
    encode_terminal_receipt,
)
from disclosure_anchor.domain import ids
from tests.integration._support import engine_or_skip


def _sha(char: str) -> str:
    return "sha256:" + char * 64


class RemoteParseCheckpointIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.document_id = ids.new_document_id()
        self.run_id = ids.new_processing_run_id()
        self.attempt_id = "rpa_" + ids.new_ulid()
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO disclosure_core.document (document_id,status) VALUES (:d,'registered')"), {"d": self.document_id})
            conn.execute(text("INSERT INTO disclosure_core.processing_run (processing_run_id,document_id,artifact_owner_processing_run_id,run_kind,status,input_raw_file_hash,provider_document_relpath) VALUES (:r,:d,:r,'parse','running',:h,'derived/checkpoint/provider.json')"), {"r": self.run_id, "d": self.document_id, "h": _sha("a")})

    def tearDown(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM disclosure_core.processing_run WHERE processing_run_id=:r"), {"r": self.run_id})
            conn.execute(text("DELETE FROM disclosure_core.document WHERE document_id=:d"), {"d": self.document_id})
        self.engine.dispose()

    def _attempt(self, *, attempt_id: str | None = None, fence: str = "fence-1") -> RemoteParseAttempt:
        return RemoteParseAttempt(
            attempt_id=attempt_id or self.attempt_id,
            processing_run_id=self.run_id,
            document_id=self.document_id,
            attempt_generation=1,
            fence_identity=fence,
            source_pdf_sha256=_sha("a"),
            parser_target_sha256=_sha("b"),
            request_sha256=_sha("c"),
            runtime_epoch_sha256=_sha("d"),
            client_submit_key="submit-" + (attempt_id or self.attempt_id),
        )

    def _receipt(self, *, attempt_id: str | None = None, owner: str = "owner-1"):
        return encode_terminal_receipt(TerminalReceipt(
            attempt_identity=attempt_id or self.attempt_id, fence_identity="fence-1",
            source_pdf_sha256=_sha("a"), artifact_owner_identity=owner,
            artifact_byte_count=10, artifact_sha256=_sha("e"),
            resume_token_sha256=_sha("f"),
        ))

    def _seed_attempt_state(self, state: str, attempt_id: str) -> RemoteParseAttempt:
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            current = uow.remote_parse_attempts.add(self._attempt(attempt_id=attempt_id))
            if state != "prepared":
                current = uow.remote_parse_attempts.checkpoint_submitted(
                    attempt_id=attempt_id,
                    fence_identity="fence-1",
                    expected_version=current.row_version,
                    remote_task_identity="task-" + attempt_id,
                )
            if state not in {"prepared", "submitted"}:
                current = uow.remote_parse_attempts.checkpoint_terminal(
                    attempt_id=attempt_id,
                    fence_identity="fence-1",
                    expected_version=current.row_version,
                    remote_task_identity="task-" + attempt_id,
                    receipt=self._receipt(attempt_id=attempt_id),
                )
            for next_state in {
                "materializing": ("materializing",),
                "local_materialized": ("materializing", "local_materialized"),
                "finish_committed": (
                    "materializing", "local_materialized", "finish_committed",
                ),
            }.get(state, ()):
                current = uow.remote_parse_attempts.transition(
                    attempt_id=attempt_id,
                    fence_identity="fence-1",
                    expected_state=current.state,
                    expected_version=current.row_version,
                    next_state=next_state,
                )
            uow.commit()
            return current

    def test_cas_first_terminal_wins_idempotently_and_rejects_old_fence(self) -> None:
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            created = uow.remote_parse_attempts.add(self._attempt())
            submitted = uow.remote_parse_attempts.checkpoint_submitted(
                attempt_id=created.attempt_id, fence_identity="fence-1",
                expected_version=0,
                remote_task_identity="task-1",
            )
            uow.commit()
        receipt = self._receipt()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            won = uow.remote_parse_attempts.checkpoint_terminal(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_version=submitted.row_version,
                remote_task_identity="task-1", receipt=receipt,
            )
            uow.commit()
        self.assertEqual(won.state, "remote_terminal")
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            replay = uow.remote_parse_attempts.checkpoint_terminal(
                attempt_id=self.attempt_id, fence_identity="fence-1",
                expected_version=submitted.row_version,
                remote_task_identity="task-1", receipt=receipt,
            )
            self.assertEqual(replay.terminal_receipt_sha256, receipt.sha256)
            with self.assertRaises(RemoteParseCheckpointConflict):
                uow.remote_parse_attempts.checkpoint_terminal(
                    attempt_id=self.attempt_id, fence_identity="fence-1",
                    expected_version=submitted.row_version,
                    remote_task_identity="task-1", receipt=self._receipt(owner="other"),
                )
            with self.assertRaises(RemoteParseCheckpointConflict):
                uow.remote_parse_attempts.transition(
                    attempt_id=self.attempt_id, fence_identity="old-fence",
                    expected_state="remote_terminal", expected_version=won.row_version,
                    next_state="materializing",
                )

    def test_concurrent_identical_terminal_checkpoint_has_one_state_and_no_conflict(self) -> None:
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            created = uow.remote_parse_attempts.add(self._attempt())
            submitted = uow.remote_parse_attempts.checkpoint_submitted(
                attempt_id=created.attempt_id, fence_identity="fence-1",
                expected_version=0, remote_task_identity="task-1",
            )
            uow.commit()
        receipt = self._receipt()

        def checkpoint() -> str:
            with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                result = uow.remote_parse_attempts.checkpoint_terminal(
                    attempt_id=self.attempt_id, fence_identity="fence-1",
                    expected_version=submitted.row_version,
                    remote_task_identity="task-1", receipt=receipt,
                )
                uow.commit()
                return result.terminal_receipt_sha256 or ""

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _index: checkpoint(), range(2)))
        self.assertEqual(results, (receipt.sha256, receipt.sha256))

    def test_partial_unique_current_and_private_acl(self) -> None:
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.remote_parse_attempts.add(self._attempt())
            with self.assertRaises(IntegrityError):
                uow.remote_parse_attempts.add(self._attempt(attempt_id="rpa_" + ids.new_ulid(), fence="fence-2"))
        with self.engine.connect() as conn:
            for role in (READER_ROLE, FUTURE_L2_READER_ROLE):
                self.assertFalse(conn.execute(text("SELECT has_table_privilege(:r,'disclosure_ops.remote_parse_attempt','SELECT')"), {"r": role}).scalar_one())
                self.assertFalse(conn.execute(text("SELECT has_table_privilege(:r,'disclosure_ops.remote_parse_resume_secret','SELECT')"), {"r": role}).scalar_one())
            self.assertTrue(conn.execute(text("SELECT has_table_privilege(:r,'disclosure_ops.remote_parse_attempt','SELECT,INSERT,UPDATE,DELETE')"), {"r": APP_ROLE}).scalar_one())

    def test_every_generic_transition_satisfies_real_database_source_shape(self) -> None:
        for source_state, target_state in sorted(ALLOWED_TRANSITIONS):
            with self.subTest(source=source_state, target=target_state):
                attempt_id = "rpa_" + ids.new_ulid()
                current = self._seed_attempt_state(source_state, attempt_id)
                terminal_sha = current.terminal_receipt_sha256
                with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
                    result = uow.remote_parse_attempts.transition(
                        attempt_id=attempt_id,
                        fence_identity="fence-1",
                        expected_state=source_state,
                        expected_version=current.row_version,
                        next_state=target_state,
                    )
                    uow.commit()
                self.assertEqual(result.state, target_state)
                self.assertEqual(result.is_current, target_state not in {
                    "acked", "remote_failed", "local_failed", "superseded",
                })
                if terminal_sha is not None:
                    self.assertEqual(result.terminal_receipt_sha256, terminal_sha)
                with self.engine.begin() as conn:
                    conn.execute(text(
                        "DELETE FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=:a"
                    ), {"a": attempt_id})

    def test_add_and_database_reject_noncanonical_lifecycle_shapes(self) -> None:
        with (
            SqlAlchemyUnitOfWork(engine=self.engine) as uow,
            self.assertRaisesRegex(ValueError, "canonical prepared"),
        ):
            uow.remote_parse_attempts.add(
                replace(
                    self._attempt(),
                    state="submitted",
                    row_version=1,
                    remote_task_identity="task-1",
                )
            )

        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO disclosure_ops.remote_parse_attempt "
                "(attempt_id,processing_run_id,document_id,attempt_generation,fence_identity,"
                "source_pdf_sha256,parser_target_sha256,request_sha256,runtime_epoch_sha256,"
                "client_submit_key,state,is_current,row_version) VALUES "
                "(:a,:r,:d,1,'fence-1',:s,:p,:q,:e,:k,'prepared',true,0)"
            ), {"a": self.attempt_id, "r": self.run_id, "d": self.document_id,
                "s": _sha("a"), "p": _sha("b"), "q": _sha("c"), "e": _sha("d"),
                "k": "submit-" + self.attempt_id})
        with self.assertRaises(IntegrityError), self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE disclosure_ops.remote_parse_attempt SET is_current=false "
                "WHERE attempt_id=:a"
            ), {"a": self.attempt_id})

        with self.assertRaises(IntegrityError), self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE disclosure_ops.remote_parse_attempt "
                "SET state='submitted',row_version=1 WHERE attempt_id=:a"
            ), {"a": self.attempt_id})

        with self.assertRaises(IntegrityError), self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE disclosure_ops.remote_parse_attempt SET state='remote_terminal',"
                "is_current=true,row_version=2,remote_task_identity='task-1',"
                "terminal_receipt_sha256=:h,terminal_receipt_bytes=:b,"
                "terminal_receipt_byte_count=65537,result_owner_identity='owner-1',"
                "result_artifact_sha256=:h,result_artifact_bytes=1 WHERE attempt_id=:a"
            ), {"a": self.attempt_id, "h": _sha("e"), "b": b"x" * 65537})

    def test_repository_redecodes_terminal_bytes_even_if_object_was_forged(self) -> None:
        current = self._seed_attempt_state("submitted", self.attempt_id)
        encoded = self._receipt()
        forged = object.__new__(EncodedTerminalReceipt)
        object.__setattr__(forged, "receipt", replace(
            encoded.receipt, artifact_owner_identity="forged-owner"
        ))
        object.__setattr__(forged, "exact_bytes", encoded.exact_bytes)
        object.__setattr__(forged, "sha256", encoded.sha256)
        object.__setattr__(forged, "byte_count", encoded.byte_count)
        with (
            SqlAlchemyUnitOfWork(engine=self.engine) as uow,
            self.assertRaisesRegex(RemoteParseCheckpointConflict, "self-consistent"),
        ):
            uow.remote_parse_attempts.checkpoint_terminal(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_version=current.row_version,
                remote_task_identity="task-" + self.attempt_id,
                receipt=forged,
            )

    def test_loaded_projection_must_match_canonical_receipt(self) -> None:
        current = self._seed_attempt_state("remote_terminal", self.attempt_id)
        self.assertIsNotNone(current.terminal_receipt_sha256)
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE disclosure_ops.remote_parse_attempt "
                "SET result_owner_identity='drifted-owner' WHERE attempt_id=:a"
            ), {"a": self.attempt_id})
        with (
            SqlAlchemyUnitOfWork(engine=self.engine) as uow,
            self.assertRaisesRegex(RemoteParseCheckpointConflict, "identity drifted"),
        ):
            uow.remote_parse_attempts.get(self.attempt_id)

    def test_identical_terminal_replay_remains_idempotent_after_local_failure(self) -> None:
        terminal = self._seed_attempt_state("remote_terminal", self.attempt_id)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            failed = uow.remote_parse_attempts.transition(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_state="remote_terminal",
                expected_version=terminal.row_version,
                next_state="local_failed",
            )
            uow.commit()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            replay = uow.remote_parse_attempts.checkpoint_terminal(
                attempt_id=self.attempt_id,
                fence_identity="fence-1",
                expected_version=1,
                remote_task_identity="task-" + self.attempt_id,
                receipt=self._receipt(),
            )
            self.assertEqual(replay.row_version, failed.row_version)
            self.assertEqual(replay.state, "local_failed")

    def test_document_generation_is_never_reused_after_finalization(self) -> None:
        first = self._seed_attempt_state("prepared", self.attempt_id)
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.remote_parse_attempts.transition(
                attempt_id=first.attempt_id,
                fence_identity="fence-1",
                expected_state="prepared",
                expected_version=0,
                next_state="remote_failed",
            )
            uow.commit()
        with (
            SqlAlchemyUnitOfWork(engine=self.engine) as uow,
            self.assertRaises(IntegrityError),
        ):
            uow.remote_parse_attempts.add(
                self._attempt(attempt_id="rpa_" + ids.new_ulid(), fence="fence-2")
            )

    def test_private_secret_is_exact_first_write_wins(self) -> None:
        token = b"opaque-resume-token"
        secret = RemoteParseResumeSecret(
            attempt_id=self.attempt_id,
            secret_kind="submission",
            token_bytes=token,
            token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
            token_byte_count=len(token),
        )
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.remote_parse_attempts.add(self._attempt())
            uow.remote_parse_attempts.put_secret(secret)
            uow.remote_parse_attempts.put_secret(secret)
            loaded = uow.remote_parse_attempts.get_secret(
                self.attempt_id, "submission"
            )
            self.assertEqual(loaded, secret)
            other = b"conflicting-token"
            with self.assertRaises(RemoteParseCheckpointConflict):
                uow.remote_parse_attempts.put_secret(
                    RemoteParseResumeSecret(
                        attempt_id=self.attempt_id,
                        secret_kind="submission",
                        token_bytes=other,
                        token_sha256="sha256:" + hashlib.sha256(other).hexdigest(),
                        token_byte_count=len(other),
                    )
                )


if __name__ == "__main__":
    unittest.main()
