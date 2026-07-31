"""Retrieval labels and captions must not rewrite source structure."""

from __future__ import annotations

import unittest

from tests.unit.test_unit_builder import _build, _element, _heading


class SourceStructureProjectionTests(unittest.TestCase):
    def test_regulatory_form_labels_do_not_project_a_physical_table(self) -> None:
        rows = [
            ["投资者关系活动类别", "业绩说明会"],
            ["参与单位名称及人员姓名", "机构甲"],
            ["投资者关系活动主要内容介绍", "问：产能如何？\n答：保持稳定。"],
        ]
        units, _ = _build(
            [
                _element(
                    0,
                    kind="table",
                    raw_kind="table",
                    table_caption=[],
                    table_footnote=[],
                    table_html="<table><tr><td>投资者关系活动类别</td></tr></table>",
                    table={
                        "headers": [],
                        "rows": rows,
                        "merged_cells": [],
                    },
                )
            ],
            filing_type="investor_relations",
        )

        self.assertEqual(units[0].payload["rows"], rows)
        self.assertEqual(units[0].heading_path, [])
        self.assertIsNone(units[0].title)

    def test_caption_never_opens_or_replaces_a_proven_section(self) -> None:
        elements = [
            _element(0, text="重要提示"),
            _element(
                1,
                kind="table",
                raw_kind="table",
                table_caption=["释义"],
                table_footnote=[],
                table_html="<table><tr><td>公司</td><td>本公司</td></tr></table>",
                table={
                    "headers": ["术语", "含义"],
                    "rows": [["公司", "本公司"]],
                    "merged_cells": [],
                },
            ),
            _element(2, text="后续风险说明。"),
        ]
        units, _ = _build(
            elements,
            headings=[_heading(1, 0, text="重要提示", section_end=2)],
        )

        self.assertEqual(units[0].title, "重要提示")
        self.assertEqual(units[0].heading_path, ["重要提示"])
        table = next(
            part for part in units[0].payload["parts"] if part["kind"] == "table"
        )
        self.assertEqual(table["caption"], ["释义"])


if __name__ == "__main__":
    unittest.main()
