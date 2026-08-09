"""06R search projection: tokenizer + deterministic row computation (no DB).

Covers the pure, DB-free surface of milestone 06R: the pinned jieba tokenizer
and source-bound SearchAtom replay. Body fields are declared by the unit source
projection; they are never discovered by recursively walking payload JSON.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import unittest

from disclosure_anchor.adapters.retrieval import tokenizer
from disclosure_anchor.application.contracts.unit_source_projection import (
    SearchTargetContractError,
    materialize_search_projection,
    search_text_values,
)
from disclosure_anchor.application.use_cases.build_search_projection import (
    compute_search_projection_row,
)
from tests.unit.test_unit_builder import (
    _build,
    _element,
    _sample_share_change,
)

_BUILT_AT = datetime(2026, 7, 17, tzinfo=timezone.utc)


def _search_locator(
    *,
    projection_kind: str,
    projection_target: str,
    targets: list[tuple[str, str]],
) -> dict[str, object]:
    return {
        "source_projection": {
            "version": "unit-source-projection.v4",
            "payload": {
                "kind": projection_kind,
                "sources": [{"source": {}, "field": {}}],
                "target_field": projection_target,
                "transform": "test",
            },
            "heading_path": [],
            "structured": [
                {
                    "kind": "derived_field",
                    "source": {},
                    "target_field": target_field,
                    "transform": "identity.v1",
                }
                for target_field, role in targets
                if role == "structured"
            ],
            "provenance": [],
            "search_targets": [target_field for target_field, _role in targets],
            "search_atoms": [],
            "physical_context": None,
        }
    }


class TokenizerTests(unittest.TestCase):
    def test_index_and_query_analyzers_are_deterministic(self) -> None:
        first = tokenizer.index_word_tokens("应收账款账龄分析")
        second = tokenizer.index_word_tokens("应收账款账龄分析")
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertEqual(
            tokenizer.query_word_tokens("应收账款账龄分析"),
            tokenizer.query_word_tokens("应收账款账龄分析"),
        )

    def test_analyzers_normalize_width_and_case_and_empty(self) -> None:
        # NFKC folds full-width forms; casefold lowercases; blank -> "".
        self.assertEqual(
            tokenizer.normalize_search_text("ＡＢＣ％ＤＥＦ＿ＧＨ＼Ｉ"),
            "abc%def_gh\\i",
        )
        self.assertEqual(
            tokenizer.index_word_tokens("１２３"),
            tokenizer.index_word_tokens("123"),
        )
        self.assertIn("abc", tokenizer.index_word_tokens("ABC def").split())
        self.assertEqual(tokenizer.index_word_tokens("   "), "")
        self.assertEqual(tokenizer.query_word_tokens("   "), ())

    def test_search_mode_index_contains_exact_query_subterms(self) -> None:
        indexed = set(
            tokenizer.index_word_tokens("股份变动及股东情况").split()
        )
        for query in ("股份变动", "股东情况"):
            self.assertLessEqual(
                set(tokenizer.query_word_tokens(query)),
                indexed,
            )

    def test_query_groups_have_no_content_alias_expansion(self) -> None:
        self.assertEqual(
            tokenizer.build_search_tsquery_groups("商誉减值"),
            ("'商誉'", "'减值'"),
        )
        self.assertEqual(
            tokenizer.build_search_tsquery("商誉减值"), "'商誉' & '减值'"
        )
        self.assertEqual(tokenizer.build_search_tsquery_groups("  "), ())
        self.assertEqual(tokenizer.build_search_tsquery("  "), "")
        self.assertEqual(tokenizer.build_search_tsquery("半年报"), "'半年报'")
        self.assertNotIn("半年度", tokenizer.build_search_tsquery("半年报"))
        self.assertIn(
            "'\\\\'",
            tokenizer.build_search_tsquery_groups("ＡＢＣ％ＤＥＦ＿ＧＨ＼Ｉ"),
        )


class SearchTargetTests(unittest.TestCase):
    def test_search_plan_binds_transforms_and_grouping_not_provenance(self) -> None:
        text_payload = {"text": "甲\n乙"}
        ordinary_locator = _search_locator(
            projection_kind="text_identity",
            projection_target="payload.text",
            targets=[("payload.text", "payload")],
        )
        safe_locator = copy.deepcopy(ordinary_locator)
        safe_locator["source_projection"]["payload"]["transform"] = (
            "safe_text.v1"
        )
        ordinary = materialize_search_projection(
            payload_kind="text",
            payload=text_payload,
            artifact_locator=ordinary_locator,
        )
        safe = materialize_search_projection(
            payload_kind="text",
            payload=text_payload,
            artifact_locator=safe_locator,
        )
        self.assertEqual(ordinary.values, ("甲\n乙",))
        self.assertEqual(safe.values, ("甲", "乙"))
        self.assertNotEqual(ordinary.plan, safe.plan)

        parts = [
            {
                "kind": "text",
                "text": value,
                "artifact_locator": _search_locator(
                    projection_kind="text_identity_exact",
                    projection_target="payload.text",
                    targets=[("payload.text", "payload")],
                ),
            }
            for value in ("股", "份变动")
        ]
        mixed_payload = {"semantic_type": "document", "parts": parts}
        ungrouped_locator = _search_locator(
            projection_kind="container",
            projection_target="payload.parts",
            targets=[],
        )
        grouped_locator = copy.deepcopy(ungrouped_locator)
        grouped_locator["source_projection"]["search_atoms"] = [
            {
                "boundary": {
                    "kind": "source_evidence_run",
                    "source_evidence_sha256": "sha256:" + "a" * 64,
                    "page_idx": 0,
                    "run_index": 0,
                },
                "target_fields": [
                    "payload.parts.0.text",
                    "payload.parts.1.text",
                ],
                "transform": "exact_concat.v1",
            }
        ]
        ungrouped = materialize_search_projection(
            payload_kind="mixed",
            payload=mixed_payload,
            artifact_locator=ungrouped_locator,
        )
        grouped = materialize_search_projection(
            payload_kind="mixed",
            payload=mixed_payload,
            artifact_locator=grouped_locator,
        )
        self.assertEqual(ungrouped.values, ("股", "份变动"))
        self.assertEqual(grouped.values, ("股份变动",))
        self.assertNotEqual(ungrouped.plan, grouped.plan)

        inactive_child_transform = copy.deepcopy(mixed_payload)
        inactive_child_transform["parts"][0]["artifact_locator"][
            "source_projection"
        ]["payload"]["transform"] = "safe_text.v1"
        same_grouped = materialize_search_projection(
            payload_kind="mixed",
            payload=inactive_child_transform,
            artifact_locator=grouped_locator,
        )
        self.assertEqual(same_grouped, grouped)

        provenance_only = copy.deepcopy(grouped_locator)
        boundary = provenance_only["source_projection"]["search_atoms"][0][
            "boundary"
        ]
        boundary["source_evidence_sha256"] = "sha256:" + "b" * 64
        boundary["page_idx"] = 8
        boundary["run_index"] = 3
        provenance_materialized = materialize_search_projection(
            payload_kind="mixed",
            payload=mixed_payload,
            artifact_locator=provenance_only,
        )
        self.assertEqual(provenance_materialized, grouped)

        invalid = copy.deepcopy(grouped_locator)
        invalid["source_projection"]["search_atoms"][0]["boundary"][
            "source_evidence_sha256"
        ] = "not-a-hash"
        with self.assertRaises(SearchTargetContractError):
            materialize_search_projection(
                payload_kind="mixed",
                payload=mixed_payload,
                artifact_locator=invalid,
            )

    def test_non_primary_source_alternative_has_no_second_search_leaf(self) -> None:
        payload = {
            "text": "50",
            "representation_role": "unresolved_source_alternative",
            "search_policy": "none",
        }
        locator = _search_locator(
            projection_kind="text_identity_exact",
            projection_target="payload.text",
            targets=[],
        )

        self.assertEqual(
            search_text_values(
                payload_kind="text",
                payload=payload,
                artifact_locator=locator,
            ),
            (),
        )

        primary = copy.deepcopy(locator)
        primary["source_projection"]["search_targets"] = ["payload.text"]
        with self.assertRaises(SearchTargetContractError):
            search_text_values(
                payload_kind="text",
                payload=payload,
                artifact_locator=primary,
            )
        malformed = {**payload, "search_policy": "primary"}
        with self.assertRaises(SearchTargetContractError):
            search_text_values(
                payload_kind="text",
                payload=malformed,
                artifact_locator=locator,
            )

    def test_text_has_one_explicit_payload_target(self) -> None:
        (unit,), _ = _build([_element(0, text="货币资金明细")])

        self.assertEqual(
            search_text_values(
                payload_kind=unit.payload_kind,
                payload=unit.payload,
                artifact_locator=unit.artifact_locator,
            ),
            ("货币资金明细",),
        )
        graph = unit.artifact_locator["source_projection"]
        self.assertEqual(
            graph["search_targets"],
            ["payload.text"],
        )
        for typed_element in (
            _element(
                0,
                text="第一项",
                raw_kind="list",
                list_items=["第一项"],
                list_subtype="ordered",
            ),
            _element(
                0,
                text="print('x')",
                raw_kind="code",
                code_body="print('x')",
                code_caption=[],
                code_footnote=[],
            ),
        ):
            with self.subTest(raw_kind=typed_element["raw_kind"]):
                (typed_unit,), _ = _build([typed_element])
                self.assertEqual(
                    typed_unit.artifact_locator["source_projection"][
                        "search_targets"
                    ],
                    ["payload.text"],
                )

        unsafe_text = "福建\ue000表格\ufffd数值⟦未解码字形 cid=9⟧尾部"
        (unsafe_unit,), _ = _build([_element(0, text=unsafe_text)])
        self.assertEqual(unsafe_unit.payload["text"], "福建\n表格\n数值\n尾部")
        self.assertNotIn("\ue000", unsafe_unit.payload["text"])
        self.assertNotIn("\ufffd", unsafe_unit.payload["text"])
        self.assertNotIn("未解码字形", unsafe_unit.payload["text"])
        self.assertEqual(
            search_text_values(
                payload_kind=unsafe_unit.payload_kind,
                payload=unsafe_unit.payload,
                artifact_locator=unsafe_unit.artifact_locator,
            ),
            ("福建", "表格", "数值", "尾部"),
        )
        row = compute_search_projection_row(
            asset_id="ua_unsafe_glyph_segments",
            title=None,
            heading_path=[],
            payload_kind=unsafe_unit.payload_kind,
            payload=unsafe_unit.payload,
            semantic_keys=["document_content"],
            artifact_locator=unsafe_unit.artifact_locator,
            built_at=_BUILT_AT,
        )
        self.assertEqual(row["body_atoms"], ("福建", "表格", "数值", "尾部"))
        self.assertNotIn("\ue000", row["body_tokens"])
        self.assertNotIn("\ufffd", row["body_tokens"])
        self.assertNotIn("未解码字形", row["body_tokens"])

        missing = copy.deepcopy(unit.artifact_locator)
        missing["source_projection"]["search_targets"] = []
        with self.assertRaises(SearchTargetContractError):
            search_text_values(
                payload_kind=unit.payload_kind,
                payload=unit.payload,
                artifact_locator=missing,
            )

    def test_table_indexes_only_source_owned_evidence_fields(self) -> None:
        (unit,), _ = _build(
            [
                _element(
                    0,
                    kind="table",
                    raw_kind="table",
                    table_caption=["应收账款账龄"],
                    table_footnote=["注1"],
                    table={
                        "headers": ["账龄", "金额"],
                        "rows": [["1年以内", "100"]],
                        "merged_cells": [],
                    },
                )
            ]
        )
        unit.payload.update(
            {
                "context": "CONTEXT_SENTINEL",
                "metadata": "METADATA_SENTINEL",
                "raw_html": "<b>RAW_SENTINEL</b>",
            }
        )
        body = " ".join(
            search_text_values(
                payload_kind=unit.payload_kind,
                payload=unit.payload,
                artifact_locator=unit.artifact_locator,
            )
        )

        for expected in ("应收账款账龄", "账龄", "金额", "1年以内", "100", "注1"):
            self.assertIn(expected, body)
        for excluded in (
            "CONTEXT_SENTINEL",
            "METADATA_SENTINEL",
            "RAW_SENTINEL",
            str(unit.payload.get("unit") or ""),
        ):
            if excluded:
                self.assertNotIn(excluded, body)

        missing_rows = copy.deepcopy(unit.artifact_locator)
        missing_rows["source_projection"]["search_targets"].remove(
            "payload.rows"
        )
        with self.assertRaises(SearchTargetContractError):
            search_text_values(
                payload_kind=unit.payload_kind,
                payload=unit.payload,
                artifact_locator=missing_rows,
            )

    def test_mixed_container_has_no_targets_and_replays_parts_in_order(self) -> None:
        elements, headings = _sample_share_change()
        units, _ = _build(elements, headings=headings)
        (unit,) = units

        self.assertEqual(
            unit.artifact_locator["source_projection"]["search_targets"],
            [],
        )
        values = search_text_values(
            payload_kind="mixed",
            payload=unit.payload,
            artifact_locator=unit.artifact_locator,
        )
        self.assertIn("单位：股", " ".join(values))
        self.assertIn("股份总数", " ".join(values))
        row = compute_search_projection_row(
            asset_id="ua_mixed_atoms",
            title=unit.title,
            heading_path=unit.heading_path,
            payload_kind=unit.payload_kind,
            payload=unit.payload,
            semantic_keys=unit.semantic_keys,
            artifact_locator=unit.artifact_locator,
            built_at=_BUILT_AT,
        )
        self.assertEqual(
            row["body_atoms"],
            tuple(
                tokenizer.normalize_search_text(value)
                for value in values
                if tokenizer.normalize_search_text(value).strip()
            ),
        )
        self.assertNotIn(" ".join(values), row["body_atoms"])

    def test_native_retrieval_run_rejoins_only_proved_split_words(self) -> None:
        parts = [
            {
                "kind": "text",
                "text": text,
                "artifact_locator": _search_locator(
                    projection_kind="text_identity_exact",
                    projection_target="payload.text",
                    targets=[("payload.text", "payload")],
                ),
            }
            for text in ("股", "份变动")
        ]
        locator = _search_locator(
            projection_kind="container",
            projection_target="payload.parts",
            targets=[],
        )
        locator["source_projection"]["search_atoms"] = [
            {
                "boundary": {
                    "kind": "source_evidence_run",
                    "source_evidence_sha256": "sha256:" + "a" * 64,
                    "page_idx": 0,
                    "run_index": 0,
                },
                "target_fields": [
                    "payload.parts.0.text",
                    "payload.parts.1.text",
                ],
                "transform": "exact_concat.v1",
            }
        ]
        payload = {"semantic_type": "document", "parts": parts}

        self.assertEqual(
            search_text_values(
                payload_kind="mixed",
                payload=payload,
                artifact_locator=locator,
            ),
            ("股份变动",),
        )
        row = compute_search_projection_row(
            asset_id="ua_native_split_run",
            title=None,
            heading_path=[],
            payload_kind="mixed",
            payload=payload,
            semantic_keys=["document_content"],
            artifact_locator=locator,
            built_at=_BUILT_AT,
        )
        self.assertEqual(row["body_atoms"], ("股份变动",))

    def test_grouped_search_atoms_reject_unproved_or_reordered_joins(self) -> None:
        parts = [
            {
                "kind": "text",
                "text": text,
                "artifact_locator": _search_locator(
                    projection_kind="text_identity_exact",
                    projection_target="payload.text",
                    targets=[("payload.text", "payload")],
                ),
            }
            for text in ("甲", "乙", "丙")
        ]
        payload = {"semantic_type": "document", "parts": parts}
        boundary = {
            "kind": "source_evidence_run",
            "source_evidence_sha256": "sha256:" + "a" * 64,
            "page_idx": 0,
            "run_index": 0,
        }
        invalid_atoms = (
            [
                {
                    "boundary": {"kind": "source_occurrence_singleton"},
                    "target_fields": [
                        "payload.parts.0.text",
                        "payload.parts.1.text",
                    ],
                    "transform": "exact_concat.v1",
                },
                {
                    "boundary": {"kind": "source_occurrence_singleton"},
                    "target_fields": ["payload.parts.2.text"],
                    "transform": "exact_concat.v1",
                },
            ],
            [
                {
                    "boundary": boundary,
                    "target_fields": [
                        "payload.parts.0.text",
                        "payload.parts.2.text",
                    ],
                    "transform": "exact_concat.v1",
                },
                {
                    "boundary": {"kind": "source_occurrence_singleton"},
                    "target_fields": ["payload.parts.1.text"],
                    "transform": "exact_concat.v1",
                },
            ],
            [
                {
                    "boundary": boundary,
                    "target_fields": ["payload.parts.0.text"],
                    "transform": "exact_concat.v1",
                },
                {
                    "boundary": boundary,
                    "target_fields": [
                        "payload.parts.1.text",
                        "payload.parts.2.text",
                    ],
                    "transform": "exact_concat.v1",
                },
            ],
        )
        for search_atoms in invalid_atoms:
            with self.subTest(search_atoms=search_atoms):
                locator = _search_locator(
                    projection_kind="container",
                    projection_target="payload.parts",
                    targets=[],
                )
                locator["source_projection"]["search_atoms"] = search_atoms
                with self.assertRaises(SearchTargetContractError):
                    search_text_values(
                        payload_kind="mixed",
                        payload=payload,
                        artifact_locator=locator,
                    )

        table = {
            "kind": "table",
            "caption": [],
            "headers": ["项目"],
            "rows": [["金额"]],
            "notes": [],
            "artifact_locator": _search_locator(
                projection_kind="table_identity",
                projection_target="payload",
                targets=[
                    ("payload.caption", "payload"),
                    ("payload.headers", "payload"),
                    ("payload.rows", "payload"),
                    ("payload.notes", "payload"),
                ],
            ),
        }
        with_table = {**payload, "parts": [*parts, table]}
        locator = _search_locator(
            projection_kind="container",
            projection_target="payload.parts",
            targets=[],
        )
        locator["source_projection"]["search_atoms"] = [
            {
                "boundary": boundary,
                "target_fields": [
                    "payload.parts.0.text",
                    "payload.parts.1.text",
                    "payload.parts.2.text",
                ],
                "transform": "exact_concat.v1",
            }
        ]
        with self.assertRaises(SearchTargetContractError):
            search_text_values(
                payload_kind="mixed",
                payload=with_table,
                artifact_locator=locator,
            )

    def test_unproved_or_unknown_target_fails_closed(self) -> None:
        (unit,), _ = _build([_element(0, text="原文")])
        graph = unit.artifact_locator["source_projection"]
        graph["search_targets"][0] = "payload.context"
        unit.payload["context"] = "伪上下文"

        with self.assertRaises(SearchTargetContractError):
            search_text_values(
                payload_kind="text",
                payload=unit.payload,
                artifact_locator=unit.artifact_locator,
            )
        for unsupported_kind in ("qa", "unknown"):
            with self.subTest(payload_kind=unsupported_kind):
                with self.assertRaises(SearchTargetContractError):
                    search_text_values(
                        payload_kind=unsupported_kind,
                        payload=unit.payload,
                        artifact_locator=unit.artifact_locator,
                    )


class ComputeRowTests(unittest.TestCase):
    def test_empty_visual_routes_by_structure_without_invented_body_text(self) -> None:
        row = compute_search_projection_row(
            asset_id="ua_empty_visual",
            title="第一节 经营情况",
            heading_path=["第一节 经营情况", "一、收入分析"],
            payload_kind="text",
            payload={
                "image_ref": "images/" + "d" * 64 + ".png",
                "caption": "",
                "visual_kind": "image",
                "context": "UNBOUND_CONTEXT",
            },
            semantic_keys=["business_overview"],
            artifact_locator=_search_locator(
                projection_kind="image_identity",
                projection_target="payload.image_ref",
                targets=[],
            ),
            built_at=_BUILT_AT,
        )

        self.assertTrue(row["title_tokens"])
        self.assertTrue(row["path_tokens"])
        self.assertEqual(row["body_tokens"], "")
        self.assertEqual(row["key_tokens"], "business_overview")
        self.assertNotIn("images", row["body_tokens"])
        self.assertNotIn("unbound_context", row["body_tokens"])

        (visual_unit,), _ = _build(
            [
                _element(
                    0,
                    kind="image",
                    raw_kind="image",
                    text="唯一图内事实VIS123",
                    image_path="images/source.png",
                    image_caption=["图标题CAPTION"],
                    image_footnote=["唯一脚注NOTE456"],
                )
            ]
        )
        visual_unit.payload["context"] = "UNBOUND_CONTEXT"
        visual_row = compute_search_projection_row(
            asset_id="ua_bound_visual",
            title=None,
            heading_path=[],
            payload_kind="text",
            payload=visual_unit.payload,
            semantic_keys=[],
            artifact_locator=visual_unit.artifact_locator,
            built_at=_BUILT_AT,
        )
        for expected in ("caption", "vis123", "note456"):
            self.assertIn(expected, visual_row["body_tokens"])
        self.assertNotIn("unbound_context", visual_row["body_tokens"])

    def test_row_fields_exclude_generated_tsv_and_exclude_raw_html(self) -> None:
        (unit,), _ = _build(
            [
                _element(
                    0,
                    kind="table",
                    raw_kind="table",
                    table_caption=["现金流量表补充资料"],
                    table_footnote=[],
                    table={
                        "headers": ["项目"],
                        "rows": [["经营活动", "100"]],
                        "merged_cells": [],
                    },
                )
            ]
        )
        unit.payload["raw_html"] = "<x>NOPE</x>"
        row = compute_search_projection_row(
            asset_id="ua_x",
            title="现金流量表",
            heading_path=["第八节 财务报告", "现金流量表补充资料"],
            payload_kind=unit.payload_kind,
            payload=unit.payload,
            semantic_keys=["cash_flow_statement", "financial_report_chapter"],
            artifact_locator=unit.artifact_locator,
            built_at=_BUILT_AT,
        )
        self.assertEqual(row["asset_id"], "ua_x")
        self.assertEqual(row["title_text"], "现金流量表")
        self.assertEqual(row["heading_path_text"], "第八节 财务报告 > 现金流量表补充资料")
        self.assertEqual(row["retrieval_rules_version"], tokenizer.RETRIEVAL_RULES_VERSION)
        # Semantic keys are joined untokenized (controlled ASCII tokens).
        self.assertEqual(
            row["key_tokens"], "cash_flow_statement financial_report_chapter"
        )
        self.assertFalse(row["header_row_candidate"])
        self.assertEqual(row["built_at"], _BUILT_AT)
        self.assertNotIn("search_tsv", row)
        self.assertEqual(
            row["body_atoms"],
            (
                "现金流量表补充资料",
                "项目",
                "经营活动",
                "100",
            ),
        )
        self.assertTrue(row["title_tokens"])
        self.assertNotIn("nope", row["body_tokens"])

    def test_empty_unit_yields_legal_empty_strings(self) -> None:
        row = compute_search_projection_row(
            asset_id="ua_empty",
            title=None,
            heading_path=[],
            payload_kind="text",
            payload={"text": ""},
            semantic_keys=None,
            artifact_locator=_search_locator(
                projection_kind="text_identity",
                projection_target="payload.text",
                targets=[("payload.text", "payload")],
            ),
            built_at=_BUILT_AT,
        )
        self.assertEqual(row["title_text"], "")
        self.assertEqual(row["heading_path_text"], "")
        self.assertEqual(row["title_tokens"], "")
        self.assertEqual(row["path_tokens"], "")
        self.assertEqual(row["body_tokens"], "")
        self.assertEqual(row["body_atoms"], ())
        self.assertEqual(row["key_tokens"], "")
        self.assertEqual(row["retrieval_rules_version"], tokenizer.RETRIEVAL_RULES_VERSION)

    def test_structural_section_keeps_table_and_explanation_retrievable(self) -> None:
        elements, headings = _sample_share_change()
        elements.extend(
            [
                _element(5, text="股权激励行权导致股本增加。"),
                _element(6, text="股份变动的批准情况\n董事会审议通过。"),
            ]
        )
        for heading in headings:
            heading["section_span"][1] = 6
        units, _ = _build(
            elements,
            headings=headings,
            filing_type="semiannual_report",
        )
        self.assertTrue(units)
        rows = [
            compute_search_projection_row(
                asset_id=f"ua_share_changes_{index}",
                title=unit.title,
                heading_path=unit.heading_path,
                payload_kind=unit.payload_kind,
                payload=unit.payload,
                semantic_keys=unit.semantic_keys,
                artifact_locator=unit.artifact_locator,
                built_at=_BUILT_AT,
            )
            for index, unit in enumerate(units)
        ]
        for row in rows:
            self.assertEqual(row["title_text"], "1、股份变动情况")
            self.assertEqual(
                row["heading_path_text"],
                "第七节 股份变动及股东情况 > 一、股份变动情况 > 1、股份变动情况",
            )
        body = "\n".join(
            " ".join(
                search_text_values(
                    payload_kind=unit.payload_kind,
                    payload=unit.payload,
                    artifact_locator=unit.artifact_locator,
                )
            )
            for unit in units
        )
        for evidence in (
            "单位：股",
            "股份总数",
            "843,978,741",
            "股权激励行权导致股本增加",
            "董事会审议通过",
        ):
            self.assertIn(evidence, body)

        for query, channel in (
            ("股份变动情况", "title_tokens"),
            ("第七节 股份变动及股东情况", "path_tokens"),
            ("股份总数", "body_tokens"),
            ("股份变动的原因", "body_tokens"),
            ("股权激励行权股本增加", "body_tokens"),
            ("股份变动的批准情况", "body_tokens"),
            ("董事会审议通过", "body_tokens"),
        ):
            query_tokens = set(tokenizer.query_word_tokens(query))
            self.assertTrue(
                any(
                    query_tokens <= set(str(row[channel]).split())
                    for row in rows
                ),
                (query, channel, rows),
            )


if __name__ == "__main__":
    unittest.main()
