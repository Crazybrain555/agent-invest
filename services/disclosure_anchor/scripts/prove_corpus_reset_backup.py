"""Restore a corpus-reset archive into a blank managed scratch and prove it.

The scratch database is lease-bound, created from ``template0``, and never
migrated before restore: the archive itself must recreate every disclosure
schema object without changing catalog objects outside the service scope.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from typing import Any

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from scripts.corpus_reparse_manifest import (  # noqa: E402
    CorpusManifest,
    ManifestError,
    canonical_hash,
    load_manifest,
    validate_code_snapshot,
    validate_reset_bundle_paths,
)
from scripts.corpus_reset_backup import (  # noqa: E402
    BACKUP_EXTENSIONS,
    BACKUP_SCHEMAS,
    backup_bundle_paths,
    build_restore_proof,
    client_version,
    database_security_state_sha256,
    postgres_client_environment,
    restore_proof_path,
    verify_backup_archive,
    verify_backup_bundle,
    write_hashed_json_once,
)
from scripts.corpus_reset_state import (  # noqa: E402
    manifest_postgres_state,
    postgres_state,
)
from scripts.managed_scratch_database import (  # noqa: E402
    ManagedScratchDatabase,
    is_managed_scratch_database,
)
from disclosure_anchor.adapters.db.postgres.connection import (  # noqa: E402
    admin_database_url,
)
from disclosure_anchor.adapters.db.postgres.schema import (  # noqa: E402
    PUBLIC_SCHEMA,
)
from disclosure_anchor.settings import load_settings  # noqa: E402


def pg_restore_command(
    pg_restore: Path,
    *,
    backup: Path,
    database_name: str,
) -> tuple[str, ...]:
    if not database_name or database_name != database_name.strip():
        raise ManifestError("pg_restore requires an exact database name")
    return (
        str(pg_restore),
        f"--dbname={database_name}",
        "--single-transaction",
        "--exit-on-error",
        str(backup),
    )


def _scratch_identity(connection: Connection) -> tuple[str, str]:
    database_name, database_marker = connection.execute(
        text(
            "SELECT current_database(), "
            "shobj_description(oid, 'pg_database') "
            "FROM pg_database WHERE datname = current_database()"
        )
    ).one()
    name = str(database_name)
    marker = "" if database_marker is None else str(database_marker)
    if not is_managed_scratch_database(name, marker):
        raise ManifestError(
            f"refusing unmanaged scratch database identity: {name}"
        )
    return name, marker


def _outside_service_catalog(connection: Connection) -> dict[str, Any]:
    """Inventory user objects that a service-scoped restore must not change."""

    schemas = list(
        connection.execute(
            text(
                """
                SELECT nspname
                  FROM pg_namespace
                 WHERE nspname <> ALL(:service_schemas)
                   AND nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                   AND nspname <> 'information_schema'
                 ORDER BY nspname
                """
            ),
            {"service_schemas": list(BACKUP_SCHEMAS)},
        ).scalars()
    )
    extensions = list(
        connection.execute(
            text("SELECT extname FROM pg_extension ORDER BY extname")
        ).scalars()
    )
    non_extension_relations = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT namespace.nspname AS schema_name,
                       relation.relname AS relation_name,
                       relation.relkind::text AS relation_kind
                  FROM pg_class AS relation
                  JOIN pg_namespace AS namespace
                    ON namespace.oid = relation.relnamespace
                 WHERE namespace.nspname <> ALL(:service_schemas)
                   AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                   AND namespace.nspname <> 'information_schema'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pg_depend AS dependency
                        WHERE dependency.classid = 'pg_class'::regclass
                          AND dependency.objid = relation.oid
                          AND dependency.deptype = 'e'
                   )
                 ORDER BY namespace.nspname, relation.relname, relation.relkind
                """
            ),
            {"service_schemas": list(BACKUP_SCHEMAS)},
        ).mappings()
    ]
    globals_by_kind: dict[str, list[str]] = {}
    for kind, query in (
        ("foreign_server", "SELECT srvname FROM pg_foreign_server ORDER BY srvname"),
        ("event_trigger", "SELECT evtname FROM pg_event_trigger ORDER BY evtname"),
        ("publication", "SELECT pubname FROM pg_publication ORDER BY pubname"),
        ("subscription", "SELECT subname FROM pg_subscription ORDER BY subname"),
    ):
        globals_by_kind[kind] = [
            str(value) for value in connection.execute(text(query)).scalars()
        ]
    return {
        "schemas": [str(value) for value in schemas],
        "extensions": [str(value) for value in extensions],
        "non_extension_relations": non_extension_relations,
        "globals": globals_by_kind,
    }


