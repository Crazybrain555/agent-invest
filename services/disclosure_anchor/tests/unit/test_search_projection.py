"""Provider-native search projection and tokenizer tests (DB-free)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import unittest

from disclosure_anchor.adapters.retrieval import tokenizer
from disclosure_anchor.application.contracts.provider_document import ProviderPayload
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderUnitSearchContractError,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
    provider_unit_search_text_values,
)
from disclosure_anchor.application.use_cases.build_search_projection import (
    compute_search_projection_row,
)
from tests.unit.test_provider_unit_builder import (
    _admitted,
    _block,
    _document,
    _identical_text_parts_document,
    _representative_document,
    _visual_only_document,
)


_BUILT_AT = datetime(2026, 8, 12, tzinfo=timezone.utc)


def _heading_only_document():  # type: ignore[no-untyped-def]
    return _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "text",
                    (ProviderPayload("text", None, "唯一标题"),),
                    annotation="title",
                    level=1,
                ),
            ),
        ),
        segments=(),
    )


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

    def test_analyzers_normalize_width_case_and_empty(self) -> None:
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
        indexed = set(tokenizer.index_word_tokens("股份变动及股东情况").split())
        for query in ("股份变动", "股东情况"):
            self.assertLessEqual(set(tokenizer.query_word_tokens(query)), indexed)

    def test_query_groups_have_no_content_alias_expansion(self) -> None:
        self.assertEqual(
            tokenizer.build_search_tsquery_groups("商誉减值"),
            ("'商誉'", "'减值'"),
        )
        self.assertEqual(
            tokenizer.build_search_tsquery("商誉减值"),
            "'商誉' & '减值'",
        )
        self.assertEqual(tokenizer.build_search_tsquery_groups("  "), ())
        self.assertEqual(tokenizer.build_search_tsquery("  "), "")
        self.assertNotIn("半年度", tokenizer.build_search_tsquery("半年报"))


class ProviderSearchTargetTests(unittest.TestCase):
    def test_title_is_excluded_and_body_replays_only_explicit_bindings(self) -> None:
        draft = build_provider_units(_admitted(_representative_document())).units[1]

        values = provider_unit_search_text_values(
            payload_kind=draft.payload_kind,
            payload=draft.payload,
            title=draft.title,
            artifact_locator=provider_unit_locator_to_payload(draft.locator),
        )

        body = " ".join(values)
        self.assertNotIn("第一章 标题", body)
        for expected in ("正文", "甲", "表一", "□适用"):
            self.assertIn(expected, body)
        self.assertNotIn("image_0001", body)

        row = compute_search_projection_row(
            asset_id="asset_1",
            title=draft.title,
            heading_path=draft.heading_path,
            payload_kind=draft.payload_kind,
            payload=draft.payload,
            semantic_keys=draft.semantic_keys,
            artifact_locator=provider_unit_locator_to_payload(draft.locator),
            built_at=_BUILT_AT,
        )
        self.assertEqual(row["title_text"], "第一章 标题")
        self.assertEqual(row["heading_path_text"], "第一章 标题")
        self.assertEqual(
            row["body_atoms"],
            tuple(
                tokenizer.normalize_search_text(value)
                for value in values
                if tokenizer.normalize_search_text(value).strip()
            ),
        )

    def test_equal_source_occurrences_remain_two_search_atoms(self) -> None:
        draft = build_provider_units(
            _admitted(_identical_text_parts_document())
        ).units[0]

        values = provider_unit_search_text_values(
            payload_kind=draft.payload_kind,
            payload=draft.payload,
            title=draft.title,
            artifact_locator=provider_unit_locator_to_payload(draft.locator),
        )

        self.assertEqual(values, ("相同正文", "相同正文"))

    def test_heading_only_and_visual_only_units_have_no_body_atoms(self) -> None:
        for document in (_heading_only_document(), _visual_only_document("f")):
            with self.subTest(document=document):
                draft = build_provider_units(_admitted(document)).units[0]
                row = compute_search_projection_row(
                    asset_id="asset_empty",
                    title=draft.title,
                    heading_path=draft.heading_path,
                    payload_kind=draft.payload_kind,
                    payload=draft.payload,
                    semantic_keys=draft.semantic_keys,
                    artifact_locator=provider_unit_locator_to_payload(draft.locator),
                    built_at=_BUILT_AT,
                )
                self.assertEqual(row["body_atoms"], ())
                self.assertEqual(row["body_tokens"], "")

    def test_unknown_or_cross_part_locator_fails_closed(self) -> None:
        draft = build_provider_units(_admitted(_representative_document())).units[1]
        locator_payload = provider_unit_locator_to_payload(draft.locator)
        locator_payload["contract_version"] = "unknown.v1"
        with self.assertRaises(ProviderUnitSearchContractError):
            provider_unit_search_text_values(
                payload_kind=draft.payload_kind,
                payload=draft.payload,
                title=draft.title,
                artifact_locator=locator_payload,
            )

        body_binding = next(
            binding
            for binding in draft.locator.search_targets
            if binding.source.source_index == 2
        )
        assert body_binding.destination.part_index == 0
        forged_binding = replace(
            body_binding,
            destination=replace(body_binding.destination, part_index=1),
        )
        forged_locator = replace(
            draft.locator,
            search_targets=tuple(
                forged_binding if item == body_binding else item
                for item in draft.locator.search_targets
            ),
        )
        with self.assertRaises(ProviderUnitSearchContractError):
            provider_unit_search_text_values(
                payload_kind=draft.payload_kind,
                payload=draft.payload,
                title=draft.title,
                artifact_locator=provider_unit_locator_to_payload(forged_locator),
            )

    def test_row_shape_stays_database_compatible(self) -> None:
        draft = build_provider_units(_admitted(_representative_document())).units[1]
        row = compute_search_projection_row(
            asset_id="asset_shape",
            title=draft.title,
            heading_path=draft.heading_path,
            payload_kind=draft.payload_kind,
            payload=draft.payload,
            semantic_keys=draft.semantic_keys,
            artifact_locator=provider_unit_locator_to_payload(draft.locator),
            built_at=_BUILT_AT,
        )

        self.assertEqual(
            set(row),
            {
                "asset_id",
                "retrieval_rules_version",
                "title_text",
                "heading_path_text",
                "title_tokens",
                "path_tokens",
                "body_tokens",
                "body_atoms",
                "key_tokens",
                "header_row_candidate",
                "built_at",
            },
        )
        self.assertEqual(
            row["retrieval_rules_version"],
            "rp-2026.08-provider-unit-v1",
        )
        self.assertEqual(row["key_tokens"], "document_content")
        self.assertFalse(row["header_row_candidate"])
        self.assertNotIn("search_tsv", row)


if __name__ == "__main__":
    unittest.main()
