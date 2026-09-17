"""Mac-side worker composition values and declared process ceilings.

These are requested local settings and ceilings that the binding step turns
into the attested process profile, the stream activation and the private
worker overlay. ``stream_ceiling`` is a selected candidate, never a claim that
the value is qualified.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from disclosure_anchor.application.contracts.closed_document import (
    canonical_bytes,
    load_closed_object,
    require_bool,
    require_fields,
    require_int,
    require_number,
    sha256_of,
)


LOCAL_WORKER_PROFILE_CONTRACT = "mineru.local-worker-profile.v1"
_MAX_BYTES = 16 * 1024
_FIELDS = frozenset({
    "contract_version",
    "mac_preflight_workers",
    "mac_finalize_workers",
    "provider_poll_milliseconds",
    "admission_probe_milliseconds",
    "commit_stage_seconds",
    "archive_member_count_limit",
    "stream_ceiling",
    "stream_source_ages",
    "stream_policy",
    "process_ceilings",
})
_AGE_FIELDS = frozenset({"api_max_age_seconds", "gpu_max_age_seconds"})
_POLICY_BYTE_FIELDS = ("gpu_pause_bytes", "gpu_reduce_bytes", "gpu_recover_bytes", "host_pause_bytes", "host_recover_bytes")
_POLICY_TIME_FIELDS = ("sample_max_age_seconds", "missing_pause_seconds", "recovery_seconds", "reduction_interval_seconds")
_POLICY_FIELDS = frozenset(_POLICY_BYTE_FIELDS + _POLICY_TIME_FIELDS)
_CEILING_BYTE_FIELDS = (
    "source_pdf_bytes_limit", "rasterized_page_bytes_limit", "decoded_payload_bytes_limit",
    "reorder_buffer_bytes_limit", "terminal_output_bytes_limit", "temporary_disk_bytes_limit",
    "db_staged_bytes_limit", "gpu_allocated_bytes_limit",
)
_CEILING_COUNT_FIELDS = (
    "resident_pages_limit", "unpublished_pages_limit", "cpu_worker_threads", "raster_stage_slots",
    "layout_stage_slots", "postprocess_stage_slots", "native_owner_slots",
)
_CEILING_FIELDS = frozenset(_CEILING_BYTE_FIELDS + _CEILING_COUNT_FIELDS + ("hybrid_ocr_override",))


@dataclass(frozen=True, slots=True)
class StreamSourceAges:
    api_max_age_seconds: float
    gpu_max_age_seconds: float

    def __post_init__(self) -> None:
        for name in _AGE_FIELDS:
            require_number(getattr(self, name), label=name)


@dataclass(frozen=True, slots=True)
class StreamPolicyValues:
    gpu_pause_bytes: int
    gpu_reduce_bytes: int
    gpu_recover_bytes: int
    host_pause_bytes: int
    host_recover_bytes: int
    sample_max_age_seconds: float
    missing_pause_seconds: float
    recovery_seconds: float
    reduction_interval_seconds: float

    def __post_init__(self) -> None:
        for name in _POLICY_BYTE_FIELDS:
            require_int(getattr(self, name), label=name)
        for name in _POLICY_TIME_FIELDS:
            require_number(getattr(self, name), label=name)
        if not self.gpu_pause_bytes < self.gpu_reduce_bytes < self.gpu_recover_bytes:
            raise ValueError("stream policy GPU hysteresis must be pause < reduce < recover")
        if self.host_pause_bytes >= self.host_recover_bytes:
            raise ValueError("stream policy host hysteresis must be pause < recover")


@dataclass(frozen=True, slots=True)
class ProcessCeilings:
    source_pdf_bytes_limit: int
    rasterized_page_bytes_limit: int
    decoded_payload_bytes_limit: int
    reorder_buffer_bytes_limit: int
    terminal_output_bytes_limit: int
    temporary_disk_bytes_limit: int
    db_staged_bytes_limit: int
    gpu_allocated_bytes_limit: int
    resident_pages_limit: int
    unpublished_pages_limit: int
    cpu_worker_threads: int
    raster_stage_slots: int
    layout_stage_slots: int
    postprocess_stage_slots: int
    native_owner_slots: int
    hybrid_ocr_override: bool

    def __post_init__(self) -> None:
        for name in _CEILING_BYTE_FIELDS:
            require_int(getattr(self, name), label=name)
        for name in _CEILING_COUNT_FIELDS:
            require_int(getattr(self, name), label=name, maximum=(1 << 31) - 1)
        require_bool(self.hybrid_ocr_override, label="hybrid_ocr_override")


@dataclass(frozen=True, slots=True)
class MineruLocalWorkerProfile:
    contract_version: str
    mac_preflight_workers: int
    mac_finalize_workers: int
    provider_poll_milliseconds: int
    admission_probe_milliseconds: int
    commit_stage_seconds: int
    archive_member_count_limit: int
    stream_ceiling: int
    stream_source_ages: StreamSourceAges
    stream_policy: StreamPolicyValues
    process_ceilings: ProcessCeilings

    def __post_init__(self) -> None:
        if self.contract_version != LOCAL_WORKER_PROFILE_CONTRACT:
            raise ValueError("MinerU local worker profile contract is unsupported")
        require_int(self.mac_preflight_workers, label="mac_preflight_workers", maximum=16)
        require_int(self.mac_finalize_workers, label="mac_finalize_workers", maximum=64)
        require_int(self.provider_poll_milliseconds, label="provider_poll_milliseconds", maximum=60_000)
        require_int(self.admission_probe_milliseconds, label="admission_probe_milliseconds", maximum=60_000)
        require_int(self.commit_stage_seconds, label="commit_stage_seconds", minimum=60, maximum=86_400)
        require_int(self.archive_member_count_limit, label="archive_member_count_limit", maximum=100_000)
        require_int(self.stream_ceiling, label="stream_ceiling", maximum=128)
        for name, kind in (("stream_source_ages", StreamSourceAges), ("stream_policy", StreamPolicyValues),
                           ("process_ceilings", ProcessCeilings)):
            value = getattr(self, name)
            if type(value) is not kind:
                raise ValueError(f"local worker profile {name} must use the exact type")
            value.__post_init__()
        if self.stream_policy.sample_max_age_seconds < max(
            self.stream_source_ages.api_max_age_seconds, self.stream_source_ages.gpu_max_age_seconds,
        ):
            raise ValueError("stream policy maximum age is below a source maximum age")

    @property
    def exact_bytes(self) -> bytes:
        self.__post_init__()
        return canonical_bytes(asdict(self))

    @property
    def sha256(self) -> str:
        return sha256_of(self.exact_bytes)


def decode_mineru_local_worker_profile(payload: bytes) -> MineruLocalWorkerProfile:
    value = load_closed_object(payload, label="MinerU local worker profile", maximum_bytes=_MAX_BYTES)
    require_fields(value, _FIELDS, label="MinerU local worker profile")
    fields: dict[str, Any] = dict(value)
    for name, kind, names in (
        ("stream_source_ages", StreamSourceAges, _AGE_FIELDS),
        ("stream_policy", StreamPolicyValues, _POLICY_FIELDS),
        ("process_ceilings", ProcessCeilings, _CEILING_FIELDS),
    ):
        nested = value[name]
        if type(nested) is not dict:
            raise ValueError(f"local worker profile {name} must be an object")
        require_fields(nested, names, label=f"local worker profile {name}")
        fields[name] = kind(**nested)
    return MineruLocalWorkerProfile(**fields)


__all__ = [
    "LOCAL_WORKER_PROFILE_CONTRACT",
    "MineruLocalWorkerProfile",
    "ProcessCeilings",
    "StreamPolicyValues",
    "StreamSourceAges",
    "decode_mineru_local_worker_profile",
]
