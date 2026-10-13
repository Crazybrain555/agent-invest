"""Independent identity acceptance for an evidence-bound historical code."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import unittest

from disclosure_anchor.application.contracts.historical_security_registration import (
    load_binding,
)
from disclosure_anchor.application.services.source_security_resolution import (
    resolve_acquisition_subject,
)
from disclosure_anchor.application.services.subject_resolver import SubjectCandidate, SubjectResolver
from disclosure_anchor.application.use_cases.historical_security_binding import (
    BindingEvidence,
    HistoricalSecurityBinding,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import (
    HistoricalSecurityBindingRequiredError,
    RegistrationMetadataError,
    SourceRecoveryError,
)
from tests.unit._fakes import FakeUnitOfWork
from tests.unit.test_historical_security_contract import (
    _COMPANY,
    _CURRENT_SECURITY,
    _HASH_A,
    _HASH_B,
    _IDENTIFIER,
    _INDEX_ACCESS,
    _OLD_SECURITY,
    _PROFILE_ACCESS,
    _binding_document,
    _json_bytes,
)


_NOW = datetime(2025, 9, 25, tzinfo=timezone.utc)
_CODE_IDENTITY = "sha256:" + "9" * 64
_ORG = "profile-org-context"


def _seed_anchor() -> FakeUnitOfWork:
    uow = FakeUnitOfWork()
    # The default unit fakes predate the historical read/lock ports.
    uow.securities.get_for_update = uow.securities.get
    uow.company_identifiers.list_by_scheme_value = lambda scheme, value: [
        row for row in uow.company_identifiers.all()
        if row.scheme == scheme and row.normalized_value == value
    ]
    uow.source_accesses.list_historical_security_bindings = lambda security_id: [
        row for row in uow.source_accesses.all()
        if row.provider_interface == "local:historical_security_binding.v1"
        and row.security_id == security_id
    ]
    uow.companies.add(e.Company(
        company_id=_COMPANY, legal_name="上市主体",
        unified_social_credit_code="91320000TEST000001",
    ))
    uow.securities.add(e.Security(
        security_id=_CURRENT_SECURITY, company_id=_COMPANY,
        security_code="302132", exchange="SZSE", status="active",
    ))
    uow.tracked_companies.add(e.TrackedCompany(
        tracked_company_id="tc_" + "8" * 26,
        company_id=_COMPANY, security_id=_CURRENT_SECURITY,
    ))
    uow.source_accesses.add(e.SourceAccess(
        source_access_id=_PROFILE_ACCESS, provider="cninfo",
        provider_interface="cninfo:p_stock2100", accessed_at=_NOW, status="ok",
        result_snapshot={"profile": {
            "uscc": "91320000TEST000001", "security_code": "302132",
        }},
    ))
    uow.company_identifiers.add(e.CompanyIdentifier(
        identifier_id=_IDENTIFIER, company_id=_COMPANY, scheme="uscc",
        raw_value="91320000TEST000001", normalized_value="91320000TEST000001",
        source_access_id=_PROFILE_ACCESS, observed_at=_NOW,
    ))
    uow.company_identifiers.add(e.CompanyIdentifier(
        identifier_id="ci_" + "A" * 26, company_id=_COMPANY,
        scheme="cninfo_org_id", raw_value=_ORG, normalized_value=_ORG,
        source_access_id=_PROFILE_ACCESS, observed_at=_NOW,
    ))
    return uow


def _candidate() -> dict[str, object]:
    return {
        "provider_document_id": "pid-1",
        "security_code": "300114",
        "exchange": "SZSE",
        "announcement_date": "2024-03-01",
        "title": "原始中文标题",
        "provider_org_id": _ORG,
    }


def _bind(uow: FakeUnitOfWork) -> tuple[str, str]:
    binding = load_binding(_json_bytes(_binding_document()))
    use_case = HistoricalSecurityBinding(
        uow_factory=lambda: uow, code_identity=_CODE_IDENTITY, clock=lambda: _NOW,
    )
    evidence = BindingEvidence(sha256=_HASH_A, byte_count=123)
    plan = use_case.preview(binding, evidence=evidence)
    result = use_case.execute(plan, evidence=evidence, decided_by="fixture reviewer")
    return result.binding_source_access_id, result.historical_security_id


def _seed_index(uow: FakeUnitOfWork, candidate: dict[str, object]) -> None:
    uow.source_accesses.add(e.SourceAccess(
        source_access_id=_INDEX_ACCESS, provider="cninfo",
        provider_interface="cninfo:p_info3015", accessed_at=_NOW, status="ok",
        result_hash=_HASH_B, company_id=_COMPANY,
        security_id=_CURRENT_SECURITY,
        result_snapshot={
            "candidates": [deepcopy(candidate)],
            "identity_context": {"query_profile_org_id": _ORG},
        },
    ))


class HistoricalSecurityBindingAcceptanceTests(unittest.TestCase):
    def test_named_binding_creates_only_old_security_and_is_idempotent(self) -> None:
        uow = _seed_anchor()
        binding = load_binding(_json_bytes(_binding_document()))
        use_case = HistoricalSecurityBinding(
            uow_factory=lambda: uow, code_identity=_CODE_IDENTITY, clock=lambda: _NOW,
        )
        evidence = BindingEvidence(sha256=_HASH_A, byte_count=123)
        plan = use_case.preview(binding, evidence=evidence)
        self.assertEqual(len(uow.securities.all()), 1)
        self.assertEqual(uow.commit_count, 0)

        first = use_case.execute(plan, evidence=evidence, decided_by="fixture reviewer")
        second = use_case.execute(plan, evidence=evidence, decided_by="fixture reviewer")
        self.assertTrue(first.created_historical_security)
        self.assertTrue(first.recorded)
        self.assertFalse(second.recorded)
        self.assertEqual(second.binding_source_access_id, first.binding_source_access_id)
        self.assertEqual(len(uow.securities.all()), 2)
        old = uow.securities.get(first.historical_security_id)
        self.assertEqual((old.company_id, old.security_code, old.status), (_COMPANY, "300114", "historical"))
        self.assertEqual(uow.tracked_companies.get_by_company_id(_COMPANY).security_id, _CURRENT_SECURITY)
        self.assertEqual(uow.companies.get(_COMPANY).unified_social_credit_code, "91320000TEST000001")
        access = uow.source_accesses.get(first.binding_source_access_id)
        self.assertEqual(access.result_hash, binding.sha256())
        self.assertEqual(access.result_snapshot, binding.to_document())
        self.assertEqual(uow.commit_count, 1)

    def test_binding_refuses_missing_or_conflicting_strong_evidence_without_mutation(self) -> None:
        for label in ("missing profile USCC", "ambiguous org owner", "old code belongs elsewhere"):
            with self.subTest(label=label):
                uow = _seed_anchor()
                if label == "missing profile USCC":
                    uow.source_accesses.get(_PROFILE_ACCESS).result_snapshot = {"profile": {
                        "uscc": None, "security_code": "302132",
                    }}
                elif label == "ambiguous org owner":
                    uow.company_identifiers.add(e.CompanyIdentifier(
                        identifier_id="ci_" + "B" * 26,
                        company_id="co_" + "C" * 26, scheme="cninfo_org_id",
                        raw_value=_ORG, normalized_value=_ORG,
                        source_access_id=_PROFILE_ACCESS, observed_at=_NOW,
                    ))
                else:
                    uow.securities.add(e.Security(
                        security_id=_OLD_SECURITY, company_id="co_" + "C" * 26,
                        security_code="300114", exchange="SZSE", status="historical",
                    ))
                binding = load_binding(_json_bytes(_binding_document()))
                use_case = HistoricalSecurityBinding(
                    uow_factory=lambda: uow, code_identity=_CODE_IDENTITY,
                )
                before = (len(uow.securities.all()), len(uow.source_accesses.all()))
                with self.assertRaises(SourceRecoveryError):
                    use_case.preview(
                        binding, evidence=BindingEvidence(sha256=_HASH_A, byte_count=123),
                    )
                self.assertEqual(
                    (len(uow.securities.all()), len(uow.source_accesses.all())), before,
                )
                self.assertEqual(uow.commit_count, 0)

    def test_historical_candidate_requires_exact_index_scope_and_binding(self) -> None:
        uow = _seed_anchor()
        binding_access_id, historical_security_id = _bind(uow)
        candidate = _candidate()
        _seed_index(uow, candidate)
        resolver = SubjectResolver()
        verified = resolve_acquisition_subject(
            uow, candidate=candidate, index_source_access_id=_INDEX_ACCESS,
            expected_binding_source_access_id=binding_access_id,
            subject_resolver=resolver,
        )
        self.assertEqual(verified.subject.security.security_id, historical_security_id)
        self.assertEqual(verified.subject.company.company_id, _COMPANY)
        self.assertEqual(verified.provenance.index_source_access_id, _INDEX_ACCESS)
        self.assertEqual(verified.provenance.binding_source_access_id, binding_access_id)

        with self.assertRaises(HistoricalSecurityBindingRequiredError):
            resolve_acquisition_subject(
                uow, candidate=candidate, index_source_access_id=None,
                subject_resolver=resolver,
                expected_binding_source_access_id=binding_access_id,
            )
        with self.assertRaisesRegex(RegistrationMetadataError, "differs from index access"):
            resolve_acquisition_subject(
                uow, candidate={**candidate, "title": "different"},
                index_source_access_id=_INDEX_ACCESS, subject_resolver=resolver,
                expected_binding_source_access_id=binding_access_id,
            )
        index = uow.source_accesses.get(_INDEX_ACCESS)
        original_snapshot = index.result_snapshot
        try:
            for label, changed_candidate, message in (
                ("out of range", {**candidate, "announcement_date": "2025-03-01"},
                 "outside every approved binding"),
                ("candidate org contradiction",
                 {**candidate, "candidate_provider_org_id": "other"}, "candidate org"),
            ):
                with self.subTest(label=label):
                    index.result_snapshot = {
                        "candidates": [deepcopy(changed_candidate)],
                        "identity_context": {"query_profile_org_id": _ORG},
                    }
                    with self.assertRaisesRegex(RegistrationMetadataError, message):
                        resolve_acquisition_subject(
                            uow, candidate=changed_candidate,
                            index_source_access_id=_INDEX_ACCESS, subject_resolver=resolver,
                            expected_binding_source_access_id=binding_access_id,
                        )
        finally:
            index.result_snapshot = original_snapshot
        with self.assertRaises(HistoricalSecurityBindingRequiredError):
            resolver.resolve(
                uow, SubjectCandidate(
                    security_code="300114", exchange="SZSE",
                    legal_name="上市主体", credit_code="91320000TEST000001",
                ),
            )
        self.assertEqual(uow.tracked_companies.get_by_company_id(_COMPANY).security_id, _CURRENT_SECURITY)

    def test_current_security_keeps_ordinary_resolution_and_query_org_is_not_uscc(self) -> None:
        uow = _seed_anchor()
        current = resolve_acquisition_subject(
            uow, candidate={"security_code": "302132", "exchange": "SZSE"},
            index_source_access_id=None, subject_resolver=SubjectResolver(),
        )
        self.assertEqual(current.subject.security.security_id, _CURRENT_SECURITY)
        self.assertIsNone(current.provenance)
        self.assertEqual(uow.companies.get(_COMPANY).unified_social_credit_code, "91320000TEST000001")
        self.assertNotEqual(_ORG, uow.companies.get(_COMPANY).unified_social_credit_code)


if __name__ == "__main__":
    unittest.main()
