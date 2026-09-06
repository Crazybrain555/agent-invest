"""Cooperative execution permission, separate from durable claim identity."""

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from threading import Event


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


__all__ = ["StageLeaseGuard", "StageLeaseLost"]
