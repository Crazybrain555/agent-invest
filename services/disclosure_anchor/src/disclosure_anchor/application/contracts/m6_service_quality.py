"""Source/provider integrity scope, separate from full Unit qualification v1.

Checks reference observations made by the bound lifecycle. Hashes and these
closed values provide integrity, not proof that a verifier actually ran. The
adapter must establish baseline applicability and validate the original objects.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from disclosure_anchor.application.contracts.diagnostic_json import bounded_json_bytes
from disclosure_anchor.application.contracts.m6_common import (
    M6ClosedModel, M6Hash, M6Id, M6PositiveInt, M6Reason,
)
from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6ReasonPolicy, M6ReviewRecord, M6Verdict,
)


M6ServiceCheckId = Literal[
    "provider_artifact_closure", "provider_content_integrity",
    "provider_page_closure", "source_identity",
]
M6_SERVICE_PROVIDER_CHECKS: tuple[M6ServiceCheckId, ...] = (
    "provider_artifact_closure", "provider_content_integrity",
    "provider_page_closure", "source_identity",
)


class M6ServiceCheckResult(M6ClosedModel):
    check_id: M6ServiceCheckId
    outcome: Literal["pass", "fail", "unverified"]
    evidence_sha256: M6Hash | None

    @model_validator(mode="after")
    def evidence_required(self) -> Self:
        if self.outcome != "unverified" and self.evidence_sha256 is None:
            raise ValueError("verified service check requires evidence bytes")
        return self


class M6ServiceQualityPlan(M6ClosedModel):
    contract_version: Literal["m6.service-quality-plan.v1"] = "m6.service-quality-plan.v1"
    mode: Literal["service_diagnostic"]
    parser_target_sha256: M6Hash
    parser_baseline_evidence_sha256: M6Hash
    quality_verifier_sha256: M6Hash
    required_checks: Annotated[tuple[M6ServiceCheckId, ...], Field(min_length=4, max_length=4)]
    reason_policies: Annotated[tuple[M6ReasonPolicy, ...], Field(max_length=256)]

    @model_validator(mode="after")
    def fixed_scope(self) -> Self:
        if self.required_checks != M6_SERVICE_PROVIDER_CHECKS:
            raise ValueError("service plan requires all four ordered provider/source checks")
        reasons = tuple(item.reason for item in self.reason_policies)
        if reasons != tuple(sorted(set(reasons))):
            raise ValueError("service reason policies must be sorted and unique")
        return self


class M6ServiceQualificationObservation(M6ClosedModel):
    mode: Literal["service_diagnostic"]
    attempt_id: M6Id
    source_pdf_sha256: M6Hash
    source_byte_count: M6PositiveInt
    source_page_count: M6PositiveInt
    provider_page_count: M6PositiveInt
    provider_bundle_sha256: M6Hash
    parser_target_sha256: M6Hash
    quality_verifier_sha256: M6Hash
    source_observed_record_sha256: M6Hash
    output_sealed_record_sha256: M6Hash
    review_reasons: Annotated[tuple[M6Id, ...], Field(max_length=256)]
    checks: Annotated[tuple[M6ServiceCheckResult, ...], Field(max_length=4)]

    @model_validator(mode="after")
    def ordered_observations(self) -> Self:
        if self.review_reasons != tuple(sorted(set(self.review_reasons))):
            raise ValueError("service review reasons must be sorted and unique")
        checks = tuple(item.check_id for item in self.checks)
        if checks != tuple(sorted(set(checks))):
            raise ValueError("service check results must be sorted and unique")
        return self


class M6ServiceQualificationEvidence(M6ClosedModel):
    contract_version: Literal["m6.service-qualification-evidence.v1"] = "m6.service-qualification-evidence.v1"
    observation: M6ServiceQualificationObservation
    reviews: Annotated[tuple[M6ReviewRecord, ...], Field(max_length=256)]

    @model_validator(mode="after")
    def review_binding(self) -> Self:
        keys = tuple((item.reason, item.reviewer_identity) for item in self.reviews)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("service reviews must be ordered and unique per reason/reviewer")
        digest = self.observation.canonical_sha256()
        if any(item.observation_sha256 != digest for item in self.reviews):
            raise ValueError("service review belongs to different observation")
        if any(item.reason not in self.observation.review_reasons for item in self.reviews):
            raise ValueError("service review reason absent from observation")
        return self


class M6ServiceDocumentQualification(M6ClosedModel):
    contract_version: Literal["m6.service-document-qualification.v1"] = "m6.service-document-qualification.v1"
    evidence_sha256: M6Hash
    plan_sha256: M6Hash
    verdict: M6Verdict
    reasons: tuple[M6Reason, ...]
    scorable_page_count: M6PositiveInt | None

    @model_validator(mode="after")
    def scoped_projection(self) -> Self:
        if (self.verdict == "scorable") != (self.scorable_page_count is not None):
            raise ValueError("only a service-qualified document exposes scoped pages")
        if (self.verdict == "scorable") != (not self.reasons):
            raise ValueError("service qualification reasons disagree with verdict")
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValueError("service qualification reasons must be ordered and unique")
        return self


def qualify_service_document(
    evidence: M6ServiceQualificationEvidence, plan: M6ServiceQualityPlan,
) -> M6ServiceDocumentQualification:
    if type(evidence) is not M6ServiceQualificationEvidence or type(plan) is not M6ServiceQualityPlan:
        raise ValueError("service qualification requires its exact evidence and plan family")
    observation = evidence.observation
    if (observation.parser_target_sha256 != plan.parser_target_sha256
            or observation.quality_verifier_sha256 != plan.quality_verifier_sha256):
        raise ValueError("service target or verifier differs from the frozen plan")
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
    policies = {item.reason: item.disposition for item in plan.reason_policies}
    for reason in observation.review_reasons:
        disposition = policies.get(reason)
        reviews = tuple(item for item in evidence.reviews if item.reason == reason)
        if disposition == "score_hard_fail" or any(item.decision == "reject" for item in reviews):
            failures.add("review_rejected:" + reason)
        elif disposition is None:
            pending.add("unplanned_reason:" + reason)
        elif disposition == "review_required" and not any(item.decision == "accept" for item in reviews):
            pending.add("review_pending:" + reason)
    verdict: M6Verdict = "not_scorable" if failures else "review_pending" if pending else "scorable"
    return M6ServiceDocumentQualification(
        evidence_sha256=evidence.canonical_sha256(), plan_sha256=plan.canonical_sha256(),
        verdict=verdict, reasons=tuple(sorted(failures | pending)),
        scorable_page_count=observation.source_page_count if verdict == "scorable" else None,
    )


def verify_service_quality_report(
    report: object, *, plan: M6ServiceQualityPlan,
) -> tuple[M6ServiceQualificationEvidence, M6ServiceDocumentQualification]:
    """Replay a sealed scoped projection; this does not perform artifact IO."""
    maximum = 2 * 1024 * 1024 + 8192
    if type(report) is not dict or set(report) != {"evidence", "qualification"}:
        raise ValueError("service quality report fields are not closed")
    bounded_json_bytes(report, maximum_bytes=maximum)
    evidence = M6ServiceQualificationEvidence.from_canonical_bytes(
        bounded_json_bytes(report["evidence"], maximum_bytes=maximum), maximum_bytes=maximum,
    )
    qualification = M6ServiceDocumentQualification.from_canonical_bytes(
        bounded_json_bytes(report["qualification"], maximum_bytes=maximum), maximum_bytes=maximum,
    )
    expected = qualify_service_document(evidence, plan)
    if qualification.canonical_bytes() != expected.canonical_bytes():
        raise ValueError("service quality report differs from its exact evidence projection")
    return evidence, qualification
