"""Require all-history execution-spec backfill before runtime cutover.

Revision ID: 0061_validate_v4_spec
Revises: 0060_v4_execution_spec
"""

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import OPS_SCHEMA

revision = "0061_validate_v4_spec"
down_revision = "0060_v4_execution_spec"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(f"ALTER TABLE {OPS_SCHEMA}.remote_parse_v4_checkpoint VALIDATE CONSTRAINT fk_v4_checkpoint_execution_spec")


def downgrade() -> None:
    # Validation adds no data and remains true; dropping its proof is unnecessary.
    pass
