"""Idempotent database/role/schema bootstrap.

This runs *before* migrations. It creates the cluster-level roles, verifies the
shared ``invest_engine`` database already exists, and creates the three owned
schemas with their schema-level USAGE grants. Table/view grants are applied by
the migration once the objects exist.

The service never creates or owns the shared database; database provisioning is
a repository-level operation outside this component boundary.
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
    SHARED_DATABASE_OWNER_ROLE,
)
from disclosure_anchor.domain.errors import ConfigurationError


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
    """Fail unless the existing shared database is not service-owned."""

    with admin_engine.connect() as conn:
        database_owner = conn.execute(
            text(
                "SELECT r.rolname FROM pg_database AS d "
                "JOIN pg_roles AS r ON r.oid = d.datdba "
                "WHERE d.datname = :name"
            ),
            {"name": DATABASE_NAME},
        ).scalar()
        if database_owner is None:
            raise ConfigurationError(
                f"shared database {DATABASE_NAME!r} does not exist; "
                "provision it at the repository level before service bootstrap"
            )
        if database_owner != SHARED_DATABASE_OWNER_ROLE:
            raise ConfigurationError(
                f"shared database {DATABASE_NAME!r} must be owned by repository "
                f"role {SHARED_DATABASE_OWNER_ROLE!r}, got {database_owner!r}; "
                "reassign ownership outside this service before bootstrap"
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
    """Full bootstrap: roles + shared DB check, then owned schemas/grants."""

    ensure_roles(admin_engine)
    ensure_database(admin_engine)
    ensure_schemas_and_base_grants(target_engine)
