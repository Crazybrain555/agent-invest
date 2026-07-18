"""Pure S1-S7 document_unit builder stages."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import hashlib
import re
import unicodedata
from typing import Any, Callable, Iterable

from disclosure_anchor.adapters.unit_builder import rules, toc_outline
from disclosure_anchor.adapters.unit_builder.source_projection import (
    project_official_ir_form,
)
from disclosure_anchor.adapters.unit_builder.table_grid import (
    merge_table_grids_with_stats,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    empty_projection_graph,
    source_selector,
    source_value_sha256,
)
from disclosure_anchor.domain.value_objects.comparison_text import comparison_text


ImageBytesResolver = Callable[[str], bytes]


@dataclass(frozen=True)
class PreparedElement:
    kind: str
    order_index: int
    intra_order: int = 0
    text: str | None = None
    # Exact parser value before S1 cleanup.  Logical projections may bind a
    # replayable char span into this value; it is internal builder state and
    # never copied into the public payload or locator.
    source_text: str | None = None
    raw_kind: str | None = None
    page_no: int | None = None
    heading_level: int | None = None
    table: dict[str, Any] | None = None
    table_caption: list[str] = field(default_factory=list)
    table_footnote: list[str] = field(default_factory=list)
    table_html: str | None = None
    table_parse_failed: bool = False
    image_path: str | None = None
    payload: dict[str, Any] | None = None
    quality_status: str = "ok"
    artifact_locator: dict[str, Any] | None = None
    applicability: str | None = None
    # Source ownership is explicit internal state.  Parser-labelled page
    # furniture is document-level evidence even when exact-dedup changes the
    # locator derivation; it must never inherit whichever business section
    # happened to be active at the page boundary.
    inherits_section: bool = True
    heading_path: list[str] = field(default_factory=list)
    # Complete S2 stack used for section identity. ``heading_path`` persists the
    # same source hierarchy; keeping the separate field prevents later grouping
    # stages from accidentally substituting a derived/local mixed-unit path.
    structural_path: list[str] = field(default_factory=list)
    # Internal identity of the concrete heading occurrences that own this
    # element. Textual paths are not identities: two sibling sections may have
    # the same title and must still remain separate.
    section_path: list[int] = field(default_factory=list)
    title: str | None = None
    region_role: str | None = None
    region_id: str | None = None


@dataclass(frozen=True)
class UnitDraft:
    payload_kind: str
    payload: dict[str, Any]
    source_order: int
    intra_order: int = 0
    heading_path: list[str] = field(default_factory=list)
    structural_path: list[str] = field(default_factory=list)
    section_path: list[int] = field(default_factory=list)
    title: str | None = None
    semantic_key: str | None = None
    semantic_keys: list[str] | None = None
    quality_status: str = "ok"
    applicability: str | None = None
    artifact_locator: dict[str, Any] | None = None
    region_role: str | None = None
    region_id: str | None = None
    source_segments: list[tuple[str, dict[str, Any] | None]] = field(
        default_factory=list
    )


@dataclass
class BuildStats:
    generated_by_kind: Counter[str] = field(default_factory=Counter)
    dropped_by_kind: Counter[str] = field(default_factory=Counter)
    dropped_unknown_by_raw_kind: Counter[str] = field(default_factory=Counter)
    stripped_marker_lines: int = 0
    dropped_blank_table_rows: int = 0
    heading_only_carriers_preserved: int = 0
    heading_outline_units_generated: int = 0
    anchored_header_units: int = 0
    merged_cover_title_fragments: int = 0
    deduplicated_registered_header_lines: int = 0
    deduplicated_page_number_lines: int = 0
    toc_entry_headings_demoted: int = 0
    toc_page_boundaries_synthesized: int = 0
    needs_review_count: int = 0
    unusable_count: int = 0
    official_ir_form_status: str = "not_applicable"
    official_ir_projected_carriers: int = 0
    official_ir_reason_code: str | None = None
    # Per-source transform/exclusion ledger.  Counts explain volume; this
    # ledger makes every non-payload disposition independently auditable.
    source_dispositions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_by_kind": dict(self.generated_by_kind),
            "dropped_by_kind": dict(self.dropped_by_kind),
            "dropped_unknown_by_raw_kind": dict(self.dropped_unknown_by_raw_kind),
            "stripped_marker_lines": self.stripped_marker_lines,
            "dropped_blank_table_rows": self.dropped_blank_table_rows,
            "heading_only_carriers_preserved": (
                self.heading_only_carriers_preserved
            ),
            "heading_outline_units_generated": (
                self.heading_outline_units_generated
            ),
            "anchored_header_units": self.anchored_header_units,
            "merged_cover_title_fragments": self.merged_cover_title_fragments,
            "deduplicated_registered_header_lines": (
                self.deduplicated_registered_header_lines
            ),
            "deduplicated_page_number_lines": self.deduplicated_page_number_lines,
            "toc_entry_headings_demoted": self.toc_entry_headings_demoted,
            "toc_page_boundaries_synthesized": (
                self.toc_page_boundaries_synthesized
            ),
            "needs_review_count": self.needs_review_count,
            "unusable_count": self.unusable_count,
            "official_ir_form_status": self.official_ir_form_status,
            "official_ir_projected_carriers": (
                self.official_ir_projected_carriers
            ),
            "official_ir_reason_code": self.official_ir_reason_code,
            "source_dispositions": list(self.source_dispositions),
        }


@dataclass(frozen=True)
class Stage1Result:
    elements: list[PreparedElement]
    stats: BuildStats


@dataclass(frozen=True)
class _HeadingStackEntry:
    """One concrete source heading occurrence in the active hierarchy."""

    logical_level: int
    title: str
    occurrence_id: int
    source_level: int
    pattern_level: int | None
    artifact_locator: dict[str, Any] | None
    projection_field: str = "text"
    projection_index: int | None = None
    outline_family: str | None = None
    outline_ordinal: int | None = None
    dotted_components: tuple[int, ...] | None = None
    # The document's TOC declared this title as a top-level section, so the
    # entry carries proven depth even without a grammar pattern.
    toc_proven: bool = False


@dataclass(frozen=True)
class _HeadingPatternEvidence:
    level: int | None
    family: str | None = None
    ordinal: int | None = None
    dotted_components: tuple[int, ...] | None = None


@dataclass(frozen=True)
class _FurnitureDedupPlan:
    suppressed_indices: frozenset[int] = frozenset()
    source_locators_by_canonical: dict[int, list[dict[str, Any]]] = field(
        default_factory=dict
    )


def s1_preprocess_elements(
    elements: Iterable[dict[str, Any]],
    *,
    image_bytes_resolver: ImageBytesResolver | None = None,
) -> Stage1Result:
    stats = BuildStats()
    prepared: list[PreparedElement] = []
    previous_non_furniture: PreparedElement | None = None
    raw_elements = list(elements)
    furniture_plan = _page_furniture_dedup_plan(raw_elements)
    raw_by_source_item_index = {
        source_item_index: element
        for element in raw_elements
        if isinstance(
            (source_item_index := element.get("source_item_index")), int
        )
        and not isinstance(source_item_index, bool)
    }

    for element_index, element in enumerate(raw_elements):
        kind = str(element.get("kind", "unknown"))
        order_index = int(element.get("order_index", len(prepared)))
        intra_order = _int_or_none(element.get("projection_intra_order")) or 0
        raw_kind = str(element.get("raw_kind", kind))
        source_text = _element_text(element)
        page_no = _int_or_none(element.get("page_no"))
        region_role = _optional_region_value(element.get("projection_region_role"))
        region_id = _optional_text_value(element.get("projection_region_id"))
        inherits_section = element.get("projection_inherits_section") is not False
        if element_index in furniture_plan.suppressed_indices:
            stats.dropped_by_kind["page_furniture_exact_duplicate"] += 1
            continue
        locator = _artifact_locator(element)
        continuation_indices = locator.get("continuation_source_item_indices")
        if isinstance(continuation_indices, list):
            continuation_locators = [
                _artifact_locator(raw_by_source_item_index[index])
                for index in continuation_indices
                if isinstance(index, int)
                and not isinstance(index, bool)
                and index in raw_by_source_item_index
            ]
            if continuation_locators:
                locator["continuation_source_locators"] = continuation_locators
        source_locators = furniture_plan.source_locators_by_canonical.get(
            element_index
        )
        if source_locators:
            locator = _canonical_duplicate_locator(locator, source_locators)
        if kind == "page_furniture":
            text = _clean_text(source_text)
            if not text:
                stats.dropped_by_kind[kind] += 1
                continue
            if rules.is_exact_page_number_metadata(
                text,
                raw_kind=raw_kind,
                page_no=page_no,
            ):
                stats.deduplicated_page_number_lines += 1
                for source_locator in _locator_source_leaves(locator):
                    _record_source_disposition(
                        stats,
                        source_locator,
                        role="external_metadata",
                        reason="exact_page_number",
                    )
                continue
            # Parser furniture labels are useful evidence but not infallible.
            # Preserve a unique labelled carrier as reviewable text; suppress
            # only a repeated cross-page signature proven by the pre-pass.
            if not source_locators:
                locator["derivation"] = {
                    "kind": "page_furniture_retained",
                    "reason": "unique_source_carrier",
                }
            item = PreparedElement(
                kind="text",
                order_index=order_index,
                intra_order=intra_order,
                raw_kind=raw_kind,
                page_no=page_no,
                text=text,
                source_text=source_text,
                quality_status="needs_review",
                artifact_locator=locator,
                inherits_section=False,
                region_role=region_role,
                region_id=region_id,
            )
            prepared.append(item)
            previous_non_furniture = item
            continue
        image_path = str(element.get("image_path") or "").strip()
        if kind in {"image", "equation"} and image_path:
            is_equation = kind == "equation"
            # MinerU equations never carry image_caption/image_footnote; only
            # image/chart elements do.  The equation caption is recovered from
            # its formula content below, so those reads are skipped here.
            caption_values = (
                []
                if is_equation
                else _source_text_values(element.get("image_caption"))
            )
            caption = "\n".join(caption_values)
            content = _source_text(_element_text(element))
            footnote_values = (
                []
                if is_equation
                else _source_text_values(element.get("image_footnote"))
            )
            if is_equation and not caption and content:
                caption = content
            # chart is a distinct MinerU visual type (mapper: kind="image",
            # raw_kind="chart"); equation keeps its own kind; everything else
            # is a plain image.  visual_subtype is a typed sub_type carried by
            # image/chart only.
            visual_kind = (
                "chart"
                if raw_kind == "chart"
                else "equation"
                if is_equation
                else "image"
            )
            raw_visual_subtype = None if is_equation else element.get("visual_subtype")
            visual_subtype = (
                raw_visual_subtype
                if isinstance(raw_visual_subtype, str) and raw_visual_subtype
                else None
            )
            context, context_locator = _image_context(
                previous_non_furniture, page_no
            )
            image_ref = _content_addressed_image_ref(
                image_path,
                image_bytes_resolver=image_bytes_resolver,
            )
            image_source = source_selector(locator, field="image")
            if image_source is not None:
                locator = _with_payload_projection(
                    locator,
                    {
                        "kind": "image_identity",
                        "sources": [image_source],
                        "target_field": "payload",
                        "transform": "sha256_bytes.v1",
                    },
                ) or locator
            if caption_values:
                caption_source = source_selector(locator, field="image_caption")
                if caption_source is not None:
                    locator = _with_structured_projection(
                        locator,
                        {
                            "kind": "derived_field",
                            "source": caption_source,
                            "target_field": "payload.caption",
                            "transform": "ordered_nonempty_lines.v1",
                        },
                    ) or locator
            if visual_subtype:
                subtype_source = source_selector(locator, field="visual_subtype")
                if subtype_source is not None:
                    locator = _with_structured_projection(
                        locator,
                        {
                            "kind": "derived_field",
                            "source": subtype_source,
                            "target_field": "payload.visual_subtype",
                            "transform": "identity.v1",
                        },
                    ) or locator
            if content:
                content_source = source_selector(locator, field="text")
                if content_source is not None:
                    content_target = (
                        "payload.caption"
                        if content == caption
                        else "payload.content"
                    )
                    locator = _with_structured_projection(
                        locator,
                        {
                            "kind": "derived_field",
                            "source": content_source,
                            "target_field": content_target,
                            "transform": "trim.v1",
                        },
                    ) or locator
            emitted_note_index = 0
            for note_index, raw_note in enumerate(
                [] if is_equation else (element.get("image_footnote") or [])
            ):
                cleaned_note = _source_text(str(raw_note))
                if not cleaned_note:
                    continue
                note_source = source_selector(
                    locator,
                    field="image_footnote",
                    index=note_index,
                )
                if note_source is not None:
                    locator = _with_structured_projection(
                        locator,
                        {
                            "kind": "derived_field",
                            "source": note_source,
                            "target_field": (
                                f"payload.notes.{emitted_note_index}"
                            ),
                            "transform": "trim.v1",
                        },
                    ) or locator
                emitted_note_index += 1
            if context and context_locator:
                locator["context_source_locator"] = context_locator
                locator["context_projection"] = {
                    "source_field": "text",
                    "target_field": "payload.context",
                    "derivation": "identity",
                }
                context_source = source_selector(context_locator, field="text")
                if context_source is not None:
                    locator = _with_structured_projection(
                        locator,
                        {
                            "kind": "derived_field",
                            "source": context_source,
                            "target_field": "payload.context",
                            "transform": "same_page_previous_heading.v1",
                        },
                    ) or locator
            payload: dict[str, Any] = {
                "image_ref": image_ref,
                "caption": caption,
                "context": context,
                "visual_kind": visual_kind,
            }
            if visual_subtype:
                payload["visual_subtype"] = visual_subtype
            if content and content != caption:
                payload["content"] = content
            if footnote_values:
                payload["notes"] = footnote_values
            item = PreparedElement(
                kind="text",
                order_index=order_index,
                intra_order=intra_order,
                raw_kind=raw_kind,
                page_no=page_no,
                payload=payload,
                title=context or caption or None,
                quality_status="needs_review",
                artifact_locator=locator,
                inherits_section=inherits_section,
                region_role=region_role,
                region_id=region_id,
            )
            prepared.append(item)
            previous_non_furniture = item
            continue
        if kind in {"text", "heading", "equation"}:
            text = _clean_text(source_text)
            if not text:
                stats.dropped_by_kind[kind] += 1
                continue
            output_kind = "text" if kind == "equation" else kind
            item = PreparedElement(
                kind=output_kind,
                order_index=order_index,
                intra_order=intra_order,
                raw_kind=raw_kind,
                page_no=page_no,
                heading_level=_int_or_none(element.get("heading_level")),
                text=text,
                source_text=source_text,
                artifact_locator=locator,
                inherits_section=inherits_section,
                region_role=region_role,
                region_id=region_id,
            )
            prepared.append(item)
            previous_non_furniture = item
            continue
        if kind == "unknown":
            text = _clean_text(source_text)
            if not text:
                stats.dropped_by_kind[kind] += 1
                stats.dropped_unknown_by_raw_kind[raw_kind] += 1
                continue
            item = PreparedElement(
                kind="text",
                order_index=order_index,
                intra_order=intra_order,
                raw_kind=raw_kind,
                page_no=page_no,
                heading_level=_int_or_none(element.get("heading_level")),
                text=text,
                source_text=source_text,
                quality_status="needs_review",
                artifact_locator=locator,
                inherits_section=inherits_section,
                region_role=region_role,
                region_id=region_id,
            )
            prepared.append(item)
            previous_non_furniture = item
            continue
        if kind == "image":
            caption = _clean_text(_caption_text(element))
            if caption:
                locator = _artifact_locator(element)
                locator["derivation"] = {
                    "kind": "image_caption_without_image",
                    "reason": "missing_image_path",
                }
                caption_source = source_selector(locator, field="image_caption")
                if caption_source is not None:
                    locator = _with_payload_projection(
                        locator,
                        {
                            "kind": "text_identity",
                            "sources": [caption_source],
                            "target_field": "payload.text",
                            "transform": "clean_text.v1",
                        },
                    ) or locator
                item = PreparedElement(
                    kind="text",
                    order_index=order_index,
                    intra_order=intra_order,
                    raw_kind=raw_kind,
                    page_no=page_no,
                    payload={"text": caption},
                    source_text=_caption_text(element),
                    title=caption,
                    quality_status="needs_review",
                    artifact_locator=locator,
                    inherits_section=inherits_section,
                    region_role=region_role,
                    region_id=region_id,
                )
                prepared.append(item)
                previous_non_furniture = item
            else:
                # No content-addressable source or unique caption exists. The
                # nearby context is already preserved by its own source carrier.
                stats.dropped_by_kind[kind] += 1
            continue
        if kind == "table":
            # Captions are source evidence even when MinerU attached a
            # checkbox declaration. Title selection below excludes those
            # markers without deleting them from the payload.
            captions = [str(caption) for caption in element.get("table_caption") or []]
            item = PreparedElement(
                kind="table",
                order_index=order_index,
                intra_order=intra_order,
                raw_kind=raw_kind,
                page_no=page_no,
                heading_level=_int_or_none(element.get("heading_level")),
                table=dict(element.get("table") or {"headers": [], "rows": []}),
                table_caption=captions,
                table_footnote=[
                    str(item) for item in element.get("table_footnote") or []
                ],
                table_html=element.get("table_html"),
                table_parse_failed=bool(element.get("table_parse_failed")),
                artifact_locator=locator,
                inherits_section=inherits_section,
                region_role=region_role,
                region_id=region_id,
            )
            prepared.append(item)
            previous_non_furniture = item
            continue
        stats.dropped_by_kind[kind] += 1

    return Stage1Result(elements=prepared, stats=stats)


def _page_furniture_dedup_plan(
    elements: list[dict[str, Any]],
) -> _FurnitureDedupPlan:
    """Plan exact deduplication as disjoint source-identity components."""

    occurrences: dict[tuple[str, str], list[tuple[int, int | None]]] = {}
    for index, element in enumerate(elements):
        if str(element.get("kind")) != "page_furniture":
            continue
        text = _clean_text(_element_text(element))
        signature = _comparison_text(text)
        if signature:
            raw_kind = str(element.get("raw_kind") or "")
            occurrences.setdefault((raw_kind, signature), []).append(
                (index, _int_or_none(element.get("page_no")))
            )
    duplicate_edges: list[tuple[int, int]] = []
    for _, items in occurrences.items():
        pages = {page for _, page in items if page is not None}
        if len(pages) >= 2:
            indices = [index for index, _ in items]
            first_furniture = indices[0]
            cover_heading_candidates = [
                index
                for index, candidate in enumerate(elements[:first_furniture])
                if str(candidate.get("kind")) == "heading"
                and _comparison_text(_element_text(candidate))
                == _comparison_text(_element_text(elements[first_furniture]))
            ]
            adjacent_real_duplicates = [
                index + 1
                for index in indices
                if index + 1 < len(elements)
                and str(elements[index + 1].get("kind")) in {"text", "heading"}
                and _comparison_text(_element_text(elements[index]))
                == _comparison_text(_element_text(elements[index + 1]))
            ]
            group_indices = [*cover_heading_candidates, *indices]
            for real_index in adjacent_real_duplicates:
                furniture_index = real_index - 1
                if elements[real_index].get("page_no") == elements[
                    furniture_index
                ].get("page_no"):
                    group_indices.append(real_index)
            unique_indices = list(dict.fromkeys(group_indices))
            duplicate_edges.extend(
                (unique_indices[0], index) for index in unique_indices[1:]
            )
    for index, element in enumerate(elements[:-1]):
        if str(element.get("kind")) != "page_furniture":
            continue
        following = elements[index + 1]
        if str(following.get("kind")) not in {"text", "heading"}:
            continue
        signature = _comparison_text(_element_text(element))
        if (
            signature
            and signature == _comparison_text(_element_text(following))
            and element.get("page_no") == following.get("page_no")
        ):
            duplicate_edges.append((index, index + 1))
    return _furniture_components_plan(elements, duplicate_edges)


def _furniture_components_plan(
    elements: list[dict[str, Any]], duplicate_edges: Iterable[tuple[int, int]]
) -> _FurnitureDedupPlan:
    """Close overlapping duplicate edges before selecting representatives."""

    parent: dict[int, int] = {}

    def find(index: int) -> int:
        root = parent.setdefault(index, index)
        while root != parent[root]:
            root = parent[root]
        while index != root:
            next_index = parent[index]
            parent[index] = root
            index = next_index
        return root

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left, right in duplicate_edges:
        union(left, right)

    components: dict[int, list[int]] = {}
    for index in parent:
        components.setdefault(find(index), []).append(index)

    suppressed: set[int] = set()
    locators_by_canonical: dict[int, list[dict[str, Any]]] = {}
    for component in components.values():
        indices = sorted(component)
        furniture_indices = [
            index
            for index in indices
            if str(elements[index].get("kind")) == "page_furniture"
        ]
        if not furniture_indices:
            continue
        first_furniture = furniture_indices[0]
        cover_headings = [
            index
            for index in indices
            if index < first_furniture
            and str(elements[index].get("kind")) == "heading"
        ]
        real_carriers = [
            index
            for index in indices
            if str(elements[index].get("kind")) in {"text", "heading"}
        ]
        canonical = (
            cover_headings[-1]
            if cover_headings
            else real_carriers[0]
            if real_carriers
            else first_furniture
        )
        suppressed.update(index for index in indices if index != canonical)
        locators_by_canonical[canonical] = [
            _artifact_locator(elements[index]) for index in indices
        ]
    return _FurnitureDedupPlan(
        suppressed_indices=frozenset(suppressed),
        source_locators_by_canonical=locators_by_canonical,
    )


def _locator_source_leaves(locator: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every concrete source atom represented by an aggregate locator."""

    children = locator.get("source_locators")
    if not isinstance(children, list):
        return [locator]
    leaves: list[dict[str, Any]] = []
    for child in children:
        if isinstance(child, dict):
            leaves.extend(_locator_source_leaves(child))
    return leaves or [locator]


