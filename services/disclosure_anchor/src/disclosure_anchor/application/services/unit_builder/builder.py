"""Pure S1-S7 document_unit builder stages."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import hashlib
import re
import unicodedata
from typing import Any, Callable, Iterable

from disclosure_anchor.application.contracts import content_annotations
from disclosure_anchor.application.services.unit_builder import retrieval_routing
from disclosure_anchor.application.contracts.document_structure import (
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
    text: str | None = None
    raw_kind: str | None = None
    page_no: int | None = None
    table: dict[str, Any] | None = None
    table_caption: list[str] = field(default_factory=list)
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
    notes: list[str],
    embedded_media: list[dict[str, Any]],
) -> dict[str, Any] | None:
    output = dict(locator or {})
    selector = _required_source_selector(output, field="table")
    graph = _projection_graph(output)
    projection_sources = [selector]
    projection_sources.extend(
        _required_source_selector(output, field="table_caption", index=index)
        for index in range(len(captions))
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
    heading_anchors = _proven_headings(
        structure_proof,
        raw_by_index=raw_by_index,
    )
    headings = [heading for heading in heading_anchors if heading.propagates]
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
        path = paths[owner.node_id] if owner is not None else ()
        if (
            source_index in heading_text_sources
            and element.kind == "text"
            and element.payload is None
        ):
            continue
        if not _is_empty_table_element(element):
            represented.update(item.node_id for item in path)
        titles = [item.title for item in path]
        placed.append(
            replace(
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
        )

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
    return sorted(placed, key=lambda item: item.order_index)


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
        if _carries_native_context(unit):
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
    for native_unit in native_units:
        s1.stats.generated_by_kind[native_unit.payload_kind] += 1
    units = sorted(
        [*text_units, *table_units, *native_units], key=_unit_sort_key
    )
    grouped = _group_structural_units(
        units,
        document_title=(
            str(normalized_ir["title"])
            if isinstance(normalized_ir.get("title"), str)
            and str(normalized_ir["title"]).strip()
            else None
        ),
        stats=s1.stats,
    )
    grouped = _attribute_native_units(grouped)
    grouped = _flag_coverage_gap_owners(grouped)
    grouped = _suppress_punctuation_only_natives(grouped, stats=s1.stats)
    return (
        s7_finalize_units(
            grouped,
            filing_type=filing_type,
            stats=s1.stats,
        ),
        s1.stats,
    )


def _native_anchor_source_index(unit: UnitDraft) -> int | None:
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
    predecessor = context.get("predecessor")
    if isinstance(predecessor, Mapping):
        ref = predecessor.get("source")
        if isinstance(ref, Mapping):
            index = ref.get("source_item_index")
            if isinstance(index, int) and not isinstance(index, bool):
                return index
    return None


def _attribute_native_units(units: list[UnitDraft]) -> list[UnitDraft]:
    """Carry each anchor's published section onto its native recovery.

    Attribution is transitive proof, not invention: the anchor relation is
    word-order proven and the anchor's published unit already carries its
    proven section path — including the container that swallowed a page
    frame the recovery happens to trail. It rides inside the physical
    context because the public heading_path contract demands projections
    and proof ancestry that a recovery deliberately does not claim.
    """

    paths_by_source: dict[int, list[str]] = {}
    for unit in units:
        if _carries_native_context(unit):
            continue
        for source_index in _unit_source_item_indices_for_attribution(unit):
            paths_by_source.setdefault(source_index, list(unit.heading_path))
    output: list[UnitDraft] = []
    for unit in units:
        if not _carries_native_context(unit):
            output.append(unit)
            continue
        anchor_index = _native_anchor_source_index(unit)
        anchor_path = (
            paths_by_source.get(anchor_index, [])
            if anchor_index is not None
            else []
        )
        locator = dict(unit.artifact_locator or {})
        graph = dict(locator.get("source_projection") or {})
        context = dict(graph.get("physical_context") or {})
        context["anchor_heading_path"] = list(anchor_path)
        graph["physical_context"] = context
        locator["source_projection"] = graph
        output.append(replace(unit, artifact_locator=locator))
    return output


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
        if _carries_native_context(unit):
            parts = (
                unit.payload.get("parts")
                if isinstance(unit.payload, dict)
                else None
            )
            texts = (
                [
                    part.get("text")
                    for part in parts
                    if isinstance(part, dict) and part.get("kind") == "text"
                ]
                if isinstance(parts, list)
                else []
            )
            joined = "".join(text for text in texts if isinstance(text, str))
            if (
                isinstance(parts, list)
                and len(texts) == len(parts)
                and punctuation_only_text(joined)
            ):
                stats.punctuation_only_native_runs += 1
                continue
        output.append(unit)
    return output


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


def _unit_sort_key(unit: UnitDraft) -> tuple[int, int, int, int]:
    if unit.native_order_anchor is not None:
        anchor_order, page_idx, span_start = unit.native_order_anchor
        return (anchor_order, 1, page_idx, span_start)
    return (unit.source_order, 0, 0, 0)


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
    for index, caption in enumerate(element.table_caption):
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
            (caption, "table_caption", index)
            for index, caption in enumerate(element.table_caption)
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
    return str(part.get("text") or "")


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
