"""Hide Unit-build failures remediated by a later successful generation.

Revision ID: 0048_remediation_ops
Revises: 0047_drop_unit_semantic_key
Create Date: 2026-08-22
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    OPS_SCHEMA,
)


revision: str = "0048_remediation_ops"
down_revision: Union[str, None] = "0047_drop_unit_semantic_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PENDING_BUILD_VIEW = "pending_build_v1"
_TERMINAL_VIEW = "unit_build_terminal_v1"


def upgrade() -> None:
    op.execute(_pending_build_view_sql(remediation_aware=True))
    op.execute(_terminal_view_sql(remediation_aware=True))
    _grant_views()


def downgrade() -> None:
    op.execute(_pending_build_view_sql(remediation_aware=False))
    op.execute(_terminal_view_sql(remediation_aware=False))
    _grant_views()


def _grant_views() -> None:
    for view in (_PENDING_BUILD_VIEW, _TERMINAL_VIEW):
        op.execute(f"GRANT SELECT ON {OPS_SCHEMA}.{view} TO {APP_ROLE}")


def _unremediated_predicate(alias: str) -> str:
    return f"""
        AND NOT EXISTS (
            SELECT 1
              FROM {CORE_SCHEMA}.processing_run newer
             WHERE newer.document_id = {alias}.document_id
               AND newer.run_kind IN ('parse', 'rebuild_units')
               AND newer.status = 'succeeded'
               AND newer.unit_build_status = 'succeeded'
               AND (newer.started_at, newer.processing_run_id)
                   > ({alias}.started_at, {alias}.processing_run_id)
        )"""


def _pending_build_view_sql(*, remediation_aware: bool) -> str:
    remediation_predicate = (
        _unremediated_predicate("r") if remediation_aware else ""
    )
    return f"""
    CREATE OR REPLACE VIEW {OPS_SCHEMA}.{_PENDING_BUILD_VIEW} AS
    SELECT r.processing_run_id, r.document_id, r.unit_build_status,
           r.unit_build_attempt_count
      FROM {CORE_SCHEMA}.processing_run r
     WHERE r.status = 'succeeded'
       AND r.unit_build_status IN ('not_started', 'failed')
       AND r.run_kind IN ('parse', 'rebuild_units')
       AND r.provider_document_relpath IS NOT NULL
       AND r.normalized_ir_relpath IS NULL{remediation_predicate}
    """


def _terminal_view_sql(*, remediation_aware: bool) -> str:
    remediation_predicate = (
        _unremediated_predicate("r") if remediation_aware else ""
    )
    return f"""
    CREATE OR REPLACE VIEW {OPS_SCHEMA}.{_TERMINAL_VIEW} AS
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
      FROM {CORE_SCHEMA}.processing_run r
     WHERE r.run_kind IN ('parse', 'rebuild_units')
       AND r.status = 'succeeded'
       AND (
           r.unit_build_status = 'failed'
           OR r.semantic_adjudication_status IN (
               'degraded_unavailable', 'failed_closed'
           )
       ){remediation_predicate}
    """
