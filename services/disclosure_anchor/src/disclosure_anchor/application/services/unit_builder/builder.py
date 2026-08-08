"""Pure S1-S7 document_unit builder stages."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import hashlib
import re
import unicodedata
from typing import Any, Callable, Iterable, cast

from disclosure_anchor.application.contracts import content_annotations
from disclosure_anchor.application.services.unit_builder import retrieval_routing
from disclosure_anchor.application.contracts.document_structure import (
    OWNER_SCOPE_V1_DOCUMENT_STRUCTURE_ALGORITHM,
    OWNER_SCOPE_V2_DOCUMENT_STRUCTURE_ALGORITHM,
    validate_document_structure,
)
from disclosure_anchor.application.contracts.source_evidence_occurrence import (
    punctuation_only_text,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    empty_projection_graph,
    payload_source_refs,
    public_artifact_locator,
    source_selector,
    source_value_sha256,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    require_current_normalized_ir,
)
from disclosure_anchor.application.contracts.publication_safety import (
    semantic_payload_without_unsafe_glyphs,
    unsafe_semantic_characters,
)
from disclosure_anchor.domain.value_objects.comparison_text import (
    source_carrier_search_surfaces,
    strict_source_comparison_text,
)


@dataclass(frozen=True)
class ResolvedImageArtifact:
    content: bytes
    artifact_role: str
    sha256: str
    size_bytes: int
    media_type: str


ImageArtifactResolver = Callable[[str, str], ResolvedImageArtifact]


class SourceEvidenceClosureError(ValueError):
    """A substantive parser carrier cannot be published without its evidence."""


@dataclass(frozen=True)
class PreparedElement:
    kind: str
    order_index: int
    source_order_phase: int = 0
    text: str | None = None
    raw_kind: str | None = None
    page_no: int | None = None
    table: dict[str, Any] | None = None
    table_caption: list[str] = field(default_factory=list)
    table_caption_source_indices: list[int] = field(default_factory=list)
    table_footnote: list[str] = field(default_factory=list)
    table_html: str | None = None
    payload: dict[str, Any] | None = None
    quality_status: str = "ok"
    artifact_locator: dict[str, Any] | None = None
    # Source ownership is explicit internal state.  Parser-labelled page
    # furniture is document-level evidence even when exact-dedup changes the
    # locator derivation; it must never inherit whichever business section
    # happened to be active at the page boundary.
    inherits_section: bool = True
    heading_path: list[str] = field(default_factory=list)
    # Internal identity of the concrete heading occurrences that own this
    # element. Textual paths are not identities: two sibling sections may have
    # the same title and must still remain separate.
    section_path: list[int] = field(default_factory=list)
    title: str | None = None


@dataclass(frozen=True)
class UnitDraft:
    payload_kind: str
    payload: dict[str, Any]
    source_order: int
    source_order_phase: int = 0
    heading_path: list[str] = field(default_factory=list)
    section_path: list[int] = field(default_factory=list)
    title: str | None = None
    semantic_key: str | None = None
    semantic_keys: list[str] | None = None
    quality_status: str = "ok"
    applicability: str | None = None
    artifact_locator: dict[str, Any] | None = None
    # Internal ownership flag.  Retained page furniture is published as
    # document-level evidence, but it is transparent to aggregation of the
    # concrete business-heading occurrence on either side.
    detached_from_section: bool = False
    # Native stream runs interleave between carriers by their proven anchor
    # (preceding carrier element order, page, word span) instead of carrying
    # a carrier order of their own.
    native_order_anchor: tuple[int, int, int] | None = None


@dataclass
class BuildStats:
    generated_by_kind: Counter[str] = field(default_factory=Counter)
    dropped_by_kind: Counter[str] = field(default_factory=Counter)
    heading_only_carriers_preserved: int = 0
    heading_outline_units_generated: int = 0
    deduplicated_page_number_lines: int = 0
    needs_review_count: int = 0
    unusable_count: int = 0
    provider_attested_pages: int = 0
    order_conflict_events: int = 0
    span_overlap_pages: int = 0
    punctuation_only_native_runs: int = 0
    native_recovery_gap_count: int = 0
    native_recovery_leaf_count: int = 0
    unsafe_semantic_payload_count: int = 0
    unsafe_heading_flattened_count: int = 0
    owner_scope_flattened_heading_count: int = 0
    unsafe_document_title_label_count: int = 0
    non_primary_source_alternative_count: int = 0
    page_furniture_support_count: int = 0
    # Per-source transform/exclusion ledger.  Counts explain volume; this
    # ledger makes every non-payload disposition independently auditable.
    source_dispositions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_by_kind": dict(self.generated_by_kind),
            "dropped_by_kind": dict(self.dropped_by_kind),
            "heading_only_carriers_preserved": (self.heading_only_carriers_preserved),
            "heading_outline_units_generated": (self.heading_outline_units_generated),
            "deduplicated_page_number_lines": self.deduplicated_page_number_lines,
            "needs_review_count": self.needs_review_count,
            "unusable_count": self.unusable_count,
            "provider_attested_pages": self.provider_attested_pages,
            "order_conflict_events": self.order_conflict_events,
            "span_overlap_pages": self.span_overlap_pages,
            "punctuation_only_native_runs": self.punctuation_only_native_runs,
            "native_recovery_gap_count": self.native_recovery_gap_count,
            "native_recovery_leaf_count": self.native_recovery_leaf_count,
            "unsafe_semantic_payload_count": self.unsafe_semantic_payload_count,
            "unsafe_heading_flattened_count": self.unsafe_heading_flattened_count,
            "owner_scope_flattened_heading_count": (
                self.owner_scope_flattened_heading_count
            ),
            "unsafe_document_title_label_count": (
                self.unsafe_document_title_label_count
            ),
            "non_primary_source_alternative_count": (
                self.non_primary_source_alternative_count
            ),
            "page_furniture_support_count": self.page_furniture_support_count,
            "source_dispositions": list(self.source_dispositions),
        }


@dataclass(frozen=True)
class Stage1Result:
    elements: list[PreparedElement]
    stats: BuildStats


def s1_preprocess_elements(
    elements: Iterable[dict[str, Any]],
    *,
    structure_proof: Mapping[str, Any],
    image_artifact_resolver: ImageArtifactResolver | None = None,
) -> Stage1Result:
    stats = BuildStats()
    prepared: list[PreparedElement] = []
    raw_elements = list(elements)
    raw_by_source_item_index = {
        source_item_index: element
        for element in raw_elements
        if isinstance((source_item_index := element.get("source_item_index")), int)
        and not isinstance(source_item_index, bool)
    }
    suppressed_frame_members = _frame_projection(
        structure_proof,
        raw_by_source_item_index=raw_by_source_item_index,
    )

    for element_index, element in enumerate(raw_elements):
        kind = str(element.get("kind", "unknown"))
        order_index = int(element.get("order_index", len(prepared)))
        raw_kind = str(element.get("raw_kind", kind))
        source_text = _element_text(element)
        page_no = _int_or_none(element.get("page_no"))
        source_item_index = _int_or_none(element.get("source_item_index"))
        if source_item_index is None:
            raise SourceEvidenceClosureError(
                f"NormalizedIR carrier {element_index} has no source_item_index"
            )
        if source_item_index in suppressed_frame_members:
            stats.dropped_by_kind["proven_page_frame_externalized"] += 1
            _record_source_disposition(
                stats,
                _artifact_locator(element),
                role="external_metadata",
                reason="proven_running_furniture",
            )
            continue
        inherits_section = True
        locator = _artifact_locator(element)
        if kind == "page_furniture":
            text = _clean_text(source_text)
            if not text:
                stats.dropped_by_kind[kind] += 1
                continue
            locator["derivation"] = {
                "kind": "page_furniture_retained",
                "reason": "running_role_unproved",
            }
            item = PreparedElement(
                kind="text",
                order_index=order_index,
                raw_kind=raw_kind,
                page_no=page_no,
                text=text,
                quality_status="needs_review",
                artifact_locator=locator,
                inherits_section=False,
            )
            prepared.append(item)
            continue
        if kind == "text" and raw_kind == "list":
            raw_list_items = element.get("list_items")
            if not isinstance(raw_list_items, list) or not all(
                isinstance(value, str) for value in raw_list_items
            ):
                raise SourceEvidenceClosureError(
                    "MinerU list carrier has invalid list_items "
                    f"(order_index={order_index}, page_no={page_no})"
                )
            if not any(value.strip() for value in raw_list_items):
                stats.dropped_by_kind["list_proven_empty"] += 1
                continue
            if not source_text:
                raise SourceEvidenceClosureError(
                    "MinerU list carrier has no mapped text projection "
                    f"(order_index={order_index}, page_no={page_no})"
                )
            locator["derivation"] = {
                "kind": "typed_list_carrier",
                "reason": "mineru_ordered_list_items",
            }
            list_text_source = source_selector(locator, field="text")
            if list_text_source is None:
                raise SourceEvidenceClosureError(
                    "MinerU list carrier lacks a strict source identity "
                    f"(order_index={order_index}, page_no={page_no})"
                )
            locator = (
                _with_payload_projection(
                    locator,
                    {
                        "kind": "text_identity_exact",
                        "sources": [list_text_source],
                        "target_field": "payload.text",
                        "transform": "identity.v1",
                    },
                )
                or locator
            )
            locator = _with_exact_structured_projection(
                locator,
                source_field="list_items",
                target_field="payload.list_items",
            )
            typed_list_payload: dict[str, Any] = {
                "text": source_text,
                "list_items": list(raw_list_items),
            }
            list_subtype = element.get("list_subtype")
            if isinstance(list_subtype, str) and list_subtype:
                typed_list_payload["list_subtype"] = list_subtype
                locator = _with_exact_structured_projection(
                    locator,
                    source_field="list_subtype",
                    target_field="payload.list_subtype",
                )
            item = PreparedElement(
                kind="text",
                order_index=order_index,
                raw_kind=raw_kind,
                page_no=page_no,
                payload=typed_list_payload,
                artifact_locator=locator,
                inherits_section=inherits_section,
            )
            prepared.append(item)
            continue
        if kind == "text" and raw_kind == "code":
            if not source_text.strip():
                raise SourceEvidenceClosureError(
                    "MinerU code carrier has no mapped text evidence "
                    f"(order_index={order_index}, page_no={page_no})"
                )
            code_source = source_selector(locator, field="text")
            if code_source is None:
                raise SourceEvidenceClosureError(
                    "MinerU code carrier lacks a strict source identity "
                    f"(order_index={order_index}, page_no={page_no})"
                )
            locator = (
                _with_payload_projection(
                    locator,
                    {
                        "kind": "text_identity_exact",
                        "sources": [code_source],
                        "target_field": "payload.text",
                        "transform": "identity.v1",
                    },
                )
                or locator
            )
            code_body = element.get("code_body")
            code_caption = element.get("code_caption")
            code_footnote = element.get("code_footnote")
            typed_fields_valid = (
                isinstance(code_body, str)
                and bool(code_body.strip())
                and isinstance(code_caption, list)
                and all(isinstance(value, str) for value in code_caption)
                and isinstance(code_footnote, list)
                and all(isinstance(value, str) for value in code_footnote)
            )
            if not typed_fields_valid:
                raise SourceEvidenceClosureError(
                    "MinerU code carrier has invalid typed fields "
                    f"(order_index={order_index}, page_no={page_no})"
                )
            locator["derivation"] = {
                "kind": "typed_code_carrier",
                "reason": "mineru_code_body_with_associated_fields",
            }
            code_payload: dict[str, Any] = {"text": source_text}
            for source_field, source_value in (
                ("code_body", code_body),
                ("code_caption", code_caption),
                ("code_footnote", code_footnote),
            ):
                code_payload[source_field] = (
                    list(source_value)
                    if isinstance(source_value, list)
                    else source_value
                )
                locator = _with_exact_structured_projection(
                    locator,
                    source_field=source_field,
                    target_field=f"payload.{source_field}",
                )
            if "code_subtype" in element:
                code_subtype = element.get("code_subtype")
                if not isinstance(code_subtype, str):
                    raise SourceEvidenceClosureError(
                        "MinerU code carrier has invalid code_subtype "
                        f"(order_index={order_index}, page_no={page_no})"
                    )
                code_payload["code_subtype"] = code_subtype
                locator = _with_exact_structured_projection(
                    locator,
                    source_field="code_subtype",
                    target_field="payload.code_subtype",
                )
            item = PreparedElement(
                kind="text",
                order_index=order_index,
                raw_kind=raw_kind,
                page_no=page_no,
                payload=code_payload,
                artifact_locator=locator,
                inherits_section=inherits_section,
            )
            prepared.append(item)
            continue
        image_path = str(element.get("image_path") or "").strip()
        if kind in {"image", "equation"} and image_path:
            is_equation = kind == "equation"
            # MinerU equations never carry image_caption/image_footnote; only
            # image/chart elements do.  The equation caption is recovered from
            # its formula content below, so those reads are skipped here.
            caption_values = (
                [] if is_equation else _source_text_values(element.get("image_caption"))
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
            image_ref, evidence_artifact = _bound_image_artifact(
                f"evidence_image_{source_item_index:06d}",
                image_path,
                image_artifact_resolver=image_artifact_resolver,
            )
            locator["evidence_artifacts"] = [evidence_artifact]
            image_source = _required_source_selector(locator, field="image")
            locator = (
                _with_payload_projection(
                    locator,
                    {
                        "kind": "image_identity",
                        "sources": [image_source],
                        "target_field": "payload.image_ref",
                        "transform": "sha256_bytes.v1",
                    },
                )
                or locator
            )
            if caption_values:
                caption_source = _required_source_selector(
                    locator,
                    field="image_caption",
                )
                locator = (
                    _with_structured_projection(
                        locator,
                        {
                            "kind": "derived_field",
                            "source": caption_source,
                            "target_field": "payload.caption",
                            "transform": "ordered_nonempty_lines.v1",
                        },
                    )
                    or locator
                )
            if visual_subtype:
                subtype_source = _required_source_selector(
                    locator, field="visual_subtype"
                )
                locator = (
                    _with_structured_projection(
                        locator,
                        {
                            "kind": "derived_field",
                            "source": subtype_source,
                            "target_field": "payload.visual_subtype",
                            "transform": "identity.v1",
                        },
                    )
                    or locator
                )
            if content:
                content_source = _required_source_selector(locator, field="text")
                content_target = (
                    "payload.caption" if content == caption else "payload.content"
                )
                locator = (
                    _with_structured_projection(
                        locator,
                        {
                            "kind": "derived_field",
                            "source": content_source,
                            "target_field": content_target,
                            "transform": "trim.v1",
                        },
                    )
                    or locator
                )
            semantic_text = element.get("visual_semantic_text")
            if isinstance(semantic_text, str) and semantic_text:
                semantic_source = _required_source_selector(
                    locator,
                    field="visual_semantic_text",
                )
                locator = (
                    _with_structured_projection(
                        locator,
                        {
                            "kind": "derived_field",
                            "source": semantic_source,
                            "target_field": "payload.semantic_text",
                            "transform": "identity.v1",
                        },
                    )
                    or locator
                )
            emitted_note_index = 0
            for note_index, raw_note in enumerate(
                [] if is_equation else (element.get("image_footnote") or [])
            ):
                cleaned_note = _source_text(str(raw_note))
                if not cleaned_note:
                    continue
                note_source = _required_source_selector(
                    locator,
                    field="image_footnote",
                    index=note_index,
                )
                locator = (
                    _with_structured_projection(
                        locator,
                        {
                            "kind": "derived_field",
                            "source": note_source,
                            "target_field": (f"payload.notes.{emitted_note_index}"),
                            "transform": "trim.v1",
                        },
                    )
                    or locator
                )
                emitted_note_index += 1
            payload: dict[str, Any] = {
                "image_ref": image_ref,
                "caption": caption,
                "visual_kind": visual_kind,
            }
            if visual_subtype:
                payload["visual_subtype"] = visual_subtype
            if content and content != caption:
                payload["content"] = content
            if footnote_values:
                payload["notes"] = footnote_values
            if isinstance(semantic_text, str) and semantic_text:
                payload["semantic_text"] = semantic_text
            text_format = element.get("text_format")
            if is_equation and isinstance(text_format, str) and text_format:
                payload["text_format"] = text_format
                locator = _with_exact_structured_projection(
                    locator,
                    source_field="text_format",
                    target_field="payload.text_format",
                )
            visual_search_targets: list[str] = []
            if caption:
                visual_search_targets.append("payload.caption")
            if content and content != caption:
                visual_search_targets.append("payload.content")
            if isinstance(semantic_text, str) and semantic_text:
                visual_search_targets.append("payload.semantic_text")
            visual_search_targets.extend(
                f"payload.notes.{note_index}"
                for note_index in range(len(footnote_values))
            )
            locator = _with_search_targets(locator, visual_search_targets) or locator
            item = PreparedElement(
                kind="text",
                order_index=order_index,
                raw_kind=raw_kind,
                page_no=page_no,
                payload=payload,
                quality_status="needs_review",
                artifact_locator=locator,
                inherits_section=inherits_section,
            )
            prepared.append(item)
            continue
        if kind == "equation":
            if not source_text:
                raise SourceEvidenceClosureError(
                    "equation has no mapped image or text evidence "
                    f"(order_index={order_index}, page_no={page_no})"
                )
            equation_source = source_selector(locator, field="text")
            if equation_source is None:
                raise SourceEvidenceClosureError(
                    "equation lacks a strict source identity "
                    f"(order_index={order_index}, page_no={page_no})"
                )
            locator = (
                _with_payload_projection(
                    locator,
                    {
                        "kind": "text_identity_exact",
                        "sources": [equation_source],
                        "target_field": "payload.text",
                        "transform": "identity.v1",
                    },
                )
                or locator
            )
            equation_payload: dict[str, Any] = {"text": source_text}
            text_format = element.get("text_format")
            if isinstance(text_format, str) and text_format:
                equation_payload["text_format"] = text_format
                locator = _with_exact_structured_projection(
                    locator,
                    source_field="text_format",
                    target_field="payload.text_format",
                )
            item = PreparedElement(
                kind="text",
                order_index=order_index,
                raw_kind=raw_kind,
                page_no=page_no,
                payload=equation_payload,
                artifact_locator=locator,
                inherits_section=inherits_section,
            )
            prepared.append(item)
            continue
        if kind == "text":
            text = _clean_text(source_text)
            if not text:
                stats.dropped_by_kind[kind] += 1
                continue
            item = PreparedElement(
                kind="text",
                order_index=order_index,
                raw_kind=raw_kind,
                page_no=page_no,
                text=text,
                artifact_locator=locator,
                inherits_section=inherits_section,
            )
            prepared.append(item)
            continue
        if kind == "image":
            raise SourceEvidenceClosureError(
                "image carrier has no image artifact "
                f"(order_index={order_index}, page_no={page_no})"
            )
        if kind == "table":
            # Captions are source evidence even when MinerU attached a
            # checkbox declaration. Title selection below excludes those
            # markers without deleting them from the payload.
            captions = [str(caption) for caption in element.get("table_caption") or []]
            table = dict(element.get("table") or {"headers": [], "rows": []})
            evidence_artifacts: list[dict[str, Any]] = []
            table_image_path = element.get("image_path")
            if isinstance(table_image_path, str) and table_image_path:
                _, outer_artifact = _bound_image_artifact(
                    f"evidence_image_{source_item_index:06d}",
                    table_image_path,
                    image_artifact_resolver=image_artifact_resolver,
                )
                evidence_artifacts.append(outer_artifact)
            raw_embedded_media = table.get("embedded_media")
            if isinstance(raw_embedded_media, list):
                public_media: list[dict[str, Any]] = []
                for raw_media in raw_embedded_media:
                    if not isinstance(raw_media, Mapping):
                        raise SourceEvidenceClosureError(
                            "table embedded media is not an object "
                            f"(order_index={order_index})"
                        )
                    role = raw_media.get("artifact_role")
                    media_path = raw_media.get("image_path")
                    if (
                        not isinstance(role, str)
                        or not role
                        or not isinstance(media_path, str)
                        or not media_path
                    ):
                        raise SourceEvidenceClosureError(
                            "table embedded media lacks artifact identity "
                            f"(order_index={order_index})"
                        )
                    image_ref, artifact = _bound_image_artifact(
                        role,
                        media_path,
                        image_artifact_resolver=image_artifact_resolver,
                    )
                    evidence_artifacts.append(artifact)
                    public_media.append(
                        {
                            key: value
                            for key, value in raw_media.items()
                            if key not in {"artifact_role", "image_path"}
                        }
                        | {"image_ref": image_ref}
                    )
                table["embedded_media"] = public_media
            if evidence_artifacts:
                locator["evidence_artifacts"] = evidence_artifacts
            item = PreparedElement(
                kind="table",
                order_index=order_index,
                raw_kind=raw_kind,
                page_no=page_no,
                table=table,
                table_caption=captions,
                table_caption_source_indices=list(range(len(captions))),
                table_footnote=[
                    str(item) for item in element.get("table_footnote") or []
                ],
                table_html=element.get("table_html"),
                artifact_locator=locator,
                inherits_section=inherits_section,
            )
            prepared.append(item)
            continue
        raise SourceEvidenceClosureError(
            "unsupported NormalizedIR carrier kind "
            f"{kind!r} (order_index={order_index}, raw_kind={raw_kind!r})"
        )

    return Stage1Result(elements=prepared, stats=stats)


def _frame_projection(
    proof: Mapping[str, Any],
    *,
    raw_by_source_item_index: Mapping[int, dict[str, Any]],
) -> frozenset[int]:
    """Project parser-proven page frames without re-inferring their role."""

    suppressed: set[int] = set()
    for frame in proof["page_frames"]:
        members = [int(value) for value in frame["member_source_item_indices"]]
        if any(member not in raw_by_source_item_index for member in members):
            raise SourceEvidenceClosureError(
                "running furniture proof references an absent source carrier"
            )
        suppressed.update(members)
    return frozenset(suppressed)


def _projection_graph(locator: dict[str, Any]) -> dict[str, Any]:
    raw = locator.get("source_projection")
    graph = empty_projection_graph()
    if not isinstance(raw, dict):
        return graph
    graph["payload"] = raw.get("payload")
    for graph_field in (
        "heading_path",
        "structured",
        "provenance",
        "search_targets",
    ):
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
    if payload_projection.get("target_field") == "payload.text":
        graph["search_targets"] = ["payload.text"]
    output["source_projection"] = graph
    return output or None


def _with_search_targets(
    locator: dict[str, Any] | None,
    targets: Iterable[str],
) -> dict[str, Any] | None:
    output = dict(locator or {})
    graph = _projection_graph(output)
    graph["search_targets"] = list(dict.fromkeys(targets))
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


def _with_exact_structured_projection(
    locator: dict[str, Any] | None,
    *,
    source_field: str,
    target_field: str,
) -> dict[str, Any]:
    output = dict(locator or {})
    selector = source_selector(output, field=source_field)
    if selector is None:
        raise SourceEvidenceClosureError(
            f"source field {source_field!r} lacks a strict source identity"
        )
    return (
        _with_structured_projection(
            output,
            {
                "kind": "derived_field",
                "source": selector,
                "target_field": target_field,
                "transform": "identity_json.v1",
            },
        )
        or output
    )


def _text_source_selectors(
    locator: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not locator:
        raise SourceEvidenceClosureError("text payload has no source locator")
    return [_required_source_selector(locator, field="text")]


def _required_source_selector(
    locator: dict[str, Any],
    *,
    field: str,
    index: int | None = None,
) -> dict[str, Any]:
    selector = source_selector(locator, field=field, index=index)
    if selector is None:
        raise SourceEvidenceClosureError(
            f"source field {field!r} lacks a strict source identity"
        )
    return selector


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
            "text_identity_exact",
            "text_concat",
        }
        and existing.get("target_field") == target_field
    ):
        # Typed ownership is authoritative over recursive navigation hints.
        # Several builder stages decorate the same locator; re-inferring an
        # already-bound target would turn duplicate lineage back into payload.
        graph["search_targets"] = [target_field]
        output["source_projection"] = graph
        return output or None
    selectors = _text_source_selectors(locator)
    projection_kind = "text_concat" if len(selectors) > 1 else "text_identity"
    graph["payload"] = {
        "kind": projection_kind,
        "sources": selectors,
        "target_field": target_field,
        "transform": (
            "ordered_text_concat.v1"
            if projection_kind == "text_concat"
            else "clean_text.v1"
        ),
    }
    graph["search_targets"] = [target_field]
    output["source_projection"] = graph
    return output or None


def _with_table_payload_projection(
    locator: dict[str, Any] | None,
    *,
    captions: list[str],
    caption_source_indices: list[int],
    notes: list[str],
    embedded_media: list[dict[str, Any]],
) -> dict[str, Any] | None:
    output = dict(locator or {})
    selector = _required_source_selector(output, field="table")
    graph = _projection_graph(output)
    projection_sources = [selector]
    projection_sources.extend(
        _required_source_selector(
            output,
            field="table_caption",
            index=source_index,
        )
        for source_index in caption_source_indices
    )
    projection_sources.extend(
        _required_source_selector(output, field="table_note", index=index)
        for index in range(len(notes))
    )
    graph["payload"] = {
        "kind": "table_identity",
        "sources": projection_sources,
        "target_field": "payload",
        "transform": "table_identity.v1",
    }
    graph["search_targets"] = [
        "payload.caption",
        "payload.headers",
        "payload.rows",
        "payload.notes",
        *[
            f"payload.embedded_media.{index}.semantic_text"
            for index, media in enumerate(embedded_media)
            if isinstance(media.get("semantic_text"), str)
            and media["semantic_text"]
        ],
    ]
    output["source_projection"] = graph
    return output or None


@dataclass(frozen=True)
class _ProvenHeading:
    node_id: int
    parent_node_id: int | None
    propagates: bool
    section_start: int
    section_end: int
    title: str
    refs: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class _OwnerScopeBreak:
    boundary_source_item_index: int
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


def s2_apply_structure_proof(
    elements: Iterable[PreparedElement],
    *,
    raw_elements: Iterable[Mapping[str, Any]],
    structure_proof: Mapping[str, Any],
    stats: BuildStats | None = None,
) -> list[PreparedElement]:
    """Project one validated parser proof; never infer a second hierarchy."""

    prepared = list(elements)
    raw_by_index = {
        int(element["source_item_index"]): element for element in raw_elements
    }
    prepared_by_index = {
        source_index: element
        for element in prepared
        if (source_index := _prepared_source_item_index(element)) is not None
    }
    heading_anchors = _publication_safe_heading_anchors(
        _proven_headings(
            structure_proof,
            raw_by_index=raw_by_index,
        ),
        stats=stats,
    )
    owner_scope_breaks = _proven_owner_scope_breaks(structure_proof)
    flattened_ids = _owner_scope_flattened_node_ids(
        owner_scope_breaks,
        anchors=heading_anchors,
    )
    if stats is not None:
        stats.owner_scope_flattened_heading_count += sum(
            heading.propagates and heading.node_id in flattened_ids
            for heading in heading_anchors
        )
    headings = [
        heading
        for heading in heading_anchors
        if heading.propagates and heading.node_id not in flattened_ids
    ]
    by_id = {heading.node_id: heading for heading in headings}
    paths = {
        heading.node_id: _proven_heading_path(heading, by_id=by_id)
        for heading in headings
    }
    heading_text_sources = {
        int(ref["source_item_index"])
        for heading in headings
        for ref in heading.refs
        if _heading_ref_field(ref) == "text"
        and _ref_is_full_text(ref, raw_by_index=raw_by_index)
    }
    represented: set[int] = set()
    placed: list[PreparedElement] = []
    for element in prepared:
        source_index = _prepared_source_item_index(element)
        if source_index is None:
            raise SourceEvidenceClosureError(
                f"prepared carrier {element.order_index} has no source identity"
            )
        if not element.inherits_section:
            placed.append(
                replace(
                    element,
                    heading_path=[],
                    section_path=[],
                    title=None,
                )
            )
            continue
        owner = _proven_heading_owner(source_index, headings=headings, paths=paths)
        selected_only = _selected_only_boundary_break(
            source_index,
            owner=owner,
            scope_breaks=owner_scope_breaks,
        )
        if selected_only is not None:
            if element.kind != "table":
                raise SourceEvidenceClosureError(
                    "selected-only owner break does not select a table caption"
                )
            body, detached_caption = _split_owner_boundary_caption(
                element,
                scope_break=selected_only,
            )
            placed_body = _place_prepared_element(
                body,
                owner=owner,
                paths=paths,
                heading_text_sources=heading_text_sources,
                represented=represented,
                prepared_by_index=prepared_by_index,
            )
            if placed_body is not None:
                placed.append(placed_body)
            target = (
                by_id[selected_only.target_node_id]
                if selected_only.target_node_id is not None
                else None
            )
            placed_caption = _place_prepared_element(
                detached_caption,
                owner=target,
                paths=paths,
                heading_text_sources=heading_text_sources,
                represented=represented,
                prepared_by_index=prepared_by_index,
            )
            assert placed_caption is not None
            placed.append(placed_caption)
            continue
        owner = _owner_after_scope_break(
            source_index,
            owner=owner,
            by_id=by_id,
            scope_breaks=owner_scope_breaks,
        )
        placed_element = _place_prepared_element(
            element,
            owner=owner,
            paths=paths,
            heading_text_sources=heading_text_sources,
            represented=represented,
            prepared_by_index=prepared_by_index,
        )
        if placed_element is not None:
            placed.append(placed_element)

    children = {
        heading.parent_node_id
        for heading in headings
        if heading.parent_node_id is not None
    }
    leaves = [
        heading
        for heading in headings
        if heading.node_id not in represented and heading.node_id not in children
    ]
    for heading in leaves:
        path = paths[heading.node_id]
        source = prepared_by_index[int(heading.refs[0]["source_item_index"])]
        locator = _heading_only_locator(
            heading,
            path=path,
            prepared_by_index=prepared_by_index,
        )
        placed.append(
            PreparedElement(
                kind="text",
                raw_kind=source.raw_kind,
                order_index=source.order_index,
                page_no=source.page_no,
                text=heading.title,
                quality_status=source.quality_status,
                artifact_locator=locator,
                heading_path=[item.title for item in path],
                section_path=[item.node_id for item in path],
                title=heading.title,
            )
        )
    if stats is not None:
        stats.heading_only_carriers_preserved += len(leaves)
    return sorted(placed, key=lambda item: (item.order_index, item.source_order_phase))


def _publication_safe_heading_anchors(
    headings: Sequence[_ProvenHeading],
    *,
    stats: BuildStats | None,
) -> list[_ProvenHeading]:
    """Flatten an unsafe heading subtree before its carrier can be suppressed.

    A title containing an undecoded glyph cannot be a semantic label.  Its
    physical carrier must therefore remain ordinary content, and descendants
    cannot skip over that unpublishable parent to create a finer owner.  The
    nearest safe ancestor (or the document root) consequently owns the whole
    subtree without any guessed glyph replacement.
    """

    blocked = {
        heading.node_id
        for heading in headings
        if unsafe_semantic_characters(heading.title)
    }
    changed = True
    while changed:
        changed = False
        for heading in headings:
            if (
                heading.node_id not in blocked
                and heading.parent_node_id in blocked
            ):
                blocked.add(heading.node_id)
                changed = True
    if stats is not None:
        stats.unsafe_heading_flattened_count += sum(
            heading.propagates and heading.node_id in blocked
            for heading in headings
        )
    return [heading for heading in headings if heading.node_id not in blocked]


def _proven_headings(
    proof: Mapping[str, Any],
    *,
    raw_by_index: Mapping[int, Mapping[str, Any]],
) -> list[_ProvenHeading]:
    output: list[_ProvenHeading] = []
    for value in proof["headings"]:
        refs = tuple(value["source_refs"])
        title = _clean_text(
            "".join(_heading_ref_value(ref, raw_by_index=raw_by_index) for ref in refs)
        )
        if not title:
            raise SourceEvidenceClosureError(
                f"proved heading {value['node_id']} has no visible title"
            )
        output.append(
            _ProvenHeading(
                node_id=int(value["node_id"]),
                parent_node_id=(
                    int(value["parent_node_id"])
                    if value["parent_node_id"] is not None
                    else None
                ),
                propagates=bool(value["propagates"]),
                section_start=int(value["section_span"][0]),
                section_end=int(value["section_span"][1]),
                title=title,
                refs=refs,
            )
        )
    return output


def _proven_heading_path(
    heading: _ProvenHeading,
    *,
    by_id: Mapping[int, _ProvenHeading],
) -> tuple[_ProvenHeading, ...]:
    path = [heading]
    parent_id = heading.parent_node_id
    while parent_id is not None:
        parent = by_id[parent_id]
        path.append(parent)
        parent_id = parent.parent_node_id
    return tuple(reversed(path))


def _proven_heading_owner(
    source_index: int,
    *,
    headings: Iterable[_ProvenHeading],
    paths: Mapping[int, tuple[_ProvenHeading, ...]],
) -> _ProvenHeading | None:
    candidates = [
        heading
        for heading in headings
        if heading.section_start <= source_index <= heading.section_end
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda heading: len(paths[heading.node_id]))
    owner = candidates[-1]
    ancestor_ids = {item.node_id for item in paths[owner.node_id]}
    if any(candidate.node_id not in ancestor_ids for candidate in candidates):
        raise SourceEvidenceClosureError(
            f"structure proof has overlapping owners at source {source_index}"
        )
    return owner


def _proven_owner_scope_breaks(
    structure_proof: Mapping[str, Any],
) -> tuple[_OwnerScopeBreak, ...]:
    values = structure_proof.get("owner_scope_breaks", [])
    if (
        structure_proof.get("algorithm_version")
        == OWNER_SCOPE_V1_DOCUMENT_STRUCTURE_ALGORITHM
        and values
    ):
        raise SourceEvidenceClosureError(
            "legacy owner-scope breaks cannot drive current publication"
        )
    if (
        structure_proof.get("algorithm_version")
        == OWNER_SCOPE_V2_DOCUMENT_STRUCTURE_ALGORITHM
        and values
    ):
        # v13 breaks predate materialization policies; guessing a default
        # would silently grant them v14 placement semantics.
        raise SourceEvidenceClosureError(
            "v13 owner-scope breaks require a reparse under the v14 "
            "materialization contract before publication"
        )
    output: list[_OwnerScopeBreak] = []
    for value in values:
        ref = value["boundary_source_ref"]
        output.append(
            _OwnerScopeBreak(
                boundary_source_item_index=int(ref["source_item_index"]),
                boundary_field=str(ref["field"]),
                boundary_index=(
                    int(ref["index"]) if ref.get("index") is not None else None
                ),
                boundary_text_span=(
                    int(ref["text_span"][0]),
                    int(ref["text_span"][1]),
                ),
                boundary_value_sha256=str(ref["value_sha256"]),
                page_index=int(ref["page_index"]),
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
        )
    return tuple(output)


def _owner_scope_flattened_node_ids(
    scope_breaks: Sequence[_OwnerScopeBreak],
    *,
    anchors: Sequence[_ProvenHeading],
) -> frozenset[int]:
    """Collect the accepted subtrees a flatten policy folds into its target.

    The flattened nodes stay proven headings in the proof; they only lose
    section materialization so their carriers become ordinary ordered leaves
    of the target and the target keeps exactly one physical occurrence.
    """

    flattened: set[int] = set()
    by_id = {anchor.node_id: anchor for anchor in anchors}
    children: dict[int, list[int]] = {}
    for anchor in anchors:
        if anchor.parent_node_id is not None:
            children.setdefault(anchor.parent_node_id, []).append(anchor.node_id)
    for scope_break in scope_breaks:
        if scope_break.materialization_policy == "direct_target":
            if scope_break.flatten_subtree_root_node_id is not None:
                raise SourceEvidenceClosureError(
                    "direct-target owner break carries a flatten subtree root"
                )
            continue
        if scope_break.materialization_policy != "flatten_intervening_subtree":
            raise SourceEvidenceClosureError(
                "owner scope break materialization policy is unknown"
            )
        root = scope_break.flatten_subtree_root_node_id
        target = scope_break.target_node_id
        if (
            root is None
            or target is None
            or root not in by_id
            or not by_id[root].propagates
            or by_id[root].parent_node_id != target
        ):
            raise SourceEvidenceClosureError(
                "owner scope flatten root is not an accepted child of its target"
            )
        subtree = {root}
        stack = [root]
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in subtree:
                    subtree.add(child)
                    stack.append(child)
        if scope_break.current_owner_node_id not in subtree:
            raise SourceEvidenceClosureError(
                "owner scope flatten subtree does not contain the boundary owner"
            )
        flattened.update(subtree)
    return frozenset(flattened)


def _selected_only_boundary_break(
    source_index: int,
    *,
    owner: _ProvenHeading | None,
    scope_breaks: Sequence[_OwnerScopeBreak],
) -> _OwnerScopeBreak | None:
    if owner is None:
        return None
    matches = [
        scope_break
        for scope_break in scope_breaks
        if scope_break.boundary_source_item_index == source_index
        and scope_break.current_owner_node_id == owner.node_id
        and scope_break.boundary_carrier_scope == "selected_only"
    ]
    if len(matches) > 1:
        raise SourceEvidenceClosureError("multiple selected-only boundary breaks")
    return matches[0] if matches else None


def _owner_after_scope_break(
    source_index: int,
    *,
    owner: _ProvenHeading | None,
    by_id: Mapping[int, _ProvenHeading],
    scope_breaks: Sequence[_OwnerScopeBreak],
) -> _ProvenHeading | None:
    """Apply the latest exact break only within its original owner interval."""

    if owner is None:
        return None
    applicable = [
        scope_break
        for scope_break in scope_breaks
        if scope_break.current_owner_node_id == owner.node_id
        and owner.section_start
        < scope_break.boundary_source_item_index
        <= source_index
    ]
    if not applicable:
        return owner
    latest = max(
        applicable,
        key=lambda scope_break: scope_break.boundary_source_item_index,
    )
    return by_id[latest.target_node_id] if latest.target_node_id is not None else None


def _place_prepared_element(
    element: PreparedElement,
    *,
    owner: _ProvenHeading | None,
    paths: Mapping[int, tuple[_ProvenHeading, ...]],
    heading_text_sources: set[int],
    represented: set[int],
    prepared_by_index: Mapping[int, PreparedElement],
) -> PreparedElement | None:
    source_index = _prepared_source_item_index(element)
    assert source_index is not None
    path = paths[owner.node_id] if owner is not None else ()
    if (
        source_index in heading_text_sources
        and element.kind == "text"
        and element.payload is None
    ):
        return None
    if not _is_empty_table_element(element):
        represented.update(item.node_id for item in path)
    titles = [item.title for item in path]
    return replace(
        element,
        heading_path=titles,
        section_path=[item.node_id for item in path],
        title=titles[-1] if titles else None,
        artifact_locator=_locator_with_proven_heading_sources(
            element.artifact_locator,
            path,
            prepared_by_index=prepared_by_index,
        ),
    )


def _split_owner_boundary_caption(
    element: PreparedElement,
    *,
    scope_break: _OwnerScopeBreak,
) -> tuple[PreparedElement, PreparedElement]:
    if (
        scope_break.boundary_field != "table_caption"
        or scope_break.boundary_index is None
    ):
        raise SourceEvidenceClosureError(
            "selected-only owner break lacks an indexed table caption"
        )
    source_indices = _table_caption_source_indices(element)
    positions = [
        position
        for position, source_index in enumerate(source_indices)
        if source_index == scope_break.boundary_index
    ]
    if len(positions) != 1:
        raise SourceEvidenceClosureError(
            "selected-only owner break caption is absent or duplicated"
        )
    position = positions[0]
    caption = element.table_caption[position]
    if source_value_sha256(caption) != scope_break.boundary_value_sha256:
        raise SourceEvidenceClosureError(
            "selected-only owner break caption differs from its proof"
        )
    locator = {
        key: value
        for key, value in dict(element.artifact_locator or {}).items()
        if key != "evidence_artifacts"
    }
    selector = source_selector(
        locator,
        field="table_caption",
        index=scope_break.boundary_index,
        char_span=list(scope_break.boundary_text_span),
        value_sha256=scope_break.boundary_value_sha256,
    )
    if selector is None:
        raise SourceEvidenceClosureError(
            "selected-only owner break caption has no strict source selector"
        )
    caption_locator = _with_payload_projection(
        locator,
        {
            "kind": "text_identity_exact",
            "sources": [selector],
            "target_field": "payload.caption",
            "transform": "identity.v1",
        },
    )
    body = replace(
        element,
        table_caption=[
            value
            for index, value in enumerate(element.table_caption)
            if index != position
        ],
        table_caption_source_indices=[
            value
            for index, value in enumerate(source_indices)
            if index != position
        ],
    )
    detached = PreparedElement(
        kind="text",
        raw_kind="table_caption",
        order_index=element.order_index,
        source_order_phase=1,
        page_no=element.page_no,
        payload={"caption": caption},
        quality_status="needs_review",
        artifact_locator=caption_locator,
    )
    return body, detached


def _table_caption_source_indices(element: PreparedElement) -> list[int]:
    if not element.table_caption_source_indices:
        return list(range(len(element.table_caption)))
    if len(element.table_caption_source_indices) != len(element.table_caption):
        raise SourceEvidenceClosureError("table caption source indices are invalid")
    return list(element.table_caption_source_indices)


def _prepared_source_item_index(element: PreparedElement) -> int | None:
    return _int_or_none((element.artifact_locator or {}).get("source_item_index"))


def _heading_ref_field(ref: Mapping[str, Any]) -> str:
    field = ref.get("field", "text")
    if not isinstance(field, str) or not field:
        raise SourceEvidenceClosureError("heading source field is invalid")
    return field


def _heading_ref_value(
    ref: Mapping[str, Any],
    *,
    raw_by_index: Mapping[int, Mapping[str, Any]],
) -> str:
    source = raw_by_index[int(ref["source_item_index"])]
    field = _heading_ref_field(ref)
    value: object = source.get(field)
    index = ref.get("index")
    if index is not None:
        if not isinstance(value, list):
            raise SourceEvidenceClosureError("indexed heading source is not a list")
        value = value[int(index)]
    if not isinstance(value, str):
        raise SourceEvidenceClosureError("heading source is not text")
    start, end = (int(part) for part in ref["text_span"])
    return value[start:end]


def _ref_is_full_text(
    ref: Mapping[str, Any],
    *,
    raw_by_index: Mapping[int, Mapping[str, Any]],
) -> bool:
    if _heading_ref_field(ref) != "text" or ref.get("index") is not None:
        return False
    value = raw_by_index[int(ref["source_item_index"])].get("text")
    return isinstance(value, str) and list(ref["text_span"]) == [0, len(value)]


def _heading_ref_selector(
    ref: Mapping[str, Any],
    *,
    prepared_by_index: Mapping[int, PreparedElement],
) -> dict[str, Any]:
    source_index = int(ref["source_item_index"])
    locator = prepared_by_index[source_index].artifact_locator or {}
    selector = source_selector(
        locator,
        field=_heading_ref_field(ref),
        index=_int_or_none(ref.get("index")),
        char_span=[int(part) for part in ref["text_span"]],
    )
    if selector is None:
        raise SourceEvidenceClosureError(
            f"proved heading source {source_index} has no strict locator"
        )
    return selector


def _locator_with_proven_heading_sources(
    locator: dict[str, Any] | None,
    path: Iterable[_ProvenHeading],
    *,
    prepared_by_index: Mapping[int, PreparedElement],
) -> dict[str, Any] | None:
    output = dict(locator or {})
    projections: list[dict[str, Any]] = []
    for target_index, heading in enumerate(path):
        selectors = [
            _heading_ref_selector(
                ref,
                prepared_by_index=prepared_by_index,
            )
            for ref in heading.refs
        ]
        projections.append(
            {
                "target_index": target_index,
                "kind": "source_concat" if len(selectors) > 1 else "source_field",
                **(
                    {"sources": selectors}
                    if len(selectors) > 1
                    else {"selector": selectors[0]}
                ),
                "transform": "clean_text.v1",
            }
        )
    return _with_heading_projection(output, projections)


def _heading_only_locator(
    heading: _ProvenHeading,
    *,
    path: Iterable[_ProvenHeading],
    prepared_by_index: Mapping[int, PreparedElement],
) -> dict[str, Any] | None:
    first = prepared_by_index[int(heading.refs[0]["source_item_index"])]
    output = _locator_with_proven_heading_sources(
        first.artifact_locator,
        path,
        prepared_by_index=prepared_by_index,
    )
    selectors = [
        _heading_ref_selector(ref, prepared_by_index=prepared_by_index)
        for ref in heading.refs
    ]
    output = _with_payload_projection(
        output,
        {
            "kind": "text_concat" if len(selectors) > 1 else "text_identity",
            "sources": selectors,
            "target_field": "payload.text",
            "transform": (
                "ordered_text_concat.v1" if len(selectors) > 1 else "clean_text.v1"
            ),
        },
    )
    if output is not None:
        output["derivation"] = {
            "kind": "heading_without_payload",
            "reason": "proved_heading_has_no_descendant_payload",
        }
    return output


def _prepared_section_identity(element: PreparedElement) -> tuple[object, ...]:
    if not element.inherits_section:
        return ("detached", element.raw_kind)
    if element.section_path:
        return ("occurrence", *element.section_path)
    return ("path", *element.heading_path)


def s3_build_text_units(elements: Iterable[PreparedElement]) -> list[UnitDraft]:
    units: list[UnitDraft] = []
    buffer: list[PreparedElement] = []

    def flush() -> None:
        if not buffer:
            return
        text = "\n".join(item.text or "" for item in buffer if item.text).strip()
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
                    heading_path=list(buffer[0].heading_path),
                    section_path=list(buffer[0].section_path),
                    title=buffer[0].title,
                    quality_status=quality,
                    artifact_locator=_prepared_group_locator(buffer),
                    detached_from_section=not buffer[0].inherits_section,
                )
            )
        buffer.clear()

    for element in elements:
        if element.kind == "text" and element.payload is None:
            same_section = not buffer or (
                _prepared_section_identity(element)
                == _prepared_section_identity(buffer[-1])
            )
            if buffer and not same_section:
                flush()
            buffer.append(element)
        else:
            flush()
            if element.kind == "text" and element.payload is not None:
                units.append(
                    UnitDraft(
                        payload_kind="text",
                        payload=element.payload,
                        source_order=element.order_index,
                        source_order_phase=element.source_order_phase,
                        heading_path=list(element.heading_path),
                        section_path=list(element.section_path),
                        title=element.title,
                        quality_status=element.quality_status,
                        artifact_locator=element.artifact_locator,
                        detached_from_section=not element.inherits_section,
                    )
                )
    flush()
    return units


def _prepared_group_locator(
    elements: list[PreparedElement],
    *,
    join_transform: str = "ordered_text_concat.v1",
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
    first = public_artifact_locator(locators[0]) or {}
    graph = empty_projection_graph()
    graph["payload"] = (
        {
            "kind": "text_concat" if len(payload_sources) > 1 else "text_identity",
            "sources": payload_sources,
            "target_field": "payload.text",
            "transform": (
                join_transform if len(payload_sources) > 1 else "clean_text.v1"
            ),
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
    graph["search_targets"] = ["payload.text"]
    first["source_projection"] = graph
    return first


def s5_build_table_units(
    elements: Iterable[PreparedElement], stats: BuildStats
) -> list[UnitDraft]:
    units: list[UnitDraft] = []
    for element in elements:
        if element.kind != "table":
            continue
        if _is_empty_table_element(element):
            stats.dropped_by_kind["table_empty"] += 1
            continue
        units.append(_table_to_unit(element))
    return units


def _is_heading_only_evidence(unit: UnitDraft) -> bool:
    derivation = (unit.artifact_locator or {}).get("derivation")
    return bool(
        unit.payload_kind == "text"
        and isinstance(derivation, dict)
        and derivation.get("kind") == "heading_without_payload"
    )


def _group_structural_units(
    units: list[UnitDraft],
    *,
    document_title: str | None,
    stats: BuildStats,
) -> list[UnitDraft]:
    """Assemble exactly one primary block per proven section occurrence.

    Page furniture and payload kinds never create a business boundary.
    Unproved furniture remains source-ordered, document-scope evidence inside
    the current container, while empty child headings become heading parts of
    their proven parent occurrence.  This single pass avoids nested mixed
    payloads and keeps every atomic locator intact.
    """

    output: list[UnitDraft] = []
    pending: list[UnitDraft] = []
    pending_identity: tuple[object, ...] | None = None
    pending_path: list[str] = []
    pending_section_path: list[int] = []
    pending_has_payload = False
    held_native: list[UnitDraft] = []
    held_furniture: list[UnitDraft] = []

    def context(
        unit: UnitDraft,
    ) -> tuple[tuple[object, ...], list[str], list[int]] | None:
        if unit.detached_from_section:
            return None
        if not unit.heading_path:
            # Registered metadata can label evidence for retrieval, but it is
            # not a PDF section occurrence and therefore cannot create a
            # grouping boundary.  The same source PDF must keep the same unit
            # boundaries whether its provider title is present or absent.
            return None
        path = list(unit.heading_path)
        section_path = list(unit.section_path)
        if _is_heading_only_evidence(unit) and len(path) > 1:
            path = path[:-1]
            if section_path:
                section_path = section_path[:-1]
        identity = ("occurrence", *section_path) if section_path else ("path", *path)
        return identity, path, section_path

    def drain_held() -> None:
        nonlocal held_furniture, held_native
        if held_furniture:
            # Furniture that arrived while its section was still heading-only
            # never became container content; it trails the heading unit as
            # ordinary document-scope evidence instead.
            output.extend(held_furniture)
            held_furniture = []
        if held_native:
            output.extend(
                sorted(held_native, key=lambda unit: unit.source_order)
            )
            held_native = []

    def flush() -> None:
        nonlocal pending, pending_identity, pending_path, pending_section_path
        nonlocal pending_has_payload
        pending_has_payload = False
        if not pending:
            drain_held()
            return
        members = pending
        path = pending_path
        section_path = pending_section_path
        pending = []
        pending_identity = None
        pending_path = []
        pending_section_path = []
        first = members[0]
        needs_container = (
            len(members) > 1
            or first.detached_from_section
            or first.heading_path != path
        )
        if not needs_container:
            output.append(members[0])
            drain_held()
            return

        title = path[-1] if path else first.title
        locator: dict[str, Any] = {}
        locator = (
            _with_payload_projection(
                locator,
                {
                    "kind": "container",
                    "sources": [],
                    "target_field": "payload.parts",
                    "transform": "ordered_parts.v1",
                },
            )
            or locator
        )
        first_heading_projection = list(
            _projection_graph(dict(first.artifact_locator or {}))["heading_path"]
        )[: len(path)]
        locator = _with_heading_projection(locator, first_heading_projection) or locator
        parts: list[dict[str, Any]] = []
        saw_heading_part = False
        for member in members:
            part = _unit_part(member)
            if member.detached_from_section:
                part.pop("heading_path", None)
            elif _is_heading_only_evidence(member):
                saw_heading_part = True
            parts.append(part)
        output.append(
            UnitDraft(
                payload_kind="mixed",
                payload={
                    "semantic_type": ("section" if section_path else "document"),
                    "order_status": "unresolved_physical_fallback",
                    "parts": parts,
                },
                source_order=first.source_order,
                heading_path=path,
                section_path=section_path,
                title=title,
                quality_status=_worst_quality(members),
                artifact_locator=locator or None,
            )
        )
        if saw_heading_part:
            stats.heading_outline_units_generated += 1
        drain_held()

    for unit in units:
        if unit.detached_from_section:
            if pending:
                if unit.payload_kind == "mixed":
                    # Native gap runs are already complete mixed evidence;
                    # nesting them as container parts would violate the
                    # no-nested-mixed invariant, so they trail their
                    # surrounding section container instead.
                    held_native.append(unit)
                elif pending_has_payload:
                    pending.append(unit)
                else:
                    # A heading with no substantive member yet is not a
                    # section occurrence; furniture must not fabricate one.
                    held_furniture.append(unit)
            else:
                output.append(unit)
            continue
        unit_context = context(unit)
        if unit_context is None:
            flush()
            output.append(unit)
            continue
        unit_identity, path, section_path = unit_context
        if pending and unit_identity != pending_identity:
            flush()
        if not _is_heading_only_evidence(unit):
            if held_furniture and pending:
                # The section proved real content after a page break, so the
                # furniture that arrived in between rides inside it after all.
                pending.extend(held_furniture)
                held_furniture.clear()
            pending_has_payload = True
        pending.append(unit)
        pending_identity = unit_identity
        pending_path = path
        pending_section_path = section_path
    flush()
    labeled_output = [
        (
            replace(unit, title=document_title)
            if document_title is not None
            and not unit.heading_path
            and unit.title is None
            and not _carries_native_context(unit)
            else unit
        )
        for unit in output
    ]
    seen_sections: set[tuple[int, ...]] = set()
    for unit in labeled_output:
        if unit.section_path:
            occurrence = tuple(unit.section_path)
            if occurrence in seen_sections:
                raise SourceEvidenceClosureError(
                    "one proven section occurrence produced multiple primary units"
                )
            seen_sections.add(occurrence)
        if unit.payload_kind == "mixed" and any(
            isinstance(part, Mapping) and part.get("kind") == "mixed"
            for part in unit.payload.get("parts", [])
        ):
            raise SourceEvidenceClosureError(
                "structural occurrence assembly produced nested mixed evidence"
            )
    return labeled_output


def s7_finalize_units(
    units: Iterable[UnitDraft],
    *,
    filing_type: str | None,
    stats: BuildStats,
) -> list[UnitDraft]:
    filing_keys = (
        ("investor_communication",)
        if filing_type in {"investor_relations", "performance_briefing"}
        else ()
    )
    finalized: list[UnitDraft] = []
    for unit in units:
        if _carries_native_context(unit) or _is_native_only_mixed_owner(unit):
            # Native gap evidence never takes taxonomy labels: it stays on
            # the document-content fallback so retrieval routing can neither
            # invent structure nor hide the run behind a filing-level key.
            semantic_key = retrieval_routing.FALLBACK_KEY
            semantic_keys = [retrieval_routing.FALLBACK_KEY]
        else:
            note_keys = _note_keys_for_unit(unit)
            matched_keys = semantic_keys_for_unit(unit, filing_type=filing_type)
            candidates = _stable_semantic_keys(
                [unit.semantic_key] if unit.semantic_key else [],
                matched_keys,
                note_keys,
                filing_keys,
                unit.semantic_keys or [],
            )
            semantic_key = (
                candidates[0] if candidates else retrieval_routing.FALLBACK_KEY
            )
            semantic_keys = candidates or [retrieval_routing.FALLBACK_KEY]
        quality_status = _final_quality_status(unit)
        if quality_status == "needs_review":
            stats.needs_review_count += 1
        if quality_status == "unusable":
            stats.unusable_count += 1
        stats.generated_by_kind[unit.payload_kind] += 1
        payload = dict(unit.payload)
        if unit.payload_kind == "mixed":
            parts = payload.get("parts")
            if isinstance(parts, list):
                payload["parts"] = [
                    {
                        **part,
                        "artifact_locator": public_artifact_locator(
                            part.get("artifact_locator")
                            if isinstance(part.get("artifact_locator"), Mapping)
                            else None
                        ),
                    }
                    if isinstance(part, dict)
                    else part
                    for part in parts
                ]
        finalized.append(
            replace(
                unit,
                payload=payload,
                semantic_key=semantic_key,
                # New output always has at least the controlled
                # document_content fallback, so scalar and array expose
                # one consistent non-empty retrieval state.
                semantic_keys=semantic_keys,
                quality_status=quality_status,
                artifact_locator=public_artifact_locator(unit.artifact_locator),
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
    image_artifact_resolver: ImageArtifactResolver | None = None,
    native_units: Sequence[UnitDraft] = (),
) -> tuple[list[UnitDraft], BuildStats]:
    require_current_normalized_ir(normalized_ir)
    raw_elements = normalized_ir.get("elements", [])
    if not isinstance(raw_elements, list) or not all(
        isinstance(element, Mapping) for element in raw_elements
    ):
        raise SourceEvidenceClosureError("NormalizedIR elements are invalid")
    source_pdf_sha256 = normalized_ir.get("source_pdf_sha256")
    proof = validate_document_structure(
        normalized_ir.get("structure_proof"),
        elements=raw_elements,
        expected_source_pdf_sha256=(
            source_pdf_sha256 if isinstance(source_pdf_sha256, str) else None
        ),
    )
    s1 = s1_preprocess_elements(
        raw_elements,
        structure_proof=proof,
        image_artifact_resolver=image_artifact_resolver,
    )
    placed = s2_apply_structure_proof(
        s1.elements,
        raw_elements=raw_elements,
        structure_proof=proof,
        stats=s1.stats,
    )
    # QA discrimination was removed: transcripts
    # stay raw text units with full provenance; question/answer semantics are
    # not an L1 concern and no payload_kind="qa" is emitted anymore.
    text_units = s3_build_text_units(placed)
    table_units = s5_build_table_units(placed, s1.stats)
    s1.stats.native_recovery_gap_count = len(native_units)
    s1.stats.native_recovery_leaf_count = sum(
        len(parts)
        for native_unit in native_units
        if isinstance((parts := native_unit.payload.get("parts")), list)
    )
    units = sorted(
        [*text_units, *table_units, *native_units], key=_unit_sort_key
    )
    raw_document_title = (
        str(normalized_ir["title"])
        if isinstance(normalized_ir.get("title"), str)
        and str(normalized_ir["title"]).strip()
        else None
    )
    document_title = (
        raw_document_title
        if raw_document_title is not None
        and not unsafe_semantic_characters(raw_document_title)
        else None
    )
    if raw_document_title is not None and document_title is None:
        s1.stats.unsafe_document_title_label_count += 1
    grouped = _group_structural_units(
        units,
        document_title=document_title,
        stats=s1.stats,
    )
    grouped = _classify_page_furniture_supports(
        grouped,
        raw_elements=raw_elements,
        stats=s1.stats,
    )
    grouped = _classify_owner_native_alternatives(
        grouped,
        raw_elements=raw_elements,
        stats=s1.stats,
    )
    grouped = _flag_coverage_gap_owners(grouped)
    grouped = _suppress_punctuation_only_natives(grouped, stats=s1.stats)
    grouped = _embed_native_recoveries(grouped, raw_elements=raw_elements)
    grouped = _coalesce_adjacent_root_units(grouped)
    grouped = _sanitize_unsafe_semantic_units(grouped, stats=s1.stats)
    return (
        s7_finalize_units(
            grouped,
            filing_type=filing_type,
            stats=s1.stats,
        ),
        s1.stats,
    )


def _native_containment_owner(unit: UnitDraft) -> int | None:
    locator = unit.artifact_locator
    if not isinstance(locator, Mapping):
        return None
    graph = locator.get("source_projection")
    context = (
        graph.get("physical_context") if isinstance(graph, Mapping) else None
    )
    if not isinstance(context, Mapping):
        return None
    owner = context.get("containment_owner")
    if isinstance(owner, int) and not isinstance(owner, bool):
        return owner
    return None


def _embed_native_recoveries(
    units: list[UnitDraft],
    *,
    raw_elements: Sequence[Mapping[str, Any]],
) -> list[UnitDraft]:
    """Publish native recoveries as leaves of one existing coarse owner.

    A source-native gap is evidence missing from the provider carrier, not a
    business section.  Giving every gap its own ``document_units_v1`` row made
    table digits look like stand-alone gibberish and created duplicate search
    targets.  This pass keeps the exact selectors and physical context on the
    leaf while assigning the leaf to a table/section/root unit.

    Containment is authoritative. Otherwise equal predecessor/successor
    owners are authoritative; a disagreement or missing side is flattened to
    a document-root segment. No text phrase, issuer, filing, or document
    identifier participates.
    """

    recoveries = [unit for unit in units if _carries_native_context(unit)]
    if not recoveries:
        return units
    owners = [unit for unit in units if not _carries_native_context(unit)]
    if not owners:
        return [_native_only_document_owner(recoveries)]

    owner_indices_by_source: dict[int, list[int]] = {}
    for owner_index, owner in enumerate(owners):
        for source_index in _unit_source_item_indices_for_attribution(owner):
            owner_indices_by_source.setdefault(source_index, []).append(owner_index)
    element_kinds = {
        element_index: str(element.get("kind", ""))
        for element in raw_elements
        if isinstance(
            (element_index := element.get("source_item_index")), int
        )
        and not isinstance(element_index, bool)
    }

    assigned: dict[int, list[UnitDraft]] = {}
    root_recoveries: list[UnitDraft] = []
    for recovery in recoveries:
        recovery_owner_index = _native_recovery_owner_index(
            recovery,
            owners=owners,
            owner_indices_by_source=owner_indices_by_source,
            element_kinds=element_kinds,
        )
        if recovery_owner_index is None:
            root_recoveries.append(recovery)
        else:
            assigned.setdefault(recovery_owner_index, []).append(recovery)

    output = [
        _embed_recoveries_in_owner(owner, assigned.get(index, ()))
        for index, owner in enumerate(owners)
    ]
    if root_recoveries:
        # A page-only/prefix gap or a gap crossing sibling owners has no
        # defensible section placement.  Each maximal gap remains an honest
        # root *segment* at its physical position; combining non-contiguous
        # root leaves across child sections would corrupt flat unit order.
        output.extend(
            _native_only_document_owner((recovery,))
            for recovery in root_recoveries
        )
    return sorted(output, key=_unit_sort_key)


_CONTAINMENT_PRIMARY_FIELD_BY_KIND = {
    "table": "table",
    "image": "image",
    "equation": "image",
}


def _native_recovery_owner_index(
    recovery: UnitDraft,
    *,
    owners: list[UnitDraft],
    owner_indices_by_source: Mapping[int, list[int]],
    element_kinds: Mapping[int, str],
) -> int | None:
    context = _native_physical_context(recovery)
    if context.get("relation") == "bounded_by_same_source":
        # Containment names a physical region inside one element's primary
        # body, so only primary payload selector ownership competes here: a
        # unit holding just an associated selector (e.g. a selected-only
        # detached table caption) never buys containment. A unique primary
        # owner wins even when it is heading-only — exact containment is the
        # proof that the missing native content is that section's own body.
        # This branch is CLOSED: zero or multiple primary owners flatten to
        # an honest root segment and never fall through to the element-level
        # adjacency lanes, which would let an associated owner back in.
        containment_owner = context.get("containment_owner")
        if (
            isinstance(containment_owner, int)
            and not isinstance(containment_owner, bool)
            and context.get("order_basis") == "containment_proven"
        ):
            expected_field = _CONTAINMENT_PRIMARY_FIELD_BY_KIND.get(
                element_kinds.get(containment_owner, ""), "text"
            )
            primary = [
                candidate
                for candidate in owner_indices_by_source.get(
                    containment_owner, []
                )
                if _unit_claims_primary_payload_field(
                    owners[candidate],
                    source_index=containment_owner,
                    expected_field=expected_field,
                )
            ]
            if len(primary) == 1:
                return primary[0]
        return None

    predecessor = _context_source_index(context.get("predecessor"))
    successor = _context_source_index(context.get("successor"))
    relation = context.get("relation")
    predecessor_owner = _unique_owner_index(
        predecessor, owner_indices_by_source=owner_indices_by_source
    )
    successor_owner = _unique_owner_index(
        successor, owner_indices_by_source=owner_indices_by_source
    )
    if relation in {"page_prefix", "page_only"}:
        return None
    if relation == "page_suffix":
        # One-sided adjacency cannot turn a heading-only marker into a
        # content owner. Keep the heading intact and flatten the residual to
        # a root segment; a real table/paragraph/section carrier still owns
        # its page suffix.
        return (
            predecessor_owner
            if predecessor_owner is not None
            and not _payload_is_own_heading(owners[predecessor_owner])
            else None
        )
    if relation == "between_mapped_sources":
        return (
            predecessor_owner
            if predecessor_owner is not None
            and predecessor_owner == successor_owner
            and not _payload_is_own_heading(owners[predecessor_owner])
            else None
        )
    return None


def _unit_claims_primary_payload_field(
    unit: UnitDraft,
    *,
    source_index: int,
    expected_field: str,
) -> bool:
    """Whether one unit's payload projection claims the element's primary field."""

    locators: list[Mapping[str, Any]] = []
    if unit.payload_kind == "mixed":
        for part in unit.payload.get("parts", ()):
            if isinstance(part, Mapping):
                part_locator = part.get("artifact_locator")
                if isinstance(part_locator, Mapping):
                    locators.append(part_locator)
    if isinstance(unit.artifact_locator, Mapping):
        locators.append(unit.artifact_locator)
    for locator in locators:
        graph = locator.get("source_projection")
        if not isinstance(graph, Mapping):
            continue
        payload_projection = graph.get("payload")
        if not isinstance(payload_projection, Mapping):
            continue
        sources = payload_projection.get("sources")
        if not isinstance(sources, list):
            continue
        for entry in sources:
            if not isinstance(entry, Mapping):
                continue
            source = entry.get("source")
            field = entry.get("field")
            if (
                isinstance(source, Mapping)
                and isinstance(field, Mapping)
                and source.get("source_item_index") == source_index
                and field.get("kind") == expected_field
            ):
                return True
    return False


def _native_physical_context(unit: UnitDraft) -> Mapping[str, Any]:
    locator = unit.artifact_locator
    graph = (
        locator.get("source_projection") if isinstance(locator, Mapping) else None
    )
    context = graph.get("physical_context") if isinstance(graph, Mapping) else None
    if not isinstance(context, Mapping):
        raise SourceEvidenceClosureError("native recovery has no physical context")
    return context


def _context_source_index(raw: object) -> int | None:
    if not isinstance(raw, Mapping):
        return None
    source = raw.get("source")
    value = source.get("source_item_index") if isinstance(source, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _unique_owner_index(
    source_index: int | None,
    *,
    owner_indices_by_source: Mapping[int, list[int]],
) -> int | None:
    if source_index is None:
        return None
    candidates = owner_indices_by_source.get(source_index, [])
    return candidates[0] if len(candidates) == 1 else None


def _payload_is_own_heading(unit: UnitDraft) -> bool:
    """Return true when one atomic payload only repeats its heading source."""

    if (
        unit.payload_kind != "text"
        or not unit.heading_path
        or unit.payload.get("text") != unit.heading_path[-1]
    ):
        return False
    graph = _projection_graph(dict(unit.artifact_locator or {}))
    payload = graph.get("payload")
    headings = graph.get("heading_path")
    if (
        not isinstance(payload, Mapping)
        or not isinstance(headings, list)
        or not headings
    ):
        return False
    payload_sources = payload.get("sources")
    last = headings[-1]
    if not isinstance(payload_sources, list) or not isinstance(last, Mapping):
        return False
    heading_sources = last.get("sources")
    if not isinstance(heading_sources, list):
        selector = last.get("selector")
        heading_sources = [selector] if isinstance(selector, Mapping) else []
    return payload_sources == heading_sources


def _embed_recoveries_in_owner(
    owner: UnitDraft,
    recoveries: Iterable[UnitDraft],
) -> UnitDraft:
    recovery_list = list(recoveries)
    if not recovery_list:
        return owner
    owner_parts = (
        [dict(part) for part in owner.payload.get("parts", ())]
        if owner.payload_kind == "mixed"
        else [_unit_part(owner)]
    )
    # Insert from the end so recoveries sharing one carrier anchor retain
    # their canonical order instead of repeatedly occupying the same slot.
    for recovery in reversed(sorted(recovery_list, key=_unit_sort_key)):
        native_parts = _native_recovery_parts(
            recovery,
            owner_heading_path=owner.heading_path,
        )
        insertion = _native_insertion_index(owner_parts, recovery)
        owner_parts[insertion:insertion] = native_parts

    owner_was_mixed = owner.payload_kind == "mixed"
    if owner_was_mixed:
        locator = owner.artifact_locator
    else:
        graph = empty_projection_graph()
        graph["payload"] = {
            "kind": "container",
            "sources": [],
            "target_field": "payload.parts",
            "transform": "ordered_parts.v1",
        }
        owner_graph = _projection_graph(dict(owner.artifact_locator or {}))
        graph["heading_path"] = list(owner_graph["heading_path"])
        locator = {"source_projection": graph}
    return replace(
        owner,
        payload_kind="mixed",
        payload={
            "semantic_type": "section" if owner.section_path else "document",
            "order_status": "unresolved_physical_fallback",
            "parts": owner_parts,
        },
        quality_status=_worst_quality([owner, *recovery_list]),
        # Converting an atomic owner into a container moves its typed
        # applicability claim to the original atomic child via _unit_part().
        # Keeping the same claim on the new outer container would either lack
        # a matching source projection or publish the assertion twice.
        applicability=owner.applicability if owner_was_mixed else None,
        artifact_locator=locator,
    )


def _native_recovery_parts(
    recovery: UnitDraft,
    *,
    owner_heading_path: list[str],
) -> list[dict[str, Any]]:
    context = dict(_native_physical_context(recovery))
    context["anchor_heading_path"] = list(owner_heading_path)
    parts = recovery.payload.get("parts")
    if not isinstance(parts, list) or not parts:
        raise SourceEvidenceClosureError("native recovery has no payload leaves")
    output: list[dict[str, Any]] = []
    for raw_part in parts:
        if not isinstance(raw_part, Mapping):
            raise SourceEvidenceClosureError("native recovery leaf is invalid")
        part = dict(raw_part)
        locator = dict(part.get("artifact_locator") or {})
        graph = dict(locator.get("source_projection") or {})
        graph["physical_context"] = context
        locator["source_projection"] = graph
        part["artifact_locator"] = locator
        part.pop("heading_path", None)
        part.pop("applicability", None)
        output.append(part)
    return output


def _native_insertion_index(
    owner_parts: list[dict[str, Any]],
    recovery: UnitDraft,
) -> int:
    context = _native_physical_context(recovery)
    containment = context.get("containment_owner")
    predecessor = _context_source_index(context.get("predecessor"))
    successor = _context_source_index(context.get("successor"))
    after = (
        containment
        if isinstance(containment, int) and not isinstance(containment, bool)
        else predecessor
    )
    if after is not None:
        matches = [
            index
            for index, part in enumerate(owner_parts)
            if after in _part_source_item_indices(part)
        ]
        if matches:
            return matches[-1] + 1
    if successor is not None:
        matches = [
            index
            for index, part in enumerate(owner_parts)
            if successor in _part_source_item_indices(part)
        ]
        if matches:
            return matches[0]
    return len(owner_parts)


def _part_source_item_indices(part: Mapping[str, Any]) -> set[int]:
    locator = part.get("artifact_locator")
    refs = payload_source_refs(
        payload_kind=str(part.get("kind")),
        payload=part,
        artifact_locator=locator if isinstance(locator, Mapping) else None,
    )
    return {
        int(ref["source_item_index"])
        for ref in refs
        if isinstance(ref.get("source_item_index"), int)
        and not isinstance(ref.get("source_item_index"), bool)
    }


def _native_only_document_owner(recoveries: Iterable[UnitDraft]) -> UnitDraft:
    recovery_list = sorted(recoveries, key=_unit_sort_key)
    graph = empty_projection_graph()
    graph["payload"] = {
        "kind": "container",
        "sources": [],
        "target_field": "payload.parts",
        "transform": "ordered_parts.v1",
    }
    return UnitDraft(
        payload_kind="mixed",
        payload={
            "semantic_type": "document",
            "order_status": "unresolved_physical_fallback",
            "parts": [
                part
                for recovery in recovery_list
                for part in _native_recovery_parts(
                    recovery,
                    owner_heading_path=[],
                )
            ],
        },
        source_order=min(recovery.source_order for recovery in recovery_list),
        quality_status=_worst_quality(recovery_list),
        artifact_locator={"source_projection": graph},
        native_order_anchor=recovery_list[0].native_order_anchor,
    )


def _coalesce_adjacent_root_units(units: list[UnitDraft]) -> list[UnitDraft]:
    """Publish one durable document-root owner for each contiguous root span."""

    output: list[UnitDraft] = []
    pending: list[UnitDraft] = []

    def eligible(unit: UnitDraft) -> bool:
        return bool(
            not unit.heading_path
            and not unit.section_path
            and not _carries_native_context(unit)
        )

    def flush() -> None:
        if not pending:
            return
        if len(pending) == 1:
            output.append(pending.pop())
            return
        if all(unit.detached_from_section for unit in pending):
            output.extend(pending)
            pending.clear()
            return
        titles = {unit.title for unit in pending if unit.title is not None}
        if len(titles) > 1:
            raise SourceEvidenceClosureError(
                "one document-root span has conflicting metadata titles"
            )
        parts: list[dict[str, Any]] = []
        evidence_artifacts: list[dict[str, Any]] = []
        seen_artifacts: set[tuple[object, ...]] = set()
        for unit in pending:
            if unit.payload_kind == "mixed":
                raw_parts = unit.payload.get("parts")
                if not isinstance(raw_parts, list) or not all(
                    isinstance(part, Mapping) for part in raw_parts
                ):
                    raise SourceEvidenceClosureError(
                        "document-root mixed unit has invalid parts"
                    )
                parts.extend(dict(part) for part in raw_parts)
            else:
                parts.append(_unit_part(unit))
            unit_locator = unit.artifact_locator
            raw_artifacts = (
                unit_locator.get("evidence_artifacts")
                if isinstance(unit_locator, Mapping)
                else None
            )
            if isinstance(raw_artifacts, list):
                for artifact in raw_artifacts:
                    if not isinstance(artifact, Mapping):
                        continue
                    identity = (
                        artifact.get("artifact_role"),
                        artifact.get("sha256"),
                    )
                    if identity in seen_artifacts:
                        continue
                    seen_artifacts.add(identity)
                    evidence_artifacts.append(dict(artifact))
        graph = empty_projection_graph()
        graph["payload"] = {
            "kind": "container",
            "sources": [],
            "target_field": "payload.parts",
            "transform": "ordered_parts.v1",
        }
        root_locator: dict[str, Any] = {"source_projection": graph}
        if evidence_artifacts:
            root_locator["evidence_artifacts"] = evidence_artifacts
        first = pending[0]
        output.append(
            UnitDraft(
                payload_kind="mixed",
                payload={
                    "semantic_type": "document",
                    "order_status": "unresolved_physical_fallback",
                    "parts": parts,
                },
                source_order=first.source_order,
                title=next(iter(titles)) if titles else None,
                quality_status=_worst_quality(pending),
                artifact_locator=root_locator,
                native_order_anchor=first.native_order_anchor,
            )
        )
        pending.clear()

    for unit in units:
        if eligible(unit):
            pending.append(unit)
            continue
        flush()
        output.append(unit)
    flush()
    return output


def _flag_coverage_gap_owners(units: list[UnitDraft]) -> list[UnitDraft]:
    """A containment-proven recovery marks its owner unit for review.

    The gap proves native words fell inside the owner element's span
    without being covered by its payload — the carrier lost content
    there (a dropped wrapped minus sign, a truncated cell). The owner
    keeps publishing, but never as a silent ``ok``; the recovery unit's
    ``containment_owner`` stays the public, typed reason. Anchors that
    only prove page adjacency (``page_suffix``/``between_mapped_sources``)
    say nothing about the carrier's interior and must not flag it.
    """

    owners: set[int] = set()
    for unit in units:
        if not _carries_native_context(unit):
            continue
        owner = _native_containment_owner(unit)
        if owner is not None:
            owners.add(owner)
    if not owners:
        return units
    output: list[UnitDraft] = []
    for unit in units:
        if (
            _carries_native_context(unit)
            or unit.quality_status != "ok"
            or not (owners & _unit_source_item_indices_for_attribution(unit))
        ):
            output.append(unit)
            continue
        output.append(replace(unit, quality_status="needs_review"))
    return output


def _payload_projection_source_indices(
    locator: object,
) -> frozenset[int] | None:
    """Source item indices behind one leaf's payload projection, if closed."""

    if not isinstance(locator, Mapping):
        return None
    graph = locator.get("source_projection")
    if not isinstance(graph, Mapping):
        return None
    payload_projection = graph.get("payload")
    if not isinstance(payload_projection, Mapping):
        return None
    sources = payload_projection.get("sources")
    if not isinstance(sources, list) or not sources:
        return None
    indices: set[int] = set()
    for entry in sources:
        source = entry.get("source") if isinstance(entry, Mapping) else None
        index = source.get("source_item_index") if isinstance(source, Mapping) else None
        if not isinstance(index, int) or isinstance(index, bool):
            return None
        indices.add(index)
    return frozenset(indices)


