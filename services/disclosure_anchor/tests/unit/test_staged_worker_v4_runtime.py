from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import sqlalchemy as sa

from disclosure_anchor.adapters.runtime.staged_worker_v4 import (
    build_staged_worker_v4_runtime,
    _require_execution_composition,
    configured_mineru_capacity,
)
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.contracts.mineru_process_profile import (
    encode_mineru_process_profile,
)
from disclosure_anchor.cli.worker import worker_database_pool_budget
from disclosure_anchor.settings import load_settings
from tests.unit._codex_model_catalog_fixture import prepare_catalog_sha256
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_settings import _env, _mineru_topology
from tests._f5_upgrade_q0_fixture import synthetic_parent_q0
from tests._f5_upgrade_u01_fixture import build_u01, synthetic_member
from tests.unit.test_staged_coordinator_persistence_v4 import _Factory, _Repository, _prepared_authority


class StagedWorkerV4RuntimeTests(unittest.TestCase):
    def _private_file(self, path: Path, payload: bytes) -> None:
        path.write_bytes(payload)
        path.chmod(0o600)

    def test_real_default_off_composition_builds_without_db_or_network(self) -> None:
        self._assert_real_composition(drain_existing=False)

    def test_drain_composition_keeps_prepared_admission_and_submission(self) -> None:
        self._assert_real_composition(drain_existing=True)

    def _assert_real_composition(self, *, drain_existing: bool) -> None:
        # The production loader rejects every symlinked ancestor. macOS maps
        # /var to /private/var, so use the canonical temporary root here.
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp:
            root = Path(tmp)
            profile = replace(
                _profile(),
                api_task_slots=1,
                api_max_pending_tasks=1,
                registry_nonterminal_cap=1,
                registry_terminal_cap=127,
                processing_window_size=16,
                cpu_worker_threads=3,
                omp_thread_count=1,
                raster_stage_slots=1,
                layout_stage_slots=1,
                postprocess_stage_slots=1,
                native_owner_slots=1,
                requested_hybrid_batch_ratio=1,
                effective_hybrid_batch_ratio=1,
                finalizer_slots=1,
                gpu_request_slots=7,
            )
            profile_path = root / "process-profile.json"
            keyring_path = root / "provider-keyring.json"
            self._private_file(profile_path, encode_mineru_process_profile(profile))
            self._private_file(
                keyring_path,
                json.dumps(
                    {
                        "format": "disclosure-v4-secret-keyring.v1",
                        "primary_kek_id": "kek-primary",
                        "keks": {"kek-primary": "11" * 32},
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            environment = {
                **_env(root),
                **_mineru_topology(),
                "DISCLOSURE_SEMANTIC_CODEX_MODEL_CATALOG_SHA256": prepare_catalog_sha256(
                    Path(_env(root)["DISCLOSURE_RUNTIME_ROOT"])
                ),
                "WORKER_PARSE_EXECUTION_MODE": "staged-v4",
                "WORKER_DRAIN_EXISTING": "true" if drain_existing else "false",
                "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256": (
                    profile.runtime_bundle_identity_sha256
                ),
                "DISCLOSURE_V4_PROCESS_PROFILE_FILE": str(profile_path),
                "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": profile.sha256,
                "DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT": "8192",
                "DISCLOSURE_V4_SECRET_KEYRING_FILE": str(keyring_path),
                "DISCLOSURE_V4_PROVIDER_POLL_MILLISECONDS": "1500",
                "DISCLOSURE_V4_ADMISSION_PROBE_MILLISECONDS": "2000",
            }
            engine = sa.create_engine("sqlite+pysqlite:///:memory:")
            try:
                with mock.patch.dict(os.environ, environment, clear=True):
                    settings = load_settings()
                    FileStorePathBuilder(settings).data_path(Path()).mkdir(parents=True)
                    pool_budget = worker_database_pool_budget(settings)
                    runtime = build_staged_worker_v4_runtime(
                        settings=settings,
                        engine=engine,
                        ownership_guard=lambda: None,
                        admission_guard=lambda: None,
                        process_scope_classes=("annual_report",),
                        progress=lambda _snapshot: None,
                        owner_identity="staged-v4-test-boot",
                    )
                try:
                    backend = runtime.coordinator._backend
                    self.assertTrue(runtime.remote._allow_task_submission)
                    if drain_existing:
                        self.assertIs(backend._new_work_admitter, backend._persistence)
                        self.assertIsNone(runtime.coordinator._admission_observer)
                        # Exercise the actually composed backend boundary: no ordinary
                        # source scan/observation/rejection/H0, and no outcome laundering.
                        from disclosure_anchor.application.services.staged_parse_coordinator import AdmissionOutcome
                        from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
                        outcome = AdmissionOutcome(work=(), backlog_exists=True,
                                                   blocked_dimensions=("snapshot_bytes",))
                        with mock.patch.object(backend._persistence, "admit_new", return_value=outcome) as prepared:
                            self.assertIs(backend.admit_new(limit=2, available_credits=ResourceCreditVector()), outcome)
                        prepared.assert_called_once_with(limit=2, available_credits=ResourceCreditVector())
                        large = _prepared_authority("attempt-a-large", snapshot_bytes=200,
                                                    database_now=datetime(2026, 9, 4, tzinfo=UTC))
                        small = _prepared_authority("attempt-b-small", snapshot_bytes=50,
                                                    database_now=datetime(2026, 9, 4, tzinfo=UTC))
                        repository = _Repository((large, small))
                        backend._persistence._uow_factory = _Factory(repository)
                        constrained = replace(runtime.coordinator._limits.credits, snapshot_bytes=100)
                        outcome = backend.admit_new(limit=2, available_credits=constrained)
                        self.assertEqual(tuple(w.attempt_id for w in outcome.work), (small.attempt_id,))
                        self.assertIn("snapshot_bytes", outcome.blocked_dimensions)
                        self.assertTrue(outcome.backlog_exists)
                        claimed = repository.heads[small.attempt_id]
                        self.assertEqual((claimed.client_submit_key, claimed.request_sha256,
                                          claimed.checkpoint.sha256, claimed.source_pdf_sha256),
                                         (small.client_submit_key, small.request_sha256,
                                          small.checkpoint.sha256, small.source_pdf_sha256))
                        self.assertEqual(repository.heads[large.attempt_id], large)
                        outcome = backend.admit_new(limit=1, available_credits=runtime.coordinator._limits.credits)
                        self.assertEqual(tuple(w.attempt_id for w in outcome.work), (large.attempt_id,))
                        self.assertEqual(repository.recovery_list_calls, 0)
                    else:
                        self.assertIsNot(backend._new_work_admitter, backend._persistence)
                        self.assertIs(runtime.coordinator._admission_observer, backend._new_work_admitter)
                    self.assertEqual(runtime.owner_identity, "staged-v4-test-boot")
                    self.assertRegex(runtime.worker_profile_sha256, r"^sha256:[0-9a-f]{64}$")
                    limits = runtime.coordinator._limits
                    self.assertEqual(limits.admission_probe_seconds, 2.0)
                    self.assertEqual(runtime.coordinator._backend._poll_seconds, 1.5)
                    self.assertNotEqual(runtime.coordinator._backend._poll_seconds, limits.poll_seconds)
                    self.assertEqual(
                        limits.credits.documents,
                        profile.registry_nonterminal_cap
                        + profile.registry_terminal_cap,
                    )
                    self.assertEqual(
                        limits.credits.provider_result_bytes,
                        profile.max_unacked_result_bytes,
                    )
                    self.assertEqual(pool_budget.pool_size, 16)
                    self.assertEqual(pool_budget.max_overflow, 2)
                    self.assertTrue(
                        (
                            root
                            / "services"
                            / "disclosure_anchor"
                            / "runtime"
                            / "staged_v4"
                            / "scratch"
                        ).is_dir()
                    )
                finally:
                    runtime.close()
            finally:
                engine.dispose()

    def test_drain_keeps_verified_upgrade_compulsory_and_unscoped(self) -> None:
        parent = synthetic_parent_q0(self)
        bundle = build_u01(parent, members=[synthetic_member(parent, 0)])
        evidence = bundle.verify()
        execution = evidence.execution
        self.assertIsNotNone(execution)
        settings = bundle.settings.model_copy(update={"worker_drain_existing": True})
        kwargs = dict(process_profile_sha256=bundle.process_profile.sha256,
                      worker_profile_sha256=bundle.worker_profile.sha256,
                      runtime_identity_sha256=bundle.current_runtime,
                      expected_capacity=configured_mineru_capacity(settings, None), scoped=False)
        _require_execution_composition(settings, execution, **kwargs)
        with self.assertRaisesRegex(ValueError, "cannot carry its verified context"):
            _require_execution_composition(settings, None, **kwargs)
        with self.assertRaisesRegex(ValueError, "unscoped resident"):
            _require_execution_composition(settings, execution, **dict(kwargs, scoped=True))
        with self.assertRaisesRegex(ValueError, "differs from the verified"):
            _require_execution_composition(settings, execution,
                                           **dict(kwargs, worker_profile_sha256="sha256:" + "f" * 64))

    def test_drain_refuses_scoped_composition_before_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            environment = {**_env(Path(tmp)), "WORKER_PARSE_EXECUTION_MODE": "staged-v4",
                           "WORKER_DRAIN_EXISTING": "true"}
            with mock.patch.dict(os.environ, environment, clear=True):
                settings = load_settings()
            for recovery_only in (False, True):
                with (self.subTest(recovery_only=recovery_only),
                      mock.patch("disclosure_anchor.adapters.runtime.staged_worker_v4.load_staged_v4_settings") as loader,
                      mock.patch("disclosure_anchor.adapters.runtime.staged_worker_v4.require_commissioning_recovery_scope") as scope,
                      self.assertRaisesRegex(ValueError, "unscoped resident")):
                    build_staged_worker_v4_runtime(
                        settings=settings, engine=mock.MagicMock(), ownership_guard=lambda: None,
                        admission_guard=lambda: None, process_scope_classes=None, progress=lambda _s: None,
                        admission_document_ids=("doc-existing",), recovery_only=recovery_only,
                    )
                loader.assert_not_called()
                scope.assert_not_called()

    def test_legacy_mode_rejects_before_staged_config_or_scratch(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp:
            root = Path(tmp)
            environment = {
                **_env(root),
                **_mineru_topology(),
                "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": "invalid",
            }
            engine = sa.create_engine("sqlite+pysqlite:///:memory:")
            try:
                with (
                    mock.patch.dict(os.environ, environment, clear=True),
                    mock.patch(
                        "disclosure_anchor.adapters.runtime.staged_worker_v4."
                        "load_staged_v4_settings"
                    ) as staged_loader,
                    self.assertRaisesRegex(ValueError, "explicit staged-v4"),
                ):
                    build_staged_worker_v4_runtime(
                        settings=load_settings(),
                        engine=engine,
                        ownership_guard=lambda: None,
                        admission_guard=lambda: None,
                        process_scope_classes=None,
                        progress=lambda _snapshot: None,
                    )
                staged_loader.assert_not_called()
                self.assertFalse(
                    (
                        root
                        / "services"
                        / "disclosure_anchor"
                        / "runtime"
                        / "staged_v4"
                    ).exists()
                )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
