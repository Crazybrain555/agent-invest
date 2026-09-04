"""Crash-safe, work-conserving coordinator for staged whole-PDF parsing.

The coordinator owns scheduling only.  The backend owns every durable state
transition and all provider/database IO.  In particular, ``prepared`` work is
sent only to ``prepare_remote_io``; that operation must finish all local
preflight and durably commit ``reconciling`` before the coordinator permits
the first provider lookup or POST.

There is deliberately no cancelled completion state.  Closing admission lets
already accepted work drain through durable finish/failure and remote ACK.
Every own-claimed attempt is renewed while it waits as well as while it runs;
otherwise a congested lane could turn an expired queue into a restart livelock.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, fields
from enum import Enum
from math import isfinite
from threading import Event
import time
from typing import Protocol

from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
    STAGED_RESOURCE_STATE_TRANSITIONS,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RecoveryCandidate,
)


class CoordinatorLane(str, Enum):
    PREFLIGHT = "preflight"
    REMOTE = "remote"
    LOCAL_PREPARE = "local_prepare"
    LOCAL = "local"
    COMMIT = "commit"
    CLEANUP = "cleanup"
    ACK = "ack"


class CoordinatorTerminal(str, Enum):
    QUIESCENT = "quiescent"
    STUCK_OPEN_CIRCUIT = "stuck_open_circuit"


def _positive_credit_delta(
    before: ResourceCreditVector,
    after: ResourceCreditVector,
) -> ResourceCreditVector:
    return ResourceCreditVector(
        **{
            item.name: max(0, getattr(after, item.name) - getattr(before, item.name))
            for item in fields(ResourceCreditVector)
        }
    )


@dataclass(frozen=True, slots=True)
class CoordinatorWork:
    """Content-free projection of one claimed durable attempt."""

    attempt_id: str
    state: str
    lifecycle_version: int
    claim_generation: int
    claim_owner_identity: str | None
    lease_expires_monotonic: float | None
    credit_reservation: ResourceCreditVector
    credits: ResourceCreditVector

    def __post_init__(self) -> None:
        if not self.attempt_id.strip() or len(self.attempt_id) > 128:
            raise ValueError("coordinator attempt identity is invalid")
        if not self.state.strip() or len(self.state) > 64:
            raise ValueError("coordinator work state is invalid")
        for value, label in (
            (self.lifecycle_version, "lifecycle version"),
            (self.claim_generation, "claim generation"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"coordinator {label} is invalid")
        if (self.claim_owner_identity is None) != (
            self.lease_expires_monotonic is None
        ):
            raise ValueError("coordinator claim owner and lease must be paired")
        if self.claim_owner_identity is not None and (
            not self.claim_owner_identity.strip()
            or len(self.claim_owner_identity) > 128
            or self.lease_expires_monotonic is None
            or isinstance(self.lease_expires_monotonic, bool)
            or not isinstance(self.lease_expires_monotonic, (int, float))
            or not isfinite(self.lease_expires_monotonic)
            or self.lease_expires_monotonic <= 0
        ):
            raise ValueError("coordinator claim projection is invalid")
        if not self.credits.fits(self.credit_reservation):
            raise ValueError("exact credits exceed the lifecycle reservation")
        if self.state in _LANE_BY_STATE:
            if (
                self.claim_generation < 1
                or self.claim_owner_identity is None
                or self.lease_expires_monotonic is None
                or not self.credit_reservation.nonzero()
            ):
                raise ValueError(
                    "nonfinal coordinator work requires a live claim and "
                    "nonzero lifecycle reservation"
                )
        elif self.state in _FINAL_STATES and (
            self.claim_owner_identity is not None
            or self.lease_expires_monotonic is not None
            or self.credit_reservation != ResourceCreditVector()
            or self.credits != ResourceCreditVector()
        ):
            raise ValueError(
                "final coordinator work must release its claim and all credits"
            )


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    """One bounded backlog read with exact vector-pressure telemetry."""

    work: tuple[CoordinatorWork, ...]
    backlog_exists: bool
    blocked_dimensions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        credit_names = tuple(item.name for item in fields(ResourceCreditVector))
        canonical_blocked = tuple(
            name for name in credit_names if name in self.blocked_dimensions
        )
        if (
            type(self.work) is not tuple
            or any(type(item) is not CoordinatorWork for item in self.work)
            or type(self.backlog_exists) is not bool
            or type(self.blocked_dimensions) is not tuple
            or canonical_blocked != self.blocked_dimensions
            or any(name not in credit_names for name in self.blocked_dimensions)
            or (not self.backlog_exists and self.blocked_dimensions)
            or (self.backlog_exists and not self.work and not self.blocked_dimensions)
        ):
            raise ValueError("admission outcome is not closed")


class AdmissionInterrupted(RuntimeError):
    """Admission failed after one or more claims became durable.

    The coordinator must account these claims before opening its circuit.  An
    exception without this witness would make already-owned work disappear
    from the in-process credit ledger until the next recovery boot.
    """

    def __init__(
        self,
        message: str,
        *,
        claimed_work: tuple[CoordinatorWork, ...],
    ) -> None:
        super().__init__(message)
        if (
            type(claimed_work) is not tuple
            or not claimed_work
            or any(type(work) is not CoordinatorWork for work in claimed_work)
        ):
            raise ValueError("interrupted admission witness is not closed")
        attempt_ids = tuple(work.attempt_id for work in claimed_work)
        if len(attempt_ids) != len(set(attempt_ids)) or any(
            work.state not in _LANE_BY_STATE for work in claimed_work
        ):
            raise ValueError("interrupted admission witness is not closed")
        self.claimed_work = claimed_work


class RecoveryDeferred(RuntimeError):
    """A live durable claim cannot be taken until its lease becomes available."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float,
        durable_work: CoordinatorWork,
    ) -> None:
        super().__init__(message)
        if (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, (int, float))
            or not isfinite(retry_after_seconds)
            or retry_after_seconds <= 0
        ):
            raise ValueError("recovery retry delay must be positive")
        self.retry_after_seconds = float(retry_after_seconds)
        self.durable_work = durable_work


class RetryStage(RuntimeError):
    """The backend preserved durable state and requests a bounded retry."""

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        if (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, (int, float))
            or not isfinite(retry_after_seconds)
            or retry_after_seconds <= 0
        ):
            raise ValueError("stage retry delay must be positive")
        self.retry_after_seconds = float(retry_after_seconds)


class StageWaiting(RuntimeError):
    """A healthy bounded poll observed durable work that is not terminal yet.

    Normal provider execution can outlive many claim-renewal windows.  Unlike
    ``RetryStage``, this outcome does not consume an error budget or close new
    admission; the backend must still enforce the attempt's durable runaway
    envelope and raise a real failure when that boundary is crossed.
    """

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        if (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, (int, float))
            or not isfinite(retry_after_seconds)
            or retry_after_seconds <= 0
        ):
            raise ValueError("stage wait delay must be positive")
        self.retry_after_seconds = float(retry_after_seconds)


class StageLeaseLost(RuntimeError):
    """A bounded backend step lost its execution fence before a side effect."""


