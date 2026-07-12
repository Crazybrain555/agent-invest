"""Queue reads over the ops.*_v1 views (08 §1).

The views expose facts only; every threshold predicate lives here so the
worker, doctor, and manual inspection share one definition. Nothing in this
module writes business state except the pinned stale-run reclaim UPDATE.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA, OPS_SCHEMA


# Closed vocabulary for tracked_company.sync_frequency; unknown/null values
# fall back to the global interval.
SYNC_FREQUENCY_SECONDS = {"hourly": 3600, "daily": 86400, "weekly": 604800}


def sync_due(
    conn: Connection, *, interval_seconds: int, limit: int
) -> list[dict[str, Any]]:
    """Companies due for an index sync.

    Due-ness compares the checkpoint's updated_at TIMESTAMP against the
    per-company effective interval (sync_frequency vocabulary, else the
    global default) — the old window_end::date comparison truncated both
    sides to dates and stretched a daily cadence to every-other-day.
    Never-synced companies (no checkpoint) are always due. The tracked row's
    lookback/process_classes overrides ride along for the caller.
    """

    rows = conn.execute(
        text(
            f"""
            SELECT v.tracked_company_id, v.company_id, v.security_id, v.window_end,
                   s.security_code, s.exchange,
                   tc.lookback, tc.process_classes, tc.sync_frequency,
                   sc.updated_at AS last_synced_at
              FROM {OPS_SCHEMA}.sync_due_v1 v
              LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = v.security_id
              JOIN {CORE_SCHEMA}.tracked_company tc
                ON tc.tracked_company_id = v.tracked_company_id
              LEFT JOIN {CORE_SCHEMA}.source_checkpoint sc
                ON sc.provider = 'cninfo'
               AND sc.scope_key = v.company_id || '\:p_info3015'
             WHERE sc.updated_at IS NULL
                OR sc.updated_at < now() - make_interval(secs => CASE tc.sync_frequency
                       WHEN 'hourly' THEN 3600
                       WHEN 'daily' THEN 86400
                       WHEN 'weekly' THEN 604800
                       ELSE :interval END)
             ORDER BY sc.updated_at NULLS FIRST, v.company_id
             LIMIT :limit
            """
        ),
        {"interval": interval_seconds, "limit": limit},
    ).mappings()
    return [dict(row) for row in rows]


# Cascade layer resolution (round21): a tracked company's process_classes
# REPLACES the global policy for that company; NULL inherits the global
# tuple. Same expression drives download and parse — one processing surface.
_EFFECTIVE_CLASSES = (
    "CASE WHEN jsonb_typeof(tc_scope.process_classes) = 'array' "
    "THEN ARRAY(SELECT jsonb_array_elements_text(tc_scope.process_classes)) "
    "ELSE CAST(:scope_classes AS text[]) END"
)

# Carrier precedence (process-classes audit 2026-07-12): a document whose
# codes mark it as a procedural carrier (0129 中介机构报告 — 法律意见书/核查
# 意见/受托管理报告/评级/督导) processes ONLY when that carrier class is
# itself effective, no matter what subject classes ride along on the shared
# F006V segments. ANY-hit alone let every incentive legal opinion through.
# Per-company overrides opt back in by adding the class to process_classes.
CARRIER_CLASSES = ("intermediary_report",)


def _processing_scope_sql(*, category_expr: str, title_expr: str) -> str:
    """Scope predicate shared by the download and parse queues.

    Eligibility: coded rows hit when any F006V segment maps to an effective
    class, code-less rows when a title rule does; both additionally hit
    when a title_topic rule matches (provider-code blind spots, 0021).
    Carrier guard: no carrier-class hit may sit outside the effective set —
    coded rows detect carriers via class rules, code-less via title rules.
    Callers must bind :scope_classes AND :carrier_classes.

    NULLIF: the web channel stores raw_category as '' (candidate snapshots
    and provider_metadata alike), so empty string must route to the
    code-less branch or web documents never reach the title rules.
    """

    return f"""
               AND ((CASE WHEN NULLIF({category_expr}, '') IS NOT NULL
                    THEN EXISTS (
                        SELECT 1
                          FROM unnest(string_to_array(
                                   {category_expr}, '||'))
                               AS seg(code)
                          JOIN {CORE_SCHEMA}.classification_rule cr
                            ON cr.rule_set = 'class'
                           AND seg.code LIKE cr.prefix || '%'
                         WHERE cr.value = ANY({_EFFECTIVE_CLASSES}))
                    ELSE EXISTS (
                        SELECT 1 FROM {CORE_SCHEMA}.classification_rule tr
                         WHERE tr.rule_set = 'title'
                           AND {title_expr} LIKE '%' || tr.prefix || '%'
                           AND tr.value = ANY({_EFFECTIVE_CLASSES})) END)
                    OR EXISTS (
                        SELECT 1 FROM {CORE_SCHEMA}.classification_rule tt
                         WHERE tt.rule_set = 'title_topic'
                           AND {title_expr} LIKE '%' || tt.prefix || '%'
                           AND tt.value = ANY({_EFFECTIVE_CLASSES})))
               AND (CASE WHEN NULLIF({category_expr}, '') IS NOT NULL
                    THEN NOT EXISTS (
                        SELECT 1
                          FROM unnest(string_to_array(
                                   {category_expr}, '||'))
                               AS seg(code)
                          JOIN {CORE_SCHEMA}.classification_rule cx
                            ON cx.rule_set = 'class'
                           AND seg.code LIKE cx.prefix || '%'
                         WHERE cx.value = ANY(CAST(:carrier_classes AS text[]))
                           AND NOT cx.value = ANY({_EFFECTIVE_CLASSES}))
                    ELSE NOT EXISTS (
                        SELECT 1 FROM {CORE_SCHEMA}.classification_rule tx
                         WHERE tx.rule_set = 'title'
                           AND {title_expr} LIKE '%' || tx.prefix || '%'
                           AND tx.value = ANY(CAST(:carrier_classes AS text[]))
                           AND NOT tx.value = ANY({_EFFECTIVE_CLASSES})) END)"""


def _download_scope_sql(scope_classes: tuple[str, ...] | None) -> str:
    """Processing-scope predicate for the download queue."""

    if scope_classes is None:
        return ""
    return _processing_scope_sql(
        category_expr="q.candidate->>'raw_category'",
        title_expr="q.candidate->>'title'",
    )


def pending_download_count(
    conn: Connection,
    *,
    max_retries: int,
    scope_classes: tuple[str, ...] | None = None,
) -> int:
    """Backfill backpressure input (changedetection.io MAX_QUEUE_SIZE pattern)."""

    params: dict[str, Any] = {"max_retries": max_retries}
    if scope_classes is not None:
        params["scope_classes"] = list(scope_classes)
        params["carrier_classes"] = list(CARRIER_CLASSES)
    row = conn.execute(
        text(
            f"""
            SELECT count(*) FROM {OPS_SCHEMA}.pending_download_v1 q
              LEFT JOIN {CORE_SCHEMA}.tracked_company tc_scope
                ON tc_scope.company_id = q.company_id
             WHERE q.failed_download_count < :max_retries
               AND (q.company_id IS NULL
                    OR EXISTS (SELECT 1 FROM {CORE_SCHEMA}.tracked_company tc
                                WHERE tc.company_id = q.company_id
                                  AND tc.status = 'active'))
               {_download_scope_sql(scope_classes)}
            """
        ),
        params,
    ).scalar()
    return int(row or 0)


def pending_downloads(
    conn: Connection,
    *,
    max_retries: int,
    limit: int,
    scope_classes: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    # Pool membership drives acquisition: a company's candidates download
    # only while an ACTIVE tracked row exists. Covers both round19 (paused =
    # 停止一切获取, queued backlog included) and round22 untrack (deleting the
    # tracked row must NOT re-open the backlog, which the old only-paused-
    # blocks NOT-EXISTS form would have done). Candidates without a company
    # ref stay eligible. The view keeps exposing every candidate; predicates
    # live here.
    params: dict[str, Any] = {"max_retries": max_retries, "limit": limit}
    if scope_classes is not None:
        params["scope_classes"] = list(scope_classes)
        params["carrier_classes"] = list(CARRIER_CLASSES)
    rows = conn.execute(
        text(
            f"""
            SELECT q.provider_document_id, q.download_url, q.title,
                   q.announcement_date, q.source_access_id, q.company_id,
                   q.candidate, q.failed_download_count
              FROM {OPS_SCHEMA}.pending_download_v1 q
              LEFT JOIN {CORE_SCHEMA}.tracked_company tc_scope
                ON tc_scope.company_id = q.company_id
             WHERE q.failed_download_count < :max_retries
               AND (q.company_id IS NULL
                    OR EXISTS (SELECT 1 FROM {CORE_SCHEMA}.tracked_company tc
                                WHERE tc.company_id = q.company_id
                                  AND tc.status = 'active'))
               {_download_scope_sql(scope_classes)}
             ORDER BY q.announcement_date, q.provider_document_id
             LIMIT :limit
            """
        ),
        params,
    ).mappings()
    return [dict(row) for row in rows]


def pending_parse(
    conn: Connection,
    *,
    max_retries: int,
    limit: int,
    scope_classes: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Documents awaiting parse, excluding non-retryable and exhausted ones.

    Oversized documents (07 §3.9) are excluded in SQL: they used to be
    LIMIT-selected first and then skipped, permanently occupying every batch
    slot (round8 audit blocker). With scope_classes set (parse scope 'core'),
    coded documents parse when any F006V segment hits a core class through
    classification_rule (0016 — classification is view-derived, so the queue
    joins the same rules); code-less channels fall back to the registration
    filing_type. None → parse everything ('all').
    """

    scope_sql = ""
    params: dict[str, Any] = {"max_retries": max_retries, "limit": limit}
    if scope_classes is not None:
        scope_sql = _processing_scope_sql(
            category_expr="d.provider_metadata->>'raw_category'",
            title_expr="d.title",
        )
        params["scope_classes"] = list(scope_classes)
        params["carrier_classes"] = list(CARRIER_CLASSES)
    rows = conn.execute(
        text(
            f"""
            SELECT q.document_id, q.status, q.failed_parse_count,
                   q.last_failed_retryable,
                   COALESCE((d.provider_metadata->>'oversized')::boolean, false)
                       AS oversized
              FROM {OPS_SCHEMA}.pending_parse_v1 q
              JOIN {CORE_SCHEMA}.document d ON d.document_id = q.document_id
              LEFT JOIN {CORE_SCHEMA}.tracked_company tc_scope
                ON tc_scope.company_id = d.company_id
             WHERE COALESCE(q.last_failed_retryable, true)
               AND q.failed_parse_count < :max_retries
               AND NOT COALESCE((d.provider_metadata->>'oversized')::boolean, false)
               {scope_sql}
             ORDER BY q.document_id
             LIMIT :limit
            """
        ),
        params,
    ).mappings()
    return [dict(row) for row in rows]


