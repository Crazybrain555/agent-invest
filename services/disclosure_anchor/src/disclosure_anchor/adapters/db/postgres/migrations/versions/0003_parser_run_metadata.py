"""add parser run metadata columns

Revision ID: 0003_parser_run_metadata
Revises: 0002_harden_ops_permissions
Create Date: 2026-06-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA, PUBLIC_SCHEMA

# revision identifiers, used by Alembic.
revision: str = "0003_parser_run_metadata"
down_revision: Union[str, None] = "0002_harden_ops_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("processing_run", schema=CORE_SCHEMA) as batch:
        batch.add_column(sa.Column("parser_backend", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("input_raw_file_hash", sa.String(length=128), nullable=True)
        )
        batch.add_column(sa.Column("parser_artifact_relpath", sa.Text(), nullable=True))
    op.execute(_processing_runs_view_sql())


def downgrade() -> None:
    op.execute(_processing_runs_view_sql(include_phase04_columns=False))
    with op.batch_alter_table("processing_run", schema=CORE_SCHEMA) as batch:
        batch.drop_column("parser_artifact_relpath")
        batch.drop_column("input_raw_file_hash")
        batch.drop_column("parser_backend")


def _processing_runs_view_sql(*, include_phase04_columns: bool = True) -> str:
    columns = [
        "r.processing_run_id",
        "r.document_id",
        "r.run_kind",
        "r.status",
        "r.parser_name",
        "r.parser_version",
        "r.artifact_hash",
        "r.content_hash_aggregate",
        "r.structure_hash",
        "r.is_active",
        "r.started_at",
        "r.finished_at",
        "r.created_at",
    ]
    if include_phase04_columns:
        columns.extend(["r.parser_backend", "r.input_raw_file_hash"])
    select_columns = ",\n        ".join(columns)
    return f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.processing_runs_v1 AS
    SELECT
        {select_columns}
    FROM {CORE_SCHEMA}.processing_run r
    """
