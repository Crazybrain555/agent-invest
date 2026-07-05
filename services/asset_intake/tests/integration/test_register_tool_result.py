"""Live-DB tool_result registration tests (M-C P6). Skips without intake DSNs."""

from __future__ import annotations

import os
import unittest
import uuid

from sqlalchemy import text

ADMIN_URL = os.environ.get("ASSET_INTAKE_ADMIN_DATABASE_URL")
MIGRATION_URL = os.environ.get("ASSET_INTAKE_MIGRATION_DATABASE_URL")


@unittest.skipUnless(
    ADMIN_URL and MIGRATION_URL,
    "live-DB test: set ASSET_INTAKE_ADMIN_DATABASE_URL and ASSET_INTAKE_MIGRATION_DATABASE_URL",
)
class RegisterToolResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import subprocess
        from pathlib import Path

        from asset_intake.db.bootstrap import bootstrap_all
        from asset_intake.db.connection import create_db_engine

        service_root = Path(__file__).resolve().parents[2]
        assert ADMIN_URL is not None
        cls.engine = create_db_engine(ADMIN_URL)
        admin = create_db_engine(ADMIN_URL, autocommit=True)
        bootstrap_all(admin)
        admin.dispose()
        subprocess.run(
            [".venv/bin/python", "-m", "alembic", "upgrade", "head"],
            cwd=service_root, env=dict(os.environ, PYTHONPATH="src"), check=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def _submission(self, items, query=None):
        from envelope_kernel import PayloadKind

        from asset_intake.application.register_tool_result import ToolResultSubmission

        return ToolResultSubmission(
            provider="web_search",
            tool="test_search",
            adapter="tests.tool_result",
            adapter_version="1",
            payload_kind=PayloadKind.SEARCH_RESULT,
            query=query or {"q": f"nvda earnings {uuid.uuid4().hex[:6]}"},
            returned_items=items,
        )

    def test_materialized_then_observed_and_view_readable(self) -> None:
        from asset_intake.application.register_tool_result import register_tool_result

        items = [{"locator": "https://example.com/a", "title": "A", "snippet": "..."}]
        query = {"q": f"unique {uuid.uuid4().hex[:8]}"}
        first = register_tool_result(self.engine, self._submission(items, query))
        self.assertEqual(first.status, "materialized")
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT asset_kind, payload_kind, tool, source_tier, trace_level, asset_uri "
                    "FROM intake_public.data_assets_v1 WHERE asset_id = :a"
                ),
                {"a": first.asset_id},
            ).first()
        self.assertEqual(
            (row.asset_kind, row.payload_kind, row.tool, row.source_tier, row.trace_level),
            ("tool_result", "search_result", "test_search", "tier_2", "G2"),
        )
        self.assertEqual(row.asset_uri, f"asset://asset_intake/v1/tool_result/{first.asset_id}")

        second = register_tool_result(self.engine, self._submission(items, query))
        self.assertEqual(second.status, "observed")
        self.assertEqual(second.asset_id, first.asset_id)

    def test_empty_items_record_access_only(self) -> None:
        from asset_intake.application.register_tool_result import register_tool_result

        outcome = register_tool_result(self.engine, self._submission([]))
        self.assertEqual(outcome.status, "empty")
        with self.engine.connect() as conn:
            access = conn.execute(
                text(
                    "SELECT result_status, tool, dataset_key FROM intake_core.source_access "
                    "WHERE access_id = :a"
                ),
                {"a": outcome.access_id},
            ).first()
        self.assertEqual((access.result_status, access.tool), ("empty", "test_search"))
        self.assertIsNone(access.dataset_key)

    def test_item_without_locator_fails_fast(self) -> None:
        from asset_intake.application.register_tool_result import register_tool_result

        with self.assertRaises(ValueError) as ctx:
            register_tool_result(
                self.engine, self._submission([{"title": "no locator"}])
            )
        self.assertIn("locator", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
