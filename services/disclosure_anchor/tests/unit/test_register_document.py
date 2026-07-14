import unittest
from datetime import date
from pathlib import Path

from disclosure_anchor.application.ports.file_store import (
    QuarantineResult,
    RawDocumentWriteResult,
)
from disclosure_anchor.application.services.register_document import (
    DocumentRegistration,
    register_document,
)
from disclosure_anchor.application.services.subject_resolver import ResolvedSubject
from disclosure_anchor.application.use_cases.register_local_pdf import (
    RegisterLocalPdf,
    RegisterLocalPdfCommand,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import (
    DocumentIdentityConflictError,
    InvalidRawDocumentError,
    SubjectIdentityConflictError,
    SubjectIdentityRaceError,
)
from disclosure_anchor.domain.value_objects import ReportPeriod
from tests.unit._fakes import FakeUnitOfWork


def _subject(uow: FakeUnitOfWork) -> ResolvedSubject:
    company = uow.companies.add(e.Company(company_id="co_1", legal_name="江海股份"))
    security = uow.securities.add(
        e.Security(
            security_id="sec_1",
            company_id=company.company_id,
            security_code="002484",
            exchange="SZSE",
        )
    )
    return ResolvedSubject(company=company, security=security)


def _doc_meta(*, report_period: ReportPeriod | None = None) -> DocumentRegistration:
    return DocumentRegistration(
        provider="cninfo",
        provider_document_id="1225000001",
        title="公告",
        announcement_date=date(2026, 7, 5),
        report_period=report_period,
        filename="sample.pdf",
    )


def _raw(raw_hash: str = "sha256:raw") -> RawDocumentWriteResult:
    return RawDocumentWriteResult(
        relpath=Path("raw_documents/cninfo/002484/2026/1225000001/sample.pdf"),
        raw_file_hash=raw_hash,
        byte_count=100,
        created=True,
    )


class _SubjectResolver:
    def __init__(self, subject: ResolvedSubject) -> None:
        self.subject = subject

    def resolve(self, *_):
        return self.subject


class _FailingSubjectResolver:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def resolve(self, *_):
        self.calls += 1
        raise self.exc


class _RacingSubjectResolver:
    def __init__(self, subject: ResolvedSubject) -> None:
        self.subject = subject
        self.calls = 0

    def resolve(self, *_):
        self.calls += 1
        if self.calls == 1:
            raise SubjectIdentityRaceError("raced subject unique")
        return self.subject


class _RawStore:
    def __init__(self, *, fail_put: bool = False) -> None:
        self.fail_put = fail_put
        self.put_calls = 0

    def put_raw_document(self, **kwargs) -> RawDocumentWriteResult:
        self.put_calls += 1
        expected = kwargs.get("expected_raw_file_hash")
        if self.fail_put or (expected is not None and expected != "sha256:raw"):
            raise InvalidRawDocumentError("expected hash mismatch")
        return _raw()

    def verify_raw_document(self, **_):
        raise AssertionError("not used by register tests")

    def quarantine_raw_document(self, **kwargs) -> QuarantineResult:
        return QuarantineResult(
            path=Path("runtime/quarantine") / kwargs["input_file"].name,
            reason=kwargs["reason"],
            byte_count=12,
        )


class RegisterDocumentTests(unittest.TestCase):
    def test_supersedes_prior_document_with_same_provider_document_id(self) -> None:
        uow = FakeUnitOfWork()
        subject = _subject(uow)
        prior = uow.documents.add(
            e.Document(
                document_id="doc_old",
                status="registered",
                provider="cninfo",
                provider_document_id="1225000001",
                raw_file_hash="sha256:old",
            )
        )

        outcome = register_document(
            uow,
            subject=subject,
            doc_meta=_doc_meta(report_period=ReportPeriod.parse("2025A")),
            raw=_raw("sha256:new"),
        )

        self.assertFalse(outcome.reused_existing_document)
        self.assertEqual(outcome.document.supersedes_document_id, prior.document_id)
        self.assertEqual(outcome.outbox_event.event_kind, "document_registered")
        self.assertEqual(outcome.outbox_event.change_kind, "materialized")

    def test_reused_document_emits_observed_event(self) -> None:
        uow = FakeUnitOfWork()
        subject = _subject(uow)
        existing = uow.documents.add(
            e.Document(
                document_id="doc_existing",
                status="registered",
                provider="cninfo",
                provider_document_id="1225000001",
                raw_file_hash="sha256:raw",
            )
        )

        outcome = register_document(
            uow,
            subject=subject,
            doc_meta=_doc_meta(),
            raw=_raw(),
        )

        self.assertTrue(outcome.reused_existing_document)
        self.assertIs(outcome.document, existing)
        self.assertEqual(outcome.outbox_event.event_kind, "document_observed")
        self.assertEqual(outcome.outbox_event.change_kind, "observed")
        self.assertEqual(outcome.outbox_event.subject_kind, "document")
        self.assertEqual(outcome.outbox_event.subject_ref, existing.document_id)

    def test_register_local_pdf_recovers_document_identity_race_by_reuse(self) -> None:
        uow = FakeUnitOfWork()
        subject = _subject(uow)
        original_add = uow.documents.add
        state = {"raised": False}

        def add_with_race(document: e.Document) -> e.Document:
            if not state["raised"]:
                state["raised"] = True
                original_add(
                    e.Document(
                        document_id="doc_raced",
                        status="registered",
                        provider=document.provider,
                        provider_document_id=document.provider_document_id,
                        raw_file_hash=document.raw_file_hash,
                    )
                )
                raise DocumentIdentityConflictError("raced insert")
            return original_add(document)

        uow.documents.add = add_with_race
        use_case = RegisterLocalPdf(
            raw_store=_RawStore(),
            uow_factory=lambda: uow,
            subject_resolver=_SubjectResolver(subject),
        )

        result = use_case.execute(
            RegisterLocalPdfCommand(
                file_path=Path("sample.pdf"),
                company_legal_name="江海股份",
                security_code="002484",
                exchange="SZSE",
                filing_type="other",
                title="公告",
                announcement_date=date(2026, 7, 5),
                provider_document_id="1225000001",
                provider="cninfo",
            )
        )

        self.assertTrue(result.reused_existing_document)
        self.assertEqual(result.document_id, "doc_raced")
        self.assertEqual(uow.commit_count, 1)

    def test_register_local_pdf_retries_subject_identity_race_only(self) -> None:
        uow = FakeUnitOfWork()
        subject = _subject(uow)
        resolver = _RacingSubjectResolver(subject)
        use_case = RegisterLocalPdf(
            raw_store=_RawStore(),
            uow_factory=lambda: uow,
            subject_resolver=resolver,
        )

        result = use_case.execute(
            RegisterLocalPdfCommand(
                file_path=Path("sample.pdf"),
                company_legal_name="江海股份",
                security_code="002484",
                exchange="SZSE",
                filing_type="other",
                title="公告",
                announcement_date=date(2026, 7, 5),
                provider_document_id="1225000001",
                provider="cninfo",
            )
        )

        self.assertIsNotNone(result.document_id)
        self.assertEqual(resolver.calls, 2)

    def test_register_local_pdf_does_not_retry_semantic_subject_conflict(self) -> None:
        uow = FakeUnitOfWork()
        resolver = _FailingSubjectResolver(
            SubjectIdentityConflictError("semantic identity conflict")
        )
        use_case = RegisterLocalPdf(
            raw_store=_RawStore(),
            uow_factory=lambda: uow,
            subject_resolver=resolver,
        )

        with self.assertRaises(SubjectIdentityConflictError):
            use_case.execute(
                RegisterLocalPdfCommand(
                    file_path=Path("sample.pdf"),
                    company_legal_name="江海股份",
                    security_code="002484",
                    exchange="SZSE",
                    filing_type="other",
                    title="公告",
                    announcement_date=date(2026, 7, 5),
                    provider_document_id="1225000001",
                    provider="cninfo",
                )
            )

        self.assertEqual(resolver.calls, 1)

    def test_preflight_exempts_pending_legal_name_placeholder(self) -> None:
        # `make track` seeds a placeholder ledger name; registering with the
        # real legal name before the first credentialed sync must succeed and
        # upgrade the placeholder, never contest it (round23).
        uow = FakeUnitOfWork()
        company = uow.companies.add(
            e.Company(
                company_id="co_pending",
                legal_name="PENDING_LEGAL_NAME 002484.SZSE",
            )
        )
        uow.securities.add(
            e.Security(
                security_id="sec_pending",
                company_id=company.company_id,
                security_code="002484",
                exchange="SZSE",
            )
        )
        use_case = RegisterLocalPdf(raw_store=_RawStore(), uow_factory=lambda: uow)

        result = use_case.execute(
            RegisterLocalPdfCommand(
                file_path=Path("sample.pdf"),
                company_legal_name="江海股份",
                security_code="002484",
                exchange="SZSE",
                filing_type="other",
                title="公告",
                announcement_date=date(2026, 7, 5),
                provider_document_id="1225000001",
                provider="cninfo",
                company_credit_code="91320600725062086F",
            )
        )

        self.assertIsNotNone(result.document_id)
        refreshed = uow.companies.get(company.company_id)
        self.assertEqual(refreshed.legal_name, "江海股份")
        contested = [
            row for row in uow.company_identifiers.all() if row.status == "contested"
        ]
        self.assertEqual(contested, [])

    def test_preflight_still_rejects_real_legal_name_mismatch(self) -> None:
        uow = FakeUnitOfWork()
        company = uow.companies.add(
            e.Company(company_id="co_real", legal_name="江海股份")
        )
        uow.securities.add(
            e.Security(
                security_id="sec_real",
                company_id=company.company_id,
                security_code="002484",
                exchange="SZSE",
            )
        )
        use_case = RegisterLocalPdf(raw_store=_RawStore(), uow_factory=lambda: uow)

        with self.assertRaises(SubjectIdentityConflictError):
            use_case.execute(
                RegisterLocalPdfCommand(
                    file_path=Path("sample.pdf"),
                    company_legal_name="完全不同的公司名",
                    security_code="002484",
                    exchange="SZSE",
                    filing_type="other",
                    title="公告",
                    announcement_date=date(2026, 7, 5),
                    provider_document_id="1225000001",
                    provider="cninfo",
                )
            )

    def test_hash_mismatch_is_quarantined_and_records_failed_source_access(self) -> None:
        uow = FakeUnitOfWork()
        use_case = RegisterLocalPdf(raw_store=_RawStore(), uow_factory=lambda: uow)

        result = use_case.execute(
            RegisterLocalPdfCommand(
                file_path=Path("sample.pdf"),
                company_legal_name="江海股份",
                security_code="002484",
                exchange="SZSE",
                filing_type="other",
                title="公告",
                announcement_date=date(2026, 7, 5),
                provider_document_id="1225000001",
                provider="cninfo",
                expected_raw_file_hash="sha256:wrong",
            )
        )

        self.assertIsNone(result.document_id)
        self.assertEqual(result.quarantine_reason, "invalid_raw_document")
        source_access = uow.source_accesses.get(result.source_access_id)
        self.assertIsNotNone(source_access)
        self.assertEqual(source_access.status, "failed")
        self.assertIn("expected hash mismatch", source_access.error)
        self.assertEqual(len(uow.documents.all()), 0)

    def test_report_period_required_only_for_periodic_filings(self) -> None:
        with self.assertRaises(ValueError):
            RegisterLocalPdfCommand(
                file_path=Path("sample.pdf"),
                company_legal_name="江海股份",
                security_code="002484",
                exchange="SZSE",
                filing_type="annual_report",
                title="公告",
                announcement_date=date(2026, 7, 5),
                provider_document_id="1225000001",
                provider="cninfo",
            )

        command = RegisterLocalPdfCommand(
            file_path=Path("sample.pdf"),
            company_legal_name="江海股份",
            security_code="002484",
            exchange="SZSE",
            filing_type="other",
            title="公告",
            announcement_date=date(2026, 7, 5),
            provider_document_id="1225000001",
            provider="cninfo",
        )
        self.assertIsNone(command.report_period)

    def test_cninfo_provider_document_id_requires_numeric_textid(self) -> None:
        for provider_document_id in (
            "年度报告目录",
            "local-deadbeef",
            "periodic",
            "１２２５０８７１６９",
            "1" * 129,
        ):
            with self.subTest(provider_document_id=provider_document_id):
                with self.assertRaisesRegex(ValueError, "numeric TEXTID"):
                    RegisterLocalPdfCommand(
                        file_path=Path("sample.pdf"),
                        company_legal_name="江海股份",
                        security_code="002484",
                        exchange="SZSE",
                        filing_type="other",
                        title="公告",
                        announcement_date=date(2026, 7, 5),
                        provider_document_id=provider_document_id,
                        provider="cninfo",
                    )

    def test_command_canonicalizes_security_identity(self) -> None:
        command = RegisterLocalPdfCommand(
            file_path=Path("sample.pdf"),
            company_legal_name="江海股份",
            security_code=" 002484 ",
            exchange=" szse ",
            filing_type="other",
            title="公告",
            announcement_date=date(2026, 7, 5),
            provider_document_id="1225087169",
            provider="cninfo",
        )

        self.assertEqual(command.security_code, "002484")
        self.assertEqual(command.exchange, "SZSE")


if __name__ == "__main__":
    unittest.main()
