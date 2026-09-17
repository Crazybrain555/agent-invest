"""Bind a verified release to an attested runtime: profile, activation, overlay.

Inputs are already-obtained evidence (runtime bundle from the attester, the
collector observation, the deployment qualification receipt) plus explicit
private references. Every identity and structural check runs before the only
live action, two read-only loopback HTTP samples of the API (health and
pressure), so that the owner and cgroup identities are live facts, never
copied from an older binding. Nothing here deploys or admits work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import re
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from pydantic import ValidationError

from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    MinerUDeploymentGateError,
    verify_mineru_heldout_validation,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    canonical_payload_sha256,
    client_bundle_identity,
    verify_runtime_manifest_payload,
    writer_code_digest,
)
from disclosure_anchor.adapters.runtime.mineru_process_profile import load_mineru_process_profile
from disclosure_anchor.adapters.runtime.mineru_release_package import (
    ReleaseIdentityError,
    ReleaseInputError,
    VerifyReport,
    write_new_json,
)
from disclosure_anchor.adapters.runtime.mineru_stream_activation import load_mineru_stream_activation
from disclosure_anchor.application.contracts.closed_document import (
    canonical_bytes,
    load_closed_object,
    require_fields,
    require_int,
    require_str,
    sha256_of,
)
from disclosure_anchor.application.contracts.mineru_process_pressure import (
    MineruProcessPressure,
    ProcessPressureOwner,
)
from disclosure_anchor.application.contracts.mineru_process_profile import (
    EXPLICIT_PROCESS_PROFILE_CONTRACT,
    MineruProcessProfile,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


BINDING_CONTRACT = "m6.release-binding.v1"
PRIVATE_INPUTS_CONTRACT = "m6.release-bind-private-inputs.v1"
_PRIVATE_INPUT_FIELDS = frozenset({
    "contract_version", "mineru_bin", "api_url", "observability_url", "inference_upstream_url", "gpu_uuid",
    "smoke_receipt_path", "validation_receipt_path", "canary_cache_path", "canary_max_age_seconds",
})
_GPU_UUID_RE = re.compile(r"^GPU-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@%+=,-]+$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
_MAX_HTTP_BYTES = 1 << 20
_LABEL_PREFIX = "io.agent-invest.mineru."


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not followed for local readbacks", headers, fp)


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def loopback_endpoint(value: object, *, label: str) -> str:
    """Accept only an explicit loopback http base endpoint; return it normalized."""

    url = require_str(value, label=label)
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.username or parts.password or parts.query or parts.fragment:
        raise ReleaseInputError(f"{label} must be a plain http loopback endpoint without credentials, query or fragment")
    if parts.hostname not in _LOOPBACK_HOSTS:
        raise ReleaseInputError(f"{label} must address the local tunnel endpoint")
    try:
        port = parts.port
    except ValueError as exc:
        raise ReleaseInputError(f"{label} port is invalid") from exc
    if port is None or not 1 <= port <= 65535:
        raise ReleaseInputError(f"{label} must carry an explicit valid port")
    if parts.path not in ("", "/"):
        raise ReleaseInputError(f"{label} must be a base endpoint without a path")
    return f"http://{parts.hostname}:{port}"


def _read_private(path: Path, *, label: str, maximum: int = 16 * 1024 * 1024) -> bytes:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ReleaseInputError(f"{label} must be an existing absolute regular file")
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ReleaseInputError(f"{label} must be owner-only (0600) and owned by the caller")
    raw = path.read_bytes()
    if not raw or len(raw) > maximum:
        raise ReleaseInputError(f"{label} bytes are outside the closed envelope")
    return raw


@dataclass(frozen=True, slots=True)
class BindPrivateInputs:
    mineru_bin: Path
    api_url: str
    observability_url: str
    inference_upstream_url: str
    gpu_uuid: str
    smoke_receipt_path: Path
    validation_receipt_path: Path
    canary_cache_path: Path
    canary_max_age_seconds: int


def load_bind_private_inputs(path: Path) -> BindPrivateInputs:
    raw = _read_private(path, label="bind private inputs")
    try:
        value = load_closed_object(raw, label="bind private inputs", maximum_bytes=65536)
        require_fields(value, _PRIVATE_INPUT_FIELDS, label="bind private inputs")
    except ValueError as exc:
        raise ReleaseInputError(str(exc)) from exc
    if value["contract_version"] != PRIVATE_INPUTS_CONTRACT:
        raise ReleaseInputError("bind private inputs contract is unsupported")
    mineru_bin = Path(require_str(value["mineru_bin"], label="mineru_bin"))
    if not mineru_bin.is_absolute() or not mineru_bin.is_file():
        raise ReleaseInputError("mineru_bin must be an existing absolute file")
    api_url = loopback_endpoint(value["api_url"], label="api_url")
    observability_url = require_str(value["observability_url"], label="observability_url")
    inference_upstream_url = require_str(value["inference_upstream_url"], label="inference_upstream_url")
    if not observability_url.startswith("http://") or not inference_upstream_url.startswith("http://"):
        raise ReleaseInputError("observability and inference upstream URLs must be explicit http URLs")
    if _GPU_UUID_RE.fullmatch(require_str(value["gpu_uuid"], label="gpu_uuid")) is None:
        raise ReleaseInputError("bind gpu_uuid must be a canonical NVIDIA UUID")
    paths = {}
    for name in ("smoke_receipt_path", "validation_receipt_path", "canary_cache_path"):
        candidate = Path(require_str(value[name], label=name))
        if not candidate.is_absolute() or not candidate.is_file():
            raise ReleaseInputError(f"bind {name} must be an existing absolute file")
        paths[name] = candidate
    if len({p.resolve() for p in paths.values()}) != 3:
        raise ReleaseInputError("bind evidence paths must differ")
    try:
        max_age = require_int(value["canary_max_age_seconds"], label="canary_max_age_seconds", maximum=30 * 86400)
    except ValueError as exc:
        raise ReleaseInputError(str(exc)) from exc
    return BindPrivateInputs(
        mineru_bin=mineru_bin, api_url=api_url, observability_url=observability_url,
        inference_upstream_url=inference_upstream_url, gpu_uuid=value["gpu_uuid"], canary_max_age_seconds=max_age, **paths,
    )


def load_runtime_bundle(path: Path) -> tuple[dict[str, Any], str]:
    raw = _read_private(path, label="runtime bundle")
    wrapper = strict_json_loads(raw)
    if type(wrapper) is not dict or set(wrapper) != {"identity_sha256", "manifest"}:
        raise ReleaseInputError("runtime bundle must carry exactly identity_sha256 and manifest")
    manifest = wrapper["manifest"]
    identity = wrapper["identity_sha256"]
    if type(manifest) is not dict or type(identity) is not str or canonical_payload_sha256(manifest) != identity:
        raise ReleaseIdentityError("runtime bundle identity does not match its manifest")
    if manifest.get("contract_version") != "mineru-runtime-bundle.v11":
        raise ReleaseIdentityError("runtime bundle is not an explicit-capacity v11 bundle")
    return manifest, identity


def _endpoint_sha256(value: str) -> str:
    return sha256_of(value.rstrip("/").encode("utf-8"))


VALIDATION_RECEIPT_SCHEMA = "mineru_heldout_validation_receipt.v2"
_VALIDATION_RECEIPT_FIELDS = frozenset({
    "schema", "status", "created_at_utc", "policy", "database_access", "queue_access", "document_count",
    "documents", "epoch_before", "epoch_after",
})
_EVIDENCE_WRAPPER_FIELDS = frozenset({"receipt_sha256", "source_bytes_sha256", "receipt"})
_SMOKE_RECEIPT_FIELDS = frozenset({
    "schema", "status", "started_at_utc", "finished_at_utc", "elapsed_seconds", "database_access", "queue_access",
    "input", "identity", "topology", "orchestrator", "runtime_manifest", "canary", "provider", "cleanup",
    "diagnostic_disposal",
})


def load_validation_receipt(path: Path, *, runtime_identity: str) -> tuple[dict[str, Any], str]:
    """Structural gate for a held-out validation receipt before the full verifier runs.

    A self-asserted pass label is not evidence: the receipt must carry the
    complete closed structure the deployment gate consumes (bracketing epochs,
    every document's complete smoke receipt with canary, provider, cleanup and
    disposal evidence, consistent per-document hashes) and every document must
    bind the given runtime identity. The complete semantic verification still
    runs afterwards through the existing deployment-gate verifier.
    """

    raw = _read_private(path, label="deployment qualification receipt")
    receipt = strict_json_loads(raw)
    if type(receipt) is not dict or set(receipt) != _VALIDATION_RECEIPT_FIELDS:
        raise ReleaseIdentityError("deployment qualification receipt fields are not the closed held-out validation set")
    if receipt["schema"] != VALIDATION_RECEIPT_SCHEMA or receipt["status"] != "pass":
        raise ReleaseIdentityError("deployment qualification receipt is not a passing held-out validation receipt")
    if receipt["database_access"] != "none" or receipt["queue_access"] != "none":
        raise ReleaseIdentityError("deployment qualification receipt was not database and queue free")
    documents = receipt["documents"]
    count = receipt["document_count"]
    if type(documents) is not list or type(count) is not int or isinstance(count, bool) or count != len(documents) or count < 2:
        raise ReleaseIdentityError("deployment qualification receipt document count is inconsistent")
    for name in ("epoch_before", "epoch_after"):
        wrapper = receipt[name]
        if type(wrapper) is not dict or set(wrapper) != _EVIDENCE_WRAPPER_FIELDS or type(wrapper["receipt"]) is not dict:
            raise ReleaseIdentityError(f"deployment qualification receipt lacks a complete {name} epoch receipt")
        if sha256_of(canonical_bytes(wrapper["receipt"])) != wrapper["receipt_sha256"]:
            raise ReleaseIdentityError(f"deployment qualification {name} receipt hash does not match its content")
    for entry in documents:
        if type(entry) is not dict or set(entry) != _EVIDENCE_WRAPPER_FIELDS:
            raise ReleaseIdentityError("deployment qualification document wrapper fields are not closed")
        smoke = entry["receipt"]
        if type(smoke) is not dict or set(smoke) != _SMOKE_RECEIPT_FIELDS:
            raise ReleaseIdentityError("deployment qualification document lacks a complete smoke receipt")
        if sha256_of(canonical_bytes(smoke)) != entry["receipt_sha256"]:
            raise ReleaseIdentityError("deployment qualification document hash does not match its receipt")
        identity = smoke["identity"]
        if type(identity) is not dict or identity.get("runtime_manifest_identity_sha256") != runtime_identity:
            raise ReleaseIdentityError("deployment qualification document does not bind the runtime bundle")
        for name in ("input", "canary", "provider", "cleanup", "diagnostic_disposal", "orchestrator"):
            if type(smoke[name]) is not dict:
                raise ReleaseIdentityError(f"deployment qualification document {name} evidence is missing")
    return receipt, sha256_of(canonical_bytes(receipt))


def verify_deployment_qualification(
    *, receipt_path: Path, bundle_wrapper: dict[str, Any], report: VerifyReport, inputs: BindPrivateInputs, now: datetime,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """Run the existing deployment-gate held-out verifier against the bundle and release.

    Returns the receipt, its canonical identity and the orchestrator owners
    recorded by each held-out document (the API epoch that was qualified).
    """

    capacity = report.inputs.capacity
    deployment = report.inputs.deployment_profile
    identity = bundle_wrapper["identity_sha256"]
    receipt, receipt_sha256 = load_validation_receipt(receipt_path, runtime_identity=identity)
    try:
        local_client = client_bundle_identity(inputs.mineru_bin)
        local_code_digest = writer_code_digest()
        manifest = verify_runtime_manifest_payload(
            bundle_wrapper, configured_identity=identity, local_client_identity=local_client,
            local_processing_window_size=capacity.processing_window_size, local_writer_code_digest=local_code_digest,
            expected_capacity=capacity,
        )
    except (OSError, ValueError) as exc:
        raise ReleaseIdentityError(f"runtime bundle cannot be verified against the local client: {exc}") from exc
    if manifest.max_concurrent_requests != capacity.parse_active_limit:
        raise ReleaseIdentityError("runtime bundle task slots differ from the release capacity")
    expected_identity: dict[str, object] = {
        "local_client_identity_sha256": local_client.package_set_sha256,
        "local_content_package_versions": dict(local_client.content_package_versions),
        "local_processing_window_size": capacity.processing_window_size,
        "local_writer_code_sha256": local_code_digest,
        "runtime_manifest_identity_sha256": identity,
        "orchestrator_runtime_identity_sha256": manifest.orchestrator_identity_sha256,
        "provider_runtime_identity_sha256": manifest.provider_identity_sha256,
        "served_model_id": manifest.served_model_id,
        "orchestrator_task_slots": manifest.max_concurrent_requests,
    }
    expected_topology = {
        "api_endpoint_sha256": _endpoint_sha256(inputs.api_url),
        "observability_endpoint_sha256": _endpoint_sha256(inputs.observability_url),
        "inference_upstream_sha256": _endpoint_sha256(inputs.inference_upstream_url),
    }
    topology = manifest.manifest["topology"]
    if any(topology.get(field) != value for field, value in expected_topology.items()):
        raise ReleaseIdentityError("runtime bundle endpoint topology differs from the private inputs")
    try:
        verify_mineru_heldout_validation(
            receipt, expected_identity=expected_identity, expected_topology=expected_topology,
            expected_runtime_manifest=manifest.manifest, runtime_identity=identity,
            task_slots=capacity.parse_active_limit, task_retention_seconds=deployment.api_task_retention_seconds,
            cleanup_interval_seconds=deployment.api_task_cleanup_interval_seconds,
            observability_url=inputs.observability_url, max_age_seconds=inputs.canary_max_age_seconds,
            current=now, expected_capacity=capacity,
        )
    except MinerUDeploymentGateError as exc:
        raise ReleaseIdentityError(f"deployment qualification receipt does not verify: {exc}") from exc
    owners: list[dict[str, Any]] = []
    for entry in receipt["documents"]:
        after = entry.get("receipt", {}).get("orchestrator", {}).get("after", {}) if type(entry) is dict else {}
        owner = after.get("capacity_observation", {}).get("owner") if type(after) is dict else None
        if type(owner) is not dict:
            raise ReleaseIdentityError("deployment qualification receipt does not record the qualified API owner")
        owners.append(owner)
    return receipt, receipt_sha256, owners


def verify_observation(path: Path, *, manifest: dict[str, Any], report: VerifyReport) -> dict[str, Any]:
    """The collector observation must bind the bundle and the exact release build identities."""

    raw = _read_private(path, label="runtime observation")
    observation = strict_json_loads(raw)
    if type(observation) is not dict or observation.get("schema") != "mineru-windows-runtime-observation.v6":
        raise ReleaseIdentityError("runtime observation is not a collector v6 observation")
    topology = manifest["topology"]
    if observation.get("compose_sha256") != topology.get("windows_compose_sha256"):
        raise ReleaseIdentityError("runtime observation compose differs from the runtime bundle")
    if observation.get("windows_node_identity_sha256") != topology.get("windows_node_identity_sha256"):
        raise ReleaseIdentityError("runtime observation host identity differs from the runtime bundle")
    if observation.get("collector_sha256") != report.manifest.installation["collector_sha256"]:
        raise ReleaseIdentityError("runtime observation collector differs from the release collector")
    api = observation.get("api")
    if type(api) is not dict or api.get("image_id") != manifest["orchestrator"].get("container_image_digest"):
        raise ReleaseIdentityError("runtime observation API image differs from the runtime bundle")
    compatibility = observation.get("api_compatibility")
    labels = compatibility.get("image_labels") if type(compatibility) is dict else None
    if type(labels) is not dict:
        raise ReleaseIdentityError("runtime observation lacks the API image labels")
    build = report.manifest.api_build
    expected_labels = {
        "compatibility-patcher-sha256": build["patcher_sha256"],
        "compatibility-dockerfile-sha256": build["dockerfile_sha256"],
        "task-protocol-v2-sha256": build["task_protocol_v2_sha256"],
        "capacity-config-sha256": build["capacity_config_sha256"],
        "capacity-sources-sha256": build["capacity_sources_sha256"],
    }
    for name, expected in expected_labels.items():
        if labels.get(_LABEL_PREFIX + name) != expected:
            raise ReleaseIdentityError(f"deployed API image label {name} differs from the release build identity")
    if manifest["orchestrator"].get("capacity_source_sha256") != build["capacity_source_sha256"]:
        raise ReleaseIdentityError("runtime bundle capacity helper sources differ from the release build")
    return observation


def fetch_json(url: str, *, timeout: float = 10.0) -> tuple[dict[str, Any], bytes]:
    """Read one bounded JSON object from a loopback endpoint; no proxies, no redirects."""

    if urlsplit(url).hostname not in _LOOPBACK_HOSTS:
        raise ReleaseInputError("only loopback endpoints are read")
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with _OPENER.open(request, timeout=timeout) as response:
        raw = response.read(_MAX_HTTP_BYTES + 1)
    if len(raw) > _MAX_HTTP_BYTES:
        raise ReleaseIdentityError(f"response exceeds byte bound: {url}")
    value = strict_json_loads(raw)
    if type(value) is not dict:
        raise ReleaseIdentityError(f"response is not a JSON object: {url}")
    return value, raw


@dataclass(frozen=True, slots=True)
class LiveIdentity:
    owner: dict[str, Any]
    cgroup_identity_sha256: str
    cgroup_max_bytes: int | None
    vm_total_bytes: int
    health_raw: bytes
    pressure_raw: bytes


def sample_live_identity(api_url: str, *, capacity_sha256: str) -> LiveIdentity:
    health, health_raw = fetch_json(api_url + "/health")
    if health.get("status") != "healthy" or health.get("queued_tasks") != 0 or health.get("processing_tasks") != 0:
        raise ReleaseIdentityError("API is not healthy and idle")
    observation = health.get("capacity_observation")
    if type(observation) is not dict or observation.get("capacity_config_sha256") != capacity_sha256:
        raise ReleaseIdentityError("live API capacity identity differs from the release capacity")
    try:
        owner = ProcessPressureOwner.model_validate(observation.get("owner"))
    except ValidationError as exc:
        raise ReleaseIdentityError(f"live API owner is invalid: {exc}") from exc
    pressure_value, pressure_raw = fetch_json(api_url + "/agent/telemetry/pressure/v1")
    try:
        pressure = MineruProcessPressure.model_validate(pressure_value)
    except ValidationError as exc:
        raise ReleaseIdentityError(f"live API pressure sample is invalid: {exc}") from exc
    if pressure.owner != owner or pressure.capacity_config_sha256 != capacity_sha256:
        raise ReleaseIdentityError("live health and pressure samples disagree on owner or capacity")
    return LiveIdentity(
        owner=owner.model_dump(), cgroup_identity_sha256=pressure.memory.cgroup_identity_sha256,
        cgroup_max_bytes=pressure.memory.cgroup_max_bytes, vm_total_bytes=pressure.memory.vm_total_bytes,
        health_raw=health_raw, pressure_raw=pressure_raw,
    )


def assert_structurally_bindable(report: VerifyReport) -> None:
    """Static compatibility with the Mac contracts, checked before any live read or output."""

    capacity = report.inputs.capacity
    local = report.inputs.local_profile
    deployment = report.inputs.deployment_profile
    if local.stream_ceiling > capacity.total_nonterminal_limit:
        raise ReleaseIdentityError("local stream ceiling exceeds the capacity nonterminal depth P")
    if deployment.api_memory_limit_bytes is None:
        raise ReleaseIdentityError("binding requires an explicit API memory limit in the deployment profile")
    if capacity.result_reservation_bytes * capacity.total_nonterminal_limit > capacity.max_unacked_result_bytes:
        raise ReleaseIdentityError(
            "capacity is not bindable on the Mac: the process-profile contract requires B*P <= L "
            "(API build legality is only B <= L); this release cannot be called fully runnable"
        )


def _command_option(command: list[Any], option: str) -> str:
    for index, item in enumerate(command[:-1]):
        if item == option and type(command[index + 1]) is str:
            return command[index + 1]
    raise ReleaseIdentityError(f"inference command lacks {option}")


def build_process_profile(report: VerifyReport, manifest: dict[str, Any], identity: str, live: LiveIdentity) -> MineruProcessProfile:
    capacity = report.inputs.capacity
    deployment = report.inputs.deployment_profile
    local = report.inputs.local_profile
    orchestrator = manifest["orchestrator"]
    inference = manifest["inference_server"]
    if orchestrator.get("capacity_config_sha256") != capacity.sha256:
        raise ReleaseIdentityError("runtime bundle capacity differs from the release capacity")
    if manifest["topology"].get("windows_compose_sha256") != report.manifest.projection["compose_sha256"]:
        raise ReleaseIdentityError("runtime bundle compose differs from the release compose")
    for name, expected in (
        ("task_retention_seconds", deployment.api_task_retention_seconds),
        ("task_cleanup_interval_seconds", deployment.api_task_cleanup_interval_seconds),
    ):
        if orchestrator.get(name) != expected:
            raise ReleaseIdentityError(f"runtime bundle {name} differs from the deployment profile")
    registry_records = orchestrator.get("task_registry_max_records")
    if type(registry_records) is not int or registry_records <= capacity.total_nonterminal_limit:
        raise ReleaseIdentityError("runtime bundle task registry does not exceed the nonterminal depth")
    command = inference.get("command")
    if type(command) is not list:
        raise ReleaseIdentityError("runtime bundle inference command is missing")
    max_num_seqs = int(_command_option(command, "--max-num-seqs"))
    cache_gb = int(_command_option(command, "--mm-processor-cache-gb"))
    if max_num_seqs != deployment.inference_max_num_seqs or cache_gb != deployment.inference_mm_processor_cache_gb:
        raise ReleaseIdentityError("runtime bundle inference command differs from the deployment profile")
    memory_limit = deployment.api_memory_limit_bytes
    if memory_limit is None:
        raise ReleaseIdentityError("binding requires an explicit API memory limit in the deployment profile")
    if live.cgroup_max_bytes is not None and live.cgroup_max_bytes != memory_limit:
        raise ReleaseIdentityError("live API cgroup ceiling differs from the deployment profile memory limit")
    declared = deployment.inference_declared_defaults
    ceilings = local.process_ceilings
    effective_ratio = 1 if ceilings.hybrid_ocr_override else capacity.hybrid_batch_ratio_requested
    return MineruProcessProfile(
        contract_version=EXPLICIT_PROCESS_PROFILE_CONTRACT,
        runtime_bundle_identity_sha256=identity,
        orchestrator_image_identity_sha256=str(orchestrator["container_image_digest"]),
        inference_image_identity_sha256=str(inference["container_image_digest"]),
        model_snapshot_identity_sha256=canonical_payload_sha256(
            {k: inference[k] for k in ("model_repository", "model_snapshot_revision")}
        ),
        host_runtime_identity_sha256=str(manifest["topology"]["windows_node_identity_sha256"]),
        vllm_engine_args_sha256=canonical_payload_sha256(command),
        api_task_slots=capacity.parse_active_limit,
        api_max_pending_tasks=capacity.total_nonterminal_limit,
        registry_nonterminal_cap=capacity.total_nonterminal_limit,
        registry_terminal_cap=registry_records - capacity.total_nonterminal_limit,
        processing_window_size=capacity.processing_window_size,
        raster_stage_slots=ceilings.raster_stage_slots,
        layout_stage_slots=ceilings.layout_stage_slots,
        postprocess_stage_slots=ceilings.postprocess_stage_slots,
        native_owner_slots=ceilings.native_owner_slots,
        cpu_worker_threads=ceilings.cpu_worker_threads,
        omp_thread_count=capacity.omp_num_threads,
        requested_hybrid_batch_ratio=capacity.hybrid_batch_ratio_requested,
        effective_hybrid_batch_ratio=effective_ratio,
        hybrid_ocr_override=ceilings.hybrid_ocr_override,
        inference_concurrency=capacity.final_http_limit_per_loop,
        vllm_max_num_seqs=max_num_seqs,
        vllm_max_model_len=int(inference["max_model_len"]),
        vllm_max_num_batched_tokens=None,
        vllm_gpu_memory_utilization_millionths=declared.vllm_gpu_memory_utilization_millionths,
        vllm_tensor_parallel_size=declared.vllm_tensor_parallel_size,
        vllm_pipeline_parallel_size=declared.vllm_pipeline_parallel_size,
        vllm_mm_processor_cache_bytes=cache_gb * (1 << 30),
        vllm_enforce_eager=declared.vllm_enforce_eager,
        vllm_enable_prefix_caching=declared.vllm_enable_prefix_caching,
        pipeline_inference_locks=capacity.pipeline_inference_locks,
        finalizer_slots=capacity.finalizer_active_limit,
        result_reservation_bytes=capacity.result_reservation_bytes,
        max_unacked_result_bytes=capacity.max_unacked_result_bytes,
        source_pdf_bytes_limit=ceilings.source_pdf_bytes_limit,
        resident_pages_limit=ceilings.resident_pages_limit,
        rasterized_page_bytes_limit=ceilings.rasterized_page_bytes_limit,
        decoded_payload_bytes_limit=ceilings.decoded_payload_bytes_limit,
        cpu_working_set_bytes_limit=memory_limit,
        gpu_allocated_bytes_limit=ceilings.gpu_allocated_bytes_limit,
        gpu_request_slots=capacity.final_http_limit_per_loop,
        reorder_buffer_bytes_limit=ceilings.reorder_buffer_bytes_limit,
        terminal_output_bytes_limit=ceilings.terminal_output_bytes_limit,
        temporary_disk_bytes_limit=ceilings.temporary_disk_bytes_limit,
        db_staged_bytes_limit=ceilings.db_staged_bytes_limit,
        unpublished_pages_limit=ceilings.unpublished_pages_limit,
        container_memory_limit_bytes=memory_limit,
        host_runtime_memory_limit_bytes=live.vm_total_bytes,
        task_retention_seconds=deployment.api_task_retention_seconds,
        task_cleanup_interval_seconds=deployment.api_task_cleanup_interval_seconds,
    )


def build_activation(report: VerifyReport, identity: str, live: LiveIdentity, gpu_uuid: str) -> dict[str, Any]:
    local = report.inputs.local_profile
    capacity = report.inputs.capacity
    owner_json = canonical_bytes(live.owner)
    policy = {
        "qualified_max": local.stream_ceiling,
        "runtime_identity_sha256": identity,
        "owner_identity_sha256": sha256_of(owner_json),
        **{name: getattr(local.stream_policy, name) for name in (
            "gpu_pause_bytes", "gpu_reduce_bytes", "gpu_recover_bytes", "host_pause_bytes", "host_recover_bytes",
            "sample_max_age_seconds", "missing_pause_seconds", "recovery_seconds", "reduction_interval_seconds",
        )},
    }
    return {
        "schema": "mineru.stream-activation.v1",
        "runtime_identity_sha256": identity,
        "capacity_config_sha256": capacity.sha256,
        "owner": live.owner,
        "cgroup_identity_sha256": live.cgroup_identity_sha256,
        "cgroup_max_bytes": live.cgroup_max_bytes,
        "gpu_uuid": gpu_uuid,
        "api_max_age_seconds": local.stream_source_ages.api_max_age_seconds,
        "gpu_max_age_seconds": local.stream_source_ages.gpu_max_age_seconds,
        "policy": policy,
    }


def overlay_lines(values: dict[str, str]) -> bytes:
    lines = []
    for key, value in values.items():
        if _ENV_KEY_RE.fullmatch(key) is None or _ENV_VALUE_RE.fullmatch(value) is None:
            raise ReleaseIdentityError(f"overlay value for {key} is not shell-safe without quoting")
        lines.append(f"{key}={value}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def merge_base_env(base: bytes, overlay: dict[str, str]) -> bytes:
    """Replace overlay keys in a base env file verbatim otherwise; values are never echoed."""

    seen: set[str] = set()
    lines: list[str] = []
    for line in base.decode("utf-8").splitlines():
        stripped = line.lstrip()
        key = None
        if stripped and not stripped.startswith("#") and "=" in stripped:
            candidate = stripped.split("=", 1)[0].strip()
            if candidate.startswith("export "):
                candidate = candidate[len("export "):].strip()
            key = candidate
        if key in overlay:
            lines.append(f"{key}={overlay[key]}")
            seen.add(key)
        else:
            lines.append(line)
    for key, value in overlay.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class BindResult:
    output: Path
    binding: dict[str, Any]


def bind_release(
    *, report: VerifyReport, runtime_bundle: Path, observation: Path, deployment_qualification: Path,
    private_inputs: Path, output: Path, base_env: Path | None = None, now: datetime | None = None,
) -> BindResult:
    if not output.is_absolute() or output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ReleaseInputError("bind output must be a new absolute directory under an existing parent")
    if not report.passed:
        raise ReleaseIdentityError("release package did not verify; refusing to bind")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    # 1. Static compatibility and every identity check, before any live read.
    assert_structurally_bindable(report)
    inputs = load_bind_private_inputs(private_inputs)
    if inputs.validation_receipt_path.resolve() != deployment_qualification.resolve():
        raise ReleaseIdentityError("private inputs validation receipt differs from the qualification argument")
    bundle_wrapper = strict_json_loads(_read_private(runtime_bundle, label="runtime bundle"))
    manifest, identity = load_runtime_bundle(runtime_bundle)
    assert type(bundle_wrapper) is dict
    verify_observation(observation, manifest=manifest, report=report)
    receipt, qualification_sha256, qualified_owners = verify_deployment_qualification(
        receipt_path=deployment_qualification, bundle_wrapper=bundle_wrapper, report=report, inputs=inputs, now=current,
    )
    base_raw = None
    if base_env is not None:
        base_raw = _read_private(base_env, label="base worker env", maximum=1 << 20)
        expected_uuid = None
        for line in base_raw.decode("utf-8").splitlines():
            if line.startswith("DISCLOSURE_GPU_EXPECTED_UUID="):
                expected_uuid = line.split("=", 1)[1].strip().strip("'\"")
        if expected_uuid is not None and expected_uuid != inputs.gpu_uuid:
            raise ReleaseIdentityError("private inputs gpu_uuid differs from the base env GPU identity")
    capacity = report.inputs.capacity
    # 2. Live loopback samples: owner and cgroup are facts of the running API.
    live = sample_live_identity(inputs.api_url, capacity_sha256=capacity.sha256)
    if any(owner != live.owner for owner in qualified_owners):
        raise ReleaseIdentityError(
            "live API owner differs from the owner the deployment qualification recorded; "
            "a new API epoch requires a new qualification, not a rebinding"
        )
    profile = build_process_profile(report, manifest, identity, live)
    activation = build_activation(report, identity, live, inputs.gpu_uuid)
    activation_raw = canonical_bytes(activation)
    activation_sha256 = sha256_of(activation_raw)
    # 3. Outputs, then reload every artifact through the product loaders.
    output.mkdir(mode=0o700)
    capacity_path = output / "capacity-config.json"
    profile_path = output / "process-profile.json"
    activation_path = output / "activation.json"
    write_new_exact(capacity_path, report.inputs.capacity_bytes)
    write_new_exact(profile_path, profile.exact_bytes)
    write_new_exact(activation_path, activation_raw)
    write_new_exact(output / "health-observation.json", live.health_raw)
    write_new_exact(output / "pressure-observation.json", live.pressure_raw)
    uid = os.getuid()
    loaded_profile = load_mineru_process_profile(profile_path, expected_sha256=profile.sha256, expected_owner_uid=uid)
    loaded_activation = load_mineru_stream_activation(
        activation_path, expected_sha256=activation_sha256, expected_owner_uid=uid,
        expected_capacity=capacity, expected_runtime_identity_sha256=identity,
    )
    if loaded_profile.sha256 != profile.sha256 or loaded_activation is None:
        raise ReleaseIdentityError("bound artifacts did not reload through the product loaders")
    local = report.inputs.local_profile
    overlay = {
        "WORKER_PARSE_EXECUTION_MODE": "staged-v4",
        "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256": identity,
        "DISCLOSURE_MINERU_CAPACITY_CONFIG": str(capacity_path),
        "DISCLOSURE_MINERU_CAPACITY_CONFIG_SHA256": capacity.sha256,
        "DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG": str(activation_path),
        "DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG_SHA256": activation_sha256,
        "DISCLOSURE_MINERU_SMOKE_RECEIPT": str(inputs.smoke_receipt_path),
        "DISCLOSURE_MINERU_VALIDATION_RECEIPT": str(inputs.validation_receipt_path),
        "DISCLOSURE_MINERU_CANARY_CACHE": str(inputs.canary_cache_path),
        "DISCLOSURE_MINERU_API_TASK_SLOTS": str(capacity.parse_active_limit),
        "DISCLOSURE_MINERU_API_INFERENCE_CONCURRENCY": str(capacity.final_http_limit_per_loop),
        "WORKER_GPU_REQUEST_BUDGET": str(capacity.final_http_limit_per_loop),
        "MINERU_PROCESSING_WINDOW_SIZE": str(capacity.processing_window_size),
        "WORKER_PARSE_CONCURRENCY": str(local.mac_preflight_workers),
        "WORKER_FINALIZE_CONCURRENCY": str(local.mac_finalize_workers),
        "DISCLOSURE_V4_PROCESS_PROFILE_FILE": str(profile_path),
        "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": profile.sha256,
        "DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT": str(local.archive_member_count_limit),
        "DISCLOSURE_V4_COMMIT_STAGE_SECONDS": str(local.commit_stage_seconds),
        "DISCLOSURE_V4_PROVIDER_POLL_MILLISECONDS": str(local.provider_poll_milliseconds),
        "DISCLOSURE_V4_ADMISSION_PROBE_MILLISECONDS": str(local.admission_probe_milliseconds),
    }
    overlay_raw = overlay_lines(overlay)
    write_new_exact(output / "worker-overlay.env", overlay_raw)
    merged_sha256 = None
    if base_raw is not None:
        merged = merge_base_env(base_raw, overlay)
        write_new_exact(output / "worker.env", merged)
        merged_sha256 = sha256_of(merged)
    binding = {
        "contract_version": BINDING_CONTRACT,
        "release_manifest_sha256": report.manifest.sha256,
        "source_head": report.manifest.source["head"],
        "runtime_bundle_identity_sha256": identity,
        "deployment_qualification_sha256": qualification_sha256,
        "deployment_qualification_document_count": receipt.get("document_count"),
        "capacity_config_sha256": capacity.sha256,
        "process_profile_sha256": profile.sha256,
        "activation_sha256": activation_sha256,
        "stream_ceiling": local.stream_ceiling,
        "owner": live.owner,
        "cgroup_identity_sha256": live.cgroup_identity_sha256,
        "cgroup_max_bytes": live.cgroup_max_bytes,
        "gpu_uuid": inputs.gpu_uuid,
        "worker_overlay_sha256": sha256_of(overlay_raw),
        "worker_env_sha256": merged_sha256,
        "bound_at_utc": current.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "paths": {
            "capacity_config": str(capacity_path), "process_profile": str(profile_path),
            "activation": str(activation_path), "worker_overlay": str(output / "worker-overlay.env"),
        },
    }
    write_new_json(output / "binding.json", binding)
    return BindResult(output=output, binding=binding)


__all__ = [
    "BINDING_CONTRACT",
    "PRIVATE_INPUTS_CONTRACT",
    "BindPrivateInputs",
    "BindResult",
    "LiveIdentity",
    "assert_structurally_bindable",
    "bind_release",
    "build_activation",
    "build_process_profile",
    "fetch_json",
    "load_bind_private_inputs",
    "load_runtime_bundle",
    "load_validation_receipt",
    "loopback_endpoint",
    "merge_base_env",
    "overlay_lines",
    "sample_live_identity",
    "verify_deployment_qualification",
    "verify_observation",
]
