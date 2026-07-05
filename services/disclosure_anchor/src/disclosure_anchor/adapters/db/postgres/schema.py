"""Canonical schema, role and version-table names for the PostgreSQL layout.

These constants are the single source of truth shared by bootstrap, migrations,
ORM models and tests so the names never drift across the codebase.
"""

from __future__ import annotations

# Schemas inside the disclosure_anchor database.
CORE_SCHEMA = "disclosure_core"
PUBLIC_SCHEMA = "disclosure_public"
OPS_SCHEMA = "disclosure_ops"

ALL_SCHEMAS = (CORE_SCHEMA, PUBLIC_SCHEMA, OPS_SCHEMA)

# Cluster-level roles.
OWNER_ROLE = "disclosure_owner"
APP_ROLE = "disclosure_app"
READER_ROLE = "disclosure_reader"
FUTURE_L2_READER_ROLE = "future_l2_reader"

ALL_ROLES = (OWNER_ROLE, APP_ROLE, READER_ROLE, FUTURE_L2_READER_ROLE)
READ_ONLY_PUBLIC_ROLES = (READER_ROLE, FUTURE_L2_READER_ROLE)

# Default database name owned by this service.
DATABASE_NAME = "disclosure_anchor"

# Alembic version table lives in the ops schema, not the implicit public schema.
ALEMBIC_VERSION_TABLE = "alembic_version"
ALEMBIC_VERSION_TABLE_SCHEMA = OPS_SCHEMA

# Public read views exposed to sibling services.
PUBLIC_VIEWS = (
    "documents_v1",
    "document_units_v1",
    "processing_runs_v1",
    "source_refs_v1",
    "change_events_v1",
)
