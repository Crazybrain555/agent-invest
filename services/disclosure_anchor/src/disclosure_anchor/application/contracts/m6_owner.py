"""Private M6 owner control wire; bootstrap identity precedes input preparation.

The anchor has no corpus/spec hash, so T0 can be captured before freezing inputs.
The controller then binds the exact spec. Credentials are outside these durable
models; the transport authenticates the caller, not an arbitrary claimed role.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from disclosure_anchor.application.contracts.m6_common import (
    M6ClosedModel, M6Hash, M6Id, M6NonnegativeInt, M6PositiveInt,
)
from disclosure_anchor.application.contracts.m6_run import M6ClockDomain, M6ResourceEnvelope, M6RunSpec
from disclosure_anchor.application.contracts.m6_run_events import M6ProducerEvent, M6RunEvent


class M6OwnerAnchor(M6ClosedModel):
    contract_version: Literal["m6.owner-anchor.v1"] = "m6.owner-anchor.v1"
    run_id: M6Id
    clock: M6ClockDomain
    owner_process_epoch_sha256: M6Hash
    owner_source_sha256: M6Hash
    gpu_device_identity_sha256: M6Hash
    t0_ticks: M6NonnegativeInt
    planned_seconds: M6PositiveInt
    deadline_ticks: M6PositiveInt
    max_close_ticks: M6PositiveInt
    resources: M6ResourceEnvelope

    @model_validator(mode="after")
    def original_interval(self) -> Self:
        if self.deadline_ticks != self.t0_ticks + self.planned_seconds * self.clock.qpc_frequency_hz:
            raise ValueError("owner anchor deadline differs from its original QPC interval")
        if self.max_close_ticks < self.deadline_ticks:
            raise ValueError("owner close bound precedes admission deadline")
        return self

    def assert_spec(self, spec: M6RunSpec) -> None:
        for name in ("run_id", "clock", "t0_ticks", "planned_seconds", "deadline_ticks", "max_close_ticks", "resources"):
            if getattr(self, name) != getattr(spec, name):
                raise ValueError("owner anchor differs from frozen spec: " + name)
        if (self.owner_source_sha256 != spec.runtime.owner_source_sha256
                or self.gpu_device_identity_sha256 != spec.runtime.gpu_device_identity_sha256):
            raise ValueError("owner source/device differs from frozen runtime")


class M6BindOwner(M6ClosedModel):
    kind: Literal["bind"] = "bind"
    anchor_sha256: M6Hash


class M6OwnerControl(M6ClosedModel):
    kind: Literal["status", "lease", "open", "stop"]


class M6AppendObservation(M6ClosedModel):
    kind: Literal["append"] = "append"
    event: M6ProducerEvent


class M6AdmissionClosedAck(M6ClosedModel):
    """Sent only after every in-flight claim has returned and been accounted for.

    The immutable receipt contains the exact claim set and any unclaimed H0s;
    the owner compares the count/last sequence with its admitted observations.
    A nonzero unresolved count remains a permanent measurement incident.
    """

    kind: Literal["admission_closed"] = "admission_closed"
    runner_epoch_sha256: M6Hash
    last_producer_sequence: M6NonnegativeInt
    admitted_attempt_count: M6NonnegativeInt
    unresolved_claim_count: M6NonnegativeInt
    reconciliation_receipt_sha256: M6Hash


class M6CloseOwner(M6ClosedModel):
    kind: Literal["close"] = "close"
    ownership_receipt_sha256: M6Hash
    residual_count: M6NonnegativeInt
    children_exited: bool
    reason: Literal["deadline_drained", "stop_requested", "failed"]


M6OwnerCommand = Annotated[
    M6BindOwner | M6OwnerControl | M6AppendObservation | M6AdmissionClosedAck | M6CloseOwner,
    Field(discriminator="kind"),
]


class M6OwnerRequest(M6ClosedModel):
    contract_version: Literal["m6.owner-request.v1"] = "m6.owner-request.v1"
    run_id: M6Id
    spec_sha256: M6Hash
    request_id: M6Id
    command: M6OwnerCommand

    @model_validator(mode="after")
    def observation_binding(self) -> Self:
        if isinstance(self.command, M6AppendObservation) and (
            self.command.event.run_id != self.run_id or self.command.event.spec_sha256 != self.spec_sha256
        ):
            raise ValueError("owner request contains a different run/spec observation")
        return self


class M6OwnerStatus(M6ClosedModel):
    contract_version: Literal["m6.owner-status.v1"] = "m6.owner-status.v1"
    run_id: M6Id
    spec_sha256: M6Hash
    anchor_sha256: M6Hash
    owner_process_epoch_sha256: M6Hash
    observed_qpc_ticks: M6NonnegativeInt
    state: Literal["bound", "open", "stopping", "draining", "closed", "failed"]
    last_sequence: M6NonnegativeInt
    # A lease is a guard only; actual admissions still need durable claim/stamp.
    admission_valid_until_ticks: M6PositiveInt | None

    @model_validator(mode="after")
    def lease_state(self) -> Self:
        if self.admission_valid_until_ticks is not None and (
            self.state != "open" or self.admission_valid_until_ticks <= self.observed_qpc_ticks
        ):
            raise ValueError("owner lease must be positive and admission open")
        return self


class M6OwnerReply(M6ClosedModel):
    contract_version: Literal["m6.owner-reply.v1"] = "m6.owner-reply.v1"
    request_sha256: M6Hash
    outcome: Literal["ok", "conflict", "rejected"]
    status: M6OwnerStatus
    record: M6RunEvent | None
    error_code: M6Id | None

    @model_validator(mode="after")
    def outcome_binding(self) -> Self:
        if (self.outcome == "ok") != (self.error_code is None):
            raise ValueError("owner reply outcome disagrees with error")
        if self.outcome == "conflict" and self.record is None:
            raise ValueError("producer conflict must retain its stamped observation")
        if self.outcome == "rejected" and self.record is not None:
            raise ValueError("a stamped observation cannot be reported as rejected")
        if self.record is not None and (
            self.record.event.run_id != self.status.run_id
            or self.record.event.spec_sha256 != self.status.spec_sha256
            or self.record.stamp.sequence > self.status.last_sequence
            or self.record.stamp.received_qpc_ticks > self.status.observed_qpc_ticks
        ):
            raise ValueError("owner record differs from reply status")
        return self
