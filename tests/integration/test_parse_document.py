"""Phase 04 parse-document integration tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.application.ports.parser import ParserOptions, ParserResult
from disclosure_anchor.application.use_cases.parse_document import (
    ParseDocument,
    ParseDocumentCommand,
)
from disclosure_anchor.application.use_cases.register_local_pdf import (
    RegisterLocalPdf,
    RegisterLocalPdfCommand,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import ParserError
from disclosure_anchor.settings import Settings
from tests.integration._support import engine_or_skip


def _settings(root: Path) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        mineru_model_cache=shared_root / "model_cache" / "mineru",
        hf_home=shared_root / "model_cache" / "huggingface",
        modelscope_cache=shared_root / "model_cache" / "modelscope",
    )


class FakeParser:
    def parse(
        self,
        *,
        input_pdf: Path,
        output_dir: Path,
        options: ParserOptions,
        document_metadata: dict[str, Any],
    ) -> ParserResult:
        nested = output_dir / "sample" / "auto"
        nested.mkdir(parents=True)
        content_list = nested / "sample_content_list.json"
        content_list.write_text(
            json.dumps([{"type": "text", "text": "真实解析烟测", "page_idx": 0}]),
            encoding="utf-8",
        )
        markdown = nested / "sample.md"
        markdown.write_text("真实解析烟测", encoding="utf-8")
        return ParserResult(
            parser_name="MinerU",
            parser_version="3.4.0",
            parser_backend=options.backend,
            parser_method=options.method,
            artifact_root=nested,
            content_list_path=content_list,
            markdown_path=markdown,
            normalized_ir={
                "contract_version": "normalized_ir.v1",
                "created_at": "2026-06-29T00:00:00+00:00",
                "document_id": document_metadata["document_id"],
                "source_pdf": document_metadata["source_pdf"],
                "title": document_metadata["title"],
                "parser": {
                    "name": "MinerU",
                    "package_version": "3.4.0",
                    "backend": options.backend,
                    "method": options.method,
                    "language": options.language,
                    "formula": options.formula,
                    "table": options.table,
                },
                "parser_artifacts": {},
                "parsed_pages": {"start_page_no": 1, "end_page_no": 1, "full_pdf": True},
                "elements": [
                    {
                        "ir_id": "fake_ir_0000",
                        "kind": "text",
                        "order_index": 0,
                        "source_item_index": 0,
                        "page_idx": 0,
                        "page_no": 1,
                        "text": "真实解析烟测",
                    }
                ],
            },
        )


class FailingParser:
    def parse(self, **_: Any) -> ParserResult:
        raise ParserError("parser failed for test")


class TrackingParser(FakeParser):
    def __init__(self) -> None:
        self.called = False

    def parse(self, **kwargs: Any) -> ParserResult:
        self.called = True
        return super().parse(**kwargs)


class ParseDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.settings = _settings(self.root)
        self.paths = FileStorePathBuilder(self.settings)
        self.raw_store = RawDocumentStore(self.paths)
        self.artifact_store = ArtifactStore(self.paths)
        self.provider_document_ids: list[str] = []

    def tearDown(self) -> None:
        for provider_document_id in self.provider_document_ids:
            self._delete_by_provider_document_id(provider_document_id)
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _delete_by_provider_document_id(self, provider_document_id: str) -> None:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT document_id, source_access_id, company_id, security_id "
                    "FROM disclosure_core.document "
                    "WHERE provider = 'local' AND provider_document_id = :pid"
                ),
                {"pid": provider_document_id},
            ).all()
            document_ids = [row[0] for row in rows]
            source_access_ids = [row[1] for row in rows if row[1]]
            security_ids = [row[3] for row in rows if row[3]]
            company_ids = [row[2] for row in rows if row[2]]
            for document_id in document_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_ops.outbox_event WHERE document_id = :id"
                    ),
                    {"id": document_id},
                )
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.document_unit WHERE document_id = :id"
                    ),
                    {"id": document_id},
                )
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.processing_run WHERE document_id = :id"
                    ),
                    {"id": document_id},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.document WHERE document_id = :id"),
                    {"id": document_id},
                )
            for source_access_id in source_access_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.source_access "
                        "WHERE source_access_id = :id"
                    ),
                    {"id": source_access_id},
                )
            for security_id in security_ids:
                conn.execute(
                    text("DELETE FROM disclosure_core.security WHERE security_id = :id"),
                    {"id": security_id},
                )
            for company_id in company_ids:
                conn.execute(
                    text("DELETE FROM disclosure_core.company WHERE company_id = :id"),
                    {"id": company_id},
                )

    def _register_document(self) -> str:
        provider_document_id = "phase04-" + ids.new_ulid()
        self.provider_document_ids.append(provider_document_id)
        pdf = self.root / "realistic.pdf"
        pdf.write_bytes(b"%PDF-1.4\nrealistic phase04 pdf\n%%EOF\n")
        use_case = RegisterLocalPdf(
            raw_store=self.raw_store,
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=self.engine),
        )
        result = use_case.execute(
            RegisterLocalPdfCommand(
                file_path=pdf,
                company_legal_name=f"Phase04 Test Co {provider_document_id}",
                security_code="P04" + provider_document_id[-4:],
                exchange="LOCAL",
                filing_type="annual_report",
                title="Phase 04 local PDF",
                announcement_date=date(2026, 6, 29),
                report_period="2025A",
                provider_document_id=provider_document_id,
            )
        )
        return result.document_id

    def test_parse_document_writes_processing_run_and_normalized_ir(self) -> None:
        document_id = self._register_document()
        use_case = ParseDocument(
            parser=FakeParser(),
            path_builder=self.paths,
            raw_store=self.raw_store,
            artifact_store=self.artifact_store,
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=self.engine),
        )

        result = use_case.execute(ParseDocumentCommand(document_id=document_id))

        self.assertEqual(result.status, "succeeded")
        self.assertTrue(result.parser_artifact_relpath.startswith("parser_artifacts/local/"))
        self.assertTrue(result.normalized_ir_relpath.startswith("derived/normalized_ir/local/"))
        normalized_path = self.settings.disclosure_data_root / "data" / result.normalized_ir_relpath
        self.assertTrue(normalized_path.is_file())
        normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
        self.assertEqual(normalized["parser_artifacts"]["artifact_root_relpath"], result.parser_artifact_relpath)
        self.assertFalse(Path(normalized["parser_artifacts"]["content_list_relpath"]).is_absolute())

        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT status, parser_name, parser_version, parser_backend, "
                    "input_raw_file_hash, parser_artifact_relpath, normalized_ir_relpath, "
                    "artifact_hash, is_active "
                    "FROM disclosure_core.processing_run WHERE processing_run_id = :id"
                ),
                {"id": result.processing_run_id},
            ).one()
        self.assertEqual(row.status, "succeeded")
        self.assertEqual(row.parser_name, "MinerU")
        self.assertEqual(row.parser_version, "3.4.0")
        self.assertEqual(row.parser_backend, "pipeline")
        self.assertTrue(row.input_raw_file_hash.startswith("sha256:"))
        self.assertEqual(row.parser_artifact_relpath, result.parser_artifact_relpath)
        self.assertEqual(row.normalized_ir_relpath, result.normalized_ir_relpath)
        self.assertTrue(row.artifact_hash.startswith("sha256:"))
        self.assertFalse(row.is_active)

    def test_parser_failure_records_failed_run_without_disturbing_active_run(self) -> None:
        document_id = self._register_document()
        active_run_id = ids.new_processing_run_id()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            uow.processing_runs.add(
                e.ProcessingRun(
                    processing_run_id=active_run_id,
                    document_id=document_id,
                    run_kind="publish",
                    status="succeeded",
                    is_active=True,
                )
            )
            uow.commit()

        use_case = ParseDocument(
            parser=FailingParser(),
            path_builder=self.paths,
            raw_store=self.raw_store,
            artifact_store=self.artifact_store,
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=self.engine),
        )
        result = use_case.execute(ParseDocumentCommand(document_id=document_id))

        self.assertEqual(result.status, "failed")
        self.assertIsNotNone(result.error)
        self.assertIn("parser failed", result.error["message"])
        with self.engine.connect() as conn:
            active_status = conn.execute(
                text(
                    "SELECT status, is_active FROM disclosure_core.processing_run "
                    "WHERE processing_run_id = :id"
                ),
                {"id": active_run_id},
            ).one()
            failed_status = conn.execute(
                text(
                    "SELECT status, is_active FROM disclosure_core.processing_run "
                    "WHERE processing_run_id = :id"
                ),
                {"id": result.processing_run_id},
            ).one()
        self.assertEqual(active_status.status, "succeeded")
        self.assertTrue(active_status.is_active)
        self.assertEqual(failed_status.status, "failed")
        self.assertFalse(failed_status.is_active)

    def test_raw_hash_mismatch_fails_before_parser_is_called(self) -> None:
        document_id = self._register_document()
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            document = uow.documents.get(document_id)
            self.assertIsNotNone(document)

        assert document is not None
        raw_path = self.settings.disclosure_data_root / "data" / document.raw_file_relpath
        raw_path.write_bytes(b"%PDF-1.4\ntampered raw bytes\n%%EOF\n")
        parser = TrackingParser()
        use_case = ParseDocument(
            parser=parser,
            path_builder=self.paths,
            raw_store=self.raw_store,
            artifact_store=self.artifact_store,
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=self.engine),
        )

        result = use_case.execute(ParseDocumentCommand(document_id=document_id))

        self.assertEqual(result.status, "failed")
        self.assertFalse(parser.called)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error["error_code"], "raw_hash_mismatch")


if __name__ == "__main__":
    unittest.main()
