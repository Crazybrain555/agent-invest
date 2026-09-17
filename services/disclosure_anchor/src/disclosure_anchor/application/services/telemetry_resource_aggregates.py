"""Pure window aggregates over already-replayed synchronized telemetry frames.

Nothing here reads a file, a clock or a device: the caller supplies the exact
frames a sealed observer replay produced and the UTC bracket to score them in.
Every component counter is kept separately and a counter that could not be
trusted - unsupported sample, reset, or too few samples - becomes ``None`` with
a named problem. A missing measure is never clamped to zero, because a zero
OOM/preemption delta is the very claim a resource gate is asked to prove.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from disclosure_anchor.application.contracts.synchronized_telemetry import SynchronizedTelemetryFrameV2

# The host counters the M6 resource gate reads, by their exact frame field names.
_MEMORY_COUNTERS: tuple[str, ...] = ("oom_total", "oom_kill_total", "oom_group_kill_total")
_QUEUE_COUNTER = "vllm_preemptions_total"
_HOST_COUNTERS: tuple[str, ...] = (*_MEMORY_COUNTERS, _QUEUE_COUNTER)


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


__all__ = ["ResourceAggregates", "derive_resource_aggregates"]
