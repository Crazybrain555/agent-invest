"""Versioned source-to-unit projection contract.

Artifact locators remain useful navigation hints.  This contract separately
states which concrete NormalizedIR field owns each public unit field, so an
auditor never has to infer ownership by recursively walking arbitrary JSON.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from typing import Any, Mapping, cast

from disclosure_anchor.application.contracts.publication_safety import (
    conservative_semantic_segments,
)


UNIT_SOURCE_PROJECTION_VERSION = "unit-source-projection.v4"

NORMALIZED_IR_SOURCE_KIND = "normalized_ir_element"
SOURCE_EVIDENCE_ATOM_KIND = "source_evidence_atom"
SOURCE_EVIDENCE_GEOMETRY_ISSUE_KIND = "source_evidence_geometry_issue"
SOURCE_EVIDENCE_VISUAL_PAGE_KIND = "source_evidence_visual_page"
NATIVE_SOURCE_KINDS = frozenset(
    {
        SOURCE_EVIDENCE_ATOM_KIND,
        SOURCE_EVIDENCE_GEOMETRY_ISSUE_KIND,
        SOURCE_EVIDENCE_VISUAL_PAGE_KIND,
    }
)
SOURCE_IDENTITY_FIELDS = ("kind", "ir_id", "source_item_index", "order_index")
SOURCE_GEOMETRY_FIELDS = ("page_no", "bbox")
PUBLIC_ARTIFACT_LOCATOR_FIELDS = frozenset(
    {"evidence_artifacts", "review_reason", "source_projection"}
)

SOURCE_FIELD_KINDS = frozenset(
    {
        "text",
        "table",
        "table_caption",
        "table_note",
        "image",
        "image_caption",
        "image_footnote",
        "visual_subtype",
        "visual_semantic_text",
        "list_items",
        "list_subtype",
        "code_body",
        "code_caption",
        "code_footnote",
        "code_subtype",
        "text_format",
    }
)

PAYLOAD_PROJECTION_KINDS = frozenset(
    {
        "text_identity",
        "text_identity_exact",
        "text_concat",
        "table_identity",
        "image_identity",
        "container",
    }
)

HEADING_PROJECTION_KINDS = frozenset(
    {
        "source_field",
        "source_concat",
    }
)

class SearchTargetContractError(ValueError):
    """A search target is not closed over an audited source projection edge."""


def source_ref_from_locator(locator: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return one strict physical source reference.

    Builder input locators do not yet carry a discriminator, so a complete
    NormalizedIR triple is promoted to the explicit v4 variant. Native source
    evidence is already a closed discriminated reference and is never
    disguised as an IR element.
    """

    kind = locator.get("kind")
    if kind in NATIVE_SOURCE_KINDS:
        return _native_source_ref(locator, kind=str(kind))
    if kind not in {None, NORMALIZED_IR_SOURCE_KIND}:
        return None
    ir_id = locator.get("ir_id")
    source_item_index = locator.get("source_item_index")
    order_index = locator.get("order_index")
    if (
        not isinstance(ir_id, str)
        or not ir_id
        or not isinstance(source_item_index, int)
        or isinstance(source_item_index, bool)
        or not isinstance(order_index, int)
        or isinstance(order_index, bool)
    ):
        return None
    ref: dict[str, Any] = {
        "kind": NORMALIZED_IR_SOURCE_KIND,
        "ir_id": ir_id,
        "source_item_index": source_item_index,
        "order_index": order_index,
    }
    for field in SOURCE_GEOMETRY_FIELDS:
        if field in locator:
            ref[field] = locator[field]
    return ref


def source_ref_identity(ref: Mapping[str, Any]) -> tuple[object, ...] | None:
    """Return the immutable identity of one canonical v4 source reference."""

    canonical = source_ref_from_locator(ref)
    if canonical is None:
        return None
    kind = canonical["kind"]
    if kind == NORMALIZED_IR_SOURCE_KIND:
        return (
            kind,
            canonical["ir_id"],
            canonical["source_item_index"],
            canonical["order_index"],
        )
    if kind == SOURCE_EVIDENCE_ATOM_KIND:
        return (
            kind,
            canonical["source_evidence_sha256"],
            canonical["atom_index"],
        )
    if kind == SOURCE_EVIDENCE_GEOMETRY_ISSUE_KIND:
        return (
            kind,
            canonical["source_evidence_sha256"],
            canonical["page_idx"],
            canonical["word_order"],
        )
    return (
        kind,
        canonical["source_evidence_sha256"],
        canonical["page_idx"],
    )


