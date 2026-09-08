"""Allow explicit observer-v3 evidence without rewriting historical v2 rows.

Revision ID: 0062_observer_v3_evidence
Revises: 0061_validate_v4_spec
"""

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import OPS_SCHEMA

revision = "0062_observer_v3_evidence"
down_revision = "0061_validate_v4_spec"
branch_labels = None
depends_on = None

_TABLE = "durable_publish_supplement"
_CONSTRAINT = "ck_durable_publish_supplement_contract"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, schema=OPS_SCHEMA, type_="check")
    op.create_check_constraint(
        _CONSTRAINT, _TABLE,
        "observer_contract_version IN ('mineru.synchronized-telemetry-receipt.v2', 'mineru.synchronized-telemetry-receipt.v3')",
        schema=OPS_SCHEMA,
    )


def downgrade() -> None:
    # PostgreSQL validates existing rows. A v3 row blocks downgrade; it is never
    # removed or relabelled as v2 to make the old constraint fit.
    op.drop_constraint(_CONSTRAINT, _TABLE, schema=OPS_SCHEMA, type_="check")
    op.create_check_constraint(
        _CONSTRAINT, _TABLE,
        "observer_contract_version = 'mineru.synchronized-telemetry-receipt.v2'",
        schema=OPS_SCHEMA,
    )
