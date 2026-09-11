"""Independently authored M6 fixtures: synthetic identities, journals and reducer glue.

Everything here is a placeholder. No production PDF, PostgreSQL row, Windows
host or credential byte is represented. Hashes are labelled digests, the QPC
frequency is a synthetic 10 MHz, and every journal is built by the test in the
physical append order it wants the reader to see.

The only production logic reused for *construction* is `canonical_json_sha256`
for the clock-domain binding (a model cannot be instantiated without it) and
the contract models themselves. Expected receipt totals are never computed by
the reducer under test; tests hand-derive them from the scenario.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib

from disclosure_anchor.application.contracts.m6_campaign import (
    M6CampaignScope, M6CorpusEntry, M6CorpusManifest, M6Mode,
)
from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6CheckId, M6CheckResult, M6QualificationEvidence,
    M6QualificationObservation, M6QualityPlan, M6ReasonPolicy, M6ReviewRecord,
)
from disclosure_anchor.application.contracts.m6_run import (
    M6ClockDomain, M6ResourceEnvelope, M6RunReceipt, M6RunSpec, M6RuntimeIdentity,
    M6SourceHistoryFact,
)
from disclosure_anchor.application.contracts.m6_run_events import (
    M6AdmissionControl, M6AttemptAdmitted, M6AttemptFinal, M6DocumentQualified,
    M6EventPayload, M6MeasurementIncident, M6OwnerResumed, M6OwnerStamp, M6ProducerEvent,
    M6ProducerKind, M6PublicationCommitted, M6PublicConfirmation, M6RemoteAccepted,
    M6ResourcesClosed, M6RunClosed, M6RunEvent, M6RunStarted, M6ServiceValidated,
    M6VerifierDrained,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import canonical_json_sha256
from disclosure_anchor.application.services.m6_run_accounting import reduce_m6_run


QPC_HZ = 10_000_000
T0 = 5_000_000
PLANNED_SECONDS = 60
CLOSE_GRACE_SECONDS = 30
STOP_BUDGET_SECONDS = 5

OWNER_EPOCH = "sha256:" + hashlib.sha256(b"m6-synthetic:owner-epoch-1").hexdigest()
OWNER_EPOCH_2 = "sha256:" + hashlib.sha256(b"m6-synthetic:owner-epoch-2").hexdigest()
RUNNER_EPOCH = "sha256:" + hashlib.sha256(b"m6-synthetic:runner-epoch-1").hexdigest()
RUNNER_EPOCH_2 = "sha256:" + hashlib.sha256(b"m6-synthetic:runner-epoch-2").hexdigest()
PUBLIC_EPOCH = "sha256:" + hashlib.sha256(b"m6-synthetic:public-verifier-epoch-1").hexdigest()
QUALITY_EPOCH = "sha256:" + hashlib.sha256(b"m6-synthetic:quality-verifier-epoch-1").hexdigest()

# The twelve contract invariants are independent test inputs, not an import of
# the production registry: dropping one in production must still fail a test.
SERVICE_CHECKS: tuple[M6CheckId, ...] = (
    "artifact_closure", "block_conservation", "finding_binding", "heading_occurrence_closure",
    "independent_rebuild_match", "logical_table_conservation", "page_closure", "reading_order_contiguity",
    "repair_binding", "retrieval_target_binding", "source_identity", "table_segment_conservation",
)
E2E_CHECKS: tuple[M6CheckId, ...] = tuple(sorted((*SERVICE_CHECKS, "public_units_hash_match")))


def digest(label: str) -> str:
    """Deterministic labelled placeholder hash in M6Hash form."""
    return "sha256:" + hashlib.sha256(("m6-synthetic:" + label).encode("utf-8")).hexdigest()


def hex_digest(label: str) -> str:
    return hashlib.sha256(("m6-synthetic:" + label).encode("utf-8")).hexdigest()


def sha256_of(payload: bytes) -> str:
    """Independent digest of exact bytes, for journal prefix expectations."""
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def line_of(record: M6RunEvent) -> bytes:
    return record.canonical_bytes() + b"\n"


def lines_of(records: Iterable[M6RunEvent]) -> tuple[bytes, ...]:
    return tuple(line_of(record) for record in records)


# --- identities -------------------------------------------------------------

def clock_domain(boot_label: str = "boot-a", *, frequency_hz: int = QPC_HZ) -> M6ClockDomain:
    boot = digest("boot:" + boot_label)
    return M6ClockDomain(
        host_assignment_identity_sha256=digest("host-assignment"),
        boot_identity_sha256=boot,
        qpc_frequency_hz=frequency_hz,
        clock_domain_identity_sha256=canonical_json_sha256({
            "boot_identity_sha256": boot, "clock_source": "QueryPerformanceCounter",
            "frequency_hz": frequency_hz,
        }),
    )


def envelope(**overrides: int) -> M6ResourceEnvelope:
    values: dict[str, int] = {
        "max_events": 200, "max_record_bytes": 8192, "max_log_bytes": 2_000_000,
        "max_attempts": 20, "max_verifier_backlog_bytes": 1_000_000,
        "stop_admission_budget_ticks": STOP_BUDGET_SECONDS * QPC_HZ,
    }
    values.update(overrides)
    return M6ResourceEnvelope(**values)


def runtime_identity(mode: M6Mode, *, profile_label: str = "profile-1") -> M6RuntimeIdentity:
    return M6RuntimeIdentity(
        source_commit=hashlib.sha1(b"m6-synthetic:commit").hexdigest(),
        source_manifest_sha256=digest("source-manifest"),
        runtime_bundle_identity_sha256=digest("runtime-bundle"),
        process_profile_sha256=digest("process-profile:" + profile_label),
        worker_profile_sha256=digest("worker-profile") if mode == "e2e_publication" else None,
        owner_source_sha256=digest("owner-source"),
        gpu_device_identity_sha256=digest("gpu-device"),
        deployment_qualification_sha256=digest("deployment-qualification"),
    )


def entry(
    label: str, *, pages: int, mode: M6Mode, origin: str = "fresh",
    byte_count: int | None = None, stratum: str = "native",
) -> M6CorpusEntry:
    return M6CorpusEntry(
        source_pdf_sha256=digest("source-pdf:" + label),
        source_byte_count=byte_count if byte_count is not None else 1000 + pages * 4096,
        source_page_count=pages,
        document_id=("doc-" + label) if mode == "e2e_publication" else None,
        stratum=stratum, origin=origin,  # type: ignore[arg-type]
    )


def manifest(mode: M6Mode, *entries: M6CorpusEntry, campaign_id: str = "campaign-1") -> M6CorpusManifest:
    ordered = tuple(sorted(entries, key=lambda item: item.source_pdf_sha256))
    return M6CorpusManifest(campaign_id=campaign_id, mode=mode, entries=ordered)


def quality_plan(mode: M6Mode, *policies: tuple[str, str]) -> M6QualityPlan:
    return M6QualityPlan(
        mode=mode,
        required_checks=E2E_CHECKS if mode == "e2e_publication" else SERVICE_CHECKS,
        reason_policies=tuple(
            M6ReasonPolicy(reason=reason, disposition=disposition)  # type: ignore[arg-type]
            for reason, disposition in sorted(policies)
        ),
    )


def run_spec(
    *, manifest: M6CorpusManifest, plan: M6QualityPlan, phase: str = "short_batch",
    planned_seconds: int = PLANNED_SECONDS, t0: int = T0, carry_in: tuple[str, ...] = (),
    boot_label: str = "boot-a", resources: M6ResourceEnvelope | None = None,
    run_id: str = "run-1", close_grace_seconds: int = CLOSE_GRACE_SECONDS,
    start_condition: str = "cold", profile_label: str = "profile-1",
) -> M6RunSpec:
    clock = clock_domain(boot_label)
    deadline = t0 + planned_seconds * clock.qpc_frequency_hz
    scope = (
        M6CampaignScope.from_manifest(manifest).canonical_sha256()
        if manifest.mode == "e2e_publication" else None
    )
    return M6RunSpec(
        run_id=run_id, campaign_id=manifest.campaign_id, mode=manifest.mode,
        phase=phase, start_condition=start_condition,  # type: ignore[arg-type]
        clock=clock, runtime=runtime_identity(manifest.mode, profile_label=profile_label),
        manifest_sha256=manifest.canonical_sha256(), scope_sha256=scope,
        quality_plan_sha256=plan.canonical_sha256(), t0_ticks=t0,
        planned_seconds=planned_seconds, deadline_ticks=deadline,
        max_close_ticks=deadline + close_grace_seconds * clock.qpc_frequency_hz,
        carry_in_attempt_ids=tuple(carry_in), resources=resources or envelope(),
    )


@dataclass(frozen=True)
class RunFixture:
    spec: M6RunSpec
    manifest: M6CorpusManifest
    plan: M6QualityPlan
    entries: dict[str, M6CorpusEntry]

    def at(self, seconds: float) -> int:
        """QPC tick `seconds` after T0 (exact for multiples of 1e-7 s)."""
        return self.spec.t0_ticks + round(seconds * self.spec.clock.qpc_frequency_hz)

    @property
    def deadline(self) -> int:
        return self.spec.deadline_ticks


def make_fixture(
    mode: M6Mode, entries: dict[str, tuple[int, str]] | None = None, *,
    policies: tuple[tuple[str, str], ...] = (), **spec_kwargs: object,
) -> RunFixture:
    """`entries` maps a label to (page_count, origin)."""
    if entries is None:
        entries = {"a": (7, "fresh")}
    built = {label: entry(label, pages=pages, mode=mode, origin=origin)
             for label, (pages, origin) in entries.items()}
    corpus = manifest(mode, *built.values())
    plan = quality_plan(mode, *policies)
    spec = run_spec(manifest=corpus, plan=plan, **spec_kwargs)  # type: ignore[arg-type]
    return RunFixture(spec=spec, manifest=corpus, plan=plan, entries=built)


# --- owner journal ----------------------------------------------------------

class Journal:
    """Builds owner-stamped records in exactly the order the test appends them.

    Producer sequences are tracked per (kind, incarnation). Owner sequence and
    stamps are assigned by this builder; tests override any of them to craft
    adversarial evidence.
    """

    def __init__(self, fixture: RunFixture, *, owner_epoch: str = OWNER_EPOCH) -> None:
        self.fixture = fixture
        self.spec = fixture.spec
        self.spec_sha = fixture.spec.canonical_sha256()
        self.owner_epoch = owner_epoch
        self.records: list[M6RunEvent] = []
        self.sequence = 0
        self.producer_sequences: dict[tuple[str, str], int] = {}
        self.runner_kind: M6ProducerKind = (
            "e2e_runner" if fixture.spec.mode == "e2e_publication" else "service_runner"
        )

    # -- primitives --
    def at(self, seconds: float) -> int:
        return self.fixture.at(seconds)

    def event(
        self, kind: M6ProducerKind, epoch: str, payload: M6EventPayload, *,
        sequence: int | None = None, run_id: str | None = None, spec_sha: str | None = None,
    ) -> M6ProducerEvent:
        key = (kind, epoch)
        if sequence is None:
            sequence = self.producer_sequences.get(key, 0) + 1
        self.producer_sequences[key] = max(self.producer_sequences.get(key, 0), sequence)
        return M6ProducerEvent(
            run_id=run_id if run_id is not None else self.spec.run_id,
            spec_sha256=spec_sha if spec_sha is not None else self.spec_sha,
            producer_kind=kind, producer_epoch_sha256=epoch, producer_sequence=sequence,
            payload=payload,
        )

    def stamp(
        self, event: M6ProducerEvent, tick: int, *, sequence: int | None = None,
        owner_epoch: str | None = None, boot: str | None = None, record: bool = True,
    ) -> M6RunEvent:
        if sequence is None:
            sequence = self.sequence + 1
        if record:
            self.sequence = max(self.sequence, sequence)
        stamped = M6RunEvent(event=event, stamp=M6OwnerStamp(
            sequence=sequence, received_qpc_ticks=tick,
            boot_identity_sha256=boot if boot is not None else self.spec.clock.boot_identity_sha256,
            owner_process_epoch_sha256=owner_epoch if owner_epoch is not None else self.owner_epoch,
            producer_event_sha256=event.canonical_sha256(),
        ))
        if record:
            self.records.append(stamped)
        return stamped

    def append(
        self, kind: M6ProducerKind, epoch: str, payload: M6EventPayload, tick: int,
        **stamp_kwargs: object,
    ) -> M6RunEvent:
        return self.stamp(self.event(kind, epoch, payload), tick, **stamp_kwargs)  # type: ignore[arg-type]

    def owner(self, payload: M6EventPayload, tick: int, **stamp_kwargs: object) -> M6RunEvent:
        return self.append("owner", self.owner_epoch, payload, tick, **stamp_kwargs)

    def runner(self, payload: M6EventPayload, tick: int, *, epoch: str = RUNNER_EPOCH,
               **stamp_kwargs: object) -> M6RunEvent:
        return self.append(self.runner_kind, epoch, payload, tick, **stamp_kwargs)

    def public(self, payload: M6EventPayload, tick: int, **stamp_kwargs: object) -> M6RunEvent:
        return self.append("public_verifier", PUBLIC_EPOCH, payload, tick, **stamp_kwargs)

    def quality(self, payload: M6EventPayload, tick: int, **stamp_kwargs: object) -> M6RunEvent:
        return self.append("quality_verifier", QUALITY_EPOCH, payload, tick, **stamp_kwargs)

    def lines(self) -> tuple[bytes, ...]:
        return lines_of(self.records)

    def prefix_sha256(self) -> str:
        return sha256_of(b"".join(self.lines()))

    # -- owner lifecycle --
    def start(self, tick: int | None = None) -> M6RunEvent:
        return self.owner(M6RunStarted(
            clock=self.spec.clock, t0_ticks=self.spec.t0_ticks, deadline_ticks=self.spec.deadline_ticks,
        ), self.spec.t0_ticks if tick is None else tick)

    def opened(self, tick: int) -> M6RunEvent:
        return self.owner(M6AdmissionControl(kind="admission_opened"), tick)

    def stop_requested(self, tick: int) -> M6RunEvent:
        return self.owner(M6AdmissionControl(kind="stop_admission_requested"), tick)

    def stop_effective(self, tick: int) -> M6RunEvent:
        return self.owner(M6AdmissionControl(kind="stop_admission_effective"), tick)

    def drained(self, tick: int, *, label: str = "drain-1") -> M6RunEvent:
        payload = M6VerifierDrained(drain_receipt_sha256=digest("drain:" + label))
        # The reader binds drain to the verifier of the mode; see AUTHORSHIP.md.
        if self.spec.mode == "e2e_publication":
            return self.public(payload, tick)
        return self.quality(payload, tick)

    def resources_closed(self, tick: int, *, residual: int = 0, children_exited: bool = True) -> M6RunEvent:
        return self.owner(M6ResourcesClosed(
            residual_count=residual, children_exited=children_exited,
            ownership_receipt_sha256=digest("ownership-receipt"),
        ), tick)

    def closed(self, tick: int, *, reason: str = "deadline_drained", tclose: int | None = None) -> M6RunEvent:
        return self.owner(M6RunClosed(
            tclose_ticks=tick if tclose is None else tclose, reason=reason,  # type: ignore[arg-type]
        ), tick)

    def incident(self, tick: int, code: str = "residual_claims") -> M6RunEvent:
        return self.owner(M6MeasurementIncident(code=code, evidence_sha256=digest("incident:" + code)), tick)

    def resumed(self, tick: int, *, new_epoch: str = OWNER_EPOCH_2, previous: str | None = None,
                t0: int | None = None, deadline: int | None = None, clock: M6ClockDomain | None = None,
                **stamp_kwargs: object) -> M6RunEvent:
        payload = M6OwnerResumed(
            clock=clock or self.spec.clock,
            t0_ticks=self.spec.t0_ticks if t0 is None else t0,
            deadline_ticks=self.spec.deadline_ticks if deadline is None else deadline,
            previous_owner_epoch_sha256=previous if previous is not None else self.owner_epoch,
        )
        self.owner_epoch = new_epoch
        return self.append("owner", new_epoch, payload, tick, **stamp_kwargs)

    def close_run(self, *, stop_at: float, drain_at: float | None = None, close_at: float | None = None,
                  reason: str = "deadline_drained", request_at: float | None = None,
                  residual: int = 0, children_exited: bool = True) -> None:
        """Ordinary full closure: request/effective stop, drain, resources, close."""
        request = stop_at if request_at is None else request_at
        self.stop_requested(self.at(request))
        self.stop_effective(self.at(stop_at + 1))
        self.drained(self.at(stop_at + 2 if drain_at is None else drain_at))
        self.resources_closed(self.at(stop_at + 3 if close_at is None else close_at - 1),
                              residual=residual, children_exited=children_exited)
        self.closed(self.at(stop_at + 4 if close_at is None else close_at), reason=reason)

    # -- attempts --
    def admission(self, source: M6CorpusEntry, attempt_id: str, *,
                  processing_run_id: str | None = None, **overrides: object) -> M6AttemptAdmitted:
        """Admission payload only; nothing is appended."""
        if processing_run_id is None and self.spec.mode == "e2e_publication":
            processing_run_id = "prun-" + attempt_id
        values: dict[str, object] = {
            "attempt_id": attempt_id, "fence_identity": "fence-" + attempt_id,
            "document_id": source.document_id, "processing_run_id": processing_run_id,
            "source_pdf_sha256": source.source_pdf_sha256,
            "source_byte_count": source.source_byte_count,
            "source_page_count": source.source_page_count,
            "process_profile_sha256": self.spec.runtime.process_profile_sha256,
        }
        values.update(overrides)
        return M6AttemptAdmitted(**values)  # type: ignore[arg-type]

    def admit(self, source: M6CorpusEntry, attempt_id: str, tick: int, *,
              processing_run_id: str | None = None, epoch: str = RUNNER_EPOCH,
              sequence: int | None = None, **overrides: object) -> M6AttemptAdmitted:
        payload = self.admission(source, attempt_id, processing_run_id=processing_run_id, **overrides)
        self.runner(payload, tick, epoch=epoch, sequence=sequence)
        return payload

    def accept(self, attempt_id: str, tick: int, *, epoch: str = RUNNER_EPOCH) -> M6RemoteAccepted:
        payload = M6RemoteAccepted(
            attempt_id=attempt_id, remote_task_identity_sha256=digest("task:" + attempt_id),
            acceptance_receipt_sha256=digest("acceptance:" + attempt_id),
        )
        self.runner(payload, tick, epoch=epoch)
        return payload

    def commit(self, admission: M6AttemptAdmitted, tick: int, *, ledger_seq: int,
               epoch: str = RUNNER_EPOCH, **overrides: object) -> M6PublicationCommitted:
        assert admission.processing_run_id is not None and admission.document_id is not None
        values: dict[str, object] = {
            "attempt_id": admission.attempt_id, "processing_run_id": admission.processing_run_id,
            "document_id": admission.document_id, "source_pdf_sha256": admission.source_pdf_sha256,
            "source_page_count": admission.source_page_count, "ledger_seq": ledger_seq,
            "winner_sha256": digest("winner:" + admission.attempt_id),
            "durable_base_sha256": digest("base:" + admission.attempt_id),
        }
        values.update(overrides)
        payload = M6PublicationCommitted(**values)  # type: ignore[arg-type]
        self.runner(payload, tick, epoch=epoch)
        return payload

    def confirm(self, admission: M6AttemptAdmitted, tick: int, *, ledger_seq: int,
                **overrides: object) -> M6PublicConfirmation:
        assert admission.processing_run_id is not None and admission.document_id is not None
        attempt_id = admission.attempt_id
        values: dict[str, object] = {
            "attempt_id": attempt_id, "processing_run_id": admission.processing_run_id,
            "document_id": admission.document_id, "source_pdf_sha256": admission.source_pdf_sha256,
            "source_page_count": admission.source_page_count, "ledger_seq": ledger_seq,
            "winner_sha256": digest("winner:" + attempt_id),
            "durable_base_sha256": digest("base:" + attempt_id),
            "public_units_sha256": public_units_for(attempt_id),
            "artifact_closure_sha256": digest("artifact-closure:" + attempt_id),
            "consumer_check_receipt_sha256": digest("consumer-check:" + attempt_id),
            "history_audit_receipt_sha256": audit_for(attempt_id),
            "verifier_identity": "public-verifier-1",
        }
        values.update(overrides)
        payload = M6PublicConfirmation(**values)  # type: ignore[arg-type]
        self.public(payload, tick)
        return payload

    def qualify(self, attempt_id: str, evidence: M6QualificationEvidence | str, tick: int) -> M6DocumentQualified:
        sha = evidence if isinstance(evidence, str) else evidence.canonical_sha256()
        payload = M6DocumentQualified(attempt_id=attempt_id, qualification_evidence_sha256=sha)
        self.quality(payload, tick)
        return payload

    def validate(self, attempt_id: str, tick: int, *, bundle: str | None = None,
                 epoch: str = RUNNER_EPOCH) -> M6ServiceValidated:
        payload = M6ServiceValidated(
            attempt_id=attempt_id,
            provider_bundle_sha256=bundle or bundle_for(attempt_id),
            validation_receipt_sha256=digest("validation:" + attempt_id),
        )
        self.runner(payload, tick, epoch=epoch)
        return payload

    def final(self, attempt_id: str, tick: int, *, outcome: str | None = None,
              disposition: str = "consumed", epoch: str = RUNNER_EPOCH,
              owner_sequence: int | None = None,
              **overrides: object) -> M6AttemptFinal:
        if outcome is None:
            outcome = "published" if self.spec.mode == "e2e_publication" else "diagnostic_disposed"
        submitted = disposition != "not_submitted"
        values: dict[str, object] = {
            "attempt_id": attempt_id, "outcome": outcome, "remote_disposition": disposition,
            "remote_receipt_sha256": digest("remote-receipt:" + attempt_id) if submitted else None,
            "remote_task_identity_sha256": digest("task:" + attempt_id) if submitted else None,
            "cleanup_receipt_sha256": digest("cleanup:" + attempt_id),
        }
        values.update(overrides)
        payload = M6AttemptFinal(**values)  # type: ignore[arg-type]
        self.runner(payload, tick, epoch=epoch, sequence=owner_sequence)
        return payload


def public_units_for(attempt_id: str) -> str:
    return digest("public-units:" + attempt_id)


def audit_for(attempt_id: str) -> str:
    return digest("history-audit:" + attempt_id)


def bundle_for(attempt_id: str) -> str:
    return digest("provider-bundle:" + attempt_id)


# --- qualification evidence and history --------------------------------------

def check_results(
    checks: tuple[M6CheckId, ...] = E2E_CHECKS, *, failing: tuple[str, ...] = (),
    unverified: tuple[str, ...] = (), omit: tuple[str, ...] = (),
) -> tuple[M6CheckResult, ...]:
    results = []
    for check in sorted(checks):
        if check in omit:
            continue
        outcome = "fail" if check in failing else "unverified" if check in unverified else "pass"
        results.append(M6CheckResult(
            check_id=check, outcome=outcome,  # type: ignore[arg-type]
            evidence_sha256=None if outcome == "unverified" else digest("check-evidence:" + check),
        ))
    return tuple(results)


def observation(
    source: M6CorpusEntry, mode: M6Mode, *, attempt_id: str, provider_pages: int | None = None,
    unit_count: int = 12, unusable: int = 0, needs_review: int = 0,
    review_reasons: tuple[str, ...] = (), checks: tuple[M6CheckResult, ...] | None = None,
    **overrides: object,
) -> M6QualificationObservation:
    e2e = mode == "e2e_publication"
    values: dict[str, object] = {
        "mode": mode, "source_pdf_sha256": source.source_pdf_sha256,
        "source_byte_count": source.source_byte_count, "source_page_count": source.source_page_count,
        "provider_page_count": source.source_page_count if provider_pages is None else provider_pages,
        "processing_run_id": ("prun-" + attempt_id) if e2e else None,
        "provider_bundle_sha256": bundle_for(attempt_id),
        "provider_document_sha256": digest("provider-document:" + attempt_id),
        "public_units_sha256": public_units_for(attempt_id) if e2e else None,
        "unit_count": unit_count, "unusable_unit_count": unusable,
        "needs_review_unit_count": needs_review, "review_reasons": tuple(sorted(review_reasons)),
        "checks": checks if checks is not None else check_results(E2E_CHECKS if e2e else SERVICE_CHECKS),
    }
    values.update(overrides)
    return M6QualificationObservation(**values)  # type: ignore[arg-type]


def review(obs: M6QualificationObservation, reason: str, decision: str, *,
           reviewer: str = "reviewer-1") -> M6ReviewRecord:
    return M6ReviewRecord(
        observation_sha256=obs.canonical_sha256(), reason=reason, reviewer_identity=reviewer,
        decision=decision, evidence_sha256=digest("review:" + reason + ":" + reviewer),  # type: ignore[arg-type]
    )


def evidence(obs: M6QualificationObservation, *reviews: M6ReviewRecord) -> M6QualificationEvidence:
    return M6QualificationEvidence(observation=obs, reviews=tuple(reviews))


def qualification_for(source: M6CorpusEntry, mode: M6Mode, attempt_id: str, **kwargs: object) -> M6QualificationEvidence:
    return evidence(observation(source, mode, attempt_id=attempt_id, **kwargs))  # type: ignore[arg-type]


def history_fact(
    source: M6CorpusEntry, *, attempt_id: str, ledger_seq: int, scan_complete: bool = True,
    variants: int = 1, first_pages: int | None = None, first_run: str | None = None,
    audit: str | None = None,
) -> M6SourceHistoryFact:
    return M6SourceHistoryFact(
        source_pdf_sha256=source.source_pdf_sha256, scan_complete=scan_complete,
        first_processing_run_id=first_run if first_run is not None else "prun-" + attempt_id,
        first_ledger_seq=ledger_seq,
        first_source_page_count=source.source_page_count if first_pages is None else first_pages,
        source_page_variants=variants,
        audit_receipt_sha256=audit if audit is not None else audit_for(attempt_id),
    )


def unknown_history(source: M6CorpusEntry, *, attempt_id: str) -> M6SourceHistoryFact:
    """History whose scan is incomplete and found nothing: never proves novelty."""
    return M6SourceHistoryFact(
        source_pdf_sha256=source.source_pdf_sha256, scan_complete=False,
        first_processing_run_id=None, first_ledger_seq=None, first_source_page_count=None,
        source_page_variants=0, audit_receipt_sha256=audit_for(attempt_id),
    )


# --- reducer glue -----------------------------------------------------------

def reduce(
    fixture: RunFixture, journal: Journal | Iterable[bytes], *,
    history: tuple[M6SourceHistoryFact, ...] = (),
    qualifications: tuple[M6QualificationEvidence, ...] = (),
) -> M6RunReceipt:
    lines = journal.lines() if isinstance(journal, Journal) else journal
    return reduce_m6_run(
        spec=fixture.spec, manifest=fixture.manifest, quality_plan=fixture.plan,
        journal_lines=lines, history=history, qualifications=qualifications,
    )


def outcomes(receipt: M6RunReceipt) -> dict[str, str]:
    return {line.attempt_id: line.outcome for line in receipt.sources}


def e2e_publish(
    journal: Journal, source: M6CorpusEntry, attempt_id: str, *, admit_at: float,
    ledger_seq: int, step: float = 1.0, final_at: float | None = None,
) -> tuple[M6AttemptAdmitted, M6QualificationEvidence, M6SourceHistoryFact]:
    """Full successful E2E attempt in one contiguous physical block."""
    admission = journal.admit(source, attempt_id, journal.at(admit_at))
    journal.accept(attempt_id, journal.at(admit_at + step))
    journal.commit(admission, journal.at(admit_at + 2 * step), ledger_seq=ledger_seq)
    journal.confirm(admission, journal.at(admit_at + 3 * step), ledger_seq=ledger_seq)
    proof = qualification_for(source, "e2e_publication", attempt_id)
    journal.qualify(attempt_id, proof, journal.at(admit_at + 4 * step))
    journal.final(attempt_id, journal.at(admit_at + 5 * step if final_at is None else final_at))
    return admission, proof, history_fact(source, attempt_id=attempt_id, ledger_seq=ledger_seq)


def service_validate(
    journal: Journal, source: M6CorpusEntry, attempt_id: str, *, admit_at: float, step: float = 1.0,
    epoch: str = RUNNER_EPOCH,
) -> tuple[M6AttemptAdmitted, M6QualificationEvidence]:
    admission = journal.admit(source, attempt_id, journal.at(admit_at), epoch=epoch)
    journal.accept(attempt_id, journal.at(admit_at + step), epoch=epoch)
    journal.validate(attempt_id, journal.at(admit_at + 2 * step), epoch=epoch)
    proof = qualification_for(source, "service_diagnostic", attempt_id)
    journal.qualify(attempt_id, proof, journal.at(admit_at + 3 * step))
    journal.final(attempt_id, journal.at(admit_at + 4 * step), epoch=epoch)
    return admission, proof
