"""Canonical schema, role and version-table names for the PostgreSQL layout.

These constants are the single source of truth shared by bootstrap, migrations,
ORM models and tests so the names never drift across the codebase.
"""

from __future__ import annotations

# Schemas owned by this service inside the shared agent-invest database.
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

# Shared monorepo database; this service owns only the disclosure_* schemas in it.
DATABASE_NAME = "invest_engine"

# Alembic version table lives in the ops schema, not the implicit public schema.
ALEMBIC_VERSION_TABLE = "alembic_version"
ALEMBIC_VERSION_TABLE_SCHEMA = OPS_SCHEMA

# Public read views exposed to sibling services.
PUBLIC_VIEWS = (
    "documents_v1",
    "document_units_v1",
    "document_categories_v1",
    "processing_runs_v1",
    "source_refs_v1",
    "change_events_v1",
    "tracked_companies_v1",
    # 06R derived retrieval projection (regenerable, no event semantics).
    "unit_search_projection_v1",
    # Sparse lossless body windows for PostgreSQL-unsafe parent vectors.
    "unit_body_search_windows_v1",
    # Source-bound normalized body atoms for exact substring candidates.
    "unit_search_atoms_v1",
    # Derived per-document heading-tree skeleton (regenerable, no events).
    "document_outline_v1",
)
