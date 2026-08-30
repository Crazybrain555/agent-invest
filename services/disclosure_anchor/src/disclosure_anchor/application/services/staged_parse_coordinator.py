"""Crash-safe, work-conserving coordinator for staged whole-PDF parsing.

The coordinator owns scheduling only.  The backend owns every durable state
transition and all provider/database IO.  In particular, ``prepared`` work is
sent only to ``prepare_remote_io``; that operation must finish all local
preflight and durably commit ``reconciling`` before the coordinator permits
the first provider lookup or POST.

There is deliberately no cancelled completion state.  Closing admission lets
already accepted work drain through durable finish/failure and remote ACK.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, fields
from enum import Enum
from threading import Event
import time
from typing import Protocol


class CoordinatorLane(str, Enum):
    PREFLIGHT = "preflight"
    REMOTE = "remote"
    LOCAL_PREPARE = "local_prepare"
    LOCAL = "local"
    COMMIT = "commit"
    ACK = "ack"


class CoordinatorTerminal(str, Enum):
    QUIESCENT = "quiescent"
    STUCK_OPEN_CIRCUIT = "stuck_open_circuit"


@dataclass(frozen=True, slots=True)
class CreditVector:
    """Non-fungible process credits held by durable work.

    A byte in one dimension can never pay for another dimension.  Values are
    exact ownership projections, not utilization estimates.
    """

    documents: int = 0
    remote_waits: int = 0
    retained_results: int = 0
    retained_bytes: int = 0
    local_items: int = 0
    compressed_bytes: int = 0
    decoded_bytes: int = 0
    temp_disk_bytes: int = 0
    db_stage_items: int = 0
    ack_items: int = 0
    unpublished_pages: int = 0

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"credit {item.name} must be a non-negative integer")

    def __add__(self, other: CreditVector) -> CreditVector:
        return CreditVector(
            **{
                item.name: getattr(self, item.name) + getattr(other, item.name)
                for item in fields(self)
            }
        )

    def __sub__(self, other: CreditVector) -> CreditVector:
        values = {
            item.name: getattr(self, item.name) - getattr(other, item.name)
            for item in fields(self)
        }
        if any(value < 0 for value in values.values()):
            raise ValueError("credit release would make ownership negative")
        return CreditVector(**values)

    def fits(self, limit: CreditVector) -> bool:
        return all(
            getattr(self, item.name) <= getattr(limit, item.name)
            for item in fields(self)
        )

    def nonzero(self) -> dict[str, int]:
        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if getattr(self, item.name)
        }


def _positive_credit_delta(before: CreditVector, after: CreditVector) -> CreditVector:
    return CreditVector(
        **{
            item.name: max(0, getattr(after, item.name) - getattr(before, item.name))
            for item in fields(CreditVector)
        }
    )


@dataclass(frozen=True, slots=True)
class CoordinatorWork:
    """Content-free projection of one claimed durable attempt."""

    attempt_id: str
    state: str
    row_version: int
    claim_generation: int
    claim_owner_identity: str | None
    lease_expires_monotonic: float | None
    credit_reservation: CreditVector
    credits: CreditVector

    def __post_init__(self) -> None:
        if not self.attempt_id.strip() or len(self.attempt_id) > 128:
            raise ValueError("coordinator attempt identity is invalid")
        if not self.state.strip() or len(self.state) > 64:
            raise ValueError("coordinator work state is invalid")
        for value, label in (
            (self.row_version, "row version"),
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
            or self.credit_reservation != CreditVector()
            or self.credits != CreditVector()
        ):
            raise ValueError(
                "final coordinator work must release its claim and all credits"
            )


class RecoveryDeferred(RuntimeError):
    """A live durable claim cannot be taken until its lease becomes available."""

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        if retry_after_seconds <= 0:
            raise ValueError("recovery retry delay must be positive")
        self.retry_after_seconds = float(retry_after_seconds)


class RetryStage(RuntimeError):
    """The backend preserved durable state and requests a bounded retry."""

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        if retry_after_seconds <= 0:
            raise ValueError("stage retry delay must be positive")
        self.retry_after_seconds = float(retry_after_seconds)


class StageLeaseLost(RuntimeError):
    """A bounded backend step lost its execution fence before a side effect."""


@dataclass(frozen=True, slots=True)
class StageLeaseGuard:
    """Cooperative hard boundary checked around every backend IO chunk."""

    deadline_monotonic: float
    _revoked: Event
    _monotonic: Callable[[], float]

    def checkpoint(self) -> None:
        if self._revoked.is_set() or self._monotonic() > self.deadline_monotonic:
            raise StageLeaseLost("bounded stage lease expired")

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
    ) -> Sequence[CoordinatorWork]: ...

    def claim_recovery(self, work: CoordinatorWork) -> CoordinatorWork: ...

    def admit_new(
        self, *, limit: int, available_credits: CreditVector
    ) -> Sequence[CoordinatorWork]:
        """Create/claim only work whose aggregate credits fit the allowance."""

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
        credit_allowance: CreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        """Run local-only preflight, then durably enter reconciling."""

    def run_remote(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: CreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork: ...

    def prepare_local_io(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: CreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        """Durably enter materializing and reserve exact local projections."""

    def run_local(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: CreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork: ...

    def commit(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: CreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork: ...

    def acknowledge(
        self, work: CoordinatorWork, *, stage_guard: StageLeaseGuard
    ) -> CoordinatorWork: ...


@dataclass(frozen=True, slots=True)
class CoordinatorLimits:
    credits: CreditVector
    recovery_page_size: int = 128
    admission_batch_size: int = 32
    preflight_workers: int = 1
    remote_workers: int = 1
    local_prepare_workers: int = 1
    local_workers: int = 2
    commit_workers: int = 2
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
            (self.ack_workers, "ACK workers"),
            (self.claim_lease_seconds, "claim lease seconds"),
            (self.retry_consecutive_threshold, "retry consecutive threshold"),
            (self.retry_max_attempts, "retry max attempts"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if not 1 <= self.claim_lease_seconds <= 300:
            raise ValueError("claim lease seconds must fit the DB 1..300 contract")
        if not 0 < self.claim_renew_margin_seconds < self.claim_lease_seconds:
            raise ValueError("claim renewal margin must be inside the lease")
        if (
            self.max_stage_step_seconds <= 0
            or self.max_stage_step_seconds + self.claim_renew_margin_seconds
            >= self.claim_lease_seconds
        ):
            raise ValueError("bounded stage plus renewal margin must fit the lease")
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
    credits_in_use: CreditVector
    credits_limit: CreditVector
    completed: int
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class CoordinatorResult:
    terminal: CoordinatorTerminal
    recovery_complete: bool
    admitted: int
    completed: int
    final_states: tuple[tuple[str, str], ...]
    errors: tuple[str, ...]
    credits_in_use: CreditVector


_FINAL_STATES = frozenset(
    {"acked", "remote_failed", "local_failed", "pre_submission_failed", "superseded"}
)
_LANE_BY_STATE = {
    "prepared": CoordinatorLane.PREFLIGHT,
    "reconciling": CoordinatorLane.REMOTE,
    "submitted": CoordinatorLane.REMOTE,
    "remote_terminal": CoordinatorLane.LOCAL_PREPARE,
    "materializing": CoordinatorLane.LOCAL,
    "local_materialized": CoordinatorLane.COMMIT,
    "finish_committed": CoordinatorLane.ACK,
    "remote_failure_committed": CoordinatorLane.ACK,
    "local_failure_committed": CoordinatorLane.ACK,
}
_LANE_PRIORITY = (
    CoordinatorLane.ACK,
    CoordinatorLane.COMMIT,
    CoordinatorLane.LOCAL,
    CoordinatorLane.LOCAL_PREPARE,
    CoordinatorLane.REMOTE,
    CoordinatorLane.PREFLIGHT,
)
_ALLOWED_LANE_TRANSITIONS = {
    CoordinatorLane.PREFLIGHT: frozenset(
        {("prepared", "reconciling"), ("prepared", "pre_submission_failed")}
    ),
    CoordinatorLane.REMOTE: frozenset(
        {
            ("reconciling", "submitted"),
            ("submitted", "remote_terminal"),
            ("submitted", "remote_failure_committed"),
        }
    ),
    CoordinatorLane.LOCAL_PREPARE: frozenset(
        {
            ("remote_terminal", "materializing"),
            ("remote_terminal", "local_failure_committed"),
        }
    ),
    CoordinatorLane.LOCAL: frozenset(
        {
            ("materializing", "local_materialized"),
            ("materializing", "local_failure_committed"),
        }
    ),
    CoordinatorLane.COMMIT: frozenset(
        {("local_materialized", "finish_committed")}
    ),
    CoordinatorLane.ACK: frozenset(
        {
            ("finish_committed", "acked"),
            ("remote_failure_committed", "remote_failed"),
            ("local_failure_committed", "local_failed"),
        }
    ),
}


class _CreditLedger:
    def __init__(self, limit: CreditVector) -> None:
        self.limit = limit
        self.in_use = CreditVector()
        self.by_attempt: dict[str, CreditVector] = {}

    def can_add(self, work: CoordinatorWork) -> bool:
        previous = self.by_attempt.get(work.attempt_id, CreditVector())
        candidate = self.in_use - previous + work.credits
        return candidate.fits(self.limit)

    def replace(self, work: CoordinatorWork, *, allow_oversubscribed: bool) -> None:
        previous = self.by_attempt.get(work.attempt_id, CreditVector())
        candidate = self.in_use - previous + work.credits
        if not allow_oversubscribed and not candidate.fits(self.limit):
            raise ValueError("new work exceeds coordinator credit limits")
        self.by_attempt[work.attempt_id] = work.credits
        self.in_use = candidate

    def release(self, attempt_id: str) -> None:
        previous = self.by_attempt.pop(attempt_id, None)
        if previous is not None:
            self.in_use = self.in_use - previous

    def allowance_for(self, attempt_id: str) -> CreditVector:
        previous = self.by_attempt.get(attempt_id, CreditVector())
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
        recovery_complete = False
        circuit_open = False
        blocked_reason: str | None = "recovery_barrier"
        last_progress = self._monotonic()
        lane_limits = {
            CoordinatorLane.PREFLIGHT: self._limits.preflight_workers,
            CoordinatorLane.REMOTE: self._limits.remote_workers,
            CoordinatorLane.LOCAL_PREPARE: self._limits.local_prepare_workers,
            CoordinatorLane.LOCAL: self._limits.local_workers,
            CoordinatorLane.COMMIT: self._limits.commit_workers,
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
            tuple[CoordinatorLane, CoordinatorWork, StageLeaseGuard, CreditVector],
        ] = {}
        reconciled_results: dict[Future[CoordinatorWork], CoordinatorWork] = {}
        in_flight_failures: set[Future[CoordinatorWork]] = set()
        provisional_local: dict[Future[CoordinatorWork], CreditVector] = {}
        provisional_local_total = CreditVector()

        def emit() -> None:
            self._progress(
                CoordinatorSnapshot(
                    admission_open=admission_open,
                    recovery_complete=recovery_complete,
                    circuit_open=circuit_open,
                    queued=tuple((lane.value, len(queues[lane])) for lane in CoordinatorLane),
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
                )
            )

        def place(work: CoordinatorWork, *, recovery: bool) -> None:
            nonlocal completed, last_progress, circuit_open, blocked_reason
            previous = known.get(work.attempt_id)
            if previous is not None and (
                work.claim_generation < previous.claim_generation
                or work.row_version < previous.row_version
            ):
                raise RuntimeError("durable coordinator projection moved backwards")
            known[work.attempt_id] = work
            if recovery and (
                not ledger.can_add(work)
                or not work.credit_reservation.fits(ledger.limit)
            ):
                oversubscribed_recovery.add(work.attempt_id)
            ledger.replace(work, allow_oversubscribed=recovery)
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
                    prior, updated, lane,
                    f"illegal transition {prior.state}->{updated.state}",
                )
                return False
            if updated.row_version != prior.row_version + 1:
                preserve_contract_violation(
                    prior, updated, lane, "row version did not advance exactly once"
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
                and updated.credit_reservation != CreditVector()
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
                or renewed.row_version != work.row_version
                or renewed.claim_generation != work.claim_generation
                or renewed.claim_owner_identity != work.claim_owner_identity
                or renewed.credit_reservation != work.credit_reservation
                or renewed.credits != work.credits
                or renewed.lease_expires_monotonic is None
                or work.lease_expires_monotonic is None
                or renewed.lease_expires_monotonic
                <= work.lease_expires_monotonic
            ):
                preserve_contract_violation(
                    work, renewed, lane, "claim renewal changed durable work"
                )
                raise RuntimeError("claim renewal contract violation")
            known[work.attempt_id] = renewed
            return renewed

        def needs_renewal(work: CoordinatorWork, now: float) -> bool:
            return (
                work.lease_expires_monotonic is None
                or work.lease_expires_monotonic - now
                <= self._limits.max_stage_step_seconds
                + self._limits.claim_renew_margin_seconds
            )

        def transition_hold(
            work: CoordinatorWork, lane: CoordinatorLane
        ) -> CreditVector:
            names_by_lane = {
                CoordinatorLane.PREFLIGHT: {"remote_waits"},
                CoordinatorLane.REMOTE: {
                    "retained_results",
                    "retained_bytes",
                    "ack_items",
                },
                CoordinatorLane.LOCAL_PREPARE: {
                    "local_items",
                    "compressed_bytes",
                    "decoded_bytes",
                    "temp_disk_bytes",
                    "ack_items",
                },
                CoordinatorLane.LOCAL: {
                    "db_stage_items",
                    "unpublished_pages",
                    "ack_items",
                },
                CoordinatorLane.COMMIT: {"ack_items"},
                CoordinatorLane.ACK: set(),
            }
            names = names_by_lane[lane]
            return CreditVector(
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
                    for item in fields(CreditVector)
                }
            )

        def guard_in_flight(now: float) -> None:
            nonlocal circuit_open, admission_open, blocked_reason
            for future, (lane, work, stage_guard, _grant) in tuple(in_flight.items()):
                if future.done():
                    continue
                if now > stage_guard.deadline_monotonic:
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
                        continue
                    if (
                        durable.attempt_id == work.attempt_id
                        and durable.claim_generation == work.claim_generation
                        and durable.claim_owner_identity == work.claim_owner_identity
                        and durable.state == work.state
                        and durable.row_version == work.row_version
                        and durable.credits == work.credits
                        and durable.credit_reservation == work.credit_reservation
                        and durable.lease_expires_monotonic is not None
                        and work.lease_expires_monotonic is not None
                        and durable.lease_expires_monotonic
                        > work.lease_expires_monotonic
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
                        and durable.row_version == work.row_version + 1
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
                else:
                    in_flight[future] = (lane, renewed, stage_guard, _grant)

        try:
            # Startup is an exhaustive keyset recovery barrier.  No new work
            # is read until every current nonfinal attempt has been claimed or
            # placed on an explicit deferred-claim timer.
            after: str | None = None
            deferred_claims: dict[str, tuple[float, CoordinatorWork]] = {}
            while True:
                page = tuple(
                    self._backend.list_recoverable(
                        after_attempt_id=after,
                        limit=self._limits.recovery_page_size,
                    )
                )
                if not page:
                    break
                ids = [work.attempt_id for work in page]
                if ids != sorted(ids) or (after is not None and ids[0] <= after):
                    raise RuntimeError("recovery keyset page is not strictly ordered")
                for work in page:
                    try:
                        place(self._backend.claim_recovery(work), recovery=True)
                    except RecoveryDeferred as exc:
                        deferred_claims[work.attempt_id] = (
                            self._monotonic() + exc.retry_after_seconds,
                            work,
                        )
                after = ids[-1]
                if len(page) < self._limits.recovery_page_size:
                    break

            recovery_complete = not deferred_claims
            admission_open = recovery_complete and not stop_requested()
            blocked_reason = None if admission_open else "admission_closed"
            emit()

            while True:
                now = self._monotonic()
                if stop_requested():
                    admission_open = False
                    blocked_reason = "draining"

                for attempt_id, (ready_at, work) in tuple(
                    deferred_claims.items()
                ):
                    if ready_at > now:
                        continue
                    try:
                        claimed = self._backend.claim_recovery(work)
                    except RecoveryDeferred as exc:
                        deferred_claims[attempt_id] = (
                            now + exc.retry_after_seconds,
                            work,
                        )
                    else:
                        deferred_claims.pop(attempt_id, None)
                        place(claimed, recovery=True)
                        last_progress = now
                recovery_complete = not deferred_claims
                if (
                    recovery_complete
                    and not retry_degraded
                    and not circuit_open
                    and not stop_requested()
                ):
                    admission_open = True
                    if blocked_reason == "recovery_barrier":
                        blocked_reason = None
                elif not recovery_complete and not stop_requested():
                    admission_open = False
                    blocked_reason = "recovery_barrier"

                for attempt_id, (ready_at, work) in tuple(retry_at.items()):
                    if ready_at <= now:
                        retry_at.pop(attempt_id)
                        lane = _LANE_BY_STATE.get(work.state)
                        if lane is None:
                            raise RuntimeError("retry work has unsupported durable state")
                        queues[lane].append(work)

                active_ids = set(known)
                if (
                    admission_open
                    and not circuit_open
                    and not oversubscribed_recovery
                    and ledger.in_use.fits(ledger.limit)
                ):
                    capacity = max(0, self._limits.admission_batch_size - len(active_ids))
                    if capacity > 0:
                        available_credits = ledger.limit - ledger.in_use
                        admitted_batch = tuple(
                            self._backend.admit_new(
                                limit=capacity,
                                available_credits=available_credits,
                            )
                        )
                        if len(admitted_batch) > capacity:
                            raise RuntimeError("backend exceeded its admission count grant")
                        for work in admitted_batch:
                            if work.attempt_id in active_ids or work.attempt_id in final:
                                raise RuntimeError("new admission duplicated an active attempt")
                            if (
                                not ledger.can_add(work)
                                or not work.credit_reservation.fits(ledger.limit)
                            ):
                                # ``admit_new`` has already durably created and
                                # claimed the attempt. Preserve it and drain it;
                                # never drop a backend contract violation.
                                place(work, recovery=True)
                                circuit_open = True
                                admission_open = False
                                blocked_reason = "admission_credit_contract_violation"
                                errors.append(
                                    f"{work.attempt_id}:admission exceeded credit grant"
                                )
                            else:
                                place(work, recovery=False)
                            active_ids.add(work.attempt_id)
                            admitted += 1
                            last_progress = now

                for lane in _LANE_PRIORITY:
                    active = sum(
                        1
                        for active_lane, _, _, _ in in_flight.values()
                        if active_lane == lane
                    )
                    while queues[lane] and active < lane_limits[lane]:
                        work = queues[lane].popleft()
                        local_hold = transition_hold(work, lane)
                        if work.attempt_id in oversubscribed_recovery:
                            # Recovery may begin above the configured envelope,
                            # but only one positive-growth emergency grant may
                            # run at a time. This drains without multiplying the
                            # existing overage across concurrent owners.
                            if local_hold != CreditVector() and provisional_local:
                                queues[lane].appendleft(work)
                                break
                        elif not (
                            ledger.in_use + provisional_local_total + local_hold
                        ).fits(ledger.limit):
                            queues[lane].appendleft(work)
                            break
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
                        else:
                            future = pools[lane].submit(
                                self._backend.acknowledge,
                                work,
                                stage_guard=stage_guard,
                            )
                        in_flight[future] = (lane, work, stage_guard, local_hold)
                        if local_hold != CreditVector():
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
                            or retry_now - started
                            >= self._limits.retry_stuck_seconds
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
                        retry_at[work.attempt_id] = (retry_now + backoff, work)
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
                        if self._monotonic() > stage_guard.deadline_monotonic:
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
    "CoordinatorLane",
    "CoordinatorLimits",
    "CoordinatorResult",
    "CoordinatorSnapshot",
    "CoordinatorTerminal",
    "CoordinatorWork",
    "CreditVector",
    "RecoveryDeferred",
    "RetryStage",
    "StageLeaseGuard",
    "StageLeaseLost",
    "StagedCoordinatorBackend",
    "StagedParseCoordinator",
]
