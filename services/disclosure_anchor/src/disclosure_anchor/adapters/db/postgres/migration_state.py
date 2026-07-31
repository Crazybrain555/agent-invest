"""Alembic graph inspection shared by doctor and integration gates."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script.revision import RevisionError
from alembic.script import ScriptDirectory


def _script_directory() -> ScriptDirectory:
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
    return ScriptDirectory.from_config(config)


def migration_heads() -> tuple[str, ...]:
    """Return graph heads using Alembic's parser, independent of the cwd."""

    return tuple(sorted(_script_directory().get_heads()))


def single_migration_head() -> str:
    heads = migration_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one migration head, got {list(heads)}")
    return heads[0]


@lru_cache(maxsize=None)
def migration_ancestry(revision: str) -> frozenset[str]:
    """Return one known revision and all of its graph ancestors.

    Reset safety uses this graph relation to derive schema capabilities.  A
    database revision unknown to this worktree, or one that is not on the
    single supported head lineage, cannot authorize a destructive reset.
    """

    scripts = _script_directory()
    head = single_migration_head()
    try:
        head_ancestry = {
            script.revision
            for script in scripts.iterate_revisions(head, "base")
        }
        ancestry = {
            script.revision
            for script in scripts.iterate_revisions(revision, "base")
        }
    except RevisionError as exc:
        raise RuntimeError(f"unknown Alembic revision {revision!r}") from exc
    if revision not in head_ancestry or not ancestry:
        raise RuntimeError(
            f"Alembic revision {revision!r} is not on supported head {head!r}"
        )
    return frozenset(ancestry)
