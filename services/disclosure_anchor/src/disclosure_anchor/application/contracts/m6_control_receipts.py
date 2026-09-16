"""Control receipts the runner deposits with the M6 owner before admission close and run close.

Each model reproduces the field set the native owner checks by hash and shape
(`mineru_m6_run_control.cs` AdmissionClosed/Close). Canonical bytes are the
sorted-key, whitespace-free form shared with the native `Object()` writer, and
every number is an integer so both sides re-serialize identically.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator
from typing_extensions import Self

from disclosure_anchor.application.contracts.m6_common import M6ClosedModel, M6Hash, M6Id, M6NonnegativeInt

M6_ADMISSION_RECONCILIATION_CONTRACT = "m6.admission-reconciliation.v1"
M6_OWNERSHIP_CLOSURE_CONTRACT = "m6.ownership-closure.v1"
M6_RESOURCE_AUDIT_CONTRACT = "m6.resource-audit.v1"
M6_UNRESOLVED_CLAIMS_CONTRACT = "m6.unresolved-claims.v1"


def attempt_set_sha256(attempt_ids: Iterable[str]) -> str:
    """Native ``AttemptSetSha``: per-id digests, ordinal order, one line each, then one digest."""
    lines = sorted("sha256:" + hashlib.sha256(item.encode("utf-8")).hexdigest() for item in attempt_ids)
    digest = hashlib.sha256()
    for line in lines:
        digest.update((line + "\n").encode("utf-8"))
    return "sha256:" + digest.hexdigest()


class M6AdmissionReconciliationReceipt(M6ClosedModel):
    contract_version: Literal["m6.admission-reconciliation.v1"] = "m6.admission-reconciliation.v1"
    run_id: M6Id
    spec_sha256: M6Hash
    runner_epoch_sha256: M6Hash
    last_producer_sequence: M6NonnegativeInt
    admitted_attempt_count: M6NonnegativeInt
    admitted_attempt_set_sha256: M6Hash
    unresolved_claim_count: M6NonnegativeInt
    unresolved_receipt_sha256: M6Hash | None

    @model_validator(mode="after")
    def unresolved_receipt_matches_count(self) -> Self:
        if (self.unresolved_claim_count == 0) != (self.unresolved_receipt_sha256 is None):
            raise ValueError("unresolved receipt is present exactly when claims are unresolved")
        return self


class M6OwnershipClosureReceipt(M6ClosedModel):
    contract_version: Literal["m6.ownership-closure.v1"] = "m6.ownership-closure.v1"
    run_id: M6Id
    spec_sha256: M6Hash
    runner_epoch_sha256: M6Hash
    admitted_attempt_count: M6NonnegativeInt
    admitted_attempt_set_sha256: M6Hash
    final_attempt_count: M6NonnegativeInt
    final_attempt_set_sha256: M6Hash
    residual_count: M6NonnegativeInt
    children_exited: bool
    resource_audit_sha256: M6Hash


class M6ResourceAuditReceipt(M6ClosedModel):
    """The runner's own audit of what it still holds when it asks to close."""

    contract_version: Literal["m6.resource-audit.v1"] = "m6.resource-audit.v1"
    run_id: M6Id
    spec_sha256: M6Hash
    runner_epoch_sha256: M6Hash
    owner_identity: M6Id
    coordinator_terminal: Literal["quiescent", "stuck_open_circuit"]
    in_flight_count: M6NonnegativeInt
    credits_in_use_zero: bool
    scratch_residual_count: M6NonnegativeInt
    children_exited: bool


class M6UnresolvedClaim(M6ClosedModel):
    attempt_id: M6Id
    state: Annotated[str, Field(strict=True, min_length=1, max_length=64)]


class M6UnresolvedClaimsReceipt(M6ClosedModel):
    contract_version: Literal["m6.unresolved-claims.v1"] = "m6.unresolved-claims.v1"
    run_id: M6Id
    spec_sha256: M6Hash
    runner_epoch_sha256: M6Hash
    unresolved_attempt_count: M6NonnegativeInt
    unresolved_attempt_set_sha256: M6Hash
    attempts: Annotated[tuple[M6UnresolvedClaim, ...], Field(max_length=4096)]

    @model_validator(mode="after")
    def set_matches_attempts(self) -> Self:
        ids = tuple(item.attempt_id for item in self.attempts)
        if len(ids) != len(set(ids)) or ids != tuple(sorted(ids)):
            raise ValueError("unresolved claims must be sorted and unique")
        if self.unresolved_attempt_count != len(ids):
            raise ValueError("unresolved claim count differs from its list")
        if self.unresolved_attempt_set_sha256 != attempt_set_sha256(ids):
            raise ValueError("unresolved attempt set digest differs from its list")
        return self


M6ControlReceipt = (
    M6AdmissionReconciliationReceipt | M6OwnershipClosureReceipt | M6ResourceAuditReceipt | M6UnresolvedClaimsReceipt
)

__all__ = [
    "M6_ADMISSION_RECONCILIATION_CONTRACT", "M6_OWNERSHIP_CLOSURE_CONTRACT", "M6_RESOURCE_AUDIT_CONTRACT",
    "M6_UNRESOLVED_CLAIMS_CONTRACT", "M6AdmissionReconciliationReceipt", "M6ControlReceipt",
    "M6OwnershipClosureReceipt", "M6ResourceAuditReceipt", "M6UnresolvedClaim", "M6UnresolvedClaimsReceipt",
    "attempt_set_sha256",
]
