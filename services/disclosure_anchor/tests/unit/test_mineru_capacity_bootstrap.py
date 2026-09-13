"""Independent startup mapping and authority anchors; no serving/runtime activation."""

import dataclasses
import os
from pathlib import Path
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import patch

from tests._mineru_capacity_bootstrap_fixture import CapacityBootstrapFixture
from tests._mineru_capacity_config_fixture import CAPACITY_BYTES, capacity_payload


EXPECTED_ENVIRONMENT = {
    "MINERU_API_MAX_CONCURRENT_REQUESTS": "2",
    "MINERU_API_MAX_PENDING_TASKS": "3",
    "MINERU_API_FINALIZER_SLOTS": "1",
    "MINERU_PROCESSING_WINDOW_SIZE": "16",
    "OMP_NUM_THREADS": "4",
    "MKL_NUM_THREADS": "2",
    "OPENBLAS_NUM_THREADS": "1",
    "MINERU_PDF_RENDER_THREADS": "3",
    "MINERU_HYBRID_BATCH_RATIO": "2",
    "MINERU_ENABLE_PIPELINE_INFERENCE_LOCKS": "1",
    "MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES": "31",
    "MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES": "73",
}
PATH_KEY = "MINERU_CAPACITY_CONFIG_PATH"
SHA_KEY = "MINERU_CAPACITY_CONFIG_SHA256"


class MineruCapacityBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mineru-capacity-bootstrap-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.fixture = CapacityBootstrapFixture(self.root)
        self.addCleanup(self.fixture.close)
        self.bootstrap = self.fixture.bootstrap
        self.config = self.fixture.codec.MineruCapacityConfig(**capacity_payload())
        self.file = self.root / "startup.json"
        self.file.write_bytes(CAPACITY_BYTES)
        self.file.chmod(0o600)
        self.uid = self.file.stat().st_uid

    def environment(self):
        return {
            **EXPECTED_ENVIRONMENT,
            PATH_KEY: str(self.file),
            SHA_KEY: self.config.sha256,
            "UNRELATED_EXPLICIT_VALUE": "preserve",
        }

    def test_literal_mapping_is_closed_independent_of_host_environment_and_fresh_each_call(
        self,
    ):
        original = dataclasses.asdict(self.config)
        with patch.dict(
            os.environ,
            {"OMP_NUM_THREADS": "999", "MINERU_API_MAX_PENDING_TASKS": "999"},
        ):
            actual = self.bootstrap.capacity_environment(self.config)
        self.assertEqual(actual, EXPECTED_ENVIRONMENT)
        self.assertEqual(dataclasses.asdict(self.config), original)
        actual["OMP_NUM_THREADS"] = "mutated projection"
        self.assertEqual(
            self.bootstrap.capacity_environment(self.config), EXPECTED_ENVIRONMENT
        )
        for source, copy, raw in self.fixture.originals.values():
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(copy.read_bytes(), raw)
        self.assertIs(
            self.bootstrap.MineruCapacityConfig, self.fixture.codec.MineruCapacityConfig
        )
        self.assertIs(
            self.bootstrap.read_mineru_capacity_file,
            self.fixture.reader.read_mineru_capacity_file,
        )

    def test_both_absent_anchors_select_legacy_without_any_file_read_but_partial_empty_reject(
        self,
    ):
        with patch.object(
            self.bootstrap,
            "read_mineru_capacity_file",
            side_effect=AssertionError("unselected file must not be read"),
        ):
            value = {"OMP_NUM_THREADS": "irrelevant legacy value"}
            self.assertIsNone(
                self.bootstrap.read_startup_capacity(
                    MappingProxyType(value), expected_owner_uid=self.uid
                )
            )
            self.assertEqual(value, {"OMP_NUM_THREADS": "irrelevant legacy value"})
            for anchors in (
                {PATH_KEY: str(self.file)},
                {SHA_KEY: self.config.sha256},
                {PATH_KEY: "", SHA_KEY: self.config.sha256},
                {PATH_KEY: str(self.file), SHA_KEY: ""},
                {PATH_KEY: None, SHA_KEY: self.config.sha256},
                {PATH_KEY: str(self.file), SHA_KEY: None},
            ):
                with self.subTest(anchors=anchors), self.assertRaises(ValueError):
                    self.bootstrap.read_startup_capacity(
                        anchors, expected_owner_uid=self.uid
                    )

    def test_real_file_and_explicit_mapping_return_exact_config_without_mutating_either_environment(
        self,
    ):
        environment = self.environment()
        saved = environment.copy()
        marker = {
            "MINERU_API_MAX_CONCURRENT_REQUESTS": "128",
            "OMP_NUM_THREADS": "256",
            PATH_KEY: "/not-used",
            SHA_KEY: "not-used",
        }
        with patch.dict(os.environ, marker):
            process_before = dict(os.environ)
            config = self.bootstrap.read_startup_capacity(
                MappingProxyType(environment), expected_owner_uid=self.uid
            )
            self.assertTrue(
                dict(os.environ) == process_before,
                "bootstrap mutated process environment",
            )
        self.assertIs(type(config), self.fixture.codec.MineruCapacityConfig)
        self.assertEqual(config.exact_bytes, CAPACITY_BYTES)
        self.assertEqual(config.sha256, self.config.sha256)
        self.assertEqual(environment, saved)
        self.assertEqual(self.file.read_bytes(), CAPACITY_BYTES)

    def test_each_mapped_environment_variable_is_required_exact_and_cannot_silently_default(
        self,
    ):
        for key, expected in EXPECTED_ENVIRONMENT.items():
            for change in ("missing", "different", "leading_zero", "wrong_type"):
                environment = self.environment()
                if change == "missing":
                    del environment[key]
                elif change == "different":
                    environment[key] = "999"
                elif change == "leading_zero":
                    environment[key] = "0" + expected
                else:
                    environment[key] = int(expected)
                original = environment.copy()
                with (
                    self.subTest(key=key, change=change),
                    self.assertRaises(ValueError),
                ):
                    self.bootstrap.read_startup_capacity(
                        MappingProxyType(environment), expected_owner_uid=self.uid
                    )
                self.assertEqual(environment, original)

    def test_actual_file_hash_owner_and_canonical_errors_are_not_overridden_by_consistent_env(
        self,
    ):
        for change in ("hash", "owner", "noncanonical"):
            environment = self.environment()
            uid = self.uid
            if change == "hash":
                environment[SHA_KEY] = "sha256:" + "0" * 64
            elif change == "owner":
                uid += 1
            else:
                import hashlib

                raw = CAPACITY_BYTES + b"\n"
                self.file.write_bytes(raw)
                environment[SHA_KEY] = "sha256:" + hashlib.sha256(raw).hexdigest()
            original = environment.copy()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.bootstrap.read_startup_capacity(
                    MappingProxyType(environment), expected_owner_uid=uid
                )
            self.assertEqual(environment, original)

    def test_http_h_is_single_loop_not_multiplied_by_n_and_exact_codec_family_is_required(
        self,
    ):
        self.assertIsNone(self.bootstrap.verify_http_capacity(self.config, 7))
        for wrong in (14, 6, True, 7.0, "7", None):
            with self.subTest(http=wrong), self.assertRaises(ValueError):
                self.bootstrap.verify_http_capacity(self.config, wrong)
        h_one = self.fixture.codec.MineruCapacityConfig(
            **capacity_payload(final_http_limit_per_loop=1)
        )
        with self.assertRaises(ValueError):
            self.bootstrap.verify_http_capacity(h_one, True)

        class Derived(self.fixture.codec.MineruCapacityConfig):
            pass

        for wrong in (None, capacity_payload(), Derived(**capacity_payload())):
            for function, args in (
                (self.bootstrap.capacity_environment, (wrong,)),
                (self.bootstrap.verify_http_capacity, (wrong, 7)),
            ):
                with (
                    self.subTest(type=type(wrong).__name__, function=function.__name__),
                    self.assertRaises(ValueError),
                ):
                    function(*args)
        varied = self.fixture.codec.MineruCapacityConfig(
            **capacity_payload(
                omp_num_threads=8,
                mkl_num_threads=3,
                openblas_num_threads=2,
                pdf_render_processes_requested=5,
            )
        )
        expected = {
            **EXPECTED_ENVIRONMENT,
            "OMP_NUM_THREADS": "8",
            "MKL_NUM_THREADS": "3",
            "OPENBLAS_NUM_THREADS": "2",
            "MINERU_PDF_RENDER_THREADS": "5",
        }
        self.assertEqual(varied.contract_version, self.config.contract_version)
        self.assertEqual(self.bootstrap.capacity_environment(varied), expected)
        self.assertIsNone(self.bootstrap.verify_http_capacity(varied, 7))
