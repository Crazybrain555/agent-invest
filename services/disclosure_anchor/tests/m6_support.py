"""Synthetic identity/journal builders for M6 contract tests, never parser oracles."""

from __future__ import annotations

import hashlib

from disclosure_anchor.application.contracts.m6_campaign import M6CampaignScope, M6CorpusEntry, M6CorpusManifest
from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6CheckResult, M6QualificationEvidence, M6QualificationObservation, M6QualityPlan, M6_SERVICE_CHECKS,
)
from disclosure_anchor.application.contracts.m6_run import (
    M6ClockDomain, M6ResourceEnvelope, M6RunSpec, M6RuntimeIdentity, M6SourceHistoryFact,
)
from disclosure_anchor.application.contracts.m6_run_events import (
    M6AdmissionControl, M6AttemptAdmitted, M6AttemptFinal, M6DocumentQualified, M6OwnerStamp,
    M6ProducerEvent, M6PublicationCommitted, M6PublicConfirmation, M6RemoteAccepted,
    M6ResourcesClosed, M6RunClosed, M6RunEvent, M6RunStarted, M6ServiceValidated, M6VerifierDrained,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import canonical_json_sha256
from disclosure_anchor.application.services.m6_run_accounting import reduce_m6_run


def sha(label):
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def changed(value, **updates):
    return type(value).model_validate({**value.model_dump(), **updates})


class RunExample:
    def __init__(self, *, service=False, origin="fresh", sources=1, phase="short_batch"):
        mode = "service_diagnostic" if service else "e2e_publication"
        self.manifest = M6CorpusManifest(campaign_id="campaign", mode=mode, entries=tuple(sorted((
            M6CorpusEntry(source_pdf_sha256=sha(f"source-{i}"), source_byte_count=1000+i,
                          source_page_count=7+i, document_id=None if service else f"doc-{i}",
                          stratum="contract_fixture", origin=origin)
            for i in range(sources)
        ), key=lambda e: e.source_pdf_sha256)))
        self.plan = M6QualityPlan(mode=mode, required_checks=tuple(sorted(
            M6_SERVICE_CHECKS if service else (*M6_SERVICE_CHECKS, "public_units_hash_match")
        )), reason_policies=())
        self.clock = M6ClockDomain(host_assignment_identity_sha256=sha("host"), boot_identity_sha256=sha("boot"),
            qpc_frequency_hz=10, clock_domain_identity_sha256=canonical_json_sha256({
                "boot_identity_sha256": sha("boot"), "clock_source": "QueryPerformanceCounter", "frequency_hz": 10,
            }))
        self.spec = M6RunSpec(run_id="measurement-1", campaign_id="campaign", mode=mode, phase=phase,
            start_condition="cold", clock=self.clock, runtime=M6RuntimeIdentity(
                source_commit="a"*40, source_manifest_sha256=sha("source-manifest"),
                runtime_bundle_identity_sha256=sha("runtime"), process_profile_sha256=sha("profile"),
                worker_profile_sha256=None if service else sha("worker"), owner_source_sha256=sha("owner-source"),
                gpu_device_identity_sha256=sha("gpu"), deployment_qualification_sha256=sha("deployment")),
            manifest_sha256=self.manifest.canonical_sha256(),
            scope_sha256=None if service else M6CampaignScope.from_manifest(self.manifest).canonical_sha256(),
            quality_plan_sha256=self.plan.canonical_sha256(), t0_ticks=100, planned_seconds=3600,
            deadline_ticks=36100, max_close_ticks=40000,
            carry_in_attempt_ids=tuple(f"attempt-{i}" for i in range(sources)) if origin == "carry_in" else (),
            resources=M6ResourceEnvelope(max_events=1000, max_record_bytes=16384, max_log_bytes=1_000_000,
                max_attempts=100, max_verifier_backlog_bytes=1_000_000, stop_admission_budget_ticks=10))
        self.records = []
        self.history = []
        self.evidence = []
        self.sequences = {}
        self.owner_epoch = sha("owner-1")

    def add(self, payload, tick, *, producer=None, producer_epoch=None, owner_epoch=None, boot=None):
        if owner_epoch is not None:
            self.owner_epoch = owner_epoch
        if producer is None:
            kind = payload.kind
            if kind in {"public_confirmation", "verifier_drained"}:
                producer = "public_verifier" if self.spec.mode == "e2e_publication" else "quality_verifier"
            elif kind == "document_qualified":
                producer = "quality_verifier"
            elif kind in {"attempt_admitted", "remote_accepted", "publication_committed", "service_validated", "attempt_final"}:
                producer = "service_runner" if self.spec.mode == "service_diagnostic" else "e2e_runner"
            else:
                producer = "owner"
        epoch = producer_epoch or (self.owner_epoch if producer == "owner" else sha(producer))
        key = producer, epoch
        self.sequences[key] = self.sequences.get(key, 0) + 1
        event = M6ProducerEvent(run_id=self.spec.run_id, spec_sha256=self.spec.canonical_sha256(),
            producer_kind=producer, producer_epoch_sha256=epoch, producer_sequence=self.sequences[key], payload=payload)
        stamp = M6OwnerStamp(sequence=len(self.records)+1, received_qpc_ticks=tick,
            boot_identity_sha256=boot or self.clock.boot_identity_sha256,
            owner_process_epoch_sha256=self.owner_epoch, producer_event_sha256=event.canonical_sha256())
        record = M6RunEvent(event=event, stamp=stamp)
        self.records.append(record)
        return record

    def start(self):
        self.add(M6RunStarted(clock=self.clock, t0_ticks=100, deadline_ticks=36100), 100)
        self.add(M6AdmissionControl(kind="admission_opened"), 101)

    def document(self, index=0, *, confirmed_tick=300, qualified_tick=301):
        entry = self.manifest.entries[index]
        attempt = f"attempt-{index}"
        run = f"processing-{index}" if entry.document_id is not None else None
        observation = M6QualificationObservation(mode=self.spec.mode, source_pdf_sha256=entry.source_pdf_sha256,
            source_byte_count=entry.source_byte_count, source_page_count=entry.source_page_count,
            provider_page_count=entry.source_page_count, processing_run_id=run,
            provider_bundle_sha256=sha(attempt+"bundle"), provider_document_sha256=sha(attempt+"provider"),
            public_units_sha256=sha(attempt+"units") if run else None,
            unit_count=20, unusable_unit_count=0, needs_review_unit_count=0, review_reasons=(),
            checks=tuple(M6CheckResult(check_id=c, outcome="pass", evidence_sha256=sha(attempt+c)) for c in self.plan.required_checks))
        evidence = M6QualificationEvidence(observation=observation, reviews=())
        self.evidence.append(evidence)
        self.add(M6AttemptAdmitted(attempt_id=attempt, fence_identity="fence-"+attempt,
            document_id=entry.document_id, processing_run_id=run, source_pdf_sha256=entry.source_pdf_sha256,
            source_byte_count=entry.source_byte_count, source_page_count=entry.source_page_count,
            process_profile_sha256=self.spec.runtime.process_profile_sha256), 200)
        self.add(M6RemoteAccepted(attempt_id=attempt, remote_task_identity_sha256=sha(attempt+"task"),
            acceptance_receipt_sha256=sha(attempt+"accept")), 210)
        if run:
            self.history.append(M6SourceHistoryFact(source_pdf_sha256=entry.source_pdf_sha256, scan_complete=True,
                first_processing_run_id=run, first_ledger_seq=index+1, first_source_page_count=entry.source_page_count,
                source_page_variants=1, audit_receipt_sha256=sha(attempt+"history")))
            committed = M6PublicationCommitted(attempt_id=attempt, processing_run_id=run, document_id=entry.document_id,
                source_pdf_sha256=entry.source_pdf_sha256, source_page_count=entry.source_page_count,
                ledger_seq=index+1, winner_sha256=sha(attempt+"winner"), durable_base_sha256=sha(attempt+"base"))
            self.add(committed, 250)
            self.add(M6PublicConfirmation(**{k:v for k,v in committed.model_dump().items() if k != "kind"},
                public_units_sha256=observation.public_units_sha256, artifact_closure_sha256=sha(attempt+"artifacts"),
                consumer_check_receipt_sha256=sha(attempt+"consumer"), history_audit_receipt_sha256=sha(attempt+"history"),
                verifier_identity="public-reader"), confirmed_tick)
        else:
            self.add(M6ServiceValidated(attempt_id=attempt, provider_bundle_sha256=observation.provider_bundle_sha256,
                validation_receipt_sha256=sha(attempt+"validated")), confirmed_tick)
        self.add(M6DocumentQualified(attempt_id=attempt, qualification_evidence_sha256=evidence.canonical_sha256()), qualified_tick)
        self.add(M6AttemptFinal(attempt_id=attempt, outcome="published" if run else "diagnostic_disposed",
            remote_disposition="consumed", remote_receipt_sha256=sha(attempt+"ack"),
            remote_task_identity_sha256=sha(attempt+"task"), cleanup_receipt_sha256=sha(attempt+"cleanup")), max(qualified_tick, confirmed_tick)+1)

    def close(self):
        self.add(M6AdmissionControl(kind="stop_admission_effective"), max(36100, self.records[-1].stamp.received_qpc_ticks))
        self.add(M6VerifierDrained(drain_receipt_sha256=sha("drain")), 36200)
        self.add(M6ResourcesClosed(residual_count=0, children_exited=True, ownership_receipt_sha256=sha("close")), 36300)
        self.add(M6RunClosed(tclose_ticks=36400, reason="deadline_drained"), 36400)

    def rewrite(self, kind, *, payload_updates=None, stamp_updates=None, event_updates=None):
        for i, record in enumerate(self.records):
            if record.event.payload.kind != kind:
                continue
            payload = changed(record.event.payload, **(payload_updates or {}))
            event = changed(record.event, payload=payload, **(event_updates or {}))
            stamp = changed(record.stamp, producer_event_sha256=event.canonical_sha256(), **(stamp_updates or {}))
            self.records[i] = M6RunEvent(event=event, stamp=stamp)
            return
        raise AssertionError(kind)

    def replay(self, *, raw=None):
        return reduce_m6_run(spec=self.spec, manifest=self.manifest, quality_plan=self.plan,
            journal_lines=raw if raw is not None else [r.canonical_bytes()+b"\n" for r in self.records],
            history=tuple(self.history), qualifications=tuple(self.evidence))


def golden(**kwargs):
    example = RunExample(**kwargs)
    example.start()
    example.document()
    example.close()
    return example
