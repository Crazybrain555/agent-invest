"""Closed contracts for one reviewed local execution upgrade (U01).

One upgrade relates a fixed set of roles and never merges them:

* the immutable qualification Q0: original runtime R0, writer W0, the
  smoke/canary/held-out receipts, process profile P0, worker profile WP0 and
  stream activation A0. It is re-verified as history with its original dates
  and is never rewritten or relabelled as a current PASS;
* the recovery origin: the execution every listed obligation was frozen under
  (its runtime, process profile and worker profile);
* the actual current execution (the target): every loaded source byte (release
  manifest), the worker interpreter's packages, its writer, runtime, process
  profile, worker profile and stream activation;
* the untouched legacy obligations H0/spec: every unresolved attempt at
  capture, listed in one immutable inventory and bound to the recovery origin.

``worker-local-execution-upgrade.v1`` is the historical first edge Q0 -> E1.
Its recovery origin is its parent Q0 itself (R0/P0/WP0), and E1 must move every
local identity. Its decoder, checks and texts are unchanged.

``worker-local-execution-upgrade.v2`` names Q0 as ``qualification_anchor`` and
a separately archived ``recovery_origin`` (for example E1/R1/P1/WP1/A1) beside
the target, and is proven by two direct relations:

* Q0 -> target: the same computation. Only the local writer and the
  references that name the runtime may move;
* origin -> target: the same computation again, plus a new release.

Identities move only where their bytes moved, so a target may change its
release while keeping the origin's writer and runtime.

In both versions the runtime manifests may differ only in the local writer
digest. A process profile moves only its runtime reference, a worker profile
only its process-profile reference. One approval is one exact relation: a v2
relation never reads or re-verifies an older upgrade, and there is no
transitive chain, hash allowlist or ambient authorization. The runtime adapter
reads and hashes the files; this module performs no IO.

``worker-qualified-runtime-upgrade.v1`` (``newly_qualified_result_runtime``)
is a separate branch, not a loosening of those predicates. The target runtime
carries its own new qualification Qnew (the exact deployment path verifies it
as current), and one archived recovery origin names where every listed
obligation was frozen. Only never-submitted ``prepared`` obligations qualify,
each with a read-only original-key lookup proving absence within the key
lifetime. The origin -> target relation allows only the closed result-storage
axes below; models, inference, MinerU versions, parsing knobs, topology and
every frozen request fact stay equal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
import hashlib
from math import isfinite
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from disclosure_anchor.application.contracts.closed_document import (
    canonical_bytes,
    load_closed_object,
    require_fields,
    require_int,
    require_sha256,
    require_str,
)
from disclosure_anchor.application.contracts.mineru_process_profile import MineruProcessProfile
from disclosure_anchor.application.contracts.staged_resource_credit import (
    STAGED_RESOURCE_STATE_TRANSITIONS,
)
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4
from disclosure_anchor.application.ports.new_work_admission import NewWorkAdmissionUnavailable

if TYPE_CHECKING:
    from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import SubmissionIntentV4
    from disclosure_anchor.application.contracts.v4_prepared_execution_spec import (
        V4PreparedExecutionSpec,
    )
    from disclosure_anchor.application.ports.remote_parse_v4_repository import RemoteParseV4Authority
    from disclosure_anchor.application.ports.remote_provider_v4 import RemoteSubmissionCommandV4


UPGRADE_CONTRACT = "worker-local-execution-upgrade.v1"
UPGRADE_CONTRACT_V2 = "worker-local-execution-upgrade.v2"
QUALIFIED_UPGRADE_CONTRACT = "worker-qualified-runtime-upgrade.v1"
QUALIFIED_TRANSITION_KIND = "newly_qualified_result_runtime"
KEY_LOOKUP_CONTRACT = "worker-legacy-key-lookup.v1"
REVIEW_CONTRACT = "worker-local-execution-upgrade-review.v1"
RELEASE_MANIFEST_CONTRACT = "worker-execution-release.v1"
INVENTORY_CONTRACT = "worker-legacy-scope-inventory.v1"
TRANSITION_KIND = "local_operational_compatible"
QualificationOrigin = Literal["exact", "compatible_parent"]
MAX_UPGRADE_BYTES = 64 * 1024
MAX_REVIEW_BYTES = 16 * 1024
MAX_RELEASE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_INVENTORY_BYTES = 16 * 1024 * 1024
MAX_KEY_LOOKUP_BYTES = 4 * 1024 * 1024
_MAX_RELEASE_FILES = 100_000
_MAX_MEMBERS = 1_000_000
_UNRESOLVED_STATES = frozenset(STAGED_RESOURCE_STATE_TRANSITIONS)


class LegacyExecutionRefused(ValueError):
    """A legacy obligation is outside the verified upgrade edge."""


class LegacyObligationsOpen(NewWorkAdmissionUnavailable):
    """New H0 admission waits until every legacy obligation is closed."""


def _absolute_path(value: object, *, label: str) -> str:
    text = require_str(value, label=label)
    pure = PurePosixPath(text)
    if (
        not pure.is_absolute()
        or "\x00" in text
        or ".." in pure.parts
        or pure.as_posix() != text
    ):
        raise ValueError(f"{label} must be a normalized absolute path")
    return text


def _relative_path(value: object, *, label: str) -> str:
    text = require_str(value, label=label, maximum=1024)
    pure = PurePosixPath(text)
    if (
        pure.is_absolute()
        or "\x00" in text
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != text
    ):
        raise ValueError(f"{label} must be a normalized relative path")
    return text


def _printable(value: object, *, label: str, maximum: int) -> str:
    text = require_str(value, label=label, maximum=maximum)
    if text != text.strip() or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{label} must be printable text without surrounding space")
    return text


def _utc_timestamp(value: object, *, label: str) -> str:
    text = require_str(value, label=label, maximum=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must carry a timezone")
    return text


def _identifier(value: object, *, label: str) -> str:
    return _printable(value, label=label, maximum=128)


@dataclass(frozen=True, slots=True)
class ParentQualification:
    runtime_identity_sha256: str
    writer_code_sha256: str
    smoke_receipt_sha256: str
    canary_cache_sha256: str
    validation_receipt_sha256: str
    process_profile_file: str
    process_profile_sha256: str
    worker_profile_sha256: str
    stream_activation_file: str
    stream_activation_sha256: str
    qualified_at_utc: str
    service_epoch_sha256: str


@dataclass(frozen=True, slots=True)
class CurrentExecution:
    release_manifest_file: str
    release_manifest_sha256: str
    source_revision: str
    writer_code_sha256: str
    runtime_bundle_file: str
    runtime_bundle_sha256: str
    runtime_identity_sha256: str
    process_profile_sha256: str
    worker_profile_sha256: str
    capacity_config_sha256: str
    stream_activation_sha256: str


@dataclass(frozen=True, slots=True)
class CompatibilityBasis:
    exact_change_manifest_sha256: str
    independent_test_evidence_sha256: str
    independent_code_review_sha256: str


@dataclass(frozen=True, slots=True)
class LegacyScopeReference:
    inventory_file: str
    inventory_sha256: str
    member_count: int


@dataclass(frozen=True, slots=True)
class LocalExecutionUpgrade:
    """The U01 proposal: one exact Q0 -> E1 edge over one legacy inventory."""

    parent: ParentQualification
    current: CurrentExecution
    basis: CompatibilityBasis
    legacy_scope: LegacyScopeReference

    def __post_init__(self) -> None:
        parent, current = self.parent, self.current
        if (
            parent.runtime_identity_sha256 == current.runtime_identity_sha256
            or parent.writer_code_sha256 == current.writer_code_sha256
            or parent.process_profile_sha256 == current.process_profile_sha256
            or parent.worker_profile_sha256 == current.worker_profile_sha256
            or parent.stream_activation_sha256 == current.stream_activation_sha256
        ):
            raise ValueError("local execution upgrade must change exactly the local execution identities")

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_version": UPGRADE_CONTRACT,
            "transition_kind": TRANSITION_KIND,
            "parent_qualification": _dataclass_payload(self.parent),
            "current_execution": _dataclass_payload(self.current),
            "compatibility_basis": _dataclass_payload(self.basis),
            "legacy_scope": _dataclass_payload(self.legacy_scope),
        }


def _dataclass_payload(value: object) -> dict[str, Any]:
    return {name: getattr(value, name) for name in value.__dataclass_fields__}  # type: ignore[attr-defined]


_PARENT_SHA_FIELDS = (
    "runtime_identity_sha256", "writer_code_sha256", "smoke_receipt_sha256", "canary_cache_sha256",
    "validation_receipt_sha256", "process_profile_sha256", "worker_profile_sha256",
    "stream_activation_sha256", "service_epoch_sha256",
)
_CURRENT_SHA_FIELDS = (
    "release_manifest_sha256", "writer_code_sha256", "runtime_bundle_sha256", "runtime_identity_sha256",
    "process_profile_sha256", "worker_profile_sha256", "capacity_config_sha256",
    "stream_activation_sha256",
)


def decode_local_execution_upgrade(payload: bytes) -> LocalExecutionUpgrade:
    value = load_closed_object(payload, label="local execution upgrade", maximum_bytes=MAX_UPGRADE_BYTES)
    require_fields(value, {
        "contract_version", "transition_kind", "parent_qualification", "current_execution",
        "compatibility_basis", "legacy_scope",
    }, label="local execution upgrade")
    if value["contract_version"] != UPGRADE_CONTRACT or value["transition_kind"] != TRANSITION_KIND:
        raise ValueError("local execution upgrade contract or transition kind is unsupported")
    parent_raw, current_raw = value["parent_qualification"], value["current_execution"]
    basis_raw, scope_raw = value["compatibility_basis"], value["legacy_scope"]
    if not all(type(item) is dict for item in (parent_raw, current_raw, basis_raw, scope_raw)):
        raise ValueError("local execution upgrade sections must be objects")
    require_fields(parent_raw, set(ParentQualification.__dataclass_fields__), label="parent qualification")
    require_fields(current_raw, set(CurrentExecution.__dataclass_fields__), label="current execution")
    require_fields(basis_raw, set(CompatibilityBasis.__dataclass_fields__), label="compatibility basis")
    require_fields(scope_raw, set(LegacyScopeReference.__dataclass_fields__), label="legacy scope")
    for name in _PARENT_SHA_FIELDS:
        require_sha256(parent_raw[name], label=f"parent {name}")
    for name in _CURRENT_SHA_FIELDS:
        require_sha256(current_raw[name], label=f"current {name}")
    for name in CompatibilityBasis.__dataclass_fields__:
        require_sha256(basis_raw[name], label=f"compatibility {name}")
    parent = ParentQualification(
        **{name: parent_raw[name] for name in _PARENT_SHA_FIELDS},
        process_profile_file=_absolute_path(parent_raw["process_profile_file"], label="parent process profile file"),
        stream_activation_file=_absolute_path(
            parent_raw["stream_activation_file"], label="parent stream activation file",
        ),
        qualified_at_utc=_utc_timestamp(parent_raw["qualified_at_utc"], label="parent qualified_at_utc"),
    )
    current = CurrentExecution(
        **{name: current_raw[name] for name in _CURRENT_SHA_FIELDS},
        release_manifest_file=_absolute_path(current_raw["release_manifest_file"], label="release manifest file"),
        runtime_bundle_file=_absolute_path(current_raw["runtime_bundle_file"], label="runtime bundle file"),
        source_revision=_printable(current_raw["source_revision"], label="source revision", maximum=128),
    )
    basis = CompatibilityBasis(**{name: basis_raw[name] for name in CompatibilityBasis.__dataclass_fields__})
    scope = LegacyScopeReference(
        inventory_file=_absolute_path(scope_raw["inventory_file"], label="legacy inventory file"),
        inventory_sha256=require_sha256(scope_raw["inventory_sha256"], label="legacy inventory sha256"),
        member_count=require_int(scope_raw["member_count"], label="legacy member count", minimum=0,
                                 maximum=_MAX_MEMBERS),
    )
    return LocalExecutionUpgrade(parent=parent, current=current, basis=basis, legacy_scope=scope)


def encode_local_execution_upgrade(upgrade: LocalExecutionUpgrade) -> bytes:
    encoded = canonical_bytes(upgrade.to_payload())
    if decode_local_execution_upgrade(encoded) != upgrade:
        raise ValueError("local execution upgrade does not round-trip")
    return encoded


@dataclass(frozen=True, slots=True)
class RecoveryOrigin:
    """The archived execution a v2 inventory's obligations were frozen under.

    Every identity is re-derived from these archived files, verified against
    their own pins. They are never compared with the current tree.
    """

    release_manifest_file: str
    release_manifest_sha256: str
    source_revision: str
    writer_code_sha256: str
    runtime_bundle_file: str
    runtime_bundle_sha256: str
    runtime_identity_sha256: str
    process_profile_file: str
    process_profile_sha256: str
    worker_profile_sha256: str
    capacity_config_sha256: str
    stream_activation_file: str
    stream_activation_sha256: str


@dataclass(frozen=True, slots=True)
class LocalExecutionUpgradeV2:
    """The v2 proposal: Q0 anchor, one recovery origin and the target over one inventory."""

    qualification_anchor: ParentQualification
    recovery_origin: RecoveryOrigin
    current: CurrentExecution
    basis: CompatibilityBasis
    legacy_scope: LegacyScopeReference

    def __post_init__(self) -> None:
        if (
            type(self.qualification_anchor) is not ParentQualification
            or type(self.recovery_origin) is not RecoveryOrigin
            or type(self.current) is not CurrentExecution
            or type(self.basis) is not CompatibilityBasis
            or type(self.legacy_scope) is not LegacyScopeReference
        ):
            raise ValueError("local execution upgrade v2 sections must be exact")
        origin, current = self.recovery_origin, self.current
        if current.release_manifest_sha256 == origin.release_manifest_sha256:
            raise ValueError("a recovery upgrade target must be a new release, not its recovery origin")
        if current.capacity_config_sha256 != origin.capacity_config_sha256:
            raise ValueError("a recovery upgrade cannot move the recovery origin's explicit capacity")

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_version": UPGRADE_CONTRACT_V2,
            "transition_kind": TRANSITION_KIND,
            "qualification_anchor": _dataclass_payload(self.qualification_anchor),
            "recovery_origin": _dataclass_payload(self.recovery_origin),
            "current_execution": _dataclass_payload(self.current),
            "compatibility_basis": _dataclass_payload(self.basis),
            "legacy_scope": _dataclass_payload(self.legacy_scope),
        }


_ORIGIN_SHA_FIELDS = (
    "release_manifest_sha256", "writer_code_sha256", "runtime_bundle_sha256", "runtime_identity_sha256",
    "process_profile_sha256", "worker_profile_sha256", "capacity_config_sha256", "stream_activation_sha256",
)
_ORIGIN_FILE_FIELDS = (
    "release_manifest_file", "runtime_bundle_file", "process_profile_file", "stream_activation_file",
)


def decode_local_execution_upgrade_v2(payload: bytes) -> LocalExecutionUpgradeV2:
    value = load_closed_object(payload, label="local execution upgrade", maximum_bytes=MAX_UPGRADE_BYTES)
    require_fields(value, {
        "contract_version", "transition_kind", "qualification_anchor", "recovery_origin",
        "current_execution", "compatibility_basis", "legacy_scope",
    }, label="local execution upgrade v2")
    if value["contract_version"] != UPGRADE_CONTRACT_V2 or value["transition_kind"] != TRANSITION_KIND:
        raise ValueError("local execution upgrade contract or transition kind is unsupported")
    anchor_raw, origin_raw = value["qualification_anchor"], value["recovery_origin"]
    current_raw, basis_raw, scope_raw = value["current_execution"], value["compatibility_basis"], value["legacy_scope"]
    if not all(type(item) is dict for item in (anchor_raw, origin_raw, current_raw, basis_raw, scope_raw)):
        raise ValueError("local execution upgrade sections must be objects")
    require_fields(anchor_raw, set(ParentQualification.__dataclass_fields__), label="qualification anchor")
    require_fields(origin_raw, set(RecoveryOrigin.__dataclass_fields__), label="recovery origin")
    require_fields(current_raw, set(CurrentExecution.__dataclass_fields__), label="current execution")
    require_fields(basis_raw, set(CompatibilityBasis.__dataclass_fields__), label="compatibility basis")
    require_fields(scope_raw, set(LegacyScopeReference.__dataclass_fields__), label="legacy scope")
    for name in _PARENT_SHA_FIELDS:
        require_sha256(anchor_raw[name], label=f"qualification anchor {name}")
    for name in _ORIGIN_SHA_FIELDS:
        require_sha256(origin_raw[name], label=f"recovery origin {name}")
    for name in _CURRENT_SHA_FIELDS:
        require_sha256(current_raw[name], label=f"current {name}")
    for name in CompatibilityBasis.__dataclass_fields__:
        require_sha256(basis_raw[name], label=f"compatibility {name}")
    anchor = ParentQualification(
        **{name: anchor_raw[name] for name in _PARENT_SHA_FIELDS},
        process_profile_file=_absolute_path(
            anchor_raw["process_profile_file"], label="qualification anchor process profile file",
        ),
        stream_activation_file=_absolute_path(
            anchor_raw["stream_activation_file"], label="qualification anchor stream activation file",
        ),
        qualified_at_utc=_utc_timestamp(
            anchor_raw["qualified_at_utc"], label="qualification anchor qualified_at_utc",
        ),
    )
    origin = RecoveryOrigin(
        **{name: origin_raw[name] for name in _ORIGIN_SHA_FIELDS},
        **{name: _absolute_path(origin_raw[name], label=f"recovery origin {name}") for name in _ORIGIN_FILE_FIELDS},
        source_revision=_printable(origin_raw["source_revision"], label="recovery origin source revision", maximum=128),
    )
    current = CurrentExecution(
        **{name: current_raw[name] for name in _CURRENT_SHA_FIELDS},
        release_manifest_file=_absolute_path(current_raw["release_manifest_file"], label="release manifest file"),
        runtime_bundle_file=_absolute_path(current_raw["runtime_bundle_file"], label="runtime bundle file"),
        source_revision=_printable(current_raw["source_revision"], label="source revision", maximum=128),
    )
    basis = CompatibilityBasis(**{name: basis_raw[name] for name in CompatibilityBasis.__dataclass_fields__})
    scope = LegacyScopeReference(
        inventory_file=_absolute_path(scope_raw["inventory_file"], label="legacy inventory file"),
        inventory_sha256=require_sha256(scope_raw["inventory_sha256"], label="legacy inventory sha256"),
        member_count=require_int(scope_raw["member_count"], label="legacy member count", minimum=0,
                                 maximum=_MAX_MEMBERS),
    )
    return LocalExecutionUpgradeV2(
        qualification_anchor=anchor, recovery_origin=origin, current=current, basis=basis, legacy_scope=scope,
    )


def encode_local_execution_upgrade_v2(upgrade: LocalExecutionUpgradeV2) -> bytes:
    encoded = canonical_bytes(upgrade.to_payload())
    if decode_local_execution_upgrade_v2(encoded) != upgrade:
        raise ValueError("local execution upgrade does not round-trip")
    return encoded


# Runtime manifest fields a newly qualified result-storage runtime may move:
# the native image and its compatibility layers, the service configuration
# carrying the storage policy, the capacity config with its legacy per-task
# budgets, the Windows compose/collector files, and the local writer. Anything
# else (models, inference server, MinerU versions, parsing windows, ratios,
# locks, commands, endpoints, node identity) must stay equal.
QUALIFIED_RUNTIME_CHANGE_FIELDS = frozenset({
    ("client", "writer_code_sha256"),
    ("orchestrator", "capacity_config"),
    ("orchestrator", "capacity_config_sha256"),
    ("orchestrator", "capacity_runtime_compatibility_sha256"),
    ("orchestrator", "capacity_source_sha256"),
    ("orchestrator", "container_image_digest"),
    ("orchestrator", "heap_return_compatibility_sha256"),
    ("orchestrator", "max_unacked_result_bytes"),
    ("orchestrator", "service_config_sha256"),
    ("orchestrator", "task_result_reservation_bytes"),
    ("topology", "windows_collector_sha256"),
    ("topology", "windows_compose_sha256"),
})
# Process-profile fields the same runtime may move: its contract (v2 -> v3),
# its runtime and native image references, the legacy per-task result budgets
# replaced by the bound storage policy, and the Mac ceilings that policy sizes.
QUALIFIED_PROCESS_PROFILE_CHANGE_FIELDS = frozenset({
    "contract_version",
    "decoded_payload_bytes_limit",
    "max_unacked_result_bytes",
    "orchestrator_image_identity_sha256",
    "result_reservation_bytes",
    "result_storage_policy_sha256",
    "runtime_bundle_identity_sha256",
    "temporary_disk_bytes_limit",
    "terminal_output_bytes_limit",
})
# Compute limits a capacity change must keep: only the result budgets move.
_CAPACITY_COMPUTE_FIELDS = (
    "parse_active_limit", "total_nonterminal_limit", "finalizer_active_limit",
    "final_http_limit_per_loop", "api_process_limit", "api_event_loop_limit",
    "processing_window_size", "omp_num_threads", "mkl_num_threads", "openblas_num_threads",
    "pdf_render_processes_requested", "hybrid_batch_ratio_requested", "pipeline_inference_locks",
)
_STORAGE_CAPACITY_CONTRACT = "mineru.capacity-config.v2"


@dataclass(frozen=True, slots=True)
class KeyLookupEvidenceReference:
    evidence_file: str
    evidence_sha256: str
    key_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class QualifiedRuntimeUpgrade:
    """One reviewed edge from an archived origin to a newly qualified runtime.

    ``target_qualification`` is Qnew, the target's own qualification; the
    exact deployment path verifies it as current and it is never inherited.
    ``recovery_origin`` is where every inventory member was frozen.
    ``runtime_changes`` names exactly the manifest fields that moved.
    """

    target_qualification: ParentQualification
    recovery_origin: RecoveryOrigin
    current: CurrentExecution
    runtime_changes: tuple[tuple[str, str], ...]
    basis: CompatibilityBasis
    legacy_scope: LegacyScopeReference
    key_lookups: KeyLookupEvidenceReference

    def __post_init__(self) -> None:
        if (
            type(self.target_qualification) is not ParentQualification
            or type(self.recovery_origin) is not RecoveryOrigin
            or type(self.current) is not CurrentExecution
            or type(self.basis) is not CompatibilityBasis
            or type(self.legacy_scope) is not LegacyScopeReference
            or type(self.key_lookups) is not KeyLookupEvidenceReference
            or type(self.runtime_changes) is not tuple
        ):
            raise ValueError("qualified runtime upgrade sections must be exact")
        qualification, origin, current = self.target_qualification, self.recovery_origin, self.current
        if (
            qualification.runtime_identity_sha256, qualification.writer_code_sha256,
            qualification.process_profile_sha256, qualification.worker_profile_sha256,
            qualification.stream_activation_sha256,
        ) != (
            current.runtime_identity_sha256, current.writer_code_sha256, current.process_profile_sha256,
            current.worker_profile_sha256, current.stream_activation_sha256,
        ):
            raise ValueError("the new qualification must qualify exactly the target execution")
        if (
            origin.runtime_identity_sha256 == current.runtime_identity_sha256
            or origin.release_manifest_sha256 == current.release_manifest_sha256
        ):
            raise ValueError("a qualified runtime upgrade moves to a new runtime and release")
        if (
            not self.runtime_changes
            or list(self.runtime_changes) != sorted(set(self.runtime_changes))
            or any(
                type(item) is not tuple or len(item) != 2 or item not in QUALIFIED_RUNTIME_CHANGE_FIELDS
                for item in self.runtime_changes
            )
        ):
            raise ValueError("qualified runtime changes must be a sorted subset of the result-storage axes")
        if self.legacy_scope.member_count < 1:
            raise ValueError("a qualified runtime upgrade carries at least one legacy obligation")
        if self.key_lookups.key_ttl_seconds < 1:
            raise ValueError("the provider key lifetime must be positive")

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_version": QUALIFIED_UPGRADE_CONTRACT,
            "transition_kind": QUALIFIED_TRANSITION_KIND,
            "target_qualification": _dataclass_payload(self.target_qualification),
            "recovery_origin": _dataclass_payload(self.recovery_origin),
            "current_execution": _dataclass_payload(self.current),
            "runtime_changes": [list(item) for item in self.runtime_changes],
            "compatibility_basis": _dataclass_payload(self.basis),
            "legacy_scope": _dataclass_payload(self.legacy_scope),
            "key_lookups": _dataclass_payload(self.key_lookups),
        }


def decode_qualified_runtime_upgrade(payload: bytes) -> QualifiedRuntimeUpgrade:
    value = load_closed_object(payload, label="qualified runtime upgrade", maximum_bytes=MAX_UPGRADE_BYTES)
    require_fields(value, {
        "contract_version", "transition_kind", "target_qualification", "recovery_origin",
        "current_execution", "runtime_changes", "compatibility_basis", "legacy_scope", "key_lookups",
    }, label="qualified runtime upgrade")
    if (
        value["contract_version"] != QUALIFIED_UPGRADE_CONTRACT
        or value["transition_kind"] != QUALIFIED_TRANSITION_KIND
    ):
        raise ValueError("qualified runtime upgrade contract or transition kind is unsupported")
    sections = (
        value["target_qualification"], value["recovery_origin"], value["current_execution"],
        value["compatibility_basis"], value["legacy_scope"], value["key_lookups"],
    )
    if not all(type(item) is dict for item in sections):
        raise ValueError("qualified runtime upgrade sections must be objects")
    qualification_raw, origin_raw, current_raw, basis_raw, scope_raw, lookups_raw = sections
    require_fields(qualification_raw, set(ParentQualification.__dataclass_fields__), label="target qualification")
    require_fields(origin_raw, set(RecoveryOrigin.__dataclass_fields__), label="recovery origin")
    require_fields(current_raw, set(CurrentExecution.__dataclass_fields__), label="current execution")
    require_fields(basis_raw, set(CompatibilityBasis.__dataclass_fields__), label="compatibility basis")
    require_fields(scope_raw, set(LegacyScopeReference.__dataclass_fields__), label="legacy scope")
    require_fields(lookups_raw, set(KeyLookupEvidenceReference.__dataclass_fields__), label="key lookups")
    for name in _PARENT_SHA_FIELDS:
        require_sha256(qualification_raw[name], label=f"target qualification {name}")
    for name in _ORIGIN_SHA_FIELDS:
        require_sha256(origin_raw[name], label=f"recovery origin {name}")
    for name in _CURRENT_SHA_FIELDS:
        require_sha256(current_raw[name], label=f"current {name}")
    for name in CompatibilityBasis.__dataclass_fields__:
        require_sha256(basis_raw[name], label=f"compatibility {name}")
    changes = value["runtime_changes"]
    if type(changes) is not list or any(
        type(item) is not list or len(item) != 2 or not all(type(part) is str for part in item)
        for item in changes
    ):
        raise ValueError("qualified runtime changes must be [section, field] pairs")
    qualification = ParentQualification(
        **{name: qualification_raw[name] for name in _PARENT_SHA_FIELDS},
        process_profile_file=_absolute_path(
            qualification_raw["process_profile_file"], label="target qualification process profile file",
        ),
        stream_activation_file=_absolute_path(
            qualification_raw["stream_activation_file"], label="target qualification stream activation file",
        ),
        qualified_at_utc=_utc_timestamp(
            qualification_raw["qualified_at_utc"], label="target qualification qualified_at_utc",
        ),
    )
    origin = RecoveryOrigin(
        **{name: origin_raw[name] for name in _ORIGIN_SHA_FIELDS},
        **{name: _absolute_path(origin_raw[name], label=f"recovery origin {name}") for name in _ORIGIN_FILE_FIELDS},
        source_revision=_printable(origin_raw["source_revision"], label="recovery origin source revision", maximum=128),
    )
    current = CurrentExecution(
        **{name: current_raw[name] for name in _CURRENT_SHA_FIELDS},
        release_manifest_file=_absolute_path(current_raw["release_manifest_file"], label="release manifest file"),
        runtime_bundle_file=_absolute_path(current_raw["runtime_bundle_file"], label="runtime bundle file"),
        source_revision=_printable(current_raw["source_revision"], label="source revision", maximum=128),
    )
    return QualifiedRuntimeUpgrade(
        target_qualification=qualification,
        recovery_origin=origin,
        current=current,
        runtime_changes=tuple((item[0], item[1]) for item in changes),
        basis=CompatibilityBasis(**{name: basis_raw[name] for name in CompatibilityBasis.__dataclass_fields__}),
        legacy_scope=LegacyScopeReference(
            inventory_file=_absolute_path(scope_raw["inventory_file"], label="legacy inventory file"),
            inventory_sha256=require_sha256(scope_raw["inventory_sha256"], label="legacy inventory sha256"),
            member_count=require_int(scope_raw["member_count"], label="legacy member count", minimum=0,
                                     maximum=_MAX_MEMBERS),
        ),
        key_lookups=KeyLookupEvidenceReference(
            evidence_file=_absolute_path(lookups_raw["evidence_file"], label="key lookup evidence file"),
            evidence_sha256=require_sha256(lookups_raw["evidence_sha256"], label="key lookup evidence sha256"),
            key_ttl_seconds=require_int(lookups_raw["key_ttl_seconds"], label="provider key lifetime", minimum=0,
                                        maximum=366 * 24 * 60 * 60),
        ),
    )


def encode_qualified_runtime_upgrade(upgrade: QualifiedRuntimeUpgrade) -> bytes:
    encoded = canonical_bytes(upgrade.to_payload())
    if decode_qualified_runtime_upgrade(encoded) != upgrade:
        raise ValueError("qualified runtime upgrade does not round-trip")
    return encoded


AnyExecutionUpgrade = LocalExecutionUpgrade | LocalExecutionUpgradeV2 | QualifiedRuntimeUpgrade


def decode_execution_upgrade(payload: bytes) -> AnyExecutionUpgrade:
    """Select the decoder by the closed contract version.

    Only an exact v2 or qualified contract version selects its decoder; every
    other payload keeps the unchanged v1 decoder and its refusals.
    """

    value = load_closed_object(payload, label="local execution upgrade", maximum_bytes=MAX_UPGRADE_BYTES)
    if value.get("contract_version") == UPGRADE_CONTRACT_V2:
        return decode_local_execution_upgrade_v2(payload)
    if value.get("contract_version") == QUALIFIED_UPGRADE_CONTRACT:
        return decode_qualified_runtime_upgrade(payload)
    return decode_local_execution_upgrade(payload)


@dataclass(frozen=True, slots=True)
class LegacyKeyLookup:
    """One read-only lookup of an obligation's original key on the origin API."""

    attempt_id: str
    client_submit_key: str
    lookup_request_sha256: str
    http_status: int
    response_sha256: str
    response_byte_count: int
    observed_at_utc: str


