"""Batch tracking-pool writes — the ONE write path for tracked_company.

The DB is the pool's source of truth (round22); every mutation route — CSV
import (`make track`), direct adds (`--codes`), and the admin API
(PUT /v1/admin/tracked-companies) — funnels through this use case OFFLINE:
subjects resolve via SubjectResolver's no-name path (PENDING_LEGAL_NAME
placeholder, upgraded in place on the first credentialed sync), so intake
needs no CNINFO reachability. The worker then picks new companies up on its
next round and backfills the default initial lookback window.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from disclosure_anchor.application.ports.disclosure_source import SourceCompanyProfile
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.subject_resolver import (
    PENDING_LEGAL_NAME_PREFIX,
    SubjectCandidate,
    SubjectResolver,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import DisclosureAnchorError
from disclosure_anchor.domain.value_objects import canonical_security_identity

SYNC_FREQUENCIES = ("hourly", "daily", "weekly")
# The pool has exactly two lifecycle states; the empty/None default of "active"
# is applied by the CSV/API intake layers before an entry reaches here.
TRACK_STATUSES = ("active", "paused")


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

    def __post_init__(self) -> None:
        security_code, exchange = canonical_security_identity(
            self.security_code, self.exchange
        )
        object.__setattr__(self, "security_code", security_code)
        object.__setattr__(self, "exchange", exchange)


TRACK_MODES = ("full_row", "ensure")


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
    # "full_row" = every route's historical upsert semantics (absent optional
    # field clears the stale override). "ensure" = membership only: existing
    # rows keep status and all overrides untouched (`track --codes` shortcut,
    # round23 — a quick add must not wipe a curated row).
    mode: str = "full_row"


@dataclass(frozen=True)
class TrackEntryResult:
    security_code: str
    exchange: str
    tracked_company_id: str
    company_id: str
    created: bool
    # Visibility for the full-row footgun (round23): which existing non-null
    # overrides this upsert cleared back to inherit, and any status flip.
    action: str = "updated"  # "created" | "updated" | "unchanged"
    cleared_overrides: tuple[str, ...] = ()
    status_change: str | None = None


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


@dataclass(frozen=True)
class ProfileResolution:
    security_code: str
    exchange: str
    legal_name: str | None
    resolved: bool


class ResolveTrackedProfiles:
    """Best-effort on-add company-name resolution (round22c).

    Industry analog: Miniflux / changedetection.io fetch feed/watch metadata
    at creation time. Intake stays offline-first; this runs AFTER the track
    upsert commits, upgrades PENDING_LEGAL_NAME placeholders through the
    same SubjectResolver path the credentialed sync uses, and keeps the
    placeholder on any per-company failure — the worker's first sync remains
    the fallback that heals whatever this pass could not.
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        profile_loader: Callable[[str], SourceCompanyProfile | None],
        resolver: SubjectResolver | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._profile_loader = profile_loader
        self._resolver = resolver or SubjectResolver()

    def execute(
        self, codes: tuple[tuple[str, str], ...]
    ) -> tuple[ProfileResolution, ...]:
        results: list[ProfileResolution] = []
        for security_code, exchange in codes:
            with self._uow_factory() as uow:
                security = uow.securities.get_by_code_exchange(security_code, exchange)
                company = (
                    uow.companies.get(security.company_id)
                    if security is not None and security.company_id
                    else None
                )
                if company is None or not company.legal_name.startswith(
                    PENDING_LEGAL_NAME_PREFIX
                ):
                    continue  # already resolved (or unknown) — nothing to do
                try:
                    profile = self._profile_loader(security_code)
                    if profile is None or not profile.legal_name:
                        results.append(
                            ProfileResolution(security_code, exchange, None, False)
                        )
                        continue
                    self._resolver.resolve(
                        uow,
                        SubjectCandidate(
                            security_code=security_code,
                            exchange=exchange,
                            legal_name=profile.legal_name,
                            board=None,
                            credit_code=profile.uscc,
                        ),
                    )
                    uow.commit()
                    results.append(
                        ProfileResolution(
                            security_code, exchange, profile.legal_name, True
                        )
                    )
                except DisclosureAnchorError as exc:
                    # Offline/quota/conflict: keep the placeholder, next sync heals.
                    results.append(
                        ProfileResolution(security_code, exchange, None, False)
                    )
                    if getattr(exc, "error_code", None) == "quota_exhausted":
                        # Quota is batch-wide. Do not burn one request per
                        # remaining code; placeholders are deliberately healed
                        # by the worker's first sync on a later quota window.
                        break
        return tuple(results)


