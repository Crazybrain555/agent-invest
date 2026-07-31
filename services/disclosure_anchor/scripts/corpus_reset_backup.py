"""Manifest-bound PostgreSQL archive and scratch-restore proof helpers.

The archive is deliberately narrower than the shared ``invest_engine``
database: it contains only this service's three schemas plus the ``pg_trgm``
extension they depend on.  A backup is not allowed to authorize a reset until
the exact archive has restored successfully into a blank, runner-managed
scratch database and that restored database matches the frozen pre-reset
manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import Connection, URL, make_url

from scripts.corpus_reparse_manifest import (
    CorpusManifest,
    ManifestError,
    canonical_hash,
    hash_file,
    write_once_durable,
)
from scripts.corpus_reset_state import (
    assert_post_reset_postgres_state,
    manifest_postgres_state,
)
from scripts.corpus_reset_digest import (
    RESET_ZERO_STATE_KEYS as _RESET_ZERO_STATE_KEYS,
)
from disclosure_anchor.adapters.db.postgres.schema import (
    CORE_SCHEMA,
    OPS_SCHEMA,
    PUBLIC_SCHEMA,
)
from scripts.managed_scratch_database import is_managed_scratch_database


BACKUP_METADATA_SCHEMA = "corpus-reset-backup-metadata.v4"
RESTORE_PROOF_SCHEMA = "corpus-reset-restore-proof.v4"
RESET_RECEIPT_SCHEMA = "corpus-reset-receipt.v4"
BACKUP_SCHEMAS = (CORE_SCHEMA, OPS_SCHEMA, PUBLIC_SCHEMA)
BACKUP_EXTENSIONS = ("pg_trgm",)
RESET_ARTIFACT_FAMILIES = {
    "parser_artifact": "parser_artifacts",
    "normalized_ir": "derived/normalized_ir",
    "document_units": "derived/document_unit_snapshots",
}
RESET_ZERO_STATE_KEYS = _RESET_ZERO_STATE_KEYS
_HASH_PREFIX_LENGTH = len("sha256:") + 64


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _HASH_PREFIX_LENGTH
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


@dataclass(frozen=True)
class ArchiveVerification:
    """Validated immutable archive bytes and metadata identity."""

    backup_sha256: str
    metadata: dict[str, Any]
    metadata_sha256: str


@dataclass(frozen=True)
class BackupBundleVerification:
    """Structured identity of a restore-proven backup bundle."""

    backup_sha256: str
    metadata: dict[str, Any]
    metadata_sha256: str
    proof: dict[str, Any]
    proof_sha256: str
    source_database_identity: dict[str, Any]


@dataclass(frozen=True)
class ResetReceiptVerification:
    """Validated post-reset receipt and its immutable hash."""

    receipt: dict[str, Any]
    receipt_sha256: str
    backup: BackupBundleVerification


def metadata_path(backup: Path) -> Path:
    return backup.with_suffix(backup.suffix + ".metadata.json")


def restore_proof_path(backup: Path) -> Path:
    return backup.with_suffix(backup.suffix + ".restore-proof.json")


def reset_receipt_path(backup: Path) -> Path:
    return backup.with_suffix(backup.suffix + ".reset-receipt.json")


def hash_sidecar_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".sha256")


def backup_bundle_paths(backup: Path) -> tuple[Path, ...]:
    """Return every immutable member created for one PostgreSQL archive."""

    metadata = metadata_path(backup)
    proof = restore_proof_path(backup)
    receipt = reset_receipt_path(backup)
    return (
        backup,
        hash_sidecar_path(backup),
        metadata,
        hash_sidecar_path(metadata),
        proof,
        hash_sidecar_path(proof),
        receipt,
        hash_sidecar_path(receipt),
    )


def fsync_directory(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_hash_sidecar_once(path: Path, digest: str) -> None:
    if not _is_hash(digest):
        raise ManifestError(f"invalid sha256 digest for {path}: {digest!r}")
    write_bytes_atomically_once(
        hash_sidecar_path(path),
        (digest + "\n").encode("ascii"),
    )


def publish_file_once(source: Path, destination: Path) -> None:
    """Publish one fsynced same-directory file without an overwrite race."""

    if source.parent.resolve() != destination.parent.resolve():
        raise ManifestError(
            "immutable archive staging file must share the destination directory"
        )
    if not source.is_file() or source.is_symlink():
        raise ManifestError(f"archive staging path is not a regular file: {source}")
    try:
        with source.open("rb") as handle:
            os.fsync(handle.fileno())
        os.link(source, destination)
        fsync_directory(destination.parent)
    except FileExistsError as exc:
        raise ManifestError(
            f"refusing to overwrite immutable archive {destination}"
        ) from exc
    except OSError as exc:
        raise ManifestError(
            f"cannot publish immutable archive {destination}: {exc}"
        ) from exc
    finally:
        try:
            source.unlink()
        except FileNotFoundError:
            pass


def write_bytes_atomically_once(path: Path, payload: bytes) -> None:
    """Publish complete bytes write-once; a crash leaves only hidden staging."""

    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(
        f".{path.name}.partial.{os.getpid()}.{secrets.token_hex(8)}"
    )
    try:
        write_once_durable(staging, payload)
        publish_file_once(staging, path)
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass


def write_hashed_json_once(path: Path, payload: dict[str, Any]) -> str:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    sidecar = hash_sidecar_path(path)
    if path.exists() or sidecar.exists():
        raise ManifestError(f"refusing to overwrite proof or sidecar: {path}")
    write_bytes_atomically_once(path, encoded)
    write_bytes_atomically_once(sidecar, (digest + "\n").encode("ascii"))
    return digest


def load_hashed_json(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = path.read_bytes()
        expected = hash_sidecar_path(path).read_text(encoding="ascii").strip()
    except OSError as exc:
        raise ManifestError(f"cannot read proof bundle file {path}: {exc}") from exc
    actual = "sha256:" + hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ManifestError(
            f"proof bundle hash mismatch for {path}: expected {expected}, got {actual}"
        )
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid proof JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"proof JSON is not an object: {path}")
    return value, actual


def recover_hashed_json_sidecar(path: Path) -> str:
    """Recover only a missing sidecar for a complete canonical JSON record.

    The JSON file is written and fsynced before its hash sidecar.  A power loss
    in that narrow window must not strand a completed destructive reset, but a
    partial, non-canonical, symlinked, or already-contradictory record must
    still fail closed.
    """

    sidecar = hash_sidecar_path(path)
    if sidecar.exists():
        _value, digest = load_hashed_json(path)
        return digest
    if not path.is_file() or path.is_symlink():
        raise ManifestError(
            f"cannot recover hash sidecar for missing or unsafe JSON {path}"
        )
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(
            f"cannot recover hash sidecar for incomplete JSON {path}"
        ) from exc
    if not isinstance(value, dict):
        raise ManifestError(f"proof JSON is not an object: {path}")
    canonical = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if payload != canonical:
        raise ManifestError(f"refusing sidecar recovery for non-canonical JSON {path}")
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    write_bytes_atomically_once(sidecar, (digest + "\n").encode("ascii"))
    return digest


def _postgres_url(database_url: str) -> URL:
    try:
        parsed = make_url(database_url)
    except Exception as exc:
        raise ManifestError("database URL is not a valid SQLAlchemy URL") from exc
    if parsed.get_backend_name() != "postgresql":
        raise ManifestError("PostgreSQL backup requires a PostgreSQL database URL")
    return parsed


def postgres_client_environment(database_url: str) -> dict[str, str]:
    """Pass exact libpq connection parameters via env, never process argv.

    ``PGDATABASE`` is a database-name parameter, not a reliable recursive
    conninfo carrier across every PostgreSQL client build.  Split the
    SQLAlchemy URL explicitly so Unix-socket ``host`` query parameters and
    credentials cannot silently fall back to ``/tmp:5432``.
    """

    parsed = _postgres_url(database_url)
    environment = os.environ.copy()
    for key in (
        "PGDATABASE",
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGPASSWORD",
        "PGSSLMODE",
        "PGCONNECT_TIMEOUT",
        "PGAPPNAME",
        "PGOPTIONS",
        "PGSSLROOTCERT",
        "PGSSLCERT",
        "PGSSLKEY",
    ):
        environment.pop(key, None)
    database = parsed.database
    if not database:
        raise ManifestError("PostgreSQL backup URL lacks a database name")
    environment["PGDATABASE"] = database
    query = parsed.query
    host = query.get("host") or parsed.host
    port = query.get("port") or parsed.port
    if host:
        environment["PGHOST"] = str(host)
    if port:
        environment["PGPORT"] = str(port)
    if parsed.username:
        environment["PGUSER"] = parsed.username
    if parsed.password:
        environment["PGPASSWORD"] = parsed.password
    query_environment = {
        "sslmode": "PGSSLMODE",
        "connect_timeout": "PGCONNECT_TIMEOUT",
        "application_name": "PGAPPNAME",
        "options": "PGOPTIONS",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
    }
    for query_key, environment_key in query_environment.items():
        value = query.get(query_key)
        if value:
            environment[environment_key] = str(value)
    return environment


def database_identity(connection: Connection) -> dict[str, Any]:
    """Read the live PostgreSQL cluster/database identity explicitly."""

    row = (
        connection.execute(
            text(
                """
            SELECT current_database() AS database_name,
                   database.oid AS database_oid,
                   control.system_identifier::text AS system_identifier,
                   current_setting('server_version_num')::integer
                     AS server_version_num
              FROM pg_database AS database
              CROSS JOIN pg_control_system() AS control
             WHERE database.datname = current_database()
            """
            )
        )
        .mappings()
        .one()
    )
    return _validate_database_identity(
        {
            "database_name": str(row["database_name"]),
            "database_oid": int(row["database_oid"]),
            "system_identifier": str(row["system_identifier"]),
            "server_version_num": int(row["server_version_num"]),
        },
        label="live",
    )


def assert_same_database_identity(
    lock_connection: Connection,
    operation_connection: Connection,
) -> dict[str, Any]:
    """Prove the worker-lock and maintenance sessions share one lock domain."""

    lock_identity = database_identity(lock_connection)
    operation_identity = database_identity(operation_connection)
    if lock_identity != operation_identity:
        raise ManifestError(
            "worker singleton lock and maintenance operation target different "
            f"PostgreSQL databases: lock={lock_identity}, "
            f"operation={operation_identity}"
        )
    return operation_identity


def database_security_state_sha256(connection: Connection) -> str:
    """Hash owner and explicit ACL state for every service-schema object."""

    schemas = [
        {
            "schema_name": str(row["schema_name"]),
            "owner": str(row["owner"]),
            "acl": list(row["acl"]),
        }
        for row in connection.execute(
            text(
                """
                SELECT namespace.nspname AS schema_name,
                       owner.rolname AS owner,
                       COALESCE(
                           ARRAY(
                               SELECT item::text
                                 FROM unnest(namespace.nspacl) AS item
                                ORDER BY item::text
                           ),
                           ARRAY[]::text[]
                       ) AS acl
                  FROM pg_namespace AS namespace
                  JOIN pg_roles AS owner
                    ON owner.oid = namespace.nspowner
                 WHERE namespace.nspname = ANY(:schemas)
                 ORDER BY namespace.nspname
                """
            ),
            {"schemas": list(BACKUP_SCHEMAS)},
        ).mappings()
    ]
    relations = [
        {
            "schema_name": str(row["schema_name"]),
            "relation_name": str(row["relation_name"]),
            "relation_kind": str(row["relation_kind"]),
            "owner": str(row["owner"]),
            "acl": list(row["acl"]),
        }
        for row in connection.execute(
            text(
                """
                SELECT namespace.nspname AS schema_name,
                       relation.relname AS relation_name,
                       relation.relkind::text AS relation_kind,
                       owner.rolname AS owner,
                       COALESCE(
                           ARRAY(
                               SELECT item::text
                                 FROM unnest(relation.relacl) AS item
                                ORDER BY item::text
                           ),
                           ARRAY[]::text[]
                       ) AS acl
                  FROM pg_class AS relation
                  JOIN pg_namespace AS namespace
                    ON namespace.oid = relation.relnamespace
                  JOIN pg_roles AS owner
                    ON owner.oid = relation.relowner
                 WHERE namespace.nspname = ANY(:schemas)
                 ORDER BY namespace.nspname, relation.relname,
                          relation.relkind
                """
            ),
            {"schemas": list(BACKUP_SCHEMAS)},
        ).mappings()
    ]
    routines = [
        {
            "schema_name": str(row["schema_name"]),
            "routine_name": str(row["routine_name"]),
            "identity_arguments": str(row["identity_arguments"]),
            "routine_kind": str(row["routine_kind"]),
            "owner": str(row["owner"]),
            "acl": list(row["acl"]),
        }
        for row in connection.execute(
            text(
                """
                SELECT namespace.nspname AS schema_name,
                       routine.proname AS routine_name,
                       pg_get_function_identity_arguments(routine.oid)
                         AS identity_arguments,
                       routine.prokind::text AS routine_kind,
                       owner.rolname AS owner,
                       COALESCE(
                           ARRAY(
                               SELECT item::text
                                 FROM unnest(routine.proacl) AS item
                                ORDER BY item::text
                           ),
                           ARRAY[]::text[]
                       ) AS acl
                  FROM pg_proc AS routine
                  JOIN pg_namespace AS namespace
                    ON namespace.oid = routine.pronamespace
                  JOIN pg_roles AS owner
                    ON owner.oid = routine.proowner
                 WHERE namespace.nspname = ANY(:schemas)
                 ORDER BY namespace.nspname, routine.proname,
                          pg_get_function_identity_arguments(routine.oid),
                          routine.prokind
                """
            ),
            {"schemas": list(BACKUP_SCHEMAS)},
        ).mappings()
    ]
    default_privileges = [
        {
            "role": str(row["role"]),
            "schema_name": str(row["schema_name"]),
            "object_type": str(row["object_type"]),
            "acl": list(row["acl"]),
        }
        for row in connection.execute(
            text(
                """
                SELECT role.rolname AS role,
                       namespace.nspname AS schema_name,
                       defaults.defaclobjtype::text AS object_type,
                       ARRAY(
                           SELECT item::text
                             FROM unnest(defaults.defaclacl) AS item
                            ORDER BY item::text
                       ) AS acl
                  FROM pg_default_acl AS defaults
                  JOIN pg_roles AS role
                    ON role.oid = defaults.defaclrole
                  JOIN pg_namespace AS namespace
                    ON namespace.oid = defaults.defaclnamespace
                 WHERE namespace.nspname = ANY(:schemas)
                 ORDER BY role.rolname, namespace.nspname,
                          defaults.defaclobjtype
                """
            ),
            {"schemas": list(BACKUP_SCHEMAS)},
        ).mappings()
    ]
    if [record["schema_name"] for record in schemas] != sorted(BACKUP_SCHEMAS):
        raise ManifestError("backup security state schema coverage mismatch")
    return canonical_hash(
        {
            "schemas": schemas,
            "relations": relations,
            "routines": routines,
            "default_privileges": default_privileges,
        }
    )


def client_version(binary: Path) -> str:
    try:
        completed = subprocess.run(
            (str(binary), "--version"),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ManifestError(f"cannot execute PostgreSQL client {binary.name}") from exc
    version = completed.stdout.strip()
    if not version:
        raise ManifestError(f"PostgreSQL client returned no version: {binary}")
    return version


def validate_artifact_family_state(
    value: object,
) -> list[dict[str, Any]]:
    """Validate the complete, ordered file set for every reset family."""

    if not isinstance(value, list):
        raise ManifestError("reset evidence lacks artifact family state")
    fields = {
        "family",
        "relpath",
        "state",
        "file_count",
        "directory_count",
        "byte_count",
        "tree_sha256",
        "entries",
    }
    for record in value:
        if not isinstance(record, dict) or set(record) != fields:
            raise ManifestError("reset evidence has invalid artifact family record")
        family = record.get("family")
        entries = record.get("entries")
        counters = (
            record.get("file_count"),
            record.get("directory_count"),
            record.get("byte_count"),
        )
        if (
            not isinstance(family, str)
            or family not in RESET_ARTIFACT_FAMILIES
            or record.get("relpath") != RESET_ARTIFACT_FAMILIES[family]
            or record.get("state") not in {"present", "absent"}
            or any(
                not isinstance(count, int) or isinstance(count, bool) or count < 0
                for count in counters
            )
            or not _is_hash(record.get("tree_sha256"))
            or not isinstance(entries, list)
            or (
                record.get("state") == "absent"
                and (
                    any(cast(int, count) != 0 for count in counters)
                    or entries
                )
            )
        ):
            raise ManifestError("reset evidence has invalid artifact family state")
        normalized_entries: list[dict[str, Any]] = []
        seen_relpaths: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ManifestError("reset evidence has invalid artifact entry")
            kind = entry.get("kind")
            relpath = entry.get("relpath")
            relative = Path(relpath) if isinstance(relpath, str) else Path()
            if (
                kind not in {"directory", "file"}
                or not isinstance(relpath, str)
                or not relpath
                or relative.is_absolute()
                or ".." in relative.parts
                or relpath in seen_relpaths
            ):
                raise ManifestError("reset evidence has unsafe artifact entry")
            if kind == "directory":
                if set(entry) != {"kind", "relpath"}:
                    raise ManifestError(
                        "reset evidence has invalid artifact directory"
                    )
            else:
                if (
                    set(entry)
                    != {"kind", "relpath", "byte_count", "content_sha256"}
                    or not isinstance(entry.get("byte_count"), int)
                    or isinstance(entry.get("byte_count"), bool)
                    or cast(int, entry["byte_count"]) < 0
                    or not _is_hash(entry.get("content_sha256"))
                ):
                    raise ManifestError("reset evidence has invalid artifact file")
            seen_relpaths.add(relpath)
            normalized_entries.append(entry)
        if normalized_entries != sorted(
            normalized_entries,
            key=lambda entry: str(entry["relpath"]),
        ):
            raise ManifestError("reset evidence artifact entries are not ordered")
        file_entries = [
            entry for entry in normalized_entries if entry["kind"] == "file"
        ]
        directory_entries = [
            entry
            for entry in normalized_entries
            if entry["kind"] == "directory"
        ]
        if (
            record["file_count"] != len(file_entries)
            or record["directory_count"] != len(directory_entries)
            or record["byte_count"]
            != sum(cast(int, entry["byte_count"]) for entry in file_entries)
            or record["tree_sha256"] != canonical_hash(normalized_entries)
        ):
            raise ManifestError("reset evidence artifact counters or hash mismatch")
    families = [str(record["family"]) for record in value]
    if families != sorted(RESET_ARTIFACT_FAMILIES):
        raise ManifestError("reset evidence artifact family coverage mismatch")
    return cast(list[dict[str, Any]], value)


def build_backup_metadata(
    *,
    manifest: CorpusManifest,
    backup_sha256: str,
    source_database_identity: dict[str, Any],
    source_postgres_state_sha256: str,
    source_security_state_sha256: str,
    pg_dump_version: str,
) -> dict[str, Any]:
    if source_postgres_state_sha256 != canonical_hash(
        manifest_postgres_state(manifest)
    ):
        raise ManifestError(
            "backup source PostgreSQL state differs from manifest"
        )
    if not _is_hash(source_security_state_sha256):
        raise ManifestError("backup source security state hash is invalid")
    return {
        "schema": BACKUP_METADATA_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": manifest.sha256,
        "backup_sha256": backup_sha256,
        "source_database_identity": _validate_database_identity(
            source_database_identity,
            label="backup source",
        ),
        "source_postgres_state_sha256": source_postgres_state_sha256,
        "source_security_state_sha256": source_security_state_sha256,
        "pg_dump_version": pg_dump_version,
    }


def build_restore_proof(
    *,
    manifest: CorpusManifest,
    backup_sha256: str,
    backup_metadata_sha256: str,
    scratch_database: str,
    scratch_database_marker: str,
    restored_state: str,
    restored_postgres_state_sha256: str,
    restored_security_state_sha256: str,
    pg_restore_version: str,
) -> dict[str, Any]:
    if restored_postgres_state_sha256 != canonical_hash(
        manifest_postgres_state(manifest)
    ):
        raise ManifestError(
            "restored PostgreSQL state differs from manifest"
        )
    if not _is_hash(restored_security_state_sha256):
        raise ManifestError("restored security state hash is invalid")
    return {
        "schema": RESTORE_PROOF_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": manifest.sha256,
        "backup_sha256": backup_sha256,
        "backup_metadata_sha256": backup_metadata_sha256,
        "scratch_database": scratch_database,
        "scratch_database_marker": scratch_database_marker,
        "restored_state": restored_state,
        "restored_postgres_state_sha256": restored_postgres_state_sha256,
        "restored_security_state_sha256": restored_security_state_sha256,
        "pg_restore_version": pg_restore_version,
    }


def _validate_metadata(
    metadata: dict[str, Any],
    *,
    manifest: CorpusManifest,
    backup_sha256: str,
) -> None:
    if metadata.get("schema") != BACKUP_METADATA_SCHEMA:
        raise ManifestError("backup metadata schema mismatch")
    if metadata.get("manifest_sha256") != manifest.sha256:
        raise ManifestError("backup metadata belongs to another manifest")
    if metadata.get("backup_sha256") != backup_sha256:
        raise ManifestError("backup metadata belongs to another archive")
    _validate_database_identity(
        metadata.get("source_database_identity"),
        label="backup source",
    )
    pg_dump_version = metadata.get("pg_dump_version")
    if not isinstance(pg_dump_version, str) or not pg_dump_version:
        raise ManifestError("backup metadata lacks pg_dump_version")
    expected_state_hash = canonical_hash(manifest_postgres_state(manifest))
    if metadata.get("source_postgres_state_sha256") != expected_state_hash:
        raise ManifestError(
            "backup metadata PostgreSQL state differs from manifest"
        )
    if not _is_hash(metadata.get("source_security_state_sha256")):
        raise ManifestError("backup metadata lacks a source security state hash")


def verify_backup_archive(
    backup: Path,
    *,
    manifest: CorpusManifest,
) -> ArchiveVerification:
    """Validate immutable archive bytes, metadata, and manifest binding."""

    if not backup.is_file() or backup.is_symlink():
        raise ManifestError(f"PostgreSQL archive is missing or unsafe: {backup}")
    sidecar = hash_sidecar_path(backup)
    try:
        expected_backup_hash = sidecar.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise ManifestError(f"backup hash sidecar is missing: {sidecar}") from exc
    actual_backup_hash = hash_file(backup)
    if actual_backup_hash != expected_backup_hash:
        raise ManifestError(
            "pre-reset database backup hash mismatch: "
            f"expected {expected_backup_hash}, got {actual_backup_hash}"
        )
    metadata, metadata_sha256 = load_hashed_json(metadata_path(backup))
    _validate_metadata(
        metadata,
        manifest=manifest,
        backup_sha256=actual_backup_hash,
    )
    return ArchiveVerification(
        backup_sha256=actual_backup_hash,
        metadata=metadata,
        metadata_sha256=metadata_sha256,
    )


def _validate_managed_scratch_identity(
    database_name: object,
    database_marker: object,
) -> tuple[str, str]:
    name = database_name if isinstance(database_name, str) else ""
    marker = database_marker if isinstance(database_marker, str) else ""
    if not is_managed_scratch_database(name, marker):
        raise ManifestError("restore proof lacks a managed scratch database identity")
    return name, marker


def source_database_identity(
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Return the exact source cluster/database identity from metadata."""

    return _validate_database_identity(
        metadata.get("source_database_identity"),
        label="backup source",
    )


