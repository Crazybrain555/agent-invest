"""Exact local and attested MinerU runtime identity helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any


RUNTIME_MANIFEST_CONTRACT = "mineru-runtime-bundle.v6"
MINERU_PROCESSING_WINDOW_SIZE = 16
MINERU_API_PROTOCOL_VERSION = 2
MINERU_API_DEFAULT_TASK_SLOTS = 1
MINERU_API_MAX_SUPPORTED_TASK_SLOTS = 3
MINERU_API_INFERENCE_MAX_CONCURRENCY = 7
MINERU_API_TASK_RETENTION_SECONDS = 600
MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS = 30
MINERU_API_OUTPUT_ROOT_POLICY = "dedicated-scratch-retention.v1"
MINERU_API_TRANSPORT_PROFILE = "pinned-ssh-local-forward.v1"
MINERU_API_EXPOSURE_POLICY = "windows-loopback-only.v1"
MINERU_API_EGRESS_POLICY = "dedicated-internal-vllm-only.v1"
MINERU_HEAP_RETURN_POLICY = "glibc-malloc-trim-per-window.v1"
MINERU_WINDOWS_COMPOSE_PATH = r"C:\ProgramData\compose.tailnet.yaml"
MINERU_WINDOWS_COLLECTOR_PATH = (
    r"C:\ProgramData\agent-invest\mineru-runtime-v6\collect_mineru_runtime.ps1"
)
MINERU_SMOKE_INPUT_NAME = "sample_announcement.pdf"
MINERU_SMOKE_INPUT_SHA256 = (
    "sha256:863da8f0e9aba6a19b9cb697265d4898fccaa4f5f457a93f5cc0c847b398e93f"
)
MINERU_CONTENT_PACKAGE_VERSIONS = {
    "mineru_version": "3.4.4",
    "pdftext_version": "0.6.3",
    "pypdfium2_version": "4.30.0",
    "mineru_vl_utils_version": "1.0.5",
}
_CONTENT_PACKAGE_DISTRIBUTIONS = {
    "mineru_version": "mineru",
    "pdftext_version": "pdftext",
    "pypdfium2_version": "pypdfium2",
    "mineru_vl_utils_version": "mineru-vl-utils",
}
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_REVISION_RE = re.compile(r"^[a-f0-9]{40,64}$")
_NORMALIZED_DISTRIBUTION_RE = re.compile(r"[-_.]+")
_MANIFEST_FIELDS = {
    "contract_version",
    "client",
    "orchestrator",
    "inference_server",
    "topology",
}
_CLIENT_MANIFEST_FIELDS = {
    "package_set_sha256",
    "writer_code_sha256",
    *MINERU_CONTENT_PACKAGE_VERSIONS,
}
_ORCHESTRATOR_MANIFEST_FIELDS = {
    "container_image_digest",
    "base_container_image_digest",
    "content_environment_sha256",
    "service_config_sha256",
    "mount_policy_sha256",
    "network_policy_sha256",
    "heap_return_compatibility_sha256",
    "capacity_runtime_compatibility_sha256",
    "heap_return_policy",
    "mineru_version",
    "api_protocol_version",
    "max_concurrent_requests",
    "inference_max_concurrency",
    "processing_window_size",
    "task_retention_seconds",
    "task_cleanup_interval_seconds",
    "output_root_policy",
    "command",
}
_INFERENCE_SERVER_MANIFEST_FIELDS = {
    "container_image_digest",
    "content_environment_sha256",
    "server_config_sha256",
    "mineru_version",
    "max_model_len",
    "model_repository",
    "served_model_id",
    "model_snapshot_revision",
    "vllm_version",
    "command",
}
_TOPOLOGY_MANIFEST_FIELDS = {
    "api_transport",
    "api_exposure",
    "orchestrator_egress_policy",
    "api_endpoint_sha256",
    "observability_endpoint_sha256",
    "inference_upstream_sha256",
    "ssh_host_key_sha256",
    "windows_node_identity_sha256",
    "windows_compose_path",
    "windows_compose_sha256",
    "windows_collector_path",
    "windows_collector_sha256",
}
_CREDENTIAL_COMMAND_FLAGS = {
    "--access-token",
    "--api-key",
    "--auth-token",
    "--hf-token",
    "--password",
    "--secret",
    "--token",
}
_WRITER_CODE_RELPATHS = (
    "scripts/mineru_smoke.py",
    "scripts/mineru_staged_load.py",
    "src/disclosure_anchor/adapters/parsers/mineru_medium/artifacts.py",
    "src/disclosure_anchor/adapters/parsers/mineru_medium/parser.py",
    "src/disclosure_anchor/adapters/parsers/mineru_medium/process.py",
    "src/disclosure_anchor/adapters/runtime/mineru_canary.py",
    "src/disclosure_anchor/adapters/runtime/mineru_deployment_gate.py",
    "src/disclosure_anchor/adapters/runtime/mineru_identity.py",
    "src/disclosure_anchor/adapters/runtime/mineru_orchestrator.py",
    "src/disclosure_anchor/adapters/runtime/mineru_process_isolation.py",
    "src/disclosure_anchor/adapters/storage/provider_document_source.py",
    "src/disclosure_anchor/application/ports/parser.py",
)


@dataclass(frozen=True)
class MinerUClientIdentity:
    package_set_sha256: str
    python_version: str
    content_package_versions: Mapping[str, str]


@dataclass(frozen=True)
class VerifiedMinerURuntimeManifest:
    manifest: dict[str, Any]
    identity_sha256: str
    orchestrator_identity_sha256: str
    provider_identity_sha256: str
    served_model_id: str
    max_concurrent_requests: int


def canonical_payload_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def client_bundle_identity(mineru_bin: Path) -> MinerUClientIdentity:
    python = mineru_bin.parent / "python"
    if not python.is_file():
        raise ValueError(f"venv python not found next to {mineru_bin}")
    try:
        listing = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                (
                    "import json, sys\n"
                    "from importlib.metadata import distributions\n"
                    "names = sorted(\n"
                    "    f\"{d.metadata['Name']}=={d.version}\"\n"
                    "    for d in distributions()\n"
                    "    if d.metadata['Name']\n"
                    ")\n"
                    "json.dump({'python_version': sys.version.split()[0], "
                    "'packages': names}, sys.stdout)\n"
                ),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
        manifest = json.loads(listing)
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("cannot inspect MinerU client environment") from exc
    if not isinstance(manifest, dict):
        raise ValueError("MinerU client environment root must be an object")
    python_version = manifest.get("python_version")
    packages = manifest.get("packages")
    if (
        not isinstance(python_version, str)
        or not python_version
        or not isinstance(packages, list)
        or not all(isinstance(item, str) and "==" in item for item in packages)
    ):
        raise ValueError("MinerU client environment has invalid package metadata")
    versions: dict[str, str] = {}
    for item in packages:
        name, separator, version = item.partition("==")
        normalized = _normalize_distribution_name(name)
        if not separator or not normalized or not version or normalized in versions:
            raise ValueError("MinerU client package listing is ambiguous")
        versions[normalized] = version
    content_versions: dict[str, str] = {}
    for field, distribution in _CONTENT_PACKAGE_DISTRIBUTIONS.items():
        version = versions.get(_normalize_distribution_name(distribution))
        if version is None:
            raise ValueError(f"MinerU client package is missing: {distribution}")
        content_versions[field] = version
    canonical_manifest = {
        "python_version": python_version,
        "packages": packages,
    }
    return MinerUClientIdentity(
        package_set_sha256=canonical_payload_sha256(canonical_manifest),
        python_version=python_version,
        content_package_versions=content_versions,
    )


def client_bundle_digest(mineru_bin: Path) -> str:
    """Compatibility wrapper for callers that need only the exact digest."""

    return client_bundle_identity(mineru_bin).package_set_sha256


def verify_runtime_manifest_payload(
    payload: object,
    *,
    configured_identity: str,
    local_client_identity: MinerUClientIdentity,
    local_processing_window_size: int,
    local_writer_code_digest: str,
) -> VerifiedMinerURuntimeManifest:
    if not isinstance(payload, dict):
        raise ValueError("runtime attestation root must be an object")
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("runtime attestation manifest must be an object")
    if manifest.get("contract_version") != RUNTIME_MANIFEST_CONTRACT:
        raise ValueError(
            f"runtime manifest contract must be {RUNTIME_MANIFEST_CONTRACT}"
        )
    if set(manifest) != _MANIFEST_FIELDS:
        raise ValueError("runtime manifest fields are not closed")
    manifest_identity = canonical_payload_sha256(manifest)
    if payload.get("identity_sha256") != manifest_identity:
        raise ValueError("runtime attestation self-hash is invalid")
    if manifest_identity != configured_identity:
        raise ValueError(
            "runtime manifest identity does not match "
            "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256"
        )
    local = manifest.get("client")
    orchestrator = manifest.get("orchestrator")
    inference_server = manifest.get("inference_server")
    topology = manifest.get("topology")
    if (
        not isinstance(local, dict)
        or not isinstance(orchestrator, dict)
        or not isinstance(inference_server, dict)
        or not isinstance(topology, dict)
    ):
        raise ValueError(
            "runtime manifest client/orchestrator/inference_server/topology "
            "must be objects"
        )
    if set(local) != _CLIENT_MANIFEST_FIELDS:
        raise ValueError("runtime manifest client fields are not closed")
    if set(orchestrator) != _ORCHESTRATOR_MANIFEST_FIELDS:
        raise ValueError("runtime manifest orchestrator fields are not closed")
    if set(inference_server) != _INFERENCE_SERVER_MANIFEST_FIELDS:
        raise ValueError("runtime manifest inference-server fields are not closed")
    if set(topology) != _TOPOLOGY_MANIFEST_FIELDS:
        raise ValueError("runtime manifest topology fields are not closed")
    if local.get("package_set_sha256") != local_client_identity.package_set_sha256:
        raise ValueError("runtime manifest local client digest is stale")
    if local.get("writer_code_sha256") != local_writer_code_digest:
        raise ValueError("runtime manifest local writer code drifted")
    for field, expected in MINERU_CONTENT_PACKAGE_VERSIONS.items():
        measured = local_client_identity.content_package_versions.get(field)
        if measured != expected:
            raise ValueError(
                f"measured MinerU client {field} must be pinned to {expected}"
            )
        if local.get(field) != measured:
            raise ValueError(f"runtime manifest local {field} drifted")

    _verify_orchestrator_manifest(
        orchestrator,
        expected_processing_window_size=local_processing_window_size,
    )
    _verify_inference_server_manifest(inference_server)
    _verify_topology_manifest(topology)
    served_model_id = inference_server["served_model_id"]
    return VerifiedMinerURuntimeManifest(
        manifest=dict(manifest),
        identity_sha256=manifest_identity,
        orchestrator_identity_sha256=canonical_payload_sha256(orchestrator),
        provider_identity_sha256=canonical_payload_sha256(inference_server),
        served_model_id=served_model_id,
        max_concurrent_requests=int(orchestrator["max_concurrent_requests"]),
    )


def _verify_orchestrator_manifest(
    orchestrator: dict[str, Any],
    *,
    expected_processing_window_size: int,
) -> None:
    _require_sha256(
        orchestrator.get("container_image_digest"),
        label="orchestrator image digest",
    )
    _require_sha256(
        orchestrator.get("base_container_image_digest"),
        label="orchestrator base image digest",
    )
    for field in (
        "content_environment_sha256",
        "service_config_sha256",
        "mount_policy_sha256",
        "network_policy_sha256",
        "heap_return_compatibility_sha256",
        "capacity_runtime_compatibility_sha256",
    ):
        _require_sha256(
            orchestrator.get(field),
            label=f"orchestrator {field}",
        )
    if orchestrator.get("heap_return_policy") != MINERU_HEAP_RETURN_POLICY:
        raise ValueError("runtime manifest orchestrator heap-return policy drifted")
    if (
        orchestrator.get("mineru_version")
        != MINERU_CONTENT_PACKAGE_VERSIONS["mineru_version"]
    ):
        raise ValueError("runtime manifest orchestrator MinerU version drifted")
    task_slots = orchestrator.get("max_concurrent_requests")
    if (
        isinstance(task_slots, bool)
        or not isinstance(task_slots, int)
        or not 1 <= task_slots <= MINERU_API_MAX_SUPPORTED_TASK_SLOTS
    ):
        raise ValueError(
            "runtime manifest orchestrator max_concurrent_requests must be "
            f"between 1 and {MINERU_API_MAX_SUPPORTED_TASK_SLOTS}"
        )
    fixed_values = {
        "api_protocol_version": MINERU_API_PROTOCOL_VERSION,
        "inference_max_concurrency": MINERU_API_INFERENCE_MAX_CONCURRENCY,
        "processing_window_size": MINERU_PROCESSING_WINDOW_SIZE,
        "task_retention_seconds": MINERU_API_TASK_RETENTION_SECONDS,
        "task_cleanup_interval_seconds": MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS,
    }
    for field, expected in fixed_values.items():
        value = orchestrator.get(field)
        if isinstance(value, bool) or value != expected:
            raise ValueError(
                f"runtime manifest orchestrator {field} must be {expected}"
            )
    if expected_processing_window_size != MINERU_PROCESSING_WINDOW_SIZE:
        raise ValueError(
            "local expected MinerU processing window drifted from the v3 contract"
        )
    if orchestrator.get("output_root_policy") != MINERU_API_OUTPUT_ROOT_POLICY:
        raise ValueError("runtime manifest orchestrator output-root policy drifted")
    command = _verified_command(
        orchestrator.get("command"),
        component="orchestrator",
    )
    if command[0] != "mineru-api":
        raise ValueError("runtime manifest orchestrator command is not mineru-api")
    if _unique_flag_value(
        command,
        "--max-concurrency",
        component="orchestrator",
    ) != str(MINERU_API_INFERENCE_MAX_CONCURRENCY):
        raise ValueError("runtime manifest orchestrator must pin max_concurrency=7")


def _verify_inference_server_manifest(inference_server: dict[str, Any]) -> None:
    _require_sha256(
        inference_server.get("container_image_digest"),
        label="inference-server image digest",
    )
    for field in ("model_repository", "served_model_id", "vllm_version"):
        value = inference_server.get(field)
        if (
            not isinstance(value, str)
            or not value
            or value.lower() in {"main", "latest"}
        ):
            raise ValueError(f"runtime manifest inference-server {field} is not pinned")
    revision = inference_server.get("model_snapshot_revision")
    if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
        raise ValueError(
            "runtime manifest inference-server model revision is not immutable"
        )
    for field in ("server_config_sha256", "content_environment_sha256"):
        _require_sha256(
            inference_server.get(field),
            label=f"inference-server {field}",
        )
    if (
        inference_server.get("mineru_version")
        != MINERU_CONTENT_PACKAGE_VERSIONS["mineru_version"]
    ):
        raise ValueError("runtime manifest inference-server MinerU version drifted")
    if inference_server.get("max_model_len") != 8192:
        raise ValueError("runtime manifest must pin max_model_len=8192")
    command = _verified_command(
        inference_server.get("command"),
        component="inference-server",
    )
    if command[0] != "mineru-openai-server":
        raise ValueError(
            "runtime manifest inference-server command is not mineru-openai-server"
        )
    if (
        _unique_flag_value(
            command,
            "--max-num-seqs",
            component="inference-server",
        )
        != "128"
    ):
        raise ValueError("runtime manifest must pin max_num_seqs=128")
    if (
        _unique_flag_value(
            command,
            "--mm-processor-cache-gb",
            component="inference-server",
        )
        != "0"
    ):
        raise ValueError("runtime manifest must pin mm_processor_cache_gb=0")


def _verify_topology_manifest(topology: dict[str, Any]) -> None:
    expected = {
        "api_transport": MINERU_API_TRANSPORT_PROFILE,
        "api_exposure": MINERU_API_EXPOSURE_POLICY,
        "orchestrator_egress_policy": MINERU_API_EGRESS_POLICY,
    }
    for field, value in expected.items():
        if topology.get(field) != value:
            raise ValueError(f"runtime manifest topology {field} drifted")
    for field in (
        "api_endpoint_sha256",
        "observability_endpoint_sha256",
        "inference_upstream_sha256",
        "ssh_host_key_sha256",
        "windows_node_identity_sha256",
        "windows_compose_sha256",
        "windows_collector_sha256",
    ):
        _require_sha256(topology.get(field), label=f"topology {field}")
    if topology.get("windows_compose_path") != MINERU_WINDOWS_COMPOSE_PATH:
        raise ValueError("runtime manifest Windows compose path drifted")
    if topology.get("windows_collector_path") != MINERU_WINDOWS_COLLECTOR_PATH:
        raise ValueError("runtime manifest Windows collector path drifted")


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"runtime manifest {label} is not pinned")
    return value


def _verified_command(value: object, *, component: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError(f"runtime manifest {component} command is invalid")
    if any(_is_credential_command_token(item) for item in value):
        raise ValueError(
            f"runtime manifest {component} command must not contain credentials"
        )
    return value


def writer_code_digest() -> str:
    """Bind a smoke receipt to the exact local writer/gate source bytes."""

    service_root = Path(__file__).resolve().parents[4]
    digest = hashlib.sha256()
    for relpath in _WRITER_CODE_RELPATHS:
        path = service_root / relpath
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"MinerU writer source is missing or unsafe: {relpath}")
        payload = path.read_bytes()
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _normalize_distribution_name(value: str) -> str:
    return _NORMALIZED_DISTRIBUTION_RE.sub("-", value).lower()


def _unique_flag_value(
    command: list[str],
    flag: str,
    *,
    component: str,
) -> str:
    positions = [index for index, value in enumerate(command) if value == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        raise ValueError(
            f"runtime manifest {component} {flag} must occur exactly once with a value"
        )
    value = command[positions[0] + 1]
    if value.startswith("-"):
        raise ValueError(f"runtime manifest {component} {flag} value is invalid")
    return value


def _is_credential_command_token(value: str) -> bool:
    normalized = value.lower().replace("_", "-")
    return any(
        normalized == flag or normalized.startswith(f"{flag}=")
        for flag in _CREDENTIAL_COMMAND_FLAGS
    )


__all__ = [
    "MINERU_API_EGRESS_POLICY",
    "MINERU_API_EXPOSURE_POLICY",
    "MINERU_API_INFERENCE_MAX_CONCURRENCY",
    "MINERU_API_DEFAULT_TASK_SLOTS",
    "MINERU_API_MAX_SUPPORTED_TASK_SLOTS",
    "MINERU_API_OUTPUT_ROOT_POLICY",
    "MINERU_API_PROTOCOL_VERSION",
    "MINERU_API_TASK_RETENTION_SECONDS",
    "MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS",
    "MINERU_API_TRANSPORT_PROFILE",
    "MINERU_CONTENT_PACKAGE_VERSIONS",
    "MINERU_HEAP_RETURN_POLICY",
    "MINERU_PROCESSING_WINDOW_SIZE",
    "MINERU_SMOKE_INPUT_NAME",
    "MINERU_SMOKE_INPUT_SHA256",
    "MINERU_WINDOWS_COLLECTOR_PATH",
    "MINERU_WINDOWS_COMPOSE_PATH",
    "RUNTIME_MANIFEST_CONTRACT",
    "MinerUClientIdentity",
    "VerifiedMinerURuntimeManifest",
    "canonical_payload_sha256",
    "client_bundle_digest",
    "client_bundle_identity",
    "verify_runtime_manifest_payload",
    "writer_code_digest",
]
