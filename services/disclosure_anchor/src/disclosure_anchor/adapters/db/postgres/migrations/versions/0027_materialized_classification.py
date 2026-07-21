"""Materialized document classification + retrieval-scale indexes.

Revises the 0017 read-time-only classification decision (design:
docs/implementation/design/retrieval-scale-hardening.md §3): the 0022 view
LATERAL recomputed filing_type/topics/facets per row per read, so every
filtered list and the latest-filings endpoint scanned and classified the
whole corpus (measured 27s/page at 8.5k documents). The five classification
outputs become document columns, stamped with the classification_rule
content version; the loader refreshes stale rows on every rules reload so
the "edit JSON -> load-rules -> current everywhere" contract holds.

Public view column contracts are unchanged — same names, types, and NULL
semantics — only the derivation moves from LATERAL to materialized columns.
The classification SQL here is a frozen snapshot of
adapters/db/postgres/classification_refresh.py at 0027 time.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    CORE_SCHEMA,
    PUBLIC_SCHEMA,
)

# revision identifiers, used by Alembic.
revision: str = "0027_materialized_classification"
down_revision: Union[str, None] = "0026_document_outline_view"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CLS_EXPRESSION_SNAPSHOT = f"""
    WITH seg AS (
        SELECT btrim(u_seg.code) AS code,
               pc.category_name,
               (SELECT fr.value
                  FROM {CORE_SCHEMA}.classification_rule fr
                 WHERE fr.rule_set = 'facet'
                   AND btrim(u_seg.code) LIKE fr.prefix || '%'
                 ORDER BY fr.priority DESC
                 LIMIT 1) AS facet
          FROM unnest(string_to_array(d.provider_metadata->>'raw_category', '||'))
               AS u_seg(code)
          LEFT JOIN {CORE_SCHEMA}.provider_category pc
            ON pc.provider = d.provider
           AND pc.category_code = btrim(u_seg.code)
         WHERE NULLIF(btrim(u_seg.code), '') IS NOT NULL
    ),
    class_hits AS (
        SELECT cr.value, cr.priority
          FROM seg s
          JOIN {CORE_SCHEMA}.classification_rule cr
            ON cr.rule_set = 'class' AND s.code LIKE cr.prefix || '%'
        UNION
        SELECT tt.value, tt.priority
          FROM {CORE_SCHEMA}.classification_rule tt
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
               FROM {CORE_SCHEMA}.classification_rule tr
              WHERE NULLIF(btrim(d.provider_metadata->>'raw_category'), '') IS NULL
                AND tr.rule_set = 'title'
                AND d.title LIKE '%' || tr.prefix || '%'
              ORDER BY tr.priority DESC
              LIMIT 1),
            'other') AS derived_filing_type,
        (SELECT jsonb_agg(DISTINCT ch.value) FROM class_hits ch) AS derived_topics
"""

_BACKFILL_BATCH_SQL = f"""
UPDATE {CORE_SCHEMA}.document AS tgt
   SET class_filing_type = cls.derived_filing_type,
       class_disclosure_topics = cls.derived_topics,
       class_publisher_categories = cls.publisher_categories,
       class_market = cls.market,
       class_content_categories = cls.content_categories,
       class_rules_version = :stamp
  FROM (SELECT s.document_id, s.provider, s.title, s.provider_metadata
          FROM {CORE_SCHEMA}.document s
         WHERE s.class_rules_version IS NULL
         ORDER BY s.document_id
         LIMIT :batch) d,
       LATERAL ({_CLS_EXPRESSION_SNAPSHOT}) cls
 WHERE tgt.document_id = d.document_id
"""

_STAMP_SQL = f"""
SELECT COALESCE(
    (SELECT string_agg(rule_set || ':' || version, '|' ORDER BY rule_set)
       FROM (SELECT DISTINCT rule_set, version
               FROM {CORE_SCHEMA}.classification_rule) v),
    'empty')
"""

# 0022 LATERAL, frozen for downgrade view restoration.
_CLS_LATERAL_0022 = f"""
    LEFT JOIN LATERAL ({_CLS_EXPRESSION_SNAPSHOT}) cls ON true
"""

_DOCUMENT_COLUMNS_COMMON = """
        d.document_id,
        d.provider,
        d.provider_document_id,
        s.security_code,
        s.exchange,
        {filing_type_expr} AS filing_type,
        {topics_expr} AS disclosure_topics,
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
        {publisher_expr} AS publisher_categories,
        {market_expr} AS market,
        {content_expr} AS content_categories
"""


def _documents_view_sql(*, materialized: bool) -> str:
    if materialized:
        exprs = {
            "filing_type_expr": "COALESCE(d.class_filing_type, 'other')",
            "topics_expr": "d.class_disclosure_topics",
            "publisher_expr": "d.class_publisher_categories",
            "market_expr": "d.class_market",
            "content_expr": "d.class_content_categories",
        }
        cls_join = ""
    else:
        exprs = {
            "filing_type_expr": "cls.derived_filing_type",
            "topics_expr": "cls.derived_topics",
            "publisher_expr": "cls.publisher_categories",
            "market_expr": "cls.market",
            "content_expr": "cls.content_categories",
        }
        cls_join = _CLS_LATERAL_0022
    columns = _DOCUMENT_COLUMNS_COMMON.format(**exprs)
    return f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.documents_v1 AS
    SELECT
{columns}
    FROM {CORE_SCHEMA}.document d
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    LEFT JOIN LATERAL (
        SELECT x.document_id
          FROM {CORE_SCHEMA}.document x
         WHERE x.supersedes_document_id = d.document_id
         ORDER BY x.created_at DESC, x.document_id DESC
         LIMIT 1
    ) sb ON true{cls_join}
    """


