"""Add the append-only decision that releases one contract-class parse failure.

Revision ID: 0063_parse_requeue_decision
Revises: 0062_observer_v3_evidence
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE, CORE_SCHEMA, FUTURE_L2_READER_ROLE, OPS_SCHEMA, READER_ROLE,
)

revision: str = "0063_parse_requeue_decision"
down_revision: Union[str, None] = "0062_observer_v3_evidence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "parse_requeue_decision"
_INDEX = "ix_parse_requeue_decision_document"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("decision_id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("processing_run_id", sa.String(64), nullable=False),
        sa.Column("failure_error_code", sa.String(128), nullable=False),
        sa.Column("failure_retry_budget_class", sa.String(64), nullable=False),
        sa.Column("fixed_by", sa.String(200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=False),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], [f"{CORE_SCHEMA}.document.document_id"],
            name="fk_parse_requeue_decision_document", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["processing_run_id"],
            [f"{CORE_SCHEMA}.processing_run.processing_run_id"],
            name="fk_parse_requeue_decision_run", ondelete="RESTRICT",
        ),
        # One decision per failed run: a second release of the same evidence is
        # a duplicate, not an additional fact.
        sa.UniqueConstraint(
            "processing_run_id", name="uq_parse_requeue_decision_run"
        ),
        sa.CheckConstraint(
            "decision_id ~ '^prq_[0-9A-HJKMNP-TV-Z]{26}$'",
            name="ck_parse_requeue_decision_id",
        ),
        # Closed vocabulary, frozen with this revision: only the contract
        # classes the coordinator persists today. Automatic classes keep their
        # scheduler budget, and an unknown class is refused rather than
        # released. Widening it needs a later revision.
        sa.CheckConstraint(
            "failure_retry_budget_class IN ('provider_artifact_contract',"
            "'provider_protocol','provider_runaway','provider_terminal',"
            "'semantic_route_contract')",
            name="ck_parse_requeue_decision_class",
        ),
        sa.CheckConstraint(
            "btrim(failure_error_code) <> '' AND btrim(fixed_by) <> '' "
            "AND btrim(reason) <> '' AND btrim(decided_by) <> ''",
            name="ck_parse_requeue_decision_evidence",
        ),
        schema=OPS_SCHEMA,
    )
    op.create_index(
        _INDEX, _TABLE, ["document_id", "decided_at"], schema=OPS_SCHEMA,
    )
    op.execute(
        f"REVOKE ALL ON {OPS_SCHEMA}.{_TABLE} "
        f"FROM PUBLIC, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )
    op.execute(f"GRANT SELECT, INSERT ON {OPS_SCHEMA}.{_TABLE} TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE, schema=OPS_SCHEMA)
    op.drop_table(_TABLE, schema=OPS_SCHEMA)
