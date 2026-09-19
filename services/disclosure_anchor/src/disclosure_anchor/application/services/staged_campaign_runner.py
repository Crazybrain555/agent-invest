"""Bounded finite campaign on top of the existing scoped V4 runtime.

This is orchestration only: it freezes membership, turns deadline/external
stop into the coordinator's existing stop predicate and projects one receipt
from the coordinator's durable outcome. Admission is bounded by the frozen
ordinary membership itself and time by the deadline; there is no separate
quota and nothing here counts, claims, schedules, publishes or emits formal
M6 owner events. Those remain the coordinator, the ledger and the owner
assembly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from disclosure_anchor.application.contracts.m6_campaign import M6CampaignScope, M6CorpusManifest
from disclosure_anchor.application.contracts.staged_campaign_v4 import V4CampaignAdmissionScope
from disclosure_anchor.application.services.staged_parse_coordinator import CoordinatorResult

CAMPAIGN_RECEIPT_CONTRACT = "staged-v4-campaign.v1"
CAMPAIGN_INPUT_MAX_BYTES = 8 * 1024 * 1024
CampaignStopReason = Literal["deadline", "external_stop", "quiescent"]
CampaignActivationRole = Literal["candidate", "production"]
_STOP_REASONS: frozenset[str] = frozenset({"deadline", "external_stop", "quiescent"})


class CampaignInputError(ValueError):
    """Frozen campaign inputs do not match their pins or each other."""


def load_campaign_inputs(
    *, manifest_bytes: bytes, manifest_sha256: str, scope_bytes: bytes, scope_sha256: str,
) -> tuple[M6CorpusManifest, M6CampaignScope]:
    """Decode canonical inputs and require the caller's exact pins."""
    manifest = M6CorpusManifest.from_canonical_bytes(manifest_bytes, maximum_bytes=CAMPAIGN_INPUT_MAX_BYTES)
    scope = M6CampaignScope.from_canonical_bytes(scope_bytes, maximum_bytes=CAMPAIGN_INPUT_MAX_BYTES)
    if manifest.canonical_sha256() != manifest_sha256:
        raise CampaignInputError("campaign manifest differs from its pinned sha256")
    if scope.canonical_sha256() != scope_sha256:
        raise CampaignInputError("campaign scope differs from its pinned sha256")
    if manifest.mode != "e2e_publication":
        raise CampaignInputError("campaign requires an e2e_publication corpus")
    try:
        scope.verify_manifest(manifest)
    except ValueError as exc:
        raise CampaignInputError("campaign scope differs from frozen corpus") from exc
    return manifest, scope


@dataclass(frozen=True, slots=True)
class CampaignRunRequest:
    """One frozen membership plus one time bound.

    The ordinary (non carry-in) members are the only documents new admission
    may claim; the scoped candidate source and the prepared-claim path enforce
    that durably. The intended campaign size is therefore frozen in the
    manifest/scope, not passed as a second limit.
    """

    manifest: M6CorpusManifest
    scope: M6CampaignScope
    max_seconds: int
    activation_role: CampaignActivationRole = "candidate"
    admission_scope: V4CampaignAdmissionScope = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.manifest) is not M6CorpusManifest or type(self.scope) is not M6CampaignScope:
            raise CampaignInputError("campaign requires exact frozen manifest and scope models")
        if type(self.max_seconds) is not int or not 1 <= self.max_seconds <= 86400:
            raise CampaignInputError("campaign max_seconds must be 1..86400")
        if self.activation_role not in ("candidate", "production"):
            raise CampaignInputError("campaign activation role is invalid")
        admission_scope = V4CampaignAdmissionScope(self.scope, self.manifest)
        if not admission_scope.ordinary_document_ids:
            raise CampaignInputError("campaign has no ordinary (non carry-in) members to admit")
        object.__setattr__(self, "admission_scope", admission_scope)


@dataclass(slots=True)
class CampaignStopState:
    reason: CampaignStopReason | None = None


def campaign_stop_predicate(
    *, monotonic: Callable[[], float], deadline_monotonic: float,
    external_stop: Callable[[], bool], state: CampaignStopState,
) -> Callable[[], bool]:
    """Close new admission on external stop or deadline; the first reason latches.

    Only new admission closes. Durably claimed work is never cancelled; the
    coordinator drains it exactly as it does for commissioning, and a run that
    drains its whole membership before either bound ends ``quiescent``.
    """
    if (
        not callable(monotonic) or not callable(external_stop)
        or isinstance(deadline_monotonic, bool) or not isinstance(deadline_monotonic, (int, float))
        or type(state) is not CampaignStopState
    ):
        raise ValueError("campaign stop predicate requires callable clocks and one latch state")

    def stop_requested() -> bool:
        if state.reason is not None:
            return True
        if external_stop():
            state.reason = "external_stop"
        elif monotonic() >= deadline_monotonic:
            state.reason = "deadline"
        return state.reason is not None

    return stop_requested


