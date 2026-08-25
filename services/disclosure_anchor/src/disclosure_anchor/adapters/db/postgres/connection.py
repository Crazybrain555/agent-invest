"""SQLAlchemy engine and session construction for the PostgreSQL adapter.

Only this module knows how to turn a configured database URL into an engine.
Business code receives a session/UnitOfWork, never a raw connection string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    DATABASE_NAME,
    READER_ROLE,
)
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
    secret = settings.disclosure_reader_database_url
    url = secret.get_secret_value() if secret else None
    return _require_url(url, name="DISCLOSURE_READER_DATABASE_URL")


def admin_database_url(settings: Settings) -> str:
    secret = settings.disclosure_admin_database_url
    url = secret.get_secret_value() if secret else None
    return _require_url(url, name="DISCLOSURE_ADMIN_DATABASE_URL")


def migration_database_url(settings: Settings) -> str:
    secret = settings.disclosure_migration_database_url
    url = secret.get_secret_value() if secret else None
    return _require_url(url, name="DISCLOSURE_MIGRATION_DATABASE_URL")


@dataclass(frozen=True)
class RuntimeDatabaseIdentity:
    """Authenticated and effective PostgreSQL identities for a runtime session."""

    database_name: str
    session_role: str
    current_role: str
    session_superuser: bool
    current_superuser: bool


def inspect_runtime_database_identity(
    connection: Connection,
) -> RuntimeDatabaseIdentity:
    """Read the complete identity needed for the runtime least-privilege gate.

    ``current_user`` alone is insufficient: a superuser login can start a
    connection under ``SET ROLE disclosure_app`` while ``session_user`` retains
    the authority to reset that role. Missing role catalog rows fail closed by
    being treated as superusers.
    """

    row = connection.execute(
        text(
            """
            SELECT current_database() AS database_name,
                   session_user AS session_role,
                   current_user AS current_role,
                   COALESCE(
                       (SELECT rolsuper FROM pg_roles WHERE rolname = session_user),
                       true
                   ) AS session_superuser,
                   COALESCE(
                       (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
                       true
                   ) AS current_superuser
            """
        )
    ).mappings().one()
    return RuntimeDatabaseIdentity(
        database_name=str(row["database_name"]),
        session_role=str(row["session_role"]),
        current_role=str(row["current_role"]),
        session_superuser=bool(row["session_superuser"]),
        current_superuser=bool(row["current_superuser"]),
    )


def require_runtime_app_connection(
    connection: Connection,
) -> RuntimeDatabaseIdentity:
    """Fail unless this connection is the exact non-superuser app identity."""

    identity = inspect_runtime_database_identity(connection)
    failures: list[str] = []
    if identity.database_name != DATABASE_NAME:
        failures.append(
            f"database must be {DATABASE_NAME}, got {identity.database_name}"
        )
    if identity.session_role != APP_ROLE or identity.current_role != APP_ROLE:
        failures.append(
            f"session_user/current_user must both be {APP_ROLE}, got "
            f"{identity.session_role}/{identity.current_role}"
        )
    if identity.session_superuser or identity.current_superuser:
        failures.append("runtime database session must not be superuser")
    if failures:
        raise ConfigurationError(
            "runtime database identity check failed: " + "; ".join(failures)
        )
    return identity


def require_runtime_app_engine(engine: Engine) -> RuntimeDatabaseIdentity:
    """Apply :func:`require_runtime_app_connection` to a new engine checkout."""

    with engine.connect() as connection:
        return require_runtime_app_connection(connection)


def require_runtime_reader_connection(
    connection: Connection,
) -> RuntimeDatabaseIdentity:
    """Fail unless this connection is the exact non-superuser reader identity."""

    identity = inspect_runtime_database_identity(connection)
    failures: list[str] = []
    if identity.database_name != DATABASE_NAME:
        failures.append(
            f"database must be {DATABASE_NAME}, got {identity.database_name}"
        )
    if identity.session_role != READER_ROLE or identity.current_role != READER_ROLE:
        failures.append(
            f"session_user/current_user must both be {READER_ROLE}, got "
            f"{identity.session_role}/{identity.current_role}"
        )
    if identity.session_superuser or identity.current_superuser:
        failures.append("reader database session must not be superuser")
    if failures:
        raise ConfigurationError(
            "reader database identity check failed: " + "; ".join(failures)
        )
    return identity


def require_runtime_reader_engine(engine: Engine) -> RuntimeDatabaseIdentity:
    """Apply the strict reader identity gate to a new engine checkout."""

    with engine.connect() as connection:
        return require_runtime_reader_connection(connection)


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
    ``autocommit`` for cluster-level bootstrap statements."""

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
