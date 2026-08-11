"""Worker, corpus-admission, and document advisory-lock namespaces (08 §2).

DOC_NS is transaction-scoped and protects each state transition.  The separate
DOC_PRODUCER_NS session lease serializes a whole multi-transaction parse/build
lifecycle without conflicting with those nested transactions.  Every SQL UoW
holds CORPUS_WRITE_NS in shared session mode; cross-transaction filesystem
writers hold the same lease around their whole operation.  Destructive corpus
maintenance holds it exclusively through validation, DB/filesystem mutation,
and its durable receipt.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import zlib

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from disclosure_anchor.application.ports.unit_of_work import UnitOfWork

WORKER_NS = 815001
DOC_NS = 815002
# Outbox commit-order lock (round23): serializes [first outbox insert ..
# commit] so committed seq order == commit order and `/v1/changes` cursor
# consumption never skips a late-committing lower seq. 815003 is taken by
# the integration-test suite namespace (tests/integration/_support.py).
OUTBOX_NS = 815004
# Corpus-wide admission for every service UoW and filesystem writer.  Full
# reset and orphan GC take the exclusive side across their complete
# DB/filesystem critical section.
CORPUS_WRITE_NS = 815006
# Session lease for one document's multi-transaction producer lifecycle.
# This must be a different key from DOC_NS: nested UnitOfWork transactions
# use DOC_NS and may run on another pooled connection.
DOC_PRODUCER_NS = 815007


class WorkerBusyError(RuntimeError):
    """A resident/manual producer already owns the shared GPU work path."""


class CorpusWriteBusyError(RuntimeError):
    """Exclusive corpus maintenance is active or another exclusive owner exists."""


@contextmanager
def exclusive_worker_admission(engine: Engine) -> Iterator[None]:
    """Fail closed when another worker or manual parse producer is active."""

    with engine.connect() as conn:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:ns, 0)"),
                {"ns": WORKER_NS},
            ).scalar_one()
        )
        if not acquired:
            raise WorkerBusyError(
                "another worker or manual parse already owns GPU admission"
            )
        try:
            yield
        finally:
            conn.execute(
                text("SELECT pg_advisory_unlock(:ns, 0)"),
                {"ns": WORKER_NS},
            )


@contextmanager
def shared_corpus_mutation(engine: Engine) -> Iterator[None]:
    """Admit one direct, cross-transaction corpus writer."""

    with engine.connect() as conn:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock_shared(:ns, 0)"),
                {"ns": CORPUS_WRITE_NS},
            ).scalar_one()
        )
        if not acquired:
            raise CorpusWriteBusyError(
                "exclusive corpus maintenance is active; retry later"
            )
        conn.commit()
        try:
            yield
        finally:
            released = bool(
                conn.execute(
                    text("SELECT pg_advisory_unlock_shared(:ns, 0)"),
                    {"ns": CORPUS_WRITE_NS},
                ).scalar_one()
            )
            conn.commit()
            if not released:
                raise RuntimeError("corpus write admission lock was lost")


@contextmanager
def exclusive_corpus_mutation(engine: Engine) -> Iterator[None]:
    """Exclude all service UoWs throughout destructive corpus maintenance."""

    with engine.connect() as conn:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:ns, 0)"),
                {"ns": CORPUS_WRITE_NS},
            ).scalar_one()
        )
        if not acquired:
            raise CorpusWriteBusyError(
                "a corpus reader/writer or destructive maintenance process is active"
            )
        conn.commit()
        try:
            yield
        finally:
            released = bool(
                conn.execute(
                    text("SELECT pg_advisory_unlock(:ns, 0)"),
                    {"ns": CORPUS_WRITE_NS},
                ).scalar_one()
            )
            conn.commit()
            if not released:
                raise RuntimeError("exclusive corpus mutation lock was lost")


def acquire_corpus_write_xact_lock(connection: Connection) -> None:
    """Join corpus admission for one direct database transaction."""

    connection.execute(
        text("SELECT pg_advisory_xact_lock_shared(:ns, 0)"),
        {"ns": CORPUS_WRITE_NS},
    )


def stable_document_hash(document_id: str) -> int:
    """crc32 folded into signed int4 range (crc32 is unsigned 0..2^32-1)."""

    h = zlib.crc32(document_id.encode("utf-8"))
    return h - 2**32 if h >= 2**31 else h


def acquire_document_xact_lock(session: Session, document_id: str) -> None:
    """Blocking xact-scoped lock; released automatically at commit/rollback."""

    session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :h)"),
        {"ns": DOC_NS, "h": stable_document_hash(document_id)},
    )


def acquire_corpus_write_session_lock(session: Session) -> None:
    """Fail closed unless this SQL session joins corpus admission in shared mode."""

    acquired = bool(
        session.execute(
            text("SELECT pg_try_advisory_lock_shared(:ns, 0)"),
            {"ns": CORPUS_WRITE_NS},
        ).scalar_one()
    )
    if not acquired:
        raise CorpusWriteBusyError(
            "exclusive corpus maintenance is active; retry later"
        )


def release_corpus_write_session_lock(session: Session) -> None:
    """Release exactly one shared session-level corpus admission hold."""

    released = bool(
        session.execute(
            text("SELECT pg_advisory_unlock_shared(:ns, 0)"),
            {"ns": CORPUS_WRITE_NS},
        ).scalar_one()
    )
    if not released:
        raise RuntimeError("corpus write admission lock was lost")


@contextmanager
def shared_corpus_writer(
    uow_factory: Callable[[], UnitOfWork],
) -> Iterator[None]:
    """Span a multi-transaction DB/filesystem write with shared admission."""

    with uow_factory() as lock_uow:
        # SqlAlchemyUnitOfWork acquired one session-level shared hold on
        # entry.  End that otherwise-empty transaction immediately: the
        # advisory hold survives commit while no long-lived MVCC snapshot is
        # kept open during provider/GPU/filesystem work.
        if isinstance(getattr(lock_uow, "session", None), Session):
            lock_uow.commit()
        yield


@contextmanager
def exclusive_document_producer(
    uow_factory: Callable[[], UnitOfWork],
    document_id: str,
) -> Iterator[None]:
    """Serialize one document's full multi-transaction build."""

    with uow_factory() as lock_uow:
        session = getattr(lock_uow, "session", None)
        if not isinstance(session, Session):
            yield
            return
        document_hash = stable_document_hash(document_id)
        session.execute(
            text("SELECT pg_advisory_lock(:ns, :h)"),
            {"ns": DOC_PRODUCER_NS, "h": document_hash},
        )
        # Both the corpus admission and document lease are session-level.
        # End the lock-acquisition transaction before provider/GPU/file work.
        lock_uow.commit()
        try:
            yield
        finally:
            released = bool(
                session.execute(
                    text("SELECT pg_advisory_unlock(:ns, :h)"),
                    {"ns": DOC_PRODUCER_NS, "h": document_hash},
                ).scalar_one()
            )
            lock_uow.commit()
            if not released:
                raise RuntimeError("document producer lock was lost")


def maybe_lock_document(uow: object, document_id: str) -> None:
    """Take the document lock when the UnitOfWork is SQL-backed.

    In-memory fakes used by unit tests expose no session; absence of a
    session means absence of concurrency, so skipping is correct there.
    """

    session = getattr(uow, "session", None)
    if isinstance(session, Session):
        acquire_document_xact_lock(session, document_id)
