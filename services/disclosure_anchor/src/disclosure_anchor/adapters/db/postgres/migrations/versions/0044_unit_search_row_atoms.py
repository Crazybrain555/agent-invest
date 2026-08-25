"""Add strict source-bound Q&A table-row search atoms.

Revision ID: 0044_unit_search_row_atoms
Revises: 0043_visual_only_body_status
Create Date: 2026-08-20

The public Unit contract remains unchanged.  This regenerable child projects
only provider table rows whose exact header and contiguous ordinals prove a
three-column question/answer structure.  A hit still cites the parent Unit and
the explicit table target; no row becomes a new disclosure Unit.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from disclosure_anchor.adapters.db.postgres.schema import (
    CORE_SCHEMA,
    PUBLIC_SCHEMA,
    READ_ONLY_PUBLIC_ROLES,
)


revision: str = "0044_unit_search_row_atoms"
down_revision: Union[str, None] = "0043_visual_only_body_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VIEW_NAME = "unit_search_row_atoms_v1"
_EMPTY_MANIFEST_HASH = (
    "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5"
    "ed12ab4d8e11ba873c2f11161202b945"
)


def upgrade() -> None:
    op.add_column(
        "unit_search_projection",
        sa.Column(
            "row_atom_manifest_ready",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema=CORE_SCHEMA,
    )
    op.add_column(
        "unit_search_projection",
        sa.Column(
            "row_atom_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        schema=CORE_SCHEMA,
    )
    op.add_column(
        "unit_search_projection",
        sa.Column(
            "row_atom_manifest_hash",
            sa.String(length=71),
            nullable=False,
            server_default=sa.text(f"'{_EMPTY_MANIFEST_HASH}'"),
        ),
        schema=CORE_SCHEMA,
    )
    op.create_check_constraint(
        "ck_unit_search_projection_row_atom_manifest",
        "unit_search_projection",
        "row_atom_count >= 0 AND "
        "row_atom_manifest_hash ~ '^sha256:[0-9a-f]{64}$' AND "
        "((row_atom_manifest_ready = false AND row_atom_count = 0 AND "
        f"row_atom_manifest_hash = '{_EMPTY_MANIFEST_HASH}') OR "
        "(row_atom_manifest_ready = true AND "
        f"((row_atom_count = 0 AND row_atom_manifest_hash = '{_EMPTY_MANIFEST_HASH}') "
        "OR (row_atom_count > 0 AND "
        f"row_atom_manifest_hash <> '{_EMPTY_MANIFEST_HASH}'))))",
        schema=CORE_SCHEMA,
    )
    op.create_table(
        "unit_search_row_atom",
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("row_atom_index", sa.Integer(), nullable=False),
        sa.Column("table_target_id", sa.Text(), nullable=False),
        sa.Column("source_row_index", sa.Integer(), nullable=False),
        sa.Column("row_text", sa.Text(), nullable=False),
        sa.Column("row_tokens", sa.Text(), nullable=False),
        sa.Column("row_atom_manifest_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('simple', row_tokens), 'C')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.CheckConstraint(
            "row_atom_index >= 0 AND source_row_index >= 0",
            name="ck_unit_search_row_atom_indices",
        ),
        sa.CheckConstraint(
            "btrim(table_target_id) <> '' AND btrim(row_text) <> '' "
            "AND btrim(row_tokens) <> ''",
            name="ck_unit_search_row_atom_text",
        ),
        sa.CheckConstraint(
            f"{CORE_SCHEMA}.search_tsvector_is_safe('', '', row_tokens, '')",
            name="ck_unit_search_row_atom_tsv_safe",
        ),
        sa.CheckConstraint(
            "row_atom_manifest_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_unit_search_row_atom_manifest_hash",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            [f"{CORE_SCHEMA}.unit_search_projection.asset_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("asset_id", "row_atom_index"),
        schema=CORE_SCHEMA,
    )
    op.create_index(
        "ix_unit_search_row_atom_tsv",
        "unit_search_row_atom",
        ["search_tsv"],
        schema=CORE_SCHEMA,
        postgresql_using="gin",
    )
    op.execute(
        f"""
        CREATE VIEW {PUBLIC_SCHEMA}.{_VIEW_NAME} AS
        SELECT a.asset_id,
               a.row_atom_index,
               a.table_target_id,
               a.source_row_index,
               a.row_text,
               p.retrieval_rules_version,
               p.built_at,
               a.search_tsv AS row_search_tsv
          FROM {CORE_SCHEMA}.unit_search_row_atom a
          JOIN {CORE_SCHEMA}.unit_search_projection p
            ON p.asset_id = a.asset_id
        """
    )
    for role in READ_ONLY_PUBLIC_ROLES:
        op.execute(f"GRANT SELECT ON {PUBLIC_SCHEMA}.{_VIEW_NAME} TO {role}")


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{_VIEW_NAME}")
    op.drop_index(
        "ix_unit_search_row_atom_tsv",
        table_name="unit_search_row_atom",
        schema=CORE_SCHEMA,
    )
    op.drop_table("unit_search_row_atom", schema=CORE_SCHEMA)
    op.drop_constraint(
        "ck_unit_search_projection_row_atom_manifest",
        "unit_search_projection",
        type_="check",
        schema=CORE_SCHEMA,
    )
    op.drop_column(
        "unit_search_projection",
        "row_atom_manifest_hash",
        schema=CORE_SCHEMA,
    )
    op.drop_column(
        "unit_search_projection",
        "row_atom_count",
        schema=CORE_SCHEMA,
    )
    op.drop_column(
        "unit_search_projection",
        "row_atom_manifest_ready",
        schema=CORE_SCHEMA,
    )
