"""Resolve filing subject identity from security and identifier candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import (
    RegistrationMetadataError,
    SubjectIdentityConflictError,
)
from disclosure_anchor.domain.value_objects import canonical_security_identity


# Offline batch intake (pipeline track) creates companies before any provider
# profile is fetched; the placeholder upgrades in place on the first sync that
# supplies a real legal name — it must never count as a conflicting name.
PENDING_LEGAL_NAME_PREFIX = "PENDING_LEGAL_NAME "


@dataclass(frozen=True)
class SubjectCandidate:
    # legal_name=None means the caller makes no legal-name claim (e.g. the
    # credential-free web channel has no company profile); absence of a claim
    # is not a conflict, and a placeholder is used only if a company must be
    # created.
    security_code: str
    exchange: str
    legal_name: str | None
    board: str | None = None
    credit_code: str | None = None

    def __post_init__(self) -> None:
        security_code, exchange = canonical_security_identity(
            self.security_code, self.exchange
        )
        object.__setattr__(self, "security_code", security_code)
        object.__setattr__(self, "exchange", exchange)


@dataclass(frozen=True)
class ResolvedSubject:
    company: e.Company
    security: e.Security


class SubjectResolver:
    """Apply the 04R-D5 subject resolution order inside an active UoW."""

    def resolve(self, uow: UnitOfWork, candidate: SubjectCandidate) -> ResolvedSubject:
        security = uow.securities.get_by_code_exchange(
            candidate.security_code, candidate.exchange
        )
        if security is not None:
            company = self._company_for_security(uow, security)
            company = self._upgrade_placeholder_name(uow, company, candidate)
            identifier = (
                uow.company_identifiers.get_by_scheme_value(
                    "uscc", _normalize_identifier(candidate.credit_code)
                )
                if candidate.credit_code
                else None
            )
            if (
                candidate.legal_name is not None
                and company.legal_name != candidate.legal_name
                and candidate.credit_code
            ):
                if identifier is not None:
                    identifier.status = "contested"
                    uow.company_identifiers.update(identifier)
                else:
                    self._add_contested_uscc_identifier(
                        uow, company, candidate.credit_code
                    )
                uow.commit()
                raise SubjectIdentityConflictError(
                    "subject legal_name mismatch: "
                    f"{candidate.security_code}.{candidate.exchange} belongs to "
                    f"{company.legal_name!r}, got {candidate.legal_name!r}"
                )
            company = self._sync_uscc_identifier(uow, company, candidate.credit_code)
            self._validate_legal_name(
                company=company, candidate=candidate, identifier=identifier, uow=uow
            )
            return ResolvedSubject(company=company, security=security)

        if candidate.credit_code:
            identifier = uow.company_identifiers.get_by_scheme_value(
                "uscc", _normalize_identifier(candidate.credit_code)
            )
            if identifier is not None:
                ledger_company = uow.companies.get(identifier.company_id)
                if ledger_company is None:
                    raise RegistrationMetadataError(
                        "company identifier references missing company "
                        f"{identifier.company_id}"
                    )
                self._validate_legal_name(
                    company=ledger_company,
                    candidate=candidate,
                    identifier=identifier,
                    uow=uow,
                )
                ledger_company = self._sync_uscc_identifier(
                    uow, ledger_company, candidate.credit_code
                )
                security = self._add_security(
                    uow, company=ledger_company, candidate=candidate
                )
                return ResolvedSubject(company=ledger_company, security=security)

        company = uow.companies.add(
            e.Company(
                company_id=ids.new_company_id(),
                legal_name=candidate.legal_name
                or f"{PENDING_LEGAL_NAME_PREFIX}{candidate.security_code}.{candidate.exchange}",
                unified_social_credit_code=candidate.credit_code,
            )
        )
        if candidate.credit_code:
            self._add_uscc_identifier(uow, company, candidate.credit_code)
        security = self._add_security(uow, company=company, candidate=candidate)
        return ResolvedSubject(company=company, security=security)

    def _company_for_security(self, uow: UnitOfWork, security: e.Security) -> e.Company:
        company = uow.companies.get(security.company_id)
        if company is None:
            raise RegistrationMetadataError(
                f"security {security.security_id} references missing company "
                f"{security.company_id}"
            )
        return company

    def _upgrade_placeholder_name(
        self, uow: UnitOfWork, company: e.Company, candidate: SubjectCandidate
    ) -> e.Company:
        if (
            candidate.legal_name
            and company.legal_name.startswith(PENDING_LEGAL_NAME_PREFIX)
        ):
            company.legal_name = candidate.legal_name
            return uow.companies.update(company)
        return company

    def _validate_legal_name(
        self,
        *,
        company: e.Company,
        candidate: SubjectCandidate,
        identifier: e.CompanyIdentifier | None = None,
        uow: UnitOfWork | None = None,
    ) -> None:
        if candidate.legal_name is None or company.legal_name == candidate.legal_name:
            return
        if company.legal_name.startswith(PENDING_LEGAL_NAME_PREFIX):
            return
        if identifier is not None and uow is not None:
            identifier.status = "contested"
            uow.company_identifiers.update(identifier)
            uow.commit()
        raise SubjectIdentityConflictError(
            "subject legal_name mismatch: "
            f"{candidate.security_code}.{candidate.exchange} belongs to "
            f"{company.legal_name!r}, got {candidate.legal_name!r}"
        )

    def _sync_uscc_identifier(
        self, uow: UnitOfWork, company: e.Company, credit_code: str | None
    ) -> e.Company:
        if not credit_code:
            return company
        normalized = _normalize_identifier(credit_code)
        active = uow.company_identifiers.get_by_scheme_value("uscc", normalized)
        if active is not None and active.company_id != company.company_id:
            active.status = "contested"
            uow.company_identifiers.update(active)
            uow.commit()
            raise SubjectIdentityConflictError(
                "uscc strong identifier belongs to a different company"
            )
        if (
            company.unified_social_credit_code
            and _normalize_identifier(company.unified_social_credit_code) != normalized
        ):
            self._add_contested_uscc_identifier(uow, company, credit_code)
            uow.commit()
            raise SubjectIdentityConflictError(
                "company unified_social_credit_code conflicts with candidate uscc"
            )
        if not company.unified_social_credit_code:
            company.unified_social_credit_code = credit_code
            company = uow.companies.update(company)
        if active is None:
            self._add_uscc_identifier(uow, company, credit_code)
        return company

    def _add_security(
        self, uow: UnitOfWork, *, company: e.Company, candidate: SubjectCandidate
    ) -> e.Security:
        return uow.securities.add(
            e.Security(
                security_id=ids.new_security_id(),
                company_id=company.company_id,
                security_code=candidate.security_code,
                exchange=candidate.exchange,
                board=candidate.board,
                status="active",
            )
        )

    def _add_uscc_identifier(
        self, uow: UnitOfWork, company: e.Company, credit_code: str
    ) -> e.CompanyIdentifier:
        return uow.company_identifiers.add(
            e.CompanyIdentifier(
                identifier_id=ids.new_company_identifier_id(),
                company_id=company.company_id,
                scheme="uscc",
                raw_value=credit_code,
                normalized_value=_normalize_identifier(credit_code),
                jurisdiction="CN",
                status="active",
                observed_at=_observed_at(company),
            )
        )

    def _add_contested_uscc_identifier(
        self, uow: UnitOfWork, company: e.Company, credit_code: str
    ) -> e.CompanyIdentifier:
        return uow.company_identifiers.add(
            e.CompanyIdentifier(
                identifier_id=ids.new_company_identifier_id(),
                company_id=company.company_id,
                scheme="uscc",
                raw_value=credit_code,
                normalized_value=_normalize_identifier(credit_code),
                jurisdiction="CN",
                status="contested",
                observed_at=_observed_at(company),
            )
        )


def _normalize_identifier(value: str) -> str:
    return value.strip().upper()


def _observed_at(company: e.Company) -> datetime:
    return company.created_at or datetime.now(timezone.utc)
