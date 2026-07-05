"""Minimal FastAPI application."""

from __future__ import annotations

from disclosure_anchor.api.routers.health import router as health_router
from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
    reader_database_url,
)
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
    app_engine = None
    reader_engine = None
    if validate_runtime:
        if resolved_settings.database_url is not None:
            app_engine = create_db_engine(app_database_url(resolved_settings))
        report = run_startup_preflight(resolved_settings, engine=app_engine)
        if not report.ok:
            if app_engine is not None:
                app_engine.dispose()
            raise ConfigurationError(
                "runtime preflight failed:\n" + render_report(report.results)
            )
    elif resolved_settings.database_url is not None:
        app_engine = create_db_engine(app_database_url(resolved_settings))

    if resolved_settings.disclosure_reader_database_url is not None:
        reader_url = reader_database_url(resolved_settings)
        if app_engine is not None and reader_url == app_database_url(resolved_settings):
            reader_engine = app_engine
        else:
            reader_engine = create_db_engine(reader_url)
    else:
        reader_engine = app_engine

    app = FastAPI(title="disclosure_anchor", version="0.1.0")
    app.state.settings = resolved_settings
    if app_engine is not None:
        app.state.app_db_engine = app_engine
        app.state.db_engine = app_engine
        app.state.uow_factory = unit_of_work_factory(app_engine)
    if reader_engine is not None:
        app.state.reader_db_engine = reader_engine
    if health_router is not None:
        app.include_router(health_router)
    return app
