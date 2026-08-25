from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_FAILOVER_POLICY_VERSION,
    SEMANTIC_PROMPT_VERSION,
    SemanticAdjudicatedRoute,
    SemanticAdjudicationDecision,
    SemanticDocumentContext,
    SemanticProviderAttempt,
    SemanticProviderIdentity,
    SemanticRouteContractError,
    SemanticRouteDefinition,
    SemanticRouteTaxonomy,
)
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicationBatch,
    SemanticAdjudicationOutcome,
    SemanticAdjudicatorIdentity,
    SemanticRouteAdjudicatorError,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
)
from disclosure_anchor.application.services.semantic_router import (
    SemanticRouter,
    SemanticRouteBatchResult,
    _normalize_title,
)
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
from disclosure_anchor.application.contracts.provider_document import (
    ProviderBBox,
    ProviderDocument,
    ProviderPayload,
)


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


class _Executor:
    def __init__(
        self,
        decide: Callable[
            [SemanticAdjudicationBatch], tuple[SemanticAdjudicationDecision, ...]
        ],
    ) -> None:
        self.decide = decide
        self.calls = 0
        self._identity = SemanticProviderIdentity(
            provider_id="test-primary",
            provider="test",
            adapter_kind="test_cli",
            adapter_version="test_cli.v1",
            canonical_model="test-model",
            inference_profile="low",
            prompt_version="semantic-test-prompt.v1",
            prompt_sha256="sha256:" + "1" * 64,
            output_schema_version="semantic-test-schema.v1",
            output_schema_sha256="sha256:" + "2" * 64,
        )

    @property
    def provider_identities(self) -> tuple[SemanticProviderIdentity, ...]:
        return (self._identity,)

    def adjudicate(
        self,
        batch: SemanticAdjudicationBatch,
        *,
        group_hash: str,
    ) -> SemanticAdjudicationOutcome:
        self.calls += 1
        decisions = self.decide(batch)
        response_hash = "sha256:" + "3" * 64
        return SemanticAdjudicationOutcome(
            policy_version=SEMANTIC_FAILOVER_POLICY_VERSION,
            group_hash=group_hash,
            attempts=(
                SemanticProviderAttempt(
                    ordinal=1,
                    provider=self._identity,
                    outcome="succeeded",
                    response_sha256=response_hash,
                ),
            ),
            decisions=decisions,
            actual_result_attempt=1,
            actual_result_identity=self._identity,
            group_response_sha256=response_hash,
            degraded_unavailable=False,
        )


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
                _block(
                    1,
                    0,
                    "text",
                    (ProviderPayload("text", None, "正文。"),),
                    annotation=None,
                ),
            ),
        ),
        segments=(),
    )
    admitted = _admitted(document)
    return admitted, build_provider_units(admitted).units


def _heading_only_drafts(title: str):  # type: ignore[no-untyped-def]
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


def _two_model_drafts():  # type: ignore[no-untyped-def]
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
    return admitted, build_provider_units(admitted).units


def _select_forecast_summary(
    batch: SemanticAdjudicationBatch,
) -> tuple[SemanticAdjudicationDecision, ...]:
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


