"""Read-only PostgreSQL catalog helpers shared by doctor and tests."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection


def view_names(conn: Connection, *, schema: str) -> tuple[str, ...]:
    rows = conn.execute(
        text(
            "SELECT table_name FROM information_schema.views "
            "WHERE table_schema = :schema ORDER BY table_name"
        ),
        {"schema": schema},
    ).scalars()
    return tuple(str(row) for row in rows)
