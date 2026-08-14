from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_PROMPT_VERSION,
    SemanticAdjudicatedRoute,
    SemanticAdjudicationDecision,
    SemanticDocumentContext,
    SemanticRouteContractError,
    SemanticRouteDefinition,
    SemanticRouteTaxonomy,
)
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicationBatch,
    SemanticAdjudicatorIdentity,
    SemanticRouteAdjudicatorError,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
)
from disclosure_anchor.application.services.semantic_router import SemanticRouter
from disclosure_anchor.application.services.semantic_taxonomy import (
    load_semantic_route_taxonomy,
)
from disclosure_anchor.domain.services.unit_hashing import (
    compute_unit_hashes,
)
from tests.unit.test_provider_unit_builder import (
    _admitted,
    _block,
    _document,
    _representative_document,
)
from disclosure_anchor.application.contracts.provider_document import ProviderPayload


class _MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, SemanticAdjudicationDecision] = {}

    def get(self, cache_key: str) -> SemanticAdjudicationDecision | None:
        return self.values.get(cache_key)

    def put(self, cache_key: str, decision: SemanticAdjudicationDecision) -> None:
        self.values[cache_key] = decision


class _Adjudicator:
    def __init__(
        self,
        decide: Callable[[SemanticAdjudicationBatch], tuple[SemanticAdjudicationDecision, ...]],
    ) -> None:
        self.decide = decide
        self.calls = 0
        self._identity = SemanticAdjudicatorIdentity(
            adapter="semantic-test.v1",
            model="semantic-test-model",
            prompt_version=SEMANTIC_PROMPT_VERSION,
        )

    @property
    def identity(self) -> SemanticAdjudicatorIdentity:
        return self._identity

    def adjudicate(
        self,
        batch: SemanticAdjudicationBatch,
    ) -> tuple[SemanticAdjudicationDecision, ...]:
        self.calls += 1
        return self.decide(batch)


def _taxonomy() -> SemanticRouteTaxonomy:
    return SemanticRouteTaxonomy(
        version="semantic-test-taxonomy.v1",
        definitions=(
            SemanticRouteDefinition(
                key="revenue_and_cost",
                description="营业收入和成本",
                labels=("营业收入", "营业收入和营业成本"),
                scopes=("annual_report",),
            ),
            SemanticRouteDefinition(
                key="performance_forecast_summary",
                description="业绩预告结论",
                labels=("业绩预告",),
                scopes=("performance_forecast",),
            ),
            SemanticRouteDefinition(
                key="performance_forecast_range",
                description="业绩预告区间",
                labels=("预计业绩区间",),
                scopes=("performance_forecast",),
            ),
            SemanticRouteDefinition(
                key="performance_forecast_basis",
                description="业绩变动原因",
                labels=("业绩变动原因",),
                scopes=("performance_forecast",),
            ),
            SemanticRouteDefinition(
                key="share_repurchase_plan",
                description="股份回购方案",
                labels=("回购方案",),
                scopes=("share_repurchase",),
            ),
        ),
    )


def _drafts(title: str):  # type: ignore[no-untyped-def]
    document = _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "text",
                    (ProviderPayload("text", None, title),),
                    annotation="title",
                    level=1,
                ),
            ),
        ),
        segments=(),
    )
    admitted = _admitted(document)
    return admitted, build_provider_units(admitted).units


def _drafts_with_body(title: str, body: str):  # type: ignore[no-untyped-def]
    document = _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "text",
                    (ProviderPayload("text", None, title),),
                    annotation="title",
                    level=1,
                ),
                _block(
                    1,
                    0,
                    "text",
                    (ProviderPayload("text", None, body),),
                    annotation=None,
                ),
            ),
        ),
        segments=(),
    )
    admitted = _admitted(document)
    return admitted, build_provider_units(admitted).units


def _drafts_with_body_only(body: str):  # type: ignore[no-untyped-def]
    document = _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "text",
                    (ProviderPayload("text", None, body),),
                    annotation=None,
                ),
            ),
        ),
        segments=(),
    )
    admitted = _admitted(document)
    return admitted, build_provider_units(admitted).units


def _drafts_with_table(title: str, table_html: str):  # type: ignore[no-untyped-def]
    document = _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "text",
                    (ProviderPayload("text", None, title),),
                    annotation="title",
                    level=1,
                ),
                _block(
                    1,
                    0,
                    "table",
                    (ProviderPayload("table_body", None, table_html),),
                    annotation=None,
                ),
            ),
        ),
        segments=(),
    )
    admitted = _admitted(document)
    return admitted, build_provider_units(admitted).units


def _drafts_with_parent_heading_and_table(
    parent: str,
    title: str,
    table_html: str,
):  # type: ignore[no-untyped-def]
    document = _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "text",
                    (ProviderPayload("text", None, parent),),
                    annotation="title",
                    level=1,
                ),
                _block(
                    1,
                    0,
                    "text",
                    (ProviderPayload("text", None, title),),
                    annotation="title",
                    level=2,
                ),
                _block(
                    2,
                    0,
                    "table",
                    (ProviderPayload("table_body", None, table_html),),
                    annotation=None,
                ),
            ),
        ),
        segments=(),
    )
    admitted = _admitted(document)
    return admitted, build_provider_units(admitted).units


def _drafts_with_parent_heading(
    parent: str,
    title: str,
    body: str,
):  # type: ignore[no-untyped-def]
    document = _document(
        pages=(
            (
                _block(
                    0,
                    0,
                    "text",
                    (ProviderPayload("text", None, parent),),
                    annotation="title",
                    level=1,
                ),
                _block(
                    1,
                    0,
                    "text",
                    (ProviderPayload("text", None, title),),
                    annotation="title",
                    level=2,
                ),
                _block(
                    2,
                    0,
                    "text",
                    (ProviderPayload("text", None, body),),
                    annotation=None,
                ),
            ),
        ),
        segments=(),
    )
    admitted = _admitted(document)
    return admitted, build_provider_units(admitted).units


def _router(adjudicator: _Adjudicator, cache: _MemoryCache | None = None) -> SemanticRouter:
    return SemanticRouter(
        taxonomy=_taxonomy(),
        adjudicator=adjudicator,
        cache=cache or _MemoryCache(),
        batch_size=8,
    )


