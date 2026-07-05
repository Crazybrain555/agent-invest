import importlib
import unittest

from asset_intake.db import models
from asset_intake.db.bootstrap import quote_ident, quote_literal
from asset_intake.db.schema import ALL_ROLES, ALL_SCHEMAS, DATABASE_NAME

migration_0001 = importlib.import_module("asset_intake.db.migrations.versions.0001_initial")


class SchemaConstantTests(unittest.TestCase):
    def test_intake_namespace_only(self) -> None:
        self.assertEqual(ALL_SCHEMAS, ("intake_core", "intake_public", "intake_ops"))
        self.assertEqual(
            ALL_ROLES, ("intake_owner", "intake_app", "intake_reader", "future_l2_reader")
        )
        self.assertEqual(DATABASE_NAME, "invest_engine")

    def test_quoting_helpers(self) -> None:
        self.assertEqual(quote_ident('a"b'), '"a""b"')
        self.assertEqual(quote_literal("a'b"), "'a''b'")


class FrozenMigrationTests(unittest.TestCase):
    def test_frozen_snapshot_matches_live_models(self) -> None:
        live = {
            (t.schema, t.name): sorted(c.name for c in t.columns)
            for t in models.metadata.tables.values()
        }
        frozen = {
            (t.schema, t.name): sorted(c.name for c in t.columns)
            for t in migration_0001.FROZEN_METADATA.tables.values()
        }
        self.assertEqual(live, frozen)

    def test_no_disclosure_references_anywhere(self) -> None:
        for statement in migration_0001.GRANT_SQL:
            self.assertNotIn("disclosure", statement)
        for table in migration_0001.FROZEN_METADATA.tables.values():
            self.assertTrue(table.schema.startswith("intake_"), table.schema)

    def test_envelope_enums_match_shared_kernel(self) -> None:
        from envelope_kernel import AssetKind, QualityStatus, SourceTier, TraceLevel

        self.assertEqual(set(migration_0001.ASSET_KINDS), {k.value for k in AssetKind})
        self.assertEqual(set(migration_0001.SOURCE_TIERS), {t.value for t in SourceTier})
        self.assertEqual(set(migration_0001.TRACE_LEVELS), {t.value for t in TraceLevel})
        self.assertEqual(
            set(migration_0001.QUALITY_STATUSES), {q.value for q in QualityStatus}
        )

    def test_dedup_and_minimal_core_constraints_present(self) -> None:
        table = models.data_asset
        constraint_names = {c.name for c in table.constraints}
        self.assertIn("uq_data_asset_provider_dedup", constraint_names)
        self.assertIn("ck_data_asset_payload_or_raw", constraint_names)
        self.assertFalse(table.c.observed_at.nullable)
        self.assertFalse(table.c.source_access_id.nullable)
        self.assertFalse(table.c.processing_run_id.nullable)


if __name__ == "__main__":
    unittest.main()
