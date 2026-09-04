"""Durable claim and admission slice for the staged V4 coordinator.

This service deliberately implements only the PostgreSQL-backed persistence
half of the coordinator backend.  It is not production-composable until the
bounded remote, materialization, publication, cleanup, and ACK stage methods
are added.  PostgreSQL remains the only authority: claim witnesses are rebuilt
from a fresh durable load rather than kept in a process-local cache.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields
from math import isfinite
import time

from disclosure_anchor.application.contracts.staged_credit import (
    conservative_monotonic_deadline,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
    STAGED_RESOURCE_STATE_TRANSITIONS,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RecoveryCandidate,
    RemoteParseV4Authority,
    RemoteParseV4AuthorityViolation,
    V4AttemptFinal,
    V4ClaimHeldByOther,
    V4HeadExpectation,
    V4HeadNotFound,
    V4HeadStale,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.staged_parse_coordinator import (
    AdmissionInterrupted,
    AdmissionOutcome,
    CoordinatorLimits,
    CoordinatorWork,
    RecoveryDeferred,
)


class StagedClaimLost(RuntimeError):
    """The durable head no longer proves this boot's execution ownership."""


class StagedClaimResponseLost(RuntimeError):
    """A bounded write could not be closed by an exact durable reload."""


class StagedLeaseNotRunnable(RuntimeError):
    """A database lease cannot safely cover work in this monotonic clock domain."""


@dataclass(frozen=True, slots=True)
class _ObservedAuthority:
    authority: RemoteParseV4Authority
    monotonic_before: float
    monotonic_after: float


class _MutationOutcomeUnknown(RuntimeError):
    """The repository returned a mutation, but its outer transaction was uncertain."""


def _is_final(authority: RemoteParseV4Authority) -> bool:
    return authority.state not in STAGED_RESOURCE_STATE_TRANSITIONS


def _same_head(
    left: RemoteParseV4Authority,
    right: RemoteParseV4Authority,
) -> bool:
    return (
        left.attempt_id == right.attempt_id
        and left.fence_identity == right.fence_identity
        and left.state == right.state
        and left.lifecycle_version == right.lifecycle_version
        and left.checkpoint_sha256 == right.checkpoint_sha256
    )


