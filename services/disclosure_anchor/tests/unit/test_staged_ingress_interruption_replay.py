"""Bounded replay of a staged V4 ingress write after a proven EINTR rollback.

PostgreSQL reports an interrupted relation-file open as SQLSTATE XX000. The
adapter unit of work turns exactly that report into a typed rollback proof,
only inside a declared-replayable transaction, before any commit, and only
after its own rollback, session-lock release and connection cleanup. The
staged ingress replays its immutable command once on that proof, sharing the
two-write budget with commit-response-loss recovery. No database is used:
the SQLAlchemy session, connection and lock helpers are recording fakes, and
driver errors are built the way the engine wraps psycopg's.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import UTC, datetime
import errno
import itertools
from types import SimpleNamespace
import unittest
from unittest import mock

import psycopg
from psycopg import errors as pg_errors
from psycopg.pq import DiagnosticField
from sqlalchemy import exc as sa_exc

from disclosure_anchor.adapters.db.postgres import unit_of_work as db_uow
from disclosure_anchor.adapters.db.postgres.file_open_interruption import (
    is_interrupted_relation_file_open,
)
from disclosure_anchor.adapters.parsers.mineru_medium.v4_initial_ingress import (
    MinerUV4InitialIngressFactory,
)
from disclosure_anchor.application.contracts.mineru_process_profile import (
    encode_mineru_process_profile,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    SourcePdfObservation,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    build_staged_resource_credit_envelope,
)
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.remote_parse_v4_ingress import (
    V4InitialIngressDrift,
    V4InitialIngressNotCommitted,
)
from disclosure_anchor.application.ports.staged_new_work_v4 import V4RejectedSourcePdf
from disclosure_anchor.application.ports.unit_of_work import (
    DatabaseInterruptionRolledBack,
    replayable_transaction,
    transaction_replay_declared,
)
from disclosure_anchor.application.services.staged_ingress_v4 import (
    DurableStagedIngressV4,
    StagedIngressResponseLost,
)
from tests.unit import test_staged_new_work_admission_v4 as admission_fixture
from tests.unit import test_v4_initial_ingress as ingress_fixture
from tests.unit.test_mineru_process_profile import _profile


_INGRESS_LOGGER = "disclosure_anchor.application.services.staged_ingress_v4"
_PRODUCTION_REPORT = 'could not open file "base/16395/8556812_fsm": Interrupted system call'
_CLEANUP = ["rollback", "unlock", "unlock-commit", "session.close", "connection.close"]


def _driver_error(
    message: str = _PRODUCTION_REPORT,
    *,
    sqlstate: str = "XX000",
    severity: str | None = "ERROR",
    connection_invalidated: bool = False,
) -> BaseException:
    """A psycopg server error wrapped exactly as SQLAlchemy's engine wraps it."""

    info: dict[int, bytes | None] = {
        DiagnosticField.SQLSTATE: sqlstate.encode(),
        DiagnosticField.MESSAGE_PRIMARY: message.encode(),
    }
    if severity is not None:
        info[DiagnosticField.SEVERITY] = severity.encode()
        info[DiagnosticField.SEVERITY_NONLOCALIZED] = severity.encode()
    original = pg_errors.lookup(sqlstate)(message, info=info)
    return sa_exc.DBAPIError.instance(
        "INSERT INTO disclosure_ops.remote_parse_v4_checkpoint (attempt_id) VALUES (%(attempt_id)s)",
        {"attempt_id": "attempt-1"},
        original,
        psycopg.Error,
        connection_invalidated=connection_invalidated,
    )


class _Fake:
    """One pooled connection, its session and the corpus session lock.

    ``log`` records every step; ``failures`` raises once at the named step. A
    commit while the lock is held is the caller's business commit; the commit
    after ``unlock`` ends the cleanup transaction (``unlock-commit``).
    """

    def __init__(self) -> None:
        self.log: list[str] = []
        self.failures: dict[str, BaseException] = {}
        self.unlocked = False
        self.connection = _FakeConnection(self)
        self.session = _FakeSession(self)
        self.engine = _FakeEngine(self)

    def step(self, name: str) -> None:
        if name == "commit" and self.unlocked:
            name = "unlock-commit"
        self.log.append(name)
        if name in {"lock", "unlock"}:
            self.unlocked = name == "unlock"
        failure = self.failures.pop(name, None)
        if failure is not None:
            raise failure