def _classify_page_furniture_supports(
    units: list[UnitDraft],
    *,
    raw_elements: Sequence[Mapping[str, Any]],
    stats: BuildStats,
) -> list[UnitDraft]:
    """Retained page furniture never becomes active primary search content.

    An unproved header/footer/page-number carrier stays a published leaf for
    display and provenance, but its provider-typed ``page_furniture`` kind —
    never its text — closes its search role. Real body content that merely
    repeats a furniture string keeps its primary search edge, so no phrase
    list can leak in here.
    """

    furniture_indices = {
        source_item_index
        for element in raw_elements
        if element.get("kind") == "page_furniture"
        and isinstance(
            (source_item_index := element.get("source_item_index")), int
        )
        and not isinstance(source_item_index, bool)
    }
    if not furniture_indices:
        return units

    def support_part(part: object) -> object:
        if not isinstance(part, Mapping) or part.get("kind") != "text":
            return part
        if part.get("representation_role") is not None:
            return part
        indices = _payload_projection_source_indices(part.get("artifact_locator"))
        if indices is None or not indices <= furniture_indices:
            return part
        locator = cast(Mapping[str, Any], part["artifact_locator"])
        graph = dict(cast(Mapping[str, Any], locator["source_projection"]))
        graph["search_targets"] = []
        support_locator = dict(locator)
        support_locator["source_projection"] = graph
        return {
            **part,
            "representation_role": "page_furniture_unproved",
            "search_policy": "none",
            "quality_status": "needs_review",
            "artifact_locator": support_locator,
        }

    output: list[UnitDraft] = []
    for unit in units:
        if unit.payload_kind == "mixed":
            parts = unit.payload.get("parts")
            if not isinstance(parts, list):
                output.append(unit)
                continue
            classified = [support_part(part) for part in parts]
            changed = sum(
                original is not result
                for original, result in zip(parts, classified, strict=True)
            )
            if changed:
                stats.page_furniture_support_count += changed
                output.append(
                    replace(
                        unit,
                        payload={**unit.payload, "parts": classified},
                    )
                )
            else:
                output.append(unit)
            continue
        if unit.payload_kind == "text":
            indices = _payload_projection_source_indices(unit.artifact_locator)
            if (
                indices is not None
                and indices <= furniture_indices
                and unit.payload.get("representation_role") is None
            ):
                locator = dict(unit.artifact_locator or {})
                graph = dict(
                    cast(Mapping[str, Any], locator["source_projection"])
                )
                graph["search_targets"] = []
                locator["source_projection"] = graph
                stats.page_furniture_support_count += 1
                output.append(
                    replace(
                        unit,
                        payload={
                            **unit.payload,
                            "representation_role": "page_furniture_unproved",
                            "search_policy": "none",
                        },
                        artifact_locator=locator,
                        quality_status="needs_review",
                    )
                )
                continue
        output.append(unit)
    return output