@dataclass(frozen=True, slots=True)
class LegacyKeyLookupEvidence:
    """Closed absence of every original key, observed before the origin API retires."""

    api_runtime_identity_sha256: str
    key_ttl_seconds: int
    lookups: tuple[LegacyKeyLookup, ...]

    def __post_init__(self) -> None:
        attempts = [item.attempt_id for item in self.lookups]
        if attempts != sorted(attempts) or len(set(attempts)) != len(attempts):
            raise ValueError("key lookups must be unique and sorted by attempt")
        keys = [item.client_submit_key for item in self.lookups]
        if len(set(keys)) != len(keys):
            raise ValueError("key lookups repeat an original key")

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_version": KEY_LOOKUP_CONTRACT,
            "api_runtime_identity_sha256": self.api_runtime_identity_sha256,
            "key_ttl_seconds": self.key_ttl_seconds,
            "lookups": [_dataclass_payload(item) for item in self.lookups],
        }


def decode_legacy_key_lookup_evidence(payload: bytes) -> LegacyKeyLookupEvidence:
    value = load_closed_object(payload, label="legacy key lookup evidence", maximum_bytes=MAX_KEY_LOOKUP_BYTES)
    require_fields(value, {"contract_version", "api_runtime_identity_sha256", "key_ttl_seconds", "lookups"},
                   label="legacy key lookup evidence")
    if value["contract_version"] != KEY_LOOKUP_CONTRACT:
        raise ValueError("legacy key lookup evidence contract is unsupported")
    lookups = value["lookups"]
    if type(lookups) is not list or len(lookups) > _MAX_MEMBERS:
        raise ValueError("legacy key lookups are outside the closed envelope")
    decoded = []
    for index, item in enumerate(lookups):
        label = f"key lookup {index}"
        if type(item) is not dict:
            raise ValueError(f"{label} must be an object")
        require_fields(item, set(LegacyKeyLookup.__dataclass_fields__), label=label)
        decoded.append(LegacyKeyLookup(
            attempt_id=_identifier(item["attempt_id"], label=f"{label} attempt"),
            client_submit_key=_identifier(item["client_submit_key"], label=f"{label} key"),
            lookup_request_sha256=require_sha256(item["lookup_request_sha256"], label=f"{label} request"),
            http_status=require_int(item["http_status"], label=f"{label} status", minimum=100, maximum=599),
            response_sha256=require_sha256(item["response_sha256"], label=f"{label} response"),
            response_byte_count=require_int(item["response_byte_count"], label=f"{label} bytes", minimum=0,
                                            maximum=MAX_KEY_LOOKUP_BYTES),
            observed_at_utc=_utc_timestamp(item["observed_at_utc"], label=f"{label} observed_at_utc"),
        ))
    return LegacyKeyLookupEvidence(
        api_runtime_identity_sha256=require_sha256(
            value["api_runtime_identity_sha256"], label="key lookup API runtime",
        ),
        key_ttl_seconds=require_int(value["key_ttl_seconds"], label="provider key lifetime", minimum=1,
                                    maximum=366 * 24 * 60 * 60),
        lookups=tuple(decoded),
    )