def source_ref_sort_key(ref: Mapping[str, Any]) -> tuple[object, ...]:
    """Sort physical refs without inventing a shared IR/native order."""

    canonical = source_ref_from_locator(ref)
    if canonical is None:
        return (3,)
    if canonical["kind"] == NORMALIZED_IR_SOURCE_KIND:
        return (
            0,
            canonical["order_index"],
            canonical["source_item_index"],
            canonical["ir_id"],
        )
    page_idx = canonical["page_idx"]
    if canonical["kind"] == SOURCE_EVIDENCE_ATOM_KIND:
        return (1, page_idx, canonical["atom_order"], 0, canonical["atom_index"])
    if canonical["kind"] == SOURCE_EVIDENCE_GEOMETRY_ISSUE_KIND:
        return (1, page_idx, canonical["word_order"], 1)
    return (2, page_idx)


def _native_source_ref(
    locator: Mapping[str, Any],
    *,
    kind: str,
) -> dict[str, Any] | None:
    common = {
        "kind",
        "source_evidence_sha256",
        "source_pdf_sha256",
        "page_idx",
        "page_no",
    }
    specific = {
        SOURCE_EVIDENCE_ATOM_KIND: {
            "atom_index",
            "atom_order",
            "bbox",
            "char_span",
            "text_sha256",
        },
        SOURCE_EVIDENCE_GEOMETRY_ISSUE_KIND: {
            "word_order",
            "raw_bbox",
            "reason",
            "text_sha256",
        },
        SOURCE_EVIDENCE_VISUAL_PAGE_KIND: {
            "visual_sha256",
            *(("text_sha256",) if "text_sha256" in locator else ()),
        },
    }[kind]
    if set(locator) != common | specific:
        return None
    if not all(
        _is_sha256(locator.get(field))
        for field in ("source_evidence_sha256", "source_pdf_sha256")
    ):
        return None
    page_idx = locator.get("page_idx")
    page_no = locator.get("page_no")
    if (
        not _is_index(page_idx)
        or not _is_index(page_no)
    ):
        return None
    page_idx_value = cast(int, page_idx)
    page_no_value = cast(int, page_no)
    if page_no_value != page_idx_value + 1:
        return None
    if kind == SOURCE_EVIDENCE_ATOM_KIND:
        if (
            not _is_index(locator.get("atom_index"))
            or not _is_index(locator.get("atom_order"))
            or not _bbox(locator.get("bbox"))
            or not _span(locator.get("char_span"))
            or not _is_sha256(locator.get("text_sha256"))
        ):
            return None
    elif kind == SOURCE_EVIDENCE_GEOMETRY_ISSUE_KIND:
        raw_bbox = locator.get("raw_bbox")
        if (
            not _is_index(locator.get("word_order"))
            or (raw_bbox is not None and not _raw_bbox(raw_bbox))
            or not isinstance(locator.get("reason"), str)
            or not locator["reason"]
            or not _is_sha256(locator.get("text_sha256"))
        ):
            return None
    elif (
        not _is_sha256(locator.get("visual_sha256"))
        or (
            "text_sha256" in locator
            and not _is_sha256(locator.get("text_sha256"))
        )
    ):
        return None
    return dict(locator)


def _is_index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _bbox(value: object) -> bool:
    return (
        _raw_bbox(value)
        and isinstance(value, list)
        and float(value[0]) < float(value[2])
        and float(value[1]) < float(value[3])
    )


def _raw_bbox(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    )


def _span(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and _is_index(value[0])
        and _is_index(value[1])
        and value[0] < value[1]
    )


