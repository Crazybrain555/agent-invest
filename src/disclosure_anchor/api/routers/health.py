"""Health endpoint."""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.engine import Engine

from disclosure_anchor import __version__
from disclosure_anchor.api.schemas.health import HealthResponse
from disclosure_anchor.adapters.db.postgres.schema import (
    ALEMBIC_VERSION_TABLE,
    ALEMBIC_VERSION_TABLE_SCHEMA,
)
from disclosure_anchor.settings import Settings


try:
    from fastapi import APIRouter, Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    APIRouter = None  # type: ignore[assignment, misc]
    Request = None  # type: ignore[assignment, misc]


def health_payload(*, settings: Settings | None, engine: Engine | None) -> HealthResponse:
    data_root_mounted = True if settings is None else settings.sentinel_path.exists()
    migration_head = _migration_head(engine) if data_root_mounted else None
    status: Literal["ok", "degraded"] = (
        "ok" if data_root_mounted and migration_head is not None else "degraded"
    )
    return HealthResponse(
        status=status,
        service="disclosure_anchor",
        version=__version__,
        migration_head=migration_head,
        data_root_mounted=data_root_mounted,
    )


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
    return health_payload(settings=settings, engine=engine)


router: Any
if APIRouter is not None:
    router = APIRouter()
    router.add_api_route("/v1/health", get_health, methods=["GET"], response_model=HealthResponse)
else:
    router = None
