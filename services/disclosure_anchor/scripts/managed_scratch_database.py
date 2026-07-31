"""Lease-bound blank PostgreSQL scratch databases for tests and restore proof."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from disclosure_anchor.adapters.db.postgres.schema import OWNER_ROLE


SCRATCH_DATABASE_PREFIX = "invest_engine_scratch_"
SCRATCH_DATABASE_COMMENT_PREFIX = "disclosure_anchor:managed-scratch:v1:"
SCRATCH_DATABASE_NAME_PATTERN = re.compile(
    rf"^{re.escape(SCRATCH_DATABASE_PREFIX)}"
    r"(?P<created_at>[1-9][0-9]{8,11})_"
    r"(?P<pid>[1-9][0-9]*)_(?P<nonce>[0-9a-f]{8})$"
)
_LIFECYCLE_LOCK_NS = 815005
_ORPHAN_TTL_SECONDS = 6 * 60 * 60
_SAFE_DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def scratch_database_created_epoch(database_name: str) -> int | None:
    match = SCRATCH_DATABASE_NAME_PATTERN.fullmatch(database_name)
    if match is None:
        return None
    return int(match.group("created_at"))


def is_managed_scratch_database(
    database_name: str,
    database_comment: str | None,
) -> bool:
    created_at = scratch_database_created_epoch(database_name)
    if created_at is None:
        return False
    return database_comment == (
        f"{SCRATCH_DATABASE_COMMENT_PREFIX}{created_at}:{database_name}"
    )


def _quote_identifier(value: str) -> str:
    if _SAFE_DATABASE_NAME.fullmatch(value) is None:
        raise ValueError(f"unsafe scratch database name: {value!r}")
    return f'"{value}"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _database_lease_key(database_name: str) -> int:
    digest = hashlib.blake2b(
        database_name.encode("utf-8"),
        digest_size=8,
        person=b"disc-scratch",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


class ManagedScratchDatabase:
    """Create an empty template0 database and always force-drop it on close."""

    def __init__(self, base_url: str) -> None:
        created_at = int(time.time())
        self.database_name = (
            f"{SCRATCH_DATABASE_PREFIX}{created_at}_"
            f"{os.getpid()}_{secrets.token_hex(4)}"
        )
        _quote_identifier(self.database_name)
        parsed_url = sqlalchemy.engine.make_url(base_url)
        self.database_url = parsed_url.set(
            database=self.database_name
        ).render_as_string(hide_password=False)
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

    def provision(self) -> None:
        if self._created:
            raise RuntimeError("scratch database is already provisioned")
        with self._admin_engine.connect() as admin:
            self._lock_lifecycle(admin)
            try:
                self._reap_orphans(admin)
                admin.exec_driver_sql(
                    f"CREATE DATABASE {_quote_identifier(self.database_name)} "
                    f'OWNER "{OWNER_ROLE}" TEMPLATE template0'
                )
                self._created = True
                self._database_lease = self._admin_engine.connect()
                self._database_lease.execute(
                    text("SELECT pg_advisory_lock(:lease_key)"),
                    {"lease_key": self._lease_key},
                )
                comment = (
                    f"{SCRATCH_DATABASE_COMMENT_PREFIX}{self._created_at}:"
                    f"{self.database_name}"
                )
                admin.exec_driver_sql(
                    f"COMMENT ON DATABASE {_quote_identifier(self.database_name)} "
                    f"IS {_quote_literal(comment)}"
                )
            finally:
                self._unlock_lifecycle(admin)

    def _reap_orphans(self, admin: Connection) -> None:
        now = int(time.time())
        rows = admin.execute(
            text(
                "SELECT d.datname, shobj_description(d.oid, 'pg_database') "
                "FROM pg_database d ORDER BY d.datname"
            )
        ).all()
        for database_name, comment in rows:
            name = str(database_name)
            created_epoch = scratch_database_created_epoch(name)
            if created_epoch is None:
                continue
            marker = None if comment is None else str(comment)
            if marker is not None and not is_managed_scratch_database(name, marker):
                continue
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
                continue
            try:
                admin.exec_driver_sql(
                    f"DROP DATABASE {_quote_identifier(name)} WITH (FORCE)"
                )
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
                            "DROP DATABASE IF EXISTS "
                            f"{_quote_identifier(self.database_name)} WITH (FORCE)"
                        )
                    finally:
                        self._unlock_lifecycle(admin)
        finally:
            self._release_database_lease()
            self._admin_engine.dispose()

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
            lease.invalidate()
        finally:
            lease.close()
