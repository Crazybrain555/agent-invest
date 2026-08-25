"""Allow the application role to read, but never modify, migration head.

Revision ID: 0049_app_head_read
Revises: 0048_remediation_ops
Create Date: 2026-08-22
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    ALEMBIC_VERSION_TABLE,
    APP_ROLE,
    OPS_SCHEMA,
)


revision: str = "0049_app_head_read"
down_revision: Union[str, None] = "0048_remediation_ops"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        f"GRANT SELECT ON {OPS_SCHEMA}.{ALEMBIC_VERSION_TABLE} TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        f"REVOKE SELECT ON {OPS_SCHEMA}.{ALEMBIC_VERSION_TABLE} FROM {APP_ROLE}"
    )
