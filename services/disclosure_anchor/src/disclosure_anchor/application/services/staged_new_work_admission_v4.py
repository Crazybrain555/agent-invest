"""Controller-owned H0 admission with bounded off-controller source observation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from typing import Protocol

from disclosure_anchor.application.contracts.provider_document_admission import SourcePdfObservation
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.ports.new_work_admission import NewWorkAdmissionUnavailable
from disclosure_anchor.application.ports.remote_parse_v4_ingress import (
    V4InitialIngressCommit, V4InitialIngressNotEligible,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RecoveryCandidate, RemoteParseV4Authority,
)
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4AdmissionObservationRequest, V4AdmissionObservationResult,
    V4InitialIngressCapacityBlocked, V4OrdinaryParseCandidate,
    V4OrdinaryParseCandidateSourcePort,
    V4RejectedSourcePdf, V4SourcePdfOverLimit,
)
from disclosure_anchor.application.ports.remote_parse_v4_source_rejection import V4SourceRejectionCommit
from disclosure_anchor.application.ports.staged_provider_parser import V4StageGuard
from disclosure_anchor.application.services.staged_ingress_v4 import DurableStagedIngressV4
from disclosure_anchor.application.services.staged_parse_coordinator import (
    AdmissionInterrupted, AdmissionOutcome, CoordinatorWork,
)


class V4InitialIngressFactoryPort(Protocol):
    def source_rejection(
        self, candidate: V4OrdinaryParseCandidate, rejection: V4RejectedSourcePdf,
    ) -> V4SourceRejectionCommit: ...

    def observation_request(
        self, candidate: V4OrdinaryParseCandidate, *, available_credits: ResourceCreditVector,
    ) -> V4AdmissionObservationRequest: ...

    def observe(
        self, request: V4AdmissionObservationRequest, *, stage_guard: V4StageGuard,
    ) -> V4AdmissionObservationResult: ...

    def build(
        self, candidate: V4OrdinaryParseCandidate, *,
        source_observation: SourcePdfObservation, available_credits: ResourceCreditVector,
    ) -> V4InitialIngressCommit: ...


class V4PreparedClaimPort(Protocol):
    def admit_new(
        self, *, limit: int, available_credits: ResourceCreditVector,
    ) -> AdmissionOutcome: ...

    def claim_recovery(self, candidate: RecoveryCandidate) -> CoordinatorWork: ...


class StagedV4NewWorkAdmitter:
    """Prepared claims progress independently of readiness for new ordinary H0.

    At most one ephemeral observation is outstanding. The coordinator charges
    its credit, runs it on the shared preflight pool, and accepts its tiny result
    here only after actual IO completion. No source IO/DB/singleton closure is
    submitted wholesale to a background thread.
    """

    def __init__(
        self, *, prepared_claims: V4PreparedClaimPort,
        ordinary_candidates: V4OrdinaryParseCandidateSourcePort,
        ingress_factory: V4InitialIngressFactoryPort,
        ingress: DurableStagedIngressV4, candidate_page_size: int,
        admission_guard: Callable[[], None] = lambda: None,
        process_guard: Callable[[], None] = lambda: None,
    ) -> None:
        if (
            not callable(getattr(prepared_claims, "admit_new", None))
            or not callable(getattr(prepared_claims, "claim_recovery", None))
            or not callable(getattr(ordinary_candidates, "list_candidates", None))
            or any(not callable(getattr(ingress_factory, method, None))
                   for method in ("build", "observation_request", "observe", "source_rejection"))
            or type(ingress) is not DurableStagedIngressV4
            or not callable(admission_guard)
            or not callable(process_guard)
            or type(candidate_page_size) is not int or not 1 <= candidate_page_size <= 1000
        ):
            raise ValueError("V4 new-work admission dependencies are invalid")
        self._prepared_claims = prepared_claims
        self._ordinary_candidates = ordinary_candidates
        self._ingress_factory = ingress_factory
        self._ingress = ingress
        self._candidate_page_size = candidate_page_size
        self._admission_guard = admission_guard
        self._process_guard = process_guard
        self._after_document_id: str | None = None
        self._scan_blocked_at: dict[str, int] = {}
        self._scan_ineligible: set[str] = set()
        self._prepared_complete = False
        self._prepared_blocked_at: dict[str, int] = {}
        self._prepared_ineligible: set[str] = set()
        self._awaiting_observation: V4AdmissionObservationRequest | None = None
        self._ready_observation: V4AdmissionObservationResult | None = None
        self._observation_has_more = False

    def observe(
        self, request: V4AdmissionObservationRequest, *, stage_guard: V4StageGuard,
    ) -> V4AdmissionObservationResult:
        # Read-only immutable factory inputs; do not access mutable cursor/claim
        # state here. The future transports the completed result to controller.
        return self._ingress_factory.observe(request, stage_guard=stage_guard)

    def accept_observation(self, result: V4AdmissionObservationResult) -> None:
        if (type(result) is not V4AdmissionObservationResult
                or result.request != self._awaiting_observation
                or self._ready_observation is not None):
            raise RuntimeError("V4 admission observation result lost its owner")
        self._ready_observation = result
        self._awaiting_observation = None

    def abandon_observation(self, request: V4AdmissionObservationRequest) -> None:
        if request != self._awaiting_observation:
            raise RuntimeError("V4 admission observation cancellation lost its owner")
        self._awaiting_observation = None
        # Cursor has not advanced; the ordinary DB backlog is still authority.

    def admit_new(
        self, *, limit: int, available_credits: ResourceCreditVector,
    ) -> AdmissionOutcome:
        if type(limit) is not int or limit < 1:
            raise ValueError("V4 new-work admission limit must be positive")
        if type(available_credits) is not ResourceCreditVector:
            raise ValueError("V4 new-work admission credits must be exact")
        if self._awaiting_observation is not None:
            raise RuntimeError("V4 admission observation has not actually completed")
        prior = AdmissionOutcome(work=(), backlog_exists=False)
        prepared_finished = False
        if not self._prepared_complete:
            prior = self._prepared_claims.admit_new(limit=limit, available_credits=available_credits)
            if prior.scan_incomplete:
                return prior
            self._prepared_complete = True
            prepared_finished = True
            self._prepared_ineligible = set(prior.ineligible_dimensions)
        selected = list(prior.work)
        durably_claimed = list(prior.work)
        if self._after_document_id is None and self._ready_observation is None:
            self._scan_blocked_at.clear()
            self._scan_ineligible.clear()
        try:
            remaining = available_credits - _credits_of(prior.work)
            if prepared_finished:
                self._prepared_blocked_at = {
                    name: getattr(remaining, name) for name in prior.blocked_dimensions
                }
            if len(selected) >= limit:
                return self._outcome(tuple(selected), remaining=remaining, incomplete=True)
            if self._ready_observation is not None:
                ready = self._ready_observation
                candidate = ready.request.candidate
                if isinstance(ready.source, V4SourcePdfOverLimit):
                    self._record_blocked(
                        V4InitialIngressCapacityBlocked(
                            ("snapshot_bytes",), ineligible_dimensions=("snapshot_bytes",),
                        ), remaining,
                    )
                elif isinstance(ready.source, V4RejectedSourcePdf):
                    self._process_guard()
                    rejection = self._ingress_factory.source_rejection(candidate, ready.source)
                    try:
                        self._ingress.reject_source(rejection, write_guard=self._process_guard)
                    except V4InitialIngressNotEligible:
                        pass
                else:
                    self._admission_guard()
                    try:
                        command = self._ingress_factory.build(
                            candidate, source_observation=ready.source, available_credits=remaining,
                        )
                    except V4InitialIngressCapacityBlocked as exc:
                        self._record_blocked(exc, remaining)
                    else:
                        try:
                            authority = self._ingress.execute(command, write_guard=self._admission_guard)
                        except V4InitialIngressNotEligible:
                            pass
                        else:
                            work = self._claim_created_h0(authority)
                            durably_claimed.append(work)
                            remaining = remaining - work.credits
                            selected.append(work)
                self._after_document_id = candidate.document_id
                self._ready_observation = None
                return self._outcome(
                    tuple(selected), remaining=remaining, incomplete=self._observation_has_more,
                )
            self._admission_guard()
            page = self._ordinary_candidates.list_candidates(
                after_document_id=self._after_document_id, limit=self._candidate_page_size,
            )
            if len(page.candidates) > self._candidate_page_size or (
                self._after_document_id is not None and any(
                    item.document_id <= self._after_document_id for item in page.candidates
                )
            ):
                raise ValueError("V4 ordinary candidate cursor did not advance within its bound")
            for index, candidate in enumerate(page.candidates):
                try:
                    request = self._ingress_factory.observation_request(
                        candidate, available_credits=remaining,
                    )
                except V4InitialIngressCapacityBlocked as exc:
                    self._after_document_id = candidate.document_id
                    self._record_blocked(exc, remaining)
                    continue
                if (type(request) is not V4AdmissionObservationRequest
                        or request.candidate != candidate or not request.credits.fits(remaining)):
                    raise RuntimeError("V4 source observer requested another identity or credit")
                self._awaiting_observation = request
                self._observation_has_more = page.has_more or index + 1 < len(page.candidates)
                return AdmissionOutcome(
                    work=tuple(selected), backlog_exists=True, scan_incomplete=True,
                    observation_request=request,
                )
            return self._outcome(tuple(selected), remaining=remaining, incomplete=page.has_more)
        except NewWorkAdmissionUnavailable as exc:
            self._prepared_complete = False
            return AdmissionOutcome(
                work=tuple(durably_claimed), backlog_exists=True, deferred_reason=str(exc),
            )
        except AdmissionInterrupted:
            raise
        except Exception as exc:
            if durably_claimed:
                raise AdmissionInterrupted(
                    f"{type(exc).__name__}:{exc}", claimed_work=tuple(durably_claimed),
                ) from exc
            raise

    def _record_blocked(
        self, error: V4InitialIngressCapacityBlocked, remaining: ResourceCreditVector,
    ) -> None:
        self._scan_ineligible.update(error.ineligible_dimensions)
        for name in set(error.blocked_dimensions) - set(error.ineligible_dimensions):
            available = getattr(remaining, name)
            self._scan_blocked_at[name] = min(available, self._scan_blocked_at.get(name, available))

    def _outcome(
        self, work: tuple[CoordinatorWork, ...], *, remaining: ResourceCreditVector, incomplete: bool,
    ) -> AdmissionOutcome:
        if not incomplete:
            self._after_document_id = None
            incomplete = any(
                getattr(remaining, name) > available
                for summary in (self._scan_blocked_at, self._prepared_blocked_at)
                for name, available in summary.items()
            )
            self._prepared_complete = False
        blocked = set(self._prepared_blocked_at) | set(self._scan_blocked_at)
        ineligible = self._scan_ineligible | self._prepared_ineligible
        return AdmissionOutcome(
            work=work, backlog_exists=bool(incomplete or blocked or ineligible),
            blocked_dimensions=tuple(item.name for item in fields(ResourceCreditVector) if item.name in blocked),
            ineligible_dimensions=tuple(item.name for item in fields(ResourceCreditVector) if item.name in ineligible),
            scan_incomplete=incomplete,
        )

    def _claim_created_h0(self, authority: RemoteParseV4Authority) -> CoordinatorWork:
        if (type(authority) is not RemoteParseV4Authority or authority.state != "prepared"
                or authority.lifecycle_version != 0 or authority.claim_owner_identity is not None):
            raise ValueError("V4 ingress did not return one unclaimed H0")
        return self._prepared_claims.claim_recovery(RecoveryCandidate(
            attempt_id=authority.attempt_id, state=authority.state,
            lifecycle_version=authority.lifecycle_version, claim_generation=authority.claim_generation,
            claim_owner_identity=None, lease_remaining_seconds=None,
        ))


def _credits_of(work: tuple[CoordinatorWork, ...]) -> ResourceCreditVector:
    total = ResourceCreditVector()
    for item in work:
        total = total + item.credits
    return total


__all__ = ["StagedV4NewWorkAdmitter", "V4InitialIngressFactoryPort"]
