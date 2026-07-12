"""Schema/migration shape checks against the migrated database."""

from __future__ import annotations

import unittest

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.schema import (
    ALEMBIC_VERSION_TABLE_SCHEMA,
    CORE_SCHEMA,
    OPS_SCHEMA,
    PUBLIC_SCHEMA,
    PUBLIC_VIEWS,
)
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
            rows = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.views "
                    "WHERE table_schema = :s"
                ),
                {"s": PUBLIC_SCHEMA},
            ).scalars()
            views = set(rows)
        self.assertEqual(set(PUBLIC_VIEWS), views)

    def test_alembic_version_in_ops_schema_and_at_head(self) -> None:
        with self.engine.connect() as conn:
            schema = conn.execute(
                text(
                    "SELECT table_schema FROM information_schema.tables "
                    "WHERE table_name = 'alembic_version'"
                )
            ).scalar()
            version = conn.execute(
                text(f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE_SCHEMA}.alembic_version")
            ).scalar()
        self.assertEqual(schema, ALEMBIC_VERSION_TABLE_SCHEMA)
        self.assertEqual(version, "0021_title_topic_classification")

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


if __name__ == "__main__":
    unittest.main()
