"""Bind frozen semantic-route receipts to their processing run.

Revision ID: 0035_semantic_receipt_integrity
Revises: 0034_unit_semantic_routes
Create Date: 2026-08-13

Build may use a bounded model to choose among source-bound route candidates,
while Publish must replay that frozen decision without calling the model.  The
private receipt sidecar therefore needs a run-owned content hash.  Historical
runs remain readable with NULL because they predate semantic receipts.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA


revision: str = "0035_semantic_receipt_integrity"
down_revision: Union[str, None] = "0034_unit_semantic_routes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMN = "semantic_route_receipts_hash"
_CHECK = "ck_processing_run_semantic_receipt_hash"


def upgrade() -> None:
    op.add_column(
        "processing_run",
        sa.Column(_COLUMN, sa.String(length=128), nullable=True),
        schema=CORE_SCHEMA,
    )
    op.create_check_constraint(
        _CHECK,
        "processing_run",
        (
            f"{_COLUMN} IS NULL OR ("
            f"{_COLUMN} ~ '^sha256:[0-9a-f]{{64}}$' "
            "AND document_units_relpath IS NOT NULL)"
        ),
        schema=CORE_SCHEMA,
    )


def downgrade() -> None:
    bound_receipts = op.get_bind().execute(
        sa.text(
            f"SELECT count(*) FROM {CORE_SCHEMA}.processing_run "
            f"WHERE {_COLUMN} IS NOT NULL"
        )
    ).scalar_one()
    if bound_receipts:
        raise RuntimeError(
            "0035_semantic_receipt_integrity refuses to discard bound receipts"
        )
    op.drop_constraint(
        _CHECK,
        "processing_run",
        schema=CORE_SCHEMA,
        type_="check",
    )
    op.drop_column("processing_run", _COLUMN, schema=CORE_SCHEMA)
