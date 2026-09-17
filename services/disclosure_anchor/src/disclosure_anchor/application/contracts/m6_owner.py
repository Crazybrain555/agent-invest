"""Private M6 owner control wire; bootstrap identity precedes input preparation.

The anchor has no corpus/spec hash, so T0 can be captured before freezing inputs.
The controller then binds the exact spec. Credentials are outside these durable
models; the transport authenticates the caller, not an arbitrary claimed role.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from disclosure_anchor.application.contracts.closed_document import (
    canonical_bytes, load_closed_object, require_fields, require_int, require_sha256, sha256_of,
)
from disclosure_anchor.application.contracts.m6_common import (
    M6ClosedModel, M6Hash, M6Id, M6NonnegativeInt, M6PositiveInt,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
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



def physical_owner_epoch_sha256(anchor: M6OwnerAnchor, *, pid: object, creation_filetime_100ns: object) -> str:
    """Exactly MineruM6OwnerIdentity.ProcessEpoch; no wall clock or host IO."""
    pid = require_int(pid, label="owner pid", maximum=2**31 - 1)
    creation_filetime_100ns = require_int(creation_filetime_100ns, label="owner creation_filetime_100ns")
    return sha256_of(canonical_bytes({
        "run_id": anchor.run_id, "owner_source_sha256": anchor.owner_source_sha256,
        "boot_identity_sha256": anchor.clock.boot_identity_sha256,
        "pid": pid, "creation_filetime_100ns": creation_filetime_100ns,
    }))


def bind_physical_owner_boot(
    *, metadata_raw: bytes, body_raw: bytes, anchor: M6OwnerAnchor,
    pid: object, creation_filetime_100ns: object,
) -> str:
    """Bind the existing native diagnostic to the anchor and return its resident UTC encoding.

    Native BootId and resident LastBootUpTime hashes are NOT interchangeable.
    Both derive from this one existing physical identity record; metadata,
    original bytes, native clock, GPU and PID/birth/epoch must all agree first.
    This checks recorded evidence, not an independent hardware attestation.
    """
    metadata = load_closed_object(metadata_raw, label="owner identity metadata", maximum_bytes=1024)
    require_fields(metadata, {"contract_version", "code", "body_sha256"}, label="owner identity metadata")
    if (metadata["contract_version"] != "m6.transport-diagnostic.v1"
            or metadata["code"] != "owner_identity"
            or metadata["body_sha256"] != sha256_of(body_raw)):
        raise ValueError("owner identity metadata does not bind the exact body")
    body = load_closed_object(body_raw, label="physical owner identity", maximum_bytes=65536)
    require_fields(body, {"contract_version", "boot_counter", "boot_identity_version", "clock",
                         "creation_filetime_100ns", "gpu_device_identity_sha256", "pid",
                         "windows_boot_utc", "windows_node_identity_sha256"}, label="physical owner identity")
    if (body["contract_version"] != "m6.physical-owner-identity.v2"
            or body["boot_identity_version"] != "m6.windows-boot-counter.v1"
            or canonical_bytes(body) != body_raw):
        raise ValueError("physical owner identity is not the canonical native v2 record")
    node = require_sha256(body["windows_node_identity_sha256"], label="physical owner node")
    counter = require_int(body["boot_counter"], label="physical owner boot counter", minimum=0, maximum=2**32 - 1)
    native_boot = sha256_of(canonical_bytes({"contract_version": "m6.windows-boot-counter.v1",
                                           "windows_node_identity_sha256": node, "boot_counter": counter}))
    clock = M6ClockDomain.model_validate(body["clock"])
    clock_domain = sha256_of(canonical_bytes({"boot_identity_sha256": native_boot,
                                              "clock_source": "QueryPerformanceCounter",
                                              "frequency_hz": clock.qpc_frequency_hz}))
    if (clock != anchor.clock or native_boot != clock.boot_identity_sha256
            or clock_domain != clock.clock_domain_identity_sha256
            or body["gpu_device_identity_sha256"] != anchor.gpu_device_identity_sha256):
        raise ValueError("physical owner identity differs from the original anchor clock/GPU")
    pid = require_int(pid, label="external owner pid", maximum=2**31 - 1)
    creation_filetime_100ns = require_int(creation_filetime_100ns, label="external owner creation_filetime_100ns")
    body_pid = require_int(body["pid"], label="physical owner pid", maximum=2**31 - 1)
    birth = require_int(body["creation_filetime_100ns"], label="physical owner creation_filetime_100ns")
    if (body_pid != pid or birth != creation_filetime_100ns
            or physical_owner_epoch_sha256(anchor, pid=body_pid, creation_filetime_100ns=birth)
            != anchor.owner_process_epoch_sha256):
        raise ValueError("physical owner identity differs from the external start/original owner epoch")
    boot_utc = body["windows_boot_utc"]
    if type(boot_utc) is not str or not boot_utc.endswith("Z"):
        raise ValueError("physical owner boot UTC is not the original UTC string")
    parsed = datetime.fromisoformat(boot_utc)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("physical owner boot UTC has no UTC timezone")
    # Keep all seven .NET fractional digits. Reserializing via Python datetime
    # would change the historical resident identity even for the same instant.
    return sha256_of(canonical_bytes({"windows_node_identity_sha256": node, "boot_utc": boot_utc}))


# The canonical spec travels inside the bind request; the escaped whole wire
# (envelope + payload) is bounded separately by the transport's 65536 bytes.
M6_BIND_SPEC_MAX_BYTES = 49152


class M6BindOwner(M6ClosedModel):
    kind: Literal["bind"] = "bind"
    anchor_sha256: M6Hash
    spec_utf8: Annotated[str, Field(min_length=2, max_length=M6_BIND_SPEC_MAX_BYTES)]

    @model_validator(mode="after")
    def canonical_spec(self) -> Self:
        raw = self.spec_utf8.encode("utf-8")
        if len(raw) > M6_BIND_SPEC_MAX_BYTES or any(byte < 32 or byte == 127 for byte in raw):
            raise ValueError("bind spec exceeds its byte bound or contains control characters")
        self.spec()
        return self

    def spec(self) -> M6RunSpec:
        """The exact frozen spec these bytes denote (strict, canonical, closed)."""
        return M6RunSpec.from_canonical_bytes(self.spec_utf8.encode("utf-8"), maximum_bytes=M6_BIND_SPEC_MAX_BYTES)


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


# The owner reads these control receipts by hash at admission_closed/close.
# Deposit is the only authenticated way to place them in its private store.
M6DepositReceiptKind = Literal[
    "admission_reconciliation", "ownership_closure", "resource_audit", "unresolved_claims",
]
M6_DEPOSIT_RECEIPT_CONTRACTS: dict[str, str] = {
    "admission_reconciliation": "m6.admission-reconciliation.v1",
    "ownership_closure": "m6.ownership-closure.v1",
    "resource_audit": "m6.resource-audit.v1",
    "unresolved_claims": "m6.unresolved-claims.v1",
}
# Complete wire request stays inside MaximumWireBytes=65536 with its envelope.
M6_DEPOSIT_RECEIPT_MAX_BYTES = 49152


def _carries_float(value: object) -> bool:
    if type(value) is float:
        return True
    if type(value) is dict:
        return any(_carries_float(item) for item in value.values())
    if type(value) is list:
        return any(_carries_float(item) for item in value)
    return False


class M6DepositReceipt(M6ClosedModel):
    kind: Literal["deposit"] = "deposit"
    receipt_kind: M6DepositReceiptKind
    receipt_sha256: M6Hash
    receipt_utf8: Annotated[str, Field(min_length=2, max_length=M6_DEPOSIT_RECEIPT_MAX_BYTES)]

    @model_validator(mode="after")
    def canonical_receipt(self) -> Self:
        raw = self.receipt_utf8.encode("utf-8")
        if len(raw) > M6_DEPOSIT_RECEIPT_MAX_BYTES or any(byte < 32 or byte == 127 for byte in raw):
            raise ValueError("deposit receipt exceeds its byte bound or contains control characters")
        if "sha256:" + hashlib.sha256(raw).hexdigest() != self.receipt_sha256:
            raise ValueError("deposit receipt hash differs from its bytes")
        decoded = strict_json_loads(self.receipt_utf8)
        if type(decoded) is not dict:
            raise ValueError("deposit receipt must be a JSON object")
        if _carries_float(decoded):
            # The native owner reproduces canonical integers, not float repr.
            raise ValueError("deposit receipt must not carry non-integer numbers")
        canonical = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if canonical != self.receipt_utf8:
            raise ValueError("deposit receipt must be canonical JSON")
        if decoded.get("contract_version") != M6_DEPOSIT_RECEIPT_CONTRACTS[self.receipt_kind]:
            raise ValueError("deposit receipt contract differs from its declared kind")
        return self


M6OwnerCommand = Annotated[
    M6BindOwner | M6OwnerControl | M6AppendObservation | M6AdmissionClosedAck | M6CloseOwner | M6DepositReceipt,
    Field(discriminator="kind"),
]


class M6OwnerRequest(M6ClosedModel):
    contract_version: Literal["m6.owner-request.v2"] = "m6.owner-request.v2"
    run_id: M6Id
    spec_sha256: M6Hash
    request_id: M6Id
    command: M6OwnerCommand

    @model_validator(mode="after")
    def observation_binding(self) -> Self:
        if isinstance(self.command, M6BindOwner):
            raw = self.command.spec_utf8.encode("utf-8")
            if "sha256:" + hashlib.sha256(raw).hexdigest() != self.spec_sha256:
                raise ValueError("owner request binds spec bytes whose hash differs from spec_sha256")
            if self.command.spec().run_id != self.run_id:
                raise ValueError("owner request binds a spec frozen for a different run")
        if isinstance(self.command, M6AppendObservation) and (
            self.command.event.run_id != self.run_id or self.command.event.spec_sha256 != self.spec_sha256
        ):
            raise ValueError("owner request contains a different run/spec observation")
        if isinstance(self.command, M6DepositReceipt):
            receipt = strict_json_loads(self.command.receipt_utf8)
            assert type(receipt) is dict
            if receipt.get("run_id") != self.run_id or receipt.get("spec_sha256") != self.spec_sha256:
                raise ValueError("owner request deposits a receipt for a different run/spec")
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
