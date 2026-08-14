"""Closed semantic-route contracts for non-embedding Unit retrieval."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Literal, cast, get_args


SEMANTIC_ROUTE_RECEIPT_VERSION = "semantic_route_receipt.v1"
SEMANTIC_ROUTE_RECEIPTS_FILENAME = "semantic_route_receipts.v1.jsonl"
SEMANTIC_ROUTER_VERSION = "semantic_router.v53"
SEMANTIC_PROMPT_VERSION = "semantic_route_adjudication.v31"
SEMANTIC_FALLBACK_KEY = "document_content"
MAX_SEMANTIC_ROUTES = 8
MAX_SEMANTIC_ROUTES_PER_UNIT = MAX_SEMANTIC_ROUTES
MAX_SEMANTIC_CANDIDATES = 8

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

SemanticRouteDecisionSource = Literal[
    "deterministic",
    "rule_abstain",
    "model",
    "model_abstain",
    "fallback",
]
SemanticRouteSourceKind = Literal[
    "unit_title",
    "heading_path",
    "body_text",
    "table_text",
    "table_field_label",
    "table_column_header",
    "document_title",
    "document_filing_type",
    "document_topic",
    "document_category",
]
SemanticRouteEvidenceKind = Literal[
    "source_heading_exact",
    "source_heading_candidate",
    "source_heading_similarity",
    "source_body_candidate",
    "source_table_candidate",
    "source_labeled_field_exact",
    "source_quantitative_exact",
    "source_resolved_proposal_exact",
    "document_context_candidate",
    "model_adjudicated",
    "fallback",
]


class SemanticRouteContractError(ValueError):
    """Semantic route input, receipt, or adjudication is not closed."""


@dataclass(frozen=True, slots=True)
class SemanticRouteDefinition:
    """One member of the versioned closed vocabulary."""

    key: str
    description: str
    labels: tuple[str, ...]
    scopes: tuple[str, ...] = ()
    exclusive_container: bool = False
    overview_container: bool = False
    context_container: bool = False
    quantitative_fact: bool = False

    def __post_init__(self) -> None:
        if not _KEY_RE.fullmatch(self.key):
            raise SemanticRouteContractError(f"invalid semantic route key: {self.key}")
        if not self.description.strip() or not self.labels:
            raise SemanticRouteContractError(
                f"semantic route {self.key} needs a description and label"
            )
        if any(not label.strip() for label in self.labels):
            raise SemanticRouteContractError(
                f"semantic route {self.key} has an empty label"
            )
        if len(self.labels) != len(set(self.labels)):
            raise SemanticRouteContractError(
                f"semantic route {self.key} repeats a label"
            )
        if len(self.scopes) != len(set(self.scopes)):
            raise SemanticRouteContractError(
                f"semantic route {self.key} repeats a scope"
            )
        if self.context_container and (
            self.exclusive_container or self.overview_container
        ):
            raise SemanticRouteContractError(
                f"semantic route {self.key} cannot mix context and event containers"
            )
        if not isinstance(self.quantitative_fact, bool):
            raise SemanticRouteContractError(
                f"semantic route {self.key} quantitative-fact policy must be boolean"
            )


@dataclass(frozen=True, slots=True)
class SemanticRouteTaxonomy:
    """The complete vocabulary consumed by one router version."""

    version: str
    definitions: tuple[SemanticRouteDefinition, ...]
    fallback_key: str = SEMANTIC_FALLBACK_KEY

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.definitions:
            raise SemanticRouteContractError("semantic taxonomy is incomplete")
        keys = [item.key for item in self.definitions]
        if len(keys) != len(set(keys)):
            raise SemanticRouteContractError("semantic taxonomy repeats a key")
        context_definitions = tuple(
            item for item in self.definitions if item.context_container
        )
        for index, left in enumerate(context_definitions):
            left_labels = {_normalize_context_label(label) for label in left.labels}
            for right in context_definitions[index + 1 :]:
                scopes_overlap = (
                    not left.scopes
                    or not right.scopes
                    or bool(set(left.scopes) & set(right.scopes))
                )
                if scopes_overlap and left_labels & {
                    _normalize_context_label(label) for label in right.labels
                }:
                    raise SemanticRouteContractError(
                        "semantic context labels collide within one scope"
                    )

    def by_key(self) -> dict[str, SemanticRouteDefinition]:
        return {item.key: item for item in self.definitions}


def _normalize_context_label(value: str) -> str:
    return "".join(value.split()).replace("：", ":").casefold()


@dataclass(frozen=True, slots=True)
class SemanticDocumentContext:
    """Document-level priors; none may become a Unit route by itself."""

    title: str | None
    filing_type: str | None
    disclosure_topics: tuple[str, ...] = ()
    content_categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for values, label in (
            (self.disclosure_topics, "disclosure topic"),
            (self.content_categories, "content category"),
        ):
            if len(values) != len(set(values)) or any(not value.strip() for value in values):
                raise SemanticRouteContractError(
                    f"semantic document {label} values must be unique and nonempty"
                )


@dataclass(frozen=True, slots=True)
class SemanticRouteSource:
    """One stable, replayable text witness presented to the adjudicator."""

    source_id: str
    kind: SemanticRouteSourceKind
    text: str

    def __post_init__(self) -> None:
        if not self.source_id or not self.text.strip():
            raise SemanticRouteContractError("semantic route source is empty")
        if self.kind not in {
            "unit_title",
            "heading_path",
            "body_text",
            "table_text",
            "table_field_label",
            "table_column_header",
            "document_title",
            "document_filing_type",
            "document_topic",
            "document_category",
        }:
            raise SemanticRouteContractError("semantic route source kind is unsupported")


@dataclass(frozen=True, slots=True)
class SemanticRouteCandidate:
    """One bounded candidate; candidate presence is not a route decision."""

    key: str
    source_ids: tuple[str, ...]
    evidence_kinds: tuple[SemanticRouteEvidenceKind, ...]
    locked: bool = False

    def __post_init__(self) -> None:
        if not _KEY_RE.fullmatch(self.key):
            raise SemanticRouteContractError("semantic candidate key is invalid")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise SemanticRouteContractError("semantic candidate repeats a source")
        if not self.evidence_kinds:
            raise SemanticRouteContractError("semantic candidate has no evidence kind")
        if len(self.evidence_kinds) != len(set(self.evidence_kinds)):
            raise SemanticRouteContractError("semantic candidate repeats evidence")


@dataclass(frozen=True, slots=True)
class SemanticRouteUnitInput:
    """Canonical per-Unit input used for routing and cache identity."""

    unit_index: int
    input_hash: str
    sources: tuple[SemanticRouteSource, ...]
    candidates: tuple[SemanticRouteCandidate, ...]

    def __post_init__(self) -> None:
        if self.unit_index < 0 or not _SHA256_RE.fullmatch(self.input_hash):
            raise SemanticRouteContractError("semantic Unit input identity is invalid")
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise SemanticRouteContractError("semantic Unit input repeats a source")
        candidate_keys = [candidate.key for candidate in self.candidates]
        if len(candidate_keys) != len(set(candidate_keys)):
            raise SemanticRouteContractError("semantic Unit input repeats a candidate")
        if len(candidate_keys) > MAX_SEMANTIC_CANDIDATES:
            raise SemanticRouteContractError("semantic Unit input has too many candidates")
        known_sources = set(source_ids)
        if any(
            not set(candidate.source_ids).issubset(known_sources)
            for candidate in self.candidates
        ):
            raise SemanticRouteContractError(
                "semantic candidate cites an unknown source"
            )


@dataclass(frozen=True, slots=True)
class SemanticAdjudicatedRoute:
    """One selected closed-vocabulary route and its cited witnesses."""

    key: str
    support_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _KEY_RE.fullmatch(self.key) or not self.support_ids:
            raise SemanticRouteContractError("adjudicated route is incomplete")
        if len(self.support_ids) != len(set(self.support_ids)):
            raise SemanticRouteContractError("adjudicated route repeats support")


@dataclass(frozen=True, slots=True)
class SemanticAdjudicationDecision:
    """Model/cache result for one requested Unit; empty routes mean abstain."""

    unit_index: int
    routes: tuple[SemanticAdjudicatedRoute, ...]

    def __post_init__(self) -> None:
        if self.unit_index < 0 or len(self.routes) > MAX_SEMANTIC_ROUTES_PER_UNIT:
            raise SemanticRouteContractError("semantic adjudication size is invalid")
        keys = [route.key for route in self.routes]
        if len(keys) != len(set(keys)):
            raise SemanticRouteContractError("semantic adjudication repeats a route")


@dataclass(frozen=True, slots=True)
class SemanticAdjudicatorMetadata:
    """Auditable identity for a model decision or its exact cache replay."""

    adapter: str
    model: str
    prompt_version: str
    cache_key: str
    response_sha256: str
    cache_hit: bool

    def __post_init__(self) -> None:
        if not all((self.adapter, self.model, self.prompt_version)):
            raise SemanticRouteContractError("semantic adjudicator identity is incomplete")
        if not _SHA256_RE.fullmatch(self.cache_key) or not _SHA256_RE.fullmatch(
            self.response_sha256
        ):
            raise SemanticRouteContractError("semantic adjudicator hash is invalid")


@dataclass(frozen=True, slots=True)
class SemanticRouteEvidence:
    """Why one selected route is admissible."""

    key: str
    kinds: tuple[SemanticRouteEvidenceKind, ...]
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _KEY_RE.fullmatch(self.key) or not self.kinds:
            raise SemanticRouteContractError("semantic route evidence is incomplete")
        if len(self.kinds) != len(set(self.kinds)):
            raise SemanticRouteContractError("semantic route evidence repeats a kind")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise SemanticRouteContractError("semantic route evidence repeats a source")


@dataclass(frozen=True, slots=True)
class SemanticRouteReceipt:
    """Frozen per-Unit decision stored only in the private build snapshot."""

    taxonomy_version: str
    router_version: str
    input_hash: str
    candidate_keys: tuple[str, ...]
    semantic_keys: tuple[str, ...]
    decision_source: SemanticRouteDecisionSource
    evidence: tuple[SemanticRouteEvidence, ...]
    adjudicator: SemanticAdjudicatorMetadata | None = None
    contract_version: str = SEMANTIC_ROUTE_RECEIPT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != SEMANTIC_ROUTE_RECEIPT_VERSION:
            raise SemanticRouteContractError("semantic route receipt version is unsupported")
        if not self.taxonomy_version or self.router_version != SEMANTIC_ROUTER_VERSION:
            raise SemanticRouteContractError("semantic route receipt identity is invalid")
        if not _SHA256_RE.fullmatch(self.input_hash):
            raise SemanticRouteContractError("semantic route receipt input hash is invalid")
        if len(self.candidate_keys) != len(set(self.candidate_keys)):
            raise SemanticRouteContractError("semantic route receipt repeats a candidate")
        if not self.semantic_keys or len(self.semantic_keys) > MAX_SEMANTIC_ROUTES:
            raise SemanticRouteContractError("semantic route receipt needs routes")
        if len(self.semantic_keys) != len(set(self.semantic_keys)):
            raise SemanticRouteContractError("semantic route receipt repeats a route")
        evidence_keys = tuple(item.key for item in self.evidence)
        if evidence_keys != self.semantic_keys:
            raise SemanticRouteContractError(
                "semantic route receipt evidence must follow every selected route"
            )
        if self.decision_source in {"model", "model_abstain"}:
            if self.adjudicator is None:
                raise SemanticRouteContractError("model route receipt needs adjudicator metadata")
            if self.decision_source == "model" and any(
                key not in self.candidate_keys for key in self.semantic_keys
            ):
                raise SemanticRouteContractError("model selected a non-candidate route")
        elif self.adjudicator is not None:
            raise SemanticRouteContractError(
                "non-model route receipt cannot claim adjudicator metadata"
            )
        if self.decision_source in {"fallback", "rule_abstain", "model_abstain"}:
            if self.semantic_keys != (SEMANTIC_FALLBACK_KEY,):
                raise SemanticRouteContractError("fallback receipt must be fallback-only")
        elif SEMANTIC_FALLBACK_KEY in self.semantic_keys:
            raise SemanticRouteContractError(
                "narrow semantic routes cannot include the fallback key"
            )

    @property
    def semantic_key(self) -> str:
        return self.semantic_keys[0]


@dataclass(frozen=True, slots=True)
class SemanticRouteReceiptRow:
    """One receipt bound to the exact persisted Unit snapshot row."""

    asset_id: str
    order_index: int
    receipt: SemanticRouteReceipt

    def __post_init__(self) -> None:
        if not self.asset_id or self.order_index < 1:
            raise SemanticRouteContractError("semantic receipt row identity is invalid")


def semantic_route_receipt_row_to_payload(
    row: SemanticRouteReceiptRow,
) -> dict[str, object]:
    return {
        "asset_id": row.asset_id,
        "order_index": row.order_index,
        "semantic_route": semantic_route_receipt_to_payload(row.receipt),
    }


def semantic_route_receipt_row_from_payload(payload: object) -> SemanticRouteReceiptRow:
    root = _closed_mapping(
        payload,
        fields={"asset_id", "order_index", "semantic_route"},
        label="semantic receipt row",
    )
    order_index = root["order_index"]
    if type(order_index) is not int or order_index < 1:
        raise SemanticRouteContractError("semantic receipt order_index is invalid")
    return SemanticRouteReceiptRow(
        asset_id=_text(root["asset_id"], label="semantic receipt asset_id"),
        order_index=order_index,
        receipt=semantic_route_receipt_from_payload(root["semantic_route"]),
    )


def semantic_route_receipt_to_payload(receipt: SemanticRouteReceipt) -> dict[str, object]:
    """Serialize a receipt as a closed private snapshot object."""

    return {
        "adjudicator": (
            None
            if receipt.adjudicator is None
            else {
                "adapter": receipt.adjudicator.adapter,
                "cache_hit": receipt.adjudicator.cache_hit,
                "cache_key": receipt.adjudicator.cache_key,
                "model": receipt.adjudicator.model,
                "prompt_version": receipt.adjudicator.prompt_version,
                "response_sha256": receipt.adjudicator.response_sha256,
            }
        ),
        "candidate_keys": list(receipt.candidate_keys),
        "contract_version": receipt.contract_version,
        "decision_source": receipt.decision_source,
        "evidence": [
            {
                "key": item.key,
                "kinds": list(item.kinds),
                "source_ids": list(item.source_ids),
            }
            for item in receipt.evidence
        ],
        "input_hash": receipt.input_hash,
        "router_version": receipt.router_version,
        "semantic_keys": list(receipt.semantic_keys),
        "taxonomy_version": receipt.taxonomy_version,
    }


def semantic_route_receipt_from_payload(payload: object) -> SemanticRouteReceipt:
    """Decode one private snapshot receipt with no legacy fallback."""

    root = _closed_mapping(
        payload,
        fields={
            "adjudicator",
            "candidate_keys",
            "contract_version",
            "decision_source",
            "evidence",
            "input_hash",
            "router_version",
            "semantic_keys",
            "taxonomy_version",
        },
        label="semantic route receipt",
    )
    raw_adjudicator = root["adjudicator"]
    adjudicator = None
    if raw_adjudicator is not None:
        item = _closed_mapping(
            raw_adjudicator,
            fields={
                "adapter",
                "cache_hit",
                "cache_key",
                "model",
                "prompt_version",
                "response_sha256",
            },
            label="semantic adjudicator metadata",
        )
        cache_hit = item["cache_hit"]
        if type(cache_hit) is not bool:
            raise SemanticRouteContractError("semantic cache_hit must be boolean")
        adjudicator = SemanticAdjudicatorMetadata(
            adapter=_text(item["adapter"], label="semantic adapter"),
            model=_text(item["model"], label="semantic model"),
            prompt_version=_text(item["prompt_version"], label="semantic prompt"),
            cache_key=_text(item["cache_key"], label="semantic cache key"),
            response_sha256=_text(
                item["response_sha256"], label="semantic response hash"
            ),
            cache_hit=cache_hit,
        )
    evidence = tuple(
        _evidence_from_payload(item)
        for item in _array(root["evidence"], label="semantic route evidence")
    )
    decision_source = _text(root["decision_source"], label="decision source")
    if decision_source not in {
        "deterministic",
        "rule_abstain",
        "model",
        "model_abstain",
        "fallback",
    }:
        raise SemanticRouteContractError("semantic decision source is unsupported")
    return SemanticRouteReceipt(
        contract_version=_text(root["contract_version"], label="receipt version"),
        taxonomy_version=_text(root["taxonomy_version"], label="taxonomy version"),
        router_version=_text(root["router_version"], label="router version"),
        input_hash=_text(root["input_hash"], label="semantic input hash"),
        candidate_keys=tuple(
            _text(item, label="candidate key")
            for item in _array(root["candidate_keys"], label="candidate keys")
        ),
        semantic_keys=tuple(
            _text(item, label="semantic key")
            for item in _array(root["semantic_keys"], label="semantic keys")
        ),
        decision_source=cast(SemanticRouteDecisionSource, decision_source),
        evidence=evidence,
        adjudicator=adjudicator,
    )


def _evidence_from_payload(payload: object) -> SemanticRouteEvidence:
    item = _closed_mapping(
        payload,
        fields={"key", "kinds", "source_ids"},
        label="semantic route evidence",
    )
    kinds = tuple(
        _text(value, label="semantic evidence kind")
        for value in _array(item["kinds"], label="semantic evidence kinds")
    )
    allowed = set(get_args(SemanticRouteEvidenceKind))
    if any(kind not in allowed for kind in kinds):
        raise SemanticRouteContractError("semantic evidence kind is unsupported")
    return SemanticRouteEvidence(
        key=_text(item["key"], label="semantic evidence key"),
        kinds=cast(tuple[SemanticRouteEvidenceKind, ...], kinds),
        source_ids=tuple(
            _text(value, label="semantic source id")
            for value in _array(item["source_ids"], label="semantic source ids")
        ),
    )


def _closed_mapping(
    payload: object,
    *,
    fields: set[str],
    label: str,
) -> Mapping[str, object]:
    if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
        raise SemanticRouteContractError(f"{label} must be an object")
    if set(payload) != fields:
        raise SemanticRouteContractError(f"{label} fields are not closed")
    return cast(Mapping[str, object], payload)


def _array(payload: object, *, label: str) -> Sequence[object]:
    if not isinstance(payload, list):
        raise SemanticRouteContractError(f"{label} must be an array")
    return cast(Sequence[object], payload)


def _text(payload: object, *, label: str) -> str:
    if not isinstance(payload, str) or not payload:
        raise SemanticRouteContractError(f"{label} must be nonempty text")
    return payload


__all__ = [
    "MAX_SEMANTIC_CANDIDATES",
    "MAX_SEMANTIC_ROUTES",
    "MAX_SEMANTIC_ROUTES_PER_UNIT",
    "SEMANTIC_FALLBACK_KEY",
    "SEMANTIC_PROMPT_VERSION",
    "SEMANTIC_ROUTE_RECEIPT_VERSION",
    "SEMANTIC_ROUTE_RECEIPTS_FILENAME",
    "SEMANTIC_ROUTER_VERSION",
    "SemanticAdjudicatedRoute",
    "SemanticAdjudicationDecision",
    "SemanticAdjudicatorMetadata",
    "SemanticDocumentContext",
    "SemanticRouteCandidate",
    "SemanticRouteContractError",
    "SemanticRouteDefinition",
    "SemanticRouteEvidence",
    "SemanticRouteReceipt",
    "SemanticRouteReceiptRow",
    "SemanticRouteSource",
    "SemanticRouteTaxonomy",
    "SemanticRouteUnitInput",
    "semantic_route_receipt_from_payload",
    "semantic_route_receipt_row_from_payload",
    "semantic_route_receipt_row_to_payload",
    "semantic_route_receipt_to_payload",
]
