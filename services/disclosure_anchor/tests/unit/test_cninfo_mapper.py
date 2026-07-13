"""CNINFO mapper and filing_type map tests."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import unittest

from disclosure_anchor.adapters.sources.cninfo.mapper import (
    CninfoMappingError,
    derive_primary_class,
    derive_report_period,
    load_class_map,
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

        self.assertEqual(bundle.version, "2026-07-r9")
        self.assertEqual(
            {rule.filing_type for rule in bundle.rules},
            {
                "intermediary_report",
                "annual_report",
                "semiannual_report",
                "quarterly_report",
                "performance_forecast",
                "performance_flash",
                "investor_relations",
                "performance_briefing",
                "inquiry_regulatory",
            },
        )
        # Order constraints: carrier keywords must outrank subject keywords
        # (激励法律意见书 is a legal opinion, not an incentive filing), and
        # 半年度报告 must stay ahead of 年度报告 (substring shadowing).
        order = [rule.filing_type for rule in bundle.rules]
        self.assertEqual(order[0], "intermediary_report")
        self.assertLess(
            order.index("semiannual_report"), order.index("annual_report")
        )
        # r7: briefing and inquiry outrank the periodic-report substrings —
        # BSE "年度报告业绩说明会预告公告" and "年度报告…问询函的回复" titles
        # were being captured by 年度报告/季度报告 (and then faking a
        # report_period via derive_report_period).
        self.assertLess(
            order.index("performance_briefing"), order.index("semiannual_report")
        )
        self.assertLess(
            order.index("inquiry_regulatory"), order.index("semiannual_report")
        )
        # r7: inquiry has both keyword orders (SQL LIKE match=all is ordered;
        # "延期回复…问询函" puts 回复 before 问询).
        inquiry_keyword_sets = [
            rule.keywords for rule in bundle.rules
            if rule.filing_type == "inquiry_regulatory"
        ]
        self.assertIn(("问询", "回复"), inquiry_keyword_sets)
        self.assertIn(("回复", "问询"), inquiry_keyword_sets)

    def test_rule_bundle_parses_topic_rules(self) -> None:
        bundle = load_filing_type_rule_bundle()

        by_class = {rule.class_name: rule for rule in bundle.topic_rules}
        self.assertIn("operating_data", by_class)
        self.assertIn("销售简报", by_class["operating_data"].keywords)
        self.assertIn("经营数据", by_class["operating_data"].keywords)
        self.assertIn("产销快报", by_class["operating_data"].keywords)
        # r7 generalization audit: provider-code blind spots per class.
        self.assertIn("保费收入", by_class["operating_data"].keywords)
        self.assertIn("业绩快报", by_class["performance_flash"].keywords)
        self.assertIn("减值准备", by_class["risk_alert"].keywords)
        self.assertIn("问询函", by_class["inquiry_regulatory"].keywords)
        self.assertIn("重整", by_class["delisting_risk"].keywords)
        self.assertIn("回购报告书", by_class["share_buyback"].keywords)
        # r9 batch-3 additions (EPS lens): distress tripwire, pharma late-stage
        # approvals, license-out deals, plant commissioning milestones.
        self.assertIn("宽限期", by_class["risk_alert"].keywords)
        self.assertIn("药品注册批准", by_class["risk_alert"].keywords)
        self.assertIn("授权许可协议", by_class["major_contract"].keywords)
        self.assertIn("小时试运行", by_class["operating_data"].keywords)
        # 电量完成情况 supersedes 发电量完成情况 (substring superset).
        self.assertIn("电量完成情况", by_class["operating_data"].keywords)
        self.assertNotIn("发电量完成情况", by_class["operating_data"].keywords)
        # topic keywords are plain substrings — a '%' would act as a LIKE
        # wildcard in SQL but stay literal in the Python evaluator.
        for rule in bundle.topic_rules:
            for keyword in rule.keywords:
                self.assertNotIn("%", keyword)

    def test_rule_bundle_parses_noise_rules(self) -> None:
        bundle = load_filing_type_rule_bundle()

        all_keywords = [kw for rule in bundle.noise_rules for kw in rule.keywords]
        self.assertIn("募集资金存放", all_keywords)
        # r7: the wide 异动 keyword was narrowed to adjacency anchors so ST
        # compound titles (…异常波动暨风险提示…) and 异动问询回函 stay
        # processable; only the pure template form is rejected.
        self.assertNotIn("股票交易异常波动", all_keywords)
        self.assertIn("股票交易异常波动的公告", all_keywords)
        self.assertIn("股票交易异常波动公告", all_keywords)
        # r7 ordered word-order variants (SQL LIKE '%'-joined, order matters).
        self.assertIn("限制性股票回购注销实施", all_keywords)
        keyword_sets = [rule.keywords for rule in bundle.noise_rules]
        self.assertIn(("归还", "闲置募集资金"), keyword_sets)
        self.assertIn(("子公司发行", "票据", "提供担保"), keyword_sets)
        # r8: a bare MTN-plan keyword killed a real RMB3bn/1.73%-coupon
        # issuance. Only explicitly procedural listing/quotation shapes stay
        # behind the absolute gate.
        self.assertNotIn(("中期票据计划",), keyword_sets)
        self.assertIn(("中期票据计划", "上市"), keyword_sets)
        self.assertIn(("上市", "中期票据计划"), keyword_sets)
        self.assertIn(("中期票据计划", "挂牌"), keyword_sets)
        self.assertIn(("中期票据计划", "进行发行", "提供担保"), keyword_sets)
        self.assertNotIn(("预计满足", "条件的提示性公告"), keyword_sets)
        # r9: 已获授 wording variant of the second-type restricted-stock
        # cancellation rule (Kingsoft Office shape).
        self.assertIn(("作废", "已获授", "尚未归属", "限制性股票"), keyword_sets)

    def test_carrier_title_rules_behavior_on_codeless_channel(self) -> None:
        # Carrier keywords outrank subject keywords on the title path…
        self.assertEqual(
            map_filing_type(
                "关于2024年限制性股票激励计划的法律意见书", category_names_by_code={}
            ),
            "intermediary_report",
        )
        # …but '审计报告' is deliberately NOT a carrier keyword: a merged
        # "年度报告及审计报告" title must stay an annual report (substring
        # shadowing would otherwise misroute the annual report itself).
        self.assertEqual(
            map_filing_type(
                "某公司2024年年度报告及审计报告", category_names_by_code={}
            ),
            "annual_report",
        )

    def test_derive_primary_class_consults_topic_rules(self) -> None:
        # View parity (0021): topic hit wins over a lower-priority code
        # class, loses to a higher one, and works code-less too.
        self.assertEqual(
            derive_primary_class("012305", "2026年6月销售及近期新增项目情况简报"),
            "operating_data",
        )
        self.assertEqual(
            derive_primary_class("010301", "2025年年度报告（含主要经营数据）"),
            "annual_report",
        )
        self.assertEqual(
            derive_primary_class(None, "贵州茅台2026年第一季度主要经营数据公告"),
            "operating_data",
        )

    def test_r7_topic_rules_fill_generalization_blind_spots(self) -> None:
        # 保费收入/偿付能力: insurers' operating data files under generic
        # codes only (01010501||010113||012399) — topic grants class AND
        # download eligibility; 偿付能力 also fixes the title-fallback
        # mislabel quarterly_report (which faked a report_period).
        self.assertEqual(
            derive_primary_class("01010501||010113||012399", "中国平安保费收入公告"),
            "operating_data",
        )
        self.assertEqual(
            derive_primary_class(
                "01010501||010113||012399",
                "中国人寿偿付能力季度报告摘要（2026年第一季度）",
            ),
            "operating_data",
        )
        # 业绩快报: the dedicated flash prefixes (01211160) never occur in
        # real data — everything is coded 012111 (forecast). The topic hit
        # relabels flashes via argmax (flash 97 > forecast 96).
        self.assertEqual(
            derive_primary_class("012111", "招商银行股份有限公司2024年度业绩快报公告"),
            "performance_flash",
        )
        # 重整: bankruptcy-reorg chain files under 012399 → delisting_risk.
        self.assertEqual(
            derive_primary_class(
                "01010503||010112||012399",
                "关于公司及控股子公司重整计划执行完毕的公告",
            ),
            "delisting_risk",
        )

    def test_r7_title_rule_order_stops_substring_capture(self) -> None:
        # BSE briefing notices contain 年度报告/季度报告 substrings; the
        # briefing rule must win or report_period gets faked as an annual.
        self.assertEqual(
            map_filing_type("2025年年度报告业绩说明会预告公告", category_names_by_code={}),
            "performance_briefing",
        )
        self.assertEqual(
            map_filing_type(
                "晶科能源关于2024年年度报告的信息披露监管问询函的回复的公告",
                category_names_by_code={},
            ),
            "inquiry_regulatory",
        )
        # 业绩发布会 is the financial-sector wording of 说明会.
        self.assertEqual(
            map_filing_type("中国平安关于召开2025年度业绩发布会的公告", category_names_by_code={}),
            "performance_briefing",
        )
        # Real periodic reports never contain 说明会/问询 — still annual.
        self.assertEqual(
            map_filing_type("某公司2024年年度报告", category_names_by_code={}),
            "annual_report",
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

    def test_research_activity_category_maps_to_investor_relations(self) -> None:
        # cninfo 012001 = 调研活动: an investor-relations record that fell into
        # `other` before rule bundle 2026-07-r3 (round3 P1#6).
        self.assertEqual(
            map_filing_type(
                "012001", category_names_by_code={"012001": "调研活动"}
            ),
            "investor_relations",
        )

    def test_report_period_derivation_from_real_title_shapes(self) -> None:
        cases = [
            ("江海股份：2025年年度报告", "annual_report", "2025A"),
            ("2025年年度报告（更正后）", "annual_report", "2025A"),
            ("平安银行：2025年半年度报告", "semiannual_report", "2025Q2"),
            ("贵州茅台：2026年第一季度报告", "quarterly_report", "2026Q1"),
            ("比亚迪：2026年一季度报告", "quarterly_report", "2026Q1"),
            ("某公司：2025年第三季度报告", "quarterly_report", "2025Q3"),
            ("某公司：2026年1季度报告", "quarterly_report", "2026Q1"),
        ]
        for title, filing_type, expected in cases:
            self.assertEqual(
                derive_report_period(title, filing_type=filing_type), expected, title
            )

    def test_report_period_underivable_returns_none_and_never_raises(self) -> None:
        cases = [
            # No year in title.
            ("半年报董事会决议公告", "semiannual_report"),
            # Chinese-numeral year (H-share style) is out of the closed rule.
            ("H股公告（二零二五年年度业绩公布）", "annual_report"),
            # Quarterly without a quarter token.
            ("2026年报告", "quarterly_report"),
            # Non-periodic filing types never derive.
            ("2025年年度权益分派实施公告", "other"),
            ("关于2026年度担保计划的公告", "investor_relations"),
        ]
        for title, filing_type in cases:
            self.assertIsNone(derive_report_period(title, filing_type=filing_type), title)

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

        self.assertEqual(filing_type, "inquiry_regulatory")

    def test_filing_type_mapping_falls_back_to_other(self) -> None:
        self.assertEqual(
            map_filing_type("010112", category_names_by_code={"010112": "深市公司公告"}),
            "other",
        )

    def test_class_map_vocabulary_integrity(self) -> None:
        class_map = load_class_map()
        self.assertEqual(class_map["version"], "2026-07-r7")
        # r6 code blind-spot fills (2026-07-13 generalization audit).
        self.assertIn("011711", class_map["classes"]["financing"]["prefixes"])
        self.assertIn("011713", class_map["classes"]["financing"]["prefixes"])
        self.assertIn(
            "01239910", class_map["classes"]["meeting_resolution"]["prefixes"]
        )
        self.assertIn("0115", class_map["classes"]["equity_share_change"]["prefixes"])
        for name, spec in class_map["classes"].items():
            self.assertTrue(spec["prefixes"], name)
            self.assertIsInstance(spec["priority"], int, name)
            self.assertTrue(spec["zh"], name)
        # parse_scope classes must all exist in the class map, disjoint sets
        policy = json.loads(
            (
                Path(__file__).resolve().parents[2] / "config/processing_policy.json"
            ).read_text(encoding="utf-8")
        )
        known = set(class_map["classes"])
        process, register_only = set(policy["process"]), set(policy["register_only"])
        self.assertTrue(process <= known)
        self.assertTrue(register_only <= known)
        self.assertEqual(process & register_only, set())
        self.assertEqual(process | register_only, known)
        # corrections/amendments of core filings must process (edgartools
        # amendments=True analog)
        self.assertIn("correction_supplement", process)

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
