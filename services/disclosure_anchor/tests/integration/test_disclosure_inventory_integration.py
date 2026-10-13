"""Inventory SQL parity in the marker-verified disposable database only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

from sqlalchemy import event, text

from disclosure_anchor.adapters.db.postgres.disclosure_inventory import PostgresDisclosureInventoryReader
from disclosure_anchor.application.contracts.disclosure_inventory import (
    INVENTORY_STATUSES, continuation_cursor, normalize_query,
)
from disclosure_anchor.application.worker import queries
from disclosure_anchor.domain.ids import new_ulid
from tests.integration._support import engine_or_skip


class DisclosureInventoryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        # engine_or_skip rejects a missing/disagreeing environment or absent
        # managed-scratch marker before any test mutation; never use production.
        self.engine = engine_or_skip()
        self.addCleanup(self.engine.dispose)
        self.conn = self.engine.connect()
        self.addCleanup(self.conn.close)
        self.transaction = self.conn.begin()
        self.addCleanup(self.transaction.rollback)
        self.suffix = new_ulid()
        self.code = "99" + str(int(hashlib.sha256(self.suffix.encode()).hexdigest()[:8], 16) % 10000).zfill(4)
        self.company = "co_inventory_" + self.suffix
        self.security = "sec_inventory_" + self.suffix
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.conn.execute(text("INSERT INTO disclosure_core.company(company_id,legal_name) VALUES (:id,'Inventory synthetic')"), {"id": self.company})
        self.conn.execute(text("INSERT INTO disclosure_core.security(security_id,company_id,security_code,exchange) VALUES (:id,:company,:code,'SSE')"),
                          {"id": self.security, "company": self.company, "code": self.code})
        self.docs = {}
        self.sources = []
        self.reader = PostgresDisclosureInventoryReader(self.engine, max_download_retries=3,
            max_parse_retries=3, max_build_retries=3, scope_classes=None)

    def _document(self, name, *, status="registered", in_pool=False, provider_id=None,
                  created_at=None, hash_digit="a", provider="cninfo"):
        document = "doc_" + new_ulid()
        provider_id = provider_id or "inventory-" + self.suffix + "-" + name
        self.conn.execute(text(
            "INSERT INTO disclosure_core.document(document_id,provider,provider_document_id,"
            "security_id,company_id,title,announcement_date,raw_file_relpath,raw_file_hash,status,created_at) "
            "VALUES (:id,:provider,:pid,:security,:company,:title,'2026-01-01',:path,:hash,:status,:created)"
        ), {"id": document, "provider": provider, "pid": provider_id, "security": self.security,
            "company": self.company if in_pool else None, "title": "Synthetic " + name,
            "path": "raw/synthetic/" + document + ".pdf", "hash": "sha256:" + hash_digit * 64,
            "status": status, "created": created_at or self.now})
        self.docs[name] = document
        return document

    def _run(self, document, *, status, error=None, unit_build_status="not_started",
             delta=0, provider_output=True, attempts=0, build_error=None):
        run = "run_" + new_ulid()
        self.conn.execute(text(
            "INSERT INTO disclosure_core.processing_run(processing_run_id,document_id,"
            "artifact_owner_processing_run_id,run_kind,status,unit_build_status,"
            "provider_document_relpath,normalized_ir_relpath,started_at,error,is_active,unit_build_attempt_count,unit_build_error) "
            "VALUES (:id,:doc,:id,'parse',:status,:build,:path,:legacy_path,:started,CAST(:error AS jsonb),false,:attempts,CAST(:build_error AS jsonb))"
        ), {"id": run, "doc": document, "status": status, "build": unit_build_status,
            "path": "derived/synthetic/provider.json" if provider_output else None,
            "legacy_path": None if provider_output else "derived/synthetic/legacy.json",
            "started": self.now + timedelta(seconds=delta), "attempts": attempts,
            "error": None if error is None else json.dumps(error),
            "build_error": None if build_error is None else json.dumps(build_error)})
        return run

    def _index(self, candidates, *, coded=True, delta=0):
        source = "sa_" + new_ulid()
        enriched = [{"security_code": self.code, "exchange": "SSE",
                     "download_url": "https://example.invalid/synthetic.pdf",
                     "announcement_date": "2026-01-01", "raw_category": "0101" if coded else "", **candidate}
                    for candidate in candidates]
        self.conn.execute(text(
            "INSERT INTO disclosure_core.source_access(source_access_id,provider,provider_interface,"
            "status,accessed_at,result_snapshot,company_id) "
            "VALUES (:id,'cninfo','cninfo:p_info3015','ok',:at,CAST(:snapshot AS jsonb),:company)"
        ), {"id": source, "at": self.now + timedelta(seconds=delta),
            "snapshot": json.dumps({"candidates": enriched}), "company": self.company})
        self.sources.append(source)
        return source

    def _admit(self, *, document=None, provider_id=None, source=None, raw_hash=None):
        request = "dreq_" + new_ulid()
        target = "dprt_" + new_ulid()
        raw_hash = raw_hash or ("sha256:" + "a" * 64 if document else None)
        request_target = {"kind": "document", "document_id": document} if document else {
            "kind": "provider_document", "provider_document_id": provider_id,
            "security_code": self.code, "exchange": "SSE", "date_from": "2026-01-01", "date_to": "2026-01-01"}
        self.conn.execute(text(
            "INSERT INTO disclosure_ops.processing_request(request_id,idempotency_key,intent_sha256,"
            "contract_version,target_kind,target,enqueue_parse,requested_by) "
            "VALUES (:id,:key,:hash,'disclosure-processing-request.v1',:kind,CAST(:target AS jsonb),true,'inventory-test')"
        ), {"id": request, "key": "inventory:" + new_ulid(), "hash": "sha256:" + "b" * 64,
            "kind": request_target["kind"], "target": json.dumps(request_target)})
        self.conn.execute(text(
            "INSERT INTO disclosure_ops.processing_request_target(target_id,request_id,resolution_method,"
            "provider,provider_document_id,document_id,raw_file_hash,index_source_access_id,admit_download,admit_parse) "
            "VALUES (:id,:request,:method,:provider,:pid,:doc,:hash,:source,:download,true)"
        ), {"id": target, "request": request, "method": "document_id" if document else "local_candidate",
            "provider": "cninfo" if provider_id else None, "pid": provider_id, "doc": document,
            "hash": raw_hash, "source": source, "download": document is None})

    def _snapshot(self, **kwargs):
        return self.reader._read(self.conn, normalize_query(security_code=self.code, **kwargs))

    def _download_failure(self, provider_id, *, retryable, delta=0):
        self.conn.execute(text(
            "INSERT INTO disclosure_core.source_access(source_access_id,provider,provider_interface,"
            "status,accessed_at,query_params,error) "
            "VALUES (:id,'cninfo','cninfo:download_pdf','failed',:at,CAST(:query AS jsonb),:error)"
        ), {"id": "sa_" + new_ulid(), "at": self.now + timedelta(seconds=delta),
            "query": json.dumps({"provider_document_id": provider_id}),
            "error": json.dumps({"error_code": "synthetic_download_failure", "retryable": retryable})})

    def _release(self, document, run):
        self.conn.execute(text(
            "INSERT INTO disclosure_ops.parse_requeue_decision(decision_id,document_id,processing_run_id,"
            "failure_error_code,failure_retry_budget_class,fixed_by,reason,decided_by) "
            "VALUES (:id,:doc,:run,'synthetic_contract','semantic_route_contract','synthetic fixed parser',"
            "'synthetic scratch release','inventory-test')"
        ), {"id": "prq_" + new_ulid(), "doc": document, "run": run})

    def test_sql_partitions_canonical_corpus_and_matches_actual_queue_predicates(self) -> None:
        published = self._document("published", status="published")
        parsing = self._document("parsing")
        running = self._run(parsing, status="running")
        post = self._document("post", status="parsed")
        self._run(post, status="succeeded")
        queued = self._document("queued")
        blocked = self._document("blocked", in_pool=True)
        admitted = self._document("admitted", in_pool=True)
        self._admit(document=admitted)
        failed = self._document("failed", status="parse_failed")
        self._run(failed, status="failed", error={"stage": "parse", "error_code": "invalid_pdf",
            "retryable": False, "retry_budget_class": "item"})
        pid_queued, pid_blocked = "inventory-download-" + self.suffix, "inventory-undemanded-" + self.suffix
        source = self._index([{"provider_document_id": pid_queued, "title": "Coded preferred"},
                              {"provider_document_id": pid_blocked, "title": "Not admitted"}])
        self._index([{"provider_document_id": pid_queued, "title": "Newer code-less"}], coded=False, delta=1)
        self._admit(provider_id=pid_queued, source=source)
        snapshot = self._snapshot(limit=200)
        self.assertEqual(snapshot.known_total, 9)
        self.assertEqual(sum(snapshot.by_status.values()), snapshot.known_total)
        self.assertEqual(set(snapshot.by_status), set(INVENTORY_STATUSES))
        self.assertEqual(snapshot.active_failures, 1)
        expected_counts = dict(zip(INVENTORY_STATUSES, (1, 1, 1, 2, 2, 1, 1)))
        self.assertEqual(snapshot.by_status, expected_counts)
        items = {item["document_id"]: item for item in snapshot.items if item["document_id"]}
        selected = {row["document_id"] for row in queries.pending_parse(self.conn, max_retries=3,
            limit=100, document_ids=tuple(items), scope_classes=None)}
        self.assertEqual({key for key, item in items.items() if item["scheduling"]["parse_selected"]}, selected)
        self.assertEqual(selected, {queued, admitted})
        self.assertFalse(items[blocked]["scheduling"]["parse_selected"])
        self.assertEqual(items[parsing]["processing"]["running_processing_run_id"], running)
        self.assertFalse(items[parsing]["scheduling"]["parse_selected"])
        self.assertEqual(items[published]["processing"]["units_path"], f"/v1/documents/{published}/units")
        self.assertIn("latest_failure_nonretryable", items[failed]["scheduling"]["blocked_reasons"])
        candidates = {item["provider_document_id"]: item for item in snapshot.items if not item["document_id"]}
        selected_downloads = queries.pending_downloads(self.conn, max_retries=3, limit=100,
            provider_document_ids=(pid_queued, pid_blocked), scope_classes=None)
        self.assertEqual({key for key, item in candidates.items() if item["scheduling"]["download_selected"]},
                         {row["provider_document_id"] for row in selected_downloads})
        self.assertEqual(candidates[pid_queued]["title"], "Coded preferred")
        filtered = self._snapshot(status="failed")
        self.assertEqual(filtered.known_total, 9)
        self.assertEqual(filtered.matching_total, 1)
        self.assertEqual(filtered.items[0]["document_id"], failed)

    def test_keyset_and_latest_source_hash_do_not_double_count_versions_or_apply_wrong_hash_intent(self) -> None:
        provider_id = "inventory-version-" + self.suffix
        old = self._document("old", provider_id=provider_id, in_pool=True)
        new = self._document("new", provider_id=provider_id, in_pool=True,
                             created_at=self.now + timedelta(seconds=1), hash_digit="c")
        self._admit(document=old)
        for index in range(3):
            self._document("queued" + str(index))
        snapshot = self._snapshot(limit=200)
        self.assertEqual(snapshot.known_total, 4)
        self.assertNotIn(old, [item["document_id"] for item in snapshot.items])
        newest = next(item for item in snapshot.items if item["document_id"] == new)
        self.assertEqual(newest["original"]["raw_file_hash"], "sha256:" + "c" * 64)
        self.assertFalse(newest["scheduling"]["parse_selected"])
        first = self._snapshot(limit=2)
        self.assertTrue(first.has_more)
        second = self.reader._read(self.conn, normalize_query(security_code=self.code,
            limit=2, cursor=continuation_cursor(
                normalize_query(security_code=self.code), first.items[-1]["inventory_id"])))
        self.assertFalse(second.has_more)
        self.assertEqual([item["inventory_id"] for item in first.items + second.items],
                         [item["inventory_id"] for item in snapshot.items])
        self.assertEqual(first.by_status, second.by_status)
        # A document-specific demand for the wrong raw hash must admit nothing.
        self._admit(document=new, raw_hash="sha256:" + "a" * 64)
        reread = self._snapshot(status="downloaded_queued")
        self.assertNotIn(new, [item["document_id"] for item in reread.items])

    def test_repeated_snapshots_canonicalize_before_security_and_status_pagination(self) -> None:
        other_code = "98" + self.code[2:]
        other_security = "sec_" + new_ulid()
        self.conn.execute(text(
            "INSERT INTO disclosure_core.security(security_id,company_id,security_code,exchange) "
            "VALUES (:id,:company,:code,'SSE')"
        ), {"id": other_security, "company": self.company, "code": other_code})
        shared_id = "inventory-shared-" + self.suffix
        old = self._document("old scoped published", status="published", provider_id=shared_id)
        newest = self._document("new foreign version", provider_id=shared_id,
            created_at=self.now + timedelta(seconds=1), hash_digit="c")
        self.conn.execute(text("UPDATE disclosure_core.document SET security_id=:security WHERE document_id=:id"),
                          {"security": other_security, "id": newest})
        different_provider = self._document("different provider", status="published",
            provider="synthetic", provider_id=shared_id)
        # Missing provider identity is per-document, never one shared NULL bucket.
        self._document("null provider one", provider=None, provider_id=shared_id)
        self._document("null provider two", provider=None, provider_id=shared_id)
        missing_ref = self._document("missing provider reference")
        blank_ref = self._document("blank provider reference")
        self.conn.execute(text("UPDATE disclosure_core.document SET provider_document_id=NULL WHERE document_id=:id"),
                          {"id": missing_ref})
        self.conn.execute(text("UPDATE disclosure_core.document SET provider_document_id='' WHERE document_id=:id"),
                          {"id": blank_ref})
        ids = ["inventory-repeat-" + self.suffix + str(n) for n in range(5)]
        candidates = [{"provider_document_id": pid, "title": "Preferred coded " + str(n)}
                      for n, pid in enumerate(ids)]
        candidates.append({"provider_document_id": shared_id, "title": "Already registered"})
        for n in range(12):
            self._index(candidates, delta=n)
        self._index([{"provider_document_id": ids[0], "title": "Newer code-less"}],
                    coded=False, delta=50)
        foreign = "inventory-foreign-" + self.suffix
        self._index([{"provider_document_id": foreign, "title": "Older own security"}], delta=1)
        self._index([{"provider_document_id": foreign, "title": "Latest other security",
                      "security_code": other_code}], delta=2)
        baseline = self._snapshot(limit=200)
        self.assertEqual(baseline.known_total, 10)
        self.assertEqual(sum(baseline.by_status.values()), 10)
        self.assertEqual(baseline.by_status["published"], 1)
        self.assertNotIn(old, [item["document_id"] for item in baseline.items])
        self.assertIn(different_provider, [item["document_id"] for item in baseline.items])
        self.assertEqual(next(item["title"] for item in baseline.items
                              if item["provider_document_id"] == ids[0]), "Preferred coded 0")
        other = self.reader._read(self.conn, normalize_query(security_code=other_code))
        self.assertEqual(other.known_total, 2)
        self.assertEqual({item["document_id"] for item in other.items}, {newest, None})
        for status in (*INVENTORY_STATUSES, "failed"):
            with self.subTest(status=status):
                expected = [item for item in baseline.items if item["status"] == status
                            or (status == "failed" and item["processing"]["active_failure"])]
                observed, cursor = [], None
                while True:
                    page = self._snapshot(status=status, limit=1, cursor=cursor)
                    self.assertEqual(page.known_total, baseline.known_total)
                    self.assertEqual(page.by_status, baseline.by_status)
                    self.assertEqual(page.active_failures, baseline.active_failures)
                    self.assertEqual(page.matching_total, len(expected))
                    observed.extend(page.items)
                    if not page.has_more:
                        break
                    self.assertTrue(page.items)
                    cursor = continuation_cursor(normalize_query(security_code=self.code, status=status),
                                                 page.items[-1]["inventory_id"])
                    self.assertLessEqual(len(observed), len(expected))
                self.assertEqual(observed, expected)

    def test_bulk_parse_retry_facts_preserve_release_latches_and_charging(self) -> None:
        cases = {
            "item remaining": ("item", 2, True), "item exhausted": ("item", 3, False),
            "infrastructure remaining": ("infrastructure", 14, True),
            "infrastructure exhausted": ("infrastructure", 15, False),
            "neutral uncharged": ("neutral", 16, True),
        }
        expected = set()
        for name, (budget_class, attempts, eligible) in cases.items():
            document = self._document(name, status="parse_failed", in_pool=True)
            self._admit(document=document)
            for n in range(attempts):
                self._run(document, status="failed", delta=n, error={"stage": "parse",
                    "error_code": "synthetic_failure", "retryable": True, "retry_budget_class": budget_class})
            if eligible:
                expected.add(document)
        contract = {"stage": "parse", "error_code": "synthetic_contract", "retryable": False,
                    "retry_budget_class": "semantic_route_contract"}
        blocked = self._document("unreleased contract", status="parse_failed", in_pool=True)
        self._admit(document=blocked)
        self._run(blocked, status="failed", error=contract)
        released = self._document("released contract", status="parse_failed", in_pool=True)
        self._admit(document=released)
        run = self._run(released, status="failed", error=contract)
        self._release(released, run)
        expected.add(released)
        relatched = self._document("new failure after release", status="parse_failed", in_pool=True)
        self._admit(document=relatched)
        run = self._run(relatched, status="failed", error=contract)
        self._release(relatched, run)
        self._run(relatched, status="failed", error=contract, delta=1)
        legacy = self._document("failure without provider output", status="parse_failed", in_pool=True)
        self._admit(document=legacy)
        self._run(legacy, status="failed", error=contract, provider_output=False)
        expected.add(legacy)
        running = self._document("running", status="parse_failed", in_pool=True)
        self._admit(document=running)
        self._run(running, status="failed", error=contract)
        self._run(running, status="running", delta=1)
        published = self._document("published with history", status="published", in_pool=True)
        self._admit(document=published)
        self._run(published, status="failed", error=contract)
        snapshot = self._snapshot(limit=200)
        items = {item["document_id"]: item for item in snapshot.items}
        actual_queue = {row["document_id"] for row in queries.pending_parse(self.conn,
            max_retries=3, limit=200, document_ids=tuple(items), scope_classes=None)}
        self.assertEqual(actual_queue, expected)
        self.assertEqual({doc for doc, item in items.items() if item["scheduling"]["parse_selected"]}, expected)
        self.assertIn("item_retry_budget_exhausted", items[self.docs["item exhausted"]]["scheduling"]["blocked_reasons"])
        self.assertIn("charged_retry_budget_exhausted", items[self.docs["infrastructure exhausted"]]["scheduling"]["blocked_reasons"])
        self.assertIn("unreleased_contract_failure", items[relatched]["scheduling"]["blocked_reasons"])
        self.assertEqual(items[released]["scheduling"]["blocked_reasons"], [])
        self.assertTrue(items[released]["processing"]["active_failure"])
        self.assertFalse(items[running]["processing"]["active_failure"])
        self.assertFalse(items[published]["processing"]["active_failure"])
        failures = self._snapshot(status="failed", limit=200)
        self.assertEqual(failures.matching_total, snapshot.known_total - 2)
        self.assertEqual(failures.by_status, snapshot.by_status)

    def test_download_and_build_retry_budgets_and_terminal_failures_remain_independent(self) -> None:
        ids = {name: "inventory-download-" + self.suffix + name
               for name in ("remaining", "exhausted", "terminal", "undemanded")}
        source = self._index([{"provider_document_id": pid, "title": name} for name, pid in ids.items()])
        for name in ("remaining", "exhausted", "terminal"):
            self._admit(provider_id=ids[name], source=source)
        for n in range(2):
            self._download_failure(ids["remaining"], retryable=True, delta=n)
        for n in range(3):
            self._download_failure(ids["exhausted"], retryable=True, delta=n)
        self._download_failure(ids["terminal"], retryable=False)
        self._download_failure(ids["terminal"], retryable=True, delta=1)
        build_cases = {
            "ordinary remaining": (1, False, "build", True),
            "ordinary exhausted": (2, False, "build", False),
            "retryable remaining": (2, True, "build", True),
            "retryable exhausted": (10, True, "build", False),
            "publish quarantine": (0, False, "publish", False),
        }
        expected_build = set()
        for name, (attempts, retryable, stage, eligible) in build_cases.items():
            document = self._document(name, status="parsed")
            self._run(document, status="succeeded", unit_build_status="failed", attempts=attempts,
                build_error={"stage": stage, "error_code": "synthetic_build_failure", "retryable": retryable})
            if eligible:
                expected_build.add(document)
        remediated = self._document("remediated build history", status="parsed")
        self._run(remediated, status="succeeded", unit_build_status="failed", attempts=1,
            build_error={"stage": "build", "error_code": "old_build_failure", "retryable": False})
        self._run(remediated, status="succeeded", unit_build_status="succeeded", delta=1)
        # Successful empty builds remain outside automatic publish selection.
        self.reader = PostgresDisclosureInventoryReader(self.engine, max_download_retries=3,
            max_parse_retries=4, max_build_retries=2, scope_classes=None)
        snapshot = self._snapshot(limit=200)
        items = {item["document_id"]: item for item in snapshot.items if item["document_id"]}
        candidates = {item["provider_document_id"]: item for item in snapshot.items if not item["document_id"]}
        actual_build = {row["document_id"] for row in queries.pending_build(self.conn, max_retries=2, limit=200)} & set(items)
        actual_download = {row["provider_document_id"] for row in queries.pending_downloads(self.conn,
            max_retries=3, limit=200, provider_document_ids=tuple(ids.values()), scope_classes=None)}
        self.assertEqual(actual_build, expected_build)
        self.assertEqual({doc for doc, item in items.items() if item["scheduling"]["post_processing_selected"]}, expected_build)
        self.assertEqual(actual_download, {ids["remaining"]})
        self.assertEqual({pid for pid, item in candidates.items() if item["scheduling"]["download_selected"]}, actual_download)
        self.assertIn("download_retry_exhausted", candidates[ids["exhausted"]]["scheduling"]["blocked_reasons"])
        self.assertIn("download_nonretryable_failure", candidates[ids["terminal"]]["scheduling"]["blocked_reasons"])
        self.assertIn("terminal_publish_failure", items[self.docs["publish quarantine"]]["scheduling"]["blocked_reasons"])
        self.assertFalse(items[remediated]["processing"]["active_failure"])
        self.assertEqual(items[remediated]["processing"]["last_failure"]["error_code"], "old_build_failure")

    def test_resident_scope_and_exact_intent_match_real_queue_answers(self) -> None:
        # prefix/version are VARCHAR(32), value VARCHAR(48); keep the full ULID.
        allowed, noise = "IA" + self.suffix, "IN" + self.suffix
        scope = ("is_" + self.suffix,)
        self.conn.execute(text(
            "INSERT INTO disclosure_core.classification_rule(rule_set,prefix,value,priority,version) "
            "VALUES ('title',:allowed,:value,1,:version),('title_noise',:noise,'noise',1,:version)"
        ), {"allowed": allowed, "noise": noise, "value": scope[0], "version": "iv_" + self.suffix})
        self.conn.execute(text(
            "INSERT INTO disclosure_core.tracked_company(tracked_company_id,company_id,security_id,status) "
            "VALUES (:id,:company,:security,'paused')"
        ), {"id": "tc_" + new_ulid(), "company": self.company, "security": self.security})
        ordinary = self._document(allowed)
        noisy = self._document(allowed + noise)
        exact = self._document(allowed + " explicit", in_pool=True)
        self._admit(document=exact)
        off_scope = self._document("Outside own rules " + self.suffix, in_pool=True)
        self._admit(document=off_scope)
        wanted, other = "inventory-wanted-" + self.suffix, "inventory-other-" + self.suffix
        source = self._index([{"provider_document_id": pid, "title": noise, "raw_category": ""}
                              for pid in (wanted, other)])
        self._admit(provider_id=wanted, source=source)
        self.reader = PostgresDisclosureInventoryReader(self.engine, max_download_retries=3,
            max_parse_retries=3, max_build_retries=3, scope_classes=scope)
        snapshot = self._snapshot(limit=200)
        items = {item["document_id"]: item for item in snapshot.items if item["document_id"]}
        candidates = {item["provider_document_id"]: item for item in snapshot.items if not item["document_id"]}
        parse_ids = {row["document_id"] for row in queries.pending_parse(self.conn,
            max_retries=3, limit=200, document_ids=tuple(items), scope_classes=scope)}
        download_ids = {row["provider_document_id"] for row in queries.pending_downloads(self.conn,
            max_retries=3, limit=200, provider_document_ids=(wanted, other), scope_classes=scope)}
        self.assertEqual(parse_ids, {ordinary, exact})
        self.assertNotIn(noisy, parse_ids)
        self.assertNotIn(off_scope, parse_ids)
        self.assertEqual({doc for doc, item in items.items() if item["scheduling"]["parse_selected"]}, parse_ids)
        self.assertEqual(download_ids, {wanted})
        self.assertEqual({pid for pid, item in candidates.items() if item["scheduling"]["download_selected"]}, download_ids)

    def test_public_read_uses_read_only_one_hundred_twenty_second_transaction_and_resets_pool_state(self) -> None:
        document = self._document("public boundary", status="published")
        # Public read() uses its own connection, so make only this test's
        # synthetic published fixture visible in the managed scratch database.
        self.transaction.commit()

        def cleanup_committed_fixture():
            self.conn.rollback()
            with self.engine.begin() as conn:
                conn.execute(text("DELETE FROM disclosure_core.document WHERE document_id=:id"), {"id": document})
                conn.execute(text("DELETE FROM disclosure_core.security WHERE security_id=:id"), {"id": self.security})
                conn.execute(text("DELETE FROM disclosure_core.company WHERE company_id=:id"), {"id": self.company})

        self.addCleanup(cleanup_committed_fixture)
        with self.engine.connect() as conn:
            baseline_settings = conn.execute(text(
                "SELECT current_setting('transaction_read_only'), current_setting('statement_timeout')"
            )).one()
        observed_settings = []

        def inspect_transaction(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SET "):
                return
            # The DBAPI cursor avoids recursion into the SQLAlchemy event.
            # Observe the same actual transaction used by the inventory SELECT.
            # PostgreSQL may display 120 seconds as 2min; assert the duration.
            cursor.execute("SELECT current_setting('transaction_read_only'), "
                           "EXTRACT(EPOCH FROM current_setting('statement_timeout')::interval)")
            observed_settings.append(cursor.fetchone())

        event.listen(self.engine, "before_cursor_execute", inspect_transaction)
        try:
            snapshot = self.reader.read(normalize_query(security_code=self.code, limit=1))
        finally:
            event.remove(self.engine, "before_cursor_execute", inspect_transaction)
        self.assertEqual(snapshot.known_total, 1)
        self.assertEqual(snapshot.items[0]["document_id"], document)
        self.assertEqual(snapshot.items[0]["status"], "published")
        self.assertFalse(snapshot.has_more)
        self.assertTrue(observed_settings)
        self.assertTrue(all(read_only == "on" and timeout == 120 for read_only, timeout in observed_settings),
                        observed_settings)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT current_setting('transaction_read_only'), current_setting('statement_timeout')"
            )).one(), baseline_settings)
            self.assertEqual(conn.execute(text(
                "SELECT status FROM disclosure_core.document WHERE document_id=:id"
            ), {"id": document}).scalar_one(), "published")


if __name__ == "__main__":
    unittest.main()
