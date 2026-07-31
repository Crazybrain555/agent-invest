"""Structure-blind, hash-bound source-text conservation for MinerU carriers."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, NoReturn, TypeGuard, cast
import unicodedata

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import (
    MinerUMiddleArtifact,
    MiddleTableRoleHint,
)
from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    MINERU_SCALAR_IR_FIELDS,
    MINERU_SEQUENCE_IR_FIELDS,
    MINERU_SUPPORTED_RAW_KINDS,
    MinerUFieldContractError,
    canonical_mineru_item_sha256,
    mineru_scalar_alias,
    mineru_text_sequence,
    mineru_typed_values,
)
from disclosure_anchor.adapters.parsers.pdf_native_text import (
    BBox,
    NATIVE_TEXT_RUN_ALGORITHM,
    NativeTextAtom,
    NativeTextGeometryIssue,
    NativeTextLayoutRef,
    NativeTextPage,
    native_text_runs,
    visual_guard_page_indices,
)
from disclosure_anchor.adapters.parsers.pdf_native_structure import (
    NativeStructureIndex,
    validate_pdf_structure_artifact,
)
from disclosure_anchor.adapters.parsers.pdf_visual_evidence import (
    PNG_OPTIONS_FIELDS,
    RENDER_OPTIONS_FIELDS,
    RENDERER_IDENTITY_FIELDS,
    VisualPageEvidence,
    VisualOccurrenceRequest,
    VisualRegionRequest,
    merged_visual_region_components,
)
from disclosure_anchor.application.contracts.html_visible_text import (
    html_visible_text_segments,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


CONTRACT_VERSION = "source-evidence-conservation.v12"
ALGORITHM_VERSION = "exact-native-middle-role-or-visual.v13"
TEXT_PROJECTION = "nfkc-strip-whitespace.v1"

_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_PAGE_VISUAL_ROLE = re.compile(r"^source_page_visual_[0-9]{6}$")
_BBOX_VISUAL_ROLE = re.compile(r"^source_bbox_visual_[0-9]{6}_[0-9]{6}$")
_OCCURRENCE_VISUAL_ROLE = re.compile(r"^source_visual_occurrence_[0-9]{6}$")
_IMAGE_ARTIFACT_ROLE = re.compile(r"^evidence_image_[0-9]{6}$")
_EXTENT = 1000.0
_TOLERANCE = 1.0 / _EXTENT
_FALLBACK_REASONS = frozenset(
    {
        "mineru_locator_unproved",
        "mineru_text_missing",
        "source_native_text_absent",
        "source_native_geometry_invalid",
    }
)
_ROOT_FIELDS = frozenset(
    {
        "algorithm_version",
        "atoms",
        "carrier_support",
        "contract_version",
        "coverage",
        "mineru_artifact",
        "mineru_typed_artifact",
        "middle_artifact",
        "pages",
        "retrieval_runs",
        "source_extractor",
        "source_pdf",
        "table_role_overrides",
        "text_projection",
        "visual_occurrences",
        "visual_renderer",
    }
)
_RETRIEVAL_RUN_FIELDS = frozenset(
    {
        "atom_indices",
        "bbox",
        "boundary_basis",
        "join_algorithm",
        "layout_line",
        "page_idx",
        "run_index",
        "text_sha256",
    }
)
_TABLE_SINGLETON_ALGORITHM = "table-cell-unproved-singleton.v1"
_TABLE_TD_RUN_ALGORITHM = "pdf-struct-tree-td-run.v1"
_PAGE_FIELDS = frozenset(
    {
        "atom_count",
        "fallback_reasons",
        "fallback_required",
        "geometry_issues",
        "height",
        "modality",
        "page_idx",
        "source_order_conflicts",
        "text",
        "text_sha256",
        "visual_artifact",
        "width",
    }
)
_GEOMETRY_ISSUE_FIELDS = frozenset(
    {"page_idx", "raw_bbox", "reason", "text", "text_sha256", "word_order"}
)
_VISUAL_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_role",
        "media_type",
        "pixel_height",
        "pixel_width",
        "sha256",
        "size_bytes",
    }
)
_VISUAL_RENDERER_FIELDS = frozenset(
    {"identity", "png_options", "profile_sha256", "render_options"}
)
_VISUAL_OCCURRENCE_FIELDS = frozenset(
    {
        "artifact",
        "bbox",
        "page_idx",
        "raw_kind",
        "source_item_index",
        "source_item_sha256",
    }
)
_CARRIER_SUPPORT_FIELDS = frozenset({"bbox", "page_idx", "selector", "support"})
_GENERATED_ARTIFACT_FIELDS = frozenset({"artifact_role", "sha256", "size_bytes"})
_TABLE_ROLE_FIELDS = frozenset(
    {
        "bbox",
        "field",
        "index",
        "page_idx",
        "parent_bbox",
        "provider_deleted",
        "source_item_index",
        "text",
        "text_sha256",
    }
)


class SourceEvidenceContractError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True)
class ExtractorIdentity:
    name: str
    version: str


@dataclass(frozen=True)
class MinerUTextCarrier:
    source_item_index: int
    page_idx: int | None
    bbox: BBox | None
    field: str
    index: int | None
    source_value: str
    comparison_value: str
    boundaries: frozenset[int]
    hard_boundaries: frozenset[int]
    part_order: int


@dataclass(frozen=True, slots=True)
class MinerUVisualOccurrence:
    source_item_index: int
    source_item_sha256: str
    raw_kind: str
    page_idx: int
    bbox: BBox


@dataclass(frozen=True, slots=True)
class CarrierSourceSupport:
    source_item_index: int
    field: str
    index: int | None
    page_idx: int
    bbox: BBox
    kind: str
    artifact_role: str | None
    artifact_sha256: str | None


@dataclass(frozen=True, slots=True)
class ResolvedTableRole:
    source_item_index: int
    page_idx: int
    parent_bbox: BBox
    field: str
    index: int
    bbox: BBox
    provider_deleted: bool
    text: str


@dataclass(frozen=True)
class _Occurrence:
    carrier: MinerUTextCarrier
    start: int
    end: int

    @property
    def position(self) -> tuple[int, int, int]:
        return self.carrier.source_item_index, self.carrier.part_order, self.start


# PDF symbol fonts map checkbox marks into the Private Use Area while the
# provider normalizes the same visible mark to a BALLOT BOX codepoint; exact
# occurrence matching therefore needs one canonical form. Only glyphs proven
# on real corpus enter this table — unknown variants still fall back to the
# conservation net and surface as redundant native recovery, never data loss.
from disclosure_anchor.adapters.parsers.comparison import (  # noqa: E402
    comparison_text,
)


def resolve_middle_table_roles(
    content_list: Sequence[Mapping[str, Any]],
    *,
    middle_artifact: MinerUMiddleArtifact,
    source_pages: Sequence[NativeTextPage],
) -> tuple[ResolvedTableRole, ...]:
    """Bind provider table roles to exact, same-page source PDF text slices."""

    _validate_pages(source_pages, middle_artifact.page_count)
    bindings = _bind_middle_roles_to_content(
        content_list,
        middle_artifact.table_roles,
    )
    pages = {page.page_idx: page for page in source_pages}
    used_atoms: set[tuple[int, int]] = set()
    resolved: list[ResolvedTableRole] = []
    for source_item_index, hint in bindings:
        page = pages[hint.page_idx]
        if page.geometry_issues:
            _fail(
                "middle_role_source_geometry_unproved",
                "table role page has native words without usable geometry",
            )
        # Center ownership decides membership: a word belongs to the role
        # exactly when its center falls inside the role bbox. Words that
        # merely graze the tolerance-inflated edge belong to the neighbor
        # region; the continuity, reuse and non-empty proofs below plus the
        # provider caption comparison still gate a mis-sliced role.
        selected = [
            atom_index
            for atom_index, atom in enumerate(page.atoms)
            if _bbox_center_in_role(atom.bbox, hint.role_bbox, page)
        ]
        if not selected or selected != list(range(selected[0], selected[-1] + 1)):
            _fail(
                "middle_role_source_span_unproved",
                "table role does not select one continuous native atom run",
            )
        atoms = page.atoms[selected[0] : selected[-1] + 1]
        atom_keys = {(page.page_idx, atom.order) for atom in atoms}
        if used_atoms & atom_keys:
            _fail(
                "middle_role_source_span_ambiguous",
                "table roles reuse the same native source atom",
            )
        used_atoms.update(atom_keys)
        start = atoms[0].char_span[0]
        end = atoms[-1].char_span[1]
        text = page.text[start:end]
        if not comparison_text(text):
            _fail(
                "middle_role_source_text_empty",
                "table role resolves to empty source text",
            )
        resolved.append(
            ResolvedTableRole(
                source_item_index=source_item_index,
                page_idx=hint.page_idx,
                parent_bbox=hint.parent_bbox,
                field=hint.field,
                index=hint.field_index,
                bbox=hint.role_bbox,
                provider_deleted=hint.provider_deleted,
                text=text,
            )
        )
    _validate_provider_table_role_values(content_list, resolved)
    return tuple(
        sorted(
            resolved,
            key=lambda item: (
                item.source_item_index,
                0 if item.field == "table_caption" else 1,
                item.index,
            ),
        )
    )


def table_role_values_by_item(
    roles: Sequence[ResolvedTableRole],
) -> Mapping[tuple[int, str], tuple[str, ...]]:
    """Expose complete field replacements without mutating provider items."""

    grouped: dict[tuple[int, str], list[ResolvedTableRole]] = {}
    for role in roles:
        grouped.setdefault((role.source_item_index, role.field), []).append(role)
    output: dict[tuple[int, str], tuple[str, ...]] = {}
    for key, values in grouped.items():
        ordered = sorted(values, key=lambda item: item.index)
        if [item.index for item in ordered] != list(range(len(ordered))):
            _fail(
                "middle_role_index_invalid",
                "resolved table role indices are not contiguous",
            )
        output[key] = tuple(item.text for item in ordered)
    return MappingProxyType(output)


def _bind_middle_roles_to_content(
    content_list: Sequence[Mapping[str, Any]],
    hints: Sequence[MiddleTableRoleHint],
) -> tuple[tuple[int, MiddleTableRoleHint], ...]:
    parents: dict[tuple[int, BBox], list[MiddleTableRoleHint]] = {}
    for hint in hints:
        parents.setdefault((hint.page_idx, hint.parent_bbox), []).append(hint)
    content_tables: list[tuple[int, int, BBox]] = []
    for source_item_index, item in enumerate(content_list):
        if item.get("type") != "table":
            continue
        page_idx = _page_idx(item.get("page_idx"))
        bbox = _optional_bbox(item.get("bbox"))
        if page_idx is None or bbox is None:
            _fail(
                "middle_role_parent_unbound",
                "content-list table lacks a page-local locator",
            )
        content_tables.append((source_item_index, page_idx, bbox))

    used_tables: set[int] = set()
    bound: list[tuple[int, MiddleTableRoleHint]] = []
    for (page_idx, parent_bbox), parent_hints in parents.items():
        candidates = [
            source_item_index
            for source_item_index, candidate_page, candidate_bbox in content_tables
            if candidate_page == page_idx
            and max(
                abs(left - right)
                for left, right in zip(candidate_bbox, parent_bbox, strict=True)
            )
            <= 3.0
        ]
        if len(candidates) != 1 or candidates[0] in used_tables:
            _fail(
                "middle_role_parent_unbound",
                "middle table parent has no unique content-list table",
            )
        source_item_index = candidates[0]
        used_tables.add(source_item_index)
        bound.extend((source_item_index, hint) for hint in parent_hints)
    return tuple(bound)


def _validate_provider_table_role_values(
    content_list: Sequence[Mapping[str, Any]],
    roles: Sequence[ResolvedTableRole],
) -> None:
    grouped: dict[tuple[int, str], list[ResolvedTableRole]] = {}
    for role in roles:
        grouped.setdefault((role.source_item_index, role.field), []).append(role)
    for source_item_index, item in enumerate(content_list):
        if item.get("type") != "table":
            continue
        for field in ("table_caption", "table_footnote"):
            expected = sorted(
                grouped.get((source_item_index, field), ()),
                key=lambda role: role.index,
            )
            if [role.index for role in expected] != list(range(len(expected))):
                _fail(
                    "middle_role_index_invalid",
                    "middle table role indices are not contiguous",
                )
            provider = _sequence_values(item.get(field), field)
            provider_index = 0
            for role in expected:
                if provider_index < len(provider) and comparison_text(
                    provider[provider_index]
                ) == comparison_text(role.text):
                    provider_index += 1
                elif not role.provider_deleted:
                    _fail(
                        "middle_role_provider_conflict",
                        "non-deleted table role differs from content_list",
                    )
            if provider_index != len(provider):
                _fail(
                    "middle_role_provider_conflict",
                    "content_list table text has no middle role",
                )


def _sequence_values(value: object, field: str) -> list[str]:
    try:
        return mineru_text_sequence(value, field=field)
    except MinerUFieldContractError as exc:
        _fail(exc.reason_code, str(exc))


def iter_mineru_text_carriers(
    content_list: Sequence[Mapping[str, Any]],
    *,
    table_role_overrides: Sequence[ResolvedTableRole] = (),
) -> tuple[MinerUTextCarrier, ...]:
    """Enumerate the closed typed-field schema using mapped-IR field names."""

    role_values = table_role_values_by_item(table_role_overrides)
    role_bboxes: dict[tuple[int, str, int | None], BBox] = {
        (role.source_item_index, role.field, role.index): role.bbox
        for role in table_role_overrides
    }
    if len(role_bboxes) != len(table_role_overrides):
        _fail("middle_role_index_invalid", "resolved table roles are duplicated")
    carriers: list[MinerUTextCarrier] = []
    for source_index, item in enumerate(content_list):
        raw_type = item.get("type")
        if not isinstance(raw_type, str) or raw_type not in MINERU_SUPPORTED_RAW_KINDS:
            _fail("mineru_type_unsupported", f"unsupported type: {raw_type!r}")
        page_idx = _page_idx(item.get("page_idx"))
        bbox = _optional_bbox(item.get("bbox"))
        carrier_item = _with_table_role_values(
            item,
            source_item_index=source_index,
            role_values=role_values,
        )
        for part_order, (field, index, raw_value) in enumerate(
            _typed_values(carrier_item, raw_type)
        ):
            carrier_bbox = role_bboxes.get((source_index, field, index), bbox)
            comparison_value, boundaries, hard_boundaries = _project_field(
                field,
                raw_value,
            )
            if comparison_value:
                carriers.append(
                    MinerUTextCarrier(
                        source_index,
                        page_idx,
                        carrier_bbox,
                        field,
                        index,
                        raw_value,
                        comparison_value,
                        boundaries,
                        hard_boundaries,
                        part_order,
                    )
                )
    return tuple(carriers)


def _with_table_role_values(
    item: Mapping[str, Any],
    *,
    source_item_index: int,
    role_values: Mapping[tuple[int, str], tuple[str, ...]],
) -> Mapping[str, Any]:
    replacements = {
        field: list(values)
        for field in ("table_caption", "table_footnote")
        if (values := role_values.get((source_item_index, field))) is not None
    }
    return {**item, **replacements} if replacements else item


def required_carrier_visual_regions(
    content_list: Sequence[Mapping[str, Any]],
    *,
    source_pages: Sequence[NativeTextPage],
    source_pdf_page_count: int,
    table_role_overrides: Sequence[ResolvedTableRole] = (),
) -> tuple[VisualRegionRequest, ...]:
    """Return only typed carriers not fully covered by bbox-aligned native atoms."""

    _validate_pages(source_pages, source_pdf_page_count)
    carriers, records_by_page = _carrier_reconciliation(
        content_list,
        source_pages=source_pages,
        source_pdf_page_count=source_pdf_page_count,
        table_role_overrides=table_role_overrides,
    )
    requests: list[VisualRegionRequest] = []
    for carrier in carriers:
        if _native_atom_orders(carrier, records_by_page) is not None:
            continue
        page_idx, bbox = _required_carrier_locator(
            carrier,
            source_pdf_page_count=source_pdf_page_count,
        )
        requests.append(VisualRegionRequest(page_idx, bbox))
    return tuple(requests)


def mineru_visual_occurrences(
    content_list: Sequence[Mapping[str, Any]],
    *,
    source_pdf_page_count: int,
) -> tuple[MinerUVisualOccurrence, ...]:
    """Enumerate every image/chart occurrence at its exact provider index."""

    if not _is_index(source_pdf_page_count) or source_pdf_page_count < 1:
        _fail("source_page_count_invalid", "page count must be positive")
    occurrences: list[MinerUVisualOccurrence] = []
    for source_item_index, item in enumerate(content_list):
        raw_kind = item.get("type")
        if raw_kind not in {"image", "chart"}:
            continue
        page_idx = _page_idx(item.get("page_idx"))
        bbox = _optional_bbox(item.get("bbox"))
        if page_idx is None or page_idx >= source_pdf_page_count or bbox is None:
            _fail(
                "visual_occurrence_unbound",
                "image/chart occurrence requires an exact source page and bbox",
            )
        try:
            source_item_sha256 = canonical_mineru_item_sha256(item)
        except (TypeError, ValueError) as exc:
            raise SourceEvidenceContractError(
                "visual_occurrence_identity_invalid",
                "image/chart provider item is not canonical JSON",
            ) from exc
        occurrences.append(
            MinerUVisualOccurrence(
                source_item_index=source_item_index,
                source_item_sha256=source_item_sha256,
                raw_kind=cast(str, raw_kind),
                page_idx=page_idx,
                bbox=bbox,
            )
        )
    return tuple(occurrences)


def required_visual_occurrence_regions(
    content_list: Sequence[Mapping[str, Any]],
    *,
    source_pdf_page_count: int,
) -> tuple[VisualOccurrenceRequest, ...]:
    """Return one non-merged render request per image/chart occurrence."""

    return tuple(
        VisualOccurrenceRequest(
            source_item_index=occurrence.source_item_index,
            page_idx=occurrence.page_idx,
            bbox=occurrence.bbox,
        )
        for occurrence in mineru_visual_occurrences(
            content_list,
            source_pdf_page_count=source_pdf_page_count,
        )
    )


def _generated_image_annotations(
    content_list: Sequence[Mapping[str, Any]],
) -> tuple[MinerUTextCarrier, ...]:
    return _visual_text_carriers(content_list, raw_kind="image")


def _chart_visual_recognitions(
    content_list: Sequence[Mapping[str, Any]],
) -> tuple[MinerUTextCarrier, ...]:
    return _visual_text_carriers(content_list, raw_kind="chart")


def _visual_text_carriers(
    content_list: Sequence[Mapping[str, Any]],
    *,
    raw_kind: str,
) -> tuple[MinerUTextCarrier, ...]:
    annotations: list[MinerUTextCarrier] = []
    for source_index, item in enumerate(content_list):
        if item.get("type") != raw_kind:
            continue
        try:
            value = mineru_scalar_alias(item, ("text", "content"))
        except MinerUFieldContractError as exc:
            _fail(exc.reason_code, str(exc))
        if value is None:
            continue
        comparison_value, boundaries, hard_boundaries = _project_field(
            "text",
            value,
        )
        if comparison_value:
            annotations.append(
                MinerUTextCarrier(
                    source_item_index=source_index,
                    page_idx=_page_idx(item.get("page_idx")),
                    bbox=_optional_bbox(item.get("bbox")),
                    field="text",
                    index=None,
                    source_value=value,
                    comparison_value=comparison_value,
                    boundaries=boundaries,
                    hard_boundaries=hard_boundaries,
                    part_order=len(_typed_values(item, raw_kind)),
                )
            )
    return tuple(annotations)


def reconcile_source_evidence(
    *,
    source_pdf_sha256: str,
    source_pdf_page_count: int,
    source_extractor: ExtractorIdentity,
    source_pages: Sequence[NativeTextPage],
    native_structure: Mapping[str, Any],
    mineru_content_list_bytes: bytes,
    expected_mineru_artifact_sha256: str,
    canonical_content_list: Sequence[Mapping[str, Any]],
    expected_mineru_typed_artifact_sha256: str,
    mineru_extractor: ExtractorIdentity,
    middle_artifact: MinerUMiddleArtifact | None = None,
    table_role_overrides: Sequence[ResolvedTableRole] = (),
    visual_pages: Sequence[VisualPageEvidence] = (),
    visual_regions: Sequence[VisualPageEvidence] = (),
    visual_occurrence_artifacts: Sequence[VisualPageEvidence] = (),
    generated_annotation_artifacts: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    _require_sha(source_pdf_sha256, "source_pdf_sha256")
    _require_sha(expected_mineru_artifact_sha256, "mineru_artifact_sha256")
    _require_sha(
        expected_mineru_typed_artifact_sha256,
        "mineru_typed_artifact_sha256",
    )
    _identity(source_extractor, "source_extractor")
    _identity(mineru_extractor, "mineru_extractor")
    if not _is_index(source_pdf_page_count) or source_pdf_page_count < 1:
        _fail("source_page_count_invalid", "page count must be positive")
    _validate_pages(source_pages, source_pdf_page_count)
    try:
        native_structure_index = validate_pdf_structure_artifact(
            native_structure,
            expected_source_pdf_sha256=source_pdf_sha256,
            expected_page_count=source_pdf_page_count,
        )
    except ParserOutputContractError as exc:
        _fail("native_structure_invalid", str(exc))
    payload = _content_list_payload(
        mineru_content_list_bytes,
        expected_sha256=expected_mineru_artifact_sha256,
    )
    canonical_payload = _validated_canonical_content_list(
        payload,
        canonical_content_list,
    )
    artifact_hash = expected_mineru_artifact_sha256
    expected_visual_occurrences = mineru_visual_occurrences(
        payload,
        source_pdf_page_count=source_pdf_page_count,
    )
    visual_occurrence_records, visual_occurrences_by_source = (
        _visual_occurrence_records(
            expected_visual_occurrences,
            visual_occurrence_artifacts,
        )
    )
    visual_by_page = _visual_pages_by_index(
        visual_pages,
        source_pages=source_pages,
    )
    carriers, records_by_page = _carrier_reconciliation(
        canonical_payload,
        source_pages=source_pages,
        source_pdf_page_count=source_pdf_page_count,
        table_role_overrides=table_role_overrides,
    )
    visual_regions_by_role = _visual_regions_by_role(visual_regions)
    visual_recognitions = _chart_visual_recognitions(canonical_payload)
    generated_annotations = _generated_image_annotations(canonical_payload)
    carrier_support = _carrier_support_records(
        carriers,
        visual_recognitions=visual_recognitions,
        generated_annotations=generated_annotations,
        records_by_page=records_by_page,
        source_pdf_page_count=source_pdf_page_count,
        full_page_visuals=visual_by_page,
        required_visual_page_support=frozenset(
            page.page_idx
            for page in source_pages
            if not page.atoms and not page.geometry_issues
        ),
        visual_regions=visual_regions_by_role,
        visual_occurrences=visual_occurrences_by_source,
        generated_annotation_artifacts=generated_annotation_artifacts or {},
    )
    atoms = [record for records in records_by_page for record in records]
    retrieval_runs = _retrieval_run_records(
        source_pages,
        records_by_page,
        native_structure=native_structure_index,
        table_bboxes_by_page=_table_body_bboxes(
            canonical_payload,
            source_pdf_page_count=source_pdf_page_count,
        ),
    )
    dispositions = [cast(Mapping[str, Any], atom["disposition"]) for atom in atoms]
    pages: list[dict[str, Any]] = []
    for page, records in zip(source_pages, records_by_page, strict=True):
        visual = visual_by_page.get(page.page_idx)
        reasons: Counter[str] = Counter()
        if page.geometry_issues:
            reasons["source_native_geometry_invalid"] = len(page.geometry_issues)
        elif not page.atoms:
            reasons["source_native_text_absent"] = 1
        order_conflicts = 0
        for record in records:
            disposition = cast(Mapping[str, Any], record["disposition"])
            if disposition.get("kind") == "source_native_fallback":
                reasons[str(disposition["reason"])] += 1
            if disposition.get("source_order") == "conflict":
                order_conflicts += 1
        modality = (
            "native_text"
            if visual is None
            else "native_text_with_visual_guard"
            if page.atoms
            else "visual_page"
        )
        pages.append(
            {
                "page_idx": page.page_idx,
                "width": page.width,
                "height": page.height,
                "modality": modality,
                "text": page.text,
                "text_sha256": _digest(page.text.encode()),
                "atom_count": len(page.atoms),
                "fallback_required": bool(reasons),
                "fallback_reasons": dict(sorted(reasons.items())),
                "geometry_issues": [
                    {
                        "page_idx": issue.page_idx,
                        "word_order": issue.word_order,
                        "text": issue.text,
                        "text_sha256": _digest(issue.text.encode()),
                        "raw_bbox": (
                            list(issue.raw_bbox) if issue.raw_bbox is not None else None
                        ),
                        "reason": issue.reason,
                    }
                    for issue in page.geometry_issues
                ],
                "source_order_conflicts": order_conflicts,
                "visual_artifact": (
                    _visual_artifact_descriptor(visual) if visual is not None else None
                ),
            }
        )
    ledger = {
        "contract_version": CONTRACT_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "text_projection": TEXT_PROJECTION,
        "pages": pages,
        "retrieval_runs": retrieval_runs,
        "carrier_support": carrier_support,
        "visual_occurrences": visual_occurrence_records,
        "source_pdf": {
            "sha256": source_pdf_sha256,
            "page_count": source_pdf_page_count,
        },
        "source_extractor": {
            "name": source_extractor.name,
            "version": source_extractor.version,
        },
        "visual_renderer": _visual_renderer_profile(
            (*visual_pages, *visual_regions, *visual_occurrence_artifacts)
        ),
        "mineru_artifact": {
            "role": "content_list",
            "sha256": artifact_hash,
            "extractor": {
                "name": mineru_extractor.name,
                "version": mineru_extractor.version,
            },
        },
        "mineru_typed_artifact": {
            "role": "content_list_v2",
            "sha256": expected_mineru_typed_artifact_sha256,
        },
        "middle_artifact": _middle_artifact_record(middle_artifact),
        "table_role_overrides": [
            _table_role_record(role) for role in table_role_overrides
        ],
        "coverage": {
            "source_atoms": len(atoms),
            "mineru_carriers": sum(
                item.get("kind") == "mineru_carrier" for item in dispositions
            ),
            "source_native_fallbacks": sum(
                item.get("kind") == "source_native_fallback" for item in dispositions
            ),
            "retrieval_runs": len(retrieval_runs),
            "retrieval_run_atoms": sum(
                len(cast(list[int], run["atom_indices"])) for run in retrieval_runs
            ),
            "source_order_conflicts": sum(
                item.get("source_order") == "conflict" for item in dispositions
            ),
            "native_text_pages": sum(bool(page.atoms) for page in source_pages),
            "visual_pages": len(visual_pages),
            "visual_regions": len(visual_regions),
            "visual_occurrences": len(visual_occurrence_records),
            "mineru_text_carriers": len(carriers) + len(visual_recognitions),
            "middle_table_roles": len(table_role_overrides),
            "native_exact_carriers": sum(
                record["support"]["kind"] == "native_exact"
                for record in carrier_support
            ),
            "visual_bound_carriers": sum(
                record["support"]["kind"] == "visual_bound"
                for record in carrier_support
            ),
            "generated_annotations": sum(
                record["support"]["kind"] == "generated_annotation"
                for record in carrier_support
            ),
            "native_geometry_issues": sum(
                len(page.geometry_issues) for page in source_pages
            ),
        },
        "atoms": atoms,
    }
    validate_source_evidence_ledger(
        ledger,
        expected_source_pdf_sha256=source_pdf_sha256,
        expected_source_pdf_page_count=source_pdf_page_count,
        expected_mineru_artifact_sha256=artifact_hash,
        mineru_content_list_bytes=mineru_content_list_bytes,
        canonical_content_list=canonical_payload,
        expected_mineru_typed_artifact_sha256=(expected_mineru_typed_artifact_sha256),
        native_structure=native_structure,
        mineru_middle_artifact=middle_artifact,
    )
    return ledger


def validate_source_evidence_ledger(
    value: object,
    *,
    expected_source_pdf_sha256: str,
    expected_source_pdf_page_count: int,
    expected_mineru_artifact_sha256: str,
    mineru_content_list_bytes: bytes,
    canonical_content_list: Sequence[Mapping[str, Any]],
    expected_mineru_typed_artifact_sha256: str,
    native_structure: Mapping[str, Any],
    mineru_middle_artifact: MinerUMiddleArtifact | None = None,
    parser_artifacts: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Validate the closed ledger and every PDF-to-carrier disposition."""

    ledger = _mapping(value, "source_evidence_invalid")
    if set(ledger) != _ROOT_FIELDS:
        _fail("source_evidence_fields_invalid", "ledger fields are not closed")
    if (
        ledger["contract_version"] != CONTRACT_VERSION
        or ledger["algorithm_version"] != ALGORITHM_VERSION
        or ledger["text_projection"] != TEXT_PROJECTION
    ):
        _fail("source_evidence_version_unsupported", "unsupported ledger version")
    _require_sha(expected_source_pdf_sha256, "expected_source_pdf_sha256")
    _require_sha(
        expected_mineru_artifact_sha256,
        "expected_mineru_artifact_sha256",
    )
    _require_sha(
        expected_mineru_typed_artifact_sha256,
        "expected_mineru_typed_artifact_sha256",
    )
    if (
        not _is_index(expected_source_pdf_page_count)
        or expected_source_pdf_page_count < 1
    ):
        _fail("source_page_count_invalid", "expected page count must be positive")
    try:
        native_structure_index = validate_pdf_structure_artifact(
            native_structure,
            expected_source_pdf_sha256=expected_source_pdf_sha256,
            expected_page_count=expected_source_pdf_page_count,
        )
    except ParserOutputContractError as exc:
        _fail("native_structure_invalid", str(exc))

    source_pdf = _mapping(ledger["source_pdf"], "source_pdf_invalid")
    if (
        set(source_pdf) != {"page_count", "sha256"}
        or source_pdf["sha256"] != expected_source_pdf_sha256
        or source_pdf["page_count"] != expected_source_pdf_page_count
    ):
        _fail("source_pdf_identity_mismatch", "source PDF identity differs")
    _validate_identity_mapping(
        ledger["source_extractor"],
        field="source_extractor",
    )
    mineru = _mapping(ledger["mineru_artifact"], "mineru_artifact_invalid")
    if (
        set(mineru) != {"extractor", "role", "sha256"}
        or mineru["role"] != "content_list"
        or mineru["sha256"] != expected_mineru_artifact_sha256
    ):
        _fail("mineru_artifact_identity_mismatch", "MinerU artifact differs")
    _validate_identity_mapping(
        mineru["extractor"],
        field="mineru_extractor",
    )
    typed_mineru = _mapping(
        ledger["mineru_typed_artifact"],
        "mineru_typed_artifact_invalid",
    )
    if typed_mineru != {
        "role": "content_list_v2",
        "sha256": expected_mineru_typed_artifact_sha256,
    }:
        _fail(
            "mineru_typed_artifact_identity_mismatch",
            "MinerU typed artifact differs",
        )
    if ledger["middle_artifact"] != _middle_artifact_record(mineru_middle_artifact):
        _fail(
            "middle_artifact_identity_mismatch",
            "MinerU middle artifact identity differs",
        )
    typed_content = _content_list_payload(
        mineru_content_list_bytes,
        expected_sha256=expected_mineru_artifact_sha256,
    )
    canonical_content = _validated_canonical_content_list(
        typed_content,
        canonical_content_list,
    )
    expected_annotations = _generated_image_annotations(canonical_content)
    expected_visual_recognitions = _chart_visual_recognitions(canonical_content)
    expected_visual_occurrences = mineru_visual_occurrences(
        typed_content,
        source_pdf_page_count=expected_source_pdf_page_count,
    )
    occurrence_artifacts = _validate_visual_occurrence_records(
        ledger["visual_occurrences"],
        expected_occurrences=expected_visual_occurrences,
    )
    visual_renderer = ledger["visual_renderer"]
    if visual_renderer is not None:
        _validate_visual_renderer_profile(visual_renderer)

    raw_pages = _list(ledger["pages"], "source_pages_invalid")
    if len(raw_pages) != expected_source_pdf_page_count:
        _fail("source_page_closure_invalid", "ledger page count differs")
    pages: dict[int, Mapping[str, Any]] = {}
    visual_artifacts: dict[str, Mapping[str, Any]] = {}
    for expected_idx, raw_page in enumerate(raw_pages):
        page = _mapping(raw_page, "source_page_invalid")
        if set(page) != _PAGE_FIELDS or page.get("page_idx") != expected_idx:
            _fail("source_page_invalid", "page fields/order are not closed")
        width = _finite_positive(page.get("width"))
        height = _finite_positive(page.get("height"))
        text = page.get("text")
        atom_count = page.get("atom_count")
        conflicts = page.get("source_order_conflicts")
        modality = page.get("modality")
        if (
            width is None
            or height is None
            or not isinstance(text, str)
            or page.get("text_sha256") != _digest(text.encode())
            or not _is_index(atom_count)
            or not _is_index(conflicts)
            or not isinstance(page.get("fallback_required"), bool)
        ):
            _fail("source_page_invalid", "page text/geometry/count is invalid")
        reasons = _count_mapping(
            page.get("fallback_reasons"),
            allowed=_FALLBACK_REASONS,
            field="fallback_reasons",
        )
        geometry_issues = _validate_geometry_issues(
            page.get("geometry_issues"),
            page_idx=expected_idx,
        )
        geometry_count = len(geometry_issues)
        visual_artifact = page.get("visual_artifact")
        if modality == "native_text":
            if (
                not text
                or atom_count < 1
                or visual_artifact is not None
                or geometry_count
                or reasons.get("source_native_text_absent", 0)
                or reasons.get("source_native_geometry_invalid", 0)
            ):
                _fail(
                    "source_page_modality_invalid",
                    "native-text page requires atoms and no visual artifact",
                )
        elif modality == "native_text_with_visual_guard":
            if (
                not text
                or atom_count < 1
                or geometry_count < 1
                or reasons.get("source_native_geometry_invalid", 0) != geometry_count
                or reasons.get("source_native_text_absent", 0)
            ):
                _fail(
                    "source_page_modality_invalid",
                    "guarded native-text page requires geometry issues and atoms",
                )
        elif modality == "visual_page":
            if (
                text
                or atom_count != 0
                or conflicts != 0
                or page["fallback_required"] is not True
                or (
                    reasons
                    != (
                        {"source_native_geometry_invalid": geometry_count}
                        if geometry_count
                        else {"source_native_text_absent": 1}
                    )
                )
            ):
                _fail(
                    "source_page_modality_invalid",
                    "visual page must represent native-text absence or invalid geometry",
                )
        else:
            _fail("source_page_modality_invalid", "page modality is unsupported")
        if modality != "native_text":
            descriptor = _validate_visual_artifact(visual_artifact)
            role = cast(str, descriptor["artifact_role"])
            if _PAGE_VISUAL_ROLE.fullmatch(role) is None or role in visual_artifacts:
                _fail(
                    "visual_artifact_closure_invalid",
                    f"page visual artifact role is invalid or duplicated: {role}",
                )
            visual_artifacts[role] = descriptor
        if page["fallback_required"] is not bool(reasons):
            _fail(
                "source_page_fallback_invalid",
                "fallback flag/reasons do not agree",
            )
        pages[expected_idx] = page
    page_records: dict[int, list[Mapping[str, Any]]] = {
        page_idx: [] for page_idx in pages
    }
    fallback_counts: Counter[str] = Counter()
    mineru_count = 0
    conflict_count = 0
    previous_atom_key: tuple[int, int] | None = None
    for raw_atom in _list(ledger["atoms"], "source_atoms_invalid"):
        atom = _mapping(raw_atom, "source_atom_invalid")
        if set(atom) != {"disposition", "source"}:
            _fail("source_atom_invalid", "atom fields are not closed")
        source = _mapping(atom["source"], "source_atom_invalid")
        if set(source) != {
            "bbox",
            "char_span",
            "layout_path",
            "order",
            "page_idx",
            "text",
            "text_sha256",
        }:
            _fail("source_atom_invalid", "source atom fields are not closed")
        page_idx = source.get("page_idx")
        order = source.get("order")
        text = source.get("text")
        previous_source = (
            _mapping(
                page_records[page_idx][-1]["source"],
                "source_atom_invalid",
            )
            if _is_index(page_idx)
            and page_idx in page_records
            and page_records[page_idx]
            else None
        )
        if (
            not _is_index(page_idx)
            or page_idx not in pages
            or not _is_index(order)
            or (
                previous_source is not None
                and order <= cast(int, previous_source["order"])
            )
            or not isinstance(text, str)
            or not text
            or source.get("text_sha256") != _digest(text.encode())
            or _layout_path(source.get("layout_path")) is None
        ):
            _fail("source_atom_invalid", "source atom identity is invalid")
        atom_key = (page_idx, order)
        if previous_atom_key is not None and atom_key <= previous_atom_key:
            _fail(
                "source_atom_order_invalid",
                "root atoms are not in strict page-major source order",
            )
        previous_atom_key = atom_key
        _required_bbox(source.get("bbox"), normalized=False)
        page_text = cast(str, pages[page_idx]["text"])
        start, end = _span(source.get("char_span"), len(page_text))
        if page_text[start:end] != text:
            _fail("source_atom_invalid", "source atom span differs from page text")
        disposition = _mapping(
            atom["disposition"],
            "source_disposition_invalid",
        )
        kind = disposition.get("kind")
        if kind == "source_native_fallback":
            if (
                set(disposition) != {"kind", "reason"}
                or disposition.get("reason") not in _FALLBACK_REASONS
                or disposition.get("reason") == "source_order_conflict"
            ):
                _fail(
                    "source_disposition_invalid",
                    "native fallback disposition is invalid",
                )
            fallback_counts[str(disposition["reason"])] += 1
        elif kind == "mineru_carrier":
            if set(disposition) != {
                "carrier",
                "kind",
                "source_order",
            } or disposition.get("source_order") not in {"conflict", "monotonic"}:
                _fail(
                    "source_disposition_invalid",
                    "MinerU disposition is invalid",
                )
            carrier = _mapping(
                disposition["carrier"],
                "source_disposition_invalid",
            )
            if (
                set(carrier) != {"bbox", "order", "page_idx", "selector"}
                or carrier.get("page_idx") != page_idx
                or not _is_index(carrier.get("order"))
            ):
                _fail(
                    "source_disposition_invalid",
                    "MinerU carrier locator is invalid",
                )
            _required_bbox(carrier.get("bbox"), normalized=True)
            _mapping(
                carrier["selector"],
                "selector_shape_invalid",
            )
            mineru_count += 1
            if disposition["source_order"] == "conflict":
                conflict_count += 1
        else:
            _fail("source_disposition_invalid", "unsupported disposition kind")
        page_records[page_idx].append(atom)

    for page_idx, records in page_records.items():
        page = pages[page_idx]
        if len(records) != page["atom_count"]:
            _fail("source_atom_closure_invalid", "page atom count differs")
        atom_orders = [int(record["source"]["order"]) for record in records]
        issue_orders = [int(issue["word_order"]) for issue in page["geometry_issues"]]
        source_orders = sorted(atom_orders + issue_orders)
        if source_orders != list(range(len(source_orders))):
            _fail(
                "source_atom_order_invalid",
                "native atom and geometry-issue orders are not closed",
            )
        if page["modality"] == "visual_page":
            continue
        page_reasons: Counter[str] = Counter(
            {
                "source_native_geometry_invalid": len(page["geometry_issues"]),
            }
            if page["geometry_issues"]
            else {}
        )
        for atom in records:
            disposition = cast(Mapping[str, Any], atom["disposition"])
            if disposition["kind"] == "source_native_fallback":
                page_reasons[str(disposition["reason"])] += 1
        if dict(sorted(page_reasons.items())) != page["fallback_reasons"]:
            _fail(
                "source_page_fallback_invalid",
                "page fallback reasons differ from atom dispositions",
            )

    native_pages = _native_pages_from_ledger(pages, page_records)
    expected_retrieval_runs = _retrieval_run_records(
        native_pages,
        tuple(page_records[index] for index in range(len(page_records))),
        native_structure=native_structure_index,
        table_bboxes_by_page=_table_body_bboxes(
            canonical_content,
            source_pdf_page_count=expected_source_pdf_page_count,
        ),
    )
    raw_retrieval_runs = ledger["retrieval_runs"]
    if (
        not isinstance(raw_retrieval_runs, list)
        or any(
            not isinstance(run, Mapping) or set(run) != _RETRIEVAL_RUN_FIELDS
            for run in raw_retrieval_runs
        )
        or raw_retrieval_runs != expected_retrieval_runs
    ):
        _fail(
            "retrieval_run_closure_invalid",
            "retrieval runs differ from source layout/dispositions",
        )

    resolved_table_roles = (
        resolve_middle_table_roles(
            canonical_content,
            middle_artifact=mineru_middle_artifact,
            source_pages=native_pages,
        )
        if mineru_middle_artifact is not None
        else ()
    )
    expected_role_records = [_table_role_record(role) for role in resolved_table_roles]
    if ledger["table_role_overrides"] != expected_role_records:
        _fail(
            "middle_role_source_mismatch",
            "resolved table roles differ from source PDF evidence",
        )
    expected_carriers = iter_mineru_text_carriers(
        canonical_content,
        table_role_overrides=resolved_table_roles,
    )
    support_summary = _validate_carrier_support(
        ledger["carrier_support"],
        page_records=page_records,
        source_pdf_page_count=expected_source_pdf_page_count,
        expected_carriers=expected_carriers,
        expected_visual_recognitions=expected_visual_recognitions,
        expected_annotations=expected_annotations,
        full_page_visual_artifacts=visual_artifacts,
        visual_occurrence_artifacts=occurrence_artifacts,
        visual_occurrence_pages=frozenset(
            occurrence.page_idx for occurrence in expected_visual_occurrences
        ),
        required_visual_page_support=frozenset(
            page_idx
            for page_idx, page in pages.items()
            if page["modality"] == "visual_page" and not page["geometry_issues"]
        ),
    )
    region_artifacts = cast(
        Mapping[str, Mapping[str, Any]],
        support_summary["visual_artifacts"],
    )
    for role, descriptor in region_artifacts.items():
        if role in visual_artifacts:
            _fail(
                "visual_artifact_closure_invalid",
                f"visual artifact role is duplicated: {role}",
            )
        visual_artifacts[role] = descriptor
    for role, descriptor in occurrence_artifacts.items():
        if role in visual_artifacts:
            _fail(
                "visual_artifact_closure_invalid",
                f"visual artifact role is duplicated: {role}",
            )
        visual_artifacts[role] = descriptor
    if bool(visual_artifacts) is not bool(visual_renderer):
        _fail(
            "visual_renderer_closure_invalid",
            "visual renderer identity must exist exactly with rendered evidence",
        )

    coverage = _mapping(ledger["coverage"], "source_coverage_invalid")
    expected_coverage = {
        "source_atoms": sum(len(records) for records in page_records.values()),
        "mineru_carriers": mineru_count,
        "source_native_fallbacks": sum(fallback_counts.values()),
        "retrieval_runs": len(expected_retrieval_runs),
        "retrieval_run_atoms": sum(
            len(cast(list[int], run["atom_indices"])) for run in expected_retrieval_runs
        ),
        "source_order_conflicts": conflict_count,
        "native_text_pages": sum(
            page["modality"] in {"native_text", "native_text_with_visual_guard"}
            for page in pages.values()
        ),
        "visual_pages": sum(
            page["visual_artifact"] is not None for page in pages.values()
        ),
        "visual_regions": len(region_artifacts),
        "visual_occurrences": len(occurrence_artifacts),
        "mineru_text_carriers": support_summary["mineru_text_carriers"],
        "middle_table_roles": len(resolved_table_roles),
        "native_exact_carriers": support_summary["native_exact_carriers"],
        "visual_bound_carriers": support_summary["visual_bound_carriers"],
        "generated_annotations": support_summary["generated_annotations"],
        "native_geometry_issues": sum(
            len(page["geometry_issues"]) for page in pages.values()
        ),
    }
    if set(coverage) != set(expected_coverage) or coverage != expected_coverage:
        _fail("source_coverage_invalid", "coverage counters do not reconcile")
    if parser_artifacts is not None:
        _validate_visual_manifest_binding(
            parser_artifacts,
            visual_artifacts=visual_artifacts,
            generated_artifacts=cast(
                Mapping[str, Mapping[str, Any]],
                support_summary["generated_artifacts"],
            ),
        )
    return ledger


