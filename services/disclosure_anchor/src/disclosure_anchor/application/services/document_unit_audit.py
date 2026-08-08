"""Pure source-identity audit for NormalizedIR and built document units.

The audit deliberately does not use a document-wide string haystack.  Source
carriers and output units are joined only through explicit artifact locators;
otherwise one repeated heading could hide the loss of another occurrence.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import re
import statistics
from typing import Any, Callable, Iterable, Mapping, cast
import unicodedata

from disclosure_anchor.application.contracts.document_structure import (
    DocumentStructureContractError,
    OWNER_SCOPE_V1_DOCUMENT_STRUCTURE_ALGORITHM,
    OWNER_SCOPE_V2_DOCUMENT_STRUCTURE_ALGORITHM,
    validate_document_structure,
)
from disclosure_anchor.application.contracts import content_annotations
from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    assess_normalized_ir_table_reconciliation,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    NormalizedIRVersionError,
    is_visual_only_table_element,
    table_has_visible_text_evidence,
    validate_normalized_ir_contract,
    validate_reconciliation_generation,
)
from disclosure_anchor.application.contracts.source_evidence import (
    MappedSourceEvent,
    NativeTextEvent,
    SourceEvidenceProof,
)
from disclosure_anchor.application.contracts.source_evidence_occurrence import (
    SourceMappedAnchor,
    SourceNativeOccurrence,
    SourceOccurrenceIdentityError,
    punctuation_only_text,
    geometry_issue_occurrence,
    mapped_source_anchor,
    native_text_occurrence,
    visual_page_occurrence,
)
from disclosure_anchor.application.contracts.publication_safety import (
    semantic_payload_without_unsafe_glyphs,
    unsafe_semantic_characters,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    HEADING_PROJECTION_KINDS,
    PAYLOAD_PROJECTION_KINDS,
    PUBLIC_ARTIFACT_LOCATOR_FIELDS,
    SOURCE_EVIDENCE_GEOMETRY_ISSUE_KIND,
    SOURCE_EVIDENCE_VISUAL_PAGE_KIND,
    SOURCE_FIELD_KINDS,
    SearchTargetContractError,
    UNIT_SOURCE_PROJECTION_VERSION,
    projection_target_value,
    requires_primary_search_leaf,
    search_text_values,
    source_ref_from_locator,
    source_ref_identity,
    source_value_sha256,
)
from disclosure_anchor.domain.value_objects.comparison_text import (
    comparison_text,
    source_carrier_search_surfaces,
    strict_source_comparison_text,
)
from disclosure_anchor.domain.value_objects.semantic_key import (
    SemanticKeyInvariantError,
    validate_semantic_key_state,
)


_HEX_DIGEST_RE = re.compile(r"(?i)(?:^|/)([0-9a-f]{64})(?:\.[a-z0-9]+)?$")
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_ARTIFACT_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_IMAGE_MEDIA_TYPES = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})
_ARTIFACT_FIELDS = frozenset({"artifact_role", "sha256", "size_bytes", "media_type"})
_VISUAL_ARTIFACT_FIELDS = _ARTIFACT_FIELDS | {"pixel_width", "pixel_height"}
_TEXT_KINDS = {"text", "heading", "equation", "unknown", "page_furniture"}
_TEXT_PAYLOAD_FIELDS = frozenset(
    {
        "caption",
        "code_body",
        "code_caption",
        "code_footnote",
        "code_subtype",
        "content",
        "context",
        "image_ref",
        "list_items",
        "list_subtype",
        "notes",
        "representation_role",
        "search_policy",
        "text",
        "text_format",
        "visual_kind",
        "visual_subtype",
    }
)
_TABLE_PAYLOAD_FIELDS = frozenset(
    {
        "caption",
        "cells",
        "embedded_media",
        "headers",
        "merged_cells",
        "notes",
        "rows",
        "unit",
    }
)
_MIXED_PAYLOAD_FIELDS = frozenset({"order_status", "parts", "semantic_type"})
_MIXED_PART_FIELDS = frozenset(
    {
        "applicability",
        "artifact_locator",
        "heading_path",
        "kind",
        "order",
        "quality_status",
        "representation_role",
        "search_policy",
    }
)


@dataclass(frozen=True)
class AuditDocumentMetadata:
    document_id: str
    title: str | None
    filing_type: str | None
    security_code: str | None = None
    security_name: str | None = None


@dataclass(frozen=True)
class AuditUnitView:
    order_index: int
    payload_kind: str
    payload: dict[str, Any]
    title: str | None
    heading_path: list[str]
    semantic_key: str | None
    semantic_keys: list[str] | None
    quality_status: str
    applicability: str | None
    artifact_locator: dict[str, Any] | None


@dataclass(frozen=True)
class AuditFinding:
    code: str
    severity: str
    message: str
    source_ref: str | None = None
    unit_order: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "source_ref": self.source_ref,
            "unit_order": self.unit_order,
        }


def _audit_error(
    findings: list[AuditFinding],
    code: str,
    message: str,
    *,
    source_ref: str | None = None,
    unit_order: int | None = None,
) -> None:
    findings.append(AuditFinding(code, "error", message, source_ref, unit_order))


@dataclass(frozen=True)
class DocumentAuditReport:
    document_id: str
    metrics: dict[str, Any]
    findings: tuple[AuditFinding, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "ok": self.ok,
            "metrics": self.metrics,
            "findings": [item.as_dict() for item in self.findings],
        }


@dataclass(frozen=True)
class _SourceIndex:
    elements: dict[str, dict[str, Any]]
    by_ir_id: dict[str, str]
    by_source_item_index: dict[int, str]
    by_order_index: dict[int, str]
    by_identity: dict[tuple[object, ...], str]


@dataclass
class _CoverageState:
    payload_refs: set[str] = field(default_factory=set)
    searchable_payload_refs: set[str] = field(default_factory=set)
    structure_refs: set[str] = field(default_factory=set)
    structured_refs: set[str] = field(default_factory=set)
    validated_structured_refs: set[str] = field(default_factory=set)
    refs_by_unit: dict[int, set[str]] = field(default_factory=dict)
    image_payloads: dict[str, list[tuple[str, dict[str, Any]]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    table_payloads: dict[
        str,
        list[tuple[str, dict[str, Any]]],
    ] = field(default_factory=lambda: defaultdict(list))
    selector_claims: dict[str, list["_ResolvedSelector"]] = field(
        default_factory=lambda: defaultdict(list)
    )
    payload_projections: list["_ResolvedPayloadProjection"] = field(
        default_factory=list
    )
    carrier_occurrences: list["_CarrierOccurrence"] = field(default_factory=list)
    carrier_quality: dict[str, str] = field(default_factory=dict)
    required_search_carriers: set[str] = field(default_factory=set)
    closed_search_carriers: set[str] = field(default_factory=set)
    primary_search_leaf_count: int = 0
    non_primary_source_alternative_count: int = 0
    # One active primary search leaf per payload source ref: counts recomputed
    # here from elements and final units, never from builder markers.
    active_search_ref_counts: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    duplicate_active_primary_count: int = 0
    page_furniture_active_search_count: int = 0


@dataclass(frozen=True)
class _ResolvedSelector:
    ref: str
    kind: str
    value: Any
    field: dict[str, Any]
    role: str
    unit_order: int
    carrier_id: str


@dataclass(frozen=True)
class _ResolvedPayloadProjection:
    kind: str
    transform: str
    target_field: str
    target_value: Any
    selectors: tuple[_ResolvedSelector, ...]
    unit_order: int
    carrier_id: str


@dataclass(frozen=True)
class _CarrierOccurrence:
    unit_order: int
    carrier_id: str
    payload_refs: frozenset[str]
    heading_path: tuple[str, ...]
    headings: tuple["_HeadingProjection", ...]
    artifacts: tuple[tuple[str, dict[str, Any]], ...] = ()
    container_semantic_type: str | None = None
    container_parts: tuple[
        tuple[frozenset[str], str],
        ...,
    ] = ()


@dataclass(frozen=True)
class _HeadingProjection:
    target_index: int
    kind: str
    selectors: tuple[_ResolvedSelector, ...]


@dataclass(frozen=True)
class _ProofHeading:
    node_id: int
    parent_node_id: int | None
    propagates: bool
    section_start: int
    section_end: int
    title: str
    source_refs: tuple["_ProofSourceRef", ...]


@dataclass(frozen=True)
class _ProofSourceRef:
    ref: str
    field: str
    index: int | None
    text_span: tuple[int, int]


@dataclass(frozen=True)
class _ProofOwnerScopeBreak:
    boundary_source_item_index: int
    boundary_ref: str
    boundary_field: str
    boundary_index: int | None
    boundary_text_span: tuple[int, int]
    boundary_value_sha256: str
    page_index: int
    eligibility_basis: str
    relative_rank: str
    current_owner_node_id: int
    target_node_id: int | None
    boundary_carrier_scope: str
    source_atom_orders: tuple[int, ...]
    materialization_policy: str
    flatten_subtree_root_node_id: int | None


@dataclass(frozen=True)
class _StructureProofIndex:
    headings: dict[int, _ProofHeading]
    frame_source_indices: frozenset[int]
    owner_scope_breaks: tuple[_ProofOwnerScopeBreak, ...]
    # Node ids whose accepted subtree a flatten policy folds into a target,
    # mapped to that target node id.
    flattened_node_targets: Mapping[int, int] = field(default_factory=dict)


@dataclass
class _DispositionState:
    external_refs: set[str] = field(default_factory=set)


class _NativeProofAuditError(ValueError):
    """Parser-neutral source facts cannot be matched to the IR."""


@dataclass(frozen=True)
class _AuditedNativeGap:
    page_idx: int
    word_order_span: tuple[int, int]
    occurrences: tuple[SourceNativeOccurrence, ...]
    predecessor: SourceMappedAnchor | None
    successor: SourceMappedAnchor | None
    relation: str


@dataclass(frozen=True)
class _NativeAuditPlan:
    occurrences: tuple[SourceNativeOccurrence, ...]
    gaps: tuple[_AuditedNativeGap, ...]
    page_bases: Mapping[int, str]


def audit_document(
    *,
    normalized_ir: dict[str, Any],
    units: Iterable[AuditUnitView],
    metadata: AuditDocumentMetadata,
    source_proof: SourceEvidenceProof,
    source_dispositions: Iterable[Mapping[str, Any]] = (),
    image_hashes: Mapping[str, str] | None = None,
) -> DocumentAuditReport:
    """Audit one builder replay without database or filesystem access."""

    unit_list = list(units)
    findings: list[AuditFinding] = []
    if not _validate_reconciliation(normalized_ir, findings=findings):
        return DocumentAuditReport(
            document_id=metadata.document_id,
            metrics={
                "finding_count": len(findings),
                "error_count": sum(item.severity == "error" for item in findings),
            },
            findings=tuple(findings),
        )
    source = _build_source_index(normalized_ir, metadata=metadata, findings=findings)
    structure = _build_structure_proof_index(
        normalized_ir,
        source=source,
        source_proof=source_proof,
        findings=findings,
    )
    source, native_plan = _extend_source_index_with_native_evidence(
        source,
        normalized_ir=normalized_ir,
        source_proof=source_proof,
        findings=findings,
    )
    _validate_units(
        unit_list,
        document_title=metadata.title,
        findings=findings,
    )
    dispositions = _validate_source_dispositions(
        source_dispositions,
        source=source,
        structure=structure,
        findings=findings,
    )
    state = _collect_unit_coverage(
        unit_list,
        source=source,
        findings=findings,
    )
    _validate_source_visual_evidence(
        source=source,
        source_proof=source_proof,
        state=state,
        findings=findings,
    )
    _validate_native_gap_units(
        unit_list,
        native_plan=native_plan,
        source=source,
        state=state,
        findings=findings,
    )
    _validate_structure_projections(
        source,
        structure=structure,
        state=state,
        findings=findings,
    )
    _validate_quality_lower_bounds(
        unit_list,
        source=source,
        state=state,
        findings=findings,
    )
    located_structured_refs = set(state.structured_refs)
    validated_structured_refs = state.validated_structured_refs
    for ref in sorted(
        located_structured_refs - validated_structured_refs,
        key=source_order(source),
    ):
        _audit_error(
            findings,
            "structured_source_unproved",
            "structured locator has no independently proven field projection",
            source_ref=ref,
        )
    state.structured_refs = validated_structured_refs

    substantive_refs = {
        ref for ref, element in source.elements.items() if _is_substantive(element)
    }
    empty_refs = set(source.elements) - substantive_refs
    covered_refs = (
        state.payload_refs
        | state.structure_refs
        | state.structured_refs
        | dispositions.external_refs
        | empty_refs
    )
    suppressed_refs = {
        ref
        for ref in substantive_refs - covered_refs
        if "_native_source_ref" in source.elements[ref]
        and punctuation_only_text(str(source.elements[ref].get("text") or ""))
    }
    uncovered_refs = substantive_refs - covered_refs - suppressed_refs
    for ref in sorted(
        uncovered_refs,
        key=source_order(source),
    ):
        _audit_error(
            findings,
            "source_atom_uncovered",
            "non-empty source carrier has no locator-backed representation",
            source_ref=ref,
        )

    _validate_projection_partitions(
        source,
        state=state,
        findings=findings,
    )
    _validate_images(
        source,
        state=state,
        image_hashes=dict(image_hashes or {}),
        findings=findings,
    )
    _validate_table_media(
        source,
        state=state,
        image_hashes=dict(image_hashes or {}),
        findings=findings,
    )
    _validate_output_role_closure(
        source=source,
        state=state,
        external_refs=dispositions.external_refs,
        empty_refs=empty_refs,
        findings=findings,
    )
    _validate_unit_source_order(
        unit_list,
        source=source,
        state=state,
        findings=findings,
    )

    payload_chars = sum(_unit_visible_chars(unit) for unit in unit_list)
    tiny_units = sum(1 for unit in unit_list if 0 < _unit_visible_chars(unit) < 50)
    coverage_classes = {
        "payload": len(substantive_refs & state.payload_refs),
        "structure": len(
            (substantive_refs - state.payload_refs) & state.structure_refs
        ),
        "structured": len(
            (substantive_refs - state.payload_refs - state.structure_refs)
            & state.structured_refs
        ),
        "provenance": 0,
        "external_metadata": len(
            (
                substantive_refs
                - state.payload_refs
                - state.structure_refs
                - state.structured_refs
            )
            & dispositions.external_refs
        ),
        "non_primary_source_alternative": (
            state.non_primary_source_alternative_count
        ),
        "proven_empty": len(empty_refs),
        "suppressed_nonsemantic": len(suppressed_refs),
        "uncovered": len(uncovered_refs),
    }
    missing_search_carriers = (
        state.required_search_carriers - state.closed_search_carriers
    )
    metrics: dict[str, Any] = {
        "source_elements": len(source.elements),
        "substantive_source_atoms": len(substantive_refs),
        "coverage": coverage_classes,
        "unit_count": len(unit_list),
        "unit_kinds": _counts(unit.payload_kind for unit in unit_list),
        "quality_statuses": _counts(unit.quality_status for unit in unit_list),
        "tiny_units_lt_50_chars": tiny_units,
        "visible_payload_chars": payload_chars,
        "typed_payload_projections": len(state.payload_projections),
        "primary_search": {
            "required_carriers": len(state.required_search_carriers),
            "closed_carriers": len(state.closed_search_carriers),
            "leaf_count": state.primary_search_leaf_count,
            "duplicate_active_primary": state.duplicate_active_primary_count,
            "page_furniture_active": state.page_furniture_active_search_count,
            "missing_carriers": len(missing_search_carriers),
        },
        "finding_count": len(findings),
        "error_count": sum(item.severity == "error" for item in findings),
    }
    return DocumentAuditReport(
        document_id=metadata.document_id,
        metrics=metrics,
        findings=tuple(findings),
    )


def _build_source_index(
    normalized_ir: dict[str, Any],
    *,
    metadata: AuditDocumentMetadata,
    findings: list[AuditFinding],
) -> _SourceIndex:
    """Index an already contract-validated NormalizedIR document."""

    if normalized_ir.get("document_id") != metadata.document_id:
        _audit_error(
            findings,
            "document_id_mismatch",
            "manifest and NormalizedIR document_id differ",
        )
    raw_elements = cast(list[dict[str, Any]], normalized_ir["elements"])
    elements = {cast(str, raw["ir_id"]): raw for raw in raw_elements}
    by_source = {
        cast(int, raw["source_item_index"]): cast(str, raw["ir_id"])
        for raw in raw_elements
    }
    by_order = {
        cast(int, raw["order_index"]): cast(str, raw["ir_id"]) for raw in raw_elements
    }
    by_identity: dict[tuple[object, ...], str] = {}
    for raw in raw_elements:
        ref = cast(str, raw["ir_id"])
        if raw.get("document_id") not in {None, metadata.document_id}:
            _audit_error(
                findings,
                "element_document_id_mismatch",
                "element document_id differs from its IR",
                source_ref=ref,
            )
        canonical_ref = source_ref_from_locator(
            {
                key: raw[key]
                for key in (
                    "ir_id",
                    "source_item_index",
                    "order_index",
                    "page_no",
                    "bbox",
                )
                if key in raw
            }
        )
        assert canonical_ref is not None
        identity = source_ref_identity(canonical_ref)
        if identity is not None:
            by_identity[identity] = ref
        page_idx = raw.get("page_idx")
        page_no = raw.get("page_no")
        if (
            isinstance(page_idx, int)
            and isinstance(page_no, int)
            and page_no != page_idx + 1
        ):
            _audit_error(
                findings,
                "page_number_mismatch",
                "page_no must equal page_idx + 1",
                source_ref=ref,
            )
    return _SourceIndex(
        elements=elements,
        by_ir_id={ref: ref for ref in elements},
        by_source_item_index=by_source,
        by_order_index=by_order,
        by_identity=by_identity,
    )


def _extend_source_index_with_native_evidence(
    source: _SourceIndex,
    *,
    normalized_ir: Mapping[str, Any],
    source_proof: SourceEvidenceProof,
    findings: list[AuditFinding],
) -> tuple[_SourceIndex, _NativeAuditPlan]:
    """Add only source-native occurrences that require public payload edges."""

    try:
        plan = _build_native_audit_plan(
            normalized_ir=normalized_ir,
            source=source,
            proof=source_proof,
        )
    except _NativeProofAuditError as exc:
        _audit_error(
            findings,
            "source_evidence_projection_invalid",
            str(exc),
        )
        return source, _NativeAuditPlan((), (), {})
    elements = dict(source.elements)
    by_order = dict(source.by_order_index)
    by_identity = dict(source.by_identity)
    for occurrence in plan.occurrences:
        ref = occurrence.occurrence_id
        identity = source_ref_identity(occurrence.source_ref)
        if identity is None or ref in elements or identity in by_identity:
            _audit_error(
                findings,
                "source_evidence_identity_duplicate",
                "source-native occurrence identity/order is duplicated",
                source_ref=ref,
            )
            continue
        elements[ref] = occurrence.as_source_element()
        by_identity[identity] = ref
    return (
        _SourceIndex(
            elements=elements,
            by_ir_id=dict(source.by_ir_id),
            by_source_item_index=dict(source.by_source_item_index),
            by_order_index=by_order,
            by_identity=by_identity,
        ),
        plan,
    )


def _build_native_audit_plan(
    *,
    normalized_ir: Mapping[str, Any],
    source: _SourceIndex,
    proof: SourceEvidenceProof,
) -> _NativeAuditPlan:
    """Independently derive physical gaps from typed events, not builder output."""

    if (
        normalized_ir.get("source_pdf_sha256") != proof.identity.source_pdf_sha256
        or normalized_ir.get("source_pdf_page_count") != proof.identity.page_count
    ):
        raise _NativeProofAuditError(
            "typed source proof differs from NormalizedIR PDF identity"
        )
    run_by_atom = {
        atom_index: (run.page_idx, run.run_index)
        for run in proof.retrieval_runs
        for atom_index in run.atom_indices
    }
    occurrences: list[SourceNativeOccurrence] = []
    gaps: list[_AuditedNativeGap] = []
    page_bases: dict[int, str] = {}
    for page in proof.pages:
        predecessor: SourceMappedAnchor | None = None
        pending: list[SourceNativeOccurrence] = []
        carrier_orders: dict[int, list[int]] = {}
        order_conflict = False

        def flush(successor: SourceMappedAnchor | None) -> None:
            if not pending:
                return
            if predecessor is not None and successor is not None:
                relation = (
                    "bounded_by_same_source"
                    if predecessor.source_ref == successor.source_ref
                    else "between_mapped_sources"
                )
            elif successor is not None:
                relation = "page_prefix"
            elif predecessor is not None:
                relation = "page_suffix"
            else:
                relation = "page_only"
            gaps.append(
                _AuditedNativeGap(
                    page_idx=page.page_idx,
                    word_order_span=(
                        pending[0].word_order,
                        pending[-1].word_order + 1,
                    ),
                    occurrences=tuple(pending),
                    predecessor=predecessor,
                    successor=successor,
                    relation=relation,
                )
            )
            pending.clear()

        for event in page.events:
            if isinstance(event, MappedSourceEvent):
                carrier_orders.setdefault(
                    event.source_item_index,
                    [],
                ).append(event.word_order)
                if event.order_state == "conflict":
                    order_conflict = True
                ref = source.by_source_item_index.get(event.source_item_index)
                element = source.elements.get(ref) if ref is not None else None
                if element is None:
                    raise _NativeProofAuditError(
                        "mapped source event has no NormalizedIR carrier"
                    )
                try:
                    anchor = mapped_source_anchor(
                        page_idx=page.page_idx,
                        atom_index=event.atom_index,
                        word_order=event.word_order,
                        source_item_index=event.source_item_index,
                        order_state=event.order_state,
                        element=element,
                    )
                except SourceOccurrenceIdentityError as exc:
                    raise _NativeProofAuditError(str(exc)) from exc
                flush(anchor)
                predecessor = anchor
                continue
            if isinstance(event, NativeTextEvent):
                retrieval_run = run_by_atom.get(event.atom_index)
                if retrieval_run is None:
                    raise _NativeProofAuditError(
                        f"native text atom {event.atom_index} belongs to no "
                        "retrieval run"
                    )
                occurrence = native_text_occurrence(
                    proof.identity,
                    page_idx=page.page_idx,
                    event=event,
                    retrieval_run=retrieval_run,
                )
            else:
                occurrence = geometry_issue_occurrence(
                    proof.identity,
                    page_idx=page.page_idx,
                    event=event,
                )
            occurrences.append(occurrence)
            pending.append(occurrence)
        flush(None)
        spans = sorted(
            (min(orders), max(orders) + 1)
            for orders in carrier_orders.values()
        )
        span_overlap = any(
            later[0] < earlier[1]
            for earlier, later in zip(spans, spans[1:], strict=False)
        )
        page_bases[page.page_idx] = (
            "provider_attested"
            if span_overlap or order_conflict
            else "native_proven"
        )
        if page.visual_only is not None:
            visual = visual_page_occurrence(
                proof.identity,
                page_idx=page.page_idx,
                artifact=page.visual_only.visual_artifact,
                semantic_text=page.visual_only.semantic_text,
                semantic_text_sha256=page.visual_only.semantic_text_sha256,
            )
            occurrences.append(visual)
            gaps.append(
                _AuditedNativeGap(
                    page_idx=page.page_idx,
                    word_order_span=(0, 0),
                    occurrences=(visual,),
                    predecessor=None,
                    successor=None,
                    relation="page_only",
                )
            )
    return _NativeAuditPlan(
        occurrences=tuple(occurrences),
        gaps=tuple(gaps),
        page_bases=page_bases,
    )


def _validate_reconciliation(
    normalized_ir: dict[str, Any], *, findings: list[AuditFinding]
) -> bool:
    try:
        version = validate_normalized_ir_contract(normalized_ir)
    except NormalizedIRVersionError as exc:
        _audit_error(
            findings,
            (
                "structure_proof_invalid"
                if exc.reason_code.startswith("structure_proof_")
                else "normalized_ir_version_invalid"
            ),
            f"{exc.reason_code}: {exc}",
        )
        return False
    try:
        assessment = assess_normalized_ir_table_reconciliation(normalized_ir)
    except ValueError as exc:
        _audit_error(
            findings,
            "table_reconciliation_invalid",
            str(exc),
        )
        return False
    try:
        validate_reconciliation_generation(
            version=version,
            algorithm_version=assessment.algorithm_version,
        )
    except NormalizedIRVersionError as exc:
        _audit_error(
            findings,
            "table_reconciliation_contract_mismatch",
            f"{exc.reason_code}: {exc}",
        )
        return False
    return True


def _validate_source_dispositions(
    values: Iterable[Mapping[str, Any]],
    *,
    source: _SourceIndex,
    structure: _StructureProofIndex,
    findings: list[AuditFinding],
) -> _DispositionState:
    state = _DispositionState()
    seen_refs: set[str] = set()

    allowed = {("external_metadata", "proven_running_furniture")}
    for position, raw in enumerate(values):
        proof = dict(raw)
        role = proof.get("role")
        reason = proof.get("reason")
        if not isinstance(role, str) or not isinstance(reason, str):
            _audit_error(
                findings,
                "source_disposition_invalid",
                f"disposition {position} requires role and reason",
            )
            continue
        resolved_ref = _resolve_disposition(proof, source=source, findings=findings)
        if resolved_ref is None:
            continue
        ref = resolved_ref
        if ref in seen_refs:
            _audit_error(
                findings,
                "source_disposition_duplicate",
                "one source atom cannot have multiple dispositions",
                source_ref=ref,
            )
            continue
        seen_refs.add(ref)
        if (role, reason) not in allowed:
            _audit_error(
                findings,
                "source_disposition_role_invalid",
                f"unsupported disposition {role}/{reason}",
                source_ref=ref,
            )
            continue
        source_index = source.elements[ref].get("source_item_index")
        valid = isinstance(source_index, int) and not isinstance(source_index, bool)
        if valid and reason == "proven_running_furniture":
            valid = source_index in structure.frame_source_indices
        if not valid:
            _audit_error(
                findings,
                "source_disposition_proof_invalid",
                f"source does not prove {role}/{reason}",
                source_ref=ref,
            )
            continue
        state.external_refs.add(ref)
    return state


def _resolve_source_identity(
    identity: Mapping[str, Any],
    *,
    source: _SourceIndex,
    on_unresolved: Callable[[], None],
) -> tuple[set[str], bool] | None:
    """Resolve ir_id/source_item_index/order_index to candidate source atoms.

    Returns (candidate refs, whether any identity field was supplied), or None
    when a supplied field does not resolve — in that case ``on_unresolved`` has
    already emitted the caller's finding and the caller must return None.  Each
    caller keeps its own conflict/unresolved codes and any extra validation.
    """

    candidates: set[str] = set()
    supplied = False
    ir_id = identity.get("ir_id")
    if ir_id is not None:
        supplied = True
        if not isinstance(ir_id, str) or ir_id not in source.by_ir_id:
            on_unresolved()
            return None
        candidates.add(source.by_ir_id[ir_id])
    source_index = identity.get("source_item_index")
    if source_index is not None:
        supplied = True
        if (
            not isinstance(source_index, int)
            or source_index not in source.by_source_item_index
        ):
            on_unresolved()
            return None
        candidates.add(source.by_source_item_index[source_index])
    order_index = identity.get("order_index")
    if order_index is not None:
        supplied = True
        if not isinstance(order_index, int) or order_index not in source.by_order_index:
            on_unresolved()
            return None
        candidates.add(source.by_order_index[order_index])
    return candidates, supplied


def _resolve_disposition(
    proof: Mapping[str, Any],
    *,
    source: _SourceIndex,
    findings: list[AuditFinding],
) -> str | None:
    resolved = _resolve_source_identity(
        proof,
        source=source,
        on_unresolved=lambda: _unresolved_disposition(findings),
    )
    if resolved is None:
        return None
    candidates, supplied = resolved
    if not supplied or len(candidates) != 1:
        _audit_error(
            findings,
            (
                "source_disposition_identity_conflict"
                if len(candidates) > 1
                else "source_disposition_identity_unresolved"
            ),
            "source disposition must resolve to exactly one source atom",
        )
        return None
    return next(iter(candidates))


def _unresolved_disposition(findings: list[AuditFinding]) -> None:
    _audit_error(
        findings,
        "source_disposition_identity_unresolved",
        "a supplied source-disposition identity does not exist",
    )
    return None


def _independent_marker_value(text: str) -> str | None:
    compact = _norm(text).rstrip("。.")
    checked = "√☑✓"
    unchecked = "□☐"
    if re.fullmatch(rf"[{checked}]适用[{unchecked}]不适用", compact):
        return "applicable"
    if re.fullmatch(rf"[{unchecked}]适用[{checked}]不适用", compact):
        return "not_applicable"
    return None


def _projection_unit_declaration(text: str) -> str | None:
    """Replay one complete measurement-unit declaration, never a substring.

    The declaration grammar itself is source-format knowledge, so it stays in
    one place; the audit only re-derives the published scalar from it.
    """

    units = {
        re.sub(r"\s+", "", value)
        for label, value in content_annotations.parse_unit_declarations(text)
        if label.endswith("单位")
    }
    return next(iter(units)) if len(units) == 1 else None


def _audit_clean_text(value: str) -> str:
    cleaned = "".join(
        char
        for char in value
        if not (unicodedata.category(char) == "Cc" and char not in "\n\t")
    )
    return "\n".join(
        line.strip() for line in cleaned.splitlines() if line.strip()
    ).strip()


def _validate_source_visual_evidence(
    *,
    source: _SourceIndex,
    source_proof: SourceEvidenceProof,
    state: _CoverageState,
    findings: list[AuditFinding],
) -> None:
    expected_by_ref: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    owned_roles_by_ref: dict[str, set[str]] = defaultdict(set)
    for binding in source_proof.visual_bindings:
        descriptor = binding.artifact.as_dict()
        ref = source.by_source_item_index.get(binding.source_item_index)
        if ref is None or source.elements[ref].get("page_idx") != binding.page_idx:
            _audit_error(
                findings,
                "source_visual_binding_invalid",
                "source visual binding does not resolve to its declared source page",
                source_ref=ref,
            )
            continue
        expected_by_ref[ref][binding.artifact.artifact_role] = descriptor
    for ref, element in source.elements.items():
        raw_descriptor = element.get("_required_visual_artifact")
        if isinstance(raw_descriptor, Mapping):
            descriptor = dict(raw_descriptor)
            expected_by_ref[ref][str(descriptor["artifact_role"])] = descriptor
        owned_roles_by_ref[ref].update(expected_by_ref[ref])
        source_index = element.get("source_item_index")
        if (
            isinstance(source_index, int)
            and not isinstance(source_index, bool)
            and str(element.get("image_path") or "").strip()
        ):
            owned_roles_by_ref[ref].add(f"evidence_image_{source_index:06d}")
        table = element.get("table")
        media = table.get("embedded_media") if isinstance(table, Mapping) else None
        if isinstance(media, list):
            owned_roles_by_ref[ref].update(
                str(item["artifact_role"])
                for item in media
                if isinstance(item, Mapping)
                and isinstance(item.get("artifact_role"), str)
            )
    known_roles = {
        role for descriptors in expected_by_ref.values() for role in descriptors
    }
    carriers_by_id = {
        carrier.carrier_id: carrier for carrier in state.carrier_occurrences
    }
    for carrier in state.carrier_occurrences:
        refs = set(carrier.payload_refs)
        # Mixed parts are separately audited carriers. Requiring the outer
        # envelope to duplicate every child visual descriptor creates two
        # ownership claims for one artifact and breaks leaf-only native
        # recovery. The envelope owns no child bytes of its own.
        owned_refs = set(refs)
        owned_refs.update(
            ref
            for part_refs, _carrier_id in carrier.container_parts
            for ref in part_refs
        )
        expected: dict[str, dict[str, Any]] = {}
        for ref in refs:
            expected.update(expected_by_ref.get(ref, {}))
        owned_roles = {
            role for ref in owned_refs for role in owned_roles_by_ref.get(ref, ())
        }
        actual: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for role, descriptor in carrier.artifacts:
            actual[role].append(descriptor)
        child_actual: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for _part_refs, child_id in carrier.container_parts:
            child = carriers_by_id.get(child_id)
            if child is None:
                continue
            for role, descriptor in child.artifacts:
                child_actual[role].append(descriptor)
        for role, descriptor in expected.items():
            if actual.get(role) != [descriptor]:
                _audit_error(
                    findings,
                    "source_visual_evidence_missing",
                    f"{carrier.carrier_id} lacks exact visual descriptor {role}",
                    unit_order=carrier.unit_order,
                )
        for role in set(actual) & set(child_actual) - set(expected):
            if actual[role] != child_actual[role]:
                _audit_error(
                    findings,
                    "source_visual_evidence_missing",
                    f"{carrier.carrier_id} carries a forged child visual {role}",
                    unit_order=carrier.unit_order,
                )
        for role in set(actual) & known_roles - set(expected) - owned_roles:
            _audit_error(
                findings,
                "source_visual_evidence_misbound",
                f"source visual role {role} is bound outside its carrier",
                unit_order=carrier.unit_order,
            )
        for role in set(actual) - owned_roles:
            _audit_error(
                findings,
                "evidence_artifact_unowned",
                f"{carrier.carrier_id} binds unowned evidence artifact {role}",
                unit_order=carrier.unit_order,
            )


def _validate_native_gap_units(
    units: list[AuditUnitView],
    *,
    native_plan: _NativeAuditPlan,
    source: _SourceIndex,
    state: _CoverageState,
    findings: list[AuditFinding],
) -> None:
    """Validate native leaves inside independently chosen coarse owners."""

    units_by_order = {unit.order_index: unit for unit in units}
    carriers = {
        occurrence.carrier_id: occurrence for occurrence in state.carrier_occurrences
    }
    native_gap_refs: set[str] = set()
    valid_native_carriers: set[str] = set()
    last_native_part_by_anchor: dict[tuple[int, int, int], tuple[int, int]] = {}
    for gap in native_plan.gaps:
        refs = tuple(
            source.by_identity.get(identity)
            for item in gap.occurrences
            if (identity := source_ref_identity(item.source_ref)) is not None
        )
        if len(refs) != len(gap.occurrences) or any(ref is None for ref in refs):
            _audit_error(
                findings,
                "source_native_gap_identity_missing",
                "native gap occurrence has no audited source identity",
            )
            continue
        resolved_refs = cast(tuple[str, ...], refs)
        native_gap_refs.update(resolved_refs)
        gap_texts = [item.text for item in gap.occurrences]
        if all(isinstance(text, str) for text in gap_texts) and punctuation_only_text(
            "".join(cast(list[str], gap_texts))
        ):
            # The builder suppresses pure leader/placeholder runs; the
            # audit re-derives that from its own partition. Absence is
            # the only legal state — publishing such a run is rejected.
            for ref in resolved_refs:
                if state.selector_claims.get(ref):
                    _audit_error(
                        findings,
                        "source_native_gap_suppression_invalid",
                        "punctuation-only native run must not publish",
                        source_ref=ref,
                    )
            continue
        punctuation_refs: set[str] = set()
        ref_cursor = 0
        for occurrences in _native_expected_part_groups(gap.occurrences):
            group_refs = resolved_refs[ref_cursor : ref_cursor + len(occurrences)]
            ref_cursor += len(occurrences)
            group_text = (
                "".join(str(occurrence.text) for occurrence in occurrences)
                if all(occurrence.text is not None for occurrence in occurrences)
                else ""
            )
            if group_text and punctuation_only_text(group_text):
                punctuation_refs.update(group_refs)
                for ref in group_refs:
                    if state.selector_claims.get(ref):
                        _audit_error(
                            findings,
                            "source_native_gap_suppression_invalid",
                            "punctuation-only native leaf must not publish",
                            source_ref=ref,
                        )
                continue
        terminal_refs = punctuation_refs
        emitted_refs = tuple(ref for ref in resolved_refs if ref not in terminal_refs)
        if not emitted_refs:
            continue
        owners: list[int] = []
        for ref in emitted_refs:
            element = source.elements[ref]
            claims = state.selector_claims.get(ref, [])
            payload_claims = [claim for claim in claims if claim.role == "payload"]
            if len(payload_claims) != 1:
                _audit_error(
                    findings,
                    "source_native_payload_count_invalid",
                    "source-native occurrence requires exactly one payload "
                    f"owner, got {len(payload_claims)}",
                    source_ref=ref,
                )
            else:
                owners.append(payload_claims[0].unit_order)
            if any(claim.role != "payload" for claim in claims):
                _audit_error(
                    findings,
                    "source_native_role_invalid",
                    "source-native occurrence has a non-payload owner",
                    source_ref=ref,
                )
            expected_field = "image" if element.get("kind") == "image" else "text"
            if any(claim.kind != expected_field for claim in payload_claims):
                _audit_error(
                    findings,
                    "source_native_selector_invalid",
                    "source-native selector kind is not physical",
                    source_ref=ref,
                )
        owner_orders = set(owners)
        unit = (
            units_by_order.get(next(iter(owner_orders)))
            if len(owners) == len(emitted_refs) and len(owner_orders) == 1
            else None
        )
        if unit is None:
            _audit_error(
                findings,
                "source_native_gap_unit_count_invalid",
                "one maximal native gap requires exactly one coarse owner, "
                f"got {len(owner_orders)}",
            )
            continue
        predecessor_owner = _native_anchor_owner_order(
            gap.predecessor,
            source=source,
            state=state,
        )
        successor_owner = _native_anchor_owner_order(
            gap.successor,
            source=source,
            state=state,
        )
        page_basis = native_plan.page_bases.get(gap.page_idx)
        if page_basis is None:
            _audit_error(
                findings,
                "source_native_physical_context_invalid",
                "native gap page has no audited order basis",
                unit_order=unit.order_index,
            )
            continue
        root_owner = (
            unit.heading_path == []
            and unit.payload_kind == "mixed"
            and unit.payload.get("semantic_type") == "document"
        )
        expected_owner = (
            predecessor_owner
            if gap.relation in {"bounded_by_same_source", "page_suffix"}
            else (
                predecessor_owner
                if gap.relation == "between_mapped_sources"
                and predecessor_owner is not None
                and predecessor_owner == successor_owner
                else None
            )
        )
        if (
            expected_owner is not None
            and unit.order_index != expected_owner
            and not root_owner
        ):
            _audit_error(
                findings,
                "source_native_owner_invalid",
                "native gap is neither in its proven owner nor conservatively "
                "flattened to a document-root segment",
                unit_order=unit.order_index,
            )
        if expected_owner is None and not root_owner:
            _audit_error(
                findings,
                "source_native_owner_invalid",
                "unresolved native gap must flatten to a document-root segment",
                unit_order=unit.order_index,
            )
        root_fallback = root_owner and unit.order_index != expected_owner
        if root_fallback:
            root_order_valid = True
            # A coalesced document-root owner may contain both the mapped
            # anchor carrier and the native leaf.  Outer unit order cannot
            # distinguish them; exact relative order is checked below against
            # the mixed part indices.  Compare unit orders only when the
            # anchor remains in a different durable owner.
            if (
                predecessor_owner is not None
                and predecessor_owner != unit.order_index
            ):
                root_order_valid = unit.order_index > predecessor_owner
            if (
                root_order_valid
                and page_basis == "native_proven"
                and successor_owner is not None
                and successor_owner != predecessor_owner
                and successor_owner != unit.order_index
            ):
                root_order_valid = unit.order_index < successor_owner
            if not root_order_valid:
                _audit_error(
                    findings,
                    "source_native_linearization_invalid",
                    "document-root native segment contradicts mapped anchors",
                    unit_order=unit.order_index,
                )
        part_indices, carrier_ids = _validate_native_gap_parts(
            unit,
            gap=gap,
            expected_refs=resolved_refs,
            terminal_refs=terminal_refs,
            outer=carriers.get(f"unit:{unit.order_index}"),
            source=source,
            page_basis=page_basis,
            anchor_heading_path=list(unit.heading_path),
            findings=findings,
        )
        valid_native_carriers.update(carrier_ids)
        if part_indices:
            predecessor_source_index = (
                gap.predecessor.source_item_index
                if gap.predecessor is not None
                else -1
            )
            anchor_key = (
                unit.order_index,
                gap.page_idx,
                predecessor_source_index,
            )
            previous = last_native_part_by_anchor.get(anchor_key)
            if previous is not None and (
                gap.word_order_span[0] <= previous[0]
                or part_indices[0] <= previous[1]
            ):
                _audit_error(
                    findings,
                    "source_native_linearization_invalid",
                    "native gaps sharing one anchor are not physically ordered",
                    unit_order=unit.order_index,
                )
            last_native_part_by_anchor[anchor_key] = (
                gap.word_order_span[0],
                part_indices[-1],
            )
        if isinstance(unit.artifact_locator, Mapping) and (
            "review_reason" in unit.artifact_locator
        ):
            _audit_error(
                findings,
                "source_native_physical_context_invalid",
                "placement has no review lane: review_reason is retired",
                unit_order=unit.order_index,
            )
        predecessor = gap.predecessor
        if predecessor is None:
            continue
        predecessor_identity = source_ref_identity(predecessor.source_ref)
        predecessor_ref = (
            source.by_identity.get(predecessor_identity)
            if predecessor_identity is not None
            else None
        )
        claims = (
            state.selector_claims.get(predecessor_ref, [])
            if predecessor_ref is not None
            else []
        )
        owner_orders = {
            claim.unit_order for claim in claims if claim.role == "payload"
        }
        if gap.relation == "bounded_by_same_source":
            # The audit re-derives the containment owner from its own
            # partition: proven interior words the owner's payload missed
            # mean the owner may not publish as a silent ok.
            for owner_order in owner_orders:
                owner_unit = units_by_order.get(owner_order)
                if owner_unit is not None and owner_unit.quality_status == "ok":
                    _audit_error(
                        findings,
                        "source_coverage_gap_unflagged",
                        "containment-proven native gap requires its owner "
                        "unit to be marked for review",
                        unit_order=owner_order,
                    )

    for unit in units:
        graph = _unit_projection_graph(unit)
        if graph is not None and (
            graph.get("physical_context") is not None or graph.get("search_atoms") != []
        ):
            _audit_error(
                findings,
                "source_native_gap_membership_invalid",
                "coarse owner cannot carry native gap context or retrieval runs",
                unit_order=unit.order_index,
            )
        unit_refs = state.refs_by_unit.get(unit.order_index, set())
        outer = carriers.get(f"unit:{unit.order_index}")
        if outer is not None and isinstance(unit.payload.get("parts"), list):
            for part, (_bindings, carrier_id) in zip(
                cast(list[object], unit.payload["parts"]),
                outer.container_parts,
                strict=True,
            ):
                locator = (
                    part.get("artifact_locator")
                    if isinstance(part, Mapping)
                    else None
                )
                child_graph = (
                    locator.get("source_projection")
                    if isinstance(locator, Mapping)
                    else None
                )
                if (
                    isinstance(child_graph, Mapping)
                    and child_graph.get("physical_context") is not None
                    and carrier_id not in valid_native_carriers
                ):
                    _audit_error(
                        findings,
                        "source_native_physical_context_invalid",
                        "ordinary owner leaf carries an unproved native context",
                        unit_order=unit.order_index,
                    )
        if not (unit_refs & native_gap_refs):
            continue
        if outer is None or not outer.container_parts:
            _audit_error(
                findings,
                "source_native_independent_unit_invalid",
                "native evidence must be a mixed owner leaf, not a top-level gap",
                unit_order=unit.order_index,
            )
            continue
        native_only = all(
            bool(bindings)
            and all(
                "_native_source_ref" in source.elements[ref]
                for ref in bindings
            )
            for bindings, _carrier_id in outer.container_parts
        )
        if native_only and (
            unit.heading_path != []
            or unit.title is not None
            or unit.applicability is not None
            or unit.semantic_key != "document_content"
            or unit.semantic_keys != ["document_content"]
            or unit.payload.get("semantic_type") != "document"
        ):
            _audit_error(
                findings,
                "source_native_root_projection_invalid",
                "native-only root segment cannot publish title, section, "
                "applicability, or business taxonomy",
                unit_order=unit.order_index,
            )


def _validate_native_gap_parts(
    unit: AuditUnitView,
    *,
    gap: _AuditedNativeGap,
    expected_refs: tuple[str, ...],
    terminal_refs: set[str],
    outer: _CarrierOccurrence | None,
    source: _SourceIndex,
    page_basis: str,
    anchor_heading_path: list[str],
    findings: list[AuditFinding],
) -> tuple[list[int], set[str]]:
    parts = unit.payload.get("parts")
    if (
        unit.payload_kind != "mixed"
        or not isinstance(parts, list)
        or outer is None
        or len(outer.container_parts) != len(parts)
    ):
        _audit_error(
            findings,
            "source_native_independent_unit_invalid",
            "native gap requires leaves inside one mixed coarse owner",
            unit_order=unit.order_index,
        )
        return [], set()

    gap_ref_set = set(expected_refs) - terminal_refs
    selected: list[tuple[int, Mapping[str, Any], frozenset[str], str]] = []
    for owner_part_index, (part, (bindings, carrier_id)) in enumerate(
        zip(parts, outer.container_parts, strict=True)
    ):
        overlap = set(bindings) & gap_ref_set
        if not overlap:
            continue
        if set(bindings) - gap_ref_set or not isinstance(part, Mapping):
            _audit_error(
                findings,
                "source_native_gap_membership_invalid",
                "native leaf mixes one gap with another source",
                unit_order=unit.order_index,
            )
            continue
        if "/part:" not in carrier_id:
            _audit_error(
                findings,
                "source_native_independent_unit_invalid",
                "native claim is not owned by a mixed part",
                unit_order=unit.order_index,
            )
        selected.append((owner_part_index, part, bindings, carrier_id))

    if set().union(
        *(set(bindings) for _index, _part, bindings, _carrier in selected)
    ) != gap_ref_set:
        _audit_error(
            findings,
            "source_native_gap_membership_invalid",
            "native owner leaves do not exactly cover one maximal gap",
            unit_order=unit.order_index,
        )
        return [], set()

    selected_indices = [index for index, _part, _bindings, _carrier in selected]
    if selected_indices != list(
        range(selected_indices[0], selected_indices[-1] + 1)
    ):
        _audit_error(
            findings,
            "source_native_linearization_invalid",
            "one native gap is split by unrelated owner leaves",
            unit_order=unit.order_index,
        )

    expected_groups: list[
        tuple[tuple[SourceNativeOccurrence, ...], set[str]]
    ] = []
    ref_cursor = 0
    for occurrences in _native_expected_part_groups(gap.occurrences):
        group_refs = set(
            expected_refs[ref_cursor : ref_cursor + len(occurrences)]
        )
        ref_cursor += len(occurrences)
        terminal_overlap = group_refs & terminal_refs
        if terminal_overlap:
            if terminal_overlap != group_refs:
                _audit_error(
                    findings,
                    "source_native_support_terminal_invalid",
                    "one retrieval-run leaf cannot be partly terminal and partly payload",
                    unit_order=unit.order_index,
                )
            continue
        expected_groups.append((occurrences, group_refs))
    if len(selected) != len(expected_groups):
        _audit_error(
            findings,
            "source_native_gap_membership_invalid",
            "native leaves do not preserve retrieval-run grouping",
            unit_order=unit.order_index,
        )
        return selected_indices, {
            carrier_id for _index, _part, _bindings, carrier_id in selected
        }
    for part_index, (
        (_owner_part_index, part, bindings, _carrier_id),
        (occurrences, expected_group_refs),
    ) in enumerate(
        zip(selected, expected_groups, strict=True)
    ):
        expected_kind = "text" if occurrences[0].text is not None else "image"
        expected_text = (
            "".join(str(occurrence.text) for occurrence in occurrences)
            if expected_kind == "text"
            else None
        )
        locator = part.get("artifact_locator")
        child_graph = (
            locator.get("source_projection") if isinstance(locator, Mapping) else None
        )
        payload_edge = (
            child_graph.get("payload") if isinstance(child_graph, Mapping) else None
        )
        if (
            expected_text is not None
            and isinstance(payload_edge, Mapping)
            and str(payload_edge.get("transform", "")).startswith("safe_")
        ):
            safe_expected = semantic_payload_without_unsafe_glyphs(expected_text)
            expected_text = safe_expected if isinstance(safe_expected, str) else None
        if (
            set(bindings) != expected_group_refs
            or part.get("kind") != expected_kind
            or part.get("order") != occurrences[0].word_order
            or (expected_text is not None and part.get("text") != expected_text)
            or part.get("heading_path", []) != []
            or part.get("applicability") is not None
        ):
            _audit_error(
                findings,
                "source_native_gap_membership_invalid",
                f"native gap part {part_index} differs from its occurrence",
                unit_order=unit.order_index,
            )
            continue
        expected_alternative = bool(
            expected_text is not None
            and _native_group_is_owner_search_alternative(
                gap=gap,
                text=expected_text,
                source=source,
            )
        )
        alternative_shape_valid = (
            part.get("representation_role")
            == "unresolved_source_alternative"
            and part.get("search_policy") == "none"
            and part.get("quality_status") == "needs_review"
        )
        if expected_alternative != alternative_shape_valid or (
            not expected_alternative
            and (
                "representation_role" in part
                or "search_policy" in part
            )
        ):
            _audit_error(
                findings,
                "source_native_alternative_role_invalid",
                (
                    "native leaf does not match its independently replayed "
                    "owner-search role"
                ),
                unit_order=unit.order_index,
            )
        if (
            not isinstance(child_graph, Mapping)
            or child_graph.get("search_atoms") != []
            or not _native_context_matches(
                child_graph.get("physical_context"),
                gap=gap,
                page_basis=page_basis,
                anchor_heading_path=anchor_heading_path,
            )
            or (
                child_graph.get("search_targets")
                != ([] if expected_alternative else ["payload.text"])
                if expected_kind == "text"
                else child_graph.get("search_targets") != []
            )
        ):
            _audit_error(
                findings,
                "source_native_gap_membership_invalid",
                f"native gap part {part_index} source edge is not exact",
                unit_order=unit.order_index,
            )
    _validate_native_part_anchor_order(
        unit,
        gap=gap,
        selected_indices=selected_indices,
        outer=outer,
        source=source,
        page_basis=page_basis,
        findings=findings,
    )
    return selected_indices, {
        carrier_id for _index, _part, _bindings, carrier_id in selected
    }


def _native_group_is_owner_search_alternative(
    *,
    gap: _AuditedNativeGap,
    text: str,
    source: _SourceIndex,
) -> bool:
    """Recompute a coarse-owner search redirect without claiming an alias."""

    predecessor = gap.predecessor
    successor = gap.successor
    if (
        gap.relation != "bounded_by_same_source"
        or predecessor is None
        or successor is None
        or predecessor.source_item_index != successor.source_item_index
    ):
        return False
    owner_ref = source.by_source_item_index.get(predecessor.source_item_index)
    if owner_ref is None:
        return False
    residual = strict_source_comparison_text(text)
    if not residual or punctuation_only_text(text):
        return False
    return any(
        residual in surface
        for surface in source_carrier_search_surfaces(source.elements[owner_ref])
    )


def _native_anchor_owner_order(
    anchor: SourceMappedAnchor | None,
    *,
    source: _SourceIndex,
    state: _CoverageState,
) -> int | None:
    ref = _native_anchor_ref(anchor, source=source)
    if ref is None:
        return None
    owners = {
        claim.unit_order
        for claim in state.selector_claims.get(ref, ())
        if claim.role == "payload"
    }
    return next(iter(owners)) if len(owners) == 1 else None


def _native_anchor_ref(
    anchor: SourceMappedAnchor | None,
    *,
    source: _SourceIndex,
) -> str | None:
    if anchor is None:
        return None
    identity = source_ref_identity(anchor.source_ref)
    return source.by_identity.get(identity) if identity is not None else None


def _validate_native_part_anchor_order(
    unit: AuditUnitView,
    *,
    gap: _AuditedNativeGap,
    selected_indices: list[int],
    outer: _CarrierOccurrence,
    source: _SourceIndex,
    page_basis: str,
    findings: list[AuditFinding],
) -> None:
    if not selected_indices:
        return

    def bound_part_index(ref: str | None) -> int | None:
        if ref is None:
            return None
        indices = [
            index
            for index, (bindings, _carrier_id) in enumerate(outer.container_parts)
            if ref in bindings
        ]
        return indices[0] if len(indices) == 1 else None

    predecessor_index = bound_part_index(
        _native_anchor_ref(gap.predecessor, source=source)
    )
    successor_index = bound_part_index(
        _native_anchor_ref(gap.successor, source=source)
    )
    valid = True
    if predecessor_index is not None:
        valid = selected_indices[0] > predecessor_index
    if (
        valid
        and page_basis == "native_proven"
        and gap.relation == "between_mapped_sources"
        and successor_index is not None
        and successor_index != predecessor_index
    ):
        valid = selected_indices[-1] < successor_index
    if (
        valid
        and gap.relation == "page_prefix"
        and successor_index is not None
        and page_basis == "native_proven"
    ):
        valid = selected_indices[-1] < successor_index
    if not valid:
        _audit_error(
            findings,
            "source_native_linearization_invalid",
            "native owner leaves contradict their mapped anchors",
            unit_order=unit.order_index,
        )


def _native_expected_part_groups(
    occurrences: tuple[SourceNativeOccurrence, ...],
) -> tuple[tuple[SourceNativeOccurrence, ...], ...]:
    groups: list[tuple[SourceNativeOccurrence, ...]] = []
    pending: list[SourceNativeOccurrence] = []
    for occurrence in occurrences:
        if occurrence.text is None:
            if pending:
                groups.append(tuple(pending))
                pending.clear()
            groups.append((occurrence,))
            continue
        if pending and (
            occurrence.retrieval_run is None
            or occurrence.retrieval_run != pending[-1].retrieval_run
        ):
            groups.append(tuple(pending))
            pending.clear()
        pending.append(occurrence)
    if pending:
        groups.append(tuple(pending))
    return tuple(groups)


def _native_context_matches(
    raw: object,
    *,
    gap: _AuditedNativeGap,
    page_basis: str,
    anchor_heading_path: list[str],
) -> bool:
    if not isinstance(raw, Mapping) or set(raw) != {
        "version",
        "scope",
        "source_evidence_sha256",
        "source_pdf_sha256",
        "page_idx",
        "page_no",
        "word_order_span",
        "predecessor",
        "successor",
        "relation",
        "order_basis",
        "containment_owner",
        "page_order_basis",
        "anchor_heading_path",
    }:
        return False
    if gap.relation == "bounded_by_same_source" and gap.predecessor is not None:
        expected_basis = "containment_proven"
        expected_owner: int | None = gap.predecessor.source_item_index
    else:
        expected_basis = page_basis
        expected_owner = None
    first_ref = gap.occurrences[0].source_ref
    return (
        raw.get("version") == "source-native-placement.v2"
        and raw.get("scope") == "native_gap"
        and raw.get("source_evidence_sha256") == first_ref["source_evidence_sha256"]
        and raw.get("source_pdf_sha256") == first_ref["source_pdf_sha256"]
        and raw.get("page_idx") == gap.page_idx
        and raw.get("page_no") == gap.page_idx + 1
        and raw.get("word_order_span") == list(gap.word_order_span)
        and raw.get("predecessor")
        == (gap.predecessor.as_dict() if gap.predecessor is not None else None)
        and raw.get("successor")
        == (gap.successor.as_dict() if gap.successor is not None else None)
        and raw.get("relation") == gap.relation
        and raw.get("order_basis") == expected_basis
        and raw.get("containment_owner") == expected_owner
        and raw.get("page_order_basis") == page_basis
        and raw.get("anchor_heading_path") == anchor_heading_path
    )


def _unit_projection_graph(
    unit: AuditUnitView,
) -> Mapping[str, Any] | None:
    locator = unit.artifact_locator
    graph = locator.get("source_projection") if isinstance(locator, Mapping) else None
    return graph if isinstance(graph, Mapping) else None


def _collect_unit_coverage(
    units: list[AuditUnitView],
    *,
    source: _SourceIndex,
    findings: list[AuditFinding],
) -> _CoverageState:
    state = _CoverageState()
    for unit in units:
        unit_carrier_id = f"unit:{unit.order_index}"
        state.carrier_quality[unit_carrier_id] = unit.quality_status
        _validate_payload_field_closure(
            payload_kind=unit.payload_kind,
            payload=unit.payload,
            unit=unit,
            findings=findings,
        )
        refs: set[str] = set()
        role_refs: dict[str, set[str]] = defaultdict(set)
        outer_occurrence_index = len(state.carrier_occurrences)
        if unit.artifact_locator is None:
            _audit_error(
                findings,
                "unit_locator_missing",
                "source-derived unit has no artifact locator",
                unit_order=unit.order_index,
            )
        else:
            graph_present, refs = _collect_projection_graph(
                unit.artifact_locator,
                payload_kind=unit.payload_kind,
                payload=unit.payload,
                heading_path=unit.heading_path,
                applicability=unit.applicability,
                carrier_id=unit_carrier_id,
                source=source,
                state=state,
                findings=findings,
                unit=unit,
                local_roles=role_refs,
            )
            if not graph_present:
                _projection_finding(
                    findings,
                    unit=unit,
                    code="source_projection_missing",
                    message=(
                        "every source-derived unit requires a typed "
                        "unit-source-projection.v4 graph"
                    ),
                )
            if not refs and unit.payload_kind != "mixed":
                _audit_error(
                    findings,
                    "unit_locator_unresolved",
                    "unit locator has no resolvable source identity",
                    unit_order=unit.order_index,
                )
        payload_refs = role_refs["payload"]
        state.refs_by_unit[unit.order_index] = set(payload_refs)
        for ref in payload_refs:
            element = source.elements[ref]
            if element.get("kind") == "table" and _carrier_projects_table(
                state,
                carrier_id=f"unit:{unit.order_index}",
                ref=ref,
            ):
                state.table_payloads[ref].append(
                    (f"unit:{unit.order_index}", unit.payload)
                )
            if element.get("kind") in {"image", "equation"} or (
                is_visual_only_table_element(element)
            ):
                state.image_payloads[ref].append(
                    (f"unit:{unit.order_index}", unit.payload)
                )
        if unit.payload_kind != "mixed":
            continue
        parts = unit.payload.get("parts")
        if not isinstance(parts, list) or not parts:
            continue
        container_parts: list[tuple[frozenset[str], str]] = []
        for part_index, part in enumerate(parts):
            carrier_id = f"unit:{unit.order_index}/part:{part_index}"
            if not isinstance(part, dict):
                container_parts.append((frozenset(), carrier_id))
                continue
            container_parts.append((frozenset(), carrier_id))
            binding_index = len(container_parts) - 1
            part_kind = part.get("kind")
            if part_kind not in {"text", "table", "image"}:
                _projection_finding(
                    findings,
                    unit=unit,
                    code="mixed_part_kind_invalid",
                    message="mixed part kind is outside the public sum type",
                )
            else:
                _validate_payload_field_closure(
                    payload_kind=("text" if part_kind == "image" else part_kind),
                    payload=part,
                    unit=unit,
                    findings=findings,
                    mixed_part=True,
                )
            raw_quality = part.get("quality_status", "ok")
            state.carrier_quality[carrier_id] = (
                raw_quality if isinstance(raw_quality, str) else ""
            )
            if raw_quality not in {"ok", "needs_review", "unusable"}:
                _projection_finding(
                    findings,
                    unit=unit,
                    code="mixed_part_quality_status_invalid",
                    message="mixed part quality_status is outside the public enum",
                )
            locator = part.get("artifact_locator")
            if not isinstance(locator, dict):
                _audit_error(
                    findings,
                    "mixed_part_locator_missing",
                    "mixed source part has no artifact locator",
                    unit_order=unit.order_index,
                )
                continue
            part_roles: dict[str, set[str]] = defaultdict(set)
            raw_heading_path = part.get("heading_path", [])
            if not isinstance(raw_heading_path, list) or any(
                not isinstance(value, str) or not value.strip()
                for value in raw_heading_path
            ):
                _projection_finding(
                    findings,
                    unit=unit,
                    code="mixed_part_heading_path_invalid",
                    message="mixed part heading_path must be an array of non-empty strings",
                )
                part_heading_path: list[str] = []
            else:
                part_heading_path = list(raw_heading_path)
            raw_applicability = part.get("applicability")
            if raw_applicability not in {None, "applicable", "not_applicable"}:
                _projection_finding(
                    findings,
                    unit=unit,
                    code="mixed_part_applicability_invalid",
                    message="mixed part applicability is outside the public enum",
                )
                part_applicability = None
            else:
                part_applicability = raw_applicability
            graph_present, part_refs = _collect_projection_graph(
                locator,
                payload_kind=str(part_kind),
                payload=part,
                heading_path=part_heading_path,
                applicability=part_applicability,
                carrier_id=carrier_id,
                source=source,
                state=state,
                findings=findings,
                unit=unit,
                local_roles=part_roles,
            )
            if not graph_present:
                _projection_finding(
                    findings,
                    unit=unit,
                    code="source_projection_missing",
                    message=(
                        "every mixed source part requires a typed "
                        "unit-source-projection.v4 graph"
                    ),
                )
            if not part_refs:
                _audit_error(
                    findings,
                    "mixed_part_locator_unresolved",
                    "mixed part locator has no resolvable source identity",
                    unit_order=unit.order_index,
                )
            part_payload_refs = part_roles["payload"]
            container_parts[binding_index] = (
                frozenset(part_payload_refs),
                carrier_id,
            )
            state.refs_by_unit[unit.order_index].update(part_payload_refs)
            for ref in part_payload_refs:
                element = source.elements[ref]
                if element.get("kind") == "table" and _carrier_projects_table(
                    state,
                    carrier_id=carrier_id,
                    ref=ref,
                ):
                    state.table_payloads[ref].append((carrier_id, part))
                if element.get("kind") in {"image", "equation"} or (
                    is_visual_only_table_element(element)
                ):
                    state.image_payloads[ref].append((carrier_id, part))
        _validate_mixed_container_envelope(
            unit,
            parts=parts,
            part_bindings=container_parts,
            source=source,
            findings=findings,
        )
        if len(state.carrier_occurrences) > outer_occurrence_index:
            outer = state.carrier_occurrences[outer_occurrence_index]
            if outer.carrier_id == f"unit:{unit.order_index}":
                semantic_type = unit.payload.get("semantic_type")
                state.carrier_occurrences[outer_occurrence_index] = _CarrierOccurrence(
                    unit_order=outer.unit_order,
                    carrier_id=outer.carrier_id,
                    payload_refs=frozenset(),
                    heading_path=outer.heading_path,
                    headings=outer.headings,
                    artifacts=outer.artifacts,
                    container_semantic_type=(
                        semantic_type if isinstance(semantic_type, str) else None
                    ),
                    container_parts=tuple(container_parts),
                )
    return state


def _carrier_projects_table(
    state: _CoverageState,
    *,
    carrier_id: str,
    ref: str,
) -> bool:
    return any(
        projection.carrier_id == carrier_id
        and projection.kind == "table_identity"
        and any(selector.ref == ref and selector.kind == "table" for selector in projection.selectors)
        for projection in state.payload_projections
    )


def _validate_mixed_container_envelope(
    unit: AuditUnitView,
    *,
    parts: list[object],
    part_bindings: list[tuple[frozenset[str], str]],
    source: _SourceIndex,
    findings: list[AuditFinding],
) -> None:
    """Prove that one mixed envelope is the ordered closure of its parts."""

    semantic_type = unit.payload.get("semantic_type")
    if semantic_type not in {"document", "section"}:
        _projection_finding(
            findings,
            unit=unit,
            code="mixed_container_semantic_type_invalid",
            message="mixed semantic_type must be document or section",
        )
    if unit.payload.get("order_status") != "unresolved_physical_fallback":
        _projection_finding(
            findings,
            unit=unit,
            code="mixed_container_order_status_invalid",
            message=(
                "mixed parts expose stable source order only; exact reading "
                "order is not claimed"
            ),
        )
    intervals: list[tuple[int, int]] = []
    non_furniture_parts = 0
    for part, (refs, _carrier_id) in zip(
        parts,
        part_bindings,
        strict=True,
    ):
        if not isinstance(part, dict):
            _projection_finding(
                findings,
                unit=unit,
                code="mixed_part_invalid",
                message="every mixed part must be an object",
            )
            continue
        if not refs:
            _projection_finding(
                findings,
                unit=unit,
                code="mixed_part_payload_ownership_missing",
                message="every mixed part requires typed payload ownership",
            )
            continue
        if any(source.elements[ref].get("kind") != "page_furniture" for ref in refs):
            non_furniture_parts += 1
        source_orders = sorted(
            {
                int(source.elements[ref]["order_index"])
                for ref in refs
                if isinstance(source.elements[ref].get("order_index"), int)
                and not isinstance(source.elements[ref].get("order_index"), bool)
            }
        )
        if not source_orders:
            native_orders = sorted(
                {
                    int(source.elements[ref]["_native_word_order"])
                    for ref in refs
                    if isinstance(
                        source.elements[ref].get("_native_word_order"), int
                    )
                    and not isinstance(
                        source.elements[ref].get("_native_word_order"), bool
                    )
                }
            )
            if native_orders and part.get("order") != native_orders[0]:
                _projection_finding(
                    findings,
                    unit=unit,
                    code="mixed_part_order_invalid",
                    message=(
                        "native mixed part order must equal its first physical "
                        "word order"
                    ),
                )
            continue
        interval = (source_orders[0], source_orders[-1])
        intervals.append(interval)
        part_order = part.get("order")
        if (
            not isinstance(part_order, int)
            or isinstance(part_order, bool)
            or part_order != interval[0]
        ):
            _projection_finding(
                findings,
                unit=unit,
                code="mixed_part_order_invalid",
                message="mixed part order must equal its first physical source order",
            )

    if non_furniture_parts == 0:
        _projection_finding(
            findings,
            unit=unit,
            code="mixed_container_payload_missing",
            message="mixed container cannot consist only of document furniture",
        )
    if not intervals:
        return
    if any(
        left[1] >= right[0]
        for left, right in zip(intervals, intervals[1:], strict=False)
    ):
        _projection_finding(
            findings,
            unit=unit,
            code="mixed_container_source_order_invalid",
            message="mixed parts must be non-overlapping and source ordered",
        )


def _build_structure_proof_index(
    normalized_ir: Mapping[str, Any],
    *,
    source: _SourceIndex,
    source_proof: SourceEvidenceProof,
    findings: list[AuditFinding],
) -> _StructureProofIndex:
    raw_elements = normalized_ir.get("elements")
    source_hash = normalized_ir.get("source_pdf_sha256")
    if not isinstance(raw_elements, list):
        return _StructureProofIndex({}, frozenset(), ())
    try:
        proof = validate_document_structure(
            normalized_ir.get("structure_proof"),
            elements=raw_elements,
            expected_source_pdf_sha256=(
                source_hash if isinstance(source_hash, str) else None
            ),
        )
    except DocumentStructureContractError as exc:
        _audit_error(
            findings,
            "structure_proof_invalid",
            f"{exc.reason_code}: {exc}",
        )
        return _StructureProofIndex({}, frozenset(), ())

    headings: dict[int, _ProofHeading] = {}
    for raw_heading in proof["headings"]:
        source_refs: list[_ProofSourceRef] = []
        title_parts: list[str] = []
        for raw_ref in raw_heading["source_refs"]:
            source_index = int(raw_ref["source_item_index"])
            ref = source.by_source_item_index[source_index]
            field = str(raw_ref["field"])
            index = int(raw_ref["index"]) if raw_ref.get("index") is not None else None
            start, end = (int(value) for value in raw_ref["text_span"])
            source_refs.append(
                _ProofSourceRef(
                    ref=ref,
                    field=field,
                    index=index,
                    text_span=(start, end),
                )
            )
            title_parts.append(
                _proof_source_text(
                    source.elements[ref],
                    field=field,
                    index=index,
                )[start:end]
            )
        node_id = int(raw_heading["node_id"])
        headings[node_id] = _ProofHeading(
            node_id=node_id,
            parent_node_id=(
                int(raw_heading["parent_node_id"])
                if raw_heading["parent_node_id"] is not None
                else None
            ),
            propagates=bool(raw_heading["propagates"]),
            section_start=int(raw_heading["section_span"][0]),
            section_end=int(raw_heading["section_span"][1]),
            title=_audit_clean_text("".join(title_parts)),
            source_refs=tuple(source_refs),
        )
    frames = frozenset(
        int(source_index)
        for frame in proof["page_frames"]
        for source_index in frame["member_source_item_indices"]
    )
    if proof.get("algorithm_version") in {
        OWNER_SCOPE_V1_DOCUMENT_STRUCTURE_ALGORITHM,
        OWNER_SCOPE_V2_DOCUMENT_STRUCTURE_ALGORITHM,
    } and proof.get("owner_scope_breaks"):
        # Pre-v14 breaks carry neither the current selector shape (v12) nor
        # a materialization policy (v13); auditing them against the current
        # placement semantics would guess. Fail with a typed terminal.
        _audit_error(
            findings,
            "structure_proof_reparse_required",
            "legacy owner-scope breaks cannot be audited under the current "
            "materialization contract; reparse the document",
        )
        return _StructureProofIndex({}, frozenset(), ())
    scope_breaks_list: list[_ProofOwnerScopeBreak] = []
    for value in proof.get("owner_scope_breaks", []):
        boundary = value["boundary_source_ref"]
        source_index = int(boundary["source_item_index"])
        scope_break = _ProofOwnerScopeBreak(
            boundary_source_item_index=source_index,
            boundary_ref=source.by_source_item_index[source_index],
            boundary_field=str(boundary["field"]),
            boundary_index=(
                int(boundary["index"])
                if boundary.get("index") is not None
                else None
            ),
            boundary_text_span=(
                int(boundary["text_span"][0]),
                int(boundary["text_span"][1]),
            ),
            boundary_value_sha256=str(boundary["value_sha256"]),
            page_index=int(boundary["page_index"]),
            eligibility_basis=str(value["eligibility_basis"]),
            relative_rank=str(value["relative_rank"]),
            current_owner_node_id=int(value["current_owner_node_id"]),
            target_node_id=(
                int(value["target_node_id"])
                if value["target_node_id"] is not None
                else None
            ),
            boundary_carrier_scope=str(value["boundary_carrier_scope"]),
            source_atom_orders=tuple(
                int(order) for order in value["source_atom_orders"]
            ),
            materialization_policy=str(value["materialization_policy"]),
            flatten_subtree_root_node_id=(
                int(value["flatten_subtree_root_node_id"])
                if value["flatten_subtree_root_node_id"] is not None
                else None
            ),
        )
        _validate_owner_scope_break_witness(
            scope_break,
            source=source,
            source_proof=source_proof,
            headings=headings,
            findings=findings,
        )
        scope_breaks_list.append(scope_break)
    scope_breaks = tuple(scope_breaks_list)
    return _StructureProofIndex(
        headings,
        frames,
        scope_breaks,
        _flattened_node_targets(scope_breaks, headings=headings),
    )


def _flattened_node_targets(
    scope_breaks: tuple[_ProofOwnerScopeBreak, ...],
    *,
    headings: Mapping[int, _ProofHeading],
) -> dict[int, int]:
    """Map every flattened subtree node to the target that absorbs it."""

    children: dict[int, list[int]] = {}
    for node_id, heading in headings.items():
        if heading.parent_node_id is not None:
            children.setdefault(heading.parent_node_id, []).append(node_id)
    targets: dict[int, int] = {}
    for scope_break in scope_breaks:
        if scope_break.materialization_policy != "flatten_intervening_subtree":
            continue
        root = scope_break.flatten_subtree_root_node_id
        target = scope_break.target_node_id
        if root is None or target is None or root not in headings:
            # The structure-contract validation already reports this shape.
            continue
        stack = [root]
        seen = {root}
        while stack:
            node = stack.pop()
            targets[node] = target
            for child in children.get(node, []):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
    return targets


def _validate_owner_scope_break_witness(
    scope_break: _ProofOwnerScopeBreak,
    *,
    source: _SourceIndex,
    source_proof: SourceEvidenceProof,
    headings: Mapping[int, _ProofHeading],
    findings: list[AuditFinding],
) -> None:
    page = next(
        (item for item in source_proof.pages if item.page_idx == scope_break.page_index),
        None,
    )
    selected = (
        _mapped_selector_events(
            page,
            source_item_index=scope_break.boundary_source_item_index,
            field=scope_break.boundary_field,
            index=scope_break.boundary_index,
        )
        if page is not None
        else ()
    )
    source_value = _proof_source_text(
        source.elements[scope_break.boundary_ref],
        field=scope_break.boundary_field,
        index=scope_break.boundary_index,
    )
    spans = sorted(event.selector_char_span for event in selected)
    expected_span_end = len(comparison_text(source_value))
    exact_spans = bool(
        spans
        and spans[0][0] == 0
        and spans[-1][1] == expected_span_end
        and all(left[1] == right[0] for left, right in zip(spans, spans[1:]))
    )
    if (
        page is None
        or tuple(event.word_order for event in selected)
        != scope_break.source_atom_orders
        or any(event.source_item_index != scope_break.boundary_source_item_index for event in selected)
        or not exact_spans
        or not _events_are_closed_native_lines(page, selected)
    ):
        _audit_error(
            findings,
            "owner_scope_break_source_witness_invalid",
            "owner scope break atoms do not close over one exact native selector",
            source_ref=scope_break.boundary_ref,
        )
        return

    if scope_break.eligibility_basis == "numbered_caption_native_break":
        caption_boxes = {event.carrier_bbox for event in selected}
        body = _mapped_selector_events(
            page,
            source_item_index=scope_break.boundary_source_item_index,
            field="table_html",
            index=None,
        )
        body_boxes = {event.carrier_bbox for event in body}
        geometry_valid = len(caption_boxes) == len(body_boxes) == 1
        if geometry_valid:
            caption_box = next(iter(caption_boxes))
            body_box = next(iter(body_boxes))
            geometry_valid = not _positive_bbox_overlap(caption_box, body_box)
            if scope_break.boundary_carrier_scope == "selected_and_same_carrier":
                geometry_valid = geometry_valid and caption_box[3] <= body_box[1]
            else:
                successor = _mapped_selector_events(
                    page,
                    source_item_index=(
                        scope_break.boundary_source_item_index + 1
                    ),
                    field="table_html",
                    index=None,
                )
                successor_boxes = {event.carrier_bbox for event in successor}
                geometry_valid = bool(
                    geometry_valid
                    and body_box[3] <= caption_box[1]
                    and len(successor_boxes) == 1
                    and caption_box[3] <= next(iter(successor_boxes))[1]
                )
        geometry_valid = geometry_valid and _numbered_break_layout_valid(
            selected,
            page=page,
            source_proof=source_proof,
        )
        if not geometry_valid:
            _audit_error(
                findings,
                "owner_scope_break_geometry_invalid",
                "numbered owner break scope differs from exact native geometry",
                source_ref=scope_break.boundary_ref,
            )
    else:
        owner = headings.get(scope_break.current_owner_node_id)
        owner_events = tuple(
            event
            for ref in (() if owner is None else owner.source_refs)
            for event in _mapped_selector_events(
                next(
                    (
                        item
                        for item in source_proof.pages
                        if item.page_idx
                        == int(source.elements[ref.ref].get("page_idx", -1))
                    ),
                    None,
                ),
                source_item_index=int(
                    source.elements[ref.ref]["source_item_index"]
                ),
                field=ref.field,
                index=ref.index,
            )
        )
        if not _same_native_display_family(
            selected,
            owner_events,
            source_proof=source_proof,
        ):
            _audit_error(
                findings,
                "owner_scope_break_layout_invalid",
                "unnumbered owner break does not repeat its accepted native style",
                source_ref=scope_break.boundary_ref,
            )


def _mapped_selector_events(
    page: object,
    *,
    source_item_index: int,
    field: str,
    index: int | None,
) -> tuple[MappedSourceEvent, ...]:
    events = getattr(page, "events", ())
    return tuple(
        sorted(
            (
                event
                for event in events
                if isinstance(event, MappedSourceEvent)
                and event.source_item_index == source_item_index
                and event.selector_field == field
                and event.selector_index == index
            ),
            key=lambda event: event.word_order,
        )
    )


def _events_are_closed_native_lines(
    page: object,
    selected: tuple[MappedSourceEvent, ...],
) -> bool:
    if not selected:
        return False
    selected_lines = {event.native_layout_path[:3] for event in selected}
    all_events = getattr(page, "events", ())
    selected_orders = {event.word_order for event in selected}
    complete_orders = {
        event.word_order
        for event in all_events
        if isinstance(event, (MappedSourceEvent, NativeTextEvent))
        and (
            event.native_layout_path[:3]
            if isinstance(event, MappedSourceEvent)
            else event.layout_path[:3]
        )
        in selected_lines
    }
    return selected_orders == complete_orders


def _positive_bbox_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return min(left[2], right[2]) > max(left[0], right[0]) and min(
        left[3], right[3]
    ) > max(left[1], right[1])


def _native_line_metrics(
    events: Iterable[MappedSourceEvent],
) -> tuple[tuple[tuple[float, float, float, float], float], ...]:
    by_line: dict[tuple[int, int, int], list[MappedSourceEvent]] = defaultdict(list)
    for event in events:
        by_line[event.native_layout_path[:3]].append(event)
    return tuple(
        (
            (
                min(event.atom_bbox[0] for event in line),
                min(event.atom_bbox[1] for event in line),
                max(event.atom_bbox[2] for event in line),
                max(event.atom_bbox[3] for event in line),
            ),
            statistics.median(
                event.atom_bbox[3] - event.atom_bbox[1] for event in line
            ),
        )
        for line in by_line.values()
    )


def _page_line_heights(page: object) -> tuple[float, ...]:
    by_line: dict[tuple[int, int, int], list[tuple[float, float, float, float]]] = (
        defaultdict(list)
    )
    for event in getattr(page, "events", ()):
        if isinstance(event, MappedSourceEvent):
            by_line[event.native_layout_path[:3]].append(event.atom_bbox)
        elif isinstance(event, NativeTextEvent):
            by_line[event.layout_path[:3]].append(event.bbox)
    return tuple(
        statistics.median(box[3] - box[1] for box in boxes)
        for boxes in by_line.values()
    )


def _numbered_break_layout_valid(
    events: tuple[MappedSourceEvent, ...],
    *,
    page: object,
    source_proof: SourceEvidenceProof,
) -> bool:
    metrics = _native_line_metrics(events)
    if not metrics:
        return False
    all_lefts = sorted(
        box[0]
        for proof_page in source_proof.pages
        for box, _height in _native_line_metrics(
            tuple(
                event
                for event in proof_page.events
                if isinstance(event, MappedSourceEvent)
            )
        )
    )
    document_left = all_lefts[int((len(all_lefts) - 1) * 0.05)] if all_lefts else 0
    near_left = metrics[0][0][0] <= document_left + 5 * metrics[0][1]
    page_width = getattr(page, "width", None)
    centered = isinstance(page_width, float) and all(
        abs(box[0] - (page_width - box[2])) <= 2 * height
        for box, height in metrics
    )
    heights = _page_line_heights(page)
    modal = (
        Counter(round(height, 2) for height in heights).most_common(1)[0][0]
        if heights
        else 0
    )
    display = all(round(height, 2) > modal for _box, height in metrics)
    return near_left or (centered and display)


def _same_native_display_family(
    candidate: tuple[MappedSourceEvent, ...],
    owner: tuple[MappedSourceEvent, ...],
    *,
    source_proof: SourceEvidenceProof,
) -> bool:
    candidate_metrics = _native_line_metrics(candidate)
    owner_metrics = _native_line_metrics(owner)
    if not candidate_metrics or len(candidate_metrics) != len(owner_metrics):
        return False
    candidate_page = next(
        (page for page in source_proof.pages if candidate[0] in page.events),
        None,
    )
    owner_page = next(
        (page for page in source_proof.pages if owner and owner[0] in page.events),
        None,
    )
    if candidate_page is None or owner_page is None:
        return False

    def display_line(
        metrics: tuple[tuple[tuple[float, float, float, float], float], ...],
        page: object,
        *,
        require_display_height: bool,
    ) -> bool:
        width = getattr(page, "width", None)
        height = getattr(page, "height", None)
        if not isinstance(width, float) or not isinstance(height, float):
            return False
        centered = all(
            abs(box[0] - (width - box[2])) <= 2 * line_height
            for box, line_height in metrics
        )
        page_front = max(box[3] for box, _line_height in metrics) <= height * 0.38
        if not require_display_height:
            return centered and page_front
        line_heights = _page_line_heights(page)
        modal = (
            Counter(round(value, 2) for value in line_heights).most_common(1)[0][0]
            if line_heights
            else 0
        )
        return centered and page_front and all(
            round(value, 2) > modal for _box, value in metrics
        )

    return bool(
        _events_are_closed_native_lines(candidate_page, candidate)
        and _events_are_closed_native_lines(owner_page, owner)
        and display_line(candidate_metrics, candidate_page, require_display_height=False)
        and display_line(owner_metrics, owner_page, require_display_height=True)
        and [round(value, 2) for _box, value in candidate_metrics]
        == [round(value, 2) for _box, value in owner_metrics]
        and len({event.native_layout_path[:2] for event in candidate})
        == len({event.native_layout_path[:2] for event in owner})
    )


def _validate_structure_projections(
    source: _SourceIndex,
    *,
    structure: _StructureProofIndex,
    state: _CoverageState,
    findings: list[AuditFinding],
) -> None:
    """Validate unit ancestry solely against the parser's closed proof."""

    paths = {
        node_id: _proof_heading_path(heading, headings=structure.headings)
        for node_id, heading in structure.headings.items()
    }

    def proved_path(
        refs: frozenset[str],
        *,
        carrier_id: str,
        unit_order: int,
    ) -> tuple[_ProofHeading, ...] | None:
        if not refs or all(
            source.elements[ref].get("kind") == "page_furniture" for ref in refs
        ):
            return ()
        return _proved_exact_path(
            refs,
            source=source,
            structure=structure,
            paths=paths,
            state=state,
            carrier_id=carrier_id,
            findings=findings,
            unit_order=unit_order,
        )

    for occurrence in state.carrier_occurrences:
        if not occurrence.payload_refs and occurrence.container_semantic_type is None:
            continue
        if occurrence.payload_refs and any(
            "_native_source_ref" in source.elements[ref]
            for ref in occurrence.payload_refs
        ):
            # Native recoveries carry transitive anchor attribution and are
            # validated by the native-gap path, not the carrier proof replay.
            continue
        semantic_type = occurrence.container_semantic_type
        if semantic_type is None:
            expected = proved_path(
                occurrence.payload_refs,
                carrier_id=occurrence.carrier_id,
                unit_order=occurrence.unit_order,
            )
            if expected is None:
                continue
        else:
            contributions: list[tuple[_ProofHeading, ...]] = []
            for part_refs, carrier_id in occurrence.container_parts:
                if not part_refs:
                    continue
                if all(
                    "_native_source_ref" in source.elements[ref]
                    for ref in part_refs
                ):
                    # Native recovery leaves inherit the already verified
                    # coarse owner. They are not new structure-proof claims.
                    continue
                part_path = proved_path(
                    part_refs,
                    carrier_id=carrier_id,
                    unit_order=occurrence.unit_order,
                )
                if part_path is None:
                    continue
                if all(
                    source.elements[ref].get("kind") == "page_furniture"
                    for ref in part_refs
                ):
                    continue
                deepest = part_path[-1] if part_path else None
                if deepest is not None and _payload_projection_matches_heading(
                    state,
                    carrier_id=carrier_id,
                    heading=deepest,
                ):
                    contributions.append(part_path[:-1])
                else:
                    contributions.append(part_path)
            expected = contributions[0] if contributions else ()
            if any(
                tuple(item.node_id for item in contribution)
                != tuple(item.node_id for item in expected)
                for contribution in contributions[1:]
            ):
                _audit_error(
                    findings,
                    "mixed_part_structural_scope_invalid",
                    "mixed parts do not contribute to one exact section occurrence",
                    unit_order=occurrence.unit_order,
                )
        expected_titles = tuple(heading.title for heading in expected)
        projected = tuple(
            sorted(occurrence.headings, key=lambda item: item.target_index)
        )
        path_matches = occurrence.heading_path == expected_titles and tuple(
            item.target_index for item in projected
        ) == tuple(range(len(expected)))
        if not path_matches:
            _audit_error(
                findings,
                "structure_proof_path_mismatch",
                (
                    "public heading_path and its section identity must equal "
                    "the explicit structure-proof ancestry"
                ),
                unit_order=occurrence.unit_order,
            )
        else:
            for actual, heading in zip(projected, expected, strict=True):
                if actual.kind not in {"source_field", "source_concat"} or not (
                    _heading_projection_matches_proof(actual, heading=heading)
                ):
                    _audit_error(
                        findings,
                        "structure_proof_source_mismatch",
                        (
                            "heading projection selectors must equal the proved "
                            "heading source refs and text spans"
                        ),
                        source_ref=(
                            actual.selectors[0].ref if actual.selectors else None
                        ),
                        unit_order=occurrence.unit_order,
                    )
        if semantic_type is not None:
            if (semantic_type == "section") != bool(expected):
                _audit_error(
                    findings,
                    "mixed_container_semantic_scope_invalid",
                    (
                        "mixed semantic_type must agree with its proved "
                        "section occurrence"
                    ),
                    unit_order=occurrence.unit_order,
                )


