"""Independent R20 release projections; literal oracles, no Docker or network."""

from copy import deepcopy
from dataclasses import replace
import importlib
import json
import unittest

from tests.unit.test_mineru_release_projection_independent import LITERAL_ENV, _capacity


DEPLOYMENT = {
    "contract_version": "mineru.deployment-profile.v1",
    "project_name": "independent-release",
    "base_image_repo_digest": "mineru@sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458",
    "api_image_reference": "test/mineru-api:independent",
    "api_device_profile": "cuda0",
    "api_memory_limit_bytes": 34359738368,
    "api_phase_trace": False,
    "api_output_root_windows": "C:/Users/help/workspaces/independent/output",
    "api_task_retention_seconds": 600,
    "api_task_cleanup_interval_seconds": 30,
    "api_published_port": 30003,
    "vllm_observability_published_port": 30001,
    "inference_max_num_seqs": 128,
    "inference_mm_processor_cache_gb": 0,
    "inference_declared_defaults": {
        "vllm_gpu_memory_utilization_millionths": 900000,
        "vllm_tensor_parallel_size": 1,
        "vllm_pipeline_parallel_size": 1,
        "vllm_enforce_eager": False,
        "vllm_enable_prefix_caching": False,
    },
}
API_ARGV = ["--host", "0.0.0.0", "--port", "8000",
            "--allow-public-http-client", "--max-concurrency", "18"]


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class MineruReleaseComposeIndependentTests(unittest.TestCase):
    def setUp(self):
        self.plan = importlib.import_module(
            "disclosure_anchor.application.services.mineru_release_plan",
        )
        self.contract = importlib.import_module(
            "disclosure_anchor.application.contracts.mineru_deployment_profile",
        )
        self.deployment = self.contract.decode_mineru_deployment_profile(_wire(DEPLOYMENT))
        self.capacity = _capacity()

    def document(self, capacity=None):
        return self.plan.compose_document(self.deployment, capacity or self.capacity)

    def assert_refused(self, document):
        try:
            differences = self.plan.verify_compose_projection(
                document, self.capacity, self.deployment,
            )
        except ValueError:
            return
        self.assertTrue(differences, "contradictory Compose was accepted")

    def test_literal_capacity_and_h_argv_survive_structured_roundtrip(self):
        document = self.document()
        api = document["services"]["mineru-api"]
        self.assertEqual(api["command"], API_ARGV)
        self.assertEqual({key: api["environment"][key] for key in LITERAL_ENV}, LITERAL_ENV)
        # The explicit-capacity image owns the path/SHA ENV anchors. Compose
        # must not introduce an override, even one matching today's image.
        self.assertNotIn("MINERU_CAPACITY_CONFIG_SHA256", api["environment"])
        self.assertNotIn("MINERU_CAPACITY_CONFIG_PATH", api["environment"])
        self.assertEqual(api["image"], "test/mineru-api:independent")
        self.assertEqual(api["mem_limit"], 34359738368)
        self.assertEqual(api["memswap_limit"], 34359738368)
        self.assertEqual(self.plan.verify_compose_projection(
            document, self.capacity, self.deployment,
        ), [])
        raw = self.plan.render_compose_yaml(document)
        self.assertEqual(self.plan.parse_compose_yaml(raw), document)
        self.assertEqual(self.plan.render_compose_yaml(document), raw)
        api["environment"]["OMP_NUM_THREADS"] = "999"
        self.assertEqual(self.document()["services"]["mineru-api"]["environment"]["OMP_NUM_THREADS"], "4")

    def test_h_and_l_change_without_old_numeric_replacement_or_multiplying_n(self):
        capacity = replace(self.capacity, parse_active_limit=8, total_nonterminal_limit=10,
                           final_http_limit_per_loop=22, max_unacked_result_bytes=3221225472)
        api = self.document(capacity)["services"]["mineru-api"]
        self.assertEqual(api["command"], API_ARGV[:-1] + ["22"])
        self.assertEqual(api["environment"]["MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES"], "3221225472")
        self.assertEqual(api["environment"]["MINERU_API_MAX_CONCURRENT_REQUESTS"], "8")
        self.assertEqual(api["environment"]["MINERU_API_MAX_PENDING_TASKS"], "10")
        self.assertNotIn("MINERU_CAPACITY_CONFIG_SHA256", api["environment"])

    def test_every_missing_or_wrong_numeric_projection_and_lock_fails(self):
        for key in LITERAL_ENV:
            for action in ("missing", "wrong"):
                with self.subTest(key=key, action=action):
                    document = self.document()
                    env = document["services"]["mineru-api"]["environment"]
                    if action == "missing":
                        del env[key]
                    else:
                        env[key] = "0" if key.endswith("LOCKS") else "999"
                    self.assert_refused(document)

    def test_duplicate_cli_alias_missing_h_and_extra_capacity_variable_fail(self):
        for command in (API_ARGV[:-2], API_ARGV + ["--max-concurrency", "18"],
                        API_ARGV + ["--max-concurrency=18"], API_ARGV[:-1] + ["126"]):
            with self.subTest(command=command):
                document = self.document()
                document["services"]["mineru-api"]["command"] = command
                self.assert_refused(document)
        document = self.document()
        document["services"]["mineru-api"]["environment"]["MINERU_CAPACITY_OTHER"] = "1"
        self.assert_refused(document)

    def test_compose_cannot_override_image_baked_capacity_anchors(self):
        for key, value in (("MINERU_CAPACITY_CONFIG_SHA256", "sha256:" + "f" * 64),
                           ("MINERU_CAPACITY_CONFIG_SHA256", self.capacity.sha256),
                           ("MINERU_CAPACITY_CONFIG_PATH", "/usr/local/etc/mineru/capacity.json"),
                           ("MINERU_CAPACITY_CONFIG_PATH", "/tmp/old-capacity.json")):
            document = self.document()
            document["services"]["mineru-api"]["environment"][key] = value
            self.assert_refused(document)

    def test_duplicate_yaml_keys_are_not_silently_last_wins(self):
        cases = (
            b"services:\n  mineru-api: {}\n  mineru-api: {}\n",
            b"services:\n  mineru-api:\n    environment:\n      OMP_NUM_THREADS: '4'\n      OMP_NUM_THREADS: '1'\n",
        )
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.plan.parse_compose_yaml(raw)

    def test_current_runtime_rejects_w32_but_not_api_legal_byte_backpressure(self):
        # P9 * 256 MiB > 2 GiB. The API's reservation backpressure makes this
        # legal; separate Mac profile eligibility is not falsely claimed here.
        self.assertIsNone(self.plan.require_current_runtime_capabilities(self.capacity))
        with self.assertRaises(ValueError):
            self.plan.require_current_runtime_capabilities(
                replace(self.capacity, processing_window_size=32),
            )

    def test_deployment_input_is_closed_and_exact_typed(self):
        for changed in ({**DEPLOYMENT, "unknown": 1},
                        {**DEPLOYMENT, "api_published_port": True},
                        {**DEPLOYMENT, "api_memory_limit_bytes": -1},
                        {**DEPLOYMENT, "api_phase_trace": 1},
                        {**DEPLOYMENT, "api_task_cleanup_interval_seconds": 601}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.contract.decode_mineru_deployment_profile(_wire(changed))
        missing = deepcopy(DEPLOYMENT)
        del missing["api_device_profile"]
        with self.assertRaises(ValueError):
            self.contract.decode_mineru_deployment_profile(_wire(missing))
        raw = _wire(DEPLOYMENT)
        with self.assertRaises(ValueError):
            self.contract.decode_mineru_deployment_profile(raw[:-1] + b',"api_phase_trace":false}')