def validate_mapped_element_bindings(
    value: Mapping[str, Any],
    *,
    elements: Sequence[Mapping[str, Any]],
) -> None:
    """Bind a validated source ledger to the mapped current-IR carriers."""

    for raw_atom in cast(list[Any], value["atoms"]):
        atom = cast(Mapping[str, Any], raw_atom)
        disposition = cast(Mapping[str, Any], atom["disposition"])
        if disposition["kind"] != "mineru_carrier":
            continue
        source = cast(Mapping[str, Any], atom["source"])
        carrier = cast(Mapping[str, Any], disposition["carrier"])
        selector = cast(Mapping[str, Any], carrier["selector"])
        element = _resolve_ir_selector_element(
            elements,
            selector["source_item_index"],
        )
        if element.get("page_idx") != source["page_idx"]:
            _fail(
                "selector_page_mismatch",
                "source atom page differs from mapped IR carrier",
            )
        resolved = resolve_ir_text_selector(elements, selector)
        # Both sides meet in the one canonical comparison space: the raw IR
        # slice may carry a PUA checkbox glyph that the atom (or vice versa)
        # normalizes away, and content identity is what the selector proves.
        if comparison_text(cast(str, source["text"])) != comparison_text(resolved):
            _fail(
                "selector_text_mismatch",
                f"source atom differs from mapped IR selector: {resolved!r}",
            )

    for raw_record in cast(list[Any], value["carrier_support"]):
        record = cast(Mapping[str, Any], raw_record)
        selector = cast(Mapping[str, Any], record["selector"])
        element = _resolve_ir_selector_element(
            elements,
            selector["source_item_index"],
        )
        if element.get("page_idx") != record["page_idx"]:
            _fail(
                "selector_page_mismatch",
                "carrier support page differs from mapped IR carrier",
            )
        _, selector_end = _validate_full_selector(selector)
        resolved = resolve_ir_text_selector(elements, selector)
        if not resolved or len(resolved) != selector_end:
            _fail(
                "selector_text_mismatch",
                "carrier support selector is not the complete mapped field",
            )

    for raw_record in cast(list[Any], value["visual_occurrences"]):
        record = cast(Mapping[str, Any], raw_record)
        element = _resolve_ir_selector_element(
            elements,
            record["source_item_index"],
        )
        if element.get("page_idx") != record["page_idx"]:
            _fail(
                "selector_page_mismatch",
                "visual occurrence page differs from mapped IR carrier",
            )


