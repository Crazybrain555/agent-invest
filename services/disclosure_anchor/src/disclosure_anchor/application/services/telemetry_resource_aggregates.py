"""Pure window aggregates over already-replayed synchronized telemetry frames.

Nothing here reads a file, a clock or a device: the caller supplies the exact
frames a sealed observer replay produced and the UTC bracket to score them in.
Every component counter is kept separately and a counter that could not be
trusted - unsupported sample, reset, or too few samples - becomes ``None`` with
a named problem. A missing measure is never clamped to zero, because a zero
OOM/preemption delta is the very claim a resource gate is asked to prove.

``derive_resource_aggregates`` scores v2 frames in the historical UTC bracket.
``derive_resource_aggregates_v4`` scores v3 frames in the caller's own Mac
monotonic window: every frame carries a proved capture interval, so a gauge
includes every interval that could overlap the window and a counter is credited
only from an envelope whose edges are proved outside it. The two functions share
the result type and the delivery thresholds; they do not share a time domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re

from disclosure_anchor.application.contracts.synchronized_telemetry import (
    SynchronizedSamplingPlanV1, SynchronizedTelemetryFrameV2, SynchronizedTelemetryFrameV3,
)
from disclosure_anchor.application.services.resident_measurement_policy import (
    CaptureBounds, CounterPoint, PullTiming, counter_envelope, possibly_overlaps, pull_capture_bounds,
)

# The host counters the M6 resource gate reads, by their exact frame field names.
_MEMORY_COUNTERS: tuple[str, ...] = ("oom_total", "oom_kill_total", "oom_group_kill_total")
_QUEUE_COUNTER = "vllm_preemptions_total"
_HOST_COUNTERS: tuple[str, ...] = (*_MEMORY_COUNTERS, _QUEUE_COUNTER)
# ``counter_envelope`` validates the epoch format of every point it is given,
# including the ones it never selects. An unsupported host observation carries
# no epoch at all, so its point takes this sentinel: ``supported=False`` fails
# closed the moment such a point falls inside a selected envelope, so the
# sentinel can never stand in for an epoch the original evidence does not have.
_ABSENT_EPOCH_SENTINEL = "sha256:" + "0" * 64


@dataclass(frozen=True, slots=True)
class ResourceAggregates:
    """What one observation window proves about GPU headroom and host pressure.

    `gpu_first_utc`/`gpu_last_utc` and their host counterparts are the extremes
    of the **supported** samples, so a caller can test coverage against its own
    bracket without re-deriving which samples were usable.
    """

    gpu_free_min_bytes: int | None
    gpu_supported_samples: int
    gpu_unsupported_samples: int
    gpu_first_utc: datetime | None
    gpu_last_utc: datetime | None
    oom_total_delta: int | None
    oom_kill_total_delta: int | None
    oom_group_kill_total_delta: int | None
    preemption_delta: int | None
    host_supported_samples: int
    host_unsupported_samples: int
    host_first_utc: datetime | None
    host_last_utc: datetime | None
    problems: tuple[str, ...]
    gpu_nominal_interval_ms: int | None = None
    host_nominal_interval_ms: int | None = None
    # Only the v4 derivation has proved capture intervals, so these describe the
    # measured uncertainty of the samples it credited: the widest capture
    # interval, the largest proved delivery age, the wire sequences of the
    # counter edges it selected, and the clock domain the window belongs to.
    # They are reported evidence, never a correction applied to a measurement.
    capture_width_max_ns: int | None = None
    capture_age_max_ns: int | None = None
    counter_edge_sequences: tuple[int, int] | None = None
    window_domain: str | None = None

    @property
    def component_deltas(self) -> tuple[tuple[str, int | None], ...]:
        """The four zero-gate components in a fixed order; a `None` is unproven, never zero."""
        return (
            ("oom_total", self.oom_total_delta), ("oom_kill_total", self.oom_kill_total_delta),
            ("oom_group_kill_total", self.oom_group_kill_total_delta),
            (_QUEUE_COUNTER, self.preemption_delta),
        )


def _in_window(
    frames: tuple[SynchronizedTelemetryFrameV2, ...], lane: str,
    window_start_utc: datetime, window_end_utc: datetime,
) -> list[SynchronizedTelemetryFrameV2]:
    selected = [frame for frame in frames
                if frame.lane == lane and window_start_utc <= frame.clock.observed_at_utc <= window_end_utc]
    # Observation order, not file order: first/last and reset detection both depend on it.
    return sorted(selected, key=lambda frame: (frame.clock.observed_at_utc, frame.sequence))


def _nominal_interval(frames: list[SynchronizedTelemetryFrameV2]) -> int | None:
    # The slowest declared cadence in the window: a coverage bracket must tolerate it.
    return max((frame.quality.nominal_interval_ms for frame in frames), default=None)


def _counter_envelope(
    frames: tuple[SynchronizedTelemetryFrameV2, ...], start: datetime, end: datetime,
) -> tuple[list[SynchronizedTelemetryFrameV2], tuple[str, ...]]:
    """Nearest outer samples, not inner samples, bound a monotone counter delta.

    The delta is a conservative upper envelope for [start, end]. An increment
    in the small outer slivers cannot be timed more precisely and is NOT
    silently discarded. No endpoint, late boundary or missing cadence => unknown.
    All same-timestamp samples remain included so a reset cannot be hidden.
    """
    ordered = sorted((f for f in frames if f.lane == "host_slow"),
                     key=lambda f: (f.clock.observed_at_utc, f.sequence))
    left = [f for f in ordered if f.clock.observed_at_utc <= start]
    right = [f for f in ordered if f.clock.observed_at_utc >= end]
    problems: list[str] = []
    if not left:
        problems.append("host_counter_boundary_missing:start")
    if not right:
        problems.append("host_counter_boundary_missing:end")
    if problems:
        return _in_window(frames, "host_slow", start, end), tuple(problems)
    first, last = left[-1].clock.observed_at_utc, right[0].clock.observed_at_utc
    selected = [f for f in ordered if first <= f.clock.observed_at_utc <= last]
    cadence = {f.quality.nominal_interval_ms for f in selected}
    if len(cadence) != 1:
        problems.append("host_counter_cadence_changed")
    interval = timedelta(milliseconds=min(cadence))
    if start - first > interval or last - end > interval:
        problems.append("host_counter_boundary_gap")
    nominal_ns = min(cadence) * 1_000_000
    if (any(f.quality.status == "late" or f.quality.missed_deadlines for f in selected)
            or any(b.clock.scheduled_monotonic_ns - a.clock.scheduled_monotonic_ns != nominal_ns
                   for a, b in zip(selected, selected[1:]))):
        problems.append("host_counter_sample_gap")
    return selected, tuple(problems)


def derive_resource_aggregates(
    frames: tuple[SynchronizedTelemetryFrameV2, ...], *, window_start_utc: datetime,
    window_end_utc: datetime,
) -> ResourceAggregates:
    """GPU gauges use in-window samples; counters use the nearest outer envelope.

    A counter is unknown if either outer boundary is missing, cadence is not
    covered, any required sample is unsupported, or that component resets.
    GPU free is a sampled minimum, never a proof about every instant.
    """
    if type(frames) is not tuple or any(
        type(frame) is not SynchronizedTelemetryFrameV2 for frame in frames
    ):
        raise ValueError("resource aggregates require exact replayed v2 telemetry frames")
    if window_end_utc < window_start_utc:
        raise ValueError("resource aggregate window ends before it starts")
    problems: list[str] = []

    gpu_frames = _in_window(frames, "gpu_fast", window_start_utc, window_end_utc)
    gpu_supported = [frame for frame in gpu_frames if frame.gpu.status == "supported" and frame.gpu.values is not None]
    gpu_unsupported = len(gpu_frames) - len(gpu_supported)
    if gpu_unsupported:
        problems.append("gpu_unsupported_in_window")
    if len(gpu_supported) < 2:
        problems.append("gpu_samples_insufficient")
    gpu_free_min: int | None = None
    if gpu_unsupported == 0 and len(gpu_supported) >= 2:
        gpu_free_min = min(
            frame.gpu.values.framebuffer_free_bytes for frame in gpu_supported if frame.gpu.values is not None
        )

    host_frames, envelope_problems = _counter_envelope(frames, window_start_utc, window_end_utc)
    problems.extend(envelope_problems)
    # Both host observations feed the zero gate, so a frame is usable only when both are supported.
    host_supported = [
        frame for frame in host_frames
        if frame.host_cgroup.status == "supported" and frame.host_cgroup.values is not None
        and frame.queue_vllm.status == "supported" and frame.queue_vllm.values is not None
    ]
    host_unsupported = len(host_frames) - len(host_supported)
    if host_unsupported:
        problems.append("host_unsupported_in_window")
    if len(host_supported) < 2:
        problems.append("host_samples_insufficient")
    deltas: dict[str, int | None] = dict.fromkeys(_HOST_COUNTERS)
    if not envelope_problems and host_unsupported == 0 and len(host_supported) >= 2:
        series = {name: _series(host_supported, name) for name in _HOST_COUNTERS}
        for name, values in series.items():
            if any(later < earlier for earlier, later in zip(values, values[1:], strict=False)):
                problems.append("counter_reset:" + name)
                continue
            deltas[name] = values[-1] - values[0]

    return ResourceAggregates(
        gpu_free_min_bytes=gpu_free_min,
        gpu_supported_samples=len(gpu_supported), gpu_unsupported_samples=gpu_unsupported,
        gpu_first_utc=gpu_supported[0].clock.observed_at_utc if gpu_supported else None,
        gpu_last_utc=gpu_supported[-1].clock.observed_at_utc if gpu_supported else None,
        oom_total_delta=deltas["oom_total"], oom_kill_total_delta=deltas["oom_kill_total"],
        oom_group_kill_total_delta=deltas["oom_group_kill_total"], preemption_delta=deltas[_QUEUE_COUNTER],
        host_supported_samples=len(host_supported), host_unsupported_samples=host_unsupported,
        host_first_utc=host_supported[0].clock.observed_at_utc if host_supported else None,
        host_last_utc=host_supported[-1].clock.observed_at_utc if host_supported else None,
        problems=tuple(sorted(set(problems))),
        gpu_nominal_interval_ms=_nominal_interval(gpu_frames),
        host_nominal_interval_ms=_nominal_interval(host_frames),
    )


def _series(frames: list[SynchronizedTelemetryFrameV2], counter: str) -> list[int]:
    """One counter's supported values in observation order; the caller owns reset detection."""
    ordered = sorted(frames, key=lambda frame: (frame.clock.observed_at_utc, frame.sequence))
    values: list[int] = []
    for frame in ordered:
        if counter == _QUEUE_COUNTER:
            queue = frame.queue_vllm.values
            assert queue is not None
            values.append(queue.vllm_preemptions_total)
        else:
            host = frame.host_cgroup.values
            assert host is not None
            values.append(int(getattr(host.memory_events, counter)))
    return values


