"""Live-DB bootstrap + migration round-trip tests.

Skip cleanly unless ASSET_INTAKE_ADMIN_DATABASE_URL and
ASSET_INTAKE_MIGRATION_DATABASE_URL are set. Every run asserts the disclosure_*
footprint is byte-identical before and after (hard boundary: this service must
never touch sibling objects).
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from sqlalchemy import text

SERVICE_ROOT = Path(__file__).resolve().parents[2]

ADMIN_URL = os.environ.get("ASSET_INTAKE_ADMIN_DATABASE_URL")
MIGRATION_URL = os.environ.get("ASSET_INTAKE_MIGRATION_DATABASE_URL")

DISCLOSURE_SNAPSHOT_SQL = """
    SELECT n.nspname, c.relname, c.relkind
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname LIKE 'disclosure%'
    ORDER BY 1, 2, 3
"""


@unittest.skipUnless(
    ADMIN_URL and MIGRATION_URL,
    "live-DB test: set ASSET_INTAKE_ADMIN_DATABASE_URL and ASSET_INTAKE_MIGRATION_DATABASE_URL",
)
class BootstrapAndMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        from asset_intake.db.connection import create_db_engine

        assert ADMIN_URL is not None
        self.admin_engine = create_db_engine(ADMIN_URL, autocommit=True)
        with self.admin_engine.connect() as conn:
            self.disclosure_before = conn.execute(text(DISCLOSURE_SNAPSHOT_SQL)).fetchall()

    def tearDown(self) -> None:
        with self.admin_engine.connect() as conn:
            disclosure_after = conn.execute(text(DISCLOSURE_SNAPSHOT_SQL)).fetchall()
        self.assertEqual(self.disclosure_before, disclosure_after)
        self.admin_engine.dispose()

    def _alembic(self, *args: str) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = "src"
        result = subprocess.run(
            [".venv/bin/python", "-m", "alembic", *args],
            cwd=SERVICE_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"alembic {args}: {result.stderr}")

    def test_bootstrap_and_migration_round_trip(self) -> None:
        from asset_intake.db.bootstrap import bootstrap_all

        # Bootstrap is idempotent: run twice.
        bootstrap_all(self.admin_engine)
        bootstrap_all(self.admin_engine)

        with self.admin_engine.connect() as conn:
            schemas = {
                r[0]
                for r in conn.execute(
                    text("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'intake%'")
                )
            }
            self.assertEqual(schemas, {"intake_core", "intake_public", "intake_ops"})
            roles = {
                r[0]
                for r in conn.execute(
                    text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'intake%'")
                )
            }
            self.assertEqual(roles, {"intake_owner", "intake_app", "intake_reader"})

        # Migration round trip: up, down, up.
        self._alembic("upgrade", "head")
        self._alembic("downgrade", "base")
        self._alembic("upgrade", "head")

        with self.admin_engine.connect() as conn:
            tables = {
                (r[0], r[1])
                for r in conn.execute(
                    text(
                        "SELECT table_schema, table_name FROM information_schema.tables "
                        "WHERE table_schema LIKE 'intake%' AND table_type = 'BASE TABLE'"
                    )
                )
            }
        self.assertEqual(
            tables,
            {
                ("intake_core", "processing_run"),
                ("intake_core", "source_access"),
                ("intake_core", "data_asset"),
                ("intake_ops", "outbox_event"),
                ("intake_ops", "alembic_version"),
            },
        )

    def test_reader_cannot_touch_core_or_disclosure(self) -> None:
        from asset_intake.db.bootstrap import bootstrap_all

        bootstrap_all(self.admin_engine)
        self._alembic("upgrade", "head")

        with self.admin_engine.connect() as conn:
            conn.execute(text("BEGIN"))
            conn.execute(text("SET LOCAL ROLE intake_reader"))
            for target in ("intake_core.data_asset", "disclosure_core.document"):
                with self.assertRaises(Exception, msg=target):
                    conn.execute(text(f"SELECT 1 FROM {target} LIMIT 1"))
                conn.execute(text("ROLLBACK"))
                conn.execute(text("BEGIN"))
                conn.execute(text("SET LOCAL ROLE intake_reader"))
            conn.execute(text("ROLLBACK"))


if __name__ == "__main__":
    unittest.main()
