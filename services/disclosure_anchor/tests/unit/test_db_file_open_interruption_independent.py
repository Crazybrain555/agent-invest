"""Independent counterexamples at the real UoW/ingress transaction boundary."""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import psycopg
from sqlalchemy.exc import InternalError

from disclosure_anchor.adapters.db.postgres import unit_of_work as db_uow
from disclosure_anchor.application.ports.remote_parse_v4_ingress import V4InitialIngressNotCommitted
from disclosure_anchor.application.ports.unit_of_work import (
    DatabaseInterruptionRolledBack, replayable_transaction, transaction_replay_declared,
)
from disclosure_anchor.application.services.staged_ingress_v4 import (
    DurableStagedIngressV4, StagedIngressResponseLost,
)
from tests.unit import test_v4_initial_ingress as ingress_fixture


_MESSAGE = 'could not open file "base/12345/67890_fsm": Interrupted system call'


class _ServerOpenError(psycopg.errors.InternalError_):
    @property
    def diag(self):
        return SimpleNamespace(message_primary=str(self), sqlstate=self.sqlstate, severity_nonlocalized="ERROR")


class _ServerFatalError(_ServerOpenError):
    severity = "FATAL"

    @property
    def diag(self):
        return SimpleNamespace(message_primary=str(self), sqlstate=self.sqlstate, severity_nonlocalized=self.severity)


class _ServerPanicError(_ServerFatalError):
    severity = "PANIC"


class _ServerCancelledError(psycopg.errors.QueryCanceled):
    @property
    def diag(self):
        return SimpleNamespace(message_primary=str(self), sqlstate=self.sqlstate)


def _db_error(message=_MESSAGE, error_type=_ServerOpenError):
    return InternalError("fixture SQL is never executed", {}, error_type(message))


def _command():
    fixture = ingress_fixture.MinerUV4InitialIngressFactoryTests()
    fixture.setUp()
    return fixture._factory().build(
        ingress_fixture._candidate(), source_observation=fixture.source.observation,
        available_credits=fixture.credit.reservation,
    )


class _Harness:
    """Replace physical IO only; exercise the concrete UoW enter/exit/commit."""
    def __init__(self, modes, *, error=None, cleanup_failure=None, invalidated=False):
        self.modes = list(modes)
        self.error = error if error is not None else _db_error()
        self.cleanup_failure = cleanup_failure
        self.invalidated = invalidated
        self.connections, self.sessions, self.calls, self.events = [], [], [], []
        self.result = object()
        self.stack = ExitStack()

    def __enter__(self):
        self.stack.enter_context(patch.object(db_uow, "Session", side_effect=lambda **kw: kw["bind"].fixture_session))
        self.stack.enter_context(patch.object(db_uow, "acquire_corpus_write_session_lock", side_effect=lambda s: self.events.append("lock")))

        def release(session):
            self.events.append("unlock")
            if self.cleanup_failure == "unlock":
                raise RuntimeError("unlock failed")

        self.stack.enter_context(patch.object(db_uow, "release_corpus_write_session_lock", side_effect=release))
        return self

    def __exit__(self, *args):
        self.stack.close()

    def factory(self):
        index = len(self.connections)
        if index >= len(self.modes):
            raise AssertionError("unexpected extra UoW: bounded transaction budget exceeded")
        mode = self.modes[index]
        session, connection, engine = MagicMock(), MagicMock(), MagicMock()
        connection.invalidated = self.invalidated
        connection.fixture_session = session
        engine.connect.return_value = connection
        self.connections.append(connection)
        self.sessions.append(session)

        def rollback():
            self.events.append("rollback")
            if self.cleanup_failure == "rollback":
                raise RuntimeError("rollback failed")

        def invalidate():
            connection.invalidated = True
            self.events.append("invalidate")

        def close(which):
            self.events.append(which)
            if self.cleanup_failure == which:
                raise RuntimeError(f"{which} failed")

        commit_calls = [0]

        def commit():
            commit_calls[0] += 1
            self.events.append("commit")
            if mode == "commit_lost" and commit_calls[0] == 1:
                raise RuntimeError("business commit response lost")
            if mode == "commit_eintr" and commit_calls[0] == 1:
                raise self.error
            if self.cleanup_failure == "cleanup_commit":
                raise RuntimeError("unlock transaction commit failed")

        session.rollback.side_effect = rollback
        session.commit.side_effect = commit
        session.close.side_effect = lambda: close("session_close")
        connection.close.side_effect = lambda: close("connection_close")
        connection.invalidate.side_effect = invalidate
        harness = self

        class BoundUnitOfWork(db_uow.SqlAlchemyUnitOfWork):
            def _bind_repositories(self, ignored_session):
                def mutate(command):
                    harness.events.append("mutate")
                    harness.calls.append(command)
                    if mode == "eintr":
                        raise harness.error
                    return harness.result

                def reconcile(command):
                    harness.events.append("reconcile")
                    if mode == "reconcile_absent":
                        raise V4InitialIngressNotCommitted("fixture packet absent")
                    return SimpleNamespace(authority=harness.result)

                self.remote_parse_v4_ingress = SimpleNamespace(commit=mutate, reconcile=reconcile)

        return BoundUnitOfWork(engine=engine)


