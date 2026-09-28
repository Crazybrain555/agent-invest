"""Independent refusal cases for release binding; no live HTTP or credentials."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from disclosure_anchor.adapters.runtime import mineru_release_binding as binding
from disclosure_anchor.adapters.runtime.mineru_identity import canonical_payload_sha256
from disclosure_anchor.application.contracts.mineru_deployment_profile import decode_mineru_deployment_profile
from disclosure_anchor.application.contracts.mineru_local_worker_profile import decode_mineru_local_worker_profile
from disclosure_anchor.application.services import mineru_release_plan as plan
from tests.unit.test_mineru_release_compose_independent import DEPLOYMENT, LEGACY_INFERENCE_ARGV, _wire
from tests.unit.test_mineru_release_package_independent import LOCAL
from tests.unit.test_mineru_result_storage_release import storage_capacity


IDENTITY = "sha256:" + "1" * 64
COMPOSE = "sha256:" + "3" * 64


class MineruReleaseBindingIndependentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.evidence = self.root / "dummy-reference.json"
        self.evidence.write_text("{}")
        self.private = self.root / "private.json"
        self.document = {
            "contract_version": "m6.release-bind-private-inputs.v1",
            "api_url": "http://127.0.0.1:30003",
            "gpu_uuid": "GPU-12345678-abcd-4321-9876-abcdef123456",
            "smoke_receipt_path": str(self.evidence),
            "validation_receipt_path": str(self.evidence),
            "canary_cache_path": str(self.evidence),
        }

    def write(self):
        self.private.write_text(json.dumps(self.document))
        self.private.chmod(0o600)

    def test_a_pass_label_and_matching_hash_do_not_prove_deployment_qualification(self):
        # Missing canary identities, before/after epoch, consumption/absence,
        # closure and conservation. The thin binder must reuse real validation,
        # not let this self-asserted pass become a qualification hash.
        path = self.root / "claimed-pass.json"
        path.write_text(json.dumps({
            "schema": "mineru_heldout_validation_receipt.v2", "status": "pass",
            "documents": [{"receipt": {"identity": {
                "runtime_manifest_identity_sha256": IDENTITY,
            }}}],
        }))
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            binding.load_validation_receipt(path, runtime_identity=IDENTITY)

    def test_local_endpoint_validation_uses_parsed_authority_not_a_string_prefix(self):
        for url in ("http://127.0.0.1:30003@outside.invalid", "http://localhost:30003@outside.invalid",
                    "http://127.0.0.1:30003#ignored", "http://127.0.0.1:30003?unexpected=1",
                    "http://127.0.0.1:not-a-port", "http://127.0.0.1:0"):
            with self.subTest(url=url):
                self.document["api_url"] = url
                self.write()
                with self.assertRaises(ValueError):
                    binding.load_bind_private_inputs(self.private)

    @unittest.skipIf(os.name == "nt", "POSIX private configuration boundary")
    def test_private_binding_is_not_accepted_with_group_or_world_read_access(self):
        self.write()
        for mode in (0o640, 0o644, 0o660):
            with self.subTest(mode=oct(mode)):
                self.private.chmod(mode)
                with self.assertRaises(ValueError):
                    binding.load_bind_private_inputs(self.private)


class ReleaseBindingInferenceArgvTests(unittest.TestCase):
    """The bound GPU fraction must be the one the attested inference argv carries."""

    def setUp(self):
        self.capacity = storage_capacity()
        self.local = decode_mineru_local_worker_profile(_wire(LOCAL))

    def deployment(self, millionths):
        value = deepcopy(DEPLOYMENT)
        value["inference_declared_defaults"]["vllm_gpu_memory_utilization_millionths"] = millionths
        return decode_mineru_deployment_profile(_wire(value))

    def bind(self, millionths, command):
        report = SimpleNamespace(
            inputs=SimpleNamespace(capacity=self.capacity, deployment_profile=self.deployment(millionths),
                                   local_profile=self.local),
            manifest=SimpleNamespace(projection={"compose_sha256": COMPOSE}),
        )
        manifest = {
            "orchestrator": {
                "capacity_config_sha256": self.capacity.sha256, "task_retention_seconds": 600,
                "task_cleanup_interval_seconds": 30, "task_registry_max_records": 64,
                "container_image_digest": "sha256:" + "a" * 64,
            },
            "inference_server": {
                "command": command, "container_image_digest": "sha256:" + "b" * 64, "max_model_len": 8192,
                "model_repository": "opendatalab/MinerU2.5-Pro-2605-1.2B",
                "model_snapshot_revision": "bff20d4ae2bf202df9f45284b4d43681555a97ed",
            },
            "topology": {"windows_compose_sha256": COMPOSE, "windows_node_identity_sha256": "sha256:" + "c" * 64},
        }
        live = binding.LiveIdentity(
            owner={}, cgroup_identity_sha256="sha256:" + "d" * 64, cgroup_max_bytes=None,
            vm_total_bytes=1 << 36, health_raw=b"", pressure_raw=b"",
        )
        return binding.build_process_profile(report, manifest, IDENTITY, live)

    def test_profile_fraction_is_bound_from_the_argv_the_release_deploys(self):
        for millionths, expected in ((400000, LEGACY_INFERENCE_ARGV + ["--gpu-memory-utilization", "0.400000"]),
                                     (500000, LEGACY_INFERENCE_ARGV)):
            with self.subTest(millionths=millionths):
                # The argv a deployment runs is the one parsed back from the rendered Compose bytes.
                raw = plan.render_compose_yaml(plan.compose_document(self.deployment(millionths), self.capacity))
                service = plan.parse_compose_yaml(raw)["services"]["mineru-openai-server"]
                deployed = service["entrypoint"] + service["command"]
                self.assertEqual(deployed, expected)
                profile = self.bind(millionths, deployed)
                self.assertEqual(profile.vllm_gpu_memory_utilization_millionths, millionths)
                self.assertEqual(profile.vllm_engine_args_sha256, canonical_payload_sha256(expected))
                self.assertEqual((profile.vllm_max_num_seqs, profile.vllm_mm_processor_cache_bytes), (128, 0))

    def test_declared_fraction_that_did_not_reach_the_argv_cannot_be_bound(self):
        explicit = LEGACY_INFERENCE_ARGV + ["--gpu-memory-utilization", "0.400000"]
        cases = {
            400000: (
                LEGACY_INFERENCE_ARGV,  # the engine would keep MinerU's helper fraction
                LEGACY_INFERENCE_ARGV + ["--gpu-memory-utilization", "0.4"],
                LEGACY_INFERENCE_ARGV + ["--gpu-memory-utilization=0.400000"],
                LEGACY_INFERENCE_ARGV + ["--gpu_memory_utilization", "0.400000"],
                explicit + ["--gpu-memory-utilization", "0.400000"],
                explicit + ["--gpu-memory-utilization", "0.5"],
                explicit + ["--kv-cache-memory-bytes", "4294967296"],
                explicit[:6] + ["64"] + explicit[7:],
                None,
            ),
            500000: (
                LEGACY_INFERENCE_ARGV + ["--gpu-memory-utilization", "0.500000"],
                LEGACY_INFERENCE_ARGV + ["--gpu-memory-utilization", "0.400000"],
            ),
        }
        for millionths, commands in cases.items():
            for command in commands:
                with self.subTest(millionths=millionths, command=command), self.assertRaisesRegex(
                    binding.ReleaseIdentityError, "not the release projection",
                ):
                    self.bind(millionths, command)