def _proof_heading_path(
    heading: _ProofHeading,
    *,
    headings: Mapping[int, _ProofHeading],
) -> tuple[_ProofHeading, ...]:
    path = [heading]
    parent_id = heading.parent_node_id
    while parent_id is not None:
        parent = headings[parent_id]
        path.append(parent)
        parent_id = parent.parent_node_id
    return tuple(reversed(path))


def _proved_exact_path(
    payload_refs: Iterable[str],
    *,
    source: _SourceIndex,
    structure: _StructureProofIndex,
    paths: Mapping[int, tuple[_ProofHeading, ...]],
    state: _CoverageState,
    carrier_id: str,
    findings: list[AuditFinding],
    unit_order: int,
) -> tuple[_ProofHeading, ...] | None:
    carrier_paths: list[tuple[_ProofHeading, ...]] = []
    for ref in payload_refs:
        source_index = source.elements[ref].get("source_item_index")
        if not isinstance(source_index, int) or isinstance(source_index, bool):
            continue
        if source_index in structure.frame_source_indices:
            carrier_paths.append(())
            continue
        candidates = [
            heading
            for heading in structure.headings.values()
            if heading.propagates
            and heading.section_start <= source_index <= heading.section_end
        ]
        if not candidates:
            carrier_paths.append(())
            continue
        candidates.sort(key=lambda item: len(paths[item.node_id]))
        owner = candidates[-1]
        owner_path = paths[owner.node_id]
        owner_ids = {item.node_id for item in owner_path}
        if any(item.node_id not in owner_ids for item in candidates):
            _audit_error(
                findings,
                "structure_proof_section_ambiguous",
                "one payload carrier belongs to overlapping proof sections",
                source_ref=ref,
                unit_order=unit_order,
            )
            carrier_paths.append(())
            continue
        applicable = [
            scope_break
            for scope_break in structure.owner_scope_breaks
            if scope_break.current_owner_node_id == owner.node_id
            and owner.section_start
            < scope_break.boundary_source_item_index
            <= source_index
        ]
        if applicable:
            latest = max(
                applicable,
                key=lambda item: item.boundary_source_item_index,
            )
            if source_index != latest.boundary_source_item_index or (
                latest.boundary_carrier_scope == "selected_and_same_carrier"
                or _carrier_selects_owner_boundary(
                    state,
                    carrier_id=carrier_id,
                    scope_break=latest,
                    findings=findings,
                    unit_order=unit_order,
                )
            ):
                owner_path = (
                    paths[latest.target_node_id]
                    if latest.target_node_id is not None
                    else ()
                )
        if owner_path and structure.flattened_node_targets:
            absorbed_target = next(
                (
                    structure.flattened_node_targets[item.node_id]
                    for item in owner_path
                    if item.node_id in structure.flattened_node_targets
                ),
                None,
            )
            if absorbed_target is not None:
                owner_path = paths[absorbed_target]
        carrier_paths.append(owner_path)
    if not carrier_paths:
        return ()
    expected = carrier_paths[0]
    expected_ids = tuple(heading.node_id for heading in expected)
    if any(
        tuple(heading.node_id for heading in path) != expected_ids
        for path in carrier_paths[1:]
    ):
        _audit_error(
            findings,
            "structure_proof_section_mixed",
            "one payload occurrence mixes different proved section owners",
            unit_order=unit_order,
        )
        return None
    return expected


