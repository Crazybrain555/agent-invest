"""Closed documents of the same-root registry maintenance, as the Mac sees them.

The maintenance runs on Windows (``run_mineru_same_root_maintenance.ps1`` and
the one-shot helper beside the packaged task protocol). The Mac creates the
operation identity, decodes the plan the Windows child observed and writes the
operator approval that binds exactly one plan by digest. Linux root tuples are
decoded as exact JSON integers; booleans or partial tuples are never identity.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import secrets
from typing import Any

from disclosure_anchor.application.contracts.closed_document import (
    canonical_bytes,
    load_closed_object,
    require_fields,
    require_int,
    require_sha256,
    require_str,
)


SAME_ROOT_OPERATION_KIND = "same-root-reattestation"
SAME_ROOT_PHASES = ("prestop", "observe", "apply", "restore", "close", "status")
SAME_ROOT_APPROVAL_CONTRACT = "m6.same-root-approval.v1"
SAME_ROOT_PLAN_CONTRACT = "mineru-same-root-plan.v1"
SAME_ROOT_OBSERVATION_SCHEMA = "mineru-same-root-observation.v1"
ROOT_TUPLE_FIELDS = ("path", "device", "inode", "uid", "mode")
_OPERATION_RE = re.compile(r"^sr-[0-9a-f]{32}$")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_MAX_U64 = (1 << 64) - 1
_MAX_REGISTRY_BYTES = 16 * 1024 * 1024
_PLAN_FIELDS = frozenset({
    "contract_version", "operation_id", "sequence", "generation", "baseline", "captured_utc",
    "release_manifest_sha256", "api_image_id", "windows_identity_sha256", "registry_file_id", "approved_root",
    "current_root", "outcome", "registry_state", "expected_r0_sha256", "expected_r0_bytes", "expected_body_sha256",
    "expected_r1_sha256", "expected_r1_bytes", "record_count", "submission_watermark_bucket", "helper_observation",
})
_OBSERVATION_FIELDS = frozenset({
    "schema", "root", "registry_schema", "approved_root", "current_root", "registry_state", "outcome",
    "r0_sha256", "r0_bytes", "body_sha256", "r1_sha256", "r1_bytes", "record_count",
    "submission_watermark_bucket", "registry_file", "control_directory", "spool_directory",
})


def new_same_root_operation_id() -> str:
    return "sr-" + secrets.token_hex(16)


def require_same_root_operation_id(value: object) -> str:
    if type(value) is not str or _OPERATION_RE.fullmatch(value) is None:
        raise ValueError("same-root operation id must be sr-<32 lowercase hex>")
    return value


def require_root_tuple(value: object, *, label: str) -> dict[str, Any]:
    """One closed Linux root identity with exact non-negative integers."""

    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    require_fields(value, frozenset(ROOT_TUPLE_FIELDS), label=label)
    path = require_str(value["path"], label=f"{label} path")
    if not path.startswith("/"):
        raise ValueError(f"{label} path must be absolute")
    for name in ("device", "inode", "uid", "mode"):
        require_int(value[name], label=f"{label} {name}", minimum=0, maximum=_MAX_U64)
    return {name: value[name] for name in ROOT_TUPLE_FIELDS}


def _utc(value: object, *, label: str) -> str:
    text = require_str(value, label=label, maximum=40)
    if _UTC_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be UTC seconds (YYYY-MM-DDTHH:MM:SSZ)")
    return text


@dataclass(frozen=True, slots=True)
class SameRootApproval:
    operation_id: str
    plan_sha256: str
    approved_by: str
    decision_reference: str
    approved_at_utc: str

    def __post_init__(self) -> None:
        require_same_root_operation_id(self.operation_id)
        require_sha256(self.plan_sha256, label="approval plan_sha256")
        for name in ("approved_by", "decision_reference"):
            text = require_str(getattr(self, name), label=f"approval {name}", maximum=200)
            if not text.isprintable() or text != text.strip():
                raise ValueError(f"approval {name} must be printable without surrounding space")
        _utc(self.approved_at_utc, label="approval approved_at_utc")

    def to_payload(self) -> dict[str, Any]:
        return {"contract_version": SAME_ROOT_APPROVAL_CONTRACT, **asdict(self)}


def encode_same_root_approval(approval: SameRootApproval) -> bytes:
    return canonical_bytes(approval.to_payload())


def decode_same_root_approval(raw: bytes) -> SameRootApproval:
    value = load_closed_object(raw, label="same-root approval", maximum_bytes=65536)
    require_fields(value, frozenset({"contract_version", *SameRootApproval.__dataclass_fields__}), label="same-root approval")
    if value["contract_version"] != SAME_ROOT_APPROVAL_CONTRACT:
        raise ValueError("same-root approval contract is unsupported")
    return SameRootApproval(**{name: value[name] for name in SameRootApproval.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class SameRootPlan:
    """The Windows child's plan: its own fields plus the helper's exact observation line."""

    operation_id: str
    sequence: int
    generation: int
    baseline: str
    captured_utc: str
    release_manifest_sha256: str
    api_image_id: str
    windows_identity_sha256: str
    registry_file_id: str
    approved_root: dict[str, Any]
    current_root: dict[str, Any]
    outcome: str
    registry_state: str
    expected_r0_sha256: str
    expected_r0_bytes: int
    expected_body_sha256: str
    expected_r1_sha256: str
    expected_r1_bytes: int
    record_count: int
    submission_watermark_bucket: int | None
    helper_observation: str
    observation: dict[str, Any]


def _observation(line: object) -> dict[str, Any]:
    text = require_str(line, label="plan helper_observation", maximum=1_048_576)
    value = load_closed_object(text.encode("utf-8"), label="helper observation", maximum_bytes=1_048_576)
    require_fields(value, _OBSERVATION_FIELDS, label="helper observation")
    if value["schema"] != SAME_ROOT_OBSERVATION_SCHEMA:
        raise ValueError("helper observation schema is unsupported")
    return value


def decode_same_root_plan(raw: bytes) -> SameRootPlan:
    """Decode the plan and require every plan field to restate the helper's own observation exactly."""

    value = load_closed_object(raw, label="same-root plan", maximum_bytes=1_048_576)
    require_fields(value, _PLAN_FIELDS, label="same-root plan")
    if value["contract_version"] != SAME_ROOT_PLAN_CONTRACT:
        raise ValueError("same-root plan contract is unsupported")
    observation = _observation(value["helper_observation"])
    approved = require_root_tuple(value["approved_root"], label="plan approved_root")
    current = require_root_tuple(value["current_root"], label="plan current_root")
    if value["outcome"] not in ("noop", "reattest") or value["registry_state"] not in ("r0", "r1"):
        raise ValueError("plan outcome or registry state is outside the closed vocabulary")
    watermark = value["submission_watermark_bucket"]
    if watermark is not None:
        require_int(watermark, label="plan submission_watermark_bucket", minimum=-1)
    restated = {
        "approved_root": "approved_root", "current_root": "current_root", "outcome": "outcome",
        "registry_state": "registry_state", "expected_r0_sha256": "r0_sha256", "expected_r0_bytes": "r0_bytes",
        "expected_body_sha256": "body_sha256", "expected_r1_sha256": "r1_sha256", "expected_r1_bytes": "r1_bytes",
        "record_count": "record_count", "submission_watermark_bucket": "submission_watermark_bucket",
    }
    for plan_name, observed_name in restated.items():
        if value[plan_name] != observation[observed_name] or type(value[plan_name]) is not type(observation[observed_name]):
            raise ValueError(f"plan {plan_name} does not restate the helper observation exactly")
    unchanged = (approved["device"], approved["inode"]) == (current["device"], current["inode"])
    if unchanged != (value["outcome"] == "noop"):
        raise ValueError("plan outcome disagrees with its root tuples")
    return SameRootPlan(
        operation_id=require_same_root_operation_id(value["operation_id"]),
        sequence=require_int(value["sequence"], label="plan sequence"),
        generation=require_int(value["generation"], label="plan generation"),
        baseline=require_str(value["baseline"], label="plan baseline", maximum=64),
        captured_utc=require_str(value["captured_utc"], label="plan captured_utc", maximum=64),
        release_manifest_sha256=require_sha256(value["release_manifest_sha256"], label="plan release manifest"),
        api_image_id=require_sha256(value["api_image_id"], label="plan api_image_id"),
        windows_identity_sha256=require_sha256(value["windows_identity_sha256"], label="plan Windows identity"),
        registry_file_id=require_str(value["registry_file_id"], label="plan registry_file_id", maximum=64),
        approved_root=approved,
        current_root=current,
        outcome=value["outcome"],
        registry_state=value["registry_state"],
        expected_r0_sha256=require_sha256(value["expected_r0_sha256"], label="plan r0 sha256"),
        expected_r0_bytes=require_int(value["expected_r0_bytes"], label="plan r0 bytes", maximum=_MAX_REGISTRY_BYTES),
        expected_body_sha256=require_sha256(value["expected_body_sha256"], label="plan body sha256"),
        expected_r1_sha256=require_sha256(value["expected_r1_sha256"], label="plan r1 sha256"),
        expected_r1_bytes=require_int(value["expected_r1_bytes"], label="plan r1 bytes", maximum=_MAX_REGISTRY_BYTES),
        record_count=require_int(value["record_count"], label="plan record_count", minimum=0),
        submission_watermark_bucket=watermark,
        helper_observation=value["helper_observation"],
        observation=observation,
    )


__all__ = [
    "ROOT_TUPLE_FIELDS",
    "SAME_ROOT_APPROVAL_CONTRACT",
    "SAME_ROOT_OBSERVATION_SCHEMA",
    "SAME_ROOT_OPERATION_KIND",
    "SAME_ROOT_PHASES",
    "SAME_ROOT_PLAN_CONTRACT",
    "SameRootApproval",
    "SameRootPlan",
    "decode_same_root_approval",
    "decode_same_root_plan",
    "encode_same_root_approval",
    "new_same_root_operation_id",
    "require_root_tuple",
    "require_same_root_operation_id",
]
