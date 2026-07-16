"""Pure source-identity audit for NormalizedIR and built document units.

The audit deliberately does not use a document-wide string haystack.  Source
carriers and output units are joined only through explicit artifact locators;
otherwise one repeated heading could hide the loss of another occurrence.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Callable, Iterable, Mapping
import unicodedata

from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    ReconciliationCompatibility,
    assess_normalized_ir_table_reconciliation,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    NormalizedIRVersionError,
    validate_normalized_ir_contract,
    validate_reconciliation_generation,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    HEADING_PROJECTION_KINDS,
    PAYLOAD_PROJECTION_KINDS,
    SOURCE_FIELD_KINDS,
    TABLE_LOCATOR_FIELDS,
    UNIT_SOURCE_PROJECTION_VERSION,
    source_value_sha256,
)
from disclosure_anchor.domain.value_objects.semantic_key import (
    SemanticKeyInvariantError,
    validate_semantic_key_state,
)


_HEX_DIGEST_RE = re.compile(r"(?i)(?:^|/)([0-9a-f]{64})(?:\.[a-z0-9]+)?$")
_TEXT_KINDS = {"text", "heading", "equation", "unknown", "page_furniture"}


@dataclass(frozen=True)
class AuditDocumentMetadata:
    document_id: str
    title: str | None
    filing_type: str
    security_code: str | None = None
    security_name: str | None = None


@dataclass(frozen=True)
class AuditUnitView:
    order_index: int
    payload_kind: str
    payload: dict[str, Any]
    title: str | None
    heading_path: list[str]
    structural_path: list[str]
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


@dataclass
class _CoverageState:
    payload_refs: set[str] = field(default_factory=set)
    structure_refs: set[str] = field(default_factory=set)
    structured_refs: set[str] = field(default_factory=set)
    validated_structured_refs: set[str] = field(default_factory=set)
    refs_by_unit: dict[int, set[str]] = field(default_factory=dict)
    structure_texts: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    table_payloads: dict[str, list[tuple[str, dict[str, Any]]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    image_payloads: dict[str, list[tuple[str, dict[str, Any]]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    applicability_targets: dict[str, set[tuple[int, str | None]]] = field(
        default_factory=lambda: defaultdict(set)
    )
    exact_dedup_groups: list[frozenset[str]] = field(default_factory=list)
    provenance_refs: set[str] = field(default_factory=set)
    projected_table_refs: set[str] = field(default_factory=set)
    selector_claims: dict[str, list["_ResolvedSelector"]] = field(
        default_factory=lambda: defaultdict(list)
    )
    payload_projections: list["_ResolvedPayloadProjection"] = field(
        default_factory=list
    )


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
    target_field: str
    target_value: Any
    selectors: tuple[_ResolvedSelector, ...]
    index: int | None
    count: int | None
    source_sha256: str | None
    unit_order: int
    carrier_id: str


@dataclass
class _DispositionState:
    external_refs: set[str] = field(default_factory=set)
    partial_external_refs: set[str] = field(default_factory=set)
    text_overrides: dict[str, str] = field(default_factory=dict)
    structure_text_overrides: dict[str, str] = field(default_factory=dict)
    expected_applicability: dict[str, str] = field(default_factory=dict)


def audit_document(
    *,
    normalized_ir: dict[str, Any],
    units: Iterable[AuditUnitView],
    metadata: AuditDocumentMetadata,
    source_dispositions: Iterable[Mapping[str, Any]] = (),
    image_hashes: Mapping[str, str] | None = None,
) -> DocumentAuditReport:
    """Audit one builder replay without database or filesystem access."""

    unit_list = list(units)
    findings: list[AuditFinding] = []
    source = _build_source_index(normalized_ir, metadata=metadata, findings=findings)
    _validate_reconciliation(normalized_ir, findings=findings)
    _validate_units(unit_list, metadata=metadata, findings=findings)
    dispositions = _validate_source_dispositions(
        source_dispositions,
        source=source,
        metadata=metadata,
        findings=findings,
    )
    state = _collect_unit_coverage(
        unit_list,
        source=source,
        metadata=metadata,
        expected_applicability=dispositions.expected_applicability,
        findings=findings,
    )
    located_structured_refs = set(state.structured_refs)
    validated_applicability_refs = _validate_applicability_targets(
        expected=dispositions.expected_applicability,
        state=state,
        findings=findings,
    )
    validated_structured_refs = (
        state.validated_structured_refs | validated_applicability_refs
    )
    for ref in sorted(
        located_structured_refs - validated_structured_refs,
        key=source_order(source),
    ):
        findings.append(
            AuditFinding(
                code="structured_source_unproved",
                severity="error",
                message="structured locator has no independently proven field projection",
                source_ref=ref,
            )
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
        | state.provenance_refs
        | dispositions.external_refs
        | empty_refs
    )
    for ref in sorted(substantive_refs - covered_refs, key=source_order(source)):
        findings.append(
            AuditFinding(
                code="source_atom_uncovered",
                severity="error",
                message="non-empty source carrier has no locator-backed representation",
                source_ref=ref,
            )
        )

    _validate_structure_texts(
        source,
        state=state,
        source_text_overrides=dispositions.structure_text_overrides,
        findings=findings,
    )
    conservation = _validate_text_conservation(
        source,
        units=unit_list,
        state=state,
        excluded_refs=dispositions.external_refs,
        source_text_overrides=dispositions.text_overrides,
        findings=findings,
    )
    _validate_projection_partitions(
        source,
        state=state,
        source_text_overrides=dispositions.text_overrides,
        findings=findings,
    )
    _validate_tables(source, state=state, findings=findings)
    _validate_images(
        source,
        state=state,
        image_hashes=dict(image_hashes or {}),
        findings=findings,
    )
    _validate_output_role_closure(
        source=source,
        state=state,
        external_refs=dispositions.external_refs,
        partial_external_refs=dispositions.partial_external_refs,
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
    tiny_units = sum(
        1
        for unit in unit_list
        if 0 < _unit_visible_chars(unit) < 50
    )
    coverage_classes = {
        "payload": len(substantive_refs & state.payload_refs),
        "structure": len(
            (substantive_refs - state.payload_refs) & state.structure_refs
        ),
        "structured": len(
            (substantive_refs - state.payload_refs - state.structure_refs)
            & state.structured_refs
        ),
        "provenance": len(
            (
                substantive_refs
                - state.payload_refs
                - state.structure_refs
                - state.structured_refs
            )
            & state.provenance_refs
        ),
        "external_metadata": len(
            (
                substantive_refs
                - state.payload_refs
                - state.structure_refs
                - state.structured_refs
                - state.provenance_refs
            )
            & dispositions.external_refs
        ),
        "proven_empty": len(empty_refs),
        "uncovered": len(substantive_refs - covered_refs),
    }
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
        "text_components": conservation["components"],
        "source_text_chars": conservation["source_chars"],
        "output_text_chars": conservation["output_chars"],
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
    if normalized_ir.get("document_id") != metadata.document_id:
        findings.append(
            AuditFinding(
                code="document_id_mismatch",
                severity="error",
                message="manifest and NormalizedIR document_id differ",
            )
        )
    raw_elements = normalized_ir.get("elements")
    if not isinstance(raw_elements, list):
        findings.append(
            AuditFinding(
                code="elements_invalid",
                severity="error",
                message="NormalizedIR elements must be an array",
            )
        )
        raw_elements = []

    elements: dict[str, dict[str, Any]] = {}
    by_ir_id: dict[str, str] = {}
    by_source: dict[int, str] = {}
    by_order: dict[int, str] = {}
    previous_order: int | None = None
    for position, raw in enumerate(raw_elements):
        if not isinstance(raw, dict):
            findings.append(
                AuditFinding(
                    code="source_element_invalid",
                    severity="error",
                    message=f"element {position} is not an object",
                )
            )
            continue
        ir_id = raw.get("ir_id")
        source_index = raw.get("source_item_index")
        order_index = raw.get("order_index")
        if not isinstance(ir_id, str) or not ir_id:
            findings.append(
                AuditFinding(
                    code="source_identity_missing",
                    severity="error",
                    message=f"element {position} has no ir_id",
                )
            )
            ref = f"position:{position}"
        else:
            ref = ir_id
        if ref in elements:
            findings.append(
                AuditFinding(
                    code="source_identity_duplicate",
                    severity="error",
                    message=f"duplicate ir_id {ref!r}",
                    source_ref=ref,
                )
            )
            ref = f"{ref}#position:{position}"
        elements[ref] = raw
        if isinstance(ir_id, str) and ir_id:
            by_ir_id.setdefault(ir_id, ref)
        if isinstance(source_index, int):
            if source_index in by_source:
                findings.append(
                    AuditFinding(
                        code="source_item_index_duplicate",
                        severity="error",
                        message=f"duplicate source_item_index {source_index}",
                        source_ref=ref,
                    )
                )
            by_source.setdefault(source_index, ref)
        else:
            findings.append(
                AuditFinding(
                    code="source_item_index_missing",
                    severity="error",
                    message="source_item_index must be an integer",
                    source_ref=ref,
                )
            )
        if isinstance(order_index, int):
            if order_index in by_order:
                findings.append(
                    AuditFinding(
                        code="source_order_duplicate",
                        severity="error",
                        message=f"duplicate order_index {order_index}",
                        source_ref=ref,
                    )
                )
            by_order.setdefault(order_index, ref)
            if previous_order is not None and order_index <= previous_order:
                findings.append(
                    AuditFinding(
                        code="source_order_not_increasing",
                        severity="error",
                        message="elements are not strictly ordered by order_index",
                        source_ref=ref,
                    )
                )
            previous_order = order_index
        else:
            findings.append(
                AuditFinding(
                    code="source_order_missing",
                    severity="error",
                    message="order_index must be an integer",
                    source_ref=ref,
                )
            )
        if raw.get("document_id") not in {None, metadata.document_id}:
            findings.append(
                AuditFinding(
                    code="element_document_id_mismatch",
                    severity="error",
                    message="element document_id differs from its IR",
                    source_ref=ref,
                )
            )
        page_idx = raw.get("page_idx")
        page_no = raw.get("page_no")
        if isinstance(page_idx, int) and isinstance(page_no, int) and page_no != page_idx + 1:
            findings.append(
                AuditFinding(
                    code="page_number_mismatch",
                    severity="error",
                    message="page_no must equal page_idx + 1",
                    source_ref=ref,
                )
            )
    return _SourceIndex(
        elements=elements,
        by_ir_id=by_ir_id,
        by_source_item_index=by_source,
        by_order_index=by_order,
    )


def _validate_reconciliation(
    normalized_ir: dict[str, Any], *, findings: list[AuditFinding]
) -> None:
    try:
        version = validate_normalized_ir_contract(normalized_ir)
    except NormalizedIRVersionError as exc:
        findings.append(
            AuditFinding(
                code="normalized_ir_version_invalid",
                severity="error",
                message=f"{exc.reason_code}: {exc}",
            )
        )
        return
    try:
        assessment = assess_normalized_ir_table_reconciliation(normalized_ir)
    except ValueError as exc:
        findings.append(
            AuditFinding(
                code="table_reconciliation_invalid",
                severity="error",
                message=str(exc),
            )
        )
        return
    try:
        validate_reconciliation_generation(
            version=version,
            algorithm_version=assessment.algorithm_version,
        )
    except NormalizedIRVersionError as exc:
        findings.append(
            AuditFinding(
                code="table_reconciliation_contract_mismatch",
                severity="error",
                message=f"{exc.reason_code}: {exc}",
            )
        )
        return
    if assessment.compatibility is ReconciliationCompatibility.REPARSE_REQUIRED:
        findings.append(
            AuditFinding(
                code="table_reconciliation_reparse_required",
                severity="error",
                message="legacy table restoration changed physical carriers",
            )
        )


def _validate_source_dispositions(
    values: Iterable[Mapping[str, Any]],
    *,
    source: _SourceIndex,
    metadata: AuditDocumentMetadata,
    findings: list[AuditFinding],
) -> _DispositionState:
    state = _DispositionState()
    seen_refs: set[str] = set()
    furniture_pages: dict[str, set[int]] = defaultdict(set)
    furniture_pages_by_signature: dict[tuple[str, int], set[str]] = defaultdict(set)
    for ref, element in source.elements.items():
        if element.get("kind") != "page_furniture":
            continue
        signature = _norm(_element_text(element))
        page_no = element.get("page_no")
        if signature and isinstance(page_no, int):
            furniture_pages[signature].add(page_no)
            furniture_pages_by_signature[(signature, page_no)].add(ref)

    allowed = {
        ("external_metadata", "repeated_page_furniture"),
        ("external_metadata", "exact_page_number"),
        ("external_metadata", "registered_security_header"),
        ("partial_external_metadata", "registered_security_header"),
        ("structured_applicability", "explicit_source_marker"),
    }
    for position, raw in enumerate(values):
        proof = dict(raw)
        role = proof.get("role")
        reason = proof.get("reason")
        if not isinstance(role, str) or not isinstance(reason, str):
            findings.append(
                AuditFinding(
                    code="source_disposition_invalid",
                    severity="error",
                    message=f"disposition {position} requires role and reason",
                )
            )
            continue
        resolved_ref = _resolve_disposition(
            proof, source=source, findings=findings
        )
        if resolved_ref is None:
            continue
        ref = resolved_ref
        if ref in seen_refs:
            findings.append(
                AuditFinding(
                    code="source_disposition_duplicate",
                    severity="error",
                    message="one source atom cannot have multiple dispositions",
                    source_ref=ref,
                )
            )
            continue
        seen_refs.add(ref)
        if (role, reason) not in allowed:
            findings.append(
                AuditFinding(
                    code="source_disposition_role_invalid",
                    severity="error",
                    message=f"unsupported disposition {role}/{reason}",
                    source_ref=ref,
                )
            )
            continue
        element = source.elements[ref]
        replacement = proof.get("replacement_text")
        if replacement is not None and not isinstance(replacement, str):
            findings.append(
                AuditFinding(
                    code="source_disposition_invalid",
                    severity="error",
                    message="replacement_text must be text",
                    source_ref=ref,
                )
            )
            continue

        valid = False
        structure_residual: str | None = None
        if reason == "repeated_page_furniture":
            signature = _norm(_element_text(element))
            page_no = element.get("page_no")
            valid = (
                bool(signature)
                and len(furniture_pages.get(signature, set())) >= 2
                and (
                    element.get("kind") == "page_furniture"
                    or (
                        element.get("kind") in {"text", "heading"}
                        and isinstance(page_no, int)
                        and bool(
                            furniture_pages_by_signature.get(
                                (signature, page_no)
                            )
                        )
                    )
                )
            )
        elif reason == "exact_page_number":
            valid = _independent_page_number_metadata(element)
        elif reason == "registered_security_header":
            expected_replacement = replacement or ""
            valid = _independent_registered_header_match(
                _element_text(element),
                replacement=expected_replacement,
                metadata=metadata,
            )
        elif reason == "explicit_source_marker":
            value = proof.get("value")
            projection = _independent_applicability_projection(
                _element_text(element)
            )
            valid = (
                projection is not None
                and
                isinstance(value, str)
                and value == projection[0]
                and isinstance(replacement, str)
                and _norm(replacement) == _norm(projection[1])
            )
            if projection is not None:
                structure_residual = projection[2]
        if not valid:
            findings.append(
                AuditFinding(
                    code="source_disposition_proof_invalid",
                    severity="error",
                    message=f"source does not prove {role}/{reason}",
                    source_ref=ref,
                )
            )
            continue

        if reason == "registered_security_header" and (
            (role == "external_metadata" and bool(replacement))
            or (role == "partial_external_metadata" and not replacement)
        ):
            findings.append(
                AuditFinding(
                    code="source_disposition_role_invalid",
                    severity="error",
                    message="registered-header role disagrees with its residual text",
                    source_ref=ref,
                )
            )
            continue

        if role == "external_metadata":
            state.external_refs.add(ref)
        elif role == "partial_external_metadata":
            state.partial_external_refs.add(ref)
            state.text_overrides[ref] = replacement or ""
        else:
            assert role == "structured_applicability"
            value = proof.get("value")
            assert isinstance(value, str)
            state.expected_applicability[ref] = value
            state.text_overrides[ref] = replacement or ""
            if structure_residual is not None:
                state.structure_text_overrides[ref] = structure_residual
    return state


def _resolve_disposition(
    proof: Mapping[str, Any],
    *,
    source: _SourceIndex,
    findings: list[AuditFinding],
) -> str | None:
    candidates: set[str] = set()
    supplied = False
    ir_id = proof.get("ir_id")
    if ir_id is not None:
        supplied = True
        if not isinstance(ir_id, str) or ir_id not in source.by_ir_id:
            _unresolved_disposition(findings)
            return None
        candidates.add(source.by_ir_id[ir_id])
    source_index = proof.get("source_item_index")
    if source_index is not None:
        supplied = True
        if (
            not isinstance(source_index, int)
            or source_index not in source.by_source_item_index
        ):
            _unresolved_disposition(findings)
            return None
        candidates.add(source.by_source_item_index[source_index])
    order_index = proof.get("order_index")
    if order_index is not None:
        supplied = True
        if not isinstance(order_index, int) or order_index not in source.by_order_index:
            _unresolved_disposition(findings)
            return None
        candidates.add(source.by_order_index[order_index])
    if not supplied or len(candidates) != 1:
        findings.append(
            AuditFinding(
                code=(
                    "source_disposition_identity_conflict"
                    if len(candidates) > 1
                    else "source_disposition_identity_unresolved"
                ),
                severity="error",
                message="source disposition must resolve to exactly one source atom",
            )
        )
        return None
    return next(iter(candidates))


def _unresolved_disposition(findings: list[AuditFinding]) -> None:
    findings.append(
        AuditFinding(
            code="source_disposition_identity_unresolved",
            severity="error",
            message="a supplied source-disposition identity does not exist",
        )
    )
    return None


def _independent_page_number_metadata(element: Mapping[str, Any]) -> bool:
    if (
        element.get("kind") != "page_furniture"
        or element.get("raw_kind") != "page_number"
    ):
        return False
    page_no = element.get("page_no")
    if not isinstance(page_no, int) or page_no < 1:
        return False
    compact = _norm(_element_text(dict(element)))
    if re.fullmatch(r"(?:第)?\d{1,5}(?:页)?", compact):
        return True
    match = re.fullmatch(
        r"(?:第)?(?P<current>\d{1,5})(?:页)?[/／](?:共)?(?P<total>\d{1,5})(?:页)?",
        compact,
    )
    return bool(
        match
        and int(match.group("total")) >= int(match.group("current"))
    )


def _independent_registered_header_match(
    text: str,
    *,
    replacement: str,
    metadata: AuditDocumentMetadata,
) -> bool:
    compact = _norm(text)
    code_values = re.findall(
        r"(?:[ABH]股)?(?:证券|股票)?代码[：:](?P<value>[0-9A-Z.\-]{2,20})",
        compact,
    )
    name_values = re.findall(
        r"(?:[ABH]股)?(?:证券|股票)?简称[：:]"
        r"(?P<value>.+?)(?=(?:[ABH]股)?(?:证券|股票)?(?:代码|简称)[：:]|公告编号[：:]|$)",
        compact,
    )
    if not code_values and not name_values:
        return False
    codes = {_norm(metadata.security_code or "")}
    names = {_norm(metadata.security_name or "")}
    if metadata.title and "：" in metadata.title:
        names.add(_norm(metadata.title.split("：", 1)[0]))
    if metadata.title and ":" in metadata.title:
        names.add(_norm(metadata.title.split(":", 1)[0]))
    codes.discard("")
    names.discard("")
    if code_values and (not codes or any(_norm(value) not in codes for value in code_values)):
        return False
    if name_values and (not names or any(_norm(value) not in names for value in name_values)):
        return False
    expected_keep = ""
    announcement = re.search(r"公告编号[：:].+$", compact)
    if announcement is not None:
        expected_keep = announcement.group(0)
    return _norm(replacement) == expected_keep


def _independent_marker_value(text: str) -> str | None:
    compact = _norm(text).rstrip("。.")
    checked = "√☑✓"
    unchecked = "□☐"
    if re.fullmatch(rf"[{checked}]适用[{unchecked}]不适用", compact):
        return "applicable"
    if re.fullmatch(rf"[{unchecked}]适用[{checked}]不适用", compact):
        return "not_applicable"
    return None


def _independent_applicability_projection(
    text: str,
) -> tuple[str, str, str | None] | None:
    """Return (value, payload residual, structural residual).

    The source may be a marker line, a short label followed by its marker, or
    one physical heading with a marker glued to its tail.  Those are distinct
    projections of the same atom and must be proven without importing builder
    rules as the audit oracle.
    """

    lines = text.splitlines()
    if not lines:
        return None
    first_value = _independent_marker_value(lines[0])
    if first_value is not None:
        residual = (
            "\n".join(lines[1:]).strip()
            if first_value == "applicable"
            else text.strip()
        )
        return first_value, residual, None

    if (
        len(lines) >= 2
        and len(lines[0].strip()) <= 24
        and not lines[0].strip().endswith(("。", "；"))
        and (second_value := _independent_marker_value(lines[1])) is not None
    ):
        marker_values = {
            value
            for line in lines[1:]
            if (value := _independent_marker_value(line)) is not None
        }
        if marker_values == {second_value}:
            return second_value, text.strip(), None

    checked = "√☑✓"
    unchecked = "□☐"
    patterns = (
        ("applicable", rf"[{checked}]\s*适\s*用\s*[{unchecked}]\s*不\s*适\s*用\s*[。.]?\s*$"),
        ("not_applicable", rf"[{unchecked}]\s*适\s*用\s*[{checked}]\s*不\s*适\s*用\s*[。.]?\s*$"),
    )
    for value, pattern in patterns:
        match = re.search(pattern, text)
        if match is None or not text[: match.start()].strip():
            continue
        marker_residual = (
            "" if value == "applicable" else text[match.start() :].strip()
        )
        return value, marker_residual, text[: match.start()].rstrip()
    return None


def _validate_applicability_targets(
    *,
    expected: Mapping[str, str],
    state: _CoverageState,
    findings: list[AuditFinding],
) -> set[str]:
    valid: set[str] = set()
    for ref, expected_value in expected.items():
        targets = state.applicability_targets.get(ref, set())
        if len(targets) != 1:
            findings.append(
                AuditFinding(
                    code="applicability_target_count_invalid",
                    severity="error",
                    message=f"structured marker resolves to {len(targets)} output targets",
                    source_ref=ref,
                )
            )
            continue
        unit_order, actual = next(iter(targets))
        if actual != expected_value:
            findings.append(
                AuditFinding(
                    code="applicability_value_mismatch",
                    severity="error",
                    message=f"expected {expected_value}, got {actual}",
                    source_ref=ref,
                    unit_order=unit_order,
                )
            )
            continue
        valid.add(ref)
    return valid


def _collect_unit_coverage(
    units: list[AuditUnitView],
    *,
    source: _SourceIndex,
    metadata: AuditDocumentMetadata,
    expected_applicability: Mapping[str, str],
    findings: list[AuditFinding],
) -> _CoverageState:
    state = _CoverageState()
    for unit in units:
        refs: set[str] = set()
        role_refs: dict[str, set[str]] = defaultdict(set)
        if unit.artifact_locator is None:
            findings.append(
                AuditFinding(
                    code="unit_locator_missing",
                    severity="error",
                    message="source-derived unit has no artifact locator",
                    unit_order=unit.order_index,
                )
            )
        else:
            graph_present, refs = _collect_projection_graph(
                unit.artifact_locator,
                payload=unit.payload,
                heading_path=unit.heading_path,
                applicability=unit.applicability,
                carrier_id=f"unit:{unit.order_index}",
                source=source,
                metadata=metadata,
                state=state,
                findings=findings,
                unit=unit,
                local_roles=role_refs,
            )
            if not graph_present:
                refs = _walk_locator(
                    unit.artifact_locator,
                    role="payload",
                    source=source,
                    state=state,
                    findings=findings,
                    unit=unit,
                    local_roles=role_refs,
                )
            if not refs and unit.payload_kind != "mixed":
                findings.append(
                    AuditFinding(
                        code="unit_locator_unresolved",
                        severity="error",
                        message="unit locator has no resolvable source identity",
                        unit_order=unit.order_index,
                    )
                )
        payload_refs = role_refs["payload"]
        state.refs_by_unit[unit.order_index] = set(payload_refs)
        for ref in (payload_refs | role_refs["structured"]) & expected_applicability.keys():
            state.applicability_targets[ref].add(
                (unit.order_index, unit.applicability)
            )

        if unit.payload_kind == "table":
            for ref in payload_refs - state.projected_table_refs:
                state.table_payloads[ref].append(
                    (f"unit:{unit.order_index}", unit.payload)
                )
        if _payload_image_ref(unit.payload):
            for ref in payload_refs:
                state.image_payloads[ref].append(
                    (f"unit:{unit.order_index}", unit.payload)
                )
        if unit.payload_kind != "mixed":
            continue
        parts = unit.payload.get("parts")
        if not isinstance(parts, list) or not parts:
            continue
        for part_index, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            locator = part.get("artifact_locator")
            if not isinstance(locator, dict):
                findings.append(
                    AuditFinding(
                        code="mixed_part_locator_missing",
                        severity="error",
                        message="mixed source part has no artifact locator",
                        unit_order=unit.order_index,
                    )
                )
                continue
            part_roles: dict[str, set[str]] = defaultdict(set)
            carrier_id = f"unit:{unit.order_index}/part:{part_index}"
            part_heading_path = _string_list(part.get("heading_path"))
            graph_present, part_refs = _collect_projection_graph(
                locator,
                payload=part,
                heading_path=part_heading_path,
                applicability=(
                    str(part.get("applicability"))
                    if part.get("applicability") is not None
                    else unit.applicability
                ),
                carrier_id=carrier_id,
                source=source,
                metadata=metadata,
                state=state,
                findings=findings,
                unit=unit,
                local_roles=part_roles,
            )
            if not graph_present:
                part_refs = _walk_locator(
                    locator,
                    role="payload",
                    source=source,
                    state=state,
                    findings=findings,
                    unit=unit,
                    local_roles=part_roles,
                )
            if not part_refs:
                findings.append(
                    AuditFinding(
                        code="mixed_part_locator_unresolved",
                        severity="error",
                        message="mixed part locator has no resolvable source identity",
                        unit_order=unit.order_index,
                    )
                )
            part_payload_refs = part_roles["payload"]
            state.refs_by_unit[unit.order_index].update(part_payload_refs)
            for ref in (
                part_payload_refs | part_roles["structured"]
            ) & expected_applicability.keys():
                state.applicability_targets[ref].add(
                    (unit.order_index, unit.applicability)
                )
            if part.get("kind") == "table":
                for ref in part_payload_refs - state.projected_table_refs:
                    state.table_payloads[ref].append((carrier_id, part))
            if _payload_image_ref(part):
                for ref in part_payload_refs:
                    state.image_payloads[ref].append((carrier_id, part))
    return state


def _collect_projection_graph(
    locator: dict[str, Any],
    *,
    payload: Mapping[str, Any],
    heading_path: list[str],
    applicability: str | None,
    carrier_id: str,
    source: _SourceIndex,
    metadata: AuditDocumentMetadata,
    state: _CoverageState,
    findings: list[AuditFinding],
    unit: AuditUnitView,
    local_roles: dict[str, set[str]],
) -> tuple[bool, set[str]]:
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
    # The outer locator is a navigation hint, not an ownership oracle, but any
    # identity it does publish must still agree with the typed graph.
    if any(key in locator for key in ("ir_id", "source_item_index", "order_index")):
        _resolve_locator(locator, source=source, findings=findings, unit=unit)
    expected_keys = {"version", "payload", "heading_path", "structured", "provenance"}
    if set(raw) != expected_keys or raw.get("version") != UNIT_SOURCE_PROJECTION_VERSION:
        _projection_finding(
            findings,
            unit=unit,
            code="source_projection_contract_invalid",
            message="source_projection has an unsupported version or open field set",
        )
        return True, set()

    refs: set[str] = set()
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
            if kind == "document_metadata":
                if entry.get("field") != "title" or _norm(heading_path[target]) != _norm(metadata.title or ""):
                    _projection_finding(
                        findings,
                        unit=unit,
                        code="heading_projection_mismatch",
                        message="document-title projection does not match the public path",
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
                state.structure_texts[selected.ref].add(
                    _norm(heading_path[target])
                )
            if (
                len(selected_values) == len(raw_selectors)
                and _norm("".join(str(value.value) for value in selected_values))
                != _norm(heading_path[target])
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
                else _projection_target(payload, target_field)
            )
            if entry.get("kind") == "applicability_marker":
                expected = _independent_marker_value(str(selected.value))
                if expected is None or actual != expected:
                    _projection_finding(
                        findings,
                        unit=unit,
                        code="structured_projection_mismatch",
                        message="applicability marker does not reproduce its target",
                        source_ref=selected.ref,
                    )
                state.applicability_targets[selected.ref].add(
                    (unit.order_index, applicability)
                )
            elif entry.get("kind") == "derived_field":
                if actual in {None, ""} or _norm(actual) not in _norm(selected.value):
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

    provenance_entries = raw.get("provenance")
    if not isinstance(provenance_entries, list):
        _projection_finding(
            findings,
            unit=unit,
            code="provenance_projection_invalid",
            message="provenance projection must be an array",
        )
    else:
        for entry in provenance_entries:
            if not isinstance(entry, dict):
                _projection_finding(
                    findings,
                    unit=unit,
                    code="provenance_projection_invalid",
                    message="provenance entries must be objects",
                )
                continue
            kind = entry.get("kind")
            source_ref = _resolve_strict_source_ref(
                entry.get("source"), source=source, findings=findings, unit=unit
            )
            if source_ref is None:
                continue
            refs.add(source_ref)
            state.provenance_refs.add(source_ref)
            if kind == "exact_duplicate_of":
                canonical = _resolve_strict_source_ref(
                    entry.get("canonical"),
                    source=source,
                    findings=findings,
                    unit=unit,
                )
                if canonical is not None:
                    refs.add(canonical)
                    state.exact_dedup_groups.append(
                        frozenset({source_ref, canonical})
                    )
            elif kind == "table_continuation_ghost":
                root = _resolve_strict_source_ref(
                    entry.get("root"),
                    source=source,
                    findings=findings,
                    unit=unit,
                )
                if root is not None:
                    refs.add(root)
            else:
                _projection_finding(
                    findings,
                    unit=unit,
                    code="provenance_projection_invalid",
                    message="unsupported provenance projection kind",
                    source_ref=source_ref,
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
        target_field=target_field,
        target_value=_projection_target(payload, target_field),
        selectors=tuple(selectors),
        index=_strict_optional_int(raw.get("index")),
        count=_strict_optional_int(raw.get("count")),
        source_sha256=(
            str(raw["source_sha256"])
            if isinstance(raw.get("source_sha256"), str)
            else None
        ),
        unit_order=unit.order_index,
        carrier_id=carrier_id,
    )
    state.payload_projections.append(projection)
    # A table can be losslessly projected into a text/QA payload through
    # table_cell/table_rows selectors.  Physical selector type, not the outer
    # payload kind, determines whether the source is partitioned.  The table
    # partition validator still rejects gaps and overlapping whole+slices.
    state.projected_table_refs.update(
        selector.ref
        for selector in selectors
        if selector.kind.startswith("table_")
    )
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
        "table_header": common | {"index", "char_span"},
        "table_cell": common | {"row", "column", "char_span"},
        "table_rows": common | {"row_indices"},
        "table_note": common | {"index", "char_span"},
        "table_html": common | {"char_span"},
        "image": common,
        "image_caption": common | {"index", "char_span"},
        "image_footnote": common | {"index", "char_span"},
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
    required = {"ir_id", "source_item_index", "order_index"}
    allowed = required | {"page_no", "bbox", *TABLE_LOCATOR_FIELDS}
    if not required.issubset(raw) or not set(raw).issubset(allowed):
        _projection_finding(
            findings,
            unit=unit,
            code="source_ref_invalid",
            message="source reference requires closed ir/item/order identity",
        )
        return None
    table_fields = set(raw) & set(TABLE_LOCATOR_FIELDS)
    if table_fields and table_fields != set(TABLE_LOCATOR_FIELDS):
        _projection_finding(
            findings,
            unit=unit,
            code="source_ref_table_bundle_invalid",
            message="table provenance fields are an all-or-none bundle",
        )
        return None
    ir_id = raw.get("ir_id")
    source_item_index = raw.get("source_item_index")
    order_index = raw.get("order_index")
    if (
        not isinstance(ir_id, str)
        or not ir_id
        or not isinstance(source_item_index, int)
        or isinstance(source_item_index, bool)
        or not isinstance(order_index, int)
        or isinstance(order_index, bool)
    ):
        _projection_finding(
            findings,
            unit=unit,
            code="source_ref_identity_invalid",
            message="source identity values have invalid types",
        )
        return None
    identities = (
        source.by_ir_id.get(ir_id),
        source.by_source_item_index.get(source_item_index),
        source.by_order_index.get(order_index),
    )
    if None in identities or len(set(identities)) != 1:
        _projection_finding(
            findings,
            unit=unit,
            code="source_ref_identity_invalid",
            message="source identity fields do not resolve to one physical carrier",
        )
        return None
    ref = str(identities[0])
    element = source.elements[ref]
    for source_field in set(raw) - required:
        if raw[source_field] != element.get(source_field):
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


def _select_source_value(
    element: dict[str, Any], field: dict[str, Any]
) -> Any:
    kind = str(field["kind"])
    value: Any
    if kind == "text":
        value = _element_text(element)
    elif kind == "table":
        value = element.get("table")
    elif kind == "table_caption":
        value = _string_list(element.get("table_caption"))[_selector_index(field)]
    elif kind == "table_header":
        table = _source_table(element)
        value = _string_list(table.get("headers"))[_selector_index(field)]
    elif kind == "table_cell":
        table = _source_table(element)
        row = _selector_nonnegative_int(field, "row")
        column = _selector_nonnegative_int(field, "column")
        raw_rows = table.get("rows")
        if not isinstance(raw_rows, list) or not isinstance(raw_rows[row], list):
            raise TypeError("table rows are not a rectangular array")
        value = str(raw_rows[row][column])
    elif kind == "table_rows":
        table = _source_table(element)
        raw_rows = table.get("rows")
        indices = field.get("row_indices")
        if not isinstance(raw_rows, list) or not isinstance(indices, list) or not indices:
            raise TypeError("table_rows requires non-empty row_indices")
        parsed = [_selector_exact_int(index) for index in indices]
        if len(set(parsed)) != len(parsed) or parsed != sorted(parsed):
            raise ValueError("row_indices must be unique and ordered")
        value = [list(raw_rows[index]) for index in parsed]
    elif kind == "table_note":
        value = _string_list(element.get("table_footnote"))[_selector_index(field)]
    elif kind == "table_html":
        value = str(element.get("table_html") or "")
    elif kind == "image":
        value = str(element.get("image_path") or "")
    elif kind == "image_caption":
        captions = _string_list(element.get("image_caption") or element.get("caption"))
        value = captions[_selector_index(field)] if "index" in field else _image_caption(element)
    elif kind == "image_footnote":
        notes = _string_list(element.get("image_footnote"))
        value = notes[_selector_index(field)]
    else:
        raise ValueError(f"unsupported selector kind {kind}")
    span = field.get("char_span")
    if span is not None:
        if (
            not isinstance(value, str)
            or not isinstance(span, list)
            or len(span) != 2
        ):
            raise TypeError("char_span requires a string field and [start,end]")
        start = _selector_exact_int(span[0])
        end = _selector_exact_int(span[1])
        if start < 0 or end <= start or end > len(value):
            raise IndexError("char_span is outside the selected source value")
        value = value[start:end]
    return value


def _source_table(element: dict[str, Any]) -> dict[str, Any]:
    value = element.get("table")
    if not isinstance(value, dict):
        raise TypeError("source table is not an object")
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


def _strict_optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _projection_target(payload: Mapping[str, Any], target_field: object) -> Any:
    if target_field == "payload":
        return payload
    if not isinstance(target_field, str) or not target_field.startswith("payload."):
        return None
    current: Any = payload
    for part in target_field.removeprefix("payload.").split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return None
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
            continue
        return None
    return current


def _validate_payload_projection_value(
    projection: _ResolvedPayloadProjection,
    *,
    findings: list[AuditFinding],
) -> None:
    if projection.kind == "container":
        if not isinstance(projection.target_value, list):
            findings.append(
                AuditFinding(
                    code="payload_projection_mismatch",
                    severity="error",
                    message="container projection target must be an ordered parts array",
                    unit_order=projection.unit_order,
                )
            )
        return
    if projection.target_value is None:
        findings.append(
            AuditFinding(
                code="payload_projection_target_missing",
                severity="error",
                message=f"projection target {projection.target_field!r} is absent",
                unit_order=projection.unit_order,
            )
        )
        return
    if projection.kind in {
        "text_identity",
        "text_concat",
        "text_partition",
        "exact_duplicate_text",
    }:
        selected = "".join(str(item.value) for item in projection.selectors)
        if _projection_norm(selected) != _projection_norm(projection.target_value):
            findings.append(
                AuditFinding(
                    code="payload_projection_mismatch",
                    severity="error",
                    message="selected source text differs from its payload target",
                    source_ref=(projection.selectors[0].ref if projection.selectors else None),
                    unit_order=projection.unit_order,
                )
            )
    elif projection.kind in {"table_identity", "table_partition"}:
        if not _table_projection_matches(projection):
            findings.append(
                AuditFinding(
                    code="table_projection_mismatch",
                    severity="error",
                    message="selected table fields differ from the table payload",
                    source_ref=(projection.selectors[0].ref if projection.selectors else None),
                    unit_order=projection.unit_order,
                )
            )


def _projection_norm(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold().replace(r"\~", "~")
    return re.sub(r"\s+", "", normalized)


def _table_projection_matches(projection: _ResolvedPayloadProjection) -> bool:
    if not isinstance(projection.target_value, Mapping):
        return False
    payload = projection.target_value
    if projection.kind == "table_identity":
        if len(projection.selectors) != 1:
            return False
        selector = projection.selectors[0]
        if selector.kind != "table" or not isinstance(selector.value, dict):
            return False
        table = selector.value
        expected = {
            "headers": [str(value) for value in table.get("headers") or []],
            "rows": [
                [str(value) for value in row] for row in table.get("rows") or []
            ],
            "merged_cells": [
                dict(value) for value in table.get("merged_cells") or []
            ],
        }
        return all(payload.get(key) == value for key, value in expected.items())
    rows: list[list[str]] = []
    captions: list[str] = []
    notes: list[str] = []
    for selector in projection.selectors:
        if selector.kind == "table_rows":
            rows.extend(
                [str(value) for value in row]
                for row in selector.value
            )
        elif selector.kind == "table_caption":
            captions.append(str(selector.value))
        elif selector.kind == "table_note":
            notes.append(str(selector.value))
        else:
            return False
    return (
        payload.get("headers") == []
        and payload.get("rows") == rows
        and payload.get("merged_cells") == []
        and payload.get("caption") == captions
        and payload.get("notes") == notes
    )


def _projection_finding(
    findings: list[AuditFinding],
    *,
    unit: AuditUnitView,
    code: str,
    message: str,
    source_ref: str | None = None,
) -> None:
    findings.append(
        AuditFinding(
            code=code,
            severity="error",
            message=message,
            source_ref=source_ref,
            unit_order=unit.order_index,
        )
    )


def _walk_locator(
    locator: dict[str, Any],
    *,
    role: str,
    source: _SourceIndex,
    state: _CoverageState,
    findings: list[AuditFinding],
    unit: AuditUnitView,
    local_roles: dict[str, set[str]],
) -> set[str]:
    refs: set[str] = set()
    ref = _resolve_locator(locator, source=source, findings=findings, unit=unit)
    if ref is not None:
        refs.add(ref)
        _role_set(state, role).add(ref)
        local_roles[role].add(ref)

    derivation = locator.get("derivation")
    source_locators = locator.get("source_locators")
    if isinstance(source_locators, list):
        group: set[str] = set()
        for child in source_locators:
            if not isinstance(child, dict):
                findings.append(
                    AuditFinding(
                        code="source_locator_invalid",
                        severity="error",
                        message="source_locators must contain objects",
                        unit_order=unit.order_index,
                    )
                )
                continue
            child_refs = _walk_locator(
                child,
                role=role,
                source=source,
                state=state,
                findings=findings,
                unit=unit,
                local_roles=local_roles,
            )
            refs.update(child_refs)
            group.update(child_refs)
        if (
            isinstance(derivation, dict)
            and derivation.get("kind") == "exact_duplicate_carriers"
            and len(group) > 1
        ):
            state.exact_dedup_groups.append(frozenset(group))

    heading_locators = locator.get("heading_source_locators")
    if isinstance(heading_locators, list):
        # Search consumers see ``heading_path``.  Auditing only an internal
        # hierarchy would let a persisted/public breadcrumb silently lose the
        # very ancestor that makes the unit retrievable.
        path_norms = {
            _norm(value) for value in unit.heading_path if _norm(value)
        }
        for child in heading_locators:
            if not isinstance(child, dict):
                findings.append(
                    AuditFinding(
                        code="heading_source_locator_invalid",
                        severity="error",
                        message="heading_source_locators must contain objects",
                        unit_order=unit.order_index,
                    )
                )
                continue
            heading_text = child.get("heading_text")
            heading_norm = _norm(heading_text) if isinstance(heading_text, str) else ""
            if not heading_norm or heading_norm not in path_norms:
                findings.append(
                    AuditFinding(
                        code="heading_source_path_mismatch",
                        severity="error",
                        message="heading locator text is absent from the unit path",
                        unit_order=unit.order_index,
                    )
                )
            child_refs = _walk_locator(
                child,
                role="structure",
                source=source,
                state=state,
                findings=findings,
                unit=unit,
                local_roles=local_roles,
            )
            refs.update(child_refs)
            for child_ref in child_refs:
                if heading_norm:
                    state.structure_texts[child_ref].add(heading_norm)

    applicability_locator = locator.get("applicability_source_locator")
    if isinstance(applicability_locator, dict):
        applicability_refs = _walk_locator(
                applicability_locator,
                role="structured",
                source=source,
                state=state,
                findings=findings,
                unit=unit,
                local_roles=local_roles,
            )
        refs.update(applicability_refs)
        for child_ref in applicability_refs:
            state.applicability_targets[child_ref].add(
                (unit.order_index, unit.applicability)
            )

    continuation_indices = locator.get("continuation_source_item_indices")
    if isinstance(continuation_indices, list):
        for value in continuation_indices:
            if isinstance(value, int) and value in source.by_source_item_index:
                continuation_ref = source.by_source_item_index[value]
                refs.add(continuation_ref)
                # A continuation atom proves lineage of the reconciled root; it
                # is not a second payload owner.  Treating it as payload made a
                # merged table look like two independently emitted tables.
                state.provenance_refs.add(continuation_ref)
    return refs


def _resolve_locator(
    locator: dict[str, Any],
    *,
    source: _SourceIndex,
    findings: list[AuditFinding],
    unit: AuditUnitView,
) -> str | None:
    candidates: set[str] = set()
    supplied = False
    ir_id = locator.get("ir_id")
    if ir_id is not None:
        supplied = True
        if not isinstance(ir_id, str) or ir_id not in source.by_ir_id:
            _unresolved_locator(findings, unit=unit)
            return None
        candidates.add(source.by_ir_id[ir_id])
    source_index = locator.get("source_item_index")
    if source_index is not None:
        supplied = True
        if (
            not isinstance(source_index, int)
            or source_index not in source.by_source_item_index
        ):
            _unresolved_locator(findings, unit=unit)
            return None
        candidates.add(source.by_source_item_index[source_index])
    order_index = locator.get("order_index")
    if order_index is not None:
        supplied = True
        if not isinstance(order_index, int) or order_index not in source.by_order_index:
            _unresolved_locator(findings, unit=unit)
            return None
        candidates.add(source.by_order_index[order_index])
    if len(candidates) > 1:
        findings.append(
            AuditFinding(
                code="locator_identity_conflict",
                severity="error",
                message="locator identity fields resolve to different source atoms",
                unit_order=unit.order_index,
            )
        )
        return None
    if not supplied or not candidates:
        if supplied:
            _unresolved_locator(findings, unit=unit)
        return None
    ref = next(iter(candidates))
    element = source.elements[ref]
    for locator_field in ("page_no", "bbox"):
        if (
            locator_field in locator
            and locator_field in element
            and locator[locator_field] != element[locator_field]
        ):
            findings.append(
                AuditFinding(
                    code="locator_geometry_mismatch",
                    severity="error",
                    message=f"locator {locator_field} differs from its source atom",
                    source_ref=ref,
                    unit_order=unit.order_index,
                )
            )
    return ref


def _unresolved_locator(
    findings: list[AuditFinding], *, unit: AuditUnitView
) -> None:
    findings.append(
        AuditFinding(
            code="locator_identity_unresolved",
            severity="error",
            message="a supplied locator identity does not exist in the source IR",
            unit_order=unit.order_index,
        )
    )


def _role_set(state: _CoverageState, role: str) -> set[str]:
    if role == "structure":
        return state.structure_refs
    if role == "structured":
        return state.structured_refs
    return state.payload_refs


def _validate_structure_texts(
    source: _SourceIndex,
    *,
    state: _CoverageState,
    source_text_overrides: Mapping[str, str],
    findings: list[AuditFinding],
) -> None:
    for ref in sorted(state.structure_refs, key=source_order(source)):
        element = source.elements[ref]
        text = source_text_overrides.get(ref, _element_text(element))
        if not text:
            continue
        normalized = _norm(text)
        if not any(normalized in value for value in state.structure_texts.get(ref, set())):
            findings.append(
                AuditFinding(
                    code="structure_text_mismatch",
                    severity="error",
                    message="source heading text is not represented by its locator group",
                    source_ref=ref,
                )
            )


def _validate_text_conservation(
    source: _SourceIndex,
    *,
    units: list[AuditUnitView],
    state: _CoverageState,
    excluded_refs: set[str],
    source_text_overrides: dict[str, str],
    findings: list[AuditFinding],
) -> dict[str, int]:
    # Typed payload projections validate their selected source values directly
    # (including text/table-cell concatenation and QA partitions).  The older
    # connected-component check operates on whole source carriers and whole
    # unit payloads, so applying it to a typed unit would double-count mixed
    # carriers or sliced children.  Keep it only as the compatibility oracle
    # for units whose legacy locators have no typed payload projection.
    typed_unit_orders = {
        projection.unit_order for projection in state.payload_projections
    }
    source_to_units: dict[str, set[int]] = defaultdict(set)
    for unit_order, refs in state.refs_by_unit.items():
        if unit_order in typed_unit_orders:
            continue
        for ref in refs & state.payload_refs:
            source_to_units[ref].add(unit_order)

    duplicate_of = _validated_duplicate_map(
        source,
        groups=state.exact_dedup_groups,
        findings=findings,
    )
    unit_by_order = {unit.order_index: unit for unit in units}
    legacy_payload_refs = set(source_to_units)
    pending_sources = {
        ref
        for ref in legacy_payload_refs
        if ref not in excluded_refs
        and _source_text_for_ref(
            source,
            ref,
            source_text_overrides=source_text_overrides,
        )
    }
    visited_sources: set[str] = set()
    visited_units: set[int] = set()
    component_count = 0
    source_chars = 0
    output_chars = 0

    for start in sorted(pending_sources, key=source_order(source)):
        if start in visited_sources:
            continue
        queue: deque[tuple[str, str | int]] = deque([("source", start)])
        component_sources: set[str] = set()
        component_units: set[int] = set()
        while queue:
            kind, value = queue.popleft()
            if kind == "source":
                ref = str(value)
                if ref in visited_sources:
                    continue
                visited_sources.add(ref)
                component_sources.add(ref)
                for unit_order in source_to_units.get(ref, set()):
                    queue.append(("unit", unit_order))
            else:
                unit_order = int(value)
                if unit_order in visited_units:
                    continue
                visited_units.add(unit_order)
                component_units.add(unit_order)
                for ref in state.refs_by_unit.get(unit_order, set()):
                    if ref in pending_sources:
                        queue.append(("source", ref))

        ordered_sources = sorted(component_sources, key=source_order(source))
        source_text = "".join(
            _norm(
                _source_text_for_ref(
                    source,
                    ref,
                    source_text_overrides=source_text_overrides,
                )
            )
            for ref in ordered_sources
            if ref not in duplicate_of
        )
        output_text = "".join(
            _norm(_unit_primary_text(unit_by_order[order]))
            for order in sorted(component_units)
            if order in unit_by_order
        )
        component_count += 1
        source_chars += len(source_text)
        output_chars += len(output_text)
        if source_text != output_text:
            findings.append(
                AuditFinding(
                    code="text_component_mismatch",
                    severity="error",
                    message=(
                        "locator-connected text differs: "
                        f"source_chars={len(source_text)} output_chars={len(output_text)}"
                    ),
                    source_ref=ordered_sources[0] if ordered_sources else None,
                )
            )
    return {
        "components": component_count,
        "source_chars": source_chars,
        "output_chars": output_chars,
    }


def _validated_duplicate_map(
    source: _SourceIndex,
    *,
    groups: list[frozenset[str]],
    findings: list[AuditFinding],
) -> dict[str, str]:
    duplicate_of: dict[str, str] = {}
    for group in groups:
        ordered = sorted(group, key=source_order(source))
        values = {_norm(_source_text(source.elements[ref])) for ref in ordered}
        values.discard("")
        if len(values) > 1:
            findings.append(
                AuditFinding(
                    code="exact_dedup_content_mismatch",
                    severity="error",
                    message="exact duplicate derivation joins different source content",
                    source_ref=ordered[0] if ordered else None,
                )
            )
            continue
        if ordered:
            for ref in ordered[1:]:
                duplicate_of[ref] = ordered[0]
    return duplicate_of


def _validate_projection_partitions(
    source: _SourceIndex,
    *,
    state: _CoverageState,
    source_text_overrides: Mapping[str, str],
    findings: list[AuditFinding],
) -> None:
    _validate_text_partition_groups(state.payload_projections, findings=findings)
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
                residual_text=source_text_overrides.get(ref),
                claims=claims,
                findings=findings,
            )


def _validate_text_partition_groups(
    projections: list[_ResolvedPayloadProjection],
    *,
    findings: list[AuditFinding],
) -> None:
    groups: dict[str, list[_ResolvedPayloadProjection]] = defaultdict(list)
    for projection in projections:
        if projection.kind == "text_partition" and projection.source_sha256:
            groups[projection.source_sha256].append(projection)
    for digest, members in groups.items():
        counts = {member.count for member in members}
        indices = [member.index for member in members]
        if (
            len(counts) != 1
            or None in counts
            or any(index is None for index in indices)
        ):
            findings.append(
                AuditFinding(
                    code="text_partition_contract_invalid",
                    severity="error",
                    message="text partition requires one non-null count and index per child",
                    unit_order=members[0].unit_order,
                )
            )
            continue
        count = next(iter(counts))
        assert count is not None
        parsed_indices = [int(index) for index in indices if index is not None]
        if len(members) != count or sorted(parsed_indices) != list(range(count)):
            findings.append(
                AuditFinding(
                    code="text_partition_membership_invalid",
                    severity="error",
                    message="text partition children are missing, duplicated, or out of range",
                    unit_order=members[0].unit_order,
                )
            )
            continue
        ordered = sorted(members, key=lambda item: int(item.index or 0))
        reconstructed = "".join(
            _projection_norm(member.target_value) for member in ordered
        )
        actual = "sha256:" + hashlib.sha256(reconstructed.encode("utf-8")).hexdigest()
        if actual != digest:
            findings.append(
                AuditFinding(
                    code="text_partition_hash_mismatch",
                    severity="error",
                    message="ordered partition children do not reconstruct the source hash",
                    unit_order=members[0].unit_order,
                )
            )


def _validate_text_selector_partition(
    ref: str,
    *,
    text: str,
    residual_text: str | None,
    claims: list[_ResolvedSelector],
    findings: list[AuditFinding],
) -> None:
    payload_claims = [
        claim for claim in claims if claim.role == "payload" and claim.kind == "text"
    ]
    if not payload_claims or not text:
        return
    whole = [claim for claim in payload_claims if "char_span" not in claim.field]
    sliced = [claim for claim in payload_claims if "char_span" in claim.field]
    if len(whole) > 1 or (whole and sliced):
        findings.append(
            AuditFinding(
                code="text_selector_overlap",
                severity="error",
                message="one text carrier has overlapping whole/sliced payload claims",
                source_ref=ref,
            )
        )
        return
    if whole:
        return
    coverage = [0] * len(text)
    for claim in sliced:
        span = claim.field["char_span"]
        for index in range(int(span[0]), int(span[1])):
            coverage[index] += 1
    relevant_text = text if residual_text is None else residual_text
    if not relevant_text:
        relevant: list[int] = []
    elif residual_text is None or residual_text == text:
        relevant = [index for index, char in enumerate(text) if not char.isspace()]
    else:
        start = text.find(relevant_text)
        if start < 0 or start != text.rfind(relevant_text):
            findings.append(
                AuditFinding(
                    code="text_selector_residual_invalid",
                    severity="error",
                    message="source disposition residual is not one unique text span",
                    source_ref=ref,
                )
            )
            return
        relevant = [
            start + index
            for index, char in enumerate(relevant_text)
            if not char.isspace()
        ]
    if any(coverage[index] == 0 for index in relevant):
        findings.append(
            AuditFinding(
                code="text_selector_gap",
                severity="error",
                message="text payload slices leave non-whitespace source content uncovered",
                source_ref=ref,
            )
        )
    if any(coverage[index] > 1 for index in relevant):
        findings.append(
            AuditFinding(
                code="text_selector_overlap",
                severity="error",
                message="text payload slices overlap source content",
                source_ref=ref,
            )
        )


def _validate_table_selector_partition(
    ref: str,
    *,
    element: dict[str, Any],
    claims: list[_ResolvedSelector],
    findings: list[AuditFinding],
) -> None:
    table = _source_table(element)
    raw_rows = table.get("rows")
    rows: list[Any] = raw_rows if isinstance(raw_rows, list) else []
    headers = _string_list(table.get("headers"))
    captions = _string_list(element.get("table_caption"))
    notes = _string_list(element.get("table_footnote"))
    payload_whole = [
        claim for claim in claims if claim.role == "payload" and claim.kind == "table"
    ]
    payload_slices = [
        claim
        for claim in claims
        if claim.role == "payload" and claim.kind.startswith("table_")
    ]
    if len(payload_whole) > 1 or (payload_whole and payload_slices):
        findings.append(
            AuditFinding(
                code="table_selector_overlap",
                severity="error",
                message="one table has overlapping whole and sliced payload ownership",
                source_ref=ref,
            )
        )
        return
    if payload_whole:
        return

    # Structure is contextual and may truthfully repeat (the same table
    # caption can head several retrieval units).  Payload ownership must remain
    # disjoint.  Track union coverage for gaps and payload-only counts for
    # overlap, mirroring the text-slice validator above.
    coverage: dict[tuple[int, int], list[int]] = {}
    payload_coverage: dict[tuple[int, int], list[int]] = {}
    for row_index, row in enumerate(rows):
        if not isinstance(row, list):
            continue
        for column, value in enumerate(row):
            coverage[(row_index, column)] = [0] * len(str(value))
            payload_coverage[(row_index, column)] = [0] * len(str(value))
    header_counts = [0] * len(headers)
    payload_header_counts = [0] * len(headers)
    caption_counts = [0] * len(captions)
    payload_caption_counts = [0] * len(captions)
    note_counts = [0] * len(notes)
    payload_note_counts = [0] * len(notes)
    html_count = 0
    payload_html_count = 0
    for claim in claims:
        field = claim.field
        if claim.kind == "table_rows":
            for row_index in field["row_indices"]:
                row = rows[row_index]
                if not isinstance(row, list):
                    continue
                for column, value in enumerate(row):
                    coverage[(row_index, column)] = [
                        count + 1 for count in coverage[(row_index, column)]
                    ] or [1] * len(str(value))
                    if claim.role == "payload":
                        payload_coverage[(row_index, column)] = [
                            count + 1
                            for count in payload_coverage[(row_index, column)]
                        ] or [1] * len(str(value))
        elif claim.kind == "table_cell":
            key = (int(field["row"]), int(field["column"]))
            span = field.get("char_span") or [0, len(coverage[key])]
            for index in range(int(span[0]), int(span[1])):
                coverage[key][index] += 1
                if claim.role == "payload":
                    payload_coverage[key][index] += 1
        elif claim.kind == "table_header":
            index = int(field["index"])
            header_counts[index] += 1
            if claim.role == "payload":
                payload_header_counts[index] += 1
        elif claim.kind == "table_caption":
            index = int(field["index"])
            caption_counts[index] += 1
            if claim.role == "payload":
                payload_caption_counts[index] += 1
        elif claim.kind == "table_note":
            index = int(field["index"])
            note_counts[index] += 1
            if claim.role == "payload":
                payload_note_counts[index] += 1
        elif claim.kind == "table_html":
            html_count += 1
            if claim.role == "payload":
                payload_html_count += 1

    gap = False
    overlap = False
    for (row_index, column), counts in coverage.items():
        value = str(rows[row_index][column])
        for index, char in enumerate(value):
            if char.isspace():
                continue
            gap = gap or counts[index] == 0
            overlap = overlap or payload_coverage[(row_index, column)][index] > 1
    for values, counts, payload_counts in (
        (headers, header_counts, payload_header_counts),
        (captions, caption_counts, payload_caption_counts),
        (notes, note_counts, payload_note_counts),
    ):
        gap = gap or any(value.strip() and count == 0 for value, count in zip(values, counts))
        overlap = overlap or any(count > 1 for count in payload_counts)
    raw_html = str(element.get("table_html") or "")
    gap = gap or bool(raw_html.strip() and html_count == 0 and not rows and not headers)
    overlap = overlap or payload_html_count > 1
    if gap:
        findings.append(
            AuditFinding(
                code="table_selector_gap",
                severity="error",
                message="table slices leave non-empty source fields uncovered",
                source_ref=ref,
            )
        )
    if overlap:
        findings.append(
            AuditFinding(
                code="table_selector_overlap",
                severity="error",
                message="table slices claim source content more than once",
                source_ref=ref,
            )
        )


def _validate_tables(
    source: _SourceIndex,
    *,
    state: _CoverageState,
    findings: list[AuditFinding],
) -> None:
    for ref in sorted(
        state.payload_refs - state.projected_table_refs,
        key=source_order(source),
    ):
        element = source.elements[ref]
        if element.get("kind") != "table" or not _is_substantive(element):
            continue
        occurrences = state.table_payloads.get(ref, [])
        if len(occurrences) != 1:
            findings.append(
                AuditFinding(
                    code="table_payload_count_invalid",
                    severity="error",
                    message=(
                        "source table resolves to "
                        f"{len(occurrences)} table payload occurrences"
                    ),
                    source_ref=ref,
                )
            )
            continue
        _, payload = occurrences[0]
        table_value = element.get("table")
        table: dict[str, Any] = table_value if isinstance(table_value, dict) else {}
        captions = _string_list(element.get("table_caption"))
        notes = _string_list(element.get("table_footnote"))
        raw_html = str(element.get("table_html") or "")
        parse_failed = bool(element.get("table_parse_failed"))
        grid_empty = not (table.get("headers") or table.get("rows"))
        if parse_failed or (grid_empty and raw_html.strip()):
            expected: dict[str, Any] = {
                "caption": captions,
                "raw_html": raw_html,
                "notes": notes,
            }
            if not grid_empty:
                expected.update(
                    {
                        "headers": [
                            str(value) for value in table.get("headers") or []
                        ],
                        "rows": [
                            [str(value) for value in row]
                            for row in table.get("rows") or []
                        ],
                        "merged_cells": [
                            dict(value)
                            for value in table.get("merged_cells") or []
                        ],
                    }
                )
            actual: dict[str, Any] = {
                key: payload.get(key) for key in expected
            }
        else:
            expected = {
                "caption": captions,
                "headers": [str(value) for value in table.get("headers") or []],
                "rows": [
                    [str(value) for value in row]
                    for row in table.get("rows") or []
                ],
                "merged_cells": [
                    dict(value) for value in table.get("merged_cells") or []
                ],
                "notes": notes,
            }
            actual = {key: payload.get(key) for key in expected}
        if actual != expected:
            findings.append(
                AuditFinding(
                    code="table_structure_mismatch",
                    severity="error",
                    message="table payload differs from its source grid/fallback",
                    source_ref=ref,
                )
            )


def _validate_images(
    source: _SourceIndex,
    *,
    state: _CoverageState,
    image_hashes: dict[str, str],
    findings: list[AuditFinding],
) -> None:
    for ref, element in source.elements.items():
        if element.get("kind") != "image":
            continue
        image_path = str(element.get("image_path") or "").strip()
        if not image_path:
            continue
        occurrences = state.image_payloads.get(ref, [])
        if len(occurrences) != 1:
            findings.append(
                AuditFinding(
                    code="image_payload_count_invalid",
                    severity="error",
                    message=(
                        "source image resolves to "
                        f"{len(occurrences)} image payload occurrences"
                    ),
                    source_ref=ref,
                )
            )
            continue
        _, payload = occurrences[0]
        image_ref = _payload_image_ref(payload)
        refs = {image_ref} if image_ref is not None else set()
        if len(refs) != 1:
            findings.append(
                AuditFinding(
                    code="image_ref_missing",
                    severity="error",
                    message="source image does not resolve to one content-addressed ref",
                    source_ref=ref,
                )
            )
            continue
        expected = image_hashes.get(ref) or _digest_from_path(image_path)
        actual = _digest_from_path(next(iter(refs)))
        if expected is None:
            findings.append(
                AuditFinding(
                    code="image_hash_unavailable",
                    severity="error",
                    message="non-addressed source image was not hashed by the caller",
                    source_ref=ref,
                )
            )
        elif actual != expected.removeprefix("sha256:"):
            findings.append(
                AuditFinding(
                    code="image_hash_mismatch",
                    severity="error",
                    message="image_ref digest differs from source image bytes",
                    source_ref=ref,
                )
            )


def _validate_output_role_closure(
    *,
    source: _SourceIndex,
    state: _CoverageState,
    external_refs: set[str],
    partial_external_refs: set[str],
    empty_refs: set[str],
    findings: list[AuditFinding],
) -> None:
    emitted_refs = state.payload_refs | state.structure_refs | state.structured_refs
    for ref in sorted(external_refs & emitted_refs, key=source_order(source)):
        findings.append(
            AuditFinding(
                code="external_source_emitted",
                severity="error",
                message="fully externalized source atom is still emitted",
                source_ref=ref,
            )
        )
    for ref in sorted(empty_refs & emitted_refs, key=source_order(source)):
        findings.append(
            AuditFinding(
                code="empty_source_emitted",
                severity="error",
                message="proven-empty source atom is still emitted",
                source_ref=ref,
            )
        )
    for ref in sorted(
        partial_external_refs - state.payload_refs,
        key=source_order(source),
    ):
        findings.append(
            AuditFinding(
                code="partial_external_payload_missing",
                severity="error",
                message="partial metadata extraction has no residual payload carrier",
                source_ref=ref,
            )
        )
    for unit_order, refs in sorted(state.refs_by_unit.items()):
        if not refs:
            findings.append(
                AuditFinding(
                    code="output_source_closure_missing",
                    severity="error",
                    message="output carrier has no payload source edge",
                    unit_order=unit_order,
                )
            )


def _validate_unit_source_order(
    units: list[AuditUnitView],
    *,
    source: _SourceIndex,
    state: _CoverageState,
    findings: list[AuditFinding],
) -> None:
    previous_max: int | None = None
    previous_refs: set[str] | None = None
    for unit in sorted(units, key=lambda item: item.order_index):
        refs = state.refs_by_unit.get(unit.order_index, set())
        orders = [
            value
            for ref in refs
            if isinstance((value := source.elements[ref].get("order_index")), int)
        ]
        if not orders:
            continue
        current_min = min(orders)
        current_max = max(orders)
        if (
            previous_max is not None
            and refs != previous_refs
            and current_min < previous_max
        ):
            findings.append(
                AuditFinding(
                    code="unit_source_order_invalid",
                    severity="error",
                    message="unit payload sources move backwards in document order",
                    unit_order=unit.order_index,
                )
            )
        previous_max = max(previous_max or current_max, current_max)
        previous_refs = set(refs)


def _validate_units(
    units: list[AuditUnitView],
    *,
    metadata: AuditDocumentMetadata,
    findings: list[AuditFinding],
) -> None:
    if not units:
        findings.append(
            AuditFinding(
                code="empty_unit_output",
                severity="error",
                message="builder returned no document units",
            )
        )
    expected_orders = list(range(1, len(units) + 1))
    actual_orders = [unit.order_index for unit in units]
    if actual_orders != expected_orders:
        findings.append(
            AuditFinding(
                code="unit_order_invalid",
                severity="error",
                message="unit order_index values must be unique, contiguous, and ordered",
            )
        )
    for unit in units:
        try:
            validate_semantic_key_state(unit.semantic_key, unit.semantic_keys)
        except SemanticKeyInvariantError as exc:
            findings.append(
                AuditFinding(
                    code="semantic_key_invalid",
                    severity="error",
                    message=f"{exc.reason_code}: {exc}",
                    unit_order=unit.order_index,
                )
            )
        if any(not isinstance(value, str) or not value.strip() for value in unit.heading_path):
            findings.append(
                AuditFinding(
                    code="heading_path_segment_invalid",
                    severity="error",
                    message="heading_path contains an empty/non-string segment",
                    unit_order=unit.order_index,
                )
            )
        if "公告头信息" in unit.heading_path:
            findings.append(
                AuditFinding(
                    code="synthetic_header_anchor",
                    severity="error",
                    message="synthetic 公告头信息 path is forbidden",
                    unit_order=unit.order_index,
                )
            )
        if (
            unit.heading_path != unit.structural_path
            and not _has_typed_headerless_anchor(unit, metadata=metadata)
        ):
            findings.append(
                AuditFinding(
                    code="public_heading_path_mismatch",
                    severity="error",
                    message=(
                        "public heading_path must preserve source structure or use one "
                        "explicit typed source/document-title anchor"
                    ),
                    unit_order=unit.order_index,
                )
            )
        if unit.quality_status not in {"ok", "needs_review", "unusable"}:
            findings.append(
                AuditFinding(
                    code="quality_status_invalid",
                    severity="error",
                    message="quality_status is outside the public enum",
                    unit_order=unit.order_index,
                )
            )
        if unit.applicability not in {None, "applicable", "not_applicable"}:
            findings.append(
                AuditFinding(
                    code="applicability_invalid",
                    severity="error",
                    message="applicability is outside the public enum",
                    unit_order=unit.order_index,
                )
            )
        _validate_qa_projection(unit, findings=findings)
        _validate_title_projection(unit, metadata=metadata, findings=findings)


def _has_typed_headerless_anchor(
    unit: AuditUnitView,
    *,
    metadata: AuditDocumentMetadata,
) -> bool:
    """Accept one provenance-backed retrieval anchor when no hierarchy exists."""

    if unit.structural_path or len(unit.heading_path) != 1:
        return False
    locator = unit.artifact_locator
    if not isinstance(locator, dict):
        return False
    graph = locator.get("source_projection")
    if not isinstance(graph, dict):
        return False
    entries = graph.get("heading_path")
    if not isinstance(entries, list) or len(entries) != 1:
        return False
    entry = entries[0]
    if not isinstance(entry, dict) or entry.get("target_index") != 0:
        return False
    if entry.get("kind") == "document_metadata":
        return (
            entry.get("field") == "title"
            and _norm(unit.heading_path[0]) == _norm(metadata.title or "")
        )
    if entry.get("kind") == "source_field":
        return isinstance(entry.get("selector"), dict)
    return (
        entry.get("kind") == "source_concat"
        and isinstance(entry.get("sources"), list)
        and bool(entry["sources"])
        and all(isinstance(value, dict) for value in entry["sources"])
    )


def _validate_qa_projection(
    unit: AuditUnitView, *, findings: list[AuditFinding]
) -> None:
    if unit.payload_kind != "qa":
        return
    question = unit.payload.get("question")
    answer = unit.payload.get("answer")
    raw_text = unit.payload.get("raw_text")
    if not all(
        isinstance(value, str) and bool(value.strip())
        for value in (question, answer, raw_text)
    ):
        findings.append(
            AuditFinding(
                code="qa_projection_invalid",
                severity="error",
                message="QA payload requires non-empty question, answer, and raw_text",
                unit_order=unit.order_index,
            )
        )
        return
    assert isinstance(question, str)
    assert isinstance(answer, str)
    assert isinstance(raw_text, str)
    normalized_raw = _norm(raw_text)
    question_offset = normalized_raw.find(_norm(question))
    answer_offset = normalized_raw.find(_norm(answer), max(0, question_offset))
    if question_offset < 0 or answer_offset < question_offset:
        findings.append(
            AuditFinding(
                code="qa_projection_mismatch",
                severity="error",
                message="question and answer are not ordered substrings of raw_text",
                unit_order=unit.order_index,
            )
        )
    if _norm(unit.title or "") != _norm(question):
        findings.append(
            AuditFinding(
                code="qa_title_mismatch",
                severity="error",
                message="QA title must equal its question",
                unit_order=unit.order_index,
            )
        )


def _validate_title_projection(
    unit: AuditUnitView,
    *,
    metadata: AuditDocumentMetadata,
    findings: list[AuditFinding],
) -> None:
    if unit.title is None:
        return
    title = _norm(unit.title)
    if not title:
        findings.append(
            AuditFinding(
                code="title_invalid",
                severity="error",
                message="title must be non-empty when present",
                unit_order=unit.order_index,
            )
        )
        return
    candidates = [metadata.title or "", *unit.heading_path]
    candidates.extend(_payload_title_candidates(unit.payload_kind, unit.payload))
    if title not in {_norm(value) for value in candidates if _norm(value)}:
        findings.append(
            AuditFinding(
                code="title_provenance_missing",
                severity="error",
                message="title is not anchored in public path, payload, or document title",
                unit_order=unit.order_index,
            )
        )


def _payload_title_candidates(kind: str, payload: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("text", "question", "caption", "context", "title"):
        values.extend(_string_list(payload.get(key)))
    if kind == "mixed":
        for part in payload.get("parts") or []:
            if not isinstance(part, Mapping):
                continue
            values.extend(_payload_title_candidates(str(part.get("kind") or "text"), part))
            values.extend(_string_list(part.get("heading_path")))
            values.extend(_string_list(part.get("local_heading")))
    return values


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
        return bool(
            table.get("headers")
            or table.get("rows")
            or str(element.get("table_html") or "").strip()
            or any(
                value.strip()
                for value in _string_list(element.get("table_caption"))
            )
            or any(
                value.strip()
                for value in _string_list(element.get("table_footnote"))
            )
        )
    if kind == "image":
        return bool(
            str(element.get("image_path") or "").strip()
            or _image_source_text(element)
        )
    return bool(_element_text(element).strip())


def _source_text(element: dict[str, Any]) -> str:
    if element.get("kind") in _TEXT_KINDS:
        return _element_text(element)
    if element.get("kind") == "image":
        return _image_source_text(element)
    return ""


def _source_text_for_ref(
    source: _SourceIndex,
    ref: str,
    *,
    source_text_overrides: dict[str, str],
) -> str:
    if ref in source_text_overrides:
        return source_text_overrides[ref]
    return _source_text(source.elements[ref])


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
    if kind == "qa":
        raw = payload.get("raw_text")
        if isinstance(raw, str) and raw:
            return raw
        return "\n".join(
            str(payload.get(key) or "") for key in ("question", "answer")
        )
    if kind == "text":
        if _payload_image_ref(payload):
            values = [
                *_string_list(payload.get("caption")),
                *_string_list(payload.get("content")),
                *_string_list(payload.get("notes")),
            ]
            return "\n".join(value for value in values if value)
        value = payload.get("text")
        return value if isinstance(value, str) else ""
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
        if not values and isinstance(unit.payload.get("raw_html"), str):
            values.append(str(unit.payload["raw_html"]))
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
