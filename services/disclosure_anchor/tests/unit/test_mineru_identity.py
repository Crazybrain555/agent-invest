"""Closed MinerU runtime-bundle v6 identity regressions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_capacity_evaluator_identity
from disclosure_anchor.adapters.runtime import mineru_identity

from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_API_EGRESS_POLICY,
    MINERU_API_EXPOSURE_POLICY,
    MINERU_API_INFERENCE_MAX_CONCURRENCY,
    MINERU_API_DEFAULT_TASK_SLOTS,
    MINERU_API_OUTPUT_ROOT_POLICY,
    MINERU_API_PROTOCOL_VERSION,
    MINERU_API_TRANSPORT_PROFILE,
    MINERU_CONTENT_PACKAGE_VERSIONS,
    MINERU_HEAP_RETURN_POLICY,
    MINERU_PROCESSING_WINDOW_SIZE,
    MINERU_WINDOWS_COLLECTOR_PATH,
    MINERU_WINDOWS_COMPOSE_PATH,
    RUNTIME_MANIFEST_CONTRACT,
    MinerUClientIdentity,
    canonical_payload_sha256,
    verify_runtime_manifest_payload,
)


LOCAL_DIGEST = "sha256:" + "1" * 64
CODE_DIGEST = "sha256:" + "2" * 64


def _sha(character: str) -> str:
    return "sha256:" + character * 64


def _client_identity() -> MinerUClientIdentity:
    return MinerUClientIdentity(
        package_set_sha256=LOCAL_DIGEST,
        python_version="3.13.7",
        content_package_versions=dict(MINERU_CONTENT_PACKAGE_VERSIONS),
    )


def _manifest() -> dict[str, object]:
    return {
        "contract_version": RUNTIME_MANIFEST_CONTRACT,
        "client": {
            "package_set_sha256": LOCAL_DIGEST,
            "writer_code_sha256": CODE_DIGEST,
            **MINERU_CONTENT_PACKAGE_VERSIONS,
        },
        "orchestrator": {
            "container_image_digest": _sha("3"),
            "base_container_image_digest": _sha("2"),
            "content_environment_sha256": _sha("4"),
            "service_config_sha256": _sha("5"),
            "mount_policy_sha256": _sha("6"),
            "network_policy_sha256": _sha("7"),
            "heap_return_compatibility_sha256": _sha("8"),
            "capacity_runtime_compatibility_sha256": _sha("9"),
            "heap_return_policy": MINERU_HEAP_RETURN_POLICY,
            "mineru_version": "3.4.4",
            "api_protocol_version": MINERU_API_PROTOCOL_VERSION,
            "max_concurrent_requests": MINERU_API_DEFAULT_TASK_SLOTS,
            "inference_max_concurrency": MINERU_API_INFERENCE_MAX_CONCURRENCY,
            "processing_window_size": MINERU_PROCESSING_WINDOW_SIZE,
            "task_retention_seconds": 600,
            "task_cleanup_interval_seconds": 30,
            "output_root_policy": MINERU_API_OUTPUT_ROOT_POLICY,
            "command": [
                "mineru-api",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
                "--allow-public-http-client",
                "--max-concurrency",
                "7",
            ],
        },
        "inference_server": {
            "container_image_digest": _sha("8"),
            "content_environment_sha256": _sha("9"),
            "server_config_sha256": _sha("a"),
            "mineru_version": "3.4.4",
            "max_model_len": 8192,
            "model_repository": "provider/model",
            "served_model_id": "provider/model/snapshots/" + "b" * 40,
            "model_snapshot_revision": "b" * 40,
            "vllm_version": "0.21.0",
            "command": [
                "mineru-openai-server",
                "--host",
                "0.0.0.0",
                "--port",
                "30000",
                "--max-num-seqs",
                "128",
                "--mm-processor-cache-gb",
                "0",
            ],
        },
        "topology": {
            "api_transport": MINERU_API_TRANSPORT_PROFILE,
            "api_exposure": MINERU_API_EXPOSURE_POLICY,
            "orchestrator_egress_policy": MINERU_API_EGRESS_POLICY,
            "api_endpoint_sha256": _sha("b"),
            "observability_endpoint_sha256": _sha("c"),
            "inference_upstream_sha256": _sha("d"),
            "ssh_host_key_sha256": _sha("e"),
            "windows_node_identity_sha256": _sha("f"),
            "windows_compose_path": MINERU_WINDOWS_COMPOSE_PATH,
            "windows_compose_sha256": _sha("0"),
            "windows_collector_path": MINERU_WINDOWS_COLLECTOR_PATH,
            "windows_collector_sha256": _sha("1"),
        },
    }


def _payload(manifest: dict[str, object]) -> tuple[dict[str, object], str]:
    identity = canonical_payload_sha256(manifest)
    return {"identity_sha256": identity, "manifest": manifest}, identity


def _verify(manifest: dict[str, object]):  # type: ignore[no-untyped-def]
    payload, identity = _payload(manifest)
    return verify_runtime_manifest_payload(
        payload,
        configured_identity=identity,
        local_client_identity=_client_identity(),
        local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
        local_writer_code_digest=CODE_DIGEST,
    )


class MinerURuntimeIdentityV6Tests(unittest.TestCase):
    def test_valid_v6_closes_all_three_runtime_roles_and_topology(self) -> None:
        manifest = _manifest()
        verified = _verify(manifest)

        self.assertEqual(verified.manifest, manifest)
        self.assertEqual(verified.max_concurrent_requests, 1)
        self.assertEqual(verified.identity_sha256, canonical_payload_sha256(manifest))
        self.assertEqual(
            verified.orchestrator_identity_sha256,
            canonical_payload_sha256(manifest["orchestrator"]),
        )
        self.assertEqual(
            verified.provider_identity_sha256,
            canonical_payload_sha256(manifest["inference_server"]),
        )
        self.assertEqual(
            verified.served_model_id,
            manifest["inference_server"]["served_model_id"],  # type: ignore[index]
        )

    def test_rejects_v2_or_unknown_top_level_fields(self) -> None:
        for mutate in ("v2", "extra", "missing"):
            with self.subTest(mutate=mutate):
                manifest = _manifest()
                if mutate == "v2":
                    manifest["contract_version"] = "mineru-runtime-bundle.v2"
                elif mutate == "extra":
                    manifest["server"] = {}
                else:
                    manifest.pop("topology")
                with self.assertRaises(ValueError):
                    _verify(manifest)

    def test_every_nested_object_is_closed(self) -> None:
        for section in ("client", "orchestrator", "inference_server", "topology"):
            for mutation in ("extra", "missing"):
                with self.subTest(section=section, mutation=mutation):
                    manifest = _manifest()
                    nested = manifest[section]
                    assert isinstance(nested, dict)
                    if mutation == "extra":
                        nested["unexpected"] = "drift"
                    else:
                        nested.pop(next(iter(nested)))
                    with self.assertRaisesRegex(ValueError, "fields are not closed"):
                        _verify(manifest)

    def test_client_digest_code_and_named_packages_are_exact(self) -> None:
        mutations = {
            "package_set_sha256": _sha("0"),
            "writer_code_sha256": _sha("0"),
            "mineru_version": "3.4.5",
            "pdftext_version": "0.6.4",
            "pypdfium2_version": "4.31.0",
            "mineru_vl_utils_version": "1.0.6",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                manifest = _manifest()
                client = manifest["client"]
                assert isinstance(client, dict)
                client[field] = value
                with self.assertRaises(ValueError):
                    _verify(manifest)

    def test_orchestrator_bounded_capacity_and_fixed_content_contract(self) -> None:
        mutations = {
            "mineru_version": "3.4.5",
            "api_protocol_version": 3,
            "max_concurrent_requests": 4,
            "inference_max_concurrency": 8,
            "processing_window_size": 64,
            "output_root_policy": "mutable-output.v1",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                manifest = _manifest()
                orchestrator = manifest["orchestrator"]
                assert isinstance(orchestrator, dict)
                orchestrator[field] = value
                with self.assertRaises(ValueError):
                    _verify(manifest)

        for task_slots in (1, 2, 3):
            manifest = _manifest()
            orchestrator = manifest["orchestrator"]
            assert isinstance(orchestrator, dict)
            orchestrator["max_concurrent_requests"] = task_slots
            self.assertEqual(_verify(manifest).max_concurrent_requests, task_slots)

    def test_orchestrator_expected_window_argument_is_also_fixed(self) -> None:
        manifest = _manifest()
        payload, identity = _payload(manifest)
        with self.assertRaisesRegex(ValueError, "expected MinerU processing window"):
            verify_runtime_manifest_payload(
                payload,
                configured_identity=identity,
                local_client_identity=_client_identity(),
                local_processing_window_size=64,
                local_writer_code_digest=CODE_DIGEST,
            )

    def test_orchestrator_retention_and_cleanup_are_bounded(self) -> None:
        for retention, cleanup in ((0, 60), (600, 0), (60, 61), (True, 1)):
            with self.subTest(retention=retention, cleanup=cleanup):
                manifest = _manifest()
                orchestrator = manifest["orchestrator"]
                assert isinstance(orchestrator, dict)
                orchestrator["task_retention_seconds"] = retention
                orchestrator["task_cleanup_interval_seconds"] = cleanup
                with self.assertRaises(ValueError):
                    _verify(manifest)

    def test_orchestrator_command_pins_binary_and_inference_concurrency(self) -> None:
        for command in (
            ["python", "--max-concurrency", "7"],
            ["mineru-api", "--max-concurrency", "8"],
            ["mineru-api", "--max-concurrency", "7", "--max-concurrency", "7"],
        ):
            with self.subTest(command=command):
                manifest = _manifest()
                orchestrator = manifest["orchestrator"]
                assert isinstance(orchestrator, dict)
                orchestrator["command"] = command
                with self.assertRaises(ValueError):
                    _verify(manifest)

    def test_both_runtime_commands_reject_credential_flags(self) -> None:
        for section in ("orchestrator", "inference_server"):
            with self.subTest(section=section):
                manifest = _manifest()
                runtime = manifest[section]
                assert isinstance(runtime, dict)
                command = runtime["command"]
                assert isinstance(command, list)
                command.append("--api-key=secret")
                with self.assertRaisesRegex(ValueError, "must not contain credentials"):
                    _verify(manifest)

    def test_orchestrator_and_topology_hashes_are_immutable(self) -> None:
        targets = (
            ("orchestrator", "container_image_digest"),
            ("orchestrator", "base_container_image_digest"),
            ("orchestrator", "service_config_sha256"),
            ("orchestrator", "mount_policy_sha256"),
            ("orchestrator", "network_policy_sha256"),
            ("orchestrator", "heap_return_compatibility_sha256"),
            ("topology", "api_endpoint_sha256"),
            ("topology", "observability_endpoint_sha256"),
            ("topology", "inference_upstream_sha256"),
            ("topology", "ssh_host_key_sha256"),
            ("topology", "windows_node_identity_sha256"),
        )
        for section, field in targets:
            with self.subTest(section=section, field=field):
                manifest = _manifest()
                nested = manifest[section]
                assert isinstance(nested, dict)
                nested[field] = "latest"
                with self.assertRaisesRegex(ValueError, "is not pinned"):
                    _verify(manifest)

    def test_topology_requires_ssh_loopback_and_isolated_egress(self) -> None:
        for field in (
            "api_transport",
            "api_exposure",
            "orchestrator_egress_policy",
        ):
            with self.subTest(field=field):
                manifest = _manifest()
                topology = manifest["topology"]
                assert isinstance(topology, dict)
                topology[field] = "tailscale-serve-public.v1"
                with self.assertRaisesRegex(ValueError, field):
                    _verify(manifest)

    def test_inference_server_keeps_v2_model_and_vllm_constraints(self) -> None:
        cases = {
            "max_model_len": 16384,
            "model_snapshot_revision": "latest",
            "model_repository": "latest",
            "mineru_version": "3.4.5",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                manifest = _manifest()
                server = manifest["inference_server"]
                assert isinstance(server, dict)
                server[field] = value
                with self.assertRaises(ValueError):
                    _verify(manifest)

        command_cases = (
            [
                "mineru-openai-server",
                "--max-num-seqs",
                "127",
                "--mm-processor-cache-gb",
                "0",
            ],
            [
                "mineru-openai-server",
                "--max-num-seqs",
                "128",
                "--mm-processor-cache-gb",
                "1",
            ],
        )
        for command in command_cases:
            with self.subTest(command=command):
                manifest = deepcopy(_manifest())
                server = manifest["inference_server"]
                assert isinstance(server, dict)
                server["command"] = command
                with self.assertRaises(ValueError):
                    _verify(manifest)

    def test_attestation_self_hash_and_configured_identity_both_match(self) -> None:
        manifest = _manifest()
        payload, identity = _payload(manifest)
        payload["identity_sha256"] = _sha("0")
        with self.assertRaisesRegex(ValueError, "self-hash"):
            verify_runtime_manifest_payload(
                payload,
                configured_identity=identity,
                local_client_identity=_client_identity(),
                local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
                local_writer_code_digest=CODE_DIGEST,
            )

        payload, _ = _payload(manifest)
        with self.assertRaisesRegex(
            ValueError,
            "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256",
        ):
            verify_runtime_manifest_payload(
                payload,
                configured_identity=_sha("0"),
                local_client_identity=_client_identity(),
                local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
                local_writer_code_digest=CODE_DIGEST,
            )

    def test_bounded_http_bytes_change_writer_and_evaluator_identities(self) -> None:
        relative = "src/disclosure_anchor/adapters/runtime/bounded_http.py"
        self.assertIn(relative, mineru_identity._WRITER_CODE_RELPATHS)
        self.assertIn(
            relative,
            mineru_capacity_evaluator_identity.EVALUATOR_COMPONENT_PATHS,
        )
        with tempfile.TemporaryDirectory() as tmp:
            service_root = Path(tmp) / "service"
            all_paths = set(mineru_identity._WRITER_CODE_RELPATHS) | set(
                mineru_capacity_evaluator_identity.EVALUATOR_COMPONENT_PATHS
            )
            for relpath in all_paths:
                path = service_root / relpath
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"source:{relpath}".encode())
            fake_identity_file = (
                service_root
                / "src/disclosure_anchor/adapters/runtime/mineru_identity.py"
            )
            with patch.object(
                mineru_identity,
                "__file__",
                str(fake_identity_file),
            ):
                writer_before = mineru_identity.writer_code_digest()
                evaluator_before = mineru_capacity_evaluator_identity.commissioning_evaluator_identity(
                    service_root=service_root
                )["bundle_sha256"]
                (service_root / relative).write_bytes(b"changed transport bytes")
                writer_after = mineru_identity.writer_code_digest()
                evaluator_after = mineru_capacity_evaluator_identity.commissioning_evaluator_identity(
                    service_root=service_root
                )["bundle_sha256"]
            self.assertNotEqual(writer_before, writer_after)
            self.assertNotEqual(evaluator_before, evaluator_after)


if __name__ == "__main__":
    unittest.main()
