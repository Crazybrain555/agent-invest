"""Independent source-bound acceptance for optional statement-route ambiguity."""

from __future__ import annotations

import unittest

from disclosure_anchor.application.contracts.semantic_routes import (
    SemanticAdjudicationDecision,
    SemanticDocumentContext,
)
from disclosure_anchor.application.services.semantic_router import SemanticRouter
from disclosure_anchor.application.services.semantic_taxonomy import (
    load_semantic_route_taxonomy,
)
from tests.unit.test_semantic_router import (
    _Executor,
    _drafts_with_bodies_and_table,
    _drafts_with_parent_heading_and_table,
    _drafts_with_table,
    _heading_only_drafts,
)


_STATEMENTS = (
    ("资产负债表", "balance_sheet", "balance_sheet_parent"),
    ("利润表", "income_statement", "income_statement_parent"),
    ("现金流量表", "cash_flow_statement", "cash_flow_statement_parent"),
    ("所有者权益变动表", "equity_statement", "equity_statement_parent"),
)


def _whole_table(counterparty: str) -> str:
    return (
        "<table><tr><th>项目</th><th>合并本期</th><th>合并上期</th>"
        f"<th>{counterparty}本期</th><th>{counterparty}上期</th></tr>"
        "<tr><td>甲项目</td><td>100</td><td>90</td><td>50</td><td>45</td>"
        "</tr></table>"
    )


class SemanticEnrichmentPolicyIndependentTests(unittest.TestCase):
    def test_source_titled_joint_whole_tables_keep_both_statement_topics(self) -> None:
        taxonomy = load_semantic_route_taxonomy()
        for statement, consolidated, parent in _STATEMENTS:
            for counterparty in ("公司", "银行"):
                for conjunction in ("及", "和"):
                    title = f"合并{conjunction}{counterparty}{statement}"
                    with self.subTest(title=title):
                        admitted, drafts = _drafts_with_bodies_and_table(
                            title,
                            ("（三） 审计报告", "财务报表未经审计。"),
                            _whole_table(counterparty),
                        )
                        self.assertEqual(len(drafts), 1)
                        self.assertIn("审计报告", str(drafts[0].payload))
                        executor = _Executor(
                            lambda _batch: self.fail(
                                "an exact joint statement must not call a model"
                            )
                        )
                        result = SemanticRouter(
                            taxonomy=taxonomy,
                            executor=executor,
                        ).route(
                            admitted=admitted,
                            document=SemanticDocumentContext(
                                title=None,
                                filing_type="quarterly_report",
                            ),
                            drafts=drafts,
                        )

                        self.assertEqual(
                            result.units[0].semantic_keys,
                            (consolidated, parent),
                        )
                        self.assertTrue(
                            {consolidated, parent}.issubset(
                                result.units[0].section_keys or ()
                            )
                        )
                        self.assertEqual(
                            result.receipts[0].candidate_keys,
                            (consolidated, parent),
                        )
                        self.assertEqual(result.receipts[0].decision_source, "deterministic")
                        self.assertEqual(executor.calls, 0)
                        self.assertEqual(result.units[0].payload, drafts[0].payload)
                        self.assertEqual(result.units[0].locator, drafts[0].locator)
                        self.assertEqual(result.units[0].content_hash, drafts[0].content_hash)
                        self.assertNotIn("audit_opinion", result.units[0].semantic_keys)

    def test_heading_only_joint_title_keeps_section_but_no_direct_tags(self) -> None:
        admitted, drafts = _heading_only_drafts("合并及公司现金流量表")
        executor = _Executor(lambda _batch: self.fail("heading-only must not call model"))
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(), executor=executor
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="quarterly_report"),
            drafts=drafts,
        )
        self.assertIsNone(result.units[0].semantic_keys)
        self.assertEqual(
            result.units[0].section_keys,
            ("cash_flow_statement", "cash_flow_statement_parent"),
        )
        self.assertEqual(result.units[0].payload, drafts[0].payload)
        self.assertEqual(executor.calls, 0)

    def test_single_source_statement_titles_keep_one_direct_route(self) -> None:
        taxonomy = load_semantic_route_taxonomy()
        for statement, consolidated, parent in _STATEMENTS:
            for title, expected in (
                (f"合并{statement}", consolidated),
                (f"母公司{statement}", parent),
            ):
                with self.subTest(title=title):
                    admitted, drafts = _drafts_with_table(title, _whole_table("公司"))
                    executor = _Executor(
                        lambda _batch: self.fail(
                            "one exact statement title must not need a model"
                        )
                    )
                    result = SemanticRouter(
                        taxonomy=taxonomy,
                        executor=executor,
                    ).route(
                        admitted=admitted,
                        document=SemanticDocumentContext(
                            title=None,
                            filing_type="quarterly_report",
                        ),
                        drafts=drafts,
                    )
                    self.assertEqual(result.units[0].semantic_keys, (expected,))
                    self.assertEqual(result.units[0].payload, drafts[0].payload)
                    self.assertEqual(executor.calls, 0)

    def test_joint_words_only_in_ancestor_or_document_do_not_tag_child(self) -> None:
        title = "合并及公司现金流量表"
        table = (
            "<table><tr><th>项目</th><th>本期</th></tr>"
            "<tr><td>一般说明</td><td>100</td></tr></table>"
        )
        taxonomy = load_semantic_route_taxonomy()
        ancestor_admitted, ancestor_drafts = _drafts_with_parent_heading_and_table(
            title, "其他说明", table
        )
        document_admitted, document_drafts = _drafts_with_table("其他说明", table)
        for admitted, drafts, document_title in (
            (ancestor_admitted, ancestor_drafts, None),
            (document_admitted, document_drafts, title),
        ):
            with self.subTest(document_title=document_title):
                result = SemanticRouter(
                    taxonomy=taxonomy,
                    executor=_Executor(
                        lambda batch: tuple(
                            SemanticAdjudicationDecision(
                                unit_index=unit.unit_index, routes=()
                            )
                            for unit in batch.units
                        )
                    ),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=document_title,
                        filing_type="quarterly_report",
                    ),
                    drafts=drafts,
                )
                child = result.units[-1]
                self.assertEqual(child.title, "其他说明")
                self.assertFalse(child.semantic_keys)
                self.assertEqual(child.payload, drafts[-1].payload)
                self.assertEqual(child.locator, drafts[-1].locator)

    def test_joint_words_inside_an_inexact_title_do_not_create_direct_pair(self) -> None:
        admitted, drafts = _drafts_with_table(
            "合并及公司现金流量表补充说明",
            "<table><tr><td>项目</td><td>本期</td></tr>"
            "<tr><td>一般说明</td><td>100</td></tr></table>",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            executor=_Executor(
                lambda batch: tuple(
                    SemanticAdjudicationDecision(unit_index=u.unit_index, routes=())
                    for u in batch.units
                )
            ),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="quarterly_report"),
            drafts=drafts,
        )
        self.assertFalse(result.units[0].semantic_keys)
        self.assertEqual(result.units[0].payload, drafts[0].payload)
        self.assertEqual(result.units[0].locator, drafts[0].locator)


if __name__ == "__main__":
    unittest.main()
