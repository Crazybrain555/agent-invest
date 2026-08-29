"""Resident synchronized telemetry observer scheduling and evidence tests."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.synchronized_telemetry_observer import (
    ObserverState,
    SynchronizedObserverLimits,
    SynchronizedTelemetryEvidenceError,
    run_synchronized_telemetry_observer,
    verify_synchronized_telemetry_observer,
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
    TelemetrySampleIdentity,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64


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
    def __init__(self, value: object, *, delay: float = 0) -> None:
        self.value = value
        self.delay = delay
        self.started: list[int] = []

    def sample(self) -> object:
        self.started.append(time.monotonic_ns())
        if self.delay:
            time.sleep(self.delay)
        return self.value


class _FailingSampler:
    def sample(self) -> GpuLaneSnapshot:
        raise ConnectionError("not evidence-safe to expose")


class _DelayedFailingSampler:
    def sample(self) -> GpuLaneSnapshot:
        time.sleep(0.3)
        raise ConnectionError("not evidence-safe to expose")


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
    ):
        return run_synchronized_telemetry_observer(
            artifact_root=root,
            process_profile=_profile(),
            gpu_sampler=gpu_sampler or _Sampler(_gpu_snapshot()),  # type: ignore[arg-type]
            host_sampler=host_sampler or _Sampler(_host_snapshot()),  # type: ignore[arg-type]
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
            frames_bytes = (result.run_directory / "frames.v1.jsonl").read_bytes()
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
            self.assertEqual(len(host.started), 1)

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
            self.assertTrue(result.receipt.epoch_changed)

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
            frames_path = result.run_directory / "frames.v1.jsonl"
            original = frames_path.read_bytes()
            frames_path.write_bytes(original.replace(b'"sequence":0', b'"sequence":9', 1))
            os.chmod(frames_path, 0o600)
            with self.assertRaisesRegex(ValueError, "hash drifted|sequence"):
                verify_synchronized_telemetry_observer(
                    artifact_root=root,
                    run_id=result.receipt.run_id,
                )

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
            self.assertTrue((run_dir / "frames.v1.jsonl").exists())
            self.assertFalse((run_dir / "receipt.v1.json").exists())

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
