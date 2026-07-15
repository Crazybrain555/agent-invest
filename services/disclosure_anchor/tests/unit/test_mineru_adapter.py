import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import MinerUArtifactReader
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
)
from disclosure_anchor.adapters.parsers.mineru import mineru_process
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.parser import (
    MinerUDocumentParser,
    _needs_native_text,
    map_reconciled_mineru_content_list,
)
from disclosure_anchor.adapters.parsers.native_text import (
    NativeTextExtractionError,
    NativeTextTimeoutError,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.domain.errors import (
    ParserOutputContractError,
    ParserVersionProbeError,
)


class MinerUProcessTests(unittest.TestCase):
    def test_command_includes_stable_phase04_options(self) -> None:
        process = MinerUProcess(executable=Path("/opt/mineru/bin/mineru"))
        command = process.command_for(
            input_pdf=Path("input.pdf"),
            output_dir=Path("out"),
            options=ParserOptions(start_page=0, end_page=2),
        )
        self.assertEqual(command[:5], ["/opt/mineru/bin/mineru", "-p", "input.pdf", "-o", "out"])
        self.assertIn("-m", command)
        self.assertIn("auto", command)
        self.assertIn("-b", command)
        self.assertIn("pipeline", command)
        self.assertIn("-f", command)
        self.assertIn("false", command)
        self.assertIn("-t", command)
        self.assertIn("true", command)
        self.assertIn("-s", command)
        self.assertIn("0", command)
        self.assertIn("-e", command)
        self.assertIn("2", command)
        self.assertNotIn("-u", command)

    def test_command_appends_server_url_for_http_client_backend(self) -> None:
        process = MinerUProcess(executable=Path("/opt/mineru/bin/mineru"))
        command = process.command_for(
            input_pdf=Path("input.pdf"),
            output_dir=Path("out"),
            options=ParserOptions(
                backend="vlm-http-client", server_url="http://192.168.1.50:30000"
            ),
        )
        self.assertIn("vlm-http-client", command)
        url_index = command.index("-u")
        self.assertEqual(command[url_index + 1], "http://192.168.1.50:30000")

    def test_shutdown_kills_every_registered_process_group(self) -> None:
        process = mock.MagicMock(pid=43210)
        process.poll.return_value = None
        mineru_process._register_process(process)
        try:
            with mock.patch.object(mineru_process.os, "killpg") as killpg:
                terminated = mineru_process.terminate_active_mineru_processes()
        finally:
            mineru_process._unregister_process(process)

        self.assertEqual(terminated, 1)
        killpg.assert_called_once_with(43210, mineru_process.signal.SIGKILL)

    def test_version_probe_timeout_kills_registered_process_group(self) -> None:
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

        killpg.assert_called_once_with(54321, mineru_process.signal.SIGKILL)
        process.wait.assert_called_once_with()
        self.assertNotIn(process, mineru_process._ACTIVE_PROCESSES)


class MinerUArtifactReaderTests(unittest.TestCase):
    def test_locates_nested_content_list_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "sample" / "auto"
            nested.mkdir(parents=True)
            content_list = nested / "sample_content_list.json"
            content_list.write_text('[{"type": "text", "text": "hello"}]', encoding="utf-8")
            (nested / "sample_content_list_v2.json").write_text("[]", encoding="utf-8")
            model = nested / "sample_model.json"
            model.write_text("[]", encoding="utf-8")
            markdown = nested / "sample.md"
            markdown.write_text("hello", encoding="utf-8")

            reader = MinerUArtifactReader()
            artifacts = reader.locate(root)
            self.assertEqual(artifacts.content_list_path, content_list)
            self.assertEqual(artifacts.markdown_path, markdown)
            self.assertEqual(artifacts.model_path, model)
            self.assertEqual(reader.read_content_list(content_list)[0]["text"], "hello")


class MinerUMapperTests(unittest.TestCase):
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
                {"type": "text", "text": "正文", "page_idx": 0},
                {"type": "page_number", "text": "1 / 2", "page_idx": 0},
                {
                    "type": "table",
                    "page_idx": 1,
                    "table_caption": ["表 1"],
                    "table_footnote": ["注"],
                    "table_body": (
                        "<table><tr><th rowspan=\"2\">项目</th><th>金额</th></tr>"
                        "<tr><td>10</td></tr></table>"
                    ),
                    "img_path": "images/a.jpg",
                },
                {"type": "equation", "text": "E=mc^2", "page_idx": 1},
                {"type": "mystery", "text": "保留未知类型", "page_idx": 1},
            ],
            parser_info=MinerUParserInfo(
                name="MinerU",
                package_version="3.4.0",
                backend="pipeline",
                method="auto",
                language="ch",
                formula=False,
                table=True,
            ),
            document_metadata={
                "document_id": "doc_01K0000000000000000000000",
                "source_pdf": "raw_documents/local/sample.pdf",
                "title": "sample",
            },
            parser_artifacts={
                "artifact_root_relpath": "parser_artifacts/sample",
                "content_list_relpath": "parser_artifacts/sample/sample.json",
            },
        )
        self.assertEqual(normalized["contract_version"], "normalized_ir.v2")
        self.assertEqual(normalized["parsed_pages"]["start_page_no"], 1)
        self.assertEqual(normalized["parsed_pages"]["end_page_no"], 2)
        self.assertEqual(
            [item["kind"] for item in normalized["elements"]],
            ["heading", "text", "page_furniture", "table", "equation", "unknown"],
        )
        self.assertEqual(normalized["elements"][0]["raw_kind"], "text")
        self.assertEqual(normalized["elements"][0]["heading_level"], 1)
        self.assertEqual(normalized["elements"][2]["raw_kind"], "page_number")
        self.assertEqual(normalized["elements"][3]["table"]["headers"], ["项目", "金额"])
        self.assertEqual(normalized["elements"][3]["table"]["rows"], [["项目", "10"]])
        self.assertEqual(
            normalized["elements"][3]["table"]["merged_cells"],
            [{"row": 0, "col": 0, "rowspan": 2, "colspan": 1}],
        )
        self.assertEqual(normalized["elements"][5]["raw_kind"], "mystery")
        json.dumps(normalized, ensure_ascii=False)

    def test_preserves_string_list_items_and_rejects_malformed_lists(self) -> None:
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
                {
                    "type": "list",
                    "list_items": ["可读项", {"text": "非稳定嵌套形状"}],
                    "page_idx": 0,
                },
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
            ),
            document_metadata={
                "document_id": "doc_01K0000000000000000000000",
                "source_pdf": "raw_documents/local/sample.pdf",
                "title": "sample",
            },
        )

        self.assertEqual(
            [element["kind"] for element in normalized["elements"]],
            ["text", "unknown", "unknown", "unknown"],
        )
        self.assertEqual(normalized["elements"][0]["raw_kind"], "list")
        self.assertEqual(
            normalized["elements"][0]["text"],
            "1、第一项\n\n2、第二项",
        )
        self.assertTrue(
            all("text" not in element for element in normalized["elements"][1:])
        )

    def test_aggregate_table_locator_semantics_fail_loud(self) -> None:
        base_locator = {
            "algorithm_version": "mineru-aggregate-table-restore.v3",
            "page_span": [1, 2],
            "page_bboxes": [
                {"page_no": 1, "bbox": [100, 700, 900, 900]},
                {"page_no": 2, "bbox": [100, 100, 900, 300]},
            ],
            "model_table_indices": [0, 1],
            "continuation_source_item_indices": [1],
        }
        variants = {
            "reversed_span": {**base_locator, "page_span": [2, 1]},
            "nonconsecutive_pages": {
                **base_locator,
                "page_span": [1, 3],
                "page_bboxes": [
                    {"page_no": 1, "bbox": [100, 700, 900, 900]},
                    {"page_no": 3, "bbox": [100, 100, 900, 300]},
                ],
            },
            "duplicate_page_number": {
                **base_locator,
                "page_bboxes": [
                    {"page_no": 1, "bbox": [100, 700, 900, 900]},
                    {"page_no": 1, "bbox": [100, 100, 900, 300]},
                ],
            },
            "zero_width_bbox": {
                **base_locator,
                "page_bboxes": [
                    {"page_no": 1, "bbox": [100, 700, 100, 900]},
                    {"page_no": 2, "bbox": [100, 100, 900, 300]},
                ],
            },
            "model_length_mismatch": {
                **base_locator,
                "model_table_indices": [0, 1, 2],
            },
            "continuation_precedes_root": {
                **base_locator,
                "continuation_source_item_indices": [0],
            },
            "partial_bundle": {
                key: value
                for key, value in base_locator.items()
                if key != "page_span"
            },
        }
        mapper = MinerUToNormalizedIRMapper()
        parser_info = MinerUParserInfo(
            name="MinerU",
            package_version="3.4.0",
            backend="pipeline",
            method="auto",
            language="ch",
            formula=False,
            table=True,
        )
        metadata = {
            "document_id": "doc_locator",
            "source_pdf": "raw/sample.pdf",
            "title": "sample",
        }
        for label, locator in variants.items():
            with self.subTest(label=label):
                with self.assertRaises(ParserOutputContractError):
                    mapper.map_content_list(
                        content_list=[
                            {
                                "type": "table",
                                "page_idx": 0,
                                "table_body": "<table><tr><td>A</td></tr></table>",
                                "_mineru_aggregate_table_locator": locator,
                            }
                        ],
                        parser_info=parser_info,
                        document_metadata=metadata,
                    )

        invalid_carriers = (
            {
                "type": "text",
                "text": "正文",
                "_mineru_aggregate_table_locator": base_locator,
            },
            {
                "type": "table",
                "table_body": "<table><tr><td>A</td></tr></table>",
                "_mineru_aggregate_table_locator": [base_locator],
            },
        )
        for item in invalid_carriers:
            with self.subTest(invalid_carrier=item["type"]):
                with self.assertRaises(ParserOutputContractError):
                    mapper.map_content_list(
                        content_list=[item],
                        parser_info=parser_info,
                        document_metadata=metadata,
                    )


