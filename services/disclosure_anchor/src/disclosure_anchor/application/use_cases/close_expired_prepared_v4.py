"""Execute one reviewed expired-prepared closure plan through the ordinary V4 chain.

Members close one at a time, in plan order, each through its own durable CAS
steps: claimed with the repository's exact CAS, failed before any submission
with the typed ``original_key_expired`` cause, and closed by its owned local
cleanup (no provider task, no ACK); that member's final commit records its
failed run, document and outbox in one transaction. There is no batch
transaction: a refusal (another head, a live claim, another decision) stops
the run at that member before anything is written for it, while earlier
members stay durably closed. Re-running the same plan and decision is safe: a
closed member is reported again unchanged, and an interrupted one continues
only when its failure names exactly this decision.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import Event
from typing import TypeVar

from disclosure_anchor.application.contracts.expired_prepared_closure import (
    ORIGINAL_KEY_EXPIRED_ERROR_CODE,
    ORIGINAL_KEY_LIFETIME_RETRY_CLASS,
    ClosureMemberV1,
    ExpiredPreparedClosurePlanV1,
    ExpiredPreparedClosureRefused,
    build_expired_prepared_closure_plan,
    closure_decision_sha256,
    closure_failure_message,
    require_frozen_prepared_member,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    FailureReceiptV4,
    LocalCleanupReceiptV4,
)
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    LegacyKeyLookupEvidence,
    LegacyScopeInventory,
    LegacyScopeMember,
    require_key_lookup_coverage,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RecoveryCandidate,
    RemoteParseV4Authority,
    V4ClaimHeldByOther,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.staged_coordinator_backend_v4 import (
    DurableStagedCoordinatorBackendV4,
    ExpectedV4AttemptFailure,
)
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
)
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseGuard
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorWork,
    RecoveryDeferred,
)

_EvidenceT = TypeVar("_EvidenceT")


@dataclass(frozen=True, slots=True)
class ClosedMember:
    attempt_id: str
    document_id: str
    processing_run_id: str
    final_state: str
    failure_receipt_sha256: str
    cleanup_receipt_sha256: str
    continued_from: str


class ExpiredPreparedClosure:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        persistence: DurableStagedCoordinatorPersistenceV4,
        backend: DurableStagedCoordinatorBackendV4,
        utc_now: Callable[[], datetime],
        monotonic: Callable[[], float],
        claim_lease_seconds: int = 120,
        stage_seconds: float = 60.0,
    ) -> None:
        self._uow_factory = uow_factory
        self._persistence = persistence
        self._backend = backend
        self._utc_now = utc_now
        self._monotonic = monotonic
        self._claim_lease_seconds = claim_lease_seconds
        self._stage_seconds = stage_seconds

    def _load(self, attempt_id: str) -> RemoteParseV4Authority:
        with self._uow_factory() as uow:
            return uow.remote_parse_v4.load(attempt_id)

    def plan(
        self,
        *,
        inventory: LegacyScopeInventory,
        inventory_sha256: str,
        key_lookups: LegacyKeyLookupEvidence,
        key_lookups_sha256: str,
        origin_runtime_identity_sha256: str,
        key_ttl_seconds: int,
    ) -> ExpiredPreparedClosurePlanV1:
        """Read-only: the reviewable closure of every expired member, or a refusal."""

        heads = {member.attempt_id: self._load(member.attempt_id) for member in inventory.members}
        return build_expired_prepared_closure_plan(
            inventory=inventory, inventory_sha256=inventory_sha256,
            key_lookups=key_lookups, key_lookups_sha256=key_lookups_sha256,
            origin_runtime_identity_sha256=origin_runtime_identity_sha256,
            key_ttl_seconds=key_ttl_seconds, heads=heads, now=self._utc_now(),
        )

    def execute(
        self,
        *,
        plan: ExpiredPreparedClosurePlanV1,
        inventory: LegacyScopeInventory,
        inventory_sha256: str,
        key_lookups: LegacyKeyLookupEvidence,
        key_lookups_sha256: str,
        decided_by: str,
        reason: str,
    ) -> tuple[ClosedMember, ...]:
        if (inventory_sha256, key_lookups_sha256) != (plan.inventory_sha256, plan.key_lookups_sha256):
            raise ExpiredPreparedClosureRefused("closure inputs are not the reviewed plan's inputs")
        try:
            require_key_lookup_coverage(
                key_lookups, inventory, origin_runtime_identity_sha256=plan.origin_runtime_identity_sha256,
                key_ttl_seconds=plan.key_ttl_seconds,
            )
        except ValueError as exc:
            raise ExpiredPreparedClosureRefused(f"never-accepted proof is incomplete: {exc}") from exc
        decision = closure_decision_sha256(plan_sha256=plan.sha256, decided_by=decided_by, reason=reason)
        message = closure_failure_message(plan_sha256=plan.sha256, decision_sha256=decision)
        members = {item.attempt_id: item for item in inventory.members}
        now = self._utc_now().timestamp()
        closed = []
        for planned in plan.members:
            member = members.get(planned.attempt_id)
            if member is None or _planned_projection(member, plan.key_ttl_seconds) != planned:
                raise ExpiredPreparedClosureRefused(f"{planned.attempt_id} differs from the reviewed plan")
            if member.runtime_epoch_sha256 != plan.origin_runtime_identity_sha256:
                raise ExpiredPreparedClosureRefused(f"{planned.attempt_id} is not bound to the plan's origin")
            if now < planned.key_expired_at_unix:
                raise ExpiredPreparedClosureRefused(f"{planned.attempt_id} original key has not expired")
            closed.append(self._close(member, planned, message))
        return tuple(closed)

    def _close(self, member: LegacyScopeMember, planned: ClosureMemberV1, message: str) -> ClosedMember:
        head = self._load(planned.attempt_id)
        continued_from = head.state
        work: CoordinatorWork | None = None
        if head.state == "prepared":
            require_frozen_prepared_member(head, member)
            work = self._claim(head)
            expiry = ExpectedV4AttemptFailure(
                error_code=ORIGINAL_KEY_EXPIRED_ERROR_CODE,
                message=message,
                retry_budget_class=ORIGINAL_KEY_LIFETIME_RETRY_CLASS,
            )
            work = self._backend.close_prepared_before_submission(
                work, member=member, failure=expiry, stage_guard=self._guard(work),
            )
            head = self._load(planned.attempt_id)
        if head.state == "cleanup_pending":
            _require_own_closure(head, planned, message)
            work = (
                self._covering(work)
                if work is not None and work.state == "cleanup_pending"
                else self._claim(head)
            )
            self._backend.finish_closed_cleanup(work, stage_guard=self._guard(work))
            head = self._load(planned.attempt_id)
        if head.state != "pre_submission_failed":
            raise ExpiredPreparedClosureRefused(
                f"{planned.attempt_id} is {head.state}; only its captured prepared H0 or this closure may continue"
            )
        failure = _require_own_closure(head, planned, message)
        cleanup = _single(head, "cleanup_receipt", LocalCleanupReceiptV4)
        return ClosedMember(
            attempt_id=head.attempt_id,
            document_id=head.document_id,
            processing_run_id=head.processing_run_id,
            final_state=head.state,
            failure_receipt_sha256=failure.sha256,
            cleanup_receipt_sha256=cleanup.sha256,
            continued_from=continued_from,
        )

    def _claim(self, head: RemoteParseV4Authority) -> CoordinatorWork:
        lease = head.database_lease
        try:
            work = self._persistence.claim_recovery(RecoveryCandidate(
                attempt_id=head.attempt_id,
                state=head.state,
                lifecycle_version=head.lifecycle_version,
                claim_generation=head.claim_generation,
                claim_owner_identity=head.claim_owner_identity,
                lease_remaining_seconds=(
                    None if head.claim_owner_identity is None
                    else (lease.remaining_microseconds / 1_000_000 if lease is not None else 0.0)
                ),
            ))
        except (RecoveryDeferred, V4ClaimHeldByOther) as exc:
            raise ExpiredPreparedClosureRefused(f"{head.attempt_id} is claimed by a live owner") from exc
        if (work.attempt_id, work.state, work.lifecycle_version) != (
            head.attempt_id, head.state, head.lifecycle_version,
        ):
            raise ExpiredPreparedClosureRefused(f"{head.attempt_id} head moved while it was claimed")
        return work

    def _covering(self, work: CoordinatorWork) -> CoordinatorWork:
        """Renew only when the remaining lease cannot cover one bounded stage."""
        expires = work.lease_expires_monotonic
        if expires is not None and expires - self._monotonic() > self._stage_seconds:
            return work
        return self._persistence.renew_claim(work, lease_seconds=self._claim_lease_seconds)

    def _guard(self, work: CoordinatorWork) -> StageLeaseGuard:
        return StageLeaseGuard(
            deadline_monotonic=self._monotonic() + self._stage_seconds,
            _revoked=Event(),
            _monotonic=self._monotonic,
            claim_deadline_monotonic=work.lease_expires_monotonic,
            attempt_id=work.attempt_id,
            lane="cleanup",
        )


def _planned_projection(member: LegacyScopeMember, key_ttl_seconds: int) -> ClosureMemberV1:
    return ClosureMemberV1(
        attempt_id=member.attempt_id,
        document_id=member.document_id,
        processing_run_id=member.processing_run_id,
        fence_identity=member.fence_identity,
        h0_checkpoint_sha256=member.h0_checkpoint_sha256,
        client_submit_key=member.client_submit_key,
        submission_epoch_unix=member.submission_epoch_unix,
        key_expired_at_unix=member.submission_epoch_unix + key_ttl_seconds,
    )


def _single(authority: RemoteParseV4Authority, kind: str, expected: type[_EvidenceT]) -> _EvidenceT:
    values = [item.value for item in authority.evidence if item.kind == kind]
    if len(values) != 1 or type(values[0]) is not expected:
        raise ExpiredPreparedClosureRefused(f"{authority.attempt_id} lacks exactly one {kind}")
    return values[0]


def _require_own_closure(
    authority: RemoteParseV4Authority, planned: ClosureMemberV1, message: str,
) -> FailureReceiptV4:
    """The head's failure is exactly this decision's closure of its captured H0."""

    failure = _single(authority, "failure_receipt", FailureReceiptV4)
    if (
        authority.checkpoint_history[0].sha256 != planned.h0_checkpoint_sha256
        or authority.fence_identity != planned.fence_identity
        or failure.source_checkpoint_sha256 != planned.h0_checkpoint_sha256
        or failure.outcome != "pre_submission_failure"
        or failure.submission_was_attempted
        or failure.error_code != ORIGINAL_KEY_EXPIRED_ERROR_CODE
        or failure.message != message
    ):
        raise ExpiredPreparedClosureRefused(
            f"{planned.attempt_id} was closed by another cause or decision"
        )
    return failure