def _document_units_view_sql(*, materialized: bool) -> str:
    if materialized:
        filing_type = "COALESCE(d.class_filing_type, 'other')"
        topics = "d.class_disclosure_topics"
        publisher = "d.class_publisher_categories"
        market = "d.class_market"
        content = "d.class_content_categories"
        cls_join = ""
    else:
        filing_type = "cls.derived_filing_type"
        topics = "cls.derived_topics"
        publisher = "cls.publisher_categories"
        market = "cls.market"
        content = "cls.content_categories"
        cls_join = _CLS_LATERAL_0022
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
        {filing_type} AS filing_type,
        {topics} AS disclosure_topics,
        d.report_period,
        d.announcement_date,
        u.processing_run_id AS producer_action_ref,
        d.source_access_id AS source_ref,
        u.document_id AS parent_ref,
        'document_unit'::text AS asset_kind,
        u.created_at AS observed_at,
        CASE
            WHEN {filing_type} IN ('investor_relations','performance_briefing')
                THEN 'tier_0b'
            ELSE 'tier_0a'
        END AS source_tier,
        'G0'::text AS trace_level,
        d.raw_file_hash,
        u.query_projection_hash,
        {publisher} AS publisher_categories,
        {market} AS market,
        {content} AS content_categories
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    JOIN {CORE_SCHEMA}.processing_run r
      ON r.processing_run_id = u.processing_run_id{cls_join}
    """


def upgrade() -> None:
    for name, column in (
        ("class_filing_type", sa.String(length=64)),
        ("class_market", sa.Text()),
        ("class_rules_version", sa.String(length=256)),
    ):
        op.add_column(
            "document", sa.Column(name, column, nullable=True), schema=CORE_SCHEMA
        )
    for name in (
        "class_disclosure_topics",
        "class_publisher_categories",
        "class_content_categories",
    ):
        op.add_column(
            "document",
            sa.Column(name, sa.dialects.postgresql.JSONB(), nullable=True),
            schema=CORE_SCHEMA,
        )

    conn = op.get_bind()
    stamp = str(conn.execute(sa.text(_STAMP_SQL)).scalar())
    while True:
        updated = conn.execute(
            sa.text(_BACKFILL_BATCH_SQL), {"stamp": stamp, "batch": 5000}
        ).rowcount
        if not updated or updated < 5000:
            break

    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        f"CREATE INDEX ix_document_class_type_date ON {CORE_SCHEMA}.document "
        "(class_filing_type, announcement_date DESC NULLS LAST, document_id DESC)"
    )
    op.execute(
        f"CREATE INDEX ix_document_latest_filing_group ON {CORE_SCHEMA}.document "
        "(company_id, class_filing_type, report_period, "
        "announcement_date DESC NULLS LAST, document_id DESC)"
    )
    op.execute(
        f"CREATE INDEX ix_document_class_topics ON {CORE_SCHEMA}.document "
        "USING gin (class_disclosure_topics)"
    )
    op.execute(
        f"CREATE INDEX ix_document_class_content ON {CORE_SCHEMA}.document "
        "USING gin (class_content_categories)"
    )
    op.execute(
        f"CREATE INDEX ix_document_title_trgm ON {CORE_SCHEMA}.document "
        "USING gin (title gin_trgm_ops)"
    )
    op.execute(
        f"CREATE INDEX ix_document_supersedes ON {CORE_SCHEMA}.document "
        "(supersedes_document_id) WHERE supersedes_document_id IS NOT NULL"
    )
    op.execute(
        f"CREATE INDEX ix_unit_search_projection_rules_version "
        f"ON {CORE_SCHEMA}.unit_search_projection (retrieval_rules_version)"
    )

    op.execute(_documents_view_sql(materialized=True))
    op.execute(_document_units_view_sql(materialized=True))


def downgrade() -> None:
    op.execute(_documents_view_sql(materialized=False))
    op.execute(_document_units_view_sql(materialized=False))
    for index in (
        "ix_document_class_type_date",
        "ix_document_latest_filing_group",
        "ix_document_class_topics",
        "ix_document_class_content",
        "ix_document_title_trgm",
        "ix_document_supersedes",
    ):
        op.execute(f"DROP INDEX IF EXISTS {CORE_SCHEMA}.{index}")
    op.execute(
        f"DROP INDEX IF EXISTS {CORE_SCHEMA}.ix_unit_search_projection_rules_version"
    )
    for name in (
        "class_filing_type",
        "class_market",
        "class_rules_version",
        "class_disclosure_topics",
        "class_publisher_categories",
        "class_content_categories",
    ):
        op.drop_column("document", name, schema=CORE_SCHEMA)
