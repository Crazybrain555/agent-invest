"""Transactional in-memory store for processing-request unit tests.

Reads see committed rows plus the reader's own staged writes (PostgreSQL READ
COMMITTED shape); a per-key lock is held until the transaction ends, like the
xact advisory lock. Unique keys and the one-open-attempt rule are enforced at
write time, so the same races the database refuses are refused here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
import threading
from typing import Any

from disclosure_anchor.application.contracts.processing_request import (
    AttemptRecord,
    ProcessingFacts,
    ProcessingIntent,
    RequestRecord,
    TargetRecord,
)
from disclosure_anchor.application.ports.processing_requests import (
    LocalDocument,
    PersistedCandidate,
    ProcessingAttemptConflict,
)

from tests.unit._fakes import FakeUnitOfWork


class UniqueViolation(RuntimeError):
    pass


@dataclass
class _Committed:
    requests: dict[str, RequestRecord] = field(default_factory=dict)
    attempts: dict[str, list[AttemptRecord]] = field(default_factory=dict)
    targets: dict[str, TargetRecord] = field(default_factory=dict)


class ProcessingRequestStore:
    """Shared committed state, documents and candidates for one test."""

    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        self.clock = clock
        self.committed = _Committed()
        self.documents: dict[str, LocalDocument] = {}
        self.candidates: dict[str, PersistedCandidate] = {}
        self.guard = threading.Lock()
        self.key_locks: dict[str, threading.Lock] = {}
        self.admission_lock = threading.Lock()
        self.commits = 0

    def key_lock(self, key: str) -> threading.Lock:
        with self.guard:
            return self.key_locks.setdefault(key, threading.Lock())


class FakeProcessingRequestRepository:
    def __init__(self, store: ProcessingRequestStore) -> None:
        self._store = store
        self._requests: dict[str, RequestRecord] = {}
        self._attempt_writes: dict[str, list[AttemptRecord]] = {}
        self._targets: dict[str, TargetRecord] = {}
        self._held: list[threading.Lock] = []

    # -- transaction plumbing ---------------------------------------------------
    def end(self, *, commit: bool) -> None:
        if commit:
            with self._store.guard:
                committed = self._store.committed
                for request in self._requests.values():
                    if any(
                        existing.idempotency_key == request.idempotency_key
                        for existing in committed.requests.values()
                    ):
                        raise UniqueViolation("idempotency_key")
                    committed.requests[request.request_id] = request
                for request_id, attempts in self._attempt_writes.items():
                    committed.attempts[request_id] = list(attempts)
                for request_id, target in self._targets.items():
                    if request_id in committed.targets:
                        raise UniqueViolation("target request")
                    committed.targets[request_id] = target
                self._store.commits += 1
        self._requests.clear()
        self._attempt_writes.clear()
        self._targets.clear()
        while self._held:
            self._held.pop().release()

    # -- locks -------------------------------------------------------------------
    def lock_idempotency_key(self, idempotency_key: str) -> None:
        lock = self._store.key_lock(idempotency_key)
        lock.acquire()
        self._held.append(lock)

    def lock_request_admission(self) -> None:
        self._store.admission_lock.acquire()
        self._held.append(self._store.admission_lock)

    # -- requests ------------------------------------------------------------------
    def get(self, request_id: str) -> RequestRecord | None:
        if request_id in self._requests:
            return self._requests[request_id]
        with self._store.guard:
            return self._store.committed.requests.get(request_id)

    def get_by_key(self, idempotency_key: str) -> RequestRecord | None:
        for request in self._all_requests():
            if request.idempotency_key == idempotency_key:
                return request
        return None

    def count_created_since(self, since: datetime) -> int:
        return sum(1 for request in self._all_requests() if request.created_at >= since)

    def add_request(
        self, *, request_id: str, intent: ProcessingIntent, requested_by: str
    ) -> RequestRecord:
        record = RequestRecord(
            request_id=request_id,
            idempotency_key=intent.idempotency_key,
            intent_sha256=intent.intent_sha256,
            target=intent.target.to_json(),
            enqueue_parse=intent.enqueue_parse,
            created_at=self._store.clock(),
        )
        self._requests[request_id] = record
        return record

    def _all_requests(self) -> list[RequestRecord]:
        with self._store.guard:
            committed = list(self._store.committed.requests.values())
        return committed + list(self._requests.values())

    # -- attempts ------------------------------------------------------------------
    def attempts(self, request_id: str) -> list[AttemptRecord]:
        if request_id in self._attempt_writes:
            return list(self._attempt_writes[request_id])
        with self._store.guard:
            return list(self._store.committed.attempts.get(request_id, []))

    def due_discovery_requests(self, *, now: datetime, limit: int) -> list[RequestRecord]:
        due: list[tuple[datetime, RequestRecord]] = []
        for request in self._all_requests():
            if request.target.get("kind") not in ("provider_document", "title_match"):
                continue
            if self.target(request.request_id) is not None:
                continue
            attempts = self.attempts(request.request_id)
            latest = attempts[-1] if attempts else None
            if latest is None:
                due.append((request.created_at, request))
            elif latest.finished_at is None:
                if latest.lease_expires_at <= now:
                    due.append((latest.lease_expires_at, request))
            elif latest.next_attempt_at is not None and latest.next_attempt_at <= now:
                due.append((latest.next_attempt_at, request))
        due.sort(key=lambda item: (item[0], item[1].request_id))
        return [request for _, request in due[:limit]]

    def start_attempt(
        self,
        *,
        attempt_id: str,
        request_id: str,
        attempt_no: int,
        started_at: datetime,
        lease_expires_at: datetime,
    ) -> AttemptRecord:
        attempts = self.attempts(request_id)
        if any(attempt.finished_at is None for attempt in attempts) or any(
            attempt.attempt_no == attempt_no for attempt in attempts
        ):
            raise ProcessingAttemptConflict("open attempt exists")
        record = AttemptRecord(
            attempt_id=attempt_id,
            attempt_no=attempt_no,
            started_at=started_at,
            lease_expires_at=lease_expires_at,
        )
        self._attempt_writes[request_id] = [*attempts, record]
        return record

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
        if outcome == "abandoned":
            raise ValueError("an abandoned attempt is closed only by abandon_attempt")
        return self._close(
            attempt_id,
            lambda attempt: attempt.lease_expires_at > finished_at,
            finished_at=finished_at,
            outcome=outcome,
            next_attempt_at=next_attempt_at,
            profile_source_access_id=profile_source_access_id,
            index_source_access_id=index_source_access_id,
            provider_row_count=provider_row_count,
            choices=None if choices is None else tuple(dict(c) for c in choices),
            choices_truncated=choices_truncated,
            error=None if error is None else dict(error),
        )

    def abandon_attempt(
        self,
        *,
        attempt_id: str,
        finished_at: datetime,
        next_attempt_at: datetime | None,
        error: Mapping[str, object],
    ) -> bool:
        return self._close(
            attempt_id,
            lambda attempt: attempt.lease_expires_at <= finished_at,
            finished_at=finished_at,
            outcome="abandoned",
            next_attempt_at=next_attempt_at,
            error=dict(error),
        )

    def _close(
        self, attempt_id: str, allowed: Callable[[AttemptRecord], bool], **values: Any
    ) -> bool:
        """The guarded UPDATE: only an open attempt the lease rule allows."""

        for request_id in self._request_ids_with_attempts():
            attempts = self.attempts(request_id)
            for index, attempt in enumerate(attempts):
                if attempt.attempt_id != attempt_id:
                    continue
                if attempt.finished_at is not None or not allowed(attempt):
                    return False
                attempts[index] = replace(attempt, **values)
                self._attempt_writes[request_id] = attempts
                return True
        return False

    def _request_ids_with_attempts(self) -> list[str]:
        with self._store.guard:
            ids = set(self._store.committed.attempts)
        return sorted(ids | set(self._attempt_writes))

    # -- target ----------------------------------------------------------------------
    def target(self, request_id: str) -> TargetRecord | None:
        if request_id in self._targets:
            return self._targets[request_id]
        with self._store.guard:
            return self._store.committed.targets.get(request_id)

    def add_target(self, *, request_id: str, target: TargetRecord) -> TargetRecord:
        if self.target(request_id) is not None:
            raise UniqueViolation("target request")
        self._targets[request_id] = target
        return target

    # -- local identity --------------------------------------------------------------
    def document(self, document_id: str) -> LocalDocument | None:
        return self._store.documents.get(document_id)

    def latest_provider_document(
        self, *, provider: str, provider_document_id: str
    ) -> LocalDocument | None:
        for document in self._store.documents.values():
            if (
                document.provider == provider
                and document.provider_document_id == provider_document_id
            ):
                return document
        return None

    def persisted_candidate(self, provider_document_id: str) -> PersistedCandidate | None:
        return self._store.candidates.get(provider_document_id)


class FakeProcessingRequestUow(FakeUnitOfWork):
    """FakeUnitOfWork plus the transactional request store."""

    def __init__(self, store: ProcessingRequestStore, base: FakeUnitOfWork | None = None) -> None:
        super().__init__()
        if base is not None:
            # Share the plain fake repositories (source accesses, securities…)
            # across every unit of work of one test.
            self.__dict__.update(
                {
                    name: value
                    for name, value in base.__dict__.items()
                    if name not in ("commit_count", "rollback_count")
                }
            )
        self.processing_requests = FakeProcessingRequestRepository(store)
        self._committed = False

    def __enter__(self) -> FakeProcessingRequestUow:
        self._committed = False
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if not self._committed:
            self.processing_requests.end(commit=False)
        self.rollback_count += 1

    def commit(self) -> None:
        self.processing_requests.end(commit=True)
        self._committed = True
        self.commit_count += 1


def uow_factory(
    store: ProcessingRequestStore, base: FakeUnitOfWork | None = None
) -> Callable[[], FakeProcessingRequestUow]:
    shared = base or FakeUnitOfWork()
    return lambda: FakeProcessingRequestUow(store, shared)


class StoreStatusReader:
    """Status facts from the fake store; document/download facts injected."""

    def __init__(
        self,
        store: ProcessingRequestStore,
        *,
        max_download_retries: int = 3,
        facts: Callable[[RequestRecord, TargetRecord | None], dict[str, Any]] | None = None,
    ) -> None:
        self._store = store
        self._max_download_retries = max_download_retries
        self._facts = facts
        self.reads = 0

    def read(self, request_id: str, *, observed_at: datetime) -> ProcessingFacts | None:
        self.reads += 1
        with self._store.guard:
            request = self._store.committed.requests.get(request_id)
            attempts = tuple(self._store.committed.attempts.get(request_id, []))
            target = self._store.committed.targets.get(request_id)
        if request is None:
            return None
        extra = self._facts(request, target) if self._facts is not None else {}
        return ProcessingFacts(
            request=request,
            attempts=attempts,
            target=target,
            observed_at=observed_at,
            max_download_retries=self._max_download_retries,
            **extra,
        )


def local_document(
    document_id: str,
    *,
    status: str = "registered",
    provider_document_id: str = "1220000001",
    security_code: str = "600519",
    exchange: str = "SSE",
    announcement_date: date = date(2026, 3, 30),
) -> LocalDocument:
    return LocalDocument(
        document_id=document_id,
        status=status,
        raw_file_hash="sha256:" + "a" * 64,
        provider="cninfo",
        provider_document_id=provider_document_id,
        security_code=security_code,
        exchange=exchange,
        announcement_date=announcement_date,
    )
