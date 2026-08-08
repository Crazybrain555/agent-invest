"""Closed contract for parser-produced document structure evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import re
from typing import Any, Never, cast


DOCUMENT_STRUCTURE_VERSION = "document_structure.v1"
LEGACY_DOCUMENT_STRUCTURE_ALGORITHM = "document-structure-evidence.v10"
PREVIOUS_DOCUMENT_STRUCTURE_ALGORITHM = "document-structure-evidence.v11"
OWNER_SCOPE_V1_DOCUMENT_STRUCTURE_ALGORITHM = "document-structure-evidence.v12"
DOCUMENT_STRUCTURE_ALGORITHM = "document-structure-evidence.v13"

_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_LEGACY_ROOT_FIELDS = frozenset(
    {
        "algorithm_version",
        "carrier_set_sha256",
        "conflicts",
        "contract_version",
        "coverage",
        "headings",
        "native",
        "page_frames",
        "source_pdf_page_count",
        "source_pdf_sha256",
    }
)
_ROOT_FIELDS = _LEGACY_ROOT_FIELDS | {"owner_scope_breaks"}
_HEADING_FIELDS = frozenset(
    {
        "evidence_kinds",
        "heading_level",
        "native_node_id",
        "native_role",
        "native_segment_id",
        "node_id",
        "parent_node_id",
        "propagates",
        "section_span",
        "source_refs",
    }
)
_FRAME_FIELDS = frozenset(
    {
        "group_id",
        "member_source_item_indices",
        "proof_kind",
        "representative_source_item_index",
        "role",
    }
)
_OWNER_SCOPE_BREAK_V1_FIELDS = frozenset(
    {
        "boundary_start_order",
        "eligibility_basis",
        "page_index",
        "relative_rank",
        "source_atom_orders",
    }
)
_OWNER_SCOPE_BREAK_FIELDS = frozenset(
    {
        "boundary_carrier_scope",
        "boundary_source_ref",
        "current_owner_node_id",
        "eligibility_basis",
        "flatten_subtree_root_node_id",
        "materialization_policy",
        "relative_rank",
        "source_atom_orders",
        "target_node_id",
    }
)
_OWNER_SCOPE_MATERIALIZATION_POLICIES = frozenset(
    {"direct_target", "flatten_intervening_subtree"}
)
_BOUNDARY_SOURCE_REF_FIELDS = frozenset(
    {
        "field",
        "index",
        "page_index",
        "source_item_index",
        "source_item_sha256",
        "text_span",
        "value_sha256",
    }
)
_LEGACY_EVIDENCE = frozenset(
    {"bookmark", "mineru_v2_title", "printed_toc", "struct_tree"}
)
_EVIDENCE = _LEGACY_EVIDENCE | {"native_layout"}
_HEADING_SOURCE_KIND_PAIRS = frozenset(
    {
        ("page_furniture", "footer"),
        ("page_furniture", "header"),
        ("page_furniture", "page_number"),
        ("text", "aside_text"),
        ("text", "page_footnote"),
        ("text", "phonetic"),
        ("text", "ref_text"),
        ("text", "text"),
    }
)
_FRAME_ROLES = frozenset({"running_furniture"})
_FRAME_PROOFS = frozenset({"native_artifact"})
_NATIVE_STATUSES = frozenset({"malformed", "partial", "untagged", "usable"})


class DocumentStructureContractError(ValueError):
    """A proof is malformed or is not bound to its source carriers."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def carrier_set_sha256(elements: Sequence[Mapping[str, Any]]) -> str:
    """Hash ordered source identities without re-encoding their payloads."""

    identities: list[list[object]] = []
    for position, element in enumerate(elements):
        index = _integer(element.get("source_item_index"), minimum=0)
        digest = element.get("source_item_sha256")
        if index is None or not isinstance(digest, str) or not digest:
            _fail(
                "carrier_identity_invalid",
                f"carrier {position} lacks source index/hash",
            )
        identities.append([index, digest])
    encoded = json.dumps(
        identities,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_document_structure(
    value: object,
    *,
    elements: Sequence[Mapping[str, Any]],
    expected_source_pdf_sha256: str | None = None,
    available_artifact_roles: set[str] | None = None,
) -> Mapping[str, Any]:
    """Validate binding, heading DAG, exact spans, and frame ownership."""

    proof = _mapping(value, "structure_proof_invalid")
    algorithm = proof.get("algorithm_version")
    if proof.get("contract_version") != DOCUMENT_STRUCTURE_VERSION or algorithm not in {
        LEGACY_DOCUMENT_STRUCTURE_ALGORITHM,
        PREVIOUS_DOCUMENT_STRUCTURE_ALGORITHM,
        OWNER_SCOPE_V1_DOCUMENT_STRUCTURE_ALGORITHM,
        DOCUMENT_STRUCTURE_ALGORITHM,
    }:
        _fail("structure_proof_version_unsupported", "unsupported proof version")
    expected_fields = (
        _ROOT_FIELDS
        if algorithm
        in {
            OWNER_SCOPE_V1_DOCUMENT_STRUCTURE_ALGORITHM,
            DOCUMENT_STRUCTURE_ALGORITHM,
        }
        else _LEGACY_ROOT_FIELDS
    )
    if set(proof) != expected_fields:
        _fail("structure_proof_fields_invalid", "root fields are not closed")
    source_hash = proof["source_pdf_sha256"]
    if not isinstance(source_hash, str) or _SHA256_RE.fullmatch(source_hash) is None:
        _fail("structure_proof_source_hash_invalid", "invalid source PDF hash")
    if expected_source_pdf_sha256 is not None and source_hash != expected_source_pdf_sha256:
        _fail("structure_proof_source_hash_mismatch", "source PDF hash differs")
    page_count = _integer(proof["source_pdf_page_count"], minimum=1)
    if page_count is None:
        _fail("structure_proof_page_count_invalid", "invalid source PDF page count")
    if proof["carrier_set_sha256"] != carrier_set_sha256(elements):
        _fail("structure_proof_carrier_hash_mismatch", "carrier set hash differs")

    native = _mapping(proof["native"], "structure_proof_native_invalid")
    if set(native) != {"artifact_role", "status"}:
        _fail("structure_proof_native_invalid", "native fields are not closed")
    role = native["artifact_role"]
    if native["status"] not in _NATIVE_STATUSES or not isinstance(role, str) or not role:
        _fail("structure_proof_native_invalid", "invalid native evidence")
    if available_artifact_roles is not None and role not in available_artifact_roles:
        _fail("structure_proof_native_artifact_missing", "native artifact is absent")

    elements_by_index = {
        int(element["source_item_index"]): element for element in elements
    }
    headings = _list(proof["headings"], "structure_proof_headings_invalid")
    heading_by_id: dict[int, Mapping[str, Any]] = {}
    heading_members: set[int] = set()
    heading_spans: dict[tuple[int, str, int | None], list[tuple[int, int]]] = {}
    for raw_heading in headings:
        heading = _mapping(raw_heading, "structure_proof_heading_invalid")
        if not set(heading) <= _HEADING_FIELDS or (
            _HEADING_FIELDS
            - {"native_node_id", "native_role", "native_segment_id"}
        ) - set(heading):
            _fail("structure_proof_heading_invalid", "heading fields are invalid")
        node_id = _integer(heading["node_id"], minimum=1)
        level = _integer(heading["heading_level"], minimum=1, maximum=32)
        if node_id is None or node_id in heading_by_id or level is None:
            _fail("structure_proof_heading_invalid", "invalid heading id/level")
        evidence = _list(
            heading["evidence_kinds"],
            "structure_proof_heading_evidence_invalid",
        )
        allowed_evidence = (
            _LEGACY_EVIDENCE
            if algorithm == LEGACY_DOCUMENT_STRUCTURE_ALGORITHM
            else _EVIDENCE
        )
        if (
            not evidence
            or len(evidence) != len(set(evidence))
            or not set(evidence) <= allowed_evidence
        ):
            _fail("structure_proof_heading_evidence_invalid", "invalid evidence")
        native_fields = {
            field
            for field in ("native_node_id", "native_role", "native_segment_id")
            if field in heading
        }
        if "struct_tree" in evidence:
            if (
                native_fields
                != {"native_node_id", "native_role", "native_segment_id"}
                or _integer(heading.get("native_node_id"), minimum=1) is None
                or not isinstance(heading.get("native_role"), str)
                or not heading["native_role"]
                or not isinstance(heading.get("native_segment_id"), str)
                or not heading["native_segment_id"]
            ):
                _fail(
                    "structure_proof_native_ref_invalid",
                    "StructTree heading lacks exact native provenance",
                )
        elif native_fields:
            _fail(
                "structure_proof_native_ref_invalid",
                "non-native heading carries native provenance",
            )
        span = _range(heading["section_span"], "structure_proof_section_span_invalid")
        propagates = heading["propagates"]
        if not isinstance(propagates, bool):
            _fail(
                "structure_proof_heading_invalid",
                "heading propagation flag is invalid",
            )
        refs = _list(heading["source_refs"], "structure_proof_source_refs_invalid")
        if not refs:
            _fail("structure_proof_source_refs_invalid", "heading has no source")
        for raw_ref in refs:
            ref = _mapping(raw_ref, "structure_proof_source_ref_invalid")
            if (
                not {"source_item_index", "field", "text_span"} <= set(ref)
                or set(ref)
                - {"source_item_index", "field", "index", "text_span"}
            ):
                _fail("structure_proof_source_ref_invalid", "source ref is not closed")
            source_index = _integer(ref["source_item_index"], minimum=0)
            if (
                source_index is None
                or source_index not in elements_by_index
                or not span[0] <= source_index <= span[1]
            ):
                _fail("structure_proof_source_ref_invalid", "invalid heading source")
            text_span = _range(
                ref["text_span"],
                "structure_proof_text_span_invalid",
            )
            field = ref.get("field")
            index = ref.get("index")
            text = _source_text_value(
                elements_by_index[source_index],
                field=field,
                index=index,
            )
            if (
                text_span[0] == text_span[1] or text_span[1] > len(text)
            ):
                _fail("structure_proof_text_span_invalid", "invalid text span")
            assert isinstance(field, str)
            typed_index = index if isinstance(index, int) else None
            key = (source_index, field, typed_index)
            if any(
                text_span[0] < prior[1] and prior[0] < text_span[1]
                for prior in heading_spans.get(key, [])
            ):
                _fail(
                    "structure_proof_source_ref_invalid",
                    "heading source spans overlap",
                )
            heading_spans.setdefault(key, []).append(text_span)
            heading_members.add(source_index)
        ref_indices = {
            int(ref["source_item_index"])
            for ref in refs
        }
        if not propagates and (
            heading["parent_node_id"] is not None
            or span != (min(ref_indices), max(ref_indices))
        ):
            _fail(
                "structure_proof_heading_invalid",
                "anchor-only heading controls section structure",
            )
        heading_by_id[node_id] = heading
    _validate_parents(heading_by_id)

    prior_boundary = -1
    current_scope_breaks: list[Mapping[str, Any]] = []
    for raw_break in _list(
        proof.get("owner_scope_breaks", []),
        "structure_proof_owner_scope_breaks_invalid",
    ):
        scope_break = _mapping(
            raw_break,
            "structure_proof_owner_scope_break_invalid",
        )
        if algorithm == OWNER_SCOPE_V1_DOCUMENT_STRUCTURE_ALGORITHM:
            boundary = _validate_owner_scope_break_v1(
                scope_break,
                elements_by_index=elements_by_index,
                heading_members=heading_members,
                page_count=page_count,
            )
        else:
            boundary = _validate_owner_scope_break(
                scope_break,
                elements_by_index=elements_by_index,
                headings=heading_by_id,
                heading_members=heading_members,
                page_count=page_count,
            )
            current_scope_breaks.append(scope_break)
        if boundary <= prior_boundary:
            _fail(
                "structure_proof_owner_scope_break_invalid",
                "owner scope breaks are not strictly source ordered",
            )
        prior_boundary = boundary
    if current_scope_breaks:
        _validate_owner_scope_break_policies(
            current_scope_breaks,
            elements_by_index=elements_by_index,
            headings=heading_by_id,
            page_frames=proof["page_frames"],
        )

    frame_members: set[int] = set()
    frame_ids: set[str] = set()
    for raw_frame in _list(
        proof["page_frames"],
        "structure_proof_frames_invalid",
    ):
        frame = _mapping(raw_frame, "structure_proof_frame_invalid")
        group_id = frame.get("group_id")
        if (
            set(frame) != _FRAME_FIELDS
            or not isinstance(group_id, str)
            or not group_id
            or group_id in frame_ids
            or frame.get("role") not in _FRAME_ROLES
            or frame.get("proof_kind") not in _FRAME_PROOFS
        ):
            _fail("structure_proof_frame_invalid", "invalid frame")
        members = _indices(
            frame["member_source_item_indices"],
            elements_by_index=elements_by_index,
        )
        representative = frame["representative_source_item_index"]
        if (
            frame_members.intersection(members)
            or heading_members.intersection(members)
            or (representative is not None and representative not in members)
        ):
            _fail("structure_proof_frame_invalid", "frame ownership overlaps")
        frame_ids.add(group_id)
        frame_members.update(members)

    for raw_conflict in _list(
        proof["conflicts"],
        "structure_proof_conflicts_invalid",
    ):
        conflict = _mapping(raw_conflict, "structure_proof_conflict_invalid")
        allowed_conflict_fields = {
            "bookmark_order",
            "native_node_id",
            "native_roles",
            "relation",
            "source_item_indices",
        }
        if (
            set(conflict) - allowed_conflict_fields
            or not isinstance(conflict.get("relation"), str)
            or not conflict["relation"]
            or "source_item_indices" not in conflict
        ):
            _fail("structure_proof_conflict_invalid", "invalid conflict")
        _indices(
            conflict["source_item_indices"],
            elements_by_index=elements_by_index,
            allow_empty=True,
        )
        native_roles = conflict.get("native_roles")
        if native_roles is not None and (
            not isinstance(native_roles, list)
            or any(
                not isinstance(role, str) or not role
                for role in native_roles
            )
            or len(native_roles) != len(set(native_roles))
        ):
            _fail(
                "structure_proof_conflict_invalid",
                "invalid native conflict roles",
            )
    coverage = _mapping(proof["coverage"], "structure_proof_coverage_invalid")
    if any(_integer(value, minimum=0) is None for value in coverage.values()):
        _fail("structure_proof_coverage_invalid", "coverage must be counts")
    return proof


def _validate_owner_scope_break_v1(
    scope_break: Mapping[str, Any],
    *,
    elements_by_index: Mapping[int, Mapping[str, Any]],
    heading_members: set[int],
    page_count: int,
) -> int:
    boundary = _integer(scope_break.get("boundary_start_order"), minimum=0)
    page_index = _integer(scope_break.get("page_index"), minimum=0)
    atoms = _owner_scope_atom_orders(scope_break)
    basis = scope_break.get("eligibility_basis")
    relative_rank = scope_break.get("relative_rank")
    if (
        set(scope_break) != _OWNER_SCOPE_BREAK_V1_FIELDS
        or boundary is None
        or boundary not in elements_by_index
        or boundary in heading_members
        or page_index is None
        or page_index >= page_count
        or not atoms
        or basis
        not in {"numbered_layout_break", "unnumbered_display_table_start"}
        or (basis == "numbered_layout_break" and relative_rank != "peer_or_higher")
        or (basis == "unnumbered_display_table_start" and relative_rank != "unknown")
    ):
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "legacy owner scope break is not source-bound",
        )
    if _element_page_index(elements_by_index[boundary]) != page_index:
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope break page differs from its source carrier",
        )
    return boundary


