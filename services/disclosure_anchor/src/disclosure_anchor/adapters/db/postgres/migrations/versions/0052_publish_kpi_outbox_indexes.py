"""Bound durable publish KPI replay and late-evidence lookup.

Revision ID: 0052_publish_kpi_indexes
Revises: 0051_uscc_observation
Create Date: 2026-08-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import OPS_SCHEMA


revision: str = "0052_publish_kpi_indexes"
down_revision: Union[str, None] = "0051_uscc_observation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_outbox_publish_kpi_base_time",
        "outbox_event",
        ["occurred_at", "processing_run_id"],
        schema=OPS_SCHEMA,
        postgresql_where=sa.text("event_kind = 'processing_run_published'"),
    )
    op.create_index(
        "ix_outbox_publish_kpi_supplement_run",
        "outbox_event",
        ["processing_run_id", "seq"],
        schema=OPS_SCHEMA,
        postgresql_where=sa.text(
            "event_kind = 'processing_run_publish_evidence_backfilled'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_publish_kpi_supplement_run",
        table_name="outbox_event",
        schema=OPS_SCHEMA,
    )
    op.drop_index(
        "ix_outbox_publish_kpi_base_time",
        table_name="outbox_event",
        schema=OPS_SCHEMA,
    )
