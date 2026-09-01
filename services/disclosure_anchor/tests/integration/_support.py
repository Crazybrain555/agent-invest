"""Shared helpers for disposable PostgreSQL integration tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import subprocess
import sys
from typing import Optional
import unittest

from scripts.managed_scratch_database import (
    SCRATCH_DATABASE_COMMENT_PREFIX,
    SCRATCH_DATABASE_NAME_PATTERN,
    SCRATCH_DATABASE_PREFIX,
    is_managed_scratch_database,
    scratch_database_created_epoch,
)

try:
    from sqlalchemy import text
    from sqlalchemy.engine import URL, Engine, make_url

    from disclosure_anchor.adapters.db.postgres.connection import create_db_engine
    from disclosure_anchor.adapters.db.postgres.schema import (
        CORE_SCHEMA,
    )

    _IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover - depends on local environment
    _IMPORT_ERROR = exc


SERVICE_ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_ENV_KEY = "DISCLOSURE_TEST_DATABASE_URL"
# Every database URL variable that integration code, Alembic, or the runtime
# settings can read. The scratch runner pins all of them to one URL; direct
# invocation must prove they agree before touching any database.
DATABASE_ENV_KEYS = (
    TEST_DATABASE_ENV_KEY,
    "DISCLOSURE_MIGRATION_DATABASE_URL",
    "DATABASE_URL",
    "DISCLOSURE_ADMIN_DATABASE_URL",
    "DISCLOSURE_READER_DATABASE_URL",
)
_DEFAULT_POSTGRESQL_PORT = 5432


def pinned_database_environment(
    base_environment: Mapping[str, str],
    database_url: str,
) -> dict[str, str]:
    """Return one child environment with only canonical pinned DB URL keys.

    Environment lookups are case-insensitive in service settings but the
    parent process may be running on a case-sensitive OS.  Delete every
    case-equivalent spelling before appending the canonical keys so inherited
    insertion order cannot redirect a child process after the URL is pinned.
    """

    environment = dict(base_environment)
    for key in tuple(environment):
        if key.upper() in DATABASE_ENV_KEYS:
            environment.pop(key)
    for key in DATABASE_ENV_KEYS:
        environment[key] = database_url
    return environment


def _database_environment_variants() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in DATABASE_ENV_KEYS
    }


def _reject_noncanonical_database_environment_keys() -> None:
    for key in _database_environment_variants():
        if key != key.upper():
            raise RuntimeError(
                f"{key} is a noncanonical database environment variable; "
                "integration tests refuse case-ambiguous database settings"
            )


def _require_sqlalchemy() -> None:
    if _IMPORT_ERROR is not None:
        raise unittest.SkipTest(f"sqlalchemy/adapter unavailable: {_IMPORT_ERROR}")


@dataclass(frozen=True)
class DatabaseDestination:
    """Non-secret identity of the server database a URL connects to."""

    backend: str
    host: str | None
    port: int
    database: str

    def __str__(self) -> str:
        host = self.host if self.host else "<default-host>"
        return f"{self.backend}://{host}:{self.port}/{self.database}"


@dataclass(frozen=True, repr=False)
class DatabaseConnectionIdentity:
    """Parsed connection identity whose credentials are never rendered."""

    destination: DatabaseDestination
    drivername: str
    username: str | None
    password: str | None
    query: tuple[tuple[str, tuple[str, ...]], ...]


def _query_value(url: URL, key: str) -> str | None:
    value = url.query.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return ",".join(value) or None


def _parsed_database_url(
    url_text: str,
    *,
    name: str,
) -> tuple[URL, DatabaseDestination]:
    """Normalize one configured URL to backend/host/effective port/database.

    Credentials, driver spelling, an explicit default port, and connection
    options are not part of the destination. libpq-style ``host``/``port``/
    ``dbname`` query keys win over the URL authority, as the psycopg dialect
    applies them. ``name`` is the environment variable being validated; the URL
    text never enters the error because it may carry a password.
    """

    _require_sqlalchemy()
    try:
        url = make_url(url_text)
        host = _query_value(url, "host") or url.host
        port_text = _query_value(url, "port")
        if port_text:
            port = int(port_text)
        else:
            port = url.port or _DEFAULT_POSTGRESQL_PORT
        database = _query_value(url, "dbname") or url.database
        backend = url.get_backend_name()
    except Exception:
        raise RuntimeError(
            f"{name} is not a parsable database URL; integration tests refuse "
            "to guess its destination"
        ) from None
    if not database:
        raise RuntimeError(
            f"{name} does not name a database; integration tests refuse to "
            "guess its destination"
        )
    destination = DatabaseDestination(
        backend=backend,
        host=host,
        port=port,
        database=database,
    )
    return url, destination


def database_destination(url_text: str, *, name: str) -> DatabaseDestination:
    """Return the non-secret destination selected by one database URL."""

    _, destination = _parsed_database_url(url_text, name=name)
    return destination


def database_connection_identity(
    url_text: str,
    *,
    name: str,
) -> DatabaseConnectionIdentity:
    """Return an exact parsed identity without exposing credentials in errors."""

    url, destination = _parsed_database_url(url_text, name=name)
    normalized_query = tuple(
        sorted(
            (
                key,
                (value,) if isinstance(value, str) else tuple(value),
            )
            for key, value in url.query.items()
        )
    )
    return DatabaseConnectionIdentity(
        destination=destination,
        drivername=url.drivername,
        username=url.username,
        password=url.password,
        query=normalized_query,
    )


def require_single_database_destination(test_url: str) -> DatabaseDestination:
    """Fail closed unless every configured database variable targets the test DB.

    Direct ``unittest`` invocation inherits the developer shell: the engine
    follows ``DISCLOSURE_TEST_DATABASE_URL`` while Alembic and the runtime
    settings follow ``DISCLOSURE_MIGRATION_DATABASE_URL``/``DATABASE_URL``. A
    mixed environment is refused before any connection or subprocess rather
    than letting a migration round trip run against another database.
    The complete parsed connection identity must agree, including credentials,
    driver and connection options.  The managed runner pins this exact URL for
    every role; a direct invocation with a second identity is refused rather
    than letting test validation and Alembic use different authorities.
    """

    _reject_noncanonical_database_environment_keys()
    expected = database_destination(test_url, name=TEST_DATABASE_ENV_KEY)
    expected_identity = database_connection_identity(
        test_url,
        name=TEST_DATABASE_ENV_KEY,
    )
    for key in DATABASE_ENV_KEYS:
        if key == TEST_DATABASE_ENV_KEY:
            continue
        configured = os.environ.get(key)
        if not configured:
            continue
        actual = database_destination(configured, name=key)
        if actual != expected:
            raise RuntimeError(
                f"{key} targets {actual} but {TEST_DATABASE_ENV_KEY} targets "
                f"{expected}; refusing a mixed integration database environment"
            )
        actual_identity = database_connection_identity(configured, name=key)
        if actual_identity != expected_identity:
            raise RuntimeError(
                f"{key} does not exactly match {TEST_DATABASE_ENV_KEY}; "
                "refusing different credentials, driver, or connection options "
                "in the integration database environment"
            )
    return expected


def _configured_test_database_url() -> Optional[str]:
    """Return only the disposable integration-test database URL.

    ``DATABASE_URL`` is the resident worker's production database in the
    normal developer environment. Falling back to it lets committed fixture
    rows enter the real global queues before tearDown can remove them. When
    the test URL is present, every other configured database variable must
    point at the same destination so no code path can reach a second database.
    """

    _reject_noncanonical_database_environment_keys()
    url = os.environ.get(TEST_DATABASE_ENV_KEY)
    if url:
        require_single_database_destination(url)
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


def alembic_subprocess_environment(engine: Engine) -> dict[str, str]:
    """Child environment for ``python -m alembic`` bound to the test engine.

    Every integration database variable is pinned to the engine's own URL, so
    an inherited migration/runtime/admin/reader URL can never redirect a
    downgrade or upgrade away from the database the test is reading. The URL
    keeps the engine's login; Alembic still needs it to be owner-capable and
    fails closed otherwise.
    """

    url = engine.url.render_as_string(hide_password=False)
    environment = pinned_database_environment(os.environ, url)
    environment["PYTHONPATH"] = "src"
    return environment


def run_alembic(engine: Engine, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run Alembic in the service root against the validated test engine only."""

    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=SERVICE_ROOT,
        env=alembic_subprocess_environment(engine),
        capture_output=True,
        text=True,
        check=False,
    )


def engine_or_skip() -> "Engine":
    """Return an engine to the migrated database, or skip the test."""

    _require_sqlalchemy()

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
