"""Build a v7 manifest from a live, pinned-SSH Windows observation."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any

from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_API_EGRESS_POLICY,
    MINERU_API_EXPOSURE_POLICY,
    MINERU_API_INFERENCE_MAX_CONCURRENCY,
    MINERU_API_OUTPUT_ROOT_POLICY,
    MINERU_API_PROTOCOL_VERSION,
    MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS,
    MINERU_API_TASK_RETENTION_SECONDS,
    MINERU_API_TRANSPORT_PROFILE,
    MINERU_HEAP_RETURN_POLICY,
    MINERU_HYBRID_BATCH_RATIO,
    MINERU_PIPELINE_INFERENCE_LOCKS_ENABLED,
    MINERU_PROCESSING_WINDOW_SIZE,
    MINERU_WINDOWS_COLLECTOR_PATH,
    MINERU_WINDOWS_COMPOSE_PATH,
    RUNTIME_MANIFEST_CONTRACT,
    canonical_payload_sha256,
    client_bundle_identity,
    verify_runtime_manifest_payload,
    writer_code_digest,
)


EXPECTED_REPO_DIGEST = (
    "mineru@sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458"
)
EXPECTED_IMAGE_ID = (
    "sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458"
)
EXPECTED_API_COMPAT_IMAGE = "agent-invest/mineru-api:3.4.4-serial-v1"
EXPECTED_COMPAT_MARKER_SCHEMA = "mineru-runtime-compatibility.v5"
EXPECTED_CAPACITY_POLICY = "single-owner-serial-mineru.v1"
EXPECTED_COMPAT_PREIMAGES = {
    "mineru/cli/fast_api.py": (
        "sha256:f7f233d86ae0f5aab6ffe5d8eccef4344c968aeaf879563dae99d4875057ee39"
    ),
    "mineru/backend/vlm/vlm_analyze.py": (
        "sha256:0fadf7a94ae702861b4a1fa7f42358c6687cfc63fbe322c004fb1d3248658390"
    ),
    "mineru/backend/hybrid/hybrid_analyze.py": (
        "sha256:404ce6552e9d7374b96de798d2d0f7d72927eef9485668e79c82c5002b36adb0"
    ),
    "mineru/utils/model_utils.py": (
        "sha256:7662656c5c406ab704065b8a3a6e662b662b0bb877b76b08c7d8a8a7eaf9c109"
    ),
    "mineru_vl_utils/post_process/cross_page_table.py": (
        "sha256:97581c69b92ae80df2a11f3dc986f329b26edca5af57e6052929aeadefab898f"
    ),
    "mineru_vl_utils/vlm_client/http_client.py": (
        "sha256:afe42d8a5e310d27cb0173abf4d59ed6197bc0b60a0258f321a6cdedd07c6ba7"
    ),
}
COMPAT_LABEL_KEYS = {
    "io.agent-invest.mineru.base-image-digest",
    "io.agent-invest.mineru.capacity-policy",
    "io.agent-invest.mineru.compatibility-policy",
    "io.agent-invest.mineru.compatibility-patcher-sha256",
    "io.agent-invest.mineru.compatibility-dockerfile-sha256",
}
API_ENV_KEYS = {
    "MINERU_MODEL_SOURCE",
    "MINERU_MALLOC_TRIM",
    "MINERU_PHASE_TRACE",
    "MINERU_API_MAX_CONCURRENT_REQUESTS",
    "MINERU_API_MAX_PENDING_TASKS",
    "MINERU_PROCESSING_WINDOW_SIZE",
    "MINERU_API_OUTPUT_ROOT",
    "MINERU_API_TASK_RETENTION_SECONDS",
    "MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS",
    "MINERU_API_DISABLE_ACCESS_LOG",
    "MINERU_API_ENABLE_FASTAPI_DOCS",
    "MINERU_ENABLE_PIPELINE_INFERENCE_LOCKS",
    "MINERU_HYBRID_BATCH_RATIO",
}
INFERENCE_ENV_KEYS = {"MINERU_MODEL_SOURCE"}
EXPECTED_API_COMMAND = [
    "mineru-api",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
    "--allow-public-http-client",
    "--max-concurrency",
    "7",
]
EXPECTED_INFERENCE_COMMAND = [
    "mineru-openai-server",
    "--host",
    "0.0.0.0",
    "--port",
    "30000",
    "--max-num-seqs",
    "128",
    "--mm-processor-cache-gb",
    "0",
]
EXPECTED_COMPOSE_PATH = MINERU_WINDOWS_COMPOSE_PATH
EXPECTED_COLLECTOR_PATH = MINERU_WINDOWS_COLLECTOR_PATH
EXPECTED_OUTPUT_ROOT = r"C:\ProgramData\agent-invest\mineru-api-output"
EXPECTED_MODEL_REPOSITORY = "opendatalab/MinerU2.5-Pro-2605-1.2B"
EXPECTED_MODEL_REVISION = "bff20d4ae2bf202df9f45284b4d43681555a97ed"
EXPECTED_PROXY_CODE_SHA256 = (
    "sha256:991ff233fb77188f402dba81a8ebb6519630122087a8b5744396e7ebd8c63922"
)
SSH_HOST_RE = re.compile(r"^(?!-)[A-Za-z0-9.-]+$")
SSH_USER_RE = re.compile(r"^(?!-)[A-Za-z0-9_.-]+$")
SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def _windows_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("remote Windows path is invalid")
    return value.replace("/", "\\").rstrip("\\").casefold()


def _canonical_remote_collector_path(value: object) -> str:
    """Accept only the current versioned, non-ambiguous collector target."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("remote collector path is invalid")
    path = value.replace("/", "\\")
    if path.startswith("\\\\") or re.fullmatch(r"[A-Za-z]:\\.*", path) is None:
        raise ValueError("remote collector path must be a local absolute drive path")
    if ":" in path[2:]:
        raise ValueError("remote collector path must not contain an ADS")
    parts = path[3:].split("\\")
    if any(
        not part
        or part in {".", ".."}
        or part.endswith((" ", "."))
        for part in parts
    ):
        raise ValueError("remote collector path contains an ambiguous segment")
    if path.casefold() != EXPECTED_COLLECTOR_PATH.casefold():
        raise ValueError("remote collector path is not the current versioned target")
    return EXPECTED_COLLECTOR_PATH


