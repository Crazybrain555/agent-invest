"""A2 explicit startup settings and versioned unknown-value boundary tests."""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from disclosure_anchor.adapters.runtime import mineru_capacity_config as loader
from disclosure_anchor.application.contracts.mineru_process_profile import (
    decode_mineru_process_profile,
)
from disclosure_anchor.settings import load_settings
from tests._mineru_capacity_config_fixture import canonical_payload
from tests._mineru_capacity_v11_fixture import digest
from tests._mineru_package_a_fixture import explicit_payload, private_json
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_settings import _env, _mineru_topology


class ExplicitCapacitySettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.capacity = self.root / "capacity.json"
        private_json(self.capacity, explicit_payload())
        self.base = {**_env(self.root), **_mineru_topology()}

    def environment(self, http=14):
        private_json(self.capacity, explicit_payload(http))
        return {
            **self.base,
            "WORKER_PARSE_EXECUTION_MODE": "staged-v4",
            "DISCLOSURE_MINERU_CAPACITY_CONFIG": str(self.capacity),
            "DISCLOSURE_MINERU_CAPACITY_CONFIG_SHA256": digest(explicit_payload(http)),
            "DISCLOSURE_MINERU_API_TASK_SLOTS": "5",
            "DISCLOSURE_MINERU_API_INFERENCE_CONCURRENCY": str(http),
            "WORKER_GPU_REQUEST_BUDGET": str(http),
            "WORKER_MINERU_CLIENT_OUTSTANDING_WINDOW": "5",
        }

    def test_default_legacy_envelope_and_no_optional_load_remain_unchanged(self):
        with patch.dict(os.environ, self.base, clear=True):
            settings = load_settings()
        self.assertEqual(
            (
                settings.disclosure_mineru_api_task_slots,
                settings.disclosure_mineru_api_inference_concurrency,
                settings.mineru_processing_window_size,
                settings.worker_gpu_request_budget,
            ),
            (1, 7, 16, 7),
        )
        self.assertIsNone(loader.load_configured_mineru_capacity(settings))
        for field, value in (
            ("DISCLOSURE_MINERU_API_TASK_SLOTS", "5"),
            ("DISCLOSURE_MINERU_API_INFERENCE_CONCURRENCY", "14"),
            ("MINERU_PROCESSING_WINDOW_SIZE", "32"),
        ):
            with (
                self.subTest(field=field),
                patch.dict(os.environ, {**self.base, field: value}, clear=True),
                self.assertRaises(ValidationError),
            ):
                load_settings()

    def test_global_H_is_not_multiplied_by_five_native_documents(self):
        for http in (14, 20):
            environment = self.environment(http)
            with (
                self.subTest(http=http),
                patch.dict(os.environ, environment, clear=True),
            ):
                settings = load_settings()
                self.assertEqual(
                    settings.mineru_effective_inference_request_upper_bound, http
                )
                loaded = loader.load_configured_mineru_capacity(settings)
                self.assertIs(type(loaded), loader.LoadedMineruCapacityConfig)
                self.assertEqual(loaded.exact_bytes, self.capacity.read_bytes())
                self.assertEqual(loaded.config.final_http_limit_per_loop, http)
            with (
                patch.dict(
                    os.environ,
                    {**environment, "WORKER_GPU_REQUEST_BUDGET": str(5 * http)},
                    clear=True,
                ),
                self.assertRaises(ValidationError),
            ):
                load_settings()

    def test_pair_and_version_selection_fail_closed_before_file_io(self):
        environment = self.environment()
        for missing in (
            "DISCLOSURE_MINERU_CAPACITY_CONFIG",
            "DISCLOSURE_MINERU_CAPACITY_CONFIG_SHA256",
        ):
            broken = {
                key: value for key, value in environment.items() if key != missing
            }
            with (
                self.subTest(missing=missing),
                patch.dict(os.environ, broken, clear=True),
                self.assertRaises(ValidationError),
            ):
                load_settings()

        for delta in (
            {"WORKER_PARSE_EXECUTION_MODE": "legacy-sync"},
            {"DISCLOSURE_MINERU_CAPACITY_CONFIG_SHA256": "not-a-hash"},
            {"DISCLOSURE_MINERU_CAPACITY_CONFIG": "relative.json"},
        ):
            with (
                self.subTest(delta=delta),
                patch.dict(os.environ, {**environment, **delta}, clear=True),
                self.assertRaises(ValidationError),
            ):
                load_settings()

    def test_explicit_window_range_is_not_silently_fixed_to_legacy_sixteen(self):
        for window in (1, 32, 512):
            environment = self.environment()
            payload = explicit_payload()
            payload["processing_window_size"] = window
            private_json(self.capacity, payload)
            environment.update(
                MINERU_PROCESSING_WINDOW_SIZE=str(window),
                DISCLOSURE_MINERU_CAPACITY_CONFIG_SHA256=digest(payload),
            )
            with (
                self.subTest(window=window),
                patch.dict(os.environ, environment, clear=True),
            ):
                self.assertEqual(load_settings().mineru_processing_window_size, window)
        for window in (0, 513):
            with (
                patch.dict(
                    os.environ,
                    {
                        **self.environment(),
                        "MINERU_PROCESSING_WINDOW_SIZE": str(window),
                    },
                    clear=True,
                ),
                self.assertRaises(ValidationError),
            ):
                load_settings()

    def test_configured_loader_rejects_hash_and_canonical_byte_drift(self):
        with patch.dict(os.environ, self.environment(), clear=True):
            settings = load_settings()
        private_json(self.capacity, explicit_payload(20))
        with self.assertRaises(ValueError):
            loader.load_configured_mineru_capacity(settings)
        self.capacity.write_bytes(
            canonical_payload(explicit_payload()).replace(b'":', b'": ', 1)
        )
        with self.assertRaises(ValueError):
            loader.load_configured_mineru_capacity(settings)


class VersionedUnknownProcessProfileTests(unittest.TestCase):
    def test_v2_unknown_is_explicit_null_with_distinct_exact_identity(self):
        profile = replace(
            _profile(),
            contract_version="mineru.process-profile.v2",
            vllm_max_num_batched_tokens=None,
        )
        self.assertIsNone(
            decode_mineru_process_profile(
                profile.exact_bytes
            ).vllm_max_num_batched_tokens
        )
        self.assertIsNone(
            json.loads(profile.exact_bytes)["vllm_max_num_batched_tokens"]
        )
        self.assertNotEqual(
            profile.sha256, replace(profile, vllm_max_num_batched_tokens=32768).sha256
        )
        payload = json.loads(profile.exact_bytes)
        del payload["vllm_max_num_batched_tokens"]
        with self.assertRaises(ValueError):
            decode_mineru_process_profile(canonical_payload(payload))

    def test_v1_and_other_fields_cannot_adopt_the_unknown_exception(self):
        with self.assertRaises(ValueError):
            replace(
                _profile(),
                contract_version="mineru.process-profile.v1",
                vllm_max_num_batched_tokens=None,
            )
        for value in (0, -1, True, 1.0, "unknown"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(
                    _profile(),
                    contract_version="mineru.process-profile.v2",
                    vllm_max_num_batched_tokens=value,
                )
        with self.assertRaises(ValueError):
            replace(
                _profile(),
                contract_version="mineru.process-profile.v2",
                vllm_max_model_len=None,
            )
        legacy = replace(_profile(), contract_version="mineru.process-profile.v1")
        self.assertEqual(decode_mineru_process_profile(legacy.exact_bytes), legacy)
