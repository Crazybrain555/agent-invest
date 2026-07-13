from __future__ import annotations

import unittest

from disclosure_anchor.adapters.db.postgres.migration_state import (
    migration_heads,
    single_migration_head,
)


class MigrationStateTests(unittest.TestCase):
    def test_head_comes_from_alembic_graph(self) -> None:
        heads = migration_heads()
        self.assertEqual(len(heads), 1)
        self.assertEqual(single_migration_head(), heads[0])


if __name__ == "__main__":
    unittest.main()