def carrier_source_support_index(
    value: Mapping[str, Any],
) -> Mapping[tuple[int, str, int | None], CarrierSourceSupport]:
    """Expose source-backed carriers from an already validated ledger."""

    result: dict[tuple[int, str, int | None], CarrierSourceSupport] = {}
    for raw_record in cast(list[Any], value["carrier_support"]):
        record = cast(Mapping[str, Any], raw_record)
        selector = cast(Mapping[str, Any], record["selector"])
        support = cast(Mapping[str, Any], record["support"])
        kind = cast(str, support["kind"])
        if kind == "generated_annotation":
            continue
        key = _selector_key(selector)
        artifact = support.get("artifact")
        artifact_mapping = (
            cast(Mapping[str, Any], artifact) if isinstance(artifact, Mapping) else None
        )
        result[key] = CarrierSourceSupport(
            source_item_index=key[0],
            field=key[1],
            index=key[2],
            page_idx=cast(int, record["page_idx"]),
            bbox=cast(BBox, tuple(record["bbox"])),
            kind=kind,
            artifact_role=(
                cast(str, artifact_mapping["artifact_role"])
                if artifact_mapping is not None
                else None
            ),
            artifact_sha256=(
                cast(str, artifact_mapping["sha256"])
                if artifact_mapping is not None
                else None
            ),
        )
    return MappingProxyType(result)