def verify_backup_bundle(
    backup: Path,
    *,
    manifest: CorpusManifest,
) -> BackupBundleVerification:
    """Require a readable archive plus a successful manifest-exact restore."""

    verified = verify_backup_archive(
        backup,
        manifest=manifest,
    )
    proof, proof_sha256 = load_hashed_json(restore_proof_path(backup))
    if proof.get("schema") != RESTORE_PROOF_SCHEMA:
        raise ManifestError("backup restore-proof schema mismatch")
    expected_binding = {
        "manifest_sha256": manifest.sha256,
        "backup_sha256": verified.backup_sha256,
        "backup_metadata_sha256": verified.metadata_sha256,
    }
    for field, expected in expected_binding.items():
        if proof.get(field) != expected:
            raise ManifestError(f"backup restore proof {field} mismatch")
    if proof.get("restored_state") != "pre_reset_manifest_exact":
        raise ManifestError("backup lacks a successful manifest-exact restore proof")
    if not isinstance(proof.get("pg_restore_version"), str) or not proof[
        "pg_restore_version"
    ]:
        raise ManifestError("backup restore proof lacks pg_restore version")
    expected_state_hash = canonical_hash(manifest_postgres_state(manifest))
    if (
        proof.get("restored_postgres_state_sha256")
        != expected_state_hash
        or verified.metadata.get("source_postgres_state_sha256")
        != expected_state_hash
    ):
        raise ManifestError(
            "backup restore PostgreSQL state differs from source manifest"
        )
    if proof.get("restored_security_state_sha256") != verified.metadata.get(
        "source_security_state_sha256"
    ):
        raise ManifestError(
            "backup restore security state differs from source owner/ACL state"
        )
    _validate_managed_scratch_identity(
        proof.get("scratch_database"),
        proof.get("scratch_database_marker"),
    )
    return BackupBundleVerification(
        backup_sha256=verified.backup_sha256,
        metadata=verified.metadata,
        metadata_sha256=verified.metadata_sha256,
        proof=proof,
        proof_sha256=proof_sha256,
        source_database_identity=source_database_identity(verified.metadata),
    )