def _canonical_duplicate_locator(
    canonical: dict[str, Any], source_locators: list[dict[str, Any]]
) -> dict[str, Any]:
    locator = dict(canonical)
    identity_fields = ("ir_id", "source_item_index", "order_index")

    def identity(value: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(value.get(field) for field in identity_fields)

    # The selected public representative owns the payload.  Equivalent
    # carriers are lineage only; list order from the dedup pre-pass must never
    # silently promote an older cover/header occurrence to payload canonical.
    locators = [dict(canonical)]
    identities = {identity(canonical)}
    for item in source_locators:
        item_identity = identity(item)
        if item_identity in identities:
            continue
        locators.append(dict(item))
        identities.add(item_identity)
    locator["source_locators"] = locators
    orders = [
        value
        for item in locators
        if (value := _int_or_none(item.get("order_index"))) is not None
    ]
    if orders:
        locator["source_order_span"] = [min(orders), max(orders)]
    pages = [
        value
        for item in locators
        if (value := _int_or_none(item.get("page_no"))) is not None
    ]
    if pages and min(pages) != max(pages):
        locator["page_span"] = [min(pages), max(pages)]
    locator["derivation"] = {
        "kind": "exact_duplicate_carriers",
        "reason": "nfkc_whitespace_normalized_text_equal",
    }
    return locator


def _comparison_text(text: str) -> str:
    """Normalize only representation-level differences for exact comparison."""

    return comparison_text(text)


def _projection_graph(locator: dict[str, Any]) -> dict[str, Any]:
    raw = locator.get("source_projection")
    graph = empty_projection_graph()
    if not isinstance(raw, dict):
        return graph
    graph["payload"] = raw.get("payload")
    for graph_field in ("heading_path", "structured", "provenance"):
        value = raw.get(graph_field)
        graph[graph_field] = list(value) if isinstance(value, list) else []
    return graph


def _merged_projection_entries(
    *entry_groups: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge closed projection entries without duplicating identity edges."""

    merged: list[dict[str, Any]] = []
    for entries in entry_groups:
        for entry in entries:
            value = dict(entry)
            if value not in merged:
                merged.append(value)
    return merged


def _with_payload_projection(
    locator: dict[str, Any] | None,
    payload_projection: dict[str, Any],
) -> dict[str, Any] | None:
    output = dict(locator or {})
    graph = _projection_graph(output)
    graph["payload"] = payload_projection
    output["source_projection"] = graph
    return output or None


def _with_heading_projection(
    locator: dict[str, Any] | None,
    heading_projection: list[dict[str, Any]],
) -> dict[str, Any] | None:
    output = dict(locator or {})
    graph = _projection_graph(output)
    graph["heading_path"] = list(heading_projection)
    output["source_projection"] = graph
    return output or None


def _with_structured_projection(
    locator: dict[str, Any] | None,
    structured_projection: dict[str, Any],
) -> dict[str, Any] | None:
    output = dict(locator or {})
    graph = _projection_graph(output)
    graph["structured"] = [*graph["structured"], structured_projection]
    output["source_projection"] = graph
    return output or None


def _text_source_selectors(
    locator: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not locator:
        return []
    return [
        selector
        for leaf in _locator_source_leaves(locator)
        if (selector := _selector_for_locator(leaf, fallback_field="text"))
        is not None
    ]


def _selector_for_locator(
    locator: dict[str, Any],
    *,
    fallback_field: str,
    index: int | None = None,
) -> dict[str, Any] | None:
    """Build a source selector, honoring an evidence-slice derivation."""

    source_slice = locator.get("source_slice")
    if isinstance(source_slice, dict):
        return dict(source_slice)
    return source_selector(locator, field=fallback_field, index=index)


def _with_text_payload_projection(
    locator: dict[str, Any] | None,
    *,
    target_field: str = "payload.text",
) -> dict[str, Any] | None:
    output = dict(locator or {})
    graph = _projection_graph(output)
    existing = graph.get("payload")
    if (
        isinstance(existing, dict)
        and existing.get("kind")
        in {
            "text_identity",
            "text_concat",
            "text_partition",
            "exact_duplicate_text",
        }
        and existing.get("target_field") == target_field
    ):
        # Typed ownership is authoritative over recursive navigation hints.
        # Several builder stages decorate the same locator; re-inferring an
        # already-bound target would turn duplicate lineage back into payload.
        output["source_projection"] = graph
        return output or None
    selectors = _text_source_selectors(locator)
    if not selectors:
        return locator
    derivation = output.get("derivation")
    derivation_kind = (
        derivation.get("kind") if isinstance(derivation, dict) else None
    )
    if derivation_kind == "exact_duplicate_carriers" and len(selectors) > 1:
        canonical = selectors[0]
        graph["payload"] = {
            "kind": "exact_duplicate_text",
            "sources": [canonical],
            "target_field": target_field,
            "transform": "clean_text.v1",
        }
        graph["provenance"] = [
            *graph["provenance"],
            *(
                {
                    "kind": "exact_duplicate_of",
                    "source": selector["source"],
                    "canonical": canonical["source"],
                }
                for selector in selectors[1:]
            ),
        ]
    else:
        graph["payload"] = {
            "kind": "text_concat" if len(selectors) > 1 else "text_identity",
            "sources": selectors,
            "target_field": target_field,
            "transform": "clean_text.v1",
        }
    output["source_projection"] = graph
    return output or None


def _with_table_payload_projection(
    locator: dict[str, Any] | None,
) -> dict[str, Any] | None:
    output = dict(locator or {})
    selector = _selector_for_locator(output, fallback_field="table")
    if selector is None:
        return locator
    graph = _projection_graph(output)
    selector_kind = selector.get("field", {}).get("kind")
    annotation_slices = output.get("annotation_source_slices")
    projection_sources = [selector]
    if isinstance(annotation_slices, list):
        projection_sources.extend(
            dict(value) for value in annotation_slices if isinstance(value, dict)
        )
    graph["payload"] = {
        "kind": (
            "table_partition" if selector_kind == "table_rows" else "table_identity"
        ),
        "sources": projection_sources,
        "target_field": "payload",
        "transform": (
            "table_rows.v1"
            if selector_kind == "table_rows"
            else "table_identity.v1"
        ),
    }
    continuation_locators = output.get("continuation_source_locators")
    if isinstance(continuation_locators, list):
        graph["provenance"] = [
            *graph["provenance"],
            *(
                {
                    "kind": "table_continuation_ghost",
                    "source": continuation_source["source"],
                    "root": selector["source"],
                }
                for child in continuation_locators
                if isinstance(child, dict)
                and (
                    continuation_source := source_selector(child, field="table")
                )
                is not None
            ),
        ]
    output["source_projection"] = graph
    return output or None


def s2_apply_heading_tree(
    elements: Iterable[PreparedElement],
    *,
    qa_heading_mode: bool = False,
    toc_root_keys: frozenset[str] = frozenset(),
    toc_page_boundaries: tuple[TocPageBoundary, ...] = (),
    stats: BuildStats | None = None,
) -> list[PreparedElement]:
    elements = list(elements)
    # Per-document reliability gate (docs/implementation/design/
    # heading-level-arbitration.md): a layout backend that emits one constant
    # heading_level for the whole document proves nothing about depth, so only
    # a varied level set lets parser levels outrank the enumeration grammar in
    # the source_level arbitration below.
    parser_heading_levels = {
        element.heading_level
        for element in elements
        if element.kind == "heading" and element.heading_level
    }
    parser_levels_informative = len(parser_heading_levels) >= 2
    stack: list[_HeadingStackEntry] = []
    placed: list[PreparedElement] = []
    heading_occurrence_ids: set[int] = set()
    represented_heading_ids: set[int] = set()
    heading_carriers: dict[int, PreparedElement] = {}
    next_occurrence_id = 1
    pending_boundaries = list(toc_page_boundaries)
    for element in elements:
        # TOC-declared sections whose openers exist only as page-margin
        # furniture (side-tab layouts): the declared page boundary opens the
        # section, with provenance on the TOC entry line itself.  Informative
        # parser levels mean real structure exists — never synthesize there.
        while (
            pending_boundaries
            and not parser_levels_informative
            and element.page_no is not None
            and element.page_no >= pending_boundaries[0].pdf_page
        ):
            boundary = pending_boundaries.pop(0)
            stack = [
                _HeadingStackEntry(
                    logical_level=1,
                    title=boundary.title,
                    occurrence_id=next_occurrence_id,
                    source_level=1,
                    pattern_level=None,
                    artifact_locator=boundary.artifact_locator,
                    toc_proven=True,
                )
            ]
            next_occurrence_id += 1
            if stats is not None:
                stats.toc_page_boundaries_synthesized += 1
        text = (element.text or "").strip()
        if (
            qa_heading_mode
            and element.region_role == "attachment"
            and element.kind == "heading"
        ):
            # Official-form attachments are siblings of the narrative run.
            stack = []
        if (
            qa_heading_mode
            and element.region_role == "attachment"
            and element.kind == "table"
            and element.table_caption
        ):
            first_caption = str(element.table_caption[0]).strip()
            if first_caption:
                stack = [
                    _HeadingStackEntry(
                        logical_level=1,
                        title=first_caption,
                        occurrence_id=next_occurrence_id,
                        source_level=1,
                        pattern_level=None,
                        artifact_locator=element.artifact_locator,
                        projection_field="table_caption",
                        projection_index=0,
                    )
                ]
                next_occurrence_id += 1
        if (
            not parser_levels_informative
            and element.kind == "table"
            and element.table_caption
        ):
            section_key = _caption_section_key(
                str(element.table_caption[0]), toc_root_keys
            )
            if section_key is not None and not (
                stack and _section_continuation_key(stack[0].title) == section_key
            ):
                # A section title set flush against its first table gets
                # folded into that table's caption by the layout backend
                # (释义 / 第十节财务报告 / 附表：…); the caption is
                # parser-attributed structure carrying the lost section
                # boundary, so it opens the section the table and the
                # following elements belong to. A continuation caption
                # ("…（续）") of the already-open section never reopens it.
                stack = [
                    _HeadingStackEntry(
                        logical_level=1,
                        title=str(element.table_caption[0]).strip(),
                        occurrence_id=next_occurrence_id,
                        source_level=1,
                        pattern_level=None,
                        artifact_locator=element.artifact_locator,
                        projection_field="table_caption",
                        projection_index=0,
                        toc_proven=True,
                    )
                ]
                next_occurrence_id += 1
        clean_text, trailing_marker = rules.split_trailing_applicability_marker(text)
        heading_candidate = element
        marker_locator: dict[str, Any] | None = None
        if trailing_marker is not None:
            heading_locator = _prepared_text_slice_locator(element, clean_text)
            marker_locator = _prepared_text_slice_locator(element, trailing_marker)
            if heading_locator is None or marker_locator is None:
                # A logical split without two replayable physical slices would
                # turn a parser string into synthetic structure.  Keep the
                # original carrier intact and surface it for later review.
                clean_text = text
                trailing_marker = None
                heading_candidate = replace(element, quality_status="needs_review")
            else:
                heading_candidate = replace(
                    element,
                    text=clean_text,
                    source_text=clean_text,
                    artifact_locator=heading_locator,
                )
        level = _heading_level_for(heading_candidate)
        if level is not None:
            heading_text = heading_candidate.text or ""
            evidence = _heading_pattern_evidence(heading_text, stack=stack)
            pattern_level = evidence.level
            source_level, toc_proven = _arbitrated_source_level(
                heading_candidate,
                fallback_level=level,
                evidence=evidence,
                stack=stack,
                parser_levels_informative=parser_levels_informative,
                toc_root_keys=toc_root_keys,
            )
            dotted_parent_proven = False
            if not toc_proven and evidence.dotted_components is not None:
                components = evidence.dotted_components
                proven_parent = (
                    next(
                        (
                            stack[: index + 1]
                            for index in range(len(stack) - 1, -1, -1)
                            if stack[index].dotted_components == components[:-1]
                        ),
                        None,
                    )
                    if len(components) > 1
                    else None
                )
                if proven_parent is not None:
                    # Only a concrete parent occurrence in the active stack can
                    # own this child. A document-global numeric token cache can
                    # cross-link equal ``1.`` branches under different roots.
                    stack = list(proven_parent)
                    dotted_parent_proven = True
                else:
                    # No numeric-prefix proof means another numeric branch can
                    # never be used merely because its abstract depth is
                    # smaller (``1.`` is not the parent of orphan ``2.1``).
                    stack = _before_outline_family(stack, "dotted")
            if toc_proven:
                # A TOC-declared top-level section opens a fresh root no
                # matter what its own enumerator family would suggest.
                stack = []
                logical_level = 1
            elif pattern_level is None:
                stack, logical_level = _place_unnumbered_heading(
                    stack,
                    source_level=source_level,
                )
            elif dotted_parent_proven:
                logical_level = stack[-1].logical_level + 1
            else:
                stack, logical_level = _place_patterned_heading(
                    stack,
                    source_level=source_level,
                    pattern_level=pattern_level,
                )
            # A child heading carries every retained ancestor in its path, so
            # those ancestors remain addressable even without direct body text.
            represented_heading_ids.update(
                entry.occurrence_id for entry in stack
            )
            occurrence_id = next_occurrence_id
            stack.append(
                _HeadingStackEntry(
                    logical_level=logical_level,
                    title=heading_text,
                    occurrence_id=occurrence_id,
                    source_level=source_level,
                    pattern_level=pattern_level,
                    artifact_locator=heading_candidate.artifact_locator,
                    outline_family=evidence.family,
                    outline_ordinal=evidence.ordinal,
                    dotted_components=evidence.dotted_components,
                    toc_proven=toc_proven,
                )
            )
            next_occurrence_id += 1
            structural_path = [entry.title for entry in stack]
            section_path = [entry.occurrence_id for entry in stack]
            heading_occurrence_ids.add(occurrence_id)
            locator = _locator_with_heading_sources(
                _with_text_payload_projection(heading_candidate.artifact_locator),
                stack,
            ) or {}
            locator["derivation"] = {
                "kind": "heading_without_payload",
                "reason": "source_heading_has_no_descendant_payload",
            }
            heading_carriers[occurrence_id] = PreparedElement(
                kind="text",
                raw_kind=element.raw_kind,
                order_index=element.order_index,
                intra_order=element.intra_order,
                page_no=element.page_no,
                text=heading_text,
                quality_status=element.quality_status,
                artifact_locator=locator,
                heading_path=list(structural_path),
                structural_path=list(structural_path),
                section_path=list(section_path),
                title=heading_text,
                region_role=element.region_role,
                region_id=element.region_id,
            )
            if trailing_marker is not None:
                represented_heading_ids.update(section_path)
                placed.append(
                    PreparedElement(
                        kind="text",
                        raw_kind=element.raw_kind,
                        order_index=element.order_index,
                        intra_order=element.intra_order,
                        page_no=element.page_no,
                        text=trailing_marker,
                        source_text=trailing_marker,
                        quality_status=element.quality_status,
                        artifact_locator=_locator_with_heading_sources(
                            marker_locator,
                            stack,
                        ),
                        heading_path=_project_heading_path(structural_path),
                        structural_path=structural_path,
                        section_path=section_path,
                        title=structural_path[-1],
                        region_role=element.region_role,
                        region_id=element.region_id,
                    )
                )
            continue
        detached_furniture = not element.inherits_section
        owning_stack = [] if detached_furniture else stack
        structural_path = (
            [] if detached_furniture else [entry.title for entry in owning_stack]
        )
        section_path = (
            []
            if detached_furniture
            else [entry.occurrence_id for entry in owning_stack]
        )
        if not _is_empty_table_element(element):
            represented_heading_ids.update(section_path)
        heading_path = _project_heading_path(structural_path)
        title = element.title or (heading_path[-1] if heading_path else None)
        element_values = dict(element.__dict__)
        if element.kind == "heading":
            element_values["kind"] = "text"
        placed.append(
            PreparedElement(
                **{
                    **element_values,
                    "heading_path": heading_path,
                    "structural_path": structural_path,
                    "section_path": section_path,
                    "title": title,
                    "artifact_locator": _locator_with_heading_sources(
                        element.artifact_locator,
                        owning_stack,
                    ),
                }
            )
        )
    # A non-empty source heading is evidence even when the branch has no body.
    # Preserve orphan leaves now; a later stage groups sibling leaves into a
    # searchable outline unit so we do not trade content conservation for a
    # cloud of one-line public fragments.
    orphan_ids = heading_occurrence_ids - represented_heading_ids
    if stats is not None:
        stats.heading_only_carriers_preserved += len(orphan_ids)
    placed.extend(heading_carriers[item] for item in orphan_ids)
    return sorted(placed, key=lambda item: (item.order_index, item.intra_order))


@dataclass(frozen=True)
class TocPageBoundary:
    """A TOC-declared section boundary for a body without in-flow headings.

    Some designed reports carry section names only as page-margin furniture
    (side tabs): the TOC declares "第一节 释义 …… 3" but no heading element
    ever opens the section in the reading flow.  The declared entry plus the
    printed→PDF page alignment (page-number furniture) is then the only real
    evidence of the boundary; the opened section's provenance points at the
    TOC entry line itself — no synthetic content, only a tree boundary.
    """

    title: str
    pdf_page: int
    artifact_locator: dict[str, Any] | None


def _printed_page_map(
    raw_elements: Iterable[Mapping[str, Any]],
) -> dict[int, int]:
    """Map printed page numbers to PDF pages from page-number furniture.

    Runs over the raw parser elements: S1 legitimately drops page-number
    furniture from the unit stream, but the numbers are the only alignment
    evidence between TOC-declared printed pages and PDF pages.
    """

    mapping: dict[int, int] = {}
    for element in raw_elements:
        if (
            element.get("kind") == "page_furniture"
            and element.get("raw_kind") == "page_number"
        ):
            text = str(element.get("text") or "").strip()
            page_no = _int_or_none(element.get("page_no"))
            # isascii guard: "③".isdigit() is True but int("③") raises.
            if text.isascii() and text.isdigit() and page_no is not None:
                mapping.setdefault(int(text), page_no)
    return mapping


def _toc_page_boundaries(
    pages: dict[int, list[PreparedElement]],
    qualified_pages: set[int],
    elements: list[PreparedElement],
    *,
    demoted_ids: set[int],
    printed_map: Mapping[int, int],
) -> tuple[TocPageBoundary, ...]:
    """Bind TOC-declared paged entries to PDF-page boundaries (fail closed).

    Only entries whose stripped title matches NO heading anywhere in the
    document qualify (a matching heading means the normal TOC pin places the
    section; demoted TOC-entry headings never claim); every boundary needs
    direct page-number-furniture evidence for its printed page, and the
    resulting sequence must be strictly increasing — a second section
    starting on an already-claimed PDF page is dropped rather than guessed.
    """

    claimed: set[str] = set()
    for element in elements:
        if (
            element.kind == "heading"
            and element.text
            and id(element) not in demoted_ids
        ):
            claimed.add(
                toc_outline.normalize_section_title(
                    toc_outline.strip_outline_enumerator(element.text)
                )
            )
    candidates: list[TocPageBoundary] = []
    for page in sorted(qualified_pages):
        for element in pages.get(page, []):
            if not element.text:
                continue
            entries, _unparsed = toc_outline.parse_toc_entries(
                element.text, include_bare=True
            )
            for entry in entries:
                if entry.page is None:
                    continue
                key = toc_outline.normalize_section_title(
                    toc_outline.strip_outline_enumerator(entry.title)
                )
                if len(key) < 2 or key in claimed:
                    continue
                pdf_page = printed_map.get(entry.page)
                if pdf_page is None:
                    continue
                locator = _prepared_text_slice_locator(element, entry.title)
                if locator is None:
                    continue
                candidates.append(
                    TocPageBoundary(
                        title=entry.title,
                        pdf_page=pdf_page,
                        artifact_locator=locator,
                    )
                )
    ordered = sorted(candidates, key=lambda b: b.pdf_page)
    deduped: list[TocPageBoundary] = []
    for boundary in ordered:
        if deduped and boundary.pdf_page <= deduped[-1].pdf_page:
            continue
        deduped.append(boundary)
    return tuple(deduped)


def _front_matter_toc_scan(
    elements: list[PreparedElement],
    *,
    stats: BuildStats,
    printed_map: Mapping[int, int] | None = None,
) -> tuple[
    list[PreparedElement], frozenset[str], tuple[TocPageBoundary, ...]
]:
    """Scan front matter for TOC pages; demote TOC-entry headings.

    Per the parser output contract a TOC page's titles may arrive as text
    or heading elements, with page numbers inline or in a detached column,
    so both kinds join the per-page block (one element per TOC line never
    reaches the TOC-shape threshold alone; the page is the natural block).
    Bare-entry and weak-enumeration grammars apply only to pages anchored
    by their own 目录 marker (this page or the one before, for wrapped
    TOCs).  On a page that qualified as a TOC, heading elements whose own
    line reads "title + page number" are the TOC's entries, not section
    openers, and are demoted to text so they never enter the heading tree.
    """

    pages: dict[int, list[PreparedElement]] = {}
    marker_pages: set[int] = set()
    for element in elements:
        page = element.page_no or 0
        if element.kind in ("text", "heading") and element.text and page <= 30:
            pages.setdefault(page, []).append(element)
            if toc_outline.is_toc_marker(element.text.strip()):
                marker_pages.add(page)

    keys: set[str] = set()
    demoted_ids: set[int] = set()
    qualified_pages: set[int] = set()
    for page, page_elements in sorted(pages.items()):
        anchored = page in marker_pages or (page - 1) in marker_pages
        block = "\n".join(
            element.text
            for element in page_elements
            if element.text and not toc_outline.is_toc_marker(element.text.strip())
        )
        analysis = toc_outline.analyze_toc_block(
            block, marker_anchored=anchored
        )
        keys.update(analysis.keys)
        if analysis.qualified:
            qualified_pages.add(page)
            for element in page_elements:
                if (
                    element.kind == "heading"
                    and element.text
                    and toc_outline.is_page_annotated_entry(element.text)
                ):
                    demoted_ids.add(id(element))

    boundaries = _toc_page_boundaries(
        pages,
        qualified_pages,
        elements,
        demoted_ids=demoted_ids,
        printed_map=printed_map or {},
    )
    if not demoted_ids:
        return elements, frozenset(keys), boundaries
    demoted_elements = [
        (
            replace(element, kind="text", heading_level=None)
            if id(element) in demoted_ids
            else element
        )
        for element in elements
    ]
    stats.toc_entry_headings_demoted += len(demoted_ids)
    return demoted_elements, frozenset(keys), boundaries


def _section_continuation_key(title: str) -> str:
    """Continuation-tolerant identity key ("第十节财务报告（续）" == "…报告")."""

    key = toc_outline.normalize_section_title(
        toc_outline.strip_section_enumerator(title)
    )
    return key[:-1] if key.endswith("续") else key


def _caption_section_key(
    caption: str, toc_root_keys: frozenset[str]
) -> str | None:
    """Identity key when a table caption is a swallowed section title.

    Strong evidence only: a statutory 第X章/第X节 enumerator on the caption
    itself, a FIXED_L1 title, or a TOC-declared top-level section name.
    Ordinary captions (单位：元, "(1) 商誉账面原值") never qualify.
    """

    text = caption.strip()
    if not text:
        return None
    key = _section_continuation_key(text)
    if not key:
        return None
    if toc_outline.strip_section_enumerator(text) != text:
        return key
    if _normalized_title(text) in rules.FIXED_L1_TITLES:
        return key
    normalized = toc_outline.normalize_section_title(
        toc_outline.strip_outline_enumerator(text)
    )
    if normalized in toc_root_keys:
        return key
    return None


def _arbitrated_source_level(
    heading_candidate: PreparedElement,
    *,
    fallback_level: int,
    evidence: _HeadingPatternEvidence,
    stack: list[_HeadingStackEntry],
    parser_levels_informative: bool,
    toc_root_keys: frozenset[str] = frozenset(),
) -> tuple[int, bool]:
    """Pick the source level from the most reliable available depth signal.

    Informative parser levels stay authoritative. In the degenerate regime
    (one constant level document-wide) the grammar evidence level is the
    only honest depth — including dotted chains ("1.2"), which match no
    HEADING_PATTERNS entry and would otherwise keep the meaningless parser
    constant. A heading with no depth evidence at all anchors to the stack
    top instead of resetting to root: below a top that proved its depth
    (grammar/dotted), beside a top that could not (FIXED anchors and other
    anchored headings); an empty stack keeps the honest root for
    document-front matter.
    """

    if parser_levels_informative:
        return (
            max(1, min(7, heading_candidate.heading_level or fallback_level)),
            False,
        )
    if toc_root_keys:
        # The document's own TOC is the strongest signal in the degenerate
        # regime and outranks generic enumeration conventions: a body
        # heading whose top-level-stripped title the TOC declares as a
        # top-level section is proven top-level even when its own
        # enumerator ("1. 释义", "一、基本情况") would grammar-map deeper.
        toc_key = toc_outline.normalize_section_title(
            toc_outline.strip_outline_enumerator(heading_candidate.text or "")
        )
        if toc_key in toc_root_keys:
            return 1, True
    if evidence.level is not None:
        return max(1, min(7, evidence.level)), False
    title = _normalized_title(heading_candidate.text or "")
    if (
        evidence.dotted_components is None
        and title not in rules.FIXED_L1_TITLES
        and stack
    ):
        top_entry = stack[-1]
        top_has_depth_evidence = (
            top_entry.pattern_level is not None
            or top_entry.dotted_components is not None
            or top_entry.toc_proven
        )
        return (
            min(7, top_entry.source_level + (1 if top_has_depth_evidence else 0)),
            False,
        )
    return max(1, min(7, fallback_level)), False


def _place_unnumbered_heading(
    stack: list[_HeadingStackEntry],
    *,
    source_level: int,
) -> tuple[list[_HeadingStackEntry], int]:
    """Place an unnumbered parser heading using parser hierarchy evidence.

    Numbered headings may have been flattened by the parser and therefore use
    a deeper logical level than their source level. An unnumbered source-level
    child must remain below that parent, while equal source levels are siblings.
    """

    parents = [entry for entry in stack if entry.source_level < source_level]
    same_level = [entry for entry in stack if entry.source_level == source_level]
    parent_level = parents[-1].logical_level if parents else 0
    sibling_level = max(
        (entry.logical_level for entry in same_level),
        default=0,
    )
    return parents, max(source_level, parent_level + 1, sibling_level)


def _place_patterned_heading(
    stack: list[_HeadingStackEntry],
    *,
    source_level: int,
    pattern_level: int,
) -> tuple[list[_HeadingStackEntry], int]:
    """Place a numbered heading without discarding parser-proven parents.

    Numbering is useful when a parser flattens siblings to one source level,
    but it is weaker than an explicit parser parent/child relation.  Retain the
    deepest stack prefix supported by either source hierarchy or numbering,
    then place the new node below that prefix.  This also handles a flattened
    ``一、`` -> ``（一）`` sequence without making numbering authoritative over
    a real h1/h2/h3 tree.
    """

    parent_index: int | None = None
    for index, entry in enumerate(stack):
        if (
            entry.source_level < source_level
            or entry.logical_level < pattern_level
        ):
            parent_index = index
    parents = stack[: parent_index + 1] if parent_index is not None else []
    parent_level = parents[-1].logical_level if parents else 0
    return parents, max(pattern_level, parent_level + 1)


def _project_heading_path(structural_path: list[str]) -> list[str]:
    """Persist the complete source breadcrumb on the public unit.

    Display clients may abbreviate a long breadcrumb, but the L1 evidence
    contract must not discard intermediate headings from the searchable path.
    """

    return list(structural_path)


def _locator_with_heading_sources(
    locator: dict[str, Any] | None,
    stack: list[_HeadingStackEntry],
) -> dict[str, Any] | None:
    """Attach source locators for every heading represented in a unit path.

    Headings with body content are structural carriers rather than duplicate
    payload units.  Their provenance still has to survive on the descendant
    unit that publishes the heading text in ``heading_path``.  Keeping these
    locators separate from payload ``source_locators`` makes the distinction
    explicit and preserves any exact-dedup lineage on a canonical heading.
    """

    output = dict(locator or {})
    heading_sources: list[dict[str, Any]] = []
    heading_projection: list[dict[str, Any]] = []
    heading_provenance: list[dict[str, Any]] = []
    for target_index, entry in enumerate(stack):
        source = dict(entry.artifact_locator or {})
        if not source:
            continue
        source["heading_text"] = entry.title
        source["source_field"] = entry.projection_field
        if entry.projection_index is not None:
            source["source_index"] = entry.projection_index
        heading_sources.append(source)
        if entry.projection_field == "text":
            projected_source = _with_text_payload_projection(source)
            source_graph = _projection_graph(dict(projected_source or {}))
            payload_projection = source_graph.get("payload")
            selectors = (
                [
                    dict(selector)
                    for selector in payload_projection.get("sources", [])
                    if isinstance(selector, dict)
                ]
                if isinstance(payload_projection, dict)
                else []
            )
            heading_provenance = _merged_projection_entries(
                heading_provenance,
                source_graph["provenance"],
            )
            if selectors:
                heading_projection.append(
                    {
                        "target_index": target_index,
                        "kind": (
                            "source_concat"
                            if len(selectors) > 1
                            else "source_field"
                        ),
                        **(
                            {"sources": selectors}
                            if len(selectors) > 1
                            else {"selector": selectors[0]}
                        ),
                        "transform": "clean_text.v1",
                    }
                )
                continue
        selector = _selector_for_locator(
            source,
            fallback_field=entry.projection_field,
            index=entry.projection_index,
        )
        if selector is not None:
            heading_projection.append(
                {
                    "target_index": target_index,
                    "kind": "source_field",
                    "selector": selector,
                    "transform": "clean_text.v1",
                }
            )
    if heading_sources:
        output["heading_source_locators"] = heading_sources
    projected = _with_heading_projection(output, heading_projection)
    if projected is None:
        return None
    graph = _projection_graph(projected)
    graph["provenance"] = _merged_projection_entries(
        graph["provenance"],
        heading_provenance,
    )
    projected["source_projection"] = graph
    return projected


def _unit_section_path(unit: UnitDraft) -> list[str]:
    """Return the lossless source hierarchy used for identity decisions."""

    return unit.structural_path or unit.heading_path


def _unit_section_identity(unit: UnitDraft) -> tuple[object, ...]:
    """Return a concrete section identity, with a legacy textual fallback."""

    if unit.section_path:
        return ("occurrence", *unit.section_path)
    return ("path", *_unit_section_path(unit))


def _prepared_section_identity(element: PreparedElement) -> tuple[object, ...]:
    if element.section_path:
        return ("occurrence", *element.section_path)
    return ("path", *(element.structural_path or element.heading_path))


_CN_NUMERALS = {c: i for i, c in enumerate("零一二三四五六七八九", start=0)}


def _cn_ordinal(text: str) -> int | None:
    """Parse 一/十/十二/二十一/百-scale Chinese ordinals."""

    if not text:
        return None
    total, unit_value, digit = 0, 0, 0
    for char in text:
        if char in _CN_NUMERALS:
            digit = _CN_NUMERALS[char]
        elif char == "十":
            unit_value = (digit or 1) * 10
            total += unit_value
            digit = 0
        elif char == "百":
            total += (digit or 1) * 100
            digit = 0
        else:
            return None
    return total + digit or None


_ORDINAL_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^第([一二三四五六七八九十百]+)[节章]"),
    re.compile(r"^([一二三四五六七八九十百]+)、"),
    re.compile(r"^[（(]([一二三四五六七八九十百]+)[）)]"),
    re.compile(r"^[（(](\d+)[）)]"),
    re.compile(r"^(\d+)[.、．)）\s]"),
)


def _heading_ordinal(text: str) -> int | None:
    for pattern in _ORDINAL_RES:
        match = pattern.match(text)
        if match:
            token = match.group(1)
            return int(token) if token.isdigit() else _cn_ordinal(token)
    return None


def _heading_pattern_evidence(
    text: str, *, stack: list[_HeadingStackEntry]
) -> _HeadingPatternEvidence:
    dotted = rules.DOTTED_CHAIN_HEADING_RE.match(text)
    if dotted is None:
        dotted = rules.DOT_NUMBER_HEADING_RE.match(text)
    if dotted is not None:
        components = tuple(
            int(value)
            for value in re.split(r"[.．]", dotted.group("token"))
        )
        return _HeadingPatternEvidence(
            level=4 + len(components),
            family="dotted",
            ordinal=components[-1],
            dotted_components=components,
        )
    alpha = rules.PAREN_ALPHA_HEADING_RE.match(text)
    if alpha is not None:
        token = alpha.group("token").lower()
        latin_ordinal = ord(token) - ord("a") + 1 if len(token) == 1 else None
        roman_ordinal = _roman_ordinal(token)
        last = stack[-1] if stack else None
        if (
            latin_ordinal is not None
            and last is not None
            and last.outline_family == "latin"
            and last.outline_ordinal is not None
            and latin_ordinal == last.outline_ordinal + 1
        ):
            # ``(h)`` -> ``(i)`` is an alphabetic sibling run; the same glyph
            # must not be forced into Roman depth merely by spelling.
            return _HeadingPatternEvidence(
                last.logical_level, "latin", latin_ordinal
            )
        if (
            roman_ordinal is not None
            and last is not None
            and last.outline_family == "roman"
            and last.outline_ordinal is not None
            and roman_ordinal == last.outline_ordinal + 1
        ):
            return _HeadingPatternEvidence(
                last.logical_level, "roman", roman_ordinal
            )
        if roman_ordinal == 1 and last is not None:
            # ``(i)`` after ``(h)`` was already consumed by the proven Latin
            # sequence branch above.  In every other active outline it opens
            # the Roman child run, including direct ``1.1 -> (i) -> (ii)``.
            # This avoids making the following ``(ii)`` a child of a falsely
            # Latin ``(i)``.
            return _HeadingPatternEvidence(
                last.logical_level + 1, "roman", roman_ordinal
            )
        if len(token) > 1 and roman_ordinal is not None:
            latin_parent = next(
                (entry for entry in reversed(stack) if entry.outline_family == "latin"),
                None,
            )
            return _HeadingPatternEvidence(
                (latin_parent.logical_level + 1) if latin_parent else None,
                "roman",
                roman_ordinal,
            )
        if latin_ordinal is not None:
            non_clause_parent = next(
                (
                    entry
                    for entry in reversed(stack)
                    if entry.outline_family not in {"latin", "roman"}
                ),
                stack[-1] if stack else None,
            )
            return _HeadingPatternEvidence(
                (non_clause_parent.logical_level + 1)
                if non_clause_parent is not None
                else 1,
                "latin",
                latin_ordinal,
            )
    for index, (level, pattern) in enumerate(rules.HEADING_PATTERNS):
        if pattern.match(text):
            families = (
                "cn_section",
                "cn_dunhao",
                "cn_paren",
                "digit_dunhao",
                "digit_dot",
                "digit_paren",
                "circled",
            )
            return _HeadingPatternEvidence(level, families[index])
    return _HeadingPatternEvidence(None)


def _before_outline_family(
    stack: list[_HeadingStackEntry], family: str
) -> list[_HeadingStackEntry]:
    for index, entry in enumerate(stack):
        if entry.outline_family == family:
            return stack[:index]
    return stack


def _roman_ordinal(token: str) -> int | None:
    values = {"i": 1, "v": 5, "x": 10}
    if not token or any(char not in values for char in token):
        return None
    total = 0
    previous = 0
    for char in reversed(token):
        value = values[char]
        if value < previous:
            total -= value
        else:
            total += value
            previous = value
    if not 1 <= total <= 39:
        return None
    ones = ("", "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix")
    canonical = "x" * (total // 10) + ones[total % 10]
    return total if canonical == token else None


def s3_build_text_units(
    elements: Iterable[PreparedElement], *, stats: BuildStats | None = None
) -> list[UnitDraft]:
    units: list[UnitDraft] = []
    buffer: list[PreparedElement] = []

    def flush() -> None:
        if not buffer:
            return
        text = "\n".join(item.text or "" for item in buffer if item.text).strip()
        applicability = next(
            (item.applicability for item in buffer if item.applicability is not None),
            None,
        )
        if text:
            quality = (
                "needs_review"
                if any(item.quality_status == "needs_review" for item in buffer)
                else "ok"
            )
            units.append(
                UnitDraft(
                    payload_kind="text",
                    payload={"text": text},
                    source_order=buffer[0].order_index,
                    intra_order=buffer[0].intra_order,
                    heading_path=list(buffer[0].heading_path),
                    structural_path=list(buffer[0].structural_path),
                    section_path=list(buffer[0].section_path),
                    title=buffer[0].title,
                    quality_status=quality,
                    applicability=applicability,
                    artifact_locator=_prepared_group_locator(buffer),
                    region_role=buffer[0].region_role,
                    region_id=buffer[0].region_id,
                    source_segments=[
                        (item.text or "", item.artifact_locator)
                        for item in buffer
                        if item.text
                    ],
                )
            )
        elif applicability == "applicable":
            # Placeholder so _sink_leading_applicable can move the flag onto
            # the section content that follows (usually a table).
            units.append(
                UnitDraft(
                    payload_kind="text",
                    payload={"text": ""},
                    source_order=buffer[0].order_index,
                    intra_order=buffer[0].intra_order,
                    heading_path=list(buffer[0].heading_path),
                    structural_path=list(buffer[0].structural_path),
                    section_path=list(buffer[0].section_path),
                    title=buffer[0].title,
                    applicability="applicable",
                    artifact_locator=_prepared_group_locator(buffer),
                    region_role=buffer[0].region_role,
                    region_id=buffer[0].region_id,
                )
            )
        buffer.clear()

    for element in elements:
        if element.kind == "text" and element.payload is None:
            cleaned_text, applicability = _strip_declaration_lines(
                element.text or "", stats=stats
            )
            cleaned_locator = element.artifact_locator
            cleaned_quality = element.quality_status
            if (
                applicability is not None
                and cleaned_text
                and cleaned_text != (element.text or "")
            ):
                residual_locator = _prepared_text_slice_locator(
                    element, cleaned_text
                )
                if residual_locator is None:
                    # Structured extraction may not detach text from its
                    # physical carrier unless the residual is replayable.
                    # Keep the complete source visible and defer the marker
                    # interpretation instead of publishing unverifiable
                    # cleaned text.
                    cleaned_text = element.text or ""
                    applicability = None
                    cleaned_quality = "needs_review"
                else:
                    cleaned_locator = residual_locator
            if applicability is not None and stats is not None:
                _record_source_disposition(
                    stats,
                    element.artifact_locator,
                    role="structured_applicability",
                    reason="explicit_source_marker",
                    replacement_text=cleaned_text,
                    value=applicability,
                )
            normalized = replace(
                element,
                text=cleaned_text,
                applicability=applicability,
                quality_status=cleaned_quality,
                artifact_locator=cleaned_locator,
            )
            if applicability == "applicable" and not cleaned_text:
                marker_locator = dict(normalized.artifact_locator or {})
                marker_locator["applicability_marker_text"] = element.text or ""
                normalized = replace(
                    normalized,
                    artifact_locator=marker_locator or None,
                )
            if applicability is not None:
                # A declaration has carrier-local scope.  It is a hard
                # segment boundary even when adjacent text has the same
                # section identity; coalescing first loses later markers.
                flush()
                buffer.append(normalized)
                if not (
                    applicability == "not_applicable"
                    and rules.is_pure_marker_line(cleaned_text)
                ):
                    flush()
                continue
            same_section = not buffer or (
                _prepared_section_identity(normalized)
                == _prepared_section_identity(buffer[-1])
                and normalized.region_role == buffer[-1].region_role
                and normalized.region_id == buffer[-1].region_id
            )
            if buffer and not same_section:
                flush()
            buffer.append(normalized)
        else:
            flush()
            if element.kind == "text" and element.payload is not None:
                units.append(
                    UnitDraft(
                        payload_kind="text",
                        payload=element.payload,
                        source_order=element.order_index,
                        intra_order=element.intra_order,
                        heading_path=list(element.heading_path),
                        structural_path=list(element.structural_path),
                        section_path=list(element.section_path),
                        title=element.title,
                        quality_status=element.quality_status,
                        artifact_locator=element.artifact_locator,
                        region_role=element.region_role,
                        region_id=element.region_id,
                    )
                )
    flush()
    return units


def _prepared_group_locator(
    elements: list[PreparedElement],
) -> dict[str, Any] | None:
    """Compose typed text ownership separately from navigation lineage."""

    locators = [dict(item.artifact_locator or {}) for item in elements]
    if not any(locators):
        return None
    if len(elements) == 1:
        return _with_text_payload_projection(locators[0] or None)
    projected_locators = [
        dict(_with_text_payload_projection(locator or None) or {})
        for locator in locators
    ]
    graphs = [_projection_graph(locator) for locator in projected_locators]
    payload_sources: list[dict[str, Any]] = []
    for graph in graphs:
        payload_projection = graph.get("payload")
        if not isinstance(payload_projection, dict):
            continue
        payload_sources.extend(
            dict(selector)
            for selector in payload_projection.get("sources", [])
            if isinstance(selector, dict)
        )
    first = dict(locators[0])
    first.pop("derivation", None)
    first["derivation"] = {
        "kind": "coalesced_text_carriers",
        "reason": "consecutive_source_text_in_one_section",
    }
    first["source_order_span"] = [
        min(item.order_index for item in elements),
        max(item.order_index for item in elements),
    ]
    first["source_locators"] = locators
    pages = [item.page_no for item in elements if item.page_no is not None]
    if pages and min(pages) != max(pages):
        first["page_span"] = [min(pages), max(pages)]
    graph = empty_projection_graph()
    graph["payload"] = (
        {
            "kind": (
                "text_concat" if len(payload_sources) > 1 else "text_identity"
            ),
            "sources": payload_sources,
            "target_field": "payload.text",
            "transform": "clean_text.v1",
        }
        if payload_sources
        else None
    )
    graph["heading_path"] = list(graphs[0]["heading_path"])
    graph["structured"] = _merged_projection_entries(
        *(value["structured"] for value in graphs)
    )
    graph["provenance"] = _merged_projection_entries(
        *(value["provenance"] for value in graphs)
    )
    first["source_projection"] = graph
    return first




def s5_build_table_units(
    elements: Iterable[PreparedElement], stats: BuildStats
) -> list[UnitDraft]:
    items = list(elements)
    units: list[UnitDraft] = []
    index = 0
    while index < len(items):
        element = items[index]
        if element.kind != "table":
            index += 1
            continue
        if _is_empty_table_element(element):
            stats.dropped_by_kind["table_empty"] += 1
            index += 1
            continue
        group = [element]
        index += 1
        while index < len(items):
            candidate = items[index]
            if _is_empty_table_element(candidate):
                stats.dropped_by_kind["table_empty"] += 1
                index += 1
                continue
            break
        previous_text, previous_locator = _previous_text_before(items, element)
        units.append(
            _table_group_to_unit(
                group,
                previous_text=previous_text,
                previous_locator=previous_locator,
                stats=stats,
            )
        )
    return units


def s6_filter_units(units: Iterable[UnitDraft], stats: BuildStats) -> list[UnitDraft]:
    kept: list[UnitDraft] = []
    for unit in units:
        if unit.payload_kind == "table" and _table_payload_is_empty(unit.payload):
            stats.dropped_by_kind["table_empty"] += 1
            continue
        kept.append(unit)
    return kept


def _group_heading_only_evidence(
    units: list[UnitDraft],
    *,
    filing_type: str | None,
    document_title: str | None,
    stats: BuildStats,
) -> list[UnitDraft]:
    """Group empty-branch headings into parent-scoped outline evidence.

    Structure-aware chunkers normally attach headings to descendant chunks.
    An empty leaf has no such descendant, so dropping it would remove a source
    fact while publishing it as an ordinary text unit would create a tiny
    fragment.  We instead publish one outline unit per concrete parent
    occurrence.  Every heading part retains its full path and source locator.
    """

    output: list[UnitDraft] = []
    pending: list[UnitDraft] = []
    pending_parent: tuple[object, ...] | None = None

    def parent_identity(unit: UnitDraft) -> tuple[object, ...]:
        if unit.section_path:
            return ("occurrence", *unit.section_path[:-1])
        return ("path", *_unit_section_path(unit)[:-1])

    def flush() -> None:
        nonlocal pending, pending_parent
        if not pending:
            return
        if len(pending) == 1:
            output.append(pending[0])
            pending = []
            pending_parent = None
            return
        members = pending
        pending = []
        pending_parent = None
        parent_path = _unit_section_path(members[0])[:-1]
        if not parent_path and not document_title:
            # Root siblings have no proven common parent.  Grouping them under
            # the first sibling would fabricate a hierarchy; preserve the
            # individual evidence carriers instead.
            output.extend(members)
            return
        anchor = parent_path[-1] if parent_path else document_title
        public_path = list(parent_path) or ([anchor] if anchor else [])
        parent_section_path = (
            list(members[0].section_path[:-1])
            if members[0].section_path
            else []
        )
        parts: list[dict[str, Any]] = []
        for member in members:
            part = _unit_part(member, include_heading=True)
            part["role"] = "heading"
            if member.title:
                part["title"] = member.title
            parts.append(part)
        locator = _mixed_locator(members) or {}
        locator["derivation"] = {
            "kind": "heading_outline",
            "reason": "consecutive_source_headings_have_no_descendant_payload",
        }
        locator["source_order_span"] = [
            min(member.source_order for member in members),
            max(member.source_order for member in members),
        ]
        locator["source_locators"] = [
            dict(member.artifact_locator or {}) for member in members
        ]
        locator = _with_payload_projection(
            locator,
            {
                "kind": "container",
                "sources": [],
                "target_field": "payload.parts",
                "transform": "ordered_parts.v1",
            },
        ) or locator
        if parent_path:
            member_graph = _projection_graph(
                dict(members[0].artifact_locator or {})
            )
            parent_projection = list(member_graph["heading_path"])[
                : len(parent_path)
            ]
        elif document_title:
            parent_projection = [
                {
                    "target_index": 0,
                    "kind": "document_metadata",
                    "field": "title",
                }
            ]
        else:
            parent_projection = []
        locator = _with_heading_projection(locator, parent_projection) or locator
        output.append(
            UnitDraft(
                payload_kind="mixed",
                payload={
                    "semantic_type": "section",
                    "parts": parts,
                },
                source_order=members[0].source_order,
                intra_order=members[0].intra_order,
                heading_path=public_path,
                structural_path=public_path,
                section_path=parent_section_path,
                title=anchor,
                semantic_keys=_member_semantic_keys(
                    members,
                    filing_type=filing_type,
                ),
                quality_status=_worst_quality(members),
                artifact_locator=locator,
            )
        )
        stats.heading_outline_units_generated += 1

    for unit in units:
        if not _is_heading_only_evidence(unit):
            flush()
            output.append(unit)
            continue
        identity = parent_identity(unit)
        if pending and identity != pending_parent:
            flush()
        pending.append(unit)
        pending_parent = identity
    flush()
    return output


def _is_heading_only_evidence(unit: UnitDraft) -> bool:
    derivation = (unit.artifact_locator or {}).get("derivation")
    return bool(
        unit.payload_kind == "text"
        and isinstance(derivation, dict)
        and derivation.get("kind") == "heading_without_payload"
    )


def s7_finalize_units(
    units: Iterable[UnitDraft],
    *,
    filing_type: str | None,
    document_title: str | None = None,
    stats: BuildStats,
) -> list[UnitDraft]:
    filing_keys = (
        ("investor_communication",)
        if filing_type in {"investor_relations", "performance_briefing"}
        else ()
    )
    finalized: list[UnitDraft] = []
    for unit in units:
        note_keys = _note_keys_for_unit(unit)
        matched_keys = semantic_keys_for_unit(unit, filing_type=filing_type)
        candidates = _stable_semantic_keys(
            [unit.semantic_key] if unit.semantic_key else [],
            matched_keys,
            note_keys,
            filing_keys,
            unit.semantic_keys or [],
        )
        semantic_key = candidates[0] if candidates else rules.SEMANTIC_FALLBACK_KEY
        semantic_keys = candidates or [rules.SEMANTIC_FALLBACK_KEY]
        quality_status = _final_quality_status(unit)
        if quality_status == "needs_review":
            stats.needs_review_count += 1
        if quality_status == "unusable":
            stats.unusable_count += 1
        stats.generated_by_kind[unit.payload_kind] += 1
        finalized.append(
            UnitDraft(
                **{
                    **unit.__dict__,
                    "semantic_key": semantic_key,
                    # New output always has at least the controlled
                    # document_content fallback, so scalar and array expose
                    # one consistent non-empty retrieval state.
                    "semantic_keys": semantic_keys,
                    "quality_status": quality_status,
                }
            )
        )
    return finalized


def _stable_semantic_keys(*groups: Iterable[str]) -> list[str]:
    keys: list[str] = []
    for group in groups:
        for key in group:
            if key and key not in keys:
                keys.append(key)
    return keys


def build_unit_drafts_s1_s7(
    normalized_ir: dict[str, Any],
    *,
    filing_type: str | None,
    document_title: str | None = None,
    security_code: str | None = None,
    security_name: str | None = None,
    image_bytes_resolver: ImageBytesResolver | None = None,
) -> tuple[list[UnitDraft], BuildStats]:
    qa_mode = filing_type in {"investor_relations", "performance_briefing"}
    projection = project_official_ir_form(
        normalized_ir.get("elements", []),
        filing_type=filing_type,
    )
    s1 = s1_preprocess_elements(
        projection.elements,
        image_bytes_resolver=image_bytes_resolver,
    )
    s1.stats.official_ir_form_status = projection.status
    s1.stats.official_ir_projected_carriers = projection.projected_carriers
    s1.stats.official_ir_reason_code = projection.reason_code
    elements = _merge_registered_cover_title_fragments(
        s1.elements,
        document_title=document_title,
        stats=s1.stats,
    )
    elements = _deduplicate_registered_security_headers(
        elements,
        security_code=security_code,
        security_name=security_name,
        document_title=document_title,
        stats=s1.stats,
    )
    elements, toc_root_keys, toc_page_boundaries = _front_matter_toc_scan(
        elements,
        stats=s1.stats,
        printed_map=_printed_page_map(normalized_ir.get("elements", [])),
    )
    placed = s2_apply_heading_tree(
        elements,
        qa_heading_mode=qa_mode,
        toc_root_keys=toc_root_keys,
        toc_page_boundaries=toc_page_boundaries,
        stats=s1.stats,
    )
    # QA discrimination was removed: transcripts
    # stay raw text units with full provenance; question/answer semantics are
    # not an L1 concern and no payload_kind="qa" is emitted anymore.
    text_units = s3_build_text_units(placed, stats=s1.stats)
    table_units = s5_build_table_units(placed, s1.stats)
    units = sorted([*text_units, *table_units], key=_unit_sort_key)
    units = _sink_leading_applicable(units)
    kept = s6_filter_units(units, s1.stats)
    kept = _group_heading_only_evidence(
        kept,
        filing_type=filing_type,
        document_title=document_title,
        stats=s1.stats,
    )
    kept = _anchor_headerless_units(kept, document_title=document_title, stats=s1.stats)
    return (
        s7_finalize_units(
            kept,
            filing_type=filing_type,
            document_title=document_title,
            stats=s1.stats,
        ),
        s1.stats,
    )


def _deduplicate_registered_security_headers(
    elements: list[PreparedElement],
    *,
    security_code: str | None,
    security_name: str | None,
    document_title: str | None,
    stats: BuildStats,
) -> list[PreparedElement]:
    """Remove only page-header values equal to registered document metadata."""

    output: list[PreparedElement] = []
    for element in elements:
        text = (element.text or "").strip()
        match = rules.match_registered_security_header(
            text,
            security_code=security_code,
            security_name=security_name,
            document_title=document_title,
        )
        if (
            element.kind not in {"text", "heading"}
            or element.raw_kind not in {"header", "page_header"}
            or match is None
        ):
            output.append(element)
            continue
        if element.page_no != 1:
            output.append(replace(element, quality_status="needs_review"))
            continue
        residual_locator: dict[str, Any] | None = None
        if match.replacement:
            residual_locator = _prepared_text_slice_locator(
                element,
                match.replacement,
            )
            if residual_locator is None:
                # The metadata match may be valid while cleanup changed string
                # offsets.  Without a unique source slice, retaining the whole
                # carrier is safer than publishing an unprovable residual.
                output.append(replace(element, quality_status="needs_review"))
                continue
        stats.deduplicated_registered_header_lines += match.metadata_value_count
        _record_source_disposition(
            stats,
            element.artifact_locator,
            role=(
                "partial_external_metadata"
                if match.replacement
                else "external_metadata"
            ),
            reason="registered_security_header",
            replacement_text=match.replacement,
        )
        if match.replacement:
            locator = dict(residual_locator or {})
            locator["derivation"] = {
                "kind": "registered_header_metadata_deduplicated",
                "reason": "exact_registered_value_match",
            }
            output.append(
                replace(
                    element,
                    text=match.replacement,
                    source_text=match.replacement,
                    artifact_locator=locator,
                )
            )
    return output


def _record_source_disposition(
    stats: BuildStats,
    locator: dict[str, Any] | None,
    *,
    role: str,
    reason: str,
    replacement_text: str | None = None,
    value: str | None = None,
) -> None:
    proof: dict[str, Any] = {
        key: (locator or {}).get(key)
        for key in ("ir_id", "source_item_index", "order_index", "page_no")
        if (locator or {}).get(key) is not None
    }
    proof.update({"role": role, "reason": reason})
    if replacement_text is not None:
        proof["replacement_text"] = replacement_text
    if value is not None:
        proof["value"] = value
    stats.source_dispositions.append(proof)


def _merge_registered_cover_title_fragments(
    elements: list[PreparedElement],
    *,
    document_title: str | None,
    stats: BuildStats,
) -> list[PreparedElement]:
    """Join only a page-1 heading chain proven by the registered title.

    MinerU can turn visual line wrapping on an announcement cover into a false
    parent/child tree (for example ``...补`` + ``充公告``). We do not infer a
    join from typography or length. A contiguous range inside the opening
    page-1 heading run must concatenate exactly to the registered title or its
    issuer-prefix suffix after whitespace/NFKC normalization.
    """

    if not document_title:
        return elements
    run_start = 0
    while run_start < len(elements):
        element = elements[run_start]
        if (
            element.kind != "text"
            or element.page_no != 1
            or not element.text
            or rules.strip_header_kv_line(element.text) != ""
        ):
            break
        run_start += 1
    prefix_end = run_start
    while prefix_end < len(elements):
        element = elements[prefix_end]
        if element.kind != "heading" or element.page_no != 1 or not element.text:
            break
        prefix_end += 1
    if prefix_end - run_start < 2:
        return elements

    targets = [(document_title, _comparison_text(document_title))]
    issuer_split = re.split(r"[：:]", document_title, maxsplit=1)
    if len(issuer_split) == 2 and issuer_split[1].strip():
        suffix = issuer_split[1].strip()
        targets.append((suffix, _comparison_text(suffix)))

    match: tuple[int, int, str] | None = None
    for width in range(prefix_end, 1, -1):
        for start in range(run_start, prefix_end - width + 1):
            end = start + width
            joined = _comparison_text(
                "".join(str(element.text or "") for element in elements[start:end])
            )
            display = next(
                (value for value, normalized in targets if joined == normalized),
                None,
            )
            if display is not None:
                match = (start, end, display)
                break
        if match is not None:
            break
    if match is None:
        return elements

    start, end, display = match
    source_members = elements[start:end]
    merged = replace(
        elements[start],
        kind="heading",
        text=display,
        heading_level=1,
        title=None,
        artifact_locator=_prepared_group_locator(source_members),
    )
    stats.merged_cover_title_fragments += end - start - 1
    return [*elements[:start], merged, *elements[end:]]


def _anchor_headerless_units(
    units: list[UnitDraft], *, document_title: str | None = None, stats: BuildStats
) -> list[UnitDraft]:
    """Anchor headerless evidence only to a truthful document/source title."""

    out: list[UnitDraft] = []
    for unit in units:
        if unit.heading_path:
            out.append(unit)
            continue
        # A payload title (including a QA question or image caption) is not its
        # own source ancestor.  Only registered document metadata may provide a
        # headerless public retrieval anchor.
        anchor, anchor_projection = _headerless_anchor_projection(
            unit,
            document_title=document_title,
        )
        if not anchor:
            out.append(unit)
            continue
        stats.anchored_header_units += 1
        out.append(
            UnitDraft(
                **{
                    **unit.__dict__,
                    "heading_path": [anchor],
                    "structural_path": [],
                    "title": unit.title or anchor,
                    "artifact_locator": _with_heading_projection(
                        unit.artifact_locator,
                        [anchor_projection] if anchor_projection else [],
                    ),
                }
            )
        )
    return out


def _headerless_anchor_projection(
    unit: UnitDraft,
    *,
    document_title: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    locator = unit.artifact_locator or {}
    if unit.title:
        if unit.payload_kind == "table":
            captions = [str(value) for value in unit.payload.get("caption") or []]
            for index, caption in enumerate(captions):
                if _comparison_text(caption) != _comparison_text(unit.title):
                    continue
                selector = source_selector(
                    locator,
                    field="table_caption",
                    index=index,
                )
                if selector is not None:
                    return unit.title, {
                        "target_index": 0,
                        "kind": "source_field",
                        "selector": selector,
                        "transform": "clean_text.v1",
                    }
            return unit.title, None
        if unit.payload_kind == "text" and "image_ref" in unit.payload:
            context = str(unit.payload.get("context") or "")
            caption = str(unit.payload.get("caption") or "")
            if context and _comparison_text(context) == _comparison_text(unit.title):
                context_locator = locator.get("context_source_locator")
                selector = (
                    source_selector(context_locator, field="text")
                    if isinstance(context_locator, dict)
                    else None
                )
            elif caption and _comparison_text(caption) == _comparison_text(unit.title):
                selector = source_selector(locator, field="image_caption")
            else:
                selector = None
            if selector is not None:
                return unit.title, {
                    "target_index": 0,
                    "kind": "source_field",
                    "selector": selector,
                    "transform": "clean_text.v1",
                }
            return unit.title, None
    if document_title:
        return document_title, {
            "target_index": 0,
            "kind": "document_metadata",
            "field": "title",
        }
    return None, None


def _member_semantic_keys(
    members: list[UnitDraft], *, filing_type: str | None
) -> list[str] | None:
    """Recall keys of the grouped members — column-bound, never in payload.

    Embedding keys in parts would push a rules-derived value into content_hash
    (U2 forbids rule upgrades masquerading as content changes).
    """

    keys: list[str] = []
    for member in members:
        keys = _stable_semantic_keys(
            keys,
            [member.semantic_key] if member.semantic_key else [],
            semantic_keys_for_unit(member, filing_type=filing_type),
            _note_keys_for_unit(member),
            member.semantic_keys or [],
        )
    return keys or None


def _unit_part(
    unit: UnitDraft,
    *,
    include_heading: bool,
) -> dict[str, Any]:
    part: dict[str, Any] = {"kind": unit.payload_kind, "order": unit.source_order}
    if unit.payload_kind == "text" and "image_ref" in unit.payload:
        part["kind"] = "image"
    part.update(unit.payload)
    if include_heading and unit.heading_path:
        part["heading_path"] = list(unit.heading_path)
    if unit.applicability:
        part["applicability"] = unit.applicability
    if unit.quality_status != "ok":
        part["quality_status"] = unit.quality_status
    if unit.artifact_locator:
        # The parent mixed locator is only a coarse anchor. Each member keeps
        # its complete source locator so table/page provenance remains lossless.
        part["artifact_locator"] = dict(unit.artifact_locator)
    return part


def _worst_quality(units: list[UnitDraft]) -> str:
    if any(unit.quality_status == "unusable" for unit in units):
        return "unusable"
    if any(unit.quality_status == "needs_review" for unit in units):
        return "needs_review"
    return "ok"


def _mixed_locator(units: list[UnitDraft]) -> dict[str, Any] | None:
    """Return only locator fields that truthfully describe a mixed unit.

    Table locators such as ``page_bboxes`` and ``model_table_indices`` are a
    complete bundle for one logical table.  Copying that bundle from the first
    member and then widening ``page_span`` across later members makes the
    mixed unit claim internally inconsistent table provenance.  Each part
    already retains its complete locator, so the parent exposes only its first
    physical anchor plus the overall page range.
    """

    if not units:
        return None
    first = units[0].artifact_locator or {}
    locator = {
        key: first[key] for key in ("order_index", "page_no", "bbox") if key in first
    }
    pages: list[int] = []
    saw_page_span = False
    for unit in units:
        member_locator = unit.artifact_locator or {}
        page_no = _int_or_none(member_locator.get("page_no"))
        if page_no is not None:
            pages.append(page_no)
        page_span = member_locator.get("page_span")
        if (
            isinstance(page_span, list)
            and len(page_span) == 2
            and (start_page := _int_or_none(page_span[0])) is not None
            and (end_page := _int_or_none(page_span[1])) is not None
        ):
            pages.extend((start_page, end_page))
            saw_page_span = True
    if pages and (saw_page_span or min(pages) != max(pages)):
        locator["page_span"] = [min(pages), max(pages)]
    return locator or None


def _unit_sort_key(unit: UnitDraft) -> tuple[int, int]:
    return (unit.source_order, unit.intra_order)


def _note_keys_for_unit(unit: UnitDraft) -> list[str]:
    """章节键（含祖先继承，round13 用户裁决）：标题优先，然后沿 heading_path
    自深向浅逐级取键——"(1) 明细情况" 这类无科目语义的叶子从最近的科目祖先
    继承（19、其他非流动金融资产 → other_noncurrent_financial_assets），
    章级键（第八节 财务报告 → financial_report_chapter）一并入组。返回按
    深度有序去重的列表，首个即最具体键。"""

    # 词表键对全部文档类型开放（round13：审计报告等 'other' 文档同样承载
    # 报表与附注结构；标题匹配有界，误配风险可控且被类扫描看护）。
    keys: list[str] = []
    candidates = [
        unit.title,
        *reversed(_unit_section_path(unit)),
        *reversed(unit.heading_path),
    ]
    for candidate in candidates:
        for key in rules.note_keys_for_title(candidate):
            if key not in keys:
                keys.append(key)
    return keys


def semantic_keys_for_unit(unit: UnitDraft, *, filing_type: str | None) -> list[str]:
    source_path = _unit_section_path(unit)
    text = " ".join(
        part
        for part in [
            unit.title or "",
            " ".join(source_path),
            _table_caption_first(unit),
        ]
        if part
    )
    # leaf_only rules see the unit's own slot only (title/deepest heading/
    # caption) so a combined ancestor title never leaks onto descendants.
    leaf_text = " ".join(
        part
        for part in [
            unit.title or "",
            source_path[-1] if source_path else "",
            _table_caption_first(unit),
        ]
        if part
    )
    keys: list[str] = []
    for rule in rules.SEMANTIC_KEY_RULES:
        if (
            rule.filing_type_limited
            and filing_type not in rules.SEMANTIC_LIMITED_FILING_TYPES
        ):
            continue
        haystack = leaf_text if rule.leaf_only else text
        if all(token in haystack for token in rule.required) and (
            not rule.any_required
            or any(token in haystack for token in rule.any_required)
        ):
            if rule.semantic_key not in keys:
                keys.append(rule.semantic_key)
    return keys


def _clean_text(value: str) -> str:
    cleaned = "".join(
        char
        for char in value
        if not (unicodedata.category(char) == "Cc" and char not in "\n\t")
    )
    lines = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if rules.NOISE_SEPARATOR_RE.match(stripped):
            continue
        lines.append(line.strip())
    return "\n".join(line for line in lines if line).strip()


def _element_text(element: dict[str, Any]) -> str:
    for key in ("text", "content"):
        value = element.get(key)
        if value is not None:
            return str(value)
    return ""


def _caption_text(element: dict[str, Any]) -> str:
    for key in ("caption", "image_caption", "text"):
        value = element.get(key)
        if value is not None:
            if isinstance(value, list):
                return " ".join(str(item) for item in value)
            return str(value)
    return ""


def _source_text(value: str) -> str:
    """Trim a structured source field without rewriting its internal syntax."""

    return value.strip()


def _source_text_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _source_text(str(item)))]


def _image_context(
    previous: PreparedElement | None, page_no: int | None
) -> tuple[str, dict[str, Any] | None]:
    if previous is None or previous.kind != "heading":
        return "", None
    if previous.page_no != page_no:
        return "", None
    return previous.text or "", dict(previous.artifact_locator or {}) or None


def _content_addressed_image_ref(
    image_path: str,
    *,
    image_bytes_resolver: ImageBytesResolver | None,
) -> str:
    filename = image_path.rsplit("/", 1)[-1]
    suffix = ""
    if "." in filename:
        suffix = "." + filename.rsplit(".", 1)[1]
    if image_bytes_resolver is not None:
        # MinerU may emit 64-hex filenames that are identifiers rather than a
        # hash of the file bytes.  Whenever the artifact is available, bytes
        # are the authority; a hash-looking name is never proof.
        digest = hashlib.sha256(image_bytes_resolver(image_path)).hexdigest()
        return f"images/{digest}{suffix}"
    stem = filename.rsplit(".", 1)[0]
    if re.fullmatch(r"[0-9a-fA-F]{64}", stem):
        # Pure/offline builder callers may supply an already-bound reference.
        # Production BuildUnits always provides a resolver for parser assets.
        return f"images/{filename}"
    raise ValueError(f"image bytes required for non-hash image name: {image_path}")


def _artifact_locator(element: dict[str, Any]) -> dict[str, Any]:
    locator: dict[str, Any] = {"order_index": element.get("order_index")}
    if element.get("page_no") is not None:
        locator["page_no"] = element.get("page_no")
    if element.get("bbox") is not None:
        locator["bbox"] = element.get("bbox")
    for key in (
        "ir_id",
        "source_item_index",
        "source",
        "source_order_index",
        "source_order_span",
        "page_span",
        "page_bboxes",
        "model_table_indices",
        "continuation_source_item_indices",
        "table_locator_algorithm",
        "source_slice",
        "annotation_source_slices",
        "derivation",
    ):
        if element.get(key) is not None:
            locator[key] = element.get(key)
    return locator


def _heading_level_for(element: PreparedElement) -> int | None:
    text = (element.text or "").strip()
    if not text:
        return None
    if (
        element.region_role == "narrative"
        and _official_numbered_question_match(text) is not None
    ):
        # Within a structurally proven official form, an Arabic-numbered
        # question line is transcript body, not a business-section heading;
        # demoting it keeps the narrative in one raw-text evidence carrier.
        return None
    if text.endswith(("?", "？")) or rules.QUESTION_START_RE.match(text):
        return None
    # A declaration line must never enter the heading tree even when a layout
    # parser labels it as a heading.
    if rules.is_declaration_line(text):
        return None
    # Table footnotes ([注1] …, 注：…) belong to the preceding table.
    if rules.FOOTNOTE_LINE_RE.match(text):
        return None
    # Public boundaries require parser/layout evidence. Numbered plain text is
    # retained as body content: guessing a heading from typography-free text
    # was the main source of small, low-recall fragments in the corpus replay.
    if element.kind != "heading":
        return None
    normalized_title = _normalized_title(text)
    if normalized_title in rules.FIXED_L1_TITLES:
        return 1
    for level, pattern in rules.HEADING_PATTERNS:
        if pattern.match(text):
            return level
    if element.heading_level is not None:
        return max(1, min(7, element.heading_level))
    return None


def _normalized_title(text: str) -> str:
    return re.sub(r"\s+", "", text).rstrip("：:")


def _strip_declaration_lines(
    text: str, *, stats: BuildStats | None
) -> tuple[str, str | None]:
    """Strip unit-declaration lines and a leading applicability marker.

    Returns the remaining text and the section applicability. A leading
    not_applicable marker with no other content keeps the marker line as the
    unit text (that IS the section's disclosure); a leading applicable marker
    with no other content returns empty text — the flag is sunk onto the next
    unit of the same section by _sink_leading_applicable.

    A marker on line two directly after a short label line (…说明 style) flags
    the unit without stripping, so composite declarations keep their prose.
    """

    if not text:
        return text, None
    lines = text.splitlines()
    kept: list[str] = []
    applicability: str | None = None
    for index, line in enumerate(lines):
        if applicability is None and index == 0 and rules.is_pure_marker_line(line):
            applicability = rules.classify_marker_line(line)
            if stats is not None:
                stats.stripped_marker_lines += 1
            if applicability == "not_applicable":
                kept.append(line.strip())
            continue
        if (
            applicability is None
            and index == 1
            and rules.is_pure_marker_line(line)
            and len(lines[0].strip()) <= 24
            and not lines[0].strip().endswith(("。", "；"))
        ):
            # Label-then-marker composite: flag it, keep the text intact.
            applicability = rules.classify_marker_line(line)
            kept.append(line)
            continue
        kept.append(line)
    return "\n".join(kept).strip(), applicability


def _same_or_child_section(candidate: UnitDraft, section: UnitDraft) -> bool:
    """Match concrete heading occurrences, never merely equal path text."""

    if candidate.section_path or section.section_path:
        if not section.section_path:
            return not candidate.section_path
        return (
            candidate.section_path[: len(section.section_path)]
            == section.section_path
        )
    candidate_path = _unit_section_path(candidate)
    section_path = _unit_section_path(section)
    if not section_path:
        return not candidate_path
    return candidate_path[: len(section_path)] == section_path


def _sink_leading_applicable(units: list[UnitDraft]) -> list[UnitDraft]:
    """Attach dangling √适用 declarations to the section content they open.

    An applicable marker with no prose means "content follows" (usually a
    table); the marker line itself must not survive as a unit — the flag
    moves onto the immediately following unit of the
    same section. With no such sibling the declaration stays as its own unit.
    """

    out = list(units)
    dropped: set[int] = set()
    # Freeze the source marker set before mutating followers.  A non-text
    # follower (notably an image whose payload also uses ``payload_kind=text``)
    # may receive applicability, but must never become a second marker and be
    # rewritten as ``{"text": "适用"}`` on the next loop iteration.
    dangling_indices = [
        index
        for index, unit in enumerate(out)
        if (
            unit.payload_kind == "text"
            and "text" in unit.payload
            and unit.applicability == "applicable"
            and not str(unit.payload.get("text", "")).strip()
        )
    ]
    for index in dangling_indices:
        if index in dropped:
            continue
        unit = out[index]
        follower = index + 1
        if follower < len(out) and _same_or_child_section(
            out[follower], unit
        ):
            if out[follower].applicability in {None, "applicable"}:
                locator = dict(out[follower].artifact_locator or {})
                if unit.artifact_locator:
                    locator["applicability_source_locator"] = dict(
                        unit.artifact_locator
                    )
                    marker_graph = _projection_graph(unit.artifact_locator)
                    marker_structured = list(marker_graph["structured"])
                    marker_source = _selector_for_locator(
                        unit.artifact_locator,
                        fallback_field="text",
                    )
                    if marker_source is not None:
                        marker_structured = _merged_projection_entries(
                            marker_structured,
                            [
                                {
                                    "kind": "applicability_marker",
                                    "source": marker_source,
                                    "target_field": "applicability",
                                    "transform": "applicability_marker.v1",
                                }
                            ],
                        )
                    follower_graph = _projection_graph(locator)
                    follower_graph["structured"] = _merged_projection_entries(
                        follower_graph["structured"],
                        marker_structured,
                    )
                    locator["source_projection"] = follower_graph
                out[follower] = UnitDraft(
                    **{
                        **out[follower].__dict__,
                        "applicability": "applicable",
                        "artifact_locator": locator or None,
                    }
                )
                dropped.add(index)
            else:
                locator = dict(unit.artifact_locator or {})
                locator["review_reason"] = "conflicting_applicability_markers"
                marker_text = str(
                    locator.get("applicability_marker_text") or "适用"
                )
                out[index] = UnitDraft(
                    **{
                        **unit.__dict__,
                        "payload": {"text": marker_text},
                        "quality_status": "needs_review",
                        "artifact_locator": _with_text_payload_projection(
                            locator
                        ),
                    }
                )
        else:
            locator = dict(unit.artifact_locator or {})
            marker_text = str(locator.get("applicability_marker_text") or "适用")
            out[index] = UnitDraft(
                **{
                    **unit.__dict__,
                    "payload": {"text": marker_text},
                    "artifact_locator": _with_text_payload_projection(locator),
                }
            )
    return [unit for index, unit in enumerate(out) if index not in dropped]


# Dot-numbering disambiguation for the ``N.`` question separator: a western dot
# followed by a digit is normally a decimal number ("1.5亿元"), so ``\.(?!\d)``
# rejects that form.  The single ``\.(?=\d{4}\s*年)`` carve-out re-admits a dot
# glued to a 4-digit year + 年 ("3.2024年经营情况"; the vlm backend spaces
# ASCII tokens, so "16.2024 年…" must qualify too), which is numbered-question
# grammar, not a decimal.  This is structural grammar, not a sample-specific
# phrase rule.
_OFFICIAL_NUMBERED_QUESTION_RE = re.compile(
    r"^\s*(?P<ordinal>\d{1,3})"
    r"(?:[、．]|\.(?!\d)|\.(?=\d{4}\s*年))\s*(?P<body>\S.*)$"
)

def _official_numbered_question_match(text: str) -> re.Match[str] | None:
    return _OFFICIAL_NUMBERED_QUESTION_RE.match(text)


def _prepared_text_slice_locator(
    element: PreparedElement,
    selected_text: str,
) -> dict[str, Any] | None:
    """Bind one uniquely addressable logical value to its physical text.

    ``PreparedElement.text`` may already be cleaned, so offsets must be taken
    against ``source_text``.  Ambiguous or non-verbatim values fail closed;
    callers retain the original carrier instead of inventing provenance.
    """

    source_text = element.source_text
    if source_text is None:
        source_text = element.text or ""
    if not selected_text:
        return None
    start = source_text.find(selected_text)
    if start < 0 or start != source_text.rfind(selected_text):
        return None
    return _sliced_locator(
        element.artifact_locator,
        start=start,
        end=start + len(selected_text),
        selected_value=selected_text,
    )


def _sliced_locator(
    locator: dict[str, Any] | None,
    *,
    start: int,
    end: int,
    selected_value: str,
) -> dict[str, Any] | None:
    if locator is None:
        return None
    output = dict(locator)
    source_slice = output.get("source_slice")
    if isinstance(source_slice, dict):
        typed = {
            "source": dict(source_slice.get("source") or {}),
            "field": dict(source_slice.get("field") or {}),
        }
        field = typed["field"]
        base_span = field.get("char_span")
        base_start = (
            base_span[0]
            if isinstance(base_span, list)
            and len(base_span) == 2
            and all(isinstance(value, int) for value in base_span)
            else 0
        )
        if field.get("kind") in {"text", "table_cell"}:
            field["char_span"] = [base_start + start, base_start + end]
            field["value_sha256"] = source_value_sha256(selected_value)
            output["source_slice"] = typed
        return output
    selector = source_selector(
        output,
        field="text",
        char_span=[start, end],
        value_sha256=source_value_sha256(selected_value),
    )
    if selector is not None:
        output["source_slice"] = selector
    return output



def _is_empty_table_element(element: PreparedElement) -> bool:
    if element.kind != "table":
        return False
    table = element.table or {}
    return (
        not table.get("headers")
        and not table.get("rows")
        and not (element.table_html or "")
        and not any(str(value).strip() for value in element.table_caption)
        and not any(str(value).strip() for value in element.table_footnote)
    )


def _previous_text_before(
    elements: list[PreparedElement], current: PreparedElement
) -> tuple[str, dict[str, Any] | None]:
    current_path = current.structural_path or current.heading_path
    for element in reversed(elements):
        if element.order_index >= current.order_index:
            continue
        element_path = element.structural_path or element.heading_path
        if (
            element.kind == "text"
            and element.text
            and element_path == current_path
            and rules.is_unit_declaration_line(element.text)
        ):
            return element.text, dict(element.artifact_locator or {}) or None
        # Unit declarations describe only the immediately following table in
        # the same source section; never scan across another content carrier.
        return "", None
    return "", None


def _table_group_to_unit(
    group: list[PreparedElement],
    *,
    previous_text: str,
    previous_locator: dict[str, Any] | None = None,
    stats: BuildStats | None = None,
) -> UnitDraft:
    first = group[0]
    source_grid = first.table or {}
    source_grid_empty = not (
        source_grid.get("headers") or source_grid.get("rows")
    )
    if first.table_parse_failed or (
        source_grid_empty and str(first.table_html or "").strip()
    ):
        fallback_payload: dict[str, Any] = {
            "caption": list(first.table_caption),
            "raw_html": first.table_html or "",
            "notes": list(first.table_footnote),
        }
        if not source_grid_empty:
            fallback_payload.update(
                {
                    "headers": [
                        str(value) for value in source_grid.get("headers") or []
                    ],
                    "rows": [
                        [str(value) for value in row]
                        for row in source_grid.get("rows") or []
                    ],
                    "merged_cells": [
                        dict(value)
                        for value in source_grid.get("merged_cells") or []
                    ],
                }
            )
        return UnitDraft(
            payload_kind="table",
            payload=fallback_payload,
            source_order=first.order_index,
            heading_path=list(first.heading_path),
            structural_path=list(first.structural_path),
            section_path=list(first.section_path),
            title=_table_title(first),
            quality_status="needs_review",
            artifact_locator=_with_table_payload_projection(
                first.artifact_locator
            ),
        )

    headers, rows, merged_cells, dropped_blank_rows = _merged_table_grid(group)
    if stats is not None:
        stats.dropped_blank_table_rows += dropped_blank_rows
    detected_unit, unit_projection = _detect_unit_with_projection(
        first,
        headers=headers,
        previous_text=previous_text,
        previous_locator=previous_locator,
    )
    payload = {
        "caption": list(first.table_caption),
        "unit": detected_unit,
        "headers": headers,
        "rows": rows,
        # Row/column spans are table meaning, not provenance.  Keeping them in
        # payload makes content identity change when the logical grid changes.
        "merged_cells": merged_cells,
        "notes": _merged_notes(group),
    }
    locator = dict(_with_table_payload_projection(first.artifact_locator) or {})
    if unit_projection is not None:
        locator["unit_projection"] = unit_projection
        locator = _with_structured_projection(
            locator,
            {
                "kind": "derived_field",
                **unit_projection,
            },
        ) or locator
    if len(group) > 1:
        locator["merge_reason"] = "continued_table"
        page_numbers = [item.page_no for item in group if item.page_no is not None]
        if page_numbers:
            locator["page_span"] = [min(page_numbers), max(page_numbers)]
    has_grid_content = bool(headers or rows)
    return UnitDraft(
        payload_kind="table",
        payload=payload,
        source_order=first.order_index,
        heading_path=list(first.heading_path),
        structural_path=list(first.structural_path),
        section_path=list(first.section_path),
        title=_table_title(first),
        quality_status="ok" if has_grid_content else "needs_review",
        applicability=_table_applicability(group),
        artifact_locator=locator or None,
    )


def _merged_table_grid(
    group: list[PreparedElement],
) -> tuple[list[str], list[list[str]], list[dict[str, int]], int]:
    return merge_table_grids_with_stats([element.table or {} for element in group])


def _merged_notes(group: list[PreparedElement]) -> list[str]:
    notes: list[str] = []
    for element in group:
        notes.extend(element.table_footnote)
    return notes


def _table_title(element: PreparedElement) -> str | None:
    captions = [caption.strip() for caption in element.table_caption if caption.strip()]
    for caption in captions:
        if not rules.is_unit_declaration_line(caption) and not rules.is_declaration_line(
            caption
        ):
            return caption
    # A unit/currency declaration is table metadata, not its business title.
    # Preferring it erased the deepest structural leaf from mixed payloads
    # (招商银行: ``(b) 损失准备变动情况`` became ``单位：人民币百万元``),
    # weakening both lexical and embedding retrieval.  The declaration stays
    # losslessly in payload.caption/unit; use it as title only when no heading
    # exists at all.
    if element.title:
        return element.title
    return next(
        (caption for caption in captions if rules.is_unit_declaration_line(caption)),
        None,
    )


def _table_applicability(group: list[PreparedElement]) -> str | None:
    values = {
        value
        for element in group
        for caption in element.table_caption
        if rules.is_pure_marker_line(caption)
        if (value := rules.classify_marker_line(caption)) is not None
    }
    return next(iter(values)) if len(values) == 1 else None


def _detect_unit_with_projection(
    element: PreparedElement,
    *,
    headers: list[str],
    previous_text: str,
    previous_locator: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    candidates: list[tuple[str, str, int | None, dict[str, Any] | None]] = [
        *(
            (caption, "table_caption", index, None)
            for index, caption in enumerate(element.table_caption)
        ),
        *(
            (header, "table_header", index, None)
            for index, header in enumerate(headers)
        ),
        (previous_text, "text", None, previous_locator),
    ]
    for candidate, source_field, source_index, source_locator in candidates:
        if not rules.is_unit_declaration_line(candidate):
            continue
        match = rules.UNIT_DECLARATION_VALUE_RE.search(candidate)
        if match:
            selector_locator = source_locator or element.artifact_locator
            selector = source_selector(
                selector_locator or {},
                field=source_field,
                index=source_index,
            )
            if selector is None:
                return re.sub(r"\s+", "", match.group(1)), None
            projection: dict[str, Any] = {
                "source": selector,
                "target_field": "payload.unit",
                "transform": "unit_declaration.v1",
            }
            return re.sub(r"\s+", "", match.group(1)), projection
    return None, None


def _table_payload_is_empty(payload: dict[str, Any]) -> bool:
    if str(payload.get("raw_html") or "").strip():
        return False
    if any(
        str(value).strip()
        for field in ("caption", "notes")
        for value in payload.get(field) or []
    ) or str(payload.get("unit") or "").strip():
        return False
    # A header-skeleton table (headers, zero data rows) is original content:
    # 分部信息-class sections disclose an empty template, and dropping the
    # unit swallowed the whole heading branch from every path
    # (ub-2026.07-18 swallowed-heading audit).
    if any(str(cell).strip() for cell in payload.get("headers") or []):
        return False
    rows = payload.get("rows") or []
    if not rows:
        return True
    return all(not "".join(str(cell).strip() for cell in row) for row in rows)


def _final_quality_status(unit: UnitDraft) -> str:
    if unit.quality_status == "unusable" or _main_text_is_unusable(unit):
        return "unusable"
    if unit.quality_status == "needs_review":
        return "needs_review"
    return "ok"


def _main_text_is_unusable(unit: UnitDraft) -> bool:
    text = _main_text(unit)
    if not re.sub(r"\s+", "", text):
        return True
    total = len(text)
    bad = sum(
        1
        for char in text
        if char == "\ufffd"
        or (unicodedata.category(char).startswith("C") and char not in "\n\t\r")
    )
    return total > 0 and bad / total > rules.GIBBERISH_RATIO_MAX


def _main_text(unit: UnitDraft) -> str:
    if unit.payload_kind == "mixed":
        return " ".join(
            filter(None, (_part_text(part) for part in unit.payload.get("parts", [])))
        )
    if unit.payload_kind == "text":
        if "text" in unit.payload:
            return str(unit.payload.get("text") or "")
        return " ".join(str(value) for value in unit.payload.values() if value)
    if unit.payload_kind == "table":
        return _table_cells_text(unit.payload)
    return ""


def _table_cells_text(payload: Mapping[str, Any]) -> str:
    """Linearize one table payload/part into its searchable cell text."""

    rows = payload.get("rows") or []
    headers = payload.get("headers") or []
    return " ".join(
        [str(value) for value in payload.get("caption") or []]
        + [str(payload.get("unit") or "")]
        + [str(cell) for cell in headers]
        + [str(cell) for row in rows for cell in row]
        + [str(value) for value in payload.get("notes") or []]
        + [str(payload.get("raw_html") or "")]
    )


def _part_text(part: dict[str, Any]) -> str:
    kind = str(part.get("kind", "text"))
    if kind == "table":
        return _table_cells_text(part)
    if kind == "image":
        return " ".join(
            filter(
                None, (str(part.get("caption") or ""), str(part.get("context") or ""))
            )
        )
    return str(part.get("text") or "")


def _table_caption_first(unit: UnitDraft) -> str:
    caption = unit.payload.get("caption") if unit.payload_kind == "table" else None
    if isinstance(caption, list) and caption:
        return str(caption[0])
    return ""


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_text_value(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_region_value(value: Any) -> str | None:
    region = _optional_text_value(value)
    if region not in {None, "metadata", "narrative", "footer", "attachment"}:
        raise ValueError(f"unsupported source projection region: {region!r}")
    return region