def _blank_scratch_evidence(
    connection: Connection,
) -> tuple[tuple[str, str], dict[str, Any]]:
    identity = _scratch_identity(connection)
    service_schema_count = int(
        connection.execute(
            text(
                "SELECT count(*) FROM pg_namespace "
                "WHERE nspname = ANY(:schemas)"
            ),
            {"schemas": list(BACKUP_SCHEMAS)},
        ).scalar_one()
    )
    required_extension_count = int(
        connection.execute(
            text(
                "SELECT count(*) FROM pg_extension "
                "WHERE extname = ANY(:extensions)"
            ),
            {"extensions": list(BACKUP_EXTENSIONS)},
        ).scalar_one()
    )
    if service_schema_count or required_extension_count:
        raise ManifestError(
            "managed scratch is not blank before archive restore: "
            f"schemas={service_schema_count}, "
            f"extensions={required_extension_count}"
        )
    return identity, _outside_service_catalog(connection)


def _run_pg_restore(
    *,
    pg_restore: Path,
    backup: Path,
    database_url: str,
) -> None:
    environment = postgres_client_environment(database_url)
    try:
        completed = subprocess.run(
            pg_restore_command(
                pg_restore,
                backup=backup,
                database_name=environment["PGDATABASE"],
            ),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ManifestError(f"cannot execute pg_restore: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-2000:]
        raise ManifestError(
            f"pg_restore failed with exit {completed.returncode}: {detail}"
        )
    if completed.stderr.strip():
        raise ManifestError(
            "pg_restore emitted warnings; refusing restore proof: "
            f"{completed.stderr.strip()[-2000:]}"
        )


def _validate_restored_database(
    connection: Connection,
    *,
    manifest: CorpusManifest,
    expected_scratch_identity: tuple[str, str],
    blank_catalog: dict[str, Any],
    expected_security_state_sha256: str,
) -> dict[str, Any]:
    if _scratch_identity(connection) != expected_scratch_identity:
        raise ManifestError("scratch database identity changed during restore")
    restored_postgres_state = postgres_state(connection)
    expected_postgres_state = manifest_postgres_state(manifest)
    if restored_postgres_state != expected_postgres_state:
        actual_scopes = restored_postgres_state.get("scopes")
        expected_scopes = expected_postgres_state.get("scopes")
        assert isinstance(actual_scopes, dict)
        assert isinstance(expected_scopes, dict)
        changed = {
            scope: {
                field: (
                    actual_scopes[scope].get(field),
                    expected_scopes[scope].get(field),
                )
                for field in (
                    "descriptor_sha256",
                    "state_sha256",
                    "copy_byte_count",
                )
                if actual_scopes[scope].get(field)
                != expected_scopes[scope].get(field)
            }
            for scope in sorted(expected_scopes)
            if actual_scopes.get(scope) != expected_scopes[scope]
        }
        raise ManifestError(
            "restored archive is not the manifest-exact PostgreSQL state; "
            f"changed_scopes={changed}"
        )
    restored_security_state_sha256 = database_security_state_sha256(connection)
    if restored_security_state_sha256 != expected_security_state_sha256:
        raise ManifestError(
            "restored owner/ACL/default-privilege state differs from source"
        )
    restored_views = sorted(
        str(view)
        for view in connection.execute(
            text(
                "SELECT viewname FROM pg_views "
                "WHERE schemaname = :schema ORDER BY viewname"
            ),
            {"schema": PUBLIC_SCHEMA},
        ).scalars()
    )
    if not restored_views:
        raise ManifestError("restored database has no public views")
    for view in restored_views:
        connection.exec_driver_sql(
            f'SELECT * FROM "{PUBLIC_SCHEMA}"."{view}" LIMIT 0'
        )

    invalid_indexes = int(
        connection.execute(
            text(
                """
                SELECT count(*)
                  FROM pg_index AS index
                  JOIN pg_class AS relation
                    ON relation.oid = index.indexrelid
                  JOIN pg_namespace AS namespace
                    ON namespace.oid = relation.relnamespace
                 WHERE namespace.nspname = ANY(:schemas)
                   AND NOT index.indisvalid
                """
            ),
            {"schemas": list(BACKUP_SCHEMAS)},
        ).scalar_one()
    )
    pg_trgm_installed = bool(
        connection.execute(
            text(
                "SELECT EXISTS("
                "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"
                ")"
            )
        ).scalar_one()
    )
    if invalid_indexes:
        raise ManifestError(
            f"restored archive contains {invalid_indexes} invalid indexes"
        )
    if not pg_trgm_installed:
        raise ManifestError("restored archive did not recreate pg_trgm")
    service_schemas = [
        str(value)
        for value in connection.execute(
            text(
                "SELECT nspname FROM pg_namespace "
                "WHERE nspname = ANY(:schemas) ORDER BY nspname"
            ),
            {"schemas": list(BACKUP_SCHEMAS)},
        ).scalars()
    ]
    if service_schemas != sorted(BACKUP_SCHEMAS):
        raise ManifestError(
            "restored service schema inventory mismatch: "
            f"{service_schemas}"
        )
    restored_outside = _outside_service_catalog(connection)
    blank_extensions = set(blank_catalog["extensions"])
    restored_extensions = set(restored_outside["extensions"])
    added_extensions = sorted(restored_extensions - blank_extensions)
    unexpected: list[str] = []
    if blank_extensions - restored_extensions:
        unexpected.append("baseline_extensions_removed")
    for field in ("schemas", "non_extension_relations", "globals"):
        if restored_outside[field] != blank_catalog[field]:
            unexpected.append(field)
    if added_extensions != list(BACKUP_EXTENSIONS):
        unexpected.append("extensions")
    if unexpected:
        raise ManifestError(
            "restored archive changed catalog outside the service scope: "
            f"{unexpected}"
        )
    return {
        "restored_state": "pre_reset_manifest_exact",
        "restored_postgres_state_sha256": canonical_hash(
            restored_postgres_state
        ),
        "restored_security_state_sha256": restored_security_state_sha256,
    }


def prove_backup_restore(
    *,
    manifest: CorpusManifest,
    backup: Path,
    pg_restore: Path,
    scratch_base_url: str,
) -> str:
    verified = verify_backup_archive(
        backup,
        manifest=manifest,
    )
    proof_path = restore_proof_path(backup)
    if proof_path.exists() or proof_path.with_suffix(
        proof_path.suffix + ".sha256"
    ).exists():
        raise ManifestError(
            f"refusing to overwrite immutable restore proof {proof_path}"
        )

    scratch = ManagedScratchDatabase(scratch_base_url)
    try:
        scratch.provision()
        blank_engine = sqlalchemy.create_engine(
            scratch.database_url,
            poolclass=NullPool,
        )
        try:
            with blank_engine.connect() as connection:
                scratch_identity, blank_catalog = _blank_scratch_evidence(
                    connection
                )
        finally:
            blank_engine.dispose()
        _run_pg_restore(
            pg_restore=pg_restore,
            backup=backup,
            database_url=scratch.database_url,
        )
        engine = sqlalchemy.create_engine(
            scratch.database_url,
            poolclass=NullPool,
        )
        try:
            with engine.connect() as connection:
                restored = _validate_restored_database(
                    connection,
                    manifest=manifest,
                    expected_scratch_identity=scratch_identity,
                    blank_catalog=blank_catalog,
                    expected_security_state_sha256=verified.metadata[
                        "source_security_state_sha256"
                    ],
                )
        finally:
            engine.dispose()
        proof = build_restore_proof(
            manifest=manifest,
            backup_sha256=verified.backup_sha256,
            backup_metadata_sha256=verified.metadata_sha256,
            scratch_database=scratch_identity[0],
            scratch_database_marker=scratch_identity[1],
            pg_restore_version=client_version(pg_restore),
            **restored,
        )
        digest = write_hashed_json_once(proof_path, proof)
        verify_backup_bundle(
            backup,
            manifest=manifest,
        )
        return digest
    finally:
        scratch.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prove_corpus_reset_backup",
        description=__doc__,
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--pg-restore", required=True, type=Path)
    args = parser.parse_args(argv)

    settings = load_settings()
    data_root = Path(settings.disclosure_data_root)
    manifest = load_manifest(
        args.manifest,
        data_root=data_root,
        verify_raw_files=False,
    )
    validate_code_snapshot(manifest)
    validate_reset_bundle_paths(
        data_root,
        args.manifest,
        args.manifest.with_suffix(args.manifest.suffix + ".sha256"),
        *backup_bundle_paths(args.backup),
    )
    digest = prove_backup_restore(
        manifest=manifest,
        backup=args.backup,
        pg_restore=args.pg_restore,
        scratch_base_url=admin_database_url(settings),
    )
    print(
        f"[restore-proof] manifest={manifest.sha256} proof={digest} "
        f"-> {restore_proof_path(args.backup)}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ManifestError as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
