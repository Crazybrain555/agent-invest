"""Idempotent role/schema bootstrap for the intake_* footprint.

Runs *before* migrations, connected to the shared invest_engine database as the
cluster admin. It creates the intake roles (NOLOGIN groups) and the three
intake schemas with schema-level USAGE grants. Table/view grants are applied by
migrations once objects exist.

It never creates or alters the database itself, and it never references
disclosure_* objects. All statements are safe to re-run.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from asset_intake.db.schema import (
    ALL_ROLES,
    APP_ROLE,
    CORE_SCHEMA,
    DATABASE_NAME,
    OPS_SCHEMA,
    OWNER_ROLE,
    PUBLIC_SCHEMA,
    READ_ONLY_PUBLIC_ROLES,
)


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ensure_connected_to_service_database(engine: Engine) -> None:
    """Fail fast if the admin DSN points at the wrong database."""

    with engine.connect() as conn:
        current = conn.execute(text("SELECT current_database()")).scalar()
    if current != DATABASE_NAME:
        raise RuntimeError(
            f"bootstrap must connect to {DATABASE_NAME!r}, got {current!r};"
            " the shared database is never created by this service"
        )


def ensure_roles(admin_engine: Engine) -> None:
    """Create the intake roles as NOLOGIN groups if they do not exist.

    LOGIN capability and passwords are an out-of-band operational concern and
    are never created or committed here; tests exercise privileges via
    ``SET ROLE``. Role names come from the trusted ``schema`` constants, so they
    are quoted and interpolated rather than bound.
    """

    with admin_engine.connect() as conn:
        for role in ALL_ROLES:
            conn.execute(
                text(
                    f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_roles WHERE rolname = {quote_literal(role)}
                        ) THEN
                            CREATE ROLE {quote_ident(role)} NOLOGIN;
                        END IF;
                    END
                    $$;
                    """
                )
            )
        conn.commit()


def ensure_schemas_and_base_grants(admin_engine: Engine) -> None:
    """Create the three intake schemas (owned by intake_owner) and USAGE grants."""

    statements: list[str] = []
    for schema in (CORE_SCHEMA, PUBLIC_SCHEMA, OPS_SCHEMA):
        statements.append(
            f"CREATE SCHEMA IF NOT EXISTS {quote_ident(schema)} "
            f"AUTHORIZATION {quote_ident(OWNER_ROLE)}"
        )

    # App may use core/ops (read+write) and read public views.
    statements.append(
        f"GRANT USAGE ON SCHEMA {quote_ident(CORE_SCHEMA)}, "
        f"{quote_ident(OPS_SCHEMA)} TO {quote_ident(APP_ROLE)}"
    )
    statements.append(
        f"GRANT USAGE ON SCHEMA {quote_ident(PUBLIC_SCHEMA)} TO {quote_ident(APP_ROLE)}"
    )

    # Read-only roles may only use the public schema.
    reader_list = ", ".join(quote_ident(r) for r in READ_ONLY_PUBLIC_ROLES)
    statements.append(
        f"GRANT USAGE ON SCHEMA {quote_ident(PUBLIC_SCHEMA)} TO {reader_list}"
    )

    with admin_engine.connect() as conn:
        for statement in statements:
            conn.execute(text(statement))
        conn.commit()


def bootstrap_all(admin_engine: Engine) -> None:
    """Full bootstrap over one admin connection to invest_engine."""

    ensure_connected_to_service_database(admin_engine)
    ensure_roles(admin_engine)
    ensure_schemas_and_base_grants(admin_engine)
