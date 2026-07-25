"""Worker advisory-lock namespaces and the document-level lock (08 §2).

Constants are asserted by tests/doctor against pg_locks.classid, so they must
never drift. The document lock is transaction-scoped and taken inside every
transaction that rewrites a document's state (register reuse, parse finish,
publish); it guards against a manual CLI run racing the worker on the same
document — cross-worker exclusion is already the singleton lock's job.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import zlib

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

WORKER_NS = 815001
DOC_NS = 815002
# Outbox commit-order lock (round23): serializes [first outbox insert ..
# commit] so committed seq order == commit order and `/v1/changes` cursor
# consumption never skips a late-committing lower seq. 815003 is taken by
# the integration-test suite namespace (tests/integration/_support.py).
OUTBOX_NS = 815004


class WorkerBusyError(RuntimeError):
    """A resident/manual producer already owns the shared GPU work path."""


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


def maybe_lock_document(uow: object, document_id: str) -> None:
    """Take the document lock when the UnitOfWork is SQL-backed.

    In-memory fakes used by unit tests expose no session; absence of a
    session means absence of concurrency, so skipping is correct there.
    """

    session = getattr(uow, "session", None)
    if isinstance(session, Session):
        acquire_document_xact_lock(session, document_id)
