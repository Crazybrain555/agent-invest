from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from disclosure_anchor.application.contracts.provider_document_admission import (
    SourcePdfObservation,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    provider_document_envelope_from_bytes,
)
from disclosure_anchor.application.ports.file_store import (
    ArtifactWriteResult,
    RawDocumentVerification,
)
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.provider_document_source import (
    ProviderDocumentSourceError,
)
from disclosure_anchor.application.ports.provider_parser import ProviderParserResult
from disclosure_anchor.application.use_cases.parse_document import (
    ParseDocument,
    ParseDocumentCommand,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import (
    ParseDocumentError,
    ParserOutputContractError,
    ParserTimeoutError,
)
from tests.unit._fakes import FakeUnitOfWork
from tests.unit.test_provider_document_admission import _provider_document


_RAW_HASH = "sha256:" + "a" * 64
_RUNTIME_HASH = "sha256:" + "b" * 64
_RAW_RELPATH = Path(
    "raw_documents/cninfo/002484/2026/pid_1/sha256_" + "a" * 64 + ".pdf"
)


def _options(**overrides: object) -> ParserOptions:
    values: dict[str, object] = {
        "backend": "hybrid-http-client",
        "effort": "medium",
        "image_analysis": False,
        "server_url": "http://127.0.0.1:30000",
        "runtime_bundle_identity_sha256": _RUNTIME_HASH,
    }
    values.update(overrides)
    return ParserOptions(**values)  # type: ignore[arg-type]


class _PathBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root

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

    def provider_document_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        artifact_owner_processing_run_id: str,
    ) -> Path:
        return (
            Path("derived/provider_documents")
            / provider
            / security_code
            / provider_document_id
            / artifact_owner_processing_run_id
            / "provider_document.v1.json"
        )


class _RawStore:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok

    def verify_raw_document(
        self, *, relpath: Path, expected_hash: str
    ) -> RawDocumentVerification:
        return RawDocumentVerification(
            relpath=relpath,
            expected_hash=expected_hash,
            actual_hash=_RAW_HASH if self.ok else None,
            ok=self.ok,
            message="ok" if self.ok else "raw missing",
        )


class _ProviderSource:
    def __init__(
        self,
        *,
        observation: SourcePdfObservation | None = None,
        error: ProviderDocumentSourceError | None = None,
    ) -> None:
        self.observation = observation or SourcePdfObservation(
            sha256=_RAW_HASH,
            page_count=1,
        )
        self.error = error

    def observe_source_pdf(self, _relpath: Path) -> SourcePdfObservation:
        if self.error is not None:
            raise self.error
        return self.observation

    def read_provider_document_record(self, _relpath: Path) -> bytes:
        raise AssertionError("parse does not read a provider record")

    def rebuild_provider_document(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("parse does not re-admit its new record")


class _ArtifactStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: dict[Path, bytes] = {}

    def write_text_atomic(
        self, *, relpath: Path, text: str
    ) -> ArtifactWriteResult:
        if self.fail:
            raise OSError("artifact volume unavailable")
        payload = text.encode("utf-8")
        self.records[relpath] = payload
        return ArtifactWriteResult(
            relpath=relpath,
            artifact_hash=_sha(payload),
            byte_count=len(payload),
        )

    def write_json_atomic(self, **_kwargs: object) -> ArtifactWriteResult:
        raise AssertionError("provider record must use canonical text bytes")

    def write_jsonl_atomic(self, **_kwargs: object) -> ArtifactWriteResult:
        raise AssertionError("not used")


class _Parser:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        readiness_error: Exception | None = None,
    ) -> None:
        self.error = error
        self.readiness_error = readiness_error
        self.called = False

    def identity(self) -> ParserIdentity:
        return ParserIdentity(name="MinerU", version="3.4.4")

    def readiness(self, _options: ParserOptions) -> None:
        if self.readiness_error is not None:
            raise self.readiness_error

    def parse(
        self,
        *,
        input_pdf: Path,
        output_dir: Path,
        options: ParserOptions,
        source_pdf_sha256: str,
    ) -> ProviderParserResult:
        del input_pdf
        self.called = True
        if self.error is not None:
            raise self.error
        target = options.target_identity(self.identity())
        leaf = output_dir / ("sha256_" + source_pdf_sha256.removeprefix("sha256:"))
        leaf = leaf / "hybrid_auto"
        leaf.mkdir(parents=True)
        return ProviderParserResult(
            target_identity=target,
            artifact_root=leaf,
            provider_document=_provider_document(),
        )


def _uow() -> FakeUnitOfWork:
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
            raw_file_relpath=str(_RAW_RELPATH),
            raw_file_hash=_RAW_HASH,
        )
    )
    return uow


