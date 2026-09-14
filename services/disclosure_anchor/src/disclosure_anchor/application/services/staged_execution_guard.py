"""Cooperative execution permission, separate from durable claim identity."""

from collections.abc import Callable
from dataclasses import dataclass, field
from math import isfinite
from threading import Event, Lock


class StageLeaseLost(RuntimeError):
    """A bounded backend step lost its execution fence before a side effect."""


@dataclass(frozen=True, slots=True)
class StageLeaseGuard:
    """Cooperative hard boundary checked around every backend IO chunk."""

    deadline_monotonic: float
    _revoked: Event
    _monotonic: Callable[[], float]
    claim_deadline_monotonic: float | None = None
    _lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)

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
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not isfinite(observed)
            or self._revoked.is_set()
            or observed >= deadline
        ):
            self._revoked.set()
            raise StageLeaseLost("bounded stage lease expired")
        return float(deadline - observed)

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

    def revoke(self) -> None:
        self._revoked.set()


__all__ = ["StageLeaseGuard", "StageLeaseLost"]
