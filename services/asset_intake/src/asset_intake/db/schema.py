"""Canonical schema, role and version-table names for the PostgreSQL layout.

Single source of truth shared by bootstrap, migrations, table definitions and
tests so the names never drift. This service owns only the intake_* schemas and
intake_* roles inside the shared invest_engine database; disclosure_* objects
are strictly read-only and never appear in any statement here.
"""

from __future__ import annotations

# Schemas owned by this service inside the shared invest_engine database.
CORE_SCHEMA = "intake_core"
PUBLIC_SCHEMA = "intake_public"
OPS_SCHEMA = "intake_ops"

ALL_SCHEMAS = (CORE_SCHEMA, PUBLIC_SCHEMA, OPS_SCHEMA)

# Cluster-level roles. future_l2_reader is shared with sibling services and is
# created idempotently by whichever service bootstraps first.
OWNER_ROLE = "intake_owner"
APP_ROLE = "intake_app"
READER_ROLE = "intake_reader"
FUTURE_L2_READER_ROLE = "future_l2_reader"

ALL_ROLES = (OWNER_ROLE, APP_ROLE, READER_ROLE, FUTURE_L2_READER_ROLE)
READ_ONLY_PUBLIC_ROLES = (READER_ROLE, FUTURE_L2_READER_ROLE)

# Shared monorepo database; must already exist (bootstrap never creates it).
DATABASE_NAME = "invest_engine"

# Alembic version table lives in the ops schema, not the implicit public schema.
ALEMBIC_VERSION_TABLE = "alembic_version"
ALEMBIC_VERSION_TABLE_SCHEMA = OPS_SCHEMA
