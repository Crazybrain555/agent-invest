"""Single-snapshot SQL inventory of owned source, run and scheduling facts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from disclosure_anchor.adapters.db.postgres.processing_requests import _last_failure
from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA as C, OPS_SCHEMA as O
from disclosure_anchor.application.contracts.disclosure_inventory import (
    INVENTORY_STATUSES, InventoryQuery, InventorySnapshot,
)
from disclosure_anchor.application.worker import queries


def inventory_sql(scope_classes: tuple[str, ...] | None) -> str:
    """Reuse scheduler fragments verbatim rather than defining another eligibility model."""
    parse_scope = "" if scope_classes is None else queries._processing_scope_sql(
        category_expr="d.provider_metadata->>'raw_category'", title_expr="d.title",
    )
    parse_admission = queries._parse_admission_sql(
        "AND (d.company_id IS NULL OR tc_scope.status = 'active')", parse_scope,
    )
    status_counts = ", ".join(
        f"'{status}', count(*) FILTER (WHERE status = '{status}')" for status in INVENTORY_STATUSES
    )
    # Row sets read by several steps (canonical documents, scoped, runs, facts) are
    # written once; single-reader steps and the classified/matching projections
    # stream. Each queue policy runs only over that queue's rows: CASE stops the
    # parse scope gate becoming an early filter over every document, and the
    # publish_queue fence keeps the unit-existence check a per-run probe instead
    # of an aggregate over all document units.
    return f"""
    WITH canonical_documents AS MATERIALIZED (
      SELECT DISTINCT ON (
          COALESCE(d.provider, ''), CASE WHEN NULLIF(d.provider, '') IS NULL THEN d.document_id
            ELSE COALESCE(NULLIF(d.provider_document_id, ''), d.document_id) END)
             d.document_id, d.provider, d.provider_document_id, d.security_id, d.title,
             d.announcement_date, d.status, d.raw_file_hash, d.raw_file_relpath,
             d.current_processing_run_id
        FROM {C}.document d
       ORDER BY COALESCE(d.provider, ''), CASE WHEN NULLIF(d.provider, '') IS NULL THEN d.document_id
                  ELSE COALESCE(NULLIF(d.provider_document_id, ''), d.document_id) END,
                d.created_at DESC, d.document_id DESC
    ), candidates AS (
      SELECT DISTINCT ON (c->>'provider_document_id')
             c->>'provider_document_id' AS provider_document_id, c AS candidate
        FROM {C}.source_access sa,
             jsonb_array_elements(sa.result_snapshot->'candidates') AS c
       WHERE sa.provider = 'cninfo' AND sa.status = 'ok'
         AND sa.provider_interface IN ('cninfo:p_info3015', 'cninfo:hisAnnouncement')
         AND NULLIF(c->>'provider_document_id', '') IS NOT NULL
       ORDER BY c->>'provider_document_id',
                CASE WHEN COALESCE(c->>'raw_category', '') <> '' THEN 0 ELSE 1 END,
                sa.accessed_at DESC, sa.source_access_id DESC
    ), universe AS (
      SELECT 'document:' || d.document_id AS inventory_id, d.document_id,
             d.provider, d.provider_document_id, s.security_code, s.exchange,
             d.title, d.announcement_date::text AS announcement_date,
             d.status AS document_status, d.raw_file_hash,
             (d.raw_file_hash IS NOT NULL AND d.raw_file_relpath IS NOT NULL) AS archived,
             d.current_processing_run_id
        FROM canonical_documents d LEFT JOIN {C}.security s ON s.security_id = d.security_id
      UNION ALL
      SELECT 'candidate:cninfo:' || c.provider_document_id, NULL::text, 'cninfo',
             c.provider_document_id, c.candidate->>'security_code', c.candidate->>'exchange',
             c.candidate->>'title', c.candidate->>'announcement_date', NULL::text, NULL::text,
             false, NULL::text
        FROM candidates c
       WHERE NOT EXISTS (SELECT 1 FROM canonical_documents d
                          WHERE d.provider = 'cninfo' AND d.provider_document_id = c.provider_document_id)
    ), scoped AS MATERIALIZED (
      SELECT * FROM universe WHERE CAST(:security_code AS text) IS NULL OR security_code = :security_code
    ), parse_selected AS (
      SELECT q.document_id FROM {O}.pending_parse_v1 q
      JOIN scoped target ON target.document_id = q.document_id
      JOIN {C}.document d ON d.document_id = q.document_id
      LEFT JOIN {C}.tracked_company tc_scope ON tc_scope.company_id = d.company_id
      WHERE CASE WHEN ({queries._PARSE_RETRY_ELIGIBLE_SQL}) THEN true {parse_admission} ELSE false END
    ), download_selected AS (
      SELECT q.provider_document_id FROM {O}.pending_download_v1 q
      JOIN scoped target ON target.provider = 'cninfo' AND target.provider_document_id = q.provider_document_id
      LEFT JOIN {C}.tracked_company tc_scope ON tc_scope.company_id = q.company_id
      WHERE q.failed_download_count < :max_download_retries {queries._download_admission_sql(scope_classes)}
    ), publish_queue AS MATERIALIZED (
      SELECT q.processing_run_id, q.document_id FROM {O}.pending_publish_v1 q
      JOIN scoped target ON target.document_id = q.document_id
    ), post_selected AS (
      SELECT q.document_id FROM {O}.pending_build_v1 q
      JOIN scoped target ON target.document_id = q.document_id
      JOIN {C}.processing_run r ON r.processing_run_id = q.processing_run_id
      WHERE ({queries._BUILD_RETRY_ELIGIBLE_SQL}) AND NOT ({queries._TERMINAL_PUBLISH_QUARANTINE_SQL})
      UNION
      SELECT q.document_id FROM publish_queue q
      WHERE EXISTS (SELECT 1 FROM {C}.document_unit u WHERE u.processing_run_id = q.processing_run_id)
    ), relevant_runs AS MATERIALIZED (
      SELECT r.* FROM {C}.processing_run r JOIN scoped target ON target.document_id = r.document_id
    ), running AS (
      SELECT DISTINCT ON (document_id) document_id, processing_run_id, started_at
      FROM relevant_runs WHERE status = 'running'
      ORDER BY document_id, started_at DESC, processing_run_id DESC
    ), latest_run AS (
      SELECT DISTINCT ON (document_id) document_id, status, unit_build_status,
             unit_build_error, unit_build_attempt_count
      FROM relevant_runs ORDER BY document_id, started_at DESC, processing_run_id DESC
    ), parse_failures AS MATERIALIZED (
      SELECT r.* FROM relevant_runs r WHERE r.run_kind = 'parse' AND r.status = 'failed'
        AND r.provider_document_relpath IS NOT NULL AND r.normalized_ir_relpath IS NULL
    ), parse_retry AS (
      SELECT failed_run.document_id,
             count(*) FILTER (WHERE error->>'retry_budget_class' = 'item') AS item_failures,
             count(*) FILTER (WHERE error->>'retry_budget_class' IN ('item', 'infrastructure')) AS charged_failures,
             count(*) FILTER (WHERE ({queries.PARSE_UNRELEASED_CONTRACT_FAILURE_SQL})) AS contract_failures
      FROM parse_failures failed_run GROUP BY failed_run.document_id
    ), latest_parse_failure AS (
      SELECT DISTINCT ON (r.document_id) r.document_id,
             (r.error->>'retryable')::boolean AS retryable,
             EXISTS (SELECT 1 FROM {O}.parse_requeue_decision p
                      WHERE p.processing_run_id = r.processing_run_id) AS released
      FROM parse_failures r ORDER BY r.document_id, r.started_at DESC, r.processing_run_id DESC
    ), last_run_failure AS (
      SELECT DISTINCT ON (r.document_id) r.document_id, r.processing_run_id,
             CASE WHEN r.unit_build_status = 'failed'
                  THEN CASE WHEN r.unit_build_error->>'stage' = 'publish' THEN 'publish' ELSE 'build' END
                  ELSE 'parse' END AS failure_stage,
             CASE WHEN r.unit_build_status = 'failed' THEN r.unit_build_error ELSE r.error END AS failure_error,
             COALESCE(r.finished_at, r.started_at) AS failure_at
      FROM relevant_runs r WHERE r.status = 'failed' OR r.unit_build_status = 'failed'
      ORDER BY r.document_id, COALESCE(r.finished_at, r.started_at) DESC, r.processing_run_id DESC
    ), download_failures AS (
      SELECT f.provider_document_id, count(*) AS failure_count,
             bool_or(f.nonretryable AND f.resolved_by_source_access_id IS NULL) AS nonretryable
      FROM {O}.download_failure_resolution_v1 f
      JOIN scoped target ON target.provider = 'cninfo' AND target.provider_document_id = f.provider_document_id
      GROUP BY f.provider_document_id
    ), last_download_failure AS (
      SELECT DISTINCT ON (sa.query_params->>'provider_document_id')
             sa.query_params->>'provider_document_id' AS provider_document_id,
             sa.error AS failure_error, sa.accessed_at AS failure_at
      FROM {C}.source_access sa
      JOIN scoped target ON target.provider = 'cninfo'
        AND target.provider_document_id = sa.query_params->>'provider_document_id'
      WHERE sa.provider = 'cninfo' AND sa.provider_interface = 'cninfo:download_pdf' AND sa.status = 'failed'
      ORDER BY sa.query_params->>'provider_document_id', sa.accessed_at DESC, sa.source_access_id DESC
    ), facts AS MATERIALIZED (
      SELECT target.*, running.processing_run_id AS running_processing_run_id,
             running.started_at AS running_started_at,
             ps.document_id IS NOT NULL AS parse_selected,
             ds.provider_document_id IS NOT NULL AS download_selected,
             post.document_id IS NOT NULL AS post_processing_selected,
             COALESCE(df.failure_count, 0) AS download_failure_count,
             COALESCE(df.nonretryable, false) AS download_nonretryable,
             COALESCE(pr.item_failures, 0) AS item_failures,
             COALESCE(pr.charged_failures, 0) AS charged_failures,
             COALESCE(pr.contract_failures, 0) AS contract_failures,
             lp.retryable AS latest_parse_retryable, COALESCE(lp.released, false) AS latest_parse_released,
             lr.unit_build_attempt_count, lr.unit_build_error,
             CASE WHEN target.document_id IS NULL THEN 'download' ELSE rf.failure_stage END AS failure_stage,
             CASE WHEN target.document_id IS NULL THEN to_jsonb(ldf.failure_error) ELSE rf.failure_error END AS failure_error,
             CASE WHEN target.document_id IS NULL THEN ldf.failure_at ELSE rf.failure_at END AS failure_at,
             rf.processing_run_id AS failure_run_id,
             CASE WHEN target.document_status = 'published' THEN false
                  WHEN running.processing_run_id IS NOT NULL THEN false
                  WHEN target.document_id IS NULL THEN COALESCE(df.failure_count, 0) > 0
                  ELSE target.document_status = 'parse_failed'
                       OR COALESCE(lr.status = 'failed' OR lr.unit_build_status = 'failed', false) END AS active_failure
      FROM scoped target
      LEFT JOIN parse_selected ps ON ps.document_id = target.document_id
      LEFT JOIN download_selected ds ON target.provider = 'cninfo' AND ds.provider_document_id = target.provider_document_id
      LEFT JOIN post_selected post ON post.document_id = target.document_id
      LEFT JOIN running ON running.document_id = target.document_id
      LEFT JOIN latest_run lr ON lr.document_id = target.document_id
      LEFT JOIN parse_retry pr ON pr.document_id = target.document_id
      LEFT JOIN latest_parse_failure lp ON lp.document_id = target.document_id
      LEFT JOIN last_run_failure rf ON rf.document_id = target.document_id
      LEFT JOIN download_failures df ON target.provider = 'cninfo' AND df.provider_document_id = target.provider_document_id
      LEFT JOIN last_download_failure ldf ON target.provider = 'cninfo' AND ldf.provider_document_id = target.provider_document_id
    ), classified AS NOT MATERIALIZED (
      SELECT facts.*, CASE
        WHEN document_status = 'published' THEN 'published'
        WHEN running_processing_run_id IS NOT NULL THEN 'parsing'
        WHEN document_status = 'parsed' THEN 'post_processing'
        WHEN archived AND parse_selected THEN 'downloaded_queued'
        WHEN archived THEN 'downloaded_not_queued'
        WHEN download_selected THEN 'not_downloaded_queued'
        ELSE 'not_downloaded_not_queued' END AS status
      FROM facts
    ), matching AS NOT MATERIALIZED (
      SELECT * FROM classified WHERE CAST(:status AS text) IS NULL
        OR status = :status OR (:status = 'failed' AND active_failure)
    ), page AS (
      SELECT * FROM matching WHERE CAST(:after_id AS text) IS NULL OR inventory_id > :after_id
      ORDER BY inventory_id LIMIT :page_limit
    )
    SELECT statement_timestamp() AS observed_at,
           (SELECT count(*) FROM classified) AS known_total,
           (SELECT count(*) FROM matching) AS matching_total,
           (SELECT jsonb_build_object({status_counts}) FROM classified) AS by_status,
           (SELECT count(*) FROM classified WHERE active_failure) AS active_failures,
           COALESCE((SELECT jsonb_agg(to_jsonb(page) ORDER BY inventory_id) FROM page), '[]'::jsonb) AS items
    """


class PostgresDisclosureInventoryReader:
    def __init__(
        self, engine: Engine, *, max_download_retries: int, max_parse_retries: int,
        max_build_retries: int, scope_classes: tuple[str, ...] | None,
    ) -> None:
        if any(type(value) is not int or value < 1 for value in (
            max_download_retries, max_parse_retries, max_build_retries,
        )):
            raise ValueError("Inventory retry limits must be positive integers")
        self._engine = engine
        self._download = max_download_retries
        self._parse = max_parse_retries
        self._build = max_build_retries
        self._scope_classes = scope_classes

    def read(self, query: InventoryQuery) -> InventorySnapshot:
        with self._engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            # Bounds the whole global snapshot; limit only bounds the returned page.
            conn.execute(text("SET LOCAL statement_timeout = '120s'"))
            try:
                return self._read(conn, query)
            finally:
                conn.rollback()

    def _read(self, conn: Connection, query: InventoryQuery) -> InventorySnapshot:
        parameters = {
            "security_code": query.security_code, "status": query.status,
            "after_id": query.after_inventory_id, "page_limit": query.limit + 1,
            "max_retries": self._parse,
            "max_retries_ceiling": self._parse * queries.RETRY_CEILING_MULTIPLIER,
            "max_download_retries": self._download,
            "scope_classes": None if self._scope_classes is None else list(self._scope_classes),
            "carrier_classes": list(queries.CARRIER_CLASSES),
        }
        # Build uses the same parameter names as parse. Distinct bound parameters
        # preserve independently configured retry budgets in the combined read.
        sql = inventory_sql(self._scope_classes).replace(
            queries._BUILD_RETRY_ELIGIBLE_SQL,
            queries._BUILD_RETRY_ELIGIBLE_SQL.replace(":max_retries_ceiling", ":max_build_ceiling")
                .replace(":max_retries", ":max_build_retries"),
        )
        parameters["max_build_retries"] = self._build
        parameters["max_build_ceiling"] = self._build * queries.RETRY_CEILING_MULTIPLIER
        row = conn.execute(text(sql), parameters).mappings().one()
        items = row["items"]
        return InventorySnapshot(
            observed_at=row["observed_at"].isoformat(), known_total=int(row["known_total"]),
            matching_total=int(row["matching_total"]),
            by_status={key: int(value) for key, value in row["by_status"].items()},
            active_failures=int(row["active_failures"]),
            items=tuple(self._public_item(item) for item in items[:query.limit]),
            has_more=len(items) > query.limit,
        )

    def _public_item(self, row: Mapping[str, Any]) -> dict[str, Any]:
        document_id = row["document_id"]
        archived = row["archived"]
        published = row["document_status"] == "published"
        running = row["running_processing_run_id"] is not None
        processing_state = (
            "published" if published else "parse_running" if running else
            "post_processing" if row["document_status"] == "parsed" else
            "parse_failed" if row["active_failure"] and document_id is not None else
            "not_started" if document_id is not None else "waiting_for_original"
        )
        last_failure = None
        if row["failure_at"] is not None:
            safe = _last_failure(row["failure_error"], None) or {}
            last_failure = {"stage": row["failure_stage"], "error_code": safe.get("error_code"),
                            "retryable": safe.get("retryable"), "occurred_at": row["failure_at"],
                            "processing_run_id": row["failure_run_id"]}
        reasons: list[str] = []
        if not archived and not row["download_selected"]:
            if document_id is not None:
                reasons.append("original_metadata_missing")
            if row["download_failure_count"] >= self._download:
                reasons.append("download_retry_exhausted")
            if row["download_nonretryable"]:
                reasons.append("download_nonretryable_failure")
            if not reasons:
                reasons.append("not_selected_by_download_queue")
        elif archived and not published and not running and not row["parse_selected"]:
            if row["document_status"] == "parsed":
                if not row["post_processing_selected"]:
                    if row["unit_build_error"] is not None:
                        safe = _last_failure(row["unit_build_error"], None) or {}
                        reasons.append("terminal_publish_failure" if safe.get("retryable") is False
                                       and row["unit_build_error"].get("stage") == "publish"
                                       else "post_processing_not_selected")
                    else:
                        reasons.append("post_processing_not_selected")
            else:
                if row["latest_parse_retryable"] is False and not row["latest_parse_released"]:
                    reasons.append("latest_failure_nonretryable")
                if row["contract_failures"] > 0:
                    reasons.append("unreleased_contract_failure")
                if row["item_failures"] >= self._parse:
                    reasons.append("item_retry_budget_exhausted")
                if row["charged_failures"] >= self._parse * queries.RETRY_CEILING_MULTIPLIER:
                    reasons.append("charged_retry_budget_exhausted")
                if not reasons:
                    reasons.append("not_selected_by_parse_queue")
        return {
            **{key: row[key] for key in (
                "inventory_id", "document_id", "provider", "provider_document_id", "security_code",
                "exchange", "title", "announcement_date", "status",
            )},
            "original": {"state": "archived_recorded" if archived else "not_archived",
                         "raw_file_hash": row["raw_file_hash"],
                         "text_path": f"/v1/documents/{document_id}/text" if archived else None},
            "processing": {"state": processing_state, "document_status": row["document_status"],
                           "current_processing_run_id": row["current_processing_run_id"],
                           "running_processing_run_id": row["running_processing_run_id"],
                           "running_started_at": row["running_started_at"],
                           "active_failure": row["active_failure"], "last_failure": last_failure,
                           "units_path": f"/v1/documents/{document_id}/units" if published else None},
            "scheduling": {"download_selected": row["download_selected"],
                           "parse_selected": row["parse_selected"],
                           "post_processing_selected": row["post_processing_selected"],
                           "blocked_reasons": reasons},
        }