def _drafts_with_bodies(title: str, *bodies: str):  # type: ignore[no-untyped-def]
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
                *(
                    _block(
                        index,
                        0,
                        "text",
                        (ProviderPayload("text", None, body),),
                        annotation=None,
                    )
                    for index, body in enumerate(bodies, start=1)
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


def _drafts_with_bodies_and_table(
    title: str,
    bodies: tuple[str, ...],
    table_html: str,
):  # type: ignore[no-untyped-def]
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
                *(
                    _block(
                        index,
                        0,
                        "text",
                        (ProviderPayload("text", None, body),),
                        annotation=None,
                    )
                    for index, body in enumerate(bodies, start=1)
                ),
                _block(
                    len(bodies) + 1,
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

        self.assertEqual(len(taxonomy.definitions), 344)
        self.assertEqual(len(taxonomy.by_key()), 344)
        self.assertNotIn(taxonomy.fallback_key, taxonomy.by_key())
        self.assertNotIn("other_information", taxonomy.by_key())
        self.assertNotIn("other_significant_events", taxonomy.by_key())
        self.assertEqual(
            taxonomy.by_key()["audit_opinion"].labels,
            ("审计报告", "审计意见"),
        )
        self.assertEqual(
            taxonomy.by_key()["key_audit_matters"].labels,
            ("关键审计事项",),
        )
        self.assertIn(
            "预期信用损失",
            taxonomy.by_key()["credit_risk"].labels,
        )
        self.assertIn(
            "合同履约成本",
            taxonomy.by_key()["revenue_recognition_policy"].labels,
        )
        self.assertIn(
            "权益工具",
            taxonomy.by_key()["financial_instruments_policy"].heading_labels,
        )
        self.assertIn(
            "金融负债的终止确认",
            taxonomy.by_key()["financial_instruments_policy"].heading_labels,
        )
        self.assertEqual(
            taxonomy.by_key()["transaction_share_lockup"].labels,
            ("锁定期安排", "股份锁定安排", "股份锁定期"),
        )
        self.assertEqual(
            taxonomy.by_key()["transition_period_profit_loss"].labels,
            ("期间损益安排", "过渡期损益安排", "损益归属期间安排"),
        )
        self.assertEqual(
            taxonomy.by_key()["issue_allottees"].labels,
            ("配售对象", "发行对象", "认购对象"),
        )
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
        self.assertTrue(taxonomy.by_key()["directors_report"].context_container)
        self.assertTrue(taxonomy.by_key()["board_committees"].context_container)
        self.assertTrue(taxonomy.by_key()["risk_management"].context_container)
        self.assertFalse(taxonomy.by_key()["business_risk"].context_container)
        self.assertTrue(taxonomy.by_key()["issuance_plan"].section_container)
        self.assertTrue(taxonomy.by_key()["transaction_risk"].section_container)
        self.assertTrue(taxonomy.by_key()["definitions"].section_container)
        self.assertTrue(
            taxonomy.by_key()["performance_commitment"].section_container
        )
        self.assertTrue(
            taxonomy.by_key()["transaction_commitments"].section_container
        )
        self.assertFalse(taxonomy.by_key()["issuance_plan"].context_container)
        self.assertFalse(taxonomy.by_key()["revenue_and_cost"].context_container)
        self.assertTrue(taxonomy.by_key()["revenue_and_cost"].quantitative_topic)
        self.assertFalse(taxonomy.by_key()["audit_opinion"].quantitative_topic)
        for key in (
            "performance_forecast_period",
            "performance_forecast_range",
            "performance_forecast_comparison",
            "performance_forecast_basis",
            "performance_forecast_risk",
        ):
            with self.subTest(role_anchor=key):
                self.assertTrue(taxonomy.by_key()[key].role_anchor)
                self.assertFalse(taxonomy.by_key()[key].exclusive_container)
        self.assertEqual(
            [
                (item.label, item.keys)
                for item in taxonomy.composite_sections
            ],
            [
                (
                    "公司治理、环境和社会",
                    ("governance", "environment_social"),
                ),
                (
                    "公司简介和主要财务指标",
                    ("company_profile", "company_profile_metrics"),
                ),
                (
                    "合并和公司资产负债表",
                    ("balance_sheet", "balance_sheet_parent"),
                ),
                (
                    "合并和银行资产负债表",
                    ("balance_sheet", "balance_sheet_parent"),
                ),
                (
                    "合并和公司利润表",
                    ("income_statement", "income_statement_parent"),
                ),
                (
                    "合并和银行利润表",
                    ("income_statement", "income_statement_parent"),
                ),
                (
                    "合并和公司现金流量表",
                    ("cash_flow_statement", "cash_flow_statement_parent"),
                ),
                (
                    "合并和银行现金流量表",
                    ("cash_flow_statement", "cash_flow_statement_parent"),
                ),
                (
                    "合并和公司所有者权益变动表",
                    ("equity_statement", "equity_statement_parent"),
                ),
                (
                    "合并和公司股东权益变动表",
                    ("equity_statement", "equity_statement_parent"),
                ),
                (
                    "合并和银行所有者权益变动表",
                    ("equity_statement", "equity_statement_parent"),
                ),
                (
                    "合并和银行股东权益变动表",
                    ("equity_statement", "equity_statement_parent"),
                ),
            ],
        )
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
        self.assertIn(
            "主要控股、参股企业分析",
            taxonomy.by_key()["subsidiaries_analysis"].labels,
        )
        self.assertIn(
            "restructuring_assets",
            taxonomy.by_key()["issue_size"].scopes,
        )
        self.assertIn(
            "performance_flash",
            taxonomy.by_key()["risk_warning"].scopes,
        )
        self.assertNotIn(
            "performance_forecast",
            taxonomy.by_key()["risk_warning"].scopes,
        )
        self.assertIn(
            "风险提示",
            taxonomy.by_key()["performance_forecast_risk"].labels,
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

    def test_combined_company_profile_heading_keeps_direct_and_section_routes(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_body(
            "第二节 公司简介和主要财务指标",
            "公司基本信息及主要会计数据如下。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail(
                "an exact direct route must not be weakened by its section alias"
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
                filing_type="semiannual_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("company_profile_metrics",))
        self.assertEqual(
            result.units[0].section_keys,
            ("company_profile", "company_profile_metrics"),
        )
        self.assertEqual(adjudicator.calls, 0)

    def test_exact_context_heading_substrings_do_not_create_direct_locks(
        self,
    ) -> None:
        cases = (
            (
                "与金融工具相关的风险",
                "本集团持有金融工具并披露相关风险及风险管理政策。",
                "financial_instruments_policy",
                "financial_instrument_risk",
            ),
            (
                "财务报表的编制基础",
                "本节披露相关具体情况。",
                "financial_statements_section",
                "basis_of_preparation",
            ),
            (
                "合并财务报表主要项目注释",
                "本节披露相关具体情况。",
                "financial_statements_section",
                "consolidated_notes",
            ),
            (
                "母公司财务报表主要项目注释",
                "本节披露相关具体情况。",
                "financial_statements_section",
                "parent_company_notes",
            ),
        )
        for title, body, forbidden_direct, required_section in cases:
            with self.subTest(title=title):
                admitted, drafts = _drafts_with_body(title, body)
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail(
                        "an exact context heading must not need model adjudication"
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

                self.assertNotIn(
                    forbidden_direct,
                    result.units[0].semantic_keys or (),
                )
                self.assertIn(
                    required_section,
                    result.units[0].section_keys or (),
                )
                self.assertEqual(adjudicator.calls, 0)

    def test_source_reviewed_context_direct_overlaps_remain_intentional(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_table(
            "（二）内部控制审计报告",
            "<table><tr><td>内控审计报告意见类型</td>"
            "<td>标准无保留意见</td></tr></table>",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("the source-reviewed overlap is deterministic")
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

        self.assertEqual(
            result.units[0].semantic_keys,
            ("internal_control", "audit_opinion"),
        )
        self.assertEqual(result.units[0].section_keys, ("internal_control",))
        self.assertEqual(adjudicator.calls, 0)

    def test_internal_control_context_without_opinion_evidence_is_its_own_topic(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_body(
            "（二）内部控制审计报告",
            "公司持续完善内部控制制度并强化监督检查。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail(
                "a context heading without opinion evidence must not call the model"
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

        self.assertEqual(result.units[0].semantic_keys, ("internal_control",))
        self.assertEqual(result.units[0].section_keys, ("internal_control",))
        self.assertEqual(adjudicator.calls, 0)

    def test_combined_restructuring_pricing_headings_keep_broad_and_narrow_routes(
        self,
    ) -> None:
        cases = (
            (
                "5、发行股份的定价依据、定价基准日和发行价格",
                "根据《重组管理办法》规定，上市公司发行股份的价格不得低于市场参考价的80%。本次发行价格为6.85元/股。",
            ),
            (
                "3、发行股份的定价基准日、定价依据和发行价格",
                "本次发行股份募集配套资金的定价基准日为发行期首日，发行价格不低于市场参考价的80%。",
            ),
        )
        for title, body in cases:
            with self.subTest(title=title):
                admitted, drafts = _drafts_with_body(title, body)
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail(
                        "exact combined pricing headings must be deterministic"
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
                    ("valuation_pricing", "issue_price"),
                )
                self.assertEqual(adjudicator.calls, 0)

    def test_directors_report_context_and_exact_periodic_topics_are_source_bound(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "第四节 董事会报告",
            "九、管理合约",
            "报告期内不存在有关重大部分业务的管理合约。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("exact periodic routes must be deterministic")
        )
        router = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        )

        context_result = router.route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
        )
        self.assertEqual(
            context_result.units[1].section_keys,
            ("directors_report",),
        )

        for title, expected in (
            ("第二节 致股东", "shareholder_letter"),
            ("四、主要控股、参股企业分析", "subsidiaries_analysis"),
        ):
            with self.subTest(title=title):
                direct_admitted, direct_drafts = _drafts_with_body(
                    title,
                    "本节按报告期事实披露相关情况。",
                )
                direct_result = router.route(
                    admitted=direct_admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type="annual_report",
                    ),
                    drafts=direct_drafts,
                )
                self.assertEqual(direct_result.units[0].semantic_keys, (expected,))
        self.assertEqual(adjudicator.calls, 0)

    def test_event_section_contexts_and_scope_extensions_are_exact(self) -> None:
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("exact event routes must be deterministic")
        )
        router = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        )
        for parent, child, expected in (
            ("第一节 释义", "（一）普通术语", "definitions"),
            ("（七）业绩补偿安排", "2、补偿数额的确定", "performance_commitment"),
            (
                "六、本次重组相关方作出的重要承诺",
                "（一）上市公司作出的重要承诺",
                "transaction_commitments",
            ),
        ):
            with self.subTest(parent=parent):
                admitted, drafts = _drafts_with_parent_heading(
                    parent,
                    child,
                    "本节披露与该标题直接相关的具体内容。",
                )
                result = router.route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type="restructuring_assets",
                    ),
                    drafts=drafts,
                )
                self.assertIn(expected, result.units[1].section_keys or ())

        for filing_type, title, expected in (
            ("performance_flash", "三、风险提示", "risk_warning"),
            ("restructuring_assets", "6、发行数量", "issue_size"),
            ("restructuring_assets", "发行价格", "issue_price"),
        ):
            with self.subTest(filing_type=filing_type, title=title):
                admitted, drafts = _drafts_with_body(
                    title,
                    "本节披露与该标题直接相关的具体内容。",
                )
                result = router.route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )
                self.assertIn(expected, result.units[0].semantic_keys or ())
        self.assertEqual(adjudicator.calls, 0)

    def test_nearby_semantic_phrases_do_not_widen_new_exact_routes(self) -> None:
        cases = (
            ("致股东事项说明", "annual_report"),
            ("董事会专项工作报告", "annual_report"),
            ("普通术语补充", "restructuring_assets"),
            ("相关方一般承诺", "restructuring_assets"),
        )
        for title, filing_type in cases:
            with self.subTest(title=title):
                admitted, drafts = _drafts_with_body(
                    title,
                    "本节为相邻但不等同于受控标题的内容。",
                )
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail("inexact labels must not call the model")
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )
                self.assertIsNone(result.units[0].semantic_keys)
                self.assertIsNone(result.units[0].section_keys)
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

    def test_heading_only_restructuring_overview_is_section_context_only(self) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "第一节 本次交易概况",
            "4、发行股份的种类、每股面值、上市地点",
            "本次发行的股份种类为人民币普通股。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("exact transaction overview must be deterministic")
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

        self.assertIsNone(result.units[0].semantic_keys)

        self.assertEqual(
            result.units[0].section_keys,
            ("restructuring_transaction_overview",),
        )
        self.assertFalse(result.receipts[0].candidate_keys)
        self.assertEqual(
            result.units[1].section_keys,
            ("restructuring_transaction_overview",),
        )
        self.assertEqual(adjudicator.calls, 0)

    def test_investor_protection_section_is_exact_and_scope_bound(self) -> None:
        for filing_type, expected in (
            ("restructuring_assets", ("investor_protection_arrangements",)),
            ("annual_report", None),
        ):
            with self.subTest(filing_type=filing_type):
                admitted, drafts = _drafts_with_parent_heading(
                    "六、本次重组对中小投资者权益保护的安排",
                    "（一）严格履行上市公司信息披露义务",
                    "上市公司将继续依法及时、准确地履行信息披露义务。",
                )
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail(
                        "exact investor protection section must not call model"
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
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )

                self.assertEqual(result.units[0].section_keys, expected)
                self.assertEqual(result.units[1].section_keys, expected)
                self.assertEqual(adjudicator.calls, 0)

    def test_event_section_requires_exact_heading_and_scope(self) -> None:
        cases = (
            ("二、本次配股的认购方法说明", "rights_issue", True, None),
            ("二、本次配股的认购方法", "annual_report", True, None),
            (
                "二、本次配股的认购方法",
                "rights_issue",
                False,
                ("subscription_arrangements",),
            ),
        )
        for parent, filing_type, has_content, expected in cases:
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

                self.assertEqual(result.units[0].section_keys, expected)

    def test_restructuring_risk_keeps_directly_named_object_topic(self) -> None:
        for title, expected in (
            (
                "（三）标的资产的评估值风险",
                ("transaction_risk", "target_asset"),
            ),
            (
                "（五）业绩承诺及其执行风险",
                ("transaction_risk", "performance_commitment"),
            ),
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

                self.assertEqual(result.units[0].semantic_keys, expected)
                self.assertEqual(adjudicator.calls, 0)

    def test_official_year_prefixed_and_risk_sections_project_without_model(
        self,
    ) -> None:
        cases = (
            (
                "第五节 公司治理报告暨企业管治报告",
                "公司年度治理工作回顾",
                ("governance",),
                None,
            ),
            (
                "第六节 公司治理",
                "2025年度薪酬情况",
                ("governance", "directors_management"),
                ("directors_management",),
            ),
            (
                "第三节 经营情况讨论与分析",
                "公司面临的风险和应对措施",
                ("business_review", "risk_management"),
                ("risk_management", "business_risk"),
            ),
            (
                "第六节 公司治理",
                "审计委员会",
                ("governance", "board_committees"),
                ("board_committees",),
            ),
        )
        for parent, title, expected, expected_semantic in cases:
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

                self.assertEqual(result.units[1].semantic_keys, expected_semantic)
                self.assertEqual(result.units[1].section_keys, expected)
                self.assertEqual(adjudicator.calls, 0)

    def test_exact_composite_heading_projects_each_section_to_anchor_and_content(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "第四节 公司治理、环境和社会",
            "一、董事会工作情况",
            "报告期内公司推进治理建设并履行环境与社会责任。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("exact composite sections must not call model")
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

        self.assertEqual(
            result.units[0].section_keys,
            ("governance", "environment_social"),
        )
        self.assertEqual(
            result.units[1].section_keys,
            ("governance", "environment_social"),
        )
        self.assertEqual(adjudicator.calls, 0)

    def test_near_composite_phrase_does_not_widen_section_context(self) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "第四节 公司治理、环境和社会补充说明",
            "一、董事会工作情况",
            "报告期内公司推进相关工作。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("inexact composite context must abstain")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
        )

        self.assertIsNone(result.units[1].section_keys)

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

        self.assertEqual(
            result.units[1].section_keys,
            ("directors_report", "investment_analysis"),
        )
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

        self.assertEqual(result.units[1].section_keys, ("directors_report",))
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
        self.assertEqual(result.units[0].section_keys, ("important_notice",))
        self.assertEqual(adjudicator.calls, 0)

    def test_exact_topic_parent_is_structural_even_without_container_flag(self) -> None:
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
                self.assertEqual(
                    result.units[1].section_keys,
                    ("business_review",) if not context_container else None,
                )
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

    def test_heading_only_unit_keeps_exact_structural_context(self) -> None:
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
        self.assertEqual(result.units[1].section_keys, ("business_review",))
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

    def test_negative_or_not_applicable_labeled_field_keeps_topic(self) -> None:
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
        for body in ("资金来源：不适用", "资金来源：未发生", "资金来源：无"):
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

                self.assertEqual(
                    result.units[0].semantic_keys,
                    ("share_buyback_funding",),
                )
                self.assertEqual(
                    result.receipts[0].decision_source,
                    "deterministic",
                )
                self.assertEqual(adjudicator.calls, 0)

    def test_incidental_negated_sentence_is_not_a_labeled_field(self) -> None:
        taxonomy = SemanticRouteTaxonomy(
            version="labeled-field-incidental.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="share_buyback_funding",
                    description="股份回购资金来源",
                    labels=("资金来源",),
                    scopes=("share_buyback",),
                ),
            ),
        )
        admitted, drafts = _drafts_with_body(
            "回购资金安排",
            "公司尚未披露资金来源：自有资金",
        )
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
        self.assertIn("source_quantitative_topic", candidate.evidence_kinds)

    def test_periodic_compound_metric_fields_lock_each_direct_topic(self) -> None:
        admitted, drafts = _drafts_with_body(
            "总体经营情况分析",
            (
                "本公司平均总资产收益率(ROAA)和平均净资产收益率(ROAE)"
                "分别为1.14%和13.48%；贷款和垫款总额74,643.73亿元，"
                "客户存款总额99,591.97亿元。"
            ),
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("controlled quantitative fields are closed")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某银行第一季度报告",
                filing_type="quarterly_report",
            ),
            drafts=drafts,
        )

        self.assertTrue(
            {
                "return_on_equity",
                "bank_loans_advances",
                "bank_customer_deposits",
            }.issubset(result.units[0].semantic_keys or ())
        )

    def test_product_type_loan_quality_heading_is_exact_but_policy_is_not(self) -> None:
        admitted, drafts = _drafts_with_body(
            "本公司按产品类型划分的贷款和垫款资产质量情况",
            "本表列示不良、关注及逾期贷款余额和比率。",
        )
        routed = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("the exact source heading is closed")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某银行第一季度报告",
                filing_type="quarterly_report",
            ),
            drafts=drafts,
        )
        self.assertIn("bank_loan_quality", routed.units[0].semantic_keys or ())

        admitted, drafts = _drafts_with_body(
            "按产品类型划分的贷款和垫款资产质量管理制度",
            "本制度规定数据填报职责。",
        )
        negative = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
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
                title="某银行第一季度报告",
                filing_type="quarterly_report",
            ),
            drafts=drafts,
        )
        self.assertNotIn("bank_loan_quality", negative.units[0].semantic_keys or ())

    def test_incentive_result_table_locks_assessment_and_cancellation(self) -> None:
        admitted, drafts = _drafts_with_body(
            "本次限制性股票归属的具体情况",
            (
                "5名激励对象考核结果为不合格，可归属数量为0股。"
                "上述未归属股份由公司予以作废。"
            ),
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("source-bound result fields are closed")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="限制性股票归属公告",
                filing_type="equity_incentive",
            ),
            drafts=drafts,
        )
        self.assertTrue(
            {
                "incentive_performance_assessment",
                "incentive_cancellation",
            }.issubset(result.units[0].semantic_keys or ())
        )

    def test_buyback_purpose_sentence_locks_purpose_not_funding(self) -> None:
        admitted, drafts = _drafts_with_body(
            "拟回购股份的用途、数量、占总股本比例和资金总额",
            "本次回购的股份拟用于注销并减少注册资本。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("the buyback object and purpose are closed")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="股份回购方案公告",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )
        self.assertIn("share_buyback_purpose", result.units[0].semantic_keys or ())

        admitted, drafts = _drafts_with_body(
            "回购资金安排",
            "本次回购资金用于公司回购专用账户。",
        )
        negative = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
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
                title="股份回购方案公告",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )
        self.assertNotIn("share_buyback_purpose", negative.units[0].semantic_keys or ())

    def test_event_labeled_field_allows_one_current_event_prefix(self) -> None:
        admitted, drafts = _drafts_with_body(
            "重要内容提示",
            "1、本次归属股票上市流通日：2026年6月2日",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("the current event field is closed")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="限制性股票归属公告",
                filing_type="equity_incentive",
            ),
            drafts=drafts,
        )
        self.assertIn("unlock_schedule", result.units[0].semantic_keys or ())

    def test_periodic_quantitative_topic_prefers_longest_controlled_field(self) -> None:
        cases = (
            (
                "经营情况",
                "本集团实现净利息收入556.42亿元。",
                "bank_net_interest_income",
                "interest_income",
            ),
            (
                "金融资产",
                "其他债权投资为100亿元。",
                "other_debt_investments",
                "debt_investments",
            ),
            (
                "金融资产",
                "其他权益工具投资为100亿元。",
                "other_equity_investments",
                "other_equity_instruments",
            ),
        )
        for title, body, required, forbidden in cases:
            with self.subTest(required=required, forbidden=forbidden):
                admitted, drafts = _drafts_with_body(title, body)
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "a longest controlled quantitative field is closed"
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type="quarterly_report",
                    ),
                    drafts=drafts,
                )

                self.assertIn(required, result.units[0].semantic_keys or ())
                self.assertNotIn(forbidden, result.units[0].semantic_keys or ())

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
        self.assertNotIn("source_quantitative_topic", candidate.evidence_kinds)

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
        self.assertNotIn("source_quantitative_topic", question.evidence_kinds)

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
                "performance_forecast_risk",
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

                self.assertEqual(result.units[0].semantic_keys, (expected,))
                self.assertEqual(
                    result.receipts[0].decision_source,
                    "deterministic",
                )
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

    def test_content_bearing_explicit_risk_title_gets_business_risk_topic(self) -> None:
        admitted, drafts = _drafts_with_body(
            "4、汇率波动风险",
            "报告期产生汇兑损失，并持续管理外币风险敞口。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("explicit risk title must be deterministic")
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

        self.assertIn("business_risk", result.units[0].semantic_keys or ())
        business_risk_evidence = next(
            item
            for item in result.receipts[0].evidence
            if item.key == "business_risk"
        )
        self.assertIn("source_heading_risk_topic", business_risk_evidence.kinds)

    def test_accounting_policy_risk_title_is_not_business_risk_topic(self) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "三、重要会计政策和会计估计",
            "(一)信用风险显著增加",
            "本集团在每个资产负债表日评估相关金融工具的信用风险。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("exact accounting route must be deterministic")
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

        routed_child = result.units[-1]
        self.assertIn("accounting_policies", routed_child.section_keys or ())
        self.assertNotIn("business_risk", routed_child.semantic_keys or ())

    def test_heading_only_risk_anchor_does_not_claim_business_risk_content(self) -> None:
        admitted, drafts = _heading_only_drafts("1.1市场风险")
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("heading-only periodic route must abstain")
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

        self.assertNotIn("business_risk", result.units[0].semantic_keys or ())

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

    def test_true_overview_body_anchor_can_add_labeled_secondary_with_model(
        self,
    ) -> None:
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

        def decide(batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
            unit = batch.units[0]
            return (
                SemanticAdjudicationDecision(
                    unit_index=unit.unit_index,
                    routes=tuple(
                        SemanticAdjudicatedRoute(
                            key=candidate.key,
                            support_ids=candidate.source_ids,
                        )
                        for candidate in unit.candidates
                    ),
                ),
            )

        adjudicator = _Adjudicator(decide)

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
        self.assertEqual(result.receipts[0].decision_source, "model")
        self.assertEqual(adjudicator.calls, 1)

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

    def test_exact_interest_term_deadline_heading_routes_terms(self) -> None:
        admitted, drafts = _drafts_with_body(
            "9、付息的期限和方式",
            "本次付息期间为2025年8月16日至2026年8月15日。",
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
            "未分配利润增加100万元，其他综合收益下降20万元，"
            "外币报表折算差额减少5万元。",
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
            "未分配利润增加100万元，其他综合收益下降20万元。",
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

    def test_periodic_typed_table_field_matches_body_topic(self) -> None:
        body_admitted, body_drafts = _drafts_with_body(
            "经营情况讨论与分析",
            "营业收入为100万元，营业成本为80万元。",
        )
        table_admitted, table_drafts = _drafts_with_table(
            "经营情况讨论与分析",
            (
                "<table><tr><td>营业收入</td><td>100万元</td></tr>"
                "<tr><td>营业成本</td><td>80万元</td></tr></table>"
            ),
        )
        routed: list[tuple[str, ...] | None] = []
        for admitted, drafts in (
            (body_admitted, body_drafts),
            (table_admitted, table_drafts),
        ):
            result = SemanticRouter(
                taxonomy=load_semantic_route_taxonomy(),
                adjudicator=_Adjudicator(
                    lambda _batch: self.fail(
                        "typed periodic fields must be deterministic"
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
            routed.append(result.units[0].semantic_keys)
            self.assertEqual(
                result.receipts[0].decision_source,
                "deterministic",
            )

        self.assertEqual(
            routed,
            [("business_review", "revenue_and_cost")] * 2,
        )

    def test_periodic_directional_fact_can_lock_without_an_invented_value(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_body_only(
            "受人民币汇率变动影响，外币财务报表折算差额减少。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("an explicit directional fact is closed")
            ),
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
            result.units[0].semantic_keys,
            ("foreign_currency_translation",),
        )

    def test_topics_keep_modality_while_forecast_roles_remain_exact(
        self,
    ) -> None:
        cases = (
            (
                "上年同期经营业绩和财务状况",
                "上年同期归属于股东的净利润为100万元。",
                "performance_forecast",
                "performance_forecast_comparison",
                "performance_forecast_range",
            ),
            (
                "本期业绩变动的主要原因",
                "子公司预计净利润为100万元。",
                "performance_forecast",
                "performance_forecast_basis",
                "performance_forecast_range",
            ),
            (
                "公司面临的风险和应对措施",
                "若营业成本上升20%，公司利润可能下降。",
                "annual_report",
                "revenue_and_cost",
                None,
            ),
            (
                "4、卓越的管理团队及人才培养机制",
                (
                    "公司营业收入变动原因说明：主要系受益于 AI 算力需求持续"
                    "爆发，推动整体营业收入增长。"
                ),
                "semiannual_report",
                "revenue_and_cost",
                None,
            ),
            (
                "5、主要原材料价格波动风险",
                (
                    "如果未来主要原材料价格持续上涨，公司将面临营业成本上升、"
                    "毛利率水平下降的风险。"
                ),
                "semiannual_report",
                "revenue_and_cost",
                None,
            ),
            (
                "经营情况讨论与分析",
                "公司持续推动营业收入增长，员工人数为37693人。",
                "annual_report",
                "revenue_and_cost",
                None,
            ),
            (
                "经营情况讨论与分析",
                "若原材料价格上涨且公司不能及时调价，营业成本上升20%。",
                "annual_report",
                "revenue_and_cost",
                None,
            ),
            (
                "经营情况讨论与分析",
                "如果未来市场持续恶化并出现大量退货，营业收入下降20%。",
                "annual_report",
                "revenue_and_cost",
                None,
            ),
            (
                "经营情况讨论与分析",
                "由于行业持续低迷及客户需求变化，营业收入下降20%。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "上年同期营业收入为100万元。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "去年营业成本为80万元。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "历史上营业收入达到100万元。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "公司预计营业收入为100万元。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "公司预计本报告期营业收入为100万元。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "公司目标营业收入达到100万元。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "根据规划，未来营业成本为80万元。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "公司拟新增固定资产100万元。",
                "annual_report",
                "fixed_assets",
            ),
            (
                "经营情况讨论与分析",
                "公司拟计提资产减值损失100万元。",
                "annual_report",
                "asset_impairment_loss",
            ),
            (
                "经营情况讨论与分析",
                "公司拟确认营业收入100万元。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "公司拟提取盈余公积100万元。",
                "annual_report",
                "surplus_reserve",
            ),
            (
                "经营情况讨论与分析",
                "存在营业成本上升20%的可能性。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "营业收入下降20%的风险不容忽视。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "营业收入下降20%左右的风险不容忽视。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "存在营业成本上升20个百分点以上的风险。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "存在营业成本上升20亿元左右的可能性。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "营业收入下降20%的风险不可忽视。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "营业成本上升20%的风险值得关注。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "营业收入下降20%的风险仍然存在。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "营业成本上升20%的风险较为突出。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "公司将营业收入100万元作为明年经营目标。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "公司将营业收入100万元作为未来规划。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "公司将营业收入100万元作为预期值。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "营业收入100万元为预测值。",
                "annual_report",
                "revenue_and_cost",
            ),
            (
                "经营情况讨论与分析",
                "营业收入100万元仅为预算。",
                "annual_report",
                "revenue_and_cost",
            ),
        )
        for case in cases:
            title, body, filing_type, expected, *forbidden_values = case
            forbidden = forbidden_values[0] if forbidden_values else None
            with self.subTest(title=title, body=body, expected=expected):
                admitted, drafts = _drafts_with_body(title, body)
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "source-bound quantitative topics must not call model"
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )

                self.assertIn(expected, result.units[0].semantic_keys or ())
                if forbidden is not None:
                    self.assertNotIn(
                        forbidden,
                        result.units[0].semantic_keys or (),
                    )

    def test_table_of_contents_is_exclusive_and_unrelated_numbers_do_not_lock(
        self,
    ) -> None:
        cases = (
            ("目录", "营业收入 112-116", "table_of_contents", "revenue_and_cost"),
            ("目录", "审计报告 1-6", "table_of_contents", "audit_opinion"),
            (
                "经营情况讨论与分析",
                "公司提及营业收入，员工人数为37693人。",
                None,
                "revenue_and_cost",
            ),
        )
        for title, body, expected, forbidden in cases:
            with self.subTest(title=title, body=body):
                admitted, drafts = _drafts_with_body(title, body)
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "mechanical negatives must not call model"
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

                if expected is not None:
                    self.assertEqual(result.units[0].semantic_keys, (expected,))
                self.assertNotIn(forbidden, result.units[0].semantic_keys or ())

    def test_quantitative_topic_survives_nearby_context_words(
        self,
    ) -> None:
        cases = (
            "上年同期为80万元，本期营业收入为100万元。",
            "根据规划推进经营，报告期营业收入为100万元。",
            "公司围绕年度目标持续经营，本期营业收入100万元。",
            "本集团已将固定资产100万元转入投资性房地产。",
            "本集团于本年度将固定资产100万元转入投资性房地产。",
            "本集团已于本期将固定资产100万元转入投资性房地产。",
            "本期采用模拟估值，固定资产100万元。",
            "预计负债方面，本期营业成本为100万元。",
            "目标公司本期营业收入100万元。",
            "股权激励计划本期管理费用100万元。",
            "本期营业收入100万元，达到年度目标。",
            "本期营业收入100万元超出预算。",
            "本期营业收入100万元的风险调整后收益率为10%。",
            "本期营业收入100万元的风险敞口已对冲。",
            "本期营业收入100万元风险可控。",
            (
                "本集团在本年度处置部分对子公司的投资，相关交易导致"
                "增加资本公积17.14亿元。"
            ),
        )
        for body in cases:
            with self.subTest(body=body):
                admitted, drafts = _drafts_with_body(
                    "经营情况讨论与分析",
                    body,
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail("the current-period fact is closed")
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

                self.assertTrue(result.units[0].semantic_keys)

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

    def test_exact_forecast_period_is_a_role_without_range_leakage(self) -> None:
        admitted, drafts = _drafts_with_body(
            "（一）业绩预告期间",
            "2026年1月1日至2026年6月30日。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("exact forecast roles are deterministic")
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
            ("performance_forecast_period",),
        )
        self.assertNotIn(
            "performance_forecast_range",
            result.units[0].semantic_keys or (),
        )
        self.assertEqual(adjudicator.calls, 0)

    def test_exact_forecast_period_keeps_independently_typed_range(self) -> None:
        admitted, drafts = _drafts_with_table(
            "（一）业绩预告期间",
            (
                "<table><tr><th>预计净利润区间</th>"
                "<td>-18亿元至-15亿元</td></tr></table>"
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("independently typed roles are deterministic")
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
            ("performance_forecast_period", "performance_forecast_range"),
        )
        range_evidence = next(
            item
            for item in result.receipts[0].evidence
            if item.key == "performance_forecast_range"
        )
        self.assertIn("source_labeled_field_exact", range_evidence.kinds)
        self.assertEqual(adjudicator.calls, 0)

    def test_event_metric_label_adjacent_to_value_is_a_direct_fact(self) -> None:
        admitted, drafts = _drafts_with_body(
            "一、2026年7月份销售情况简报",
            "商品猪销售收入88.97亿元，商品猪销售均价10.64元/公斤。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("explicit event metric values must not call model")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="operating_data",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            set(result.units[0].semantic_keys or ()),
            {"price_changes", "sales_volume"},
        )
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

    def test_flash_variance_template_heading_is_exact_and_scope_closed(self) -> None:
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("standard flash heading must not call model")
        )
        router = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        )
        admitted, drafts = _drafts_with_body(
            "（二）变动幅度达 30%以上指标的说明",
            "净利润和基本每股收益变动的主要原因如下。",
        )

        result = router.route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司2022年度业绩快报公告",
                filing_type="performance_flash",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[0].semantic_keys,
            ("performance_flash_variance",),
        )
        self.assertIn(
            "performance_flash_variance",
            result.units[0].section_keys or (),
        )
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

        for title, filing_type in (
            ("变动幅度未达30%的指标说明", "performance_flash"),
            ("变动幅度达30%以上指标的管理制度", "performance_flash"),
            ("变动幅度达30%以上指标的说明", "annual_report"),
        ):
            with self.subTest(title=title, filing_type=filing_type):
                admitted, drafts = _drafts_with_body(title, "本段为相邻概念正文。")
                adjacent = router.route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )
                self.assertNotIn(
                    "performance_flash_variance",
                    adjacent.units[0].semantic_keys or (),
                )
                self.assertNotIn(
                    "performance_flash_variance",
                    adjacent.units[0].section_keys or (),
                )
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

    def test_legacy_retryable_adjudicator_outage_propagates_without_silent_abstention(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_body(
            "业绩预告和预计业绩区间",
            "业绩变动原因",
        )

        def unavailable(_batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
            raise SemanticRouteAdjudicatorError(
                "quota unavailable",
                reason_code="rate_limited",
                retryable=True,
            )

        router = _router(_Adjudicator(unavailable))
        context = SemanticDocumentContext(
            title="某公司业绩预告",
            filing_type="performance_forecast",
        )
        with self.assertRaises(SemanticRouteAdjudicatorError) as raised:
            router.route(
                admitted=admitted,
                document=context,
                drafts=drafts,
            )

        self.assertEqual(raised.exception.reason_code, "rate_limited")

    def test_nonretryable_adjudicator_outage_still_fails_closed(self) -> None:
        admitted, drafts = _drafts_with_body(
            "业绩预告和预计业绩区间",
            "业绩变动原因",
        )

        def unavailable(_batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
            raise SemanticRouteAdjudicatorError(
                "configuration invalid",
                reason_code="invalid_configuration",
                retryable=False,
            )

        with self.assertRaises(SemanticRouteAdjudicatorError):
            _router(_Adjudicator(unavailable)).route(
                admitted=admitted,
                document=SemanticDocumentContext(
                    title="某公司业绩预告",
                    filing_type="performance_forecast",
                ),
                drafts=drafts,
            )

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
        self.assertEqual(result.units[0].section_keys, ("revenue_and_cost",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_exact_structural_parent_and_local_body_corroborate_direct_topic(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "19、固定资产",
            "19.2 折旧方法",
            "固定资产采用年限平均法计提折旧。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("source-bound corroboration is deterministic")
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

        child = result.units[1]
        self.assertEqual(child.section_keys, ("fixed_assets",))
        self.assertEqual(child.semantic_keys, ("fixed_assets",))
        self.assertIn("source_section_exact", result.receipts[1].evidence[0].kinds)
        self.assertIn("source_body_candidate", result.receipts[1].evidence[0].kinds)
        self.assertEqual(adjudicator.calls, 0)

    def test_exact_structural_parent_without_local_topic_stays_section_only(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "19、固定资产",
            "19.2 其他说明",
            "本节未披露其他事项。",
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("structure alone must not use the model")
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

        self.assertEqual(result.units[1].section_keys, ("fixed_assets",))
        self.assertIsNone(result.units[1].semantic_keys)

    def test_exact_own_context_heading_with_body_is_direct_and_structural(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_body(
            "经营情况讨论与分析",
            "报告期内公司继续推进主营业务经营。",
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail(
                    "an exact own context heading is deterministic"
                )
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("business_review",))
        self.assertEqual(result.units[0].section_keys, ("business_review",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")

    def test_exact_context_heading_with_mechanical_toc_stays_structural(self) -> None:
        admitted, drafts = _drafts_with_body(
            "第九节 财务报告",
            "内容\n页码\n审计报告 1 - 6\n合并资产负债表 7 - 8",
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("a mechanical TOC must not use the model")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_keys)
        self.assertEqual(
            result.units[0].section_keys,
            ("financial_report_chapter",),
        )

    def test_locked_route_keeps_independently_labeled_body_secondary(self) -> None:
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
            ("share_buyback_plan", "share_buyback_funding"),
        )
        self.assertIn(
            "share_buyback_funding",
            result.receipts[0].candidate_keys,
        )
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_body_reference_without_labeled_value_stays_candidate_only(self) -> None:
        admitted, drafts = _drafts_with_body(
            "回购方案",
            "相关内容详见回购资金来源章节。",
        )
        taxonomy = SemanticRouteTaxonomy(
            version="summary-secondary-reference.v1",
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

        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("a locked route must not call the model")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="回购报告书",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("share_buyback_plan",))
        self.assertIn("share_buyback_funding", result.receipts[0].candidate_keys)

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

    def test_reusable_standard_headings_lock_their_central_topic_without_model(
        self,
    ) -> None:
        cases = (
            (
                "11.2.3预期信用损失的确定",
                "本集团计量预期信用损失。",
                "annual_report",
                "credit_risk",
            ),
            (
                "-权益工具",
                "本集团发行的权益工具按合同条款分类。",
                "annual_report",
                "financial_instruments_policy",
            ),
            (
                "(b) 金融负债",
                "本集团对金融负债进行初始确认和后续计量。",
                "semiannual_report",
                "financial_instruments_policy",
            ),
            (
                "(i) 金融资产的分类",
                "本集团依据管理业务模式划分金融资产类别。",
                "semiannual_report",
                "financial_instruments_policy",
            ),
            (
                "7、控制的判断标准和合并财务报表的编制方法",
                "合并财务报表抵销集团内部交易。",
                "semiannual_report",
                "scope_of_consolidation",
            ),
            (
                "18、持有待售的非流动资产或处置组",
                "符合条件的处置组划分为持有待售类别。",
                "semiannual_report",
                "held_for_sale",
            ),
            (
                "（六）配售对象",
                "本次配股向股权登记日收市后的股东配售。",
                "rights_issue",
                "issue_allottees",
            ),
            (
                "(三) 资产、负债情况分析",
                "本节分析报告期末主要资产和负债变化。",
                "semiannual_report",
                "assets_liabilities_analysis",
            ),
            (
                "十二、募集资金使用进展说明",
                "公司披露报告期募集资金使用进展。",
                "semiannual_report",
                "fundraising_usage",
            ),
            (
                "(3) 辞退福利的会计处理方法",
                "辞退福利在满足确认条件时计入当期损益。",
                "semiannual_report",
                "employee_benefits_policy",
            ),
            (
                "(2) 报告分部的财务信息",
                "本节披露各报告分部的资产、收入和经营成果。",
                "semiannual_report",
                "segment_information",
            ),
            (
                "30.1按照业务类型披露收入确认和计量所采用的会计政策 -续",
                "本集团按履约义务确认收入。",
                "annual_report",
                "revenue_recognition_policy",
            ),
            (
                "22.1消耗性生物资产",
                "消耗性生物资产按成本计量。",
                "annual_report",
                "biological_assets",
            ),
            (
                "3、在合营企业或联营企业中的权益 -续",
                "本集团披露合营企业和联营企业的汇总财务信息。",
                "annual_report",
                "interests_in_other_entities",
            ),
            (
                "（二）确保本次交易的定价公平、公允",
                "交易作价以评估值为依据。",
                "restructuring_assets",
                "valuation_pricing",
            ),
            (
                "7、锁定期安排",
                "交易对方取得的股份自登记日起十二个月内不转让。",
                "restructuring_assets",
                "transaction_share_lockup",
            ),
            (
                "8、期间损益安排",
                "损益归属期间的盈利由上市公司享有。",
                "restructuring_assets",
                "transition_period_profit_loss",
            ),
            (
                "（2）后续安排及对本次交易的影响",
                "本节披露后续安排及其交易影响。",
                "restructuring_assets",
                "transaction_impact",
            ),
            (
                "①担保情况",
                "本节列示标的公司接受担保的情况。",
                "restructuring_assets",
                "guarantee_overview",
            ),
        )
        for title, body, filing_type, expected in cases:
            with self.subTest(title=title):
                admitted, drafts = _drafts_with_body(title, body)
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail(
                        "a reusable source heading must not need model adjudication"
                    )
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="披露文件",
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )

                self.assertIn(expected, result.units[0].semantic_keys or ())
                self.assertEqual(result.receipts[0].decision_source, "deterministic")
                self.assertEqual(adjudicator.calls, 0)

    def test_common_annual_template_headings_lock_direct_and_specific_section(
        self,
    ) -> None:
        cases = (
            ("“一、上市公司的人员独立", "corporate_independence"),
            ("三、上市公司的机构独立", "corporate_independence"),
            ("四、上市公司的业务独立", "corporate_independence"),
            ("五、上市公司的资产独立", "corporate_independence"),
            ("一、承诺事项履行情况", "annual_commitment_fulfillment"),
            (
                "（二）公司资产或项目存在盈利预测，且报告期仍处在盈利预测期间，"
                "公司就资产或项目达到原盈利预测及其原因做出说明",
                "asset_profit_forecast_fulfillment",
            ),
            (
                "二、控股股东及其关联方对上市公司的非经营性占用资金情况",
                "non_operating_funds_occupation",
            ),
            ("三、违规对外担保情况", "illegal_external_guarantees"),
            ("三、违规担保情况", "illegal_external_guarantees"),
            (
                "七、与上年度财务报告相比，合并报表范围发生变化的情况说明",
                "scope_of_consolidation",
            ),
            ("八、聘任、解聘会计师事务所情况", "audit_firm_engagement"),
            (
                "九、年度报告披露后面临暂停上市和终止上市情况",
                "delisting_risk",
            ),
            ("十、破产重整相关事项", "bankruptcy_reorganization"),
            ("十一、重大诉讼、仲裁事项", "major_litigation_arbitration"),
            ("十二、处罚及整改情况", "regulatory_penalties_remediation"),
            (
                "八、上市公司及其董事、高级管理人员、控股股东、实际控制人涉嫌违法违规、受到处罚及整改情况",
                "regulatory_penalties_remediation",
            ),
            (
                "十三、公司及其第一大股东的诚信状况",
                "controller_creditworthiness",
            ),
            (
                "九、报告期内公司及其控股股东、实际控制人诚信状况的说明",
                "controller_creditworthiness",
            ),
            (
                "十四、重大收购资产事项进展情况",
                "major_asset_acquisition_progress",
            ),
            (
                "（一）与日常经营相关的关联交易",
                "daily_related_party_transactions",
            ),
            (
                "（二）资产或股权收购、出售发生的关联交易",
                "related_party_asset_equity_transactions",
            ),
            (
                "（三）共同对外投资的关联交易",
                "related_party_joint_investments",
            ),
            ("（四）关联债权债务往来", "related_party_debt_dealings"),
            (
                "是否存在非经营性关联债权债务往来",
                "related_party_debt_dealings",
            ),
            ("其他重大关联交易", "related_party_overview"),
            ("关联担保情况", "guarantee_overview"),
            (
                "为其他单位提供债务担保形成的或有负债及其财务影响",
                "guarantee_overview",
            ),
            (
                "（五）与存在关联关系的财务公司的往来情况",
                "related_finance_company_dealings",
            ),
            (
                "（六）公司控股的财务公司与关联方的往来情况",
                "related_finance_company_dealings",
            ),
            ("1、委托理财情况", "entrusted_wealth_management"),
            ("2、委托贷款情况", "entrusted_loans"),
            ("（二）非募集资金使用情况", "non_fundraising_usage"),
            ("十六、重大合同及其履行情况", "major_contracts"),
            (
                "十九、购买、出售或赎回本公司之上市证券",
                "listed_securities_transactions",
            ),
            ("（一）报告期内证券发行情况", "securities_issuance_in_period"),
            (
                "四、股份回购在报告期的具体实施情况",
                "share_buyback_implementation",
            ),
            (
                "七、按照《联交所上市规则》关于公众持股量的说明",
                "public_float_statement",
            ),
            (
                "（三）发行人或投资者选择权条款、投资者保护条款的触发和执行情况",
                "bond_option_investor_protection",
            ),
            ("（六）报告期内信用评级结果调整情况", "bond_credit_rating_change"),
            (
                "（七）担保情况、偿债计划及其他偿债保障措施在报告期内的执行情况和变化情况及对债券投资者权益的影响",
                "bond_repayment_safeguards",
            ),
            (
                "五、报告期内合并报表范围亏损超过上年末净资产 10%",
                "bond_major_loss_trigger",
            ),
            (
                "六、报告期末除债券外的有息债务逾期情况",
                "overdue_debt",
            ),
            (
                "十三、市值管理制度和估值提升计划的制定落实情况",
                "market_value_management",
            ),
            (
                "十一、报告期内，公司及公司董事、高级管理人员受监管部门处罚等情况",
                "regulatory_penalties_remediation",
            ),
            ("十七、报告期内的内部控制制度建设及实施情况", "internal_control"),
            ("（一）内控评价报告", "internal_control"),
            ("二十一、环境信息披露情况", "environment_social"),
            ("二十二、社会责任情况", "environment_social"),
            (
                "二十三、巩固拓展脱贫攻坚成果、乡村振兴的情况",
                "environment_social",
            ),
            ("（三）控股股东和实际控制人情况", "controlling_shareholder_profile"),
            ("四、控股股东情况", "controlling_shareholder_profile"),
            ("五、实际控制人情况", "controlling_shareholder_profile"),
            ("（四）控股股东股份质押情况", "share_pledge"),
            ("十九、内部控制评价报告或内部控制审计报告", "internal_control"),
            (
                "七、是否存在被控股股东及其他关联方非经营性占用资金情况",
                "non_operating_funds_occupation",
            ),
            ("5、存货分析", "inventory"),
            ("（3）存货明细表", "inventory"),
            ("12、或有负债", "commitments_contingencies"),
            ("14.1.1 存货类别", "inventory"),
            ("14.1.2发出存货的计价方法", "inventory"),
            ("14.1.3存货的盘存制度", "inventory"),
            ("14.1.4低值易耗品和包装物的摊销方法", "inventory"),
        )
        for title, expected in cases:
            with self.subTest(title=title, expected=expected):
                admitted, drafts = _drafts_with_body(title, "□适用 √不适用")
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail(
                        "a regulated annual template heading must be deterministic"
                    )
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司年度报告",
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )

                self.assertIn(expected, result.units[0].semantic_keys or ())
                self.assertIn(expected, result.units[0].section_keys or ())
                self.assertEqual(result.receipts[0].decision_source, "deterministic")
                self.assertEqual(adjudicator.calls, 0)

    def test_common_annual_template_routes_do_not_widen_to_nearby_titles(
        self,
    ) -> None:
        cases = (
            ("关于“上市公司的人员独立”的说明", "corporate_independence"),
            ("存货管理人员培训", "inventory"),
            ("经营性资金往来情况", "non_operating_funds_occupation"),
            ("合规对外担保情况", "illegal_external_guarantees"),
            ("一般合同纠纷说明", "major_litigation_arbitration"),
            ("日常经营情况", "daily_related_party_transactions"),
            ("资产出售事项进展", "major_asset_acquisition_progress"),
            (
                "其他重大关联交易",
                "related_party_asset_equity_transactions",
            ),
            ("是否存在非经营性资金往来", "related_party_debt_dealings"),
            ("其他重大关联事项说明", "related_party_overview"),
            ("关联担保管理制度", "guarantee_overview"),
            ("一般担保风险分析", "guarantee_overview"),
            (
                "与财务公司的日常沟通情况",
                "related_finance_company_dealings",
            ),
            ("募集资金使用情况", "non_fundraising_usage"),
            ("一般合同履约情况", "major_contracts"),
            ("证券发行市场回顾", "securities_issuance_in_period"),
            ("股份回购制度建设", "share_buyback_implementation"),
            ("公众持股比例变化分析", "public_float_statement"),
            ("债券信用评级机构联系人", "bond_credit_rating_change"),
            ("偿债能力分析", "bond_repayment_safeguards"),
            ("合并报表亏损原因分析", "bond_major_loss_trigger"),
            ("逾期应收款情况", "overdue_debt"),
            ("（一）股东情况表", "controlling_shareholder_profile"),
            (
                "普通股股东总数及前十名股东持股情况",
                "controlling_shareholder_profile",
            ),
            ("主要资产被查封、扣押、冻结的情况", "share_pledge"),
            ("八、股份冻结情况", "share_pledge"),
            ("截至2025年12月31日，公司资产抵押或质押情况如下", "share_pledge"),
            ("公司资产抵押及质押情况", "share_pledge"),
            ("三、股份变动及股东情况", "controlling_shareholder_profile"),
            ("（三）控股股东和实际控制人情况", "share_changes"),
        )

        def abstain(
            batch: SemanticAdjudicationBatch,
        ) -> tuple[SemanticAdjudicationDecision, ...]:
            return tuple(
                SemanticAdjudicationDecision(unit_index=unit.unit_index, routes=())
                for unit in batch.units
            )

        for title, forbidden in cases:
            with self.subTest(title=title, forbidden=forbidden):
                admitted, drafts = _drafts_with_body(title, "本节披露相关情况。")
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(abstain),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司年度报告",
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )

                self.assertNotIn(forbidden, result.units[0].semantic_keys or ())
                self.assertNotIn(forbidden, result.units[0].section_keys or ())

    def test_reporting_period_prefixed_template_heading_matches_whole_label(
        self,
    ) -> None:
        cases = (
            (
                "二、报告期内控股股东及其他关联方非经营性占用资金情况",
                "semiannual_report",
                "non_operating_funds_occupation",
            ),
            (
                # The taxonomy only carries the bare 核心竞争力分析 label, so
                # this held-out heading matches through the deixis branch alone.
                "三、报告期内核心竞争力分析",
                "semiannual_report",
                "core_competitiveness",
            ),
        )
        for title, filing_type, expected in cases:
            with self.subTest(title=title, expected=expected):
                admitted, drafts = _drafts_with_body(title, "□适用 √不适用")
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail(
                        "a period-prefixed regulated heading must be deterministic"
                    )
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司半年度报告",
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )

                self.assertIn(expected, result.units[0].semantic_keys or ())
                self.assertIn(expected, result.units[0].section_keys or ())
                self.assertEqual(result.receipts[0].decision_source, "deterministic")
                self.assertEqual(adjudicator.calls, 0)

    def test_cumulative_pledge_disclosure_heading_locks_direct_topic(self) -> None:
        # The 80%-threshold pledge disclosure is a template heading whose only
        # controlled witness is the 累计质押 alias: a contains lock yields the
        # direct topic while the non-exact heading legitimately stays out of
        # section_keys.
        admitted, drafts = _drafts_with_body(
            "（四）公司第一大股东累计质押股份数量占其所持公司股份数量比例达到 80%",
            "□适用 √不适用",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("a unique pledge heading must be deterministic")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertIn("share_pledge", result.units[0].semantic_keys or ())
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_checkbox_applicability_suffix_is_normalized_away(self) -> None:
        # Template headings sometimes arrive with the applicability checkboxes
        # glued onto the heading text (observed on the held-out semiannual
        # environment chapter).  Only checkbox-marked 适用/不适用 tokens are
        # source noise; a bare statement keeps its words.
        glued = (
            "四、纳入环境信息依法披露企业名单的上市公司及其主要子公司的"
            "环境信息情况√适用 □不适用"
        )
        clean = "纳入环境信息依法披露企业名单的上市公司及其主要子公司的环境信息情况"
        self.assertEqual(_normalize_title(glued), _normalize_title(clean))
        self.assertEqual(
            _normalize_title("五、环境保护√适用"),
            _normalize_title("环境保护"),
        )
        for glyph in "√□☑☒":
            with self.subTest(glyph=glyph):
                self.assertEqual(
                    _normalize_title(f"重大合同{glyph}不适用"),
                    _normalize_title("重大合同"),
                )
        # A semantic core ending in 适用 loses only the checkbox token.
        self.assertEqual(_normalize_title("适用范围□适用"), "适用范围")
        self.assertEqual(_normalize_title("本节不适用"), "本节不适用")

    def test_reporting_period_prefix_never_splits_an_unrelated_word(self) -> None:
        def abstain(
            batch: SemanticAdjudicationBatch,
        ) -> tuple[SemanticAdjudicationDecision, ...]:
            return tuple(
                SemanticAdjudicationDecision(unit_index=unit.unit_index, routes=())
                for unit in batch.units
            )

        cases = (
            # 报告期内+部控制… must not be misread as the 内部控制 heading; the
            # remainder after the deixis is not a closed label by itself.
            ("报告期内部控制评价情况", "internal_control"),
            ("报告期内部控制审计报告执行情况", "internal_control"),
        )
        for title, forbidden in cases:
            with self.subTest(title=title, forbidden=forbidden):
                admitted, drafts = _drafts_with_body(title, "本节披露相关情况。")
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(abstain),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司年度报告",
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )

                self.assertNotIn(forbidden, result.units[0].semantic_keys or ())
                self.assertNotIn(forbidden, result.units[0].section_keys or ())

    def test_reporting_period_prefixed_heading_stays_scope_bound(self) -> None:
        admitted, drafts = _drafts_with_body(
            "二、报告期内控股股东及其他关联方非经营性占用资金情况",
            "□适用 √不适用",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail(
                "an out-of-scope heading must not reach the adjudicator"
            )
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司第一季度报告",
                filing_type="quarterly_report",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_keys)
        self.assertNotIn(
            "non_operating_funds_occupation",
            result.units[0].section_keys or (),
        )
        self.assertEqual(adjudicator.calls, 0)

    def test_reporting_period_prefix_collision_never_locks_either_key(self) -> None:
        taxonomy = SemanticRouteTaxonomy(
            version="semantic-test-taxonomy.deixis.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="bare_topic",
                    description="经营专项说明",
                    labels=("经营专项说明",),
                    scopes=("annual_report",),
                ),
                SemanticRouteDefinition(
                    key="prefixed_topic",
                    description="报告期内经营专项说明",
                    labels=("报告期内经营专项说明",),
                    scopes=("annual_report",),
                ),
            ),
        )
        admitted, drafts = _drafts_with_body(
            "报告期内经营专项说明",
            "本节披露相关情况。",
        )

        def abstain(
            batch: SemanticAdjudicationBatch,
        ) -> tuple[SemanticAdjudicationDecision, ...]:
            return tuple(
                SemanticAdjudicationDecision(unit_index=unit.unit_index, routes=())
                for unit in batch.units
            )

        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=_Adjudicator(abstain),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        # Both keys witness the same exact title, so neither may be uniquely
        # locked into a deterministic route; the ambiguity stays with the
        # bounded adjudicator, and structural section labels abstain too.
        self.assertNotEqual(result.receipts[0].decision_source, "deterministic")
        self.assertIsNone(result.units[0].semantic_keys)
        self.assertIsNone(result.units[0].section_keys)

    def test_heading_only_unit_has_no_direct_candidates_or_model_call(self) -> None:
        admitted, drafts = _heading_only_drafts(
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
        self.assertEqual(result.receipts[0].decision_source, "fallback")
        self.assertFalse(result.receipts[0].candidate_keys)
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

    def test_periodic_closed_heading_body_conflict_can_use_bounded_model(self) -> None:
        admitted, drafts = _drafts_with_body(
            "收入和成本专题说明",
            "报告期内营业利润发生重大变化。",
        )
        taxonomy = SemanticRouteTaxonomy(
            version="periodic-bounded-adjudication.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="revenue_topic",
                    description="营业收入主题",
                    labels=("收入和成本专题说明",),
                    scopes=("annual_report",),
                    overview_container=True,
                ),
                SemanticRouteDefinition(
                    key="cost_topic",
                    description="营业成本主题",
                    labels=("收入和成本专题说明",),
                    scopes=("annual_report",),
                    overview_container=True,
                ),
                SemanticRouteDefinition(
                    key="profit_topic",
                    description="营业利润主题",
                    labels=("营业利润",),
                    scopes=("annual_report",),
                ),
            ),
        )

        def decide(batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
            unit = batch.units[0]
            profit = next(item for item in unit.candidates if item.key == "profit_topic")
            return (
                SemanticAdjudicationDecision(
                    unit_index=unit.unit_index,
                    routes=(
                        SemanticAdjudicatedRoute(
                            key="profit_topic",
                            support_ids=profit.source_ids,
                        ),
                    ),
                ),
            )

        adjudicator = _Adjudicator(decide)
        result = SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("profit_topic",))
        self.assertEqual(result.receipts[0].decision_source, "model")
        self.assertEqual(adjudicator.calls, 1)

    def test_multiple_body_phrase_candidates_use_bounded_model(self) -> None:
        admitted, drafts = _drafts_with_body(
            "附件",
            "业绩预告 预计业绩区间",
        )

        def decide(batch: SemanticAdjudicationBatch):  # type: ignore[no-untyped-def]
            unit = batch.units[0]
            selected = tuple(
                SemanticAdjudicatedRoute(
                    key=candidate.key,
                    support_ids=candidate.source_ids,
                )
                for candidate in unit.candidates
                if candidate.key
                in {
                    "performance_forecast_summary",
                    "performance_forecast_range",
                }
            )
            return (
                SemanticAdjudicationDecision(
                    unit_index=unit.unit_index,
                    routes=selected,
                ),
            )

        adjudicator = _Adjudicator(decide)

        result = _router(adjudicator).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司业绩预告",
                filing_type="performance_forecast",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            result.units[0].semantic_keys,
            ("performance_forecast_range", "performance_forecast_summary"),
        )
        self.assertEqual(result.receipts[0].decision_source, "model")
        self.assertEqual(adjudicator.calls, 1)

    def test_v2_receipt_replay_is_independent_of_current_batch_size(self) -> None:
        admitted, drafts = _two_model_drafts()
        self.assertEqual(len(drafts), 2)
        context = SemanticDocumentContext(
            title="某公司业绩预告",
            filing_type="performance_forecast",
        )
        executor = _Executor(_select_forecast_summary)
        routed = SemanticRouter(
            taxonomy=_taxonomy(),
            executor=executor,
            batch_size=2,
        ).route(admitted=admitted, document=context, drafts=drafts)

        self.assertEqual(executor.calls, 1)
        self.assertEqual(
            routed.receipts[0].adjudication,
            routed.receipts[1].adjudication,
        )
        for replay_batch_size in (1, 5):
            replay_executor = _Executor(
                lambda _batch: self.fail("replay must not invoke a provider")
            )
            replayed = SemanticRouter(
                taxonomy=_taxonomy(),
                executor=replay_executor,
                batch_size=replay_batch_size,
            ).replay(
                admitted=admitted,
                document=context,
                drafts=drafts,
                receipts=routed.receipts,
            )
            self.assertEqual(replayed.units, routed.units)
            self.assertEqual(replay_executor.calls, 0)

    def test_v2_receipt_replay_rejects_tampered_group_membership(self) -> None:
        admitted, drafts = _two_model_drafts()
        context = SemanticDocumentContext(
            title="某公司业绩预告",
            filing_type="performance_forecast",
        )
        router = SemanticRouter(
            taxonomy=_taxonomy(),
            executor=_Executor(_select_forecast_summary),
            batch_size=1,
        )
        routed = router.route(admitted=admitted, document=context, drafts=drafts)
        first_adjudication = routed.receipts[0].adjudication
        self.assertIsNotNone(first_adjudication)
        tampered = (
            routed.receipts[0],
            replace(routed.receipts[1], adjudication=first_adjudication),
        )

        with self.assertRaisesRegex(
            SemanticRouteContractError,
            "group membership or order drifted",
        ):
            router.replay(
                admitted=admitted,
                document=context,
                drafts=drafts,
                receipts=tampered,
            )

    def test_v2_receipt_replay_rejects_member_lineage_drift(self) -> None:
        admitted, drafts = _two_model_drafts()
        context = SemanticDocumentContext(
            title="某公司业绩预告",
            filing_type="performance_forecast",
        )
        router = SemanticRouter(
            taxonomy=_taxonomy(),
            executor=_Executor(_select_forecast_summary),
            batch_size=2,
        )
        routed = router.route(admitted=admitted, document=context, drafts=drafts)
        second_adjudication = routed.receipts[1].adjudication
        self.assertIsNotNone(second_adjudication)
        assert second_adjudication is not None
        changed_hash = "sha256:" + "4" * 64
        changed_attempt = replace(
            second_adjudication.attempts[0],
            response_sha256=changed_hash,
        )
        changed_adjudication = replace(
            second_adjudication,
            attempts=(changed_attempt,),
            group_response_sha256=changed_hash,
        )
        tampered = (
            routed.receipts[0],
            replace(routed.receipts[1], adjudication=changed_adjudication),
        )

        with self.assertRaisesRegex(
            SemanticRouteContractError,
            "group lineage differs",
        ):
            router.replay(
                admitted=admitted,
                document=context,
                drafts=drafts,
                receipts=tampered,
            )

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

    def test_publish_replay_rejects_structurally_readable_historical_router(self) -> None:
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
        historical = tuple(
            replace(receipt, router_version="semantic_router.v98")
            for receipt in routed.receipts
        )

        with self.assertRaisesRegex(SemanticRouteContractError, "router is stale"):
            router.replay(
                admitted=admitted,
                document=context,
                drafts=drafts,
                receipts=historical,
            )

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


    def test_truthfulness_boilerplate_cannot_make_a_title_direct(self) -> None:
        guarantees = (
            (
                "本公司及董事会全体成员保证信息披露内容的真实、准确、完整，"
                "没有虚假记载、误导性陈述或重大遗漏。"
            ),
            (
                "本公司董事会及全体董事保证本公告内容不存在任何虚假记载、"
                "误导性陈述或者重大遗漏，并对其内容的真实性、准确性和完整性"
                "依法承担法律责任。"
            ),
        )
        for guarantee in guarantees:
            with self.subTest(guarantee=guarantee):
                admitted, drafts = _drafts_with_body(
                    "关于以集中竞价交易方式回购A股股份的回购报告书",
                    guarantee,
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "truthfulness boilerplate must not route"
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="关于以集中竞价交易方式回购A股股份的回购报告书",
                        filing_type="share_buyback",
                    ),
                    drafts=drafts,
                )

                self.assertIsNone(result.units[0].semantic_keys)
                self.assertFalse(result.receipts[0].candidate_keys)
                self.assertEqual(result.receipts[0].decision_source, "fallback")

        admitted, drafts = _drafts_with_body(
            "2026年7月份销售简报",
            guarantees[0] + "本月销售商品猪666.1万头，销售均价为10.64元/公斤。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("substantive title carrier is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="2026年7月份销售简报",
                filing_type="operating_data",
            ),
            drafts=drafts,
        )
        self.assertIn("operating_metrics", result.units[0].semantic_keys or ())

        admitted, drafts = _drafts_with_table(
            "2026年7月份销售简报",
            f"<table><tr><td>{guarantees[0]}</td></tr></table>",
        )
        table_only = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("table boilerplate must not route")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="2026年7月份销售简报",
                filing_type="operating_data",
            ),
            drafts=drafts,
        )
        self.assertIsNone(table_only.units[0].semantic_keys)

        admitted, drafts = _drafts_with_table(
            "2026年7月份销售简报",
            (
                f"<table><tr><td>{guarantees[0]}</td></tr>"
                "<tr><td>本月销售商品猪666.1万头</td></tr></table>"
            ),
        )
        substantive_table = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("substantive table is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="2026年7月份销售简报",
                filing_type="operating_data",
            ),
            drafts=drafts,
        )
        self.assertIn("operating_metrics", substantive_table.units[0].semantic_keys or ())

        admitted, drafts = _drafts_with_table(
            "关于以集中竞价交易方式回购A股股份的回购报告书",
            (
                "<table><tr><td>证券代码</td><td>600028</td></tr>"
                "<tr><td>证券简称</td><td>中国石化</td></tr>"
                f"<tr><td colspan='2'>{guarantees[1]}</td></tr></table>"
            ),
        )
        metadata_table = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("metadata plus boilerplate must not route")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )
        self.assertIsNone(metadata_table.units[0].semantic_keys)

        admitted, drafts = _drafts_with_table(
            "关于以集中竞价交易方式回购A股股份的回购报告书",
            (
                "<table><tr><td>回购金额</td><td>1,000万元</td></tr>"
                f"<tr><td colspan='2'>{guarantees[1]}</td></tr></table>"
            ),
        )
        factual_table = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("factual cover field is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )
        self.assertIn("share_buyback_plan", factual_table.units[0].semantic_keys or ())

    def test_page_carryover_metadata_cannot_make_a_continuation_direct(self) -> None:
        cases = (
            (
                "审计报告 - 续",
                ("德师报(审)字(26)第 P05529 号", "(第 2 页，共 6 页)"),
                "audit_opinion",
            ),
            (
                "四、关键审计事项 -续",
                ("(第 4 页，共 6 页)",),
                "key_audit_matters",
            ),
        )
        for title, bodies, forbidden in cases:
            with self.subTest(title=title):
                admitted, drafts = _drafts_with_bodies(title, *bodies)
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "page carryover metadata must not route"
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司年度报告",
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )
                self.assertNotIn(forbidden, result.units[0].semantic_keys or ())
                self.assertFalse(result.receipts[0].candidate_keys)

        admitted, drafts = _drafts_with_bodies(
            "审计报告 - 续",
            "德师报(审)字(26)第 P05529 号",
            "我们认为财务报表在所有重大方面公允反映了财务状况。",
        )
        substantive = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("substantive audit text is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )
        self.assertIn("audit_opinion", substantive.units[0].semantic_keys or ())

    def test_interest_bearing_debt_structure_table_is_deterministic(self) -> None:
        admitted, drafts = _drafts_with_table(
            "（2）有息负债及结构",
            (
                "<table><tr><td>融资途径</td><td>融资余额</td><td>期限结构</td></tr>"
                "<tr><td>银行贷款</td><td>25,778.74</td>"
                "<td>短期借款、一年内到期的非流动负债、长期借款</td></tr>"
                "<tr><td>债券</td><td>2,936.02</td><td>应付债券</td></tr>"
                "<tr><td>其他借款</td><td>7,133.56</td>"
                "<td>其他应付款、其他非流动负债</td></tr></table>"
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("structured debt facts must not call model")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(
            set(result.units[0].semantic_keys or ()),
            {
                "bonds_payable",
                "long_term_borrowings",
                "noncurrent_due_within_one_year",
                "other_noncurrent_liabilities",
                "other_payables",
                "short_term_borrowings",
            },
        )
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

        admitted, drafts = _drafts_with_table(
            "（2）有息负债及结构",
            "<table><tr><td>融资途径</td><td>融资余额</td></tr>"
            "<tr><td>债券</td><td>详见应付债券附注第2页</td></tr></table>",
        )
        no_structure_header = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
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
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )
        self.assertNotEqual(
            no_structure_header.receipts[0].decision_source,
            "deterministic",
        )

        admitted, drafts = _drafts_with_table(
            "（2）有息负债及结构",
            "<table><tr><td>长期借款管理制度</td><td>第2号</td></tr>"
            "<tr><td>是否涉及短期借款政策</td><td>1</td></tr></table>",
        )
        policy_table = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda batch: tuple(
                    SemanticAdjudicationDecision(unit_index=unit.unit_index, routes=())
                    for unit in batch.units
                )
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )
        self.assertNotEqual(policy_table.receipts[0].decision_source, "deterministic")

    def test_governance_duties_do_not_become_current_plans(self) -> None:
        admitted, drafts = _drafts_with_body(
            "（二）董事会及管理层的职责",
            "董事会负责决定经营计划和投资方案，制订利润分配方案。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("governance duties must remain lexical")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_keys)
        self.assertEqual(result.receipts[0].decision_source, "fallback")
        self.assertEqual(adjudicator.calls, 0)

        admitted, drafts = _drafts_with_body(
            "（二）董事会及管理层的职责",
            "董事会负责制订利润分配方案；公司已审议通过本年度利润分配方案。",
        )
        mixed = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
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
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )
        self.assertIn("profit_distribution_plan", mixed.receipts[0].candidate_keys)

        admitted, drafts = _drafts_with_body(
            "（二）董事会及管理层的职责",
            "董事会负责制订利润分配方案，并已审议通过本年度利润分配方案。",
        )
        comma_mixed = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda batch: tuple(
                    SemanticAdjudicationDecision(unit_index=unit.unit_index, routes=())
                    for unit in batch.units
                )
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )
        self.assertIn(
            "profit_distribution_plan",
            comma_mixed.receipts[0].candidate_keys,
        )

        admitted, drafts = _drafts_with_body(
            "（二）董事会及管理层的职责",
            "董事会负责制订利润分配方案且公司已制定本年度利润分配方案。",
        )
        conjunction_mixed = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda batch: tuple(
                    SemanticAdjudicationDecision(unit_index=unit.unit_index, routes=())
                    for unit in batch.units
                )
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )
        self.assertIn(
            "profit_distribution_plan",
            conjunction_mixed.receipts[0].candidate_keys,
        )

    def test_accounting_definition_and_treatment_are_direct_topics(self) -> None:
        cases = (
            (
                "33、所得税",
                "所得税费用包括当期所得税和递延所得税。",
                {"deferred_tax", "income_tax_expense"},
            ),
            (
                "34.1.5 租赁变更",
                "本集团重新计量租赁负债，并相应调减使用权资产的账面价值。",
                {"lease_liabilities", "right_of_use_assets"},
            ),
        )
        for title, body, expected in cases:
            with self.subTest(title=title):
                admitted, drafts = _drafts_with_parent_heading(
                    "(三)重要会计政策和会计估计",
                    title,
                    body,
                )
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail(
                        "accounting relation facts must not call model"
                    )
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司年度报告",
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )

                self.assertEqual(set(result.units[1].semantic_keys or ()), expected)
                self.assertEqual(
                    result.receipts[1].decision_source,
                    "deterministic",
                )
                self.assertEqual(adjudicator.calls, 0)

        admitted, drafts = _drafts_with_parent_heading(
            "(三)重要会计政策和会计估计",
            "24、长期资产减值",
            "本集团检查采用成本模式计量的投资性房地产、固定资产、在建工程、使用权资产是否减值。",
        )
        generic_list = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("exact impairment heading is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )
        self.assertEqual(generic_list.units[1].semantic_keys, ("asset_impairment_loss",))

        admitted, drafts = _drafts_with_parent_heading(
            "(三)重要会计政策和会计估计",
            "24、长期资产减值",
            (
                "本集团在每一个资产负债表日检查长期股权投资、采用成本模式计量的"
                "投资性房地产、固定资产、在建工程、使用权资产、采用成本模式计量的"
                "生产性生物资产是否存在可能发生减值的迹象。"
            ),
        )
        long_asset_list = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("exact impairment heading is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )
        self.assertEqual(
            long_asset_list.units[1].semantic_keys,
            ("asset_impairment_loss",),
        )

        for connector in (
            "和",
            "及",
            "与",
            "以及",
            "或",
            "/",
            "／",
            "并",
            "同",
        ):
            with self.subTest(asset_list_connector=connector):
                admitted, drafts = _drafts_with_parent_heading(
                    "(三)重要会计政策和会计估计",
                    "24、长期资产减值",
                    (
                        "本集团检查固定资产、使用权资产"
                        f"{connector}采用成本模式计量的生产性生物资产是否减值。"
                    ),
                )
                list_variant = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "exact impairment heading is deterministic"
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司年度报告",
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )
                self.assertEqual(
                    list_variant.units[1].semantic_keys,
                    ("asset_impairment_loss",),
                )

        for body in (
            "调整租赁负债和使用权资产。",
            "同时调整租赁负债及使用权资产。",
            "对租赁负债与使用权资产进行相应调整。",
            "租赁负债和使用权资产均相应调整。",
            "分别调减使用权资产和租赁负债。",
            "重新计量租赁负债并相应调减使用权资产。",
            "无需调整租赁负债和使用权资产。",
        ):
            with self.subTest(collective_lease_action=body):
                admitted, drafts = _drafts_with_parent_heading(
                    "(三)重要会计政策和会计估计",
                    "其他说明",
                    body,
                )
                collective = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "controlled collective lease relation is deterministic"
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司年度报告",
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )
                self.assertEqual(
                    set(collective.units[1].semantic_keys or ()),
                    {"lease_liabilities", "right_of_use_assets"},
                )

        admitted, drafts = _drafts_with_parent_heading(
            "(三)重要会计政策和会计估计",
            "其他说明",
            "对使用权资产及其累计折旧相应调整。",
        )
        same_object_extension = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("same-object extension is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )
        self.assertEqual(
            same_object_extension.units[1].semantic_keys,
            ("right_of_use_assets",),
        )

        for body in (
            "调整不属于租赁负债和使用权资产的固定资产。",
            "调整固定资产而非租赁负债和使用权资产。",
            "除租赁负债和使用权资产外固定资产进行调整。",
            "租赁负债和使用权资产以外的固定资产进行调整。",
            "租赁负债和使用权资产用于说明固定资产调整。",
            "该事项不属于使用权资产的调整。",
            "该事项并非使用权资产的调整。",
            "该事项不是使用权资产的调整。",
            "该事项是与使用权资产无关的调整。",
            "调整范围不包括使用权资产。",
            "与使用权资产相比固定资产调整幅度更高。",
            "使用权资产高于调整后的固定资产。",
            "使用权资产的定义不包括调整后的固定资产。",
            "租赁负债和使用权资产的定义不包括调整后的固定资产。",
        ):
            with self.subTest(collective_lease_negative=body):
                admitted, drafts = _drafts_with_parent_heading(
                    "(三)重要会计政策和会计估计",
                    "其他说明",
                    body,
                )
                negative = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
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
                        title="某公司年度报告",
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )
                self.assertNotEqual(
                    negative.receipts[1].decision_source,
                    "deterministic",
                )

        for body in (
            "本集团未确认使用权资产。",
            "使用权资产无需调整。",
        ):
            with self.subTest(negative_accounting_fact=body):
                admitted, drafts = _drafts_with_parent_heading(
                    "(三)重要会计政策和会计估计",
                    "其他说明",
                    body,
                )
                negative_fact = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "negative accounting fact is still a direct topic"
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司年度报告",
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )
                self.assertEqual(
                    negative_fact.units[1].semantic_keys,
                    ("right_of_use_assets",),
                )

        for body in (
            "调整事项详见租赁负债会计政策。",
            "所得税费用包括项目详见递延所得税附注。",
            "所得税费用由递延所得税附注解释。",
            "调整事项请见附注，租赁负债和使用权资产列示于附注。",
            "租赁负债的调整见本财务报表附注三。",
            "使用权资产计量情况见第十节。",
            "租赁负债的计量列示于本财务报表附注三。",
            "使用权资产计量情况见上述附注。",
        ):
            with self.subTest(cross_reference=body):
                admitted, drafts = _drafts_with_parent_heading(
                    "(三)重要会计政策和会计估计",
                    "其他说明",
                    body,
                )
                cross_reference = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
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
                        title="某公司年度报告",
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )
                self.assertNotEqual(
                    cross_reference.receipts[1].decision_source,
                    "deterministic",
                )

        for body, expected in (
            (
                "所得税费用主要包括当期所得税和递延所得税。",
                {"deferred_tax", "income_tax_expense"},
            ),
            (
                "所得税费用由当期所得税和递延所得税组成。",
                {"deferred_tax", "income_tax_expense"},
            ),
            (
                "所得税费用主要由当期所得税和递延所得税构成。",
                {"deferred_tax", "income_tax_expense"},
            ),
            (
                "当期所得税和递延所得税共同构成所得税费用。",
                {"deferred_tax", "income_tax_expense"},
            ),
            (
                "所得税费用包括当期所得税，同时包括递延所得税。",
                {"deferred_tax", "income_tax_expense"},
            ),
            (
                "所得税费用包括当期所得税，并且包括递延所得税。",
                {"deferred_tax", "income_tax_expense"},
            ),
            (
                "租赁负债按变更后付款额重新计量，并相应调整使用权资产。",
                {"lease_liabilities", "right_of_use_assets"},
            ),
            (
                "使用权资产账面价值相应调减，租赁负债相应减少。",
                {"lease_liabilities", "right_of_use_assets"},
            ),
            (
                "本集团重新计量租赁负债，相关金额列示于本财务报表附注三。",
                {"lease_liabilities"},
            ),
            (
                "所得税费用包括当期所得税和递延所得税，相关金额列示于本财务报表附注三。",
                {"deferred_tax", "income_tax_expense"},
            ),
        ):
            with self.subTest(accounting_variant=body):
                admitted, drafts = _drafts_with_parent_heading(
                    "(三)重要会计政策和会计估计",
                    "其他说明",
                    body,
                )
                relation = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "direct accounting treatment must not call model"
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司年度报告",
                        filing_type="annual_report",
                    ),
                    drafts=drafts,
                )
                self.assertEqual(set(relation.units[1].semantic_keys or ()), expected)

    def test_key_audit_selection_procedure_is_direct_but_report_reference_is_not(self) -> None:
        admitted, drafts = _drafts_with_body(
            "七、注册会计师对财务报表审计的责任 -续",
            "我们确定哪些事项最为重要，因而构成关键审计事项。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("KAM procedure must not call model")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司年度报告",
                filing_type="annual_report",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("key_audit_matters",))
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

    def test_buyback_result_notice_window_is_not_a_result_or_capital_change(self) -> None:
        admitted, drafts = _drafts_with_body(
            "相关主体在回购期间的增减持计划",
            "自首次披露回购事项之日起至披露回购结果暨股份变动公告期间不得减持。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("notice window must remain lexical")
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

        self.assertNotIn("share_buyback_completion", result.units[0].semantic_keys or ())
        self.assertNotIn("share_capital_change", result.units[0].semantic_keys or ())
        self.assertEqual(result.receipts[0].decision_source, "rule_abstain")
        self.assertEqual(adjudicator.calls, 0)

        admitted, drafts = _drafts_with_body(
            "相关主体在回购期间的增减持计划",
            "自首次披露回购事项之日起至披露回购结果和股份变动公告期间不得减持。",
        )
        conjunction_variant_adjudicator = _Adjudicator(
            lambda _batch: self.fail("normalized notice-window synonym is lexical")
        )
        conjunction_variant = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=conjunction_variant_adjudicator,
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
            conjunction_variant.receipts[0].decision_source,
            "rule_abstain",
        )
        self.assertEqual(conjunction_variant_adjudicator.calls, 0)

        admitted, drafts = _drafts_with_body(
            "相关主体在回购期间的增减持计划",
            (
                "自首次披露回购事项之日起至披露回购结果暨股份变动公告期间不得减持；"
                "本次回购已完成并导致股份变动。"
            ),
        )
        mixed = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
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
                title="关于回购股份方案的公告",
                filing_type="share_buyback",
            ),
            drafts=drafts,
        )
        self.assertIn("share_buyback_completion", mixed.receipts[0].candidate_keys)
        self.assertIn("share_capital_change", mixed.receipts[0].candidate_keys)

        admitted, drafts = _drafts_with_body(
            "相关主体在回购期间的增减持计划",
            (
                "自首次披露回购事项之日起至披露回购结果暨股份变动公告期间不得减持，"
                "本次回购已完成并导致股份变动。"
            ),
        )
        comma_mixed = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda batch: tuple(
                    SemanticAdjudicationDecision(unit_index=unit.unit_index, routes=())
                    for unit in batch.units
                )
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
        self.assertIn(
            "share_buyback_completion",
            comma_mixed.receipts[0].candidate_keys,
        )
        self.assertIn("share_capital_change", comma_mixed.receipts[0].candidate_keys)

        admitted, drafts = _drafts_with_body(
            "相关主体在回购期间的增减持计划",
            (
                "自首次披露回购事项之日起至披露回购结果暨股份变动公告期间不得减持且"
                "本次回购已完成并导致股份变动。"
            ),
        )
        conjunction_mixed = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda batch: tuple(
                    SemanticAdjudicationDecision(unit_index=unit.unit_index, routes=())
                    for unit in batch.units
                )
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
        self.assertIn(
            "share_capital_change",
            conjunction_mixed.receipts[0].candidate_keys,
        )

    def test_correction_title_keeps_overview_and_underlying_shareholder_event(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_body(
            "赛力斯集团股份有限公司关于公司董事、高级管理人员及骨干团队增持A股及H股股份结果的更正公告",
            "更正后：增持股数为1,000万股。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("anchored correction title is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="关于股份增持结果的更正公告",
                filing_type="correction_supplement",
            ),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_key, "correction_overview")
        self.assertIn("shareholder_change", result.units[0].semantic_keys or ())
        self.assertIn("corrected_data", result.units[0].semantic_keys or ())

        admitted, drafts = _drafts_with_body(
            "某公司关于产品库存增加结果的更正公告",
            "原公告中的产品库存有误，更正后为100件。",
        )
        negative = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("anchored non-shareholder correction is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="correction_supplement",
            ),
            drafts=drafts,
        )
        self.assertNotIn("shareholder_change", negative.units[0].semantic_keys or ())

        admitted, drafts = _drafts_with_body(
            "某公司关于股东增持可转换公司债券结果的更正公告",
            "更正后：股东增持可转换公司债券1,000张。",
        )
        bond_negative = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
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
                filing_type="correction_supplement",
            ),
            drafts=drafts,
        )
        self.assertNotIn(
            "shareholder_change",
            bond_negative.units[0].semantic_keys or (),
        )

    def test_new_exact_route_labels_are_scope_bound(self) -> None:
        cases = (
            ("7、转股起止日期", "convertible_bond", "conversion_terms"),
            ("二、回购方案的主要内容", "share_buyback", "share_buyback_plan"),
            ("4、ESG", "annual_report", "environment_social"),
            (
                "2、房地产开发项目存货的减值",
                "annual_report",
                "inventory",
            ),
            (
                "(iv) 存货可变现净值",
                "semiannual_report",
                "inventory",
            ),
            (
                "(1)未决诉讼仲裁形成的或有负债及其财务影响",
                "annual_report",
                "major_litigation_arbitration",
            ),
        )
        for title, filing_type, expected in cases:
            with self.subTest(title=title, expected=expected):
                admitted, drafts = _drafts_with_body(title, "本节披露相关具体事实。")
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail("exact title must be deterministic")
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )
                self.assertIn(expected, result.units[0].semantic_keys or ())
                self.assertIn(expected, result.units[0].section_keys or ())

        for title, filing_type, forbidden in (
            ("转股起止日期管理制度", "convertible_bond", "conversion_terms"),
            ("回购方案实施进展", "share_buyback", "share_buyback_plan"),
            ("ESG培训安排", "annual_report", "environment_social"),
            ("房地产开发项目进度", "annual_report", "inventory"),
            ("存货可变现净值管理制度", "semiannual_report", "inventory"),
            ("一般未决诉讼事项", "annual_report", "major_litigation_arbitration"),
        ):
            with self.subTest(title=title, forbidden=forbidden):
                admitted, drafts = _drafts_with_body(title, "本节披露相邻事项。")
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
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
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )
                self.assertNotIn(forbidden, result.units[0].semantic_keys or ())
                self.assertNotIn(forbidden, result.units[0].section_keys or ())

        admitted, drafts = _drafts_with_body(
            "一、可转换公司债券基本概况",
            "7、转股起止日期：2022年2月21日起至2027年8月15日止。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("typed conversion field is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="convertible_bond",
            ),
            drafts=drafts,
        )
        self.assertIn("conversion_terms", result.units[0].semantic_keys or ())

    def test_convertible_bond_fields_use_closed_factual_witnesses(self) -> None:
        cases = (
            (
                "特别提示",
                "5、“牧原转债”票面利率：第一年0.20%，第二年0.40%。",
                {"interest_terms"},
            ),
            (
                "一、可转换公司债券基本概况",
                "13、担保情况：本次发行的可转换公司债券不提供担保。",
                {"bond_guarantee_terms"},
            ),
            (
                "一、可转换公司债券基本概况",
                "14、“牧原转债”信用评级：主体AAA，债项AAA，展望稳定。",
                {"bond_credit_rating"},
            ),
            (
                "二、本期付息方案",
                (
                    "个人投资者债券利息所得税由公司按20%代扣代缴；"
                    "合格境外机构投资者暂免征收，其他持有人自行缴纳。"
                ),
                {"bond_interest_tax"},
            ),
            (
                "（3）付息方式",
                "④应付税项由持有人承担。",
                {"bond_interest_tax"},
            ),
        )
        for title, body, expected in cases:
            with self.subTest(title=title, expected=expected):
                admitted, drafts = _drafts_with_body(title, body)
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail("closed bond field is deterministic")
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type="convertible_bond",
                    ),
                    drafts=drafts,
                )
                self.assertTrue(expected.issubset(result.units[0].semantic_keys or ()))

        for body, forbidden in (
            ("公司预计“牧原转债”票面利率可能调整。", "interest_terms"),
            ("担保情况管理制度由公司董事会审议。", "bond_guarantee_terms"),
            ("债券信用评级机构联系人为张某。", "bond_credit_rating"),
            ("本期每10张债券派发利息15元（含税）。", "bond_interest_tax"),
        ):
            with self.subTest(body=body, forbidden=forbidden):
                admitted, drafts = _drafts_with_body("本期付息相关说明", body)
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
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
                        filing_type="convertible_bond",
                    ),
                    drafts=drafts,
                )
                self.assertNotIn(forbidden, result.units[0].semantic_keys or ())

    def test_annual_incentive_nonimplementation_sets_topic_without_inventing_applicability(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_body(
            "十五、公司股权激励计划、员工持股计划或其他员工激励措施的实施情况",
            "本公司于报告期内未实施股权激励计划、员工持股计划或其他员工激励措施。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("exact annual incentive title is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
        )

        self.assertEqual(result.units[0].semantic_keys, ("incentive_implementation",))
        self.assertIsNone(result.units[0].applicability)

        admitted, drafts = _drafts_with_body(
            "员工激励措施培训情况",
            "公司未实施本次员工培训计划。",
        )
        negative = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("nearby heading must abstain")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
        )
        self.assertNotIn("incentive_implementation", negative.units[0].semantic_keys or ())
        self.assertIsNone(negative.units[0].applicability)

    def test_litigation_route_preserves_source_checkbox_applicability(
        self,
    ) -> None:
        admitted, drafts = _drafts_with_bodies(
            "十一、重大诉讼、仲裁事项",
            "□适用 √不适用",
            (
                "本报告期公司无单一诉讼达到重大的事项，"
                "但累计诉讼金额达到披露要求，公司已披露累计诉讼情况公告。"
            ),
        )
        self.assertEqual(drafts[0].applicability, "not_applicable")
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("exact litigation title is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
        )

        self.assertIn("major_litigation_arbitration", result.units[0].semantic_keys or ())
        self.assertEqual(result.units[0].applicability, "not_applicable")

        admitted, drafts = _drafts_with_bodies(
            "十一、重大诉讼、仲裁事项",
            "□适用 √不适用",
            "本报告期公司不存在重大诉讼或仲裁。",
        )
        negative = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("exact litigation title is deterministic")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(title=None, filing_type="annual_report"),
            drafts=drafts,
        )
        self.assertEqual(negative.units[0].applicability, "not_applicable")

    def test_long_report_semantic_witnesses_are_closed_and_deterministic(
        self,
    ) -> None:
        no_call = _Adjudicator(
            lambda _batch: self.fail("closed long-report witnesses are deterministic")
        )

        admitted, drafts = _drafts_with_body(
            "金融负债的分类、确认及计量",
            "本节规定金融负债的初始确认、后续计量和终止确认。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=no_call,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="semiannual_report",
            ),
            drafts=drafts,
        )
        self.assertEqual(
            result.units[0].semantic_keys,
            ("financial_instruments_policy",),
        )

        admitted, drafts = _drafts_with_body(
            "研究开发费用加计扣除",
            "研发费用按规定加计扣除；形成无形资产的，按200%税前摊销。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=no_call,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="semiannual_report",
            ),
            drafts=drafts,
        )
        self.assertEqual(result.units[0].semantic_keys, ("rd_expenses",))

        admitted, drafts = _drafts_with_body(
            "市值管理",
            "公司将市值管理作为系统性工程，注销回购股份以提升股东的每股收益。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=no_call,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="semiannual_report",
            ),
            drafts=drafts,
        )
        self.assertTrue(
            {"market_value_management", "earnings_per_share"}.issubset(
                result.units[0].semantic_keys or ()
            )
        )

        admitted, drafts = _drafts_with_bodies(
            "利润表及现金流量表相关科目变动分析表",
            "销售费用变动原因说明：销售规模增加。",
            "管理费用变动原因说明：管理投入增加。",
            "财务费用变动原因说明：汇兑损益变化。",
            "研发费用变动原因说明：研发投入增加。",
            "营业收入变动原因说明：产品销量增加。",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=no_call,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="semiannual_report",
            ),
            drafts=drafts,
        )
        self.assertEqual(
            set(result.units[0].semantic_keys or ()),
            {
                "admin_expenses",
                "finance_expenses",
                "rd_expenses",
                "revenue_and_cost",
                "selling_expenses",
            },
        )

    def test_ragged_table_header_cannot_lock_a_financial_route(self) -> None:
        ragged = (
            "<table><tr><td>营业收入</td><td>本期数</td><td>上期数</td></tr>"
            "<tr><td>甲</td><td>1</td><td>2</td></tr>"
            "<tr><td>乙</td><td>3</td></tr></table>"
        )
        admitted, drafts = _drafts_with_table("经营情况", ragged)
        abstain = _Adjudicator(
            lambda batch: tuple(
                SemanticAdjudicationDecision(unit_index=unit.unit_index, routes=())
                for unit in batch.units
            )
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=abstain,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="semiannual_report",
            ),
            drafts=drafts,
        )
        self.assertNotIn("revenue_and_cost", result.units[0].semantic_keys or ())

    def test_malformed_table_grammar_cannot_lock_financial_routes(self) -> None:
        malformed_tables = (
            "<table><tfoot><tr><td>营业收入</td><td>100</td></tr></tfoot>"
            "<tfoot><tr><td>研发费用</td><td>20</td></tr></tfoot></table>",
            "<table><div><tr><td>营业收入</td><td>100</td></tr>"
            "<tr><td>研发费用</td><td>20</td></tr></div></table>",
        )
        for malformed in malformed_tables:
            with self.subTest(malformed=malformed):
                admitted, drafts = _drafts_with_table("经营情况", malformed)
                no_call = _Adjudicator(
                    lambda _batch: self.fail(
                        "malformed table text must not require adjudication"
                    )
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=no_call,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title=None,
                        filing_type="semiannual_report",
                    ),
                    drafts=drafts,
                )
                self.assertEqual(result.units[0].semantic_keys, None)
                self.assertEqual(result.receipts[0].decision_source, "rule_abstain")
                self.assertEqual(no_call.calls, 0)
                self.assertEqual(
                    result.units[0].payload.get("table_body"),
                    malformed,
                )

    def test_materiality_and_closed_tables_do_not_require_model_adjudication(
        self,
    ) -> None:
        no_call = _Adjudicator(
            lambda _batch: self.fail("closed table semantics are deterministic")
        )
        admitted, drafts = _drafts_with_bodies_and_table(
            "5、重要性标准确定方法和选择依据",
            (
                "√适用 □不适用",
                "在判断重要性时，本集团从项目性质和金额两方面予以判断。",
            ),
            "<table><tr><td>项目</td><td>重要性标准</td></tr>"
            "<tr><td>重要的资本化研发项目</td>"
            "<td>金额占研发支出10%以上</td></tr></table>",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=no_call,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="semiannual_report",
            ),
            drafts=drafts,
        )
        self.assertIsNone(result.units[0].semantic_keys)

        admitted, drafts = _drafts_with_bodies_and_table(
            "5、重要性标准确定方法和选择依据",
            ("在判断重要性时，本集团从项目性质和金额两方面予以判断。",),
            "<table><tr><td>项目</td><td>重要性标准</td></tr>"
            "<tr><td>营业收入</td><td>金额超过1000万元</td></tr>"
            "<tr><td>研发费用</td><td>金额超过500万元</td></tr></table>",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=no_call,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="semiannual_report",
            ),
            drafts=drafts,
        )
        self.assertIsNone(result.units[0].semantic_keys)

        admitted, drafts = _drafts_with_bodies_and_table(
            "重要非全资子公司的主要财务信息",
            ("（5）结构化主体的相关情况：", "□适用 √不适用"),
            '<table><tr><td rowspan="2">子公司名称</td>'
            '<td colspan="2">本期发生额</td></tr>'
            "<tr><td>营业收入</td><td>净利润</td></tr>"
            "<tr><td>甲公司</td><td>100</td><td>20</td></tr></table>",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=no_call,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="semiannual_report",
            ),
            drafts=drafts,
        )
        self.assertTrue(
            {
                "interests_in_other_entities",
                "subsidiaries_analysis",
                "structured_entities",
                "revenue_and_cost",
            }.issubset(result.units[0].semantic_keys or ())
        )

        admitted, drafts = _drafts_with_table(
            "购销商品、提供和接受劳务的关联交易",
            "<table><tr><td>关联方</td><td>关联交易内容</td>"
            "<td>本期发生额</td><td>上期发生额</td></tr>"
            "<tr><td>东风汽车财务有限公司</td><td>利息收入</td>"
            "<td></td><td>11.31</td></tr>"
            "<tr><td>某融资租赁有限公司</td><td>出售商品、提供劳务</td>"
            "<td>22,979.38</td><td>2,477.00</td></tr></table>",
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=no_call,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="semiannual_report",
            ),
            drafts=drafts,
        )
        self.assertTrue(
            {"daily_related_party_transactions", "interest_income"}.issubset(
                result.units[0].semantic_keys or ()
            )
        )
        self.assertNotIn(
            "revenue_recognition_policy",
            result.units[0].semantic_keys or (),
        )
        self.assertNotIn("lease_note", result.units[0].semantic_keys or ())

    def test_foreign_numbered_page_footnote_is_not_direct_semantic_evidence(
        self,
    ) -> None:
        document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "现金储备"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (
                            ProviderPayload(
                                "text",
                                None,
                                "现金储备超过731.5亿元<sup>2</sup>。",
                            ),
                        ),
                        annotation=None,
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, "其他指标"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        3,
                        0,
                        "text",
                        (ProviderPayload("text", None, "其他指标保持稳定。"),),
                        annotation=None,
                    ),
                    _block(
                        4,
                        0,
                        "page_footnote",
                        (
                            ProviderPayload(
                                "text",
                                None,
                                (
                                    "<sup>2</sup>现金储备包含货币资金、"
                                    "交易性金融资产。"
                                ),
                            ),
                        ),
                        annotation="page_footnote",
                    ),
                ),
            ),
            segments=(),
        )
        admitted = _admitted(document)
        drafts = build_provider_units(admitted).units
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("a foreign numbered footnote is related-only")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title=None,
                filing_type="semiannual_report",
            ),
            drafts=drafts,
        )
        self.assertEqual(len(result.units), 2)
        self.assertNotIn(
            "monetary_funds",
            result.units[1].semantic_keys or (),
        )
        self.assertNotIn(
            "trading_financial_assets",
            result.units[1].semantic_keys or (),
        )
        self.assertEqual(adjudicator.calls, 0)

    def test_numbered_page_footnote_requires_an_owned_antecedent(
        self,
    ) -> None:
        context = SemanticDocumentContext(title=None, filing_type="semiannual_report")
        footnote = (
            "<sup>2</sup>现金储备包含货币资金、交易性金融资产。"
        )

        def route(document: ProviderDocument) -> tuple[SemanticRouteBatchResult, int]:
            admitted = _admitted(document)
            drafts = build_provider_units(admitted).units
            def decide(
                batch: SemanticAdjudicationBatch,
            ) -> tuple[SemanticAdjudicationDecision, ...]:
                return tuple(
                    SemanticAdjudicationDecision(
                        unit_index=unit.unit_index,
                        routes=tuple(
                            SemanticAdjudicatedRoute(
                                key=candidate.key,
                                support_ids=candidate.source_ids,
                            )
                            for candidate in unit.candidates
                            if candidate.key
                            in {"monetary_funds", "trading_financial_assets"}
                        ),
                    )
                    for unit in batch.units
                )
            adjudicator = _Adjudicator(decide)
            result = SemanticRouter(
                taxonomy=load_semantic_route_taxonomy(),
                adjudicator=adjudicator,
                cache=_MemoryCache(),
            ).route(
                admitted=admitted,
                document=context,
                drafts=drafts,
            )
            return result, adjudicator.calls

        owned_document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "其他指标<sup>2</sup>"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "page_footnote",
                        (ProviderPayload("text", None, footnote),),
                        annotation="page_footnote",
                    ),
                ),
            ),
            segments=(),
        )
        result, _ = route(owned_document)
        self.assertIn(
            "trading_financial_assets",
            result.units[0].semantic_keys or (),
        )

        wrapped_document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "（七）担保情况及对债"),),
                        annotation="title",
                        level=2,
                        bbox=ProviderBBox(100, 100, 900, 119),
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (
                            ProviderPayload(
                                "text",
                                None,
                                "券投资者权益<sup>2</sup>的影响",
                            ),
                        ),
                        annotation="paragraph",
                        bbox=ProviderBBox(150, 127, 390, 145),
                    ),
                    _block(
                        2,
                        0,
                        "page_footnote",
                        (ProviderPayload("text", None, footnote),),
                        annotation="page_footnote",
                    ),
                ),
            ),
            segments=(),
        )
        admitted = _admitted(wrapped_document)
        draft = build_provider_units(admitted).units[0]
        result, _ = route(wrapped_document)
        self.assertEqual(
            [
                fragment.source_index
                for fragment in draft.locator.heading_chain[-1].continuation_fragments
            ],
            [1],
        )
        self.assertIn(
            "trading_financial_assets",
            result.units[0].semantic_keys or (),
        )

        foreign_document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "现金储备<sup>2</sup>"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "流动性<sup>2</sup>"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        2,
                        0,
                        "text",
                        (ProviderPayload("text", None, "其他指标"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        3,
                        0,
                        "page_footnote",
                        (ProviderPayload("text", None, footnote),),
                        annotation="page_footnote",
                    ),
                ),
            ),
            segments=(),
        )
        result, _ = route(foreign_document)
        self.assertNotIn(
            "trading_financial_assets",
            result.units[-1].semantic_keys or (),
        )

        mixed_document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "现金储备<sup>2</sup>"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "text",
                        (ProviderPayload("text", None, "其他指标<sup>2</sup>"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        2,
                        0,
                        "page_footnote",
                        (ProviderPayload("text", None, footnote),),
                        annotation="page_footnote",
                    ),
                ),
            ),
            segments=(),
        )
        result, _ = route(mixed_document)
        self.assertIn(
            "trading_financial_assets",
            result.units[-1].semantic_keys or (),
        )

        no_antecedent_document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "其他指标"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "page_footnote",
                        (ProviderPayload("text", None, footnote),),
                        annotation="page_footnote",
                    ),
                ),
            ),
            segments=(),
        )
        result, calls = route(no_antecedent_document)
        self.assertNotIn("monetary_funds", result.units[0].semantic_keys or ())
        self.assertNotIn(
            "trading_financial_assets",
            result.units[0].semantic_keys or (),
        )
        self.assertEqual(calls, 0)

        unnumbered_document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (ProviderPayload("text", None, "其他指标"),),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "page_footnote",
                        (
                            ProviderPayload(
                                "text",
                                None,
                                "现金储备包含货币资金、交易性金融资产。",
                            ),
                        ),
                        annotation="page_footnote",
                    ),
                ),
            ),
            segments=(),
        )
        result, _ = route(unnumbered_document)
        self.assertIn(
            "trading_financial_assets",
            result.units[0].semantic_keys or (),
        )

    def test_cross_reference_carrier_must_end_at_the_citation_target(self) -> None:
        pure_references = (
            "关于其他重要事项，详见公司2026年半年度报告全文。",
            (
                "关于其他重要事项，详见公司2026年半年度报告全文"
                "（公告编号：2026-001）。"
            ),
            "关于其他重要事项，详见《关于完成重大资产收购的公告》。",
            (
                "关于其他重要事项，详见"
                "《关于完成重大资产收购，更正相关事项的公告》。"
            ),
            (
                "关于其他重要事项，详见"
                "《关于完成重大资产收购；更正相关事项的公告》。"
            ),
        )
        for body in pure_references:
            with self.subTest(pure_reference=body):
                admitted, drafts = _drafts_with_parent_heading(
                    "第六节 重要事项",
                    "其他重要事项",
                    body,
                )
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail(
                        "a pure cross-reference carrier must not call the model"
                    )
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司2026年半年度报告",
                        filing_type="semiannual_report",
                    ),
                    drafts=drafts,
                )
                self.assertIsNone(result.units[1].semantic_keys)
                self.assertEqual(adjudicator.calls, 0)

        factual_tails = (
            (
                "详见公司2026年半年度报告全文，"
                "其中公司本期完成重大资产收购。"
            ),
            (
                "关于其他重要事项，详见公司2026年半年度报告全文，"
                "公司本期完成重大资产收购。"
            ),
            "公司本期完成重大资产收购具体情况详见相关公告全文。",
            (
                "公司本期完成重大资产收购具体情况详见"
                "《关于完成重大资产收购的公告》。"
            ),
            (
                "公司本期完成重大资产收购具体情况详见"
                "“关于完成重大资产收购的公告”。"
            ),
            (
                "公司本期完成重大资产收购具体内容参见"
                "《关于本次重大资产收购完成的公告》"
                "（公告编号：2026-001）。"
            ),
            (
                "公司本期完成重大资产收购具体情况详见"
                "《关于完成重大资产收购，更正相关事项的公告》。"
            ),
            (
                "公司本期完成重大资产收购具体情况详见"
                "《关于完成重大资产收购；更正相关事项的公告》。"
            ),
        )
        for body in factual_tails:
            with self.subTest(factual_tail=body):
                admitted, drafts = _drafts_with_parent_heading(
                    "第六节 重要事项",
                    "其他重要事项",
                    body,
                )
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail(
                        "an exact important-matters fact is deterministic"
                    )
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司2026年半年度报告",
                        filing_type="semiannual_report",
                    ),
                    drafts=drafts,
                )
                self.assertEqual(
                    result.units[1].semantic_keys,
                    ("major_asset_acquisition_progress",),
                )
                self.assertEqual(adjudicator.calls, 0)

        for filing_type in ("risk_alert", "share_buyback", "other"):
            with self.subTest(out_of_scope=filing_type):
                admitted, drafts = _drafts_with_parent_heading(
                    "第六节 重要事项",
                    "其他重要事项",
                    "公司本期完成重大资产收购。",
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
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
                        title="某公司公告",
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )
                self.assertNotIn(
                    "major_asset_acquisition_progress",
                    result.units[1].semantic_keys or (),
                )

        for body in (
            "公司协助客户完成重大资产收购。",
            "公司完成重大资产收购前期准备工作。",
        ):
            with self.subTest(non_issuer_or_incomplete=body):
                admitted, drafts = _drafts_with_parent_heading(
                    "第六节 重要事项",
                    "其他重要事项",
                    body,
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
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
                        title="某公司2026年半年度报告",
                        filing_type="semiannual_report",
                    ),
                    drafts=drafts,
                )
                self.assertNotIn(
                    "major_asset_acquisition_progress",
                    result.units[1].semantic_keys or (),
                )

        quoted_notice_titles = (
            (
                "详见公司《关于完成重大资产收购的公告》，"
                "公司董事会今日召开会议。"
            ),
            (
                "公司已披露《关于完成重大资产收购的公告》，"
                "本期无其他重大事项。"
            ),
            "详见《关于完成重大资产收购的公告。",
        )
        for body in quoted_notice_titles:
            with self.subTest(quoted_notice_title=body):
                admitted, drafts = _drafts_with_parent_heading(
                    "第六节 重要事项",
                    "其他重要事项",
                    body,
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
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
                        title="某公司2026年半年度报告",
                        filing_type="semiannual_report",
                    ),
                    drafts=drafts,
                )
                self.assertNotIn(
                    "major_asset_acquisition_progress",
                    result.units[1].semantic_keys or (),
                )

    def test_forecast_range_table_requires_typed_period_and_numeric_range(self) -> None:
        positive_table = (
            "<table><tr><td>项目</td><td>本报告期</td><td>上年同期</td></tr>"
            "<tr><td>归属于上市公司股东的净利润</td>"
            "<td>盈利：351万元-474万元</td><td>盈利：12,665.43万元</td>"
            "</tr></table>"
        )
        admitted, drafts = _drafts_with_table(
            "2、预计的经营业绩：同向下降",
            positive_table,
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("typed forecast ranges are deterministic")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司半年度业绩预告",
                filing_type="performance_forecast",
            ),
            drafts=drafts,
        )
        self.assertEqual(
            result.units[0].semantic_keys,
            ("performance_forecast_range",),
        )
        self.assertEqual(result.receipts[0].decision_source, "deterministic")
        self.assertEqual(adjudicator.calls, 0)

        for filing_type, table_html in (
            (
                "performance_forecast",
                positive_table.replace("351万元-474万元", "351万元"),
            ),
            ("semiannual_report", positive_table),
            (
                "performance_forecast",
                positive_table.replace("351万元-474万元", "2023年-2024年"),
            ),
        ):
            with self.subTest(filing_type=filing_type):
                admitted, drafts = _drafts_with_table(
                    "预计的经营业绩说明",
                    table_html,
                )
                negative = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "an unqualified forecast table must not call a model"
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司报告",
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )
                self.assertNotIn(
                    "performance_forecast_range",
                    negative.units[0].semantic_keys or (),
                )

        split_document = _document(
            pages=(
                (
                    _block(
                        0,
                        0,
                        "text",
                        (
                            ProviderPayload(
                                "text",
                                None,
                                "2、预计的经营业绩：同向下降",
                            ),
                        ),
                        annotation="title",
                        level=1,
                    ),
                    _block(
                        1,
                        0,
                        "table",
                        (
                            ProviderPayload(
                                "table_body",
                                None,
                                (
                                    "<table><tr><td>项目</td><td>本报告期</td>"
                                    "</tr><tr><td>净利润</td><td>351万元</td>"
                                    "</tr></table>"
                                ),
                            ),
                        ),
                        annotation=None,
                    ),
                    _block(
                        2,
                        0,
                        "table",
                        (
                            ProviderPayload(
                                "table_body",
                                None,
                                (
                                    "<table><tr><td>其他项目</td>"
                                    "<td>351万元-474万元</td></tr></table>"
                                ),
                            ),
                        ),
                        annotation=None,
                    ),
                ),
            ),
            segments=(),
        )
        split_admitted = _admitted(split_document)
        split_drafts = build_provider_units(split_admitted).units
        self.assertEqual(len(split_drafts), 1)
        split_result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("separate tables cannot corroborate")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=split_admitted,
            document=SemanticDocumentContext(
                title="某公司业绩预告",
                filing_type="performance_forecast",
            ),
            drafts=split_drafts,
        )
        self.assertNotIn(
            "performance_forecast_range",
            split_result.units[0].semantic_keys or (),
        )

    def test_forecast_accountant_firm_heading_is_a_scoped_risk_role(self) -> None:
        admitted, drafts = _drafts_with_body(
            "二、与会计师事务所沟通情况",
            "本次业绩预告相关财务数据未经注册会计师审计。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("an exact forecast role is deterministic")
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
            ("performance_forecast_risk",),
        )
        self.assertEqual(adjudicator.calls, 0)

        admitted, drafts = _drafts_with_body(
            "会计师事务所沟通管理制度",
            "本制度规定年度沟通流程。",
        )
        negative = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("a management policy stays lexical")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司业绩预告",
                filing_type="performance_forecast",
            ),
            drafts=drafts,
        )
        self.assertNotIn(
            "performance_forecast_risk",
            negative.units[0].semantic_keys or (),
        )

    def test_credit_impairment_preparation_is_an_exact_heading_alias(self) -> None:
        admitted, drafts = _drafts_with_body(
            "1．信用减值准备",
            "本期按预期信用损失模型计提336,717.80元并转回1,197,181.15元。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("the exact preparation heading is deterministic")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司计提减值准备公告",
                filing_type="risk_alert",
            ),
            drafts=drafts,
        )
        self.assertEqual(result.units[0].semantic_keys, ("credit_impairment_loss",))
        self.assertEqual(
            result.units[0].section_keys,
            ("credit_impairment_loss",),
        )
        self.assertEqual(adjudicator.calls, 0)

        for title in ("信用减值准备管理制度", "坏账准备"):
            with self.subTest(title=title):
                admitted, drafts = _drafts_with_body(title, "本制度自发布之日起施行。")
                negative = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail("adjacent concepts stay lexical")
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司风险提示公告",
                        filing_type="risk_alert",
                    ),
                    drafts=drafts,
                )
                self.assertNotIn(
                    "credit_impairment_loss",
                    negative.units[0].semantic_keys or (),
                )

    def test_incentive_quantity_price_adjustment_requires_local_change_facts(self) -> None:
        admitted, drafts = _drafts_with_body(
            "三、《关于调整2020年股票期权激励计划授予数量及授予价格的公告》更正情况",
            (
                "更正前：股票期权总量由30.4万份调整为36.76万份，"
                "授予价格由10元/股调整为9.81291元/股；"
                "更正后：股票期权总量由30.4万份调整为35.488万份，"
                "授予价格由10元/股调整为7.01元/股。"
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("independent adjustment facts are deterministic")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司股权激励更正公告",
                filing_type="equity_incentive",
            ),
            drafts=drafts,
        )
        self.assertEqual(result.units[0].semantic_keys, ("incentive_adjustment",))
        self.assertEqual(adjudicator.calls, 0)

        negatives = (
            (
                "关于股票期权激励计划授予数量及授予价格的公告",
                "本次授予数量为30万份，授予价格为10元/股。",
            ),
            (
                "关于调整股票期权激励计划授予数量及授予价格的公告",
                "具体内容详见同日披露的调整公告。",
            ),
            (
                "法律意见书",
                "律师认为本次调整已获得必要授权。",
            ),
        )
        for title, body in negatives:
            with self.subTest(title=title):
                admitted, drafts = _drafts_with_body(title, body)
                negative = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "an unsupported adjustment must stay lexical"
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司股权激励公告",
                        filing_type="equity_incentive",
                    ),
                    drafts=drafts,
                )
                self.assertNotIn(
                    "incentive_adjustment",
                    negative.units[0].semantic_keys or (),
                )

    def test_major_contract_amount_and_risk_routes_are_scope_bound(self) -> None:
        cases = (
            (
                "1、排他性许可协议",
                (
                    "本次交易对价包括首付款42,000万元及后续权利金，"
                    "权利金金额为净销售额的15%。"
                ),
                "contract_value",
            ),
            ("（1）首付款", "首付款合计42,000万元。", "contract_value"),
            ("（2）权利金", "权利金金额为净销售额的15%。", "contract_value"),
            ("八、风险提示", "合同履行尚需审批，结果存在不确定性。", "contract_risk"),
        )
        for title, body, expected in cases:
            with self.subTest(title=title):
                admitted, drafts = _drafts_with_body(title, body)
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail("closed contract facts are deterministic")
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司签署重大合同公告",
                        filing_type="major_contract",
                    ),
                    drafts=drafts,
                )
                self.assertIn(expected, result.units[0].semantic_keys or ())
                self.assertEqual(adjudicator.calls, 0)

        admitted, drafts = _drafts_with_body(
            "八、风险提示",
            "公司经营存在一般市场风险。",
        )
        out_of_scope = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("out-of-scope risk headings stay lexical")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司其他公告",
                filing_type="other",
            ),
            drafts=drafts,
        )
        self.assertNotIn("contract_risk", out_of_scope.units[0].semantic_keys or ())

        value_negatives = (
            (
                "major_contract",
                "排他性许可协议",
                "首付款支付义务由双方另行约定。",
            ),
            (
                "major_contract",
                "排他性许可协议",
                "首付款逾期违约金为5万元。",
            ),
            (
                "major_contract",
                "排他性许可协议",
                "首付款逾期利息为5%。",
            ),
            (
                "major_contract",
                "排他性许可协议",
                "具体首付款42,000万元详见《许可协议》。",
            ),
            (
                "other",
                "合作安排",
                "本次交易对价包括首付款42,000万元及后续权利金。",
            ),
        )
        for filing_type, title, body in value_negatives:
            with self.subTest(value_negative=(filing_type, title)):
                admitted, drafts = _drafts_with_body(title, body)
                negative = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=_Adjudicator(
                        lambda _batch: self.fail(
                            "unsupported or out-of-scope amounts stay lexical"
                        )
                    ),
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司公告",
                        filing_type=filing_type,
                    ),
                    drafts=drafts,
                )
                self.assertNotIn(
                    "contract_value",
                    negative.units[0].semantic_keys or (),
                )

    def test_transaction_pricing_requires_heading_and_local_pricing_evidence(self) -> None:
        admitted, drafts = _drafts_with_body(
            "（一）许可协议定价情况",
            "经采用收益法评估，评估值为42,000万元并作为作价依据。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("source-bound transaction pricing is deterministic")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司关联交易公告",
                filing_type="major_contract",
            ),
            drafts=drafts,
        )
        self.assertEqual(
            result.units[0].semantic_keys,
            ("transaction_pricing_basis",),
        )
        self.assertEqual(adjudicator.calls, 0)

        admitted, drafts = _drafts_with_body(
            "许可协议定价情况",
            "具体内容详见《资产评估报告》。",
        )
        negative = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("a pricing cross-reference stays lexical")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司关联交易公告",
                filing_type="major_contract",
            ),
            drafts=drafts,
        )
        self.assertNotIn(
            "transaction_pricing_basis",
            negative.units[0].semantic_keys or (),
        )

    def test_title_bound_internal_pricing_reference_is_not_direct(self) -> None:
        admitted, drafts = _drafts_with_body(
            "四、关联交易的定价政策及定价依据",
            (
                "定价政策及定价依据详见下文"
                "“五、关联交易协议的主要内容之（二）之3.认购价格”。"
            ),
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("a pure internal reference stays lexical")
        )

        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司向特定对象发行股票暨关联交易公告",
                filing_type="additional_issuance",
            ),
            drafts=drafts,
        )

        self.assertIsNone(result.units[0].semantic_keys)
        self.assertEqual(adjudicator.calls, 0)

    def test_internal_pricing_reference_with_local_fact_remains_direct(self) -> None:
        bodies = (
            "定价政策及定价依据详见下文“认购价格为7.01元/股”。",
            (
                "定价政策及定价依据详见下文"
                "“五、关联交易协议的主要内容之（二）之3.认购价格”，"
                "本次发行价格不低于定价基准日前二十个交易日交易均价的80%。"
            ),
            (
                "定价政策及定价依据详见下文"
                "“五、关联交易协议的主要内容之（二）之3.认购价格”，"
                "本次交易定价公平合理。"
            ),
        )
        for body in bodies:
            with self.subTest(body=body):
                admitted, drafts = _drafts_with_body(
                    "四、关联交易的定价政策及定价依据",
                    body,
                )
                adjudicator = _Adjudicator(
                    lambda _batch: self.fail("the exact factual heading is closed")
                )
                result = SemanticRouter(
                    taxonomy=load_semantic_route_taxonomy(),
                    adjudicator=adjudicator,
                    cache=_MemoryCache(),
                ).route(
                    admitted=admitted,
                    document=SemanticDocumentContext(
                        title="某公司向特定对象发行股票暨关联交易公告",
                        filing_type="additional_issuance",
                    ),
                    drafts=drafts,
                )
                self.assertEqual(
                    result.units[0].semantic_keys,
                    ("transaction_pricing_basis",),
                )
                self.assertEqual(adjudicator.calls, 0)

    def test_named_agreement_contents_is_structural_context_not_fake_direct(self) -> None:
        admitted, drafts = _drafts_with_parent_heading(
            "三、《排他性许可协议》、《知识产权转让合同》主要内容",
            "（一）排他性许可协议",
            "许可区域为中国大陆地区及美国区域。",
        )
        adjudicator = _Adjudicator(
            lambda _batch: self.fail("agreement context is deterministic structure")
        )
        result = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=adjudicator,
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司签署重大合同公告",
                filing_type="major_contract",
            ),
            drafts=drafts,
        )
        self.assertIsNone(result.units[0].semantic_keys)
        self.assertEqual(
            result.units[0].section_keys,
            ("transaction_agreement_terms",),
        )
        self.assertEqual(
            result.units[1].section_keys,
            ("transaction_agreement_terms",),
        )
        self.assertEqual(adjudicator.calls, 0)

        admitted, drafts = _drafts_with_parent_heading(
            "《风险管理制度》主要内容",
            "适用范围",
            "本制度适用于公司各部门。",
        )
        negative = SemanticRouter(
            taxonomy=load_semantic_route_taxonomy(),
            adjudicator=_Adjudicator(
                lambda _batch: self.fail("a management policy is not an agreement")
            ),
            cache=_MemoryCache(),
        ).route(
            admitted=admitted,
            document=SemanticDocumentContext(
                title="某公司制度公告",
                filing_type="other",
            ),
            drafts=drafts,
        )
        self.assertNotIn(
            "transaction_agreement_terms",
            negative.units[0].section_keys or (),
        )
        self.assertNotIn(
            "transaction_agreement_terms",
            negative.units[1].section_keys or (),
        )


if __name__ == "__main__":
    unittest.main()
