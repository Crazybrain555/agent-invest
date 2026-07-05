"""Shared helpers for Phase 02 PostgreSQL integration tests.

Integration tests require a migrated local ``disclosure_anchor`` database. When
the database URL is absent or the cluster is unreachable / unmigrated, tests skip
cleanly so the suite stays green without external resources.
"""

from __future__ import annotations

import os
import unittest
from typing import Optional

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


def _database_url() -> Optional[str]:
    return os.environ.get("DISCLOSURE_MIGRATION_DATABASE_URL") or os.environ.get(
        "DATABASE_URL"
    )


def engine_or_skip() -> "Engine":
    """Return an engine to the migrated database, or skip the test."""

    if _IMPORT_ERROR is not None:
        raise unittest.SkipTest(f"sqlalchemy/adapter unavailable: {_IMPORT_ERROR}")

    url = _database_url()
    if not url:
        raise unittest.SkipTest(
            "no DISCLOSURE_MIGRATION_DATABASE_URL/DATABASE_URL configured"
        )

    try:
        engine = create_db_engine(url)
        with engine.connect() as conn:
            migrated = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_name = 'document'"
                ),
                {"s": CORE_SCHEMA},
            ).scalar()
    except Exception as exc:  # connection failed
        raise unittest.SkipTest(f"database not reachable: {exc}")

    if not migrated:
        raise unittest.SkipTest(
            "database is not migrated; run `make db-create migrate` first"
        )
    _acquire_suite_lock(url)
    return engine


# Integration tests write fixed identifiers into the shared local database, so
# two concurrent runs (e.g. the VSCode Testing panel racing a terminal run)
# violate unique constraints against each other's rows. A process-wide session
# advisory lock serializes whole runs instead: the second run waits, it does
# not fail. The lock lives on a dedicated connection and is released when the
# test process exits. 815003 = test-suite namespace (worker uses 815001/815002).
_SUITE_LOCK_NS = 815003
_suite_lock_conn = None


def _acquire_suite_lock(url: str) -> None:
    global _suite_lock_conn
    if _suite_lock_conn is not None:
        return
    engine = create_db_engine(url)
    conn = engine.connect()
    conn.execute(text("SELECT pg_advisory_lock(:ns, 0)"), {"ns": _SUITE_LOCK_NS})
    _suite_lock_conn = conn
