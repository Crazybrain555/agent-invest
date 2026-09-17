"""Derived `m6.delivery-report.v1`: what one frozen evaluation plan scores from existing M6 evidence.

The report is a projection, never an authority. Readiness, credit and run
validity come from `reduce_m6_run`; obligation closure comes from the owner
journal, the runner's control receipts and the external exit records. Every
proof this document cannot find stays in `unknowns` and can never become a
pass, so a high window count with an open obligation still fails delivery.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from disclosure_anchor.application.contracts.m6_common import (
    M6ClosedModel, M6Hash, M6Id, M6NonnegativeInt, M6PositiveInt, M6Reason,
)
from disclosure_anchor.application.contracts.m6_evaluation_plan import M6ResourceGates
from disclosure_anchor.application.contracts.m6_run import M6PublicationMetrics, M6ServiceMetrics

M6_DELIVERY_REPORT_CONTRACT = "m6.delivery-report.v1"
M6_DELIVERY_REPORT_MAX_BYTES = 8 * 1024 * 1024

M6Seconds = Annotated[float, Field(strict=True, ge=0.0, le=1e15)]
M6Rate = Annotated[float, Field(strict=True, ge=0.0, le=1e15)]
M6ReasonList = Annotated[tuple[M6Reason, ...], Field(max_length=4096)]
M6IdList = Annotated[tuple[M6Id, ...], Field(max_length=100_000)]


class M6EvidenceInput(M6ClosedModel):
    """One immutable file the report was derived from, by the exact bytes read."""

    path: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
    sha256: M6Hash


class M6DeliveryEvidence(M6ClosedModel):
    inputs: Annotated[tuple[M6EvidenceInput, ...], Field(max_length=100_000)]


class M6RunValidity(M6ClosedModel):
    """The reduced journal's own verdict; `unknown` when no receipt could be produced."""

    status: Literal["complete", "incomplete", "invalid", "unknown"]
    incomplete_reasons: M6ReasonList
    invalid_reasons: M6ReasonList
    spec_sha256: M6Hash
    intent_sha256: M6Hash | None
    evaluation_plan_sha256: M6Hash
    journal_prefix_sha256: M6Hash | None
    events_total: M6NonnegativeInt
    duplicate_events: M6NonnegativeInt
    owner_epoch_sha256: M6Hash | None
    t0_ticks: M6NonnegativeInt
    deadline_ticks: M6PositiveInt
    tclose_ticks: M6NonnegativeInt | None
    qpc_frequency_hz: M6PositiveInt

    @model_validator(mode="after")
    def reasons_projected(self) -> Self:
        for reasons in (self.incomplete_reasons, self.invalid_reasons):
            if reasons != tuple(sorted(set(reasons))):
                raise ValueError("validity reasons must be sorted and unique")
        if self.status == "unknown" and (self.incomplete_reasons or self.invalid_reasons):
            raise ValueError("an unknown validity carries no reduced reasons")
        return self


class M6OwnershipClosed(M6ClosedModel):
    residual_count: M6NonnegativeInt
    children_exited: bool
    receipt_sha256: M6Hash


class M6BusinessObligationsClosed(M6ClosedModel):
    """Every claimed obligation's final disposition, its ACK/absence proof and the external exits."""

    all_closed: bool
    admitted_attempts: M6NonnegativeInt
    final_attempts: M6NonnegativeInt
    attempts_without_final: M6IdList
    remote_open: M6IdList
    ack_or_absence_proven: bool
    admission_reconciled: bool
    ownership_closed: M6OwnershipClosed | None
    run_closed_reason: Literal["deadline_drained", "stop_requested", "failed"] | None
    external_owner_exit_verified: bool
    local_children_reaped: bool
    missing: M6ReasonList

    @model_validator(mode="after")
    def closure_requires_every_proof(self) -> Self:
        for names in (self.attempts_without_final, self.remote_open):
            if names != tuple(sorted(set(names))):
                raise ValueError("obligation attempt IDs must be sorted and unique")
        if self.missing != tuple(sorted(set(self.missing))):
            raise ValueError("missing proofs must be sorted and unique")
        if self.all_closed and self.missing:
            raise ValueError("a closed delivery cannot list a missing proof")
        return self


class M6QualifiedSource(M6ClosedModel):
    """One deduplicated source that earned first-qualified-publication credit."""

    source_pdf_sha256: M6Hash
    attempt_id: M6Id
    page_count: M6PositiveInt
    ready_received_ticks: M6NonnegativeInt | None
    outcome: Literal["credited_window", "credited_whole_run_only"]


class M6PublicationQualified(M6ClosedModel):
    documents: M6NonnegativeInt
    pages: M6NonnegativeInt
    sources: Annotated[tuple[M6QualifiedSource, ...], Field(max_length=10_000)]


