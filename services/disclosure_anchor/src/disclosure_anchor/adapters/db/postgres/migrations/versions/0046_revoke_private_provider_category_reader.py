"""Remove reader access to the private provider-category table.

Revision ID: 0046_revoke_private_acl
Revises: 0045_semantic_terminal
Create Date: 2026-08-22
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    CORE_SCHEMA,
    READER_ROLE,
)


revision: str = "0046_revoke_private_acl"
down_revision: Union[str, None] = "0045_semantic_terminal"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        f"REVOKE SELECT ON {CORE_SCHEMA}.provider_category FROM {READER_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        f"GRANT SELECT ON {CORE_SCHEMA}.provider_category TO {READER_ROLE}"
    )
