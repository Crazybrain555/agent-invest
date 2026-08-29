"""Closed-contract tests for synchronized, content-free capacity telemetry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import asdict
import hashlib
import json
import unittest

from pydantic import ValidationError

from disclosure_anchor.adapters.runtime.mineru_phase_trace import MineruPhaseEvent
from disclosure_anchor.adapters.runtime.mineru_phase_trace_capture import (
    MineruPhaseTraceCapture,
    parse_phase_trace_capture,
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
    ProgressEvent,
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
    parse_canonical_json_artifact,
    validate_credit_event_chain,
    validate_frame_sequence,
)


RUN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
HASH = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
START = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _phase_document_end_event(*, process_epoch: str) -> MineruPhaseEvent:
    """Build one complete current trace event; production fields stay strict."""

    return MineruPhaseEvent(
        append_index=None,
        actual_decoded_bytes=None,
        backend="hybrid",
        duration_ns=1_500_000_000,
        ended_monotonic_ns=1_750_000_000,
        event="document_end",
        hybrid_batch_ratio_effective=1,
        hybrid_batch_ratio_ocr_override=False,
        hybrid_batch_ratio_requested=1,
        hybrid_layout_batch_cap=1,
        hybrid_mfr_batch_cap=16,
        hybrid_ocr_det_batch_cap=8,
        hybrid_table_orientation_batch_cap=8,
        inner_inference_concurrency=7,
        max_resident_decoded_bytes=1,
        max_resident_pages=16,
        max_resident_windows=1,
        outcome="success",
        page_count=1,
        page_end_exclusive=None,
        page_start=None,
        phase="document",
        pipeline_depth=0,
        pipeline_mode="serial",
        process_epoch=process_epoch,
        profile_id="serial",
        profile_sha256=HASH_B,
        reserved_decoded_bytes=None,
        reserved_windows=None,
        resident_decoded_bytes_after_acquire=None,
        resident_pages_after_acquire=None,
        resident_windows_after_acquire=None,
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


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _capture(event: MineruPhaseEvent) -> tuple[MineruPhaseTraceCapture, bytes]:
    line = "MINERU_PHASE_TRACE " + json.dumps(
        {"schema": "mineru-phase-trace.v3", **asdict(event)},
        sort_keys=True,
        separators=(",", ":"),
    )
    trace = f"{line}\n".encode()
    payload = _canonical(
        {
            "active_profile_sha256": HASH_B,
            "execution_mode": "serial",
            "collected_at_utc": (START + timedelta(seconds=2)).isoformat(),
            "collector_path": r"C:\collector.ps1",
            "collector_sha256": HASH,
            "container": {
                "health": "healthy",
                "id": "f" * 64,
                "image": "mineru-api",
                "image_id": HASH,
                "name": "mineru-api",
                "oom_killed": False,
                "restart_count": 0,
                "running": True,
                "started_at_utc": START.isoformat().replace("+00:00", "Z"),
                "status": "running",
            },
            "line_count": 1,
            "lines": [line],
            "schema": "mineru-phase-trace-capture.v1",
            "since_utc": START.isoformat(),
            "trace_bytes": len(trace),
            "trace_lines_sha256": _digest(trace),
            "until_utc": (START + timedelta(seconds=2)).isoformat(),
            "windows_node_identity_sha256": HASH,
        }
    )
    return parse_phase_trace_capture(payload), payload


def _binding(capture: MineruPhaseTraceCapture) -> PhaseClockBinding:
    return PhaseClockBinding(
        phase_process_epoch=capture.events[0].process_epoch,
        runtime_bundle_identity_sha256=HASH,
        container_id=capture.container_id,
        container_started_at_utc=datetime.fromisoformat(
            capture.container_started_at_utc
        ),
        phase_node_identity_sha256=capture.windows_node_identity_sha256,
        observer_node_identity_sha256=capture.windows_node_identity_sha256,
        phase_boot_identity_sha256=HASH_B,
        observer_boot_identity_sha256=HASH_B,
        phase_clock_source="linux.clock_gettime.CLOCK_MONOTONIC",
        observer_process_epoch_sha256=HASH_C,
        clock_domain_identity_sha256=HASH_C,
        observer_clock_source="linux.clock_gettime.CLOCK_MONOTONIC",
        attestor_source_sha256=HASH,
    )


def _summary_inputs(
    capture: MineruPhaseTraceCapture,
    capture_bytes: bytes,
    progress_events: tuple[ProgressEvent, ...],
    binding: PhaseClockBinding,
) -> dict[str, object]:
    frames = _complete_frames()
    frames_bytes = _canonical(
        [frame.model_dump(mode="json") for frame in frames]
    )
    progress_bytes = _canonical(
        [event.model_dump(mode="json") for event in progress_events]
    )
    binding_bytes = _canonical(binding.model_dump(mode="json"))
    receipt = _receipt().model_copy(
        update={
            "artifacts": TelemetryArtifacts(
                frames_sha256=_digest(frames_bytes),
                progress_events_sha256=_digest(progress_bytes),
                vector_events_sha256=None,
                phase_capture_sha256=_digest(capture_bytes),
                phase_clock_binding_sha256=_digest(binding_bytes),
            )
        }
    )
    receipt_bytes = _canonical(receipt.model_dump(mode="json"))
    return {
        "telemetry_receipt": receipt,
        "telemetry_frames": frames,
        "progress_events": progress_events,
        "phase_capture_artifact": capture_bytes,
        "telemetry_receipt_artifact": receipt_bytes,
        "telemetry_frames_artifact": frames_bytes,
        "progress_events_artifact": progress_bytes,
        "phase_clock_binding_artifact": binding_bytes,
    }


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
    def test_canonical_artifact_rejects_nonfinite_json_numbers(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-finite JSON number"):
            parse_canonical_json_artifact(b'{"value":NaN}', label="test")

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

        frame_payload = frames[2].model_dump()
        frame_payload["clock"]["observed_at_utc"] = START + timedelta(seconds=2)
        wrong_wall = SynchronizedTelemetryFrame.model_validate(frame_payload)
        with self.assertRaisesRegex(ValueError, "wall and monotonic"):
            validate_frame_sequence(
                (frames[0], frames[1], wrong_wall, *frames[3:]),
                receipt=_receipt(),
            )

    def test_receipt_cadence_is_derived_from_internal_and_boundary_gaps(self) -> None:
        frames = _complete_frames()
        payload = _receipt().model_dump()
        payload["status"] = "incomplete"
        payload["finished_at_utc"] = START + timedelta(seconds=10)
        payload["finished_monotonic_ns"] = 10_000_000_000
        payload["lane_quality"][0].update(
            maximum_gap_ms=8250,
            missed_deadline_count=32,
        )
        payload["lane_quality"][1].update(
            maximum_gap_ms=9000,
            missed_deadline_count=8,
        )
        receipt = SynchronizedTelemetryReceipt.model_validate(payload)
        validate_frame_sequence(frames, receipt=receipt)

        forged = payload.copy()
        forged["lane_quality"] = [dict(item) for item in payload["lane_quality"]]
        forged["lane_quality"][0]["missed_deadline_count"] = 0
        forged["lane_quality"][1]["missed_deadline_count"] = 0
        forged["status"] = "complete"
        self.assertEqual(
            SynchronizedTelemetryReceipt.model_validate(forged).status,
            "complete",
        )
        with self.assertRaisesRegex(ValueError, "lane quality receipt drifted"):
            validate_frame_sequence(
                frames,
                receipt=SynchronizedTelemetryReceipt.model_validate(forged),
            )

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
            blocked_interval_started_monotonic_ns=0,
            blocked_duration_ns=10,
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
        event = _phase_document_end_event(process_epoch=process_epoch)
        capture, capture_bytes = _capture(event)
        binding = _binding(capture).model_copy(
            update={"clock_domain_identity_sha256": HASH}
        )
        with self.assertRaisesRegex(ValidationError, "kernel clock domains differ"):
            PhaseClockBinding.model_validate(
                {
                    **_binding(capture).model_dump(mode="json"),
                    "observer_boot_identity_sha256": HASH,
                }
            )
        with self.assertRaisesRegex(ValueError, "not comparable"):
            summarize_synchronized_phase_capture(
                capture,
                **_summary_inputs(capture, capture_bytes, (), binding),
            )
        correct_binding = binding.model_copy(
            update={"clock_domain_identity_sha256": HASH_C}
        )
        wrong_clock_progress = BlockedProgressEvent(
            run_id=RUN_ID,
            sequence=0,
            process_epoch_sha256=HASH_C,
            process_profile_sha256=HASH_B,
            clock_domain_identity_sha256=HASH,
            observed_at_utc=START,
            monotonic_ns=1_000_000_000,
            blocked_reason="gpu_input_starved",
            blocked_interval_started_monotonic_ns=999_999_900,
            blocked_duration_ns=100,
        )
        with self.assertRaisesRegex(ValueError, "progress and phase clocks"):
            summarize_synchronized_phase_capture(
                capture,
                **_summary_inputs(
                    capture,
                    capture_bytes,
                    (wrong_clock_progress,),
                    correct_binding,
                ),
            )
        wrong_identity_progress = wrong_clock_progress.model_copy(
            update={
                "process_epoch_sha256": HASH,
                "clock_domain_identity_sha256": HASH_C,
            }
        )
        with self.assertRaisesRegex(ValueError, "process/profile identity"):
            summarize_synchronized_phase_capture(
                capture,
                **_summary_inputs(
                    capture,
                    capture_bytes,
                    (wrong_identity_progress,),
                    correct_binding,
                ),
            )

    def test_phase_summary_rejects_duplicate_or_unclosed_progress(self) -> None:
        process_epoch = "d" * 32
        event = _phase_document_end_event(process_epoch=process_epoch)
        capture, capture_bytes = _capture(event)
        binding = _binding(capture)

        def summarize(progress_events: tuple[ProgressEvent, ...]) -> None:
            summarize_synchronized_phase_capture(
                capture,
                **_summary_inputs(
                    capture, capture_bytes, progress_events, binding
                ),
            )

        def committed(
            *, sequence: int, source: str, cumulative: int, monotonic_ns: int
        ) -> DurablePageCommitEvent:
            return DurablePageCommitEvent(
                run_id=RUN_ID,
                sequence=sequence,
                process_epoch_sha256=HASH_C,
                process_profile_sha256=HASH_B,
                clock_domain_identity_sha256=HASH_C,
                observed_at_utc=START,
                monotonic_ns=monotonic_ns,
                source_identity_sha256=source,
                committed_source_pages=1,
                cumulative_unique_source_pages=cumulative,
                commit_latency_ns=1,
            )

        with self.assertRaisesRegex(ValueError, "source identity repeats"):
            summarize(
                (
                    committed(
                        sequence=0,
                        source=HASH,
                        cumulative=1,
                        monotonic_ns=500_000_000,
                    ),
                    committed(
                        sequence=1,
                        source=HASH,
                        cumulative=2,
                        monotonic_ns=600_000_000,
                    ),
                )
            )
        with self.assertRaisesRegex(ValueError, "cumulative count drifted"):
            summarize(
                (
                    committed(
                        sequence=0,
                        source=HASH,
                        cumulative=1,
                        monotonic_ns=500_000_000,
                    ),
                    committed(
                        sequence=1,
                        source=HASH_B,
                        cumulative=3,
                        monotonic_ns=600_000_000,
                    ),
                )
            )
        blocked = (
            BlockedProgressEvent(
                run_id=RUN_ID,
                sequence=0,
                process_epoch_sha256=HASH_C,
                process_profile_sha256=HASH_B,
                clock_domain_identity_sha256=HASH_C,
                observed_at_utc=START,
                monotonic_ns=600_000_000,
                blocked_reason="gpu_input_starved",
                blocked_interval_started_monotonic_ns=400_000_000,
                blocked_duration_ns=200_000_000,
            ),
            BlockedProgressEvent(
                run_id=RUN_ID,
                sequence=1,
                process_epoch_sha256=HASH_C,
                process_profile_sha256=HASH_B,
                clock_domain_identity_sha256=HASH_C,
                observed_at_utc=START,
                monotonic_ns=700_000_000,
                blocked_reason="host_memory_pressure",
                blocked_interval_started_monotonic_ns=500_000_000,
                blocked_duration_ns=200_000_000,
            ),
        )
        with self.assertRaisesRegex(ValueError, "intervals overlap"):
            summarize(blocked)

    def test_phase_summary_recomputes_artifacts_and_binding_identity(self) -> None:
        capture, capture_bytes = _capture(
            _phase_document_end_event(process_epoch="d" * 32)
        )
        binding = _binding(capture)
        inputs = _summary_inputs(capture, capture_bytes, (), binding)
        summary = summarize_synchronized_phase_capture(capture, **inputs)
        self.assertEqual(summary.clock_domain_identity_sha256, HASH_C)

        stale_frames = dict(inputs)
        stale_frames["telemetry_frames_artifact"] = _canonical([])
        with self.assertRaisesRegex(ValueError, "differs from parsed frames"):
            summarize_synchronized_phase_capture(capture, **stale_frames)

        forged_binding = binding.model_copy(
            update={
                "phase_boot_identity_sha256": HASH,
                "observer_boot_identity_sha256": HASH,
            }
        )
        forged = dict(inputs)
        forged["phase_clock_binding_artifact"] = _canonical(
            forged_binding.model_dump(mode="json")
        )
        with self.assertRaisesRegex(
            ValueError, "phase_clock_binding_sha256 artifact hash drifted"
        ):
            summarize_synchronized_phase_capture(capture, **forged)

        wrong_runtime_binding = binding.model_copy(
            update={"runtime_bundle_identity_sha256": HASH_B}
        )
        wrong_runtime = _summary_inputs(
            capture, capture_bytes, (), wrong_runtime_binding
        )
        with self.assertRaisesRegex(ValueError, "runtime identity drifted"):
            summarize_synchronized_phase_capture(capture, **wrong_runtime)

        noncanonical = dict(inputs)
        noncanonical["telemetry_receipt_artifact"] = (
            inputs["telemetry_receipt_artifact"] + b"\n"  # type: ignore[operator]
        )
        with self.assertRaisesRegex(ValueError, "not canonical"):
            summarize_synchronized_phase_capture(capture, **noncanonical)


if __name__ == "__main__":
    unittest.main()
