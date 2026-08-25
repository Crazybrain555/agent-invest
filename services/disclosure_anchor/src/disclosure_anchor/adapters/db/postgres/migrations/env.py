"""Alembic environment for disclosure_anchor.

Online migrations require ``DISCLOSURE_MIGRATION_DATABASE_URL``. Offline SQL
generation may use that explicit setting or ``sqlalchemy.url`` in
``alembic.ini``; the runtime app ``DATABASE_URL`` is never a migration fallback.

Migrations connect and immediately ``SET ROLE disclosure_owner`` so every created
object is owned by the owner role. The Alembic version table lives in the ops
schema, never the implicit ``public`` schema.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection

from disclosure_anchor.adapters.db.postgres.connection import create_db_engine
from disclosure_anchor.adapters.db.postgres.models import Base
from disclosure_anchor.adapters.db.postgres.schema import (
    ALEMBIC_VERSION_TABLE,
    ALEMBIC_VERSION_TABLE_SCHEMA,
    OWNER_ROLE,
)
from disclosure_anchor.application.worker.locks import exclusive_corpus_mutation
from disclosure_anchor.settings import load_settings

config = context.config
target_metadata = Base.metadata


def _preflight_private_unit_route_convergence(connection: Connection) -> None:
    """Fail before immutable 0047 can drop a scalar not represented plurally."""

    route_columns = set(
        connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'disclosure_core' "
                "AND table_name = 'document_unit' "
                "AND column_name IN ('semantic_key', 'semantic_keys')"
            )
        ).scalars()
    )
    # Before 0034 the plural column does not exist yet, so there is nothing to
    # compare; 0034 creates and backfills it before the irreversible 0047 drop.
    # At/after 0047 the scalar is absent and the forward-only 0050 assertion
    # owns validation of the surviving plural route arrays.
    if route_columns != {"semantic_key", "semantic_keys"}:
        return
    mismatches = connection.execute(
        text(
            """
            SELECT count(*)
              FROM disclosure_core.document_unit
             WHERE CASE
                 WHEN semantic_key IS NULL AND semantic_keys IS NULL THEN false
                 WHEN semantic_key IS NULL OR semantic_keys IS NULL THEN true
                 WHEN jsonb_typeof(semantic_keys) IS DISTINCT FROM 'array' THEN true
                 WHEN jsonb_array_length(semantic_keys) NOT BETWEEN 1 AND 8 THEN true
                 WHEN semantic_keys->>0 IS DISTINCT FROM semantic_key THEN true
                 ELSE false
             END
            """
        )
    ).scalar_one()
    if mismatches:
        raise RuntimeError(
            "migration preflight refuses to cross 0047 while private scalar and "
            "plural Unit routes differ"
        )


def _resolve_url(*, offline: bool) -> str:
    try:
        settings = load_settings()
    except Exception:  # pragma: no cover - falls back to ini for offline tooling
        settings = None

    if settings is not None:
        secret = settings.disclosure_migration_database_url
        if secret is not None:
            return secret.get_secret_value()

    if not offline:
        raise RuntimeError(
            "No online migration database URL: set "
            "DISCLOSURE_MIGRATION_DATABASE_URL"
        )

    ini_url = config.get_main_option("sqlalchemy.url")
    if not ini_url:
        raise RuntimeError(
            "No offline migration database URL: set "
            "DISCLOSURE_MIGRATION_DATABASE_URL or sqlalchemy.url in alembic.ini"
        )
    return ini_url


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(offline=True),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=ALEMBIC_VERSION_TABLE,
        version_table_schema=ALEMBIC_VERSION_TABLE_SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_resolve_url(offline=False), set_role=OWNER_ROLE)
    try:
        with exclusive_corpus_mutation(engine):
            with engine.connect() as connection:
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    version_table=ALEMBIC_VERSION_TABLE,
                    version_table_schema=ALEMBIC_VERSION_TABLE_SCHEMA,
                    include_schemas=True,
                )
                with context.begin_transaction():
                    _preflight_private_unit_route_convergence(connection)
                    context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
