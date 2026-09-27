"""Managed closure of expired, never-submitted prepared V4 obligations (Pro VI.2).

A fixed legacy inventory whose original keys expired before any submission is
closed through the ordinary pre-submission failure and owned cleanup, never by
direct SQL, a new key, an altered H0 or an extended key lifetime. The proof
that no key was ever accepted is composed only of existing, auditable facts:

* supervised lookups of every original key, answered by the origin runtime's
  registry after the inventory capture and while each key was still inside
  its actual lifetime, all closed 404 (a 404 after expiry proves nothing);
* a head that is still exactly the captured, never-submitted prepared H0: a V4
  attempt makes its submission intent durable (``reconciling``) before any
  POST, so an unmoved prepared H0 was never submitted by any worker, including
  after the lookups.

Closing is conditional: a key still inside its lifetime is never closed here.
Requeue is a separate, explicit decision, never part of the closure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import TYPE_CHECKING, Any

from disclosure_anchor.application.contracts.closed_document import (
    canonical_bytes,
    load_closed_object,
    require_fields,
    require_int,
    require_sha256,
    require_str,
)
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    LegacyKeyLookupEvidence,
    LegacyScopeInventory,
    LegacyScopeMember,
    require_key_lookup_coverage,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from disclosure_anchor.application.ports.remote_parse_v4_repository import RemoteParseV4Authority

CLOSURE_PLAN_CONTRACT = "worker-expired-prepared-closure-plan.v1"
ORIGINAL_KEY_EXPIRED_ERROR_CODE = "original_key_expired"
# A closed contract class: the scheduler never retries it by itself.
ORIGINAL_KEY_LIFETIME_RETRY_CLASS = "original_key_lifetime"
MAX_CLOSURE_PLAN_BYTES = 256 * 1024
_PREPARED_EVIDENCE = frozenset({"preparation_intent", "snapshot_receipt"})


class ExpiredPreparedClosureRefused(ValueError):
    """The named obligation cannot be closed by this managed entry."""


@dataclass(frozen=True, slots=True)
class ClosureMemberV1:
    """One expired prepared obligation exactly as it is closed."""

    attempt_id: str
    document_id: str
    processing_run_id: str
    fence_identity: str
    h0_checkpoint_sha256: str
    client_submit_key: str
    submission_epoch_unix: int
    key_expired_at_unix: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "document_id": self.document_id,
            "processing_run_id": self.processing_run_id,
            "fence_identity": self.fence_identity,
            "h0_checkpoint_sha256": self.h0_checkpoint_sha256,
            "client_submit_key": self.client_submit_key,
            "submission_epoch_unix": self.submission_epoch_unix,
            "key_expired_at_unix": self.key_expired_at_unix,
        }


@dataclass(frozen=True, slots=True)
class ExpiredPreparedClosurePlanV1:
    """The reviewed closure: exact inputs, exact members, nothing inferred later."""

    inventory_sha256: str
    key_lookups_sha256: str
    origin_runtime_identity_sha256: str
    key_ttl_seconds: int
    planned_at_utc: str
    members: tuple[ClosureMemberV1, ...]
    # Obligations whose original key is still valid: never closed here.
    still_valid: tuple[tuple[str, int], ...]
    contract_version: str = CLOSURE_PLAN_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != CLOSURE_PLAN_CONTRACT:
            raise ValueError("expired prepared closure plan contract is unsupported")
        for value, label in (
            (self.inventory_sha256, "closure inventory"),
            (self.key_lookups_sha256, "closure key lookups"),
            (self.origin_runtime_identity_sha256, "closure origin runtime"),
        ):
            require_sha256(value, label=label)
        require_int(self.key_ttl_seconds, label="closure key lifetime")
        _utc(self.planned_at_utc, label="closure plan time")
        identities = [item.attempt_id for item in self.members]
        if not identities or identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError("closure members must be a non-empty, sorted, unique set")
        if any(
            item.key_expired_at_unix != item.submission_epoch_unix + self.key_ttl_seconds
            for item in self.members
        ):
            raise ValueError("closure member expiry is not its epoch plus the actual lifetime")
        if set(identities) & {attempt for attempt, _ in self.still_valid}:
            raise ValueError("a closure member cannot also be still valid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "inventory_sha256": self.inventory_sha256,
            "key_lookups_sha256": self.key_lookups_sha256,
            "origin_runtime_identity_sha256": self.origin_runtime_identity_sha256,
            "key_ttl_seconds": self.key_ttl_seconds,
            "planned_at_utc": self.planned_at_utc,
            "members": [item.to_payload() for item in self.members],
            "still_valid": [
                {"attempt_id": attempt, "key_expires_at_unix": expires} for attempt, expires in self.still_valid
            ],
        }

    @property
    def exact_bytes(self) -> bytes:
        return canonical_bytes(self.to_payload())

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.exact_bytes).hexdigest()


def decode_expired_prepared_closure_plan(payload: bytes) -> ExpiredPreparedClosurePlanV1:
    value = load_closed_object(payload, label="expired prepared closure plan", maximum_bytes=MAX_CLOSURE_PLAN_BYTES)
    require_fields(value, {
        "contract_version", "inventory_sha256", "key_lookups_sha256", "origin_runtime_identity_sha256",
        "key_ttl_seconds", "planned_at_utc", "members", "still_valid",
    }, label="expired prepared closure plan")
    members = value["members"]
    still_valid = value["still_valid"]
    if type(members) is not list or type(still_valid) is not list:
        raise ValueError("closure plan member lists must be arrays")
    decoded_members = []
    for index, item in enumerate(members):
        label = f"closure member {index}"
        if type(item) is not dict:
            raise ValueError(f"{label} must be an object")
        require_fields(item, set(ClosureMemberV1.__dataclass_fields__), label=label)
        decoded_members.append(ClosureMemberV1(
            attempt_id=require_str(item["attempt_id"], label=f"{label} attempt", maximum=128),
            document_id=require_str(item["document_id"], label=f"{label} document", maximum=128),
            processing_run_id=require_str(item["processing_run_id"], label=f"{label} run", maximum=128),
            fence_identity=require_str(item["fence_identity"], label=f"{label} fence", maximum=256),
            h0_checkpoint_sha256=require_sha256(item["h0_checkpoint_sha256"], label=f"{label} H0"),
            client_submit_key=require_str(item["client_submit_key"], label=f"{label} key", maximum=256),
            submission_epoch_unix=require_int(item["submission_epoch_unix"], label=f"{label} epoch", minimum=0),
            key_expired_at_unix=require_int(item["key_expired_at_unix"], label=f"{label} expiry", minimum=0),
        ))
    decoded_valid = []
    for index, item in enumerate(still_valid):
        label = f"still-valid member {index}"
        if type(item) is not dict:
            raise ValueError(f"{label} must be an object")
        require_fields(item, {"attempt_id", "key_expires_at_unix"}, label=label)
        decoded_valid.append((
            require_str(item["attempt_id"], label=f"{label} attempt", maximum=128),
            require_int(item["key_expires_at_unix"], label=f"{label} expiry", minimum=0),
        ))
    plan = ExpiredPreparedClosurePlanV1(
        inventory_sha256=value["inventory_sha256"],
        key_lookups_sha256=value["key_lookups_sha256"],
        origin_runtime_identity_sha256=value["origin_runtime_identity_sha256"],
        key_ttl_seconds=value["key_ttl_seconds"],
        planned_at_utc=value["planned_at_utc"],
        members=tuple(decoded_members),
        still_valid=tuple(decoded_valid),
        contract_version=value["contract_version"],
    )
    if plan.exact_bytes != payload:
        raise ValueError("expired prepared closure plan is not canonical")
    return plan


def require_frozen_prepared_member(authority: RemoteParseV4Authority, member: LegacyScopeMember) -> None:
    """The head is still exactly the member's captured, never-submitted prepared H0."""

    attempt = member.attempt_id
    if (
        member.observed_state != "prepared"
        or member.accepted_submission_sha256 is not None
        or member.observed_checkpoint_sha256 != member.h0_checkpoint_sha256
    ):
        raise ExpiredPreparedClosureRefused(f"{attempt} was not captured as a never-submitted prepared H0")
    history = authority.checkpoint_history
    if (
        authority.attempt_id != attempt
        or authority.state != "prepared"
        or not authority.is_current
        or len(history) != 1
        or history[0].sha256 != member.h0_checkpoint_sha256
        or authority.checkpoint_sha256 != member.h0_checkpoint_sha256
        or authority.lifecycle_version != member.observed_lifecycle_version
    ):
        raise ExpiredPreparedClosureRefused(f"{attempt} is no longer exactly its captured prepared H0")
    observed = (
        authority.document_id, authority.processing_run_id, authority.attempt_generation,
        authority.fence_identity, authority.source_pdf_sha256, authority.parser_target_sha256,
        authority.request_sha256, authority.runtime_epoch_sha256, authority.client_submit_key,
    )
    expected = (
        member.document_id, member.processing_run_id, member.attempt_generation,
        member.fence_identity, member.source_pdf_sha256, member.parser_target_sha256,
        member.request_sha256, member.runtime_epoch_sha256, member.client_submit_key,
    )
    if observed != expected:
        raise ExpiredPreparedClosureRefused(f"{attempt} identity drifted from its capture")
    spec = authority.execution_spec
    if spec is None or (
        spec.sha256, spec.prepared_submission.submission_epoch_unix,
        spec.prepared_submission.client_submit_key, spec.process_profile_sha256, spec.worker_profile.sha256,
    ) != (
        member.execution_spec_sha256, member.submission_epoch_unix, member.client_submit_key,
        member.process_profile_sha256, member.worker_profile_sha256,
    ):
        raise ExpiredPreparedClosureRefused(f"{attempt} execution spec drifted from its capture")
    if any(item.kind not in _PREPARED_EVIDENCE for item in authority.evidence):
        raise ExpiredPreparedClosureRefused(f"{attempt} carries evidence past preparation")