def _classify_owner_native_alternatives(
    units: list[UnitDraft],
    *,
    raw_elements: Sequence[Mapping[str, Any]],
    stats: BuildStats,
) -> list[UnitDraft]:
    """Keep coarse-owner duplicates as non-primary, positioned alternatives.

    ``bounded_by_same_source`` proves a coarse owner, never a cell or physical
    occurrence alias.  Consequently this pass must not delete a native leaf.
    When the same strict surface is already searchable in that owner, the leaf
    remains in payload/provenance but carries no second primary search edge.
    Nonmatching residuals remain primary searchable recoveries.
    """

    owners = {
        source_item_index: element
        for element in raw_elements
        if isinstance(
            (source_item_index := element.get("source_item_index")), int
        )
        and not isinstance(source_item_index, bool)
    }
    output: list[UnitDraft] = []
    for unit in units:
        if not _carries_native_context(unit):
            output.append(unit)
            continue
        context = _native_physical_context(unit)
        owner_index = context.get("containment_owner")
        owner = (
            owners.get(owner_index)
            if isinstance(owner_index, int) and not isinstance(owner_index, bool)
            else None
        )
        carrier_surfaces = (
            source_carrier_search_surfaces(owner)
            if isinstance(owner, Mapping)
            else ()
        )
        if not (
            context.get("relation") == "bounded_by_same_source"
            and context.get("order_basis") == "containment_proven"
            and carrier_surfaces
        ):
            output.append(unit)
            continue
        parts = unit.payload.get("parts")
        if not isinstance(parts, list) or not parts:
            output.append(unit)
            continue
        classified: list[object] = []
        alternative_count = 0
        for part in parts:
            if not isinstance(part, Mapping):
                classified.append(part)
                continue
            native_text = part.get("text")
            if part.get("kind") != "text" or not isinstance(native_text, str):
                classified.append(part)
                continue
            residual = strict_source_comparison_text(native_text)
            if (
                not residual
                or punctuation_only_text(native_text)
                or not any(residual in surface for surface in carrier_surfaces)
            ):
                classified.append(part)
                continue
            locator = part.get("artifact_locator")
            graph = (
                locator.get("source_projection")
                if isinstance(locator, Mapping)
                else None
            )
            if not isinstance(locator, Mapping) or not isinstance(graph, Mapping):
                raise SourceEvidenceClosureError(
                    "native owner alternative has no source projection"
                )
            alternative_graph = dict(graph)
            alternative_graph["search_targets"] = []
            alternative_locator = dict(locator)
            alternative_locator["source_projection"] = alternative_graph
            classified.append(
                {
                    **part,
                    "representation_role": "unresolved_source_alternative",
                    "search_policy": "none",
                    "quality_status": "needs_review",
                    "artifact_locator": alternative_locator,
                }
            )
            alternative_count += 1
        if alternative_count == 0:
            output.append(unit)
            continue
        stats.non_primary_source_alternative_count += alternative_count
        output.append(
            replace(
                unit,
                payload={**unit.payload, "parts": classified},
                quality_status=_at_least_needs_review(unit.quality_status),
            )
        )
    return output


