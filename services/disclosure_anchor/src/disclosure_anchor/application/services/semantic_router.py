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
from disclosure_anchor.application.contracts.provider_unit import ProviderUnitDraft
from disclosure_anchor.application.contracts.semantic_routes import (
    MAX_SEMANTIC_CANDIDATES,
    SEMANTIC_FALLBACK_KEY,
    SEMANTIC_ROUTER_VERSION,
    SemanticAdjudicatedRoute,
    SemanticAdjudicationDecision,
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
_TITLE_NUMBERING_RE = re.compile(
    rf"^\s*(?:"
    rf"第{_CN_ORDINAL}[节章]\s*|"
    rf"[（(](?:\d{{1,3}}|{_CN_ORDINAL})[）)]\s*|"
    rf"{_CN_ORDINAL}\s*、\s*|"
    rf"\d{{1,3}}(?:[.．]\d{{1,3}})+\s+|"
    rf"\d{{1,3}}\s*(?:、|[.．](?!\d)|[)）])\s*|"
    rf"\d{{1,3}}\s+(?=\S)"
    rf")"
)
_CONTINUATION_RE = re.compile(r"\s*(?:[（(]\s*续\s*[）)]|[-—–－]\s*续)\s*$")
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


@dataclass(frozen=True, slots=True)
class SemanticRouteBatchResult:
    units: tuple[ProviderUnitDraft, ...]
    receipts: tuple[SemanticRouteReceipt, ...]

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
        adjudicator: SemanticRouteAdjudicatorPort,
        cache: SemanticRouteCachePort,
        batch_size: int = 16,
    ) -> None:
        if batch_size < 1 or batch_size > 32:
            raise ValueError("semantic adjudication batch size must be 1..32")
        self.taxonomy = taxonomy
        self.adjudicator = adjudicator
        self.cache = cache
        self.batch_size = batch_size
        self._definitions = taxonomy.by_key()
        self._normalized_labels = {
            item.key: tuple(dict.fromkeys(_normalize_title(label) for label in item.labels))
            for item in taxonomy.definitions
            if item.key != taxonomy.fallback_key
        }
        self._composite_sections = {
            _normalize_title(item.label): item.keys
            for item in taxonomy.composite_sections
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
        model_inputs: list[SemanticRouteUnitInput] = []
        for unit_input in inputs:
            deterministic = self._deterministic_candidates(unit_input)
            requires_model = self._requires_model(
                unit_input,
                document=document,
            )
            if deterministic:
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
                    decision = cached_decisions[unit_input.unit_index]
                    assert decision is not None
                    decision = self._canonicalize_decision(unit_input, decision)
                    self._validate_decision(unit_input, decision)
                    receipts[unit_input.unit_index] = self._model_receipt(
                        unit_input=unit_input,
                        decision=decision,
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
        return SemanticRouteBatchResult(units=routed, receipts=ordered_receipts)

    def _adjudicate_batch(
        self,
        *,
        document: SemanticDocumentContext,
        requested: tuple[SemanticRouteUnitInput, ...],
    ) -> dict[int, SemanticAdjudicationDecision]:
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
        expected_model_group_hashes: dict[int, str] = {}
        model_inputs = tuple(
            unit_input
            for unit_input in inputs
            if self._requires_model(unit_input, document=document)
        )
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
        candidates = self._candidates(
            document=document,
            unit_index=draft.unit_index,
            sources=sources,
            section_keys=section_keys,
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
                    for normalized_label in self._normalized_labels["definitions"]
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
                        for label in self._normalized_labels[definition.key]
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
                        for label in self._normalized_labels[definition.key]
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
            if (
                definition.key == self.taxonomy.fallback_key
                or definition.context_container
                or (
                    is_definitions_context
                    and definition.key != "definitions"
                )
            ):
                continue
            labels = self._normalized_labels[definition.key]
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
                        and min(len(title_core), len(label)) >= 4
                        and (label in title_core or title_core in label)
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
                        and not _is_temporal_trigger_reference(
                            source=source,
                            normalized_label=label,
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
                        definition_state.add(
                            score=(
                                900
                                if (
                                    quantitative_topic
                                    or resolved_proposal_exact
                                    or labeled_field_exact
                                )
                                else 300 + min(len(label), 40)
                            ),
                            source_id=source.source_id,
                            evidence_kind=(
                                "source_resolved_proposal_exact"
                                if resolved_proposal_exact
                                else (
                                    "source_labeled_field_exact"
                                    if labeled_field_exact
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
                        for label in labels
                    )
                ):
                    similarity = max(
                        _title_similarity(title_core, surface)
                        for surface in (
                            *labels,
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
                and "source_heading_candidate" in evidence
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

        if not any(source.kind in {"body_text", "table_text"} for source in sources):
            return None
        classification_scopes = {
            value
            for value in (document.filing_type, *document.disclosure_topics)
            if value
        }
        keys: list[str] = []
        for heading in draft.heading_path:
            title_core = _normalize_title(heading)
            composite = self._composite_sections.get(title_core)
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
                    (definition.context_container or definition.section_container)
                    and (
                        not definition.scopes
                        or bool(set(definition.scopes) & classification_scopes)
                    )
                    and any(
                        _title_is_exact_field(title_core, label)
                        for label in self._normalized_labels[definition.key]
                    )
                )
            }
            if len(matching) > 1:
                raise SemanticRouteContractError(
                    "one source heading maps to multiple normalized sections"
                )
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
            if any(
                _title_is_exact_field(title, label)
                for key in overview_keys
                for label in self._normalized_labels[key]
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
        else:
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

    def _deterministic_receipt(
        self,
        unit_input: SemanticRouteUnitInput,
        selected: tuple[SemanticRouteCandidate, ...],
    ) -> SemanticRouteReceipt:
        keys = tuple(item.key for item in selected)
        return SemanticRouteReceipt(
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
        return not self._deterministic_candidates(
            unit_input
        ) and _semantic_input_requires_model(
            unit_input,
            document=document,
        )

    def _fallback_receipt(
        self,
        unit_input: SemanticRouteUnitInput,
    ) -> SemanticRouteReceipt:
        return SemanticRouteReceipt(
            taxonomy_version=self.taxonomy.version,
            router_version=SEMANTIC_ROUTER_VERSION,
            input_hash=unit_input.input_hash,
            candidate_keys=(),
            semantic_keys=(SEMANTIC_FALLBACK_KEY,),
            decision_source="fallback",
            evidence=(
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
        return SemanticRouteReceipt(
            taxonomy_version=self.taxonomy.version,
            router_version=SEMANTIC_ROUTER_VERSION,
            input_hash=unit_input.input_hash,
            candidate_keys=tuple(item.key for item in unit_input.candidates),
            semantic_keys=(SEMANTIC_FALLBACK_KEY,),
            decision_source="rule_abstain",
            evidence=(
                SemanticRouteEvidence(
                    key=SEMANTIC_FALLBACK_KEY,
                    kinds=("fallback",),
                    source_ids=(),
                ),
            ),
        )

    def _model_receipt(
        self,
        *,
        unit_input: SemanticRouteUnitInput,
        decision: SemanticAdjudicationDecision,
        cache_key: str,
        cache_hit: bool,
    ) -> SemanticRouteReceipt:
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
        identity = self.adjudicator.identity
        return _cache_key_for_identity(
            input_hash=input_hash,
            group_hash=group_hash,
            adapter=identity.adapter,
            model=identity.model,
            prompt_version=identity.prompt_version,
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
    if document.filing_type in _PERIODIC_FILING_TYPES:
        return False
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
        # Exact routes are already sufficient L1 routing facts.  Optional body
        # secondaries remain available to lexical retrieval; paying for a
        # model to enrich them made the adjudicator the main event-filing path
        # without improving the Unit's primary retrieval owner.
        return False
    evidence = tuple(
        set(candidate.evidence_kinds) for candidate in unit_input.candidates
    )
    has_controlled_heading = any(
        "source_heading_candidate" in kinds for kinds in evidence
    )
    has_unit_content = any(
        kinds & {"source_body_candidate", "source_table_candidate"}
        for kinds in evidence
    )
    # A model may resolve only a genuine within-Unit conflict: the closed
    # shortlist must contain controlled heading evidence and local body/table
    # evidence, but neither side has already established a locked route.  The
    # evidence may support different candidates—that is the ambiguity the
    # model is for.  Pure body collisions and fuzzy-title recall remain lexical.
    return has_controlled_heading and has_unit_content


def _semantic_adjudication_groups(
    inputs: Sequence[SemanticRouteUnitInput],
    *,
    batch_size: int,
) -> tuple[tuple[SemanticRouteUnitInput, ...], ...]:
    return tuple(
        tuple(inputs[offset : offset + batch_size])
        for offset in range(0, len(inputs), batch_size)
    )


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
    part_kind_by_source = {
        source_index: part.kind
        for part in draft.locator.parts
        for source_index in part.block_source_indices
    }
    for binding in draft.locator.search_targets:
        if binding.destination.kind == "unit_title":
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
        if receipt.semantic_keys == (SEMANTIC_FALLBACK_KEY,)
        else receipt.semantic_keys
    )
    hashes = compute_unit_hashes(
        payload_kind=draft.payload_kind,
        payload=draft.payload,
        title=draft.title,
        heading_path=list(draft.heading_path),
        semantic_key=None if semantic_keys is None else semantic_keys[0],
        semantic_keys=None if semantic_keys is None else list(semantic_keys),
        section_keys=None if section_keys is None else list(section_keys),
        applicability=draft.applicability,
        quality_status=draft.quality_status,
        order_index=draft.unit_index + 1,
    )
    return replace(
        draft,
        semantic_key=None if semantic_keys is None else semantic_keys[0],
        semantic_keys=semantic_keys,
        section_keys=section_keys,
        content_hash=hashes.content_hash,
        query_projection_hash=hashes.query_projection_hash,
        structure_hash=hashes.structure_hash,
    )


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
        "变动幅度为",
    )
    if label.endswith(connectors):
        direct_result = re.compile(re.escape(label) + value)
    else:
        connector = "|".join(
            re.escape(item) for item in sorted(connectors, key=len, reverse=True)
        )
        link = rf"(?::)?(?:(?:{connector}))?"
        direct_result = re.compile(re.escape(label) + link + value)
    matches = list(direct_result.finditer(normalized_source))
    if set(definition.scopes) == _PERIODIC_FILING_TYPES:
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

    periodic_definition = set(definition.scopes) == _PERIODIC_FILING_TYPES

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
    if not normalized_source.startswith(label):
        return False
    suffix = normalized_source[len(label) :].strip()
    value = suffix[1:].strip() if suffix.startswith(":") else suffix
    if source.kind in {"table_field_label", "table_column_header"}:
        return True
    if source.kind == "table_text":
        # Visible table segments do not carry a typed header/field-label role.
        # A bare cell (or a prefix such as ``激励对象姓名``) is therefore only
        # lexical/candidate evidence.  Lock it only when the same source atom
        # contains an explicit closed ``label: value`` field.
        return suffix.startswith(":") and bool(value)
    if value:
        return suffix.startswith(":")
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


def _is_resolved_proposal_fact(
    *,
    definition: SemanticRouteDefinition,
    normalized_label: str,
    source: SemanticRouteSource,
) -> bool:
    """Lock a route named inside a formally approved proposal title."""

    if (
        definition.overview_container
        or set(definition.scopes) == _PERIODIC_FILING_TYPES
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


def _hash_json(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _normalize_title(value: str) -> str:
    core = _TITLE_NUMBERING_RE.sub("", value).strip().rstrip("：: ")
    core = _CONTINUATION_RE.sub("", core).strip().rstrip("：: ")
    core = _TITLE_PREFIX_RE.sub("", core).strip()
    core = _TITLE_YEAR_PREFIX_RE.sub("", core).strip()
    return _normalize_text(core).replace("及", "和").replace("与", "和")


def _normalize_text(value: str) -> str:
    return _SPACE_RE.sub("", value).replace("：", ":").lower()


def _normalize_match_text(value: str) -> str:
    return _normalize_text(value).replace("及", "和").replace("与", "和")


def _normalize_field_source(value: str) -> str:
    stripped = re.sub(r"^\s*[●•▪◼■◆◇√□☑☒*\-—–－]+\s*", "", value)
    stripped = _TITLE_NUMBERING_RE.sub("", stripped).strip()
    return _normalize_match_text(stripped)


def _title_is_exact_field(title: str, label: str) -> bool:
    """Match a standard heading or a heading followed by its field value."""

    return (
        title == label
        or (len(label) >= 2 and title.startswith(f"{label}:"))
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