class M6WindowExclusions(M6ClosedModel):
    """Why an admitted source is not in the main-window numerator; one bucket per accounting outcome."""

    replay: M6NonnegativeInt
    carry_in: M6NonnegativeInt
    not_first_publish: M6NonnegativeInt
    late_or_after_window: M6NonnegativeInt
    after_deadline: M6NonnegativeInt
    failed: M6NonnegativeInt
    quality_not_scorable: M6NonnegativeInt
    quality_review_pending: M6NonnegativeInt
    confirmation_missing: M6NonnegativeInt
    page_count_conflict: M6NonnegativeInt
    novelty_unverified: M6NonnegativeInt
    qualification_missing: M6NonnegativeInt


class M6SubWindow(M6ClosedModel):
    index: M6NonnegativeInt
    start_ticks: M6NonnegativeInt
    end_ticks: M6NonnegativeInt
    documents: M6NonnegativeInt
    pages: M6NonnegativeInt

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_ticks <= self.start_ticks:
            raise ValueError("sub window must end after it starts")
        return self


class M6DeliveryMainWindow(M6ClosedModel):
    """The plan's frozen interval in owner ticks; rates use the plan span, never the observed interval."""

    start_ticks: M6NonnegativeInt
    end_ticks: M6NonnegativeInt
    span_seconds: M6PositiveInt
    documents: M6NonnegativeInt
    pages: M6NonnegativeInt
    documents_per_hour: M6Rate
    pages_per_minute: M6Rate
    sub_windows: Annotated[tuple[M6SubWindow, ...], Field(max_length=1440)]
    excluded: M6WindowExclusions

    @model_validator(mode="after")
    def tiled(self) -> Self:
        if self.end_ticks <= self.start_ticks:
            raise ValueError("main window must end after it starts")
        for position, window in enumerate(self.sub_windows):
            if window.index != position:
                raise ValueError("sub windows must be reported in tiling order")
        if self.sub_windows and (
            self.sub_windows[0].start_ticks != self.start_ticks
            or self.sub_windows[-1].end_ticks != self.end_ticks
        ):
            raise ValueError("sub windows must tile exactly the main window")
        return self


class M6WholeRun(M6ClosedModel):
    documents: M6NonnegativeInt
    pages: M6NonnegativeInt
    metrics: Annotated[M6PublicationMetrics | M6ServiceMetrics, Field(discriminator="kind")] | None
    elapsed_seconds: M6Seconds | None


class M6Drain(M6ClosedModel):
    """Credit that closed after the admission deadline: whole-run only, never the main window."""

    documents: M6NonnegativeInt
    pages: M6NonnegativeInt


class M6ServiceDiagnostic(M6ClosedModel):
    validated_documents: M6NonnegativeInt
    validated_pages: M6NonnegativeInt
    replay_pages: M6NonnegativeInt


class M6ResourceSafety(M6ClosedModel):
    """`unknown` whenever the plan's gates cannot be measured from the supplied receipt."""

    status: Literal["pass", "fail", "unknown"]
    gates: M6ResourceGates | None
    evidence_sha256: M6Hash | None
    reason: M6Reason


class M6LatencyMeasure(M6ClosedModel):
    p95_s: M6Seconds
    max_s: M6Seconds

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.p95_s > self.max_s:
            raise ValueError("a nearest-rank p95 cannot exceed the observed maximum")
        return self


class M6LatencyClass(M6ClosedModel):
    """One size class: owner-tick measures, plus the stage-clock remote/public measures.

    The owner measures are stamped by the physical owner; the stage measures are
    stamped by the Mac runner and verifier processes. The two clocks are never
    mixed inside one measure, and `remote_samples`/`public_samples` say how many
    attempts actually produced each stage measure.
    """

    n: M6NonnegativeInt
    admission_to_remote_accepted: M6LatencyMeasure | None
    remote_accepted_to_publication_committed: M6LatencyMeasure | None
    publication_committed_to_public_confirmation: M6LatencyMeasure | None
    admission_to_public_confirmation: M6LatencyMeasure | None
    remote_post_to_terminal: M6LatencyMeasure | None
    terminal_to_public_confirmation: M6LatencyMeasure | None
    remote_samples: M6NonnegativeInt
    public_samples: M6NonnegativeInt
    remote_resends: M6NonnegativeInt
    remote_terminal_failures: M6NonnegativeInt

    @model_validator(mode="after")
    def empty_class_has_no_measure(self) -> Self:
        if self.n == 0 and any(item is not None for item in (
            self.admission_to_remote_accepted, self.remote_accepted_to_publication_committed,
            self.publication_committed_to_public_confirmation, self.admission_to_public_confirmation,
            self.remote_post_to_terminal, self.terminal_to_public_confirmation,
        )):
            raise ValueError("an empty size class cannot report a latency measure")
        if self.n == 0 and (self.remote_samples or self.public_samples
                            or self.remote_resends or self.remote_terminal_failures):
            raise ValueError("an empty size class cannot report a stage sample")
        if (self.remote_samples > self.n or self.public_samples > self.n
                or self.public_samples > self.remote_samples):
            raise ValueError("stage samples exceed the attempts that could produce them")
        if (self.remote_samples == 0) != (self.remote_post_to_terminal is None):
            raise ValueError("a remote measure exists exactly when the class produced a remote sample")
        if (self.public_samples == 0) != (self.terminal_to_public_confirmation is None):
            raise ValueError("a public measure exists exactly when the class produced a public sample")
        return self