def _private_regular_file(path: Path, *, label: str) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise ValueError(f"{label} must be an owner-only 0600 regular file")


def _known_host_key_sha256(path: Path, *, expected_host: str) -> str:
    _private_regular_file(path, label="known_hosts")
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("known_hosts must contain exactly one pinned key")
    fields = lines[0].split()
    if len(fields) != 3 or fields[0] != expected_host or fields[1] != "ssh-ed25519":
        raise ValueError("known_hosts must pin the exact host with one Ed25519 key")
    try:
        key_blob = base64.b64decode(fields[2], validate=True)
    except ValueError as exc:
        raise ValueError("known_hosts public key is not canonical base64") from exc
    return "sha256:" + hashlib.sha256(key_blob).hexdigest()


def _environment(values: object, *, allowlist: set[str]) -> dict[str, str]:
    if (
        not isinstance(values, dict)
        or set(values) != allowlist
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in values.items()
        )
    ):
        raise ValueError("remote environment observation is invalid")
    return {str(key): str(value) for key, value in values.items()}


def _command(component: dict[str, Any]) -> list[str]:
    entrypoint = component.get("entrypoint")
    command = component.get("command")
    if (
        not isinstance(entrypoint, list)
        or not isinstance(command, list)
        or not all(isinstance(item, str) and item for item in entrypoint + command)
    ):
        raise ValueError("remote container command observation is invalid")
    return [*entrypoint, *command]


def _endpoint_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.rstrip("/").encode()).hexdigest()


def _expected_serial_runtime(environment: dict[str, str]) -> dict[str, Any]:
    try:
        configured_window_size = int(environment["MINERU_PROCESSING_WINDOW_SIZE"])
    except (KeyError, ValueError) as exc:
        raise ValueError("remote API processing window is invalid") from exc
    if configured_window_size <= 0:
        raise ValueError("remote API processing window is invalid")
    payload = {
        "inner_inference_concurrency": 7,
        "owner_task_slots": 1,
        "pipeline_depth": 0,
        "pipeline_mode": "serial",
        "profile_id": f"serial-w{configured_window_size}",
        "schema": "mineru-serial-execution-profile.v1",
        "vllm_max_num_seqs": 128,
        "window_size": configured_window_size,
    }
    profile_sha256 = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "configured_window_size": configured_window_size,
        "mode": "serial",
        "owner_task_slots": 1,
        "profile_sha256": profile_sha256,
        "schema": "mineru-serial-runtime.v1",
    }


