"""Add the provider-native primary output without changing public views.

Revision ID: 0032_provider_document_output
Revises: 0031_artifact_owner_run
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    OPS_SCHEMA,
)

revision: str = "0032_provider_document_output"
down_revision: Union[str, None] = "0031_artifact_owner_run"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OUTPUT_CHECK = "ck_processing_run_primary_output_exactly_one"
_PARSE_VIEW = "pending_parse_v1"
_BUILD_VIEW = "pending_build_v1"
_PUBLISH_VIEW = "pending_publish_v1"


def upgrade() -> None:
    connection = op.get_bind()
    invalid = connection.execute(
        sa.text(
            f"""
            SELECT count(*)
            FROM {CORE_SCHEMA}.processing_run
            WHERE run_kind IN ('parse', 'rebuild_units')
              AND normalized_ir_relpath IS NULL
            """
        )
    ).scalar_one()
    if invalid:
        raise RuntimeError(
            "0032_provider_document_output requires every historical parse/rebuild "
            "run to reference normalized_ir_relpath"
        )

    with op.batch_alter_table("processing_run", schema=CORE_SCHEMA) as batch:
        batch.add_column(
            sa.Column("provider_document_relpath", sa.Text(), nullable=True)
        )
        batch.create_check_constraint(
            _OUTPUT_CHECK,
            "(run_kind NOT IN ('parse', 'rebuild_units') OR "
            "num_nonnulls(normalized_ir_relpath, provider_document_relpath) = 1) "
            "AND (provider_document_relpath IS NULL OR "
            "run_kind IN ('parse', 'rebuild_units'))",
        )

    op.execute(_pending_parse_view_sql(provider_only=True))
    op.execute(_pending_build_view_sql(provider_only=True))
    op.execute(_pending_publish_view_sql(provider_only=True))
    _grant_queue_views()


def downgrade() -> None:
    connection = op.get_bind()
    has_provider_rows = bool(
        connection.execute(
            sa.text(
                f"""
                SELECT EXISTS (
                    SELECT 1
                    FROM {CORE_SCHEMA}.processing_run
                    WHERE provider_document_relpath IS NOT NULL
                )
                """
            )
        ).scalar_one()
    )
    if has_provider_rows:
        raise RuntimeError(
            "0032_provider_document_output cannot downgrade while provider "
            "document rows exist"
        )

    op.execute(_pending_parse_view_sql(provider_only=False))
    op.execute(_pending_build_view_sql(provider_only=False))
    op.execute(_pending_publish_view_sql(provider_only=False))
    _grant_queue_views()
    with op.batch_alter_table("processing_run", schema=CORE_SCHEMA) as batch:
        batch.drop_constraint(_OUTPUT_CHECK, type_="check")
        batch.drop_column("provider_document_relpath")


def _grant_queue_views() -> None:
    for view in (_PARSE_VIEW, _BUILD_VIEW, _PUBLISH_VIEW):
        op.execute(f"GRANT SELECT ON {OPS_SCHEMA}.{view} TO {APP_ROLE}")


def _pending_parse_view_sql(*, provider_only: bool) -> str:
    output_predicate = (
        "\n            AND r.provider_document_relpath IS NOT NULL"
        "\n            AND r.normalized_ir_relpath IS NULL"
        if provider_only
        else ""
    )
    return f"""
    CREATE OR REPLACE VIEW {OPS_SCHEMA}.{_PARSE_VIEW} AS
    SELECT d.document_id, d.status,
        (SELECT count(*) FROM {CORE_SCHEMA}.processing_run r
          WHERE r.document_id=d.document_id AND r.status='failed'
            AND r.run_kind='parse'{output_predicate}) AS failed_parse_count,
        (SELECT (r.error->>'retryable')::boolean
           FROM {CORE_SCHEMA}.processing_run r
          WHERE r.document_id=d.document_id AND r.status='failed'
            AND r.run_kind='parse'{output_predicate}
          ORDER BY r.started_at DESC, r.processing_run_id DESC LIMIT 1)
          AS last_failed_retryable
      FROM {CORE_SCHEMA}.document d
      WHERE d.status IN ('registered','parse_failed')
        AND NOT EXISTS (SELECT 1 FROM {CORE_SCHEMA}.processing_run r
          WHERE r.document_id=d.document_id AND r.status='running')
    """


def _pending_build_view_sql(*, provider_only: bool) -> str:
    output_predicate = (
        "\n        AND r.run_kind IN ('parse', 'rebuild_units')"
        "\n        AND r.provider_document_relpath IS NOT NULL"
        "\n        AND r.normalized_ir_relpath IS NULL"
        if provider_only
        else ""
    )
    return f"""
    CREATE OR REPLACE VIEW {OPS_SCHEMA}.{_BUILD_VIEW} AS
    SELECT r.processing_run_id, r.document_id, r.unit_build_status,
           r.unit_build_attempt_count
      FROM {CORE_SCHEMA}.processing_run r
      WHERE r.status='succeeded'
        AND r.unit_build_status IN ('not_started','failed'){output_predicate}
    """


def _pending_publish_view_sql(*, provider_only: bool) -> str:
    output_predicate = (
        "\n        AND r.run_kind IN ('parse', 'rebuild_units')"
        "\n        AND r.provider_document_relpath IS NOT NULL"
        "\n        AND r.normalized_ir_relpath IS NULL"
        if provider_only
        else ""
    )
    return f"""
    CREATE OR REPLACE VIEW {OPS_SCHEMA}.{_PUBLISH_VIEW} AS
    SELECT r.processing_run_id, r.document_id
      FROM {CORE_SCHEMA}.processing_run r
      WHERE r.status='succeeded'
        AND r.unit_build_status='succeeded'
        AND r.is_active=false{output_predicate}
        AND r.started_at > COALESCE((
            SELECT a.started_at
            FROM {CORE_SCHEMA}.processing_run a
            WHERE a.document_id=r.document_id AND a.is_active
        ), '-infinity'::timestamptz)
    """
