from __future__ import annotations

import importlib
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from disclosure_anchor.adapters.db.postgres.migration_state import (
    migration_heads,
    single_migration_head,
)


class MigrationStateTests(unittest.TestCase):
    def test_head_comes_from_alembic_graph(self) -> None:
        heads = migration_heads()
        self.assertEqual(len(heads), 1)
        self.assertEqual(single_migration_head(), heads[0])
        self.assertEqual(heads[0], "0032_provider_document_output")

        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0032_provider_document_output"
        )
        self.assertEqual(migration.down_revision, "0031_artifact_owner_run")

    def test_0031_reset_gate_remains_immutable_history(self) -> None:

        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0031_artifact_owner_run"
        )
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = True
        with (
            patch.object(migration.op, "get_bind", return_value=connection),
            patch.object(migration.op, "execute") as execute,
            self.assertRaisesRegex(RuntimeError, "derived corpus reset"),
        ):
            migration.upgrade()
        execute.assert_not_called()

    def test_0032_queue_views_are_provider_only_without_public_view_drift(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0032_provider_document_output"
        )

        for sql in (
            migration._pending_build_view_sql(provider_only=True),
            migration._pending_publish_view_sql(provider_only=True),
        ):
            self.assertIn("provider_document_relpath IS NOT NULL", sql)
            self.assertIn("normalized_ir_relpath IS NULL", sql)
            self.assertIn("run_kind IN ('parse', 'rebuild_units')", sql)
        parse_sql = migration._pending_parse_view_sql(provider_only=True)
        self.assertIn("provider_document_relpath IS NOT NULL", parse_sql)
        self.assertIn("normalized_ir_relpath IS NULL", parse_sql)
        source = Path(migration.__file__).read_text(encoding="utf-8")
        self.assertNotIn("disclosure_public", source)
        self.assertNotIn("processing_runs_v1", source)

    def test_0032_upgrade_and_downgrade_fail_before_destructive_ddl(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0032_provider_document_output"
        )
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = 1
        with (
            patch.object(migration.op, "get_bind", return_value=connection),
            patch.object(migration.op, "batch_alter_table") as batch,
            self.assertRaisesRegex(RuntimeError, "historical parse/rebuild"),
        ):
            migration.upgrade()
        batch.assert_not_called()

        connection.execute.return_value.scalar_one.return_value = True
        with (
            patch.object(migration.op, "get_bind", return_value=connection),
            patch.object(migration.op, "execute") as execute,
            patch.object(migration.op, "batch_alter_table") as batch,
            self.assertRaisesRegex(RuntimeError, "cannot downgrade"),
        ):
            migration.downgrade()
        execute.assert_not_called()
        batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
