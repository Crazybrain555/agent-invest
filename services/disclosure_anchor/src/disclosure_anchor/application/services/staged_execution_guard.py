"""Cooperative execution permission, separate from durable claim identity."""

from collections.abc import Callable
from dataclasses import dataclass, field
from math import isfinite
from threading import Event, Lock
import time
from typing import Literal

from disclosure_anchor.application.ports.staged_execution import StageNote, StageObserverPort


# Why a bounded stage lost permission. The provenance comes from the actual
# revoker (or the guard's own clock), never from matching an error message:
# ``operator_cancel`` and ``public_stop`` are drains of an already decided stop,
# ``ownership_lost`` and ``deadline_exhausted`` are faults, and ``unspecified``
# (a legacy caller or an externally set event) is never evidence of a pure
# cancellation.
RevocationProvenance = Literal[
    "operator_cancel",
    "public_stop",
    "ownership_lost",
    "deadline_exhausted",
    "unspecified",
]
REVOCATION_PROVENANCES: frozenset[str] = frozenset(
    {"operator_cancel", "public_stop", "ownership_lost", "deadline_exhausted", "unspecified"}
)


class StageLeaseLost(RuntimeError):
    """A bounded backend step lost its execution fence before a side effect."""

    def __init__(
        self,
        message: str = "bounded stage lease expired",
        *,
        provenance: RevocationProvenance = "unspecified",
    ) -> None:
        if provenance not in REVOCATION_PROVENANCES:
            raise ValueError("stage revocation provenance is outside the closed vocabulary")
        super().__init__(message)
        self.provenance: RevocationProvenance = provenance


@dataclass(frozen=True, slots=True)
class StageLeaseGuard:
    """Cooperative hard boundary checked around every backend IO chunk.

    The optional observer only receives scalar notes keyed by this guard's
    attempt/lane; it never affects the fence, and its failures are counted by
    the observer instead of surfacing into the guarded stage.
    """

    deadline_monotonic: float
    _revoked: Event
    _monotonic: Callable[[], float]
    claim_deadline_monotonic: float | None = None
    attempt_id: str | None = None
    lane: str | None = None
    observer: StageObserverPort | None = field(default=None, compare=False)
    # The coordinator's shared heavy-work permit for this stage: True granted,
    # False dispatched without it (the stage must stop before any whole-object
    # decode), None for a standalone caller that runs one stage at a time.
    heavy_work_permitted: bool | None = field(default=None, compare=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)
    _revocation: RevocationProvenance | None = field(
        default=None, init=False, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if (
            isinstance(self.deadline_monotonic, bool)
            or not isinstance(self.deadline_monotonic, (int, float))
            or not isfinite(self.deadline_monotonic)
            or self.deadline_monotonic <= 0
        ):
            raise ValueError("stage lease deadline must be finite and positive")
        if self.claim_deadline_monotonic is not None:
            self._validate_claim_deadline(self.claim_deadline_monotonic)

    def checkpoint(self) -> None:
        self.remaining_seconds()

    def remaining_seconds(self) -> float:
        """Return the live bounded-stage budget after checking the claim fence."""

        with self._lock:
            return self._remaining_seconds()

    def _remaining_seconds(self) -> float:
        observed = self._monotonic()
        deadline = self.deadline_monotonic
        if self.claim_deadline_monotonic is not None:
            deadline = min(deadline, self.claim_deadline_monotonic)
        if self._revoked.is_set():
            self._record_revocation("unspecified")
        elif (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not isfinite(observed)
            or observed >= deadline
        ):
            # The guard's own clock proves the budget is gone; that is a
            # fault even when a cancellation arrives at the same time.
            self._record_revocation("deadline_exhausted")
            self._revoked.set()
        else:
            return float(deadline - observed)
        raise StageLeaseLost(
            "bounded stage lease expired",
            provenance=self._revocation or "unspecified",
        )

    def refresh_claim_deadline(self, value: float) -> None:
        """Extend verified claim coverage; never revive a lost execution fence."""

        with self._lock:
            self._remaining_seconds()
            self._validate_claim_deadline(value)
            if self.claim_deadline_monotonic is None or value <= self.claim_deadline_monotonic:
                raise ValueError("claim deadline must strictly increase an existing fence")
            object.__setattr__(self, "claim_deadline_monotonic", float(value))

    @staticmethod
    def _validate_claim_deadline(value: float) -> None:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not isfinite(value) or value <= 0):
            raise ValueError("claim deadline must be finite and positive")

    def revoke(self, provenance: RevocationProvenance = "unspecified") -> None:
        """Withdraw permission; the first recorded provenance is immutable."""

        if provenance not in REVOCATION_PROVENANCES:
            raise ValueError("stage revocation provenance is outside the closed vocabulary")
        with self._lock:
            self._record_revocation(provenance)
            self._revoked.set()

    @property
    def revocation_provenance(self) -> RevocationProvenance | None:
        """Why permission was withdrawn, or ``None`` while it is still live."""

        with self._lock:
            if self._revocation is None and self._revoked.is_set():
                return "unspecified"
            return self._revocation

    def _record_revocation(self, provenance: RevocationProvenance) -> None:
        # Caller holds ``_lock``.
        if self._revocation is None:
            object.__setattr__(self, "_revocation", provenance)

    def note(self, kind: str, **scalars: int | str | None) -> None:
        """Emit one scalar observation; never raises and never touches the fence."""

        observer = self.observer
        if observer is None:
            return
        try:
            observer.note(StageNote(
                attempt_id=self.attempt_id, lane=self.lane, kind=kind,
                monotonic_ns=time.monotonic_ns(), scalars=tuple(sorted(scalars.items())),
            ))
        except Exception as exc:  # noqa: BLE001 - measurement must not fail the stage
            try:
                observer.record_failure(exc)
            except Exception:  # noqa: BLE001 - a failing observer cannot be reported further
                pass


__all__ = [
    "REVOCATION_PROVENANCES",
    "RevocationProvenance",
    "StageLeaseGuard",
    "StageLeaseLost",
]