def _verify_api_compatibility(
    value: object,
    *,
    expected_patcher_sha256: str,
    expected_dockerfile_sha256: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "marker",
        "actual_source_sha256",
        "capacity_runtime",
        "heap_trim_enabled",
        "phase_trace_enabled",
        "hybrid_batch_ratio_requested",
        "max_pending_tasks_requested",
        "max_pending_tasks_effective",
        "pipeline_inference_locks_enabled",
        "image_labels",
    }:
        raise ValueError("remote API compatibility evidence fields drifted")
    marker = value.get("marker")
    actual = value.get("actual_source_sha256")
    labels = value.get("image_labels")
    if (
        not isinstance(marker, dict)
        or set(marker) != {
            "schema",
            "policy",
            "capacity_policy",
            "mineru_version",
            "mineru_vl_utils_version",
            "base_image_digest",
            "patcher_sha256",
            "preimage_sha256",
            "patched_source_sha256",
        }
        or not isinstance(actual, dict)
        or not isinstance(labels, dict)
        or set(labels) != COMPAT_LABEL_KEYS
    ):
        raise ValueError("remote API compatibility marker or labels drifted")
    patched = marker.get("patched_source_sha256")
    if (
        marker.get("schema") != EXPECTED_COMPAT_MARKER_SCHEMA
        or marker.get("policy") != MINERU_HEAP_RETURN_POLICY
        or marker.get("capacity_policy") != EXPECTED_CAPACITY_POLICY
        or marker.get("mineru_version") != "3.4.4"
        or marker.get("mineru_vl_utils_version") != "1.0.5"
        or marker.get("base_image_digest") != EXPECTED_IMAGE_ID
        or marker.get("patcher_sha256") != expected_patcher_sha256
        or marker.get("preimage_sha256") != EXPECTED_COMPAT_PREIMAGES
        or not isinstance(patched, dict)
        or set(patched) != set(EXPECTED_COMPAT_PREIMAGES)
        or actual != patched
        or any(SHA256_RE.fullmatch(str(item)) is None for item in patched.values())
        or not isinstance(value.get("capacity_runtime"), dict)
        or value.get("heap_trim_enabled") is not True
        or not isinstance(value.get("phase_trace_enabled"), bool)
        or value.get("hybrid_batch_ratio_requested") not in {1, 2, 4, 8}
        or value.get("pipeline_inference_locks_enabled") is not True
    ):
        raise ValueError("remote API heap-return marker or source bytes drifted")
    if labels != {
        "io.agent-invest.mineru.base-image-digest": EXPECTED_IMAGE_ID,
        "io.agent-invest.mineru.capacity-policy": EXPECTED_CAPACITY_POLICY,
        "io.agent-invest.mineru.compatibility-policy": MINERU_HEAP_RETURN_POLICY,
        "io.agent-invest.mineru.compatibility-patcher-sha256": (
            expected_patcher_sha256
        ),
        "io.agent-invest.mineru.compatibility-dockerfile-sha256": (
            expected_dockerfile_sha256
        ),
    }:
        raise ValueError("remote API compatibility image labels drifted")
    return dict(value)


