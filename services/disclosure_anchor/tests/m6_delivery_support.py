"""Independent synthetic journals for the frozen M6 delivery interval.

These are raw, owner-ordered facts, not successful report-shaped fixtures.
They certify arithmetic only, never real PDF quality, resource safety or exits.
Expected counts live in the tests and are calculated by hand.
"""

from dataclasses import dataclass
import hashlib
import json

from disclosure_anchor.application.contracts.m6_document_qualification import M6QualificationEvidence
from disclosure_anchor.application.contracts.m6_campaign_intent import M6CampaignIntent
from disclosure_anchor.application.contracts.m6_evaluation_plan import M6EvaluationPlan
from disclosure_anchor.application.contracts.m6_run import M6SourceHistoryFact
from tests import m6_support as m6


@dataclass
class DeliveryCase:
    fixture: m6.RunFixture
    journal: m6.Journal
    history: tuple[M6SourceHistoryFact, ...]
    qualifications: tuple[M6QualificationEvidence, ...]


def evaluation_plan(*, required_safety: bool = False) -> M6EvaluationPlan:
    return M6EvaluationPlan.model_validate({
        "mode": "e2e_publication",
        "main_window": {"start_offset_seconds": 600, "end_offset_seconds": 4200},
        "sub_window_seconds": 1200,
        "readiness": {"rule": "max_qualified_committed_confirmed", "max_observations": 3},
        "credit": {"replay_excluded": True, "carry_in_excluded": True,
                   "not_first_excluded": True, "late_backfill": False},
        "size_classes": {"short_max_pages": 50, "medium_max_pages": 149},
        "delivery_gates": {"main_docs_per_hour_min": 55, "main_pages_per_min_min": 140},
        "latency_gates": ({"short_p95_s": 180, "short_max_s": 300, "long_p95_s": 720,
                           "long_max_s": 900, "remote_to_public_p95_s": 180,
                           "remote_to_public_max_s": 300, "admission_to_public_max_s": 1200}
                          if required_safety else None),
        "resource_gates": ({"gpu_free_min_bytes": 1610612736, "oom_max": 0, "preemption_max": 0}
                           if required_safety else None),
    })


def campaign_intent(case: DeliveryCase, plan: M6EvaluationPlan) -> M6CampaignIntent:
    return campaign_intent_for_spec(case.fixture.spec, plan)


def campaign_intent_for_spec(spec, plan, **overrides) -> M6CampaignIntent:
    spec_fields = ("run_id", "campaign_id", "mode", "phase", "start_condition", "manifest_sha256",
                   "scope_sha256", "quality_plan_sha256", "carry_in_attempt_ids", "planned_seconds", "resources")
    runtime_fields = ("source_commit", "source_manifest_sha256", "runtime_bundle_identity_sha256",
                      "process_profile_sha256", "worker_profile_sha256", "deployment_qualification_sha256")
    values = {
        "evaluation_plan_sha256": plan.canonical_sha256(),
        "run": {name: getattr(spec, name) for name in spec_fields},
        "runtime": {name: getattr(spec.runtime, name) for name in runtime_fields},
        "release_manifest_sha256": m6.digest("delivery-release"),
        "binding_sha256": m6.digest("delivery-binding"),
        "close_grace_seconds": 2400, "memory_bytes": 536870912,
        "bootstrap_bind_seconds": 120, "ready_wait_seconds": 30,
        "runner_stop_reserve_seconds": 60, "verifier_deadline_seconds": 300,
        "verifier_identity": "delivery-test-verifier", "stop_propagation_reserve_ns": 30000000000,
    }
    values.update(overrides)
    return M6CampaignIntent.model_validate(values)


