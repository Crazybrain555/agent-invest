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
from disclosure_anchor.application.contracts.mineru_capacity_config import MineruCapacityConfig, AnyMineruCapacityConfig, MineruCapacityConfigV2
from disclosure_anchor.application.contracts.mineru_process_pressure import ProcessPressureOwner
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.services.mineru_stream_policy import (
    STREAM_POLICY_ALGORITHM_V1,
    STREAM_POLICY_ALGORITHM_V2,
    StreamPolicyConfig,
)


STREAM_ACTIVATION_CONTRACT = "mineru.stream-activation.v1"
STREAM_ACTIVATION_CONTRACT_V2 = "mineru.stream-activation.v2"
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


class _PolicyV2(_Policy):
    # v2 names the executable algorithm explicitly; the v1 schema predates it
    # and always meant the superseded qualified-start algorithm. The name is
    # activation identity only; the policy settings stay the same fields.
    algorithm: Literal["mineru.stream-policy.v2"]


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


class _ActivationV2(_Closed):
    schema_version: Literal["mineru.stream-activation.v2"] = Field(alias="schema")
    runtime_identity_sha256: _Hash
    capacity_config_sha256: _Hash
    owner: ProcessPressureOwner
    cgroup_identity_sha256: _Hash
    cgroup_max_bytes: _Bytes | None
    gpu_uuid: _GPUUUID
    api_max_age_seconds: _Seconds
    gpu_max_age_seconds: _Seconds
    policy: _PolicyV2


@dataclass(frozen=True, slots=True)
class LoadedMineruStreamActivation:
    """Validated configuration, with the hash of the exact securely read file."""

    binding: PressureBinding
    policy: StreamPolicyConfig
    source_path: Path
    source_sha256: str
    # The algorithm the activation names. The loader always sets it from the
    # file schema: v1 files predate the field and name the superseded
    # qualified-start algorithm; they stay decodable, never executable.
    algorithm: str = STREAM_POLICY_ALGORITHM_V2


def load_mineru_stream_activation(
    path: Path | None,
    *,
    expected_sha256: str | None,
    expected_owner_uid: int,
    expected_capacity: AnyMineruCapacityConfig | None,
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

    ``mineru.stream-activation.v2`` adds ``policy.algorithm`` =
    ``mineru.stream-policy.v2``. A v1 file still decodes (old releases stay
    auditable) into a config naming ``mineru.stream-policy.v1``; the policy
    refuses to execute that superseded algorithm.
    """
    if path is None and expected_sha256 is None:
        return None
    if path is None or expected_sha256 is None:
        raise ValueError("stream activation requires paired absolute path and SHA-256")
    if expected_capacity is None or type(expected_capacity) not in (
        MineruCapacityConfig, MineruCapacityConfigV2,
    ):
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
        decoded = strict_json_loads(payload)
        v2 = isinstance(decoded, dict) and decoded.get("schema") == STREAM_ACTIVATION_CONTRACT_V2
        document: _Activation | _ActivationV2 = (
            _ActivationV2.model_validate(decoded) if v2 else _Activation.model_validate(decoded)
        )
    except (ValueError, RecursionError) as error:
        raise ValueError("stream activation is not closed strict UTF-8 JSON") from error
    if document.capacity_config_sha256 != expected_capacity.sha256:
        raise ValueError("stream activation capacity differs from selected authority")
    if document.runtime_identity_sha256 != expected_runtime_identity_sha256:
        raise ValueError("stream activation runtime differs from selected identity")
    # Future policy additions must explicitly enter this versioned schema, not
    # inherit an implementation default through dataclass construction.
    policy_fields = {field.name for field in fields(StreamPolicyConfig)}
    if (
        set(_Policy.model_fields) != policy_fields
        or set(_PolicyV2.model_fields) != policy_fields | {"algorithm"}
    ):
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
    settings = document.policy.model_dump()
    algorithm = settings.pop("algorithm", STREAM_POLICY_ALGORITHM_V1)
    if isinstance(document, _ActivationV2) != (algorithm == STREAM_POLICY_ALGORITHM_V2):
        raise ValueError("stream activation schema and policy algorithm disagree")
    policy = StreamPolicyConfig(**settings)
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
        algorithm=algorithm,
    )


def require_executable_stream_activation(loaded: LoadedMineruStreamActivation) -> None:
    """Refuse to run a policy under an activation that qualified another algorithm.

    A v1 activation was qualified with the superseded qualified-start rule.
    Running the v2 algorithm under its identity would let a changed algorithm
    pose as the old qualification, so execution needs an explicit v2 file.
    """

    if type(loaded) is not LoadedMineruStreamActivation or loaded.algorithm != STREAM_POLICY_ALGORITHM_V2:
        raise ValueError(
            "stream activation names a superseded policy algorithm; "
            "execution requires a mineru.stream-activation.v2 file naming mineru.stream-policy.v2"
        )


__all__ = [
    "STREAM_ACTIVATION_CONTRACT",
    "STREAM_ACTIVATION_CONTRACT_V2",
    "LoadedMineruStreamActivation",
    "load_mineru_stream_activation",
    "require_executable_stream_activation",
]
