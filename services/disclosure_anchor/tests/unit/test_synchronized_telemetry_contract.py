"""Closed-contract tests for synchronized, content-free capacity telemetry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from pydantic import ValidationError

from disclosure_anchor.adapters.runtime.mineru_phase_trace import MineruPhaseEvent
from disclosure_anchor.adapters.runtime.mineru_phase_trace_capture import (
    MineruPhaseTraceCapture,
    summarize_synchronized_phase_capture,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    ApiProcessObservation,
    ApiProcessTelemetryValues,
    BlockedProgressEvent,
    CapacityVector,
    CapacityVectorCreditEvent,
    CapacityVectorSnapshot,
    CgroupCpuStat,
    CgroupMemoryEvents,
    CgroupMemoryStat,
    DocumentProfileLifecycle,
    DurablePageCommitEvent,
    GpuObservation,
    GpuTelemetryValues,
    HostCgroupObservation,
    HostCgroupTelemetryValues,
    LaneQualitySummary,
    MeasuredSafetyMargin,
    PhaseClockBinding,
    PressureLine,
    PressureSample,
    ProcessProfileLifecycle,
    ProcessProfileParameters,
    QueueVllmObservation,
    QueueVllmTelemetryValues,
    SampleClock,
    SampleQuality,
    SynchronizedPhaseSummary,
    SynchronizedTelemetryFrame,
    SynchronizedTelemetryReceipt,
    TelemetryArtifacts,
    validate_credit_event_chain,
    validate_frame_sequence,
)


RUN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
HASH = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
START = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _pressure(*, full: bool = True) -> PressureSample:
    line = PressureLine(
        avg10_pct=0,
        avg60_pct=0,
        avg300_pct=0,
        total_stall_us=0,
    )
    return PressureSample(
        some=line,
        full_status="supported" if full else "unsupported",
        full_reason=None if full else "collector_unsupported",
        full=line if full else None,
    )


def _gpu() -> GpuObservation:
    return GpuObservation(
        status="supported",
        reason=None,
        values=GpuTelemetryValues(
            device_identity_sha256=HASH,
            utilization_pct=80,
            framebuffer_used_bytes=10,
            framebuffer_free_bytes=6,
            framebuffer_total_bytes=16,
            power_usage_watts=250,
        ),
    )


def _process() -> ApiProcessObservation:
    return ApiProcessObservation(
        status="supported",
        reason=None,
        values=ApiProcessTelemetryValues(
            process_epoch_sha256=HASH_B,
            cpu_user_ns_total=100,
            cpu_system_ns_total=50,
            rss_bytes=1000,
            rss_hwm_bytes=1200,
            thread_count=8,
        ),
    )


def _host() -> HostCgroupObservation:
    return HostCgroupObservation(
        status="supported",
        reason=None,
        values=HostCgroupTelemetryValues(
            parent_cgroup_epoch_sha256=HASH_C,
            docker_vm_memory_total_bytes=32_000,
            docker_vm_memory_available_bytes=10_000,
            memory_current_bytes=20_000,
            memory_max_status="bounded",
            memory_max_bytes=32_000,
            memory_stat=CgroupMemoryStat(
                anon_bytes=10_000,
                file_bytes=5_000,
                shmem_bytes=100,
                slab_bytes=500,
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
                usage_ns_total=1000,
                user_ns_total=600,
                system_ns_total=300,
                throttled_ns_total=0,
                throttled_periods_total=0,
            ),
            cpu_psi=_pressure(full=False),
            io_psi=_pressure(),
        ),
    )


def _queue() -> QueueVllmObservation:
    return QueueVllmObservation(
        status="supported",
        reason=None,
        values=QueueVllmTelemetryValues(
            api_queued_tasks=1,
            api_processing_tasks=1,
            api_nonterminal_tasks=2,
            api_http_active_requests=2,
            api_http_pending_requests=0,
            api_max_pending_tasks=4,
            vllm_requests_running=7,
            vllm_requests_waiting=0,
            vllm_kv_cache_usage_ratio=0.1,
            vllm_preemptions_total=0,
        ),
    )


def _unsupported() -> dict[str, object]:
    return {
        "status": "unsupported",
        "reason": "not_due_at_this_tick",
        "values": None,
    }


def _frame(
    *,
    sequence: int,
    lane: str,
    started_ns: int,
    first: bool,
) -> SynchronizedTelemetryFrame:
    nominal = 250 if lane == "gpu_fast" else 1000
    return SynchronizedTelemetryFrame(
        run_id=RUN_ID,
        sequence=sequence,
        lane=lane,
        runtime_bundle_identity_sha256=HASH,
        process_profile_sha256=HASH_B,
        observer_source_sha256=HASH_C,
        clock=SampleClock(
            clock_domain_identity_sha256=HASH_C,
            observed_at_utc=START + timedelta(microseconds=started_ns / 1000),
            scheduled_monotonic_ns=started_ns,
            started_monotonic_ns=started_ns,
            finished_monotonic_ns=started_ns + 1_000_000,
        ),
        quality=SampleQuality(
            nominal_interval_ms=nominal,
            observed_interval_ms=None if first else float(nominal),
            collection_duration_ms=1,
            missed_deadlines=0,
            status="first" if first else "on_time",
        ),
        gpu=_gpu() if lane == "gpu_fast" else GpuObservation(**_unsupported()),
        api_process=_process()
        if lane == "host_slow"
        else ApiProcessObservation(**_unsupported()),
        host_cgroup=_host()
        if lane == "host_slow"
        else HostCgroupObservation(**_unsupported()),
        queue_vllm=_queue(),
    )


def _complete_frames() -> tuple[SynchronizedTelemetryFrame, ...]:
    schedule = (
        ("gpu_fast", 0, True),
        ("host_slow", 0, True),
        ("gpu_fast", 250_000_000, False),
        ("gpu_fast", 500_000_000, False),
        ("gpu_fast", 750_000_000, False),
        ("gpu_fast", 1_000_000_000, False),
        ("host_slow", 1_000_000_000, False),
        ("gpu_fast", 1_250_000_000, False),
        ("gpu_fast", 1_500_000_000, False),
        ("gpu_fast", 1_750_000_000, False),
    )
    return tuple(
        _frame(sequence=index, lane=lane, started_ns=started_ns, first=first)
        for index, (lane, started_ns, first) in enumerate(schedule)
    )


def _vector(value: int) -> CapacityVector:
    return CapacityVector(
        source_disk_bytes=value,
        raster_cpu_bytes=value,
        tensor_cpu_bytes=value,
        tensor_gpu_bytes=value,
        model_cpu_bytes=value,
        model_gpu_bytes=value,
        document_owner_bytes=value,
        task_slots=value,
        native_owner_slots=value,
        vllm_sequence_slots=value,
    )


def _process_profile() -> ProcessProfileLifecycle:
    return ProcessProfileLifecycle(
        runtime_bundle_identity_sha256=HASH,
        process_epoch_sha256=HASH_C,
        process_profile_sha256=HASH_B,
        clock_domain_identity_sha256=HASH_C,
        started_at_utc=START,
        started_monotonic_ns=0,
        parameters=ProcessProfileParameters(
            requested_hybrid_batch_ratio=4,
            effective_hybrid_batch_ratio=4,
            api_task_slots=2,
            api_max_pending_tasks=4,
            inference_concurrency=7,
            processing_window_size=16,
            vllm_max_num_seqs=128,
        ),
    )


def _receipt(*, status: str = "complete") -> SynchronizedTelemetryReceipt:
    return SynchronizedTelemetryReceipt(
        run_id=RUN_ID,
        runtime_bundle_identity_sha256=HASH,
        process_profile=_process_profile(),
        observer_source_sha256=HASH_C,
        clock_domain_identity_sha256=HASH_C,
        started_at_utc=START,
        finished_at_utc=START + timedelta(seconds=2),
        started_monotonic_ns=0,
        finished_monotonic_ns=2_000_000_000,
        status=status,
        lane_quality=(
            LaneQualitySummary(
                lane="gpu_fast",
                nominal_interval_ms=250,
                sample_count=8,
                maximum_gap_ms=250,
                late_sample_count=0,
                missed_deadline_count=0,
                supported_frame_count=8,
            ),
            LaneQualitySummary(
                lane="host_slow",
                nominal_interval_ms=1000,
                sample_count=2,
                maximum_gap_ms=1000,
                late_sample_count=0,
                missed_deadline_count=0,
                supported_frame_count=2,
            ),
        ),
        observer_cpu_ns=20_000_000,
        observed_clock_divergence_ns=0,
        epoch_changed=False,
        unsupported_observation_count=0,
        safety_margin=MeasuredSafetyMargin(
            sample_count=30,
            capacity=_vector(10),
            model_baseline=_vector(2),
            uncertainty=_vector(1),
            safety_margin=_vector(2),
        ),
        artifacts=TelemetryArtifacts(
            frames_sha256=HASH,
            progress_events_sha256=HASH_B,
            vector_events_sha256=HASH_C,
        ),
    )


class SynchronizedTelemetryContractTests(unittest.TestCase):
    def test_frames_require_explicit_coverage_and_cadence(self) -> None:
        fast = _frame(sequence=0, lane="gpu_fast", started_ns=0, first=True)
        slow = _frame(sequence=1, lane="host_slow", started_ns=0, first=True)

        self.assertEqual(fast.gpu.status, "supported")
        self.assertEqual(fast.host_cgroup.reason, "not_due_at_this_tick")
        self.assertEqual(slow.host_cgroup.status, "supported")
        with self.assertRaises(ValidationError):
            GpuObservation(status="unsupported", reason=None, values=None)
        payload = fast.model_dump()
        payload["quality"]["nominal_interval_ms"] = 1000
        with self.assertRaises(ValidationError):
            SynchronizedTelemetryFrame.model_validate(payload)

    def test_profile_lifecycles_are_process_or_document_frozen(self) -> None:
        process = _process_profile()
        document = DocumentProfileLifecycle(
            attempt_identity_sha256=HASH,
            process_profile_sha256=process.process_profile_sha256,
            document_profile_sha256=HASH_C,
            started_monotonic_ns=1,
            finished_monotonic_ns=2,
            window_size=8,
            pipeline_depth=1,
            max_resident_pages=16,
            outcome="succeeded",
        )

        self.assertEqual(process.lifecycle, "startup_only")
        self.assertEqual(document.lifecycle, "document_frozen")
        payload = document.model_dump()
        payload["max_resident_pages"] = 8
        with self.assertRaises(ValidationError):
            DocumentProfileLifecycle.model_validate(payload)

    def test_vector_margin_snapshot_and_event_chain_conserve_each_dimension(self) -> None:
        CapacityVectorSnapshot(
            capacity=_vector(10),
            model_baseline=_vector(2),
            safety_margin=_vector(2),
            active_reserved=_vector(3),
            available=_vector(3),
        )
        reserve = CapacityVectorCreditEvent(
            sequence=0,
            process_epoch_sha256=HASH,
            lease_identity_sha256=HASH_B,
            action="reserve",
            delta=_vector(2),
            available_before=_vector(5),
            available_after=_vector(3),
            monotonic_ns=1,
        )
        returned = CapacityVectorCreditEvent(
            sequence=1,
            process_epoch_sha256=HASH,
            lease_identity_sha256=HASH_B,
            action="return",
            delta=_vector(2),
            available_before=_vector(3),
            available_after=_vector(5),
            monotonic_ns=2,
        )
        validate_credit_event_chain((reserve, returned), require_closed=True)

        with self.assertRaisesRegex(ValueError, "unreturned"):
            validate_credit_event_chain((reserve,), require_closed=True)
        with self.assertRaises(ValidationError):
            MeasuredSafetyMargin(
                sample_count=1,
                capacity=_vector(10),
                model_baseline=_vector(2),
                uncertainty=_vector(3),
                safety_margin=_vector(2),
            )

    def test_receipt_fails_closed_on_epoch_overhead_or_missing_coverage(self) -> None:
        receipt = _receipt()
        self.assertFalse(receipt.activation_authorized)

        for update, expected_status in (
            ({"epoch_changed": True}, "unsafe"),
            ({"observer_cpu_ns": 50_000_000}, "unsafe"),
            ({"unsupported_observation_count": 1}, "incomplete"),
        ):
            payload = receipt.model_dump()
            payload.update(update)
            payload["status"] = expected_status
            parsed = SynchronizedTelemetryReceipt.model_validate(payload)
            self.assertEqual(parsed.status, expected_status)
        payload = receipt.model_dump()
        payload["observer_cpu_ns"] = 50_000_000
        with self.assertRaises(ValidationError):
            SynchronizedTelemetryReceipt.model_validate(payload)
        payload = receipt.model_dump()
        payload["finished_at_utc"] = START + timedelta(seconds=2.1)
        payload["observed_clock_divergence_ns"] = 100_000_000
        with self.assertRaises(ValidationError):
            SynchronizedTelemetryReceipt.model_validate(payload)

    def test_frame_sequence_aligns_lanes_to_one_receipt_clock(self) -> None:
        frames = _complete_frames()
        validate_frame_sequence(frames, receipt=_receipt())

        payload = frames[-1].model_dump()
        payload["quality"]["observed_interval_ms"] = 900
        bad = SynchronizedTelemetryFrame.model_validate(payload)
        with self.assertRaisesRegex(ValueError, "interval differs"):
            validate_frame_sequence((*frames[:-1], bad), receipt=_receipt())
        payload = frames[-1].model_dump()
        payload["clock"]["clock_domain_identity_sha256"] = HASH
        wrong_clock = SynchronizedTelemetryFrame.model_validate(payload)
        with self.assertRaisesRegex(ValueError, "identity drifted"):
            validate_frame_sequence(
                (*frames[:-1], wrong_clock),
                receipt=_receipt(),
            )
        payload = _receipt().model_dump()
        payload["lane_quality"][0]["sample_count"] = 7
        payload["lane_quality"][0]["supported_frame_count"] = 7
        forged_quality = SynchronizedTelemetryReceipt.model_validate(payload)
        with self.assertRaisesRegex(ValueError, "lane quality receipt drifted"):
            validate_frame_sequence(frames, receipt=forged_quality)
        payload = _receipt().model_dump()
        payload["unsupported_observation_count"] = 1
        payload["status"] = "incomplete"
        forged_unsupported = SynchronizedTelemetryReceipt.model_validate(payload)
        with self.assertRaisesRegex(ValueError, "unsupported observation count"):
            validate_frame_sequence(frames, receipt=forged_unsupported)

    def test_progress_and_phase_summary_are_content_free_and_covered(self) -> None:
        blocked = BlockedProgressEvent(
            run_id=RUN_ID,
            sequence=0,
            process_epoch_sha256=HASH,
            process_profile_sha256=HASH_B,
            clock_domain_identity_sha256=HASH_C,
            observed_at_utc=START,
            monotonic_ns=10,
            blocked_reason="gpu_input_starved",
            blocked_duration_ns=100,
        )
        committed = DurablePageCommitEvent(
            run_id=RUN_ID,
            sequence=1,
            process_epoch_sha256=HASH,
            process_profile_sha256=HASH_B,
            clock_domain_identity_sha256=HASH_C,
            observed_at_utc=START,
            monotonic_ns=20,
            source_identity_sha256=HASH_C,
            committed_source_pages=10,
            cumulative_unique_source_pages=10,
            commit_latency_ns=200,
        )
        summary = SynchronizedPhaseSummary(
            runtime_bundle_identity_sha256=HASH,
            process_profile_sha256=HASH_B,
            clock_domain_identity_sha256=HASH_C,
            phase_capture_sha256=HASH,
            telemetry_receipt_sha256=HASH_C,
            phase_started_monotonic_ns=10,
            phase_finished_monotonic_ns=20,
            telemetry_started_monotonic_ns=0,
            telemetry_finished_monotonic_ns=30,
            gpu_fast_samples_in_phase=4,
            host_slow_samples_in_phase=1,
            blocked_duration_ns=blocked.blocked_duration_ns,
            unique_durable_source_pages_committed=(
                committed.committed_source_pages
            ),
        )

        encoded = json.dumps(
            [
                blocked.model_dump(mode="json"),
                committed.model_dump(mode="json"),
                summary.model_dump(mode="json"),
            ],
            sort_keys=True,
        )
        self.assertIn("blocked_reason", encoded)
        self.assertIn("unique_durable_pages_committed", encoded)
        for forbidden in ("document_id", "company_name", "source_url", "task_id"):
            self.assertNotIn(forbidden, encoded)

    def test_phase_summary_rejects_an_unbound_monotonic_clock(self) -> None:
        process_epoch = "d" * 32
        event = MineruPhaseEvent(
            append_index=None,
            actual_decoded_bytes=None,
            backend="hybrid",
            duration_ns=1_500_000_000,
            ended_monotonic_ns=1_750_000_000,
            event="document_end",
            inner_inference_concurrency=7,
            max_resident_decoded_bytes=1,
            max_resident_pages=16,
            outcome="success",
            page_count=1,
            page_end_exclusive=None,
            page_start=None,
            phase="document",
            pipeline_depth=0,
            pipeline_mode="legacy",
            process_epoch=process_epoch,
            profile_id="legacy",
            profile_sha256=HASH_B,
            reserved_decoded_bytes=None,
            resident_decoded_bytes_after_acquire=None,
            resident_pages_after_acquire=None,
            sequence=1,
            started_monotonic_ns=250_000_000,
            source_pdf_bytes=1,
            total_windows=1,
            trace_id="e" * 32,
            window_index=None,
            window_page_count=None,
            window_size=16,
            vllm_max_num_seqs=128,
        )
        capture = MineruPhaseTraceCapture(
            active_profile_sha256=HASH_B,
            capacity_mode="legacy",
            collected_at_utc=START.isoformat(),
            collector_path=r"C:\\collector.ps1",
            collector_sha256=HASH,
            container_id="f" * 64,
            container_image="mineru-api",
            container_image_id=HASH,
            container_started_at_utc=START.isoformat(),
            line_count=1,
            lines_sha256=HASH,
            since_utc=START.isoformat(),
            trace_bytes=1,
            until_utc=(START + timedelta(seconds=2)).isoformat(),
            windows_node_identity_sha256=HASH,
            events=(event,),
        )
        binding = PhaseClockBinding(
            phase_process_epoch=process_epoch,
            clock_domain_identity_sha256=HASH,
            binding_artifact_sha256=HASH,
        )
        with self.assertRaisesRegex(ValueError, "not comparable"):
            summarize_synchronized_phase_capture(
                capture,
                telemetry_receipt=_receipt(),
                telemetry_frames=(
                    *_complete_frames(),
                ),
                progress_events=(),
                phase_capture_sha256=HASH,
                telemetry_receipt_sha256=HASH_B,
                phase_clock_binding=binding,
            )
        correct_binding = binding.model_copy(
            update={"clock_domain_identity_sha256": HASH_C}
        )
        wrong_clock_progress = BlockedProgressEvent(
            run_id=RUN_ID,
            sequence=0,
            process_epoch_sha256=HASH,
            process_profile_sha256=HASH_B,
            clock_domain_identity_sha256=HASH,
            observed_at_utc=START,
            monotonic_ns=1_000_000_000,
            blocked_reason="gpu_input_starved",
            blocked_duration_ns=100,
        )
        with self.assertRaisesRegex(ValueError, "progress and phase clocks"):
            summarize_synchronized_phase_capture(
                capture,
                telemetry_receipt=_receipt(),
                telemetry_frames=_complete_frames(),
                progress_events=(wrong_clock_progress,),
                phase_capture_sha256=HASH,
                telemetry_receipt_sha256=HASH_B,
                phase_clock_binding=correct_binding,
            )


if __name__ == "__main__":
    unittest.main()
