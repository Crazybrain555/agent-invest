"""filing_type fully view-derived; drop the materialized column

Revision ID: 0017_filing_type_derived
Revises: 0016_classification_rules
Create Date: 2026-07-08

User ruling: the document table holds provider facts only — a rules-derived
filing_type value in the table is not a fact (and served zero rows: every
current document carries F006V codes). The view now derives filing_type as
COALESCE(code-rule argmax, title-rule argmax); title keyword rules live in
classification_rule (rule_set='title', loaded from filing_type_map.json, file
order = priority so 半年度报告 shadows 年度报告). Code-less channels classify
via the stored title — still zero materialized judgment.
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
revision: str = "0017_filing_type_derived"
down_revision: Union[str, None] = "0016_classification_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.documents_v1")
    op.execute(_documents_view_sql(column_fallback=False))
    op.execute(_document_units_view_sql(column_fallback=False))
    _grant_views()
    # DROP COLUMN cascades the 0007 composite index; recreate without the
    # derived column.
    op.execute(f"ALTER TABLE {CORE_SCHEMA}.document DROP COLUMN filing_type")
    op.execute(
        f"CREATE INDEX ix_document_company_period ON {CORE_SCHEMA}.document "
        "(company_id, report_period)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {CORE_SCHEMA}.ix_document_company_period")
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.document ADD COLUMN filing_type varchar(64) NULL"
    )
    op.execute(
        f"CREATE INDEX ix_document_company_period_type ON {CORE_SCHEMA}.document "
        "(company_id, report_period, filing_type)"
    )
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.documents_v1")
    op.execute(_documents_view_sql(column_fallback=True))
    op.execute(_document_units_view_sql(column_fallback=True))
    _grant_views()


def _grant_views() -> None:
    for view in ("documents_v1", "document_units_v1"):
        op.execute(
            f"GRANT SELECT ON {PUBLIC_SCHEMA}.{view} TO "
            f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
        )


# One class map, two outputs (0016) + title-rule fallback for code-less
# channels (0017). derived_filing_type: code-rule argmax wins; else the
# highest-priority title keyword rule (LIKE with embedded % for all-match).
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
            COALESCE(
                (SELECT cr.value
                   FROM seg s
                   JOIN {core}.classification_rule cr
                     ON cr.rule_set = 'class' AND s.code LIKE cr.prefix || '%'
                  ORDER BY cr.priority DESC, cr.value
                  LIMIT 1),
                (SELECT tr.value
                   FROM {core}.classification_rule tr
                  WHERE tr.rule_set = 'title'
                    AND d.title LIKE '%' || tr.prefix || '%'
                  ORDER BY tr.priority DESC
                  LIMIT 1),
                'other') AS derived_filing_type,
            (SELECT jsonb_agg(DISTINCT cr.value)
               FROM seg s
               JOIN {core}.classification_rule cr
                 ON cr.rule_set = 'class' AND s.code LIKE cr.prefix || '%')
              AS derived_topics
    ) cls ON true
"""


def _filing_expr(column_fallback: bool) -> str:
    if column_fallback:
        return "COALESCE(cls.derived_filing_type, d.filing_type)"
    return "cls.derived_filing_type"


def _documents_view_sql(*, column_fallback: bool) -> str:
    filing = f"{_filing_expr(column_fallback)} AS filing_type,"
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.documents_v1 AS
    SELECT
        d.document_id,
        d.provider,
        d.provider_document_id,
        s.security_code,
        s.exchange,
        {filing}
        cls.derived_topics AS disclosure_topics,
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
        d.provider_metadata,
        cls.publisher_categories,
        cls.market,
        cls.content_categories
    FROM {CORE_SCHEMA}.document d
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    LEFT JOIN LATERAL (
        SELECT x.document_id
          FROM {CORE_SCHEMA}.document x
         WHERE x.supersedes_document_id = d.document_id
         ORDER BY x.created_at DESC, x.document_id DESC
         LIMIT 1
    ) sb ON true{_CLS_LATERAL.format(core=CORE_SCHEMA)}
    """


def _document_units_view_sql(*, column_fallback: bool) -> str:
    filing_expr = _filing_expr(column_fallback)
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
        {filing_expr} AS filing_type,
        cls.derived_topics AS disclosure_topics,
        d.report_period,
        d.announcement_date,
        u.processing_run_id AS producer_action_ref,
        d.source_access_id AS source_ref,
        u.document_id AS parent_ref,
        'document_unit'::text AS asset_kind,
        u.created_at AS observed_at,
        CASE
            WHEN {filing_expr} IN ('investor_relations','performance_briefing')
                THEN 'tier_0b'
            ELSE 'tier_0a'
        END AS source_tier,
        'G0'::text AS trace_level,
        d.raw_file_hash,
        u.query_projection_hash,
        cls.publisher_categories,
        cls.market,
        cls.content_categories
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    JOIN {CORE_SCHEMA}.processing_run r ON r.processing_run_id = u.processing_run_id{_CLS_LATERAL.format(core=CORE_SCHEMA)}
    """
