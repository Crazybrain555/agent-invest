"""Bounded online C target. Startup N/P/H and held permits never change here.

Algorithm ``mineru.stream-policy.v2`` is the only executable policy. A cold
start and a target-zero recovery obey the same rule: the original recover
floors (GPU and host) must hold on fresh, identity-valid, known samples for the
full recovery interval before the target becomes 1, then it grows by one per
further interval up to the qualified ceiling. The interval must be continuously
attested: consecutive healthy samples may be at most ``sample_max_age_seconds``
apart, so an unobserved gap between two fresh samples earns no credit. There is no direct start at the
qualified maximum and no probe below the recover floor. A below-recover band
with provably no provider work is reported as ``idle_memory_deficit``; the
target stays where it is.

``new_post_allowed`` is the separate permission for a new remote POST. It is
true only for a fresh, known, safe decision with a positive target outside the
pause/reduce bands. A held positive target during an unknown grace interval
keeps accepted work running but never authorizes a new POST.

The superseded ``mineru.stream-policy.v1`` (qualified start) stays decodable in
activation files for audits of old releases; the execution compositions refuse
it (see ``require_executable_stream_activation``). This class implements v2 only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from math import isfinite
import re
from threading import Lock
import time
from collections.abc import Callable

from disclosure_anchor.application.ports.mineru_stream_pressure import (
    StreamPressurePort, StreamPressureSample, StreamSubmissionDeferred,
)


STREAM_POLICY_ALGORITHM_V1 = "mineru.stream-policy.v1"
STREAM_POLICY_ALGORITHM_V2 = "mineru.stream-policy.v2"
# Decision reasons whose fresh, known sample may authorize a new POST.
_NEW_POST_REASONS = frozenset({"holding_pressure", "recovery", "qualified_target"})


@dataclass(frozen=True, slots=True)
class StreamPolicyConfig:
    qualified_max: int
    runtime_identity_sha256: str
    owner_identity_sha256: str
    gpu_pause_bytes: int = 512 * 1024**2
    gpu_reduce_bytes: int = 1024**3
    gpu_recover_bytes: int = 1536 * 1024**2
    host_pause_bytes: int = 4 * 1024**3
    host_recover_bytes: int = 6 * 1024**3
    sample_max_age_seconds: float = 3.0
    missing_pause_seconds: float = 10.0
    recovery_seconds: float = 10.0
    reduction_interval_seconds: float = 2.0

    def __post_init__(self) -> None:
        if type(self.qualified_max) is not int or not 1 <= self.qualified_max <= 128:
            raise ValueError("qualified stream capacity is invalid")
        for value in (self.runtime_identity_sha256, self.owner_identity_sha256):
            if type(value) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
                raise ValueError("stream policy identity is invalid")
        for name in ("gpu_pause_bytes", "gpu_reduce_bytes", "gpu_recover_bytes", "host_pause_bytes", "host_recover_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("stream memory threshold is invalid")
        if not self.gpu_pause_bytes < self.gpu_reduce_bytes < self.gpu_recover_bytes or self.host_pause_bytes >= self.host_recover_bytes:
            raise ValueError("stream pressure hysteresis is invalid")
        for name in ("sample_max_age_seconds", "missing_pause_seconds", "recovery_seconds", "reduction_interval_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isfinite(value) or value <= 0:
                raise ValueError("stream policy timing is invalid")

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class StreamAdmissionDecision:
    target: int
    reason: str
    sample_sequence: int | None
    evidence_sha256: str | None
    sample_observed_monotonic: float | None
    unsafe: bool = False
    new_post_allowed: bool = False


class MineruStreamPolicy:
    """The executable ``mineru.stream-policy.v2`` algorithm."""

    algorithm = STREAM_POLICY_ALGORITHM_V2

    def __init__(self, config: StreamPolicyConfig) -> None:
        if type(config) is not StreamPolicyConfig:
            raise ValueError("stream policy requires an exact config")
        self.config = config
        self._target = 0
        self._last_sequence = -1
        self._last_sample: StreamPressureSample | None = None
        self._last_now = -1.0
        self._missing_since: float | None = None
        self._healthy_since: float | None = None
        # Observation time of the latest sample credited to the healthy window.
        self._healthy_observed: float | None = None
        self._last_reduction = float("-inf")
        self._unsafe_reason: str | None = None

    def evaluate(self, sample: StreamPressureSample | None, *, now: float) -> StreamAdmissionDecision:
        if isinstance(now, bool) or not isfinite(now) or now < 0 or now < self._last_now:
            raise ValueError("stream policy clock regressed")
        self._last_now = now
        c = self.config
        if sample is not None:
            if type(sample) is not StreamPressureSample:
                raise ValueError("stream pressure port returned an invalid sample")
            if (sample.runtime_identity_sha256, sample.owner_identity_sha256) != (c.runtime_identity_sha256, c.owner_identity_sha256):
                self._unsafe_reason = "identity_drift"
            if sample.sequence < self._last_sequence or (sample.sequence == self._last_sequence and sample != self._last_sample):
                self._unsafe_reason = "sample_sequence_drift"
            if self._last_sample is not None and sample.observed_monotonic < self._last_sample.observed_monotonic:
                self._unsafe_reason = "sample_clock_drift"
            if sample.observed_monotonic > now:
                self._unsafe_reason = "sample_from_future"
            if sample.unsafe_reason:
                self._unsafe_reason = sample.unsafe_reason
            self._last_sequence = sample.sequence
            self._last_sample = sample
        if self._unsafe_reason is not None:
            self._target = 0
            self._healthy_since = None
            return self._decision(sample, "unsafe:" + self._unsafe_reason, unsafe=True)
        # An observed hard-pressure value closes new submissions even when
        # the other lane is unknown. Retained stale low values are conservative
        # pause evidence, never permission to recover/increase; recovery below
        # still requires the entire joined observation to be fresh and valid.
        if sample is not None and (
            (sample.gpu_free_bytes is not None and sample.gpu_free_bytes < c.gpu_pause_bytes)
            or (sample.host_available_bytes is not None and sample.host_available_bytes < c.host_pause_bytes)
        ):
            self._target = 0
            self._healthy_since = None
            return self._decision(sample, "memory_pause")
        unknown = (sample is None or sample.unknown_reason is not None
                   or now - sample.observed_monotonic > c.sample_max_age_seconds
                   or any(value is None for value in (sample.gpu_free_bytes, sample.host_available_bytes, sample.http_active, sample.http_pending)))
        if unknown:
            self._healthy_since = None
            if self._missing_since is None:
                self._missing_since = now
            if now - self._missing_since >= c.missing_pause_seconds:
                self._target = 0
            return self._decision(sample, "pressure_unknown")
        assert sample is not None and sample.gpu_free_bytes is not None and sample.host_available_bytes is not None
        self._missing_since = None
        if sample.gpu_free_bytes < c.gpu_reduce_bytes:
            self._healthy_since = None
            if now - self._last_reduction >= c.reduction_interval_seconds:
                self._target = max(0, self._target - 1)
                self._last_reduction = now
            return self._decision(sample, "gpu_memory_pressure")
        # Cold start and recovery from target zero share this one rule: the
        # original recover floors, on fresh known samples, for the whole
        # interval, then one slot. Pending H demand neither vetoes nor speeds
        # it, and nothing jumps to the qualified maximum.
        clear = sample.gpu_free_bytes >= c.gpu_recover_bytes and sample.host_available_bytes >= c.host_recover_bytes
        if not clear:
            self._healthy_since = None
            provider_idle = (
                sample.provider_nonterminal_tasks == 0
                and sample.http_active == 0
                and sample.http_pending == 0
            )
            if self._target == 0 and provider_idle:
                # Physical shortage with no provider work to shed: an explicit
                # deficit, not an admission probe and not recovered service.
                return self._decision(sample, "idle_memory_deficit")
            return self._decision(sample, "holding_pressure")
        if (
            self._healthy_since is not None
            and (
                self._healthy_observed is None
                or sample.observed_monotonic - self._healthy_observed > c.sample_max_age_seconds
            )
        ):
            # Nothing attests the interval between the two samples.
            self._healthy_since = None
        self._healthy_observed = sample.observed_monotonic
        if self._healthy_since is None:
            self._healthy_since = now
        elif now - self._healthy_since >= c.recovery_seconds:
            self._target = min(c.qualified_max, self._target + 1)
            self._healthy_since = now
        return self._decision(sample, "recovery" if self._target < c.qualified_max else "qualified_target")

    def _decision(self, sample: StreamPressureSample | None, reason: str, *, unsafe: bool = False) -> StreamAdmissionDecision:
        return StreamAdmissionDecision(
            self._target, reason, None if sample is None else sample.sequence,
            None if sample is None else sample.evidence_sha256,
            None if sample is None else sample.observed_monotonic, unsafe,
            new_post_allowed=(not unsafe and self._target > 0 and reason in _NEW_POST_REASONS),
        )


class StreamAdmissionControl:
    """One policy used by the scheduler and the pre-POST effect boundary."""

    def __init__(self, policy: MineruStreamPolicy, pressure: StreamPressurePort, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.policy, self.pressure, self._monotonic = policy, pressure, monotonic
        self._lock = Lock()

    def current(self) -> StreamAdmissionDecision:
        with self._lock:
            return self.policy.evaluate(self.pressure.latest(), now=self._monotonic())

    def assert_submission_allowed(self, *, runtime_identity_sha256: str) -> None:
        if runtime_identity_sha256 != self.policy.config.runtime_identity_sha256:
            raise ValueError("submission runtime differs from stream policy")
        decision = self.current()
        if not decision.new_post_allowed or decision.unsafe:
            raise StreamSubmissionDeferred(decision.reason, unsafe=decision.unsafe)
        # This is an already durable remote_wait permit, not a new grant.
        # A positive reduced target cannot revoke it or count it twice.