def _carrier_selects_owner_boundary(
    state: _CoverageState,
    *,
    carrier_id: str,
    scope_break: _ProofOwnerScopeBreak,
    findings: list[AuditFinding],
    unit_order: int,
) -> bool:
    selectors = [
        selector
        for projection in state.payload_projections
        if projection.carrier_id == carrier_id
        for selector in projection.selectors
        if selector.ref == scope_break.boundary_ref
    ]
    selected = [
        selector
        for selector in selectors
        if selector.kind == scope_break.boundary_field
        and selector.field.get("index") == scope_break.boundary_index
        and tuple(selector.field.get("char_span", ()))
        == scope_break.boundary_text_span
        and selector.field.get("value_sha256")
        == scope_break.boundary_value_sha256
    ]
    if selected and len(selectors) != len(selected):
        _audit_error(
            findings,
            "owner_scope_break_selector_mixed",
            "selected-only boundary carrier also owns old-scope representations",
            source_ref=scope_break.boundary_ref,
            unit_order=unit_order,
        )
    return len(selected) == 1 and len(selectors) == 1


def _heading_projection_matches_proof(
    projection: _HeadingProjection,
    *,
    heading: _ProofHeading,
) -> bool:
    if len(projection.selectors) != len(heading.source_refs):
        return False
    for selector, expected in zip(
        projection.selectors,
        heading.source_refs,
        strict=True,
    ):
        span = selector.field.get("char_span")
        expected_fields = {"kind", "char_span"}
        if expected.index is not None:
            expected_fields.add("index")
        if (
            selector.ref != expected.ref
            or selector.kind != expected.field
            or set(selector.field) != expected_fields
            or selector.field.get("index") != expected.index
            or not isinstance(span, list)
            or tuple(span) != expected.text_span
        ):
            return False
    return True


