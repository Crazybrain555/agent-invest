"""Deployment topology inputs that vary between MinerU Windows deployments.

Everything not listed here (entrypoints, container names, health checks,
networks, the proxy program, fixed API environment) is the release plan's
template. Values are requested topology, not observed runtime facts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Literal

from disclosure_anchor.application.contracts.closed_document import (
    canonical_bytes,
    load_closed_object,
    require_bool,
    require_fields,
    require_int,
    require_sha256,
    require_str,
    sha256_of,
)


DEPLOYMENT_PROFILE_CONTRACT = "mineru.deployment-profile.v1"
DIAGNOSTIC_DEPLOYMENT_PROFILE_CONTRACT = "mineru.deployment-profile.v2"
OWNER_MEMORY_WORKER_CLASS = "mineru_inference_memory.worker.OwnerMemoryWorker"
OWNER_MEMORY_MODE = "mineru.owner-memory.v1"
OWNER_MEMORY_BASE = "mineru@sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458"
OWNER_MEMORY_PREIMAGES = {
    "vllm/v1/worker/worker_base.py": "sha256:e6017acf80e442405f09f99413c0227515ae097d3e1dc79005b0b5c266932f12",
    "vllm/v1/worker/gpu_worker.py": "sha256:53196cef1d32b5c5207c8986a5666352fd5ba0b79c418c074311147f266cadc4",
    "vllm/v1/worker/gpu_model_runner.py": "sha256:95b7ee3ff5eecdb0fa791f375b2aeedc6ac507f3ea4a9683700d3b202b2caf08",
    "vllm/v1/worker/gpu/mm/encoder_runner.py": "sha256:909181912d39bf4734d5202340a77813252136bc1f38c458f879b05743a522bf",
}
_MAX_BYTES = 16 * 1024
_FIELDS = frozenset({
    "contract_version",
    "project_name",
    "base_image_repo_digest",
    "api_image_reference",
    "api_device_profile",
    "api_memory_limit_bytes",
    "api_phase_trace",
    "api_output_root_windows",
    "api_task_retention_seconds",
    "api_task_cleanup_interval_seconds",
    "api_published_port",
    "vllm_observability_published_port",
    "inference_max_num_seqs",
    "inference_mm_processor_cache_gb",
    "inference_declared_defaults",
})
_DECLARED_FIELDS = frozenset({
    "vllm_gpu_memory_utilization_millionths",
    "vllm_tensor_parallel_size",
    "vllm_pipeline_parallel_size",
    "vllm_enforce_eager",
    "vllm_enable_prefix_caching",
})
_REPO_DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
_IMAGE_REFERENCE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+$")
_PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_WINDOWS_ROOT_RE = re.compile(r"^[A-Za-z]:/(?:[^/\\:*?\"<>|\r\n]+/)*[^/\\:*?\"<>|\r\n]+$")


@dataclass(frozen=True, slots=True)
class InferenceDeclaredDefaults:
    """Engine settings not observable from the runtime bundle; declared input."""

    vllm_gpu_memory_utilization_millionths: int
    vllm_tensor_parallel_size: int
    vllm_pipeline_parallel_size: int
    vllm_enforce_eager: bool
    vllm_enable_prefix_caching: bool

    def __post_init__(self) -> None:
        require_int(self.vllm_gpu_memory_utilization_millionths,
                    label="vllm_gpu_memory_utilization_millionths", maximum=1_000_000)
        require_int(self.vllm_tensor_parallel_size, label="vllm_tensor_parallel_size", maximum=64)
        require_int(self.vllm_pipeline_parallel_size, label="vllm_pipeline_parallel_size", maximum=64)
        require_bool(self.vllm_enforce_eager, label="vllm_enforce_eager")
        require_bool(self.vllm_enable_prefix_caching, label="vllm_enable_prefix_caching")


@dataclass(frozen=True, slots=True)
class InferenceDiagnostic:
    """One reviewed diagnostic context; its content-derived tag is never an image-ID proof."""

    contract_version: str
    context_sha256: str
    worker_class: str

    def __post_init__(self) -> None:
        if self.contract_version != OWNER_MEMORY_MODE or self.worker_class != OWNER_MEMORY_WORKER_CLASS:
            raise ValueError("inference diagnostic selection is unsupported")
        require_sha256(self.context_sha256, label="inference diagnostic context sha256")

    @property
    def image_reference(self) -> str:
        return "agent-invest/mineru-inference:owner-memory-" + self.context_sha256[7:]


@dataclass(frozen=True, slots=True)
class MineruDeploymentProfile:
    contract_version: str
    project_name: str
    base_image_repo_digest: str
    api_image_reference: str
    api_device_profile: Literal["cpu", "cuda0"]
    api_memory_limit_bytes: int | None
    api_phase_trace: bool
    api_output_root_windows: str
    api_task_retention_seconds: int
    api_task_cleanup_interval_seconds: int
    api_published_port: int
    vllm_observability_published_port: int
    inference_max_num_seqs: int
    inference_mm_processor_cache_gb: int
    inference_declared_defaults: InferenceDeclaredDefaults
    inference_diagnostic: InferenceDiagnostic | None = None

    def __post_init__(self) -> None:
        if self.contract_version == DEPLOYMENT_PROFILE_CONTRACT:
            if self.inference_diagnostic is not None:
                raise ValueError("raw v1 deployment profile cannot select a diagnostic")
        elif self.contract_version == DIAGNOSTIC_DEPLOYMENT_PROFILE_CONTRACT:
            if type(self.inference_diagnostic) is not InferenceDiagnostic:
                raise ValueError("v2 deployment profile requires the exact diagnostic type")
            self.inference_diagnostic.__post_init__()
        else:
            raise ValueError("MinerU deployment profile contract is unsupported")
        if _PROJECT_NAME_RE.fullmatch(require_str(self.project_name, label="project_name", maximum=64)) is None:
            raise ValueError("deployment profile project_name is invalid")
        if _REPO_DIGEST_RE.fullmatch(require_str(self.base_image_repo_digest, label="base_image_repo_digest")) is None:
            raise ValueError("deployment profile base image must be a repo@sha256 digest")
        if _IMAGE_REFERENCE_RE.fullmatch(require_str(self.api_image_reference, label="api_image_reference")) is None:
            raise ValueError("deployment profile API image reference must be name:tag")
        if self.api_device_profile not in ("cpu", "cuda0"):
            raise ValueError("deployment profile API device profile must be cpu or cuda0")
        if self.api_memory_limit_bytes is not None:
            require_int(self.api_memory_limit_bytes, label="api_memory_limit_bytes", minimum=1 << 30)
        require_bool(self.api_phase_trace, label="api_phase_trace")
        if _WINDOWS_ROOT_RE.fullmatch(require_str(self.api_output_root_windows, label="api_output_root_windows")) is None:
            raise ValueError("deployment profile output root must be an absolute Windows path with forward slashes")
        require_int(self.api_task_retention_seconds, label="api_task_retention_seconds", maximum=86_400)
        require_int(self.api_task_cleanup_interval_seconds, label="api_task_cleanup_interval_seconds", maximum=86_400)
        if self.api_task_cleanup_interval_seconds > self.api_task_retention_seconds:
            raise ValueError("deployment profile cleanup cadence exceeds task retention")
        for name in ("api_published_port", "vllm_observability_published_port"):
            require_int(getattr(self, name), label=name, minimum=1024, maximum=65535)
        if self.api_published_port == self.vllm_observability_published_port:
            raise ValueError("deployment profile published ports must differ")
        require_int(self.inference_max_num_seqs, label="inference_max_num_seqs", maximum=65536)
        require_int(self.inference_mm_processor_cache_gb, label="inference_mm_processor_cache_gb", minimum=0, maximum=1024)
        if type(self.inference_declared_defaults) is not InferenceDeclaredDefaults:
            raise ValueError("deployment profile declared engine defaults must use the exact type")
        self.inference_declared_defaults.__post_init__()
        if self.inference_diagnostic is not None and (
            self.base_image_repo_digest != OWNER_MEMORY_BASE
            or self.inference_max_num_seqs != 128 or self.inference_mm_processor_cache_gb != 0
            or self.inference_declared_defaults != InferenceDeclaredDefaults(500_000, 1, 1, False, True)
        ):
            raise ValueError("owner-memory diagnostic must preserve the observed base and engine settings")

    @property
    def api_device_mode(self) -> str:
        return "cuda:0" if self.api_device_profile == "cuda0" else "cpu"

    @property
    def exact_bytes(self) -> bytes:
        self.__post_init__()
        value = asdict(self)
        if self.contract_version == DEPLOYMENT_PROFILE_CONTRACT:
            del value["inference_diagnostic"]
        return canonical_bytes(value)

    @property
    def sha256(self) -> str:
        return sha256_of(self.exact_bytes)


def decode_mineru_deployment_profile(payload: bytes) -> MineruDeploymentProfile:
    """Decode any strict JSON layout of the closed document; identity is canonical."""

    value = load_closed_object(payload, label="MinerU deployment profile", maximum_bytes=_MAX_BYTES)
    expected = _FIELDS | {"inference_diagnostic"} if value.get("contract_version") == DIAGNOSTIC_DEPLOYMENT_PROFILE_CONTRACT else _FIELDS
    require_fields(value, expected, label="MinerU deployment profile")
    declared = value["inference_declared_defaults"]
    if type(declared) is not dict:
        raise ValueError("deployment profile declared engine defaults must be an object")
    require_fields(declared, _DECLARED_FIELDS, label="deployment profile declared engine defaults")
    fields: dict[str, Any] = dict(value)
    fields["inference_declared_defaults"] = InferenceDeclaredDefaults(**declared)
    if "inference_diagnostic" in fields:
        diagnostic = fields["inference_diagnostic"]
        if type(diagnostic) is not dict:
            raise ValueError("inference diagnostic must be an object")
        require_fields(diagnostic, {"contract_version", "context_sha256", "worker_class"}, label="inference diagnostic")
        fields["inference_diagnostic"] = InferenceDiagnostic(**diagnostic)
    return MineruDeploymentProfile(**fields)


__all__ = [
    "DEPLOYMENT_PROFILE_CONTRACT",
    "DIAGNOSTIC_DEPLOYMENT_PROFILE_CONTRACT",
    "InferenceDiagnostic",
    "OWNER_MEMORY_WORKER_CLASS",
    "OWNER_MEMORY_PREIMAGES",
    "verify_inference_diagnostic",
    "inference_diagnostic_config",
    "InferenceDeclaredDefaults",
    "MineruDeploymentProfile",
    "decode_mineru_deployment_profile",
]

def verify_inference_diagnostic(observation: dict[str, Any], profile: Any, sources: dict[str, str]) -> dict[str, Any]:
    if type(profile) is not MineruDeploymentProfile:
        raise ValueError("diagnostic deployment profile must use the exact type")
    profile.__post_init__()
    require_fields(sources, {"Dockerfile", "mineru_inference_memory/__init__.py", "mineru_inference_memory/core.py", "mineru_inference_memory/worker.py"}, label="diagnostic context sources")
    for name, digest in sources.items():
        require_sha256(digest, label=name)
    diagnostic = profile.inference_diagnostic
    if diagnostic is None or sha256_of(canonical_bytes(sources)) != diagnostic.context_sha256:
        raise ValueError("diagnostic context differs from deployment input")
    value = observation.get("inference_diagnostic")
    if type(value) is not dict or set(value) != {"contract_version", "image_labels", "module_sha256", "actual_source_sha256", "actual_diagnostic_source_sha256", "loaded_workers", "container_epoch"}:
        raise ValueError("remote diagnostic source observation is not closed")
    expected_labels = {
        "io.agent-invest.mineru.owner-memory-module-sha256": sources["mineru_inference_memory/worker.py"],
        "io.agent-invest.mineru.owner-memory-dockerfile-sha256": sources["Dockerfile"],
        "io.agent-invest.mineru.owner-memory-base-repo-digest": OWNER_MEMORY_BASE,
        "io.agent-invest.mineru.owner-memory-context-sha256": diagnostic.context_sha256,
    }
    if (value["contract_version"] != "mineru.owner-memory-observation.v1"
            or value["image_labels"] != expected_labels
            or value["module_sha256"] != sources["mineru_inference_memory/worker.py"]
            or value["actual_source_sha256"] != OWNER_MEMORY_PREIMAGES
            or value["actual_diagnostic_source_sha256"] != {name: digest for name, digest in sources.items() if name != "Dockerfile"}):
        raise ValueError("remote diagnostic image/module/source preimages drifted")
    # Current container/process facts prove loading, but never enter stable R.
    inference = observation.get("inference")
    epoch = value["container_epoch"]
    if (type(inference) is not dict or type(epoch) is not dict
            or set(epoch) != {"container_id", "started_at"}
            or re.fullmatch(r"[0-9a-f]{64}", str(epoch.get("container_id"))) is None
            or type(epoch.get("started_at")) is not str
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", epoch["started_at"]) is None
            or epoch != {key: inference.get(key) for key in ("container_id", "started_at")}):
        raise ValueError("remote diagnostic container epoch is not the observed current epoch")
    workers = value["loaded_workers"]
    if type(workers) is not list or len(workers) != 1:
        raise ValueError("remote diagnostic worker loading was not observed")
    worker = workers[0]
    fields = {"contract_version", "module_sha256", "worker_class", "pid", "proc_start_ticks", "device",
              "instance_id", "observed_proc_start_ticks", "namespace_init_start_ticks"}
    if (type(worker) is not dict or set(worker) != fields
            or worker["contract_version"] != "mineru.owner-memory-identity.v1"
            or worker["module_sha256"] != value["module_sha256"]
            or worker["worker_class"] != diagnostic.worker_class or worker["device"] != "cuda:0"
            or type(worker["pid"]) is not int or worker["pid"] <= 0
            or type(worker["proc_start_ticks"]) is not int or worker["proc_start_ticks"] <= 0
            or type(worker["observed_proc_start_ticks"]) is not int
            or worker["proc_start_ticks"] != worker["observed_proc_start_ticks"]
            or type(worker["namespace_init_start_ticks"]) is not int or worker["namespace_init_start_ticks"] <= 0
            or worker["namespace_init_start_ticks"] > worker["proc_start_ticks"]
            or type(worker["instance_id"]) is not str or not 1 <= len(worker["instance_id"]) <= 128):
        raise ValueError("remote diagnostic loaded worker identity drifted")
    return dict(value)



def inference_diagnostic_config(proof: dict[str, Any], profile: MineruDeploymentProfile) -> dict[str, Any]:
    """Stable source/config identity, excluding process and epoch observations."""

    if profile.inference_diagnostic is None:
        raise ValueError("diagnostic configuration requires a diagnostic profile")
    return {
        "contract_version": proof["contract_version"],
        "image_labels": proof["image_labels"],
        "module_sha256": proof["module_sha256"],
        "actual_source_sha256": proof["actual_source_sha256"],
        "actual_diagnostic_source_sha256": proof["actual_diagnostic_source_sha256"],
        "worker_class": profile.inference_diagnostic.worker_class,
    }
