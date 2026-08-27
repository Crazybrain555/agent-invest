"""Ports for passive capacity sampling; implementations must remain read-only."""

from __future__ import annotations

from typing import Protocol

from disclosure_anchor.application.contracts.capacity import RawSampleValues


class CapacitySamplerPort(Protocol):
    source: str
    cadence_seconds: float

    def sample(self) -> RawSampleValues:
        """Read one external point without mutating the observed service."""


__all__ = ["CapacitySamplerPort"]
