"""Create one immutable manifest-bound PostgreSQL custom-format archive.

The source database is never passed on the process command line.  The tool
holds the worker singleton and a read-only repeatable-read transaction while
``pg_dump`` imports an exported snapshot of the manifest-exact pre-reset state.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import secrets
import subprocess
import sys

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from scripts.corpus_reparse_manifest import (  # noqa: E402
    CorpusManifest,
    ManifestError,
    canonical_hash,
    hash_file,
    load_manifest,
    validate_code_snapshot,
    validate_reset_bundle_paths,
)
from scripts.corpus_reset_backup import (  # noqa: E402
    BACKUP_EXTENSIONS,
    BACKUP_SCHEMAS,
    assert_same_database_identity,
    backup_bundle_paths,
    build_backup_metadata,
    client_version,
    database_security_state_sha256,
    metadata_path,
    postgres_client_environment,
    publish_file_once,
    write_hash_sidecar_once,
    write_hashed_json_once,
)
from scripts.corpus_reset_quiescence import worker_singleton_lock  # noqa: E402
from scripts.corpus_reset_state import (  # noqa: E402
    detect_reset_state,
    manifest_postgres_state,
)
from disclosure_anchor.application.worker.locks import (  # noqa: E402
    exclusive_corpus_mutation,
)
from disclosure_anchor.adapters.db.postgres.connection import (  # noqa: E402
    create_db_engine,
    migration_database_url,
)
from disclosure_anchor.cli.worker import (  # noqa: E402
    assert_worker_singleton_or_cancel,
    worker_database_url,
)
from disclosure_anchor.settings import load_settings  # noqa: E402


def pg_dump_command(
    pg_dump: Path,
    *,
    snapshot: str,
    destination: Path,
) -> tuple[str, ...]:
    command = [
        str(pg_dump),
        "--format=custom",
        f"--file={destination}",
        f"--snapshot={snapshot}",
        "--strict-names",
        "--no-large-objects",
        "--no-publications",
        "--no-subscriptions",
    ]
    command.extend(f"--schema={schema}" for schema in BACKUP_SCHEMAS)
    command.extend(
        f"--extension={extension}" for extension in BACKUP_EXTENSIONS
    )
    return tuple(command)


def _staging_path(backup: Path) -> Path:
    return backup.with_name(
        f".{backup.name}.partial.{os.getpid()}.{secrets.token_hex(8)}"
    )


def _require_new_bundle(backup: Path) -> None:
    existing = [path for path in backup_bundle_paths(backup) if path.exists()]
    if existing:
        raise ManifestError(
            f"refusing backup creation over existing bundle members: {existing}"
        )


def _run_pg_dump(
    *,
    pg_dump: Path,
    database_url: str,
    snapshot: str,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError as exc:
        raise ManifestError(
            f"backup staging file already exists: {destination}"
        ) from exc
    else:
        os.close(descriptor)
    try:
        completed = subprocess.run(
            pg_dump_command(
                pg_dump,
                snapshot=snapshot,
                destination=destination,
            ),
            env=postgres_client_environment(database_url),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ManifestError(f"cannot execute pg_dump: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-2000:]
        raise ManifestError(
            f"pg_dump failed with exit {completed.returncode}: {detail}"
        )
    if completed.stderr.strip():
        raise ManifestError(
            "pg_dump emitted warnings; refusing an unreviewed reset archive: "
            f"{completed.stderr.strip()[-2000:]}"
        )
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise ManifestError("pg_dump produced no custom-format archive")


def create_backup(
    *,
    manifest: CorpusManifest,
    backup: Path,
    database_url: str,
    pg_dump: Path,
    engine: Engine,
    lock_connection: Connection,
) -> str:
    """Create and publish archive, hash sidecar and metadata write-once."""

    _require_new_bundle(backup)
    staging = _staging_path(backup)
    if staging.exists():
        raise ManifestError(f"backup staging path already exists: {staging}")
    try:
        with engine.connect() as connection, connection.begin():
            connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            live_identity = assert_same_database_identity(
                lock_connection,
                connection,
            )
            if detect_reset_state(connection, manifest) != "pre_reset":
                raise ManifestError(
                    "backup source is not the manifest-exact pre-reset state"
                )
            running = int(
                connection.execute(
                    text(
                        "SELECT count(*) FROM disclosure_core.processing_run "
                        "WHERE status = 'running'"
                    )
                ).scalar_one()
            )
            if running:
                raise ManifestError(
                    f"cannot back up while {running} processing runs are running"
                )
            source_state_sha256 = canonical_hash(
                manifest_postgres_state(manifest)
            )
            source_security_state_sha256 = database_security_state_sha256(
                connection
            )
            snapshot = str(
                connection.execute(text("SELECT pg_export_snapshot()")).scalar_one()
            )
            assert_worker_singleton_or_cancel(lock_connection)
            _run_pg_dump(
                pg_dump=pg_dump,
                database_url=database_url,
                snapshot=snapshot,
                destination=staging,
            )
            assert_worker_singleton_or_cancel(lock_connection)
            with engine.connect() as verification_connection:
                assert_same_database_identity(
                    lock_connection,
                    verification_connection,
                )
                if (
                    detect_reset_state(
                        verification_connection,
                        manifest,
                    )
                    != "pre_reset"
                ):
                    raise ManifestError(
                        "live PostgreSQL state changed during pg_dump"
                    )
                if (
                    database_security_state_sha256(verification_connection)
                    != source_security_state_sha256
                ):
                    raise ManifestError(
                        "live owner/ACL state changed during pg_dump"
                    )
            backup_sha256 = hash_file(staging)
            metadata = build_backup_metadata(
                manifest=manifest,
                backup_sha256=backup_sha256,
                pg_dump_version=client_version(pg_dump),
                source_database_identity=live_identity,
                source_postgres_state_sha256=source_state_sha256,
                source_security_state_sha256=source_security_state_sha256,
            )
            publish_file_once(staging, backup)
            write_hash_sidecar_once(backup, backup_sha256)
            write_hashed_json_once(metadata_path(backup), metadata)
            return backup_sha256
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="create_corpus_reset_backup",
        description=__doc__,
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--pg-dump", required=True, type=Path)
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

    lock_database_url = worker_database_url(settings)
    source_database_url = migration_database_url(settings)
    engine = create_db_engine(source_database_url)
    try:
        # The dumped state must not race the nightly GC deleters, which hold
        # this same corpus-mutation lock; acquire it before the worker
        # singleton in the same order as reset_derived_corpus.
        with ExitStack() as stack:
            stack.enter_context(exclusive_corpus_mutation(engine))
            lock_connection = stack.enter_context(
                worker_singleton_lock(lock_database_url)
            )
            assert_worker_singleton_or_cancel(lock_connection)
            digest = create_backup(
                manifest=manifest,
                backup=args.backup,
                database_url=source_database_url,
                pg_dump=args.pg_dump,
                engine=engine,
                lock_connection=lock_connection,
            )
            assert_worker_singleton_or_cancel(lock_connection)
    finally:
        engine.dispose()
    print(
        f"[backup] manifest={manifest.sha256} archive={digest} -> {args.backup}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ManifestError as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
