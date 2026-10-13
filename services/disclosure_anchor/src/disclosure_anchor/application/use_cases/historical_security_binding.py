"""Record one operator-named historical security binding.

Preview reads only. Execute re-verifies the anchor and the existing bindings
inside one transaction that first row-locks the company's current security,
then creates the historical Security (when absent) and appends the binding
SourceAccess together. Re-running an executed plan returns the recorded row.

The binding never edits the company, its USCC identifier, the current
security, the tracked row or its checkpoint, and never deletes or rewrites an
earlier binding: a later decision may only add a consistent approved scope.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from disclosure_anchor.application.contracts.historical_security_registration import (
    HISTORICAL_SECURITY_BINDING_DATASET,
    HISTORICAL_SECURITY_BINDING_INTERFACE,
    HISTORICAL_SECURITY_BINDING_PLAN_SCHEMA,
    HISTORICAL_SECURITY_BINDING_SCHEMA,
    HISTORICAL_SECURITY_STATUS,
    PROVIDER,
    BindingEvidenceFileV1,
    BindingPreflightV1,
    HistoricalSecurityBindingPlanV1,
    HistoricalSecurityBindingV1,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.source_security_resolution import (
    BindingAnchor,
    VerifiedBinding,
    load_bindings,
    verify_binding_anchor,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import (
    HistoricalSecurityProvenanceError,
    SourceRecoveryError,
)


@dataclass(frozen=True)
class BindingEvidence:
    """Hash and size of the official evidence file, measured by the caller."""

    sha256: str
    byte_count: int


@dataclass(frozen=True)
class BindingExecutionResult:
    binding_source_access_id: str
    historical_security_id: str
    binding_sha256: str
    created_historical_security: bool
    recorded: bool


class HistoricalSecurityBinding:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        code_identity: str,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._uow_factory = uow_factory
        self._code_identity = code_identity
        self._clock = clock

    def preview(
        self,
        binding: HistoricalSecurityBindingV1,
        *,
        evidence: BindingEvidence,
    ) -> HistoricalSecurityBindingPlanV1:
        _require_evidence(binding, evidence)
        with self._uow_factory() as uow:
            anchor = _verified_anchor(uow, binding)
            preflight = _preflight(uow, binding, anchor)
        return HistoricalSecurityBindingPlanV1.model_validate(
            {
                "schema": HISTORICAL_SECURITY_BINDING_PLAN_SCHEMA,
                "binding": binding.to_document(),
                "binding_sha256": binding.sha256(),
                "evidence_file": BindingEvidenceFileV1(
                    sha256=evidence.sha256, byte_count=evidence.byte_count
                ).model_dump(mode="json"),
                "preflight": preflight.model_dump(mode="json"),
                "code_identity": self._code_identity,
            },
            strict=True,
        )

    def execute(
        self,
        plan: HistoricalSecurityBindingPlanV1,
        *,
        evidence: BindingEvidence,
        decided_by: str,
    ) -> BindingExecutionResult:
        binding = plan.binding
        if plan.code_identity != self._code_identity:
            raise SourceRecoveryError(
                "CODE_IDENTITY_CHANGED",
                "the binding plan was previewed by different code; preview again",
            )
        if decided_by != binding.decided_by:
            raise SourceRecoveryError(
                "DECIDED_BY_MISMATCH",
                f"only {binding.decided_by!r} may execute this named decision",
            )
        _require_evidence(binding, evidence)
        with self._uow_factory() as uow:
            # Every binding writer locks the current security first, so the
            # anchor, the historical security and its bindings cannot move
            # under this transaction.
            if uow.securities.get_for_update(binding.current_security_id) is None:
                raise SourceRecoveryError(
                    "ANCHOR_NOT_VERIFIED",
                    f"current security {binding.current_security_id} is missing",
                )
            anchor = _verified_anchor(uow, binding)
            preflight = _preflight(uow, binding, anchor)
            if preflight.action == "already_recorded":
                recorded = _recorded_binding(uow, binding, preflight)
                return BindingExecutionResult(
                    binding_source_access_id=recorded.source_access.source_access_id,
                    historical_security_id=str(preflight.historical_security_id),
                    binding_sha256=binding.sha256(),
                    created_historical_security=False,
                    recorded=False,
                )
            if preflight != plan.preflight:
                raise SourceRecoveryError(
                    "PREFLIGHT_CHANGED",
                    "ledger state differs from the previewed plan; preview again",
                )
            created = preflight.action == "create_historical_security"
            if created:
                security = uow.securities.add(
                    e.Security(
                        security_id=ids.new_security_id(),
                        company_id=binding.target_company_id,
                        security_code=binding.old_code,
                        exchange=binding.exchange,
                        board=anchor.current_security.board,
                        status=HISTORICAL_SECURITY_STATUS,
                    )
                )
            else:
                locked = uow.securities.get_for_update(str(preflight.historical_security_id))
                if locked is None:
                    raise SourceRecoveryError(
                        "PREFLIGHT_CHANGED", "historical security vanished; preview again"
                    )
                security = locked
            access = uow.source_accesses.add(
                e.SourceAccess(
                    source_access_id=ids.new_source_access_id(),
                    provider=PROVIDER,
                    provider_interface=HISTORICAL_SECURITY_BINDING_INTERFACE,
                    dataset_key=HISTORICAL_SECURITY_BINDING_DATASET,
                    query_params={
                        "schema": HISTORICAL_SECURITY_BINDING_SCHEMA,
                        "old_code": binding.old_code,
                        "exchange": binding.exchange,
                        "target_company_id": binding.target_company_id,
                        "current_security_id": binding.current_security_id,
                        "official_evidence_sha256": binding.official_evidence.sha256,
                    },
                    accessed_at=self._clock(),
                    status="ok",
                    result_hash=binding.sha256(),
                    result_snapshot=binding.to_document(),
                    company_id=binding.target_company_id,
                    security_id=security.security_id,
                )
            )
            uow.commit()
        return BindingExecutionResult(
            binding_source_access_id=access.source_access_id,
            historical_security_id=security.security_id,
            binding_sha256=binding.sha256(),
            created_historical_security=created,
            recorded=True,
        )


def _require_evidence(binding: HistoricalSecurityBindingV1, evidence: BindingEvidence) -> None:
    official = binding.official_evidence
    if evidence.sha256 != official.sha256 or evidence.byte_count != official.byte_count:
        raise SourceRecoveryError(
            "EVIDENCE_MISMATCH",
            f"evidence file is {evidence.sha256}/{evidence.byte_count} bytes, the "
            f"decision cites {official.sha256}/{official.byte_count}",
        )


def _verified_anchor(uow: UnitOfWork, binding: HistoricalSecurityBindingV1) -> BindingAnchor:
    try:
        return verify_binding_anchor(uow, binding)
    except HistoricalSecurityProvenanceError as exc:
        raise SourceRecoveryError("ANCHOR_NOT_VERIFIED", str(exc)) from exc


def _preflight(
    uow: UnitOfWork, binding: HistoricalSecurityBindingV1, anchor: BindingAnchor
) -> BindingPreflightV1:
    company_legal_name = anchor.company.legal_name
    tracked_security_id = str(anchor.tracked.security_id)
    old = uow.securities.get_by_code_exchange(binding.old_code, binding.exchange)
    if old is None:
        return BindingPreflightV1(
            action="create_historical_security",
            historical_security_id=None,
            existing_binding_source_access_ids=[],
            company_legal_name=company_legal_name,
            tracked_security_id=tracked_security_id,
        )
    if old.company_id != binding.target_company_id:
        raise SourceRecoveryError(
            "OLD_CODE_HELD_BY_OTHER_COMPANY",
            f"{binding.old_code}.{binding.exchange} is security {old.security_id} of "
            f"company {old.company_id}; a code reused by another entity is not an alias",
        )
    if old.status != HISTORICAL_SECURITY_STATUS:
        raise SourceRecoveryError(
            "OLD_CODE_NOT_HISTORICAL",
            f"{binding.old_code}.{binding.exchange} is security {old.security_id} with "
            f"status {old.status!r}; a binding never relabels an existing security",
        )
    try:
        existing = load_bindings(uow, old)
    except HistoricalSecurityProvenanceError as exc:
        raise SourceRecoveryError("BINDING_RECORDS_INVALID", str(exc)) from exc
    for recorded in existing:
        if recorded.decision.facts() != binding.facts():
            raise SourceRecoveryError(
                "BINDING_FACTS_CONFLICT",
                f"binding {recorded.source_access.source_access_id} records different "
                "identity facts; an earlier decision is never overridden",
            )
    same = [
        recorded
        for recorded in existing
        if recorded.source_access.result_hash == binding.sha256()
    ]
    return BindingPreflightV1(
        action="already_recorded" if same else "append_binding",
        historical_security_id=old.security_id,
        existing_binding_source_access_ids=[
            recorded.source_access.source_access_id for recorded in existing
        ],
        company_legal_name=company_legal_name,
        tracked_security_id=tracked_security_id,
    )


def _recorded_binding(
    uow: UnitOfWork,
    binding: HistoricalSecurityBindingV1,
    preflight: BindingPreflightV1,
) -> VerifiedBinding:
    security = uow.securities.get(str(preflight.historical_security_id))
    if security is None:
        raise SourceRecoveryError("PREFLIGHT_CHANGED", "historical security vanished")
    for recorded in load_bindings(uow, security):
        if recorded.source_access.result_hash == binding.sha256():
            return recorded
    raise SourceRecoveryError("PREFLIGHT_CHANGED", "recorded binding vanished")


__all__ = [
    "BindingEvidence",
    "BindingExecutionResult",
    "HistoricalSecurityBinding",
]
