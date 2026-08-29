"""Strict, content-free reader for bounded Windows MinerU trace captures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import PureWindowsPath
import re
from typing import Final, Iterable

from pydantic import TypeAdapter

from disclosure_anchor.adapters.runtime.mineru_phase_trace import (
    MineruPhaseEvent,
    parse_phase_trace_line,
    summarize_complete_phase_trace,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    BlockedProgressEvent,
    CapacityProgressEventEnvelope,
    DurablePageCommitEvent,
    PhaseClockBinding,
    ProgressEvent,
    SynchronizedPhaseSummary,
    SynchronizedTelemetryFrame,
    SynchronizedTelemetryReceipt,
    canonical_json_artifact_sha256,
    parse_canonical_json_artifact,
    validate_telemetry_artifact_hashes,
    validate_frame_sequence,
)


PHASE_TRACE_CAPTURE_SCHEMA: Final = "mineru-phase-trace-capture.v1"
PHASE_TRACE_CAPTURE_SUMMARY_SCHEMA: Final = "mineru-phase-trace-capture-summary.v1"
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_CONTAINER_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_MAX_TRACE_LINES = 200_000
_MAX_TRACE_BYTES = 268_435_456
_MAX_TRACE_LINE_BYTES = 8_192
_CAPTURE_FIELDS = frozenset(
    {
        "active_profile_sha256",
        "capacity_mode",
        "collected_at_utc",
        "collector_path",
        "collector_sha256",
        "container",
        "line_count",
        "lines",
        "schema",
        "since_utc",
        "trace_bytes",
        "trace_lines_sha256",
        "until_utc",
        "windows_node_identity_sha256",
    }
)
_CONTAINER_FIELDS = frozenset(
    {
        "health",
        "id",
        "image",
        "image_id",
        "name",
        "oom_killed",
        "restart_count",
        "running",
        "started_at_utc",
        "status",
    }
)


@dataclass(frozen=True, slots=True)
class MineruPhaseTraceCapture:
    active_profile_sha256: str
    capacity_mode: str
    collected_at_utc: str
    collector_path: str
    collector_sha256: str
    container_id: str
    container_image: str
    container_image_id: str
    container_started_at_utc: str
    line_count: int
    lines_sha256: str
    since_utc: str
    trace_bytes: int
    until_utc: str
    windows_node_identity_sha256: str
    events: tuple[MineruPhaseEvent, ...]


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("phase trace capture JSON contains a duplicate field")
        result[key] = value
    return result


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"phase trace capture {label} is invalid")
    return value


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"phase trace capture {label} is invalid")
    return value


def _summary_integer(value: dict[str, object], field: str) -> int:
    return _integer(value.get(field), label=f"summary.{field}")


def _utc(value: object, *, label: str) -> datetime:
    text = _text(value, label=label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"phase trace capture {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"phase trace capture {label} is not UTC")
    return parsed


def parse_phase_trace_capture(payload: str | bytes) -> MineruPhaseTraceCapture:
    """Parse one self-contained capture; external runtime binding happens later."""

    if isinstance(payload, bytes):
        if len(payload) > _MAX_TRACE_BYTES:
            raise ValueError("phase trace capture payload exceeds its bound")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("phase trace capture is not UTF-8") from exc
    elif isinstance(payload, str):
        text = payload
        if len(text.encode("utf-8")) > _MAX_TRACE_BYTES:
            raise ValueError("phase trace capture payload exceeds its bound")
    else:
        raise ValueError("phase trace capture payload is invalid")
    try:
        value = json.loads(text, object_pairs_hook=_unique_json_object)
    except json.JSONDecodeError as exc:
        raise ValueError("phase trace capture JSON is invalid") from exc
    if not isinstance(value, dict) or set(value) != _CAPTURE_FIELDS:
        raise ValueError("phase trace capture fields drifted")
    if value.get("schema") != PHASE_TRACE_CAPTURE_SCHEMA:
        raise ValueError("phase trace capture schema drifted")

    capacity_mode = _text(value.get("capacity_mode"), label="capacity_mode")
    if capacity_mode not in {"legacy", "candidate"}:
        raise ValueError("phase trace capture capacity mode is invalid")
    active_profile_sha256 = _text(
        value.get("active_profile_sha256"), label="active_profile_sha256"
    )
    collector_sha256 = _text(value.get("collector_sha256"), label="collector_sha256")
    node_sha256 = _text(
        value.get("windows_node_identity_sha256"),
        label="windows_node_identity_sha256",
    )
    lines_sha256 = _text(
        value.get("trace_lines_sha256"), label="trace_lines_sha256"
    )
    if any(
        _SHA256_RE.fullmatch(item) is None
        for item in (
            active_profile_sha256,
            collector_sha256,
            node_sha256,
            lines_sha256,
        )
    ):
        raise ValueError("phase trace capture hash identity is invalid")

    collector_path = _text(value.get("collector_path"), label="collector_path")
    collector_windows_path = PureWindowsPath(collector_path)
    if (
        not collector_windows_path.is_absolute()
        or collector_windows_path.drive.casefold() != "c:"
        or ".." in collector_windows_path.parts
    ):
        raise ValueError("phase trace capture collector path is invalid")

    since = _utc(value.get("since_utc"), label="since_utc")
    until = _utc(value.get("until_utc"), label="until_utc")
    collected = _utc(value.get("collected_at_utc"), label="collected_at_utc")
    if not since < until <= collected or (until - since).total_seconds() > 21_600:
        raise ValueError("phase trace capture time bounds are invalid")

    container = value.get("container")
    if not isinstance(container, dict) or set(container) != _CONTAINER_FIELDS:
        raise ValueError("phase trace capture container fields drifted")
    container_id = _text(container.get("id"), label="container.id")
    container_image = _text(container.get("image"), label="container.image")
    container_image_id = _text(container.get("image_id"), label="container.image_id")
    container_started = _utc(
        container.get("started_at_utc"), label="container.started_at_utc"
    )
    if (
        container.get("name") != "mineru-api"
        or _CONTAINER_ID_RE.fullmatch(container_id) is None
        or _SHA256_RE.fullmatch(container_image_id) is None
        or container.get("restart_count") != 0
        or container.get("oom_killed") is not False
        or container.get("running") is not True
        or container.get("status") != "running"
        or container.get("health") != "healthy"
        or container_started > since
    ):
        raise ValueError("phase trace capture container is not a clean source")

    line_count = _integer(value.get("line_count"), label="line_count", minimum=1)
    trace_bytes = _integer(value.get("trace_bytes"), label="trace_bytes", minimum=1)
    lines = value.get("lines")
    if (
        not isinstance(lines, list)
        or not 1 <= len(lines) <= _MAX_TRACE_LINES
        or line_count != len(lines)
        or not all(isinstance(line, str) for line in lines)
    ):
        raise ValueError("phase trace capture lines are invalid")
    trace_text = "".join(f"{line}\n" for line in lines)
    encoded_trace = trace_text.encode("utf-8")
    if (
        any(len(line.encode("utf-8")) > _MAX_TRACE_LINE_BYTES for line in lines)
        or len(encoded_trace) != trace_bytes
        or trace_bytes > _MAX_TRACE_BYTES
        or _sha256_text(trace_text) != lines_sha256
    ):
        raise ValueError("phase trace capture byte evidence drifted")

    events = tuple(parse_phase_trace_line(line) for line in lines)
    if any(event.profile_sha256 != active_profile_sha256 for event in events):
        raise ValueError("phase trace capture event profile drifted")
    process_epochs = {event.process_epoch for event in events}
    if len(process_epochs) != 1:
        raise ValueError("phase trace capture spans multiple process epochs")

    return MineruPhaseTraceCapture(
        active_profile_sha256=active_profile_sha256,
        capacity_mode=capacity_mode,
        collected_at_utc=str(value["collected_at_utc"]),
        collector_path=collector_path,
        collector_sha256=collector_sha256,
        container_id=container_id,
        container_image=container_image,
        container_image_id=container_image_id,
        container_started_at_utc=str(container["started_at_utc"]),
        line_count=line_count,
        lines_sha256=lines_sha256,
        since_utc=str(value["since_utc"]),
        trace_bytes=trace_bytes,
        until_utc=str(value["until_utc"]),
        windows_node_identity_sha256=node_sha256,
        events=events,
    )


def _group_by_trace(
    events: Iterable[MineruPhaseEvent],
) -> tuple[tuple[MineruPhaseEvent, ...], ...]:
    grouped: dict[tuple[str, str], list[MineruPhaseEvent]] = {}
    for event in events:
        grouped.setdefault((event.process_epoch, event.trace_id), []).append(event)
    ordered = [
        tuple(sorted(trace_events, key=lambda event: event.sequence))
        for trace_events in grouped.values()
    ]
    return tuple(
        sorted(
            ordered,
            key=lambda trace_events: (
                trace_events[0].started_monotonic_ns,
                trace_events[0].trace_id,
            ),
        )
    )


def summarize_phase_trace_capture(
    capture: MineruPhaseTraceCapture,
    *,
    expected_profile_sha256: str,
    expected_capacity_mode: str,
    expected_collector_sha256: str,
    expected_windows_node_identity_sha256: str,
    expected_container_id: str,
    require_pipeline_overlap: bool,
) -> dict[str, object]:
    """Bind a capture to attested runtime identity and close every document DAG."""

    if (
        _SHA256_RE.fullmatch(expected_profile_sha256) is None
        or _SHA256_RE.fullmatch(expected_collector_sha256) is None
        or _SHA256_RE.fullmatch(expected_windows_node_identity_sha256) is None
        or _CONTAINER_ID_RE.fullmatch(expected_container_id) is None
        or expected_capacity_mode not in {"legacy", "candidate"}
    ):
        raise ValueError("expected phase trace capture identity is invalid")
    if (
        capture.active_profile_sha256 != expected_profile_sha256
        or capture.capacity_mode != expected_capacity_mode
        or capture.collector_sha256 != expected_collector_sha256
        or capture.windows_node_identity_sha256
        != expected_windows_node_identity_sha256
        or capture.container_id != expected_container_id
    ):
        raise ValueError("phase trace capture does not match runtime attestation")

    traces = _group_by_trace(capture.events)
    if not traces:
        raise ValueError("phase trace capture contains no document")
    documents = [
        summarize_complete_phase_trace(
            trace,
            expected_profile_sha256=expected_profile_sha256,
            require_pipeline_overlap=require_pipeline_overlap,
        )
        for trace in traces
    ]
    if expected_capacity_mode == "legacy" and any(
        document["pipeline_mode"] != "legacy" for document in documents
    ):
        raise ValueError("legacy capture contains a candidate trace")
    if expected_capacity_mode == "candidate" and any(
        document["pipeline_mode"] != "depth1" for document in documents
    ):
        raise ValueError("candidate capture contains a fallback trace")

    total_duration_ns = sum(
        _summary_integer(item, "document_duration_ns") for item in documents
    )
    total_vlm_active_ns = sum(
        _summary_integer(item, "vlm_active_ns") for item in documents
    )
    return {
        "active_profile_sha256": capture.active_profile_sha256,
        "a_b_overlap_ns": sum(
            _summary_integer(item, "a_b_overlap_ns") for item in documents
        ),
        "b_c_overlap_ns": sum(
            _summary_integer(item, "b_c_overlap_ns") for item in documents
        ),
        "capacity_mode": capture.capacity_mode,
        "collector_sha256": capture.collector_sha256,
        "container_id": capture.container_id,
        "document_count": len(documents),
        "documents": documents,
        "line_count": capture.line_count,
        "max_actual_decoded_bytes": max(
            _summary_integer(item, "max_actual_decoded_bytes")
            for item in documents
        ),
        "max_observed_resident_decoded_bytes": max(
            _summary_integer(item, "max_observed_resident_decoded_bytes")
            for item in documents
        ),
        "max_observed_resident_pages": max(
            _summary_integer(item, "max_observed_resident_pages")
            for item in documents
        ),
        "page_count": sum(
            _summary_integer(item, "page_count") for item in documents
        ),
        "schema": PHASE_TRACE_CAPTURE_SUMMARY_SCHEMA,
        "total_document_duration_ns": total_duration_ns,
        "trace_lines_sha256": capture.lines_sha256,
        "vlm_active_ns": total_vlm_active_ns,
        "vlm_document_duty_ppm": (
            total_vlm_active_ns * 1_000_000 // total_duration_ns
        ),
        "vlm_supply_gap_ns": sum(
            _summary_integer(item, "vlm_supply_gap_ns") for item in documents
        ),
        "windows_node_identity_sha256": capture.windows_node_identity_sha256,
    }


def summarize_synchronized_phase_capture(
    capture: MineruPhaseTraceCapture,
    *,
    telemetry_receipt: SynchronizedTelemetryReceipt,
    telemetry_frames: tuple[SynchronizedTelemetryFrame, ...],
    progress_events: tuple[ProgressEvent, ...],
    phase_capture_artifact: bytes,
    telemetry_receipt_artifact: bytes,
    telemetry_frames_artifact: bytes,
    progress_events_artifact: bytes,
    phase_clock_binding_artifact: bytes,
    vector_events_artifact: bytes | None = None,
) -> SynchronizedPhaseSummary:
    """Bind one complete phase capture to independently sampled host telemetry."""

    parse_canonical_json_artifact(
        phase_capture_artifact, label="phase capture"
    )
    parsed_capture = parse_phase_trace_capture(phase_capture_artifact)
    if parsed_capture != capture:
        raise ValueError("phase capture artifact differs from parsed capture")
    receipt_value = parse_canonical_json_artifact(
        telemetry_receipt_artifact, label="telemetry receipt"
    )
    parsed_receipt = SynchronizedTelemetryReceipt.model_validate(receipt_value)
    if parsed_receipt != telemetry_receipt:
        raise ValueError("telemetry receipt artifact differs from parsed receipt")
    frames_value = parse_canonical_json_artifact(
        telemetry_frames_artifact, label="telemetry frames"
    )
    parsed_frames = tuple(
        TypeAdapter(list[SynchronizedTelemetryFrame]).validate_python(frames_value)
    )
    if parsed_frames != telemetry_frames:
        raise ValueError("telemetry frames artifact differs from parsed frames")
    progress_value = parse_canonical_json_artifact(
        progress_events_artifact, label="progress events"
    )
    parsed_progress = tuple(
        CapacityProgressEventEnvelope.model_validate(item).root
        for item in TypeAdapter(list[object]).validate_python(progress_value)
    )
    if parsed_progress != progress_events:
        raise ValueError("progress artifact differs from parsed events")
    binding_value = parse_canonical_json_artifact(
        phase_clock_binding_artifact, label="phase clock binding"
    )
    phase_clock_binding = PhaseClockBinding.model_validate(binding_value)
    phase_capture_sha256 = canonical_json_artifact_sha256(
        phase_capture_artifact, label="phase capture"
    )
    telemetry_receipt_sha256 = canonical_json_artifact_sha256(
        telemetry_receipt_artifact, label="telemetry receipt"
    )
    validate_telemetry_artifact_hashes(
        telemetry_receipt,
        frames_artifact=telemetry_frames_artifact,
        progress_events_artifact=progress_events_artifact,
        vector_events_artifact=vector_events_artifact,
        phase_capture_artifact=phase_capture_artifact,
        phase_clock_binding_artifact=phase_clock_binding_artifact,
    )
    validate_frame_sequence(telemetry_frames, receipt=telemetry_receipt)
    if not capture.events:
        raise ValueError("phase trace capture contains no event")
    process_epochs = {event.process_epoch for event in capture.events}
    if process_epochs != {phase_clock_binding.phase_process_epoch}:
        raise ValueError("phase trace clock binding process epoch drifted")
    if (
        phase_clock_binding.runtime_bundle_identity_sha256
        != telemetry_receipt.runtime_bundle_identity_sha256
        or phase_clock_binding.observer_process_epoch_sha256
        != telemetry_receipt.process_profile.process_epoch_sha256
        or phase_clock_binding.container_id != capture.container_id
        or phase_clock_binding.container_started_at_utc.isoformat()
        != capture.container_started_at_utc
        or phase_clock_binding.phase_node_identity_sha256
        != capture.windows_node_identity_sha256
    ):
        raise ValueError("phase clock binding runtime identity drifted")
    if (
        phase_clock_binding.clock_domain_identity_sha256
        != telemetry_receipt.clock_domain_identity_sha256
    ):
        raise ValueError("phase and telemetry clocks are not comparable")
    phase_start = min(event.started_monotonic_ns for event in capture.events)
    phase_finish = max(event.ended_monotonic_ns for event in capture.events)
    in_phase = tuple(
        frame
        for frame in telemetry_frames
        if phase_start
        <= frame.clock.started_monotonic_ns
        <= frame.clock.finished_monotonic_ns
        <= phase_finish
    )
    gpu_count = sum(frame.lane == "gpu_fast" for frame in in_phase)
    host_count = sum(frame.lane == "host_slow" for frame in in_phase)
    if gpu_count == 0 or host_count == 0:
        raise ValueError("phase interval lacks synchronized telemetry coverage")
    blocked_duration = 0
    committed_pages = 0
    previous_progress_monotonic_ns = -1
    previous_blocked_finish_ns = -1
    cumulative_unique_source_pages = 0
    seen_source_identities: set[str] = set()
    for expected_sequence, event in enumerate(progress_events):
        if event.run_id != telemetry_receipt.run_id:
            raise ValueError("progress event run identity drifted")
        if (
            event.process_epoch_sha256
            != telemetry_receipt.process_profile.process_epoch_sha256
            or event.process_profile_sha256
            != telemetry_receipt.process_profile.process_profile_sha256
        ):
            raise ValueError("progress event process/profile identity drifted")
        if (
            event.clock_domain_identity_sha256
            != telemetry_receipt.clock_domain_identity_sha256
        ):
            raise ValueError("progress and phase clocks are not comparable")
        if event.sequence != expected_sequence:
            raise ValueError("progress event sequence is not contiguous")
        if event.monotonic_ns < previous_progress_monotonic_ns:
            raise ValueError("progress event monotonic order drifted")
        previous_progress_monotonic_ns = event.monotonic_ns
        if isinstance(event, BlockedProgressEvent):
            if (
                event.blocked_interval_started_monotonic_ns
                < previous_blocked_finish_ns
            ):
                raise ValueError("blocked progress intervals overlap")
            previous_blocked_finish_ns = event.monotonic_ns
            overlaps_phase = (
                event.monotonic_ns >= phase_start
                and event.blocked_interval_started_monotonic_ns <= phase_finish
            )
            inside_phase = (
                phase_start <= event.blocked_interval_started_monotonic_ns
                and event.monotonic_ns <= phase_finish
            )
            if overlaps_phase and not inside_phase:
                raise ValueError("blocked progress interval crosses phase boundary")
            if inside_phase:
                blocked_duration += event.blocked_duration_ns
        elif isinstance(event, DurablePageCommitEvent):
            if event.source_identity_sha256 in seen_source_identities:
                raise ValueError("durable source identity repeats within run")
            seen_source_identities.add(event.source_identity_sha256)
            cumulative_unique_source_pages += event.committed_source_pages
            if (
                event.cumulative_unique_source_pages
                != cumulative_unique_source_pages
            ):
                raise ValueError("durable page cumulative count drifted")
            if phase_start <= event.monotonic_ns <= phase_finish:
                committed_pages += event.committed_source_pages
    return SynchronizedPhaseSummary(
        runtime_bundle_identity_sha256=(
            telemetry_receipt.runtime_bundle_identity_sha256
        ),
        process_profile_sha256=(
            telemetry_receipt.process_profile.process_profile_sha256
        ),
        clock_domain_identity_sha256=(
            telemetry_receipt.clock_domain_identity_sha256
        ),
        phase_capture_sha256=phase_capture_sha256,
        telemetry_receipt_sha256=telemetry_receipt_sha256,
        phase_started_monotonic_ns=phase_start,
        phase_finished_monotonic_ns=phase_finish,
        telemetry_started_monotonic_ns=telemetry_receipt.started_monotonic_ns,
        telemetry_finished_monotonic_ns=telemetry_receipt.finished_monotonic_ns,
        gpu_fast_samples_in_phase=gpu_count,
        host_slow_samples_in_phase=host_count,
        blocked_duration_ns=blocked_duration,
        unique_durable_source_pages_committed=committed_pages,
    )


__all__ = [
    "MineruPhaseTraceCapture",
    "PHASE_TRACE_CAPTURE_SCHEMA",
    "PHASE_TRACE_CAPTURE_SUMMARY_SCHEMA",
    "parse_phase_trace_capture",
    "summarize_phase_trace_capture",
    "summarize_synchronized_phase_capture",
]
