import json
import tempfile
import unittest
from pathlib import Path

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import MinerUArtifactReader
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
)
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.parser import MinerUDocumentParser
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.domain.errors import ParserVersionProbeError


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


class MinerUArtifactReaderTests(unittest.TestCase):
    def test_locates_nested_content_list_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "sample" / "auto"
            nested.mkdir(parents=True)
            content_list = nested / "sample_content_list.json"
            content_list.write_text('[{"type": "text", "text": "hello"}]', encoding="utf-8")
            (nested / "sample_content_list_v2.json").write_text("[]", encoding="utf-8")
            markdown = nested / "sample.md"
            markdown.write_text("hello", encoding="utf-8")

            reader = MinerUArtifactReader()
            artifacts = reader.locate(root)
            self.assertEqual(artifacts.content_list_path, content_list)
            self.assertEqual(artifacts.markdown_path, markdown)
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


class MinerUDocumentParserTests(unittest.TestCase):
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