def _middle_artifact_record(
    artifact: MinerUMiddleArtifact | None,
) -> dict[str, Any] | None:
    if artifact is None:
        return None
    _require_sha(artifact.sha256, "middle_artifact.sha256")
    if (
        not artifact.version
        or not artifact.backend
        or not _is_index(artifact.page_count)
        or artifact.page_count < 1
    ):
        _fail("middle_artifact_invalid", "middle artifact identity is invalid")
    return {
        "role": "middle",
        "sha256": artifact.sha256,
        "version": artifact.version,
        "backend": artifact.backend,
        "page_count": artifact.page_count,
    }


def _table_role_record(role: ResolvedTableRole) -> dict[str, Any]:
    return {
        "source_item_index": role.source_item_index,
        "page_idx": role.page_idx,
        "parent_bbox": list(role.parent_bbox),
        "field": role.field,
        "index": role.index,
        "bbox": list(role.bbox),
        "provider_deleted": role.provider_deleted,
        "text": role.text,
        "text_sha256": _digest(role.text.encode()),
    }


def _native_pages_from_ledger(
    pages: Mapping[int, Mapping[str, Any]],
    page_records: Mapping[int, Sequence[Mapping[str, Any]]],
) -> tuple[NativeTextPage, ...]:
    output: list[NativeTextPage] = []
    for page_idx in range(len(pages)):
        page = pages[page_idx]
        atoms: list[NativeTextAtom] = []
        for record in page_records[page_idx]:
            source = cast(Mapping[str, Any], record["source"])
            atoms.append(
                NativeTextAtom(
                    page_idx=page_idx,
                    order=cast(int, source["order"]),
                    bbox=cast(BBox, tuple(source["bbox"])),
                    char_span=cast(tuple[int, int], tuple(source["char_span"])),
                    text=cast(str, source["text"]),
                    layout=NativeTextLayoutRef(
                        *cast(
                            tuple[int, int, int, int],
                            tuple(source["layout_path"]),
                        )
                    ),
                )
            )
        issues: list[NativeTextGeometryIssue] = []
        for issue in cast(list[Mapping[str, Any]], page["geometry_issues"]):
            raw_bbox = issue["raw_bbox"]
            issues.append(
                NativeTextGeometryIssue(
                    page_idx=page_idx,
                    word_order=cast(int, issue["word_order"]),
                    text=cast(str, issue["text"]),
                    raw_bbox=(
                        cast(BBox, tuple(raw_bbox))
                        if isinstance(raw_bbox, list)
                        else None
                    ),
                    reason=cast(str, issue["reason"]),
                )
            )
        output.append(
            NativeTextPage(
                page_idx=page_idx,
                width=cast(float, page["width"]),
                height=cast(float, page["height"]),
                text=cast(str, page["text"]),
                atoms=tuple(atoms),
                geometry_issues=tuple(issues),
            )
        )
    return tuple(output)


def source_visual_artifact_descriptors(
    value: Mapping[str, Any],
) -> Mapping[str, Mapping[str, Any]]:
    """Expose the exact page and carrier visuals from a validated ledger."""

    result: dict[str, Mapping[str, Any]] = {}

    def add(raw_descriptor: object) -> None:
        descriptor = _validate_visual_artifact(raw_descriptor)
        role = cast(str, descriptor["artifact_role"])
        prior = result.setdefault(role, descriptor)
        if prior != descriptor:
            _fail(
                "visual_artifact_closure_invalid",
                f"visual artifact role has conflicting descriptors: {role}",
            )

    for raw_page in _list(value.get("pages"), "source_pages_invalid"):
        page = _mapping(raw_page, "source_page_invalid")
        if page.get("visual_artifact") is not None:
            add(page["visual_artifact"])
    for raw_record in _list(
        value.get("carrier_support"),
        "carrier_support_invalid",
    ):
        record = _mapping(raw_record, "carrier_support_invalid")
        support = _mapping(record.get("support"), "carrier_support_invalid")
        if support.get("kind") == "visual_bound":
            add(support.get("artifact"))
    for raw_record in _list(
        value.get("visual_occurrences"),
        "visual_occurrences_invalid",
    ):
        record = _mapping(raw_record, "visual_occurrence_invalid")
        add(record.get("artifact"))
    return MappingProxyType(result)


def resolve_ir_text_selector(
    elements: Sequence[Mapping[str, Any]],
    selector: Mapping[str, Any],
) -> str:
    required = {"char_span", "field", "projection", "source_item_index", "value_sha256"}
    if not required <= set(selector) or set(selector) - required - {"index"}:
        _fail("selector_shape_invalid", "selector fields are not closed")
    source_index = selector["source_item_index"]
    element = _resolve_ir_selector_element(elements, source_index)
    if selector["projection"] != TEXT_PROJECTION:
        _fail("selector_projection_unsupported", "projection is unsupported")
    field = selector["field"]
    if not isinstance(field, str):
        _fail("selector_field_invalid", "field must be text")
    value = _ir_value(element, field, selector.get("index"))
    if selector["value_sha256"] != _digest(value.encode()):
        _fail("selector_value_hash_mismatch", "mapped IR field drifted")
    start, end = _span(selector["char_span"], len(value))
    return value[start:end]


def _resolve_ir_selector_element(
    elements: Sequence[Mapping[str, Any]],
    source_index: object,
) -> Mapping[str, Any]:
    if not _is_index(source_index):
        _fail("selector_source_invalid", "source_item_index is invalid")
    matches = [
        item for item in elements if item.get("source_item_index") == source_index
    ]
    if len(matches) != 1:
        _fail("selector_source_invalid", "source item is absent or ambiguous")
    return matches[0]