@dataclass(frozen=True)
class UntrackEntryResult:
    security_code: str
    exchange: str
    tracked_company_id: str
    company_id: str


@dataclass(frozen=True)
class UntrackCompaniesResult:
    removed: tuple[UntrackEntryResult, ...]
    not_tracked: tuple[str, ...]


class UntrackCompanies:
    """Remove companies from the pool (delete the tracked row).

    Deletion semantics (round22, Miniflux DELETE-feed analog with the GLEIF
    registry discipline): the tracked row is operational subscription state
    and CAN be hard-deleted; the company/security ledger rows and any
    acquired documents are evidence and stay. Acquisition stops because the
    download queue only serves companies with an ACTIVE tracked row. For a
    reversible stop use status=paused instead; for full test-data removal
    use the purge-company CLI (test-phase tool).
    """

    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(self, codes: tuple[tuple[str, str], ...]) -> UntrackCompaniesResult:
        removed: list[UntrackEntryResult] = []
        missing: list[str] = []
        with self._uow_factory() as uow:
            for security_code, exchange in codes:
                security = uow.securities.get_by_code_exchange(security_code, exchange)
                tracked = (
                    uow.tracked_companies.get_by_company_id(security.company_id)
                    if security is not None and security.company_id
                    else None
                )
                if tracked is None:
                    missing.append(f"{security_code}.{exchange}")
                    continue
                uow.tracked_companies.delete(tracked.tracked_company_id)
                removed.append(
                    UntrackEntryResult(
                        security_code=security_code,
                        exchange=exchange,
                        tracked_company_id=tracked.tracked_company_id,
                        company_id=tracked.company_id,
                    )
                )
            uow.commit()
        return UntrackCompaniesResult(
            removed=tuple(removed), not_tracked=tuple(missing)
        )


class TrackCompanies:
    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory
        self._resolver = SubjectResolver()

    def execute(self, command: TrackCompaniesCommand) -> TrackCompaniesResult:
        if command.mode not in TRACK_MODES:
            raise ValueError(
                f"unknown mode {command.mode!r}: expected one of {TRACK_MODES}"
            )
        for entry in command.entries:
            if entry.status not in TRACK_STATUSES:
                # A misspelled status (e.g. "pasued") would silently drop the
                # company out of every queue; reject it up front like the other
                # override validations rather than persist it.
                raise ValueError(
                    f"unknown status {entry.status!r} for "
                    f"{entry.security_code}: expected one of {TRACK_STATUSES}"
                )
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
                results.append(self._track_one(uow, entry, mode=command.mode))
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

    def _track_one(
        self, uow: UnitOfWork, entry: TrackEntry, *, mode: str = "full_row"
    ) -> TrackEntryResult:
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
            if mode == "ensure":
                # Membership shortcut: the row exists, keep status and every
                # override exactly as curated (round23).
                return TrackEntryResult(
                    security_code=entry.security_code,
                    exchange=entry.exchange,
                    tracked_company_id=existing.tracked_company_id,
                    company_id=subject.company.company_id,
                    created=False,
                    action="unchanged",
                )
            cleared = tuple(
                name
                for name, old, new in (
                    ("lookback", existing.lookback, lookback),
                    ("process_classes", existing.process_classes, process_classes),
                    ("sync_frequency", existing.sync_frequency, entry.sync_frequency),
                )
                if old is not None and new is None
            )
            status_change = (
                f"{existing.status}->{entry.status}"
                if existing.status != entry.status
                else None
            )
            existing.security_id = subject.security.security_id
            existing.status = entry.status
            # Full-row upsert semantics on every route: an absent optional
            # field means "inherit the default", so an update must also CLEAR
            # a stale override (Codex acceptance P1: blank lookback left
            # {"days":30} behind). cleared_overrides/status_change give the
            # caller visibility into exactly that (round23).
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
                action="updated",
                cleared_overrides=cleared,
                status_change=status_change,
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
            action="created",
        )
