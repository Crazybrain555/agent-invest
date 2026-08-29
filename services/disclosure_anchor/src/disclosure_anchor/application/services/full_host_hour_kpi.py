"""Pure replay for a complete, non-extrapolated GPU-host UTC hour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    hardware_identity: tuple[str, str, str, str] | None = None
    profiles: set[str] = set()
    runtimes: set[str] = set()
    for span in spans:
        if not (
            hour_started_at_utc
            <= span.started_at_utc
            < span.finished_at_utc
            <= hour_finished
        ):
            reasons.add("host_assignment_coverage_outside_hour")
        if span.started_at_utc != cursor:
            reasons.add("host_assignment_coverage_gap_or_overlap")
        cursor = span.finished_at_utc
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
        span_hardware = (
            span.gpu_exporter_source_sha256,
            span.host_exporter_source_sha256,
            span.gpu_device_identity_sha256,
            span.parent_cgroup_epoch_sha256,
        )
        if hardware_identity is None:
            hardware_identity = span_hardware
        elif hardware_identity != span_hardware:
            reasons.add("resident_hardware_identity_drift")
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
        if (
            evidence.publish_durable_observed_at_utc < hour_started_at_utc
            or evidence.publish_precommit_at_utc >= hour_finished
        ):
            continue
        if not (
            hour_started_at_utc <= evidence.publish_precommit_at_utc
            and evidence.publish_durable_observed_at_utc < hour_finished
        ):
            reasons.add("publish_evidence_incomplete_or_conflicted")
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
            <= evidence.publish_precommit_at_utc
            and evidence.publish_durable_observed_at_utc < span.finished_at_utc
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
            or evidence.observer_run_id != owner.observer_run_id
            or evidence.observer_receipt_sha256 != owner.receipt_sha256
            or evidence.observer_seal_sha256 != owner.seal_sha256
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
    # Public/legacy outbox payloads are not atomically joined to a sealed
    # observer receipt.  Even apparently complete fields remain untrusted;
    # only the private ledger replay below can close this evidence.
    complete = False
    return DurableProfilePageEvidence(
        source_identity_sha256=source,
        host_assignment_identity_sha256=host_assignment if complete else None,
        boot_identity_sha256=boot if complete else None,
        runtime_bundle_identity_sha256=runtime if complete else None,
        process_profile_sha256=profile if complete else None,
        observer_run_id=None,
        observer_receipt_sha256=None,
        observer_seal_sha256=None,
        source_page_count=pages if complete else None,
        publish_precommit_at_utc=committed_at or occurred_at_utc,
        publish_durable_observed_at_utc=evidence_observed_at_utc,
        status="complete" if complete else "incomplete",
        first_durable_publish=True if complete else None,
    )


def reconcile_private_publish_ledger_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[DurableProfilePageEvidence, ...]:
    """Close base+supplement facts selected by the full-history first-source query."""

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        run_id = row.get("processing_run_id")
        if not isinstance(run_id, str):
            raise ValueError("private publish ledger row lacks processing run identity")
        grouped.setdefault(run_id, []).append(row)
    evidence: list[DurableProfilePageEvidence] = []
    for run_id in sorted(grouped):
        group = grouped[run_id]
        first = group[0]
        source = first.get("source_identity_sha256")
        pages = first.get("source_page_count")
        committed = first.get("publish_precommit_at")
        if isinstance(committed, datetime):
            committed = committed.astimezone(timezone.utc)
        if (
            not isinstance(source, str)
            or isinstance(pages, bool)
            or not isinstance(pages, int)
            or pages < 1
            or not isinstance(committed, datetime)
        ):
            raise ValueError("private publish base projection is invalid")
        status = "incomplete"
        observed = committed
        projection: tuple[str, str, str, str, str, str, str] | None = None
        conflict = first.get("source_page_variants") != 1
        for row in group:
            if (
                row.get("source_identity_sha256") != source
                or row.get("source_page_count") != pages
                or row.get("publish_precommit_at") != committed
            ):
                conflict = True
            supplement_id = row.get("supplement_id")
            if supplement_id is None:
                continue
            if not isinstance(supplement_id, str):
                conflict = True
                continue
            candidate = (
                row.get("host_assignment_identity_sha256"),
                row.get("boot_identity_sha256"),
                row.get("runtime_bundle_identity_sha256"),
                row.get("process_profile_sha256"),
                row.get("observer_run_id"),
                row.get("observer_receipt_sha256"),
                row.get("observer_seal_sha256"),
            )
            supplement_observed = row.get("publish_durable_observed_at")
            if isinstance(supplement_observed, datetime):
                supplement_observed = supplement_observed.astimezone(timezone.utc)
            supplement_precommit = row.get("supplement_publish_precommit_at")
            if isinstance(supplement_precommit, datetime):
                supplement_precommit = supplement_precommit.astimezone(timezone.utc)
            if (
                row.get("supplement_source_identity_sha256") != source
                or row.get("supplement_source_page_count") != pages
                or supplement_precommit != committed
                or row.get("observer_contract_version")
                != "mineru.synchronized-telemetry-receipt.v2"
                or not all(isinstance(value, str) for value in candidate)
                or not isinstance(supplement_observed, datetime)
                or supplement_observed < committed
            ):
                conflict = True
                continue
            typed = (
                str(candidate[0]), str(candidate[1]), str(candidate[2]),
                str(candidate[3]), str(candidate[4]), str(candidate[5]),
                str(candidate[6]),
            )
            if projection is not None and projection != typed:
                conflict = True
            projection = typed  # identical repeated supplements remain auditable/idempotent
            observed = max(observed, supplement_observed)
        if conflict:
            status = "conflict"
            projection = None
        elif projection is not None:
            status = "complete"
        evidence.append(
            DurableProfilePageEvidence(
                source_identity_sha256=source,
                host_assignment_identity_sha256=projection[0] if projection else None,
                boot_identity_sha256=projection[1] if projection else None,
                runtime_bundle_identity_sha256=projection[2] if projection else None,
                process_profile_sha256=projection[3] if projection else None,
                observer_run_id=projection[4] if projection else None,
                observer_receipt_sha256=projection[5] if projection else None,
                observer_seal_sha256=projection[6] if projection else None,
                source_page_count=pages if projection else None,
                publish_precommit_at_utc=committed,
                publish_durable_observed_at_utc=observed,
                status=status,
                first_durable_publish=True if projection else None,
            )
        )
    return tuple(evidence)


__all__ = [
    "aggregate_full_gpu_host_hour",
    "project_publish_payload_for_host_hour",
    "reconcile_private_publish_ledger_rows",
]
