from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import reparse_corpus
from scripts.corpus_reparse_manifest import (
    MANIFEST_SCHEMA,
    CorpusManifest,
    ManifestError,
    canonical_hash,
    load_manifest,
    safe_data_path,
    validate_code_snapshot,
    validate_reset_bundle_paths,
    write_manifest,
)
from scripts.corpus_reset_backup import recover_hashed_json_sidecar
from scripts.reset_derived_corpus import (
    _artifact_family_state,
    _artifact_inventory,
    _artifact_partition_state,
    _trash_artifact_inventory,
)
from scripts.corpus_reset_quiescence import (
    assert_destructive_services_quiescent,
)
from tests.unit._corpus_reset_manifest import postgres_state_matrix
from disclosure_anchor.application.worker import worker as worker_module
from disclosure_anchor.application.ports.parser import (
    ParserIdentity,
    ParserOptions,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from disclosure_anchor.cli import worker as worker_cli


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _document(
    raw_hash: str,
    *,
    document_id: str = "doc_1",
) -> dict[str, object]:
    raw_relpath = f"raw_documents/cninfo/{document_id}/document.pdf"
    return {
        "document_id": document_id,
        "raw_file_relpath": raw_relpath,
        "raw_file_hash": raw_hash,
        "old_status": "published",
        "old_current_processing_run_id": None,
        "input_identity_sha256": canonical_hash(
            {"document_id": document_id, "raw_file_hash": raw_hash}
        ),
    }


def _header(documents: list[dict[str, object]]) -> dict[str, object]:
    code_snapshot: dict[str, object] = {
        "git_head": "0" * 40,
        "scope": [
            "services/disclosure_anchor/config",
            "services/disclosure_anchor/contracts",
            "services/disclosure_anchor/scripts",
            "services/disclosure_anchor/src",
        ],
        "tracked_diff_sha256": _sha256(b"diff"),
        "untracked_inventory_sha256": _sha256(b"untracked"),
        "untracked_file_count": 0,
    }
    return {
        "manifest_schema": MANIFEST_SCHEMA,
        "generated_at": "2026-07-27T00:00:00+00:00",
        "document_count": len(documents),
        "processing_run_count": 0,
        "postgres_state": postgres_state_matrix(),
        "target_identity": {
            "parser_target": ParserTargetIdentity(
                name="MinerU",
                package_version="3.4.0",
                backend="vlm-http-client",
                method="auto",
                language="ch",
                formula=False,
                table=True,
                image_analysis=True,
                runtime_bundle_identity_sha256=_sha256(b"deployment"),
            ).to_payload(),
            "max_parse_retries": 3,
            "max_build_retries": 3,
            "builder_rules_version": "ub-test",
            "retrieval_rules_version": "rp-test",
        },
        "code_snapshot": code_snapshot,
    }


class CorpusReparseManifestTests(unittest.TestCase):
    def test_target_identity_freezes_effective_parser_and_retry_policy(self) -> None:
        deps = MagicMock()
        deps.parser_factory.return_value.identity.return_value = ParserIdentity(
            name="MinerU",
            version="3.4.0",
        )
        deps.parser_options = ParserOptions(
            backend="hybrid-http-client",
            formula=True,
            table=False,
            effort="medium",
            image_analysis=True,
            runtime_bundle_identity_sha256=_sha256(b"deployment"),
        )
        deps.config.max_parse_retries = 4
        deps.config.max_build_retries = 5
        identity = reparse_corpus._target_identity(deps)
        parser_target = identity["parser_target"]

        self.assertTrue(parser_target["formula"])
        self.assertFalse(parser_target["table"])
        self.assertEqual(parser_target["effort"], "medium")
        self.assertFalse(parser_target["image_analysis"])
        self.assertTrue(parser_target["full_pdf"])
        self.assertEqual(identity["max_parse_retries"], 4)
        self.assertEqual(identity["max_build_retries"], 5)
        self.assertIn("retrieval_rules_version", identity)

        deps.parser_options = ParserOptions(
            backend="vlm-http-client",
            start_page=0,
            runtime_bundle_identity_sha256=_sha256(b"deployment"),
        )
        with self.assertRaisesRegex(ManifestError, "full PDF"):
            reparse_corpus._target_identity(deps)

    def test_cli_help_imports_without_runtime_environment(self) -> None:
        root = Path(__file__).resolve().parents[2]
        for script in ("reparse_corpus.py", "reset_derived_corpus.py"):
            with self.subTest(script=script):
                completed = subprocess.run(
                    [sys.executable, str(root / "scripts" / script), "--help"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("usage:", completed.stdout)

    def test_manifest_is_canonical_write_once_and_tamper_evident(self) -> None:
        raw_hash = _sha256(b"%PDF-")
        document = _document(raw_hash)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_root = root / "service"
            raw = data_root / "data" / str(document["raw_file_relpath"])
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"%PDF-")
            path = root / "manifest.jsonl"
            digest = write_manifest(
                path,
                header=_header([document]),
                documents=[document],
                runs=[],
            )
            manifest = load_manifest(
                path,
                data_root=data_root,
                verify_raw_files=True,
            )
            self.assertEqual(manifest.sha256, digest)
            with self.assertRaisesRegex(ManifestError, "overwrite"):
                write_manifest(
                    path,
                    header=_header([document]),
                    documents=[document],
                    runs=[],
                )
            path.write_bytes(path.read_bytes().replace(b"doc_1", b"doc_X", 1))
            with self.assertRaisesRegex(ManifestError, "hash mismatch"):
                load_manifest(path)

    def test_manifest_rejects_partial_inventory_and_old_schema(self) -> None:
        document = _document(_sha256(b"%PDF-"))
        with tempfile.TemporaryDirectory() as tmp:
            partial = _header([document])
            partial["document_count"] = 0
            with self.assertRaisesRegex(ManifestError, "document_count"):
                write_manifest(
                    Path(tmp) / "partial.jsonl",
                    header=partial,
                    documents=[document],
                    runs=[],
                )
            old = _header([document])
            old["manifest_schema"] = "corpus-reparse-reset-manifest.v4"
            with self.assertRaisesRegex(ManifestError, "schema mismatch"):
                write_manifest(
                    Path(tmp) / "old.jsonl",
                    header=old,
                    documents=[document],
                    runs=[],
                )
            legacy_path = Path(tmp) / "legacy-v4.jsonl"
            legacy_payload = (
                json.dumps(
                    {"record_type": "header", **old},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
                + json.dumps(
                    {"record_type": "document", **document},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            legacy_path.write_bytes(legacy_payload)
            legacy_path.with_suffix(".jsonl.sha256").write_text(
                _sha256(legacy_payload) + "\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(ManifestError, "schema mismatch"):
                load_manifest(legacy_path)

            incomplete = _header([document])
            state = incomplete["postgres_state"]
            assert isinstance(state, dict)
            scopes = state["scopes"]
            assert isinstance(scopes, dict)
            scopes.pop("document_unit")
            with self.assertRaisesRegex(ManifestError, "scope coverage"):
                write_manifest(
                    Path(tmp) / "incomplete-state.jsonl",
                    header=incomplete,
                    documents=[document],
                    runs=[],
                )

    def test_manifest_rejects_raw_mismatch_and_storage_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "service"
            raw = (
                data_root
                / "data"
                / "raw_documents"
                / "cninfo"
                / "doc_1"
                / "document.pdf"
            )
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"%PDF-")
            document = _document(_sha256(b"different"))
            path = Path(tmp) / "manifest.jsonl"
            write_manifest(
                path,
                header=_header([document]),
                documents=[document],
                runs=[],
            )
            with self.assertRaisesRegex(ManifestError, "raw hash mismatch"):
                load_manifest(
                    path,
                    data_root=data_root,
                    verify_raw_files=True,
                )
            for relpath in (
                "/tmp/file",
                "../raw_documents/file",
                "derived/normalized_ir/file",
                "raw_documents/../outside.pdf",
            ):
                with self.subTest(relpath=relpath):
                    with self.assertRaises(ManifestError):
                        safe_data_path(data_root, relpath, family="raw")

    def test_reset_bundle_is_one_non_derived_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "service"
            bundle = data_root / "audit" / "reset-bundles" / "operation-1"
            manifest = bundle / "manifest.jsonl"
            journal = bundle / "journal.jsonl"
            self.assertEqual(
                validate_reset_bundle_paths(data_root, manifest, journal),
                bundle.resolve(),
            )
            with self.assertRaisesRegex(ManifestError, "share one"):
                validate_reset_bundle_paths(
                    data_root,
                    manifest,
                    data_root
                    / "audit"
                    / "reset-bundles"
                    / "operation-2"
                    / "journal.jsonl",
                )

            run = {
                "processing_run_id": "run_1",
                "document_id": "doc_1",
                "run_kind": "parse",
                "status": "succeeded",
                "is_active": True,
                "input_raw_file_hash": _sha256(b"raw"),
                "parser_artifact_relpath": "parser_artifacts/doc_1/run_1",
                "normalized_ir_relpath": (
                    "derived/normalized_ir/doc_1/run_1/normalized_ir.v4.json"
                ),
                "document_units_relpath": None,
            }
            frozen = CorpusManifest(
                header={},
                documents=(),
                runs=(run,),
                sha256=_sha256(b"manifest"),
            )
            parser_file = (
                data_root
                / "data"
                / "parser_artifacts"
                / "doc_1"
                / "run_1"
                / "result.json"
            )
            parser_file.parent.mkdir(parents=True)
            parser_file.write_text("parser", encoding="utf-8")
            orphan = (
                data_root
                / "data"
                / "parser_artifacts"
                / "orphan"
                / "unregistered.bin"
            )
            orphan.parent.mkdir(parents=True)
            orphan.write_bytes(b"orphan")
            normalized = (
                data_root
                / "data"
                / str(run["normalized_ir_relpath"])
            )
            normalized.parent.mkdir(parents=True)
            normalized.write_text("{}", encoding="utf-8")
            inventory = _artifact_inventory(frozen, data_root)
            exact_state = _artifact_family_state(inventory)
            parser_state = next(
                record
                for record in exact_state
                if record["family"] == "parser_artifact"
            )
            self.assertTrue(
                any(
                    entry["relpath"] == "orphan/unregistered.bin"
                    for entry in parser_state["entries"]
                )
            )
            trash_root = data_root / "audit" / "reset-trash" / "operation"
            trash_state = _artifact_family_state(
                _trash_artifact_inventory(
                    inventory,
                    trash_root=trash_root,
                )
            )
            self.assertEqual(
                _artifact_partition_state(exact_state, trash_state),
                exact_state,
            )
            moved_parser = (
                trash_root / "data" / "parser_artifacts" / "doc_1" / "run_1"
            )
            moved_parser.mkdir(parents=True)
            (moved_parser / "result.json").write_text(
                "parser",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ManifestError,
                "both live and reset trash",
            ):
                _artifact_partition_state(
                    exact_state,
                    _artifact_family_state(
                        _trash_artifact_inventory(
                            inventory,
                            trash_root=trash_root,
                        )
                    ),
                )

            torn_record = bundle / "torn-record.json"
            torn_record.parent.mkdir(parents=True)
            torn_record.write_bytes(b'{"schema":')
            with self.assertRaisesRegex(ManifestError, "incomplete JSON"):
                recover_hashed_json_sidecar(torn_record)

    def test_code_snapshot_drift_is_rejected(self) -> None:
        manifest = MagicMock()
        manifest.header = {"code_snapshot": {"git_head": "old"}}
        with patch(
            "scripts.corpus_reparse_manifest.capture_code_snapshot",
            return_value={"git_head": "new"},
        ):
            with self.assertRaisesRegex(ManifestError, "code drifted"):
                validate_code_snapshot(manifest)


class ExactReplayTests(unittest.TestCase):
    def test_manifest_composes_one_runtime_database_and_raw_guard(self) -> None:
        manifest = MagicMock()
        manifest.sha256 = "sha256:" + "1" * 64
        manifest.documents = (
            {
                "document_id": "doc_1",
                "raw_file_hash": "sha256:" + "2" * 64,
            },
        )
        generation = reparse_corpus.ReparseGeneration(
            started_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
            receipt_sha256="sha256:" + "3" * 64,
            database_identity={},
        )
        settings = MagicMock()
        deps = MagicMock()
        engine = MagicMock()
        with (
            patch.object(reparse_corpus, "_validate_runtime_identity") as runtime,
            patch.object(reparse_corpus, "_validate_document_truth") as database,
        ):
            guard = reparse_corpus._exact_replay_guard(
                manifest, generation, settings
            )
            guard.runtime_check(deps)
            guard.database_check(engine)
        runtime.assert_called_once_with(manifest, deps, settings)
        database.assert_called_once_with(engine, manifest)
        guard.raw_identity_check("doc_1", "sha256:" + "2" * 64)
        with self.assertRaisesRegex(RuntimeError, "raw identity drifted"):
            guard.raw_identity_check("doc_1", "sha256:" + "3" * 64)
        with self.assertRaisesRegex(RuntimeError, "outside"):
            guard.raw_identity_check("doc_2", "sha256:" + "2" * 64)

    def test_parse_admission_invokes_raw_guard(self) -> None:
        raw_guard = MagicMock()
        deps = MagicMock()
        deps.replay_raw_identity_guard = raw_guard
        deps.page_counter = None
        worker_module._parse_work_items(
            [{
                "document_id": "doc_1",
                "raw_file_hash": "sha256:" + "3" * 64,
                "raw_byte_count": 5,
            }],
            deps=deps,
        )
        raw_guard.assert_called_once_with(
            "doc_1", "sha256:" + "3" * 64
        )

    def test_generation_and_runtime_preconditions_fail_closed(self) -> None:
        manifest = MagicMock()
        manifest.header = {
            "target_identity": _header([])["target_identity"]
        }
        connection = MagicMock()
        connection.execute.return_value.scalars.return_value = ["run_bad"]
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        with self.assertRaisesRegex(ManifestError, "non-manifest"):
            reparse_corpus._assert_generation_run_identity(
                engine,
                manifest,
                generation_started_at=datetime(
                    2026, 7, 27, tzinfo=timezone.utc
                ),
            )
        statement, params = connection.execute.call_args.args
        self.assertIn("builder_rules_version", str(statement))
        self.assertEqual(params["builder_rules_version"], "ub-test")

        migration_connection = MagicMock()
        migration_connection.execute.return_value.scalar_one_or_none.return_value = (
            "old"
        )
        migration_engine = MagicMock()
        migration_engine.connect.return_value.__enter__.return_value = (
            migration_connection
        )
        with (
            patch.object(reparse_corpus, "single_migration_head", return_value="head"),
            self.assertRaisesRegex(ManifestError, "expected head, got old"),
        ):
            reparse_corpus._assert_migration_head(migration_engine)

        base_status = {
            "complete": False,
            "invariant_documents": 0,
            "global_invariants": {},
            "state_counts": {"pending": 0},
        }
        self.assertEqual(
            reparse_corpus._status_exit_code({**base_status, "complete": True}),
            0,
        )
        self.assertEqual(
            reparse_corpus._status_exit_code(
                {**base_status, "invariant_documents": 1}
            ),
            reparse_corpus.INVARIANT_EXIT_CODE,
        )
        self.assertEqual(
            reparse_corpus._status_exit_code(
                {**base_status, "state_counts": {"pending": 1}}
            ),
            reparse_corpus.CONTINUE_EXIT_CODE,
        )
        self.assertEqual(
            reparse_corpus._status_exit_code(base_status),
            reparse_corpus.TERMINAL_FAILURE_EXIT_CODE,
        )

    def test_resident_entry_reuses_singleton_and_existing_loop(self) -> None:
        settings = MagicMock()
        guard = worker_cli.ExactReplayGuard(
            manifest_sha256="sha256:" + "1" * 64,
            reset_boundary_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
            document_count=1,
            runtime_check=MagicMock(),
            database_check=MagicMock(),
            raw_identity_check=MagicMock(),
        )
        lock_connection = MagicMock()
        lock_connection.execute.return_value.scalar_one.return_value = True
        lock_engine = MagicMock()
        lock_engine.connect.return_value = lock_connection
        with (
            patch.object(worker_cli, "_print_version_banner"),
            patch.object(
                worker_cli.sqlalchemy,
                "create_engine",
                return_value=lock_engine,
            ),
            patch.object(worker_cli, "_database_url", return_value="postgresql://x"),
            patch.object(worker_cli, "_run_loop", return_value=0) as loop,
        ):
            result = worker_cli.run_resident_worker(
                settings,
                exact_replay_guard=guard,
            )
        self.assertEqual(result, 0)
        loop.assert_called_once()
        _, call_kwargs = loop.call_args
        self.assertIs(call_kwargs["lock_conn"], lock_connection)
        self.assertIs(call_kwargs["exact_replay_guard"], guard)
        lock_connection.execute.return_value.scalar_one.return_value = False
        with (
            patch.object(worker_cli, "_print_version_banner"),
            patch.object(
                worker_cli.sqlalchemy,
                "create_engine",
                return_value=lock_engine,
            ),
            patch.object(worker_cli, "_database_url", return_value="postgresql://x"),
        ):
            with self.assertRaisesRegex(
                worker_cli.WorkerSingletonGuardError,
                "stop and drain",
            ):
                worker_cli.run_resident_worker(
                    settings,
                    exact_replay_guard=guard,
                )

    def test_run_uses_only_the_resident_worker_composition(self) -> None:
        manifest = MagicMock()
        manifest.header = {}
        generation = reparse_corpus.ReparseGeneration(
            started_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
            receipt_sha256="sha256:" + "3" * 64,
            database_identity={"database_name": "scratch"},
        )
        guard = MagicMock(spec=worker_cli.ExactReplayGuard)
        engine = MagicMock()
        lock_connection = MagicMock()
        lock_context = MagicMock()
        lock_context.__enter__.return_value = lock_connection
        with (
            patch.object(reparse_corpus, "load_settings") as load_settings,
            patch.object(reparse_corpus, "_validate_bundle_paths"),
            patch.object(reparse_corpus, "load_manifest", return_value=manifest),
            patch.object(reparse_corpus, "validate_code_snapshot"),
            patch.object(
                reparse_corpus,
                "migration_database_url",
                return_value="postgresql://owner/scratch",
            ) as migration_url,
            patch.object(
                reparse_corpus,
                "create_db_engine",
                return_value=engine,
            ) as create_engine,
            patch.object(
                reparse_corpus,
                "worker_database_url",
                return_value="postgresql://app/scratch",
            ),
            patch.object(
                reparse_corpus,
                "worker_singleton_lock",
                return_value=lock_context,
            ),
            patch.object(
                reparse_corpus,
                "assert_same_database_identity",
            ) as same_database,
            patch.object(
                reparse_corpus,
                "assert_worker_singleton_or_cancel",
            ),
            patch.object(reparse_corpus, "_validate_document_truth"),
            patch.object(
                reparse_corpus,
                "_load_reparse_generation",
                return_value=generation,
            ),
            patch.object(reparse_corpus, "_assert_migration_head"),
            patch.object(reparse_corpus, "_assert_generation_run_identity"),
            patch.object(
                reparse_corpus,
                "_exact_replay_guard",
                return_value=guard,
            ),
            patch.object(
                reparse_corpus,
                "build_worker_dependencies",
            ) as build_deps,
            patch.object(
                reparse_corpus,
                "run_resident_worker",
                return_value=0,
            ) as resident,
        ):
            result = reparse_corpus.main(
                [
                    "--run",
                    "--manifest",
                    "/tmp/manifest.jsonl",
                    "--reset-receipt",
                    "/tmp/backup.reset-receipt.json",
                ]
            )
        self.assertEqual(result, 0)
        migration_url.assert_called_once_with(load_settings.return_value)
        create_engine.assert_called_once_with("postgresql://owner/scratch")
        same_database.assert_called_once_with(
            lock_connection,
            engine.connect.return_value.__enter__.return_value,
        )
        build_deps.assert_not_called()
        resident.assert_called_once_with(
            load_settings.return_value,
            exact_replay_guard=guard,
        )


class ResetQuiescenceTests(unittest.TestCase):
    def test_reset_requires_disabled_unloaded_services_and_no_processes(
        self,
    ) -> None:
        controller = MagicMock()
        controller.is_loaded.return_value = False
        controller.is_disabled.return_value = True
        assert_destructive_services_quiescent(
            launchctl=controller,
            process_rows=["1 /sbin/launchd"],
        )
        controller.is_loaded.return_value = True
        with self.assertRaisesRegex(ManifestError, "still loaded"):
            assert_destructive_services_quiescent(
                launchctl=controller,
                process_rows=["1 /sbin/launchd"],
            )
        controller.is_loaded.return_value = False
        for row in (
            "100 /bin/mineru -p sample.pdf",
            "101 python -m disclosure_anchor.cli.worker loop",
            "102 python scripts/reparse_corpus.py --run --manifest /x",
        ):
            with self.subTest(row=row):
                with self.assertRaisesRegex(ManifestError, "conflicting"):
                    assert_destructive_services_quiescent(
                        launchctl=controller,
                        process_rows=[row],
                    )


class PurgeResetTrashTests(unittest.TestCase):
    MANIFEST_SHA = "sha256:" + "a" * 64

    def _trash(self, root: Path) -> Path:
        trash = root / "audit" / "reset-trash" / ("a" * 64)
        (trash / "data" / "parser_artifacts").mkdir(parents=True)
        (trash / "data" / "parser_artifacts" / "x.json").write_text(
            "{}", encoding="utf-8"
        )
        return trash

    def test_refuses_until_status_is_a_verified_complete_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash = self._trash(root)
            for status_code in (70, 74, 75):
                code = reparse_corpus.purge_reset_trash(
                    self.MANIFEST_SHA,
                    data_root=root,
                    status_exit_code=status_code,
                    confirmed=True,
                )
                self.assertEqual(code, status_code)
                self.assertTrue(trash.exists())

    def test_dry_run_reports_and_keeps_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash = self._trash(root)
            code = reparse_corpus.purge_reset_trash(
                self.MANIFEST_SHA,
                data_root=root,
                status_exit_code=0,
                confirmed=False,
            )
            self.assertEqual(code, 0)
            self.assertTrue(trash.exists())

    def test_confirmed_purge_deletes_only_the_manifest_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash = self._trash(root)
            sibling = root / "audit" / "reset-trash" / ("b" * 64)
            sibling.mkdir(parents=True)
            code = reparse_corpus.purge_reset_trash(
                self.MANIFEST_SHA,
                data_root=root,
                status_exit_code=0,
                confirmed=True,
            )
            self.assertEqual(code, 0)
            self.assertFalse(trash.exists())
            self.assertTrue(sibling.exists())
            again = reparse_corpus.purge_reset_trash(
                self.MANIFEST_SHA,
                data_root=root,
                status_exit_code=0,
                confirmed=True,
            )
            self.assertEqual(again, 0)

    def test_force_overrides_the_gate_but_still_deletes_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trash = self._trash(root)
            code = reparse_corpus.purge_reset_trash(
                self.MANIFEST_SHA,
                data_root=root,
                status_exit_code=75,
                confirmed=True,
                forced=True,
            )
            self.assertEqual(code, 0)
            self.assertFalse(trash.exists())

    def test_symlinked_trash_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "elsewhere"
            real.mkdir()
            (root / "audit" / "reset-trash").mkdir(parents=True)
            (root / "audit" / "reset-trash" / ("a" * 64)).symlink_to(real)
            with self.assertRaisesRegex(ManifestError, "symlink"):
                reparse_corpus.purge_reset_trash(
                    self.MANIFEST_SHA,
                    data_root=root,
                    status_exit_code=0,
                    confirmed=True,
                )


if __name__ == "__main__":
    unittest.main()
