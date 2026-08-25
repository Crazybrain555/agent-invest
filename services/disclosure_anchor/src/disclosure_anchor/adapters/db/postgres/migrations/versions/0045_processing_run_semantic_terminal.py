"""Persist semantic terminal state and normalize JSON nulls.

Revision ID: 0045_semantic_terminal
Revises: 0044_unit_search_row_atoms
Create Date: 2026-08-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    OPS_SCHEMA,
)


revision: str = "0045_semantic_terminal"
down_revision: Union[str, None] = "0044_unit_search_row_atoms"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "processing_run"
_VIEW = "unit_build_terminal_v1"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("semantic_route_receipts_relpath", sa.Text(), nullable=True),
        schema=CORE_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "semantic_route_receipts_contract_version",
            sa.String(length=64),
            nullable=True,
        ),
        schema=CORE_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("semantic_adjudication_status", sa.String(length=32), nullable=True),
        schema=CORE_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("semantic_degraded_unit_count", sa.Integer(), nullable=True),
        schema=CORE_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("semantic_failover_group_count", sa.Integer(), nullable=True),
        schema=CORE_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "semantic_adjudication_summary",
            postgresql.JSONB(none_as_null=True),
            nullable=True,
        ),
        schema=CORE_SCHEMA,
    )
    checks = {
        "ck_processing_run_semantic_receipt_locator": (
            "(semantic_route_receipts_relpath IS NULL AND "
            "semantic_route_receipts_contract_version IS NULL) OR ("
            "semantic_route_receipts_relpath IS NOT NULL AND "
            "semantic_route_receipts_contract_version = "
            "'semantic_route_receipt.v2' AND "
            "semantic_route_receipts_hash IS NOT NULL)"
        ),
        "ck_processing_run_semantic_adjudication_status": (
            "semantic_adjudication_status IS NULL OR "
            "semantic_adjudication_status IN ('not_required','complete_primary',"
            "'complete_backup','degraded_unavailable','failed_closed')"
        ),
        "ck_processing_run_semantic_degraded_count": (
            "semantic_degraded_unit_count IS NULL OR "
            "semantic_degraded_unit_count >= 0"
        ),
        "ck_processing_run_semantic_failover_count": (
            "semantic_failover_group_count IS NULL OR "
            "semantic_failover_group_count >= 0"
        ),
        "ck_processing_run_semantic_summary": (
            "semantic_adjudication_summary IS NULL OR "
            "jsonb_typeof(semantic_adjudication_summary) = 'object'"
        ),
        "ck_processing_run_error_object": (
            "error IS NULL OR (jsonb_typeof(error) = 'object' AND "
            "error ?& ARRAY['stage','error_code','retryable'] AND "
            "jsonb_typeof(error->'retryable') = 'boolean')"
        ),
        "ck_processing_run_unit_build_error_object": (
            "unit_build_error IS NULL OR ("
            "jsonb_typeof(unit_build_error) = 'object' AND "
            "unit_build_error ?& ARRAY['stage','error_code','retryable'] AND "
            "jsonb_typeof(unit_build_error->'retryable') = 'boolean')"
        ),
    }
    for name, expression in checks.items():
        op.execute(
            f"ALTER TABLE {CORE_SCHEMA}.{_TABLE} ADD CONSTRAINT {name} "
            f"CHECK ({expression}) NOT VALID"
        )
    op.execute(
        f"UPDATE {CORE_SCHEMA}.{_TABLE} SET "
        "error = CASE WHEN error = 'null'::jsonb THEN NULL ELSE error END, "
        "unit_build_error = CASE WHEN unit_build_error = 'null'::jsonb "
        "THEN NULL ELSE unit_build_error END "
        "WHERE error = 'null'::jsonb OR unit_build_error = 'null'::jsonb"
    )
    # processing_run has a deferrable self-FK.  Updating legacy rows queues
    # constraint-trigger events; flush those events before PostgreSQL's ALTER
    # TABLE ... VALIDATE phase.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    for name in checks:
        op.execute(
            f"ALTER TABLE {CORE_SCHEMA}.{_TABLE} VALIDATE CONSTRAINT {name}"
        )
    op.execute(_view_sql())
    op.execute(f"GRANT SELECT ON {OPS_SCHEMA}.{_VIEW} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {OPS_SCHEMA}.{_VIEW}")
    for name in (
        "ck_processing_run_unit_build_error_object",
        "ck_processing_run_error_object",
        "ck_processing_run_semantic_summary",
        "ck_processing_run_semantic_failover_count",
        "ck_processing_run_semantic_degraded_count",
        "ck_processing_run_semantic_adjudication_status",
        "ck_processing_run_semantic_receipt_locator",
    ):
        op.drop_constraint(name, _TABLE, schema=CORE_SCHEMA, type_="check")
    for column in (
        "semantic_adjudication_summary",
        "semantic_failover_group_count",
        "semantic_degraded_unit_count",
        "semantic_adjudication_status",
        "semantic_route_receipts_contract_version",
        "semantic_route_receipts_relpath",
    ):
        op.drop_column(_TABLE, column, schema=CORE_SCHEMA)


def _view_sql() -> str:
    return f"""
    CREATE VIEW {OPS_SCHEMA}.{_VIEW} AS
    SELECT r.processing_run_id,
           r.document_id,
           r.run_kind,
           r.status AS parse_status,
           r.unit_build_status,
           r.unit_build_attempt_count,
           r.unit_build_error->>'error_code' AS error_code,
           r.unit_build_error->>'reason_code' AS reason_code,
           COALESCE((r.unit_build_error->>'retryable')::boolean, false) AS retryable,
           r.semantic_adjudication_status,
           r.semantic_degraded_unit_count,
           r.semantic_failover_group_count,
           r.semantic_adjudication_summary,
           r.semantic_route_receipts_relpath,
           r.semantic_route_receipts_contract_version,
           r.semantic_route_receipts_hash,
           r.is_active,
           r.unit_built_at,
           r.created_at
      FROM {CORE_SCHEMA}.{_TABLE} r
     WHERE r.run_kind IN ('parse', 'rebuild_units')
       AND r.status = 'succeeded'
       AND (
           r.unit_build_status = 'failed'
           OR r.semantic_adjudication_status IN (
               'degraded_unavailable', 'failed_closed'
           )
       )
    """