def build_manifest(
    observation: dict[str, Any],
    *,
    mineru_bin: Path,
    ssh_host_key_sha256: str,
    api_url: str,
    observability_url: str,
    inference_upstream_url: str,
    expected_compose_sha256: str,
    expected_collector_sha256: str,
    expected_compat_patcher_sha256: str,
    expected_compat_dockerfile_sha256: str,
    expected_collector_path: str = EXPECTED_COLLECTOR_PATH,
) -> dict[str, Any]:
    if observation.get("schema") != "mineru-windows-runtime-observation.v3":
        raise ValueError("remote runtime observation contract drifted")
    api = observation.get("api")
    proxy = observation.get("proxy")
    inference = observation.get("inference")
    health = observation.get("api_health")
    served_model = observation.get("served_model")
    if not all(
        isinstance(item, dict) for item in (api, proxy, inference, health, served_model)
    ):
        raise ValueError("remote runtime observation is incomplete")
    assert isinstance(api, dict)
    assert isinstance(proxy, dict)
    assert isinstance(inference, dict)
    assert isinstance(health, dict)
    assert isinstance(served_model, dict)
    if (
        observation.get("compose_sha256") != expected_compose_sha256
        or observation.get("collector_sha256") != expected_collector_sha256
        or _windows_path(observation.get("compose_path"))
        != _windows_path(EXPECTED_COMPOSE_PATH)
        or _windows_path(observation.get("collector_path"))
        != _windows_path(expected_collector_path)
    ):
        raise ValueError("remote compose or collector path/bytes drifted")
    if (
        api.get("image") != EXPECTED_API_COMPAT_IMAGE
        or SHA256_RE.fullmatch(str(api.get("image_id"))) is None
        or proxy.get("image") != EXPECTED_REPO_DIGEST
        or inference.get("image") != EXPECTED_REPO_DIGEST
        or proxy.get("image_id") != EXPECTED_IMAGE_ID
        or inference.get("image_id") != EXPECTED_IMAGE_ID
    ):
        raise ValueError("remote MinerU image digest drifted")
    compatibility = _verify_api_compatibility(
        observation.get("api_compatibility"),
        expected_patcher_sha256=expected_compat_patcher_sha256,
        expected_dockerfile_sha256=expected_compat_dockerfile_sha256,
    )
    task_slots = health.get("max_concurrent_requests")
    pending_requested = health.get("max_pending_tasks_requested")
    pending_effective = health.get("max_pending_tasks_effective")
    if (
        health.get("status") != "healthy"
        or health.get("version") != "3.4.4"
        or health.get("protocol_version") != MINERU_API_PROTOCOL_VERSION
        or isinstance(task_slots, bool)
        or not isinstance(task_slots, int)
        or task_slots != 1
        or isinstance(pending_requested, bool)
        or not isinstance(pending_requested, int)
        or isinstance(pending_effective, bool)
        or not isinstance(pending_effective, int)
        or pending_requested != pending_effective
        or pending_requested != 1
        or pending_effective != 1
        or health.get("processing_window_size") != MINERU_PROCESSING_WINDOW_SIZE
        or health.get("task_retention_seconds") != MINERU_API_TASK_RETENTION_SECONDS
        or health.get("task_cleanup_interval_seconds")
        != MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS
        or health.get("queued_tasks") != 0
        or health.get("processing_tasks") != 0
    ):
        raise ValueError("remote MinerU API health identity drifted or is busy")
    if api.get("networks") != ["mineru-tailnet_inference"]:
        raise ValueError("remote API is not isolated to the internal network")
    if proxy.get("networks") != [
        "mineru-tailnet_inference",
        "mineru-tailnet_runtime",
    ]:
        raise ValueError("remote API proxy network membership drifted")
    if inference.get("networks") != [
        "mineru-tailnet_inference",
        "mineru-tailnet_runtime",
    ]:
        raise ValueError("remote inference network membership drifted")
    networks = observation.get("networks")
    if not isinstance(networks, dict) or networks != {
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
    }:
        raise ValueError("remote Docker network definitions drifted")
    if (
        api.get("port") is not None
        or proxy.get("port") != {"HostIp": "127.0.0.1", "HostPort": "30003"}
        or inference.get("port") != {"HostIp": "127.0.0.1", "HostPort": "30001"}
    ):
        raise ValueError("remote MinerU ports are not exact loopback bindings")
    expected_restart = {
        "Name": "always",
        "MaximumRetryCount": 0,
    }
    if any(
        component.get("restart_policy") != expected_restart
        for component in (api, proxy, inference)
    ):
        raise ValueError("remote MinerU restart policy drifted")
    if any(
        component.get("health_state") != "healthy"
        for component in (api, proxy, inference)
    ):
        raise ValueError("remote MinerU container health is not healthy")
    if api.get("external_tcp_egress_blocked") is not True:
        raise ValueError("remote MinerU API external egress was not disproved")
    api_environment = _environment(api.get("environment"), allowlist=API_ENV_KEYS)
    mounts = api.get("mounts")
    if not isinstance(mounts, list):
        raise ValueError("remote MinerU mount policy drifted")
    mount_by_destination = {
        item.get("Destination"): item
        for item in mounts
        if isinstance(item, dict) and isinstance(item.get("Destination"), str)
    }
    expected_destinations = {"/var/lib/mineru-api-output"}
    if set(mount_by_destination) != expected_destinations or len(mounts) != len(
        expected_destinations
    ):
        raise ValueError("remote MinerU mount policy drifted")
    output_mount = mount_by_destination["/var/lib/mineru-api-output"]
    if (
        output_mount.get("Type") != "bind"
        or _windows_path(output_mount.get("Source"))
        != _windows_path(EXPECTED_OUTPUT_ROOT)
        or output_mount.get("RW") is not True
    ):
        raise ValueError("remote MinerU mount policy drifted")
    if proxy.get("mounts") != [] or inference.get("mounts") != []:
        raise ValueError("remote MinerU mount policy drifted")
    output_root = observation.get("output_root")
    if (
        not isinstance(output_root, dict)
        or _windows_path(output_root.get("path")) != _windows_path(EXPECTED_OUTPUT_ROOT)
        or output_root.get("file_count") != 0
        or output_root.get("total_bytes") != 0
    ):
        raise ValueError("remote MinerU output root drifted")
    expected_model_id = (
        "/root/.cache/huggingface/hub/"
        "models--opendatalab--MinerU2.5-Pro-2605-1.2B/snapshots/"
        + EXPECTED_MODEL_REVISION
    )
    if (
        served_model.get("id") != expected_model_id
        or served_model.get("repository") != EXPECTED_MODEL_REPOSITORY
        or served_model.get("revision") != EXPECTED_MODEL_REVISION
        or served_model.get("max_model_len") != 8192
        or served_model.get("vllm_version") != "0.21.0"
    ):
        raise ValueError("remote served model or vLLM identity drifted")

    inference_environment = _environment(
        inference.get("environment"), allowlist=INFERENCE_ENV_KEYS
    )
    if _environment(proxy.get("environment"), allowlist=set()) != {}:
        raise ValueError("remote API proxy environment drifted")
    if api_environment.get("MINERU_API_MAX_CONCURRENT_REQUESTS") != "1":
        raise ValueError("remote API environment and health task slots drifted")
    if api_environment.get("MINERU_API_MAX_PENDING_TASKS") != "1":
        raise ValueError("remote API environment and health pending depth drifted")
    if (
        compatibility.get("max_pending_tasks_requested") != pending_requested
        or compatibility.get("max_pending_tasks_effective") != pending_effective
    ):
        raise ValueError("remote API pending depth compatibility drifted")
    if api_environment.get("MINERU_MALLOC_TRIM") != "1":
        raise ValueError("remote API heap-return switch is not enabled")
    if api_environment.get("MINERU_ENABLE_PIPELINE_INFERENCE_LOCKS") != "1":
        raise ValueError("remote API pipeline inference locks are not enabled")
    ratio_raw = api_environment.get("MINERU_HYBRID_BATCH_RATIO")
    if ratio_raw not in {"1", "2", "4", "8"}:
        raise ValueError("remote API hybrid batch ratio is not closed")
    if compatibility.get("hybrid_batch_ratio_requested") != int(ratio_raw):
        raise ValueError("remote API hybrid batch ratio observation drifted")
    expected_serial_runtime = _expected_serial_runtime(api_environment)
    if compatibility.get("capacity_runtime") != expected_serial_runtime:
        raise ValueError("remote API serial observation and environment drifted")
    phase_trace_value = api_environment.get("MINERU_PHASE_TRACE")
    if phase_trace_value not in {"0", "1"}:
        raise ValueError("remote API phase-trace switch is not closed")
    if compatibility.get("phase_trace_enabled") is not (phase_trace_value == "1"):
        raise ValueError("remote API phase-trace observation and environment drifted")
    client = client_bundle_identity(mineru_bin)
    code_digest = writer_code_digest()
    api_command = _command(api)
    proxy_command = _command(proxy)
    inference_command = _command(inference)
    if (
        api_command != EXPECTED_API_COMMAND
        or inference_command != EXPECTED_INFERENCE_COMMAND
    ):
        raise ValueError("remote MinerU command drifted")
    capacity_runtime_compatibility_sha256 = canonical_payload_sha256(
        {
            "api_command": api_command,
            "api_image_id": api.get("image_id"),
            "base_image_id": EXPECTED_IMAGE_ID,
            "compatibility_labels": compatibility.get("image_labels"),
            "inference_command": inference_command,
            "inference_concurrency": MINERU_API_INFERENCE_MAX_CONCURRENCY,
            "model_repository": served_model.get("repository"),
            "model_revision": served_model.get("revision"),
            "processing_window_size": MINERU_PROCESSING_WINDOW_SIZE,
            "hybrid_batch_ratio": MINERU_HYBRID_BATCH_RATIO,
            "pipeline_inference_locks": MINERU_PIPELINE_INFERENCE_LOCKS_ENABLED,
            "task_slots": task_slots,
            "max_pending_tasks_requested": pending_requested,
            "max_pending_tasks_effective": pending_effective,
            "vllm_max_num_seqs": 128,
            "vllm_version": served_model.get("vllm_version"),
        }
    )
    if (
        len(proxy_command) != 4
        or proxy_command[:3] != ["/usr/bin/python3.12", "-I", "-c"]
        or "sha256:" + hashlib.sha256(proxy_command[3].encode("utf-8")).hexdigest()
        != EXPECTED_PROXY_CODE_SHA256
        or proxy.get("read_only_rootfs") is not True
        or proxy.get("cap_drop") != ["ALL"]
        or sorted(proxy.get("security_opt", []))
        != ["label=disable", "no-new-privileges=true"]
    ):
        raise ValueError("remote API proxy policy drifted")

    manifest = {
        "contract_version": RUNTIME_MANIFEST_CONTRACT,
        "client": {
            "package_set_sha256": client.package_set_sha256,
            "writer_code_sha256": code_digest,
            **dict(client.content_package_versions),
        },
        "orchestrator": {
            "container_image_digest": api.get("image_id"),
            "base_container_image_digest": EXPECTED_IMAGE_ID,
            "content_environment_sha256": canonical_payload_sha256(api_environment),
            "service_config_sha256": canonical_payload_sha256(
                {
                    "compose_sha256": observation.get("compose_sha256"),
                    "compose_config_sha256": observation.get("compose_config_sha256"),
                    "image": api.get("image"),
                    "image_id": api.get("image_id"),
                    "command": api_command,
                    "proxy_command": proxy_command,
                    "proxy_restart_policy": proxy.get("restart_policy"),
                    "restart_policy": api.get("restart_policy"),
                }
            ),
            "mount_policy_sha256": canonical_payload_sha256(api.get("mounts")),
            "network_policy_sha256": canonical_payload_sha256(
                {
                    "api_networks": api.get("networks"),
                    "api_port": api.get("port"),
                    "proxy_networks": proxy.get("networks"),
                    "proxy_port": proxy.get("port"),
                }
            ),
            "heap_return_compatibility_sha256": canonical_payload_sha256(
                compatibility
            ),
            "heap_return_policy": MINERU_HEAP_RETURN_POLICY,
            "mineru_version": "3.4.4",
            "api_protocol_version": MINERU_API_PROTOCOL_VERSION,
            "max_concurrent_requests": task_slots,
            "max_pending_tasks_requested": pending_requested,
            "max_pending_tasks_effective": pending_effective,
            "inference_max_concurrency": MINERU_API_INFERENCE_MAX_CONCURRENCY,
            "hybrid_batch_ratio": MINERU_HYBRID_BATCH_RATIO,
            "pipeline_inference_locks": MINERU_PIPELINE_INFERENCE_LOCKS_ENABLED,
            "processing_window_size": MINERU_PROCESSING_WINDOW_SIZE,
            "task_retention_seconds": MINERU_API_TASK_RETENTION_SECONDS,
            "task_cleanup_interval_seconds": MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS,
            "output_root_policy": MINERU_API_OUTPUT_ROOT_POLICY,
            "command": api_command,
            "capacity_runtime_compatibility_sha256": (
                capacity_runtime_compatibility_sha256
            ),
        },
        "inference_server": {
            "container_image_digest": EXPECTED_REPO_DIGEST.removeprefix("mineru@"),
            "content_environment_sha256": canonical_payload_sha256(
                inference_environment
            ),
            "server_config_sha256": canonical_payload_sha256(
                {
                    "compose_sha256": observation.get("compose_sha256"),
                    "compose_config_sha256": observation.get("compose_config_sha256"),
                    "command": inference_command,
                    "restart_policy": inference.get("restart_policy"),
                    "mounts": inference.get("mounts"),
                    "networks": inference.get("networks"),
                    "port": inference.get("port"),
                }
            ),
            "mineru_version": "3.4.4",
            "max_model_len": served_model.get("max_model_len"),
            "model_repository": served_model.get("repository"),
            "served_model_id": served_model.get("id"),
            "model_snapshot_revision": served_model.get("revision"),
            "vllm_version": served_model.get("vllm_version"),
            "command": inference_command,
        },
        "topology": {
            "api_transport": MINERU_API_TRANSPORT_PROFILE,
            "api_exposure": MINERU_API_EXPOSURE_POLICY,
            "orchestrator_egress_policy": MINERU_API_EGRESS_POLICY,
            "api_endpoint_sha256": _endpoint_sha256(api_url),
            "observability_endpoint_sha256": _endpoint_sha256(observability_url),
            "inference_upstream_sha256": _endpoint_sha256(inference_upstream_url),
            "ssh_host_key_sha256": ssh_host_key_sha256,
            "windows_node_identity_sha256": observation.get(
                "windows_node_identity_sha256"
            ),
            "windows_compose_path": EXPECTED_COMPOSE_PATH,
            "windows_compose_sha256": expected_compose_sha256,
            "windows_collector_path": expected_collector_path,
            "windows_collector_sha256": expected_collector_sha256,
        },
    }
    identity = canonical_payload_sha256(manifest)
    payload = {"identity_sha256": identity, "manifest": manifest}
    verify_runtime_manifest_payload(
        payload,
        configured_identity=identity,
        local_client_identity=client,
        local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
        local_writer_code_digest=code_digest,
    )
    return payload


