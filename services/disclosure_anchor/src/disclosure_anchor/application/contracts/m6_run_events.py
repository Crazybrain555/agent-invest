"""Closed producer observations with durable Windows-owner receipt stamps."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from disclosure_anchor.application.contracts.m6_campaign import M6DocumentId
from disclosure_anchor.application.contracts.m6_common import (
    M6ClosedModel, M6Hash, M6Id, M6NonnegativeInt, M6PositiveInt,
)
from disclosure_anchor.application.contracts.m6_run import M6ClockDomain


class M6RunStarted(M6ClosedModel):
    kind: Literal["run_started"] = "run_started"
    clock: M6ClockDomain
    t0_ticks: M6NonnegativeInt
    deadline_ticks: M6PositiveInt


class M6OwnerResumed(M6ClosedModel):
    kind: Literal["owner_resumed"] = "owner_resumed"
    clock: M6ClockDomain
    t0_ticks: M6NonnegativeInt
    deadline_ticks: M6PositiveInt
    previous_owner_epoch_sha256: M6Hash


class M6AdmissionControl(M6ClosedModel):
    kind: Literal["admission_opened", "stop_admission_requested", "stop_admission_effective"]


class M6AttemptAdmitted(M6ClosedModel):
    kind: Literal["attempt_admitted"] = "attempt_admitted"
    attempt_id: M6Id
    fence_identity: M6Id
    document_id: M6DocumentId | None
    processing_run_id: M6Id | None
    source_pdf_sha256: M6Hash
    source_byte_count: M6PositiveInt
    source_page_count: M6PositiveInt
    process_profile_sha256: M6Hash


class M6RemoteAccepted(M6ClosedModel):
    kind: Literal["remote_accepted"] = "remote_accepted"
    attempt_id: M6Id
    remote_task_identity_sha256: M6Hash
    acceptance_receipt_sha256: M6Hash


class M6PublicationCommitted(M6ClosedModel):
    kind: Literal["publication_committed"] = "publication_committed"
    attempt_id: M6Id
    processing_run_id: M6Id
    document_id: M6DocumentId
    source_pdf_sha256: M6Hash
    source_page_count: M6PositiveInt
    ledger_seq: M6PositiveInt
    winner_sha256: M6Hash
    durable_base_sha256: M6Hash


class M6PublicConfirmation(M6ClosedModel):
    kind: Literal["public_confirmation"] = "public_confirmation"
    attempt_id: M6Id
    processing_run_id: M6Id
    document_id: M6DocumentId
    source_pdf_sha256: M6Hash
    source_page_count: M6PositiveInt
    ledger_seq: M6PositiveInt
    winner_sha256: M6Hash
    durable_base_sha256: M6Hash
    public_units_sha256: M6Hash
    artifact_closure_sha256: M6Hash
    consumer_check_receipt_sha256: M6Hash
    history_audit_receipt_sha256: M6Hash
    verifier_identity: M6Id


class M6DocumentQualified(M6ClosedModel):
    kind: Literal["document_qualified"] = "document_qualified"
    attempt_id: M6Id
    qualification_evidence_sha256: M6Hash


class M6ServiceValidated(M6ClosedModel):
    kind: Literal["service_validated"] = "service_validated"
    attempt_id: M6Id
    provider_bundle_sha256: M6Hash
    validation_receipt_sha256: M6Hash


class M6AttemptFinal(M6ClosedModel):
    kind: Literal["attempt_final"] = "attempt_final"
    attempt_id: M6Id
    outcome: Literal["published", "diagnostic_disposed", "failed", "superseded"]
    remote_disposition: Literal["not_submitted", "consumed", "absent"]
    remote_receipt_sha256: M6Hash | None
    remote_task_identity_sha256: M6Hash | None
    cleanup_receipt_sha256: M6Hash

    @model_validator(mode="after")
    def remote_closure(self) -> Self:
        if (self.remote_disposition == "not_submitted") != (self.remote_receipt_sha256 is None):
            raise ValueError("remote disposition lacks exact receipt")
        if (self.remote_disposition == "not_submitted") != (self.remote_task_identity_sha256 is None):
            raise ValueError("remote disposition lacks task identity")
        if self.outcome in {"published", "diagnostic_disposed"} and self.remote_disposition == "not_submitted":
            raise ValueError("successful parse requires remote closure")
        return self


class M6VerifierDrained(M6ClosedModel):
    kind: Literal["verifier_drained"] = "verifier_drained"
    drain_receipt_sha256: M6Hash


class M6ResourcesClosed(M6ClosedModel):
    kind: Literal["resources_closed"] = "resources_closed"
    residual_count: M6NonnegativeInt
    children_exited: bool
    ownership_receipt_sha256: M6Hash


class M6RunClosed(M6ClosedModel):
    kind: Literal["run_closed"] = "run_closed"
    tclose_ticks: M6NonnegativeInt
    reason: Literal["deadline_drained", "stop_requested", "failed"]


class M6MeasurementIncident(M6ClosedModel):
    kind: Literal["measurement_incident"] = "measurement_incident"
    code: M6Id
    evidence_sha256: M6Hash


M6EventPayload = Annotated[
    M6RunStarted | M6OwnerResumed | M6AdmissionControl | M6AttemptAdmitted | M6RemoteAccepted
    | M6PublicationCommitted | M6PublicConfirmation | M6DocumentQualified | M6ServiceValidated
    | M6AttemptFinal | M6VerifierDrained | M6ResourcesClosed | M6RunClosed | M6MeasurementIncident,
    Field(discriminator="kind"),
]
M6ProducerKind = Literal["owner", "e2e_runner", "service_runner", "public_verifier", "quality_verifier"]


class M6ProducerEvent(M6ClosedModel):
    contract_version: Literal["m6.producer-event.v1"] = "m6.producer-event.v1"
    run_id: M6Id
    spec_sha256: M6Hash
    producer_kind: M6ProducerKind
    producer_epoch_sha256: M6Hash
    producer_sequence: M6PositiveInt
    payload: M6EventPayload


class M6OwnerStamp(M6ClosedModel):
    sequence: M6PositiveInt
    received_qpc_ticks: M6NonnegativeInt
    boot_identity_sha256: M6Hash
    owner_process_epoch_sha256: M6Hash
    producer_event_sha256: M6Hash


class M6RunEvent(M6ClosedModel):
    contract_version: Literal["m6.run-event.v1"] = "m6.run-event.v1"
    event: M6ProducerEvent
    stamp: M6OwnerStamp

    @model_validator(mode="after")
    def bind_entire_observation(self) -> Self:
        if self.stamp.producer_event_sha256 != self.event.canonical_sha256():
            raise ValueError("owner stamp does not bind the full producer event")
        return self