def _suppress_punctuation_only_natives(
    units: list[UnitDraft],
    *,
    stats: BuildStats,
) -> list[UnitDraft]:
    """Drop recoveries that carry only leader/placeholder punctuation.

    The provider omitted these marks deliberately (TOC dot leaders,
    empty-cell dashes); recovering them as units adds retrieval noise
    without evidence value. Suppression runs after owner flagging, so a
    lossy carrier keeps its needs_review even when its gap disappears,
    and the audit re-derives the same predicate from its own partition —
    absence is enforced, publication of such a run is rejected.
    """

    output: list[UnitDraft] = []
    for unit in units:
        if not _carries_native_context(unit):
            output.append(unit)
            continue
        parts = unit.payload.get("parts")
        if not isinstance(parts, list) or not parts:
            output.append(unit)
            continue
        retained: list[object] = []
        suppressed = 0
        for part in parts:
            text = part.get("text") if isinstance(part, Mapping) else None
            if (
                isinstance(part, Mapping)
                and part.get("kind") == "text"
                and isinstance(text, str)
                and punctuation_only_text(text)
            ):
                suppressed += 1
            else:
                retained.append(part)
        if suppressed == 0:
            output.append(unit)
            continue
        stats.punctuation_only_native_runs += suppressed
        if retained:
            output.append(
                replace(
                    unit,
                    payload={**unit.payload, "parts": retained},
                )
            )
    return output


