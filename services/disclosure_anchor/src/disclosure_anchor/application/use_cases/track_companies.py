"""Batch company-list intake — the production initialization entry.

The operator maintains one input (config/watchlist.csv, service-purpose §4.1);
this use case upserts it into tracked_company OFFLINE: subjects resolve via
SubjectResolver's no-name path (PENDING_LEGAL_NAME placeholder, upgraded in
place on the first credentialed sync), so intake needs no CNINFO reachability.
The worker then picks new companies up on its next round and backfills the
default initial lookback window.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.subject_resolver import (
    SubjectCandidate,
    SubjectResolver,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids

SYNC_FREQUENCIES = ("hourly", "daily", "weekly")


@dataclass(frozen=True)
class TrackEntry:
    security_code: str
    exchange: str
    lookback_days: int | None = None
    sync_frequency: str | None = None
    filing_categories: tuple[str, ...] | None = None
    status: str = "active"


@dataclass(frozen=True)
class TrackCompaniesCommand:
    entries: tuple[TrackEntry, ...]


@dataclass(frozen=True)
class TrackEntryResult:
    security_code: str
    exchange: str
    tracked_company_id: str
    company_id: str
    created: bool


@dataclass(frozen=True)
class TrackCompaniesResult:
    results: tuple[TrackEntryResult, ...]

    @property
    def created_count(self) -> int:
        return sum(1 for item in self.results if item.created)


class TrackCompanies:
    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory
        self._resolver = SubjectResolver()

    def execute(self, command: TrackCompaniesCommand) -> TrackCompaniesResult:
        for entry in command.entries:
            if entry.sync_frequency and entry.sync_frequency not in SYNC_FREQUENCIES:
                raise ValueError(
                    f"unknown sync_frequency {entry.sync_frequency!r} for "
                    f"{entry.security_code}: expected one of {SYNC_FREQUENCIES}"
                )
            if entry.lookback_days is not None and entry.lookback_days < 0:
                raise ValueError(
                    f"lookback_days must be non-negative for {entry.security_code}"
                )
        results: list[TrackEntryResult] = []
        with self._uow_factory() as uow:
            for entry in command.entries:
                results.append(self._track_one(uow, entry))
            uow.commit()
        return TrackCompaniesResult(results=tuple(results))

    def _track_one(self, uow: UnitOfWork, entry: TrackEntry) -> TrackEntryResult:
        subject = self._resolver.resolve(
            uow,
            SubjectCandidate(
                security_code=entry.security_code,
                exchange=entry.exchange,
                legal_name=None,
                board=None,
                credit_code=None,
            ),
        )
        lookback = (
            {"days": entry.lookback_days} if entry.lookback_days is not None else None
        )
        categories = list(entry.filing_categories) if entry.filing_categories else None
        existing = uow.tracked_companies.get_by_company_id(subject.company.company_id)
        if existing is not None:
            existing.security_id = subject.security.security_id
            existing.status = entry.status
            if lookback is not None:
                existing.lookback = lookback
            if categories is not None:
                existing.filing_categories = categories
            if entry.sync_frequency is not None:
                existing.sync_frequency = entry.sync_frequency
            uow.tracked_companies.update(existing)
            return TrackEntryResult(
                security_code=entry.security_code,
                exchange=entry.exchange,
                tracked_company_id=existing.tracked_company_id,
                company_id=subject.company.company_id,
                created=False,
            )
        tracked = uow.tracked_companies.add(
            e.TrackedCompany(
                tracked_company_id=ids.new_tracked_company_id(),
                company_id=subject.company.company_id,
                security_id=subject.security.security_id,
                status=entry.status,
                lookback=lookback,
                filing_categories=categories,
                sync_frequency=entry.sync_frequency,
            )
        )
        return TrackEntryResult(
            security_code=entry.security_code,
            exchange=entry.exchange,
            tracked_company_id=tracked.tracked_company_id,
            company_id=subject.company.company_id,
            created=True,
        )
