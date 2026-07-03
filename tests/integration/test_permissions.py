"""Least-privilege checks exercised via SET ROLE.

These run on a superuser (trust) connection but use ``SET ROLE`` to drop to the
target role; PostgreSQL then enforces that role's grants. Everything runs inside
a transaction that is rolled back, so no data persists.
"""

from __future__ import annotations

import unittest

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    FUTURE_L2_READER_ROLE,
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

    def test_reader_can_read_public_view(self) -> None:
        for role in (READER_ROLE, FUTURE_L2_READER_ROLE):
            with self.engine.connect() as conn:
                trans = conn.begin()
                try:
                    conn.execute(text(f'SET ROLE "{role}"'))
                    # Must not raise; results may be empty. Covers the views that
                    # 0006 recreated via DROP+CREATE, whose grants depend on the
                    # owner-role default privileges from 0001.
                    for view in (
                        "documents_v1",
                        "document_units_v1",
                        "source_refs_v1",
                        "change_events_v1",
                    ):
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
                        "(event_id, event_kind) VALUES ('oe_app_probe', 'probe')"
                    )
                )
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


if __name__ == "__main__":
    unittest.main()