def _payload_projection_matches_heading(
    state: _CoverageState,
    *,
    carrier_id: str,
    heading: _ProofHeading,
) -> bool:
    candidates = [
        projection
        for projection in state.payload_projections
        if projection.carrier_id == carrier_id
    ]
    if len(candidates) != 1:
        return False
    projection = candidates[0]
    if projection.target_field != "payload.text":
        return False
    expected_shape = (
        ("text_identity", "clean_text.v1")
        if len(heading.source_refs) == 1
        else ("text_concat", "ordered_text_concat.v1")
    )
    if (projection.kind, projection.transform) != expected_shape:
        return False
    return _heading_projection_matches_proof(
        _HeadingProjection(
            target_index=0,
            kind="source_concat",
            selectors=projection.selectors,
        ),
        heading=heading,
    )


def _proof_source_text(
    element: Mapping[str, Any],
    *,
    field: str,
    index: int | None,
) -> str:
    value = element[field]
    if index is None:
        assert isinstance(value, str)
        return value
    assert isinstance(value, list)
    selected = value[index]
    assert isinstance(selected, str)
    return selected


_QUALITY_RANK = {"ok": 0, "needs_review": 1, "unusable": 2}


def _validate_quality_lower_bounds(
    units: list[AuditUnitView],
    *,
    source: _SourceIndex,
    state: _CoverageState,
    findings: list[AuditFinding],
) -> None:
    for unit in units:
        required = "ok"
        text = _quality_text(unit.payload_kind, unit.payload)
        compact = re.sub(r"\s+", "", text)
        bad = sum(
            1
            for char in text
            if char == "\ufffd"
            or (unicodedata.category(char).startswith("C") and char not in "\n\t\r")
        )
        source_requires_review = any(
            _source_requires_review(source.elements[ref])
            for ref in state.refs_by_unit.get(unit.order_index, set())
        ) or _has_review_reason(unit.artifact_locator)
        if not compact and _payload_has_visual_bytes(
            unit.payload_kind,
            unit.payload,
        ):
            required = "needs_review"
        elif not compact or (
            text and bad / len(text) > content_annotations.GIBBERISH_RATIO_MAX
        ):
            required = "unusable"
        elif source_requires_review:
            required = "needs_review"
        if _QUALITY_RANK.get(unit.quality_status, -1) < _QUALITY_RANK[required]:
            _audit_error(
                findings,
                "quality_status_understated",
                (
                    f"source evidence requires at least {required}, "
                    f"got {unit.quality_status}"
                ),
                unit_order=unit.order_index,
            )
    checked_parts: set[tuple[str, str]] = set()
    for ref, claims in state.selector_claims.items():
        if not _source_requires_review(source.elements[ref]):
            continue
        for claim in claims:
            identity = (ref, claim.carrier_id)
            if (
                claim.role != "payload"
                or "/part:" not in claim.carrier_id
                or identity in checked_parts
            ):
                continue
            checked_parts.add(identity)
            actual = state.carrier_quality.get(claim.carrier_id)
            if _QUALITY_RANK.get(actual or "", -1) < _QUALITY_RANK["needs_review"]:
                _audit_error(
                    findings,
                    "mixed_part_quality_status_understated",
                    (
                        "mixed source part requires at least needs_review, "
                        f"got {actual!r}"
                    ),
                    source_ref=ref,
                    unit_order=claim.unit_order,
                )