def encode_legacy_key_lookup_evidence(evidence: LegacyKeyLookupEvidence) -> bytes:
    encoded = canonical_bytes(evidence.to_payload())
    if decode_legacy_key_lookup_evidence(encoded) != evidence:
        raise ValueError("legacy key lookup evidence does not round-trip")
    return encoded


def require_key_lookup_coverage(
    evidence: LegacyKeyLookupEvidence,
    inventory: LegacyScopeInventory,
    *,
    origin_runtime_identity_sha256: str,
    key_ttl_seconds: int,
) -> None:
    """Every original key was looked up on the origin API, absent, within its lifetime.

    The lookups follow the capture, so no member could have been accepted
    between them; each key was still inside the provider's lifetime when it
    was proven absent.
    """

    if evidence.api_runtime_identity_sha256 != origin_runtime_identity_sha256:
        raise ValueError("key lookups were not answered by the recovery origin runtime")
    if evidence.key_ttl_seconds != key_ttl_seconds:
        raise ValueError("key lookup lifetime is not the proposal's provider key lifetime")
    members = {item.attempt_id: item for item in inventory.members}
    if set(members) != {item.attempt_id for item in evidence.lookups}:
        raise ValueError("key lookups do not cover exactly the legacy inventory")
    captured = datetime.fromisoformat(inventory.captured_at_utc.replace("Z", "+00:00"))
    for lookup in evidence.lookups:
        member = members[lookup.attempt_id]
        observed = datetime.fromisoformat(lookup.observed_at_utc.replace("Z", "+00:00"))
        if lookup.client_submit_key != member.client_submit_key:
            raise ValueError(f"key lookup for {lookup.attempt_id} is not its original key")
        if lookup.http_status != 404:
            raise ValueError(f"original key of {lookup.attempt_id} was not proven absent")
        if observed < captured:
            raise ValueError(f"key lookup for {lookup.attempt_id} precedes the inventory capture")
        if observed.timestamp() - member.submission_epoch_unix >= key_ttl_seconds:
            raise ValueError(f"original key of {lookup.attempt_id} had expired when it was looked up")


