import unittest

from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
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


class MapperToIRTests(unittest.TestCase):
    def test_neutral_kind_mapping_heading_level_and_raw_kind(self) -> None:
        normalized = MinerUToNormalizedIRMapper().map_content_list(
            content_list=[
                {"type": "header", "text": "页眉", "page_idx": 0},
                {"type": "page_number", "text": "1", "page_idx": 0},
                {"type": "text", "text": "标题", "text_level": 2, "page_idx": 0},
                {"type": "unknown_raw", "text": "保留", "page_idx": 0},
            ],
            parser_info=_parser_info(),
            document_metadata={
                "document_id": "doc_1",
                "source_pdf": "raw/doc.pdf",
                "title": "sample",
            },
        )

        self.assertEqual(
            [element["kind"] for element in normalized["elements"]],
            ["page_furniture", "page_furniture", "heading", "unknown"],
        )
        self.assertEqual(normalized["elements"][0]["raw_kind"], "header")
        self.assertEqual(normalized["elements"][2]["heading_level"], 2)
        self.assertEqual(normalized["elements"][3]["raw_kind"], "unknown_raw")

    def test_structures_rowspan_colspan_table_and_preserves_qa_cell_text(self) -> None:
        normalized = MinerUToNormalizedIRMapper().map_content_list(
            content_list=[
                {
                    "type": "table",
                    "page_idx": 0,
                    "table_body": (
                        "<table>"
                        "<tr><td>问题</td><td colspan=\"2\">回答</td></tr>"
                        "<tr><td rowspan=\"2\">收入是否增长？</td><td>是</td><td>10%</td></tr>"
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
        self.assertEqual(table["headers"], ["问题", "回答", "回答"])
        self.assertEqual(table["rows"][0], ["收入是否增长？", "是", "10%"])
        self.assertIn("收入是否增长？", "".join("".join(row) for row in table["rows"]))
        self.assertEqual(
            table["merged_cells"],
            [
                {"row": 0, "col": 1, "rowspan": 1, "colspan": 2},
                {"row": 1, "col": 0, "rowspan": 2, "colspan": 1},
            ],
        )


if __name__ == "__main__":
    unittest.main()
