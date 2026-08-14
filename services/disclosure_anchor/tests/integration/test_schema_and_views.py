"""Schema/migration shape checks against the migrated database."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
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
from tests.integration._support import engine_or_skip

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
SERVICE_ROOT = Path(__file__).resolve().parents[2]


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
        self.assertEqual(present, 1)
        self.assertEqual(version, single_migration_head())
        self.assertLessEqual(
            {
                "artifact_owner_processing_run_id",
                "parser_target_identity",
                "provider_document_relpath",
                "search_projection_error",
                "semantic_route_receipts_hash",
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
            },
            processing_run_constraints,
        )

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
        self.assertLessEqual({"semantic_keys", "section_keys"}, columns)
        self.assertNotIn("content_categories", columns)

    def test_0037_removes_only_the_unit_content_facet(self) -> None:
        self.addCleanup(self._restore_migration_head)

        with self.engine.connect() as conn:
            unit_columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_public' "
                        "AND table_name = 'document_units_v1'"
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
        self.assertNotIn("content_categories", unit_columns)
        self.assertIn("content_categories", document_columns)

        down = self._alembic("downgrade", "0036_unit_section_routes")
        self.assertEqual(down.returncode, 0, down.stderr[-500:])
        with self.engine.connect() as conn:
            downgraded_unit_columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_public' "
                        "AND table_name = 'document_units_v1'"
                    )
                ).scalars()
            )
        self.assertIn("content_categories", downgraded_unit_columns)

        up = self._alembic("upgrade", "head")
        self.assertEqual(up.returncode, 0, up.stderr[-500:])

    @staticmethod
    def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=SERVICE_ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
        )

    def _restore_migration_head(self) -> None:
        restored = self._alembic("upgrade", "head")
        self.assertEqual(restored.returncode, 0, restored.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
