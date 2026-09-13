"""Independent literal v11 projection examples; no serving authority or build oracle."""

from copy import deepcopy
import hashlib
from pathlib import Path
from unittest.mock import patch

from disclosure_anchor.application.contracts.mineru_capacity_config import (
    decode_mineru_capacity_config,
)
from scripts import attest_mineru_remote_runtime as attester
from tests._mineru_capacity_config_fixture import canonical_payload, capacity_payload
from tests._mineru_capacity_health_fixture import capacity_health_payload
from tests.unit import test_attest_mineru_remote_runtime as legacy_attester
from tests.unit import test_mineru_identity as legacy_identity


SOURCE_PATHS = {
    "mineru/cli/agent_capacity_config.py": "src/disclosure_anchor/application/contracts/mineru_capacity_config.py",
    "mineru/cli/agent_capacity_file.py": "src/disclosure_anchor/adapters/runtime/mineru_capacity_file.py",
    "mineru/cli/agent_capacity_bootstrap.py": "scripts/windows/mineru_heap_trim_compat/agent_capacity_bootstrap.py",
    "mineru/cli/agent_capacity_observation.py": "scripts/windows/mineru_heap_trim_compat/agent_capacity_observation.py",
}
LABEL = "io.agent-invest.mineru."
POLICY = "single-process-explicit-capacity.v1"
VERSION = "mineru-runtime-bundle.v11"