@dataclass(frozen=True, slots=True)
class CampaignRuntimeIdentity:
    owner_identity: str
    worker_profile_sha256: str
    process_profile_sha256: str
    runtime_bundle_identity_sha256: str
    capacity_sha256: str
    stream_activation_sha256: str | None


def campaign_receipt(
    *, request: CampaignRunRequest, identity: CampaignRuntimeIdentity, result: CoordinatorResult,
    started_utc: datetime, finished_utc: datetime, monotonic_elapsed_s: float,
    stop_reason: CampaignStopReason, assembly: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project the coordinator's durable outcome under the frozen campaign identity.

    Attempt identities and states come only from ``CoordinatorResult``; the
    receipt invents no document-level admission list. Which members were
    claimed is reconciled from the durable attempt rows, not from this file.
    ``assembly`` is the M6 owner assembly record when the run was owner-bound;
    a failed assembly makes the run evidence-incomplete, never clean.
    """
    if assembly is not None and (type(assembly) is not dict or assembly.get("status") not in {"complete", "failed"}):
        raise ValueError("campaign assembly record must report complete or failed")
    if type(request) is not CampaignRunRequest or type(identity) is not CampaignRuntimeIdentity:
        raise ValueError("campaign receipt requires exact request and runtime identity")
    if type(result) is not CoordinatorResult:
        raise ValueError("campaign receipt requires the coordinator's own result")
    if started_utc.tzinfo is None or finished_utc.tzinfo is None or finished_utc < started_utc:
        raise ValueError("campaign receipt timestamps must be timezone-aware and ordered")
    if isinstance(monotonic_elapsed_s, bool) or not isinstance(monotonic_elapsed_s, (int, float)) or monotonic_elapsed_s < 0:
        raise ValueError("campaign receipt elapsed time must be non-negative")
    if stop_reason not in _STOP_REASONS:
        raise ValueError("campaign receipt stop reason is not closed")
    ordinary = request.admission_scope.ordinary_document_ids
    clean = (
        result.terminal.value == "quiescent" and result.recovery_complete
        and not result.errors and not result.credits_in_use.nonzero()
        and (assembly is None or assembly["status"] == "complete")
    )
    return {
        "contract_version": CAMPAIGN_RECEIPT_CONTRACT,
        "campaign_id": request.scope.campaign_id,
        "manifest_sha256": request.manifest.canonical_sha256(),
        "scope_sha256": request.scope.canonical_sha256(),
        "activation_role": request.activation_role,
        "owner_identity": identity.owner_identity,
        "worker_profile_sha256": identity.worker_profile_sha256,
        "process_profile_sha256": identity.process_profile_sha256,
        "runtime_bundle_identity_sha256": identity.runtime_bundle_identity_sha256,
        "capacity_sha256": identity.capacity_sha256,
        "stream_activation_sha256": identity.stream_activation_sha256,
        "max_seconds": request.max_seconds,
        "ordinary_member_count": len(ordinary),
        "carry_in_member_count": len(request.admission_scope.document_ids) - len(ordinary),
        "started_utc": started_utc.astimezone(UTC).isoformat(),
        "finished_utc": finished_utc.astimezone(UTC).isoformat(),
        "monotonic_elapsed_s": float(monotonic_elapsed_s),
        "terminal": result.terminal.value,
        "clean": clean,
        "recovery_complete": result.recovery_complete,
        "admitted": result.admitted,
        "completed": result.completed,
        "final_states": [list(item) for item in result.final_states],
        "errors": list(result.errors),
        "credits_in_use": result.credits_in_use.nonzero(),
        "stop_reason": stop_reason,
        "m6_assembly": assembly,
        "authority": (
            "finite scoped campaign run bounded by frozen membership and deadline; "
            "not a formal M6 owner receipt, publication credit or qualification"
        ),
    }


def campaign_spool_fact_bound(request: CampaignRunRequest) -> int:
    """Lifecycle-fact capacity for one owner-bound run.

    Every member of the frozen scope, carried-in members included, may produce
    up to four facts (admitted, remote accepted, publication committed, final);
    eight more cover unavailable-fact notes. Counting ordinary members only
    starved the spool as soon as recovered attempts were announced.
    """
    return 4 * len(request.admission_scope.document_ids) + 8


__all__ = [
    "CAMPAIGN_RECEIPT_CONTRACT", "CampaignActivationRole", "CampaignInputError", "CampaignRunRequest",
    "CampaignRuntimeIdentity", "CampaignStopReason", "CampaignStopState", "campaign_receipt",
    "campaign_spool_fact_bound", "campaign_stop_predicate", "load_campaign_inputs",
]
