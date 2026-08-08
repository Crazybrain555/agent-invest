"""BuildUnits boundary tests for source-bound NormalizedIR v4."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest import mock

from scripts import audit_unit_corpus

from disclosure_anchor.adapters.parsers.mineru.source_evidence_validator import (
    MinerUSourceEvidenceValidator,
)
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.application.contracts import (
    canonical_occurrence,
    source_evidence_projection,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    search_text_values,
)
from disclosure_anchor.application.ports.file_store import ArtifactWriteResult
from disclosure_anchor.application.services import (
    document_unit_audit as document_unit_audit_module,
    unit_preparation,
)
from disclosure_anchor.application.services.document_unit_audit import (
    AuditFinding,
    DocumentAuditReport,
)
from disclosure_anchor.application.services.unit_builder import (
    source_native_fallback,
)
from disclosure_anchor.application.services.unit_builder.builder import (
    SourceEvidenceClosureError,
)
from disclosure_anchor.application.use_cases import build_units as build_units_module
from disclosure_anchor.application.use_cases.build_units import (
    SNAPSHOT_KEYS,
    BuildUnits,
    BuildUnitsCommand,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import BuildUnitsError
from tests.unit._current_ir import write_text_ir_bundle
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
    def write_jsonl_atomic(
        self,
        *,
        relpath: Path,
        rows: list[object],
    ) -> ArtifactWriteResult:
        super().write_jsonl_atomic(relpath=relpath, rows=rows)
        return ArtifactWriteResult(
            relpath=relpath,
            artifact_hash="sha256:" + "0" * 64,
            byte_count=1,
        )


def _setup(
    root: Path,
    *,
    native_only_texts: tuple[str, ...] = (),
    native_page_texts: tuple[str, ...] | None = None,
    full_pdf: bool = True,
    page_visual: bool = False,
    image: bool = False,
    class_filing_type: str | None = "other",
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
            class_filing_type=class_filing_type,
            class_rules_version=(
                "test-materialized-v1" if class_filing_type is not None else None
            ),
        )
    )
    ir_relpath = Path(
        "derived/normalized_ir/cninfo/002484/pid_1/run_1/normalized_ir.v4.json"
    )
    normalized_ir = write_text_ir_bundle(
        root,
        ir_relpath,
        native_only_texts=native_only_texts,
        native_page_texts=native_page_texts,
        full_pdf=full_pdf,
        page_visual=page_visual,
        image=image,
    )
    run = e.ProcessingRun(
        processing_run_id="run_1",
        document_id=document.document_id,
        artifact_owner_processing_run_id="run_1",
        run_kind="parse",
        status="succeeded",
        parser_target_identity=normalized_ir["parser"],
        normalized_ir_relpath=str(ir_relpath),
        artifact_hash=_sha256((root / ir_relpath).read_bytes()),
    )
    uow.processing_runs.add(run)
    return uow, ir_relpath


def _use_case(
    root: Path,
    uow: FakeUnitOfWork,
    *,
    artifact_store: ArtifactStore | None = None,
) -> BuildUnits:
    paths = _PathBuilder(root)
    return BuildUnits(
        path_builder=paths,
        artifact_store=artifact_store or ArtifactStore(paths),
        uow_factory=lambda: uow,
        source_evidence_validator=MinerUSourceEvidenceValidator(),
    )


def _rewrite_ir(
    root: Path,
    uow: FakeUnitOfWork,
    ir_relpath: Path,
    payload: dict[str, object],
) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    (root / ir_relpath).write_bytes(encoded)
    run = uow.processing_runs.get("run_1")
    run.artifact_hash = _sha256(encoded)
    uow.processing_runs.update(run)


def _run_shared_preparation_paths(
    service_root: Path,
    *,
    source_gap: bool,
) -> tuple[object, dict[str, object], object, object, FakeUnitOfWork]:
    data_root = service_root / "data"
    uow, ir_relpath = _setup(
        data_root,
        native_only_texts=(("MinerU遗漏但PDF可见",) if source_gap else ()),
        page_visual=source_gap,
        image=True,
    )
    run = uow.processing_runs.get("run_1")
    replay_ir = json.loads((data_root / ir_relpath).read_bytes())
    replay_bundle = audit_unit_corpus._load_persisted_source_bundle(
        replay_ir,
        data_root=service_root,
    )
    document = uow.documents.get("doc_1")
    assert document is not None
    filing_type = document.class_filing_type
    assert filing_type is not None
    entry = audit_unit_corpus.ManifestEntry(
        document_id="doc_1",
        provider="cninfo",
        provider_document_id="pid_1",
        processing_run_id="run_1",
        security_code="002484",
        security_name=None,
        company_name=None,
        title="公告",
        filing_type=filing_type,
        normalized_ir_relpath=str(ir_relpath),
        normalized_ir_sha256=run.artifact_hash,
    )
    production: list[object] = []
    corpus: list[object] = []
    prepare = unit_preparation.prepare_and_audit_units

    def capture(target: list[object]):
        def wrapped(**kwargs: object) -> object:
            result = prepare(**kwargs)
            target.append(result)
            return result

        return wrapped

    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(
                build_units_module,
                "prepare_and_audit_units",
                side_effect=capture(production),
            )
        )
        publication = _use_case(data_root, uow).execute(
            BuildUnitsCommand(processing_run_id="run_1")
        )
        stack.enter_context(
            mock.patch.object(
                audit_unit_corpus,
                "prepare_and_audit_units",
                side_effect=capture(corpus),
            )
        )
        stack.enter_context(
            mock.patch.object(
                audit_unit_corpus,
                "_replay_source_ir",
                return_value=(
                    replay_ir,
                    {"transient_source_evidence_rebuilt": True},
                    replay_bundle.ledger,
                    replay_bundle.proof,
                    replay_bundle.native_structure_index,
                ),
            )
        )
        replay = audit_unit_corpus._audit_one((entry, str(service_root), True))
    return publication, replay, production[0], corpus[0], uow


def _prepared_bytes(value: object) -> bytes:
    drafts, stats, report = value
    return json.dumps(
        {
            "drafts": [asdict(draft) for draft in drafts],
            "stats": stats.as_dict(),
            "report": report.as_dict(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


class BuildUnitsTests(unittest.TestCase):
    def test_valid_v4_build_writes_audited_snapshot_and_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _setup(root)

            result = _use_case(root, uow).execute(
                BuildUnitsCommand(processing_run_id="run_1")
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.unit_count, 1)
            rows = uow.document_units.list_by_processing_run("run_1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].title, "重要提示")
            self.assertEqual(rows[0].heading_path, ["重要提示"])
            self.assertIn(
                "公司存在退市风险",
                json.dumps(rows[0].payload, ensure_ascii=False),
            )
            snapshot = root / str(result.document_units_relpath)
            records = [
                json.loads(line)
                for line in snapshot.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(set(records[0]), SNAPSHOT_KEYS)
            self.assertEqual(
                result.content_hash_aggregate,
                uow.processing_runs.get("run_1").content_hash_aggregate,
            )

    def test_build_consumes_only_materialized_filing_classification(self) -> None:
        structures: list[list[tuple[object, ...]]] = []
        for materialized in ("investor_relations", None):
            with (
                self.subTest(materialized=materialized),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                uow, _ = _setup(
                    root,
                    class_filing_type=materialized,
                )
                document = uow.documents.get("doc_1")
                assert document is not None
                document.title = "2025年年度报告"
                document.provider_metadata = {"raw_category": "010301"}
                uow.documents.update(document)
                with mock.patch.object(
                    build_units_module,
                    "prepare_and_audit_units",
                    wraps=unit_preparation.prepare_and_audit_units,
                ) as prepare:
                    result = _use_case(root, uow).execute(
                        BuildUnitsCommand(processing_run_id="run_1")
                    )

                self.assertEqual(result.status, "succeeded")
                self.assertEqual(
                    prepare.call_args.kwargs["filing_type"],
                    materialized,
                )
                self.assertEqual(
                    prepare.call_args.kwargs["metadata"].filing_type,
                    materialized,
                )
                structures.append(
                    [
                        (
                            unit.order_index,
                            unit.payload_kind,
                            unit.title,
                            tuple(unit.heading_path),
                            unit.payload,
                            unit.applicability,
                            unit.page_no,
                            unit.artifact_locator,
                        )
                        for unit in uow.document_units.list_by_processing_run("run_1")
                    ]
                )
        self.assertEqual(structures[0], structures[1])

    def test_ir_hash_mismatch_fails_before_any_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _setup(root)
            run = uow.processing_runs.get("run_1")
            run.artifact_hash = "sha256:" + "0" * 64
            uow.processing_runs.update(run)

            result = _use_case(root, uow).execute(
                BuildUnitsCommand(processing_run_id="run_1")
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error["error_code"], "IR_HASH_MISMATCH")
            self.assertEqual(
                uow.document_units.list_by_processing_run("run_1"),
                [],
            )

    def test_partial_pdf_is_never_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _setup(root, full_pdf=False)

            result = _use_case(root, uow).execute(
                BuildUnitsCommand(processing_run_id="run_1")
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(
                result.error["error_code"],
                "PARTIAL_PDF_NOT_PUBLISHABLE",
            )

    def test_source_artifact_pair_is_rechecked_before_publication(self) -> None:
        for case in (
            "source_evidence_hash_mismatch",
            "typed_artifact_missing",
            "typed_ledger_hash_mismatch",
            "typed_projection_mismatch",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                uow, ir_relpath = _setup(root)
                if case == "source_evidence_hash_mismatch":
                    (root / "parser/a/source_evidence.json").write_text(
                        "{}",
                        encoding="utf-8",
                    )
                    expected_code = "PARSER_ARTIFACT_IDENTITY_MISMATCH"
                    expected_reason = None
                elif case == "typed_artifact_missing":
                    (root / "parser/a/content_list_v2.json").unlink()
                    expected_code = "PARSER_ARTIFACT_MISSING"
                    expected_reason = None
                else:
                    ir_path = root / ir_relpath
                    payload = json.loads(ir_path.read_text(encoding="utf-8"))
                    typed_path = root / "parser/a/content_list_v2.json"
                    typed = json.loads(typed_path.read_text(encoding="utf-8"))
                    if case == "typed_projection_mismatch":
                        typed[0][0]["content"]["title_content"][0]["content"] += "篡改"
                        expected_reason = "mineru_text_projection_invalid"
                    else:
                        expected_reason = "mineru_typed_artifact_identity_mismatch"
                    typed_bytes = json.dumps(
                        typed,
                        ensure_ascii=False,
                        indent=(2 if case == "typed_ledger_hash_mismatch" else None),
                        separators=(
                            None if case == "typed_ledger_hash_mismatch" else (",", ":")
                        ),
                        sort_keys=True,
                    ).encode()
                    typed_path.write_bytes(typed_bytes)
                    descriptor = payload["parser_artifacts"]["files"]["content_list_v2"]
                    descriptor["sha256"] = _sha256(typed_bytes)
                    descriptor["size_bytes"] = len(typed_bytes)
                    _rewrite_ir(root, uow, ir_relpath, payload)
                    expected_code = "SOURCE_EVIDENCE_INVALID"

                result = _use_case(root, uow).execute(
                    BuildUnitsCommand(processing_run_id="run_1")
                )

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error["error_code"], expected_code)
                if expected_reason is not None:
                    self.assertEqual(result.error["reason_code"], expected_reason)
                self.assertEqual(
                    uow.document_units.list_by_processing_run("run_1"),
                    [],
                )

    def test_publication_and_corpus_replay_share_byte_exact_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            publication, replay, production, corpus, uow = (
                _run_shared_preparation_paths(
                    Path(tmp),
                    source_gap=False,
                )
            )

            self.assertEqual(publication.status, "succeeded")
            self.assertTrue(replay["ok"])
            self.assertEqual(_prepared_bytes(production), _prepared_bytes(corpus))
            rows = uow.document_units.list_by_processing_run("run_1")
            self.assertEqual(len(rows), 1)
            image = next(
                part
                for row in rows
                for part in row.payload.get("parts", [])
                if part["kind"] == "image"
            )
            self.assertRegex(
                image["image_ref"],
                r"^images/[0-9a-f]{64}\.png$",
            )

    def test_source_gap_is_atomically_projected_in_both_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            publication, replay, production, corpus, uow = (
                _run_shared_preparation_paths(
                    Path(tmp),
                    source_gap=True,
                )
            )

            self.assertEqual(publication.status, "succeeded")
            self.assertTrue(replay["ok"])
            self.assertEqual(_prepared_bytes(production), _prepared_bytes(corpus))
            drafts, _stats, report = production
            self.assertTrue(report.ok, [item.as_dict() for item in report.findings])
            self.assertEqual(len(drafts), 1)
            owner = drafts[0]
            self.assertEqual(owner.payload_kind, "mixed")
            self.assertEqual(owner.heading_path, ["重要提示"])
            self.assertEqual(owner.title, "重要提示")
            self.assertEqual(owner.payload["semantic_type"], "section")
            parts = [
                part
                for part in owner.payload["parts"]
                if (
                    part.get("artifact_locator", {})
                    .get("source_projection", {})
                    .get("physical_context")
                    is not None
                )
            ]
            self.assertEqual(
                [part["text"] for part in parts],
                ["MinerU遗漏但PDF可见", "不可定位字符"],
            )
            self.assertEqual(
                [
                    part["artifact_locator"]["source_projection"]["payload"]["sources"][
                        0
                    ]["source"]["kind"]
                    for part in parts
                ],
                [
                    "source_evidence_atom",
                    "source_evidence_geometry_issue",
                ],
            )
            self.assertTrue(
                all(
                    part["artifact_locator"]["source_projection"]["search_targets"]
                    == ["payload.text"]
                    for part in parts
                )
            )
            self.assertEqual(
                search_text_values(
                    payload_kind=owner.payload_kind,
                    payload=owner.payload,
                    artifact_locator=owner.artifact_locator,
                ),
                (
                    "公司存在退市风险，请投资者注意。",
                    "MinerU遗漏但PDF可见",
                    "不可定位字符",
                    "模型生成的来源图片描述",
                ),
            )
            rows = uow.document_units.list_by_processing_run("run_1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].page_no, 1)
            run = uow.processing_runs.get("run_1")
            gate_path = (
                Path(tmp)
                / "data"
                / Path(str(run.document_units_relpath)).parent
                / "publication_gate.v1.json"
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            self.assertEqual(gate["decision"], "publish")
            self.assertEqual(gate["processing_run_id"], "run_1")
            self.assertEqual(gate["document_id"], "doc_1")
            self.assertTrue(all(gate["checks"].values()))

    def test_missing_one_native_occurrence_fails_the_shared_final_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _setup(
                root,
                native_only_texts=("MinerU遗漏但PDF可见",),
                page_visual=True,
            )
            native_drafts = unit_preparation.native_stream_unit_drafts

            def drop_first_native_part(
                *args: Any,
                **kwargs: Any,
            ) -> object:
                output = native_drafts(*args, **kwargs)
                supplemental = output[-1]
                parts = list(supplemental.payload["parts"])
                return [
                    *output[:-1],
                    replace(
                        supplemental,
                        payload={
                            **supplemental.payload,
                            "parts": parts[1:],
                        },
                    ),
                ]

            with mock.patch.object(
                unit_preparation,
                "native_stream_unit_drafts",
                side_effect=drop_first_native_part,
            ):
                result = _use_case(root, uow).execute(
                    BuildUnitsCommand(processing_run_id="run_1")
                )

            self.assertEqual(result.status, "failed")
            self.assertEqual(
                result.error["error_code"],
                "UNIT_SOURCE_AUDIT_FAILED",
            )
            self.assertEqual(
                uow.document_units.list_by_processing_run("run_1"),
                [],
            )

    def test_native_geometry_quality_cannot_be_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _setup(
                root,
                native_only_texts=("MinerU遗漏但PDF可见",),
                page_visual=True,
            )
            native_drafts = unit_preparation.native_stream_unit_drafts

            def downgrade_native_geometry(
                *args: Any,
                **kwargs: Any,
            ) -> object:
                output = native_drafts(*args, **kwargs)
                supplemental = output[-1]
                payload = copy.deepcopy(supplemental.payload)
                for part in payload["parts"]:
                    source = part["artifact_locator"]["source_projection"]["payload"][
                        "sources"
                    ][0]["source"]
                    if source["kind"] == "source_evidence_geometry_issue":
                        part["quality_status"] = "ok"
                return [
                    *output[:-1],
                    replace(
                        supplemental,
                        payload=payload,
                        quality_status="ok",
                    ),
                ]

            with mock.patch.object(
                unit_preparation,
                "native_stream_unit_drafts",
                side_effect=downgrade_native_geometry,
            ):
                result = _use_case(root, uow).execute(
                    BuildUnitsCommand(processing_run_id="run_1")
                )

            self.assertEqual(result.status, "failed")
            self.assertEqual(
                result.error["error_code"],
                "UNIT_SOURCE_AUDIT_FAILED",
            )
            self.assertEqual(
                uow.document_units.list_by_processing_run("run_1"),
                [],
            )

    def test_native_gap_projection_mutations_fail_the_shared_final_audit(
        self,
    ) -> None:
        cases = (
            "title",
            "heading",
            "taxonomy",
            "applicability",
            "physical_context",
            "anchor_heading_path",
            "part_order",
            "part_source_ref",
            "drop_part",
            "duplicate_part",
            "outer_physical_context",
            "ordinary_child_context",
            "root_taxonomy",
            "wrong_owner",
            "unit_order",
        )
        two_gap_cases = {"root_taxonomy", "wrong_owner", "unit_order"}
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if case in two_gap_cases:
                    uow, _ = _setup(
                        root,
                        native_page_texts=(
                            "重要提示",
                            "物理缺口甲",
                            "公司存在退市风险，请投资者注意。",
                            "物理缺口乙",
                        ),
                    )
                else:
                    uow, _ = _setup(
                        root,
                        native_only_texts=("MinerU遗漏但PDF可见",),
                        page_visual=True,
                    )
                bind_visuals = unit_preparation.bind_visual_page_evidence

                def forge_final_projection(
                    drafts: Any,
                    proof: Any,
                ) -> Any:
                    # Mutate the final public carrier/leaf shape. Patching the
                    # retired intermediate gap envelope only tests that the
                    # materializer discards unowned fields.
                    output = [
                        replace(
                            unit,
                            payload=copy.deepcopy(unit.payload),
                            artifact_locator=copy.deepcopy(unit.artifact_locator),
                        )
                        for unit in bind_visuals(drafts, proof)
                    ]
                    if case == "unit_order":
                        return list(reversed(output))
                    if case == "root_taxonomy":
                        root_index = next(
                            index
                            for index, unit in enumerate(output)
                            if unit.heading_path == []
                        )
                        output[root_index] = replace(
                            output[root_index],
                            semantic_key="shareholder_structure",
                            semantic_keys=["shareholder_structure"],
                        )
                        return output
                    native_positions: list[tuple[int, int]] = []
                    ordinary_positions: list[tuple[int, int]] = []
                    for unit_index, unit in enumerate(output):
                        for part_index, part in enumerate(
                            unit.payload.get("parts", [])
                        ):
                            graph = (
                                part.get("artifact_locator", {})
                                .get("source_projection", {})
                            )
                            target = (
                                native_positions
                                if graph.get("physical_context") is not None
                                else ordinary_positions
                            )
                            target.append((unit_index, part_index))
                    self.assertTrue(native_positions)
                    unit_index, part_index = native_positions[0]
                    owner = output[unit_index]
                    part = owner.payload["parts"][part_index]
                    child_graph = part["artifact_locator"]["source_projection"]
                    if case == "wrong_owner":
                        root_index = next(
                            index
                            for index, unit in enumerate(output)
                            if unit.heading_path == []
                        )
                        section_index = next(
                            index
                            for index, unit in enumerate(output)
                            if unit.heading_path
                        )
                        moved = output[root_index].payload["parts"].pop(0)
                        output[section_index].payload["parts"].insert(1, moved)
                        del output[root_index]
                        return output
                    if case == "title":
                        part["title"] = "公告"
                    elif case == "heading":
                        part["heading_path"] = ["伪章节"]
                    elif case == "taxonomy":
                        part["semantic_key"] = "shareholder_structure"
                    elif case == "applicability":
                        part["applicability"] = "applicable"
                    elif case == "physical_context":
                        child_graph["physical_context"]["word_order_span"][1] += 1
                    elif case == "anchor_heading_path":
                        child_graph["physical_context"]["anchor_heading_path"] = [
                            "伪章节"
                        ]
                    elif case == "part_order":
                        part["order"] += 1
                    elif case == "part_source_ref":
                        child_graph["payload"]["sources"][0]["source"][
                            "atom_index"
                        ] += 1
                    elif case == "drop_part":
                        owner.payload["parts"].pop(part_index)
                    elif case == "duplicate_part":
                        owner.payload["parts"].insert(
                            part_index + 1, copy.deepcopy(part)
                        )
                    elif case == "outer_physical_context":
                        owner.artifact_locator["source_projection"][
                            "physical_context"
                        ] = copy.deepcopy(child_graph["physical_context"])
                    elif case == "ordinary_child_context":
                        ordinary_unit, ordinary_part = ordinary_positions[0]
                        ordinary_graph = output[ordinary_unit].payload["parts"][
                            ordinary_part
                        ]["artifact_locator"]["source_projection"]
                        ordinary_graph["physical_context"] = copy.deepcopy(
                            child_graph["physical_context"]
                        )
                    return output

                with mock.patch.object(
                    unit_preparation,
                    "bind_visual_page_evidence",
                    side_effect=forge_final_projection,
                ):
                    result = _use_case(root, uow).execute(
                        BuildUnitsCommand(processing_run_id="run_1")
                    )

                self.assertEqual(result.status, "failed")
                self.assertEqual(
                    result.error["error_code"],
                    "UNIT_SOURCE_AUDIT_FAILED",
                )
                expected_code = {
                    "anchor_heading_path": (
                        "source_native_gap_membership_invalid"
                    ),
                    "wrong_owner": "source_native_owner_invalid",
                    "root_taxonomy": "source_native_root_projection_invalid",
                    "unit_order": "source_native_linearization_invalid",
                }.get(case)
                if expected_code is not None:
                    self.assertIn(expected_code, result.error["message"])
                self.assertEqual(
                    uow.document_units.list_by_processing_run("run_1"),
                    [],
                )

    def test_image_projection_contract_cannot_be_forged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _setup(root, image=True)
            # The forged image part belongs to a MinerU carrier, so the last
            # pass that still sees every draft is the visual binder.
            bind_visuals = unit_preparation.bind_visual_page_evidence

            def forge_image_projection(
                *args: Any,
                **kwargs: Any,
            ) -> object:
                output = bind_visuals(*args, **kwargs)
                mutated: list[object] = []
                forged = False
                for draft in output:
                    payload = copy.deepcopy(draft.payload)
                    for part in payload.get("parts", []):
                        if part.get("kind") != "image" or forged:
                            continue
                        projection = part["artifact_locator"]["source_projection"][
                            "payload"
                        ]
                        projection["target_field"] = "payload.caption"
                        projection["transform"] = "forged.v999"
                        forged = True
                    mutated.append(
                        replace(draft, payload=payload)
                        if payload != draft.payload
                        else draft
                    )
                self.assertTrue(forged)
                return mutated

            with mock.patch.object(
                unit_preparation,
                "bind_visual_page_evidence",
                side_effect=forge_image_projection,
            ):
                result = _use_case(root, uow).execute(
                    BuildUnitsCommand(processing_run_id="run_1")
                )

            self.assertEqual(result.status, "failed")
            self.assertEqual(
                result.error["error_code"],
                "UNIT_SOURCE_AUDIT_FAILED",
            )
            self.assertEqual(
                uow.document_units.list_by_processing_run("run_1"),
                [],
            )

    def test_source_replay_audit_blocks_snapshot_and_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _setup(root)
            report = DocumentAuditReport(
                document_id="doc_1",
                metrics={"error_count": 1},
                findings=(
                    AuditFinding(
                        code="source_atom_uncovered",
                        severity="error",
                        message="missing",
                        source_ref="ir_1",
                    ),
                ),
            )

            with mock.patch(
                "disclosure_anchor.application.services.unit_preparation.audit_document",
                return_value=report,
            ):
                result = _use_case(root, uow).execute(
                    BuildUnitsCommand(processing_run_id="run_1")
                )

            self.assertEqual(result.status, "failed")
            self.assertEqual(
                result.error["error_code"],
                "UNIT_SOURCE_AUDIT_FAILED",
            )
            self.assertEqual(
                uow.document_units.list_by_processing_run("run_1"),
                [],
            )

    def test_unaddressable_carrier_is_structured_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _setup(root)
            with mock.patch(
                "disclosure_anchor.application.use_cases.build_units."
                "prepare_and_audit_units",
                side_effect=SourceEvidenceClosureError("unaddressable"),
            ):
                result = _use_case(root, uow).execute(
                    BuildUnitsCommand(processing_run_id="run_1")
                )

            self.assertEqual(result.status, "failed")
            self.assertEqual(
                result.error["error_code"],
                "SOURCE_EVIDENCE_UNADDRESSABLE",
            )

    def test_unexpected_builder_error_remains_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _setup(root)
            with (
                mock.patch(
                    "disclosure_anchor.application.use_cases.build_units."
                    "prepare_and_audit_units",
                    side_effect=RuntimeError("boom"),
                ),
                self.assertRaises(BuildUnitsError) as raised,
            ):
                _use_case(root, uow).execute(
                    BuildUnitsCommand(processing_run_id="run_1")
                )

            self.assertEqual(
                raised.exception.error["error_code"],
                "BUILD_PREPARATION_FAILED",
            )
            self.assertIn("RuntimeError: boom", raised.exception.error["message"])

    def test_existing_units_reject_duplicate_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _setup(root)
            first = _use_case(root, uow).execute(
                BuildUnitsCommand(processing_run_id="run_1")
            )
            self.assertEqual(first.status, "succeeded")

            with self.assertRaises(BuildUnitsError) as raised:
                _use_case(root, uow).execute(
                    BuildUnitsCommand(processing_run_id="run_1")
                )

            self.assertEqual(
                raised.exception.error["error_code"],
                "UNITS_ALREADY_BUILT",
            )

    def test_snapshot_verification_failure_marks_run_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, _ = _setup(root)
            paths = _PathBuilder(root)

            result = _use_case(
                root,
                uow,
                artifact_store=_BadHashArtifactStore(paths),
            ).execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "failed")
            self.assertEqual(
                result.error["error_code"],
                "ARTIFACT_WRITE_FAILED",
            )
            self.assertEqual(
                uow.document_units.list_by_processing_run("run_1"),
                [],
            )

    def test_legacy_ir_requires_reparse_instead_of_guessing_structure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow, ir_relpath = _setup(root)
            payload = json.loads((root / ir_relpath).read_text(encoding="utf-8"))
            payload["contract_version"] = "normalized_ir.v3"
            payload["parser_artifacts"] = {
                "artifact_root_relpath": "parser/a",
                "content_list_relpath": "parser/a/content.json",
            }
            payload.pop("source_pdf_sha256")
            payload.pop("source_pdf_page_count")
            payload.pop("structure_proof")
            legacy_relpath = ir_relpath.with_name("normalized_ir.v3.json")
            run = uow.processing_runs.get("run_1")
            run.normalized_ir_relpath = str(legacy_relpath)
            uow.processing_runs.update(run)
            _rewrite_ir(root, uow, legacy_relpath, payload)

            result = _use_case(root, uow).execute(
                BuildUnitsCommand(processing_run_id="run_1")
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error["error_code"], "IR_CONTRACT_TOO_OLD")
            self.assertEqual(
                result.error["reason_code"],
                "structure_proof_reparse_required",
            )


class BuildUnitsDependencyTests(unittest.TestCase):
    def test_application_unit_path_does_not_import_adapters(self) -> None:
        trees: dict[str, ast.AST] = {}
        for name, module in {
            "build_units": build_units_module,
            "document_unit_audit": document_unit_audit_module,
        }.items():
            module_path = module.__file__
            assert module_path is not None
            trees[name] = ast.parse(Path(module_path).read_text(encoding="utf-8"))
        for name, tree in trees.items():
            imported = [
                module
                for node in ast.walk(tree)
                for module in (
                    [node.module]
                    if isinstance(node, ast.ImportFrom) and node.module
                    else [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
            ]
            with self.subTest(module=name):
                self.assertFalse(
                    [
                        module
                        for module in imported
                        if module.startswith("disclosure_anchor.adapters")
                    ]
                )
        audit_tree = trees["document_unit_audit"]
        audit_imported = {
            node.module
            for node in ast.walk(audit_tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        for module in (
            "disclosure_anchor.application.contracts.source_evidence_projection",
            "disclosure_anchor.application.contracts.canonical_occurrence",
        ):
            self.assertNotIn(module, audit_imported)
        prohibited_calls = {
            source_evidence_projection: (
                "native_evidence_gaps",
                "native_evidence_occurrences",
                "native_gap_physical_context",
                "native_gap_search_atoms",
            ),
            canonical_occurrence: ("canonical_occurrence_stream",),
            source_native_fallback: ("native_stream_unit_drafts",),
        }
        # A prohibited name that no longer exists silently stops protecting
        # anything, so the isolation list is pinned to live exports.
        for module, names in prohibited_calls.items():
            for name in names:
                with self.subTest(export=name):
                    self.assertTrue(hasattr(module, name))
        prohibited = {name for names in prohibited_calls.values() for name in names}
        called = {
            (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            for node in ast.walk(audit_tree)
            if isinstance(node, ast.Call)
        }
        self.assertTrue(prohibited.isdisjoint(called))


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    unittest.main()
