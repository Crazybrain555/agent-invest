"""Close parser identity and deterministic search-projection run state.

Revision ID: 0029_run_projection_state
Revises: 0028_safe_search_windows
Create Date: 2026-07-28

``parser_target_identity`` binds a processing run to the exact parser target
that produced it. ``search_projection_error`` records only a deterministic,
non-retryable projection failure for the retrieval-rules version that observed
it. Both are internal run facts; neither changes a public read view.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA

# revision identifiers, used by Alembic.
revision: str = "0029_run_projection_state"
down_revision: Union[str, None] = "0028_safe_search_windows"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PARSER_TARGET_CONSTRAINT = "ck_processing_run_parser_target_identity"
_PROJECTION_ERROR_CONSTRAINT = "ck_processing_run_search_projection_error"


def upgrade() -> None:
    op.add_column(
        "processing_run",
        sa.Column(
            "parser_target_identity",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema=CORE_SCHEMA,
    )
    op.add_column(
        "processing_run",
        sa.Column(
            "search_projection_error",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema=CORE_SCHEMA,
    )
    op.create_check_constraint(
        _PARSER_TARGET_CONSTRAINT,
        "processing_run",
        (
            "parser_target_identity IS NULL "
            "OR jsonb_typeof(parser_target_identity) = 'object'"
        ),
        schema=CORE_SCHEMA,
    )
    op.create_check_constraint(
        _PROJECTION_ERROR_CONSTRAINT,
        "processing_run",
        (
            "search_projection_error IS NULL OR ("
            "jsonb_typeof(search_projection_error) = 'object' "
            "AND COALESCE(search_projection_error->>'stage' = "
            "'search_projection', false) "
            "AND COALESCE(search_projection_error->'retryable' = "
            "'false'::jsonb, false) "
            "AND NULLIF(btrim(search_projection_error->>'error_code'), '') "
            "IS NOT NULL "
            "AND NULLIF(btrim("
            "search_projection_error->>'retrieval_rules_version'), '') "
            "IS NOT NULL)"
        ),
        schema=CORE_SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        _PROJECTION_ERROR_CONSTRAINT,
        "processing_run",
        schema=CORE_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        _PARSER_TARGET_CONSTRAINT,
        "processing_run",
        schema=CORE_SCHEMA,
        type_="check",
    )
    op.drop_column(
        "processing_run",
        "search_projection_error",
        schema=CORE_SCHEMA,
    )
    op.drop_column(
        "processing_run",
        "parser_target_identity",
        schema=CORE_SCHEMA,
    )
