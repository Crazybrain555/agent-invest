"""heading_path_text derived breadcrumb column on document_units_v1

Revision ID: 0015_heading_path_text
Revises: 0014_disclosure_topics
Create Date: 2026-07-07
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    PUBLIC_SCHEMA,
    READER_ROLE,
)

# revision identifiers, used by Alembic.
revision: str = "0015_heading_path_text"
down_revision: Union[str, None] = "0014_disclosure_topics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Round10 user ruling on 多级标题: heading_path (1-4 level array) stays the
    # structured multi-level title; this derived breadcrumb ("第八节 财务报告 >
    # 七、合并财务报表项目注释 > 75、其他综合收益") makes it keyword-greppable
    # today, and is the same field the 06R projection will index (U7 family).
    # Derived in the view — no storage, no hash impact.
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(_document_units_view_sql(with_breadcrumb=True))
    _grant_view()


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(_document_units_view_sql(with_breadcrumb=False))
    _grant_view()


def _grant_view() -> None:
    op.execute(
        f"GRANT SELECT ON {PUBLIC_SCHEMA}.document_units_v1 TO "
        f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )


def _document_units_view_sql(*, with_breadcrumb: bool) -> str:
    extra = (
        "\n        (SELECT string_agg(seg.value, ' > ' ORDER BY seg.ordinality)\n"
        "           FROM jsonb_array_elements_text(u.heading_path)\n"
        "                WITH ORDINALITY AS seg(value, ordinality)\n"
        "        ) AS heading_path_text,"
        if with_breadcrumb
        else ""
    )
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.document_units_v1 AS
    SELECT
        u.asset_id,
        u.document_id,
        u.processing_run_id,
        COALESCE(r.is_active, false) AS is_active_run,
        u.provider_document_id,
        u.payload_kind,
        u.heading_path,{extra}
        u.title,
        u.order_index,
        u.semantic_key,
        u.semantic_keys,
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
        d.filing_type,
        d.disclosure_topics,
        d.report_period,
        d.announcement_date,
        u.processing_run_id AS producer_action_ref,
        d.source_access_id AS source_ref,
        u.document_id AS parent_ref,
        'document_unit'::text AS asset_kind,
        u.created_at AS observed_at,
        CASE
            WHEN d.filing_type IN ('investor_relations','performance_briefing')
                THEN 'tier_0b'
            ELSE 'tier_0a'
        END AS source_tier,
        'G0'::text AS trace_level,
        d.raw_file_hash,
        u.query_projection_hash
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    JOIN {CORE_SCHEMA}.processing_run r ON r.processing_run_id = u.processing_run_id
    """
