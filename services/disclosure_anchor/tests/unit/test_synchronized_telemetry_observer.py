"""Resident synchronized telemetry observer scheduling and evidence tests."""

from __future__ import annotations

from datetime import UTC, datetime
from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import subprocess
import sys
from typing import Any
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import (
    ObserverState,
    SynchronizedObserverLimits,
    SynchronizedObserverResult,
    SynchronizedTelemetryEvidenceError,
    run_synchronized_telemetry_observer,
    verify_synchronized_telemetry_observer,
    validate_synchronized_telemetry_v2,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    ApiProcessObservation,
    ApiProcessTelemetryValues,
    CgroupCpuStat,
    CgroupMemoryEvents,
    CgroupMemoryStat,
    GpuObservation,
    GpuTelemetryValues,
    HostCgroupObservation,
    HostCgroupTelemetryValues,
    PressureLine,
    PressureSample,
    ProcessProfileLifecycle,
    ProcessProfileParameters,
    QueueVllmObservation,
    QueueVllmTelemetryValues,
)
from disclosure_anchor.application.ports.synchronized_telemetry import (
    GpuLaneSnapshot,
    HostLaneSnapshot,
    ResidentTelemetryCollectorSpec,
    TelemetrySampleIdentity,
    TelemetrySnapshotDeadline,
    TelemetrySnapshotDeadlineExceeded,
    TelemetrySnapshotTransportUnavailable,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
COLLECTOR_ID = "sha256:" + "e" * 64


def _profile() -> ProcessProfileLifecycle:
    return ProcessProfileLifecycle(
        runtime_bundle_identity_sha256=HASH_A,
        process_epoch_sha256=HASH_B,
        process_profile_sha256=HASH_C,
        clock_domain_identity_sha256=HASH_D,
        started_at_utc=datetime(2026, 8, 30, tzinfo=UTC),
        started_monotonic_ns=1,
        parameters=ProcessProfileParameters(
            requested_hybrid_batch_ratio=1,
            effective_hybrid_batch_ratio=1,
            api_task_slots=1,
            api_max_pending_tasks=2,
            inference_concurrency=7,
            processing_window_size=16,
            vllm_max_num_seqs=128,
        ),
    )


def _identity(*, runtime: str = HASH_A) -> TelemetrySampleIdentity:
    return TelemetrySampleIdentity(
        runtime_bundle_identity_sha256=runtime,
        process_profile_sha256=HASH_C,
        clock_domain_identity_sha256=HASH_D,
    )


def _pressure() -> PressureSample:
    line = PressureLine(
        avg10_pct=0,
        avg60_pct=0,
        avg300_pct=0,
        total_stall_us=0,
    )
    return PressureSample(
        some=line,
        full_status="supported",
        full_reason=None,
        full=line,
    )


def _gpu_snapshot(*, runtime: str = HASH_A) -> GpuLaneSnapshot:
    return GpuLaneSnapshot(
        identity=_identity(runtime=runtime),
        gpu=GpuObservation(
            status="supported",
            reason=None,
            values=GpuTelemetryValues(
                device_identity_sha256=HASH_A,
                utilization_pct=70,
                framebuffer_used_bytes=8,
                framebuffer_free_bytes=8,
                framebuffer_total_bytes=16,
                power_usage_watts=200,
            ),
        ),
    )


def _host_snapshot() -> HostLaneSnapshot:
    return HostLaneSnapshot(
        identity=_identity(),
        api_process=ApiProcessObservation(
            status="supported",
            reason=None,
            values=ApiProcessTelemetryValues(
                process_epoch_sha256=HASH_B,
                cpu_user_ns_total=1,
                cpu_system_ns_total=1,
                rss_bytes=10,
                rss_hwm_bytes=10,
                thread_count=1,
            ),
        ),
        host_cgroup=HostCgroupObservation(
            status="supported",
            reason=None,
            values=HostCgroupTelemetryValues(
                parent_cgroup_epoch_sha256=HASH_B,
                docker_vm_memory_total_bytes=100,
                docker_vm_memory_available_bytes=50,
                memory_current_bytes=20,
                memory_max_status="bounded",
                memory_max_bytes=100,
                memory_stat=CgroupMemoryStat(
                    anon_bytes=1,
                    file_bytes=1,
                    shmem_bytes=1,
                    slab_bytes=1,
                ),
                memory_events=CgroupMemoryEvents(
                    low_total=0,
                    high_total=0,
                    max_total=0,
                    oom_total=0,
                    oom_kill_total=0,
                    oom_group_kill_total=0,
                ),
                memory_psi=_pressure(),
                cpu_stat=CgroupCpuStat(
                    usage_ns_total=2,
                    user_ns_total=1,
                    system_ns_total=1,
                    throttled_ns_total=0,
                    throttled_periods_total=0,
                ),
                cpu_psi=_pressure(),
                io_psi=_pressure(),
            ),
        ),
        queue_vllm=QueueVllmObservation(
            status="supported",
            reason=None,
            values=QueueVllmTelemetryValues(
                api_queued_tasks=0,
                api_processing_tasks=1,
                api_nonterminal_tasks=1,
                api_http_active_requests=1,
                api_http_pending_requests=0,
                api_max_pending_tasks=2,
                vllm_requests_running=1,
                vllm_requests_waiting=0,
                vllm_kv_cache_usage_ratio=0.1,
                vllm_preemptions_total=0,
            ),
        ),
    )


class _Sampler:
    collector_identity_sha256 = COLLECTOR_ID
    def __init__(self, value: object, *, delay: float = 0) -> None:
        self.value = value
        self.delay = delay
        self.started: list[int] = []

    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> object:
        self.started.append(time.monotonic_ns())
        if self.delay:
            time.sleep(self.delay)
        return self.value


class _FailingSampler:
    collector_identity_sha256 = COLLECTOR_ID
    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> GpuLaneSnapshot:
        raise TelemetrySnapshotTransportUnavailable("not evidence-safe to expose")


class _DelayedFailingSampler:
    collector_identity_sha256 = COLLECTOR_ID
    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> GpuLaneSnapshot:
        time.sleep(0.3)
        raise TelemetrySnapshotTransportUnavailable("not evidence-safe to expose")


class _DeadlineSampler:
    collector_identity_sha256 = COLLECTOR_ID
    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> GpuLaneSnapshot:
        self.deadline = deadline
        raise TelemetrySnapshotDeadlineExceeded("resident snapshot deadline")


class _ProgrammingErrorSampler:
    collector_identity_sha256 = COLLECTOR_ID
    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> GpuLaneSnapshot:
        raise AssertionError("collector programming defect")


class _ForeverHangSampler:
    collector_identity_sha256 = COLLECTOR_ID
    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> GpuLaneSnapshot:
        while True:
            time.sleep(60)


class _LateSampler:
    collector_identity_sha256 = COLLECTOR_ID
    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> GpuLaneSnapshot:
        time.sleep(0.4)
        return _gpu_snapshot()


class _HostSequenceSampler:
    collector_identity_sha256 = COLLECTOR_ID

    def __init__(self, values: list[HostLaneSnapshot], *, scenario: str) -> None:
        self.values = values
        self.scenario = scenario
        self.index = 0

    def snapshot(self, *, deadline: TelemetrySnapshotDeadline) -> HostLaneSnapshot:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


def _test_collector_factory(config: dict[str, object]) -> object:
    lane = config["lane"]
    mode = config.get("mode", "normal")
    if mode == "factory_failure":
        raise RuntimeError("factory bootstrap failed")
    if mode == "slow_startup":
        startup_delay = config.get("startup_delay", 0.2)
        assert isinstance(startup_delay, (int, float))
        time.sleep(float(startup_delay))
    if mode == "spawn_descendant":
        child = multiprocessing.get_context("spawn").Process(target=_descendant_sleep)
        child.start()
    if mode == "transport_failure":
        return _FailingSampler()
    if mode == "delayed_transport_failure":
        return _DelayedFailingSampler()
    if mode == "deadline":
        return _DeadlineSampler()
    if mode == "programming_error":
        return _ProgrammingErrorSampler()
    if mode == "hang":
        return _ForeverHangSampler()
    if mode == "late":
        return _LateSampler()
    if mode in {"counter_regression", "oom_increment"}:
        baseline = _host_snapshot()
        if mode == "counter_regression":
            assert baseline.api_process.values is not None
            changed = replace(
                baseline,
                api_process=baseline.api_process.model_copy(
                    update={"values": baseline.api_process.values.model_copy(update={"cpu_user_ns_total": 0})}
                ),
            )
        else:
            assert baseline.host_cgroup.values is not None
            events = baseline.host_cgroup.values.memory_events.model_copy(update={"oom_total": 1})
            changed = replace(
                baseline,
                host_cgroup=baseline.host_cgroup.model_copy(
                    update={"values": baseline.host_cgroup.values.model_copy(update={"memory_events": events})}
                ),
            )
        return _HostSequenceSampler([baseline, changed], scenario=str(mode))
    snapshot = _gpu_snapshot(runtime=str(config.get("runtime", HASH_A))) if lane == "gpu" else _host_snapshot()
    delay = config.get("delay", 0)
    assert isinstance(delay, (int, float))
    return _Sampler(snapshot, delay=float(delay))


def _descendant_sleep() -> None:
    time.sleep(60)


def _collector_spec(*, lane: str, mode: str = "normal", **config: object) -> ResidentTelemetryCollectorSpec:
    payload = {"lane": lane, "mode": mode, **config}
    return ResidentTelemetryCollectorSpec(
        factory_module=__name__,
        factory_qualname="_test_collector_factory",
        canonical_config_json=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        expected_collector_identity_sha256=COLLECTOR_ID,
    )


def _spec_for_sampler(value: object | None, *, lane: str) -> ResidentTelemetryCollectorSpec:
    if value is None:
        return _collector_spec(lane=lane)
    if isinstance(value, _Sampler):
        runtime = value.value.identity.runtime_bundle_identity_sha256 if isinstance(value.value, GpuLaneSnapshot) else HASH_A
        return _collector_spec(lane=lane, delay=value.delay, runtime=runtime)
    modes = {
        _FailingSampler: "transport_failure",
        _DelayedFailingSampler: "delayed_transport_failure",
        _DeadlineSampler: "deadline",
        _ProgrammingErrorSampler: "programming_error",
        _ForeverHangSampler: "hang",
        _LateSampler: "late",
    }
    for kind, mode in modes.items():
        if isinstance(value, kind):
            return _collector_spec(lane=lane, mode=mode)
    if isinstance(value, _HostSequenceSampler):
        return _collector_spec(lane=lane, mode=value.scenario)
    if isinstance(value, ResidentTelemetryCollectorSpec):
        return value
    raise TypeError(f"unknown test collector {type(value).__name__}")


class SynchronizedTelemetryObserverTests(unittest.TestCase):
    def _run(
        self,
        root: Path,
        *,
        gpu_sampler: object | None = None,
        host_sampler: object | None = None,
        duration: float = 1.08,
        cancel_event: threading.Event | None = None,
        limits: SynchronizedObserverLimits | None = None,
        run_id: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    ) -> SynchronizedObserverResult:
        return run_synchronized_telemetry_observer(
            artifact_root=root,
            process_profile=_profile(),
            gpu_collector=_spec_for_sampler(gpu_sampler, lane="gpu"),
            host_collector=_spec_for_sampler(host_sampler, lane="host"),
            duration_seconds=duration,
            cancel_event=cancel_event,
            limits=limits,
            run_id=run_id,
            process_cpu_ns=lambda: 0,
        )

    def test_complete_run_is_private_canonical_jsonl_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            result = self._run(root)

            self.assertEqual(result.state, ObserverState.SEALED)
            self.assertEqual(result.receipt.status, "complete")
            self.assertEqual(result.receipt.termination_reason, "duration_elapsed")
            replay = verify_synchronized_telemetry_observer(
                artifact_root=root,
                run_id=result.receipt.run_id,
            )
            self.assertEqual(replay.receipt, result.receipt)
            self.assertEqual(replay.frames, result.frames)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(result.run_directory.stat().st_mode), 0o700)
            for path in result.run_directory.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.stat().st_nlink, 1)
            frames_bytes = (result.run_directory / "frames.v2.jsonl").read_bytes()
            self.assertTrue(frames_bytes.endswith(b"\n"))
            for line in frames_bytes.splitlines():
                self.assertEqual(
                    line,
                    json.dumps(
                        json.loads(line),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode(),
                )

    def test_lane_ownership_is_mechanically_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(Path(temporary) / "telemetry", duration=0.3)
            index = next(index for index, frame in enumerate(result.frames) if frame.lane == "gpu_fast")
            frame = result.frames[index]
            invalid = frame.model_copy(
                update={
                    "api_process": frame.api_process.model_copy(
                        update={"reason": "collector_disabled"}
                    )
                }
            )
            frames = list(result.frames)
            frames[index] = invalid
            with self.assertRaisesRegex(ValueError, "owned by the host lane"):
                validate_synchronized_telemetry_v2(tuple(frames), receipt=result.receipt)

    def test_slow_host_lane_does_not_block_gpu_deadlines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gpu = _Sampler(_gpu_snapshot())
            host = _Sampler(_host_snapshot(), delay=0.7)
            result = self._run(
                Path(temporary) / "telemetry",
                gpu_sampler=gpu,
                host_sampler=host,
                duration=1.08,
            )

            gpu_starts = [
                frame.clock.started_monotonic_ns
                for frame in result.frames
                if frame.lane == "gpu_fast"
            ]
            self.assertGreaterEqual(len(gpu_starts), 5)
            self.assertLess(
                max(right - left for left, right in zip(gpu_starts, gpu_starts[1:])),
                400_000_000,
            )

    def test_slow_gpu_sampler_skips_deadlines_without_catch_up_burst(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gpu = _Sampler(_gpu_snapshot(), delay=0.55)
            result = self._run(
                Path(temporary) / "telemetry",
                gpu_sampler=gpu,
                duration=1.2,
            )

            quality = {item.lane: item for item in result.receipt.lane_quality}
            self.assertEqual(result.receipt.status, "incomplete")
            self.assertGreater(quality["gpu_fast"].missed_deadline_count, 0)
            starts = [
                frame.clock.started_monotonic_ns
                for frame in result.frames
                if frame.lane == "gpu_fast"
            ]
            self.assertTrue(
                all(right - left >= 500_000_000 for left, right in zip(starts, starts[1:]))
            )

    def test_sampler_failure_seals_incomplete_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                Path(temporary) / "telemetry",
                gpu_sampler=_FailingSampler(),
                duration=0.4,
            )

            self.assertEqual(result.receipt.status, "incomplete")
            self.assertEqual(
                result.receipt.termination_reason,
                "sampler_or_transport_shutdown",
            )
            self.assertGreater(result.receipt.unsupported_observation_count, 0)
            verify_synchronized_telemetry_observer(
                artifact_root=Path(temporary) / "telemetry",
                run_id=result.receipt.run_id,
            )

    def test_deadline_is_typed_incomplete_but_programming_error_fails_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                Path(temporary) / "deadline",
                gpu_sampler=_DeadlineSampler(),
                duration=0.4,
            )
            self.assertEqual(result.receipt.status, "incomplete")
            gpu = next(frame for frame in result.frames if frame.lane == "gpu_fast")
            self.assertEqual(gpu.gpu.reason, "deadline_exceeded")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(SynchronizedTelemetryEvidenceError) as caught:
                self._run(
                    Path(temporary) / "programming-error",
                    gpu_sampler=_ProgrammingErrorSampler(),
                    duration=0.4,
                )
            self.assertIsInstance(caught.exception.__cause__, AssertionError)

    def test_internal_stop_wakes_other_lane_without_extra_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            host = _Sampler(_host_snapshot())
            result = self._run(
                Path(temporary) / "telemetry",
                gpu_sampler=_DelayedFailingSampler(),
                host_sampler=host,
                duration=2,
            )

            self.assertEqual(
                result.receipt.termination_reason,
                "sampler_or_transport_shutdown",
            )
            self.assertLessEqual(
                sum(frame.lane == "host_slow" for frame in result.frames),
                1,
            )

    def test_pre_cancelled_run_seals_incomplete_receipt_for_both_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cancel = threading.Event()
            cancel.set()
            result = self._run(
                Path(temporary) / "telemetry",
                duration=1,
                cancel_event=cancel,
            )

            self.assertEqual(result.receipt.status, "incomplete")
            self.assertEqual(result.receipt.termination_reason, "cancelled")
            self.assertEqual({frame.lane for frame in result.frames}, {"gpu_fast", "host_slow"})

    def test_duration_shorter_than_host_period_still_covers_both_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                Path(temporary) / "telemetry",
                duration=0.08,
            )

            self.assertEqual(result.receipt.status, "complete")
            self.assertEqual(
                {frame.lane for frame in result.frames},
                {"gpu_fast", "host_slow"},
            )

    def test_both_collectors_ready_before_clock_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            started = time.monotonic()
            result = self._run(
                Path(temporary) / "telemetry",
                gpu_sampler=_collector_spec(
                    lane="gpu", mode="slow_startup", startup_delay=0.25
                ),
                host_sampler=_collector_spec(
                    lane="host", mode="slow_startup", startup_delay=0.25
                ),
                duration=0.08,
            )
            self.assertGreater(time.monotonic() - started, 0.5)
            self.assertEqual(result.receipt.status, "complete")
            self.assertGreaterEqual(
                min(frame.clock.started_monotonic_ns for frame in result.frames),
                result.receipt.started_monotonic_ns,
            )

    def test_factory_and_pickle_startup_failures_cleanup_all_children(self) -> None:
        baseline = {child.pid for child in multiprocessing.active_children()}
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(SynchronizedTelemetryEvidenceError, "FAILED_EVIDENCE"):
                self._run(
                    Path(temporary) / "factory",
                    gpu_sampler=_collector_spec(lane="gpu", mode="factory_failure"),
                    duration=0.1,
                )
        invalid = object.__new__(ResidentTelemetryCollectorSpec)
        object.__setattr__(invalid, "factory_module", __name__)
        object.__setattr__(invalid, "factory_qualname", "_test_collector_factory")
        object.__setattr__(invalid, "canonical_config_json", threading.Lock())
        object.__setattr__(invalid, "expected_collector_identity_sha256", COLLECTOR_ID)
        object.__setattr__(invalid, "descendants_capability", "forbidden")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(SynchronizedTelemetryEvidenceError, "FAILED_EVIDENCE"):
                self._run(
                    Path(temporary) / "pickle",
                    gpu_sampler=invalid,
                    duration=0.1,
                )
        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()}, baseline
        )

    def test_descendant_capability_violation_is_failed_evidence(self) -> None:
        baseline = {child.pid for child in multiprocessing.active_children()}
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(SynchronizedTelemetryEvidenceError, "FAILED_EVIDENCE"):
                self._run(
                    Path(temporary) / "telemetry",
                    gpu_sampler=_collector_spec(lane="gpu", mode="spawn_descendant"),
                    duration=0.1,
                )
        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()}, baseline
        )

    def test_twenty_runs_do_not_leak_file_descriptors_or_children(self) -> None:
        before_fds = len(os.listdir("/dev/fd"))
        before_children = {child.pid for child in multiprocessing.active_children()}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            for index in range(20):
                result = self._run(
                    root,
                    duration=0.02,
                    run_id=f"00000000-0000-4000-8000-{index:012x}",
                )
                self.assertEqual(result.receipt.status, "complete")
        self.assertEqual(len(os.listdir("/dev/fd")), before_fds)
        self.assertEqual(
            {child.pid for child in multiprocessing.active_children()}, before_children
        )

    def test_spawn_safe_main_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "spawn_smoke.py"
            script.write_text(
                """
from pathlib import Path
import tempfile
from tests.unit.test_synchronized_telemetry_observer import (
    _collector_spec, _profile,
)
from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import (
    run_synchronized_telemetry_observer,
)

if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as temporary:
        result = run_synchronized_telemetry_observer(
            artifact_root=Path(temporary) / 'telemetry',
            process_profile=_profile(),
            gpu_collector=_collector_spec(lane='gpu'),
            host_collector=_collector_spec(lane='host'),
            duration_seconds=0.08,
            process_cpu_ns=lambda: 0,
        )
        print(result.receipt.status)
""".lstrip(),
                encoding="utf-8",
            )
            environment = dict(os.environ)
            service_root = Path(__file__).resolve().parents[2]
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(service_root), str(service_root / "src"))
            )
            completed = subprocess.run(
                [sys.executable, str(script)],
                cwd=service_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "complete")

    def test_forever_hang_is_killed_at_deadline_without_orphan_process(self) -> None:
        before = {process.pid for process in __import__("multiprocessing").active_children()}
        with tempfile.TemporaryDirectory() as temporary:
            started = time.monotonic()
            result = self._run(
                Path(temporary) / "telemetry",
                gpu_sampler=_ForeverHangSampler(),
                duration=0.8,
            )
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(result.receipt.status, "incomplete")
            gpu = [frame for frame in result.frames if frame.lane == "gpu_fast"]
            self.assertTrue(gpu)
            self.assertTrue(all(frame.gpu.reason == "deadline_exceeded" for frame in gpu))
        after = {process.pid for process in __import__("multiprocessing").active_children()}
        self.assertEqual(after, before)

    def test_cancel_during_hang_has_bounded_drain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cancel = threading.Event()
            timer = threading.Timer(0.1, cancel.set)
            timer.start()
            started = time.monotonic()
            try:
                result = self._run(
                    Path(temporary) / "telemetry",
                    gpu_sampler=_ForeverHangSampler(),
                    duration=2,
                    cancel_event=cancel,
                )
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(result.receipt.termination_reason, "cancelled")

    def test_late_return_is_discarded_and_does_not_pollute_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            late = self._run(root, gpu_sampler=_LateSampler(), duration=0.8)
            gpu = [frame for frame in late.frames if frame.lane == "gpu_fast"]
            self.assertTrue(gpu)
            self.assertTrue(all(frame.gpu.reason == "deadline_exceeded" for frame in gpu))
            clean = self._run(
                root,
                duration=1.08,
                run_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            )
            self.assertTrue(any(frame.gpu.status == "supported" for frame in clean.frames))

    def test_cancel_during_first_sample_drains_and_seals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cancel = threading.Event()
            timer = threading.Timer(0.05, cancel.set)
            timer.start()
            try:
                result = self._run(
                    Path(temporary) / "telemetry",
                    gpu_sampler=_Sampler(_gpu_snapshot(), delay=0.2),
                    duration=1,
                    cancel_event=cancel,
                )
            finally:
                timer.cancel()

            self.assertEqual(result.receipt.termination_reason, "cancelled")
            self.assertEqual(result.receipt.status, "incomplete")

    def test_identity_drift_is_unsafe_and_stops_both_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                Path(temporary) / "telemetry",
                gpu_sampler=_Sampler(_gpu_snapshot(runtime=HASH_B)),
                duration=1,
            )

            self.assertEqual(result.receipt.status, "unsafe")
            self.assertEqual(result.receipt.termination_reason, "identity_drift")
            self.assertFalse(result.receipt.epoch_changed)
            self.assertEqual(result.receipt.safety_drift_reasons, ("identity_drift",))

    def test_counter_regression_and_oom_increment_are_closed_unsafe_drifts(self) -> None:
        baseline = _host_snapshot()
        assert baseline.api_process.values is not None
        regressed = replace(
            baseline,
            api_process=baseline.api_process.model_copy(
                update={
                    "values": baseline.api_process.values.model_copy(
                        update={"cpu_user_ns_total": 0}
                    )
                }
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                Path(temporary) / "regression",
                host_sampler=_HostSequenceSampler([baseline, regressed], scenario="counter_regression"),
                duration=1.2,
            )
            self.assertEqual(result.receipt.status, "unsafe")
            self.assertIn("counter_regression", result.receipt.safety_drift_reasons)

        assert baseline.host_cgroup.values is not None
        events = baseline.host_cgroup.values.memory_events.model_copy(update={"oom_total": 1})
        oom = replace(
            baseline,
            host_cgroup=baseline.host_cgroup.model_copy(
                update={
                    "values": baseline.host_cgroup.values.model_copy(
                        update={"memory_events": events}
                    )
                }
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                Path(temporary) / "oom",
                host_sampler=_HostSequenceSampler([baseline, oom], scenario="oom_increment"),
                duration=1.2,
            )
            self.assertEqual(result.receipt.status, "unsafe")
            self.assertIn("oom_increment", result.receipt.safety_drift_reasons)

    def test_mailbox_overflow_never_blocks_gpu_and_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                Path(temporary) / "telemetry",
                host_sampler=_Sampler(_host_snapshot(), delay=0.9),
                duration=1.2,
                limits=SynchronizedObserverLimits(mailbox_records_per_lane=1),
            )

            self.assertEqual(result.receipt.status, "incomplete")
            self.assertEqual(result.receipt.termination_reason, "queue_overflow")

    def test_frame_bound_stops_sampling_and_seals_incomplete_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                Path(temporary) / "telemetry",
                duration=1,
                limits=SynchronizedObserverLimits(maximum_frame_records=2),
            )

            self.assertEqual(len(result.frames), 2)
            self.assertEqual(result.receipt.status, "incomplete")
            self.assertEqual(
                result.receipt.termination_reason,
                "artifact_bound_exceeded",
            )

    def test_replay_rejects_tamper_and_unexpected_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            result = self._run(root)
            frames_path = result.run_directory / "frames.v2.jsonl"
            original = frames_path.read_bytes()
            frames_path.write_bytes(original.replace(b'"sequence":0', b'"sequence":9', 1))
            os.chmod(frames_path, 0o600)
            with self.assertRaisesRegex(ValueError, "hash drifted|sequence"):
                verify_synchronized_telemetry_observer(
                    artifact_root=root,
                    run_id=result.receipt.run_id,
                )

    def test_return_requires_post_receipt_replay_from_anchored_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            original = __import__(
                "disclosure_anchor.adapters.runtime.synchronized_telemetry_observer",
                fromlist=["_FrameWriter"],
            )._FrameWriter.replay_unsealed

            def tamper_then_replay(writer: Any) -> Any:
                path = writer.run_directory / "frames.v2.jsonl"
                path.write_bytes(path.read_bytes().replace(b'"sequence":0', b'"sequence":9', 1))
                os.chmod(path, 0o600)
                return original(writer)

            with patch(
                "disclosure_anchor.adapters.runtime.synchronized_telemetry_observer._FrameWriter.replay_unsealed",
                new=tamper_then_replay,
            ):
                with self.assertRaisesRegex(SynchronizedTelemetryEvidenceError, "FAILED_EVIDENCE"):
                    self._run(root, duration=0.3)

    def test_sealed_return_rejects_modified_seal_before_anchored_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            module = __import__(
                "disclosure_anchor.adapters.runtime.synchronized_telemetry_observer",
                fromlist=["_FrameWriter"],
            )
            original = module._FrameWriter.write_seal

            def write_then_modify(writer: Any, seal: Any) -> None:
                original(writer, seal)
                path = writer.run_directory / "seal.v2.json"
                path.write_bytes(path.read_bytes() + b" ")
                os.chmod(path, 0o600)

            with patch(
                "disclosure_anchor.adapters.runtime.synchronized_telemetry_observer._FrameWriter.write_seal",
                new=write_then_modify,
            ):
                with self.assertRaisesRegex(SynchronizedTelemetryEvidenceError, "FAILED_EVIDENCE"):
                    self._run(root, duration=0.3)

    def test_atomic_seal_name_replace_before_first_same_fd_stat_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            module = __import__(
                "disclosure_anchor.adapters.runtime.synchronized_telemetry_observer",
                fromlist=["_FrameWriter"],
            )
            original = module._FrameWriter._bind_written_descriptor

            def replace_before_bind(
                writer: Any,
                filename: str,
                descriptor: int,
                *,
                maximum_bytes: int,
            ) -> Any:
                if filename == "seal.v2.json":
                    path = writer.run_directory / filename
                    replacement = writer.run_directory / "seal-replacement.tmp"
                    replacement.write_bytes(path.read_bytes())
                    os.chmod(replacement, 0o600)
                    os.replace(replacement, path)
                return original(
                    writer,
                    filename,
                    descriptor,
                    maximum_bytes=maximum_bytes,
                )

            with patch(
                "disclosure_anchor.adapters.runtime.synchronized_telemetry_observer._FrameWriter._bind_written_descriptor",
                new=replace_before_bind,
            ):
                with self.assertRaisesRegex(SynchronizedTelemetryEvidenceError, "FAILED_EVIDENCE"):
                    self._run(root, duration=0.3)

    def test_sealed_return_rejects_root_or_run_rename_and_replace(self) -> None:
        module = __import__(
            "disclosure_anchor.adapters.runtime.synchronized_telemetry_observer",
            fromlist=["_FrameWriter"],
        )
        original = module._FrameWriter.write_seal
        for replace_root in (True, False):
            with self.subTest(replace_root=replace_root), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "telemetry"

                def write_then_replace(writer: Any, seal: Any) -> None:
                    original(writer, seal)
                    if replace_root:
                        moved = root.with_name("telemetry-moved")
                        root.rename(moved)
                        root.mkdir(mode=0o700)
                        (root / writer.run_directory.name).mkdir(mode=0o700)
                    else:
                        run = writer.run_directory
                        moved = run.with_name(run.name + "-moved")
                        run.rename(moved)
                        run.mkdir(mode=0o700)

                with patch(
                    "disclosure_anchor.adapters.runtime.synchronized_telemetry_observer._FrameWriter.write_seal",
                    new=write_then_replace,
                ):
                    with self.assertRaisesRegex(SynchronizedTelemetryEvidenceError, "FAILED_EVIDENCE"):
                        self._run(root, duration=0.3)

    def test_receipt_write_failure_is_failed_evidence_not_false_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            with patch(
                "disclosure_anchor.adapters.runtime.synchronized_telemetry_observer._FrameWriter.write_receipt",
                side_effect=OSError("disk failed"),
            ):
                with self.assertRaisesRegex(
                    SynchronizedTelemetryEvidenceError, "FAILED_EVIDENCE"
                ):
                    self._run(root, duration=0.3)
            run_dir = root / "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            self.assertTrue((run_dir / "frames.v2.jsonl").exists())
            self.assertFalse((run_dir / "receipt.v2.json").exists())

    def test_frame_fsync_failure_is_failed_evidence_and_does_not_mask_cause(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            with patch(
                "disclosure_anchor.adapters.runtime.synchronized_telemetry_observer.os.fsync",
                side_effect=[None, None, OSError("fsync failed"), None],
            ):
                with self.assertRaisesRegex(
                    SynchronizedTelemetryEvidenceError, "FAILED_EVIDENCE"
                ) as caught:
                    self._run(root, duration=0.08)
            self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_existing_run_and_symlink_root_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            real = parent / "real"
            real.mkdir(mode=0o700)
            link = parent / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(SynchronizedTelemetryEvidenceError):
                self._run(link, duration=0.3)


if __name__ == "__main__":
    unittest.main()