class M6LatencyClasses(M6ClosedModel):
    short: M6LatencyClass
    medium: M6LatencyClass
    long: M6LatencyClass


class M6LatencyGateResult(M6ClosedModel):
    status: Literal["pass", "fail", "unknown"]
    failed: M6ReasonList

    @model_validator(mode="after")
    def failures_named(self) -> Self:
        if self.failed != tuple(sorted(set(self.failed))):
            raise ValueError("failed latency gates must be sorted and unique")
        if (self.status == "fail") != bool(self.failed):
            raise ValueError("a failed latency gate status must name its gates")
        return self


class M6LatencyClocks(M6ClosedModel):
    """The two clock domains a measure may be stamped by; they are never subtracted from each other."""

    owner: Literal["owner_qpc_received_ticks"] = "owner_qpc_received_ticks"
    stage: Literal["mac_monotonic_ns"] = "mac_monotonic_ns"


class M6StageTiming(M6ClosedModel):
    """Whether the Mac stage observation could time this run at all, and why not.

    `measured` requires a complete, lossless observation, one agreed monotonic
    clock binding across the runner and verifier processes, and at least one
    attempt whose remote episode was timed end to end.
    """

    status: Literal["measured", "unknown"]
    reasons: M6ReasonList
    observation_status: Literal["complete", "partial", "invalid"] | None
    clock_binding_sha256: M6Hash | None
    attempts_required: M6NonnegativeInt
    attempts_measured: M6NonnegativeInt
    notes_total: M6NonnegativeInt

    @model_validator(mode="after")
    def unknown_names_its_reason(self) -> Self:
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValueError("stage timing reasons must be sorted and unique")
        if (self.status == "unknown") != bool(self.reasons):
            raise ValueError("an unknown stage timing must name every reason it could not measure")
        if self.status == "measured" and (
            self.clock_binding_sha256 is None or self.observation_status != "complete"
            or self.attempts_measured == 0
        ):
            raise ValueError("a measured stage timing requires a bound clock and a timed attempt")
        if self.attempts_measured > self.attempts_required:
            raise ValueError("more attempts were timed than the journal required")
        return self


class M6LatencyBySize(M6ClosedModel):
    clocks: M6LatencyClocks = M6LatencyClocks()
    classes: M6LatencyClasses
    stage_timing: M6StageTiming
    gates: M6LatencyGateResult


class M6DeliveryReport(M6ClosedModel):
    """One frozen plan scored against one run's immutable evidence."""

    contract_version: Literal["m6.delivery-report.v1"] = "m6.delivery-report.v1"
    run_validity: M6RunValidity
    business_obligations_closed: M6BusinessObligationsClosed
    publication_qualified: M6PublicationQualified
    main_window: M6DeliveryMainWindow
    whole_run: M6WholeRun
    drain: M6Drain
    service_diagnostic: M6ServiceDiagnostic | None
    resource_safety: M6ResourceSafety
    latency_by_size: M6LatencyBySize
    unknowns: M6ReasonList
    delivery_pass: bool
    evidence: M6DeliveryEvidence

    @model_validator(mode="after")
    def pass_requires_every_proof(self) -> Self:
        if self.unknowns != tuple(sorted(set(self.unknowns))):
            raise ValueError("unknown proofs must be sorted and unique")
        if self.delivery_pass and (
            self.run_validity.status != "complete"
            or not self.business_obligations_closed.all_closed
            or self.resource_safety.status == "fail"
            or self.latency_by_size.gates.status == "fail"
        ):
            raise ValueError("a passing delivery requires a complete run with every obligation closed")
        if self.publication_qualified.documents != len(self.publication_qualified.sources):
            raise ValueError("qualified documents must be one per deduplicated source")
        if self.main_window.documents > self.publication_qualified.documents:
            raise ValueError("the main window cannot credit more documents than the run qualified")
        return self


__all__ = [
    "M6_DELIVERY_REPORT_CONTRACT", "M6_DELIVERY_REPORT_MAX_BYTES",
    "M6BusinessObligationsClosed", "M6DeliveryEvidence", "M6DeliveryMainWindow", "M6DeliveryReport", "M6Drain",
    "M6EvidenceInput", "M6LatencyBySize", "M6LatencyClass", "M6LatencyClasses", "M6LatencyClocks",
    "M6LatencyGateResult", "M6LatencyMeasure", "M6OwnershipClosed", "M6PublicationQualified",
    "M6QualifiedSource", "M6Rate", "M6ReasonList", "M6ResourceSafety", "M6RunValidity", "M6Seconds",
    "M6ServiceDiagnostic", "M6StageTiming", "M6SubWindow", "M6WholeRun", "M6WindowExclusions",
]
