"""Canonical write-time refresh for materialized document classification.

Single authority for the classification expression at runtime (register
path and the rules loader); migration 0027 carries a frozen snapshot of the
same semantics for the one-off backfill.  The expression is field-for-field
the 0022 view LATERAL, including NULL behavior, so the public views keep
their exact column contract while reading materialized columns.

The stamp ties rows to the classification_rule content that produced them:
reloading rules (``make load-rules``) refreshes every row whose stamp
differs, preserving the 0017 operational contract — edit JSON, load rules,
everything is current — while list/filter reads stay index-backed
(design: docs/implementation/design/retrieval-scale-hardening.md §3).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA

# Content digest, not just declared versions: an operator edit that forgets
# the version bump still changes the digest, so load-rules always refreshes
# exactly the rows classified under different rule content (review finding,
# 2026-07-22 — version-only stamps split-brained silently on such edits).
CURRENT_STAMP_SQL = f"""
SELECT COALESCE(
    md5(string_agg(
        rule_set || ':' || prefix || ':' || value || ':' || priority::text
            || ':' || version,
        '|' ORDER BY rule_set, prefix, value, priority, version)),
    'empty')
  FROM {CORE_SCHEMA}.classification_rule
"""

_CLS_EXPRESSION = f"""
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

_REFRESH_ASSIGNMENTS = """
       SET class_filing_type = cls.derived_filing_type,
           class_disclosure_topics = cls.derived_topics,
           class_publisher_categories = cls.publisher_categories,
           class_market = cls.market,
           class_content_categories = cls.content_categories,
           class_rules_version = :stamp
"""

REFRESH_ONE_SQL = f"""
UPDATE {CORE_SCHEMA}.document AS tgt
{_REFRESH_ASSIGNMENTS}
  FROM (SELECT s.document_id, s.provider, s.title, s.provider_metadata
          FROM {CORE_SCHEMA}.document s
         WHERE s.document_id = :document_id) d,
       LATERAL ({_CLS_EXPRESSION}) cls
 WHERE tgt.document_id = d.document_id
"""

REFRESH_STALE_BATCH_SQL = f"""
UPDATE {CORE_SCHEMA}.document AS tgt
{_REFRESH_ASSIGNMENTS}
  FROM (SELECT s.document_id, s.provider, s.title, s.provider_metadata
          FROM {CORE_SCHEMA}.document s
         WHERE s.class_rules_version IS DISTINCT FROM :stamp
         ORDER BY s.document_id
         LIMIT :batch) d,
       LATERAL ({_CLS_EXPRESSION}) cls
 WHERE tgt.document_id = d.document_id
"""


def current_rules_stamp(conn: Connection) -> str:
    return str(conn.execute(text(CURRENT_STAMP_SQL)).scalar())


def refresh_document_classification(
    conn: Connection, *, document_id: str, stamp: str | None = None
) -> None:
    """Stamp one document inside the caller's transaction (register path)."""

    conn.execute(
        text(REFRESH_ONE_SQL),
        {
            "document_id": document_id,
            "stamp": stamp or current_rules_stamp(conn),
        },
    )


def refresh_stale_documents(
    engine: Engine, *, batch_size: int = 5000
) -> int:
    """Refresh every document whose stamp predates the loaded rules.

    One transaction per keyset batch — a corpus-scale refresh must never
    ride one giant transaction (WAL bloat, long locks, all-or-nothing).
    """

    with engine.begin() as conn:
        stamp = current_rules_stamp(conn)
    total = 0
    while True:
        with engine.begin() as conn:
            result = conn.execute(
                text(REFRESH_STALE_BATCH_SQL),
                {"stamp": stamp, "batch": batch_size},
            )
            updated = result.rowcount or 0
        total += updated
        if updated < batch_size:
            return total
