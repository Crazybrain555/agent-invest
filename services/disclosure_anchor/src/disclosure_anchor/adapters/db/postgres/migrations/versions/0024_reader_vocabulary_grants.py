"""reader-role grants for /v1/health and /v1/classification

Revision ID: 0024_reader_vocabulary_grants
Revises: 0023_prod_readiness_hardening
Create Date: 2026-07-14

Found by live smoke after DISCLOSURE_READER_DATABASE_URL got a real reader
role (round23): with the reader engine no longer falling back to the app
role, /v1/health could not read the alembic version table (reported
"degraded" forever) and GET /v1/classification could not read the rule
vocabulary. Narrow grants only — schema USAGE plus SELECT on exactly these
two tables; no other core/ops object opens up to the read-only roles.
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    ALEMBIC_VERSION_TABLE,
    ALEMBIC_VERSION_TABLE_SCHEMA,
    CORE_SCHEMA,
    OPS_SCHEMA,
    READ_ONLY_PUBLIC_ROLES,
)

# revision identifiers, used by Alembic.
revision: str = "0024_reader_vocabulary_grants"
down_revision: Union[str, None] = "0023_prod_readiness_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for role in READ_ONLY_PUBLIC_ROLES:
        op.execute(f"GRANT USAGE ON SCHEMA {OPS_SCHEMA} TO {role}")
        op.execute(
            f"GRANT SELECT ON {ALEMBIC_VERSION_TABLE_SCHEMA}."
            f"{ALEMBIC_VERSION_TABLE} TO {role}"
        )
        op.execute(f"GRANT USAGE ON SCHEMA {CORE_SCHEMA} TO {role}")
        op.execute(
            f"GRANT SELECT ON {CORE_SCHEMA}.classification_rule TO {role}"
        )


def downgrade() -> None:
    for role in READ_ONLY_PUBLIC_ROLES:
        op.execute(
            f"REVOKE SELECT ON {CORE_SCHEMA}.classification_rule FROM {role}"
        )
        op.execute(f"REVOKE USAGE ON SCHEMA {CORE_SCHEMA} FROM {role}")
        op.execute(
            f"REVOKE SELECT ON {ALEMBIC_VERSION_TABLE_SCHEMA}."
            f"{ALEMBIC_VERSION_TABLE} FROM {role}"
        )
        op.execute(f"REVOKE USAGE ON SCHEMA {OPS_SCHEMA} FROM {role}")
