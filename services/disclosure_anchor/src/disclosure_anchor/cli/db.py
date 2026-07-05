"""CLI for database bootstrap (roles, database, schemas, base grants).

Usage:
    python -m disclosure_anchor.cli.db create

``create`` is idempotent. It connects as the admin URL (a superuser on the
``postgres`` database) to create roles and the service database, then connects to
the service database to create schemas and schema-level grants. Migrations
(``make migrate``) create the tables, views and object grants afterwards.
"""

from __future__ import annotations

import sys

from pydantic import ValidationError

from disclosure_anchor.adapters.db.postgres.bootstrap import (
    ensure_database,
    ensure_roles,
    ensure_schemas_and_base_grants,
)
from disclosure_anchor.adapters.db.postgres.connection import (
    admin_database_url,
    create_db_engine,
    migration_database_url,
)
from disclosure_anchor.domain.errors import ConfigurationError
from disclosure_anchor.settings import load_settings


def _create() -> int:
    settings = load_settings()

    admin_engine = create_db_engine(admin_database_url(settings), autocommit=True)
    try:
        ensure_roles(admin_engine)
        ensure_database(admin_engine)
    finally:
        admin_engine.dispose()

    target_engine = create_db_engine(migration_database_url(settings), autocommit=True)
    try:
        ensure_schemas_and_base_grants(target_engine)
    finally:
        target_engine.dispose()

    print("[OK] db-create: roles, database and schemas are present")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "create"

    if command != "create":
        print(f"[FAIL] unknown db command: {command!r}", file=sys.stderr)
        return 2

    try:
        return _create()
    except (ConfigurationError, ValidationError) as exc:
        print(f"[FAIL] db-create: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
