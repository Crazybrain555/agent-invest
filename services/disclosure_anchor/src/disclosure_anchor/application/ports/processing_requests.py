"""Ports for explicit processing requests and quick original-text reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from disclosure_anchor.application.contracts.processing_request import (
    AttemptRecord,
    ProcessingFacts,
    ProcessingIntent,
    RequestRecord,
    TargetRecord,
)
from disclosure_anchor.application.contracts.quick_pdf_text import QuickPdfTextResult
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.domain.errors import DisclosureAnchorError


class ProcessingAttemptConflict(DisclosureAnchorError):
    """Another resolution attempt of the same request is already open."""


@dataclass(frozen=True)
class LocalDocument:
    """Registered document identity needed to bind a request target."""

    document_id: str
    status: str
    raw_file_hash: str | None
    provider: str | None
    provider_document_id: str | None
    security_code: str | None
    exchange: str | None
    announcement_date: date | None


@dataclass(frozen=True)
class PersistedCandidate:
    """The index candidate the download queue would read for one CNINFO id."""

    index_source_access_id: str
    candidate: Mapping[str, object]


class ProcessingRequestRepository(Protocol):
    def lock_idempotency_key(self, idempotency_key: str) -> None:
        """Serialize every writer of one key until the transaction ends."""
        ...

    def lock_request_admission(self) -> None:
        """Serialize new-request creation for the rolling limit."""
        ...

    def get(self, request_id: str) -> RequestRecord | None: ...

    def get_by_key(self, idempotency_key: str) -> RequestRecord | None: ...

    def count_created_since(self, since: datetime) -> int: ...

    def add_request(
        self, *, request_id: str, intent: ProcessingIntent, requested_by: str
    ) -> RequestRecord: ...

    def attempts(self, request_id: str) -> list[AttemptRecord]: ...

    def due_discovery_requests(self, *, now: datetime, limit: int) -> list[RequestRecord]:
        """Unresolved discovery requests whose next attempt is due, oldest first."""
        ...

    def start_attempt(
        self,
        *,
        attempt_id: str,
        request_id: str,
        attempt_no: int,
        started_at: datetime,
        lease_expires_at: datetime,
    ) -> AttemptRecord: ...

    def finish_attempt(
        self,
        *,
        attempt_id: str,
        outcome: str,
        finished_at: datetime,
        next_attempt_at: datetime | None = None,
        profile_source_access_id: str | None = None,
        index_source_access_id: str | None = None,
        provider_row_count: int | None = None,
        choices: Sequence[Mapping[str, object]] | None = None,
        choices_truncated: bool = False,
        error: Mapping[str, object] | None = None,
    ) -> bool:
        """Record the executor's own outcome while its lease still holds.

        False when the attempt is no longer open or its lease ended at or
        before ``finished_at``; the caller must then commit nothing from it.
        """
        ...

    def abandon_attempt(
        self,
        *,
        attempt_id: str,
        finished_at: datetime,
        next_attempt_at: datetime | None,
        error: Mapping[str, object],
    ) -> bool:
        """Close an open attempt whose lease ended at or before ``finished_at``."""
        ...

    def target(self, request_id: str) -> TargetRecord | None: ...

    def add_target(self, *, request_id: str, target: TargetRecord) -> TargetRecord: ...

    def document(self, document_id: str) -> LocalDocument | None: ...

    def latest_provider_document(
        self, *, provider: str, provider_document_id: str
    ) -> LocalDocument | None: ...

    def persisted_candidate(self, provider_document_id: str) -> PersistedCandidate | None: ...


class ProcessingRequestUnitOfWork(UnitOfWork, Protocol):
    processing_requests: ProcessingRequestRepository


class ProcessingStatusReader(Protocol):
    def read(self, request_id: str, *, observed_at: datetime) -> ProcessingFacts | None:
        """Every fact derive_status needs, read in one fresh read-only snapshot."""
        ...


@dataclass(frozen=True)
class OriginalLocator:
    """Private archived-original location of one registered document."""

    document_id: str
    status: str
    raw_file_hash: str | None
    raw_file_relpath: str | None
    provider: str | None
    provider_document_id: str | None
    security_code: str | None
    exchange: str | None
    title: str | None
    announcement_date: date | None
    source_ref: str | None = None
    company_ref: str | None = None
    security_ref: str | None = None


class OriginalLocatorReader(Protocol):
    def locate(self, document_id: str) -> OriginalLocator | None: ...


class QuickTextExtractor(Protocol):
    def extract(
        self,
        *,
        root: Path,
        relpath: Path,
        expected_sha256: str,
        cursor: str | None = None,
        start_page: int = 1,
        char_offset: int = 0,
        max_chars: int = 40000,
        max_pages: int = 20,
    ) -> QuickPdfTextResult: ...
