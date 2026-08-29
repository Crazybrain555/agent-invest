"""Closed wire contract for the default-off Windows resident telemetry exporter."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from disclosure_anchor.application.contracts.synchronized_telemetry import (
    ApiProcessObservation,
    GpuObservation,
    HostCgroupObservation,
    QueueVllmObservation,
    parse_canonical_json_artifact,
)


WIRE_VERSION: Literal["mineru.windows-resident-telemetry.v1"] = (
    "mineru.windows-resident-telemetry.v1"
)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ResidentIdentity(_Closed):
    exporter_source_sha256: str
    host_assignment_identity_sha256: str
    boot_identity_sha256: str
    runtime_bundle_identity_sha256: str
    process_profile_sha256: str
    clock_domain_identity_sha256: str
    exporter_process_epoch_sha256: str

    @model_validator(mode="after")
    def _canonical_hashes(self) -> "ResidentIdentity":
        for name, value in self:
            digest = value.removeprefix("sha256:")
            if (
                not value.startswith("sha256:")
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{name} is not a canonical SHA-256")
        return self


class WindowsGpuResidentSample(_Closed):
    contract_version: Literal["mineru.windows-resident-telemetry.v1"] = WIRE_VERSION
    lane: Literal["gpu_fast"]
    sequence: int = Field(ge=1)
    observed_at_utc: datetime
    sampled_monotonic_ns: int = Field(ge=0)
    identity: ResidentIdentity
    gpu: GpuObservation

    @model_validator(mode="after")
    def _clock(self) -> "WindowsGpuResidentSample":
        if self.observed_at_utc.tzinfo is None or self.observed_at_utc.utcoffset() != timezone.utc.utcoffset(self.observed_at_utc):
            raise ValueError("observed_at_utc must be UTC")
        return self


class WindowsHostResidentSample(_Closed):
    contract_version: Literal["mineru.windows-resident-telemetry.v1"] = WIRE_VERSION
    lane: Literal["host_slow"]
    sequence: int = Field(ge=1)
    observed_at_utc: datetime
    sampled_monotonic_ns: int = Field(ge=0)
    identity: ResidentIdentity
    api_process: ApiProcessObservation
    host_cgroup: HostCgroupObservation
    queue_vllm: QueueVllmObservation

    @model_validator(mode="after")
    def _clock(self) -> "WindowsHostResidentSample":
        if self.observed_at_utc.tzinfo is None or self.observed_at_utc.utcoffset() != timezone.utc.utcoffset(self.observed_at_utc):
            raise ValueError("observed_at_utc must be UTC")
        return self


def decode_windows_resident_sample(
    payload: bytes,
    *,
    lane: Literal["gpu_fast", "host_slow"],
    maximum_bytes: int = 64 * 1024,
) -> WindowsGpuResidentSample | WindowsHostResidentSample:
    """Decode exact canonical bytes; duplicate/nonfinite/noncanonical input is rejected."""

    value = parse_canonical_json_artifact(
        payload,
        label="Windows resident telemetry sample",
        maximum_bytes=maximum_bytes,
    )
    if not isinstance(value, dict):
        raise ValueError("Windows resident telemetry sample must be an object")
    model = WindowsGpuResidentSample if lane == "gpu_fast" else WindowsHostResidentSample
    sample = model.model_validate(value)
    return sample


__all__ = [
    "ResidentIdentity",
    "WindowsGpuResidentSample",
    "WindowsHostResidentSample",
    "decode_windows_resident_sample",
]
