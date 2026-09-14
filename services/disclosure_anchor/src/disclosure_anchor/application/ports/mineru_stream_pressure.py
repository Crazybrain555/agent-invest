"""Read-only, non-blocking pressure input for whole-document admission."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import re
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StreamPressureSample:
    """One joined sample; time is the oldest contributing local observation.

    Missing required input is unknown. Evidence keeps the original per-lane
    timestamps and errors; the policy never substitutes zeros for them.
    """

    sequence: int
    observed_monotonic: float
    runtime_identity_sha256: str
    owner_identity_sha256: str
    evidence_sha256: str
    gpu_free_bytes: int | None
    host_available_bytes: int | None
    http_active: int | None
    http_pending: int | None
    unknown_reason: str | None = None
    unsafe_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("stream sample sequence is invalid")
        if isinstance(self.observed_monotonic, bool) or not isfinite(self.observed_monotonic) or self.observed_monotonic < 0:
            raise ValueError("stream sample monotonic time is invalid")
        for value in (self.runtime_identity_sha256, self.owner_identity_sha256, self.evidence_sha256):
            if type(value) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
                raise ValueError("stream sample identity is invalid")
        for measurement in (self.gpu_free_bytes, self.host_available_bytes, self.http_active, self.http_pending):
            if measurement is not None and (type(measurement) is not int or measurement < 0):
                raise ValueError("stream pressure value is invalid")
        for reason in (self.unknown_reason, self.unsafe_reason):
            if reason is not None and (type(reason) is not str or not reason or len(reason) > 256):
                raise ValueError("stream pressure reason is invalid")


class StreamPressurePort(Protocol):
    def latest(self) -> StreamPressureSample | None:
        """Return the cached observation without network, subprocess or wait."""
        ...


class StreamSubmissionDeferred(RuntimeError):
    """A proved-absent submission must wait; its durable intent stays owned."""

    def __init__(self, message: str, *, unsafe: bool = False) -> None:
        super().__init__(message)
        if type(unsafe) is not bool:
            raise ValueError("submission pause safety flag is invalid")
        self.unsafe = unsafe


class StreamSubmissionGuard(Protocol):
    def assert_submission_allowed(self, *, runtime_identity_sha256: str) -> None: ...
