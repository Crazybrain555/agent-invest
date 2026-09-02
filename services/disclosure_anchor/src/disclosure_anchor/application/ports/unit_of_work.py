"""Unit-of-work port.

The UnitOfWork is the transaction boundary for use cases. It exposes the
repositories and commit/rollback control. Concrete implementation lives in
``adapters/db/postgres``.
"""

from __future__ import annotations

from typing import Protocol, Self, runtime_checkable

from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Repository,
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
