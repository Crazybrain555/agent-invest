"""BuildUnits use case tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.application.ports.file_store import ArtifactWriteResult
from disclosure_anchor.application.use_cases.build_units import (
    BuildUnits,
    BuildUnitsCommand,
    SNAPSHOT_KEYS,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import BuildUnitsError
from tests.unit._fakes import FakeUnitOfWork


class _PathBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root

    def data_path(self, relpath: Path) -> Path:
        return self.root / relpath

    def document_units_snapshot_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        return (
            Path("derived/document_unit_snapshots")
            / provider
            / security_code
            / provider_document_id
            / processing_run_id
            / "document_units.v1.jsonl"
        )


class _BadHashArtifactStore(ArtifactStore):
    def write_jsonl_atomic(self, *, relpath: Path, rows: list[object]):
        super().write_jsonl_atomic(relpath=relpath, rows=rows)
        return ArtifactWriteResult(
            relpath=relpath,
            artifact_hash="sha256:" + "0" * 64,
            byte_count=1,
        )


def _normalized_ir() -> dict:
    return {
        "contract_version": "normalized_ir.v2",
        "document_id": "doc_1",
        "created_at": "2026-07-05T00:00:00Z",
        "source_pdf": "raw.pdf",
        "title": "公告",
        "parser": {},
        "parser_artifacts": {
            "artifact_root_relpath": "parser/a",
            "content_list_relpath": "parser/a/content.json",
        },
        "parsed_pages": {"start_page_no": 1, "end_page_no": 1, "full_pdf": True},
        "elements": [
            {
                "ir_id": "ir_1",
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 1,
                "source_item_index": 1,
                "heading_level": 1,
                "text": "重要提示",
            },
            {
                "ir_id": "ir_2",
                "kind": "text",
                "raw_kind": "text",
                "order_index": 2,
                "source_item_index": 2,
                "text": "公司存在退市风险，请投资者注意。",
            },
        ],
    }


def _image_ir() -> dict:
    return {
        **_normalized_ir(),
        "elements": [
            {
                "ir_id": "ir_image",
                "kind": "image",
                "raw_kind": "image",
                "order_index": 1,
                "source_item_index": 1,
                "caption": "股权结构图",
                "image_path": "images/plot.png",
            }
        ],
    }


def _uow(root: Path, *, contract_version: str = "normalized_ir.v2") -> tuple[FakeUnitOfWork, Path]:
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
    document = uow.documents.add(
        e.Document(
            document_id="doc_1",
            status="parsed",
            company_id=company.company_id,
            security_id=security.security_id,
            provider="cninfo",
            provider_document_id="pid_1",
            title="公告",
        )
    )
    ir_relpath = Path("derived/normalized_ir/cninfo/002484/pid_1/run_1/normalized_ir.v2.json")
    payload = {**_normalized_ir(), "contract_version": contract_version}
    path = root / ir_relpath
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    uow.processing_runs.add(
        e.ProcessingRun(
            processing_run_id="run_1",
            document_id=document.document_id,
            run_kind="parse",
            status="succeeded",
            normalized_ir_relpath=str(ir_relpath),
        )
    )
    return uow, ir_relpath


class BuildUnitsTests(unittest.TestCase):
    def test_unknown_preparation_failure_preserves_structured_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _uow(root)
            paths = _PathBuilder(root)
            use_case = BuildUnits(
                path_builder=paths,
                artifact_store=ArtifactStore(paths),
                uow_factory=lambda: uow,
            )
            with (
                mock.patch(
                    "disclosure_anchor.application.use_cases.build_units.build_unit_drafts_s1_s7",
                    side_effect=RuntimeError("builder regression"),
                ),
                self.assertRaises(BuildUnitsError) as caught,
            ):
                use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))

        self.assertEqual(
            caught.exception.error["error_code"], "BUILD_PREPARATION_FAILED"
        )
        self.assertEqual(
            uow.processing_runs.get("run_1").unit_build_error["error_code"],
            "BUILD_PREPARATION_FAILED",
        )

    def test_build_writes_snapshot_stats_and_document_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _uow(root)
            paths = _PathBuilder(root)
            use_case = BuildUnits(
                path_builder=paths,
                artifact_store=ArtifactStore(paths),
                uow_factory=lambda: uow,
            )

            result = use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.unit_count, 1)
            run = uow.processing_runs.get("run_1")
            self.assertEqual(run.builder_rules_version, "ub-2026.07-18")
            self.assertEqual(run.unit_build_attempt_count, 1)
            self.assertTrue(run.document_units_relpath.endswith("document_units.v1.jsonl"))
            units = uow.document_units.list_by_processing_run("run_1")
            self.assertEqual(len(units), 1)
            snapshot_path = paths.data_path(Path(run.document_units_relpath))
            rows = [
                json.loads(line)
                for line in snapshot_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(set(rows[0]), SNAPSHOT_KEYS)
            self.assertNotIn("structure_hash", rows[0])
            self.assertNotIn("query_projection_hash", rows[0])
            self.assertEqual(rows[0]["payload"], units[0].payload)
            stats_path = snapshot_path.parent / "build_stats.v1.json"
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            self.assertEqual(stats["generated_by_kind"]["text"], 1)

    def test_build_resolves_non_hash_image_from_parser_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_bytes = b"not already content addressed"
            uow, ir_relpath = _uow(root)
            image_path = root / "parser/a/images/plot.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(image_bytes)
            (root / ir_relpath).write_text(
                json.dumps(_image_ir(), ensure_ascii=False),
                encoding="utf-8",
            )
            paths = _PathBuilder(root)
            use_case = BuildUnits(
                path_builder=paths,
                artifact_store=ArtifactStore(paths),
                uow_factory=lambda: uow,
            )

            result = use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "succeeded")
            units = uow.document_units.list_by_processing_run("run_1")
            self.assertEqual(len(units), 1)
            digest = hashlib.sha256(image_bytes).hexdigest()
            self.assertEqual(units[0].payload["image_ref"], f"images/{digest}.png")
            self.assertEqual(units[0].quality_status, "needs_review")

    def test_old_ir_contract_is_dead_lettered_and_counts_an_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _uow(root, contract_version="normalized_ir.v1")
            paths = _PathBuilder(root)
            use_case = BuildUnits(
                path_builder=paths,
                artifact_store=ArtifactStore(paths),
                uow_factory=lambda: uow,
            )

            result = use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error["error_code"], "IR_CONTRACT_TOO_OLD")
            run = uow.processing_runs.get("run_1")
            self.assertEqual(run.unit_build_attempt_count, 1)

    def test_rejects_already_built_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _uow(root)
            uow.document_units.add(
                e.DocumentUnit(
                    asset_id=ids.new_asset_id(),
                    document_id="doc_1",
                    processing_run_id="run_1",
                    payload_kind="text",
                    order_index=1,
                    payload={"text": "old"},
                    content_hash="sha256:old",
                )
            )
            use_case = BuildUnits(
                path_builder=_PathBuilder(root),
                artifact_store=ArtifactStore(_PathBuilder(root)),
                uow_factory=lambda: uow,
            )

            with self.assertRaises(BuildUnitsError) as ctx:
                use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(ctx.exception.error["error_code"], "UNITS_ALREADY_BUILT")

    def test_snapshot_verification_failure_marks_run_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _uow(root)
            paths = _PathBuilder(root)
            use_case = BuildUnits(
                path_builder=paths,
                artifact_store=_BadHashArtifactStore(paths),
                uow_factory=lambda: uow,
            )

            result = use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error["error_code"], "ARTIFACT_WRITE_FAILED")
            self.assertEqual(uow.processing_runs.get("run_1").unit_build_attempt_count, 1)
            self.assertEqual(uow.document_units.list_by_processing_run("run_1"), [])


if __name__ == "__main__":
    unittest.main()
