"""06R retrieval search projection: weighted tsvector + trgm channels

Revision ID: 0025_retrieval_search_projection
Revises: 0024_reader_vocabulary_grants
Create Date: 2026-07-17

Derived layer only (milestone 06R, U7 red line): every column regenerates
deterministically from persisted units via the pinned application-side jieba
tokenizer; nothing here enters content/query-projection hashes and rebuilds
emit no outbox events. Weighted single-GIN tsvector (A=title, B=breadcrumb,
C=body, D=semantic keys) over pre-tokenized text with the built-in ``simple``
config; pg_trgm GIN over the raw title/breadcrumb strings is the short-query
and substring fallback channel.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

from disclosure_anchor.adapters.db.postgres.schema import (
    CORE_SCHEMA,
    PUBLIC_SCHEMA,
    READ_ONLY_PUBLIC_ROLES,
)

# revision identifiers, used by Alembic.
revision: str = "0025_retrieval_search_projection"
down_revision: Union[str, None] = "0024_reader_vocabulary_grants"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TSV_EXPRESSION = (
    "setweight(to_tsvector('simple', title_tokens), 'A') || "
    "setweight(to_tsvector('simple', path_tokens), 'B') || "
    "setweight(to_tsvector('simple', body_tokens), 'C') || "
    "setweight(to_tsvector('simple', key_tokens), 'D')"
)

_VIEW_NAME = "unit_search_projection_v1"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "unit_search_projection",
        sa.Column("asset_id", sa.String(length=64), primary_key=True),
        sa.Column("retrieval_rules_version", sa.String(length=64), nullable=False),
        sa.Column("title_text", sa.Text(), nullable=False),
        sa.Column("heading_path_text", sa.Text(), nullable=False),
        sa.Column("title_tokens", sa.Text(), nullable=False),
        sa.Column("path_tokens", sa.Text(), nullable=False),
        sa.Column("body_tokens", sa.Text(), nullable=False),
        sa.Column("key_tokens", sa.Text(), nullable=False),
        sa.Column(
            "header_row_candidate",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "search_tsv",
            TSVECTOR(),
            sa.Computed(_TSV_EXPRESSION, persisted=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            [f"{CORE_SCHEMA}.document_unit.asset_id"],
            ondelete="CASCADE",
        ),
        schema=CORE_SCHEMA,
    )
    op.create_index(
        "ix_unit_search_projection_tsv",
        "unit_search_projection",
        ["search_tsv"],
        unique=False,
        schema=CORE_SCHEMA,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_unit_search_projection_title_trgm",
        "unit_search_projection",
        ["title_text"],
        unique=False,
        schema=CORE_SCHEMA,
        postgresql_using="gin",
        postgresql_ops={"title_text": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_unit_search_projection_path_trgm",
        "unit_search_projection",
        ["heading_path_text"],
        unique=False,
        schema=CORE_SCHEMA,
        postgresql_using="gin",
        postgresql_ops={"heading_path_text": "gin_trgm_ops"},
    )
    op.execute(
        f"""
        CREATE VIEW {PUBLIC_SCHEMA}.{_VIEW_NAME} AS
        SELECT p.asset_id,
               p.retrieval_rules_version,
               p.title_text,
               p.heading_path_text,
               p.title_tokens,
               p.path_tokens,
               p.body_tokens,
               p.key_tokens,
               p.header_row_candidate,
               p.built_at,
               p.search_tsv
          FROM {CORE_SCHEMA}.unit_search_projection p
        """
    )
    for role in READ_ONLY_PUBLIC_ROLES:
        op.execute(f"GRANT SELECT ON {PUBLIC_SCHEMA}.{_VIEW_NAME} TO {role}")


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{_VIEW_NAME}")
    op.drop_index(
        "ix_unit_search_projection_path_trgm",
        table_name="unit_search_projection",
        schema=CORE_SCHEMA,
    )
    op.drop_index(
        "ix_unit_search_projection_title_trgm",
        table_name="unit_search_projection",
        schema=CORE_SCHEMA,
    )
    op.drop_index(
        "ix_unit_search_projection_tsv",
        table_name="unit_search_projection",
        schema=CORE_SCHEMA,
    )
    op.drop_table("unit_search_projection", schema=CORE_SCHEMA)