def _source_requires_review(element: Mapping[str, Any]) -> bool:
    kind = str(element.get("kind") or "")
    raw_kind = str(element.get("raw_kind") or "")
    if raw_kind in {
        SOURCE_EVIDENCE_GEOMETRY_ISSUE_KIND,
        SOURCE_EVIDENCE_VISUAL_PAGE_KIND,
    } or isinstance(element.get("_required_visual_artifact"), Mapping):
        return True
    if kind in {"unknown", "page_furniture", "image"}:
        return True
    if kind == "equation" and str(element.get("image_path") or "").strip():
        return True
    if raw_kind == "list" and "list_items" not in element:
        return True
    if raw_kind == "code" and not all(
        field in element for field in ("code_body", "code_caption", "code_footnote")
    ):
        return True
    if kind != "table":
        return False
    if (
        is_visual_only_table_element(element)
        and str(element.get("image_path") or "").strip()
    ):
        return True
    table = element.get("table")
    grid = table if isinstance(table, Mapping) else {}
    has_grid = bool(grid.get("headers") or grid.get("rows"))
    return bool(
        (not has_grid and str(element.get("table_html") or "").strip()) or not has_grid
    )


def _has_review_reason(locator: Mapping[str, Any] | None) -> bool:
    if not isinstance(locator, Mapping):
        return False
    if isinstance(locator.get("review_reason"), str) and locator["review_reason"]:
        return True
    return any(
        _has_review_reason(value)
        for value in locator.values()
        if isinstance(value, Mapping)
    ) or any(
        _has_review_reason(value)
        for value in locator.values()
        if isinstance(value, list)
        for value in value
        if isinstance(value, Mapping)
    )


