"""Queue reads over the ops.*_v1 views (08 §1).

The views expose facts only; every threshold predicate lives here so the
worker, doctor, and manual inspection share one definition. Nothing in this
module writes business state except the pinned stale-run reclaim UPDATE.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA, OPS_SCHEMA

_TERMINAL_PUBLISH_QUARANTINE_SQL = """
    COALESCE(r.unit_build_error->>'stage', '') = 'publish'
    AND COALESCE(
        (r.unit_build_error->>'retryable')::boolean,
        false
    ) = false
"""
# Retry charging is source-typed when the failure is created.  Scheduling
# never infers infrastructure or cancellation semantics from error-code text.
_PARSE_FAILURE_CONTRACT_VALID_SQL = """
    NOT EXISTS (
        SELECT 1
          FROM disclosure_core.processing_run AS failed_run
         WHERE failed_run.document_id = q.document_id
           AND failed_run.run_kind = 'parse'
           AND failed_run.provider_document_relpath IS NOT NULL
           AND failed_run.normalized_ir_relpath IS NULL
           AND failed_run.status = 'failed'
           AND (
                jsonb_typeof(failed_run.error->'retryable')
                    IS DISTINCT FROM 'boolean'
                OR failed_run.error->>'retry_budget_class' IS NULL
                OR failed_run.error->>'retry_budget_class'
                   NOT IN ('item', 'infrastructure', 'neutral')
           )
    )
"""
_PARSE_ITEM_FAILURE_COUNT_SQL = """
    (SELECT count(*)
       FROM disclosure_core.processing_run AS item_failure
      WHERE item_failure.document_id = q.document_id
        AND item_failure.run_kind = 'parse'
        AND item_failure.provider_document_relpath IS NOT NULL
        AND item_failure.normalized_ir_relpath IS NULL
        AND item_failure.status = 'failed'
        AND item_failure.error->>'retry_budget_class' = 'item')
"""
_PARSE_CHARGED_FAILURE_COUNT_SQL = """
    (SELECT count(*)
       FROM disclosure_core.processing_run AS charged_failure
      WHERE charged_failure.document_id = q.document_id
        AND charged_failure.run_kind = 'parse'
        AND charged_failure.provider_document_relpath IS NOT NULL
        AND charged_failure.normalized_ir_relpath IS NULL
        AND charged_failure.status = 'failed'
        AND charged_failure.error->>'retry_budget_class'
            IN ('item', 'infrastructure'))
"""
_PARSE_RETRY_ELIGIBLE_SQL = f"""
    COALESCE(q.last_failed_retryable, true)
    AND ({_PARSE_FAILURE_CONTRACT_VALID_SQL})
    AND {_PARSE_ITEM_FAILURE_COUNT_SQL} < :max_retries
    AND {_PARSE_CHARGED_FAILURE_COUNT_SQL} < :max_retries_ceiling
"""
_BUILD_RETRY_ELIGIBLE_SQL = """
    q.unit_build_attempt_count < :max_retries
    OR (
        COALESCE(
            (r.unit_build_error->>'retryable')::boolean,
            false
        ) = true
        AND q.unit_build_attempt_count < :max_retries_ceiling
    )
