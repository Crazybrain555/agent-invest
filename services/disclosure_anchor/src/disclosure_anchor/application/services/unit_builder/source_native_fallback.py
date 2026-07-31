"""Materialize native source evidence as first-class canonical-stream units."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from disclosure_anchor.application.contracts.canonical_occurrence import (
    CanonicalOccurrenceStream,
)
from disclosure_anchor.application.contracts.source_evidence import (
    SourceEvidenceProof,
    VisualArtifactProof,
)
from disclosure_anchor.application.contracts.source_evidence_projection import (
    NativeEvidenceOccurrence,
    SourceEvidenceProjectionError,
    native_gap_physical_context,
    native_gap_search_atoms,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    empty_projection_graph,
    payload_source_refs,
    source_selector,
    source_value_sha256,
)
from disclosure_anchor.application.services.unit_builder.builder import (
    SourceEvidenceClosureError,
    UnitDraft,
)


def native_stream_unit_drafts(
    stream: CanonicalOccurrenceStream,
    *,
    element_orders: Mapping[int, int],
) -> list[UnitDraft]:
    """Materialize each native gap run at its proven canonical position.

    Placement is already decided by the stream; this function only shapes
    payloads. There is no review lane for placement: every entry carries a
    proven or attested position, and `detached_from_section=True` lets the
    structural grouper keep a run inline inside the surrounding proven
    section container without ever creating a business boundary.
    """

    drafts: list[UnitDraft] = []
    page_bases = {page.page_idx: page.order_basis for page in stream.pages}
    anchor_order = -1
    for entry in stream.entries:
        if entry.kind == "mineru_carrier":
            if entry.source_item_index is None:
                raise SourceEvidenceClosureError(
                    "canonical carrier entry lacks a source identity"
                )
            element_order = element_orders.get(entry.source_item_index)
            if element_order is None:
                raise SourceEvidenceClosureError(
                    f"canonical carrier {entry.source_item_index} has no "
                    "NormalizedIR element order"
                )
            anchor_order = element_order
            continue
        if entry.gap is None:
            continue
        gap = entry.gap
        parts = [_native_part(occurrence) for occurrence in gap.occurrences]
        evidence_artifacts = _unique_artifacts(
            occurrence.visual_artifact
            for occurrence in gap.occurrences
            if occurrence.visual_artifact is not None
        )
        graph = empty_projection_graph()
        graph["payload"] = {
            "kind": "container",
            "sources": [],
            "target_field": "payload.parts",
            "transform": "ordered_parts.v1",
        }
        try:
            graph["search_atoms"] = native_gap_search_atoms(gap)
            page_order_basis = page_bases.get(gap.page_idx)
            if page_order_basis is None:
                raise SourceEvidenceClosureError(
                    f"native gap page {gap.page_idx} has no stream order basis"
                )
            graph["physical_context"] = native_gap_physical_context(
                gap,
                order_basis=entry.order_basis,
                containment_owner=entry.containment_owner,
                page_order_basis=page_order_basis,
            )
        except SourceEvidenceProjectionError as exc:
            raise SourceEvidenceClosureError(str(exc)) from exc
        locator: dict[str, Any] = {"source_projection": graph}
        if evidence_artifacts:
            locator["evidence_artifacts"] = [
                artifact.as_dict() for artifact in evidence_artifacts
            ]
        quality_status = (
            "needs_review"
            if any(occurrence.needs_review for occurrence in gap.occurrences)
            else "ok"
        )
        drafts.append(
            UnitDraft(
                payload_kind="mixed",
                payload={"semantic_type": "document", "parts": parts},
                source_order=entry.stream_order,
                quality_status=quality_status,
                artifact_locator=locator,
                detached_from_section=True,
                native_order_anchor=(
                    anchor_order,
                    gap.page_idx,
                    gap.word_order_span[0],
                ),
            )
        )
    return drafts


def _native_part(occurrence: NativeEvidenceOccurrence) -> dict[str, Any]:
    if occurrence.text is not None:
        selector = source_selector(
            occurrence.source_ref,
            field="text",
            value_sha256=source_value_sha256(occurrence.text),
        )
        if selector is None:
            raise SourceEvidenceClosureError(
                f"native text source reference is invalid: {occurrence.occurrence_id}"
            )
        graph = empty_projection_graph()
        graph["payload"] = {
            "kind": "text_identity_exact",
            "sources": [selector],
            "target_field": "payload.text",
            "transform": "identity.v1",
        }
        graph["search_targets"] = ["payload.text"]
        locator: dict[str, Any] = {"source_projection": graph}
        if occurrence.visual_artifact is not None:
            locator["evidence_artifacts"] = [
                occurrence.visual_artifact.as_dict()
            ]
        part: dict[str, Any] = {
            "kind": "text",
            "order": occurrence.word_order,
            "text": occurrence.text,
            "artifact_locator": locator,
        }
        if occurrence.needs_review:
            part["quality_status"] = "needs_review"
        return part

    artifact = occurrence.visual_artifact
    if artifact is None:
        raise SourceEvidenceClosureError(
            f"native visual source lacks bytes: {occurrence.occurrence_id}"
        )
    digest = artifact.sha256.removeprefix("sha256:")
    image_ref = f"evidence/{digest}.png"
    selector = source_selector(
        occurrence.source_ref,
        field="image",
        value_sha256=source_value_sha256(image_ref),
    )
    if selector is None:
        raise SourceEvidenceClosureError(
            f"native visual source reference is invalid: {occurrence.occurrence_id}"
        )
    graph = empty_projection_graph()
    graph["payload"] = {
        "kind": "image_identity",
        "sources": [selector],
        "target_field": "payload.image_ref",
        "transform": "sha256_bytes.v1",
    }
    return {
        "kind": "image",
        "order": occurrence.word_order,
        "image_ref": image_ref,
        "caption": [],
        "content": [],
        "notes": [],
        "visual_kind": "image",
        "quality_status": "needs_review",
        "artifact_locator": {
            "source_projection": graph,
            "evidence_artifacts": [artifact.as_dict()],
        },
    }


def _unique_artifacts(
    artifacts: Any,
) -> list[VisualArtifactProof]:
    output: list[VisualArtifactProof] = []
    for artifact in artifacts:
        if artifact not in output:
            output.append(artifact)
    return output


def bind_visual_page_evidence(
    drafts: list[UnitDraft],
    proof: SourceEvidenceProof,
) -> list[UnitDraft]:
    """Bind validated page/crop images to the units that make them searchable."""

    visual_by_source: dict[
        int,
        list[tuple[str, dict[str, object]]],
    ] = {}
    for binding in proof.visual_bindings:
        visual_by_source.setdefault(binding.source_item_index, []).append(
            (binding.kind, binding.artifact.as_dict())
        )
    if not visual_by_source:
        return drafts
    bound: list[UnitDraft] = []
    for draft in drafts:
        locator = draft.artifact_locator
        if locator is None:
            bound.append(draft)
            continue
        payload = _bind_mixed_part_visuals(
            draft.payload,
            visual_by_source=visual_by_source,
        )
        source_indices = _unit_source_item_indices(draft)
        updated = _bind_locator_visuals(
            locator,
            source_indices=source_indices,
            visual_by_source=visual_by_source,
        )
        if updated == locator and payload is draft.payload:
            bound.append(draft)
            continue
        bound.append(
            replace(
                draft,
                payload=payload,
                artifact_locator=updated,
            )
        )
    return bound


def _bind_mixed_part_visuals(
    payload: dict[str, Any],
    *,
    visual_by_source: Mapping[
        int,
        list[tuple[str, dict[str, object]]],
    ],
) -> dict[str, Any]:
    parts = payload.get("parts")
    if not isinstance(parts, list):
        return payload
    updated_parts: list[Any] = []
    changed = False
    for raw_part in parts:
        if not isinstance(raw_part, Mapping):
            updated_parts.append(raw_part)
            continue
        part = dict(raw_part)
        locator = part.get("artifact_locator")
        if not isinstance(locator, Mapping):
            updated_parts.append(part)
            continue
        part_kind = str(part.get("kind") or "text")
        source_indices = _payload_source_item_indices(
            payload_kind="text" if part_kind == "image" else part_kind,
            payload=part,
            artifact_locator=locator,
        )
        updated_locator = _bind_locator_visuals(
            locator,
            source_indices=source_indices,
            visual_by_source=visual_by_source,
        )
        if updated_locator != locator:
            part["artifact_locator"] = updated_locator
            changed = True
        updated_parts.append(part)
    if not changed:
        return payload
    updated_payload = dict(payload)
    updated_payload["parts"] = updated_parts
    return updated_payload


def _bind_locator_visuals(
    locator: Mapping[str, Any],
    *,
    source_indices: set[int],
    visual_by_source: Mapping[
        int,
        list[tuple[str, dict[str, object]]],
    ],
) -> dict[str, Any]:
    additions = [
        (kind, descriptor)
        for source_index in sorted(source_indices)
        for kind, descriptor in visual_by_source.get(source_index, ())
    ]
    if not additions:
        return dict(locator)
    updated = dict(locator)
    existing = updated.get("evidence_artifacts")
    existing_artifacts = (
        [dict(item) for item in existing if isinstance(item, Mapping)]
        if isinstance(existing, list)
        else []
    )
    occurrence_artifacts = [
        item for kind, item in additions if kind == "occurrence_crop"
    ]
    guard_artifacts = [
        item for kind, item in additions if kind == "carrier_guard"
    ]
    merged: list[dict[str, Any]] = []
    known: set[tuple[object, object]] = set()
    for descriptor in [
        *occurrence_artifacts,
        *existing_artifacts,
        *guard_artifacts,
    ]:
        identity = (
            descriptor.get("artifact_role"),
            descriptor.get("sha256"),
        )
        if identity in known:
            continue
        merged.append(dict(descriptor))
        known.add(identity)
    updated["evidence_artifacts"] = merged
    return updated


def _unit_source_item_indices(draft: UnitDraft) -> set[int]:
    return _payload_source_item_indices(
        payload_kind=draft.payload_kind,
        payload=draft.payload,
        artifact_locator=draft.artifact_locator,
    )


def _payload_source_item_indices(
    *,
    payload_kind: str,
    payload: Mapping[str, Any],
    artifact_locator: Mapping[str, Any] | None,
) -> set[int]:
    return {
        int(ref["source_item_index"])
        for ref in payload_source_refs(
            payload_kind=payload_kind,
            payload=payload,
            artifact_locator=artifact_locator,
        )
        if isinstance(ref.get("source_item_index"), int)
        and not isinstance(ref.get("source_item_index"), bool)
    }