@dataclass(frozen=True, slots=True)
class StageLeaseGuard:
    """Cooperative hard boundary checked around every backend IO chunk."""

    deadline_monotonic: float
    _revoked: Event
    _monotonic: Callable[[], float]

    def __post_init__(self) -> None:
        if (
            isinstance(self.deadline_monotonic, bool)
            or not isinstance(self.deadline_monotonic, (int, float))
            or not isfinite(self.deadline_monotonic)
            or self.deadline_monotonic <= 0
        ):
            raise ValueError("stage lease deadline must be finite and positive")

    def checkpoint(self) -> None:
        self.remaining_seconds()

    def remaining_seconds(self) -> float:
        """Return the live bounded-stage budget after checking the claim fence."""

        observed = self._monotonic()
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not isfinite(observed)
            or self._revoked.is_set()
            or observed >= self.deadline_monotonic
        ):
            raise StageLeaseLost("bounded stage lease expired")
        return max(0.0, float(self.deadline_monotonic - observed))

    def revoke(self) -> None:
        self._revoked.set()


class StagedCoordinatorBackend(Protocol):
    """Bounded staged operations owned by the durable coordinator.

    Every implementation must call ``stage_guard.checkpoint()`` immediately
    before and after each network, filesystem, or database IO chunk.  A single
    chunk must itself be bounded by the guard deadline; returning after lease
    loss is a contract violation and cannot authorize another side effect.
    """

    """Durable operations used by the scheduling core.

    Every method must either return the exact reloaded durable projection,
    raise ``RetryStage`` after preserving its prior state, or raise an
    unexpected exception that opens the circuit.  No method may return an
    in-memory-only transition.
    """

    def list_recoverable(
        self, *, after_attempt_id: str | None, limit: int
    ) -> Sequence[RecoveryCandidate]:
        """Read an exact side-effect-free keyset page of current nonfinal heads.

        No row may be filtered after applying ``limit`` because a short page
        ends the startup recovery barrier.
        """

    def claim_recovery(
        self, candidate: RecoveryCandidate
    ) -> CoordinatorWork:
        """Claim one freshly reloaded candidate and return durable work.

        A head that completed between scan and claim may return its exact final
        projection.  A live foreign claim raises ``RecoveryDeferred`` with the
        durable foreign-owned projection.
        """

    def admit_new(
        self, *, limit: int, available_credits: ResourceCreditVector
    ) -> AdmissionOutcome:
        """Claim fitting work and report exact blocked backlog dimensions.

        A backend that fails after one or more claims commit must raise
        ``AdmissionInterrupted`` with every observed durable claim.
        """

    def renew_claim(
        self, work: CoordinatorWork, *, lease_seconds: int
    ) -> CoordinatorWork:
        """Extend only the same owner/generation claim without changing state."""

    def reload_claim(self, work: CoordinatorWork) -> CoordinatorWork:
        """Reload exact durable state after a renewal/commit response race."""

    def prepare_remote_io(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        """Run local-only preflight, then durably enter reconciling."""

    def run_remote(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork: ...

    def prepare_local_io(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        """Durably enter materializing and reserve exact local projections."""

    def run_local(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork: ...

    def commit(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork: ...

    def cleanup(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork: ...

    def acknowledge(
        self, work: CoordinatorWork, *, stage_guard: StageLeaseGuard
    ) -> CoordinatorWork: ...


@dataclass(frozen=True, slots=True)
class CoordinatorLimits:
    credits: ResourceCreditVector
    recovery_page_size: int = 128
    admission_batch_size: int = 32
    preflight_workers: int = 1
    remote_workers: int = 1
    local_prepare_workers: int = 1
    local_workers: int = 2
    commit_workers: int = 2
    cleanup_workers: int = 2
    ack_workers: int = 2
    poll_seconds: float = 0.1
    idle_open_circuit_seconds: float = 300.0
    claim_lease_seconds: int = 120
    claim_renew_margin_seconds: float = 30.0
    max_stage_step_seconds: float = 60.0
    retry_initial_backoff_seconds: float = 0.25
    retry_max_backoff_seconds: float = 30.0
    retry_consecutive_threshold: int = 3
    retry_max_attempts: int = 8
    retry_stuck_seconds: float = 300.0

    def __post_init__(self) -> None:
        for value, label in (
            (self.recovery_page_size, "recovery page size"),
            (self.admission_batch_size, "admission batch size"),
            (self.preflight_workers, "preflight workers"),
            (self.remote_workers, "remote workers"),
            (self.local_prepare_workers, "local prepare workers"),
            (self.local_workers, "local workers"),
            (self.commit_workers, "commit workers"),
            (self.cleanup_workers, "cleanup workers"),
            (self.ack_workers, "ACK workers"),
            (self.claim_lease_seconds, "claim lease seconds"),
            (self.retry_consecutive_threshold, "retry consecutive threshold"),
            (self.retry_max_attempts, "retry max attempts"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if not 1 <= self.claim_lease_seconds <= 300:
            raise ValueError("claim lease seconds must fit the DB 1..300 contract")
        if self.recovery_page_size > 1000:
            raise ValueError("recovery page size must fit the DB 1..1000 contract")
        for timing_value, label in (
            (self.poll_seconds, "poll seconds"),
            (self.idle_open_circuit_seconds, "idle open-circuit seconds"),
            (self.claim_renew_margin_seconds, "claim renewal margin seconds"),
            (self.max_stage_step_seconds, "maximum stage step seconds"),
            (self.retry_initial_backoff_seconds, "initial retry backoff seconds"),
            (self.retry_max_backoff_seconds, "maximum retry backoff seconds"),
            (self.retry_stuck_seconds, "retry stuck seconds"),
        ):
            if (
                isinstance(timing_value, bool)
                or not isinstance(timing_value, (int, float))
                or not isfinite(timing_value)
                or timing_value <= 0
            ):
                raise ValueError(f"{label} must be finite and positive")
        if not 0 < self.claim_renew_margin_seconds < self.claim_lease_seconds:
            raise ValueError("claim renewal margin must be inside the lease")
        if (
            self.max_stage_step_seconds <= 0
            or self.max_stage_step_seconds + self.claim_renew_margin_seconds
            >= self.claim_lease_seconds
        ):
            raise ValueError("bounded stage plus renewal margin must fit the lease")
        if self.poll_seconds > self.max_stage_step_seconds:
            raise ValueError(
                "poll interval must not exceed the maximum stage step"
            )
        if (
            self.poll_seconds <= 0
            or self.idle_open_circuit_seconds <= 0
            or self.retry_initial_backoff_seconds <= 0
            or self.retry_max_backoff_seconds < self.retry_initial_backoff_seconds
            or self.retry_stuck_seconds <= 0
        ):
            raise ValueError("coordinator timing limits must be positive")


@dataclass(frozen=True, slots=True)
class CoordinatorSnapshot:
    admission_open: bool
    recovery_complete: bool
    circuit_open: bool
    queued: tuple[tuple[str, int], ...]
    in_flight: tuple[tuple[str, int], ...]
    credits_in_use: ResourceCreditVector
    credits_limit: ResourceCreditVector
    completed: int
    blocked_reason: str | None
    credit_blocked_by_lane: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class CoordinatorResult:
    terminal: CoordinatorTerminal
    recovery_complete: bool
    admitted: int
    completed: int
    final_states: tuple[tuple[str, str], ...]
    errors: tuple[str, ...]
    credits_in_use: ResourceCreditVector


_FINAL_STATES = frozenset(
    {
        "acked",
        "remote_failed",
        "local_failed",
        "pre_submission_failed",
        "preparation_failed",
        "superseded",
    }
)
_LANE_BY_STATE = {
    "prepared": CoordinatorLane.PREFLIGHT,
    "reconciling": CoordinatorLane.REMOTE,
    "submitted": CoordinatorLane.REMOTE,
    "remote_terminal": CoordinatorLane.LOCAL_PREPARE,
    "materializing": CoordinatorLane.LOCAL,
    "local_materialized": CoordinatorLane.COMMIT,
    "publish_committed": CoordinatorLane.CLEANUP,
    "cleanup_pending": CoordinatorLane.CLEANUP,
    "ack_pending": CoordinatorLane.ACK,
}
_LANE_PRIORITY = (
    CoordinatorLane.ACK,
    CoordinatorLane.CLEANUP,
    CoordinatorLane.COMMIT,
    CoordinatorLane.LOCAL,
    CoordinatorLane.LOCAL_PREPARE,
    CoordinatorLane.REMOTE,
    CoordinatorLane.PREFLIGHT,
)
_STATE_PRIORITY_WITHIN_LANE = {
    CoordinatorLane.CLEANUP: {
        "cleanup_pending": 0,
        "publish_committed": 1,
    },
}
_ALLOWED_LANE_TRANSITIONS = {
    CoordinatorLane.PREFLIGHT: frozenset(
        ("prepared", target) for target in STAGED_RESOURCE_STATE_TRANSITIONS["prepared"]
    ),
    CoordinatorLane.REMOTE: frozenset(
        (source, target)
        for source in ("reconciling", "submitted")
        for target in STAGED_RESOURCE_STATE_TRANSITIONS[source]
    ),
    CoordinatorLane.LOCAL_PREPARE: frozenset(
        ("remote_terminal", target)
        for target in STAGED_RESOURCE_STATE_TRANSITIONS["remote_terminal"]
    ),
    CoordinatorLane.LOCAL: frozenset(
        ("materializing", target)
        for target in STAGED_RESOURCE_STATE_TRANSITIONS["materializing"]
    ),
    CoordinatorLane.COMMIT: frozenset(
        ("local_materialized", target)
        for target in STAGED_RESOURCE_STATE_TRANSITIONS["local_materialized"]
    ),
    CoordinatorLane.CLEANUP: frozenset(
        (source, target)
        for source in ("publish_committed", "cleanup_pending")
        for target in STAGED_RESOURCE_STATE_TRANSITIONS[source]
    ),
    CoordinatorLane.ACK: frozenset(
        (source, target)
        for source in ("ack_pending",)
        for target in STAGED_RESOURCE_STATE_TRANSITIONS[source]
    ),
}


class _CreditLedger:
    def __init__(self, limit: ResourceCreditVector) -> None:
        self.limit = limit
        self.in_use = ResourceCreditVector()
        self.by_attempt: dict[str, ResourceCreditVector] = {}

    def can_add(self, work: CoordinatorWork) -> bool:
        previous = self.by_attempt.get(work.attempt_id, ResourceCreditVector())
        candidate = self.in_use - previous + work.credits
        return candidate.fits(self.limit)

    def replace(self, work: CoordinatorWork, *, allow_oversubscribed: bool) -> None:
        previous = self.by_attempt.get(work.attempt_id, ResourceCreditVector())
        candidate = self.in_use - previous + work.credits
        if not allow_oversubscribed and not candidate.fits(self.limit):
            raise ValueError("new work exceeds coordinator credit limits")
        self.by_attempt[work.attempt_id] = work.credits
        self.in_use = candidate

    def release(self, attempt_id: str) -> None:
        previous = self.by_attempt.pop(attempt_id, None)
        if previous is not None:
            self.in_use = self.in_use - previous

    def allowance_for(self, attempt_id: str) -> ResourceCreditVector:
        previous = self.by_attempt.get(attempt_id, ResourceCreditVector())
        return self.limit - (self.in_use - previous)


class StagedParseCoordinator:
    """Run a recovery-first staged parse dispatcher until stop and quiescence."""

    def __init__(
        self,
        *,
        backend: StagedCoordinatorBackend,
        limits: CoordinatorLimits,
        progress: Callable[[CoordinatorSnapshot], None] = lambda _snapshot: None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._limits = limits
        self._progress = progress
        self._monotonic = monotonic

    def run(
        self,
        *,
        stop_requested: Callable[[], bool] = lambda: False,
    ) -> CoordinatorResult:
        queues = {lane: deque[CoordinatorWork]() for lane in CoordinatorLane}
        retry_at: dict[str, tuple[float, CoordinatorWork]] = {}
        retry_attempts: dict[str, int] = {}
        retry_started_at: dict[str, float] = {}
        consecutive_retries = 0
        retry_degraded = False
        known: dict[str, CoordinatorWork] = {}
        final: dict[str, str] = {}
        errors: list[str] = []
        ledger = _CreditLedger(self._limits.credits)
        oversubscribed_recovery: set[str] = set()
        admitted = 0
        completed = 0
        admission_open = False
        admission_blocked_dimensions: tuple[str, ...] = ()
        admission_blocked_available: ResourceCreditVector | None = None
        recovery_complete = False
        circuit_open = False
        blocked_reason: str | None = "recovery_barrier"
        last_progress = self._monotonic()
        waiting_renewal_floor = float("inf")
        lane_limits = {
            CoordinatorLane.PREFLIGHT: self._limits.preflight_workers,
            CoordinatorLane.REMOTE: self._limits.remote_workers,
            CoordinatorLane.LOCAL_PREPARE: self._limits.local_prepare_workers,
            CoordinatorLane.LOCAL: self._limits.local_workers,
            CoordinatorLane.COMMIT: self._limits.commit_workers,
            CoordinatorLane.CLEANUP: self._limits.cleanup_workers,
            CoordinatorLane.ACK: self._limits.ack_workers,
        }
        pools = {
            lane: ThreadPoolExecutor(
                max_workers=lane_limits[lane],
                thread_name_prefix=f"staged-{lane.value}",
            )
            for lane in CoordinatorLane
        }
        in_flight: dict[
            Future[CoordinatorWork],
            tuple[
                CoordinatorLane,
                CoordinatorWork,
                StageLeaseGuard,
                ResourceCreditVector,
            ],
        ] = {}
        reconciled_results: dict[Future[CoordinatorWork], CoordinatorWork] = {}
        in_flight_failures: set[Future[CoordinatorWork]] = set()
        provisional_local: dict[Future[CoordinatorWork], ResourceCreditVector] = {}
        provisional_local_total = ResourceCreditVector()
        credit_blocked_by_lane: dict[CoordinatorLane, set[str]] = {
            lane: set() for lane in CoordinatorLane
        }

        def emit() -> None:
            self._progress(
                CoordinatorSnapshot(
                    admission_open=admission_open,
                    recovery_complete=recovery_complete,
                    circuit_open=circuit_open,
                    queued=tuple(
                        (lane.value, len(queues[lane])) for lane in CoordinatorLane
                    ),
                    in_flight=tuple(
                        (
                            lane.value,
                            sum(
                                1
                                for active_lane, _, _, _ in in_flight.values()
                                if active_lane == lane
                            ),
                        )
                        for lane in CoordinatorLane
                    ),
                    credits_in_use=ledger.in_use + provisional_local_total,
                    credits_limit=ledger.limit,
                    completed=completed,
                    blocked_reason=blocked_reason,
                    credit_blocked_by_lane=tuple(
                        (lane.value, tuple(sorted(credit_blocked_by_lane[lane])))
                        for lane in CoordinatorLane
                    ),
                )
            )

        def track_waiting_lease(work: CoordinatorWork) -> None:
            nonlocal waiting_renewal_floor
            if work.lease_expires_monotonic is None:
                raise RuntimeError("waiting work lacks a claim lease")
            waiting_renewal_floor = min(
                waiting_renewal_floor,
                work.lease_expires_monotonic,
            )

        def place(work: CoordinatorWork, *, recovery: bool) -> None:
            nonlocal completed, last_progress, circuit_open, blocked_reason
            previous = known.get(work.attempt_id)
            if previous is not None and (
                work.claim_generation < previous.claim_generation
                or work.lifecycle_version < previous.lifecycle_version
            ):
                raise RuntimeError("durable coordinator projection moved backwards")
            known[work.attempt_id] = work
            if recovery and (
                not ledger.can_add(work)
                or not work.credit_reservation.fits(ledger.limit)
            ):
                oversubscribed_recovery.add(work.attempt_id)
            ledger.replace(work, allow_oversubscribed=recovery)
            if recovery and not ledger.in_use.fits(ledger.limit):
                # Aggregate recovery overage is owned collectively: marking
                # only the row that crossed the limit can strand an earlier
                # FIFO owner that must run to release the saturated dimension.
                oversubscribed_recovery.update(known)
            if oversubscribed_recovery and ledger.in_use.fits(ledger.limit):
                oversubscribed_recovery.intersection_update(
                    attempt_id
                    for attempt_id, durable in known.items()
                    if not durable.credit_reservation.fits(ledger.limit)
                )
            if work.state in _FINAL_STATES:
                ledger.release(work.attempt_id)
                final[work.attempt_id] = work.state
                known.pop(work.attempt_id, None)
                retry_at.pop(work.attempt_id, None)
                oversubscribed_recovery.discard(work.attempt_id)
                completed += 1
                last_progress = self._monotonic()
                return
            lane = _LANE_BY_STATE.get(work.state)
            if lane is None:
                circuit_open = True
                blocked_reason = "unsupported_durable_state"
                errors.append(f"{work.attempt_id}:unsupported state:{work.state}")
                return
            queues[lane].append(work)
            track_waiting_lease(work)

        def reserve_deferred(work: CoordinatorWork) -> None:
            """Account durable ownership without making the live claim runnable."""

            previous = known.get(work.attempt_id)
            if previous is not None and (
                work.claim_generation < previous.claim_generation
                or work.lifecycle_version < previous.lifecycle_version
            ):
                raise RuntimeError("deferred recovery projection moved backwards")
            known[work.attempt_id] = work
            if not ledger.can_add(work) or not work.credit_reservation.fits(
                ledger.limit
            ):
                oversubscribed_recovery.add(work.attempt_id)
            ledger.replace(work, allow_oversubscribed=True)
            if not ledger.in_use.fits(ledger.limit):
                oversubscribed_recovery.update(known)
            else:
                oversubscribed_recovery.intersection_update(
                    attempt_id
                    for attempt_id, durable in known.items()
                    if not durable.credit_reservation.fits(ledger.limit)
                )

        def preserve_contract_violation(
            prior: CoordinatorWork,
            updated: CoordinatorWork,
            lane: CoordinatorLane,
            message: str,
        ) -> None:
            nonlocal circuit_open, admission_open, blocked_reason
            # The backend says this projection is already durable. Preserve
            # both its exact state and resource ownership before opening the
            # circuit; silently dropping it would lose recoverable work.
            known[updated.attempt_id] = updated
            ledger.replace(updated, allow_oversubscribed=True)
            circuit_open = True
            admission_open = False
            blocked_reason = "durable_transition_contract_violation"
            errors.append(f"{prior.attempt_id}:{lane.value}:{message}")

        def place_transition(
            prior: CoordinatorWork,
            updated: CoordinatorWork,
            lane: CoordinatorLane,
        ) -> bool:
            if updated.attempt_id != prior.attempt_id:
                preserve_contract_violation(
                    prior, updated, lane, "attempt identity changed"
                )
                return False
            if (prior.state, updated.state) not in _ALLOWED_LANE_TRANSITIONS[lane]:
                preserve_contract_violation(
                    prior,
                    updated,
                    lane,
                    f"illegal transition {prior.state}->{updated.state}",
                )
                return False
            if updated.lifecycle_version != prior.lifecycle_version + 1:
                preserve_contract_violation(
                    prior,
                    updated,
                    lane,
                    "lifecycle version did not advance exactly once",
                )
                return False
            if updated.claim_generation != prior.claim_generation:
                preserve_contract_violation(
                    prior, updated, lane, "claim generation changed during stage"
                )
                return False
            if updated.state in _FINAL_STATES:
                if (
                    updated.claim_owner_identity is not None
                    or updated.lease_expires_monotonic is not None
                ):
                    preserve_contract_violation(
                        prior, updated, lane, "final state retained a live claim"
                    )
                    return False
            elif (
                updated.claim_owner_identity != prior.claim_owner_identity
                or updated.lease_expires_monotonic is None
            ):
                preserve_contract_violation(
                    prior, updated, lane, "stage changed or dropped claim ownership"
                )
                return False
            if (
                updated.state not in _FINAL_STATES
                and updated.credit_reservation != prior.credit_reservation
            ):
                preserve_contract_violation(
                    prior, updated, lane, "stage changed lifecycle reservation"
                )
                return False
            if (
                updated.state in _FINAL_STATES
                and updated.credit_reservation != ResourceCreditVector()
            ):
                preserve_contract_violation(
                    prior, updated, lane, "final state retained lifecycle reservation"
                )
                return False
            try:
                place(
                    updated,
                    recovery=updated.attempt_id in oversubscribed_recovery,
                )
            except ValueError as exc:
                preserve_contract_violation(prior, updated, lane, str(exc))
                return False
            return True

        def renew(work: CoordinatorWork, lane: CoordinatorLane) -> CoordinatorWork:
            renewed = self._backend.renew_claim(
                work, lease_seconds=self._limits.claim_lease_seconds
            )
            if (
                renewed.attempt_id != work.attempt_id
                or renewed.state != work.state
                or renewed.lifecycle_version != work.lifecycle_version
                or renewed.claim_generation != work.claim_generation
                or renewed.claim_owner_identity != work.claim_owner_identity
                or renewed.credit_reservation != work.credit_reservation
                or renewed.credits != work.credits
                or renewed.lease_expires_monotonic is None
                or work.lease_expires_monotonic is None
                or renewed.lease_expires_monotonic <= work.lease_expires_monotonic
            ):
                preserve_contract_violation(
                    work, renewed, lane, "claim renewal changed durable work"
                )
                raise RuntimeError("claim renewal contract violation")
            minimum_lease = (
                self._limits.max_stage_step_seconds
                + self._limits.claim_renew_margin_seconds
            )
            if renewed.lease_expires_monotonic - self._monotonic() <= minimum_lease:
                preserve_contract_violation(
                    work,
                    renewed,
                    lane,
                    "claim renewal cannot cover the bounded stage",
                )
                raise RuntimeError("claim renewal is too short for the bounded stage")
            known[work.attempt_id] = renewed
            return renewed

        def needs_renewal(work: CoordinatorWork, now: float) -> bool:
            return (
                work.lease_expires_monotonic is None
                or work.lease_expires_monotonic - now
                <= self._limits.max_stage_step_seconds
                + self._limits.claim_renew_margin_seconds
            )

        def fail_waiting_renewal(
            work: CoordinatorWork,
            lane: CoordinatorLane,
            kind: str,
            exc: Exception,
        ) -> None:
            nonlocal circuit_open, admission_open, blocked_reason
            circuit_open = True
            admission_open = False
            blocked_reason = "claim_renewal_failed"
            errors.append(
                f"{work.attempt_id}:{lane.value}:{kind}:"
                f"{type(exc).__name__}:{exc}"
            )

        def renew_waiting(
            work: CoordinatorWork,
            lane: CoordinatorLane,
        ) -> CoordinatorWork | None:
            """Renew one waiting claim, reconciling a lost renewal response."""

            try:
                return renew(work, lane)
            except Exception as exc:  # noqa: BLE001 - reconcile commit race
                if circuit_open:
                    return None
                try:
                    durable = self._backend.reload_claim(work)
                except Exception as reload_exc:  # noqa: BLE001
                    fail_waiting_renewal(
                        work,
                        lane,
                        "claim-reload-wait",
                        reload_exc,
                    )
                    return None
                threshold = (
                    self._limits.max_stage_step_seconds
                    + self._limits.claim_renew_margin_seconds
                )
                if (
                    durable.attempt_id == work.attempt_id
                    and durable.state == work.state
                    and durable.lifecycle_version == work.lifecycle_version
                    and durable.claim_generation == work.claim_generation
                    and durable.claim_owner_identity == work.claim_owner_identity
                    and durable.credit_reservation == work.credit_reservation
                    and durable.credits == work.credits
                    and durable.lease_expires_monotonic is not None
                    and work.lease_expires_monotonic is not None
                    and durable.lease_expires_monotonic
                    > work.lease_expires_monotonic
                    and durable.lease_expires_monotonic - self._monotonic()
                    > threshold
                ):
                    known[work.attempt_id] = durable
                    return durable
                fail_waiting_renewal(work, lane, "claim-wait", exc)
                return None

        def guard_waiting(now: float) -> None:
            """Keep every own-claimed queue/retry owner live without busy scans."""

            nonlocal waiting_renewal_floor
            threshold = (
                self._limits.max_stage_step_seconds
                + self._limits.claim_renew_margin_seconds
            )
            if circuit_open or now + threshold < waiting_renewal_floor:
                return
            next_floor = float("inf")
            for lane in CoordinatorLane:
                queue = queues[lane]
                for index in range(len(queue)):
                    work = queue[index]
                    if needs_renewal(work, now):
                        renewed = renew_waiting(work, lane)
                        if renewed is None:
                            return
                        queue[index] = work = renewed
                    assert work.lease_expires_monotonic is not None
                    next_floor = min(next_floor, work.lease_expires_monotonic)
            for attempt_id, (ready_at, work) in tuple(retry_at.items()):
                try:
                    retry_lane = _LANE_BY_STATE[work.state]
                except KeyError as exc:
                    raise RuntimeError(
                        "retry work has unsupported durable state"
                    ) from exc
                if needs_renewal(work, now):
                    renewed = renew_waiting(work, retry_lane)
                    if renewed is None:
                        return
                    retry_at[attempt_id] = (ready_at, renewed)
                    work = renewed
                assert work.lease_expires_monotonic is not None
                next_floor = min(next_floor, work.lease_expires_monotonic)
            waiting_renewal_floor = next_floor

        def bounded_defer_delay(
            work: CoordinatorWork,
            *,
            now: float,
            requested_seconds: float,
        ) -> float:
            configured_max_wait = (
                self._limits.claim_lease_seconds
                - self._limits.max_stage_step_seconds
                - self._limits.claim_renew_margin_seconds
            )
            lease_remaining = (
                (work.lease_expires_monotonic or now)
                - now
                - self._limits.claim_renew_margin_seconds
            )
            safe_wait = max(
                self._limits.poll_seconds,
                min(configured_max_wait, lease_remaining),
            )
            return min(requested_seconds, safe_wait)

        def exceeded_dimensions(
            used: ResourceCreditVector,
            limit: ResourceCreditVector,
        ) -> tuple[str, ...]:
            return tuple(
                item.name
                for item in fields(ResourceCreditVector)
                if getattr(used, item.name) > getattr(limit, item.name)
            )

        def positive_dimensions(value: ResourceCreditVector) -> tuple[str, ...]:
            return tuple(
                item.name
                for item in fields(ResourceCreditVector)
                if getattr(value, item.name) > 0
            )

        def transition_hold(work: CoordinatorWork) -> ResourceCreditVector:
            names_by_state = {
                "prepared": {"remote_waits"},
                "reconciling": {"provider_tasks", "ack_items"},
                "submitted": {"provider_result_bytes"},
                "remote_terminal": {
                    "materialization_items",
                    "compressed_bytes",
                    "decoded_bytes",
                    "temp_disk_bytes",
                },
                "materializing": {
                    "output_items",
                    "output_bytes",
                    "output_pages",
                },
                "local_materialized": set(),
                "publish_committed": set(),
                "cleanup_pending": set(),
                "ack_pending": set(),
            }
            try:
                names = names_by_state[work.state]
            except KeyError as exc:
                raise RuntimeError(
                    f"unsupported credit transition source: {work.state}"
                ) from exc
            return ResourceCreditVector(
                **{
                    item.name: (
                        max(
                            0,
                            getattr(work.credit_reservation, item.name)
                            - getattr(work.credits, item.name),
                        )
                        if item.name in names
                        else 0
                    )
                    for item in fields(ResourceCreditVector)
                }
            )

        def guard_in_flight(now: float) -> None:
            nonlocal circuit_open, admission_open, blocked_reason
            for future, (lane, work, stage_guard, _grant) in tuple(in_flight.items()):
                if future.done():
                    continue
                if now >= stage_guard.deadline_monotonic:
                    if future not in in_flight_failures:
                        circuit_open = True
                        admission_open = False
                        blocked_reason = "bounded_stage_deadline_exceeded"
                        errors.append(
                            f"{work.attempt_id}:{lane.value}:stage deadline exceeded"
                        )
                        in_flight_failures.add(future)
                        stage_guard.revoke()
                    # Stop extending ownership after the backend broke its
                    # bounded-step contract. Its lease/fence checks must then
                    # prevent any further side effect.
                    continue
                if future in reconciled_results:
                    continue
                if not needs_renewal(work, now):
                    continue
                try:
                    renewed = renew(work, lane)
                except Exception as exc:  # noqa: BLE001 - reconcile commit race
                    if future.done():
                        continue
                    try:
                        durable = self._backend.reload_claim(work)
                    except Exception as reload_exc:  # noqa: BLE001
                        if future not in in_flight_failures:
                            circuit_open = True
                            admission_open = False
                            blocked_reason = "in_flight_claim_reconcile_failed"
                            errors.append(
                                f"{work.attempt_id}:{lane.value}:claim-reload:"
                                f"{type(reload_exc).__name__}:{reload_exc}"
                            )
                            in_flight_failures.add(future)
                            stage_guard.revoke()
                        continue
                    if (
                        durable.attempt_id == work.attempt_id
                        and durable.claim_generation == work.claim_generation
                        and durable.claim_owner_identity == work.claim_owner_identity
                        and durable.state == work.state
                        and durable.lifecycle_version == work.lifecycle_version
                        and durable.credits == work.credits
                        and durable.credit_reservation == work.credit_reservation
                        and durable.lease_expires_monotonic is not None
                        and work.lease_expires_monotonic is not None
                        and durable.lease_expires_monotonic
                        > work.lease_expires_monotonic
                        and durable.lease_expires_monotonic
                        > stage_guard.deadline_monotonic
                        + self._limits.claim_renew_margin_seconds
                    ):
                        known[work.attempt_id] = durable
                        ledger.replace(
                            durable,
                            allow_oversubscribed=(
                                durable.attempt_id in oversubscribed_recovery
                            ),
                        )
                        in_flight[future] = (lane, durable, stage_guard, _grant)
                    elif (
                        durable.attempt_id == work.attempt_id
                        and durable.claim_generation == work.claim_generation
                        and (work.state, durable.state)
                        in _ALLOWED_LANE_TRANSITIONS[lane]
                        and durable.lifecycle_version == work.lifecycle_version + 1
                    ):
                        # The stage committed and its response raced renewal.
                        # Consume the exact durable projection when the bounded
                        # future returns; never dispatch a duplicate side effect.
                        reconciled_results[future] = durable
                    elif future not in in_flight_failures:
                        circuit_open = True
                        admission_open = False
                        blocked_reason = "in_flight_claim_lost"
                        errors.append(
                            f"{work.attempt_id}:{lane.value}:claim:"
                            f"{type(exc).__name__}:{exc}"
                        )
                        in_flight_failures.add(future)
                        stage_guard.revoke()
                else:
                    in_flight[future] = (lane, renewed, stage_guard, _grant)

        try:
            # Startup is an exhaustive keyset recovery barrier.  No new work
            # is read until every current nonfinal attempt has been claimed or
            # placed on an explicit deferred-claim timer.
            after: str | None = None
            barrier_exhausted = False
            deferred_claims: dict[str, tuple[float, RecoveryCandidate]] = {}
            while True:
                page = tuple(
                    self._backend.list_recoverable(
                        after_attempt_id=after,
                        limit=self._limits.recovery_page_size,
                    )
                )
                if not page:
                    barrier_exhausted = True
                    break
                if any(type(item) is not RecoveryCandidate for item in page):
                    raise RuntimeError(
                        "recovery page is not a candidate projection"
                    )
                ids = [recovery_candidate.attempt_id for recovery_candidate in page]
                if (
                    ids != sorted(ids)
                    or len(ids) != len(set(ids))
                    or (after is not None and ids[0] <= after)
                ):
                    raise RuntimeError("recovery keyset page is not strictly ordered")
                for recovery_candidate in page:
                    try:
                        place(
                            self._backend.claim_recovery(recovery_candidate),
                            recovery=True,
                        )
                    except RecoveryDeferred as exc:
                        if (
                            exc.durable_work.attempt_id
                            != recovery_candidate.attempt_id
                            or exc.durable_work.state in _FINAL_STATES
                            or exc.durable_work.claim_owner_identity is None
                        ):
                            raise RuntimeError(
                                "deferred recovery returned a foreign or final projection"
                        )
                        reserve_deferred(exc.durable_work)
                        deferred_claims[recovery_candidate.attempt_id] = (
                            self._monotonic() + exc.retry_after_seconds,
                            recovery_candidate,
                        )
                    guard_waiting(self._monotonic())
                    if circuit_open:
                        break
                if circuit_open:
                    break
                after = ids[-1]
                if len(page) < self._limits.recovery_page_size:
                    barrier_exhausted = True
                    break

            recovery_complete = barrier_exhausted and not deferred_claims
            admission_open = (
                recovery_complete and not circuit_open and not stop_requested()
            )
            if admission_open:
                blocked_reason = None
            elif not circuit_open:
                blocked_reason = "admission_closed"
            emit()

            while True:
                now = self._monotonic()
                if stop_requested():
                    admission_open = False
                    blocked_reason = "draining"
                guard_waiting(now)

                for attempt_id, (ready_at, recovery_candidate) in tuple(
                    deferred_claims.items()
                ):
                    if stop_requested() or circuit_open:
                        break
                    if ready_at > now:
                        continue
                    try:
                        claimed = self._backend.claim_recovery(recovery_candidate)
                    except RecoveryDeferred as exc:
                        if (
                            exc.durable_work.attempt_id
                            != recovery_candidate.attempt_id
                            or exc.durable_work.state in _FINAL_STATES
                            or exc.durable_work.claim_owner_identity is None
                        ):
                            raise RuntimeError(
                                "deferred recovery returned a foreign or final projection"
                            )
                        reserve_deferred(exc.durable_work)
                        deferred_claims[attempt_id] = (
                            now + exc.retry_after_seconds,
                            recovery_candidate,
                        )
                    else:
                        deferred_claims.pop(attempt_id, None)
                        place(claimed, recovery=True)
                        last_progress = now
                recovery_complete = barrier_exhausted and not deferred_claims
                if (
                    recovery_complete
                    and not retry_degraded
                    and not circuit_open
                    and not stop_requested()
                ):
                    admission_open = True
                    if blocked_reason == "recovery_barrier":
                        blocked_reason = None
                elif (
                    not recovery_complete
                    and not circuit_open
                    and not stop_requested()
                ):
                    admission_open = False
                    blocked_reason = "recovery_barrier"

                for attempt_id, (ready_at, work) in tuple(retry_at.items()):
                    if ready_at <= now:
                        retry_at.pop(attempt_id)
                        lane = _LANE_BY_STATE.get(work.state)
                        if lane is None:
                            raise RuntimeError(
                                "retry work has unsupported durable state"
                            )
                        queues[lane].append(work)

                active_ids = set(known)
                if admission_open and not circuit_open:
                    if oversubscribed_recovery or not ledger.in_use.fits(ledger.limit):
                        blocked_reason = "oversubscribed_recovery_drain"
                    else:
                        available_credits = ledger.limit - ledger.in_use
                        saturated = tuple(
                            item.name
                            for item in fields(ResourceCreditVector)
                            if getattr(available_credits, item.name) == 0
                        )
                        capacity = min(
                            self._limits.admission_batch_size,
                            available_credits.documents,
                        )
                        if capacity == 0:
                            admission_blocked_dimensions = saturated or ("documents",)
                            admission_blocked_available = available_credits
                            blocked_reason = "credit_backpressure:" + ",".join(
                                admission_blocked_dimensions
                            )
                        elif (
                            admission_blocked_dimensions
                            and admission_blocked_available is not None
                            and not any(
                                getattr(available_credits, name)
                                > getattr(admission_blocked_available, name)
                                for name in admission_blocked_dimensions
                            )
                        ):
                            blocked_reason = "credit_backpressure:" + ",".join(
                                admission_blocked_dimensions
                            )
                        else:
                            if blocked_reason is not None and (
                                blocked_reason.startswith("credit_backpressure:")
                                or blocked_reason == "oversubscribed_recovery_drain"
                            ):
                                blocked_reason = None
                            try:
                                admission = self._backend.admit_new(
                                    limit=capacity,
                                    available_credits=available_credits,
                                )
                            except AdmissionInterrupted as exc:
                                for work in exc.claimed_work:
                                    if (
                                        work.attempt_id in active_ids
                                        or work.attempt_id in final
                                    ):
                                        raise RuntimeError(
                                            "interrupted admission duplicated an active attempt"
                                        ) from exc
                                    # These claims are already durable. Account their
                                    # exact reservation even when it exceeds the grant;
                                    # recovery on the next boot will see the same rows.
                                    place(work, recovery=True)
                                    active_ids.add(work.attempt_id)
                                    admitted += 1
                                    last_progress = now
                                circuit_open = True
                                admission_open = False
                                blocked_reason = "admission_interrupted"
                                errors.append(
                                    "admission interrupted after "
                                    f"{len(exc.claimed_work)} durable claim(s) "
                                    f"[{','.join(work.attempt_id for work in exc.claimed_work)}]:"
                                    f"{exc}"
                                )
                            else:
                                if type(admission) is not AdmissionOutcome:
                                    raise RuntimeError(
                                        "backend returned an invalid admission outcome"
                                    )
                                admitted_batch = admission.work
                                if len(admitted_batch) > capacity:
                                    raise RuntimeError(
                                        "backend exceeded its admission count grant"
                                    )
                                for work in admitted_batch:
                                    if (
                                        work.attempt_id in active_ids
                                        or work.attempt_id in final
                                    ):
                                        raise RuntimeError(
                                            "new admission duplicated an active attempt"
                                        )
                                    valid_initial_shape = (
                                        work.state == "prepared"
                                        and work.lifecycle_version == 0
                                    )
                                    if (
                                        not valid_initial_shape
                                        or not ledger.can_add(work)
                                        or not work.credit_reservation.fits(ledger.limit)
                                    ):
                                        # ``admit_new`` has already durably created and
                                        # claimed the attempt. Preserve it and drain it;
                                        # never drop a backend contract violation.
                                        place(work, recovery=True)
                                        circuit_open = True
                                        admission_open = False
                                        blocked_reason = (
                                            "admission_initial_state_contract_violation"
                                            if not valid_initial_shape
                                            else "admission_credit_contract_violation"
                                        )
                                        errors.append(
                                            f"{work.attempt_id}:admission returned an invalid "
                                            "initial state"
                                            if not valid_initial_shape
                                            else f"{work.attempt_id}:admission exceeded credit grant"
                                        )
                                    else:
                                        place(work, recovery=False)
                                    active_ids.add(work.attempt_id)
                                    admitted += 1
                                    last_progress = now
                                if (
                                    admission.backlog_exists
                                    and admission.blocked_dimensions
                                ):
                                    admission_blocked_dimensions = (
                                        admission.blocked_dimensions
                                    )
                                    admission_blocked_available = (
                                        ledger.limit - ledger.in_use
                                    )
                                    blocked_reason = "credit_backpressure:" + ",".join(
                                        admission_blocked_dimensions
                                    )
                                else:
                                    admission_blocked_dimensions = ()
                                    admission_blocked_available = None

                # Claims returned by recovery/admission may have less time
                # remaining than this coordinator's configured lease.  Guard
                # them before any lane selection or poll sleep, not merely on
                # the next loop turn.
                guard_waiting(self._monotonic())
                credit_blocked_by_lane = {lane: set() for lane in CoordinatorLane}
                for lane in _LANE_PRIORITY:
                    if circuit_open:
                        break
                    active = sum(
                        1
                        for active_lane, _, _, _ in in_flight.values()
                        if active_lane == lane
                    )
                    while queues[lane] and active < lane_limits[lane]:
                        queue = queues[lane]
                        priorities = _STATE_PRIORITY_WITHIN_LANE.get(lane)
                        ordered_indices = sorted(
                            range(len(queue)),
                            key=lambda candidate: (
                                priorities.get(queue[candidate].state, len(priorities))
                                if priorities
                                else 0,
                                candidate,
                            ),
                        )
                        selected: (
                            tuple[int, CoordinatorWork, ResourceCreditVector] | None
                        ) = None
                        first_shortages: tuple[str, ...] = ()
                        for position, index in enumerate(ordered_indices):
                            queued_work = queue[index]
                            candidate_hold = transition_hold(queued_work)
                            if position > 0 and any(
                                getattr(candidate_hold, name) > 0
                                for name in first_shortages
                            ):
                                continue
                            if queued_work.attempt_id in oversubscribed_recovery:
                                shortages = (
                                    positive_dimensions(candidate_hold)
                                    if candidate_hold != ResourceCreditVector()
                                    and provisional_local
                                    else ()
                                )
                            else:
                                shortages = exceeded_dimensions(
                                    ledger.in_use
                                    + provisional_local_total
                                    + candidate_hold,
                                    ledger.limit,
                                )
                            if shortages:
                                if position == 0:
                                    first_shortages = shortages
                                    credit_blocked_by_lane[lane].update(shortages)
                                continue
                            selected = (index, queued_work, candidate_hold)
                            break
                        if selected is None:
                            break
                        index, work, local_hold = selected
                        del queue[index]
                        try:
                            if needs_renewal(work, now):
                                work = renew(work, lane)
                        except Exception as exc:  # noqa: BLE001 - claim loss is fatal
                            circuit_open = True
                            admission_open = False
                            blocked_reason = "claim_renewal_failed"
                            errors.append(
                                f"{work.attempt_id}:{lane.value}:claim:{type(exc).__name__}:{exc}"
                            )
                            break
                        stage_guard = StageLeaseGuard(
                            deadline_monotonic=(
                                now + self._limits.max_stage_step_seconds
                            ),
                            _revoked=Event(),
                            _monotonic=self._monotonic,
                        )
                        if lane == CoordinatorLane.PREFLIGHT:
                            future = pools[lane].submit(
                                self._backend.prepare_remote_io,
                                work,
                                credit_allowance=local_hold,
                                stage_guard=stage_guard,
                            )
                        elif lane == CoordinatorLane.REMOTE:
                            future = pools[lane].submit(
                                self._backend.run_remote,
                                work,
                                credit_allowance=local_hold,
                                stage_guard=stage_guard,
                            )
                        elif lane == CoordinatorLane.LOCAL_PREPARE:
                            future = pools[lane].submit(
                                self._backend.prepare_local_io,
                                work,
                                credit_allowance=local_hold,
                                stage_guard=stage_guard,
                            )
                        elif lane == CoordinatorLane.LOCAL:
                            future = pools[lane].submit(
                                self._backend.run_local,
                                work,
                                credit_allowance=local_hold,
                                stage_guard=stage_guard,
                            )
                        elif lane == CoordinatorLane.COMMIT:
                            future = pools[lane].submit(
                                self._backend.commit,
                                work,
                                credit_allowance=local_hold,
                                stage_guard=stage_guard,
                            )
                        elif lane == CoordinatorLane.CLEANUP:
                            future = pools[lane].submit(
                                self._backend.cleanup,
                                work,
                                credit_allowance=local_hold,
                                stage_guard=stage_guard,
                            )
                        else:
                            future = pools[lane].submit(
                                self._backend.acknowledge,
                                work,
                                stage_guard=stage_guard,
                            )
                        in_flight[future] = (lane, work, stage_guard, local_hold)
                        if local_hold != ResourceCreditVector():
                            provisional_local[future] = local_hold
                            provisional_local_total = (
                                provisional_local_total + local_hold
                            )
                        active += 1

                if not in_flight:
                    if (
                        any(queues.values())
                        and not circuit_open
                        and not retry_at
                        and not deferred_claims
                    ):
                        circuit_open = True
                        admission_open = False
                        blocked_reason = "resource_credit_grant_unavailable"
                        errors.append(
                            "durable queued work cannot obtain its next credit grant"
                        )
                    if (
                        recovery_complete
                        and not circuit_open
                        and not any(queues.values())
                        and not retry_at
                        and not known
                    ):
                        emit()
                        return CoordinatorResult(
                            terminal=CoordinatorTerminal.QUIESCENT,
                            recovery_complete=recovery_complete,
                            admitted=admitted,
                            completed=completed,
                            final_states=tuple(sorted(final.items())),
                            errors=tuple(errors),
                            credits_in_use=ledger.in_use,
                        )
                    if circuit_open or (
                        stop_requested()
                        and now - last_progress
                        >= self._limits.idle_open_circuit_seconds
                    ):
                        emit()
                        return CoordinatorResult(
                            terminal=CoordinatorTerminal.STUCK_OPEN_CIRCUIT,
                            recovery_complete=recovery_complete,
                            admitted=admitted,
                            completed=completed,
                            final_states=tuple(sorted(final.items())),
                            errors=tuple(errors),
                            credits_in_use=ledger.in_use,
                        )
                    time.sleep(self._limits.poll_seconds)
                    emit()
                    continue

                done, _ = wait(
                    tuple(in_flight),
                    timeout=self._limits.poll_seconds,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    guard_in_flight(self._monotonic())
                    emit()
                    continue
                for future in done:
                    lane, work, stage_guard, granted_delta = in_flight.pop(future)
                    released_local_hold = (
                        provisional_local.pop(future)
                        if future in provisional_local
                        else None
                    )
                    if released_local_hold is not None:
                        provisional_local_total = (
                            provisional_local_total - released_local_hold
                        )
                    try:
                        if future in reconciled_results:
                            # Observe the future exception/result only to drain
                            # thread ownership; durable reload is authoritative.
                            try:
                                future.result()
                            except Exception:  # noqa: BLE001
                                pass
                            updated = reconciled_results.pop(future)
                        else:
                            updated = future.result()
                    except StageWaiting as exc:
                        wait_now = self._monotonic()
                        retry_at[work.attempt_id] = (
                            wait_now
                            + bounded_defer_delay(
                                work,
                                now=wait_now,
                                requested_seconds=exc.retry_after_seconds,
                            ),
                            work,
                        )
                        track_waiting_lease(work)
                        last_progress = wait_now
                    except RetryStage as exc:
                        retry_now = self._monotonic()
                        count = retry_attempts.get(work.attempt_id, 0) + 1
                        retry_attempts[work.attempt_id] = count
                        started = retry_started_at.setdefault(
                            work.attempt_id, retry_now
                        )
                        consecutive_retries += 1
                        if (
                            count > self._limits.retry_max_attempts
                            or retry_now - started >= self._limits.retry_stuck_seconds
                        ):
                            circuit_open = True
                            admission_open = False
                            blocked_reason = "retry_stage_stuck"
                            errors.append(
                                f"{work.attempt_id}:{lane.value}:retry budget exhausted"
                            )
                            continue
                        exponent = min(count - 1, 30)
                        backoff = min(
                            self._limits.retry_max_backoff_seconds,
                            max(
                                exc.retry_after_seconds,
                                self._limits.retry_initial_backoff_seconds
                                * (2**exponent),
                            ),
                        )
                        retry_at[work.attempt_id] = (
                            retry_now
                            + bounded_defer_delay(
                                work,
                                now=retry_now,
                                requested_seconds=backoff,
                            ),
                            work,
                        )
                        track_waiting_lease(work)
                        last_progress = retry_now
                        if (
                            consecutive_retries
                            >= self._limits.retry_consecutive_threshold
                        ):
                            retry_degraded = True
                            admission_open = False
                            blocked_reason = "retry_degraded"
                    except StageLeaseLost as exc:
                        circuit_open = True
                        admission_open = False
                        blocked_reason = "bounded_stage_deadline_exceeded"
                        errors.append(
                            f"{work.attempt_id}:{lane.value}:stage deadline exceeded:{exc}"
                        )
                        in_flight_failures.add(future)
                    except Exception as exc:  # noqa: BLE001 - opens the circuit visibly
                        circuit_open = True
                        admission_open = False
                        blocked_reason = f"{lane.value}_unexpected_failure"
                        errors.append(
                            f"{work.attempt_id}:{lane.value}:{type(exc).__name__}:{exc}"
                        )
                    else:
                        if self._monotonic() >= stage_guard.deadline_monotonic:
                            preserve_contract_violation(
                                work, updated, lane, "bounded stage exceeded deadline"
                            )
                        elif not _positive_credit_delta(
                            work.credits, updated.credits
                        ).fits(granted_delta):
                            preserve_contract_violation(
                                work,
                                updated,
                                lane,
                                "durable transition exceeded its credit grant",
                            )
                        elif place_transition(work, updated, lane):
                            retry_attempts.pop(work.attempt_id, None)
                            retry_started_at.pop(work.attempt_id, None)
                            consecutive_retries = 0
                            retry_degraded = False
                            last_progress = self._monotonic()
                guard_in_flight(self._monotonic())
                emit()
        finally:
            # Running IO is never cancelled.  Executor shutdown waits for every
            # owner that entered a thread, preserving the drain contract.
            for pool in pools.values():
                pool.shutdown(wait=True, cancel_futures=False)


__all__ = [
    "AdmissionInterrupted",
    "AdmissionOutcome",
    "CoordinatorLane",
    "CoordinatorLimits",
    "CoordinatorResult",
    "CoordinatorSnapshot",
    "CoordinatorTerminal",
    "CoordinatorWork",
    "ResourceCreditVector",
    "RecoveryCandidate",
    "RecoveryDeferred",
    "RetryStage",
    "StageLeaseGuard",
    "StageLeaseLost",
    "StageWaiting",
    "StagedCoordinatorBackend",
    "StagedParseCoordinator",
]
