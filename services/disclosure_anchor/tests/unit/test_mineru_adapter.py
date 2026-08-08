import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import (
    MinerUArtifactReader,
)
from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    mineru_provider_item_sha256,
    resolved_table_html,
)
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper as _MinerUToNormalizedIRMapper,
)
from disclosure_anchor.adapters.parsers.mineru.structure_proof import (
    _assign_owner_scope_materialization,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence import (
    CarrierSourceSupport,
    ResolvedTableRole,
    SourceEvidenceContractError,
)
from tests.unit._native_support import (
    build_proof_with_auto_native,
    test_carrier_source_support as _test_carrier_source_support,
)
from disclosure_anchor.adapters.parsers.mineru.table_html_structure import (
    ParsedHtmlTable,
    TableHtmlStructureError,
    parse_table_html_structure,
)
from disclosure_anchor.adapters.parsers.mineru.text_projection import (
    build_mineru_text_projections,
)
from disclosure_anchor.adapters.parsers.pdf_native_text import (
    NativeTextAtom,
    NativeTextExtraction,
    NativeTextLayoutRef,
    NativeTextPage,
)
from disclosure_anchor.adapters.parsers.pdf_native_structure import (
    NATIVE_PDF_STRUCTURE_VERSION,
    NativeStructureIndex,
    validate_pdf_structure_artifact,
)
from disclosure_anchor.adapters.parsers.pdf_visual_evidence import (
    PNG_OPTIONS,
    RENDERER_IDENTITY,
    RENDER_OPTIONS,
    RenderedVisualEvidence,
    VisualPageEvidence,
)
from disclosure_anchor.adapters.parsers.mineru import mineru_process
from disclosure_anchor.adapters.parsers.mineru.existing_artifact_pipeline import (
    map_reconciled_mineru_content_list,
)
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.parser import (
    MinerUDocumentParser,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.contracts.normalized_ir import (
    NormalizedIRVersionError,
    validate_current_normalized_ir_for_write,
)
from disclosure_anchor.application.contracts.document_structure import (
    DocumentStructureContractError,
    validate_document_structure,
)
from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    TableReconciliationContractError,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    source_value_sha256,
)
from disclosure_anchor.domain.errors import (
    ParserBackendOverloadedError,
    ParserCancelledError,
    ParserLocalInvocationError,
    ParserOutputContractError,
    ParserTaskDeadlineError,
    ParserTaskError,
    ParserVersionProbeError,
)

from tests.unit._native_index import (
    marked_object,
    native_bookmark,
    native_index,
    native_node,
)


def _parser_info() -> MinerUParserInfo:
    return MinerUParserInfo(
        name="MinerU",
        package_version="3.4.0",
        backend="pipeline",
        method="auto",
        language="ch",
        formula=False,
        table=True,
        runtime_bundle_identity_sha256="sha256:" + "b" * 64,
    )


def _untagged_proof(
    content_list: list[dict[str, Any]],
    *,
    page_count: int = 1,
) -> dict[str, Any]:
    return build_proof_with_auto_native(
        native=native_index(page_count=page_count),
        content_list=content_list,
        source_pdf_sha256="sha256:" + "a" * 64,
    )


def _title_block(
    text: str,
    bbox: list[int],
    *,
    level: int = 1,
) -> dict[str, Any]:
    return {
        "type": "title",
        "bbox": bbox,
        "content": {
            "level": level,
            "title_content": [{"type": "text", "content": text}],
        },
    }


def _paragraph_block(
    text: str,
    bbox: list[int],
) -> dict[str, Any]:
    return {
        "type": "paragraph",
        "bbox": bbox,
        "content": {
            "paragraph_content": [{"type": "text", "content": text}],
        },
    }


def _v2_structure_proof(
    *,
    native: NativeStructureIndex,
    legacy_content_list: list[dict[str, Any]],
    content_list_v2: list[list[dict[str, Any]]],
    source_pdf_sha256: str = "sha256:" + "a" * 64,
    start_page: int | None = None,
    end_page: int | None = None,
    source_pages: tuple[Any, ...] | None = None,
    carrier_source_support: Mapping[
        tuple[int, str, int | None], CarrierSourceSupport
    ]
    | None = None,
    heading_display_texts: tuple[str, ...] = (),
    body_texts: tuple[str, ...] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    page_count = native.source_pdf_page_count
    projections = build_mineru_text_projections(
        legacy_content_list,
        content_list_v2,
        serializer_backend="pipeline",
        page_offset=start_page or 0,
        expected_page_count=page_count,
    )
    canonical_content = list(projections.canonical_items)
    if source_pages is not None and carrier_source_support is None:
        carrier_source_support = _test_carrier_source_support(
            canonical_content,
            source_pages=source_pages,
        )
    return (
        build_proof_with_auto_native(
            native=native,
            content_list=canonical_content,
            source_pdf_sha256=source_pdf_sha256,
            content_list_v2=content_list_v2,
            text_projections=projections,
            source_pages=source_pages,
            carrier_source_support=carrier_source_support,
            start_page=start_page,
            end_page=end_page,
            heading_display_texts=heading_display_texts,
            body_texts=body_texts,
        ),
        canonical_content,
    )


# Shared implementation lives in tests/unit/_native_support.py.
def _structure_elements(
    content_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            **item,
            "kind": "text",
            "raw_kind": item["type"],
            "source_item_index": index,
            "source_item_sha256": mineru_provider_item_sha256(item),
        }
        for index, item in enumerate(content_list)
    ]


def _native_heading_case(
    specs: list[
        tuple[
            str,
            str,
            str,
            tuple[str, ...],
            tuple[int, ...],
        ]
    ],
    *,
    provider_levels: tuple[int, ...] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], NativeStructureIndex]:
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": text,
            "page_idx": 0,
            "bbox": [100, 100 + index * 60, 300, 130 + index * 60],
            **(
                {"text_level": provider_levels[index]}
                if provider_levels is not None
                else {}
            ),
        }
        for index, (text, _, _, _, _) in enumerate(specs)
    ]
    native = native_index(
        nodes=[
            native_node(
                index + 1,
                role,
                [(0, 7 + index)],
                segment_id=segment_id,
                ancestor_roles=ancestor_roles,
                ancestor_node_ids=ancestor_node_ids,
            )
            for index, (
                _,
                role,
                segment_id,
                ancestor_roles,
                ancestor_node_ids,
            ) in enumerate(specs)
        ],
        marked_objects=[
            marked_object(
                0,
                7 + index,
                index,
                text=str(item["text"]),
                bbox=list(item["bbox"]),
            )
            for index, item in enumerate(content)
        ],
    )
    if provider_levels is None:
        proof = build_proof_with_auto_native(
            native=native,
            content_list=content,
            source_pdf_sha256="sha256:" + "a" * 64,
        )
    else:
        proof, content = _v2_structure_proof(
            native=native,
            legacy_content_list=content,
            content_list_v2=[
                [
                    _title_block(
                        str(item["text"]),
                        list(item["bbox"]),
                        level=level,
                    )
                    for item, level in zip(
                        content,
                        provider_levels,
                        strict=True,
                    )
                ]
            ],
        )
    return proof, content, native


class MinerUToNormalizedIRMapper(_MinerUToNormalizedIRMapper):
    """Test facade that supplies an explicit untagged proof when irrelevant."""

    def map_content_list(
        self,
        *,
        content_list: list[dict[str, Any]],
        parser_info: MinerUParserInfo,
        document_metadata: dict[str, Any],
        structure_proof: dict[str, Any] | None = None,
        source_pdf_sha256: str = "sha256:" + "a" * 64,
        source_pdf_page_count: int | None = None,
        table_structures: Mapping[int, ParsedHtmlTable] | None = None,
        parser_artifacts: dict[str, Any] | None = None,
        start_page: int | None = None,
        end_page: int | None = None,
    ) -> dict[str, Any]:
        page_count = source_pdf_page_count or max(
            (
                int(item["page_idx"]) + 1
                for item in content_list
                if isinstance(item.get("page_idx"), int)
                and not isinstance(item.get("page_idx"), bool)
            ),
            default=1,
        )
        try:
            proof = structure_proof or _untagged_proof(
                content_list,
                page_count=page_count,
            )
        except SourceEvidenceContractError as exc:
            raise ParserOutputContractError(
                f"invalid MinerU provider payload [{exc.reason_code}]: {exc}"
            ) from exc
        materialized_tables = dict(table_structures or {})
        for index, item in enumerate(content_list):
            if item.get("type") != "table" or index in materialized_tables:
                continue
            html = resolved_table_html(item)
            if isinstance(html, str) and html.strip():
                try:
                    materialized_tables[index] = parse_table_html_structure(html)
                except TableHtmlStructureError as exc:
                    raise ParserOutputContractError(
                        f"MinerU table HTML has no valid logical cells: {exc}"
                    ) from exc
        return super().map_content_list(
            content_list=content_list,
            parser_info=parser_info,
            document_metadata=document_metadata,
            structure_proof=proof,
            source_pdf_sha256=source_pdf_sha256,
            source_pdf_page_count=page_count,
            table_structures=materialized_tables,
            parser_artifacts=parser_artifacts,
            start_page=start_page,
            end_page=end_page,
        )


def _artifact_manifest(
    root: str,
    content_name: str,
    *,
    image_paths: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    present = {
        "availability": "present",
        "sha256": "sha256:" + ("a" * 64),
        "size_bytes": 2,
    }
    files = {
        "content_list": {
            **present,
            "relpath": f"{root}/{content_name}",
        },
        "content_list_v2": {
            **present,
            "relpath": f"{root}/content_list_v2.json",
        },
        "middle": {
            **present,
            "relpath": f"{root}/middle.json",
        },
        "model": {
            **present,
            "relpath": f"{root}/model.json",
        },
        "pdf_structure": {
            **present,
            "relpath": f"{root}/pdf_structure.json",
        },
        "source_evidence": {
            **present,
            "relpath": f"{root}/source_evidence.json",
        },
        "visual_semantics": {
            **present,
            "relpath": f"{root}/visual_semantics.json",
        },
    }
    files.update(
        {
            f"evidence_image_{index:06d}": {
                **present,
                "relpath": f"{root}/{path}",
            }
            for index, path in (image_paths or {}).items()
        }
    )
    return {
        "artifact_root_relpath": root,
        "files": files,
    }


def _attach_closed_table_diagnostics(normalized: dict[str, Any]) -> None:
    table_count = sum(
        element.get("raw_kind") == "table" for element in normalized["elements"]
    )
    normalized["parser_diagnostics"] = {
        "table_reconciliation": {
            "algorithm_version": "mineru-page-local-table-closure.v6",
            "model_hash": "sha256:" + "a" * 64,
            "content_tables": table_count,
            "model_tables": table_count,
            "matched_tables": table_count,
            "page_local_closed": True,
        },
        "visual_semantics": {
            "contract_version": "visual-semantics.v1",
            "artifact_role": "visual_semantics",
            "artifact_sha256": "sha256:" + "a" * 64,
            "disposition_count": 0,
            "status_counts": {
                "semantic_text": 0,
                "guard_only": 0,
                "unresolved": 0,
            },
        },
    }


def _native_text_fixture(
    pages: list[list[str]],
    *,
    bboxes: list[list[tuple[float, float, float, float]]] | None = None,
) -> NativeTextExtraction:
    native_pages: list[NativeTextPage] = []
    for page_idx, values in enumerate(pages):
        text = "\n".join(values)
        atoms: list[NativeTextAtom] = []
        cursor = 0
        for order, value in enumerate(values):
            if order:
                cursor += 1
            start = cursor
            cursor += len(value)
            atoms.append(
                NativeTextAtom(
                    page_idx=page_idx,
                    order=order,
                    bbox=(
                        bboxes[page_idx][order]
                        if bboxes is not None
                        else (
                            10.0,
                            10.0 + order * 20,
                            100.0,
                            20.0 + order * 20,
                        )
                    ),
                    char_span=(start, cursor),
                    text=value,
                    layout=NativeTextLayoutRef(0, order, 0, 0),
                )
            )
        native_pages.append(
            NativeTextPage(
                page_idx=page_idx,
                width=1000.0,
                height=1000.0,
                text=text,
                atoms=tuple(atoms),
            )
        )
    return NativeTextExtraction(
        pages=tuple(native_pages),
        pdftotext_version="pdftotext fixture",
        pdfinfo_version="pdfinfo fixture",
    )


def _untagged_native(
    page_count: int,
    *,
    source_pdf_sha256: str = "sha256:" + "a" * 64,
) -> dict[str, Any]:
    return {
        "contract_version": NATIVE_PDF_STRUCTURE_VERSION,
        "source_pdf_sha256": source_pdf_sha256,
        "source_pdf_page_count": page_count,
        "native_status": "untagged",
        "pdfium_tagged": False,
        "role_map": {},
        "segments": [],
        "nodes": [],
        "marked_content": [],
        "bookmarks": [],
        "diagnostics": {
            "parent_conflicts": 0,
            "unresolved": [],
            "root_reachable_nodes": 0,
            "visible_mcid_anchors": 0,
            "marked_content_objects": 0,
            "referenced_mcid_refs": 0,
            "resolved_mcid_refs": 0,
            "unresolved_mcid_refs": [],
            "object_issues": [],
        },
    }


def _validated_native_index(artifact: dict[str, Any]) -> NativeStructureIndex:
    """Read a complete artifact exactly as the production pipeline does."""

    return validate_pdf_structure_artifact(
        artifact,
        expected_source_pdf_sha256=str(artifact["source_pdf_sha256"]),
        expected_page_count=int(artifact["source_pdf_page_count"]),
    )


class MinerUProcessTests(unittest.TestCase):
    def tearDown(self) -> None:
        mineru_process._MINERU_SHUTDOWN_REQUESTED.clear()

    def test_command_includes_stable_phase04_options(self) -> None:
        process = MinerUProcess(executable=Path("/opt/mineru/bin/mineru"))
        command = process.command_for(
            input_pdf=Path("input.pdf"),
            output_dir=Path("out"),
            options=ParserOptions(
                start_page=0,
                end_page=2,
                http_request_concurrency=3,
            ),
        )
        self.assertEqual(
            command[:5], ["/opt/mineru/bin/mineru", "-p", "input.pdf", "-o", "out"]
        )
        self.assertIn("-m", command)
        self.assertIn("auto", command)
        self.assertIn("-b", command)
        self.assertIn("pipeline", command)
        self.assertIn("-f", command)
        self.assertEqual(command[command.index("-f") + 1], "true")
        self.assertIn("-t", command)
        self.assertIn("true", command)
        self.assertIn("-s", command)
        self.assertIn("0", command)
        self.assertIn("-e", command)
        self.assertIn("2", command)
        self.assertNotIn("-u", command)
        self.assertNotIn("--max-concurrency", command)
        self.assertNotIn("--image-analysis", command)
        self.assertNotIn("--effort", command)

    def test_command_appends_server_url_for_http_client_backend(self) -> None:
        process = MinerUProcess(executable=Path("/opt/mineru/bin/mineru"))
        command = process.command_for(
            input_pdf=Path("input.pdf"),
            output_dir=Path("out"),
            options=ParserOptions(
                backend="vlm-http-client",
                server_url="http://192.168.1.50:30000",
                http_request_concurrency=3,
            ),
        )
        self.assertIn("vlm-http-client", command)
        url_index = command.index("-u")
        self.assertEqual(command[url_index + 1], "http://192.168.1.50:30000")
        concurrency_index = command.index("--max-concurrency")
        self.assertEqual(command[concurrency_index + 1], "3")
        image_analysis_index = command.index("--image-analysis")
        self.assertEqual(command[image_analysis_index + 1], "true")

    def test_hybrid_high_effort_preserves_image_analysis(self) -> None:
        process = MinerUProcess(executable=Path("/opt/mineru/bin/mineru"))
        command = process.command_for(
            input_pdf=Path("input.pdf"),
            output_dir=Path("out"),
            options=ParserOptions(backend="hybrid-engine"),
        )

        self.assertEqual(command[command.index("--effort") + 1], "high")
        self.assertEqual(
            command[command.index("--image-analysis") + 1],
            "true",
        )

    def test_run_aligns_inner_deadline_and_classifies_process_failures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            options = ParserOptions(timeout_seconds=3600)
            runner = MinerUProcess(
                executable=Path("mineru"),
                extra_env={
                    "MINERU_TASK_RESULT_TIMEOUT_SECONDS": "999",
                    "MINERU_TABLE_MERGE_ENABLE": "1",
                },
            )
            self.assertEqual(
                mineru_process._task_result_timeout_seconds(600),
                450,
            )
            self.assertEqual(
                mineru_process._task_result_timeout_seconds(900),
                675,
            )
            self.assertEqual(
                mineru_process._task_result_timeout_seconds(901),
                676,
            )

            succeeded = mock.Mock(pid=101, returncode=0)
            succeeded.communicate.return_value = ("ok", "")
            with mock.patch.object(
                mineru_process.subprocess,
                "Popen",
                return_value=succeeded,
            ) as popen:
                runner.run(
                    input_pdf=input_pdf,
                    output_dir=root / "success",
                    options=options,
                )
            self.assertEqual(
                popen.call_args.kwargs["env"]["MINERU_TASK_RESULT_TIMEOUT_SECONDS"],
                "2700",
            )
            self.assertEqual(
                popen.call_args.kwargs["env"][
                    "MINERU_LOCAL_API_STARTUP_TIMEOUT_SECONDS"
                ],
                "120",
            )
            self.assertEqual(
                popen.call_args.kwargs["env"][
                    "MINERU_TASK_RESULT_DOWNLOAD_TIMEOUT_SECONDS"
                ],
                "120",
            )
            self.assertEqual(
                popen.call_args.kwargs["env"]["MINERU_TABLE_MERGE_ENABLE"],
                "0",
            )

            with mock.patch.object(
                mineru_process.subprocess,
                "Popen",
                side_effect=OSError("not executable"),
            ):
                with self.assertRaises(ParserLocalInvocationError):
                    runner.run(
                        input_pdf=input_pdf,
                        output_dir=root / "spawn-error",
                        options=options,
                    )

            failures = (
                (
                    "Error: Timed out waiting for result of task task-1 for input.pdf",
                    ParserTaskDeadlineError,
                ),
                (
                    '{"task_id":"task-2","status":"failed","error":""}',
                    ParserTaskError,
                ),
                (
                    '{"task_id":"task-3","status":"failed",'
                    '"error":"HTTP 429 Too Many Requests"}',
                    ParserBackendOverloadedError,
                ),
                (
                    "Unexpected status code: [429], response body: busy",
                    ParserBackendOverloadedError,
                ),
                (
                    "Local mineru-api exited before becoming healthy.",
                    ParserLocalInvocationError,
                ),
                (
                    "Timed out downloading result ZIP for task task-4",
                    ParserTaskError,
                ),
            )
            for stderr, expected_error in failures:
                with self.subTest(expected_error=expected_error.__name__):
                    failed = mock.Mock(pid=102, returncode=1)
                    failed.communicate.return_value = ("", stderr)
                    with mock.patch.object(
                        mineru_process.subprocess,
                        "Popen",
                        return_value=failed,
                    ):
                        with self.assertRaises(expected_error) as caught:
                            runner.run(
                                input_pdf=input_pdf,
                                output_dir=root / expected_error.__name__,
                                options=options,
                            )
                    self.assertIs(type(caught.exception), expected_error)

    def test_shutdown_kills_every_registered_process_group(self) -> None:
        process = mock.MagicMock(pid=43210)
        process.poll.return_value = None
        mineru_process._register_process(process)
        try:
            with mock.patch.object(mineru_process.os, "killpg") as killpg:
                terminated = mineru_process.terminate_active_mineru_processes(
                    grace_seconds=0
                )
        finally:
            mineru_process._unregister_process(process)

        self.assertEqual(terminated, 1)
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(43210, mineru_process.signal.SIGINT),
                mock.call(43210, mineru_process.signal.SIGKILL),
            ],
        )

    def test_worker_shutdown_is_not_classified_as_task_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF")
            process = mock.MagicMock(pid=43211, returncode=-9)
            process.poll.return_value = None

            def cancel_during_wait(*, timeout):  # noqa: ANN001
                del timeout
                mineru_process.terminate_active_mineru_processes(grace_seconds=0)
                return "", ""

            process.communicate.side_effect = cancel_during_wait
            with (
                mock.patch.object(
                    mineru_process.subprocess,
                    "Popen",
                    return_value=process,
                ),
                mock.patch.object(mineru_process.os, "killpg"),
            ):
                with self.assertRaises(ParserCancelledError):
                    MinerUProcess(executable=Path("mineru")).run(
                        input_pdf=input_pdf,
                        output_dir=root / "out",
                        options=ParserOptions(timeout_seconds=60),
                    )

    def test_version_probe_parses_click_contract_and_cleans_up_timeout(self) -> None:
        succeeded = mock.MagicMock(pid=54320, returncode=0)
        succeeded.communicate.return_value = ("mineru, version 3.4.0\n", "")
        with mock.patch.object(
            mineru_process.subprocess,
            "Popen",
            return_value=succeeded,
        ):
            self.assertEqual(
                MinerUProcess(executable=Path("mineru")).version(),
                "3.4.0",
            )

        malformed = mock.MagicMock(pid=54320, returncode=0)
        malformed.communicate.return_value = ("MinerU release 3.4.0", "")
        with mock.patch.object(
            mineru_process.subprocess,
            "Popen",
            return_value=malformed,
        ):
            with self.assertRaisesRegex(
                ParserVersionProbeError,
                "unsupported output contract",
            ):
                MinerUProcess(executable=Path("mineru")).version()

        process = mock.MagicMock(pid=54321, returncode=None)
        process.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["mineru", "-v"], timeout=0.01
        )
        with (
            mock.patch.object(mineru_process.subprocess, "Popen", return_value=process),
            mock.patch.object(mineru_process.os, "killpg") as killpg,
        ):
            probe = MinerUProcess(
                executable=Path("mineru"), version_timeout_seconds=0.01
            )
            with self.assertRaises(ParserVersionProbeError):
                probe.version()

        killpg.assert_called_once_with(54321, mineru_process.signal.SIGINT)
        process.wait.assert_called_once_with(
            timeout=mineru_process._GRACEFUL_STOP_SECONDS
        )
        self.assertNotIn(process, mineru_process._ACTIVE_PROCESSES)

    def test_late_process_registration_is_cancelled_immediately(self) -> None:
        mineru_process._MINERU_SHUTDOWN_REQUESTED.set()
        process = mock.MagicMock(pid=54322)

        with mock.patch.object(mineru_process.os, "killpg") as killpg:
            cancelled = mineru_process._register_process(process)
        try:
            self.assertTrue(cancelled)
            killpg.assert_called_once_with(54322, mineru_process.signal.SIGINT)
            self.assertIn(process, mineru_process._CANCELLED_PROCESSES)
        finally:
            mineru_process._unregister_process(process)