def _quality_text(kind: str, payload: Mapping[str, Any]) -> str:
    if kind == "mixed":
        parts = payload.get("parts")
        if not isinstance(parts, list):
            return ""
        return " ".join(
            _quality_text(
                "text"
                if str(part.get("kind") or "text") == "image"
                else str(part.get("kind") or "text"),
                part,
            )
            for part in parts
            if isinstance(part, Mapping)
        )
    if kind == "table":
        return " ".join(
            [
                *_string_list(payload.get("caption")),
                str(payload.get("unit") or ""),
                *_string_list(payload.get("headers")),
                *[
                    str(value)
                    for row in payload.get("rows") or []
                    if isinstance(row, list)
                    for value in row
                ],
                *_string_list(payload.get("notes")),
            ]
        )
    if _payload_image_ref(dict(payload)) is not None:
        return " ".join(
            [
                *_string_list(payload.get("caption")),
                *_string_list(payload.get("content")),
                *_string_list(payload.get("notes")),
                *_string_list(payload.get("context")),
            ]
        )
    if "text" in payload:
        return str(payload.get("text") or "")
    return " ".join(str(value) for value in payload.values() if value)


def _payload_has_visual_bytes(
    kind: str,
    payload: Mapping[str, Any],
) -> bool:
    if kind == "mixed":
        parts = payload.get("parts")
        return isinstance(parts, list) and any(
            isinstance(part, Mapping)
            and _payload_has_visual_bytes(
                str(part.get("kind") or "text"),
                part,
            )
            for part in parts
        )
    return _payload_image_ref(dict(payload)) is not None


def _validate_payload_field_closure(
    *,
    payload_kind: str,
    payload: Mapping[str, Any],
    unit: AuditUnitView,
    findings: list[AuditFinding],
    mixed_part: bool = False,
) -> None:
    """Reject output fields that have no place in the public carrier schema."""

    allowed = {
        "text": _TEXT_PAYLOAD_FIELDS,
        "table": _TABLE_PAYLOAD_FIELDS,
        "mixed": _MIXED_PAYLOAD_FIELDS,
    }.get(payload_kind)
    if allowed is None:
        _projection_finding(
            findings,
            unit=unit,
            code="payload_kind_invalid",
            message=f"unsupported payload kind {payload_kind!r}",
        )
        return
    if mixed_part:
        allowed = allowed | _MIXED_PART_FIELDS
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        _projection_finding(
            findings,
            unit=unit,
            code="payload_field_unproven",
            message=(
                "payload carries fields outside its closed source projection "
                "schema: " + ", ".join(unexpected)
            ),
        )


def _collect_evidence_artifacts(
    locator: Mapping[str, Any],
    *,
    findings: list[AuditFinding],
    unit: AuditUnitView,
) -> tuple[tuple[str, dict[str, Any]], ...]:
    if "evidence_artifacts" not in locator:
        return ()
    raw = locator["evidence_artifacts"]
    if not isinstance(raw, list):
        _projection_finding(
            findings,
            unit=unit,
            code="evidence_artifacts_invalid",
            message="evidence_artifacts must be an array of closed image descriptors",
        )
        return ()
    artifacts: list[tuple[str, dict[str, Any]]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            _projection_finding(
                findings,
                unit=unit,
                code="evidence_artifacts_invalid",
                message="each evidence artifact must be an object",
            )
            continue
        descriptor = dict(item)
        role = descriptor.get("artifact_role")
        fields = frozenset(descriptor)
        positive_fields = (
            ("size_bytes",)
            if fields == _ARTIFACT_FIELDS
            else ("size_bytes", "pixel_width", "pixel_height")
            if fields == _VISUAL_ARTIFACT_FIELDS
            else ()
        )
        if (
            not isinstance(role, str)
            or _ARTIFACT_ROLE_RE.fullmatch(role) is None
            or _SHA256_RE.fullmatch(str(descriptor.get("sha256") or "")) is None
            or descriptor.get("media_type") not in _IMAGE_MEDIA_TYPES
            or not positive_fields
            or any(
                isinstance(descriptor.get(field), bool)
                or not isinstance(descriptor.get(field), int)
                or int(descriptor[field]) < 1
                for field in positive_fields
            )
        ):
            _projection_finding(
                findings,
                unit=unit,
                code="evidence_artifact_descriptor_invalid",
                message="evidence artifact descriptor is not closed over verified bytes",
            )
            if not isinstance(role, str):
                continue
        artifacts.append((role, descriptor))
    duplicate_roles = {
        role
        for role, count in Counter(role for role, _ in artifacts).items()
        if count > 1
    }
    if duplicate_roles:
        _projection_finding(
            findings,
            unit=unit,
            code="evidence_artifact_role_duplicate",
            message="one carrier repeats an evidence artifact role",
        )
    return tuple(artifacts)


def _collect_projection_graph(
    locator: dict[str, Any],
    *,
    payload_kind: str,
    payload: Mapping[str, Any],
    heading_path: list[str],
    applicability: str | None,
    carrier_id: str,
    source: _SourceIndex,
    state: _CoverageState,
    findings: list[AuditFinding],
    unit: AuditUnitView,
    local_roles: dict[str, set[str]],
) -> tuple[bool, set[str]]:
    artifacts = _collect_evidence_artifacts(
        locator,
        findings=findings,
        unit=unit,
    )
    unexpected_locator_fields = sorted(set(locator) - PUBLIC_ARTIFACT_LOCATOR_FIELDS)
    if unexpected_locator_fields:
        _projection_finding(
            findings,
            unit=unit,
            code="artifact_locator_field_unproven",
            message=(
                "artifact_locator carries derivable or unsupported fields: "
                + ", ".join(unexpected_locator_fields)
            ),
        )
    raw = locator.get("source_projection")
    if raw is None:
        return False, set()
    if not isinstance(raw, dict):
        _projection_finding(
            findings,
            unit=unit,
            code="source_projection_invalid",
            message="source_projection must be an object",
        )
        return True, set()
    expected_keys = {
        "version",
        "payload",
        "heading_path",
        "structured",
        "provenance",
        "search_targets",
        "search_atoms",
        "physical_context",
    }
    if (
        set(raw) != expected_keys
        or raw.get("version") != UNIT_SOURCE_PROJECTION_VERSION
    ):
        _projection_finding(
            findings,
            unit=unit,
            code="source_projection_contract_invalid",
            message="source_projection has an unsupported version or open field set",
        )
        return True, set()

    refs: set[str] = set()
    projected_headings: list[_HeadingProjection] = []
    payload_projection = raw.get("payload")
    if payload_projection is not None:
        payload_refs = _collect_payload_projection(
            payload_projection,
            payload=payload,
            carrier_id=carrier_id,
            source=source,
            state=state,
            findings=findings,
            unit=unit,
        )
        refs.update(payload_refs)
        state.payload_refs.update(payload_refs)
        local_roles["payload"].update(payload_refs)

    heading_entries = raw.get("heading_path")
    if not isinstance(heading_entries, list):
        _projection_finding(
            findings,
            unit=unit,
            code="heading_projection_invalid",
            message="heading_path projection must be an array",
        )
    else:
        seen_targets: set[int] = set()
        for entry in heading_entries:
            if not isinstance(entry, dict):
                _projection_finding(
                    findings,
                    unit=unit,
                    code="heading_projection_invalid",
                    message="heading projection entries must be objects",
                )
                continue
            target = entry.get("target_index")
            if (
                not isinstance(target, int)
                or isinstance(target, bool)
                or target < 0
                or target >= len(heading_path)
                or target in seen_targets
            ):
                _projection_finding(
                    findings,
                    unit=unit,
                    code="heading_source_path_mismatch",
                    message="heading projection target indices must be unique and in range",
                )
                continue
            seen_targets.add(target)
            kind = entry.get("kind")
            if kind not in HEADING_PROJECTION_KINDS:
                _projection_finding(
                    findings,
                    unit=unit,
                    code="heading_projection_invalid",
                    message="heading projection kind is unsupported",
                )
                continue
            raw_selectors: list[dict[str, Any]]
            if kind == "source_field" and isinstance(entry.get("selector"), dict):
                raw_selectors = [entry["selector"]]
            elif kind == "source_concat" and isinstance(entry.get("sources"), list):
                raw_selectors = [
                    value for value in entry["sources"] if isinstance(value, dict)
                ]
                if not raw_selectors or len(raw_selectors) != len(entry["sources"]):
                    _projection_finding(
                        findings,
                        unit=unit,
                        code="heading_projection_invalid",
                        message="source_concat requires a non-empty typed selector array",
                    )
                    continue
            else:
                _projection_finding(
                    findings,
                    unit=unit,
                    code="heading_projection_invalid",
                    message="heading projection requires a typed source selector",
                )
                continue
            selected_values: list[_ResolvedSelector] = []
            for raw_selector in raw_selectors:
                selected = _resolve_source_selector(
                    raw_selector,
                    role="structure",
                    carrier_id=carrier_id,
                    source=source,
                    findings=findings,
                    unit=unit,
                )
                if selected is None:
                    continue
                selected_values.append(selected)
                refs.add(selected.ref)
                state.structure_refs.add(selected.ref)
                state.selector_claims[selected.ref].append(selected)
                local_roles["structure"].add(selected.ref)
            selected_text = [str(value.value).strip() for value in selected_values]
            transform = entry.get("transform")
            if kind == "source_concat" and transform == "exact_concat.v1":
                expected_heading = "".join(selected_text)
            elif kind == "source_concat" and transform in {
                "ordered_text_concat.v1",
                "ordered_visible_fields.v1",
            }:
                expected_heading = "\n".join(selected_text)
            elif kind == "source_field" and transform == "clean_text.v1":
                expected_heading = selected_text[0] if len(selected_text) == 1 else ""
            elif kind == "source_concat" and transform == "clean_text.v1":
                # A merged printed title joins its line carriers verbatim
                # and strips only the outer whitespace, exactly like the
                # single-field clean_text lane.
                raw_values = [str(value.value) for value in selected_values]
                expected_heading = "".join(raw_values).strip()
            else:
                expected_heading = ""
            if selected_values:
                projected_headings.append(
                    _HeadingProjection(
                        target_index=target,
                        kind=str(kind),
                        selectors=tuple(selected_values),
                    )
                )
            if len(selected_values) == len(raw_selectors) and (
                not expected_heading or expected_heading != heading_path[target]
            ):
                _projection_finding(
                    findings,
                    unit=unit,
                    code="heading_projection_mismatch",
                    message="selected source heading differs from the public path segment",
                    source_ref=(selected_values[0].ref if selected_values else None),
                )
        if seen_targets != set(range(len(heading_path))):
            _projection_finding(
                findings,
                unit=unit,
                code="heading_source_path_mismatch",
                message="every public heading_path segment requires one ordered projection",
            )

    structured_entries = raw.get("structured")
    applicability_edges = 0
    if not isinstance(structured_entries, list):
        _projection_finding(
            findings,
            unit=unit,
            code="structured_projection_invalid",
            message="structured projection must be an array",
        )
    else:
        for entry in structured_entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("source"), dict):
                _projection_finding(
                    findings,
                    unit=unit,
                    code="structured_projection_invalid",
                    message="structured projection requires a typed source selector",
                )
                continue
            selected = _resolve_source_selector(
                entry["source"],
                role="structured",
                carrier_id=carrier_id,
                source=source,
                findings=findings,
                unit=unit,
            )
            if selected is None:
                continue
            refs.add(selected.ref)
            state.structured_refs.add(selected.ref)
            state.selector_claims[selected.ref].append(selected)
            local_roles["structured"].add(selected.ref)
            target_field = entry.get("target_field")
            actual = (
                applicability
                if target_field == "applicability"
                else projection_target_value(payload, target_field)
            )
            if entry.get("kind") == "applicability_marker":
                applicability_edges += 1
                expected = _independent_marker_value(str(selected.value))
                if expected is None or actual != expected:
                    _projection_finding(
                        findings,
                        unit=unit,
                        code="structured_projection_mismatch",
                        message="applicability marker does not reproduce its target",
                        source_ref=selected.ref,
                    )
                else:
                    state.validated_structured_refs.add(selected.ref)
            elif entry.get("kind") == "derived_field":
                transform = entry.get("transform")
                if transform in {"identity.v1", "identity_json.v1"}:
                    matches = actual == selected.value
                elif transform == "trim.v1":
                    matches = (
                        isinstance(selected.value, str)
                        and actual == selected.value.strip()
                    )
                elif transform == "ordered_nonempty_lines.v1":
                    values = (
                        selected.value
                        if isinstance(selected.value, list)
                        else [selected.value]
                    )
                    matches = actual == "\n".join(
                        str(value).strip() for value in values if str(value).strip()
                    )
                elif transform == "unit_declaration.v2":
                    matches = actual == _projection_unit_declaration(
                        str(selected.value)
                    )
                else:
                    matches = False
                if not matches:
                    _projection_finding(
                        findings,
                        unit=unit,
                        code="structured_projection_mismatch",
                        message="derived field is not anchored in its selected source value",
                        source_ref=selected.ref,
                    )
                else:
                    state.validated_structured_refs.add(selected.ref)
            else:
                _projection_finding(
                    findings,
                    unit=unit,
                    code="structured_projection_invalid",
                    message="unsupported structured projection kind",
                    source_ref=selected.ref,
                )
    if applicability is not None and applicability_edges < 1:
        _projection_finding(
            findings,
            unit=unit,
            code="applicability_projection_missing",
            message=(
                "non-null applicability requires at least one typed source projection"
            ),
        )

    provenance_entries = raw.get("provenance")
    if provenance_entries != []:
        _projection_finding(
            findings,
            unit=unit,
            code="provenance_projection_invalid",
            message="fresh source projections do not support provenance aliases",
        )
    primary_required = requires_primary_search_leaf(
        payload_kind=payload_kind,
        payload=payload,
    )
    if (
        payload_kind == "text"
        and payload.get("representation_role")
        == "unresolved_source_alternative"
        and payload.get("search_policy") == "none"
    ):
        state.non_primary_source_alternative_count += 1
    if primary_required:
        state.required_search_carriers.add(carrier_id)
    try:
        search_values = search_text_values(
            payload_kind=payload_kind,
            payload=payload,
            artifact_locator=locator,
        )
    except SearchTargetContractError as exc:
        _projection_finding(
            findings,
            unit=unit,
            code="search_target_contract_invalid",
            message=str(exc),
        )
        if primary_required:
            _projection_finding(
                findings,
                unit=unit,
                code="primary_search_leaf_missing",
                message="reader-visible carrier has no closed primary search leaf",
            )
    else:
        state.primary_search_leaf_count += len(search_values)
        if primary_required:
            if search_values:
                state.closed_search_carriers.add(carrier_id)
            else:
                _projection_finding(
                    findings,
                    unit=unit,
                    code="primary_search_leaf_missing",
                    message=(
                        "reader-visible carrier has no closed primary search leaf"
                    ),
                )
        if search_values:
            for ref in sorted(local_roles["payload"]):
                state.active_search_ref_counts[ref] += 1
                if state.active_search_ref_counts[ref] == 2:
                    state.duplicate_active_primary_count += 1
                    _projection_finding(
                        findings,
                        unit=unit,
                        code="duplicate_active_primary_search_projection",
                        message=(
                            "one payload source ref feeds more than one "
                            "active primary search leaf"
                        ),
                    )
                element = source.elements.get(ref)
                if (
                    isinstance(element, Mapping)
                    and element.get("kind") == "page_furniture"
                ):
                    state.page_furniture_active_search_count += 1
                    _projection_finding(
                        findings,
                        unit=unit,
                        code="page_furniture_active_search",
                        message=(
                            "an unproved page-furniture carrier is active "
                            "primary search content"
                        ),
                    )
            state.searchable_payload_refs.update(local_roles["payload"])
    state.carrier_occurrences.append(
        _CarrierOccurrence(
            unit_order=unit.order_index,
            carrier_id=carrier_id,
            payload_refs=frozenset(local_roles["payload"]),
            heading_path=tuple(heading_path),
            headings=tuple(projected_headings),
            artifacts=artifacts,
        )
    )
    return True, refs


