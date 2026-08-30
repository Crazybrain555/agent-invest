"""Canonical startup-only capacity identity for one MinerU process epoch."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any

from disclosure_anchor.application.contracts.strict_json import strict_json_loads


PROCESS_PROFILE_CONTRACT = "mineru.process-profile.v1"
_FIELDS = frozenset(
    {
        "contract_version",
        "runtime_bundle_identity_sha256",
        "orchestrator_image_identity_sha256",
        "inference_image_identity_sha256",
        "model_snapshot_identity_sha256",
        "host_runtime_identity_sha256",
        "vllm_engine_args_sha256",
        "api_task_slots",
        "api_max_pending_tasks",
        "registry_nonterminal_cap",
        "registry_terminal_cap",
        "processing_window_size",
        "raster_stage_slots",
        "layout_stage_slots",
        "postprocess_stage_slots",
        "native_owner_slots",
        "cpu_worker_threads",
        "omp_thread_count",
        "requested_hybrid_batch_ratio",
        "effective_hybrid_batch_ratio",
        "hybrid_ocr_override",
        "inference_concurrency",
        "vllm_max_num_seqs",
        "vllm_max_model_len",
        "vllm_max_num_batched_tokens",
        "vllm_gpu_memory_utilization_millionths",
        "vllm_tensor_parallel_size",
        "vllm_pipeline_parallel_size",
        "vllm_mm_processor_cache_bytes",
        "vllm_enforce_eager",
        "vllm_enable_prefix_caching",
        "pipeline_inference_locks",
        "finalizer_slots",
        "result_reservation_bytes",
        "max_unacked_result_bytes",
        "source_pdf_bytes_limit",
        "resident_pages_limit",
        "rasterized_page_bytes_limit",
        "decoded_payload_bytes_limit",
        "cpu_working_set_bytes_limit",
        "gpu_allocated_bytes_limit",
        "gpu_request_slots",
        "reorder_buffer_bytes_limit",
        "terminal_output_bytes_limit",
        "temporary_disk_bytes_limit",
        "db_staged_bytes_limit",
        "unpublished_pages_limit",
        "container_memory_limit_bytes",
        "host_runtime_memory_limit_bytes",
        "task_retention_seconds",
        "task_cleanup_interval_seconds",
    }
)
_HYBRID_BATCH_RATIOS = frozenset({1, 2, 4, 8})
_MAX_PROFILE_BYTES = 64 * 1024
_MAX_INT32 = (1 << 31) - 1
_MAX_INT64 = (1 << 63) - 1
_SHA256_PREFIX = "sha256:"


@dataclass(frozen=True, slots=True)
class MineruProcessProfile:
    """Hard ceilings frozen for the lifetime of one attested process.

    These values are identity, not Auto recommendations.  A fast controller may
    schedule below the ceilings, but changing a field requires a quiescent new
    process epoch and a new profile hash.
    """

    contract_version: str
    runtime_bundle_identity_sha256: str
    orchestrator_image_identity_sha256: str
    inference_image_identity_sha256: str
    model_snapshot_identity_sha256: str
    host_runtime_identity_sha256: str
    vllm_engine_args_sha256: str
    api_task_slots: int
    api_max_pending_tasks: int
    registry_nonterminal_cap: int
    registry_terminal_cap: int
    processing_window_size: int
    raster_stage_slots: int
    layout_stage_slots: int
    postprocess_stage_slots: int
    native_owner_slots: int
    cpu_worker_threads: int
    omp_thread_count: int
    requested_hybrid_batch_ratio: int
    effective_hybrid_batch_ratio: int
    hybrid_ocr_override: bool
    inference_concurrency: int
    vllm_max_num_seqs: int
    vllm_max_model_len: int
    vllm_max_num_batched_tokens: int
    vllm_gpu_memory_utilization_millionths: int
    vllm_tensor_parallel_size: int
    vllm_pipeline_parallel_size: int
    vllm_mm_processor_cache_bytes: int
    vllm_enforce_eager: bool
    vllm_enable_prefix_caching: bool
    pipeline_inference_locks: bool
    finalizer_slots: int
    result_reservation_bytes: int
    max_unacked_result_bytes: int
    source_pdf_bytes_limit: int
    resident_pages_limit: int
    rasterized_page_bytes_limit: int
    decoded_payload_bytes_limit: int
    cpu_working_set_bytes_limit: int
    gpu_allocated_bytes_limit: int
    gpu_request_slots: int
    reorder_buffer_bytes_limit: int
    terminal_output_bytes_limit: int
    temporary_disk_bytes_limit: int
    db_staged_bytes_limit: int
    unpublished_pages_limit: int
    container_memory_limit_bytes: int
    host_runtime_memory_limit_bytes: int
    task_retention_seconds: int
    task_cleanup_interval_seconds: int

    def __post_init__(self) -> None:
        if self.contract_version != PROCESS_PROFILE_CONTRACT:
            raise ValueError("MinerU process profile contract is unsupported")
        for name in (
            "runtime_bundle_identity_sha256",
            "orchestrator_image_identity_sha256",
            "inference_image_identity_sha256",
            "model_snapshot_identity_sha256",
            "host_runtime_identity_sha256",
            "vllm_engine_args_sha256",
        ):
            value = getattr(self, name)
            if not _is_sha256(value):
                raise ValueError(f"MinerU process profile {name} is not canonical sha256")
        for name in (
            "api_task_slots",
            "api_max_pending_tasks",
            "registry_nonterminal_cap",
            "registry_terminal_cap",
            "processing_window_size",
            "raster_stage_slots",
            "layout_stage_slots",
            "postprocess_stage_slots",
            "native_owner_slots",
            "cpu_worker_threads",
            "omp_thread_count",
            "inference_concurrency",
            "vllm_max_num_seqs",
            "vllm_max_model_len",
            "vllm_max_num_batched_tokens",
            "vllm_gpu_memory_utilization_millionths",
            "vllm_tensor_parallel_size",
            "vllm_pipeline_parallel_size",
            "gpu_request_slots",
            "resident_pages_limit",
            "unpublished_pages_limit",
            "finalizer_slots",
            "task_retention_seconds",
            "task_cleanup_interval_seconds",
        ):
            _validate_positive_bounded_int(name, getattr(self, name), _MAX_INT32)
        for name in (
            "result_reservation_bytes",
            "max_unacked_result_bytes",
            "source_pdf_bytes_limit",
            "rasterized_page_bytes_limit",
            "decoded_payload_bytes_limit",
            "cpu_working_set_bytes_limit",
            "gpu_allocated_bytes_limit",
            "reorder_buffer_bytes_limit",
            "terminal_output_bytes_limit",
            "temporary_disk_bytes_limit",
            "db_staged_bytes_limit",
            "container_memory_limit_bytes",
            "host_runtime_memory_limit_bytes",
        ):
            _validate_positive_bounded_int(name, getattr(self, name), _MAX_INT64)
        _validate_nonnegative_bounded_int(
            "vllm_mm_processor_cache_bytes",
            self.vllm_mm_processor_cache_bytes,
            _MAX_INT64,
        )
        for name in (
            "requested_hybrid_batch_ratio",
            "effective_hybrid_batch_ratio",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value not in _HYBRID_BATCH_RATIOS:
                raise ValueError(f"MinerU process profile {name} is unsupported")
        if type(self.hybrid_ocr_override) is not bool:
            raise ValueError("MinerU process profile OCR override must be boolean")
        for name in (
            "vllm_enforce_eager",
            "vllm_enable_prefix_caching",
            "pipeline_inference_locks",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"MinerU process profile {name} must be boolean")
        if self.api_task_slots > self.api_max_pending_tasks:
            raise ValueError("MinerU task slots exceed pending-task admission")
        if self.api_max_pending_tasks > self.registry_nonterminal_cap:
            raise ValueError("MinerU pending-task admission exceeds nonterminal registry")
        for name in (
            "raster_stage_slots",
            "layout_stage_slots",
            "postprocess_stage_slots",
            "native_owner_slots",
        ):
            if getattr(self, name) > self.registry_nonterminal_cap:
                raise ValueError(f"MinerU {name} exceeds nonterminal registry")
        if self.inference_concurrency > self.vllm_max_num_seqs:
            raise ValueError("MinerU inference concurrency exceeds vLLM sequences")
        if self.gpu_request_slots > self.vllm_max_num_seqs:
            raise ValueError("MinerU GPU request slots exceed vLLM sequences")
        if self.finalizer_slots > self.api_max_pending_tasks:
            raise ValueError("MinerU finalizer slots exceed pending-task admission")
        if (
            self.result_reservation_bytes * self.api_max_pending_tasks
            > self.max_unacked_result_bytes
        ):
            raise ValueError("MinerU admitted result reservations exceed retained-result budget")
        if self.task_cleanup_interval_seconds > self.task_retention_seconds:
            raise ValueError("MinerU cleanup cadence exceeds task retention")
        if self.vllm_gpu_memory_utilization_millionths > 1_000_000:
            raise ValueError("MinerU vLLM GPU utilization exceeds one millionth scale")
        if self.container_memory_limit_bytes > self.host_runtime_memory_limit_bytes:
            raise ValueError("MinerU container memory exceeds host runtime memory")
        if self.cpu_working_set_bytes_limit > self.container_memory_limit_bytes:
            raise ValueError("MinerU CPU working set exceeds container memory identity")
        if self.hybrid_ocr_override:
            if self.effective_hybrid_batch_ratio != 1:
                raise ValueError("MinerU OCR override requires effective batch ratio 1")
        elif self.effective_hybrid_batch_ratio != self.requested_hybrid_batch_ratio:
            raise ValueError("MinerU effective batch ratio drifted without OCR override")

    @property
    def exact_bytes(self) -> bytes:
        return _canonical_bytes(self)

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.exact_bytes).hexdigest()


def encode_mineru_process_profile(profile: MineruProcessProfile) -> bytes:
    """Return the sole canonical JSON representation of ``profile``."""

    return profile.exact_bytes


def decode_mineru_process_profile(payload: bytes) -> MineruProcessProfile:
    """Decode exact canonical bytes; noncanonical aliases fail closed."""

    if type(payload) is not bytes or not payload or len(payload) > _MAX_PROFILE_BYTES:
        raise ValueError("MinerU process profile bytes are outside the closed envelope")
    try:
        decoded = strict_json_loads(payload)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("MinerU process profile is not strict UTF-8 JSON") from exc
    if not isinstance(decoded, dict) or set(decoded) != _FIELDS:
        raise ValueError("MinerU process profile fields are not closed")
    profile = MineruProcessProfile(**decoded)
    if profile.exact_bytes != payload:
        raise ValueError("MinerU process profile bytes are not canonical")
    return profile


def _canonical_bytes(profile: MineruProcessProfile) -> bytes:
    payload: dict[str, Any] = asdict(profile)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_positive_bounded_int(name: str, value: object, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(
            f"MinerU process profile {name} must be within 1..{maximum}"
        )


def _validate_nonnegative_bounded_int(name: str, value: object, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(
            f"MinerU process profile {name} must be within 0..{maximum}"
        )


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith(_SHA256_PREFIX):
        return False
    digest = value[len(_SHA256_PREFIX) :]
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


__all__ = [
    "MineruProcessProfile",
    "PROCESS_PROFILE_CONTRACT",
    "decode_mineru_process_profile",
    "encode_mineru_process_profile",
]