def _slug(message: str) -> str:
    """Name a rejected envelope by its own reason, without re-wording it."""
    return re.sub(r"[^a-z0-9]+", "-", message.lower()).strip("-")[:120] or "unnamed"


def _capture_bounds(frame: SynchronizedTelemetryFrameV3) -> CaptureBounds:
    """The one causal enclosure of this frame's source collection, computed once."""
    witness = frame.resident_exporter_provenance
    return pull_capture_bounds(
        PullTiming(
            request_nonce=witness.request_nonce, after_sequence=witness.after_sequence,
            sample_sequence=witness.wire_sequence,
            local_request_ns=witness.local_request_monotonic_ns,
            local_response_ns=witness.local_response_monotonic_ns,
            q_request_ns=witness.native_request_received_monotonic_ns,
            q_capture_start_ns=witness.wire_sampled_monotonic_ns,
            q_capture_end_ns=witness.native_capture_finished_monotonic_ns,
            q_reply_ns=witness.native_reply_started_monotonic_ns,
        ),
        expected_nonce=witness.request_nonce, expected_after_sequence=witness.after_sequence,
    )


def _counter_point(frame: SynchronizedTelemetryFrameV3, bounds: CaptureBounds, counter: str) -> CounterPoint:
    """One host counter reading placed on its own proved capture interval.

    A memory counter belongs to the parent cgroup epoch; the vLLM preemption
    counter belongs to the serving API process epoch. A frame that does not
    carry all three host observations cannot name either epoch, so it becomes an
    explicitly unsupported point rather than a guessed value.
    """
    host = frame.host_cgroup.values if frame.host_cgroup.status == "supported" else None
    queue = frame.queue_vllm.values if frame.queue_vllm.status == "supported" else None
    api = frame.api_process.values if frame.api_process.status == "supported" else None
    sequence = frame.resident_exporter_provenance.wire_sequence
    if host is None or queue is None or api is None:
        return CounterPoint(
            sequence=sequence, epoch_sha256=_ABSENT_EPOCH_SENTINEL,
            earliest_start_ns=bounds.earliest_start_ns, latest_end_ns=bounds.latest_end_ns,
            value=None, supported=False,
        )
    if counter == _QUEUE_COUNTER:
        value, epoch = queue.vllm_preemptions_total, api.process_epoch_sha256
    else:
        value, epoch = int(getattr(host.memory_events, counter)), host.parent_cgroup_epoch_sha256
    return CounterPoint(
        sequence=sequence, epoch_sha256=epoch, earliest_start_ns=bounds.earliest_start_ns,
        latest_end_ns=bounds.latest_end_ns, value=value, supported=True,
    )