"""
# Safety valve: even infra-coded failures stop retrying past this multiple
# of max_retries, so a document that somehow always dies infra-coded cannot
# churn forever.
RETRY_CEILING_MULTIPLIER = 5

# Closed vocabulary for tracked_company.sync_frequency; unknown/null values
# fall back to the global interval.
SYNC_FREQUENCY_SECONDS = {"hourly": 3600, "daily": 86400, "weekly": 604800}


def candidate_code_counts(conn: Connection) -> dict[str, int]:
    """Count F006V segments in the candidate universe, once per announcement.

    Auditing only ``document`` observes survivors after the processing gate and
    therefore hides the exact unknown codes that prevented a download.  Source
    snapshots are append-only and overlap. Select the latest *non-empty-code*
    observation of each CNINFO provider_document_id (falling back to latest
    overall) so a newer code-less web fallback cannot mask API F006V evidence.
    """

    rows = conn.execute(
        text(
            f"""
            WITH candidate_versions AS (
                SELECT candidate,
                       row_number() OVER (
                           PARTITION BY candidate->>'provider_document_id'
                           ORDER BY
                                    (NULLIF(btrim(
                                        candidate->>'raw_category'), '')
                                        IS NOT NULL) DESC,
                                    sa.accessed_at DESC,
                                    sa.source_access_id DESC) AS recency
                  FROM {CORE_SCHEMA}.source_access sa
                  CROSS JOIN LATERAL jsonb_array_elements(
                      CASE
                          WHEN jsonb_typeof(sa.result_snapshot->'candidates') = 'array'
                              THEN sa.result_snapshot->'candidates'
                          ELSE '[]'::jsonb
                      END) AS candidate
                 WHERE sa.provider = 'cninfo'
                   AND sa.provider_interface IN (:api_interface, :web_interface)
                   AND sa.status = 'ok'
                   AND jsonb_typeof(sa.result_snapshot->'candidates') = 'array'
                   AND NULLIF(candidate->>'provider_document_id', '') IS NOT NULL
            ), segments AS (
                SELECT candidate->>'provider_document_id' AS provider_document_id,
                       btrim(segment.code) AS code
                  FROM candidate_versions
                  CROSS JOIN LATERAL unnest(string_to_array(
                      candidate->>'raw_category', '||')) AS segment(code)
                 WHERE recency = 1
                   AND NULLIF(btrim(segment.code), '') IS NOT NULL
            )
            SELECT code, count(DISTINCT provider_document_id)::int AS candidate_count
              FROM segments
             GROUP BY code
             ORDER BY candidate_count DESC, code
            """
        ),
        {
            "api_interface": "cninfo:p_info3015",
            "web_interface": "cninfo:hisAnnouncement",
        },
    ).all()
    return {str(row.code): int(row.candidate_count) for row in rows}


def sync_due(
    conn: Connection, *, interval_seconds: int, limit: int
) -> list[dict[str, Any]]:
    """Companies due for an index sync.

    Due-ness compares the checkpoint's updated_at TIMESTAMP against the
    per-company effective interval (sync_frequency vocabulary, else the
    global default) — the old window_end::date comparison truncated both
    sides to dates and stretched a daily cadence to every-other-day.
    Never-synced companies (no checkpoint) are due unless their most recent
    worker-level sync failure is inside the short per-company retry cooldown.
    Ordering untouched companies before failed ones prevents a persistent
    first-page failure set from starving the rest of a large tracked pool.
    The tracked row's lookback/process_classes overrides ride along for the
    caller.
    """

    rows = conn.execute(
        text(
            f"""
            SELECT v.tracked_company_id, v.company_id, v.security_id, v.window_end,
                   s.security_code, s.exchange,
                   tc.lookback, tc.process_classes, tc.sync_frequency,
                   sc.updated_at AS last_synced_at,
                   sync_failure.last_failed_at AS last_sync_failed_at
              FROM {OPS_SCHEMA}.sync_due_v1 v
              LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = v.security_id
              JOIN {CORE_SCHEMA}.tracked_company tc
                ON tc.tracked_company_id = v.tracked_company_id
              LEFT JOIN {CORE_SCHEMA}.source_checkpoint sc
                ON sc.provider = 'cninfo'
               AND sc.scope_key = v.company_id || chr(58) || 'p_info3015'
              LEFT JOIN LATERAL (
                   SELECT max(sa.accessed_at) AS last_failed_at
                     FROM {CORE_SCHEMA}.source_access sa
                    WHERE sa.provider = 'cninfo'
                      AND sa.provider_interface = 'cninfo:worker_sync_failure'
                      AND sa.company_id = v.company_id
              ) sync_failure ON true
             WHERE (sc.updated_at IS NULL
                OR sc.updated_at < now() - make_interval(secs => CASE tc.sync_frequency
                       WHEN 'hourly' THEN 3600
                       WHEN 'daily' THEN 86400
                       WHEN 'weekly' THEN 604800
                       ELSE :interval END))
               AND (sync_failure.last_failed_at IS NULL
                    OR sync_failure.last_failed_at
                       <= now() - make_interval(secs => 60))
             ORDER BY sync_failure.last_failed_at NULLS FIRST,
                      sc.updated_at NULLS FIRST,
                      v.company_id
             LIMIT :limit
            """
        ),
        {"interval": interval_seconds, "limit": limit},
    ).mappings()
    return [dict(row) for row in rows]


# Cascade layer resolution (round21): a tracked company's process_classes
# REPLACES the global policy for that company; NULL inherits the global
# tuple. Same expression drives download and parse — one processing surface.
_EFFECTIVE_CLASSES = f"""
    CASE
      WHEN tc_scope.process_classes IS NULL
        OR (jsonb_typeof(tc_scope.process_classes) = 'array'
            AND jsonb_array_length(tc_scope.process_classes) = 0)
      THEN CAST(:scope_classes AS text[])
      WHEN jsonb_typeof(tc_scope.process_classes) = 'array'
        AND NOT EXISTS (
            SELECT 1
              FROM jsonb_array_elements(tc_scope.process_classes)
                   AS override_item(value)
             WHERE jsonb_typeof(override_item.value) <> 'string'
                OR NOT EXISTS (
                    SELECT 1
                      FROM {CORE_SCHEMA}.classification_rule known_rule
                     WHERE known_rule.rule_set IN ('class', 'title', 'title_topic')
                       AND known_rule.value = override_item.value #>> '{{}}'))
      THEN ARRAY(
          SELECT override_item.value #>> '{{}}'
            FROM jsonb_array_elements(tc_scope.process_classes)
                 AS override_item(value))
      ELSE ARRAY[]::text[]
    END
"""

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
    Hard-noise guard: a title_noise hit excludes the row absolutely —
    codes, topic hits, and per-company overrides cannot readmit it. The
    rule set is deliberately narrow: routine filings that carry a new
    share-count, dilution, debt, cash, project, or risk fact do not belong.
    Callers must bind :scope_classes AND :carrier_classes.

    NULLIF: the web channel stores raw_category as '' (candidate snapshots
    and provider_metadata alike), so empty string must route to the
    code-less branch or web documents never reach the title rules.
    """

    return f"""
               AND ((CASE WHEN NULLIF(btrim({category_expr}), '') IS NOT NULL
                    THEN EXISTS (
                        SELECT 1
                          FROM unnest(string_to_array(
                                   {category_expr}, '||'))
                               AS seg(code)
                          JOIN {CORE_SCHEMA}.classification_rule cr
                            ON cr.rule_set = 'class'
                           AND btrim(seg.code) LIKE cr.prefix || '%'
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
               AND (CASE WHEN NULLIF(btrim({category_expr}), '') IS NOT NULL
                    THEN NOT EXISTS (
                        SELECT 1
                          FROM unnest(string_to_array(
                                   {category_expr}, '||'))
                               AS seg(code)
                          JOIN {CORE_SCHEMA}.classification_rule cx
                            ON cx.rule_set = 'class'
                           AND btrim(seg.code) LIKE cx.prefix || '%'
                         WHERE cx.value = ANY(CAST(:carrier_classes AS text[]))
                           AND NOT cx.value = ANY({_EFFECTIVE_CLASSES}))
                    ELSE NOT EXISTS (
                        SELECT 1 FROM {CORE_SCHEMA}.classification_rule tx
                         WHERE tx.rule_set = 'title'
                           AND {title_expr} LIKE '%' || tx.prefix || '%'
                           AND tx.value = ANY(CAST(:carrier_classes AS text[]))
                           AND NOT tx.value = ANY({_EFFECTIVE_CLASSES})) END)
               AND NOT EXISTS (
                        SELECT 1 FROM {CORE_SCHEMA}.classification_rule nz
                         WHERE nz.rule_set = 'title_noise'
                           AND {title_expr} LIKE '%' || nz.prefix || '%')"""


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


def pending_parse_backlog_count(
    conn: Connection,
    *,
    scope_classes: tuple[str, ...] | None = None,
    require_active_company_scope: bool = True,
) -> int:
    """Count downloaded raw documents still awaiting a successful parse.

    This is deliberately broader than :func:`pending_parse`: non-retryable
    and retry-exhausted documents still occupy raw storage and therefore
    remain part of the backfill admission pressure. The processing predicate
    is retained so metadata-only/noise documents do not consume the
    processing watermark. Ordinary scheduling admits only unbound documents
    or documents whose company is currently tracked and active. A manifest-
    bound frozen replay may explicitly disable that admission check.
    """

    scope_sql = ""
    company_scope_sql = (
        "AND (d.company_id IS NULL OR tc_scope.status = 'active')"
        if require_active_company_scope
        else ""
    )
    params: dict[str, Any] = {}
    if scope_classes is not None:
        scope_sql = _processing_scope_sql(
            category_expr="d.provider_metadata->>'raw_category'",
            title_expr="d.title",
        )
        params["scope_classes"] = list(scope_classes)
        params["carrier_classes"] = list(CARRIER_CLASSES)
    row = conn.execute(
        text(
            f"""
            SELECT count(*)
              FROM {OPS_SCHEMA}.pending_parse_v1 q
              JOIN {CORE_SCHEMA}.document d ON d.document_id = q.document_id
             LEFT JOIN {CORE_SCHEMA}.tracked_company tc_scope
                ON tc_scope.company_id = d.company_id
             WHERE true
               {company_scope_sql}
               {scope_sql}
            """
        ),
        params,
    ).scalar()
    return int(row or 0)


def pending_processing_backlog_count(
    conn: Connection,
    *,
    max_retries: int,
    scope_classes: tuple[str, ...] | None = None,
) -> int:
    """Admission watermark across undiscovered raw work and parse backlog.

    Counting only pending downloads lets a healthy downloader turn the whole
    queue into raw files while the GPU is unavailable.  The sum is stable as
    downloads move candidates into pending-parse, so first-sync admission can
    continue only after parsing actually drains work.
    """

    return pending_download_count(
        conn,
        max_retries=max_retries,
        scope_classes=scope_classes,
    ) + pending_parse_backlog_count(conn, scope_classes=scope_classes)


def pending_downloads(
    conn: Connection,
    *,
    max_retries: int,
    limit: int,
    scope_classes: tuple[str, ...] | None = None,
    security_code: str | None = None,
    min_announcement_date: date | None = None,
) -> list[dict[str, Any]]:
    # Pool membership drives acquisition: a company's candidates download
    # only while an ACTIVE tracked row exists. Covers both round19 (paused =
    # 停止一切获取, queued backlog included) and round22 untrack (deleting the
    # tracked row must NOT re-open the backlog, which the old only-paused-
    # blocks NOT-EXISTS form would have done). Candidates without a company
    # ref stay eligible. The view keeps exposing every candidate; predicates
    # live here. security_code narrows to one company for the CLI sync's
    # immediate-download pass — same gates as the worker, never a bypass
    # (round23).
    params: dict[str, Any] = {"max_retries": max_retries, "limit": limit}
    if scope_classes is not None:
        params["scope_classes"] = list(scope_classes)
        params["carrier_classes"] = list(CARRIER_CLASSES)
    company_sql = ""
    if security_code is not None:
        params["security_code"] = security_code
        company_sql = (
            "AND (q.candidate ->> 'security_code') = :security_code"
        )
    date_sql = ""
    if min_announcement_date is not None:
        # CLI sync's immediate pass only downloads the recent overlap window;
        # historical backfill candidates drain through worker rounds at the
        # provider's paced QPS (round23).
        params["min_announcement_date"] = min_announcement_date
        # announcement_date is text in the view (jsonb ->> extraction);
        # cast before comparing with the date bind param (round23 review B1:
        # text >= date has no operator and crashed every CLI sync).
        date_sql = "AND (q.announcement_date)::date >= :min_announcement_date"
    rows = conn.execute(
        text(
            f"""
            SELECT q.provider_document_id, q.download_url, q.title,
                   q.announcement_date, q.source_access_id, q.company_id,
                   q.candidate, q.failed_download_count,
                   q.already_registered, q.signature_differs
              FROM {OPS_SCHEMA}.pending_download_v1 q
              LEFT JOIN {CORE_SCHEMA}.tracked_company tc_scope
                ON tc_scope.company_id = q.company_id
             WHERE q.failed_download_count < :max_retries
               AND (q.company_id IS NULL
                    OR EXISTS (SELECT 1 FROM {CORE_SCHEMA}.tracked_company tc
                                WHERE tc.company_id = q.company_id
                                  AND tc.status = 'active'))
               {company_sql}
               {date_sql}
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
    require_active_company_scope: bool = True,
    after_document_id: str | None = None,
    document_ids: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Documents awaiting parse, excluding non-retryable and exhausted ones.

    Raw archive byte_count is returned as a scheduling cost signal, never an
    eligibility gate. CNINFO's F005N hint is not unit-stable in the real
    corpus, while source_access.result_snapshot.byte_count is measured after
    download and bound to the archived raw hash. With scope_classes set
    (parse scope 'core'), coded documents parse when any F006V segment hits a
    core class through classification_rule (0016 — classification is
    view-derived, so the queue joins the same rules); code-less channels fall
    back to the registration filing_type. None → parse everything ('all').
    Current active-company admission is independent of that taxonomy scope;
    only a raw-identity-guarded frozen replay may explicitly bypass it.
    """

    scope_sql = ""
    company_scope_sql = (
        "AND (d.company_id IS NULL OR tc_scope.status = 'active')"
        if require_active_company_scope
        else ""
    )
    params: dict[str, Any] = {
        "max_retries": max_retries,
        "max_retries_ceiling": max_retries * RETRY_CEILING_MULTIPLIER,
        "limit": limit,
    }
    if scope_classes is not None:
        scope_sql = _processing_scope_sql(
            category_expr="d.provider_metadata->>'raw_category'",
            title_expr="d.title",
        )
        params["scope_classes"] = list(scope_classes)
        params["carrier_classes"] = list(CARRIER_CLASSES)
    cursor_sql = ""
    if after_document_id is not None and document_ids is not None:
        raise ValueError(
            "after_document_id and document_ids are mutually exclusive"
        )
    if document_ids is not None:
        if not document_ids:
            return []
        cursor_sql = "AND q.document_id = ANY(:document_ids)"
        params["document_ids"] = list(document_ids)
    if after_document_id is not None:
        cursor_sql = "AND q.document_id > :after_document_id"
        params["after_document_id"] = after_document_id
    rows = conn.execute(
        text(
            f"""
            SELECT q.document_id, q.status, q.failed_parse_count,
                   q.last_failed_retryable,
                   d.raw_file_relpath, d.raw_file_hash,
                   CASE
                     WHEN jsonb_typeof(sa.result_snapshot->'byte_count') = 'number'
                     THEN (sa.result_snapshot->>'byte_count')::numeric::bigint
                     ELSE NULL
                   END AS raw_byte_count
              FROM {OPS_SCHEMA}.pending_parse_v1 q
              JOIN {CORE_SCHEMA}.document d ON d.document_id = q.document_id
              LEFT JOIN {CORE_SCHEMA}.source_access sa
                ON sa.source_access_id = d.source_access_id
              LEFT JOIN {CORE_SCHEMA}.tracked_company tc_scope
                ON tc_scope.company_id = d.company_id
             WHERE ({_PARSE_RETRY_ELIGIBLE_SQL})
               {company_scope_sql}
               {scope_sql}
               {cursor_sql}
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
            SELECT q.processing_run_id, q.document_id, q.unit_build_status,
                   q.unit_build_attempt_count
              FROM {OPS_SCHEMA}.pending_build_v1 q
             JOIN {CORE_SCHEMA}.processing_run r
                ON r.processing_run_id = q.processing_run_id
             WHERE ({_BUILD_RETRY_ELIGIBLE_SQL})
               AND NOT ({_TERMINAL_PUBLISH_QUARANTINE_SQL})
             ORDER BY q.processing_run_id
             LIMIT :limit
            """
        ),
        {
            "max_retries": max_retries,
            "max_retries_ceiling": max_retries * RETRY_CEILING_MULTIPLIER,
            "limit": limit,
        },
    ).mappings()
    return [dict(row) for row in rows]


def pending_publish(conn: Connection, *, limit: int) -> list[dict[str, Any]]:
    """Latest non-empty publishable run per document.

    Empty succeeded builds require the explicit ``allow_empty + reason``
    operator path.  Excluding them here prevents a permanent EMPTY_RUN from
    consuming one automatic worker slot forever.
    """

    rows = conn.execute(
        text(
            f"""
            SELECT DISTINCT ON (q.document_id) q.processing_run_id, q.document_id
              FROM {OPS_SCHEMA}.pending_publish_v1 q
              JOIN {CORE_SCHEMA}.processing_run r
                ON r.processing_run_id = q.processing_run_id
             WHERE EXISTS (
                   SELECT 1 FROM {CORE_SCHEMA}.document_unit u
                    WHERE u.processing_run_id = q.processing_run_id)
             ORDER BY q.document_id, r.started_at DESC, q.processing_run_id DESC
             LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    return [dict(row) for row in rows]


def pending_build_count(conn: Connection, *, max_retries: int) -> int:
    return int(
        conn.execute(
            text(
                f"SELECT count(*) FROM {OPS_SCHEMA}.pending_build_v1 q "
                f"JOIN {CORE_SCHEMA}.processing_run r "
                "ON r.processing_run_id = q.processing_run_id "
                f"WHERE ({_BUILD_RETRY_ELIGIBLE_SQL}) "
                f"AND NOT ({_TERMINAL_PUBLISH_QUARANTINE_SQL})"
            ),
            {
                "max_retries": max_retries,
                "max_retries_ceiling": (
                    max_retries * RETRY_CEILING_MULTIPLIER
                ),
            },
        ).scalar_one()
    )


def pending_publish_count(conn: Connection) -> int:
    # Same non-empty predicate as pending_publish (empty runs are the
    # explicit allow_empty operator path, not automatic backlog).
    return int(
        conn.execute(
            text(
                f"""
                SELECT count(DISTINCT q.document_id)
                  FROM {OPS_SCHEMA}.pending_publish_v1 q
                 WHERE EXISTS (
                       SELECT 1 FROM {CORE_SCHEMA}.document_unit u
                        WHERE u.processing_run_id = q.processing_run_id)
                """
            )
        ).scalar_one()
    )


def download_dead_letter_count(conn: Connection, *, max_retries: int) -> int:
    """Distinct candidates permanently out of the download queue: a terminal
    (retryable=false) failure or an exhausted retry budget. Same expressions
    the 0023 view/queries use — single definition lives here (08 §1)."""

    return int(
        conn.execute(
            text(
                f"""
                SELECT count(*) FROM (
                    SELECT f.query_params->>'provider_document_id' AS pid
                      FROM {CORE_SCHEMA}.source_access f
                     WHERE f.provider = 'cninfo'
                       AND f.provider_interface = 'cninfo:download_pdf'
                       AND f.status = 'failed'
                     GROUP BY 1
                    HAVING count(*) >= :max_retries
                        OR bool_or(
                            f.error IS NOT NULL
                            AND (f.error)::jsonb->>'retryable' = 'false')
                ) dead
                """
            ),
            {"max_retries": max_retries},
        ).scalar_one()
    )


def parse_dead_letter_count(conn: Connection, *, max_retries: int) -> int:
    """Pending-parse facts with no legal automatic retry action."""

    return int(
        conn.execute(
            text(
                f"SELECT count(*) FROM {OPS_SCHEMA}.pending_parse_v1 q "
                f"WHERE NOT ({_PARSE_RETRY_ELIGIBLE_SQL})"
            ),
            {
                "max_retries": max_retries,
                "max_retries_ceiling": (
                    max_retries * RETRY_CEILING_MULTIPLIER
                ),
            },
        ).scalar_one()
    )


def retrying_document_count(conn: Connection, *, max_retries: int) -> int:
    """Documents still pending whose latest failure is retryable — the
    actionable "currently failing, will be retried" gauge. The raw
    retryable_failed_run_v1 view counts historical failed RUNS and keeps
    counting them after the document succeeds (batch-0 finding: 55 stale
    rows read as a live backlog)."""

    return int(
        conn.execute(
            text(
                f"SELECT count(*) FROM {OPS_SCHEMA}.pending_parse_v1 q "
                "WHERE q.failed_parse_count > 0 "
                f"AND ({_PARSE_RETRY_ELIGIBLE_SQL})"
            ),
            {
                "max_retries": max_retries,
                "max_retries_ceiling": (
                    max_retries * RETRY_CEILING_MULTIPLIER
                ),
            },
        ).scalar_one()
    )


def last_outbox_event_at(conn: Connection) -> Any:
    return conn.execute(
        text(f"SELECT max(created_at) FROM {OPS_SCHEMA}.outbox_event")
    ).scalar_one_or_none()


def reclaim_stale_runs(conn: Connection, *, threshold_seconds: int) -> int:
    """Fail runs stuck in 'running' beyond the threshold (08 §1 pinned SQL)."""

    result = conn.execute(
        text(
            f"""
            UPDATE {CORE_SCHEMA}.processing_run
               SET status='failed', finished_at=now(),
                   error='{{"stage"\\:"parse","error_code"\\:"stale_reclaimed",'
                         '"retryable"\\:true,'
                         '"retry_budget_class"\\:"infrastructure"}}'::jsonb
             WHERE processing_run_id IN (
                 SELECT processing_run_id FROM {OPS_SCHEMA}.stale_running_run_v1
                  WHERE started_at < now() - make_interval(secs => :threshold))
            """
        ),
        {"threshold": threshold_seconds},
    )
    return int(result.rowcount or 0)