def sha_bytes(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def digest(value):
    return sha_bytes(canonical_payload(value))


def source_pins():
    root = Path(legacy_attester.__file__).resolve().parents[2]
    return {wire: sha_bytes((root / local).read_bytes()) for wire, local in SOURCE_PATHS.items()}


def idle_health(payload):
    """Transform AE's literal occupied wire to an explicitly empty owner; no DTO projection."""
    health = capacity_health_payload()
    config_sha = digest(payload)
    health.update(processing_tasks=0, max_concurrent_requests=payload["parse_active_limit"],
                  max_pending_tasks_requested=payload["total_nonterminal_limit"],
                  max_pending_tasks_effective=payload["total_nonterminal_limit"],
                  processing_window_size=payload["processing_window_size"])
    health["task_protocol_runtime"].update(
        capacity_config_sha256=config_sha,
        task_result_reservation_bytes=payload["result_reservation_bytes"],
        max_unacked_result_bytes=payload["max_unacked_result_bytes"],
    )
    admission = health["task_admission"]
    for field in ("accepted_processing_tasks", "accepted_finalizing_tasks", "durable_nonterminal_tasks",
                  "scheduled_tasks", "active_processors"):
        admission[field] = 0
    admission.update(nonterminal_limit=payload["total_nonterminal_limit"],
                     admission_open=True, blocked_reason=None)
    observed = health["capacity_observation"]
    observed["capacity_config_sha256"] = config_sha
    observed["resolved_limits"] = {
        name: payload[name] for name in (
            "parse_active_limit", "total_nonterminal_limit", "finalizer_active_limit",
            "result_reservation_bytes", "max_unacked_result_bytes", "final_http_limit_per_loop",
        )
    }
    observed["stage_counters"] = {
        "result_capacity_waiting": 0, "parse_waiting": 0, "parse_active": 0,
        "finalizer_waiting": 0, "finalizer_active": 0,
    }
    observed["http_counters"] = {"active_requests": 0, "pending_requests": 0}
    return health


def observation(payload=None):
    payload = capacity_payload() if payload is None else deepcopy(payload)
    value = legacy_attester._observation()
    value["schema"] = "mineru-windows-runtime-observation.v6"
    value["api_health"] = idle_health(payload)
    value["api"]["command"][-1] = str(payload["final_http_limit_per_loop"])
    env_names = {
        "MINERU_API_MAX_CONCURRENT_REQUESTS": "parse_active_limit",
        "MINERU_API_MAX_PENDING_TASKS": "total_nonterminal_limit",
        "MINERU_API_FINALIZER_SLOTS": "finalizer_active_limit",
        "MINERU_PROCESSING_WINDOW_SIZE": "processing_window_size",
        "MINERU_HYBRID_BATCH_RATIO": "hybrid_batch_ratio_requested",
        "MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES": "result_reservation_bytes",
        "MINERU_TASK_PROTOCOL_V2_MAX_UNACKED_BYTES": "max_unacked_result_bytes",
        "OMP_NUM_THREADS": "omp_num_threads", "MKL_NUM_THREADS": "mkl_num_threads",
        "OPENBLAS_NUM_THREADS": "openblas_num_threads",
        "MINERU_PDF_RENDER_THREADS": "pdf_render_processes_requested",
    }
    value["api"]["environment"].update({env: str(payload[field]) for env, field in env_names.items()})
    compat = value["api_compatibility"]
    del compat["capacity_runtime"]
    compat.update(
        capacity_sources_actual_sha256=source_pins(),
        capacity_config_file={"path": "/usr/local/etc/mineru/capacity.json",
                              "sha256": digest(payload), "byte_count": len(canonical_payload(payload))},
        hybrid_batch_ratio_requested=payload["hybrid_batch_ratio_requested"],
        max_pending_tasks_requested=payload["total_nonterminal_limit"],
        max_pending_tasks_effective=payload["total_nonterminal_limit"],
        task_result_reservation_bytes=payload["result_reservation_bytes"],
        max_unacked_result_bytes=payload["max_unacked_result_bytes"],
    )
    compat["marker"]["capacity_policy"] = POLICY
    compat["image_labels"].update({
        LABEL + "capacity-policy": POLICY,
        LABEL + "capacity-config-sha256": digest(payload),
        LABEL + "capacity-sources-sha256": digest(source_pins()),
    })
    return value


def build(value, payload, **overrides):
    kwargs = dict(
        mineru_bin=Path("/private/unused-mineru"), ssh_host_key_sha256="sha256:" + "6" * 64,
        api_url="http://127.0.0.1:30002", observability_url="http://127.0.0.1:30001/v1",
        inference_upstream_url="http://mineru-openai-server:30000/v1",
        expected_compose_sha256="sha256:" + "3" * 64,
        expected_collector_sha256="sha256:" + "7" * 64,
        expected_compat_patcher_sha256=legacy_attester.PATCHER_DIGEST,
        expected_compat_dockerfile_sha256=legacy_attester.DOCKERFILE_DIGEST,
        expected_task_protocol_v2_sha256=legacy_attester.TASK_PROTOCOL_DIGEST,
        expected_capacity=decode_mineru_capacity_config(canonical_payload(payload)),
        expected_capacity_source_sha256=source_pins(),
    )
    kwargs.update(overrides)
    with (patch.object(attester, "client_bundle_identity", return_value=legacy_attester.CLIENT),
          patch.object(attester, "writer_code_digest", return_value=legacy_attester.CODE_DIGEST)):
        return attester.build_manifest(value, **kwargs)


def expected_orchestrator(value, config):
    """Hand-expanded accepted v11 static domains, using raw literals and stdlib SHA only.

    It does not call build_manifest or any production projection/canonical helper.
    Old topology/client validation remains in the borrowed tests; this oracle owns
    all 28 orchestrator fields and every changed capacity/source/hash domain.
    """
    api, proxy = value["api"], value["proxy"]
    inference, model, compat = value["inference"], value["served_model"], value["api_compatibility"]
    command = ["mineru-api", "--host", "0.0.0.0", "--port", "8000", "--allow-public-http-client",
               "--max-concurrency", str(config["final_http_limit_per_loop"])]
    inference_command = ["mineru-openai-server", "--host", "0.0.0.0", "--port", "30000",
                         "--max-num-seqs", "128", "--mm-processor-cache-gb", "0"]
    capacity_domain = {
        "api_command": command, "api_image_id": api["image_id"],
        "base_image_id": inference["image_id"], "compatibility_labels": compat["image_labels"],
        "inference_command": inference_command, "inference_concurrency": config["final_http_limit_per_loop"],
        "model_repository": model["repository"], "model_revision": model["revision"],
        "processing_window_size": config["processing_window_size"],
        "hybrid_batch_ratio": config["hybrid_batch_ratio_requested"], "pipeline_inference_locks": True,
        "task_slots": config["parse_active_limit"],
        "max_pending_tasks_requested": config["total_nonterminal_limit"],
        "max_pending_tasks_effective": config["total_nonterminal_limit"],
        "vllm_max_num_seqs": 128, "vllm_version": "0.21.0", "task_registry_max_records": 128,
        "task_result_reservation_bytes": config["result_reservation_bytes"],
        "max_unacked_result_bytes": config["max_unacked_result_bytes"],
        "capacity_config_sha256": digest(config), "capacity_source_sha256": source_pins(),
    }
    return {
        "container_image_digest": api["image_id"], "base_container_image_digest": inference["image_id"],
        "content_environment_sha256": digest(api["environment"]),
        "service_config_sha256": digest({
            "compose_sha256": value["compose_sha256"], "compose_config_sha256": value["compose_config_sha256"],
            "image": api["image"], "image_id": api["image_id"], "command": command,
            "proxy_command": proxy["entrypoint"] + proxy["command"],
            "proxy_restart_policy": proxy["restart_policy"], "restart_policy": api["restart_policy"],
        }),
        "mount_policy_sha256": digest(api["mounts"]),
        "network_policy_sha256": digest({
            "api_networks": api["networks"], "api_port": api["port"],
            "proxy_networks": proxy["networks"], "proxy_port": proxy["port"],
        }),
        "heap_return_compatibility_sha256": digest(compat),
        "heap_return_policy": "glibc-malloc-trim-per-window.v1", "mineru_version": "3.4.4",
        "api_protocol_version": 2, "max_concurrent_requests": config["parse_active_limit"],
        "max_pending_tasks_requested": config["total_nonterminal_limit"],
        "max_pending_tasks_effective": config["total_nonterminal_limit"],
        "inference_max_concurrency": config["final_http_limit_per_loop"],
        "hybrid_batch_ratio": config["hybrid_batch_ratio_requested"], "pipeline_inference_locks": True,
        "processing_window_size": config["processing_window_size"], "task_retention_seconds": 600,
        "task_cleanup_interval_seconds": 30, "task_registry_max_records": 128,
        "task_result_reservation_bytes": config["result_reservation_bytes"],
        "max_unacked_result_bytes": config["max_unacked_result_bytes"],
        "output_root_policy": "dedicated-scratch-retention.v1", "command": command,
        "capacity_runtime_compatibility_sha256": digest(capacity_domain),
        "capacity_config": deepcopy(config), "capacity_config_sha256": digest(config),
        "capacity_source_sha256": source_pins(),
    }


def independent_manifest(payload):
    # Reuse the previous literal client/provider/topology, not attester output.
    value = legacy_identity._manifest()
    value["contract_version"] = VERSION
    value["orchestrator"] = expected_orchestrator(observation(payload), payload)
    return value


def envelope(manifest):
    return {"manifest": manifest, "identity_sha256": digest(manifest)}
