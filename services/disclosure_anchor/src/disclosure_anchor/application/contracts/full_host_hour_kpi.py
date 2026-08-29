"""Pure, content-free evidence contract for one complete GPU-host UTC hour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _hash(value: str, label: str) -> None:
    if _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} is not a canonical SHA-256")


def _utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{label} must be UTC")


class VerifiedTelemetryCoverage(_Closed):
    """Summary produced only after observer-v2 receipt/seal/frame verification."""

    started_at_utc: datetime
    finished_at_utc: datetime
    host_assignment_identity_sha256: str
    boot_identity_sha256: str
    gpu_exporter_process_epoch_sha256: str
    host_exporter_process_epoch_sha256: str
    runtime_bundle_identity_sha256: str
    process_profile_sha256: str
    observer_process_epoch_sha256: str
    receipt_sha256: str
    seal_sha256: str
    status: str
    gpu_lane_complete: bool
    host_lane_complete: bool
    observer_overhead_safe: bool
    exporter_overhead_safe: bool
    exporter_overhead_attestation_sha256: str | None

    @model_validator(mode="after")
    def _closed(self) -> "VerifiedTelemetryCoverage":
        _utc(self.started_at_utc, "started_at_utc")
        _utc(self.finished_at_utc, "finished_at_utc")
        if self.finished_at_utc <= self.started_at_utc:
            raise ValueError("coverage interval is empty")
        for name in (
            "host_assignment_identity_sha256",
            "boot_identity_sha256",
            "gpu_exporter_process_epoch_sha256",
            "host_exporter_process_epoch_sha256",
            "runtime_bundle_identity_sha256",
            "process_profile_sha256",
            "observer_process_epoch_sha256",
            "receipt_sha256",
            "seal_sha256",
        ):
            _hash(getattr(self, name), name)
        if self.exporter_overhead_attestation_sha256 is not None:
            _hash(
                self.exporter_overhead_attestation_sha256,
                "exporter_overhead_attestation_sha256",
            )
        if self.exporter_overhead_safe != (
            self.exporter_overhead_attestation_sha256 is not None
        ):
            raise ValueError("exporter overhead status lacks an anchored attestation")
        if self.status not in {"complete", "incomplete", "unsafe"}:
            raise ValueError("coverage status is invalid")
        return self


class DurableProfilePageEvidence(_Closed):
    source_identity_sha256: str
    host_assignment_identity_sha256: str | None
    boot_identity_sha256: str | None
    runtime_bundle_identity_sha256: str | None
    process_profile_sha256: str | None
    source_page_count: int | None = Field(default=None, ge=1)
    publish_committed_at_utc: datetime
    evidence_observed_at_utc: datetime
    status: str
    first_durable_publish: bool | None

    @model_validator(mode="after")
    def _closed(self) -> "DurableProfilePageEvidence":
        _hash(self.source_identity_sha256, "source_identity_sha256")
        if self.process_profile_sha256 is not None:
            _hash(self.process_profile_sha256, "process_profile_sha256")
        if self.runtime_bundle_identity_sha256 is not None:
            _hash(self.runtime_bundle_identity_sha256, "runtime_bundle_identity_sha256")
        if self.host_assignment_identity_sha256 is not None:
            _hash(self.host_assignment_identity_sha256, "host_assignment_identity_sha256")
        if self.boot_identity_sha256 is not None:
            _hash(self.boot_identity_sha256, "boot_identity_sha256")
        _utc(self.publish_committed_at_utc, "publish_committed_at_utc")
        _utc(self.evidence_observed_at_utc, "evidence_observed_at_utc")
        if self.evidence_observed_at_utc < self.publish_committed_at_utc:
            raise ValueError("publish evidence cannot be observed before commit")
        if self.status not in {"complete", "incomplete", "conflict"}:
            raise ValueError("publish evidence status is invalid")
        if self.status == "complete" and (
            self.runtime_bundle_identity_sha256 is None
            or self.host_assignment_identity_sha256 is None
            or self.boot_identity_sha256 is None
            or self.process_profile_sha256 is None
            or self.source_page_count is None
            or self.first_durable_publish is not True
        ):
            raise ValueError("complete publish evidence requires runtime, profile and page count")
        if self.status != "complete" and any(
            value is not None
            for value in (
                self.host_assignment_identity_sha256,
                self.boot_identity_sha256,
                self.runtime_bundle_identity_sha256,
                self.process_profile_sha256,
                self.source_page_count,
                self.first_durable_publish,
            )
        ):
            raise ValueError("incomplete/conflict evidence cannot carry trusted projections")
        return self


IncompleteReason = Literal[
    "exporter_overhead_unverified_or_unsafe",
    "host_assignment_coverage_gap_or_overlap",
    "host_assignment_coverage_outside_hour",
    "host_assignment_identity_drift",
    "host_boot_identity_drift",
    "missing_host_assignment_coverage",
    "observer_overhead_unsafe",
    "publish_evidence_has_no_unique_host_span",
    "publish_evidence_host_runtime_profile_mismatch",
    "publish_evidence_incomplete_or_conflicted",
    "publish_history_scan_incomplete",
    "publish_page_count_conflict",
    "resident_exporter_epoch_drift",
    "telemetry_coverage_incomplete",
]


class FullGpuHostHourKpi(_Closed):
    contract_version: Literal["mineru.full-gpu-host-hour-kpi.v1"] = (
        "mineru.full-gpu-host-hour-kpi.v1"
    )
    hour_started_at_utc: datetime
    hour_finished_at_utc: datetime
    complete: bool
    incomplete_reasons: tuple[IncompleteReason, ...]
    host_unique_durable_pages: int | None = Field(default=None, ge=0)
    profile_eligible_unique_durable_pages: int | None = Field(default=None, ge=0)
    profile_eligible_process_profile_sha256: str | None = None
    host_assignment_identity_sha256: str | None = None

    @model_validator(mode="after")
    def _hour(self) -> "FullGpuHostHourKpi":
        _utc(self.hour_started_at_utc, "hour_started_at_utc")
        _utc(self.hour_finished_at_utc, "hour_finished_at_utc")
        if self.hour_finished_at_utc - self.hour_started_at_utc != timedelta(hours=1):
            raise ValueError("GPU host KPI denominator is not exactly one hour")
        if self.hour_started_at_utc.minute or self.hour_started_at_utc.second or self.hour_started_at_utc.microsecond:
            raise ValueError("GPU host KPI bucket is not UTC-hour aligned")
        if self.complete:
            if (
                self.incomplete_reasons
                or self.host_unique_durable_pages is None
                or self.host_assignment_identity_sha256 is None
            ):
                raise ValueError("complete KPI cannot carry incomplete evidence")
            _hash(
                self.host_assignment_identity_sha256,
                "host_assignment_identity_sha256",
            )
        elif not self.incomplete_reasons or self.host_unique_durable_pages is not None:
            raise ValueError("incomplete KPI cannot expose a numerator")
        if (self.profile_eligible_unique_durable_pages is None) != (
            self.profile_eligible_process_profile_sha256 is None
        ):
            raise ValueError("profile eligible KPI fields must be present together")
        if self.profile_eligible_process_profile_sha256 is not None:
            _hash(
                self.profile_eligible_process_profile_sha256,
                "profile_eligible_process_profile_sha256",
            )
        if not self.complete and any(
            value is not None
            for value in (
                self.profile_eligible_unique_durable_pages,
                self.profile_eligible_process_profile_sha256,
                self.host_assignment_identity_sha256,
            )
        ):
            raise ValueError("incomplete KPI cannot expose comparison identities")
        return self


__all__ = [
    "DurableProfilePageEvidence",
    "FullGpuHostHourKpi",
    "VerifiedTelemetryCoverage",
]
