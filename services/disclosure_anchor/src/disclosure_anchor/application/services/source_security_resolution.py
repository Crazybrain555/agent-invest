"""Resolve the registration subject of one acquired index candidate.

A current security keeps the SubjectResolver path unchanged. A historical
security (an old exchange code of a company already anchored by a
source-bound USCC) is usable only through its binding evidence chain:

1. the exact index SourceAccess that carried the candidate, whose stored
   candidate is canonically identical to the one being registered;
2. one or more recorded ``historical-security-binding.v1`` decisions whose
   identity facts agree, re-hashed from storage;
3. the binding's anchor, re-read now: company, active USCC identifier and its
   profile SourceAccess, the current security and the tracked row;
4. an approved index interface, query owner and announcement date;
5. provider org ids only as consistency checks, never as identity.

The historical code stays on its own Security row and the query owner stays
on the index access: nothing is rewritten, and no SubjectCandidate is built
from a legal name the provider never returned for the old code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
import re
from typing import Union

from disclosure_anchor.application.contracts.historical_security_registration import (
    ASSOCIATION_BASES,
    BINDABLE_INDEX_INTERFACES,
    HISTORICAL_ACQUISITION_PROVENANCE_SCHEMA,
    HISTORICAL_SECURITY_BINDING_DATASET,
    HISTORICAL_SECURITY_BINDING_INTERFACE,
    HISTORICAL_SECURITY_STATUS,
    PROFILE_INTERFACE,
    PROVIDER,
    RETAINED_REGISTRATION_SCHEMA,
    ContractViolation,
    HistoricalSecurityBindingV1,
    binding_from_snapshot,
    candidate_sha256,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.subject_resolver import (
    ResolvedSubject,
    SubjectCandidate,
    SubjectResolver,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import (
    HistoricalSecurityBindingRequiredError,
    HistoricalSecurityProvenanceError,
    RegistrationMetadataError,
)
from disclosure_anchor.domain.value_objects import infer_mainland_exchange


_ISSUER = object()
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class HistoricalAcquisitionProvenance:
    """Verified lineage of one historical-code acquisition.

    Issued only by :func:`resolve_acquisition_subject`; a caller cannot
    construct one from an arbitrary claim.
    """

    index_source_access_id: str
    index_result_hash: str
    candidate_sha256: str
    binding_source_access_id: str
    binding_sha256: str
    acquisition_scope_company_id: str
    acquisition_scope_security_id: str
    original_candidate_code: str
    exchange: str
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _ISSUER:
            raise TypeError(
                "historical acquisition provenance is issued only by "
                "resolve_acquisition_subject"
            )

    def snapshot(self) -> dict[str, object]:
        return {
            "schema": HISTORICAL_ACQUISITION_PROVENANCE_SCHEMA,
            "index_source_access_id": self.index_source_access_id,
            "index_result_hash": self.index_result_hash,
            "candidate_sha256": self.candidate_sha256,
            "binding_source_access_id": self.binding_source_access_id,
            "binding_sha256": self.binding_sha256,
            "acquisition_scope_company_id": self.acquisition_scope_company_id,
            "acquisition_scope_security_id": self.acquisition_scope_security_id,
            "original_candidate_code": self.original_candidate_code,
            "exchange": self.exchange,
        }


@dataclass(frozen=True)
class RetainedRecoveryProvenance:
    """A verified historical acquisition registered from a retained archive."""

    acquisition: HistoricalAcquisitionProvenance
    recovery_of_source_access_id: str
    failed_access_projection_sha256: str
    replay_plan_sha256: str
    raw_file_relpath: str
    raw_file_hash: str
    byte_count: int
    association_basis: str

    def __post_init__(self) -> None:
        if type(self.acquisition) is not HistoricalAcquisitionProvenance:
            raise TypeError("retained recovery requires verified acquisition provenance")
        if not isinstance(self.recovery_of_source_access_id, str) or not self.recovery_of_source_access_id:
            raise ValueError("recovery_of_source_access_id is required")
        for label in ("failed_access_projection_sha256", "replay_plan_sha256", "raw_file_hash"):
            value = getattr(self, label)
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"{label} must be a canonical sha256")
        if type(self.byte_count) is not int or self.byte_count < 1:
            raise ValueError("byte_count must be a positive integer")
        if self.association_basis not in ASSOCIATION_BASES:
            raise ValueError(f"unknown association_basis {self.association_basis!r}")
        if not self.raw_file_relpath.endswith(
            f"sha256_{self.raw_file_hash.removeprefix('sha256:')}.pdf"
        ):
            raise ValueError("raw_file_relpath is not the archive path of raw_file_hash")

    def snapshot(self) -> dict[str, object]:
        return {
            "schema": RETAINED_REGISTRATION_SCHEMA,
            "recovery_of_source_access_id": self.recovery_of_source_access_id,
            "failed_access_projection_sha256": self.failed_access_projection_sha256,
            "replay_plan_sha256": self.replay_plan_sha256,
            "raw_file_relpath": self.raw_file_relpath,
            "raw_file_hash": self.raw_file_hash,
            "byte_count": self.byte_count,
            "association_basis": self.association_basis,
        }


RegistrationProvenance = Union[HistoricalAcquisitionProvenance, RetainedRecoveryProvenance]


@dataclass(frozen=True)
class VerifiedAcquisitionSubject:
    subject: ResolvedSubject
    # None on the current-security path, which records no extra lineage.
    provenance: HistoricalAcquisitionProvenance | None


@dataclass(frozen=True)
class VerifiedBinding:
    source_access: e.SourceAccess
    decision: HistoricalSecurityBindingV1


@dataclass(frozen=True)
class BindingAnchor:
    company: e.Company
    current_security: e.Security
    tracked: e.TrackedCompany


def candidate_security(
    uow: UnitOfWork, *, security_code: str, exchange: str | None
) -> tuple[e.Security, e.Company]:
    """The ledger security a candidate names, probed exactly as downloads do."""

    if exchange:
        exchanges = [exchange]
    else:
        try:
            exchanges = [infer_mainland_exchange(security_code), "LOCAL"]
        except ValueError:
            # Provider-neutral/manual snapshots may carry an extensible local
            # identity. Mainland shapes, however, must never be probed through
            # known-wrong exchanges now that lookups validate canonical pairs.
            exchanges = ["LOCAL"]
    security = None
    for candidate_exchange in exchanges:
        security = uow.securities.get_by_code_exchange(security_code, candidate_exchange)
        if security is not None:
            break
    if security is None:
        raise RegistrationMetadataError(
            f"security must be synced before download: {security_code}"
        )
    company = uow.companies.get(security.company_id)
    if company is None:
        raise RegistrationMetadataError(
            f"security {security.security_id} references missing company"
        )
    return security, company


def resolve_acquisition_subject(
    uow: UnitOfWork,
    *,
    candidate: Mapping[str, object],
    index_source_access_id: str | None,
    subject_resolver: SubjectResolver,
    expected_binding_source_access_id: str | None = None,
) -> VerifiedAcquisitionSubject:
    security_code = _candidate_str(candidate, "security_code")
    security, company = candidate_security(
        uow,
        security_code=security_code,
        exchange=_optional_str(candidate.get("exchange")),
    )
    if security.status != HISTORICAL_SECURITY_STATUS:
        if expected_binding_source_access_id is not None:
            raise HistoricalSecurityProvenanceError(
                f"{security.security_code}.{security.exchange} is not a historical "
                "security; a binding cannot apply to it"
            )
        subject = subject_resolver.resolve(
            uow,
            SubjectCandidate(
                security_code=security.security_code,
                exchange=security.exchange,
                legal_name=company.legal_name,
                credit_code=company.unified_social_credit_code,
            ),
        )
        return VerifiedAcquisitionSubject(subject=subject, provenance=None)
    return _resolve_historical(
        uow,
        candidate=candidate,
        security=security,
        company=company,
        index_source_access_id=index_source_access_id,
        expected_binding_source_access_id=expected_binding_source_access_id,
    )


def load_bindings(uow: UnitOfWork, security: e.Security) -> list[VerifiedBinding]:
    """Every recorded binding of one historical security, re-verified.

    Coexisting bindings must agree on every identity fact; they may differ
    only in their approved scopes. A malformed or drifted row blocks use.
    """

    bindings: list[VerifiedBinding] = []
    for access in uow.source_accesses.list_historical_security_bindings(
        security_id=security.security_id
    ):
        if (
            access.provider != PROVIDER
            or access.provider_interface != HISTORICAL_SECURITY_BINDING_INTERFACE
            or access.dataset_key != HISTORICAL_SECURITY_BINDING_DATASET
            or access.status != "ok"
            or access.company_id != security.company_id
            or access.security_id != security.security_id
        ):
            raise HistoricalSecurityProvenanceError(
                f"binding access {access.source_access_id} does not describe "
                f"security {security.security_id}"
            )
        try:
            decision = binding_from_snapshot(access.result_snapshot)
        except ContractViolation as exc:
            raise HistoricalSecurityProvenanceError(
                f"binding access {access.source_access_id} is not a valid decision: {exc}"
            ) from exc
        if decision.sha256() != access.result_hash:
            raise HistoricalSecurityProvenanceError(
                f"binding access {access.source_access_id} decision hash drifted"
            )
        if (
            decision.target_company_id != security.company_id
            or decision.old_code != security.security_code
            or decision.exchange != security.exchange
        ):
            raise HistoricalSecurityProvenanceError(
                f"binding access {access.source_access_id} names another security"
            )
        bindings.append(VerifiedBinding(source_access=access, decision=decision))
    if len({binding.decision.facts() for binding in bindings}) > 1:
        raise HistoricalSecurityProvenanceError(
            f"historical security {security.security_id} has contradictory bindings; "
            "no binding applies until they are reviewed"
        )
    return bindings


def verify_binding_anchor(
    uow: UnitOfWork, decision: HistoricalSecurityBindingV1
) -> BindingAnchor:
    """Re-read the source-bound anchor a binding was decided against."""

    def refuse(message: str) -> HistoricalSecurityProvenanceError:
        return HistoricalSecurityProvenanceError(
            f"binding anchor for {decision.old_code}.{decision.exchange}: {message}"
        )

    company = uow.companies.get(decision.target_company_id)
    if company is None:
        raise refuse(f"company {decision.target_company_id} is missing")
    expected = decision.expected_uscc
    if _normalized(company.unified_social_credit_code) != expected.value:
        raise refuse("company USCC no longer equals the anchored value")
    identifier = uow.company_identifiers.get(expected.identifier_id)
    if (
        identifier is None
        or identifier.scheme != "uscc"
        or identifier.company_id != company.company_id
        or identifier.normalized_value != expected.value
        or identifier.status != "active"
        or identifier.source_access_id != expected.profile_source_access_id
    ):
        raise refuse(f"USCC identifier {expected.identifier_id} is not the active anchor")
    for row in uow.company_identifiers.list_by_scheme_value("uscc", expected.value):
        if row.company_id != company.company_id or row.status == "contested":
            raise refuse("USCC value is contested or held by another company")
    profile = uow.source_accesses.get(expected.profile_source_access_id)
    profile_snapshot = (
        profile.result_snapshot.get("profile")
        if profile is not None and isinstance(profile.result_snapshot, Mapping)
        else None
    )
    if (
        profile is None
        or profile.provider != PROVIDER
        or profile.provider_interface != PROFILE_INTERFACE
        or profile.status != "ok"
        or not isinstance(profile_snapshot, Mapping)
        or _normalized(_optional_str(profile_snapshot.get("uscc"))) != expected.value
        or profile_snapshot.get("security_code") != decision.current_code
    ):
        raise refuse(
            f"profile access {expected.profile_source_access_id} does not observe "
            "the anchored USCC for the current code"
        )
    current = uow.securities.get(decision.current_security_id)
    if (
        current is None
        or current.company_id != company.company_id
        or current.security_code != decision.current_code
        or current.exchange != decision.exchange
        or current.status == HISTORICAL_SECURITY_STATUS
    ):
        raise refuse(f"current security {decision.current_security_id} drifted")
    tracked = uow.tracked_companies.get_by_company_id(company.company_id)
    if tracked is None or tracked.security_id != current.security_id:
        raise refuse("tracked row no longer points at the current security")
    observation = decision.query_org_observation
    if observation is not None:
        rows = uow.company_identifiers.list_by_scheme_value(
            "cninfo_org_id", observation.value
        )
        if not rows or any(row.company_id != company.company_id for row in rows):
            raise refuse("query org observation is missing or held by another company")
        if not any(
            row.status == "active" and row.source_access_id == observation.source_access_id
            for row in rows
        ):
            raise refuse("query org observation is not the recorded profile context")
    return BindingAnchor(company=company, current_security=current, tracked=tracked)


def index_candidate(
    index_access: e.SourceAccess, provider_document_id: str
) -> Mapping[str, object]:
    """The one stored candidate of an index access for this provider id."""

    snapshot = index_access.result_snapshot
    candidates = snapshot.get("candidates") if isinstance(snapshot, Mapping) else None
    if not isinstance(candidates, list):
        raise HistoricalSecurityProvenanceError(
            f"index access {index_access.source_access_id} has no candidate list"
        )
    matches = [
        item
        for item in candidates
        if isinstance(item, Mapping)
        and item.get("provider_document_id") == provider_document_id
    ]
    try:
        identities = {candidate_sha256(item) for item in matches}
    except ContractViolation as exc:
        raise HistoricalSecurityProvenanceError(
            f"index access {index_access.source_access_id} candidate "
            f"{provider_document_id} cannot be hashed: {exc}"
        ) from exc
    if len(identities) != 1:
        raise HistoricalSecurityProvenanceError(
            f"index access {index_access.source_access_id} does not carry exactly one "
            f"candidate {provider_document_id}"
        )
    return matches[0]


def _resolve_historical(
    uow: UnitOfWork,
    *,
    candidate: Mapping[str, object],
    security: e.Security,
    company: e.Company,
    index_source_access_id: str | None,
    expected_binding_source_access_id: str | None,
) -> VerifiedAcquisitionSubject:
    label = f"{security.security_code}.{security.exchange}"
    if not index_source_access_id:
        raise HistoricalSecurityBindingRequiredError(
            f"historical security {label} needs the index source access that "
            "carried the candidate; use the evidence-bound registration path"
        )
    provider_document_id = _candidate_str(candidate, "provider_document_id")
    announcement_date = _candidate_date(candidate)
    try:
        candidate_hash = candidate_sha256(candidate)
    except ContractViolation as exc:
        raise HistoricalSecurityProvenanceError(
            f"candidate {provider_document_id} cannot be hashed: {exc}"
        ) from exc
    index = uow.source_accesses.get(index_source_access_id)
    if (
        index is None
        or index.provider != PROVIDER
        or index.provider_interface not in BINDABLE_INDEX_INTERFACES
        or index.status != "ok"
        or not index.result_hash
        or index.company_id is None
        or index.security_id is None
    ):
        raise HistoricalSecurityProvenanceError(
            f"index access {index_source_access_id} is not a complete provider index"
        )
    stored = index_candidate(index, provider_document_id)
    if candidate_sha256(stored) != candidate_hash:
        raise HistoricalSecurityProvenanceError(
            f"candidate {provider_document_id} differs from index access "
            f"{index_source_access_id}"
        )
    bindings = load_bindings(uow, security)
    if not bindings:
        raise HistoricalSecurityBindingRequiredError(
            f"historical security {label} has no recorded binding"
        )
    applicable = [
        binding
        for binding in bindings
        if binding.decision.approves(
            index_provider_interface=str(index.provider_interface),
            query_company_id=index.company_id,
            query_security_id=index.security_id,
            announcement_date=announcement_date,
        )
    ]
    if expected_binding_source_access_id is not None:
        applicable = [
            binding
            for binding in applicable
            if binding.source_access.source_access_id == expected_binding_source_access_id
        ]
    if not applicable:
        raise HistoricalSecurityProvenanceError(
            f"candidate {provider_document_id} ({announcement_date.isoformat()}, "
            f"{index.provider_interface}, query {index.company_id}/{index.security_id}) "
            f"is outside every approved binding of {label}"
        )
    chosen = min(applicable, key=lambda binding: binding.source_access.source_access_id)
    verify_binding_anchor(uow, chosen.decision)
    _check_org_consistency(
        uow,
        candidate=candidate,
        index=index,
        decision=chosen.decision,
        company=company,
    )
    return VerifiedAcquisitionSubject(
        subject=ResolvedSubject(company=company, security=security),
        provenance=HistoricalAcquisitionProvenance(
            index_source_access_id=index.source_access_id,
            index_result_hash=str(index.result_hash),
            candidate_sha256=candidate_hash,
            binding_source_access_id=chosen.source_access.source_access_id,
            binding_sha256=str(chosen.source_access.result_hash),
            acquisition_scope_company_id=index.company_id,
            acquisition_scope_security_id=index.security_id,
            original_candidate_code=security.security_code,
            exchange=security.exchange,
            _issuer=_ISSUER,
        ),
    )


def _check_org_consistency(
    uow: UnitOfWork,
    *,
    candidate: Mapping[str, object],
    index: e.SourceAccess,
    decision: HistoricalSecurityBindingV1,
    company: e.Company,
) -> None:
    """Org ids can only contradict a binding; agreement proves nothing."""

    observation = decision.query_org_observation
    snapshot = index.result_snapshot if isinstance(index.result_snapshot, Mapping) else {}
    context = snapshot.get("identity_context")
    if isinstance(context, Mapping):
        query_profile_org = _optional_str(context.get("query_profile_org_id"))
    else:
        # Legacy snapshots projected the query profile org onto every
        # candidate; it is query context, never a candidate observation.
        query_profile_org = _optional_str(candidate.get("provider_org_id"))
    if (
        observation is not None
        and query_profile_org is not None
        and query_profile_org != observation.value
    ):
        raise HistoricalSecurityProvenanceError(
            f"query profile org {query_profile_org} contradicts the binding's "
            f"observation {observation.value}"
        )
    own_org = _optional_str(candidate.get("candidate_provider_org_id"))
    if own_org is None:
        return
    if observation is not None and own_org != observation.value:
        raise HistoricalSecurityProvenanceError(
            f"candidate org {own_org} contradicts the binding's query org "
            f"{observation.value}; review before use"
        )
    for row in uow.company_identifiers.list_by_scheme_value("cninfo_org_id", own_org):
        if row.company_id != company.company_id:
            raise HistoricalSecurityProvenanceError(
                f"candidate org {own_org} is recorded for another company"
            )


def _candidate_str(candidate: Mapping[str, object], key: str) -> str:
    value = candidate.get(key)
    if value is None or value == "":
        raise RegistrationMetadataError(f"candidate missing field {key}")
    return str(value)


def _candidate_date(candidate: Mapping[str, object]) -> date:
    value = _candidate_str(candidate, "announcement_date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RegistrationMetadataError(
            f"candidate announcement_date is not an ISO date: {value!r}"
        ) from exc


def _optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _normalized(value: str | None) -> str | None:
    return value.strip().upper() if value else None


__all__ = [
    "BindingAnchor",
    "HistoricalAcquisitionProvenance",
    "RegistrationProvenance",
    "RetainedRecoveryProvenance",
    "VerifiedAcquisitionSubject",
    "VerifiedBinding",
    "candidate_security",
    "index_candidate",
    "load_bindings",
    "resolve_acquisition_subject",
    "verify_binding_anchor",
]