class _FakeConnection:
    def __init__(self, fake: _Fake) -> None:
        self._fake = fake
        self.invalidated = False

    def invalidate(self) -> None:
        self._fake.log.append("invalidate")
        self.invalidated = True

    def close(self) -> None:
        self._fake.step("connection.close")


class _FakeSession:
    def __init__(self, fake: _Fake) -> None:
        self._fake = fake

    def rollback(self) -> None:
        self._fake.step("rollback")

    def commit(self) -> None:
        self._fake.step("commit")

    def close(self) -> None:
        self._fake.step("session.close")


class _FakeEngine:
    def __init__(self, fake: _Fake) -> None:
        self._fake = fake

    def connect(self) -> _FakeConnection:
        self._fake.log.append("connect")
        self._fake.connection.invalidated = False
        return self._fake.connection


_Unit = db_uow.SqlAlchemyUnitOfWork


@contextmanager
def _adapter(
    fake: _Fake, ingress: object | None = None,
) -> Iterator[tuple[Callable[[], _Unit], list[_Unit]]]:
    """Real ``SqlAlchemyUnitOfWork`` instances over the fake; records each unit."""

    units: list[_Unit] = []

    def factory() -> _Unit:
        unit = _Unit(engine=fake.engine)  # type: ignore[arg-type]
        units.append(unit)
        return unit

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(db_uow, "Session", lambda **_kwargs: fake.session))
        stack.enter_context(mock.patch.object(
            db_uow, "acquire_corpus_write_session_lock", lambda _session: fake.step("lock"),
        ))
        stack.enter_context(mock.patch.object(
            db_uow, "release_corpus_write_session_lock", lambda _session: fake.step("unlock"),
        ))
        if ingress is not None:
            stack.enter_context(mock.patch.object(
                db_uow, "PostgresRemoteParseV4IngressCommitter", lambda _session, **_kwargs: ingress,
            ))
        yield factory, units


class _Committer:
    """Scripted ``remote_parse_v4_ingress``.

    Each write or reconciliation takes its next outcome: an exception is
    raised, anything else is returned.
    """

    def __init__(self, *, writes: list[object], reconciles: list[object] | None = None) -> None:
        self.writes = list(writes)
        self.reconciles = list(reconciles or ())
        self.written: list[object] = []
        self.reconciled: list[object] = []

    @staticmethod
    def _next(outcomes: list[object]) -> object:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def commit(self, command: object) -> object:
        self.written.append(command)
        return self._next(self.writes)

    reject_source = commit

    def reconcile(self, command: object) -> object:
        self.reconciled.append(command)
        return SimpleNamespace(authority=self._next(self.reconciles))

    def reconcile_source_rejection(self, command: object) -> None:
        self.reconciled.append(command)
        self._next(self.reconciles)


