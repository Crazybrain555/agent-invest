"""classification_rule table + derived classification/facet view columns

Revision ID: 0016_classification_rules
Revises: 0015_heading_path_text
Create Date: 2026-07-08

Design: docs/implementation/design/classification-facets-and-derived-views.md.
User ruling: document keeps provider facts; derived classifications move to
the views. One class map, two outputs — disclosure_topics = full hit set,
filing_type = highest-priority hit (COALESCE to the registration-time
fallback for channels without F006V codes). Three fact-facet columns
(publisher/market/content) decompose the raw code string mechanically via
cninfo's own tree. The materialized document.disclosure_topics column (0014)
is dropped; vocabulary upgrades become `make load-rules` with no stale rows.
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
revision: str = "0016_classification_rules"
down_revision: Union[str, None] = "0015_heading_path_text"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE {CORE_SCHEMA}.classification_rule (
            rule_set varchar(16) NOT NULL,
            prefix   varchar(32) NOT NULL,
            value    varchar(48) NOT NULL,
            priority integer NOT NULL DEFAULT 0,
            version  varchar(32) NOT NULL,
            PRIMARY KEY (rule_set, prefix, value)
        )
        """
    )
    op.execute(
        f"GRANT SELECT ON {CORE_SCHEMA}.classification_rule TO "
        f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )
    op.execute(
        f"GRANT INSERT, DELETE, TRUNCATE ON {CORE_SCHEMA}.classification_rule TO {APP_ROLE}"
    )
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.documents_v1")
    op.execute(_documents_view_sql(with_facets=True))
    op.execute(_document_units_view_sql(with_facets=True))
    _grant_views()
    op.execute(f"DROP INDEX IF EXISTS {CORE_SCHEMA}.ix_document_disclosure_topics")
    op.execute(f"ALTER TABLE {CORE_SCHEMA}.document DROP COLUMN disclosure_topics")


def downgrade() -> None:
    # Downgrade restores the 0015 shapes; materialized topics come back empty
    # (they were derivable data — re-run the old backfill if ever needed).
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.document ADD COLUMN disclosure_topics jsonb NULL"
    )
    op.execute(
        f"CREATE INDEX ix_document_disclosure_topics ON {CORE_SCHEMA}.document "
        "USING gin (disclosure_topics) WHERE disclosure_topics IS NOT NULL"
    )
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.documents_v1")
    op.execute(_documents_view_sql(with_facets=False))
    op.execute(_document_units_view_sql(with_facets=False))
    _grant_views()
    op.execute(f"DROP TABLE {CORE_SCHEMA}.classification_rule")


def _grant_views() -> None:
    for view in ("documents_v1", "document_units_v1"):
        op.execute(
            f"GRANT SELECT ON {PUBLIC_SCHEMA}.{view} TO "
            f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
        )


# One class map, two outputs: topics = every class whose prefix matches any
# segment; filing_type = the highest-priority such class. Facet rules split
# the segments into publisher/market/content by longest-priority match.
_CLS_LATERAL = """
    LEFT JOIN LATERAL (
        WITH seg AS (
            SELECT u_seg.code,
                   pc.category_name,
                   (SELECT fr.value
                      FROM {core}.classification_rule fr
                     WHERE fr.rule_set = 'facet'
                       AND u_seg.code LIKE fr.prefix || '%'
                     ORDER BY fr.priority DESC
                     LIMIT 1) AS facet
              FROM unnest(string_to_array(d.provider_metadata->>'raw_category', '||'))
                   AS u_seg(code)
              LEFT JOIN {core}.provider_category pc ON pc.category_code = u_seg.code
        )
        SELECT
            (SELECT jsonb_agg(jsonb_build_object('code', s.code, 'name', s.category_name)
                              ORDER BY s.code)
               FROM seg s WHERE s.facet = 'publisher') AS publisher_categories,
            (SELECT min(s.category_name) FROM seg s WHERE s.facet = 'market') AS market,
            (SELECT jsonb_agg(jsonb_build_object('code', s.code, 'name', s.category_name)
                              ORDER BY s.code)
               FROM seg s WHERE s.facet IS NULL) AS content_categories,
            (SELECT cr.value
               FROM seg s
               JOIN {core}.classification_rule cr
                 ON cr.rule_set = 'class' AND s.code LIKE cr.prefix || '%'
              ORDER BY cr.priority DESC, cr.value
              LIMIT 1) AS derived_filing_type,
            (SELECT jsonb_agg(DISTINCT cr.value)
               FROM seg s
               JOIN {core}.classification_rule cr
                 ON cr.rule_set = 'class' AND s.code LIKE cr.prefix || '%')
              AS derived_topics
    ) cls ON true
"""


