"""BuildUnits use case tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.parsers.mineru.table_reconciler import (
    TableReconciliationStats,
)
from disclosure_anchor.application.ports.file_store import ArtifactWriteResult
from disclosure_anchor.application.contracts.normalized_ir import (
    CURRENT_NORMALIZED_IR_VERSION,
    normalized_ir_filename,
)
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
        "contract_version": CURRENT_NORMALIZED_IR_VERSION,
        "document_id": "doc_1",
        "created_at": "2026-07-05T00:00:00Z",
        "source_pdf": "raw.pdf",
        "title": "公告",
        "parser": {
            "name": "MinerU",
            "package_version": "3.4.0",
            "backend": "pipeline",
            "method": "auto",
            "language": "ch",
            "formula": False,
            "table": True,
        },
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


def _uow(
    root: Path, *, contract_version: str = CURRENT_NORMALIZED_IR_VERSION
) -> tuple[FakeUnitOfWork, Path]:
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
    ir_relpath = Path(
        "derived/normalized_ir/cninfo/002484/pid_1/run_1"
    ) / (
        normalized_ir_filename(contract_version)
        if contract_version in {"normalized_ir.v2", "normalized_ir.v3"}
        else f"{contract_version}.json"
    )
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
    def test_matching_ir_artifact_hash_builds_normally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, ir_relpath = _uow(root)
            run = uow.processing_runs.get("run_1")
            run.artifact_hash = (
                "sha256:" + hashlib.sha256((root / ir_relpath).read_bytes()).hexdigest()
            )
            uow.processing_runs.update(run)
            paths = _PathBuilder(root)
            use_case = BuildUnits(
                path_builder=paths,
                artifact_store=ArtifactStore(paths),
                uow_factory=lambda: uow,
            )

            result = use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "succeeded")

    def test_ir_hash_mismatch_fails_structured_before_building(self) -> None:
        # The IR sits in the overwritable derived area; a corrupted or
        # overwritten IR must fail loudly instead of publishing
        # self-consistent bad units (round23).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, ir_relpath = _uow(root)
            run = uow.processing_runs.get("run_1")
            run.artifact_hash = "sha256:" + "0" * 64
            uow.processing_runs.update(run)
            paths = _PathBuilder(root)
            use_case = BuildUnits(
                path_builder=paths,
                artifact_store=ArtifactStore(paths),
                uow_factory=lambda: uow,
            )

            result = use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error["error_code"], "IR_HASH_MISMATCH")
            self.assertEqual(
                uow.processing_runs.get("run_1").unit_build_error["error_code"],
                "IR_HASH_MISMATCH",
            )
            self.assertEqual(uow.document_units.list_by_processing_run("run_1"), [])

    def test_locator_diagnostics_have_no_builder_rules_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, ir_relpath = _uow(root)
            ir_path = root / ir_relpath
            payload = json.loads(ir_path.read_text(encoding="utf-8"))
            reconciliation = TableReconciliationStats(
                model_status="absent", content_tables=0
            ).as_dict()
            self.assertNotIn("table_builder_semantics_version", reconciliation)
            payload["parser_diagnostics"] = {
                "table_reconciliation": reconciliation
            }
            ir_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            paths = _PathBuilder(root)
            result = BuildUnits(
                path_builder=paths,
                artifact_store=ArtifactStore(paths),
                uow_factory=lambda: uow,
            ).execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "succeeded")

    def test_reconciled_ir_requires_complete_consistent_diagnostics(self) -> None:
        cases = {
            "not_an_object": [],
            "missing_counter": {
                key: value
                for key, value in TableReconciliationStats(
                    model_status="absent", content_tables=0
                ).as_dict().items()
                if key != "located_tables"
            },
            "unexpected_hash_without_model": {
                **TableReconciliationStats(
                    model_status="absent", content_tables=0
                ).as_dict(),
                "model_hash": 7,
            },
            "impossible_supported_formula": {
                **TableReconciliationStats(
                    model_status="supported",
                    content_tables=2,
                    model_hash="sha256:" + "a" * 64,
                    model_tables=2,
                    uniquely_matched_tables=2,
                    candidate_groups=1,
                    proven_groups=1,
                    locator_only_groups=1,
                    locator_only_tables=2,
                    located_groups=1,
                    located_tables=2,
                ).as_dict(),
                "restored_groups": 1,
            },
        }
        for label, reconciliation in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                uow, ir_relpath = _uow(root)
                ir_path = root / ir_relpath
                payload = json.loads(ir_path.read_text(encoding="utf-8"))
                payload["parser_diagnostics"] = {
                    "table_reconciliation": reconciliation
                }
                ir_path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                use_case = BuildUnits(
                    path_builder=_PathBuilder(root),
                    artifact_store=ArtifactStore(_PathBuilder(root)),
                    uow_factory=lambda: uow,
                )

                result = use_case.execute(
                    BuildUnitsCommand(processing_run_id="run_1")
                )

                self.assertEqual(result.status, "failed")
                self.assertEqual(
                    result.error["error_code"],
                    "IR_TABLE_RECONCILIATION_INVALID",
                )
                self.assertEqual(
                    uow.document_units.list_by_processing_run("run_1"), []
                )

    def test_legacy_reconciliation_without_restoration_remains_buildable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, ir_relpath = _uow(root, contract_version="normalized_ir.v2")
            ir_path = root / ir_relpath
            payload = json.loads(ir_path.read_text(encoding="utf-8"))
            # Exact shape emitted by the previous restore.v3 generation.  It
            # intentionally lacks v4 locator-only counters; algorithm
            # classification must happen before current-shape validation.
            reconciliation = {
                "algorithm_version": "mineru-aggregate-table-restore.v3",
                "table_builder_semantics_version": "table-builder-semantics.v2",
                "model_status": "absent",
                "model_hash": None,
                "content_tables": 0,
                "model_tables": 0,
                "uniquely_matched_tables": 0,
                "ambiguous_matches": 0,
                "candidate_groups": 0,
                "proven_groups": 0,
                "unproven_groups": 0,
                "restoration_rejected_groups": 0,
                "unresolved_groups": 0,
                "located_groups": 0,
                "located_tables": 0,
                "restored_groups": 0,
                "restored_tables": 0,
            }
            payload["parser_diagnostics"] = {
                "table_reconciliation": reconciliation
            }
            ir_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            use_case = BuildUnits(
                path_builder=_PathBuilder(root),
                artifact_store=ArtifactStore(_PathBuilder(root)),
                uow_factory=lambda: uow,
            )

            result = use_case.execute(
                BuildUnitsCommand(processing_run_id="run_1")
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.error, None)
            self.assertEqual(len(uow.document_units.list_by_processing_run("run_1")), 1)

    def test_aggregate_locator_cannot_bypass_missing_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, ir_relpath = _uow(root)
            ir_path = root / ir_relpath
            payload = json.loads(ir_path.read_text(encoding="utf-8"))
            payload["elements"][0].update(
                {
                    "kind": "table",
                    "raw_kind": "table",
                    "page_no": 1,
                    "table_html": "<table><tr><td>A</td></tr></table>",
                    "page_span": [1, 2],
                    "page_bboxes": [
                        {"page_no": 1, "bbox": [0, 0, 10, 10]},
                        {"page_no": 2, "bbox": [0, 0, 10, 10]},
                    ],
                    "model_table_indices": [0, 1],
                    "continuation_source_item_indices": [2],
                    "table_locator_algorithm": (
                        "mineru-aggregate-table-locator.v4"
                    ),
                }
            )
            ir_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            paths = _PathBuilder(root)
            result = BuildUnits(
                path_builder=paths,
                artifact_store=ArtifactStore(paths),
                uow_factory=lambda: uow,
            ).execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "failed")
            self.assertEqual(
                result.error["error_code"], "IR_TABLE_RECONCILIATION_INVALID"
            )
            self.assertEqual(uow.document_units.list_by_processing_run("run_1"), [])

    def test_valid_locator_only_ir_builds_one_logical_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, ir_relpath = _uow(root)
            ir_path = root / ir_relpath
            payload = json.loads(ir_path.read_text(encoding="utf-8"))
            algorithm = "mineru-aggregate-table-locator.v4"
            payload["elements"] = [
                {
                    "ir_id": "ir_0000",
                    "kind": "table",
                    "raw_kind": "table",
                    "order_index": 0,
                    "source_item_index": 0,
                    "page_no": 1,
                    "bbox": [0, 0, 10, 10],
                    "table_html": "<table><tr><td>A</td></tr><tr><td>B</td></tr></table>",
                    "table": {"headers": ["A"], "rows": [["B"]]},
                    "table_caption": [],
                    "table_footnote": [],
                    "page_span": [1, 2],
                    "page_bboxes": [
                        {"page_no": 1, "bbox": [0, 0, 10, 10]},
                        {"page_no": 2, "bbox": [0, 0, 10, 10]},
                    ],
                    "model_table_indices": [0, 1],
                    "continuation_source_item_indices": [1],
                    "table_locator_algorithm": algorithm,
                },
                {
                    "ir_id": "ir_0001",
                    "kind": "table",
                    "raw_kind": "table",
                    "order_index": 1,
                    "source_item_index": 1,
                    "page_no": 2,
                    "bbox": [0, 0, 10, 10],
                    "table_html": "",
                    "table": {"headers": [], "rows": []},
                    "table_caption": [],
                    "table_footnote": [],
                },
            ]
            payload["parser_diagnostics"] = {
                "table_reconciliation": TableReconciliationStats(
                    model_status="supported",
                    content_tables=2,
                    model_hash="sha256:" + "d" * 64,
                    model_tables=2,
                    uniquely_matched_tables=2,
                    candidate_groups=1,
                    proven_groups=1,
                    locator_only_groups=1,
                    locator_only_tables=2,
                    located_groups=1,
                    located_tables=2,
                ).as_dict()
            }
            ir_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            paths = _PathBuilder(root)
            result = BuildUnits(
                path_builder=paths,
                artifact_store=ArtifactStore(paths),
                uow_factory=lambda: uow,
            ).execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.unit_count, 1)
            unit = uow.document_units.list_by_processing_run("run_1")[0]
            self.assertEqual(unit.payload_kind, "table")
            self.assertEqual(unit.artifact_locator["page_span"], [1, 2])
            self.assertEqual(len(unit.artifact_locator["page_bboxes"]), 2)

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
            self.assertEqual(run.builder_rules_version, "ub-2026.07-62")
            self.assertEqual(run.unit_build_attempt_count, 1)
            self.assertTrue(
                run.document_units_relpath.endswith("document_units.v1.jsonl")
            )
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

    def test_build_hashes_image_bytes_even_when_source_name_looks_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_bytes = b"mineru identifier is not a content hash"
            misleading = "a" * 64
            uow, ir_relpath = _uow(root)
            image_path = root / f"parser/a/images/{misleading}.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(image_bytes)
            normalized_ir = _image_ir()
            normalized_ir["elements"][0]["image_path"] = (
                f"images/{misleading}.png"
            )
            (root / ir_relpath).write_text(
                json.dumps(normalized_ir, ensure_ascii=False),
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
            actual = hashlib.sha256(image_bytes).hexdigest()
            self.assertEqual(units[0].payload["image_ref"], f"images/{actual}.png")

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

    def test_contract_version_must_match_artifact_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, ir_relpath = _uow(root)
            wrong_relpath = ir_relpath.with_name("normalized_ir.v2.json")
            (root / ir_relpath).rename(root / wrong_relpath)
            run = uow.processing_runs.get("run_1")
            run.normalized_ir_relpath = str(wrong_relpath)
            uow.processing_runs.update(run)

            result = BuildUnits(
                path_builder=_PathBuilder(root),
                artifact_store=ArtifactStore(_PathBuilder(root)),
                uow_factory=lambda: uow,
            ).execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error["error_code"], "IR_CONTRACT_UNSUPPORTED")
            self.assertEqual(
                result.error["reason_code"], "contract_filename_mismatch"
            )

    def test_current_contract_rejects_retired_native_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, ir_relpath = _uow(root)
            path = root / ir_relpath
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["native_text"] = {"status": "empty"}
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            result = BuildUnits(
                path_builder=_PathBuilder(root),
                artifact_store=ArtifactStore(_PathBuilder(root)),
                uow_factory=lambda: uow,
            ).execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error["error_code"], "IR_CONTRACT_UNSUPPORTED")
            self.assertEqual(result.error["reason_code"], "v3_native_text_forbidden")

    def test_v2_cannot_claim_locator_v4_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, ir_relpath = _uow(root, contract_version="normalized_ir.v2")
            path = root / ir_relpath
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["parser_diagnostics"] = {
                "table_reconciliation": TableReconciliationStats(
                    model_status="absent", content_tables=0
                ).as_dict()
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            result = BuildUnits(
                path_builder=_PathBuilder(root),
                artifact_store=ArtifactStore(_PathBuilder(root)),
                uow_factory=lambda: uow,
            ).execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "failed")
            self.assertEqual(
                result.error["error_code"],
                "IR_TABLE_RECONCILIATION_CONTRACT_MISMATCH",
            )
            self.assertEqual(result.error["reason_code"], "v2_locator_v4_forbidden")

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
            self.assertEqual(
                uow.processing_runs.get("run_1").unit_build_attempt_count, 1
            )
            self.assertEqual(uow.document_units.list_by_processing_run("run_1"), [])


if __name__ == "__main__":
    unittest.main()
