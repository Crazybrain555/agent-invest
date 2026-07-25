"""Run PostgreSQL integration tests in one disposable database.

The configured runtime database is used only as a maintenance connection for
creating a pristine sibling database. The unittest child receives only the
scratch URL and temporary filesystem roots, so committed fixtures are never
visible to the resident production worker.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from types import FrameType

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from disclosure_anchor.adapters.db.postgres.bootstrap import (
    ensure_schemas_and_base_grants,
)
from disclosure_anchor.adapters.db.postgres.schema import OWNER_ROLE
from tests.integration._support import (
    TEST_DATABASE_COMMENT_PREFIX,
    TEST_DATABASE_PREFIX,
    is_managed_test_database_identity,
    test_database_created_epoch,
)


_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_LIFECYCLE_LOCK_NS = 815005
_ORPHAN_TTL_SECONDS = 6 * 60 * 60
_SAFE_DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_DATABASE_ENV_KEYS = (
    "DISCLOSURE_TEST_DATABASE_URL",
    "DISCLOSURE_MIGRATION_DATABASE_URL",
    "DATABASE_URL",
    "DISCLOSURE_ADMIN_DATABASE_URL",
    "DISCLOSURE_READER_DATABASE_URL",
)
_MINERU_RUNTIME_ENV_KEYS = (
    "DISCLOSURE_MINERU_BIN",
    "DISCLOSURE_MINERU_BACKEND",
    "DISCLOSURE_MINERU_SERVER_URL",
)


def _quote_identifier(value: str) -> str:
    if _SAFE_DATABASE_NAME.fullmatch(value) is None:
        raise ValueError(f"unsafe scratch database name: {value!r}")
    return f'"{value}"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _database_lease_key(database_name: str) -> int:
    """Return a stable signed bigint key in the integration-test namespace."""

    digest = hashlib.blake2b(
        database_name.encode("utf-8"),
        digest_size=8,
        person=b"disc-itest",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


class ScratchIntegrationDatabase:
    """Provision, lease, migrate, and destroy one integration-test database."""

    def __init__(self, base_url: str, *, real_mineru: bool = False) -> None:
        created_at = int(time.time())
        self.database_name = (
            f"{TEST_DATABASE_PREFIX}{created_at}_{os.getpid()}_{secrets.token_hex(4)}"
        )
        _quote_identifier(self.database_name)
        parsed_url = sqlalchemy.engine.make_url(base_url)
        self.database_url = parsed_url.set(database=self.database_name).render_as_string(
            hide_password=False
        )
        maintenance_url = parsed_url.set(database="postgres").render_as_string(
            hide_password=False
        )
        self._created_at = created_at
        self._admin_engine = sqlalchemy.create_engine(
            maintenance_url,
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
        )
        self._database_lease: Connection | None = None
        self._lease_key = _database_lease_key(self.database_name)
        self._created = False
        self._closed = False
        self._real_mineru = real_mineru
        self._roots = tempfile.TemporaryDirectory(prefix="disclosure-itest-")

    def __enter__(self) -> dict[str, str]:
        try:
            self._provision()
            environment = self._test_environment()
            self._migrate(environment)
            print(
                f"[integration-db] ready {self.database_name} "
                "(production database remains untouched)",
                flush=True,
            )
            return environment
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _provision(self) -> None:
        with self._admin_engine.connect() as admin:
            self._lock_lifecycle(admin)
            try:
                self._reap_orphans(admin)
                admin.exec_driver_sql(
                    f"CREATE DATABASE {_quote_identifier(self.database_name)} "
                    f'OWNER "{OWNER_ROLE}" TEMPLATE template0'
                )
                self._created = True
                # Hold the liveness lease in the maintenance DB. A SIGKILL
                # releases this session lock even if the unittest child keeps
                # a target-DB connection alive.
                self._database_lease = self._admin_engine.connect()
                self._database_lease.execute(
                    text("SELECT pg_advisory_lock(:lease_key)"),
                    {"lease_key": self._lease_key},
                )
                comment = (
                    f"{TEST_DATABASE_COMMENT_PREFIX}{self._created_at}:"
                    f"{self.database_name}"
                )
                admin.exec_driver_sql(
                    f"COMMENT ON DATABASE {_quote_identifier(self.database_name)} "
                    f"IS {_quote_literal(comment)}"
                )
            finally:
                self._unlock_lifecycle(admin)

        schema_engine = sqlalchemy.create_engine(
            self.database_url, isolation_level="AUTOCOMMIT"
        )
        try:
            ensure_schemas_and_base_grants(schema_engine)
        finally:
            schema_engine.dispose()

    def _test_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for key in _DATABASE_ENV_KEYS:
            environment[key] = self.database_url
        root = Path(self._roots.name)
        data_root = root / "services" / "disclosure_anchor"
        shared_root = root / "shared"
        environment.update(
            {
                "DISCLOSURE_DATA_ROOT": str(data_root),
                "DISCLOSURE_SHARED_ROOT": str(shared_root),
                "DISCLOSURE_RUNTIME_ROOT": str(data_root / "runtime"),
                "PYTHONPATH": "src",
            }
        )
        if not self._real_mineru:
            for key in _MINERU_RUNTIME_ENV_KEYS:
                environment.pop(key, None)
            # MinerU has a PATH fallback when its explicit binary is absent.
            # A known-missing scratch path makes accidental parser execution
            # fail locally instead of reaching the resident GPU service.
            environment.update(
                {
                    "DISCLOSURE_MINERU_BIN": str(
                        root / "disabled-mineru" / "mineru"
                    ),
                    "MINERU_MODEL_CACHE": str(
                        shared_root / "model_cache" / "mineru"
                    ),
                    "HF_HOME": str(
                        shared_root / "model_cache" / "huggingface"
                    ),
                    "MODELSCOPE_CACHE": str(
                        shared_root / "model_cache" / "modelscope"
                    ),
                    "WORKER_PARSE_CONCURRENCY": "1",
                }
            )
        return environment

    def _migrate(self, environment: dict[str, str]) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_SERVICE_ROOT,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"scratch database migration failed with exit {completed.returncode}"
            )
        loaded = subprocess.run(
            [sys.executable, "scripts/load_classification_rules.py"],
            cwd=_SERVICE_ROOT,
            env=environment,
            check=False,
        )
        if loaded.returncode != 0:
            raise RuntimeError(
                "scratch classification-rule load failed with exit "
                f"{loaded.returncode}"
            )

    def _reap_orphans(self, admin: Connection) -> None:
        now = int(time.time())
        rows = admin.execute(
            text(
                "SELECT d.datname, shobj_description(d.oid, 'pg_database') "
                "FROM pg_database d "
                "ORDER BY d.datname"
            )
        ).all()
        for database_name, comment in rows:
            name = str(database_name)
            created_epoch = test_database_created_epoch(name)
            if created_epoch is None:
                continue
            marker = None if comment is None else str(comment)
            if marker is not None and not is_managed_test_database_identity(
                name, marker
            ):
                continue
            # An exact marker is published only after the parent owns the
            # lease, so a free lease proves that parent is gone. Unmarked DBs
            # retain a TTL for the tiny CREATE-before-lease crash window.
            if marker is None and now - created_epoch < _ORPHAN_TTL_SECONDS:
                continue
            lease_key = _database_lease_key(name)
            lease_acquired = bool(
                admin.execute(
                    text("SELECT pg_try_advisory_lock(:lease_key)"),
                    {"lease_key": lease_key},
                ).scalar()
            )
            if not lease_acquired:
                print(
                    f"[integration-db] retained active orphan candidate {name}",
                    flush=True,
                )
                continue
            try:
                admin.exec_driver_sql(
                    f"DROP DATABASE {_quote_identifier(name)} WITH (FORCE)"
                )
                print(f"[integration-db] reaped orphan {name}", flush=True)
            finally:
                admin.execute(
                    text("SELECT pg_advisory_unlock(:lease_key)"),
                    {"lease_key": lease_key},
                )

    @staticmethod
    def _lock_lifecycle(admin: Connection) -> None:
        admin.execute(
            text("SELECT pg_advisory_lock(:namespace, 0)"),
            {"namespace": _LIFECYCLE_LOCK_NS},
        )

    @staticmethod
    def _unlock_lifecycle(admin: Connection) -> None:
        admin.execute(
            text("SELECT pg_advisory_unlock(:namespace, 0)"),
            {"namespace": _LIFECYCLE_LOCK_NS},
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._created:
                with self._admin_engine.connect() as admin:
                    self._lock_lifecycle(admin)
                    try:
                        self._release_database_lease()
                        admin.exec_driver_sql(
                            f"DROP DATABASE IF EXISTS "
                            f"{_quote_identifier(self.database_name)} WITH (FORCE)"
                        )
                        print(
                            f"[integration-db] dropped {self.database_name}",
                            flush=True,
                        )
                    finally:
                        self._unlock_lifecycle(admin)
        finally:
            self._release_database_lease()
            self._admin_engine.dispose()
            self._roots.cleanup()

    def _release_database_lease(self) -> None:
        lease = self._database_lease
        self._database_lease = None
        if lease is None:
            return
        try:
            lease.execute(
                text("SELECT pg_advisory_unlock(:lease_key)"),
                {"lease_key": self._lease_key},
            )
        except Exception:
            # NullPool makes close a physical disconnect, which releases the
            # session lock even when the explicit unlock cannot be delivered.
            lease.invalidate()
        finally:
            lease.close()


def _signal_process_group(child: subprocess.Popen[bytes], signum: int) -> None:
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def _run_unittest_child(
    environment: dict[str, str],
    *,
    test_names: Sequence[str],
    verbose: bool,
) -> int:
    command = [sys.executable, "-m", "unittest"]
    if verbose:
        command.append("-v")
    if test_names:
        command.extend(test_names)
    else:
        command.extend(
            ["discover", "-s", "tests/integration", "-t", ".", "-p", "test_*.py"]
        )
    child = subprocess.Popen(
        command,
        cwd=_SERVICE_ROOT,
        env=environment,
        start_new_session=True,
    )
    try:
        return child.wait()
    except BaseException:
        if child.poll() is None:
            # Let unittest unwind the active test first. MinerUProcess catches
            # BaseException and stops its detached CLI/API process groups.
            _signal_process_group(child, signal.SIGINT)
            try:
                child.wait(timeout=65)
            except subprocess.TimeoutExpired:
                _signal_process_group(child, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _signal_process_group(child, signal.SIGKILL)
                    child.wait()
        raise


def _termination_requested(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


def _base_database_url() -> str | None:
    return os.environ.get("DISCLOSURE_MIGRATION_DATABASE_URL") or os.environ.get(
        "DATABASE_URL"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tests", nargs="*", help="optional unittest dotted names")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--real-mineru",
        action="store_true",
        help=(
            "preserve the explicit MinerU runtime/server/cache environment "
            "for GPU-backed smoke tests"
        ),
    )
    args = parser.parse_args(argv)
    base_url = _base_database_url()
    if not base_url:
        print("[skip] no PostgreSQL runtime URL available for scratch provisioning")
        return 0

    previous_sigterm = signal.signal(signal.SIGTERM, _termination_requested)
    try:
        with ScratchIntegrationDatabase(
            base_url, real_mineru=bool(args.real_mineru)
        ) as environment:
            return _run_unittest_child(
                environment,
                test_names=args.tests,
                verbose=bool(args.verbose),
            )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
