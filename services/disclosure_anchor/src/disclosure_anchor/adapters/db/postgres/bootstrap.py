"""Idempotent database/role/schema bootstrap.

This runs *before* migrations. It creates the cluster-level roles, the service
database (owned by ``disclosure_owner``) and the three schemas with their
schema-level USAGE grants. Table/view grants are applied by the migration once
the objects exist.

All statements are safe to re-run. ``CREATE DATABASE`` cannot run inside a
transaction, so the admin engine uses AUTOCOMMIT.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    ALL_ROLES,
    CORE_SCHEMA,
    DATABASE_NAME,
    OPS_SCHEMA,
    OWNER_ROLE,
    PUBLIC_SCHEMA,
    READ_ONLY_PUBLIC_ROLES,
)


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ensure_roles(admin_engine: Engine) -> None:
    """Create the four service roles as NOLOGIN groups if they do not exist.

    LOGIN capability and passwords are an out-of-band operational concern and are
    never created or committed here; tests exercise privileges via ``SET ROLE``.
    Role names come from the trusted ``schema`` constants, so they are quoted and
    interpolated rather than bound (bound params cannot reach inside a DO block).
    """

    with admin_engine.connect() as conn:
        for role in ALL_ROLES:
            conn.execute(
                text(
                    f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_roles WHERE rolname = {_quote_literal(role)}
                        ) THEN
                            CREATE ROLE {_quote_ident(role)} NOLOGIN;
                        END IF;
                    END
                    $$;
                    """
                )
            )


def ensure_database(admin_engine: Engine) -> None:
    """Create the service database owned by ``disclosure_owner`` if absent."""

    with admin_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": DATABASE_NAME},
        ).scalar()
        if not exists:
            conn.execute(
                text(
                    f"CREATE DATABASE {_quote_ident(DATABASE_NAME)} "
                    f"OWNER {_quote_ident(OWNER_ROLE)}"
                )
            )


def ensure_schemas_and_base_grants(target_engine: Engine) -> None:
    """Create the three schemas (owned by owner) and schema-level USAGE grants."""

    statements: list[str] = []
    for schema in (CORE_SCHEMA, PUBLIC_SCHEMA, OPS_SCHEMA):
        statements.append(
            f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(schema)} "
            f"AUTHORIZATION {_quote_ident(OWNER_ROLE)}"
        )

    # App may use core/ops (read+write) and read public views.
    statements.append(
        f"GRANT USAGE ON SCHEMA {_quote_ident(CORE_SCHEMA)}, "
        f"{_quote_ident(OPS_SCHEMA)} TO {_quote_ident(APP_ROLE)}"
    )
    statements.append(
        f"GRANT USAGE ON SCHEMA {_quote_ident(PUBLIC_SCHEMA)} TO {_quote_ident(APP_ROLE)}"
    )

    # Read-only roles may only use the public schema.
    reader_list = ", ".join(_quote_ident(r) for r in READ_ONLY_PUBLIC_ROLES)
    statements.append(
        f"GRANT USAGE ON SCHEMA {_quote_ident(PUBLIC_SCHEMA)} TO {reader_list}"
    )

    with target_engine.connect() as conn:
        for statement in statements:
            conn.execute(text(statement))


def bootstrap_all(admin_engine: Engine, target_engine: Engine) -> None:
    """Full bootstrap: roles + database (admin), then schemas/grants (target)."""

    ensure_roles(admin_engine)
    ensure_database(admin_engine)
    ensure_schemas_and_base_grants(target_engine)
