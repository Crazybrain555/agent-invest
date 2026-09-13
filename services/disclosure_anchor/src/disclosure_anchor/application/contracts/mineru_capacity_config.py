"""Explicit startup capacity for one MinerU API process and event loop.

This module also ships, byte for byte, as a standalone MinerU module. Keep it
stdlib-only. The bounds describe supported configuration, not measured hardware
capacity or a throughput recommendation. No runtime observations belong here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any


CAPACITY_CONFIG_CONTRACT = "mineru.capacity-config.v1"
_MAX_BYTES = 64 * 1024
_MAX_INT64 = (1 << 63) - 1
_FIELDS = frozenset(
    {
        "contract_version",
        "parse_active_limit",
        "total_nonterminal_limit",
        "finalizer_active_limit",
        "final_http_limit_per_loop",
        "api_process_limit",
        "api_event_loop_limit",
        "processing_window_size",
        "omp_num_threads",
        "mkl_num_threads",
        "openblas_num_threads",
        "pdf_render_processes_requested",
        "hybrid_batch_ratio_requested",
        "pipeline_inference_locks",
        "result_reservation_bytes",
        "max_unacked_result_bytes",
    }
)


@dataclass(frozen=True, slots=True)
class MineruCapacityConfig:
    """Immutable requested limits; changing them requires a new process epoch.

    P includes all accepted nonterminal tasks, including those waiting for result
    capacity. N and F limit physically active parse and finalizer work. H is the
    shared final HTTP limit for the serving loop, not a per-document allowance.
    """

    contract_version: str
    parse_active_limit: int
    total_nonterminal_limit: int
    finalizer_active_limit: int
    final_http_limit_per_loop: int
    api_process_limit: int
    api_event_loop_limit: int
    processing_window_size: int
    omp_num_threads: int
    mkl_num_threads: int
    openblas_num_threads: int
    pdf_render_processes_requested: int
    hybrid_batch_ratio_requested: int
    pipeline_inference_locks: bool
    result_reservation_bytes: int
    max_unacked_result_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.contract_version) is not str
            or self.contract_version != CAPACITY_CONFIG_CONTRACT
        ):
            raise ValueError("MinerU capacity config contract is unsupported")
        for name in (
            "parse_active_limit",
            "total_nonterminal_limit",
            "finalizer_active_limit",
            "final_http_limit_per_loop",
        ):
            _positive_int(name, getattr(self, name), 128)
        for name in ("api_process_limit", "api_event_loop_limit"):
            _positive_int(name, getattr(self, name), 1)
        _positive_int("processing_window_size", self.processing_window_size, 1024)
        for name in (
            "omp_num_threads",
            "mkl_num_threads",
            "openblas_num_threads",
            "pdf_render_processes_requested",
        ):
            _positive_int(name, getattr(self, name), 256)
        if (
            type(self.hybrid_batch_ratio_requested) is not int
            or self.hybrid_batch_ratio_requested not in (1, 2, 4, 8)
        ):
            raise ValueError("MinerU requested hybrid batch ratio is unsupported")
        if self.pipeline_inference_locks is not True:
            raise ValueError("MinerU capacity config requires original inference locks")
        for name in ("result_reservation_bytes", "max_unacked_result_bytes"):
            _positive_int(name, getattr(self, name), _MAX_INT64)
        if self.parse_active_limit > self.total_nonterminal_limit:
            raise ValueError("MinerU parse limit exceeds total nonterminal limit")
        if self.finalizer_active_limit > self.total_nonterminal_limit:
            raise ValueError("MinerU finalizer limit exceeds total nonterminal limit")
        if self.result_reservation_bytes > self.max_unacked_result_bytes:
            raise ValueError("MinerU single result reservation exceeds result capacity")

    @property
    def exact_bytes(self) -> bytes:
        return encode_mineru_capacity_config(self)

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.exact_bytes).hexdigest()


def encode_mineru_capacity_config(config: MineruCapacityConfig) -> bytes:
    """Encode the exact validated type without defaults or observed values."""

    if type(config) is not MineruCapacityConfig:
        raise ValueError("MinerU capacity config must use the exact contract type")
    # Revalidate at the byte boundary, including objects reconstructed by callers.
    config.__post_init__()
    return json.dumps(
        asdict(config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def decode_mineru_capacity_config(payload: bytes) -> MineruCapacityConfig:
    """Accept only closed, canonical UTF-8 JSON bytes within the finite envelope."""

    if type(payload) is not bytes or not payload or len(payload) > _MAX_BYTES:
        raise ValueError("MinerU capacity config bytes are outside the closed envelope")
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("MinerU capacity config is not strict UTF-8 JSON") from exc
    if type(decoded) is not dict or set(decoded) != _FIELDS:
        raise ValueError("MinerU capacity config fields are not closed")
    config = MineruCapacityConfig(**decoded)
    if config.exact_bytes != payload:
        raise ValueError("MinerU capacity config bytes are not canonical")
    return config


def _positive_int(name: str, value: object, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"MinerU capacity config {name} must be within 1..{maximum}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("MinerU capacity config has a duplicate field")
        value[name] = item
    return value


def _reject_constant(value: str) -> Any:
    raise ValueError(f"MinerU capacity config has a non-finite value: {value}")


__all__ = [
    "CAPACITY_CONFIG_CONTRACT",
    "MineruCapacityConfig",
    "decode_mineru_capacity_config",
    "encode_mineru_capacity_config",
]
