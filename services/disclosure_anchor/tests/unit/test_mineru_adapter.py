import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import MinerUArtifactReader
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
    TABLE_RECONCILIATION_ALGORITHM_VERSION,
)
from disclosure_anchor.adapters.parsers.mineru import mineru_process
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.parser import (
    MinerUDocumentParser,
    map_reconciled_mineru_content_list,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    TableReconciliationContractError,
)
from disclosure_anchor.domain.errors import (
    ParserOutputContractError,
    ParserVersionProbeError,
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
    def test_table_html_alias_uses_nonempty_value_and_rejects_conflicts(self) -> None:
        mapper = MinerUToNormalizedIRMapper()
        metadata = {"document_id": "doc_table_alias", "title": "样本"}
        normalized = mapper.map_content_list(
            content_list=[
                {
                    "type": "table",
                    "page_idx": 0,
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
                        "table_body": "<table><tr><td>A</td></tr></table>",
                        "table_html": "<table><tr><td>B</td></tr></table>",
                    }
                ],
                parser_info=_parser_info(),
                document_metadata=metadata,
            )

    def test_repeated_page_edge_headings_become_furniture_by_layout_evidence(
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

        self.assertEqual(normalized["elements"][0]["kind"], "heading")
        self.assertEqual(
            [item["kind"] for item in normalized["elements"][1:]],
            ["page_furniture", "page_furniture", "page_furniture"],
        )
        self.assertTrue(
            all(item["raw_kind"] == "text" for item in normalized["elements"][1:])
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

        self.assertTrue(all(item["kind"] == "heading" for item in normalized["elements"]))

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
                {"type": "aside_text", "text": "补充说明", "page_idx": 1},
                {"type": "page_footnote", "text": "定义：口径说明", "page_idx": 1},
                {
                    "type": "chart",
                    "page_idx": 1,
                    "img_path": "images/chart.jpg",
                    "content": "| 指标 | 数值 |\n| --- | --- |\n| 收入 | 10 |",
                    "chart_caption": ["收入结构", "按期末数"],
                    "chart_footnote": ["注：未经审计"],
                    "sub_type": "bar",
                },
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
        self.assertEqual(normalized["contract_version"], "normalized_ir.v3")
        self.assertEqual(normalized["parsed_pages"]["start_page_no"], 1)
        self.assertEqual(normalized["parsed_pages"]["end_page_no"], 2)
        self.assertEqual(
            [item["kind"] for item in normalized["elements"]],
            [
                "heading",
                "text",
                "page_furniture",
                "table",
                "equation",
                "text",
                "text",
                "image",
                "unknown",
            ],
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
        visual = normalized["elements"][7]
        self.assertEqual(visual["raw_kind"], "chart")
        self.assertEqual(visual["text"], "| 指标 | 数值 |\n| --- | --- |\n| 收入 | 10 |")
        self.assertEqual(visual["image_caption"], ["收入结构", "按期末数"])
        self.assertEqual(visual["image_footnote"], ["注：未经审计"])
        self.assertEqual(visual["visual_subtype"], "bar")
        self.assertEqual(normalized["elements"][8]["raw_kind"], "mystery")
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

        for field, value in (
            ("chart_caption", ["合法", {"text": "非字符串"}]),
            ("chart_footnote", {"text": "非数组"}),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ParserOutputContractError):
                    mapper.map_content_list(
                        content_list=[
                            {
                                "type": "chart",
                                "page_idx": 0,
                                "img_path": "images/chart.jpg",
                                field: value,
                            }
                        ],
                        parser_info=_parser_info(),
                        document_metadata={"document_id": "doc_bad_visual"},
                    )

    def test_aggregate_table_locator_semantics_fail_loud(self) -> None:
        base_locator = {
            "algorithm_version": TABLE_RECONCILIATION_ALGORITHM_VERSION,
            "page_span": [1, 2],
            "page_bboxes": [
                {"page_no": 1, "bbox": [100, 700, 900, 900]},
                {"page_no": 2, "bbox": [100, 100, 900, 300]},
            ],
            "model_table_indices": [0, 1],
            "continuation_source_item_indices": [1],
        }
        variants: dict[str, dict[str, Any]] = {
            "old_algorithm_version": {
                **base_locator,
                "algorithm_version": "mineru-aggregate-table-restore.v3",
            },
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
                                "bbox": [100, 700, 900, 900],
                                "table_body": "<table><tr><td>A</td></tr></table>",
                                "_mineru_aggregate_table_locator": locator,
                            },
                            {
                                "type": "table",
                                "page_idx": 1,
                                "bbox": [100, 100, 900, 300],
                                "table_body": "",
                            },
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

    def test_aggregate_table_locator_matches_source_content_list(self) -> None:
        locator = {
            "algorithm_version": TABLE_RECONCILIATION_ALGORITHM_VERSION,
            "page_span": [1, 2],
            "page_bboxes": [
                {"page_no": 1, "bbox": [100, 700, 900, 900]},
                {"page_no": 2, "bbox": [100, 100, 900, 300]},
            ],
            "model_table_indices": [4, 7],
            "continuation_source_item_indices": [1],
        }
        mapper = MinerUToNormalizedIRMapper()
        normalized = mapper.map_content_list(
            content_list=[
                {
                    "type": "table",
                    "page_idx": 0,
                    "bbox": [101, 699, 899, 901],
                    "table_body": "<table><tr><td>A</td></tr></table>",
                    "_mineru_aggregate_table_locator": locator,
                },
                {
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [98, 102, 902, 298],
                    "table_body": "  ",
                },
            ],
            parser_info=_parser_info(),
            document_metadata={
                "document_id": "doc_locator_valid",
                "source_pdf": "raw/sample.pdf",
                "title": "sample",
            },
        )

        carrier, ghost = normalized["elements"]
        self.assertEqual(carrier["page_no"], 1)
        self.assertEqual(carrier["page_span"], [1, 2])
        self.assertEqual(carrier["model_table_indices"], [4, 7])
        self.assertEqual(carrier["continuation_source_item_indices"], [1])
        self.assertEqual(ghost["page_no"], 2)
        self.assertEqual(ghost["table_html"], "  ")

    def test_aggregate_table_locator_source_mismatches_fail_loud(self) -> None:
        base_locator = {
            "algorithm_version": TABLE_RECONCILIATION_ALGORITHM_VERSION,
            "page_span": [1, 2],
            "page_bboxes": [
                {"page_no": 1, "bbox": [100, 700, 900, 900]},
                {"page_no": 2, "bbox": [100, 100, 900, 300]},
            ],
            "model_table_indices": [0, 1],
            "continuation_source_item_indices": [1],
        }

        def content_list(
            *,
            root_page_idx: Any = 0,
            continuation: dict[str, Any] | None = None,
            locator: dict[str, Any] | None = None,
        ) -> list[dict[str, Any]]:
            return [
                {
                    "type": "table",
                    "page_idx": root_page_idx,
                    "bbox": [100, 700, 900, 900],
                    "table_body": "<table><tr><td>A</td></tr></table>",
                    "_mineru_aggregate_table_locator": locator or base_locator,
                },
                continuation
                or {
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [100, 100, 900, 300],
                    "table_body": "",
                },
            ]

        out_of_range_locator = {
            **base_locator,
            "continuation_source_item_indices": [2],
        }
        variants: dict[str, list[dict[str, Any]]] = {
            "root_page_mismatch": content_list(root_page_idx=1),
            "root_page_idx_missing": [
                {
                    "type": "table",
                    "bbox": [100, 700, 900, 900],
                    "table_body": "<table><tr><td>A</td></tr></table>",
                    "_mineru_aggregate_table_locator": base_locator,
                },
                {
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [100, 100, 900, 300],
                    "table_body": "",
                },
            ],
            "root_boolean_page_idx": content_list(root_page_idx=True),
            "root_empty_table_html": [
                {
                    "type": "table",
                    "page_idx": 0,
                    "bbox": [100, 700, 900, 900],
                    "table_body": "",
                    "_mineru_aggregate_table_locator": base_locator,
                },
                {
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [100, 100, 900, 300],
                    "table_body": "",
                },
            ],
            "root_bbox_missing": [
                {
                    "type": "table",
                    "page_idx": 0,
                    "table_body": "<table><tr><td>A</td></tr></table>",
                    "_mineru_aggregate_table_locator": base_locator,
                },
                {
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [100, 100, 900, 300],
                    "table_body": "",
                },
            ],
            "root_bbox_nonfinite": [
                {
                    "type": "table",
                    "page_idx": 0,
                    "bbox": [100, 700, float("nan"), 900],
                    "table_body": "<table><tr><td>A</td></tr></table>",
                    "_mineru_aggregate_table_locator": base_locator,
                },
                {
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [100, 100, 900, 300],
                    "table_body": "",
                },
            ],
            "root_bbox_wrong_geometry": [
                {
                    "type": "table",
                    "page_idx": 0,
                    "bbox": [100, 700, 100, 900],
                    "table_body": "<table><tr><td>A</td></tr></table>",
                    "_mineru_aggregate_table_locator": base_locator,
                },
                {
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [100, 100, 900, 300],
                    "table_body": "",
                },
            ],
            "root_bbox_mismatch": [
                {
                    "type": "table",
                    "page_idx": 0,
                    "bbox": [90, 700, 900, 900],
                    "table_body": "<table><tr><td>A</td></tr></table>",
                    "_mineru_aggregate_table_locator": base_locator,
                },
                {
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [100, 100, 900, 300],
                    "table_body": "",
                },
            ],
            "continuation_out_of_range": content_list(locator=out_of_range_locator),
            "continuation_not_table": content_list(
                continuation={
                    "type": "text",
                    "page_idx": 1,
                    "bbox": [100, 100, 900, 300],
                    "text": "正文",
                }
            ),
            "continuation_nonempty_table_body": content_list(
                continuation={
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [100, 100, 900, 300],
                    "table_body": "<table><tr><td>B</td></tr></table>",
                }
            ),
            "continuation_nonempty_table_html": content_list(
                continuation={
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [100, 100, 900, 300],
                    "table_body": "",
                    "table_html": "<table><tr><td>B</td></tr></table>",
                }
            ),
            "continuation_page_idx_missing": content_list(
                continuation={
                    "type": "table",
                    "bbox": [100, 100, 900, 300],
                    "table_body": "",
                }
            ),
            "continuation_page_mismatch": content_list(
                continuation={
                    "type": "table",
                    "page_idx": 2,
                    "bbox": [100, 100, 900, 300],
                    "table_body": "",
                }
            ),
            "continuation_bbox_missing": content_list(
                continuation={"type": "table", "page_idx": 1, "table_body": ""}
            ),
            "continuation_bbox_wrong_geometry": content_list(
                continuation={
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [100, 100, 900, 100],
                    "table_body": "",
                }
            ),
            "continuation_bbox_mismatch": content_list(
                continuation={
                    "type": "table",
                    "page_idx": 1,
                    "bbox": [100, 104, 900, 300],
                    "table_body": "",
                }
            ),
        }
        mapper = MinerUToNormalizedIRMapper()
        metadata = {
            "document_id": "doc_locator_invalid_source",
            "source_pdf": "raw/sample.pdf",
            "title": "sample",
        }
        for label, content in variants.items():
            with self.subTest(label=label):
                with self.assertRaises(ParserOutputContractError):
                    mapper.map_content_list(
                        content_list=content,
                        parser_info=_parser_info(),
                        document_metadata=metadata,
                    )


class MinerUDocumentParserTests(unittest.TestCase):
    def test_parser_attaches_locator_without_splitting_physical_tables(self) -> None:
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
            [[["A"], ["B"]], []],
        )
        root, ghost = result.normalized_ir["elements"]
        self.assertEqual(
            [root["table_html"], ghost["table_html"]],
            [
                "<table><tr><td>A</td></tr><tr><td>B</td></tr></table>",
                "",
            ],
        )
        self.assertEqual(root["page_span"], [1, 2])
        self.assertEqual(
            root["page_bboxes"],
            [
                {"page_no": 1, "bbox": [100.0, 700.0, 900.0, 900.0]},
                {"page_no": 2, "bbox": [100.0, 100.0, 900.0, 300.0]},
            ],
        )
        self.assertEqual(root["model_table_indices"], [0, 1])
        self.assertEqual(root["continuation_source_item_indices"], [1])
        self.assertEqual(
            root["table_locator_algorithm"],
            "mineru-aggregate-table-locator.v4",
        )
        diagnostics = result.normalized_ir["parser_diagnostics"][
            "table_reconciliation"
        ]
        self.assertEqual(
            diagnostics["algorithm_version"],
            "mineru-aggregate-table-locator.v4",
        )
        self.assertEqual(diagnostics["located_groups"], 1)
        self.assertEqual(diagnostics["located_tables"], 2)
        self.assertEqual(diagnostics["locator_only_groups"], 1)
        self.assertEqual(diagnostics["locator_only_tables"], 2)
        self.assertEqual(diagnostics["restored_groups"], 0)
        self.assertEqual(diagnostics["restored_tables"], 0)
        self.assertEqual(diagnostics["restoration_rejected_groups"], 0)
        self.assertEqual(diagnostics["unresolved_groups"], 0)
        self.assertRegex(diagnostics["model_hash"], r"^sha256:[a-f0-9]{64}$")
        self.assertIsNotNone(result.model_path)
        self.assertEqual(result.model_path.name, "sample_model.json")

    def test_reconciliation_diagnostics_preserve_other_parser_diagnostics(
        self,
    ) -> None:
        mapper = mock.Mock(spec=MinerUToNormalizedIRMapper)
        mapper.map_content_list.return_value = {
            "contract_version": "normalized_ir.v3",
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

    def test_reconciliation_contract_reason_is_preserved_in_error(
        self,
    ) -> None:
        mapper = mock.Mock(spec=MinerUToNormalizedIRMapper)
        mapper.map_content_list.return_value = {
            "contract_version": "normalized_ir.v3",
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
        with mock.patch(
            "disclosure_anchor.adapters.parsers.mineru.parser."
            "validate_table_reconciliation_payload",
            side_effect=TableReconciliationContractError(
                "locator_table_grid", "root grid missing"
            ),
        ):
            with self.assertRaises(ParserOutputContractError) as caught:
                map_reconciled_mineru_content_list(
                    content_list=[],
                    model_path=None,
                    mapper=mapper,
                    parser_info=_parser_info(),
                    document_metadata={
                        "document_id": "doc_bad_locator",
                        "source_pdf": "raw/sample.pdf",
                        "title": "sample",
                    },
                )

        self.assertIn("locator_table_grid", str(caught.exception))
        self.assertIn("root grid missing", str(caught.exception))

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
