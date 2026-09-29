"""Read-only, non-blocking pressure input for whole-document admission."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import re
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StreamPressureSample:
    """One joined sample; time is the oldest contributing local observation.

    That time is a conservative bound in the local monotonic domain and may
    precede its origin: a remote sample can already be old when first read.
    Before every lane has published once there is no joined time: it is
    ``None``, never a placeholder number, and the sample must be unknown.
    Missing required input is unknown. Evidence keeps the original per-lane
    timestamps and errors; the policy never substitutes zeros for them.

    ``provider_nonterminal_tasks`` is the validated API health's durable
    nonterminal count. ``None`` means unknown and is never read as idle.
    """

    sequence: int
    observed_monotonic: float | None
    runtime_identity_sha256: str
    owner_identity_sha256: str
    evidence_sha256: str
    gpu_free_bytes: int | None
    host_available_bytes: int | None
    http_active: int | None
    http_pending: int | None
    unknown_reason: str | None = None
    unsafe_reason: str | None = None
    provider_nonterminal_tasks: int | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("stream sample sequence is invalid")
        if self.observed_monotonic is not None and (
            isinstance(self.observed_monotonic, bool)
            or not isinstance(self.observed_monotonic, (int, float))
            or not isfinite(self.observed_monotonic)
        ):
            raise ValueError("stream sample monotonic time is invalid")
        for value in (self.runtime_identity_sha256, self.owner_identity_sha256, self.evidence_sha256):
            if type(value) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
                raise ValueError("stream sample identity is invalid")
        for measurement in (self.gpu_free_bytes, self.host_available_bytes, self.http_active, self.http_pending,
                            self.provider_nonterminal_tasks):
            if measurement is not None and (type(measurement) is not int or measurement < 0):
                raise ValueError("stream pressure value is invalid")
        for reason in (self.unknown_reason, self.unsafe_reason):
            if reason is not None and (type(reason) is not str or not reason or len(reason) > 256):
                raise ValueError("stream pressure reason is invalid")
        if self.observed_monotonic is None and self.unknown_reason is None:
            raise ValueError("stream sample without a joined time must be unknown")


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
