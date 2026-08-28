"""Resident-worker MinerU deployment gate regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mineru_canary import (
    MinerUCanaryError,
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
    MINERU_WINDOWS_COLLECTOR_PATH,
    MINERU_WINDOWS_COMPOSE_PATH,
    MINERU_SMOKE_INPUT_NAME,
    MINERU_SMOKE_INPUT_SHA256,
    MinerUClientIdentity,
    canonical_payload_sha256,
)
from disclosure_anchor.adapters.runtime.mineru_orchestrator import (
    MinerUOrchestratorHealth,
    MinerUOrchestratorUnavailableError,
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


class MinerUDeploymentGateTests(unittest.TestCase):
    def _fixture(
        self,
        root: Path,
        *,
        passed_at: datetime,
    ) -> tuple[Settings, Path, Path, MinerUClientIdentity]:
        smoke_started_at = passed_at - timedelta(seconds=30)
        canary_passed_at = passed_at - timedelta(seconds=29)
        smoke_finished_at = passed_at - timedelta(seconds=28)
        service = root / "service"
        shared = root / "shared"
        for path in (service, service / "runtime", shared):
            path.mkdir(parents=True, exist_ok=True)
        mineru = root / "mineru"
        mineru.write_text("executable", encoding="utf-8")
        receipt_path = root / "receipt.json"
        cache_path = root / "cache.json"
        staged_load_path = root / "staged-load.json"
        staged_confirmation_path = root / "staged-load-confirmation.json"
        api_url = "http://127.0.0.1:30002"
        observability_url = "http://127.0.0.1:30001/v1"
        inference_upstream_url = "http://mineru-openai-server:30000/v1"
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
            "inference_max_concurrency": 7,
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
            "api_endpoint_sha256": canonical_prefixed_endpoint_sha256(api_url),
            "observability_endpoint_sha256": canonical_prefixed_endpoint_sha256(
                observability_url
            ),
            "inference_upstream_sha256": canonical_prefixed_endpoint_sha256(
                inference_upstream_url
            ),
            "ssh_host_key_sha256": "sha256:" + "b" * 64,
            "windows_node_identity_sha256": "sha256:" + "c" * 64,
            "windows_compose_path": MINERU_WINDOWS_COMPOSE_PATH,
            "windows_compose_sha256": "sha256:" + "d" * 64,
            "windows_collector_path": MINERU_WINDOWS_COLLECTOR_PATH,
            "windows_collector_sha256": "sha256:" + "e" * 64,
        }
        manifest = {
            "contract_version": "mineru-runtime-bundle.v6",
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
        cache = {
            "schema": "mineru_multimodal_canary.v2",
            "passed_at_utc": canary_passed_at.isoformat(),
            "observability_endpoint_sha256": canonical_endpoint_sha256(
                observability_url
            ),
            "runtime_bundle_identity_sha256": runtime_identity,
            "model_id_sha256": model_id_sha256(MODEL_ID),
            "attempts": 3,
            "request_sha256": canary_request_sha256(MODEL_ID),
            "response_sha256": ["d" * 64, "e" * 64, "f" * 64],
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
        receipt = {
            "schema": "mineru_smoke_receipt.v4",
            "status": "pass",
            "started_at_utc": smoke_started_at.isoformat(),
            "finished_at_utc": smoke_finished_at.isoformat(),
            "elapsed_seconds": 2.0,
            "database_access": "none",
            "queue_access": "none",
            "input": {
                "profile": "deployment_frozen_v1",
                "logical_name": MINERU_SMOKE_INPUT_NAME,
                "sha256": MINERU_SMOKE_INPUT_SHA256,
                "bytes": 329,
            },
            "identity": {
                "local_client_identity_sha256": LOCAL_DIGEST,
                "local_content_package_versions": dict(MINERU_CONTENT_PACKAGE_VERSIONS),
                "local_processing_window_size": MINERU_PROCESSING_WINDOW_SIZE,
                "local_writer_code_sha256": CODE_DIGEST,
                "runtime_manifest_identity_sha256": runtime_identity,
                "orchestrator_runtime_identity_sha256": (
                    canonical_payload_sha256(orchestrator)
                ),
                "provider_runtime_identity_sha256": canonical_payload_sha256(server),
                "served_model_id": MODEL_ID,
                "orchestrator_task_slots": 1,
            },
            "runtime_manifest": manifest,
            "canary": cache,
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
                "before": api_health(completed=10),
                "after": api_health(completed=11),
                "terminal_active_tasks": 0,
                "stop_semantics": "drain-not-cancel.v1",
            },
            "provider": {
                "target_identity": target.to_payload(),
                "provider_bundle_sha256": "sha256:" + "4" * 64,
                "page_count": 1,
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
        staged_input_sha256 = "sha256:" + "5" * 64
        staged_cleanup = {
            "external_api_temp_dirs_created": 0,
            "external_api_temp_dirs_after": 0,
            "api_temp_cleanup_errors": [],
            "external_mineru_processes_after": 0,
            "temporary_tree_removed": True,
            "observation_error": None,
        }
        corpus_documents = [
            {
                "logical_name": f"real-{index:02d}.pdf",
                "sha256": f"sha256:{index:064x}",
                "bytes": 1024 + index,
                "page_count": 600 if index == 16 else (100 if index == 15 else 7),
                "workload_class": (
                    "huge" if index == 16 else ("heavy" if index == 15 else "regular")
                ),
            }
            for index in range(1, 17)
        ]

        def selected_stage_documents(count: int) -> list[dict[str, object]]:
            selected = [
                corpus_documents[0],
                corpus_documents[14],
                corpus_documents[15],
            ]
            selected_hashes = {str(item["sha256"]) for item in selected}
            for item in corpus_documents:
                if len(selected) >= count:
                    break
                if str(item["sha256"]) not in selected_hashes:
                    selected.append(item)
                    selected_hashes.add(str(item["sha256"]))
            return selected

        staged_load = {
            "schema": "mineru_staged_load_receipt.v6",
            "receipt_schema_version": 6,
            "execution_id": "11111111-1111-4111-8111-111111111111",
            "status": "pass",
            "failure": None,
            "started_at_utc": (passed_at - timedelta(seconds=27)).isoformat(),
            "finished_at_utc": (passed_at - timedelta(seconds=17)).isoformat(),
            "elapsed_seconds": 10.0,
            "topology": receipt["topology"],
            "database_access": "none",
            "queue_access": "none",
            "fixed_stage_document_counts": [4, 8, 16],
            "orchestrator_task_concurrency": 1,
            "orchestrator_inference_concurrency": 7,
            "effective_inference_request_upper_bound": 7,
            "safety_limits": {
                "profile": "whole-document-runaway-and-drain.v1",
                "document_runaway_timeout_seconds": 86400,
                "api_drain_timeout_seconds": 86400,
            },
            "input": {
                "profile": "operator_frozen_heterogeneous_v2",
                "logical_name": "real-corpus.json",
                "sha256": staged_input_sha256,
                "bytes": sum(int(item["bytes"]) for item in corpus_documents),
                "minimum_required_pages": 7,
                "documents": corpus_documents,
            },
            "identity": receipt["identity"],
            "host_capacity": host_capacity_evidence(),
            "stages": [
                {
                    "stage_document_count": document_count,
                    "client_outstanding_window": 1,
                    "peak_client_outstanding": 1,
                    "admission_order_profile": "copy-index-fifo.v1",
                    "admission_order_copy_indices": list(
                        range(1, document_count + 1)
                    ),
                    "admission": {
                        "profile": "copy-index-fifo.v1",
                        "expected_copy_indices": list(
                            range(1, document_count + 1)
                        ),
                        "admission_order_copy_indices": list(
                            range(1, document_count + 1)
                        ),
                        "records": [
                            {
                                "copy_index": index,
                                "admission_ordinal": index - 1,
                                "state": "completed",
                            }
                            for index in range(1, document_count + 1)
                        ],
                        "closed": True,
                        "abort_reason": None,
                    },
                    "selection_profile": "per_stage_regular_heavy_huge.v1",
                    "orchestrator_task_concurrency": 1,
                    "orchestrator_inference_concurrency": 7,
                    "effective_inference_request_upper_bound": 7,
                    "status": "pass",
                    "failure": None,
                    "elapsed_seconds": 2.0,
                    "documents": [
                        {
                            "copy_index": copy_index,
                            "status": "pass",
                            "logical_name": document["logical_name"],
                            "input_sha256": document["sha256"],
                            "page_count": document["page_count"],
                            "block_count": 2,
                            "elapsed_seconds": 1.0,
                            "workload_class": document["workload_class"],
                            "provider_bundle_sha256": "sha256:" + "6" * 64,
                        }
                        for copy_index, document in enumerate(
                            selected_stage_documents(document_count),
                            start=1,
                        )
                    ],
                    "metrics": {
                        "sample_count": 1,
                        "sampling_failures": [],
                        "terminal_sample_observed_seconds": 1.0,
                        "observer": {
                            "profile": "metrics-observer.v1",
                            "state": "CLOSED",
                            "observation_complete": True,
                            "hard_failure": None,
                            "transitions": [
                                {
                                    "from": "STARTING",
                                    "to": "HEALTHY",
                                    "reason": "valid_metrics_sample",
                                    "observed_seconds": 0.0,
                                },
                                {
                                    "from": "HEALTHY",
                                    "to": "CLOSED",
                                    "reason": "monitor_stopped",
                                    "observed_seconds": 1.0,
                                },
                            ],
                        },
                        "baseline": {
                            "running": 0,
                            "waiting": 0,
                            "preemptions": 0,
                            "kv_cache": 0,
                        },
                        "range": {
                            "running": {"min": 0, "max": 1},
                            "waiting": {"min": 0, "max": 0},
                            "preemptions": {"min": 0, "max": 0},
                            "kv_cache": {"min": 0, "max": 0},
                        },
                        "percentiles": {
                            "running_p95": 1,
                            "waiting_p95": 0,
                            "kv_cache_p95": 0,
                        },
                    },
                    "orchestrator": staged_orchestrator_evidence(
                        concurrency=document_count,
                        completed_before=20 + sum((4, 8, 16)[:stage_index]),
                    ),
                    "cleanup": staged_cleanup,
                }
                for stage_index, document_count in enumerate((4, 8, 16))
            ],
            "cleanup": staged_cleanup,
        }
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        staged_load_path.write_text(json.dumps(staged_load), encoding="utf-8")
        staged_confirmation = json.loads(json.dumps(staged_load))
        staged_confirmation["execution_id"] = "22222222-2222-4222-8222-222222222222"
        staged_confirmation["started_at_utc"] = (
            passed_at - timedelta(seconds=16)
        ).isoformat()
        staged_confirmation["finished_at_utc"] = (
            passed_at - timedelta(seconds=6)
        ).isoformat()
        staged_confirmation["elapsed_seconds"] = 10.0
        # Retained terminal populations may differ between executions; keep a
        # distinct valid fixture so the default pair exercises that freedom.
        for stage in staged_confirmation["stages"]:
            orchestrator = stage["orchestrator"]
            for sample_name in ("baseline", "terminal"):
                orchestrator[sample_name]["completed_tasks"] += 28
            for sample in orchestrator["samples"]:
                sample["completed_tasks"] += 28
            orchestrator["range"]["completed_tasks"]["min"] += 28
            orchestrator["range"]["completed_tasks"]["max"] += 28
        staged_confirmation_path.write_text(
            json.dumps(staged_confirmation), encoding="utf-8"
        )
        for evidence_path in (
            cache_path,
            receipt_path,
            staged_load_path,
            staged_confirmation_path,
        ):
            evidence_path.chmod(0o600)
        settings = Settings(
            disclosure_data_root=service,
            disclosure_shared_root=shared,
            disclosure_runtime_root=service / "runtime",
            mineru_model_cache=shared / "mineru-cache",
            hf_home=shared / "hf",
            modelscope_cache=shared / "modelscope",
            disclosure_mineru_bin=mineru,
            disclosure_mineru_api_url=api_url,
            disclosure_mineru_observability_url=observability_url,
            disclosure_mineru_inference_upstream_url=inference_upstream_url,
            disclosure_mineru_runtime_bundle_identity_sha256=runtime_identity,
            disclosure_mineru_smoke_receipt=receipt_path,
            disclosure_mineru_canary_cache=cache_path,
            disclosure_mineru_staged_load_receipt=staged_load_path,
            disclosure_mineru_staged_load_confirmation_receipt=(
                staged_confirmation_path
            ),
            disclosure_mineru_staged_corpus_sha256=staged_input_sha256,
            disclosure_mineru_docker_memory_reserve_bytes=1024,
            worker_parse_concurrency=16,
            worker_mineru_client_outstanding_window=1,
            worker_gpu_request_budget=7,
            worker_gpu_max_sequences=128,
        )
        return settings, receipt_path, cache_path, client

    def _identity_patches(self, client: MinerUClientIdentity) -> tuple[object, object]:
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

    def test_matching_fresh_pair_allows_worker_composition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, _, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            client_patch, code_patch = self._identity_patches(client)
            with client_patch, code_patch:
                require_mineru_deployment_gate(settings)

    def test_evidence_files_must_be_private_and_independent(self) -> None:
        for tamper in ("mode", "hardlink"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                settings, receipt_path, _, client = self._fixture(
                    Path(tmp), passed_at=datetime.now(UTC)
                )
                assert settings.disclosure_mineru_staged_load_receipt is not None
                if tamper == "mode":
                    receipt_path.chmod(0o644)
                else:
                    staged_path = settings.disclosure_mineru_staged_load_receipt
                    staged_path.unlink()
                    os.link(receipt_path, staged_path)
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(
                        MinerUDeploymentGateError,
                        "0600|hard-linked",
                    ),
                ):
                    require_mineru_deployment_gate(settings)

    def test_evidence_growth_after_fstat_is_bounded_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, receipt_path, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            real_fstat = os.fstat
            growth_injected = False

            def fstat_with_growth(descriptor: int) -> os.stat_result:
                nonlocal growth_injected
                metadata = real_fstat(descriptor)
                if not growth_injected:
                    growth_injected = True
                    with receipt_path.open("ab") as output:
                        output.write(b" " * (2 * 1024 * 1024 + 1))
                return metadata

            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate.os.fstat",
                    side_effect=fstat_with_growth,
                ),
                self.assertRaisesRegex(
                    MinerUDeploymentGateError,
                    "size limit|changed while being read",
                ),
            ):
                require_mineru_deployment_gate(settings)

    def test_evidence_size_limits_are_type_specific_and_bounded(self) -> None:
        for setting_name, label in (
            ("disclosure_mineru_staged_load_receipt", "staged-load receipt"),
            (
                "disclosure_mineru_staged_load_confirmation_receipt",
                "staged-load confirmation receipt",
            ),
        ):
            with (
                self.subTest(setting_name=setting_name),
                tempfile.TemporaryDirectory() as tmp,
            ):
                settings, _, _, client = self._fixture(
                    Path(tmp), passed_at=datetime.now(UTC)
                )
                staged_path = getattr(settings, setting_name)
                assert staged_path is not None
                with staged_path.open("ab") as output:
                    output.write(b" " * (2 * 1024 * 1024))
                client_patch, code_patch = self._identity_patches(client)
                with client_patch, code_patch:
                    require_mineru_deployment_gate(settings)

                with staged_path.open("ab") as output:
                    output.truncate(64 * 1024 * 1024 + 1)
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(
                        MinerUDeploymentGateError,
                        f"{label} exceeds the size limit",
                    ),
                ):
                    require_mineru_deployment_gate(settings)

        for setting_name, label in (
            ("disclosure_mineru_smoke_receipt", "smoke receipt"),
            ("disclosure_mineru_canary_cache", "canary cache"),
        ):
            with (
                self.subTest(setting_name=setting_name),
                tempfile.TemporaryDirectory() as tmp,
            ):
                settings, _, _, client = self._fixture(
                    Path(tmp), passed_at=datetime.now(UTC)
                )
                evidence_path = getattr(settings, setting_name)
                assert evidence_path is not None
                with evidence_path.open("ab") as output:
                    output.truncate(2 * 1024 * 1024 + 1)
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(
                        MinerUDeploymentGateError,
                        f"{label} exceeds the size limit",
                    ),
                ):
                    require_mineru_deployment_gate(settings)

    def test_staged_corpus_is_pinned_and_shared_by_both_runs(self) -> None:
        for tamper in ("configured_hash", "confirmation_input"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                settings, _, _, client = self._fixture(
                    Path(tmp), passed_at=datetime.now(UTC)
                )
                if tamper == "configured_hash":
                    settings = settings.model_copy(
                        update={
                            "disclosure_mineru_staged_corpus_sha256": (
                                "sha256:" + "0" * 64
                            )
                        }
                    )
                else:
                    confirmation_path = (
                        settings.disclosure_mineru_staged_load_confirmation_receipt
                    )
                    assert confirmation_path is not None
                    confirmation = json.loads(
                        confirmation_path.read_text(encoding="utf-8")
                    )
                    confirmation["input"]["logical_name"] = "other.pdf"
                    confirmation_path.write_text(
                        json.dumps(confirmation), encoding="utf-8"
                    )
                    confirmation_path.chmod(0o600)
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(MinerUDeploymentGateError, "input"),
                ):
                    require_mineru_deployment_gate(settings)

    def test_smoke_health_rejects_boolean_counters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, receipt_path, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["orchestrator"]["before"]["queued_tasks"] = False
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            receipt_path.chmod(0o600)
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                self.assertRaisesRegex(MinerUDeploymentGateError, "health"),
            ):
                require_mineru_deployment_gate(settings)

    def test_smoke_accepts_retained_gauge_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, receipt_path, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["orchestrator"]["before"] = api_health(completed=2, failed=1)
            receipt["orchestrator"]["after"] = api_health(completed=0, failed=0)
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            receipt_path.chmod(0o600)
            client_patch, code_patch = self._identity_patches(client)
            with client_patch, code_patch:
                require_mineru_deployment_gate(settings)

    def test_smoke_rejects_legacy_or_mixed_semantics(self) -> None:
        for tamper, expected in (
            ("legacy", "legacy cumulative-gauge smoke receipt"),
            ("wrong_semantics", "smoke API evidence"),
            ("delta_field", "smoke API evidence"),
        ):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                settings, receipt_path, _, client = self._fixture(
                    Path(tmp), passed_at=datetime.now(UTC)
                )
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if tamper == "legacy":
                    receipt["schema"] = "mineru_smoke_receipt.v3"
                elif tamper == "wrong_semantics":
                    receipt["orchestrator"]["task_registry_semantics"] = "counter.v1"
                else:
                    receipt["orchestrator"]["completed_delta"] = 1
                receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                receipt_path.chmod(0o600)
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(MinerUDeploymentGateError, expected),
                ):
                    require_mineru_deployment_gate(settings)

    def test_staged_rejects_legacy_receipt_without_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, _, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            staged_path = settings.disclosure_mineru_staged_load_receipt
            assert staged_path is not None
            staged = json.loads(staged_path.read_text(encoding="utf-8"))
            staged["schema"] = "mineru_staged_load_receipt.v4"
            staged_path.write_text(json.dumps(staged), encoding="utf-8")
            staged_path.chmod(0o600)
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                self.assertRaisesRegex(
                    MinerUDeploymentGateError,
                    "legacy cumulative-gauge staged-load receipt",
                ),
            ):
                require_mineru_deployment_gate(settings)

    def test_staged_rejects_v5_receipt_for_new_commissioning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, _, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            staged_path = settings.disclosure_mineru_staged_load_receipt
            assert staged_path is not None
            staged = json.loads(staged_path.read_text(encoding="utf-8"))
            staged["schema"] = "mineru_staged_load_receipt.v5"
            staged["receipt_schema_version"] = 5
            staged_path.write_text(json.dumps(staged), encoding="utf-8")
            staged_path.chmod(0o600)
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                self.assertRaisesRegex(MinerUDeploymentGateError, "not PASS"),
            ):
                require_mineru_deployment_gate(settings)

    def test_staged_rejects_missing_or_drifted_safety_limits(self) -> None:
        for tamper in (
            "missing",
            "short_document",
            "short_drain",
            "different",
        ):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                settings, _, _, client = self._fixture(
                    Path(tmp), passed_at=datetime.now(UTC)
                )
                staged_path = settings.disclosure_mineru_staged_load_receipt
                assert staged_path is not None
                staged = json.loads(staged_path.read_text(encoding="utf-8"))
                if tamper == "missing":
                    staged.pop("safety_limits")
                elif tamper == "short_document":
                    staged["safety_limits"][
                        "document_runaway_timeout_seconds"
                    ] = 1800
                elif tamper == "short_drain":
                    staged["safety_limits"]["api_drain_timeout_seconds"] = 1800
                else:
                    staged["safety_limits"]["api_drain_timeout_seconds"] = 172800
                staged_path.write_text(json.dumps(staged), encoding="utf-8")
                staged_path.chmod(0o600)
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(
                        MinerUDeploymentGateError,
                        "safety limits drifted",
                    ),
                ):
                    require_mineru_deployment_gate(settings)

    def test_staged_api_retained_gauges_may_change_between_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, _, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            staged_path = settings.disclosure_mineru_staged_load_receipt
            assert staged_path is not None
            staged = json.loads(staged_path.read_text(encoding="utf-8"))
            evidence = staged["stages"][1]["orchestrator"]
            for sample_name in ("baseline", "terminal"):
                evidence[sample_name]["completed_tasks"] += 1000
            evidence["samples"][0]["completed_tasks"] += 1000
            evidence["range"]["completed_tasks"]["min"] += 1000
            evidence["range"]["completed_tasks"]["max"] += 1000
            staged_path.write_text(json.dumps(staged), encoding="utf-8")
            staged_path.chmod(0o600)
            client_patch, code_patch = self._identity_patches(client)
            with client_patch, code_patch:
                require_mineru_deployment_gate(settings)

    def test_staged_api_retained_gauges_may_change_between_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, _, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            confirmation_path = (
                settings.disclosure_mineru_staged_load_confirmation_receipt
            )
            assert confirmation_path is not None
            confirmation = json.loads(confirmation_path.read_text(encoding="utf-8"))
            for stage in confirmation["stages"]:
                orchestrator = stage["orchestrator"]
                for sample_name in ("baseline", "terminal"):
                    orchestrator[sample_name]["completed_tasks"] -= 28
                for sample in orchestrator["samples"]:
                    sample["completed_tasks"] -= 28
                orchestrator["range"]["completed_tasks"]["min"] -= 28
                orchestrator["range"]["completed_tasks"]["max"] -= 28
            confirmation_path.write_text(json.dumps(confirmation), encoding="utf-8")
            confirmation_path.chmod(0o600)
            client_patch, code_patch = self._identity_patches(client)
            with client_patch, code_patch:
                require_mineru_deployment_gate(settings)

    def test_staged_api_retained_gauges_may_decrease_within_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, _, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            staged_path = settings.disclosure_mineru_staged_load_receipt
            assert staged_path is not None
            staged = json.loads(staged_path.read_text(encoding="utf-8"))
            evidence = staged["stages"][1]["orchestrator"]
            evidence["baseline"] = api_health(completed=2, failed=2)
            evidence["samples"] = [
                {
                    "observed_seconds": 0.25,
                    "queued_tasks": 0,
                    "processing_tasks": 1,
                    "completed_tasks": 1,
                    "failed_tasks": 1,
                },
                {
                    "observed_seconds": 0.5,
                    "queued_tasks": 0,
                    "processing_tasks": 1,
                    "completed_tasks": 0,
                    "failed_tasks": 0,
                },
            ]
            evidence["sample_count"] = 2
            evidence["terminal"] = api_health(completed=1, failed=0)
            evidence["range"]["completed_tasks"] = {"min": 0, "max": 2}
            evidence["range"]["failed_tasks"] = {"min": 0, "max": 2}
            staged_path.write_text(json.dumps(staged), encoding="utf-8")
            staged_path.chmod(0o600)
            client_patch, code_patch = self._identity_patches(client)
            with client_patch, code_patch:
                require_mineru_deployment_gate(settings)

    def test_receipt_elapsed_must_cover_positive_stage_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, _, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            staged_path = settings.disclosure_mineru_staged_load_receipt
            assert staged_path is not None
            staged = json.loads(staged_path.read_text(encoding="utf-8"))
            staged["elapsed_seconds"] = 0.0
            staged_path.write_text(json.dumps(staged), encoding="utf-8")
            staged_path.chmod(0o600)
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                self.assertRaisesRegex(MinerUDeploymentGateError, "elapsed"),
            ):
                require_mineru_deployment_gate(settings)

    def test_worker_fanout_cannot_exceed_staged_envelope(self) -> None:
        unsafe_profiles = (
            (17, 21, 128),
            (16, 128, 128),
            (16, 21, 127),
        )
        for concurrency, budget, max_sequences in unsafe_profiles:
            with (
                self.subTest(
                    concurrency=concurrency,
                    budget=budget,
                    max_sequences=max_sequences,
                ),
                tempfile.TemporaryDirectory() as tmp,
            ):
                settings, _, _, client = self._fixture(
                    Path(tmp), passed_at=datetime.now(UTC)
                )
                settings = settings.model_copy(
                    update={
                        "worker_parse_concurrency": concurrency,
                        "worker_gpu_request_budget": budget,
                        "worker_gpu_max_sequences": max_sequences,
                    }
                )
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(
                        MinerUDeploymentGateError,
                        "staged 16-document/bounded-client/attested-active envelope",
                    ),
                ):
                    require_mineru_deployment_gate(settings)

        with tempfile.TemporaryDirectory() as tmp:
            settings, _, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            settings = settings.model_copy(
                update={
                    "worker_parse_concurrency": 8,
                    "worker_gpu_request_budget": 7,
                }
            )
            client_patch, code_patch = self._identity_patches(client)
            with client_patch, code_patch:
                require_mineru_deployment_gate(settings)

    def test_staged_load_must_be_pass_and_match_current_identity(self) -> None:
        for tamper, expected in (("status", "not PASS"), ("endpoint", "endpoint")):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                settings, _, _, client = self._fixture(
                    Path(tmp), passed_at=datetime.now(UTC)
                )
                assert settings.disclosure_mineru_staged_load_receipt is not None
                staged_path = settings.disclosure_mineru_staged_load_receipt
                staged = json.loads(staged_path.read_text(encoding="utf-8"))
                if tamper == "status":
                    staged["status"] = "fail"
                else:
                    staged["topology"]["api_endpoint_sha256"] = "sha256:" + "0" * 64
                staged_path.write_text(json.dumps(staged), encoding="utf-8")
                client_patch, code_patch = self._identity_patches(client)

                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(MinerUDeploymentGateError, expected),
                ):
                    require_mineru_deployment_gate(settings)

    def test_staged_load_metrics_and_document_sequence_are_recomputed(self) -> None:
        for tamper, expected in (
            ("metrics", "metrics"),
            ("activity", "metrics"),
            ("busy_baseline", "metrics"),
            ("sampling_outages", "metrics"),
            ("sampling_prefix", "metrics"),
            ("sampling_duration", "metrics"),
            ("terminal_before_gap", "metrics"),
            ("admission_profile", "FIFO admission"),
            ("admission_order", "FIFO admission"),
            ("admission_ordinal", "FIFO admission"),
            ("admission_missing_copy", "FIFO admission"),
            ("document_status", "document evidence"),
            ("logical_name", "document evidence"),
            ("input_sha", "document evidence"),
            ("page_count", "document evidence"),
            ("missing_block_count", "document evidence"),
            ("block_count_type", "document evidence"),
            ("elapsed_negative", "document evidence"),
            ("unexpected_document_field", "document evidence"),
            ("bundle_hash", "document evidence"),
            ("copy_index", "document evidence"),
        ):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                settings, _, _, client = self._fixture(
                    Path(tmp), passed_at=datetime.now(UTC)
                )
                assert settings.disclosure_mineru_staged_load_receipt is not None
                staged_path = settings.disclosure_mineru_staged_load_receipt
                staged = json.loads(staged_path.read_text(encoding="utf-8"))
                if tamper == "metrics":
                    staged["stages"][0]["metrics"]["range"]["preemptions"]["max"] = 1
                elif tamper == "activity":
                    staged["stages"][0]["metrics"]["range"]["running"]["max"] = 0
                elif tamper == "busy_baseline":
                    staged["stages"][0]["metrics"]["baseline"]["running"] = 1
                elif tamper == "sampling_outages":
                    staged["stages"][0]["metrics"]["sampling_failures"] = [
                        {
                            "observed_seconds": 0.5,
                            "duration_seconds": 1,
                            "failure": "MetricsTransportUnavailableError:timeout",
                        },
                        {
                            "observed_seconds": 0.75,
                            "duration_seconds": 1,
                            "failure": "MetricsTransportUnavailableError:timeout",
                        },
                    ]
                elif tamper == "sampling_prefix":
                    staged["stages"][0]["metrics"]["sampling_failures"] = [
                        {
                            "observed_seconds": 0.5,
                            "duration_seconds": 1,
                            "failure": "ValueError:bad payload",
                        }
                    ]
                elif tamper == "sampling_duration":
                    staged["stages"][0]["metrics"]["sampling_failures"] = [
                        {
                            "observed_seconds": 0.5,
                            "duration_seconds": 10.001,
                            "failure": "MetricsTransportUnavailableError:timeout",
                        }
                    ]
                elif tamper == "terminal_before_gap":
                    staged["stages"][0]["metrics"]["sampling_failures"] = [
                        {
                            "observed_seconds": 1.0,
                            "duration_seconds": 1,
                            "failure": "MetricsTransportUnavailableError:timeout",
                        }
                    ]
                elif tamper == "admission_profile":
                    staged["stages"][0]["admission_order_profile"] = "other"
                elif tamper == "admission_order":
                    staged["stages"][0]["admission_order_copy_indices"] = [2, 1, 3, 4]
                elif tamper == "admission_ordinal":
                    staged["stages"][0]["admission"]["records"][1][
                        "admission_ordinal"
                    ] = 0
                elif tamper == "admission_missing_copy":
                    staged["stages"][0]["admission"]["records"].pop()
                elif tamper == "document_status":
                    staged["stages"][0]["documents"][0]["status"] = "fail"
                elif tamper == "logical_name":
                    staged["stages"][0]["documents"][0]["logical_name"] = "other.pdf"
                elif tamper == "input_sha":
                    staged["stages"][0]["documents"][0]["input_sha256"] = (
                        "sha256:" + "0" * 64
                    )
                elif tamper == "page_count":
                    staged["stages"][0]["documents"][0]["page_count"] += 1
                elif tamper == "missing_block_count":
                    del staged["stages"][0]["documents"][0]["block_count"]
                elif tamper == "block_count_type":
                    staged["stages"][0]["documents"][0]["block_count"] = "2"
                elif tamper == "elapsed_negative":
                    staged["stages"][0]["documents"][0]["elapsed_seconds"] = -1
                elif tamper == "unexpected_document_field":
                    staged["stages"][0]["documents"][0]["extra"] = "unexpected"
                elif tamper == "bundle_hash":
                    staged["stages"][0]["documents"][0]["provider_bundle_sha256"] = (
                        "not-a-hash"
                    )
                else:
                    staged["stages"][0]["documents"][0]["copy_index"] = 2
                staged_path.write_text(json.dumps(staged), encoding="utf-8")
                client_patch, code_patch = self._identity_patches(client)

                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(MinerUDeploymentGateError, expected),
                ):
                    require_mineru_deployment_gate(settings)

    def test_staged_load_api_accounting_is_recomputed(self) -> None:
        for tamper in (
            "processing_above_limit",
            "active_above_window",
            "wrong_semantics",
            "unexpected_field",
            "negative_gauge",
            "boolean_gauge",
            "no_processing",
            "terminal_busy",
            "range",
            "sample_count",
            "sampling_gap",
            "observer_incomplete",
        ):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                settings, _, _, client = self._fixture(
                    Path(tmp), passed_at=datetime.now(UTC)
                )
                assert settings.disclosure_mineru_staged_load_receipt is not None
                staged_path = settings.disclosure_mineru_staged_load_receipt
                staged = json.loads(staged_path.read_text(encoding="utf-8"))
                evidence = staged["stages"][1]["orchestrator"]
                if tamper == "processing_above_limit":
                    evidence["samples"][0]["processing_tasks"] = 4
                elif tamper == "active_above_window":
                    evidence["samples"][0]["queued_tasks"] = 2
                    evidence["range"]["queued_tasks"]["max"] = 2
                elif tamper == "wrong_semantics":
                    evidence["task_registry_semantics"] = "cumulative-counters.v1"
                elif tamper == "unexpected_field":
                    evidence["completed_delta"] = 7
                elif tamper == "negative_gauge":
                    evidence["samples"][0]["completed_tasks"] = -1
                elif tamper == "boolean_gauge":
                    evidence["samples"][0]["failed_tasks"] = False
                elif tamper == "no_processing":
                    evidence["samples"][0]["processing_tasks"] = 0
                    evidence["range"]["processing_tasks"]["max"] = 0
                elif tamper == "terminal_busy":
                    evidence["terminal"]["processing_tasks"] = 1
                elif tamper == "range":
                    evidence["range"]["processing_tasks"]["max"] = 2
                elif tamper == "sample_count":
                    evidence["sample_count"] = 2
                elif tamper == "sampling_gap":
                    evidence["sampling_failures"] = [
                        {
                            "observed_seconds": 0.3,
                            "duration_seconds": 0.1,
                            "failure": (
                                "MinerUOrchestratorUnavailableError:route loss"
                            ),
                        }
                    ]
                else:
                    evidence["observer"]["observation_complete"] = False
                staged_path.write_text(json.dumps(staged), encoding="utf-8")
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(
                        MinerUDeploymentGateError,
                        "staged-load API",
                    ),
                ):
                    require_mineru_deployment_gate(settings)

    def test_staged_load_host_capacity_evidence_is_recomputed(self) -> None:
        # /proc/meminfo is read independently inside each container. Safe
        # MemAvailable values may differ slightly even though MemTotal and
        # the Docker VM identity are the same; the gate must recompute the
        # minimum rather than require byte-for-byte equality.
        with tempfile.TemporaryDirectory() as tmp:
            settings, _, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            evidence_paths = (
                settings.disclosure_mineru_staged_load_receipt,
                settings.disclosure_mineru_staged_load_confirmation_receipt,
            )
            for evidence_path in evidence_paths:
                assert evidence_path is not None
                staged = json.loads(evidence_path.read_text(encoding="utf-8"))
                for sample in staged["host_capacity"]["samples"]:
                    for index, container in enumerate(sample["containers"]):
                        container["docker_vm_memory_available_bytes"] = (
                            16384 - index * 4096
                        )
                staged["host_capacity"]["summary"][
                    "min_docker_vm_memory_available_bytes"
                ] = 8192
                evidence_path.write_text(json.dumps(staged), encoding="utf-8")
            client_patch, code_patch = self._identity_patches(client)
            with client_patch, code_patch:
                require_mineru_deployment_gate(settings)

        for tamper in ("restart", "oom", "reserve", "gap", "epoch"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                settings, _, _, client = self._fixture(
                    Path(tmp), passed_at=datetime.now(UTC)
                )
                assert settings.disclosure_mineru_staged_load_receipt is not None
                staged_path = settings.disclosure_mineru_staged_load_receipt
                staged = json.loads(staged_path.read_text(encoding="utf-8"))
                host = staged["host_capacity"]
                if tamper == "restart":
                    host["samples"][1]["containers"][0]["restart_count"] = 1
                elif tamper == "oom":
                    host["samples"][1]["containers"][0]["memory_events"]["oom_kill"] = 1
                elif tamper == "reserve":
                    host["samples"][1]["containers"][0][
                        "docker_vm_memory_available_bytes"
                    ] = 512
                elif tamper == "gap":
                    host["samples"][1]["observed_seconds"] = 20.0
                else:
                    host["samples"][1]["containers"][0]["id"] = "a" * 64
                staged_path.write_text(json.dumps(staged), encoding="utf-8")
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(
                        MinerUDeploymentGateError,
                        "host-capacity",
                    ),
                ):
                    require_mineru_deployment_gate(settings)

    def test_smoke_and_staged_load_timestamps_are_strictly_chained(self) -> None:
        for tamper in ("smoke_after_canary", "staged_before_smoke"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                passed_at = datetime.now(UTC)
                settings, receipt_path, _, client = self._fixture(
                    Path(tmp), passed_at=passed_at
                )
                assert settings.disclosure_mineru_staged_load_receipt is not None
                if tamper == "smoke_after_canary":
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    receipt["started_at_utc"] = (
                        passed_at + timedelta(seconds=1)
                    ).isoformat()
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                    expected = "smoke/canary timeline"
                else:
                    staged_path = settings.disclosure_mineru_staged_load_receipt
                    staged = json.loads(staged_path.read_text(encoding="utf-8"))
                    staged["started_at_utc"] = (
                        passed_at - timedelta(seconds=29)
                    ).isoformat()
                    staged_path.write_text(json.dumps(staged), encoding="utf-8")
                    expected = "smoke/staged-load timeline"
                client_patch, code_patch = self._identity_patches(client)
                with (
                    client_patch,
                    code_patch,
                    self.assertRaisesRegex(MinerUDeploymentGateError, expected),
                ):
                    require_mineru_deployment_gate(settings)

    def test_stale_pair_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, _, _, client = self._fixture(
                Path(tmp),
                passed_at=datetime.now(UTC) - timedelta(days=31),
            )
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                self.assertRaisesRegex(MinerUDeploymentGateError, "stale"),
            ):
                require_mineru_deployment_gate(settings)

    def test_custom_input_receipt_is_not_deployment_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, receipt_path, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["input"]["profile"] = "diagnostic_custom"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                self.assertRaisesRegex(MinerUDeploymentGateError, "frozen"),
            ):
                require_mineru_deployment_gate(settings)

    def test_canary_request_identity_is_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, receipt_path, cache_path, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            cache["request_sha256"] = "0" * 64
            receipt["canary"] = cache
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                self.assertRaisesRegex(MinerUDeploymentGateError, "request identity"),
            ):
                require_mineru_deployment_gate(settings)

    def test_served_model_identity_is_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, receipt_path, cache_path, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            cache["model_id_sha256"] = "0" * 64
            receipt["canary"] = cache
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                self.assertRaisesRegex(MinerUDeploymentGateError, "model identity"),
            ):
                require_mineru_deployment_gate(settings)

    def test_provider_runtime_identity_is_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, receipt_path, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["identity"]["provider_runtime_identity_sha256"] = (
                "sha256:" + "0" * 64
            )
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                self.assertRaisesRegex(MinerUDeploymentGateError, "identity drifted"),
            ):
                require_mineru_deployment_gate(settings)

    def test_writer_code_identity_is_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, _, _, client = self._fixture(
                Path(tmp), passed_at=datetime.now(UTC)
            )
            with (
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate.client_bundle_identity",
                    return_value=client,
                ),
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate.writer_code_digest",
                    return_value="sha256:" + "8" * 64,
                ),
                self.assertRaisesRegex(MinerUDeploymentGateError, "writer code"),
            ):
                require_mineru_deployment_gate(settings)

    def test_resident_checker_rate_limits_exact_model_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            passed_at = datetime.now(UTC)
            settings, _, _, client = self._fixture(Path(tmp), passed_at=passed_at)
            clock = {"monotonic": 0.0}
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate.probe_mineru_served_model",
                    return_value=MODEL_ID,
                ) as probe,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate.fetch_mineru_orchestrator_health",
                    return_value=orchestrator_health(completed=100),
                ) as orchestrator_probe,
            ):
                checker = MinerUDeploymentChecker(
                    settings,
                    wall_clock=lambda: passed_at,
                    monotonic_clock=lambda: clock["monotonic"],
                )
                checker.assert_admission()
                probe.assert_called_once_with(
                    "http://127.0.0.1:30001/v1",
                    expected_model_id=MODEL_ID,
                )
                self.assertEqual(orchestrator_probe.call_count, 1)
                clock["monotonic"] = 299
                checker.assert_admission()
                self.assertEqual(probe.call_count, 1)
                clock["monotonic"] = 300
                checker.assert_admission()
                self.assertEqual(probe.call_count, 2)
                self.assertEqual(orchestrator_probe.call_count, 2)

    def test_live_transport_outage_has_typed_transient_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            passed_at = datetime.now(UTC)
            settings, _, _, client = self._fixture(Path(tmp), passed_at=passed_at)
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                    "fetch_mineru_orchestrator_health",
                    side_effect=MinerUOrchestratorUnavailableError(
                        "endpoint unavailable"
                    ),
                ),
            ):
                checker = MinerUDeploymentChecker(
                    settings,
                    wall_clock=lambda: passed_at,
                    monotonic_clock=lambda: 0.0,
                )
                with self.assertRaisesRegex(
                    MinerUDeploymentUnavailableError,
                    "probe unavailable",
                ):
                    checker.assert_admission()

    def test_resident_checker_keeps_live_lease_after_static_startup_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            passed_at = datetime.now(UTC)
            settings, _, _, client = self._fixture(Path(tmp), passed_at=passed_at)
            wall_clock = {"now": passed_at}
            monotonic = {"now": 0.0}
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                    "probe_mineru_served_model",
                    return_value=MODEL_ID,
                ) as probe,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                    "fetch_mineru_orchestrator_health",
                    return_value=orchestrator_health(completed=100),
                ) as orchestrator_probe,
            ):
                checker = MinerUDeploymentChecker(
                    settings,
                    wall_clock=lambda: wall_clock["now"],
                    monotonic_clock=lambda: monotonic["now"],
                )
                checker.assert_admission()
                wall_clock["now"] = passed_at + timedelta(days=31)
                monotonic["now"] = 300.0
                checker.assert_admission()

            self.assertEqual(probe.call_count, 2)
            self.assertEqual(orchestrator_probe.call_count, 2)

    def test_process_local_api_incident_pauses_until_idle_then_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            passed_at = datetime.now(UTC)
            settings, _, _, client = self._fixture(Path(tmp), passed_at=passed_at)
            client_patch, code_patch = self._identity_patches(client)
            with (
                client_patch,
                code_patch,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                    "probe_mineru_served_model",
                    return_value=MODEL_ID,
                ) as model_probe,
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                    "fetch_mineru_orchestrator_health",
                    side_effect=(
                        orchestrator_health(completed=100),
                        orchestrator_health(completed=100, queued=1),
                        orchestrator_health(completed=101),
                    ),
                ) as orchestrator_probe,
            ):
                checker = MinerUDeploymentChecker(
                    settings,
                    wall_clock=lambda: passed_at,
                    monotonic_clock=lambda: 0.0,
                )
                checker.assert_admission()
                incident_token = mark_mineru_orchestrator_incident()
                try:
                    with self.assertRaisesRegex(
                        MinerUDeploymentUnavailableError,
                        "drain is still in progress",
                    ):
                        checker.assert_admission()
                finally:
                    finish_mineru_orchestrator_incident(incident_token)
                with self.assertRaisesRegex(
                    MinerUDeploymentUnavailableError,
                    "undrained work",
                ):
                    checker.assert_admission()
                checker.assert_admission()
                checker.assert_admission()
            self.assertEqual(orchestrator_probe.call_count, 3)
            self.assertEqual(model_probe.call_count, 2)

    def test_incident_recovery_keeps_permanent_model_drift_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            passed_at = datetime.now(UTC)
            settings, _, _, client = self._fixture(Path(tmp), passed_at=passed_at)
            client_patch, code_patch = self._identity_patches(client)
            with client_patch, code_patch:
                checker = MinerUDeploymentChecker(
                    settings,
                    wall_clock=lambda: passed_at,
                    monotonic_clock=lambda: 0.0,
                )
            incident_token = mark_mineru_orchestrator_incident()
            finish_mineru_orchestrator_incident(incident_token)
            with (
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                    "fetch_mineru_orchestrator_health",
                    return_value=orchestrator_health(completed=100),
                ),
                patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                    "probe_mineru_served_model",
                    side_effect=MinerUCanaryError("model identity drifted"),
                ),
            ):
                with self.assertRaises(MinerUDeploymentGateError) as raised:
                    checker.assert_admission()
            self.assertIs(type(raised.exception), MinerUDeploymentGateError)

    def test_new_incident_during_live_proof_keeps_admission_paused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            passed_at = datetime.now(UTC)
            settings, _, _, client = self._fixture(Path(tmp), passed_at=passed_at)
            client_patch, code_patch = self._identity_patches(client)
            with client_patch, code_patch:
                checker = MinerUDeploymentChecker(
                    settings,
                    wall_clock=lambda: passed_at,
                    monotonic_clock=lambda: 0.0,
                )
            incident_token = mark_mineru_orchestrator_incident()
            finish_mineru_orchestrator_incident(incident_token)
            proof_incident_tokens: list[int] = []

            def mark_during_model_probe(*_args: object, **_kwargs: object) -> str:
                proof_incident_tokens.append(mark_mineru_orchestrator_incident())
                return MODEL_ID

            try:
                with (
                    patch(
                        "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                        "fetch_mineru_orchestrator_health",
                        return_value=orchestrator_health(completed=100),
                    ),
                    patch(
                        "disclosure_anchor.adapters.runtime.mineru_deployment_gate."
                        "probe_mineru_served_model",
                        side_effect=mark_during_model_probe,
                    ),
                    self.assertRaisesRegex(
                        MinerUDeploymentUnavailableError,
                        "changed during live admission proof",
                    ),
                ):
                    checker.assert_admission()
            finally:
                for token in proof_incident_tokens:
                    finish_mineru_orchestrator_incident(token)

    def test_parse_disabled_does_not_require_remote_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                disclosure_data_root=root / "service",
                disclosure_shared_root=root / "shared",
                disclosure_runtime_root=root / "service" / "runtime",
                mineru_model_cache=root / "shared" / "mineru",
                hf_home=root / "shared" / "hf",
                modelscope_cache=root / "shared" / "modelscope",
                worker_batch_parse=0,
                worker_parse_concurrency=1,
                worker_gpu_request_budget=7,
                worker_gpu_max_sequences=128,
            )
            require_mineru_deployment_gate(settings)
            with self.assertRaisesRegex(
                MinerUDeploymentGateError,
                "required",
            ):
                require_mineru_deployment_gate(
                    settings,
                    parse_enabled=True,
                )


def canonical_endpoint_sha256(endpoint: str) -> str:
    import hashlib

    return hashlib.sha256(endpoint.rstrip("/").encode()).hexdigest()


def canonical_prefixed_endpoint_sha256(endpoint: str) -> str:
    return "sha256:" + canonical_endpoint_sha256(endpoint)


def api_health(
    *,
    completed: int,
    queued: int = 0,
    processing: int = 0,
    failed: int = 0,
) -> dict[str, object]:
    return {
        "status": "healthy",
        "version": "3.4.4",
        "protocol_version": 2,
        "queued_tasks": queued,
        "processing_tasks": processing,
        "completed_tasks": completed,
        "failed_tasks": failed,
        "max_concurrent_requests": 1,
        "processing_window_size": 16,
        "task_retention_seconds": 600,
        "task_cleanup_interval_seconds": 30,
    }


def orchestrator_health(
    *,
    completed: int,
    queued: int = 0,
    processing: int = 0,
    failed: int = 0,
) -> MinerUOrchestratorHealth:
    return MinerUOrchestratorHealth(
        **api_health(
            completed=completed,
            queued=queued,
            processing=processing,
            failed=failed,
        )
    )


def staged_orchestrator_evidence(
    *,
    concurrency: int,
    completed_before: int,
) -> dict[str, object]:
    queued = 0
    sample = {
        "observed_seconds": 0.25,
        "queued_tasks": queued,
        "processing_tasks": 1,
        "completed_tasks": completed_before,
        "failed_tasks": 0,
    }
    return {
        "task_registry_semantics": "retained-terminal-gauges.v1",
        "baseline": api_health(completed=completed_before),
        "samples": [sample],
        "sample_count": 1,
        "sampling_failures": [],
        "observer": {
            "profile": "orchestrator-observer.v1",
            "state": "CLOSED",
            "observation_complete": True,
            "hard_failure": None,
            "admission_stop_reason": None,
            "transitions": [
                {
                    "from": "STARTING",
                    "to": "HEALTHY",
                    "reason": "valid_orchestrator_sample",
                    "observed_seconds": 0.25,
                },
                {
                    "from": "HEALTHY",
                    "to": "CLOSED",
                    "reason": "monitor_stopped",
                    "observed_seconds": 0.5,
                },
            ],
        },
        "terminal": api_health(completed=completed_before + concurrency),
        "terminal_active_tasks": 0,
        "preflight_drain_seconds": 0.0,
        "terminal_drain_seconds": 0.5,
        "stop_semantics": "drain-not-cancel.v1",
        "range": {
            "queued_tasks": {"min": 0, "max": queued},
            "processing_tasks": {"min": 0, "max": 1},
            "completed_tasks": {
                "min": completed_before,
                "max": completed_before + concurrency,
            },
            "failed_tasks": {"min": 0, "max": 0},
        },
    }


def host_capacity_evidence() -> dict[str, object]:
    started_at = "2026-08-25T00:00:00+00:00"

    def container(name: str, character: str) -> dict[str, object]:
        return {
            "name": name,
            "id": character * 64,
            "started_at_utc": started_at,
            "restart_count": 0,
            "oom_killed": False,
            "exit_code": 0,
            "running": True,
            "status": "running",
            "health": "healthy",
            "pid": 100,
            "memory_current_bytes": 2048,
            "memory_max_bytes": None,
            "memory_events": {"oom": 0, "oom_kill": 0, "high": 0},
            "pid1_rss_bytes": 1024,
            "pid1_rss_hwm_bytes": 2048,
            "docker_vm_memory_total_bytes": 32768,
            "docker_vm_memory_available_bytes": 16384,
        }

    samples = []
    for observed_seconds, observed_at in (
        (0.0, "2026-08-25T00:00:01+00:00"),
        (9.0, "2026-08-25T00:00:10+00:00"),
    ):
        samples.append(
            {
                "schema": "mineru-host-capacity-sample.v1",
                "observed_at_utc": observed_at,
                "collector_path": MINERU_WINDOWS_COLLECTOR_PATH,
                "collector_sha256": "sha256:" + "e" * 64,
                "windows_node_identity_sha256": "sha256:" + "c" * 64,
                "containers": [
                    container("mineru-api", "1"),
                    container("mineru-api-proxy", "2"),
                    container("mineru-openai-server", "3"),
                ],
                "observed_seconds": observed_seconds,
            }
        )
    return {
        "schema": "mineru-host-capacity-evidence.v2",
        "status": "pass",
        "failure": None,
        "sample_interval_seconds": 5.0,
        "max_sample_gap_seconds": 15.0,
        "docker_memory_reserve_bytes": 1024,
        "collector_path": MINERU_WINDOWS_COLLECTOR_PATH,
        "collector_sha256": "sha256:" + "e" * 64,
        "windows_node_identity_sha256": "sha256:" + "c" * 64,
        "samples": samples,
        "violations": [],
        "sampling_failures": [],
        "summary": {
            "sample_count": 2,
            "max_api_pid1_rss_hwm_bytes": 2048,
            "min_docker_vm_memory_available_bytes": 16384,
        },
    }


if __name__ == "__main__":
    unittest.main()
