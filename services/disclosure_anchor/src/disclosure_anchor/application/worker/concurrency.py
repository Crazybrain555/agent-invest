"""Client-side adaptive parse concurrency.

Netflix concurrency-limits AIMDLimit pattern (loss-based): the configured
concurrency is only the upper bound; the effective in-flight limit grows
additively on success and backs off multiplicatively on infrastructure
failures, so dispatch tracks what the backend can actually sustain instead
of pushing a fixed batch into it and tripping breakers.  Latency-gradient
controllers (Envoy adaptive concurrency) were rejected for this workload:
document parses vary 30s-300s by size, which destabilizes any minRTT
baseline — their own documented limitation.

Not thread-safe by contract: the parse stage records outcomes from its
main dispatch loop only.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AdaptiveConcurrencyLimit:
    max_limit: int
    min_limit: int = 1
    backoff_ratio: float = 0.5
    _limit: float = field(init=False)

    def __post_init__(self) -> None:
        if self.max_limit < 1:
            raise ValueError("max_limit must be positive")
        if not 0.0 < self.backoff_ratio < 1.0:
            raise ValueError("backoff_ratio must be in (0, 1)")
        self.min_limit = max(1, min(self.min_limit, self.max_limit))
        # Start wide open: the bound was chosen for a healthy backend and
        # evidence of overload arrives as drops, which back off decisively.
        self._limit = float(self.max_limit)

    @property
    def current(self) -> int:
        return max(self.min_limit, int(self._limit))

    def on_success(self, *, inflight: int) -> None:
        # Growing while underutilized would balloon the limit without
        # evidence that the backend sustains it (AIMDLimit guard).
        if inflight * 2 >= self.current:
            self._limit = min(float(self.max_limit), self._limit + 1.0)

    def on_drop(self) -> None:
        self._limit = max(
            float(self.min_limit), self._limit * self.backoff_ratio
        )
