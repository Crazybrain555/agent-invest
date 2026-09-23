"""Scratch isolation from an inherited staged N12 serving configuration.

All paths, identities and credentials below are synthetic. These tests exercise
the actual environment builder and settings validators without provisioning a
database, reading runtime configuration files or contacting a parser.
"""

from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest import mock

from disclosure_anchor.settings import Settings, load_settings, load_staged_v4_settings
from tests.integration._runner import ScratchIntegrationDatabase
from tests.integration._support import DATABASE_ENV_KEYS


_BASE_URL = "postgresql+psycopg://placeholder:placeholder@localhost:55432/postgres"
_PRODUCTION_URL = (
    "postgresql+psycopg://placeholder:placeholder@localhost:55432/invest_engine"
)
_PRODUCTION_ROOT = Path("/synthetic-serving-root")


def _staged_environment() -> dict[str, str]:
    root = _PRODUCTION_ROOT
    return {
        **dict.fromkeys(DATABASE_ENV_KEYS, _PRODUCTION_URL),
        "DISCLOSURE_DATA_ROOT": str(root / "services/disclosure_anchor"),
        "DISCLOSURE_RUNTIME_ROOT": str(root / "services/disclosure_anchor/runtime"),
        "DISCLOSURE_SHARED_ROOT": str(root / "shared"),
        "MINERU_MODEL_CACHE": str(root / "shared/model_cache/mineru"),
        "HF_HOME": str(root / "shared/model_cache/huggingface"),
        "MODELSCOPE_CACHE": str(root / "shared/model_cache/modelscope"),
        "DISCLOSURE_MINERU_BIN": str(root / "bin/mineru"),
        "DISCLOSURE_MINERU_BACKEND": "hybrid-http-client",
        "DISCLOSURE_MINERU_API_URL": "http://127.0.0.1:30003",
        "DISCLOSURE_MINERU_OBSERVABILITY_URL": "http://127.0.0.1:30001/v1",
        "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL": (
            "http://mineru-openai-server:30000/v1"
        ),
        "DISCLOSURE_GPU_METRICS_URL": "http://127.0.0.1:30004/metrics",
        "DISCLOSURE_DCGM_METRICS_URL": "http://127.0.0.1:30004/metrics",
        "DISCLOSURE_GPU_EXPECTED_UUID": "GPU-11111111-2222-3333-4444-555555555555",
        "DISCLOSURE_MINERU_CAPACITY_CONFIG": str(root / "capacity-config.json"),
        "DISCLOSURE_MINERU_CAPACITY_CONFIG_SHA256": "sha256:" + "a" * 64,
        "DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG": str(root / "stream-pressure.json"),
        "DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG_SHA256": "sha256:" + "b" * 64,
        "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256": "sha256:" + "c" * 64,
        "DISCLOSURE_MINERU_API_TASK_SLOTS": "12",
        "DISCLOSURE_MINERU_API_INFERENCE_CONCURRENCY": "14",
        "MINERU_PROCESSING_WINDOW_SIZE": "16",
        "WORKER_PARSE_EXECUTION_MODE": "staged-v4",
        "WORKER_PARSE_CONCURRENCY": "16",
        "WORKER_FINALIZE_CONCURRENCY": "2",
        "WORKER_GPU_REQUEST_BUDGET": "14",
        "WORKER_GPU_MAX_SEQUENCES": "128",
        "WORKER_MINERU_CLIENT_OUTSTANDING_WINDOW": "12",
        "DISCLOSURE_V4_PROCESS_PROFILE_FILE": str(root / "process-profile.json"),
        "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": "sha256:" + "d" * 64,
        "DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT": "8192",
        "DISCLOSURE_V4_SECRET_KEYRING_FILE": str(root / "placeholder-keyring.json"),
        # Unrelated business limits must survive serving-environment isolation.
        "WORKER_BATCH_PARSE": "37",
    }


