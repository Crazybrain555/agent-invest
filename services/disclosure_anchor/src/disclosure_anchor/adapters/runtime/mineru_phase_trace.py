"""Strict reader for MinerU's content-free capacity phase trace."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Final, Iterable


PHASE_TRACE_PREFIX: Final = "MINERU_PHASE_TRACE "
PHASE_TRACE_SCHEMA: Final = "mineru-phase-trace.v4"
_HEX32_RE = re.compile(r"^[a-f0-9]{32}$")
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_EVENTS = frozenset({"document_start", "interval_complete", "document_end"})
_OUTCOMES = frozenset({"started", "success", "error"})
_PIPELINE_MODES = frozenset({"serial"})
_WINDOW_PHASES = frozenset(
    {
        "window_append",
        "window_layout",
        "window_postprocess",
        "window_render",
        "window_total",
        "window_vlm",
    }
)
_PHASES = frozenset({"document", "document_finalize", *_WINDOW_PHASES})
_FIELDS = frozenset(
    {
        "append_index",
        "actual_decoded_bytes",
        "backend",
        "duration_ns",
        "ended_monotonic_ns",
        "event",
        "hybrid_batch_ratio_effective",
        "hybrid_batch_ratio_ocr_override",
        "hybrid_batch_ratio_requested",
        "hybrid_layout_batch_cap",
        "hybrid_mfr_batch_cap",
        "hybrid_ocr_det_batch_cap",
        "hybrid_table_orientation_batch_cap",
        "inner_inference_concurrency",
        "max_resident_decoded_bytes",
        "max_resident_pages",
        "max_resident_windows",
        "outcome",
        "page_count",
        "page_end_exclusive",
        "page_start",
        "phase",
        "pipeline_depth",
        "pipeline_mode",
        "process_epoch",
        "profile_id",
        "profile_sha256",
        "reserved_decoded_bytes",
        "reserved_windows",
        "resident_decoded_bytes_after_acquire",
        "resident_pages_after_acquire",
        "resident_windows_after_acquire",
        "schema",
        "sequence",
        "started_monotonic_ns",
        "source_pdf_bytes",
        "total_windows",
        "trace_id",
        "window_index",
        "window_page_count",
        "window_size",
        "vllm_max_num_seqs",
    }
)


@dataclass(frozen=True, slots=True)
class MineruPhaseEvent:
    append_index: int | None
    actual_decoded_bytes: int | None
    backend: str
    duration_ns: int
    ended_monotonic_ns: int
    event: str
    hybrid_batch_ratio_effective: int | None
    hybrid_batch_ratio_ocr_override: bool | None
    hybrid_batch_ratio_requested: int | None
    hybrid_layout_batch_cap: int | None
    hybrid_mfr_batch_cap: int | None
    hybrid_ocr_det_batch_cap: int | None
    hybrid_table_orientation_batch_cap: int | None
    inner_inference_concurrency: int
    max_resident_decoded_bytes: int
    max_resident_pages: int
    max_resident_windows: int
    outcome: str
    page_count: int
    page_end_exclusive: int | None
    page_start: int | None
    phase: str
    pipeline_depth: int
    pipeline_mode: str
    process_epoch: str
    profile_id: str
    profile_sha256: str
    reserved_decoded_bytes: int | None
    reserved_windows: int | None
    resident_decoded_bytes_after_acquire: int | None
    resident_pages_after_acquire: int | None
    resident_windows_after_acquire: int | None
    sequence: int
    started_monotonic_ns: int
    source_pdf_bytes: int
    total_windows: int
    trace_id: str
    window_index: int | None
    window_page_count: int | None
    window_size: int
    vllm_max_num_seqs: int


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"phase trace {label} is invalid")
    return value


def _nullable_integer(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label=label)


def _nullable_boolean(value: object, *, label: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f"phase trace {label} is invalid")


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"phase trace {label} is invalid")
    return value


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("phase trace JSON contains a duplicate field")
        result[key] = value
    return result


def parse_phase_trace_line(line: str) -> MineruPhaseEvent:
    """Parse one exact trace line; unknown fields fail closed."""

    if not isinstance(line, str) or not line.startswith(PHASE_TRACE_PREFIX):
        raise ValueError("phase trace prefix is invalid")
    try:
        payload = json.loads(
            line.removeprefix(PHASE_TRACE_PREFIX),
            object_pairs_hook=_unique_json_object,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("phase trace JSON is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError("phase trace fields drifted")
    if payload.get("schema") != PHASE_TRACE_SCHEMA:
        raise ValueError("phase trace schema drifted")

    backend = _text(payload.get("backend"), label="backend")
    event = _text(payload.get("event"), label="event")
    outcome = _text(payload.get("outcome"), label="outcome")
    phase = _text(payload.get("phase"), label="phase")
    pipeline_mode = _text(payload.get("pipeline_mode"), label="pipeline_mode")
    process_epoch = _text(payload.get("process_epoch"), label="process_epoch")
    profile_id = _text(payload.get("profile_id"), label="profile_id")
    profile_sha256 = _text(payload.get("profile_sha256"), label="profile_sha256")
    trace_id = _text(payload.get("trace_id"), label="trace_id")
    if backend not in {"hybrid", "vlm"}:
        raise ValueError("phase trace backend drifted")
    if (
        event not in _EVENTS
        or outcome not in _OUTCOMES
        or phase not in _PHASES
        or pipeline_mode not in _PIPELINE_MODES
    ):
        raise ValueError("phase trace vocabulary drifted")
    if (
        _HEX32_RE.fullmatch(process_epoch) is None
        or _HEX32_RE.fullmatch(trace_id) is None
        or _PROFILE_ID_RE.fullmatch(profile_id) is None
        or _SHA256_RE.fullmatch(profile_sha256) is None
    ):
        raise ValueError("phase trace opaque identity is invalid")

    started_monotonic_ns = _integer(
        payload.get("started_monotonic_ns"),
        label="started_monotonic_ns",
        minimum=1,
    )
    ended_monotonic_ns = _integer(
        payload.get("ended_monotonic_ns"),
        label="ended_monotonic_ns",
        minimum=started_monotonic_ns,
    )
    duration_ns = _integer(payload.get("duration_ns"), label="duration_ns")
    if duration_ns != ended_monotonic_ns - started_monotonic_ns:
        raise ValueError("phase trace interval duration drifted")
    page_count = _integer(payload.get("page_count"), label="page_count")
    source_pdf_bytes = _integer(
        payload.get("source_pdf_bytes"), label="source_pdf_bytes"
    )
    sequence = _integer(payload.get("sequence"), label="sequence", minimum=1)
    total_windows = _integer(payload.get("total_windows"), label="total_windows")
    window_size = _integer(payload.get("window_size"), label="window_size")
    pipeline_depth = _integer(payload.get("pipeline_depth"), label="pipeline_depth")
    max_resident_pages = _integer(
        payload.get("max_resident_pages"), label="max_resident_pages", minimum=1
    )
    max_resident_windows = _integer(
        payload.get("max_resident_windows"),
        label="max_resident_windows",
        minimum=1,
    )
    max_resident_decoded_bytes = _integer(
        payload.get("max_resident_decoded_bytes"),
        label="max_resident_decoded_bytes",
        minimum=1,
    )
    inner_inference_concurrency = _integer(
        payload.get("inner_inference_concurrency"),
        label="inner_inference_concurrency",
        minimum=1,
    )
    vllm_max_num_seqs = _integer(
        payload.get("vllm_max_num_seqs"), label="vllm_max_num_seqs", minimum=1
    )
    if vllm_max_num_seqs < inner_inference_concurrency:
        raise ValueError("phase trace VLM capacity is invalid")
    ratio_requested = _nullable_integer(
        payload.get("hybrid_batch_ratio_requested"),
        label="hybrid_batch_ratio_requested",
    )
    ratio_effective = _nullable_integer(
        payload.get("hybrid_batch_ratio_effective"),
        label="hybrid_batch_ratio_effective",
    )
    ratio_ocr_override = _nullable_boolean(
        payload.get("hybrid_batch_ratio_ocr_override"),
        label="hybrid_batch_ratio_ocr_override",
    )
    layout_cap = _nullable_integer(
        payload.get("hybrid_layout_batch_cap"),
        label="hybrid_layout_batch_cap",
    )
    mfr_cap = _nullable_integer(
        payload.get("hybrid_mfr_batch_cap"),
        label="hybrid_mfr_batch_cap",
    )
    ocr_cap = _nullable_integer(
        payload.get("hybrid_ocr_det_batch_cap"),
        label="hybrid_ocr_det_batch_cap",
    )
    orientation_cap = _nullable_integer(
        payload.get("hybrid_table_orientation_batch_cap"),
        label="hybrid_table_orientation_batch_cap",
    )
    ratio_fields = (
        ratio_requested,
        ratio_effective,
        ratio_ocr_override,
        layout_cap,
        mfr_cap,
        ocr_cap,
        orientation_cap,
    )
    if backend == "hybrid":
        if (
            ratio_requested not in {1, 2, 4, 8}
            or ratio_effective not in {1, 2, 4, 8}
            or not isinstance(ratio_ocr_override, bool)
            or layout_cap != min(8, ratio_effective)
            or mfr_cap != ratio_effective * 16
            or ocr_cap != ratio_effective * 8
            or orientation_cap != ocr_cap
            or (ratio_ocr_override and ratio_effective != 1)
            or (not ratio_ocr_override and ratio_effective != ratio_requested)
        ):
            raise ValueError("phase trace hybrid batch ratio drifted")
    elif any(value is not None for value in ratio_fields):
        raise ValueError("VLM phase trace unexpectedly has hybrid batch fields")
    append_index = _nullable_integer(payload.get("append_index"), label="append_index")
    window_index = _nullable_integer(payload.get("window_index"), label="window_index")
    page_start = _nullable_integer(payload.get("page_start"), label="page_start")
    page_end_exclusive = _nullable_integer(
        payload.get("page_end_exclusive"), label="page_end_exclusive"
    )
    window_page_count = _nullable_integer(
        payload.get("window_page_count"), label="window_page_count"
    )
    actual_decoded_bytes = _nullable_integer(
        payload.get("actual_decoded_bytes"), label="actual_decoded_bytes"
    )
    reserved_decoded_bytes = _nullable_integer(
        payload.get("reserved_decoded_bytes"), label="reserved_decoded_bytes"
    )
    reserved_windows = _nullable_integer(
        payload.get("reserved_windows"), label="reserved_windows"
    )
    resident_decoded_bytes_after_acquire = _nullable_integer(
        payload.get("resident_decoded_bytes_after_acquire"),
        label="resident_decoded_bytes_after_acquire",
    )
    resident_pages_after_acquire = _nullable_integer(
        payload.get("resident_pages_after_acquire"),
        label="resident_pages_after_acquire",
    )
    resident_windows_after_acquire = _nullable_integer(
        payload.get("resident_windows_after_acquire"),
        label="resident_windows_after_acquire",
    )
    window_values = (
        window_index,
        page_start,
        page_end_exclusive,
        window_page_count,
    )
    if any(value is None for value in window_values) != all(
        value is None for value in window_values
    ):
        raise ValueError("phase trace window fields are partially null")
    has_window = window_index is not None
    if has_window:
        assert window_index is not None
        assert page_start is not None
        assert page_end_exclusive is not None
        assert window_page_count is not None
        if (
            window_index >= total_windows
            or not 0 <= page_start < page_end_exclusive <= page_count
            or window_page_count != page_end_exclusive - page_start
        ):
            raise ValueError("phase trace window range is invalid")

    if page_count == 0:
        if total_windows != 0 or has_window:
            raise ValueError("empty phase trace document has windows")
    elif (
        window_size <= 0
        or total_windows != (page_count + window_size - 1) // window_size
    ):
        raise ValueError("phase trace document/window dimensions drifted")

    if event == "document_start":
        if (
            phase != "document"
            or outcome != "started"
            or duration_ns != 0
            or has_window
            or append_index is not None
        ):
            raise ValueError("phase trace document-start event is invalid")
    elif event == "document_end":
        if (
            phase != "document"
            or outcome not in {"success", "error"}
            or has_window
            or append_index is not None
        ):
            raise ValueError("phase trace document-end event is invalid")
    elif (
        outcome not in {"success", "error"}
        or phase == "document"
        or (phase in _WINDOW_PHASES) != has_window
        or (append_index is not None) != (phase == "window_append")
        or (append_index is not None and append_index != window_index)
    ):
        raise ValueError("phase trace interval event is invalid")

    credit_values = (
        actual_decoded_bytes,
        reserved_decoded_bytes,
        reserved_windows,
        resident_decoded_bytes_after_acquire,
        resident_pages_after_acquire,
        resident_windows_after_acquire,
    )
    all_credit_null = all(value is None for value in credit_values)
    if (
        pipeline_depth != 0
        or max_resident_pages != window_size
        or max_resident_windows != 1
    ):
        raise ValueError("phase trace serial profile is invalid")
    if not all_credit_null:
        raise ValueError("phase trace serial event unexpectedly owns credits")

    return MineruPhaseEvent(
        append_index=append_index,
        actual_decoded_bytes=actual_decoded_bytes,
        backend=backend,
        duration_ns=duration_ns,
        ended_monotonic_ns=ended_monotonic_ns,
        event=event,
        hybrid_batch_ratio_effective=ratio_effective,
        hybrid_batch_ratio_ocr_override=ratio_ocr_override,
        hybrid_batch_ratio_requested=ratio_requested,
        hybrid_layout_batch_cap=layout_cap,
        hybrid_mfr_batch_cap=mfr_cap,
        hybrid_ocr_det_batch_cap=ocr_cap,
        hybrid_table_orientation_batch_cap=orientation_cap,
        inner_inference_concurrency=inner_inference_concurrency,
        max_resident_decoded_bytes=max_resident_decoded_bytes,
        max_resident_pages=max_resident_pages,
        max_resident_windows=max_resident_windows,
        outcome=outcome,
        page_count=page_count,
        page_end_exclusive=page_end_exclusive,
        page_start=page_start,
        phase=phase,
        pipeline_depth=pipeline_depth,
        pipeline_mode=pipeline_mode,
        process_epoch=process_epoch,
        profile_id=profile_id,
        profile_sha256=profile_sha256,
        reserved_decoded_bytes=reserved_decoded_bytes,
        reserved_windows=reserved_windows,
        resident_decoded_bytes_after_acquire=resident_decoded_bytes_after_acquire,
        resident_pages_after_acquire=resident_pages_after_acquire,
        resident_windows_after_acquire=resident_windows_after_acquire,
        sequence=sequence,
        started_monotonic_ns=started_monotonic_ns,
        source_pdf_bytes=source_pdf_bytes,
        total_windows=total_windows,
        trace_id=trace_id,
        window_index=window_index,
        window_page_count=window_page_count,
        window_size=window_size,
        vllm_max_num_seqs=vllm_max_num_seqs,
    )


def _assert_non_overlapping(events: Iterable[MineruPhaseEvent]) -> None:
    intervals = sorted(
        events,
        key=lambda event: (
            event.started_monotonic_ns,
            event.ended_monotonic_ns,
            event.sequence,
        ),
    )
    if any(
        later.started_monotonic_ns < earlier.ended_monotonic_ns
        for earlier, later in zip(intervals, intervals[1:])
    ):
        raise ValueError("phase trace native-owner intervals overlap")


def validate_complete_phase_trace(
    events: Iterable[MineruPhaseEvent],
    *,
    expected_profile_sha256: str,
    require_pipeline_overlap: bool = False,
) -> tuple[MineruPhaseEvent, ...]:
    """Prove one successful trace is a closed, ordered interval DAG."""

    ordered = tuple(events)
    if len(ordered) < 3:
        raise ValueError("phase trace is incomplete")
    if _SHA256_RE.fullmatch(expected_profile_sha256) is None:
        raise ValueError("expected phase trace profile hash is invalid")
    first, last = ordered[0], ordered[-1]
    if first.profile_sha256 != expected_profile_sha256:
        raise ValueError("phase trace profile hash does not match runtime attestation")
    identity = (
        first.process_epoch,
        first.trace_id,
        first.backend,
        first.pipeline_mode,
        first.pipeline_depth,
        first.profile_id,
        first.profile_sha256,
        first.page_count,
        first.source_pdf_bytes,
        first.window_size,
        first.total_windows,
        first.max_resident_pages,
        first.max_resident_windows,
        first.max_resident_decoded_bytes,
        first.inner_inference_concurrency,
        first.vllm_max_num_seqs,
        first.hybrid_batch_ratio_requested,
        first.hybrid_batch_ratio_effective,
        first.hybrid_batch_ratio_ocr_override,
        first.hybrid_layout_batch_cap,
        first.hybrid_mfr_batch_cap,
        first.hybrid_ocr_det_batch_cap,
        first.hybrid_table_orientation_batch_cap,
    )
    if any(
        (
            event.process_epoch,
            event.trace_id,
            event.backend,
            event.pipeline_mode,
            event.pipeline_depth,
            event.profile_id,
            event.profile_sha256,
            event.page_count,
            event.source_pdf_bytes,
            event.window_size,
            event.total_windows,
            event.max_resident_pages,
            event.max_resident_windows,
            event.max_resident_decoded_bytes,
            event.inner_inference_concurrency,
            event.vllm_max_num_seqs,
            event.hybrid_batch_ratio_requested,
            event.hybrid_batch_ratio_effective,
            event.hybrid_batch_ratio_ocr_override,
            event.hybrid_layout_batch_cap,
            event.hybrid_mfr_batch_cap,
            event.hybrid_ocr_det_batch_cap,
            event.hybrid_table_orientation_batch_cap,
        )
        != identity
        for event in ordered
    ):
        raise ValueError("phase trace identity drifted within document")
    if [event.sequence for event in ordered] != list(range(1, len(ordered) + 1)):
        raise ValueError("phase trace sequence has a gap")
    if any(
        later.ended_monotonic_ns < earlier.ended_monotonic_ns
        for earlier, later in zip(ordered, ordered[1:])
    ):
        raise ValueError("phase trace emission order regressed")
    if (
        first.event != "document_start"
        or last.event != "document_end"
        or last.outcome != "success"
        or any(event.outcome == "error" for event in ordered)
        or last.started_monotonic_ns != first.started_monotonic_ns
    ):
        raise ValueError("phase trace document closure failed")
    if any(
        event.started_monotonic_ns < first.started_monotonic_ns
        or event.ended_monotonic_ns > last.ended_monotonic_ns
        for event in ordered[1:-1]
    ):
        raise ValueError("phase trace interval escapes the document")

    completions = ordered[1:-1]
    finalize = [event for event in completions if event.phase == "document_finalize"]
    if len(finalize) != 1:
        raise ValueError("phase trace finalize closure failed")
    expected_window_phases = {
        "window_append",
        "window_render",
        "window_total",
        "window_vlm",
    }
    if first.backend == "hybrid":
        expected_window_phases.update({"window_layout", "window_postprocess"})
    by_window: dict[int, dict[str, MineruPhaseEvent]] = {}
    for event in completions:
        if event.window_index is None:
            continue
        phases = by_window.setdefault(event.window_index, {})
        if event.phase in phases:
            raise ValueError("phase trace window phase is duplicated")
        phases[event.phase] = event
    if set(by_window) != set(range(first.total_windows)):
        raise ValueError("phase trace window indexes are incomplete")

    expected_page_start = 0
    totals: list[MineruPhaseEvent] = []
    renders: list[MineruPhaseEvent] = []
    vlms: list[MineruPhaseEvent] = []
    postprocesses: list[MineruPhaseEvent] = []
    appends: list[MineruPhaseEvent] = []
    native_intervals: list[MineruPhaseEvent] = []
    for window_index in range(first.total_windows):
        phases = by_window[window_index]
        if set(phases) != expected_window_phases:
            raise ValueError("phase trace window phases are incomplete")
        ranges = {
            (event.page_start, event.page_end_exclusive)
            for event in phases.values()
        }
        if len(ranges) != 1:
            raise ValueError("phase trace window range drifted")
        page_start, page_end_exclusive = ranges.pop()
        assert page_start is not None
        assert page_end_exclusive is not None
        if page_start != expected_page_start:
            raise ValueError("phase trace page coverage has a gap")
        expected_page_start = page_end_exclusive

        total = phases["window_total"]
        render = phases["window_render"]
        vlm = phases["window_vlm"]
        append = phases["window_append"]
        children = [event for phase, event in phases.items() if phase != "window_total"]
        if any(
            event.started_monotonic_ns < total.started_monotonic_ns
            or event.ended_monotonic_ns > total.ended_monotonic_ns
            for event in children
        ):
            raise ValueError("phase trace child interval escapes its window")
        if first.backend == "hybrid":
            layout = phases["window_layout"]
            postprocess = phases["window_postprocess"]
            if not (
                render.ended_monotonic_ns <= layout.started_monotonic_ns
                and layout.ended_monotonic_ns <= vlm.started_monotonic_ns
                and vlm.ended_monotonic_ns <= postprocess.started_monotonic_ns
                and postprocess.ended_monotonic_ns <= append.started_monotonic_ns
            ):
                raise ValueError("phase trace hybrid window DAG is invalid")
            postprocesses.append(postprocess)
            native_intervals.extend((render, layout, postprocess))
        elif not (
            render.ended_monotonic_ns <= vlm.started_monotonic_ns
            and vlm.ended_monotonic_ns <= append.started_monotonic_ns
        ):
            raise ValueError("phase trace VLM window DAG is invalid")
        totals.append(total)
        renders.append(render)
        vlms.append(vlm)
        appends.append(append)

    if expected_page_start != first.page_count:
        raise ValueError("phase trace page coverage is incomplete")
    if [event.append_index for event in sorted(appends, key=lambda event: event.sequence)] != list(
        range(first.total_windows)
    ):
        raise ValueError("phase trace append order drifted")
    if finalize[0].started_monotonic_ns < max(
        (event.ended_monotonic_ns for event in totals),
        default=first.started_monotonic_ns,
    ):
        raise ValueError("phase trace finalize overlaps window ownership")

    if require_pipeline_overlap:
        raise ValueError("serial phase trace cannot require pipeline overlap")
    if any(
        earlier.ended_monotonic_ns > later.started_monotonic_ns
        for earlier, later in zip(totals, totals[1:])
    ):
        raise ValueError("phase trace serial windows overlap")
    return ordered


def _overlap_ns(left: MineruPhaseEvent, right: MineruPhaseEvent) -> int:
    return max(
        0,
        min(left.ended_monotonic_ns, right.ended_monotonic_ns)
        - max(left.started_monotonic_ns, right.started_monotonic_ns),
    )


def summarize_complete_phase_trace(
    events: Iterable[MineruPhaseEvent],
    *,
    expected_profile_sha256: str,
    require_pipeline_overlap: bool = False,
) -> dict[str, object]:
    """Reduce one validated content-free trace to commissioning metrics."""

    ordered = validate_complete_phase_trace(
        events,
        expected_profile_sha256=expected_profile_sha256,
        require_pipeline_overlap=require_pipeline_overlap,
    )
    first, last = ordered[0], ordered[-1]
    completions = ordered[1:-1]
    by_phase: dict[str, list[MineruPhaseEvent]] = {}
    by_window: dict[int, dict[str, MineruPhaseEvent]] = {}
    for event in completions:
        by_phase.setdefault(event.phase, []).append(event)
        if event.window_index is not None:
            by_window.setdefault(event.window_index, {})[event.phase] = event

    document_duration_ns = last.ended_monotonic_ns - first.started_monotonic_ns
    if document_duration_ns <= 0:
        raise ValueError("phase trace document duration is empty")
    vlms = [by_window[index]["window_vlm"] for index in range(first.total_windows)]
    vlm_active_ns = sum(event.duration_ns for event in vlms)
    vlm_supply_gap_ns = sum(
        max(0, later.started_monotonic_ns - earlier.ended_monotonic_ns)
        for earlier, later in zip(vlms, vlms[1:])
    )
    a_b_overlap_ns = 0
    b_c_overlap_ns = 0

    phase_duration_ns = {
        phase: sum(event.duration_ns for event in phase_events)
        for phase, phase_events in sorted(by_phase.items())
    }
    actual_decoded = [
        event.actual_decoded_bytes
        for event in completions
        if event.actual_decoded_bytes is not None
    ]
    resident_pages = [
        event.resident_pages_after_acquire
        for event in completions
        if event.resident_pages_after_acquire is not None
    ]
    resident_decoded = [
        event.resident_decoded_bytes_after_acquire
        for event in completions
        if event.resident_decoded_bytes_after_acquire is not None
    ]
    return {
        "a_b_overlap_ns": a_b_overlap_ns,
        "b_c_overlap_ns": b_c_overlap_ns,
        "document_duration_ns": document_duration_ns,
        "inner_inference_concurrency": first.inner_inference_concurrency,
        "max_actual_decoded_bytes": max(actual_decoded, default=0),
        "max_observed_resident_decoded_bytes": max(resident_decoded, default=0),
        "max_observed_resident_pages": max(resident_pages, default=0),
        "page_count": first.page_count,
        "pages_per_host_hour_milli": (
            first.page_count * 3_600 * 1_000_000_000 * 1_000
            // document_duration_ns
        ),
        "phase_duration_ns": phase_duration_ns,
        "pipeline_mode": first.pipeline_mode,
        "profile_id": first.profile_id,
        "profile_sha256": first.profile_sha256,
        "schema": "mineru-phase-trace-summary.v2",
        "source_pdf_bytes": first.source_pdf_bytes,
        "total_windows": first.total_windows,
        "vlm_active_ns": vlm_active_ns,
        "vlm_document_duty_ppm": vlm_active_ns * 1_000_000 // document_duration_ns,
        "vlm_supply_gap_ns": vlm_supply_gap_ns,
        "vllm_max_num_seqs": first.vllm_max_num_seqs,
        "window_size": first.window_size,
    }


__all__ = [
    "MineruPhaseEvent",
    "PHASE_TRACE_PREFIX",
    "PHASE_TRACE_SCHEMA",
    "parse_phase_trace_line",
    "summarize_complete_phase_trace",
    "validate_complete_phase_trace",
]
