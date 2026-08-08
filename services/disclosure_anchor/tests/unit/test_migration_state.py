from __future__ import annotations

import importlib
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
        self.assertEqual(heads[0], "0032_root_heading_path_text")

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

    def test_root_heading_path_migration_is_reversible_without_fake_title(self) -> None:
        migration = importlib.import_module(
            "disclosure_anchor.adapters.db.postgres.migrations.versions."
            "0032_root_heading_path_text"
        )

        upgraded = migration._document_units_view_sql(root_empty_string=True)
        downgraded = migration._document_units_view_sql(root_empty_string=False)

        self.assertIn("COALESCE((SELECT string_agg", upgraded)
        self.assertIn("''::text", upgraded)
        self.assertNotIn("COALESCE((SELECT string_agg", downgraded)
        self.assertIn("u.heading_path", upgraded)
        self.assertIn("u.title", upgraded)


if __name__ == "__main__":
    unittest.main()
