"""RebuildUnits use case tests (rules-only iteration, no re-parse)."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from disclosure_anchor.application.use_cases.rebuild_units import (
    RebuildUnits,
    RebuildUnitsCommand,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import BuildUnitsError

from tests.unit._fakes import FakeUnitOfWork


def _document(document_id: str = "doc_1") -> e.Document:
    return e.Document(
        document_id=document_id,
        company_id="co_1",
        security_id="sec_1",
        provider="cninfo",
        provider_document_id="pid-1",
        title="年报",
        announcement_date=datetime(2026, 4, 10, tzinfo=timezone.utc).date(),
        report_period="2025A",
        raw_file_relpath="raw/x.pdf",
        raw_file_hash="sha256:" + "a" * 64,
        source_access_id="sa_1",
        status="published",
    )


def _parse_run(document_id: str = "doc_1") -> e.ProcessingRun:
    return e.ProcessingRun(
        processing_run_id="run_parse_1",
        document_id=document_id,
        artifact_owner_processing_run_id="run_parse_1",
        run_kind="parse",
        status="succeeded",
        parser_name="MinerU",
        parser_version="3.4.4",
        parser_backend="hybrid-http-client",
        parser_method="auto",
        parser_language="ch",
        parser_target_identity={"effort": "medium"},
        input_raw_file_hash="sha256:" + "a" * 64,
        parser_artifact_relpath="parser_artifacts/x",
        artifact_hash="sha256:" + "b" * 64,
        provider_document_relpath=(
            "derived/provider_documents/cninfo/000001/pid-1/run_parse_1/"
            "provider_document.v1.json"
        ),
    )


class RebuildUnitsTests(unittest.TestCase):
    def test_rebuild_copies_parser_provenance_into_succeeded_run(self) -> None:
        uow = FakeUnitOfWork()
        uow.documents.add(_document())
        uow.processing_runs.add(_parse_run())

        result = RebuildUnits(uow_factory=lambda: uow).execute(
            RebuildUnitsCommand(document_id="doc_1")
        )
        self.assertEqual(result.source_processing_run_id, "run_parse_1")
        self.assertEqual(result.status, "succeeded")
        run = uow.processing_runs.get(result.processing_run_id)
        self.assertEqual(run.run_kind, "rebuild_units")
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(
            run.artifact_owner_processing_run_id,
            "run_parse_1",
        )
        self.assertIsNone(run.normalized_ir_relpath)
        self.assertEqual(
            run.provider_document_relpath,
            "derived/provider_documents/cninfo/000001/pid-1/run_parse_1/"
            "provider_document.v1.json",
        )
        self.assertEqual(run.parser_name, "MinerU")
        self.assertEqual(run.input_raw_file_hash, "sha256:" + "a" * 64)
        events = [
            event
            for event in uow.outbox.items.values()
            if event.event_kind == "processing_run_created"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["status"], "succeeded")

        chained = RebuildUnits(uow_factory=lambda: uow).execute(
            RebuildUnitsCommand(document_id="doc_1")
        )
        self.assertEqual(
            chained.source_processing_run_id,
            "run_parse_1",
        )
        self.assertEqual(
            uow.processing_runs.get(
                chained.processing_run_id
            ).artifact_owner_processing_run_id,
            "run_parse_1",
        )
        self.assertEqual(
            uow.processing_runs.get(chained.processing_run_id).provider_document_relpath,
            _parse_run().provider_document_relpath,
        )

        invalid_uow = FakeUnitOfWork()
        invalid_uow.documents.add(_document())
        invalid = _parse_run()
        invalid.artifact_owner_processing_run_id = "run_missing"
        invalid_uow.processing_runs.add(invalid)
        with self.assertRaises(BuildUnitsError) as invalid_ctx:
            RebuildUnits(uow_factory=lambda: invalid_uow).execute(
                RebuildUnitsCommand(document_id="doc_1")
            )
        self.assertEqual(
            invalid_ctx.exception.error["error_code"],
            "ARTIFACT_OWNER_INVALID",
        )

    def test_rebuild_without_succeeded_parse_run_is_typed_error(self) -> None:
        uow = FakeUnitOfWork()
        uow.documents.add(_document())

        with self.assertRaises(BuildUnitsError) as ctx:
            RebuildUnits(uow_factory=lambda: uow).execute(
                RebuildUnitsCommand(document_id="doc_1")
            )
        self.assertEqual(ctx.exception.error["error_code"], "NO_SUCCEEDED_PARSE_RUN")

    def test_rebuild_missing_document_is_typed_error(self) -> None:
        uow = FakeUnitOfWork()

        with self.assertRaises(BuildUnitsError) as ctx:
            RebuildUnits(uow_factory=lambda: uow).execute(
                RebuildUnitsCommand(document_id="doc_missing")
            )
        self.assertEqual(ctx.exception.error["error_code"], "DOCUMENT_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
