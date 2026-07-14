"""Tracking-pool read endpoint (round22: the DB is the pool's source of truth).

Rows come from disclosure_public.tracked_companies_v1 (raw override columns,
NULL = inherit); the cascade resolves HERE because the global processing
policy lives in config/processing_policy.json, which SQL cannot see. The
effective_* fields are API-derived (DERIVED whitelist, like asset_uri).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.schema import PUBLIC_SCHEMA
from disclosure_anchor.adapters.sources.cninfo.mapper import load_processing_policy
from disclosure_anchor.api.db import reader_engine_from_request
from disclosure_anchor.api.errors import not_found, validation_error
from disclosure_anchor.api.pagination import (
    DEFAULT_LIMIT,
    TrackedCompanyCursor,
    decode_tracked_company_cursor,
    encode_tracked_company_cursor,
    tracked_company_cursor_from_row,
    validate_limit,
)
from disclosure_anchor.api.schemas.public import (
    TrackedCompanyListResponse,
    TrackedCompanyV1,
)
from disclosure_anchor.application.worker.queries import SYNC_FREQUENCY_SECONDS

try:
    from fastapi import APIRouter, Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    APIRouter = None  # type: ignore[assignment, misc]
    Request = None  # type: ignore[assignment, misc]

TRACKED_STATUSES = ("active", "paused")

TRACKED_COLUMNS = (
    "tracked_company_id",
    "company_ref",
    "security_ref",
    "security_code",
    "exchange",
    "legal_name",
    "legal_name_status",
    "status",
    "lookback_days",
    "sync_frequency",
    "process_classes",
    "last_synced_at",
    "synced_through",
    "created_at",
    "updated_at",
    "contract_version",
)


def list_tracked_companies(
    request: Request,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> TrackedCompanyListResponse:
    if status is not None and status not in TRACKED_STATUSES:
        raise validation_error(
            "status", f"expected one of {list(TRACKED_STATUSES)}"
        )
    page_size = validate_limit(limit)
    decoded = decode_tracked_company_cursor(cursor)
    context = _CascadeContext.from_request(request)
    where: list[str] = []
    params: dict[str, Any] = {}
    if status is not None:
        where.append("status = :status")
        params["status"] = status
    _append_tracked_cursor(where=where, params=params, cursor=decoded)
    params["limit_plus_one"] = page_size + 1
    sql = (
        f"SELECT {', '.join(TRACKED_COLUMNS)} "
        f"FROM {PUBLIC_SCHEMA}.tracked_companies_v1 "
        f"{_where_sql(where)} "
        "ORDER BY security_code ASC NULLS LAST, tracked_company_id ASC "
        "LIMIT :limit_plus_one"
    )
    engine = reader_engine_from_request(request)
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(text(sql), params).mappings()]
    page_rows = rows[:page_size]
    next_cursor = None
    if len(rows) > page_size:
        next_cursor = encode_tracked_company_cursor(
            tracked_company_cursor_from_row(page_rows[-1])
        )
    return TrackedCompanyListResponse(
        items=[context.resolve(row) for row in page_rows],
        next_cursor=next_cursor,
    )


def get_tracked_company(
    security_code: str,
    request: Request,
    exchange: str,
) -> TrackedCompanyV1:
    context = _CascadeContext.from_request(request)
    sql = (
        f"SELECT {', '.join(TRACKED_COLUMNS)} "
        f"FROM {PUBLIC_SCHEMA}.tracked_companies_v1 "
        "WHERE security_code = :security_code AND exchange = :exchange"
    )
    engine = reader_engine_from_request(request)
    with engine.connect() as conn:
        row = conn.execute(
            text(sql),
            {"security_code": security_code, "exchange": exchange},
        ).mappings().one_or_none()
    if row is None:
        not_found(f"company is not tracked: {security_code}.{exchange}")
    return context.resolve(dict(row))


def _append_tracked_cursor(
    *,
    where: list[str],
    params: dict[str, Any],
    cursor: TrackedCompanyCursor | None,
) -> None:
    if cursor is None:
        return
    params["cursor_tracked_company_id"] = cursor.tracked_company_id
    if cursor.security_code is None:
        # Already in the NULL security_code tail — advance on the id alone.
        where.append(
            "security_code IS NULL "
            "AND tracked_company_id > :cursor_tracked_company_id"
        )
        return
    params["cursor_security_code"] = cursor.security_code
    where.append(
        "("
        "security_code IS NULL OR "
        "security_code > :cursor_security_code OR "
        "(security_code = :cursor_security_code "
        "AND tracked_company_id > :cursor_tracked_company_id)"
        ")"
    )


def _where_sql(where: list[str]) -> str:
    return "WHERE " + " AND ".join(where) if where else ""


class _CascadeContext:
    """The config-cascade inputs shared by the list and single endpoints."""

    def __init__(
        self,
        *,
        global_classes: list[str],
        default_lookback_days: int,
        default_sync_seconds: int,
    ) -> None:
        self._global_classes = global_classes
        self._default_lookback_days = default_lookback_days
        self._default_sync_seconds = default_sync_seconds

    @classmethod
    def from_request(cls, request: Request) -> "_CascadeContext":
        settings = request.app.state.settings
        return cls(
            global_classes=list(
                load_processing_policy(settings.disclosure_processing_policy_path)
            ),
            default_lookback_days=settings.disclosure_initial_lookback_days,
            default_sync_seconds=settings.disclosure_sync_interval_seconds,
        )

    def resolve(self, row: dict[str, Any]) -> TrackedCompanyV1:
        return _tracked_company(
            row,
            global_classes=self._global_classes,
            default_lookback_days=self._default_lookback_days,
            default_sync_seconds=self._default_sync_seconds,
        )


def _tracked_company(
    row: dict[str, Any],
    *,
    global_classes: list[str],
    default_lookback_days: int,
    default_sync_seconds: int,
    now: datetime | None = None,
) -> TrackedCompanyV1:
    lookback_days = row["lookback_days"]
    process_classes = row["process_classes"]
    effective_sync_seconds = SYNC_FREQUENCY_SECONDS.get(
        row["sync_frequency"] or "", default_sync_seconds
    )
    last_synced_at = row["last_synced_at"]
    if last_synced_at is None:
        sync_state = "never_synced"
    else:
        age = ((now or datetime.now(timezone.utc)) - last_synced_at).total_seconds()
        sync_state = "due" if age >= effective_sync_seconds else "fresh"
    return TrackedCompanyV1(
        **row,
        effective_lookback_days=(
            lookback_days if lookback_days is not None else default_lookback_days
        ),
        effective_sync_seconds=effective_sync_seconds,
        effective_process_classes=(
            list(process_classes) if process_classes else global_classes
        ),
        sync_state=sync_state,
    )


router: Any
if APIRouter is not None:
    router = APIRouter()
    router.add_api_route(
        "/v1/tracked-companies",
        list_tracked_companies,
        methods=["GET"],
        response_model=TrackedCompanyListResponse,
    )
    router.add_api_route(
        "/v1/tracked-companies/{security_code}",
        get_tracked_company,
        methods=["GET"],
        response_model=TrackedCompanyV1,
    )
else:
    router = None
