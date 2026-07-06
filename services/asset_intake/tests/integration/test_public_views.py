"""intake_public view contract and role lockdown tests (M-C P4). Live-DB gated."""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from asset_intake.contracts import PUBLIC_VIEW_COLUMNS as EXPECTED_VIEW_COLUMNS

ADMIN_URL = os.environ.get("ASSET_INTAKE_ADMIN_DATABASE_URL")
MIGRATION_URL = os.environ.get("ASSET_INTAKE_MIGRATION_DATABASE_URL")


@unittest.skipUnless(
    ADMIN_URL and MIGRATION_URL,
    "live-DB test: set ASSET_INTAKE_ADMIN_DATABASE_URL and ASSET_INTAKE_MIGRATION_DATABASE_URL",
)
class PublicViewTests(unittest.TestCase):
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
        from tests.integration._cleanup import purge_test_rows

        purge_test_rows(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        from tests.integration._cleanup import purge_test_rows

        purge_test_rows(cls.engine)
        cls.engine.dispose()

    def test_view_columns_match_contract(self) -> None:
        with self.engine.connect() as conn:
            for view, expected in EXPECTED_VIEW_COLUMNS.items():
                rows = conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'intake_public' AND table_name = :v "
                        "ORDER BY ordinal_position"
                    ),
                    {"v": view},
                ).fetchall()
                self.assertEqual([r[0] for r in rows], expected, view)

    def test_registered_asset_readable_via_view_with_asset_uri(self) -> None:
        from envelope_kernel import SourceTier, TraceLevel

        from asset_intake.application.register_dataset import register_dataset_snapshot
        from asset_intake.providers.port import DatasetRequest, DatasetResult, ScopeHints
        from asset_intake.providers.registry import load_dataset_entries

        entry = load_dataset_entries()["cn_equity.eod_quote"]
        security = f"V{uuid.uuid4().hex[:6].upper()}.SZ"

        class FakeProvider:
            provider_name = "fake_provider"
            adapter_name = "tests.fake"
            adapter_version = "1"
            source_tier = SourceTier.TIER_1
            trace_level = TraceLevel.G1

            def fetch(self, request: DatasetRequest) -> DatasetResult:
                return DatasetResult(
                    records=[{
                        "security": security, "trade_date": "2026-07-03", "open": 1.0,
                        "high": 1.2, "low": 0.9, "close": 1.1, "pre_close": 1.0,
                        "volume": 100.0, "amount": 110.0, "adj_factor": 2.0,
                    }],
                    returned_fields=["security", "trade_date"],
                    provider_as_of="20260706",
                    locator="fake://eod",
                    scope=ScopeHints(published_at=datetime(2026, 7, 3, 16, tzinfo=UTC)),
                )

        outcome = register_dataset_snapshot(
            self.engine, entry, FakeProvider(),
            DatasetRequest(dataset_key=entry.dataset_key, query_params={
                "security": security, "start_date": "2026-07-01", "end_date": "2026-07-03",
            }),
        )
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT asset_uri, provider, semantic_key, is_active "
                    "FROM intake_public.data_assets_v1 WHERE asset_id = :a"
                ),
                {"a": outcome.asset_id},
            ).first()
            self.assertEqual(
                row.asset_uri, f"asset://asset_intake/v1/dataset_snapshot/{outcome.asset_id}"
            )
            event = conn.execute(
                text(
                    "SELECT change_seq, source, event_kind FROM intake_public.change_events_v1 "
                    "WHERE asset_id = :a ORDER BY change_seq"
                ),
                {"a": outcome.asset_id},
            ).first()
            self.assertEqual((event.source, event.event_kind), ("asset_intake", "materialized"))
            self.assertGreater(event.change_seq, 0)

    def test_reader_can_select_views_but_not_core(self) -> None:
        with self.engine.connect() as conn:
            conn.execute(text("BEGIN"))
            conn.execute(text("SET LOCAL ROLE intake_reader"))
            for view in EXPECTED_VIEW_COLUMNS:
                conn.execute(text(f"SELECT 1 FROM intake_public.{view} LIMIT 1"))
            conn.execute(text("ROLLBACK"))
            for target in ("intake_core.data_asset", "intake_ops.outbox_event"):
                conn.execute(text("BEGIN"))
                conn.execute(text("SET LOCAL ROLE intake_reader"))
                with self.assertRaises(Exception, msg=target):
                    conn.execute(text(f"SELECT 1 FROM {target} LIMIT 1"))
                conn.execute(text("ROLLBACK"))

    def test_supersede_partial_index_exists(self) -> None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'intake_core' "
                    "AND indexname = 'ix_data_asset_provider_semantic_active'"
                )
            ).first()
        self.assertIsNotNone(row)
        self.assertIn("WHERE is_active", row.indexdef)


if __name__ == "__main__":
    unittest.main()
