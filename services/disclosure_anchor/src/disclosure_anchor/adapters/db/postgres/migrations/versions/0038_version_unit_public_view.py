"""Version the Unit public view after removing a v1 field.

Revision ID: 0038_unit_contract_v2
Revises: 0037_unit_facets
Create Date: 2026-08-15

0037 removed ``content_categories`` from ``document_units_v1`` during
pre-production cleanup.  Field deletion is nevertheless a breaking public
contract change.  Restore the deprecated v1 projection and expose the clean
v2 shape with a Unit-owned ``body_status`` field.  The provider facet remains
a Document fact; v1 only joins it for compatibility and never stores it on a
Unit. Downgrade removes v2 but deliberately preserves the restored v1 shape;
the known-broken 39-column v1 must never be re-exposed.
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


revision: str = "0038_unit_contract_v2"
down_revision: Union[str, None] = "0037_unit_facets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_V1_VIEW = "document_units_v1"
_V2_VIEW = "document_units_v2"


def upgrade() -> None:
    op.execute(
        _document_units_view_sql(
            view_name=_V1_VIEW,
            contract_version="document_unit.v1",
            include_content_categories=True,
            replace_existing=True,
        )
    )
    op.execute(
        _document_units_view_sql(
            view_name=_V2_VIEW,
            contract_version="document_unit.v2",
            include_content_categories=False,
        )
    )
    _grant_views((_V1_VIEW, _V2_VIEW))


def downgrade() -> None:
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_V2_VIEW}")
    # Deliberately keep the restored 40-column v1 view. Recreating the
    # 39-column shape emitted by 0037 would repeat the public-contract break
    # that this corrective migration exists to close. A deeper downgrade
    # remains possible because 0037's own downgrade recreates its predecessor.


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
    replace_existing: bool = False,
) -> str:
    if view_name not in {_V1_VIEW, _V2_VIEW}:
        raise ValueError("unsupported document Unit public view")
    if contract_version not in {"document_unit.v1", "document_unit.v2"}:
        raise ValueError("unsupported document Unit public contract version")
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
        if contract_version == "document_unit.v2"
        else ""
    )
    create = "CREATE OR REPLACE VIEW" if replace_existing else "CREATE VIEW"
    return f"""
    {create} {PUBLIC_SCHEMA}.{view_name} AS
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
