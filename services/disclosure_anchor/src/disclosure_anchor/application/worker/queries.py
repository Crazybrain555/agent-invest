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


def sync_due(
    conn: Connection, *, interval_seconds: int, limit: int
) -> list[dict[str, Any]]:
    """Companies whose checkpoint window_end is older than the sync interval.

    window_end is NULL for never-synced companies → always due.
    """

    rows = conn.execute(
        text(
            f"""
            SELECT v.tracked_company_id, v.company_id, v.security_id, v.window_end,
                   s.security_code, s.exchange
              FROM {OPS_SCHEMA}.sync_due_v1 v
              LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = v.security_id
             WHERE v.window_end IS NULL
                OR v.window_end::date < (now() - make_interval(secs => :interval))::date
             ORDER BY v.window_end NULLS FIRST, v.company_id
             LIMIT :limit
            """
        ),
        {"interval": interval_seconds, "limit": limit},
    ).mappings()
    return [dict(row) for row in rows]


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
    conn: Connection, *, max_retries: int, limit: int
) -> list[dict[str, Any]]:
    """Documents awaiting parse, excluding non-retryable and exhausted ones.

    The oversized flag rides along so the worker can skip 07 §3.9 documents
    without a second query.
    """

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
             ORDER BY q.document_id
             LIMIT :limit
            """
        ),
        {"max_retries": max_retries, "limit": limit},
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