def _visual_pages_by_index(
    visual_pages: Sequence[VisualPageEvidence],
    *,
    source_pages: Sequence[NativeTextPage],
) -> dict[int, VisualPageEvidence]:
    expected = set(visual_guard_page_indices(source_pages))
    by_page: dict[int, VisualPageEvidence] = {}
    for descriptor in visual_pages:
        if (
            not _is_index(descriptor.page_idx)
            or descriptor.page_idx in by_page
            or descriptor.page_idx not in expected
            or descriptor.bbox is not None
        ):
            _fail(
                "visual_artifact_closure_invalid",
                "visual evidence page is duplicated or needs no visual guard",
            )
        by_page[descriptor.page_idx] = descriptor
    if set(by_page) != expected:
        missing = sorted(expected - set(by_page))
        extra = sorted(set(by_page) - expected)
        _fail(
            "visual_artifact_closure_invalid",
            f"visual evidence pages differ; missing={missing}, extra={extra}",
        )
    return by_page


def _visual_regions_by_role(
    visual_regions: Sequence[VisualPageEvidence],
) -> dict[str, VisualPageEvidence]:
    by_role: dict[str, VisualPageEvidence] = {}
    for descriptor in visual_regions:
        if (
            descriptor.bbox is None
            or _BBOX_VISUAL_ROLE.fullmatch(descriptor.artifact_role) is None
            or descriptor.artifact_role in by_role
        ):
            _fail(
                "visual_artifact_closure_invalid",
                "carrier visual region is not a unique bbox artifact",
            )
        _required_bbox(descriptor.bbox, normalized=True)
        by_role[descriptor.artifact_role] = descriptor
    return by_role


def _visual_occurrence_records(
    occurrences: Sequence[MinerUVisualOccurrence],
    artifacts: Sequence[VisualPageEvidence],
) -> tuple[list[dict[str, Any]], dict[int, VisualPageEvidence]]:
    expected = {item.source_item_index: item for item in occurrences}
    by_source: dict[int, VisualPageEvidence] = {}
    for descriptor in artifacts:
        role = descriptor.artifact_role
        match = _OCCURRENCE_VISUAL_ROLE.fullmatch(role)
        source_item_index = int(role.rsplit("_", 1)[-1]) if match is not None else None
        if source_item_index is None:
            _fail(
                "visual_occurrence_artifact_invalid",
                "visual occurrence crop has an invalid role",
            )
        occurrence = expected.get(source_item_index)
        if (
            occurrence is None
            or source_item_index in by_source
            or descriptor.page_idx != occurrence.page_idx
            or descriptor.bbox != occurrence.bbox
        ):
            _fail(
                "visual_occurrence_artifact_invalid",
                "visual occurrence crop differs from its provider occurrence",
            )
        by_source[source_item_index] = descriptor
    if set(by_source) != set(expected):
        _fail(
            "visual_occurrence_closure_invalid",
            "visual occurrence crops do not close over image/chart items",
        )
    records = [
        {
            "source_item_index": occurrence.source_item_index,
            "source_item_sha256": occurrence.source_item_sha256,
            "raw_kind": occurrence.raw_kind,
            "page_idx": occurrence.page_idx,
            "bbox": list(occurrence.bbox),
            "artifact": _visual_artifact_descriptor(
                by_source[occurrence.source_item_index]
            ),
        }
        for occurrence in occurrences
    ]
    return records, by_source


def _validate_visual_occurrence_records(
    value: object,
    *,
    expected_occurrences: Sequence[MinerUVisualOccurrence],
) -> dict[str, Mapping[str, Any]]:
    records = _list(value, "visual_occurrences_invalid")
    if len(records) != len(expected_occurrences):
        _fail(
            "visual_occurrence_closure_invalid",
            "visual occurrence record count differs from content_list",
        )
    artifacts: dict[str, Mapping[str, Any]] = {}
    for raw_record, expected in zip(records, expected_occurrences, strict=True):
        record = _mapping(raw_record, "visual_occurrence_invalid")
        if set(record) != _VISUAL_OCCURRENCE_FIELDS or (
            record.get("source_item_index") != expected.source_item_index
            or record.get("source_item_sha256") != expected.source_item_sha256
            or record.get("raw_kind") != expected.raw_kind
            or record.get("page_idx") != expected.page_idx
            or _required_bbox(record.get("bbox"), normalized=True) != expected.bbox
        ):
            _fail(
                "visual_occurrence_identity_mismatch",
                "visual occurrence identity/order differs from content_list",
            )
        artifact = _validate_visual_artifact(record.get("artifact"))
        role = cast(str, artifact["artifact_role"])
        if (
            role != f"source_visual_occurrence_{expected.source_item_index:06d}"
            or role in artifacts
        ):
            _fail(
                "visual_occurrence_artifact_invalid",
                "visual occurrence artifact role is invalid or duplicated",
            )
        artifacts[role] = artifact
    return artifacts


def _visual_artifact_descriptor(
    visual: VisualPageEvidence,
) -> dict[str, Any]:
    descriptor = {
        "artifact_role": visual.artifact_role,
        "sha256": visual.sha256,
        "size_bytes": visual.size_bytes,
        "pixel_width": visual.pixel_width,
        "pixel_height": visual.pixel_height,
        "media_type": visual.media_type,
    }
    _validate_visual_artifact(descriptor)
    return descriptor


def _visual_renderer_profile(
    visual_pages: Sequence[VisualPageEvidence],
) -> dict[str, Any] | None:
    if not visual_pages:
        return None
    first = visual_pages[0]
    if any(
        item.renderer != first.renderer
        or item.render_options != first.render_options
        or item.png_options != first.png_options
        for item in visual_pages[1:]
    ):
        _fail(
            "visual_renderer_closure_invalid",
            "visual pages were not produced by one renderer profile",
        )
    profile: dict[str, Any] = {
        "identity": _json_record(asdict(first.renderer)),
        "render_options": _json_record(asdict(first.render_options)),
        "png_options": _json_record(asdict(first.png_options)),
    }
    profile["profile_sha256"] = _digest(_canonical_json(profile))
    _validate_visual_renderer_profile(profile)
    return profile


def _validate_visual_artifact(value: object) -> Mapping[str, Any]:
    descriptor = _mapping(value, "visual_artifact_invalid")
    if set(descriptor) != _VISUAL_ARTIFACT_FIELDS:
        _fail("visual_artifact_invalid", "visual artifact fields are not closed")
    role = descriptor.get("artifact_role")
    sha256 = descriptor.get("sha256")
    size_bytes = descriptor.get("size_bytes")
    pixel_width = descriptor.get("pixel_width")
    pixel_height = descriptor.get("pixel_height")
    if (
        not isinstance(role, str)
        or (
            _PAGE_VISUAL_ROLE.fullmatch(role) is None
            and _BBOX_VISUAL_ROLE.fullmatch(role) is None
            and _OCCURRENCE_VISUAL_ROLE.fullmatch(role) is None
        )
        or not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
        or not _is_index(size_bytes)
        or size_bytes < 1
        or not _is_index(pixel_width)
        or pixel_width < 1
        or not _is_index(pixel_height)
        or pixel_height < 1
        or descriptor.get("media_type") != "image/png"
    ):
        _fail("visual_artifact_invalid", "visual artifact descriptor is invalid")
    return descriptor


def _validate_carrier_support(
    value: object,
    *,
    page_records: Mapping[int, Sequence[Mapping[str, Any]]],
    source_pdf_page_count: int,
    expected_carriers: Sequence[MinerUTextCarrier],
    expected_visual_recognitions: Sequence[MinerUTextCarrier],
    expected_annotations: Sequence[MinerUTextCarrier],
    full_page_visual_artifacts: Mapping[str, Mapping[str, Any]],
    visual_occurrence_artifacts: Mapping[str, Mapping[str, Any]],
    visual_occurrence_pages: frozenset[int],
    required_visual_page_support: frozenset[int],
) -> dict[str, Any]:
    expected_source = {_carrier_key(carrier): carrier for carrier in expected_carriers}
    expected_visual = {
        _carrier_key(carrier): carrier for carrier in expected_visual_recognitions
    }
    expected_generated = {
        _carrier_key(carrier): carrier for carrier in expected_annotations
    }
    if (
        set(expected_source) & set(expected_visual)
        or set(expected_source) & set(expected_generated)
        or set(expected_visual) & set(expected_generated)
    ):
        _fail(
            "carrier_support_closure_invalid",
            "source, visual-recognition, and generated carrier identities overlap",
        )
    expected = {**expected_source, **expected_visual, **expected_generated}
    seen: set[tuple[int, str, int | None]] = set()
    native_count = 0
    visual_count = 0
    components: dict[
        str,
        tuple[Mapping[str, Any], int, BBox],
    ] = {}
    visual_requests: list[VisualRegionRequest] = []
    full_page_support: set[int] = set()
    generated_artifacts: dict[str, Mapping[str, Any]] = {}

    for raw_record in _list(value, "carrier_support_invalid"):
        record = _mapping(raw_record, "carrier_support_invalid")
        if set(record) != _CARRIER_SUPPORT_FIELDS:
            _fail(
                "carrier_support_invalid",
                "carrier support fields are not closed",
            )
        page_idx = record.get("page_idx")
        if (
            not _is_index(page_idx)
            or page_idx >= source_pdf_page_count
            or page_idx not in page_records
        ):
            _fail("carrier_support_invalid", "carrier page is invalid")
        bbox = _required_bbox(record.get("bbox"), normalized=True)
        selector = _mapping(record.get("selector"), "selector_shape_invalid")
        key, _selector_end = _validate_full_selector(selector)
        carrier = expected.get(key)
        if key in seen or carrier is None:
            _fail(
                "carrier_support_invalid",
                "carrier selector is duplicated or absent from MinerU",
            )
        seen.add(key)
        if (
            selector != _full_selector(carrier)
            or page_idx != carrier.page_idx
            or bbox != carrier.bbox
        ):
            _fail(
                "carrier_support_identity_mismatch",
                "carrier support differs from MinerU artifact",
            )
        support = _mapping(record.get("support"), "carrier_support_invalid")
        if key in expected_generated:
            if (
                set(support) != {"artifact", "kind"}
                or support.get("kind") != "generated_annotation"
            ):
                _fail(
                    "generated_annotation_misclassified",
                    "image description must remain a generated annotation",
                )
            artifact = _validate_generated_artifact(support["artifact"])
            role = cast(str, artifact["artifact_role"])
            if role != f"evidence_image_{key[0]:06d}":
                _fail(
                    "generated_artifact_invalid",
                    "generated artifact does not match its source item",
                )
            prior_generated = generated_artifacts.setdefault(role, artifact)
            if prior_generated != artifact:
                _fail(
                    "generated_artifact_invalid",
                    "generated artifact identity is inconsistent",
                )
            continue

        if key in expected_visual:
            if (
                set(support) != {"artifact", "component_bbox", "kind"}
                or support.get("kind") != "visual_bound"
            ):
                _fail(
                    "visual_recognition_support_invalid",
                    "chart recognition requires its exact occurrence crop",
                )
            component_bbox = _required_bbox(
                support["component_bbox"],
                normalized=True,
            )
            role = f"source_visual_occurrence_{key[0]:06d}"
            artifact = _validate_visual_artifact(support["artifact"])
            if component_bbox != bbox or artifact != visual_occurrence_artifacts.get(
                role
            ):
                _fail(
                    "visual_recognition_support_invalid",
                    "chart recognition differs from its occurrence crop",
                )
            visual_count += 1
            continue

        native_orders = _native_orders_for_selector(
            selector,
            page_idx=page_idx,
            page_records=page_records[page_idx],
        )
        if native_orders is not None:
            if support != {
                "kind": "native_exact",
                "source_atom_orders": list(native_orders),
            }:
                _fail(
                    "carrier_native_support_invalid",
                    "native support does not close the complete carrier",
                )
            native_count += 1
            continue

        if (
            set(support) != {"artifact", "component_bbox", "kind"}
            or support.get("kind") != "visual_bound"
        ):
            _fail(
                "carrier_visual_support_invalid",
                "non-native carrier requires closed visual support",
            )
        component_bbox = _required_bbox(
            support["component_bbox"],
            normalized=True,
        )
        if not _bbox_contains(component_bbox, bbox):
            _fail(
                "carrier_visual_support_invalid",
                "visual component does not contain its carrier bbox",
            )
        artifact = _validate_visual_artifact(support["artifact"])
        role = cast(str, artifact["artifact_role"])
        full_page_role = f"source_page_visual_{page_idx + 1:06d}"
        full_page_artifact = full_page_visual_artifacts.get(full_page_role)
        if full_page_artifact is not None:
            if (
                role != full_page_role
                or artifact != full_page_artifact
                or component_bbox != (0.0, 0.0, 1000.0, 1000.0)
            ):
                _fail(
                    "carrier_visual_support_invalid",
                    "guarded page carrier must reuse its full-page visual",
                )
            full_page_support.add(page_idx)
            visual_count += 1
            continue
        if (
            _BBOX_VISUAL_ROLE.fullmatch(role) is None
            or int(role.split("_")[3]) != page_idx + 1
        ):
            _fail(
                "carrier_visual_support_invalid",
                "visual component role does not match its source page",
            )
        prior = components.setdefault(
            role,
            (artifact, page_idx, component_bbox),
        )
        if prior != (artifact, page_idx, component_bbox):
            _fail(
                "carrier_visual_support_invalid",
                "visual component identity is inconsistent",
            )
        visual_requests.append(VisualRegionRequest(page_idx, bbox))
        visual_count += 1

    if seen != set(expected):
        _fail(
            "carrier_support_closure_invalid",
            "carrier support does not close the MinerU typed fields",
        )
    supported_visual_pages = full_page_support | set(visual_occurrence_pages)
    if not required_visual_page_support.issubset(supported_visual_pages):
        missing = sorted(required_visual_page_support - supported_visual_pages)
        _fail(
            "visual_page_search_carrier_missing",
            f"visual-only source pages lack a searchable carrier: {missing}",
        )

    expected_components = merged_visual_region_components(visual_requests)
    expected_roles: dict[tuple[int, BBox], str] = {}
    page_counts: Counter[int] = Counter()
    for component in expected_components:
        component_idx = page_counts[component.page_idx]
        page_counts[component.page_idx] += 1
        expected_roles[(component.page_idx, component.bbox)] = (
            f"source_bbox_visual_{component.page_idx + 1:06d}_{component_idx + 1:06d}"
        )
    if {
        (page_idx, component_bbox): role
        for role, (_, page_idx, component_bbox) in components.items()
    } != expected_roles:
        _fail(
            "carrier_visual_component_invalid",
            "visual artifacts are not the exact merged carrier bbox components",
        )

    return {
        "mineru_text_carriers": len(expected_source) + len(expected_visual),
        "native_exact_carriers": native_count,
        "visual_bound_carriers": visual_count,
        "generated_annotations": len(expected_generated),
        "visual_artifacts": {role: values[0] for role, values in components.items()},
        "generated_artifacts": generated_artifacts,
    }