def _host_usable(frame: SynchronizedTelemetryFrameV3) -> bool:
    """Every host counter and both host epochs come from the same sample."""
    return (frame.host_cgroup.status == "supported" and frame.queue_vllm.status == "supported"
            and frame.api_process.status == "supported")


def derive_resource_aggregates_v4(
    frames: tuple[SynchronizedTelemetryFrameV3, ...], *, plan: SynchronizedSamplingPlanV1,
    window_started_monotonic_ns: int, window_finished_monotonic_ns: int,
) -> ResourceAggregates:
    """Score one Mac monotonic window against proved per-frame capture intervals.

    The window, the frozen plan and every frame must live in the same observer
    clock domain and the same run; a caller that mixes them has a bug, not an
    unknown, so those are raised rather than named. Everything the evidence
    itself leaves open - an unusable request witness, an unsupported sample, a
    counter edge that is not proved outside the window - becomes a named problem
    and a ``None`` measure. No midpoint, no edge sample is dropped, and no
    counter delta is inferred from samples taken inside the window alone.
    """
    if type(frames) is not tuple or any(
        type(frame) is not SynchronizedTelemetryFrameV3 for frame in frames
    ):
        raise ValueError("v4 resource aggregates require exact replayed v3 telemetry frames")
    if not isinstance(plan, SynchronizedSamplingPlanV1):
        raise ValueError("v4 resource aggregates require the frozen sampling plan")
    for label, instant in (("start", window_started_monotonic_ns), ("finish", window_finished_monotonic_ns)):
        if type(instant) is not int or instant < 1:
            raise ValueError(f"resource aggregate window {label} must be a positive monotonic instant")
    if window_finished_monotonic_ns <= window_started_monotonic_ns:
        raise ValueError("resource aggregate window ends before it starts")
    for frame in frames:
        if frame.run_id != plan.run_id:
            raise ValueError("resource aggregate frame belongs to another run")
        if frame.clock.clock_domain_identity_sha256 != plan.observer_clock_domain_identity_sha256:
            raise ValueError("resource aggregate frame belongs to another observer clock domain")

    problems: list[str] = []
    prepared: list[tuple[SynchronizedTelemetryFrameV3, CaptureBounds]] = []
    # A frame whose own request witness does not prove a capture interval is
    # excluded from every credit; its lane then proves nothing in this window.
    unusable_lanes: set[str] = set()
    for frame in frames:
        try:
            bounds = _capture_bounds(frame)
        except ValueError:
            problems.append(
                f"capture_bounds_invalid:{frame.lane}:{frame.resident_exporter_provenance.wire_sequence}"
            )
            unusable_lanes.add(frame.lane)
            continue
        prepared.append((frame, bounds))

    def lane_samples(lane: str) -> list[tuple[SynchronizedTelemetryFrameV3, CaptureBounds]]:
        # Per-lane wire sequence is the source's own order; the global frame
        # sequence interleaves the two lanes and must never order a counter.
        return sorted(
            (item for item in prepared if item[0].lane == lane),
            key=lambda item: item[0].resident_exporter_provenance.wire_sequence,
        )

    def overlapping(
        samples: list[tuple[SynchronizedTelemetryFrameV3, CaptureBounds]],
    ) -> list[tuple[SynchronizedTelemetryFrameV3, CaptureBounds]]:
        return [item for item in samples if possibly_overlaps(
            item[1], start_ns=window_started_monotonic_ns, end_ns=window_finished_monotonic_ns,
        )]

    credited: list[CaptureBounds] = []
    gpu_window = overlapping(lane_samples("gpu_fast"))
    credited.extend(bounds for _, bounds in gpu_window)
    gpu_supported = [frame for frame, _ in gpu_window
                     if frame.gpu.status == "supported" and frame.gpu.values is not None]
    gpu_unsupported = len(gpu_window) - len(gpu_supported)
    if gpu_unsupported:
        problems.append("gpu_unsupported_in_window")
    if len(gpu_supported) < 2:
        problems.append("gpu_samples_insufficient")
    gpu_free_min: int | None = None
    if "gpu_fast" not in unusable_lanes and gpu_unsupported == 0 and len(gpu_supported) >= 2:
        gpu_free_min = min(
            frame.gpu.values.framebuffer_free_bytes for frame in gpu_supported if frame.gpu.values is not None
        )

    host_samples = lane_samples("host_slow")
    host_window = overlapping(host_samples)
    credited.extend(bounds for _, bounds in host_window)
    host_supported = [frame for frame, _ in host_window if _host_usable(frame)]
    host_unsupported = len(host_window) - len(host_supported)

    deltas: dict[str, int | None] = dict.fromkeys(_HOST_COUNTERS)
    edges: set[tuple[int, int]] = set()
    proved_counters = 0
    if "host_slow" not in unusable_lanes:
        for counter in _HOST_COUNTERS:
            points = [_counter_point(frame, bounds, counter) for frame, bounds in host_samples]
            try:
                envelope = counter_envelope(
                    points, window_start_ns=window_started_monotonic_ns,
                    window_end_ns=window_finished_monotonic_ns,
                )
            except ValueError as error:
                problems.append(f"host_counter:{counter}:{_slug(str(error))}")
                continue
            deltas[counter] = envelope.outer_delta
            edges.add((envelope.before_sequence, envelope.after_sequence))
            proved_counters += 1
            credited.extend(
                bounds for frame, bounds in host_samples
                if envelope.before_sequence <= frame.resident_exporter_provenance.wire_sequence
                <= envelope.after_sequence
            )

    return ResourceAggregates(
        gpu_free_min_bytes=gpu_free_min,
        gpu_supported_samples=len(gpu_supported), gpu_unsupported_samples=gpu_unsupported,
        gpu_first_utc=gpu_supported[0].clock.observed_at_utc if gpu_supported else None,
        gpu_last_utc=gpu_supported[-1].clock.observed_at_utc if gpu_supported else None,
        oom_total_delta=deltas["oom_total"], oom_kill_total_delta=deltas["oom_kill_total"],
        oom_group_kill_total_delta=deltas["oom_group_kill_total"], preemption_delta=deltas[_QUEUE_COUNTER],
        host_supported_samples=len(host_supported), host_unsupported_samples=host_unsupported,
        host_first_utc=host_supported[0].clock.observed_at_utc if host_supported else None,
        host_last_utc=host_supported[-1].clock.observed_at_utc if host_supported else None,
        problems=tuple(sorted(set(problems))),
        gpu_nominal_interval_ms=plan.gpu_nominal_interval_ms,
        host_nominal_interval_ms=plan.host_nominal_interval_ms,
        capture_width_max_ns=max((bounds.width_ns for bounds in credited), default=None),
        capture_age_max_ns=max((bounds.receive_age_upper_ns for bounds in credited), default=None),
        counter_edge_sequences=(
            next(iter(edges)) if proved_counters == len(_HOST_COUNTERS) and len(edges) == 1 else None
        ),
        window_domain=plan.observer_clock_domain_identity_sha256,
    )


__all__ = ["ResourceAggregates", "derive_resource_aggregates", "derive_resource_aggregates_v4"]