def _collect_payload_projection(
    raw: object,
    *,
    payload: Mapping[str, Any],
    carrier_id: str,
    source: _SourceIndex,
    state: _CoverageState,
    findings: list[AuditFinding],
    unit: AuditUnitView,
) -> set[str]:
    if not isinstance(raw, dict):
        _projection_finding(
            findings,
            unit=unit,
            code="payload_projection_invalid",
            message="payload projection must be an object",
        )
        return set()
    kind = raw.get("kind")
    if kind not in PAYLOAD_PROJECTION_KINDS:
        _projection_finding(
            findings,
            unit=unit,
            code="payload_projection_invalid",
            message="unsupported payload projection kind",
        )
        return set()
    expected_fields = {"kind", "sources", "target_field", "transform"}
    if set(raw) != expected_fields:
        _projection_finding(
            findings,
            unit=unit,
            code="payload_projection_contract_invalid",
            message="payload projection has an open or legacy field set",
        )
    sources = raw.get("sources")
    if not isinstance(sources, list) or (not sources and kind != "container"):
        _projection_finding(
            findings,
            unit=unit,
            code="payload_projection_sources_invalid",
            message="payload projection requires an explicit source array",
        )
        return set()
    selectors: list[_ResolvedSelector] = []
    for selector in sources:
        selected = _resolve_source_selector(
            selector,
            role="payload",
            carrier_id=carrier_id,
            source=source,
            findings=findings,
            unit=unit,
        )
        if selected is None:
            continue
        selectors.append(selected)
        state.selector_claims[selected.ref].append(selected)
    target_field = raw.get("target_field")
    if not isinstance(target_field, str):
        _projection_finding(
            findings,
            unit=unit,
            code="payload_projection_target_invalid",
            message="payload projection target_field must be a string",
        )
        return {selector.ref for selector in selectors}
    projection = _ResolvedPayloadProjection(
        kind=str(kind),
        transform=str(raw.get("transform") or ""),
        target_field=target_field,
        target_value=projection_target_value(payload, target_field),
        selectors=tuple(selectors),
        unit_order=unit.order_index,
        carrier_id=carrier_id,
    )
    state.payload_projections.append(projection)
    _validate_payload_projection_value(projection, findings=findings)
    return {selector.ref for selector in selectors}


def _resolve_source_selector(
    raw: object,
    *,
    role: str,
    carrier_id: str,
    source: _SourceIndex,
    findings: list[AuditFinding],
    unit: AuditUnitView,
) -> _ResolvedSelector | None:
    if not isinstance(raw, dict) or set(raw) != {"source", "field"}:
        _projection_finding(
            findings,
            unit=unit,
            code="source_selector_invalid",
            message="source selector must contain only source and field",
        )
        return None
    ref = _resolve_strict_source_ref(
        raw.get("source"), source=source, findings=findings, unit=unit
    )
    field_value = raw.get("field")
    if ref is None or not isinstance(field_value, dict):
        return None
    field_selector = dict(field_value)
    kind = field_selector.get("kind")
    if kind not in SOURCE_FIELD_KINDS:
        _projection_finding(
            findings,
            unit=unit,
            code="source_selector_field_invalid",
            message="source selector field kind is unsupported",
            source_ref=ref,
        )
        return None
    common = {"kind", "value_sha256"}
    allowed_by_kind = {
        "text": common | {"char_span"},
        "table": common,
        "table_caption": common | {"index", "char_span"},
        "table_note": common | {"index", "char_span"},
        "image": common,
        "image_caption": common | {"index", "char_span"},
        "image_footnote": common | {"index", "char_span"},
        "visual_subtype": common,
        "visual_semantic_text": common,
        "list_items": common,
        "list_subtype": common,
        "code_body": common,
        "code_caption": common,
        "code_footnote": common,
        "code_subtype": common,
        "text_format": common,
    }
    if not set(field_selector).issubset(allowed_by_kind[str(kind)]):
        _projection_finding(
            findings,
            unit=unit,
            code="source_selector_field_invalid",
            message="source selector field has unsupported keys",
            source_ref=ref,
        )
        return None
    try:
        value = _select_source_value(source.elements[ref], field_selector)
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        _projection_finding(
            findings,
            unit=unit,
            code="source_selector_range_invalid",
            message=f"source selector cannot be replayed: {exc}",
            source_ref=ref,
        )
        return None
    digest = field_selector.get("value_sha256")
    if digest is not None and (
        not isinstance(digest, str) or digest != source_value_sha256(value)
    ):
        _projection_finding(
            findings,
            unit=unit,
            code="source_selector_hash_mismatch",
            message="selected source value differs from its bound digest",
            source_ref=ref,
        )
    return _ResolvedSelector(
        ref=ref,
        kind=str(kind),
        value=value,
        field=field_selector,
        role=role,
        unit_order=unit.order_index,
        carrier_id=carrier_id,
    )


def _resolve_strict_source_ref(
    raw: object,
    *,
    source: _SourceIndex,
    findings: list[AuditFinding],
    unit: AuditUnitView,
) -> str | None:
    if not isinstance(raw, dict):
        _projection_finding(
            findings,
            unit=unit,
            code="source_ref_invalid",
            message="source reference must be an object",
        )
        return None
    canonical = source_ref_from_locator(raw)
    identity = source_ref_identity(canonical) if canonical is not None else None
    if canonical is None or identity is None or raw != canonical:
        _projection_finding(
            findings,
            unit=unit,
            code="source_ref_invalid",
            message="source reference is not one closed v3 physical identity",
        )
        return None
    ref = source.by_identity.get(identity)
    if ref is None:
        _projection_finding(
            findings,
            unit=unit,
            code="source_ref_identity_invalid",
            message="source identity does not resolve to one physical carrier",
        )
        return None
    element = source.elements[ref]
    native_ref = element.get("_native_source_ref")
    if native_ref is not None:
        if native_ref != canonical:
            _projection_finding(
                findings,
                unit=unit,
                code="source_ref_provenance_mismatch",
                message="source-native reference differs from the validated ledger",
                source_ref=ref,
            )
            return None
        return ref
    for source_field in {"page_no", "bbox"} & set(canonical):
        if canonical[source_field] != element.get(source_field):
            _projection_finding(
                findings,
                unit=unit,
                code="source_ref_provenance_mismatch",
                message=(
                    f"source reference field {source_field} differs from NormalizedIR"
                ),
                source_ref=ref,
            )
            return None
    return ref


def _select_source_value(element: dict[str, Any], field: dict[str, Any]) -> Any:
    kind = str(field["kind"])
    value: Any
    if kind == "text":
        value = _element_text(element)
    elif kind == "table":
        value = element.get("table")
    elif kind == "table_caption":
        value = _string_list(element.get("table_caption"))[_selector_index(field)]
    elif kind == "table_note":
        value = _string_list(element.get("table_footnote"))[_selector_index(field)]
    elif kind == "image":
        value = str(element.get("image_path") or "")
    elif kind == "image_caption":
        captions = _string_list(element.get("image_caption") or element.get("caption"))
        value = (
            captions[_selector_index(field)]
            if "index" in field
            else _image_caption(element)
        )
    elif kind == "image_footnote":
        notes = _string_list(element.get("image_footnote"))
        value = notes[_selector_index(field)]
    elif kind == "visual_subtype":
        value = str(element.get("visual_subtype") or "")
    elif kind == "visual_semantic_text":
        value = str(element.get("visual_semantic_text") or "")
    elif kind == "list_items":
        value = _string_list(element.get("list_items"))
    elif kind == "list_subtype":
        value = str(element.get("list_subtype") or "")
    elif kind == "code_body":
        value = str(element.get("code_body") or "")
    elif kind == "code_caption":
        value = _string_list(element.get("code_caption"))
    elif kind == "code_footnote":
        value = _string_list(element.get("code_footnote"))
    elif kind == "code_subtype":
        value = str(element.get("code_subtype") or "")
    elif kind == "text_format":
        value = str(element.get("text_format") or "")
    else:
        raise ValueError(f"unsupported selector kind {kind}")
    span = field.get("char_span")
    if span is not None:
        if not isinstance(value, str) or not isinstance(span, list) or len(span) != 2:
            raise TypeError("char_span requires a string field and [start,end]")
        start = _selector_exact_int(span[0])
        end = _selector_exact_int(span[1])
        if start < 0 or end <= start or end > len(value):
            raise IndexError("char_span is outside the selected source value")
        value = value[start:end]
    return value


def _selector_index(field: dict[str, Any]) -> int:
    return _selector_nonnegative_int(field, "index")


def _selector_nonnegative_int(field: dict[str, Any], key: str) -> int:
    value = _selector_exact_int(field.get(key))
    if value < 0:
        raise ValueError(f"{key} must be non-negative")
    return value


def _selector_exact_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("selector index must be an integer")
    return value


def _validate_payload_projection_value(
    projection: _ResolvedPayloadProjection,
    *,
    findings: list[AuditFinding],
) -> None:
    if projection.kind == "container":
        if (
            projection.transform != "ordered_parts.v1"
            or projection.target_field != "payload.parts"
            or projection.selectors
            or not isinstance(projection.target_value, list)
        ):
            _audit_error(
                findings,
                "payload_projection_mismatch",
                (
                    "container projection must be the closed ordered parts "
                    "transform without duplicated source selectors"
                ),
                unit_order=projection.unit_order,
            )
        return
    if projection.target_value is None:
        _audit_error(
            findings,
            "payload_projection_target_missing",
            f"projection target {projection.target_field!r} is absent",
            unit_order=projection.unit_order,
        )
        return
    if projection.kind in {
        "text_identity",
        "text_concat",
    }:
        safe = projection.transform.startswith("safe_")
        transform = (
            projection.transform.removeprefix("safe_")
            if safe
            else projection.transform
        )
        values = [str(item.value).strip() for item in projection.selectors]
        if projection.kind == "text_concat" and transform in {
            "ordered_text_concat.v1",
            "ordered_visible_fields.v1",
        }:
            expected = "\n".join(values)
        elif (
            projection.kind == "text_concat"
            and transform == "exact_concat.v1"
        ):
            expected = "".join(values)
        elif len(values) == 1 and transform in {
            "clean_text.v1",
            "ordered_visible_fields.v1",
        }:
            expected = values[0]
        else:
            expected = None
        if safe and expected is not None:
            expected = semantic_payload_without_unsafe_glyphs(expected)
        if expected is None or expected != projection.target_value:
            _audit_error(
                findings,
                "payload_projection_mismatch",
                ("selected source text does not exactly replay its declared transform"),
                source_ref=(
                    projection.selectors[0].ref if projection.selectors else None
                ),
                unit_order=projection.unit_order,
            )
    elif projection.kind == "text_identity_exact":
        selected = "".join(str(item.value) for item in projection.selectors)
        expected = (
            semantic_payload_without_unsafe_glyphs(selected)
            if projection.transform == "safe_identity.v1"
            else selected
        )
        if (
            projection.transform not in {"identity.v1", "safe_identity.v1"}
            or expected != projection.target_value
        ):
            _audit_error(
                findings,
                "payload_projection_mismatch",
                "exact source text differs from its payload target",
                source_ref=(
                    projection.selectors[0].ref if projection.selectors else None
                ),
                unit_order=projection.unit_order,
            )
    elif projection.kind == "table_identity":
        if not _table_projection_matches(projection):
            _audit_error(
                findings,
                "table_projection_mismatch",
                "selected table fields differ from the table payload",
                source_ref=(
                    projection.selectors[0].ref if projection.selectors else None
                ),
                unit_order=projection.unit_order,
            )
    elif projection.kind == "image_identity":
        valid = (
            projection.transform == "sha256_bytes.v1"
            and projection.target_field == "payload.image_ref"
            and len(projection.selectors) == 1
            and projection.selectors[0].kind == "image"
            and isinstance(projection.target_value, str)
            and _digest_from_path(projection.target_value) is not None
        )
        if not valid:
            _audit_error(
                findings,
                "image_projection_mismatch",
                (
                    "image projection must bind one source image to one "
                    "content-addressed payload.image_ref"
                ),
                source_ref=(
                    projection.selectors[0].ref if projection.selectors else None
                ),
                unit_order=projection.unit_order,
            )


def _projection_norm(value: Any) -> str:
    return comparison_text(str(value))


def _table_projection_matches(projection: _ResolvedPayloadProjection) -> bool:
    if not isinstance(projection.target_value, Mapping):
        return False
    payload = projection.target_value
    grids = [selector for selector in projection.selectors if selector.kind == "table"]
    if len(grids) != 1 or not isinstance(grids[0].value, dict):
        return False
    table = grids[0].value
    if projection.transform not in {
        "table_identity.v1",
        "safe_table_identity.v1",
    }:
        return False
    expected: dict[str, Any] = {
        "caption": [
            str(selector.value)
            for selector in projection.selectors
            if selector.kind == "table_caption"
        ],
        "headers": [str(value) for value in table.get("headers") or []],
        "rows": [[str(value) for value in row] for row in table.get("rows") or []],
        "merged_cells": [dict(value) for value in table.get("merged_cells") or []],
        "notes": [
            str(selector.value)
            for selector in projection.selectors
            if selector.kind == "table_note"
        ],
    }
    if "cells" in table:
        expected["cells"] = [dict(value) for value in table.get("cells") or []]
    if projection.transform == "safe_table_identity.v1":
        safe_expected = semantic_payload_without_unsafe_glyphs(expected)
        if not isinstance(safe_expected, dict):
            return False
        expected = safe_expected
    supported = {"table", "table_caption", "table_note"}
    if any(selector.kind not in supported for selector in projection.selectors):
        return False
    if not all(
        payload.get(key, [] if value == [] else None) == value
        for key, value in expected.items()
    ):
        return False
    return _table_media_projection_matches(
        table.get("embedded_media"),
        payload.get("embedded_media"),
        safe=projection.transform == "safe_table_identity.v1",
    )


def _table_media_projection_matches(
    source: object,
    target: object,
    *,
    safe: bool = False,
) -> bool:
    if source is None:
        return target is None
    if (
        not isinstance(source, list)
        or not isinstance(target, list)
        or len(source) != len(target)
    ):
        return False
    for raw_source, raw_target in zip(source, target, strict=True):
        if not isinstance(raw_source, Mapping) or not isinstance(raw_target, Mapping):
            return False
        expected = {
            key: value
            for key, value in raw_source.items()
            if key not in {"artifact_role", "image_path"}
        }
        if safe:
            safe_expected = semantic_payload_without_unsafe_glyphs(expected)
            if not isinstance(safe_expected, dict):
                return False
            expected = safe_expected
        if (
            set(raw_target) != {*expected, "image_ref"}
            or any(raw_target.get(key) != value for key, value in expected.items())
            or not isinstance(raw_target.get("image_ref"), str)
            or _digest_from_path(str(raw_target["image_ref"])) is None
        ):
            return False
    return True


def _projection_finding(
    findings: list[AuditFinding],
    *,
    unit: AuditUnitView,
    code: str,
    message: str,
    source_ref: str | None = None,
) -> None:
    _audit_error(
        findings,
        code,
        message,
        source_ref=source_ref,
        unit_order=unit.order_index,
    )


def _validate_projection_partitions(
    source: _SourceIndex,
    *,
    state: _CoverageState,
    findings: list[AuditFinding],
) -> None:
    for ref, claims in state.selector_claims.items():
        element = source.elements[ref]
        if element.get("kind") == "table":
            _validate_table_selector_partition(
                ref,
                element=element,
                claims=claims,
                findings=findings,
            )
        elif element.get("kind") in _TEXT_KINDS:
            _validate_text_selector_partition(
                ref,
                text=_element_text(element),
                claims=claims,
                findings=findings,
            )


def _validate_text_selector_partition(
    ref: str,
    *,
    text: str,
    claims: list[_ResolvedSelector],
    findings: list[AuditFinding],
) -> None:
    content_claims: list[_ResolvedSelector] = []
    seen_structure_fields: list[dict[str, Any]] = []
    for claim in claims:
        if claim.role not in {"payload", "structure"} or claim.kind != "text":
            continue
        # One heading source is intentionally projected onto every descendant
        # breadcrumb. Repeating the same structure edge is navigation reuse,
        # not overlapping content ownership.
        if claim.role == "structure":
            if claim.field in seen_structure_fields:
                continue
            seen_structure_fields.append(claim.field)
        content_claims.append(claim)
    if not content_claims or not text:
        return
    payload_claims = [claim for claim in content_claims if claim.role == "payload"]
    payload_whole = [
        claim for claim in payload_claims if "char_span" not in claim.field
    ]
    payload_sliced = [claim for claim in payload_claims if "char_span" in claim.field]
    if len(payload_whole) > 1 or (payload_whole and payload_sliced):
        _audit_error(
            findings,
            "text_selector_overlap",
            "one text carrier has overlapping whole/sliced payload claims",
            source_ref=ref,
        )
        return
    if any("char_span" not in claim.field for claim in content_claims):
        return
    coverage = [0] * len(text)
    payload_coverage = [0] * len(text)
    for claim in content_claims:
        span = claim.field["char_span"]
        for index in range(int(span[0]), int(span[1])):
            coverage[index] = 1
            if claim.role == "payload":
                payload_coverage[index] += 1
    relevant = [index for index, char in enumerate(text) if not char.isspace()]
    if any(coverage[index] == 0 for index in relevant):
        _audit_error(
            findings,
            "text_selector_gap",
            "text payload slices leave non-whitespace source content uncovered",
            source_ref=ref,
        )
    if any(payload_coverage[index] > 1 for index in relevant):
        _audit_error(
            findings,
            "text_selector_overlap",
            "text payload slices overlap source content",
            source_ref=ref,
        )


