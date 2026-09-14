"""Explicit A2 startup examples composed from existing literal v11/receipt fixtures.

No PDF, provider, database or live runtime is consulted. Canonical settings and
health use N5/P6/F1/ratio2, matching the approved parameter family, not live IDs.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import json

from disclosure_anchor.application.contracts.mineru_capacity_config import (
    decode_mineru_capacity_config,
)
from disclosure_anchor.adapters.runtime.mineru_canary import (
    canary_request_sha256,
    model_id_sha256,
)
from disclosure_anchor.settings import Settings
from tests._mineru_capacity_config_fixture import canonical_payload, capacity_payload
from tests._mineru_capacity_v11_fixture import digest, idle_health, independent_manifest
from tests.unit.test_mineru_deployment_gate import MinerUDeploymentGateTests


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def explicit_payload(http: int = 14):
    return capacity_payload(
        parse_active_limit=5,
        total_nonterminal_limit=6,
        finalizer_active_limit=1,
        final_http_limit_per_loop=http,
        hybrid_batch_ratio_requested=2,
        processing_window_size=16,
        omp_num_threads=4,
        mkl_num_threads=4,
        openblas_num_threads=1,
        pdf_render_processes_requested=3,
        result_reservation_bytes=256 * 1024**2,
        max_unacked_result_bytes=2 * 1024**3,
    )


def private_json(path: Path, value):
    path.write_bytes(canonical_payload(value))
    path.chmod(0o600)


def gate_fixture(
    root: Path, http: int = 14, *, engine_token_arguments: tuple[str, ...] = ()
):
    """Reuse the old whole receipt topology; replace only explicit identity domains."""
    old, client, validation = MinerUDeploymentGateTests()._fixture(root, now=NOW)
    payload = explicit_payload(http)
    config = decode_mineru_capacity_config(canonical_payload(payload))
    capacity_path = root / "capacity.json"
    private_json(capacity_path, payload)
    manifest = independent_manifest(payload)
    manifest["inference_server"]["command"].extend(engine_token_arguments)
    smoke_path = old.disclosure_mineru_smoke_receipt
    assert smoke_path is not None
    smoke = json.loads(smoke_path.read_bytes())
    manifest["topology"].update(smoke["topology"])
    identity = digest(manifest)

    for receipt in [smoke, *(item["receipt"] for item in validation["documents"])]:
        receipt["runtime_manifest"] = deepcopy(manifest)
        receipt["identity"].update(
            runtime_manifest_identity_sha256=identity,
            orchestrator_runtime_identity_sha256=digest(manifest["orchestrator"]),
            provider_runtime_identity_sha256=digest(manifest["inference_server"]),
            orchestrator_task_slots=5,
            served_model_id=manifest["inference_server"]["served_model_id"],
        )
        receipt["canary"].update(
            runtime_bundle_identity_sha256=identity,
            model_id_sha256=model_id_sha256(
                manifest["inference_server"]["served_model_id"]
            ),
            request_sha256=canary_request_sha256(
                manifest["inference_server"]["served_model_id"]
            ),
        )
        receipt["provider"]["target_identity"]["runtime_bundle_identity_sha256"] = (
            identity
        )
        receipt["diagnostic_disposal"]["runtime_bundle_identity_sha256"] = identity
        for when in ("before", "after"):
            receipt["orchestrator"][when] = idle_health(payload)
    for wrapper in validation["documents"]:
        wrapper["receipt_sha256"] = digest(wrapper["receipt"])
    for when in ("epoch_before", "epoch_after"):
        wrapper = validation[when]
        epoch = wrapper["receipt"]["service_epoch"]
        epoch.update(
            runtime_manifest_identity_sha256=identity,
            collector_sha256=manifest["topology"]["windows_collector_sha256"],
            windows_node_identity_sha256=manifest["topology"][
                "windows_node_identity_sha256"
            ],
            windows_compose_sha256=manifest["topology"]["windows_compose_sha256"],
            writer_code_sha256=manifest["client"]["writer_code_sha256"],
            api_image_digest=manifest["orchestrator"]["container_image_digest"],
        )
        wrapper["receipt"]["service_epoch_sha256"] = digest(epoch)
        wrapper["receipt_sha256"] = digest(wrapper["receipt"])
    private_json(smoke_path, smoke)
    private_json(old.disclosure_mineru_canary_cache, smoke["canary"])
    private_json(old.disclosure_mineru_validation_receipt, validation)
    settings = Settings(
        **dict(
            old.model_dump(),
            worker_parse_execution_mode="staged-v4",
            worker_parse_concurrency=5,
            worker_mineru_client_outstanding_window=5,
            worker_gpu_request_budget=http,
            disclosure_mineru_api_task_slots=5,
            disclosure_mineru_api_inference_concurrency=http,
            disclosure_mineru_capacity_config=capacity_path,
            disclosure_mineru_capacity_config_sha256=config.sha256,
            disclosure_mineru_runtime_bundle_identity_sha256=identity,
        )
    )
    profile = replace(
        MinerUDeploymentGateTests._staged_profile(settings),
        contract_version="mineru.process-profile.v2",
        api_task_slots=5,
        api_max_pending_tasks=6,
        registry_nonterminal_cap=6,
        registry_terminal_cap=122,
        cpu_worker_threads=3,
        omp_thread_count=4,
        raster_stage_slots=5,
        layout_stage_slots=5,
        postprocess_stage_slots=5,
        native_owner_slots=5,
        requested_hybrid_batch_ratio=2,
        effective_hybrid_batch_ratio=2,
        inference_concurrency=http,
        gpu_request_slots=http,
        finalizer_slots=1,
        vllm_max_num_batched_tokens=None,
    )
    return settings, profile, config, client