_SAFE_TEXT_TRANSFORMS = {
    "clean_text.v1": "safe_clean_text.v1",
    "identity.v1": "safe_identity.v1",
    "exact_concat.v1": "safe_exact_concat.v1",
    "ordered_text_concat.v1": "safe_ordered_text_concat.v1",
    "ordered_visible_fields.v1": "safe_ordered_visible_fields.v1",
}


def _sanitize_unsafe_semantic_units(
    units: list[UnitDraft],
    *,
    stats: BuildStats,
) -> list[UnitDraft]:
    """Keep undecoded glyphs in source evidence, never semantic payload text."""

    output: list[UnitDraft] = []
    for unit in units:
        if _unsafe_text(unit.title) or any(_unsafe_text(item) for item in unit.heading_path):
            raise SourceEvidenceClosureError(
                "undecoded glyph cannot publish as a title or heading path"
            )
        if unit.payload_kind != "mixed":
            payload, locator, changed = _sanitize_atomic_semantic_payload(
                payload_kind=unit.payload_kind,
                payload=unit.payload,
                artifact_locator=unit.artifact_locator,
            )
            if not changed:
                output.append(unit)
                continue
            stats.unsafe_semantic_payload_count += 1
            output.append(
                replace(
                    unit,
                    payload=payload,
                    artifact_locator=locator,
                    quality_status=_at_least_needs_review(unit.quality_status),
                    applicability=None,
                )
            )
            continue

        raw_parts = unit.payload.get("parts")
        if not isinstance(raw_parts, list):
            output.append(unit)
            continue
        parts: list[object] = []
        changed_count = 0
        for raw_part in raw_parts:
            if not isinstance(raw_part, Mapping):
                parts.append(raw_part)
                continue
            raw_locator = raw_part.get("artifact_locator")
            semantic_fields = {
                key: value
                for key, value in raw_part.items()
                if key != "artifact_locator"
            }
            sanitized, locator, changed = _sanitize_atomic_semantic_payload(
                payload_kind=str(raw_part.get("kind")),
                payload=semantic_fields,
                artifact_locator=(
                    raw_locator if isinstance(raw_locator, Mapping) else None
                ),
            )
            if not changed:
                parts.append(raw_part)
                continue
            changed_count += 1
            sanitized.pop("applicability", None)
            sanitized["quality_status"] = _at_least_needs_review(
                str(raw_part.get("quality_status") or "ok")
            )
            if locator is not None:
                sanitized["artifact_locator"] = locator
            parts.append(sanitized)
        if changed_count == 0:
            output.append(unit)
            continue
        stats.unsafe_semantic_payload_count += changed_count
        output.append(
            replace(
                unit,
                payload={**unit.payload, "parts": parts},
                quality_status=_at_least_needs_review(unit.quality_status),
                applicability=None,
            )
        )
    return output


