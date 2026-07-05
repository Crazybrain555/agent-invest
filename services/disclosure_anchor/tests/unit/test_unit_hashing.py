"""Unit hash contract tests."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from disclosure_anchor.domain.services.unit_hashing import (
    canonical_json,
    compute_unit_hashes,
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

    def test_canonical_json_shape_is_pinned(self) -> None:
        self.assertEqual(
            canonical_json({"b": "中文", "a": None}),
            '{"a":null,"b":"中文"}',
        )

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


if __name__ == "__main__":
    unittest.main()
