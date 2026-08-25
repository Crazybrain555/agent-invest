"""Remote MinerU v3 attestation unit tests; no SSH/GPU access."""

from __future__ import annotations

import base64
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import yaml

from scripts.attest_mineru_remote_runtime import (
    EXPECTED_IMAGE_ID,
    EXPECTED_MODEL_REVISION,
    EXPECTED_REPO_DIGEST,
    _known_host_key_sha256,
    _ssh_base,
    build_manifest,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_CONTENT_PACKAGE_VERSIONS,
    MinerUClientIdentity,
    canonical_payload_sha256,
)


CLIENT = MinerUClientIdentity(
    package_set_sha256="sha256:" + "1" * 64,
    python_version="3.13.7",
    content_package_versions=MINERU_CONTENT_PACKAGE_VERSIONS,
)
CODE_DIGEST = "sha256:" + "2" * 64


def _observation() -> dict[str, Any]:
    api_environment = [
        "MINERU_MODEL_SOURCE=local",
        "MINERU_API_MAX_CONCURRENT_REQUESTS=3",
        "MINERU_PROCESSING_WINDOW_SIZE=16",
        "MINERU_API_OUTPUT_ROOT=/var/lib/mineru-api-output",
        "MINERU_API_TASK_RETENTION_SECONDS=600",
        "MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS=30",
        "MINERU_API_DISABLE_ACCESS_LOG=true",
        "MINERU_API_ENABLE_FASTAPI_DOCS=false",
    ]
    return {
        "schema": "mineru-windows-runtime-observation.v2",
        "collector_path": (
            r"C:\ProgramData\agent-invest\mineru\collect_mineru_runtime.ps1"
        ),
        "collector_sha256": "sha256:" + "7" * 64,
        "compose_path": r"C:\ProgramData\compose.tailnet.yaml",
        "compose_sha256": "sha256:" + "3" * 64,
        "compose_config_sha256": "sha256:" + "4" * 64,
        "windows_node_identity_sha256": "sha256:" + "5" * 64,
        "networks": {
            "inference": {
                "name": "mineru-tailnet_inference",
                "driver": "bridge",
                "internal": True,
            },
            "runtime": {
                "name": "mineru-tailnet_runtime",
                "driver": "bridge",
                "internal": False,
            },
        },
        "api": {
            "image": EXPECTED_REPO_DIGEST,
            "image_id": EXPECTED_IMAGE_ID,
            "entrypoint": ["mineru-api"],
            "command": [
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
                "--allow-public-http-client",
                "--max-concurrency",
                "7",
            ],
            "environment": {
                item.partition("=")[0]: item.partition("=")[2]
                for item in api_environment
            },
            "mounts": [
                {
                    "Type": "bind",
                    "Source": "C:/ProgramData/agent-invest/mineru-api-output",
                    "Destination": "/var/lib/mineru-api-output",
                    "RW": True,
                    "Propagation": "rprivate",
                }
            ],
            "networks": ["mineru-tailnet_inference"],
            "port": None,
            "restart_policy": {"Name": "always", "MaximumRetryCount": 0},
            "health_state": "healthy",
            "external_tcp_egress_blocked": True,
        },
        "proxy": {
            "image": EXPECTED_REPO_DIGEST,
            "image_id": EXPECTED_IMAGE_ID,
            "entrypoint": ["/usr/bin/python3.12"],
            "command": [
                "-I",
                "-c",
                yaml.safe_load(
                    (Path(__file__).resolve().parents[2]
                    / "config"
                    / "mineru-windows.compose.yaml").read_text()
                )["services"]["mineru-api-proxy"]["command"][2],
            ],
            "environment": {},
            "mounts": [],
            "networks": [
                "mineru-tailnet_inference",
                "mineru-tailnet_runtime",
            ],
            "port": {"HostIp": "127.0.0.1", "HostPort": "30003"},
            "restart_policy": {"Name": "always", "MaximumRetryCount": 0},
            "health_state": "healthy",
            "read_only_rootfs": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges=true", "label=disable"],
        },
        "inference": {
            "image": EXPECTED_REPO_DIGEST,
            "image_id": EXPECTED_IMAGE_ID,
            "entrypoint": ["mineru-openai-server"],
            "command": [
                "--host",
                "0.0.0.0",
                "--port",
                "30000",
                "--max-num-seqs",
                "128",
                "--mm-processor-cache-gb",
                "0",
            ],
            "environment": {"MINERU_MODEL_SOURCE": "local"},
            "mounts": [],
            "networks": [
                "mineru-tailnet_inference",
                "mineru-tailnet_runtime",
            ],
            "port": {"HostIp": "127.0.0.1", "HostPort": "30001"},
            "restart_policy": {"Name": "always", "MaximumRetryCount": 0},
            "health_state": "healthy",
        },
        "api_health": {
            "status": "healthy",
            "version": "3.4.4",
            "protocol_version": 2,
            "queued_tasks": 0,
            "processing_tasks": 0,
            "completed_tasks": 0,
            "failed_tasks": 0,
            "max_concurrent_requests": 3,
            "processing_window_size": 16,
            "task_retention_seconds": 600,
            "task_cleanup_interval_seconds": 30,
        },
        "served_model": {
            "id": (
                "/root/.cache/huggingface/hub/"
                "models--opendatalab--MinerU2.5-Pro-2605-1.2B/snapshots/"
                + EXPECTED_MODEL_REVISION
            ),
            "repository": "opendatalab/MinerU2.5-Pro-2605-1.2B",
            "revision": EXPECTED_MODEL_REVISION,
            "max_model_len": 8192,
            "vllm_version": "0.21.0",
        },
        "output_root": {
            "path": r"C:\ProgramData\agent-invest\mineru-api-output",
            "file_count": 0,
            "total_bytes": 0,
        },
    }