class DatabaseFileOpenInterruptionIndependentTests(unittest.TestCase):
    def test_concrete_uow_grants_proof_only_after_all_cleanup_completed(self):
        with _Harness(["eintr"]) as harness:
            with self.assertRaises(DatabaseInterruptionRolledBack) as raised:
                with replayable_transaction(), harness.factory() as uow:
                    raise harness.error
            proof = raised.exception
            self.assertIsNot(proof, harness.error)
            self.assertIs(proof.unit_of_work, uow)
            self.assertIs(proof.__cause__, harness.error)
            self.assertEqual(harness.events, ["lock", "rollback", "unlock", "commit", "session_close", "connection_close"])
            harness.connections[0].invalidate.assert_not_called()
            self.assertFalse(transaction_replay_declared())
        # The same concrete error outside the explicit opt-in stays untouched.
        with _Harness(["eintr"]) as harness:
            with self.assertRaises(InternalError) as raised:
                with harness.factory():
                    raise harness.error
            self.assertIs(raised.exception, harness.error)

    def test_rollback_unlock_and_both_close_failures_never_grant_replay(self):
        for failure in ("rollback", "unlock", "cleanup_commit", "session_close", "connection_close"):
            with self.subTest(failure=failure), _Harness(["eintr"], cleanup_failure=failure) as harness:
                with self.assertRaises(Exception) as raised:
                    DurableStagedIngressV4(uow_factory=harness.factory).execute(_command(), write_guard=lambda: None)
                self.assertEqual(len(harness.calls), 1)
                self.assertFalse(type(raised.exception).__module__.startswith("disclosure_anchor.application.ports."))

    def test_invalidated_connection_never_grants_replay(self):
        for physical_invalidated in (True, False):
            error = _db_error()
            error.connection_invalidated = not physical_invalidated
            with self.subTest(physical_invalidated=physical_invalidated), _Harness(
                ["eintr"], error=error, invalidated=physical_invalidated,
            ) as harness:
                with self.assertRaises(Exception) as raised:
                    DurableStagedIngressV4(uow_factory=harness.factory).execute(_command(), write_guard=lambda: None)
                self.assertEqual(len(harness.calls), 1)
                self.assertIs(raised.exception, harness.error)

    def test_explicit_business_commit_started_never_grants_rollback_proof(self):
        for mode in ("commit_eintr", "success"):
            with self.subTest(mode=mode), _Harness([mode]) as harness:
                with self.assertRaises(Exception) as raised:
                    with replayable_transaction(), harness.factory() as uow:
                        uow.commit()
                        raise harness.error
                self.assertIs(raised.exception, harness.error)

    def test_adjacent_db_errors_and_same_text_non_db_error_stop_without_replay(self):
        errors = (
            _db_error('could not open file "base/12345/67890_fsm": Input/output error'),
            _db_error('could not read file "base/12345/67890_fsm": Interrupted system call'),
            _db_error('could not open file "base/12345/67890_fsm": No such file or directory'),
            _db_error(_MESSAGE, _ServerCancelledError),
            _db_error(_MESSAGE, _ServerFatalError),
            _db_error(_MESSAGE, _ServerPanicError),
            InternalError("fixture", {}, RuntimeError(_MESSAGE)),
            RuntimeError(_MESSAGE),
            DatabaseInterruptionRolledBack("another UoW's rollback proof", unit_of_work=object()),
        )
        for error in errors:
            with self.subTest(error=str(error)), _Harness(["eintr"], error=error) as harness:
                with self.assertRaises(Exception) as raised:
                    DurableStagedIngressV4(uow_factory=harness.factory).execute(_command(), write_guard=lambda: None)
                self.assertIs(raised.exception, error)
                self.assertEqual(len(harness.calls), 1)

    def test_one_safe_replay_rechecks_guard_and_uses_identical_frozen_command(self):
        command = _command()
        with _Harness(["eintr", "success"]) as harness:
            guard = MagicMock()
            result = DurableStagedIngressV4(uow_factory=harness.factory).execute(command, write_guard=guard)
            self.assertIs(result, harness.result)
            self.assertEqual(guard.call_count, 3)
            self.assertEqual(len(harness.calls), 2)
            self.assertTrue(all(item is command for item in harness.calls))
            self.assertNotIn("reconcile", harness.events)
            first_close = harness.events.index("connection_close")
            second_mutation = harness.events.index("mutate", harness.events.index("mutate") + 1)
            self.assertLess(first_close, second_mutation)

    def test_second_guard_revocation_prevents_any_second_transaction(self):
        revoked = RuntimeError("write ownership revoked")
        with _Harness(["eintr"]) as harness:
            guard = MagicMock(side_effect=[None, revoked])
            with self.assertRaises(RuntimeError) as raised:
                DurableStagedIngressV4(uow_factory=harness.factory).execute(_command(), write_guard=guard)
            self.assertIs(raised.exception, revoked)
            self.assertEqual(len(harness.calls), 1)
            self.assertEqual(len(harness.connections), 1)

    def test_two_safe_interruptions_exhaust_budget_and_preserve_cause(self):
        with _Harness(["eintr", "eintr"]) as harness:
            with self.assertRaises(Exception) as raised:
                DurableStagedIngressV4(uow_factory=harness.factory).execute(_command(), write_guard=lambda: None)
            self.assertEqual(len(harness.calls), 2)
            causes = []
            error = raised.exception
            while error is not None:
                causes.append(error)
                error = error.__cause__
            self.assertIn(harness.error, causes)
            self.assertNotIn("reconcile", harness.events)

    def test_interrupt_then_commit_loss_share_two_write_budget(self):
        with _Harness(["eintr", "commit_lost", "reconcile_absent"]) as harness:
            with self.assertRaises(StagedIngressResponseLost):
                DurableStagedIngressV4(uow_factory=harness.factory).execute(_command(), write_guard=lambda: None)
            self.assertEqual(len(harness.calls), 2)
            self.assertEqual(harness.events.count("reconcile"), 1)

    def test_commit_loss_then_interrupt_share_two_write_budget(self):
        with _Harness(["commit_lost", "reconcile_absent", "eintr"]) as harness:
            with self.assertRaises(Exception):
                DurableStagedIngressV4(uow_factory=harness.factory).execute(_command(), write_guard=lambda: None)
            self.assertEqual(len(harness.calls), 2)
            self.assertEqual(harness.events.count("reconcile"), 1)

    def test_already_committed_reconciliation_does_not_replay(self):
        with _Harness(["commit_lost", "reconcile_found"]) as harness:
            result = DurableStagedIngressV4(uow_factory=harness.factory).execute(_command(), write_guard=lambda: None)
            self.assertIs(result, harness.result)
            self.assertEqual(len(harness.calls), 1)
            self.assertEqual(harness.events.count("reconcile"), 1)
