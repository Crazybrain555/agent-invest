"""SQLAlchemy engine and session construction for the PostgreSQL adapter.

Only this module knows how to turn a configured database URL into an engine.
Business code receives a session/UnitOfWork, never a raw connection string.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from disclosure_anchor.domain.errors import ConfigurationError
from disclosure_anchor.settings import Settings


def _require_url(value: Optional[str], *, name: str) -> str:
    if not value:
        raise ConfigurationError(
            f"{name} is not configured; set it in the environment before using the database"
        )
    return value


def app_database_url(settings: Settings) -> str:
    url = settings.database_url.get_secret_value() if settings.database_url else None
    return _require_url(url, name="DATABASE_URL")


def reader_database_url(settings: Settings) -> str:
    secret = settings.disclosure_reader_database_url or settings.database_url
    url = secret.get_secret_value() if secret else None
    return _require_url(url, name="DISCLOSURE_READER_DATABASE_URL or DATABASE_URL")


def uses_reader_database_url_fallback(settings: Settings) -> bool:
    return settings.disclosure_reader_database_url is None and settings.database_url is not None


def admin_database_url(settings: Settings) -> str:
    secret = settings.disclosure_admin_database_url
    url = secret.get_secret_value() if secret else None
    return _require_url(url, name="DISCLOSURE_ADMIN_DATABASE_URL")


def migration_database_url(settings: Settings) -> str:
    secret = settings.disclosure_migration_database_url
    url = secret.get_secret_value() if secret else None
    return _require_url(url, name="DISCLOSURE_MIGRATION_DATABASE_URL")


def create_db_engine(
    url: str,
    *,
    set_role: Optional[str] = None,
    echo: bool = False,
    autocommit: bool = False,
) -> Engine:
    """Create an engine. When ``set_role`` is given, every new connection runs
    ``SET ROLE`` so objects are created/owned by that role rather than the
    connecting login (used for migrations and owner-scoped writes). Use
    ``autocommit`` for bootstrap statements such as ``CREATE DATABASE``."""

    kwargs: dict[str, object] = {"echo": echo, "future": True, "pool_pre_ping": True}
    if autocommit:
        kwargs["isolation_level"] = "AUTOCOMMIT"
    engine = create_engine(url, **kwargs)

    if set_role:
        # Validated against the known role allowlist by callers; quote defensively.
        safe_role = '"' + set_role.replace('"', '""') + '"'

        @event.listens_for(engine, "connect")
        def _apply_set_role(dbapi_connection: object, _connection_record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute(f"SET ROLE {safe_role}")
            finally:
                cursor.close()

    return engine
