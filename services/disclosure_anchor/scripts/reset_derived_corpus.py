"""Selective, manifest-bound reset of every reproducible parse generation.

This tool preserves raw PDFs, source registry/provenance, source-level outbox
events, quarantine, audit material, exports, and acquisition checkpoints.  It
resets only processing runs, document units, search projections, run/unit
outbox events, and the three complete parse-derived artifact families.

Database reset is one transaction.  Files are then atomically moved on the
same volume into an audit trash tree.  Rehearsal runs that same transaction
with a rollback; recovery derives progress from the live database and the
live/trash partition instead of an append-only phase log.  Permanent trash
deletion is intentionally separate.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
import sys
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from scripts.corpus_reparse_manifest import (  # noqa: E402
    CorpusManifest,
    ManifestError,
    PathFamily,
    canonical_hash,
    load_manifest,
    safe_data_path,
    validate_code_snapshot,
    validate_reset_bundle_paths,
)
from scripts.corpus_reset_backup import (  # noqa: E402
    BackupBundleVerification,
    RESET_ARTIFACT_FAMILIES,
    assert_same_database_identity,
    backup_bundle_paths,
    build_reset_receipt,
    database_identity,
    database_security_state_sha256,
    fsync_directory,
    hash_sidecar_path,
    recover_hashed_json_sidecar,
    reset_receipt_path,
    validate_artifact_family_state,
    verify_reset_receipt,
    verify_backup_bundle,
    write_reset_receipt_once,
)
from scripts.corpus_reset_quiescence import (  # noqa: E402
    assert_destructive_services_quiescent,
    worker_singleton_lock,
)
from scripts.corpus_reset_state import (  # noqa: E402
    detect_reset_state,
    inspect_reset_state,
    reset_transaction,
)
from disclosure_anchor.adapters.db.postgres.connection import (  # noqa: E402
    create_db_engine,
    migration_database_url,
)
from disclosure_anchor.application.worker.locks import (  # noqa: E402
    exclusive_corpus_mutation,
)
from disclosure_anchor.cli.worker import (  # noqa: E402
    assert_worker_singleton_or_cancel,
    worker_database_url,
)
from disclosure_anchor.settings import load_settings  # noqa: E402


def _assert_backup_source_database(
    connection: Connection,
    backup: BackupBundleVerification,
) -> dict[str, Any]:
    live_identity = database_identity(connection)
    if live_identity != backup.source_database_identity:
        raise ManifestError(
            "live database identity differs from the restore-proven backup "
            f"source: expected={backup.source_database_identity}, "
            f"actual={live_identity}"
        )
    if (
        database_security_state_sha256(connection)
        != backup.metadata["source_security_state_sha256"]
    ):
        raise ManifestError(
            "live owner/ACL state differs from the restore-proven backup source"
        )
    return live_identity


def _artifact_inventory(
    manifest: CorpusManifest,
    data_root: Path,
) -> list[tuple[Path, PathFamily, str]]:
    # First validate every DB-recorded path against the bounded storage
    # contract.  Then move the three complete structural families, not just
    # referenced runs: interrupted/legacy orphan files are parse-derived too,
    # while sibling derived/exports and all source evidence remain outside.
    fields: tuple[tuple[str, PathFamily], ...] = (
        ("parser_artifact_relpath", "parser_artifact"),
        ("normalized_ir_relpath", "normalized_ir"),
        ("document_units_relpath", "document_units"),
    )
    for run in manifest.runs:
        for field, family in fields:
            relpath = run.get(field)
            if relpath is None:
                continue
            if not isinstance(relpath, str) or not relpath:
                raise ManifestError(
                    f"run {run['processing_run_id']} has invalid {field}"
                )
            safe_data_path(
                data_root,
                relpath,
                family=family,
            )
    families = tuple(
        (cast(PathFamily, family), relpath)
        for family, relpath in RESET_ARTIFACT_FAMILIES.items()
    )
    return [
        (
            safe_data_path(data_root, relpath, family=family),
            family,
            relpath,
        )
        for family, relpath in families
    ]


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _stable_file_sha256(path: Path) -> tuple[int, str]:
    """Hash one regular file and reject any identity/content race."""

    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ManifestError(f"artifact family contains special file: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if _stat_identity(opened) != _stat_identity(before):
                raise ManifestError(
                    f"artifact file changed before hashing: {path}"
                )
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
            finished = os.fstat(handle.fileno())
    except OSError as exc:
        raise ManifestError(f"cannot hash artifact file {path}: {exc}") from exc
    try:
        after = path.lstat()
    except OSError as exc:
        raise ManifestError(
            f"artifact file disappeared while hashing: {path}"
        ) from exc
    if (
        _stat_identity(finished) != _stat_identity(before)
        or _stat_identity(after) != _stat_identity(before)
    ):
        raise ManifestError(f"artifact file changed while hashing: {path}")
    return before.st_size, "sha256:" + digest.hexdigest()


def _artifact_family_state(
    inventory: list[tuple[Path, PathFamily, str]],
) -> list[dict[str, Any]]:
    state: list[dict[str, Any]] = []
    for root, family, relpath in inventory:
        if not root.exists():
            state.append(
                {
                    "family": family,
                    "relpath": relpath,
                    "state": "absent",
                    "file_count": 0,
                    "directory_count": 0,
                    "byte_count": 0,
                    "tree_sha256": canonical_hash([]),
                    "entries": [],
                }
            )
            continue
        if root.is_symlink() or not root.is_dir():
            raise ManifestError(
                f"artifact family root is not a safe directory: {root}"
            )
        entries: list[dict[str, Any]] = []
        file_count = 0
        directory_count = 0
        byte_count = 0
        directory_identities: list[
            tuple[Path, tuple[int, int, int, int, int]]
        ] = [(root, _stat_identity(root.lstat()))]
        for path in sorted(
            root.rglob("*"),
            key=lambda candidate: str(candidate.relative_to(root)),
        ):
            if path.is_symlink():
                raise ManifestError(
                    f"artifact family inventory contains symlink: {path}"
                )
            relative = str(path.relative_to(root))
            if path.is_dir():
                directory_identities.append(
                    (path, _stat_identity(path.lstat()))
                )
                directory_count += 1
                entries.append({"kind": "directory", "relpath": relative})
            elif path.is_file():
                size, content_sha256 = _stable_file_sha256(path)
                file_count += 1
                byte_count += size
                entries.append(
                    {
                        "kind": "file",
                        "relpath": relative,
                        "byte_count": size,
                        "content_sha256": content_sha256,
                    }
                )
            else:
                raise ManifestError(
                    f"artifact family contains special file: {path}"
                )
        for path, expected_identity in directory_identities:
            try:
                current_identity = _stat_identity(path.lstat())
            except OSError as exc:
                raise ManifestError(
                    f"artifact directory changed while hashing: {path}"
                ) from exc
            if current_identity != expected_identity:
                raise ManifestError(
                    f"artifact directory changed while hashing: {path}"
                )
        state.append(
            {
                "family": family,
                "relpath": relpath,
                "state": "present",
                "file_count": file_count,
                "directory_count": directory_count,
                "byte_count": byte_count,
                "tree_sha256": canonical_hash(entries),
                "entries": entries,
            }
        )
    normalized = sorted(state, key=lambda item: str(item["family"]))
    validate_artifact_family_state(normalized)
    return normalized


def _trash_artifact_inventory(
    inventory: list[tuple[Path, PathFamily, str]],
    *,
    trash_root: Path,
) -> list[tuple[Path, PathFamily, str]]:
    return [
        (trash_root / "data" / relpath, family, relpath)
        for _source, family, relpath in inventory
    ]


def _artifact_partition_state(
    live_state: list[dict[str, Any]],
    trash_state: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reconstruct the pre-reset tree from an exact live/trash partition."""

    live_by_family = {str(item["family"]): item for item in live_state}
    trash_by_family = {str(item["family"]): item for item in trash_state}
    expected_families = set(RESET_ARTIFACT_FAMILIES)
    if (
        set(live_by_family) != expected_families
        or set(trash_by_family) != expected_families
    ):
        raise ManifestError("artifact live/trash family coverage mismatch")
    reconstructed: list[dict[str, Any]] = []
    for family in sorted(expected_families):
        live = live_by_family[family]
        trash = trash_by_family[family]
        live_present = live.get("state") == "present"
        trash_present = trash.get("state") == "present"
        if live_present and trash_present:
            raise ManifestError(
                f"artifact family exists in both live and reset trash: {family}"
            )
        if live_present:
            reconstructed.append(live)
        elif trash_present:
            reconstructed.append(trash)
        else:
            if live != trash:
                raise ManifestError(
                    f"absent artifact family state is inconsistent: {family}"
                )
            reconstructed.append(live)
    return reconstructed