def _commands() -> tuple[object, object]:
    """One real initial-ingress command and one real source-rejection command."""

    profile = _profile()
    events = itertools.count(1)
    factory = MinerUV4InitialIngressFactory(
        source=ingress_fixture._Source(),
        paths=ingress_fixture._Paths(),
        parser_identity=ParserIdentity(name="MinerU", version="3.4.4"),
        parser_options=ParserOptions(
            timeout_seconds=21_600,
            api_url="http://127.0.0.1:30003",
            api_drain_timeout_seconds=86_400,
            server_url="http://mineru-openai-server:30000/v1",
            http_request_concurrency=7,
            runtime_bundle_identity_sha256=profile.runtime_bundle_identity_sha256,
        ),
        process_profile=profile,
        process_profile_exact_bytes=encode_mineru_process_profile(profile),
        worker_profile=StagedWorkerProfileV4(profile.sha256, 1, 1),
        result_lease_seconds=300,
        remote_runaway_seconds=86_400,
        archive_member_count_limit=100_000,
        archive_uncompressed_byte_limit=16 * 1024 * 1024 * 1024,
        max_retries=3,
        scope_classes=("annual_report",),
        utc_now=lambda: datetime(2026, 10, 10, tzinfo=UTC),
        attempt_id_factory=lambda: "attempt-1",
        fence_id_factory=lambda: "fence-1",
        processing_run_id_factory=lambda: "run-1",
        outbox_event_id_factory=lambda: f"event-{next(events)}",
    )
    candidate = ingress_fixture._candidate()
    source_sha = candidate.raw_file_hash
    credit = build_staged_resource_credit_envelope(
        profile=profile, source_pdf_sha256=source_sha, source_byte_count=1024, source_page_count=2,
    )
    ingress = factory.build(
        candidate,
        source_observation=SourcePdfObservation(sha256=source_sha, byte_count=1024, page_count=2),
        available_credits=credit.reservation,
    )
    rejection = factory.source_rejection(
        candidate,
        V4RejectedSourcePdf(sha256=source_sha, byte_count=1024, reason_code="source_pdf_no_usable_pages"),
    )
    return ingress, rejection


class RelationFileOpenInterruptionTests(unittest.TestCase):
    def test_production_report_and_every_relation_fork_spelling_are_recognized(self) -> None:
        for path in (
            "base/16395/8556812_fsm", "base/16395/8556754_vm", "base/16395/8556812",
            "base/16395/8556812_init", "global/1262", "base/16395/t3_16400_fsm",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_interrupted_relation_file_open(
                    _driver_error(f'could not open file "{path}": Interrupted system call')
                ))

    def test_adjacent_database_reports_are_not_recognized(self) -> None:
        segment = 'could not open file "base/16395/8556812.1" (target block 131072): Interrupted system call'
        mismatched = pg_errors.InternalError_(_PRODUCTION_REPORT, info={
            DiagnosticField.SQLSTATE: b"XX001",
            DiagnosticField.SEVERITY_NONLOCALIZED: b"ERROR",
            DiagnosticField.MESSAGE_PRIMARY: _PRODUCTION_REPORT.encode(),
        })
        cases: dict[str, BaseException] = {
            "invalidated connection": _driver_error(connection_invalidated=True),
            "fatal": _driver_error(severity="FATAL"),
            "panic": _driver_error(severity="PANIC"),
            "severity absent": _driver_error(severity=None),
            "read": _driver_error(
                'could not read blocks 0..0 in file "base/16395/8556812_fsm": Interrupted system call'
            ),
            "write": _driver_error('could not write block 7 in file "base/16395/8556812": Interrupted system call'),
            "fsync": _driver_error('could not fsync file "base/16395/8556812": Interrupted system call'),
            "segment open": _driver_error(segment),
            "other errno": _driver_error('could not open file "base/16395/8556812_fsm": Input/output error'),
            "non-relation file": _driver_error(
                'could not open file "pg_logical/replorigin_checkpoint": Interrupted system call'
            ),
            "absolute path": _driver_error('could not open file "/private/tmp/16395": Interrupted system call'),
            "traversal": _driver_error(
                'could not open file "base/16395/../../global/1262": Interrupted system call'
            ),
            "prefixed": _driver_error("ERROR:  " + _PRODUCTION_REPORT),
            "suffixed": _driver_error(_PRODUCTION_REPORT + "\nDETAIL: retry"),
            "other internal error": _driver_error("cache lookup failed for relation 8556812"),
            "missing file": _driver_error(
                'could not open file "base/16395/8556812_fsm": No such file or directory', sqlstate="58P01",
            ),
            "io error": _driver_error(
                'could not open file "base/16395/8556812_fsm": Input/output error', sqlstate="58030",
            ),
            "disk full": _driver_error(
                'could not extend file "base/16395/8556812": No space left on device', sqlstate="53100",
            ),
            "statement timeout": _driver_error("canceling statement due to statement timeout", sqlstate="57014"),
            "lock timeout": _driver_error("canceling statement due to lock timeout", sqlstate="55P03"),
            "admin shutdown": _driver_error(
                "terminating connection due to administrator command", sqlstate="57P01", severity="FATAL",
            ),
            "sqlstate mismatch": sa_exc.DBAPIError.instance("SELECT 1", {}, mismatched, psycopg.Error),
        }
        for label, error in cases.items():
            with self.subTest(label):
                self.assertFalse(is_interrupted_relation_file_open(error))

    def test_non_driver_errors_are_not_recognized(self) -> None:
        duck = Exception(_PRODUCTION_REPORT)
        duck.sqlstate = "XX000"  # type: ignore[attr-defined]
        duck.diag = SimpleNamespace(  # type: ignore[attr-defined]
            sqlstate="XX000", severity_nonlocalized="ERROR", message_primary=_PRODUCTION_REPORT,
        )
        for error in (
            None,
            InterruptedError(errno.EINTR, "Interrupted system call"),
            OSError(errno.EINTR, "Interrupted system call"),
            KeyboardInterrupt(),
            TimeoutError(),
            RuntimeError(_PRODUCTION_REPORT),
            sa_exc.InternalError("SELECT 1", {}, duck),
            _driver_error().orig,
        ):
            with self.subTest(error=type(error).__name__):
                self.assertFalse(is_interrupted_relation_file_open(error))


