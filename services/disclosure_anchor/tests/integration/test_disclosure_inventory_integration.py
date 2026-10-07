"""Inventory SQL parity in the marker-verified disposable database only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

from sqlalchemy import text

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
                  created_at=None, hash_digit="a"):
        document = "doc_" + new_ulid()
        provider_id = provider_id or "inventory-" + self.suffix + "-" + name
        self.conn.execute(text(
            "INSERT INTO disclosure_core.document(document_id,provider,provider_document_id,"
            "security_id,company_id,title,announcement_date,raw_file_relpath,raw_file_hash,status,created_at) "
            "VALUES (:id,'cninfo',:pid,:security,:company,:title,'2026-01-01',:path,:hash,:status,:created)"
        ), {"id": document, "pid": provider_id, "security": self.security,
            "company": self.company if in_pool else None, "title": "Synthetic " + name,
            "path": "raw/synthetic/" + document + ".pdf", "hash": "sha256:" + hash_digit * 64,
            "status": status, "created": created_at or self.now})
        self.docs[name] = document
        return document

    def _run(self, document, *, status, error=None, unit_build_status="not_started"):
        run = "run_" + new_ulid()
        self.conn.execute(text(
            "INSERT INTO disclosure_core.processing_run(processing_run_id,document_id,"
            "artifact_owner_processing_run_id,run_kind,status,unit_build_status,"
            "provider_document_relpath,started_at,error,is_active) "
            "VALUES (:id,:doc,:id,'parse',:status,:build,:path,:started,CAST(:error AS jsonb),false)"
        ), {"id": run, "doc": document, "status": status, "build": unit_build_status,
            "path": "derived/synthetic/provider.json", "started": self.now,
            "error": None if error is None else json.dumps(error)})
        return run

    def _index(self, candidates, *, coded=True, delta=0):
        source = "sa_" + new_ulid()
        enriched = [{**candidate, "security_code": self.code, "exchange": "SSE",
                     "download_url": "https://example.invalid/synthetic.pdf",
                     "announcement_date": "2026-01-01", "raw_category": "0101" if coded else ""}
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


if __name__ == "__main__":
    unittest.main()
