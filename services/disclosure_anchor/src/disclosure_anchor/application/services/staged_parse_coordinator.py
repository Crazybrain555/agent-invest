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

Every circuit that is not a pure operator drain is a public fault. Its first
cause is latched in the injected stop control before the failing Future is
done or the controller unwinds, so a concurrent stop request, a later error or
an exception discarded by durable reconciliation can never hide it. The
latch halts the whole worker; durable persistence belongs to the adapter.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, fields, replace
from enum import Enum
from math import isfinite
import re
from threading import Event
import time
from typing import Literal, Protocol

from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
    STAGED_RESOURCE_STATE_TRANSITIONS,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RecoveryCandidate,
)
from disclosure_anchor.application.ports.remote_provider_v4 import NATIVE_STORAGE_HOLD_REASONS
from disclosure_anchor.application.ports.semantic_routes import SemanticRouteAdjudicatorError
from disclosure_anchor.application.ports.staged_provider_parser import (
    MATERIALIZATION_TRANSFER_INTEGRITY_HOLD_REASONS,
)
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4AdmissionObservationPort,
    V4AdmissionObservationRequest,
)
from disclosure_anchor.application.ports.staged_execution import StageObserverPort
from disclosure_anchor.application.ports.worker_stop_control import (
    MAX_STOP_PROVIDER_ATTEMPTS,
    PublicStopCause,
    StopProviderAttempt,
    WorkerStopControlPort,
    exception_class_name,
    exception_fingerprint,
    stop_count,
    stop_identifier,
    stop_sha256,
    stop_token,
)
from disclosure_anchor.application.services.staged_admission_observation import StagedAdmissionObservation
from disclosure_anchor.application.services.staged_execution_guard import (
    RevocationProvenance,
    StageLeaseGuard,
    StageLeaseLost,
)
from disclosure_anchor.application.services.mineru_stream_policy import StreamAdmissionControl, StreamAdmissionDecision
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch


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


def work_disk_footprint(credits: ResourceCreditVector, margin: int = 0) -> int:
    """Bytes one attempt owns or was promised on the Mac work volume, each extent once.

    The source snapshot is its own file. A LOCAL temp grant covers the spool,
    the unpacked tree and its serialized outputs; the promoted output is that
    tree renamed and the spool lies inside the grant, so the larger of the
    grant and the retained spool plus output counts, never both. ``margin``
    is the allocation rounding of one document's files. Durable credits are
    verified ownership after recovery: counted, never reserved from free
    space again.
    """

    logical = credits.snapshot_bytes + _local_extent_bytes(credits)
    return logical + margin if logical else 0


def _local_extent_bytes(credits: ResourceCreditVector) -> int:
    """The LOCAL part of a footprint: the grant, or the retained spool plus output."""

    return max(credits.temp_disk_bytes, credits.compressed_bytes + credits.output_bytes)


