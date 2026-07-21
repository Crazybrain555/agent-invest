"""DB-gated tracked-pool API chain (round22: DB is the pool's truth).

PUT /v1/admin/tracked-companies writes through TrackCompanies; GET
/v1/tracked-companies reads tracked_companies_v1 and resolves the config
cascade. Run-unique security codes; tearDown removes every row this test
creates (tracked_company + security + company).
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from sqlalchemy import text

from disclosure_anchor.domain import ids
from disclosure_anchor.main import create_app
from disclosure_anchor.settings import Settings
from tests.integration._support import engine_or_skip
from tests.integration.test_filing_api_runtime import (
    _ADMIN_HEADERS,
    _ADMIN_TOKEN,
    _api_request,
)


def _settings(root: Path) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        mineru_model_cache=shared_root / "model_cache" / "mineru",
        hf_home=shared_root / "model_cache" / "huggingface",
        modelscope_cache=shared_root / "model_cache" / "modelscope",
        disclosure_enable_admin_api=True,
        disclosure_admin_token=_ADMIN_TOKEN,
        # No credentials: the on-add profile fetch must stay offline in tests
        # (env may carry real CNINFO keys).
        cninfo_access_key=None,
        cninfo_access_secret=None,
        cninfo_access_token=None,
    )


def _list_all_tracked(app, query: dict | None = None) -> list[dict]:
    """Walk cursor pages: the shared pool holds 1,000+ real companies and the
    T-prefixed test codes sort after every numeric code, i.e. onto the last
    page — a single default-limit GET can never see them."""

    items: list[dict] = []
    cursor: str | None = None
    while True:
        q = dict(query or {})
        q["limit"] = "1000"
        if cursor:
            q["cursor"] = cursor
        got = _api_request(app, "GET", "/v1/tracked-companies", query=q)
        assert got.status_code == 200, got.body
        payload = got.json()
        items.extend(payload["items"])
        cursor = payload.get("next_cursor")
        if not cursor:
            return items


class TrackedPoolApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.tmpdir = tempfile.TemporaryDirectory(prefix="tracked-pool-api-")
        self.settings = _settings(Path(self.tmpdir.name))
        app = create_app(settings=self.settings, validate_runtime=False)
        app.state.app_db_engine = self.engine
        app.state.db_engine = self.engine
        app.state.reader_db_engine = self.engine
        self.app = app
        suffix = ids.new_ulid()[-6:]
        self.code_override = f"T22A{suffix}"
        self.code_inherit = f"T22B{suffix}"

    def tearDown(self) -> None:
        with self.engine.begin() as conn:
            for code in (self.code_override, self.code_inherit):
                company_ids = [
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT company_id FROM disclosure_core.security "
                            "WHERE security_code = :code AND exchange = 'LOCAL'"
                        ),
                        {"code": code},
                    )
                ]
                for company_id in company_ids:
                    conn.execute(
                        text(
                            "DELETE FROM disclosure_core.tracked_company "
                            "WHERE company_id = :id"
                        ),
                        {"id": company_id},
                    )
                    conn.execute(
                        text(
                            "DELETE FROM disclosure_core.security "
                            "WHERE company_id = :id"
                        ),
                        {"id": company_id},
                    )
                    conn.execute(
                        text(
                            "DELETE FROM disclosure_core.company "
                            "WHERE company_id = :id"
                        ),
                        {"id": company_id},
                    )
        self.engine.dispose()
        self.tmpdir.cleanup()

    def test_put_then_get_resolves_cascade_and_status_filter(self) -> None:
        put = _api_request(
            self.app,
            "PUT",
            "/v1/admin/tracked-companies",
            headers=_ADMIN_HEADERS,
            json_body={
                "entries": [
                    {
                        "security_code": self.code_override,
                        "exchange": "LOCAL",
                        "lookback_days": 30,
                        "sync_frequency": "hourly",
                        "process_classes": ["annual_report"],
                    },
                    {
                        "security_code": self.code_inherit,
                        "exchange": "LOCAL",
                        "status": "paused",
                    },
                ]
            },
        )
        self.assertEqual(put.status_code, 200, put.body)
        payload = put.json()
        self.assertEqual(payload["created_count"], 2)
        self.assertFalse(payload["dry_run"])

        by_code = {
            item["security_code"]: item for item in _list_all_tracked(self.app)
        }

        override = by_code[self.code_override]
        self.assertEqual(override["effective_lookback_days"], 30)
        self.assertEqual(override["effective_sync_seconds"], 3600)
        self.assertEqual(override["effective_process_classes"], ["annual_report"])
        self.assertEqual(override["contract_version"], "tracked_company.v1")
        # Offline intake without credentials: placeholder name, never synced.
        self.assertEqual(override["legal_name_status"], "pending")
        self.assertEqual(override["sync_state"], "never_synced")
        self.assertIsNone(override["last_synced_at"])
        self.assertIsNone(override["synced_through"])

        inherit = by_code[self.code_inherit]
        self.assertEqual(inherit["status"], "paused")
        self.assertIsNone(inherit["process_classes"])
        self.assertEqual(
            inherit["effective_lookback_days"],
            self.settings.disclosure_initial_lookback_days,
        )
        self.assertEqual(
            inherit["effective_sync_seconds"],
            self.settings.disclosure_sync_interval_seconds,
        )
        # Inherit rows resolve to the global policy's process list.
        self.assertGreater(len(inherit["effective_process_classes"]), 0)

        active_codes = {
            item["security_code"]
            for item in _list_all_tracked(self.app, {"status": "active"})
        }
        self.assertIn(self.code_override, active_codes)
        self.assertNotIn(self.code_inherit, active_codes)

    def test_put_upsert_clears_absent_overrides(self) -> None:
        first = _api_request(
            self.app,
            "PUT",
            "/v1/admin/tracked-companies",
            headers=_ADMIN_HEADERS,
            json_body={
                "entries": [
                    {
                        "security_code": self.code_override,
                        "exchange": "LOCAL",
                        "lookback_days": 30,
                        "process_classes": ["annual_report"],
                    }
                ]
            },
        )
        self.assertEqual(first.status_code, 200, first.body)

        second = _api_request(
            self.app,
            "PUT",
            "/v1/admin/tracked-companies",
            headers=_ADMIN_HEADERS,
            json_body={
                "entries": [
                    {"security_code": self.code_override, "exchange": "LOCAL"}
                ]
            },
        )
        self.assertEqual(second.status_code, 200, second.body)
        self.assertEqual(second.json()["created_count"], 0)

        got_items = _list_all_tracked(self.app)
        row = {
            item["security_code"]: item for item in got_items
        }[self.code_override]
        self.assertIsNone(row["lookback_days"])
        self.assertIsNone(row["process_classes"])

    def test_delete_removes_pool_row_and_keeps_ledger(self) -> None:
        put = _api_request(
            self.app,
            "PUT",
            "/v1/admin/tracked-companies",
            headers=_ADMIN_HEADERS,
            json_body={
                "entries": [
                    {"security_code": self.code_override, "exchange": "LOCAL"}
                ]
            },
        )
        self.assertEqual(put.status_code, 200, put.body)

        deleted = _api_request(
            self.app,
            "DELETE",
            f"/v1/admin/tracked-companies/{self.code_override}",
            headers=_ADMIN_HEADERS,
            query={"exchange": "LOCAL"},
        )
        self.assertEqual(deleted.status_code, 200, deleted.body)
        payload = deleted.json()
        self.assertEqual(payload["security_code"], self.code_override)
        self.assertEqual(payload["documents_retained"], 0)

        codes = {
            item["security_code"] for item in _list_all_tracked(self.app)
        }
        self.assertNotIn(self.code_override, codes)
        # Ledger rows (company + security) survive the pool removal.
        with self.engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.security "
                    "WHERE security_code = :code AND exchange = 'LOCAL'"
                ),
                {"code": self.code_override},
            ).scalar()
        self.assertEqual(count, 1)

        again = _api_request(
            self.app,
            "DELETE",
            f"/v1/admin/tracked-companies/{self.code_override}",
            headers=_ADMIN_HEADERS,
            query={"exchange": "LOCAL"},
        )
        self.assertEqual(again.status_code, 404, again.body)
        self.assertEqual(again.json()["error_code"], "NOT_FOUND")

    def test_unknown_process_class_and_bad_status_filter_are_422(self) -> None:
        bad_put = _api_request(
            self.app,
            "PUT",
            "/v1/admin/tracked-companies",
            headers=_ADMIN_HEADERS,
            json_body={
                "entries": [
                    {
                        "security_code": self.code_override,
                        "exchange": "LOCAL",
                        "process_classes": ["not_a_class"],
                    }
                ]
            },
        )
        self.assertEqual(bad_put.status_code, 422, bad_put.body)
        self.assertEqual(bad_put.json()["error_code"], "VALIDATION_ERROR")

        bad_get = _api_request(
            self.app, "GET", "/v1/tracked-companies", query={"status": "gone"}
        )
        self.assertEqual(bad_get.status_code, 422, bad_get.body)
        self.assertEqual(bad_get.json()["error_code"], "VALIDATION_ERROR")


if __name__ == "__main__":
    unittest.main()
