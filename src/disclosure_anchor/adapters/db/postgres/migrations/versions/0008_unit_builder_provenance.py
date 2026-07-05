"""unit builder rule provenance

Revision ID: 0008_unit_builder_provenance
Revises: 0007_envelope_and_feed_hardening
Create Date: 2026-07-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    PUBLIC_SCHEMA,
    READER_ROLE,
)

# revision identifiers, used by Alembic.
revision: str = "0008_unit_builder_provenance"
down_revision: Union[str, None] = "0007_envelope_and_feed_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.processing_runs_v1")
    with op.batch_alter_table("processing_run", schema=CORE_SCHEMA) as batch:
        batch.add_column(
            sa.Column("builder_rules_version", sa.String(length=32), nullable=True)
        )
    op.execute(_processing_runs_view_sql_0008())
    _grant_processing_runs_view()


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.processing_runs_v1")
    op.execute(_processing_runs_view_sql_0007())
    _grant_processing_runs_view()
    with op.batch_alter_table("processing_run", schema=CORE_SCHEMA) as batch:
        batch.drop_column("builder_rules_version")


def _grant_processing_runs_view() -> None:
    op.execute(
        f"GRANT SELECT ON {PUBLIC_SCHEMA}.processing_runs_v1 "
        f"TO {APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )


def _processing_runs_view_sql_0008() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.processing_runs_v1 AS
    SELECT
        r.processing_run_id,
        r.document_id,
        r.run_kind,
        r.status,
        r.parser_name,
        r.parser_version,
        r.artifact_hash,
        r.content_hash_aggregate,
        r.structure_hash,
        r.is_active,
        r.started_at,
        r.finished_at,
        r.created_at,
        r.parser_backend,
        r.input_raw_file_hash,
        r.parser_method,
        r.parser_language,
        r.unit_build_status,
        r.unit_build_attempt_count,
        r.unit_built_at,
        r.builder_rules_version
    FROM {CORE_SCHEMA}.processing_run r
    """


def _processing_runs_view_sql_0007() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.processing_runs_v1 AS
    SELECT
        r.processing_run_id,
        r.document_id,
        r.run_kind,
        r.status,
        r.parser_name,
        r.parser_version,
        r.artifact_hash,
        r.content_hash_aggregate,
        r.structure_hash,
        r.is_active,
        r.started_at,
        r.finished_at,
        r.created_at,
        r.parser_backend,
        r.input_raw_file_hash,
        r.parser_method,
        r.parser_language,
        r.unit_build_status,
        r.unit_build_attempt_count,
        r.unit_built_at
    FROM {CORE_SCHEMA}.processing_run r
    """
