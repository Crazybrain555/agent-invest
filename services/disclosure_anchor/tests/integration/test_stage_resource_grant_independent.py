"""Scratch-PG persistence witness for one frozen V5 stage grant.

Run only through the service's managed integration runner. The test uses the
real repository/UoW and a synthetic, bounded result; no parser or provider.
"""

from __future__ import annotations

from dataclasses import replace
import time
from threading import Event
import unittest

import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres.unit_of_work import (
    SqlAlchemyUnitOfWork,
    unit_of_work_factory,
)
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    canonical_result_owner_v2,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    TERMINAL_RECEIPT_V5_CONTRACT,
    TerminalResultStorageV1,
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    advance_remote_parse_checkpoint_v4,
    build_materialization_intent_v4,
    build_stage_resource_grant_v1,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4AuthorityViolation,
    V4SuccessorAppend,
)
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
)
from disclosure_anchor.application.services.staged_parse_coordinator import StageLeaseGuard
from tests.integration._remote_parse_v4_factory import (
    build_v4_authority_fixture,
    install_submitted_cycle,
    sha256_bytes,
)
from tests.integration._support import engine_or_skip
from tests.integration.test_staged_coordinator_persistence_v4 import _limits


class _LoseCommitResponseOnce:
    """Return real PG commit, then lose only the outer response once."""

    def __init__(self, delegate: SqlAlchemyUnitOfWork, once: list[bool]) -> None:
        self.delegate = delegate
        self.once = once

    @property
    def remote_parse_v4(self):
        return self.delegate.remote_parse_v4

    def __enter__(self):
        self.delegate.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self.delegate.__exit__(exc_type, exc, traceback)

    def commit(self) -> None:
        self.delegate.commit()
        if self.once[0]:
            self.once[0] = False
            raise RuntimeError("synthetic commit response lost")


class StageResourceGrantPersistenceIndependentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.addCleanup(self.engine.dispose)
        self.fixture = build_v4_authority_fixture()
        self.addCleanup(self._clean_rows)
        with self.engine.begin() as conn:
            install_submitted_cycle(conn, self.fixture, include_secret=True)
        self._make_v5_records()

    def _clean_rows(self) -> None:
        with self.engine.begin() as conn:
            # engine_or_skip supplies only the managed scratch database.
            conn.exec_driver_sql("TRUNCATE TABLE disclosure_ops.remote_parse_attempt CASCADE")
            conn.execute(sa.text(
                "DELETE FROM disclosure_core.processing_run WHERE processing_run_id=:run"
            ), {"run": self.fixture.processing_run_id})
            conn.execute(sa.text(
                "DELETE FROM disclosure_core.document WHERE document_id=:document"
            ), {"document": self.fixture.document_id})

    def _backend(self, *, owner: str = "worker-test", factory=None):
        limits = _limits()
        limits = replace(limits, credits=replace(
            limits.credits, provider_result_bytes=100_000, compressed_bytes=100_000,
        ))
        return DurableStagedCoordinatorPersistenceV4(
            uow_factory=factory or unit_of_work_factory(self.engine),
            limits=limits, owner_identity=owner,
        )

    @staticmethod
    def _stage_guard() -> StageLeaseGuard:
        return StageLeaseGuard(
            deadline_monotonic=time.monotonic() + 60,
            _revoked=Event(), _monotonic=time.monotonic,
        )

    def _authority(self):
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            return uow.remote_parse_v4.load(self.fixture.attempt_id)

    def _make_v5_records(self) -> None:
        fixture = self.fixture
        artifact_bytes = b"z" * 50_000
        artifact_sha = sha256_bytes(artifact_bytes)
        owner = canonical_result_owner_v2(
            task_id=fixture.accepted.remote_task_identity,
            artifact_sha256=artifact_sha.removeprefix("sha256:"),
            artifact_byte_count=len(artifact_bytes),
        )
        storage = TerminalResultStorageV1(
            policy_sha256=sha256_bytes(b"synthetic-storage-policy"),
            selected_bytes=200_000, member_count=4,
            inventory_sha256=sha256_bytes(b"synthetic-inventory"),
        )
        self.terminal = replace(
            fixture.terminal, contract_version=TERMINAL_RECEIPT_V5_CONTRACT,
            result_storage=storage, artifact_sha256=artifact_sha,
            artifact_byte_count=len(artifact_bytes), result_owner_identity=owner,
        )
        self.remote_terminal = advance_remote_parse_checkpoint_v4(
            fixture.submitted, state="remote_terminal",
            held_resource_credit=replace(
                fixture.remote_terminal.held_resource_credit,
                provider_result_bytes=len(artifact_bytes),
            ),
            terminal_receipt_sha256=self.terminal.sha256,
        )
        grant = build_stage_resource_grant_v1(
            reservation=fixture.reservation,
            terminal_receipt_sha256=self.terminal.sha256,
            storage_policy_sha256=storage.policy_sha256,
            inventory_sha256=storage.inventory_sha256,
            artifact_byte_count=len(artifact_bytes),
            selected_bytes=storage.selected_bytes,
            member_count=storage.member_count,
            decode_working_set_bytes=100_000,
            decode_input_limit_bytes=50_000,
        )
        old = fixture.materialization_intent
        self.intent = build_materialization_intent_v4(
            reservation=fixture.reservation,
            source_checkpoint=self.remote_terminal,
            terminal_receipt_sha256=self.terminal.sha256,
            remote_task_identity=self.terminal.remote_task_identity,
            artifact_owner_identity=self.terminal.result_owner_identity,
            artifact_sha256=self.terminal.artifact_sha256,
            artifact_byte_count=self.terminal.artifact_byte_count,
            provider_envelope_context=old.provider_envelope_context,
            allowance_sha256=old.allowance_sha256,
            provider_capability_kind=old.provider_capability_kind,
            provider_capability_sha256=old.provider_capability_sha256,
            provider_capability_byte_count=old.provider_capability_byte_count,
            output_dir_name=old.output_dir_name,
            provider_envelope_relpath=old.provider_envelope_relpath,
            output_manifest_relpath=old.output_manifest_relpath,
            member_count_limit=storage.member_count,
            uncompressed_byte_limit=storage.selected_bytes,
            resource_grant=grant,
        )
        self.materializing = advance_remote_parse_checkpoint_v4(
            self.remote_terminal, state="materializing",
            held_resource_credit=self.intent.held_resource_credit,
            materialization_intent_sha256=self.intent.sha256,
        )

    def _claim_and_append_terminal(self):
        backend = self._backend()
        candidates = backend.list_recoverable(after_attempt_id=None, limit=10)
        candidate = next(item for item in candidates if item.attempt_id == self.fixture.attempt_id)
        work = backend.claim_recovery(candidate)
        authority = self._authority()
        terminal_work = backend.append_successor(
            work,
            V4SuccessorAppend(
                claim=authority.claim_witness, successor=self.remote_terminal,
                new_evidence=(encode_remote_parse_evidence_v4(self.terminal),),
            ),
            stage_guard=self._stage_guard(),
        )
        self.assertEqual(terminal_work.state, "remote_terminal")
        self.assertEqual(terminal_work.credit_reservation.provider_result_bytes, 50_000)
        return terminal_work

    def test_v5_grant_append_survives_lost_commit_and_recovery_without_double_charge(self) -> None:
        fixture = self.fixture
        with self.engine.connect() as conn:
            h0 = conn.execute(sa.text(
                "SELECT resource_reservation_bytes,resource_reservation_sha256,checkpoint_bytes "
                "FROM disclosure_ops.remote_parse_v4_checkpoint "
                "WHERE attempt_id=:attempt AND lifecycle_version=0"
            ), {"attempt": fixture.attempt_id}).one()
            original_h0 = tuple(bytes(value) if isinstance(value, memoryview) else value for value in h0)
        terminal_work = self._claim_and_append_terminal()
        authority = self._authority()
        append = V4SuccessorAppend(
            claim=authority.claim_witness, successor=self.materializing,
            new_evidence=(encode_remote_parse_evidence_v4(self.intent),),
        )
        lost = [True]

        def uncertain_factory():
            return _LoseCommitResponseOnce(SqlAlchemyUnitOfWork(engine=self.engine), lost)

        committed = self._backend(factory=uncertain_factory).append_successor(
            terminal_work, append, stage_guard=self._stage_guard(),
        )
        self.assertFalse(lost[0])
        self.assertEqual(committed.state, "materializing")
        self.assertEqual(committed.credit_reservation.provider_result_bytes, 50_000)
        self.assertEqual(committed.credit_reservation.compressed_bytes, 50_000)
        self.assertEqual(committed.credit_reservation.decoded_bytes, 100_000)
        self.assertEqual(committed.credit_reservation.temp_disk_bytes, 350_000)
        self.assertEqual(committed.credit_reservation.output_bytes, 300_000)
        self.assertEqual(committed.credits, self.intent.held_resource_credit)

        with self.engine.begin() as conn:
            current_h0 = conn.execute(sa.text(
                "SELECT resource_reservation_bytes,resource_reservation_sha256,checkpoint_bytes "
                "FROM disclosure_ops.remote_parse_v4_checkpoint "
                "WHERE attempt_id=:attempt AND lifecycle_version=0"
            ), {"attempt": fixture.attempt_id}).one()
            current_h0 = tuple(bytes(value) if isinstance(value, memoryview) else value for value in current_h0)
            self.assertEqual(current_h0, original_h0)
            self.assertEqual(current_h0[0], fixture.reservation.canonical_bytes)
            self.assertEqual(current_h0[1], fixture.reservation.sha256)
            self.assertEqual(current_h0[2], fixture.prepared.canonical_bytes)
            rows = conn.execute(sa.text(
                "SELECT evidence_bytes,evidence_sha256 FROM disclosure_ops.remote_parse_v4_evidence "
                "WHERE attempt_id=:attempt AND evidence_kind='materialization_intent'"
            ), {"attempt": fixture.attempt_id}).one()
            self.assertEqual(bytes(rows[0]), self.intent.canonical_bytes)
            self.assertEqual(rows[1], self.intent.sha256)
            count = conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_ops.remote_parse_v4_checkpoint "
                "WHERE attempt_id=:attempt AND state='materializing'"
            ), {"attempt": fixture.attempt_id}).scalar_one()
            self.assertEqual(count, 1)
            held = conn.execute(sa.text(
                "SELECT held_provider_result_bytes,held_compressed_bytes,"
                "held_decoded_bytes,held_temp_disk_bytes FROM disclosure_ops.remote_parse_v4_checkpoint "
                "WHERE attempt_id=:attempt AND state='materializing'"
            ), {"attempt": fixture.attempt_id}).one()
            self.assertEqual(tuple(held), (50_000, 50_000, 100_000, 350_000))
            conn.execute(sa.text(
                "UPDATE disclosure_ops.remote_parse_attempt "
                "SET claim_lease_until=clock_timestamp()-interval '1 second' "
                "WHERE attempt_id=:attempt"
            ), {"attempt": fixture.attempt_id})

        restarted = self._backend(owner="worker-restarted")
        candidates = restarted.list_recoverable(after_attempt_id=None, limit=10)
        candidate = next(item for item in candidates if item.attempt_id == fixture.attempt_id)
        recovered = restarted.claim_recovery(candidate)
        self.assertEqual(recovered.state, "materializing")
        self.assertEqual(recovered.credit_reservation, committed.credit_reservation)
        self.assertEqual(recovered.credits, committed.credits)
        loaded = restarted.load_owned_authority(recovered)
        by_kind = {item.kind: item.value for item in loaded.evidence}
        self.assertEqual(by_kind["terminal_receipt"], self.terminal)
        self.assertEqual(by_kind["materialization_intent"], self.intent)

    def test_underaccounted_materializing_head_is_rejected_without_append(self) -> None:
        terminal_work = self._claim_and_append_terminal()
        authority = self._authority()
        undercounted = replace(
            self.materializing,
            held_resource_credit=replace(
                self.materializing.held_resource_credit,
                temp_disk_bytes=self.fixture.reservation.reserved_credit.temp_disk_bytes,
            ),
        )
        with self.assertRaises(RemoteParseV4AuthorityViolation):
            self._backend().append_successor(
                terminal_work,
                V4SuccessorAppend(
                    claim=authority.claim_witness, successor=undercounted,
                    new_evidence=(encode_remote_parse_evidence_v4(self.intent),),
                ),
                stage_guard=self._stage_guard(),
            )
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(sa.text(
                "SELECT state FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=:attempt"
            ), {"attempt": self.fixture.attempt_id}).scalar_one(), "remote_terminal")
            self.assertEqual(conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_ops.remote_parse_v4_evidence "
                "WHERE attempt_id=:attempt AND evidence_kind='materialization_intent'"
            ), {"attempt": self.fixture.attempt_id}).scalar_one(), 0)
