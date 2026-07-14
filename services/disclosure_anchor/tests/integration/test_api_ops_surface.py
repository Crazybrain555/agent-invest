"""DB-gated tests for the 2026-07-14 ops/read API additions (round23).

Covers /v1/health queue gauges, /v1/classification, the disclosure_topic
filter, and tracked-companies pagination + single-item lookup. Read-only
against the shared cluster: assertions are shape/invariant-based (counts are
non-negative, pages don't overlap) and never depend on specific business
rows, so nothing is written and nothing needs cleanup.
"""

from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest

from tests.integration._support import engine_or_skip

try:
    from fastapi.testclient import TestClient

    from disclosure_anchor.main import create_app
    from disclosure_anchor.settings import Settings

    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc


def _settings() -> "Settings":
    root = Path(tempfile.gettempdir()) / "disclosure_api_ops_surface_test"
    return Settings(
        disclosure_data_root=Path(
            os.environ.get("DISCLOSURE_DATA_ROOT", str(root / "data"))
        ),
        disclosure_shared_root=root / "shared",
        disclosure_runtime_root=root / "runtime",
        mineru_model_cache=root / "mineru",
        hf_home=root / "hf",
        modelscope_cache=root / "modelscope",
        database_url=os.environ.get("DATABASE_URL"),
    )


class ApiOpsSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"fastapi unavailable: {_IMPORT_ERROR}")
        cls.engine = engine_or_skip()
        app = create_app(_settings(), validate_runtime=False)
        app.state.app_db_engine = cls.engine
        app.state.db_engine = cls.engine
        app.state.reader_db_engine = cls.engine
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "client"):
            cls.client.__exit__(None, None, None)
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def test_health_exposes_queue_gauges(self) -> None:
        payload = self.client.get("/v1/health").json()
        queues = payload["queues"]
        self.assertIsNotNone(queues, "queues must resolve with a live engine")
        for gauge in (
            "pending_download",
            "pending_parse",
            "pending_build",
            "pending_publish",
            "download_dead_letters",
            "parse_dead_letters",
            "retrying_documents",
            "sync_due",
            "backfill_pending",
        ):
            self.assertGreaterEqual(queues[gauge], 0, gauge)

    def test_classification_lists_full_vocabulary(self) -> None:
        payload = self.client.get("/v1/classification").json()
        self.assertGreaterEqual(len(payload["classes"]), 31)
        self.assertTrue(payload["class_map_version"])
        names = {item["name"] for item in payload["classes"]}
        self.assertIn("annual_report", names)
        dispositions = {item["disposition"] for item in payload["classes"]}
        self.assertLessEqual(
            dispositions, {"process", "register_only", "unknown_disposition"}
        )

    def test_disclosure_topic_filter_accepts_and_validates(self) -> None:
        ok = self.client.get(
            "/v1/documents", params={"disclosure_topic": "annual_report", "limit": 5}
        )
        self.assertEqual(ok.status_code, 200)
        for item in ok.json()["items"]:
            self.assertIn("annual_report", item["disclosure_topics"])
        bad = self.client.get("/v1/documents", params={"disclosure_topic": "  "})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(bad.json()["error_code"], "VALIDATION_ERROR")

    def test_tracked_companies_paginate_without_overlap(self) -> None:
        first = self.client.get("/v1/tracked-companies", params={"limit": 2}).json()
        self.assertLessEqual(len(first["items"]), 2)
        if not first.get("next_cursor"):
            self.skipTest("pool too small to paginate")
        second = self.client.get(
            "/v1/tracked-companies",
            params={"limit": 2, "cursor": first["next_cursor"]},
        ).json()
        first_ids = {item["tracked_company_id"] for item in first["items"]}
        second_ids = {item["tracked_company_id"] for item in second["items"]}
        self.assertFalse(first_ids & second_ids, "pages must not overlap")

    def test_tracked_company_single_lookup_and_404(self) -> None:
        page = self.client.get("/v1/tracked-companies", params={"limit": 1}).json()
        if not page["items"]:
            self.skipTest("empty pool")
        item = page["items"][0]
        single = self.client.get(
            f"/v1/tracked-companies/{item['security_code']}",
            params={"exchange": item["exchange"]},
        )
        self.assertEqual(single.status_code, 200)
        self.assertEqual(
            single.json()["tracked_company_id"], item["tracked_company_id"]
        )
        self.assertIn("effective_lookback_days", single.json())
        missing = self.client.get(
            "/v1/tracked-companies/999999", params={"exchange": "SSE"}
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error_code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
