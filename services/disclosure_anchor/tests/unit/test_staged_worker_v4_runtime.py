from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import sqlalchemy as sa

from disclosure_anchor.adapters.runtime.staged_worker_v4 import (
    build_staged_worker_v4_runtime,
)
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.contracts.mineru_process_profile import (
    encode_mineru_process_profile,
)
from disclosure_anchor.cli.worker import worker_database_pool_budget
from disclosure_anchor.settings import load_settings
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_settings import _env, _mineru_topology


class StagedWorkerV4RuntimeTests(unittest.TestCase):
    def _private_file(self, path: Path, payload: bytes) -> None:
        path.write_bytes(payload)
        path.chmod(0o600)

    def test_real_default_off_composition_builds_without_db_or_network(self) -> None:
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
                "WORKER_PARSE_EXECUTION_MODE": "staged-v4",
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
