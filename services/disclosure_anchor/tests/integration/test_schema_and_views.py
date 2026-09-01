"""Schema/migration shape checks against the migrated database."""

from __future__ import annotations

import subprocess
import unittest

from sqlalchemy import exc, text

from disclosure_anchor.adapters.db.postgres.catalog import view_names
from disclosure_anchor.adapters.db.postgres.schema import (
    ALEMBIC_VERSION_TABLE_SCHEMA,
    CORE_SCHEMA,
    OPS_SCHEMA,
    PUBLIC_SCHEMA,
    PUBLIC_VIEWS,
)
from disclosure_anchor.adapters.db.postgres.migration_state import single_migration_head
from tests.integration._support import engine_or_skip, run_alembic

EXPECTED_CORE_TABLES = {
    "company",
    "company_identifier",
    "security",
    "tracked_company",
    "source_access",
    "source_checkpoint",
    "document",
    "processing_run",
    "document_unit",
}


class SchemaShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_core_tables_exist(self) -> None:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :s"
                ),
                {"s": CORE_SCHEMA},
            ).scalars()
            tables = set(rows)
        self.assertTrue(EXPECTED_CORE_TABLES.issubset(tables), tables)

    def test_outbox_in_ops_schema(self) -> None:
        with self.engine.connect() as conn:
            present = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_name = 'outbox_event'"
                ),
                {"s": OPS_SCHEMA},
            ).scalar()
        self.assertTrue(present)

    def test_public_views_exist(self) -> None:
        with self.engine.connect() as conn:
            views = set(view_names(conn, schema=PUBLIC_SCHEMA))
        self.assertEqual(set(PUBLIC_VIEWS), views)

    def test_unit_public_view_is_granted_to_all_readers(self) -> None:
        with self.engine.connect() as conn:
            for role in (
                "disclosure_app",
                "disclosure_reader",
                "future_l2_reader",
            ):
                allowed = conn.execute(
                    text("SELECT has_table_privilege(:role, :view, 'SELECT')"),
                    {
                        "role": role,
                        "view": "disclosure_public.document_units_v1",
                    },
                ).scalar_one()
                self.assertTrue(allowed, role)

    def test_row_atom_public_view_is_read_only_for_all_readers(self) -> None:
        with self.engine.connect() as conn:
            for role in (
                "disclosure_app",
                "disclosure_reader",
                "future_l2_reader",
            ):
                can_select = conn.execute(
                    text("SELECT has_table_privilege(:role, :view, 'SELECT')"),
                    {
                        "role": role,
                        "view": "disclosure_public.unit_search_row_atoms_v1",
                    },
                ).scalar_one()
                self.assertTrue(can_select, role)
                for privilege in ("INSERT", "UPDATE", "DELETE"):
                    can_write = conn.execute(
                        text(
                            "SELECT has_table_privilege(:role, :view, :privilege)"
                        ),
                        {
                            "role": role,
                            "view": (
                                "disclosure_public.unit_search_row_atoms_v1"
                            ),
                            "privilege": privilege,
                        },
                    ).scalar_one()
                    self.assertFalse(can_write, (role, privilege))

    def test_alembic_version_in_ops_schema_and_at_head(self) -> None:
        with self.engine.connect() as conn:
            present = conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = :schema "
                    "AND table_name = 'alembic_version'"
                ),
                {"schema": ALEMBIC_VERSION_TABLE_SCHEMA},
            ).scalar_one()
            version = conn.execute(
                text(
                    f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE_SCHEMA}.alembic_version"
                )
            ).scalar()
            processing_run_columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema "
                        "AND table_name = 'processing_run'"
                    ),
                    {"schema": CORE_SCHEMA},
                ).scalars()
            )
            processing_run_constraints = set(
                conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = "
                        "'disclosure_core.processing_run'::regclass"
                    )
                ).scalars()
            )
            document_unit_columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema "
                        "AND table_name = 'document_unit'"
                    ),
                    {"schema": CORE_SCHEMA},
                ).scalars()
            )
            document_unit_constraints = set(
                conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE conrelid = "
                        "'disclosure_core.document_unit'::regclass"
                    )
                ).scalars()
            )
            terminal_view = conn.execute(
                text("SELECT to_regclass('disclosure_ops.unit_build_terminal_v1')")
            ).scalar_one()
        self.assertEqual(present, 1)
        self.assertEqual(version, single_migration_head())
        self.assertLessEqual(
            {
                "artifact_owner_processing_run_id",
                "parser_target_identity",
                "provider_document_relpath",
                "search_projection_error",
                "semantic_route_receipts_hash",
                "semantic_route_receipts_relpath",
                "semantic_route_receipts_contract_version",
                "semantic_adjudication_status",
                "semantic_degraded_unit_count",
                "semantic_failover_group_count",
                "semantic_adjudication_summary",
            },
            processing_run_columns,
        )
        self.assertLessEqual(
            {
                "ck_processing_run_parse_artifact_owner",
                "ck_processing_run_rebuild_artifact_owner",
                "fk_processing_run_artifact_owner",
                "ck_processing_run_parser_target_identity",
                "ck_processing_run_primary_output_exactly_one",
                "ck_processing_run_search_projection_error",
                "ck_processing_run_semantic_receipt_hash",
                "ck_processing_run_semantic_receipt_locator",
                "ck_processing_run_semantic_adjudication_status",
                "ck_processing_run_semantic_degraded_count",
                "ck_processing_run_semantic_failover_count",
                "ck_processing_run_semantic_summary",
                "ck_processing_run_error_object",
                "ck_processing_run_unit_build_error_object",
            },
            processing_run_constraints,
        )
        self.assertIn("semantic_keys", document_unit_columns)
        self.assertNotIn("semantic_key", document_unit_columns)
        self.assertIn("ck_document_unit_semantic_keys", document_unit_constraints)
        self.assertEqual(terminal_view, "disclosure_ops.unit_build_terminal_v1")

    def test_document_provider_hash_unique_index_exists(self) -> None:
        with self.engine.connect() as conn:
            present = conn.execute(
                text(
                    "SELECT 1 FROM pg_indexes "
                    "WHERE schemaname = :schema "
                    "AND indexname = 'uq_document_provider_doc_hash' "
                    "AND indexdef LIKE '%UNIQUE%'"
                ),
                {"schema": CORE_SCHEMA},
            ).scalar()
        self.assertEqual(present, 1)

    def test_security_identity_has_unique_and_canonical_constraints(self) -> None:
        with self.engine.connect() as conn:
            names = set(
                conn.execute(
                    text(
                        "SELECT constraint_name FROM information_schema.table_constraints "
                        "WHERE table_schema = :schema AND table_name = 'security'"
                    ),
                    {"schema": CORE_SCHEMA},
                ).scalars()
            )
        self.assertTrue(
            {
                "uq_security_code_exchange",
                "ck_security_code_canonical",
                "ck_security_exchange_canonical",
                "ck_security_mainland_exchange_code",
            }
            <= names,
            names,
        )

    def test_security_constraints_reject_unicode_aliases_and_wrong_exchange(
        self,
    ) -> None:
        company_id = "co_schema_security_constraints"
        conn = self.engine.connect()
        txn = conn.begin()
        try:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company (company_id, legal_name) "
                    "VALUES (:id, '约束构造公司') ON CONFLICT (company_id) DO NOTHING"
                ),
                {"id": company_id},
            )
            invalid = (
                ("000001\t", "SZSE"),
                ("000001\u00a0", "SZSE"),
                ("000001", "SZSE\t"),
                ("000001", "SSE"),
                ("1", "SZSE"),
            )
            for index, (code, exchange) in enumerate(invalid):
                with self.assertRaises(exc.IntegrityError), conn.begin_nested():
                    conn.execute(
                        text(
                            "INSERT INTO disclosure_core.security "
                            "(security_id, company_id, security_code, exchange) "
                            "VALUES (:id, :company, :code, :exchange)"
                        ),
                        {
                            "id": f"sec_schema_invalid_{index}",
                            "company": company_id,
                            "code": code,
                            "exchange": exchange,
                        },
                    )
        finally:
            txn.rollback()
            conn.close()

    def test_public_views_do_not_expose_relpath_columns(self) -> None:
        with self.engine.connect() as conn:
            leaking = conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND (column_name LIKE '%relpath%' "
                    "OR column_name LIKE '%abs_path%' OR column_name = 'error')"
                ),
                {"s": PUBLIC_SCHEMA},
            ).all()
        self.assertEqual(leaking, [], f"public views leak internal columns: {leaking}")

    def test_0034_semantic_routes_upgrade_downgrade_upgrade(self) -> None:
        self.addCleanup(self._restore_migration_head)

        down = self._alembic("downgrade", "0033_unit_schema_convergence")
        self.assertEqual(down.returncode, 0, down.stderr[-500:])
        with self.engine.connect() as conn:
            columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_public' "
                        "AND table_name = 'document_units_v1'"
                    )
                ).scalars()
            )
        self.assertNotIn("semantic_keys", columns)
        self.assertNotIn("section_keys", columns)
        self.assertNotIn("content_categories", columns)

        up = self._alembic("upgrade", "head")
        self.assertEqual(up.returncode, 0, up.stderr[-500:])
        with self.engine.connect() as conn:
            columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_public' "
                        "AND table_name = 'document_units_v1'"
                    )
                ).scalars()
            )
        self.assertLessEqual({"semantic_keys", "section_keys", "body_status"}, columns)
        self.assertNotIn("content_categories", columns)

    def test_0038_versions_the_unit_content_facet_removal(self) -> None:
        self.addCleanup(self._restore_migration_head)

        at_0038 = self._alembic("downgrade", "0038_unit_contract_v2")
        self.assertEqual(at_0038.returncode, 0, at_0038.stderr[-500:])

        with self.engine.connect() as conn:
            v1_columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_public' "
                        "AND table_name = 'document_units_v1'"
                    )
                ).scalars()
            )
            v2_columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_public' "
                        "AND table_name = 'document_units_v2'"
                    )
                ).scalars()
            )
            document_columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_public' "
                        "AND table_name = 'documents_v1'"
                    )
                ).scalars()
            )
        self.assertIn("content_categories", v1_columns)
        self.assertNotIn("content_categories", v2_columns)
        self.assertIn("content_categories", document_columns)

        down = self._alembic("downgrade", "0037_unit_facets")
        self.assertEqual(down.returncode, 0, down.stderr[-500:])
        with self.engine.connect() as conn:
            preserved_v1_columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_public' "
                        "AND table_name = 'document_units_v1'"
                    )
                ).scalars()
            )
            preserved_v2 = conn.execute(
                text("SELECT to_regclass('disclosure_public.document_units_v2')")
            ).scalar_one_or_none()
            current_revision = conn.execute(
                text(
                    f"SELECT version_num FROM "
                    f"{ALEMBIC_VERSION_TABLE_SCHEMA}.alembic_version"
                )
            ).scalar_one()
        self.assertIn("content_categories", preserved_v1_columns)
        self.assertIsNone(preserved_v2)
        self.assertEqual(current_revision, "0037_unit_facets")

        up = self._alembic("upgrade", "head")
        self.assertEqual(up.returncode, 0, up.stderr[-500:])

    def test_0039_keeps_only_the_clean_v1_unit_view(self) -> None:
        self.addCleanup(self._restore_migration_head)

        with self.engine.connect() as conn:
            v1_columns = tuple(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_public' "
                        "AND table_name = 'document_units_v1' "
                        "ORDER BY ordinal_position"
                    )
                ).scalars()
            )
            v2 = conn.execute(
                text("SELECT to_regclass('disclosure_public.document_units_v2')")
            ).scalar_one_or_none()

        self.assertEqual(v1_columns[-1], "body_status")
        self.assertNotIn("content_categories", v1_columns)
        self.assertIsNone(v2)

        down = self._alembic("downgrade", "0038_unit_contract_v2")
        self.assertEqual(down.returncode, 0, down.stderr[-500:])
        with self.engine.connect() as conn:
            restored_v1 = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_public' "
                        "AND table_name = 'document_units_v1'"
                    )
                ).scalars()
            )
            restored_v2 = conn.execute(
                text("SELECT to_regclass('disclosure_public.document_units_v2')")
            ).scalar_one_or_none()
        self.assertIn("content_categories", restored_v1)
        self.assertEqual(restored_v2, "disclosure_public.document_units_v2")

        up = self._alembic("upgrade", "head")
        self.assertEqual(up.returncode, 0, up.stderr[-500:])

    def test_0045_through_0047_preserve_plural_routes_and_clean_json_nulls(self) -> None:
        self.addCleanup(self._restore_migration_head)
        document_id = "doc_schema_semantic_terminal"
        run_id = "run_schema_semantic_terminal"
        asset_id = "du_schema_semantic_terminal"
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document (document_id, status) "
                    "VALUES (:document_id, 'registered')"
                ),
                {"document_id": document_id},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.processing_run "
                    "(processing_run_id, document_id, "
                    "artifact_owner_processing_run_id, run_kind, status, "
                    "normalized_ir_relpath) VALUES "
                    "(:run_id, :document_id, :run_id, 'parse', 'succeeded', "
                    "'derived/schema/normalized_ir.v4.json')"
                ),
                {"run_id": run_id, "document_id": document_id},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document_unit "
                    "(asset_id, document_id, processing_run_id, payload_kind, "
                    "order_index, semantic_keys, payload, content_hash) VALUES "
                    "(:asset_id, :document_id, :run_id, 'text', 1, "
                    "'[\"alpha_route\",\"beta_route\"]'::jsonb, "
                    "'{\"text\":\"probe\"}'::jsonb, 'sha256:probe')"
                ),
                {
                    "asset_id": asset_id,
                    "document_id": document_id,
                    "run_id": run_id,
                },
            )

        down = self._alembic("downgrade", "0044_unit_search_row_atoms")
        self.assertEqual(down.returncode, 0, down.stderr[-1000:])
        with self.engine.begin() as conn:
            scalar, plural = conn.execute(
                text(
                    "SELECT semantic_key, semantic_keys "
                    "FROM disclosure_core.document_unit WHERE asset_id = :asset_id"
                ),
                {"asset_id": asset_id},
            ).one()
            self.assertEqual(scalar, "alpha_route")
            self.assertEqual(plural, ["alpha_route", "beta_route"])
            conn.execute(
                text(
                    "UPDATE disclosure_core.processing_run "
                    "SET error = 'null'::jsonb, unit_build_error = 'null'::jsonb "
                    "WHERE processing_run_id = :run_id"
                ),
                {"run_id": run_id},
            )

        up = self._alembic("upgrade", "head")
        self.assertEqual(up.returncode, 0, up.stderr[-1000:])
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT error IS NULL, unit_build_error IS NULL "
                    "FROM disclosure_core.processing_run "
                    "WHERE processing_run_id = :run_id"
                ),
                {"run_id": run_id},
            ).one()
            self.assertEqual(tuple(row), (True, True))
            columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_core' "
                        "AND table_name = 'document_unit'"
                    )
                ).scalars()
            )
            plural = conn.execute(
                text(
                    "SELECT semantic_keys FROM disclosure_core.document_unit "
                    "WHERE asset_id = :asset_id"
                ),
                {"asset_id": asset_id},
            ).scalar_one()
            self.assertNotIn("semantic_key", columns)
            self.assertEqual(plural, ["alpha_route", "beta_route"])
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.document_unit "
                    "WHERE asset_id = :asset_id"
                ),
                {"asset_id": asset_id},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.processing_run "
                    "WHERE processing_run_id = :run_id"
                ),
                {"run_id": run_id},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.document "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": document_id},
            )

    def test_online_preflight_refuses_0047_scalar_only_route_loss(self) -> None:
        self.addCleanup(self._restore_migration_head)
        document_id = "doc_schema_scalar_only_guard"
        run_id = "run_schema_scalar_only_guard"
        asset_id = "du_schema_scalar_only_guard"

        down = self._alembic("downgrade", "0046_revoke_private_acl")
        self.assertEqual(down.returncode, 0, down.stderr[-1000:])
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document (document_id, status) "
                    "VALUES (:document_id, 'registered')"
                ),
                {"document_id": document_id},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.processing_run "
                    "(processing_run_id, document_id, "
                    "artifact_owner_processing_run_id, run_kind, status, "
                    "normalized_ir_relpath) VALUES "
                    "(:run_id, :document_id, :run_id, 'parse', 'succeeded', "
                    "'derived/schema/normalized_ir.v4.json')"
                ),
                {"run_id": run_id, "document_id": document_id},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document_unit "
                    "(asset_id, document_id, processing_run_id, payload_kind, "
                    "order_index, semantic_key, semantic_keys, payload, "
                    "content_hash) VALUES "
                    "(:asset_id, :document_id, :run_id, 'text', 1, "
                    "'scalar_only_route', NULL, '{\"text\":\"probe\"}'::jsonb, "
                    "'sha256:probe')"
                ),
                {
                    "asset_id": asset_id,
                    "document_id": document_id,
                    "run_id": run_id,
                },
            )

        refused = self._alembic("upgrade", "head")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("refuses to cross 0047", refused.stderr)
        with self.engine.begin() as conn:
            version, scalar, plural = conn.execute(
                text(
                    "SELECT v.version_num, u.semantic_key, u.semantic_keys "
                    "FROM disclosure_ops.alembic_version v "
                    "CROSS JOIN disclosure_core.document_unit u "
                    "WHERE u.asset_id = :asset_id"
                ),
                {"asset_id": asset_id},
            ).one()
            self.assertEqual(version, "0046_revoke_private_acl")
            self.assertEqual(scalar, "scalar_only_route")
            self.assertIsNone(plural)
            conn.execute(
                text(
                    "UPDATE disclosure_core.document_unit "
                    "SET semantic_keys = jsonb_build_array(semantic_key) "
                    "WHERE asset_id = :asset_id"
                ),
                {"asset_id": asset_id},
            )

        repaired = self._alembic("upgrade", "head")
        self.assertEqual(repaired.returncode, 0, repaired.stderr[-1000:])
        with self.engine.begin() as conn:
            plural = conn.execute(
                text(
                    "SELECT semantic_keys FROM disclosure_core.document_unit "
                    "WHERE asset_id = :asset_id"
                ),
                {"asset_id": asset_id},
            ).scalar_one()
            self.assertEqual(plural, ["scalar_only_route"])
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.document_unit "
                    "WHERE asset_id = :asset_id"
                ),
                {"asset_id": asset_id},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.processing_run "
                    "WHERE processing_run_id = :run_id"
                ),
                {"run_id": run_id},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.document "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": document_id},
            )

    def test_0050_refuses_invalid_surviving_plural_routes(self) -> None:
        self.addCleanup(self._restore_migration_head)
        document_id = "doc_schema_0050_plural_guard"
        run_id = "run_schema_0050_plural_guard"
        asset_id = "du_schema_0050_plural_guard"

        down = self._alembic("downgrade", "0049_app_head_read")
        self.assertEqual(down.returncode, 0, down.stderr[-1000:])
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document (document_id, status) "
                    "VALUES (:document_id, 'registered')"
                ),
                {"document_id": document_id},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.processing_run "
                    "(processing_run_id, document_id, "
                    "artifact_owner_processing_run_id, run_kind, status, "
                    "normalized_ir_relpath) VALUES "
                    "(:run_id, :document_id, :run_id, 'parse', 'succeeded', "
                    "'derived/schema/normalized_ir.v4.json')"
                ),
                {"run_id": run_id, "document_id": document_id},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document_unit "
                    "(asset_id, document_id, processing_run_id, payload_kind, "
                    "order_index, semantic_keys, payload, content_hash) VALUES "
                    "(:asset_id, :document_id, :run_id, 'text', 1, "
                    "'[\"duplicate_route\",\"duplicate_route\"]'::jsonb, "
                    "'{\"text\":\"probe\"}'::jsonb, 'sha256:probe')"
                ),
                {
                    "asset_id": asset_id,
                    "document_id": document_id,
                    "run_id": run_id,
                },
            )

        refused = self._alembic("upgrade", "head")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("invalid document_unit.semantic_keys", refused.stderr)
        with self.engine.begin() as conn:
            version, plural = conn.execute(
                text(
                    "SELECT v.version_num, u.semantic_keys "
                    "FROM disclosure_ops.alembic_version v "
                    "CROSS JOIN disclosure_core.document_unit u "
                    "WHERE u.asset_id = :asset_id"
                ),
                {"asset_id": asset_id},
            ).one()
            self.assertEqual(version, "0049_app_head_read")
            self.assertEqual(plural, ["duplicate_route", "duplicate_route"])
            conn.execute(
                text(
                    "UPDATE disclosure_core.document_unit "
                    "SET semantic_keys = '[\"duplicate_route\"]'::jsonb "
                    "WHERE asset_id = :asset_id"
                ),
                {"asset_id": asset_id},
            )

        repaired = self._alembic("upgrade", "head")
        self.assertEqual(repaired.returncode, 0, repaired.stderr[-1000:])
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.document_unit "
                    "WHERE asset_id = :asset_id"
                ),
                {"asset_id": asset_id},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.processing_run "
                    "WHERE processing_run_id = :run_id"
                ),
                {"run_id": run_id},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.document "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": document_id},
            )

    def _alembic(self, *args: str) -> subprocess.CompletedProcess[str]:
        # Never build the child environment here: run_alembic pins every
        # database URL variable to this test engine.
        return run_alembic(self.engine, *args)

    def _restore_migration_head(self) -> None:
        restored = self._alembic("upgrade", "head")
        self.assertEqual(restored.returncode, 0, restored.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
