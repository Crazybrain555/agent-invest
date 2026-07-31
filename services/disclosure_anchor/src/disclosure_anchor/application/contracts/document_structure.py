"""Closed contract for parser-produced document structure evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import re
from typing import Any, Never, cast


DOCUMENT_STRUCTURE_VERSION = "document_structure.v1"
DOCUMENT_STRUCTURE_ALGORITHM = "document-structure-evidence.v8"

_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_ROOT_FIELDS = frozenset(
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
_EVIDENCE = frozenset(
    {"bookmark", "mineru_v2_title", "printed_toc", "struct_tree"}
)
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
    if set(proof) != _ROOT_FIELDS:
        _fail("structure_proof_fields_invalid", "root fields are not closed")
    if (
        proof["contract_version"] != DOCUMENT_STRUCTURE_VERSION
        or proof["algorithm_version"] != DOCUMENT_STRUCTURE_ALGORITHM
    ):
        _fail("structure_proof_version_unsupported", "unsupported proof version")
    source_hash = proof["source_pdf_sha256"]
    if not isinstance(source_hash, str) or _SHA256_RE.fullmatch(source_hash) is None:
        _fail("structure_proof_source_hash_invalid", "invalid source PDF hash")
    if expected_source_pdf_sha256 is not None and source_hash != expected_source_pdf_sha256:
        _fail("structure_proof_source_hash_mismatch", "source PDF hash differs")
    if _integer(proof["source_pdf_page_count"], minimum=1) is None:
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
        if not evidence or len(evidence) != len(set(evidence)) or not set(evidence) <= _EVIDENCE:
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
    "DocumentStructureContractError",
    "carrier_set_sha256",
    "validate_document_structure",
]