class ReplayableUnitOfWorkTests(unittest.TestCase):
    def test_declared_interruption_is_proven_after_rollback_unlock_and_close(self) -> None:
        fake, error = _Fake(), _driver_error()
        with _adapter(fake) as (factory, _units):
            unit = factory()
            with self.assertRaises(DatabaseInterruptionRolledBack) as raised:
                with replayable_transaction(), unit:
                    raise error
        proof = raised.exception
        self.assertIs(proof.__cause__, error)
        self.assertIs(proof.unit_of_work, unit)
        self.assertEqual(fake.log, ["connect", "lock", *_CLEANUP])
        self.assertFalse(fake.connection.invalidated)
        self.assertNotIn("INSERT", str(proof))
        self.assertIsNone(getattr(error, "__notes__", None))

    def test_undeclared_or_unrecognized_errors_propagate_unchanged(self) -> None:
        cases = (
            ("undeclared interruption", False, _driver_error()),
            ("other internal error", True, _driver_error("cache lookup failed for relation 8556812")),
            ("cancellation", True, _driver_error("canceling statement due to user request", sqlstate="57014")),
            ("application error", True, ValueError("not a database error")),
        )
        for label, declared, error in cases:
            with self.subTest(label):
                fake = _Fake()
                with _adapter(fake) as (factory, _units):
                    with self.assertRaises(type(error)) as raised:
                        with replayable_transaction() if declared else nullcontext(), factory():
                            raise error
                self.assertIs(raised.exception, error)
                self.assertEqual(fake.log, ["connect", "lock", *_CLEANUP])
                self.assertIsNone(getattr(error, "__notes__", None))

    def test_any_commit_withholds_the_proof(self) -> None:
        for commit_interrupted in (False, True):
            with self.subTest(commit_interrupted=commit_interrupted):
                fake, error = _Fake(), _driver_error()
                if commit_interrupted:
                    fake.failures["commit"] = error
                with _adapter(fake) as (factory, _units):
                    with self.assertRaises(sa_exc.InternalError) as raised:
                        with replayable_transaction(), factory() as unit:
                            unit.commit()
                            raise error
                self.assertIs(raised.exception, error)
                self.assertEqual(fake.log, ["connect", "lock", "commit", *_CLEANUP])

    def test_unproven_cleanup_withholds_the_proof(self) -> None:
        for step in ("rollback", "unlock", "unlock-commit"):
            with self.subTest(failing=step):
                fake, error = _Fake(), _driver_error()
                with _adapter(fake) as (factory, _units):
                    with self.assertRaises(sa_exc.InternalError) as raised:
                        with replayable_transaction(), factory():
                            fake.failures[step] = RuntimeError(f"{step} failed")
                            raise error
                self.assertIs(raised.exception, error)
                self.assertTrue(fake.connection.invalidated)
                self.assertIn("SqlAlchemyUnitOfWork cleanup also failed", "\n".join(error.__notes__))
        with self.subTest("connection invalidated before cleanup"):
            fake, error = _Fake(), _driver_error()
            with _adapter(fake) as (factory, _units):
                with self.assertRaises(sa_exc.InternalError) as raised:
                    with replayable_transaction(), factory():
                        fake.connection.invalidate()
                        raise error
            self.assertIs(raised.exception, error)
            self.assertNotIn("unlock", fake.log)
            self.assertIsNone(getattr(error, "__notes__", None))

    def test_close_failure_is_never_replaced_by_a_proof(self) -> None:
        fake, error, closing = _Fake(), _driver_error(), OSError("connection close failed")
        with _adapter(fake) as (factory, _units):
            with self.assertRaises(OSError) as raised:
                with replayable_transaction(), factory():
                    fake.failures["connection.close"] = closing
                    raise error
        self.assertIs(raised.exception, closing)

    def test_declaration_is_scoped_and_fixed_at_entry(self) -> None:
        self.assertFalse(transaction_replay_declared())
        with replayable_transaction():
            with replayable_transaction():
                self.assertTrue(transaction_replay_declared())
            self.assertTrue(transaction_replay_declared())
        with self.assertRaises(ValueError), replayable_transaction():
            raise ValueError("body failed")
        self.assertFalse(transaction_replay_declared())
        fake, error = _Fake(), _driver_error()
        with _adapter(fake) as (factory, _units):
            unit = factory()
            unit.__enter__()
            with replayable_transaction():
                self.assertIsNone(unit.__exit__(type(error), error, None))


class DurableStagedIngressReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.command, self.rejection = _commands()
        self.guard_calls = 0

    def _guard(self) -> None:
        self.guard_calls += 1

    def test_interrupted_write_replays_the_same_command_once_in_a_fresh_unit(self) -> None:
        authority = object()
        fake, committer = _Fake(), _Committer(writes=[_driver_error(), authority])
        with _adapter(fake, committer) as (factory, units):
            with self.assertLogs(_INGRESS_LOGGER, level="WARNING") as logs:
                result = DurableStagedIngressV4(uow_factory=factory).execute(
                    self.command, write_guard=self._guard,
                )
        self.assertIs(result, authority)
        self.assertEqual(len(committer.written), 2)
        self.assertTrue(all(written is self.command for written in committer.written))
        self.assertEqual(committer.reconciled, [])
        self.assertEqual(len(units), 2)
        self.assertIsNot(units[0], units[1])
        # Before each write, and before the one commit that was reached.
        self.assertEqual(self.guard_calls, 3)
        self.assertEqual(fake.log, ["connect", "lock", *_CLEANUP, "connect", "lock", "commit", *_CLEANUP])
        [line] = logs.output
        self.assertIn(f"staged ingress {self.command.proposal.attempt_id}: write 1/2", line)
        for private in ("INSERT", "attempt_id':", self.command.expected_raw_file_hash):
            self.assertNotIn(private, line)

    def test_source_rejection_shares_the_replay_rule(self) -> None:
        committer = _Committer(writes=[_driver_error(), None])
        with _adapter(_Fake(), committer) as (factory, units):
            with self.assertLogs(_INGRESS_LOGGER, level="WARNING") as logs:
                DurableStagedIngressV4(uow_factory=factory).reject_source(
                    self.rejection, write_guard=self._guard,
                )
        self.assertEqual(len(committer.written), 2)
        self.assertTrue(all(written is self.rejection for written in committer.written))
        self.assertEqual(len(units), 2)
        self.assertIn(f"staged ingress {self.rejection.processing_run_id}: write 1/2", logs.output[0])

    def test_repeated_interruption_ends_visible_with_the_original_chain(self) -> None:
        second = _driver_error()
        committer = _Committer(writes=[_driver_error(), second])
        with _adapter(_Fake(), committer) as (factory, units):
            with self.assertLogs(_INGRESS_LOGGER, level="WARNING") as logs:
                with self.assertRaises(DatabaseInterruptionRolledBack) as raised:
                    DurableStagedIngressV4(uow_factory=factory).execute(
                        self.command, write_guard=self._guard,
                    )
        proof = raised.exception
        self.assertIs(proof.__cause__, second)
        self.assertIsInstance(second.orig, pg_errors.InternalError_)  # type: ignore[attr-defined]
        self.assertIs(proof.unit_of_work, units[1])
        self.assertIn("write 2/2 rolled back after a database interruption; no replay left",
                      "\n".join(proof.__notes__))
        self.assertEqual(len(committer.written), 2)
        self.assertEqual(committer.reconciled, [])
        self.assertEqual(len(logs.output), 1)

    def test_replay_and_commit_response_loss_share_two_writes(self) -> None:
        reconciled_authority = object()
        cases = (
            # (label, writes, reconciles, response lost on business commit number)
            ("loss then interruption", [object(), _driver_error()], [V4InitialIngressNotCommitted("absent")]),
            ("interruption then absent loss", [_driver_error(), object()], [V4InitialIngressNotCommitted("absent")]),
            ("interruption then committed loss", [_driver_error(), object()], [reconciled_authority]),
        )
        for label, writes, reconciles in cases:
            with self.subTest(label):
                fake = _Fake()
                lost = _driver_error(
                    "server closed the connection unexpectedly", sqlstate="08006", connection_invalidated=True,
                )
                fake.failures["commit"] = lost
                committer = _Committer(writes=writes, reconciles=reconciles)
                with _adapter(fake, committer) as (factory, units):
                    ingress = DurableStagedIngressV4(uow_factory=factory)
                    replays = (
                        self.assertLogs(_INGRESS_LOGGER, level="WARNING")
                        if label.startswith("interruption") else self.assertNoLogs(_INGRESS_LOGGER)
                    )
                    with replays:
                        if label == "loss then interruption":
                            with self.assertRaises(DatabaseInterruptionRolledBack):
                                ingress.execute(self.command, write_guard=self._guard)
                        elif label == "interruption then absent loss":
                            with self.assertRaisesRegex(StagedIngressResponseLost, "remained absent") as raised:
                                ingress.execute(self.command, write_guard=self._guard)
                            self.assertIs(raised.exception.__cause__, lost)
                        else:
                            self.assertIs(
                                ingress.execute(self.command, write_guard=self._guard), reconciled_authority,
                            )
                self.assertEqual(len(committer.written), 2)
                self.assertEqual(committer.reconciled, [self.command])
                # Two write units plus one read-only reconciliation unit, never a third write.
                self.assertEqual(len(units), 3)

    def test_commit_time_interruption_is_reconciled_not_proven(self) -> None:
        authority = object()
        fake = _Fake()
        fake.failures["commit"] = _driver_error()
        committer = _Committer(writes=[authority, authority], reconciles=[V4InitialIngressNotCommitted("absent")])
        with _adapter(fake, committer) as (factory, _units):
            with self.assertNoLogs(_INGRESS_LOGGER):
                result = DurableStagedIngressV4(uow_factory=factory).execute(
                    self.command, write_guard=self._guard,
                )
        self.assertIs(result, authority)
        self.assertEqual(committer.reconciled, [self.command])
        self.assertEqual(len(committer.written), 2)

    def test_unproven_rollback_is_not_replayed(self) -> None:
        fake, error = _Fake(), _driver_error()
        fake.failures["rollback"] = RuntimeError("rollback failed")
        committer = _Committer(writes=[error])
        with _adapter(fake, committer) as (factory, units):
            with self.assertNoLogs(_INGRESS_LOGGER), self.assertRaises(sa_exc.InternalError) as raised:
                DurableStagedIngressV4(uow_factory=factory).execute(
                    self.command, write_guard=self._guard,
                )
        self.assertIs(raised.exception, error)
        self.assertIn("SqlAlchemyUnitOfWork cleanup also failed", "\n".join(error.__notes__))
        self.assertEqual(len(committer.written), 1)
        self.assertEqual(len(units), 1)

    def test_replay_needs_the_guard_again_and_stops_on_drift(self) -> None:
        with self.subTest("guard rejects the replay"):
            rejected = RuntimeError("admission readiness lost")

            def guard() -> None:
                self._guard()
                if self.guard_calls == 2:
                    raise rejected

            committer = _Committer(writes=[_driver_error()])
            with _adapter(_Fake(), committer) as (factory, units):
                with self.assertLogs(_INGRESS_LOGGER, level="WARNING"), self.assertRaises(RuntimeError) as raised:
                    DurableStagedIngressV4(uow_factory=factory).execute(
                        self.command, write_guard=guard,
                    )
            self.assertIs(raised.exception, rejected)
            self.assertEqual(len(committer.written), 1)
            self.assertEqual(len(units), 1)
        with self.subTest("replay finds a partial packet"):
            drift = V4InitialIngressDrift("v4 initial ingress packet is only partially present")
            committer = _Committer(writes=[_driver_error(), drift])
            with _adapter(_Fake(), committer) as (factory, _units):
                with self.assertLogs(_INGRESS_LOGGER, level="WARNING"), self.assertRaises(
                    V4InitialIngressDrift,
                ) as raised:
                    DurableStagedIngressV4(uow_factory=factory).execute(
                        self.command, write_guard=self._guard,
                    )
            self.assertIs(raised.exception, drift)
            self.assertEqual(committer.reconciled, [])

    def test_a_proof_for_another_unit_is_not_replayed(self) -> None:
        stranger = object()
        committer = _Committer(writes=[_driver_error()])

        class _OtherProofUnit:
            remote_parse_v4_ingress = committer

            def __enter__(self) -> _OtherProofUnit:
                return self

            def __exit__(self, _type: object, exc: BaseException | None, _tb: object) -> None:
                if exc is not None:
                    raise DatabaseInterruptionRolledBack("rolled back", unit_of_work=stranger) from exc

        units: list[_OtherProofUnit] = []

        def factory() -> _OtherProofUnit:
            units.append(_OtherProofUnit())
            return units[-1]

        with self.assertNoLogs(_INGRESS_LOGGER), self.assertRaises(DatabaseInterruptionRolledBack) as raised:
            DurableStagedIngressV4(uow_factory=factory).execute(  # type: ignore[arg-type]
                self.command, write_guard=self._guard,
            )
        self.assertIs(raised.exception.unit_of_work, stranger)
        self.assertEqual(len(units), 1)