def require_result_runtime_change(
    origin_manifest: object,
    target_manifest: object,
    *,
    changes: tuple[tuple[str, str], ...],
) -> None:
    """The target manifest equals the origin except exactly the declared fields."""

    if type(origin_manifest) is not dict or type(target_manifest) is not dict:
        raise ValueError("runtime manifests must be objects")
    if set(origin_manifest) != set(target_manifest):
        raise ValueError("target runtime manifest sections differ from the recovery origin")
    moved: set[tuple[str, str]] = set()
    for section, origin_value in origin_manifest.items():
        target_value = target_manifest[section]
        if type(origin_value) is dict and type(target_value) is dict:
            for name in set(origin_value) | set(target_value):
                if origin_value.get(name, _ABSENT) != target_value.get(name, _ABSENT):
                    moved.add((section, name))
        elif origin_value != target_value:
            raise ValueError(f"target runtime manifest {section} differs from the recovery origin")
    if moved - QUALIFIED_RUNTIME_CHANGE_FIELDS:
        raise ValueError(
            "target runtime differs from the recovery origin outside the result-storage axes: "
            + ", ".join(".".join(item) for item in sorted(moved - QUALIFIED_RUNTIME_CHANGE_FIELDS))
        )
    if moved != set(changes):
        raise ValueError("target runtime changes are not exactly the reviewed changes")


_ABSENT = object()


def require_result_capacity_change(origin_capacity: object, target_capacity: object) -> None:
    """Only the result budgets move: the target is a storage capacity with equal compute limits."""

    if type(origin_capacity) is not dict or type(target_capacity) is not dict:
        raise ValueError("capacity configs must be objects")
    if target_capacity.get("contract_version") != _STORAGE_CAPACITY_CONTRACT:
        raise ValueError("a qualified result runtime requires a result-storage capacity config")
    for name in _CAPACITY_COMPUTE_FIELDS:
        if name not in origin_capacity or origin_capacity[name] != target_capacity.get(name, _ABSENT):
            raise ValueError(f"target capacity changes compute limit {name}")


