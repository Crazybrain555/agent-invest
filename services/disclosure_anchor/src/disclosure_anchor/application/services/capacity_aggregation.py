"""Pure replay aggregation for passive capacity observations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, cast

from disclosure_anchor.application.contracts.capacity import (
    ApiIntervalMetrics,
    ApiSampleValues,
    CapacityCoverageSet,
    CapacityObservationInterval,
    CapacityRawSample,
    CoverageEvidence,
    GpuIntervalMetrics,
    GpuSampleValues,
    HostIntervalMetrics,
    HostSampleValues,
    MetricAggregate,
    RawSampleValues,
    SafetyViolation,
    VllmIntervalMetrics,
    VllmSampleValues,
    chained_record_payload,
)


SOURCE_CADENCE_SECONDS = {
    "api": 1.0,
    "gpu": 1.0,
    "host": 5.0,
    "vllm": 1.0,
}
SOURCE_REQUIRED_COVERAGE = {
    "api": 0.99,
    "gpu": 0.99,
    "host": 1.0,
    "vllm": 0.99,
}
SOURCE_MAX_GAP_SECONDS = {
    "api": 5.0,
    "gpu": 5.0,
    "host": 15.0,
    "vllm": 5.0,
}


@dataclass(frozen=True, slots=True)
class _WeightedValue:
    value: float
    seconds: float


@dataclass(frozen=True, slots=True)
class _SourceWindow:
    source: str
    start_seconds: float
    end_seconds: float
    samples: tuple[CapacityRawSample, ...]
    segments: tuple[tuple[float, RawSampleValues], ...]
    transitions: tuple[CapacityRawSample, ...]
    coverage: CoverageEvidence


def aggregate_capacity_interval(
    samples: Sequence[CapacityRawSample],
    *,
    run_id: str,
    interval_index: int,
    start_seconds: float,
    end_seconds: float,
    observed_at_utc: datetime,
    runtime_bundle_identity_sha256: str,
    observer_source_sha256: str,
    previous_record_sha256: str | None,
) -> CapacityObservationInterval:
    """Aggregate one interval using bounded sample-and-hold semantics.

    A valid gauge sample is held until the next observation, the interval end,
    or the source maximum gap, whichever is first. Missing samples are never
    imputed. A sample immediately before the left boundary may cover into the
    interval; a sample immediately after the right boundary only closes the
    preceding hold and contributes no value of its own.
    """

    if end_seconds <= start_seconds:
        raise ValueError("capacity interval must have positive duration")
    windows = {
        source: _source_window(
            samples,
            source=source,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        for source in ("api", "gpu", "host", "vllm")
    }
    violations: set[SafetyViolation] = set()

    api = _api_metrics(windows["api"], violations=violations)
    gpu = _gpu_metrics(windows["gpu"])
    host = _host_metrics(windows["host"], violations=violations)
    vllm = _vllm_metrics(windows["vllm"], violations=violations)
    coverage = CapacityCoverageSet(
        api=windows["api"].coverage,
        gpu=windows["gpu"].coverage,
        host=windows["host"].coverage,
        vllm=windows["vllm"].coverage,
    )
    coverage_complete = all(
        item.status == "complete"
        for item in (coverage.api, coverage.gpu, coverage.host, coverage.vllm)
    )
    status = "unsafe" if violations else "complete" if coverage_complete else "incomplete"
    payload: dict[str, Any] = {
        "contract_version": "capacity_observation.interval.v1",
        "run_id": run_id,
        "interval_index": interval_index,
        "previous_record_sha256": previous_record_sha256,
        "record_sha256": "sha256:" + "0" * 64,
        "runtime_bundle_identity_sha256": runtime_bundle_identity_sha256,
        "observer_source_sha256": observer_source_sha256,
        "observed_at_utc": observed_at_utc.isoformat(),
        "monotonic_start_seconds": round(start_seconds, 6),
        "monotonic_end_seconds": round(end_seconds, 6),
        "wall_seconds": round(end_seconds - start_seconds, 6),
        "status": status,
        "coverage": coverage.model_dump(mode="json"),
        "api": api.model_dump(mode="json"),
        "gpu": gpu.model_dump(mode="json"),
        "host": host.model_dump(mode="json"),
        "vllm": vllm.model_dump(mode="json"),
        "safety_violations": sorted(violations),
    }
    draft = CapacityObservationInterval.model_validate(payload)
    normalized = draft.model_dump(mode="json")
    normalized["record_sha256"] = None
    return CapacityObservationInterval.model_validate(
        chained_record_payload(normalized)
    )


def _source_window(
    samples: Sequence[CapacityRawSample],
    *,
    source: str,
    start_seconds: float,
    end_seconds: float,
) -> _SourceWindow:
    source_samples = tuple(
        sorted(
            (sample for sample in samples if sample.source == source),
            key=lambda sample: (sample.monotonic_offset_seconds, sample.sequence),
        )
    )
    before = [
        sample
        for sample in source_samples
        if sample.monotonic_offset_seconds < start_seconds
    ]
    middle = [
        sample
        for sample in source_samples
        if start_seconds <= sample.monotonic_offset_seconds < end_seconds
    ]
    after = [
        sample
        for sample in source_samples
        if sample.monotonic_offset_seconds >= end_seconds
    ]
    relevant: list[CapacityRawSample] = []
    if before:
        relevant.append(before[-1])
    relevant.extend(middle)
    if after:
        relevant.append(after[0])

    baseline = [
        sample
        for sample in source_samples
        if sample.monotonic_offset_seconds <= start_seconds
    ]
    transitions: list[CapacityRawSample] = []
    if baseline:
        transitions.append(baseline[-1])
    transitions.extend(
        sample
        for sample in source_samples
        if start_seconds < sample.monotonic_offset_seconds <= end_seconds
    )

    max_gap = SOURCE_MAX_GAP_SECONDS[source]
    segments: list[tuple[float, RawSampleValues]] = []
    for index, sample in enumerate(relevant):
        sample_offset = sample.monotonic_offset_seconds
        if sample_offset >= end_seconds:
            break
        next_offset = (
            relevant[index + 1].monotonic_offset_seconds
            if index + 1 < len(relevant)
            else end_seconds
        )
        segment_start = max(start_seconds, sample_offset)
        segment_end = min(end_seconds, next_offset, sample_offset + max_gap)
        if (
            sample.status == "available"
            and sample.values is not None
            and segment_end > segment_start
        ):
            segments.append((segment_end - segment_start, sample.values))

    duration = end_seconds - start_seconds
    covered = min(duration, sum(seconds for seconds, _ in segments))
    ratio = covered / duration
    valid_middle = [sample for sample in middle if sample.status == "available"]
    expected = max(1, math.ceil(duration / SOURCE_CADENCE_SECONDS[source]))
    observed_gap = _maximum_valid_gap(
        relevant,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        max_gap_seconds=max_gap,
    )
    required = SOURCE_REQUIRED_COVERAGE[source]
    reason: str | None = None
    right_boundary_unavailable = any(
        abs(sample.monotonic_offset_seconds - end_seconds) <= 1e-9
        and sample.status == "unavailable"
        for sample in transitions
    )
    if right_boundary_unavailable:
        reason = "boundary_sample_unavailable"
    elif not segments:
        reason = "no_samples"
    elif observed_gap > max_gap + 1e-9:
        reason = "max_gap_exceeded"
    elif ratio + 1e-9 < required:
        reason = "coverage_below_threshold"
    coverage = CoverageEvidence(
        status="complete" if reason is None else "incomplete",
        valid_samples=len(valid_middle),
        expected_samples=expected,
        covered_seconds=round(covered, 6),
        coverage_ratio=round(ratio, 9),
        required_ratio=required,
        max_gap_seconds=round(observed_gap, 6),
        allowed_max_gap_seconds=max_gap,
        reason_code=cast(Any, reason),
    )
    return _SourceWindow(
        source=source,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        samples=tuple(relevant),
        segments=tuple(segments),
        transitions=tuple(transitions),
        coverage=coverage,
    )


def _transition_values(window: _SourceWindow) -> tuple[RawSampleValues, ...]:
    """Return the left baseline plus available points in ``(start, end]``.

    Time-weighted gauges intentionally remain half-open segments. State and
    true-counter transitions use this separate sequence so a change exactly on
    an interval boundary belongs to the interval ending at that boundary once.
    """

    return tuple(
        cast(RawSampleValues, sample.values)
        for sample in window.transitions
        if sample.status == "available" and sample.values is not None
    )


def _maximum_valid_gap(
    samples: Sequence[CapacityRawSample],
    *,
    start_seconds: float,
    end_seconds: float,
    max_gap_seconds: float,
) -> float:
    available = [
        sample.monotonic_offset_seconds
        for sample in samples
        if sample.status == "available"
    ]
    if not available:
        return end_seconds - start_seconds
    points: list[float] = []
    preceding = [offset for offset in available if offset <= start_seconds]
    if preceding and start_seconds - preceding[-1] <= max_gap_seconds:
        points.append(start_seconds)
    points.extend(
        offset for offset in available if start_seconds < offset < end_seconds
    )
    following = [offset for offset in available if offset >= end_seconds]
    last_before = [offset for offset in available if offset < end_seconds]
    if (
        following
        and last_before
        and following[0] - last_before[-1] <= max_gap_seconds
    ) or (
        last_before and end_seconds - last_before[-1] <= max_gap_seconds
    ):
        points.append(end_seconds)
    if not points:
        return end_seconds - start_seconds
    if points[0] > start_seconds:
        points.insert(0, start_seconds)
    if points[-1] < end_seconds:
        points.append(end_seconds)
    return max(right - left for left, right in zip(points, points[1:]))


def _metric(
    window: _SourceWindow,
    selector: Callable[[RawSampleValues], float | int | None],
) -> MetricAggregate:
    weighted: list[_WeightedValue] = []
    for seconds, values in window.segments:
        selected = selector(values)
        if selected is not None:
            weighted.append(_WeightedValue(float(selected), seconds))
    if not weighted:
        return MetricAggregate(
            current=None,
            minimum=None,
            maximum=None,
            time_weighted_mean=None,
            time_weighted_p50=None,
            time_weighted_p95=None,
            observed_seconds=0.0,
        )
    observed_seconds = sum(item.seconds for item in weighted)
    metric_values = [item.value for item in weighted]
    return MetricAggregate(
        current=round(weighted[-1].value, 6),
        minimum=round(min(metric_values), 6),
        maximum=round(max(metric_values), 6),
        time_weighted_mean=round(
            sum(item.value * item.seconds for item in weighted) / observed_seconds,
            6,
        ),
        time_weighted_p50=round(_weighted_quantile(weighted, 0.50), 6),
        time_weighted_p95=round(_weighted_quantile(weighted, 0.95), 6),
        observed_seconds=round(observed_seconds, 6),
    )


def _weighted_quantile(values: Sequence[_WeightedValue], quantile: float) -> float:
    ordered = sorted(values, key=lambda item: item.value)
    target = sum(item.seconds for item in ordered) * quantile
    cumulative = 0.0
    for item in ordered:
        cumulative += item.seconds
        if cumulative + 1e-12 >= target:
            return item.value
    return ordered[-1].value


def _seconds_where(
    window: _SourceWindow,
    predicate: Callable[[RawSampleValues], bool],
) -> float:
    return round(
        sum(seconds for seconds, values in window.segments if predicate(values)),
        6,
    )


def _api_metrics(
    window: _SourceWindow,
    *,
    violations: set[SafetyViolation],
) -> ApiIntervalMetrics:
    def api(values: RawSampleValues) -> ApiSampleValues:
        return cast(ApiSampleValues, values)

    slots = {api(values).task_slots for _, values in window.segments}
    transition_slots = {
        api(values).task_slots for values in _transition_values(window)
    }
    if len(transition_slots) > 1:
        violations.add("api_identity_drift")
    return ApiIntervalMetrics(
        queued_tasks=_metric(window, lambda values: api(values).queued_tasks),
        processing_tasks=_metric(window, lambda values: api(values).processing_tasks),
        completed_tasks_gauge=_metric(
            window, lambda values: api(values).completed_tasks_gauge
        ),
        failed_tasks_gauge=_metric(
            window, lambda values: api(values).failed_tasks_gauge
        ),
        processing_task_seconds=_seconds_where(
            window, lambda values: api(values).processing_tasks > 0
        ),
        queued_task_seconds=_seconds_where(
            window, lambda values: api(values).queued_tasks > 0
        ),
        idle_task_seconds=_seconds_where(
            window,
            lambda values: api(values).queued_tasks == 0
            and api(values).processing_tasks == 0,
        ),
        task_slots=next(iter(slots)) if len(slots) == 1 else None,
    )


def _vllm_metrics(
    window: _SourceWindow,
    *,
    violations: set[SafetyViolation],
) -> VllmIntervalMetrics:
    def vllm(values: RawSampleValues) -> VllmSampleValues:
        return cast(VllmSampleValues, values)

    counter_values = [
        vllm(values).preemptions_total
        for values in _transition_values(window)
        if vllm(values).preemptions_total is not None
    ]
    delta, valid = _counter_delta(counter_values)
    if not valid:
        violations.add("vllm_counter_reset")
    return VllmIntervalMetrics(
        requests_running=_metric(
            window, lambda values: vllm(values).requests_running
        ),
        requests_waiting=_metric(
            window, lambda values: vllm(values).requests_waiting
        ),
        kv_cache_usage_ratio=_metric(
            window, lambda values: vllm(values).kv_cache_usage_ratio
        ),
        preemptions_delta=delta,
        counter_epoch_valid=valid,
    )


def _gpu_metrics(window: _SourceWindow) -> GpuIntervalMetrics:
    def gpu(values: RawSampleValues) -> GpuSampleValues:
        return cast(GpuSampleValues, values)

    return GpuIntervalMetrics(
        gpu_utilization_pct=_metric(
            window, lambda values: gpu(values).gpu_utilization_pct
        ),
        framebuffer_used_bytes=_metric(
            window, lambda values: gpu(values).framebuffer_used_bytes
        ),
        framebuffer_free_bytes=_metric(
            window, lambda values: gpu(values).framebuffer_free_bytes
        ),
        power_usage_watts=_metric(
            window, lambda values: gpu(values).power_usage_watts
        ),
        temperature_celsius=_metric(
            window, lambda values: gpu(values).temperature_celsius
        ),
    )


def _host_metrics(
    window: _SourceWindow,
    *,
    violations: set[SafetyViolation],
) -> HostIntervalMetrics:
    def host(values: RawSampleValues) -> HostSampleValues:
        return cast(HostSampleValues, values)

    transition_values = _transition_values(window)
    epochs = {
        host(values).container_epoch_sha256 for values in transition_values
    }
    epoch_changed = len(epochs) > 1
    if epoch_changed:
        violations.add("host_container_epoch_changed")

    counters = {
        "restart": [host(values).restart_count_total for values in transition_values],
        "oom": [host(values).cgroup_oom_total for values in transition_values],
        "oom_kill": [
            host(values).cgroup_oom_kill_total for values in transition_values
        ],
        "high": [host(values).cgroup_high_total for values in transition_values],
    }
    deltas: dict[str, int | None] = {}
    valid = not epoch_changed
    for name, counter_values in counters.items():
        delta, counter_valid = _counter_delta(counter_values)
        deltas[name] = delta if valid and counter_valid else None
        valid = valid and counter_valid
    if not valid and not epoch_changed:
        violations.add("host_counter_reset")

    for sample_values in transition_values:
        sample = host(sample_values)
        if "memory_reserve_crossed" in sample.safety_violation_codes:
            violations.add("host_memory_reserve_crossed")
        if "container_state_unsafe" in sample.safety_violation_codes:
            violations.add("host_container_state_unsafe")
        if "cgroup_oom_observed" in sample.safety_violation_codes:
            violations.add("host_cgroup_oom_observed")
    return HostIntervalMetrics(
        docker_vm_memory_available_bytes=_metric(
            window, lambda values: host(values).docker_vm_memory_available_bytes
        ),
        api_pid1_rss_bytes=_metric(
            window, lambda values: host(values).api_pid1_rss_bytes
        ),
        api_pid1_rss_hwm_bytes=_metric(
            window, lambda values: host(values).api_pid1_rss_hwm_bytes
        ),
        memory_reserve_crossed_seconds=_seconds_where(
            window,
            lambda values: host(values).docker_vm_memory_available_bytes
            < host(values).docker_memory_reserve_bytes,
        ),
        restart_delta=deltas["restart"],
        cgroup_oom_delta=deltas["oom"],
        cgroup_oom_kill_delta=deltas["oom_kill"],
        cgroup_high_delta=deltas["high"],
        counter_epoch_valid=valid,
        container_epoch_changed=epoch_changed,
    )


def _counter_delta(values: Sequence[int | None]) -> tuple[int | None, bool]:
    present = [value for value in values if value is not None]
    if not present:
        return None, True
    if any(right < left for left, right in zip(present, present[1:])):
        return None, False
    return present[-1] - present[0], True


__all__ = [
    "SOURCE_CADENCE_SECONDS",
    "SOURCE_MAX_GAP_SECONDS",
    "SOURCE_REQUIRED_COVERAGE",
    "aggregate_capacity_interval",
]
