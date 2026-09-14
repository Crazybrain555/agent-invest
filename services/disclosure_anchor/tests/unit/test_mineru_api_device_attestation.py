"""Independent device-selection attestation tests; no SSH, Docker, or GPU calls."""

from copy import deepcopy
import unittest

from tests._mineru_capacity_config_fixture import capacity_payload
from tests._mineru_capacity_v11_fixture import build, digest, observation


class ApiDeviceAttestationTests(unittest.TestCase):
    def test_explicit_device_is_preserved_in_identity_without_changing_capacity(self):
        config = capacity_payload(
            parse_active_limit=5,
            total_nonterminal_limit=6,
            finalizer_active_limit=1,
            final_http_limit_per_loop=7,
            processing_window_size=16,
            omp_num_threads=4,
            mkl_num_threads=4,
            openblas_num_threads=1,
            hybrid_batch_ratio_requested=1,
        )
        original = observation(config)
        baseline = build(original, config)
        identities = {baseline["identity_sha256"]}
        baseline_orchestrator = baseline["manifest"]["orchestrator"]
        for mode in ("cpu", "cuda:0"):
            with self.subTest(mode=mode):
                raw = deepcopy(original)
                raw["api"]["environment"]["MINERU_DEVICE_MODE"] = mode
                before = deepcopy(raw)
                actual = build(raw, config)
                self.assertEqual(raw, before)
                orchestrator = actual["manifest"]["orchestrator"]
                self.assertEqual(
                    orchestrator["content_environment_sha256"],
                    digest(raw["api"]["environment"]),
                )
                expected = dict(baseline_orchestrator)
                expected["content_environment_sha256"] = digest(raw["api"]["environment"])
                self.assertEqual(orchestrator, expected)
                self.assertNotIn(actual["identity_sha256"], identities)
                identities.add(actual["identity_sha256"])
        self.assertEqual(len(identities), 3)

    def test_device_mode_type_and_spelling_are_closed_with_value_errors(self):
        config = capacity_payload()
        for value in (
            "cuda", "cuda:1", "CUDA:0", "cuda:0\n", "auto", "", 0, False,
            None, ["cuda:0"], {"mode": "cuda:0"},
        ):
            with self.subTest(value=value):
                raw = observation(config)
                raw["api"]["environment"]["MINERU_DEVICE_MODE"] = value
                with self.assertRaises(ValueError):
                    build(raw, config)

    def test_device_selection_never_widens_other_environment_allowlist(self):
        config = capacity_payload()
        for key, value in (
            ("NVIDIA_VISIBLE_DEVICES", "all"),
            ("CUDA_VISIBLE_DEVICES", "1"),
            ("MINERU_UNKNOWN_DEVICE_FLAG", "1"),
            ("mineru_device_mode", "cuda:0"),
        ):
            with self.subTest(key=key):
                raw = observation(config)
                raw["api"]["environment"]["MINERU_DEVICE_MODE"] = "cuda:0"
                raw["api"]["environment"][key] = value
                with self.assertRaises(ValueError):
                    build(raw, config)

    def test_self_consistent_gpu_environment_does_not_authorize_capacity_drift(self):
        config = capacity_payload()
        for env, value in (
            ("MINERU_API_MAX_CONCURRENT_REQUESTS", "128"),
            ("MINERU_API_MAX_PENDING_TASKS", "128"),
            ("MINERU_PROCESSING_WINDOW_SIZE", "128"),
            ("OMP_NUM_THREADS", "128"),
        ):
            with self.subTest(env=env):
                raw = observation(config)
                raw["api"]["environment"]["MINERU_DEVICE_MODE"] = "cuda:0"
                raw["api"]["environment"][env] = value
                with self.assertRaises(ValueError):
                    build(raw, config)


if __name__ == "__main__":
    unittest.main(verbosity=2)
