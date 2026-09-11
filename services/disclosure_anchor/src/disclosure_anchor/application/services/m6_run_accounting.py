"""Bounded replay of the physical owner's ordered journal; no clock or IO calls.

Producer arrival may be out of business order. Physical owner records may not:
sorting a damaged journal would hide gaps, clock regressions and recovery cost.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import hashlib
from typing import Literal

from disclosure_anchor.application.contracts.m6_campaign import M6CampaignScope, M6CorpusManifest
from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6QualificationEvidence, M6QualityPlan, qualify_document,
)
from disclosure_anchor.application.contracts.m6_run import (
    M6PublicationMetrics, M6RunReceipt, M6RunSpec, M6ServiceMetrics, M6SourceHistoryFact,
    M6SourceOutcome,
)
from disclosure_anchor.application.contracts.m6_run_events import (
    M6AdmissionControl, M6AttemptAdmitted, M6AttemptFinal, M6DocumentQualified,
    M6MeasurementIncident, M6OwnerResumed, M6PublicationCommitted, M6PublicConfirmation,
    M6RemoteAccepted, M6ResourcesClosed, M6RunClosed, M6RunEvent, M6RunStarted,
    M6ServiceValidated, M6VerifierDrained,
    M6EventPayload,
)


@dataclass(frozen=True, slots=True)
class _Fact:
    payload: M6EventPayload
    received_ticks: int


@dataclass
class _Attempt:
    admission: M6AttemptAdmitted
    admission_received_ticks: int
    facts: dict[str, _Fact] = field(default_factory=dict)


_OWNER_KINDS = frozenset({
    "run_started", "owner_resumed", "admission_opened", "stop_admission_requested",
    "stop_admission_effective", "resources_closed", "run_closed", "measurement_incident",
})


class _Replay:
    def __init__(self, spec: M6RunSpec, manifest: M6CorpusManifest) -> None:
        self.spec = spec
        self.spec_sha = spec.canonical_sha256()
        self.entries = {entry.source_pdf_sha256: entry for entry in manifest.entries}
        self.carry_in = frozenset(spec.carry_in_attempt_ids)
        self.invalid: set[str] = set()
        self.incomplete: set[str] = set()
        self.attempts: dict[str, _Attempt] = {}
        self.owner_records: dict[int, str] = {}
        self.producer_records: dict[tuple[str, str, int], str] = {}
        self.producer_sequences: dict[tuple[str, str], int] = {}
        self.producer_counts: dict[tuple[str, str], int] = {}
        self.last_sequence = 0
        self.last_tick = spec.t0_ticks
        self.owner_epoch: str | None = None
        self.started = False
        self.opened = False
        self.stopped = False
        self.stop_requested: int | None = None
        self.stop_effective: int | None = None
        self.verifier_drained = False
        self.resources_closed = False
        self.resources_observed = False
        self.closed: int | None = None
        self.close_reason: Literal["deadline_drained", "stop_requested", "failed"] | None = None
        self.cross_boot = False
        self.duplicates = 0

    def consume(self, record: M6RunEvent) -> None:
        event, stamp = record.event, record.stamp
        if event.run_id != self.spec.run_id or event.spec_sha256 != self.spec_sha:
            self.invalid.add("run_or_spec_mismatch")
            return
        record_sha = record.canonical_sha256()
        previous = self.owner_records.get(stamp.sequence)
        if previous is not None:
            if previous == record_sha:
                self.duplicates += 1
            else:
                self.invalid.add("owner_sequence_conflict")
            return
        self.owner_records[stamp.sequence] = record_sha
        if self.closed is not None:
            self.invalid.add("event_after_close")
            if stamp.boot_identity_sha256 != self.spec.clock.boot_identity_sha256:
                self.cross_boot = True
                self.incomplete.add("boot_identity_changed")
            return
        if stamp.sequence <= self.last_sequence:
            self.invalid.add("owner_record_reordered")
        elif stamp.sequence != self.last_sequence + 1:
            self.incomplete.add("event_gap")
        self.last_sequence = stamp.sequence
        if stamp.boot_identity_sha256 != self.spec.clock.boot_identity_sha256:
            self.cross_boot = True
            self.incomplete.add("boot_identity_changed")
            return  # Never subtract or credit QPC observations from another boot.
        if stamp.received_qpc_ticks < self.last_tick:
            self.invalid.add("clock_regression")
        self.last_tick = stamp.received_qpc_ticks
        payload = event.payload
        if self.owner_epoch is None:
            self.owner_epoch = stamp.owner_process_epoch_sha256
        elif stamp.owner_process_epoch_sha256 != self.owner_epoch:
            if not isinstance(payload, M6OwnerResumed):
                self.invalid.add("owner_epoch_changed_without_resume")
                return
        if event.producer_kind == "owner" and event.producer_epoch_sha256 != stamp.owner_process_epoch_sha256:
            self.invalid.add("owner_producer_epoch_mismatch")
        producer = (event.producer_kind, event.producer_epoch_sha256)
        key = (*producer, event.producer_sequence)
        previous = self.producer_records.get(key)
        if previous is not None:
            if previous == stamp.producer_event_sha256:
                self.duplicates += 1
            else:
                self.invalid.add("producer_event_conflict")
            return
        self.producer_records[key] = stamp.producer_event_sha256
        self.producer_counts[producer] = self.producer_counts.get(producer, 0) + 1
        # Network arrival order is not producer order; gaps are checked at EOF.
        self.producer_sequences[producer] = max(
            self.producer_sequences.get(producer, 0), event.producer_sequence,
        )
        if not self.started and not isinstance(payload, M6RunStarted):
            self.invalid.add("run_start_missing")
            return
        expected_producer = "owner" if payload.kind in _OWNER_KINDS else (
            "public_verifier" if isinstance(payload, M6PublicConfirmation) else
            "quality_verifier" if isinstance(payload, M6DocumentQualified) else
            "public_verifier" if isinstance(payload, M6VerifierDrained)
            and self.spec.mode == "e2e_publication" else
            "quality_verifier" if isinstance(payload, M6VerifierDrained) else
            "e2e_runner" if self.spec.mode == "e2e_publication" else "service_runner"
        )
        if event.producer_kind != expected_producer:
            self.invalid.add("event_producer_role_mismatch")
            return
        if isinstance(payload, (M6RunStarted, M6OwnerResumed)):
            if (payload.clock != self.spec.clock or payload.t0_ticks != self.spec.t0_ticks
                    or payload.deadline_ticks != self.spec.deadline_ticks):
                self.invalid.add("original_clock_or_deadline_drift")
            if isinstance(payload, M6RunStarted):
                if self.started or stamp.sequence != 1 or stamp.received_qpc_ticks != self.spec.t0_ticks:
                    self.invalid.add("invalid_run_start")
                self.started = True
            else:
                if (payload.previous_owner_epoch_sha256 != self.owner_epoch
                        or stamp.owner_process_epoch_sha256 == self.owner_epoch):
                    self.invalid.add("invalid_owner_resume")
                self.owner_epoch = stamp.owner_process_epoch_sha256
        elif isinstance(payload, M6AdmissionControl):
            self.admission_control(payload, stamp.received_qpc_ticks)
        elif isinstance(payload, M6AttemptAdmitted):
            self.admit(payload, stamp.received_qpc_ticks)
        elif isinstance(payload, M6VerifierDrained):
            if not self.stopped:
                self.incomplete.add("verifier_drained_before_stop")
            self.verifier_drained = True
        elif isinstance(payload, M6ResourcesClosed):
            if self.resources_observed:
                self.invalid.add("duplicate_resources_closed")
                return
            self.resources_observed = True
            self.resources_closed = payload.residual_count == 0 and payload.children_exited
            if not self.resources_closed:
                self.incomplete.add("residual_resources")
            if not self.verifier_drained or any("attempt_final" not in a.facts for a in self.attempts.values()):
                self.incomplete.add("resources_closed_before_drain")
        elif isinstance(payload, M6RunClosed):
            self.closed = payload.tclose_ticks
            self.close_reason = payload.reason
            if payload.tclose_ticks <= self.spec.t0_ticks:
                self.invalid.add("zero_length_or_negative_run")
            if payload.tclose_ticks != stamp.received_qpc_ticks:
                self.invalid.add("close_tick_not_owner_received")
            if payload.tclose_ticks > self.spec.max_close_ticks:
                self.incomplete.add("close_budget_exceeded")
            if payload.reason == "failed":
                self.incomplete.add("run_failed")
            if (self.spec.phase in {"hour_baseline", "stability_repeat"}
                    and payload.tclose_ticks < self.spec.deadline_ticks):
                self.incomplete.add("formal_interval_not_covered")
        elif isinstance(payload, M6MeasurementIncident):
            self.incomplete.add("measurement_incident:" + payload.code)
        else:
            self.attempt_fact(record)

    def admission_control(self, payload: M6AdmissionControl, tick: int) -> None:
        if payload.kind == "admission_opened":
            if self.opened or self.stopped or tick >= self.spec.deadline_ticks:
                self.invalid.add("invalid_admission_open")
            self.opened = True
        elif payload.kind == "stop_admission_requested":
            if self.stop_requested is None:
                self.stop_requested = tick
        else:
            if self.stopped:
                self.invalid.add("duplicate_admission_stop")
            self.stopped = True
            self.stop_effective = tick
            due = min(self.stop_requested, self.spec.deadline_ticks) if self.stop_requested is not None else self.spec.deadline_ticks
            if tick > due + self.spec.resources.stop_admission_budget_ticks:
                self.incomplete.add("stop_admission_budget_exceeded")

    def admit(self, payload: M6AttemptAdmitted, tick: int) -> None:
        if payload.attempt_id in self.attempts:
            self.invalid.add("duplicate_attempt_admission")
            return
        if len(self.attempts) >= self.spec.resources.max_attempts:
            self.incomplete.add("attempt_bound_exceeded")
            return
        carry_in = payload.attempt_id in self.carry_in
        if not carry_in and (not self.opened or self.stopped):
            self.invalid.add("admission_outside_window")
        if not carry_in:
            due = min(self.stop_requested, self.spec.deadline_ticks) if self.stop_requested is not None else self.spec.deadline_ticks
            if tick > due + self.spec.resources.stop_admission_budget_ticks:
                self.incomplete.add("stop_admission_budget_exceeded")
        if payload.process_profile_sha256 != self.spec.runtime.process_profile_sha256:
            self.invalid.add("profile_drift")
        entry = self.entries.get(payload.source_pdf_sha256)
        if entry is None or (payload.document_id, payload.source_byte_count, payload.source_page_count) != (
            entry.document_id, entry.source_byte_count, entry.source_page_count,
        ):
            self.invalid.add("admission_source_not_in_manifest")
        if (self.spec.mode == "e2e_publication") != (payload.processing_run_id is not None):
            self.invalid.add("admission_run_mode_mismatch")
        if entry is not None and (entry.origin == "carry_in") != carry_in:
            self.invalid.add("carry_in_manifest_mismatch")
        if self.verifier_drained or self.resources_closed:
            self.invalid.add("admission_after_drain")
        self.attempts[payload.attempt_id] = _Attempt(payload, tick)

    def attempt_fact(self, record: M6RunEvent) -> None:
        payload = record.event.payload
        if not isinstance(payload, (
            M6RemoteAccepted, M6PublicationCommitted, M6PublicConfirmation, M6DocumentQualified,
            M6ServiceValidated, M6AttemptFinal,
        )):
            raise AssertionError("closed event dispatch is incomplete")
        if self.verifier_drained or self.resources_closed:
            self.invalid.add("attempt_evidence_after_drain")
        if isinstance(payload, (M6PublicationCommitted, M6PublicConfirmation)) and self.spec.mode != "e2e_publication":
            self.invalid.add("publication_in_service_mode")
            return
        if isinstance(payload, M6ServiceValidated) and self.spec.mode != "service_diagnostic":
            self.invalid.add("service_validation_in_publication_mode")
            return
        attempt = self.attempts.get(payload.attempt_id)
        if attempt is None:
            self.invalid.add("attempt_not_admitted")
            return
        previous = attempt.facts.get(payload.kind)
        if previous is not None:
            if previous.payload != payload:
                self.invalid.add("attempt_fact_conflict")
            else:
                self.duplicates += 1
            return
        attempt.facts[payload.kind] = _Fact(payload, record.stamp.received_qpc_ticks)

    def finish(self) -> None:
        for producer, maximum in self.producer_sequences.items():
            count = self.producer_counts[producer]
            if count != maximum:
                self.incomplete.add("producer_sequence_gap")
        if not self.started:
            self.incomplete.add("run_not_started")
        if not self.stopped:
            self.incomplete.add("admission_not_stopped")
        if not self.verifier_drained:
            self.incomplete.add("verifier_not_drained")
        if not self.resources_closed:
            self.incomplete.add("resources_not_closed")
        if self.closed is None:
            self.incomplete.add("run_not_closed")
        if set(self.spec.carry_in_attempt_ids) - self.attempts.keys():
            self.incomplete.add("carry_in_obligations_missing")
        for attempt in self.attempts.values():
            final = attempt.facts.get("attempt_final")
            if final is None:
                self.incomplete.add("cleanup_or_ack_unresolved")
                continue
            payload = final.payload
            assert isinstance(payload, M6AttemptFinal)
            if "remote_accepted" in attempt.facts and payload.remote_disposition == "not_submitted":
                self.invalid.add("accepted_remote_obligation_discarded")
            accepted = attempt.facts.get("remote_accepted")
            if accepted is not None:
                accepted_payload = accepted.payload
                assert isinstance(accepted_payload, M6RemoteAccepted)
                if accepted_payload.remote_task_identity_sha256 != payload.remote_task_identity_sha256:
                    self.invalid.add("remote_closure_task_mismatch")
            elif payload.outcome in {"published", "diagnostic_disposed"}:
                self.incomplete.add("remote_acceptance_missing")
            if payload.outcome == "published" and self.spec.mode != "e2e_publication":
                self.invalid.add("publication_in_service_mode")
            if payload.outcome == "diagnostic_disposed" and self.spec.mode != "service_diagnostic":
                self.invalid.add("service_disposal_in_publication_mode")


def _source_outcome(
    replay: _Replay, attempt: _Attempt, plan: M6QualityPlan,
    history: dict[str, M6SourceHistoryFact], evidence: dict[str, M6QualificationEvidence],
) -> M6SourceOutcome:
    admission = attempt.admission
    source = admission.source_pdf_sha256
    outcome: Literal[
        "credited_window", "credited_whole_run_only", "carry_in", "replay", "not_first_publish",
        "novelty_unverified", "quality_not_scorable", "quality_review_pending", "confirmation_missing",
        "page_count_conflict", "failed",
        "qualification_missing", "admitted_after_deadline",
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

    final = attempt.facts.get("attempt_final")
    if final is not None:
        final_payload = final.payload
        assert isinstance(final_payload, M6AttemptFinal)
        if final_payload.outcome in {"failed", "superseded"}:
            outcome = "failed"
            return line()
    qualified = attempt.facts.get("document_qualified")
    if qualified is None:
        outcome = "qualification_missing"
        return line()
    payload = qualified.payload
    assert isinstance(payload, M6DocumentQualified)
    qualification_evidence = evidence.get(payload.qualification_evidence_sha256)
    if qualification_evidence is None:
        replay.incomplete.add("qualification_evidence_missing")
        outcome = "qualification_missing"
        return line()
    observation = qualification_evidence.observation
    if (observation.source_pdf_sha256, observation.source_byte_count, observation.source_page_count,
        observation.processing_run_id, observation.mode) != (
        source, admission.source_byte_count, admission.source_page_count,
        admission.processing_run_id, replay.spec.mode,
    ):
        replay.invalid.add("qualification_source_mismatch")
        outcome = "quality_not_scorable"
        return line()
    qualification = qualify_document(qualification_evidence, plan)
    if qualification.verdict != "scorable":
        outcome = "quality_review_pending" if qualification.verdict == "review_pending" else "quality_not_scorable"
        return line()
    if replay.spec.mode == "service_diagnostic":
        valid = attempt.facts.get("service_validated")
        if valid is None:
            return line()
        validated = valid.payload
        assert isinstance(validated, M6ServiceValidated)
        if validated.provider_bundle_sha256 != observation.provider_bundle_sha256:
            replay.invalid.add("service_provider_bundle_mismatch")
            outcome = "quality_not_scorable"
            return line()
        ready_tick = max(qualified.received_ticks, valid.received_ticks)
    else:
        committed = attempt.facts.get("publication_committed")
        confirmed = attempt.facts.get("public_confirmation")
        if committed is None or confirmed is None:
            return line()
        commit, confirmation = committed.payload, confirmed.payload
        assert isinstance(commit, M6PublicationCommitted) and isinstance(confirmation, M6PublicConfirmation)
        if (commit.source_page_count != admission.source_page_count
                or confirmation.source_page_count != admission.source_page_count):
            replay.incomplete.add("page_count_conflict")
            outcome = "page_count_conflict"
            return line()
        if ((commit.document_id, commit.processing_run_id, commit.source_pdf_sha256) != (
            admission.document_id, admission.processing_run_id, source,
        ) or any(getattr(commit, key) != getattr(confirmation, key) for key in (
            "document_id", "processing_run_id", "source_pdf_sha256", "ledger_seq",
            "winner_sha256", "durable_base_sha256",
        ))):
            replay.invalid.add("publication_identity_conflict")
            return line()
        fact = history.get(source)
        if fact is None or not fact.scan_complete or fact.first_ledger_seq is None:
            replay.incomplete.add("history_scan_incomplete")
            outcome = "novelty_unverified"
            return line()
        if (fact.audit_receipt_sha256 != confirmation.history_audit_receipt_sha256
                or observation.public_units_sha256 != confirmation.public_units_sha256):
            replay.invalid.add("public_confirmation_evidence_mismatch")
            return line()
        if fact.source_page_variants != 1 or fact.first_source_page_count != admission.source_page_count:
            replay.incomplete.add("page_count_conflict")
            outcome = "page_count_conflict"
            return line()
        if (fact.first_processing_run_id, fact.first_ledger_seq) != (commit.processing_run_id, commit.ledger_seq):
            outcome = "not_first_publish"
            return line()
        # A late quality decision or ledger observation cannot backfill earlier credit.
        ready_tick = max(qualified.received_ticks, committed.received_ticks, confirmed.received_ticks)
    if admission.attempt_id in replay.carry_in:
        outcome = "carry_in"
    elif attempt.admission_received_ticks >= replay.spec.deadline_ticks:
        outcome = "admitted_after_deadline"
    elif replay.entries[source].origin == "replay" and replay.spec.mode == "e2e_publication":
        outcome = "replay"
    else:
        outcome = "credited_window" if ready_tick < replay.spec.deadline_ticks else "credited_whole_run_only"
    return line()


def reduce_m6_run(
    *, spec: M6RunSpec, manifest: M6CorpusManifest, quality_plan: M6QualityPlan,
    journal_lines: Iterable[bytes], history: tuple[M6SourceHistoryFact, ...],
    qualifications: tuple[M6QualificationEvidence, ...],
) -> M6RunReceipt:
    """Replay LF-terminated canonical records in physical append order.

    The source adapter must use bounded readline(max_record_bytes + 2). The
    reducer additionally limits records, bytes, identities, and attempt state.
    Partial/malformed evidence is retained as an incomplete/invalid receipt;
    programmer, iterator and IO exceptions propagate rather than look complete.
    """
    if (spec.manifest_sha256 != manifest.canonical_sha256()
            or spec.campaign_id != manifest.campaign_id or spec.mode != manifest.mode
            or spec.quality_plan_sha256 != quality_plan.canonical_sha256() or spec.mode != quality_plan.mode):
        raise ValueError("run inputs differ from frozen spec")
    if spec.mode == "e2e_publication" and spec.scope_sha256 != M6CampaignScope.from_manifest(manifest).canonical_sha256():
        raise ValueError("run campaign scope differs from manifest")
    if len(history) > len(manifest.entries) or len(qualifications) > spec.resources.max_attempts:
        raise ValueError("run evidence exceeds declared bounded scope")
    history_by_source = {item.source_pdf_sha256: item for item in history}
    evidence_by_sha = {item.canonical_sha256(): item for item in qualifications}
    if len(history_by_source) != len(history) or len(evidence_by_sha) != len(qualifications):
        raise ValueError("duplicate evidence identity")
    replay = _Replay(spec, manifest)
    digest = hashlib.sha256()
    count = consumed_bytes = 0
    for raw in journal_lines:
        if type(raw) is not bytes:
            raise TypeError("M6 journal source must yield bytes")
        if count >= spec.resources.max_events or consumed_bytes + len(raw) > spec.resources.max_log_bytes or len(raw) > spec.resources.max_record_bytes + 1:
            replay.incomplete.add("event_log_bound_exceeded")
            break
        count += 1
        digest.update(raw)
        consumed_bytes += len(raw)
        if not raw.endswith(b"\n"):
            replay.incomplete.add("event_log_truncated")
            break
        try:
            record = M6RunEvent.from_canonical_bytes(raw[:-1], maximum_bytes=spec.resources.max_record_bytes)
        except ValueError:
            replay.invalid.add("malformed_event_record")
            continue
        replay.consume(record)
    replay.finish()
    lines = tuple(_source_outcome(replay, attempt, quality_plan, history_by_source, evidence_by_sha)
                  for _, attempt in sorted(replay.attempts.items()))
    # Deduplicate eligible full sources, not attempts, profiles or document aliases.
    eligible: dict[str, M6SourceOutcome] = {}
    carry_in: dict[str, int] = {}
    for line in lines:
        if line.outcome == "carry_in":
            carry_in[line.source_pdf_sha256] = line.page_count
        if line.outcome in {"credited_window", "credited_whole_run_only"}:
            previous = eligible.get(line.source_pdf_sha256)
            if previous is None or (line.ready_received_ticks or 0) < (previous.ready_received_ticks or 0):
                eligible[line.source_pdf_sha256] = line
    window = sum(line.page_count for line in eligible.values() if line.outcome == "credited_window")
    whole = sum(line.page_count for line in eligible.values())
    status: Literal["complete", "incomplete", "invalid"] = (
        "invalid" if replay.invalid else "incomplete" if replay.incomplete else "complete"
    )
    metrics: M6PublicationMetrics | M6ServiceMetrics | None = None
    if status == "complete":
        metrics = M6PublicationMetrics(window_pages=window, whole_run_pages=whole, carry_in_pages=sum(carry_in.values())) if spec.mode == "e2e_publication" else M6ServiceMetrics(
            window_pages=window, whole_run_pages=whole, carry_in_pages=sum(carry_in.values()),
            replay_pages=sum(line.page_count for line in eligible.values() if replay.entries[line.source_pdf_sha256].origin == "replay"),
        )
    elapsed = None if replay.cross_boot or replay.closed is None or replay.closed < spec.t0_ticks else replay.closed - spec.t0_ticks
    return M6RunReceipt(
        run_id=spec.run_id, spec_sha256=spec.canonical_sha256(),
        journal_prefix_sha256="sha256:" + digest.hexdigest(), journal_bytes_consumed=consumed_bytes,
        mode=spec.mode, phase=spec.phase, status=status,
        incomplete_reasons=tuple(sorted(replay.incomplete)), invalid_reasons=tuple(sorted(replay.invalid)),
        t0_ticks=spec.t0_ticks, deadline_ticks=spec.deadline_ticks, tclose_ticks=replay.closed,
        elapsed_ticks=elapsed, qpc_frequency_hz=spec.clock.qpc_frequency_hz,
        stop_requested_ticks=replay.stop_requested, stop_effective_ticks=replay.stop_effective,
        close_reason=replay.close_reason,
        metrics=metrics, sources=lines, events_total=count, duplicate_events=replay.duplicates,
    )