def _validate_full_selector(
    selector: Mapping[str, Any],
) -> tuple[tuple[int, str, int | None], int]:
    required = {"char_span", "field", "projection", "source_item_index", "value_sha256"}
    if (
        not required <= set(selector)
        or set(selector) - required - {"index"}
        or selector.get("projection") != TEXT_PROJECTION
        or not isinstance(selector.get("value_sha256"), str)
        or _SHA256.fullmatch(cast(str, selector["value_sha256"])) is None
    ):
        _fail("selector_shape_invalid", "carrier selector fields are invalid")
    start, end = _span(selector["char_span"])
    if start != 0:
        _fail("selector_shape_invalid", "carrier selector must cover the full field")
    return _selector_key(selector), end


def _native_orders_for_selector(
    selector: Mapping[str, Any],
    *,
    page_idx: int,
    page_records: Sequence[Mapping[str, Any]],
) -> tuple[int, ...] | None:
    key = _selector_key(selector)
    _, expected_end = _span(selector["char_span"])
    matches: list[tuple[int, int, int]] = []
    for record in page_records:
        disposition = cast(Mapping[str, Any], record["disposition"])
        if disposition.get("kind") != "mineru_carrier":
            continue
        locator = cast(Mapping[str, Any], disposition["carrier"])
        if locator.get("page_idx") != page_idx:
            continue
        atom_selector = cast(Mapping[str, Any], locator["selector"])
        if _selector_key(atom_selector) != key:
            continue
        start, end = _span(atom_selector["char_span"], expected_end)
        source = cast(Mapping[str, Any], record["source"])
        matches.append((start, end, cast(int, source["order"])))
    return _closed_span_orders(matches, expected_end)


def _validate_generated_artifact(value: object) -> Mapping[str, Any]:
    artifact = _mapping(value, "generated_artifact_invalid")
    if set(artifact) != _GENERATED_ARTIFACT_FIELDS:
        _fail(
            "generated_artifact_invalid",
            "generated artifact fields are not closed",
        )
    if (
        not isinstance(artifact.get("artifact_role"), str)
        or _IMAGE_ARTIFACT_ROLE.fullmatch(cast(str, artifact["artifact_role"])) is None
        or not isinstance(artifact.get("sha256"), str)
        or _SHA256.fullmatch(cast(str, artifact["sha256"])) is None
        or not _positive_int(artifact.get("size_bytes"))
    ):
        _fail("generated_artifact_invalid", "generated artifact is invalid")
    return artifact


def _validate_geometry_issues(
    value: object,
    *,
    page_idx: int,
) -> list[Mapping[str, Any]]:
    issues = [
        _mapping(item, "source_geometry_issue_invalid")
        for item in _list(value, "source_geometry_issue_invalid")
    ]
    previous_order = -1
    for issue in issues:
        if set(issue) != _GEOMETRY_ISSUE_FIELDS:
            _fail(
                "source_geometry_issue_invalid",
                "geometry issue fields are not closed",
            )
        order = issue.get("word_order")
        text = issue.get("text")
        reason = issue.get("reason")
        raw_bbox = issue.get("raw_bbox")
        if (
            issue.get("page_idx") != page_idx
            or not _is_index(order)
            or order <= previous_order
            or not isinstance(text, str)
            or not text
            or issue.get("text_sha256") != _digest(text.encode())
            or reason not in {"bbox_missing_or_non_finite", "bbox_non_positive_extent"}
        ):
            _fail(
                "source_geometry_issue_invalid",
                "geometry issue identity/text/order is invalid",
            )
        previous_order = order
        if reason == "bbox_missing_or_non_finite":
            if raw_bbox is not None:
                _fail(
                    "source_geometry_issue_invalid",
                    "missing/non-finite geometry must not publish a raw bbox",
                )
            continue
        if (
            not isinstance(raw_bbox, list)
            or len(raw_bbox) != 4
            or any(
                isinstance(item, bool) or not isinstance(item, (int, float))
                for item in raw_bbox
            )
        ):
            _fail(
                "source_geometry_issue_invalid",
                "non-positive geometry requires four finite coordinates",
            )
        box = [float(item) for item in raw_bbox]
        if not all(math.isfinite(item) for item in box) or (
            box[0] < box[2] and box[1] < box[3]
        ):
            _fail(
                "source_geometry_issue_invalid",
                "raw bbox does not demonstrate non-positive extent",
            )
    return issues


def _validate_visual_renderer_profile(value: object) -> None:
    profile = _mapping(value, "visual_renderer_invalid")
    if set(profile) != _VISUAL_RENDERER_FIELDS:
        _fail("visual_renderer_invalid", "visual renderer fields are not closed")
    identity = _closed_record(
        profile.get("identity"),
        fields=RENDERER_IDENTITY_FIELDS,
        reason="visual_renderer_invalid",
    )
    render_options = _closed_record(
        profile.get("render_options"),
        fields=RENDER_OPTIONS_FIELDS,
        reason="visual_renderer_invalid",
    )
    png_options = _closed_record(
        profile.get("png_options"),
        fields=PNG_OPTIONS_FIELDS,
        reason="visual_renderer_invalid",
    )
    if not all(
        isinstance(identity.get(field), str) and identity[field]
        for field in ("engine", "engine_version", "library", "library_version")
    ) or not (
        isinstance(identity.get("engine_flags"), list)
        and all(isinstance(flag, str) for flag in identity["engine_flags"])
    ):
        _fail("visual_renderer_invalid", "visual renderer identity is invalid")
    if (
        not _positive_int(render_options.get("dpi"))
        or not _positive_int(render_options.get("scale_numerator"))
        or not _positive_int(render_options.get("scale_denominator"))
        or not _fixed_int_list(render_options.get("crop"), length=4)
        or not _fixed_int_list(render_options.get("fill_color"), length=4)
        or not _fixed_int_list(png_options.get("dpi"), length=2, positive=True)
    ):
        _fail("visual_renderer_invalid", "visual renderer geometry is invalid")
    expected_profile = {
        "identity": identity,
        "render_options": render_options,
        "png_options": png_options,
    }
    if profile.get("profile_sha256") != _digest(_canonical_json(expected_profile)):
        _fail("visual_renderer_invalid", "visual renderer profile hash differs")


def _validate_visual_manifest_binding(
    parser_artifacts: Mapping[str, Any],
    *,
    visual_artifacts: Mapping[str, Mapping[str, Any]],
    generated_artifacts: Mapping[str, Mapping[str, Any]],
) -> None:
    files = parser_artifacts.get("files")
    if not isinstance(files, Mapping):
        _fail("visual_manifest_invalid", "parser artifact manifest is invalid")
    manifest_roles = {
        role
        for role in files
        if isinstance(role, str)
        and (
            _PAGE_VISUAL_ROLE.fullmatch(role) is not None
            or _BBOX_VISUAL_ROLE.fullmatch(role) is not None
            or _OCCURRENCE_VISUAL_ROLE.fullmatch(role) is not None
        )
    }
    if manifest_roles != set(visual_artifacts):
        _fail(
            "visual_manifest_closure_invalid",
            "visual roles differ between source ledger and parser manifest",
        )
    for role, visual in visual_artifacts.items():
        manifest = _mapping(files[role], "visual_manifest_invalid")
        if (
            manifest.get("availability") != "present"
            or manifest.get("sha256") != visual["sha256"]
            or manifest.get("size_bytes") != visual["size_bytes"]
        ):
            _fail(
                "visual_manifest_identity_mismatch",
                f"visual artifact identity differs for role {role}",
            )
    for role, generated in generated_artifacts.items():
        manifest = _mapping(files.get(role), "visual_manifest_invalid")
        if (
            manifest.get("availability") != "present"
            or manifest.get("sha256") != generated["sha256"]
            or manifest.get("size_bytes") != generated["size_bytes"]
        ):
            _fail(
                "generated_manifest_identity_mismatch",
                f"generated annotation source artifact differs for role {role}",
            )


def _json_record(value: Mapping[str, Any]) -> dict[str, Any]:
    decoded = json.loads(json.dumps(value, separators=(",", ":"), sort_keys=True))
    if not isinstance(decoded, dict):
        _fail("visual_renderer_invalid", "renderer profile is not an object")
    return cast(dict[str, Any], decoded)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _closed_record(
    value: object,
    *,
    fields: frozenset[str],
    reason: str,
) -> Mapping[str, Any]:
    record = _mapping(value, reason)
    if set(record) != fields:
        _fail(reason, "renderer profile fields are not closed")
    return record


def _positive_int(value: object) -> TypeGuard[int]:
    return _is_index(value) and value > 0


def _fixed_int_list(
    value: object,
    *,
    length: int,
    positive: bool = False,
) -> bool:
    return (
        isinstance(value, list)
        and len(value) == length
        and all(_positive_int(item) if positive else _is_index(item) for item in value)
    )


def _carrier_reconciliation(
    content_list: Sequence[Mapping[str, Any]],
    *,
    source_pages: Sequence[NativeTextPage],
    source_pdf_page_count: int,
    table_role_overrides: Sequence[ResolvedTableRole] = (),
) -> tuple[tuple[MinerUTextCarrier, ...], list[list[dict[str, Any]]]]:
    carriers = iter_mineru_text_carriers(
        content_list,
        table_role_overrides=table_role_overrides,
    )
    by_page: dict[int, list[MinerUTextCarrier]] = {}
    for carrier in carriers:
        if carrier.page_idx is not None and carrier.page_idx < source_pdf_page_count:
            by_page.setdefault(carrier.page_idx, []).append(carrier)
    records = [
        (_reconcile_page(page, by_page.get(page.page_idx, ())) if page.atoms else [])
        for page in source_pages
    ]
    return carriers, records


def _carrier_key(
    carrier: MinerUTextCarrier,
) -> tuple[int, str, int | None]:
    return carrier.source_item_index, carrier.field, carrier.index


def _selector_key(
    selector: Mapping[str, Any],
) -> tuple[int, str, int | None]:
    source_index = selector.get("source_item_index")
    field = selector.get("field")
    index = selector.get("index")
    if (
        not _is_index(source_index)
        or not isinstance(field, str)
        or (index is not None and not _is_index(index))
    ):
        _fail("selector_shape_invalid", "selector identity is invalid")
    return source_index, field, index


def _native_atom_orders(
    carrier: MinerUTextCarrier,
    records_by_page: Sequence[Sequence[Mapping[str, Any]]],
) -> tuple[int, ...] | None:
    if carrier.page_idx is None or carrier.page_idx >= len(records_by_page):
        return None
    return _native_orders_for_selector(
        _full_selector(carrier),
        page_idx=carrier.page_idx,
        page_records=records_by_page[carrier.page_idx],
    )


def _closed_span_orders(
    matches: Sequence[tuple[int, int, int]],
    expected_end: int,
) -> tuple[int, ...] | None:
    cursor = 0
    orders: list[int] = []
    for start, end, order in sorted(matches):
        if start != cursor:
            return None
        cursor = end
        orders.append(order)
    return tuple(orders) if cursor == expected_end else None


def _required_carrier_locator(
    carrier: MinerUTextCarrier,
    *,
    source_pdf_page_count: int,
) -> tuple[int, BBox]:
    if (
        carrier.page_idx is None
        or carrier.page_idx >= source_pdf_page_count
        or carrier.bbox is None
    ):
        _fail(
            "mineru_carrier_unbound",
            "typed MinerU text requires a valid source page and bbox",
        )
    return carrier.page_idx, carrier.bbox