class StagedAdmissionReplayTests(unittest.TestCase):
    def test_admission_replays_only_the_transaction_never_source_or_build(self) -> None:
        # The shared admission fixture: one ordinary candidate, its real MinerU
        # ingress factory and an H0 committer over the candidate view.
        admitter, candidates, claims, claimed, credit = (
            admission_fixture.StagedV4NewWorkAdmitterTests._fixture(self)  # type: ignore[arg-type]
        )
        fixture_ingress = admitter._ingress._uow_factory().remote_parse_v4_ingress
        written: list[object] = []

        class _InterruptedOnce:
            def commit(self, command: object) -> object:
                written.append(command)
                if len(written) == 1:
                    raise _driver_error()
                return fixture_ingress.commit(command)

        with _adapter(_Fake(), _InterruptedOnce()) as (factory, _units):
            admitter._ingress = DurableStagedIngressV4(uow_factory=factory)
            build = admitter._ingress_factory.build
            with mock.patch.object(admitter._ingress_factory, "build", wraps=build) as built:
                with self.assertLogs(_INGRESS_LOGGER, level="WARNING"):
                    outcome = admission_fixture.StagedV4NewWorkAdmitterTests._admit(
                        admitter, limit=1, available_credits=credit.reservation,
                    )
        self.assertEqual(outcome.work, (claimed,))
        self.assertEqual(claims.claims, ["attempt-1"])
        self.assertEqual(candidates.reads, ["doc-1"])
        self.assertEqual(candidates.admitted, ["doc-1"])
        self.assertEqual(built.call_count, 1)
        self.assertEqual(len(written), 2)
        self.assertIs(written[0], written[1])


if __name__ == "__main__":
    unittest.main()