def _credit_union(
    left: ResourceCreditVector,
    right: ResourceCreditVector,
) -> ResourceCreditVector:
    return ResourceCreditVector(
        **{
            item.name: max(getattr(left, item.name), getattr(right, item.name))
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
    # While a stage grant this coordinator admitted is not yet durable, it
    # overlays ``credit_reservation`` (what the attempt may use now) and this
    # field keeps the reservation the durable head still carries. Every
    # durable identity check compares this value; None means
    # ``credit_reservation`` is itself the durable one.
    durable_credit_reservation: ResourceCreditVector | None = None

    @property
    def durable_reservation(self) -> ResourceCreditVector:
        """The reservation the durable head carries, beneath any admitted grant."""
        if self.durable_credit_reservation is None:
            return self.credit_reservation
        return self.durable_credit_reservation

    def __post_init__(self) -> None:
        if not self.attempt_id.strip() or len(self.attempt_id) > 128:
            raise ValueError("coordinator attempt identity is invalid")
        if self.durable_credit_reservation is not None and (
            type(self.durable_credit_reservation) is not ResourceCreditVector
            or not self.durable_credit_reservation.fits(self.credit_reservation)
        ):
            raise ValueError("an admitted grant only grows the durable lifecycle reservation")
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
    scan_incomplete: bool = False
    ineligible_dimensions: tuple[str, ...] = ()
    deferred_reason: str | None = None
    observation_request: V4AdmissionObservationRequest | None = None

    def __post_init__(self) -> None:
        credit_names = tuple(item.name for item in fields(ResourceCreditVector))
        canonical_blocked = tuple(
            name for name in credit_names if name in self.blocked_dimensions
        )
        if (
            type(self.work) is not tuple
            or any(type(item) is not CoordinatorWork for item in self.work)
            or type(self.backlog_exists) is not bool
            or type(self.scan_incomplete) is not bool
            or (self.observation_request is not None and (
                type(self.observation_request) is not V4AdmissionObservationRequest
                or not self.backlog_exists or not self.scan_incomplete
                or self.deferred_reason is not None
            ))
            or (self.deferred_reason is not None and (
                type(self.deferred_reason) is not str or not self.deferred_reason.strip()
                or self.scan_incomplete
            ))
            or type(self.ineligible_dimensions) is not tuple
            or self.ineligible_dimensions != tuple(
                name for name in credit_names if name in self.ineligible_dimensions
            )
            or type(self.blocked_dimensions) is not tuple
            or canonical_blocked != self.blocked_dimensions
            or any(name not in credit_names for name in self.blocked_dimensions)
            or (not self.backlog_exists and (
                self.blocked_dimensions or self.ineligible_dimensions or self.scan_incomplete
                or self.deferred_reason is not None
            ))
            or (self.backlog_exists and not self.work and not (
                self.blocked_dimensions or self.ineligible_dimensions or self.scan_incomplete
                or self.deferred_reason is not None
            ))
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
    envelope and raise a real failure when that boundary is crossed.  A plain
    wait (for example a local stream-pressure pause) does not end an open
    retry episode either; only ``StageProviderWaiting`` does.
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


class StageAdmissionDeferred(StageWaiting):
    """An unsafe reader closed a submission proved absent before POST.

    Keep its durable reservation for a later recovery, without polling this
    unaccepted submission forever while already accepted work drains.
    """


class StageProviderWaiting(StageWaiting):
    """A provider round-trip observed this attempt's task pending/processing.

    Raise it only after an authoritative status answer and the attempt's
    runaway check. The attempt is alive on the provider, so its failure
    episode is over: its retry count and first-failure time restart. Local
    waits and other attempts' answers restart nothing, and the global soft
    retry counter still clears only on a durable transition.
    """


class StageResourceGrantRequired(StageWaiting):
    """A stage needs a larger effective reservation before its durable commit.

    ``required`` is the complete reservation the pending transition needs; the
    backend made no durable change. Only the coordinator admits the growth,
    against its ledger, and then replays the stage with that reservation. A
    requirement the ledger can never hold stops the site durably
    (``stage_grant_unsatisfiable``); the verified result is kept.
    """

    def __init__(self, message: str, *, required: ResourceCreditVector) -> None:
        super().__init__(message, retry_after_seconds=0.001)
        if type(required) is not ResourceCreditVector:
            raise ValueError("stage grant requirement must be an exact credit vector")
        self.required = required


class StageHeavyWorkRequired(StageWaiting):
    """The stage reached whole-object heavy work without the shared permit.

    Nothing was written for that work: the same attempt is dispatched again,
    with the permit, once it is free. It spends no retry budget.
    """


class StageCapacityBlocked(StageWaiting):
    """This attempt cannot proceed under the configured limits at all.

    The backend keeps every durable fact and resource. A document-local hold
    (decode envelope, spent transfer budget) stays claimed and visible, without
    retries or failure, while other work proceeds. A native storage hold, an
    unprovable transfer prefix, or local holds that alone use up a ledger
    dimension stop the site durably instead. ``dimensions`` name what cannot
    be satisfied.
    """

    def __init__(self, message: str, *, dimensions: tuple[str, ...]) -> None:
        super().__init__(message, retry_after_seconds=0.001)
        if not dimensions or any(type(item) is not str or not item for item in dimensions):
            raise ValueError("capacity block must name its dimensions")
        self.dimensions = dimensions


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
    admission_probe_seconds: float = 1.0
    idle_open_circuit_seconds: float = 300.0
    claim_lease_seconds: int = 120
    claim_renew_margin_seconds: float = 30.0
    max_stage_step_seconds: float = 60.0
    commit_stage_seconds: float | None = None
    # A storage-managed worker's frozen, finite LOCAL (download/unpack/decode)
    # stage bound: set once at dispatch, with independent claim renewal.
    local_stage_seconds: float | None = None
    # Whole-object heavy work (LOCAL decode, COMMIT reopen/build/readiness/
    # promotion) shares these permits across lanes. One until real memory
    # evidence justifies more; separate lane pools never overlap it.
    heavy_work_permits: int = 1
    # The Mac work volume's business quota D over distinct owned or promised
    # extents (source snapshot, spool, unpacked tree, private output), each
    # document with its allocation rounding. None for a runtime without a
    # result storage policy.
    work_disk_bytes: int | None = None
    work_disk_margin_bytes: int = 0
    # D kept free for one document's largest LOCAL growth (the policy's
    # maximal grant). Admission never takes it, so documents that own only
    # their source snapshot never fill D past the point where a waiting
    # grant can start once LOCAL work drains.
    work_disk_local_reserve_bytes: int = 0
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
            (self.heavy_work_permits, "heavy work permits"),
            (self.claim_lease_seconds, "claim lease seconds"),
            (self.retry_consecutive_threshold, "retry consecutive threshold"),
            (self.retry_max_attempts, "retry max attempts"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if not 1 <= self.claim_lease_seconds <= 300:
            raise ValueError("claim lease seconds must fit the DB 1..300 contract")
        if self.work_disk_bytes is not None and (
            isinstance(self.work_disk_bytes, bool)
            or not isinstance(self.work_disk_bytes, int)
            or self.work_disk_bytes < 1
        ):
            raise ValueError("work disk quota must be a positive integer")
        if (
            isinstance(self.work_disk_margin_bytes, bool)
            or not isinstance(self.work_disk_margin_bytes, int)
            or self.work_disk_margin_bytes < 0
        ):
            raise ValueError("work disk allocation margin must be a non-negative integer")
        if (
            isinstance(self.work_disk_local_reserve_bytes, bool)
            or not isinstance(self.work_disk_local_reserve_bytes, int)
            or self.work_disk_local_reserve_bytes < 0
            or (
                self.work_disk_bytes is not None
                and self.work_disk_local_reserve_bytes >= self.work_disk_bytes
            )
        ):
            raise ValueError("work disk LOCAL reserve must be a non-negative integer below the quota")
        if self.recovery_page_size > 1000:
            raise ValueError("recovery page size must fit the DB 1..1000 contract")
        for timing_value, label in (
            (self.poll_seconds, "poll seconds"),
            (self.admission_probe_seconds, "admission probe seconds"),
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
        if self.commit_stage_seconds is not None and (
            isinstance(self.commit_stage_seconds, bool)
            or not isinstance(self.commit_stage_seconds, (int, float))
            or not isfinite(self.commit_stage_seconds)
            or self.commit_stage_seconds < self.max_stage_step_seconds
        ):
            raise ValueError("commit stage budget must be finite and cover the bounded stage step")
        if self.local_stage_seconds is not None and (
            isinstance(self.local_stage_seconds, bool)
            or not isinstance(self.local_stage_seconds, (int, float))
            or not isfinite(self.local_stage_seconds)
            or self.local_stage_seconds < self.max_stage_step_seconds
        ):
            raise ValueError("local stage budget must be finite and cover the bounded stage step")
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
    stream_target: int | None = None
    stream_actual: int | None = None
    stream_reason: str | None = None
    stream_evidence_sha256: str | None = None


# Why ``run`` returned: an idle observation, a public stop (the injected
# control holds its immutable first cause), a stop request drained with no
# fault, or a circuit whose cause could not be latched.
CoordinatorTermination = Literal["quiescent", "public_stop", "operator_drain", "circuit"]


@dataclass(frozen=True, slots=True)
class CoordinatorResult:
    """Aggregate totals and a bounded recent-final diagnostic sample, not history.

    ``stop_cause`` is the control's first cause when one was latched (by this
    coordinator or by another worker plane); ``termination_kind`` is ``None``
    only for results not produced by this coordinator.
    """
    terminal: CoordinatorTerminal
    recovery_complete: bool
    admitted: int
    completed: int
    final_states: tuple[tuple[str, str], ...]
    errors: tuple[str, ...]
    credits_in_use: ResourceCreditVector
    stop_cause: PublicStopCause | None = None
    termination_kind: CoordinatorTermination | None = None


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


_SEMANTIC_CANCELLED_REASON = "cancelled"
_DRAIN_PROVENANCES: frozenset[str] = frozenset({"operator_cancel", "public_stop"})


@dataclass(frozen=True, slots=True)
class _StageFailure:
    """How one stage exception affects the worker stop control.

    ``retry_neutral`` keeps the existing bounded retry/wait semantics,
    ``cancellation`` is positive evidence of an already decided drain, and
    ``fault`` carries the public stop cause to latch.
    """

    disposition: Literal["retry_neutral", "cancellation", "fault"]
    cause: PublicStopCause | None = None


# The V4 backend's fixed RetryStage literals: only these are echoed by an
# exhaustion diagnostic. Any other text is reduced to its fingerprint.
_RECOGNIZED_RETRY_MESSAGES = frozenset({
    "provider acknowledgement was unavailable",
    "provider poll episode was unavailable",
    "provider result materialization was unavailable",
    "provider submission episode was unavailable",
    "stage credit grant exhausted",
})
_CAUSE_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")
_HTTP_STATUS_RE = re.compile(r"[A-Za-z0-9 ]{1,80} returned HTTP ([1-5][0-9]{2})")
_MAX_RETRY_CAUSES = 4


def _next_cause(error: BaseException) -> BaseException | None:
    if error.__cause__ is not None:
        return error.__cause__
    return None if error.__suppress_context__ else error.__context__


def _retry_exhaustion_detail(
    error: RetryStage,
    *,
    attempts: int,
    max_attempts: int,
    elapsed_seconds: float,
    window_seconds: float,
) -> str:
    """Name the tripped bounds and the last failure without echoing its text.

    Keeps a recognized fixed literal, cause type names and an HTTP status
    parsed from an exact message pattern; unknown text may carry a URL, token
    or path, so it is only fingerprinted.
    """

    try:
        message = str(error)
    except Exception:  # noqa: BLE001 - a broken __str__ is just unrecognized
        message = ""
    last = message if message in _RECOGNIZED_RETRY_MESSAGES else (
        "unrecognized " + exception_fingerprint(error)
    )
    causes: list[str] = []
    status: str | None = None
    seen = {id(error)}
    cause = _next_cause(error)
    while cause is not None and id(cause) not in seen and len(causes) < _MAX_RETRY_CAUSES:
        seen.add(id(cause))
        name = type(cause).__name__
        causes.append(name if _CAUSE_TYPE_RE.fullmatch(name) else "unnamed")
        if status is None:
            try:
                matched = _HTTP_STATUS_RE.fullmatch(str(cause))
            except Exception:  # noqa: BLE001 - a broken __str__ has no status
                matched = None
            status = None if matched is None else matched.group(1)
        cause = _next_cause(cause)
    detail = (
        f"attempts={attempts}/{max_attempts}, "
        f"elapsed={elapsed_seconds:.1f}s/{window_seconds:g}s, "
        f"last={last}, causes={'<-'.join(causes) or 'none'}"
    )
    return detail if status is None else f"{detail}, http_status={status}"


def _site_hold_reason(dimensions: tuple[str, ...]) -> str | None:
    """The stop token of a hold that no retained attempt can clear by itself."""

    if any(item in NATIVE_STORAGE_HOLD_REASONS for item in dimensions):
        return "native_storage_hold"
    if any(item in MATERIALIZATION_TRANSFER_INTEGRITY_HOLD_REASONS for item in dimensions):
        return "transfer_integrity_hold"
    return None


def _stop_cause(
    *,
    kind: str,
    reason_code: str,
    origin: str,
    work: CoordinatorWork | None = None,
    lane: CoordinatorLane | None = None,
    error: BaseException | None = None,
    provider_attempts: tuple[StopProviderAttempt, ...] = (),
) -> PublicStopCause:
    """Project the dispatch-time durable identity; never a new DB read."""

    return PublicStopCause(
        kind=kind,
        reason_code=stop_token(reason_code) or "unclassified",
        origin=origin,
        exception_class=None if error is None else exception_class_name(error),
        exception_fingerprint=None if error is None else exception_fingerprint(error),
        attempt_id=None if work is None else stop_identifier(work.attempt_id),
        lane=None if lane is None else stop_token(lane.value),
        state_at_dispatch=None if work is None else stop_token(work.state),
        lifecycle_version_at_dispatch=None if work is None else stop_count(work.lifecycle_version),
        claim_generation=None if work is None else stop_count(work.claim_generation),
        claim_owner_identity=None if work is None else stop_identifier(work.claim_owner_identity),
        provider_attempts=provider_attempts,
    )


def _provider_attempts(error: SemanticRouteAdjudicatorError) -> tuple[StopProviderAttempt, ...]:
    """Whitelist provider identity, outcome, reason and cache key only."""

    projected: list[StopProviderAttempt] = []
    for attempt in tuple(getattr(error, "attempts", ()))[:MAX_STOP_PROVIDER_ATTEMPTS]:
        try:
            provider = attempt.provider
            projected.append(
                StopProviderAttempt(
                    ordinal=attempt.ordinal,
                    outcome=stop_token(attempt.outcome) or "unclassified",
                    provider_id=stop_identifier(provider.provider_id),
                    provider=stop_identifier(provider.provider),
                    adapter_kind=stop_identifier(provider.adapter_kind),
                    adapter_version=stop_identifier(provider.adapter_version),
                    canonical_model=stop_identifier(provider.canonical_model),
                    inference_profile=stop_identifier(provider.inference_profile),
                    reason_code=stop_token(attempt.reason_code),
                    cache_key=stop_sha256(attempt.cache_key),
                )
            )
        except (AttributeError, TypeError, ValueError):
            # A malformed attempt is omitted, never guessed.
            continue
    return tuple(projected)


def _classify_stage_failure(
    error: BaseException,
    *,
    lane: CoordinatorLane,
    work: CoordinatorWork,
) -> _StageFailure:
    if isinstance(error, (RetryStage, StageWaiting, RecoveryDeferred)):
        return _StageFailure("retry_neutral")
    if isinstance(error, StageLeaseLost):
        provenance = error.provenance
        if provenance in _DRAIN_PROVENANCES:
            return _StageFailure("cancellation")
        kind, reason = {
            "ownership_lost": ("ownership_lost", "in_flight_claim_lost"),
            "deadline_exhausted": ("deadline_exhausted", "bounded_stage_deadline_exceeded"),
        }.get(provenance, ("stage_fault", "stage_lease_lost"))
        return _StageFailure(
            "fault",
            _stop_cause(kind=kind, reason_code=reason, origin="stage_call",
                        work=work, lane=lane, error=error),
        )
    if isinstance(error, SemanticRouteAdjudicatorError):
        if error.reason_code == _SEMANTIC_CANCELLED_REASON:
            return _StageFailure("cancellation")
        return _StageFailure(
            "fault",
            _stop_cause(
                kind="semantic_failed_closed",
                reason_code=error.reason_code,
                origin="stage_call",
                work=work,
                lane=lane,
                error=error,
                provider_attempts=_provider_attempts(error),
            ),
        )
    return _StageFailure(
        "fault",
        _stop_cause(kind="stage_fault", reason_code=f"{lane.value}_unexpected_failure",
                    origin="stage_call", work=work, lane=lane, error=error),
    )


def _classify_stage_failure_safely(
    error: BaseException,
    *,
    lane: CoordinatorLane,
    work: CoordinatorWork,
) -> _StageFailure:
    try:
        return _classify_stage_failure(error, lane=lane, work=work)
    except Exception:  # noqa: BLE001 - classification must never replace the stage error
        return _StageFailure(
            "fault",
            _stop_cause(kind="stage_fault", reason_code="unclassified_stage_failure",
                        origin="stage_call", lane=lane),
        )


class StagedParseCoordinator:
    """Run a recovery-first staged parse dispatcher until stop and quiescence."""

    def __init__(
        self,
        *,
        backend: StagedCoordinatorBackend,
        limits: CoordinatorLimits,
        progress: Callable[[CoordinatorSnapshot], None] = lambda _snapshot: None,
        monotonic: Callable[[], float] = time.monotonic,
        process_guard: Callable[[], None] = lambda: None,
        admission_observer: V4AdmissionObservationPort | None = None,
        stream_control: StreamAdmissionControl | None = None,
        stage_observer: StageObserverPort | None = None,
        stop_control: WorkerStopControlPort | None = None,
    ) -> None:
        self._backend = backend
        self._limits = limits
        self._progress = progress
        self._monotonic = monotonic
        self._process_guard = process_guard
        self._admission_observer = admission_observer
        self._stream_control = stream_control
        # Measurement only: each stage guard carries attempt/lane for the notes.
        self._stage_observer = stage_observer
        if stream_control is not None and stream_control.policy.config.qualified_max > limits.credits.remote_waits:
            raise ValueError("stream qualified capacity exceeds hard remote credits")
        # Production composition injects the process-wide durable control. A
        # coordinator composed without one still latches and halts on its
        # first public fault; it only has no durable record of it.
        if stop_control is not None and not all(
            callable(getattr(stop_control, name, None))
            for name in ("trip", "is_tripped", "first_cause")
        ):
            raise ValueError("worker stop control is invalid")
        self._stop_control: WorkerStopControlPort = (
            stop_control if stop_control is not None else InProcessWorkerStopLatch()
        )

    @property
    def stop_control(self) -> WorkerStopControlPort:
        return self._stop_control

    def _trip(self, cause: PublicStopCause) -> str | None:
        """Latch a public fault; report, never raise, if the control fails."""

        try:
            self._stop_control.trip(cause)
        except Exception as exc:  # noqa: BLE001 - the original fault stays primary
            return f"worker stop control failed to latch {cause.reason_code}:{type(exc).__name__}:{exc}"
        return None

    def _run_stage_call(
        self,
        call: Callable[..., CoordinatorWork],
        lane: CoordinatorLane,
        work: CoordinatorWork,
        /,
        **kwargs: object,
    ) -> CoordinatorWork:
        """Run one backend stage and latch a public fault before its Future is done.

        Classification happens here, in the stage thread, so neither the
        controller's reconciliation path (which reads and discards a raced
        Future's exception) nor a concurrent stop request can hide the cause.
        """

        try:
            return call(work, **kwargs)
        except BaseException as error:
            failure = _classify_stage_failure_safely(error, lane=lane, work=work)
            if failure.cause is not None:
                problem = self._trip(failure.cause)
                if problem is not None:
                    error.add_note(problem)
            raise

    def _guard_process(self) -> None:
        """Probe process ownership; a failed probe is an ownership stop."""

        try:
            self._process_guard()
        except BaseException as error:
            if not isinstance(error, (KeyboardInterrupt, SystemExit)):
                problem = self._trip(
                    _stop_cause(kind="ownership_lost", reason_code="process_guard_failed",
                                origin="coordinator", error=error)
                )
                if problem is not None:
                    error.add_note(problem)
            raise

    def run(
        self,
        *,
        stop_requested: Callable[[], bool] = lambda: False,
        wait_for_stream_admission: bool = False,
    ) -> CoordinatorResult:
        queues = {lane: deque[CoordinatorWork]() for lane in CoordinatorLane}
        retry_at: dict[str, tuple[float, CoordinatorWork]] = {}
        retry_attempts: dict[str, int] = {}
        retry_started_at: dict[str, float] = {}
        consecutive_retries = 0
        retry_degraded = False
        known: dict[str, CoordinatorWork] = {}
        final: dict[str, str] = {}
        final_history_limit = max(
            self._limits.recovery_page_size, self._limits.admission_batch_size,
        )
        errors: list[str] = []
        ledger = _CreditLedger(self._limits.credits)
        oversubscribed_recovery: set[str] = set()
        admitted = 0
        completed = 0
        admission_open = False
        admission_blocked_dimensions: tuple[str, ...] = ()
        admission_scan_incomplete = False
        admission_backlog_exhausted = False
        admission_probe_at = 0.0
        admission_deferred = False
        last_admission_available: ResourceCreditVector | None = None
        recovery_complete = False
        circuit_open = False
        blocked_reason: str | None = "recovery_barrier"
        last_progress = self._monotonic()
        stream_decision: StreamAdmissionDecision | None = None
        stream_failure: str | None = None
        stream_parked: dict[str, CoordinatorWork] = {}
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
        observation = StagedAdmissionObservation(self._admission_observer)
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
        # Stage grants this coordinator admitted: attempt -> (durable
        # reservation before the grant, admitted effective reservation). The
        # admitted value overlays every projection of the attempt until the
        # transition that carries it is durable; a restart simply asks again.
        admitted_grants: dict[str, tuple[ResourceCreditVector, ResourceCreditVector]] = {}
        # Admitted grants still waiting for dispatch capacity: new admission
        # yields to them so later work cannot take the space they need.
        grant_waiting: set[str] = set()
        # Document-local holds (decode envelope, spent transfer budget): kept
        # claimed and visible, never failed or ACKed, while other work runs.
        capacity_holds: dict[str, tuple[CoordinatorLane, CoordinatorWork, tuple[str, ...]]] = {}
        # Whole-object heavy work shares a small permit set across lanes. Each
        # holder is one in-flight stage, released when its Future completes for
        # any reason. COMMIT takes a permit at dispatch; LOCAL only after its
        # last run stopped before a decode for want of one. CLEANUP decodes
        # nothing and ACK, renewal and reconcile never wait for a permit.
        heavy_holders: set[Future[CoordinatorWork]] = set()
        heavy_ready: set[str] = set()
        # A circuit opened by a fault (latched in the stop control) versus one
        # opened only to end an operator drain promptly after cancellation.
        fault_circuit = False
        draining_observed = False
        public_stop_observed = False
        operator_interrupted = False

        def trip_fault(cause: PublicStopCause) -> None:
            nonlocal fault_circuit
            fault_circuit = True
            problem = self._trip(cause)
            if problem is not None:
                errors.append(problem)

        def observe_public_stop() -> None:
            """Halt dispatch and revoke every running stage on any plane's stop."""

            nonlocal public_stop_observed, circuit_open, admission_open, blocked_reason
            cause = self._stop_control.first_cause()
            public_stop_observed = True
            circuit_open = True
            admission_open = False
            blocked_reason = "public_stop:" + (
                cause.reason_code if cause is not None else "unclassified"
            )
            for _, _, running_guard, _ in in_flight.values():
                running_guard.revoke("public_stop")

        def termination(terminal: CoordinatorTerminal) -> CoordinatorTermination:
            if terminal is CoordinatorTerminal.QUIESCENT:
                return "quiescent"
            if self._stop_control.first_cause() is not None:
                return "public_stop"
            if draining_observed and not fault_circuit:
                return "operator_drain"
            return "circuit"

        def emit() -> None:
            self._progress(
                CoordinatorSnapshot(
                    stream_target=None if stream_decision is None else stream_decision.target,
                    stream_actual=None if stream_decision is None else ledger.in_use.remote_waits + provisional_local_total.remote_waits,
                    stream_reason=None if stream_decision is None else stream_decision.reason,
                    stream_evidence_sha256=None if stream_decision is None else stream_decision.evidence_sha256,
                    admission_open=admission_open,
                    recovery_complete=recovery_complete,
                    circuit_open=circuit_open,
                    queued=tuple(
                        (lane.value, len(queues[lane]) + int(
                            lane == CoordinatorLane.PREFLIGHT
                            and observation.pending and not observation.active_slots
                        )) for lane in CoordinatorLane
                    ),
                    in_flight=tuple(
                        (
                            lane.value,
                            sum(
                                1
                                for active_lane, _, _, _ in in_flight.values()
                                if active_lane == lane
                            ) + (observation.active_slots if lane == CoordinatorLane.PREFLIGHT else 0),
                        )
                        for lane in CoordinatorLane
                    ),
                    credits_in_use=ledger.in_use + provisional_local_total + observation.credits,
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
                admitted_grants.pop(work.attempt_id, None)
                grant_waiting.discard(work.attempt_id)
                capacity_holds.pop(work.attempt_id, None)
                ledger.release(work.attempt_id)
                final[work.attempt_id] = work.state
                if len(final) > final_history_limit:
                    del final[next(iter(final))]
                known.pop(work.attempt_id, None)
                retry_at.pop(work.attempt_id, None)
                retry_attempts.pop(work.attempt_id, None)
                retry_started_at.pop(work.attempt_id, None)
                oversubscribed_recovery.discard(work.attempt_id)
                completed += 1
                last_progress = self._monotonic()
                return
            lane = _LANE_BY_STATE.get(work.state)
            if lane is None:
                circuit_open = True
                blocked_reason = "unsupported_durable_state"
                errors.append(f"{work.attempt_id}:unsupported state:{work.state}")
                trip_fault(_stop_cause(kind="coordinator_circuit",
                                      reason_code="unsupported_durable_state",
                                      origin="coordinator", work=work))
                return
            queues[lane].append(work)
            track_waiting_lease(work)

        def durable_reservation(work: CoordinatorWork) -> ResourceCreditVector:
            """The reservation the durable head carries for this projection."""
            return work.durable_reservation

        def overlay(durable: CoordinatorWork) -> CoordinatorWork:
            """Apply an admitted, not yet durable grant to one durable projection.

            The grant only widens what the attempt may use (its
            ``credit_reservation``); the durable reservation travels beside it
            so every persistence identity check still compares durable values.
            """
            pending = admitted_grants.get(durable.attempt_id)
            if pending is None or durable.state in _FINAL_STATES:
                return durable
            before, admitted_value = pending
            carried = durable.durable_reservation
            if carried == admitted_value:
                # The transition carrying the grant is durable now.
                admitted_grants.pop(durable.attempt_id, None)
                grant_waiting.discard(durable.attempt_id)
                return replace(durable, credit_reservation=carried, durable_credit_reservation=None)
            if carried != before:
                raise ValueError("durable reservation drifted under an admitted grant")
            return replace(durable, credit_reservation=admitted_value, durable_credit_reservation=carried)

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
            trip_fault(_stop_cause(kind="coordinator_circuit",
                                  reason_code="durable_transition_contract_violation",
                                  origin="coordinator", work=prior, lane=lane))

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
            if updated.state not in _FINAL_STATES:
                try:
                    projected = overlay(updated)
                except ValueError:
                    projected = updated
                if projected.credit_reservation != prior.credit_reservation:
                    preserve_contract_violation(
                        prior, updated, lane, "stage changed lifecycle reservation"
                    )
                    return False
                updated = projected
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
                or renewed.credit_reservation != durable_reservation(work)
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
            renewed = overlay(renewed)
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
            trip_fault(_stop_cause(kind="ownership_lost", reason_code="claim_renewal_failed",
                                  origin="coordinator", work=work, lane=lane, error=exc))

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
                    and durable.credit_reservation == durable_reservation(work)
                    and durable.credits == work.credits
                    and durable.lease_expires_monotonic is not None
                    and work.lease_expires_monotonic is not None
                    and durable.lease_expires_monotonic
                    > work.lease_expires_monotonic
                    and durable.lease_expires_monotonic - self._monotonic()
                    > threshold
                ):
                    durable = overlay(durable)
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
            for attempt_id, work in tuple(stream_parked.items()):
                if needs_renewal(work, now):
                    renewed = renew_waiting(work, CoordinatorLane.REMOTE)
                    if renewed is None:
                        return
                    stream_parked[attempt_id] = work = renewed
                assert work.lease_expires_monotonic is not None
                next_floor = min(next_floor, work.lease_expires_monotonic)
            for attempt_id, (hold_lane, work, shortage) in tuple(capacity_holds.items()):
                if needs_renewal(work, now):
                    renewed = renew_waiting(work, hold_lane)
                    if renewed is None:
                        return
                    capacity_holds[attempt_id] = (hold_lane, renewed, shortage)
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

        def stop_for_hold(
            work: CoordinatorWork,
            lane: CoordinatorLane,
            reason_code: str,
            error: BaseException,
        ) -> None:
            """Stop the site for a hold; the attempt keeps its durable state.

            Nothing is failed, cleaned or ACKed: its task, evidence, spool,
            result and credits stay as they are for the operator.
            """

            nonlocal circuit_open, admission_open, blocked_reason
            circuit_open = True
            admission_open = False
            blocked_reason = reason_code
            trip_fault(_stop_cause(kind="coordinator_circuit", reason_code=reason_code,
                                   origin="coordinator", work=work, lane=lane, error=error))

        def exhausted_by_holds() -> tuple[str, ...]:
            """Positive ledger dimensions that held attempts alone use up."""

            held = ResourceCreditVector()
            for _hold_lane, held_work, _shortage in capacity_holds.values():
                held = held + held_work.credits
            return tuple(
                item.name
                for item in fields(ResourceCreditVector)
                if 0 < getattr(ledger.limit, item.name) <= getattr(held, item.name)
            )

        def positive_dimensions(value: ResourceCreditVector) -> tuple[str, ...]:
            return tuple(
                item.name
                for item in fields(ResourceCreditVector)
                if getattr(value, item.name) > 0
            )

        def work_disk_owned() -> Iterator[ResourceCreditVector]:
            """Each known attempt's owned plus in-flight promised credits."""

            promised = {held.attempt_id: hold for _, held, _, hold in in_flight.values()}
            for attempt_id in set(ledger.by_attempt) | set(promised):
                yield (
                    ledger.by_attempt.get(attempt_id, ResourceCreditVector())
                    + promised.get(attempt_id, ResourceCreditVector())
                )

        def work_disk_in_use() -> int:
            """Every known attempt's owned plus in-flight promised work-volume bytes."""

            margin = self._limits.work_disk_margin_bytes
            return sum(work_disk_footprint(credits, margin) for credits in work_disk_owned())

        def work_disk_source_only() -> int:
            """D held by attempts that own only their source snapshot."""

            margin = self._limits.work_disk_margin_bytes
            return sum(
                work_disk_footprint(credits, margin)
                for credits in work_disk_owned()
                if not _local_extent_bytes(credits)
            )

        def work_disk_growth(work: CoordinatorWork, hold: ResourceCreditVector) -> int:
            margin = self._limits.work_disk_margin_bytes
            current = ledger.by_attempt.get(work.attempt_id, work.credits)
            return work_disk_footprint(current + hold, margin) - work_disk_footprint(current, margin)

        def work_disk_shortage(work: CoordinatorWork, hold: ResourceCreditVector) -> tuple[str, ...]:
            """Refuse a transition whose new promise would carry the volume past D.

            Only growth is checked: a transition that keeps or shrinks the
            footprint (COMMIT, CLEANUP, ACK) is never blocked, so the stages that
            release space always run.
            """

            limit = self._limits.work_disk_bytes
            growth = 0 if limit is None else work_disk_growth(work, hold)
            if limit is None or growth <= 0 or work_disk_in_use() + growth <= limit:
                return ()
            return ("work_disk_bytes",)

        def yields_work_disk(work: CoordinatorWork, hold: ResourceCreditVector, head_growth: int) -> bool:
            """Whether a later candidate leaves D to the waiting head of its lane.

            It does while the head fits once LOCAL work drains, which needs no
            growth: admission leaves the LOCAL reserve free, so the head waits
            for draining work only and is never overtaken indefinitely. Source
            snapshots recovered beyond that (another policy's admission) let
            fitting work run instead, so they drain. An attempt that already
            owns LOCAL extents never waits behind the head.
            """

            limit = self._limits.work_disk_bytes
            if limit is None or work_disk_growth(work, hold) <= 0:
                return False
            if _local_extent_bytes(ledger.by_attempt.get(work.attempt_id, work.credits)):
                return False
            return head_growth <= limit - work_disk_source_only()

        def needs_heavy_permit(lane: CoordinatorLane, work: CoordinatorWork) -> bool:
            return lane == CoordinatorLane.COMMIT or (
                lane == CoordinatorLane.LOCAL and work.attempt_id in heavy_ready
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

            def refresh_guard_claim(
                guard: StageLeaseGuard, renewed_work: CoordinatorWork,
            ) -> bool:
                nonlocal circuit_open, admission_open, blocked_reason
                if guard.claim_deadline_monotonic is None:
                    return True
                try:
                    if renewed_work.lease_expires_monotonic is None:
                        raise StageLeaseLost("renewal has no verified claim deadline")
                    guard.refresh_claim_deadline(
                        renewed_work.lease_expires_monotonic
                        - self._limits.claim_renew_margin_seconds
                    )
                except (StageLeaseLost, ValueError) as exc:
                    circuit_open = True
                    admission_open = False
                    blocked_reason = "in_flight_claim_lost"
                    errors.append(
                        f"{renewed_work.attempt_id}:{lane.value}:claim-guard:"
                        f"{type(exc).__name__}:{exc}"
                    )
                    in_flight_failures.add(future)
                    guard.revoke("ownership_lost")
                    trip_fault(_stop_cause(kind="ownership_lost", reason_code="in_flight_claim_lost",
                                          origin="coordinator", work=renewed_work, lane=lane,
                                          error=exc))
                    return False
                return True

            for future, (lane, work, stage_guard, _grant) in tuple(in_flight.items()):
                if future.done():
                    continue
                try:
                    stage_guard.checkpoint()
                except StageLeaseLost as lost:
                    if lost.provenance in _DRAIN_PROVENANCES:
                        # Revoked by an already decided stop; the stage drains
                        # at its next checkpoint and is not a deadline fault.
                        continue
                    if future not in in_flight_failures:
                        circuit_open = True
                        admission_open = False
                        blocked_reason = "bounded_stage_deadline_exceeded"
                        errors.append(
                            f"{work.attempt_id}:{lane.value}:stage deadline exceeded"
                        )
                        in_flight_failures.add(future)
                        stage_guard.revoke("deadline_exhausted")
                        trip_fault(_stop_cause(
                            kind="deadline_exhausted",
                            reason_code="bounded_stage_deadline_exceeded",
                            origin="coordinator", work=work, lane=lane, error=lost,
                        ))
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
                            stage_guard.revoke("ownership_lost")
                            trip_fault(_stop_cause(
                                kind="ownership_lost",
                                reason_code="in_flight_claim_reconcile_failed",
                                origin="coordinator", work=work, lane=lane, error=reload_exc,
                            ))
                        continue
                    if (
                        durable.attempt_id == work.attempt_id
                        and durable.claim_generation == work.claim_generation
                        and durable.claim_owner_identity == work.claim_owner_identity
                        and durable.state == work.state
                        and durable.lifecycle_version == work.lifecycle_version
                        and durable.credits == work.credits
                        and durable.credit_reservation == durable_reservation(work)
                        and durable.lease_expires_monotonic is not None
                        and work.lease_expires_monotonic is not None
                        and durable.lease_expires_monotonic
                        > work.lease_expires_monotonic
                        and durable.lease_expires_monotonic
                        > min(
                            stage_guard.deadline_monotonic,
                            self._monotonic() + self._limits.max_stage_step_seconds,
                        )
                        + self._limits.claim_renew_margin_seconds
                    ):
                        if not refresh_guard_claim(stage_guard, durable):
                            continue
                        durable = overlay(durable)
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
                        stage_guard.revoke("ownership_lost")
                        trip_fault(_stop_cause(kind="ownership_lost", reason_code="in_flight_claim_lost",
                                              origin="coordinator", work=work, lane=lane, error=exc))
                else:
                    if not refresh_guard_claim(stage_guard, renewed):
                        continue
                    in_flight[future] = (lane, renewed, stage_guard, _grant)

        try:
            # Startup is an exhaustive keyset recovery barrier.  No new work
            # is read until every current nonfinal attempt has been claimed or
            # placed on an explicit deferred-claim timer.
            after: str | None = None
            barrier_exhausted = False
            deferred_claims: dict[str, tuple[float, RecoveryCandidate]] = {}
            while True:
                self._guard_process()
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
                self._guard_process()
                if self._stop_control.is_tripped() and not public_stop_observed:
                    observe_public_stop()
                if stop_requested() or circuit_open:
                    observation.cancel()
                if observation.collect():
                    admission_probe_at = 0.0
                    last_progress = self._monotonic()
                now = self._monotonic()
                if self._stream_control is not None:
                    stream_decision = self._stream_control.current()
                    if stream_decision.unsafe and stream_failure is None:
                        stream_failure = stream_decision.reason
                        errors.append("stream pressure closed new submissions: " + stream_failure)
                    if stream_failure is not None:
                        observation.cancel()
                if stop_requested():
                    admission_open = False
                    draining_observed = True
                    if not public_stop_observed:
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
                    admission_open = not admission_deferred or now >= admission_probe_at
                    if blocked_reason == "recovery_barrier" or (blocked_reason is not None and blocked_reason.startswith("stream_pause:")):
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
                if stream_failure is not None or (
                    stream_decision is not None and not stream_decision.new_post_allowed
                ):
                    # New work needs a new remote permit, so it follows the
                    # same fresh/known POST permission as the effect boundary.
                    admission_open = False
                    if not circuit_open and not stop_requested():
                        blocked_reason = "stream_pause:" + (stream_failure or (stream_decision.reason if stream_decision is not None else "unknown"))
                if grant_waiting and admission_open and not circuit_open:
                    # A verified result waits for its stage grant: new work
                    # would take the capacity it needs, so admission yields.
                    blocked_reason = "stage_grant_waiting"
                elif blocked_reason == "stage_grant_waiting":
                    blocked_reason = None
                if (
                    admission_open and not grant_waiting
                    and not circuit_open and not observation.pending
                ):
                    committed_and_transient = ledger.in_use + provisional_local_total
                    if oversubscribed_recovery or not committed_and_transient.fits(ledger.limit):
                        blocked_reason = "oversubscribed_recovery_drain"
                    else:
                        available_credits = ledger.limit - committed_and_transient
                        saturated = tuple(
                            item.name
                            for item in fields(ResourceCreditVector)
                            if getattr(available_credits, item.name) == 0
                        )
                        capacity = min(
                            self._limits.admission_batch_size,
                            available_credits.documents,
                        )
                        if self._limits.work_disk_bytes is not None:
                            # A new H0 owns its source snapshot on the work
                            # volume with one document's allocation margin.
                            # Admission offers only D's headroom above the
                            # LOCAL reserve, each admitted document's margin
                            # set aside first.
                            headroom = max(
                                0,
                                self._limits.work_disk_bytes
                                - self._limits.work_disk_local_reserve_bytes
                                - work_disk_in_use(),
                            )
                            margin = self._limits.work_disk_margin_bytes
                            capacity = min(capacity, headroom // (margin + 1))
                            snapshot_room = headroom - capacity * margin
                            available_credits = replace(
                                available_credits,
                                documents=min(available_credits.documents, capacity),
                                snapshot_bytes=min(available_credits.snapshot_bytes, snapshot_room),
                            )
                            if capacity == 0 or snapshot_room == 0:
                                capacity = 0
                                saturated = ("work_disk_bytes", *saturated)
                        if capacity == 0:
                            admission_blocked_dimensions = saturated or ("documents",)
                            blocked_reason = "credit_backpressure:" + ",".join(
                                admission_blocked_dimensions
                            )
                        elif (
                            not admission_scan_incomplete
                            and now < admission_probe_at
                            and last_admission_available is not None
                            and not any(
                                getattr(available_credits, item.name)
                                > getattr(last_admission_available, item.name)
                                for item in fields(ResourceCreditVector)
                            )
                        ):
                            # A completed empty scan is not useful every scheduler
                            # tick. Probe periodically for new rows even if credits
                            # stay unchanged; a released credit wakes it early.
                            pass
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
                                trip_fault(_stop_cause(kind="coordinator_circuit",
                                                      reason_code="admission_interrupted",
                                                      origin="coordinator", error=exc))
                            else:
                                if type(admission) is not AdmissionOutcome:
                                    raise RuntimeError(
                                        "backend returned an invalid admission outcome"
                                    )
                                admitted_batch = admission.work
                                # A scoped, one-shot run may wait for cold stream
                                # recovery only until the source has actually
                                # reported no remaining backlog. Recovery's
                                # target zero alone says nothing about the source.
                                admission_backlog_exhausted = not admission.backlog_exists
                                admission_deferred = admission.deferred_reason is not None
                                if not admission_deferred and blocked_reason is not None and (
                                    blocked_reason.startswith("admission_deferred:")
                                ):
                                    blocked_reason = None
                                admission_scan_incomplete = admission.scan_incomplete
                                admission_probe_at = (
                                    self._monotonic() + self._limits.admission_probe_seconds
                                    if admission.deferred_reason is not None or (
                                        not admitted_batch and not admission.scan_incomplete
                                    )
                                    else 0.0
                                )
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
                                        or (
                                            self._limits.work_disk_bytes is not None
                                            and work_disk_in_use() + work_disk_footprint(
                                                work.credits, self._limits.work_disk_margin_bytes,
                                            )
                                            > self._limits.work_disk_bytes
                                            - self._limits.work_disk_local_reserve_bytes
                                        )
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
                                        trip_fault(_stop_cause(kind="coordinator_circuit",
                                                              reason_code=blocked_reason,
                                                              origin="coordinator", work=work))
                                    else:
                                        place(work, recovery=False)
                                    active_ids.add(work.attempt_id)
                                    admitted += 1
                                    last_progress = now
                                if admission.observation_request is not None:
                                    requested = admission.observation_request.credits
                                    if not (ledger.in_use + provisional_local_total + requested).fits(ledger.limit):
                                        raise RuntimeError("admission observation exceeded shared credit grant")
                                    observation.enqueue(admission.observation_request)
                                held_after_admission = ledger.in_use + provisional_local_total + observation.credits
                                last_admission_available = (
                                    ledger.limit - held_after_admission
                                    if held_after_admission.fits(ledger.limit) else None
                                )
                                if admission.deferred_reason is not None:
                                    admission_open = False
                                    blocked_reason = "admission_deferred:" + admission.deferred_reason
                                elif (
                                    admission.backlog_exists
                                    and admission.blocked_dimensions
                                ):
                                    admission_blocked_dimensions = (
                                        admission.blocked_dimensions
                                    )
                                    blocked_reason = "credit_backpressure:" + ",".join(
                                        admission_blocked_dimensions
                                    )
                                else:
                                    admission_blocked_dimensions = ()
                                    if admission.ineligible_dimensions:
                                        blocked_reason = "profile_ineligible:" + ",".join(
                                            admission.ineligible_dimensions
                                        )
                                    elif blocked_reason is not None and blocked_reason.startswith(
                                        "profile_ineligible:"
                                    ):
                                        blocked_reason = None

                # Claims returned by recovery/admission may have less time
                # remaining than this coordinator's configured lease.  Guard
                # them before any lane selection or poll sleep, not merely on
                # the next loop turn.
                guard_waiting(self._monotonic())
                self._guard_process()
                credit_blocked_by_lane = {lane: set() for lane in CoordinatorLane}
                for hold_lane, _held_work, shortage in capacity_holds.values():
                    # A capacity hold stays visible until larger limits
                    # release it; it is re-published every scheduling tick.
                    credit_blocked_by_lane[hold_lane].update(shortage)
                stream_deferred = False
                for lane in _LANE_PRIORITY:
                    if circuit_open:
                        break
                    active = sum(
                        1
                        for active_lane, _, _, _ in in_flight.values()
                        if active_lane == lane
                    )
                    if lane == CoordinatorLane.PREFLIGHT:
                        active += observation.active_slots
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
                        head_disk_growth = 0
                        for position, index in enumerate(ordered_indices):
                            queued_work = queue[index]
                            candidate_hold = transition_hold(queued_work)
                            if stream_failure is not None and (
                                lane == CoordinatorLane.PREFLIGHT or candidate_hold.remote_waits > 0
                            ):
                                stream_deferred = True
                                continue
                            if (
                                stream_decision is not None
                                and candidate_hold.remote_waits > 0
                                and (
                                    not stream_decision.new_post_allowed
                                    or ledger.in_use.remote_waits + provisional_local_total.remote_waits
                                    + candidate_hold.remote_waits > stream_decision.target
                                )
                            ):
                                # This is a new remote permit. Existing durable
                                # remote_waits and every tail lane keep running.
                                stream_deferred = True
                                continue
                            if (
                                needs_heavy_permit(lane, queued_work)
                                and len(heavy_holders) >= self._limits.heavy_work_permits
                            ):
                                # Another stage holds the heavy-work permit:
                                # this one stays queued and visible, never
                                # partly started.
                                credit_blocked_by_lane[lane].add("heavy_work")
                                continue
                            if position > 0 and any(
                                yields_work_disk(queued_work, candidate_hold, head_disk_growth)
                                if name == "work_disk_bytes"
                                else getattr(candidate_hold, name) > 0
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
                                    + observation.credits
                                    + candidate_hold,
                                    ledger.limit,
                                ) + work_disk_shortage(queued_work, candidate_hold)
                            if shortages:
                                if position == 0:
                                    first_shortages = shortages
                                    head_disk_growth = work_disk_growth(queued_work, candidate_hold)
                                    credit_blocked_by_lane[lane].update(shortages)
                                continue
                            selected = (index, queued_work, candidate_hold)
                            break
                        if selected is None:
                            break
                        index, work, local_hold = selected
                        del queue[index]
                        grant_waiting.discard(work.attempt_id)
                        heavy = needs_heavy_permit(lane, work)
                        try:
                            # Admission and preceding dispatch/renewal work may
                            # have consumed the tick's original clock budget.
                            # Decide against current time, not that stale tick.
                            if needs_renewal(work, self._monotonic()):
                                work = renew(work, lane)
                        except Exception as exc:  # noqa: BLE001 - claim loss is fatal
                            circuit_open = True
                            admission_open = False
                            blocked_reason = "claim_renewal_failed"
                            errors.append(
                                f"{work.attempt_id}:{lane.value}:claim:{type(exc).__name__}:{exc}"
                            )
                            trip_fault(_stop_cause(kind="ownership_lost",
                                                  reason_code="claim_renewal_failed",
                                                  origin="coordinator", work=work, lane=lane,
                                                  error=exc))
                            break
                        long_stage = (
                            self._limits.commit_stage_seconds if lane == CoordinatorLane.COMMIT
                            else self._limits.local_stage_seconds if lane == CoordinatorLane.LOCAL
                            else None
                        )
                        stage_guard = StageLeaseGuard(
                            # A long stage's logical bound is fixed here, once;
                            # renewing its claim never extends it.
                            deadline_monotonic=(
                                self._monotonic() + (
                                    long_stage
                                    if long_stage is not None
                                    else self._limits.max_stage_step_seconds
                                )
                            ),
                            _revoked=Event(),
                            _monotonic=self._monotonic,
                            claim_deadline_monotonic=(
                                work.lease_expires_monotonic
                                - self._limits.claim_renew_margin_seconds
                                if long_stage is not None
                                and work.lease_expires_monotonic is not None
                                else None
                            ),
                            attempt_id=work.attempt_id,
                            lane=lane.value,
                            observer=self._stage_observer,
                            heavy_work_permitted=heavy,
                        )
                        if lane == CoordinatorLane.PREFLIGHT:
                            future = pools[lane].submit(
                                self._run_stage_call,
                                self._backend.prepare_remote_io,
                                lane,
                                work,
                                credit_allowance=local_hold,
                                stage_guard=stage_guard,
                            )
                        elif lane == CoordinatorLane.REMOTE:
                            future = pools[lane].submit(
                                self._run_stage_call,
                                self._backend.run_remote,
                                lane,
                                work,
                                credit_allowance=local_hold,
                                stage_guard=stage_guard,
                            )
                        elif lane == CoordinatorLane.LOCAL_PREPARE:
                            future = pools[lane].submit(
                                self._run_stage_call,
                                self._backend.prepare_local_io,
                                lane,
                                work,
                                credit_allowance=local_hold,
                                stage_guard=stage_guard,
                            )
                        elif lane == CoordinatorLane.LOCAL:
                            future = pools[lane].submit(
                                self._run_stage_call,
                                self._backend.run_local,
                                lane,
                                work,
                                credit_allowance=local_hold,
                                stage_guard=stage_guard,
                            )
                        elif lane == CoordinatorLane.COMMIT:
                            future = pools[lane].submit(
                                self._run_stage_call,
                                self._backend.commit,
                                lane,
                                work,
                                credit_allowance=local_hold,
                                stage_guard=stage_guard,
                            )
                        elif lane == CoordinatorLane.CLEANUP:
                            future = pools[lane].submit(
                                self._run_stage_call,
                                self._backend.cleanup,
                                lane,
                                work,
                                credit_allowance=local_hold,
                                stage_guard=stage_guard,
                            )
                        else:
                            future = pools[lane].submit(
                                self._run_stage_call,
                                self._backend.acknowledge,
                                lane,
                                work,
                                stage_guard=stage_guard,
                            )
                        in_flight[future] = (lane, work, stage_guard, local_hold)
                        if heavy:
                            heavy_holders.add(future)
                        if local_hold != ResourceCreditVector():
                            provisional_local[future] = local_hold
                            provisional_local_total = (
                                provisional_local_total + local_hold
                            )
                        active += 1

                preflight_active = sum(
                    1 for lane, _, _, _ in in_flight.values()
                    if lane == CoordinatorLane.PREFLIGHT
                )
                if (
                    observation.pending and not observation.active_slots
                    and not circuit_open and not stop_requested()
                    and stream_failure is None
                    and preflight_active < lane_limits[CoordinatorLane.PREFLIGHT]
                ):
                    observation.dispatch(
                        pools[CoordinatorLane.PREFLIGHT],
                        stage_guard=StageLeaseGuard(
                            deadline_monotonic=self._monotonic() + self._limits.max_stage_step_seconds,
                            _revoked=Event(), _monotonic=self._monotonic,
                            lane="preflight_observation", observer=self._stage_observer,
                        ),
                    )

                if not in_flight:
                    if (
                        stream_failure is not None and not observation.pending
                        and not retry_at and not deferred_claims
                        and all(
                            lane == CoordinatorLane.PREFLIGHT or transition_hold(work).remote_waits > 0
                            for lane, queue in queues.items() for work in queue
                        )
                    ):
                        # Every accepted/runnable tail has drained. Parked
                        # intents and pre-submit queues stay durable and keep
                        # their reservations; no completion/ACK is invented.
                        circuit_open = True
                        admission_open = False
                        blocked_reason = "stream_pressure_closed"
                        trip_fault(_stop_cause(kind="coordinator_circuit",
                                              reason_code="stream_pressure_closed",
                                              origin="coordinator"))
                        emit()
                        return CoordinatorResult(
                            terminal=CoordinatorTerminal.STUCK_OPEN_CIRCUIT,
                            recovery_complete=recovery_complete, admitted=admitted,
                            completed=completed, final_states=tuple(sorted(final.items())),
                            errors=tuple(errors), credits_in_use=ledger.in_use,
                            stop_cause=self._stop_control.first_cause(),
                            termination_kind=termination(CoordinatorTerminal.STUCK_OPEN_CIRCUIT),
                        )
                    if (
                        any(queues.values())
                        and not circuit_open
                        and not retry_at
                        and not deferred_claims
                        and not observation.pending
                        and not stream_deferred
                    ):
                        circuit_open = True
                        admission_open = False
                        blocked_reason = "resource_credit_grant_unavailable"
                        errors.append(
                            "durable queued work cannot obtain its next credit grant"
                        )
                        trip_fault(_stop_cause(kind="coordinator_circuit",
                                              reason_code="resource_credit_grant_unavailable",
                                              origin="coordinator"))
                    if (
                        recovery_complete
                        and not circuit_open
                        and not any(queues.values())
                        and not retry_at
                        and not known
                        and not observation.pending
                        and (not admission_scan_incomplete or stop_requested())
                        and (
                            not wait_for_stream_admission
                            or admission_backlog_exhausted
                            or stream_decision is None
                            or stream_decision.new_post_allowed
                            or stop_requested()
                        )
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
                            stop_cause=self._stop_control.first_cause(),
                            termination_kind=termination(CoordinatorTerminal.QUIESCENT),
                        )
                    if not observation.pending and (circuit_open or (
                        stop_requested()
                        and now - last_progress
                        >= self._limits.idle_open_circuit_seconds
                    )):
                        emit()
                        return CoordinatorResult(
                            terminal=CoordinatorTerminal.STUCK_OPEN_CIRCUIT,
                            recovery_complete=recovery_complete,
                            admitted=admitted,
                            completed=completed,
                            final_states=tuple(sorted(final.items())),
                            errors=tuple(errors),
                            credits_in_use=ledger.in_use,
                            stop_cause=self._stop_control.first_cause(),
                            termination_kind=termination(CoordinatorTerminal.STUCK_OPEN_CIRCUIT),
                        )
                    time.sleep(self._limits.poll_seconds)
                    emit()
                    continue

                done, _ = wait(
                    tuple(in_flight),
                    timeout=self._limits.poll_seconds,
                    return_when=FIRST_COMPLETED,
                )
                self._guard_process()
                if not done:
                    guard_in_flight(self._monotonic())
                    emit()
                    continue
                for future in done:
                    lane, work, stage_guard, granted_delta = in_flight.pop(future)
                    # The permit ends with the stage, whatever its outcome.
                    heavy_holders.discard(future)
                    if lane == CoordinatorLane.LOCAL:
                        heavy_ready.discard(work.attempt_id)
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
                    except StageAdmissionDeferred as exc:
                        # Only the HTTP effect boundary can prove absence;
                        # the backend has made no durable successor/effect.
                        stream_parked[work.attempt_id] = work
                        track_waiting_lease(work)
                        if stream_failure is None:
                            stream_failure = str(exc)
                            errors.append("stream pressure closed new submissions: " + stream_failure)
                        admission_open = False
                        observation.cancel()
                    except StageCapacityBlocked as exc:
                        site_hold = _site_hold_reason(exc.dimensions)
                        if site_hold is not None:
                            errors.append(
                                f"{work.attempt_id}:{lane.value}:{site_hold}:"
                                + ",".join(exc.dimensions)
                            )
                            stop_for_hold(work, lane, site_hold, exc)
                        else:
                            capacity_holds[work.attempt_id] = (lane, work, exc.dimensions)
                            credit_blocked_by_lane[lane].update(exc.dimensions)
                            errors.append(
                                f"{work.attempt_id}:{lane.value}:stage_capacity_hold:"
                                + ",".join(exc.dimensions)
                            )
                            track_waiting_lease(work)
                            exhausted = exhausted_by_holds()
                            if exhausted:
                                # Held documents alone fill a dimension other
                                # work needs: nothing more can progress here.
                                errors.append("capacity_holds_exhausted:" + ",".join(exhausted))
                                stop_for_hold(work, lane, "capacity_holds_exhausted", exc)
                        last_progress = self._monotonic()
                    except StageResourceGrantRequired as exc:
                        wait_now = self._monotonic()
                        required = _credit_union(work.credit_reservation, exc.required)
                        if required == work.credit_reservation:
                            circuit_open = True
                            admission_open = False
                            blocked_reason = "stage_grant_contract_violation"
                            errors.append(
                                f"{work.attempt_id}:{lane.value}:stage requested a grant it holds"
                            )
                            trip_fault(_stop_cause(kind="coordinator_circuit",
                                                  reason_code="stage_grant_contract_violation",
                                                  origin="coordinator", work=work, lane=lane))
                        elif not required.fits(ledger.limit):
                            # No ledger can ever hold this grant, a contradiction
                            # of the configured limits: stop the site with the
                            # verified result kept, never fail, ACK or spin it.
                            shortage = exceeded_dimensions(required, ledger.limit)
                            errors.append(
                                f"{work.attempt_id}:{lane.value}:stage_grant_unsatisfiable:"
                                + ",".join(shortage)
                            )
                            stop_for_hold(work, lane, "stage_grant_unsatisfiable", exc)
                        else:
                            admitted_grants[work.attempt_id] = (
                                durable_reservation(work), required,
                            )
                            grant_waiting.add(work.attempt_id)
                            granted = replace(
                                work, credit_reservation=required,
                                durable_credit_reservation=durable_reservation(work),
                            )
                            known[work.attempt_id] = granted
                            queues[lane].append(granted)
                            track_waiting_lease(granted)
                        last_progress = wait_now
                    except StageHeavyWorkRequired:
                        # Stopped before its heavy work with nothing written:
                        # queued again, dispatched with the next free permit.
                        heavy_ready.add(work.attempt_id)
                        queues[lane].append(work)
                        track_waiting_lease(work)
                        last_progress = self._monotonic()
                    except StageWaiting as exc:
                        wait_now = self._monotonic()
                        if isinstance(exc, StageProviderWaiting):
                            # The provider answered for this attempt: its
                            # failure episode ended, so both bounds restart.
                            retry_attempts.pop(work.attempt_id, None)
                            retry_started_at.pop(work.attempt_id, None)
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
                                f"{work.attempt_id}:{lane.value}:retry budget exhausted ("
                                + _retry_exhaustion_detail(
                                    exc,
                                    attempts=count,
                                    max_attempts=self._limits.retry_max_attempts,
                                    elapsed_seconds=retry_now - started,
                                    window_seconds=self._limits.retry_stuck_seconds,
                                )
                                + ")"
                            )
                            trip_fault(_stop_cause(kind="coordinator_circuit",
                                                  reason_code="retry_stage_stuck",
                                                  origin="coordinator", work=work, lane=lane,
                                                  error=exc))
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
                        in_flight_failures.add(future)
                        if exc.provenance in _DRAIN_PROVENANCES:
                            # Revoked by an already decided stop: the stage
                            # drained at its checkpoint, durable state intact.
                            if not public_stop_observed and not fault_circuit:
                                blocked_reason = "operator_cancelled"
                            errors.append(
                                f"{work.attempt_id}:{lane.value}:{exc.provenance}:{exc}"
                            )
                        else:
                            if exc.provenance == "ownership_lost":
                                errors.append(
                                    f"{work.attempt_id}:{lane.value}:ownership lost:{exc}"
                                )
                            else:
                                blocked_reason = "bounded_stage_deadline_exceeded"
                                errors.append(
                                    f"{work.attempt_id}:{lane.value}:stage deadline exceeded:{exc}"
                                )
                            lost = _classify_stage_failure_safely(exc, lane=lane, work=work)
                            if lost.cause is not None:
                                trip_fault(lost.cause)
                    except Exception as exc:  # noqa: BLE001 - opens the circuit visibly
                        failure = _classify_stage_failure_safely(exc, lane=lane, work=work)
                        circuit_open = True
                        admission_open = False
                        if failure.disposition == "cancellation":
                            # Positive cancellation evidence ends the drain
                            # promptly. It is retry-neutral: the durable
                            # attempt, its output and cache stay as they are.
                            if not public_stop_observed and not fault_circuit:
                                blocked_reason = "operator_cancelled"
                            errors.append(
                                f"{work.attempt_id}:{lane.value}:cancelled:"
                                f"{type(exc).__name__}:{exc}"
                            )
                        else:
                            blocked_reason = f"{lane.value}_unexpected_failure"
                            errors.append(
                                f"{work.attempt_id}:{lane.value}:{type(exc).__name__}:{exc}"
                            )
                            if failure.cause is not None:
                                # Already latched in the stage thread; this
                                # only records that a fault opened the circuit.
                                trip_fault(failure.cause)
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
        except BaseException as error:
            # A synchronous controller failure (recovery, admission, reload,
            # claim, progress or observation) latches its cause before the
            # drain below waits for running stages. An interrupt is an
            # operator's positive cancellation, never a public fault.
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                operator_interrupted = True
            elif not self._stop_control.is_tripped():
                problem = self._trip(
                    _stop_cause(kind="coordinator_fault",
                                reason_code="controller_unexpected_failure",
                                origin="coordinator", error=error)
                )
                if problem is not None:
                    error.add_note(problem)
            raise
        finally:
            # Revoke permission for further effects before waiting for actual
            # completion, including when the controller/singleton probe raises.
            # A running Future is not cancelled or treated as drained early.
            # The provenance tells each draining stage why it stopped.
            observation.cancel()
            if in_flight:
                provenance = self._drain_provenance(
                    stop_requested, operator_interrupted=operator_interrupted,
                )
                for _, _, stage_guard, _ in in_flight.values():
                    stage_guard.revoke(provenance)
            for pool in pools.values():
                pool.shutdown(wait=True, cancel_futures=False)
            observation.drained()

    def _drain_provenance(
        self,
        stop_requested: Callable[[], bool],
        *,
        operator_interrupted: bool,
    ) -> RevocationProvenance:
        if self._stop_control.is_tripped():
            return "public_stop"
        if operator_interrupted:
            return "operator_cancel"
        try:
            requested = stop_requested()
        except Exception:  # noqa: BLE001 - an unknown stop state is not a cancellation
            return "unspecified"
        return "operator_cancel" if requested else "unspecified"


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
    "StageProviderWaiting",
    "StageCapacityBlocked",
    "StageResourceGrantRequired",
    "StageWaiting",
    "StagedCoordinatorBackend",
    "StagedParseCoordinator",
]
