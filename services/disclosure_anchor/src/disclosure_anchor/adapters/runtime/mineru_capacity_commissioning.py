"""Fail-closed A-B-B-A commissioning for one immutable MinerU profile."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import json
import math
import re
from typing import Any, Final, Mapping, Sequence
import uuid

from disclosure_anchor.adapters.runtime.mineru_phase_trace_capture import (
    MineruPhaseTraceCapture,
    summarize_phase_trace_capture,
)
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    MinerUDeploymentGateError,
    staged_load_metrics_are_proved,
    verify_host_capacity_evidence,
    verify_staged_load_admission_evidence,
    verify_staged_load_inference_liveness_evidence,
    verify_staged_load_orchestrator_evidence,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_STAGED_LOAD_MINIMUM_RUNAWAY_TIMEOUT_SECONDS,
    MINERU_WINDOWS_COLLECTOR_PATH,
)


CAPACITY_COMMISSIONING_SCHEMA: Final = "mineru-capacity-commissioning.v2"
CAPACITY_COMMISSIONING_FIELDS: Final = frozenset(
    {
        "arm_finished_at_utc",
        "arm_execution_ids",
        "arm_modes",
        "arm_pages_per_host_hour_milli",
        "arm_started_at_utc",
        "baseline_ceiling_pages_per_host_hour_milli",
        "baseline_profile_sha256",
        "baseline_repeat_spread_basis_points",
        "candidate_absolute_gain_pages_per_host_hour_milli",
        "candidate_floor_pages_per_host_hour_milli",
        "candidate_profile_sha256",
        "candidate_relative_gain_basis_points",
        "candidate_repeat_spread_basis_points",
        "collector_sha256",
        "collector_path",
        "decision",
        "docker_memory_reserve_bytes",
        "empirical_repeat_noise_pages_per_host_hour_milli",
        "findings",
        "maximum_repeat_spread_basis_points",
        "minimum_improvement_basis_points",
        "output_semantics",
        "page_count_per_arm",
        "profile_commissioning_authorized",
        "schema",
        "selection_rule",
        "windows_node_identity_sha256",
    }
)
_STAGED_LOAD_SCHEMA = "mineru_staged_load_receipt.v7"
_STAGED_LOAD_SCHEMA_VERSION = 7
_STAGED_LOAD_SAFETY_LIMITS_PROFILE = "whole-document-runaway-and-drain.v1"
_STAGE_COUNTS = (4, 8, 16)
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_TIMELINE_TOLERANCE_SECONDS = 0.050
_GRADUATION_WAITING_PEAK_EXCLUSIVE = 32.0
_GRADUATION_WAITING_P95_MAX = 10.0
_GRADUATION_KV_PEAK_EXCLUSIVE = 0.90
_GRADUATION_TERMINAL_DRAIN_SECONDS_MAX = 120.0


@dataclass(frozen=True, slots=True)
class _ArmEvidence:
    capture_summary: Mapping[str, object]
    elapsed_seconds: float
    execution_id: str
    finished_at: datetime
    identity: Mapping[str, object]
    input_evidence: Mapping[str, object]
    output_fingerprint: tuple[tuple[object, ...], ...]
    page_count: int
    topology: Mapping[str, object]
    api_container_id: str
    stable_container_epochs: Mapping[str, tuple[str, str]]
    api_image_id: str
    memory_reserve_bytes: int
    safety_limits: Mapping[str, object]
    started_at: datetime
    expected_campaign_epoch_sha256: str | None
    observed_campaign_epoch_sha256: str


def _sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"capacity commissioning {label} is invalid")
    return value


def _positive_float(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"capacity commissioning {label} is invalid")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"capacity commissioning {label} is invalid")
    return result


def _nonnegative_float(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"capacity commissioning {label} is invalid")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"capacity commissioning {label} is invalid")
    return result


def _basis_points(
    value: object,
    *,
    label: str,
    allow_zero: bool,
) -> int:
    minimum = 0 if allow_zero else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= 10_000
    ):
        raise ValueError(f"capacity commissioning {label} is invalid")
    return value


def _positive_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"capacity commissioning {label} is invalid")
    return value


def _is_safe_staged_timeout(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and value >= MINERU_STAGED_LOAD_MINIMUM_RUNAWAY_TIMEOUT_SECONDS
    )


def _pages_per_host_hour_milli(*, pages: int, elapsed_seconds: float) -> int:
    """Calculate a cross-runtime-stable fixed-point throughput value."""

    rate = (
        Decimal(pages)
        * Decimal(3_600_000)
        / Decimal(str(elapsed_seconds))
    )
    return int(rate.to_integral_value(rounding=ROUND_FLOOR))


def _spread_basis_points(rates: tuple[int, int]) -> int:
    ceiling = max(rates)
    spread = ceiling - min(rates)
    return (spread * 10_000 + ceiling - 1) // ceiling


def _relative_gain_basis_points(*, floor: int, ceiling: int) -> int:
    return ((floor - ceiling) * 10_000) // ceiling


def _utc(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"capacity commissioning {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"capacity commissioning {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"capacity commissioning {label} is not UTC")
    return parsed


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("capacity commissioning evidence is not canonical JSON") from exc


def _cleanup_is_closed(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("temporary_tree_removed") is True
        and value.get("external_api_temp_dirs_after") == 0
        and value.get("api_temp_cleanup_errors") == []
        and value.get("external_mineru_processes_after") == 0
        and value.get("observation_error") is None
    )


def _stage_safety_is_closed(
    stage: Mapping[str, object],
    *,
    stage_elapsed_seconds: float,
) -> float:
    """Recompute the staged-load metrics/API graduation gates."""

    metrics = stage.get("metrics")
    if (
        not isinstance(metrics, dict)
        or metrics.get("sampling_failures") != []
        or not staged_load_metrics_are_proved(
            metrics,
            stage_elapsed_seconds=stage_elapsed_seconds,
        )
    ):
        raise ValueError("capacity commissioning metrics are not proved")
    ranges = metrics.get("range")
    percentiles = metrics.get("percentiles")
    baseline = metrics.get("baseline")
    assert isinstance(ranges, dict)
    assert isinstance(percentiles, dict)
    assert isinstance(baseline, dict)
    waiting_range = ranges.get("waiting")
    kv_range = ranges.get("kv_cache")
    preemption_range = ranges.get("preemptions")
    if not all(
        isinstance(item, dict)
        for item in (waiting_range, kv_range, preemption_range)
    ):
        raise ValueError("capacity commissioning metric ranges are invalid")
    assert isinstance(waiting_range, dict)
    assert isinstance(kv_range, dict)
    assert isinstance(preemption_range, dict)
    waiting_peak = _nonnegative_float(
        waiting_range.get("max"), label="waiting peak"
    )
    waiting_p95 = _nonnegative_float(
        percentiles.get("waiting_p95"), label="waiting p95"
    )
    kv_peak = _nonnegative_float(kv_range.get("max"), label="KV peak")
    baseline_preemptions = _nonnegative_float(
        baseline.get("preemptions"), label="preemption baseline"
    )
    if (
        waiting_peak >= _GRADUATION_WAITING_PEAK_EXCLUSIVE
        or waiting_p95 > _GRADUATION_WAITING_P95_MAX
        or kv_peak >= _GRADUATION_KV_PEAK_EXCLUSIVE
        or preemption_range.get("min") != baseline_preemptions
        or preemption_range.get("max") != baseline_preemptions
    ):
        raise ValueError("capacity commissioning metrics failed graduation")

    orchestrator = stage.get("orchestrator")
    try:
        verify_staged_load_orchestrator_evidence(
            orchestrator,
            stage_elapsed_seconds=stage_elapsed_seconds,
            task_slots=1,
            client_outstanding_window=1,
        )
    except MinerUDeploymentGateError as exc:
        raise ValueError(
            "capacity commissioning orchestrator evidence is not proved"
        ) from exc
    assert isinstance(orchestrator, dict)
    baseline_health = orchestrator.get("baseline")
    terminal_health = orchestrator.get("terminal")
    terminal_drain = orchestrator.get("terminal_drain_seconds")
    if (
        not isinstance(baseline_health, dict)
        or not isinstance(terminal_health, dict)
        or _nonnegative_float(
            terminal_drain,
            label="terminal drain",
        )
        > _GRADUATION_TERMINAL_DRAIN_SECONDS_MAX
    ):
        raise ValueError("capacity commissioning orchestrator did not drain cleanly")
    return baseline_preemptions


def _arm_evidence(
    receipt: Mapping[str, Any],
    capture: MineruPhaseTraceCapture,
    *,
    expected_mode: str,
    expected_profile_sha256: str,
    expected_collector_sha256: str,
    expected_node_sha256: str,
    expected_docker_memory_reserve_bytes: int,
    expected_collector_path: str,
) -> _ArmEvidence:
    safety_limits = receipt.get("safety_limits")
    if (
        receipt.get("schema") != _STAGED_LOAD_SCHEMA
        or receipt.get("receipt_schema_version") != _STAGED_LOAD_SCHEMA_VERSION
        or receipt.get("status") != "pass"
        or receipt.get("failure") is not None
        or receipt.get("secondary_failures") != []
        or receipt.get("database_access") != "none"
        or receipt.get("queue_access") != "none"
        or receipt.get("fixed_stage_document_counts") != list(_STAGE_COUNTS)
        or receipt.get("orchestrator_task_concurrency") != 1
        or receipt.get("orchestrator_inference_concurrency") != 7
        or receipt.get("effective_inference_request_upper_bound") != 7
        or not isinstance(safety_limits, dict)
        or set(safety_limits)
        != {
            "profile",
            "document_runaway_timeout_seconds",
            "api_drain_timeout_seconds",
        }
        or safety_limits.get("profile") != _STAGED_LOAD_SAFETY_LIMITS_PROFILE
        or not _is_safe_staged_timeout(
            safety_limits.get("document_runaway_timeout_seconds")
        )
        or not _is_safe_staged_timeout(
            safety_limits.get("api_drain_timeout_seconds")
        )
        or not _cleanup_is_closed(receipt.get("cleanup"))
    ):
        raise ValueError("capacity commissioning staged-load arm is not PASS")
    execution_id = receipt.get("execution_id")
    try:
        parsed_execution_id = uuid.UUID(str(execution_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("capacity commissioning execution identity is invalid") from exc
    if str(parsed_execution_id) != execution_id:
        raise ValueError("capacity commissioning execution identity is not canonical")
    reported_elapsed_seconds = _positive_float(
        receipt.get("elapsed_seconds"), label="arm elapsed seconds"
    )
    started = _utc(receipt.get("started_at_utc"), label="arm start")
    finished = _utc(receipt.get("finished_at_utc"), label="arm finish")
    capture_since = _utc(capture.since_utc, label="capture start")
    capture_until = _utc(capture.until_utc, label="capture end")
    if not capture_since <= started < finished <= capture_until:
        raise ValueError("capacity commissioning capture does not enclose its arm")
    if capture.collector_path != expected_collector_path:
        raise ValueError(
            "capacity commissioning capture collector path drifted"
        )
    timeline_elapsed_seconds = (finished - started).total_seconds()
    if (
        timeline_elapsed_seconds <= 0
        or abs(timeline_elapsed_seconds - reported_elapsed_seconds)
        > _TIMELINE_TOLERANCE_SECONDS
    ):
        raise ValueError("capacity commissioning arm timeline is inconsistent")

    try:
        epochs = verify_host_capacity_evidence(
            receipt.get("host_capacity"),
            expected_identity={
                "collector_path": expected_collector_path,
                "collector_sha256": expected_collector_sha256,
                "docker_memory_reserve_bytes": (
                    expected_docker_memory_reserve_bytes
                ),
                "windows_node_identity_sha256": expected_node_sha256,
            },
            receipt_elapsed_seconds=reported_elapsed_seconds,
            expected_epochs=None,
        )
    except MinerUDeploymentGateError as exc:
        raise ValueError(
            "capacity commissioning host evidence is not proved"
        ) from exc
    try:
        expected_campaign_epoch_sha256, observed_campaign_epoch_sha256 = (
            verify_staged_load_inference_liveness_evidence(
                receipt,
                host_epochs=epochs,
            )
        )
    except MinerUDeploymentGateError as exc:
        raise ValueError(
            "capacity commissioning arm-boundary inference liveness is not proved"
        ) from exc
    api_container_id = epochs["mineru-api"][0]
    capture_summary = summarize_phase_trace_capture(
        capture,
        expected_profile_sha256=expected_profile_sha256,
        expected_capacity_mode=expected_mode,
        expected_collector_sha256=expected_collector_sha256,
        expected_windows_node_identity_sha256=expected_node_sha256,
        expected_container_id=api_container_id,
        require_pipeline_overlap=expected_mode == "candidate",
    )

    stages = receipt.get("stages")
    if not isinstance(stages, list) or len(stages) != len(_STAGE_COUNTS):
        raise ValueError("capacity commissioning stages are incomplete")
    outputs: list[tuple[object, ...]] = []
    page_count = 0
    total_stage_elapsed = 0.0
    staged_preemptions: float | None = None
    for stage, expected_count in zip(stages, _STAGE_COUNTS, strict=True):
        stage_elapsed = (
            _positive_float(stage.get("elapsed_seconds"), label="stage elapsed")
            if isinstance(stage, dict)
            else None
        )
        if (
            not isinstance(stage, dict)
            or stage.get("status") != "pass"
            or stage.get("failure") is not None
            or stage.get("stage_document_count") != expected_count
            or stage.get("client_outstanding_window") != 1
            or stage.get("selection_profile")
            != "per_stage_regular_heavy_huge.v1"
            or stage.get("orchestrator_task_concurrency") != 1
            or stage.get("orchestrator_inference_concurrency") != 7
            or stage.get("effective_inference_request_upper_bound") != 7
            or not _cleanup_is_closed(stage.get("cleanup"))
            or stage_elapsed is None
        ):
            raise ValueError("capacity commissioning stage is not PASS")
        total_stage_elapsed += stage_elapsed
        try:
            verify_staged_load_admission_evidence(
                stage,
                document_count=expected_count,
            )
        except MinerUDeploymentGateError as exc:
            raise ValueError(
                "capacity commissioning FIFO admission is not proved"
            ) from exc
        current_preemptions = _stage_safety_is_closed(
            stage,
            stage_elapsed_seconds=stage_elapsed,
        )
        if staged_preemptions is None:
            staged_preemptions = current_preemptions
        elif current_preemptions != staged_preemptions:
            raise ValueError(
                "capacity commissioning preemption baseline changed between stages"
            )
        documents = stage.get("documents")
        if not isinstance(documents, list) or len(documents) != expected_count:
            raise ValueError("capacity commissioning stage documents are incomplete")
        for copy_index, document in enumerate(documents, start=1):
            if (
                not isinstance(document, dict)
                or document.get("status") != "pass"
                or document.get("copy_index") != copy_index
                or not isinstance(document.get("page_count"), int)
                or isinstance(document.get("page_count"), bool)
                or document["page_count"] <= 0
                or not isinstance(document.get("block_count"), int)
                or isinstance(document.get("block_count"), bool)
                or document["block_count"] < 0
                or _positive_float(
                    document.get("elapsed_seconds"), label="document elapsed"
                )
                <= 0
                or _SHA256_RE.fullmatch(str(document.get("input_sha256"))) is None
                or _SHA256_RE.fullmatch(str(document.get("provider_bundle_sha256")))
                is None
            ):
                raise ValueError("capacity commissioning document output is invalid")
            page_count += int(document["page_count"])
            outputs.append(
                (
                    expected_count,
                    copy_index,
                    document.get("logical_name"),
                    document.get("workload_class"),
                    document.get("input_sha256"),
                    document.get("page_count"),
                    document.get("block_count"),
                )
            )
    if total_stage_elapsed > reported_elapsed_seconds:
        raise ValueError("capacity commissioning stage time exceeds arm time")
    if (
        capture_summary.get("document_count") != sum(_STAGE_COUNTS)
        or capture_summary.get("page_count") != page_count
    ):
        raise ValueError("capacity commissioning trace/output conservation failed")
    identity = receipt.get("identity")
    input_evidence = receipt.get("input")
    topology = receipt.get("topology")
    if not all(isinstance(item, dict) for item in (identity, input_evidence, topology)):
        raise ValueError("capacity commissioning arm identity is incomplete")
    assert isinstance(identity, dict)
    assert isinstance(input_evidence, dict)
    assert isinstance(topology, dict)
    return _ArmEvidence(
        capture_summary=capture_summary,
        elapsed_seconds=timeline_elapsed_seconds,
        execution_id=str(execution_id),
        finished_at=finished,
        identity=identity,
        input_evidence=input_evidence,
        output_fingerprint=tuple(outputs),
        page_count=page_count,
        topology=topology,
        api_container_id=api_container_id,
        stable_container_epochs=epochs,
        api_image_id=capture.container_image_id,
        memory_reserve_bytes=expected_docker_memory_reserve_bytes,
        safety_limits=safety_limits,
        started_at=started,
        expected_campaign_epoch_sha256=expected_campaign_epoch_sha256,
        observed_campaign_epoch_sha256=observed_campaign_epoch_sha256,
    )


def evaluate_capacity_commissioning(
    arms: Sequence[tuple[Mapping[str, Any], MineruPhaseTraceCapture]],
    *,
    expected_legacy_profile_sha256: str,
    expected_candidate_profile_sha256: str,
    expected_collector_sha256: str,
    expected_collector_path: str,
    expected_docker_memory_reserve_bytes: int,
    expected_windows_node_identity_sha256: str,
    minimum_improvement_basis_points: int,
    maximum_repeat_spread_basis_points: int,
) -> dict[str, object]:
    """Return COMMISSION only for a stable, material A-B-B-A improvement."""

    if len(arms) != 4:
        raise ValueError("capacity commissioning requires exact A-B-B-A arms")
    legacy_hash = _sha256(
        expected_legacy_profile_sha256, label="legacy profile hash"
    )
    candidate_hash = _sha256(
        expected_candidate_profile_sha256, label="candidate profile hash"
    )
    collector_hash = _sha256(
        expected_collector_sha256, label="collector hash"
    )
    node_hash = _sha256(
        expected_windows_node_identity_sha256, label="Windows node hash"
    )
    if (
        not isinstance(expected_collector_path, str)
        or expected_collector_path != MINERU_WINDOWS_COLLECTOR_PATH
    ):
        raise ValueError("capacity commissioning collector path is invalid")
    memory_reserve = _positive_integer(
        expected_docker_memory_reserve_bytes,
        label="Docker memory reserve",
    )
    minimum_improvement = _basis_points(
        minimum_improvement_basis_points,
        label="minimum improvement basis points",
        allow_zero=False,
    )
    maximum_repeat_spread = _basis_points(
        maximum_repeat_spread_basis_points,
        label="maximum repeat spread basis points",
        allow_zero=True,
    )
    modes = ("legacy", "candidate", "candidate", "legacy")
    profile_hashes = (legacy_hash, candidate_hash, candidate_hash, legacy_hash)
    evidence = tuple(
        _arm_evidence(
            receipt,
            capture,
            expected_mode=mode,
            expected_profile_sha256=profile_hash,
            expected_collector_sha256=collector_hash,
            expected_node_sha256=node_hash,
            expected_docker_memory_reserve_bytes=memory_reserve,
            expected_collector_path=expected_collector_path,
        )
        for (receipt, capture), mode, profile_hash in zip(
            arms, modes, profile_hashes, strict=True
        )
    )
    if len({item.execution_id for item in evidence}) != 4:
        raise ValueError("capacity commissioning reused an execution receipt")
    if any(
        earlier.finished_at > later.started_at
        for earlier, later in zip(evidence, evidence[1:], strict=False)
    ):
        raise ValueError(
            "capacity commissioning arms are not chronological non-overlapping A-B-B-A"
        )
    if len({_canonical(item.input_evidence) for item in evidence}) != 1:
        raise ValueError("capacity commissioning corpus drifted between arms")
    if len({_canonical(item.topology) for item in evidence}) != 1:
        raise ValueError("capacity commissioning topology drifted between arms")
    if len({item.output_fingerprint for item in evidence}) != 1:
        raise ValueError("capacity commissioning output semantics drifted")
    if len({item.page_count for item in evidence}) != 1:
        raise ValueError("capacity commissioning page conservation drifted")
    if len({item.api_image_id for item in evidence}) != 1:
        raise ValueError("capacity commissioning API image drifted")
    if len({item.memory_reserve_bytes for item in evidence}) != 1:
        raise ValueError("capacity commissioning memory reserve drifted")
    if len({_canonical(item.safety_limits) for item in evidence}) != 1:
        raise ValueError("capacity commissioning safety limits drifted")
    stable_identity_fields = (
        "local_client_identity_sha256",
        "local_content_package_versions",
        "local_processing_window_size",
        "local_writer_code_sha256",
        "served_model_id",
        "orchestrator_task_slots",
    )
    if any(
        len({_canonical(item.identity.get(field)) for item in evidence}) != 1
        for field in stable_identity_fields
    ):
        raise ValueError("capacity commissioning non-capacity identity drifted")
    if _canonical(evidence[0].identity) != _canonical(evidence[3].identity):
        raise ValueError("capacity commissioning A identities are not repeatable")
    if _canonical(evidence[1].identity) != _canonical(evidence[2].identity):
        raise ValueError("capacity commissioning B identities are not repeatable")
    proxy_epochs = {
        item.stable_container_epochs["mineru-api-proxy"] for item in evidence
    }
    inference_epochs = {
        item.stable_container_epochs["mineru-openai-server"] for item in evidence
    }
    if len(proxy_epochs) != 1 or len(inference_epochs) != 1:
        raise ValueError("capacity commissioning stable service epoch drifted")
    campaign_epoch_sha256 = evidence[0].observed_campaign_epoch_sha256
    if (
        any(
            item.observed_campaign_epoch_sha256 != campaign_epoch_sha256
            for item in evidence
        )
        or any(
            item.expected_campaign_epoch_sha256 != campaign_epoch_sha256
            for item in evidence
        )
    ):
        raise ValueError("capacity commissioning campaign epoch preflight drifted")

    pages = evidence[0].page_count
    rates = tuple(
        _pages_per_host_hour_milli(
            pages=pages,
            elapsed_seconds=item.elapsed_seconds,
        )
        for item in evidence
    )
    baseline_rates = (rates[0], rates[3])
    candidate_rates = (rates[1], rates[2])
    baseline_ceiling = max(baseline_rates)
    candidate_floor = min(candidate_rates)
    baseline_repeat_spread = _spread_basis_points(baseline_rates)
    candidate_repeat_spread = _spread_basis_points(candidate_rates)
    relative_gain = _relative_gain_basis_points(
        floor=candidate_floor,
        ceiling=baseline_ceiling,
    )
    empirical_noise_margin = max(
        max(baseline_rates) - min(baseline_rates),
        max(candidate_rates) - min(candidate_rates),
    )
    absolute_gain = candidate_floor - baseline_ceiling
    findings: list[str] = []
    if candidate_floor <= baseline_ceiling:
        findings.append("candidate_floor_did_not_beat_baseline_ceiling")
    if relative_gain < minimum_improvement:
        findings.append("candidate_gain_below_predeclared_minimum")
    if baseline_repeat_spread > maximum_repeat_spread:
        findings.append("baseline_repeat_spread_exceeded_policy")
    if candidate_repeat_spread > maximum_repeat_spread:
        findings.append("candidate_repeat_spread_exceeded_policy")
    if absolute_gain <= empirical_noise_margin:
        findings.append("candidate_gain_did_not_exceed_empirical_repeat_noise")
    commissioning_authorized = not findings
    decision = "COMMISSION" if commissioning_authorized else "STOP"
    return {
        "arm_finished_at_utc": [item.finished_at.isoformat() for item in evidence],
        "arm_execution_ids": [item.execution_id for item in evidence],
        "arm_modes": list(modes),
        "arm_pages_per_host_hour_milli": list(rates),
        "arm_started_at_utc": [item.started_at.isoformat() for item in evidence],
        "baseline_ceiling_pages_per_host_hour_milli": baseline_ceiling,
        "baseline_profile_sha256": legacy_hash,
        "baseline_repeat_spread_basis_points": baseline_repeat_spread,
        "candidate_absolute_gain_pages_per_host_hour_milli": absolute_gain,
        "candidate_floor_pages_per_host_hour_milli": candidate_floor,
        "candidate_profile_sha256": candidate_hash,
        "candidate_relative_gain_basis_points": relative_gain,
        "candidate_repeat_spread_basis_points": candidate_repeat_spread,
        "collector_sha256": collector_hash,
        "collector_path": expected_collector_path,
        "decision": decision,
        "empirical_repeat_noise_pages_per_host_hour_milli": (
            empirical_noise_margin
        ),
        "findings": findings,
        "maximum_repeat_spread_basis_points": maximum_repeat_spread,
        "minimum_improvement_basis_points": minimum_improvement,
        "docker_memory_reserve_bytes": memory_reserve,
        "output_semantics": "source-page-block-equality-with-per-arm-bundle-validation.v1",
        "page_count_per_arm": pages,
        "profile_commissioning_authorized": commissioning_authorized,
        "schema": CAPACITY_COMMISSIONING_SCHEMA,
        "selection_rule": (
            "min(B)>max(A)+repeat_noise; gain>=minimum_bps; "
            "A_spread,B_spread<=maximum_bps"
        ),
        "windows_node_identity_sha256": node_hash,
    }


__all__ = [
    "CAPACITY_COMMISSIONING_FIELDS",
    "CAPACITY_COMMISSIONING_SCHEMA",
    "evaluate_capacity_commissioning",
]
