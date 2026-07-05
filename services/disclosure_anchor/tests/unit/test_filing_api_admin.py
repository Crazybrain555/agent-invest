from datetime import date
from pathlib import Path
from types import SimpleNamespace
import unittest

from disclosure_anchor.api.routers.admin import (
    build_document_units,
    parse_document,
    publish_run,
    register_local_pdf,
)
from disclosure_anchor.api.schemas.admin import (
    ParserOptionsRequest,
    PublishRunRequest,
    PublishRunResponse,
    RegisterLocalPdfRequest,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.use_cases.build_units import BuildUnitsResult
from disclosure_anchor.application.use_cases.parse_document import ParseDocumentResult
from disclosure_anchor.application.use_cases.register_local_pdf import RegisterLocalPdfResult


class _Deps:
    def __init__(self) -> None:
        self.register_command = None
        self.parse_document_id: str | None = None
        self.parse_options: ParserOptions | None = None
        self.build_document_id: str | None = None
        self.publish_args: tuple[str, bool, str | None] | None = None

    def register_local_pdf(self, command):
        self.register_command = command
        return RegisterLocalPdfResult(
            document_id=None,
            raw_file_relpath=None,
            raw_file_hash=None,
            source_access_id="sa_1",
            outbox_event_id=None,
            quarantined_path=Path("/private/tmp/full/leak/bad.pdf"),
            quarantine_reason="invalid_raw_document",
        )

    def parse_document(self, *, document_id: str, options: ParserOptions):
        self.parse_document_id = document_id
        self.parse_options = options
        return ParseDocumentResult(
            processing_run_id="run_1",
            status="succeeded",
            parser_artifact_relpath="parser/run_1",
            normalized_ir_relpath="normalized/run_1.json",
            artifact_hash="sha256:" + "a" * 64,
        )

    def build_units(self, *, document_id: str):
        self.build_document_id = document_id
        return BuildUnitsResult(
            processing_run_id="run_1",
            status="succeeded",
            unit_count=7,
        )

    def publish_run(self, *, processing_run_id: str, allow_empty: bool, reason: str | None):
        self.publish_args = (processing_run_id, allow_empty, reason)
        return PublishRunResponse(
            document_id="doc_1",
            processing_run_id=processing_run_id,
            is_active=True,
        )


def _request(deps: _Deps) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(admin_deps=deps)))


class FilingApiAdminTests(unittest.TestCase):
    def test_register_local_pdf_returns_quarantine_basename_only(self) -> None:
        deps = _Deps()
        response = register_local_pdf(
            _request(deps),
            RegisterLocalPdfRequest(
                file_path=Path("/service/input.pdf"),
                company_legal_name="江海股份",
                security_code="002484",
                exchange="szse",
                filing_type="other",
                title="短公告",
                announcement_date=date(2026, 7, 5),
                provider_document_id="1225376481",
                provider="cninfo",
            ),
        )

        self.assertEqual(response.quarantined_path, "bad.pdf")
        self.assertNotIn("/", response.quarantined_path)
        self.assertEqual(deps.register_command.provider, "cninfo")
        self.assertEqual(deps.register_command.security_code, "002484")

    def test_parse_uses_parser_options_defaults_and_overrides(self) -> None:
        deps = _Deps()

        response = parse_document(
            "doc_1",
            _request(deps),
            ParserOptionsRequest(method="ocr", table=False, timeout_seconds=30),
        )

        self.assertEqual(response.processing_run_id, "run_1")
        self.assertEqual(deps.parse_document_id, "doc_1")
        self.assertEqual(deps.parse_options.method, "ocr")
        self.assertEqual(deps.parse_options.backend, "pipeline")
        self.assertEqual(deps.parse_options.language, "ch")
        self.assertFalse(deps.parse_options.table)
        self.assertEqual(deps.parse_options.timeout_seconds, 30)

    def test_build_units_response_shape(self) -> None:
        deps = _Deps()

        response = build_document_units("doc_1", _request(deps))

        self.assertEqual(deps.build_document_id, "doc_1")
        self.assertEqual(response.processing_run_id, "run_1")
        self.assertEqual(response.unit_build_status, "succeeded")
        self.assertEqual(response.unit_count, 7)

    def test_publish_response_shape(self) -> None:
        deps = _Deps()

        response = publish_run(
            "run_1",
            _request(deps),
            PublishRunRequest(allow_empty=True, reason="manual"),
        )

        self.assertEqual(deps.publish_args, ("run_1", True, "manual"))
        self.assertEqual(response.document_id, "doc_1")
        self.assertEqual(response.processing_run_id, "run_1")
        self.assertTrue(response.is_active)


if __name__ == "__main__":
    unittest.main()
