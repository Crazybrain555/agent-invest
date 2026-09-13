"""Independent consumer inputs, not deployed health, PDF or qualification evidence."""

from copy import deepcopy
import hashlib

from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruCapacityConfig,
)
from tests._mineru_capacity_config_fixture import canonical_payload, capacity_payload
from tests._mineru_capacity_health_fixture import capacity_health_payload


def digest(value):
    return "sha256:" + hashlib.sha256(canonical_payload(value)).hexdigest()


def config_payload(**changes):
    value = capacity_payload(
        parse_active_limit=2,
        total_nonterminal_limit=4,
        finalizer_active_limit=1,
        omp_num_threads=2,
        mkl_num_threads=2,
        hybrid_batch_ratio_requested=1,
    )
    value.update(changes)
    return value


def configuration(**changes):
    return MineruCapacityConfig(**config_payload(**changes))


def overlap_health(*, idle=False, full=False):
    """Two parse owners plus one finalizer; optional fourth queued responsibility."""
    payload = config_payload()
    value = capacity_health_payload()
    value.update(max_pending_tasks_requested=4, max_pending_tasks_effective=4)
    value["task_protocol_runtime"]["capacity_config_sha256"] = digest(payload)
    observation = value["capacity_observation"]
    observation["capacity_config_sha256"] = digest(payload)
    observation["resolved_limits"]["total_nonterminal_limit"] = 4
    observation["stage_counters"].update(parse_active=2, finalizer_waiting=0)
    admission = value["task_admission"]
    admission.update(
        nonterminal_limit=4,
        accepted_processing_tasks=2,
        accepted_finalizing_tasks=1,
        admission_open=True,
        blocked_reason=None,
    )
    if full:
        value["queued_tasks"] = 1
        admission.update(
            accepted_pending_tasks=1,
            durable_nonterminal_tasks=4,
            scheduled_tasks=4,
            queue_depth=1,
            admission_open=False,
            blocked_reason="capacity_full",
        )
    if idle:
        value.update(queued_tasks=0, processing_tasks=0)
        for field in (
            "accepted_pending_tasks",
            "accepted_processing_tasks",
            "accepted_finalizing_tasks",
            "durable_nonterminal_tasks",
            "scheduled_tasks",
            "queue_depth",
            "active_processors",
        ):
            admission[field] = 0
        admission.update(admission_open=True, blocked_reason=None)
        for field in observation["stage_counters"]:
            observation["stage_counters"][field] = 0
        observation["http_counters"] = {"active_requests": 0, "pending_requests": 0}
    return value


def legacy_health():
    value = overlap_health(idle=True)
    del value["capacity_observation"], value["task_admission"]
    value.update(
        max_concurrent_requests=1,
        max_pending_tasks_requested=1,
        max_pending_tasks_effective=1,
        processing_tasks=1,
    )
    value["task_protocol_runtime"] = {
        "schema": "mineru-task-runtime.v1",
        "enabled": True,
        "task_registry_max_records": 128,
        "task_result_reservation_bytes": 268435456,
        "max_unacked_result_bytes": 2147483648,
    }
    return deepcopy(value)


CLIENT_PACKAGES = {
    "mineru_version": "3.4.4",
    "pdftext_version": "0.6.3",
    "pypdfium2_version": "4.30.0",
    "mineru_vl_utils_version": "1.0.5",
}
CLIENT_SHA = "sha256:" + "a" * 64
WRITER_SHA = "sha256:" + "b" * 64
API_URL = "http://127.0.0.1:30002"
OBSERVABILITY_URL = "http://127.0.0.1:30001/v1"
INFERENCE_URL = "http://mineru-openai-server:30000/v1"


def runtime_manifest():
    """Handwritten schema-only v11; hashes are declared literals, not measured images."""
    payload = config_payload()
    return {
        "contract_version": "mineru-runtime-bundle.v11",
        "client": {
            "package_set_sha256": CLIENT_SHA,
            "writer_code_sha256": WRITER_SHA,
            **CLIENT_PACKAGES,
        },
        "orchestrator": {
            "container_image_digest": "sha256:" + "1" * 64,
            "base_container_image_digest": "sha256:" + "2" * 64,
            "content_environment_sha256": "sha256:" + "3" * 64,
            "service_config_sha256": "sha256:" + "4" * 64,
            "mount_policy_sha256": "sha256:" + "5" * 64,
            "network_policy_sha256": "sha256:" + "6" * 64,
            "heap_return_compatibility_sha256": "sha256:" + "7" * 64,
            "capacity_runtime_compatibility_sha256": "sha256:" + "8" * 64,
            "heap_return_policy": "glibc-malloc-trim-per-window.v1",
            "mineru_version": "3.4.4",
            "api_protocol_version": 2,
            "max_concurrent_requests": 2,
            "max_pending_tasks_requested": 4,
            "max_pending_tasks_effective": 4,
            "inference_max_concurrency": 7,
            "hybrid_batch_ratio": 1,
            "pipeline_inference_locks": True,
            "processing_window_size": 16,
            "task_retention_seconds": 600,
            "task_cleanup_interval_seconds": 30,
            "task_registry_max_records": 128,
            "task_result_reservation_bytes": 31,
            "max_unacked_result_bytes": 73,
            "output_root_policy": "dedicated-scratch-retention.v1",
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
            "capacity_config": payload,
            "capacity_config_sha256": digest(payload),
            "capacity_source_sha256": {
                "mineru/cli/agent_capacity_config.py": "sha256:" + "c" * 64,
                "mineru/cli/agent_capacity_file.py": "sha256:" + "d" * 64,
                "mineru/cli/agent_capacity_bootstrap.py": "sha256:" + "e" * 64,
                "mineru/cli/agent_capacity_observation.py": "sha256:" + "f" * 64,
            },
        },
        "inference_server": {
            "container_image_digest": "sha256:" + "2" * 64,
            "content_environment_sha256": "sha256:" + "9" * 64,
            "server_config_sha256": "sha256:" + "a" * 64,
            "mineru_version": "3.4.4",
            "max_model_len": 8192,
            "model_repository": "example/model",
            "served_model_id": "example-model",
            "model_snapshot_revision": "1" * 40,
            "vllm_version": "0.21.0",
            "command": [
                "mineru-openai-server",
                "--max-num-seqs",
                "128",
                "--mm-processor-cache-gb",
                "0",
            ],
        },
        "topology": {
            "api_transport": "pinned-ssh-local-forward.v1",
            "api_exposure": "windows-loopback-only.v1",
            "orchestrator_egress_policy": "dedicated-internal-vllm-only.v1",
            "api_endpoint_sha256": "sha256:"
            + hashlib.sha256(API_URL.encode()).hexdigest(),
            "observability_endpoint_sha256": "sha256:"
            + hashlib.sha256(OBSERVABILITY_URL.encode()).hexdigest(),
            "inference_upstream_sha256": "sha256:"
            + hashlib.sha256(INFERENCE_URL.encode()).hexdigest(),
            "ssh_host_key_sha256": "sha256:" + "3" * 64,
            "windows_node_identity_sha256": "sha256:" + "4" * 64,
            "windows_compose_path": r"C:\ProgramData\compose.tailnet.yaml",
            "windows_compose_sha256": "sha256:" + "5" * 64,
            "windows_collector_path": r"C:\ProgramData\agent-invest\mineru-runtime-v6\collect_mineru_runtime.ps1",
            "windows_collector_sha256": "sha256:" + "6" * 64,
        },
    }


def runtime_wrapper():
    manifest = runtime_manifest()
    return {"manifest": manifest, "identity_sha256": digest(manifest)}