def _carrier_support_records(
    carriers: Sequence[MinerUTextCarrier],
    *,
    visual_recognitions: Sequence[MinerUTextCarrier],
    generated_annotations: Sequence[MinerUTextCarrier],
    records_by_page: Sequence[Sequence[Mapping[str, Any]]],
    source_pdf_page_count: int,
    full_page_visuals: Mapping[int, VisualPageEvidence],
    required_visual_page_support: frozenset[int],
    visual_regions: Mapping[str, VisualPageEvidence],
    visual_occurrences: Mapping[int, VisualPageEvidence],
    generated_annotation_artifacts: Mapping[str, Path],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    used_regions: set[str] = set()
    full_page_support: set[int] = set()
    for carrier in carriers:
        page_idx, bbox = _required_carrier_locator(
            carrier,
            source_pdf_page_count=source_pdf_page_count,
        )
        native_orders = _native_atom_orders(carrier, records_by_page)
        if native_orders is not None:
            support: dict[str, Any] = {
                "kind": "native_exact",
                "source_atom_orders": list(native_orders),
            }
        elif (full_page := full_page_visuals.get(page_idx)) is not None:
            full_page_support.add(page_idx)
            support = {
                "kind": "visual_bound",
                "component_bbox": [0.0, 0.0, 1000.0, 1000.0],
                "artifact": _visual_artifact_descriptor(full_page),
            }
        else:
            matches = [
                descriptor
                for descriptor in visual_regions.values()
                if descriptor.page_idx == page_idx
                and descriptor.bbox is not None
                and _bbox_contains(descriptor.bbox, bbox)
            ]
            if len(matches) != 1:
                _fail(
                    "mineru_carrier_unbound",
                    "typed MinerU text has no unique rendered bbox component",
                )
            descriptor = matches[0]
            used_regions.add(descriptor.artifact_role)
            support = {
                "kind": "visual_bound",
                "component_bbox": list(cast(BBox, descriptor.bbox)),
                "artifact": _visual_artifact_descriptor(descriptor),
            }
        result.append(
            {
                "selector": _full_selector(carrier),
                "page_idx": page_idx,
                "bbox": list(bbox),
                "support": support,
            }
        )
    if used_regions != set(visual_regions):
        _fail(
            "visual_artifact_closure_invalid",
            "carrier visual regions contain unreferenced artifacts",
        )
    occurrence_pages = {
        descriptor.page_idx for descriptor in visual_occurrences.values()
    }
    supported_visual_pages = full_page_support | occurrence_pages
    if not required_visual_page_support.issubset(supported_visual_pages):
        missing = sorted(required_visual_page_support - supported_visual_pages)
        _fail(
            "visual_page_search_carrier_missing",
            f"visual-only source pages lack a searchable carrier: {missing}",
        )

    for recognition in visual_recognitions:
        page_idx, bbox = _required_carrier_locator(
            recognition,
            source_pdf_page_count=source_pdf_page_count,
        )
        occurrence_visual = visual_occurrences.get(recognition.source_item_index)
        if (
            occurrence_visual is None
            or occurrence_visual.page_idx != page_idx
            or occurrence_visual.bbox != bbox
        ):
            _fail(
                "visual_recognition_unbound",
                "chart recognition lacks its exact source occurrence crop",
            )
        result.append(
            {
                "selector": _full_selector(recognition),
                "page_idx": page_idx,
                "bbox": list(bbox),
                "support": {
                    "kind": "visual_bound",
                    "component_bbox": list(bbox),
                    "artifact": _visual_artifact_descriptor(occurrence_visual),
                },
            }
        )

    for annotation in generated_annotations:
        page_idx, bbox = _required_carrier_locator(
            annotation,
            source_pdf_page_count=source_pdf_page_count,
        )
        role = f"evidence_image_{annotation.source_item_index:06d}"
        path = generated_annotation_artifacts.get(role)
        if path is None:
            _fail(
                "generated_annotation_unbound",
                "generated image description lacks its provider image artifact",
            )
        result.append(
            {
                "selector": _full_selector(annotation),
                "page_idx": page_idx,
                "bbox": list(bbox),
                "support": {
                    "kind": "generated_annotation",
                    "artifact": _generated_artifact_descriptor(role, path),
                },
            }
        )
    return result


def _full_selector(carrier: MinerUTextCarrier) -> dict[str, Any]:
    selector: dict[str, Any] = {
        "source_item_index": carrier.source_item_index,
        "field": carrier.field,
        "char_span": [0, len(carrier.comparison_value)],
        "value_sha256": _digest(carrier.comparison_value.encode()),
        "projection": TEXT_PROJECTION,
    }
    if carrier.index is not None:
        selector["index"] = carrier.index
    return selector


def _generated_artifact_descriptor(role: str, path: Path) -> dict[str, Any]:
    if _IMAGE_ARTIFACT_ROLE.fullmatch(role) is None:
        _fail("generated_artifact_invalid", "generated artifact role is invalid")
    try:
        before = path.stat()
        if not path.is_file():
            raise OSError("not a file")
        with path.open("rb") as stream:
            sha256 = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        after = path.stat()
    except OSError as exc:
        raise SourceEvidenceContractError(
            "generated_artifact_invalid",
            f"cannot bind generated annotation source artifact: {path}",
        ) from exc
    if (
        before.st_size < 1
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        _fail(
            "generated_artifact_invalid",
            "generated annotation source artifact changed while hashing",
        )
    return {
        "artifact_role": role,
        "sha256": sha256,
        "size_bytes": before.st_size,
    }


def _bbox_contains(container: BBox, item: BBox) -> bool:
    return (
        container[0] <= item[0]
        and container[1] <= item[1]
        and container[2] >= item[2]
        and container[3] >= item[3]
    )


def _table_body_bboxes(
    content_list: Sequence[Mapping[str, Any]],
    *,
    source_pdf_page_count: int,
) -> Mapping[int, tuple[BBox, ...]]:
    """Return only provider-typed table regions with exact page geometry."""

    by_page: dict[int, list[BBox]] = {}
    for item in content_list:
        if item.get("type") != "table":
            continue
        page_idx = _page_idx(item.get("page_idx"))
        bbox = _optional_bbox(item.get("bbox"))
        if page_idx is None or page_idx >= source_pdf_page_count or bbox is None:
            _fail(
                "table_retrieval_boundary_unproved",
                "table retrieval boundary requires an exact page and bbox",
            )
        by_page.setdefault(page_idx, []).append(bbox)
    return {page_idx: tuple(regions) for page_idx, regions in sorted(by_page.items())}


def _retrieval_run_records(
    source_pages: Sequence[NativeTextPage],
    records_by_page: Sequence[Sequence[Mapping[str, Any]]],
    *,
    native_structure: NativeStructureIndex,
    table_bboxes_by_page: Mapping[int, Sequence[BBox]],
) -> list[dict[str, Any]]:
    """Build retrieval boundaries without changing word occurrence ownership."""

    output: list[dict[str, Any]] = []
    cells_by_key = {cell.cell_key: cell for cell in native_structure.table_cells}
    cells_by_page: dict[
        int,
        list[
            tuple[
                tuple[str, int, int],
                str,
                tuple[BBox, ...],
            ]
        ],
    ] = {}
    for cell in native_structure.table_cells:
        cells_by_page.setdefault(cell.cell_key[2], []).append(
            (cell.cell_key, comparison_text(cell.text), cell.bboxes)
        )
    native_guards_by_page: dict[int, list[BBox]] = {}
    for page_idx, bbox in native_structure.table_guard_bboxes:
        native_guards_by_page.setdefault(page_idx, []).append(bbox)
    global_offset = 0
    for page, records in zip(source_pages, records_by_page, strict=True):
        if len(records) != len(page.atoms):
            _fail(
                "retrieval_run_source_mismatch",
                "retrieval runs require one disposition per native atom",
            )
        by_order = {
            atom.order: (atom, records[index], global_offset + index)
            for index, atom in enumerate(page.atoms)
        }
        page_run_index = 0
        pending: list[tuple[NativeTextAtom, int]] = []
        pending_algorithm = NATIVE_TEXT_RUN_ALGORITHM
        pending_basis = "source_layout"
        pending_cell_key: tuple[str, int, int] | None = None

        def emit() -> None:
            nonlocal page_run_index, pending_algorithm, pending_basis, pending_cell_key
            if not pending:
                return
            if pending_algorithm == _TABLE_SINGLETON_ALGORITHM and len(pending) != 1:
                _fail(
                    "retrieval_run_table_boundary_invalid",
                    "unproved table cell retrieval runs must be singleton",
                )
            atoms = [atom for atom, _ in pending]
            if pending_algorithm == _TABLE_TD_RUN_ALGORITHM:
                if pending_cell_key is None:
                    _fail(
                        "retrieval_run_table_boundary_invalid",
                        "proved table-cell run lacks a cell identity",
                    )
                cell = cells_by_key.get(pending_cell_key)
                if cell is None or comparison_text(
                    "".join(atom.text for atom in atoms)
                ) not in comparison_text(cell.text):
                    _fail(
                        "retrieval_run_table_boundary_invalid",
                        "proved table-cell run does not replay its TD text",
                    )
            if (
                pending_algorithm == _TABLE_TD_RUN_ALGORITHM
                and pending_basis != "native_complete_cell"
                or pending_algorithm == _TABLE_SINGLETON_ALGORITHM
                and pending_basis not in {"native_table_guard", "provider_table_guard"}
                or pending_algorithm == NATIVE_TEXT_RUN_ALGORITHM
                and pending_basis != "source_layout"
            ):
                _fail(
                    "retrieval_run_boundary_basis_invalid",
                    "retrieval run algorithm and boundary evidence disagree",
                )
            output.append(
                {
                    "page_idx": page.page_idx,
                    "run_index": page_run_index,
                    "layout_line": list(atoms[0].layout.line_ref),
                    "atom_indices": [index for _, index in pending],
                    "bbox": [
                        min(atom.bbox[0] for atom in atoms),
                        min(atom.bbox[1] for atom in atoms),
                        max(atom.bbox[2] for atom in atoms),
                        max(atom.bbox[3] for atom in atoms),
                    ],
                    "text_sha256": _digest(
                        "".join(atom.text for atom in atoms).encode()
                    ),
                    "join_algorithm": pending_algorithm,
                    "boundary_basis": pending_basis,
                }
            )
            page_run_index += 1
            pending.clear()
            pending_algorithm = NATIVE_TEXT_RUN_ALGORITHM
            pending_basis = "source_layout"
            pending_cell_key = None

        for layout_run in native_text_runs(page):
            for atom_order in layout_run.atom_orders:
                atom, record, atom_index = by_order[atom_order]
                disposition = _mapping(
                    record.get("disposition"),
                    "source_disposition_invalid",
                )
                if disposition.get("kind") != "source_native_fallback":
                    emit()
                    continue
                cell_key = _native_table_cell_key(
                    atom,
                    page=page,
                    page_cells=cells_by_page.get(page.page_idx, ()),
                )
                in_provider_table = any(
                    _bbox_matches(atom.bbox, region, page)
                    for region in table_bboxes_by_page.get(page.page_idx, ())
                )
                in_native_table_guard = any(
                    _bbox_center_in_role(atom.bbox, region, page)
                    for region in native_guards_by_page.get(page.page_idx, ())
                )
                if cell_key is not None or in_native_table_guard or in_provider_table:
                    algorithm = (
                        _TABLE_TD_RUN_ALGORITHM
                        if cell_key is not None
                        else _TABLE_SINGLETON_ALGORITHM
                    )
                    basis = (
                        "native_complete_cell"
                        if cell_key is not None
                        else "native_table_guard"
                        if in_native_table_guard
                        else "provider_table_guard"
                    )
                    if pending and (
                        pending_algorithm != algorithm
                        or pending_basis != basis
                        or pending_cell_key != cell_key
                    ):
                        emit()
                    pending_algorithm = algorithm
                    pending_basis = basis
                    pending_cell_key = cell_key
                    pending.append((atom, atom_index))
                    if algorithm == _TABLE_SINGLETON_ALGORITHM:
                        emit()
                    continue
                if pending and pending_algorithm != NATIVE_TEXT_RUN_ALGORITHM:
                    emit()
                pending.append((atom, atom_index))
            emit()
        global_offset += len(page.atoms)
    return output


def _native_table_cell_key(
    atom: NativeTextAtom,
    *,
    page: NativeTextPage,
    page_cells: Sequence[tuple[tuple[str, int, int], str, tuple[BBox, ...]]],
) -> tuple[str, int, int] | None:
    text = comparison_text(atom.text)
    if not text:
        return None
    matches = {
        cell_key
        for cell_key, cell_text, bboxes in page_cells
        if text in cell_text
        and any(_bbox_center_in_role(atom.bbox, bbox, page) for bbox in bboxes)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _reconcile_page(
    page: NativeTextPage,
    carriers: Sequence[MinerUTextCarrier],
) -> list[dict[str, Any]]:
    atoms = list(page.atoms)
    used: list[_Occurrence] = []
    matches: dict[int, _Occurrence] = {}
    had_exact: dict[int, bool] = {}

    def locate(
        needle: str,
        atom_indices: Sequence[int],
    ) -> tuple[list[_Occurrence], bool]:
        exact = [
            occurrence
            for carrier in carriers
            for occurrence in _occurrences(carrier, needle)
        ]
        located = [
            item
            for item in exact
            if all(
                _bbox_matches(atoms[k].bbox, item.carrier.bbox, page)
                for k in atom_indices
            )
            and not any(_overlaps(item, prior) for prior in used)
        ]
        located_carriers = {
            (
                item.carrier.source_item_index,
                item.carrier.field,
                item.carrier.index,
            )
            for item in located
        }
        if len(located_carriers) > 1:
            located = []
        return located, bool(exact)

    for index, atom in enumerate(atoms):
        located, exact = locate(comparison_text(atom.text), (index,))
        had_exact[index] = exact
        if located:
            selected = min(located, key=lambda item: item.position)
            used.append(selected)
            matches[index] = selected

    # A page column or narrow table cell wraps one visual token across word
    # boundaries; every fragment then fails the clean-edge occurrence test on
    # its own. Maximal residual runs are therefore re-matched as one joined
    # token (longest window first) and split back into per-atom sub-spans, so
    # a wrapped number stays inside its table cell instead of leaking into a
    # redundant native recovery unit.
    index = 0
    total = len(atoms)
    while index < total:
        if index in matches:
            index += 1
            continue
        run_end = index
        while run_end < total and run_end not in matches:
            run_end += 1
        segment_start = index
        while segment_start < run_end:
            found_end = 0
            for segment_end in range(
                min(run_end, segment_start + 8), segment_start + 1, -1
            ):
                pieces = [
                    comparison_text(atoms[k].text)
                    for k in range(segment_start, segment_end)
                ]
                needle = "".join(pieces)
                if not needle or any(not piece for piece in pieces):
                    continue
                located, _ = locate(
                    needle, range(segment_start, segment_end)
                )
                if not located:
                    continue
                occurrence = min(located, key=lambda item: item.position)
                used.append(occurrence)
                offset = occurrence.start
                for k in range(segment_start, segment_end):
                    piece_length = len(comparison_text(atoms[k].text))
                    matches[k] = _Occurrence(
                        carrier=occurrence.carrier,
                        start=offset,
                        end=offset + piece_length,
                    )
                    offset += piece_length
                found_end = segment_end
                break
            segment_start = found_end if found_end else segment_start + 1
        index = run_end

    result: list[dict[str, Any]] = []
    last_position = (-1, -1, -1)
    for index, atom in enumerate(atoms):
        chosen = matches.get(index)
        if chosen is not None:
            source_order = (
                "monotonic" if chosen.position > last_position else "conflict"
            )
            if chosen.position > last_position:
                last_position = chosen.position
            disposition: dict[str, Any] = {
                "kind": "mineru_carrier",
                "source_order": source_order,
                "carrier": {
                    "page_idx": chosen.carrier.page_idx,
                    "bbox": list(cast(BBox, chosen.carrier.bbox)),
                    "order": chosen.carrier.source_item_index,
                    "selector": _selector(chosen),
                },
            }
        else:
            reason = (
                "mineru_locator_unproved"
                if had_exact.get(index)
                else "mineru_text_missing"
            )
            disposition = {"kind": "source_native_fallback", "reason": reason}
        result.append(
            {
                "source": {
                    "page_idx": page.page_idx,
                    "bbox": list(atom.bbox),
                    "order": atom.order,
                    "layout_path": [
                        atom.layout.flow_index,
                        atom.layout.block_index,
                        atom.layout.line_index,
                        atom.layout.word_index,
                    ],
                    "char_span": list(atom.char_span),
                    "text": atom.text,
                    "text_sha256": _digest(atom.text.encode()),
                },
                "disposition": disposition,
            }
        )
    return result


def _selector(item: _Occurrence) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source_item_index": item.carrier.source_item_index,
        "field": item.carrier.field,
        "char_span": [item.start, item.end],
        "value_sha256": _digest(item.carrier.comparison_value.encode()),
        "projection": TEXT_PROJECTION,
    }
    if item.carrier.index is not None:
        result["index"] = item.carrier.index
    return result


def _typed_values(
    item: Mapping[str, Any], kind: str
) -> list[tuple[str, int | None, str]]:
    try:
        return list(mineru_typed_values(item, kind))
    except MinerUFieldContractError as exc:
        _fail(exc.reason_code, str(exc))


def _ir_value(element: Mapping[str, Any], field: str, index: object) -> str:
    raw = element.get(field)
    if field in MINERU_SCALAR_IR_FIELDS:
        if index is not None or not isinstance(raw, str):
            _fail("selector_field_invalid", "scalar field selector is invalid")
        return _project_field(field, raw)[0]
    if field in MINERU_SEQUENCE_IR_FIELDS:
        if not _is_index(index) or not isinstance(raw, list) or index >= len(raw):
            _fail("selector_field_invalid", "sequence field selector is invalid")
        selected = raw[index]
        if not isinstance(selected, str):
            _fail("selector_field_invalid", "sequence field value is invalid")
        return _project_field(field, selected)[0]
    _fail("selector_field_invalid", f"unsupported selector field: {field!r}")


def _project_field(
    field: str,
    value: str,
) -> tuple[str, frozenset[int], frozenset[int]]:
    segments = html_visible_text_segments(value) if field == "table_html" else (value,)
    projected: list[str] = []
    boundaries = {0}
    hard_boundaries: set[int] = set()
    for segment_index, segment in enumerate(segments):
        if segment_index and projected:
            hard_boundaries.add(len(projected))
            boundaries.add(len(projected))
        after_whitespace = False
        for char in unicodedata.normalize("NFKC", segment):
            if char.isspace():
                after_whitespace = True
                continue
            if after_whitespace:
                boundaries.add(len(projected))
                after_whitespace = False
            projected.append(char)
    boundaries.add(len(projected))
    return (
        "".join(projected),
        frozenset(boundaries),
        frozenset(hard_boundaries),
    )


def _occurrences(carrier: MinerUTextCarrier, needle: str) -> list[_Occurrence]:
    result: list[_Occurrence] = []
    offset = 0
    while (start := carrier.comparison_value.find(needle, offset)) >= 0:
        end = start + len(needle)
        left = carrier.comparison_value[start - 1] if start else ""
        right = (
            carrier.comparison_value[end] if end < len(carrier.comparison_value) else ""
        )
        crosses_hard_boundary = any(
            start < boundary < end for boundary in carrier.hard_boundaries
        )
        if (
            not crosses_hard_boundary
            and not (
                left
                and needle[0].isascii()
                and needle[0].isalnum()
                and start not in carrier.boundaries
                and ((left.isascii() and left.isalnum()) or left in ",，.．%％")
            )
            and not (
                right
                and needle[-1].isascii()
                and needle[-1].isalnum()
                and end not in carrier.boundaries
                and ((right.isascii() and right.isalnum()) or right in ",，.．%％")
            )
        ):
            result.append(_Occurrence(carrier, start, end))
        offset = start + 1
    return result


def _bbox_matches(source: BBox, candidate: BBox | None, page: NativeTextPage) -> bool:
    if candidate is None:
        return False
    sx0, sy0, sx1, sy1 = (
        source[0] / page.width,
        source[1] / page.height,
        source[2] / page.width,
        source[3] / page.height,
    )
    cx0, cy0, cx1, cy1 = (value / _EXTENT for value in candidate)
    return (
        sx1 >= cx0 - _TOLERANCE
        and sx0 <= cx1 + _TOLERANCE
        and sy1 >= cy0 - _TOLERANCE
        and sy0 <= cy1 + _TOLERANCE
    )


def _bbox_center_in_role(
    source: BBox,
    role: BBox,
    page: NativeTextPage,
) -> bool:
    """Assign a whole Poppler word only when its center is inside the role."""

    center = (
        (source[0] + source[2]) / 2 / page.width * _EXTENT,
        (source[1] + source[3]) / 2 / page.height * _EXTENT,
    )
    return role[0] <= center[0] <= role[2] and role[1] <= center[1] <= role[3]


def _overlaps(left: _Occurrence, right: _Occurrence) -> bool:
    return (
        left.carrier.source_item_index == right.carrier.source_item_index
        and left.carrier.field == right.carrier.field
        and left.carrier.index == right.carrier.index
        and left.start < right.end
        and right.start < left.end
    )


def _validate_pages(pages: Sequence[NativeTextPage], page_count: int) -> None:
    if [page.page_idx for page in pages] != list(range(page_count)):
        _fail("source_page_closure_invalid", "source page range is not closed")
    for page in pages:
        if min(page.width, page.height) <= 0 or not all(
            math.isfinite(value) for value in (page.width, page.height)
        ):
            _fail("source_page_geometry_invalid", "page size is invalid")
        source_orders = sorted(
            [atom.order for atom in page.atoms]
            + [issue.word_order for issue in page.geometry_issues]
        )
        if source_orders != list(range(len(source_orders))):
            _fail("source_atom_order_invalid", "atom order is not closed")
        for issue in page.geometry_issues:
            if (
                issue.page_idx != page.page_idx
                or not issue.text
                or issue.reason
                not in {"bbox_missing_or_non_finite", "bbox_non_positive_extent"}
                or (
                    issue.reason == "bbox_missing_or_non_finite"
                    and issue.raw_bbox is not None
                )
                or (
                    issue.reason == "bbox_non_positive_extent"
                    and (
                        issue.raw_bbox is None
                        or (
                            issue.raw_bbox[0] < issue.raw_bbox[2]
                            and issue.raw_bbox[1] < issue.raw_bbox[3]
                        )
                    )
                )
            ):
                _fail(
                    "source_geometry_issue_invalid",
                    "native geometry issue is invalid",
                )
        previous_end = 0
        previous_layout: tuple[int, int, int, int] | None = None
        layout_paths: set[tuple[int, int, int, int]] = set()
        for atom in page.atoms:
            _required_bbox(atom.bbox, normalized=False)
            start, end = _span(atom.char_span, len(page.text))
            layout_path = (
                atom.layout.flow_index,
                atom.layout.block_index,
                atom.layout.line_index,
                atom.layout.word_index,
            )
            if (
                atom.page_idx != page.page_idx
                or start < previous_end
                or not atom.text
                or page.text[start:end] != atom.text
                or any(not _is_index(item) for item in layout_path)
                or layout_path in layout_paths
                or (previous_layout is not None and layout_path <= previous_layout)
            ):
                _fail("source_atom_invalid", "atom text/span is invalid")
            layout_paths.add(layout_path)
            previous_layout = layout_path
            previous_end = end
        native_text_runs(page)


def _page_idx(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _layout_path(value: object) -> tuple[int, int, int, int] | None:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not _is_index(item) for item in value)
    ):
        return None
    return cast(tuple[int, int, int, int], tuple(value))


def _optional_bbox(value: object) -> BBox | None:
    try:
        return _required_bbox(value, normalized=True)
    except SourceEvidenceContractError:
        return None


def _required_bbox(value: object, *, normalized: bool) -> BBox:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        _fail("bbox_invalid", "bbox requires four finite numbers")
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float)) for item in value
    ):
        _fail("bbox_invalid", "bbox requires four finite numbers")
    box = cast(BBox, tuple(float(item) for item in value))
    if not all(math.isfinite(item) for item in box):
        _fail("bbox_invalid", "bbox requires four finite numbers")
    if (
        min(box) < 0
        or box[0] >= box[2]
        or box[1] >= box[3]
        or (normalized and max(box) > _EXTENT)
    ):
        _fail("bbox_invalid", "bbox geometry is invalid")
    return box