def source_selector(
    locator: Mapping[str, Any],
    *,
    field: str,
    index: int | None = None,
    char_span: list[int] | None = None,
    value_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Build a closed source-field selector from a concrete locator."""

    if field not in SOURCE_FIELD_KINDS:
        raise ValueError(f"unsupported source field: {field}")
    source = source_ref_from_locator(locator)
    if source is None:
        return None
    field_selector: dict[str, Any] = {"kind": field}
    if index is not None:
        field_selector["index"] = index
    if char_span is not None:
        field_selector["char_span"] = list(char_span)
    if value_sha256 is not None:
        field_selector["value_sha256"] = value_sha256
    return {"source": source, "field": field_selector}


def source_value_sha256(value: object) -> str:
    """Hash a selected source value using the projection contract encoding."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def empty_projection_graph() -> dict[str, Any]:
    """Return a fresh graph with every role present and no implicit edges."""

    return {
        "version": UNIT_SOURCE_PROJECTION_VERSION,
        "payload": None,
        "heading_path": [],
        "structured": [],
        "provenance": [],
        "search_targets": [],
        "search_atoms": [],
        "physical_context": None,
    }


def projection_target_value(
    payload: Mapping[str, Any],
    target_field: object,
) -> Any:
    """Resolve one declared payload path without recursively discovering fields."""

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


def search_text_values(
    *,
    payload_kind: str,
    payload: Mapping[str, Any],
    artifact_locator: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Replay ordered body-search text from explicit, audited target paths.

    A mixed container is deliberately non-searchable by itself. Its parts
    retain their own atomic projection graphs, so no second payload walker can
    invent fields or duplicate content.
    """

    if payload_kind == "mixed":
        _atomic_search_text_values(
            payload_kind="mixed",
            payload=payload,
            artifact_locator=artifact_locator,
        )
        graph = cast(
            Mapping[str, Any],
            cast(Mapping[str, Any], artifact_locator)["source_projection"],
        )
        search_atoms = graph.get("search_atoms")
        if not isinstance(search_atoms, list):
            raise SearchTargetContractError("search_atoms must be an array")
        if search_atoms:
            return _mixed_search_atom_values(payload, search_atoms)
        parts = payload.get("parts")
        if not isinstance(parts, list):
            raise SearchTargetContractError("mixed payload parts must be an array")
        values: list[str] = []
        for part in parts:
            if not isinstance(part, Mapping):
                raise SearchTargetContractError("mixed part must be an object")
            part_locator = part.get("artifact_locator")
            values.extend(
                search_text_values(
                    payload_kind=str(part.get("kind")),
                    payload=part,
                    artifact_locator=(
                        part_locator if isinstance(part_locator, Mapping) else None
                    ),
                )
            )
        return tuple(values)
    if payload_kind not in {"text", "table", "image"}:
        raise SearchTargetContractError(
            f"unsupported search payload kind: {payload_kind!r}"
        )
    return _atomic_search_text_values(
        payload_kind=payload_kind,
        payload=payload,
        artifact_locator=artifact_locator,
    )


def _atomic_search_text_values(
    *,
    payload_kind: str,
    payload: Mapping[str, Any],
    artifact_locator: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    if not isinstance(artifact_locator, Mapping):
        raise SearchTargetContractError("searchable payload lacks an artifact locator")
    graph = artifact_locator.get("source_projection")
    if not isinstance(graph, Mapping):
        raise SearchTargetContractError("searchable payload lacks source_projection")
    if graph.get("version") != UNIT_SOURCE_PROJECTION_VERSION:
        raise SearchTargetContractError(
            "searchable payload has an unsupported source_projection contract"
        )
    targets = graph.get("search_targets")
    search_atoms = graph.get("search_atoms")
    if not isinstance(targets, list):
        raise SearchTargetContractError("search_targets must be an array")
    if not isinstance(search_atoms, list):
        raise SearchTargetContractError("search_atoms must be an array")
    if payload_kind == "mixed":
        if targets:
            raise SearchTargetContractError(
                "mixed container must not duplicate part search targets"
            )
        return ()
    if search_atoms:
        raise SearchTargetContractError(
            "non-mixed payload cannot declare grouped search atoms"
        )

    non_primary_alternative = _is_non_primary_source_alternative(
        payload_kind=payload_kind,
        payload=payload,
    )
    if non_primary_alternative:
        if targets:
            raise SearchTargetContractError(
                "non-primary source alternative cannot declare a search target"
            )
        return ()

    required_targets = _required_primary_search_targets(
        payload_kind=payload_kind,
        payload=payload,
    )
    if required_targets and tuple(targets) != required_targets:
        raise SearchTargetContractError(
            "reader-visible payload does not declare its complete primary "
            "search target set"
        )

    payload_projection = graph.get("payload")
    safe_projection = bool(
        isinstance(payload_projection, Mapping)
        and str(payload_projection.get("transform", "")).startswith("safe_")
    )
    structured = graph.get("structured")
    if not isinstance(structured, list):
        raise SearchTargetContractError("structured projection must be an array")
    seen: set[str] = set()
    values: list[str] = []
    for target_field in targets:
        if (
            not isinstance(target_field, str)
            or not target_field.startswith("payload.")
            or target_field in seen
        ):
            raise SearchTargetContractError(
                "search target path is invalid or duplicated"
            )
        seen.add(target_field)
        owned_by_payload = _payload_edge_owns_target(
            payload_projection, target_field
        )
        owned_by_structured = any(
            isinstance(entry, Mapping)
            and entry.get("target_field") == target_field
            for entry in structured
        )
        if not (owned_by_payload or owned_by_structured):
            raise SearchTargetContractError(
                f"no source projection edge owns search target {target_field!r}"
            )
        _validate_search_target_for_kind(
            payload_kind=payload_kind,
            payload=payload,
            target_field=target_field,
        )
        value = projection_target_value(payload, target_field)
        for leaf in _search_leaf_text(value, target_field=target_field):
            segments = conservative_semantic_segments(leaf)
            if safe_projection:
                values.extend(
                    line
                    for segment in segments
                    for line in segment.splitlines()
                    if line
                )
            else:
                values.extend(segments)
    return tuple(value for value in values if value.strip())


def requires_primary_search_leaf(
    *,
    payload_kind: str,
    payload: Mapping[str, Any],
) -> bool:
    """Whether ordinary reader-visible semantics require a search leaf."""

    try:
        if _is_non_primary_source_alternative(
            payload_kind=payload_kind,
            payload=payload,
        ):
            return False
    except SearchTargetContractError:
        # The full search-contract validator reports the malformed role.  A
        # malformed marker must never make the carrier look exempt here.
        return True

    if payload_kind == "text" and "image_ref" not in payload:
        return _has_safe_semantic_text(payload.get("text"))
    if payload_kind != "table":
        return False
    if any(
        _has_safe_semantic_text(payload.get(field))
        for field in ("caption", "headers", "rows", "notes")
    ):
        return True
    media = payload.get("embedded_media")
    return isinstance(media, list) and any(
        isinstance(item, Mapping)
        and _has_safe_semantic_text(item.get("semantic_text"))
        for item in media
    )


def _required_primary_search_targets(
    *,
    payload_kind: str,
    payload: Mapping[str, Any],
) -> tuple[str, ...]:
    if not requires_primary_search_leaf(
        payload_kind=payload_kind,
        payload=payload,
    ):
        return ()
    if payload_kind == "text":
        return ("payload.text",)
    media = payload.get("embedded_media")
    media_targets = (
        tuple(
            f"payload.embedded_media.{index}.semantic_text"
            for index, item in enumerate(media)
            if isinstance(item, Mapping)
            and isinstance(item.get("semantic_text"), str)
            and bool(item["semantic_text"])
        )
        if isinstance(media, list)
        else ()
    )
    return (
        "payload.caption",
        "payload.headers",
        "payload.rows",
        "payload.notes",
        *media_targets,
    )


def _has_safe_semantic_text(value: object) -> bool:
    if isinstance(value, str):
        return any(part.strip() for part in conservative_semantic_segments(value))
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return any(_has_safe_semantic_text(item) for item in value)
    return False


def _is_non_primary_source_alternative(
    *,
    payload_kind: str,
    payload: Mapping[str, Any],
) -> bool:
    """Validate and identify the one closed non-primary representation role."""

    role = payload.get("representation_role")
    policy = payload.get("search_policy")
    if role is None and policy is None:
        return False
    if (
        payload_kind != "text"
        or role != "unresolved_source_alternative"
        or policy != "none"
    ):
        raise SearchTargetContractError(
            "representation_role/search_policy is open or invalid"
        )
    return True


def _mixed_search_atom_values(
    payload: Mapping[str, Any],
    entries: Sequence[object],
) -> tuple[str, ...]:
    parts = payload.get("parts")
    if not isinstance(parts, list):
        raise SearchTargetContractError("mixed payload parts must be an array")
    searchable: dict[str, str] = {}
    for part_index, raw_part in enumerate(parts):
        if not isinstance(raw_part, Mapping):
            raise SearchTargetContractError("mixed part must be an object")
        part_kind = str(raw_part.get("kind"))
        if part_kind != "text":
            continue
        raw_locator = raw_part.get("artifact_locator")
        locator = raw_locator if isinstance(raw_locator, Mapping) else None
        values = search_text_values(
            payload_kind=part_kind,
            payload=raw_part,
            artifact_locator=locator,
        )
        if values:
            if values != (raw_part.get("text"),):
                raise SearchTargetContractError(
                    "grouped native text part must expose one exact text target"
                )
            searchable[f"payload.parts.{part_index}.text"] = values[0]

    used: set[str] = set()
    used_runs: set[tuple[tuple[str, object], ...]] = set()
    output: list[str] = []
    previous_last = -1
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != {
            "boundary",
            "target_fields",
            "transform",
        }:
            raise SearchTargetContractError(
                "grouped search atom has an open or invalid field set"
            )
        boundary = raw_entry.get("boundary")
        target_fields = raw_entry.get("target_fields")
        if (
            not _search_boundary(boundary)
            or raw_entry.get("transform") != "exact_concat.v1"
            or not isinstance(target_fields, list)
            or not target_fields
        ):
            raise SearchTargetContractError(
                "grouped search atom boundary/transform is invalid"
            )
        run_identity: tuple[tuple[str, object], ...] | None = None
        if isinstance(boundary, Mapping) and boundary.get("kind") == (
            "source_evidence_run"
        ):
            run_identity = tuple(sorted(boundary.items()))
            if run_identity in used_runs:
                raise SearchTargetContractError(
                    "one source retrieval run cannot form multiple search atoms"
                )
            used_runs.add(run_identity)
        elif len(target_fields) != 1:
            raise SearchTargetContractError(
                "a singleton search boundary must own exactly one target"
            )
        resolved: list[str] = []
        indices: list[int] = []
        for target_field in target_fields:
            if (
                not isinstance(target_field, str)
                or target_field not in searchable
                or target_field in used
            ):
                raise SearchTargetContractError(
                    "grouped search target is invalid or duplicated"
                )
            index_text = target_field.removeprefix("payload.parts.").split(
                ".", 1
            )[0]
            indices.append(int(index_text))
            resolved.append(searchable[target_field])
            used.add(target_field)
        if (
            indices != sorted(indices)
            or len(indices) != len(set(indices))
            or indices[0] <= previous_last
        ):
            raise SearchTargetContractError(
                "grouped search targets are not strictly ordered"
            )
        previous_last = indices[-1]
        output.append("".join(resolved))
    if used != set(searchable):
        raise SearchTargetContractError(
            "grouped search atoms do not exactly cover searchable text parts"
        )
    return tuple(output)


def _search_boundary(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    kind = value.get("kind")
    if kind == "source_occurrence_singleton":
        return set(value) == {"kind"}
    return bool(
        kind == "source_evidence_run"
        and set(value)
        == {
            "kind",
            "page_idx",
            "run_index",
            "source_evidence_sha256",
        }
        and _is_index(value.get("page_idx"))
        and _is_index(value.get("run_index"))
        and _is_sha256(value.get("source_evidence_sha256"))
    )


def _payload_edge_owns_target(raw: object, target_field: str) -> bool:
    if not isinstance(raw, Mapping):
        return False
    edge_target = raw.get("target_field")
    if edge_target == target_field:
        return True
    return (
        edge_target == "payload"
        and raw.get("kind") == "table_identity"
        and target_field.startswith("payload.")
    )


def _validate_search_target_for_kind(
    *,
    payload_kind: str,
    payload: Mapping[str, Any],
    target_field: str,
) -> None:
    allowed = (
        {"payload.text"}
        if payload_kind == "text" and "image_ref" not in payload
        else {
            "payload.caption",
            "payload.content",
            "payload.semantic_text",
        }
        if payload_kind in {"text", "image"}
        else {
        "payload.caption",
        "payload.headers",
        "payload.rows",
        "payload.notes",
        }
        if payload_kind == "table"
        else set()
    )
    table_media_semantic = (
        payload_kind == "table"
        and target_field.startswith("payload.embedded_media.")
        and target_field.endswith(".semantic_text")
        and target_field.removeprefix("payload.embedded_media.").removesuffix(
            ".semantic_text"
        ).isdigit()
    )
    if target_field in allowed or table_media_semantic or (
        payload_kind in {"text", "image"}
        and target_field.startswith("payload.notes.")
        and target_field.removeprefix("payload.notes.").isdigit()
    ):
        return
    raise SearchTargetContractError(
        f"unsupported search target for {payload_kind}: {target_field!r}"
    )


def _search_leaf_text(value: object, *, target_field: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        output: list[str] = []
        for item in value:
            output.extend(_search_leaf_text(item, target_field=target_field))
        return output
    raise SearchTargetContractError(
        f"search target {target_field!r} is absent or not textual"
    )


def public_artifact_locator(
    locator: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Remove navigation mirrors that are derivable from typed selectors."""

    if locator is None:
        return None
    public = {
        key: locator[key]
        for key in PUBLIC_ARTIFACT_LOCATOR_FIELDS
        if key in locator
    }
    return public or None


def payload_source_refs(
    *,
    payload_kind: str,
    payload: Mapping[str, Any],
    artifact_locator: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], ...]:
    """Return the unique physical payload refs in deterministic source order."""

    locators: list[Mapping[str, Any] | None]
    if payload_kind == "mixed":
        parts = payload.get("parts")
        locators = (
            [
                part.get("artifact_locator")
                if isinstance(part, Mapping)
                and isinstance(part.get("artifact_locator"), Mapping)
                else None
                for part in parts
            ]
            if isinstance(parts, list)
            else []
        )
    else:
        locators = [artifact_locator]

    by_identity: dict[tuple[object, ...], dict[str, Any]] = {}
    for locator in locators:
        if not isinstance(locator, Mapping):
            continue
        graph = locator.get("source_projection")
        projection = graph.get("payload") if isinstance(graph, Mapping) else None
        sources = projection.get("sources") if isinstance(projection, Mapping) else None
        if not isinstance(sources, list):
            continue
        for selector in sources:
            source = selector.get("source") if isinstance(selector, Mapping) else None
            ref = source_ref_from_locator(source) if isinstance(source, Mapping) else None
            if ref is None:
                continue
            identity = source_ref_identity(ref)
            if identity is None:
                continue
            prior = by_identity.setdefault(identity, ref)
            if prior != ref:
                raise ValueError("payload source identity has conflicting geometry")
    return tuple(
        by_identity[identity]
        for identity in sorted(
            by_identity,
            key=lambda item: source_ref_sort_key(by_identity[item]),
        )
    )


def payload_page_no(
    *,
    payload_kind: str,
    payload: Mapping[str, Any],
    artifact_locator: Mapping[str, Any] | None,
) -> int | None:
    """Derive the public page anchor from the first physical payload ref."""

    refs = payload_source_refs(
        payload_kind=payload_kind,
        payload=payload,
        artifact_locator=artifact_locator,
    )
    if not refs:
        return None
    page_no = refs[0].get("page_no")
    return (
        page_no
        if isinstance(page_no, int)
        and not isinstance(page_no, bool)
        and page_no > 0
        else None
    )
