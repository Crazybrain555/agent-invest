"""A2 capacity authority through real deployment, manifest and receipt parsers."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_deployment_gate as gate
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    parse_mineru_orchestrator_health_payload,
)
from disclosure_anchor.settings import Settings
from tests._mineru_capacity_v11_fixture import digest, idle_health
from tests._mineru_package_a_fixture import (
    NOW,
    explicit_payload,
    gate_fixture,
    private_json,
)
from tests.unit import test_mineru_deployment_gate as legacy_gate


class ExplicitDeploymentCompositionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def checker(self, settings, profile, client, **kwargs):
        client_patch, writer_patch = (
            legacy_gate.MinerUDeploymentGateTests._identity_patches(client)
        )
        with client_patch, writer_patch:
            return gate.MinerUDeploymentChecker(
                settings, process_profile=profile, wall_clock=lambda: NOW, **kwargs
            )

    def test_checker_loads_explicit_H14_H20_and_forwards_same_authority_to_health(self):
        for http in (14, 20):
            root = self.root / str(http)
            root.mkdir()
            settings, profile, capacity, client = gate_fixture(root, http)
            with self.subTest(http=http):
                checker = self.checker(settings, profile, client)
                self.assertEqual(checker.expected_capacity, capacity)
                observed = parse_mineru_orchestrator_health_payload(
                    idle_health(explicit_payload(http)),
                    expected_task_slots=None,
                    expected_capacity=capacity,
                )
                with (
                    patch.object(
                        gate, "fetch_mineru_orchestrator_health", return_value=observed
                    ) as fetch,
                    patch.object(gate, "probe_mineru_served_model"),
                ):
                    checker.assert_admission()
                self.assertIs(
                    fetch.call_args.kwargs["expected_capacity"],
                    checker.expected_capacity,
                )
                self.assertIsNone(fetch.call_args.kwargs["expected_task_slots"])

    def test_supplied_DTO_must_match_external_settings_hash_and_explicit_mode(self):
        settings, profile, capacity, client = gate_fixture(self.root)
        checker = self.checker(settings, profile, client, expected_capacity=capacity)
        self.assertIs(checker.expected_capacity, capacity)
        wrong = replace(capacity, final_http_limit_per_loop=20)
        with self.assertRaises(ValueError):
            self.checker(settings, profile, client, expected_capacity=wrong)
        legacy_root = self.root / "legacy"
        legacy, _, _ = legacy_gate.MinerUDeploymentGateTests()._fixture(
            legacy_root, now=NOW
        )
        with self.assertRaises(ValueError):
            self.checker(legacy, profile, client, expected_capacity=capacity)

    def test_changed_file_cannot_be_replaced_by_a_manifest_derived_capacity(self):
        settings, profile, _, client = gate_fixture(self.root)
        private_json(settings.disclosure_mineru_capacity_config, explicit_payload(20))
        with self.assertRaises(ValueError):
            self.checker(settings, profile, client)

    def test_profile_P_F_H_ratio_thread_and_window_drift_are_rejected(self):
        settings, profile, _, client = gate_fixture(self.root)
        changes = (
            {
                "api_max_pending_tasks": 5,
                "registry_nonterminal_cap": 5,
                "registry_terminal_cap": 123,
            },
            {"finalizer_slots": 2},
            {"inference_concurrency": 20},
            {"requested_hybrid_batch_ratio": 4, "effective_hybrid_batch_ratio": 4},
            {"omp_thread_count": 2},
            {"cpu_worker_threads": 4},
            {"processing_window_size": 32},
            {"gpu_request_slots": 70},
        )
        for update in changes:
            with (
                self.subTest(update=update),
                self.assertRaises(gate.MinerUDeploymentGateError),
            ):
                self.checker(settings, replace(profile, **update), client)

    def test_explicit_profile_cannot_admit_legacy_evidence_or_v1_profile(self):
        settings, profile, capacity, client = gate_fixture(self.root)
        with self.assertRaises(gate.MinerUDeploymentGateError):
            self.checker(
                settings,
                replace(
                    profile,
                    contract_version="mineru.process-profile.v1",
                    vllm_max_num_batched_tokens=32768,
                ),
                client,
            )
        legacy_root = self.root / "legacy"
        legacy, _, _ = legacy_gate.MinerUDeploymentGateTests()._fixture(
            legacy_root, now=NOW
        )
        selected = Settings(
            **dict(
                settings.model_dump(),
                disclosure_mineru_smoke_receipt=legacy.disclosure_mineru_smoke_receipt,
                disclosure_mineru_canary_cache=legacy.disclosure_mineru_canary_cache,
                disclosure_mineru_validation_receipt=legacy.disclosure_mineru_validation_receipt,
                disclosure_mineru_runtime_bundle_identity_sha256=legacy.disclosure_mineru_runtime_bundle_identity_sha256,
            )
        )
        with self.assertRaises(gate.MinerUDeploymentGateError):
            self.checker(
                selected,
                replace(
                    profile,
                    runtime_bundle_identity_sha256=selected.disclosure_mineru_runtime_bundle_identity_sha256,
                ),
                client,
                expected_capacity=capacity,
            )

    def test_smoke_and_heldout_samples_require_full_matching_capacity_wire(self):
        for which in ("smoke", "heldout"):
            folder = self.root / which
            folder.mkdir()
            settings, profile, _, client = gate_fixture(folder)
            target = (
                settings.disclosure_mineru_smoke_receipt
                if which == "smoke"
                else settings.disclosure_mineru_validation_receipt
            )
            value = json.loads(target.read_bytes())
            if which == "smoke":
                value["orchestrator"]["after"] = legacy_gate.health(completed=2)
            else:
                receipt = value["documents"][0]["receipt"]
                receipt["orchestrator"]["after"] = idle_health(explicit_payload(20))
                value["documents"][0]["receipt_sha256"] = digest(receipt)
            private_json(target, value)
            with (
                self.subTest(which=which),
                self.assertRaises(gate.MinerUDeploymentGateError),
            ):
                self.checker(settings, profile, client)

    def test_unmeasured_vllm_token_limit_cannot_be_reported_as_known(self):
        settings, profile, _, client = gate_fixture(self.root)
        # This literal manifest contains no max-num-batched-tokens argument.
        self.checker(settings, profile, client)
        with self.assertRaises(gate.MinerUDeploymentGateError):
            self.checker(
                settings, replace(profile, vllm_max_num_batched_tokens=32768), client
            )

    def test_known_token_limit_requires_exact_unambiguous_engine_evidence(self):
        for index, arguments in enumerate(
            (("--max-num-batched-tokens", "16384"), ("--max-num-batched-tokens=16384",))
        ):
            folder = self.root / str(index)
            folder.mkdir()
            settings, profile, _, client = gate_fixture(
                folder, engine_token_arguments=arguments
            )
            with self.subTest(arguments=arguments):
                self.checker(
                    settings,
                    replace(profile, vllm_max_num_batched_tokens=16384),
                    client,
                )
                for claimed in (None, 32768):
                    with self.assertRaises(gate.MinerUDeploymentGateError):
                        self.checker(
                            settings,
                            replace(profile, vllm_max_num_batched_tokens=claimed),
                            client,
                        )
        for index, arguments in enumerate(
            (
                ("--max-num-batched-tokens",),
                ("--max-num-batched-tokens=0",),
                ("--max-num-batched-tokens=16384", "--max-num-batched-tokens=16384"),
            )
        ):
            folder = self.root / f"invalid-{index}"
            folder.mkdir()
            settings, profile, _, client = gate_fixture(
                folder, engine_token_arguments=arguments
            )
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(gate.MinerUDeploymentGateError),
            ):
                self.checker(
                    settings,
                    replace(profile, vllm_max_num_batched_tokens=16384),
                    client,
                )
