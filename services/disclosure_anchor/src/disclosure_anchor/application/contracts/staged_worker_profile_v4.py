"""Exact local composition identity, distinct from the remote MinerU process."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
import re
from typing import Any, cast

from disclosure_anchor.application.contracts.strict_json import strict_json_loads


STAGED_WORKER_PROFILE_V4_CONTRACT = "staged-worker-composition.v1"
STAGED_WORKER_PROFILE_V4_COMMIT_CONTRACT = "staged-worker-composition.v2"


@dataclass(frozen=True, slots=True)
class StagedWorkerProfileV4:
    """Hard local ceilings and probe policy for one worker boot/attempt.

    The version identifies the seven-lane/credit/DB projection; actual dispatch
    remains work-conserving below these ceilings. No hardware name is encoded.
    """

    process_profile_sha256: str
    mac_preflight_workers: int
    mac_finalize_workers: int
    provider_poll_milliseconds: int = 1000
    admission_probe_milliseconds: int = 1000
    contract_version: str = STAGED_WORKER_PROFILE_V4_CONTRACT
    commit_stage_seconds: int | None = None

    def __post_init__(self) -> None:
        if type(self.contract_version) is not str or self.contract_version not in {
            STAGED_WORKER_PROFILE_V4_CONTRACT, STAGED_WORKER_PROFILE_V4_COMMIT_CONTRACT,
        }:
            raise ValueError("staged worker profile contract is unsupported")
        if self.contract_version == STAGED_WORKER_PROFILE_V4_CONTRACT:
            if self.commit_stage_seconds is not None:
                raise ValueError("v1 worker profile does not support a commit budget")
        elif type(self.commit_stage_seconds) is not int or not 60 <= self.commit_stage_seconds <= 86400:
            raise ValueError("v2 commit stage budget must be an integer in 60..86400 seconds")
        if (type(self.process_profile_sha256) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", self.process_profile_sha256) is None):
            raise ValueError("staged worker process profile identity is invalid")
        for name in ("mac_preflight_workers", "mac_finalize_workers",
                     "provider_poll_milliseconds", "admission_probe_milliseconds"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if max(self.provider_poll_milliseconds, self.admission_probe_milliseconds) > 60_000:
            raise ValueError("staged worker probe interval exceeds the bounded stage budget")

    @property
    def exact_bytes(self) -> bytes:
        value = asdict(self)
        if self.contract_version == STAGED_WORKER_PROFILE_V4_CONTRACT:
            del value["commit_stage_seconds"]
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.exact_bytes).hexdigest()


def decode_staged_worker_profile_v4(payload: bytes) -> StagedWorkerProfileV4:
    if type(payload) is not bytes or not 0 < len(payload) <= 4096:
        raise ValueError("staged worker profile must be bounded exact bytes")
    value = strict_json_loads(payload)
    expected_fields = {item.name for item in fields(StagedWorkerProfileV4)}
    if type(value) is dict and value.get("contract_version") == STAGED_WORKER_PROFILE_V4_CONTRACT:
        expected_fields.remove("commit_stage_seconds")
    if type(value) is not dict or set(value) != expected_fields:
        raise ValueError("staged worker profile fields are not closed")
    profile = StagedWorkerProfileV4(**cast(dict[str, Any], value))
    if profile.exact_bytes != payload:
        raise ValueError("staged worker profile bytes are not canonical")
    return profile
