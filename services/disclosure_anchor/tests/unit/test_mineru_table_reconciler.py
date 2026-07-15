"""MinerU aggregate-table page-local restoration tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest

from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
)
from disclosure_anchor.adapters.parsers.mineru.table_reconciler import (
    TableReconciliationStats,
    reconcile_content_list_tables,
)
from disclosure_anchor.adapters.unit_builder.builder import (
    UnitDraft,
    build_unit_drafts_s1_s7,
)


def _table(page: int, bbox: list[float], html: str) -> dict[str, object]:
    return {
        "type": "table",
        "page_idx": page,
        "bbox": bbox,
        "table_body": html,
        "table_caption": [],
        "table_footnote": [],
    }


def _pipeline_page(
    page: int, bbox: list[float], html: str
) -> dict[str, object]:
    return {
        "page_info": {"page_no": page, "width": 1000, "height": 1000},
        "layout_dets": [{"label": "table", "bbox": bbox, "html": html}],
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _unit_semantics(content: list[dict[str, Any]]) -> list[tuple[object, ...]]:
    mapper = MinerUToNormalizedIRMapper()
    normalized = mapper.map_content_list(
        content_list=content,
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
            "document_id": "doc_reconcile",
            "source_pdf": "raw/sample.pdf",
            "title": "样本公告",
        },
    )
    units, _ = build_unit_drafts_s1_s7(
        normalized,
        filing_type="semiannual_report",
        document_title="样本公告",
    )
    return [
        (
            unit.payload_kind,
            unit.payload,
            unit.heading_path,
            unit.structural_path,
            unit.title,
            unit.semantic_key,
            unit.semantic_keys,
            unit.quality_status,
            unit.applicability,
        )
        for unit in units
    ]


class MinerUTableReconcilerTests(unittest.TestCase):
    def test_reconciliation_stats_reject_impossible_cross_field_states(self) -> None:
        valid = {
            "model_status": "supported",
            "content_tables": 2,
            "model_hash": "sha256:" + "a" * 64,
            "model_tables": 2,
            "uniquely_matched_tables": 2,
            "candidate_groups": 1,
            "proven_groups": 1,
            "restoration_rejected_groups": 1,
            "located_groups": 1,
            "located_tables": 2,
        }
        self.assertEqual(
            TableReconciliationStats(**valid).as_dict()["unresolved_groups"],
            1,
        )
        variants = {
            "candidate_formula": {"candidate_groups": 2},
            "proven_formula": {"restoration_rejected_groups": 0},
            "located_formula": {"located_groups": 0},
            "located_table_count": {"located_tables": 1},
            "unique_match_bound": {"uniquely_matched_tables": 1},
            "restored_table_bound": {
                "restoration_rejected_groups": 0,
                "restored_groups": 1,
                "restored_tables": 3,
            },
        }
        for label, override in variants.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    TableReconciliationStats(**{**valid, **override})

        for status in (
            "absent",
            "unreadable",
            "invalid_json",
            "unsupported_schema",
        ):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    TableReconciliationStats(
                        model_status=status,
                        content_tables=2,
                        restored_groups=1,
                    )

        for status, model_hash in (
            ("absent", "sha256:" + "a" * 64),
            ("unreadable", "sha256:" + "a" * 64),
            ("invalid_json", None),
            ("unsupported_schema", None),
            ("supported", "not-a-sha256"),
        ):
            with self.subTest(status=status, model_hash=model_hash):
                with self.assertRaises(ValueError):
                    TableReconciliationStats(
                        model_status=status,
                        content_tables=2,
                        model_hash=model_hash,
                    )

    def test_pipeline_restores_page_local_html_without_mutating_input(self) -> None:
        first = "<table><tr><th>项目</th></tr><tr><td>甲</td></tr></table>"
        second = "<table><tr><td>乙</td></tr></table>"
        third = "<table><tr><td>丙</td></tr></table>"
        aggregate = (
            "<table><tr><th>项目</th></tr><tr><td>甲</td></tr>"
            "<tr><td>乙</td></tr><tr><td>丙</td></tr></table>"
        )
        content = [
            _table(0, [100, 700, 900, 900], aggregate),
            _table(1, [100, 100, 900, 300], ""),
            _table(2, [100, 100, 900, 300], ""),
        ]
        content[1]["table_footnote"] = [""]
        content[2]["table_caption"] = [" "]
        original = copy.deepcopy(content)
        model = [
            _pipeline_page(0, [100, 700, 900, 900], first),
            _pipeline_page(1, [100, 100, 900, 300], second),
            _pipeline_page(2, [100, 100, 900, 300], third),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "sample_model.json"
            _write_json(model_path, model)
            result = reconcile_content_list_tables(content, model_path=model_path)

        self.assertEqual(content, original)
        self.assertEqual(
            [item["table_body"] for item in result.content_list],
            [first, second, third],
        )
        self.assertNotIn("_mineru_aggregate_table_locator", result.content_list[0])
        self.assertEqual(result.content_list[1]["table_footnote"], [])
        self.assertEqual(result.content_list[2]["table_caption"], [])
        self.assertEqual(result.stats.candidate_groups, 1)
        self.assertEqual(result.stats.proven_groups, 1)
        self.assertEqual(result.stats.located_groups, 1)
        self.assertEqual(result.stats.located_tables, 3)
        self.assertEqual(result.stats.restored_groups, 1)
        self.assertEqual(result.stats.restored_tables, 3)
        self.assertEqual(
            result.stats.as_dict()["unresolved_groups"],
            0,
        )

    def test_vlm_normalized_bboxes_attach_page_locator(self) -> None:
        first = "<table><tr><td>A</td></tr></table>"
        second = "<table><tr><td>B</td></tr></table>"
        aggregate = "<table><tr><td>A</td></tr><tr><td>B</td></tr></table>"
        content = [
            _table(0, [100, 700, 900, 900], aggregate),
            _table(1, [100, 100, 900, 300], ""),
        ]
        model = [
            [{"type": "table", "bbox": [0.1, 0.7, 0.9, 0.9], "content": first}],
            [{"type": "table", "bbox": [0.1, 0.1, 0.9, 0.3], "content": second}],
        ]

        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "sample_model.json"
            _write_json(model_path, model)
            result = reconcile_content_list_tables(content, model_path=model_path)

        self.assertEqual(
            [item["table_body"] for item in result.content_list],
            [first, second],
        )
        self.assertEqual(result.stats.restored_groups, 1)

    def test_absent_invalid_ambiguous_and_unproven_models_fail_closed(self) -> None:
        aggregate = "<table><tr><td>A</td></tr><tr><td>B</td></tr></table>"
        content = [
            _table(0, [100, 700, 900, 900], aggregate),
            _table(1, [100, 100, 900, 300], ""),
        ]
        absent = reconcile_content_list_tables(content, model_path=None)
        self.assertEqual(absent.content_list, content)
        self.assertEqual(absent.stats.model_status, "absent")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid_path = root / "invalid.json"
            invalid_path.write_text("not json", encoding="utf-8")
            invalid = reconcile_content_list_tables(content, model_path=invalid_path)
            self.assertEqual(invalid.content_list, content)
            self.assertEqual(invalid.stats.model_status, "invalid_json")

            first = "<table><tr><td>A</td></tr></table>"
            second = "<table><tr><td>B</td></tr></table>"
            ambiguous_path = root / "ambiguous.json"
            _write_json(
                ambiguous_path,
                [
                    {
                        "page_info": {"page_no": 0, "width": 1000, "height": 1000},
                        "layout_dets": [
                            {"label": "table", "bbox": [100, 700, 900, 900], "html": first},
                            {"label": "table", "bbox": [101, 700, 900, 900], "html": first},
                        ],
                    },
                    _pipeline_page(1, [100, 100, 900, 300], second),
                ],
            )
            ambiguous = reconcile_content_list_tables(content, model_path=ambiguous_path)
            self.assertEqual(ambiguous.content_list, content)
            self.assertGreater(ambiguous.stats.ambiguous_matches, 0)

            unproven_path = root / "unproven.json"
            _write_json(
                unproven_path,
                [
                    _pipeline_page(0, [100, 700, 900, 900], first),
                    _pipeline_page(
                        1,
                        [100, 100, 900, 300],
                        "<table><tr><td>C</td></tr></table>",
                    ),
                ],
            )
            unproven = reconcile_content_list_tables(content, model_path=unproven_path)
            self.assertEqual(unproven.content_list, content)
            self.assertEqual(unproven.stats.unproven_groups, 1)
            self.assertEqual(unproven.stats.located_tables, 0)

    def test_model_schema_and_bbox_validation_is_fail_closed(self) -> None:
        aggregate = "<table><tr><td>A</td></tr><tr><td>B</td></tr></table>"
        content = [
            _table(0, [100, 700, 900, 900], aggregate),
            _table(1, [100, 100, 900, 300], ""),
        ]
        valid_pages = [
            _pipeline_page(0, [100, 700, 900, 900], "<table><tr><td>A</td></tr></table>"),
            _pipeline_page(1, [100, 100, 900, 300], "<table><tr><td>B</td></tr></table>"),
        ]
        variants = [
            [{"page_info": {"page_no": 0}, "layout_dets": []}],
            [*valid_pages, {"page_info": {"page_no": 2, "width": 1000, "height": 1000}, "layout_dets": [{"label": "table", "bbox": [1, 2, 3], "html": "<table/>"}]}],
            [*valid_pages, {"page_info": {"page_no": 2, "width": 1000, "height": 1000}, "layout_dets": [{"label": "table", "bbox": [1, 2, 3, 4], "html": ""}]}],
        ]
        for model in variants:
            with self.subTest(model=model):
                with tempfile.TemporaryDirectory() as tmp:
                    model_path = Path(tmp) / "sample_model.json"
                    _write_json(model_path, model)
                    result = reconcile_content_list_tables(content, model_path=model_path)
                self.assertEqual(result.content_list, content)
                self.assertEqual(result.stats.model_status, "unsupported_schema")

        for bbox in ([True, 700, 900, 900], [100, float("nan"), 900, 900]):
            candidate = copy.deepcopy(content)
            candidate[0]["bbox"] = bbox
            with tempfile.TemporaryDirectory() as tmp:
                model_path = Path(tmp) / "sample_model.json"
                _write_json(model_path, valid_pages)
                result = reconcile_content_list_tables(candidate, model_path=model_path)
            self.assertEqual(result.content_list, candidate)
            self.assertEqual(result.stats.located_tables, 0)

    def test_interleaved_furniture_and_page_shapes_do_not_change_semantics(self) -> None:
        first = "<table><tr><th>项目</th><th>值</th></tr><tr><td>A</td><td>1</td></tr></table>"
        second = "<table><tr><td>B</td><td>2</td></tr></table>"
        aggregate = (
            "<table><tr><th>项目</th><th>值</th></tr>"
            "<tr><td>A</td><td>1</td></tr><tr><td>B</td><td>2</td></tr></table>"
        )
        content = [
            _table(0, [100, 700, 900, 900], aggregate),
            {
                "type": "header",
                "page_idx": 0,
                "text": "公司简称",
                "bbox": [100, 40, 500, 60],
            },
            _table(1, [100, 100, 900, 300], ""),
            {
                "type": "header",
                "page_idx": 1,
                "text": "公司简称",
                "bbox": [100, 40, 500, 60],
            },
            {"type": "page_number", "page_idx": 1, "text": "2"},
        ]
        model = [
            _pipeline_page(0, [100, 700, 900, 900], first),
            _pipeline_page(1, [100, 100, 900, 300], second),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "sample_model.json"
            _write_json(model_path, model)
            result = reconcile_content_list_tables(content, model_path=model_path)

        self.assertEqual(result.stats.located_groups, 1)
        self.assertEqual(result.stats.restored_groups, 1)
        self.assertEqual(result.content_list[0]["table_body"], first)
        self.assertEqual(result.content_list[2]["table_body"], second)

    def test_unique_furniture_and_substantive_text_stop_candidate_group(self) -> None:
        first = "<table><tr><td>A</td></tr></table>"
        second = "<table><tr><td>B</td></tr></table>"
        aggregate = "<table><tr><td>A</td></tr><tr><td>B</td></tr></table>"
        blockers = [
            {
                "type": "header",
                "page_idx": 0,
                "text": "仅此一处的标题",
                "bbox": [100, 40, 500, 60],
            },
            {
                "type": "text",
                "page_idx": 0,
                "text": "真实正文不能被表格 locator 跨过。",
                "bbox": [100, 700, 500, 730],
            },
        ]
        model = [
            _pipeline_page(0, [100, 700, 900, 900], first),
            _pipeline_page(1, [100, 100, 900, 300], second),
        ]
        for blocker in blockers:
            with self.subTest(kind=blocker["type"]):
                content: list[dict[str, Any]] = [
                    _table(0, [100, 700, 900, 900], aggregate),
                    blocker,
                    _table(1, [100, 100, 900, 300], ""),
                ]
                with tempfile.TemporaryDirectory() as tmp:
                    model_path = Path(tmp) / "sample_model.json"
                    _write_json(model_path, model)
                    result = reconcile_content_list_tables(
                        content, model_path=model_path
                    )
                self.assertEqual(result.content_list, content)
                self.assertEqual(result.stats.candidate_groups, 0)
                self.assertEqual(result.stats.restored_groups, 0)

    def test_repeated_structural_caption_is_not_running_furniture(self) -> None:
        first = "<table><tr><td>A</td></tr></table>"
        second = "<table><tr><td>B</td></tr></table>"
        aggregate = "<table><tr><td>A</td></tr><tr><td>B</td></tr></table>"
        model = [
            _pipeline_page(0, [100, 700, 900, 900], first),
            _pipeline_page(1, [100, 100, 900, 300], second),
        ]
        for text in (
            "合并利润表",
            "2025年半年度财务报表附注",
            "第十节财务报告",
            "二、财务报表",
            "补充资料",
            "合并财务报表主要项目注释",
            "母公司财务报表重要项目附注",
        ):
            with self.subTest(text=text):
                statement_header: dict[str, Any] = {
                    "type": "header",
                    "text": text,
                    "bbox": [400, 40, 600, 60],
                }
                content: list[dict[str, Any]] = [
                    _table(0, [100, 700, 900, 900], aggregate),
                    {**statement_header, "page_idx": 0},
                    _table(1, [100, 100, 900, 300], ""),
                    {**statement_header, "page_idx": 1},
                ]
                with tempfile.TemporaryDirectory() as tmp:
                    model_path = Path(tmp) / "sample_model.json"
                    _write_json(model_path, model)
                    result = reconcile_content_list_tables(
                        content, model_path=model_path
                    )
                self.assertEqual(result.content_list, content)
                self.assertEqual(result.stats.candidate_groups, 0)

    def test_visual_statement_header_after_ghost_keeps_locator_only(self) -> None:
        first = "<table><tr><td>A</td></tr></table>"
        second = "<table><tr><td>B</td></tr></table>"
        aggregate = "<table><tr><td>A</td></tr><tr><td>B</td></tr></table>"
        model = [
            _pipeline_page(0, [100, 700, 900, 900], first),
            _pipeline_page(1, [100, 100, 900, 300], second),
        ]
        variants: list[list[dict[str, Any]]] = [
            [
                _table(0, [100, 700, 900, 900], aggregate),
                _table(1, [100, 100, 900, 300], ""),
                {
                    "type": "header",
                    "page_idx": 1,
                    "text": "合并利润表",
                    "bbox": [100, 40, 900, 60],
                },
            ],
            [
                _table(0, [100, 700, 900, 900], aggregate),
                _table(1, [100, 100, 900, 300], ""),
                {
                    "type": "header",
                    "page_idx": 0,
                    "text": "安徽大地熊新材料股份有限公司2025年半年度合并利润表",
                    "bbox": [100, 40, 900, 60],
                },
                {
                    "type": "header",
                    "page_idx": 1,
                    "text": "安徽大地熊新材料股份有限公司2025年半年度合并利润表",
                    "bbox": [100, 40, 900, 60],
                },
            ],
            [
                _table(0, [100, 700, 900, 900], aggregate),
                _table(1, [100, 100, 900, 300], ""),
                {
                    "type": "page_number",
                    "page_idx": 1,
                    "text": "未经审计合并利润表",
                    "bbox": [100, 40, 900, 60],
                },
            ],
            [
                _table(0, [100, 700, 900, 900], aggregate),
                {
                    "type": "page_number",
                    "page_idx": 1,
                    "text": "未经审计财务报表补充资料",
                    "bbox": [100, 900, 900, 930],
                },
                _table(1, [100, 100, 900, 300], ""),
            ],
            [
                _table(0, [100, 700, 900, 900], aggregate),
                {
                    "type": "page_number",
                    "page_idx": 1,
                    "text": "未经审计财务报表补充资料",
                    "bbox": [910, 40, 990, 60],
                },
                _table(1, [100, 100, 900, 300], ""),
            ],
            [
                _table(0, [100, 700, 900, 900], aggregate),
                {
                    "type": "header",
                    "page_idx": 0,
                    "text": "货币资金",
                    "bbox": [50, 40, 250, 60],
                },
                {
                    "type": "page_number",
                    "page_idx": 1,
                    "text": "1",
                    "bbox": [20, 40, 40, 60],
                },
                _table(1, [100, 100, 900, 300], ""),
                {
                    "type": "header",
                    "page_idx": 1,
                    "text": "货币资金",
                    "bbox": [50, 40, 250, 60],
                },
            ],
        ]

        for content in variants:
            with self.subTest(
                title=next(
                    str(item["text"]) for item in content if item.get("text")
                )
            ):
                with tempfile.TemporaryDirectory() as tmp:
                    model_path = Path(tmp) / "sample_model.json"
                    _write_json(model_path, model)
                    result = reconcile_content_list_tables(
                        content, model_path=model_path
                    )
                self.assertEqual(result.stats.candidate_groups, 1)
                self.assertEqual(result.stats.proven_groups, 1)
                self.assertEqual(result.stats.restored_groups, 0)
                self.assertEqual(result.stats.restoration_rejected_groups, 1)
                self.assertEqual(
                    _unit_semantics(result.content_list),
                    _unit_semantics(content),
                )
                self.assertNotIn(
                    "公告头信息", repr(_unit_semantics(result.content_list))
                )

    def test_nonblank_continuation_metadata_keeps_locator_only(self) -> None:
        first = "<table><tr><td>A</td></tr></table>"
        second = "<table><tr><td>B</td></tr></table>"
        aggregate = "<table><tr><td>A</td></tr><tr><td>B</td></tr></table>"
        content = [
            _table(0, [100, 700, 900, 900], aggregate),
            _table(1, [100, 100, 900, 300], ""),
        ]
        content[1]["table_footnote"] = ["真实脚注"]
        model = [
            _pipeline_page(0, [100, 700, 900, 900], first),
            _pipeline_page(1, [100, 100, 900, 300], second),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "sample_model.json"
            _write_json(model_path, model)
            result = reconcile_content_list_tables(content, model_path=model_path)
        self.assertEqual(result.content_list[0]["table_body"], aggregate)
        self.assertEqual(result.content_list[1]["table_body"], "")
        locator = result.content_list[0]["_mineru_aggregate_table_locator"]
        self.assertEqual(locator["page_span"], [1, 2])
        self.assertEqual(result.stats.located_groups, 1)
        self.assertEqual(result.stats.restored_groups, 0)
        self.assertEqual(result.stats.restoration_rejected_groups, 1)
        self.assertEqual(result.stats.as_dict()["unresolved_groups"], 1)

    def test_left_margin_page_number_below_table_keeps_locator_only(self) -> None:
        first = "<table><tr><td>A</td></tr></table>"
        second = "<table><tr><td>B</td></tr></table>"
        aggregate = "<table><tr><td>A</td></tr><tr><td>B</td></tr></table>"
        content = [
            _table(0, [100, 700, 900, 900], aggregate),
            {
                "type": "header",
                "page_idx": 0,
                "text": "货币资金",
                "bbox": [50, 140, 250, 160],
            },
            {
                "type": "page_number",
                "page_idx": 1,
                "text": "1",
                "bbox": [20, 140, 40, 160],
            },
            {
                "type": "header",
                "page_idx": 1,
                "text": "货币资金",
                "bbox": [50, 140, 250, 160],
            },
            _table(1, [100, 20, 900, 100], ""),
        ]
        model = [
            _pipeline_page(0, [100, 700, 900, 900], first),
            _pipeline_page(1, [100, 20, 900, 100], second),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "sample_model.json"
            _write_json(model_path, model)
            result = reconcile_content_list_tables(content, model_path=model_path)

        self.assertEqual(result.stats.candidate_groups, 1)
        self.assertEqual(result.stats.proven_groups, 1)
        self.assertEqual(result.stats.restored_groups, 0)
        self.assertEqual(result.stats.restoration_rejected_groups, 1)
        self.assertEqual(_unit_semantics(result.content_list), _unit_semantics(content))

    def test_aside_text_split_prefix_after_ghost_keeps_locator_only(self) -> None:
        first = "<table><tr><td>A</td></tr></table>"
        second = "<table><tr><td>B</td></tr></table>"
        aggregate = "<table><tr><td>A</td></tr><tr><td>B</td></tr></table>"
        content = [
            _table(0, [100, 700, 900, 900], aggregate),
            _table(1, [100, 100, 900, 300], ""),
            {
                "type": "aside_text",
                "page_idx": 1,
                "text": "1",
                "bbox": [20, 40, 40, 60],
            },
            {
                "type": "text",
                "text_level": 1,
                "page_idx": 1,
                "text": "货币资金",
                "bbox": [50, 40, 250, 60],
            },
        ]
        model = [
            _pipeline_page(0, [100, 700, 900, 900], first),
            _pipeline_page(1, [100, 100, 900, 300], second),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "sample_model.json"
            _write_json(model_path, model)
            result = reconcile_content_list_tables(content, model_path=model_path)

        self.assertEqual(result.stats.candidate_groups, 1)
        self.assertEqual(result.stats.proven_groups, 1)
        self.assertEqual(result.stats.restored_groups, 0)
        self.assertEqual(result.stats.restoration_rejected_groups, 1)
        self.assertEqual(_unit_semantics(result.content_list), _unit_semantics(content))

    def test_continuation_header_cells_keep_locator_only(self) -> None:
        first = "<table><tr><td>A</td></tr></table>"
        second = "<table><tr><th>B</th></tr></table>"
        aggregate = "<table><tr><td>A</td></tr><tr><th>B</th></tr></table>"
        content = [
            _table(0, [100, 700, 900, 900], aggregate),
            _table(1, [100, 100, 900, 300], ""),
        ]
        model = [
            _pipeline_page(0, [100, 700, 900, 900], first),
            _pipeline_page(1, [100, 100, 900, 300], second),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "sample_model.json"
            _write_json(model_path, model)
            result = reconcile_content_list_tables(content, model_path=model_path)
        self.assertEqual(result.content_list[0]["table_body"], aggregate)
        self.assertEqual(result.content_list[1]["table_body"], "")
        self.assertEqual(result.stats.proven_groups, 1)
        self.assertEqual(result.stats.restored_groups, 0)
        self.assertEqual(result.stats.restoration_rejected_groups, 1)

    def test_span_semantics_must_match_aggregate_exactly(self) -> None:
        first = "<table><tr><td rowspan='2'>A</td><td>1</td></tr><tr><td>2</td></tr></table>"
        second = "<table><tr><td>B</td><td>3</td></tr></table>"
        aggregate = "<table><tr><td>A</td><td>1</td></tr><tr><td>2</td></tr><tr><td>B</td><td>3</td></tr></table>"
        content = [
            _table(0, [100, 700, 900, 900], aggregate),
            _table(1, [100, 100, 900, 300], ""),
        ]
        model = [
            _pipeline_page(0, [100, 700, 900, 900], first),
            _pipeline_page(1, [100, 100, 900, 300], second),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "sample_model.json"
            _write_json(model_path, model)
            result = reconcile_content_list_tables(content, model_path=model_path)

        self.assertEqual(result.content_list, content)
        self.assertEqual(result.stats.unproven_groups, 1)

    def test_merged_cell_coordinates_remerge_exactly(self) -> None:
        first = (
            "<table><tr><th>项目</th><th>值</th></tr>"
            "<tr><td>A</td><td>1</td></tr></table>"
        )
        second = "<table><tr><td colspan='2'>B</td></tr></table>"
        aggregate = (
            "<table><tr><th>项目</th><th>值</th></tr>"
            "<tr><td>A</td><td>1</td></tr>"
            "<tr><td colspan='2'>B</td></tr></table>"
        )
        content = [
            _table(0, [100, 700, 900, 900], aggregate),
            _table(1, [100, 100, 900, 300], ""),
        ]
        model = [
            _pipeline_page(0, [100, 700, 900, 900], first),
            _pipeline_page(1, [100, 100, 900, 300], second),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "sample_model.json"
            _write_json(model_path, model)
            result = reconcile_content_list_tables(content, model_path=model_path)

        self.assertEqual(result.stats.proven_groups, 1)
        self.assertEqual(result.stats.restored_groups, 1)
        self.assertEqual(result.stats.restoration_rejected_groups, 0)
        self.assertEqual(_unit_semantics(result.content_list), _unit_semantics(content))

    def test_page_local_mapping_remerges_to_identical_builder_semantics(self) -> None:
        first = "<table><tr><td>项目</td><td>A</td></tr></table>"
        second = "<table><tr><td>项目</td><td>B</td></tr></table>"
        aggregate = (
            "<table><tr><td>项目</td><td>A</td></tr>"
            "<tr><td>项目</td><td>B</td></tr></table>"
        )
        content = [
            _table(0, [100, 700, 900, 900], aggregate),
            _table(1, [100, 100, 900, 300], ""),
        ]
        model = [
            _pipeline_page(0, [100, 700, 900, 900], first),
            _pipeline_page(1, [100, 100, 900, 300], second),
        ]
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
            "document_id": "doc_reconcile",
            "source_pdf": "raw/sample.pdf",
            "title": "样本公告",
        }

        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "sample_model.json"
            _write_json(model_path, model)
            result = reconcile_content_list_tables(content, model_path=model_path)
            second_pass = reconcile_content_list_tables(
                result.content_list, model_path=model_path
            )

        self.assertEqual(second_pass.content_list, result.content_list)
        before_ir = mapper.map_content_list(
            content_list=content,
            parser_info=parser_info,
            document_metadata=metadata,
        )
        after_ir = mapper.map_content_list(
            content_list=result.content_list,
            parser_info=parser_info,
            document_metadata=metadata,
        )
        self.assertEqual(after_ir["elements"][0]["table_html"], first)
        self.assertEqual(after_ir["elements"][1]["table_html"], second)
        self.assertNotIn("page_span", after_ir["elements"][0])
        self.assertEqual(result.stats.restored_groups, 1)

        before_units, _ = build_unit_drafts_s1_s7(before_ir, filing_type="other")
        after_units, _ = build_unit_drafts_s1_s7(after_ir, filing_type="other")
        def semantic(unit: UnitDraft) -> tuple[object, ...]:
            return (
                unit.payload_kind,
                unit.payload,
                unit.heading_path,
                unit.title,
                unit.semantic_key,
                unit.semantic_keys,
                unit.quality_status,
                unit.applicability,
            )

        self.assertEqual(
            [semantic(unit) for unit in before_units],
            [semantic(unit) for unit in after_units],
        )
        locator = after_units[0].artifact_locator
        self.assertIsNotNone(locator)
        assert locator is not None
        self.assertEqual(locator["page_span"], [1, 2])


if __name__ == "__main__":
    unittest.main()
