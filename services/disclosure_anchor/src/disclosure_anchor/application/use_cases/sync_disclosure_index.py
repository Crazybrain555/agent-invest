"""Synchronize disclosure index candidates from a provider source."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureSourcePort,
    DisclosureWindow,
    SourceCompanyProfile,
    SourceSecurity,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.subject_resolver import (
    SubjectCandidate,
    SubjectResolver,
)
from disclosure_anchor.application.services.cninfo_profile_access import (
    CNINFO_PROFILE_INTERFACE,
    add_cninfo_profile_access,
    add_failed_cninfo_profile_access,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import DisclosureAnchorError, SourceRequestError


CNINFO_PROVIDER = "cninfo"
PROFILE_INTERFACE = CNINFO_PROFILE_INTERFACE
INDEX_INTERFACE = "cninfo:p_info3015"
# Credential-free public-website channel (same provider namespace; see
# adapters/sources/cninfo/web_source.py for the verified id/size equivalence).
WEB_INDEX_INTERFACE = "cninfo:hisAnnouncement"


class SyncDisclosureIndexError(DisclosureAnchorError):
    """Raised when index sync fails after durable failure state is recorded."""


class CompanyNotTrackedError(DisclosureAnchorError):
    """Sync requires prior pool membership; tracking is an explicit operator act.

    Sync never adds a company to the tracked pool and never changes its
    status: `make track` / PUT /v1/admin/tracked-companies own membership,
    `paused` stays paused across explicit syncs (round23).
    """

    def __init__(self, security_code: str, exchange: str) -> None:
        self.security_code = security_code
        self.exchange = exchange
        super().__init__(
            f"company {security_code} ({exchange}) is not in the tracked pool; "
            "track it first via `make track CODES=...` or "
            "PUT /v1/admin/tracked-companies"
        )


@dataclass(frozen=True)
class SyncDisclosureIndexCommand:
    security_code: str
    exchange: str
    window_start: date
    window_end: date


@dataclass(frozen=True)
class SyncDisclosureIndexResult:
    company_id: str
    security_id: str
    profile_source_access_id: str
    index_source_access_id: str
    checkpoint_id: str
    candidate_count: int
    empty: bool


def compute_sync_window(
    *,
    uow_factory: Callable[[], UnitOfWork],
    company: str,
    exchange: str,
    explicit_window_days: int | None,
    today: date,
    overlap_days: int,
    initial_lookback_days: int = 1095,
    explicit_window_start: date | None = None,
    explicit_window_end: date | None = None,
) -> tuple[date, date]:
    """Effective sync window for one company (shared by CLI sync and the
    on-demand admin sync endpoint): explicit date range > explicit window
    days > checkpoint+overlap > per-company lookback > global initial
    backfill.

    The explicit [start, end] range is the backfill channel (round23,
    industry-standard shape: Airflow/edgartools/OpenBB all take absolute
    date ranges for历史补数); the pool's lookback_days stays the relative
    first-sync depth and is never a query parameter.
    """

    if explicit_window_start is not None or explicit_window_end is not None:
        if explicit_window_days is not None:
            raise ValueError("window and from/to are mutually exclusive")
        if explicit_window_start is None or explicit_window_end is None:
            raise ValueError("from/to must be provided together")
        if explicit_window_start > explicit_window_end:
            raise ValueError("window start must not be after window end")
        if explicit_window_end > today:
            raise ValueError("window end must not be in the future")
        return explicit_window_start, explicit_window_end
    if explicit_window_days is not None:
        if explicit_window_days < 0:
            raise ValueError("window must be non-negative")
        return today - timedelta(days=explicit_window_days), today
    with uow_factory() as uow:
        security = uow.securities.get_by_code_exchange(company, exchange)
        if security is None:
            # First contact: default historical backfill (user decision
            # 2026-07-06, 三年是底线); explicit window stays the override.
            return today - timedelta(days=initial_lookback_days), today
        checkpoint = uow.source_checkpoints.get_by_scope(
            "cninfo", f"{security.company_id}:p_info3015"
        )
        if checkpoint is None or not checkpoint.cursor:
            tracked = uow.tracked_companies.get_by_company_id(security.company_id)
            days = initial_lookback_days
            if tracked and isinstance(tracked.lookback, dict):
                override = tracked.lookback.get("days")
                if isinstance(override, int) and override >= 0:
                    days = override
            return today - timedelta(days=days), today
        window_end = checkpoint.cursor.get("window_end")
        if not isinstance(window_end, str):
            raise ValueError("checkpoint cursor missing window_end")
        return date.fromisoformat(window_end) - timedelta(days=overlap_days), today


class SyncDisclosureIndex:
    """Persist CNINFO announcement candidates and advance checkpoint safely."""

    def __init__(
        self,
        *,
        source: DisclosureSourcePort,
        profile_loader: Callable[[str], SourceCompanyProfile | None],
        uow_factory: Callable[[], UnitOfWork],
        subject_resolver: SubjectResolver | None = None,
        index_interface: str = INDEX_INTERFACE,
    ) -> None:
        self._source = source
        self._profile_loader = profile_loader
        self._uow_factory = uow_factory
        self._subject_resolver = subject_resolver or SubjectResolver()
        self._index_interface = index_interface

    def execute(self, command: SyncDisclosureIndexCommand) -> SyncDisclosureIndexResult:
        window = DisclosureWindow(command.window_start, command.window_end)
        now = datetime.now(timezone.utc)
        # Membership precheck happens BEFORE the provider profile call: an
        # untracked company must not burn CNINFO quota, and the resolver must
        # not create ledger rows for it (round23).
        self._require_tracked(command)
        try:
            profile = self._profile_loader(command.security_code)
        except DisclosureAnchorError as exc:
            # The profile fetch fails BEFORE any UoW opens; without this trace
            # a first-sync failure left no source_access at all (round8:
            # 300750 was untraceable). Persist the failure, then re-raise.
            failed_access = self._record_failed_profile_access(
                command=command, error=exc, now=now
            )
            raise SyncDisclosureIndexError(
                "CNINFO profile fetch failed; "
                f"source_access_id={failed_access.source_access_id}"
            ) from exc

        with self._uow_factory() as uow:
            profile_access = self._record_profile_access(
                uow=uow,
                command=command,
                profile=profile,
                now=now,
            )
            subject = self._resolve_subject(
                uow=uow,
                command=command,
                profile=profile,
                profile_access=profile_access,
            )
            self._record_cninfo_org_identifier(
                uow=uow,
                company_id=subject.company.company_id,
                provider_org_id=profile.provider_org_id if profile else None,
                source_access_id=profile_access.source_access_id,
                observed_at=now,
            )
            tracked = self._refresh_tracked_company(
                uow=uow,
                command=command,
                company_id=subject.company.company_id,
                security_id=subject.security.security_id,
            )
            try:
                refs = self._source.search_announcements(
                    SourceSecurity(
                        security_code=command.security_code,
                        exchange=command.exchange,
                        security_name=profile.security_name if profile else None,
                    ),
                    window,
                )
            except DisclosureAnchorError as exc:
                failed = self._record_failed_index_access(
                    uow=uow,
                    command=command,
                    company_id=subject.company.company_id,
                    security_id=subject.security.security_id,
                    error=exc,
                    now=now,
                )
                uow.commit()
                raise SyncDisclosureIndexError(
                    f"CNINFO index sync failed; source_access_id={failed.source_access_id}"
                ) from exc

            index_access = self._record_index_access(
                uow=uow,
                command=command,
                refs=refs,
                company_id=subject.company.company_id,
                security_id=subject.security.security_id,
                provider_org_id=profile.provider_org_id if profile else None,
                now=now,
            )
            checkpoint = self._upsert_checkpoint(
                uow=uow,
                company_id=subject.company.company_id,
                window_end=command.window_end,
                window_start=command.window_start,
                now=now,
            )
            # Keep the variable live so mypy sees the tracked-company write as intentional.
            _ = tracked
            uow.commit()

        return SyncDisclosureIndexResult(
            company_id=subject.company.company_id,
            security_id=subject.security.security_id,
            profile_source_access_id=profile_access.source_access_id,
            index_source_access_id=index_access.source_access_id,
            checkpoint_id=checkpoint.source_checkpoint_id,
            candidate_count=len(refs),
            empty=not refs,
        )

    def load_persisted_candidates(self, *, company_id: str) -> list[dict[str, object]]:
        with self._uow_factory() as uow:
            snapshots = uow.source_accesses.list_candidate_snapshots(
                provider=CNINFO_PROVIDER,
                provider_interface=INDEX_INTERFACE,
                company_id=company_id,
            )
        candidates: list[dict[str, object]] = []
        for snapshot in snapshots:
            raw_candidates = snapshot.get("candidates")
            if isinstance(raw_candidates, list):
                candidates.extend(
                    item for item in raw_candidates if isinstance(item, dict)
                )
        return candidates

    def _record_failed_profile_access(
        self,
        *,
        command: SyncDisclosureIndexCommand,
        error: DisclosureAnchorError,
        now: datetime,
    ) -> e.SourceAccess:
        with self._uow_factory() as uow:
            access = add_failed_cninfo_profile_access(
                uow=uow,
                security_code=command.security_code,
                error=error,
                accessed_at=now,
            )
            uow.commit()
        return access

    def _record_profile_access(
        self,
        *,
        uow: UnitOfWork,
        command: SyncDisclosureIndexCommand,
        profile: SourceCompanyProfile | None,
        now: datetime,
    ) -> e.SourceAccess:
        return add_cninfo_profile_access(
            uow=uow,
            security_code=command.security_code,
            profile=profile,
            accessed_at=now,
        )

    def _resolve_subject(
        self,
        *,
        uow: UnitOfWork,
        command: SyncDisclosureIndexCommand,
        profile: SourceCompanyProfile | None,
        profile_access: e.SourceAccess,
    ) -> Any:
        # No profile (e.g. web fallback channel) → no legal-name claim; the
        # resolver treats None as "unknown", never as a conflicting name.
        legal_name = profile.legal_name if profile else None
        return self._subject_resolver.resolve(
            uow,
            SubjectCandidate(
                security_code=command.security_code,
                exchange=command.exchange,
                legal_name=legal_name,
                board=_board_from_exchange(command.exchange),
                credit_code=profile.uscc if profile else None,
                identifier_source_access_id=(
                    profile_access.source_access_id
                    if profile is not None and profile.uscc
                    else None
                ),
                identifier_observed_at=(
                    profile_access.accessed_at
                    if profile is not None and profile.uscc
                    else None
                ),
            ),
        )

    def _record_cninfo_org_identifier(
        self,
        *,
        uow: UnitOfWork,
        company_id: str,
        provider_org_id: str | None,
        source_access_id: str,
        observed_at: datetime,
    ) -> None:
        if not provider_org_id:
            return
        existing = uow.company_identifiers.get_by_scheme_value(
            "cninfo_org_id", provider_org_id
        )
        if existing is not None:
            return
        uow.company_identifiers.add(
            e.CompanyIdentifier(
                identifier_id=ids.new_company_identifier_id(),
                company_id=company_id,
                scheme="cninfo_org_id",
                raw_value=provider_org_id,
                normalized_value=provider_org_id,
                jurisdiction="CN",
                source_access_id=source_access_id,
                status="active",
                observed_at=observed_at,
            )
        )

    def _require_tracked(self, command: SyncDisclosureIndexCommand) -> None:
        with self._uow_factory() as uow:
            security = uow.securities.get_by_code_exchange(
                command.security_code, command.exchange
            )
            if security is None:
                raise CompanyNotTrackedError(command.security_code, command.exchange)
            tracked = uow.tracked_companies.get_by_company_id(security.company_id)
            if tracked is None:
                raise CompanyNotTrackedError(command.security_code, command.exchange)

    def _refresh_tracked_company(
        self,
        *,
        uow: UnitOfWork,
        command: SyncDisclosureIndexCommand,
        company_id: str,
        security_id: str,
    ) -> e.TrackedCompany:
        # Bookkeeping only: keep security_id current. Membership and status
        # are watchlist-managed (make track / admin PUT); sync never creates
        # pool rows, never resurrects paused, never touches the cascade
        # overrides (round21/round23). Re-verified inside this transaction:
        # the precheck ran in an earlier one and untrack may have raced.
        existing = uow.tracked_companies.get_by_company_id(company_id)
        if existing is None:
            raise CompanyNotTrackedError(command.security_code, command.exchange)
        existing.security_id = security_id
        return uow.tracked_companies.update(existing)

    def _record_index_access(
        self,
        *,
        uow: UnitOfWork,
        command: SyncDisclosureIndexCommand,
        refs: Sequence[AnnouncementRef],
        company_id: str,
        security_id: str,
        provider_org_id: str | None,
        now: datetime,
    ) -> e.SourceAccess:
        candidates = [
            _candidate_snapshot(
                ref,
                exchange=command.exchange,
                provider_org_id=provider_org_id,
            )
            for ref in refs
        ]
        snapshot: dict[str, object] = {
            "result": "empty" if not candidates else "ok",
            "candidates": candidates,
        }
        if candidates:
            snapshot["raw_records"] = [dict(ref.raw_record) for ref in refs]
        return uow.source_accesses.add(
            e.SourceAccess(
                source_access_id=ids.new_source_access_id(),
                provider=CNINFO_PROVIDER,
                provider_interface=self._index_interface,
                dataset_key="p_info3015",
                query_params=_index_query_params(command),
                accessed_at=now,
                status="ok",
                result_hash=_canonical_sha256(snapshot),
                result_snapshot=snapshot,
                company_id=company_id,
                security_id=security_id,
            )
        )

    def _record_failed_index_access(
        self,
        *,
        uow: UnitOfWork,
        command: SyncDisclosureIndexCommand,
        company_id: str,
        security_id: str,
        error: Exception,
        now: datetime,
    ) -> e.SourceAccess:
        return uow.source_accesses.add(
            e.SourceAccess(
                source_access_id=ids.new_source_access_id(),
                provider=CNINFO_PROVIDER,
                provider_interface=self._index_interface,
                dataset_key="p_info3015",
                query_params=_index_query_params(command),
                accessed_at=now,
                status="failed",
                error=_json(
                    error.to_error(stage="index")
                    if isinstance(error, SourceRequestError)
                    else {
                        "stage": "index",
                        "error_code": type(error).__name__,
                        "retryable": False,
                        "provider_document_id": None,
                    }
                ),
                result_snapshot={"result": "failed"},
                company_id=company_id,
                security_id=security_id,
            )
        )

    def _upsert_checkpoint(
        self,
        *,
        uow: UnitOfWork,
        company_id: str,
        window_end: date,
        window_start: date | None = None,
        now: datetime,
    ) -> e.SourceCheckpoint:
        scope_key = f"{company_id}:p_info3015"
        existing = uow.source_checkpoints.get_by_scope(CNINFO_PROVIDER, scope_key)
        cursor_end = window_end
        if existing is not None and existing.cursor:
            prior = existing.cursor.get("window_end")
            if isinstance(prior, str) and prior:
                prior_end = date.fromisoformat(prior)
                # Monotonic cursor: a historical backfill (explicit
                # from/to with end < today, round23) must not drag
                # window_end backwards — the next worker round would
                # re-sync everything since that date. The daily
                # increment anchor only ever moves forward.
                cursor_end = max(prior_end, window_end)
        cursor = {
            "window_end": cursor_end.isoformat(),
            # Audit fields (design/watchlist-operations.md §5.4): what window
            # this sync actually covered and when. Readers only use window_end.
            "window_start": window_start.isoformat() if window_start else None,
            "synced_at": now.isoformat(),
        }
        if existing is None:
            return uow.source_checkpoints.add(
                e.SourceCheckpoint(
                    source_checkpoint_id=ids.new_source_checkpoint_id(),
                    provider=CNINFO_PROVIDER,
                    scope_key=scope_key,
                    cursor=cursor,
                    updated_at=now,
                )
            )
        existing.cursor = cursor
        existing.updated_at = now
        return uow.source_checkpoints.update(existing)


def _candidate_snapshot(
    ref: AnnouncementRef,
    *,
    exchange: str,
    provider_org_id: str | None,
) -> dict[str, object]:
    index_updated_at = (
        ref.index_updated_at.isoformat() if ref.index_updated_at is not None else None
    )
    return {
        "provider_document_id": ref.provider_document_id,
        "title": ref.title,
        "download_url": ref.download_url,
        "raw_category": ref.raw_category,
        "category_names": ref.category_names,
        "filing_type": ref.filing_type or "other",
        "report_period": ref.report_period,
        "announcement_date": ref.announcement_date.isoformat(),
        "security_code": ref.security_code,
        "exchange": exchange,
        "security_name": ref.security_name,
        "provider_org_id": provider_org_id,
        "object_id": ref.object_id,
        "rec_id": ref.rec_id,
        "file_signature_hint": {
            "file_size": ref.file_size,
            "etag": None,
            "last_modified": None,
            "index_updated_at": index_updated_at,
        },
    }


def _index_query_params(command: SyncDisclosureIndexCommand) -> dict[str, object]:
    params: dict[str, object] = {
        "scode": command.security_code,
        "sdate": command.window_start.isoformat(),
        "edate": command.window_end.isoformat(),
    }
    return params


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _board_from_exchange(exchange: str) -> str | None:
    if exchange in {"SZSE", "SSE"}:
        return exchange
    return None
