"""canonical security keys and code-less-only title fallback

Revision ID: 0022_prescale_hardening
Revises: 0021_title_topic_classification
Create Date: 2026-07-13

The pre-scale audit found two boundaries that the 13-company corpus did not
exercise well:

* the exact-string security unique key allowed case/whitespace aliases;
* 0021's public-view title fallback ran for coded rows even though worker
  eligibility and the documented cascade reserve that fallback for code-less
  channels.  Narrow ``title_topic`` rules remain additive for both paths.

The public view column contracts are unchanged.
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
revision: str = "0022_prescale_hardening"
down_revision: Union[str, None] = "0021_title_topic_classification"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PYTHON_STRIP_CHARS_SQL = (
    r"U&' \0009\000A\000B\000C\000D\001C\001D\001E\001F"
    r"\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005"
    r"\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000'"
)
_CODE_TRIM_SQL = f"btrim(security_code, {_PYTHON_STRIP_CHARS_SQL})"
_EXCHANGE_TRIM_SQL = f"btrim(exchange, {_PYTHON_STRIP_CHARS_SQL})"


def upgrade() -> None:
    op.create_check_constraint(
        "ck_security_code_canonical",
        "security",
        f"security_code = {_CODE_TRIM_SQL}",
        schema=CORE_SCHEMA,
    )
    op.create_check_constraint(
        "ck_security_exchange_canonical",
        "security",
        f"exchange = upper({_EXCHANGE_TRIM_SQL})",
        schema=CORE_SCHEMA,
    )
    op.create_check_constraint(
        "ck_security_mainland_exchange_code",
        "security",
        "exchange NOT IN ('SSE', 'SZSE', 'BSE') OR ("
        "security_code ~ '^[0-9]{6}$' AND CASE "
        "WHEN security_code LIKE '92%' OR security_code LIKE '4%' "
        "  OR security_code LIKE '8%' THEN exchange = 'BSE' "
        "WHEN security_code LIKE '6%' OR security_code LIKE '9%' "
        "  THEN exchange = 'SSE' "
        "WHEN security_code LIKE '0%' OR security_code LIKE '2%' "
        "  OR security_code LIKE '3%' THEN exchange = 'SZSE' "
        "ELSE false END)",
        schema=CORE_SCHEMA,
    )
    op.execute(_documents_view_sql(cls_lateral=_CLS_LATERAL_0022))
    op.execute(_document_units_view_sql(cls_lateral=_CLS_LATERAL_0022))
    _grant_views()


def downgrade() -> None:
    op.execute(_documents_view_sql(cls_lateral=_CLS_LATERAL_0021))
    op.execute(_document_units_view_sql(cls_lateral=_CLS_LATERAL_0021))
    _grant_views()
    op.drop_constraint(
        "ck_security_mainland_exchange_code",
        "security",
        schema=CORE_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "ck_security_exchange_canonical",
        "security",
        schema=CORE_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "ck_security_code_canonical",
        "security",
        schema=CORE_SCHEMA,
        type_="check",
    )


def _grant_views() -> None:
    for view in ("documents_v1", "document_units_v1"):
        op.execute(
            f"GRANT SELECT ON {PUBLIC_SCHEMA}.{view} TO "
            f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
        )


_CLS_LATERAL_0022 = """
    LEFT JOIN LATERAL (
        WITH seg AS (
            SELECT btrim(u_seg.code) AS code,
                   pc.category_name,
                   (SELECT fr.value
                      FROM {core}.classification_rule fr
                     WHERE fr.rule_set = 'facet'
                       AND btrim(u_seg.code) LIKE fr.prefix || '%'
                     ORDER BY fr.priority DESC
                     LIMIT 1) AS facet
              FROM unnest(string_to_array(d.provider_metadata->>'raw_category', '||'))
                   AS u_seg(code)
              LEFT JOIN {core}.provider_category pc
                ON pc.provider = d.provider
               AND pc.category_code = btrim(u_seg.code)
             WHERE NULLIF(btrim(u_seg.code), '') IS NOT NULL
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
                  WHERE NULLIF(btrim(d.provider_metadata->>'raw_category'), '') IS NULL
                    AND tr.rule_set = 'title'
                    AND d.title LIKE '%' || tr.prefix || '%'
                  ORDER BY tr.priority DESC
                  LIMIT 1),
                'other') AS derived_filing_type,
            (SELECT jsonb_agg(DISTINCT ch.value) FROM class_hits ch)
              AS derived_topics
    ) cls ON true
"""


# Frozen 0021 behavior for downgrade: broad title fallback after code/topic miss.
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


def _documents_view_sql(*, cls_lateral: str) -> str:
    return f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.documents_v1 AS
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
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.document_units_v1 AS
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
    JOIN {CORE_SCHEMA}.processing_run r
      ON r.processing_run_id = u.processing_run_id{cls_lateral.format(core=CORE_SCHEMA)}
    """
