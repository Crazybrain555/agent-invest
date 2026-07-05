"""Live-DB FakeProvider registration matrix (framework v1.2 §8 P3b).

Covers ok / empty / error source_access states and the idempotency tri-state
(observed / materialized / supersede) against the real intake schema. Skips
without the intake DSNs; asserts the disclosure_* footprint is untouched.
"""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import UTC, datetime

from sqlalchemy import text

ADMIN_URL = os.environ.get("ASSET_INTAKE_ADMIN_DATABASE_URL")
MIGRATION_URL = os.environ.get("ASSET_INTAKE_MIGRATION_DATABASE_URL")

DISCLOSURE_SNAPSHOT_SQL = """
    SELECT n.nspname, c.relname FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname LIKE 'disclosure%' ORDER BY 1, 2
"""


@unittest.skipUnless(
    ADMIN_URL and MIGRATION_URL,
    "live-DB test: set ASSET_INTAKE_ADMIN_DATABASE_URL and ASSET_INTAKE_MIGRATION_DATABASE_URL",
)
class RegisterDatasetSnapshotTests(unittest.TestCase):
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
        env = dict(os.environ, PYTHONPATH="src")
        subprocess.run(
            [".venv/bin/python", "-m", "alembic", "upgrade", "head"],
            cwd=service_root, env=env, check=True, capture_output=True,
        )
        admin.dispose()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        from asset_intake.providers.registry import load_dataset_entries

        self.entry = load_dataset_entries()["cn_equity.eod_quote"]
        with self.engine.connect() as conn:
            self.disclosure_before = conn.execute(text(DISCLOSURE_SNAPSHOT_SQL)).fetchall()
        # Unique security per test run keeps semantic keys isolated and reruns idempotent.
        self.security = f"T{uuid.uuid4().hex[:6].upper()}.SZ"
        self.params = {
            "security": self.security,
            "start_date": "2026-07-01",
            "end_date": "2026-07-03",
        }

    def tearDown(self) -> None:
        with self.engine.connect() as conn:
            after = conn.execute(text(DISCLOSURE_SNAPSHOT_SQL)).fetchall()
        self.assertEqual(self.disclosure_before, after)

    def _provider(self, records, *, error=None):
        from envelope_kernel import SourceTier, TraceLevel

        from asset_intake.providers.port import DatasetResult, ProviderError, ScopeHints

        class FakeProvider:
            provider_name = "fake_provider"
            adapter_name = "tests.fake"
            adapter_version = "1"
            source_tier = SourceTier.TIER_1
            trace_level = TraceLevel.G1

            def fetch(self, request):
                if error:
                    raise ProviderError(error)
                return DatasetResult(
                    records=records,
                    returned_fields=sorted({k for r in records for k in r}) if records else [],
                    provider_as_of="20260706",
                    locator="fake://eod?params_hash=x",
                    scope=ScopeHints(published_at=datetime(2026, 7, 3, 16, tzinfo=UTC)),
                    stats={"rows": len(records)},
                )

        return FakeProvider()

    def _row(self, trade_date="2026-07-03", close=10.29, factor=85.33):
        return {
            "security": self.security, "trade_date": trade_date, "open": 10.2, "high": 10.4,
            "low": 10.1, "close": close, "pre_close": 10.28, "volume": 86332664.0,
            "amount": 888789393.0, "adj_factor": factor,
        }

    def _register(self, provider):
        from asset_intake.application.register_dataset import register_dataset_snapshot
        from asset_intake.providers.port import DatasetRequest

        return register_dataset_snapshot(
            self.engine, self.entry, provider,
            DatasetRequest(dataset_key=self.entry.dataset_key, query_params=self.params),
        )

    def _fetch_one(self, sql, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).first()

    def test_materialized_then_observed_then_supersede(self) -> None:
        first = self._register(self._provider([self._row()]))
        self.assertEqual(first.status, "materialized")
        assert first.asset_id is not None
        asset = self._fetch_one(
            "SELECT asset_kind, payload_kind, provider, source_tier, trace_level, is_active,"
            " semantic_key, material_type FROM intake_core.data_asset WHERE asset_id = :a",
            a=first.asset_id,
        )
        self.assertEqual(
            (asset.asset_kind, asset.payload_kind, asset.provider, asset.source_tier,
             asset.trace_level, asset.is_active, asset.material_type),
            ("dataset_snapshot", "recordset", "fake_provider", "tier_1", "G1", True, "market_quote"),
        )
        event = self._fetch_one(
            "SELECT event_kind, subject_ref FROM intake_ops.outbox_event WHERE asset_id = :a",
            a=first.asset_id,
        )
        self.assertEqual(event.event_kind, "materialized")
        self.assertTrue(
            event.subject_ref.startswith("asset://asset_intake/v1/dataset_snapshot/"),
            event.subject_ref,
        )

        second = self._register(self._provider([self._row()]))
        self.assertEqual(second.status, "observed")
        self.assertEqual(second.asset_id, first.asset_id)
        observed_event = self._fetch_one(
            "SELECT count(*) AS n FROM intake_ops.outbox_event"
            " WHERE asset_id = :a AND event_kind = 'observed'",
            a=first.asset_id,
        )
        self.assertEqual(observed_event.n, 1)

        third = self._register(self._provider([self._row(factor=86.0)]))
        self.assertEqual(third.status, "materialized")
        self.assertEqual(third.superseded_asset_id, first.asset_id)
        old = self._fetch_one(
            "SELECT is_active, superseded_by FROM intake_core.data_asset WHERE asset_id = :a",
            a=first.asset_id,
        )
        self.assertEqual((old.is_active, old.superseded_by), (False, third.asset_id))

    def test_empty_records_source_access_without_asset(self) -> None:
        outcome = self._register(self._provider([]))
        self.assertEqual(outcome.status, "empty")
        access = self._fetch_one(
            "SELECT result_status, result_count, dataset_key FROM intake_core.source_access"
            " WHERE access_id = :a", a=outcome.access_id,
        )
        self.assertEqual((access.result_status, access.result_count), ("empty", 0))
        self.assertEqual(access.dataset_key, "cn_equity.eod_quote")
        asset = self._fetch_one(
            "SELECT count(*) AS n FROM intake_core.data_asset WHERE source_access_id = :a",
            a=outcome.access_id,
        )
        self.assertEqual(asset.n, 0)

    def test_provider_error_distinct_from_empty(self) -> None:
        outcome = self._register(self._provider(None, error="rate limited"))
        self.assertEqual(outcome.status, "error")
        access = self._fetch_one(
            "SELECT result_status, error FROM intake_core.source_access WHERE access_id = :a",
            a=outcome.access_id,
        )
        self.assertEqual(access.result_status, "error")
        self.assertIn("rate limited", access.error["message"])
        run = self._fetch_one(
            "SELECT status FROM intake_core.processing_run WHERE run_id = :r", r=outcome.run_id
        )
        self.assertEqual(run.status, "failed")

    def test_invalid_request_rejected_before_any_row(self) -> None:
        from asset_intake.providers.registry import RegistryError

        with self.assertRaises(RegistryError):
            from asset_intake.application.register_dataset import register_dataset_snapshot
            from asset_intake.providers.port import DatasetRequest

            register_dataset_snapshot(
                self.engine, self.entry, self._provider([self._row()]),
                DatasetRequest(dataset_key=self.entry.dataset_key,
                               query_params={**self.params, "bogus": 1}),
            )


if __name__ == "__main__":
    unittest.main()
