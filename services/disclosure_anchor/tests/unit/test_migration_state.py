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
        self.assertEqual(heads[0], "0031_artifact_owner_run")

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


if __name__ == "__main__":
    unittest.main()
