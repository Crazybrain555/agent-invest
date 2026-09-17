"""R22 private measurement rules: pure, no I/O and no production-worker dependency.

Integration status: candidate core, NOT wired into the R22 entry points. The
companion implementation-steps-r22.md defines the v4 receipt/v3 frame migration.
Existing source/runtime/epoch validators must run before these timing rules.
All integers are nanoseconds; never pass a UTC timestamp or another clock boot.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

NS = 1_000_000_000
MAX_I64 = (1 << 63) - 1
SAMPLE_MAX_SECONDS = 8500
PRE_GO_MAX_SECONDS = 20
POST_SAMPLE_MAX_SECONDS = 60
LANE_MAX_SECONDS = SAMPLE_MAX_SECONDS + PRE_GO_MAX_SECONDS + POST_SAMPLE_MAX_SECONDS
WIRE_MAX_SECONDS = LANE_MAX_SECONDS + 10
FINITE_COMMAND_MAX_SECONDS = WIRE_MAX_SECONDS + 10
DEFAULT_MAX_AGE_NS = NS


def _integer(value: int, label: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_I64:
        raise ValueError(f"{label}: expected a bounded integer >= {minimum}")
    return value


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label}: invalid SHA-256 identity")
    return value


@dataclass(frozen=True, slots=True)
class SamplingPlan:
    """Frozen BEFORE the first collector call, from the owner's retained intent.

    start_ns is in the independent Mac observer's time.monotonic_ns domain.
    planned_end_ns is derived, never supplied by the last available frame.
    Canonical serialization and private-file ownership stay with existing code.
    """
    run_id: str
    owner_intent_sha256: str
    observer_clock_domain_sha256: str
    start_ns: int
    duration_ns: int
    gpu_period_ns: int = 250_000_000
    host_period_ns: int = NS

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id: missing")
        _digest(self.owner_intent_sha256, "owner_intent_sha256")
        _digest(self.observer_clock_domain_sha256, "observer_clock_domain_sha256")
        _integer(self.start_ns, "start_ns", 1)
        _integer(self.duration_ns, "duration_ns", 1)
        if self.start_ns + self.duration_ns > MAX_I64:
            raise ValueError("planned end exceeds bounded clock range")
        if self.duration_ns > SAMPLE_MAX_SECONDS * NS:
            raise ValueError("duration exceeds finite measurement ceiling")
        if self.gpu_period_ns not in (250_000_000, 500_000_000):
            raise ValueError("unsupported GPU cadence")
        if self.host_period_ns != NS:
            raise ValueError("this policy freezes the existing 1 s host cadence")

    @property
    def end_ns(self) -> int:
        return self.start_ns + self.duration_ns

    def period_ns(self, lane: str) -> int:
        if lane == "gpu_fast":
            return self.gpu_period_ns
        if lane == "host_slow":
            return self.host_period_ns
        raise ValueError("unknown lane")


@dataclass(frozen=True, slots=True)
class SlotCoverage:
    expected: int
    observed: int
    missing: int
    leading_missing: int
    trailing_missing: int
    interior_missing: int

    @property
    def complete(self) -> bool:
        return self.missing == 0


def slot_coverage(plan: SamplingPlan, lane: str, scheduled_ns: Iterable[int]) -> SlotCoverage:
    """Exact half-open slot accounting; duplicates/reordering/off-grid are invalid.

    Collection duration, lateness, support, global sequence and raw-byte counts
    remain independently validated. A canceled run does NOT shorten this plan.
    """
    period = plan.period_ns(lane)
    expected = (plan.duration_ns + period - 1) // period
    previous = -1
    first = None
    observed = 0
    for stamp in scheduled_ns:
        _integer(stamp, "scheduled_ns", 1)
        offset = stamp - plan.start_ns
        if not 0 <= offset < plan.duration_ns or offset % period:
            raise ValueError("scheduled slot is outside the frozen grid")
        index = offset // period
        if index <= previous:
            raise ValueError("duplicate or out-of-order scheduled slot")
        if first is None:
            first = index
        previous = index
        observed += 1
    if first is None:
        return SlotCoverage(expected, 0, expected, expected, 0, 0)
    leading = first
    trailing = expected - 1 - previous
    missing = expected - observed
    return SlotCoverage(expected, observed, missing, leading, trailing, missing - leading - trailing)


@dataclass(frozen=True, slots=True)
class PullTiming:
    """One original reply bound to the requesting collector, not a cached reply.

    q_* are same-native-boot QPC nanoseconds; local_* same-Mac-boot monotonic.
    New pull.v1 is fresh-per-request: native sampling begins AFTER validating
    this request. It must not return the old endpoint's latest-sample cache.
    QPC durations are diagnostics, never subtracted from Mac timestamps.
    """
    request_nonce: str
    after_sequence: int
    sample_sequence: int
    local_request_ns: int
    local_response_ns: int
    q_request_ns: int
    q_capture_start_ns: int
    q_capture_end_ns: int
    q_reply_ns: int


@dataclass(frozen=True, slots=True)
class CaptureBounds:
    """Conservative containment interval for the full source collection operation."""
    earliest_start_ns: int
    latest_end_ns: int
    receive_age_upper_ns: int
    round_trip_ns: int
    native_service_ns: int
    source_collection_ns: int

    @property
    def width_ns(self) -> int:
        return self.latest_end_ns - self.earliest_start_ns


def pull_capture_bounds(
    timing: PullTiming, *, expected_nonce: str, expected_after_sequence: int,
    maximum_age_ns: int = DEFAULT_MAX_AGE_NS,
) -> CaptureBounds:
    """Fresh-request causal enclosure, independent of offset AND clock rates.

    Protocol prerequisite: the pinned pull.v1 endpoint invokes the REAL backend
    exactly once for this valid request; it has no periodic sampler/cache path.
    Existing closed decoding, source/session/boot/epoch and per-lane monotonic
    ordering must pass first. Retain the original request-bound witness.
    Native event order is checked only within native QPC. Source duration and
    local RTT are not compared as if oscillator rates were identical.

    Mac s occurs before issuing this request; Mac f after its complete reply.
    Hence the actual backend operation is contained in [s,f] by causality.
    No midpoint, global calibration, slope, guessed ppm or UTC gate is needed.
    The width is reported explicitly, not described as an exact sample instant.
    """
    if (not isinstance(expected_nonce, str)
            or re.fullmatch(r"[0-9a-f]{32}", expected_nonce) is None
            or timing.request_nonce != expected_nonce):
        raise ValueError("request nonce mismatch")
    _integer(expected_after_sequence, "expected_after_sequence")
    if (type(timing.after_sequence) is not int
            or timing.after_sequence != expected_after_sequence
            or type(timing.sample_sequence) is not int
            or timing.sample_sequence != expected_after_sequence + 1):
        raise ValueError("request/sample sequence mismatch")
    for label in ("local_request_ns", "local_response_ns", "q_request_ns",
                  "q_capture_start_ns", "q_capture_end_ns", "q_reply_ns"):
        _integer(getattr(timing, label), label, 1)
    _integer(maximum_age_ns, "maximum_age_ns", 1)
    if maximum_age_ns > DEFAULT_MAX_AGE_NS:
        raise ValueError("R22 does not widen the existing one-second freshness ceiling")
    s, f = timing.local_request_ns, timing.local_response_ns
    a, b = timing.q_request_ns, timing.q_reply_ns
    c, d = timing.q_capture_start_ns, timing.q_capture_end_ns
    if not s <= f or not a <= c <= d <= b:
        raise ValueError("invalid ordering or cached sample on the fresh-pull path")
    age = f - s
    if age > maximum_age_ns:
        raise ValueError("fresh sample delivery not proved within one second")
    return CaptureBounds(s, f, age, age, b - a, d - c)


@dataclass(frozen=True, slots=True)
class CounterPoint:
    sequence: int
    epoch_sha256: str
    earliest_start_ns: int
    latest_end_ns: int
    value: int | None
    supported: bool = True


@dataclass(frozen=True, slots=True)
class CounterEnvelope:
    before_sequence: int
    after_sequence: int
    outer_delta: int


def counter_envelope(
    points: Iterable[CounterPoint], *, window_start_ns: int, window_end_ns: int,
) -> CounterEnvelope:
    """Zero credit only from a PROVED outer envelope, not request-start times.

    Caller first proves frozen slot coverage, freshness and same Mac domain for
    the whole stream. Unknown support/reset/gaps inside the selected envelope
    fail closed; an observed positive outer delta conservatively fails a zero
    counter gate, without claiming the increment occurred in the inner window.
    """
    _integer(window_start_ns, "window_start_ns", 1)
    _integer(window_end_ns, "window_end_ns", window_start_ns + 1)
    values = tuple(points)
    if not values:
        raise ValueError("counter stream missing")
    for index, point in enumerate(values):
        _integer(point.sequence, "sequence", 1)
        _digest(point.epoch_sha256, "epoch")
        _integer(point.earliest_start_ns, "earliest_start_ns", 1)
        _integer(point.latest_end_ns, "latest_end_ns", 1)
        if type(point.supported) is not bool:
            raise ValueError("counter support flag must be boolean")
        if point.earliest_start_ns > point.latest_end_ns:
            raise ValueError("invalid counter capture interval")
        if index and point.sequence <= values[index - 1].sequence:
            raise ValueError("counter sequence rollback or duplicate")
    left = [i for i, p in enumerate(values) if p.latest_end_ns <= window_start_ns]
    right = [i for i, p in enumerate(values) if p.earliest_start_ns >= window_end_ns]
    if not left or not right:
        raise ValueError("counter edge is not proved outside the business interval")
    first, last = max(left), min(right)
    if first >= last:
        raise ValueError("counter edge order invalid")
    selected = values[first:last + 1]
    previous = None
    for point in selected:
        if not point.supported or type(point.value) is not int or point.value < 0:
            raise ValueError("counter unsupported or invalid")
        if point.epoch_sha256 != selected[0].epoch_sha256:
            raise ValueError("counter epoch changed")
        if previous is not None:
            if point.sequence != previous.sequence + 1:
                raise ValueError("counter gap in outer envelope")
            if previous.value is None or point.value < previous.value:
                raise ValueError("counter reset in outer envelope")
        previous = point
    first_value, last_value = selected[0].value, selected[-1].value
    assert first_value is not None and last_value is not None
    return CounterEnvelope(selected[0].sequence, selected[-1].sequence, last_value - first_value)


def possibly_overlaps(bounds: CaptureBounds, *, start_ns: int, end_ns: int) -> bool:
    """Include uncertain-edge gauges conservatively; no midpoint/drop-tail rule."""
    if start_ns >= end_ns:
        raise ValueError("empty gauge window")
    return bounds.latest_end_ns >= start_ns and bounds.earliest_start_ns < end_ns


@dataclass(frozen=True, slots=True)
class CoverageBudget:
    """Terms supplied by the EXISTING launch-budget/composition-root authority.

    This function must NOT become another source for prepare/stage/launch limits.
    The independent driver freezes those exact existing terms before any child.
    """
    entry_to_spawn_ns: int
    launcher_transport_ns: int
    finish_poll_allowance_ns: int
    retrieval_ns: int
    driver_start_ns: int
    driver_launch_ns: int
    driver_cleanup_ns: int
    edge_reserve_each_ns: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            _integer(getattr(self, name), name)

    @property
    def required_ns(self) -> int:
        return (self.entry_to_spawn_ns + self.launcher_transport_ns
                + self.finish_poll_allowance_ns + self.retrieval_ns
                + self.driver_start_ns + self.driver_launch_ns + self.driver_cleanup_ns
                + 2 * self.edge_reserve_each_ns)

    def require_fits(self, duration_ns: int) -> int:
        _integer(duration_ns, "duration_ns", 1)
        if duration_ns > SAMPLE_MAX_SECONDS * NS or self.required_ns > duration_ns:
            raise ValueError("frozen coverage budget does not fit")
        return duration_ns - self.required_ns


def require_start_headroom(*, earliest_starter_spawn_ns: int, sampling_start_ns: int) -> None:
    """Local causal pre-GO bound; no subtraction against native startup QPC."""
    _integer(earliest_starter_spawn_ns, "earliest_starter_spawn_ns", 1)
    _integer(sampling_start_ns, "sampling_start_ns", earliest_starter_spawn_ns)
    if sampling_start_ns - earliest_starter_spawn_ns > PRE_GO_MAX_SECONDS * NS:
        raise ValueError("native pre-GO reserve exhausted; no PDF admission")
