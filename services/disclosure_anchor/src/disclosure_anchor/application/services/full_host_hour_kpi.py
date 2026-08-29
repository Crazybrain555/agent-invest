"""Pure replay for a complete, non-extrapolated GPU-host UTC hour."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from disclosure_anchor.application.contracts.full_host_hour_kpi import (
    DurableProfilePageEvidence,
    FullGpuHostHourKpi,
    IncompleteReason,
    VerifiedTelemetryCoverage,
)


def aggregate_full_gpu_host_hour(
    *,
    hour_started_at_utc: datetime,
    coverage: Iterable[VerifiedTelemetryCoverage],
    publish_evidence: Iterable[DurableProfilePageEvidence],
    publish_history_scan_complete: bool,
) -> FullGpuHostHourKpi:
    hour_finished = hour_started_at_utc + timedelta(hours=1)
    reasons: set[IncompleteReason] = set()
    if not publish_history_scan_complete:
        reasons.add("publish_history_scan_incomplete")
    spans = sorted(coverage, key=lambda item: item.started_at_utc)
    if not spans:
        reasons.add("missing_host_assignment_coverage")
    cursor = hour_started_at_utc
    host_identity: str | None = None
    boot_identity: str | None = None
    exporter_epochs: tuple[str, str] | None = None
    profiles: set[str] = set()
    runtimes: set[str] = set()
    for span in spans:
        if span.started_at_utc > cursor:
            reasons.add("host_assignment_coverage_gap_or_overlap")
        cursor = max(cursor, span.finished_at_utc)
        if host_identity is None:
            host_identity = span.host_assignment_identity_sha256
        elif host_identity != span.host_assignment_identity_sha256:
            reasons.add("host_assignment_identity_drift")
        if boot_identity is None:
            boot_identity = span.boot_identity_sha256
        elif boot_identity != span.boot_identity_sha256:
            reasons.add("host_boot_identity_drift")
        span_exporter_epochs = (
            span.gpu_exporter_process_epoch_sha256,
            span.host_exporter_process_epoch_sha256,
        )
        if exporter_epochs is None:
            exporter_epochs = span_exporter_epochs
        elif exporter_epochs != span_exporter_epochs:
            reasons.add("resident_exporter_epoch_drift")
        profiles.add(span.process_profile_sha256)
        runtimes.add(span.runtime_bundle_identity_sha256)
        if span.status != "complete" or not span.gpu_lane_complete or not span.host_lane_complete:
            reasons.add("telemetry_coverage_incomplete")
        if not span.observer_overhead_safe:
            reasons.add("observer_overhead_unsafe")
        if not span.exporter_overhead_safe:
            reasons.add("exporter_overhead_unverified_or_unsafe")
    if cursor < hour_finished:
        reasons.add("host_assignment_coverage_gap_or_overlap")

    pages: dict[str, int] = {}
    evidence_profiles: set[str] = set()
    conflicted: set[str] = set()
    for evidence in publish_evidence:
        if not (hour_started_at_utc <= evidence.publish_committed_at_utc < hour_finished):
            continue
        if (
            evidence.status != "complete"
            or evidence.runtime_bundle_identity_sha256 is None
            or evidence.process_profile_sha256 is None
            or evidence.source_page_count is None
            or evidence.first_durable_publish is not True
        ):
            reasons.add("publish_evidence_incomplete_or_conflicted")
            continue
        matching_spans = [
            span
            for span in spans
            if span.started_at_utc
            <= evidence.publish_committed_at_utc
            < span.finished_at_utc
        ]
        if len(matching_spans) != 1:
            reasons.add("publish_evidence_has_no_unique_host_span")
            continue
        owner = matching_spans[0]
        if (
            evidence.host_assignment_identity_sha256
            != owner.host_assignment_identity_sha256
            or evidence.boot_identity_sha256 != owner.boot_identity_sha256
            or evidence.runtime_bundle_identity_sha256
            != owner.runtime_bundle_identity_sha256
            or evidence.process_profile_sha256 != owner.process_profile_sha256
        ):
            reasons.add("publish_evidence_host_runtime_profile_mismatch")
            continue
        key = evidence.source_identity_sha256
        evidence_profiles.add(evidence.process_profile_sha256)
        previous = pages.get(key)
        if previous is not None and previous != evidence.source_page_count:
            conflicted.add(key)
            reasons.add("publish_page_count_conflict")
        else:
            pages[key] = evidence.source_page_count
    for key in conflicted:
        pages.pop(key, None)
    if reasons:
        return FullGpuHostHourKpi(
            hour_started_at_utc=hour_started_at_utc,
            hour_finished_at_utc=hour_finished,
            complete=False,
            incomplete_reasons=tuple(sorted(reasons)),
        )
    host_pages = sum(pages.values())
    eligible_profile = (
        next(iter(profiles))
        if len(profiles) == 1
        and len(runtimes) == 1
        and (not evidence_profiles or evidence_profiles == profiles)
        else None
    )
    return FullGpuHostHourKpi(
        hour_started_at_utc=hour_started_at_utc,
        hour_finished_at_utc=hour_finished,
        complete=True,
        incomplete_reasons=(),
        host_unique_durable_pages=host_pages,
        profile_eligible_unique_durable_pages=(host_pages if eligible_profile else None),
        profile_eligible_process_profile_sha256=eligible_profile,
        host_assignment_identity_sha256=host_identity,
    )


def project_publish_payload_for_host_hour(
    payload: Mapping[str, Any],
    *,
    event_kind: str,
    occurred_at_utc: datetime,
    evidence_observed_at_utc: datetime,
) -> DurableProfilePageEvidence:
    """Project durable evidence; legacy payloads missing runtime/profile stay incomplete."""

    source = payload.get("source_identity")
    if not isinstance(source, str):
        raise ValueError("publish payload source identity is missing")
    pages = payload.get("source_page_count")
    runtime = payload.get("runtime_bundle_identity_sha256")
    profile = payload.get("process_profile_sha256")
    host_assignment = payload.get("host_assignment_identity_sha256")
    boot = payload.get("boot_identity_sha256")
    raw_commit = payload.get("publish_committed_at")
    try:
        committed_at = (
            datetime.fromisoformat(raw_commit)
            if isinstance(raw_commit, str)
            else None
        )
    except ValueError:
        committed_at = None
    if event_kind not in {
        "processing_run_published",
        "processing_run_publish_evidence_backfilled",
    }:
        raise ValueError("publish evidence event kind is invalid")
    if event_kind == "processing_run_published" and committed_at != occurred_at_utc:
        committed_at = None
    complete = (
        isinstance(pages, int)
        and not isinstance(pages, bool)
        and pages > 0
        and isinstance(runtime, str)
        and isinstance(profile, str)
        and isinstance(host_assignment, str)
        and isinstance(boot, str)
        and committed_at is not None
        and payload.get("is_first_durable_publish") is True
    )
    return DurableProfilePageEvidence(
        source_identity_sha256=source,
        host_assignment_identity_sha256=host_assignment if complete else None,
        boot_identity_sha256=boot if complete else None,
        runtime_bundle_identity_sha256=runtime if complete else None,
        process_profile_sha256=profile if complete else None,
        source_page_count=pages if complete else None,
        publish_committed_at_utc=committed_at or occurred_at_utc,
        evidence_observed_at_utc=evidence_observed_at_utc,
        status="complete" if complete else "incomplete",
        first_durable_publish=True if complete else None,
    )


__all__ = ["aggregate_full_gpu_host_hour", "project_publish_payload_for_host_hour"]
