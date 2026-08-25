"""Least-privilege checks exercised via SET ROLE.

These run on a superuser (trust) connection but use ``SET ROLE`` to drop to the
target role; PostgreSQL then enforces that role's grants. Everything runs inside
a transaction that is rolled back, so no data persists.
"""

from __future__ import annotations

import unittest

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from disclosure_anchor.adapters.db.postgres.catalog import view_names
from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    FUTURE_L2_READER_ROLE,
    PUBLIC_SCHEMA,
    READER_ROLE,
)
from tests.integration._support import engine_or_skip


class PermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_reader_cannot_read_private_core(self) -> None:
        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text(f'SET ROLE "{READER_ROLE}"'))
                with self.assertRaises(ProgrammingError):
                    conn.execute(text("SELECT * FROM disclosure_core.document"))
            finally:
                trans.rollback()

    def test_reader_cannot_read_private_provider_category(self) -> None:
        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text(f'SET ROLE "{READER_ROLE}"'))
                with self.assertRaises(ProgrammingError):
                    conn.execute(
                        text("SELECT * FROM disclosure_core.provider_category")
                    )
            finally:
                trans.rollback()

    def test_reader_can_read_public_view(self) -> None:
        with self.engine.connect() as catalog_conn:
            public_views = view_names(catalog_conn, schema=PUBLIC_SCHEMA)
        for role in (READER_ROLE, FUTURE_L2_READER_ROLE):
            with self.engine.connect() as conn:
                trans = conn.begin()
                try:
                    conn.execute(text(f'SET ROLE "{role}"'))
                    # Must not raise; results may be empty. Covers views recreated
                    # by 0007 via DROP+CREATE plus the retained source_refs_v1.
                    for view in public_views:
                        conn.execute(
                            text(f"SELECT * FROM disclosure_public.{view} LIMIT 1")
                        ).all()
                finally:
                    trans.rollback()

    def test_reader_cannot_write_public_view(self) -> None:
        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text(f'SET ROLE "{READER_ROLE}"'))
                with self.assertRaises(ProgrammingError):
                    conn.execute(
                        text(
                            "INSERT INTO disclosure_core.company "
                            "(company_id, legal_name) VALUES ('co_denied', 'x')"
                        )
                    )
            finally:
                trans.rollback()

    def test_app_can_write_core(self) -> None:
        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text(f'SET ROLE "{APP_ROLE}"'))
                conn.execute(
                    text(
                        "INSERT INTO disclosure_core.company "
                        "(company_id, legal_name) VALUES ('co_app_probe', 'probe')"
                    )
                )
                count = conn.execute(
                    text(
                        "SELECT count(*) FROM disclosure_core.company "
                        "WHERE company_id = 'co_app_probe'"
                    )
                ).scalar()
                self.assertEqual(count, 1)
            finally:
                trans.rollback()

    def test_app_can_write_ops_outbox(self) -> None:
        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text(f'SET ROLE "{APP_ROLE}"'))
                conn.execute(
                    text(
                        "INSERT INTO disclosure_ops.outbox_event "
                        "(event_id, event_kind, change_kind, subject_kind, subject_ref) "
                        "VALUES ('oe_app_probe', 'probe', 'observed', 'source_access', 'sa_probe')"
                    )
                )
            finally:
                trans.rollback()

    def test_app_can_read_ops_queue_views(self) -> None:
        for view in (
            "pending_parse_v1",
            "pending_build_v1",
            "pending_publish_v1",
            "retryable_failed_run_v1",
            "stale_running_run_v1",
            "unit_build_terminal_v1",
        ):
            with self.engine.connect() as conn:
                trans = conn.begin()
                try:
                    conn.execute(text(f'SET ROLE "{APP_ROLE}"'))
                    conn.execute(text(f"SELECT * FROM disclosure_ops.{view} LIMIT 1")).all()
                finally:
                    trans.rollback()

    def test_app_cannot_modify_alembic_version(self) -> None:
        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text(f'SET ROLE "{APP_ROLE}"'))
                with self.assertRaises(ProgrammingError):
                    conn.execute(
                        text(
                            "UPDATE disclosure_ops.alembic_version "
                            "SET version_num = version_num"
                        )
                    )
            finally:
                trans.rollback()

    def test_health_roles_can_read_migration_head(self) -> None:
        for role in (APP_ROLE, READER_ROLE, FUTURE_L2_READER_ROLE):
            with self.engine.connect() as conn:
                trans = conn.begin()
                try:
                    conn.execute(text(f'SET ROLE "{role}"'))
                    version = conn.execute(
                        text(
                            "SELECT version_num "
                            "FROM disclosure_ops.alembic_version"
                        )
                    ).scalar_one()
                    self.assertTrue(version)
                finally:
                    trans.rollback()


if __name__ == "__main__":
    unittest.main()
