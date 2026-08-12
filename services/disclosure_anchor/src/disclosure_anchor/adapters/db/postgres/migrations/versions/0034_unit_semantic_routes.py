"""Restore plural Unit semantic routes and inherited content categories.

Revision ID: 0034_unit_semantic_routes
Revises: 0033_unit_schema_convergence
Create Date: 2026-08-13

``semantic_key`` is the optional primary Unit route required by the L1
protocol.  ``semantic_keys`` is the complete ordered route set for a mixed
Unit and is not derivable from the scalar when secondary routes exist.
``content_categories`` remains a Document fact and is inherited into the
public Unit view without being copied into the Unit table.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    PUBLIC_SCHEMA,
    READER_ROLE,
)

revision: str = "0034_unit_semantic_routes"
down_revision: Union[str, None] = "0033_unit_schema_convergence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VIEW = "document_units_v1"
_INDEX = "ix_document_unit_semantic_keys"
_CHECK = "ck_document_unit_semantic_key_set"


def upgrade() -> None:
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_VIEW}")
    op.add_column(
        "document_unit",
        sa.Column("semantic_keys", JSONB(), nullable=True),
        schema=CORE_SCHEMA,
    )
    # Preserve any real scalar route that predates this migration.  New
    # writers may later add secondary routes, but never manufacture a
    # document_content placeholder merely to make this column non-null.
    op.execute(
        f"UPDATE {CORE_SCHEMA}.document_unit "
        "SET semantic_keys = jsonb_build_array(semantic_key) "
        "WHERE semantic_key IS NOT NULL"
    )
    op.create_check_constraint(
        _CHECK,
        "document_unit",
        "(semantic_key IS NULL AND semantic_keys IS NULL) OR ("
        "semantic_key IS NOT NULL "
        "AND jsonb_typeof(semantic_keys) = 'array' "
        "AND jsonb_array_length(semantic_keys) > 0 "
        "AND semantic_keys ->> 0 = semantic_key)",
        schema=CORE_SCHEMA,
    )
    op.execute(
        f"CREATE INDEX {_INDEX} ON {CORE_SCHEMA}.document_unit "
        "USING gin (semantic_keys) WHERE semantic_keys IS NOT NULL"
    )
    op.execute(_document_units_view_sql(include_semantic_routes=True))
    _grant_view()


def downgrade() -> None:
    independent_routes = op.get_bind().execute(
        sa.text(
            f"""
            SELECT count(*)
            FROM {CORE_SCHEMA}.document_unit
            WHERE semantic_keys IS NOT NULL
              AND semantic_keys <> jsonb_build_array(semantic_key)
            """
        )
    ).scalar_one()
    if independent_routes:
        raise RuntimeError(
            "0034_unit_semantic_routes refuses to discard secondary Unit routes"
        )
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_VIEW}")
    op.execute(f"DROP INDEX IF EXISTS {CORE_SCHEMA}.{_INDEX}")
    op.drop_constraint(_CHECK, "document_unit", schema=CORE_SCHEMA, type_="check")
    op.drop_column("document_unit", "semantic_keys", schema=CORE_SCHEMA)
    op.execute(_document_units_view_sql(include_semantic_routes=False))
    _grant_view()


def _grant_view() -> None:
    op.execute(
        f"GRANT SELECT ON {PUBLIC_SCHEMA}.{_VIEW} TO "
        f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )


def _document_units_view_sql(*, include_semantic_routes: bool) -> str:
    semantic_keys = "\n        u.semantic_keys," if include_semantic_routes else ""
    content_categories = (
        ",\n        d.class_content_categories AS content_categories"
        if include_semantic_routes
        else ""
    )
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.{_VIEW} AS
    SELECT
        u.asset_id,
        u.document_id,
        u.processing_run_id,
        COALESCE(r.is_active, false) AS is_active_run,
        u.provider_document_id,
        u.payload_kind,
        u.heading_path,
        (SELECT string_agg(seg.value, ' > ' ORDER BY seg.ordinality)
           FROM jsonb_array_elements_text(u.heading_path)
                WITH ORDINALITY AS seg(value, ordinality)
        ) AS heading_path_text,
        u.title,
        u.order_index,
        u.semantic_key,{semantic_keys}
        u.payload,
        u.content_hash,
        u.structure_hash,
        u.quality_status,
        u.applicability,
        u.page_no,
        u.artifact_locator,
        u.created_at,
        'document_unit.v1'::text AS contract_version,
        d.company_id AS company_ref,
        d.security_id AS security_ref,
        s.security_code,
        s.exchange,
        COALESCE(d.class_filing_type, 'other') AS filing_type,
        d.class_disclosure_topics AS disclosure_topics,
        d.report_period,
        d.announcement_date,
        u.processing_run_id AS producer_action_ref,
        d.source_access_id AS source_ref,
        u.document_id AS parent_ref,
        'document_unit'::text AS asset_kind,
        u.created_at AS observed_at,
        CASE
            WHEN COALESCE(d.class_filing_type, 'other')
                 IN ('investor_relations','performance_briefing')
                THEN 'tier_0b'
            ELSE 'tier_0a'
        END AS source_tier,
        'G0'::text AS trace_level,
        d.raw_file_hash,
        u.query_projection_hash{content_categories}
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    JOIN {CORE_SCHEMA}.processing_run r
      ON r.processing_run_id = u.processing_run_id
    """
