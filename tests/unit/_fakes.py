"""In-memory repositories for unit-level use case tests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Generic, TypeVar

from disclosure_anchor.domain import entities as e


T = TypeVar("T")


class _Repo(Generic[T]):
    def __init__(self, items: Iterable[T] = ()) -> None:
        self.items: dict[str, T] = {}
        for item in items:
            self.add(item)

    def add(self, item: T) -> T:
        self.items[self._key(item)] = item
        return item

    def get(self, item_id: str) -> T | None:
        return self.items.get(item_id)

    def update(self, item: T) -> T:
        self.items[self._key(item)] = item
        return item

    def all(self) -> list[T]:
        return list(self.items.values())

    def _key(self, item: T) -> str:
        raise NotImplementedError


class CompanyRepo(_Repo[e.Company]):
    def _key(self, item: e.Company) -> str:
        return item.company_id

    def get_by_legal_name(self, legal_name: str) -> e.Company | None:
        return next((item for item in self.items.values() if item.legal_name == legal_name), None)

    def get_by_credit_code(self, uscc: str) -> e.Company | None:
        return next(
            (
                item
                for item in self.items.values()
                if item.unified_social_credit_code == uscc
            ),
            None,
        )


class CompanyIdentifierRepo(_Repo[e.CompanyIdentifier]):
    def _key(self, item: e.CompanyIdentifier) -> str:
        return item.identifier_id

    def get_by_scheme_value(
        self, scheme: str, normalized_value: str
    ) -> e.CompanyIdentifier | None:
        return next(
            (
                item
                for item in self.items.values()
                if item.scheme == scheme
                and item.normalized_value == normalized_value
                and item.status == "active"
            ),
            None,
        )


class SecurityRepo(_Repo[e.Security]):
    def _key(self, item: e.Security) -> str:
        return item.security_id

    def get_by_code_exchange(self, security_code: str, exchange: str) -> e.Security | None:
        return next(
            (
                item
                for item in self.items.values()
                if item.security_code == security_code and item.exchange == exchange
            ),
            None,
        )


class SourceAccessRepo(_Repo[e.SourceAccess]):
    def _key(self, item: e.SourceAccess) -> str:
        return item.source_access_id


class SourceCheckpointRepo(_Repo[e.SourceCheckpoint]):
    def _key(self, item: e.SourceCheckpoint) -> str:
        return item.source_checkpoint_id


class TrackedCompanyRepo(_Repo[e.TrackedCompany]):
    def _key(self, item: e.TrackedCompany) -> str:
        return item.tracked_company_id


class DocumentRepo(_Repo[e.Document]):
    def _key(self, item: e.Document) -> str:
        return item.document_id

    def get_by_provider_document_and_hash(
        self, *, provider: str, provider_document_id: str, raw_file_hash: str
    ) -> e.Document | None:
        return next(
            (
                item
                for item in self.items.values()
                if item.provider == provider
                and item.provider_document_id == provider_document_id
                and item.raw_file_hash == raw_file_hash
            ),
            None,
        )

    def latest_by_provider_document(
        self, *, provider: str, provider_document_id: str
    ) -> e.Document | None:
        matches = [
            item
            for item in self.items.values()
            if item.provider == provider and item.provider_document_id == provider_document_id
        ]
        return matches[-1] if matches else None


class ProcessingRunRepo(_Repo[e.ProcessingRun]):
    def _key(self, item: e.ProcessingRun) -> str:
        return item.processing_run_id


class DocumentUnitRepo(_Repo[e.DocumentUnit]):
    def _key(self, item: e.DocumentUnit) -> str:
        return item.asset_id


class OutboxRepo(_Repo[e.OutboxEvent]):
    def __init__(self, items: Iterable[e.OutboxEvent] = ()) -> None:
        self._next_seq = 1
        super().__init__(items)

    def _key(self, item: e.OutboxEvent) -> str:
        return item.event_id

    def add(self, item: e.OutboxEvent) -> e.OutboxEvent:
        if item.seq is None:
            item.seq = self._next_seq
            self._next_seq += 1
        return super().add(item)


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.companies = CompanyRepo()
        self.company_identifiers = CompanyIdentifierRepo()
        self.securities = SecurityRepo()
        self.tracked_companies = TrackedCompanyRepo()
        self.source_accesses = SourceAccessRepo()
        self.source_checkpoints = SourceCheckpointRepo()
        self.documents = DocumentRepo()
        self.processing_runs = ProcessingRunRepo()
        self.document_units = DocumentUnitRepo()
        self.outbox = OutboxRepo()
        self.commit_count = 0
        self.rollback_count = 0

    def __enter__(self) -> "FakeUnitOfWork":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.rollback()
        else:
            self.rollback()

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def flush(self) -> None:
        return None