def _move_artifacts_to_trash(
    inventory: list[tuple[Path, PathFamily, str]],
    *,
    data_root: Path,
    trash_root: Path,
) -> dict[str, int]:
    lexical_service_root = data_root.absolute()
    lexical_trash = trash_root.absolute()
    try:
        trash_relative = lexical_trash.relative_to(lexical_service_root)
    except ValueError as exc:
        raise ManifestError(
            f"reset trash must stay inside service data root: {trash_root}"
        ) from exc
    if (
        len(trash_relative.parts) != 3
        or trash_relative.parts[:2] != ("audit", "reset-trash")
    ):
        raise ManifestError(
            "reset trash must be one manifest directory under "
            f"{data_root / 'audit' / 'reset-trash'}"
        )
    current = lexical_service_root
    for part in trash_relative.parts:
        if current.is_symlink():
            raise ManifestError(
                f"reset-trash root traverses a symlink: {trash_root}"
            )
        current = current / part
    if current.is_symlink():
        raise ManifestError(f"reset-trash root is a symlink: {trash_root}")

    service_root = data_root.resolve()
    dedicated_root = (service_root / "audit" / "reset-trash").resolve()
    resolved_trash = lexical_trash.resolve()
    if resolved_trash.parent != dedicated_root:
        raise ManifestError(
            f"reset trash escaped its dedicated audit root: {trash_root}"
        )

    def destination_for(relpath: str) -> Path:
        relative = Path("data") / relpath
        if relative.is_absolute() or ".." in relative.parts:
            raise ManifestError(f"unsafe reset-trash relpath: {relpath!r}")
        destination = resolved_trash / relative
        current = resolved_trash
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ManifestError(
                    "reset-trash destination traverses a symlink: "
                    f"{destination}"
                )
            if current.exists():
                try:
                    current.resolve().relative_to(resolved_trash)
                except ValueError as exc:
                    raise ManifestError(
                        f"reset-trash destination escapes its root: {destination}"
                    ) from exc
        return destination

    counters = {"moved": 0, "already_moved": 0, "absent": 0}
    for source, family, relpath in inventory:
        destination = destination_for(relpath)
        if source.exists() and destination.exists():
            raise ManifestError(
                f"both source and reset-trash destination exist: {source}"
            )
        if destination.exists():
            if not destination.is_dir():
                raise ManifestError(
                    f"reset-trash structural family is not a directory: {destination}"
                )
            counters["already_moved"] += 1
            continue
        if not source.exists():
            counters["absent"] += 1
            continue
        if not source.is_dir():
            raise ManifestError(
                f"live structural artifact family is not a directory: {source}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        current = service_root
        for part in destination.parent.relative_to(service_root).parts:
            fsync_directory(current)
            current /= part
        # mkdir follows existing components; validate again before rename so a
        # pre-created ``data``/family symlink cannot redirect parse artifacts
        # into raw storage or an arbitrary external directory.
        destination = destination_for(relpath)
        try:
            source.rename(destination)
        except OSError as exc:
            raise ManifestError(
                f"cannot move {family} artifact {source} to reset trash: {exc}"
            ) from exc
        fsync_directory(source.parent)
        if destination.parent != source.parent:
            fsync_directory(destination.parent)
        counters["moved"] += 1
    return counters


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reset_derived_corpus", description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rehearse", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings()
    data_root = Path(settings.disclosure_data_root)
    validate_reset_bundle_paths(
        data_root,
        args.manifest,
        args.manifest.with_suffix(args.manifest.suffix + ".sha256"),
        *backup_bundle_paths(args.backup),
    )
    manifest = load_manifest(
        args.manifest,
        data_root=data_root,
        # Only the destructive pass re-hashes every raw PDF in the corpus.
        # Rehearsal deletes nothing, so a full-corpus sha256 sweep there buys
        # no safety and costs a whole pass over the raw archive.
        verify_raw_files=args.apply,
    )
    validate_code_snapshot(manifest)
    backup_verification = verify_backup_bundle(
        args.backup,
        manifest=manifest,
    )
    inventory = _artifact_inventory(manifest, data_root)

    lock_database_url = worker_database_url(settings)
    engine = create_db_engine(migration_database_url(settings))
    try:
        with ExitStack() as mutation_stack:
            if args.apply or args.rehearse:
                mutation_stack.enter_context(exclusive_corpus_mutation(engine))
            lock_connection = mutation_stack.enter_context(
                worker_singleton_lock(lock_database_url)
            )
            return _run_locked_reset(
                args=args,
                data_root=data_root,
                manifest=manifest,
                backup_verification=backup_verification,
                inventory=inventory,
                engine=engine,
                lock_connection=lock_connection,
            )
    finally:
        engine.dispose()


def _run_locked_reset(
    *,
    args: argparse.Namespace,
    data_root: Path,
    manifest: CorpusManifest,
    backup_verification: BackupBundleVerification,
    inventory: list[tuple[Path, PathFamily, str]],
    engine: Engine,
    lock_connection: Connection,
) -> int:
    try:
        assert_destructive_services_quiescent()
        assert_worker_singleton_or_cancel(lock_connection)
        with engine.connect() as connection:
            live_identity = assert_same_database_identity(
                lock_connection,
                connection,
            )
            _assert_backup_source_database(
                connection,
                backup_verification,
            )
            state = detect_reset_state(connection, manifest)
        trash_root = (
            data_root
            / "audit"
            / "reset-trash"
            / manifest.sha256.removeprefix("sha256:")
        )
        live_artifact_state = _artifact_family_state(inventory)
        trash_artifact_state = _artifact_family_state(
            _trash_artifact_inventory(
                inventory,
                trash_root=trash_root,
            )
        )

        if args.rehearse:
            if state != "pre_reset":
                raise ManifestError("rollback rehearsal requires pre-reset state")
            if any(
                item["state"] == "present"
                for item in trash_artifact_state
            ):
                raise ManifestError(
                    "rollback rehearsal found parse artifacts in reset trash"
                )
            with engine.connect() as connection:
                zero_counts = reset_transaction(
                    connection,
                    manifest,
                    commit=False,
                )
            with engine.connect() as connection:
                assert_same_database_identity(lock_connection, connection)
                if detect_reset_state(connection, manifest) != "pre_reset":
                    raise ManifestError(
                        "database did not return to pre-reset state after rollback"
                    )
            if _artifact_family_state(inventory) != live_artifact_state:
                raise ManifestError(
                    "parse-derived file set changed during rollback rehearsal"
                )
            assert_worker_singleton_or_cancel(lock_connection)
            live_summary = {
                str(item["family"]): (
                    item["state"],
                    item["file_count"],
                    item["byte_count"],
                    item["tree_sha256"],
                )
                for item in live_artifact_state
            }
            print(
                f"[rehearsal] rollback verified for {manifest.sha256}: "
                f"zero_state={zero_counts} artifacts={live_summary}"
            )
            return 0

        receipt_file = reset_receipt_path(args.backup)
        receipt_sidecar = hash_sidecar_path(receipt_file)
        if receipt_file.exists() and not receipt_sidecar.exists():
            recover_hashed_json_sidecar(receipt_file)
        if receipt_file.exists() or receipt_sidecar.exists():
            if state != "post_reset":
                raise ManifestError(
                    "reset receipt no longer matches database zero state"
                )
            receipt = verify_reset_receipt(
                args.backup,
                manifest=manifest,
            )
            with engine.connect() as connection:
                live_identity = _assert_backup_source_database(
                    connection,
                    receipt.backup,
                )
                if live_identity != receipt.receipt["database_identity"]:
                    raise ManifestError(
                        "completed reset receipt no longer identifies this database"
                    )
            if any(
                item["state"] != "absent"
                for item in live_artifact_state
            ):
                raise ManifestError(
                    "completed reset has a live derived artifact family again"
                )
            if trash_artifact_state != receipt.receipt["artifact_family_state"]:
                raise ManifestError(
                    "completed reset trash inventory differs from its receipt"
                )
            assert_worker_singleton_or_cancel(lock_connection)
            print(
                "[skip] reset completion revalidated for "
                f"{manifest.sha256} receipt={receipt.receipt_sha256}"
            )
            return 0
        if state == "pre_reset":
            if any(
                item["state"] == "present"
                for item in trash_artifact_state
            ):
                raise ManifestError(
                    "pre-reset database state cannot have moved artifact "
                    "families in reset trash"
                )
        source_artifact_state = _artifact_partition_state(
            live_artifact_state,
            trash_artifact_state,
        )

        committed_zero_counts: dict[str, int] | None = None
        if state == "pre_reset":
            assert_worker_singleton_or_cancel(lock_connection)
            with engine.connect() as connection:
                committed_zero_counts = reset_transaction(
                    connection,
                    manifest,
                    commit=True,
                )
            assert_worker_singleton_or_cancel(lock_connection)

        counters = _move_artifacts_to_trash(
            inventory,
            data_root=data_root,
            trash_root=trash_root,
        )
        live_artifact_state = _artifact_family_state(inventory)
        if any(
            item["state"] != "absent" for item in live_artifact_state
        ):
            raise ManifestError(
                "derived artifact family remained live after reset move"
            )
        trash_artifact_state = _artifact_family_state(
            _trash_artifact_inventory(
                inventory,
                trash_root=trash_root,
            )
        )
        if trash_artifact_state != source_artifact_state:
            raise ManifestError(
                "reset trash inventory differs from the pre-reset source"
            )
        assert_worker_singleton_or_cancel(lock_connection)
        with engine.connect() as connection:
            live_identity = assert_same_database_identity(
                lock_connection,
                connection,
            )
            post_state, live_postgres_state, zero_state = inspect_reset_state(
                connection,
                manifest,
            )
            if post_state != "post_reset":
                raise ManifestError("database zero-state validation failed")
            live_identity = _assert_backup_source_database(
                connection,
                backup_verification,
            )
            boundary_value = connection.execute(
                text("SELECT clock_timestamp()")
            ).scalar_one()
        if not isinstance(boundary_value, datetime):
            raise ManifestError(
                "database clock_timestamp() did not return a timestamp"
            )
        if boundary_value.tzinfo is None:
            raise ManifestError(
                "database reset boundary timestamp lacks a timezone"
            )
        if (
            committed_zero_counts is not None
            and zero_state != committed_zero_counts
        ):
            raise ManifestError(
                "post-reset zero state differs from the committed reset "
                "transaction"
            )
        reset_boundary_at = boundary_value.astimezone(
            timezone.utc
        ).isoformat()
        receipt_payload = build_reset_receipt(
            manifest=manifest,
            backup=backup_verification,
            live_database_identity=live_identity,
            reset_boundary_at=reset_boundary_at,
            zero_state=zero_state,
            post_reset_postgres_state=live_postgres_state,
            trash_root=str(trash_root),
            artifact_family_state=trash_artifact_state,
        )
        receipt_sha256 = write_reset_receipt_once(
            args.backup,
            receipt_payload,
        )
        assert_worker_singleton_or_cancel(lock_connection)
        print(
            f"[complete] manifest={manifest.sha256} db=zero "
            f"artifacts={counters} trash={trash_root} "
            f"receipt={receipt_sha256}"
        )
        return 0
    except OSError as exc:
        raise ManifestError(
            f"reset filesystem operation failed: {exc}"
        ) from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ManifestError as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