def require_result_process_profile_change(
    origin: MineruProcessProfile,
    target: MineruProcessProfile,
    *,
    origin_runtime_sha256: str,
    target_runtime_sha256: str,
) -> None:
    """The target profile equals the origin except the closed result-storage fields."""

    if type(origin) is not MineruProcessProfile or type(target) is not MineruProcessProfile:
        raise ValueError("process profiles must be exact")
    if (
        origin.runtime_bundle_identity_sha256 != origin_runtime_sha256
        or target.runtime_bundle_identity_sha256 != target_runtime_sha256
    ):
        raise ValueError("process profiles do not bind the recovery origin/target runtimes")
    if target.result_storage_policy_sha256 is None:
        raise ValueError("a qualified result runtime binds a result storage policy")
    moved = {
        name for name in MineruProcessProfile.__dataclass_fields__
        if getattr(origin, name) != getattr(target, name)
    }
    if moved - QUALIFIED_PROCESS_PROFILE_CHANGE_FIELDS:
        raise ValueError(
            "target process profile differs from the recovery origin outside the result-storage fields: "
            + ", ".join(sorted(moved - QUALIFIED_PROCESS_PROFILE_CHANGE_FIELDS))
        )


@dataclass(frozen=True, slots=True)
class LocalExecutionUpgradeReview:
    proposal_sha256: str
    reviewer_reference: str
    decision_reference: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_version": REVIEW_CONTRACT, "verdict": "GO",
            "proposal_sha256": self.proposal_sha256,
            "reviewer_reference": self.reviewer_reference,
            "decision_reference": self.decision_reference,
        }


def decode_local_execution_upgrade_review(payload: bytes) -> LocalExecutionUpgradeReview:
    value = load_closed_object(payload, label="local execution upgrade review", maximum_bytes=MAX_REVIEW_BYTES)
    require_fields(value, {
        "contract_version", "verdict", "proposal_sha256", "reviewer_reference", "decision_reference",
    }, label="local execution upgrade review")
    if value["contract_version"] != REVIEW_CONTRACT or value["verdict"] != "GO":
        raise ValueError("local execution upgrade review is not a GO review of this contract")
    return LocalExecutionUpgradeReview(
        proposal_sha256=require_sha256(value["proposal_sha256"], label="reviewed proposal sha256"),
        reviewer_reference=_printable(value["reviewer_reference"], label="reviewer reference", maximum=256),
        decision_reference=_printable(value["decision_reference"], label="decision reference", maximum=256),
    )


@dataclass(frozen=True, slots=True)
class ReleaseFile:
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True, slots=True)
class ExecutionReleaseManifest:
    """E1: exact bytes of every file the worker can load, plus its packages."""

    source_revision: str
    writer_code_sha256: str
    worker_python_version: str
    worker_package_set_sha256: str
    files: tuple[ReleaseFile, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_version": RELEASE_MANIFEST_CONTRACT,
            "source_revision": self.source_revision,
            "writer_code_sha256": self.writer_code_sha256,
            "worker_python_version": self.worker_python_version,
            "worker_package_set_sha256": self.worker_package_set_sha256,
            "files": [{"path": item.path, "sha256": item.sha256, "bytes": item.bytes} for item in self.files],
        }


def decode_execution_release_manifest(payload: bytes) -> ExecutionReleaseManifest:
    value = load_closed_object(payload, label="execution release manifest",
                               maximum_bytes=MAX_RELEASE_MANIFEST_BYTES)
    require_fields(value, {
        "contract_version", "source_revision", "writer_code_sha256", "worker_python_version",
        "worker_package_set_sha256", "files",
    }, label="execution release manifest")
    if value["contract_version"] != RELEASE_MANIFEST_CONTRACT:
        raise ValueError("execution release manifest contract is unsupported")
    raw_files = value["files"]
    if type(raw_files) is not list or not 1 <= len(raw_files) <= _MAX_RELEASE_FILES:
        raise ValueError("execution release manifest files are outside the closed envelope")
    files: list[ReleaseFile] = []
    for index, item in enumerate(raw_files):
        if type(item) is not dict:
            raise ValueError(f"execution release file {index} must be an object")
        require_fields(item, {"path", "sha256", "bytes"}, label=f"execution release file {index}")
        files.append(ReleaseFile(
            path=_relative_path(item["path"], label=f"execution release file {index} path"),
            sha256=require_sha256(item["sha256"], label=f"execution release file {index} sha256"),
            bytes=require_int(item["bytes"], label=f"execution release file {index} bytes", minimum=0),
        ))
    paths = [item.path for item in files]
    if paths != sorted(paths) or len(set(paths)) != len(paths):
        raise ValueError("execution release files must be unique and sorted by path")
    return ExecutionReleaseManifest(
        source_revision=_printable(value["source_revision"], label="source revision", maximum=128),
        writer_code_sha256=require_sha256(value["writer_code_sha256"], label="release writer sha256"),
        worker_python_version=_printable(value["worker_python_version"], label="worker python", maximum=64),
        worker_package_set_sha256=require_sha256(
            value["worker_package_set_sha256"], label="worker package set sha256",
        ),
        files=tuple(files),
    )


def encode_execution_release_manifest(manifest: ExecutionReleaseManifest) -> bytes:
    encoded = canonical_bytes(manifest.to_payload())
    if decode_execution_release_manifest(encoded) != manifest:
        raise ValueError("execution release manifest does not round-trip")
    return encoded


@dataclass(frozen=True, slots=True)
class LegacyScopeMember:
    """One unresolved obligation as observed at capture; never a mutable cursor."""

    attempt_id: str
    document_id: str
    processing_run_id: str
    attempt_generation: int
    fence_identity: str
    h0_checkpoint_sha256: str
    execution_spec_sha256: str
    source_pdf_sha256: str
    parser_target_sha256: str
    request_sha256: str
    runtime_epoch_sha256: str
    client_submit_key: str
    submission_epoch_unix: int
    process_profile_sha256: str
    worker_profile_sha256: str
    observed_state: str
    observed_lifecycle_version: int
    observed_checkpoint_sha256: str
    accepted_submission_sha256: str | None


_MEMBER_SHA_FIELDS = (
    "h0_checkpoint_sha256", "execution_spec_sha256", "source_pdf_sha256", "parser_target_sha256",
    "request_sha256", "runtime_epoch_sha256", "process_profile_sha256", "worker_profile_sha256",
    "observed_checkpoint_sha256",
)
_MEMBER_TEXT_FIELDS = (
    "attempt_id", "document_id", "processing_run_id", "fence_identity", "client_submit_key",
)


def _decode_member(value: object, *, index: int) -> LegacyScopeMember:
    label = f"legacy member {index}"
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    require_fields(value, set(LegacyScopeMember.__dataclass_fields__), label=label)
    state = value["observed_state"]
    if state not in _UNRESOLVED_STATES:
        raise ValueError(f"{label} observed state is not an unresolved responsibility")
    accepted = value["accepted_submission_sha256"]
    return LegacyScopeMember(
        **{name: _identifier(value[name], label=f"{label} {name}") for name in _MEMBER_TEXT_FIELDS},
        **{name: require_sha256(value[name], label=f"{label} {name}") for name in _MEMBER_SHA_FIELDS},
        attempt_generation=require_int(value["attempt_generation"], label=f"{label} generation", minimum=0),
        submission_epoch_unix=require_int(value["submission_epoch_unix"], label=f"{label} epoch", minimum=0),
        observed_state=state,
        observed_lifecycle_version=require_int(
            value["observed_lifecycle_version"], label=f"{label} lifecycle", minimum=0,
        ),
        accepted_submission_sha256=(
            None if accepted is None else require_sha256(accepted, label=f"{label} accepted sha256")
        ),
    )


@dataclass(frozen=True, slots=True)
class LegacyScopeInventory:
    captured_at_utc: str
    members: tuple[LegacyScopeMember, ...]

    def __post_init__(self) -> None:
        identities = [item.attempt_id for item in self.members]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError("legacy inventory members must be unique and sorted by attempt")
        for name in ("fence_identity", "client_submit_key", "h0_checkpoint_sha256", "execution_spec_sha256"):
            values = [getattr(item, name) for item in self.members]
            if len(set(values)) != len(values):
                raise ValueError(f"legacy inventory repeats a member {name}")

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_version": INVENTORY_CONTRACT,
            "captured_at_utc": self.captured_at_utc,
            "member_count": len(self.members),
            "members": [_dataclass_payload(item) for item in self.members],
        }


def decode_legacy_scope_inventory(payload: bytes) -> LegacyScopeInventory:
    value = load_closed_object(payload, label="legacy scope inventory", maximum_bytes=MAX_INVENTORY_BYTES)
    require_fields(value, {"contract_version", "captured_at_utc", "member_count", "members"},
                   label="legacy scope inventory")
    if value["contract_version"] != INVENTORY_CONTRACT:
        raise ValueError("legacy scope inventory contract is unsupported")
    members = value["members"]
    if type(members) is not list or len(members) > _MAX_MEMBERS:
        raise ValueError("legacy scope inventory members are outside the closed envelope")
    if require_int(value["member_count"], label="legacy member count", minimum=0, maximum=_MAX_MEMBERS) != len(
        members
    ):
        raise ValueError("legacy scope inventory member count disagrees with its members")
    return LegacyScopeInventory(
        captured_at_utc=_utc_timestamp(value["captured_at_utc"], label="inventory captured_at_utc"),
        members=tuple(_decode_member(item, index=index) for index, item in enumerate(members)),
    )


def encode_legacy_scope_inventory(inventory: LegacyScopeInventory) -> bytes:
    encoded = canonical_bytes(inventory.to_payload())
    if decode_legacy_scope_inventory(encoded) != inventory:
        raise ValueError("legacy scope inventory does not round-trip")
    return encoded