def _validate_owner_scope_break(
    scope_break: Mapping[str, Any],
    *,
    elements_by_index: Mapping[int, Mapping[str, Any]],
    headings: Mapping[int, Mapping[str, Any]],
    heading_members: set[int],
    page_count: int,
) -> int:
    if set(scope_break) != _OWNER_SCOPE_BREAK_FIELDS:
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope break fields are not closed",
        )
    raw_ref = _mapping(
        scope_break.get("boundary_source_ref"),
        "structure_proof_owner_scope_break_invalid",
    )
    allowed_ref_fields = _BOUNDARY_SOURCE_REF_FIELDS
    if raw_ref.get("index") is None:
        allowed_ref_fields = allowed_ref_fields - {"index"}
    if set(raw_ref) != allowed_ref_fields:
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope boundary selector is not closed",
        )
    boundary = _integer(raw_ref.get("source_item_index"), minimum=0)
    page_index = _integer(raw_ref.get("page_index"), minimum=0)
    index = raw_ref.get("index")
    typed_index = _integer(index, minimum=0) if index is not None else None
    field = raw_ref.get("field")
    if (
        boundary is None
        or boundary not in elements_by_index
        or boundary in heading_members
        or page_index is None
        or page_index >= page_count
        or (index is not None and typed_index is None)
    ):
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope boundary identity is invalid",
        )
    element = elements_by_index[boundary]
    selected = _selected_source_text(element, field=field, index=typed_index)
    text_span = _range(
        raw_ref.get("text_span"),
        "structure_proof_owner_scope_break_invalid",
    )
    if (
        text_span != (0, len(selected))
        or raw_ref.get("value_sha256") != _source_value_sha256(selected)
        or raw_ref.get("source_item_sha256") != element.get("source_item_sha256")
        or _element_page_index(element) != page_index
        or not _owner_scope_atom_orders(scope_break)
    ):
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope boundary differs from its immutable source field",
        )

    owners = [
        heading
        for heading in headings.values()
        if bool(heading["propagates"])
        and int(heading["section_span"][0]) < boundary
        <= int(heading["section_span"][1])
    ]
    if not owners:
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope break has no accepted current owner",
        )
    owners.sort(key=lambda heading: _heading_depth(heading, headings=headings))
    owner = owners[-1]
    owner_path = _heading_path(owner, headings=headings)
    owner_ids = {int(item["node_id"]) for item in owner_path}
    if any(int(item["node_id"]) not in owner_ids for item in owners):
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope break current owner is ambiguous",
        )
    owner_id = int(owner["node_id"])
    if scope_break.get("current_owner_node_id") != owner_id:
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope break current owner was not independently derived",
        )

    basis = scope_break.get("eligibility_basis")
    relative_rank = scope_break.get("relative_rank")
    carrier_scope = scope_break.get("boundary_carrier_scope")
    target: int | None
    if basis == "numbered_caption_native_break":
        if field != "table_caption" or typed_index is None:
            _fail(
                "structure_proof_owner_scope_break_invalid",
                "numbered break must select one table caption",
            )
        candidate_rank = printed_number_rank(selected)
        owner_rank = printed_number_rank(
            _heading_text(owner, elements_by_index=elements_by_index)
        )
        expected_relative = (
            "peer"
            if candidate_rank is not None and candidate_rank == owner_rank
            else "higher"
            if candidate_rank is not None
            and owner_rank is not None
            and candidate_rank < owner_rank
            else None
        )
        if (
            expected_relative is None
            or relative_rank != expected_relative
            or carrier_scope not in {"selected_only", "selected_and_same_carrier"}
        ):
            _fail(
                "structure_proof_owner_scope_break_invalid",
                "numbered break rank or carrier scope is invalid",
            )
        assert candidate_rank is not None
        ranked_path = [
            (
                ancestor,
                printed_number_rank(
                    _heading_text(
                        ancestor,
                        elements_by_index=elements_by_index,
                    )
                ),
            )
            for ancestor in reversed(owner_path)
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
            parent = peer.get("parent_node_id")
            target = int(parent) if isinstance(parent, int) else None
        else:
            target = next(
                (
                    int(ancestor["node_id"])
                    for ancestor, rank in ranked_path
                    if rank is not None and rank < candidate_rank
                ),
                None,
            )
    elif basis == "unnumbered_display_peer_break":
        if (
            field != "text"
            or typed_index is not None
            or relative_rank != "unnumbered_peer"
            or carrier_scope != "selected_and_same_carrier"
            or printed_number_rank(
                _heading_text(owner, elements_by_index=elements_by_index)
            )
            is not None
        ):
            _fail(
                "structure_proof_owner_scope_break_invalid",
                "unnumbered peer break is invalid",
            )
        parent = owner.get("parent_node_id")
        target = int(parent) if isinstance(parent, int) else None
    else:
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope break eligibility is invalid",
        )
    if scope_break.get("target_node_id") != target:
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope break target was not independently derived",
        )
    flatten_root = scope_break.get("flatten_subtree_root_node_id")
    if scope_break.get(
        "materialization_policy"
    ) not in _OWNER_SCOPE_MATERIALIZATION_POLICIES or (
        flatten_root is not None and _integer(flatten_root, minimum=1) is None
    ):
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope break materialization is not a closed policy",
        )
    return boundary