class SemanticTaxonomyTests(unittest.TestCase):
    def test_packaged_taxonomy_is_closed_and_has_no_fake_fallback_route(self) -> None:
        taxonomy = load_semantic_route_taxonomy()

        self.assertEqual(len(taxonomy.definitions), 302)
        self.assertEqual(len(taxonomy.by_key()), 302)
        self.assertNotIn(taxonomy.fallback_key, taxonomy.by_key())
        self.assertNotIn("other_information", taxonomy.by_key())
        self.assertNotIn("other_significant_events", taxonomy.by_key())
        self.assertTrue(taxonomy.by_key()["balance_sheet"].exclusive_container)
        self.assertTrue(
            taxonomy.by_key()["financial_statements_section"].exclusive_container
        )
        self.assertFalse(
            taxonomy.by_key()["investor_questions_answers"].exclusive_container
        )
        self.assertTrue(taxonomy.by_key()["performance_flash_data"].exclusive_container)
        self.assertTrue(taxonomy.by_key()["share_buyback_plan"].overview_container)
        self.assertTrue(
            taxonomy.by_key()["performance_forecast_summary"].overview_container
        )
        self.assertTrue(taxonomy.by_key()["business_review"].context_container)
        self.assertTrue(taxonomy.by_key()["board_committees"].context_container)
        self.assertTrue(taxonomy.by_key()["risk_management"].context_container)
        self.assertTrue(taxonomy.by_key()["issuance_plan"].section_container)
        self.assertTrue(taxonomy.by_key()["transaction_risk"].section_container)
        self.assertFalse(taxonomy.by_key()["issuance_plan"].context_container)
        self.assertFalse(taxonomy.by_key()["revenue_and_cost"].context_container)
        self.assertFalse(taxonomy.by_key()["decision_procedures"].overview_container)
        self.assertIn("share_buyback_cancellation_arrangement", taxonomy.by_key())
        self.assertIn("share_buyback_account", taxonomy.by_key())
        self.assertIn("回购用途", taxonomy.by_key()["share_buyback_purpose"].labels)
        self.assertIn("更正为", taxonomy.by_key()["corrected_data"].labels)
        self.assertIn("作废", taxonomy.by_key()["incentive_cancellation"].labels)
        self.assertIn("付息日", taxonomy.by_key()["bond_interest_dates"].labels)
        self.assertIn("业务指标", taxonomy.by_key()["operating_metrics"].labels)
        self.assertIn(
            "外币财务报表折算差额",
            taxonomy.by_key()["foreign_currency_translation"].labels,
        )
        self.assertNotIn(
            "外币财务报表折算",
            taxonomy.by_key()["foreign_currency_items"].labels,
        )
        self.assertIn(
            "获准许的弥偿条文",
            taxonomy.by_key()["director_indemnity_insurance"].labels,
        )
        self.assertIn(
            "其他重要业务指标",
            taxonomy.by_key()["operating_metrics"].labels,
        )
        self.assertTrue(
            {"annual_report", "semiannual_report", "quarterly_report"}.issubset(
                taxonomy.by_key()["operating_metrics"].scopes
            )
        )

    def test_context_labels_cannot_collide_in_overlapping_scopes(self) -> None:
        with self.assertRaisesRegex(
            SemanticRouteContractError,
            "context labels collide",
        ):
            SemanticRouteTaxonomy(
                version="context-collision.v1",
                definitions=(
                    SemanticRouteDefinition(
                        key="section_a",
                        description="甲章节",
                        labels=("经营 情况：",),
                        scopes=("annual_report",),
                        context_container=True,
                    ),
                    SemanticRouteDefinition(
                        key="section_b",
                        description="乙章节",
                        labels=("经营情况:",),
                        scopes=(),
                        context_container=True,
                    ),
                ),
            )

        taxonomy = SemanticRouteTaxonomy(
            version="context-distinct-scopes.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="section_a",
                    description="甲章节",
                    labels=("经营情况",),
                    scopes=("annual_report",),
                    context_container=True,
                ),
                SemanticRouteDefinition(
                    key="section_b",
                    description="乙章节",
                    labels=("经营情况",),
                    scopes=("major_contract",),
                    context_container=True,
                ),
            ),
        )
        self.assertEqual(len(taxonomy.definitions), 2)

    def test_exact_context_heading_projects_section_without_model(self) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "第三节 经营情况讨论与分析",
            "报告期内主要工作",
            "公司围绕年度目标稳步推进各项经营工作。",
        )
        taxonomy = SemanticRouteTaxonomy(
            version="context-route.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="business_review",
                    description="经营情况讨论与分析",
                    labels=("经营情况讨论与分析",),
                    scopes=("annual_report",),
                    context_container=True,
                ),
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("exact context-only route must be deterministic")
        )

        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
        )

        self.assertIsNone(result.units[1].semantic_keys)
        self.assertEqual(result.units[1].section_keys, ("business_review",))
        self.assertEqual(result.receipts[1].decision_source, "fallback")
        self.assertEqual(adjudicator.calls, 0)

    def test_exact_event_heading_projects_section_without_propagating_direct_key(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "二、本次配股的认购方法",
            "（一）办理事项",
            "投资者应当在规定时间内办理相关手续。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("an exact event section must not call model")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="rights_issue"),
            drafts=drafts,
        )

        self.assertIsNone(result.units[1].semantic_keys)
        self.assertEqual(
            result.units[1].section_keys,
            ("subscription_arrangements",),
        )
        self.assertEqual(adjudicator.calls, 0)

    def test_event_section_requires_exact_heading_scope_and_unit_content(self) -> None:
        cases = (
            ("二、本次配股的认购方法说明", "rights_issue", True),
            ("二、本次配股的认购方法", "annual_report", True),
            ("二、本次配股的认购方法", "rights_issue", False),
        )
        for parent, filing_type, has_content in cases:
            with self.subTest(
                parent=parent,
                filing_type=filing_type,
                has_content=has_content,
            ):
                admitted, drafts = _drafts_with_parent_heading(
                    parent,
                    "（一）办理事项",
                    "投资者应当在规定时间内办理相关手续。",
                )
                selected = drafts if has_content else drafts[:1]
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "an ineligible event section must not call model"
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type=filing_type,
                    ),
                    drafts=selected,
                )

                self.assertTrue(
                    all(unit.section_keys is None for unit in result.units)
                )

    def test_restructuring_risk_suffix_is_the_direct_semantic_centre(self) -> None:
        for title in (
            "（三）标的资产的评估值风险",
            "（五）业绩承诺及其执行风险",
        ):
            with self.subTest(title=title):
                admitted, drafts = _drafts_with_body(
                    title,
                    "相关标的资产和业绩承诺存在执行与估值不确定性。",
                )
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail(
                        "a restructuring risk heading must be deterministic"
                    )
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type="restructuring_assets",
                    ),
                    drafts=drafts,
                )

                self.assertEqual(
                    result.units[0].semantic_keys,
                    ("transaction_risk",),
                )
                self.assertNotIn(
                    "target_asset",
                    result.units[0].semantic_keys or (),
                )
                self.assertNotIn(
                    "performance_commitment",
                    result.units[0].semantic_keys or (),
                )
                self.assertEqual(adjudicator.calls, 0)

    def test_official_year_prefixed_and_risk_sections_project_without_model(
        self,
    ) -> None:
        cases = (
            (
                "第五节 公司治理报告暨企业管治报告",
                "公司年度治理工作回顾",
                ("governance",),
            ),
            (
                "第六节 公司治理",
                "2025年度薪酬情况",
                ("governance", "directors_management"),
            ),
            (
                "第三节 经营情况讨论与分析",
                "公司面临的风险和应对措施",
                ("business_review", "risk_management"),
            ),
            (
                "第六节 公司治理",
                "审计委员会",
                ("governance", "board_committees"),
            ),
        )
        for parent, title, expected in cases:
            with self.subTest(title=title):
                admitted, drafts = _drafts_with_parent_heading(
                    parent,
                    title,
                    "报告期内公司按照监管要求披露本节内容。",
                )
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail(
                        "exact official section routes must not call the model"
                    )
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )

                self.assertIsNone(result.units[1].semantic_keys)
                self.assertEqual(result.units[1].section_keys, expected)
                self.assertEqual(adjudicator.calls, 0)

    def test_nearby_governance_heading_does_not_widen_exact_section_alias(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "第七节 企业管治报告",
            "报告期内主要工作",
            "公司按照监管要求推进本期工作。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("an inexact section must not call the model")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[1].semantic_keys)
        self.assertIsNone(result.units[1].section_keys)
        self.assertEqual(adjudicator.calls, 0)

    def test_official_company_investment_heading_projects_exact_section(self) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "第四节 董事会报告",
            "二、公司投资情况",
            "报告期内公司按监管要求披露本节投资事项。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("an exact investment section must not call model")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
        )

        self.assertEqual(result.units[1].section_keys, ("investment_analysis",))
        self.assertEqual(adjudicator.calls, 0)

    def test_nearby_investment_phrase_does_not_widen_exact_section_alias(self) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "第四节 董事会报告",
            "二、公司投资情况说明补充",
            "报告期内公司按监管要求披露本节投资事项。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("an inexact investment section must not call model")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
        )

        self.assertIsNone(result.units[1].section_keys)
        self.assertEqual(adjudicator.calls, 0)

    def test_generic_financial_titles_do_not_claim_bank_only_routes(self) -> None:
        taxonomy = load_semantic_route_taxonomy()
        keys = {definition.key for definition in taxonomy.definitions}

        self.assertIn("interest_income", keys)
        self.assertIn("interest_expense", keys)
        self.assertIn("capital_management", keys)
        self.assertNotIn("bank_interest_income", keys)
        self.assertNotIn("bank_interest_expense", keys)
        self.assertNotIn("bank_capital_management", keys)
        self.assertIn("bank_net_interest_income", keys)

    def test_other_information_is_not_a_synthetic_route(self) -> None:
        admitted, drafts = _drafts_with_body(
            "其他信息",
            "本节仅列示未归入前述标准章节的其他信息。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("other information must not call the model")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_keys)
        self.assertIsNone(result.units[0].section_keys)
        self.assertEqual(adjudicator.calls, 0)

    def test_committee_mention_in_body_does_not_create_a_direct_route(self) -> None:
        admitted, drafts = _drafts_with_body(
            "重要提示",
            "本季度报告已经董事会审计委员会审议通过。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("an exact title must not call the model")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="quarterly_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("important_notice",))
        self.assertIsNone(result.units[0].section_keys)
        self.assertEqual(adjudicator.calls, 0)

    def test_ordinary_or_inexact_parent_heading_never_propagates(self) -> None:
        cases = (
            ("第三节 经营情况讨论与分析", False),
            ("第三节 经营情况讨论与分析概述", True),
        )
        for parent, context_container in cases:
            with self.subTest(parent=parent, context_container=context_container):
                admitted, drafts = _drafts_with_parent_heading(
                    parent,
                    "报告期内主要工作",
                    "公司围绕年度目标稳步推进各项经营工作。",
                )
                taxonomy = SemanticRouteTaxonomy(
                    version="context-negative.v1",
                    definitions=(
                        SemanticRouteDefinition(
                            key="business_review",
                            description="经营情况讨论与分析",
                            labels=("经营情况讨论与分析",),
                            scopes=("annual_report",),
                            context_container=context_container,
                        ),
                    ),
                )
                result = SemanticRouter(
                    taxonomy=taxonomy,
                    adjudicator=_Adjudicator(
                        lambda batch: tuple(
                            SemanticAdjudicationDecision(
                                unit_index=unit.unit_index,
                                routes=(),
                            )
                            for unit in batch.units
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )

                self.assertIsNone(result.units[1].semantic_key)
                self.assertIsNone(result.units[1].section_keys)
                self.assertEqual(result.receipts[1].decision_source, "fallback")

    def test_direct_unit_route_and_section_projection_do_not_compete(self) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "第三节 经营情况讨论与分析",
            "主要经营数据",
            "营业收入：100万元。",
        )
        taxonomy = SemanticRouteTaxonomy(
            version="context-order.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="business_review",
                    description="经营情况讨论与分析",
                    labels=("经营情况讨论与分析",),
                    scopes=("annual_report",),
                    context_container=True,
                ),
                SemanticRouteDefinition(
                    key="revenue_and_cost",
                    description="营业收入和成本",
                    labels=("营业收入",),
                    scopes=("annual_report",),
                ),
            ),
        )

        def decide(
            batch: SemanticAdjudicationBatch,
        ) -> tuple[SemanticAdjudicationDecision, ...]:
            unit = batch.units[0]
            candidates = {candidate.key: candidate for candidate in unit.candidates}
            self.assertEqual(set(candidates), {"revenue_and_cost"})
            return (
                SemanticAdjudicationDecision(
                    unit_index=unit.unit_index,
                    routes=(
                        SemanticAdjudicatedRoute(
                            key="revenue_and_cost",
                            support_ids=candidates["revenue_and_cost"].source_ids,
                        ),
                    ),
                ),
            )

        adjudicator = _Adjudicator(decide)
        router = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        )
        result = router.route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[1].semantic_keys,
            ("revenue_and_cost",),
        )
        self.assertEqual(result.units[1].section_keys, ("business_review",))
        replayed = router.replay(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
            receipts=result.receipts,
        )
        self.assertEqual(replayed.units[1].semantic_keys, result.units[1].semantic_keys)
        self.assertEqual(replayed.units[1].section_keys, result.units[1].section_keys)
        self.assertEqual(adjudicator.calls, 0)

    def test_all_exact_normalized_section_ancestors_are_projected(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "第三节 经营情况讨论与分析"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "一、重要事项"),),
                        annotation="title",
                        level=2,
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, "1、进展情况"),),
                        annotation="title",
                        level=2,
                    ),
                    _block(
                        3,
                        0,
                        "text",
                        (ProviderPayload("text", None, "报告期内工作持续推进。"),),
                        annotation=None,
                    ),
                ),
            ),
            segments=(),
        )
        admitted = _admitted(document)
        drafts = build_provider_units(admitted).units
        taxonomy = SemanticRouteTaxonomy(
            version="nearest-context.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="business_review",
                    description="经营情况讨论与分析",
                    labels=("经营情况讨论与分析",),
                    scopes=("annual_report",),
                    context_container=True,
                ),
                SemanticRouteDefinition(
                    key="important_matters",
                    description="重要事项",
                    labels=("重要事项",),
                    scopes=("annual_report",),
                    context_container=True,
                ),
            ),
        )
        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("nearest context-only route is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
        )

        self.assertIsNone(result.units[2].semantic_keys)
        self.assertEqual(
            result.units[2].section_keys,
            ("business_review", "important_matters"),
        )
        self.assertFalse(result.receipts[2].candidate_keys)

    def test_heading_only_unit_does_not_inherit_context(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "第三节 经营情况讨论与分析"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "一、报告期内主要工作"),),
                        annotation="title",
                        level=2,
                    ),
                ),
            ),
            segments=(),
        )
        admitted = _admitted(document)
        drafts = build_provider_units(admitted).units
        taxonomy = SemanticRouteTaxonomy(
            version="empty-context.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="business_review",
                    description="经营情况讨论与分析",
                    labels=("经营情况讨论与分析",),
                    scopes=("annual_report",),
                    context_container=True,
                ),
            ),
        )
        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("empty child has no model candidate")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
        )

        self.assertIsNone(result.units[1].semantic_keys)
        self.assertIsNone(result.units[1].section_keys)
        self.assertEqual(result.receipts[1].decision_source, "fallback")

    def test_direct_body_evidence_precedes_similarity_only_at_candidate_cap(self) -> None:
        admitted, drafts = _drafts_with_body(
            "本次回购方案的主要内容如下：",
            "回购资金来源为自有资金和回购专项贷款",
        )
        router = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("broad candidate sets must not call the model")
            ),
            cache=_MemoryCache(),
        )
        prepared = router._prepare_input(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="股份回购公告",
                filing_type="share_buyback",
            ),
            draft=drafts[0],
        )

        self.assertEqual(len(prepared.candidates), 8)
        self.assertIn(
            "share_buyback_funding",
            {candidate.key for candidate in prepared.candidates},
        )

    def test_short_standard_fields_require_explicit_structure_to_lock(self) -> None:
        cases = (
            (
                "更正内容",
                "更正为：经审计净利润为一百万元",
                "correction_supplement",
                "corrected_data",
                True,
            ),
            (
                "激励计划实施情况",
                "作废",
                "equity_incentive",
                "incentive_cancellation",
                False,
            ),
        )
        for title, body, filing_type, expected, locked in cases:
            with self.subTest(expected=expected):
                admitted, drafts = _drafts_with_body(title, body)
                router = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail("candidate preparation only")
                    ),
                    cache=_MemoryCache(),
                )

                prepared = router._prepare_input(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type=filing_type,
                    ),
                    draft=drafts[0],
                )

                candidate = next(
                    item for item in prepared.candidates if item.key == expected
                )
                self.assertEqual(candidate.locked, locked)
                self.assertIn(
                    (
                        "source_labeled_field_exact"
                        if locked
                        else "source_body_candidate"
                    ),
                    candidate.evidence_kinds,
                )

    def test_nonperiodic_labeled_field_with_value_is_exact_without_model(self) -> None:
        admitted, drafts = _drafts_with_body(
            "回购资金安排",
            "资金来源：自有资金和回购专项贷款",
        )
        taxonomy = SemanticRouteTaxonomy(
            version="labeled-field.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="share_buyback_funding",
                    description="股份回购资金来源",
                    labels=("资金来源",),
                    scopes=("share_buyback",),
                ),
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("an exact labeled field must not call the model")
        )

        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="股份回购公告",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("share_buyback_funding",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        evidence = result.receipts[0].evidence[0]
        self.assertIn("source_labeled_field_exact", evidence.kinds)
        self.assertEqual(adjudicator.calls, 0)

    def test_empty_or_negated_labeled_field_never_locks_a_route(self) -> None:
        taxonomy = SemanticRouteTaxonomy(
            version="labeled-field-negative.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="share_buyback_funding",
                    description="股份回购资金来源",
                    labels=("资金来源",),
                    scopes=("share_buyback",),
                ),
            ),
        )
        for body in ("资金来源：不适用", "公司尚未披露资金来源：自有资金"):
            with self.subTest(body=body):
                admitted, drafts = _drafts_with_body("回购资金安排", body)
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail("one weak field must not call the model")
                )
                result = SemanticRouter(
                    taxonomy=taxonomy,
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="股份回购公告",
                        filing_type="share_buyback",
                    ),
                    drafts=drafts,
                )

                self.assertIsNone(result.units[0].semantic_keys)
                self.assertNotEqual(result.receipts[0].decision_source, "deterministic")
                self.assertEqual(adjudicator.calls, 0)

    def test_definitions_table_terms_never_authorize_event_routes(self) -> None:
        admitted, drafts = _drafts_with_parent_heading_and_table(
            "释义",
            "一、普通术语",
            (
                "<table>"
                "<tr><td>标的资产</td><td>指</td><td>某公司全部股权</td></tr>"
                "<tr><td>业绩承诺方</td><td>指</td><td>本次交易对方</td></tr>"
                "</table>"
            ),
        )
        drafts = (
            *drafts[:-1],
            replace(
                drafts[-1],
                heading_path=("释义", "一、普通术语"),
                locator=replace(
                    drafts[-1].locator,
                    heading_chain=(
                        drafts[0].locator.heading_chain[-1],
                        drafts[-1].locator.heading_chain[-1],
                    ),
                ),
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("glossary terms must remain lexical evidence")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="重大资产重组报告书",
                filing_type="restructuring_assets",
            ),
            drafts=drafts,
        )

        glossary_unit = result.units[-1]
        glossary_receipt = result.receipts[-1]
        self.assertIsNone(glossary_unit.semantic_keys)
        self.assertNotIn("target_asset", glossary_receipt.candidate_keys)
        self.assertNotIn("performance_commitment", glossary_receipt.candidate_keys)
        self.assertEqual(glossary_receipt.decision_source, "fallback")
        self.assertEqual(adjudicator.calls, 0)

    def test_definitions_child_title_never_authorizes_an_event_route(self) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "释义",
            "标的资产",
            "某公司全部股权",
        )
        drafts = (
            *drafts[:-1],
            replace(
                drafts[-1],
                heading_path=("释义", "标的资产"),
                locator=replace(
                    drafts[-1].locator,
                    heading_chain=(
                        drafts[0].locator.heading_chain[-1],
                        drafts[-1].locator.heading_chain[-1],
                    ),
                ),
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("glossary child headings are lexical evidence")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="重大资产重组报告书",
                filing_type="restructuring_assets",
            ),
            drafts=drafts,
        )

        child = result.units[-1]
        receipt = result.receipts[-1]
        self.assertIsNone(child.semantic_keys)
        self.assertNotIn("target_asset", receipt.candidate_keys)
        self.assertEqual(receipt.decision_source, "fallback")
        self.assertEqual(adjudicator.calls, 0)

    def test_untyped_table_cells_do_not_lock_a_route(self) -> None:
        admitted, drafts = _drafts_with_table(
            "交易基本信息",
            "<table><tr><td>标的资产</td><td>某公司全部股权</td></tr></table>",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("untyped table cells must stay lexical evidence")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="重大资产重组报告书",
                filing_type="restructuring_assets",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_keys)
        self.assertIn("target_asset", result.receipts[0].candidate_keys)
        self.assertEqual(result.receipts[0].decision_source, "rule_abstain")
        self.assertEqual(adjudicator.calls, 0)

    def test_explicit_table_label_value_atom_still_locks_a_route(self) -> None:
        admitted, drafts = _drafts_with_table(
            "交易基本信息",
            "<table><tr><td>标的资产：某公司全部股权</td></tr></table>",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("an explicit table label-value is deterministic")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="重大资产重组报告书",
                filing_type="restructuring_assets",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("target_asset",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_periodic_metric_subject_with_change_result_locks_direct_topic(self) -> None:
        admitted, drafts = _drafts_with_body(
            "所有者权益",
            "外币财务报表折算差额减少 12.5 亿元。",
        )
        router = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("candidate preparation only")
            ),
            cache=_MemoryCache(),
        )

        prepared = router._prepare_input(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="quarterly_report",
            ),
            draft=drafts[0],
        )

        candidate = next(
            item
            for item in prepared.candidates
            if item.key == "foreign_currency_translation"
        )
        self.assertTrue(candidate.locked)
        self.assertIn("source_quantitative_exact", candidate.evidence_kinds)

    def test_periodic_background_asset_near_number_does_not_lock_topic(self) -> None:
        admitted, drafts = _drafts_with_body(
            "短期借款分类",
            "抵押借款由投资性房地产、固定资产提供担保，借款余额为100亿元。",
        )
        router = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(lambda _batch: self.fail("periodic never models")),
            cache=_MemoryCache(),
        )

        prepared = router._prepare_input(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="annual_report",
            ),
            draft=drafts[0],
        )

        candidate = next(
            item for item in prepared.candidates if item.key == "investment_property"
        )
        self.assertFalse(candidate.locked)
        self.assertNotIn("source_quantitative_exact", candidate.evidence_kinds)

    def test_standardized_financial_field_without_number_remains_a_candidate(self) -> None:
        admitted, drafts = _drafts_with_body(
            "所有者权益",
            "外币财务报表折算差额受汇率变化影响。",
        )
        router = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("candidate preparation only")
            ),
            cache=_MemoryCache(),
        )

        prepared = router._prepare_input(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="quarterly_report",
            ),
            draft=drafts[0],
        )

        candidate = next(
            item
            for item in prepared.candidates
            if item.key == "foreign_currency_translation"
        )
        self.assertFalse(candidate.locked)
        self.assertIn("source_body_candidate", candidate.evidence_kinds)

    def test_dated_inquiry_background_does_not_lock_question_route(self) -> None:
        admitted, drafts = _drafts_with_body(
            "审核问询函回复",
            "公司于2024年收到审核问询函，并对问询问题逐项说明和回复。",
        )
        router = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("candidate preparation only")
            ),
            cache=_MemoryCache(),
        )

        prepared = router._prepare_input(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="审核问询函回复修订提示公告",
                filing_type="inquiry_regulatory",
            ),
            draft=drafts[0],
        )

        question = next(
            item for item in prepared.candidates if item.key == "inquiry_question"
        )
        self.assertFalse(question.locked)
        self.assertNotIn("source_quantitative_exact", question.evidence_kinds)

    def test_contained_statement_label_is_not_an_exclusive_container(self) -> None:
        admitted, drafts = _drafts_with_table(
            "附注为财务报表的组成部分",
            (
                "<table><tr><td>资产处置收益</td><td>70,849.09</td></tr>"
                "<tr><td>信用减值损失</td><td>-48,141,938,216.28</td></tr></table>"
            ),
        )
        router = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("candidate preparation only")
            ),
            cache=_MemoryCache(),
        )

        result = router.route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertNotIn(
            "financial_statements_section",
            result.units[0].semantic_keys or (),
        )
        self.assertNotIn(
            "financial_statements_section",
            result.receipts[0].candidate_keys,
        )

    def test_formally_approved_proposal_is_exact_direct_evidence(self) -> None:
        admitted, drafts = _drafts_with_body(
            "预留限制性股票授予情况",
            (
                "董事会审议通过《关于 2024 年限制性股票激励计划首次授予"
                "限制性股票第一个归属期归属条件成就的议案》。"
            ),
        )
        router = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("candidate preparation only")
            ),
            cache=_MemoryCache(),
        )

        prepared = router._prepare_input(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="equity_incentive",
            ),
            draft=drafts[0],
        )

        candidate = next(
            item
            for item in prepared.candidates
            if item.key == "incentive_condition_satisfaction"
        )
        self.assertTrue(candidate.locked)
        self.assertIn(
            "source_resolved_proposal_exact",
            candidate.evidence_kinds,
        )
        self.assertNotIn(
            "incentive_plan_overview",
            {item.key for item in prepared.candidates},
        )

    def test_unapproved_proposal_mention_remains_a_model_candidate(self) -> None:
        admitted, drafts = _drafts_with_body(
            "后续安排",
            (
                "如达到归属条件，公司将提交《关于限制性股票归属条件成就的"
                "议案》审议。"
            ),
        )
        router = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("candidate preparation only")
            ),
            cache=_MemoryCache(),
        )

        prepared = router._prepare_input(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="equity_incentive",
            ),
            draft=drafts[0],
        )

        candidate = next(
            item
            for item in prepared.candidates
            if item.key == "incentive_condition_satisfaction"
        )
        self.assertFalse(candidate.locked)
        self.assertIn("source_body_candidate", candidate.evidence_kinds)

    def test_legal_time_window_does_not_create_an_event_route(self) -> None:
        admitted, drafts = _drafts_with_body(
            "限制性股票归属安排",
            (
                "限制性股票不得在自可能对公司证券价格产生重大影响的事件发生之日，"
                "或者进入决策程序之日，至依法披露之日内归属。"
            ),
        )
        router = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("candidate preparation only")
            ),
            cache=_MemoryCache(),
        )

        prepared = router._prepare_input(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="equity_incentive",
            ),
            draft=drafts[0],
        )

        self.assertNotIn(
            "decision_procedures",
            {candidate.key for candidate in prepared.candidates},
        )


