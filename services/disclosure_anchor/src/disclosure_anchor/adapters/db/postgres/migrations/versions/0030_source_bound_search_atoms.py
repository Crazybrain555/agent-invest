"""Source-bound body atoms for exact CJK substring candidates.

Revision ID: 0030_source_bound_search_atoms
Revises: 0029_run_projection_state
Create Date: 2026-07-28

The existing body tsvector remains the word-search channel.  This child table
adds one NFKC/casefolded row per nonblank leaf selected by the unit's explicit
``search_targets`` graph.  It never joins adjacent targets or mixed parts, so
a trigram candidate can be exactly rechecked inside one source-bound atom.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    CORE_SCHEMA,
    PUBLIC_SCHEMA,
    READ_ONLY_PUBLIC_ROLES,
)

revision: str = "0030_source_bound_search_atoms"
down_revision: Union[str, None] = "0029_run_projection_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VIEW_NAME = "unit_search_atoms_v1"


def upgrade() -> None:
    op.create_table(
        "unit_search_atom",
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("atom_index", sa.Integer(), nullable=False),
        sa.Column("atom_text", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "atom_index >= 0",
            name="ck_unit_search_atom_index",
        ),
        sa.CheckConstraint(
            "btrim(atom_text) <> ''",
            name="ck_unit_search_atom_text",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            [f"{CORE_SCHEMA}.unit_search_projection.asset_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("asset_id", "atom_index"),
        schema=CORE_SCHEMA,
    )
    op.create_index(
        "ix_unit_search_atom_text_trgm",
        "unit_search_atom",
        ["atom_text"],
        schema=CORE_SCHEMA,
        postgresql_using="gin",
        postgresql_ops={"atom_text": "gin_trgm_ops"},
    )
    op.execute(
        f"""
        CREATE VIEW {PUBLIC_SCHEMA}.{_VIEW_NAME} AS
        SELECT a.asset_id,
               a.atom_index,
               a.atom_text,
               p.retrieval_rules_version,
               p.built_at
          FROM {CORE_SCHEMA}.unit_search_atom a
          JOIN {CORE_SCHEMA}.unit_search_projection p
            ON p.asset_id = a.asset_id
        """
    )
    for role in READ_ONLY_PUBLIC_ROLES:
        op.execute(f"GRANT SELECT ON {PUBLIC_SCHEMA}.{_VIEW_NAME} TO {role}")


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{_VIEW_NAME}")
    op.drop_index(
        "ix_unit_search_atom_text_trgm",
        table_name="unit_search_atom",
        schema=CORE_SCHEMA,
    )
    op.drop_table("unit_search_atom", schema=CORE_SCHEMA)