class IntegrationRunnerStagedEnvironmentIndependentTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = mock.MagicMock()
        engine.connect.side_effect = AssertionError("environment construction must not connect")
        patcher = mock.patch(
            "scripts.managed_scratch_database.sqlalchemy.create_engine",
            return_value=engine,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(engine.connect.assert_not_called)

    def _scratch(self, *, real_mineru: bool = False) -> ScratchIntegrationDatabase:
        scratch = ScratchIntegrationDatabase(_BASE_URL, real_mineru=real_mineru)
        self.addCleanup(scratch.close)
        return scratch

    def _settings(self, environment: dict[str, str]) -> Settings:
        with mock.patch.dict(os.environ, environment, clear=True):
            return load_settings()

    def _assert_pinned_database(
        self, environment: dict[str, str], settings: Settings, scratch: ScratchIntegrationDatabase
    ) -> None:
        self.assertEqual(
            {key for key in environment if key.upper() in DATABASE_ENV_KEYS},
            set(DATABASE_ENV_KEYS),
        )
        for key in DATABASE_ENV_KEYS:
            self.assertEqual(environment[key], scratch.database_url, key)
        for value in (
            settings.database_url,
            settings.disclosure_admin_database_url,
            settings.disclosure_reader_database_url,
            settings.disclosure_migration_database_url,
        ):
            self.assertIsNotNone(value)
            assert value is not None
            self.assertEqual(value.get_secret_value(), scratch.database_url)
        self.assertNotEqual(scratch.database_url, _PRODUCTION_URL)

    def _assert_scratch_roots(
        self, settings: Settings, scratch: ScratchIntegrationDatabase
    ) -> None:
        root = Path(scratch._roots.name)
        self.assertEqual(settings.disclosure_data_root, root / "services/disclosure_anchor")
        self.assertEqual(settings.disclosure_runtime_root, root / "services/disclosure_anchor/runtime")
        self.assertEqual(settings.disclosure_shared_root, root / "shared")

    def test_n12_parent_is_valid_before_default_child_disables_serving(self) -> None:
        inherited = _staged_environment()
        parent = self._settings(inherited)
        self.assertEqual(parent.worker_parse_execution_mode, "staged-v4")
        self.assertEqual(parent.disclosure_mineru_api_task_slots, 12)
        self.assertEqual(parent.mineru_effective_inference_request_upper_bound, 14)
        scratch = self._scratch()
        with mock.patch.dict(os.environ, inherited, clear=True):
            environment = scratch._test_environment()
            self.assertEqual(dict(os.environ), inherited)

        settings = self._settings(environment)
        self._assert_pinned_database(environment, settings, scratch)
        self._assert_scratch_roots(settings, scratch)
        for value in (
            settings.disclosure_mineru_api_url,
            settings.disclosure_mineru_observability_url,
            settings.disclosure_mineru_inference_upstream_url,
            settings.disclosure_gpu_metrics_url,
            settings.disclosure_dcgm_metrics_url,
            settings.disclosure_mineru_capacity_config,
            settings.disclosure_mineru_capacity_config_sha256,
            settings.disclosure_mineru_stream_pressure_config,
            settings.disclosure_mineru_stream_pressure_config_sha256,
        ):
            self.assertIsNone(value)
        self.assertEqual(settings.worker_parse_execution_mode, "legacy-sync")
        self.assertEqual(settings.worker_parse_concurrency, 1)
        self.assertEqual(settings.worker_batch_parse, 37)
        binary = settings.disclosure_mineru_bin
        self.assertIsNotNone(binary)
        assert binary is not None
        self.assertTrue(binary.is_relative_to(Path(scratch._roots.name)))
        self.assertFalse(binary.exists())
        for cache in settings.model_cache_paths:
            self.assertTrue(cache.is_relative_to(settings.disclosure_shared_root))

    def test_case_insensitive_serving_aliases_cannot_escape_default_isolation(self) -> None:
        inherited = {key.lower(): value for key, value in _staged_environment().items()}
        # Colliding DB spellings must still yield exactly one canonical scratch value.
        inherited["DATABASE_URL"] = _PRODUCTION_URL
        inherited["Disclosure_Migration_Database_Url"] = _PRODUCTION_URL
        self.assertEqual(self._settings(inherited).disclosure_mineru_api_task_slots, 12)
        scratch = self._scratch()
        with mock.patch.dict(os.environ, inherited, clear=True):
            environment = scratch._test_environment()
        settings = self._settings(environment)
        self._assert_pinned_database(environment, settings, scratch)
        self._assert_scratch_roots(settings, scratch)
        self.assertIsNone(settings.disclosure_mineru_api_url)
        self.assertIsNone(settings.disclosure_gpu_metrics_url)
        self.assertIsNone(settings.disclosure_dcgm_metrics_url)
        self.assertIsNone(settings.disclosure_mineru_stream_pressure_config)
        self.assertIsNone(settings.disclosure_mineru_capacity_config)
        self.assertEqual(settings.worker_parse_execution_mode, "legacy-sync")
        self.assertEqual(settings.worker_batch_parse, 37)
        self.assertIsNotNone(settings.disclosure_mineru_bin)
        assert settings.disclosure_mineru_bin is not None
        self.assertTrue(settings.disclosure_mineru_bin.is_relative_to(Path(scratch._roots.name)))
        self.assertFalse(settings.disclosure_mineru_bin.exists())

    def test_real_mineru_opt_in_preserves_n12_serving_but_never_parent_database(self) -> None:
        inherited = _staged_environment()
        inherited["database_url"] = _PRODUCTION_URL
        inherited["disclosure_migration_database_url"] = _PRODUCTION_URL
        scratch = self._scratch(real_mineru=True)
        with mock.patch.dict(os.environ, inherited, clear=True):
            environment = scratch._test_environment()
        settings = self._settings(environment)
        self._assert_pinned_database(environment, settings, scratch)
        self._assert_scratch_roots(settings, scratch)
        overridden = set(DATABASE_ENV_KEYS) | {
            "DISCLOSURE_DATA_ROOT", "DISCLOSURE_RUNTIME_ROOT", "DISCLOSURE_SHARED_ROOT"
        }
        for key, value in inherited.items():
            if key.upper() not in overridden:
                self.assertEqual(environment[key], value, key)
        self.assertEqual(settings.worker_parse_execution_mode, "staged-v4")
        self.assertEqual(settings.disclosure_mineru_api_task_slots, 12)
        self.assertEqual(settings.worker_parse_concurrency, 16)
        self.assertEqual(settings.worker_finalize_concurrency, 2)
        self.assertEqual(settings.worker_gpu_request_budget, 14)
        self.assertEqual(settings.worker_mineru_client_outstanding_window, 12)
        with mock.patch.dict(os.environ, environment, clear=True):
            staged = load_staged_v4_settings()
        self.assertEqual(staged.process_profile_file, _PRODUCTION_ROOT / "process-profile.json")
        self.assertEqual(staged.process_profile_sha256, "sha256:" + "d" * 64)
        self.assertEqual(staged.archive_member_count_limit, 8192)


if __name__ == "__main__":
    unittest.main()
