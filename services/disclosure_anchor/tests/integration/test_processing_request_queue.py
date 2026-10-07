"""Explicit processing requests on the managed scratch DB (0066).

Exact-filing admission must reach the real queue predicates — the staged-v4
candidate source and its locked H0-shaped recheck included — while pool,
taxonomy and retry gates keep every other row where it was. Requests must be
durable across "restarts" and resolved by the worker resolver without any
client re-POST. Runs only under ``make test-integration``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import threading
from typing import Any
import unittest

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from disclosure_anchor.adapters.db.postgres.processing_requests import (
    PostgresProcessingStatusReader,
)
from disclosure_anchor.adapters.db.postgres.staged_new_work_v4 import (
    PostgresV4OrdinaryParseCandidateSource,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import (
    processing_request_uow_factory,
)
from disclosure_anchor.application.contracts.processing_request import normalize_intent
from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureWindow,
    SourceSecurity,
)
from disclosure_anchor.application.use_cases.processing_requests import (
    ProcessingRequestPolicy,
    ProcessingRequestResolver,
    ProcessingRequestService,
)
from disclosure_anchor.application.worker import queries
from disclosure_anchor.application.worker.locks import acquire_document_xact_lock
from disclosure_anchor.domain import ids
from tests.integration._support import engine_or_skip

SCOPE = ("annual_report",)
OFF_SCOPE_TITLE = "关于召开2026年第一次临时股东大会的通知"
HASH = "sha256:" + "c" * 64


def _code(seed: str) -> str:
    return "60" + str(int(seed[-8:], 36) % 10000).zfill(4)


class ProcessingRequestQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.suffix = ids.new_ulid().lower()
        self.code = _code(self.suffix)
        self.company_id = f"co_pr{self.suffix}"
        self.security_id = f"sec_pr{self.suffix}"
        self.tracked_id = f"trk_pr{self.suffix}"
        self.doc_ids: list[str] = []
        self.sa_ids: list[str] = []
        self.request_ids: list[str] = []

    def tearDown(self) -> None:
        with self.engine.begin() as conn:
            request_ids = list(self.request_ids)
            request_ids += [
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT request_id FROM disclosure_ops.processing_request "
                        "WHERE idempotency_key LIKE :prefix"
                    ),
                    {"prefix": f"it-{self.suffix}%"},
                )
            ]
            if request_ids:
                touched = [
                    value
                    for row in conn.execute(
                        text(
                            "SELECT profile_source_access_id, index_source_access_id "
                            "FROM disclosure_ops.processing_request_attempt "
                            "WHERE request_id = ANY(:ids)"
                        ),
                        {"ids": request_ids},
                    )
                    for value in row
                    if value is not None
                ]
                self.sa_ids.extend(touched)
                for table in ("processing_request_target", "processing_request_attempt",
                              "processing_request"):
                    conn.execute(
                        text(f"DELETE FROM disclosure_ops.{table} WHERE request_id = ANY(:ids)"),
                        {"ids": request_ids},
                    )
            if self.doc_ids:
                conn.execute(
                    text("DELETE FROM disclosure_core.processing_run WHERE document_id = ANY(:ids)"),
                    {"ids": self.doc_ids},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.document WHERE document_id = ANY(:ids)"),
                    {"ids": self.doc_ids},
                )
            if self.sa_ids:
                conn.execute(
                    text("DELETE FROM disclosure_core.company_identifier WHERE source_access_id = ANY(:ids)"),
                    {"ids": self.sa_ids},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.source_access WHERE source_access_id = ANY(:ids)"),
                    {"ids": self.sa_ids},
                )
            conn.execute(
                text("DELETE FROM disclosure_core.tracked_company WHERE company_id IN "
                     "(SELECT company_id FROM disclosure_core.security WHERE security_code = :code)"),
                {"code": self.code},
            )
            company_ids = [
                row[0]
                for row in conn.execute(
                    text("SELECT company_id FROM disclosure_core.security WHERE security_code = :code"),
                    {"code": self.code},
                )
            ]
            conn.execute(
                text("DELETE FROM disclosure_core.security WHERE security_code = :code"),
                {"code": self.code},
            )
            if company_ids:
                conn.execute(
                    text("DELETE FROM disclosure_core.company_identifier WHERE company_id = ANY(:ids)"),
                    {"ids": company_ids},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.company WHERE company_id = ANY(:ids)"),
                    {"ids": company_ids},
                )
        self.engine.dispose()

    # -- fixtures --------------------------------------------------------------
    def _subject(self, conn: Any, *, tracked_status: str | None = "paused") -> None:
        conn.execute(
            text("INSERT INTO disclosure_core.company (company_id, legal_name) VALUES (:id, :name)"),
            {"id": self.company_id, "name": f"请求测试公司{self.suffix}"},
        )
        conn.execute(
            text(
                "INSERT INTO disclosure_core.security (security_id, company_id, security_code, exchange) "
                "VALUES (:id, :company, :code, 'SSE')"
            ),
            {"id": self.security_id, "company": self.company_id, "code": self.code},
        )
        if tracked_status is not None:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.tracked_company "
                    "(tracked_company_id, company_id, security_id, status) "
                    "VALUES (:id, :company, :security, :status)"
                ),
                {"id": self.tracked_id, "company": self.company_id,
                 "security": self.security_id, "status": tracked_status},
            )

    def _document(self, conn: Any, *, title: str = OFF_SCOPE_TITLE, status: str = "registered") -> str:
        document_id = f"doc_pr{self.suffix}{len(self.doc_ids)}"
        pid = f"9{len(self.doc_ids)}{self.suffix[-6:]}"
        conn.execute(
            text(
                "INSERT INTO disclosure_core.document "
                "(document_id, company_id, security_id, status, title, provider, provider_document_id, "
                " provider_metadata, raw_file_relpath, raw_file_hash, announcement_date) "
                "VALUES (:id, :company, :security, :status, :title, 'cninfo', :pid, "
                " '{\"raw_category\": \"\"}'::jsonb, :relpath, :hash, DATE '2026-03-30')"
            ),
            {"id": document_id, "company": self.company_id, "security": self.security_id,
             "status": status, "title": title, "pid": pid,
             "relpath": f"raw_documents/cninfo/{self.code}/2026/{pid}/sha256_{'c' * 64}.pdf",
             "hash": HASH},
        )
        self.doc_ids.append(document_id)
        return document_id

    def _request(self, conn: Any, *, kind: str = "document") -> str:
        request_id = "dreq_" + ids.new_ulid()
        conn.execute(
            text(
                "INSERT INTO disclosure_ops.processing_request (request_id, idempotency_key, "
                " intent_sha256, contract_version, target_kind, target, enqueue_parse, requested_by) "
                "VALUES (:id, :key, :intent, 'disclosure-processing-request.v1', :kind, "
                " CAST(:target AS jsonb), true, 'integration')"
            ),
            {"id": request_id, "key": f"it-{self.suffix}-{ids.new_ulid()}", "intent": HASH,
             "kind": kind, "target": json.dumps({"kind": kind})},
        )
        self.request_ids.append(request_id)
        return request_id

    def _target(self, conn: Any, request_id: str, **values: Any) -> None:
        row = {"admit_download": False, "admit_parse": True, "provider": None,
               "provider_document_id": None, "document_id": None, "raw_file_hash": None,
               "index_source_access_id": None, "resolution_method": "document_id"}
        row.update(values)
        conn.execute(
            text(
                "INSERT INTO disclosure_ops.processing_request_target (target_id, request_id, "
                " resolution_method, provider, provider_document_id, document_id, raw_file_hash, "
                " index_source_access_id, admit_download, admit_parse) "
                "VALUES (:target_id, :request_id, :resolution_method, :provider, "
                " :provider_document_id, :document_id, :raw_file_hash, :index_source_access_id, "
                " :admit_download, :admit_parse)"
            ),
            {"target_id": "dprt_" + ids.new_ulid(), "request_id": request_id, **row},
        )

    def _eligible(self, conn: Any, **kwargs: Any) -> list[str]:
        return [
            row["document_id"]
            for row in queries.pending_parse(
                conn, max_retries=3, limit=500000, scope_classes=SCOPE,
                require_active_company_scope=True, **kwargs,
            )
        ]

    # -- parse admission ---------------------------------------------------------
    def test_exact_parse_admission_bypasses_pool_and_taxonomy_but_never_retry_gates(self) -> None:
        with self.engine.connect() as conn:
            txn = conn.begin()
            try:
                self._subject(conn, tracked_status="paused")
                target = self._document(conn)
                control = self._document(conn)
                self.assertNotIn(target, self._eligible(conn))
                request_id = self._request(conn)
                self._target(conn, request_id, document_id=target, raw_file_hash=HASH)
                eligible = self._eligible(conn)
                self.assertIn(target, eligible)
                self.assertNotIn(control, eligible)
                # The locked H0/source-rejection recheck asks the same question.
                self.assertEqual(self._eligible(conn, document_ids=(target,)), [target])
                self.assertEqual(self._eligible(conn, document_ids=(control,)), [])
                before = queries.pending_parse_backlog_count(conn, scope_classes=SCOPE)
                self.assertGreaterEqual(before, 1)
                # A raw-hash mismatch binds nothing.
                other = self._request(conn)
                self._target(conn, other, document_id=control, raw_file_hash="sha256:" + "d" * 64)
                self.assertNotIn(control, self._eligible(conn))
                # Retry gates still close the admitted document.
                for n in range(3):
                    conn.execute(
                        text(
                            "INSERT INTO disclosure_core.processing_run (processing_run_id, document_id, "
                            " artifact_owner_processing_run_id, run_kind, status, started_at, error, "
                            " provider_document_relpath) VALUES (:id, :doc, :id, 'parse', 'failed', "
                            " now() - (:n || ' minutes')::interval, CAST(:error AS jsonb), 'x')"
                        ),
                        {"id": f"run_pr{self.suffix}{n}", "doc": target, "n": str(n + 1),
                         "error": json.dumps({"stage": "parse", "error_code": "T", "retryable": True,
                                              "retry_budget_class": "item"})},
                    )
                self.assertNotIn(target, self._eligible(conn))
            finally:
                txn.rollback()

    def test_running_and_published_documents_are_never_readmitted(self) -> None:
        with self.engine.connect() as conn:
            txn = conn.begin()
            try:
                self._subject(conn, tracked_status=None)
                running = self._document(conn)
                published = self._document(conn, status="published")
                for document_id in (running, published):
                    self._target(conn, self._request(conn), document_id=document_id, raw_file_hash=HASH)
                conn.execute(
                    text(
                        "INSERT INTO disclosure_core.processing_run (processing_run_id, document_id, "
                        " artifact_owner_processing_run_id, run_kind, status, started_at, "
                        " provider_document_relpath) VALUES (:id, :doc, :id, 'parse', 'running', now(), 'x')"
                    ),
                    {"id": f"run_pr{self.suffix}r", "doc": running},
                )
                eligible = self._eligible(conn)
                self.assertNotIn(running, eligible)
                self.assertNotIn(published, eligible)
            finally:
                txn.rollback()

    def test_staged_candidate_source_and_locked_recheck_admit_only_the_exact_document(self) -> None:
        with self.engine.begin() as conn:
            self._subject(conn, tracked_status="paused")
            target = self._document(conn)
            control = self._document(conn)
            self._target(conn, self._request(conn), document_id=target, raw_file_hash=HASH)
        source = PostgresV4OrdinaryParseCandidateSource(
            engine=self.engine, max_retries=3, scope_classes=SCOPE,
        )
        page = source.list_candidates(after_document_id=None, limit=500000)
        listed = [candidate.document_id for candidate in page.candidates]
        self.assertIn(target, listed)
        self.assertNotIn(control, listed)
        latest = source.latest_document_id()
        self.assertIsNotNone(latest)
        with self.engine.connect() as conn:
            txn = conn.begin()
            try:
                from sqlalchemy.orm import Session

                session = Session(bind=conn)
                acquire_document_xact_lock(session, target)
                self.assertEqual(self._eligible(session.connection(), document_ids=(target,)), [target])
                self.assertEqual(self._eligible(session.connection(), document_ids=(control,)), [])
            finally:
                txn.rollback()

    # -- download admission --------------------------------------------------------
    def test_exact_download_admission_selects_only_the_requested_candidate(self) -> None:
        with self.engine.connect() as conn:
            txn = conn.begin()
            try:
                self._subject(conn, tracked_status=None)
                wanted, other = f"8{self.suffix[-7:]}1", f"8{self.suffix[-7:]}2"
                access_id = f"sa_pr{self.suffix}"
                self.sa_ids.append(access_id)
                conn.execute(
                    text(
                        "INSERT INTO disclosure_core.source_access (source_access_id, provider, "
                        " provider_interface, accessed_at, status, result_snapshot, company_id, security_id) "
                        "VALUES (:id, 'cninfo', 'cninfo:p_info3015', now(), 'ok', CAST(:snap AS jsonb), "
                        " :company, :security)"
                    ),
                    {"id": access_id, "company": self.company_id, "security": self.security_id,
                     "snap": json.dumps({"candidates": [
                         {"provider_document_id": pid, "title": OFF_SCOPE_TITLE, "raw_category": "",
                          "download_url": f"http://x/{pid}.PDF", "announcement_date": "2026-03-30",
                          "security_code": self.code, "exchange": "SSE"}
                         for pid in (wanted, other)]})},
                )

                def downloadable() -> list[str]:
                    return [row["provider_document_id"] for row in queries.pending_downloads(
                        conn, max_retries=3, limit=500000, scope_classes=SCOPE)]

                self.assertNotIn(wanted, downloadable())
                self._target(conn, self._request(conn, kind="provider_document"),
                             resolution_method="local_candidate", provider="cninfo",
                             provider_document_id=wanted, index_source_access_id=access_id,
                             admit_download=True)
                pids = downloadable()
                self.assertIn(wanted, pids)
                self.assertNotIn(other, pids)
                self.assertEqual(
                    [row["provider_document_id"] for row in queries.pending_downloads(
                        conn, max_retries=3, limit=5, scope_classes=SCOPE,
                        provider_document_ids=(wanted,))],
                    [wanted],
                )
                for n in range(3):
                    conn.execute(
                        text(
                            "INSERT INTO disclosure_core.source_access (source_access_id, provider, "
                            " provider_interface, accessed_at, status, query_params, error) VALUES "
                            " (:id, 'cninfo', 'cninfo:download_pdf', now(), 'failed', CAST(:q AS jsonb), "
                            " CAST(:e AS jsonb))"
                        ),
                        {"id": f"sa_pr{self.suffix}f{n}",
                         "q": json.dumps({"provider_document_id": wanted}),
                         "e": json.dumps({"error_code": "http_503", "retryable": True})},
                    )
                self.assertNotIn(wanted, downloadable())  # budget unchanged by admission
            finally:
                txn.rollback()

    # -- request lifecycle ---------------------------------------------------------
    def _service(self) -> ProcessingRequestService:
        return ProcessingRequestService(
            uow_factory=processing_request_uow_factory(self.engine),
            status_reader=PostgresProcessingStatusReader(
                self.engine, max_download_retries=3, max_parse_retries=3,
                max_build_retries=3, scope_classes=SCOPE,
            ),
            policy=ProcessingRequestPolicy(daily_request_limit=10000),
            clock=lambda: datetime.now(timezone.utc),
        )

    def _title_intent(self, key: str) -> Any:
        return normalize_intent(
            {"idempotency_key": key, "target": {
                "kind": "title_match", "security_code": self.code, "exchange": "SSE",
                "date_from": "2026-03-01", "date_to": "2026-03-31", "title_contains": "年度报告"}},
            today=date(2026, 10, 7),
        )

    def test_concurrent_same_key_submissions_join_one_durable_request(self) -> None:
        service = self._service()
        key = f"it-{self.suffix}-same"
        views: list[Any] = []
        barrier = threading.Barrier(6)

        def submit() -> None:
            barrier.wait()
            views.append(service.submit(self._title_intent(key)))

        threads = [threading.Thread(target=submit) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({view.request.request_id for view in views}), 1)
        self.assertEqual(sum(1 for view in views if not view.replayed), 1)
        with self.engine.connect() as conn:
            count = conn.execute(
                text("SELECT count(*) FROM disclosure_ops.processing_request WHERE idempotency_key = :k"),
                {"k": key},
            ).scalar_one()
        self.assertEqual(count, 1)

    def test_worker_resolves_pending_request_after_restart_without_repost(self) -> None:
        with self.engine.begin() as conn:
            self._subject(conn, tracked_status="paused")
        request_id = self._service().submit(self._title_intent(f"it-{self.suffix}-restart")).request.request_id
        pid = f"7{self.suffix[-7:]}1"

        class _Source:
            def search_announcements(self, security: SourceSecurity, window: DisclosureWindow) -> list[AnnouncementRef]:
                day = date(2026, 3, 30)
                if not window.start <= day <= window.end:
                    return []
                return [AnnouncementRef(
                    provider="cninfo", provider_document_id=pid, title="2025年年度报告",
                    download_url=f"https://static.cninfo.com.cn/finalpage/2026-03-30/{pid}.PDF",
                    raw_category="010301", announcement_date=day, security_code=security.security_code,
                    security_name="测试", file_size=1, index_updated_at=None,
                    raw_record={"announcementId": pid})]

            def download_pdf_to(self, ref: AnnouncementRef, sink: object) -> object:
                raise AssertionError("no download in discovery")

        # A fresh resolver stands in for a restarted worker: only the tables carry state.
        resolver = ProcessingRequestResolver(
            uow_factory=processing_request_uow_factory(self.engine), source=_Source(),
            profile_loader=lambda code: None, index_interface="cninfo:p_info3015",
            classify_failure=lambda exc: "item", clock=lambda: datetime.now(timezone.utc),
        )
        resolver.run(limit=10)
        status = self._service().get(request_id).status
        self.assertEqual(status["resolution"]["state"], "resolved")
        self.assertEqual(status["state"], "download_queued")
        self.assertEqual(status["filing"]["provider_document_id"], pid)
        with self.engine.connect() as conn:
            tracked = conn.execute(
                text("SELECT status FROM disclosure_core.tracked_company WHERE tracked_company_id = :id"),
                {"id": self.tracked_id},
            ).scalar_one()
            checkpoints = conn.execute(
                text("SELECT count(*) FROM disclosure_core.source_checkpoint WHERE scope_key LIKE :s"),
                {"s": f"{self.company_id}:%"},
            ).scalar_one()
            # Python None reached the table as SQL NULL, not the JSON null literal.
            nulls = conn.execute(
                text("SELECT outcome, choices IS NULL, error IS NULL "
                     "FROM disclosure_ops.processing_request_attempt WHERE request_id = :rid"),
                {"rid": request_id},
            ).one()
        self.assertEqual((tracked, checkpoints), ("paused", 0))
        self.assertEqual(tuple(nulls), ("matched", True, True))

    def test_stale_lease_is_abandoned_with_history_and_rescheduled(self) -> None:
        request_id = self._service().submit(self._title_intent(f"it-{self.suffix}-lease")).request.request_id
        stale = datetime.now(timezone.utc) - timedelta(hours=1)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_ops.processing_request_attempt (attempt_id, request_id, "
                    " attempt_no, started_at, lease_expires_at) VALUES (:id, :rid, 1, :started, :lease)"
                ),
                {"id": "dpra_" + ids.new_ulid(), "rid": request_id, "started": stale,
                 "lease": stale + timedelta(seconds=900)},
            )
        resolver = ProcessingRequestResolver(
            uow_factory=processing_request_uow_factory(self.engine), source=object(),  # type: ignore[arg-type]
            profile_loader=lambda code: None, index_interface="cninfo:p_info3015",
            classify_failure=lambda exc: "item", clock=lambda: datetime.now(timezone.utc),
        )
        # Its expired lease orders it first; limit 1 keeps other rows untouched.
        self.assertEqual(resolver.run(limit=1).abandoned, 1)
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT outcome, next_attempt_at IS NOT NULL, finished_at >= lease_expires_at, "
                     "choices IS NULL, jsonb_typeof(error) "
                     "FROM disclosure_ops.processing_request_attempt WHERE request_id = :rid"),
                {"rid": request_id},
            ).one()
        self.assertEqual(tuple(row), ("abandoned", True, True, True, "object"))

    def _resolver(self, source: Any, clock: Any) -> ProcessingRequestResolver:
        return ProcessingRequestResolver(
            uow_factory=processing_request_uow_factory(self.engine), source=source,
            profile_loader=lambda code: None, index_interface="cninfo:p_info3015",
            classify_failure=lambda exc: "item", clock=clock,
        )

    def _announcement(self, pid: str, security: SourceSecurity, day: date) -> AnnouncementRef:
        return AnnouncementRef(
            provider="cninfo", provider_document_id=pid, title="2025年年度报告",
            download_url=f"https://static.cninfo.com.cn/finalpage/{day.isoformat()}/{pid}.PDF",
            raw_category="010301", announcement_date=day, security_code=security.security_code,
            security_name="测试", file_size=1, index_updated_at=None, raw_record={"announcementId": pid})

    def _request_rows(self, request_id: str) -> tuple[Any, int, int]:
        with self.engine.connect() as conn:
            attempt = conn.execute(
                text("SELECT outcome, finished_at >= lease_expires_at AS after_lease, error "
                     "FROM disclosure_ops.processing_request_attempt WHERE request_id = :rid "
                     "ORDER BY attempt_no DESC LIMIT 1"),
                {"rid": request_id},
            ).mappings().one()
            targets = conn.execute(
                text("SELECT count(*) FROM disclosure_ops.processing_request_target WHERE request_id = :rid"),
                {"rid": request_id},
            ).scalar_one()
            observations = conn.execute(
                text("SELECT count(*) FROM disclosure_core.source_access "
                     "WHERE result_snapshot->'selection'->>'request_id' = :rid"),
                {"rid": request_id},
            ).scalar_one()
        return attempt, int(targets), int(observations)

    def test_stale_completion_commits_nothing_and_closes_its_lease(self) -> None:
        with self.engine.begin() as conn:
            self._subject(conn, tracked_status=None)
        request_id = self._service().submit(self._title_intent(f"it-{self.suffix}-stale")).request.request_id
        base = datetime.now(timezone.utc)
        offset = {"seconds": 0}
        test = self

        class _StallingSource:
            def search_announcements(self, security: SourceSecurity, window: DisclosureWindow) -> list[AnnouncementRef]:
                offset["seconds"] = 901  # this executor outlives its 900 s lease
                day = date(2026, 3, 30)
                if security.security_code != test.code or not window.start <= day <= window.end:
                    return []
                return [test._announcement(f"6{test.suffix[-7:]}1", security, day)]

        resolver = self._resolver(_StallingSource(), lambda: base + timedelta(seconds=offset["seconds"]))
        resolver.run(limit=10)
        attempt, targets, observations = self._request_rows(request_id)
        self.assertEqual((attempt["outcome"], attempt["after_lease"], targets, observations),
                         ("abandoned", True, 0, 0))
        self.assertEqual(attempt["error"]["error_code"], "attempt_lease_expired")
        with processing_request_uow_factory(self.engine)() as uow:
            repo = uow.processing_requests
            started = base + timedelta(hours=2)
            open_attempt = repo.start_attempt(
                attempt_id="dpra_" + ids.new_ulid(), request_id=request_id, attempt_no=2,
                started_at=started, lease_expires_at=started + timedelta(seconds=900))
            # Neither completion path may cross the lease boundary.
            self.assertFalse(repo.abandon_attempt(
                attempt_id=open_attempt.attempt_id, finished_at=started + timedelta(seconds=1),
                next_attempt_at=None, error={"error_code": "attempt_lease_expired", "retryable": True}))
            self.assertFalse(repo.finish_attempt(
                attempt_id=open_attempt.attempt_id, outcome="not_found",
                finished_at=started + timedelta(seconds=900)))

    def test_late_contradicting_local_row_is_never_bound(self) -> None:
        with self.engine.begin() as conn:
            self._subject(conn, tracked_status=None)
        request_id = self._service().submit(self._title_intent(f"it-{self.suffix}-late")).request.request_id
        pid = f"5{self.suffix[-7:]}1"
        late_document = f"doc_pr{self.suffix}late"
        self.doc_ids.append(late_document)
        test = self

        class _RacingSource:
            def search_announcements(self, security: SourceSecurity, window: DisclosureWindow) -> list[AnnouncementRef]:
                day = date(2026, 3, 30)
                if security.security_code != test.code or not window.start <= day <= window.end:
                    return []
                # Registered meanwhile with this CNINFO id, but dated outside the request window.
                with test.engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO disclosure_core.document (document_id, company_id, security_id, "
                            " status, title, provider, provider_document_id, provider_metadata, "
                            " raw_file_relpath, raw_file_hash, announcement_date) VALUES (:id, :company, "
                            " :security, 'registered', '2025年年度报告', 'cninfo', :pid, "
                            " '{\"raw_category\": \"\"}'::jsonb, :relpath, :hash, DATE '2026-02-01')"
                        ),
                        {"id": late_document, "company": test.company_id, "security": test.security_id,
                         "pid": pid, "hash": HASH,
                         "relpath": f"raw_documents/cninfo/{test.code}/2026/{pid}/sha256_{'c' * 64}.pdf"},
                    )
                return [test._announcement(pid, security, day)]

        self._resolver(_RacingSource(), lambda: datetime.now(timezone.utc)).run(limit=10)
        attempt, targets, observations = self._request_rows(request_id)
        self.assertEqual((attempt["outcome"], targets, observations), ("failed", 0, 1))
        self.assertEqual((attempt["error"]["error_code"], attempt["error"]["found"]["document_id"]),
                         ("local_filing_mismatch", late_document))

    def test_schema_refuses_contradictory_rows(self) -> None:
        with self.engine.connect() as conn:
            self._subject_checks(conn)

    def _subject_checks(self, conn: Any) -> None:
        cases = [
            ("duplicate key", lambda: [self._request_with_key(conn, "it-dup-key-000000000"),
                                       self._request_with_key(conn, "it-dup-key-000000000")]),
            ("download admission bound to a document", lambda: self._target(
                conn, self._request(conn), document_id=self._document(conn), raw_file_hash=HASH,
                admit_download=True)),
            ("two open attempts", lambda: self._two_open_attempts(conn)),
            ("failed without error", lambda: self._finished_attempt(conn, "failed")),
            ("failed with JSON null error", lambda: self._finished_attempt(conn, "failed", error="null")),
            ("deferred without error", lambda: self._finished_attempt(conn, "deferred")),
            ("ambiguous without choices", lambda: self._finished_attempt(conn, "ambiguous")),
            ("outcome recorded after the lease", lambda: self._finished_attempt(
                conn, "not_found", finished="now() + interval '2 minutes'")),
            ("abandoned while still leased", lambda: self._finished_attempt(
                conn, "abandoned", error='{"error_code": "attempt_lease_expired"}')),
            ("open attempt with a scheduled retry", lambda: conn.execute(text(
                "INSERT INTO disclosure_ops.processing_request_attempt (attempt_id, request_id, attempt_no, "
                " started_at, lease_expires_at, next_attempt_at) VALUES (:id, :rid, 1, now(), "
                " now() + interval '1 minute', now())"),
                {"id": "dpra_" + ids.new_ulid(), "rid": self._request(conn)})),
            ("request target without kind", lambda: conn.execute(text(
                "INSERT INTO disclosure_ops.processing_request (request_id, idempotency_key, "
                " intent_sha256, contract_version, target_kind, target, enqueue_parse, requested_by) "
                "VALUES (:id, :key, :intent, 'disclosure-processing-request.v1', 'document', "
                " '{}'::jsonb, true, 'integration')"),
                {"id": "dreq_" + ids.new_ulid(), "key": f"it-{self.suffix}-nokind", "intent": HASH})),
        ]
        for name, action in cases:
            with self.subTest(case=name):
                txn = conn.begin()
                try:
                    if name != "duplicate key":
                        self._subject(conn, tracked_status=None)
                    with self.assertRaises(IntegrityError):
                        action()
                finally:
                    txn.rollback()

    def _request_with_key(self, conn: Any, key: str) -> None:
        conn.execute(
            text(
                "INSERT INTO disclosure_ops.processing_request (request_id, idempotency_key, "
                " intent_sha256, contract_version, target_kind, target, enqueue_parse, requested_by) "
                "VALUES (:id, :key, :intent, 'disclosure-processing-request.v1', 'document', "
                " '{\"kind\": \"document\"}'::jsonb, true, 'integration')"
            ),
            {"id": "dreq_" + ids.new_ulid(), "key": key, "intent": HASH},
        )

    def _finished_attempt(self, conn: Any, outcome: str, *, error: str | None = None,
                          finished: str = "now()") -> None:
        conn.execute(
            text(
                "INSERT INTO disclosure_ops.processing_request_attempt (attempt_id, request_id, "
                " attempt_no, started_at, lease_expires_at, finished_at, outcome, error) VALUES "
                f"(:id, :rid, 1, now(), now() + interval '1 minute', {finished}, :outcome, "
                " CAST(:error AS jsonb))"
            ),
            {"id": "dpra_" + ids.new_ulid(), "rid": self._request(conn), "outcome": outcome,
             "error": error},
        )

    def _two_open_attempts(self, conn: Any) -> None:
        request_id = self._request(conn)
        for number in (1, 2):
            conn.execute(
                text(
                    "INSERT INTO disclosure_ops.processing_request_attempt (attempt_id, request_id, "
                    " attempt_no, started_at, lease_expires_at) VALUES (:id, :rid, :n, now(), "
                    " now() + interval '15 minutes')"
                ),
                {"id": "dpra_" + ids.new_ulid(), "rid": request_id, "n": number},
            )


if __name__ == "__main__":
    unittest.main()