class MinerUDocumentParserTests(unittest.TestCase):
    def test_native_text_title_gate_covers_observed_ir_information_families(
        self,
    ) -> None:
        for title in (
            "平安银行调研活动信息(4)",
            "平安银行投资者关系管理信息",
            "某公司投资者沟通情况通报",
            "某公司业绩交流会问答实录",
        ):
            with self.subTest(title=title):
                self.assertTrue(_needs_native_text({"title": title}))
        self.assertFalse(
            _needs_native_text(
                {
                    "title": "某公司关于回购股份的公告",
                    "provider_category_names": ["业绩说明会"],
                }
            )
        )
        self.assertTrue(
            _needs_native_text(
                {
                    "title": "平安银行：业绩说明会、路演活动信息",
                    "provider_category_names": ["业绩说明会"],
                }
            )
        )
        self.assertFalse(
            _needs_native_text(
                {"title": "某公司：业绩说明会、路演活动信息"}
            )
        )

    def test_ir_form_adds_native_text_shadow_but_normal_pdf_does_not(self) -> None:
        class SuccessfulProcess:
            def run(
                self, *, input_pdf: Path, output_dir: Path, options: ParserOptions
            ) -> None:
                nested = output_dir / "sample" / "auto"
                nested.mkdir(parents=True)
                (nested / "sample_content_list.json").write_text(
                    '[{"type": "text", "text": "hello", "page_idx": 0}]',
                    encoding="utf-8",
                )

            def version(self) -> str:
                return "3.4.0"

        native_payload = {
            "status": "ok",
            "extractor": {"name": "pdfplumber", "version": "0.11.10"},
            "content_hash": "sha256:" + "0" * 64,
            "non_whitespace_chars": 5,
            "pages": [
                {"page_no": 1, "text": "hello", "non_whitespace_chars": 5}
            ],
        }
        extractor = mock.Mock()
        extractor.extract.return_value = native_payload

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF-1.4\nsample\n%%EOF\n")
            parser = MinerUDocumentParser(
                process=SuccessfulProcess(),
                native_text_extractor=extractor,
                parser_version="3.4.0",
            )

            ir_result = parser.parse(
                input_pdf=input_pdf,
                output_dir=root / "ir-out",
                options=ParserOptions(),
                document_metadata={
                    "document_id": "doc_ir",
                    "source_pdf": "raw_documents/local/ir.pdf",
                    "title": "某公司投资者关系活动记录表",
                },
            )
            self.assertEqual(ir_result.normalized_ir["native_text"], native_payload)
            self.assertEqual(
                ir_result.normalized_ir["parser_diagnostics"][
                    "native_text_shadow"
                ],
                {"status": "ok", "error_code": None},
            )
            self.assertEqual(
                ir_result.normalized_ir["parser_diagnostics"][
                    "table_reconciliation"
                ]["model_status"],
                "absent",
            )
            extractor.extract.assert_called_once_with(input_pdf, timeout_seconds=None)

            extractor.reset_mock()
            ordinary_result = parser.parse(
                input_pdf=input_pdf,
                output_dir=root / "ordinary-out",
                options=ParserOptions(),
                document_metadata={
                    "document_id": "doc_ordinary",
                    "source_pdf": "raw_documents/local/ordinary.pdf",
                    "title": "某公司关于回购股份的公告",
                    "provider_category_names": ["业绩说明会"],
                },
            )
            self.assertNotIn("native_text", ordinary_result.normalized_ir)
            extractor.extract.assert_not_called()

            partial_result = parser.parse(
                input_pdf=input_pdf,
                output_dir=root / "partial-out",
                options=ParserOptions(start_page=0, end_page=0),
                document_metadata={
                    "document_id": "doc_partial_ir",
                    "source_pdf": "raw_documents/local/partial-ir.pdf",
                    "title": "某公司投资者关系活动记录表",
                },
            )
            self.assertNotIn("native_text", partial_result.normalized_ir)
            extractor.extract.assert_not_called()

    def test_parser_reconciles_proven_model_table_pair_before_mapping(self) -> None:
        first = "<table><tr><td>A</td></tr></table>"
        second = "<table><tr><td>B</td></tr></table>"

        class SuccessfulProcess:
            def run(
                self, *, input_pdf: Path, output_dir: Path, options: ParserOptions
            ) -> None:
                nested = output_dir / "sample" / "auto"
                nested.mkdir(parents=True)
                content = [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "bbox": [100, 700, 900, 900],
                        "table_body": (
                            "<table><tr><td>A</td></tr>"
                            "<tr><td>B</td></tr></table>"
                        ),
                    },
                    {
                        "type": "table",
                        "page_idx": 1,
                        "bbox": [100, 100, 900, 300],
                        "table_body": "",
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
                (nested / "sample_model.json").write_text(
                    json.dumps(model), encoding="utf-8"
                )

            def version(self) -> str:
                return "3.4.0"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF-1.4\nsample\n%%EOF\n")
            result = MinerUDocumentParser(
                process=SuccessfulProcess(), parser_version="3.4.0"
            ).parse(
                input_pdf=input_pdf,
                output_dir=root / "out",
                options=ParserOptions(),
                document_metadata={
                    "document_id": "doc_table_reconcile",
                    "source_pdf": "raw_documents/local/sample.pdf",
                    "title": "普通公告",
                },
            )

        self.assertEqual(
            [element["table"]["rows"] for element in result.normalized_ir["elements"]],
            [[["A"]], [["B"]]],
        )
        self.assertNotIn("page_span", result.normalized_ir["elements"][0])
        diagnostics = result.normalized_ir["parser_diagnostics"][
            "table_reconciliation"
        ]
        self.assertEqual(diagnostics["located_groups"], 1)
        self.assertEqual(diagnostics["located_tables"], 2)
        self.assertEqual(diagnostics["restored_groups"], 1)
        self.assertEqual(diagnostics["restored_tables"], 2)
        self.assertRegex(diagnostics["model_hash"], r"^sha256:[a-f0-9]{64}$")
        self.assertIsNotNone(result.model_path)
        self.assertEqual(result.model_path.name, "sample_model.json")

    def test_expected_native_shadow_failures_preserve_mineru_result(self) -> None:
        class SuccessfulProcess:
            def run(
                self, *, input_pdf: Path, output_dir: Path, options: ParserOptions
            ) -> None:
                nested = output_dir / "sample" / "auto"
                nested.mkdir(parents=True)
                (nested / "sample_content_list.json").write_text(
                    '[{"type": "text", "text": "MinerU正文", "page_idx": 0}]',
                    encoding="utf-8",
                )

            def version(self) -> str:
                return "3.4.0"

        failures = (
            (
                NativeTextTimeoutError("shadow timeout", error_code="timeout"),
                "timeout",
            ),
            (
                NativeTextExtractionError(
                    "bad PDF shadow", error_code="pdf_parse_error"
                ),
                "pdf_parse_error",
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF-1.4\nsample\n%%EOF\n")
            for index, (failure, error_code) in enumerate(failures):
                with self.subTest(error_code=error_code):
                    extractor = mock.Mock()
                    extractor.extract.side_effect = failure
                    result = MinerUDocumentParser(
                        process=SuccessfulProcess(),
                        native_text_extractor=extractor,
                        parser_version="3.4.0",
                    ).parse(
                        input_pdf=input_pdf,
                        output_dir=root / f"out-{index}",
                        options=ParserOptions(),
                        document_metadata={
                            "document_id": f"doc_shadow_{index}",
                            "source_pdf": "raw/sample.pdf",
                            "title": "某公司投资者关系活动记录表",
                        },
                    )
                    self.assertNotIn("native_text", result.normalized_ir)
                    self.assertEqual(
                        result.normalized_ir["parser_diagnostics"][
                            "native_text_shadow"
                        ],
                        {"status": "unavailable", "error_code": error_code},
                    )
                    self.assertEqual(
                        result.normalized_ir["elements"][0]["text"],
                        "MinerU正文",
                    )

    def test_exhausted_native_budget_degrades_without_calling_extractor(
        self,
    ) -> None:
        class SuccessfulProcess:
            def run(
                self, *, input_pdf: Path, output_dir: Path, options: ParserOptions
            ) -> None:
                nested = output_dir / "sample" / "auto"
                nested.mkdir(parents=True)
                (nested / "sample_content_list.json").write_text(
                    '[]', encoding="utf-8"
                )

            def version(self) -> str:
                return "3.4.0"

        extractor = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            result = MinerUDocumentParser(
                process=SuccessfulProcess(),
                native_text_extractor=extractor,
                parser_version="3.4.0",
            ).parse(
                input_pdf=input_pdf,
                output_dir=root / "out",
                options=ParserOptions(timeout_seconds=0),
                document_metadata={
                    "document_id": "doc_budget",
                    "source_pdf": "raw/sample.pdf",
                    "title": "某公司投资者关系活动记录表",
                },
            )
        extractor.extract.assert_not_called()
        self.assertEqual(
            result.normalized_ir["parser_diagnostics"]["native_text_shadow"],
            {"status": "unavailable", "error_code": "budget_exhausted"},
        )

    def test_unknown_native_shadow_error_remains_fatal(self) -> None:
        class SuccessfulProcess:
            def run(
                self, *, input_pdf: Path, output_dir: Path, options: ParserOptions
            ) -> None:
                nested = output_dir / "sample" / "auto"
                nested.mkdir(parents=True)
                (nested / "sample_content_list.json").write_text(
                    '[]', encoding="utf-8"
                )

            def version(self) -> str:
                return "3.4.0"

        extractor = mock.Mock()
        extractor.extract.side_effect = ValueError("unexpected shadow bug")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            parser = MinerUDocumentParser(
                process=SuccessfulProcess(),
                native_text_extractor=extractor,
                parser_version="3.4.0",
            )
            with self.assertRaisesRegex(ValueError, "unexpected shadow bug"):
                parser.parse(
                    input_pdf=input_pdf,
                    output_dir=root / "out",
                    options=ParserOptions(),
                    document_metadata={
                        "document_id": "doc_unknown_shadow",
                        "source_pdf": "raw/sample.pdf",
                        "title": "某公司投资者关系活动记录表",
                    },
                )

    def test_reconciliation_diagnostics_preserve_other_parser_diagnostics(
        self,
    ) -> None:
        mapper = mock.Mock(spec=MinerUToNormalizedIRMapper)
        mapper.map_content_list.return_value = {
            "parser_diagnostics": {"future_probe": {"status": "ok"}}
        }
        normalized, reconciliation = map_reconciled_mineru_content_list(
            content_list=[],
            model_path=None,
            mapper=mapper,
            parser_info=MinerUParserInfo(
                name="MinerU",
                package_version="3.4.0",
                backend="pipeline",
                method="auto",
                language="ch",
                formula=False,
                table=True,
            ),
            document_metadata={
                "document_id": "doc_diagnostics",
                "source_pdf": "raw/sample.pdf",
                "title": "sample",
            },
        )

        self.assertEqual(
            normalized["parser_diagnostics"]["future_probe"],
            {"status": "ok"},
        )
        self.assertEqual(
            normalized["parser_diagnostics"]["table_reconciliation"][
                "model_status"
            ],
            "absent",
        )
        self.assertEqual(reconciliation.stats.model_status, "absent")

    def test_version_probe_failure_fails_closed(self) -> None:
        class VersionFailingProcess:
            def run(self, *, input_pdf: Path, output_dir: Path, options: ParserOptions):
                nested = output_dir / "sample" / "auto"
                nested.mkdir(parents=True)
                (nested / "sample_content_list.json").write_text(
                    '[{"type": "text", "text": "hello", "page_idx": 0}]',
                    encoding="utf-8",
                )

            def version(self) -> str:
                raise ParserVersionProbeError("version failed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_pdf = root / "input.pdf"
            input_pdf.write_bytes(b"%PDF-1.4\nsample\n%%EOF\n")
            parser = MinerUDocumentParser(process=VersionFailingProcess())

            with self.assertRaises(ParserVersionProbeError):
                parser.parse(
                    input_pdf=input_pdf,
                    output_dir=root / "out",
                    options=ParserOptions(),
                    document_metadata={
                        "document_id": "doc_01K0000000000000000000000",
                        "source_pdf": "raw_documents/local/sample.pdf",
                        "title": "sample",
                    },
                )


if __name__ == "__main__":
    unittest.main()