def require_same_computation(
    parent_manifest: object,
    current_manifest: object,
    *,
    parent_writer_sha256: str,
    current_writer_sha256: str,
) -> None:
    """Only ``client.writer_code_sha256`` may differ between M0 and M1."""

    if type(parent_manifest) is not dict or type(current_manifest) is not dict:
        raise ValueError("runtime manifests must be objects")
    parent_client, current_client = parent_manifest.get("client"), current_manifest.get("client")
    if type(parent_client) is not dict or type(current_client) is not dict:
        raise ValueError("runtime manifest client sections must be objects")
    if (
        parent_client.get("writer_code_sha256") != parent_writer_sha256
        or current_client.get("writer_code_sha256") != current_writer_sha256
        or parent_writer_sha256 == current_writer_sha256
    ):
        raise ValueError("runtime manifests do not carry the upgrade's exact parent/current writers")
    if (
        {key: item for key, item in parent_manifest.items() if key != "client"}
        != {key: item for key, item in current_manifest.items() if key != "client"}
        or {key: item for key, item in parent_client.items() if key != "writer_code_sha256"}
        != {key: item for key, item in current_client.items() if key != "writer_code_sha256"}
    ):
        raise ValueError(
            "current runtime differs from the parent beyond the local writer "
            "(model, image, packages, topology, capacity or commands changed)"
        )


def require_computation_invariance(
    reference_manifest: object,
    target_manifest: object,
    *,
    reference_writer_sha256: str,
    target_writer_sha256: str,
    reference: str,
) -> None:
    """v2: the target manifest equals the reference except its local writer.

    Unlike the v1 edge the writer may also stay: a release can change without
    changing the fingerprinted writer, and no identity is forced to move.
    """

    if type(reference_manifest) is not dict or type(target_manifest) is not dict:
        raise ValueError("runtime manifests must be objects")
    reference_client, target_client = reference_manifest.get("client"), target_manifest.get("client")
    if type(reference_client) is not dict or type(target_client) is not dict:
        raise ValueError("runtime manifest client sections must be objects")
    if (
        reference_client.get("writer_code_sha256") != reference_writer_sha256
        or target_client.get("writer_code_sha256") != target_writer_sha256
    ):
        raise ValueError(f"runtime manifests do not carry the exact {reference} and target writers")
    if (
        {key: item for key, item in reference_manifest.items() if key != "client"}
        != {key: item for key, item in target_manifest.items() if key != "client"}
        or {key: item for key, item in reference_client.items() if key != "writer_code_sha256"}
        != {key: item for key, item in target_client.items() if key != "writer_code_sha256"}
    ):
        raise ValueError(
            f"target runtime differs from the {reference} beyond the local writer "
            "(model, image, packages, topology, capacity or commands changed)"
        )


def require_process_profile_mapping(
    parent: MineruProcessProfile,
    current: MineruProcessProfile,
    *,
    parent_runtime_sha256: str,
    current_runtime_sha256: str,
    reference: str = "parent",
    target: str = "current",
) -> None:
    """P1 is P0 with only the runtime reference moved from R0 to R1.

    v2 applies the same relation to anchor -> target and origin -> target.
    """

    if type(parent) is not MineruProcessProfile or type(current) is not MineruProcessProfile:
        raise ValueError("process profiles must be exact")
    if (
        parent.runtime_bundle_identity_sha256 != parent_runtime_sha256
        or current.runtime_bundle_identity_sha256 != current_runtime_sha256
    ):
        raise ValueError(f"process profiles do not bind the upgrade's {reference}/{target} runtime")
    if replace(parent, runtime_bundle_identity_sha256=current_runtime_sha256) != current:
        raise ValueError(
            f"{target} process profile changes a physical ceiling; only the runtime reference may move"
        )


def derive_parent_worker_profile(
    current: StagedWorkerProfileV4, *, parent_process_profile_sha256: str,
) -> StagedWorkerProfileV4:
    """WP0 is WP1 with only the process-profile reference moved back to P0."""

    if type(current) is not StagedWorkerProfileV4:
        raise ValueError("worker profile must be exact")
    return replace(current, process_profile_sha256=parent_process_profile_sha256)


