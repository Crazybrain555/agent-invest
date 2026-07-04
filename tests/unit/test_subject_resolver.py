import unittest
from datetime import datetime, timezone

from disclosure_anchor.application.services.subject_resolver import (
    SubjectCandidate,
    SubjectResolver,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import SubjectIdentityConflictError
from tests.unit._fakes import FakeUnitOfWork


class SubjectResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.uow = FakeUnitOfWork()
        self.resolver = SubjectResolver()

    def test_creates_company_security_and_identifier_from_new_subject(self) -> None:
        result = self.resolver.resolve(
            self.uow,
            SubjectCandidate(
                security_code="002484",
                exchange="SZSE",
                legal_name="江海股份",
                credit_code="  uscc-1  ",
            ),
        )

        self.assertEqual(result.company.legal_name, "江海股份")
        self.assertEqual(result.security.company_id, result.company.company_id)
        identifiers = self.uow.company_identifiers.all()
        self.assertEqual(len(identifiers), 1)
        self.assertEqual(identifiers[0].scheme, "uscc")
        self.assertEqual(identifiers[0].normalized_value, "USCC-1")

    def test_security_match_backfills_missing_uscc_ledger(self) -> None:
        company = self.uow.companies.add(
            e.Company(company_id="co_1", legal_name="江海股份")
        )
        self.uow.securities.add(
            e.Security(
                security_id="sec_1",
                company_id=company.company_id,
                security_code="002484",
                exchange="SZSE",
            )
        )

        result = self.resolver.resolve(
            self.uow,
            SubjectCandidate(
                security_code="002484",
                exchange="SZSE",
                legal_name="江海股份",
                credit_code="uscc-2",
            ),
        )

        self.assertEqual(result.company.company_id, company.company_id)
        self.assertEqual(result.company.unified_social_credit_code, "uscc-2")
        identifiers = self.uow.company_identifiers.all()
        self.assertEqual(len(identifiers), 1)
        self.assertEqual(identifiers[0].company_id, company.company_id)

    def test_identifier_legal_name_mismatch_marks_identifier_contested(self) -> None:
        company = self.uow.companies.add(
            e.Company(company_id="co_1", legal_name="Old Name")
        )
        identifier = self.uow.company_identifiers.add(
            e.CompanyIdentifier(
                identifier_id="ci_1",
                company_id=company.company_id,
                scheme="uscc",
                raw_value="USCC-1",
                normalized_value="USCC-1",
                observed_at=datetime.now(timezone.utc),
            )
        )

        with self.assertRaises(SubjectIdentityConflictError):
            self.resolver.resolve(
                self.uow,
                SubjectCandidate(
                    security_code="002484",
                    exchange="SZSE",
                    legal_name="New Name",
                    credit_code="uscc-1",
                ),
            )

        self.assertEqual(identifier.status, "contested")

    def test_security_legal_name_mismatch_rejects_subject(self) -> None:
        company = self.uow.companies.add(
            e.Company(company_id="co_1", legal_name="Old Name")
        )
        self.uow.securities.add(
            e.Security(
                security_id="sec_1",
                company_id=company.company_id,
                security_code="002484",
                exchange="SZSE",
            )
        )

        with self.assertRaises(SubjectIdentityConflictError):
            self.resolver.resolve(
                self.uow,
                SubjectCandidate(
                    security_code="002484",
                    exchange="SZSE",
                    legal_name="New Name",
                ),
            )

    def test_security_uscc_conflict_is_contested_before_name_rejection(self) -> None:
        company = self.uow.companies.add(
            e.Company(
                company_id="co_1",
                legal_name="Old Name",
                unified_social_credit_code="OLD-USCC",
            )
        )
        self.uow.securities.add(
            e.Security(
                security_id="sec_1",
                company_id=company.company_id,
                security_code="002484",
                exchange="SZSE",
            )
        )

        with self.assertRaises(SubjectIdentityConflictError):
            self.resolver.resolve(
                self.uow,
                SubjectCandidate(
                    security_code="002484",
                    exchange="SZSE",
                    legal_name="New Name",
                    credit_code="NEW-USCC",
                ),
            )

        identifiers = self.uow.company_identifiers.all()
        self.assertEqual(len(identifiers), 1)
        self.assertEqual(identifiers[0].status, "contested")
        self.assertEqual(identifiers[0].normalized_value, "NEW-USCC")
        self.assertEqual(self.uow.commit_count, 1)

    def test_security_new_uscc_is_contested_before_name_rejection(self) -> None:
        company = self.uow.companies.add(
            e.Company(company_id="co_1", legal_name="Old Name")
        )
        self.uow.securities.add(
            e.Security(
                security_id="sec_1",
                company_id=company.company_id,
                security_code="002484",
                exchange="SZSE",
            )
        )

        with self.assertRaises(SubjectIdentityConflictError):
            self.resolver.resolve(
                self.uow,
                SubjectCandidate(
                    security_code="002484",
                    exchange="SZSE",
                    legal_name="New Name",
                    credit_code="NEW-USCC",
                ),
            )

        identifiers = self.uow.company_identifiers.all()
        self.assertEqual(len(identifiers), 1)
        self.assertEqual(identifiers[0].status, "contested")
        self.assertEqual(identifiers[0].normalized_value, "NEW-USCC")
        self.assertIsNone(company.unified_social_credit_code)
        self.assertEqual(self.uow.commit_count, 1)


if __name__ == "__main__":
    unittest.main()
