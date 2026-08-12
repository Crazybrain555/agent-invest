"""Unit hash contract tests."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from disclosure_anchor.domain.services.unit_hashing import (
    canonical_json,
    compute_unit_hashes,
)
from disclosure_anchor.domain.value_objects.semantic_key import (
    validate_optional_semantic_key,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "unit_hashing"
    / "golden_hashes.json"
)


class UnitHashingTests(unittest.TestCase):
    def test_golden_hashes_are_stable(self) -> None:
        for case in json.loads(FIXTURE.read_text(encoding="utf-8")):
            with self.subTest(case=case["name"]):
                got = compute_unit_hashes(**case["input"])
                self.assertEqual(got.content_hash, case["expected"]["content_hash"])
                self.assertEqual(
                    got.query_projection_hash,
                    case["expected"]["query_projection_hash"],
                )
                self.assertEqual(got.structure_hash, case["expected"]["structure_hash"])

    def test_golden_cases_carry_a_valid_optional_semantic_key(self) -> None:
        for case in json.loads(FIXTURE.read_text(encoding="utf-8")):
            with self.subTest(case=case["name"]):
                validate_optional_semantic_key(case["input"]["semantic_key"])

    def test_canonical_json_shape_is_pinned(self) -> None:
        self.assertEqual(
            canonical_json({"b": "中文", "a": None}),
            '{"a":null,"b":"中文"}',
        )

    def test_canonical_json_rejects_non_finite_numbers(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_json({"value": value})

    def test_title_changes_only_query_projection_hash(self) -> None:
        base = {
            "payload_kind": "text",
            "payload": {"text": "正文"},
            "title": "原标题",
            "heading_path": ["一、业务"],
            "semantic_key": None,
            "quality_status": "ok",
            "order_index": 1,
        }
        first = compute_unit_hashes(**base)
        second = compute_unit_hashes(**{**base, "title": "新标题"})

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(first.query_projection_hash, second.query_projection_hash)
        self.assertEqual(first.structure_hash, second.structure_hash)

    def test_order_changes_only_structure_hash(self) -> None:
        base = {
            "payload_kind": "table",
            "payload": {"headers": ["项目"], "rows": [["收入"]]},
            "title": "主营业务",
            "heading_path": ["第二节 公司简介"],
            "semantic_key": None,
            "quality_status": "ok",
            "order_index": 1,
        }
        first = compute_unit_hashes(**base)
        second = compute_unit_hashes(**{**base, "order_index": 2})

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.query_projection_hash, second.query_projection_hash)
        self.assertNotEqual(first.structure_hash, second.structure_hash)

    def test_payload_changes_only_content_hash(self) -> None:
        base = {
            "payload_kind": "qa",
            "payload": {"question": "问题？", "answer": "回答", "raw_text": "问：问题？"},
            "title": "投资者关系活动",
            "heading_path": ["投资者关系活动记录表"],
            "semantic_key": None,
            "quality_status": "ok",
            "order_index": 1,
        }
        first = compute_unit_hashes(**base)
        second = compute_unit_hashes(
            **{**base, "payload": {**base["payload"], "answer": "新回答"}}
        )

        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.query_projection_hash, second.query_projection_hash)
        self.assertEqual(first.structure_hash, second.structure_hash)

    def test_mixed_annotations_are_projection_not_content_and_legacy_type_is_ignored(
        self,
    ) -> None:
        base = {
            "payload_kind": "mixed",
            "payload": {
                "semantic_type": "section",
                "parts": [
                    {
                        "kind": "text",
                        "order": 12,
                        "text": "正文",
                        "local_heading": ["（一）收入"],
                        "applicability": "applicable",
                    }
                ],
            },
            "title": "经营情况",
            "heading_path": ["第三节 管理层讨论与分析", "一、经营情况"],
            "semantic_key": "document_content",
            "quality_status": "ok",
            "order_index": 1,
        }
        first = compute_unit_hashes(**base)
        legacy_type_only = compute_unit_hashes(
            **{
                **base,
                "payload": {**base["payload"], "semantic_type": "document"},
            }
        )
        changed_payload = {
            **base["payload"],
            "parts": [
                {
                    **base["payload"]["parts"][0],
                    "order": 99,
                    "local_heading": ["（一）主营业务收入"],
                    "quality_status": "needs_review",
                }
            ],
        }
        second = compute_unit_hashes(
            **{**base, "payload": changed_payload}
        )

        self.assertEqual(first, legacy_type_only)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(first.query_projection_hash, second.query_projection_hash)
        self.assertEqual(first.structure_hash, second.structure_hash)

    def test_mixed_part_content_and_list_order_remain_content_identity(self) -> None:
        base = {
            "payload_kind": "mixed",
            "payload": {
                "semantic_type": "section",
                "parts": [
                    {"kind": "text", "order": 4, "text": "甲"},
                    {"kind": "text", "order": 9, "text": "乙"},
                ],
            },
            "title": "业务",
            "heading_path": ["一、业务"],
            "semantic_key": "document_content",
            "quality_status": "ok",
            "order_index": 1,
        }
        first = compute_unit_hashes(**base)
        changed_text = compute_unit_hashes(
            **{
                **base,
                "payload": {
                    **base["payload"],
                    "parts": [
                        {"kind": "text", "order": 4, "text": "甲变更"},
                        {"kind": "text", "order": 9, "text": "乙"},
                    ],
                },
            }
        )
        reversed_parts = compute_unit_hashes(
            **{
                **base,
                "payload": {
                    **base["payload"],
                    "parts": list(reversed(base["payload"]["parts"])),
                },
            }
        )

        self.assertNotEqual(first.content_hash, changed_text.content_hash)
        self.assertNotEqual(first.content_hash, reversed_parts.content_hash)

    def test_mixed_part_order_locator_is_not_identity(self) -> None:
        base = {
            "payload_kind": "mixed",
            "payload": {
                "semantic_type": "section",
                "parts": [
                    {"kind": "text", "order": 4, "text": "甲"},
                    {"kind": "text", "order": 9, "text": "乙"},
                ],
            },
            "title": "业务",
            "heading_path": ["一、业务"],
            "semantic_key": "document_content",
            "quality_status": "ok",
            "order_index": 1,
        }
        first = compute_unit_hashes(**base)
        second = compute_unit_hashes(
            **{
                **base,
                "payload": {
                    **base["payload"],
                    "parts": [
                        {"kind": "text", "order": 400, "text": "甲"},
                        {"kind": "text", "order": 900, "text": "乙"},
                    ],
                },
            }
        )

        self.assertEqual(first, second)

    def test_mixed_part_locator_is_provenance_but_grid_spans_are_content(self) -> None:
        common = {
            "payload_kind": "mixed",
            "title": "业务",
            "heading_path": ["一、业务"],
            "semantic_key": "document_content",
            "quality_status": "ok",
            "order_index": 1,
        }
        first = compute_unit_hashes(
            payload={
                "semantic_type": "section",
                "parts": [
                    {
                        "kind": "table",
                        "order": 4,
                        "headers": ["项目", "金额"],
                        "rows": [["甲", "1"]],
                        "merged_cells": [
                            {
                                "row": 0,
                                "col": 0,
                                "rowspan": 1,
                                "colspan": 2,
                            }
                        ],
                        "artifact_locator": {"page_no": 1},
                    }
                ],
            },
            **common,
        )
        second = compute_unit_hashes(
            payload={
                "semantic_type": "section",
                "parts": [
                    {
                        "kind": "table",
                        "order": 4,
                        "headers": ["项目", "金额"],
                        "rows": [["甲", "1"]],
                        "merged_cells": [],
                        "artifact_locator": {"page_no": 2},
                    }
                ],
            },
            **common,
        )

        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.query_projection_hash, second.query_projection_hash)
        self.assertEqual(first.structure_hash, second.structure_hash)

        locator_only = compute_unit_hashes(
            payload={
                "semantic_type": "section",
                "parts": [
                    {
                        "kind": "table",
                        "order": 4,
                        "headers": ["项目", "金额"],
                        "rows": [["甲", "1"]],
                        "merged_cells": [
                            {
                                "row": 0,
                                "col": 0,
                                "rowspan": 1,
                                "colspan": 2,
                            }
                        ],
                        "artifact_locator": {"page_no": 99},
                    }
                ],
            },
            **common,
        )
        self.assertEqual(first, locator_only)

    def test_mixed_parts_must_be_nonempty_objects(self) -> None:
        common = {
            "payload_kind": "mixed",
            "title": "业务",
            "heading_path": ["一、业务"],
            "semantic_key": "document_content",
            "quality_status": "ok",
            "order_index": 1,
        }
        for parts in (None, [], ["正文"]):
            with self.subTest(parts=parts):
                with self.assertRaises(ValueError):
                    compute_unit_hashes(
                        payload={"semantic_type": "section", "parts": parts},
                        **common,
                    )


if __name__ == "__main__":
    unittest.main()
