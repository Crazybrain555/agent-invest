"""SQLAlchemy UnitOfWork.

The UnitOfWork opens one session/transaction, exposes the repositories bound to
it, and owns commit/rollback. Exiting the context without an explicit commit
rolls back, so use cases must commit deliberately.
"""

from __future__ import annotations

from collections.abc import Callable
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session
from disclosure_anchor.application.ports import repositories as ports_repos
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.worker.locks import (
    acquire_corpus_write_session_lock,
    release_corpus_write_session_lock,
)
from disclosure_anchor.adapters.db.postgres.repositories import (
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


class SqlAlchemyUnitOfWork:
    """Transaction boundary backed by a single SQLAlchemy session."""

    # Attributes are declared with the *port* types so the concrete class
    # satisfies the UnitOfWork protocol (protocol attributes are invariant;
    # adapters speak to callers in contract types, not implementation types).
    companies: ports_repos.CompanyRepository
    company_identifiers: ports_repos.CompanyIdentifierRepository
    securities: ports_repos.SecurityRepository
    tracked_companies: ports_repos.TrackedCompanyRepository
    source_accesses: ports_repos.SourceAccessRepository
    source_checkpoints: ports_repos.SourceCheckpointRepository
    documents: ports_repos.DocumentRepository
    processing_runs: ports_repos.ProcessingRunRepository
    remote_parse_attempts: ports_repos.RemoteParseAttemptRepository
    document_units: ports_repos.DocumentUnitRepository
    outbox: ports_repos.OutboxRepository
    publish_evidence: ports_repos.PublishEvidenceRepository

    def __init__(
        self,
        *,
        engine: Engine,
    ) -> None:
        self._engine = engine
        self._connection: Connection | None = None
        self._session: Session | None = None
        self._corpus_write_lock_held = False

    # -- context management -------------------------------------------------
    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        # Session-level advisory locks belong to the physical PostgreSQL
        # connection, not to SQLAlchemy's Session. Binding the Session to an
        # explicitly checked-out Connection keeps that same backend pinned
        # across commit()/rollback() until __exit__ releases the lock.
        self._connection = self._engine.connect()
        self._session = Session(
            bind=self._connection,
            expire_on_commit=False,
            future=True,
        )
        try:
            acquire_corpus_write_session_lock(self._session)
            self._corpus_write_lock_held = True
            self._bind_repositories(self._session)
        except Exception:
            self._session.rollback()
            self._session.close()
            self._connection.close()
            self._session = None
            self._connection = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        try:
            if exc_type is not None:
                self.rollback()
            else:
                # Default-safe: rollback anything not explicitly committed.
                self.rollback()
        finally:
            assert self._session is not None
            assert self._connection is not None
            try:
                if self._corpus_write_lock_held:
                    release_corpus_write_session_lock(self._session)
                    self._corpus_write_lock_held = False
                self._session.commit()
            except BaseException:
                # Never return a connection with uncertain session-lock state
                # to the pool. A dead connection releases PostgreSQL advisory
                # locks server-side; an apparently live but inconsistent one
                # must be invalidated explicitly.
                self._connection.invalidate()
                raise
            finally:
                self._session.close()
                self._connection.close()
                self._session = None
                self._connection = None

    def _bind_repositories(self, session: Session) -> None:
        self.companies = CompanyRepository(session)
        self.company_identifiers = CompanyIdentifierRepository(session)
        self.securities = SecurityRepository(session)
        self.tracked_companies = TrackedCompanyRepository(session)
        self.source_accesses = SourceAccessRepository(session)
        self.source_checkpoints = SourceCheckpointRepository(session)
        self.documents = DocumentRepository(session)
        self.processing_runs = ProcessingRunRepository(session)
        self.remote_parse_attempts = RemoteParseAttemptRepository(session)
        self.document_units = DocumentUnitRepository(session)
        self.outbox = OutboxRepository(session)
        self.publish_evidence = PublishEvidenceRepository(session)

    # -- transaction control ------------------------------------------------
    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("UnitOfWork used outside of its context manager")
        return self._session

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def flush(self) -> None:
        self.session.flush()


def unit_of_work_factory(engine: Engine) -> Callable[[], UnitOfWork]:
    """Build UnitOfWork instances from a process-level engine."""

    return lambda: SqlAlchemyUnitOfWork(engine=engine)