def _sanitize_atomic_semantic_payload(
    *,
    payload_kind: str,
    payload: Mapping[str, Any],
    artifact_locator: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    if not _semantic_value_has_unsafe_glyph(payload):
        return dict(payload), (
            dict(artifact_locator) if artifact_locator is not None else None
        ), False
    if payload_kind not in {"text", "table"}:
        raise SourceEvidenceClosureError(
            f"unsafe semantic glyph has no projection transform for {payload_kind!r}"
        )
    if not isinstance(artifact_locator, Mapping):
        raise SourceEvidenceClosureError(
            "unsafe semantic glyph has no source projection"
        )
    locator = dict(artifact_locator)
    graph_raw = locator.get("source_projection")
    if not isinstance(graph_raw, Mapping):
        raise SourceEvidenceClosureError(
            "unsafe semantic glyph has no source projection graph"
        )
    graph = dict(graph_raw)
    edge_raw = graph.get("payload")
    if not isinstance(edge_raw, Mapping):
        raise SourceEvidenceClosureError(
            "unsafe semantic glyph has no payload projection edge"
        )
    edge = dict(edge_raw)
    transform = edge.get("transform")
    if payload_kind == "table":
        if edge.get("kind") != "table_identity" or transform != "table_identity.v1":
            raise SourceEvidenceClosureError(
                "unsafe table glyph lacks the exact table projection"
            )
        edge["transform"] = "safe_table_identity.v1"
    else:
        safe_transform = _SAFE_TEXT_TRANSFORMS.get(str(transform))
        if safe_transform is None:
            raise SourceEvidenceClosureError(
                "unsafe text glyph lacks a closed semantic transform"
            )
        edge["transform"] = safe_transform
    graph["payload"] = edge
    structured = graph.get("structured")
    if isinstance(structured, list):
        graph["structured"] = [
            dict(entry)
            for entry in structured
            if not (
                isinstance(entry, Mapping)
                and entry.get("target_field") == "applicability"
            )
        ]
    locator["source_projection"] = graph
    sanitized = semantic_payload_without_unsafe_glyphs(dict(payload))
    if not isinstance(sanitized, dict):
        raise SourceEvidenceClosureError("sanitized semantic payload is invalid")
    return sanitized, locator, True


def _semantic_value_has_unsafe_glyph(value: object) -> bool:
    if isinstance(value, str):
        return bool(unsafe_semantic_characters(value))
    if isinstance(value, Mapping):
        return any(
            _semantic_value_has_unsafe_glyph(item)
            for key, item in value.items()
            if key != "artifact_locator"
        )
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return any(_semantic_value_has_unsafe_glyph(item) for item in value)
    return False


def _unsafe_text(value: object) -> bool:
    return isinstance(value, str) and bool(unsafe_semantic_characters(value))


def _at_least_needs_review(value: str) -> str:
    return "unusable" if value == "unusable" else "needs_review"


def _unit_source_item_indices_for_attribution(unit: UnitDraft) -> set[int]:
    refs = payload_source_refs(
        payload_kind=unit.payload_kind,
        payload=unit.payload,
        artifact_locator=unit.artifact_locator,
    )
    return {
        int(ref["source_item_index"])
        for ref in refs
        if isinstance(ref.get("source_item_index"), int)
        and not isinstance(ref.get("source_item_index"), bool)
    }


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


def _unit_part(
    unit: UnitDraft,
) -> dict[str, Any]:
    part: dict[str, Any] = {"kind": unit.payload_kind, "order": unit.source_order}
    if unit.payload_kind == "text" and "image_ref" in unit.payload:
        part["kind"] = "image"
    part.update(unit.payload)
    if unit.heading_path:
        part["heading_path"] = list(unit.heading_path)
    if unit.applicability:
        part["applicability"] = unit.applicability
    if unit.quality_status != "ok":
        part["quality_status"] = unit.quality_status
    if unit.artifact_locator:
        part["artifact_locator"] = dict(unit.artifact_locator)
    return part


def _worst_quality(units: list[UnitDraft]) -> str:
    if any(unit.quality_status == "unusable" for unit in units):
        return "unusable"
    if any(unit.quality_status == "needs_review" for unit in units):
        return "needs_review"
    return "ok"


def _carries_native_context(unit: UnitDraft) -> bool:
    """Native stream units must never receive invented document structure."""

    locator = unit.artifact_locator
    if not isinstance(locator, Mapping):
        return False
    graph = locator.get("source_projection")
    return (
        isinstance(graph, Mapping)
        and graph.get("physical_context") is not None
    )


def _is_native_only_mixed_owner(unit: UnitDraft) -> bool:
    """Identify a root segment whose every leaf is source-native evidence."""

    parts = unit.payload.get("parts") if unit.payload_kind == "mixed" else None
    if not isinstance(parts, list) or not parts:
        return False
    for part in parts:
        locator = part.get("artifact_locator") if isinstance(part, Mapping) else None
        graph = (
            locator.get("source_projection")
            if isinstance(locator, Mapping)
            else None
        )
        if not isinstance(graph, Mapping) or graph.get("physical_context") is None:
            return False
    return True


def _unit_sort_key(unit: UnitDraft) -> tuple[int, int, int, int, int]:
    if unit.native_order_anchor is not None:
        anchor_order, page_idx, span_start = unit.native_order_anchor
        return (anchor_order, 2, 0, page_idx, span_start)
    return (unit.source_order, unit.source_order_phase, 0, 0, 0)


def _note_keys_for_unit(unit: UnitDraft) -> list[str]:
    return retrieval_routing.note_keys(_routing_evidence(unit))


def semantic_keys_for_unit(unit: UnitDraft, *, filing_type: str | None) -> list[str]:
    """Compatibility wrapper around the post-assembly routing layer."""

    return retrieval_routing.semantic_keys(
        _routing_evidence(unit),
        filing_type=filing_type,
    )


def _routing_evidence(unit: UnitDraft) -> retrieval_routing.RoutingEvidence:
    caption = unit.payload.get("caption") if unit.payload_kind == "table" else None
    return retrieval_routing.RoutingEvidence(
        title=unit.title,
        heading_path=tuple(unit.heading_path),
        table_caption=(
            tuple(str(value) for value in caption) if isinstance(caption, list) else ()
        ),
        members=_mixed_routing_members(unit),
    )


def _mixed_routing_members(
    unit: UnitDraft,
) -> tuple[retrieval_routing.RoutingMemberEvidence, ...]:
    """Read routing cues from completed parts without changing their boundary."""

    parts = unit.payload.get("parts") if unit.payload_kind == "mixed" else None
    if not isinstance(parts, list):
        return ()
    members: list[retrieval_routing.RoutingMemberEvidence] = []
    parent_path = tuple(unit.heading_path)
    for part in parts:
        if not isinstance(part, dict):
            continue
        raw_path = part.get("heading_path")
        explicit_path = (
            tuple(str(value) for value in raw_path)
            if isinstance(raw_path, list)
            else ()
        )
        explicit_title = explicit_path[-1] if explicit_path else None
        raw_caption = part.get("caption")
        captions = (
            tuple(str(value) for value in raw_caption)
            if isinstance(raw_caption, list)
            else ()
        )
        if not explicit_path and explicit_title is None and not captions:
            continue
        path = explicit_path or parent_path
        members.append(
            retrieval_routing.RoutingMemberEvidence(
                title=explicit_title or unit.title,
                heading_path=path or tuple(unit.heading_path),
                table_caption=captions,
            )
        )
    return tuple(members)


def _clean_text(value: str) -> str:
    # Evidence carriers are immutable. Presentation/search normalization may
    # ignore surrounding whitespace downstream, but L1 must not delete a
    # separator or control character that physically exists in the source.
    return value.strip()


def _element_text(element: dict[str, Any]) -> str:
    value = element.get("text")
    return str(value) if value is not None else ""


def _source_text(value: str) -> str:
    """Trim a structured source field without rewriting its internal syntax."""

    return value.strip()


def _source_text_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _source_text(str(item)))]


