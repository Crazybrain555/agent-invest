"""CNINFO mapper and filing_type map tests."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import unittest

from disclosure_anchor.adapters.sources.cninfo.mapper import (
    CninfoMappingError,
    category_prefix_matches,
    load_filing_type_rule_bundle,
    map_filing_type,
    map_p_info3015_record,
    map_p_stock2100_record,
    split_category_segments,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "cninfo"


class CninfoMapperTests(unittest.TestCase):
    def test_filing_type_rule_bundle_has_required_seed_rules(self) -> None:
        bundle = load_filing_type_rule_bundle()

        self.assertEqual(bundle.version, "2026-07-r2")
        self.assertEqual(
            {rule.filing_type for rule in bundle.rules},
            {
                "annual_report",
                "semiannual_report",
                "quarterly_report",
                "performance_forecast",
                "performance_flash",
                "investor_relations",
                "performance_briefing",
                "inquiry_reply",
            },
        )

    def test_f006v_segments_are_split_before_filing_type_mapping(self) -> None:
        filing_type = map_filing_type(
            "01010503||010112||010301",
            category_names_by_code={
                "01010503": "临时公告",
                "010112": "深市公司公告",
                "010301": "年度报告",
            },
        )

        self.assertEqual(filing_type, "annual_report")
        self.assertEqual(
            split_category_segments("01010503||010112||010301"),
            ["01010503", "010112", "010301"],
        )

    def test_semiannual_is_not_shadowed_by_annual_substring(self) -> None:
        # "半年度报告" contains the substring "年度报告"; rule order in the
        # bundle must classify it as semiannual, never annual.
        for raw in ("半年度报告", "2025年半年度报告", "公告||半年度报告全文"):
            self.assertEqual(
                map_filing_type(raw, category_names_by_code={}),
                "semiannual_report",
                raw,
            )
        self.assertEqual(
            map_filing_type("年度报告", category_names_by_code={}), "annual_report"
        )
        self.assertEqual(
            map_filing_type("第一季度报告", category_names_by_code={}),
            "quarterly_report",
        )

    def test_filing_type_mapping_returns_first_non_other_match(self) -> None:
        filing_type = map_filing_type(
            "012111||010301",
            category_names_by_code={
                "012111": "业绩预告",
                "010301": "年度报告",
            },
        )

        self.assertEqual(filing_type, "performance_forecast")

    def test_filing_type_mapping_supports_inquiry_reply_all_keywords(self) -> None:
        filing_type = map_filing_type(
            "019999",
            category_names_by_code={"019999": "问询函回复公告"},
        )

        self.assertEqual(filing_type, "inquiry_reply")

    def test_filing_type_mapping_falls_back_to_other(self) -> None:
        self.assertEqual(
            map_filing_type("010112", category_names_by_code={"010112": "深市公司公告"}),
            "other",
        )

    def test_category_prefix_matching_uses_segments(self) -> None:
        self.assertTrue(category_prefix_matches("010301||010112", ["0103"]))
        self.assertFalse(category_prefix_matches("010301||010112", ["0120"]))
        self.assertTrue(category_prefix_matches("010301||010112", None))

    def test_p_info3015_mapper_uses_textid_as_provider_document_id(self) -> None:
        payload = json.loads(
            (FIXTURE_ROOT / "p_info3015_sample.json").read_text(encoding="utf-8")
        )
        record = payload["records"][0]

        ref = map_p_info3015_record(record)

        self.assertEqual(ref.provider, "cninfo")
        self.assertEqual(ref.provider_document_id, record["TEXTID"])
        self.assertNotEqual(ref.provider_document_id, str(record["OBJECTID"]))
        self.assertNotEqual(ref.provider_document_id, record["RECID"])
        self.assertEqual(ref.raw_category, "010301||010112")
        self.assertEqual(ref.announcement_date, date(2026, 7, 1))
        self.assertEqual(ref.object_id, 90000001)
        self.assertEqual(ref.rec_id, "rec-test-000001-1")
        self.assertEqual(ref.file_size, 512)
        self.assertEqual(ref.index_updated_at.tzinfo.key, "Asia/Shanghai")

    def test_p_stock2100_mapper_extracts_org_id_and_uscc(self) -> None:
        payload = json.loads(
            (FIXTURE_ROOT / "p_stock2100_sample.json").read_text(encoding="utf-8")
        )

        profile = map_p_stock2100_record(payload["records"][0])

        self.assertEqual(profile.security_code, "000001")
        self.assertEqual(profile.legal_name, "平安银行股份有限公司")
        self.assertEqual(profile.provider_org_id, "cninfo-org-test-000001")
        self.assertEqual(profile.uscc, "91440300192185379H")

    def test_missing_required_p_info3015_field_fails_loudly(self) -> None:
        payload = json.loads(
            (FIXTURE_ROOT / "p_info3015_sample.json").read_text(encoding="utf-8")
        )
        record = dict(payload["records"][0])
        del record["TEXTID"]

        with self.assertRaisesRegex(CninfoMappingError, "TEXTID"):
            map_p_info3015_record(record)


if __name__ == "__main__":
    unittest.main()
