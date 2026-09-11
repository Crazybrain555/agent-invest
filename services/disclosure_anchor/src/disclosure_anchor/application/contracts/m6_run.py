"""Private M6 run identity, physical-clock interval and qualified projections."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from disclosure_anchor.application.contracts.m6_campaign import M6Mode
from disclosure_anchor.application.contracts.m6_common import (
    M6ClosedModel, M6Hash, M6Id, M6NonnegativeInt, M6PositiveInt, M6Reason,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import canonical_json_sha256


class M6ClockDomain(M6ClosedModel):
    host_assignment_identity_sha256: M6Hash
    boot_identity_sha256: M6Hash
    qpc_frequency_hz: M6PositiveInt
    clock_domain_identity_sha256: M6Hash

    @model_validator(mode="after")
    def clock_binding(self) -> Self:
        expected = canonical_json_sha256({
            "boot_identity_sha256": self.boot_identity_sha256,
            "clock_source": "QueryPerformanceCounter", "frequency_hz": self.qpc_frequency_hz,
        })
        if expected != self.clock_domain_identity_sha256:
            raise ValueError("QPC clock domain differs from boot/frequency")
        return self


class M6RuntimeIdentity(M6ClosedModel):
    source_commit: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{40}$")]
    source_manifest_sha256: M6Hash
    runtime_bundle_identity_sha256: M6Hash
    process_profile_sha256: M6Hash
    worker_profile_sha256: M6Hash | None
    owner_source_sha256: M6Hash
    gpu_device_identity_sha256: M6Hash
    deployment_qualification_sha256: M6Hash


class M6ResourceEnvelope(M6ClosedModel):
    max_events: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
    max_record_bytes: Annotated[int, Field(strict=True, ge=1, le=1_048_576)]
    max_log_bytes: Annotated[int, Field(strict=True, ge=1, le=1_073_741_824)]
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=100_000)]
    max_verifier_backlog_bytes: M6PositiveInt
    stop_admission_budget_ticks: M6PositiveInt

    @model_validator(mode="after")
    def consistent_bounds(self) -> Self:
        if self.max_log_bytes < self.max_record_bytes or self.max_events < self.max_attempts:
            raise ValueError("M6 resource envelope cannot hold its declared records/attempts")
        return self


class M6RunSpec(M6ClosedModel):
    contract_version: Literal["m6.run-spec.v1"] = "m6.run-spec.v1"
    run_id: M6Id
    campaign_id: M6Id
    mode: M6Mode
    phase: Literal["short_batch", "hour_baseline", "stability_repeat", "recovery_experiment"]
    start_condition: Literal["cold", "resident_warm", "warm_service_disclosed"]
    clock: M6ClockDomain
    runtime: M6RuntimeIdentity
    manifest_sha256: M6Hash
    scope_sha256: M6Hash | None
    quality_plan_sha256: M6Hash
    t0_ticks: M6NonnegativeInt
    planned_seconds: M6PositiveInt
    deadline_ticks: M6PositiveInt
    max_close_ticks: M6PositiveInt
    carry_in_attempt_ids: Annotated[tuple[M6Id, ...], Field(max_length=100_000)]
    resources: M6ResourceEnvelope

    @model_validator(mode="after")
    def interval_and_mode(self) -> Self:
        if self.deadline_ticks != self.t0_ticks + self.planned_seconds * self.clock.qpc_frequency_hz:
            raise ValueError("deadline must retain the original exact QPC interval")
        if self.max_close_ticks < self.deadline_ticks:
            raise ValueError("whole-run close bound precedes admission deadline")
        if self.phase in {"hour_baseline", "stability_repeat"} and self.planned_seconds < 3600:
            raise ValueError("formal hour/stability run requires at least 3600 seconds")
        if self.mode == "e2e_publication":
            if self.scope_sha256 is None or self.runtime.worker_profile_sha256 is None:
                raise ValueError("E2E run requires campaign and worker identities")
        elif self.scope_sha256 is not None or self.runtime.worker_profile_sha256 is not None:
            raise ValueError("service diagnostic cannot claim publication scope/worker")
        if self.carry_in_attempt_ids != tuple(sorted(set(self.carry_in_attempt_ids))):
            raise ValueError("carry-in attempt IDs must be sorted and unique")
        if len(self.carry_in_attempt_ids) > self.resources.max_attempts:
            raise ValueError("carry-in exceeds the run's attempt bound")
        return self


class M6SourceHistoryFact(M6ClosedModel):
    source_pdf_sha256: M6Hash
    scan_complete: bool
    first_processing_run_id: M6Id | None
    first_ledger_seq: M6PositiveInt | None
    first_source_page_count: M6PositiveInt | None
    source_page_variants: M6NonnegativeInt
    audit_receipt_sha256: M6Hash

    @model_validator(mode="after")
    def first_identity(self) -> Self:
        values = (self.first_processing_run_id, self.first_ledger_seq, self.first_source_page_count)
        if any(item is None for item in values) != all(item is None for item in values):
            raise ValueError("first ledger identity is partial")
        if (self.source_page_variants == 0) != (self.first_ledger_seq is None):
            raise ValueError("history variants disagree with first ledger identity")
        return self


class M6SourceOutcome(M6ClosedModel):
    source_pdf_sha256: M6Hash
    attempt_id: M6Id
    outcome: Literal[
        "credited_window", "credited_whole_run_only", "carry_in", "replay",
        "not_first_publish", "novelty_unverified", "quality_not_scorable",
        "quality_review_pending", "confirmation_missing", "page_count_conflict", "failed",
        "qualification_missing", "admitted_after_deadline",
    ]
    page_count: M6PositiveInt
    ready_received_ticks: M6NonnegativeInt | None
    admission_received_ticks: M6NonnegativeInt


class M6PublicationMetrics(M6ClosedModel):
    kind: Literal["first_qualified_publication"] = "first_qualified_publication"
    window_pages: M6NonnegativeInt
    whole_run_pages: M6NonnegativeInt
    carry_in_pages: M6NonnegativeInt

    @model_validator(mode="after")
    def page_subsets(self) -> Self:
        if self.window_pages > self.whole_run_pages:
            raise ValueError("window pages exceed whole-run pages")
        return self


class M6ServiceMetrics(M6ClosedModel):
    kind: Literal["service_validated_source_pages"] = "service_validated_source_pages"
    window_pages: M6NonnegativeInt
    whole_run_pages: M6NonnegativeInt
    replay_pages: M6NonnegativeInt
    carry_in_pages: M6NonnegativeInt

    @model_validator(mode="after")
    def page_subsets(self) -> Self:
        if self.window_pages > self.whole_run_pages or self.replay_pages > self.whole_run_pages:
            raise ValueError("service page subsets exceed whole-run pages")
        return self


class M6RunReceipt(M6ClosedModel):
    contract_version: Literal["m6.run-receipt.v1"] = "m6.run-receipt.v1"
    run_id: M6Id
    spec_sha256: M6Hash
    journal_prefix_sha256: M6Hash
    journal_bytes_consumed: M6NonnegativeInt
    mode: M6Mode
    phase: Literal["short_batch", "hour_baseline", "stability_repeat", "recovery_experiment"]
    status: Literal["complete", "incomplete", "invalid"]
    incomplete_reasons: tuple[M6Reason, ...]
    invalid_reasons: tuple[M6Reason, ...]
    t0_ticks: M6NonnegativeInt
    deadline_ticks: M6PositiveInt
    tclose_ticks: M6NonnegativeInt | None
    elapsed_ticks: M6NonnegativeInt | None
    qpc_frequency_hz: M6PositiveInt
    stop_requested_ticks: M6NonnegativeInt | None
    stop_effective_ticks: M6NonnegativeInt | None
    close_reason: Literal["deadline_drained", "stop_requested", "failed"] | None
    metrics: Annotated[M6PublicationMetrics | M6ServiceMetrics, Field(discriminator="kind")] | None
    sources: tuple[M6SourceOutcome, ...]
    events_total: M6NonnegativeInt
    duplicate_events: M6NonnegativeInt

    @model_validator(mode="after")
    def trustworthy_projection(self) -> Self:
        expected = "invalid" if self.invalid_reasons else "incomplete" if self.incomplete_reasons else "complete"
        if self.status != expected or (self.status == "complete") != (self.metrics is not None):
            raise ValueError("run completeness disagrees with trusted metrics")
        for reasons in (self.incomplete_reasons, self.invalid_reasons):
            if reasons != tuple(sorted(set(reasons))):
                raise ValueError("receipt reasons must be sorted and unique")
        if self.metrics is not None and (
            (self.mode == "e2e_publication") != isinstance(self.metrics, M6PublicationMetrics)
        ):
            raise ValueError("metrics authority differs from run mode")
        if self.elapsed_ticks is not None and (
            self.tclose_ticks is None or self.elapsed_ticks != self.tclose_ticks - self.t0_ticks
        ):
            raise ValueError("elapsed ticks differ from whole-run interval")
        if self.status == "complete" and (self.elapsed_ticks is None or self.elapsed_ticks <= 0):
            raise ValueError("complete run requires a positive whole-run interval")
        return self