def publication_case(*, threshold: bool = False, omit_final: str | None = None,
                     physical_owner: tuple[int, int] | None = None) -> DeliveryCase:
    """Boundary vector: seven fresh + replay/carry-in/prior-publication sources.

    a is one tick before 600; b is on 600; c/d straddle 1800 by one
    tick; e/f straddle 4200; g's quality arrives after early publication.
    The threshold vector has 55 distinct 155-page sources ready in the main
    interval. It alone does not prove delivery: resource/exit facts are absent.
    """
    hz = m6.QPC_HZ
    if threshold:
        entries = {f"t{i:02d}": (155, "fresh") for i in range(55)}
        ready_offsets = {name: (700 + i * 60) * hz for i, name in enumerate(entries)}
        carry_in = ()
    else:
        entries = {
            "a": (7, "fresh"), "b": (11, "fresh"), "c": (13, "fresh"),
            "d": (17, "fresh"), "e": (19, "fresh"), "f": (23, "fresh"),
            "g": (29, "fresh"), "r": (31, "replay"), "k": (37, "carry_in"),
            "n": (41, "fresh"),
        }
        ready_offsets = {
            "a": 600 * hz - 1, "b": 600 * hz, "c": 1800 * hz - 1,
            "d": 1800 * hz, "e": 4200 * hz - 1, "f": 4200 * hz,
            "g": 4201 * hz, "r": 2000 * hz, "k": 2100 * hz, "n": 2200 * hz,
        }
        carry_in = ("att-k",)
    fixture = m6.make_fixture(
        "e2e_publication", entries, planned_seconds=4800, close_grace_seconds=2400,
        phase="hour_baseline", carry_in=carry_in,
        resources=m6.envelope(max_attempts=100, max_events=1000, max_log_bytes=8_000_000),
    )
    # Independent native ProcessEpoch formula, bound before any journal event
    # exists. Do not splice one real owner's exit into another synthetic run.
    epoch = m6.OWNER_EPOCH
    if physical_owner is not None:
        pid, birth = physical_owner
        identity = {"run_id": fixture.spec.run_id,
                    "owner_source_sha256": fixture.spec.runtime.owner_source_sha256,
                    "boot_identity_sha256": fixture.spec.clock.boot_identity_sha256,
                    "pid": pid, "creation_filetime_100ns": birth}
        raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        epoch = "sha256:" + hashlib.sha256(raw).hexdigest()
    j = m6.Journal(fixture, owner_epoch=epoch)
    j.start()
    j.opened(j.at(1))
    admissions = {}
    proofs = {}
    history = []
    for i, (name, source) in enumerate(fixture.entries.items(), 1):
        attempt = "att-" + name
        admissions[name] = j.admit(source, attempt, j.at(2 + i))
        proofs[name] = m6.qualification_for(source, "e2e_publication", attempt)
        history.append(m6.history_fact(
            source, attempt_id=attempt, ledger_seq=i,
            first_run="prun-previous-n" if name == "n" else None,
        ))
    for i, name in enumerate(entries, 1):
        j.accept("att-" + name, j.at(100 + i))
    for i, name in enumerate(entries, 1):
        j.commit(admissions[name], j.at(200 + i), ledger_seq=i)
        j.confirm(admissions[name], j.at(200 + i), ledger_seq=i)
    for name in sorted(ready_offsets, key=ready_offsets.__getitem__):
        j.qualify("att-" + name, proofs[name], fixture.spec.t0_ticks + ready_offsets[name])
    for name in entries:
        if name != omit_final:
            j.final("att-" + name, j.at(4400))
    j.close_run(stop_at=4800, close_at=4804)
    return DeliveryCase(fixture, j, tuple(history), tuple(proofs.values()))


def empty_report_wire() -> dict:
    """Handwritten unproved report for schema acceptance, not produced by the reporter."""
    latency = {
        "n": 0, "admission_to_remote_accepted": None, "remote_accepted_to_publication_committed": None,
        "publication_committed_to_public_confirmation": None, "admission_to_public_confirmation": None,
        "remote_post_to_terminal": None, "terminal_to_public_confirmation": None,
        "remote_samples": 0, "public_samples": 0, "remote_resends": 0, "remote_terminal_failures": 0,
    }
    return {
        "contract_version": "m6.delivery-report.v1",
        "run_validity": {
            "status": "unknown", "incomplete_reasons": [], "invalid_reasons": [],
            "spec_sha256": m6.digest("spec"), "intent_sha256": None,
            "evaluation_plan_sha256": m6.digest("plan"), "journal_prefix_sha256": None,
            "events_total": 0, "duplicate_events": 0, "owner_epoch_sha256": None,
            "t0_ticks": 0, "deadline_ticks": 4800, "tclose_ticks": None, "qpc_frequency_hz": 1,
        },
        "business_obligations_closed": {
            "all_closed": False, "admitted_attempts": 0, "final_attempts": 0, "attempts_without_final": [],
            "remote_open": [], "ack_or_absence_proven": False, "admission_reconciled": False,
            "ownership_closed": None, "run_closed_reason": None, "external_owner_exit_verified": False,
            "local_children_reaped": False, "missing": ["owner_journal_absent"],
        },
        "publication_qualified": {"documents": 0, "pages": 0, "sources": []},
        "main_window": {
            "start_ticks": 600, "end_ticks": 4200, "span_seconds": 3600,
            "documents": 0, "pages": 0, "documents_per_hour": 0.0, "pages_per_minute": 0.0,
            "sub_windows": [
                {"index": 0, "start_ticks": 600, "end_ticks": 1800, "documents": 0, "pages": 0},
                {"index": 1, "start_ticks": 1800, "end_ticks": 3000, "documents": 0, "pages": 0},
                {"index": 2, "start_ticks": 3000, "end_ticks": 4200, "documents": 0, "pages": 0},
            ],
            "excluded": dict.fromkeys((
                "replay", "carry_in", "not_first_publish", "late_or_after_window", "after_deadline", "failed",
                "quality_not_scorable", "quality_review_pending", "confirmation_missing", "page_count_conflict",
                "novelty_unverified", "qualification_missing"), 0),
        },
        "whole_run": {"documents": 0, "pages": 0, "metrics": None, "elapsed_seconds": None},
        "drain": {"documents": 0, "pages": 0}, "service_diagnostic": None,
        "resource_safety": {"status": "unknown", "gates": None, "evidence_sha256": None,
                            "reason": "resource_telemetry_receipt_absent"},
        "latency_by_size": {"clocks": {"owner": "owner_qpc_received_ticks", "stage": "mac_monotonic_ns"},
                            "classes": {"short": dict(latency), "medium": dict(latency), "long": dict(latency)},
                            "stage_timing": {"status": "unknown", "reasons": ["stage_observation_absent"],
                                             "observation_status": None, "clock_binding_sha256": None,
                                             "attempts_required": 0, "attempts_measured": 0, "notes_total": 0},
                            "gates": {"status": "unknown", "failed": []}},
        "unknowns": ["owner_journal_absent"], "delivery_pass": False, "evidence": {"inputs": []},
    }
