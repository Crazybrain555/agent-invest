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


@dataclass(frozen=True, slots=True)
class CoordinatorWork:
    """Content-free projection of one claimed durable attempt."""

    attempt_id: str
    state: str
    row_version: int
    claim_generation: int
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


class StagedCoordinatorBackend(Protocol):
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

    def prepare_remote_io(self, work: CoordinatorWork) -> CoordinatorWork:
        """Run local-only preflight, then durably enter reconciling."""

    def run_remote(self, work: CoordinatorWork) -> CoordinatorWork: ...

    def prepare_local_io(self, work: CoordinatorWork) -> CoordinatorWork:
        """Durably enter materializing and reserve exact local projections."""

    def run_local(self, work: CoordinatorWork) -> CoordinatorWork: ...

    def commit(self, work: CoordinatorWork) -> CoordinatorWork: ...

    def acknowledge(self, work: CoordinatorWork) -> CoordinatorWork: ...


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
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if self.poll_seconds <= 0 or self.idle_open_circuit_seconds <= 0:
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
        known: dict[str, CoordinatorWork] = {}
        final: dict[str, str] = {}
        errors: list[str] = []
        ledger = _CreditLedger(self._limits.credits)
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
        operations = {
            CoordinatorLane.PREFLIGHT: self._backend.prepare_remote_io,
            CoordinatorLane.REMOTE: self._backend.run_remote,
            CoordinatorLane.LOCAL_PREPARE: self._backend.prepare_local_io,
            CoordinatorLane.LOCAL: self._backend.run_local,
            CoordinatorLane.COMMIT: self._backend.commit,
            CoordinatorLane.ACK: self._backend.acknowledge,
        }
        pools = {
            lane: ThreadPoolExecutor(
                max_workers=lane_limits[lane],
                thread_name_prefix=f"staged-{lane.value}",
            )
            for lane in CoordinatorLane
        }
        in_flight: dict[
            Future[CoordinatorWork], tuple[CoordinatorLane, CoordinatorWork]
        ] = {}

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
                            sum(1 for active_lane, _ in in_flight.values() if active_lane == lane),
                        )
                        for lane in CoordinatorLane
                    ),
                    credits_in_use=ledger.in_use,
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
            ledger.replace(work, allow_oversubscribed=recovery or previous is not None)
            if work.state in _FINAL_STATES:
                ledger.release(work.attempt_id)
                final[work.attempt_id] = work.state
                known.pop(work.attempt_id, None)
                retry_at.pop(work.attempt_id, None)
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

        try:
            # Startup is an exhaustive keyset recovery barrier.  No new work
            # is read until every current nonfinal attempt has been claimed or
            # placed on an explicit deferred-claim timer.
            after: str | None = None
            deferred_claims: deque[CoordinatorWork] = deque()
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
                    except RecoveryDeferred:
                        deferred_claims.append(work)
                after = ids[-1]
                if len(page) < self._limits.recovery_page_size:
                    break

            while deferred_claims and not stop_requested():
                work = deferred_claims.popleft()
                try:
                    place(self._backend.claim_recovery(work), recovery=True)
                except RecoveryDeferred as exc:
                    deferred_claims.append(work)
                    time.sleep(min(self._limits.poll_seconds, exc.retry_after_seconds))

            recovery_complete = not deferred_claims
            admission_open = recovery_complete and not stop_requested()
            blocked_reason = None if admission_open else "admission_closed"
            emit()

            while True:
                now = self._monotonic()
                if stop_requested():
                    admission_open = False
                    blocked_reason = "draining"

                for attempt_id, (ready_at, work) in tuple(retry_at.items()):
                    if ready_at <= now:
                        retry_at.pop(attempt_id)
                        lane = _LANE_BY_STATE.get(work.state)
                        if lane is None:
                            raise RuntimeError("retry work has unsupported durable state")
                        queues[lane].append(work)

                active_ids = set(known)
                if admission_open and not circuit_open and ledger.in_use.fits(ledger.limit):
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
                            if not ledger.can_add(work):
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
                        1 for active_lane, _ in in_flight.values() if active_lane == lane
                    )
                    while queues[lane] and active < lane_limits[lane]:
                        work = queues[lane].popleft()
                        in_flight[pools[lane].submit(operations[lane], work)] = (lane, work)
                        active += 1

                if not in_flight:
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
                        not admission_open
                        and now - last_progress >= self._limits.idle_open_circuit_seconds
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
                    emit()
                    continue
                for future in done:
                    lane, work = in_flight.pop(future)
                    try:
                        updated = future.result()
                    except RetryStage as exc:
                        retry_at[work.attempt_id] = (
                            self._monotonic() + exc.retry_after_seconds,
                            work,
                        )
                    except Exception as exc:  # noqa: BLE001 - opens the circuit visibly
                        circuit_open = True
                        admission_open = False
                        blocked_reason = f"{lane.value}_unexpected_failure"
                        errors.append(
                            f"{work.attempt_id}:{lane.value}:{type(exc).__name__}:{exc}"
                        )
                    else:
                        if updated.attempt_id != work.attempt_id:
                            raise RuntimeError("stage returned a different attempt identity")
                        place(updated, recovery=False)
                        last_progress = self._monotonic()
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
    "StagedCoordinatorBackend",
    "StagedParseCoordinator",
]
