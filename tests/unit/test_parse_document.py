import unittest
from pathlib import Path
from typing import Any

from disclosure_anchor.application.ports.file_store import (
    ArtifactWriteResult,
    RawDocumentVerification,
)
from disclosure_anchor.application.ports.parser import (
    ParserIdentity,
    ParserOptions,
    ParserResult,
)
from disclosure_anchor.application.use_cases.parse_document import (
    ParseDocument,
    ParseDocumentCommand,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import (
    ParseDocumentError,
    ParserInvocationError,
    ParserOutputContractError,
    ParserTimeoutError,
    ParserUnknownError,
    ParserVersionProbeError,
)
from tests.unit._fakes import FakeUnitOfWork


class _PathBuilder:
    def __init__(self) -> None:
        self.root = Path("/tmp/disclosure-anchor-test")

    def data_path(self, relpath: Path) -> Path:
        return self.root / relpath

    def parser_run_artifacts_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        return (
            Path("parser_artifacts")
            / provider
            / security_code
            / provider_document_id
            / processing_run_id
        )

    def normalized_ir_run_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        return (
            Path("derived/normalized_ir")
            / provider
            / security_code
            / provider_document_id
            / processing_run_id
            / "normalized_ir.v2.json"
        )


class _RawStore:
    def __init__(self, *, ok: bool = True, actual_hash: str | None = "sha256:raw") -> None:
        self.ok = ok
        self.actual_hash = actual_hash

    def verify_raw_document(self, *, relpath: Path, expected_hash: str):
        return RawDocumentVerification(
            relpath=relpath,
            expected_hash=expected_hash,
            actual_hash=self.actual_hash,
            ok=self.ok,
            message="ok" if self.ok else "raw verification failed",
        )

    def put_raw_document(self, **_):
        raise AssertionError("not used by parse tests")

    def quarantine_raw_document(self, **_):
        raise AssertionError("not used by parse tests")


class _ArtifactStore:
    def __init__(self) -> None:
        self.payloads: dict[str, object] = {}

    def write_json_atomic(self, *, relpath: Path, payload: object):
        self.payloads[str(relpath)] = payload
        return ArtifactWriteResult(
            relpath=relpath,
            artifact_hash="sha256:artifact",
            byte_count=10,
        )

    def write_jsonl_atomic(self, **_):
        raise AssertionError("not used by parse tests")

    def write_text_atomic(self, **_):
        raise AssertionError("not used by parse tests")


class _Parser:
    def __init__(self, *, error: Exception | None = None, identity_error: Exception | None = None) -> None:
        self.error = error
        self.identity_error = identity_error
        self.called = False

    def identity(self) -> ParserIdentity:
        if self.identity_error is not None:
            raise self.identity_error
        return ParserIdentity(
            name="MinerU",
            version="3.4.0",
            backend="pipeline",
            method="auto",
            language="ch",
        )

    def parse(
        self,
        *,
        input_pdf: Path,
        output_dir: Path,
        options: ParserOptions,
        document_metadata: dict[str, Any],
    ) -> ParserResult:
        self.called = True
        if self.error is not None:
            raise self.error
        artifact_root = output_dir / "sample" / "auto"
        return ParserResult(
            parser_name="MinerU",
            parser_version="3.4.0",
            parser_backend=options.backend,
            parser_method=options.method,
            parser_language=options.language,
            artifact_root=artifact_root,
            content_list_path=artifact_root / "content_list.json",
            markdown_path=artifact_root / "sample.md",
            normalized_ir={
                "contract_version": "normalized_ir.v2",
                "document_id": document_metadata["document_id"],
                "source_pdf": document_metadata["source_pdf"],
                "title": document_metadata["title"],
                "parser": {},
                "parser_artifacts": {},
                "parsed_pages": {"start_page_no": 1, "end_page_no": 1},
                "elements": [],
            },
        )


def _uow_with_document() -> FakeUnitOfWork:
    uow = FakeUnitOfWork()
    company = uow.companies.add(e.Company(company_id="co_1", legal_name="江海股份"))
    security = uow.securities.add(
        e.Security(
            security_id="sec_1",
            company_id=company.company_id,
            security_code="002484",
            exchange="SZSE",
        )
    )
    uow.documents.add(
        e.Document(
            document_id="doc_1",
            status="registered",
            company_id=company.company_id,
            security_id=security.security_id,
            provider="cninfo",
            provider_document_id="pid_1",
            title="公告",
            raw_file_relpath="raw_documents/doc.pdf",
            raw_file_hash="sha256:raw",
        )
    )
    return uow


def _use_case(
    uow: FakeUnitOfWork,
    *,
    parser: _Parser | None = None,
    raw_store: _RawStore | None = None,
    artifact_store: _ArtifactStore | None = None,
) -> tuple[ParseDocument, _ArtifactStore]:
    artifact_store = artifact_store or _ArtifactStore()
    return (
        ParseDocument(
            parser=parser or _Parser(),
            path_builder=_PathBuilder(),
            raw_store=raw_store or _RawStore(),
            artifact_store=artifact_store,
            uow_factory=lambda: uow,
            default_timeout_seconds=42,
        ),
        artifact_store,
    )


class ParseDocumentUnitTests(unittest.TestCase):
    def test_raw_missing_and_hash_mismatch_fail_before_parser(self) -> None:
        for actual_hash, expected_code in (
            (None, "raw_missing"),
            ("sha256:other", "raw_hash_mismatch"),
        ):
            with self.subTest(expected_code=expected_code):
                uow = _uow_with_document()
                parser = _Parser()
                use_case, _ = _use_case(
                    uow,
                    parser=parser,
                    raw_store=_RawStore(ok=False, actual_hash=actual_hash),
                )

                result = use_case.execute(ParseDocumentCommand(document_id="doc_1"))

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error["error_code"], expected_code)
                self.assertFalse(parser.called)

    def test_missing_metadata_rejects_before_run_creation(self) -> None:
        uow = _uow_with_document()
        document = uow.documents.get("doc_1")
        document.raw_file_hash = None
        use_case, _ = _use_case(uow)

        with self.assertRaises(ParseDocumentError):
            use_case.execute(ParseDocumentCommand(document_id="doc_1"))

        self.assertEqual(len(uow.processing_runs.all()), 0)

    def test_successive_parses_create_independent_runs(self) -> None:
        uow = _uow_with_document()
        use_case, artifact_store = _use_case(uow)

        first = use_case.execute(ParseDocumentCommand(document_id="doc_1"))
        second = use_case.execute(
            ParseDocumentCommand(
                document_id="doc_1",
                options=ParserOptions(start_page=0, end_page=0),
            )
        )

        self.assertNotEqual(first.processing_run_id, second.processing_run_id)
        self.assertEqual(len(uow.processing_runs.all()), 2)
        self.assertEqual(uow.documents.get("doc_1").status, "parsed")
        latest_payload = artifact_store.payloads[second.normalized_ir_relpath]
        self.assertFalse(latest_payload["parsed_pages"]["full_pdf"])

    def test_typed_parser_exceptions_map_to_structured_errors(self) -> None:
        cases = (
            (ParserTimeoutError("timeout"), "parse_timeout", True),
            (ParserInvocationError("invoke"), "parser_invocation_failed", True),
            (
                ParserOutputContractError("bad output"),
                "parser_output_contract_failed",
                False,
            ),
            (ParserUnknownError("unknown"), "parser_unknown_failed", False),
        )
        for exc, error_code, retryable in cases:
            with self.subTest(error_code=error_code):
                uow = _uow_with_document()
                use_case, _ = _use_case(uow, parser=_Parser(error=exc))

                result = use_case.execute(ParseDocumentCommand(document_id="doc_1"))

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error["error_code"], error_code)
                self.assertEqual(result.error["retryable"], retryable)

    def test_version_probe_failure_fails_closed_without_parse(self) -> None:
        uow = _uow_with_document()
        parser = _Parser(identity_error=ParserVersionProbeError("version failed"))
        use_case, _ = _use_case(uow, parser=parser)

        result = use_case.execute(ParseDocumentCommand(document_id="doc_1"))

        self.assertEqual(result.status, "failed")
        self.assertFalse(parser.called)
        self.assertEqual(result.error["error_code"], "parser_version_probe_failed")
        self.assertEqual(uow.documents.get("doc_1").status, "parse_failed")

    def test_unknown_exception_persists_failed_run_then_reraises(self) -> None:
        uow = _uow_with_document()
        use_case, _ = _use_case(uow, parser=_Parser(error=RuntimeError("boom")))

        with self.assertRaises(RuntimeError):
            use_case.execute(ParseDocumentCommand(document_id="doc_1"))

        run = uow.processing_runs.all()[0]
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error["error_code"], "RuntimeError")
        self.assertFalse(run.error["retryable"])

    def test_published_document_parse_failure_does_not_downgrade(self) -> None:
        uow = _uow_with_document()
        active_run = uow.processing_runs.add(
            e.ProcessingRun(
                processing_run_id="run_active",
                document_id="doc_1",
                run_kind="publish",
                status="succeeded",
                is_active=True,
            )
        )
        document = uow.documents.get("doc_1")
        document.status = "published"
        document.current_processing_run_id = active_run.processing_run_id
        use_case, _ = _use_case(
            uow,
            parser=_Parser(error=ParserInvocationError("failed reparse")),
        )

        result = use_case.execute(ParseDocumentCommand(document_id="doc_1"))

        self.assertEqual(result.status, "failed")
        self.assertEqual(document.status, "published")
        self.assertEqual(document.current_processing_run_id, "run_active")

    def test_parse_events_include_envelope_fields_and_occurred_at(self) -> None:
        uow = _uow_with_document()
        use_case, _ = _use_case(uow)

        result = use_case.execute(ParseDocumentCommand(document_id="doc_1"))

        events = uow.outbox.all()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.event_kind, "processing_run_created")
        self.assertEqual(event.change_kind, "observed")
        self.assertEqual(event.subject_kind, "processing_run")
        self.assertEqual(event.subject_ref, result.processing_run_id)
        self.assertIsNotNone(event.occurred_at)


if __name__ == "__main__":
    unittest.main()
