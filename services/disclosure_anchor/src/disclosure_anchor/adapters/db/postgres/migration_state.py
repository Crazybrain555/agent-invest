"""Alembic graph inspection shared by doctor and integration gates."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def migration_heads() -> tuple[str, ...]:
    """Return graph heads using Alembic's parser, independent of the cwd."""

    service_root = Path(__file__).resolve().parents[5]
    config = Config(str(service_root / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(
            service_root
            / "src"
            / "disclosure_anchor"
            / "adapters"
            / "db"
            / "postgres"
            / "migrations"
        ),
    )
    return tuple(sorted(ScriptDirectory.from_config(config).get_heads()))


def single_migration_head() -> str:
    heads = migration_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one migration head, got {list(heads)}")
    return heads[0]
