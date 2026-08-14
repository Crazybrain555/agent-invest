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
        self.assertEqual(heads[0], "0037_unit_facets")

        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0037_remove_unit_content_categories"
        )
        self.assertEqual(migration.down_revision, "0036_unit_section_routes")

    def test_0033_unit_view_keeps_only_unit_owned_scope_fields(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0033_unit_schema_convergence"
        )

        current = migration._document_units_view_sql(
            include_legacy_duplicates=False
        )
        self.assertIn("u.semantic_key", current)
        self.assertNotIn("u.semantic_keys", current)
        self.assertNotIn("publisher_categories", current)
        self.assertNotIn("class_market", current)
        self.assertNotIn("content_categories", current)

        legacy = migration._document_units_view_sql(include_legacy_duplicates=True)
        self.assertIn("u.semantic_keys", legacy)
        self.assertIn("publisher_categories", legacy)

    def test_0033_refuses_to_drop_independent_plural_semantics(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0033_unit_schema_convergence"
        )
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = 1
        with (
            patch.object(migration.op, "get_bind", return_value=connection),
            patch.object(migration.op, "execute") as execute,
            patch.object(migration.op, "drop_column") as drop_column,
            self.assertRaisesRegex(RuntimeError, "information not present"),
        ):
            migration.upgrade()
        execute.assert_not_called()
        drop_column.assert_not_called()

    def test_0034_restores_unit_routes_and_only_the_content_facet(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0034_unit_semantic_routes"
        )

        current = migration._document_units_view_sql(include_semantic_routes=True)
        self.assertIn("u.semantic_key", current)
        self.assertIn("u.semantic_keys", current)
        self.assertIn("class_content_categories AS content_categories", current)
        self.assertNotIn("publisher_categories", current)
        self.assertNotIn("class_market", current)

        prior = migration._document_units_view_sql(include_semantic_routes=False)
        self.assertNotIn("u.semantic_keys", prior)
        self.assertNotIn("content_categories", prior)

    def test_0034_refuses_to_discard_secondary_routes(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0034_unit_semantic_routes"
        )
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = 1
        with (
            patch.object(migration.op, "get_bind", return_value=connection),
            patch.object(migration.op, "execute") as execute,
            patch.object(migration.op, "drop_column") as drop_column,
            self.assertRaisesRegex(RuntimeError, "secondary Unit routes"),
        ):
            migration.downgrade()
        execute.assert_not_called()
        drop_column.assert_not_called()

    def test_0035_refuses_to_discard_hash_bound_semantic_receipts(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0035_semantic_receipt_integrity"
        )
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = 1
        with (
            patch.object(migration.op, "get_bind", return_value=connection),
            patch.object(migration.op, "drop_constraint") as drop_constraint,
            patch.object(migration.op, "drop_column") as drop_column,
            self.assertRaisesRegex(RuntimeError, "bound receipts"),
        ):
            migration.downgrade()
        drop_constraint.assert_not_called()
        drop_column.assert_not_called()

    def test_0036_separates_section_routes_from_unit_topics(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0036_unit_section_routes"
        )

        current = migration._document_units_view_sql(include_section_keys=True)
        self.assertIn("u.semantic_keys", current)
        self.assertIn("u.section_keys", current)
        self.assertIn("class_content_categories AS content_categories", current)
        self.assertNotIn("publisher_categories", current)
        self.assertNotIn("class_market", current)

        prior = migration._document_units_view_sql(include_section_keys=False)
        self.assertIn("u.semantic_keys", prior)
        self.assertNotIn("u.section_keys", prior)
        self.assertIn("class_content_categories AS content_categories", prior)

    def test_0036_refuses_to_discard_section_routes(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0036_unit_section_routes"
        )
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = 1
        with (
            patch.object(migration.op, "get_bind", return_value=connection),
            patch.object(migration.op, "execute") as execute,
            patch.object(migration.op, "drop_column") as drop_column,
            self.assertRaisesRegex(RuntimeError, "normalized section routes"),
        ):
            migration.downgrade()
        execute.assert_not_called()
        drop_column.assert_not_called()

    def test_0037_keeps_content_categories_document_only(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0037_remove_unit_content_categories"
        )

        current = migration._document_units_view_sql(
            include_content_categories=False
        )
        self.assertIn("u.semantic_keys", current)
        self.assertIn("u.section_keys", current)
        self.assertNotIn("content_categories", current)

        prior = migration._document_units_view_sql(include_content_categories=True)
        self.assertIn("class_content_categories AS content_categories", prior)

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
