"""Unit-of-work port.

The UnitOfWork is the transaction boundary for use cases. It exposes the
repositories and commit/rollback control. Concrete implementation lives in
``adapters/db/postgres``.

A caller that can replay a whole transaction declares it with
``replayable_transaction``. Only a unit of work entered inside that scope may
exit with ``DatabaseInterruptionRolledBack``; everywhere else the original
database error propagates unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol, Self, runtime_checkable

from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Repository,
)
from disclosure_anchor.application.ports.remote_parse_v4_failure_committer import (
    RemoteParseV4FailureCommitter,
)
from disclosure_anchor.application.ports.remote_parse_v4_ingress import (
    RemoteParseV4IngressCommitter,
)
from disclosure_anchor.application.ports.repositories import (
    CompanyIdentifierRepository,
    CompanyRepository,
    DocumentRepository,
    DocumentUnitRepository,
    OutboxRepository,
    ProcessingRunRepository,
    PublishEvidenceRepository,
    RemoteParseAttemptRepository,
    SecurityRepository,
    SourceAccessRepository,
    SourceCheckpointRepository,
    TrackedCompanyRepository,
)


_REPLAY_DECLARED: ContextVar[bool] = ContextVar(
    "unit_of_work_replay_declared", default=False,
)


class DatabaseInterruptionRolledBack(RuntimeError):
    """Proof that a database interruption left no trace of one transaction.

    A unit of work raises it, chained from the original database error, only
    when it was entered inside ``replayable_transaction``, the database
    interrupted a statement in a way the adapter recognizes as transient, no
    commit had begun, and the unit's own rollback, session-lock release and
    connection cleanup all succeeded. The proof covers the transaction of
    ``unit_of_work`` and nothing else.
    """

    def __init__(self, message: str, *, unit_of_work: object) -> None:
        super().__init__(message)
        self.unit_of_work = unit_of_work


@contextmanager
def replayable_transaction() -> Iterator[None]:
    """Declare that the caller can replay the units of work entered here.

    The caller holds the transaction's complete immutable input and replays
    all of it in a fresh unit of work.
    """

    token = _REPLAY_DECLARED.set(True)
    try:
        yield
    finally:
        _REPLAY_DECLARED.reset(token)


def transaction_replay_declared() -> bool:
    """Whether the current context is inside ``replayable_transaction``."""

    return _REPLAY_DECLARED.get()


@runtime_checkable
class UnitOfWork(Protocol):
    companies: CompanyRepository
    company_identifiers: CompanyIdentifierRepository
    securities: SecurityRepository
    tracked_companies: TrackedCompanyRepository
    source_accesses: SourceAccessRepository
    source_checkpoints: SourceCheckpointRepository
    documents: DocumentRepository
    processing_runs: ProcessingRunRepository
    remote_parse_attempts: RemoteParseAttemptRepository
    remote_parse_v4: RemoteParseV4Repository
    remote_parse_v4_failures: RemoteParseV4FailureCommitter
    remote_parse_v4_ingress: RemoteParseV4IngressCommitter
    document_units: DocumentUnitRepository
    outbox: OutboxRepository
    publish_evidence: PublishEvidenceRepository

    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def flush(self) -> None: ...