class MinerUArtifactReaderTests(unittest.TestCase):
    def test_locates_nested_content_list_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "sample" / "auto"
            nested.mkdir(parents=True)
            content_list = nested / "sample_content_list.json"
            content_list.write_text(
                '[{"type": "text", "text": "hello"}]', encoding="utf-8"
            )
            (nested / "sample_content_list_v2.json").write_text("[]", encoding="utf-8")
            middle = nested / "sample_middle.json"
            middle.write_text("{}", encoding="utf-8")
            model = nested / "sample_model.json"
            model.write_text("[]", encoding="utf-8")
            markdown = nested / "sample.md"
            markdown.write_text("hello", encoding="utf-8")
            (nested / "unrelated.md").write_text("wrong", encoding="utf-8")

            reader = MinerUArtifactReader()
            artifacts = reader.locate(root)
            self.assertEqual(artifacts.paths["content_list"], content_list)
            self.assertEqual(artifacts.paths["middle"], middle)
            self.assertEqual(
                artifacts.paths["content_list_v2"],
                nested / "sample_content_list_v2.json",
            )
            self.assertEqual(artifacts.paths["markdown"], markdown)
            self.assertEqual(artifacts.paths["model"], model)
            self.assertEqual(reader.read_content_list(content_list)[0]["text"], "hello")

    def test_embedded_table_media_are_registered_per_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = root / "images"
            images.mkdir()
            table_crop = images / "table.png"
            shared = images / "shared.png"
            table_crop.write_bytes(b"table-crop")
            shared.write_bytes(b"shared-cell-image")

            content_list = root / "sample_content_list.json"
            content_list.write_text(
                json.dumps(
                    [
                        {
                            "type": "table",
                            "img_path": "images/table.png",
                            "table_body": (
                                "<table><tr><td>"
                                '<img src="images/shared.png"/>'
                                '<img src="images/shared.png"/>'
                                "</td></tr></table>"
                            ),
                        }
                    ]
                ),
                encoding="utf-8",
            )
            artifact = MinerUArtifactReader().read_content_artifact(content_list)

        self.assertEqual(
            artifact.evidence_image_paths,
            {
                "evidence_image_000000": table_crop.resolve(),
                "evidence_table_media_000000_000000": shared.resolve(),
                "evidence_table_media_000000_000001": shared.resolve(),
            },
        )

    def test_content_artifact_rejects_unsafe_or_absent_image_paths(self) -> None:
        unsafe_values = (
            "../outside.png",
            "/absolute.png",
            "C:/windows.png",
            "images\\backslash.png",
            "file:images/value.png",
        )
        for value in unsafe_values:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                content_list = Path(tmp) / "sample_content_list.json"
                content_list.write_text(
                    json.dumps([{"type": "image", "img_path": value}]),
                    encoding="utf-8",
                )
                with self.assertRaises(ParserOutputContractError):
                    MinerUArtifactReader().read_content_artifact(content_list)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content_list = root / "sample_content_list.json"
            content_list.write_text(
                json.dumps([{"type": "image", "img_path": "images/missing.png"}]),
                encoding="utf-8",
            )
            with self.assertRaises(ParserOutputContractError):
                MinerUArtifactReader().read_content_artifact(content_list)

            directory = root / "images" / "directory.png"
            directory.mkdir(parents=True)
            content_list.write_text(
                json.dumps([{"type": "image", "img_path": "images/directory.png"}]),
                encoding="utf-8",
            )
            with self.assertRaises(ParserOutputContractError):
                MinerUArtifactReader().read_content_artifact(content_list)

            outside = root.parent / f"{root.name}-outside.png"
            outside.write_bytes(b"outside")
            symlink = root / "images" / "symlink.png"
            symlink.symlink_to(outside)
            content_list.write_text(
                json.dumps([{"type": "image", "img_path": "images/symlink.png"}]),
                encoding="utf-8",
            )
            try:
                with self.assertRaises(ParserOutputContractError):
                    MinerUArtifactReader().read_content_artifact(content_list)
            finally:
                outside.unlink()