def _span(value: object, maximum: int | None = None) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        _fail("char_span_invalid", "char span is invalid")
    start, end = value
    if not _is_index(start) or not _is_index(end):
        _fail("char_span_invalid", "char span is invalid")
    if start >= end or (maximum is not None and end > maximum):
        _fail("char_span_invalid", "char span is empty or out of range")
    return start, end


def _identity(value: ExtractorIdentity, field: str) -> None:
    if not value.name.strip() or not value.version.strip():
        _fail("extractor_identity_invalid", f"{field} requires name/version")


def _validate_identity_mapping(value: object, *, field: str) -> None:
    identity = _mapping(value, "extractor_identity_invalid")
    if set(identity) != {"name", "version"}:
        _fail("extractor_identity_invalid", f"{field} fields are not closed")
    if not all(
        isinstance(identity.get(key), str) and identity[key].strip()
        for key in ("name", "version")
    ):
        _fail("extractor_identity_invalid", f"{field} requires name/version")


def _mapping(value: object, reason_code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(reason_code, "value must be an object")
    return value


def _content_list_payload(
    value: bytes,
    *,
    expected_sha256: str,
) -> list[dict[str, Any]]:
    if _digest(value) != expected_sha256:
        _fail("mineru_artifact_hash_mismatch", "MinerU artifact hash differs")
    try:
        decoded: object = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceEvidenceContractError(
            "mineru_artifact_invalid",
            f"invalid content_list JSON: {exc}",
        ) from exc
    if not isinstance(decoded, list) or not all(
        isinstance(item, dict) for item in decoded
    ):
        _fail("mineru_artifact_invalid", "content_list must be an object array")
    return cast(list[dict[str, Any]], decoded)


def _validated_canonical_content_list(
    legacy: Sequence[Mapping[str, Any]],
    canonical: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    if len(legacy) != len(canonical):
        _fail(
            "mineru_text_projection_invalid",
            "canonical MinerU item count differs",
        )
    output: list[Mapping[str, Any]] = []
    for source_item_index, (raw, projected) in enumerate(
        zip(legacy, canonical, strict=True)
    ):
        if (
            set(raw) != set(projected)
            or raw.get("type") != projected.get("type")
            or raw.get("page_idx") != projected.get("page_idx")
            or raw.get("bbox") != projected.get("bbox")
        ):
            _fail(
                "mineru_text_projection_invalid",
                "canonical MinerU item identity differs "
                f"(source_item_index={source_item_index})",
            )
        output.append(projected)
    return tuple(output)


def _list(value: object, reason_code: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(reason_code, "value must be an array")
    return value


def _count_mapping(
    value: object,
    *,
    allowed: frozenset[str],
    field: str,
) -> dict[str, int]:
    raw = _mapping(value, "source_page_fallback_invalid")
    if not set(raw) <= allowed:
        _fail(
            "source_page_fallback_invalid",
            f"{field} contains an unsupported reason",
        )
    counts: dict[str, int] = {}
    for key, count in raw.items():
        if not isinstance(key, str) or not _is_index(count) or count < 1:
            _fail(
                "source_page_fallback_invalid",
                f"{field} counts must be positive integers",
            )
        counts[key] = count
    return counts


def _finite_positive(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        return None
    return float(value)


def _require_sha(value: str, field: str) -> None:
    if _SHA256.fullmatch(value) is None:
        _fail("sha256_invalid", f"{field} is invalid")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _fail(reason_code: str, message: str) -> NoReturn:
    raise SourceEvidenceContractError(reason_code, message)


def _is_index(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
