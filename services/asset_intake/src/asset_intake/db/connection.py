"""SQLAlchemy engine construction. Only this module turns URLs into engines."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


class ConfigurationError(RuntimeError):
    """A required configuration value is missing."""


def require_url(value: Optional[str], *, name: str) -> str:
    if not value:
        raise ConfigurationError(
            f"{name} is not configured; set it in the environment before using the database"
        )
    return value


def create_db_engine(
    url: str, *, autocommit: bool = False, set_role: Optional[str] = None
) -> Engine:
    if autocommit:
        engine = create_engine(url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    else:
        engine = create_engine(url, pool_pre_ping=True)

    if set_role is not None:
        quoted_role = '"' + set_role.replace('"', '""') + '"'

        @event.listens_for(engine, "connect")
        def _set_role(dbapi_connection: Any, connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"SET ROLE {quoted_role}")
            finally:
                cursor.close()

    return engine
