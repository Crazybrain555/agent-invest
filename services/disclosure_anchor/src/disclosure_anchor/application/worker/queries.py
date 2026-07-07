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
    lookback/filing_categories overrides ride along for the caller.
    """

    rows = conn.execute(
        text(
            f"""
            SELECT v.tracked_company_id, v.company_id, v.security_id, v.window_end,
                   s.security_code, s.exchange,
                   tc.lookback, tc.filing_categories, tc.sync_frequency,
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


def pending_download_count(conn: Connection, *, max_retries: int) -> int:
    """Backfill backpressure input (changedetection.io MAX_QUEUE_SIZE pattern)."""

    row = conn.execute(
        text(
            f"""
            SELECT count(*) FROM {OPS_SCHEMA}.pending_download_v1
             WHERE failed_download_count < :max_retries
            """
        ),
        {"max_retries": max_retries},
    ).scalar()
    return int(row or 0)


def pending_downloads(
    conn: Connection, *, max_retries: int, limit: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            f"""
            SELECT provider_document_id, download_url, title, announcement_date,
                   source_access_id, company_id, candidate, failed_download_count
              FROM {OPS_SCHEMA}.pending_download_v1
             WHERE failed_download_count < :max_retries
             ORDER BY announcement_date, provider_document_id
             LIMIT :limit
            """
        ),
        {"max_retries": max_retries, "limit": limit},
    ).mappings()
    return [dict(row) for row in rows]


def pending_parse(
    conn: Connection,
    *,
    max_retries: int,
    limit: int,
    scope_topics: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Documents awaiting parse, excluding non-retryable and exhausted ones.

    Oversized documents (07 §3.9) are excluded in SQL: they used to be
    LIMIT-selected first and then skipped, permanently occupying every batch
    slot (round8 audit blocker). With scope_topics set (parse scope 'core'),
    'other' documents are parsed only when disclosure_topics (round9 second-
    level classification) hits a core topic; every non-other filing_type
    always parses. None → parse everything ('all').
    """

    scope_sql = ""
    params: dict[str, Any] = {"max_retries": max_retries, "limit": limit}
    if scope_topics is not None:
        scope_sql = """
               AND (d.filing_type <> 'other'
                    OR (d.disclosure_topics IS NOT NULL
                        AND d.disclosure_topics ?| CAST(:scope_topics AS text[])))"""
        params["scope_topics"] = list(scope_topics)
    rows = conn.execute(
        text(
            f"""
            SELECT q.document_id, q.status, q.failed_parse_count,
                   q.last_failed_retryable,
                   COALESCE((d.provider_metadata->>'oversized')::boolean, false)
                       AS oversized
              FROM {OPS_SCHEMA}.pending_parse_v1 q
              JOIN {CORE_SCHEMA}.document d ON d.document_id = q.document_id
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