def require_activation_mapping(
    parent: object,
    current: object,
    *,
    parent_runtime_sha256: str,
    current_runtime_sha256: str,
    reference: str = "parent",
    target: str = "current",
) -> None:
    """A1 is A0 with only its two runtime references moved from R0 to R1.

    v2 applies the same relation to anchor -> target and origin -> target.
    """

    if type(parent) is not dict or type(current) is not dict:
        raise ValueError("stream activations must be objects")
    parent_policy, current_policy = parent.get("policy"), current.get("policy")
    if type(parent_policy) is not dict or type(current_policy) is not dict:
        raise ValueError("stream activation policies must be objects")
    if (
        parent.get("runtime_identity_sha256") != parent_runtime_sha256
        or parent_policy.get("runtime_identity_sha256") != parent_runtime_sha256
        or current.get("runtime_identity_sha256") != current_runtime_sha256
        or current_policy.get("runtime_identity_sha256") != current_runtime_sha256
    ):
        raise ValueError(f"stream activations do not bind the upgrade's {reference}/{target} runtime")

    def normalized(activation: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        rest = {key: item for key, item in activation.items() if key not in {"runtime_identity_sha256", "policy"}}
        rest["policy"] = {key: item for key, item in policy.items() if key != "runtime_identity_sha256"}
        return rest

    if normalized(parent, parent_policy) != normalized(current, current_policy):
        raise ValueError(
            f"{target} stream activation changes owner, cgroup, GPU, ages or policy; only the runtime may move"
        )


@dataclass(frozen=True, slots=True)
class VerifiedLegacyExecutionAuthorization:
    """Non-persistent proof that one exact legacy POST belongs to the edge.

    Issued only by the resolver through ``VerifiedQualifiedExecution`` after
    the full H0/spec closure, and checked again at the POST boundary against
    the command and the issuing context. It never rewrites the request, key or
    runtime of the original attempt.

    ``parent_*`` keep their v1 names and always carry the obligation's own
    frozen execution: the v1 parent, or the v2 recovery origin. A v2
    qualification anchor never appears in a proof.
    """

    attempt_id: str
    fence_identity: str
    h0_checkpoint_sha256: str
    execution_spec_sha256: str
    submission_intent_sha256: str
    source_pdf_sha256: str
    request_sha256: str
    client_submit_key: str
    parent_runtime_identity_sha256: str
    parent_process_profile_sha256: str
    active_runtime_identity_sha256: str
    upgrade_sha256: str
    issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class VerifiedQualifiedExecution:
    """The one verified U01 relation shared by every boundary.

    v1 relates Q0/E1/H0; its legacy members are bound to Q0 itself. v2
    relates the Q0 anchor, one recovery origin and the target; its members
    are bound to the recovery origin. The resolver, the POST boundary, the
    composition and the new-H0 hold consume this one object in both versions.
    It has no business writes. It is produced only by the deployment gate's
    compatibility branch after every file, byte, profile, activation,
    qualification, origin and inventory check has passed.
    """

    upgrade: AnyExecutionUpgrade
    upgrade_sha256: str
    review: LocalExecutionUpgradeReview
    review_sha256: str
    inventory: LegacyScopeInventory
    # The canary time of the qualification in force: Q0's original time for
    # v1/v2 (the v1 name is kept), Qnew's own time for a qualified upgrade.
    parent_qualified_at: datetime
    _members: Mapping[str, LegacyScopeMember] = field(init=False, repr=False, compare=False)
    _issuer: object = field(init=False, repr=False, compare=False)

    @property
    def qualification_origin(self) -> QualificationOrigin:
        """``exact`` when the target carries its own new qualification."""

        return "exact" if isinstance(self.upgrade, QualifiedRuntimeUpgrade) else "compatible_parent"

    def __post_init__(self) -> None:
        if (
            type(self.upgrade) not in (LocalExecutionUpgrade, LocalExecutionUpgradeV2, QualifiedRuntimeUpgrade)
            or type(self.inventory) is not LegacyScopeInventory
        ):
            raise ValueError("verified execution requires exact upgrade and inventory contracts")
        if self.review.proposal_sha256 != self.upgrade_sha256:
            raise ValueError("verified execution review does not approve this proposal")
        if self.upgrade.legacy_scope.member_count != len(self.inventory.members):
            raise ValueError("verified execution inventory disagrees with the proposal")
        if isinstance(self.upgrade, QualifiedRuntimeUpgrade) and any(
            member.observed_state != "prepared" or member.accepted_submission_sha256 is not None
            for member in self.inventory.members
        ):
            # Anything past prepared may have been accepted or be an unknown
            # POST: it stays with its own runtime, never moves to Qnew.
            raise ValueError("a qualified runtime upgrade carries only never-submitted prepared obligations")
        bound = self._member_execution()
        for member in self.inventory.members:
            if (member.runtime_epoch_sha256, member.process_profile_sha256, member.worker_profile_sha256) != bound:
                raise ValueError(
                    f"legacy member {member.attempt_id} is not bound to the {self._member_pair_label()}"
                )
        object.__setattr__(self, "_members", {item.attempt_id: item for item in self.inventory.members})
        object.__setattr__(self, "_issuer", object())

    @property
    def upgrade_contract_version(self) -> str:
        if isinstance(self.upgrade, QualifiedRuntimeUpgrade):
            return QUALIFIED_UPGRADE_CONTRACT
        return UPGRADE_CONTRACT_V2 if isinstance(self.upgrade, LocalExecutionUpgradeV2) else UPGRADE_CONTRACT

    @property
    def qualification_anchor(self) -> ParentQualification:
        """The qualification in force: v1 parent, v2 anchor Q0, or the target's Qnew."""

        upgrade = self.upgrade
        if isinstance(upgrade, QualifiedRuntimeUpgrade):
            return upgrade.target_qualification
        return upgrade.qualification_anchor if isinstance(upgrade, LocalExecutionUpgradeV2) else upgrade.parent

    @property
    def recovery_origin(self) -> RecoveryOrigin | None:
        """The separately archived origin; None for v1, whose origin is Q0."""

        upgrade = self.upgrade
        return None if isinstance(upgrade, LocalExecutionUpgrade) else upgrade.recovery_origin

    def _member_execution(self) -> tuple[str, str, str]:
        upgrade = self.upgrade
        bound: ParentQualification | RecoveryOrigin = (
            upgrade.parent if isinstance(upgrade, LocalExecutionUpgrade) else upgrade.recovery_origin
        )
        return bound.runtime_identity_sha256, bound.process_profile_sha256, bound.worker_profile_sha256

    def _member_pair_label(self) -> str:
        if isinstance(self.upgrade, LocalExecutionUpgrade):
            return "single verified parent profile pair"
        return "verified recovery origin profile pair"

    @property
    def member_runtime_identity_sha256(self) -> str:
        """The runtime every legacy member was frozen under (v1 R0, v2 origin)."""

        return self._member_execution()[0]

    @property
    def member_process_profile_sha256(self) -> str:
        return self._member_execution()[1]

    @property
    def member_worker_profile_sha256(self) -> str:
        return self._member_execution()[2]

    @property
    def parent_runtime_identity_sha256(self) -> str:
        """The v1 name of ``member_runtime_identity_sha256``; never a v2 anchor."""

        return self.member_runtime_identity_sha256

    @property
    def current_runtime_identity_sha256(self) -> str:
        return self.upgrade.current.runtime_identity_sha256

    @property
    def member_attempt_ids(self) -> tuple[str, ...]:
        return tuple(item.attempt_id for item in self.inventory.members)

    def member(self, attempt_id: str) -> LegacyScopeMember:
        member = self._members.get(attempt_id)
        if member is None:
            raise LegacyExecutionRefused(
                f"attempt {attempt_id} is not an obligation of the verified legacy scope"
            )
        return member

    def is_member(self, attempt_id: str) -> bool:
        return attempt_id in self._members

    def require_current_execution(self, authority: RemoteParseV4Authority) -> None:
        """A head outside the inventory must be exactly the current composition's work.

        Such heads exist only after legacy closure (new H0 under the current
        runtime and profiles); they are ordinary exact-path work, never a
        legacy continuation.
        """

        spec = authority.execution_spec
        current = self.upgrade.current
        if spec is None or (
            spec.worker_profile.sha256, spec.process_profile_sha256,
            spec.prepared_submission.runtime_bundle_identity_sha256,
            spec.parser_options.runtime_bundle_identity_sha256,
        ) != (
            current.worker_profile_sha256, current.process_profile_sha256,
            current.runtime_identity_sha256, current.runtime_identity_sha256,
        ):
            raise LegacyExecutionRefused(
                f"attempt {authority.attempt_id} is neither a verified legacy obligation "
                "nor bound to the verified current execution"
            )

    def require_active_worker_profile(self, active: StagedWorkerProfileV4) -> None:
        if type(active) is not StagedWorkerProfileV4 or active.sha256 != self.upgrade.current.worker_profile_sha256:
            raise LegacyExecutionRefused("active worker composition is not the verified current execution")

    def require_legacy_execution(
        self,
        authority: RemoteParseV4Authority,
        spec: V4PreparedExecutionSpec,
        *,
        active_worker_profile: StagedWorkerProfileV4,
    ) -> LegacyScopeMember:
        """Allow one listed obligation to run on the target; every H0 fact stays exact."""

        self.require_active_worker_profile(active_worker_profile)
        return self._require_member(authority, spec)

    def require_member_progress(self, authority: RemoteParseV4Authority) -> LegacyScopeMember:
        """The read-only scope check of one member against its capture."""

        spec = authority.execution_spec
        if spec is None:
            raise LegacyExecutionRefused(f"legacy obligation {authority.attempt_id} lacks its execution spec")
        return self._require_member(authority, spec)

    def _require_member(
        self, authority: RemoteParseV4Authority, spec: V4PreparedExecutionSpec,
    ) -> LegacyScopeMember:
        runtime, process_profile, worker_profile = self._member_execution()
        if (
            spec.worker_profile.sha256 != worker_profile
            or spec.process_profile_sha256 != process_profile
            or spec.prepared_submission.runtime_bundle_identity_sha256 != runtime
            or spec.parser_options.runtime_bundle_identity_sha256 != runtime
        ):
            pair = (
                "verified parent profile pair"
                if isinstance(self.upgrade, LocalExecutionUpgrade)
                else "verified recovery origin profile pair"
            )
            raise LegacyExecutionRefused(f"execution spec is not bound to the {pair}")
        member = self.member(authority.attempt_id)
        history = authority.checkpoint_history
        prepared = spec.prepared_submission
        observed = (
            authority.document_id, authority.processing_run_id, authority.attempt_generation,
            authority.fence_identity, authority.source_pdf_sha256, authority.parser_target_sha256,
            authority.request_sha256, authority.runtime_epoch_sha256, authority.client_submit_key,
            prepared.submission_epoch_unix, spec.sha256, history[0].sha256,
        )
        expected = (
            member.document_id, member.processing_run_id, member.attempt_generation,
            member.fence_identity, member.source_pdf_sha256, member.parser_target_sha256,
            member.request_sha256, member.runtime_epoch_sha256, member.client_submit_key,
            member.submission_epoch_unix, member.execution_spec_sha256, member.h0_checkpoint_sha256,
        )
        if observed != expected:
            raise LegacyExecutionRefused(
                f"legacy obligation {member.attempt_id} identity drifted from the verified inventory"
            )
        observed_version = member.observed_lifecycle_version
        prefix = history[: observed_version + 1]
        if (
            authority.lifecycle_version < observed_version
            or len(prefix) != observed_version + 1
            or prefix[-1].sha256 != member.observed_checkpoint_sha256
            or any(item.lifecycle_version != index for index, item in enumerate(prefix))
            or any(
                prefix[index].previous_checkpoint_sha256 != prefix[index - 1].sha256
                for index in range(1, len(prefix))
            )
        ):
            raise LegacyExecutionRefused(
                f"legacy obligation {member.attempt_id} history is not a monotonic continuation of its capture"
            )
        return member

    def authorize_submission(
        self,
        authority: RemoteParseV4Authority,
        spec: V4PreparedExecutionSpec,
        intent: SubmissionIntentV4,
        *,
        active_worker_profile: StagedWorkerProfileV4,
    ) -> VerifiedLegacyExecutionAuthorization:
        member = self.require_legacy_execution(authority, spec, active_worker_profile=active_worker_profile)
        if (
            intent.attempt_id, intent.fence_identity, intent.source_pdf_sha256, intent.request_sha256,
            intent.runtime_epoch_sha256, intent.client_submit_key, intent.submission_epoch_unix,
        ) != (
            member.attempt_id, member.fence_identity, member.source_pdf_sha256, member.request_sha256,
            member.runtime_epoch_sha256, member.client_submit_key, member.submission_epoch_unix,
        ):
            raise LegacyExecutionRefused(
                f"legacy obligation {member.attempt_id} submission intent is not its original request/key"
            )
        return VerifiedLegacyExecutionAuthorization(
            attempt_id=member.attempt_id,
            fence_identity=member.fence_identity,
            h0_checkpoint_sha256=member.h0_checkpoint_sha256,
            execution_spec_sha256=member.execution_spec_sha256,
            submission_intent_sha256=intent.sha256,
            source_pdf_sha256=member.source_pdf_sha256,
            request_sha256=member.request_sha256,
            client_submit_key=member.client_submit_key,
            parent_runtime_identity_sha256=self.member_runtime_identity_sha256,
            parent_process_profile_sha256=self.member_process_profile_sha256,
            active_runtime_identity_sha256=self.current_runtime_identity_sha256,
            upgrade_sha256=self.upgrade_sha256,
            issuer=self._issuer,
        )

    def require_submission(
        self,
        command: RemoteSubmissionCommandV4,
        authorization: VerifiedLegacyExecutionAuthorization,
        *,
        now_unix: float | None = None,
    ) -> str:
        """Check the POST command against its proof; return the active (current) runtime.

        A qualified upgrade approved each original key only inside its provider
        key lifetime. ``now_unix`` is the transport's wall clock at the moment
        the POST would leave: after a long wait the key may have expired, and
        an expired key's 404 no longer proves the task was never accepted, so
        it is refused here, before any byte is sent. Reconciling a task the
        lookup finds never reaches this check.
        """

        if (
            type(authorization) is not VerifiedLegacyExecutionAuthorization
            or authorization.issuer is not self._issuer
            or authorization.upgrade_sha256 != self.upgrade_sha256
            or authorization.active_runtime_identity_sha256 != self.current_runtime_identity_sha256
            or authorization.parent_runtime_identity_sha256 != self.member_runtime_identity_sha256
        ):
            raise LegacyExecutionRefused("legacy submission proof was not issued by this verified execution")
        member = self.member(authorization.attempt_id)
        if (
            authorization.fence_identity, authorization.h0_checkpoint_sha256,
            authorization.execution_spec_sha256, authorization.source_pdf_sha256,
            authorization.request_sha256, authorization.client_submit_key,
            authorization.parent_process_profile_sha256,
        ) != (
            member.fence_identity, member.h0_checkpoint_sha256, member.execution_spec_sha256,
            member.source_pdf_sha256, member.request_sha256, member.client_submit_key,
            self.member_process_profile_sha256,
        ):
            raise LegacyExecutionRefused(
                f"legacy submission proof for {authorization.attempt_id} is not its verified obligation"
            )
        intent = command.submission_intent
        observed = (
            intent.sha256, intent.attempt_id, intent.fence_identity, intent.source_pdf_sha256,
            intent.request_sha256, intent.runtime_epoch_sha256, intent.client_submit_key,
            "sha256:" + hashlib.sha256(command.request_exact_bytes).hexdigest(),
            command.parser_options.runtime_bundle_identity_sha256,
        )
        expected = (
            authorization.submission_intent_sha256, authorization.attempt_id,
            authorization.fence_identity, authorization.source_pdf_sha256,
            authorization.request_sha256, authorization.parent_runtime_identity_sha256,
            authorization.client_submit_key, authorization.request_sha256,
            authorization.parent_runtime_identity_sha256,
        )
        if observed != expected:
            raise LegacyExecutionRefused(
                f"legacy submission command drifted from its proof for {authorization.attempt_id}"
            )
        if isinstance(self.upgrade, QualifiedRuntimeUpgrade):
            if isinstance(now_unix, bool) or not isinstance(now_unix, (int, float)) or not isfinite(now_unix):
                raise LegacyExecutionRefused(
                    f"legacy submission of {member.attempt_id} lacks the POST-boundary wall clock"
                )
            if now_unix - member.submission_epoch_unix >= self.upgrade.key_lookups.key_ttl_seconds:
                raise LegacyExecutionRefused(
                    f"original key of {member.attempt_id} expired before its POST; "
                    "an expired key is never submitted again"
                )
        return self.current_runtime_identity_sha256

    def summary(self) -> dict[str, Any]:
        """Identities only: safe for logs, preflight JSON, boot receipts and doctor.

        v1 keeps its historical ``parent_*`` keys. v2 names each role exactly:
        ``anchor_*`` (Q0), ``origin_*`` (the members' execution) and the
        unprefixed current keys (the target).
        """

        upgrade = self.upgrade
        if isinstance(upgrade, QualifiedRuntimeUpgrade):
            qualification, origin, target = upgrade.target_qualification, upgrade.recovery_origin, upgrade.current
            return {
                "upgrade_contract_version": QUALIFIED_UPGRADE_CONTRACT,
                "qualification_origin": self.qualification_origin,
                "upgrade_sha256": self.upgrade_sha256,
                "review_sha256": self.review_sha256,
                "qualification_runtime_identity_sha256": qualification.runtime_identity_sha256,
                "qualification_smoke_receipt_sha256": qualification.smoke_receipt_sha256,
                "qualification_canary_cache_sha256": qualification.canary_cache_sha256,
                "qualification_validation_receipt_sha256": qualification.validation_receipt_sha256,
                "qualification_qualified_at_utc": qualification.qualified_at_utc,
                "origin_release_manifest_sha256": origin.release_manifest_sha256,
                "origin_source_revision": origin.source_revision,
                "origin_writer_code_sha256": origin.writer_code_sha256,
                "origin_runtime_identity_sha256": origin.runtime_identity_sha256,
                "origin_process_profile_sha256": origin.process_profile_sha256,
                "origin_worker_profile_sha256": origin.worker_profile_sha256,
                "origin_capacity_config_sha256": origin.capacity_config_sha256,
                "origin_stream_activation_sha256": origin.stream_activation_sha256,
                "release_manifest_sha256": target.release_manifest_sha256,
                "source_revision": target.source_revision,
                "writer_code_sha256": target.writer_code_sha256,
                "current_runtime_identity_sha256": target.runtime_identity_sha256,
                "process_profile_sha256": target.process_profile_sha256,
                "worker_profile_sha256": target.worker_profile_sha256,
                "stream_activation_sha256": target.stream_activation_sha256,
                "capacity_config_sha256": target.capacity_config_sha256,
                "runtime_changes": [".".join(item) for item in upgrade.runtime_changes],
                "key_lookup_evidence_sha256": upgrade.key_lookups.evidence_sha256,
                "key_ttl_seconds": upgrade.key_lookups.key_ttl_seconds,
                "legacy_inventory_sha256": upgrade.legacy_scope.inventory_sha256,
                "legacy_member_count": len(self.inventory.members),
            }
        if isinstance(upgrade, LocalExecutionUpgradeV2):
            anchor, origin, target = upgrade.qualification_anchor, upgrade.recovery_origin, upgrade.current
            return {
                "upgrade_contract_version": UPGRADE_CONTRACT_V2,
                "qualification_origin": self.qualification_origin,
                "upgrade_sha256": self.upgrade_sha256,
                "review_sha256": self.review_sha256,
                "anchor_runtime_identity_sha256": anchor.runtime_identity_sha256,
                "anchor_writer_code_sha256": anchor.writer_code_sha256,
                "anchor_process_profile_sha256": anchor.process_profile_sha256,
                "anchor_worker_profile_sha256": anchor.worker_profile_sha256,
                "anchor_stream_activation_sha256": anchor.stream_activation_sha256,
                "anchor_qualified_at_utc": anchor.qualified_at_utc,
                "origin_release_manifest_sha256": origin.release_manifest_sha256,
                "origin_source_revision": origin.source_revision,
                "origin_writer_code_sha256": origin.writer_code_sha256,
                "origin_runtime_identity_sha256": origin.runtime_identity_sha256,
                "origin_process_profile_sha256": origin.process_profile_sha256,
                "origin_worker_profile_sha256": origin.worker_profile_sha256,
                "origin_stream_activation_sha256": origin.stream_activation_sha256,
                "release_manifest_sha256": target.release_manifest_sha256,
                "source_revision": target.source_revision,
                "writer_code_sha256": target.writer_code_sha256,
                "current_runtime_identity_sha256": target.runtime_identity_sha256,
                "process_profile_sha256": target.process_profile_sha256,
                "worker_profile_sha256": target.worker_profile_sha256,
                "stream_activation_sha256": target.stream_activation_sha256,
                "capacity_config_sha256": target.capacity_config_sha256,
                "legacy_inventory_sha256": upgrade.legacy_scope.inventory_sha256,
                "legacy_member_count": len(self.inventory.members),
            }
        parent, current = upgrade.parent, upgrade.current
        return {
            "upgrade_contract_version": UPGRADE_CONTRACT,
            "qualification_origin": self.qualification_origin,
            "upgrade_sha256": self.upgrade_sha256,
            "review_sha256": self.review_sha256,
            "parent_runtime_identity_sha256": parent.runtime_identity_sha256,
            "parent_writer_code_sha256": parent.writer_code_sha256,
            "parent_process_profile_sha256": parent.process_profile_sha256,
            "parent_worker_profile_sha256": parent.worker_profile_sha256,
            "parent_qualified_at_utc": parent.qualified_at_utc,
            "release_manifest_sha256": current.release_manifest_sha256,
            "source_revision": current.source_revision,
            "writer_code_sha256": current.writer_code_sha256,
            "current_runtime_identity_sha256": current.runtime_identity_sha256,
            "process_profile_sha256": current.process_profile_sha256,
            "worker_profile_sha256": current.worker_profile_sha256,
            "stream_activation_sha256": current.stream_activation_sha256,
            "legacy_inventory_sha256": self.upgrade.legacy_scope.inventory_sha256,
            "legacy_member_count": len(self.inventory.members),
        }


__all__ = [
    "AnyExecutionUpgrade",
    "CompatibilityBasis",
    "CurrentExecution",
    "ExecutionReleaseManifest",
    "INVENTORY_CONTRACT",
    "KEY_LOOKUP_CONTRACT",
    "KeyLookupEvidenceReference",
    "LegacyKeyLookup",
    "LegacyKeyLookupEvidence",
    "MAX_KEY_LOOKUP_BYTES",
    "QUALIFIED_PROCESS_PROFILE_CHANGE_FIELDS",
    "QUALIFIED_RUNTIME_CHANGE_FIELDS",
    "QUALIFIED_TRANSITION_KIND",
    "QUALIFIED_UPGRADE_CONTRACT",
    "QualifiedRuntimeUpgrade",
    "LegacyExecutionRefused",
    "LegacyObligationsOpen",
    "LegacyScopeInventory",
    "LegacyScopeMember",
    "LegacyScopeReference",
    "LocalExecutionUpgrade",
    "LocalExecutionUpgradeReview",
    "LocalExecutionUpgradeV2",
    "ParentQualification",
    "QualificationOrigin",
    "RELEASE_MANIFEST_CONTRACT",
    "REVIEW_CONTRACT",
    "RecoveryOrigin",
    "ReleaseFile",
    "TRANSITION_KIND",
    "UPGRADE_CONTRACT",
    "UPGRADE_CONTRACT_V2",
    "VerifiedLegacyExecutionAuthorization",
    "VerifiedQualifiedExecution",
    "decode_execution_release_manifest",
    "decode_execution_upgrade",
    "decode_legacy_key_lookup_evidence",
    "decode_qualified_runtime_upgrade",
    "encode_legacy_key_lookup_evidence",
    "encode_qualified_runtime_upgrade",
    "require_key_lookup_coverage",
    "require_result_capacity_change",
    "require_result_process_profile_change",
    "require_result_runtime_change",
    "decode_legacy_scope_inventory",
    "decode_local_execution_upgrade",
    "decode_local_execution_upgrade_review",
    "decode_local_execution_upgrade_v2",
    "derive_parent_worker_profile",
    "encode_execution_release_manifest",
    "encode_legacy_scope_inventory",
    "encode_local_execution_upgrade",
    "encode_local_execution_upgrade_v2",
    "require_activation_mapping",
    "require_computation_invariance",
    "require_process_profile_mapping",
    "require_same_computation",
]
