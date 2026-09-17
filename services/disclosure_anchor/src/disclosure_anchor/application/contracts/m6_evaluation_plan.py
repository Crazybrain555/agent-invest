"""Frozen M6 evaluation plan: the scoring window and gates bound into the intent before any admission.

The plan never changes readiness or admission semantics (those stay in
`m6_run_accounting.reduce_m6_run`); it only fixes, ahead of the run, which
owner-clock interval is the main delivery window, how it is sub-divided, which
size buckets latency is reported by, and which gates a delivery must meet.
Its canonical bytes are its identity (`evaluation_plan_sha256` in the intent).
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from disclosure_anchor.application.contracts.m6_campaign import M6Mode
from disclosure_anchor.application.contracts.m6_common import M6ClosedModel

M6_EVALUATION_PLAN_CONTRACT = "m6.evaluation-plan.v1"
M6_DELIVERY_METRICS_VERSION = "m6.delivery-metrics.v1"
M6_EVALUATION_PLAN_MAX_BYTES = 65536


class M6MainWindow(M6ClosedModel):
    start_offset_seconds: Annotated[int, Field(strict=True, ge=0, le=86_400)]
    end_offset_seconds: Annotated[int, Field(strict=True, ge=1, le=86_400)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_offset_seconds <= self.start_offset_seconds:
            raise ValueError("main window must end after it starts")
        return self


class M6ReadinessRule(M6ClosedModel):
    # The reducer's ready tick: max(document_qualified, publication_committed, public_confirmation) received ticks.
    rule: Literal["max_qualified_committed_confirmed"]
    max_observations: Annotated[int, Field(strict=True, ge=1, le=3)]


class M6CreditRule(M6ClosedModel):
    # Fixed by the accounting contract; the plan states them so a report is self-describing.
    replay_excluded: Literal[True]
    carry_in_excluded: Literal[True]
    not_first_excluded: Literal[True]
    late_backfill: Literal[False]


class M6SizeClasses(M6ClosedModel):
    short_max_pages: Annotated[int, Field(strict=True, ge=1, le=100_000)]
    medium_max_pages: Annotated[int, Field(strict=True, ge=2, le=100_000)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.medium_max_pages <= self.short_max_pages:
            raise ValueError("medium bucket must end after the short bucket")
        return self


class M6DeliveryGates(M6ClosedModel):
    main_docs_per_hour_min: Annotated[int, Field(strict=True, ge=1, le=100_000)]
    main_pages_per_min_min: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]


class M6LatencyGates(M6ClosedModel):
    short_p95_s: Annotated[int, Field(strict=True, ge=1, le=86_400)]
    short_max_s: Annotated[int, Field(strict=True, ge=1, le=86_400)]
    long_p95_s: Annotated[int, Field(strict=True, ge=1, le=86_400)]
    long_max_s: Annotated[int, Field(strict=True, ge=1, le=86_400)]
    remote_to_public_p95_s: Annotated[int, Field(strict=True, ge=1, le=86_400)]
    remote_to_public_max_s: Annotated[int, Field(strict=True, ge=1, le=86_400)]
    admission_to_public_max_s: Annotated[int, Field(strict=True, ge=1, le=86_400)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if (self.short_p95_s > self.short_max_s or self.long_p95_s > self.long_max_s
                or self.remote_to_public_p95_s > self.remote_to_public_max_s):
            raise ValueError("p95 gates cannot exceed their max gates")
        return self


class M6ResourceGates(M6ClosedModel):
    gpu_free_min_bytes: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
    oom_max: Literal[0]
    preemption_max: Literal[0]


class M6EvaluationPlan(M6ClosedModel):
    contract_version: Literal["m6.evaluation-plan.v1"] = "m6.evaluation-plan.v1"
    mode: M6Mode
    metrics_version: Literal["m6.delivery-metrics.v1"] = "m6.delivery-metrics.v1"
    main_window: M6MainWindow
    sub_window_seconds: Annotated[int, Field(strict=True, ge=60, le=86_400)]
    readiness: M6ReadinessRule
    credit: M6CreditRule
    size_classes: M6SizeClasses
    delivery_gates: M6DeliveryGates | None
    latency_gates: M6LatencyGates | None
    resource_gates: M6ResourceGates | None

    @model_validator(mode="after")
    def windows_divide(self) -> Self:
        span = self.main_window.end_offset_seconds - self.main_window.start_offset_seconds
        if span % self.sub_window_seconds != 0:
            raise ValueError("sub windows must tile the main window exactly")
        return self

    @property
    def sub_window_count(self) -> int:
        return (self.main_window.end_offset_seconds - self.main_window.start_offset_seconds) // self.sub_window_seconds


__all__ = [
    "M6_DELIVERY_METRICS_VERSION", "M6_EVALUATION_PLAN_CONTRACT", "M6_EVALUATION_PLAN_MAX_BYTES",
    "M6CreditRule", "M6DeliveryGates", "M6EvaluationPlan", "M6LatencyGates", "M6MainWindow", "M6ReadinessRule",
    "M6ResourceGates", "M6SizeClasses",
]