def _bound_image_artifact(
    artifact_role: str,
    image_path: str,
    *,
    image_artifact_resolver: ImageArtifactResolver | None,
) -> tuple[str, dict[str, Any]]:
    filename = image_path.rsplit("/", 1)[-1]
    suffix = ""
    if "." in filename:
        suffix = "." + filename.rsplit(".", 1)[1]
    if image_artifact_resolver is None:
        raise SourceEvidenceClosureError(
            f"image artifact is required to bind image_ref: {image_path}"
        )
    artifact = image_artifact_resolver(artifact_role, image_path)
    actual_sha256 = "sha256:" + hashlib.sha256(artifact.content).hexdigest()
    if (
        artifact.artifact_role != artifact_role
        or artifact.sha256 != actual_sha256
        or artifact.size_bytes != len(artifact.content)
        or re.fullmatch(r"[a-z][a-z0-9_]*", artifact.artifact_role) is None
        or not artifact.media_type.startswith("image/")
    ):
        raise SourceEvidenceClosureError(
            f"image artifact descriptor differs from bytes: {image_path}"
        )
    return (
        f"images/{actual_sha256.removeprefix('sha256:')}{suffix}",
        {
            "artifact_role": artifact.artifact_role,
            "sha256": actual_sha256,
            "size_bytes": len(artifact.content),
            "media_type": artifact.media_type,
        },
    )


