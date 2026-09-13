"""Replay service integrity events with the original physical-owner state machine.

The two producer roles must reference the same full qualification evidence.
Artifact IO and native execution remain responsibilities of the fixed adapter;
this pure projection neither reconstructs them nor grants publication credit.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from disclosure_anchor.application.contracts.m6_campaign import M6CorpusManifest
from disclosure_anchor.application.contracts.m6_run import M6RunSpec, M6SourceOutcome
from disclosure_anchor.application.contracts.m6_run_events import (
    M6AttemptFinal, M6DocumentQualified, M6ServiceValidated,
)
from disclosure_anchor.application.contracts.m6_service_quality import (
    M6ServiceQualificationEvidence, M6ServiceQualityPlan, qualify_service_document,
)
from disclosure_anchor.application.contracts.m6_service_run import (
    M6ServiceProviderIntegrityMetrics, M6ServiceRunReceipt,
)
from disclosure_anchor.application.services.m6_run_accounting import (
    _Attempt, _Replay, _eligible_sources, _replay_journal,
)


def _service_source_outcome(
    replay: _Replay, attempt: _Attempt, plan: M6ServiceQualityPlan,
    evidence: dict[str, M6ServiceQualificationEvidence],
) -> M6SourceOutcome:
    admission = attempt.admission
    source = admission.source_pdf_sha256
    outcome: Literal[
        "credited_window", "credited_whole_run_only", "carry_in", "quality_not_scorable",
        "quality_review_pending", "confirmation_missing", "failed", "qualification_missing",
        "admitted_after_deadline",
    ] = "confirmation_missing"
    ready_tick: int | None = None

    def line() -> M6SourceOutcome:
        return M6SourceOutcome(
            source_pdf_sha256=source, attempt_id=admission.attempt_id, outcome=outcome,
            page_count=admission.source_page_count, ready_received_ticks=ready_tick,
            admission_received_ticks=attempt.admission_received_ticks,
        )

    if source not in replay.entries:
        return line()
    qualified = attempt.facts.get("document_qualified")
    valid = attempt.facts.get("service_validated")
    qualification_evidence: M6ServiceQualificationEvidence | None = None
    # An actual contradictory reference cannot be hidden by a failed terminal
    # or a non-scorable quality verdict. Missing, unissued facts stay missing.
    if qualified is not None:
        payload = qualified.payload
        assert isinstance(payload, M6DocumentQualified)
        if valid is not None:
            validated = valid.payload
            assert isinstance(validated, M6ServiceValidated)
            if validated.validation_receipt_sha256 != payload.qualification_evidence_sha256:
                replay.invalid.add("service_validation_evidence_mismatch")
        qualification_evidence = evidence.get(payload.qualification_evidence_sha256)
        if qualification_evidence is None:
            replay.incomplete.add("qualification_evidence_missing")
            outcome = "qualification_missing"
            return line()
        observation = qualification_evidence.observation
        conflicting = False
        if (observation.attempt_id, observation.source_pdf_sha256, observation.source_byte_count,
            observation.source_page_count, observation.mode) != (
            admission.attempt_id, source, admission.source_byte_count,
            admission.source_page_count, replay.spec.mode,
        ):
            replay.invalid.add("qualification_source_mismatch")
            conflicting = True
        if (observation.parser_target_sha256 != plan.parser_target_sha256
                or observation.quality_verifier_sha256 != plan.quality_verifier_sha256):
            replay.invalid.add("service_quality_identity_mismatch")
            conflicting = True
        if valid is not None:
            validated = valid.payload
            assert isinstance(validated, M6ServiceValidated)
            if validated.provider_bundle_sha256 != observation.provider_bundle_sha256:
                replay.invalid.add("service_provider_bundle_mismatch")
                conflicting = True
            if validated.validation_receipt_sha256 != payload.qualification_evidence_sha256:
                conflicting = True
        if conflicting:
            outcome = "quality_not_scorable"
            return line()

    final = attempt.facts.get("attempt_final")
    if final is not None:
        final_payload = final.payload
        assert isinstance(final_payload, M6AttemptFinal)
        if final_payload.outcome in {"failed", "superseded"}:
            outcome = "failed"
            return line()
    if qualified is None or qualification_evidence is None:
        outcome = "qualification_missing"
        return line()
    qualification = qualify_service_document(qualification_evidence, plan)
    if qualification.verdict != "scorable":
        outcome = "quality_review_pending" if qualification.verdict == "review_pending" else "quality_not_scorable"
        return line()
    if valid is None:
        return line()
    ready_tick = max(qualified.received_ticks, valid.received_ticks)
    if admission.attempt_id in replay.carry_in:
        outcome = "carry_in"
    elif attempt.admission_received_ticks >= replay.spec.deadline_ticks:
        outcome = "admitted_after_deadline"
    else:
        outcome = "credited_window" if ready_tick < replay.spec.deadline_ticks else "credited_whole_run_only"
    return line()


def reduce_m6_service_run(
    *, spec: M6RunSpec, manifest: M6CorpusManifest, quality_plan: M6ServiceQualityPlan,
    journal_lines: Iterable[bytes], qualifications: tuple[M6ServiceQualificationEvidence, ...],
) -> M6ServiceRunReceipt:
    """Count full sources within the original owner interval after all duties close."""
    if (type(spec) is not M6RunSpec or type(manifest) is not M6CorpusManifest
            or type(quality_plan) is not M6ServiceQualityPlan or type(qualifications) is not tuple
            or any(type(item) is not M6ServiceQualificationEvidence for item in qualifications)):
        raise ValueError("service run requires its exact input and qualification families")
    if (spec.mode != "service_diagnostic" or manifest.mode != spec.mode or quality_plan.mode != spec.mode
            or spec.manifest_sha256 != manifest.canonical_sha256()
            or spec.campaign_id != manifest.campaign_id
            or spec.quality_plan_sha256 != quality_plan.canonical_sha256()):
        raise ValueError("service run inputs differ from frozen spec")
    if len(qualifications) > spec.resources.max_attempts:
        raise ValueError("service evidence exceeds declared bounded scope")
    evidence_by_sha = {item.canonical_sha256(): item for item in qualifications}
    if len(evidence_by_sha) != len(qualifications):
        raise ValueError("duplicate service evidence identity")
    replay, prefix_sha, consumed_bytes, count = _replay_journal(spec, manifest, journal_lines)
    lines = tuple(_service_source_outcome(replay, attempt, quality_plan, evidence_by_sha)
                  for _, attempt in sorted(replay.attempts.items()))
    eligible, carry_in = _eligible_sources(lines)
    status: Literal["complete", "incomplete", "invalid"] = (
        "invalid" if replay.invalid else "incomplete" if replay.incomplete else "complete"
    )
    metrics = None
    if status == "complete":
        metrics = M6ServiceProviderIntegrityMetrics(
            window_pages=sum(line.page_count for line in eligible.values() if line.outcome == "credited_window"),
            whole_run_pages=sum(line.page_count for line in eligible.values()),
            replay_pages=sum(line.page_count for line in eligible.values()
                             if replay.entries[line.source_pdf_sha256].origin == "replay"),
            carry_in_pages=sum(carry_in.values()),
        )
    elapsed = None if replay.cross_boot or replay.closed is None or replay.closed < spec.t0_ticks else replay.closed - spec.t0_ticks
    return M6ServiceRunReceipt(
        run_id=spec.run_id, spec_sha256=spec.canonical_sha256(), journal_prefix_sha256=prefix_sha,
        journal_bytes_consumed=consumed_bytes, mode="service_diagnostic", phase=spec.phase, status=status,
        incomplete_reasons=tuple(sorted(replay.incomplete)), invalid_reasons=tuple(sorted(replay.invalid)),
        t0_ticks=spec.t0_ticks, deadline_ticks=spec.deadline_ticks, tclose_ticks=replay.closed,
        elapsed_ticks=elapsed, qpc_frequency_hz=spec.clock.qpc_frequency_hz,
        stop_requested_ticks=replay.stop_requested, stop_effective_ticks=replay.stop_effective,
        close_reason=replay.close_reason, metrics=metrics, sources=lines,
        events_total=count, duplicate_events=replay.duplicates,
    )