def _validate_database_identity(
    identity: object,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(identity, dict):
        raise ManifestError(f"reset receipt lacks {label} database identity")
    normalized = {
        "database_name": identity.get("database_name"),
        "database_oid": identity.get("database_oid"),
        "system_identifier": identity.get("system_identifier"),
        "server_version_num": identity.get("server_version_num"),
    }
    if (
        not isinstance(normalized["database_name"], str)
        or not normalized["database_name"]
        or not isinstance(normalized["system_identifier"], str)
        or not normalized["system_identifier"]
        or any(
            not isinstance(normalized[field], int)
            or isinstance(normalized[field], bool)
            or cast(int, normalized[field]) <= 0
            for field in ("database_oid", "server_version_num")
        )
    ):
        raise ManifestError(f"invalid {label} database identity")
    return normalized


def _validate_reset_boundary(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError("reset receipt lacks reset_boundary_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError("reset receipt has invalid reset_boundary_at") from exc
    if parsed.tzinfo is None:
        raise ManifestError("reset receipt reset_boundary_at lacks timezone")
    return value


def _canonical_state(
    state: object,
    *,
    label: str,
    expected_keys: frozenset[str] | None = None,
) -> dict[str, int]:
    if not isinstance(state, dict) or not state:
        raise ManifestError(f"reset receipt lacks {label} state")
    normalized: dict[str, int] = {}
    for key, value in state.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise ManifestError(f"reset receipt has invalid {label} state")
        normalized[key] = value
    if expected_keys is not None and set(normalized) != expected_keys:
        raise ManifestError(
            f"reset receipt {label} state keys mismatch: "
            f"expected={sorted(expected_keys)}, actual={sorted(normalized)}"
        )
    return normalized


def _validate_reset_receipt_payload(
    receipt: dict[str, Any],
    *,
    manifest: CorpusManifest,
    backup: BackupBundleVerification,
) -> dict[str, Any]:
    if (
        backup.metadata.get("manifest_sha256") != manifest.sha256
        or backup.proof.get("manifest_sha256") != manifest.sha256
    ):
        raise ManifestError("backup bundle belongs to another reset manifest")
    if receipt.get("schema") != RESET_RECEIPT_SCHEMA:
        raise ManifestError("reset receipt schema mismatch")
    expected_bindings = {
        "manifest_sha256": manifest.sha256,
        "backup_sha256": backup.backup_sha256,
        "backup_metadata_sha256": backup.metadata_sha256,
        "restore_proof_sha256": backup.proof_sha256,
    }
    for field, expected in expected_bindings.items():
        if receipt.get(field) != expected:
            raise ManifestError(f"reset receipt {field} mismatch")
    identity = _validate_database_identity(
        receipt.get("database_identity"),
        label="receipt",
    )
    if identity != backup.source_database_identity:
        raise ManifestError(
            "reset receipt database identity differs from backup source"
        )
    boundary = _validate_reset_boundary(receipt.get("reset_boundary_at"))
    zero_state = _canonical_state(
        receipt.get("zero_state"),
        label="zero",
        expected_keys=RESET_ZERO_STATE_KEYS,
    )
    if any(zero_state.values()):
        raise ManifestError("reset receipt zero state is not zero")
    post_reset_postgres_state = receipt.get("post_reset_postgres_state")
    if not isinstance(post_reset_postgres_state, dict):
        raise ManifestError("reset receipt lacks exact post-reset PostgreSQL state")
    assert_post_reset_postgres_state(
        manifest,
        post_reset_postgres_state,
    )
    artifact_state = validate_artifact_family_state(
        receipt.get("artifact_family_state")
    )
    trash_root = receipt.get("trash_root")
    if not isinstance(trash_root, str) or not trash_root:
        raise ManifestError("reset receipt lacks trash_root")
    return {
        **receipt,
        "database_identity": identity,
        "reset_boundary_at": boundary,
        "zero_state": zero_state,
        "post_reset_postgres_state": post_reset_postgres_state,
        "artifact_family_state": artifact_state,
    }


def build_reset_receipt(
    *,
    manifest: CorpusManifest,
    backup: BackupBundleVerification,
    live_database_identity: dict[str, Any],
    reset_boundary_at: str,
    zero_state: dict[str, int],
    post_reset_postgres_state: dict[str, object],
    trash_root: str,
    artifact_family_state: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a receipt after commit, file move and post-reset validation.

    ``reset_boundary_at`` must be the value returned by the live database
    ``clock_timestamp()`` after the reset commit, artifact move, and all
    postconditions while both mutation locks are still held.  This helper
    intentionally has no fallback to client time.
    """

    return _validate_reset_receipt_payload(
        {
            "schema": RESET_RECEIPT_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "manifest_sha256": manifest.sha256,
            "backup_sha256": backup.backup_sha256,
            "backup_metadata_sha256": backup.metadata_sha256,
            "restore_proof_sha256": backup.proof_sha256,
            "database_identity": live_database_identity,
            "reset_boundary_at": reset_boundary_at,
            "zero_state": zero_state,
            "post_reset_postgres_state": post_reset_postgres_state,
            "trash_root": trash_root,
            "artifact_family_state": artifact_family_state,
        },
        manifest=manifest,
        backup=backup,
    )


def write_reset_receipt_once(
    backup_path: Path,
    receipt: dict[str, Any],
) -> str:
    if receipt.get("schema") != RESET_RECEIPT_SCHEMA:
        raise ManifestError("reset receipt schema mismatch")
    return write_hashed_json_once(reset_receipt_path(backup_path), receipt)


def verify_reset_receipt(
    backup_path: Path,
    *,
    manifest: CorpusManifest,
) -> ResetReceiptVerification:
    backup = verify_backup_bundle(
        backup_path,
        manifest=manifest,
    )
    receipt, receipt_sha256 = load_hashed_json(reset_receipt_path(backup_path))
    receipt = _validate_reset_receipt_payload(
        receipt,
        manifest=manifest,
        backup=backup,
    )
    return ResetReceiptVerification(
        receipt=receipt,
        receipt_sha256=receipt_sha256,
        backup=backup,
    )