class MinerUMapperTests(unittest.TestCase):
    def test_structure_proof_rejects_carrier_without_validated_source_support(
        self,
    ) -> None:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "明确标题",
                "page_idx": 0,
                "bbox": [100, 100, 300, 130],
            }
        ]

        with self.assertRaisesRegex(
            ParserOutputContractError,
            "lacks validated source-PDF support",
        ):
            build_proof_with_auto_native(
                native=_validated_native_index(_untagged_native(1)),
                content_list=content,
                source_pdf_sha256="sha256:" + "a" * 64,
                carrier_source_support={},
            )

    def test_maps_every_deployed_typed_text_carrier_without_shape_loss(
        self,
    ) -> None:
        content_list = [
            {
                "type": "ref_text",
                "text": "危险废物焚烧污染控制标准(GB18484—2020)",
                "page_idx": 0,
                "bbox": [100, 100, 500, 120],
            },
            {
                "type": "phonetic",
                "text": "全体董事签字：",
                "page_idx": 0,
                "bbox": [100, 130, 300, 150],
            },
            {
                "type": "equation",
                "text": "E=mc^2",
                "text_format": "latex",
                "page_idx": 0,
                "bbox": [100, 160, 300, 190],
            },
            {
                "type": "list",
                "sub_type": "text",
                "list_items": ["第一项", "第二项"],
                "page_idx": 0,
                "bbox": [100, 200, 500, 250],
            },
            {
                "type": "list",
                "sub_type": "text",
                "list_items": ["第一项\n第二项"],
                "page_idx": 0,
                "bbox": [100, 260, 500, 310],
            },
            {
                "type": "image",
                "text": "模型生成的图片描述",
                "img_path": "images/generated.png",
                "page_idx": 0,
                "bbox": [100, 320, 500, 500],
            },
        ]

        normalized = MinerUToNormalizedIRMapper().map_content_list(
            content_list=content_list,
            parser_info=_parser_info(),
            document_metadata={
                "document_id": "doc_typed_carriers",
                "source_pdf": "raw/doc_typed_carriers.pdf",
            },
            parser_artifacts=_artifact_manifest(
                "parser/doc_typed_carriers",
                "content.json",
                image_paths={5: "images/generated.png"},
            ),
        )
        _attach_closed_table_diagnostics(normalized)
        validate_current_normalized_ir_for_write(normalized)

        self.assertEqual(
            [element["kind"] for element in normalized["elements"]],
            ["text", "text", "equation", "text", "text", "image"],
        )
        self.assertEqual(
            [element["raw_kind"] for element in normalized["elements"][:2]],
            ["ref_text", "phonetic"],
        )
        self.assertEqual(normalized["elements"][2]["text_format"], "latex")
        self.assertEqual(
            normalized["elements"][3]["list_items"],
            ["第一项", "第二项"],
        )
        self.assertEqual(
            normalized["elements"][4]["list_items"],
            ["第一项\n第二项"],
        )
        self.assertNotEqual(
            normalized["elements"][3]["source_item_sha256"],
            normalized["elements"][4]["source_item_sha256"],
        )
        self.assertEqual(
            normalized["elements"][5]["text_provenance"],
            "generated_annotation",
        )
        del normalized["elements"][5]["text_provenance"]
        with self.assertRaises(NormalizedIRVersionError) as raised:
            validate_current_normalized_ir_for_write(normalized)
        self.assertEqual(
            raised.exception.reason_code,
            "element_image_text_provenance_invalid",
        )

        with self.assertRaisesRegex(
            ParserOutputContractError,
            "unmapped payload fields",
        ):
            MinerUToNormalizedIRMapper().map_content_list(
                content_list=[{**content_list[0], "future_payload": ""}],
                parser_info=_parser_info(),
                document_metadata={"document_id": "doc_provider_schema_drift"},
            )

    def test_table_html_alias_uses_nonempty_value_and_rejects_conflicts(self) -> None:
        mapper = MinerUToNormalizedIRMapper()
        metadata = {"document_id": "doc_table_alias", "title": "样本"}
        normalized = mapper.map_content_list(
            content_list=[
                {
                    "type": "table",
                    "page_idx": 0,
                    "img_path": "images/table.jpg",
                    "table_body": "",
                    "table_html": "<table><tr><td>A</td></tr></table>",
                }
            ],
            parser_info=_parser_info(),
            document_metadata=metadata,
        )
        self.assertEqual(normalized["elements"][0]["table"]["rows"], [["A"]])

        with self.assertRaises(ParserOutputContractError):
            mapper.map_content_list(
                content_list=[
                    {
                        "type": "table",
                        "page_idx": 0,
                        "img_path": "images/table.jpg",
                        "table_body": "<table><tr><td>A</td></tr></table>",
                        "table_html": "<table><tr><td>B</td></tr></table>",
                    }
                ],
                parser_info=_parser_info(),
                document_metadata=metadata,
            )

    def test_table_media_ir_requires_exact_manifest_occurrence_roles(self) -> None:
        root = "parser/table_media"
        manifest = _artifact_manifest(
            root,
            "content.json",
            image_paths={0: "images/table.png"},
        )
        manifest["files"]["evidence_table_media_000000_000000"] = {
            "availability": "present",
            "relpath": f"{root}/images/cell.png",
            "sha256": "sha256:" + "c" * 64,
            "size_bytes": 3,
        }
        normalized = MinerUToNormalizedIRMapper().map_content_list(
            content_list=[
                {
                    "type": "table",
                    "page_idx": 0,
                    "bbox": [100, 100, 900, 900],
                    "img_path": "images/table.png",
                    "table_body": (
                        '<table><tr><td>值<img src="images/cell.png"/>'
                        "</td></tr></table>"
                    ),
                }
            ],
            parser_info=_parser_info(),
            document_metadata={
                "document_id": "doc_table_media",
                "source_pdf": "raw/doc_table_media.pdf",
            },
            parser_artifacts=manifest,
        )
        _attach_closed_table_diagnostics(normalized)

        self.assertEqual(
            normalized["elements"][0]["table"]["embedded_media"],
            [
                {
                    "occurrence_index": 0,
                    "cell_media_index": 0,
                    "row": 0,
                    "col": 0,
                    "rowspan": 1,
                    "colspan": 1,
                    "image_path": "images/cell.png",
                    "artifact_role": "evidence_table_media_000000_000000",
                }
            ],
        )
        validate_current_normalized_ir_for_write(normalized)

        del manifest["files"]["evidence_table_media_000000_000000"]
        normalized["parser_artifacts"] = manifest
        with self.assertRaisesRegex(
            NormalizedIRVersionError,
            "not manifest-bound",
        ):
            validate_current_normalized_ir_for_write(normalized)

    def test_mapper_never_promotes_repeated_text_level_without_source_proof(
        self,
    ) -> None:
        mapper = MinerUToNormalizedIRMapper()
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "2025年度财务报表附注",
                "page_idx": 0,
                "bbox": [100, 400, 360, 430],
                "text_level": 1,
            }
        ]
        content.extend(
            {
                "type": "text",
                "text": "2025年度财务报表附注",
                "page_idx": page,
                "bbox": [120 + page % 2, 80, 360 + page % 2, 100],
                "text_level": 1,
            }
            for page in (1, 2, 3)
        )

        normalized = mapper.map_content_list(
            content_list=content,
            parser_info=_parser_info(),
            document_metadata={"document_id": "doc_layout", "title": "样本"},
        )

        self.assertTrue(all(item["kind"] == "text" for item in normalized["elements"]))
        self.assertTrue(
            all(item["raw_kind"] == "text" for item in normalized["elements"])
        )
        self.assertTrue(all(item["text_level"] == 1 for item in normalized["elements"]))
        self.assertEqual(
            [item["relation"] for item in normalized["structure_proof"]["conflicts"]],
            ["provider_heading_unproved"] * 4,
        )

    def test_source_local_level_difference_keeps_exact_heading_anchor(
        self,
    ) -> None:
        content = [
            {
                "type": "text",
                "text": "明确标题",
                "page_idx": 0,
                "bbox": [100, 100, 300, 130],
                "text_level": 1,
            }
        ]
        native = native_index(
            nodes=[native_node(1, "H2", [(0, 7)], parent_consistent=None)],
            marked_objects=[
                marked_object(
                    0,
                    7,
                    0,
                    text="明确标题",
                    bbox=[100, 100, 300, 130],
                )
            ],
        )
        content_v2 = [
            [
                {
                    "type": "title",
                    "bbox": [100, 100, 300, 130],
                    "content": {
                        "level": 1,
                        "title_content": [{"type": "text", "content": "明确标题"}],
                    },
                }
            ]
        ]

        proof, content = _v2_structure_proof(
            native=native,
            legacy_content_list=content,
            content_list_v2=content_v2,
        )

        self.assertEqual(len(proof["headings"]), 1)
        self.assertEqual(proof["headings"][0]["heading_level"], 1)
        self.assertTrue(proof["headings"][0]["propagates"])
        self.assertEqual(
            proof["headings"][0]["evidence_kinds"],
            ["mineru_v2_title", "native_layout"],
        )
        self.assertIn(
            "heading_level_conflict",
            [item["relation"] for item in proof["conflicts"]],
        )

    def test_native_heading_context_and_segments_control_structure(
        self,
    ) -> None:
        proof, content, native = _native_heading_case(
            [
                ("第一结构根", "H1", "native_1", (), ()),
                ("第二结构根", "H1", "native_2", (), ()),
                (
                    "第二结构子标题",
                    "H2",
                    "native_2",
                    ("H1",),
                    (2,),
                ),
            ],
            provider_levels=(1, 2, 3),
        )

        self.assertEqual(
            [
                (
                    heading["propagates"],
                    heading["heading_level"],
                    heading["parent_node_id"],
                    heading.get("native_segment_id"),
                    heading.get("native_node_id"),
                )
                for heading in proof["headings"]
            ],
            [
                (True, 1, None, "native_1", 1),
                (True, 1, None, None, None),
                (True, 1, None, None, None),
            ],
        )
        self.assertIn(
            "heading_level_conflict",
            [item["relation"] for item in proof["conflicts"]],
        )
        self.assertIn(
            "heading_hierarchy_flattened",
            [item["relation"] for item in proof["conflicts"]],
        )
        validate_document_structure(
            proof,
            elements=_structure_elements(content),
        )

        toc_proof, _, _ = _native_heading_case(
            [
                (
                    "目录中的条目",
                    "H2",
                    "native_1",
                    ("Document", "TOC", "TOCI"),
                    (10, 11, 12),
                )
            ],
            provider_levels=(2,),
        )

        self.assertEqual(toc_proof["headings"], [])
        toc_conflict = next(
            item
            for item in toc_proof["conflicts"]
            if item["relation"] == "native_heading_non_section_ancestry"
        )
        self.assertEqual(toc_conflict["native_roles"], ["TOC", "TOCI"])

        first_node = native.nodes[0]
        ambiguous = build_proof_with_auto_native(
            native=replace(
                native,
                nodes=(first_node, replace(first_node, node_id=4)),
            ),
            content_list=[content[0]],
            source_pdf_sha256="sha256:" + "a" * 64,
        )

        # The duplicated authored claim kills the StructTree chain, but the
        # rendered native line still witnesses the heading identity at root.
        self.assertEqual(
            [
                (
                    heading["heading_level"],
                    heading["parent_node_id"],
                    heading["propagates"],
                    heading["evidence_kinds"],
                )
                for heading in ambiguous["headings"]
            ],
            [(1, None, True, ["native_layout"])],
        )
        self.assertIn(
            "native_heading_ancestry_conflict",
            [item["relation"] for item in ambiguous["conflicts"]],
        )
        validate_document_structure(
            ambiguous,
            elements=_structure_elements([content[0]]),
        )

    def test_native_paragraph_role_rejects_provider_only_title(
        self,
    ) -> None:
        content = [
            {
                "type": "text",
                "text": "非经营性占用资金",
                "page_idx": 0,
                "bbox": [100, 100, 300, 130],
                "text_level": 1,
            },
            {
                "type": "text",
                "text": "本报告期不存在相关情形。",
                "page_idx": 0,
                "bbox": [100, 150, 500, 180],
            },
        ]
        proof, content = _v2_structure_proof(
            native=native_index(
                nodes=[native_node(1, "P", [(0, 7)])],
                marked_objects=[
                    marked_object(
                        0,
                        7,
                        0,
                        text="非经营性占用资金",
                        bbox=[100, 100, 300, 130],
                    )
                ],
            ),
            legacy_content_list=content,
            content_list_v2=[
                [
                    {
                        "type": "title",
                        "bbox": [100, 100, 300, 130],
                        "content": {
                            "level": 1,
                            "title_content": [
                                {
                                    "type": "text",
                                    "content": "非经营性占用资金",
                                }
                            ],
                        },
                    },
                    _paragraph_block(
                        "本报告期不存在相关情形。",
                        [100, 150, 500, 180],
                    ),
                ]
            ],
            body_texts=("非经营性占用资金",),
        )

        self.assertEqual(proof["headings"], [])
        role_conflict = next(
            item
            for item in proof["conflicts"]
            if item["relation"] == "heading_role_conflict"
        )
        self.assertEqual(role_conflict["native_roles"], ["P"])

    def test_source_bound_v2_titles_are_canonical_structure(self) -> None:
        content = [
            {
                "type": "text",
                "text": text,
                "page_idx": 0,
                "bbox": bbox,
            }
            for text, bbox in (
                ("来源标题", [100, 200, 300, 230]),
                ("子标题候选", [100, 250, 300, 280]),
                (
                    "备注\\*: A\\_B\\`C\\~D\\$E；奇偶 \\* \\\\\\*",
                    [100, 300, 700, 330],
                ),
                ("占比 $10\\%$ 以上", [100, 350, 500, 380]),
                ("正文", [100, 400, 500, 430]),
            )
        ]
        for index, level in enumerate((1, 2, 2, 2)):
            content[index]["text_level"] = level
        proof, content = _v2_structure_proof(
            native=native_index(page_count=1),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _title_block(
                        "来源标题",
                        [100, 200, 300, 230],
                    ),
                    _title_block(
                        "子标题候选",
                        [100, 250, 300, 280],
                        level=2,
                    ),
                    _title_block(
                        "备注*: A_B`C~D$E；奇偶 \\* \\\\*",
                        [100, 300, 700, 330],
                        level=2,
                    ),
                    {
                        "type": "title",
                        "bbox": [100, 350, 500, 380],
                        "content": {
                            "level": 2,
                            "title_content": [
                                {"type": "text", "content": "占比 "},
                                {
                                    "type": "equation_inline",
                                    "content": " 10\\% ",
                                },
                                {"type": "text", "content": " 以上"},
                            ],
                        },
                    },
                    _paragraph_block(
                        "正文",
                        [100, 400, 500, 430],
                    ),
                ]
            ],
        )

        self.assertEqual(
            [
                (
                    heading["heading_level"],
                    heading["parent_node_id"],
                    heading["section_span"],
                    heading["evidence_kinds"],
                )
                for heading in proof["headings"]
            ],
            [
                (1, None, [0, 0], ["mineru_v2_title", "native_layout"]),
                (1, None, [1, 1], ["mineru_v2_title", "native_layout"]),
                (1, None, [2, 2], ["mineru_v2_title", "native_layout"]),
                (1, None, [3, 4], ["mineru_v2_title", "native_layout"]),
            ],
        )
        self.assertNotIn(
            "provider_heading_unproved",
            {item["relation"] for item in proof["conflicts"]},
        )
        validate_document_structure(
            proof,
            elements=_structure_elements(content),
        )

    def test_source_bound_v2_title_opens_a_new_section(self) -> None:
        content = [
            {
                "type": "text",
                "text": "原生标题",
                "page_idx": 0,
                "bbox": [100, 200, 300, 230],
            },
            {
                "type": "text",
                "text": "单源标题候选",
                "page_idx": 0,
                "bbox": [100, 250, 300, 280],
            },
            {
                "type": "text",
                "text": "正文",
                "page_idx": 0,
                "bbox": [100, 300, 500, 330],
            },
        ]
        content[1]["text_level"] = 1
        proof, content = _v2_structure_proof(
            native=native_index(
                nodes=[native_node(7, "H1", [(0, 7)])],
                marked_objects=[
                    marked_object(
                        0,
                        7,
                        0,
                        text="原生标题",
                        bbox=[100, 200, 300, 230],
                    )
                ],
            ),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _paragraph_block(
                        "原生标题",
                        [100, 200, 300, 230],
                    ),
                    _title_block(
                        "单源标题候选",
                        [100, 250, 300, 280],
                    ),
                    _paragraph_block(
                        "正文",
                        [100, 300, 500, 330],
                    ),
                ]
            ],
        )

        self.assertEqual(
            [
                (
                    heading["propagates"],
                    heading["section_span"],
                )
                for heading in proof["headings"]
            ],
            [(True, [0, 0]), (True, [1, 2])],
        )
        self.assertNotIn(
            "provider_heading_unproved",
            {item["relation"] for item in proof["conflicts"]},
        )
        validate_document_structure(
            proof,
            elements=_structure_elements(content),
        )

        stale = json.loads(json.dumps(proof))
        stale["algorithm_version"] = "document-structure-evidence.v2"
        with self.assertRaises(DocumentStructureContractError) as caught:
            validate_document_structure(
                stale,
                elements=_structure_elements(content),
            )
        self.assertEqual(
            caught.exception.reason_code,
            "structure_proof_version_unsupported",
        )

    def test_repeated_source_bound_provider_titles_remain_structure(self) -> None:
        content = [
            {
                "type": "text",
                "text": "跨页固定标题",
                "page_idx": page,
                "bbox": [100 + page % 2, 80, 300 + page % 2, 105],
                "text_level": 1,
            }
            for page in range(3)
        ]
        proof, content = _v2_structure_proof(
            native=native_index(page_count=3),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _title_block(
                        "跨页固定标题",
                        [100 + page % 2, 80, 300 + page % 2, 105],
                    )
                ]
                for page in range(3)
            ],
        )

        self.assertEqual(len(proof["headings"]), 3)
        self.assertEqual(
            [heading["section_span"] for heading in proof["headings"]],
            [[0, 0], [1, 1], [2, 2]],
        )
        self.assertEqual(proof["page_frames"], [])
        validate_document_structure(
            proof,
            elements=_structure_elements(content),
        )

    def test_provider_title_layout_does_not_override_typed_structure(self) -> None:
        cases = {
            "two_pages": (
                [("仅两页", page, [100, 80, 300, 105]) for page in range(2)],
                2,
            ),
            "changing_bbox": (
                [
                    ("位置变化", page, [100 + page * 4, 80, 300, 105])
                    for page in range(3)
                ],
                3,
            ),
            "body_band": (
                [("正文重复", page, [100, 400, 300, 425]) for page in range(3)],
                3,
            ),
        }
        for label, (values, _expected_headings) in cases.items():
            with self.subTest(label=label):
                page_count = max(page for _, page, _ in values) + 1
                legacy_content = [
                    {
                        "type": "text",
                        "text": text,
                        "page_idx": page,
                        "bbox": bbox,
                        "text_level": 1,
                    }
                    for text, page, bbox in values
                ]
                proof, _ = _v2_structure_proof(
                    native=native_index(page_count=page_count),
                    legacy_content_list=legacy_content,
                    content_list_v2=[
                        [
                            _title_block(
                                text,
                                bbox,
                            )
                        ]
                        for text, _, bbox in values
                    ],
                )

                self.assertEqual(
                    len(proof["headings"]),
                    len(values),
                )
                self.assertEqual(proof["page_frames"], [])

    def test_bookmark_corroborated_repeated_titles_remain_headings(self) -> None:
        content = [
            {
                "type": "text",
                "text": "重复但有书签",
                "page_idx": page,
                "bbox": [100, 80, 300, 105],
                "text_level": 1,
            }
            for page in range(3)
        ]
        proof, content = _v2_structure_proof(
            native=native_index(
                page_count=3,
                bookmarks=[
                    native_bookmark(
                        page,
                        1,
                        "重复但有书签",
                        page_idx=page,
                        destination_y=90,
                    )
                    for page in range(3)
                ],
            ),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _title_block(
                        "重复但有书签",
                        [100, 80, 300, 105],
                    )
                ]
                for _ in range(3)
            ],
        )

        self.assertEqual(len(proof["headings"]), 3)
        self.assertEqual(proof["page_frames"], [])
        self.assertTrue(
            all(
                heading["propagates"]
                and heading["evidence_kinds"]
                == ["bookmark", "mineru_v2_title", "native_layout"]
                for heading in proof["headings"]
            )
        )

    def test_uncorroborated_bookmark_opens_no_section(self) -> None:
        # A bookmark is a navigation claim, not a structure witness on
        # its own: a style-abused body line (a form serial) aligns as a
        # bookmark and nothing else, while a real exported heading also
        # reaches the StructTree or the provider title lane. Entry-level
        # admission, no lane-level voting.
        content = [
            {
                "type": "text",
                "text": "真实文档标题",
                "page_idx": 0,
                "bbox": [100, 80, 300, 105],
                "text_level": 1,
            },
            {
                "type": "text",
                "text": "样例流水号",
                "page_idx": 0,
                "bbox": [100, 120, 300, 145],
                "text_level": None,
            },
        ]
        proof, _content = _v2_structure_proof(
            native=native_index(
                page_count=1,
                bookmarks=[
                    native_bookmark(0, 1, "对不上的书签一", destination_y=90),
                    native_bookmark(1, 1, "对不上的书签二", destination_y=90),
                    native_bookmark(2, 1, "样例流水号", destination_y=130),
                ],
            ),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _title_block("真实文档标题", [100, 80, 300, 105]),
                    _paragraph_block("样例流水号", [100, 120, 300, 145]),
                ]
            ],
        )

        self.assertEqual(len(proof["headings"]), 1)
        heading = proof["headings"][0]
        self.assertEqual(
            heading["evidence_kinds"],
            ["mineru_v2_title", "native_layout"],
        )
        self.assertEqual(heading["section_span"], [0, 1])
        relations = sorted(
            conflict["relation"] for conflict in proof["conflicts"]
        )
        self.assertEqual(
            relations,
            [
                "bookmark_unaligned",
                "bookmark_unaligned",
                "bookmark_uncorroborated",
            ],
        )
        rejected = next(
            conflict
            for conflict in proof["conflicts"]
            if conflict["relation"] == "bookmark_uncorroborated"
        )
        self.assertEqual(rejected["source_item_indices"], [1])

    def _layout_atom(
        self,
        order,
        text,
        *,
        block,
        line,
        cx,
        cy,
        page_idx=0,
    ):
        from disclosure_anchor.adapters.parsers.pdf_native_text import (
            NativeTextAtom,
            NativeTextLayoutRef,
        )

        return NativeTextAtom(
            page_idx=page_idx,
            order=order,
            bbox=(cx - 10.0, cy - 4.0, cx + 10.0, cy + 4.0),
            char_span=(0, len(text)),
            text=text,
            layout=NativeTextLayoutRef(
                flow_index=0,
                block_index=block,
                line_index=line,
                word_index=0,
            ),
        )

    def _layout_page(self, atoms, *, page_idx=0):
        from disclosure_anchor.adapters.parsers.pdf_native_text import (
            NativeTextPage,
        )

        return NativeTextPage(
            page_idx=page_idx,
            width=600.0,
            height=800.0,
            text="".join(a.text for a in atoms),
            atoms=tuple(atoms),
        )

    def test_numbered_table_caption_resets_only_peer_or_higher_owner(self) -> None:
        def proof_for(owner: str, caption: str) -> dict[str, Any]:
            content = [
                {
                    "type": "text",
                    "text": owner,
                    "page_idx": 0,
                    "bbox": [300, 80, 700, 105],
                },
                {
                    "type": "table",
                    "table_caption": [caption],
                    "table_footnote": [],
                    "table_body": "<table><tr><td>值</td></tr></table>",
                    "page_idx": 0,
                    "bbox": [100, 230, 900, 700],
                },
            ]
            owner_atom = replace(
                self._layout_atom(
                    0,
                    owner,
                    block=1,
                    line=0,
                    cx=300,
                    cy=70,
                ),
                bbox=(250.0, 63.0, 350.0, 77.0),
            )
            page = self._layout_page(
                [
                    owner_atom,
                    self._layout_atom(
                        1,
                        caption,
                        block=2,
                        line=0,
                        cx=90,
                        cy=170,
                    ),
                    self._layout_atom(
                        2,
                        "值",
                        block=3,
                        line=0,
                        cx=90,
                        cy=220,
                    ),
                ]
            )
            table_role = ResolvedTableRole(
                source_item_index=1,
                page_idx=0,
                parent_bbox=(100.0, 230.0, 900.0, 700.0),
                field="table_caption",
                index=0,
                bbox=(100.0, 200.0, 300.0, 220.0),
                provider_deleted=False,
                text=caption,
            )
            support = _test_carrier_source_support(
                content,
                source_pages=(page,),
                table_role_overrides=(table_role,),
            )
            return build_proof_with_auto_native(
                native=native_index(
                    page_count=1,
                    nodes=[native_node(1, "H1", [(0, 7)])],
                    marked_objects=[
                        marked_object(
                            0,
                            7,
                            0,
                            text=owner,
                            bbox=content[0]["bbox"],
                        )
                    ],
                ),
                content_list=content,
                source_pdf_sha256="sha256:" + "a" * 64,
                source_pages=(page,),
                carrier_source_support=support,
                table_role_overrides=(table_role,),
            )

        peer = proof_for("二、原有章节", "三、新表格章节")
        caption = "三、新表格章节"
        caption_item = {
            "type": "table",
            "table_caption": [caption],
            "table_footnote": [],
            "table_body": "<table><tr><td>值</td></tr></table>",
            "page_idx": 0,
            "bbox": [100, 230, 900, 700],
        }
        self.assertEqual(
            peer["owner_scope_breaks"],
            [
                {
                    "boundary_source_ref": {
                        "source_item_index": 1,
                        "source_item_sha256": mineru_provider_item_sha256(
                            caption_item
                        ),
                        "page_index": 0,
                        "field": "table_caption",
                        "index": 0,
                        "text_span": [0, len(caption)],
                        "value_sha256": source_value_sha256(caption),
                    },
                    "source_atom_orders": [1],
                    "eligibility_basis": "numbered_caption_native_break",
                    "relative_rank": "peer",
                    "current_owner_node_id": 1,
                    "target_node_id": None,
                    "boundary_carrier_scope": "selected_and_same_carrier",
                    "materialization_policy": "direct_target",
                    "flatten_subtree_root_node_id": None,
                }
            ],
        )

        deeper = proof_for("(三)资产、负债情况分析", "1.资产及负债状况")
        self.assertEqual(deeper["owner_scope_breaks"], [])

    def test_unnumbered_display_reset_requires_same_unnumbered_owner_family(
        self,
    ) -> None:
        def proof_for(
            owner: str,
            *,
            candidate_block: int = 2,
        ) -> dict[str, Any]:
            candidate = "母公司所有者权益变动表"
            content = [
                {
                    "type": "text",
                    "text": owner,
                    "page_idx": 0,
                    "bbox": [300, 80, 700, 105],
                },
                {
                    "type": "text",
                    "text": candidate,
                    "page_idx": 0,
                    "bbox": [300, 180, 700, 205],
                },
                {
                    "type": "text",
                    "text": "2023年1—12月",
                    "page_idx": 0,
                    "bbox": [400, 210, 600, 230],
                },
                {
                    "type": "table",
                    "table_caption": [],
                    "table_footnote": [],
                    "table_body": "<table><tr><td>值</td></tr></table>",
                    "page_idx": 0,
                    "bbox": [100, 245, 900, 700],
                },
            ]
            atoms = [
                replace(
                    self._layout_atom(
                        0, owner, block=1, line=0, cx=300, cy=70
                    ),
                    bbox=(250.0, 63.0, 350.0, 77.0),
                ),
                replace(
                    self._layout_atom(
                        1,
                        candidate,
                        block=candidate_block,
                        line=0,
                        cx=300,
                        cy=150,
                    ),
                    bbox=(250.0, 143.0, 350.0, 157.0),
                ),
                self._layout_atom(
                    2,
                    "2023年1—12月",
                    block=candidate_block,
                    line=1,
                    cx=300,
                    cy=175,
                ),
                self._layout_atom(
                    3,
                    "值",
                    block=3,
                    line=0,
                    cx=90,
                    cy=220,
                ),
            ]
            page = self._layout_page(atoms)
            support = _test_carrier_source_support(
                content,
                source_pages=(page,),
            )
            return build_proof_with_auto_native(
                native=native_index(
                    page_count=1,
                    nodes=[native_node(1, "H1", [(0, 7)])],
                    marked_objects=[
                        marked_object(
                            0,
                            7,
                            0,
                            text=owner,
                            bbox=content[0]["bbox"],
                        )
                    ],
                ),
                content_list=content,
                source_pdf_sha256="sha256:" + "a" * 64,
                source_pages=(page,),
                carrier_source_support=support,
            )

        repeated_display = proof_for("合并所有者权益变动表")
        candidate = "母公司所有者权益变动表"
        candidate_item = {
            "type": "text",
            "text": candidate,
            "page_idx": 0,
            "bbox": [300, 180, 700, 205],
        }
        self.assertEqual(
            repeated_display["owner_scope_breaks"],
            [
                {
                    "boundary_source_ref": {
                        "source_item_index": 1,
                        "source_item_sha256": mineru_provider_item_sha256(
                            candidate_item
                        ),
                        "page_index": 0,
                        "field": "text",
                        "text_span": [0, len(candidate)],
                        "value_sha256": source_value_sha256(candidate),
                    },
                    "source_atom_orders": [1],
                    "eligibility_basis": "unnumbered_display_peer_break",
                    "relative_rank": "unnumbered_peer",
                    "current_owner_node_id": 1,
                    "target_node_id": None,
                    "boundary_carrier_scope": "selected_and_same_carrier",
                    "materialization_policy": "direct_target",
                    "flatten_subtree_root_node_id": None,
                }
            ],
        )

        numbered_parent = proof_for("二、财务报表")
        self.assertEqual(numbered_parent["owner_scope_breaks"], [])

        stacked_subtitle = proof_for(
            "合并所有者权益变动表",
            candidate_block=1,
        )
        self.assertEqual(stacked_subtitle["owner_scope_breaks"], [])

    def test_wrapped_tail_typed_as_title_is_rejected_by_its_block(self) -> None:
        # "票?" is the wrapped tail of the previous paragraph: both live
        # in one native block, so the provider title claim is a wrapped
        # sentence, not a heading.
        content = [
            {
                "type": "text",
                "text": "是否考虑回购股",
                "page_idx": 0,
                "bbox": [100, 80, 500, 105],
                "text_level": None,
            },
            {
                "type": "text",
                "text": "票?",
                "page_idx": 0,
                "bbox": [100, 110, 500, 135],
                "text_level": 1,
            },
        ]
        page = self._layout_page(
            [
                self._layout_atom(0, "是否考虑回购股", block=3, line=0, cx=90, cy=74),
                self._layout_atom(1, "票?", block=3, line=1, cx=90, cy=98),
            ]
        )
        proof, _content = _v2_structure_proof(
            native=native_index(page_count=1),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _paragraph_block("是否考虑回购股", [100, 80, 500, 105]),
                    _title_block("票?", [100, 110, 500, 135]),
                ]
            ],
            source_pages=(page,),
        )

        self.assertEqual(proof["headings"], [])
        self.assertIn(
            "provider_title_midflow",
            [conflict["relation"] for conflict in proof["conflicts"]],
        )

    def test_a_title_owning_its_block_still_opens_a_section(self) -> None:
        content = [
            {
                "type": "text",
                "text": "正文一段",
                "page_idx": 0,
                "bbox": [100, 80, 500, 105],
                "text_level": None,
            },
            {
                "type": "text",
                "text": "一、经营情况",
                "page_idx": 0,
                "bbox": [100, 110, 500, 135],
                "text_level": 1,
            },
        ]
        page = self._layout_page(
            [
                self._layout_atom(0, "正文一段", block=3, line=0, cx=90, cy=74),
                replace(
                    self._layout_atom(
                        1,
                        "一、经营情况",
                        block=4,
                        line=0,
                        cx=90,
                        cy=98,
                    ),
                    bbox=(80.0, 91.0, 100.0, 105.0),
                ),
                self._layout_atom(2, "后续正文", block=5, line=0, cx=90, cy=130),
                self._layout_atom(3, "更多正文", block=5, line=1, cx=90, cy=150),
            ]
        )
        proof, _content = _v2_structure_proof(
            native=native_index(page_count=1),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _paragraph_block("正文一段", [100, 80, 500, 105]),
                    _title_block("一、经营情况", [100, 110, 500, 135]),
                ]
            ],
            source_pages=(page,),
        )

        self.assertEqual(len(proof["headings"]), 1)
        self.assertEqual(
            proof["headings"][0]["evidence_kinds"],
            ["mineru_v2_title", "native_layout"],
        )

    def test_split_printed_title_lines_merge_into_one_heading(self) -> None:
        # A two-line centered document title typed as two adjacent
        # provider titles: one native block proves one printed title, and
        # the published heading joins both lines.
        content = [
            {
                "type": "text",
                "text": "财通证券股份有限公司",
                "page_idx": 0,
                "bbox": [300, 80, 700, 105],
                "text_level": 1,
            },
            {
                "type": "text",
                "text": "投资者关系活动记录表",
                "page_idx": 0,
                "bbox": [300, 110, 700, 135],
                "text_level": 1,
            },
            {
                "type": "text",
                "text": "正文内容",
                "page_idx": 0,
                "bbox": [100, 200, 500, 225],
                "text_level": None,
            },
        ]
        title_atoms = [
            self._layout_atom(
                0, "财通证券股份有限公司", block=1, line=0, cx=300, cy=74
            ),
            self._layout_atom(
                1, "投资者关系活动记录表", block=1, line=1, cx=300, cy=98
            ),
        ]
        title_atoms = [
            replace(
                atom,
                bbox=(
                    atom.bbox[0],
                    atom.bbox[1] - 3.0,
                    atom.bbox[2],
                    atom.bbox[3] + 3.0,
                ),
            )
            for atom in title_atoms
        ]
        page = self._layout_page(
            [
                *title_atoms,
                self._layout_atom(2, "正文内容", block=2, line=0, cx=90, cy=170),
                self._layout_atom(3, "邻近正文", block=2, line=1, cx=90, cy=210),
                self._layout_atom(4, "更多正文", block=2, line=2, cx=90, cy=250),
            ]
        )
        proof, _content = _v2_structure_proof(
            native=native_index(page_count=1),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _title_block("财通证券股份有限公司", [300, 80, 700, 105]),
                    _title_block("投资者关系活动记录表", [300, 110, 700, 135]),
                    _paragraph_block("正文内容", [100, 200, 500, 225]),
                ]
            ],
            source_pages=(page,),
        )

        self.assertEqual(len(proof["headings"]), 1)
        heading = proof["headings"][0]
        self.assertEqual(
            [ref["source_item_index"] for ref in heading["source_refs"]],
            [0, 1],
        )
        self.assertEqual(heading["section_span"], [0, 2])

    def test_page_front_title_absorbs_exact_display_paragraph_line(self) -> None:
        # The provider may type only the first line of a printed document
        # title. The adjacent paragraph line joins it only through exact,
        # centered page-front display geometry.
        content = [
            {
                "type": "text",
                "text": "财通证券股份有限公司",
                "page_idx": 0,
                "bbox": [300, 80, 700, 106],
                "text_level": 1,
            },
            {
                "type": "text",
                "text": "投资者关系活动记录表",
                "page_idx": 0,
                "bbox": [300, 110, 700, 136],
            },
            {
                "type": "text",
                "text": "正文内容甲乙丙丁戊己庚辛",
                "page_idx": 0,
                "bbox": [100, 200, 500, 218],
                "text_level": None,
            },
        ]
        atoms = [
            self._layout_atom(
                0, "财通证券股份有限公司", block=1, line=0, cx=300, cy=74
            ),
            self._layout_atom(
                1, "投资者关系活动记录表", block=2, line=0, cx=300, cy=152
            ),
        ]
        atoms = [
            replace(
                atoms[0],
                bbox=(atoms[0].bbox[0], 64.0, atoms[0].bbox[2], 84.0),
            ),
            replace(
                atoms[1],
                bbox=(atoms[1].bbox[0], 144.0, atoms[1].bbox[2], 160.0),
            ),
        ] + [
            self._layout_atom(
                2 + i, text, block=3, line=i, cx=90, cy=250 + i * 18
            )
            for i, text in enumerate(
                ("正文内容", "甲乙丙丁", "戊己庚辛", "更多正文")
            )
        ]
        page = self._layout_page(atoms)
        proof, _content = _v2_structure_proof(
            native=native_index(page_count=1),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _title_block("财通证券股份有限公司", [300, 80, 700, 106]),
                    _paragraph_block(
                        "投资者关系活动记录表",
                        [300, 110, 700, 136],
                    ),
                    _paragraph_block(
                        "正文内容甲乙丙丁戊己庚辛", [100, 200, 500, 218]
                    ),
                ]
            ],
            source_pages=(page,),
        )

        self.assertEqual(len(proof["headings"]), 1)
        self.assertEqual(
            [
                ref["source_item_index"]
                for ref in proof["headings"][0]["source_refs"]
            ],
            [0, 1],
        )

    def test_adjacent_titles_in_distinct_blocks_stay_separate(self) -> None:
        content = [
            {
                "type": "text",
                "text": "第一章总则",
                "page_idx": 0,
                "bbox": [100, 80, 500, 105],
                "text_level": 1,
            },
            {
                "type": "text",
                "text": "第二章财务",
                "page_idx": 0,
                "bbox": [100, 200, 500, 225],
                "text_level": 1,
            },
        ]
        page = self._layout_page(
            [
                replace(
                    self._layout_atom(
                        0, "第一章总则", block=1, line=0, cx=90, cy=74
                    ),
                    bbox=(80.0, 67.0, 100.0, 81.0),
                ),
                replace(
                    self._layout_atom(
                        1, "第二章财务", block=2, line=0, cx=90, cy=170
                    ),
                    bbox=(80.0, 163.0, 100.0, 177.0),
                ),
                self._layout_atom(2, "正文一", block=3, line=0, cx=90, cy=230),
                self._layout_atom(3, "正文二", block=3, line=1, cx=90, cy=250),
                self._layout_atom(4, "正文三", block=3, line=2, cx=90, cy=270),
            ]
        )
        proof, _content = _v2_structure_proof(
            native=native_index(page_count=1),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _title_block("第一章总则", [100, 80, 500, 105]),
                    _title_block("第二章财务", [100, 200, 500, 225]),
                ]
            ],
            source_pages=(page,),
        )

        self.assertEqual(len(proof["headings"]), 2)

    def test_cross_page_tail_is_rejected_but_numbered_heading_survives(
        self,
    ) -> None:
        content = [
            {
                "type": "text",
                "text": "是否考虑回购股",
                "page_idx": 0,
                "bbox": [100, 900, 700, 980],
                "text_level": None,
            },
            {
                "type": "text",
                "text": "票?",
                "page_idx": 1,
                "bbox": [100, 40, 300, 100],
                "text_level": 1,
            },
            {
                "type": "text",
                "text": "二、上年同期经营业绩和财务状况",
                "page_idx": 1,
                "bbox": [100, 150, 700, 210],
                "text_level": 1,
            },
        ]
        prior = replace(
            self._layout_atom(
                0,
                "是否考虑回购股",
                block=3,
                line=0,
                cx=240,
                cy=760,
                page_idx=0,
            ),
            bbox=(90.0, 756.0, 390.0, 764.0),
        )
        tail = self._layout_atom(
            0,
            "票?",
            block=1,
            line=0,
            cx=100,
            cy=60,
            page_idx=1,
        )
        major = replace(
            self._layout_atom(
                1,
                "二、上年同期经营业绩和财务状况",
                block=2,
                line=0,
                cx=190,
                cy=140,
                page_idx=1,
            ),
            bbox=(90.0, 133.0, 290.0, 147.0),
        )
        proof, _content = _v2_structure_proof(
            native=native_index(page_count=2),
            legacy_content_list=content,
            content_list_v2=[
                [_paragraph_block("是否考虑回购股", [100, 900, 700, 980])],
                [
                    _title_block("票?", [100, 40, 300, 100]),
                    _title_block(
                        "二、上年同期经营业绩和财务状况",
                        [100, 150, 700, 210],
                    ),
                ],
            ],
            source_pages=(
                self._layout_page([prior], page_idx=0),
                self._layout_page(
                    [
                        tail,
                        major,
                        self._layout_atom(
                            2,
                            "后续正文一",
                            block=3,
                            line=0,
                            cx=100,
                            cy=180,
                            page_idx=1,
                        ),
                        self._layout_atom(
                            3,
                            "后续正文二",
                            block=3,
                            line=1,
                            cx=100,
                            cy=200,
                            page_idx=1,
                        ),
                        self._layout_atom(
                            4,
                            "后续正文三",
                            block=3,
                            line=2,
                            cx=100,
                            cy=220,
                            page_idx=1,
                        ),
                    ],
                    page_idx=1,
                ),
            ),
        )

        self.assertEqual(len(proof["headings"]), 1)
        self.assertEqual(
            proof["headings"][0]["source_refs"][0]["source_item_index"],
            2,
        )
        self.assertIn(
            "provider_title_cross_page_continuation",
            [conflict["relation"] for conflict in proof["conflicts"]],
        )

    def test_multiline_numbered_heading_uses_complete_native_lines(
        self,
    ) -> None:
        title = "一、回购审批情况和回购方案内容"
        content = [
            {
                "type": "text",
                "text": title,
                "page_idx": 0,
                "bbox": [100, 100, 700, 180],
                "text_level": 1,
            }
        ]
        title_atoms = [
            replace(
                self._layout_atom(
                    0,
                    "一、回购审批情况",
                    block=1,
                    line=0,
                    cx=140,
                    cy=100,
                ),
                bbox=(149.0, 93.0, 169.0, 107.0),
            ),
            replace(
                self._layout_atom(
                    1,
                    "和回购方案内容",
                    block=2,
                    line=0,
                    cx=140,
                    cy=120,
                ),
                bbox=(120.0, 113.0, 140.0, 127.0),
            ),
        ]
        page = self._layout_page(
            [
                *title_atoms,
                self._layout_atom(2, "正文一", block=3, line=0, cx=90, cy=180),
                self._layout_atom(3, "正文二", block=3, line=1, cx=90, cy=200),
                self._layout_atom(4, "正文三", block=3, line=2, cx=90, cy=220),
            ]
        )
        proof, _content = _v2_structure_proof(
            native=native_index(page_count=1),
            legacy_content_list=content,
            content_list_v2=[
                [_title_block(title, [100, 100, 700, 180])]
            ],
            source_pages=(page,),
        )

        self.assertEqual(len(proof["headings"]), 1)
        self.assertEqual(
            proof["headings"][0]["evidence_kinds"],
            ["mineru_v2_title", "native_layout"],
        )

    def test_native_exact_atom_orders_override_an_overbroad_provider_bbox(
        self,
    ) -> None:
        content = [
            {
                "type": "text",
                "text": "公司文档标题",
                "page_idx": 0,
                "bbox": [100, 80, 900, 900],
                "text_level": 1,
            },
            {
                "type": "text",
                "text": "正文内容",
                "page_idx": 0,
                "bbox": [100, 300, 500, 340],
                "text_level": None,
            },
        ]
        title_atom = replace(
            self._layout_atom(
                0,
                "公司文档标题",
                block=1,
                line=0,
                cx=300,
                cy=90,
            ),
            bbox=(250.0, 83.0, 350.0, 97.0),
        )
        body_atoms = [
            self._layout_atom(
                index + 1,
                text,
                block=2,
                line=index,
                cx=120,
                cy=250 + index * 20,
            )
            for index, text in enumerate(
                ("正文内容", "第二行正文", "第三行正文")
            )
        ]
        page = self._layout_page([title_atom, *body_atoms])
        support = {
            (0, "text", None): CarrierSourceSupport(
                source_item_index=0,
                field="text",
                index=None,
                page_idx=0,
                bbox=(100.0, 80.0, 900.0, 900.0),
                kind="native_exact",
                source_atom_orders=(0,),
                artifact_role=None,
                artifact_sha256=None,
            ),
            (1, "text", None): CarrierSourceSupport(
                source_item_index=1,
                field="text",
                index=None,
                page_idx=0,
                bbox=(100.0, 300.0, 500.0, 340.0),
                kind="native_exact",
                source_atom_orders=(1,),
                artifact_role=None,
                artifact_sha256=None,
            ),
        }
        proof, _content = _v2_structure_proof(
            native=native_index(page_count=1),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _title_block("公司文档标题", [100, 80, 900, 900]),
                    _paragraph_block("正文内容", [100, 300, 500, 340]),
                ]
            ],
            source_pages=(page,),
            carrier_source_support=support,
        )

        self.assertEqual(len(proof["headings"]), 1)
        self.assertEqual(
            proof["headings"][0]["source_refs"][0]["source_item_index"],
            0,
        )

    def test_native_layout_requires_validated_exact_carrier_support(self) -> None:
        content = [
            {
                "type": "text",
                "text": "一、经营情况",
                "page_idx": 0,
                "bbox": [100, 80, 500, 110],
                "text_level": 1,
            }
        ]
        page = self._layout_page(
            [self._layout_atom(0, "一、经营情况", block=1, line=0, cx=90, cy=74)]
        )

        with self.assertRaises(ParserOutputContractError):
            _v2_structure_proof(
                native=native_index(page_count=1),
                legacy_content_list=content,
                content_list_v2=[
                    [_title_block("一、经营情况", [100, 80, 500, 110])]
                ],
                source_pages=(page,),
                carrier_source_support={},
            )

    def test_visual_bound_carrier_cannot_mint_native_layout_heading(self) -> None:
        content = [
            {
                "type": "text",
                "text": "一、经营情况",
                "page_idx": 0,
                "bbox": [100, 80, 500, 110],
                "text_level": 1,
            }
        ]
        page = self._layout_page(
            [self._layout_atom(0, "一、经营情况", block=1, line=0, cx=90, cy=74)]
        )
        support = {
            (0, "text", None): CarrierSourceSupport(
                source_item_index=0,
                field="text",
                index=None,
                page_idx=0,
                bbox=(100.0, 80.0, 500.0, 110.0),
                kind="visual_bound",
                source_atom_orders=(),
                artifact_role="test_visual_occurrence",
                artifact_sha256="sha256:" + "f" * 64,
            )
        }

        proof, _content = _v2_structure_proof(
            native=native_index(page_count=1),
            legacy_content_list=content,
            content_list_v2=[
                [_title_block("一、经营情况", [100, 80, 500, 110])]
            ],
            source_pages=(page,),
            carrier_source_support=support,
        )

        self.assertEqual(proof["headings"], [])

    def test_numbered_body_without_provider_title_is_flattened(self) -> None:
        content = [
            {
                "type": "text",
                "text": "普通正文",
                "page_idx": 0,
                "bbox": [100, 60, 500, 90],
            },
            {
                "type": "text",
                "text": "一、本段只是编号正文",
                "page_idx": 0,
                "bbox": [100, 100, 500, 130],
            },
        ]
        page = self._layout_page(
            [
                self._layout_atom(0, "普通正文", block=1, line=0, cx=90, cy=60),
                self._layout_atom(
                    1,
                    "一、本段只是编号正文",
                    block=2,
                    line=0,
                    cx=90,
                    cy=80,
                ),
                self._layout_atom(2, "后续正文一", block=3, line=0, cx=90, cy=100),
                self._layout_atom(3, "后续正文二", block=3, line=1, cx=90, cy=120),
                self._layout_atom(4, "后续正文三", block=3, line=2, cx=90, cy=140),
            ]
        )

        proof, _content = _v2_structure_proof(
            native=native_index(
                page_count=1,
                nodes=[native_node(1, "H1", [(0, 7)])],
                marked_objects=[
                    marked_object(
                        0,
                        7,
                        0,
                        text="一、本段只是编号正文",
                        bbox=[100, 100, 500, 130],
                    )
                ],
            ),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _paragraph_block("普通正文", [100, 60, 500, 90]),
                    _paragraph_block(
                        "一、本段只是编号正文",
                        [100, 100, 500, 130],
                    ),
                ]
            ],
            source_pages=(page,),
        )

        self.assertEqual(proof["headings"], [])

    def test_numbered_sentence_with_terminal_punctuation_is_flattened(
        self,
    ) -> None:
        text = "四、公司全体董事出席董事会会议。"
        content = [
            {
                "type": "text",
                "text": text,
                "page_idx": 0,
                "bbox": [100, 100, 500, 130],
                "text_level": 1,
            }
        ]
        page = self._layout_page(
            [self._layout_atom(0, text, block=2, line=0, cx=90, cy=80)]
        )

        proof, _content = _v2_structure_proof(
            native=native_index(
                page_count=1,
                nodes=[native_node(1, "H1", [(0, 7)])],
                marked_objects=[
                    marked_object(
                        0,
                        7,
                        0,
                        text=text,
                        bbox=[100, 100, 500, 130],
                    )
                ],
            ),
            legacy_content_list=content,
            content_list_v2=[[_title_block(text, [100, 100, 500, 130])]],
            source_pages=(page,),
            carrier_source_support={
                (0, "text", None): CarrierSourceSupport(
                    source_item_index=0,
                    field="text",
                    index=None,
                    page_idx=0,
                    bbox=(100.0, 100.0, 500.0, 130.0),
                    kind="native_exact",
                    source_atom_orders=(0,),
                    artifact_role=None,
                    artifact_sha256=None,
                )
            },
        )

        self.assertEqual(proof["headings"], [])
        self.assertIn(
            "provider_title_sentence_terminal",
            [conflict["relation"] for conflict in proof["conflicts"]],
        )

    def test_numbered_heading_ending_in_colon_remains_eligible(self) -> None:
        text = "一、重要事项："
        content = [
            {
                "type": "text",
                "text": text,
                "page_idx": 0,
                "bbox": [100, 100, 500, 130],
                "text_level": 1,
            }
        ]
        page = self._layout_page(
            [self._layout_atom(0, text, block=2, line=0, cx=90, cy=80)]
        )

        proof, _content = _v2_structure_proof(
            native=native_index(
                page_count=1,
                nodes=[native_node(1, "H1", [(0, 7)])],
                marked_objects=[
                    marked_object(
                        0,
                        7,
                        0,
                        text=text,
                        bbox=[100, 100, 500, 130],
                    )
                ],
            ),
            legacy_content_list=content,
            content_list_v2=[[_title_block(text, [100, 100, 500, 130])]],
            source_pages=(page,),
            carrier_source_support={
                (0, "text", None): CarrierSourceSupport(
                    source_item_index=0,
                    field="text",
                    index=None,
                    page_idx=0,
                    bbox=(100.0, 100.0, 500.0, 130.0),
                    kind="native_exact",
                    source_atom_orders=(0,),
                    artifact_role=None,
                    artifact_sha256=None,
                )
            },
        )

        self.assertEqual(len(proof["headings"]), 1)
        self.assertEqual(
            proof["headings"][0]["source_refs"][0]["source_item_index"],
            0,
        )

    def test_noncohesive_multiline_numbered_claim_is_flattened(self) -> None:
        title = "一、第一行第二行"
        content = [
            {
                "type": "text",
                "text": title,
                "page_idx": 0,
                "bbox": [100, 80, 500, 260],
                "text_level": 1,
            }
        ]
        page = self._layout_page(
            [
                self._layout_atom(0, "一、第一行", block=1, line=0, cx=90, cy=80),
                self._layout_atom(1, "无关正文", block=2, line=0, cx=90, cy=140),
                self._layout_atom(2, "第二行", block=3, line=0, cx=90, cy=220),
            ]
        )
        support = {
            (0, "text", None): CarrierSourceSupport(
                source_item_index=0,
                field="text",
                index=None,
                page_idx=0,
                bbox=(100.0, 80.0, 500.0, 260.0),
                kind="native_exact",
                source_atom_orders=(0, 2),
                artifact_role=None,
                artifact_sha256=None,
            )
        }

        proof, _content = _v2_structure_proof(
            native=native_index(page_count=1),
            legacy_content_list=content,
            content_list_v2=[[_title_block(title, [100, 80, 500, 260])]],
            source_pages=(page,),
            carrier_source_support=support,
        )

        self.assertEqual(proof["headings"], [])

    def test_table_role_vetoes_numbered_provider_title(self) -> None:
        text = "一、表内编号内容"
        content = [
            {
                "type": "text",
                "text": text,
                "page_idx": 0,
                "bbox": [100, 80, 500, 110],
                "text_level": 1,
            }
        ]
        page = self._layout_page(
            [
                replace(
                    self._layout_atom(0, text, block=1, line=0, cx=300, cy=74),
                    bbox=(220.0, 67.0, 380.0, 81.0),
                ),
                self._layout_atom(1, "正文一", block=2, line=0, cx=90, cy=140),
                self._layout_atom(2, "正文二", block=2, line=1, cx=90, cy=160),
                self._layout_atom(3, "正文三", block=2, line=2, cx=90, cy=180),
            ]
        )

        proof, _content = _v2_structure_proof(
            native=native_index(
                page_count=1,
                nodes=[native_node(1, "Table", [(0, 7)])],
                marked_objects=[
                    marked_object(
                        0,
                        7,
                        0,
                        text=text,
                        bbox=[100, 80, 500, 110],
                    )
                ],
            ),
            legacy_content_list=content,
            content_list_v2=[[_title_block(text, [100, 80, 500, 110])]],
            source_pages=(page,),
        )

        self.assertEqual(proof["headings"], [])

    def test_rendered_identity_survives_ambiguous_hierarchy_at_root(self) -> None:
        text = "第一章总则"
        content = [
            {
                "type": "text",
                "text": text,
                "page_idx": 0,
                "bbox": [100, 80, 500, 110],
                "text_level": 1,
            }
        ]
        page = self._layout_page(
            [
                replace(
                    self._layout_atom(0, text, block=1, line=0, cx=300, cy=74),
                    bbox=(220.0, 67.0, 380.0, 81.0),
                ),
                self._layout_atom(1, "正文一", block=2, line=0, cx=90, cy=140),
                self._layout_atom(2, "正文二", block=2, line=1, cx=90, cy=160),
                self._layout_atom(3, "正文三", block=2, line=2, cx=90, cy=180),
            ]
        )
        native = native_index(
            page_count=1,
            nodes=[
                native_node(1, "H1", [(0, 7)]),
                native_node(2, "H2", [(0, 7)], ancestor_node_ids=(1,)),
            ],
            marked_objects=[
                marked_object(
                    0,
                    7,
                    0,
                    text=text,
                    bbox=[100, 80, 500, 110],
                )
            ],
        )

        proof, projected_content = _v2_structure_proof(
            native=native,
            legacy_content_list=content,
            content_list_v2=[[_title_block(text, [100, 80, 500, 110])]],
            source_pages=(page,),
        )

        self.assertEqual(len(proof["headings"]), 1)
        heading = proof["headings"][0]
        self.assertEqual(heading["heading_level"], 1)
        self.assertIsNone(heading["parent_node_id"])
        self.assertNotIn("native_role", heading)
        self.assertNotIn("struct_tree", heading["evidence_kinds"])
        self.assertIn("native_layout", heading["evidence_kinds"])
        self.assertIn(
            "heading_hierarchy_flattened",
            [conflict["relation"] for conflict in proof["conflicts"]],
        )
        validate_document_structure(
            proof,
            elements=_structure_elements(projected_content),
        )

    def test_deeply_indented_numbered_provider_claim_is_suppressed(
        self,
    ) -> None:
        content = [
            {
                "type": "text",
                "text": "左侧正文",
                "page_idx": 0,
                "bbox": [100, 80, 500, 110],
                "text_level": None,
            },
            {
                "type": "text",
                "text": "3. 表格单元格中的一段文字",
                "page_idx": 0,
                "bbox": [350, 140, 800, 180],
                "text_level": 1,
            },
        ]
        page = self._layout_page(
            [
                self._layout_atom(
                    0,
                    "左侧正文",
                    block=1,
                    line=0,
                    cx=90,
                    cy=74,
                ),
                self._layout_atom(
                    1,
                    "3. 表格单元格中的一段文字",
                    block=2,
                    line=0,
                    cx=240,
                    cy=120,
                ),
            ]
        )
        proof, _content = _v2_structure_proof(
            native=native_index(page_count=1),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _paragraph_block("左侧正文", [100, 80, 500, 110]),
                    _title_block(
                        "3. 表格单元格中的一段文字",
                        [350, 140, 800, 180],
                    ),
                ]
            ],
            source_pages=(page,),
        )

        self.assertEqual(proof["headings"], [])
        self.assertIn(
            "provider_heading_unproved",
            [conflict["relation"] for conflict in proof["conflicts"]],
        )

    def test_printed_toc_corroborates_an_outline_only_heading(self) -> None:
        # An untagged, typeset document: the outline is the only lane,
        # but the printed TOC names the same title on its declared page —
        # the document itself is the second witness.
        from tests.unit.test_printed_toc import _atom, _page

        content = [
            {
                "type": "text",
                "text": "第一章总则",
                "page_idx": 2,
                "bbox": [100, 80, 300, 105],
                "text_level": None,
            },
            {
                "type": "text",
                "text": "第二章财务",
                "page_idx": 4,
                "bbox": [100, 80, 300, 105],
                "text_level": None,
            },
            {
                "type": "text",
                "text": "第三章治理",
                "page_idx": 6,
                "bbox": [100, 80, 300, 105],
                "text_level": None,
            },
        ]
        toc_page = _page(
            0,
            [
                _atom(0, 0, "第一章总则", line=0, x0=60, x1=240),
                _atom(0, 1, "." * 20, line=0, x0=245, x1=520),
                _atom(0, 2, "3", line=0, x0=540, x1=560),
                _atom(0, 3, "第二章财务", line=1, x0=60, x1=240),
                _atom(0, 4, "." * 20, line=1, x0=245, x1=520),
                _atom(0, 5, "5", line=1, x0=540, x1=560),
                _atom(0, 6, "第三章治理", line=2, x0=60, x1=240),
                _atom(0, 7, "." * 20, line=2, x0=245, x1=520),
                _atom(0, 8, "7", line=2, x0=540, x1=560),
            ],
        )
        proof, _content = _v2_structure_proof(
            native=native_index(
                page_count=7,
                bookmarks=[
                    native_bookmark(0, 1, "第一章总则", page_idx=2, destination_y=90),
                    native_bookmark(1, 1, "第二章财务", page_idx=4, destination_y=90),
                    native_bookmark(2, 1, "第三章治理", page_idx=6, destination_y=90),
                ],
            ),
            legacy_content_list=content,
            content_list_v2=[
                [_paragraph_block("第一章总则", [100, 80, 300, 105])],
                [],
                [_paragraph_block("第二章财务", [100, 80, 300, 105])],
                [],
                [_paragraph_block("第三章治理", [100, 80, 300, 105])],
            ],
            start_page=2,
            source_pages=(toc_page,),
        )

        self.assertEqual(
            [heading["evidence_kinds"] for heading in proof["headings"]],
            [["bookmark", "printed_toc"]] * 3,
        )
        self.assertEqual(
            [
                conflict["relation"]
                for conflict in proof["conflicts"]
                if conflict["relation"] == "bookmark_uncorroborated"
            ],
            [],
        )

    def test_struct_tree_corroborated_bookmarks_open_sections(self) -> None:
        content = [
            {
                "type": "text",
                "text": "第一章",
                "page_idx": 0,
                "bbox": [100, 80, 300, 105],
                "text_level": None,
            },
            {
                "type": "text",
                "text": "第二章",
                "page_idx": 0,
                "bbox": [100, 200, 300, 225],
                "text_level": None,
            },
        ]
        proof, _content = _v2_structure_proof(
            native=native_index(
                page_count=1,
                nodes=[
                    native_node(1, "H1", [(0, 7)]),
                    native_node(2, "H1", [(0, 8)]),
                ],
                marked_objects=[
                    marked_object(
                        0, 7, 0, text="第一章", bbox=[100, 80, 300, 105]
                    ),
                    marked_object(
                        0, 8, 1, text="第二章", bbox=[100, 200, 300, 225]
                    ),
                ],
                bookmarks=[
                    native_bookmark(0, 1, "第一章", destination_y=90),
                    native_bookmark(1, 1, "第二章", destination_y=210),
                    native_bookmark(2, 1, "对不上的书签", destination_y=90),
                ],
            ),
            legacy_content_list=content,
            content_list_v2=[
                [
                    _paragraph_block("第一章", [100, 80, 300, 105]),
                    _paragraph_block("第二章", [100, 200, 300, 225]),
                ]
            ],
        )

        self.assertEqual(
            [heading["evidence_kinds"] for heading in proof["headings"]],
            [
                ["bookmark", "native_layout", "struct_tree"],
                ["bookmark", "native_layout", "struct_tree"],
            ],
        )
        self.assertEqual(
            [
                conflict["relation"]
                for conflict in proof["conflicts"]
            ],
            ["bookmark_unaligned"],
        )

    def test_provider_title_cannot_promote_auxiliary_carriers_to_heading(
        self,
    ) -> None:
        content_items = [
            (
                "table_caption",
                {
                    "type": "table",
                    "page_idx": 0,
                    "img_path": "images/table.jpg",
                    "bbox": [100, 100, 900, 500],
                    "table_caption": ["单位：股"],
                    "table_body": ("<table><tr><td>股份总数</td></tr></table>"),
                    "table_footnote": [],
                },
            ),
            (
                "page_footnote",
                {
                    "type": "page_footnote",
                    "page_idx": 0,
                    "bbox": [100, 100, 900, 500],
                    "text": "单位：股",
                },
            ),
        ]
        for label, item in content_items:
            with self.subTest(label=label):
                with self.assertRaises(ParserOutputContractError):
                    _v2_structure_proof(
                        native=native_index(page_count=1),
                        legacy_content_list=[item],
                        content_list_v2=[
                            [
                                _title_block(
                                    "单位：股",
                                    [100, 100, 900, 500],
                                )
                            ]
                        ],
                    )

        duplicate = {
            "type": "text",
            "text": "重复 occurrence",
            "page_idx": 0,
            "bbox": [100, 100, 400, 140],
            "text_level": 1,
        }
        second = {**duplicate, "bbox": [100, 300, 400, 340]}
        repeated, _ = _v2_structure_proof(
            native=native_index(page_count=1),
            legacy_content_list=[duplicate, second],
            content_list_v2=[
                [
                    _title_block(
                        "重复 occurrence",
                        [100, 100, 400, 140],
                    ),
                    _title_block(
                        "重复 occurrence",
                        [100, 300, 400, 340],
                    ),
                ]
            ],
        )
        self.assertEqual(
            [
                heading["source_refs"][0]["source_item_index"]
                for heading in repeated["headings"]
            ],
            [0, 1],
        )

        with self.assertRaises(ParserOutputContractError):
            _v2_structure_proof(
                native=native_index(page_count=1),
                legacy_content_list=[
                    {
                        "type": "text",
                        "text": "坏 fragment",
                        "page_idx": 0,
                        "bbox": [100, 100, 400, 140],
                        "text_level": 1,
                    }
                ],
                content_list_v2=[
                    [
                        {
                            "type": "title",
                            "bbox": [100, 100, 400, 140],
                            "content": {
                                "level": 1,
                                "title_content": [
                                    {"type": "unsupported", "content": "坏"}
                                ],
                            },
                        }
                    ]
                ],
            )

        for label, content, block in (
            (
                "inline equation wrong text",
                [
                    {
                        "type": "text",
                        "text": "完全不同的 legacy 标题",
                        "page_idx": 0,
                        "bbox": [100, 100, 400, 140],
                        "text_level": 1,
                    }
                ],
                {
                    "type": "title",
                    "bbox": [100, 100, 400, 140],
                    "content": {
                        "level": 1,
                        "title_content": [
                            {"type": "text", "content": "占比 "},
                            {"type": "equation_inline", "content": "10\\%"},
                        ],
                    },
                },
            ),
            (
                "overlapping bbox",
                [
                    {
                        "type": "text",
                        "text": "相交但不是同一 occurrence",
                        "page_idx": 0,
                        "bbox": [100, 100, 400, 140],
                        "text_level": 1,
                    }
                ],
                _title_block(
                    "相交但不是同一 occurrence",
                    [390, 130, 700, 170],
                ),
            ),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ParserOutputContractError):
                    _v2_structure_proof(
                        native=native_index(page_count=1),
                        legacy_content_list=content,
                        content_list_v2=[[block]],
                    )

    def test_repeated_layout_inference_fails_closed_on_weak_or_ambiguous_evidence(
        self,
    ) -> None:
        mapper = MinerUToNormalizedIRMapper()
        content = [
            {
                "type": "text",
                "text": "可能是真实标题",
                "page_idx": page,
                "bbox": [100, 80, 300, 100],
                "text_level": 1,
            }
            for page in (0, 5, 10)
        ]
        content.extend(
            {
                "type": "text",
                "text": "仅重复两页",
                "page_idx": page,
                "bbox": [100, 80, 300, 100],
                "text_level": 1,
            }
            for page in (11, 12)
        )
        for page in (20, 21, 22):
            content.extend(
                [
                    {
                        "type": "text",
                        "text": "同页重复的业务状态",
                        "page_idx": page,
                        "bbox": [100, 145, 300, 165],
                        "text_level": 1,
                    },
                    {
                        "type": "text",
                        "text": "同页重复的业务状态",
                        "page_idx": page,
                        "bbox": [100, 400, 300, 420],
                        "text_level": 1,
                    },
                ]
            )

        normalized = mapper.map_content_list(
            content_list=content,
            parser_info=_parser_info(),
            document_metadata={"document_id": "doc_layout", "title": "样本"},
        )

        self.assertTrue(all(item["kind"] == "text" for item in normalized["elements"]))
        self.assertTrue(
            all(
                item["relation"] == "provider_heading_unproved"
                for item in normalized["structure_proof"]["conflicts"]
            )
        )

    def test_structures_rowspan_colspan_table_and_preserves_qa_cell_text(self) -> None:
        normalized = MinerUToNormalizedIRMapper().map_content_list(
            content_list=[
                {
                    "type": "table",
                    "page_idx": 0,
                    "img_path": "images/table.jpg",
                    "table_body": (
                        "<table>"
                        '<tr><td>问题</td><td colspan="2">回答</td></tr>'
                        '<tr><td rowspan="2">收入是否增长？</td><td>是</td><td>10%</td></tr>'
                        "<tr><td>原因</td><td>订单增加</td></tr>"
                        "</table>"
                    ),
                }
            ],
            parser_info=_parser_info(),
            document_metadata={
                "document_id": "doc_1",
                "source_pdf": "raw/doc.pdf",
                "title": "sample",
            },
        )

        element = normalized["elements"][0]
        table = element["table"]
        self.assertEqual(element["kind"], "table")
        self.assertEqual(element["raw_kind"], "table")
        # td-only tables carry no header evidence: the full grid stays in
        # rows and header promotion is the unit builder's business rule.
        self.assertEqual(table["headers"], [])
        self.assertEqual(table["rows"][0], ["问题", "回答", "回答"])
        self.assertEqual(table["rows"][1], ["收入是否增长？", "是", "10%"])
        self.assertIn("收入是否增长？", "".join("".join(row) for row in table["rows"]))
        self.assertEqual(
            table["merged_cells"],
            [
                {"row": 0, "col": 1, "rowspan": 1, "colspan": 2},
                {"row": 1, "col": 0, "rowspan": 2, "colspan": 1},
            ],
        )

    def test_mapper_preserves_adjacent_sparse_rows_exactly(self) -> None:
        content_list = [
            {
                "type": "table",
                "page_idx": 0,
                "img_path": "images/table.jpg",
                "table_body": (
                    "<table>"
                    "<tr><td>生产性生物资产</td><td></td></tr>"
                    "<tr><td>油气资产</td><td></td></tr>"
                    "</table>"
                ),
            }
        ]
        mapper = MinerUToNormalizedIRMapper()
        default = mapper.map_content_list(
            content_list=content_list,
            parser_info=_parser_info(),
            document_metadata={"document_id": "doc_sparse_default"},
        )
        self.assertEqual(
            default["elements"][0]["table"]["rows"],
            [["生产性生物资产", ""], ["油气资产", ""]],
        )

    def test_nonempty_html_without_cells_fails_loud(self) -> None:
        with self.assertRaisesRegex(
            ParserOutputContractError,
            "no valid logical cells",
        ):
            MinerUToNormalizedIRMapper().map_content_list(
                content_list=[
                    {
                        "type": "table",
                        "page_idx": 0,
                        "img_path": "images/table.jpg",
                        "table_body": "<div>不是表格的载体</div>",
                    }
                ],
                parser_info=_parser_info(),
                document_metadata={
                    "document_id": "doc_bad_table",
                    "source_pdf": "raw/doc.pdf",
                    "title": "sample",
                },
            )

    def test_maps_neutral_kinds_and_structured_tables(self) -> None:
        mapper = MinerUToNormalizedIRMapper()
        normalized = mapper.map_content_list(
            content_list=[
                {
                    "type": "text",
                    "text": "一、标题",
                    "page_idx": 0,
                    "bbox": [1, 2, 3, 4],
                    "text_level": 1,
                },
                {
                    "type": "text",
                    "text": "正文",
                    "text_level": 0,
                    "page_idx": 0,
                    "bbox": [1, 5, 3, 6],
                },
                {
                    "type": "page_number",
                    "text": "1 / 2",
                    "page_idx": 0,
                    "bbox": [1, 7, 3, 8],
                },
                {
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [1, 2, 3, 4],
                    "table_caption": ["表 1"],
                    "table_footnote": ["注"],
                    "table_body": (
                        '<table><tr><th rowspan="2">项目</th><th>金额</th></tr>'
                        "<tr><td>10</td></tr></table>"
                    ),
                    "img_path": "images/a.jpg",
                },
                {
                    "type": "equation",
                    "text": "E=mc^2",
                    "page_idx": 1,
                    "bbox": [1, 5, 3, 6],
                },
                {
                    "type": "aside_text",
                    "text": "补充说明",
                    "page_idx": 1,
                    "bbox": [1, 7, 3, 8],
                },
                {
                    "type": "page_footnote",
                    "text": "定义：口径说明",
                    "page_idx": 1,
                    "bbox": [1, 9, 3, 10],
                },
                {
                    "type": "chart",
                    "page_idx": 1,
                    "bbox": [1, 11, 3, 12],
                    "img_path": "images/chart.jpg",
                    "content": "| 指标 | 数值 |\n| --- | --- |\n| 收入 | 10 |",
                    "chart_caption": ["收入结构", "按期末数"],
                    "chart_footnote": ["注：未经审计"],
                    "sub_type": "bar",
                },
                {
                    "type": "code",
                    "sub_type": "algorithm",
                    "code_caption": ["算法 1"],
                    "code_body": "```python\nif ready:\n    run()\n\nreturn result\n```",
                    "code_footnote": ["保持缩进"],
                    "page_idx": 1,
                    "bbox": [1, 13, 3, 14],
                },
            ],
            parser_info=MinerUParserInfo(
                name="MinerU",
                package_version="3.4.0",
                backend="pipeline",
                method="auto",
                language="ch",
                formula=False,
                table=True,
                runtime_bundle_identity_sha256="sha256:" + "b" * 64,
            ),
            document_metadata={
                "document_id": "doc_01K0000000000000000000000",
                "source_pdf": "raw_documents/local/sample.pdf",
                "title": "sample",
            },
            parser_artifacts=_artifact_manifest(
                "parser_artifacts/sample",
                "sample.json",
                image_paths={
                    3: "images/a.jpg",
                    7: "images/chart.jpg",
                },
            ),
        )
        _attach_closed_table_diagnostics(normalized)
        validate_current_normalized_ir_for_write(normalized)
        self.assertEqual(normalized["contract_version"], "normalized_ir.v4")
        self.assertEqual(normalized["parsed_pages"]["start_page_no"], 1)
        self.assertEqual(normalized["parsed_pages"]["end_page_no"], 2)
        self.assertEqual(
            [item["kind"] for item in normalized["elements"]],
            [
                "text",
                "text",
                "page_furniture",
                "table",
                "equation",
                "text",
                "text",
                "image",
                "text",
            ],
        )
        self.assertEqual(normalized["elements"][0]["raw_kind"], "text")
        self.assertEqual(normalized["elements"][0]["text_level"], 1)
        self.assertEqual(normalized["elements"][2]["raw_kind"], "page_number")
        self.assertEqual(
            normalized["elements"][3]["table"]["headers"], ["项目", "金额"]
        )
        self.assertEqual(normalized["elements"][3]["table"]["rows"], [["项目", "10"]])
        self.assertEqual(
            normalized["elements"][3]["table"]["merged_cells"],
            [{"row": 0, "col": 0, "rowspan": 2, "colspan": 1}],
        )
        visual = normalized["elements"][7]
        self.assertEqual(visual["raw_kind"], "chart")
        self.assertEqual(visual["text_provenance"], "visual_recognition")
        self.assertEqual(
            visual["text"], "| 指标 | 数值 |\n| --- | --- |\n| 收入 | 10 |"
        )
        self.assertEqual(visual["image_caption"], ["收入结构", "按期末数"])
        self.assertEqual(visual["image_footnote"], ["注：未经审计"])
        self.assertEqual(visual["visual_subtype"], "bar")
        visual["text_provenance"] = "generated_annotation"
        with self.assertRaises(NormalizedIRVersionError) as raised:
            validate_current_normalized_ir_for_write(normalized)
        self.assertEqual(
            raised.exception.reason_code,
            "element_image_text_provenance_invalid",
        )
        visual["text_provenance"] = "visual_recognition"
        code = normalized["elements"][8]
        self.assertEqual(code["raw_kind"], "code")
        self.assertEqual(code["code_subtype"], "algorithm")
        self.assertEqual(code["code_caption"], ["算法 1"])
        self.assertEqual(code["code_footnote"], ["保持缩进"])
        self.assertEqual(
            code["text"],
            "算法 1\n```python\nif ready:\n    run()\n\nreturn result\n```\n保持缩进",
        )
        self.assertRegex(
            code["source_item_sha256"],
            r"^sha256:[a-f0-9]{64}$",
        )

    def test_empty_chart_has_no_invented_text_provenance(self) -> None:
        content_list = [
            {
                "type": "chart",
                "page_idx": 0,
                "bbox": [10, 10, 500, 500],
                "img_path": "images/chart.jpg",
                "chart_caption": [],
                "chart_footnote": [],
            }
        ]

        normalized = MinerUToNormalizedIRMapper().map_content_list(
            content_list=content_list,
            parser_info=_parser_info(),
            document_metadata={
                "document_id": "doc_empty_chart",
                "source_pdf": "raw/doc_empty_chart.pdf",
            },
            parser_artifacts=_artifact_manifest(
                "parser/doc_empty_chart",
                "content.json",
                image_paths={0: "images/chart.jpg"},
            ),
        )
        _attach_closed_table_diagnostics(normalized)

        validate_current_normalized_ir_for_write(normalized)
        self.assertNotIn("text", normalized["elements"][0])
        self.assertNotIn("text_provenance", normalized["elements"][0])
        json.dumps(normalized, ensure_ascii=False)

    def test_typed_list_and_code_payloads_fail_loud_on_malformed_shapes(self) -> None:
        mapper = MinerUToNormalizedIRMapper()
        normalized = mapper.map_content_list(
            content_list=[
                {
                    "type": "list",
                    "sub_type": "text",
                    "list_items": ["1、第一项", "", "2、第二项"],
                    "page_idx": 0,
                },
                {"type": "list", "list_items": [], "page_idx": 0},
                {"type": "list", "list_items": ["  ", "\t"], "page_idx": 0},
            ],
            parser_info=MinerUParserInfo(
                name="MinerU",
                package_version="3.4.0",
                backend="pipeline",
                method="auto",
                language="ch",
                formula=False,
                table=True,
                runtime_bundle_identity_sha256="sha256:" + "b" * 64,
            ),
            document_metadata={
                "document_id": "doc_01K0000000000000000000000",
                "source_pdf": "raw_documents/local/sample.pdf",
                "title": "sample",
            },
        )

        self.assertEqual(
            [element["kind"] for element in normalized["elements"]],
            ["text", "text", "text"],
        )
        self.assertEqual(normalized["elements"][0]["raw_kind"], "list")
        self.assertEqual(
            normalized["elements"][0]["text"],
            "1、第一项\n\n2、第二项",
        )
        self.assertTrue(
            all("text" not in element for element in normalized["elements"][1:])
        )
        self.assertEqual(normalized["elements"][0]["list_subtype"], "text")

        malformed_items = (
            {
                "type": "list",
                "list_items": ["可读项", {"text": "非稳定嵌套形状"}],
            },
            {"type": "code", "code_caption": []},
            {"type": "code", "code_body": 7, "code_caption": []},
            {"type": "mystery", "algorithm_body": "不可静默丢弃"},
            {
                "type": "chart",
                "img_path": "images/chart.jpg",
                "chart_caption": ["合法", {"text": "非字符串"}],
            },
            {
                "type": "chart",
                "img_path": "images/chart.jpg",
                "chart_footnote": {"text": "非数组"},
            },
        )
        for item in malformed_items:
            with self.subTest(item=item):
                with self.assertRaises(ParserOutputContractError):
                    mapper.map_content_list(
                        content_list=[{**item, "page_idx": 0}],
                        parser_info=_parser_info(),
                        document_metadata={"document_id": "doc_bad_typed_payload"},
                    )

        for field, value in (
            ("code_caption", ["合法", {"text": "非字符串"}]),
            ("code_footnote", {"text": "非数组"}),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ParserOutputContractError):
                    mapper.map_content_list(
                        content_list=[
                            {
                                "type": "code",
                                "page_idx": 0,
                                "code_body": "return 1",
                                field: value,
                            }
                        ],
                        parser_info=_parser_info(),
                        document_metadata={"document_id": "doc_bad_visual"},
                    )

    def test_aggregate_table_locator_is_rejected_as_provider_payload(self) -> None:
        with self.assertRaisesRegex(
            ParserOutputContractError,
            "unmapped payload fields",
        ):
            MinerUToNormalizedIRMapper().map_content_list(
                content_list=[
                    {
                        "type": "table",
                        "page_idx": 0,
                        "bbox": [100, 100, 900, 900],
                        "img_path": "images/table.jpg",
                        "table_body": "<table><tr><td>A</td></tr></table>",
                        "_mineru_aggregate_table_locator": {"stale": True},
                    }
                ],
                parser_info=_parser_info(),
                document_metadata={"document_id": "doc_stale_locator"},
            )