class AttestMinerURemoteRuntimeTests(unittest.TestCase):
    def test_build_manifest_binds_live_remote_observation(self) -> None:
        observation = _observation()
        with (
            patch(
                "scripts.attest_mineru_remote_runtime.client_bundle_identity",
                return_value=CLIENT,
            ),
            patch(
                "scripts.attest_mineru_remote_runtime.writer_code_digest",
                return_value=CODE_DIGEST,
            ),
        ):
            payload = build_manifest(
                observation,
                mineru_bin=Path("/private/mineru"),
                ssh_host_key_sha256="sha256:" + "6" * 64,
                api_url="http://127.0.0.1:30002",
                observability_url="http://127.0.0.1:30001/v1",
                inference_upstream_url="http://mineru-openai-server:30000/v1",
                expected_compose_sha256="sha256:" + "3" * 64,
                expected_collector_sha256="sha256:" + "7" * 64,
            )

        self.assertEqual(
            payload["identity_sha256"],
            canonical_payload_sha256(payload["manifest"]),
        )
        self.assertEqual(
            payload["manifest"]["topology"]["windows_node_identity_sha256"],
            "sha256:" + "5" * 64,
        )

    def test_build_manifest_rejects_exposed_or_busy_remote(self) -> None:
        for tamper in (
            "network",
            "busy",
            "image",
            "mount",
            "output",
            "collector",
            "model",
            "vllm",
        ):
            with self.subTest(tamper=tamper):
                observation = _observation()
                if tamper == "network":
                    observation["proxy"]["networks"] = ["public"]
                elif tamper == "busy":
                    observation["api_health"]["processing_tasks"] = 1
                elif tamper == "image":
                    observation["api"]["image_id"] = "sha256:" + "0" * 64
                elif tamper == "mount":
                    observation["api"]["mounts"][0]["Source"] = r"C:\temp"
                elif tamper == "output":
                    observation["output_root"]["file_count"] = 1
                elif tamper == "collector":
                    observation["collector_path"] = r"C:\temp\collector.ps1"
                elif tamper == "model":
                    observation["served_model"]["id"] = (
                        "/unrelated/models--evil--Other/snapshots/" + "0" * 40
                    )
                else:
                    observation["served_model"]["vllm_version"] = "99.0"
                with (
                    patch(
                        "scripts.attest_mineru_remote_runtime.client_bundle_identity",
                        return_value=CLIENT,
                    ),
                    patch(
                        "scripts.attest_mineru_remote_runtime.writer_code_digest",
                        return_value=CODE_DIGEST,
                    ),
                    self.assertRaises(ValueError),
                ):
                    build_manifest(
                        observation,
                        mineru_bin=Path("/private/mineru"),
                        ssh_host_key_sha256="sha256:" + "6" * 64,
                        api_url="http://127.0.0.1:30002",
                        observability_url="http://127.0.0.1:30001/v1",
                        inference_upstream_url=(
                            "http://mineru-openai-server:30000/v1"
                        ),
                        expected_compose_sha256="sha256:" + "3" * 64,
                        expected_collector_sha256="sha256:" + "7" * 64,
                    )

    def test_known_hosts_identity_hashes_the_exact_ed25519_blob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "known_hosts"
            blob = b"canonical-ed25519-key-blob"
            path.write_text(
                "100.64.0.1 ssh-ed25519 "
                + base64.b64encode(blob).decode()
                + "\n"
            )
            path.chmod(0o600)

            observed = _known_host_key_sha256(
                path, expected_host="100.64.0.1"
            )

        self.assertEqual(observed, "sha256:" + __import__("hashlib").sha256(blob).hexdigest())

    def test_ssh_base_excludes_global_known_hosts_and_rejects_options(self) -> None:
        command = _ssh_base(
            host="100.64.0.1",
            user="help",
            port=22,
            identity_file=Path("/private/key"),
            known_hosts_file=Path("/private/known_hosts"),
        )
        self.assertIn("GlobalKnownHostsFile=/dev/null", command)
        self.assertEqual(command[-2:], ["--", "help@100.64.0.1"])
        with self.assertRaises(ValueError):
            _ssh_base(
                host="-oProxyCommand=evil",
                user="help",
                port=22,
                identity_file=Path("/private/key"),
                known_hosts_file=Path("/private/known_hosts"),
            )


if __name__ == "__main__":
    unittest.main()