def _artifact_locator(element: dict[str, Any]) -> dict[str, Any]:
    locator: dict[str, Any] = {"order_index": element.get("order_index")}
    if element.get("page_no") is not None:
        locator["page_no"] = element.get("page_no")
    if element.get("bbox") is not None:
        locator["bbox"] = element.get("bbox")
    for key in (
        "ir_id",
        "source_item_index",
        "page_span",
    ):
        if element.get(key) is not None:
            locator[key] = element.get(key)
    return locator


def _is_empty_table_element(element: PreparedElement) -> bool:
    if element.kind != "table":
        return False
    table = element.table or {}
    locator = element.artifact_locator or {}
    return (
        not table.get("headers")
        and not table.get("rows")
        and not (element.table_html or "")
        and not any(str(value).strip() for value in element.table_caption)
        and not any(str(value).strip() for value in element.table_footnote)
        and not locator.get("evidence_artifacts")
    )


def _table_to_unit(
    element: PreparedElement,
) -> UnitDraft:
    first = element
    source_grid = first.table or {}
    source_grid_empty = not (source_grid.get("headers") or source_grid.get("rows"))
    if source_grid_empty and str(first.table_html or "").strip():
        raise SourceEvidenceClosureError(
            "table HTML has no reconciled logical grid "
            f"(order_index={first.order_index}, page_no={first.page_no})"
        )

    headers = [str(value) for value in source_grid.get("headers") or []]
    rows = [[str(value) for value in row] for row in source_grid.get("rows") or []]
    merged_cells = [dict(value) for value in source_grid.get("merged_cells") or []]
    detected_unit, unit_projection, unit_conflict = _detect_unit_with_projection(
        first,
    )
    notes = list(first.table_footnote)
    payload = {
        "caption": list(first.table_caption),
        "unit": detected_unit,
        "headers": headers,
        "rows": rows,
        # Row/column spans are table meaning, not provenance.  Keeping them in
        # payload makes content identity change when the logical grid changes.
        "merged_cells": merged_cells,
        "notes": notes,
    }
    if "cells" in source_grid:
        payload["cells"] = [dict(value) for value in source_grid.get("cells") or []]
    if "embedded_media" in source_grid:
        payload["embedded_media"] = [
            dict(value) for value in source_grid.get("embedded_media") or []
        ]
    locator = dict(
        _with_table_payload_projection(
            first.artifact_locator,
            captions=first.table_caption,
            caption_source_indices=_table_caption_source_indices(first),
            notes=notes,
            embedded_media=[
                dict(value)
                for value in source_grid.get("embedded_media") or []
                if isinstance(value, Mapping)
            ],
        )
        or {}
    )
    if unit_projection is not None:
        locator = (
            _with_structured_projection(
                locator,
                {
                    "kind": "derived_field",
                    **unit_projection,
                },
            )
            or locator
        )
    if unit_conflict:
        locator["review_reason"] = "conflicting_table_unit_declarations"
    applicability, applicability_projection = _table_applicability(first)
    if applicability_projection is not None:
        locator = (
            _with_structured_projection(
                locator,
                applicability_projection,
            )
            or locator
        )
    has_grid_content = bool(headers or rows)
    return UnitDraft(
        payload_kind="table",
        payload=payload,
        source_order=first.order_index,
        source_order_phase=first.source_order_phase,
        heading_path=list(first.heading_path),
        section_path=list(first.section_path),
        title=_table_title(first),
        quality_status=(
            "ok"
            if (has_grid_content and not unit_conflict and first.quality_status == "ok")
            else "needs_review"
        ),
        applicability=applicability,
        artifact_locator=locator or None,
    )


def _table_title(element: PreparedElement) -> str | None:
    # MinerU exposes ``table_caption`` as associated payload text, not as a
    # canonical table-title field.  A caption may be a name, unit, checkbox, or
    # other nearby annotation; none of those roles can replace the document
    # hierarchy.  S2 has already supplied the deepest typed heading, if any.
    return element.title


def _table_applicability(
    element: PreparedElement,
) -> tuple[str | None, dict[str, Any] | None]:
    matches: list[tuple[int, str, int, int, str]] = []
    for index, caption in zip(
        _table_caption_source_indices(element),
        element.table_caption,
        strict=True,
    ):
        offset = 0
        for source_line in caption.splitlines(keepends=True):
            line = source_line.rstrip("\r\n")
            marker = line.strip()
            value = (
                content_annotations.classify_marker_line(marker)
                if content_annotations.is_pure_marker_line(marker)
                else None
            )
            if value is not None:
                start = offset + line.find(marker)
                matches.append((index, value, start, start + len(marker), marker))
            offset += len(source_line)
    if len(matches) != 1:
        return None, None
    index, value, start, end, marker = matches[0]
    selector = source_selector(
        element.artifact_locator or {},
        field="table_caption",
        index=index,
        char_span=[start, end],
        value_sha256=source_value_sha256(marker),
    )
    if selector is None:
        raise SourceEvidenceClosureError(
            "table applicability marker lacks a strict source identity"
        )
    return value, {
        "kind": "applicability_marker",
        "source": selector,
        "target_field": "applicability",
        "transform": "applicability_marker.v2",
    }


def _detect_unit_with_projection(
    element: PreparedElement,
) -> tuple[str | None, dict[str, Any] | None, bool]:
    """Project a measurement unit only from table-associated source fields.

    MinerU's typed caption/footnote relation is structural evidence.  A nearby
    text carrier, a header cell, or a currency declaration is not proof of a
    table-wide measurement unit and therefore remains searchable raw evidence
    without populating ``payload.unit``.
    """

    candidates: list[tuple[str, str, int | None]] = [
        *(
            (caption, "table_caption", source_index)
            for source_index, caption in zip(
                _table_caption_source_indices(element),
                element.table_caption,
                strict=True,
            )
        ),
        *(
            (note, "table_note", index)
            for index, note in enumerate(element.table_footnote)
        ),
    ]
    parsed: list[tuple[str, str, int | None, int, int, str]] = []
    for candidate, source_field, source_index in candidates:
        offset = 0
        for source_line in candidate.splitlines(keepends=True):
            line = source_line.rstrip("\r\n")
            selected_line = line.strip()
            for label, value in content_annotations.parse_unit_declarations(
                selected_line
            ):
                if not re.sub(r"\s+", "", label).endswith("单位"):
                    continue
                start = offset + line.find(selected_line)
                parsed.append(
                    (
                        re.sub(r"\s+", "", value),
                        source_field,
                        source_index,
                        start,
                        start + len(selected_line),
                        selected_line,
                    )
                )
            offset += len(source_line)
    values = {value for value, *_rest in parsed}
    if not parsed:
        return None, None, False
    if len(values) != 1:
        return None, None, True

    value, source_field, source_index, start, end, selected_line = parsed[0]
    selector = source_selector(
        element.artifact_locator or {},
        field=source_field,
        index=source_index,
        char_span=[start, end],
        value_sha256=source_value_sha256(selected_line),
    )
    if selector is None:
        raise SourceEvidenceClosureError(
            "table unit declaration lacks a strict source identity"
        )
    projection: dict[str, Any] = {
        "source": selector,
        "target_field": "payload.unit",
        "transform": "unit_declaration.v2",
    }
    return value, projection, False


def _final_quality_status(unit: UnitDraft) -> str:
    if unit.quality_status == "unusable":
        return "unusable"
    if unit.quality_status == "needs_review":
        return "needs_review"
    if _main_text_is_unusable(unit):
        return "unusable"
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
    return total > 0 and bad / total > content_annotations.GIBBERISH_RATIO_MAX


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
    )


def _part_text(part: dict[str, Any]) -> str:
    kind = str(part.get("kind", "text"))
    if kind == "table":
        return _table_cells_text(part)
    if kind == "image":
        return " ".join(
            str(value)
            for field in ("caption", "content", "notes")
            for value in _payload_text_values(part.get(field))
            if value
        )
    if "text" in part:
        return str(part.get("text") or "")
    return " ".join(
        str(value)
        for value in _payload_text_values(part.get("caption"))
        if value
    )


def _payload_text_values(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value] if value is not None else []


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
