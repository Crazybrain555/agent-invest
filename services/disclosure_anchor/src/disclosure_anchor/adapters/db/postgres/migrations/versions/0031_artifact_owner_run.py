"""Bind every processing run to the parse run that owns its artifacts.

Revision ID: 0031_artifact_owner_run
Revises: 0030_source_bound_search_atoms
Create Date: 2026-07-28

Historical ``rebuild_units`` rows copied artifact paths without persisting the
parse-run owner.  Many corresponding parse rows have since been retired, so a
safe relational backfill is impossible.  This migration intentionally accepts
only the post-reset empty derived state; it never infers ownership from path
text.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    PUBLIC_SCHEMA,
    READ_ONLY_PUBLIC_ROLES,
)

revision: str = "0031_artifact_owner_run"
down_revision: Union[str, None] = "0030_source_bound_search_atoms"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VIEW_NAME = "processing_runs_v1"
_OWNER_FK = "fk_processing_run_artifact_owner"
_PARSE_OWNER_CHECK = "ck_processing_run_parse_artifact_owner"
_REBUILD_OWNER_CHECK = "ck_processing_run_rebuild_artifact_owner"
_OWNER_INDEX = "ix_processing_run_artifact_owner"


def upgrade() -> None:
    connection = op.get_bind()
    has_runs = bool(
        connection.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {CORE_SCHEMA}.processing_run)")
        ).scalar_one()
    )
    if has_runs:
        raise RuntimeError(
            "0031_artifact_owner_run requires an empty processing_run table; "
            "complete the manifest-bound derived corpus reset before upgrading"
        )

    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{_VIEW_NAME}")
    with op.batch_alter_table("processing_run", schema=CORE_SCHEMA) as batch:
        batch.add_column(
            sa.Column(
                "artifact_owner_processing_run_id",
                sa.String(length=64),
                nullable=False,
            )
        )
        batch.create_foreign_key(
            _OWNER_FK,
            "processing_run",
            ["artifact_owner_processing_run_id"],
            ["processing_run_id"],
            referent_schema=CORE_SCHEMA,
            deferrable=True,
            initially="DEFERRED",
        )
        batch.create_check_constraint(
            _PARSE_OWNER_CHECK,
            "run_kind <> 'parse' OR "
            "artifact_owner_processing_run_id = processing_run_id",
        )
        batch.create_check_constraint(
            _REBUILD_OWNER_CHECK,
            "run_kind <> 'rebuild_units' OR "
            "artifact_owner_processing_run_id <> processing_run_id",
        )
    op.create_index(
        _OWNER_INDEX,
        "processing_run",
        ["artifact_owner_processing_run_id"],
        schema=CORE_SCHEMA,
    )
    op.execute(_processing_runs_view_sql(include_owner=True))
    _grant_view()


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{_VIEW_NAME}")
    op.drop_index(
        _OWNER_INDEX,
        table_name="processing_run",
        schema=CORE_SCHEMA,
    )
    with op.batch_alter_table("processing_run", schema=CORE_SCHEMA) as batch:
        batch.drop_constraint(_REBUILD_OWNER_CHECK, type_="check")
        batch.drop_constraint(_PARSE_OWNER_CHECK, type_="check")
        batch.drop_constraint(_OWNER_FK, type_="foreignkey")
        batch.drop_column("artifact_owner_processing_run_id")
    op.execute(_processing_runs_view_sql(include_owner=False))
    _grant_view()


def _grant_view() -> None:
    for role in (APP_ROLE, *READ_ONLY_PUBLIC_ROLES):
        op.execute(f"GRANT SELECT ON {PUBLIC_SCHEMA}.{_VIEW_NAME} TO {role}")


def _processing_runs_view_sql(*, include_owner: bool) -> str:
    owner_column = (
        "\n        r.artifact_owner_processing_run_id," if include_owner else ""
    )
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.{_VIEW_NAME} AS
    SELECT
        r.processing_run_id,
        r.document_id,{owner_column}
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
