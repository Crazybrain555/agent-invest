from __future__ import annotations

import copy
import unittest
from typing import Any

from disclosure_anchor.adapters.unit_builder.builder import (
    build_unit_drafts_s1_s7,
)
from disclosure_anchor.adapters.unit_builder.source_projection import (
    project_official_ir_form,
)


def _table(
    order: int,
    rows: list[list[str]],
    *,
    captions: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "document_id": "doc_projection",
        "ir_id": f"ir_{order:04d}",
        "source_item_index": order,
        "order_index": order,
        "page_idx": 0,
        "page_no": 1,
        "kind": "table",
        "raw_kind": "table",
        "table": {"headers": [], "rows": rows, "merged_cells": []},
        "table_caption": list(captions or []),
        "table_footnote": list(notes or []),
        "table_parse_failed": False,
    }


def _form_rows(*, narrative: str = "经营保持稳健。") -> list[list[str]]:
    return [
        ["投资者关系活动类别", "特定对象调研"],
        ["参与单位名称及人员姓名", "见附件"],
        ["时间", "2026-07-16"],
        ["地点", "电话会议"],
        ["上市公司接待人员姓名", "董事会秘书"],
        ["投资者关系活动主要内容介绍", narrative],
    ]


def _text(order: int, value: str, *, kind: str = "text") -> dict[str, Any]:
    element: dict[str, Any] = {
        "document_id": "doc_projection",
        "ir_id": f"ir_{order:04d}",
        "source_item_index": order,
        "order_index": order,
        "page_idx": 0,
        "page_no": 1,
        "kind": kind,
        "raw_kind": "text",
        "text": value,
    }
    if kind == "heading":
        element["heading_level"] = 1
    return element


class SourceProjectionTests(unittest.TestCase):
    def test_unique_official_form_projects_closed_typed_slices(self) -> None:
        rows = [
            *_form_rows(),
            ["附件清单（如有）", "《参与机构名单》"],
            ["日期", "2026-07-16"],
        ]
        source = _table(
            0,
            rows,
            captions=["编号：2026-001"],
            notes=["本记录由公司披露。"],
        )

        result = project_official_ir_form(
            [source],
            filing_type="investor_relations",
        )

        self.assertEqual(result.status, "proven")
        self.assertEqual(result.projected_carriers, 1)
        self.assertEqual(
            [element["projection_region_role"] for element in result.elements],
            ["metadata", "narrative", "narrative", "footer"],
        )
        self.assertEqual(
            result.elements[0]["source_slice"]["field"]["row_indices"],
            [0, 1, 2, 3, 4],
        )
        self.assertEqual(
            result.elements[1]["source_slice"]["field"],
            {
                "kind": "table_cell",
                "row": 5,
                "column": 0,
                "char_span": [0, len("投资者关系活动主要内容介绍")],
                "value_sha256": result.elements[1]["source_slice"]["field"][
                    "value_sha256"
                ],
            },
        )
        self.assertEqual(
            result.elements[-1]["source_slice"]["field"]["row_indices"],
            [6, 7],
        )
        self.assertEqual(
            result.elements[0]["annotation_source_slices"][0]["field"]["kind"],
            "table_caption",
        )
        self.assertEqual(
            result.elements[-1]["annotation_source_slices"][0]["field"][
                "kind"
            ],
            "table_note",
        )

    def test_business_table_with_similar_words_is_not_a_form(self) -> None:
        source = _table(
            0,
            [
                ["时间", "2026年"],
                ["地点", "华东"],
                ["参与单位名称", "子公司"],
                ["经营活动主要内容", "产能建设"],
            ],
        )

        result = project_official_ir_form(
            [source],
            filing_type="investor_relations",
        )

        self.assertEqual(result.status, "not_applicable")
        self.assertEqual(result.projected_carriers, 0)
        self.assertEqual(result.elements, (source,))

    def test_multiple_form_candidates_fail_closed_without_projection(self) -> None:
        first = _table(0, _form_rows(narrative="第一场交流。"))
        second = _table(1, _form_rows(narrative="第二场交流。"))
        source = [first, second]

        result = project_official_ir_form(
            source,
            filing_type="performance_briefing",
        )

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.reason_code, "official_ir_form_signature_not_unique")
        self.assertEqual(result.projected_carriers, 0)
        self.assertEqual(result.elements, tuple(source))

    def test_attachment_scope_requires_a_proven_form_region(self) -> None:
        anchor = _table(0, _form_rows())
        attachment = _table(
            3,
            [["某某基金", "张三"]],
            captions=["附件 1：《参与机构名单》"],
        )
        source = [
            anchor,
            _text(1, "三、主要交流问题", kind="heading"),
            _text(2, "问：增长如何？\n答：保持稳健。"),
            attachment,
            _text(4, "名单续页。"),
        ]

        proven = project_official_ir_form(
            source,
            filing_type="investor_relations",
        )
        self.assertEqual(proven.status, "proven")
        attachment_index = next(
            index
            for index, element in enumerate(proven.elements)
            if element.get("ir_id") == "ir_0003"
        )
        self.assertEqual(
            proven.elements[attachment_index]["projection_region_role"],
            "attachment",
        )
        self.assertEqual(
            proven.elements[attachment_index + 1]["projection_region_role"],
            "attachment",
        )

        unproven_source = [copy.deepcopy(attachment), _text(4, "名单续页。")]
        unproven = project_official_ir_form(
            unproven_source,
            filing_type="investor_relations",
        )
        self.assertEqual(unproven.status, "not_applicable")
        self.assertNotIn("projection_region_role", unproven.elements[0])
        self.assertNotIn("projection_region_role", unproven.elements[1])

    def test_qa_partition_spans_carriers_and_keeps_heading_provenance(self) -> None:
        elements = [
            _text(0, "三、主要交流问题", kind="heading"),
            _text(1, "1. 请问收入？"),
            _text(2, "答：收入增长。"),
            _text(3, "2. 请问毛利？"),
            _text(4, "答：保持稳定。"),
        ]

        units, _ = build_unit_drafts_s1_s7(
            {"elements": elements},
            filing_type="investor_relations",
            document_title="甲公司调研记录",
        )

        self.assertEqual([unit.payload_kind for unit in units], ["qa", "qa"])
        for index, unit in enumerate(units):
            graph = (unit.artifact_locator or {})["source_projection"]
            self.assertEqual(graph["payload"]["kind"], "text_partition")
            self.assertEqual(graph["payload"]["index"], index)
            self.assertEqual(graph["payload"]["count"], 2)
            self.assertEqual(
                graph["heading_path"][0]["selector"]["source"]["ir_id"],
                "ir_0000",
            )

    def test_numbered_business_prose_without_answers_is_not_qa(self) -> None:
        elements = [
            _text(0, "一、经营计划", kind="heading"),
            _text(1, "1. 拓展海外客户。"),
            _text(2, "2. 提升研发效率。"),
        ]

        units, _ = build_unit_drafts_s1_s7(
            {"elements": elements},
            filing_type="investor_relations",
            document_title="乙公司调研记录",
        )

        self.assertTrue(units)
        self.assertNotIn("qa", [unit.payload_kind for unit in units])
        self.assertIn("拓展海外客户", str(units[0].payload))


if __name__ == "__main__":
    unittest.main()
