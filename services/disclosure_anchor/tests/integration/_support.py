"""Shared helpers for disposable PostgreSQL integration tests."""

from __future__ import annotations

import os
import secrets
import unittest
from typing import Optional

from scripts.managed_scratch_database import (
    SCRATCH_DATABASE_COMMENT_PREFIX,
    SCRATCH_DATABASE_NAME_PATTERN,
    SCRATCH_DATABASE_PREFIX,
    is_managed_scratch_database,
    scratch_database_created_epoch,
)

try:
    from sqlalchemy import text
    from sqlalchemy.engine import Engine

    from disclosure_anchor.adapters.db.postgres.connection import create_db_engine
    from disclosure_anchor.adapters.db.postgres.schema import (
        CORE_SCHEMA,
    )

    _IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover - depends on local environment
    _IMPORT_ERROR = exc


def _configured_test_database_url() -> Optional[str]:
    """Return only the disposable integration-test database URL.

    ``DATABASE_URL`` is the resident worker's production database in the
    normal developer environment. Falling back to it lets committed fixture
    rows enter the real global queues before tearDown can remove them.
    """

    url = os.environ.get("DISCLOSURE_TEST_DATABASE_URL")
    if url:
        return url
    if os.environ.get("DISCLOSURE_MIGRATION_DATABASE_URL") or os.environ.get(
        "DATABASE_URL"
    ):
        raise RuntimeError(
            "refusing integration tests against the configured runtime database; "
            "use `make test-integration` to provision a disposable scratch database"
        )
    return None


TEST_DATABASE_PREFIX = SCRATCH_DATABASE_PREFIX
TEST_DATABASE_COMMENT_PREFIX = SCRATCH_DATABASE_COMMENT_PREFIX
TEST_DATABASE_NAME_PATTERN = SCRATCH_DATABASE_NAME_PATTERN


def test_database_created_epoch(database_name: str) -> int | None:
    """Return the embedded creation epoch for an exact runner database name."""

    return scratch_database_created_epoch(database_name)


def is_managed_test_database_identity(
    database_name: str,
    database_comment: str | None,
) -> bool:
    """Require an exact runner name and its matching versioned DB marker."""

    return is_managed_scratch_database(database_name, database_comment)


def numeric_provider_document_id() -> str:
    """Run-unique ASCII-decimal TEXTID stand-in for CNINFO integration tests."""

    return str(secrets.randbits(120))


def engine_or_skip() -> "Engine":
    """Return an engine to the migrated database, or skip the test."""

    if _IMPORT_ERROR is not None:
        raise unittest.SkipTest(f"sqlalchemy/adapter unavailable: {_IMPORT_ERROR}")

    url = _configured_test_database_url()
    if not url:
        raise unittest.SkipTest(
            "no disposable integration-test database configured"
        )

    engine = None
    try:
        engine = create_db_engine(url)
        with engine.connect() as conn:
            identity = conn.execute(
                text(
                    "SELECT current_database(), "
                    "shobj_description(oid, 'pg_database') "
                    "FROM pg_database WHERE datname = current_database()"
                )
            ).one()
            migrated = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_name = 'document'"
                ),
                {"s": CORE_SCHEMA},
            ).scalar()
    except Exception as exc:  # connection failed
        if engine is not None:
            engine.dispose()
        raise unittest.SkipTest(f"database not reachable: {exc}")

    database_name, database_comment = identity
    if not is_managed_test_database_identity(
        str(database_name),
        None if database_comment is None else str(database_comment),
    ):
        engine.dispose()
        raise RuntimeError(
            "refusing integration test database without the managed scratch "
            f"identity: {database_name}"
        )
    if not migrated:
        engine.dispose()
        raise unittest.SkipTest(
            "database is not migrated; run `make db-create migrate` first"
        )
    return engine