def _new_private_json(path: Path, payload: object) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write("\n")


def _ssh_base(
    *,
    host: str,
    user: str,
    port: int,
    identity_file: Path,
    known_hosts_file: Path,
) -> list[str]:
    if (
        SSH_HOST_RE.fullmatch(host) is None
        or SSH_USER_RE.fullmatch(user) is None
        or port != 22
    ):
        raise ValueError("SSH destination is invalid")
    return [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-i",
        str(identity_file),
        "-p",
        str(port),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "CheckHostIP=no",
        "-o",
        "ConnectTimeout=15",
        "--",
        f"{user}@{host}",
    ]


def _read_remote_file(
    command: list[str],
    *,
    remote_path: str,
    allowed_remote_path: str,
) -> bytes:
    if _windows_path(remote_path) != _windows_path(allowed_remote_path):
        raise ValueError("remote evidence path is not allowlisted")
    escaped = remote_path.replace("'", "''")
    script = (
        f"$expected='{escaped}';"
        "$item=Get-Item -LiteralPath $expected -Force -ErrorAction Stop;"
        "if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)"
        "{throw 'remote evidence file is a reparse point'};"
        "if(-not $item.FullName.Equals($expected,[StringComparison]::OrdinalIgnoreCase))"
        "{throw 'remote evidence path canonicalization drifted'};"
        "$parent=$item.Directory;"
        "while($null -ne $parent){"
        "if(($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)"
        "{throw 'remote evidence parent is a reparse point'};"
        "$parent=$parent.Parent};"
        "[Convert]::ToBase64String([IO.File]::ReadAllBytes($item.FullName))"
    )
    completed = subprocess.run(
        [
            *command,
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        payload = base64.b64decode(completed.stdout.strip(), validate=True)
    except ValueError as exc:
        raise ValueError("remote evidence bytes are not canonical base64") from exc
    if not payload or len(payload) > 2 * 1024 * 1024:
        raise ValueError("remote evidence file is empty or oversized")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="attest_mineru_remote_runtime")
    parser.add_argument("--mineru-bin", type=Path, required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-user", required=True)
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--known-hosts-file", type=Path, required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:30002")
    parser.add_argument("--observability-url", default="http://127.0.0.1:30001/v1")
    parser.add_argument(
        "--inference-upstream-url",
        default="http://mineru-openai-server:30000/v1",
    )
    parser.add_argument("--manifest-out", type=Path, required=True)
    parser.add_argument("--observation-out", type=Path, required=True)
    parser.add_argument(
        "--expected-compose",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "config"
        / "mineru-windows.compose.yaml",
    )
    parser.add_argument(
        "--collector-source",
        type=Path,
        default=Path(__file__).resolve().parent
        / "windows"
        / "collect_mineru_runtime.ps1",
    )
    parser.add_argument(
        "--remote-collector-path",
        default=EXPECTED_COLLECTOR_PATH,
    )
    parser.add_argument(
        "--compat-patcher-source",
        type=Path,
        default=Path(__file__).resolve().parent
        / "windows"
        / "mineru_heap_trim_compat"
        / "patch_mineru_344.py",
    )
    parser.add_argument(
        "--compat-dockerfile-source",
        type=Path,
        default=Path(__file__).resolve().parent
        / "windows"
        / "mineru_heap_trim_compat"
        / "Dockerfile",
    )
    args = parser.parse_args(argv)
    remote_collector_path = _canonical_remote_collector_path(
        args.remote_collector_path
    )
    _private_regular_file(args.identity_file, label="SSH identity")
    host_key_sha256 = _known_host_key_sha256(
        args.known_hosts_file, expected_host=args.ssh_host
    )
    command = _ssh_base(
        host=args.ssh_host,
        user=args.ssh_user,
        port=args.ssh_port,
        identity_file=args.identity_file,
        known_hosts_file=args.known_hosts_file,
    )
    expected_compose_bytes = args.expected_compose.read_bytes()
    expected_collector_bytes = args.collector_source.read_bytes()
    expected_compat_patcher_sha256 = (
        "sha256:" + hashlib.sha256(args.compat_patcher_source.read_bytes()).hexdigest()
    )
    expected_compat_dockerfile_sha256 = (
        "sha256:"
        + hashlib.sha256(args.compat_dockerfile_source.read_bytes()).hexdigest()
    )
    if _read_remote_file(
        command,
        remote_path=EXPECTED_COMPOSE_PATH,
        allowed_remote_path=EXPECTED_COMPOSE_PATH,
    ) != (
        expected_compose_bytes
    ) or _read_remote_file(
        command,
        remote_path=remote_collector_path,
        allowed_remote_path=remote_collector_path,
    ) != (
        expected_collector_bytes
    ):
        raise SystemExit("[abort] remote compose or collector bytes drifted")
    collector_command = [
        *command,
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        remote_collector_path,
    ]
    completed = subprocess.run(
        collector_command,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    observation = json.loads(completed.stdout)
    if not isinstance(observation, dict):
        raise SystemExit("[abort] remote observation root is not an object")
    expected_compose_sha256 = (
        "sha256:" + hashlib.sha256(expected_compose_bytes).hexdigest()
    )
    expected_collector_sha256 = (
        "sha256:" + hashlib.sha256(expected_collector_bytes).hexdigest()
    )
    payload = build_manifest(
        observation,
        mineru_bin=args.mineru_bin,
        ssh_host_key_sha256=host_key_sha256,
        api_url=args.api_url,
        observability_url=args.observability_url,
        inference_upstream_url=args.inference_upstream_url,
        expected_compose_sha256=expected_compose_sha256,
        expected_collector_sha256=expected_collector_sha256,
        expected_compat_patcher_sha256=expected_compat_patcher_sha256,
        expected_compat_dockerfile_sha256=expected_compat_dockerfile_sha256,
        expected_collector_path=remote_collector_path,
    )
    _new_private_json(args.observation_out, observation)
    _new_private_json(args.manifest_out, payload)
    print(payload["identity_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
