"""Synchronize disclosure index candidates from a provider source."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from typing import Any

from disclosure_anchor.adapters.sources.cninfo.mapper import (
    CNINFO_PROVIDER,
    CninfoCompanyProfile,
    map_filing_type,
)
from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureSourcePort,
    DisclosureWindow,
    SourceSecurity,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.subject_resolver import (
    SubjectCandidate,
    SubjectResolver,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import DisclosureAnchorError


PROFILE_INTERFACE = "cninfo:p_stock2100"
INDEX_INTERFACE = "cninfo:p_info3015"


class SyncDisclosureIndexError(DisclosureAnchorError):
    """Raised when index sync fails after durable failure state is recorded."""


@dataclass(frozen=True)
class SyncDisclosureIndexCommand:
    security_code: str
    exchange: str
    window_start: date
    window_end: date
    categories: tuple[str, ...] | None = None
    category_names_by_code: Mapping[str, str] | None = None


@dataclass(frozen=True)
class SyncDisclosureIndexResult:
    company_id: str
    security_id: str
    profile_source_access_id: str
    index_source_access_id: str
    checkpoint_id: str
    candidate_count: int
    empty: bool


class SyncDisclosureIndex:
    """Persist CNINFO announcement candidates and advance checkpoint safely."""

    def __init__(
        self,
        *,
        source: DisclosureSourcePort,
        profile_loader: Callable[[str], CninfoCompanyProfile | None],
        uow_factory: Callable[[], UnitOfWork],
        subject_resolver: SubjectResolver | None = None,
    ) -> None:
        self._source = source
        self._profile_loader = profile_loader
        self._uow_factory = uow_factory
        self._subject_resolver = subject_resolver or SubjectResolver()

    def execute(self, command: SyncDisclosureIndexCommand) -> SyncDisclosureIndexResult:
        window = DisclosureWindow(command.window_start, command.window_end)
        profile = self._profile_loader(command.security_code)
        now = datetime.now(timezone.utc)

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
            )
            self._record_cninfo_org_identifier(
                uow=uow,
                company_id=subject.company.company_id,
                provider_org_id=profile.provider_org_id if profile else None,
                source_access_id=profile_access.source_access_id,
                observed_at=now,
            )
            tracked = self._upsert_tracked_company(
                uow=uow,
                company_id=subject.company.company_id,
                security_id=subject.security.security_id,
                categories=command.categories,
            )
            try:
                refs = self._source.search_announcements(
                    SourceSecurity(
                        security_code=command.security_code,
                        exchange=command.exchange,
                        security_name=profile.security_name if profile else None,
                    ),
                    window,
                    command.categories,
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
                category_names_by_code=command.category_names_by_code or {},
                now=now,
            )
            checkpoint = self._upsert_checkpoint(
                uow=uow,
                company_id=subject.company.company_id,
                window_end=command.window_end,
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

    def _record_profile_access(
        self,
        *,
        uow: UnitOfWork,
        command: SyncDisclosureIndexCommand,
        profile: CninfoCompanyProfile | None,
        now: datetime,
    ) -> e.SourceAccess:
        snapshot: dict[str, object]
        status: str
        error: str | None = None
        if profile is None:
            status = "warning"
            snapshot = {"warning": "p_stock2100 profile unavailable"}
            error = _json({"stage": "index", "error_code": "profile_unavailable", "retryable": True})
        else:
            status = "ok"
            snapshot = {"profile": asdict(profile)}
        return uow.source_accesses.add(
            e.SourceAccess(
                source_access_id=ids.new_source_access_id(),
                provider=CNINFO_PROVIDER,
                provider_interface=PROFILE_INTERFACE,
                dataset_key="p_stock2100",
                query_params={"scode": command.security_code},
                accessed_at=now,
                status=status,
                result_hash=_canonical_sha256(snapshot),
                error=error,
                result_snapshot=snapshot,
            )
        )

    def _resolve_subject(
        self,
        *,
        uow: UnitOfWork,
        command: SyncDisclosureIndexCommand,
        profile: CninfoCompanyProfile | None,
    ) -> Any:
        legal_name = profile.legal_name if profile else f"CNINFO {command.security_code}"
        return self._subject_resolver.resolve(
            uow,
            SubjectCandidate(
                security_code=command.security_code,
                exchange=command.exchange,
                legal_name=legal_name,
                board=_board_from_exchange(command.exchange),
                credit_code=profile.uscc if profile else None,
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

    def _upsert_tracked_company(
        self,
        *,
        uow: UnitOfWork,
        company_id: str,
        security_id: str,
        categories: tuple[str, ...] | None,
    ) -> e.TrackedCompany:
        existing = uow.tracked_companies.get_by_company_id(company_id)
        filing_categories = list(categories) if categories is not None else None
        if existing is None:
            return uow.tracked_companies.add(
                e.TrackedCompany(
                    tracked_company_id=ids.new_tracked_company_id(),
                    company_id=company_id,
                    security_id=security_id,
                    status="active",
                    filing_categories=filing_categories,
                )
            )
        existing.security_id = security_id
        existing.status = "active"
        if filing_categories is not None:
            existing.filing_categories = filing_categories
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
        category_names_by_code: Mapping[str, str],
        now: datetime,
    ) -> e.SourceAccess:
        candidates = [
            _candidate_snapshot(
                ref,
                exchange=command.exchange,
                provider_org_id=provider_org_id,
                category_names_by_code=category_names_by_code,
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
                provider_interface=INDEX_INTERFACE,
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
                provider_interface=INDEX_INTERFACE,
                dataset_key="p_info3015",
                query_params=_index_query_params(command),
                accessed_at=now,
                status="failed",
                error=_json(
                    {
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
        self, *, uow: UnitOfWork, company_id: str, window_end: date
    ) -> e.SourceCheckpoint:
        scope_key = f"{company_id}:p_info3015"
        existing = uow.source_checkpoints.get_by_scope(CNINFO_PROVIDER, scope_key)
        cursor = {"window_end": window_end.isoformat()}
        if existing is None:
            return uow.source_checkpoints.add(
                e.SourceCheckpoint(
                    source_checkpoint_id=ids.new_source_checkpoint_id(),
                    provider=CNINFO_PROVIDER,
                    scope_key=scope_key,
                    cursor=cursor,
                )
            )
        existing.cursor = cursor
        return uow.source_checkpoints.update(existing)


def _candidate_snapshot(
    ref: AnnouncementRef,
    *,
    exchange: str,
    provider_org_id: str | None,
    category_names_by_code: Mapping[str, str],
) -> dict[str, object]:
    index_updated_at = (
        ref.index_updated_at.isoformat() if ref.index_updated_at is not None else None
    )
    return {
        "provider_document_id": ref.provider_document_id,
        "title": ref.title,
        "download_url": ref.download_url,
        "raw_category": ref.raw_category,
        "filing_type": map_filing_type(
            ref.raw_category, category_names_by_code=category_names_by_code
        ),
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
    if command.categories is not None:
        params["categories"] = list(command.categories)
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
