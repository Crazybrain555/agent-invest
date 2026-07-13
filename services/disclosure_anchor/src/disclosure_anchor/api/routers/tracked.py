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
from disclosure_anchor.api.errors import validation_error
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


def list_tracked_companies(
    request: Request,
    status: str | None = None,
) -> TrackedCompanyListResponse:
    if status is not None and status not in TRACKED_STATUSES:
        raise validation_error(
            "status", f"expected one of {list(TRACKED_STATUSES)}"
        )
    settings = request.app.state.settings
    global_classes = list(
        load_processing_policy(settings.disclosure_processing_policy_path)
    )
    where = "WHERE status = :status" if status is not None else ""
    sql = (
        "SELECT tracked_company_id, company_ref, security_ref, security_code, "
        "exchange, legal_name, legal_name_status, status, lookback_days, "
        "sync_frequency, process_classes, last_synced_at, synced_through, "
        "created_at, updated_at, contract_version "
        f"FROM {PUBLIC_SCHEMA}.tracked_companies_v1 {where} "
        "ORDER BY security_code NULLS LAST, tracked_company_id"
    )
    engine = reader_engine_from_request(request)
    with engine.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                text(sql), {"status": status} if status is not None else {}
            ).mappings()
        ]
    return TrackedCompanyListResponse(
        items=[
            _tracked_company(
                row,
                global_classes=global_classes,
                default_lookback_days=settings.disclosure_initial_lookback_days,
                default_sync_seconds=settings.disclosure_sync_interval_seconds,
            )
            for row in rows
        ]
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
else:
    router = None
