"""Lossless PostgreSQL search vectors with sparse body windows.

Revision ID: 0028_safe_search_windows
Revises: 0027_materialized_classification
Create Date: 2026-07-27

PostgreSQL can silently omit lexemes or positions from a ``tsvector`` before
the one-megabyte datum limit is reached.  This migration makes that physical
limit observable at the write boundary and stores only unsafe unit bodies in
lossless, non-overlapping windows.  Window boundaries are selected by the
application through the database safety probe; no document vocabulary or
fixed token-count threshold participates.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    PUBLIC_SCHEMA,
    READ_ONLY_PUBLIC_ROLES,
)

# revision identifiers, used by Alembic.
revision: str = "0028_safe_search_windows"
down_revision: Union[str, None] = "0027_materialized_classification"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SAFETY_FUNCTION = "search_tsvector_is_safe"
_PARENT_VIEW = "unit_search_projection_v1"
_WINDOW_VIEW = "unit_body_search_windows_v1"

_FULL_TSV_EXPRESSION = (
    "setweight(to_tsvector('simple', title_tokens), 'A') || "
    "setweight(to_tsvector('simple', path_tokens), 'B') || "
    "setweight(to_tsvector('simple', body_tokens), 'C') || "
    "setweight(to_tsvector('simple', key_tokens), 'D')"
)
_WINDOWED_TSV_EXPRESSION = (
    "CASE WHEN body_search_windowed THEN "
    "setweight(to_tsvector('simple', title_tokens), 'A') || "
    "setweight(to_tsvector('simple', path_tokens), 'B') || "
    "setweight(to_tsvector('simple', key_tokens), 'D') "
    "ELSE "
    f"{_FULL_TSV_EXPRESSION} "
    "END"
)
_BODY_TSV_EXPRESSION = "setweight(to_tsvector('simple', body_tokens), 'C')"

_CREATE_SAFETY_FUNCTION = f"""
CREATE FUNCTION {CORE_SCHEMA}.{_SAFETY_FUNCTION}(
    title_tokens text,
    path_tokens text,
    body_tokens text,
    key_tokens text
) RETURNS boolean
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $$
DECLARE
    candidate tsvector;
    parsed_positions bigint;
    lost_positions boolean;
BEGIN
    BEGIN
        candidate :=
            setweight(
                to_tsvector(
                    'pg_catalog.simple'::regconfig,
                    title_tokens
                ),
                'A'
            )
            ||
            setweight(
                to_tsvector(
                    'pg_catalog.simple'::regconfig,
                    path_tokens
                ),
                'B'
            )
            ||
            setweight(
                to_tsvector(
                    'pg_catalog.simple'::regconfig,
                    body_tokens
                ),
                'C'
            )
            ||
            setweight(
                to_tsvector(
                    'pg_catalog.simple'::regconfig,
                    key_tokens
                ),
                'D'
            );
    EXCEPTION
        WHEN program_limit_exceeded THEN
            RETURN false;
    END;

    IF pg_column_size(candidate) >= 1048576 THEN
        RETURN false;
    END IF;

    SELECT count(*)
      INTO parsed_positions
      FROM ts_debug(
          'pg_catalog.simple'::regconfig,
          concat_ws(
              ' ',
              title_tokens,
              path_tokens,
              body_tokens,
              key_tokens
          )
      ) AS d
     WHERE d.lexemes IS NOT NULL;

    IF parsed_positions > 16383 THEN
        RETURN false;
    END IF;

    WITH parsed AS (
        SELECT lexeme,
               count(*) AS source_count
          FROM ts_debug(
              'pg_catalog.simple'::regconfig,
              concat_ws(
                  ' ',
                  title_tokens,
                  path_tokens,
                  body_tokens,
                  key_tokens
              )
          ) AS d
          CROSS JOIN LATERAL unnest(d.lexemes) AS lexeme
         WHERE d.lexemes IS NOT NULL
         GROUP BY lexeme
    ),
    stored AS (
        SELECT u.lexeme,
               cardinality(u.positions) AS stored_count
          FROM unnest(candidate)
               AS u(lexeme, positions, weights)
    )
    SELECT EXISTS (
        SELECT 1
          FROM parsed p
          LEFT JOIN stored s USING (lexeme)
         WHERE p.source_count > COALESCE(s.stored_count, 0)
    )
      INTO lost_positions;

    RETURN NOT lost_positions;
END;
$$
"""


def _create_parent_view() -> None:
    op.execute(
        f"""
        CREATE VIEW {PUBLIC_SCHEMA}.{_PARENT_VIEW} AS
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
        op.execute(f"GRANT SELECT ON {PUBLIC_SCHEMA}.{_PARENT_VIEW} TO {role}")


def _create_window_view() -> None:
    op.execute(
        f"""
        CREATE VIEW {PUBLIC_SCHEMA}.{_WINDOW_VIEW} AS
        SELECT w.asset_id,
               w.window_index,
               w.body_token_start,
               w.body_token_end,
               w.body_tokens,
               p.retrieval_rules_version,
               p.built_at,
               w.search_tsv
          FROM {CORE_SCHEMA}.unit_body_search_window w
          JOIN {CORE_SCHEMA}.unit_search_projection p
            ON p.asset_id = w.asset_id
        """
    )
    for role in READ_ONLY_PUBLIC_ROLES:
        op.execute(f"GRANT SELECT ON {PUBLIC_SCHEMA}.{_WINDOW_VIEW} TO {role}")


