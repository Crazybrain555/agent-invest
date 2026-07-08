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
        self.sa_ids: list[str] = []
        self.company_id: str | None = None
        self.tracked_id: str | None = None

    def tearDown(self) -> None:
        with self.engine.begin() as conn:
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

    def test_pending_publish_enqueues_document_without_active_run(self) -> None:
        # Pinned negative #1 inverse: no active run at all → still enqueued.
        with self.engine.begin() as conn:
            document_id = self._insert_document(conn, status="parsed")
            run_id = self._insert_run(
                conn, document_id, status="succeeded", unit_build_status="succeeded"
            )
            rows = queries.pending_publish(conn, limit=1000)
        self.assertIn(run_id, [row["processing_run_id"] for row in rows])

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
        # round19: paused = 停止一切获取 — queued backlog included. Rollback
        # keeps the shared DB untouched.
        pid_active = f"qvdl{self.suffix}act"
        pid_paused = f"qvdl{self.suffix}pau"
        conn = self.engine.connect()
        txn = conn.begin()
        try:
            for label, pid, status in (
                ("act", pid_active, "active"),
                ("pau", pid_paused, "paused"),
            ):
                company_id = f"co_qv{self.suffix}{label}"
                conn.execute(
                    text(
                        "INSERT INTO disclosure_core.company (company_id, legal_name) "
                        "VALUES (:cid, :name)"
                    ),
                    {"cid": company_id, "name": f"QV{label}公司"},
                )
                conn.execute(
                    text(
                        "INSERT INTO disclosure_core.tracked_company "
                        "(tracked_company_id, company_id, status) "
                        "VALUES (:tid, :cid, :status)"
                    ),
                    {"tid": f"tc_qv{self.suffix}{label}", "cid": company_id, "status": status},
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
