"""DB-free MinerU canary and runtime-attestation regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from disclosure_anchor.adapters.runtime.mineru_canary import (
    MinerUCanaryError,
    MinerUCanaryUnavailableError,
    canary_cache_is_fresh,
    canary_request_sha256,
    probe_mineru_served_model,
    run_mineru_multimodal_canary,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_CONTENT_PACKAGE_VERSIONS,
    MINERU_PROCESSING_WINDOW_SIZE,
    MINERU_WINDOWS_COLLECTOR_PATH,
    MINERU_WINDOWS_COMPOSE_PATH,
    MinerUClientIdentity,
    client_bundle_identity,
)
from scripts.mineru_smoke import _runtime_manifest, run_cli


_LOCAL_DIGEST = "sha256:" + "c" * 64
_CODE_DIGEST = "sha256:" + "9" * 64


def _client_identity() -> MinerUClientIdentity:
    return MinerUClientIdentity(
        package_set_sha256=_LOCAL_DIGEST,
        python_version="3.13.7",
        content_package_versions=dict(MINERU_CONTENT_PACKAGE_VERSIONS),
    )


def _manifest(*, duplicate_max_num_seqs: bool = False) -> dict[str, object]:
    command = [
        "mineru-openai-server",
        "--max-num-seqs",
        "128",
    ]
    if duplicate_max_num_seqs:
        command.extend(["--max-num-seqs", "4096"])
    command.extend(["--mm-processor-cache-gb", "0"])
    return {
        "contract_version": "mineru-runtime-bundle.v6",
        "client": {
            "package_set_sha256": _LOCAL_DIGEST,
            "writer_code_sha256": _CODE_DIGEST,
            **MINERU_CONTENT_PACKAGE_VERSIONS,
        },
        "orchestrator": {
            "container_image_digest": "sha256:" + "1" * 64,
            "base_container_image_digest": "sha256:" + "0" * 64,
            "content_environment_sha256": "sha256:" + "2" * 64,
            "service_config_sha256": "sha256:" + "3" * 64,
            "mount_policy_sha256": "sha256:" + "4" * 64,
            "network_policy_sha256": "sha256:" + "5" * 64,
            "heap_return_compatibility_sha256": "sha256:" + "6" * 64,
            "capacity_runtime_compatibility_sha256": "sha256:" + "7" * 64,
            "heap_return_policy": "glibc-malloc-trim-per-window.v1",
            "mineru_version": "3.4.4",
            "api_protocol_version": 2,
            "max_concurrent_requests": 3,
            "inference_max_concurrency": 7,
            "processing_window_size": 16,
            "task_retention_seconds": 600,
            "task_cleanup_interval_seconds": 30,
            "output_root_policy": "dedicated-scratch-retention.v1",
            "command": ["mineru-api", "--max-concurrency", "7"],
        },
        "inference_server": {
            "container_image_digest": "sha256:" + "d" * 64,
            "content_environment_sha256": "sha256:" + "e" * 64,
            "server_config_sha256": "sha256:" + "f" * 64,
            "mineru_version": "3.4.4",
            "max_model_len": 8192,
            "model_repository": "provider/model",
            "served_model_id": "provider/model",
            "model_snapshot_revision": "1" * 40,
            "vllm_version": "0.21.0",
            "command": command,
        },
        "topology": {
            "api_transport": "pinned-ssh-local-forward.v1",
            "api_exposure": "windows-loopback-only.v1",
            "orchestrator_egress_policy": "dedicated-internal-vllm-only.v1",
            "api_endpoint_sha256": "sha256:" + "6" * 64,
            "observability_endpoint_sha256": "sha256:" + "7" * 64,
            "inference_upstream_sha256": "sha256:" + "8" * 64,
            "ssh_host_key_sha256": "sha256:" + "9" * 64,
            "windows_node_identity_sha256": "sha256:" + "a" * 64,
            "windows_compose_path": MINERU_WINDOWS_COMPOSE_PATH,
            "windows_compose_sha256": "sha256:" + "b" * 64,
            "windows_collector_path": MINERU_WINDOWS_COLLECTOR_PATH,
            "windows_collector_sha256": "sha256:" + "c" * 64,
        },
    }


def _manifest_payload(manifest: dict[str, object]) -> tuple[dict[str, object], str]:
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    identity = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return {"identity_sha256": identity, "manifest": manifest}, identity


class MinerUCanaryTests(unittest.TestCase):
    def test_smoke_cli_persists_new_only_redacted_failure_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.mineru_smoke.main",
            side_effect=SystemExit(
                "[abort] remote https://user:secret@gpu.invalid failed"
            ),
        ):
            receipt = Path(tmp) / "smoke-fail.json"
            with self.assertRaises(SystemExit):
                run_cli(["--receipt-out", str(receipt)])
            payload = json.loads(receipt.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["database_access"], "none")
        self.assertIn("started_at_utc", payload)
        self.assertIn("finished_at_utc", payload)
        self.assertEqual(payload["attempt"]["input"]["status"], "observed")
        self.assertEqual(payload["cleanup"]["status"], "not_proved")
        self.assertNotIn("secret", json.dumps(payload))
        self.assertIn("<redacted-url>", payload["failure"]["detail"])

    def test_client_identity_measures_named_content_packages(self) -> None:
        listing = {
            "python_version": "3.13.7",
            "packages": [
                "mineru==3.4.4",
                "mineru-vl-utils==1.0.5",
                "pdftext==0.6.3",
                "pypdfium2==4.30.0",
            ],
        }
        result = MagicMock(stdout=json.dumps(listing))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mineru_bin = root / "mineru"
            mineru_bin.write_text("", encoding="utf-8")
            (root / "python").write_text("", encoding="utf-8")
            with patch(
                "disclosure_anchor.adapters.runtime.mineru_identity.subprocess.run",
                return_value=result,
            ) as run:
                identity = client_bundle_identity(mineru_bin)

        self.assertEqual(
            dict(identity.content_package_versions),
            MINERU_CONTENT_PACKAGE_VERSIONS,
        )
        self.assertRegex(identity.package_set_sha256, r"^sha256:[a-f0-9]{64}$")
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["-I", "-c"])

    def test_repeated_canary_binds_request_and_every_response(self) -> None:
        models = MagicMock()
        models.__enter__.return_value.read.return_value = json.dumps(
            {"data": [{"id": "mineru-model"}]}
        ).encode()
        completions = []
        for index in range(3):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                {
                    "choices": [
                        {
                            "index": index,
                            "message": {
                                "role": "assistant",
                                "content": "M7." if index == 2 else "M7",
                            },
                        }
                    ]
                }
            ).encode()
            completions.append(response)
        opener = MagicMock()
        opener.open.side_effect = [models, *completions]

        with patch(
            "disclosure_anchor.adapters.runtime.mineru_canary.urllib.request.build_opener",
            return_value=opener,
        ):
            evidence = run_mineru_multimodal_canary(
                "http://gpu:30000",
                attempts=3,
                expected_model_id="mineru-model",
            )

        self.assertEqual(evidence.attempts, 3)
        self.assertEqual(len(evidence.response_sha256), 3)
        self.assertEqual(
            evidence.request_sha256,
            canary_request_sha256("mineru-model"),
        )
        self.assertEqual(opener.open.call_count, 4)

    def test_canary_rejects_expected_token_with_extra_text(self) -> None:
        models = MagicMock()
        models.__enter__.return_value.read.return_value = json.dumps(
            {"data": [{"id": "mineru-model"}]}
        ).encode()
        completion = MagicMock()
        completion.__enter__.return_value.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "M7 is shown",
                        }
                    }
                ]
            }
        ).encode()
        opener = MagicMock()
        opener.open.side_effect = [models, completion]

        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_canary.urllib.request.build_opener",
                return_value=opener,
            ),
            self.assertRaisesRegex(MinerUCanaryError, "exact expected M7"),
        ):
            run_mineru_multimodal_canary("http://gpu:30000")

    def test_cache_requires_same_endpoint_runtime_and_fresh_time(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=UTC)
        payload = {
            "schema": "mineru_multimodal_canary.v2",
            "passed_at_utc": (now - timedelta(seconds=30)).isoformat(),
            "observability_endpoint_sha256": hashlib.sha256(
                b"http://gpu:30000"
            ).hexdigest(),
            "runtime_bundle_identity_sha256": "sha256:" + "a" * 64,
            "model_id_sha256": "c" * 64,
            "request_sha256": "d" * 64,
            "attempts": 1,
            "response_sha256": ["b" * 64],
        }
        self.assertTrue(
            canary_cache_is_fresh(
                payload,
                observability_url="http://gpu:30000/",
                runtime_bundle_identity_sha256="sha256:" + "a" * 64,
                max_age_seconds=60,
                now=now,
            )
        )
        self.assertFalse(
            canary_cache_is_fresh(
                payload,
                observability_url="http://other:30000",
                runtime_bundle_identity_sha256="sha256:" + "a" * 64,
                max_age_seconds=60,
                now=now,
            )
        )

    def test_runtime_manifest_separates_local_and_remote_identity(self) -> None:
        manifest = _manifest()
        payload, configured = _manifest_payload(manifest)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded, orchestrator_identity, remote_identity = _runtime_manifest(
                path,
                configured_identity=configured,
                local_client_identity=_client_identity(),
                local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
                local_writer_code_digest=_CODE_DIGEST,
            )

        self.assertEqual(loaded, manifest)
        self.assertRegex(orchestrator_identity, r"^sha256:[a-f0-9]{64}$")
        self.assertRegex(remote_identity, r"^sha256:[a-f0-9]{64}$")
        self.assertNotEqual(remote_identity, configured)

    def test_models_root_must_be_an_object(self) -> None:
        models = MagicMock()
        models.__enter__.return_value.read.return_value = b"[]"
        opener = MagicMock()
        opener.open.return_value = models
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_canary.urllib.request.build_opener",
                return_value=opener,
            ),
            self.assertRaisesRegex(MinerUCanaryError, "root must be an object"),
        ):
            run_mineru_multimodal_canary("http://gpu:30000")

    def test_completion_root_must_be_an_object(self) -> None:
        models = MagicMock()
        models.__enter__.return_value.read.return_value = json.dumps(
            {"data": [{"id": "mineru-model"}]}
        ).encode()
        completion = MagicMock()
        completion.__enter__.return_value.read.return_value = b"[]"
        opener = MagicMock()
        opener.open.side_effect = [models, completion]
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_canary.urllib.request.build_opener",
                return_value=opener,
            ),
            self.assertRaisesRegex(MinerUCanaryError, "root must be an object"),
        ):
            run_mineru_multimodal_canary("http://gpu:30000")

    def test_canary_rejects_empty_assistant_payload(self) -> None:
        models = MagicMock()
        models.__enter__.return_value.read.return_value = json.dumps(
            {"data": [{"id": "mineru-model"}]}
        ).encode()
        completion = MagicMock()
        completion.__enter__.return_value.read.return_value = json.dumps(
            {"choices": [{}]}
        ).encode()
        opener = MagicMock()
        opener.open.side_effect = [models, completion]

        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_canary.urllib.request.build_opener",
                return_value=opener,
            ),
            self.assertRaisesRegex(MinerUCanaryError, "no assistant message"),
        ):
            run_mineru_multimodal_canary("http://gpu:30000")

    def test_light_probe_rejects_model_drift(self) -> None:
        models = MagicMock()
        models.__enter__.return_value.read.return_value = json.dumps(
            {"data": [{"id": "replacement-model"}]}
        ).encode()
        opener = MagicMock()
        opener.open.return_value = models
        with (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_canary.urllib.request.build_opener",
                return_value=opener,
            ),
            self.assertRaisesRegex(MinerUCanaryError, "drifted"),
        ):
            probe_mineru_served_model(
                "http://gpu:30000",
                expected_model_id="attested-model",
            )

    def test_light_probe_classifies_http_statuses(self) -> None:
        opener = MagicMock()
        with patch(
            "disclosure_anchor.adapters.runtime.mineru_canary.urllib.request.build_opener",
            return_value=opener,
        ):
            for status, expected_error in (
                (404, MinerUCanaryError),
                (503, MinerUCanaryUnavailableError),
                (501, MinerUCanaryError),
            ):
                with self.subTest(status=status):
                    opener.open.side_effect = urllib.error.HTTPError(
                        "http://gpu:30000/v1/models",
                        status,
                        "probe failed",
                        {},
                        None,
                    )
                    with self.assertRaises(expected_error):
                        probe_mineru_served_model("http://gpu:30000")

    def test_runtime_manifest_rejects_duplicate_safety_flag(self) -> None:
        manifest = _manifest(duplicate_max_num_seqs=True)
        payload, configured = _manifest_payload(manifest)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly once"):
                _runtime_manifest(
                    path,
                    configured_identity=configured,
                    local_client_identity=_client_identity(),
                    local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
                    local_writer_code_digest=_CODE_DIGEST,
                )

    def test_runtime_manifest_rejects_credential_command_flag(self) -> None:
        for credential_tokens in (
            ["--api-key", "do-not-record"],
            ["--api-key=do-not-record"],
            ["--hf-token=do-not-record"],
            ["--password=do-not-record"],
            ["--access_token=do-not-record"],
        ):
            with self.subTest(credential_tokens=credential_tokens):
                manifest = _manifest()
                server = manifest["inference_server"]
                assert isinstance(server, dict)
                command = server["command"]
                assert isinstance(command, list)
                command.extend(credential_tokens)
                payload, configured = _manifest_payload(manifest)
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "runtime.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "credentials"):
                        _runtime_manifest(
                            path,
                            configured_identity=configured,
                            local_client_identity=_client_identity(),
                            local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
                            local_writer_code_digest=_CODE_DIGEST,
                        )

    def test_runtime_manifest_rejects_unmeasured_package_claim(self) -> None:
        client = _client_identity()
        drifted = MinerUClientIdentity(
            package_set_sha256=client.package_set_sha256,
            python_version=client.python_version,
            content_package_versions={
                **client.content_package_versions,
                "pdftext_version": "9.9.9",
            },
        )
        manifest = _manifest()
        payload, configured = _manifest_payload(manifest)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be pinned"):
                _runtime_manifest(
                    path,
                    configured_identity=configured,
                    local_client_identity=drifted,
                    local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
                    local_writer_code_digest=_CODE_DIGEST,
                )


if __name__ == "__main__":
    unittest.main()