def upgrade() -> None:
    op.execute(_CREATE_SAFETY_FUNCTION)
    op.execute(
        f"REVOKE ALL ON FUNCTION {CORE_SCHEMA}.{_SAFETY_FUNCTION}"
        "(text, text, text, text) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {CORE_SCHEMA}.{_SAFETY_FUNCTION}"
        f"(text, text, text, text) TO {APP_ROLE}"
    )

    connection = op.get_bind()
    unsafe_asset_id = connection.execute(
        sa.text(
            f"""
            SELECT asset_id
              FROM {CORE_SCHEMA}.unit_search_projection
             WHERE NOT {CORE_SCHEMA}.{_SAFETY_FUNCTION}(
                 title_tokens,
                 path_tokens,
                 body_tokens,
                 key_tokens
             )
             ORDER BY asset_id
             LIMIT 1
            """
        )
    ).scalar()
    if unsafe_asset_id is not None:
        raise RuntimeError(
            "cannot migrate a lossy unit_search_projection row; "
            "reset/rebuild the derived projection first: "
            f"asset_id={unsafe_asset_id}"
        )

    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_PARENT_VIEW}")
    op.drop_index(
        "ix_unit_search_projection_tsv",
        table_name="unit_search_projection",
        schema=CORE_SCHEMA,
    )
    op.add_column(
        "unit_search_projection",
        sa.Column(
            "body_search_windowed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema=CORE_SCHEMA,
    )
    op.drop_column("unit_search_projection", "search_tsv", schema=CORE_SCHEMA)
    op.add_column(
        "unit_search_projection",
        sa.Column(
            "search_tsv",
            TSVECTOR(),
            sa.Computed(_WINDOWED_TSV_EXPRESSION, persisted=True),
            nullable=False,
        ),
        schema=CORE_SCHEMA,
    )
    op.create_check_constraint(
        "ck_unit_search_projection_tsv_safe",
        "unit_search_projection",
        (
            f"{CORE_SCHEMA}.{_SAFETY_FUNCTION}("
            "title_tokens, path_tokens, "
            "CASE WHEN body_search_windowed THEN '' ELSE body_tokens END, "
            "key_tokens)"
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

    op.create_table(
        "unit_body_search_window",
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("window_index", sa.Integer(), nullable=False),
        sa.Column("body_token_start", sa.BigInteger(), nullable=False),
        sa.Column("body_token_end", sa.BigInteger(), nullable=False),
        sa.Column("body_tokens", sa.Text(), nullable=False),
        sa.Column(
            "search_tsv",
            TSVECTOR(),
            sa.Computed(_BODY_TSV_EXPRESSION, persisted=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "window_index >= 0",
            name="ck_unit_body_search_window_index",
        ),
        sa.CheckConstraint(
            "body_token_start >= 0 AND body_token_end > body_token_start",
            name="ck_unit_body_search_window_range",
        ),
        sa.CheckConstraint(
            "btrim(body_tokens) <> ''",
            name="ck_unit_body_search_window_body",
        ),
        sa.CheckConstraint(
            f"{CORE_SCHEMA}.{_SAFETY_FUNCTION}('', '', body_tokens, '')",
            name="ck_unit_body_search_window_tsv_safe",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            [f"{CORE_SCHEMA}.unit_search_projection.asset_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("asset_id", "window_index"),
        schema=CORE_SCHEMA,
    )
    op.create_index(
        "ix_unit_body_search_window_tsv",
        "unit_body_search_window",
        ["search_tsv"],
        unique=False,
        schema=CORE_SCHEMA,
        postgresql_using="gin",
    )

    _create_parent_view()
    _create_window_view()


def downgrade() -> None:
    connection = op.get_bind()
    windowed = connection.execute(
        sa.text(
            f"""
            SELECT EXISTS (
                SELECT 1
                  FROM {CORE_SCHEMA}.unit_search_projection
                 WHERE body_search_windowed
                UNION ALL
                SELECT 1
                  FROM {CORE_SCHEMA}.unit_body_search_window
            )
            """
        )
    ).scalar()
    if windowed:
        raise RuntimeError(
            "cannot downgrade while windowed search projections exist; "
            "rebuild or clear the regenerable projection first"
        )

    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_WINDOW_VIEW}")
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_PARENT_VIEW}")
    op.drop_index(
        "ix_unit_body_search_window_tsv",
        table_name="unit_body_search_window",
        schema=CORE_SCHEMA,
    )
    op.drop_table("unit_body_search_window", schema=CORE_SCHEMA)
    op.drop_index(
        "ix_unit_search_projection_tsv",
        table_name="unit_search_projection",
        schema=CORE_SCHEMA,
    )
    op.drop_constraint(
        "ck_unit_search_projection_tsv_safe",
        "unit_search_projection",
        schema=CORE_SCHEMA,
        type_="check",
    )
    op.drop_column("unit_search_projection", "search_tsv", schema=CORE_SCHEMA)
    op.drop_column(
        "unit_search_projection",
        "body_search_windowed",
        schema=CORE_SCHEMA,
    )
    op.add_column(
        "unit_search_projection",
        sa.Column(
            "search_tsv",
            TSVECTOR(),
            sa.Computed(_FULL_TSV_EXPRESSION, persisted=True),
            nullable=False,
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
    _create_parent_view()
    op.execute(
        f"DROP FUNCTION {CORE_SCHEMA}.{_SAFETY_FUNCTION}"
        "(text, text, text, text)"
    )
