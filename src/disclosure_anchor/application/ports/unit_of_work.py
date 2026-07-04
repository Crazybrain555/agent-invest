"""Unit-of-work port.

The UnitOfWork is the transaction boundary for use cases. It exposes the
repositories and commit/rollback control. Concrete implementation lives in
``adapters/db/postgres``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from disclosure_anchor.application.ports.repositories import (
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
    document_units: DocumentUnitRepository
    outbox: OutboxRepository

    def __enter__(self) -> "UnitOfWork": ...
    def __exit__(self, exc_type, exc, tb) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def flush(self) -> None: ...
