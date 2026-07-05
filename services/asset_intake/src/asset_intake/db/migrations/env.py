"""Alembic environment for asset_intake.

Resolution order for the database URL:
1. ``ASSET_INTAKE_MIGRATION_DATABASE_URL`` / ``ASSET_INTAKE_DATABASE_URL`` via
   service settings.
2. ``sqlalchemy.url`` in ``alembic.ini`` (used only for offline tooling).

Migrations connect and immediately ``SET ROLE intake_owner`` so every created
object is owned by the owner role. The Alembic version table lives in the
intake_ops schema, never the implicit ``public`` schema, and nothing here may
reference disclosure_* objects.
"""

from __future__ import annotations

from alembic import context

from asset_intake.db import models
from asset_intake.db.connection import create_db_engine
from asset_intake.db.schema import (
    ALEMBIC_VERSION_TABLE,
    ALEMBIC_VERSION_TABLE_SCHEMA,
    OWNER_ROLE,
)
from asset_intake.settings import get_settings

config = context.config
target_metadata = models.metadata


def _resolve_url() -> str:
    try:
        settings = get_settings()
    except Exception:  # pragma: no cover - falls back to ini for offline tooling
        settings = None

    if settings is not None:
        secret = settings.migration_database_url or settings.database_url
        if secret is not None:
            return secret.get_secret_value()

    ini_url = config.get_main_option("sqlalchemy.url")
    if not ini_url:
        raise RuntimeError(
            "No migration database URL: set ASSET_INTAKE_MIGRATION_DATABASE_URL or "
            "ASSET_INTAKE_DATABASE_URL, or sqlalchemy.url in alembic.ini"
        )
    return ini_url


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table=ALEMBIC_VERSION_TABLE,
        version_table_schema=ALEMBIC_VERSION_TABLE_SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_resolve_url(), set_role=OWNER_ROLE)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table=ALEMBIC_VERSION_TABLE,
            version_table_schema=ALEMBIC_VERSION_TABLE_SCHEMA,
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
