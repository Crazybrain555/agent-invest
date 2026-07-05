"""Environment isolation helpers for unit tests.

``Settings`` is a pydantic-settings model: any field not passed explicitly is
read from the ambient environment. Tests that assert "no database configured"
behavior must therefore strip the DB-related variables — otherwise they fail
whenever the developer environment (e.g. ``.vscode/test.env``) provides a real
``DATABASE_URL``.
"""

from __future__ import annotations

import os
from unittest import mock

DB_ENV_KEYS = (
    "DATABASE_URL",
    "DISCLOSURE_MIGRATION_DATABASE_URL",
    "DISCLOSURE_ADMIN_DATABASE_URL",
    "DISCLOSURE_READER_DATABASE_URL",
)


def without_db_env() -> mock._patch_dict:
    """Context manager / decorator removing all DB URLs from the environment."""

    preserved = {k: v for k, v in os.environ.items() if k not in DB_ENV_KEYS}
    return mock.patch.dict(os.environ, preserved, clear=True)
