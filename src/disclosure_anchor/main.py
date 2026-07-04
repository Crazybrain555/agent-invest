"""Minimal FastAPI application."""

from __future__ import annotations

from disclosure_anchor.api.routers.health import router as health_router
from disclosure_anchor.adapters.db.postgres.connection import app_database_url, create_db_engine
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.runtime.doctor import (
    render_report,
    run_startup_preflight,
)
from disclosure_anchor.domain.errors import ConfigurationError, MissingDependencyError
from disclosure_anchor.settings import Settings, load_settings

try:
    from fastapi import FastAPI
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local environment
    raise MissingDependencyError(
        "fastapi is not installed; install project dependencies before starting the API"
    ) from exc


def create_app(settings: Settings | None = None, *, validate_runtime: bool = True) -> FastAPI:
    resolved_settings = settings or load_settings()
    engine = None
    if validate_runtime:
        if resolved_settings.database_url is not None:
            engine = create_db_engine(app_database_url(resolved_settings))
        report = run_startup_preflight(resolved_settings, engine=engine)
        if not report.ok:
            if engine is not None:
                engine.dispose()
            raise ConfigurationError(
                "runtime preflight failed:\n" + render_report(report.results)
            )
    elif resolved_settings.database_url is not None:
        engine = create_db_engine(app_database_url(resolved_settings))

    app = FastAPI(title="disclosure_anchor", version="0.1.0")
    if engine is not None:
        app.state.db_engine = engine
        app.state.uow_factory = unit_of_work_factory(engine)
    if health_router is not None:
        app.include_router(health_router)
    return app
