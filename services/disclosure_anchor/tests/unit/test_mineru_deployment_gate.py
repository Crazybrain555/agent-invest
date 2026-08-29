"""Resident-worker MinerU deployment gate regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mineru_canary import (
    canary_request_sha256,
    model_id_sha256,
)
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    MinerUDeploymentChecker,
    MinerUDeploymentGateError,
    MinerUDeploymentUnavailableError,
    require_mineru_deployment_gate,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_CONTENT_PACKAGE_VERSIONS,
    MINERU_PROCESSING_WINDOW_SIZE,
    MINERU_SMOKE_INPUT_NAME,
    MINERU_SMOKE_INPUT_SHA256,
    MINERU_WINDOWS_COLLECTOR_PATH,
    MINERU_WINDOWS_COMPOSE_PATH,
    MinerUClientIdentity,
    canonical_payload_sha256,
)
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorHealth,
    finish_mineru_orchestrator_incident,
    mark_mineru_orchestrator_incident,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from disclosure_anchor.settings import Settings


LOCAL_DIGEST = "sha256:" + "1" * 64
CODE_DIGEST = "sha256:" + "2" * 64
MODEL_ID = "provider/model"


def endpoint_sha256(value: str, *, prefixed: bool) -> str:
    digest = hashlib.sha256(value.rstrip("/").encode()).hexdigest()
    return ("sha256:" if prefixed else "") + digest


def health(*, completed: int, queued: int = 0) -> dict[str, object]:
    return {
        "status": "healthy",
        "version": "3.4.4",
        "protocol_version": 2,
        "queued_tasks": queued,
        "processing_tasks": 0,
        "completed_tasks": completed,
        "failed_tasks": 0,
        "max_concurrent_requests": 1,
        "max_pending_tasks_requested": 1,
        "max_pending_tasks_effective": 1,
        "processing_window_size": 16,
        "task_retention_seconds": 600,
        "task_cleanup_interval_seconds": 30,
    }


class MinerUDeploymentGateTests(unittest.TestCase):
    def _fixture(
        self, root: Path, *, now: datetime
    ) -> tuple[Settings, MinerUClientIdentity, dict[str, object]]:
        service = root / "service"
        shared = root / "shared"
        for path in (service, service / "runtime", shared):
            path.mkdir(parents=True, exist_ok=True)
        mineru = root / "mineru"
        mineru.write_text("executable", encoding="utf-8")
        api_url = "http://127.0.0.1:30002"
        observability_url = "http://127.0.0.1:30001/v1"
        inference_url = "http://mineru-openai-server:30000/v1"
        client = MinerUClientIdentity(
            package_set_sha256=LOCAL_DIGEST,
            python_version="3.13.7",
            content_package_versions=dict(MINERU_CONTENT_PACKAGE_VERSIONS),
        )
        server = {
            "container_image_digest": "sha256:" + "d" * 64,
            "content_environment_sha256": "sha256:" + "e" * 64,
            "server_config_sha256": "sha256:" + "f" * 64,
            "mineru_version": "3.4.4",
            "max_model_len": 8192,
            "model_repository": "provider/model",
            "served_model_id": MODEL_ID,
            "model_snapshot_revision": "3" * 40,
            "vllm_version": "0.21.0",
            "command": [
                "mineru-openai-server",
                "--max-num-seqs",
                "128",
                "--mm-processor-cache-gb",
                "0",
            ],
        }
        orchestrator = {
            "container_image_digest": "sha256:" + "6" * 64,
            "base_container_image_digest": "sha256:" + "5" * 64,
            "content_environment_sha256": "sha256:" + "7" * 64,
            "service_config_sha256": "sha256:" + "8" * 64,
            "mount_policy_sha256": "sha256:" + "9" * 64,
            "network_policy_sha256": "sha256:" + "a" * 64,
            "heap_return_compatibility_sha256": "sha256:" + "f" * 64,
            "capacity_runtime_compatibility_sha256": "sha256:" + "0" * 64,
            "heap_return_policy": "glibc-malloc-trim-per-window.v1",
            "mineru_version": "3.4.4",
            "api_protocol_version": 2,
            "max_concurrent_requests": 1,
            "max_pending_tasks_requested": 1,
            "max_pending_tasks_effective": 1,
            "inference_max_concurrency": 7,
            "hybrid_batch_ratio": 1,
            "pipeline_inference_locks": True,
            "processing_window_size": 16,
            "task_retention_seconds": 600,
            "task_cleanup_interval_seconds": 30,
            "output_root_policy": "dedicated-scratch-retention.v1",
            "command": ["mineru-api", "--max-concurrency", "7"],
        }
        topology = {
            "api_transport": "pinned-ssh-local-forward.v1",
            "api_exposure": "windows-loopback-only.v1",
            "orchestrator_egress_policy": "dedicated-internal-vllm-only.v1",
            "api_endpoint_sha256": endpoint_sha256(api_url, prefixed=True),
            "observability_endpoint_sha256": endpoint_sha256(
                observability_url, prefixed=True
            ),
            "inference_upstream_sha256": endpoint_sha256(
                inference_url, prefixed=True
            ),
            "ssh_host_key_sha256": "sha256:" + "b" * 64,
            "windows_node_identity_sha256": "sha256:" + "c" * 64,
            "windows_compose_path": MINERU_WINDOWS_COMPOSE_PATH,
            "windows_compose_sha256": "sha256:" + "d" * 64,
            "windows_collector_path": MINERU_WINDOWS_COLLECTOR_PATH,
            "windows_collector_sha256": "sha256:" + "e" * 64,
        }
        manifest = {
            "contract_version": "mineru-runtime-bundle.v8",
            "client": {
                "package_set_sha256": LOCAL_DIGEST,
                "writer_code_sha256": CODE_DIGEST,
                **MINERU_CONTENT_PACKAGE_VERSIONS,
            },
            "orchestrator": orchestrator,
            "inference_server": server,
            "topology": topology,
        }
        runtime_identity = canonical_payload_sha256(manifest)
        identity = {
            "local_client_identity_sha256": LOCAL_DIGEST,
            "local_content_package_versions": dict(MINERU_CONTENT_PACKAGE_VERSIONS),
            "local_processing_window_size": MINERU_PROCESSING_WINDOW_SIZE,
            "local_writer_code_sha256": CODE_DIGEST,
            "runtime_manifest_identity_sha256": runtime_identity,
            "orchestrator_runtime_identity_sha256": canonical_payload_sha256(
                orchestrator
            ),
            "provider_runtime_identity_sha256": canonical_payload_sha256(server),
            "served_model_id": MODEL_ID,
            "orchestrator_task_slots": 1,
        }
        target = ParserTargetIdentity(
            name="MinerU",
            package_version="3.4.4",
            backend="hybrid-http-client",
            method="auto",
            language="ch",
            formula=True,
            table=True,
            effort="medium",
            runtime_bundle_identity_sha256=runtime_identity,
        )

        def canary(at: datetime, marker: str) -> dict[str, object]:
            return {
                "schema": "mineru_multimodal_canary.v2",
                "passed_at_utc": at.isoformat(),
                "observability_endpoint_sha256": endpoint_sha256(
                    observability_url, prefixed=False
                ),
                "runtime_bundle_identity_sha256": runtime_identity,
                "model_id_sha256": model_id_sha256(MODEL_ID),
                "attempts": 3,
                "request_sha256": canary_request_sha256(MODEL_ID),
                "response_sha256": [marker * 64, "e" * 64, "f" * 64],
            }

        def smoke(
            *, index: int, profile: str, start: datetime, pages: int
        ) -> dict[str, object]:
            source_sha = (
                MINERU_SMOKE_INPUT_SHA256
                if profile == "deployment_frozen_v1"
                else "sha256:" + f"{index:064x}"
            )
            return {
                "schema": "mineru_smoke_receipt.v5",
                "status": "pass",
                "started_at_utc": start.isoformat(),
                "finished_at_utc": (start + timedelta(seconds=2)).isoformat(),
                "elapsed_seconds": 2.0,
                "database_access": "none",
                "queue_access": "none",
                "input": {
                    "profile": profile,
                    "logical_name": (
                        MINERU_SMOKE_INPUT_NAME
                        if profile == "deployment_frozen_v1"
                        else f"heldout-{index}.pdf"
                    ),
                    "sha256": source_sha,
                    "bytes": 1000 + index,
                    "page_count": pages,
                },
                "identity": identity,
                "runtime_manifest": manifest,
                "canary": canary(start + timedelta(seconds=1), str(index)),
                "topology": {
                    key: topology[key]
                    for key in (
                        "api_endpoint_sha256",
                        "observability_endpoint_sha256",
                        "inference_upstream_sha256",
                    )
                },
                "orchestrator": {
                    "task_registry_semantics": "retained-terminal-gauges.v1",
                    "before": health(completed=index),
                    "after": health(completed=index + 1),
                    "terminal_active_tasks": 0,
                    "stop_semantics": "drain-not-cancel.v1",
                },
                "provider": {
                    "target_identity": target.to_payload(),
                    "provider_bundle_sha256": "sha256:" + f"{index + 10:064x}",
                    "page_count": pages,
                    "block_count": 2,
                    "artifact_count": 0,
                },
                "cleanup": {
                    "external_api_temp_dirs_created": 0,
                    "external_mineru_processes_after": 0,
                    "temporary_tree_removed": True,
                    "retained_parse_artifacts": 0,
                    "remote_active_tasks_after": 0,
                },
            }

        main_smoke = smoke(
            index=1,
            profile="deployment_frozen_v1",
            start=now - timedelta(seconds=60),
            pages=1,
        )
        heldout = [
            smoke(
                index=index,
                profile="diagnostic_custom",
                start=now - timedelta(seconds=55 - index * 5),
                pages=10 + index,
            )
            for index in (2, 3)
        ]
        service_epoch = {
            "schema": "mineru-service-epoch.v1",
            "runtime_manifest_identity_sha256": runtime_identity,
            "collector_sha256": topology["windows_collector_sha256"],
            "windows_node_identity_sha256": topology[
                "windows_node_identity_sha256"
            ],
            "windows_compose_sha256": topology["windows_compose_sha256"],
            "writer_code_sha256": manifest["client"]["writer_code_sha256"],
            "api_image_digest": manifest["orchestrator"]["container_image_digest"],
            "container_epoch_sha256": "sha256:" + "8" * 64,
            "api_container_id": "9" * 64,
        }

        def epoch(created: datetime) -> dict[str, object]:
            return {
                "schema": "mineru-service-epoch-freeze.v2",
                "status": "pass",
                "created_at_utc": created.isoformat(),
                "database_access": "none",
                "queue_access": "none",
                "service_epoch": service_epoch,
                "service_epoch_sha256": canonical_payload_sha256(service_epoch),
                "safety": {
                    "restart_count_total": 0,
                    "oom_killed_count": 0,
                    "unsafe_container_count": 0,
                    "cgroup_oom_total": 0,
                    "cgroup_oom_kill_total": 0,
                },
            }

        def wrapped(payload: dict[str, object]) -> dict[str, object]:
            return {
                "receipt_sha256": canonical_payload_sha256(payload),
                "source_bytes_sha256": "sha256:" + "a" * 64,
                "receipt": payload,
            }

        validation = {
            "schema": "mineru_heldout_validation_receipt.v1",
            "status": "pass",
            "created_at_utc": (now - timedelta(seconds=30)).isoformat(),
            "policy": "operator-held-out-complete-pdf.v1",
            "database_access": "none",
            "queue_access": "none",
            "document_count": 2,
            "documents": [wrapped(item) for item in heldout],
            "epoch_before": wrapped(epoch(now - timedelta(seconds=50))),
            "epoch_after": wrapped(epoch(now - timedelta(seconds=31))),
        }
        smoke_path = root / "smoke.json"
        cache_path = root / "cache.json"
        validation_path = root / "validation.json"
        for path, payload in (
            (smoke_path, main_smoke),
            (cache_path, main_smoke["canary"]),
            (validation_path, validation),
        ):
            path.write_text(json.dumps(payload), encoding="utf-8")
            path.chmod(0o600)
        settings = Settings(
            disclosure_data_root=service,
            disclosure_shared_root=shared,
            disclosure_runtime_root=service / "runtime",
            mineru_model_cache=shared / "mineru-cache",
            hf_home=shared / "hf",
            modelscope_cache=shared / "modelscope",
            mineru_processing_window_size=16,
            disclosure_mineru_bin=mineru,
            disclosure_mineru_api_url=api_url,
            disclosure_mineru_observability_url=observability_url,
            disclosure_mineru_inference_upstream_url=inference_url,
            disclosure_mineru_runtime_bundle_identity_sha256=runtime_identity,
            disclosure_mineru_smoke_receipt=smoke_path,
            disclosure_mineru_canary_cache=cache_path,
            disclosure_mineru_validation_receipt=validation_path,
            worker_parse_concurrency=16,
            worker_mineru_client_outstanding_window=1,
            worker_gpu_request_budget=7,
            worker_gpu_max_sequences=128,
        )
        return settings, client, validation

    @staticmethod
    def _identity_patches(client: MinerUClientIdentity) -> tuple[object, object]:
        return (
            patch(
                "disclosure_anchor.adapters.runtime.mineru_deployment_gate.client_bundle_identity",
                return_value=client,
            ),
            patch(
                "disclosure_anchor.adapters.runtime.mineru_deployment_gate.writer_code_digest",
                return_value=CODE_DIGEST,
            ),
        )

    def test_matching_current_validation_allows_composition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, client, _ = self._fixture(Path(tmp), now=datetime.now(UTC))
            client_patch, code_patch = self._identity_patches(client)
            with client_patch, code_patch:
                require_mineru_deployment_gate(settings)

    def test_parse_admission_fails_closed_without_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, client, _ = self._fixture(Path(tmp), now=datetime.now(UTC))
            settings = settings.model_copy(
                update={"disclosure_mineru_validation_receipt": None}
            )
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                self.assertRaisesRegex(
                    MinerUDeploymentGateError, "validation receipt"
                ),
            ):
                require_mineru_deployment_gate(settings)

    def test_legacy_smoke_schema_is_rejected_without_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, client, _ = self._fixture(Path(tmp), now=datetime.now(UTC))
            assert settings.disclosure_mineru_smoke_receipt is not None
            payload = json.loads(settings.disclosure_mineru_smoke_receipt.read_bytes())
            payload["schema"] = "mineru_smoke_receipt.v4"
            settings.disclosure_mineru_smoke_receipt.write_text(json.dumps(payload))
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                self.assertRaisesRegex(MinerUDeploymentGateError, "v5 PASS"),
            ):
                require_mineru_deployment_gate(settings)

    def test_smoke_pending_capacity_drift_is_rejected(self) -> None:
        for field in (
            "max_pending_tasks_requested",
            "max_pending_tasks_effective",
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                settings, client, _ = self._fixture(
                    Path(tmp), now=datetime.now(UTC)
                )
                assert settings.disclosure_mineru_smoke_receipt is not None
                payload = json.loads(settings.disclosure_mineru_smoke_receipt.read_bytes())
                payload["orchestrator"]["after"][field] = 2
                settings.disclosure_mineru_smoke_receipt.write_text(
                    json.dumps(payload)
                )
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(
                        MinerUDeploymentGateError, "pending"
                    ),
                ):
                    require_mineru_deployment_gate(settings)

    def test_validation_rejects_page_loss_duplicate_and_epoch_change(self) -> None:
        for tamper in (
            "page_loss",
            "duplicate",
            "epoch",
            "document_hash",
            "epoch_hash",
            "input_profile",
            "restart",
            "oom",
            "collector_binding",
            "node_binding",
            "compose_binding",
            "writer_binding",
            "image_binding",
            "bad_epoch_sha",
            "bad_container_id",
        ):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                settings, client, _ = self._fixture(
                    Path(tmp), now=datetime.now(UTC)
                )
                assert settings.disclosure_mineru_validation_receipt is not None
                value = json.loads(
                    settings.disclosure_mineru_validation_receipt.read_bytes()
                )
                documents = value["documents"]
                if tamper == "page_loss":
                    documents[0]["receipt"]["provider"]["page_count"] -= 1
                    documents[0]["receipt_sha256"] = canonical_payload_sha256(
                        documents[0]["receipt"]
                    )
                elif tamper == "duplicate":
                    documents[1]["receipt"]["input"]["sha256"] = documents[0][
                        "receipt"
                    ]["input"]["sha256"]
                    documents[1]["receipt_sha256"] = canonical_payload_sha256(
                        documents[1]["receipt"]
                    )
                elif tamper == "epoch":
                    after = value["epoch_after"]
                    after["receipt"]["service_epoch"][
                        "container_epoch_sha256"
                    ] = "sha256:" + "7" * 64
                    after["receipt"]["service_epoch_sha256"] = (
                        canonical_payload_sha256(after["receipt"]["service_epoch"])
                    )
                    after["receipt_sha256"] = canonical_payload_sha256(
                        after["receipt"]
                    )
                elif tamper == "document_hash":
                    documents[0]["receipt"]["provider"]["block_count"] += 1
                elif tamper == "epoch_hash":
                    value["epoch_after"]["receipt"]["service_epoch"][
                        "api_container_id"
                    ] = "7" * 64
                    value["epoch_after"]["receipt_sha256"] = (
                        canonical_payload_sha256(value["epoch_after"]["receipt"])
                    )
                elif tamper == "input_profile":
                    documents[0]["receipt"]["input"]["profile"] = (
                        "deployment_frozen_v1"
                    )
                    documents[0]["receipt_sha256"] = canonical_payload_sha256(
                        documents[0]["receipt"]
                    )
                elif tamper in {"restart", "oom"}:
                    field = (
                        "restart_count_total"
                        if tamper == "restart"
                        else "cgroup_oom_kill_total"
                    )
                    value["epoch_after"]["receipt"]["safety"][field] = 1
                    value["epoch_after"]["receipt_sha256"] = (
                        canonical_payload_sha256(value["epoch_after"]["receipt"])
                    )
                else:
                    field = {
                        "collector_binding": "collector_sha256",
                        "node_binding": "windows_node_identity_sha256",
                        "compose_binding": "windows_compose_sha256",
                        "writer_binding": "writer_code_sha256",
                        "image_binding": "api_image_digest",
                        "bad_epoch_sha": "container_epoch_sha256",
                        "bad_container_id": "api_container_id",
                    }[tamper]
                    replacement = (
                        "not-a-sha"
                        if tamper == "bad_epoch_sha"
                        else "7" * 63
                        if tamper == "bad_container_id"
                        else "sha256:" + "7" * 64
                    )
                    # Keep the two epoch wrappers mutually consistent so these
                    # cases prove manifest binding and field-shape validation,
                    # rather than failing only on the before/after comparison.
                    for wrapper_name in ("epoch_before", "epoch_after"):
                        wrapper = value[wrapper_name]
                        epoch = wrapper["receipt"]["service_epoch"]
                        epoch[field] = replacement
                        wrapper["receipt"]["service_epoch_sha256"] = (
                            canonical_payload_sha256(epoch)
                        )
                        wrapper["receipt_sha256"] = canonical_payload_sha256(
                            wrapper["receipt"]
                        )
                settings.disclosure_mineru_validation_receipt.write_text(
                    json.dumps(value)
                )
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaises(MinerUDeploymentGateError),
                ):
                    require_mineru_deployment_gate(settings)

    def test_evidence_growth_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, client, _ = self._fixture(Path(tmp), now=datetime.now(UTC))
            assert settings.disclosure_mineru_smoke_receipt is not None
            smoke_path = settings.disclosure_mineru_smoke_receipt
            real_fstat = os.fstat
            growth_injected = False

            def fstat_with_growth(descriptor: int) -> os.stat_result:
                nonlocal growth_injected
                metadata = real_fstat(descriptor)
                if not growth_injected:
                    growth_injected = True
                    with smoke_path.open("ab") as output:
                        output.write(b" " * (2 * 1024 * 1024 + 1))
                return metadata

            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                    "os.fstat",
                    side_effect=fstat_with_growth,
                ),
                self.assertRaisesRegex(
                    MinerUDeploymentGateError, "size limit|changed while being read"
                ),
            ):
                require_mineru_deployment_gate(settings)

    def test_evidence_same_size_overwrite_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, client, _ = self._fixture(Path(tmp), now=datetime.now(UTC))
            assert settings.disclosure_mineru_smoke_receipt is not None
            smoke_path = settings.disclosure_mineru_smoke_receipt
            real_fstat = os.fstat
            injected = False

            def fstat_with_overwrite(descriptor: int) -> os.stat_result:
                nonlocal injected
                metadata = real_fstat(descriptor)
                if not injected:
                    injected = True
                    payload = smoke_path.read_bytes()
                    smoke_path.write_bytes(b" " + payload[1:])
                return metadata

            client_patch, code_patch = self._identity_patches(client)
            with (
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                    "os.fstat",
                    side_effect=fstat_with_overwrite,
                ),
                client_patch,
                code_patch,
                self.assertRaisesRegex(MinerUDeploymentGateError, "changed"),
            ):
                require_mineru_deployment_gate(settings)

    def test_evidence_files_are_private_and_not_hardlinked(self) -> None:
        for tamper in ("mode", "hardlink"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                settings, client, _ = self._fixture(
                    Path(tmp), now=datetime.now(UTC)
                )
                assert settings.disclosure_mineru_smoke_receipt is not None
                assert settings.disclosure_mineru_validation_receipt is not None
                if tamper == "mode":
                    settings.disclosure_mineru_smoke_receipt.chmod(0o644)
                else:
                    settings.disclosure_mineru_validation_receipt.unlink()
                    os.link(
                        settings.disclosure_mineru_smoke_receipt,
                        settings.disclosure_mineru_validation_receipt,
                    )
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(
                        MinerUDeploymentGateError, "0600|hard-linked"
                    ),
                ):
                    require_mineru_deployment_gate(settings)

    def test_checker_probes_first_admission_then_uses_short_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, client, _ = self._fixture(Path(tmp), now=datetime.now(UTC))
            live = MinerUOrchestratorHealth(**health(completed=20))
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate.fetch_mineru_orchestrator_health",
                    return_value=live,
                ) as api_probe,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate.probe_mineru_served_model"
                ) as model_probe,
            ):
                checker = MinerUDeploymentChecker(settings)
                checker.assert_admission()
                checker.assert_admission()
            self.assertEqual(api_probe.call_count, 1)
            self.assertEqual(model_probe.call_count, 1)

    def test_incident_drain_and_generation_force_fresh_idle_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(UTC)
            settings, client, _ = self._fixture(Path(tmp), now=now)
            responses = (
                MinerUOrchestratorHealth(**health(completed=20)),
                MinerUOrchestratorHealth(**health(completed=20, queued=1)),
                MinerUOrchestratorHealth(**health(completed=21)),
            )
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                    "fetch_mineru_orchestrator_health",
                    side_effect=responses,
                ) as api_probe,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                    "probe_mineru_served_model"
                ) as model_probe,
            ):
                checker = MinerUDeploymentChecker(
                    settings,
                    wall_clock=lambda: now,
                    monotonic_clock=lambda: 0.0,
                )
                checker.assert_admission()
                token = mark_mineru_orchestrator_incident()
                try:
                    with self.assertRaisesRegex(
                        MinerUDeploymentUnavailableError, "drain is still in progress"
                    ):
                        checker.assert_admission()
                finally:
                    finish_mineru_orchestrator_incident(token)
                with self.assertRaisesRegex(
                    MinerUDeploymentUnavailableError, "undrained work"
                ):
                    checker.assert_admission()
                checker.assert_admission()
                checker.assert_admission()
            self.assertEqual(api_probe.call_count, 3)
            self.assertEqual(model_probe.call_count, 2)


if __name__ == "__main__":
    unittest.main()
