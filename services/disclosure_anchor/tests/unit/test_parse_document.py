import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from typing import Any

from disclosure_anchor.application.ports.file_store import (
    ArtifactWriteResult,
    RawDocumentVerification,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    CURRENT_NORMALIZED_IR_VERSION,
    normalized_ir_filename,
)
from disclosure_anchor.application.contracts.document_structure import (
    DOCUMENT_STRUCTURE_ALGORITHM,
    DOCUMENT_STRUCTURE_VERSION,
    carrier_set_sha256,
)
from disclosure_anchor.application.contracts.visual_semantics import (
    MINERU_VL_UTILS_PACKAGE_VERSION,
    VisualSemanticClosure,
    parser_target_sha256,
    visual_semantic_bytes,
    visual_semantic_diagnostics,
)
from disclosure_anchor.application.ports.parser import (
    ParserIdentity,
    ParserOptions,
    ParserResult,
)
from disclosure_anchor.application.use_cases.parse_document import (
    ParseDocument,
    ParseDocumentCommand,
    build_parser_artifact_manifest,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import (
    ParseDocumentError,
    ParserBackendOverloadedError,
    ParserCancelledError,
    ParserInvocationError,
    ParserLocalInvocationError,
    ParserOutputContractError,
    ParserTaskDeadlineError,
    ParserTaskError,
    ParserTimeoutError,
    ParserUnknownError,
    ParserVersionProbeError,
    RemoteModelAmbiguousError,
    StructureNativeEvidenceRequiredError,
)
from tests.unit._fakes import FakeUnitOfWork


_RAW_HASH = "sha256:" + "a" * 64
_RUNTIME_BUNDLE_HASH = "sha256:" + "b" * 64


def _command(
    *,
    options: ParserOptions | None = None,
) -> ParseDocumentCommand:
    return ParseDocumentCommand(
        document_id="doc_1",
        options=options
        or ParserOptions(runtime_bundle_identity_sha256=_RUNTIME_BUNDLE_HASH),
    )


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
            / normalized_ir_filename()
        )


class _RawStore:
    def __init__(self, *, ok: bool = True, actual_hash: str | None = _RAW_HASH) -> None:
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


class _UnavailableArtifactStore(_ArtifactStore):
    def write_json_atomic(self, *, relpath: Path, payload: object):
        del relpath, payload
        raise OSError("artifact volume unavailable")


