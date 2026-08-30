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
        "api_task_slots",
        "api_max_pending_tasks",
        "processing_window_size",
        "requested_hybrid_batch_ratio",
        "effective_hybrid_batch_ratio",
        "hybrid_ocr_override",
        "inference_concurrency",
        "vllm_max_num_seqs",
        "vllm_max_model_len",
        "pipeline_inference_locks",
        "finalizer_slots",
        "result_reservation_bytes",
        "max_unacked_result_bytes",
        "task_retention_seconds",
        "task_cleanup_interval_seconds",
    }
)
_HYBRID_BATCH_RATIOS = frozenset({1, 2, 4, 8})
_MAX_PROFILE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class MineruProcessProfile:
    """Hard ceilings frozen for the lifetime of one attested process.

    These values are identity, not Auto recommendations.  A fast controller may
    schedule below the ceilings, but changing a field requires a quiescent new
    process epoch and a new profile hash.
    """

    contract_version: str
    api_task_slots: int
    api_max_pending_tasks: int
    processing_window_size: int
    requested_hybrid_batch_ratio: int
    effective_hybrid_batch_ratio: int
    hybrid_ocr_override: bool
    inference_concurrency: int
    vllm_max_num_seqs: int
    vllm_max_model_len: int
    pipeline_inference_locks: bool
    finalizer_slots: int
    result_reservation_bytes: int
    max_unacked_result_bytes: int
    task_retention_seconds: int
    task_cleanup_interval_seconds: int

    def __post_init__(self) -> None:
        if self.contract_version != PROCESS_PROFILE_CONTRACT:
            raise ValueError("MinerU process profile contract is unsupported")
        for name in (
            "api_task_slots",
            "api_max_pending_tasks",
            "processing_window_size",
            "inference_concurrency",
            "vllm_max_num_seqs",
            "vllm_max_model_len",
            "finalizer_slots",
            "result_reservation_bytes",
            "max_unacked_result_bytes",
            "task_retention_seconds",
            "task_cleanup_interval_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"MinerU process profile {name} must be positive")
        for name in (
            "requested_hybrid_batch_ratio",
            "effective_hybrid_batch_ratio",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value not in _HYBRID_BATCH_RATIOS:
                raise ValueError(f"MinerU process profile {name} is unsupported")
        if type(self.hybrid_ocr_override) is not bool:
            raise ValueError("MinerU process profile OCR override must be boolean")
        if type(self.pipeline_inference_locks) is not bool:
            raise ValueError("MinerU process profile lock policy must be boolean")
        if self.api_task_slots > self.api_max_pending_tasks:
            raise ValueError("MinerU task slots exceed pending-task admission")
        if self.inference_concurrency > self.vllm_max_num_seqs:
            raise ValueError("MinerU inference concurrency exceeds vLLM sequences")
        if self.finalizer_slots > self.api_max_pending_tasks:
            raise ValueError("MinerU finalizer slots exceed pending-task admission")
        if self.result_reservation_bytes > self.max_unacked_result_bytes:
            raise ValueError("MinerU result reservation exceeds retained-result budget")
        if self.task_cleanup_interval_seconds > self.task_retention_seconds:
            raise ValueError("MinerU cleanup cadence exceeds task retention")
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


__all__ = [
    "MineruProcessProfile",
    "PROCESS_PROFILE_CONTRACT",
    "decode_mineru_process_profile",
    "encode_mineru_process_profile",
]