class SemanticRouterTests(unittest.TestCase):
    def test_semantic_routing_preserves_applicability_in_query_hash(self) -> None:
        admitted, drafts = _drafts_with_body(
            "营业收入",
            "□适用 √不适用",
        )
        self.assertEqual(drafts[0].applicability, "not_applicable")

        result = SemanticRouter(
            taxonomy=_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("exact revenue title must not call the model")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        routed = result.units[0]
        expected_hashes = compute_unit_hashes(
            payload_kind=routed.payload_kind,
            payload=routed.payload,
            title=routed.title,
            heading_path=list(routed.heading_path),
            semantic_key=routed.semantic_key,
            semantic_keys=(
                list(routed.semantic_keys)
                if routed.semantic_keys is not None
                else None
            ),
            section_keys=(
                list(routed.section_keys)
                if routed.section_keys is not None
                else None
            ),
            applicability=routed.applicability,
            quality_status=routed.quality_status,
            order_index=routed.unit_index + 1,
        )
        self.assertEqual(routed.applicability, "not_applicable")
        self.assertEqual(
            routed.query_projection_hash,
            expected_hashes.query_projection_hash,
        )

    def test_historical_grant_cohort_does_not_become_a_current_grant(self) -> None:
        admitted, drafts = _drafts_with_body(
            "本次限制性股票归属的具体情况",
            "本次归属股票数量为 100 万股，归属日为 2026 年 8 月 1 日。",
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("exact vesting title must not call the model")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="限制性股票归属结果公告",
                filing_type="equity_incentive",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[0].semantic_keys,
            ("incentive_vesting_exercise",),
        )
        self.assertEqual(result.receipts[0].decision_source, "deterministic")

    def test_typed_recipient_header_is_a_direct_secondary_route(self) -> None:
        admitted, drafts = _drafts_with_table(
            "本次归属的具体情况",
            "<table><tr><td>激励对象姓名</td><td>职务</td><td>归属数量</td>"
            "</tr><tr><td>张三</td><td>董事</td><td>1000</td></tr></table>",
        )

        adjudicator = _Adjudicator(
            lambda _batch: self.fail("an exact title does not need table enrichment")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="限制性股票归属结果公告",
                filing_type="equity_incentive",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[0].semantic_keys,
            ("incentive_vesting_exercise", "incentive_recipients"),
        )
        self.assertIn("incentive_recipients", result.receipts[0].candidate_keys)
        self.assertEqual(adjudicator.calls, 0)

    def test_two_column_buyback_form_restores_direct_field_routes(self) -> None:
        admitted, drafts = _drafts_with_table(
            "本次回购方案的主要内容如下：",
            "<table>"
            "<tr><td>回购方案首次披露日</td><td>2026/6/16</td></tr>"
            "<tr><td>回购方案实施期限</td><td>三个月</td></tr>"
            "<tr><td>回购资金来源</td><td>自有资金</td></tr>"
            "<tr><td>回购用途</td><td>维护公司价值</td></tr>"
            "<tr><td>回购股份方式</td><td>集中竞价</td></tr>"
            "<tr><td>回购证券账户名称</td><td>回购专用账户</td></tr>"
            "</table>",
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("closed form fields are deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="回购股份方案公告",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )

        self.assertTrue(
            {
                "share_buyback_plan",
                "share_buyback_term",
                "share_buyback_funding",
                "share_buyback_purpose",
                "share_buyback_method",
                "share_buyback_account",
            }.issubset(result.units[0].semantic_keys or ())
        )
        self.assertEqual(result.units[0].semantic_key, "share_buyback_plan")

    def test_two_column_investor_form_restores_overview_and_participants(self) -> None:
        admitted, drafts = _drafts_with_table(
            "投资者关系活动记录表",
            "<table>"
            "<tr><td>投资者关系活动类别</td><td>股东会</td></tr>"
            "<tr><td>参与单位及人员</td><td>某资产管理公司</td></tr>"
            "<tr><td>活动主要内容介绍</td><td>问题一：经营情况？回复：正常。</td></tr>"
            "</table>",
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("closed investor form is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="投资者关系活动记录表",
                filing_type="investor_relations",
            ),
            drafts=drafts,
        )

        self.assertTrue(
            {
                "investor_relations_overview",
                "participant_information",
            }.issubset(result.units[0].semantic_keys or ())
        )
        self.assertEqual(
            result.units[0].semantic_key,
            "investor_relations_overview",
        )

    def test_qa_headers_after_spanning_title_route_questions_answers(self) -> None:
        admitted, drafts = _drafts_with_table(
            "附件：",
            "<table>"
            '<tr><td colspan="3">年度业绩说明会交流要点</td></tr>'
            "<tr><td>序号</td><td>提问内容</td><td>回复内容</td></tr>"
            "<tr><td>1</td><td>经营情况？</td><td>经营正常。</td></tr>"
            "</table>",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("typed Q&A headers are deterministic")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="年度网上业绩说明会",
                filing_type="performance_briefing",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[0].semantic_keys,
            ("investor_questions_answers",),
        )
        self.assertEqual(adjudicator.calls, 0)

    def test_table_data_cell_does_not_become_a_typed_field(self) -> None:
        admitted, drafts = _drafts_with_table(
            "担保情况",
            "<table><tr><td>序号</td><td>被担保人</td><td>金额</td></tr>"
            "<tr><td>1</td><td>标的公司</td><td>3000</td></tr></table>",
        )
        prepared = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(lambda _batch: ()),
            cache=_MemoryCache(),
        )._prepare_input(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="重大资产重组报告书",
                filing_type="restructuring_assets",
            ),
            draft=drafts[0],
        )

        target = next(
            candidate for candidate in prepared.candidates if candidate.key == "target_asset"
        )
        self.assertFalse(target.locked)
        self.assertEqual(target.evidence_kinds, ("source_table_candidate",))

    def test_secondary_order_is_canonical_across_model_orderings(self) -> None:
        admitted, drafts = _drafts_with_body(
            "甲项指标和乙项指标",
            "丙项指标有独立披露。",
        )
        taxonomy = SemanticRouteTaxonomy(
            version="secondary-order.v1",
            definitions=tuple(
                SemanticRouteDefinition(
                    key=f"route_{suffix}",
                    description=f"{label}的直接事实",
                    labels=(label,),
                    scopes=("share_buyback",),
                )
                for suffix, label in (
                    ("a", "甲项指标"),
                    ("b", "乙项指标"),
                    ("c", "丙项指标"),
                )
            ),
        )

        def run(order: tuple[str, ...]):  # type: ignore[no-untyped-def]
            def decide(batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
                unit = batch.units[0]
                candidates = {item.key: item for item in unit.candidates}
                return (
                    SemanticAdjudicationDecision(
                        unit_index=unit.unit_index,
                        routes=tuple(
                            SemanticAdjudicatedRoute(
                                key=key,
                                support_ids=candidates[key].source_ids,
                            )
                            for key in order
                        ),
                    ),
                )

            return SemanticRouter(
                taxonomy=taxonomy,
                adjudicator=_Adjudicator(decide),
                cache=_MemoryCache(),
            ).route(
                admitted=admitted,
                document=SemanticDocumentContext(
                    title="股份回购公告",
                    filing_type="share_buyback",
                ),
                drafts=drafts,
            )

        first = run(("route_b", "route_c", "route_a"))
        second = run(("route_b", "route_a", "route_c"))

        self.assertEqual(first.units[0].semantic_keys, ("route_a", "route_b", "route_c"))
        self.assertEqual(first.units[0].semantic_keys, second.units[0].semantic_keys)
        self.assertEqual(
            first.units[0].query_projection_hash,
            second.units[0].query_projection_hash,
        )
        self.assertEqual(first.receipts[0].semantic_keys, second.receipts[0].semantic_keys)

    def test_standard_title_normalization_recovers_statutory_routes(self) -> None:
        cases = (
            (
                "未经审计资产负债表（续）",
                "quarterly_report",
                "balance_sheet_parent",
            ),
            (
                "2.1 本集团主要会计数据及财务指标",
                "quarterly_report",
                "company_profile_metrics",
            ),
            (
                "七、 公司主要会计数据和财务指标",
                "semiannual_report",
                "company_profile_metrics",
            ),
            (
                "本期业绩变动的主要原因",
                "performance_forecast",
                "performance_forecast_basis",
            ),
            (
                "上年同期经营业绩和财务状况",
                "performance_forecast",
                "performance_forecast_comparison",
            ),
            (
                "重要会计政策和会计估计的变更",
                "annual_report",
                "accounting_changes",
            ),
            (
                "主要财务数据",
                "quarterly_report",
                "company_profile_metrics",
            ),
            (
                "分季度主要财务指标",
                "annual_report",
                "company_profile_metrics",
            ),
            (
                "主要销售客户",
                "annual_report",
                "customer_concentration",
            ),
            (
                "主要供应商",
                "annual_report",
                "supplier_concentration",
            ),
            (
                "利润分配及分红派息预案",
                "annual_report",
                "profit_distribution_plan",
            ),
            (
                "预留限制性股票授予情况",
                "equity_incentive",
                "incentive_grant",
            ),
            (
                "本次限制性股票归属的具体情况",
                "equity_incentive",
                "incentive_vesting_exercise",
            ),
            (
                "债券付息方法",
                "convertible_bond",
                "bond_interest_method",
            ),
            (
                "2026 年 7 月份销售简报",
                "operating_data",
                "operating_metrics",
            ),
            (
                "风险提示",
                "performance_forecast",
                "risk_warning",
            ),
            (
                "预计回购注销后公司股权结构的变动情况",
                "share_buyback",
                "share_capital_change",
            ),
            (
                "回购方案的不确定性风险",
                "share_buyback",
                "share_buyback_risk",
            ),
            (
                "拟回购股份的用途、数量、占公司总股本的比例、资金总额",
                "share_buyback",
                "share_buyback_price_volume",
            ),
            (
                "收入 - 续",
                "annual_report",
                "revenue_recognition_policy",
            ),
            (
                "职工薪酬",
                "annual_report",
                "employee_benefits_policy",
            ),
            (
                "金融资产和金融负债的抵销",
                "annual_report",
                "financial_instruments_policy",
            ),
            (
                "利润表分析",
                "quarterly_report",
                "income_statement_analysis",
            ),
            (
                "流动性覆盖率信息",
                "quarterly_report",
                "bank_liquidity_metrics",
            ),
            (
                "可转换公司债券基本概况",
                "convertible_bond",
                "convertible_bond_overview",
            ),
            (
                "关于本次付息对象缴纳公司债券利息所得税的说明",
                "convertible_bond",
                "bond_interest_tax",
            ),
            (
                "授予日期",
                "equity_incentive",
                "incentive_grant",
            ),
            (
                "（1）授予日期：2024 年4月12 日",
                "equity_incentive",
                "incentive_grant",
            ),
            (
                "回购股份的目的",
                "share_buyback",
                "share_buyback_purpose",
            ),
            (
                "拟回购股份的种类",
                "share_buyback",
                "share_buyback_share_type",
            ),
            (
                "回购股份的方式",
                "share_buyback",
                "share_buyback_method",
            ),
            (
                "回购股份的实施期限",
                "share_buyback",
                "share_buyback_term",
            ),
            (
                "公司防范侵害债权人利益的相关安排",
                "share_buyback",
                "share_buyback_creditor_protection",
            ),
            (
                "股东会对董事会办理本次回购股份事宜的具体授权",
                "share_buyback",
                "share_buyback_authorization",
            ),
            (
                "主要资产被查封、扣押、冻结的情况",
                "annual_report",
                "restricted_assets",
            ),
            (
                "截至报告期末的优先股股东数量及持股情况",
                "quarterly_report",
                "other_equity_instruments",
            ),
            (
                "股东信息",
                "quarterly_report",
                "share_changes",
            ),
            (
                "本次交易方案调整情况",
                "restructuring_assets",
                "transaction_scheme_adjustment",
            ),
            (
                "本次交易方案调整构成交易方案重大调整",
                "restructuring_assets",
                "transaction_scheme_adjustment",
            ),
            (
                "本次交易方案调整前后对比情况",
                "restructuring_assets",
                "transaction_scheme_adjustment",
            ),
            (
                "与本次交易相关的风险",
                "restructuring_assets",
                "transaction_risk",
            ),
            (
                "本次交易方案重大调整的风险",
                "restructuring_assets",
                "transaction_risk",
            ),
            (
                "募集配套资金具体方案",
                "restructuring_assets",
                "supporting_financing",
            ),
            (
                "募集配套资金情况的简要介绍",
                "restructuring_assets",
                "supporting_financing",
            ),
            (
                "募集配套资金概况",
                "restructuring_assets",
                "supporting_financing",
            ),
            (
                "关于向不特定对象发行可转换公司债券的审核问询函回复",
                "inquiry_regulatory",
                "inquiry_response",
            ),
            (
                "关于签署重大合同的公告",
                "major_contract",
                "major_contract_overview",
            ),
            (
                "发行股份及支付现金购买资产方案简要介绍",
                "restructuring_assets",
                "restructuring_transaction_overview",
            ),
            (
                "本次交易具体方案",
                "restructuring_assets",
                "restructuring_transaction_overview",
            ),
        )
        rule_abstain_titles = {
            "股东信息",
        }
        for title, filing_type, expected in cases:
            with self.subTest(title=title):
                admitted, drafts = _drafts(title)

                def decide(
                    batch: SemanticAdjudicationBatch,
                ) -> tuple[SemanticAdjudicationDecision, ...]:
                    unit = batch.units[0]
                    candidate = next(
                        item for item in unit.candidates if item.key == expected
                    )
                    return (
                        SemanticAdjudicationDecision(
                            unit_index=unit.unit_index,
                            routes=(
                                SemanticAdjudicatedRoute(
                                    key=expected,
                                    support_ids=candidate.source_ids,
                                ),
                            ),
                        ),
                    )

                adjudicator = _Adjudicator(decide)
                router = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                )

                result = router.route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )

                expected_keys = None if title in rule_abstain_titles else (expected,)
                self.assertEqual(result.units[0].semantic_keys, expected_keys)
                expected_source = (
                    "fallback"
                    if title == "股东信息"
                    else (
                        "rule_abstain"
                        if title in rule_abstain_titles
                        else "deterministic"
                    )
                )
                self.assertEqual(result.receipts[0].decision_source, expected_source)
                self.assertEqual(adjudicator.calls, 0)

    def test_about_wrapper_does_not_promote_an_unrelated_notice(self) -> None:
        admitted, drafts = _drafts("关于审核问询函及募集说明书修订的提示性公告")
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("an unrelated notice must remain lexical")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="inquiry_regulatory",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_keys)
        self.assertEqual(result.receipts[0].decision_source, "rule_abstain")
        self.assertEqual(adjudicator.calls, 0)

    def test_major_contract_overview_keeps_exact_amount_secondary(self) -> None:
        admitted, drafts = _drafts_with_body(
            "关于签署重大合同的公告",
            "合同金额为人民币89.25亿元。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("two exact contract routes are deterministic")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="major_contract",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[0].semantic_keys,
            ("major_contract_overview", "contract_value"),
        )
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_scheme_adjustment_does_not_promote_an_untyped_table_cell(self) -> None:
        admitted, drafts = _drafts_with_table(
            "本次交易方案调整前后对比情况",
            "<table><tr><td>标的资产</td><td>某公司全部股权</td></tr></table>",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("two exact restructuring routes are deterministic")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="restructuring_assets",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[0].semantic_keys,
            ("transaction_scheme_adjustment",),
        )
        self.assertIn("target_asset", result.receipts[0].candidate_keys)
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_matching_scope_wins_duplicate_standard_title(self) -> None:
        admitted, drafts = _drafts("主要财务数据和指标")
        taxonomy = SemanticRouteTaxonomy(
            version="scoped.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="periodic_metrics",
                    description="定期报告主要指标",
                    labels=("主要财务数据和指标",),
                    scopes=("quarterly_report",),
                ),
                SemanticRouteDefinition(
                    key="flash_metrics",
                    description="业绩快报主要指标",
                    labels=("主要财务数据和指标",),
                    scopes=("performance_flash",),
                ),
            ),
        )
        router = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("matching scoped title must be deterministic")
            ),
            cache=_MemoryCache(),
        )
        result = router.route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="quarterly_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("periodic_metrics",))

    def test_known_document_scope_rejects_exact_foreign_family(self) -> None:
        admitted, drafts = _drafts("主要财务数据和指标")
        taxonomy = SemanticRouteTaxonomy(
            version="known-scope.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="flash_metrics",
                    description="业绩快报主要指标",
                    labels=("主要财务数据和指标",),
                    scopes=("performance_flash",),
                ),
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("foreign scope must not reach the model")
        )
        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="quarterly_report",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_key)
        self.assertIsNone(result.units[0].semantic_keys)
        self.assertEqual(result.receipts[0].decision_source, "fallback")

    def test_authoritative_disclosure_topic_can_open_cross_filing_scope(self) -> None:
        admitted, drafts = _drafts("风险提示")
        taxonomy = SemanticRouteTaxonomy(
            version="topic-scope.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="risk_warning",
                    description="公告集中披露的风险提示",
                    labels=("风险提示",),
                    scopes=("risk_alert",),
                ),
            ),
        )
        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("exact scoped title must be deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司销售简报",
                filing_type="operating_data",
                disclosure_topics=("operating_data", "risk_alert"),
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("risk_warning",))

    def test_provider_content_category_cannot_authorize_route_scope(self) -> None:
        admitted, drafts = _drafts("风险提示")
        taxonomy = SemanticRouteTaxonomy(
            version="category-context.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="risk_warning",
                    description="公告集中披露的风险提示",
                    labels=("风险提示",),
                    scopes=("risk_alert",),
                ),
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("provider category cannot open route scope")
        )
        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司销售简报",
                filing_type="operating_data",
                content_categories=("risk_alert",),
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_key)
        self.assertEqual(result.receipts[0].decision_source, "fallback")

    def test_low_overlap_title_does_not_manufacture_model_candidates(self) -> None:
        admitted, drafts = _drafts(
            "担保情况、偿债计划及其他偿债保障措施在报告期内的执行情况"
        )
        taxonomy = SemanticRouteTaxonomy(
            version="low-overlap.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="board_meetings_held",
                    description="董事会会议召开情况",
                    labels=("报告期内召开的董事会会议",),
                    scopes=("annual_report",),
                ),
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("low overlap must not reach the model")
        )
        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_key)
        self.assertIsNone(result.units[0].semantic_keys)
        self.assertEqual(result.receipts[0].candidate_keys, ())

    def test_scoped_statutory_full_title_locks_specific_route(self) -> None:
        admitted, drafts = _drafts("回购方案的审议及实施程序")
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("locked title must not call the model")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="关于回购股份方案的公告",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )

        self.assertNotIn("share_buyback_plan", result.receipts[0].candidate_keys)
        self.assertIn("decision_procedures", result.receipts[0].candidate_keys)
        self.assertEqual(result.units[0].semantic_keys, ("decision_procedures",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_specific_unit_canonicalizes_away_contextual_overview_route(self) -> None:
        admitted, drafts = _drafts_with_body(
            "回购方案的审议及实施程序",
            "董事会审议通过本次回购方案。",
        )
        taxonomy = SemanticRouteTaxonomy(
            version="conditional-container.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="share_buyback_plan",
                    description="股份回购方案与主要安排",
                    labels=("回购方案",),
                    scopes=("share_buyback",),
                    overview_container=True,
                ),
                SemanticRouteDefinition(
                    key="decision_procedures",
                    description="事项的决策和审议程序",
                    labels=("审议及实施程序",),
                ),
            ),
        )

        def decide(batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
            unit = batch.units[0]
            sources = {
                candidate.key: candidate.source_ids
                for candidate in unit.candidates
            }
            return (
                SemanticAdjudicationDecision(
                    unit_index=unit.unit_index,
                    routes=(
                        SemanticAdjudicatedRoute(
                            key="decision_procedures",
                            support_ids=sources["decision_procedures"],
                        ),
                        SemanticAdjudicatedRoute(
                            key="share_buyback_plan",
                            support_ids=sources["share_buyback_plan"],
                        ),
                    ),
                ),
            )

        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=_Adjudicator(decide),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="关于回购股份方案的公告",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("decision_procedures",))
        self.assertEqual(
            result.receipts[0].semantic_keys,
            ("decision_procedures",),
        )

    def test_specific_controlled_heading_ignores_overview_candidate(self) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "事项安排",
            "具体实施程序",
            "公司将按程序推进具体实施工作。",
        )
        taxonomy = SemanticRouteTaxonomy(
            version="overview-only.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="overview",
                    description="事项概览",
                    labels=("具体实施",),
                    overview_container=True,
                ),
                SemanticRouteDefinition(
                    key="procedure",
                    description="事项实施程序",
                    labels=("实施程序",),
                ),
            ),
        )

        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=_Adjudicator(
                lambda _batch: self.fail(
                    "the specific controlled heading must be deterministic"
                )
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="other"),
            drafts=(drafts[1],),
        )

        self.assertEqual(result.units[0].semantic_keys, ("procedure",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")

    def test_true_overview_body_anchor_routes_without_model(self) -> None:
        admitted, drafts = _drafts_with_body(
            "重大事项提示",
            "本次回购方案的资金来源为自有资金。",
        )
        taxonomy = SemanticRouteTaxonomy(
            version="conditional-container.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="share_buyback_plan",
                    description="股份回购方案与主要安排",
                    labels=("回购方案",),
                    scopes=("share_buyback",),
                    overview_container=True,
                ),
                SemanticRouteDefinition(
                    key="share_buyback_funding",
                    description="股份回购资金来源",
                    labels=("资金来源",),
                    scopes=("share_buyback",),
                ),
            ),
        )

        adjudicator = _Adjudicator(
            lambda _batch: self.fail("body-only candidates must remain lexical")
        )

        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="关于回购股份方案的公告",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("share_buyback_plan",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_specific_child_drops_broad_overview_before_model_admission(self) -> None:
        admitted, drafts = _drafts_with_body(
            "本次交易方案调整构成重大调整",
            "本次剔除标的资产后相关指标比例超过百分之二十。",
        )
        taxonomy = SemanticRouteTaxonomy(
            version="specific-child.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="transaction_overview",
                    description="交易方案概况",
                    labels=("交易方案",),
                    scopes=("restructuring_assets",),
                    overview_container=True,
                ),
                SemanticRouteDefinition(
                    key="target_asset",
                    description="标的资产情况",
                    labels=("标的资产",),
                    scopes=("restructuring_assets",),
                ),
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("specific child has no model ambiguity")
        )

        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="重大资产重组报告书",
                filing_type="restructuring_assets",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_keys)
        self.assertNotIn(
            "transaction_overview",
            result.receipts[0].candidate_keys,
        )
        self.assertEqual(result.receipts[0].decision_source, "rule_abstain")
        self.assertEqual(adjudicator.calls, 0)

    def test_locked_direct_route_restores_unique_overview_without_model(self) -> None:
        admitted, drafts = _drafts_with_body(
            "重要内容提示：资金来源",
            "本次回购方案的资金来源为自有资金。",
        )
        taxonomy = SemanticRouteTaxonomy(
            version="overview-recall.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="share_buyback_plan",
                    description="股份回购方案与主要安排",
                    labels=("回购方案",),
                    scopes=("share_buyback",),
                    overview_container=True,
                ),
                SemanticRouteDefinition(
                    key="share_buyback_funding",
                    description="股份回购资金来源",
                    labels=("资金来源",),
                    scopes=("share_buyback",),
                ),
            ),
        )

        adjudicator = _Adjudicator(
            lambda _batch: self.fail("locked direct evidence must bypass the model")
        )

        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="关于回购股份方案的公告",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[0].semantic_keys,
            ("share_buyback_plan", "share_buyback_funding"),
        )
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_single_paraphrased_notice_candidate_does_not_pay_for_model(self) -> None:
        admitted, drafts = _drafts("可转换公司债券 2026 年付息的公告")

        adjudicator = _Adjudicator(
            lambda _batch: self.fail("single weak candidate must not call model")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="可转换公司债券 2026 年付息的公告",
                filing_type="convertible_bond",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_keys)
        self.assertIn("interest_terms", result.receipts[0].candidate_keys)
        self.assertEqual(result.receipts[0].decision_source, "rule_abstain")
        self.assertEqual(adjudicator.calls, 0)

    def test_exact_annual_interest_calculation_heading_routes_terms(self) -> None:
        admitted, drafts = _drafts_with_body(
            "（2）年利息计算",
            "年利息的计算公式为 I=B×i，i 为当年票面利率。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("a canonical bond subheading is deterministic")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="可转换公司债券年度付息公告",
                filing_type="convertible_bond",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("interest_terms",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_source_exact_exclusive_container_drops_incidental_candidates(self) -> None:
        admitted, drafts = _drafts_with_body(
            "合并资产负债表",
            "交易性金融资产 其他权益工具投资",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("exclusive exact title must not call model")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("balance_sheet",))
        self.assertEqual(result.receipts[0].candidate_keys, ("balance_sheet",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_periodic_broad_heading_keeps_direct_metric_results_as_routes(self) -> None:
        admitted, drafts = _drafts_with_body(
            "所有者权益",
            "未分配利润增加，其他综合收益下降，外币报表折算差额减少。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("periodic body ambiguity must not call model")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="quarterly_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            set(result.units[0].semantic_keys or ()),
            {
                "foreign_currency_translation",
                "other_comprehensive_income",
                "retained_earnings",
            },
        )
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_periodic_body_without_heading_can_keep_direct_metric_results(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_body_only(
            "未分配利润增加，其他综合收益下降。",
        )
        self.assertIsNone(drafts[0].title)

        adjudicator = _Adjudicator(
            lambda _batch: self.fail("periodic body ambiguity must not call model")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="quarterly_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            set(result.units[0].semantic_keys or ()),
            {"retained_earnings", "other_comprehensive_income"},
        )
        self.assertEqual(
            set(result.receipts[0].candidate_keys),
            {"retained_earnings", "other_comprehensive_income"},
        )
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_numbered_operating_metrics_heading_is_an_exact_direct_route(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_body(
            "4.7 其他重要业务指标",
            "零售客户数和管理零售客户总资产均保持增长。",
        )
        adjudicator = _Adjudicator(lambda _batch: self.fail("model was called"))

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="quarterly_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("operating_metrics",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_similarity_only_secondary_is_removed_before_receipt(self) -> None:
        admitted, drafts = _drafts_with_body(
            "本次归属后对公司财务指标的影响",
            "总股本增加，并摊薄每股收益和净资产收益率。",
        )

        def decide(batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
            unit = batch.units[0]
            candidates = {item.key: item for item in unit.candidates}
            self.assertEqual(
                candidates["share_capital_change"].evidence_kinds,
                ("source_heading_similarity",),
            )
            return (
                SemanticAdjudicationDecision(
                    unit_index=unit.unit_index,
                    routes=(
                        SemanticAdjudicatedRoute(
                            key="dilution_impact",
                            support_ids=candidates["dilution_impact"].source_ids,
                        ),
                        SemanticAdjudicatedRoute(
                            key="share_capital_change",
                            support_ids=candidates["share_capital_change"].source_ids,
                        ),
                    ),
                ),
            )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(decide),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="限制性股票归属公告",
                filing_type="equity_incentive",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("dilution_impact",))
        self.assertEqual(result.receipts[0].semantic_keys, ("dilution_impact",))

    def test_overview_route_drops_similarity_detail_without_model(self) -> None:
        admitted, drafts = _drafts("情况概要")
        taxonomy = SemanticRouteTaxonomy(
            version="overview-similarity.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="overview",
                    description="事项情况概要方案",
                    labels=("情况概要方案",),
                    scopes=("share_buyback",),
                    overview_container=True,
                ),
                SemanticRouteDefinition(
                    key="direct_detail",
                    description="事项情况概述",
                    labels=("情况概述",),
                    scopes=("share_buyback",),
                ),
            ),
        )

        adjudicator = _Adjudicator(
            lambda _batch: self.fail("true overview anchor must be deterministic")
        )

        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司回购公告",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("overview",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_forecast_summary_keeps_numeric_range_without_model(self) -> None:
        admitted, drafts = _drafts_with_body(
            "业绩预告情况",
            "预计归属于股东的净利润为-18亿元到-15亿元。",
        )

        adjudicator = _Adjudicator(
            lambda _batch: self.fail("locked summary must not call model")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司业绩预告",
                filing_type="performance_forecast",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[0].semantic_keys,
            ("performance_forecast_summary", "performance_forecast_range"),
        )
        self.assertIn("performance_forecast_range", result.receipts[0].candidate_keys)
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_year_prefixed_flash_data_heading_is_exact_without_model(self) -> None:
        admitted, drafts = _drafts("一、2026年半年度主要财务数据和指标")
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("standard flash heading must not call model")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某银行2026年半年度业绩快报",
                filing_type="performance_flash",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[0].semantic_keys,
            ("performance_flash_data",),
        )
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_exact_periodic_heading_ignores_broader_title_candidates(self) -> None:
        admitted, drafts = _drafts_with_body(
            "公允价值计量项目相关情况及持有外币金融资产和金融负债情况",
            "交易性金融资产和其他权益工具投资的期末金额如下。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("exact periodic heading must not call model")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("fair_value_disclosure",))
        self.assertEqual(
            result.receipts[0].candidate_keys,
            ("fair_value_disclosure",),
        )
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_specific_buyback_child_does_not_admit_parent_by_lexical_overlap(self) -> None:
        admitted, drafts = _drafts(
            "拟回购股份的用途、数量、占公司总股本的比例、资金总额"
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("exact child title must be deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="关于回购股份方案的公告",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[0].semantic_keys,
            ("share_buyback_price_volume",),
        )
        self.assertNotIn("share_buyback_plan", result.receipts[0].candidate_keys)

    def test_exclusive_container_discards_body_line_item_without_model(self) -> None:
        admitted, drafts = _drafts_with_body("报表容器", "报表容器 报表行项目")
        taxonomy = SemanticRouteTaxonomy(
            version="container.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="statement_container",
                    description="完整报表",
                    labels=("报表容器",),
                    exclusive_container=True,
                ),
                SemanticRouteDefinition(
                    key="statement_line_item",
                    description="报表行项目",
                    labels=("报表行项目",),
                ),
            ),
        )

        adjudicator = _Adjudicator(
            lambda _batch: self.fail("exact exclusive container must be deterministic")
        )
        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="other"),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("statement_container",))
        self.assertEqual(adjudicator.calls, 0)

    def test_model_support_must_be_the_source_that_generated_candidate(self) -> None:
        admitted, drafts = _drafts_with_body(
            "业绩预告和预计业绩区间",
            "业绩变动原因",
        )

        def decide(batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
            unit = batch.units[0]
            return (
                SemanticAdjudicationDecision(
                    unit_index=unit.unit_index,
                    routes=(
                        SemanticAdjudicatedRoute(
                            key="performance_forecast_summary",
                            support_ids=("document:title",),
                        ),
                    ),
                ),
            )

        with self.assertRaisesRegex(
            SemanticRouteAdjudicatorError,
            "repeated an invalid",
        ):
            _router(_Adjudicator(decide)).route(
                admitted=admitted,
                document=SemanticDocumentContext(
                    title=None,
                    filing_type="performance_forecast",
                ),
                drafts=drafts,
            )

    def test_repeated_invalid_single_unit_decision_is_retryable_failure(self) -> None:
        admitted, drafts = _drafts_with_body(
            "业绩预告和预计业绩区间",
            "业绩变动原因",
        )

        def decide(batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
            unit = batch.units[0]
            return (
                SemanticAdjudicationDecision(
                    unit_index=unit.unit_index,
                    routes=(
                        SemanticAdjudicatedRoute(
                            key="performance_forecast_summary",
                            support_ids=("u999:title",),
                        ),
                    ),
                ),
            )

        with self.assertRaises(SemanticRouteAdjudicatorError) as raised:
            _router(_Adjudicator(decide)).route(
                admitted=admitted,
                document=SemanticDocumentContext(
                    title="某公司业绩预告",
                    filing_type="performance_forecast",
                ),
                drafts=drafts,
            )

        self.assertEqual(raised.exception.reason_code, "invalid_decision")
        self.assertFalse(raised.exception.retryable)

    def test_invalid_batched_support_is_retried_once_as_a_single_unit(self) -> None:
        admitted, drafts = _drafts_with_body(
            "业绩预告和预计业绩区间",
            "业绩变动原因",
        )
        calls: list[int] = []

        def decide(batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
            calls.append(len(batch.units))
            unit = batch.units[0]
            summary = next(
                item
                for item in unit.candidates
                if item.key == "performance_forecast_summary"
            )
            support = "u999:title" if len(calls) == 1 else summary.source_ids[0]
            return (
                SemanticAdjudicationDecision(
                    unit_index=unit.unit_index,
                    routes=(
                        SemanticAdjudicatedRoute(
                            key="performance_forecast_summary",
                            support_ids=(support,),
                        ),
                    ),
                ),
            )

        result = _router(_Adjudicator(decide)).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司业绩预告",
                filing_type="performance_forecast",
            ),
            drafts=drafts,
        )

        self.assertEqual(calls, [1, 1])
        self.assertEqual(result.units[0].semantic_key, "performance_forecast_summary")

    def test_sparse_diagnostic_subset_preserves_source_unit_index(self) -> None:
        admitted = _admitted(_representative_document())
        drafts = build_provider_units(admitted).units
        selected = (drafts[1],)
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("title-only ambiguity must abstain locally")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="other"),
            drafts=selected,
        )

        self.assertEqual(result.units[0].unit_index, 1)

    def test_ambiguous_exact_label_is_not_locked_into_multiple_routes(self) -> None:
        admitted, drafts = _drafts("对公司的影响")
        taxonomy = SemanticRouteTaxonomy(
            version="ambiguous.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="financial_impact",
                    description="一般财务影响",
                    labels=("对公司的影响",),
                    scopes=("correction_supplement",),
                ),
                SemanticRouteDefinition(
                    key="correction_impact",
                    description="更正影响",
                    labels=("对公司的影响",),
                    scopes=("correction_supplement",),
                ),
            ),
        )
        adjudicator = _Adjudicator(
            lambda batch: tuple(
                SemanticAdjudicationDecision(unit_index=unit.unit_index, routes=())
                for unit in batch.units
            )
        )
        router = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        )

        result = router.route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="更正公告",
                filing_type="correction_supplement",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.receipts[0].decision_source, "rule_abstain")
        self.assertEqual(adjudicator.calls, 0)

    def test_exact_own_heading_in_matching_scope_is_deterministic(self) -> None:
        admitted, drafts = _drafts("营业收入")
        adjudicator = _Adjudicator(lambda _batch: self.fail("model must not run"))

        result = _router(adjudicator).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_key, "revenue_and_cost")
        self.assertEqual(result.units[0].semantic_keys, ("revenue_and_cost",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_locked_route_does_not_call_model_for_optional_body_secondary(self) -> None:
        admitted, drafts = _drafts_with_body("回购方案", "回购资金来源为自有资金")
        taxonomy = SemanticRouteTaxonomy(
            version="summary-secondary.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="share_buyback_plan",
                    description="股份回购方案",
                    labels=("回购方案",),
                    scopes=("share_buyback",),
                ),
                SemanticRouteDefinition(
                    key="share_buyback_funding",
                    description="回购资金来源",
                    labels=("回购资金来源",),
                    scopes=("share_buyback",),
                ),
            ),
        )

        adjudicator = _Adjudicator(
            lambda _batch: self.fail("a locked route must not call the model")
        )
        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="回购报告书",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[0].semantic_keys,
            ("share_buyback_plan",),
        )
        self.assertIn(
            "share_buyback_funding",
            result.receipts[0].candidate_keys,
        )
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_scoped_controlled_title_containment_with_content_is_deterministic(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_body(
            "公司股票可能被实施交易类强制退市",
            "公司股票收盘价首次低于一元。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("a controlled direct heading must not use a model")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="终止上市风险提示公告",
                filing_type="delisting_risk",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("delisting_risk",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_title_only_multiple_candidates_abstain_without_model(self) -> None:
        admitted, drafts = _drafts(
            "重大违法强制退市的终止上市风险提示公告"
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("a title-only carrier must not use a model")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="终止上市风险提示公告",
                filing_type="delisting_risk",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_keys)
        self.assertEqual(result.receipts[0].decision_source, "rule_abstain")
        self.assertEqual(adjudicator.calls, 0)

    def test_corroborated_controlled_heading_ambiguity_uses_cached_model(self) -> None:
        admitted, drafts = _drafts_with_body(
            "业绩预告和预计业绩区间",
            "业绩变动原因",
        )

        def decide(batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
            unit = batch.units[0]
            summary = next(
                item
                for item in unit.candidates
                if item.key == "performance_forecast_summary"
            )
            return (
                SemanticAdjudicationDecision(
                    unit_index=unit.unit_index,
                    routes=(
                        SemanticAdjudicatedRoute(
                            key="performance_forecast_summary",
                            support_ids=summary.source_ids,
                        ),
                    ),
                ),
            )

        cache = _MemoryCache()
        adjudicator = _Adjudicator(decide)
        router = _router(adjudicator, cache)
        context = SemanticDocumentContext(
            title="某公司业绩预告",
            filing_type="performance_forecast",
        )

        first = router.route(admitted=admitted, document=context, drafts=drafts)
        second = router.route(admitted=admitted, document=context, drafts=drafts)

        self.assertEqual(first.units[0].semantic_key, "performance_forecast_summary")
        self.assertEqual(first.receipts[0].decision_source, "model")
        self.assertFalse(first.receipts[0].adjudicator.cache_hit)  # type: ignore[union-attr]
        self.assertTrue(second.receipts[0].adjudicator.cache_hit)  # type: ignore[union-attr]
        self.assertEqual(adjudicator.calls, 1)

    def test_multiple_body_phrase_candidates_do_not_call_model(self) -> None:
        admitted, drafts = _drafts_with_body(
            "附件",
            "业绩预告 预计业绩区间",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail(
                "body phrase collisions are lexical evidence, not model input"
            )
        )

        result = _router(adjudicator).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司业绩预告",
                filing_type="performance_forecast",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_keys)
        self.assertEqual(result.receipts[0].decision_source, "rule_abstain")
        self.assertEqual(adjudicator.calls, 0)

    def test_cache_identity_binds_the_complete_fixed_model_group(self) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "业绩预告和预计业绩区间甲"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "业绩变动原因"),),
                        annotation=None,
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, "业绩预告和预计业绩区间乙"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        3,
                        0,
                        "text",
                        (ProviderPayload("text", None, "业绩变动原因"),),
                        annotation=None,
                    ),
                ),
            ),
            segments=(),
        )
        admitted = _admitted(document)
        drafts = build_provider_units(admitted).units
        self.assertEqual(len(drafts), 2)

        def decide(batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
            return tuple(
                SemanticAdjudicationDecision(
                    unit_index=unit.unit_index,
                    routes=(
                        SemanticAdjudicatedRoute(
                            key="performance_forecast_summary",
                            support_ids=next(
                                item.source_ids
                                for item in unit.candidates
                                if item.key == "performance_forecast_summary"
                            ),
                        ),
                    ),
                )
                for unit in batch.units
            )

        cache = _MemoryCache()
        adjudicator = _Adjudicator(decide)
        router = SemanticRouter(
            taxonomy=_taxonomy(),
            adjudicator=adjudicator,
            cache=cache,
            batch_size=8,
        )
        context = SemanticDocumentContext(
            title="某公司业绩预告",
            filing_type="performance_forecast",
        )

        first = router.route(admitted=admitted, document=context, drafts=drafts)
        self.assertEqual(adjudicator.calls, 1)
        self.assertEqual(len(cache.values), 2)
        cache.values.pop(next(iter(cache.values)))
        second = router.route(admitted=admitted, document=context, drafts=drafts)
        self.assertEqual(adjudicator.calls, 2)
        self.assertTrue(
            all(
                receipt.adjudicator is not None
                and not receipt.adjudicator.cache_hit
                for receipt in second.receipts
            )
        )
        third = router.route(admitted=admitted, document=context, drafts=drafts)
        self.assertEqual(adjudicator.calls, 2)
        self.assertTrue(
            all(
                receipt.adjudicator is not None
                and receipt.adjudicator.cache_hit
                for receipt in third.receipts
            )
        )

        sparse = router.route(
            admitted=admitted,
            document=context,
            drafts=(drafts[0],),
        )
        self.assertEqual(adjudicator.calls, 3)
        self.assertNotEqual(
            first.receipts[0].adjudicator.cache_key,  # type: ignore[union-attr]
            sparse.receipts[0].adjudicator.cache_key,  # type: ignore[union-attr]
        )

    def test_model_abstention_is_a_receipt_not_a_placeholder_db_key(self) -> None:
        admitted, drafts = _drafts_with_body(
            "业绩预告和预计业绩区间",
            "业绩变动原因",
        )
        adjudicator = _Adjudicator(
            lambda batch: tuple(
                SemanticAdjudicationDecision(unit_index=unit.unit_index, routes=())
                for unit in batch.units
            )
        )

        result = _router(adjudicator).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司业绩预告",
                filing_type="performance_forecast",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_key)
        self.assertIsNone(result.units[0].semantic_keys)
        self.assertEqual(result.receipts[0].decision_source, "model_abstain")
        self.assertEqual(result.receipts[0].semantic_keys, ("document_content",))

    def test_model_can_return_eight_bounded_routes_but_not_nine(self) -> None:
        accepted = SemanticAdjudicationDecision(
            unit_index=0,
            routes=tuple(
                SemanticAdjudicatedRoute(
                    key=f"route_{index}",
                    support_ids=("u0:title",),
                )
                for index in range(8)
            ),
        )
        self.assertEqual(len(accepted.routes), 8)
        with self.assertRaisesRegex(SemanticRouteContractError, "size"):
            SemanticAdjudicationDecision(
                unit_index=0,
                routes=tuple(
                    SemanticAdjudicatedRoute(
                        key=f"route_{index}",
                        support_ids=("u0:title",),
                    )
                    for index in range(9)
                ),
            )

    def test_document_context_alone_cannot_create_a_model_candidate(self) -> None:
        admitted, drafts = _drafts("XYZQ")
        context = SemanticDocumentContext(
            title="关于回购方案的公告",
            filing_type="share_repurchase",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("document-only context must not call model")
        )
        result = _router(adjudicator).route(
            admitted=admitted,
            document=context,
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_key)
        self.assertEqual(result.receipts[0].decision_source, "fallback")

    def test_model_cannot_select_outside_candidates(self) -> None:
        admitted, drafts = _drafts_with_body(
            "业绩预告和预计业绩区间",
            "业绩变动原因",
        )

        def decide_invalid(
            batch: SemanticAdjudicationBatch,
        ) -> tuple[SemanticAdjudicationDecision, ...]:
            return (
                SemanticAdjudicationDecision(
                    unit_index=batch.units[0].unit_index,
                    routes=(
                        SemanticAdjudicatedRoute(
                            key="not_in_taxonomy",
                            support_ids=("u0:title",),
                        ),
                    ),
                ),
            )

        with self.assertRaises(SemanticRouteAdjudicatorError):
            _router(_Adjudicator(decide_invalid)).route(
                admitted=admitted,
                document=SemanticDocumentContext(
                    title="某公司业绩预告",
                    filing_type="performance_forecast",
                ),
                drafts=drafts,
            )

    def test_publish_replay_rejects_source_context_drift_without_model(self) -> None:
        admitted, drafts = _drafts_with_body(
            "业绩预告和预计业绩区间",
            "业绩变动原因",
        )
        adjudicator = _Adjudicator(
            lambda batch: tuple(
                SemanticAdjudicationDecision(unit_index=unit.unit_index, routes=())
                for unit in batch.units
            )
        )
        router = _router(adjudicator)
        context = SemanticDocumentContext(
            title="某公司业绩预告",
            filing_type="performance_forecast",
        )
        routed = router.route(admitted=admitted, document=context, drafts=drafts)

        with self.assertRaisesRegex(SemanticRouteContractError, "input drifted"):
            router.replay(
                admitted=admitted,
                document=replace(context, title="更正后的业绩预告"),
                drafts=drafts,
                receipts=routed.receipts,
            )
        self.assertEqual(adjudicator.calls, 1)

    def test_publish_replay_uses_frozen_model_identity_after_config_upgrade(self) -> None:
        admitted, drafts = _drafts_with_body(
            "业绩预告和预计业绩区间",
            "业绩变动原因",
        )
        first_adjudicator = _Adjudicator(
            lambda batch: tuple(
                SemanticAdjudicationDecision(
                    unit_index=unit.unit_index,
                    routes=(
                        SemanticAdjudicatedRoute(
                            key="performance_forecast_summary",
                            support_ids=next(
                                item.source_ids
                                for item in unit.candidates
                                if item.key == "performance_forecast_summary"
                            ),
                        ),
                    ),
                )
                for unit in batch.units
            )
        )
        context = SemanticDocumentContext(
            title="某公司业绩预告",
            filing_type="performance_forecast",
        )
        routed = _router(first_adjudicator).route(
            admitted=admitted,
            document=context,
            drafts=drafts,
        )
        upgraded_adjudicator = _Adjudicator(
            lambda _batch: self.fail("Publish replay must not call the new model")
        )
        upgraded_adjudicator._identity = SemanticAdjudicatorIdentity(
            adapter="semantic-test.v2",
            model="semantic-upgraded-model",
            prompt_version="semantic-upgraded-prompt.v2",
        )

        replayed = _router(upgraded_adjudicator).replay(
            admitted=admitted,
            document=context,
            drafts=drafts,
            receipts=routed.receipts,
        )

        self.assertEqual(
            replayed.units[0].semantic_keys,
            ("performance_forecast_summary",),
        )
        self.assertEqual(upgraded_adjudicator.calls, 0)

    def test_receipt_hash_binds_candidate_definition_content(self) -> None:
        admitted, drafts = _drafts("营业收入")
        context = SemanticDocumentContext(title=None, filing_type="annual_report")
        first_taxonomy = SemanticRouteTaxonomy(
            version="same-version.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="revenue_and_cost",
                    description="营业收入和成本",
                    labels=("营业收入",),
                    scopes=("annual_report",),
                ),
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("exact title must not call model")
        )
        first_router = SemanticRouter(
            taxonomy=first_taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        )
        routed = first_router.route(
            admitted=admitted,
            document=context,
            drafts=drafts,
        )
        changed_router = SemanticRouter(
            taxonomy=replace(
                first_taxonomy,
                definitions=(
                    replace(
                        first_taxonomy.definitions[0],
                        description="已变更的候选定义",
                    ),
                ),
            ),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        )

        with self.assertRaisesRegex(SemanticRouteContractError, "input drifted"):
            changed_router.replay(
                admitted=admitted,
                document=context,
                drafts=drafts,
                receipts=routed.receipts,
            )


if __name__ == "__main__":
    unittest.main()