def _validate_table_selector_partition(
    ref: str,
    *,
    element: dict[str, Any],
    claims: list[_ResolvedSelector],
    findings: list[AuditFinding],
) -> None:
    captions = _string_list(element.get("table_caption"))
    notes = _string_list(element.get("table_footnote"))
    payload_claims = [claim for claim in claims if claim.role == "payload"]
    allowed = {"table", "table_caption", "table_note"}
    if any(claim.kind not in allowed for claim in payload_claims):
        _audit_error(
            findings,
            "table_selector_contract_invalid",
            "table payload ownership must use the current whole-table contract",
            source_ref=ref,
        )
        return

    whole_count = sum(claim.kind == "table" for claim in payload_claims)
    caption_counts = [
        sum(
            claim.kind == "table_caption" and claim.field.get("index") == index
            for claim in payload_claims
        )
        for index in range(len(captions))
    ]
    note_counts = [
        sum(
            claim.kind == "table_note" and claim.field.get("index") == index
            for claim in payload_claims
        )
        for index in range(len(notes))
    ]
    counts = [whole_count, *caption_counts, *note_counts]
    if any(count == 0 for count in counts):
        _audit_error(
            findings,
            "table_selector_gap",
            "whole-table ownership omits a typed table field",
            source_ref=ref,
        )
    if any(count > 1 for count in counts):
        _audit_error(
            findings,
            "table_selector_overlap",
            "whole-table ownership claims a typed table field more than once",
            source_ref=ref,
        )


def _expected_visual_kind(element: Mapping[str, Any]) -> str:
    """Derive the typed visual classification from the source element."""

    if element.get("raw_kind") == "chart":
        return "chart"
    if element.get("kind") == "equation":
        return "equation"
    return "image"


def _validate_visual_payload_fields(
    element: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    ref: str,
    findings: list[AuditFinding],
) -> None:
    """Fail closed when a rebuilt image payload drops or mistypes source facts."""

    if payload.get("visual_kind") != _expected_visual_kind(element):
        _audit_error(
            findings,
            "visual_kind_mismatch",
            "payload visual_kind differs from the source element type",
            source_ref=ref,
        )
    if _optional_visual_subtype(
        element.get("visual_subtype")
    ) != _optional_visual_subtype(payload.get("visual_subtype")):
        _audit_error(
            findings,
            "visual_subtype_mismatch",
            "payload visual_subtype differs from the source element",
            source_ref=ref,
        )
    image_path = str(element.get("image_path") or "").strip()
    if not image_path:
        expected_text = "\n".join(
            value
            for value in [
                *_string_list(element.get("image_caption")),
                _element_text(dict(element)),
                *_string_list(element.get("image_footnote")),
            ]
            if value.strip()
        )
        if _projection_norm(expected_text) != _projection_norm(payload.get("text")):
            _audit_error(
                findings,
                "visual_text_mismatch",
                "text-only visual payload differs from its typed fields",
                source_ref=ref,
            )
        return

    caption_field = (
        "table_caption" if is_visual_only_table_element(element) else "image_caption"
    )
    note_field = (
        "table_footnote" if is_visual_only_table_element(element) else "image_footnote"
    )
    source_caption = _projection_norm("".join(_string_list(element.get(caption_field))))
    if source_caption and source_caption not in _projection_norm(
        payload.get("caption")
    ):
        _audit_error(
            findings,
            "image_caption_dropped",
            "source image_caption is absent from the payload caption",
            source_ref=ref,
        )
    source_notes = _projection_norm("".join(_string_list(element.get(note_field))))
    if source_notes and source_notes not in _projection_norm(
        "".join(_string_list(payload.get("notes")))
    ):
        _audit_error(
            findings,
            "image_notes_dropped",
            "source image_footnote is absent from the payload notes",
            source_ref=ref,
        )
    source_content = _projection_norm(_element_text(dict(element)))
    if source_content and source_content not in _projection_norm(
        "".join(
            [
                *_string_list(payload.get("content")),
                *_string_list(payload.get("caption")),
            ]
        )
    ):
        _audit_error(
            findings,
            "image_content_dropped",
            "source image text is absent from payload content or caption",
            source_ref=ref,
        )
    if element.get("visual_semantic_text") != payload.get("semantic_text"):
        _audit_error(
            findings,
            "visual_semantic_text_mismatch",
            "payload visual semantic text differs from its exact source field",
            source_ref=ref,
        )


def _optional_visual_subtype(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _validate_images(
    source: _SourceIndex,
    *,
    state: _CoverageState,
    image_hashes: dict[str, str],
    findings: list[AuditFinding],
) -> None:
    for ref, element in source.elements.items():
        if element.get("kind") not in {"image", "equation"}:
            continue
        image_path = str(element.get("image_path") or "").strip()
        if not image_path and element.get("kind") == "equation":
            continue
        occurrences = state.image_payloads.get(ref, [])
        if len(occurrences) != 1:
            _audit_error(
                findings,
                "image_payload_count_invalid",
                "source image resolves to "
                f"{len(occurrences)} image payload occurrences",
                source_ref=ref,
            )
            continue
        _, payload = occurrences[0]
        _validate_visual_payload_fields(element, payload, ref=ref, findings=findings)
        if not image_path:
            continue
        # Image-backed carriers must publish one content-addressed reference;
        # text-only visual fallbacks returned above after their typed fields
        # were independently checked.
        image_ref = _payload_image_ref(payload)
        if image_ref is None:
            _audit_error(
                findings,
                "image_ref_missing",
                "image-backed source has no content-addressed image_ref",
                source_ref=ref,
            )
            continue
        expected = image_hashes.get(ref) or _digest_from_path(image_path)
        actual = _digest_from_path(image_ref)
        if expected is None:
            _audit_error(
                findings,
                "image_hash_unavailable",
                "non-addressed source image was not hashed by the caller",
                source_ref=ref,
            )
        elif actual != expected.removeprefix("sha256:"):
            _audit_error(
                findings,
                "image_hash_mismatch",
                "image_ref digest differs from source image bytes",
                source_ref=ref,
            )


def _validate_table_media(
    source: _SourceIndex,
    *,
    state: _CoverageState,
    image_hashes: Mapping[str, str],
    findings: list[AuditFinding],
) -> None:
    artifacts_by_carrier = {
        carrier.carrier_id: carrier.artifacts for carrier in state.carrier_occurrences
    }
    for ref, element in source.elements.items():
        if element.get("kind") != "table":
            continue
        table = element.get("table")
        if not isinstance(table, Mapping):
            continue
        raw_media = table.get("embedded_media")
        if raw_media is None:
            continue
        occurrences = state.table_payloads.get(ref, [])
        if len(occurrences) != 1:
            _audit_error(
                findings,
                "table_payload_count_invalid",
                "source table resolves to "
                f"{len(occurrences)} table payload occurrences",
                source_ref=ref,
            )
            continue
        carrier_id, payload = occurrences[0]
        public_media = payload.get("embedded_media")
        if (
            not isinstance(raw_media, list)
            or not isinstance(public_media, list)
            or len(raw_media) != len(public_media)
        ):
            continue
        expected_refs: dict[str, str | None] = {}
        source_index = element.get("source_item_index")
        image_path = element.get("image_path")
        if (
            isinstance(source_index, int)
            and not isinstance(source_index, bool)
            and isinstance(image_path, str)
            and image_path
        ):
            expected_refs[f"evidence_image_{source_index:06d}"] = None
        for raw_source, raw_public in zip(
            raw_media,
            public_media,
            strict=True,
        ):
            assert isinstance(raw_source, Mapping)
            assert isinstance(raw_public, Mapping)
            role = raw_source.get("artifact_role")
            image_ref = raw_public.get("image_ref")
            if not isinstance(role, str) or not isinstance(image_ref, str):
                continue
            expected_refs[role] = image_ref

        artifacts = artifacts_by_carrier.get(carrier_id, ())
        by_role = {role: descriptor for role, descriptor in artifacts}
        duplicate_roles = {
            role
            for role, count in Counter(role for role, _ in artifacts).items()
            if count > 1
        }
        for role, image_ref in expected_refs.items():
            descriptor = by_role.get(role)
            if (
                descriptor is None
                or role in duplicate_roles
                or set(descriptor)
                != {"artifact_role", "sha256", "size_bytes", "media_type"}
                or descriptor.get("artifact_role") != role
                or not isinstance(descriptor.get("sha256"), str)
                or _SHA256_RE.fullmatch(str(descriptor["sha256"])) is None
                or isinstance(descriptor.get("size_bytes"), bool)
                or not isinstance(descriptor.get("size_bytes"), int)
                or int(descriptor["size_bytes"]) < 1
                or descriptor.get("media_type") not in _IMAGE_MEDIA_TYPES
            ):
                _audit_error(
                    findings,
                    "table_media_artifact_invalid",
                    f"table evidence artifact is invalid: {role}",
                    source_ref=ref,
                )
                continue
            verified = image_hashes.get(role)
            descriptor_hash = str(descriptor["sha256"]).removeprefix("sha256:")
            if verified is None or verified.removeprefix("sha256:") != descriptor_hash:
                _audit_error(
                    findings,
                    "table_media_hash_unavailable",
                    f"table evidence bytes were not verified: {role}",
                    source_ref=ref,
                )
            if (
                image_ref is not None
                and _digest_from_path(image_ref) != descriptor_hash
            ):
                _audit_error(
                    findings,
                    "table_media_hash_mismatch",
                    "table media image_ref differs from source bytes",
                    source_ref=ref,
                )
        prefix = (
            f"evidence_table_media_{source_index:06d}_"
            if isinstance(source_index, int) and not isinstance(source_index, bool)
            else ""
        )
        extras = {
            role
            for role in by_role
            if prefix and role.startswith(prefix) and role not in expected_refs
        }
        if extras:
            _audit_error(
                findings,
                "table_media_artifact_extra",
                "table unit binds unowned media artifact roles",
                source_ref=ref,
            )


def _validate_output_role_closure(
    *,
    source: _SourceIndex,
    state: _CoverageState,
    external_refs: set[str],
    empty_refs: set[str],
    findings: list[AuditFinding],
) -> None:
    emitted_refs = state.payload_refs | state.structure_refs | state.structured_refs
    for ref in sorted(external_refs & emitted_refs, key=source_order(source)):
        _audit_error(
            findings,
            "external_source_emitted",
            "fully externalized source atom is still emitted",
            source_ref=ref,
        )
    for ref in sorted(empty_refs & emitted_refs, key=source_order(source)):
        _audit_error(
            findings,
            "empty_source_emitted",
            "proven-empty source atom is still emitted",
            source_ref=ref,
        )
    for unit_order, refs in sorted(state.refs_by_unit.items()):
        if not refs:
            _audit_error(
                findings,
                "output_source_closure_missing",
                "output carrier has no payload source edge",
                unit_order=unit_order,
            )


def _validate_unit_source_order(
    units: list[AuditUnitView],
    *,
    source: _SourceIndex,
    state: _CoverageState,
    findings: list[AuditFinding],
) -> None:
    previous: tuple[int, set[str]] | None = None
    for unit in sorted(units, key=lambda item: item.order_index):
        refs = state.refs_by_unit.get(unit.order_index, set())
        ordered_refs = [
            (ref, value)
            for ref in refs
            if isinstance((value := source.elements[ref].get("order_index")), int)
        ]
        orders = [value for _, value in ordered_refs]
        if not orders:
            continue
        current_min = min(orders)
        current_max = max(orders)
        if previous is not None and refs != previous[1] and current_min < previous[0]:
            current_ref = min(ordered_refs, key=lambda item: item[1])[0]
            _audit_error(
                findings,
                "unit_source_order_invalid",
                "payload sources move backwards in document order: "
                f"previous_max={previous[0]} "
                f"current_range={current_min}:{current_max}",
                source_ref=current_ref,
                unit_order=unit.order_index,
            )
        previous = (
            max(previous[0], current_max) if previous is not None else current_max,
            set(refs),
        )


def _validate_units(
    units: list[AuditUnitView],
    *,
    document_title: str | None,
    findings: list[AuditFinding],
) -> None:
    if not units:
        _audit_error(
            findings,
            "empty_unit_output",
            "builder returned no document units",
        )
    expected_orders = list(range(1, len(units) + 1))
    actual_orders = [unit.order_index for unit in units]
    if actual_orders != expected_orders:
        _audit_error(
            findings,
            "unit_order_invalid",
            "unit order_index values must be unique, contiguous, and ordered",
        )
    for unit in units:
        unsafe = [
            *(
                unsafe_semantic_characters(unit.title)
                if isinstance(unit.title, str)
                else ()
            ),
            *(
                char
                for segment in unit.heading_path
                for char in unsafe_semantic_characters(segment)
            ),
            *_unsafe_payload_characters(unit.payload),
        ]
        if unsafe:
            _audit_error(
                findings,
                "unsafe_semantic_glyph_published",
                "private-use, replacement, control, or display-placeholder glyph leaked into semantic output",
                unit_order=unit.order_index,
            )
        try:
            validate_semantic_key_state(unit.semantic_key, unit.semantic_keys)
        except SemanticKeyInvariantError as exc:
            _audit_error(
                findings,
                "semantic_key_invalid",
                f"{exc.reason_code}: {exc}",
                unit_order=unit.order_index,
            )
        if any(
            not isinstance(value, str) or not value.strip()
            for value in unit.heading_path
        ):
            _audit_error(
                findings,
                "heading_path_segment_invalid",
                "heading_path contains an empty/non-string segment",
                unit_order=unit.order_index,
            )
        if unit.quality_status not in {"ok", "needs_review", "unusable"}:
            _audit_error(
                findings,
                "quality_status_invalid",
                "quality_status is outside the public enum",
                unit_order=unit.order_index,
            )
        if unit.applicability not in {None, "applicable", "not_applicable"}:
            _audit_error(
                findings,
                "applicability_invalid",
                "applicability is outside the public enum",
                unit_order=unit.order_index,
            )
        _validate_title_projection(
            unit,
            document_title=document_title,
            findings=findings,
        )


def _unsafe_payload_characters(value: object) -> list[str]:
    if isinstance(value, str):
        return list(unsafe_semantic_characters(value))
    if isinstance(value, Mapping):
        return [
            char
            for key, item in value.items()
            if key != "artifact_locator"
            for char in _unsafe_payload_characters(item)
        ]
    if isinstance(value, list):
        return [
            char
            for item in value
            for char in _unsafe_payload_characters(item)
        ]
    return []


def _validate_title_projection(
    unit: AuditUnitView,
    *,
    document_title: str | None,
    findings: list[AuditFinding],
) -> None:
    heading_title = _norm(unit.heading_path[-1]) if unit.heading_path else ""
    if unit.title is None:
        if heading_title:
            _audit_error(
                findings,
                "title_provenance_missing",
                "title must equal the deepest public heading_path segment",
                unit_order=unit.order_index,
            )
        return
    title = _norm(unit.title)
    if not title:
        _audit_error(
            findings,
            "title_invalid",
            "title must be non-empty when present",
            unit_order=unit.order_index,
        )
        return
    expected = heading_title or _norm(document_title or "")
    if not expected or title != expected:
        _audit_error(
            findings,
            "title_provenance_missing",
            "title must equal the deepest public heading_path segment or, "
            "without headings, the registered document title",
            unit_order=unit.order_index,
        )


def _is_substantive(element: dict[str, Any]) -> bool:
    kind = element.get("kind")
    if kind == "equation":
        return bool(
            _element_text(element).strip()
            or str(element.get("image_path") or "").strip()
            or _image_source_text(element)
        )
    if kind in _TEXT_KINDS:
        return bool(_element_text(element).strip())
    if kind == "table":
        table_value = element.get("table")
        table: dict[str, Any] = table_value if isinstance(table_value, dict) else {}
        return bool(str(element.get("image_path") or "").strip()) or (
            table_has_visible_text_evidence(
                table,
                captions=_string_list(element.get("table_caption")),
                notes=_string_list(element.get("table_footnote")),
                html=element.get("table_html") or "",
            )
        )
    if kind == "image":
        return bool(
            str(element.get("image_path") or "").strip() or _image_source_text(element)
        )
    return bool(_element_text(element).strip())


def _element_text(element: dict[str, Any]) -> str:
    value = element.get("text")
    return value if isinstance(value, str) else ""


def _image_caption(element: dict[str, Any]) -> str:
    for key in ("caption", "image_caption", "text"):
        values = _string_list(element.get(key))
        if values:
            return "\n".join(values)
    return ""


def _image_source_text(element: dict[str, Any]) -> str:
    values = [
        _image_caption(element),
        _element_text(element),
        *_string_list(element.get("image_footnote")),
    ]
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _norm(value)
        if not normalized or normalized in seen:
            continue
        output.append(value)
        seen.add(normalized)
    return "\n".join(output)


def _unit_primary_text(unit: AuditUnitView) -> str:
    return _payload_primary_text(unit.payload_kind, unit.payload)


def _payload_primary_text(kind: str, payload: dict[str, Any]) -> str:
    if kind == "mixed":
        parts = payload.get("parts")
        if not isinstance(parts, list):
            return ""
        return "".join(
            _payload_primary_text(str(part.get("kind") or "text"), part)
            for part in parts
            if isinstance(part, dict)
        )
    if kind == "text":
        if _payload_image_ref(payload):
            values = [
                *_string_list(payload.get("caption")),
                *_string_list(payload.get("content")),
                *_string_list(payload.get("notes")),
                *_string_list(payload.get("context")),
            ]
            return "\n".join(value for value in values if value)
        value = payload.get("text")
        if isinstance(value, str):
            return value
        return "\n".join(_string_list(payload.get("caption")))
    return ""


def _unit_visible_chars(unit: AuditUnitView) -> int:
    if unit.payload_kind == "table":
        values = [
            *_string_list(unit.payload.get("caption")),
            *_string_list(unit.payload.get("headers")),
            *[
                str(value)
                for row in unit.payload.get("rows") or []
                if isinstance(row, list)
                for value in row
            ],
            *_string_list(unit.payload.get("notes")),
        ]
        return len(_norm("".join(values)))
    return len(_norm(_unit_primary_text(unit)))


def _payload_image_ref(payload: dict[str, Any]) -> str | None:
    value = payload.get("image_ref")
    return value if isinstance(value, str) and value else None


def _digest_from_path(value: str) -> str | None:
    match = _HEX_DIGEST_RE.search(value)
    return match.group(1).lower() if match else None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _norm(value: Any) -> str:
    # Intentionally NFKC + whitespace only, and case-sensitive: unlike
    # comparison_text/_projection_norm this must NOT casefold, because its
    # inputs include case-significant values (uppercase security codes, English
    # headings/titles).  It also needs no LaTeX ``\~`` unescaping.  Do not fold
    # it into comparison_text.
    return "".join(unicodedata.normalize("NFKC", str(value)).split())


def source_order(source: _SourceIndex) -> Callable[[str], int]:
    def key(ref: str) -> int:
        value = source.elements[ref].get("order_index")
        return value if isinstance(value, int) else 2**31

    return key


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))