class DurableStagedCoordinatorPersistenceV4:
    """Recovery, claim renewal, reload, and claim-only admission over V4 heads."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        limits: CoordinatorLimits,
        owner_identity: str,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(limits) is not CoordinatorLimits:
            raise ValueError("staged persistence requires exact coordinator limits")
        if (
            type(owner_identity) is not str
            or not owner_identity.strip()
            or len(owner_identity.encode("utf-8")) > 128
        ):
            raise ValueError("staged persistence owner identity is invalid")
        if not callable(uow_factory) or not callable(monotonic):
            raise ValueError("staged persistence dependencies are invalid")
        self._uow_factory = uow_factory
        self._limits = limits
        self._owner_identity = owner_identity
        self._monotonic = monotonic

    def list_recoverable(
        self,
        *,
        after_attempt_id: str | None,
        limit: int,
    ) -> Sequence[RecoveryCandidate]:
        with self._uow_factory() as uow:
            return uow.remote_parse_v4.list_recoverable_heads(
                after_attempt_id=after_attempt_id,
                limit=limit,
            )

    def claim_recovery(self, candidate: RecoveryCandidate) -> CoordinatorWork:
        if type(candidate) is not RecoveryCandidate:
            raise ValueError("recovery claim requires an exact candidate")
        observed = self._load(candidate.attempt_id)
        return self._claim_observed(observed)

    def renew_claim(
        self,
        work: CoordinatorWork,
        *,
        lease_seconds: int,
    ) -> CoordinatorWork:
        if type(work) is not CoordinatorWork:
            raise ValueError("claim renewal requires exact coordinator work")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 300
        ):
            raise ValueError("claim renewal lease is outside 1..300")

        baseline = self._load(work.attempt_id)
        baseline_work = self._project_owned(baseline)
        # Reprojecting the same UTC lease through a later conservative clock
        # bracket need not reproduce the identical floating-point deadline.
        # The durable owner/generation/head/credits are the renewal witness.
        self._require_same_work(work, baseline_work, ignore_lease=True)

        last_unknown: _MutationOutcomeUnknown | None = None
        for write_number in range(2):
            try:
                renewed = self._renew_transaction(
                    baseline.authority,
                    lease_seconds=lease_seconds,
                )
            except _MutationOutcomeUnknown as exc:
                last_unknown = exc
                try:
                    durable = self._load(work.attempt_id)
                except Exception as reload_exc:
                    raise StagedClaimResponseLost(
                        f"{work.attempt_id}: claim renewal outcome could not be reloaded"
                    ) from reload_exc
                if self._renewal_committed(baseline.authority, durable.authority):
                    projected = self._project_owned(durable)
                    self._require_same_work(work, projected, ignore_lease=True)
                    return projected
                if (
                    write_number == 0
                    and _same_head(baseline.authority, durable.authority)
                    and self._is_owned_live(durable.authority)
                    and durable.authority.claim_generation
                    == baseline.authority.claim_generation
                    and durable.authority.claim_lease_until
                    == baseline.authority.claim_lease_until
                ):
                    baseline = durable
                    continue
                raise StagedClaimResponseLost(
                    f"{work.attempt_id}: claim renewal outcome is not durably closed"
                ) from exc
            projected = self._project_owned(renewed)
            if not self._renewal_committed(
                baseline.authority,
                renewed.authority,
            ):
                raise StagedClaimResponseLost(
                    f"{work.attempt_id}: claim renewal did not advance the durable lease"
                )
            self._require_same_work(work, projected, ignore_lease=True)
            return projected
        assert last_unknown is not None
        raise StagedClaimResponseLost(
            f"{work.attempt_id}: claim renewal exceeded its bounded replay"
        ) from last_unknown

    def reload_claim(self, work: CoordinatorWork) -> CoordinatorWork:
        if type(work) is not CoordinatorWork:
            raise ValueError("claim reload requires exact coordinator work")
        observed = self._load(work.attempt_id)
        authority = observed.authority
        if _is_final(authority):
            projected = self._project_final(authority)
        else:
            projected = self._project_owned(observed)
        self._require_reload_continuity(work, projected)
        return projected

    def admit_new(
        self,
        *,
        limit: int,
        available_credits: ResourceCreditVector,
    ) -> AdmissionOutcome:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("admission limit must be a positive integer")
        if type(available_credits) is not ResourceCreditVector:
            raise ValueError("admission credits must be an exact vector")
        if not available_credits.fits(self._limits.credits):
            raise ValueError("admission credits exceed coordinator capacity")

        durably_claimed: list[CoordinatorWork] = []
        try:
            return self._admit_new_claims(
                limit=limit,
                available_credits=available_credits,
                durably_claimed=durably_claimed,
            )
        except AdmissionInterrupted:
            raise
        except Exception as exc:
            if durably_claimed:
                raise AdmissionInterrupted(
                    f"{type(exc).__name__}:{exc}",
                    claimed_work=tuple(durably_claimed),
                ) from exc
            raise

    def _admit_new_claims(
        self,
        *,
        limit: int,
        available_credits: ResourceCreditVector,
        durably_claimed: list[CoordinatorWork],
    ) -> AdmissionOutcome:
        selected: list[CoordinatorWork] = []
        selected_credits = ResourceCreditVector()
        globally_blocked: list[ResourceCreditVector] = []
        presently_blocked: list[ResourceCreditVector] = []
        unprocessed_page_tail = False
        unscanned_page_exists = False
        cursor: str | None = None

        while len(selected) < limit:
            page = self._list_unclaimed_prepared(after_attempt_id=cursor)
            if not page:
                break
            for index, candidate in enumerate(page):
                cursor = candidate.attempt_id
                if len(selected) >= limit:
                    unprocessed_page_tail = True
                    break
                try:
                    observed = self._load(candidate.attempt_id)
                except V4HeadNotFound:
                    continue
                authority = observed.authority
                if not self._is_unclaimed_prepared(authority):
                    continue
                reservation = authority.reservation
                if reservation is None:
                    raise RemoteParseV4AuthorityViolation(
                        "unclaimed prepared head lacks its resource reservation"
                    )
                if not reservation.reserved_credit.fits(self._limits.credits):
                    globally_blocked.append(reservation.reserved_credit)
                    continue
                remaining = available_credits - selected_credits
                held = authority.checkpoint.held_resource_credit
                if not held.fits(remaining):
                    presently_blocked.append(held)
                    continue
                try:
                    claimed = self._claim_observed(observed)
                except RecoveryDeferred:
                    continue
                except (V4HeadStale, V4AttemptFinal):
                    continue
                if (
                    claimed.state in STAGED_RESOURCE_STATE_TRANSITIONS
                    and claimed.claim_owner_identity == self._owner_identity
                ):
                    durably_claimed.append(claimed)
                if (
                    claimed.state != "prepared"
                    or claimed.lifecycle_version != 0
                    or claimed.claim_owner_identity != self._owner_identity
                ):
                    if claimed.state in STAGED_RESOURCE_STATE_TRANSITIONS:
                        raise RemoteParseV4AuthorityViolation(
                            "admission claim returned a noninitial durable head"
                        )
                    continue
                next_credits = selected_credits + claimed.credits
                if not next_credits.fits(available_credits):
                    raise RemoteParseV4AuthorityViolation(
                        "admission claim exceeded the granted credits"
                    )
                selected.append(claimed)
                selected_credits = next_credits
                if index + 1 < len(page) and len(selected) >= limit:
                    unprocessed_page_tail = True
                    break
            if unprocessed_page_tail or len(page) < self._limits.recovery_page_size:
                break

        if (
            len(selected) >= limit
            and not unprocessed_page_tail
            and not globally_blocked
            and not presently_blocked
            and cursor is not None
        ):
            unscanned_page_exists = bool(
                self._list_unclaimed_prepared(
                    after_attempt_id=cursor,
                    limit=1,
                )
            )

        remaining = available_credits - selected_credits
        blocked = {
            item.name
            for vector in globally_blocked
            for item in fields(ResourceCreditVector)
            if getattr(vector, item.name) > getattr(self._limits.credits, item.name)
        }
        blocked.update(
            item.name
            for vector in presently_blocked
            for item in fields(ResourceCreditVector)
            if getattr(vector, item.name) > getattr(remaining, item.name)
        )
        canonical_blocked = tuple(
            item.name for item in fields(ResourceCreditVector) if item.name in blocked
        )
        backlog_exists = bool(
            globally_blocked
            or presently_blocked
            or unprocessed_page_tail
            or unscanned_page_exists
        )
        return AdmissionOutcome(
            work=tuple(selected),
            backlog_exists=backlog_exists,
            blocked_dimensions=canonical_blocked,
        )

    def _list_unclaimed_prepared(
        self,
        *,
        after_attempt_id: str | None,
        limit: int | None = None,
    ) -> tuple[RecoveryCandidate, ...]:
        with self._uow_factory() as uow:
            return uow.remote_parse_v4.list_unclaimed_prepared_heads(
                after_attempt_id=after_attempt_id,
                limit=self._limits.recovery_page_size if limit is None else limit,
            )

    def _load(self, attempt_id: str) -> _ObservedAuthority:
        before = self._now()
        with self._uow_factory() as uow:
            authority = uow.remote_parse_v4.load(attempt_id)
        after = self._now()
        self._require_monotonic_bracket(before, after)
        return _ObservedAuthority(
            authority=authority,
            monotonic_before=before,
            monotonic_after=after,
        )

    def _claim_transaction(
        self,
        authority: RemoteParseV4Authority,
    ) -> _ObservedAuthority:
        expectation = V4HeadExpectation.from_authority(authority)
        before = self._now()
        mutation_returned = False
        try:
            with self._uow_factory() as uow:
                claimed = uow.remote_parse_v4.claim(
                    expectation,
                    owner_identity=self._owner_identity,
                    lease_seconds=self._limits.claim_lease_seconds,
                )
                mutation_returned = True
                uow.commit()
        except Exception as exc:
            if mutation_returned:
                raise _MutationOutcomeUnknown(
                    f"{authority.attempt_id}: claim transaction outcome is unknown"
                ) from exc
            raise
        after = self._now()
        self._require_monotonic_bracket(before, after)
        return _ObservedAuthority(claimed, before, after)

    def _renew_transaction(
        self,
        authority: RemoteParseV4Authority,
        *,
        lease_seconds: int,
    ) -> _ObservedAuthority:
        before = self._now()
        mutation_returned = False
        try:
            with self._uow_factory() as uow:
                renewed = uow.remote_parse_v4.renew(
                    authority.claim_witness,
                    lease_seconds=lease_seconds,
                )
                mutation_returned = True
                uow.commit()
        except Exception as exc:
            if mutation_returned:
                raise _MutationOutcomeUnknown(
                    f"{authority.attempt_id}: claim renewal transaction outcome is unknown"
                ) from exc
            raise
        after = self._now()
        self._require_monotonic_bracket(before, after)
        return _ObservedAuthority(renewed, before, after)

    def _claim_observed(self, observed: _ObservedAuthority) -> CoordinatorWork:
        last_unknown: _MutationOutcomeUnknown | None = None
        for write_number in range(2):
            authority = observed.authority
            if _is_final(authority):
                return self._project_final(authority)
            if self._is_foreign_live(authority):
                raise self._foreign_deferred(observed)
            try:
                claimed = self._claim_transaction(authority)
            except V4ClaimHeldByOther:
                observed = self._load(authority.attempt_id)
                if _is_final(observed.authority):
                    return self._project_final(observed.authority)
                if self._is_foreign_live(observed.authority):
                    raise self._foreign_deferred(observed)
                if write_number == 0:
                    continue
                raise
            except (V4HeadStale, V4AttemptFinal):
                observed = self._load(authority.attempt_id)
                if _is_final(observed.authority):
                    return self._project_final(observed.authority)
                if self._is_foreign_live(observed.authority):
                    raise self._foreign_deferred(observed)
                if write_number == 0:
                    continue
                raise
            except _MutationOutcomeUnknown as exc:
                last_unknown = exc
                try:
                    durable = self._load(authority.attempt_id)
                except Exception as reload_exc:
                    raise StagedClaimResponseLost(
                        f"{authority.attempt_id}: claim outcome could not be reloaded"
                    ) from reload_exc
                if self._claim_committed(authority, durable.authority):
                    return self._project_owned(durable)
                if _is_final(durable.authority):
                    return self._project_final(durable.authority)
                if self._is_foreign_live(durable.authority):
                    raise self._foreign_deferred(durable)
                if write_number == 0:
                    observed = durable
                    continue
                raise StagedClaimResponseLost(
                    f"{authority.attempt_id}: claim outcome is not durably closed"
                ) from exc
            if not self._claim_committed(authority, claimed.authority):
                raise RemoteParseV4AuthorityViolation(
                    "claim transaction returned an invalid durable head"
                )
            return self._project_owned(claimed)
        assert last_unknown is not None
        raise StagedClaimResponseLost(
            f"{observed.authority.attempt_id}: claim exceeded its bounded replay"
        ) from last_unknown

    def _project_owned(self, observed: _ObservedAuthority) -> CoordinatorWork:
        authority = observed.authority
        if _is_final(authority):
            return self._project_final(authority)
        if not self._is_owned_live(authority):
            raise StagedClaimLost(
                "durable V4 head is not live-owned by this coordinator boot"
            )
        snapshot = authority.database_lease
        assert snapshot is not None
        try:
            deadline = conservative_monotonic_deadline(
                snapshot,
                monotonic_before=observed.monotonic_before,
                monotonic_after=observed.monotonic_after,
            )
        except ValueError as exc:
            raise StagedLeaseNotRunnable(
                "durable V4 lease cannot be projected safely"
            ) from exc
        return self._project_nonfinal(authority, deadline=deadline)

    def _project_foreign(self, observed: _ObservedAuthority) -> CoordinatorWork:
        authority = observed.authority
        if not self._is_foreign_live(authority):
            raise StagedClaimLost("durable V4 head lacks a live foreign claim")
        snapshot = authority.database_lease
        assert snapshot is not None
        remaining = snapshot.remaining_microseconds / 1_000_000
        deadline = observed.monotonic_after + remaining
        if not isfinite(deadline) or deadline <= observed.monotonic_after:
            raise StagedLeaseNotRunnable(
                "foreign durable V4 lease cannot be projected safely"
            )
        return self._project_nonfinal(authority, deadline=deadline)

    @staticmethod
    def _project_nonfinal(
        authority: RemoteParseV4Authority,
        *,
        deadline: float,
    ) -> CoordinatorWork:
        if not authority.is_current or authority.reservation is None:
            raise RemoteParseV4AuthorityViolation(
                "nonfinal V4 authority lacks current reservation ownership"
            )
        try:
            return CoordinatorWork(
                attempt_id=authority.attempt_id,
                state=authority.state,
                lifecycle_version=authority.lifecycle_version,
                claim_generation=authority.claim_generation,
                claim_owner_identity=authority.claim_owner_identity,
                lease_expires_monotonic=deadline,
                credit_reservation=authority.reservation.reserved_credit,
                credits=authority.checkpoint.held_resource_credit,
            )
        except ValueError as exc:
            raise RemoteParseV4AuthorityViolation(
                "durable V4 head contradicts the coordinator contract"
            ) from exc

    @staticmethod
    def _project_final(authority: RemoteParseV4Authority) -> CoordinatorWork:
        if not _is_final(authority):
            raise ValueError("final projection requires a final V4 authority")
        try:
            return CoordinatorWork(
                attempt_id=authority.attempt_id,
                state=authority.state,
                lifecycle_version=authority.lifecycle_version,
                claim_generation=authority.claim_generation,
                claim_owner_identity=None,
                lease_expires_monotonic=None,
                credit_reservation=ResourceCreditVector(),
                credits=ResourceCreditVector(),
            )
        except ValueError as exc:
            raise RemoteParseV4AuthorityViolation(
                "final V4 head contradicts the coordinator contract"
            ) from exc

    def _foreign_deferred(self, observed: _ObservedAuthority) -> RecoveryDeferred:
        snapshot = observed.authority.database_lease
        if snapshot is None or snapshot.remaining_microseconds <= 0:
            raise StagedClaimLost("foreign V4 claim is not durably live")
        remaining = snapshot.remaining_microseconds / 1_000_000
        retry_after = remaining + self._limits.poll_seconds
        if not isfinite(retry_after) or retry_after <= 0:
            raise StagedLeaseNotRunnable(
                "foreign V4 retry delay cannot be represented safely"
            )
        return RecoveryDeferred(
            "durable V4 recovery claim is held by another owner",
            retry_after_seconds=retry_after,
            durable_work=self._project_foreign(observed),
        )

    def _claim_committed(
        self,
        before: RemoteParseV4Authority,
        after: RemoteParseV4Authority,
    ) -> bool:
        before_live_same_owner = self._is_owned_live(before)
        generation_committed = (
            after.claim_generation == before.claim_generation
            if before_live_same_owner
            else after.claim_generation > before.claim_generation
        )
        return (
            _same_head(before, after)
            and self._is_owned_live(after)
            and generation_committed
            and (
                not before_live_same_owner
                or (
                    before.claim_lease_until is not None
                    and after.claim_lease_until is not None
                    and after.claim_lease_until >= before.claim_lease_until
                )
            )
        )

    def _renewal_committed(
        self,
        before: RemoteParseV4Authority,
        after: RemoteParseV4Authority,
    ) -> bool:
        return (
            _same_head(before, after)
            and self._is_owned_live(after)
            and after.claim_generation == before.claim_generation
            and before.claim_lease_until is not None
            and after.claim_lease_until is not None
            and after.claim_lease_until > before.claim_lease_until
        )

    def _is_owned_live(self, authority: RemoteParseV4Authority) -> bool:
        return (
            authority.is_current
            and not _is_final(authority)
            and authority.claim_owner_identity == self._owner_identity
            and authority.claim_generation >= 1
            and authority.database_lease is not None
            and authority.database_lease.remaining_microseconds > 0
        )

    def _is_foreign_live(self, authority: RemoteParseV4Authority) -> bool:
        return (
            authority.is_current
            and not _is_final(authority)
            and authority.claim_owner_identity is not None
            and authority.claim_owner_identity != self._owner_identity
            and authority.claim_generation >= 1
            and authority.database_lease is not None
            and authority.database_lease.remaining_microseconds > 0
        )

    @staticmethod
    def _is_unclaimed_prepared(authority: RemoteParseV4Authority) -> bool:
        return (
            authority.is_current
            and authority.state == "prepared"
            and authority.lifecycle_version == 0
            and authority.claim_generation == 0
            and authority.claim_owner_identity is None
            and authority.claim_lease_until is None
        )

    @staticmethod
    def _require_same_work(
        expected: CoordinatorWork,
        observed: CoordinatorWork,
        *,
        ignore_lease: bool = False,
    ) -> None:
        if (
            observed.attempt_id != expected.attempt_id
            or observed.state != expected.state
            or observed.lifecycle_version != expected.lifecycle_version
            or observed.claim_generation != expected.claim_generation
            or observed.claim_owner_identity != expected.claim_owner_identity
            or observed.credit_reservation != expected.credit_reservation
            or observed.credits != expected.credits
            or (
                not ignore_lease
                and observed.lease_expires_monotonic
                != expected.lease_expires_monotonic
            )
        ):
            raise StagedClaimLost(
                "durable V4 claim no longer matches coordinator work"
            )

    @staticmethod
    def _require_reload_continuity(
        previous: CoordinatorWork,
        observed: CoordinatorWork,
    ) -> None:
        if (
            observed.attempt_id != previous.attempt_id
            or observed.claim_generation != previous.claim_generation
            or observed.lifecycle_version not in {
                previous.lifecycle_version,
                previous.lifecycle_version + 1,
            }
        ):
            raise StagedClaimLost("durable V4 reload crossed its claim continuity")
        if observed.lifecycle_version == previous.lifecycle_version:
            if (
                observed.state != previous.state
                or observed.claim_owner_identity != previous.claim_owner_identity
                or observed.credit_reservation != previous.credit_reservation
                or observed.credits != previous.credits
            ):
                raise StagedClaimLost(
                    "durable V4 reload changed an unadvanced checkpoint"
                )
            return
        if observed.state not in STAGED_RESOURCE_STATE_TRANSITIONS.get(
            previous.state,
            frozenset(),
        ):
            raise StagedClaimLost("durable V4 reload crossed more than one transition")
        if observed.state in STAGED_RESOURCE_STATE_TRANSITIONS and (
            observed.claim_owner_identity != previous.claim_owner_identity
            or observed.credit_reservation != previous.credit_reservation
        ):
            raise StagedClaimLost("durable V4 successor lost claim continuity")

    def _now(self) -> float:
        observed = self._monotonic()
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not isfinite(observed)
        ):
            raise StagedLeaseNotRunnable("monotonic clock observation is invalid")
        return float(observed)

    @staticmethod
    def _require_monotonic_bracket(before: float, after: float) -> None:
        if after < before:
            raise StagedLeaseNotRunnable("monotonic clock moved backwards")


__all__ = [
    "DurableStagedCoordinatorPersistenceV4",
    "StagedClaimLost",
    "StagedClaimResponseLost",
    "StagedLeaseNotRunnable",
]
