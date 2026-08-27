"""Exact-current runtime identity verification for Capacity Observation v1."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_PROCESSING_WINDOW_SIZE,
    client_bundle_identity,
    verify_runtime_manifest_payload,
    writer_code_digest,
)
from disclosure_anchor.settings import Settings


@dataclass(frozen=True, slots=True)
class CapacityRuntimeTopology:
    windows_collector_sha256: str
    windows_node_identity_sha256: str
    ssh_host_key_sha256: str


def _endpoint_sha256(url: str) -> str:
    return "sha256:" + hashlib.sha256(url.rstrip("/").encode("utf-8")).hexdigest()


def verify_capacity_runtime_topology(
    *,
    settings: Settings,
    runtime_manifest_path: Path,
) -> CapacityRuntimeTopology:
    """Verify the manifest, configured slots and all observed endpoint identities."""

    if settings.disclosure_mineru_bin is None:
        raise ValueError("DISCLOSURE_MINERU_BIN is required")
    configured_identity = settings.disclosure_mineru_runtime_bundle_identity_sha256
    if configured_identity is None:
        raise ValueError(
            "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256 is required"
        )
    payload = json.loads(runtime_manifest_path.read_bytes())
    verified = verify_runtime_manifest_payload(
        payload,
        configured_identity=configured_identity,
        local_client_identity=client_bundle_identity(settings.disclosure_mineru_bin),
        local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
        local_writer_code_digest=writer_code_digest(),
    )
    if verified.max_concurrent_requests != settings.disclosure_mineru_api_task_slots:
        raise ValueError("runtime manifest task slots drifted from worker configuration")
    topology = verified.manifest["topology"]
    if not isinstance(topology, dict):
        raise ValueError("runtime topology is invalid")
    endpoint_values = {
        "api_endpoint_sha256": settings.disclosure_mineru_api_url,
        "observability_endpoint_sha256": (
            settings.disclosure_mineru_observability_url
        ),
        "inference_upstream_sha256": (
            settings.disclosure_mineru_inference_upstream_url
        ),
    }
    if any(value is None for value in endpoint_values.values()):
        raise ValueError("complete MinerU endpoint topology is required")
    for field, url in endpoint_values.items():
        assert url is not None
        if topology.get(field) != _endpoint_sha256(url):
            raise ValueError(f"runtime endpoint identity drifted: {field}")
    values = {
        "windows_collector_sha256": topology.get("windows_collector_sha256"),
        "windows_node_identity_sha256": topology.get(
            "windows_node_identity_sha256"
        ),
        "ssh_host_key_sha256": topology.get("ssh_host_key_sha256"),
    }
    if not all(isinstance(value, str) for value in values.values()):
        raise ValueError("runtime host observer identity is incomplete")
    return CapacityRuntimeTopology(
        windows_collector_sha256=str(values["windows_collector_sha256"]),
        windows_node_identity_sha256=str(values["windows_node_identity_sha256"]),
        ssh_host_key_sha256=str(values["ssh_host_key_sha256"]),
    )


__all__ = ["CapacityRuntimeTopology", "verify_capacity_runtime_topology"]
