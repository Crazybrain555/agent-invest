"""Database bootstrap CLI: `python -m asset_intake.cli.db create`.

Runs the idempotent role/schema bootstrap against the shared invest_engine
database using the admin DSN. Migrations run separately via `make migrate`.
"""

from __future__ import annotations

import argparse
import sys

from asset_intake.db.bootstrap import bootstrap_all
from asset_intake.db.connection import create_db_engine, require_url
from asset_intake.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="asset_intake.cli.db")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create", help="ensure intake roles, schemas and base grants")
    args = parser.parse_args(argv)

    if args.command == "create":
        settings = get_settings()
        url = require_url(
            settings.admin_database_url.get_secret_value()
            if settings.admin_database_url
            else None,
            name="ASSET_INTAKE_ADMIN_DATABASE_URL",
        )
        engine = create_db_engine(url, autocommit=True)
        bootstrap_all(engine)
        print("[ok] intake roles, schemas and base grants ensured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
