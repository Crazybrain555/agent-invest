"""Selective corpus-reset transaction against the disposable integration DB."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from scripts.corpus_reparse_manifest import (
    MANIFEST_SCHEMA,
    CorpusManifest,
    canonical_hash,
    document_input_identity,
    document_source_rows,
    load_manifest,
    write_manifest,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    CURRENT_NORMALIZED_IR_VERSION,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from scripts.corpus_reset_backup import (
    database_identity,
    verify_backup_bundle,
)
from scripts.corpus_reset_quiescence import worker_singleton_lock
from scripts.corpus_reset_state import (
    detect_reset_state,
    manifest_postgres_state,
    postgres_state,
    processing_run_rows,
    reset_transaction,
)
from scripts import corpus_reset_state as reset_state_module
from scripts.create_corpus_reset_backup import create_backup
from scripts.prove_corpus_reset_backup import prove_backup_restore
from scripts.reset_derived_corpus import (
    _artifact_inventory,
    _run_locked_reset,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from disclosure_anchor.application.worker.locks import (
    CorpusWriteBusyError,
    exclusive_corpus_mutation,
)
from disclosure_anchor.adapters.db.postgres.schema import APP_ROLE
from tests.integration._support import engine_or_skip


class CorpusResetIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.addCleanup(self.engine.dispose)
        self.suffix = os.urandom(5).hex()
        self.document_id = f"doc_reset_{self.suffix}"
        self.run_id = f"run_reset_{self.suffix}"
        self.asset_id = f"du_reset_{self.suffix}"
        self.company_id = f"company_reset_{self.suffix}"
        self.security_id = f"security_reset_{self.suffix}"
        self.tracked_company_id = f"tracked_reset_{self.suffix}"
        self.security_code = f"3{int(self.suffix[:8], 16) % 100_000:05d}"
        self.raw_payload = b"%PDF-1.4 reset integration\n%%EOF\n"
        self.raw_hash = "sha256:" + hashlib.sha256(self.raw_payload).hexdigest()
        data_root_env = os.environ.get("DISCLOSURE_DATA_ROOT")
        if not data_root_env:
            self.skipTest("DISCLOSURE_DATA_ROOT is required")
        self.data_root = Path(data_root_env)
        self.raw_relpath = f"raw_documents/local/reset/{self.suffix}/document.pdf"
        raw_path = self.data_root / "data" / self.raw_relpath
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(self.raw_payload)
        self.parser_relpath = f"parser_artifacts/local/reset/{self.run_id}"
        self.ir_relpath = f"derived/normalized_ir/reset/{self.run_id}.json"
        self.units_relpath = (
            f"derived/document_unit_snapshots/reset/{self.run_id}.jsonl"
        )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO disclosure_core.company
                      (company_id, legal_name)
                    VALUES (:company_id, :legal_name)
                    """
                ),
                {
                    "company_id": self.company_id,
                    "legal_name": f"Reset Integration {self.suffix}",
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO disclosure_core.security
                      (security_id, company_id, security_code, exchange,
                       board, status)
                    VALUES
                      (:security_id, :company_id, :security_code, 'SZSE',
                       'main', 'listed')
                    """
                ),
                {
                    "security_id": self.security_id,
                    "company_id": self.company_id,
                    "security_code": self.security_code,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO disclosure_core.tracked_company
                      (tracked_company_id, company_id, security_id, status,
                       lookback, process_classes, sync_frequency)
                    VALUES
                      (:tracked_company_id, :company_id, :security_id,
                       'active', '{"years": 10}'::jsonb,
                       '["periodic_report"]'::jsonb, 'daily')
                    """
                ),
                {
                    "tracked_company_id": self.tracked_company_id,
                    "company_id": self.company_id,
                    "security_id": self.security_id,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO disclosure_core.document
                      (document_id, company_id, security_id,
                       provider, provider_document_id, title,
                       announcement_date, report_period,
                       raw_file_relpath, raw_file_hash, status,
                       current_processing_run_id, provider_metadata,
                       class_filing_type, class_market, class_rules_version,
                       class_disclosure_topics, class_publisher_categories,
                       class_content_categories)
                    VALUES
                      (:document_id, :company_id, :security_id,
                       'local', :provider_document_id, 'Reset test filing',
                       DATE '2026-07-27', '2025',
                       :raw_relpath, :raw_hash, 'published', :run_id,
                       '{}'::jsonb, 'annual_report', 'cn_a', 'class-test',
                       '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)
                    """
                ),
                {
                    "document_id": self.document_id,
                    "company_id": self.company_id,
                    "security_id": self.security_id,
                    "provider_document_id": self.suffix,
                    "raw_relpath": self.raw_relpath,
                    "raw_hash": self.raw_hash,
                    "run_id": self.run_id,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO disclosure_core.processing_run
                      (processing_run_id, document_id,
                       artifact_owner_processing_run_id, run_kind, status,
                       is_active, unit_build_status, input_raw_file_hash,
                       parser_artifact_relpath, normalized_ir_relpath,
                       document_units_relpath)
                    VALUES
                      (:run_id, :document_id, :run_id, 'parse', 'succeeded', true,
                       'succeeded', :raw_hash, :parser_relpath, :ir_relpath,
                       :units_relpath)
                    """
                ),
                {
                    "run_id": self.run_id,
                    "document_id": self.document_id,
                    "raw_hash": self.raw_hash,
                    "parser_relpath": self.parser_relpath,
                    "ir_relpath": self.ir_relpath,
                    "units_relpath": self.units_relpath,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO disclosure_core.document_unit
                      (asset_id, document_id, processing_run_id, payload_kind,
                       order_index, payload, content_hash)
                    VALUES
                      (:asset_id, :document_id, :run_id, 'text', 0,
                       '{}'::jsonb, :content_hash)
                    """
                ),
                {
                    "asset_id": self.asset_id,
                    "document_id": self.document_id,
                    "run_id": self.run_id,
                    "content_hash": self.raw_hash,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO disclosure_core.unit_search_projection
                      (asset_id, retrieval_rules_version, title_text,
                       heading_path_text, title_tokens, path_tokens,
                       body_tokens, key_tokens, built_at)
                    VALUES
                      (:asset_id, 'test', '', '', '', '', '', '', now())
                    """
                ),
                {"asset_id": self.asset_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO disclosure_core.unit_search_atom
                      (asset_id, atom_index, atom_text)
                    VALUES (:asset_id, 0, '重置原子')
                    """
                ),
                {"asset_id": self.asset_id},
            )
            for event_id, event_kind, subject_kind, subject_ref, run_id, asset_id in (
                (
                    f"oe_source_{self.suffix}",
                    "document_registered",
                    "document",
                    self.document_id,
                    None,
                    None,
                ),
                (
                    f"oe_run_{self.suffix}",
                    "processing_run_created",
                    "processing_run",
                    self.run_id,
                    self.run_id,
                    None,
                ),
                (
                    f"oe_unit_{self.suffix}",
                    "document_unit_created",
                    "document_unit",
                    self.asset_id,
                    self.run_id,
                    self.asset_id,
                ),
            ):
                connection.execute(
                    text(
                        """
                        INSERT INTO disclosure_ops.outbox_event
                          (event_id, event_kind, change_kind, subject_kind,
                           subject_ref, document_id, processing_run_id, asset_id)
                        VALUES
                          (:event_id, :event_kind, 'materialized',
                           :subject_kind, :subject_ref, :document_id,
                           :run_id, :asset_id)
                        """
                    ),
                    {
                        "event_id": event_id,
                        "event_kind": event_kind,
                        "subject_kind": subject_kind,
                        "subject_ref": subject_ref,
                        "document_id": self.document_id,
                        "run_id": run_id,
                        "asset_id": asset_id,
                    },
                )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM disclosure_ops.outbox_event "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": self.document_id},
            )
            connection.execute(
                text(
                    "DELETE FROM disclosure_core.unit_search_projection "
                    "WHERE asset_id = :asset_id"
                ),
                {"asset_id": self.asset_id},
            )
            connection.execute(
                text(
                    "DELETE FROM disclosure_core.document_unit "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": self.document_id},
            )
            connection.execute(
                text(
                    "DELETE FROM disclosure_core.processing_run "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": self.document_id},
            )
            connection.execute(
                text(
                    "DELETE FROM disclosure_core.document "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": self.document_id},
            )
            connection.execute(
                text(
                    "DELETE FROM disclosure_core.tracked_company "
                    "WHERE tracked_company_id = :tracked_company_id"
                ),
                {"tracked_company_id": self.tracked_company_id},
            )
            connection.execute(
                text(
                    "DELETE FROM disclosure_core.security "
                    "WHERE security_id = :security_id"
                ),
                {"security_id": self.security_id},
            )
            connection.execute(
                text(
                    "DELETE FROM disclosure_core.company WHERE company_id = :company_id"
                ),
                {"company_id": self.company_id},
            )

    def _manifest(self, directory: Path) -> CorpusManifest:
        with self.engine.connect() as connection:
            runs = processing_run_rows(connection)
            document_count = int(
                connection.execute(
                    text("SELECT count(*) FROM disclosure_core.document")
                ).scalar_one()
            )
            source_rows = document_source_rows(connection)
            frozen_postgres_state = postgres_state(connection)
        self.assertEqual(document_count, 1)
        self.assertEqual(len(source_rows), 1)
        source_row = source_rows[0]
        input_identity = document_input_identity(source_row)
        path = directory / "reset.jsonl"
        code_snapshot: dict[str, object] = {
            "git_head": "0" * 40,
            "scope": "services/disclosure_anchor",
            "tracked_diff_sha256": "sha256:" + "1" * 64,
            "untracked_inventory_sha256": "sha256:" + "2" * 64,
            "untracked_file_count": 0,
        }
        write_manifest(
            path,
            header={
                "manifest_schema": MANIFEST_SCHEMA,
                "generated_at": "2026-07-27T00:00:00+00:00",
                "document_count": 1,
                "processing_run_count": len(runs),
                "postgres_state": frozen_postgres_state,
                "target_identity": {
                    "parser_target": ParserTargetIdentity(
                        name="FakeParser",
                        package_version="1.0",
                        backend="pipeline",
                        method="auto",
                        language="ch",
                        formula=False,
                        table=True,
                        runtime_bundle_identity_sha256="sha256:" + "b" * 64,
                    ).to_payload(),
                    "max_parse_retries": 3,
                    "max_build_retries": 3,
                    "builder_rules_version": "ub-test",
                    "retrieval_rules_version": "rp-test",
                    "normalized_ir_contract_version": (CURRENT_NORMALIZED_IR_VERSION),
                },
                "code_snapshot": code_snapshot,
            },
            documents=[
                {
                    "document_id": self.document_id,
                    "raw_file_relpath": self.raw_relpath,
                    "raw_file_hash": self.raw_hash,
                    "old_status": "published",
                    "old_current_processing_run_id": self.run_id,
                    "input_identity_sha256": canonical_hash(input_identity),
                }
            ],
            runs=runs,
        )
        return load_manifest(
            path,
            data_root=self.data_root,
            verify_raw_files=True,
        )

    def test_transaction_rolls_back_then_selectively_resets_derived_state(
        self,
    ) -> None:
        with self.engine.connect() as connection, connection.begin():
            connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_ROLE}"')
            app_database_identity = database_identity(connection)
            app_can_read_version, app_can_truncate_units = connection.execute(
                text(
                    "SELECT "
                    "has_table_privilege(current_user, "
                    "'disclosure_ops.alembic_version', 'SELECT'), "
                    "has_table_privilege(current_user, "
                    "'disclosure_core.document_unit', 'TRUNCATE')"
                )
            ).one()
        self.assertFalse(app_can_read_version)
        self.assertFalse(app_can_truncate_units)
        self.assertGreater(app_database_identity["database_oid"], 0)

        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._manifest(Path(tmp))
            locks_held = threading.Event()
            release_reset = threading.Event()
            reset_errors: list[BaseException] = []
            real_detect_reset_state = reset_state_module.detect_reset_state

            def pause_after_reset_locks(
                connection: object,
                candidate_manifest: CorpusManifest,
            ) -> str:
                locks_held.set()
                if not release_reset.wait(timeout=5):
                    raise TimeoutError("test did not release reset locks")
                return real_detect_reset_state(
                    connection,  # type: ignore[arg-type]
                    candidate_manifest,
                )

            def run_locked_rollback() -> None:
                try:
                    with self.engine.connect() as reset_connection:
                        reset_transaction(
                            reset_connection,
                            manifest,
                            commit=False,
                        )
                except BaseException as exc:
                    reset_errors.append(exc)

            with patch(
                "scripts.corpus_reset_state.detect_reset_state",
                side_effect=pause_after_reset_locks,
            ):
                reset_thread = threading.Thread(target=run_locked_rollback)
                reset_thread.start()
                self.assertTrue(locks_held.wait(timeout=5))
                try:
                    for relation in (
                        "disclosure_core.provider_category",
                        "disclosure_core.unit_body_search_window",
                        "disclosure_core.unit_search_atom",
                        "disclosure_ops.alembic_version",
                    ):
                        with self.subTest(relation=relation):
                            with self.engine.connect() as writer:
                                transaction = writer.begin()
                                writer.exec_driver_sql(
                                    "SET LOCAL lock_timeout = '100ms'"
                                )
                                with self.assertRaises(DBAPIError):
                                    writer.exec_driver_sql(
                                        f"LOCK TABLE {relation} IN ROW EXCLUSIVE MODE"
                                    )
                                transaction.rollback()
                    with self.engine.connect() as observer:
                        sequence_before = tuple(
                            observer.execute(
                                text(
                                    "SELECT last_value, is_called "
                                    "FROM disclosure_ops.outbox_event_seq_seq"
                                )
                            ).one()
                        )
                    with self.engine.connect() as writer:
                        transaction = writer.begin()
                        writer.exec_driver_sql("SET LOCAL lock_timeout = '100ms'")
                        with self.assertRaises(DBAPIError):
                            writer.execute(
                                text(
                                    """
                                    INSERT INTO disclosure_ops.outbox_event
                                      (event_id, event_kind, change_kind,
                                       subject_kind, subject_ref)
                                    VALUES
                                      (:event_id, 'document_registered',
                                       'materialized', 'document', :event_id)
                                    """
                                ),
                                {"event_id": f"blocked_{self.suffix}"},
                            )
                        transaction.rollback()
                    with self.engine.connect() as observer:
                        sequence_after = tuple(
                            observer.execute(
                                text(
                                    "SELECT last_value, is_called "
                                    "FROM disclosure_ops.outbox_event_seq_seq"
                                )
                            ).one()
                        )
                    self.assertEqual(sequence_after, sequence_before)
                finally:
                    release_reset.set()
                    reset_thread.join(timeout=5)
            self.assertFalse(reset_thread.is_alive())
            self.assertEqual(reset_errors, [])

            with exclusive_corpus_mutation(self.engine):
                with self.assertRaises(CorpusWriteBusyError):
                    with exclusive_corpus_mutation(self.engine):
                        self.fail("second destructive owner acquired the lock")
            with SqlAlchemyUnitOfWork(engine=self.engine):
                with self.assertRaises(CorpusWriteBusyError):
                    with exclusive_corpus_mutation(self.engine):
                        self.fail("maintenance acquired while a service UoW was active")
            with self.engine.connect() as connection:
                reset_transaction(connection, manifest, commit=False)
            with self.engine.connect() as connection:
                self.assertEqual(detect_reset_state(connection, manifest), "pre_reset")

            with self.engine.connect() as connection:
                reset_transaction(connection, manifest, commit=True)
            with self.engine.connect() as connection:
                self.assertEqual(detect_reset_state(connection, manifest), "post_reset")
                self.assertEqual(
                    tuple(
                        connection.execute(
                            text(
                                """
                                SELECT
                                  (SELECT count(*) FROM disclosure_core.unit_search_projection),
                                  (SELECT count(*) FROM disclosure_core.unit_body_search_window),
                                  (SELECT count(*) FROM disclosure_core.unit_search_atom)
                                """
                            )
                        ).one()
                    ),
                    (0, 0, 0),
                )
                document = connection.execute(
                    text(
                        """
                        SELECT status, current_processing_run_id,
                               raw_file_relpath, raw_file_hash
                          FROM disclosure_core.document
                         WHERE document_id = :document_id
                        """
                    ),
                    {"document_id": self.document_id},
                ).one()
                source_events = (
                    connection.execute(
                        text(
                            "SELECT event_kind FROM disclosure_ops.outbox_event "
                            "WHERE document_id = :document_id"
                        ),
                        {"document_id": self.document_id},
                    )
                    .scalars()
                    .all()
                )

        self.assertEqual(
            tuple(document),
            ("registered", None, self.raw_relpath, self.raw_hash),
        )
        self.assertEqual(source_events, ["document_registered"])

    def test_pg_dump_restores_manifest_exact_into_blank_scratch(self) -> None:
        pg_dump = shutil.which("pg_dump")
        pg_restore = shutil.which("pg_restore")
        if pg_dump is None or pg_restore is None:
            self.skipTest("pg_dump and pg_restore are required")
        database_url = self.engine.url.render_as_string(hide_password=False)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._manifest(Path(tmp))
            frozen_scopes = manifest_postgres_state(manifest)["scopes"]
            self.assertIsInstance(frozen_scopes, dict)
            self.assertIn("unit_body_search_window", frozen_scopes)
            self.assertIn("unit_search_atom", frozen_scopes)
            backup = Path(tmp) / "pre-reset.dump"
            with worker_singleton_lock(database_url) as lock_connection:
                create_backup(
                    manifest=manifest,
                    backup=backup,
                    database_url=database_url,
                    pg_dump=Path(pg_dump),
                    engine=self.engine,
                    lock_connection=lock_connection,
                )
                prove_backup_restore(
                    manifest=manifest,
                    backup=backup,
                    pg_restore=Path(pg_restore),
                    scratch_base_url=database_url,
                )
            verified = verify_backup_bundle(
                backup,
                manifest=manifest,
            )
            self.assertEqual(
                verified.proof["restored_postgres_state_sha256"],
                canonical_hash(manifest_postgres_state(manifest)),
            )
            self.assertEqual(
                verified.proof["restored_security_state_sha256"],
                verified.metadata["source_security_state_sha256"],
            )

            parser_file = self.data_root / "data" / self.parser_relpath / "result.json"
            parser_file.parent.mkdir(parents=True)
            parser_file.write_text("parser", encoding="utf-8")
            ir_file = self.data_root / "data" / self.ir_relpath
            ir_file.parent.mkdir(parents=True)
            ir_file.write_text("{}", encoding="utf-8")
            units_file = self.data_root / "data" / self.units_relpath
            units_file.parent.mkdir(parents=True)
            units_file.write_text("{}\n", encoding="utf-8")
            inventory = _artifact_inventory(manifest, self.data_root)
            rehearse_args = argparse.Namespace(
                apply=False,
                rehearse=True,
                backup=backup,
            )
            database_url = self.engine.url.render_as_string(hide_password=False)
            with (
                patch(
                    "scripts.reset_derived_corpus.assert_destructive_services_quiescent"
                ),
                worker_singleton_lock(database_url) as lock_connection,
            ):
                self.assertEqual(
                    _run_locked_reset(
                        args=rehearse_args,
                        data_root=self.data_root,
                        manifest=manifest,
                        backup_verification=verified,
                        inventory=inventory,
                        engine=self.engine,
                        lock_connection=lock_connection,
                    ),
                    0,
                )
            apply_args = argparse.Namespace(
                apply=True,
                rehearse=False,
                backup=backup,
            )
            with (
                patch(
                    "scripts.reset_derived_corpus.assert_destructive_services_quiescent"
                ),
                exclusive_corpus_mutation(self.engine),
                worker_singleton_lock(database_url) as lock_connection,
            ):
                self.assertEqual(
                    _run_locked_reset(
                        args=apply_args,
                        data_root=self.data_root,
                        manifest=manifest,
                        backup_verification=verified,
                        inventory=inventory,
                        engine=self.engine,
                        lock_connection=lock_connection,
                    ),
                    0,
                )
            with self.engine.connect() as connection:
                self.assertEqual(
                    detect_reset_state(connection, manifest),
                    "post_reset",
                )


if __name__ == "__main__":
    unittest.main()
