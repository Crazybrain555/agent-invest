"""Shared-database bootstrap boundary regressions."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from disclosure_anchor.adapters.db.postgres.bootstrap import ensure_database
from disclosure_anchor.adapters.db.postgres.schema import SHARED_DATABASE_OWNER_ROLE
from disclosure_anchor.domain.errors import ConfigurationError


class DatabaseBootstrapTests(unittest.TestCase):
    def test_existing_shared_database_is_only_verified(self) -> None:
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.scalar.return_value = (
            SHARED_DATABASE_OWNER_ROLE
        )

        ensure_database(engine)

        connection.execute.assert_called_once()

    def test_missing_or_service_owned_shared_database_fails_closed(self) -> None:
        for owner, expected in (
            (None, "repository level"),
            ("disclosure_owner", "must be owned by repository role"),
            ("disclosure_anchor", "must be owned by repository role"),
        ):
            with self.subTest(owner=owner):
                engine = MagicMock()
                connection = engine.connect.return_value.__enter__.return_value
                connection.execute.return_value.scalar.return_value = owner

                with self.assertRaisesRegex(ConfigurationError, expected):
                    ensure_database(engine)

                connection.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
