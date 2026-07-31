"""Map MinerU content-list artifacts to parser-neutral NormalizedIR."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import math
from typing import Any

from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    MINERU_CONTENT_FIELDS_BY_KIND,
    MINERU_SUPPORTED_RAW_KINDS,
    mineru_code_payload,
    mineru_list_items,
    mineru_provider_item_sha256,
    mineru_text_sequence,
    resolved_image_path,
    resolved_table_html,
    resolved_text,
)
from disclosure_anchor.adapters.parsers.mineru.geometry import is_page_index
from disclosure_anchor.adapters.parsers.mineru.table_html_structure import (
    ParsedHtmlTable,
    table_media_artifact_role,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    CURRENT_NORMALIZED_IR_VERSION,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from disclosure_anchor.application.contracts.visual_semantics import (
    VisualSemanticDisposition,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


MinerUParserInfo = ParserTargetIdentity


def _page_no(item: dict[str, Any]) -> int | None:
    page_idx = item.get("page_idx")
    return page_idx + 1 if is_page_index(page_idx) else None


def _parsed_pages(
    items: list[dict[str, Any]],
    *,
    start_page: int | None,
    end_page: int | None,
) -> dict[str, Any]:
    """Describe the requested physical page range without inventing blank pages.

    MinerU CLI page options and ``page_idx`` are zero-based.  Content lists
    omit pages without emitted blocks, so observed element pages can fill an
    unspecified bound but cannot prove that a full-PDF parse ended there.
    """

    for label, page in (("start_page", start_page), ("end_page", end_page)):
        if page is not None and not is_page_index(page):
            raise ParserOutputContractError(
                f"MinerU {label} must be a non-negative integer or null"
            )
    if start_page is not None and end_page is not None and start_page > end_page:
        raise ParserOutputContractError(
            "MinerU start_page must not exceed end_page"
        )
    page_numbers = [page for item in items if (page := _page_no(item)) is not None]
    return {
        "start_page_no": (
            start_page + 1
            if start_page is not None
            else (min(page_numbers) if page_numbers else None)
        ),
        "end_page_no": (
            end_page + 1
            if end_page is not None
            else (max(page_numbers) if page_numbers else None)
        ),
        "full_pdf": start_page is None and end_page is None,
    }


def _unmapped_provider_fields(
    item: dict[str, Any],
    *,
    raw_kind: str,
) -> list[str]:
    """Reject every provider field outside the selected typed schema."""

    accepted = MINERU_CONTENT_FIELDS_BY_KIND[raw_kind]
    return sorted(key for key in item if key not in accepted)


def _kind_from_raw(raw_kind: str, item: dict[str, Any]) -> str:
    raw_heading_level = item.get("text_level")
    if raw_heading_level is not None and (
        not isinstance(raw_heading_level, int)
        or isinstance(raw_heading_level, bool)
        or raw_heading_level < 0
    ):
        raise ParserOutputContractError(
            "MinerU text_level must be a non-negative integer or null"
        )
    if raw_kind == "text":
        return "text"
    if raw_kind in {"ref_text", "phonetic", "aside_text", "page_footnote"}:
        # MinerU emits these from its discarded-layout lane, but both are
        # source text: aside_text is supplementary margin content and a page
        # footnote can define or qualify nearby facts.  ``raw_kind`` retains
        # the auxiliary role for later retrieval ranking.
        return "text"
    if raw_kind in {"header", "page_number", "footer"}:
        return "page_furniture"
    if raw_kind in {"table", "image", "equation"}:
        return raw_kind
    if raw_kind == "chart":
        # NormalizedIR has one parser-neutral image-backed visual kind;
        # ``raw_kind=chart`` preserves the provider distinction while keeping
        # the chart image and recognized data in the same source atom.
        return "image"
    if raw_kind == "list":
        mineru_list_items(item)
        # MinerU 3.x emits prose lists as ordered string items.  NormalizedIR
        # has no parser-specific list kind, so preserve their exact order and
        # text as a neutral text element while retaining raw_kind="list".
        return "text"
    if raw_kind == "code":
        return "text"
    raise ParserOutputContractError(
        f"unsupported MinerU content-list type: {raw_kind!r}"
    )


def _finite_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(float(item))
            for item in value
        )
    ):
        return None
    bbox = (
        float(value[0]),
        float(value[1]),
        float(value[2]),
        float(value[3]),
    )
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        return None
    return bbox


class MinerUToNormalizedIRMapper:
    """Convert the stable MinerU content_list shape into NormalizedIR."""

    def map_content_list(
        self,
        *,
        content_list: list[dict[str, Any]],
        identity_content_list: Sequence[Mapping[str, Any]] | None = None,
        parser_info: MinerUParserInfo,
        document_metadata: dict[str, Any],
        structure_proof: Mapping[str, Any],
        source_pdf_sha256: str,
        source_pdf_page_count: int,
        table_structures: Mapping[int, ParsedHtmlTable] | None = None,
        table_role_values: Mapping[tuple[int, str], tuple[str, ...]] | None = None,
        visual_semantics_by_source: Mapping[
            int, VisualSemanticDisposition
        ] | None = None,
        visual_semantics_by_table_media: Mapping[
            tuple[int, int], VisualSemanticDisposition
        ] | None = None,
        parser_artifacts: Mapping[str, Any] | None = None,
        start_page: int | None = None,
        end_page: int | None = None,
    ) -> dict[str, Any]:
        document_id = str(document_metadata["document_id"])
        identity_items = identity_content_list or content_list
        if len(identity_items) != len(content_list):
            raise ParserOutputContractError(
                "MinerU canonical and identity item counts differ"
            )
        elements = [
            self._map_item(
                item=item,
                identity_item=identity_item,
                index=index,
                table_structures=table_structures or {},
                table_role_values=table_role_values or {},
                visual_semantics_by_source=visual_semantics_by_source or {},
                visual_semantics_by_table_media=(
                    visual_semantics_by_table_media or {}
                ),
                require_visual_semantics=(
                    visual_semantics_by_source is not None
                    or visual_semantics_by_table_media is not None
                ),
            )
            for index, (item, identity_item) in enumerate(
                zip(content_list, identity_items, strict=True)
            )
        ]
        return {
            "contract_version": CURRENT_NORMALIZED_IR_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "document_id": document_id,
            "source_pdf": str(document_metadata.get("source_pdf", "")),
            "source_pdf_sha256": source_pdf_sha256,
            "source_pdf_page_count": source_pdf_page_count,
            "title": document_metadata.get("title"),
            "parser": parser_info.to_payload(),
            "parser_artifacts": dict(parser_artifacts or {}),
            "parsed_pages": _parsed_pages(
                content_list,
                start_page=start_page,
                end_page=end_page,
            ),
            "elements": elements,
            "structure_proof": dict(structure_proof),
        }

    def _map_item(
        self,
        *,
        item: dict[str, Any],
        identity_item: Mapping[str, Any],
        index: int,
        table_structures: Mapping[int, ParsedHtmlTable],
        table_role_values: Mapping[tuple[int, str], tuple[str, ...]],
        visual_semantics_by_source: Mapping[int, VisualSemanticDisposition],
        visual_semantics_by_table_media: Mapping[
            tuple[int, int], VisualSemanticDisposition
        ],
        require_visual_semantics: bool,
    ) -> dict[str, Any]:
        raw_type = item.get("type")
        if (
            not isinstance(raw_type, str)
            or not raw_type
            or raw_type not in MINERU_SUPPORTED_RAW_KINDS
        ):
            raise ParserOutputContractError(
                f"unsupported MinerU content-list type: {raw_type!r}"
            )
        raw_kind = raw_type
        kind = _kind_from_raw(raw_kind, item)
        element: dict[str, Any] = {
            "ir_id": f"ir_{index:04d}",
            "kind": kind,
            "raw_kind": raw_kind,
            "order_index": index,
            "source_item_index": index,
            "source_item_sha256": mineru_provider_item_sha256(
                dict(identity_item)
            ),
        }
        if "page_idx" in item:
            page_idx = item["page_idx"]
            if not is_page_index(page_idx):
                raise ParserOutputContractError(
                    "MinerU page_idx must be a non-negative integer"
                )
            element["page_idx"] = page_idx
        if "bbox" in item:
            bbox = _finite_bbox(item["bbox"])
            if bbox is None or min(bbox) < 0 or max(bbox) > 1000:
                raise ParserOutputContractError(
                    "MinerU bbox must be a finite positive rectangle in 0..1000 space"
                )
            element["bbox"] = list(item["bbox"])
        if "text_level" in item:
            element["text_level"] = item["text_level"]
        if (page_no := _page_no(item)) is not None:
            element["page_no"] = page_no
        code_payload = mineru_code_payload(item) if raw_kind == "code" else None
        text = (
            code_payload[4]
            if code_payload is not None
            else resolved_text(
                item,
                include_content=raw_kind not in {"table", "list", "code"},
            )
        )
        if text is not None:
            element["text"] = text
        elif raw_kind == "list":
            list_items = mineru_list_items(item)
            element["list_items"] = list_items
            if any(list_item.strip() for list_item in list_items):
                element["text"] = "\n".join(list_items)
        if raw_kind == "list" and "sub_type" in item:
            list_subtype = item["sub_type"]
            if list_subtype is not None and not isinstance(list_subtype, str):
                raise ParserOutputContractError(
                    "MinerU list sub_type must be text or null"
                )
            if list_subtype:
                element["list_subtype"] = list_subtype
        if code_payload is not None:
            body, captions, footnotes, subtype, _visible_text = code_payload
            element["code_body"] = body
            element["code_caption"] = captions
            element["code_footnote"] = footnotes
            if subtype is not None:
                element["code_subtype"] = subtype
        if raw_kind == "table":
            caption_override = table_role_values.get((index, "table_caption"))
            footnote_override = table_role_values.get((index, "table_footnote"))
            element["table_caption"] = (
                list(caption_override)
                if caption_override is not None
                else mineru_text_sequence(
                    item.get("table_caption"),
                    field="table_caption",
                )
            )
            element["table_footnote"] = (
                list(footnote_override)
                if footnote_override is not None
                else mineru_text_sequence(
                    item.get("table_footnote"),
                    field="table_footnote",
                )
            )
            table_html = resolved_table_html(item) or ""
            if not table_html.strip():
                raise ParserOutputContractError(
                    "MinerU table requires non-empty page-local HTML"
                )
            if resolved_image_path(item) is None:
                raise ParserOutputContractError(
                    "MinerU table requires an image crop path"
                )
            element["table_html"] = table_html
            structure = table_structures.get(index)
            if structure is None:
                raise ParserOutputContractError(
                    "MinerU table structure was not materialized from the "
                    f"content artifact: {index}"
                )
            table: dict[str, Any] = {
                "headers": list(structure.headers),
                "rows": [list(row) for row in structure.rows],
                "cells": [
                    {
                        "row": cell.row,
                        "col": cell.col,
                        "rowspan": cell.rowspan,
                        "colspan": cell.colspan,
                        "text": cell.text,
                        "is_header": cell.is_header,
                    }
                    for cell in structure.cells
                ],
                "embedded_media": [
                    {
                        "occurrence_index": media.occurrence_index,
                        "cell_media_index": media.cell_media_index,
                        "row": media.row,
                        "col": media.col,
                        "rowspan": media.rowspan,
                        "colspan": media.colspan,
                        "image_path": media.image_path,
                        "artifact_role": table_media_artifact_role(
                            index,
                            media.occurrence_index,
                        ),
                        **(
                            {"alt_text": media.alt_text}
                            if media.alt_text is not None
                            else {}
                        ),
                        **(
                            {"title_text": media.title_text}
                            if media.title_text is not None
                            else {}
                        ),
                        **_table_semantic_fields(
                            visual_semantics_by_table_media.get(
                                (index, media.occurrence_index)
                            ),
                            required=require_visual_semantics,
                        ),
                    }
                    for media in structure.embedded_media
                ],
            }
            if structure.merged_cells:
                table["merged_cells"] = [
                    {
                        "row": row,
                        "col": col,
                        "rowspan": rowspan,
                        "colspan": colspan,
                    }
                    for row, col, rowspan, colspan in structure.merged_cells
                ]
            element["table"] = table
        if raw_kind in {"image", "chart"}:
            caption_field = (
                "chart_caption" if raw_kind == "chart" else "image_caption"
            )
            footnote_field = (
                "chart_footnote" if raw_kind == "chart" else "image_footnote"
            )
            element["image_caption"] = mineru_text_sequence(
                item.get(caption_field),
                field=caption_field,
            )
            element["image_footnote"] = mineru_text_sequence(
                item.get(footnote_field),
                field=footnote_field,
            )
            if str(element.get("text") or "").strip():
                element["text_provenance"] = (
                    "generated_annotation"
                    if raw_kind == "image"
                    else "visual_recognition"
                )
            if "sub_type" in item:
                subtype = item["sub_type"]
                if subtype is not None and not isinstance(subtype, str):
                    raise ParserOutputContractError(
                        "MinerU visual sub_type must be text or null"
                    )
                if subtype:
                    element["visual_subtype"] = subtype
        if image_path := resolved_image_path(item):
            element["image_path"] = image_path
        if raw_kind == "equation" and "text_format" in item:
            text_format = item["text_format"]
            if not isinstance(text_format, str) or not text_format:
                raise ParserOutputContractError(
                    "MinerU equation text_format must be non-empty text"
                )
            element["text_format"] = text_format
        visual_disposition = visual_semantics_by_source.get(index)
        if raw_kind in {"image", "chart"} or (
            raw_kind == "equation" and resolved_image_path(item)
        ):
            element.update(
                _visual_semantic_fields(
                    visual_disposition,
                    required=require_visual_semantics,
                    provider_text=element.get("text"),
                )
            )
        unmapped_fields = _unmapped_provider_fields(
            item,
            raw_kind=raw_kind,
        )
        if unmapped_fields:
            raise ParserOutputContractError(
                "MinerU content carrier has unmapped payload fields: "
                f"raw_kind={raw_kind!r}, fields={unmapped_fields}"
            )
        return element


def _visual_semantic_fields(
    disposition: VisualSemanticDisposition | None,
    *,
    required: bool,
    provider_text: object = None,
) -> dict[str, str]:
    if disposition is None:
        if required:
            raise ParserOutputContractError(
                "visual occurrence has no closed semantic disposition"
            )
        return {}
    if disposition.status != "semantic_text":
        return {}
    assert disposition.semantic_text is not None
    assert disposition.semantic_text_sha256 is not None
    if disposition.semantic_origin == "provider_visual_text":
        if provider_text != disposition.semantic_text:
            raise ParserOutputContractError(
                "visual disposition differs from provider semantic text"
            )
        return {}
    return {
        "visual_semantic_text": disposition.semantic_text,
    }


def _table_semantic_fields(
    disposition: VisualSemanticDisposition | None,
    *,
    required: bool,
) -> dict[str, str]:
    fields = _visual_semantic_fields(disposition, required=required)
    text = fields.get("visual_semantic_text")
    return {"semantic_text": text} if text is not None else {}