def _documents_view_sql(*, with_facets: bool) -> str:
    if with_facets:
        filing = "COALESCE(cls.derived_filing_type, d.filing_type) AS filing_type,"
        topics = "\n        cls.derived_topics AS disclosure_topics,"
        facet_cols = (
            ",\n        cls.publisher_categories,"
            "\n        cls.market,"
            "\n        cls.content_categories"
        )
        lateral = _CLS_LATERAL.format(core=CORE_SCHEMA)
    else:
        filing = "d.filing_type,"
        topics = "\n        d.disclosure_topics,"
        facet_cols = ""
        lateral = ""
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.documents_v1 AS
    SELECT
        d.document_id,
        d.provider,
        d.provider_document_id,
        s.security_code,
        s.exchange,
        {filing}{topics}
        d.title,
        d.announcement_date,
        d.report_period,
        d.raw_file_hash,
        d.status,
        d.current_processing_run_id,
        d.created_at,
        d.updated_at,
        'document.v1'::text AS contract_version,
        d.company_id AS company_ref,
        d.security_id AS security_ref,
        d.source_access_id AS source_ref,
        d.supersedes_document_id,
        d.correction_of_document_id,
        sb.document_id AS superseded_by_document_id,
        d.provider_metadata{facet_cols}
    FROM {CORE_SCHEMA}.document d
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    LEFT JOIN LATERAL (
        SELECT x.document_id
          FROM {CORE_SCHEMA}.document x
         WHERE x.supersedes_document_id = d.document_id
         ORDER BY x.created_at DESC, x.document_id DESC
         LIMIT 1
    ) sb ON true{lateral}
    """


def _document_units_view_sql(*, with_facets: bool) -> str:
    if with_facets:
        filing = "COALESCE(cls.derived_filing_type, d.filing_type) AS filing_type,"
        topics = "\n        cls.derived_topics AS disclosure_topics,"
        facet_cols = (
            ",\n        cls.publisher_categories,"
            "\n        cls.market,"
            "\n        cls.content_categories"
        )
        tier_expr = "COALESCE(cls.derived_filing_type, d.filing_type)"
        lateral = _CLS_LATERAL.format(core=CORE_SCHEMA)
    else:
        filing = "d.filing_type,"
        topics = "\n        d.disclosure_topics,"
        facet_cols = ""
        tier_expr = "d.filing_type"
        lateral = ""
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.document_units_v1 AS
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
        {filing}{topics}
        d.report_period,
        d.announcement_date,
        u.processing_run_id AS producer_action_ref,
        d.source_access_id AS source_ref,
        u.document_id AS parent_ref,
        'document_unit'::text AS asset_kind,
        u.created_at AS observed_at,
        CASE
            WHEN {tier_expr} IN ('investor_relations','performance_briefing')
                THEN 'tier_0b'
            ELSE 'tier_0a'
        END AS source_tier,
        'G0'::text AS trace_level,
        d.raw_file_hash,
        u.query_projection_hash{facet_cols}
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    JOIN {CORE_SCHEMA}.processing_run r ON r.processing_run_id = u.processing_run_id{lateral}
    """