class _Parser:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        identity_error: Exception | None = None,
        readiness_error: Exception | None = None,
        contract_version: str = CURRENT_NORMALIZED_IR_VERSION,
        remote_models: tuple[object, ...] = (),
    ) -> None:
        self.error = error
        self.identity_error = identity_error
        self.readiness_error = readiness_error
        self.contract_version = contract_version
        self.called = False
        self.document_metadata: dict[str, Any] | None = None
        # Scripted resolution results for successive resolve_remote_model
        # calls: a string is the served model, an exception is raised.
        self.remote_models = list(remote_models)

    def resolve_remote_model(self, options: ParserOptions) -> str | None:
        if not options.backend.endswith("-http-client"):
            return None
        assert self.remote_models, "unexpected resolve_remote_model call"
        item = self.remote_models.pop(0)
        if isinstance(item, Exception):
            raise item
        assert item is None or isinstance(item, str)
        return item

    def identity(self) -> ParserIdentity:
        if self.identity_error is not None:
            raise self.identity_error
        return ParserIdentity(
            name="MinerU",
            version="3.4.0",
        )

    def readiness(self, _options: ParserOptions | None = None) -> None:
        if self.readiness_error is not None:
            raise self.readiness_error

    def parse(
        self,
        *,
        input_pdf: Path,
        output_dir: Path,
        options: ParserOptions,
        document_metadata: dict[str, Any],
    ) -> ParserResult:
        self.called = True
        self.document_metadata = document_metadata
        if self.error is not None:
            raise self.error
        artifact_root = output_dir / "sample" / "auto"
        artifact_root.mkdir(parents=True)
        content_list = artifact_root / "sample_content_list.json"
        content_list.write_text("[]", encoding="utf-8")
        content_list_v2 = artifact_root / "sample_content_list_v2.json"
        content_list_v2.write_text("[[]]", encoding="utf-8")
        markdown = artifact_root / "sample.md"
        markdown.write_text("sample", encoding="utf-8")
        model = artifact_root / "sample_model.json"
        model.write_text("[]", encoding="utf-8")
        middle = artifact_root / "sample_middle.json"
        middle.write_text("{}", encoding="utf-8")
        pdf_structure = artifact_root / "sample_pdf_structure.json"
        pdf_structure.write_text(
            json.dumps(
                {
                    "source_pdf_sha256": document_metadata["raw_file_hash"],
                    "source_pdf_page_count": 1,
                }
            ),
            encoding="utf-8",
        )
        source_evidence = artifact_root / "sample_source_evidence.json"
        source_evidence.write_text("{}", encoding="utf-8")
        elements: list[dict[str, Any]] = []
        target = options.target_identity(self.identity())
        visual_closure = VisualSemanticClosure(
            source_pdf_sha256=document_metadata["raw_file_hash"],
            source_pdf_page_count=1,
            source_evidence_sha256=_hash_bytes(b"{}"),
            content_list_sha256=_hash_bytes(b"[]"),
            content_list_v2_sha256=_hash_bytes(b"[[]]"),
            middle_sha256=_hash_bytes(b"{}"),
            model_sha256=_hash_bytes(b"[]"),
            parser_target_sha256=parser_target_sha256(target.to_payload()),
            runtime_bundle_identity_sha256=_RUNTIME_BUNDLE_HASH,
            mineru_package_version="3.4.0",
            mineru_vl_utils_version=MINERU_VL_UTILS_PACKAGE_VERSION,
            enrichment_backend="http-client",
            enrichment_image_analysis=True,
            server_url_sha256=_hash_bytes(b"fixture-server"),
            formula_enabled=True,
            dispositions=(),
        )
        visual_semantics = artifact_root / "sample_visual_semantics.json"
        visual_semantics.write_bytes(visual_semantic_bytes(visual_closure))
        normalized_ir: dict[str, Any] = {
            "contract_version": self.contract_version,
            "created_at": "2026-07-16T00:00:00Z",
            "document_id": document_metadata["document_id"],
            "source_pdf": document_metadata["source_pdf"],
            "source_pdf_sha256": document_metadata["raw_file_hash"],
            "source_pdf_page_count": 1,
            "title": document_metadata["title"],
            "parser": target.to_payload(),
            "parser_artifacts": {},
            # Current write contract: parsed_pages carries exactly these three keys,
            # with full_pdf derived from whether a page window was requested.
            "parsed_pages": {
                "start_page_no": 1,
                "end_page_no": 1,
                "full_pdf": options.start_page is None and options.end_page is None,
            },
            "elements": elements,
            "parser_diagnostics": {
                "table_reconciliation": {
                    "algorithm_version": "mineru-page-local-table-closure.v6",
                    "model_hash": ("sha256:" + hashlib.sha256(b"[]").hexdigest()),
                    "content_tables": 0,
                    "model_tables": 0,
                    "matched_tables": 0,
                    "page_local_closed": True,
                },
                "visual_semantics": visual_semantic_diagnostics(visual_closure),
            },
            "structure_proof": {
                "contract_version": DOCUMENT_STRUCTURE_VERSION,
                "algorithm_version": DOCUMENT_STRUCTURE_ALGORITHM,
                "source_pdf_sha256": document_metadata["raw_file_hash"],
                "source_pdf_page_count": 1,
                "carrier_set_sha256": carrier_set_sha256(elements),
                "native": {
                    "status": "untagged",
                    "artifact_role": "pdf_structure",
                },
                "headings": [],
                "owner_scope_breaks": [],
                "page_frames": [],
                "conflicts": [],
                "coverage": {
                    "heading_nodes": 0,
                    "page_frame_groups": 0,
                },
            },
        }
        return ParserResult(
            target_identity=target,
            artifact_root=artifact_root,
            artifact_paths={
                "content_list": content_list,
                "content_list_v2": content_list_v2,
                "markdown": markdown,
                "middle": middle,
                "model": model,
                "pdf_structure": pdf_structure,
                "source_evidence": source_evidence,
                "visual_semantics": visual_semantics,
            },
            normalized_ir=normalized_ir,
        )


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


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
            raw_file_hash=_RAW_HASH,
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
    def test_artifact_manifest_hashes_roles_and_rejects_root_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifact_root"
            root.mkdir()
            content = root / "sample_content_list.json"
            content_bytes = b"[]"
            content.write_bytes(content_bytes)
            manifest = build_parser_artifact_manifest(
                artifact_root=root,
                artifact_root_relpath=Path("parser/run/auto"),
                artifact_paths={
                    "content_list": content,
                    "middle": None,
                },
            )
            self.assertEqual(
                manifest["files"]["content_list"],
                {
                    "availability": "present",
                    "relpath": "parser/run/auto/sample_content_list.json",
                    "sha256": "sha256:" + hashlib.sha256(content_bytes).hexdigest(),
                    "size_bytes": len(content_bytes),
                },
            )
            self.assertEqual(
                manifest["files"]["middle"],
                {"availability": "not_emitted"},
            )
            outside = Path(tmp) / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "escapes artifact root",
            ):
                build_parser_artifact_manifest(
                    artifact_root=root,
                    artifact_root_relpath=Path("parser/run/auto"),
                    artifact_paths={"content_list": outside},
                )

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

                result = use_case.execute(_command())

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error["error_code"], expected_code)
                self.assertFalse(parser.called)

    def test_missing_metadata_rejects_before_run_creation(self) -> None:
        uow = _uow_with_document()
        document = uow.documents.get("doc_1")
        document.raw_file_hash = None
        use_case, _ = _use_case(uow)

        with self.assertRaises(ParseDocumentError):
            use_case.execute(_command())

        self.assertEqual(len(uow.processing_runs.all()), 0)

    def test_successive_parses_create_independent_runs(self) -> None:
        uow = _uow_with_document()
        use_case, artifact_store = _use_case(uow)

        first = use_case.execute(_command())
        second = use_case.execute(
            _command(
                options=ParserOptions(
                    start_page=0,
                    end_page=0,
                    runtime_bundle_identity_sha256=_RUNTIME_BUNDLE_HASH,
                )
            )
        )

        self.assertNotEqual(first.processing_run_id, second.processing_run_id)
        self.assertEqual(len(uow.processing_runs.all()), 2)
        self.assertEqual(uow.documents.get("doc_1").status, "parsed")
        latest_payload = artifact_store.payloads[second.normalized_ir_relpath]
        self.assertFalse(latest_payload["parsed_pages"]["full_pdf"])
        self.assertEqual(
            latest_payload["parser_artifacts"]["files"]["model"]["availability"],
            "present",
        )
        self.assertRegex(
            latest_payload["parser_artifacts"]["files"]["model"]["sha256"],
            r"^sha256:[a-f0-9]{64}$",
        )

    def test_typed_parser_exceptions_map_to_structured_errors(self) -> None:
        cases = (
            (ParserTimeoutError("timeout"), "parse_timeout", True),
            (
                ParserTaskDeadlineError("task deadline"),
                "parser_task_deadline_exceeded",
                True,
            ),
            (ParserTaskError("task failed"), "parser_task_failed", True),
            (
                ParserCancelledError("worker stopped"),
                "parser_cancelled",
                True,
            ),
            (
                ParserLocalInvocationError("spawn failed"),
                "parser_local_invocation_failed",
                True,
            ),
            (
                ParserBackendOverloadedError("capacity rejected"),
                "parser_backend_overloaded",
                True,
            ),
            (ParserInvocationError("invoke"), "parser_invocation_failed", True),
            (
                ParserOutputContractError("bad output"),
                "parser_output_contract_failed",
                False,
            ),
            (
                StructureNativeEvidenceRequiredError("native lane missing"),
                "structure_native_evidence_required",
                False,
            ),
            (ParserUnknownError("unknown"), "parser_unknown_failed", False),
        )
        for exc, error_code, retryable in cases:
            with self.subTest(error_code=error_code):
                uow = _uow_with_document()
                use_case, _ = _use_case(uow, parser=_Parser(error=exc))

                result = use_case.execute(_command())

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error["error_code"], error_code)
                self.assertEqual(result.error["retryable"], retryable)

        uow = _uow_with_document()
        parser = _Parser(readiness_error=ParserVersionProbeError("remote unavailable"))
        use_case, _ = _use_case(uow, parser=parser)

        result = use_case.execute(_command())

        self.assertEqual(result.status, "failed")
        self.assertFalse(parser.called)
        self.assertEqual(result.error["error_code"], "parser_readiness_failed")
        self.assertTrue(result.error["retryable"])

    def test_http_backend_stamps_the_resolved_model_into_the_run(self) -> None:
        uow = _uow_with_document()
        parser = _Parser(remote_models=("MinerU2.5-Pro-2605-1.2B",) * 2)
        use_case, _ = _use_case(uow, parser=parser)

        result = use_case.execute(
            _command(
                options=ParserOptions(
                    backend="vlm-http-client",
                    server_url="http://gpu.example:30000",
                    runtime_bundle_identity_sha256=_RUNTIME_BUNDLE_HASH,
                )
            )
        )

        self.assertEqual(result.status, "succeeded")
        run = uow.processing_runs.get(result.processing_run_id)
        assert run is not None
        target = run.parser_target_identity
        assert isinstance(target, dict)
        self.assertEqual(target["target_contract_version"], "parser-target.v2")
        self.assertEqual(
            target["remote_model_name"], "MinerU2.5-Pro-2605-1.2B"
        )
        self.assertEqual(target["remote_selection_mode"], "explicit")
        self.assertEqual(parser.remote_models, [])

    def test_unresolved_remote_model_fails_before_the_parser_runs(self) -> None:
        for scripted, error_code, retryable in (
            (
                RemoteModelAmbiguousError("two models served"),
                "remote_model_ambiguous",
                False,
            ),
            (
                ParserVersionProbeError("model listing unavailable"),
                "remote_model_unresolved",
                True,
            ),
        ):
            with self.subTest(error_code=error_code):
                uow = _uow_with_document()
                parser = _Parser(remote_models=(scripted,))
                use_case, artifact_store = _use_case(uow, parser=parser)

                result = use_case.execute(
                    _command(
                        options=ParserOptions(
                            backend="vlm-http-client",
                            server_url="http://gpu.example:30000",
                            runtime_bundle_identity_sha256=(
                                _RUNTIME_BUNDLE_HASH
                            ),
                        )
                    )
                )

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error["error_code"], error_code)
                self.assertEqual(result.error["retryable"], retryable)
                self.assertFalse(parser.called)
                self.assertEqual(artifact_store.payloads, {})

    def test_remote_model_change_mid_run_is_a_typed_terminal(self) -> None:
        uow = _uow_with_document()
        parser = _Parser(
            remote_models=("MinerU2.5-Pro-2605-1.2B", "other-model")
        )
        use_case, artifact_store = _use_case(uow, parser=parser)

        result = use_case.execute(
            _command(
                options=ParserOptions(
                    backend="vlm-http-client",
                    server_url="http://gpu.example:30000",
                    runtime_bundle_identity_sha256=_RUNTIME_BUNDLE_HASH,
                )
            )
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error["error_code"], "remote_model_changed")
        self.assertFalse(result.error["retryable"])
        self.assertTrue(parser.called)
        self.assertEqual(artifact_store.payloads, {})

    def test_missing_native_evidence_is_typed_and_writes_no_ir(self) -> None:
        # The native lane being absent is an operational state, not a broken
        # provider artifact: it must persist as its own terminal and leave
        # no NormalizedIR behind.
        uow = _uow_with_document()
        use_case, artifact_store = _use_case(
            uow,
            parser=_Parser(
                error=StructureNativeEvidenceRequiredError("native lane missing")
            ),
        )

        result = use_case.execute(_command())

        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.error["error_code"],
            "structure_native_evidence_required",
        )
        self.assertFalse(result.error["retryable"])
        self.assertEqual(artifact_store.payloads, {})

    def test_new_parse_rejects_legacy_ir_generation(self) -> None:
        uow = _uow_with_document()
        use_case, artifact_store = _use_case(
            uow, parser=_Parser(contract_version="normalized_ir.v3")
        )

        result = use_case.execute(_command())

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error["error_code"], "parser_output_contract_failed")
        self.assertEqual(artifact_store.payloads, {})

    def test_version_probe_failure_fails_closed_without_parse(self) -> None:
        uow = _uow_with_document()
        parser = _Parser(identity_error=ParserVersionProbeError("version failed"))
        use_case, _ = _use_case(uow, parser=parser)

        result = use_case.execute(_command())

        self.assertEqual(result.status, "failed")
        self.assertFalse(parser.called)
        self.assertEqual(result.error["error_code"], "parser_version_probe_failed")
        self.assertTrue(result.error["retryable"])
        self.assertEqual(uow.documents.get("doc_1").status, "parse_failed")

    def test_unknown_exception_persists_failed_run_then_reraises(self) -> None:
        uow = _uow_with_document()
        use_case, _ = _use_case(uow, parser=_Parser(error=RuntimeError("boom")))

        with self.assertRaises(RuntimeError):
            use_case.execute(_command())

        run = uow.processing_runs.all()[0]
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error["error_code"], "RuntimeError")
        self.assertFalse(run.error["retryable"])

    def test_artifact_io_failure_is_retryable_shared_infrastructure(self) -> None:
        uow = _uow_with_document()
        use_case, _ = _use_case(
            uow,
            artifact_store=_UnavailableArtifactStore(),
        )

        result = use_case.execute(_command())

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error["stage"], "parse_io")
        self.assertEqual(result.error["error_code"], "OSError")
        self.assertTrue(result.error["retryable"])

    def test_published_document_parse_failure_does_not_downgrade(self) -> None:
        uow = _uow_with_document()
        active_run = uow.processing_runs.add(
            e.ProcessingRun(
                processing_run_id="run_active",
                document_id="doc_1",
                artifact_owner_processing_run_id="run_active",
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

        result = use_case.execute(_command())

        self.assertEqual(result.status, "failed")
        self.assertEqual(document.status, "published")
        self.assertEqual(document.current_processing_run_id, "run_active")

    def test_parse_events_include_envelope_fields_and_occurred_at(self) -> None:
        uow = _uow_with_document()
        use_case, _ = _use_case(uow)

        result = use_case.execute(_command())

        run = uow.processing_runs.get(result.processing_run_id)
        self.assertEqual(
            run.artifact_owner_processing_run_id,
            result.processing_run_id,
        )
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
