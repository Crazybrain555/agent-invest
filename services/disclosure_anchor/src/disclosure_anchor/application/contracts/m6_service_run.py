"""Source/provider integrity run projection; no Unit or public-page authority."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

from disclosure_anchor.application.contracts.m6_common import (
    M6ClosedModel, M6Hash, M6Id, M6NonnegativeInt, M6PositiveInt, M6Reason,
)
from disclosure_anchor.application.contracts.m6_run import M6SourceOutcome


class M6ServiceProviderIntegrityMetrics(M6ClosedModel):
    kind: Literal["service_provider_integrity_source_pages"] = "service_provider_integrity_source_pages"
    window_pages: M6NonnegativeInt
    whole_run_pages: M6NonnegativeInt
    replay_pages: M6NonnegativeInt
    carry_in_pages: M6NonnegativeInt

    @model_validator(mode="after")
    def page_subsets(self) -> Self:
        if self.window_pages > self.whole_run_pages or self.replay_pages > self.whole_run_pages:
            raise ValueError("service integrity page subsets exceed whole-run pages")
        return self


class M6ServiceRunReceipt(M6ClosedModel):
    contract_version: Literal["m6.service-run-receipt.v1"] = "m6.service-run-receipt.v1"
    run_id: M6Id
    spec_sha256: M6Hash
    journal_prefix_sha256: M6Hash
    journal_bytes_consumed: M6NonnegativeInt
    mode: Literal["service_diagnostic"]
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
    metrics: M6ServiceProviderIntegrityMetrics | None
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
        if self.elapsed_ticks is not None and (
            self.tclose_ticks is None or self.elapsed_ticks != self.tclose_ticks - self.t0_ticks
        ):
            raise ValueError("elapsed ticks differ from whole-run interval")
        if self.status == "complete" and (self.elapsed_ticks is None or self.elapsed_ticks <= 0):
            raise ValueError("complete run requires a positive whole-run interval")
        return self
