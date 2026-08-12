"""Remove duplicate Unit labels and document-level facet copies.

Revision ID: 0033_unit_schema_convergence
Revises: 0032_provider_document_output
Create Date: 2026-08-12

The scalar ``semantic_key`` remains nullable because the L1 protocol exposes
it as an optional Unit scope key.  The local ``semantic_keys`` array carried
the same single placeholder and is retired.  Classification facets remain on
``documents_v1`` and ``document_categories_v1``; they are no longer repeated
on every Unit row.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    PUBLIC_SCHEMA,
    READER_ROLE,
)

revision: str = "0033_unit_schema_convergence"
down_revision: Union[str, None] = "0032_provider_document_output"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VIEW = "document_units_v1"
_SEMANTIC_KEYS_INDEX = "ix_document_unit_semantic_keys"


def upgrade() -> None:
    independent_plural_values = op.get_bind().execute(
        sa.text(
            f"""
            SELECT count(*)
            FROM {CORE_SCHEMA}.document_unit
            WHERE semantic_keys IS NOT NULL
              AND (
                  semantic_key IS NULL
                  OR semantic_keys <> to_jsonb(ARRAY[semantic_key])
              )
            """
        )
    ).scalar_one()
    if independent_plural_values:
        raise RuntimeError(
            "0033_unit_schema_convergence refuses to drop semantic_keys "
            "while it contains information not present in semantic_key"
        )
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_VIEW}")
    op.execute(f"DROP INDEX IF EXISTS {CORE_SCHEMA}.{_SEMANTIC_KEYS_INDEX}")
    op.drop_column("document_unit", "semantic_keys", schema=CORE_SCHEMA)
    op.execute(_document_units_view_sql(include_legacy_duplicates=False))
    _grant_view()


def downgrade() -> None:
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_VIEW}")
    op.add_column(
        "document_unit",
        sa.Column(
            "semantic_keys",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
        schema=CORE_SCHEMA,
    )
    op.execute(
        f"CREATE INDEX {_SEMANTIC_KEYS_INDEX} ON {CORE_SCHEMA}.document_unit "
        "USING gin (semantic_keys) WHERE semantic_keys IS NOT NULL"
    )
    op.execute(_document_units_view_sql(include_legacy_duplicates=True))
    _grant_view()


def _grant_view() -> None:
    op.execute(
        f"GRANT SELECT ON {PUBLIC_SCHEMA}.{_VIEW} TO "
        f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )


def _document_units_view_sql(*, include_legacy_duplicates: bool) -> str:
    semantic_keys = "\n        u.semantic_keys," if include_legacy_duplicates else ""
    facets = (
        ",\n        d.class_publisher_categories AS publisher_categories,"
        "\n        d.class_market AS market,"
        "\n        d.class_content_categories AS content_categories"
        if include_legacy_duplicates
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
        u.query_projection_hash{facets}
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    JOIN {CORE_SCHEMA}.processing_run r
      ON r.processing_run_id = u.processing_run_id
    """