class MinerUDocumentParserTests(unittest.TestCase):
    def test_parser_registers_each_visual_occurrence_crop(self) -> None:
        class SuccessfulProcess:
            def run(
                self, *, input_pdf: Path, output_dir: Path, options: ParserOptions
            ) -> None:
                nested = output_dir / "sample" / "auto"
                images = nested / "images"
                images.mkdir(parents=True)
                (images / "figure.png").write_bytes(b"provider-figure")
                (nested / "sample_content_list.json").write_text(
                    json.dumps(
                        [
                            {
                                "type": "image",
                                "page_idx": 0,
                                "bbox": [100, 100, 500, 500],
                                "img_path": "images/figure.png",
                                "image_caption": [],
                                "image_footnote": [],
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                (nested / "sample_content_list_v2.json").write_text(
                    json.dumps(
                        [
                            [
                                {
                                    "type": "image",
                                    "bbox": [100, 100, 500, 500],
                                    "content": {
                                        "image_caption": [],
                                        "image_footnote": [],
                                    },
                                }
                            ]
                        ]
                    ),
                    encoding="utf-8",
                )
                (nested / "sample_model.json").write_text("[]", encoding="utf-8")
                (nested / "sample_middle.json").write_text(
                    json.dumps(
                        {
                            "_backend": "pipeline",
                            "_version_name": "3.4.0",
                            "pdf_info": [
                                {
                                    "page_idx": 0,
                                    "page_size": [1000, 1000],
                                    "preproc_blocks": [],
                                    "para_blocks": [],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            def version(self) -> str:
                return "3.4.0"

            def run_runtime_helper(
                self,
                *,
                script: Path,
                input_payload: str,
                options: ParserOptions,
            ) -> mineru_process.MinerURuntimeHelperResult:
                request = json.loads(input_payload)
                return mineru_process.MinerURuntimeHelperResult(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "mineru_vl_utils_version": "1.0.5",
                            "outputs": [
                                {"item_id": item["item_id"], "text": ""}
                                for item in request["items"]
                            ],
                        }
                    ),
                    stderr="",
                )

        def render_plan(
            pdf_path: Path,
            expected_pdf_sha256: str,
            *,
            full_pages: Any,
            regions: Any,
            occurrences: Any,
            artifact_dir: Path,
        ) -> RenderedVisualEvidence:
            self.assertEqual(tuple(full_pages), ())
            self.assertEqual(tuple(regions), ())
            self.assertEqual(len(occurrences), 1)
            request = occurrences[0]
            self.assertEqual(request.source_item_index, 0)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            target = artifact_dir / "source_visual_occurrence_000000.png"
            payload = b"source-pdf-occurrence"
            target.write_bytes(payload)
            return RenderedVisualEvidence(
                pages=(),
                regions=(),
                occurrences=(
                    VisualPageEvidence(
                        page_idx=0,
                        artifact_role="source_visual_occurrence_000000",
                        artifact_path=target,
                        sha256=("sha256:" + hashlib.sha256(payload).hexdigest()),
                        size_bytes=len(payload),
                        pixel_width=100,
                        pixel_height=100,
                        media_type="image/png",
                        renderer=RENDERER_IDENTITY,
                        render_options=RENDER_OPTIONS,
                        png_options=PNG_OPTIONS,
                        bbox=(100.0, 100.0, 500.0, 500.0),
                    ),
                ),
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            pdf_bytes = b"%PDF-1.4\nsample\n%%EOF\n"
            input_pdf.write_bytes(pdf_bytes)
            raw_file_hash = "sha256:" + hashlib.sha256(pdf_bytes).hexdigest()
            with (
                mock.patch(
                    "disclosure_anchor.adapters.parsers.mineru."
                    "existing_artifact_pipeline.extract_pdf_structure",
                    return_value=_untagged_native(
                        1,
                        source_pdf_sha256=raw_file_hash,
                    ),
                ),
                mock.patch(
                    "disclosure_anchor.adapters.parsers.mineru."
                    "existing_artifact_pipeline.extract_native_pages",
                    return_value=_native_text_fixture([["nearby text"]]),
                ),
                mock.patch(
                    "disclosure_anchor.adapters.parsers.mineru."
                    "existing_artifact_pipeline.render_pdf_visual_evidence",
                    side_effect=render_plan,
                ),
            ):
                result = MinerUDocumentParser(
                    process=SuccessfulProcess(),
                    parser_version="3.4.0",
                    server_url="http://fixture",
                ).parse(
                    input_pdf=input_pdf,
                    output_dir=root / "out",
                    options=ParserOptions(
                        runtime_bundle_identity_sha256="sha256:" + "b" * 64
                    ),
                    document_metadata={
                        "document_id": "doc_visual_occurrence",
                        "source_pdf": "raw/doc_visual_occurrence.pdf",
                        "raw_file_hash": raw_file_hash,
                        "title": "视觉样本",
                    },
                )

            role = "source_visual_occurrence_000000"
            self.assertIn(role, result.artifact_paths)
            self.assertTrue(result.artifact_paths[role].is_file())
            source_evidence = json.loads(
                result.artifact_paths["source_evidence"].read_text(encoding="utf-8")
            )
            self.assertEqual(
                source_evidence["visual_occurrences"][0]["artifact"]["artifact_role"],
                role,
            )
            self.assertEqual(
                result.normalized_ir["elements"][0]["raw_kind"],
                "image",
            )

    def test_parser_publishes_only_closed_page_local_tables(self) -> None:
        first = "<table><tr><td>A</td></tr></table>"
        second = "<table><tr><td>B</td></tr></table>"

        class SuccessfulProcess:
            def run(
                self, *, input_pdf: Path, output_dir: Path, options: ParserOptions
            ) -> None:
                nested = output_dir / "sample" / "auto"
                nested.mkdir(parents=True)
                images = nested / "images"
                images.mkdir()
                (images / "first.jpg").write_bytes(b"first")
                (images / "second.jpg").write_bytes(b"second")
                content = [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "bbox": [100, 700, 900, 900],
                        "img_path": "images/first.jpg",
                        "table_body": first,
                        "table_caption": [],
                        "table_footnote": [],
                    },
                    {
                        "type": "table",
                        "page_idx": 1,
                        "bbox": [100, 100, 900, 300],
                        "img_path": "images/second.jpg",
                        "table_body": second,
                        "table_caption": [],
                        "table_footnote": [],
                    },
                ]
                model = [
                    {
                        "page_info": {
                            "page_no": 0,
                            "width": 1000,
                            "height": 1000,
                        },
                        "layout_dets": [
                            {
                                "label": "table",
                                "bbox": [100, 700, 900, 900],
                                "html": first,
                            }
                        ],
                    },
                    {
                        "page_info": {
                            "page_no": 1,
                            "width": 1000,
                            "height": 1000,
                        },
                        "layout_dets": [
                            {
                                "label": "table",
                                "bbox": [100, 100, 900, 300],
                                "html": second,
                            }
                        ],
                    },
                ]
                (nested / "sample_content_list.json").write_text(
                    json.dumps(content), encoding="utf-8"
                )
                (nested / "sample_content_list_v2.json").write_text(
                    json.dumps(
                        [
                            [
                                {
                                    "type": "table",
                                    "bbox": [100, 700, 900, 900],
                                    "content": {
                                        "table_caption": [],
                                        "table_footnote": [],
                                    },
                                }
                            ],
                            [
                                {
                                    "type": "table",
                                    "bbox": [100, 100, 900, 300],
                                    "content": {
                                        "table_caption": [],
                                        "table_footnote": [],
                                    },
                                }
                            ],
                        ]
                    ),
                    encoding="utf-8",
                )
                (nested / "sample_model.json").write_text(
                    json.dumps(model), encoding="utf-8"
                )
                (nested / "sample_middle.json").write_text(
                    json.dumps(
                        {
                            "_backend": "pipeline",
                            "_version_name": "3.4.0",
                            "pdf_info": [
                                {
                                    "page_idx": page_idx,
                                    "page_size": [1000, 1000],
                                    "preproc_blocks": [],
                                    "para_blocks": [],
                                }
                                for page_idx in range(2)
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            def version(self) -> str:
                return "3.4.0"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            pdf_bytes = b"%PDF-1.4\nsample\n%%EOF\n"
            input_pdf.write_bytes(pdf_bytes)
            raw_file_hash = "sha256:" + hashlib.sha256(pdf_bytes).hexdigest()
            with (
                mock.patch(
                    "disclosure_anchor.adapters.parsers.mineru."
                    "existing_artifact_pipeline."
                    "extract_pdf_structure",
                    return_value=_untagged_native(
                        2,
                        source_pdf_sha256=raw_file_hash,
                    ),
                ),
                mock.patch(
                    "disclosure_anchor.adapters.parsers.mineru."
                    "existing_artifact_pipeline."
                    "extract_native_pages",
                    return_value=_native_text_fixture(
                        [["A"], ["B"]],
                        bboxes=[
                            [(200.0, 720.0, 300.0, 760.0)],
                            [(200.0, 120.0, 300.0, 160.0)],
                        ],
                    ),
                ),
            ):
                result = MinerUDocumentParser(
                    process=SuccessfulProcess(), parser_version="3.4.0"
                ).parse(
                    input_pdf=input_pdf,
                    output_dir=root / "out",
                    options=ParserOptions(
                        runtime_bundle_identity_sha256="sha256:" + "b" * 64
                    ),
                    document_metadata={
                        "document_id": "doc_table_reconcile",
                        "source_pdf": "raw_documents/local/sample.pdf",
                        "raw_file_hash": raw_file_hash,
                        "title": "普通公告",
                    },
                )

        self.assertEqual(
            [element["table"]["rows"] for element in result.normalized_ir["elements"]],
            [[["A"]], [["B"]]],
        )
        first_element, second_element = result.normalized_ir["elements"]
        self.assertEqual(
            [first_element["table_html"], second_element["table_html"]],
            [first, second],
        )
        self.assertNotIn("table_locator_algorithm", first_element)
        self.assertNotIn("page_span", first_element)
        diagnostics = result.normalized_ir["parser_diagnostics"]["table_reconciliation"]
        self.assertEqual(
            diagnostics["algorithm_version"],
            "mineru-page-local-table-closure.v6",
        )
        self.assertEqual(diagnostics["content_tables"], 2)
        self.assertEqual(diagnostics["model_tables"], 2)
        self.assertEqual(diagnostics["matched_tables"], 2)
        self.assertTrue(diagnostics["page_local_closed"])
        self.assertRegex(diagnostics["model_hash"], r"^sha256:[a-f0-9]{64}$")
        self.assertIsNotNone(result.artifact_paths["model"])
        self.assertEqual(result.artifact_paths["model"].name, "sample_model.json")

    def test_reconciliation_diagnostics_preserve_other_parser_diagnostics(
        self,
    ) -> None:
        mapper = mock.Mock(spec=MinerUToNormalizedIRMapper)
        mapper.map_content_list.return_value = {
            "contract_version": "normalized_ir.v4",
            "created_at": "2026-07-16T00:00:00Z",
            "document_id": "doc_diagnostics",
            "source_pdf": "raw/sample.pdf",
            "title": "sample",
            "parser": {},
            "parser_artifacts": {},
            "parsed_pages": {
                "start_page_no": None,
                "end_page_no": None,
                "full_pdf": True,
            },
            "elements": [],
            "parser_diagnostics": {"future_probe": {"status": "ok"}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "model.json"
            model_path.write_text("[]", encoding="utf-8")
            normalized, reconciliation = map_reconciled_mineru_content_list(
                content_list=[],
                model_path=model_path,
                mapper=mapper,
                parser_info=MinerUParserInfo(
                    name="MinerU",
                    package_version="3.4.0",
                    backend="pipeline",
                    method="auto",
                    language="ch",
                    formula=False,
                    table=True,
                    runtime_bundle_identity_sha256="sha256:" + "b" * 64,
                ),
                document_metadata={
                    "document_id": "doc_diagnostics",
                    "source_pdf": "raw/sample.pdf",
                    "title": "sample",
                },
                structure_proof=_untagged_proof([]),
                source_pdf_sha256="sha256:" + "a" * 64,
                source_pdf_page_count=1,
                registered_evidence_image_paths={},
            )

        self.assertEqual(
            normalized["parser_diagnostics"]["future_probe"],
            {"status": "ok"},
        )
        self.assertEqual(
            normalized["parser_diagnostics"]["table_reconciliation"][
                "page_local_closed"
            ],
            True,
        )
        self.assertTrue(reconciliation.stats.page_local_closed)

    def test_reconciliation_contract_reason_is_preserved_in_error(
        self,
    ) -> None:
        mapper = mock.Mock(spec=MinerUToNormalizedIRMapper)
        mapper.map_content_list.return_value = {
            "contract_version": "normalized_ir.v4",
            "created_at": "2026-07-16T00:00:00Z",
            "document_id": "doc_bad_locator",
            "source_pdf": "raw/sample.pdf",
            "title": "sample",
            "parser": {},
            "parser_artifacts": {},
            "parsed_pages": {
                "start_page_no": None,
                "end_page_no": None,
                "full_pdf": True,
            },
            "elements": [],
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "disclosure_anchor.adapters.parsers.mineru."
                "existing_artifact_pipeline."
                "validate_table_reconciliation_payload",
                side_effect=TableReconciliationContractError(
                    "page_local_table_grid", "grid missing"
                ),
            ),
        ):
            model_path = Path(tmp) / "model.json"
            model_path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ParserOutputContractError) as caught:
                map_reconciled_mineru_content_list(
                    content_list=[],
                    model_path=model_path,
                    mapper=mapper,
                    parser_info=_parser_info(),
                    document_metadata={
                        "document_id": "doc_bad_locator",
                        "source_pdf": "raw/sample.pdf",
                        "title": "sample",
                    },
                    structure_proof=_untagged_proof([]),
                    source_pdf_sha256="sha256:" + "a" * 64,
                    source_pdf_page_count=1,
                    registered_evidence_image_paths={},
                )

        self.assertIn("page_local_table_grid", str(caught.exception))
        self.assertIn("grid missing", str(caught.exception))

    def test_successful_parse_does_not_probe_remote_readiness(self) -> None:
        class SuccessfulProcess:
            def __init__(self) -> None:
                self.probe_calls = 0

            def run(self, *, input_pdf: Path, output_dir: Path, options: ParserOptions):
                nested = output_dir / "sample" / "auto"
                nested.mkdir(parents=True)
                (nested / "sample_content_list.json").write_text(
                    (
                        '[{"type":"text","text":"hello","page_idx":0,'
                        '"bbox":[0,0,200,200]}]'
                    ),
                    encoding="utf-8",
                )
                (nested / "sample_model.json").write_text("[]", encoding="utf-8")
                (nested / "sample_content_list_v2.json").write_text(
                    json.dumps(
                        [
                            [
                                {
                                    "type": "paragraph",
                                    "bbox": [0, 0, 200, 200],
                                    "content": {
                                        "paragraph_content": [
                                            {
                                                "type": "text",
                                                "content": "hello",
                                            }
                                        ]
                                    },
                                }
                            ]
                        ]
                    ),
                    encoding="utf-8",
                )
                (nested / "sample_middle.json").write_text(
                    json.dumps(
                        {
                            "_backend": "pipeline",
                            "_version_name": "3.4.0",
                            "pdf_info": [
                                {
                                    "page_idx": 0,
                                    "page_size": [1000, 1000],
                                    "preproc_blocks": [],
                                    "para_blocks": [],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            def version(self) -> str:
                return "3.4.0"

            def probe_server(self, server_url: str) -> None:
                self.probe_calls += 1
                raise ParserVersionProbeError(f"backend unavailable: {server_url}")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            pdf_bytes = b"%PDF-1.4\nsample\n%%EOF\n"
            input_pdf.write_bytes(pdf_bytes)
            raw_file_hash = "sha256:" + hashlib.sha256(pdf_bytes).hexdigest()
            process = SuccessfulProcess()
            parser = MinerUDocumentParser(
                process=process,
                server_url="http://gpu:30000",
            )
            with (
                mock.patch(
                    "disclosure_anchor.adapters.parsers.mineru."
                    "existing_artifact_pipeline."
                    "extract_pdf_structure",
                    return_value=_untagged_native(
                        1,
                        source_pdf_sha256=raw_file_hash,
                    ),
                ),
                mock.patch(
                    "disclosure_anchor.adapters.parsers.mineru."
                    "existing_artifact_pipeline."
                    "extract_native_pages",
                    return_value=_native_text_fixture([["hello"]]),
                ),
            ):
                result = parser.parse(
                    input_pdf=input_pdf,
                    output_dir=root / "out",
                    options=ParserOptions(
                        runtime_bundle_identity_sha256="sha256:" + "b" * 64
                    ),
                    document_metadata={
                        "document_id": "doc_01K0000000000000000000000",
                        "source_pdf": "raw_documents/local/sample.pdf",
                        "raw_file_hash": raw_file_hash,
                        "title": "sample",
                    },
                )

            self.assertEqual(result.target_identity.package_version, "3.4.0")
            self.assertEqual(process.probe_calls, 0)
            with self.assertRaises(ParserVersionProbeError):
                parser.readiness()
            self.assertEqual(process.probe_calls, 1)


class OwnerScopeMaterializationTests(unittest.TestCase):
    """Producer-side policy derivation over the raw provider stream.

    The full native-layout path already proves break emission; these cases
    pin the materialization decision itself: when a proven non-root target
    stays contiguous, when it must flatten its one intervening subtree, and
    when no bounded flatten can close it.
    """

    @staticmethod
    def _heading(
        node_id: int,
        source_index: int,
        *,
        text: str,
        section_end: int,
        parent_node_id: int | None = None,
    ) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "parent_node_id": parent_node_id,
            "propagates": True,
            "section_span": [source_index, section_end],
            "source_refs": [
                {
                    "source_item_index": source_index,
                    "field": "text",
                    "text_span": [0, len(text)],
                }
            ],
        }

    @staticmethod
    def _case(
        *,
        with_intro: bool,
        trailing_sibling: bool = False,
    ) -> tuple[
        list[dict[str, Any]],
        dict[int, dict[str, Any]],
        list[dict[str, Any]],
    ]:
        content_list: list[dict[str, Any]] = [
            {"type": "text", "text": "第十节 财务报告"},
            {"type": "text", "text": "顶层引言" if with_intro else " "},
            {"type": "text", "text": "一、旧一级"},
            {"type": "text", "text": "（一）旧二级"},
            {"type": "text", "text": "旧二级正文"},
            {
                "type": "table",
                "table_caption": ["二、新同级"],
                "table_footnote": [],
                "table_body": "<table><tr><td>值</td></tr></table>",
            },
            {"type": "text", "text": "新同级后续正文"},
        ]
        section_end = 6
        if trailing_sibling:
            content_list.extend(
                [
                    {"type": "text", "text": "三、后置一级"},
                    {"type": "text", "text": "后置一级正文"},
                    {"type": "text", "text": "回到第十节的正文"},
                ]
            )
            section_end = 9
        make = OwnerScopeMaterializationTests._heading
        headings = {
            1: make(1, 0, text="第十节 财务报告", section_end=section_end),
            2: make(2, 2, text="一、旧一级", section_end=6, parent_node_id=1),
            3: make(3, 3, text="（一）旧二级", section_end=6, parent_node_id=2),
        }
        if trailing_sibling:
            headings[4] = make(
                4, 7, text="三、后置一级", section_end=8, parent_node_id=1
            )
        records = [
            {
                "boundary_source_ref": {"source_item_index": 5},
                "current_owner_node_id": 3,
                "target_node_id": 1,
                "boundary_carrier_scope": "selected_and_same_carrier",
            }
        ]
        return content_list, headings, records

    def test_noncontiguous_target_derives_the_flatten_policy(self) -> None:
        content_list, headings, records = self._case(with_intro=True)

        _assign_owner_scope_materialization(
            records,
            heading_by_id=headings,
            content_list=content_list,
            frame_member_indices=set(),
        )

        self.assertEqual(
            records[0]["materialization_policy"],
            "flatten_intervening_subtree",
        )
        self.assertEqual(records[0]["flatten_subtree_root_node_id"], 2)

    def test_contiguous_target_stays_direct(self) -> None:
        content_list, headings, records = self._case(with_intro=False)

        _assign_owner_scope_materialization(
            records,
            heading_by_id=headings,
            content_list=content_list,
            frame_member_indices=set(),
        )

        self.assertEqual(records[0]["materialization_policy"], "direct_target")
        self.assertIsNone(records[0]["flatten_subtree_root_node_id"])

    def test_root_target_never_flattens(self) -> None:
        content_list, headings, records = self._case(with_intro=True)
        records[0]["target_node_id"] = None

        _assign_owner_scope_materialization(
            records,
            heading_by_id=headings,
            content_list=content_list,
            frame_member_indices=set(),
        )

        self.assertEqual(records[0]["materialization_policy"], "direct_target")
        self.assertIsNone(records[0]["flatten_subtree_root_node_id"])

    def test_unclosable_flatten_fails_loudly(self) -> None:
        content_list, headings, records = self._case(
            with_intro=True,
            trailing_sibling=True,
        )

        with self.assertRaisesRegex(
            ParserOutputContractError,
            "cannot close the target occurrence",
        ):
            _assign_owner_scope_materialization(
                records,
                heading_by_id=headings,
                content_list=content_list,
                frame_member_indices=set(),
            )

    def test_flatten_rejects_a_second_break_inside_the_target(self) -> None:
        content_list, headings, records = self._case(with_intro=True)
        records.append(
            {
                "boundary_source_ref": {"source_item_index": 6},
                "current_owner_node_id": 1,
                "target_node_id": None,
                "boundary_carrier_scope": "selected_and_same_carrier",
            }
        )

        with self.assertRaisesRegex(
            ParserOutputContractError,
            "overlaps another break",
        ):
            _assign_owner_scope_materialization(
                records,
                heading_by_id=headings,
                content_list=content_list,
                frame_member_indices=set(),
            )


if __name__ == "__main__":
    unittest.main()
