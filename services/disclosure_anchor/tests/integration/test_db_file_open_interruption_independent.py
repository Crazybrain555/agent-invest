"""Independent scratch transaction checks; SQL abort injection is not an OS replay."""

from __future__ import annotations

from datetime import UTC, datetime
import unittest

import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.application.services.staged_ingress_v4 import DurableStagedIngressV4
from tests.integration import test_staged_coordinator_persistence_v4 as ingress_fixture
from tests.integration._remote_parse_v4_factory import (
    build_v4_authority_fixture, install_prepared_cycle,
)
from tests.integration._support import engine_or_skip


_MESSAGE = 'could not open file "base/12345/67890_fsm": Interrupted system call'
_TABLES = (
    ("disclosure_core.processing_run", "processing_run_id", "run"),
    ("disclosure_ops.outbox_event", "processing_run_id", "run"),
    ("disclosure_ops.remote_parse_attempt", "attempt_id", "attempt"),
    ("disclosure_ops.remote_parse_v4_execution_spec", "attempt_id", "attempt"),
    ("disclosure_ops.remote_parse_v4_evidence", "attempt_id", "attempt"),
    ("disclosure_ops.remote_parse_v4_checkpoint", "attempt_id", "attempt"),
)


def _packet(connection, fixture):
    # All identifiers are fixed test-owned SQL constants, never runtime inputs.
    return {
        table: tuple(connection.execute(sa.text(
            f"SELECT to_jsonb(t) FROM {table} t WHERE {column}=:identity "
            "ORDER BY to_jsonb(t)::text"
        ), {"identity": getattr(fixture, "processing_run_id" if key == "run" else "attempt_id")}).scalars())
        for table, column, key in _TABLES
    }


class _AbortAfterPacket:
    def __init__(self, delegate, session, remaining, calls, seen_packets, fixture):
        self.delegate = delegate
        self.session = session
        self.remaining = remaining
        self.calls = calls
        self.seen_packets = seen_packets
        self.fixture = fixture

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def commit(self, command):
        self.calls.append(command)
        result = self.delegate.commit(command)
        self.seen_packets.append(_packet(self.session, self.fixture))
        if self.remaining[0]:
            self.remaining[0] -= 1
            # Real server-side XX000 abort after all six packet families exist.
            # This validates rollback/replay, not the kernel EINTR mechanism.
            self.session.execute(sa.text(
                "DO $$ BEGIN RAISE EXCEPTION USING ERRCODE='XX000', "
                "MESSAGE='could not open file \"base/12345/67890_fsm\": Interrupted system call'; END $$"
            ))
        return result


class DatabaseFileOpenInterruptionIndependentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = engine_or_skip()
        self._clean_scratch_rows()

    def _clean_scratch_rows(self):
        ingress_fixture.StagedCoordinatorPersistenceV4IntegrationTests._clean_scratch_rows(self)

    def tearDown(self):
        try:
            self._clean_scratch_rows()
        finally:
            self.engine.dispose()

    def _exercise(self, failures):
        fixture = build_v4_authority_fixture()
        sibling = build_v4_authority_fixture()
        helper = ingress_fixture.StagedCoordinatorPersistenceV4IntegrationTests
        with self.engine.begin() as connection:
            security = helper._insert_ingress_document(connection, fixture)
            install_prepared_cycle(connection, sibling)
            sibling_before = _packet(connection, sibling)
        command = helper._ingress_command(
            fixture, security_id=security,
            started_at=datetime(2026, 10, 10, 3, 2, tzinfo=UTC),
            event_id="evt_independent_file_open_interruption",
        )
        remaining = [failures]
        calls, seen_packets, before_attempts = [], [], []
        engine = self.engine

        class InjectingUnitOfWork(SqlAlchemyUnitOfWork):
            def _bind_repositories(self, session):
                super()._bind_repositories(session)
                self.remote_parse_v4_ingress = _AbortAfterPacket(
                    self.remote_parse_v4_ingress, session, remaining,
                    calls, seen_packets, fixture,
                )

        def factory():
            # Independent connection observes rollback before the next UoW.
            with engine.connect() as connection:
                before_attempts.append(_packet(connection, fixture))
            return InjectingUnitOfWork(engine=engine)

        ingress = DurableStagedIngressV4(uow_factory=factory)
        error = None
        authority = None
        try:
            authority = ingress.execute(command, write_guard=lambda: None)
        except Exception as exc:
            error = exc
        self.assertEqual(len(calls), 2, "exactly two mutation attempts, no third replay")
        self.assertTrue(all(item is command for item in calls))
        self.assertEqual(remaining[0], 0)
        self.assertEqual(len(before_attempts), 2)
        for packet in before_attempts:
            self.assertTrue(all(not rows for rows in packet.values()), packet)
        for packet in seen_packets:
            self.assertTrue(all(rows for rows in packet.values()), packet)
        with engine.connect() as connection:
            after = _packet(connection, fixture)
            self.assertEqual(_packet(connection, sibling), sibling_before)
        return authority, error, after, command

    def test_real_abort_rolls_back_whole_packet_then_commits_same_keys_once(self):
        authority, error, packet, command = self._exercise(1)
        self.assertIsNone(error)
        self.assertEqual(authority.attempt_id, command.proposal.attempt_id)
        self.assertEqual(authority.state, "prepared")
        self.assertEqual(authority.attempt_generation, 1)
        self.assertIsNone(authority.checkpoint.snapshot_receipt_sha256)
        self.assertEqual({table: len(rows) for table, rows in packet.items()},
                         {table: 1 for table, _, _ in _TABLES})
        head = packet["disclosure_ops.remote_parse_attempt"][0]
        self.assertEqual(head["fence_identity"], command.proposal.fence_identity)
        spec = packet["disclosure_ops.remote_parse_v4_execution_spec"][0]
        self.assertEqual(spec["execution_spec_sha256"], command.proposal.execution_spec.sha256)

    def test_second_real_abort_exhausts_shared_budget_without_half_packet(self):
        authority, error, packet, _ = self._exercise(2)
        self.assertIsNone(authority)
        self.assertIsNotNone(error)
        chain = []
        while error is not None:
            chain.append(str(error))
            error = error.__cause__
        self.assertTrue(any(_MESSAGE in item for item in chain), chain)
        self.assertTrue(all(not rows for rows in packet.values()), packet)
