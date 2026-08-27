"""Pure replay tests for capacity coverage, gauges and true counters."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast
import unittest

from disclosure_anchor.application.contracts.capacity import (
    ApiSampleValues,
    CapacityRawSample,
    GpuSampleValues,
    HostSampleValues,
    RawSampleValues,
    VllmSampleValues,
    chained_record_payload,
)
from disclosure_anchor.application.services.capacity_aggregation import (
    aggregate_capacity_interval,
)


RUN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RUNTIME_ID = "sha256:" + "1" * 64
SOURCE_ID = "sha256:" + "2" * 64
NOW = datetime(2026, 8, 27, tzinfo=UTC)
HostSafetyCode = Literal[
    "cgroup_oom_observed",
    "container_state_unsafe",
    "memory_reserve_crossed",
]


def _api(*, completed: int = 10, failed: int = 2) -> ApiSampleValues:
    return ApiSampleValues(
        queued_tasks=0,
        processing_tasks=1,
        completed_tasks_gauge=completed,
        failed_tasks_gauge=failed,
        task_slots=1,
        processing_window_size=16,
        task_retention_seconds=600,
        task_cleanup_interval_seconds=30,
        protocol_version=2,
    )


def _vllm(*, preemptions: int = 0) -> VllmSampleValues:
    return VllmSampleValues(
        requests_running=7,
        requests_waiting=0,
        preemptions_total=preemptions,
        kv_cache_usage_ratio=0.1,
    )


def _gpu() -> GpuSampleValues:
    return GpuSampleValues(
        exporter_family="nvidia_smi",
        device_count=1,
        device_identity_sha256="sha256:" + "3" * 64,
        gpu_utilization_pct=90,
        framebuffer_used_bytes=10_000,
        framebuffer_free_bytes=6_000,
        framebuffer_total_bytes=16_000,
        power_usage_watts=250,
        temperature_celsius=60,
    )


def _host(
    *,
    epoch: str = "4",
    available: int = 8_000,
    safety: tuple[HostSafetyCode, ...] = (),
) -> HostSampleValues:
    return HostSampleValues(
        collector_sha256="sha256:" + "5" * 64,
        windows_node_identity_sha256="sha256:" + "6" * 64,
        container_epoch_sha256="sha256:" + epoch * 64,
        container_count=3,
        restart_count_total=0,
        oom_killed_count=0,
        unsafe_container_count=0,
        cgroup_oom_total=0,
        cgroup_oom_kill_total=0,
        cgroup_high_total=0,
        docker_vm_memory_total_bytes=16_000,
        docker_vm_memory_available_bytes=available,
        docker_memory_reserve_bytes=4_000,
        api_pid1_rss_bytes=2_000,
        api_pid1_rss_hwm_bytes=3_000,
        safety_violation_codes=safety,
    )


def _samples(points: list[tuple[float, str, RawSampleValues | None]]) -> list[CapacityRawSample]:
    samples: list[CapacityRawSample] = []
    previous: str | None = None
    for sequence, (offset, source, values) in enumerate(points):
        payload: dict[str, Any] = {
            "contract_version": "capacity_observation.raw_sample.v1",
            "run_id": RUN_ID,
            "sequence": sequence,
            "previous_record_sha256": previous,
            "record_sha256": None,
            "runtime_bundle_identity_sha256": RUNTIME_ID,
            "observer_source_sha256": SOURCE_ID,
            "observed_at_utc": NOW.isoformat(),
            "monotonic_offset_seconds": offset,
            "sample_duration_seconds": 0.01,
            "source": source,
            "status": "available" if values is not None else "unavailable",
            "values": values.model_dump(mode="json") if values is not None else None,
            "reason_code": None if values is not None else "endpoint_unreachable",
        }
        sample = CapacityRawSample.model_validate(chained_record_payload(payload))
        samples.append(sample)
        previous = sample.record_sha256
    return samples


class CapacityAggregationTests(unittest.TestCase):
    def test_terminal_registry_populations_are_gauges_not_counters(self) -> None:
        points: list[tuple[float, str, RawSampleValues | None]] = []
        for offset, completed, failed in ((0.0, 10, 2), (1.0, 8, 1), (2.0, 2, 0)):
            points.extend(
                [
                    (offset, "api", _api(completed=completed, failed=failed)),
                    (offset, "vllm", _vllm()),
                    (offset, "gpu", _gpu()),
                ]
            )
        points.append((0.0, "host", _host()))
        interval = aggregate_capacity_interval(
            _samples(points),
            run_id=RUN_ID,
            interval_index=0,
            start_seconds=0,
            end_seconds=3,
            observed_at_utc=NOW,
            runtime_bundle_identity_sha256=RUNTIME_ID,
            observer_source_sha256=SOURCE_ID,
            previous_record_sha256=None,
        )

        self.assertEqual(interval.status, "complete")
        self.assertEqual(interval.api.completed_tasks_gauge.current, 2)
        self.assertEqual(interval.api.failed_tasks_gauge.current, 0)
        self.assertNotIn("vllm_counter_reset", interval.safety_violations)

    def test_missing_source_is_incomplete_and_never_imputed(self) -> None:
        points: list[tuple[float, str, RawSampleValues | None]] = [
            (0.0, "api", _api()),
            (0.0, "vllm", _vllm()),
            (0.0, "gpu", _gpu()),
            (0.0, "host", _host()),
            (1.0, "gpu", None),
            (2.0, "gpu", _gpu()),
        ]
        interval = aggregate_capacity_interval(
            _samples(points),
            run_id=RUN_ID,
            interval_index=0,
            start_seconds=0,
            end_seconds=3,
            observed_at_utc=NOW,
            runtime_bundle_identity_sha256=RUNTIME_ID,
            observer_source_sha256=SOURCE_ID,
            previous_record_sha256=None,
        )

        self.assertEqual(interval.status, "incomplete")
        self.assertEqual(interval.coverage.gpu.status, "incomplete")
        self.assertLess(interval.coverage.gpu.coverage_ratio, 0.99)

    def test_true_counter_reset_and_host_epoch_change_are_unsafe(self) -> None:
        points: list[tuple[float, str, RawSampleValues | None]] = [
            (0.0, "api", _api()),
            (0.0, "vllm", _vllm(preemptions=4)),
            (0.0, "gpu", _gpu()),
            (0.0, "host", _host(epoch="4")),
            (1.0, "api", _api()),
            (1.0, "vllm", _vllm(preemptions=1)),
            (1.0, "gpu", _gpu()),
            (1.0, "host", _host(epoch="7")),
        ]
        interval = aggregate_capacity_interval(
            _samples(points),
            run_id=RUN_ID,
            interval_index=0,
            start_seconds=0,
            end_seconds=2,
            observed_at_utc=NOW,
            runtime_bundle_identity_sha256=RUNTIME_ID,
            observer_source_sha256=SOURCE_ID,
            previous_record_sha256=None,
        )

        self.assertEqual(interval.status, "unsafe")
        self.assertIn("vllm_counter_reset", interval.safety_violations)
        self.assertIn("host_container_epoch_changed", interval.safety_violations)
        self.assertIsNone(interval.vllm.preemptions_delta)

    def test_exact_boundary_transitions_belong_to_preceding_interval_once(self) -> None:
        points: list[tuple[float, str, RawSampleValues | None]] = [
            (0.0, "api", _api()),
            (0.0, "vllm", _vllm(preemptions=5)),
            (0.0, "gpu", _gpu()),
            (0.0, "host", _host(epoch="4")),
            (60.0, "api", _api()),
            (60.0, "vllm", _vllm(preemptions=0)),
            (60.0, "gpu", _gpu()),
            (60.0, "host", _host(epoch="7")),
            (120.0, "api", _api()),
            (120.0, "vllm", _vllm(preemptions=2)),
            (120.0, "gpu", _gpu()),
            (120.0, "host", _host(epoch="7")),
        ]
        samples = _samples(points)
        first = aggregate_capacity_interval(
            samples,
            run_id=RUN_ID,
            interval_index=0,
            start_seconds=0,
            end_seconds=60,
            observed_at_utc=NOW,
            runtime_bundle_identity_sha256=RUNTIME_ID,
            observer_source_sha256=SOURCE_ID,
            previous_record_sha256=None,
        )
        second = aggregate_capacity_interval(
            samples,
            run_id=RUN_ID,
            interval_index=1,
            start_seconds=60,
            end_seconds=120,
            observed_at_utc=NOW,
            runtime_bundle_identity_sha256=RUNTIME_ID,
            observer_source_sha256=SOURCE_ID,
            previous_record_sha256=first.record_sha256,
        )

        self.assertIn("host_container_epoch_changed", first.safety_violations)
        self.assertIn("vllm_counter_reset", first.safety_violations)
        self.assertNotIn("host_container_epoch_changed", second.safety_violations)
        self.assertNotIn("vllm_counter_reset", second.safety_violations)
        self.assertEqual(second.vllm.preemptions_delta, 2)

    def test_final_boundary_unsafe_or_unavailable_sample_is_not_dropped(self) -> None:
        points: list[tuple[float, str, RawSampleValues | None]] = [
            (0.0, "api", _api()),
            (0.0, "vllm", _vllm()),
            (0.0, "gpu", _gpu()),
            (0.0, "host", _host()),
            (5.0, "gpu", None),
            (
                5.0,
                "host",
                _host(
                    available=3_000,
                    safety=("memory_reserve_crossed",),
                ),
            ),
        ]
        interval = aggregate_capacity_interval(
            _samples(points),
            run_id=RUN_ID,
            interval_index=0,
            start_seconds=0,
            end_seconds=5,
            observed_at_utc=NOW,
            runtime_bundle_identity_sha256=RUNTIME_ID,
            observer_source_sha256=SOURCE_ID,
            previous_record_sha256=None,
        )

        self.assertEqual(
            interval.coverage.gpu.reason_code,
            "boundary_sample_unavailable",
        )
        self.assertIn("host_memory_reserve_crossed", interval.safety_violations)

    def test_weighted_quantiles_and_max_gap_have_explicit_boundaries(self) -> None:
        base: list[tuple[float, str, RawSampleValues | None]] = [
            (0.0, "api", _api()),
            (0.0, "vllm", _vllm()),
            (0.0, "host", _host()),
        ]
        gpu_values: list[tuple[float, str, RawSampleValues | None]] = []
        for offset, utilization in ((0.0, 10), (1.0, 20), (2.0, 100)):
            value = cast(
                GpuSampleValues,
                _gpu().model_copy(update={"gpu_utilization_pct": utilization}),
            )
            gpu_values.append((offset, "gpu", value))
        interval = aggregate_capacity_interval(
            _samples([*base, *gpu_values]),
            run_id=RUN_ID,
            interval_index=0,
            start_seconds=0,
            end_seconds=4,
            observed_at_utc=NOW,
            runtime_bundle_identity_sha256=RUNTIME_ID,
            observer_source_sha256=SOURCE_ID,
            previous_record_sha256=None,
        )
        metric = interval.gpu.gpu_utilization_pct
        self.assertEqual(metric.time_weighted_mean, 57.5)
        self.assertEqual(metric.time_weighted_p50, 20)
        self.assertEqual(metric.time_weighted_p95, 100)

        gap = aggregate_capacity_interval(
            _samples([*base, (0.0, "gpu", _gpu())]),
            run_id=RUN_ID,
            interval_index=0,
            start_seconds=0,
            end_seconds=6,
            observed_at_utc=NOW,
            runtime_bundle_identity_sha256=RUNTIME_ID,
            observer_source_sha256=SOURCE_ID,
            previous_record_sha256=None,
        )
        self.assertEqual(gap.coverage.gpu.reason_code, "max_gap_exceeded")
        self.assertEqual(gap.coverage.gpu.covered_seconds, 5)


if __name__ == "__main__":
    unittest.main()
