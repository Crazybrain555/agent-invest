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
    def test_online_migrations_never_fall_back_to_runtime_database_url(self) -> None:
        env_path = (
            Path(__file__).parents[2]
            / "src/disclosure_anchor/adapters/db/postgres/migrations/env.py"
        )
        source = env_path.read_text(encoding="utf-8")

        self.assertIn("_resolve_url(offline=False)", source)
        self.assertIn("DISCLOSURE_MIGRATION_DATABASE_URL", source)
        self.assertNotIn("settings.database_url", source)

    def test_head_comes_from_alembic_graph(self) -> None:
        heads = migration_heads()
        self.assertEqual(len(heads), 1)
        self.assertEqual(single_migration_head(), heads[0])

        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0053_remote_parse_checkpoint"
        )
        self.assertEqual(heads[0], migration.revision)
        self.assertEqual(
            migration.down_revision,
            "0052_publish_kpi_indexes",
        )

    def test_0051_binds_only_first_later_exact_uscc_observation(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0051_backfill_uscc_source_access"
        )

        sql = migration._EXACT_PROFILE_MATCHES
        self.assertIn("provider_interface = 'cninfo:p_stock2100'", sql)
        self.assertIn("sa.query_params ->> 'scode' = sec.security_code", sql)
        self.assertIn("= ci.normalized_value", sql)
        self.assertIn("sa.accessed_at >= ci.created_at", sql)
        self.assertIn("ORDER BY sa.accessed_at ASC", sql)
        self.assertIn("ci.source_access_id IS NULL", sql)

    def test_0050_checks_both_plural_route_arrays_without_recreating_scalar(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0050_verify_unit_semantic_keys"
        )

        semantic_sql = migration._invalid_route_array_sql(
            column="semantic_keys", max_items=8
        )
        section_sql = migration._invalid_route_array_sql(
            column="section_keys", max_items=None
        )
        for sql in (semantic_sql, section_sql):
            self.assertIn("jsonb_typeof", sql)
            self.assertIn("jsonb_array_elements", sql)
            self.assertIn("count(DISTINCT", sql)
            self.assertIn("^[a-z][a-z0-9_]", sql)
        self.assertIn("jsonb_array_length(semantic_keys) > 8", semantic_sql)
        self.assertNotIn("semantic_key ", semantic_sql)

    def test_0048_ops_views_exclude_only_failures_with_later_success(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0048_remediation_aware_unit_build_ops"
        )

        for sql in (
            migration._pending_build_view_sql(remediation_aware=True),
            migration._terminal_view_sql(remediation_aware=True),
        ):
            self.assertIn("NOT EXISTS", sql)
            self.assertIn("newer.unit_build_status = 'succeeded'", sql)
            self.assertIn(
                "> (r.started_at, r.processing_run_id)",
                sql,
            )
        self.assertNotIn(
            "NOT EXISTS",
            migration._pending_build_view_sql(remediation_aware=False),
        )

    def test_0033_unit_view_keeps_only_unit_owned_scope_fields(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0033_unit_schema_convergence"
        )

        current = migration._document_units_view_sql(include_legacy_duplicates=False)
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

        current = migration._document_units_view_sql(include_content_categories=False)
        self.assertIn("u.semantic_keys", current)
        self.assertIn("u.section_keys", current)
        self.assertNotIn("content_categories", current)

        prior = migration._document_units_view_sql(include_content_categories=True)
        self.assertIn("class_content_categories AS content_categories", prior)

    def test_0038_versions_the_breaking_unit_view_change(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0038_version_unit_public_view"
        )

        v1 = migration._document_units_view_sql(
            view_name="document_units_v1",
            contract_version="document_unit.v1",
            include_content_categories=True,
        )
        v2 = migration._document_units_view_sql(
            view_name="document_units_v2",
            contract_version="document_unit.v2",
            include_content_categories=False,
        )

        self.assertIn("class_content_categories AS content_categories", v1)
        self.assertIn("'document_unit.v1'::text", v1)
        self.assertNotIn("content_categories", v2)
        self.assertIn("AS body_status", v2)
        self.assertIn("'document_unit.v2'::text", v2)

    def test_0039_converges_to_one_clean_v1_unit_view(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0039_single_unit_public_view"
        )

        current_v1 = migration._document_units_view_sql(
            view_name="document_units_v1",
            contract_version="document_unit.v1",
            include_content_categories=False,
            include_body_status=True,
        )
        legacy_v1 = migration._document_units_view_sql(
            view_name="document_units_v1",
            contract_version="document_unit.v1",
            include_content_categories=True,
            include_body_status=False,
        )

        self.assertNotIn("content_categories", current_v1)
        self.assertIn("AS body_status", current_v1)
        self.assertIn("'document_unit.v1'::text", current_v1)
        self.assertIn("class_content_categories AS content_categories", legacy_v1)
        self.assertNotIn("AS body_status", legacy_v1)

        with patch.object(migration.op, "execute") as execute:
            migration.upgrade()
        statements = [str(call.args[0]) for call in execute.call_args_list]
        self.assertIn(
            "DROP VIEW IF EXISTS disclosure_public.document_units_v2", statements[0]
        )
        self.assertIn("DROP VIEW disclosure_public.document_units_v1", statements[1])
        self.assertIn("CREATE VIEW disclosure_public.document_units_v1", statements[2])
        self.assertNotIn("document_units_v2", statements[-1])

    def test_0040_exposes_total_route_collections_without_fabricating_scalar(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0040_unit_route_arrays_nonnull"
        )

        current = migration._document_units_view_sql(route_arrays_nonnull=True)
        legacy = migration._document_units_view_sql(route_arrays_nonnull=False)

        self.assertIn(
            "COALESCE(u.semantic_keys, '[]'::jsonb) AS semantic_keys",
            current,
        )
        self.assertIn(
            "COALESCE(u.section_keys, '[]'::jsonb) AS section_keys",
            current,
        )
        self.assertIn("u.semantic_key,", current)
        self.assertNotIn("COALESCE(u.semantic_key,", current)
        self.assertNotIn("COALESCE(u.semantic_keys", legacy)
        self.assertNotIn("COALESCE(u.section_keys", legacy)

    def test_0041_drops_only_the_public_scalar_lead_key(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0041_drop_public_unit_semantic_key"
        )

        current = migration._document_units_view_sql(
            include_scalar_semantic_key=False
        )
        legacy = migration._document_units_view_sql(
            include_scalar_semantic_key=True
        )

        self.assertNotIn("u.semantic_key,", current)
        self.assertIn(
            "COALESCE(u.semantic_keys, '[]'::jsonb) AS semantic_keys", current
        )
        self.assertIn(
            "COALESCE(u.section_keys, '[]'::jsonb) AS section_keys", current
        )
        self.assertIn("u.semantic_key,", legacy)
        self.assertIn("'document_unit.v1'::text", current)
        self.assertIn("AS body_status", current)

    def test_0042_outline_aggregates_full_route_arrays(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0042_outline_full_route_keys"
        )

        current = migration._outline_view_sql(aggregate_full_arrays=True)
        legacy = migration._outline_view_sql(aggregate_full_arrays=False)

        self.assertIn("jsonb_array_elements_text(u.semantic_keys)", current)
        # The public column keeps its varchar(128)[] type across the semantic
        # widening; a schema-pinned consumer must not observe a type change.
        self.assertIn("(route.key)::varchar(128)", current)
        self.assertNotIn("u.semantic_key,", current)
        self.assertNotIn("array_agg(DISTINCT u.semantic_key)", current)
        self.assertIn("'document_outline.v1'::text", current)
        self.assertIn("array_agg(DISTINCT u.semantic_key)", legacy)

    def test_0043_classifies_visual_only_mixed_units_without_deleting_them(
        self,
    ) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0043_visual_only_unit_body_status"
        )

        current = migration._document_units_view_sql(visual_only_aware=True)
        legacy = migration._document_units_view_sql(visual_only_aware=False)

        self.assertIn("jsonb_array_elements(u.payload -> 'parts')", current)
        self.assertIn("field.key <> 'content_artifacts'", current)
        self.assertIn("THEN 'heading_only'", current)
        self.assertIn("THEN 'empty'", current)
        self.assertNotIn("jsonb_array_elements(u.payload -> 'parts')", legacy)

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
