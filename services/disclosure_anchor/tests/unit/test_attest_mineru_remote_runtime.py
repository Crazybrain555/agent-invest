"""Remote MinerU v7 attestation unit tests; no SSH/GPU access."""

from __future__ import annotations

import base64
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import yaml

from scripts.attest_mineru_remote_runtime import (
    EXPECTED_API_COMPAT_IMAGE,
    EXPECTED_COLLECTOR_PATH,
    EXPECTED_COMPAT_PREIMAGES,
    EXPECTED_IMAGE_ID,
    EXPECTED_MODEL_REVISION,
    EXPECTED_REPO_DIGEST,
    _known_host_key_sha256,
    _canonical_remote_collector_path,
    _expected_serial_runtime,
    _read_remote_file,
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
PATCHER_DIGEST = "sha256:" + "8" * 64
DOCKERFILE_DIGEST = "sha256:" + "9" * 64
API_IMAGE_ID = "sha256:" + "a" * 64


def _observation() -> dict[str, Any]:
    api_environment = [
        "MINERU_MODEL_SOURCE=local",
        "MINERU_MALLOC_TRIM=1",
        "MINERU_PHASE_TRACE=0",
        "MINERU_HYBRID_BATCH_RATIO=1",
        "MINERU_ENABLE_PIPELINE_INFERENCE_LOCKS=1",
        "MINERU_API_MAX_CONCURRENT_REQUESTS=1",
        "MINERU_API_MAX_PENDING_TASKS=1",
        "MINERU_PROCESSING_WINDOW_SIZE=16",
        "MINERU_API_OUTPUT_ROOT=/var/lib/mineru-api-output",
        "MINERU_API_TASK_RETENTION_SECONDS=600",
        "MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS=30",
        "MINERU_API_DISABLE_ACCESS_LOG=true",
        "MINERU_API_ENABLE_FASTAPI_DOCS=false",
    ]
    api_environment_map = {
        item.partition("=")[0]: item.partition("=")[2] for item in api_environment
    }
    return {
        "schema": "mineru-windows-runtime-observation.v3",
        "collector_path": EXPECTED_COLLECTOR_PATH,
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
            "image": EXPECTED_API_COMPAT_IMAGE,
            "image_id": API_IMAGE_ID,
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
            "environment": api_environment_map,
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
        "api_compatibility": {
            "marker": {
                "schema": "mineru-runtime-compatibility.v4",
                "policy": "glibc-malloc-trim-per-window.v1",
                "capacity_policy": "single-owner-serial-mineru.v1",
                "mineru_version": "3.4.4",
                "mineru_vl_utils_version": "1.0.5",
                "base_image_digest": EXPECTED_IMAGE_ID,
                "patcher_sha256": PATCHER_DIGEST,
                "preimage_sha256": EXPECTED_COMPAT_PREIMAGES,
                "patched_source_sha256": {
                    path: "sha256:" + character * 64
                    for path, character in zip(
                        EXPECTED_COMPAT_PREIMAGES,
                        ("b", "c", "d", "e", "f", "1"),
                        strict=True,
                    )
                },
            },
            "capacity_runtime": _expected_serial_runtime(api_environment_map),
            "actual_source_sha256": {
                path: "sha256:" + character * 64
                for path, character in zip(
                    EXPECTED_COMPAT_PREIMAGES,
                    ("b", "c", "d", "e", "f", "1"),
                    strict=True,
                )
            },
            "heap_trim_enabled": True,
            "phase_trace_enabled": False,
            "hybrid_batch_ratio_requested": 1,
            "max_pending_tasks_requested": 1,
            "max_pending_tasks_effective": 1,
            "pipeline_inference_locks_enabled": True,
            "image_labels": {
                "io.agent-invest.mineru.base-image-digest": EXPECTED_IMAGE_ID,
                "io.agent-invest.mineru.capacity-policy": (
                    "single-owner-serial-mineru.v1"
                ),
                "io.agent-invest.mineru.compatibility-policy": (
                    "glibc-malloc-trim-per-window.v1"
                ),
                "io.agent-invest.mineru.compatibility-patcher-sha256": (
                    PATCHER_DIGEST
                ),
                "io.agent-invest.mineru.compatibility-dockerfile-sha256": (
                    DOCKERFILE_DIGEST
                ),
            },
        },
        "proxy": {
            "image": EXPECTED_REPO_DIGEST,
            "image_id": EXPECTED_IMAGE_ID,
            "entrypoint": ["/usr/bin/python3.12"],
            "command": [
                "-I",
                "-c",
                yaml.safe_load(
                    (
                        Path(__file__).resolve().parents[2]
                        / "config"
                        / "mineru-windows.compose.yaml"
                    ).read_text()
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
            "max_concurrent_requests": 1,
            "max_pending_tasks_requested": 1,
            "max_pending_tasks_effective": 1,
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
                expected_compat_patcher_sha256=PATCHER_DIGEST,
                expected_compat_dockerfile_sha256=DOCKERFILE_DIGEST,
            )

        self.assertEqual(
            payload["identity_sha256"],
            canonical_payload_sha256(payload["manifest"]),
        )
        self.assertEqual(
            payload["manifest"]["topology"]["windows_node_identity_sha256"],
            "sha256:" + "5" * 64,
        )
        self.assertEqual(
            payload["manifest"]["topology"]["windows_collector_path"],
            EXPECTED_COLLECTOR_PATH,
        )
        self.assertEqual(
            payload["manifest"]["topology"]["windows_collector_sha256"],
            "sha256:" + "7" * 64,
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
            "compatibility",
            "serial_runtime",
            "pending_health_drift",
            "pending_compatibility_drift",
        ):
            with self.subTest(tamper=tamper):
                observation = _observation()
                if tamper == "network":
                    observation["proxy"]["networks"] = ["public"]
                elif tamper == "busy":
                    observation["api_health"]["processing_tasks"] = 1
                elif tamper == "image":
                    observation["api"]["image"] = EXPECTED_REPO_DIGEST
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
                elif tamper == "vllm":
                    observation["served_model"]["vllm_version"] = "99.0"
                elif tamper == "serial_runtime":
                    observation["api_compatibility"]["capacity_runtime"][
                        "profile_sha256"
                    ] = "sha256:" + "0" * 64
                elif tamper == "pending_health_drift":
                    observation["api_health"]["max_pending_tasks_effective"] = 2
                elif tamper == "pending_compatibility_drift":
                    observation["api_compatibility"][
                        "max_pending_tasks_effective"
                    ] = 2
                else:
                    observation["api_compatibility"]["heap_trim_enabled"] = False
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
                        inference_upstream_url=("http://mineru-openai-server:30000/v1"),
                        expected_compose_sha256="sha256:" + "3" * 64,
                        expected_collector_sha256="sha256:" + "7" * 64,
                        expected_compat_patcher_sha256=PATCHER_DIGEST,
                        expected_compat_dockerfile_sha256=DOCKERFILE_DIGEST,
                    )

    def test_known_hosts_identity_hashes_the_exact_ed25519_blob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "known_hosts"
            blob = b"canonical-ed25519-key-blob"
            path.write_text(
                "100.64.0.1 ssh-ed25519 " + base64.b64encode(blob).decode() + "\n"
            )
            path.chmod(0o600)

            observed = _known_host_key_sha256(path, expected_host="100.64.0.1")

        self.assertEqual(
            observed, "sha256:" + __import__("hashlib").sha256(blob).hexdigest()
        )

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

    def test_remote_collector_path_rejects_ambiguous_or_unversioned_targets(
        self,
    ) -> None:
        self.assertEqual(
            _canonical_remote_collector_path(EXPECTED_COLLECTOR_PATH),
            EXPECTED_COLLECTOR_PATH,
        )
        for value in (
            r"\\server\share\collect_mineru_runtime.ps1",
            r"\\?\C:\ProgramData\agent-invest\mineru-runtime-v6\collect_mineru_runtime.ps1",
            r"C:\ProgramData\agent-invest\mineru-runtime-v6\..\collect_mineru_runtime.ps1",
            EXPECTED_COLLECTOR_PATH + ":evil",
            r"C:\ProgramData\agent-invest\mineru\collect_mineru_runtime.ps1",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _canonical_remote_collector_path(value)

    def test_remote_file_read_checks_full_path_and_reparse_chain(self) -> None:
        encoded = base64.b64encode(b"collector-bytes").decode()
        with patch(
            "scripts.attest_mineru_remote_runtime.subprocess.run"
        ) as run:
            run.return_value.stdout = encoded
            observed = _read_remote_file(
                ["ssh"],
                remote_path=EXPECTED_COLLECTOR_PATH,
                allowed_remote_path=EXPECTED_COLLECTOR_PATH,
            )

        self.assertEqual(observed, b"collector-bytes")
        command = run.call_args.args[0][-1]
        self.assertIn("ReparsePoint", command)
        self.assertIn("FullName.Equals", command)


if __name__ == "__main__":
    unittest.main()
