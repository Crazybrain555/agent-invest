"""Converge the Unit public contract onto one clean v1 view.

Revision ID: 0039_single_unit_view
Revises: 0038_unit_contract_v2
Create Date: 2026-08-19

The service is still pre-production and has no reason to carry two public Unit
contracts.  Keep the clean 0038 v2 column semantics under the canonical v1
name: ``body_status`` is Unit-owned and ``content_categories`` remains a
Document-only provider facet.  Remove ``document_units_v2`` rather than
leaving an alias or a second serializer.
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


revision: str = "0039_single_unit_view"
down_revision: Union[str, None] = "0038_unit_contract_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_V1_VIEW = "document_units_v1"
_V2_VIEW = "document_units_v2"


def upgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{_V2_VIEW}")
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_V1_VIEW}")
    op.execute(
        _document_units_view_sql(
            view_name=_V1_VIEW,
            contract_version="document_unit.v1",
            include_content_categories=False,
            include_body_status=True,
        )
    )
    _grant_views((_V1_VIEW,))


def downgrade() -> None:
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_V1_VIEW}")
    op.execute(
        _document_units_view_sql(
            view_name=_V1_VIEW,
            contract_version="document_unit.v1",
            include_content_categories=True,
            include_body_status=False,
        )
    )
    op.execute(
        _document_units_view_sql(
            view_name=_V2_VIEW,
            contract_version="document_unit.v2",
            include_content_categories=False,
            include_body_status=True,
        )
    )
    _grant_views((_V1_VIEW, _V2_VIEW))


def _grant_views(view_names: tuple[str, ...]) -> None:
    for view_name in view_names:
        op.execute(
            f"GRANT SELECT ON {PUBLIC_SCHEMA}.{view_name} TO "
            f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
        )


def _document_units_view_sql(
    *,
    view_name: str,
    contract_version: str,
    include_content_categories: bool,
    include_body_status: bool,
) -> str:
    if view_name not in {_V1_VIEW, _V2_VIEW}:
        raise ValueError("unsupported document Unit public view")
    if contract_version not in {"document_unit.v1", "document_unit.v2"}:
        raise ValueError("unsupported document Unit public contract version")
    if include_content_categories == include_body_status:
        raise ValueError(
            "Unit view must expose exactly one terminal compatibility field"
        )

    content_categories = (
        ",\n        d.class_content_categories AS content_categories"
        if include_content_categories
        else ""
    )
    body_status = (
        """
        ,CASE
            WHEN u.payload_kind = 'text'
                 AND u.payload = '{"text": ""}'::jsonb
                 AND u.title IS NOT NULL
                THEN 'heading_only'
            WHEN u.payload_kind = 'text'
                 AND u.payload = '{"text": ""}'::jsonb
                THEN 'empty'
            ELSE 'content'
        END AS body_status"""
        if include_body_status
        else ""
    )
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.{view_name} AS
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
        u.semantic_key,
        u.semantic_keys,
        u.section_keys,
        u.payload,
        u.content_hash,
        u.structure_hash,
        u.quality_status,
        u.applicability,
        u.page_no,
        u.artifact_locator,
        u.created_at,
        '{contract_version}'::text AS contract_version,
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
        u.query_projection_hash{content_categories}{body_status}
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    JOIN {CORE_SCHEMA}.processing_run r
      ON r.processing_run_id = u.processing_run_id
    """
