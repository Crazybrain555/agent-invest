"""classification consults title_topic rules for coded documents too

Revision ID: 0021_title_topic_classification
Revises: 0020_tracked_pool_lifecycle
Create Date: 2026-07-12

Process-classes audit F3 (docs/agent/notes/process-classes-review-2026-07-12.md):
CNINFO files monthly operating data (销售简报/主要经营数据/发电量完成情况) under
F006V 012305 "经营环境重大变化" and never emits 010309, so the operating_data
class was dead vocabulary while its documents rode risk_alert. Fix: a new
rule_set 'title_topic' (loaded from filing_type_map.json topic_rules, priority
= the class's class_map priority) whose hits are UNIONed with code-rule hits —
for coded and code-less documents alike — before the derived_topics aggregate
and the derived_filing_type argmax. Existing 'title' rules stay a code-less
fallback only; topic rules are additive and can only ADD classes, never mask a
code hit (argmax may prefer them only by the same priority scale).
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
revision: str = "0021_title_topic_classification"
down_revision: Union[str, None] = "0020_tracked_pool_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.documents_v1")
    op.execute(_documents_view_sql(cls_lateral=_CLS_LATERAL_0021))
    op.execute(_document_units_view_sql(cls_lateral=_CLS_LATERAL_0021))
    _grant_views()


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.document_units_v1")
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.documents_v1")
    op.execute(_documents_view_sql(cls_lateral=_CLS_LATERAL_0017))
    op.execute(_document_units_view_sql(cls_lateral=_CLS_LATERAL_0017))
    _grant_views()


def _grant_views() -> None:
    for view in ("documents_v1", "document_units_v1"):
        op.execute(
            f"GRANT SELECT ON {PUBLIC_SCHEMA}.{view} TO "
            f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
        )


# 0021 derivation: class_hits = code-rule hits UNION title_topic hits (the
# union dedups on value+priority). derived_topics aggregates the union;
# derived_filing_type argmaxes the union, then falls back to code-less title
# rules, then 'other'. Facet split is untouched.
_CLS_LATERAL_0021 = """
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
        ),
        class_hits AS (
            SELECT cr.value, cr.priority
              FROM seg s
              JOIN {core}.classification_rule cr
                ON cr.rule_set = 'class' AND s.code LIKE cr.prefix || '%'
            UNION
            SELECT tt.value, tt.priority
              FROM {core}.classification_rule tt
             WHERE tt.rule_set = 'title_topic'
               AND d.title LIKE '%' || tt.prefix || '%'
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
                (SELECT ch.value
                   FROM class_hits ch
                  ORDER BY ch.priority DESC, ch.value
                  LIMIT 1),
                (SELECT tr.value
                   FROM {core}.classification_rule tr
                  WHERE tr.rule_set = 'title'
                    AND d.title LIKE '%' || tr.prefix || '%'
                  ORDER BY tr.priority DESC
                  LIMIT 1),
                'other') AS derived_filing_type,
            (SELECT jsonb_agg(DISTINCT ch.value) FROM class_hits ch)
              AS derived_topics
    ) cls ON true
"""


# 0017 form, for downgrade (code-rule hits only; title rules code-less fallback).
_CLS_LATERAL_0017 = """
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


def _documents_view_sql(*, cls_lateral: str) -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.documents_v1 AS
    SELECT
        d.document_id,
        d.provider,
        d.provider_document_id,
        s.security_code,
        s.exchange,
        cls.derived_filing_type AS filing_type,
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
    ) sb ON true{cls_lateral.format(core=CORE_SCHEMA)}
    """


def _document_units_view_sql(*, cls_lateral: str) -> str:
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
        cls.derived_filing_type AS filing_type,
        cls.derived_topics AS disclosure_topics,
        d.report_period,
        d.announcement_date,
        u.processing_run_id AS producer_action_ref,
        d.source_access_id AS source_ref,
        u.document_id AS parent_ref,
        'document_unit'::text AS asset_kind,
        u.created_at AS observed_at,
        CASE
            WHEN cls.derived_filing_type IN ('investor_relations','performance_briefing')
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
    JOIN {CORE_SCHEMA}.processing_run r ON r.processing_run_id = u.processing_run_id{cls_lateral.format(core=CORE_SCHEMA)}
    """
