"""Align explicit PDF structure to MinerU text carriers.

This module accepts explicit PDF tags/bookmarks and MinerU v2 ``title`` blocks.
It never promotes a legacy ``text_level`` hint by itself or infers a parent
from numbering or nearest-level proximity.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    mineru_provider_item_sha256,
)
from disclosure_anchor.adapters.parsers.mineru.source_evidence import (
    CarrierSourceSupport,
    ResolvedTableRole,
    comparison_text,
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
from disclosure_anchor.adapters.parsers.pdf_native_text import NativeTextPage
from disclosure_anchor.adapters.parsers.printed_toc import (
    printed_toc_witness,
)
from disclosure_anchor.application.contracts.document_structure import (
    DOCUMENT_STRUCTURE_ALGORITHM,
    DOCUMENT_STRUCTURE_VERSION,
    carrier_set_sha256,
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

    if source_pages is not None:
        _apply_native_line_grammar(
            grouped,
            carriers=carriers,
            source_pages=source_pages,
            conflicts=conflicts,
        )
    selected = _select_candidates(
        grouped,
        nonheading_roles=nonheading_roles,
        conflicts=conflicts,
        toc_corroborated=toc_corroborated,
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
    )
    parent_key_by_key = _continuous_parents(
        parent_key_by_key,
        selected=selected,
        conflicts=conflicts,
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
    # Carrier identity is the provider's raw item, exactly as the mapper
    # stamps elements: hashing the projected copy would fork the identity
    # whenever the serializer lane rewrites text (escape cleanup).
    identity_items = (
        identity_content_list
        if identity_content_list is not None
        else content_list
    )
    if len(identity_items) != len(content_list):
        raise ParserOutputContractError(
            "identity content list does not mirror the canonical list"
        )
    identities = [
        {
            "source_item_index": index,
            "source_item_sha256": mineru_provider_item_sha256(item),
        }
        for index, item in enumerate(identity_items)
    ]
    return {
        "contract_version": DOCUMENT_STRUCTURE_VERSION,
        "algorithm_version": DOCUMENT_STRUCTURE_ALGORITHM,
        "source_pdf_sha256": source_pdf_sha256,
        "source_pdf_page_count": page_count,
        "carrier_set_sha256": carrier_set_sha256(identities),
        "native": {
            "status": native.native_status,
            "artifact_role": "pdf_structure",
        },
        "headings": headings,
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
        if len(levels) > 1:
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
            chosen.propagates = False
            conflicts.append(
                {
                    "relation": "native_heading_non_section_ancestry",
                    "native_roles": sorted(non_section_ancestor_roles),
                    "source_item_indices": _ref_indices(refs),
                }
            )
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
        if roles.intersection({"TOC", "TOCI", "Table", "TD", "TH"}) and not (
            native or bookmarks
        ):
            chosen.propagates = False
        selected[refs] = chosen
        claimed.update(refs)
    return selected


def _candidate_sources(
    candidates: list[_Candidate],
    *,
    refs: tuple[_Ref, ...],
    conflicts: list[dict[str, Any]],
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
        candidates = [item for item in candidates if "struct_tree" not in item.evidence]
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


def _continuous_parents(
    parents: Mapping[tuple[_Ref, ...], tuple[_Ref, ...] | None],
    *,
    selected: Mapping[tuple[_Ref, ...], _Candidate],
    conflicts: list[dict[str, Any]],
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


def _carrier_native_blocks(
    carrier: _Carrier,
    pages_by_idx: Mapping[int, NativeTextPage],
) -> frozenset[tuple[int, int]]:
    """Poppler blocks whose word centers fall inside the carrier bbox."""

    page = pages_by_idx.get(carrier.page_idx)
    if page is None:
        return frozenset()
    x0, y0, x1, y1 = carrier.bbox
    blocks: set[tuple[int, int]] = set()
    for atom in page.atoms:
        center_x = (atom.bbox[0] + atom.bbox[2]) / 2 / page.width * 1000.0
        center_y = (atom.bbox[1] + atom.bbox[3]) / 2 / page.height * 1000.0
        if x0 <= center_x <= x1 and y0 <= center_y <= y1:
            blocks.add((atom.layout.flow_index, atom.layout.block_index))
    return frozenset(blocks)


def _apply_native_line_grammar(
    grouped: dict[tuple[_Ref, ...], list[_Candidate]],
    *,
    carriers: Sequence[_Carrier],
    source_pages: tuple[NativeTextPage, ...],
    conflicts: list[dict[str, Any]],
) -> None:
    """Let the native line layout arbitrate provider title claims.

    A printed title always owns its own layout block. A provider-typed
    title living inside another carrier's block is a wrapped sentence
    tail, not a heading (rejected); two adjacent provider titles sharing
    one block are the lines of a single printed title (merged, so the
    published heading joins their text).
    """

    pages_by_idx = {page.page_idx: page for page in source_pages}
    by_sii: dict[int, list[_Carrier]] = defaultdict(list)
    for carrier in carriers:
        by_sii[carrier.ref.source_item_index].append(carrier)
    blocks_cache: dict[int, frozenset[tuple[int, int]]] = {}

    def blocks(sii: int) -> frozenset[tuple[int, int]]:
        if sii not in blocks_cache:
            merged: set[tuple[int, int]] = set()
            for carrier in by_sii.get(sii, []):
                merged |= _carrier_native_blocks(carrier, pages_by_idx)
            blocks_cache[sii] = frozenset(merged)
        return blocks_cache[sii]

    def single_ref(refs: tuple[_Ref, ...]) -> int | None:
        return refs[0].source_item_index if len(refs) == 1 else None

    title_groups = {
        single_ref(refs): refs
        for refs in grouped
        if single_ref(refs) is not None
    }

    # Merge the lines of one printed title (adjacent title carriers
    # sharing a native block), longest chains first.
    merged_any = True
    while merged_any:
        merged_any = False
        for sii in sorted(title_groups):
            nxt = sii + 1
            if nxt not in title_groups:
                continue
            if not (blocks(sii) and blocks(sii) & blocks(nxt)):
                continue
            left_refs = title_groups[sii]
            right_refs = title_groups[nxt]
            left = grouped.pop(left_refs)
            right = grouped.pop(right_refs)
            refs = (*left_refs, *right_refs)
            evidence: set[str] = set()
            for item in (*left, *right):
                evidence |= item.evidence
            grouped[refs] = [
                _Candidate(
                    refs=refs,
                    level=left[0].level,
                    evidence=evidence,
                )
            ]
            del title_groups[sii]
            del title_groups[nxt]
            merged_any = True
            break

    # Reject a provider title that shares its block with a preceding
    # non-title carrier: a wrapped tail is not a heading. StructTree
    # evidence outranks the layout heuristic and is left alone.
    for sii, refs in sorted(title_groups.items()):
        candidates = grouped.get(refs)
        if not candidates:
            continue
        if any("struct_tree" in item.evidence for item in candidates):
            continue
        prev = sii - 1
        if prev < 0 or prev in title_groups or prev not in by_sii:
            continue
        shared = blocks(sii) & blocks(prev)
        if shared:
            conflicts.append(
                {
                    "relation": "provider_title_midflow",
                    "source_item_indices": [sii],
                }
            )
            del grouped[refs]


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
