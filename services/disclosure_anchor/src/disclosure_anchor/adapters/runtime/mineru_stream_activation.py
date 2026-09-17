"""Explicit, file-pinned activation of the existing stream pressure policy.

This file is configuration authority, not hardware qualification evidence. In
particular, binding a self-cgroup ceiling does not observe or qualify its parent
cgroups. The caller must separately retain that qualification and supply the
independently selected capacity and runtime identity. Loading starts no readers.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
from pathlib import Path
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from disclosure_anchor.adapters.runtime.mineru_capacity_file import read_mineru_capacity_file
from disclosure_anchor.adapters.runtime.mineru_stream_pressure import PressureBinding
from disclosure_anchor.application.contracts.mineru_capacity_config import MineruCapacityConfig
from disclosure_anchor.application.contracts.mineru_process_pressure import ProcessPressureOwner
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.services.mineru_stream_policy import StreamPolicyConfig


STREAM_ACTIVATION_CONTRACT = "mineru.stream-activation.v1"
_HASH_PATTERN = r"sha256:[a-f0-9]{64}"
_Hash = Annotated[str, Field(min_length=71, max_length=71, pattern="^" + _HASH_PATTERN + "$")]
_Bytes = Annotated[int, Field(gt=0, le=2**63 - 1)]
_Seconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
_GPUUUID = Annotated[str, Field(min_length=40, max_length=40, pattern=r"^GPU-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Policy(_Closed):
    # Deliberately no defaults: every existing policy setting is file authority.
    qualified_max: Annotated[int, Field(ge=1, le=128)]
    runtime_identity_sha256: _Hash
    owner_identity_sha256: _Hash
    gpu_pause_bytes: _Bytes
    gpu_reduce_bytes: _Bytes
    gpu_recover_bytes: _Bytes
    host_pause_bytes: _Bytes
    host_recover_bytes: _Bytes
    sample_max_age_seconds: _Seconds
    missing_pause_seconds: _Seconds
    recovery_seconds: _Seconds
    reduction_interval_seconds: _Seconds


class _Activation(_Closed):
    schema_version: Literal["mineru.stream-activation.v1"] = Field(alias="schema")
    runtime_identity_sha256: _Hash
    capacity_config_sha256: _Hash
    owner: ProcessPressureOwner
    cgroup_identity_sha256: _Hash
    cgroup_max_bytes: _Bytes | None
    gpu_uuid: _GPUUUID
    api_max_age_seconds: _Seconds
    gpu_max_age_seconds: _Seconds
    policy: _Policy


@dataclass(frozen=True, slots=True)
class LoadedMineruStreamActivation:
    """Validated configuration, with the hash of the exact securely read file."""

    binding: PressureBinding
    policy: StreamPolicyConfig
    source_path: Path
    source_sha256: str


def load_mineru_stream_activation(
    path: Path | None,
    *,
    expected_sha256: str | None,
    expected_owner_uid: int,
    expected_capacity: MineruCapacityConfig | None,
    expected_runtime_identity_sha256: str | None,
) -> LoadedMineruStreamActivation | None:
    """Load an explicit absolute-path/hash pair; an absent pair stays disabled.

    The closed JSON object requires ``schema``, ``runtime_identity_sha256``,
    ``capacity_config_sha256``, ``owner``, ``cgroup_identity_sha256``,
    ``cgroup_max_bytes`` (explicit null permits an unlimited self cgroup),
    ``gpu_uuid``, both ``api_max_age_seconds`` and ``gpu_max_age_seconds``, and
    ``policy``. The policy object must contain every StreamPolicyConfig field,
    including matching runtime and canonical-owner hashes. Owner is the closed
    ProcessPressureOwner object; its sorted, compact JSON bytes form the binding.

    Both independent expectations are required when enabled. This function does
    not obtain live owner/cgroup observations or prove a qualified maximum; it
    only checks that the declared maximum fits the selected startup capacity's
    nonterminal depth P (the remote-wait domain), not the parse slots N.
    File security, parsing and identity errors propagate, never disable silently.
    """
    if path is None and expected_sha256 is None:
        return None
    if path is None or expected_sha256 is None:
        raise ValueError("stream activation requires paired absolute path and SHA-256")
    if type(expected_capacity) is not MineruCapacityConfig:
        raise ValueError("stream activation requires explicit capacity authority")
    expected_capacity.__post_init__()
    if (
        type(expected_runtime_identity_sha256) is not str
        or re.fullmatch(_HASH_PATTERN, expected_runtime_identity_sha256) is None
    ):
        raise ValueError("stream activation requires explicit runtime identity")
    payload = read_mineru_capacity_file(
        path,
        expected_sha256=expected_sha256,
        expected_owner_uid=expected_owner_uid,
    )
    try:
        document = _Activation.model_validate(strict_json_loads(payload))
    except (ValueError, RecursionError) as error:
        raise ValueError("stream activation is not closed strict UTF-8 JSON") from error
    if document.capacity_config_sha256 != expected_capacity.sha256:
        raise ValueError("stream activation capacity differs from selected authority")
    if document.runtime_identity_sha256 != expected_runtime_identity_sha256:
        raise ValueError("stream activation runtime differs from selected identity")
    # Future policy additions must explicitly enter this versioned schema, not
    # inherit an implementation default through dataclass construction.
    if set(_Policy.model_fields) != {field.name for field in fields(StreamPolicyConfig)}:
        raise ValueError("stream activation schema does not cover the complete policy")
    binding = PressureBinding(
        runtime_identity_sha256=document.runtime_identity_sha256,
        capacity=expected_capacity,
        owner_json=json.dumps(document.owner.model_dump(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"),
        cgroup_identity_sha256=document.cgroup_identity_sha256,
        cgroup_max_bytes=document.cgroup_max_bytes,
        gpu_uuid=document.gpu_uuid,
        api_max_age_seconds=document.api_max_age_seconds,
        gpu_max_age_seconds=document.gpu_max_age_seconds,
    )
    policy = StreamPolicyConfig(**document.policy.model_dump())
    if (policy.runtime_identity_sha256, policy.owner_identity_sha256) != (
        binding.runtime_identity_sha256, binding.owner_sha256,
    ):
        raise ValueError("stream policy differs from runtime/owner binding")
    if policy.sample_max_age_seconds < max(binding.api_max_age_seconds, binding.gpu_max_age_seconds):
        raise ValueError("stream policy maximum age is below a source maximum age")
    # The ceiling bounds the worker's simultaneous remote waits (pre-POST,
    # remote parse/finalize and terminal polling all hold one), whose structural
    # domain is the accepted nonterminal depth P, not the parse slots N. A value
    # above N does not add parse capacity; the API still admits at most N.
    # The declared value stays a selected ceiling, never proof of qualification.
    if policy.qualified_max > expected_capacity.total_nonterminal_limit:
        raise ValueError("qualified stream maximum exceeds selected nonterminal capacity")
    return LoadedMineruStreamActivation(
        binding=binding, policy=policy, source_path=path,
        source_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
    )


__all__ = [
    "STREAM_ACTIVATION_CONTRACT",
    "LoadedMineruStreamActivation",
    "load_mineru_stream_activation",
]