def pending_build(
    conn: Connection, *, max_retries: int, limit: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            f"""
            SELECT processing_run_id, document_id, unit_build_status,
                   unit_build_attempt_count
              FROM {OPS_SCHEMA}.pending_build_v1
             WHERE unit_build_attempt_count < :max_retries
             ORDER BY processing_run_id
             LIMIT :limit
            """
        ),
        {"max_retries": max_retries, "limit": limit},
    ).mappings()
    return [dict(row) for row in rows]


def pending_publish(conn: Connection, *, limit: int) -> list[dict[str, Any]]:
    """Latest publishable run per document (ORDER pinned by 08 §1)."""

    rows = conn.execute(
        text(
            f"""
            SELECT DISTINCT ON (q.document_id) q.processing_run_id, q.document_id
              FROM {OPS_SCHEMA}.pending_publish_v1 q
              JOIN {CORE_SCHEMA}.processing_run r
                ON r.processing_run_id = q.processing_run_id
             ORDER BY q.document_id, r.started_at DESC, q.processing_run_id DESC
             LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    return [dict(row) for row in rows]


def reclaim_stale_runs(conn: Connection, *, threshold_seconds: int) -> int:
    """Fail runs stuck in 'running' beyond the threshold (08 §1 pinned SQL)."""

    result = conn.execute(
        text(
            f"""
            UPDATE {CORE_SCHEMA}.processing_run
               SET status='failed', finished_at=now(),
                   error='{{"stage"\\:"parse","error_code"\\:"stale_reclaimed","retryable"\\:true}}'::jsonb
             WHERE processing_run_id IN (
                 SELECT processing_run_id FROM {OPS_SCHEMA}.stale_running_run_v1
                  WHERE started_at < now() - make_interval(secs => :threshold))
            """
        ),
        {"threshold": threshold_seconds},
    )
    return int(result.rowcount or 0)
