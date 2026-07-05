"""harden ops permissions and add FK helper indexes

Revision ID: 0002_harden_ops_permissions
Revises: 0001_initial
Create Date: 2026-06-29
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    ALEMBIC_VERSION_TABLE,
    APP_ROLE,
    CORE_SCHEMA,
    OPS_SCHEMA,
    OWNER_ROLE,
)

# revision identifiers, used by Alembic.
revision: str = "0002_harden_ops_permissions"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_SQL: tuple[str, ...] = (
    f"CREATE INDEX IF NOT EXISTS ix_tracked_company_security "
    f"ON {CORE_SCHEMA}.tracked_company (security_id)",
    f"CREATE INDEX IF NOT EXISTS ix_source_access_company "
    f"ON {CORE_SCHEMA}.source_access (company_id)",
    f"CREATE INDEX IF NOT EXISTS ix_source_access_security "
    f"ON {CORE_SCHEMA}.source_access (security_id)",
    f"CREATE INDEX IF NOT EXISTS ix_document_source_access "
    f"ON {CORE_SCHEMA}.document (source_access_id)",
)


def upgrade() -> None:
    for statement in INDEX_SQL:
        op.execute(statement)

    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {OPS_SCHEMA}.outbox_event TO {APP_ROLE}"
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES ON {OPS_SCHEMA}.{ALEMBIC_VERSION_TABLE} FROM {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA {OPS_SCHEMA} "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA {OPS_SCHEMA} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {OPS_SCHEMA}.{ALEMBIC_VERSION_TABLE} "
        f"TO {APP_ROLE}"
    )

    for index_name in (
        "ix_document_source_access",
        "ix_source_access_security",
        "ix_source_access_company",
        "ix_tracked_company_security",
    ):
        op.execute(f"DROP INDEX IF EXISTS {CORE_SCHEMA}.{index_name}")
