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


def _known_classes() -> frozenset[str]:
    from disclosure_anchor.adapters.sources.cninfo.mapper import load_class_map

    return frozenset(load_class_map()["classes"])


@dataclass(frozen=True)
class TrackEntry:
    security_code: str
    exchange: str
    lookback_days: int | None = None
    sync_frequency: str | None = None
    process_classes: tuple[str, ...] | None = None
    status: str = "active"


@dataclass(frozen=True)
class TrackCompaniesCommand:
    entries: tuple[TrackEntry, ...]
    # Full reconciliation (design/watchlist-operations.md §5.2): tracked rows
    # absent from the entries are reported as drift; prune_drift pauses them.
    reconcile: bool = False
    prune_drift: bool = False
    # Plan mode (terraform plan / HA check_config pattern): compute the full
    # reconcile outcome, then roll back instead of committing.
    dry_run: bool = False


@dataclass(frozen=True)
class TrackEntryResult:
    security_code: str
    exchange: str
    tracked_company_id: str
    company_id: str
    created: bool


@dataclass(frozen=True)
class DriftEntry:
    tracked_company_id: str
    company_id: str
    security_code: str | None
    status: str
    action: str  # "reported" | "paused"


@dataclass(frozen=True)
class TrackCompaniesResult:
    results: tuple[TrackEntryResult, ...]
    drift: tuple[DriftEntry, ...] = ()
    dry_run: bool = False

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
            if entry.process_classes:
                unknown = [c for c in entry.process_classes if c not in _known_classes()]
                if unknown:
                    raise ValueError(
                        f"unknown process_classes {unknown} for "
                        f"{entry.security_code}: see class_map.json"
                    )
        results: list[TrackEntryResult] = []
        drift: list[DriftEntry] = []
        with self._uow_factory() as uow:
            for entry in command.entries:
                results.append(self._track_one(uow, entry))
            if command.reconcile:
                drift = self._reconcile_drift(
                    uow,
                    known_company_ids={item.company_id for item in results},
                    prune=command.prune_drift,
                )
            if not command.dry_run:
                uow.commit()
        return TrackCompaniesResult(
            results=tuple(results), drift=tuple(drift), dry_run=command.dry_run
        )

    def _reconcile_drift(
        self, uow: UnitOfWork, *, known_company_ids: set[str], prune: bool
    ) -> list[DriftEntry]:
        drift: list[DriftEntry] = []
        for tracked in uow.tracked_companies.list_all():
            if tracked.company_id in known_company_ids:
                continue
            security = (
                uow.securities.get(tracked.security_id)
                if tracked.security_id
                else None
            )
            action = "reported"
            if prune and tracked.status == "active":
                tracked.status = "paused"
                uow.tracked_companies.update(tracked)
                action = "paused"
            drift.append(
                DriftEntry(
                    tracked_company_id=tracked.tracked_company_id,
                    company_id=tracked.company_id,
                    security_code=security.security_code if security else None,
                    status=tracked.status,
                    action=action,
                )
            )
        return drift

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
        process_classes = list(entry.process_classes) if entry.process_classes else None
        existing = uow.tracked_companies.get_by_company_id(subject.company.company_id)
        if existing is not None:
            existing.security_id = subject.security.security_id
            existing.status = entry.status
            # CSV is the single source of truth: a blank cell means "use the
            # default", so reconcile must also CLEAR a stale DB override
            # (Codex acceptance P1: blank lookback left {"days":30} behind).
            existing.lookback = lookback
            existing.process_classes = process_classes
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
                process_classes=process_classes,
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
