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
    require_str,
    sha256_of,
)


DEPLOYMENT_PROFILE_CONTRACT = "mineru.deployment-profile.v1"
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

    def __post_init__(self) -> None:
        if self.contract_version != DEPLOYMENT_PROFILE_CONTRACT:
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

    @property
    def api_device_mode(self) -> str:
        return "cuda:0" if self.api_device_profile == "cuda0" else "cpu"

    @property
    def exact_bytes(self) -> bytes:
        self.__post_init__()
        return canonical_bytes(asdict(self))

    @property
    def sha256(self) -> str:
        return sha256_of(self.exact_bytes)


def decode_mineru_deployment_profile(payload: bytes) -> MineruDeploymentProfile:
    """Decode any strict JSON layout of the closed document; identity is canonical."""

    value = load_closed_object(payload, label="MinerU deployment profile", maximum_bytes=_MAX_BYTES)
    require_fields(value, _FIELDS, label="MinerU deployment profile")
    declared = value["inference_declared_defaults"]
    if type(declared) is not dict:
        raise ValueError("deployment profile declared engine defaults must be an object")
    require_fields(declared, _DECLARED_FIELDS, label="deployment profile declared engine defaults")
    fields: dict[str, Any] = dict(value)
    fields["inference_declared_defaults"] = InferenceDeclaredDefaults(**declared)
    return MineruDeploymentProfile(**fields)


__all__ = [
    "DEPLOYMENT_PROFILE_CONTRACT",
    "InferenceDeclaredDefaults",
    "MineruDeploymentProfile",
    "decode_mineru_deployment_profile",
]
