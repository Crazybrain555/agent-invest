"""Build a v3 manifest from a live, pinned-SSH Windows observation."""

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
    MINERU_API_MAX_CONCURRENT_REQUESTS,
    MINERU_API_OUTPUT_ROOT_POLICY,
    MINERU_API_PROTOCOL_VERSION,
    MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS,
    MINERU_API_TASK_RETENTION_SECONDS,
    MINERU_API_TRANSPORT_PROFILE,
    MINERU_PROCESSING_WINDOW_SIZE,
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
API_ENV_KEYS = {
    "MINERU_MODEL_SOURCE",
    "MINERU_API_MAX_CONCURRENT_REQUESTS",
    "MINERU_PROCESSING_WINDOW_SIZE",
    "MINERU_API_OUTPUT_ROOT",
    "MINERU_API_TASK_RETENTION_SECONDS",
    "MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS",
    "MINERU_API_DISABLE_ACCESS_LOG",
    "MINERU_API_ENABLE_FASTAPI_DOCS",
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
EXPECTED_COMPOSE_PATH = r"C:\ProgramData\compose.tailnet.yaml"
EXPECTED_COLLECTOR_PATH = (
    r"C:\ProgramData\agent-invest\mineru\collect_mineru_runtime.ps1"
)
EXPECTED_OUTPUT_ROOT = r"C:\ProgramData\agent-invest\mineru-api-output"
EXPECTED_MODEL_REPOSITORY = "opendatalab/MinerU2.5-Pro-2605-1.2B"
EXPECTED_MODEL_REVISION = "bff20d4ae2bf202df9f45284b4d43681555a97ed"
EXPECTED_PROXY_CODE_SHA256 = (
    "sha256:991ff233fb77188f402dba81a8ebb6519630122087a8b5744396e7ebd8c63922"
)
SSH_HOST_RE = re.compile(r"^(?!-)[A-Za-z0-9.-]+$")
SSH_USER_RE = re.compile(r"^(?!-)[A-Za-z0-9_.-]+$")


def _windows_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("remote Windows path is invalid")
    return value.replace("/", "\\").rstrip("\\").casefold()


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
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in values.items())
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
) -> dict[str, Any]:
    if observation.get("schema") != "mineru-windows-runtime-observation.v2":
        raise ValueError("remote runtime observation contract drifted")
    api = observation.get("api")
    proxy = observation.get("proxy")
    inference = observation.get("inference")
    health = observation.get("api_health")
    served_model = observation.get("served_model")
    if not all(
        isinstance(item, dict)
        for item in (api, proxy, inference, health, served_model)
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
        != _windows_path(EXPECTED_COLLECTOR_PATH)
    ):
        raise ValueError("remote compose or collector path/bytes drifted")
    if (
        api.get("image") != EXPECTED_REPO_DIGEST
        or proxy.get("image") != EXPECTED_REPO_DIGEST
        or inference.get("image") != EXPECTED_REPO_DIGEST
        or api.get("image_id") != EXPECTED_IMAGE_ID
        or proxy.get("image_id") != EXPECTED_IMAGE_ID
        or inference.get("image_id") != EXPECTED_IMAGE_ID
    ):
        raise ValueError("remote MinerU image digest drifted")
    if (
        health.get("status") != "healthy"
        or health.get("version") != "3.4.4"
        or health.get("protocol_version") != MINERU_API_PROTOCOL_VERSION
        or health.get("max_concurrent_requests") != MINERU_API_MAX_CONCURRENT_REQUESTS
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
        or proxy.get("port")
        != {"HostIp": "127.0.0.1", "HostPort": "30003"}
        or inference.get("port")
        != {"HostIp": "127.0.0.1", "HostPort": "30001"}
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
    mounts = api.get("mounts")
    if (
        not isinstance(mounts, list)
        or len(mounts) != 1
        or mounts[0].get("Type") != "bind"
        or _windows_path(mounts[0].get("Source"))
        != _windows_path(EXPECTED_OUTPUT_ROOT)
        or mounts[0].get("Destination") != "/var/lib/mineru-api-output"
        or mounts[0].get("RW") is not True
        or proxy.get("mounts") != []
        or inference.get("mounts") != []
    ):
        raise ValueError("remote MinerU mount policy drifted")
    output_root = observation.get("output_root")
    if (
        not isinstance(output_root, dict)
        or _windows_path(output_root.get("path"))
        != _windows_path(EXPECTED_OUTPUT_ROOT)
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

    api_environment = _environment(api.get("environment"), allowlist=API_ENV_KEYS)
    inference_environment = _environment(
        inference.get("environment"), allowlist=INFERENCE_ENV_KEYS
    )
    if _environment(proxy.get("environment"), allowlist=set()) != {}:
        raise ValueError("remote API proxy environment drifted")
    client = client_bundle_identity(mineru_bin)
    code_digest = writer_code_digest()
    api_command = _command(api)
    proxy_command = _command(proxy)
    inference_command = _command(inference)
    if api_command != EXPECTED_API_COMMAND or inference_command != EXPECTED_INFERENCE_COMMAND:
        raise ValueError("remote MinerU command drifted")
    if (
        len(proxy_command) != 4
        or proxy_command[:3] != ["/usr/bin/python3.12", "-I", "-c"]
        or "sha256:"
        + hashlib.sha256(proxy_command[3].encode("utf-8")).hexdigest()
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
            "container_image_digest": EXPECTED_REPO_DIGEST.removeprefix("mineru@"),
            "content_environment_sha256": canonical_payload_sha256(api_environment),
            "service_config_sha256": canonical_payload_sha256(
                {
                    "compose_sha256": observation.get("compose_sha256"),
                    "compose_config_sha256": observation.get("compose_config_sha256"),
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
            "mineru_version": "3.4.4",
            "api_protocol_version": MINERU_API_PROTOCOL_VERSION,
            "max_concurrent_requests": MINERU_API_MAX_CONCURRENT_REQUESTS,
            "inference_max_concurrency": MINERU_API_INFERENCE_MAX_CONCURRENCY,
            "processing_window_size": MINERU_PROCESSING_WINDOW_SIZE,
            "task_retention_seconds": MINERU_API_TASK_RETENTION_SECONDS,
            "task_cleanup_interval_seconds": MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS,
            "output_root_policy": MINERU_API_OUTPUT_ROOT_POLICY,
            "command": api_command,
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


def _read_remote_file(command: list[str], *, remote_path: str) -> bytes:
    if remote_path not in {EXPECTED_COMPOSE_PATH, EXPECTED_COLLECTOR_PATH}:
        raise ValueError("remote evidence path is not allowlisted")
    escaped = remote_path.replace("'", "''")
    completed = subprocess.run(
        [
            *command,
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "[Convert]::ToBase64String("
                f"[IO.File]::ReadAllBytes('{escaped}'))"
            ),
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
    parser.add_argument(
        "--observability-url", default="http://127.0.0.1:30001/v1"
    )
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
    args = parser.parse_args(argv)
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
    if _read_remote_file(command, remote_path=EXPECTED_COMPOSE_PATH) != (
        expected_compose_bytes
    ) or _read_remote_file(command, remote_path=EXPECTED_COLLECTOR_PATH) != (
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
        EXPECTED_COLLECTOR_PATH,
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
    expected_compose_sha256 = "sha256:" + hashlib.sha256(
        expected_compose_bytes
    ).hexdigest()
    expected_collector_sha256 = "sha256:" + hashlib.sha256(
        expected_collector_bytes
    ).hexdigest()
    payload = build_manifest(
        observation,
        mineru_bin=args.mineru_bin,
        ssh_host_key_sha256=host_key_sha256,
        api_url=args.api_url,
        observability_url=args.observability_url,
        inference_upstream_url=args.inference_upstream_url,
        expected_compose_sha256=expected_compose_sha256,
        expected_collector_sha256=expected_collector_sha256,
    )
    _new_private_json(args.observation_out, observation)
    _new_private_json(args.manifest_out, payload)
    print(payload["identity_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
