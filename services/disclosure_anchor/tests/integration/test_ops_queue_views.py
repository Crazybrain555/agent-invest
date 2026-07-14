"""Queue-view semantics on a live DB (08 §5) — positives plus pinned negatives."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from disclosure_anchor.application.worker import queries
from disclosure_anchor.domain import ids
from tests.integration._support import engine_or_skip


class OpsQueueViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.suffix = ids.new_ulid().lower()
        self.doc_ids: list[str] = []
        self.run_ids: list[str] = []
        self.unit_ids: list[str] = []
        self.sa_ids: list[str] = []
        self.company_id: str | None = None
        self.tracked_id: str | None = None

    def tearDown(self) -> None:
        with self.engine.begin() as conn:
            if self.unit_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.document_unit "
                        "WHERE asset_id = ANY(:ids)"
                    ),
                    {"ids": self.unit_ids},
                )
            if self.run_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.processing_run "
                        "WHERE processing_run_id = ANY(:ids)"
                    ),
                    {"ids": self.run_ids},
                )
            if self.doc_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.document "
                        "WHERE document_id = ANY(:ids)"
                    ),
                    {"ids": self.doc_ids},
                )
            if self.sa_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.source_access "
                        "WHERE source_access_id = ANY(:ids)"
                    ),
                    {"ids": self.sa_ids},
                )
            if self.tracked_id:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.tracked_company "
                        "WHERE tracked_company_id = :id"
                    ),
                    {"id": self.tracked_id},
                )
            if self.company_id:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.company WHERE company_id = :id"
                    ),
                    {"id": self.company_id},
                )
        self.engine.dispose()

    def _insert_document(self, conn, status: str = "registered") -> str:
        document_id = f"doc_qv{self.suffix}{len(self.doc_ids)}"
        conn.execute(
            text(
                "INSERT INTO disclosure_core.document "
                "(document_id, status, provider, provider_document_id, provider_metadata) "
                "VALUES (:id, :status, 'cninfo', :pid, '{}'::jsonb)"
            ),
            {"id": document_id, "status": status, "pid": f"qv{self.suffix}{len(self.doc_ids)}"},
        )
        self.doc_ids.append(document_id)
        return document_id

    def _insert_run(
        self,
        conn,
        document_id: str,
        *,
        status: str,
        unit_build_status: str = "not_started",
        is_active: bool = False,
        started_at: datetime | None = None,
        error: dict | None = None,
        attempts: int = 0,
    ) -> str:
        run_id = f"run_qv{self.suffix}{len(self.run_ids)}"
        conn.execute(
            text(
                "INSERT INTO disclosure_core.processing_run "
                "(processing_run_id, document_id, run_kind, status, unit_build_status, "
                " is_active, started_at, error, unit_build_attempt_count) "
                "VALUES (:id, :doc, 'parse', :status, :ubs, :active, :started, "
                "        CAST(:error AS jsonb), :attempts)"
            ),
            {
                "id": run_id,
                "doc": document_id,
                "status": status,
                "ubs": unit_build_status,
                "active": is_active,
                "started": started_at or datetime.now(timezone.utc),
                "error": json.dumps(error) if error is not None else None,
                "attempts": attempts,
            },
        )
        self.run_ids.append(run_id)
        return run_id

    def _insert_unit(self, conn, document_id: str, run_id: str) -> str:
        unit_id = f"du_qv{self.suffix}{len(self.unit_ids)}"
        conn.execute(
            text(
                "INSERT INTO disclosure_core.document_unit "
                "(asset_id, document_id, processing_run_id, payload_kind, "
                " order_index, payload, content_hash) "
                "VALUES (:id, :doc, :run, 'text', 1, '{}'::jsonb, :hash)"
            ),
            {
                "id": unit_id,
                "doc": document_id,
                "run": run_id,
                "hash": f"sha256:{self.suffix}",
            },
        )
        self.unit_ids.append(unit_id)
        return unit_id

    def test_pending_publish_enqueues_document_without_active_run(self) -> None:
        # Pinned negative #1 inverse: no active run at all → still enqueued.
        with self.engine.begin() as conn:
            document_id = self._insert_document(conn, status="parsed")
            run_id = self._insert_run(
                conn, document_id, status="succeeded", unit_build_status="succeeded"
            )
            self._insert_unit(conn, document_id, run_id)
            rows = queries.pending_publish(conn, limit=1000)
        self.assertIn(run_id, [row["processing_run_id"] for row in rows])

    def test_pending_publish_excludes_real_empty_run_poison(self) -> None:
        with self.engine.begin() as conn:
            document_id = self._insert_document(conn, status="parsed")
            run_id = self._insert_run(
                conn, document_id, status="succeeded", unit_build_status="succeeded"
            )
            raw_rows = conn.execute(
                text(
                    "SELECT processing_run_id FROM disclosure_ops.pending_publish_v1 "
                    "WHERE processing_run_id = :run"
                ),
                {"run": run_id},
            ).scalars()
            automatic_rows = queries.pending_publish(conn, limit=1000)
        self.assertIn(run_id, list(raw_rows), "view preserves the dead-letter fact")
        self.assertNotIn(
            run_id,
            [row["processing_run_id"] for row in automatic_rows],
            "automatic queue must not retry EMPTY_RUN forever",
        )

    def test_pending_parse_helper_excludes_non_retryable_failure(self) -> None:
        # Pinned negative #2: retryable=false excluded by the helper threshold.
        with self.engine.begin() as conn:
            document_id = self._insert_document(conn, status="parse_failed")
            self._insert_run(
                conn,
                document_id,
                status="failed",
                error={"stage": "parse", "error_code": "X", "retryable": False},
            )
            view_rows = conn.execute(
                text(
                    "SELECT document_id FROM disclosure_ops.pending_parse_v1 "
                    "WHERE document_id = :id"
                ),
                {"id": document_id},
            ).all()
            helper_rows = queries.pending_parse(conn, max_retries=3, limit=1000)
        self.assertEqual(len(view_rows), 1, "view exposes the fact row")
        self.assertNotIn(
            document_id, [row["document_id"] for row in helper_rows],
            "helper must exclude non-retryable failures",
        )

    def test_pending_parse_helper_excludes_exhausted_retries(self) -> None:
        # Pinned negative #3: failed count >= max excluded by the helper.
        with self.engine.begin() as conn:
            document_id = self._insert_document(conn, status="parse_failed")
            for _ in range(3):
                self._insert_run(
                    conn,
                    document_id,
                    status="failed",
                    error={"stage": "parse", "error_code": "T", "retryable": True},
                )
            helper_rows = queries.pending_parse(conn, max_retries=3, limit=1000)
        self.assertNotIn(document_id, [row["document_id"] for row in helper_rows])

    def test_processing_backlog_counts_download_and_all_raw_parse_work(self) -> None:
        """GPU outage pressure cannot disappear as downloads become raw files."""

        conn = self.engine.connect()
        txn = conn.begin()
        try:
            before = queries.pending_processing_backlog_count(conn, max_retries=3)
            candidate_id = f"backlog{self.suffix}candidate"
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.source_access "
                    "(source_access_id, provider, provider_interface, accessed_at, "
                    " status, result_snapshot) VALUES "
                    "(:id, 'cninfo', 'cninfo:p_info3015', now(), 'ok', "
                    " CAST(:snapshot AS jsonb))"
                ),
                {
                    "id": f"sa_qv{self.suffix}backlog",
                    "snapshot": json.dumps(
                        {
                            "candidates": [
                                {
                                    "provider_document_id": candidate_id,
                                    "title": "待下载年度报告",
                                    "raw_category": "010301",
                                    "download_url": "http://x/backlog.PDF",
                                    "announcement_date": "1990-01-01",
                                }
                            ]
                        }
                    ),
                },
            )
            # Dead-letter and oversized raw files are not parse-eligible, but
            # they still occupy disk and therefore hold the admission gate.
            dead = self._insert_document(conn, status="parse_failed")
            self._insert_run(
                conn,
                dead,
                status="failed",
                error={"stage": "parse", "error_code": "poison", "retryable": False},
            )
            oversized = f"doc_qv{self.suffix}oversized"
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document "
                    "(document_id, status, provider, provider_document_id, "
                    " provider_metadata) VALUES "
                    "(:id, 'registered', 'cninfo', :pid, "
                    " '{\"oversized\": true}'::jsonb)"
                ),
                {"id": oversized, "pid": f"oversized{self.suffix}"},
            )
            self.doc_ids.append(oversized)

            after = queries.pending_processing_backlog_count(conn, max_retries=3)
        finally:
            txn.rollback()
            conn.close()

        self.assertEqual(after - before, 3)

    def test_sync_due_includes_company_without_checkpoint(self) -> None:
        with self.engine.begin() as conn:
            self.company_id = f"co_qv{self.suffix}"
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company (company_id, legal_name) "
                    "VALUES (:id, :name)"
                ),
                {"id": self.company_id, "name": f"队列视图测试公司{self.suffix}"},
            )
            self.tracked_id = f"tc_qv{self.suffix}"
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.tracked_company "
                    "(tracked_company_id, company_id, status) VALUES (:id, :co, 'active')"
                ),
                {"id": self.tracked_id, "co": self.company_id},
            )
            rows = queries.sync_due(conn, interval_seconds=86400, limit=1000)
        match = [row for row in rows if row["company_id"] == self.company_id]
        self.assertEqual(len(match), 1)
        self.assertIsNone(match[0]["window_end"])

    def test_fresh_checkpoint_is_not_due_and_lifecycle_view_exposes_it(self) -> None:
        company_id = f"co_qv{self.suffix}freshcheckpoint"
        tracked_id = f"tc_qv{self.suffix}freshcheckpoint"
        scope_key = f"{company_id}:p_info3015"
        conn = self.engine.connect()
        txn = conn.begin()
        try:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company (company_id, legal_name) "
                    "VALUES (:company, '新鲜游标公司')"
                ),
                {"company": company_id},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.tracked_company "
                    "(tracked_company_id, company_id, status) "
                    "VALUES (:tracked, :company, 'active')"
                ),
                {"tracked": tracked_id, "company": company_id},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.source_checkpoint "
                    "(source_checkpoint_id, provider, scope_key, cursor, updated_at) "
                    "VALUES (:checkpoint, 'cninfo', :scope, "
                    "'{\"window_end\": \"2026-07-13\"}', now())"
                ),
                {"checkpoint": f"cp_qv{self.suffix}", "scope": scope_key},
            )

            due = queries.sync_due(conn, interval_seconds=86400, limit=1000)
            lifecycle = conn.execute(
                text(
                    "SELECT last_synced_at, synced_through "
                    "FROM disclosure_public.tracked_companies_v1 "
                    "WHERE tracked_company_id = :tracked"
                ),
                {"tracked": tracked_id},
            ).mappings().one()
        finally:
            txn.rollback()
            conn.close()

        self.assertNotIn(company_id, [row["company_id"] for row in due])
        self.assertIsNotNone(lifecycle["last_synced_at"])
        self.assertEqual(str(lifecycle["synced_through"]), "2026-07-13")

    def test_candidate_code_audit_prefers_older_nonempty_api_over_newer_web_empty(self) -> None:
        provider_document_id = f"f006{self.suffix}"
        code = f"UNMAPPED{self.suffix[:8]}"
        conn = self.engine.connect()
        txn = conn.begin()
        try:
            for source_id, interface, accessed_at, raw_category in (
                (
                    f"sa_qv{self.suffix}f006api",
                    "cninfo:p_info3015",
                    datetime.now(timezone.utc) - timedelta(minutes=1),
                    code,
                ),
                (
                    f"sa_qv{self.suffix}f006web",
                    "cninfo:hisAnnouncement",
                    datetime.now(timezone.utc),
                    "",
                ),
            ):
                conn.execute(
                    text(
                        "INSERT INTO disclosure_core.source_access "
                        "(source_access_id, provider, provider_interface, accessed_at, "
                        " status, result_snapshot) VALUES "
                        "(:id, 'cninfo', :interface, :accessed_at, 'ok', "
                        " CAST(:snapshot AS jsonb))"
                    ),
                    {
                        "id": source_id,
                        "interface": interface,
                        "accessed_at": accessed_at,
                        "snapshot": json.dumps(
                            {
                                "candidates": [
                                    {
                                        "provider_document_id": provider_document_id,
                                        "raw_category": raw_category,
                                    }
                                ]
                            }
                        ),
                    },
                )
            counts = queries.candidate_code_counts(conn)
        finally:
            txn.rollback()
            conn.close()

        self.assertEqual(counts[code], 1)

    def test_sync_due_failure_cooldown_preserves_first_sync_fairness(self) -> None:
        failed_company = f"co_qv{self.suffix}000failed"
        untouched_company = f"co_qv{self.suffix}999fresh"
        failure_access = f"sa_qv{self.suffix}syncfailure"
        conn = self.engine.connect()
        txn = conn.begin()
        try:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company (company_id, legal_name) VALUES "
                    "(:failed, '失败公司'), (:fresh, '未尝试公司')"
                ),
                {"failed": failed_company, "fresh": untouched_company},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.tracked_company "
                    "(tracked_company_id, company_id, status) VALUES "
                    "(:failed_id, :failed, 'active'), "
                    "(:fresh_id, :fresh, 'active')"
                ),
                {
                    "failed_id": f"tc_qv{self.suffix}failed",
                    "fresh_id": f"tc_qv{self.suffix}fresh",
                    "failed": failed_company,
                    "fresh": untouched_company,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.source_access "
                    "(source_access_id, provider, provider_interface, accessed_at, "
                    " status, company_id) VALUES "
                    "(:id, 'cninfo', 'cninfo:worker_sync_failure', now(), "
                    " 'failed', :company)"
                ),
                {"id": failure_access, "company": failed_company},
            )
            cooled = queries.sync_due(conn, interval_seconds=86400, limit=1000)
            conn.execute(
                text(
                    "UPDATE disclosure_core.source_access "
                    "SET accessed_at = now() - interval '2 minutes' "
                    "WHERE source_access_id = :id"
                ),
                {"id": failure_access},
            )
            retriable = queries.sync_due(conn, interval_seconds=86400, limit=1000)
        finally:
            txn.rollback()
            conn.close()

        cooled_ids = [row["company_id"] for row in cooled]
        retriable_ids = [row["company_id"] for row in retriable]
        self.assertNotIn(failed_company, cooled_ids)
        self.assertIn(untouched_company, cooled_ids)
        self.assertLess(
            retriable_ids.index(untouched_company),
            retriable_ids.index(failed_company),
        )

    def test_pending_download_excludes_registered_and_terminal_failures(self) -> None:
        pid_new = f"qvdl{self.suffix}new"
        pid_done = f"qvdl{self.suffix}done"
        pid_dead = f"qvdl{self.suffix}dead"

        def candidate(pid: str) -> dict:
            return {
                "provider_document_id": pid,
                "title": f"测试公告 {pid}",
                "download_url": f"http://static.cninfo.com.cn/{pid}.PDF",
                "announcement_date": "1990-01-01",
                "security_code": "T08QV",
                "exchange": "LOCAL",
                "filing_type": "other",
                "report_period": None,
                "file_signature_hint": {"file_size": 10},
            }

        with self.engine.begin() as conn:
            sa_id = f"sa_qv{self.suffix}idx"
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.source_access "
                    "(source_access_id, provider, provider_interface, accessed_at, "
                    " status, result_snapshot) "
                    "VALUES (:id, 'cninfo', 'cninfo:p_info3015', now(), 'ok', "
                    "        CAST(:snap AS jsonb))"
                ),
                {
                    "id": sa_id,
                    "snap": json.dumps(
                        {
                            "result": "ok",
                            "candidates": [
                                candidate(pid_new),
                                candidate(pid_done),
                                candidate(pid_dead),
                            ],
                        }
                    ),
                },
            )
            self.sa_ids.append(sa_id)
            # pid_done is already registered → excluded.
            done_doc = f"doc_qv{self.suffix}done"
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document "
                    "(document_id, status, provider, provider_document_id, provider_metadata) "
                    "VALUES (:id, 'registered', 'cninfo', :pid, '{}'::jsonb)"
                ),
                {"id": done_doc, "pid": pid_done},
            )
            self.doc_ids.append(done_doc)
            # pid_dead has a non-retryable download failure → excluded.
            sa_fail = f"sa_qv{self.suffix}fail"
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.source_access "
                    "(source_access_id, provider, provider_interface, accessed_at, "
                    " status, query_params, error) "
                    "VALUES (:id, 'cninfo', 'cninfo:download_pdf', now(), 'failed', "
                    "        CAST(:qp AS jsonb), :err)"
                ),
                {
                    "id": sa_fail,
                    "qp": json.dumps({"provider_document_id": pid_dead}),
                    "err": json.dumps(
                        {
                            "stage": "download",
                            "error_code": "invalid_raw_document",
                            "retryable": False,
                            "provider_document_id": pid_dead,
                        }
                    ),
                },
            )
            self.sa_ids.append(sa_fail)
            rows = queries.pending_downloads(conn, max_retries=3, limit=1000)

        pids = [row["provider_document_id"] for row in rows]
        self.assertIn(pid_new, pids)
        self.assertNotIn(pid_done, pids)
        self.assertNotIn(pid_dead, pids)
        row = next(row for row in rows if row["provider_document_id"] == pid_new)
        self.assertEqual(row["candidate"]["title"], f"测试公告 {pid_new}")

    def test_pending_download_skips_paused_companies(self) -> None:
        # Pool membership drives acquisition: active row = eligible; paused
        # row = blocked (round19: 停止一切获取, queued backlog included);
        # NO tracked row (round22 untrack) = blocked too — deleting the pool
        # row must not re-open the backlog. Rollback keeps the shared DB
        # untouched.
        pid_active = f"qvdl{self.suffix}act"
        pid_paused = f"qvdl{self.suffix}pau"
        pid_untracked = f"qvdl{self.suffix}unt"
        conn = self.engine.connect()
        txn = conn.begin()
        try:
            for label, pid, status in (
                ("act", pid_active, "active"),
                ("pau", pid_paused, "paused"),
                ("unt", pid_untracked, None),
            ):
                company_id = f"co_qv{self.suffix}{label}"
                conn.execute(
                    text(
                        "INSERT INTO disclosure_core.company (company_id, legal_name) "
                        "VALUES (:cid, :name)"
                    ),
                    {"cid": company_id, "name": f"QV{label}公司"},
                )
                if status is not None:
                    conn.execute(
                        text(
                            "INSERT INTO disclosure_core.tracked_company "
                            "(tracked_company_id, company_id, status) "
                            "VALUES (:tid, :cid, :status)"
                        ),
                        {
                            "tid": f"tc_qv{self.suffix}{label}",
                            "cid": company_id,
                            "status": status,
                        },
                    )
                conn.execute(
                    text(
                        "INSERT INTO disclosure_core.source_access "
                        "(source_access_id, provider, provider_interface, accessed_at, "
                        " status, company_id, result_snapshot) "
                        "VALUES (:id, 'cninfo', 'cninfo:p_info3015', now(), 'ok', "
                        "        :cid, CAST(:snap AS jsonb))"
                    ),
                    {
                        "id": f"sa_qv{self.suffix}{label}",
                        "cid": company_id,
                        "snap": json.dumps(
                            {
                                "result": "ok",
                                "candidates": [
                                    {
                                        "provider_document_id": pid,
                                        "title": f"测试公告 {pid}",
                                        "download_url": f"http://x/{pid}.PDF",
                                        "announcement_date": "1990-01-01",
                                    }
                                ],
                            }
                        ),
                    },
                )
            pids = [
                row["provider_document_id"]
                for row in queries.pending_downloads(conn, max_retries=3, limit=1000)
            ]
            self.assertIn(pid_active, pids)
            self.assertNotIn(pid_paused, pids)
            self.assertNotIn(pid_untracked, pids)
        finally:
            txn.rollback()
            conn.close()

    def test_pending_download_scope_filters_by_class_and_title(self) -> None:
        # round20: download-layer scoping — coded candidates gate on class
        # rules, code-less candidates on title rules. Rollback, no residue.
        pid_core = f"qvdl{self.suffix}core"
        pid_gov = f"qvdl{self.suffix}gov"
        pid_titled = f"qvdl{self.suffix}ttl"
        conn = self.engine.connect()
        txn = conn.begin()
        try:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.classification_rule "
                    "(rule_set, prefix, value, priority, version) VALUES "
                    "('class', '011301', 'dividend', 68, 'test'), "
                    "('class', '0131', 'governance_rules', 16, 'test'), "
                    "('title', '年度报告', 'annual_report', 998, 'test') "
                    "ON CONFLICT (rule_set, prefix, value) DO NOTHING"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.source_access "
                    "(source_access_id, provider, provider_interface, accessed_at, "
                    " status, result_snapshot) "
                    "VALUES (:id, 'cninfo', 'cninfo:p_info3015', now(), 'ok', "
                    "        CAST(:snap AS jsonb))"
                ),
                {
                    "id": f"sa_qv{self.suffix}scope",
                    "snap": json.dumps(
                        {
                            "result": "ok",
                            "candidates": [
                                {
                                    "provider_document_id": pid_core,
                                    "title": "分红公告",
                                    "raw_category": "01010503||011301",
                                    "download_url": "http://x/a.PDF",
                                    "announcement_date": "1990-01-01",
                                },
                                {
                                    "provider_document_id": pid_gov,
                                    "title": "章程修订",
                                    "raw_category": "01010503||013101",
                                    "download_url": "http://x/b.PDF",
                                    "announcement_date": "1990-01-01",
                                },
                                {
                                    "provider_document_id": pid_titled,
                                    "title": "某公司2025年年度报告",
                                    "download_url": "http://x/c.PDF",
                                    "announcement_date": "1990-01-01",
                                },
                            ],
                        }
                    ),
                },
            )
            scope = ("dividend", "annual_report")
            pids = [
                row["provider_document_id"]
                for row in queries.pending_downloads(
                    conn, max_retries=3, limit=1000, scope_classes=scope
                )
            ]
            self.assertIn(pid_core, pids)
            self.assertNotIn(pid_gov, pids)
            self.assertIn(pid_titled, pids)  # code-less → title rules

            # Cascade: a company override REPLACES the global tuple — a
            # governance-only override excludes the dividend candidate.
            company_id = f"co_qv{self.suffix}ovr"
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company (company_id, legal_name) "
                    "VALUES (:cid, 'QV覆盖公司')"
                ),
                {"cid": company_id},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.tracked_company "
                    "(tracked_company_id, company_id, status, process_classes) "
                    "VALUES (:tid, :cid, 'active', '[\"governance_rules\"]'::jsonb)"
                ),
                {"tid": f"tc_qv{self.suffix}ovr", "cid": company_id},
            )
            conn.execute(
                text(
                    "UPDATE disclosure_core.source_access "
                    "SET company_id = :cid WHERE source_access_id = :sid"
                ),
                {"cid": company_id, "sid": f"sa_qv{self.suffix}scope"},
            )
            override_pids = [
                row["provider_document_id"]
                for row in queries.pending_downloads(
                    conn, max_retries=3, limit=1000, scope_classes=scope
                )
            ]
            self.assertNotIn(pid_core, override_pids)  # dividend not in override
            self.assertIn(pid_gov, override_pids)      # governance now in
            all_pids = [
                row["provider_document_id"]
                for row in queries.pending_downloads(conn, max_retries=3, limit=1000)
            ]
            self.assertIn(pid_gov, all_pids)  # scope None = everything
        finally:
            txn.rollback()
            conn.close()

    def test_processing_scope_edge_inputs_and_empty_override_inheritance(self) -> None:
        """Constructed scale-edge inputs pin the SQL gate's full truth table."""

        ids_by_case = {
            name: f"qvedge{self.suffix}{index}"
            for index, name in enumerate(
                (
                    "null_title_coded",
                    "null_title_codeless",
                    "long_title",
                    "whitespace_category",
                    "spaced_multicode",
                    "unknown_override",
                    "mixed_override",
                    "nonarray_override",
                )
            )
        }
        conn = self.engine.connect()
        txn = conn.begin()
        try:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.classification_rule "
                    "(rule_set, prefix, value, priority, version) VALUES "
                    "('class', '011301', 'dividend', 68, 'test'), "
                    "('class', '0131', 'governance_rules', 16, 'test'), "
                    "('title', '年度报告', 'annual_report', 998, 'test') "
                    "ON CONFLICT (rule_set, prefix, value) DO NOTHING"
                )
            )
            inherited_company = f"co_qv{self.suffix}empty"
            unknown_company = f"co_qv{self.suffix}unknown"
            mixed_company = f"co_qv{self.suffix}mixed"
            nonarray_company = f"co_qv{self.suffix}shape"
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company (company_id, legal_name) "
                    "VALUES (:inherit, '空覆盖'), (:unknown, '未知覆盖'), "
                    "(:mixed, '混合覆盖'), (:nonarray, '异形覆盖')"
                ),
                {
                    "inherit": inherited_company,
                    "unknown": unknown_company,
                    "mixed": mixed_company,
                    "nonarray": nonarray_company,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.tracked_company "
                    "(tracked_company_id, company_id, status, process_classes) VALUES "
                    "(:ti, :inherit, 'active', '[]'::jsonb), "
                    "(:tu, :unknown, 'active', '[\"not_a_class\"]'::jsonb), "
                    "(:tm, :mixed, 'active', "
                    " '[\"dividend\",\"not_a_class\"]'::jsonb), "
                    "(:tn, :nonarray, 'active', '{\"class\":\"dividend\"}'::jsonb)"
                ),
                {
                    "ti": f"tc_qv{self.suffix}empty",
                    "tu": f"tc_qv{self.suffix}unknown",
                    "tm": f"tc_qv{self.suffix}mixed",
                    "tn": f"tc_qv{self.suffix}shape",
                    "inherit": inherited_company,
                    "unknown": unknown_company,
                    "mixed": mixed_company,
                    "nonarray": nonarray_company,
                },
            )
            inherited_candidates = [
                {
                    "provider_document_id": ids_by_case["null_title_coded"],
                    "title": None,
                    "raw_category": "011301",
                    "download_url": "http://x/e1.PDF",
                    "announcement_date": "1990-01-01",
                },
                {
                    "provider_document_id": ids_by_case["null_title_codeless"],
                    "title": None,
                    "raw_category": "",
                    "download_url": "http://x/e2.PDF",
                    "announcement_date": "1990-01-01",
                },
                {
                    "provider_document_id": ids_by_case["long_title"],
                    "title": "x" * 100_000 + "年度报告",
                    "download_url": "http://x/e3.PDF",
                    "announcement_date": "1990-01-01",
                },
                {
                    "provider_document_id": ids_by_case["whitespace_category"],
                    "title": "2025年年度报告",
                    "raw_category": "   ",
                    "download_url": "http://x/e4.PDF",
                    "announcement_date": "1990-01-01",
                },
                {
                    "provider_document_id": ids_by_case["spaced_multicode"],
                    "title": "分红公告",
                    "raw_category": " 013101 || 011301 ",
                    "download_url": "http://x/e5.PDF",
                    "announcement_date": "1990-01-01",
                },
            ]
            for source_id, company_id, candidates in (
                (
                    f"sa_qv{self.suffix}empty",
                    inherited_company,
                    inherited_candidates,
                ),
                (
                    f"sa_qv{self.suffix}unknown",
                    unknown_company,
                    [
                        {
                            "provider_document_id": ids_by_case["unknown_override"],
                            "title": "分红公告",
                            "raw_category": "011301",
                            "download_url": "http://x/e6.PDF",
                            "announcement_date": "1990-01-01",
                        }
                    ],
                ),
                (
                    f"sa_qv{self.suffix}mixed",
                    mixed_company,
                    [
                        {
                            "provider_document_id": ids_by_case["mixed_override"],
                            "title": "分红公告",
                            "raw_category": "011301",
                            "download_url": "http://x/e7.PDF",
                            "announcement_date": "1990-01-01",
                        }
                    ],
                ),
                (
                    f"sa_qv{self.suffix}shape",
                    nonarray_company,
                    [
                        {
                            "provider_document_id": ids_by_case["nonarray_override"],
                            "title": "分红公告",
                            "raw_category": "011301",
                            "download_url": "http://x/e8.PDF",
                            "announcement_date": "1990-01-01",
                        }
                    ],
                ),
            ):
                conn.execute(
                    text(
                        "INSERT INTO disclosure_core.source_access "
                        "(source_access_id, provider, provider_interface, accessed_at, "
                        " status, result_snapshot, company_id) VALUES "
                        "(:id, 'cninfo', 'cninfo:p_info3015', now(), 'ok', "
                        " CAST(:snapshot AS jsonb), :company)"
                    ),
                    {
                        "id": source_id,
                        "snapshot": json.dumps(
                            {"result": "ok", "candidates": candidates}
                        ),
                        "company": company_id,
                    },
                )
            actual = {
                row["provider_document_id"]
                for row in queries.pending_downloads(
                    conn,
                    max_retries=3,
                    limit=1000,
                    scope_classes=("dividend", "annual_report"),
                )
            }
        finally:
            txn.rollback()
            conn.close()

        self.assertIn(ids_by_case["null_title_coded"], actual)
        self.assertNotIn(ids_by_case["null_title_codeless"], actual)
        self.assertIn(ids_by_case["long_title"], actual)
        self.assertIn(ids_by_case["whitespace_category"], actual)
        self.assertIn(ids_by_case["spaced_multicode"], actual)
        self.assertNotIn(ids_by_case["unknown_override"], actual)
        self.assertNotIn(ids_by_case["mixed_override"], actual)
        self.assertNotIn(ids_by_case["nonarray_override"], actual)

    def test_pending_download_carrier_and_title_topic_gate(self) -> None:
        # Carrier precedence: a 0129-coded legal opinion rides equity_incentive
        # codes but must NOT download unless intermediary_report itself is in
        # scope. title_topic: a coded 销售简报 whose codes miss the scope still
        # downloads when the topic class is in scope (0021).
        pid_carrier = f"qvdl{self.suffix}car"
        pid_topic = f"qvdl{self.suffix}top"
        pid_codeless_carrier = f"qvdl{self.suffix}clc"
        pid_web_titled = f"qvdl{self.suffix}web"
        conn = self.engine.connect()
        txn = conn.begin()
        try:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.classification_rule "
                    "(rule_set, prefix, value, priority, version) VALUES "
                    "('class', '012325', 'equity_incentive', 76, 'test'), "
                    "('class', '0129', 'intermediary_report', 18, 'test'), "
                    "('class', '0131', 'governance_rules', 16, 'test'), "
                    "('title', '年度报告', 'annual_report', 998, 'test'), "
                    "('title', '法律意见书', 'intermediary_report', 1000, 'test'), "
                    "('title_topic', '销售简报', 'operating_data', 95, 'test') "
                    "ON CONFLICT (rule_set, prefix, value) DO NOTHING"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.source_access "
                    "(source_access_id, provider, provider_interface, accessed_at, "
                    " status, result_snapshot) "
                    "VALUES (:id, 'cninfo', 'cninfo:p_info3015', now(), 'ok', "
                    "        CAST(:snap AS jsonb))"
                ),
                {
                    "id": f"sa_qv{self.suffix}carrier",
                    "snap": json.dumps(
                        {
                            "result": "ok",
                            "candidates": [
                                {
                                    "provider_document_id": pid_carrier,
                                    "title": "关于股权激励计划的法律意见书",
                                    "raw_category": "01010503||012325||012901",
                                    "download_url": "http://x/c1.PDF",
                                    "announcement_date": "1990-01-01",
                                },
                                {
                                    "provider_document_id": pid_topic,
                                    "title": "某公司2026年6月销售简报",
                                    "raw_category": "01010503||013101",
                                    "download_url": "http://x/c2.PDF",
                                    "announcement_date": "1990-01-01",
                                },
                                {
                                    "provider_document_id": pid_codeless_carrier,
                                    "title": "关于2025年年度报告的法律意见书",
                                    "download_url": "http://x/c3.PDF",
                                    "announcement_date": "1990-01-01",
                                },
                                {
                                    # Web-channel snapshots store raw_category
                                    # as '' — must route to the title branch
                                    # (NULLIF), not the coded branch.
                                    "provider_document_id": pid_web_titled,
                                    "title": "某公司2025年年度报告",
                                    "raw_category": "",
                                    "download_url": "http://x/c4.PDF",
                                    "announcement_date": "1990-01-01",
                                },
                            ],
                        }
                    ),
                },
            )
            scope = ("equity_incentive", "operating_data", "annual_report")
            pids = [
                row["provider_document_id"]
                for row in queries.pending_downloads(
                    conn, max_retries=3, limit=1000, scope_classes=scope
                )
            ]
            self.assertNotIn(pid_carrier, pids)  # carrier code outside scope
            self.assertIn(pid_topic, pids)  # topic hit despite gov-only codes
            self.assertNotIn(pid_codeless_carrier, pids)  # carrier title hit
            self.assertIn(pid_web_titled, pids)  # '' raw_category → title branch

            # Opting the carrier class into scope re-admits both carriers.
            optin = scope + ("intermediary_report",)
            optin_pids = [
                row["provider_document_id"]
                for row in queries.pending_downloads(
                    conn, max_retries=3, limit=1000, scope_classes=optin
                )
            ]
            self.assertIn(pid_carrier, optin_pids)
            self.assertIn(pid_codeless_carrier, optin_pids)

            # Per-company override (REPLACE semantics): a company whose
            # process_classes is ["intermediary_report"] opts its carriers
            # back in even though the global scope excludes them — and
            # loses everything else.
            company_id = f"co_qv{self.suffix}car"
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company (company_id, legal_name) "
                    "VALUES (:cid, 'QV载体覆盖公司')"
                ),
                {"cid": company_id},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.tracked_company "
                    "(tracked_company_id, company_id, status, process_classes) "
                    "VALUES (:tid, :cid, 'active', "
                    "'[\"intermediary_report\"]'::jsonb)"
                ),
                {"tid": f"tc_qv{self.suffix}car", "cid": company_id},
            )
            conn.execute(
                text(
                    "UPDATE disclosure_core.source_access "
                    "SET company_id = :cid WHERE source_access_id = :sid"
                ),
                {"cid": company_id, "sid": f"sa_qv{self.suffix}carrier"},
            )
            override_pids = [
                row["provider_document_id"]
                for row in queries.pending_downloads(
                    conn, max_retries=3, limit=1000, scope_classes=scope
                )
            ]
            self.assertIn(pid_carrier, override_pids)
            self.assertNotIn(pid_topic, override_pids)  # replaced, not merged
        finally:
            txn.rollback()
            conn.close()

    def test_pending_queues_noise_gate_is_absolute(self) -> None:
        # Phase-1 ruling: a title_noise hit excludes the row from download
        # AND parse even when its codes are squarely in scope, and even for
        # a per-company override.
        pid_noise = f"qvdl{self.suffix}nz"
        pid_clean = f"qvdl{self.suffix}cl"
        pid_mtn_terms = f"qvdl{self.suffix}mtn"
        pid_redemption_warning = f"qvdl{self.suffix}redeem"
        pid_downward_revision_warning = f"qvdl{self.suffix}revise"
        conn = self.engine.connect()
        txn = conn.begin()
        try:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.classification_rule "
                    "(rule_set, prefix, value, priority, version) VALUES "
                    "('class', '0111', 'financing', 48, 'test'), "
                    "('class', '0109', 'convertible_bond', 94, 'test'), "
                    "('title_noise', '募集资金存放', 'noise', 0, 'test') "
                    "ON CONFLICT (rule_set, prefix, value) DO NOTHING"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.source_access "
                    "(source_access_id, provider, provider_interface, accessed_at, "
                    " status, result_snapshot) "
                    "VALUES (:id, 'cninfo', 'cninfo:p_info3015', now(), 'ok', "
                    "        CAST(:snap AS jsonb))"
                ),
                {
                    "id": f"sa_qv{self.suffix}noise",
                    "snap": json.dumps(
                        {
                            "result": "ok",
                            "candidates": [
                                {
                                    "provider_document_id": pid_noise,
                                    "title": "2025年度募集资金存放与使用情况的专项报告",
                                    "raw_category": "01010503||011101",
                                    "download_url": "http://x/n1.PDF",
                                    "announcement_date": "1990-01-01",
                                },
                                {
                                    "provider_document_id": pid_clean,
                                    "title": "关于向银行申请借款的公告",
                                    "raw_category": "01010503||011101",
                                    "download_url": "http://x/n2.PDF",
                                    "announcement_date": "1990-01-01",
                                },
                                {
                                    "provider_document_id": pid_mtn_terms,
                                    "title": "招商银行：[H股公告]招商银行股份有限公司伦敦分行"
                                    "在招商银行股份有限公司的50亿美元中期票据计划下发行于"
                                    "2029年到期的人民币30亿元票息为1.73%的票据",
                                    "raw_category": "01010503||010113||011103||012399",
                                    "download_url": "http://x/1225149847.PDF",
                                    "announcement_date": "2025-04-23",
                                },
                                {
                                    "provider_document_id": pid_redemption_warning,
                                    "title": "晶科能源关于“晶能转债”预计满足赎回条件的提示性公告",
                                    "raw_category": "01010503||010123||010919",
                                    "download_url": "http://x/1225006849.PDF",
                                    "announcement_date": "2025-04-03",
                                },
                                {
                                    "provider_document_id": pid_downward_revision_warning,
                                    "title": "温氏股份关于预计触发可转债转股价格"
                                    "向下修正条件的提示性公告",
                                    "raw_category": "01010503||010112||010115||010915",
                                    "download_url": "http://x/1225343892.PDF",
                                    "announcement_date": "2026-06-01",
                                },
                            ],
                        }
                    ),
                },
            )
            pids = [
                row["provider_document_id"]
                for row in queries.pending_downloads(
                    conn,
                    max_retries=3,
                    limit=1000,
                    scope_classes=("financing", "convertible_bond"),
                )
            ]
            self.assertNotIn(pid_noise, pids)
            self.assertIn(pid_clean, pids)
            self.assertIn(
                pid_mtn_terms,
                pids,
                "pid 1225149847 is substantive financing terms, not title noise",
            )
            self.assertIn(
                pid_redemption_warning,
                pids,
                "pid 1225006849 is the first redemption trigger signal",
            )
            self.assertIn(
                pid_downward_revision_warning,
                pids,
                "pid 1225343892 is a forward-looking dilution signal",
            )

            # Parse side, same guard, and status registered.
            doc_noise = self._insert_document(conn, status="registered")
            conn.execute(
                text(
                    "UPDATE disclosure_core.document SET provider_metadata = "
                    "jsonb_build_object('raw_category', CAST('01010503||011101' AS text)), "
                    "title = '2024年度募集资金存放与实际使用情况的专项报告' "
                    "WHERE document_id = :id"
                ),
                {"id": doc_noise},
            )
            parse_ids = [
                row["document_id"]
                for row in queries.pending_parse(
                    conn, max_retries=3, limit=1000, scope_classes=("financing",)
                )
            ]
            self.assertNotIn(doc_noise, parse_ids)
        finally:
            txn.rollback()
            conn.close()

    def test_pending_parse_carrier_gate_matches_download_gate(self) -> None:
        # One processing surface: the parse queue applies the same carrier
        # guard, title_topic eligibility, and ''→title-branch routing (the
        # web channel persists provider_metadata raw_category as '').
        conn = self.engine.connect()
        txn = conn.begin()
        try:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.classification_rule "
                    "(rule_set, prefix, value, priority, version) VALUES "
                    "('class', '012325', 'equity_incentive', 76, 'test'), "
                    "('class', '0131', 'governance_rules', 16, 'test'), "
                    "('class', '0129', 'intermediary_report', 18, 'test'), "
                    "('title', '年度报告', 'annual_report', 998, 'test'), "
                    "('title_topic', '销售简报', 'operating_data', 95, 'test') "
                    "ON CONFLICT (rule_set, prefix, value) DO NOTHING"
                )
            )
            doc_carrier = self._insert_document(conn, status="registered")
            doc_subject = self._insert_document(conn, status="registered")
            doc_web = self._insert_document(conn, status="registered")
            doc_topic = self._insert_document(conn, status="registered")
            for raw, title, doc_id in (
                ("01010503||012325||012901", "法律意见书", doc_carrier),
                ("01010503||012325", "激励公告", doc_subject),
                ("", "某公司2025年年度报告", doc_web),
                ("01010503||013101", "2026年6月销售简报", doc_topic),
            ):
                conn.execute(
                    text(
                        "UPDATE disclosure_core.document SET provider_metadata = "
                        "jsonb_build_object('raw_category', CAST(:raw AS text)), "
                        "title = :title WHERE document_id = :id"
                    ),
                    {"raw": raw, "title": title, "id": doc_id},
                )
            scope = ("equity_incentive", "annual_report", "operating_data")
            doc_ids = [
                row["document_id"]
                for row in queries.pending_parse(
                    conn, max_retries=3, limit=1000, scope_classes=scope
                )
            ]
            self.assertNotIn(doc_carrier, doc_ids)
            self.assertIn(doc_subject, doc_ids)
            self.assertIn(doc_web, doc_ids)  # '' raw_category → title branch
            self.assertIn(doc_topic, doc_ids)  # topic hit despite gov codes
        finally:
            txn.rollback()
            conn.close()

    def test_stale_reclaim_fails_only_over_threshold_runs(self) -> None:
        with self.engine.begin() as conn:
            document_id = self._insert_document(conn, status="parsed")
            old_run = self._insert_run(
                conn,
                document_id,
                status="running",
                started_at=datetime.now(timezone.utc) - timedelta(hours=3),
            )
            fresh_run = self._insert_run(conn, document_id, status="running")
            reclaimed = queries.reclaim_stale_runs(conn, threshold_seconds=3600)
            rows = dict(
                conn.execute(
                    text(
                        "SELECT processing_run_id, status FROM disclosure_core.processing_run "
                        "WHERE processing_run_id = ANY(:ids)"
                    ),
                    {"ids": [old_run, fresh_run]},
                ).all()
            )
        self.assertGreaterEqual(reclaimed, 1)
        self.assertEqual(rows[old_run], "failed")
        self.assertEqual(rows[fresh_run], "running")
        with self.engine.connect() as conn:
            error = conn.execute(
                text(
                    "SELECT error FROM disclosure_core.processing_run "
                    "WHERE processing_run_id = :id"
                ),
                {"id": old_run},
            ).scalar_one()
        self.assertEqual(error["error_code"], "stale_reclaimed")
        self.assertTrue(error["retryable"])


if __name__ == "__main__":
    unittest.main()
