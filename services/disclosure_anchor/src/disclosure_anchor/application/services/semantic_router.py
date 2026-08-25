"""Source-bound semantic routing for lexical retrieval without embeddings."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
import hashlib
import json
import re

from disclosure_anchor.application.contracts.html_visible_text import (
    html_table_semantic_segments,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    AdmittedProviderDocument,
)
from disclosure_anchor.application.contracts.provider_document import ProviderBlock
from disclosure_anchor.application.contracts.provider_unit import ProviderUnitDraft
from disclosure_anchor.application.contracts.semantic_routes import (
    MAX_SEMANTIC_CANDIDATES,
    SEMANTIC_FAILOVER_POLICY_VERSION,
    SEMANTIC_FALLBACK_KEY,
    SEMANTIC_ROUTE_RECEIPT_V1,
    SEMANTIC_ROUTE_RECEIPT_VERSION,
    SEMANTIC_ROUTER_VERSION,
    SemanticAdjudicatedRoute,
    SemanticAdjudicationDecision,
    SemanticAdjudicationReceipt,
    SemanticAdjudicatorMetadata,
    SemanticDocumentContext,
    SemanticRouteCandidate,
    SemanticRouteContractError,
    SemanticRouteDefinition,
    SemanticRouteEvidence,
    SemanticRouteEvidenceKind,
    SemanticRouteReceipt,
    SemanticRouteSource,
    SemanticRouteSourceKind,
    SemanticRouteTaxonomy,
    SemanticRouteUnitInput,
)
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicationBatch,
    SemanticAdjudicationExecutorPort,
    SemanticAdjudicationOutcome,
    SemanticRouteAdjudicatorPort,
    SemanticRouteAdjudicatorError,
    SemanticRouteCachePort,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    replay_provider_unit_search_binding,
    replay_provider_unit_search_binding_source_text,
)
from disclosure_anchor.domain.services.unit_hashing import compute_unit_hashes
from disclosure_anchor.domain import entities as e


_CN_ORDINAL = r"[一二三四五六七八九十百]+"
_ROMAN_OR_ALPHA_ORDINAL = r"(?:[A-Za-z]|[ivxlcdmIVXLCDM]{1,8})"
_TITLE_BULLET_RE = re.compile(
    r"^\s*[\ue000-\uf8ff●•▪◼■◆◇√□☑☒*\-—–－]+\s*"
)
_TITLE_EDGE_QUOTES_RE = re.compile(r"^[“”\"‘’']+|[“”\"‘’']+$")
_TITLE_NUMBERING_RE = re.compile(
    rf"^\s*(?:"
    rf"第{_CN_ORDINAL}[节章]\s*|"
    rf"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]\s*|"
    rf"[（(](?:\d{{1,3}}|{_CN_ORDINAL}|{_ROMAN_OR_ALPHA_ORDINAL})[）)]\s*|"
    rf"{_CN_ORDINAL}\s*、\s*|"
    rf"\d{{1,3}}(?:[.．]\d{{1,3}})+\s*(?=[A-Za-z\u3400-\u9fff])|"
    rf"\d{{1,3}}\s*(?:、|[.．](?!\d)|[)）])\s*|"
    rf"\d{{1,3}}\s+(?=\S)"
    rf")"
)
_CONTINUATION_RE = re.compile(r"\s*(?:[（(]\s*续\s*[）)]|[-—–－]\s*续)\s*$")
_APPLICABILITY_SUFFIX_RE = re.compile(r"(?:\s*[√□☑☒]\s*(?:不适用|适用))+\s*$")
_TITLE_PREFIX_RE = re.compile(
    r"^(?:(?:未经审计|本集团|本公司|本行|本次|本期)\s*)+"
)
_TITLE_YEAR_PREFIX_RE = re.compile(
    r"^(?:19|20)\d{2}\s*年(?:度|半年度|上半年度|下半年度|上半年|下半年)?\s*"
)
_SPACE_RE = re.compile(r"\s+")
_RESOLVED_PROPOSAL_RE = re.compile(
    r"(?:审议通过|表决通过|审议批准|审议同意)(?:了)?"
    r"(?:《(?P<quoted>[^》]{2,240})》|(?P<plain>关于[^。；]{2,240}?的议案))"
)
_NAMED_AGREEMENT_CONTENTS_RE = re.compile(
    r"^(?:《[^》]{2,160}(?:协议|合同)》(?:[、,，和及与])?)+主要内容$"
)

_MAX_SOURCE_TEXT = 800
_MAX_UNIT_SOURCE_TEXT = 4000
_MIN_CANDIDATE_SCORE = 55
_MIN_TITLE_SIMILARITY = 0.68
_MIN_SCOPED_TITLE_SIMILARITY = 0.30
_PERIODIC_FILING_TYPES = {
    "annual_report",
    "semiannual_report",
    "quarterly_report",
}
_MODEL_MIN_CANDIDATES = 2
_MODEL_MAX_CANDIDATES = MAX_SEMANTIC_CANDIDATES
# Exact structural headings normally cannot corroborate a direct route whose
# label is only a substring of that context.  The internal-control audit table
# is the sole source-reviewed exception because it independently reports an
# opinion; exact company-profile direct and section routes use separate paths.
_INTENTIONAL_CONTEXT_DIRECT_OVERLAPS = {
    ("internal_control", "audit_opinion"),
}
_OVERVIEW_TITLE_MARKERS = (
    "重要内容提示",
    "重大事项提示",
    "主要内容",
    "基本概况",
    "情况概要",
    "概况",
    "概要",
    "概览",
    "概述",
    "汇总",
    "一览",
    "活动记录表",
    "报告书",
    "简要介绍",
    "具体方案",
    "业绩预告情况",
)
_CORRECTION_OVERVIEW_SUFFIXES = (
    "补充更正公告",
    "更正公告",
    "更正说明",
)
_LOCAL_CONTENT_SOURCE_KINDS = frozenset(
    {
        "body_text",
        "table_text",
        "table_field_label",
        "table_column_header",
    }
)
_QUOTED_BOND_FIELD_PREFIX_RE = re.compile(
    r'^[“"「『](?P<instrument>[^”"」』]{1,24}(?:转债|债券))[”"」』]'
)
_INTEREST_BEARING_DEBT_KEYS = frozenset(
    {
        "bonds_payable",
        "long_term_borrowings",
        "noncurrent_due_within_one_year",
        "other_noncurrent_liabilities",
        "other_payables",
        "short_term_borrowings",
    }
)
_INTEREST_BEARING_DEBT_LABELS = (
    "一年内到期的非流动资产",
    "一年内到期的非流动负债",
    "其他非流动负债",
    "其他应付款",
    "应付债券",
    "长期借款",
    "短期借款",
)
_ACCOUNTING_POLICY_RELATION_KEYS = frozenset(
    {
        "deferred_tax",
        "income_tax_expense",
        "lease_liabilities",
        "right_of_use_assets",
    }
)
_REFERENCE_MARKERS = (
    "详见",
    "参见",
    "请见",
    "参阅",
    "查阅",
    "见附注",
    "列示于附注",
)
_REFERENCE_SPAN_RE = re.compile(
    r"(?:详见|参见|请见|参阅|查阅)|"
    r"(?:见|载于|列示于|披露于)(?:本|第|上述|前述|相关)"
    r"[^，,。；;]{0,20}(?:附注|报告|财务报表|章节|节|页)"
)
_BUYBACK_RESULT_NOTICE_WINDOW_RE = re.compile(
    r"回购结果(?:暨|和)股份变动公告期间"
)


@dataclass(frozen=True, slots=True)
class SemanticRouteBatchResult:
    units: tuple[ProviderUnitDraft, ...]
    receipts: tuple[SemanticRouteReceipt, ...]
    adjudication_outcomes: tuple[SemanticAdjudicationOutcome, ...] = ()

    def __post_init__(self) -> None:
        if len(self.units) != len(self.receipts):
            raise SemanticRouteContractError(
                "semantic route result must cover every Unit"
            )
        unit_indices = tuple(unit.unit_index for unit in self.units)
        if len(unit_indices) != len(set(unit_indices)) or unit_indices != tuple(
            sorted(unit_indices)
        ):
            raise SemanticRouteContractError(
                "semantic routed Units must have unique source order"
            )


@dataclass(slots=True)
class _CandidateState:
    key: str
    score: int = 0
    source_ids: list[str] | None = None
    evidence_kinds: list[SemanticRouteEvidenceKind] | None = None
    locked: bool = False

    def add(
        self,
        *,
        score: int,
        source_id: str,
        evidence_kind: SemanticRouteEvidenceKind,
        locked: bool = False,
    ) -> None:
        self.score = max(self.score, score)
        if self.source_ids is None:
            self.source_ids = []
        if source_id not in self.source_ids:
            self.source_ids.append(source_id)
        if self.evidence_kinds is None:
            self.evidence_kinds = []
        if evidence_kind not in self.evidence_kinds:
            self.evidence_kinds.append(evidence_kind)
        self.locked = self.locked or locked


class SemanticRouter:
    """Route provider Units through exact evidence then bounded adjudication."""

    def __init__(
        self,
        *,
        taxonomy: SemanticRouteTaxonomy,
        adjudicator: SemanticRouteAdjudicatorPort | None = None,
        cache: SemanticRouteCachePort | None = None,
        executor: SemanticAdjudicationExecutorPort | None = None,
        batch_size: int = 16,
    ) -> None:
        if batch_size < 1 or batch_size > 32:
            raise ValueError("semantic adjudication batch size must be 1..32")
        if executor is None and (adjudicator is None or cache is None):
            raise ValueError("legacy semantic routing needs adjudicator and cache")
        if executor is not None and (adjudicator is not None or cache is not None):
            raise ValueError("semantic router accepts either executor or legacy adapters")
        self.taxonomy = taxonomy
        self.adjudicator = adjudicator
        self.cache = cache
        self.executor = executor
        self.batch_size = batch_size
        self._definitions = taxonomy.by_key()
        self._normalized_labels = {
            item.key: tuple(dict.fromkeys(_normalize_title(label) for label in item.labels))
            for item in taxonomy.definitions
            if item.key != taxonomy.fallback_key
        }
        self._normalized_title_labels = {
            item.key: tuple(
                dict.fromkeys(
                    _normalize_title(label)
                    for label in (*item.labels, *item.heading_labels)
                )
            )
            for item in taxonomy.definitions
            if item.key != taxonomy.fallback_key
        }
        self._composite_sections = {
            _normalize_title(item.label): item.keys
            for item in taxonomy.composite_sections
        }
        self._direct_composites = {
            _normalize_title(item.label): item.keys
            for item in taxonomy.direct_composites
        }

    def route(
        self,
        *,
        admitted: AdmittedProviderDocument,
        document: SemanticDocumentContext,
        drafts: Sequence[ProviderUnitDraft],
    ) -> SemanticRouteBatchResult:
        """Create new receipts, consulting the model only for uncached ambiguity."""

        inputs = tuple(
            self._prepare_input(admitted=admitted, document=document, draft=draft)
            for draft in drafts
        )
        receipts: dict[int, SemanticRouteReceipt] = {}
        outcomes: list[SemanticAdjudicationOutcome] = []
        model_inputs: list[SemanticRouteUnitInput] = []
        for unit_input in inputs:
            deterministic = self._deterministic_candidates(unit_input)
            requires_model = self._requires_model(
                unit_input,
                document=document,
            )
            if deterministic and not requires_model:
                receipts[unit_input.unit_index] = self._deterministic_receipt(
                    unit_input,
                    deterministic,
                )
                continue
            if not unit_input.candidates:
                receipts[unit_input.unit_index] = self._fallback_receipt(unit_input)
                continue
            if not requires_model:
                receipts[unit_input.unit_index] = self._rule_abstain_receipt(unit_input)
                continue
            model_inputs.append(unit_input)

        for requested in _semantic_adjudication_groups(
            model_inputs,
            batch_size=self.batch_size,
        ):
            group_hash = _semantic_adjudication_group_hash(requested)
            if self.executor is not None:
                outcome = self.executor.adjudicate(
                    SemanticAdjudicationBatch(
                        document=document,
                        taxonomy=self.taxonomy,
                        units=requested,
                    ),
                    group_hash=group_hash,
                )
                outcomes.append(outcome)
                if outcome.group_hash != group_hash:
                    raise SemanticRouteContractError(
                        "semantic executor returned a different group identity"
                    )
                if outcome.degraded_unavailable:
                    for unit_input in requested:
                        receipts[unit_input.unit_index] = (
                            self._adjudicator_unavailable_receipt(
                                unit_input,
                                outcome=outcome,
                            )
                        )
                    continue
                decision_by_index = {
                    decision.unit_index: self._canonicalize_decision(
                        next(
                            item
                            for item in requested
                            if item.unit_index == decision.unit_index
                        ),
                        decision,
                    )
                    for decision in outcome.decisions
                }
                if set(decision_by_index) != {
                    item.unit_index for item in requested
                } or len(decision_by_index) != len(outcome.decisions):
                    raise SemanticRouteAdjudicatorError(
                        "semantic executor did not cover the exact requested Units",
                        reason_code="invalid_contract",
                        retryable=False,
                        attempts=outcome.attempts,
                    )
                for unit_input in requested:
                    decision = decision_by_index[unit_input.unit_index]
                    try:
                        self._validate_decision(unit_input, decision)
                    except SemanticRouteContractError as exc:
                        raise SemanticRouteAdjudicatorError(
                            str(exc),
                            reason_code="invalid_decision",
                            retryable=False,
                            attempts=outcome.attempts,
                        ) from exc
                    receipts[unit_input.unit_index] = self._model_receipt_v2(
                        unit_input=unit_input,
                        decision=decision,
                        outcome=outcome,
                    )
                continue
            assert self.cache is not None
            group_cache_keys = {
                unit_input.unit_index: self._cache_key(
                    input_hash=unit_input.input_hash,
                    group_hash=group_hash,
                )
                for unit_input in requested
            }
            cached_decisions = {
                unit_input.unit_index: self.cache.get(
                    group_cache_keys[unit_input.unit_index]
                )
                for unit_input in requested
            }
            if all(decision is not None for decision in cached_decisions.values()):
                for unit_input in requested:
                    cached_decision = cached_decisions[unit_input.unit_index]
                    assert cached_decision is not None
                    cached_decision = self._canonicalize_decision(
                        unit_input, cached_decision
                    )
                    self._validate_decision(unit_input, cached_decision)
                    receipts[unit_input.unit_index] = self._model_receipt(
                        unit_input=unit_input,
                        decision=cached_decision,
                        cache_key=group_cache_keys[unit_input.unit_index],
                        cache_hit=True,
                    )
                continue

            # A batch is one model input.  A partial cache hit must therefore
            # rerun the complete fixed group rather than silently change the
            # neighbouring Units that the model sees.
            invalid: list[SemanticRouteUnitInput] = []
            try:
                decision_by_index = self._adjudicate_batch(
                    document=document,
                    requested=requested,
                )
            except SemanticRouteContractError:
                decision_by_index = {}
                invalid.extend(requested)
            else:
                for unit_input in requested:
                    try:
                        self._validate_decision(
                            unit_input,
                            decision_by_index[unit_input.unit_index],
                        )
                    except SemanticRouteContractError:
                        invalid.append(unit_input)
            for unit_input in invalid:
                single_group_hash = _semantic_adjudication_group_hash((unit_input,))
                try:
                    retry = self._adjudicate_batch(
                        document=document,
                        requested=(unit_input,),
                    )[unit_input.unit_index]
                    self._validate_decision(unit_input, retry)
                except SemanticRouteContractError as exc:
                    raise SemanticRouteAdjudicatorError(
                        "semantic adjudicator repeated an invalid decision for Unit "
                        f"{unit_input.unit_index}",
                        reason_code="invalid_decision",
                        retryable=False,
                    ) from exc
                decision_by_index[unit_input.unit_index] = retry
                group_cache_keys[unit_input.unit_index] = self._cache_key(
                    input_hash=unit_input.input_hash,
                    group_hash=single_group_hash,
                )
            for unit_input in requested:
                decision = decision_by_index[unit_input.unit_index]
                cache_key = group_cache_keys[unit_input.unit_index]
                self.cache.put(cache_key, decision)
                receipts[unit_input.unit_index] = self._model_receipt(
                    unit_input=unit_input,
                    decision=decision,
                    cache_key=cache_key,
                    cache_hit=False,
                )

        ordered_receipts = tuple(receipts[draft.unit_index] for draft in drafts)
        routed = tuple(
            _apply_receipt(
                draft,
                receipt,
                section_keys=self._section_keys(
                    document=document,
                    draft=draft,
                    sources=unit_input.sources,
                ),
            )
            for draft, unit_input, receipt in zip(
                drafts,
                inputs,
                ordered_receipts,
                strict=True,
            )
        )
        return SemanticRouteBatchResult(
            units=routed,
            receipts=ordered_receipts,
            adjudication_outcomes=tuple(outcomes),
        )

    def _adjudicate_batch(
        self,
        *,
        document: SemanticDocumentContext,
        requested: tuple[SemanticRouteUnitInput, ...],
    ) -> dict[int, SemanticAdjudicationDecision]:
        assert self.adjudicator is not None
        decisions = self.adjudicator.adjudicate(
            SemanticAdjudicationBatch(
                document=document,
                taxonomy=self.taxonomy,
                units=requested,
            )
        )
        decision_by_index = {decision.unit_index: decision for decision in decisions}
        if len(decision_by_index) != len(decisions) or set(decision_by_index) != {
            unit.unit_index for unit in requested
        }:
            raise SemanticRouteContractError(
                "semantic adjudicator did not cover the exact requested Units"
            )
        requested_by_index = {unit.unit_index: unit for unit in requested}
        return {
            unit_index: self._canonicalize_decision(
                requested_by_index[unit_index],
                decision,
            )
            for unit_index, decision in decision_by_index.items()
        }

    def replay(
        self,
        *,
        admitted: AdmittedProviderDocument,
        document: SemanticDocumentContext,
        drafts: Sequence[ProviderUnitDraft],
        receipts: Sequence[SemanticRouteReceipt],
    ) -> SemanticRouteBatchResult:
        """Validate frozen receipts against fresh source replay without a model call."""

        if len(drafts) != len(receipts):
            raise SemanticRouteContractError("semantic receipt count differs from Units")
        inputs = tuple(
            self._prepare_input(
                admitted=admitted,
                document=document,
                draft=draft,
            )
            for draft in drafts
        )
        model_inputs = tuple(
            unit_input
            for unit_input in inputs
            if self._requires_model(unit_input, document=document)
        )
        model_indices = {unit_input.unit_index for unit_input in model_inputs}
        model_receipt_versions = {
            receipt.contract_version
            for unit_input, receipt in zip(inputs, receipts, strict=True)
            if unit_input.unit_index in model_indices
        }
        expected_model_group_hashes: dict[int, str]
        if len(model_receipt_versions) > 1:
            raise SemanticRouteContractError(
                "semantic replay cannot mix model receipt contract versions"
            )
        if model_receipt_versions == {SEMANTIC_ROUTE_RECEIPT_VERSION}:
            expected_model_group_hashes = _derive_v2_receipt_group_hashes(
                inputs=inputs,
                model_inputs=model_inputs,
                receipts=receipts,
            )
        else:
            expected_model_group_hashes = {}
            for group in _semantic_adjudication_groups(
                model_inputs,
                batch_size=self.batch_size,
            ):
                group_hash = _semantic_adjudication_group_hash(group)
                expected_model_group_hashes.update(
                    (unit_input.unit_index, group_hash) for unit_input in group
                )
        replayed: list[ProviderUnitDraft] = []
        for draft, unit_input, receipt in zip(
            drafts,
            inputs,
            receipts,
            strict=True,
        ):
            self._validate_receipt(
                unit_input,
                receipt,
                document=document,
                expected_model_group_hash=expected_model_group_hashes.get(
                    unit_input.unit_index
                ),
            )
            replayed.append(
                _apply_receipt(
                    draft,
                    receipt,
                    section_keys=self._section_keys(
                        document=document,
                        draft=draft,
                        sources=unit_input.sources,
                    ),
                )
            )
        return SemanticRouteBatchResult(
            units=tuple(replayed),
            receipts=tuple(receipts),
        )

    def _prepare_input(
        self,
        *,
        admitted: AdmittedProviderDocument,
        document: SemanticDocumentContext,
        draft: ProviderUnitDraft,
    ) -> SemanticRouteUnitInput:
        sources = _unit_sources(admitted=admitted, document=document, draft=draft)
        section_keys = self._section_keys(
            document=document,
            draft=draft,
            sources=sources,
        ) or ()
        candidates = (
            self._candidates(
                document=document,
                unit_index=draft.unit_index,
                sources=sources,
                section_keys=section_keys,
            )
            if (
                _draft_has_answer_content(draft)
                and not _sources_are_only_statutory_disclosure_boilerplate(sources)
                and not _sources_are_only_page_carryover_metadata(sources)
                and not _sources_are_only_cross_reference_carriers(sources)
            )
            else ()
        )
        input_hash = _semantic_input_hash(
            taxonomy=self.taxonomy,
            document=document,
            unit_index=draft.unit_index,
            sources=sources,
            candidates=candidates,
        )
        return SemanticRouteUnitInput(
            unit_index=draft.unit_index,
            input_hash=input_hash,
            sources=sources,
            candidates=candidates,
        )

    def _candidates(
        self,
        *,
        document: SemanticDocumentContext,
        unit_index: int,
        sources: tuple[SemanticRouteSource, ...],
        section_keys: tuple[str, ...],
    ) -> tuple[SemanticRouteCandidate, ...]:
        state: dict[str, _CandidateState] = {
            key: _CandidateState(key=key) for key in self._definitions
        }
        classification_scopes = {
            value
            for value in (
                document.filing_type,
                *document.disclosure_topics,
            )
            if value
        }
        has_authoritative_scope = any(
            tag != "other" for tag in classification_scopes
        )
        exact_scoped_matches: dict[str, set[str]] = {}
        exact_context_matches: dict[str, set[str]] = {}
        contains_scoped_matches: dict[str, set[str]] = {}
        has_unit_content = any(
            source.kind in {"body_text", "table_text"} for source in sources
        )
        unit_titles = tuple(
            _normalize_title(source.text)
            for source in sources
            if source.kind == "unit_title"
        )
        role_responsibility_unit = any(
            title.endswith("职责") or title.endswith("职权")
            for title in unit_titles
        )
        interest_bearing_debt_structure = (
            "有息负债和结构" in unit_titles
            and any(
                source.kind.startswith("table_") and bool(re.search(r"\d", source.text))
                for source in sources
            )
        )
        materiality_criteria_unit = _sources_define_materiality_criteria(sources)
        if (
            document.filing_type == "performance_forecast"
            and "performance_forecast_range" in state
        ):
            for source_id in _forecast_range_table_support_ids(sources):
                state["performance_forecast_range"].add(
                    score=1200,
                    source_id=source_id,
                    evidence_kind="source_quantitative_topic",
                    locked=True,
                )
        if (
            "equity_incentive" in classification_scopes
            and "incentive_adjustment" in state
        ):
            for source_id in _incentive_adjustment_support_ids(sources):
                state["incentive_adjustment"].add(
                    score=1200,
                    source_id=source_id,
                    evidence_kind="source_labeled_field_exact",
                    locked=True,
                )
        if "transaction_pricing_basis" in state:
            for source_id in _transaction_pricing_support_ids(sources):
                state["transaction_pricing_basis"].add(
                    score=1200,
                    source_id=source_id,
                    evidence_kind="source_labeled_field_exact",
                    locked=True,
                )
        if (
            document.filing_type == "major_contract"
            and "contract_value" in state
        ):
            for source_id in _major_contract_value_support_ids(sources):
                state["contract_value"].add(
                    score=1200,
                    source_id=source_id,
                    evidence_kind="source_quantitative_topic",
                    locked=True,
                )
        for source in sources:
            if source.kind != "unit_title" or not has_unit_content:
                continue
            direct_keys = self._direct_composites.get(_normalize_title(source.text))
            if direct_keys is None:
                continue
            for key in direct_keys:
                definition = self._definitions[key]
                if definition.scopes and not (
                    set(definition.scopes) & classification_scopes
                ):
                    continue
                state[key].add(
                    score=1300,
                    source_id=source.source_id,
                    evidence_kind="source_heading_exact",
                    locked=True,
                )
        if document.filing_type == "risk_alert":
            for source in sources:
                if source.kind == "unit_title":
                    normalized_title = _normalize_match_text(source.text)
                    if (
                        has_unit_content
                        and "计提" in normalized_title
                        and "信用减值损失" in normalized_title
                        and "资产减值损失" in normalized_title
                    ):
                        for key in ("credit_impairment_loss", "asset_impairment_loss"):
                            state[key].add(
                                score=1200,
                                source_id=source.source_id,
                                evidence_kind="source_heading_exact",
                                locked=True,
                            )
                    continue
                if source.kind not in _LOCAL_CONTENT_SOURCE_KINDS:
                    continue
                normalized = _normalize_match_text(source.text)
                if _clause_is_reference(normalized):
                    continue
                if (
                    "信用减值损失" in normalized
                    and "资产减值损失" in normalized
                    and any(marker in normalized for marker in ("计提", "转回"))
                    and re.search(r"\d", normalized)
                ):
                    for key in ("credit_impairment_loss", "asset_impairment_loss"):
                        state[key].add(
                            score=1200,
                            source_id=source.source_id,
                            evidence_kind="source_labeled_field_exact",
                            locked=True,
                        )
                if _is_accounting_change_event_fact(source):
                    state["accounting_changes"].add(
                        score=1200,
                        source_id=source.source_id,
                        evidence_kind="source_labeled_field_exact",
                        locked=True,
                    )
        profile_support = _guaranteed_party_profile_support_ids(sources)
        if profile_support and "guaranteed_party_profile" in section_keys:
            for source_id in profile_support:
                state["guaranteed_party_profile"].add(
                    score=1100,
                    source_id=source_id,
                    evidence_kind="source_labeled_field_exact",
                    locked=True,
                )
        terms_support = _guarantee_agreement_terms_support_ids(sources)
        if terms_support and "guarantee_agreement_terms" in section_keys:
            for source_id in terms_support:
                state["guarantee_agreement_terms"].add(
                    score=1100,
                    source_id=source_id,
                    evidence_kind="source_labeled_field_exact",
                    locked=True,
                )
        progress_support = _guarantee_progress_support_ids(sources)
        for source_id in progress_support:
            state["guarantee_progress"].add(
                score=1100,
                source_id=source_id,
                evidence_kind="source_labeled_field_exact",
                locked=True,
            )
        acquisition_definition = self._definitions.get(
            "major_asset_acquisition_progress"
        )
        acquisition_scope_matches = (
            acquisition_definition is not None
            and (
                not acquisition_definition.scopes
                or bool(
                    set(acquisition_definition.scopes) & classification_scopes
                )
            )
        )
        if acquisition_scope_matches:
            acquisition_support = _major_asset_acquisition_progress_support_ids(
                sources
            )
            for source_id in acquisition_support:
                state["major_asset_acquisition_progress"].add(
                    score=1100,
                    source_id=source_id,
                    evidence_kind="source_labeled_field_exact",
                    locked=True,
                )
        bank_quality_support = _bank_loan_quality_support_ids(sources)
        if bank_quality_support:
            for source_id in bank_quality_support:
                state["bank_loan_quality"].add(
                    score=1100,
                    source_id=source_id,
                    evidence_kind="source_labeled_field_exact",
                    locked=True,
                )
        for key, source_ids in _bank_balance_support_ids(sources).items():
            for source_id in source_ids:
                state[key].add(
                    score=1100,
                    source_id=source_id,
                    evidence_kind="source_quantitative_topic",
                    locked=True,
                )
        for source_id in _market_value_management_support_ids(sources):
            state["market_value_management"].add(
                score=1100,
                source_id=source_id,
                evidence_kind="source_labeled_field_exact",
                locked=True,
            )
        for source_id in _structured_entity_selector_support_ids(sources):
            state["structured_entities"].add(
                score=1100,
                source_id=source_id,
                evidence_kind="source_labeled_field_exact",
                locked=True,
            )
        business_risk_state = state.get("business_risk")
        business_risk_definition = self._definitions.get("business_risk")
        if (
            business_risk_state is not None
            and business_risk_definition is not None
            and has_unit_content
            and "accounting_policies" not in section_keys
            and (
                not business_risk_definition.scopes
                or bool(
                    set(business_risk_definition.scopes)
                    & classification_scopes
                )
            )
        ):
            for source in sources:
                if source.kind != "unit_title":
                    continue
                title_core = _normalize_title(source.text)
                if "风险" in title_core:
                    business_risk_state.add(
                        score=1200,
                        source_id=source.source_id,
                        evidence_kind="source_heading_risk_topic",
                        locked=True,
                    )
        risk_state = state.get("transaction_risk")
        if document.filing_type == "restructuring_assets" and risk_state is not None:
            for source in sources:
                if source.kind != "unit_title":
                    continue
                title_core = _normalize_title(source.text)
                if len(title_core) > len("风险") and title_core.endswith("风险"):
                    risk_state.add(
                        score=1200,
                        source_id=source.source_id,
                        evidence_kind="source_heading_risk_suffix",
                        locked=True,
                    )
        if document.filing_type == "correction_supplement":
            correction_state = state.get("correction_overview")
            shareholder_state = state.get("shareholder_change")
            for source in sources:
                if source.kind != "unit_title":
                    continue
                title_core = _normalize_title(source.text)
                if not _is_correction_overview_title(title_core):
                    continue
                if correction_state is not None:
                    correction_state.add(
                        score=1200,
                        source_id=source.source_id,
                        evidence_kind="source_heading_candidate",
                        locked=True,
                    )
                if (
                    shareholder_state is not None
                    and _is_shareholder_change_correction_title(title_core)
                ):
                    shareholder_state.add(
                        score=1100,
                        source_id=source.source_id,
                        evidence_kind="source_heading_candidate",
                        locked=True,
                    )
        if document.filing_type == "convertible_bond":
            tax_state = state.get("bond_interest_tax")
            if tax_state is not None:
                for source in sources:
                    if _is_bond_interest_tax_fact(source):
                        tax_state.add(
                            score=900,
                            source_id=source.source_id,
                            evidence_kind="source_labeled_field_exact",
                            locked=True,
                        )
        if document.filing_type == "equity_incentive":
            assessment_state = state.get("incentive_performance_assessment")
            cancellation_state = state.get("incentive_cancellation")
            for source in sources:
                if (
                    assessment_state is not None
                    and _is_incentive_assessment_result(source)
                ):
                    assessment_state.add(
                        score=900,
                        source_id=source.source_id,
                        evidence_kind="source_labeled_field_exact",
                        locked=True,
                    )
                if (
                    cancellation_state is not None
                    and _is_incentive_cancellation_result(source)
                ):
                    cancellation_state.add(
                        score=900,
                        source_id=source.source_id,
                        evidence_kind="source_labeled_field_exact",
                        locked=True,
                    )
        if document.filing_type == "share_buyback":
            purpose_state = state.get("share_buyback_purpose")
            if purpose_state is not None:
                for source in sources:
                    if _is_share_buyback_purpose_fact(source):
                        purpose_state.add(
                            score=900,
                            source_id=source.source_id,
                            evidence_kind="source_labeled_field_exact",
                            locked=True,
                        )
        definitions_context = self._definitions.get("definitions")
        is_definitions_context = (
            definitions_context is not None
            and any(
                source.kind in {"unit_title", "heading_path"}
                and any(
                    _title_is_exact_field(
                        _normalize_title(source.text),
                        normalized_label,
                    )
                    for normalized_label in self._normalized_title_labels["definitions"]
                )
                for source in sources
            )
        )
        for source in sources:
            if source.kind != "unit_title":
                continue
            title_core = _normalize_title(source.text)
            exact_scoped_matches[source.source_id] = {
                definition.key
                for definition in self.taxonomy.definitions
                if (
                    not definition.context_container
                    and
                    any(
                        _title_is_exact_field(title_core, label)
                        for label in self._normalized_title_labels[definition.key]
                    )
                    and (
                        not definition.scopes
                        or bool(set(definition.scopes) & classification_scopes)
                    )
                )
            }
            exact_context_matches[source.source_id] = {
                definition.key
                for definition in self.taxonomy.definitions
                if (
                    definition.context_container
                    and any(
                        _title_is_exact_field(title_core, label)
                        for label in self._normalized_title_labels[definition.key]
                    )
                    and (
                        not definition.scopes
                        or bool(set(definition.scopes) & classification_scopes)
                    )
                )
            }
            contains_scoped_matches[source.source_id] = {
                definition.key
                for definition in self.taxonomy.definitions
                if (
                    any(
                        len(label) >= 4
                        and label in title_core
                        for label in self._normalized_labels[definition.key]
                    )
                    and (
                        not definition.scopes
                        or bool(set(definition.scopes) & classification_scopes)
                    )
                )
            }

        for definition in self.taxonomy.definitions:
            if definition.context_container:
                scope_matches = not definition.scopes or bool(
                    set(definition.scopes) & classification_scopes
                )
                for source in sources:
                    if (
                        source.kind == "unit_title"
                        and has_unit_content
                        and not _sources_have_mechanical_toc(sources)
                        and scope_matches
                        and exact_context_matches.get(source.source_id)
                        == {definition.key}
                    ):
                        # A context label inherited through heading_path is
                        # structural only.  The same exact label on this
                        # content-bearing Unit is also a Unit-local topic: it
                        # names what this carrier actually contains rather
                        # than copying an ancestor onto its descendants.
                        state[definition.key].add(
                            score=1000,
                            source_id=source.source_id,
                            evidence_kind="source_heading_exact",
                            locked=True,
                        )
                continue
            if (
                definition.key == self.taxonomy.fallback_key
                or (
                    is_definitions_context
                    and definition.key != "definitions"
                )
            ):
                continue
            if (
                definition.key == "guaranteed_party_profile"
                and not profile_support
            ) or (
                definition.key == "guarantee_agreement_terms"
                and not terms_support
            ):
                # A heading plus an empty template is structural context, not
                # an answer-bearing direct route.  These two schemas have
                # closed source-bound value witnesses above.
                continue
            labels = self._normalized_title_labels[definition.key]
            body_labels = set(self._normalized_labels[definition.key])
            heading_only_labels = set(labels) - body_labels
            definition_state = state[definition.key]
            scope_matches = not definition.scopes or bool(
                set(definition.scopes) & classification_scopes
            )
            for source_index, source in enumerate(sources):
                normalized = _normalize_match_text(source.text)
                title_core = _normalize_title(source.text)
                for label in labels:
                    if not label:
                        continue
                    if (
                        source.kind == "unit_title"
                        and _title_is_exact_field(title_core, label)
                        and scope_matches
                    ):
                        definition_state.add(
                            score=1000,
                            source_id=source.source_id,
                            evidence_kind="source_heading_exact",
                            locked=(
                                exact_scoped_matches[source.source_id]
                                == {definition.key}
                            ),
                        )
                    elif (
                        source.kind == "heading_path"
                        and definition.key in section_keys
                        and _title_is_exact_field(title_core, label)
                    ):
                        # An accepted heading-path occurrence proves structural
                        # position, not a direct Unit topic by itself.  Keep it
                        # below the candidate threshold; a later Unit-local
                        # body/table witness may corroborate the same closed key.
                        definition_state.add(
                            score=0,
                            source_id=source.source_id,
                            evidence_kind="source_section_exact",
                        )
                    elif (
                        source.kind == "unit_title"
                        and _title_is_exact_field(title_core, label)
                        and not exact_scoped_matches[source.source_id]
                        and not has_authoritative_scope
                    ):
                        definition_state.add(
                            score=700,
                            source_id=source.source_id,
                            evidence_kind="source_heading_exact",
                        )
                    elif (
                        source.kind == "unit_title"
                        and (
                            not exact_context_matches[source.source_id]
                            or all(
                                (context_key, definition.key)
                                in _INTENTIONAL_CONTEXT_DIRECT_OVERLAPS
                                for context_key in exact_context_matches[
                                    source.source_id
                                ]
                            )
                        )
                        and label in body_labels
                        and min(len(title_core), len(label)) >= 4
                        and (label in title_core or title_core in label)
                        and not title_core.endswith(
                            ("管理制度", "管理办法", "工作制度")
                        )
                        and (
                            scope_matches
                            or (
                                not has_authoritative_scope
                                and not contains_scoped_matches[source.source_id]
                            )
                        )
                    ):
                        definition_state.add(
                            score=650,
                            source_id=source.source_id,
                            evidence_kind="source_heading_candidate",
                            locked=(
                                not definition.overview_container
                                and (unit_index == 0 or has_unit_content)
                                and {
                                    key
                                    for key in contains_scoped_matches[source.source_id]
                                    if not self._definitions[key].overview_container
                                }
                                == {definition.key}
                            ),
                        )
                    elif (
                        source.kind
                        in {
                            "body_text",
                            "table_text",
                            "table_field_label",
                            "table_column_header",
                        }
                        and not is_definitions_context
                        and scope_matches
                        and label in heading_only_labels
                        and _is_labeled_field_fact(
                            definition=definition,
                            normalized_label=label,
                            source=source,
                            following_sources=sources[source_index + 1 :],
                        )
                    ):
                        # Some stable provider fields are useful both as exact
                        # headings and as numbered ``label: value`` rows inside
                        # an overview Unit.  Keep them out of ordinary body
                        # contains matching so nearby prose such as a policy or
                        # management rule cannot manufacture the route.
                        definition_state.add(
                            score=900,
                            source_id=source.source_id,
                            evidence_kind="source_labeled_field_exact",
                            locked=True,
                        )
                    elif (
                        source.kind
                        in {
                            "body_text",
                            "table_text",
                            "table_field_label",
                            "table_column_header",
                        }
                        and not is_definitions_context
                        and scope_matches
                        and label in body_labels
                        and not _is_temporal_trigger_reference(
                            source=source,
                            normalized_label=label,
                        )
                        and not _body_candidate_is_context_only(
                            key=definition.key,
                            normalized_label=label,
                            source=source,
                            role_responsibility_unit=role_responsibility_unit,
                        )
                        and (
                            (len(label) >= 4 and label in normalized)
                            or normalized == label
                            or (
                                len(label) >= 3
                                and normalized.startswith(f"{label}:")
                            )
                        )
                    ):
                        quantitative_topic = _is_standardized_quantitative_topic(
                            definition=definition,
                            normalized_label=label,
                            source=source,
                            longer_labels=tuple(
                                candidate_label.rstrip(":")
                                for candidate_definition in self.taxonomy.definitions
                                if (
                                    candidate_definition.key != definition.key
                                    and candidate_definition.quantitative_topic
                                    and (
                                        not candidate_definition.scopes
                                        or bool(
                                            set(candidate_definition.scopes)
                                            & classification_scopes
                                        )
                                    )
                                )
                                for candidate_label in self._normalized_labels[
                                    candidate_definition.key
                                ]
                                if (
                                    len(candidate_label.rstrip(":"))
                                    > len(label.rstrip(":"))
                                    and label.rstrip(":")
                                    in candidate_label.rstrip(":")
                                )
                            ),
                        )
                        resolved_proposal_exact = _is_resolved_proposal_fact(
                            definition=definition,
                            normalized_label=label,
                            source=source,
                        )
                        labeled_field_exact = _is_labeled_field_fact(
                            definition=definition,
                            normalized_label=label,
                            source=source,
                            following_sources=sources[source_index + 1 :],
                        )
                        accounting_relation_exact = (
                            "accounting_policies" in section_keys
                            and _is_accounting_policy_relation_fact(
                                key=definition.key,
                                normalized_label=label,
                                source=source,
                            )
                        )
                        key_audit_procedure_exact = (
                            definition.key == "key_audit_matters"
                            and _is_key_audit_matter_procedure(source)
                        )
                        debt_structure_exact = (
                            interest_bearing_debt_structure
                            and definition.key in _INTEREST_BEARING_DEBT_KEYS
                            and source.kind.startswith("table_")
                            and _is_closed_debt_structure_cell(source)
                        )
                        definition_state.add(
                            score=(
                                900
                                if (
                                    quantitative_topic
                                    or resolved_proposal_exact
                                    or labeled_field_exact
                                    or accounting_relation_exact
                                    or key_audit_procedure_exact
                                    or debt_structure_exact
                                )
                                else 300 + min(len(label), 40)
                            ),
                            source_id=source.source_id,
                            evidence_kind=(
                                "source_resolved_proposal_exact"
                                if resolved_proposal_exact
                                else (
                                    "source_labeled_field_exact"
                                    if (
                                        labeled_field_exact
                                        or accounting_relation_exact
                                        or key_audit_procedure_exact
                                    )
                                    else (
                                        "source_quantitative_topic"
                                        if quantitative_topic
                                        else (
                                            "source_table_candidate"
                                            if source.kind.startswith("table_")
                                            else "source_body_candidate"
                                        )
                                    )
                                )
                            ),
                            locked=(
                                quantitative_topic
                                or resolved_proposal_exact
                                or labeled_field_exact
                                or accounting_relation_exact
                                or key_audit_procedure_exact
                                or debt_structure_exact
                            ),
                        )
                    elif (
                        source.kind.startswith("document_")
                        and scope_matches
                        and label in normalized
                    ):
                        definition_state.add(
                            score=80 + min(len(label), 20),
                            source_id=source.source_id,
                            evidence_kind="document_context_candidate",
                        )
                if (
                    source.kind == "unit_title"
                    and scope_matches
                    and not exact_scoped_matches[source.source_id]
                    and not any(
                        min(len(title_core), len(label)) >= 4
                        and (label in title_core or title_core in label)
                        for label in self._normalized_labels[definition.key]
                    )
                ):
                    similarity = max(
                        _title_similarity(title_core, surface)
                        for surface in (
                            *self._normalized_labels[definition.key],
                            _normalize_title(definition.description),
                        )
                    )
                    threshold = (
                        _MIN_SCOPED_TITLE_SIMILARITY
                        if (
                            definition.scopes
                            and document.filing_type not in _PERIODIC_FILING_TYPES
                        )
                        else _MIN_TITLE_SIMILARITY
                    )
                    if similarity >= threshold:
                        # Similarity is recall-only.  It never locks a route;
                        # the closed-vocabulary adjudicator must still select
                        # or abstain.  This keeps paraphrased headings visible
                        # without turning one observed phrase into a rule.
                        definition_state.add(
                            score=500 + int(similarity * 100),
                            source_id=source.source_id,
                            evidence_kind="source_heading_similarity",
                        )

        for definition in self.taxonomy.definitions:
            definition_state = state[definition.key]
            evidence = set(definition_state.evidence_kinds or ())
            if (
                not definition.overview_container
                and evidence
                & {
                    "source_heading_candidate",
                    "source_section_exact",
                }
                and evidence
                & {
                    "source_body_candidate",
                    "source_table_candidate",
                    "source_labeled_field_exact",
                    "source_quantitative_topic",
                    "source_resolved_proposal_exact",
                }
            ):
                # A controlled scoped heading corroborated by the same Unit's
                # body/table is a direct topic.  This may legitimately lock
                # more than one route when one heading explicitly names two
                # topics; title-only cover fragments remain abstentions.
                definition_state.locked = True

        source_kinds = {source.source_id: source.kind for source in sources}
        populated = [
            item
            for item in state.values()
            if (item.locked or item.score >= _MIN_CANDIDATE_SCORE)
            and not materiality_criteria_unit
            and any(
                source_kinds[source_id]
                in {
                    "unit_title",
                    "heading_path",
                    "body_text",
                    "table_text",
                    "table_field_label",
                    "table_column_header",
                }
                for source_id in (item.source_ids or ())
            )
        ]
        populated = [
            item
            for item in populated
            if (
                not self._definitions[item.key].exclusive_container
                or (
                    item.evidence_kinds is not None
                    and "source_heading_exact" in item.evidence_kinds
                )
            )
        ]
        locked_role_anchors = {
            item.key
            for item in populated
            if (
                item.locked
                and self._definitions[item.key].role_anchor
                and item.evidence_kinds is not None
                and "source_heading_exact" in item.evidence_kinds
            )
        }
        if locked_role_anchors:
            # Exact disclosure roles (for example forecast comparison versus
            # forecast basis) are mutually source-bound.  A number inside one
            # role cannot manufacture a different role anchor.  Preserve a
            # second role only when it carries its own exact heading or typed
            # field witness; otherwise the first role would incorrectly veto
            # an independently visible source field.
            populated = [
                item
                for item in populated
                if (
                    not self._definitions[item.key].role_anchor
                    or item.key in locked_role_anchors
                    or (
                        item.evidence_kinds is not None
                        and bool(
                            {
                                "source_heading_exact",
                                "source_labeled_field_exact",
                            }
                            & set(item.evidence_kinds)
                        )
                    )
                )
            ]
        overview_keys = {
            item.key
            for item in populated
            if self._definitions[item.key].overview_container
        }
        if overview_keys and not self._sources_mark_overview(
            sources,
            overview_keys=overview_keys,
        ):
            # A broad scheme/report/container label inside a concrete child
            # title is context, not a competing Unit route.  Keeping it in the
            # shortlist made table words such as “标的资产” look like a model
            # ambiguity and produced narrower but false routes.  True overview
            # titles and standard overview markers remain eligible.
            populated = [
                item
                for item in populated
                if not self._definitions[item.key].overview_container
            ]
        if document.filing_type in _PERIODIC_FILING_TYPES:
            exact_periodic = [
                item
                for item in populated
                if item.locked
                and item.evidence_kinds
                and "source_heading_exact" in item.evidence_kinds
            ]
            if exact_periodic:
                # A unique exact periodic heading is the route.  Dense notes
                # often contain broader/narrower title phrases, but those are
                # not independent Unit topics and must not force a model call.
                populated = [item for item in populated if item.locked]
        locked_exclusive = [
            item
            for item in populated
            if item.locked and self._definitions[item.key].exclusive_container
        ]
        if locked_exclusive:
            # A source-exact mechanical container cannot legally carry a
            # secondary route.  Role anchors are a separate policy and may
            # coexist with independently witnessed Unit topics.
            populated = locked_exclusive
        # Similarity and document context are recall signals, not stronger
        # evidence than a direct source heading/body/table occurrence.  Keep
        # exact locks first, then direct source evidence, and only then
        # similarity-only candidates before applying the bounded shortlist.
        populated.sort(
            key=lambda item: (
                not item.locked,
                _candidate_is_similarity_only(item),
                -item.score,
                item.key,
            )
        )
        selected = populated[:MAX_SEMANTIC_CANDIDATES]
        locked_count = sum(1 for item in populated if item.locked)
        if locked_count > MAX_SEMANTIC_CANDIDATES:
            raise SemanticRouteContractError(
                "exact semantic title produces too many locked routes"
            )
        return tuple(
            SemanticRouteCandidate(
                key=item.key,
                source_ids=tuple(item.source_ids or ()),
                evidence_kinds=tuple(item.evidence_kinds or ()),
                locked=item.locked,
            )
            for item in selected
        )

    def _section_keys(
        self,
        *,
        document: SemanticDocumentContext,
        draft: ProviderUnitDraft,
        sources: tuple[SemanticRouteSource, ...],
    ) -> tuple[str, ...] | None:
        """Derive normalized structural position without model adjudication."""

        classification_scopes = {
            value
            for value in (document.filing_type, *document.disclosure_topics)
            if value
        }
        keys: list[str] = []
        for heading in draft.heading_path:
            title_core = _normalize_title(heading)
            composite = self._composite_sections.get(title_core)
            if composite is None:
                composite = self._direct_composites.get(title_core)
            if composite is not None:
                for key in composite:
                    definition = self._definitions[key]
                    if definition.scopes and not (
                        set(definition.scopes) & classification_scopes
                    ):
                        continue
                    if key not in keys:
                        keys.append(key)
                continue
            matching = {
                definition.key
                for definition in self.taxonomy.definitions
                if (
                    (
                        not definition.scopes
                        or bool(set(definition.scopes) & classification_scopes)
                    )
                    and any(
                        _title_is_exact_field(title_core, label)
                        for label in self._normalized_title_labels[definition.key]
                    )
                )
            }
            if len(matching) > 1:
                # An accepted source heading is still ambiguous when the
                # closed catalog maps the same label to several meanings.
                # Only an explicit composite above may project several keys;
                # otherwise preserve heading_path and abstain from normalized
                # structural labels instead of inventing one or failing Build.
                continue
            if matching:
                key = next(iter(matching))
                if key not in keys:
                    keys.append(key)
        return tuple(keys) or None

    def _validate_decision(
        self,
        unit_input: SemanticRouteUnitInput,
        decision: SemanticAdjudicationDecision,
    ) -> None:
        if decision.unit_index != unit_input.unit_index:
            raise SemanticRouteContractError("semantic decision Unit differs from request")
        candidate_keys = {candidate.key for candidate in unit_input.candidates}
        selected = tuple(route.key for route in decision.routes)
        if not set(selected).issubset(candidate_keys):
            raise SemanticRouteContractError(
                f"semantic model selected a non-candidate key for Unit "
                f"{unit_input.unit_index}"
            )
        locked = tuple(candidate.key for candidate in unit_input.candidates if candidate.locked)
        if not set(locked).issubset(selected):
            raise SemanticRouteContractError(
                "semantic model removed an exact title route"
            )
        sources = {source.source_id: source for source in unit_input.sources}
        candidates = {candidate.key: candidate for candidate in unit_input.candidates}
        for route in decision.routes:
            if not set(route.support_ids).issubset(sources):
                raise SemanticRouteContractError(
                    f"semantic model cited an unknown source for Unit "
                    f"{unit_input.unit_index} route {route.key}"
                )
            if not any(
                sources[source_id].kind
                in {
                    "unit_title",
                    "heading_path",
                    "body_text",
                    "table_text",
                    "table_field_label",
                    "table_column_header",
                }
                for source_id in route.support_ids
            ):
                raise SemanticRouteContractError(
                    "document or ancestor context alone cannot support a Unit route"
                )
            if not set(route.support_ids).issubset(candidates[route.key].source_ids):
                raise SemanticRouteContractError(
                    "semantic model cited a source that did not generate its candidate"
                )
            if (
                self._definitions[route.key].exclusive_container
                and "source_heading_exact" not in candidates[route.key].evidence_kinds
            ):
                raise SemanticRouteContractError(
                    "exclusive semantic container lacks an exact source heading"
                )
        if len(decision.routes) > 1 and any(
            self._definitions[route.key].exclusive_container
            for route in decision.routes
        ):
            raise SemanticRouteContractError(
                "multiple exclusive semantic containers cannot coexist"
            )
        if (
            any(
                self._definitions[route.key].overview_container
                for route in decision.routes
            )
            and not self._is_overview_unit(unit_input)
        ):
            raise SemanticRouteContractError(
                "specific semantic Unit cannot retain an overview container"
            )
        if any(
            _similarity_only_candidate(candidates[route.key])
            for route in decision.routes[1:]
        ):
            raise SemanticRouteContractError(
                "semantic secondary lacks direct title/body/table evidence"
            )

    def _canonicalize_decision(
        self,
        unit_input: SemanticRouteUnitInput,
        decision: SemanticAdjudicationDecision,
    ) -> SemanticAdjudicationDecision:
        """Turn model membership into one stable canonical route order."""

        if self._is_overview_unit(unit_input):
            canonical = decision
        else:
            direct_routes = tuple(
                route
                for route in decision.routes
                if (
                    (definition := self._definitions.get(route.key)) is None
                    or not definition.overview_container
                )
            )
            if len(direct_routes) == len(decision.routes):
                canonical = decision
            else:
                canonical = replace(decision, routes=direct_routes)
        candidates = {candidate.key: candidate for candidate in unit_input.candidates}
        selected_by_key = {route.key: route for route in canonical.routes}
        if not set(selected_by_key).issubset(candidates):
            # Validation owns the controlled non-candidate error.  Do not let
            # canonical ordering turn model input into a raw KeyError first.
            return canonical
        exclusive_keys = tuple(
            key
            for key in selected_by_key
            if self._definitions[key].exclusive_container
        )
        if len(exclusive_keys) == 1:
            # A whole-statement/table container owns the Unit.  Row labels in
            # its payload remain lexically searchable but cannot become an
            # arbitrary capped subset of semantic secondaries.
            key = exclusive_keys[0]
            return replace(canonical, routes=(selected_by_key[key],))
        for candidate in unit_input.candidates:
            if candidate.locked and candidate.key not in selected_by_key:
                # Exact Unit-local source evidence is a deterministic fact.
                # The model judges only the remaining ambiguous membership and
                # cannot erase a route already established by that evidence.
                selected_by_key[candidate.key] = SemanticAdjudicatedRoute(
                    key=candidate.key,
                    support_ids=candidate.source_ids,
                )
        overview_anchor = self._direct_overview_anchor(unit_input)
        if overview_anchor is not None and overview_anchor.key not in selected_by_key:
            # A true overview Unit is the most useful coarse retrieval entry for
            # L2.  Once the provider title marks the Unit as an overview and the
            # sole container also has Unit-local heading/body/table evidence,
            # retaining that container is deterministic carrier routing rather
            # than an investment-semantic model judgement.
            selected_by_key[overview_anchor.key] = SemanticAdjudicatedRoute(
                key=overview_anchor.key,
                support_ids=overview_anchor.source_ids,
            )
        candidate_order = {
            candidate.key: index
            for index, candidate in enumerate(unit_input.candidates)
        }
        ordered_keys: list[str] = []
        # An exact source title is the strongest scalar-route evidence.  If
        # there is no exact title, a provider-marked overview Unit keeps its
        # single overview carrier ahead of the individual fields that its
        # table may also prove.  This makes ``semantic_key`` describe the Unit
        # while ``semantic_keys`` retains every directly supported field.
        ordered_keys.extend(
            candidate.key
            for candidate in unit_input.candidates
            if (
                candidate.locked
                and candidate.key in selected_by_key
                and "source_heading_exact" in candidate.evidence_kinds
            )
        )
        if overview_anchor is not None:
            ordered_keys.extend(
                (overview_anchor.key,)
                if (
                    overview_anchor.key in selected_by_key
                    and overview_anchor.key not in ordered_keys
                )
                else ()
            )
        ordered_keys.extend(
            candidate.key
            for candidate in unit_input.candidates
            if (
                candidate.locked
                and candidate.key in selected_by_key
                and candidate.key not in ordered_keys
            )
        )
        ordered_keys.extend(
            key
            for key in sorted(selected_by_key, key=candidate_order.__getitem__)
            if key not in ordered_keys
        )
        stable_routes = tuple(
            selected_by_key[key]
            for index, key in enumerate(ordered_keys)
            if index == 0 or not _similarity_only_candidate(candidates[key])
        )
        if stable_routes == canonical.routes:
            return canonical
        return replace(canonical, routes=stable_routes)

    def _direct_overview_anchor(
        self,
        unit_input: SemanticRouteUnitInput,
    ) -> SemanticRouteCandidate | None:
        if not self._is_overview_unit(unit_input):
            return None
        title_kinds = {
            "source_heading_exact",
            "source_heading_candidate",
            "source_heading_similarity",
        }
        direct_kinds = {
            *title_kinds,
            "source_body_candidate",
            "source_table_candidate",
        }
        anchors = tuple(
            candidate
            for candidate in unit_input.candidates
            if self._definitions[candidate.key].overview_container
            and bool(set(candidate.evidence_kinds) & direct_kinds)
        )
        title_anchors = tuple(
            candidate
            for candidate in anchors
            if bool(set(candidate.evidence_kinds) & title_kinds)
        )
        if len(title_anchors) == 1:
            return title_anchors[0]
        return anchors[0] if len(anchors) == 1 else None

    def _is_overview_unit(self, unit_input: SemanticRouteUnitInput) -> bool:
        overview_candidates = {
            candidate.key
            for candidate in unit_input.candidates
            if self._definitions[candidate.key].overview_container
        }
        return self._sources_mark_overview(
            unit_input.sources,
            overview_keys=overview_candidates,
        )

    def _sources_mark_overview(
        self,
        sources: Sequence[SemanticRouteSource],
        *,
        overview_keys: set[str],
    ) -> bool:
        if not overview_keys:
            return False
        for source in sources:
            if source.kind != "unit_title":
                continue
            title = _normalize_title(source.text)
            if any(marker in title for marker in _OVERVIEW_TITLE_MARKERS):
                return True
            if (
                "correction_overview" in overview_keys
                and _is_correction_overview_title(title)
            ):
                return True
            if any(
                _title_is_exact_field(title, label)
                for key in overview_keys
                for label in self._normalized_title_labels[key]
            ):
                return True
        return False

    def _validate_receipt(
        self,
        unit_input: SemanticRouteUnitInput,
        receipt: SemanticRouteReceipt,
        *,
        document: SemanticDocumentContext,
        expected_model_group_hash: str | None,
    ) -> None:
        if receipt.router_version != SEMANTIC_ROUTER_VERSION:
            raise SemanticRouteContractError("semantic receipt router is stale")
        if receipt.taxonomy_version != self.taxonomy.version:
            raise SemanticRouteContractError("semantic receipt taxonomy is stale")
        if receipt.input_hash != unit_input.input_hash:
            raise SemanticRouteContractError("semantic receipt source input drifted")
        if receipt.candidate_keys != tuple(
            candidate.key for candidate in unit_input.candidates
        ):
            raise SemanticRouteContractError("semantic receipt candidates drifted")
        sources = {source.source_id: source for source in unit_input.sources}
        source_ids = set(sources)
        candidate_keys = {candidate.key for candidate in unit_input.candidates}
        if receipt.decision_source == "model" and not set(
            receipt.semantic_keys
        ).issubset(candidate_keys):
            raise SemanticRouteContractError("semantic receipt has a non-candidate route")
        for evidence in receipt.evidence:
            if not set(evidence.source_ids).issubset(source_ids):
                raise SemanticRouteContractError("semantic receipt source no longer exists")
            if receipt.semantic_keys != (SEMANTIC_FALLBACK_KEY,) and not any(
                sources[source_id].kind
                in {
                    "unit_title",
                    "heading_path",
                    "body_text",
                    "table_text",
                    "table_field_label",
                    "table_column_header",
                }
                for source_id in evidence.source_ids
            ):
                raise SemanticRouteContractError(
                    "semantic receipt route lacks a Unit-local witness"
                )
        locked = tuple(
            candidate.key for candidate in self._deterministic_candidates(unit_input)
        )
        if receipt.decision_source == "deterministic":
            if receipt.semantic_keys != locked:
                raise SemanticRouteContractError(
                    "deterministic semantic receipt no longer matches exact routes"
                )
        elif receipt.decision_source == "fallback":
            if unit_input.candidates:
                raise SemanticRouteContractError(
                    "uncached fallback cannot hide semantic candidates"
                )
        elif receipt.decision_source == "rule_abstain":
            if not unit_input.candidates or self._requires_model(
                unit_input,
                document=document,
            ):
                raise SemanticRouteContractError(
                    "rule abstention no longer matches sparse model admission"
                )
        elif receipt.decision_source == "adjudicator_unavailable_abstain":
            if not unit_input.candidates or not self._requires_model(
                unit_input,
                document=document,
            ):
                raise SemanticRouteContractError(
                    "adjudicator-unavailable abstention no longer matches model admission"
                )
            self._validate_v2_adjudication_group(
                receipt,
                expected_model_group_hash=expected_model_group_hash,
            )
        else:
            if receipt.contract_version == SEMANTIC_ROUTE_RECEIPT_VERSION:
                self._validate_v2_adjudication_group(
                    receipt,
                    expected_model_group_hash=expected_model_group_hash,
                )
                decision = SemanticAdjudicationDecision(
                    unit_index=unit_input.unit_index,
                    routes=(
                        ()
                        if receipt.decision_source == "model_abstain"
                        else tuple(
                            SemanticAdjudicatedRoute(
                                key=evidence.key,
                                support_ids=evidence.source_ids,
                            )
                            for evidence in receipt.evidence
                        )
                    ),
                )
                self._validate_decision(unit_input, decision)
                return
            metadata = receipt.adjudicator
            assert metadata is not None
            if expected_model_group_hash is None:
                raise SemanticRouteContractError(
                    "semantic model receipt no longer belongs to a model group"
                )
            allowed_group_hashes = {
                expected_model_group_hash,
                _semantic_adjudication_group_hash((unit_input,)),
            }
            if metadata.cache_key not in {
                _cache_key_for_identity(
                    input_hash=unit_input.input_hash,
                    group_hash=group_hash,
                    adapter=metadata.adapter,
                    model=metadata.model,
                    prompt_version=metadata.prompt_version,
                )
                for group_hash in allowed_group_hashes
            }:
                raise SemanticRouteContractError(
                    "semantic adjudicator receipt cache identity drifted"
                )
            decision = SemanticAdjudicationDecision(
                unit_index=unit_input.unit_index,
                routes=(
                    ()
                    if receipt.decision_source == "model_abstain"
                    else tuple(
                        SemanticAdjudicatedRoute(
                            key=evidence.key,
                            support_ids=evidence.source_ids,
                        )
                        for evidence in receipt.evidence
                    )
                ),
            )
            self._validate_decision(unit_input, decision)
            if metadata.response_sha256 != _decision_hash(decision):
                raise SemanticRouteContractError(
                    "semantic adjudicator response hash drifted"
                )

    @staticmethod
    def _validate_v2_adjudication_group(
        receipt: SemanticRouteReceipt,
        *,
        expected_model_group_hash: str | None,
    ) -> None:
        adjudication = receipt.adjudication
        if adjudication is None or expected_model_group_hash is None:
            raise SemanticRouteContractError(
                "semantic v2 receipt no longer belongs to a model group"
            )
        if adjudication.policy_version != SEMANTIC_FAILOVER_POLICY_VERSION:
            raise SemanticRouteContractError("semantic failover policy drifted")
        if adjudication.group_hash != expected_model_group_hash:
            raise SemanticRouteContractError("semantic adjudication group drifted")

    def _deterministic_receipt(
        self,
        unit_input: SemanticRouteUnitInput,
        selected: tuple[SemanticRouteCandidate, ...],
    ) -> SemanticRouteReceipt:
        keys = tuple(item.key for item in selected)
        return SemanticRouteReceipt(
            contract_version=self._write_receipt_version,
            taxonomy_version=self.taxonomy.version,
            router_version=SEMANTIC_ROUTER_VERSION,
            input_hash=unit_input.input_hash,
            candidate_keys=tuple(item.key for item in unit_input.candidates),
            semantic_keys=keys,
            decision_source="deterministic",
            evidence=tuple(
                SemanticRouteEvidence(
                    key=item.key,
                    kinds=item.evidence_kinds,
                    source_ids=item.source_ids,
                )
                for item in selected
            ),
        )

    def _deterministic_candidates(
        self,
        unit_input: SemanticRouteUnitInput,
    ) -> tuple[SemanticRouteCandidate, ...]:
        selected = tuple(
            candidate for candidate in unit_input.candidates if candidate.locked
        )
        selected_by_key = {candidate.key: candidate for candidate in selected}
        overview_anchor = self._direct_overview_anchor(unit_input)
        if (
            overview_anchor is not None
            and overview_anchor.key not in selected_by_key
        ):
            selected_by_key[overview_anchor.key] = overview_anchor
        ordered_keys: list[str] = []
        ordered_keys.extend(
            candidate.key
            for candidate in unit_input.candidates
            if (
                candidate.key in selected_by_key
                and "source_heading_exact" in candidate.evidence_kinds
            )
        )
        if overview_anchor is not None and overview_anchor.key not in ordered_keys:
            ordered_keys.append(overview_anchor.key)
        ordered_keys.extend(
            candidate.key
            for candidate in unit_input.candidates
            if (
                candidate.key in selected_by_key
                and candidate.key not in ordered_keys
            )
        )
        return tuple(selected_by_key[key] for key in ordered_keys)

    def _requires_model(
        self,
        unit_input: SemanticRouteUnitInput,
        *,
        document: SemanticDocumentContext,
    ) -> bool:
        return _semantic_input_requires_model(
            unit_input,
            document=document,
        )

    def _fallback_receipt(
        self,
        unit_input: SemanticRouteUnitInput,
    ) -> SemanticRouteReceipt:
        v2 = self._write_receipt_version == SEMANTIC_ROUTE_RECEIPT_VERSION
        return SemanticRouteReceipt(
            contract_version=self._write_receipt_version,
            taxonomy_version=self.taxonomy.version,
            router_version=SEMANTIC_ROUTER_VERSION,
            input_hash=unit_input.input_hash,
            candidate_keys=(),
            semantic_keys=() if v2 else (SEMANTIC_FALLBACK_KEY,),
            decision_source="fallback",
            evidence=() if v2 else (
                SemanticRouteEvidence(
                    key=SEMANTIC_FALLBACK_KEY,
                    kinds=("fallback",),
                    source_ids=(),
                ),
            ),
        )

    def _rule_abstain_receipt(
        self,
        unit_input: SemanticRouteUnitInput,
    ) -> SemanticRouteReceipt:
        v2 = self._write_receipt_version == SEMANTIC_ROUTE_RECEIPT_VERSION
        return SemanticRouteReceipt(
            contract_version=self._write_receipt_version,
            taxonomy_version=self.taxonomy.version,
            router_version=SEMANTIC_ROUTER_VERSION,
            input_hash=unit_input.input_hash,
            candidate_keys=tuple(item.key for item in unit_input.candidates),
            semantic_keys=() if v2 else (SEMANTIC_FALLBACK_KEY,),
            decision_source="rule_abstain",
            evidence=() if v2 else (
                SemanticRouteEvidence(
                    key=SEMANTIC_FALLBACK_KEY,
                    kinds=("fallback",),
                    source_ids=(),
                ),
            ),
        )

    def _adjudicator_unavailable_receipt(
        self,
        unit_input: SemanticRouteUnitInput,
        *,
        outcome: SemanticAdjudicationOutcome,
    ) -> SemanticRouteReceipt:
        """Preserve the Unit while making unavailable enrichment explicit.

        Semantic routing is retrieval enrichment, not source admission.  A
        retryable model outage must therefore abstain instead of dropping the
        whole document.  The distinct receipt source keeps the degradation
        auditable and lets worker/doctor alert without inventing a route.
        """

        return SemanticRouteReceipt(
            contract_version=SEMANTIC_ROUTE_RECEIPT_VERSION,
            taxonomy_version=self.taxonomy.version,
            router_version=SEMANTIC_ROUTER_VERSION,
            input_hash=unit_input.input_hash,
            candidate_keys=tuple(item.key for item in unit_input.candidates),
            semantic_keys=(),
            decision_source="adjudicator_unavailable_abstain",
            evidence=(),
            adjudication=SemanticAdjudicationReceipt(
                policy_version=outcome.policy_version,
                group_hash=outcome.group_hash,
                attempts=outcome.attempts,
                actual_result_attempt=None,
                actual_result_identity=None,
                group_response_sha256=None,
            ),
        )

    def _model_receipt_v2(
        self,
        *,
        unit_input: SemanticRouteUnitInput,
        decision: SemanticAdjudicationDecision,
        outcome: SemanticAdjudicationOutcome,
    ) -> SemanticRouteReceipt:
        if outcome.actual_result_identity is None:
            raise SemanticRouteContractError("semantic model outcome has no identity")
        adjudication = SemanticAdjudicationReceipt(
            policy_version=outcome.policy_version,
            group_hash=outcome.group_hash,
            attempts=outcome.attempts,
            actual_result_attempt=outcome.actual_result_attempt,
            actual_result_identity=outcome.actual_result_identity,
            group_response_sha256=outcome.group_response_sha256,
        )
        if not decision.routes:
            return SemanticRouteReceipt(
                contract_version=SEMANTIC_ROUTE_RECEIPT_VERSION,
                taxonomy_version=self.taxonomy.version,
                router_version=SEMANTIC_ROUTER_VERSION,
                input_hash=unit_input.input_hash,
                candidate_keys=tuple(item.key for item in unit_input.candidates),
                semantic_keys=(),
                decision_source="model_abstain",
                evidence=(),
                adjudication=adjudication,
            )
        candidates = {candidate.key: candidate for candidate in unit_input.candidates}
        return SemanticRouteReceipt(
            contract_version=SEMANTIC_ROUTE_RECEIPT_VERSION,
            taxonomy_version=self.taxonomy.version,
            router_version=SEMANTIC_ROUTER_VERSION,
            input_hash=unit_input.input_hash,
            candidate_keys=tuple(item.key for item in unit_input.candidates),
            semantic_keys=tuple(route.key for route in decision.routes),
            decision_source="model",
            evidence=tuple(
                SemanticRouteEvidence(
                    key=route.key,
                    kinds=tuple(
                        dict.fromkeys(
                            (*candidates[route.key].evidence_kinds, "model_adjudicated")
                        )
                    ),
                    source_ids=route.support_ids,
                )
                for route in decision.routes
            ),
            adjudication=adjudication,
        )

    def _model_receipt(
        self,
        *,
        unit_input: SemanticRouteUnitInput,
        decision: SemanticAdjudicationDecision,
        cache_key: str,
        cache_hit: bool,
    ) -> SemanticRouteReceipt:
        assert self.adjudicator is not None
        identity = self.adjudicator.identity
        if not decision.routes:
            return SemanticRouteReceipt(
                taxonomy_version=self.taxonomy.version,
                router_version=SEMANTIC_ROUTER_VERSION,
                input_hash=unit_input.input_hash,
                candidate_keys=tuple(item.key for item in unit_input.candidates),
                semantic_keys=(SEMANTIC_FALLBACK_KEY,),
                decision_source="model_abstain",
                evidence=(
                    SemanticRouteEvidence(
                        key=SEMANTIC_FALLBACK_KEY,
                        kinds=("model_adjudicated", "fallback"),
                        source_ids=(),
                    ),
                ),
                adjudicator=SemanticAdjudicatorMetadata(
                    adapter=identity.adapter,
                    model=identity.model,
                    prompt_version=identity.prompt_version,
                    cache_key=cache_key,
                    response_sha256=_decision_hash(decision),
                    cache_hit=cache_hit,
                ),
            )
        candidates = {candidate.key: candidate for candidate in unit_input.candidates}
        evidence = tuple(
            SemanticRouteEvidence(
                key=route.key,
                kinds=tuple(
                    dict.fromkeys(
                        (*candidates[route.key].evidence_kinds, "model_adjudicated")
                    )
                ),
                source_ids=route.support_ids,
            )
            for route in decision.routes
        )
        response_sha = _decision_hash(decision)
        return SemanticRouteReceipt(
            taxonomy_version=self.taxonomy.version,
            router_version=SEMANTIC_ROUTER_VERSION,
            input_hash=unit_input.input_hash,
            candidate_keys=tuple(item.key for item in unit_input.candidates),
            semantic_keys=tuple(route.key for route in decision.routes),
            decision_source="model",
            evidence=evidence,
            adjudicator=SemanticAdjudicatorMetadata(
                adapter=identity.adapter,
                model=identity.model,
                prompt_version=identity.prompt_version,
                cache_key=cache_key,
                response_sha256=response_sha,
                cache_hit=cache_hit,
            ),
        )

    def _cache_key(self, *, input_hash: str, group_hash: str) -> str:
        assert self.adjudicator is not None
        identity = self.adjudicator.identity
        return _cache_key_for_identity(
            input_hash=input_hash,
            group_hash=group_hash,
            adapter=identity.adapter,
            model=identity.model,
            prompt_version=identity.prompt_version,
        )

    @property
    def _write_receipt_version(self) -> str:
        return (
            SEMANTIC_ROUTE_RECEIPT_VERSION
            if self.executor is not None
            else SEMANTIC_ROUTE_RECEIPT_V1
        )


def _cache_key_for_identity(
    *,
    input_hash: str,
    group_hash: str,
    adapter: str,
    model: str,
    prompt_version: str,
) -> str:
    return _hash_json(
        {
            "adapter": adapter,
            "group_hash": group_hash,
            "input_hash": input_hash,
            "model": model,
            "prompt_version": prompt_version,
        }
    )


def _semantic_input_requires_model(
    unit_input: SemanticRouteUnitInput,
    *,
    document: SemanticDocumentContext,
) -> bool:
    if not (
        _MODEL_MIN_CANDIDATES
        <= len(unit_input.candidates)
        <= _MODEL_MAX_CANDIDATES
    ):
        return False
    locked = tuple(
        candidate for candidate in unit_input.candidates if candidate.locked
    )
    if locked:
        # Exact/typed routes are already sufficient.  Independently witnessed
        # structural secondaries are promoted to locks before this boundary;
        # optional mentions remain lexical rather than paying for enrichment.
        return False
    evidence = tuple(
        set(candidate.evidence_kinds) for candidate in unit_input.candidates
    )
    has_body_candidate = any(
        kinds & {"source_body_candidate"}
        for kinds in evidence
    )
    if not has_body_candidate:
        return False
    # A model may resolve unresolved closed candidates whose labels occur in
    # Unit-local body text.  Similarity-only headings and untyped table cells
    # are recall surfaces, not sufficient authority to spend a model call;
    # deterministic routes never pay for optional enrichment.  Fixed batches
    # keep this a bounded fallback rather than a primary classifier.
    return True


def _sources_have_mechanical_toc(
    sources: Sequence[SemanticRouteSource],
) -> bool:
    body = "\n".join(
        source.text for source in sources if source.kind in {"body_text", "table_text"}
    )
    compact = re.sub(r"\s+", "", body)
    if "内容页码" not in compact:
        return False
    return len(re.findall(r"\d+\s*[-–—]\s*\d+", body)) >= 2


def _semantic_adjudication_groups(
    inputs: Sequence[SemanticRouteUnitInput],
    *,
    batch_size: int,
) -> tuple[tuple[SemanticRouteUnitInput, ...], ...]:
    return tuple(
        tuple(inputs[offset : offset + batch_size])
        for offset in range(0, len(inputs), batch_size)
    )


def _derive_v2_receipt_group_hashes(
    *,
    inputs: Sequence[SemanticRouteUnitInput],
    model_inputs: Sequence[SemanticRouteUnitInput],
    receipts: Sequence[SemanticRouteReceipt],
) -> dict[int, str]:
    """Recover historical v2 groups from receipts, independent of current settings."""

    model_indices = {unit_input.unit_index for unit_input in model_inputs}
    paired = tuple(zip(inputs, receipts, strict=True))
    for unit_input, receipt in paired:
        if unit_input.unit_index not in model_indices and receipt.adjudication is not None:
            raise SemanticRouteContractError(
                "semantic v2 receipt no longer belongs to a model group"
            )

    groups: list[
        tuple[str, list[SemanticRouteUnitInput], SemanticAdjudicationReceipt]
    ] = []
    closed_hashes: set[str] = set()
    for unit_input, receipt in paired:
        if unit_input.unit_index not in model_indices:
            continue
        if receipt.contract_version != SEMANTIC_ROUTE_RECEIPT_VERSION:
            raise SemanticRouteContractError(
                "semantic replay cannot mix model receipt contract versions"
            )
        adjudication = receipt.adjudication
        if adjudication is None:
            raise SemanticRouteContractError(
                "semantic v2 receipt no longer belongs to a model group"
            )
        group_hash = adjudication.group_hash
        if not groups or groups[-1][0] != group_hash:
            if group_hash in closed_hashes:
                raise SemanticRouteContractError(
                    "semantic v2 receipt group membership is not contiguous"
                )
            if groups:
                closed_hashes.add(groups[-1][0])
            groups.append((group_hash, [unit_input], adjudication))
            continue
        if adjudication != groups[-1][2]:
            raise SemanticRouteContractError(
                "semantic v2 receipt group lineage differs between members"
            )
        groups[-1][1].append(unit_input)

    expected: dict[int, str] = {}
    for group_hash, members, _adjudication in groups:
        if _semantic_adjudication_group_hash(members) != group_hash:
            raise SemanticRouteContractError(
                "semantic v2 receipt group membership or order drifted"
            )
        expected.update((member.unit_index, group_hash) for member in members)
    if set(expected) != model_indices:
        raise SemanticRouteContractError(
            "semantic v2 receipt groups do not cover the exact model Units"
        )
    return expected


def _semantic_adjudication_group_hash(
    inputs: Sequence[SemanticRouteUnitInput],
) -> str:
    if not inputs:
        raise SemanticRouteContractError("semantic adjudication group cannot be empty")
    return _hash_json(
        {
            "contract_version": "semantic_adjudication_group.v1",
            "input_hashes": [unit_input.input_hash for unit_input in inputs],
        }
    )


def semantic_document_context(document: e.Document) -> SemanticDocumentContext:
    """Project only the Document priors allowed to shrink Unit candidates."""

    topics = tuple(
        value
        for value in (document.class_disclosure_topics or [])
        if isinstance(value, str) and value.strip()
    )
    categories: list[str] = []
    for item in document.class_content_categories or []:
        if isinstance(item, str) and item.strip():
            categories.append(item)
        elif isinstance(item, dict):
            for field in ("code", "name"):
                value = item.get(field)
                if isinstance(value, str) and value.strip() and value not in categories:
                    categories.append(value)
    return SemanticDocumentContext(
        title=document.title,
        filing_type=document.class_filing_type,
        disclosure_topics=tuple(dict.fromkeys(topics)),
        content_categories=tuple(categories),
    )


def _unit_sources(
    *,
    admitted: AdmittedProviderDocument,
    document: SemanticDocumentContext,
    draft: ProviderUnitDraft,
) -> tuple[SemanticRouteSource, ...]:
    sources: list[SemanticRouteSource] = []
    if draft.title and draft.title.strip():
        sources.append(
            SemanticRouteSource(
                source_id=f"u{draft.unit_index}:title",
                kind="unit_title",
                text=_compact_text(draft.title),
            )
        )
    for index, heading in enumerate(draft.heading_path[:-1]):
        if heading.strip():
            sources.append(
                SemanticRouteSource(
                    source_id=f"u{draft.unit_index}:path:{index}",
                    kind="heading_path",
                    text=_compact_text(heading),
                )
            )

    body_chars = 0
    blocks_by_source = {
        block.source_index: block
        for block in admitted.envelope.provider_document.blocks
    }
    owned_source_indices = {
        source_index
        for part in draft.locator.parts
        for source_index in part.block_source_indices
    }
    if draft.locator.heading_chain:
        leaf_heading = draft.locator.heading_chain[-1]
        owned_source_indices.add(leaf_heading.source_index)
        owned_source_indices.update(
            fragment.source_index
            for fragment in leaf_heading.continuation_fragments
        )
    part_kind_by_source = {
        source_index: part.kind
        for part in draft.locator.parts
        for source_index in part.block_source_indices
    }
    for binding in draft.locator.search_targets:
        if binding.destination.kind in {"unit_title", "unit_title_fragment"}:
            continue
        block = blocks_by_source.get(binding.source.source_index)
        if block is not None and _is_unowned_numbered_page_footnote(
            block=block,
            blocks=tuple(blocks_by_source.values()),
            owned_source_indices=owned_source_indices,
        ):
            continue
        values = replay_provider_unit_search_binding(admitted, draft, binding)
        part_kind = part_kind_by_source.get(binding.source.source_index)
        default_kind: SemanticRouteSourceKind = (
            "table_text" if part_kind == "table" else "body_text"
        )
        value_kinds: list[SemanticRouteSourceKind] = [default_kind] * len(values)
        if (
            values
            and default_kind == "table_text"
            and binding.source.field == "table_body"
            and binding.source.transform == "html_visible_text_segments.v1"
        ):
            structured = html_table_semantic_segments(
                replay_provider_unit_search_binding_source_text(
                    admitted,
                    draft,
                    binding,
                )
            )
            if tuple(item.text for item in structured) == values:
                value_kinds = [item.role for item in structured]
        for value_index, value in enumerate(values):
            compact = _compact_text(value)
            if not compact or body_chars >= _MAX_UNIT_SOURCE_TEXT:
                continue
            compact = compact[: max(0, _MAX_UNIT_SOURCE_TEXT - body_chars)]
            if not compact:
                continue
            body_chars += len(compact)
            sources.append(
                SemanticRouteSource(
                    source_id=(
                        f"u{draft.unit_index}:target:{binding.source.target_id}:"
                        f"{value_index}"
                    ),
                    kind=value_kinds[value_index],
                    text=compact,
                )
            )

    if document.title and document.title.strip():
        sources.append(
            SemanticRouteSource(
                source_id="document:title",
                kind="document_title",
                text=_compact_text(document.title),
            )
        )
    if document.filing_type and document.filing_type.strip():
        sources.append(
            SemanticRouteSource(
                source_id="document:filing_type",
                kind="document_filing_type",
                text=document.filing_type,
            )
        )
    sources.extend(
        SemanticRouteSource(
            source_id=f"document:topic:{index}",
            kind="document_topic",
            text=value,
        )
        for index, value in enumerate(document.disclosure_topics)
    )
    sources.extend(
        SemanticRouteSource(
            source_id=f"document:category:{index}",
            kind="document_category",
            text=value,
        )
        for index, value in enumerate(document.content_categories)
    )
    return tuple(sources)


def _apply_receipt(
    draft: ProviderUnitDraft,
    receipt: SemanticRouteReceipt,
    *,
    section_keys: tuple[str, ...] | None,
) -> ProviderUnitDraft:
    semantic_keys = (
        None
        if not receipt.semantic_keys
        or receipt.semantic_keys == (SEMANTIC_FALLBACK_KEY,)
        else receipt.semantic_keys
    )
    hashes = compute_unit_hashes(
        payload_kind=draft.payload_kind,
        payload=draft.payload,
        title=draft.title,
        heading_path=list(draft.heading_path),
        semantic_keys=None if semantic_keys is None else list(semantic_keys),
        section_keys=None if section_keys is None else list(section_keys),
        applicability=draft.applicability,
        quality_status=draft.quality_status,
        order_index=draft.unit_index + 1,
    )
    return replace(
        draft,
        semantic_keys=semantic_keys,
        section_keys=section_keys,
        content_hash=hashes.content_hash,
        query_projection_hash=hashes.query_projection_hash,
        structure_hash=hashes.structure_hash,
    )


def _draft_has_answer_content(draft: ProviderUnitDraft) -> bool:
    """Match the public body-status boundary for answer-bearing direct routes."""

    return not (
        draft.payload_kind == "text"
        and draft.payload == {"text": ""}
    )


def _sources_are_only_statutory_disclosure_boilerplate(
    sources: Sequence[SemanticRouteSource],
) -> bool:
    """Reject title-only routing when the sole body is a truthfulness notice."""

    local_sources = tuple(
        source for source in sources if source.kind in _LOCAL_CONTENT_SOURCE_KINDS
    )
    if not local_sources:
        return False
    substantive: list[str] = []
    index = 0
    while index < len(local_sources):
        source = local_sources[index]
        text = source.text.strip()
        if not text or _is_disclosure_metadata_line(text):
            index += 1
            continue
        if index + 1 < len(local_sources) and _is_disclosure_metadata_field_pair(
            source,
            local_sources[index + 1],
        ):
            index += 2
            continue
        if source.kind in {"body_text", "table_text"}:
            substantive.append(text)
            index += 1
            continue
        if source.kind in {"table_field_label", "table_column_header"}:
            if _is_statutory_disclosure_guarantee(text):
                substantive.append(text)
                index += 1
                continue
        return False
    return bool(substantive) and _is_statutory_disclosure_guarantee(
        " ".join(substantive)
    )


def _sources_are_only_page_carryover_metadata(
    sources: Sequence[SemanticRouteSource],
) -> bool:
    """Reject a continuation title backed only by report/page furniture."""

    local_sources = tuple(
        source for source in sources if source.kind in _LOCAL_CONTENT_SOURCE_KINDS
    )
    return bool(local_sources) and all(
        _is_page_carryover_metadata_line(source.text) for source in local_sources
    )


def _sources_are_only_cross_reference_carriers(
    sources: Sequence[SemanticRouteSource],
) -> bool:
    """Suppress direct routing for a cross-reference-only notice carrier."""

    unit_titles = tuple(
        _normalize_title(source.text)
        for source in sources
        if source.kind == "unit_title" and source.text.strip()
    )
    local_sources = tuple(
        source
        for source in sources
        if source.kind in _LOCAL_CONTENT_SOURCE_KINDS and source.text.strip()
    )
    if not local_sources:
        return False
    has_reference = False
    for source in local_sources:
        clauses = _split_top_level_clauses(
            _normalize_match_text(source.text),
            separators="。；;!?！？",
        )
        for compact in clauses:
            if _is_closed_cross_reference_clause(
                compact
            ) or _is_closed_internal_cross_reference_clause(
                compact,
                unit_titles=unit_titles,
            ):
                has_reference = True
                continue
            if re.fullmatch(r"(?:\d{4}年)?\d{1,2}月\d{1,2}日", compact):
                continue
            if len(compact) <= 80 and (
                compact.endswith(("董事会", "监事会"))
                or re.fullmatch(
                    r".+(?:董事会|监事会)\d{4}年\d{1,2}月\d{1,2}日",
                    compact,
                )
            ):
                continue
            return False
    return has_reference


def _is_closed_internal_cross_reference_clause(
    compact: str,
    *,
    unit_titles: Sequence[str],
) -> bool:
    """Recognize a title-bound ``topic + see below`` carrier with no local fact.

    Provider documents commonly repeat the current heading before a quoted
    internal path.  Requiring a full balanced citation, a noun phrase already
    present in the Unit title, and no quantitative/pricing witness keeps this
    narrower than factual clauses whose citation is merely a suffix.
    """

    match = re.fullmatch(
        r"(?P<prefix>[^，,。；;!?！？“”《》「」]{2,60}?)"
        r"(?:具体内容|相关内容|具体情况|相关情况)?"
        r"(?:详见|参见|请见|参阅|查阅)"
        r"(?:(?:本公告|本报告)?下文)?"
        r"(?P<target>“[^”]{1,180}”|《[^》]{1,180}》|「[^」]{1,180}」)",
        compact,
    )
    if match is None:
        return False
    prefix = _normalize_title(match.group("prefix"))
    target = _normalize_match_text(match.group("target"))
    if not prefix or not any(
        prefix == title or title.endswith(prefix) or prefix.endswith(title)
        for title in unit_titles
    ):
        return False
    target_inner = target[1:-1]
    if not re.search(
        r"(?:"
        r"第[一二三四五六七八九十百0-9]+(?:章|节|部分|项)|"
        r"[一二三四五六七八九十]+、|"
        r"之[（(]?[一二三四五六七八九十0-9]+[）)]?|"
        r"(?:^|之)\d+[.、]|"
        r"附注(?:第)?[一二三四五六七八九十百0-9]+"
        r")",
        target_inner,
    ):
        return False
    if any(
        marker in target
        for marker in (
            "评估值",
            "收益法",
            "成本法",
            "市场法",
            "交易价格",
            "定价公平",
            "公允",
            "%",
            "％",
            "万元",
            "亿元",
            "P0=",
            "P1=",
        )
    ) or re.search(r"\d[\d,.]*(?:元/股|元|%|％|万元|亿元)", target):
        return False
    return True


def _is_closed_cross_reference_clause(compact: str) -> bool:
    """Accept only a citation whose target closes the entire clause."""

    if len(compact) > 240:
        return False
    prefix = re.match(
        r"^(?:(?:具体内容|有关内容|详细内容|相关内容|具体情况|相关情况)?"
        r"(?:详见|参见|请见|参阅|查阅)|"
        r"(?:关于)?(?:本报告期|报告期内)?(?:公司)?(?:其他)?"
        r"(?:重要事项|相关事项|有关事项)(?:具体内容)?"
        r"[,，]?(?:详见|参见|请见|参阅|查阅))",
        compact,
    )
    if prefix is None:
        return False
    target = compact[prefix.end() :]
    if not target or _has_top_level_separator(target, separators=",，"):
        return False
    citation_metadata = (
        r"(?:[（(](?:公告编号|编号|页码|第\d+页)"
        r"[:：]?[a-z0-9\-—.第页共]*[）)])?"
    )
    quoted_target = (
        r"《[^》]{1,160}(?:报告|公告|附注)[^》]{0,40}》|"
        r"“[^”]{1,160}(?:报告|公告|附注)[^”]{0,40}”|"
        r"「[^」]{1,160}(?:报告|公告|附注)[^」]{0,40}」"
    )
    if re.fullmatch(r"(?:" + quoted_target + r")" + citation_metadata, target):
        return True
    if any(marker in target for marker in "《》“”「」"):
        return False
    plain_target = (
        r".+(?:报告全文|公告全文|报告|公告|"
        r"附注(?:[一-龥\d.．、\-—~～章节页号]*))"
    )
    return bool(re.fullmatch(plain_target + citation_metadata, target))


def _split_top_level_clauses(
    compact: str,
    *,
    separators: str,
) -> tuple[str, ...]:
    """Split only outside balanced Chinese title/quotation wrappers."""

    closing = {"《": "》", "“": "”", "「": "」"}
    stack: list[str] = []
    clauses: list[str] = []
    start = 0
    for index, character in enumerate(compact):
        if character in closing:
            stack.append(closing[character])
        elif stack and character == stack[-1]:
            stack.pop()
        elif not stack and character in separators:
            if compact[start:index]:
                clauses.append(compact[start:index])
            start = index + 1
    if compact[start:]:
        clauses.append(compact[start:])
    return tuple(clauses)


def _has_top_level_separator(compact: str, *, separators: str) -> bool:
    return len(_split_top_level_clauses(compact, separators=separators)) > 1


def _is_accounting_change_event_fact(source: SemanticRouteSource) -> bool:
    """Recognize an event-local accounting-policy change, not a reference."""

    if source.kind not in _LOCAL_CONTENT_SOURCE_KINDS:
        return False
    normalized = _normalize_match_text(source.text)
    return (
        "本次会计政策变更" in normalized
        and any(
            marker in normalized
            for marker in ("执行", "采用", "变更前", "变更后", "影响")
        )
        and not _clause_is_reference(normalized)
    )


def _guaranteed_party_profile_support_ids(
    sources: Sequence[SemanticRouteSource],
) -> tuple[str, ...]:
    """Return sources proving a closed guaranteed-party identity profile."""

    summary_title = next(
        (
            source
            for source in sources
            if source.kind == "unit_title"
            and _normalize_title(source.text) == "担保对象和基本情况"
        ),
        None,
    )
    summary_header = next(
        (
            source
            for source in sources
            if source.kind == "table_column_header"
            and _normalize_match_text(source.text) == "被担保人名称"
        ),
        None,
    )
    summary_values = tuple(
        source
        for source in sources
        if source.kind == "table_text" and source.text.strip()
    )
    if (
        summary_title is not None
        and summary_header is not None
        and summary_values
    ):
        return tuple(
            dict.fromkeys(
                (
                    summary_title.source_id,
                    summary_header.source_id,
                    *(source.source_id for source in summary_values),
                )
            )
        )

    markers = {
        "被担保人名称": False,
        "统一社会信用代码": False,
        "注册资本": False,
    }
    support: list[str] = []
    local_sources = tuple(
        source for source in sources if source.kind in _LOCAL_CONTENT_SOURCE_KINDS
    )
    for index, source in enumerate(local_sources):
        if source.kind not in _LOCAL_CONTENT_SOURCE_KINDS:
            continue
        normalized = _normalize_match_text(source.text)
        matched = tuple(marker for marker in markers if marker in normalized)
        if not matched:
            continue
        for marker in matched:
            residue = normalized.split(marker, 1)[1].lstrip(":为是")
            value_source = None
            if residue:
                value_source = source
            elif index + 1 < len(local_sources):
                candidate = local_sources[index + 1]
                candidate_text = _normalize_match_text(candidate.text)
                if candidate_text and not any(
                    controlled in candidate_text for controlled in markers
                ):
                    value_source = candidate
            if value_source is None:
                continue
            markers[marker] = True
            support.extend((source.source_id, value_source.source_id))
    if markers["被担保人名称"] and (
        markers["统一社会信用代码"] or markers["注册资本"]
    ):
        return tuple(dict.fromkeys(support))
    return ()


def _guarantee_agreement_terms_support_ids(
    sources: Sequence[SemanticRouteSource],
) -> tuple[str, ...]:
    """Return explicit guarantee-term fields inside a terms section."""

    controlled = ("担保金额", "担保范围", "担保方式", "担保期限")
    support: list[str] = []
    local_sources = tuple(
        source for source in sources if source.kind in _LOCAL_CONTENT_SOURCE_KINDS
    )
    for index, source in enumerate(local_sources):
        normalized = _normalize_field_source(source.text)
        for label in controlled:
            if not normalized.startswith(label):
                continue
            suffix = normalized[len(label) :]
            if suffix.startswith((":", "为", "是")) and bool(suffix[1:]):
                support.append(source.source_id)
                break
            if (
                not suffix
                and source.kind
                in {"table_field_label", "table_column_header", "body_text"}
                and index + 1 < len(local_sources)
            ):
                candidate = local_sources[index + 1]
                candidate_text = _normalize_match_text(candidate.text)
                if candidate_text and not any(
                    candidate_text.startswith(item) for item in controlled
                ):
                    support.extend((source.source_id, candidate.source_id))
                break
    return tuple(dict.fromkeys(support))


def _guarantee_progress_support_ids(
    sources: Sequence[SemanticRouteSource],
) -> tuple[str, ...]:
    """Return signed guarantee agreements with a concrete amount."""

    support: list[str] = []
    for source in sources:
        if source.kind not in _LOCAL_CONTENT_SOURCE_KINDS:
            continue
        normalized = _normalize_match_text(source.text)
        if (
            (
                any(marker in normalized for marker in ("签署", "签订"))
                and any(
                    marker in normalized
                    for marker in ("保证合同", "担保合同", "担保协议")
                )
                or "本次担保金额" in normalized
            )
            and re.search(r"\d+(?:\.\d+)?(?:万|亿)?(?:元|美元|欧元)", normalized)
        ):
            support.append(source.source_id)
    return tuple(dict.fromkeys(support))


def _major_asset_acquisition_progress_support_ids(
    sources: Sequence[SemanticRouteSource],
) -> tuple[str, ...]:
    """Return Unit-local facts that say a major acquisition was completed."""

    support: list[str] = []
    for source in sources:
        if source.kind not in _LOCAL_CONTENT_SOURCE_KINDS:
            continue
        segments = _split_top_level_clauses(
            _normalize_match_text(source.text),
            separators="。；;!?！？,，",
        )
        for segment in segments:
            factual = _strip_closed_cross_reference_tail(segment)
            factual = re.sub(
                r"《[^》]{0,160}》|“[^”]{0,160}”|「[^」]{0,160}」",
                "",
                factual,
            )
            if not factual or _clause_is_reference(factual):
                continue
            subject = (
                r"(?:其中)?(?:"
                r"(?:公司|本公司|本集团)(?:本期|报告期内)?|"
                r"(?:本期|报告期内)(?:公司|本公司|本集团)?"
                r")?"
            )
            if re.fullmatch(
                subject
                + r"(?:已|已经)?完成(?:本次)?重大(?:资产|股权)"
                r"收购(?:事项)?",
                factual,
            ) or re.fullmatch(
                subject
                + r"重大(?:资产|股权)收购(?:事项)?(?:已|已经)完成",
                factual,
            ):
                support.append(source.source_id)
                break
    return tuple(dict.fromkeys(support))


def _strip_closed_cross_reference_tail(compact: str) -> str:
    """Remove a citation-only suffix while preserving a preceding local fact."""

    for match in re.finditer(
        r"(?:具体情况|相关情况|具体内容|有关内容|详细内容|相关内容)?"
        r"(?:详见|参见|请见|参阅|查阅)",
        compact,
    ):
        if _is_closed_cross_reference_clause(compact[match.start() :]):
            return compact[: match.start()]
    return compact


def _bank_loan_quality_support_ids(
    sources: Sequence[SemanticRouteSource],
) -> tuple[str, ...]:
    """Return a source-bound bank asset-quality table witness."""

    titles = {
        _normalize_title(source.text)
        for source in sources
        if source.kind == "unit_title"
    }
    if not any(title in {"资产质量", "贷款质量"} for title in titles):
        return ()
    support: list[str] = []
    for source in sources:
        if source.kind not in _LOCAL_CONTENT_SOURCE_KINDS:
            continue
        normalized = _normalize_match_text(source.text)
        if (
            "不良贷款率" in normalized
            and any(
                marker in normalized
                for marker in ("企业贷款", "个人贷款", "贷款和垫款")
            )
            and re.search(r"\d", normalized)
        ):
            support.append(source.source_id)
    return tuple(dict.fromkeys(support))


def _bank_balance_support_ids(
    sources: Sequence[SemanticRouteSource],
) -> dict[str, tuple[str, ...]]:
    """Bind bank loan/deposit totals inside a closed profitability table."""

    titles = {
        _normalize_title(source.text)
        for source in sources
        if source.kind == "unit_title"
    }
    if not titles & {"盈利和规模", "存贷款情况"}:
        return {}
    marker_by_key = {
        "bank_customer_deposits": ("存款总额", "客户存款", "吸收存款本金"),
        "bank_loans_advances": ("贷款总额", "贷款和垫款"),
    }
    result: dict[str, tuple[str, ...]] = {}
    for key, markers in marker_by_key.items():
        support_list: list[str] = []
        for index, source in enumerate(sources):
            normalized = _normalize_match_text(source.text)
            if source.kind != "table_text" or not any(
                marker in normalized for marker in markers
            ):
                continue
            if re.search(r"\d", normalized):
                support_list.append(source.source_id)
                continue
            if index + 1 < len(sources):
                value_source = sources[index + 1]
                if value_source.kind == "table_text" and re.fullmatch(
                    r"[()（）+\-－−0-9０-９.,，%％]+",
                    _normalize_match_text(value_source.text),
                ):
                    support_list.extend((source.source_id, value_source.source_id))
        support = tuple(dict.fromkeys(support_list))
        if support:
            result[key] = support
    return result


def _is_page_carryover_metadata_line(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).lower()
    if re.fullmatch(r"[（(]?第?\d+页[,，]?共\d+页[）)]?", compact):
        return True
    return bool(
        len(compact) <= 80
        and compact.endswith("号")
        and re.search(r"\d", compact)
        and any(
            marker in compact
            for marker in ("审字", "报字", "验字", "报(审)字", "报（审）字")
        )
    )


def _is_disclosure_metadata_line(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    return bool(
        re.fullmatch(
            r"(?:A股|B股|H股|证券|股票|债券)?(?:代码|简称)[:：].+"
            r"|公告编号[:：].+",
            compact,
        )
    )


def _is_disclosure_metadata_field_pair(
    label_source: SemanticRouteSource,
    value_source: SemanticRouteSource,
) -> bool:
    if value_source.kind != "table_text":
        return False
    label = _normalize_match_text(label_source.text).rstrip(":")
    if not re.fullmatch(
        r"(?:(?:a股|b股|h股|证券|股票|债券)?(?:代码|简称)|公告编号)",
        label,
    ):
        return False
    value = " ".join(value_source.text.split())
    return bool(value) and len(value) <= 40 and not any(
        marker in value for marker in "。；;！？!?"
    )


def _is_statutory_disclosure_guarantee(value: str) -> bool:
    compact = re.sub(r"[^a-z0-9\u3400-\u9fff]", "", value.lower())
    if len(compact) > 180 or "保证" not in compact:
        return False
    prefix, guarantee = compact.split("保证", 1)
    if len(prefix) > 60 or any(character.isdigit() for character in prefix):
        return False
    disclosure_object = any(
        marker in guarantee
        for marker in (
            "信息披露内容",
            "本公告内容",
            "本报告内容",
            "本报告书内容",
            "所载资料",
            "所载信息",
        )
    )
    truthfulness = all(marker in guarantee for marker in ("真实", "准确", "完整"))
    no_misstatement = all(
        marker in guarantee
        for marker in ("虚假记载", "误导性陈述", "重大遗漏")
    )
    if not disclosure_object or not (truthfulness or no_misstatement):
        return False
    if no_misstatement:
        suffix = guarantee.split("重大遗漏", 1)[1]
        return not suffix or bool(
            re.fullmatch(
                r"并对(?:其|本公告|本报告|本报告书)?(?:内容|信息|资料)?"
                r"的?真实(?:性)?准确(?:性)?(?:和|及)?完整(?:性)?"
                r"(?:依法)?(?:承担)?"
                r"(?:个别(?:和|及)连带的?)?(?:法律)?责任",
                suffix,
            )
        )
    return guarantee.endswith("完整")


def _is_correction_overview_title(title: str) -> bool:
    """Recognize a complete correction-announcement title, not a loose mention."""

    suffix = next(
        (item for item in _CORRECTION_OVERVIEW_SUFFIXES if title.endswith(item)),
        None,
    )
    if suffix is None:
        return False
    if title == suffix:
        return True
    subject = title[: -len(suffix)]
    about_index = subject.rfind("关于")
    return about_index >= 0 and subject[about_index + len("关于") :].endswith("的")


def _is_shareholder_change_correction_title(title: str) -> bool:
    if not _is_correction_overview_title(title):
        return False
    has_change = any(
        marker in title for marker in ("增持", "减持", "权益变动", "持股变动")
    )
    has_equity_object = any(
        marker in title for marker in ("股份", "股权", "持股", "权益")
    )
    return has_change and has_equity_object


def _semantic_input_hash(
    *,
    taxonomy: SemanticRouteTaxonomy,
    document: SemanticDocumentContext,
    unit_index: int,
    sources: tuple[SemanticRouteSource, ...],
    candidates: tuple[SemanticRouteCandidate, ...],
) -> str:
    definitions = taxonomy.by_key()
    return _hash_json(
        {
            "candidates": [
                {
                    "evidence_kinds": list(candidate.evidence_kinds),
                    "key": candidate.key,
                    "locked": candidate.locked,
                    "source_ids": list(candidate.source_ids),
                }
                for candidate in candidates
            ],
            "candidate_definitions": [
                {
                    "description": definitions[candidate.key].description,
                    "exclusive_container": definitions[candidate.key].exclusive_container,
                    "key": candidate.key,
                    "labels": list(definitions[candidate.key].labels),
                    "overview_container": definitions[
                        candidate.key
                    ].overview_container,
                    "role_anchor": definitions[candidate.key].role_anchor,
                    "section_container": definitions[
                        candidate.key
                    ].section_container,
                    "scopes": list(definitions[candidate.key].scopes),
                }
                for candidate in candidates
            ],
            "document": {
                "content_categories": list(document.content_categories),
                "disclosure_topics": list(document.disclosure_topics),
                "filing_type": document.filing_type,
                "title": document.title,
            },
            "router_version": SEMANTIC_ROUTER_VERSION,
            "sources": [
                {"kind": source.kind, "source_id": source.source_id, "text": source.text}
                for source in sources
            ],
            "taxonomy_version": taxonomy.version,
            "unit_index": unit_index,
        }
    )


def _decision_hash(decision: SemanticAdjudicationDecision) -> str:
    return _hash_json(
        {
            "routes": [
                {"key": route.key, "support_ids": list(route.support_ids)}
                for route in decision.routes
            ],
            "unit_index": decision.unit_index,
        }
    )


def _candidate_is_similarity_only(item: _CandidateState) -> bool:
    evidence = set(item.evidence_kinds or ())
    return bool(evidence) and evidence.issubset(
        {"source_heading_similarity", "document_context_candidate"}
    )


def _is_standardized_quantitative_topic(
    *,
    definition: SemanticRouteDefinition,
    normalized_label: str,
    source: SemanticRouteSource,
    longer_labels: tuple[str, ...],
) -> bool:
    """Lock a source-bound quantitative Unit topic.

    Both event and periodic topics use versioned positive allowlists.  The
    controlled label must be followed by an adjacent numeric value, optionally
    through a closed connector; periodic topics may also state an explicit
    directional result.  History, risk, conditions, plans and causality are L2
    modality—not reasons to erase the Unit's L1 retrieval topic.  A number
    elsewhere in the paragraph or a longer controlled label still cannot
    manufacture this route.
    """

    if (
        definition.overview_container
        or definition.exclusive_container
        or definition.context_container
        or source.kind != "body_text"
        or len(normalized_label.rstrip(":")) < 4
        or not definition.quantitative_topic
    ):
        return False
    label = normalized_label.rstrip(":")
    normalized_source = _normalize_match_text(source.text)
    value = r"(?:人民币)?[+\-－−]?[0-9０-９]"
    connectors = (
        "为",
        "达",
        "达到",
        "实现",
        "增加",
        "减少",
        "增长",
        "下降",
        "上升",
        "降低",
        "同比增长",
        "同比下降",
        "分别为",
        "变动幅度为",
    )
    acronym = r"(?:[（(][a-z0-9._/\-]{1,12}[）)])?"
    subject = re.escape(label) + acronym + r"(?:总额)?"
    if label.endswith(connectors):
        direct_result = re.compile(subject + value)
    else:
        connector = "|".join(
            re.escape(item) for item in sorted(connectors, key=len, reverse=True)
        )
        link = rf"(?::)?(?:(?:{connector}))?"
        direct_result = re.compile(subject + link + value)
    matches = list(direct_result.finditer(normalized_source))
    if _PERIODIC_FILING_TYPES.issubset(set(definition.scopes)):
        directional = "|".join(
            re.escape(item)
            for item in (
                "同比增长",
                "同比下降",
                "增加",
                "减少",
                "增长",
                "下降",
                "上升",
                "降低",
            )
        )
        matches.extend(
            re.compile(re.escape(label) + rf"(?:{directional})").finditer(
                normalized_source
            )
        )
        matches.extend(
            re.compile(
                re.escape(label) + r"变动原因说明(?::|$)"
            ).finditer(normalized_source)
        )
        matches.extend(
            re.compile(
                r"(?:提升|提高|增厚|摊薄|稀释)(?:股东的|公司的)?"
                + re.escape(label)
                + r"(?!披露质量|计算方法|预测口径)"
            ).finditer(normalized_source)
        )
    if not matches:
        return False
    for match in matches:
        if any(
            longer_match.start() <= match.start() < longer_match.end()
            for longer_label in longer_labels
            for longer_match in re.finditer(re.escape(longer_label), normalized_source)
        ):
            continue
        return True
    return False


def _market_value_management_support_ids(
    sources: Sequence[SemanticRouteSource],
) -> tuple[str, ...]:
    support: list[str] = []
    for source in sources:
        if source.kind not in _LOCAL_CONTENT_SOURCE_KINDS:
            continue
        normalized = _normalize_match_text(source.text)
        if _clause_is_reference(normalized) or "市值管理" not in normalized:
            continue
        issuer_fact = bool(
            re.search(
                r"(?:公司|本集团).{0,24}(?:将市值管理作为|开展市值管理|推进市值管理|"
                r"实施市值管理|落实市值管理)",
                normalized,
            )
        ) or bool(
            re.search(
                r"市值管理.{0,24}(?:统筹运用|已经制定|已制定|已经落实|已落实)",
                normalized,
            )
        )
        if issuer_fact and not any(
            marker in normalized for marker in ("管理制度规定", "职责包括")
        ):
            support.append(source.source_id)
    return tuple(dict.fromkeys(support))


def _structured_entity_selector_support_ids(
    sources: Sequence[SemanticRouteSource],
) -> tuple[str, ...]:
    local = tuple(
        source for source in sources if source.kind in _LOCAL_CONTENT_SOURCE_KINDS
    )
    for index, source in enumerate(local[:-1]):
        normalized = _normalize_match_text(source.text)
        following = _normalize_match_text(local[index + 1].text)
        if (
            re.match(r"^[（(]?[0-9一二三四五六七八九十]+[）)]", normalized)
            and "结构化主体" in normalized
            and normalized.endswith(":")
            and re.fullmatch(r"[□☐☑√✓\uf052]适用[□☐☑√✓\uf052]不适用", following)
        ):
            return (source.source_id, local[index + 1].source_id)
    return ()


def _sources_define_materiality_criteria(
    sources: Sequence[SemanticRouteSource],
) -> bool:
    titles = {
        _normalize_title(source.text)
        for source in sources
        if source.kind == "unit_title"
    }
    if "重要性标准确定方法和选择依据" not in titles:
        return False
    has_definition = any(
        source.kind in _LOCAL_CONTENT_SOURCE_KINDS
        and "在判断重要性时" in _normalize_match_text(source.text)
        for source in sources
    )
    has_criteria_header = any(
        source.kind in _LOCAL_CONTENT_SOURCE_KINDS
        and _normalize_title(source.text) == "重要性标准"
        for source in sources
    )
    return has_definition and has_criteria_header


def _is_unowned_numbered_page_footnote(
    *,
    block: ProviderBlock,
    blocks: Sequence[ProviderBlock],
    owned_source_indices: set[int],
) -> bool:
    provider_type = block.provider_type.casefold()
    annotation = (block.typed_annotation or "").casefold()
    if provider_type != "page_footnote" and annotation != "page_footnote":
        return False
    text = " ".join(payload.text for payload in block.payloads)
    marker = re.match(r"^\s*<sup>([0-9]+)</sup>", text)
    if marker is None:
        return False
    token = f"<sup>{marker.group(1)}</sup>"
    page_index = block.page_index
    source_index = block.source_index
    owned_hits: set[int] = set()
    for candidate in blocks:
        candidate_source = candidate.source_index
        candidate_type = candidate.provider_type.casefold()
        candidate_annotation = (candidate.typed_annotation or "").casefold()
        if (
            candidate_source >= source_index
            or candidate.page_index != page_index
            or candidate_type
            in {
                "frame",
                "header",
                "footer",
                "page_header",
                "page_footer",
                "page_number",
                "page_footnote",
            }
            or candidate_annotation
            in {
                "frame",
                "header",
                "footer",
                "page_header",
                "page_footer",
                "page_number",
                "page_footnote",
            }
        ):
            continue
        candidate_text = " ".join(payload.text for payload in candidate.payloads)
        if token in candidate_text:
            if candidate_source in owned_source_indices:
                owned_hits.add(candidate_source)
    return not owned_hits


def _is_labeled_field_fact(
    *,
    definition: SemanticRouteDefinition,
    normalized_label: str,
    source: SemanticRouteSource,
    following_sources: Sequence[SemanticRouteSource],
) -> bool:
    """Lock one explicit typed field/header or non-periodic labeled field.

    A controlled field label establishes the L1 topic even when its value is
    negative, absent, not applicable, historical, or planned.  Those value
    semantics belong to L2.  Periodic reports stay deterministic only for
    typed table fields/headers on the quantitative-topic allowlist; visible
    table text remains lexical/candidate evidence.
    """

    periodic_definition = _PERIODIC_FILING_TYPES.issubset(
        set(definition.scopes)
    )

    if (
        definition.overview_container
        or definition.exclusive_container
        or definition.context_container
        or source.kind
        not in {
            "body_text",
            "table_text",
            "table_field_label",
            "table_column_header",
        }
        or (
            periodic_definition
            and (
                not definition.quantitative_topic
                or source.kind
                not in {"table_field_label", "table_column_header"}
            )
        )
        or len(normalized_label.rstrip(":")) < 3
    ):
        return False
    normalized_source = _normalize_field_source(source.text)
    label = normalized_label.rstrip(":")
    if not normalized_source.startswith(label) and normalized_source.startswith(
        "本次"
    ):
        normalized_source = normalized_source[len("本次") :]
    if not normalized_source.startswith(label) and "convertible_bond" in set(
        definition.scopes
    ):
        quoted_prefix = _QUOTED_BOND_FIELD_PREFIX_RE.match(normalized_source)
        if quoted_prefix is not None:
            normalized_source = normalized_source[quoted_prefix.end() :]
    if not normalized_source.startswith(label):
        return False
    suffix = normalized_source[len(label) :].strip()
    value = suffix[1:].strip() if suffix.startswith(":") else suffix
    if source.kind in {"table_field_label", "table_column_header"}:
        if _clause_is_reference(suffix) or any(
            suffix.endswith(marker) for marker in ("管理制度", "会计政策")
        ):
            return False
        return True
    if source.kind == "table_text":
        # Visible table segments do not carry a typed header/field-label role.
        # A bare cell (or a prefix such as ``激励对象姓名``) is therefore only
        # lexical/candidate evidence.  Lock it only when the same source atom
        # contains an explicit closed ``label: value`` field.
        return suffix.startswith(":") and bool(value)
    if value:
        return any(
            suffix.startswith(connector)
            and bool(suffix[len(connector) :].strip())
            for connector in (":", "为", "是")
        )
    return any(
        candidate.kind
        in {
            "body_text",
            "table_text",
            "table_field_label",
            "table_column_header",
        }
        for candidate in following_sources
    )


def _is_bond_interest_tax_fact(source: SemanticRouteSource) -> bool:
    """Recognize a current bond-interest tax disposition, not a tax mention."""

    if source.kind not in _LOCAL_CONTENT_SOURCE_KINDS:
        return False
    normalized = _normalize_match_text(source.text)
    has_tax_subject = any(
        marker in normalized
        for marker in ("债券利息所得税", "利息所得税", "应付税项")
    )
    has_disposition = any(
        marker in normalized
        for marker in (
            "代扣代缴",
            "暂免征收",
            "免征",
            "自行缴纳",
            "由持有人承担",
        )
    )
    return has_tax_subject and has_disposition


def _is_incentive_assessment_result(source: SemanticRouteSource) -> bool:
    if source.kind not in _LOCAL_CONTENT_SOURCE_KINDS:
        return False
    normalized = _normalize_match_text(source.text)
    return bool(re.search(r"考核结果(?:为|是|:)[^。；;!?！？]{1,80}", normalized))


def _is_incentive_cancellation_result(source: SemanticRouteSource) -> bool:
    if source.kind not in _LOCAL_CONTENT_SOURCE_KINDS:
        return False
    normalized = _normalize_match_text(source.text)
    has_equity_object = any(
        marker in normalized
        for marker in ("未归属股份", "限制性股票", "激励股份", "激励权益")
    )
    has_action = any(
        marker in normalized for marker in ("予以作废", "作废处理", "已作废")
    )
    return has_equity_object and has_action


def _is_share_buyback_purpose_fact(source: SemanticRouteSource) -> bool:
    if source.kind not in _LOCAL_CONTENT_SOURCE_KINDS:
        return False
    normalized = _normalize_match_text(source.text)
    return bool(
        re.search(
            r"(?:本次)?回购(?:的)?股份(?:用途为|拟用于|将用于|用于)"
            r"[^。；;!?！？]{1,100}",
            normalized,
        )
    )


def _is_resolved_proposal_fact(
    *,
    definition: SemanticRouteDefinition,
    normalized_label: str,
    source: SemanticRouteSource,
) -> bool:
    """Lock a route named inside a formally approved proposal title."""

    if (
        definition.overview_container
        or _PERIODIC_FILING_TYPES.issubset(set(definition.scopes))
        or len(normalized_label) < 4
    ):
        return False
    normalized_source = _normalize_match_text(source.text)
    for match in _RESOLVED_PROPOSAL_RE.finditer(normalized_source):
        proposal = match.group("quoted") or match.group("plain") or ""
        if "关于" in proposal and "议案" in proposal and normalized_label in proposal:
            return True
    return False


def _is_temporal_trigger_reference(
    *,
    source: SemanticRouteSource,
    normalized_label: str,
) -> bool:
    """Reject a route mentioned only as the boundary of a legal time window."""

    normalized_source = _normalize_match_text(source.text)
    trigger = f"进入{normalized_label}之日"
    return trigger in normalized_source and (
        "不得" in normalized_source
        or "禁止" in normalized_source
        or ("自" in normalized_source and "至" in normalized_source)
    )


def _body_candidate_is_context_only(
    *,
    key: str,
    normalized_label: str,
    source: SemanticRouteSource,
    role_responsibility_unit: bool,
) -> bool:
    """Keep governance duties and notice time windows lexical-only.

    These phrases name a power, duty, or filing boundary rather than the
    Unit's disclosed plan/result.  The guard is route-specific and source-
    local so a substantive plan or result elsewhere in the Unit still wins.
    """

    if source.kind not in {"body_text", "table_text"}:
        return False
    normalized = _normalize_match_text(source.text)
    label = normalized_label.rstrip(":")
    relevant_clauses = _label_clauses(normalized, label)
    if not relevant_clauses:
        return False
    if (
        role_responsibility_unit
        and key in {"future_outlook", "profit_distribution_plan"}
    ):
        return all(
            any(marker in clause for marker in ("负责", "职责", "职权", "制订"))
            and not any(
                marker in clause
                for marker in (
                    "已审议",
                    "审议通过",
                    "已制定",
                    "已经制定",
                    "已制订",
                    "已经制订",
                    "形成",
                    "发布",
                    "批准",
                    "实施",
                    "执行",
                )
            )
            for clause in relevant_clauses
        )
    return (
        key in {"share_buyback_completion", "share_capital_change"}
        and all(
            _BUYBACK_RESULT_NOTICE_WINDOW_RE.search(clause) is not None
            for clause in relevant_clauses
        )
    )


def _is_accounting_policy_relation_fact(
    *,
    key: str,
    normalized_label: str,
    source: SemanticRouteSource,
) -> bool:
    """Lock a controlled accounting topic used in a definition or treatment."""

    if key not in _ACCOUNTING_POLICY_RELATION_KEYS or source.kind != "body_text":
        return False
    label = normalized_label.rstrip(":")
    normalized = _normalize_match_text(source.text)
    if label not in normalized:
        return False
    clauses = tuple(
        clause
        for clause in _accounting_relation_clauses(normalized, label)
        if not _clause_is_reference(clause)
    )
    for clause in clauses:
        component_relation = (
            "所得税费用" in clause
            and any(item in clause for item in ("当期所得税", "递延所得税"))
            and any(item in clause for item in ("包括", "组成", "构成"))
        )
        if key == "income_tax_expense" and component_relation:
            return True
        if key == "deferred_tax" and (
            component_relation or _has_near_accounting_action(clause, label)
        ):
            return True
        if key in {"lease_liabilities", "right_of_use_assets"} and (
            _has_near_accounting_action(clause, label)
        ):
            return True
    return False


def _label_clauses(normalized_source: str, label: str) -> tuple[str, ...]:
    """Return punctuation-local spans containing one controlled label."""

    return tuple(
        clause
        for clause in re.split(
            r"[。；;，,！？!?]|(?:并且|同时|但是|但|且)",
            normalized_source,
        )
        if label in clause
    )


def _accounting_relation_clauses(
    normalized_source: str,
    label: str,
) -> tuple[str, ...]:
    """Keep accounting relations while removing comma-local reference tails."""

    clauses: list[str] = []
    for sentence in re.split(r"[。；;！？!?]", normalized_source):
        factual_segments = tuple(
            segment
            for segment in re.split(r"[，,]", sentence)
            if segment and not _clause_is_reference(segment)
        )
        factual_clause = "，".join(factual_segments)
        if label in factual_clause:
            clauses.append(factual_clause)
    return tuple(clauses)


def _has_near_accounting_action(clause: str, label: str) -> bool:
    actions = re.compile(r"(?:重新计量|终止确认|调减|调整|确认|计量|减少|增加)")
    if label in {"租赁负债", "使用权资产"} and _has_collective_lease_action(
        clause
    ):
        return True
    label_spans = tuple(re.finditer(re.escape(label), clause))
    for action in actions.finditer(clause):
        for label_span in label_spans:
            if _label_span_is_excluded(clause, label_span):
                continue
            if action.end() <= label_span.start():
                gap = clause[action.end() : label_span.start()]
                max_gap = 12
            elif label_span.end() <= action.start():
                gap = clause[label_span.end() : action.start()]
                max_gap = 20
            else:
                continue
            if len(gap) <= max_gap and _accounting_action_gap_is_local(gap):
                return True
    return False


def _label_span_is_excluded(clause: str, label_span: re.Match[str]) -> bool:
    if any(
        frame.start() <= label_span.start() and label_span.end() <= frame.end()
        for frame in re.finditer(r"除[^。；，,]{0,60}外", clause)
    ):
        return True
    left = clause[max(0, label_span.start() - 16) : label_span.start()]
    right = clause[label_span.end() : min(len(clause), label_span.end() + 24)]
    if re.search(r"(?:不属于|并非|不是|不包括|不涉及|而非)$", left):
        return True
    if left.endswith("与") and re.match(r"[^。；，,]{0,8}(?:无关|相比|相较)", right):
        return True
    return bool(re.match(r"(?:相比|相较|高于|低于|区别于)", right))


def _has_collective_lease_action(clause: str) -> bool:
    actions = re.compile(r"(?:重新计量|终止确认|调减|调整|确认|计量|减少|增加)")
    connector = r"(?:和|及|与|以及)"
    pair = re.compile(
        rf"(?:租赁负债{connector}使用权资产|"
        rf"使用权资产{connector}租赁负债)"
    )
    pair_spans = tuple(pair.finditer(clause))
    for action in actions.finditer(clause):
        for pair_span in pair_spans:
            if action.end() <= pair_span.start():
                gap = clause[action.end() : pair_span.start()]
                action_before = True
                max_gap = 12
            elif pair_span.end() <= action.start():
                gap = clause[pair_span.end() : action.start()]
                action_before = False
                max_gap = 20
            else:
                continue
            if len(gap) <= max_gap and _collective_accounting_bridge_is_local(
                gap,
                action_before=action_before,
            ):
                return True
    return False


def _collective_accounting_bridge_is_local(
    gap: str,
    *,
    action_before: bool,
) -> bool:
    if action_before:
        return bool(
            re.fullmatch(
                r"(?:相应|同步|共同|分别|同时|一并|予以|相关的|该|本次|上述)*",
                gap,
            )
        )
    return bool(
        re.fullmatch(
            r"(?:均|分别|同时|一并|共同|相应|同步|进行|予以|仍需|进一步|"
            r"无需|不作|账面价值|累计折旧|其|的|已|应|需)*",
            gap,
        )
    )


def _accounting_action_gap_is_local(gap: str) -> bool:
    """Reject list traversal while retaining modifiers of one accounting action."""

    local_gap = gap.replace("及其", "").replace("和其", "")
    if any(
        marker in local_gap
        for marker in ("。", "；", "，", ",", "、", "/", "／", "和", "与", "或", "同")
    ):
        return False
    if any(
        marker in local_gap
        for marker in (
            "不包括",
            "不涉及",
            "不属于",
            "并非",
            "不是",
            "而非",
            "以外",
            "用于说明",
            "无关",
            "相比",
            "相较",
            "高于",
            "低于",
            "区别于",
        )
    ):
        return False
    if "及" in local_gap:
        return False
    if "并" in local_gap and not re.fullmatch(
        r"(?:并)?(?:相应|予以|同步|一并|共同|分别|同时|进行|仍需|进一步|"
        r"账面价值|累计折旧|其|的)*",
        local_gap,
    ):
        return False
    return True


def _clause_is_reference(clause: str) -> bool:
    return any(marker in clause for marker in _REFERENCE_MARKERS) or bool(
        _REFERENCE_SPAN_RE.search(clause)
    )


def _is_closed_debt_structure_cell(source: SemanticRouteSource) -> bool:
    """Accept only a source-bound list of controlled debt line items."""

    if source.kind not in {"table_text", "table_field_label", "table_column_header"}:
        return False
    residue = _normalize_match_text(source.text)
    for label in _INTEREST_BEARING_DEBT_LABELS:
        residue = residue.replace(label, "")
    residue = re.sub(r"[、，,;/／和及与()（）\[\]【】]+", "", residue)
    return not residue


def _is_key_audit_matter_procedure(source: SemanticRouteSource) -> bool:
    """Recognize substantive KAM selection procedure, not a report reference."""

    if source.kind != "body_text":
        return False
    normalized = _normalize_match_text(source.text)
    return "确定" in normalized and "构成关键审计事项" in normalized


def _forecast_range_table_support_ids(
    sources: Sequence[SemanticRouteSource],
) -> tuple[str, ...]:
    """Recognize a standard forecast table with an actual numeric range."""

    table_groups: dict[str, list[SemanticRouteSource]] = {}
    for source in sources:
        if source.kind not in {
            "table_text",
            "table_field_label",
            "table_column_header",
        }:
            continue
        target_group, separator, _value_index = source.source_id.rpartition(":")
        if not separator:
            continue
        table_groups.setdefault(target_group, []).append(source)
    for group in table_groups.values():
        headers = tuple(
            source for source in group if source.kind == "table_column_header"
        )
        header_values = {_normalize_match_text(source.text) for source in headers}
        if not {"项目", "本报告期"}.issubset(header_values):
            continue
        table_values = tuple(source for source in group if source.kind == "table_text")
        metric = next(
            (
                source
                for source in table_values
                if any(
                    marker in _normalize_match_text(source.text)
                    for marker in (
                        "归属于上市公司股东的净利润",
                        "扣除非经常性损益后的净利润",
                        "净利润",
                        "营业收入",
                    )
                )
            ),
            None,
        )
        range_value = next(
            (
                source
                for source in table_values
                if re.search(
                    r"(?:"
                    r"\d[\d,.，]*(?:亿元|万元|元/股|元|%)"
                    r"[-—–－至到~～][+\-－−]?\d"
                    r"|"
                    r"\d[\d,.，]*[-—–－至到~～][+\-－−]?\d[\d,.，]*"
                    r"(?:亿元|万元|元/股|元|%)"
                    r")",
                    _normalize_match_text(source.text),
                )
            ),
            None,
        )
        if metric is None or range_value is None:
            continue
        relevant_headers = tuple(
            source.source_id
            for source in headers
            if _normalize_match_text(source.text) in {"项目", "本报告期"}
        )
        return tuple(
            dict.fromkeys((*relevant_headers, metric.source_id, range_value.source_id))
        )
    return ()


def _incentive_adjustment_support_ids(
    sources: Sequence[SemanticRouteSource],
) -> tuple[str, ...]:
    """Lock a quantity-and-price adjustment only with Unit-local facts."""

    title = next(
        (
            source
            for source in sources
            if source.kind == "unit_title"
            and "调整" in _normalize_match_text(source.text)
            and any(
                marker in _normalize_match_text(source.text)
                for marker in ("激励计划", "股票期权", "限制性股票")
            )
            and "授予数量" in _normalize_match_text(source.text)
            and any(
                marker in _normalize_match_text(source.text)
                for marker in ("授予价格", "行权价格")
            )
        ),
        None,
    )
    if title is None:
        return ()
    local_sources = tuple(
        source for source in sources if source.kind in _LOCAL_CONTENT_SOURCE_KINDS
    )
    combined = "".join(_normalize_match_text(source.text) for source in local_sources)
    has_quantity = any(
        marker in combined
        for marker in ("授予数量", "股票期权数量", "股票期权总量", "激励计划总量")
    )
    has_price = any(marker in combined for marker in ("授予价格", "行权价格"))
    has_transition = (
        ("调整前" in combined and "调整后" in combined)
        or ("更正前" in combined and "更正后" in combined)
        or bool(re.search(r"由[^。；]{0,80}调整为", combined))
    )
    witnesses = tuple(
        source
        for source in local_sources
        if re.search(r"\d", source.text)
        and any(
            marker in _normalize_match_text(source.text)
            for marker in ("调整前", "调整后", "更正前", "更正后", "调整为")
        )
    )
    if not (has_quantity and has_price and has_transition and witnesses):
        return ()
    return tuple(dict.fromkeys((title.source_id, *(item.source_id for item in witnesses))))


def _transaction_pricing_support_ids(
    sources: Sequence[SemanticRouteSource],
) -> tuple[str, ...]:
    """Recognize recurring transaction-pricing sections with local evidence."""

    title = next(
        (
            source
            for source in sources
            if source.kind == "unit_title"
            and re.fullmatch(
                r"(?:关联交易(?:的)?|许可协议|知识产权转让(?:项目)?|交易)定价情况",
                _normalize_title(source.text),
            )
        ),
        None,
    )
    if title is None:
        return ()
    witnesses = tuple(
        source
        for source in sources
        if source.kind in _LOCAL_CONTENT_SOURCE_KINDS
        and not _clause_is_reference(_normalize_match_text(source.text))
        and any(
            marker in _normalize_match_text(source.text)
            for marker in (
                "评估值",
                "作价",
                "收益法",
                "成本法",
                "市场法",
                "定价依据",
                "交易价格",
                "公允",
            )
        )
    )
    if not witnesses:
        return ()
    return tuple(dict.fromkeys((title.source_id, *(item.source_id for item in witnesses))))


def _major_contract_value_support_ids(
    sources: Sequence[SemanticRouteSource],
) -> tuple[str, ...]:
    """Return Unit-local contract payment facts with an adjacent value."""

    amount = r"(?:人民币)?[+\-－−]?[0-9０-９][0-9０-９,.，]*"
    currency = r"(?:亿元|万元|元|%|％)"
    patterns = tuple(
        re.compile(pattern)
        for pattern in (
            rf"交易对价包括[^。；;]{{0,24}}首付款[^。；;]{{0,8}}{amount}{currency}",
            rf"首付款(?:合计|总额|金额)?(?:为|:)?{amount}{currency}",
            rf"权利金(?:金额)?为[^。；;]{{0,80}}{amount}(?:%|％)",
            rf"技术转让费(?:金额)?为[^。；;]{{0,8}}{amount}(?:亿元|万元|元)",
            rf"转让总价格为[^。；;]{{0,8}}{amount}(?:亿元|万元|元)",
        )
    )
    support: list[str] = []
    for source in sources:
        if source.kind not in _LOCAL_CONTENT_SOURCE_KINDS:
            continue
        normalized = _normalize_match_text(source.text)
        if _clause_is_reference(normalized):
            continue
        if any(pattern.search(normalized) for pattern in patterns):
            support.append(source.source_id)
    return tuple(dict.fromkeys(support))


def _hash_json(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _normalize_title(value: str) -> str:
    core = _TITLE_EDGE_QUOTES_RE.sub("", value.strip())
    core = _TITLE_BULLET_RE.sub("", core)
    core = _TITLE_NUMBERING_RE.sub("", core).strip().rstrip("：: ")
    core = _CONTINUATION_RE.sub("", core).strip().rstrip("：: ")
    # Checkbox applicability markers glued to a template heading are source
    # noise, not topic words; a bare 适用/不适用 without its checkbox glyph is
    # left untouched.
    core = _APPLICABILITY_SUFFIX_RE.sub("", core).strip().rstrip("：: ")
    core = _TITLE_PREFIX_RE.sub("", core).strip()
    core = _TITLE_YEAR_PREFIX_RE.sub("", core).strip()
    core = _TITLE_EDGE_QUOTES_RE.sub("", core.strip())
    normalized = _normalize_text(core).replace("及", "和").replace("与", "和")
    if _NAMED_AGREEMENT_CONTENTS_RE.fullmatch(normalized):
        return "协议主要内容"
    return normalized


def _normalize_text(value: str) -> str:
    return _SPACE_RE.sub("", value).replace("：", ":").lower()


def _normalize_match_text(value: str) -> str:
    return _normalize_text(value).replace("及", "和").replace("与", "和")


def _normalize_field_source(value: str) -> str:
    stripped = re.sub(
        r"^\s*[\ue000-\uf8ff●•▪◼■◆◇√□☑☒*\-—–－]+\s*",
        "",
        value,
    )
    stripped = _TITLE_NUMBERING_RE.sub("", stripped).strip()
    return _normalize_match_text(stripped)


def _title_is_exact_field(title: str, label: str) -> bool:
    """Match a standard heading or a heading followed by its field value."""

    return (
        title == label
        or (len(label) >= 2 and title.startswith(f"{label}:"))
        or (
            # Periodic templates prefix the same closed heading with the
            # reporting-period deixis.  Only the whole remainder may match a
            # label, so 报告期内部控制… can never be misread as 内部控制….
            len(label) >= 4
            and title == f"报告期内{label}"
        )
        or (
            len(label) >= 4
            and title.startswith("关于")
            and title.endswith(label)
            and (
                title == f"关于{label}"
                or "的" in title[: -len(label)]
            )
        )
    )


def _title_similarity(title: str, label: str) -> float:
    """Return a conservative Chinese-heading recall score, never a decision."""

    title_chars = "".join(character for character in title if character.isalnum())
    label_chars = "".join(character for character in label if character.isalnum())
    if min(len(title_chars), len(label_chars)) < 4:
        return 0.0
    title_unigrams = set(title_chars)
    label_unigrams = set(label_chars)
    label_coverage = len(title_unigrams & label_unigrams) / len(label_unigrams)
    title_bigrams = {
        title_chars[index : index + 2] for index in range(len(title_chars) - 1)
    }
    label_bigrams = {
        label_chars[index : index + 2] for index in range(len(label_chars) - 1)
    }
    bigram_dice = (
        2 * len(title_bigrams & label_bigrams)
        / (len(title_bigrams) + len(label_bigrams))
    )
    return 0.6 * label_coverage + 0.4 * bigram_dice


def _compact_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip()[:_MAX_SOURCE_TEXT]


def _similarity_only_candidate(candidate: SemanticRouteCandidate) -> bool:
    return set(candidate.evidence_kinds).issubset(
        {"source_heading_similarity", "document_context_candidate"}
    )


__all__ = [
    "SemanticRouteBatchResult",
    "SemanticRouter",
    "semantic_document_context",
]