class ParseDocumentTests(unittest.TestCase):
    def test_writes_one_canonical_provider_record_and_no_nir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            uow = _uow()
            store = _ArtifactStore()
            parser = _Parser()
            use_case = ParseDocument(
                parser=parser,
                provider_source=_ProviderSource(),
                path_builder=_PathBuilder(Path(tmp)),
                raw_store=_RawStore(),
                artifact_store=store,
                uow_factory=lambda: uow,
            )

            result = use_case.execute(
                ParseDocumentCommand(document_id="doc_1", options=_options())
            )

        self.assertEqual(result.status, "succeeded")
        self.assertIsNone(result.normalized_ir_relpath)
        self.assertIsNotNone(result.provider_document_relpath)
        run = uow.processing_runs.get(result.processing_run_id)
        assert run is not None and result.provider_document_relpath is not None
        self.assertIsNone(run.normalized_ir_relpath)
        self.assertEqual(run.provider_document_relpath, result.provider_document_relpath)
        record = store.records[Path(result.provider_document_relpath)]
        envelope = provider_document_envelope_from_bytes(record)
        self.assertEqual(envelope.document_id, "doc_1")
        self.assertEqual(envelope.artifact_owner_processing_run_id, run.processing_run_id)
        self.assertEqual(run.artifact_hash, _sha(record))
        self.assertTrue(parser.called)

    def test_failed_raw_verification_keeps_planned_provider_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            uow = _uow()
            parser = _Parser()
            result = ParseDocument(
                parser=parser,
                provider_source=_ProviderSource(),
                path_builder=_PathBuilder(Path(tmp)),
                raw_store=_RawStore(ok=False),
                artifact_store=_ArtifactStore(),
                uow_factory=lambda: uow,
            ).execute(ParseDocumentCommand(document_id="doc_1", options=_options()))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error["error_code"], "raw_missing")
        self.assertIsNotNone(result.provider_document_relpath)
        self.assertIsNone(result.normalized_ir_relpath)
        self.assertFalse(parser.called)

    def test_independent_source_drift_and_static_io_fail_closed(self) -> None:
        cases = (
            (
                _ProviderSource(
                    observation=SourcePdfObservation(
                        sha256="sha256:" + "0" * 64,
                        page_count=1,
                    )
                ),
                "raw_hash_mismatch",
                False,
            ),
            (
                _ProviderSource(
                    error=ProviderDocumentSourceError(
                        "source_pdf_read_failed",
                        "unsafe source path",
                        retryable=False,
                    )
                ),
                "source_pdf_read_failed",
                False,
            ),
        )
        for source, code, retryable in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                uow = _uow()
                result = ParseDocument(
                    parser=_Parser(),
                    provider_source=source,
                    path_builder=_PathBuilder(Path(tmp)),
                    raw_store=_RawStore(),
                    artifact_store=_ArtifactStore(),
                    uow_factory=lambda: uow,
                ).execute(
                    ParseDocumentCommand(document_id="doc_1", options=_options())
                )
            self.assertEqual(result.error["error_code"], code)
            self.assertEqual(result.error["retryable"], retryable)

    def test_parser_and_artifact_failures_are_classified(self) -> None:
        cases = (
            (_Parser(error=ParserTimeoutError("timeout")), _ArtifactStore(), "parse_timeout", True),
            (_Parser(), _ArtifactStore(fail=True), "OSError", True),
        )
        for parser, store, code, retryable in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                uow = _uow()
                result = ParseDocument(
                    parser=parser,
                    provider_source=_ProviderSource(),
                    path_builder=_PathBuilder(Path(tmp)),
                    raw_store=_RawStore(),
                    artifact_store=store,
                    uow_factory=lambda: uow,
                ).execute(
                    ParseDocumentCommand(document_id="doc_1", options=_options())
                )
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error["error_code"], code)
            self.assertEqual(result.error["retryable"], retryable)

    def test_invalid_writer_profile_fails_before_parser_consumes_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            uow = _uow()
            parser = _Parser(
                readiness_error=ParserOutputContractError("High is diagnostic only")
            )
            result = ParseDocument(
                parser=parser,
                provider_source=_ProviderSource(),
                path_builder=_PathBuilder(Path(tmp)),
                raw_store=_RawStore(),
                artifact_store=_ArtifactStore(),
                uow_factory=lambda: uow,
            ).execute(ParseDocumentCommand(document_id="doc_1", options=_options()))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error["error_code"], "parser_output_contract_failed")
        self.assertFalse(result.error["retryable"])
        self.assertFalse(parser.called)

    def test_missing_document_metadata_rejects_before_run_creation(self) -> None:
        uow = _uow()
        document = uow.documents.get("doc_1")
        assert document is not None
        document.raw_file_hash = None
        uow.documents.update(document)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ParseDocumentError):
                ParseDocument(
                    parser=_Parser(),
                    provider_source=_ProviderSource(),
                    path_builder=_PathBuilder(Path(tmp)),
                    raw_store=_RawStore(),
                    artifact_store=_ArtifactStore(),
                    uow_factory=lambda: uow,
                ).execute(
                    ParseDocumentCommand(document_id="doc_1", options=_options())
                )
        self.assertEqual(uow.processing_runs.items, {})


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


__all__ = ["ParseDocumentTests"]