def _owner_scope_atom_orders(scope_break: Mapping[str, Any]) -> tuple[int, ...]:
    raw = _list(
        scope_break.get("source_atom_orders"),
        "structure_proof_owner_scope_break_invalid",
    )
    atoms = tuple(_integer(value, minimum=0) for value in raw)
    if (
        not atoms
        or any(value is None for value in atoms)
        or len(atoms) != len(set(atoms))
        or tuple(sorted(cast(tuple[int, ...], atoms))) != atoms
    ):
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope source atom orders are invalid",
        )
    return cast(tuple[int, ...], atoms)


def _validate_owner_scope_break_policies(
    scope_breaks: Sequence[Mapping[str, Any]],
    *,
    elements_by_index: Mapping[int, Mapping[str, Any]],
    headings: Mapping[int, Mapping[str, Any]],
    page_frames: object,
) -> None:
    """Re-derive each break's materialization from the DAG and source order.

    A stored policy is only transport: whether a non-root target stays one
    physical occurrence is recomputed here from accepted section spans, the
    carrier order, and every break's retargeting effect.  Producer node lists
    are never trusted.
    """

    frame_members = {
        index
        for frame in cast(Sequence[Mapping[str, Any]], page_frames)
        for index in cast(
            Sequence[object], frame.get("member_source_item_indices", [])
        )
        if isinstance(index, int) and not isinstance(index, bool)
    }
    for scope_break in scope_breaks:
        policy = scope_break.get("materialization_policy")
        flatten_root = scope_break.get("flatten_subtree_root_node_id")
        target = scope_break.get("target_node_id")
        if target is None:
            if policy != "direct_target" or flatten_root is not None:
                _fail(
                    "structure_proof_owner_scope_break_invalid",
                    "a root-target break cannot flatten a subtree",
                )
            continue
        direct_runs = _target_occurrence_runs(
            int(target),
            headings=headings,
            elements_by_index=elements_by_index,
            scope_breaks=scope_breaks,
            frame_members=frame_members,
            flattened_ids=frozenset(),
        )
        if policy == "direct_target":
            if flatten_root is not None:
                _fail(
                    "structure_proof_owner_scope_break_invalid",
                    "a direct-target break cannot carry a flatten root",
                )
            if direct_runs > 1:
                _fail(
                    "structure_proof_owner_scope_break_invalid",
                    "noncontiguous target occurrence requires a flatten policy",
                )
            continue
        owner = headings[int(cast(int, scope_break["current_owner_node_id"]))]
        intervening: int | None = None
        node: Mapping[str, Any] | None = owner
        while node is not None:
            parent = node.get("parent_node_id")
            if parent == target:
                intervening = int(node["node_id"])
                break
            node = headings.get(parent) if isinstance(parent, int) else None
        if intervening is None or flatten_root != intervening:
            _fail(
                "structure_proof_owner_scope_break_invalid",
                "flatten root is not the intervening child of the target",
            )
        if direct_runs <= 1:
            _fail(
                "structure_proof_owner_scope_break_invalid",
                "flatten policy on an already contiguous target occurrence",
            )
        assert intervening is not None
        flat_runs = _target_occurrence_runs(
            int(target),
            headings=headings,
            elements_by_index=elements_by_index,
            scope_breaks=scope_breaks,
            frame_members=frame_members,
            flattened_ids=_subtree_node_ids(intervening, headings=headings),
        )
        if flat_runs != 1:
            _fail(
                "structure_proof_owner_scope_break_invalid",
                "flatten does not close the target occurrence",
            )
        span = cast(Sequence[int], headings[int(target)]["section_span"])
        for other in scope_breaks:
            if other is scope_break:
                continue
            other_boundary = int(
                cast(
                    Mapping[str, Any], other["boundary_source_ref"]
                )["source_item_index"]
            )
            if int(span[0]) < other_boundary <= int(span[1]):
                _fail(
                    "structure_proof_owner_scope_break_invalid",
                    "flatten overlaps another owner scope break",
                )


