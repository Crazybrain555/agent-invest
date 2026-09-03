"""Closed semantic-route contracts for non-embedding Unit retrieval."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
from typing import Any, Literal, cast, get_args


SEMANTIC_ROUTE_RECEIPT_V1 = "semantic_route_receipt.v1"
SEMANTIC_ROUTE_RECEIPT_VERSION = "semantic_route_receipt.v2"
SEMANTIC_ROUTE_RECEIPT_V3 = "semantic_route_receipt.v3"
SEMANTIC_ROUTE_RECEIPTS_V1_FILENAME = "semantic_route_receipts.v1.jsonl"
SEMANTIC_ROUTE_RECEIPTS_FILENAME = "semantic_route_receipts.v2.jsonl"
SEMANTIC_ROUTE_RECEIPTS_V3_FILENAME = "semantic_route_receipts.v3.jsonl"
SEMANTIC_FAILOVER_POLICY_VERSION = "availability_only.v1"
SEMANTIC_OUTPUT_SCHEMA_VERSION = "semantic_route_output.v1"
SEMANTIC_ROUTER_VERSION = "semantic_router.v101"
SEMANTIC_PROMPT_VERSION = "semantic_route_adjudication.v32"
SEMANTIC_FALLBACK_KEY = "document_content"
MAX_SEMANTIC_ROUTES = 8
MAX_SEMANTIC_ROUTES_PER_UNIT = MAX_SEMANTIC_ROUTES
MAX_SEMANTIC_CANDIDATES = 8

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ROUTER_VERSION_RE = re.compile(r"^semantic_router\.v[1-9][0-9]*$")

SemanticRouteDecisionSource = Literal[
    "deterministic",
    "rule_abstain",
    "adjudicator_unavailable_abstain",
    "model",
    "model_abstain",
    "fallback",
]
SemanticProviderAttemptOutcome = Literal[
    "cache_hit",
    "succeeded",
    "succeeded_cache_write_failed",
    "availability_failed",
    "cancelled",
    "failed_closed",
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
    "source_section_exact",
    "source_heading_candidate",
    "source_heading_similarity",
    "source_heading_risk_suffix",
    "source_heading_risk_topic",
    "source_body_candidate",
    "source_table_candidate",
    "source_labeled_field_exact",
    "source_quantitative_topic",
    "source_resolved_proposal_exact",
    "document_context_candidate",
    "model_adjudicated",
    "fallback",
]


class SemanticRouteContractError(ValueError):
    """Semantic route input, receipt, or adjudication is not closed."""


@dataclass(frozen=True, slots=True)
class SemanticAdjudicationTerminalV1:
    """One terminal summary derived only from immutable per-Unit receipts."""

    status: str
    degraded_unit_count: int
    failover_group_count: int
    summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SemanticRouteDefinition:
    """One member of the versioned closed vocabulary."""

    key: str
    description: str
    labels: tuple[str, ...]
    heading_labels: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    exclusive_container: bool = False
    overview_container: bool = False
    context_container: bool = False
    section_container: bool = False
    quantitative_topic: bool = False
    role_anchor: bool = False

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
        if any(not label.strip() for label in self.heading_labels):
            raise SemanticRouteContractError(
                f"semantic route {self.key} has an empty heading label"
            )
        if len(self.heading_labels) != len(set(self.heading_labels)):
            raise SemanticRouteContractError(
                f"semantic route {self.key} repeats a heading label"
            )
        if set(self.labels) & set(self.heading_labels):
            raise SemanticRouteContractError(
                f"semantic route {self.key} repeats a label across evidence tiers"
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
        if self.section_container and (
            self.context_container
            or self.exclusive_container
        ):
            raise SemanticRouteContractError(
                f"semantic route {self.key} has an invalid section-container policy"
            )
        if self.role_anchor and (
            self.context_container
            or self.exclusive_container
            or self.overview_container
            or self.section_container
        ):
            raise SemanticRouteContractError(
                f"semantic route {self.key} has an invalid role-anchor policy"
            )
        if not isinstance(self.quantitative_topic, bool):
            raise SemanticRouteContractError(
                f"semantic route {self.key} quantitative-topic policy must be boolean"
            )
        if not isinstance(self.role_anchor, bool):
            raise SemanticRouteContractError(
                f"semantic route {self.key} role-anchor policy must be boolean"
            )


@dataclass(frozen=True, slots=True)
class SemanticCompositeSection:
    """One exact source heading that intentionally denotes several sections."""

    label: str
    keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.label.strip() or len(self.keys) < 2:
            raise SemanticRouteContractError(
                "semantic composite section needs one label and several keys"
            )
        if len(self.keys) != len(set(self.keys)) or any(
            not _KEY_RE.fullmatch(key) for key in self.keys
        ):
            raise SemanticRouteContractError(
                "semantic composite section keys are invalid"
            )


@dataclass(frozen=True, slots=True)
class SemanticRouteTaxonomy:
    """The complete vocabulary consumed by one router version."""

    version: str
    definitions: tuple[SemanticRouteDefinition, ...]
    composite_sections: tuple[SemanticCompositeSection, ...] = ()
    direct_composites: tuple[SemanticCompositeSection, ...] = ()
    fallback_key: str = SEMANTIC_FALLBACK_KEY

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.definitions:
            raise SemanticRouteContractError("semantic taxonomy is incomplete")
        keys = [item.key for item in self.definitions]
        if len(keys) != len(set(keys)):
            raise SemanticRouteContractError("semantic taxonomy repeats a key")
        context_definitions = tuple(
            item
            for item in self.definitions
            if item.context_container or item.section_container
        )
        for index, left in enumerate(context_definitions):
            left_labels = {
                _normalize_context_label(label)
                for label in (*left.labels, *left.heading_labels)
            }
            for right in context_definitions[index + 1 :]:
                scopes_overlap = (
                    not left.scopes
                    or not right.scopes
                    or bool(set(left.scopes) & set(right.scopes))
                )
                if scopes_overlap and left_labels & {
                    _normalize_context_label(label)
                    for label in (*right.labels, *right.heading_labels)
                }:
                    raise SemanticRouteContractError(
                        "semantic context labels collide within one scope"
                    )
        definitions_by_key = self.by_key()
        composite_labels: set[str] = set()
        for composite in (*self.composite_sections, *self.direct_composites):
            normalized = _normalize_context_label(composite.label)
            if normalized in composite_labels:
                raise SemanticRouteContractError(
                    "semantic composite section repeats a label"
                )
            composite_labels.add(normalized)
            for key in composite.keys:
                definition = definitions_by_key.get(key)
                if definition is None:
                    raise SemanticRouteContractError(
                        "semantic composite section references an unknown key"
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
class SemanticProviderIdentity:
    """Exact configured and attested identity of one adjudication provider."""

    provider_id: str
    provider: str
    adapter_kind: str
    adapter_version: str
    canonical_model: str
    inference_profile: str
    prompt_version: str
    prompt_sha256: str
    output_schema_version: str
    output_schema_sha256: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.provider_id,
                self.provider,
                self.adapter_kind,
                self.adapter_version,
                self.canonical_model,
                self.inference_profile,
                self.prompt_version,
                self.output_schema_version,
            )
        ):
            raise SemanticRouteContractError("semantic provider identity is incomplete")
        if not _SHA256_RE.fullmatch(self.prompt_sha256) or not _SHA256_RE.fullmatch(
            self.output_schema_sha256
        ):
            raise SemanticRouteContractError("semantic provider contract hash is invalid")


@dataclass(frozen=True, slots=True)
class SemanticProviderAttempt:
    """One ordered provider/cache attempt for a fixed adjudication group."""

    ordinal: int
    provider: SemanticProviderIdentity
    outcome: SemanticProviderAttemptOutcome
    reason_code: str | None = None
    availability_abstain_eligible: bool = False
    cache_key: str | None = None
    response_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise SemanticRouteContractError("semantic provider attempt ordinal is invalid")
        if self.cache_key is not None and not _SHA256_RE.fullmatch(self.cache_key):
            raise SemanticRouteContractError("semantic provider cache key is invalid")
        if self.response_sha256 is not None and not _SHA256_RE.fullmatch(
            self.response_sha256
        ):
            raise SemanticRouteContractError("semantic provider response hash is invalid")
        if self.outcome in {"cache_hit", "succeeded", "succeeded_cache_write_failed"}:
            if self.reason_code is not None or self.response_sha256 is None:
                raise SemanticRouteContractError("successful provider attempt is inconsistent")
        elif not self.reason_code:
            raise SemanticRouteContractError("failed provider attempt needs a reason")
        if self.availability_abstain_eligible != (
            self.outcome == "availability_failed"
        ):
            raise SemanticRouteContractError("provider availability marker is inconsistent")


@dataclass(frozen=True, slots=True)
class SemanticAdjudicationReceipt:
    """Group-level provider lineage copied into every affected Unit receipt."""

    policy_version: str
    group_hash: str
    attempts: tuple[SemanticProviderAttempt, ...]
    actual_result_attempt: int | None
    actual_result_identity: SemanticProviderIdentity | None
    group_response_sha256: str | None

    def __post_init__(self) -> None:
        if self.policy_version != SEMANTIC_FAILOVER_POLICY_VERSION:
            raise SemanticRouteContractError("semantic failover policy is unsupported")
        if not _SHA256_RE.fullmatch(self.group_hash) or not self.attempts:
            raise SemanticRouteContractError("semantic adjudication receipt is incomplete")
        if tuple(item.ordinal for item in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise SemanticRouteContractError("semantic provider attempts are not contiguous")
        has_result = self.actual_result_attempt is not None
        if has_result != (self.actual_result_identity is not None):
            raise SemanticRouteContractError("semantic actual-result identity is inconsistent")
        if has_result != (self.group_response_sha256 is not None):
            raise SemanticRouteContractError("semantic group-response hash is inconsistent")
        if has_result:
            assert self.actual_result_attempt is not None
            if self.actual_result_attempt > len(self.attempts):
                raise SemanticRouteContractError("semantic actual-result attempt is invalid")
            attempt = self.attempts[self.actual_result_attempt - 1]
            if attempt.provider != self.actual_result_identity:
                raise SemanticRouteContractError("semantic actual-result provider drifted")
            if attempt.response_sha256 != self.group_response_sha256:
                raise SemanticRouteContractError("semantic actual-result hash drifted")
        elif not all(item.availability_abstain_eligible for item in self.attempts):
            raise SemanticRouteContractError(
                "semantic unavailable abstention contains a non-availability failure"
            )


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
    adjudication: SemanticAdjudicationReceipt | None = None
    # Existing constructors remain v1 for deterministic replay.  New runtime
    # writers opt in to v2 explicitly after producing provider-attempt lineage.
    contract_version: str = SEMANTIC_ROUTE_RECEIPT_V1

    def __post_init__(self) -> None:
        if self.contract_version not in {
            SEMANTIC_ROUTE_RECEIPT_V1,
            SEMANTIC_ROUTE_RECEIPT_VERSION,
        }:
            raise SemanticRouteContractError("semantic route receipt version is unsupported")
        # Receipt artifacts are immutable evidence and older router generations
        # must remain structurally readable by Doctor.  Current-router equality
        # is a replay concern and is enforced by SemanticRouter._validate_receipt.
        if not self.taxonomy_version or not _ROUTER_VERSION_RE.fullmatch(
            self.router_version
        ):
            raise SemanticRouteContractError("semantic route receipt identity is invalid")
        if not _SHA256_RE.fullmatch(self.input_hash):
            raise SemanticRouteContractError("semantic route receipt input hash is invalid")
        if len(self.candidate_keys) != len(set(self.candidate_keys)):
            raise SemanticRouteContractError("semantic route receipt repeats a candidate")
        if len(self.semantic_keys) > MAX_SEMANTIC_ROUTES:
            raise SemanticRouteContractError("semantic route receipt has too many routes")
        if self.contract_version == SEMANTIC_ROUTE_RECEIPT_V1 and not self.semantic_keys:
            raise SemanticRouteContractError("semantic route v1 receipt needs routes")
        if len(self.semantic_keys) != len(set(self.semantic_keys)):
            raise SemanticRouteContractError("semantic route receipt repeats a route")
        evidence_keys = tuple(item.key for item in self.evidence)
        if evidence_keys != self.semantic_keys:
            raise SemanticRouteContractError(
                "semantic route receipt evidence must follow every selected route"
            )
        if self.contract_version == SEMANTIC_ROUTE_RECEIPT_V1:
            if self.adjudication is not None:
                raise SemanticRouteContractError("semantic route v1 cannot carry v2 adjudication")
        elif self.adjudicator is not None:
            raise SemanticRouteContractError("semantic route v2 cannot carry v1 adjudicator")
        if self.decision_source in {"model", "model_abstain"}:
            if self.contract_version == SEMANTIC_ROUTE_RECEIPT_V1:
                if self.adjudicator is None:
                    raise SemanticRouteContractError(
                        "model route receipt needs adjudicator metadata"
                    )
            elif self.adjudication is None or self.adjudication.actual_result_identity is None:
                raise SemanticRouteContractError(
                    "semantic route v2 model receipt needs actual-result identity"
                )
            if self.decision_source == "model" and any(
                key not in self.candidate_keys for key in self.semantic_keys
            ):
                raise SemanticRouteContractError("model selected a non-candidate route")
        elif self.adjudicator is not None:
            raise SemanticRouteContractError(
                "non-model route receipt cannot claim adjudicator metadata"
            )
        if self.decision_source == "adjudicator_unavailable_abstain":
            if self.contract_version != SEMANTIC_ROUTE_RECEIPT_VERSION:
                raise SemanticRouteContractError(
                    "unavailable abstention requires semantic route receipt v2"
                )
            if self.adjudication is None or self.adjudication.actual_result_identity is not None:
                raise SemanticRouteContractError(
                    "unavailable abstention needs exhausted provider attempts"
                )
        elif self.decision_source not in {"model", "model_abstain"} and self.adjudication:
            raise SemanticRouteContractError(
                "non-adjudicated route receipt cannot claim provider attempts"
            )
        if self.decision_source in {
            "fallback",
            "rule_abstain",
            "model_abstain",
        }:
            expected = (
                (SEMANTIC_FALLBACK_KEY,)
                if self.contract_version == SEMANTIC_ROUTE_RECEIPT_V1
                else ()
            )
            if self.semantic_keys != expected:
                raise SemanticRouteContractError("abstain receipt selected routes are invalid")
        elif self.decision_source == "adjudicator_unavailable_abstain":
            if self.semantic_keys:
                raise SemanticRouteContractError(
                    "unavailable abstention cannot invent a semantic route"
                )
        elif SEMANTIC_FALLBACK_KEY in self.semantic_keys:
            raise SemanticRouteContractError(
                "narrow semantic routes cannot include the fallback key"
            )

    @property
    def semantic_key(self) -> str:
        return self.semantic_keys[0] if self.semantic_keys else SEMANTIC_FALLBACK_KEY


@dataclass(frozen=True, slots=True)
class SemanticRouteReceiptRow:
    """One receipt bound to the exact persisted Unit snapshot row."""

    asset_id: str
    order_index: int
    receipt: SemanticRouteReceipt

    def __post_init__(self) -> None:
        if not self.asset_id or self.order_index < 1:
            raise SemanticRouteContractError("semantic receipt row identity is invalid")


@dataclass(frozen=True, slots=True)
class SemanticRouteReceiptRowV3:
    """Pre-ID v4 receipt keyed by stable run/order/provider-draft identity."""

    processing_run_id: str
    unit_order_index: int
    provider_locator_sha256: str
    routed_draft_sha256: str
    receipt: SemanticRouteReceipt
    contract_version: str = SEMANTIC_ROUTE_RECEIPT_V3

    def __post_init__(self) -> None:
        if self.contract_version != SEMANTIC_ROUTE_RECEIPT_V3:
            raise SemanticRouteContractError(
                "semantic receipt v3 contract is unsupported"
            )
        if (
            type(self.processing_run_id) is not str
            or not self.processing_run_id
            or type(self.unit_order_index) is not int
            or self.unit_order_index < 1
        ):
            raise SemanticRouteContractError(
                "semantic receipt v3 row identity is invalid"
            )
        for value, label in (
            (self.provider_locator_sha256, "provider locator"),
            (self.routed_draft_sha256, "routed draft"),
        ):
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise SemanticRouteContractError(
                    f"semantic receipt v3 {label} hash is invalid"
                )
        if (
            type(self.receipt) is not SemanticRouteReceipt
            or self.receipt.contract_version != SEMANTIC_ROUTE_RECEIPT_VERSION
        ):
            raise SemanticRouteContractError(
                "semantic receipt v3 requires exact v2 route semantics"
            )


def semantic_adjudication_terminal_v1(
    receipts: tuple[SemanticRouteReceipt, ...],
) -> SemanticAdjudicationTerminalV1:
    """Derive the persisted semantic terminal from the exact receipt set.

    Adjudication lineage is copied into every affected Unit receipt.  Group
    accounting therefore deduplicates by the immutable group hash while the
    degraded Unit count deliberately remains per Unit.
    """

    if not isinstance(receipts, tuple) or any(
        type(item) is not SemanticRouteReceipt for item in receipts
    ):
        raise SemanticRouteContractError(
            "semantic terminal requires exact receipt tuple"
        )
    by_group: dict[str, SemanticAdjudicationReceipt] = {}
    for receipt in receipts:
        adjudication = receipt.adjudication
        if adjudication is None:
            continue
        existing = by_group.get(adjudication.group_hash)
        if existing is not None and existing != adjudication:
            raise SemanticRouteContractError(
                "semantic adjudication group lineage drifted across Units"
            )
        by_group[adjudication.group_hash] = adjudication
    groups = tuple(by_group[key] for key in sorted(by_group))
    if any(item.actual_result_attempt is None for item in groups):
        status = "degraded_unavailable"
    elif any(cast(int, item.actual_result_attempt) > 1 for item in groups):
        status = "complete_backup"
    elif groups:
        status = "complete_primary"
    else:
        status = "not_required"
    attempts = tuple(attempt for group in groups for attempt in group.attempts)
    summary = _semantic_attempt_summary(attempts)
    summary.update(
        {
            "contract_version": "semantic_adjudication_summary.v1",
            "group_count": len(groups),
            "policy_version": SEMANTIC_FAILOVER_POLICY_VERSION,
            "status": status,
        }
    )
    return SemanticAdjudicationTerminalV1(
        status=status,
        degraded_unit_count=sum(
            item.decision_source == "adjudicator_unavailable_abstain"
            for item in receipts
        ),
        failover_group_count=sum(len(item.attempts) > 1 for item in groups),
        summary=summary,
    )


def _semantic_attempt_summary(
    attempts: tuple[SemanticProviderAttempt, ...],
) -> dict[str, Any]:
    outcome_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    provider_counts: dict[str, int] = {}
    provider_models: dict[str, str] = {}
    for attempt in attempts:
        outcome_counts[attempt.outcome] = outcome_counts.get(attempt.outcome, 0) + 1
        provider_id = attempt.provider.provider_id
        provider_counts[provider_id] = provider_counts.get(provider_id, 0) + 1
        provider_models[provider_id] = attempt.provider.canonical_model
        if attempt.reason_code is not None:
            reason_counts[attempt.reason_code] = (
                reason_counts.get(attempt.reason_code, 0) + 1
            )
    return {
        "attempt_outcome_counts": dict(sorted(outcome_counts.items())),
        "provider_attempt_counts": dict(sorted(provider_counts.items())),
        "provider_models": dict(sorted(provider_models.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
    }


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


def semantic_route_receipt_row_v3_to_payload(
    row: SemanticRouteReceiptRowV3,
) -> dict[str, object]:
    return {
        "contract_version": row.contract_version,
        "processing_run_id": row.processing_run_id,
        "provider_locator_sha256": row.provider_locator_sha256,
        "routed_draft_sha256": row.routed_draft_sha256,
        "semantic_route": semantic_route_receipt_to_payload(row.receipt),
        "unit_order_index": row.unit_order_index,
    }


def semantic_route_receipt_row_v3_from_payload(
    payload: object,
) -> SemanticRouteReceiptRowV3:
    root = _closed_mapping(
        payload,
        fields={
            "contract_version",
            "processing_run_id",
            "provider_locator_sha256",
            "routed_draft_sha256",
            "semantic_route",
            "unit_order_index",
        },
        label="semantic receipt v3 row",
    )
    unit_order_index = root["unit_order_index"]
    if type(unit_order_index) is not int or unit_order_index < 1:
        raise SemanticRouteContractError(
            "semantic receipt v3 unit_order_index is invalid"
        )
    return SemanticRouteReceiptRowV3(
        contract_version=_text(
            root["contract_version"], label="semantic receipt v3 contract_version"
        ),
        processing_run_id=_text(
            root["processing_run_id"], label="semantic receipt v3 processing_run_id"
        ),
        unit_order_index=unit_order_index,
        provider_locator_sha256=_text(
            root["provider_locator_sha256"],
            label="semantic receipt v3 provider_locator_sha256",
        ),
        routed_draft_sha256=_text(
            root["routed_draft_sha256"],
            label="semantic receipt v3 routed_draft_sha256",
        ),
        receipt=semantic_route_receipt_from_payload(root["semantic_route"]),
    )


def validate_semantic_route_receipt_rows_v3(
    rows: tuple[SemanticRouteReceiptRowV3, ...],
    *,
    processing_run_id: str,
) -> None:
    if (
        not isinstance(rows, tuple)
        or type(processing_run_id) is not str
        or not processing_run_id
        or any(type(row) is not SemanticRouteReceiptRowV3 for row in rows)
    ):
        raise SemanticRouteContractError("semantic receipt v3 set identity is invalid")
    expected_order = tuple(range(1, len(rows) + 1))
    if tuple(row.unit_order_index for row in rows) != expected_order:
        raise SemanticRouteContractError(
            "semantic receipt v3 rows are not contiguous and ordered"
        )
    if any(row.processing_run_id != processing_run_id for row in rows):
        raise SemanticRouteContractError("semantic receipt v3 rows mix processing runs")
    identities = {
        (
            row.processing_run_id,
            row.unit_order_index,
            row.provider_locator_sha256,
            row.routed_draft_sha256,
        )
        for row in rows
    }
    if len(identities) != len(rows):
        raise SemanticRouteContractError(
            "semantic receipt v3 rows contain duplicate identities"
        )


def semantic_route_receipts_file_bytes_v3(
    rows: tuple[SemanticRouteReceiptRowV3, ...],
) -> bytes:
    """Return the sole durable JSONL encoding for semantic receipt v3 rows."""

    if not rows:
        raise SemanticRouteContractError("semantic receipt v3 file cannot be empty")
    validate_semantic_route_receipt_rows_v3(
        rows,
        processing_run_id=rows[0].processing_run_id,
    )
    return "".join(
        json.dumps(
            semantic_route_receipt_row_v3_to_payload(row),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for row in rows
    ).encode("utf-8")


def semantic_route_receipt_to_payload(receipt: SemanticRouteReceipt) -> dict[str, object]:
    """Serialize a receipt as a closed private snapshot object."""

    if receipt.contract_version == SEMANTIC_ROUTE_RECEIPT_VERSION:
        return {
            "adjudication": (
                None
                if receipt.adjudication is None
                else _adjudication_receipt_to_payload(receipt.adjudication)
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
            "selected_keys": list(receipt.semantic_keys),
            "taxonomy_version": receipt.taxonomy_version,
        }
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
    """Decode one private snapshot receipt by its frozen contract version."""

    if not isinstance(payload, Mapping):
        raise SemanticRouteContractError("semantic route receipt must be an object")
    contract_version = payload.get("contract_version")
    if contract_version == SEMANTIC_ROUTE_RECEIPT_VERSION:
        return _semantic_route_receipt_v2_from_payload(payload)
    if contract_version != SEMANTIC_ROUTE_RECEIPT_V1:
        raise SemanticRouteContractError("semantic route receipt version is unsupported")

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


def _semantic_route_receipt_v2_from_payload(
    payload: object,
) -> SemanticRouteReceipt:
    root = _closed_mapping(
        payload,
        fields={
            "adjudication",
            "candidate_keys",
            "contract_version",
            "decision_source",
            "evidence",
            "input_hash",
            "router_version",
            "selected_keys",
            "taxonomy_version",
        },
        label="semantic route receipt v2",
    )
    decision_source = _text(root["decision_source"], label="decision source")
    if decision_source not in {
        "deterministic",
        "rule_abstain",
        "adjudicator_unavailable_abstain",
        "model",
        "model_abstain",
        "fallback",
    }:
        raise SemanticRouteContractError("semantic decision source is unsupported")
    raw_adjudication = root["adjudication"]
    adjudication = (
        None
        if raw_adjudication is None
        else _adjudication_receipt_from_payload(raw_adjudication)
    )
    return SemanticRouteReceipt(
        contract_version=SEMANTIC_ROUTE_RECEIPT_VERSION,
        taxonomy_version=_text(root["taxonomy_version"], label="taxonomy version"),
        router_version=_text(root["router_version"], label="router version"),
        input_hash=_text(root["input_hash"], label="semantic input hash"),
        candidate_keys=tuple(
            _text(item, label="candidate key")
            for item in _array(root["candidate_keys"], label="candidate keys")
        ),
        semantic_keys=tuple(
            _text(item, label="selected key")
            for item in _array(root["selected_keys"], label="selected keys")
        ),
        decision_source=cast(SemanticRouteDecisionSource, decision_source),
        evidence=tuple(
            _evidence_from_payload(item)
            for item in _array(root["evidence"], label="semantic route evidence")
        ),
        adjudication=adjudication,
    )


def _provider_identity_to_payload(
    identity: SemanticProviderIdentity,
) -> dict[str, object]:
    return {
        "adapter_kind": identity.adapter_kind,
        "adapter_version": identity.adapter_version,
        "canonical_model": identity.canonical_model,
        "inference_profile": identity.inference_profile,
        "output_schema_sha256": identity.output_schema_sha256,
        "output_schema_version": identity.output_schema_version,
        "prompt_sha256": identity.prompt_sha256,
        "prompt_version": identity.prompt_version,
        "provider": identity.provider,
        "provider_id": identity.provider_id,
    }


def _provider_identity_from_payload(payload: object) -> SemanticProviderIdentity:
    fields = {
        "adapter_kind",
        "adapter_version",
        "canonical_model",
        "inference_profile",
        "output_schema_sha256",
        "output_schema_version",
        "prompt_sha256",
        "prompt_version",
        "provider",
        "provider_id",
    }
    item = _closed_mapping(payload, fields=fields, label="semantic provider identity")
    return SemanticProviderIdentity(
        **{field: _text(item[field], label=field) for field in fields}
    )


def _provider_attempt_to_payload(attempt: SemanticProviderAttempt) -> dict[str, object]:
    return {
        "availability_abstain_eligible": attempt.availability_abstain_eligible,
        "cache_key": attempt.cache_key,
        "ordinal": attempt.ordinal,
        "outcome": attempt.outcome,
        "provider": _provider_identity_to_payload(attempt.provider),
        "reason_code": attempt.reason_code,
        "response_sha256": attempt.response_sha256,
    }


def _provider_attempt_from_payload(payload: object) -> SemanticProviderAttempt:
    item = _closed_mapping(
        payload,
        fields={
            "availability_abstain_eligible",
            "cache_key",
            "ordinal",
            "outcome",
            "provider",
            "reason_code",
            "response_sha256",
        },
        label="semantic provider attempt",
    )
    ordinal = item["ordinal"]
    availability = item["availability_abstain_eligible"]
    if type(ordinal) is not int or type(availability) is not bool:
        raise SemanticRouteContractError("semantic provider attempt fields are invalid")
    outcome = _text(item["outcome"], label="semantic attempt outcome")
    if outcome not in set(get_args(SemanticProviderAttemptOutcome)):
        raise SemanticRouteContractError("semantic provider attempt outcome is unsupported")
    reason = item["reason_code"]
    cache_key = item["cache_key"]
    response_hash = item["response_sha256"]
    for value, label in (
        (reason, "semantic attempt reason"),
        (cache_key, "semantic attempt cache key"),
        (response_hash, "semantic attempt response hash"),
    ):
        if value is not None and (not isinstance(value, str) or not value):
            raise SemanticRouteContractError(f"{label} is invalid")
    return SemanticProviderAttempt(
        ordinal=ordinal,
        provider=_provider_identity_from_payload(item["provider"]),
        outcome=cast(SemanticProviderAttemptOutcome, outcome),
        reason_code=cast(str | None, reason),
        availability_abstain_eligible=availability,
        cache_key=cast(str | None, cache_key),
        response_sha256=cast(str | None, response_hash),
    )


def _adjudication_receipt_to_payload(
    receipt: SemanticAdjudicationReceipt,
) -> dict[str, object]:
    return {
        "actual_result_attempt": receipt.actual_result_attempt,
        "actual_result_identity": (
            None
            if receipt.actual_result_identity is None
            else _provider_identity_to_payload(receipt.actual_result_identity)
        ),
        "attempts": [_provider_attempt_to_payload(item) for item in receipt.attempts],
        "group_hash": receipt.group_hash,
        "group_response_sha256": receipt.group_response_sha256,
        "policy_version": receipt.policy_version,
    }


def _adjudication_receipt_from_payload(payload: object) -> SemanticAdjudicationReceipt:
    item = _closed_mapping(
        payload,
        fields={
            "actual_result_attempt",
            "actual_result_identity",
            "attempts",
            "group_hash",
            "group_response_sha256",
            "policy_version",
        },
        label="semantic adjudication receipt",
    )
    actual_attempt = item["actual_result_attempt"]
    if actual_attempt is not None and type(actual_attempt) is not int:
        raise SemanticRouteContractError("semantic actual-result attempt is invalid")
    response_hash = item["group_response_sha256"]
    if response_hash is not None and not isinstance(response_hash, str):
        raise SemanticRouteContractError("semantic group-response hash is invalid")
    return SemanticAdjudicationReceipt(
        policy_version=_text(item["policy_version"], label="semantic policy version"),
        group_hash=_text(item["group_hash"], label="semantic group hash"),
        attempts=tuple(
            _provider_attempt_from_payload(value)
            for value in _array(item["attempts"], label="semantic attempts")
        ),
        actual_result_attempt=actual_attempt,
        actual_result_identity=(
            None
            if item["actual_result_identity"] is None
            else _provider_identity_from_payload(item["actual_result_identity"])
        ),
        group_response_sha256=response_hash,
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
    "SEMANTIC_FAILOVER_POLICY_VERSION",
    "SEMANTIC_OUTPUT_SCHEMA_VERSION",
    "SEMANTIC_ROUTE_RECEIPT_V1",
    "SEMANTIC_ROUTE_RECEIPT_VERSION",
    "SEMANTIC_ROUTE_RECEIPT_V3",
    "SEMANTIC_ROUTE_RECEIPTS_V1_FILENAME",
    "SEMANTIC_ROUTE_RECEIPTS_FILENAME",
    "SEMANTIC_ROUTE_RECEIPTS_V3_FILENAME",
    "SEMANTIC_ROUTER_VERSION",
    "SemanticAdjudicatedRoute",
    "SemanticAdjudicationDecision",
    "SemanticAdjudicationReceipt",
    "SemanticAdjudicationTerminalV1",
    "SemanticAdjudicatorMetadata",
    "SemanticDocumentContext",
    "SemanticRouteCandidate",
    "SemanticRouteContractError",
    "SemanticRouteDefinition",
    "SemanticRouteEvidence",
    "SemanticRouteReceipt",
    "SemanticRouteReceiptRow",
    "SemanticRouteReceiptRowV3",
    "SemanticRouteSource",
    "SemanticRouteTaxonomy",
    "SemanticRouteUnitInput",
    "SemanticProviderAttempt",
    "SemanticProviderIdentity",
    "semantic_route_receipt_from_payload",
    "semantic_route_receipt_row_from_payload",
    "semantic_route_receipt_row_to_payload",
    "semantic_route_receipt_row_v3_from_payload",
    "semantic_route_receipt_row_v3_to_payload",
    "semantic_route_receipts_file_bytes_v3",
    "semantic_route_receipt_to_payload",
    "semantic_adjudication_terminal_v1",
    "validate_semantic_route_receipt_rows_v3",
]