def build_expired_prepared_closure_plan(
    *,
    inventory: LegacyScopeInventory,
    inventory_sha256: str,
    key_lookups: LegacyKeyLookupEvidence,
    key_lookups_sha256: str,
    origin_runtime_identity_sha256: str,
    key_ttl_seconds: int,
    heads: Mapping[str, RemoteParseV4Authority],
    now: datetime,
) -> ExpiredPreparedClosurePlanV1:
    """Plan the closure of every expired member; refuse unless every fact holds."""

    try:
        require_key_lookup_coverage(
            key_lookups, inventory,
            origin_runtime_identity_sha256=origin_runtime_identity_sha256, key_ttl_seconds=key_ttl_seconds,
        )
    except ValueError as exc:
        raise ExpiredPreparedClosureRefused(f"never-accepted proof is incomplete: {exc}") from exc
    now_unix = _utc(now.isoformat(), label="closure clock").timestamp()
    members: list[ClosureMemberV1] = []
    still_valid: list[tuple[str, int]] = []
    for member in inventory.members:
        if member.runtime_epoch_sha256 != origin_runtime_identity_sha256:
            raise ExpiredPreparedClosureRefused(f"{member.attempt_id} is not bound to the named origin runtime")
        head = heads.get(member.attempt_id)
        if head is None:
            raise ExpiredPreparedClosureRefused(f"{member.attempt_id} has no current head")
        require_frozen_prepared_member(head, member)
        expires = member.submission_epoch_unix + key_ttl_seconds
        if now_unix < expires:
            still_valid.append((member.attempt_id, expires))
            continue
        members.append(ClosureMemberV1(
            attempt_id=member.attempt_id,
            document_id=member.document_id,
            processing_run_id=member.processing_run_id,
            fence_identity=member.fence_identity,
            h0_checkpoint_sha256=member.h0_checkpoint_sha256,
            client_submit_key=member.client_submit_key,
            submission_epoch_unix=member.submission_epoch_unix,
            key_expired_at_unix=expires,
        ))
    if not members:
        raise ExpiredPreparedClosureRefused("no original key has expired: use the guarded original keys")
    return ExpiredPreparedClosurePlanV1(
        inventory_sha256=require_sha256(inventory_sha256, label="closure inventory"),
        key_lookups_sha256=require_sha256(key_lookups_sha256, label="closure key lookups"),
        origin_runtime_identity_sha256=origin_runtime_identity_sha256,
        key_ttl_seconds=key_ttl_seconds,
        planned_at_utc=now.astimezone(timezone.utc).isoformat(),
        members=tuple(members),
        still_valid=tuple(still_valid),
    )


def closure_decision_sha256(*, plan_sha256: str, decided_by: str, reason: str) -> str:
    """The identity every closed member's failure names."""

    return "sha256:" + hashlib.sha256(canonical_bytes({
        "contract_version": CLOSURE_PLAN_CONTRACT,
        "plan_sha256": require_sha256(plan_sha256, label="closure plan"),
        "decided_by": require_str(decided_by, label="closure decided_by", maximum=256),
        "reason": require_str(reason, label="closure reason", maximum=1024),
    })).hexdigest()


def closure_failure_message(*, plan_sha256: str, decision_sha256: str) -> str:
    return (
        "original key expired before any submission; closed by managed expired-prepared "
        f"plan {plan_sha256} decision {decision_sha256}"
    )


def _utc(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not ISO time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{label} must be UTC")
    return parsed
