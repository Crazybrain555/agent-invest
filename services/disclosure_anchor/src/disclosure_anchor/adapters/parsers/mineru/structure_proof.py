"""Align explicit PDF structure to MinerU text carriers.

This module accepts explicit PDF tags/bookmarks and MinerU v2 ``title`` blocks.
It never promotes a legacy ``text_level`` hint by itself or infers a parent
from numbering or nearest-level proximity.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import re
from typing import Any, Literal

from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    mineru_provider_item_sha256,
)
from disclosure_anchor.adapters.parsers.comparison import comparison_text
from disclosure_anchor.adapters.parsers.mineru.source_evidence import (
    CarrierSourceSupport,
    ResolvedTableRole,
    iter_mineru_text_carriers,
)
from disclosure_anchor.adapters.parsers.mineru.text_projection import (
    MinerUTextProjectionSet,
)
from disclosure_anchor.adapters.parsers.pdf_native_structure import (
    NativeBookmark,
    NativeMarkedObject,
    NativeStructureIndex,
    NativeStructureNode,
)
from disclosure_anchor.adapters.parsers.pdf_native_text import (
    NativeTextAtom,
    NativeTextPage,
)
from disclosure_anchor.adapters.parsers.printed_toc import (
    printed_toc_witness,
)
from disclosure_anchor.application.contracts.document_structure import (
    DOCUMENT_STRUCTURE_ALGORITHM,
    DOCUMENT_STRUCTURE_VERSION,
    LEGACY_DOCUMENT_STRUCTURE_ALGORITHM,
    carrier_set_sha256,
    printed_number_rank,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    source_value_sha256,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


_HEADING_ROLES = {f"H{level}": level for level in range(1, 7)}
_NON_HEADING_ROLES = frozenset({"P", "TOC", "TOCI", "Table", "TD", "TH"})
_NON_SECTION_ANCESTOR_ROLES = frozenset({"TOC", "TOCI", "Table", "TD", "TH"})
_TEXT_KINDS = frozenset(
    {
        "aside_text",
        "footer",
        "header",
        "page_footnote",
        "page_number",
        "phonetic",
        "ref_text",
        "text",
    }
)
_PRINTED_NUMBER_PREFIX = re.compile(
    r"^(?:"
    r"第[〇零一二三四五六七八九十百千万两]+[编篇章节]"
    r"|[〇零一二三四五六七八九十百千万两]+[、．.]"
    r"|[（(][〇零一二三四五六七八九十百千万两]+[）)]"
    r"|\d+(?:\.\d+)+(?:[、．.]|\s)+"
    r"|\d{1,4}[、．.)）]"
    r"|[（(]\d{1,4}[）)]"
    r")\s*(?=\S)"
)


@dataclass(frozen=True)
class _Ref:
    source_item_index: int
    field: str
    index: int | None = None


_NativeNodeKey = tuple[str, int]


@dataclass(frozen=True)
class _Carrier:
    ref: _Ref
    page_idx: int
    raw_kind: str
    source_value: str
    comparison_value: str
    bbox: tuple[float, float, float, float]
    provider_level: int | None


@dataclass
class _Candidate:
    refs: tuple[_Ref, ...]
    level: int
    evidence: set[str] = field(default_factory=set)
    native_role: str | None = None
    native_node_id: int | None = None
    native_ancestor_roles: tuple[str, ...] = ()
    native_ancestors: tuple[int, ...] = ()
    native_segment_id: str | None = None
    native_hierarchy_valid: bool = False
    bookmark_parent: tuple[_Ref, ...] | None = None
    provider_parent: tuple[_Ref, ...] | None = None
    propagates: bool = True
    source_proven: bool = False


@dataclass(frozen=True)
class _NativeLayoutWitness:
    page_idx: int
    blocks: frozenset[tuple[int, int]]
    line_bboxes: tuple[tuple[float, float, float, float], ...]
    line_heights: tuple[float, ...]
    exact_lines: bool
    centered: bool
    display_height: bool
    near_document_left: bool
    page_front: bool
    component_closed: bool


@dataclass(frozen=True)
class _NativeVisualRow:
    bbox: tuple[float, float, float, float]
    line_refs: frozenset[tuple[int, int, int]]
    height: float


@dataclass(frozen=True)
class _OwnerScopeCandidate:
    """Source-bound layout point that may end, but never create, a section."""

    ref: _Ref
    source_atom_orders: tuple[int, ...]
    eligibility_basis: Literal[
        "numbered_caption_native_break",
        "unnumbered_display_peer_break",
    ]
    boundary_carrier_scope: Literal[
        "selected_only",
        "selected_and_same_carrier",
    ]
    layout: _NativeLayoutWitness


def build_mineru_structure_proof(
    *,
    native: NativeStructureIndex,
    content_list: list[dict[str, Any]],
    source_pdf_sha256: str,
    content_list_v2: list[list[dict[str, Any]]] | None = None,
    text_projections: MinerUTextProjectionSet | None = None,
    start_page: int | None = None,
    end_page: int | None = None,
    carrier_source_support: Mapping[
        tuple[int, str, int | None],
        CarrierSourceSupport,
    ]
    | None = None,
    table_role_overrides: tuple[ResolvedTableRole, ...] = (),
    identity_content_list: list[dict[str, Any]] | None = None,
    source_pages: tuple[NativeTextPage, ...] | None = None,
) -> dict[str, Any]:
    """Build a source-bound heading DAG and page-frame ownership proof."""

    page_count = native.source_pdf_page_count
    if page_count < 1:
        raise ParserOutputContractError(
            "native PDF structure has an invalid page count"
        )
    identity_items = (
        identity_content_list
        if identity_content_list is not None
        else content_list
    )
    if len(identity_items) != len(content_list):
        raise ParserOutputContractError(
            "identity content list does not mirror the canonical list"
        )
    source_item_hashes = {
        index: mineru_provider_item_sha256(item)
        for index, item in enumerate(identity_items)
    }
    conflicts: list[dict[str, Any]] = []
    carriers = _carriers(
        content_list,
        table_role_overrides=table_role_overrides,
        conflicts=conflicts,
    )
    if carrier_source_support is not None:
        _require_source_supported_carriers(
            carriers,
            carrier_source_support=carrier_source_support,
        )
    if source_pages is not None and carrier_source_support is None:
        raise ParserOutputContractError(
            "native-layout structure proof requires validated carrier support"
        )
    by_ref = {carrier.ref: carrier for carrier in carriers}
    by_page: dict[int, list[_Carrier]] = defaultdict(list)
    for carrier in carriers:
        if carrier.page_idx >= page_count:
            raise ParserOutputContractError(
                "MinerU content_list page exceeds the source PDF"
            )
        by_page[carrier.page_idx].append(carrier)
    _validate_v2_page_closure(
        content_list_v2,
        source_pdf_page_count=page_count,
        start_page=start_page,
        end_page=end_page,
    )
    text_projections = _validated_text_projections(
        content_list,
        content_list_v2=content_list_v2,
        text_projections=text_projections,
    )
    toc_corroborated: set[_Ref] = set()
    if source_pages is not None:
        pages_by_comparison: dict[str, list[int]] = defaultdict(list)
        for carrier in carriers:
            if carrier.raw_kind in _TEXT_KINDS and carrier.comparison_value:
                pages_by_comparison[carrier.comparison_value].append(
                    carrier.page_idx
                )
        witness = printed_toc_witness(
            source_pages,
            carrier_pages_by_comparison={
                value: tuple(pages)
                for value, pages in pages_by_comparison.items()
            },
        )
        if witness is not None:
            toc_corroborated = {
                carrier.ref
                for carrier in carriers
                if carrier.raw_kind in _TEXT_KINDS
                and carrier.comparison_value
                and witness.corroborates(
                    carrier.comparison_value, carrier.page_idx
                )
            }
    grouped: dict[tuple[_Ref, ...], list[_Candidate]] = defaultdict(list)
    native_key_by_node: dict[_NativeNodeKey, tuple[_Ref, ...]] = {}
    nonheading_roles: dict[_Ref, set[str]] = defaultdict(set)
    native_artifacts: set[int] = set()

    for node in native.nodes:
        refs = _align_node(
            node,
            objects_by_ref=native.marked_objects,
            carriers_by_page=by_page,
        )
        if node.standard_role in _HEADING_ROLES:
            if refs is None:
                conflicts.append(_native_conflict(node, "native_heading_unaligned"))
                continue
            candidate = _native_heading_candidate(
                node,
                refs=refs,
                conflicts=conflicts,
            )
            grouped[refs].append(candidate)
            if candidate.native_hierarchy_valid:
                native_key_by_node[(node.segment_id, node.node_id)] = refs
        elif node.standard_role == "H":
            conflicts.append(
                _native_conflict(
                    node,
                    "native_heading_level_missing",
                    refs=refs or (),
                )
            )
        elif node.standard_role in _NON_HEADING_ROLES and refs is not None:
            for ref in refs:
                nonheading_roles[ref].add(node.standard_role)
        if node.raw_role in {"Artifact", "Header", "Footer"} and refs is not None:
            native_artifacts.update(ref.source_item_index for ref in refs)

    for candidate in _bookmark_candidates(
        native.bookmarks,
        carriers_by_page=by_page,
        conflicts=conflicts,
    ):
        grouped[candidate.refs].append(candidate)
    for candidate in _v2_title_candidates(
        content_list_v2,
        carriers_by_ref=by_ref,
        text_projections=text_projections,
    ):
        grouped[candidate.refs].append(candidate)

    owner_scope_candidates: tuple[_OwnerScopeCandidate, ...] = ()
    native_layout_witnesses: Mapping[_Ref, _NativeLayoutWitness] = {}
    if source_pages is not None:
        owner_scope_candidates, native_layout_witnesses = _apply_native_line_grammar(
            grouped,
            carriers=carriers,
            carrier_source_support=carrier_source_support,
            source_pages=source_pages,
            conflicts=conflicts,
        )
    selected = _select_candidates(
        grouped,
        nonheading_roles=nonheading_roles,
        conflicts=conflicts,
        toc_corroborated=toc_corroborated,
        require_native_layout=source_pages is not None,
    )
    for refs, candidate in selected.items():
        if candidate.source_proven:
            continue
        conflicts.append(
            {
                "relation": "provider_heading_unproved",
                "source_item_indices": _ref_indices(refs),
            }
        )
    selected = {
        key: candidate for key, candidate in selected.items() if candidate.source_proven
    }
    parent_key_by_key = _explicit_parents(
        selected,
        native_key_by_node=native_key_by_node,
        conflicts=conflicts,
        flatten_unsafe_hierarchy=source_pages is not None,
    )
    parent_key_by_key = _continuous_parents(
        parent_key_by_key,
        selected=selected,
        conflicts=conflicts,
        flatten_unsafe_hierarchy=source_pages is not None,
    )
    headings = _heading_nodes(
        selected,
        parent_key_by_key=parent_key_by_key,
        carriers_by_ref=by_ref,
        last_source_index=max(len(content_list) - 1, 0),
    )
    proven_sources = {ref for candidate in selected.values() for ref in candidate.refs}
    for carrier in carriers:
        if carrier.provider_level is None or carrier.ref in proven_sources:
            continue
        conflicts.append(
            {
                "relation": (
                    "provider_heading_native_nonheading"
                    if carrier.ref in nonheading_roles
                    else "provider_heading_unproved"
                ),
                "source_item_indices": [carrier.ref.source_item_index],
            }
        )

    frames = _native_page_frames(
        carriers,
        native_artifact_sources=native_artifacts,
        heading_sources={ref.source_item_index for ref in proven_sources},
    )
    owner_scope_breaks = _owner_scope_breaks(
        owner_scope_candidates,
        headings=headings,
        carriers_by_ref=by_ref,
        native_layout_witnesses=native_layout_witnesses,
        source_item_hashes=source_item_hashes,
        content_list=content_list,
        frame_member_indices={
            int(index)
            for frame in frames
            for index in frame["member_source_item_indices"]
        },
    )
    # Carrier identity is the provider's raw item, exactly as the mapper
    # stamps elements: hashing the projected copy would fork the identity
    # whenever the serializer lane rewrites text (escape cleanup).
    identities = [
        {
            "source_item_index": index,
            "source_item_sha256": source_item_hashes[index],
        }
        for index, item in enumerate(identity_items)
    ]
    return {
        "contract_version": DOCUMENT_STRUCTURE_VERSION,
        "algorithm_version": (
            DOCUMENT_STRUCTURE_ALGORITHM
            if source_pages is not None
            else LEGACY_DOCUMENT_STRUCTURE_ALGORITHM
        ),
        "source_pdf_sha256": source_pdf_sha256,
        "source_pdf_page_count": page_count,
        "carrier_set_sha256": carrier_set_sha256(identities),
        "native": {
            "status": native.native_status,
            "artifact_role": "pdf_structure",
        },
        "headings": headings,
        **(
            {"owner_scope_breaks": owner_scope_breaks}
            if source_pages is not None
            else {}
        ),
        "page_frames": frames,
        "conflicts": conflicts,
        "coverage": {
            "provider_heading_candidates": sum(
                carrier.provider_level is not None for carrier in carriers
            ),
            "native_heading_candidates": sum(
                node.standard_role in _HEADING_ROLES for node in native.nodes
            ),
            "bookmark_candidates": len(native.bookmarks),
            "mineru_v2_title_candidates": sum(
                isinstance(block, Mapping) and block.get("type") == "title"
                for page in (content_list_v2 or [])
                for block in page
            ),
            "proven_heading_nodes": len(headings),
            "owner_scope_breaks": len(owner_scope_breaks),
            "page_frame_groups": len(frames),
        },
    }


def _validate_v2_page_closure(
    content_list_v2: list[list[dict[str, Any]]] | None,
    *,
    source_pdf_page_count: int,
    start_page: int | None,
    end_page: int | None,
) -> None:
    start = 0 if start_page is None else start_page
    end = source_pdf_page_count - 1 if end_page is None else end_page
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 0
        or start > end
        or end >= source_pdf_page_count
    ):
        raise ParserOutputContractError(
            "MinerU v2 page range is outside the source PDF"
        )
    if content_list_v2 is not None and len(content_list_v2) != end - start + 1:
        raise ParserOutputContractError(
            "MinerU content_list_v2 page closure differs from the requested PDF range"
        )


def _validated_text_projections(
    content_list: list[dict[str, Any]],
    *,
    content_list_v2: list[list[dict[str, Any]]] | None,
    text_projections: MinerUTextProjectionSet | None,
) -> MinerUTextProjectionSet | None:
    if content_list_v2 is None:
        if text_projections is not None:
            raise ParserOutputContractError(
                "MinerU text projections require content_list_v2"
            )
        return None
    if text_projections is None:
        raise ParserOutputContractError(
            "MinerU content_list_v2 requires a proved text projection"
        )
    projections = text_projections
    if list(projections.canonical_items) != content_list:
        raise ParserOutputContractError(
            "MinerU structure content differs from its text projection"
        )
    indices = [
        index
        for page_indices in projections.legacy_indices_by_v2_page
        for index in page_indices
    ]
    if (
        len(projections.legacy_indices_by_v2_page) != len(content_list_v2)
        or any(
            len(page_indices) != len(blocks)
            for page_indices, blocks in zip(
                projections.legacy_indices_by_v2_page,
                content_list_v2,
                strict=True,
            )
        )
        or indices != list(range(len(content_list)))
    ):
        raise ParserOutputContractError(
            "MinerU text projection does not close every ordered block"
        )
    return projections


def _native_heading_candidate(
    node: NativeStructureNode,
    *,
    refs: tuple[_Ref, ...],
    conflicts: list[dict[str, Any]],
) -> _Candidate:
    hierarchy_valid = node.parent_consistent is not False
    candidate = _Candidate(
        refs=refs,
        level=_HEADING_ROLES[node.standard_role],
        evidence={"struct_tree"},
        native_role=node.standard_role,
        native_node_id=node.node_id,
        native_ancestor_roles=node.ancestor_roles,
        native_ancestors=node.ancestor_node_ids,
        native_segment_id=node.segment_id,
        native_hierarchy_valid=hierarchy_valid,
    )
    if not hierarchy_valid:
        conflicts.append(
            _native_conflict(
                node,
                "native_heading_parent_inconsistent",
                refs=refs,
            )
        )
    return candidate


def _select_candidates(
    grouped: Mapping[tuple[_Ref, ...], list[_Candidate]],
    *,
    nonheading_roles: Mapping[_Ref, set[str]],
    conflicts: list[dict[str, Any]],
    toc_corroborated: set[_Ref] = frozenset(),  # type: ignore[assignment]
    require_native_layout: bool = False,
) -> dict[tuple[_Ref, ...], _Candidate]:
    selected: dict[tuple[_Ref, ...], _Candidate] = {}
    claimed: set[_Ref] = set()
    for refs, candidates in sorted(
        grouped.items(),
        key=lambda item: tuple(
            (ref.source_item_index, ref.field, ref.index or -1) for ref in item[0]
        ),
    ):
        roles = {role for ref in refs for role in nonheading_roles.get(ref, set())}
        if roles:
            conflicts.append(
                {
                    "relation": "heading_role_conflict",
                    "native_roles": sorted(roles),
                    "source_item_indices": _ref_indices(refs),
                }
            )
        if claimed.intersection(refs):
            conflicts.append(
                {
                    "relation": "heading_boundary_conflict",
                    "source_item_indices": _ref_indices(refs),
                }
            )
            continue

        # Per-entry corroboration: a bookmark is a navigation claim, not
        # a structure witness on its own. Real headings exported from a
        # word processor carry the same style into the StructTree and are
        # typed as titles by the provider; a style-abused body line (a
        # form serial, a wrapped sentence fragment) aligns as a bookmark
        # and nothing else. One independent witness is therefore the
        # entry-level admission bar — no lane-level voting.
        if all(item.evidence == {"bookmark"} for item in candidates):
            if refs and all(ref in toc_corroborated for ref in refs):
                # The printed TOC names this carrier on its declared
                # page: the document itself is the second witness.
                for item in candidates:
                    item.evidence.add("printed_toc")
            else:
                conflicts.append(
                    {
                        "relation": "bookmark_uncorroborated",
                        "source_item_indices": _ref_indices(refs),
                    }
                )
                continue
        levels = {item.level for item in candidates}
        level_ambiguous = len(levels) > 1
        if level_ambiguous:
            conflicts.append(
                {
                    "relation": "heading_level_conflict",
                    "source_item_indices": _ref_indices(refs),
                }
            )
        sources = _candidate_sources(
            candidates,
            refs=refs,
            conflicts=conflicts,
            retain_ambiguous_identity=require_native_layout,
        )
        if sources is None:
            continue
        candidates, native, bookmarks, providers, hierarchy_ambiguous = sources

        chosen = (
            native[0]
            if native
            else bookmarks[0]
            if bookmarks
            else providers[0]
            if providers
            else candidates[0]
        )
        if require_native_layout:
            rendered = any(
                "native_layout" in item.evidence for item in candidates
            )
            printed = any(
                "printed_toc" in item.evidence for item in candidates
            )
            chosen.source_proven = bool(rendered or printed)
        else:
            chosen.source_proven = not hierarchy_ambiguous and bool(
                native or bookmarks or (providers and not roles)
            )
        chosen.propagates = chosen.source_proven
        chosen.evidence.update(
            evidence
            for item in candidates
            if "struct_tree" not in item.evidence or item is chosen
            for evidence in item.evidence
        )
        non_section_ancestor_roles = {
            role
            for item in candidates
            if "struct_tree" in item.evidence
            for role in item.native_ancestor_roles
            if role in _NON_SECTION_ANCESTOR_ROLES
        }
        if non_section_ancestor_roles:
            if require_native_layout:
                chosen.source_proven = False
                chosen.propagates = False
            else:
                chosen.propagates = False
            conflicts.append(
                {
                    "relation": "native_heading_non_section_ancestry",
                    "native_roles": sorted(non_section_ancestor_roles),
                    "source_item_indices": _ref_indices(refs),
                }
            )
        if require_native_layout:
            # A rendered heading identity does not prove a parent edge.
            # Preserve a single, internally consistent StructTree chain;
            # otherwise publish the heading at document-root level.  Provider
            # and bookmark parent hints remain observations only in v11.
            chosen.bookmark_parent = None
            chosen.provider_parent = None
            if hierarchy_ambiguous or level_ambiguous or not native:
                # StructTree is still retained in the bound native artifact and
                # conflict receipts, but once its hierarchy is discarded it can
                # no longer be claimed as exact evidence by the published
                # heading.  Identity remains independently witnessed by the
                # rendered native layout/provider title and is flattened to the
                # document root.
                chosen.evidence.discard("struct_tree")
                chosen.level = 1
                chosen.native_role = None
                chosen.native_node_id = None
                chosen.native_ancestor_roles = ()
                chosen.native_ancestors = ()
                chosen.native_segment_id = None
                chosen.native_hierarchy_valid = False
                if hierarchy_ambiguous or level_ambiguous:
                    conflicts.append(
                        {
                            "relation": "heading_hierarchy_flattened",
                            "source_item_indices": _ref_indices(refs),
                        }
                    )
        else:
            _merge_parent_hint(
                chosen,
                candidates=candidates,
                field_name="bookmark_parent",
                relation="bookmark_parent_conflict",
                refs=refs,
                conflicts=conflicts,
            )
            _merge_parent_hint(
                chosen,
                candidates=candidates,
                field_name="provider_parent",
                relation="provider_parent_conflict",
                refs=refs,
                conflicts=conflicts,
            )
        if roles.intersection({"TOC", "TOCI", "Table", "TD", "TH"}):
            if require_native_layout:
                chosen.source_proven = False
            chosen.propagates = False
        selected[refs] = chosen
        claimed.update(refs)
    return selected


def _candidate_sources(
    candidates: list[_Candidate],
    *,
    refs: tuple[_Ref, ...],
    conflicts: list[dict[str, Any]],
    retain_ambiguous_identity: bool = False,
) -> (
    tuple[
        list[_Candidate],
        list[_Candidate],
        list[_Candidate],
        list[_Candidate],
        bool,
    ]
    | None
):
    aligned_native = [item for item in candidates if "struct_tree" in item.evidence]
    native = [item for item in aligned_native if item.native_hierarchy_valid]
    native_shapes = {
        (
            item.level,
            item.native_segment_id,
            item.native_node_id,
            item.native_ancestors,
        )
        for item in native
    }
    native_identities = {
        (item.native_segment_id, item.native_node_id) for item in aligned_native
    }
    ambiguous = len(native_shapes) > 1 or len(native_identities) > 1
    if ambiguous:
        conflicts.append(
            {
                "relation": "native_heading_ancestry_conflict",
                "source_item_indices": _ref_indices(refs),
            }
        )
        if not retain_ambiguous_identity:
            candidates = [
                item for item in candidates if "struct_tree" not in item.evidence
            ]
        native = []
    if not candidates:
        return None
    return (
        candidates,
        native,
        [item for item in candidates if "bookmark" in item.evidence],
        [item for item in candidates if "mineru_v2_title" in item.evidence],
        ambiguous,
    )


def _merge_parent_hint(
    chosen: _Candidate,
    *,
    candidates: list[_Candidate],
    field_name: Literal["bookmark_parent", "provider_parent"],
    relation: str,
    refs: tuple[_Ref, ...],
    conflicts: list[dict[str, Any]],
) -> None:
    parents = {
        value for item in candidates if (value := getattr(item, field_name)) is not None
    }
    if len(parents) == 1:
        setattr(chosen, field_name, next(iter(parents)))
    elif len(parents) > 1:
        chosen.propagates = False
        conflicts.append(
            {
                "relation": relation,
                "source_item_indices": _ref_indices(refs),
            }
        )


def _explicit_parents(
    selected: Mapping[tuple[_Ref, ...], _Candidate],
    *,
    native_key_by_node: Mapping[_NativeNodeKey, tuple[_Ref, ...]],
    conflicts: list[dict[str, Any]],
    flatten_unsafe_hierarchy: bool = False,
) -> dict[tuple[_Ref, ...], tuple[_Ref, ...] | None]:
    output: dict[tuple[_Ref, ...], tuple[_Ref, ...] | None] = {}
    for key, candidate in selected.items():
        if not candidate.propagates:
            output[key] = None
            continue
        if candidate.native_hierarchy_valid and candidate.native_segment_id is not None:
            parent = _native_parent(
                key,
                candidate=candidate,
                selected=selected,
                native_key_by_node=native_key_by_node,
                conflicts=conflicts,
                flatten_unsafe_hierarchy=flatten_unsafe_hierarchy,
            )
        else:
            parents = {
                parent
                for parent in (
                    candidate.bookmark_parent,
                    candidate.provider_parent,
                )
                if parent is not None
                and parent in selected
                and selected[parent].propagates
                and parent != key
            }
            parent = next(iter(parents)) if len(parents) == 1 else None
            if len(parents) > 1:
                candidate.propagates = False
                conflicts.append(
                    {
                        "relation": "heading_parent_conflict",
                        "source_item_indices": _ref_indices(key),
                    }
                )
        if parent is not None and min(ref.source_item_index for ref in parent) > min(
            ref.source_item_index for ref in key
        ):
            if not flatten_unsafe_hierarchy:
                candidate.propagates = False
            conflicts.append(
                {
                    "relation": "heading_parent_invalid",
                    "source_item_indices": _ref_indices((*parent, *key)),
                }
            )
            parent = None
        output[key] = parent
    return output


def _native_parent(
    key: tuple[_Ref, ...],
    *,
    candidate: _Candidate,
    selected: Mapping[tuple[_Ref, ...], _Candidate],
    native_key_by_node: Mapping[_NativeNodeKey, tuple[_Ref, ...]],
    conflicts: list[dict[str, Any]],
    flatten_unsafe_hierarchy: bool = False,
) -> tuple[_Ref, ...] | None:
    """Use only the current StructTreeRoot segment for a native parent."""

    segment_id = candidate.native_segment_id
    assert segment_id is not None
    parent = next(
        (
            native_key_by_node[(segment_id, node_id)]
            for node_id in reversed(candidate.native_ancestors)
            if native_key_by_node.get((segment_id, node_id)) in selected
            and native_key_by_node[(segment_id, node_id)] != key
        ),
        None,
    )
    advisory_parents = {
        value
        for value in (
            candidate.bookmark_parent,
            candidate.provider_parent,
        )
        if value is not None and value in selected and value != key
    }
    if any(
        selected[value].native_segment_id not in {None, segment_id}
        for value in advisory_parents
    ):
        conflicts.append(
            {
                "relation": "heading_parent_segment_conflict",
                "source_item_indices": _ref_indices(key),
            }
        )
    if parent is None:
        return None
    parent_candidate = selected[parent]
    if (
        not parent_candidate.propagates
        or not parent_candidate.native_hierarchy_valid
        or parent_candidate.native_segment_id != segment_id
    ):
        if not flatten_unsafe_hierarchy:
            candidate.propagates = False
        conflicts.append(
            {
                "relation": "native_heading_parent_unavailable",
                "source_item_indices": _ref_indices((*parent, *key)),
            }
        )
        return None
    return parent


def _heading_nodes(
    selected: Mapping[tuple[_Ref, ...], _Candidate],
    *,
    parent_key_by_key: Mapping[
        tuple[_Ref, ...],
        tuple[_Ref, ...] | None,
    ],
    carriers_by_ref: Mapping[_Ref, _Carrier],
    last_source_index: int,
) -> list[dict[str, Any]]:
    keys = sorted(
        selected,
        key=lambda key: (
            min(ref.source_item_index for ref in key),
            tuple((ref.source_item_index, ref.field, ref.index or -1) for ref in key),
        ),
    )
    node_id_by_key = {key: index for index, key in enumerate(keys, start=1)}
    level_by_key: dict[tuple[_Ref, ...], int] = {}
    for key in keys:
        parent = parent_key_by_key.get(key) if selected[key].propagates else None
        level_by_key[key] = level_by_key[parent] + 1 if parent is not None else 1

    def descendant(
        candidate: tuple[_Ref, ...],
        ancestor: tuple[_Ref, ...],
    ) -> bool:
        seen: set[tuple[_Ref, ...]] = set()
        parent = parent_key_by_key.get(candidate)
        while parent is not None and parent not in seen:
            if parent == ancestor:
                return True
            seen.add(parent)
            parent = parent_key_by_key.get(parent)
        return False

    output: list[dict[str, Any]] = []
    for position, key in enumerate(keys):
        candidate = selected[key]
        parent = parent_key_by_key.get(key) if candidate.propagates else None
        end = max(ref.source_item_index for ref in key)
        if candidate.propagates:
            end = last_source_index
            for later in keys[position + 1 :]:
                if not selected[later].propagates:
                    continue
                if not descendant(later, key):
                    end = min(ref.source_item_index for ref in later) - 1
                    break
        output.append(
            {
                "node_id": node_id_by_key[key],
                "heading_level": level_by_key[key],
                "propagates": candidate.propagates,
                "parent_node_id": (
                    node_id_by_key[parent] if parent is not None else None
                ),
                "section_span": [
                    min(ref.source_item_index for ref in key),
                    end,
                ],
                "source_refs": [
                    {
                        "source_item_index": ref.source_item_index,
                        "field": ref.field,
                        **({"index": ref.index} if ref.index is not None else {}),
                        "text_span": [
                            0,
                            len(carriers_by_ref[ref].source_value),
                        ],
                    }
                    for ref in key
                ],
                "evidence_kinds": sorted(candidate.evidence),
                **(
                    {
                        "native_role": candidate.native_role,
                        "native_node_id": candidate.native_node_id,
                        "native_segment_id": candidate.native_segment_id,
                    }
                    if candidate.native_role
                    and candidate.native_node_id is not None
                    and candidate.native_segment_id is not None
                    else {}
                ),
            }
        )
    return output


def _owner_scope_breaks(
    candidates: Sequence[_OwnerScopeCandidate],
    *,
    headings: Sequence[Mapping[str, Any]],
    carriers_by_ref: Mapping[_Ref, _Carrier],
    native_layout_witnesses: Mapping[_Ref, _NativeLayoutWitness],
    source_item_hashes: Mapping[int, str],
    content_list: list[dict[str, Any]],
    frame_member_indices: set[int],
) -> list[dict[str, Any]]:
    """Close unsafe sibling carry-over without minting a missing heading.

    A record is emitted only when the current accepted owner has an exact
    native anchor and the source layout proves a new block start.  The record
    is intentionally weaker than a heading: downstream may only lift content
    to an already-proven ancestor or the document root.
    """

    heading_by_id = {int(heading["node_id"]): heading for heading in headings}

    def depth(heading: Mapping[str, Any]) -> int:
        result = 1
        parent = heading.get("parent_node_id")
        seen = {int(heading["node_id"])}
        while isinstance(parent, int) and parent not in seen:
            seen.add(parent)
            result += 1
            parent = heading_by_id[parent].get("parent_node_id")
        return result

    def heading_refs(heading: Mapping[str, Any]) -> tuple[_Ref, ...]:
        return tuple(
            _Ref(
                int(ref["source_item_index"]),
                str(ref["field"]),
                int(ref["index"]) if ref.get("index") is not None else None,
            )
            for ref in heading["source_refs"]
        )

    accepted_refs = {
        ref for heading in headings for ref in heading_refs(heading)
    }
    by_boundary: dict[int, list[_OwnerScopeCandidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.ref not in accepted_refs:
            by_boundary[candidate.ref.source_item_index].append(candidate)

    output: list[dict[str, Any]] = []
    for boundary, boundary_candidates in sorted(by_boundary.items()):
        if len(boundary_candidates) != 1:
            continue
        candidate = boundary_candidates[0]
        owners = [
            heading
            for heading in headings
            if bool(heading["propagates"])
            and int(heading["section_span"][0]) < boundary
            <= int(heading["section_span"][1])
        ]
        if not owners:
            continue
        owners.sort(key=depth)
        owner = owners[-1]
        owner_refs = heading_refs(owner)
        owner_layouts = [
            native_layout_witnesses.get(ref) for ref in owner_refs
        ]
        if not owner_layouts or any(layout is None for layout in owner_layouts):
            continue
        exact_owner_layouts = [
            layout for layout in owner_layouts if layout is not None
        ]
        if not all(layout.exact_lines for layout in exact_owner_layouts):
            continue
        owner_text = "".join(
            carriers_by_ref[ref].source_value for ref in owner_refs
        )
        relative_rank: Literal["peer", "higher", "unnumbered_peer"]
        target_node_id: int | None
        if candidate.eligibility_basis == "numbered_caption_native_break":
            candidate_rank = printed_number_rank(
                carriers_by_ref[candidate.ref].source_value
            )
            owner_rank = printed_number_rank(owner_text)
            if candidate_rank is None or owner_rank is None:
                continue
            if candidate_rank > owner_rank:
                # A deeper caption remains inside the current broad section.
                continue
            relative_rank = "peer" if candidate_rank == owner_rank else "higher"
            owner_path: list[Mapping[str, Any]] = [owner]
            parent_id = owner.get("parent_node_id")
            while isinstance(parent_id, int):
                parent = heading_by_id[parent_id]
                owner_path.append(parent)
                parent_id = parent.get("parent_node_id")
            ranked_path = [
                (
                    ancestor,
                    printed_number_rank(
                        "".join(
                            carriers_by_ref[ref].source_value
                            for ref in heading_refs(ancestor)
                        )
                    ),
                )
                for ancestor in owner_path
            ]
            peer = next(
                (
                    ancestor
                    for ancestor, rank in ranked_path
                    if rank == candidate_rank
                ),
                None,
            )
            if peer is not None:
                parent_id = peer.get("parent_node_id")
                target_node_id = (
                    int(parent_id) if isinstance(parent_id, int) else None
                )
            else:
                target_node_id = next(
                    (
                        int(ancestor["node_id"])
                        for ancestor, rank in ranked_path
                        if rank is not None and rank < candidate_rank
                    ),
                    None,
                )
        else:
            if _PRINTED_NUMBER_PREFIX.match(owner_text.lstrip()) is not None:
                continue
            if len(exact_owner_layouts) != 1 or not _same_native_display_style(
                candidate.layout,
                exact_owner_layouts[0],
            ):
                continue
            relative_rank = "unnumbered_peer"
            parent_node = owner.get("parent_node_id")
            target_node_id = (
                int(parent_node) if isinstance(parent_node, int) else None
            )
        selected_value = carriers_by_ref[candidate.ref].source_value
        output.append(
            {
                "boundary_source_ref": {
                    "source_item_index": candidate.ref.source_item_index,
                    "source_item_sha256": source_item_hashes[
                        candidate.ref.source_item_index
                    ],
                    "page_index": candidate.layout.page_idx,
                    "field": candidate.ref.field,
                    **(
                        {"index": candidate.ref.index}
                        if candidate.ref.index is not None
                        else {}
                    ),
                    "text_span": [0, len(selected_value)],
                    "value_sha256": source_value_sha256(selected_value),
                },
                "source_atom_orders": list(candidate.source_atom_orders),
                "eligibility_basis": candidate.eligibility_basis,
                "relative_rank": relative_rank,
                "current_owner_node_id": int(owner["node_id"]),
                "target_node_id": target_node_id,
                "boundary_carrier_scope": candidate.boundary_carrier_scope,
            }
        )
    _assign_owner_scope_materialization(
        output,
        heading_by_id=heading_by_id,
        content_list=content_list,
        frame_member_indices=frame_member_indices,
    )
    return output


def _assign_owner_scope_materialization(
    records: list[dict[str, Any]],
    *,
    heading_by_id: Mapping[int, Mapping[str, Any]],
    content_list: list[dict[str, Any]],
    frame_member_indices: set[int],
) -> None:
    """Decide how each break's target occurrence stays one physical segment.

    Placement never splits one proven section occurrence, so a non-root
    target that already owns earlier direct content must flatten exactly the
    one intervening accepted subtree.  Shapes this bounded policy cannot
    close fail loudly here instead of falling back to the previous sibling.
    """

    for record in records:
        target_node_id = record["target_node_id"]
        policy = "direct_target"
        flatten_root: int | None = None
        if target_node_id is not None:
            direct_runs = _owner_scope_target_runs(
                int(target_node_id),
                heading_by_id=heading_by_id,
                records=records,
                content_list=content_list,
                frame_member_indices=frame_member_indices,
                flattened_ids=frozenset(),
            )
            if direct_runs > 1:
                node: Mapping[str, Any] | None = heading_by_id[
                    int(record["current_owner_node_id"])
                ]
                while node is not None:
                    parent = node.get("parent_node_id")
                    if parent == target_node_id:
                        flatten_root = int(node["node_id"])
                        break
                    node = (
                        heading_by_id.get(parent)
                        if isinstance(parent, int)
                        else None
                    )
                if flatten_root is None:
                    raise ParserOutputContractError(
                        "owner scope break has no intervening subtree to flatten"
                    )
                flat_runs = _owner_scope_target_runs(
                    int(target_node_id),
                    heading_by_id=heading_by_id,
                    records=records,
                    content_list=content_list,
                    frame_member_indices=frame_member_indices,
                    flattened_ids=_owner_scope_subtree_ids(
                        flatten_root,
                        heading_by_id=heading_by_id,
                    ),
                )
                if flat_runs != 1:
                    raise ParserOutputContractError(
                        "owner scope flatten cannot close the target occurrence"
                    )
                span = heading_by_id[int(target_node_id)]["section_span"]
                for other in records:
                    if other is record:
                        continue
                    other_boundary = int(
                        other["boundary_source_ref"]["source_item_index"]
                    )
                    if int(span[0]) < other_boundary <= int(span[1]):
                        raise ParserOutputContractError(
                            "owner scope flatten overlaps another break"
                        )
                policy = "flatten_intervening_subtree"
        record["materialization_policy"] = policy
        record["flatten_subtree_root_node_id"] = flatten_root


def _owner_scope_subtree_ids(
    root_node_id: int,
    *,
    heading_by_id: Mapping[int, Mapping[str, Any]],
) -> frozenset[int]:
    children: dict[int, list[int]] = defaultdict(list)
    for node_id, heading in heading_by_id.items():
        parent = heading.get("parent_node_id")
        if isinstance(parent, int):
            children[parent].append(node_id)
    subtree = {root_node_id}
    stack = [root_node_id]
    while stack:
        for child in children[stack.pop()]:
            if child not in subtree:
                subtree.add(child)
                stack.append(child)
    return frozenset(subtree)


def _owner_scope_target_runs(
    target_node_id: int,
    *,
    heading_by_id: Mapping[int, Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    content_list: list[dict[str, Any]],
    frame_member_indices: set[int],
    flattened_ids: frozenset[int],
) -> int:
    """Mirror placement contiguity for one target over the provider stream."""

    def path_ids(heading: Mapping[str, Any]) -> tuple[int, ...]:
        path = [int(heading["node_id"])]
        parent = heading.get("parent_node_id")
        while isinstance(parent, int):
            path.append(parent)
            parent = heading_by_id[parent].get("parent_node_id")
        return tuple(reversed(path))

    target_path = path_ids(heading_by_id[target_node_id])
    active = {
        node_id: heading
        for node_id, heading in heading_by_id.items()
        if bool(heading["propagates"]) and node_id not in flattened_ids
    }
    dropped_carriers: set[int] = set()
    for heading in active.values():
        for raw_ref in heading["source_refs"]:
            if (
                raw_ref.get("field", "text") != "text"
                or raw_ref.get("index") is not None
            ):
                continue
            ref_index = int(raw_ref["source_item_index"])
            value = content_list[ref_index].get("text")
            if isinstance(value, str) and [
                int(part) for part in raw_ref["text_span"]
            ] == [0, len(value)]:
                dropped_carriers.add(ref_index)
    span = heading_by_id[target_node_id]["section_span"]
    runs = 0
    inside = False
    for index in range(int(span[0]), min(int(span[1]) + 1, len(content_list))):
        if index in frame_member_indices or index in dropped_carriers:
            continue
        item = content_list[index]
        raw_kind = str(item.get("type", ""))
        if raw_kind in {"header", "footer", "page_number"}:
            continue
        if raw_kind == "text":
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
        if raw_kind == "list":
            list_items = item.get("list_items")
            if not isinstance(list_items, list) or not any(
                isinstance(value, str) and value.strip() for value in list_items
            ):
                continue
        owners = [
            heading
            for heading in active.values()
            if int(heading["section_span"][0])
            <= index
            <= int(heading["section_span"][1])
        ]
        identities: list[tuple[int, ...] | None] = []
        if not owners:
            identities.append(None)
        else:
            owners.sort(key=lambda heading: len(path_ids(heading)))
            owner_path = path_ids(owners[-1])
            applicable = [
                other
                for other in records
                if int(other["current_owner_node_id"]) == owner_path[-1]
                and int(owners[-1]["section_span"][0])
                < int(other["boundary_source_ref"]["source_item_index"])
                <= index
            ]
            if applicable:
                latest = max(
                    applicable,
                    key=lambda other: int(
                        other["boundary_source_ref"]["source_item_index"]
                    ),
                )
                latest_boundary = int(
                    latest["boundary_source_ref"]["source_item_index"]
                )
                latest_target = latest.get("target_node_id")
                retargeted = (
                    path_ids(heading_by_id[int(latest_target)])
                    if latest_target is not None
                    else None
                )
                if (
                    index == latest_boundary
                    and latest.get("boundary_carrier_scope") == "selected_only"
                ):
                    identities.append(owner_path)
                    identities.append(retargeted)
                else:
                    identities.append(retargeted)
            else:
                identities.append(owner_path)
        for identity in identities:
            matches = identity == target_path
            if matches and not inside:
                runs += 1
            inside = matches
    return runs


def _same_native_display_style(
    candidate: _NativeLayoutWitness,
    accepted_owner: _NativeLayoutWitness,
) -> bool:
    """Match a repeated document-local display family without text semantics.

    The already-accepted owner must itself be a page-local display line.  A
    repeated candidate may be the modal line height on a table-heavy page, so
    requiring a second independent ``display_height`` vote there would make
    the boundary depend on the table's font distribution.  Exact geometry,
    centering, page-front placement, component closure, and equality to the
    accepted owner's native style remain mandatory.
    """

    if not (
        candidate.exact_lines
        and candidate.component_closed
        and candidate.centered
        and candidate.page_front
        and accepted_owner.exact_lines
        and accepted_owner.component_closed
        and accepted_owner.centered
        and accepted_owner.display_height
        and accepted_owner.page_front
    ):
        return False
    return bool(
        len(candidate.line_heights) == len(accepted_owner.line_heights)
        and len(candidate.blocks) == len(accepted_owner.blocks)
        and round(statistics.median(candidate.line_heights), 2)
        == round(statistics.median(accepted_owner.line_heights), 2)
    )


def _continuous_parents(
    parents: Mapping[tuple[_Ref, ...], tuple[_Ref, ...] | None],
    *,
    selected: Mapping[tuple[_Ref, ...], _Candidate],
    conflicts: list[dict[str, Any]],
    flatten_unsafe_hierarchy: bool = False,
) -> dict[tuple[_Ref, ...], tuple[_Ref, ...] | None]:
    """Reject explicit edges that cross a separate reading-order branch."""

    output = dict(parents)
    keys = sorted(
        output,
        key=lambda key: min(ref.source_item_index for ref in key),
    )

    def descendant(
        candidate: tuple[_Ref, ...],
        ancestor: tuple[_Ref, ...],
    ) -> bool:
        seen: set[tuple[_Ref, ...]] = set()
        current = output.get(candidate)
        while current is not None and current not in seen:
            if current == ancestor:
                return True
            seen.add(current)
            current = output.get(current)
        return False

    positions = {key: index for index, key in enumerate(keys)}
    for child in keys:
        parent = output[child]
        if parent is None:
            continue
        between = keys[positions[parent] + 1 : positions[child]]
        if any(
            candidate != parent and not descendant(candidate, parent)
            for candidate in between
        ):
            conflicts.append(
                {
                    "relation": "heading_parent_discontinuous",
                    "source_item_indices": _ref_indices(child),
                }
            )
            if not flatten_unsafe_hierarchy:
                selected[child].propagates = False
            output[child] = None
    for child, parent in list(output.items()):
        if parent is None or selected[parent].propagates:
            continue
        conflicts.append(
            {
                "relation": "heading_parent_anchor_only",
                "source_item_indices": _ref_indices(child),
            }
        )
        output[child] = None
    return output


def _native_visual_rows(
    lines: Mapping[
        tuple[int, int, int],
        tuple[NativeTextAtom, ...],
    ],
) -> tuple[_NativeVisualRow, ...]:
    """Merge adjacent native line fragments that paint one visual row."""

    rows: list[_NativeVisualRow] = []
    fragments: list[_NativeVisualRow] = []
    for ref, atoms in lines.items():
        if not atoms:
            continue
        bbox = (
            min(atom.bbox[0] for atom in atoms),
            min(atom.bbox[1] for atom in atoms),
            max(atom.bbox[2] for atom in atoms),
            max(atom.bbox[3] for atom in atoms),
        )
        fragments.append(
            _NativeVisualRow(
                bbox=bbox,
                line_refs=frozenset({ref}),
                height=statistics.median(
                    atom.bbox[3] - atom.bbox[1] for atom in atoms
                ),
            )
        )
    fragments.sort(key=lambda row: (row.bbox[1], row.bbox[0]))
    for fragment in fragments:
        if not rows or not _same_visual_row(rows[-1], fragment):
            rows.append(fragment)
            continue
        prior = rows[-1]
        bbox = (
            min(prior.bbox[0], fragment.bbox[0]),
            min(prior.bbox[1], fragment.bbox[1]),
            max(prior.bbox[2], fragment.bbox[2]),
            max(prior.bbox[3], fragment.bbox[3]),
        )
        rows[-1] = _NativeVisualRow(
            bbox=bbox,
            line_refs=prior.line_refs.union(fragment.line_refs),
            height=statistics.median((prior.height, fragment.height)),
        )
    return tuple(rows)


def _same_visual_row(left: _NativeVisualRow, right: _NativeVisualRow) -> bool:
    overlap = min(left.bbox[3], right.bbox[3]) - max(
        left.bbox[1], right.bbox[1]
    )
    if overlap < 0.5 * min(left.height, right.height):
        return False
    horizontal_gap = max(
        0.0,
        max(left.bbox[0], right.bbox[0])
        - min(left.bbox[2], right.bbox[2]),
    )
    return horizontal_gap <= 4 * max(left.height, right.height)


def _apply_native_line_grammar(
    grouped: dict[tuple[_Ref, ...], list[_Candidate]],
    *,
    carriers: Sequence[_Carrier],
    carrier_source_support: Mapping[
        tuple[int, str, int | None],
        CarrierSourceSupport,
    ]
    | None,
    source_pages: tuple[NativeTextPage, ...],
    conflicts: list[dict[str, Any]],
) -> tuple[
    tuple[_OwnerScopeCandidate, ...],
    Mapping[_Ref, _NativeLayoutWitness],
]:
    """Admit only rendered coarse headings and flatten uncertain detail.

    The provider, bookmark, and StructTree lanes may propose candidates.  The
    native PDF supplies a separate, deterministic rendered witness: a closed
    page-front display component or a complete numbered line at the document's
    own text margin.  No fixed document-wide font multiplier is used.
    """

    pages_by_idx = {page.page_idx: page for page in source_pages}
    by_sii: dict[int, list[_Carrier]] = defaultdict(list)
    carrier_by_ref = {carrier.ref: carrier for carrier in carriers}
    for carrier in carriers:
        by_sii[carrier.ref.source_item_index].append(carrier)

    lines_by_page: dict[
        int,
        dict[tuple[int, int, int], tuple[NativeTextAtom, ...]],
    ] = {}
    visual_rows_by_page: dict[int, tuple[_NativeVisualRow, ...]] = {}
    page_mode_height: dict[int, float] = {}
    all_line_lefts: list[float] = []
    for page in source_pages:
        mutable_lines: dict[tuple[int, int, int], list[NativeTextAtom]] = (
            defaultdict(list)
        )
        for atom in page.atoms:
            mutable_lines[atom.layout.line_ref].append(atom)
        lines = {
            ref: tuple(sorted(atoms, key=lambda atom: atom.order))
            for ref, atoms in mutable_lines.items()
        }
        lines_by_page[page.page_idx] = lines
        visual_rows = _native_visual_rows(lines)
        visual_rows_by_page[page.page_idx] = visual_rows
        heights = [
            round(
                statistics.median(
                    atom.bbox[3] - atom.bbox[1] for atom in atoms
                ),
                2,
            )
            for atoms in lines.values()
            if atoms
        ]
        if heights:
            counts = Counter(heights)
            page_mode_height[page.page_idx] = max(
                counts,
                key=lambda height: (counts[height], -height),
            )
        for atoms in lines.values():
            if atoms:
                all_line_lefts.append(min(atom.bbox[0] for atom in atoms))
    sorted_lefts = sorted(all_line_lefts)
    document_left = (
        sorted_lefts[int((len(sorted_lefts) - 1) * 0.05)]
        if sorted_lefts
        else 0.0
    )

    witness_cache: dict[_Ref, _NativeLayoutWitness | None] = {}
    atom_cache: dict[_Ref, tuple[NativeTextAtom, ...] | None] = {}

    def source_atoms(ref: _Ref) -> tuple[NativeTextAtom, ...] | None:
        if ref in atom_cache:
            return atom_cache[ref]
        carrier = carrier_by_ref.get(ref)
        if carrier is None:
            atom_cache[ref] = None
            return None
        page = pages_by_idx.get(carrier.page_idx)
        if page is None:
            atom_cache[ref] = None
            return None
        support = (
            carrier_source_support.get(
                (
                    carrier.ref.source_item_index,
                    carrier.ref.field,
                    carrier.ref.index,
                )
            )
            if carrier_source_support is not None
            else None
        )
        if support is not None and support.kind == "native_exact":
            atoms_by_order = {atom.order: atom for atom in page.atoms}
            atoms = tuple(
                atoms_by_order[order]
                for order in support.source_atom_orders
                if order in atoms_by_order
            )
            if len(atoms) != len(support.source_atom_orders):
                atom_cache[ref] = None
                return None
        else:
            # A visual-bound carrier proves visibility, not native style.
            # Missing support and provider bboxes are never allowed to mint a
            # deterministic native-layout heading witness.
            atom_cache[ref] = None
            return None
        if not atoms:
            atom_cache[ref] = None
            return None
        atom_cache[ref] = atoms
        return atoms

    def witness_ref(ref: _Ref) -> _NativeLayoutWitness | None:
        if ref in witness_cache:
            return witness_cache[ref]
        carrier = carrier_by_ref.get(ref)
        if carrier is None:
            witness_cache[ref] = None
            return None
        page = pages_by_idx.get(carrier.page_idx)
        atoms = source_atoms(ref)
        if page is None or atoms is None:
            witness_cache[ref] = None
            return None
        selected_by_line: dict[
            tuple[int, int, int],
            list[NativeTextAtom],
        ] = defaultdict(list)
        for atom in atoms:
            selected_by_line[atom.layout.line_ref].append(atom)
        ordered_refs = sorted(
            selected_by_line,
            key=lambda ref: min(atom.order for atom in selected_by_line[ref]),
        )
        line_bboxes: list[tuple[float, float, float, float]] = []
        line_heights: list[float] = []
        exact_lines = True
        for line_ref in ordered_refs:
            selected = tuple(
                sorted(selected_by_line[line_ref], key=lambda atom: atom.order)
            )
            complete = lines_by_page.get(page.page_idx, {}).get(line_ref, ())
            if tuple(atom.order for atom in selected) != tuple(
                atom.order for atom in complete
            ):
                exact_lines = False
            line_bboxes.append(
                (
                    min(atom.bbox[0] for atom in selected),
                    min(atom.bbox[1] for atom in selected),
                    max(atom.bbox[2] for atom in selected),
                    max(atom.bbox[3] for atom in selected),
                )
            )
            line_heights.append(
                statistics.median(
                    atom.bbox[3] - atom.bbox[1] for atom in selected
                )
            )
        selected_refs = frozenset(selected_by_line)
        page_rows = visual_rows_by_page.get(page.page_idx, ())
        selected_rows = tuple(
            row for row in page_rows if row.line_refs.intersection(selected_refs)
        )
        rows_closed = bool(
            selected_rows
            and frozenset(
                ref for row in selected_rows for ref in row.line_refs
            )
            == selected_refs
        )
        row_bboxes = tuple(row.bbox for row in selected_rows)
        row_heights = tuple(row.height for row in selected_rows)
        aligned = bool(
            row_bboxes
            and (
                max(box[0] for box in row_bboxes)
                - min(box[0] for box in row_bboxes)
                <= 2.25 * statistics.median(row_heights)
                or max((box[0] + box[2]) / 2 for box in row_bboxes)
                - min((box[0] + box[2]) / 2 for box in row_bboxes)
                <= 2.25 * statistics.median(row_heights)
            )
        )
        compact = bool(
            row_bboxes
            and all(
                right[1] - left[3]
                <= 2 * max(left_height, right_height)
                for (left, left_height), (right, right_height) in zip(
                    zip(row_bboxes, row_heights, strict=True),
                    zip(row_bboxes[1:], row_heights[1:], strict=True),
                    strict=False,
                )
            )
        )
        consistent_height = bool(
            row_heights
            and max(row_heights) <= 1.25 * min(row_heights)
        )
        component_closed = bool(
            rows_closed
            and bool(selected_rows)
            and aligned
            and compact
            and consistent_height
        )
        if comparison_text("".join(atom.text for atom in atoms)) != (
            carrier.comparison_value
        ):
            exact_lines = False
        modal_height = page_mode_height.get(page.page_idx, 0.0)
        effective_boxes = row_bboxes or tuple(line_bboxes)
        effective_heights = row_heights or tuple(line_heights)
        centered = bool(
            effective_boxes
            and all(
                abs(box[0] - (page.width - box[2])) <= 2 * height
                for box, height in zip(
                    effective_boxes,
                    effective_heights,
                    strict=True,
                )
            )
        )
        display_height = bool(
            modal_height > 0
            and effective_heights
            and all(
                round(height, 2) > modal_height
                for height in effective_heights
            )
        )
        result = _NativeLayoutWitness(
            page_idx=page.page_idx,
            blocks=frozenset(
                (atom.layout.flow_index, atom.layout.block_index)
                for atom in atoms
            ),
            line_bboxes=effective_boxes,
            line_heights=effective_heights,
            exact_lines=exact_lines,
            centered=centered,
            display_height=display_height,
            near_document_left=bool(
                effective_boxes
                and effective_boxes[0][0]
                <= document_left + 5 * effective_heights[0]
            ),
            page_front=bool(
                effective_boxes
                and max(box[3] for box in effective_boxes) <= page.height * 0.38
            ),
            component_closed=component_closed,
        )
        witness_cache[ref] = result
        return result

    def witness(sii: int) -> _NativeLayoutWitness | None:
        text_refs = [
            carrier.ref
            for carrier in by_sii.get(sii, ())
            if carrier.ref.field == "text" and carrier.raw_kind in _TEXT_KINDS
        ]
        return witness_ref(text_refs[0]) if len(text_refs) == 1 else None

    def document_title_component(sources: tuple[int, ...]) -> bool:
        layouts = [witness(source) for source in sources]
        if not layouts or any(layout is None for layout in layouts):
            return False
        closed = [layout for layout in layouts if layout is not None]
        if not all(
            layout.page_idx == 0
            and layout.exact_lines
            and layout.component_closed
            and layout.centered
            and layout.display_height
            and layout.page_front
            for layout in closed
        ):
            return False
        lines = sorted(
            (
                (box, height)
                for layout in closed
                for box, height in zip(
                    layout.line_bboxes,
                    layout.line_heights,
                    strict=True,
                )
            ),
            key=lambda item: (item[0][1], item[0][0]),
        )
        heights = [height for _, height in lines]
        return bool(heights) and max(heights) <= 1.25 * min(heights) and all(
            -0.5 * max(left_height, right_height)
            <= right_box[1] - left_box[3]
            <= 4 * max(left_height, right_height)
            for (left_box, left_height), (right_box, right_height) in zip(
                lines,
                lines[1:],
                strict=False,
            )
        )

    def numbered_heading(
        sii: int,
        layout: _NativeLayoutWitness | None = None,
        candidates: Sequence[_Candidate] = (),
    ) -> bool:
        layout = layout or witness(sii)
        carrier = next(
            (
                item
                for item in by_sii.get(sii, ())
                if item.ref.field == "text"
            ),
            None,
        )
        return bool(
            layout is not None
            and carrier is not None
            and any(
                "mineru_v2_title" in candidate.evidence
                for candidate in candidates
            )
            and layout.exact_lines
            and layout.component_closed
            and bool(layout.line_bboxes)
            and layout.near_document_left
            and _PRINTED_NUMBER_PREFIX.match(carrier.source_value.lstrip())
            is not None
        )

    def display_heading(layout: _NativeLayoutWitness | None) -> bool:
        return bool(
            layout is not None
            and layout.exact_lines
            and layout.component_closed
            and layout.line_bboxes
            and layout.centered
            and layout.display_height
        )

    def sentence_terminal(refs: tuple[_Ref, ...]) -> bool:
        """Reject complete statements that only look like numbered titles.

        Full-stop, question, exclamation, and semicolon endings close a
        sentence in the filing body.  A colon deliberately remains eligible:
        Chinese disclosures routinely use it for real section introductions.
        This is a recall-reducing, topic-neutral boundary rule; the source
        carrier remains ordinary content under its nearest reliable owner.
        """

        values: list[str] = []
        for ref in refs:
            carrier = next(
                (item for item in carriers if item.ref == ref),
                None,
            )
            if carrier is None:
                return False
            values.append(carrier.source_value)
        return "".join(values).rstrip().endswith(
            ("。", "！", "？", ".", "!", "?", "；", ";")
        )

    title_groups: dict[int, tuple[_Ref, ...]] = {
        refs[0].source_item_index: refs
        for refs in grouped
        if len(refs) == 1
    }
    text_ref_by_source = {
        carrier.ref.source_item_index: carrier.ref
        for carrier in carriers
        if carrier.ref.field == "text" and carrier.raw_kind in _TEXT_KINDS
    }

    # A page-0 printed document title may be split by the provider into a
    # title followed by paragraph carriers. Extend the provider anchor only
    # when the adjacent carriers independently form one exact, centered native
    # display component; the paragraph label itself contributes no title vote.
    for source in sorted(tuple(title_groups)):
        refs = title_groups.get(source)
        if refs is None:
            continue
        run = [source]
        while True:
            nxt = run[-1] + 1
            if nxt in title_groups or nxt not in text_ref_by_source:
                break
            candidate_run = (*run, nxt)
            if not document_title_component(candidate_run):
                break
            run.append(nxt)
        if len(run) == 1:
            continue
        candidates = grouped.pop(refs)
        merged_refs = tuple(text_ref_by_source[item] for item in run)
        grouped[merged_refs] = [
            _Candidate(
                refs=merged_refs,
                level=candidates[0].level,
                evidence=set().union(*(item.evidence for item in candidates)),
            )
        ]
        title_groups[source] = merged_refs
        conflicts.append(
            {
                "relation": "native_document_title_continuation",
                "source_item_indices": run,
            }
        )

    # Only a closed page-0 display component may merge adjacent candidates.
    # Bookmark and StructTree claims from the same authored PDF may repeat the
    # same wrong line split, so they remain recorded observations but are not
    # carried forward as witnesses for the merged rendered title.
    ordered_sources = sorted(title_groups)
    position = 0
    while position < len(ordered_sources):
        run = [ordered_sources[position]]
        cursor = position + 1
        while cursor < len(ordered_sources):
            nxt = ordered_sources[cursor]
            if nxt != run[-1] + 1:
                break
            refs_in_run = [title_groups[source] for source in (*run, nxt)]
            sides = [grouped[refs] for refs in refs_in_run]
            if (
                len({side[0].level for side in sides}) != 1
                or not all(
                    any(
                        "mineru_v2_title" in item.evidence
                        for item in side
                    )
                    for side in sides
                )
                or not document_title_component(tuple((*run, nxt)))
            ):
                break
            run.append(nxt)
            cursor += 1
        if len(run) > 1:
            refs_in_run = [title_groups[source] for source in run]
            candidates_in_run = [
                item for refs in refs_in_run for item in grouped[refs]
            ]
            if any(
                item.evidence.intersection({"bookmark", "struct_tree"})
                for item in candidates_in_run
            ):
                conflicts.append(
                    {
                        "relation": (
                            "authored_outline_split_rendered_document_title"
                        ),
                        "source_item_indices": run,
                    }
                )
            refs = tuple(ref for group in refs_in_run for ref in group)
            level = grouped[refs_in_run[0]][0].level
            for old_refs in refs_in_run:
                grouped.pop(old_refs)
                del title_groups[old_refs[0].source_item_index]
            grouped[refs] = [
                _Candidate(
                    refs=refs,
                    level=level,
                    evidence={"mineru_v2_title"},
                )
            ]
        position = max(cursor, position + 1)

    # A provider tail in the previous body block is not a heading.  The same
    # fail-closed rule applies at a page edge when the first and last native
    # lines have the same physical style and indent.
    for sii, refs in sorted(tuple(title_groups.items())):
        current_candidates = grouped.get(refs)
        layout = witness(sii)
        if not current_candidates or layout is None:
            continue
        prev = sii - 1
        same_page_midflow = bool(
            prev >= 0
            and prev not in title_groups
            and (previous := witness(prev)) is not None
            and previous.page_idx == layout.page_idx
            and previous.blocks.intersection(layout.blocks)
            and not numbered_heading(sii, layout, current_candidates)
            and not display_heading(layout)
        )
        cross_page = False
        if layout.page_idx > 0 and layout.line_bboxes:
            current_page = pages_by_idx[layout.page_idx]
            previous_page = pages_by_idx.get(layout.page_idx - 1)
            prior_lines = (
                list(lines_by_page.get(layout.page_idx - 1, {}).values())
                if previous_page is not None
                else []
            )
            substantial = [
                atoms
                for atoms in prior_lines
                if atoms
                and max(atom.bbox[2] for atom in atoms)
                - min(atom.bbox[0] for atom in atoms)
                > 4
                * statistics.median(
                    atom.bbox[3] - atom.bbox[1] for atom in atoms
                )
            ]
            if substantial:
                prior = max(
                    substantial,
                    key=lambda atoms: max(atom.bbox[3] for atom in atoms),
                )
                prior_box = (
                    min(atom.bbox[0] for atom in prior),
                    min(atom.bbox[1] for atom in prior),
                    max(atom.bbox[2] for atom in prior),
                    max(atom.bbox[3] for atom in prior),
                )
                prior_height = statistics.median(
                    atom.bbox[3] - atom.bbox[1] for atom in prior
                )
                current_box = layout.line_bboxes[0]
                current_height = layout.line_heights[0]
                current_width = current_box[2] - current_box[0]
                prior_width = prior_box[2] - prior_box[0]
                cross_page = bool(
                    previous_page is not None
                    and current_box[1] <= current_page.height * 0.15
                    and prior_box[3] >= previous_page.height * 0.85
                    and round(prior_height, 2) == round(current_height, 2)
                    and abs(prior_box[0] - current_box[0])
                    <= max(prior_height, current_height)
                    and current_width <= 8 * current_height
                    and prior_width >= 2 * current_width
                    and not numbered_heading(sii, layout, current_candidates)
                )
        if not same_page_midflow and not cross_page:
            continue
        conflicts.append(
            {
                "relation": (
                    "provider_title_midflow"
                    if same_page_midflow
                    else "provider_title_cross_page_continuation"
                ),
                "source_item_indices": [sii],
            }
        )
        del grouped[refs]
        title_groups.pop(sii, None)

    # A provider may style one numbered sentence as a title.  Native layout
    # proves where and how it was painted, not that a sentence-ending statement
    # is a section boundary.  More specific same/cross-page continuation proof
    # is evaluated first so the audit retains the strongest structural reason.
    # Remaining statements are suppressed before any display/numbered lane can
    # grant ``native_layout`` authority.
    for refs in tuple(grouped):
        if not sentence_terminal(refs):
            continue
        conflicts.append(
            {
                "relation": "provider_title_sentence_terminal",
                "source_item_indices": _ref_indices(refs),
            }
        )
        grouped.pop(refs)

    # Native layout qualifies an existing semantic claim; it never creates a
    # heading.  Uncertain fine candidates remain content under their nearest
    # accepted parent or the document root.
    for refs, candidates in grouped.items():
        sources = tuple(ref.source_item_index for ref in refs)
        rendered = document_title_component(sources)
        if not rendered and len(sources) == 1:
            layout = witness(sources[0])
            rendered = bool(
                layout is not None
                and layout.exact_lines
                and (
                    display_heading(layout)
                    or numbered_heading(sources[0], layout, candidates)
                )
            )
        if rendered:
            for candidate in candidates:
                candidate.evidence.add("native_layout")

    def support_for(ref: _Ref) -> CarrierSourceSupport | None:
        if carrier_source_support is None:
            return None
        return carrier_source_support.get(
            (ref.source_item_index, ref.field, ref.index)
        )

    def source_blocks(ref: _Ref) -> frozenset[tuple[int, int]] | None:
        atoms = source_atoms(ref)
        if atoms is None:
            return None
        return frozenset(
            (atom.layout.flow_index, atom.layout.block_index) for atom in atoms
        )

    def proved_table_body(ref: _Ref) -> _Carrier | None:
        carrier = carrier_by_ref.get(ref)
        support = support_for(ref)
        if (
            carrier is None
            or support is None
            or carrier.raw_kind != "table"
            or ref.field != "table_html"
            or support.kind not in {"native_exact", "visual_bound"}
        ):
            return None
        return carrier

    def normalized_line_height(layout: _NativeLayoutWitness) -> float:
        page = pages_by_idx[layout.page_idx]
        return statistics.median(layout.line_heights) * 1000.0 / page.height

    def caption_table_transition(
        carrier: _Carrier,
        layout: _NativeLayoutWitness,
    ) -> Literal["selected_only", "selected_and_same_carrier"] | None:
        """Require a source-backed caption line outside a table grid.

        MinerU may attach a heading-looking caption either to the table that
        starts below it or to the preceding page-fragment table.  Both forms
        are accepted only when the exact native line is geometrically between
        two non-overlapping, source-supported table regions.  This qualifies a
        conservative owner reset; it does not qualify a heading.
        """

        if not (
            layout.exact_lines
            and layout.component_closed
            and layout.line_bboxes
            and (layout.near_document_left or (layout.centered and layout.display_height))
        ):
            return None
        body_ref = _Ref(carrier.ref.source_item_index, "table_html")
        body = proved_table_body(body_ref)
        if body is None or body.page_idx != carrier.page_idx:
            return None
        gap_limit = 4 * normalized_line_height(layout)
        caption_box, body_box = carrier.bbox, body.bbox
        carrier_scope: Literal[
            "selected_only",
            "selected_and_same_carrier",
        ]
        if caption_box[3] <= body_box[1]:
            gap = body_box[1] - caption_box[3]
            transition_body_ref: _Ref | None = body_ref
            carrier_scope = "selected_and_same_carrier"
        elif body_box[3] <= caption_box[1]:
            gap = caption_box[1] - body_box[3]
            transition_body_ref = next(
                (
                    candidate.ref
                    for source in (carrier.ref.source_item_index + 1,)
                    for candidate in by_sii[source]
                    if candidate.page_idx == carrier.page_idx
                    and candidate.ref.field == "table_html"
                    and candidate.bbox[1] >= caption_box[3]
                    and proved_table_body(candidate.ref) is not None
                ),
                None,
            )
            if transition_body_ref is None:
                return None
            transition_body = carrier_by_ref[transition_body_ref]
            gap = max(gap, transition_body.bbox[1] - caption_box[3])
            carrier_scope = "selected_only"
        else:
            return None
        if gap < 0 or gap > gap_limit:
            return None
        assert transition_body_ref is not None
        transition_support = support_for(transition_body_ref)
        assert transition_support is not None
        if transition_support.kind == "visual_bound":
            return carrier_scope
        table_blocks = source_blocks(transition_body_ref)
        return (
            carrier_scope
            if table_blocks is not None and layout.blocks.isdisjoint(table_blocks)
            else None
        )

    def immediate_table_after_display(
        carrier: _Carrier,
        layout: _NativeLayoutWitness,
    ) -> bool:
        if not (
            layout.exact_lines
            and layout.component_closed
            and bool(layout.line_bboxes)
            and layout.centered
            and layout.page_front
        ):
            return False
        # A stacked title/subtitle/date component has one boundary candidate:
        # its first centered native line.  Later lines in that same block must
        # not independently reset ownership merely because a table follows.
        # This is source-layout evidence, not a text/date vocabulary.
        for source in sorted(by_sii):
            if source >= carrier.ref.source_item_index:
                break
            for prior in by_sii[source]:
                if (
                    prior.page_idx != carrier.page_idx
                    or prior.raw_kind != "text"
                    or prior.ref.field != "text"
                ):
                    continue
                prior_layout = witness_ref(prior.ref)
                if (
                    prior_layout is not None
                    and prior_layout.exact_lines
                    and prior_layout.component_closed
                    and prior_layout.centered
                    and layout.blocks.intersection(prior_layout.blocks)
                ):
                    return False
        intervening = 0
        lower_edge = carrier.bbox[3]
        for source in sorted(by_sii):
            if source <= carrier.ref.source_item_index:
                continue
            source_carriers = by_sii[source]
            if any(item.page_idx != carrier.page_idx for item in source_carriers):
                break
            body = next(
                (
                    item
                    for item in source_carriers
                    if item.ref.field == "table_html"
                    and proved_table_body(item.ref) is not None
                ),
                None,
            )
            if body is not None:
                gap = body.bbox[1] - lower_edge
                return 0 <= gap <= 4 * normalized_line_height(layout)
            text = next(
                (
                    item
                    for item in source_carriers
                    if item.ref.field == "text" and item.raw_kind == "text"
                ),
                None,
            )
            if text is None or intervening >= 1:
                return False
            text_layout = witness_ref(text.ref)
            if (
                text_layout is None
                or not text_layout.exact_lines
                or not text_layout.component_closed
                or not text_layout.centered
                or not layout.blocks.intersection(text_layout.blocks)
                or text.bbox[1] < lower_edge
            ):
                return False
            lower_edge = text.bbox[3]
            intervening += 1
        return False

    owner_scope_candidates: list[_OwnerScopeCandidate] = []
    for carrier in carriers:
        support = support_for(carrier.ref)
        if support is None or support.kind != "native_exact":
            continue
        layout = witness_ref(carrier.ref)
        if layout is None:
            continue
        carrier_scope = (
            caption_table_transition(carrier, layout)
            if carrier.raw_kind == "table"
            and carrier.ref.field == "table_caption"
            and _PRINTED_NUMBER_PREFIX.match(carrier.source_value.lstrip())
            else None
        )
        if (
            carrier.raw_kind == "table"
            and carrier.ref.field == "table_caption"
            and _PRINTED_NUMBER_PREFIX.match(carrier.source_value.lstrip())
            and carrier_scope is not None
        ):
            owner_scope_candidates.append(
                _OwnerScopeCandidate(
                    ref=carrier.ref,
                    source_atom_orders=support.source_atom_orders,
                    eligibility_basis="numbered_caption_native_break",
                    boundary_carrier_scope=carrier_scope,
                    layout=layout,
                )
            )
        elif (
            carrier.raw_kind == "text"
            and carrier.ref.field == "text"
            and _PRINTED_NUMBER_PREFIX.match(carrier.source_value.lstrip()) is None
            and immediate_table_after_display(carrier, layout)
        ):
            owner_scope_candidates.append(
                _OwnerScopeCandidate(
                    ref=carrier.ref,
                    source_atom_orders=support.source_atom_orders,
                    eligibility_basis="unnumbered_display_peer_break",
                    boundary_carrier_scope="selected_and_same_carrier",
                    layout=layout,
                )
            )

    return (
        tuple(owner_scope_candidates),
        {
            ref: layout
            for ref, layout in witness_cache.items()
            if layout is not None
        },
    )


def _bookmark_candidates(
    bookmarks: tuple[NativeBookmark, ...],
    *,
    carriers_by_page: Mapping[int, list[_Carrier]],
    conflicts: list[dict[str, Any]],
) -> list[_Candidate]:
    """Bind only outline entries whose page and heading level are usable.

    A destination without a page and a level outside the heading domain stay
    valid native artifact facts; they simply cannot open a section here.
    """

    output: list[_Candidate] = []
    stack: list[tuple[int, tuple[_Ref, ...] | None]] = []
    for bookmark in bookmarks:
        page_idx, level = bookmark.page_idx, bookmark.level
        destination_y = bookmark.destination_y
        if page_idx is None or level > 32:
            conflicts.append(
                {
                    "relation": "bookmark_invalid",
                    "bookmark_order": bookmark.bookmark_order,
                    "source_item_indices": [],
                }
            )
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        matches = [
            carrier.ref
            for carrier in carriers_by_page.get(page_idx, [])
            if carrier.raw_kind in _TEXT_KINDS
            and carrier.comparison_value == comparison_text(bookmark.title)
            and (
                destination_y is None
                or carrier.bbox[1] - 80 <= destination_y <= carrier.bbox[3] + 80
            )
        ]
        refs = (matches[0],) if len(matches) == 1 else None
        if refs is not None:
            output.append(
                _Candidate(
                    refs=refs,
                    level=level,
                    evidence={"bookmark"},
                    bookmark_parent=next(
                        (parent for _, parent in reversed(stack) if parent),
                        None,
                    ),
                )
            )
        else:
            conflicts.append(
                {
                    "relation": (
                        "bookmark_unaligned" if not matches else "bookmark_ambiguous"
                    ),
                    "bookmark_order": bookmark.bookmark_order,
                    "source_item_indices": _ref_indices(tuple(matches)),
                }
            )
        stack.append((level, refs))
    return output


def _v2_title_candidates(
    content_list_v2: list[list[dict[str, Any]]] | None,
    *,
    carriers_by_ref: Mapping[_Ref, _Carrier],
    text_projections: MinerUTextProjectionSet | None,
) -> list[_Candidate]:
    """Bind typed titles through the proved page/block ordinal bijection."""

    if content_list_v2 is None:
        return []
    if text_projections is None:
        raise ParserOutputContractError("MinerU v2 titles require a text projection")
    output: list[_Candidate] = []
    stack: list[tuple[int, tuple[_Ref, ...] | None]] = []
    for local_page_idx, blocks in enumerate(content_list_v2):
        for block_idx, block in enumerate(blocks):
            if block.get("type") != "title":
                continue
            level = _v2_title_level(block)
            while stack and stack[-1][0] >= level:
                stack.pop()
            source_item_index = text_projections.legacy_index(
                local_page_idx,
                block_idx,
            )
            ref = _Ref(source_item_index, "text")
            carrier = carriers_by_ref.get(ref)
            if (
                carrier is None
                or carrier.raw_kind != "text"
                or not carrier.comparison_value
            ):
                raise ParserOutputContractError(
                    "MinerU v2 title ordinal does not resolve to a text carrier"
                )
            refs = (ref,)
            output.append(
                _Candidate(
                    refs=refs,
                    level=level,
                    evidence={"mineru_v2_title"},
                    provider_parent=next(
                        (parent for _, parent in reversed(stack) if parent is not None),
                        None,
                    ),
                )
            )
            stack.append((level, refs))
    return output


def _v2_title_level(block: Mapping[str, Any]) -> int:
    content = block.get("content")
    if not isinstance(content, Mapping):
        raise ParserOutputContractError("MinerU v2 title content must be an object")
    level = content.get("level")
    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 32:
        raise ParserOutputContractError("MinerU v2 title requires content and level")
    return level


def _align_node(
    node: NativeStructureNode,
    *,
    objects_by_ref: Mapping[tuple[int, int], tuple[NativeMarkedObject, ...]],
    carriers_by_page: Mapping[int, list[_Carrier]],
) -> tuple[_Ref, ...] | None:
    objects = [
        obj for ref in node.mcid_refs for obj in objects_by_ref.get(ref, ())
    ]
    pages = {obj.page_idx for obj in objects}
    if not objects or len(pages) != 1:
        return None
    page_idx = next(iter(pages))
    objects.sort(key=lambda obj: obj.object_order)
    assignments: list[tuple[_Carrier, str, tuple[float, float, float, float]]] = []
    for obj in objects:
        text = comparison_text(obj.text or "")
        bbox = obj.bbox
        if not text or bbox is None:
            continue
        matches = [
            carrier
            for carrier in carriers_by_page.get(page_idx, [])
            if carrier.raw_kind in _TEXT_KINDS
            and _contains(carrier.bbox, bbox)
            and text in carrier.comparison_value
        ]
        if len(matches) != 1:
            return None
        assignments.append((matches[0], text, bbox))
    if not assignments:
        return None
    ordered: list[_Carrier] = []
    for carrier, _, _ in assignments:
        if not ordered or ordered[-1].ref != carrier.ref:
            ordered.append(carrier)
    if [carrier.ref.source_item_index for carrier in ordered] != sorted(
        {carrier.ref.source_item_index for carrier in ordered}
    ):
        return None
    for carrier in ordered:
        parts = [
            text for assigned, text, _ in assignments if assigned.ref == carrier.ref
        ]
        if "".join(parts) != carrier.comparison_value:
            return None
    return tuple(carrier.ref for carrier in ordered)


def _native_page_frames(
    carriers: list[_Carrier],
    *,
    native_artifact_sources: set[int],
    heading_sources: set[int],
) -> list[dict[str, Any]]:
    by_index = {
        carrier.ref.source_item_index: carrier
        for carrier in carriers
        if carrier.ref.field == "text"
    }
    frames: list[dict[str, Any]] = []
    for source in sorted(native_artifact_sources - heading_sources):
        carrier = by_index.get(source)
        if carrier is None:
            continue
        frames.append(_frame(len(frames) + 1, carrier))
    return frames


def _frame(
    number: int,
    carrier: _Carrier,
) -> dict[str, Any]:
    source = carrier.ref.source_item_index
    return {
        "group_id": f"frame_{number}",
        "role": "running_furniture",
        "member_source_item_indices": [source],
        "representative_source_item_index": source,
        "proof_kind": "native_artifact",
    }


def _carriers(
    content_list: list[dict[str, Any]],
    *,
    conflicts: list[dict[str, Any]],
    table_role_overrides: tuple[ResolvedTableRole, ...] = (),
) -> list[_Carrier]:
    """Keep only carriers with exact geometry and record every rejected item."""

    output: list[_Carrier] = []
    unbound: set[int] = set()
    for carrier in iter_mineru_text_carriers(
        content_list,
        table_role_overrides=table_role_overrides,
    ):
        if carrier.page_idx is None or carrier.bbox is None:
            if carrier.source_item_index not in unbound:
                unbound.add(carrier.source_item_index)
                conflicts.append(
                    {
                        "relation": "carrier_geometry_unbound",
                        "source_item_indices": [carrier.source_item_index],
                    }
                )
            continue
        item = content_list[carrier.source_item_index]
        raw_kind = str(item["type"])
        level = item.get("text_level")
        output.append(
            _Carrier(
                ref=_Ref(
                    carrier.source_item_index,
                    carrier.field,
                    carrier.index,
                ),
                page_idx=carrier.page_idx,
                raw_kind=raw_kind,
                source_value=carrier.source_value,
                comparison_value=carrier.comparison_value,
                bbox=carrier.bbox,
                provider_level=(
                    level
                    if carrier.field == "text"
                    and raw_kind == "text"
                    and isinstance(level, int)
                    and not isinstance(level, bool)
                    and 1 <= level <= 32
                    else None
                ),
            )
        )
    return output


def _require_source_supported_carriers(
    carriers: list[_Carrier],
    *,
    carrier_source_support: Mapping[
        tuple[int, str, int | None],
        CarrierSourceSupport,
    ],
) -> None:
    """Reject structure inputs not closed by validated source-PDF evidence."""

    for carrier in carriers:
        key = (
            carrier.ref.source_item_index,
            carrier.ref.field,
            carrier.ref.index,
        )
        support = carrier_source_support.get(key)
        if (
            support is None
            or support.kind not in {"native_exact", "visual_bound"}
            or support.page_idx != carrier.page_idx
            or support.bbox != carrier.bbox
        ):
            raise ParserOutputContractError(
                f"MinerU structure carrier lacks validated source-PDF support: {key!r}"
            )


def _native_conflict(
    node: NativeStructureNode,
    relation: str,
    *,
    refs: tuple[_Ref, ...] = (),
) -> dict[str, Any]:
    return {
        "relation": relation,
        "native_node_id": node.node_id,
        "source_item_indices": _ref_indices(refs),
    }


def _ref_indices(refs: tuple[_Ref, ...]) -> list[int]:
    return sorted({ref.source_item_index for ref in refs})


def _contains(
    carrier: tuple[float, float, float, float],
    anchor: tuple[float, float, float, float],
) -> bool:
    center_x, center_y = (anchor[0] + anchor[2]) / 2, (anchor[1] + anchor[3]) / 2
    return (
        carrier[0] - 8 <= center_x <= carrier[2] + 8
        and carrier[1] - 8 <= center_y <= carrier[3] + 8
    )


__all__ = ["build_mineru_structure_proof"]
