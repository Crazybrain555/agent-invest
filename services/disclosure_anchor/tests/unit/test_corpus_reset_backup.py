"""Safety contracts for manifest-bound database backup and restore proof."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from typing import cast

from scripts.corpus_reparse_manifest import (
    CorpusManifest,
    ManifestError,
    canonical_hash,
)
from scripts.corpus_reset_backup import (
    BACKUP_EXTENSIONS,
    BACKUP_SCHEMAS,
    RESET_ARTIFACT_FAMILIES,
    RESET_ZERO_STATE_KEYS,
    assert_same_database_identity,
    build_backup_metadata,
    build_reset_receipt,
    build_restore_proof,
    hash_sidecar_path,
    metadata_path,
    postgres_client_environment,
    recover_hashed_json_sidecar,
    reset_receipt_path,
    restore_proof_path,
    verify_backup_bundle,
    verify_reset_receipt,
    write_hash_sidecar_once,
    write_hashed_json_once,
    write_reset_receipt_once,
)
from scripts.corpus_reset_digest import RESET_MUTATED_SCOPES
from scripts.corpus_reset_state import (
    assert_post_reset_postgres_state,
    assert_zero_state_counts,
)
from scripts.create_corpus_reset_backup import (
    _run_pg_dump,
    pg_dump_command,
)
from scripts.managed_scratch_database import (
    SCRATCH_DATABASE_COMMENT_PREFIX,
)
from scripts.prove_corpus_reset_backup import pg_restore_command
from tests.unit._corpus_reset_manifest import postgres_state_matrix


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _manifest() -> CorpusManifest:
    return CorpusManifest(
        header={
            "postgres_state": postgres_state_matrix(),
            "code_snapshot": {"snapshot_sha256": _sha256(b"code")},
        },
        documents=(),
        runs=(),
        sha256=_sha256(b"manifest"),
    )


def _zero_state() -> dict[str, int]:
    return {key: 0 for key in RESET_ZERO_STATE_KEYS}


def _artifact_state() -> list[dict[str, object]]:
    return [
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
        for family, relpath in sorted(RESET_ARTIFACT_FAMILIES.items())
    ]


def _post_reset_state(manifest: CorpusManifest) -> dict[str, object]:
    state = deepcopy(manifest.header["postgres_state"])
    assert isinstance(state, dict)
    records = state["scopes"]
    assert isinstance(records, dict)
    for scope in RESET_MUTATED_SCOPES:
        if scope.value not in records:
            continue
        record = records[scope.value]
        assert isinstance(record, dict)
        record["state_sha256"] = _sha256(f"post:{scope.value}".encode())
    return state


class CorpusResetBackupTests(unittest.TestCase):
    def test_zero_state_rejects_residual_rows_and_missing_scope_proof(
        self,
    ) -> None:
        zero = _zero_state()
        assert_zero_state_counts(zero)

        residual = {**zero, "projections": 1}
        with self.assertRaisesRegex(
            ManifestError,
            r"residual rows: \{'projections': 1\}",
        ):
            assert_zero_state_counts(residual)

        incomplete = dict(zero)
        incomplete.pop("derived_events")
        with self.assertRaisesRegex(
            ManifestError,
            r"missing=\['derived_events'\]",
        ):
            assert_zero_state_counts(incomplete)

    def test_post_reset_scope_drift_is_structured_failure(self) -> None:
        manifest = _manifest()
        actual = deepcopy(manifest.header["postgres_state"])
        scopes = actual["scopes"]
        assert isinstance(scopes, dict)
        scopes.pop("document_unit")

        with self.assertRaisesRegex(
            ManifestError,
            r"missing_scopes=\['document_unit'\]",
        ):
            assert_post_reset_postgres_state(manifest, actual)

    def test_postgres_commands_have_fixed_scope_and_no_database_url(self) -> None:
        dump = pg_dump_command(
            Path("/opt/pg/bin/pg_dump"),
            snapshot="00000003-0000001A-1",
            destination=Path("/safe/pre-reset.dump"),
        )
        restore = pg_restore_command(
            Path("/opt/pg/bin/pg_restore"),
            backup=Path("/safe/pre-reset.dump"),
            database_name="scratch_db",
        )

        self.assertIn("--format=custom", dump)
        self.assertIn("--snapshot=00000003-0000001A-1", dump)
        self.assertEqual(
            {
                item.removeprefix("--schema=")
                for item in dump
                if item.startswith("--schema=")
            },
            set(BACKUP_SCHEMAS),
        )
        self.assertEqual(
            {
                item.removeprefix("--extension=")
                for item in dump
                if item.startswith("--extension=")
            },
            set(BACKUP_EXTENSIONS),
        )
        self.assertNotIn("--create", dump)
        self.assertNotIn("--clean", restore)
        self.assertNotIn("--no-owner", dump)
        self.assertNotIn("--no-privileges", dump)
        self.assertNotIn("--no-owner", restore)
        self.assertNotIn("--no-privileges", restore)
        self.assertIn("--dbname=scratch_db", restore)
        self.assertIn("--single-transaction", restore)
        self.assertIn("--exit-on-error", restore)
        self.assertFalse(
            any("postgresql://" in argument for argument in (*dump, *restore))
        )

    def test_postgres_url_is_split_into_libpq_environment(self) -> None:
        environment = postgres_client_environment(
            "postgresql+psycopg://user:secret@/invest_engine"
            "?host=%2FVolumes%2FAgentSSD%2Fpostgres%2Fsockets&port=55432"
        )
        self.assertEqual(
            environment["PGHOST"],
            "/Volumes/AgentSSD/postgres/sockets",
        )
        self.assertEqual(environment["PGPORT"], "55432")
        self.assertEqual(environment["PGDATABASE"], "invest_engine")
        self.assertEqual(environment["PGPASSWORD"], "secret")
        with self.assertRaisesRegex(ManifestError, "PostgreSQL"):
            postgres_client_environment("sqlite:///tmp/example.db")
        identity = {
            "database_name": "invest_engine",
            "database_oid": 123,
            "system_identifier": "456",
            "server_version_num": 180004,
        }
        with patch(
            "scripts.corpus_reset_backup.database_identity",
            side_effect=[identity, {**identity, "database_oid": 999}],
        ):
            with self.assertRaisesRegex(
                ManifestError,
                "different PostgreSQL databases",
            ):
                assert_same_database_identity(MagicMock(), MagicMock())

    def test_pg_dump_receives_exported_snapshot_via_fixed_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "staged.dump"
            observed: dict[str, object] = {}

            def fake_run(
                command: tuple[str, ...],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                observed["command"] = command
                observed["env"] = kwargs["env"]
                output_arg = next(
                    value for value in command if value.startswith("--file=")
                )
                Path(output_arg.removeprefix("--file=")).write_bytes(b"archive")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch(
                "scripts.create_corpus_reset_backup.subprocess.run",
                side_effect=fake_run,
            ):
                _run_pg_dump(
                    pg_dump=Path("/opt/pg/bin/pg_dump"),
                    database_url=(
                        "postgresql+psycopg://user:secret@localhost/db"
                    ),
                    snapshot="00000003-0000001A-1",
                    destination=destination,
                )

            command = cast(tuple[str, ...], observed["command"])
            self.assertIn("--snapshot=00000003-0000001A-1", command)
            self.assertFalse(any("secret" in argument for argument in command))
            environment = cast(dict[str, str], observed["env"])
            self.assertEqual(environment["PGDATABASE"], "db")
            self.assertEqual(environment["PGPASSWORD"], "secret")

    def test_bundle_requires_restore_state_identity_and_outer_hashes(self) -> None:
        manifest = _manifest()
        security_state_sha256 = _sha256(b"owner-and-acl-state")
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "pre-reset.dump"
            backup.write_bytes(b"archive")
            backup_hash = _sha256(b"archive")
            write_hash_sidecar_once(backup, backup_hash)
            metadata = build_backup_metadata(
                manifest=manifest,
                backup_sha256=backup_hash,
                source_database_identity={
                    "database_name": "invest_engine",
                    "database_oid": 12345,
                    "system_identifier": "76543210987654321",
                    "server_version_num": 180004,
                },
                source_postgres_state_sha256=canonical_hash(
                    manifest.header["postgres_state"]
                ),
                source_security_state_sha256=security_state_sha256,
                pg_dump_version="pg_dump (PostgreSQL) 18.4",
            )
            metadata_hash = write_hashed_json_once(
                metadata_path(backup),
                metadata,
            )
            scratch_name = (
                "invest_engine_scratch_1785000035_456_deadbeef"
            )
            proof = build_restore_proof(
                manifest=manifest,
                backup_sha256=backup_hash,
                backup_metadata_sha256=metadata_hash,
                scratch_database=scratch_name,
                scratch_database_marker=(
                    f"{SCRATCH_DATABASE_COMMENT_PREFIX}1785000035:"
                    f"{scratch_name}"
                ),
                restored_state="pre_reset_manifest_exact",
                restored_postgres_state_sha256=canonical_hash(
                    manifest.header["postgres_state"]
                ),
                restored_security_state_sha256=security_state_sha256,
                pg_restore_version="pg_restore (PostgreSQL) 18.4",
            )
            write_hashed_json_once(restore_proof_path(backup), proof)

            verified = verify_backup_bundle(
                backup,
                manifest=manifest,
            )
            self.assertEqual(verified.backup_sha256, backup_hash)
            with self.assertRaisesRegex(ManifestError, "differs"):
                build_reset_receipt(
                    manifest=manifest,
                    backup=verified,
                    live_database_identity={
                        **verified.source_database_identity,
                        "database_oid": 99999,
                    },
                    reset_boundary_at="2026-07-27T12:34:56+00:00",
                    zero_state=_zero_state(),
                    post_reset_postgres_state=_post_reset_state(manifest),
                    trash_root="/safe/reset-trash/manifest",
                    artifact_family_state=_artifact_state(),
                )
            changed_preserved = _post_reset_state(manifest)
            scopes = changed_preserved["scopes"]
            assert isinstance(scopes, dict)
            company = scopes["company"]
            assert isinstance(company, dict)
            company["state_sha256"] = _sha256(b"changed-preserved")
            with self.assertRaisesRegex(ManifestError, "preserved"):
                build_reset_receipt(
                    manifest=manifest,
                    backup=verified,
                    live_database_identity=verified.source_database_identity,
                    reset_boundary_at="2026-07-27T12:34:56+00:00",
                    zero_state=_zero_state(),
                    post_reset_postgres_state=changed_preserved,
                    trash_root="/safe/reset-trash/manifest",
                    artifact_family_state=_artifact_state(),
                )
            receipt = build_reset_receipt(
                manifest=manifest,
                backup=verified,
                live_database_identity=verified.source_database_identity,
                reset_boundary_at="2026-07-27T12:34:56.123456+00:00",
                zero_state=_zero_state(),
                post_reset_postgres_state=_post_reset_state(manifest),
                trash_root="/safe/reset-trash/manifest",
                artifact_family_state=_artifact_state(),
            )
            write_reset_receipt_once(backup, receipt)
            receipt_path = reset_receipt_path(backup)
            hash_sidecar_path(receipt_path).unlink()
            recovered_digest = recover_hashed_json_sidecar(receipt_path)
            receipt_verification = verify_reset_receipt(
                backup,
                manifest=manifest,
            )
            self.assertEqual(
                receipt_verification.receipt_sha256,
                recovered_digest,
            )

            proof["restored_security_state_sha256"] = _sha256(b"other-acl")
            restore_proof_path(backup).unlink()
            hash_sidecar_path(restore_proof_path(backup)).unlink()
            write_hashed_json_once(restore_proof_path(backup), proof)
            with self.assertRaisesRegex(ManifestError, "owner/ACL"):
                verify_backup_bundle(
                    backup,
                    manifest=manifest,
                )


if __name__ == "__main__":
    unittest.main()