def _subtree_node_ids(
    root_node_id: int,
    *,
    headings: Mapping[int, Mapping[str, Any]],
) -> frozenset[int]:
    children: dict[int, list[int]] = {}
    for node_id, heading in headings.items():
        parent = heading.get("parent_node_id")
        if isinstance(parent, int):
            children.setdefault(parent, []).append(node_id)
    subtree = {root_node_id}
    stack = [root_node_id]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in subtree:
                subtree.add(child)
                stack.append(child)
    return frozenset(subtree)


def _target_occurrence_runs(
    target_node_id: int,
    *,
    headings: Mapping[int, Mapping[str, Any]],
    elements_by_index: Mapping[int, Mapping[str, Any]],
    scope_breaks: Sequence[Mapping[str, Any]],
    frame_members: set[int],
    flattened_ids: frozenset[int],
) -> int:
    """Count the physical segments one target occurrence would materialize.

    Mirrors publication placement conservatively: proven full-text heading
    carriers, page furniture, frame members, and blank text carriers never
    open or close a run, while content owned by another section identity ends
    the target's run.  Divergence from the real builder can only surface as a
    loud closure failure there, never as a silent placement.
    """

    target = headings[target_node_id]
    target_path = tuple(
        int(item["node_id"])
        for item in _heading_path(target, headings=headings)
    )
    active = {
        node_id: heading
        for node_id, heading in headings.items()
        if bool(heading["propagates"]) and node_id not in flattened_ids
    }
    dropped_carriers: set[int] = set()
    for heading in active.values():
        for raw_ref in cast(Sequence[Mapping[str, Any]], heading["source_refs"]):
            if raw_ref.get("field", "text") != "text" or raw_ref.get("index") is not None:
                continue
            ref_index = int(raw_ref["source_item_index"])
            value = elements_by_index.get(ref_index, {}).get("text")
            if isinstance(value, str) and tuple(
                int(part) for part in raw_ref["text_span"]
            ) == (0, len(value)):
                dropped_carriers.add(ref_index)
    span = cast(Sequence[int], target["section_span"])
    runs = 0
    inside = False
    for index in sorted(elements_by_index):
        if not int(span[0]) <= index <= int(span[1]):
            continue
        if index in frame_members or index in dropped_carriers:
            continue
        element = elements_by_index[index]
        kind = element.get("kind")
        if kind == "page_furniture":
            continue
        if kind == "text":
            text = element.get("text")
            if not isinstance(text, str) or not text.strip():
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
            owners.sort(key=lambda item: _heading_depth(item, headings=headings))
            owner = owners[-1]
            owner_path = tuple(
                int(item["node_id"])
                for item in _heading_path(owner, headings=headings)
            )
            applicable = [
                item
                for item in scope_breaks
                if int(cast(int, item["current_owner_node_id"])) == owner_path[-1]
                and int(owner["section_span"][0])
                < int(
                    cast(
                        Mapping[str, Any], item["boundary_source_ref"]
                    )["source_item_index"]
                )
                <= index
            ]
            if applicable:
                latest = max(
                    applicable,
                    key=lambda item: int(
                        cast(
                            Mapping[str, Any], item["boundary_source_ref"]
                        )["source_item_index"]
                    ),
                )
                latest_boundary = int(
                    cast(
                        Mapping[str, Any], latest["boundary_source_ref"]
                    )["source_item_index"]
                )
                latest_target = latest.get("target_node_id")
                retargeted = (
                    tuple(
                        int(item["node_id"])
                        for item in _heading_path(
                            headings[int(latest_target)],
                            headings=headings,
                        )
                    )
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


def _element_page_index(element: Mapping[str, Any]) -> int | None:
    value = element.get("page_idx")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    page_no = element.get("page_no")
    return (
        page_no - 1
        if isinstance(page_no, int) and not isinstance(page_no, bool)
        else None
    )


def _heading_depth(
    heading: Mapping[str, Any],
    *,
    headings: Mapping[int, Mapping[str, Any]],
) -> int:
    return len(_heading_path(heading, headings=headings))


def _heading_path(
    heading: Mapping[str, Any],
    *,
    headings: Mapping[int, Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    path = [heading]
    parent = heading.get("parent_node_id")
    while isinstance(parent, int):
        current = headings[parent]
        path.append(current)
        parent = current.get("parent_node_id")
    return tuple(reversed(path))


def _heading_text(
    heading: Mapping[str, Any],
    *,
    elements_by_index: Mapping[int, Mapping[str, Any]],
) -> str:
    parts: list[str] = []
    for raw_ref in cast(list[Mapping[str, Any]], heading["source_refs"]):
        value = _selected_source_text(
            elements_by_index[int(raw_ref["source_item_index"])],
            field=raw_ref.get("field"),
            index=raw_ref.get("index"),
        )
        start, end = (int(item) for item in raw_ref["text_span"])
        parts.append(value[start:end])
    return "".join(parts)


def _selected_source_text(
    element: Mapping[str, Any],
    *,
    field: object,
    index: object,
) -> str:
    if not isinstance(field, str):
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "source selector field is invalid",
        )
    value = element.get(field)
    if index is not None:
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not isinstance(value, list)
            or index >= len(value)
        ):
            _fail(
                "structure_proof_owner_scope_break_invalid",
                "indexed source selector is invalid",
            )
        value = value[index]
    if not isinstance(value, str):
        _fail(
            "structure_proof_owner_scope_break_invalid",
            "owner scope source selector is not textual",
        )
    return value


def _source_value_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def printed_number_rank(value: str) -> int | None:
    """Return the closed coarse numbering rank used by break contracts."""

    text = value.lstrip()
    match = re.match(
        r"^第[〇零一二三四五六七八九十百千万两]+([编篇章节])",
        text,
    )
    if match is not None:
        return {"编": 0, "篇": 0, "章": 1, "节": 2}[match.group(1)]
    if re.match(r"^[〇零一二三四五六七八九十百千万两]+[、．.]", text):
        return 1
    if re.match(r"^[（(][〇零一二三四五六七八九十百千万两]+[）)]", text):
        return 2
    dotted = re.match(r"^(\d+(?:\.\d+)+)(?:[、．.]|\s)+", text)
    if dotted is not None:
        return 3 + dotted.group(1).count(".")
    if re.match(r"^\d{1,4}、", text):
        return 1
    if re.match(r"^\d{1,4}[．.)）]", text):
        return 3
    if re.match(r"^[（(]\d{1,4}[）)]", text):
        return 4
    return None


def _validate_parents(headings: Mapping[int, Mapping[str, Any]]) -> None:
    for node_id, heading in headings.items():
        if heading["parent_node_id"] is None:
            if heading["heading_level"] != 1:
                _fail(
                    "structure_proof_parent_invalid",
                    "root heading depth must be canonical level 1",
                )
            continue
        seen = {node_id}
        child = heading
        while child["parent_node_id"] is not None:
            parent_id = _integer(child["parent_node_id"], minimum=1)
            parent = headings.get(parent_id) if parent_id is not None else None
            if (
                parent is None
                or parent_id in seen
                or not parent["propagates"]
                or parent["heading_level"] + 1
                != child["heading_level"]
                or parent["section_span"][0] > child["section_span"][0]
                or parent["section_span"][1] < child["section_span"][1]
                or (
                    "struct_tree" in child["evidence_kinds"]
                    and (
                        "struct_tree" not in parent["evidence_kinds"]
                        or parent["native_segment_id"]
                        != child["native_segment_id"]
                    )
                )
            ):
                _fail("structure_proof_parent_invalid", "invalid heading parent")
            assert parent_id is not None
            seen.add(parent_id)
            child = parent


def _source_text_value(
    element: Mapping[str, Any],
    *,
    field: object,
    index: object,
) -> str:
    if (
        field != "text"
        or index is not None
        or not isinstance(element.get("text"), str)
        or (element.get("kind"), element.get("raw_kind"))
        not in _HEADING_SOURCE_KIND_PAIRS
    ):
        _fail(
            "structure_proof_source_ref_invalid",
            "heading source must be a typed text carrier",
        )
    return str(element["text"])


def _indices(
    value: object,
    *,
    elements_by_index: Mapping[int, Mapping[str, Any]],
    allow_empty: bool = False,
) -> set[int]:
    result: set[int] = set()
    for raw_index in _list(value, "structure_proof_indices_invalid"):
        index = _integer(raw_index, minimum=0)
        if index is None or index not in elements_by_index or index in result:
            _fail("structure_proof_indices_invalid", "invalid source index")
        result.add(index)
    if not allow_empty and not result:
        _fail("structure_proof_indices_invalid", "source indices are empty")
    return result


def _range(value: object, reason: str) -> tuple[int, int]:
    values = _list(value, reason)
    if len(values) != 2:
        _fail(reason, "expected a two-item range")
    start, end = _integer(values[0], minimum=0), _integer(values[1], minimum=0)
    if start is None or end is None or start > end:
        _fail(reason, "invalid range")
    return start, end


def _integer(
    value: object,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        return None
    return value if maximum is None or value <= maximum else None


def _mapping(value: object, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(reason, "expected an object")
    return cast(Mapping[str, Any], value)


def _list(value: object, reason: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(reason, "expected an array")
    return value


def _fail(reason_code: str, message: str) -> Never:
    raise DocumentStructureContractError(reason_code, message)


__all__ = [
    "DOCUMENT_STRUCTURE_ALGORITHM",
    "DOCUMENT_STRUCTURE_VERSION",
    "LEGACY_DOCUMENT_STRUCTURE_ALGORITHM",
    "OWNER_SCOPE_V1_DOCUMENT_STRUCTURE_ALGORITHM",
    "DocumentStructureContractError",
    "carrier_set_sha256",
    "printed_number_rank",
    "validate_document_structure",
]
