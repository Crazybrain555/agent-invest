"""Project root breadcrumbs and the current hierarchy capability honestly.

Revision ID: 0032_root_heading_path_text
Revises: 0031_artifact_owner_run
Create Date: 2026-08-07

``heading_path=[]`` is valid document-root content.  PostgreSQL
``string_agg`` returns NULL for that empty input while the search projection
already publishes ``""``.  This migration closes the public-view drift without
inventing a title or changing the heading path itself.
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    CORE_SCHEMA,
    PUBLIC_SCHEMA,
    READ_ONLY_PUBLIC_ROLES,
)


revision: str = "0032_root_heading_path_text"
down_revision: Union[str, None] = "0031_artifact_owner_run"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        _document_units_view_sql(
            root_empty_string=True,
            include_hierarchy_status=True,
        )
    )
    op.execute(_source_refs_view_sql(include_hierarchy_status=True))
    op.execute(_document_outline_view_sql(include_hierarchy_status=True))


def downgrade() -> None:
    # CREATE OR REPLACE cannot remove a view column.  These three public views
    # have no view-on-view dependency, so recreate their prior exact contracts
    # and restore grants explicitly.
    for view in ("document_outline_v1", "source_refs_v1", "document_units_v1"):
        op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{view}")
    op.execute(
        _document_units_view_sql(
            root_empty_string=False,
            include_hierarchy_status=False,
        )
    )
    op.execute(_source_refs_view_sql(include_hierarchy_status=False))
    op.execute(_document_outline_view_sql(include_hierarchy_status=False))
    for role in READ_ONLY_PUBLIC_ROLES:
        for view in (
            "document_units_v1",
            "source_refs_v1",
            "document_outline_v1",
        ):
            op.execute(f"GRANT SELECT ON {PUBLIC_SCHEMA}.{view} TO {role}")


def _document_units_view_sql(
    *,
    root_empty_string: bool,
    include_hierarchy_status: bool = True,
) -> str:
    breadcrumb = """
        (SELECT string_agg(seg.value, ' > ' ORDER BY seg.ordinality)
           FROM jsonb_array_elements_text(u.heading_path)
                WITH ORDINALITY AS seg(value, ordinality)
        )
    """.strip()
    if root_empty_string:
        breadcrumb = f"COALESCE({breadcrumb}, ''::text)"
    hierarchy_status = (
        ",\n        'flattened_unresolved'::text AS hierarchy_status"
        if include_hierarchy_status
        else ""
    )
    return f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.document_units_v1 AS
    SELECT
        u.asset_id,
        u.document_id,
        u.processing_run_id,
        COALESCE(r.is_active, false) AS is_active_run,
        u.provider_document_id,
        u.payload_kind,
        u.heading_path,
        {breadcrumb} AS heading_path_text,
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
        u.query_projection_hash,
        d.class_publisher_categories AS publisher_categories,
        d.class_market AS market,
        d.class_content_categories AS content_categories{hierarchy_status}
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    JOIN {CORE_SCHEMA}.processing_run r
      ON r.processing_run_id = u.processing_run_id
    """


def _source_refs_view_sql(*, include_hierarchy_status: bool) -> str:
    hierarchy_status = (
        ",\n        'flattened_unresolved'::text AS hierarchy_status"
        if include_hierarchy_status
        else ""
    )
    return f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.source_refs_v1 AS
    SELECT
        'disclosure_anchor'::text AS service,
        'source_ref.v1'::text AS contract_version,
        u.asset_id,
        d.source_access_id,
        u.document_id,
        d.provider,
        d.provider_document_id,
        d.raw_file_hash,
        u.processing_run_id,
        COALESCE(r.is_active, false) AS is_active_run,
        u.payload_kind,
        u.heading_path,
        u.title,
        u.content_hash AS unit_content_hash,
        u.quality_status,
        u.applicability,
        u.page_no,
        u.artifact_locator{hierarchy_status}
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    JOIN {CORE_SCHEMA}.processing_run r
      ON r.processing_run_id = u.processing_run_id
    """


def _document_outline_view_sql(*, include_hierarchy_status: bool) -> str:
    hierarchy_status = (
        ",\n               'flattened_unresolved'::text AS hierarchy_status"
        if include_hierarchy_status
        else ""
    )
    return f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.document_outline_v1 AS
    SELECT u.document_id,
           u.heading_path AS path,
           jsonb_array_length(u.heading_path) AS depth,
           count(*) AS unit_count,
           count(*) FILTER (WHERE u.payload_kind = 'table') AS table_count,
           count(*) FILTER (WHERE u.payload_kind = 'image') AS image_count,
           array_agg(DISTINCT u.semantic_key)
               FILTER (WHERE u.semantic_key IS NOT NULL) AS semantic_keys,
           min(u.page_no) AS page_from,
           max(u.page_no) AS page_to,
           min(u.order_index) AS first_order_index,
           'document_outline.v1'::text AS contract_version{hierarchy_status}
      FROM {CORE_SCHEMA}.document_unit u
      JOIN {CORE_SCHEMA}.processing_run pr
        ON pr.processing_run_id = u.processing_run_id
     WHERE pr.is_active
     GROUP BY u.document_id, u.heading_path
    """


__all__ = [
    "branch_labels",
    "depends_on",
    "down_revision",
    "downgrade",
    "revision",
    "upgrade",
]
