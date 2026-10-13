"""Inventory traversal, error visibility and safe fact boundaries without a DB."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from disclosure_anchor.adapters.db.postgres.disclosure_inventory import (
    PostgresDisclosureInventoryReader,
)
from disclosure_anchor.api.agent_auth import AgentApiAccess
from disclosure_anchor.api.errors import install_error_handlers
from disclosure_anchor.api.routers.disclosure_inventory import router
from disclosure_anchor.api.schemas.disclosure_inventory import DisclosureInventoryOut
from disclosure_anchor.application.contracts.disclosure_inventory import (
    INVENTORY_STATUSES, InventoryQueryInvalid, InventorySnapshot, continuation_cursor, normalize_query,
)
from disclosure_anchor.application.use_cases.read_disclosure_inventory import ReadDisclosureInventory


def _row(**overrides):
    row = {
        "inventory_id": "document:doc_example", "document_id": "doc_example",
        "provider": "cninfo", "provider_document_id": "123456", "security_code": "600519",
        "exchange": "SSE", "title": "公开公告", "announcement_date": "2026-01-01",
        "document_status": "registered", "raw_file_hash": "sha256:" + "a" * 64,
        "archived": True, "current_processing_run_id": None,
        "running_processing_run_id": None, "running_started_at": None,
        "parse_selected": True, "download_selected": False, "post_processing_selected": False,
        "download_failure_count": 0, "download_nonretryable": False,
        "item_failures": 0, "charged_failures": 0, "contract_failures": 0,
        "latest_parse_retryable": None, "latest_parse_released": False,
        "unit_build_attempt_count": 0, "unit_build_error": None,
        "failure_stage": None, "failure_error": None, "failure_at": None, "failure_run_id": None,
        "active_failure": False, "status": "downloaded_queued",
    }
    row.update(overrides)
    return row


class _Reader:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def read(self, query):
        self.calls.append(query)
        return self.snapshot


class DisclosureInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = PostgresDisclosureInventoryReader(mock.Mock(), max_download_retries=3,
            max_parse_retries=4, max_build_retries=2, scope_classes=("annual_report",))
        self.item = self.reader._public_item(_row())
        self.snapshot = InventorySnapshot("2026-10-07T00:00:00+00:00", 8, 2,
            {state: (2 if state == "downloaded_queued" else 1) for state in INVENTORY_STATUSES},
            1, (self.item,), True)
        self.port = _Reader(self.snapshot)
        self.service = ReadDisclosureInventory(self.port)

    def test_filter_bound_cursor_preserves_scoped_counts_and_list_continuation(self) -> None:
        body = self.service.read(status="downloaded_queued", security_code="600519", limit=1)
        DisclosureInventoryOut.model_validate(body)
        self.assertEqual(body["scope"]["unknown_upstream_total"], None)
        self.assertEqual(body["summary"]["known_total"], 8)
        self.assertEqual(body["summary"]["matching_total"], 2)
        cursor = body["next_cursor"]
        self.service.read(status="downloaded_queued", security_code="600519", cursor=cursor, limit=200)
        self.assertEqual(self.port.calls[-1].after_inventory_id, self.item["inventory_id"])
        for arguments in ({"status": "failed", "security_code": "600519"},
                          {"status": "downloaded_queued", "security_code": "600520"}):
            with self.assertRaises(InventoryQueryInvalid):
                self.service.read(cursor=cursor, **arguments)
        self.assertEqual(len(self.port.calls), 2)
        self.assertIn("liveness", " ".join(body["notes"]))

    def test_invalid_query_family_rejects_before_any_port_read(self) -> None:
        for arguments in ({"status": "registered"}, {"security_code": "６００５１９"},
                          {"security_code": "12345"}, {"limit": True}, {"limit": 0},
                          {"limit": 201}, {"cursor": "bad!"}, {"cursor": "a" * 2049}):
            with self.subTest(arguments=arguments), self.assertRaises(InventoryQueryInvalid):
                self.service.read(**arguments)
        self.assertEqual(self.port.calls, [])

    def test_current_retryable_failure_remains_visible_next_to_queue_selection(self) -> None:
        item = self.reader._public_item(_row(
            document_status="parse_failed", active_failure=True, failure_stage="parse",
            failure_error={"stage": "parse", "error_code": "parser_unavailable", "retryable": True,
                           "raw_path": "/private/internal", "token": "never expose"},
            failure_at="2026-10-01T00:00:00+00:00", failure_run_id="run_failed",
        ))
        self.assertEqual(item["processing"]["state"], "parse_failed")
        self.assertTrue(item["processing"]["active_failure"])
        self.assertTrue(item["scheduling"]["parse_selected"])
        self.assertEqual(item["scheduling"]["blocked_reasons"], [])
        self.assertEqual(item["processing"]["last_failure"]["error_code"], "parser_unavailable")
        self.assertNotIn("/private", json.dumps(item))
        self.assertNotIn("never expose", json.dumps(item))

    def test_blocked_failures_preserve_specific_gates_and_running_is_only_recorded_fact(self) -> None:
        item = self.reader._public_item(_row(
            parse_selected=False, status="downloaded_not_queued", latest_parse_retryable=False,
            item_failures=4, charged_failures=20, contract_failures=1, active_failure=True,
        ))
        self.assertEqual(set(item["scheduling"]["blocked_reasons"]), {
            "latest_failure_nonretryable", "unreleased_contract_failure",
            "item_retry_budget_exhausted", "charged_retry_budget_exhausted",
        })
        running = self.reader._public_item(_row(
            status="parsing", running_processing_run_id="run_owned", running_started_at="2020-01-01T00:00:00Z",
            parse_selected=False,
        ))
        self.assertEqual(running["processing"]["state"], "parse_running")
        self.assertEqual(running["processing"]["running_started_at"], "2020-01-01T00:00:00Z")
        self.assertEqual(running["scheduling"]["blocked_reasons"], [])

    def test_candidate_download_failure_and_published_history_remain_separate(self) -> None:
        row = _row(document_id=None, inventory_id="candidate:cninfo:123456", archived=False,
            document_status=None, raw_file_hash=None, parse_selected=False, active_failure=True,
            status="not_downloaded_not_queued", download_failure_count=3, download_nonretryable=True,
            failure_stage="download", failure_at="2026-01-01T00:00:00Z",
            failure_error=json.dumps({"error_code": "access_refused", "retryable": False, "secret": "hidden"}))
        candidate = self.reader._public_item(row)
        self.assertEqual(candidate["processing"]["state"], "waiting_for_original")
        self.assertEqual(candidate["processing"]["last_failure"]["retryable"], False)
        self.assertIsNone(candidate["original"]["text_path"])
        self.assertEqual(candidate["scheduling"]["blocked_reasons"], ["download_retry_exhausted", "download_nonretryable_failure"])
        published = self.reader._public_item(_row(document_status="published", status="published",
            parse_selected=False, active_failure=False, failure_stage="parse", failure_at="2020-01-01Z",
            failure_error={"error_code": "old_failure", "retryable": True}))
        self.assertEqual(published["processing"]["state"], "published")
        self.assertFalse(published["processing"]["active_failure"])
        self.assertTrue(published["processing"]["units_path"].endswith("/units"))
        self.assertEqual(published["scheduling"]["blocked_reasons"], [])

    def test_reader_executes_single_bounded_projection_read_only_with_distinct_retry_limits(self) -> None:
        engine = mock.MagicMock()
        conn = engine.connect.return_value.__enter__.return_value
        rows = [_row(), _row(inventory_id="document:doc_second")]
        conn.execute.return_value.mappings.return_value.one.return_value = {
            "observed_at": datetime(2026, 10, 7, tzinfo=timezone.utc), "known_total": 2,
            "matching_total": 2, "by_status": {key: (2 if key == "downloaded_queued" else 0) for key in INVENTORY_STATUSES},
            "active_failures": 0, "items": rows,
        }
        reader = PostgresDisclosureInventoryReader(engine, max_download_retries=3,
            max_parse_retries=4, max_build_retries=2, scope_classes=("annual_report",))
        snapshot = reader.read(normalize_query(limit=1))
        self.assertEqual(len(snapshot.items), 1)
        self.assertTrue(snapshot.has_more)
        self.assertEqual(str(conn.execute.call_args_list[0].args[0]), "SET TRANSACTION READ ONLY")
        self.assertEqual(str(conn.execute.call_args_list[1].args[0]), "SET LOCAL statement_timeout = '120s'")
        _, params = conn.execute.call_args_list[-1].args
        self.assertEqual((params["page_limit"], params["max_retries"], params["max_build_retries"]), (2, 4, 2))
        self.assertEqual(params["max_build_ceiling"], 10)
        conn.rollback.assert_called_once()

    def test_public_reader_rolls_back_failure_and_does_not_retry_a_canceled_snapshot(self) -> None:
        # SQL-fragment assertions used to pin the implementation, although the
        # scheduler's actual answers are the invariant. Scratch tests below
        # cover that parity; this case covers cleanup of the live failure family.
        engine = mock.MagicMock()
        conn = engine.connect.return_value.__enter__.return_value
        failure = OperationalError("private SQL", {}, RuntimeError("query canceled"))
        conn.execute.side_effect = (mock.Mock(), mock.Mock(), failure)
        reader = PostgresDisclosureInventoryReader(engine, max_download_retries=3,
            max_parse_retries=4, max_build_retries=2, scope_classes=("annual_report",))
        with self.assertRaises(OperationalError) as raised:
            reader.read(normalize_query(limit=1))
        self.assertIs(raised.exception, failure)
        self.assertEqual(conn.execute.call_count, 3)
        conn.rollback.assert_called_once()
        engine.connect.assert_called_once()

    def _client(self):
        app = FastAPI()
        install_error_handlers(app)
        app.state.agent_api_access = AgentApiAccess(b"r" * 32, b"w" * 32, frozenset({"testserver"}))
        app.state.disclosure_inventory = self.service
        app.include_router(router)
        return TestClient(app)

    def test_agent_route_requires_read_scope_and_rejects_invalid_repeated_unknown_queries(self) -> None:
        with self._client() as client:
            self.assertEqual(client.get("/v1/disclosure-inventory").status_code, 401)
            self.assertEqual(client.get("/v1/disclosure-inventory", headers={"Authorization": "Bearer " + "w" * 32}).status_code, 403)
            self.assertEqual(self.port.calls, [])
            headers = {"Authorization": "Bearer " + "r" * 32}
            result = client.get("/v1/disclosure-inventory?status=downloaded_queued&limit=1", headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["items"], [self.item])
            for query in ("limit=0", "limit=201", "status=registered", "unknown=1", "limit=1&limit=2"):
                result = client.get("/v1/disclosure-inventory?" + query, headers=headers)
                self.assertEqual(result.status_code, 422, result.text)
                self.assertEqual(result.json()["error_code"], "VALIDATION_ERROR")
            self.assertEqual(len(self.port.calls), 1)

    def test_database_failure_is_unavailable_not_empty_success_or_secret_leak(self) -> None:
        self.port.read = mock.Mock(side_effect=OperationalError("private SQL", {}, RuntimeError("password-secret")))
        with self._client() as client:
            result = client.get("/v1/disclosure-inventory", headers={"Authorization": "Bearer " + "r" * 32})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["error_code"], "SERVICE_UNAVAILABLE")
        self.assertNotIn("password-secret", result.text)
        self.assertNotIn("private SQL", result.text)

    def test_strict_response_dto_rejects_private_fields(self) -> None:
        body = self.service.read()
        invalid = deepcopy(body)
        invalid["items"][0]["raw_file_relpath"] = "/private/internal.pdf"
        with self.assertRaises(ValueError):
            DisclosureInventoryOut.model_validate(invalid)
        cursor = continuation_cursor(normalize_query(), "document:example")
        self.assertEqual(normalize_query(cursor=cursor).after_inventory_id, "document:example")


if __name__ == "__main__":
    unittest.main()
