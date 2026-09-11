"""Whole-document qualification from independently collected, bound evidence.

Evidence digests are integrity references. Authenticating the verifier and
reading/rebuilding the actual artifacts remains an adapter qualification gate.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from disclosure_anchor.application.contracts.m6_campaign import M6Mode
from disclosure_anchor.application.contracts.m6_common import (
    M6ClosedModel, M6Hash, M6Id, M6NonnegativeInt, M6PositiveInt, M6Reason,
)


M6CheckId = Literal[
    "source_identity", "page_closure", "block_conservation",
    "table_segment_conservation", "logical_table_conservation",
    "retrieval_target_binding", "repair_binding", "finding_binding",
    "reading_order_contiguity", "heading_occurrence_closure", "artifact_closure",
    "independent_rebuild_match", "public_units_hash_match",
]
M6_SERVICE_CHECKS: tuple[M6CheckId, ...] = (
    "source_identity", "page_closure", "block_conservation", "table_segment_conservation",
    "logical_table_conservation", "retrieval_target_binding", "repair_binding",
    "finding_binding", "reading_order_contiguity", "heading_occurrence_closure",
    "artifact_closure", "independent_rebuild_match",
)
M6Verdict = Literal["scorable", "review_pending", "not_scorable"]


class M6CheckResult(M6ClosedModel):
    check_id: M6CheckId
    outcome: Literal["pass", "fail", "unverified"]
    evidence_sha256: M6Hash | None

    @model_validator(mode="after")
    def evidence_required(self) -> Self:
        if self.outcome != "unverified" and self.evidence_sha256 is None:
            raise ValueError("verified check requires evidence bytes")
        return self


class M6ReasonPolicy(M6ClosedModel):
    reason: M6Id
    disposition: Literal["score_hard_fail", "review_required", "accepted_noncritical"]


class M6QualityPlan(M6ClosedModel):
    contract_version: Literal["m6.quality-plan.v1"] = "m6.quality-plan.v1"
    mode: M6Mode
    required_checks: Annotated[tuple[M6CheckId, ...], Field(min_length=1, max_length=13)]
    reason_policies: Annotated[tuple[M6ReasonPolicy, ...], Field(max_length=256)]

    @model_validator(mode="after")
    def invariant_checks(self) -> Self:
        if self.required_checks != tuple(sorted(set(self.required_checks))):
            raise ValueError("quality checks must be sorted and unique")
        required = set(M6_SERVICE_CHECKS)
        if self.mode == "e2e_publication":
            required.add("public_units_hash_match")
        if not required.issubset(self.required_checks):
            raise ValueError("quality plan cannot omit whole-document invariants")
        reasons = tuple(item.reason for item in self.reason_policies)
        if reasons != tuple(sorted(set(reasons))):
            raise ValueError("quality reason policies must be sorted and unique")
        return self


class M6QualificationObservation(M6ClosedModel):
    mode: M6Mode
    source_pdf_sha256: M6Hash
    source_byte_count: M6PositiveInt
    source_page_count: M6PositiveInt
    provider_page_count: M6PositiveInt
    processing_run_id: M6Id | None
    provider_bundle_sha256: M6Hash
    provider_document_sha256: M6Hash
    public_units_sha256: M6Hash | None
    unit_count: M6NonnegativeInt
    unusable_unit_count: M6NonnegativeInt
    needs_review_unit_count: M6NonnegativeInt
    review_reasons: Annotated[tuple[M6Id, ...], Field(max_length=256)]
    checks: Annotated[tuple[M6CheckResult, ...], Field(max_length=13)]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.mode == "e2e_publication") != (self.processing_run_id is not None):
            raise ValueError("qualification processing run differs from mode")
        if (self.mode == "e2e_publication") != (self.public_units_sha256 is not None):
            raise ValueError("qualification public units differ from mode")
        if self.unusable_unit_count + self.needs_review_unit_count > self.unit_count:
            raise ValueError("quality counts exceed unit count")
        if self.review_reasons != tuple(sorted(set(self.review_reasons))):
            raise ValueError("review reasons must be sorted and unique")
        checks = tuple(item.check_id for item in self.checks)
        if checks != tuple(sorted(set(checks))):
            raise ValueError("check results must be sorted and unique")
        return self


class M6ReviewRecord(M6ClosedModel):
    observation_sha256: M6Hash
    reason: M6Id
    reviewer_identity: M6Id
    decision: Literal["accept", "reject"]
    evidence_sha256: M6Hash


class M6QualificationEvidence(M6ClosedModel):
    contract_version: Literal["m6.qualification-evidence.v1"] = "m6.qualification-evidence.v1"
    observation: M6QualificationObservation
    reviews: Annotated[tuple[M6ReviewRecord, ...], Field(max_length=256)]

    @model_validator(mode="after")
    def review_binding(self) -> Self:
        keys = tuple((item.reason, item.reviewer_identity) for item in self.reviews)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("reviews must be ordered and unique per reason/reviewer")
        digest = self.observation.canonical_sha256()
        if any(item.observation_sha256 != digest for item in self.reviews):
            raise ValueError("review belongs to different observation")
        if any(item.reason not in self.observation.review_reasons for item in self.reviews):
            raise ValueError("review reason absent from observation")
        return self


class M6DocumentQualification(M6ClosedModel):
    contract_version: Literal["m6.document-qualification.v1"] = "m6.document-qualification.v1"
    evidence_sha256: M6Hash
    plan_sha256: M6Hash
    verdict: M6Verdict
    reasons: tuple[M6Reason, ...]
    scorable_page_count: M6PositiveInt | None

    @model_validator(mode="after")
    def trusted_projection(self) -> Self:
        if (self.verdict == "scorable") != (self.scorable_page_count is not None):
            raise ValueError("only a scorable document exposes qualified pages")
        if (self.verdict == "scorable") != (not self.reasons):
            raise ValueError("qualification reasons disagree with verdict")
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValueError("qualification reasons must be ordered and unique")
        return self


def qualify_document(
    evidence: M6QualificationEvidence, plan: M6QualityPlan,
) -> M6DocumentQualification:
    observation = evidence.observation
    if observation.mode != plan.mode:
        raise ValueError("qualification plan mode mismatch")
    failures: set[str] = set()
    pending: set[str] = set()
    if observation.provider_page_count != observation.source_page_count:
        failures.add("page_count_mismatch")
    checks = {item.check_id: item for item in observation.checks}
    for check_id in plan.required_checks:
        check = checks.get(check_id)
        if check is None:
            failures.add("required_check_missing:" + check_id)
        elif check.outcome != "pass":
            failures.add("check_" + check.outcome + ":" + check_id)
    if observation.unusable_unit_count:
        failures.add("unusable_units_present")
    if observation.needs_review_unit_count and not observation.review_reasons:
        pending.add("needs_review_reason_missing")
    policies = {item.reason: item.disposition for item in plan.reason_policies}
    for reason in observation.review_reasons:
        disposition = policies.get(reason)
        reviews = tuple(item for item in evidence.reviews if item.reason == reason)
        if disposition == "score_hard_fail" or any(item.decision == "reject" for item in reviews):
            failures.add("review_rejected:" + reason)
        elif disposition is None:
            # A future/unknown reason is not silently accepted by a historical plan.
            pending.add("unplanned_reason:" + reason)
        elif disposition == "review_required" and not any(
            item.decision == "accept" for item in reviews
        ):
            pending.add("review_pending:" + reason)
    # Blank pages and heading-only units are not quality failures by themselves.
    # Source/page/structure checks must still establish their full conservation.
    verdict: M6Verdict = "not_scorable" if failures else "review_pending" if pending else "scorable"
    return M6DocumentQualification(
        evidence_sha256=evidence.canonical_sha256(), plan_sha256=plan.canonical_sha256(),
        verdict=verdict, reasons=tuple(sorted(failures | pending)),
        scorable_page_count=observation.source_page_count if verdict == "scorable" else None,
    )
