"""Health endpoint."""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.engine import Engine

from disclosure_anchor import __version__
from disclosure_anchor.api.schemas.health import HealthResponse, QueueStatus
from disclosure_anchor.adapters.db.postgres.schema import (
    ALEMBIC_VERSION_TABLE,
    ALEMBIC_VERSION_TABLE_SCHEMA,
)
from disclosure_anchor.application.worker import queries
from disclosure_anchor.settings import Settings


try:
    from fastapi import APIRouter, Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    APIRouter = None  # type: ignore[assignment, misc]
    Request = None  # type: ignore[assignment, misc]


def health_payload(
    *,
    settings: Settings | None,
    engine: Engine | None,
    ops_engine: Engine | None = None,
) -> HealthResponse:
    data_root_mounted = True if settings is None else settings.sentinel_path.exists()
    migration_head = _migration_head(engine) if data_root_mounted else None
    queues = _queue_status(settings, ops_engine)
    queue_degraded = settings is not None and ops_engine is not None and (
        queues is None
        or queues.retrying_builds > 0
        or queues.build_dead_letters > 0
        or queues.active_degraded_builds > 0
    )
    status: Literal["ok", "degraded"] = (
        "ok"
        if data_root_mounted and migration_head is not None and not queue_degraded
        else "degraded"
    )
    return HealthResponse(
        status=status,
        service="disclosure_anchor",
        version=__version__,
        migration_head=migration_head,
        data_root_mounted=data_root_mounted,
        queues=queues,
    )


def _queue_status(
    settings: Settings | None, ops_engine: Engine | None
) -> QueueStatus | None:
    """One-command answer to 现在健康吗 (round23 queue-status surface).

    Reads the ops.*_v1 queue views through the app engine (they are granted
    to the app role only; SELECT usage does not depend on its write
    permission). Degrades to None instead of failing health — same stance
    as migration_head.
    """

    if settings is None or ops_engine is None:
        return None
    try:
        scope_classes = _scope_classes(settings)
        max_retries = settings.cninfo_max_retries
        with ops_engine.connect() as conn:
            return QueueStatus(
                pending_download=queries.pending_download_count(
                    conn, max_retries=max_retries, scope_classes=scope_classes
                ),
                pending_parse=queries.pending_parse_backlog_count(
                    conn, scope_classes=scope_classes
                ),
                pending_build=queries.pending_build_count(
                    conn, max_retries=settings.disclosure_max_build_retries
                ),
                pending_publish=queries.pending_publish_count(conn),
                download_dead_letters=queries.download_dead_letter_count(
                    conn, max_retries=max_retries
                ),
                parse_dead_letters=queries.parse_dead_letter_count(
                    conn, max_retries=settings.disclosure_max_parse_retries
                ),
                build_dead_letters=queries.build_dead_letter_count(
                    conn, max_retries=settings.disclosure_max_build_retries
                ),
                retrying_documents=queries.retrying_document_count(
                    conn,
                    max_retries=settings.disclosure_max_parse_retries,
                ),
                retrying_builds=queries.retrying_build_count(
                    conn, max_retries=settings.disclosure_max_build_retries
                ),
                degraded_builds=queries.degraded_build_count(conn),
                active_degraded_builds=queries.degraded_build_count(
                    conn, active_only=True
                ),
                sync_due=len(
                    queries.sync_due(
                        conn,
                        interval_seconds=settings.disclosure_sync_interval_seconds,
                        limit=100000,
                    )
                ),
                backfill_pending=queries.pending_processing_backlog_count(
                    conn,
                    max_retries=max_retries,
                    scope_classes=scope_classes,
                ),
                last_outbox_event_at=queries.last_outbox_event_at(conn),
            )
    except Exception:
        return None


def _scope_classes(settings: Settings) -> tuple[str, ...] | None:
    try:
        from disclosure_anchor.adapters.sources.cninfo.mapper import (
            load_processing_policy,
        )

        return load_processing_policy(settings.disclosure_processing_policy_path)
    except Exception:
        return None


def _migration_head(engine: Engine | None) -> str | None:
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            value = conn.execute(
                text(
                    f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE_SCHEMA}."
                    f"{ALEMBIC_VERSION_TABLE}"
                )
            ).scalar_one_or_none()
    except Exception:
        return None
    return str(value) if value is not None else None


def get_health(request: Request) -> HealthResponse:
    settings = getattr(request.app.state, "settings", None)
    engine = getattr(request.app.state, "reader_db_engine", None)
    ops_engine = getattr(request.app.state, "app_db_engine", None)
    return health_payload(settings=settings, engine=engine, ops_engine=ops_engine)


router: Any
if APIRouter is not None:
    router = APIRouter()
    router.add_api_route("/v1/health", get_health, methods=["GET"], response_model=HealthResponse)
else:
    router = None
