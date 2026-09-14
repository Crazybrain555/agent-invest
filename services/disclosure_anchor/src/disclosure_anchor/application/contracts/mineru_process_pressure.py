"""Private, read-only API pressure wire; container/VM readings are not reservations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from disclosure_anchor.application.contracts.strict_json import strict_json_loads


_Hash = Annotated[str, Field(min_length=71, max_length=71, pattern=r"^sha256:[a-f0-9]{64}$")]
_Count = Annotated[int, Field(ge=0, le=2**63 - 1)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProcessPressureOwner(_Closed):
    process_id: Annotated[int, Field(ge=1)]
    process_start_ticks: Annotated[int, Field(ge=1)]
    boot_id: str
    loop_epoch: str

    @model_validator(mode="after")
    def canonical_owner(self) -> Self:
        if any(str(UUID(value)) != value for value in (self.boot_id, self.loop_epoch)):
            raise ValueError("pressure owner UUID is not canonical")
        return self


class ProcessPressureMemory(_Closed):
    scope: Literal["self_cgroup_and_vm"]
    ancestor_visibility: Literal["not_observed"]
    cgroup_identity_sha256: _Hash
    cgroup_current_bytes: _Count
    cgroup_max_bytes: Annotated[int, Field(gt=0)] | None
    memory_events: dict[str, _Count]
    vm_total_bytes: Annotated[int, Field(gt=0)]
    vm_available_bytes: _Count

    @model_validator(mode="after")
    def complete_memory(self) -> Self:
        if not {"low", "high", "max", "oom", "oom_kill"} <= self.memory_events.keys():
            raise ValueError("pressure memory events are incomplete")
        if len(self.memory_events) > 32 or any(not key or len(key) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyz_" for c in key) for key in self.memory_events):
            raise ValueError("pressure memory event names are invalid")
        if self.vm_available_bytes > self.vm_total_bytes:
            raise ValueError("pressure VM available memory exceeds total")
        return self

    @property
    def observed_headroom_bytes(self) -> int:
        """Local cgroup/VM lower reading; hidden ancestors remain unobserved."""
        if self.cgroup_max_bytes is None:
            return self.vm_available_bytes
        return min(self.vm_available_bytes, max(0, self.cgroup_max_bytes - self.cgroup_current_bytes))


class ProcessPressureClock(_Closed):
    clock: Literal["python.monotonic_ns"]
    started_ns: _Count
    completed_ns: _Count

    @model_validator(mode="after")
    def forward(self) -> Self:
        if self.completed_ns < self.started_ns:
            raise ValueError("pressure clock bracket regressed")
        return self


class MineruProcessPressure(_Closed):
    schema_version: Literal["mineru.process-pressure.v1"] = Field(alias="schema")
    capacity_config_sha256: _Hash
    owner: ProcessPressureOwner
    memory: ProcessPressureMemory
    observed_at: ProcessPressureClock


def parse_mineru_process_pressure(
    payload: bytes, *, expected_capacity_sha256: str,
    expected_owner: Mapping[str, object], expected_cgroup_identity_sha256: str,
    expected_cgroup_max_bytes: int | None,
) -> MineruProcessPressure:
    if not payload or len(payload) > 65536:
        raise ValueError("pressure response byte bound exceeded")
    result = MineruProcessPressure.model_validate(strict_json_loads(payload))
    if (result.capacity_config_sha256 != expected_capacity_sha256
            or result.owner.model_dump() != dict(expected_owner)
            or result.memory.cgroup_identity_sha256 != expected_cgroup_identity_sha256
            or result.memory.cgroup_max_bytes != expected_cgroup_max_bytes):
        raise ValueError("pressure response differs from the qualified API/cgroup identity")
    return result
