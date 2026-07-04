"""SQLAlchemy UnitOfWork.

The UnitOfWork opens one session/transaction, exposes the repositories bound to
it, and owns commit/rollback. Exiting the context without an explicit commit
rolls back, so use cases must commit deliberately.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
    create_session_factory,
)
from disclosure_anchor.adapters.db.postgres.repositories import (
    CompanyIdentifierRepository,
    CompanyRepository,
    DocumentRepository,
    DocumentUnitRepository,
    OutboxRepository,
    ProcessingRunRepository,
    SecurityRepository,
    SourceAccessRepository,
    SourceCheckpointRepository,
    TrackedCompanyRepository,
)


class SqlAlchemyUnitOfWork:
    """Transaction boundary backed by a single SQLAlchemy session."""

    def __init__(
        self,
        *,
        engine: Optional[Engine] = None,
        session_factory: Optional[sessionmaker[Session]] = None,
    ) -> None:
        if session_factory is None:
            if engine is None:
                raise ValueError("either engine or session_factory is required")
            session_factory = create_session_factory(engine)
        self._session_factory = session_factory
        self._session: Optional[Session] = None

    # -- context management -------------------------------------------------
    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        self._session = self._session_factory()
        self._bind_repositories(self._session)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is not None:
                self.rollback()
            else:
                # Default-safe: rollback anything not explicitly committed.
                self.rollback()
        finally:
            assert self._session is not None
            self._session.close()
            self._session = None

    def _bind_repositories(self, session: Session) -> None:
        self.companies = CompanyRepository(session)
        self.company_identifiers = CompanyIdentifierRepository(session)
        self.securities = SecurityRepository(session)
        self.tracked_companies = TrackedCompanyRepository(session)
        self.source_accesses = SourceAccessRepository(session)
        self.source_checkpoints = SourceCheckpointRepository(session)
        self.documents = DocumentRepository(session)
        self.processing_runs = ProcessingRunRepository(session)
        self.document_units = DocumentUnitRepository(session)
        self.outbox = OutboxRepository(session)

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


def unit_of_work_from_settings(settings) -> SqlAlchemyUnitOfWork:  # noqa: ANN001
    """Build a UnitOfWork bound to an engine created from the app DATABASE_URL."""

    engine = create_db_engine(app_database_url(settings))
    return SqlAlchemyUnitOfWork(engine=engine)
