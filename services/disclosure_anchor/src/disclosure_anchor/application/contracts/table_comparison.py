"""Shared page-local table comparison over reader-visible projections.

One implementation of the comparison mechanics — model artifact parsing,
the MinerU inline-equation serialization stage, the strict page-local
bijection, and the hashed receipt root — consumed by the producer's
reconciler (through the adapter) and re-run by the independent audit from
hash-bound raw bytes. What is never shared between them is a result: each
side parses its own inputs and derives its own projections, matching, and
root.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html import unescape
import json
import math
import re
from typing import Any

from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    TABLE_COMPARISON_CONTRACT,
)
from disclosure_anchor.application.contracts.table_projection import (
    TableProjectionError,
    VisibleTableProjection,
    collect_table_media_sources,
    project_table_html,
)

MODEL_BBOX_MAX_DELTA = 3.0
_RECEIPT_DOMAIN_TAG = "disclosure-anchor.table-projection-receipt.v1"
_MODEL_IMAGE_DATA_URI_RE = re.compile(
    r"^data:image/[A-Za-z0-9.+-]+;base64,(?P<payload>[A-Za-z0-9+/]*={0,2})$"
)
_MINERU_INLINE_EQUATION_RE = re.compile(r"<eq>(.*?)</eq>", re.DOTALL)
_MINERU_EQ_LIKE_TAG_RE = re.compile(r"</?eq\b[^>]*(?:>|$)", re.IGNORECASE)
_SUPPORTED_MINERU_INLINE_DELIMITERS = ("$", "$")


class TableComparisonError(ValueError):
    """The page-local table comparison cannot be closed."""


@dataclass(frozen=True)
class ComparableTable:
    index: int
    page_idx: int
    bbox: tuple[float, float, float, float]
    body: VisibleTableProjection
    body_sha256: str


def comparable_table(
    *,
    index: int,
    page_idx: int,
    bbox: tuple[float, float, float, float],
    html: str,
    label: str,
) -> ComparableTable:
    try:
        body = project_table_html(html).body()
    except TableProjectionError as exc:
        raise TableComparisonError(
            f"{label} cannot be projected: {exc}"
        ) from exc
    return ComparableTable(
        index=index,
        page_idx=page_idx,
        bbox=bbox,
        body=body,
        body_sha256=body.sha256(),
    )


def mineru_exported_table_html(html: str, *, label: str) -> str:
    """Apply MinerU 3.4's bounded model-to-content equation serialization.

    ``*_model.json`` keeps inline table formulas as ``<eq>...</eq>`` while
    the official content exporter writes the configured, registered ``$``
    delimiters. This mirrors that provider stage exactly; it does not
    reinterpret dollar-delimited content or repair malformed model markup.
    """

    markers = tuple(_MINERU_EQ_LIKE_TAG_RE.finditer(html))
    depth = 0
    for marker in markers:
        token = marker.group(0)
        if token not in {"<eq>", "</eq>"}:
            raise TableComparisonError(
                f"{label} has malformed inline-equation markup"
            )
        if token == "<eq>":
            if depth:
                raise TableComparisonError(
                    f"{label} has nested inline-equation markup"
                )
            depth = 1
        else:
            if not depth:
                raise TableComparisonError(
                    f"{label} has malformed inline-equation markup"
                )
            depth = 0
    if depth:
        raise TableComparisonError(
            f"{label} has unclosed inline-equation markup"
        )
    left, right = _SUPPORTED_MINERU_INLINE_DELIMITERS
    return _MINERU_INLINE_EQUATION_RE.sub(
        lambda match: f" {left}{unescape(match.group(1))}{right} ",
        html,
    )


def model_comparable_tables(
    model_bytes: bytes,
) -> tuple[list[ComparableTable], str]:
    """Parse a raw MinerU model artifact into comparable tables."""

    model_hash = "sha256:" + hashlib.sha256(model_bytes).hexdigest()
    try:
        payload = json.loads(model_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TableComparisonError(
            f"invalid MinerU model JSON: {exc}"
        ) from exc
    if not isinstance(payload, list):
        raise TableComparisonError("unsupported MinerU model table schema")
    if all(isinstance(page, dict) for page in payload):
        return _pipeline_model_tables(payload), model_hash
    if all(isinstance(page, list) for page in payload):
        return _vlm_model_tables(payload), model_hash
    raise TableComparisonError("unsupported MinerU model table schema")


def _pipeline_model_tables(payload: list[Any]) -> list[ComparableTable]:
    tables: list[ComparableTable] = []
    for page in payload:
        page_info = page.get("page_info")
        detections = page.get("layout_dets")
        if not isinstance(page_info, dict) or not isinstance(detections, list):
            raise TableComparisonError(
                "unsupported MinerU pipeline model schema"
            )
        page_idx = _page_index(page_info.get("page_no"))
        width = _positive_float(page_info.get("width"))
        height = _positive_float(page_info.get("height"))
        if page_idx is None or width is None or height is None:
            raise TableComparisonError(
                "unsupported MinerU pipeline page geometry"
            )
        for detection in detections:
            if not isinstance(detection, dict):
                raise TableComparisonError(
                    "unsupported MinerU pipeline detection schema"
                )
            if detection.get("label") != "table":
                continue
            raw_bbox = _bbox(detection.get("bbox"))
            html = detection.get("html")
            if raw_bbox is None or not isinstance(html, str) or not html.strip():
                raise TableComparisonError(
                    "MinerU pipeline model table lacks bbox or HTML"
                )
            bbox = _normalized_bbox(
                [
                    raw_bbox[0] / width * 1000,
                    raw_bbox[1] / height * 1000,
                    raw_bbox[2] / width * 1000,
                    raw_bbox[3] / height * 1000,
                ]
            )
            if bbox is None:
                raise TableComparisonError(
                    "MinerU pipeline model table bbox is invalid"
                )
            index = len(tables)
            label = f"model table {index}"
            exported = mineru_exported_table_html(html, label=label)
            _validate_model_media_sources(exported)
            tables.append(
                comparable_table(
                    index=index,
                    page_idx=page_idx,
                    bbox=bbox,
                    html=exported,
                    label=label,
                )
            )
    return tables


def _vlm_model_tables(payload: list[Any]) -> list[ComparableTable]:
    tables: list[ComparableTable] = []
    for page_idx, page in enumerate(payload):
        for item in page:
            if not isinstance(item, dict):
                raise TableComparisonError(
                    "unsupported MinerU VLM model schema"
                )
            if item.get("type") != "table":
                continue
            raw_bbox = _bbox(item.get("bbox"))
            html = item.get("content")
            if (
                raw_bbox is None
                or any(
                    coordinate < 0 or coordinate > 1
                    for coordinate in raw_bbox
                )
                or not isinstance(html, str)
                or not html.strip()
            ):
                raise TableComparisonError(
                    "MinerU VLM model table lacks normalized bbox or HTML"
                )
            bbox = _normalized_bbox(
                [coordinate * 1000 for coordinate in raw_bbox]
            )
            if bbox is None:
                raise TableComparisonError(
                    "MinerU VLM model table bbox is invalid"
                )
            index = len(tables)
            label = f"model table {index}"
            exported = mineru_exported_table_html(html, label=label)
            _validate_model_media_sources(exported)
            tables.append(
                comparable_table(
                    index=index,
                    page_idx=page_idx,
                    bbox=bbox,
                    html=exported,
                    label=label,
                )
            )
    return tables


def _validate_model_media_sources(html: str) -> None:
    """Model media stay opaque to equality but must be sound artifacts."""

    import base64
    import binascii

    for source in collect_table_media_sources(html):
        match = _MODEL_IMAGE_DATA_URI_RE.fullmatch(source)
        if match is None:
            raise TableComparisonError(
                "MinerU model table embedded image is not a supported "
                "data URI"
            )
        try:
            payload = base64.b64decode(match.group("payload"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise TableComparisonError(
                "MinerU model table embedded image has invalid base64 bytes"
            ) from exc
        if not payload:
            raise TableComparisonError(
                "MinerU model table embedded image has empty bytes"
            )


def prove_unique_bijection(
    content_tables: list[ComparableTable],
    model_tables: list[ComparableTable],
) -> str:
    """Prove the strict page-local bijection; return its receipt root."""

    owners: dict[int, int] = {}
    matches: list[dict[str, Any]] = []
    for content in content_tables:
        candidates = [
            model
            for model in model_tables
            if model.page_idx == content.page_idx
            and _bbox_delta(model.bbox, content.bbox) <= MODEL_BBOX_MAX_DELTA
            and model.body_sha256 == content.body_sha256
        ]
        if len(candidates) != 1:
            raise TableComparisonError(
                "MinerU content table "
                f"{content.index} has {len(candidates)} exact page-local "
                "model matches"
            )
        model_index = candidates[0].index
        if model_index in owners:
            raise TableComparisonError(
                "MinerU model table "
                f"{model_index} ambiguously matches content tables "
                f"{owners[model_index]} and {content.index}"
            )
        owners[model_index] = content.index
        matches.append(
            {
                "content_index": content.index,
                "model_index": model_index,
                "projection_sha256": content.body_sha256,
            }
        )
    unmatched = sorted(set(range(len(model_tables))) - owners.keys())
    if unmatched:
        raise TableComparisonError(
            "MinerU model tables are not represented by content_list: "
            + ", ".join(str(index) for index in unmatched)
        )
    return projection_receipt_root(matches)


def projection_receipt_root(matches: list[dict[str, Any]]) -> str:
    """Hash the complete matching so any silent re-pairing is evident."""

    preimage = json.dumps(
        {
            "domain": _RECEIPT_DOMAIN_TAG,
            "comparison_contract": TABLE_COMPARISON_CONTRACT,
            "matches": sorted(
                matches,
                key=lambda match: match["content_index"],
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RawContentTable:
    """One content-artifact table item's comparison-relevant raw facts."""

    index: int
    page_idx: int
    bbox: tuple[float, float, float, float]
    html: str
    captions: tuple[str, ...]
    footnotes: tuple[str, ...]


@dataclass(frozen=True)
class ReplayedTableComparison:
    model_hash: str
    content_tables: int
    model_tables: int
    projection_root: str


def content_table_html(item: dict[str, Any]) -> str | None:
    """Resolve the closed table-HTML alias family without length picking."""

    present: list[str] = []
    for field in ("table_body", "table_html"):
        value = item.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise TableComparisonError(
                f"MinerU {field} must be text or null"
            )
        present.append(value)
    nonempty = [value for value in present if value]
    if not nonempty:
        return None
    canonical = nonempty[0]
    if any(value.strip() != canonical.strip() for value in nonempty[1:]):
        raise TableComparisonError(
            "MinerU table_body/table_html aliases conflict"
        )
    return canonical


def _string_list(item: dict[str, Any], field: str) -> tuple[str, ...]:
    value = item.get(field)
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(part, str) for part in value
    ):
        raise TableComparisonError(
            f"MinerU {field} must be a list of strings"
        )
    return tuple(value)


def raw_content_tables(content_list_bytes: bytes) -> list[RawContentTable]:
    """Extract every table item's raw comparison facts from the artifact."""

    try:
        payload = json.loads(content_list_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TableComparisonError(
            f"invalid MinerU content_list JSON: {exc}"
        ) from exc
    if not isinstance(payload, list) or any(
        not isinstance(item, dict) for item in payload
    ):
        raise TableComparisonError(
            "MinerU content_list must be a list of objects"
        )
    tables: list[RawContentTable] = []
    for index, item in enumerate(payload):
        if str(item.get("type") or "") != "table":
            continue
        page_idx = _page_index(item.get("page_idx"))
        bbox = _normalized_bbox(item.get("bbox"))
        html = content_table_html(item)
        if page_idx is None or bbox is None:
            raise TableComparisonError(
                f"MinerU content table {index} lacks a valid page-local bbox"
            )
        if not isinstance(html, str) or not html.strip():
            raise TableComparisonError(
                f"MinerU content table {index} has empty HTML"
            )
        tables.append(
            RawContentTable(
                index=index,
                page_idx=page_idx,
                bbox=bbox,
                html=html,
                captions=_string_list(item, "table_caption"),
                footnotes=_string_list(item, "table_footnote"),
            )
        )
    return tables


def replay_page_local_table_comparison(
    *,
    model_bytes: bytes,
    content_list_bytes: bytes,
) -> ReplayedTableComparison:
    """Re-run the whole comparison from raw artifact bytes.

    This is the audit's path: it never consumes a producer projection,
    candidate list, or match result — only the hash-bound raw inputs.
    """

    content = [
        comparable_table(
            index=raw.index,
            page_idx=raw.page_idx,
            bbox=raw.bbox,
            html=raw.html,
            label=f"content table {raw.index}",
        )
        for raw in raw_content_tables(content_list_bytes)
    ]
    model_tables, model_hash = model_comparable_tables(model_bytes)
    projection_root = prove_unique_bijection(content, model_tables)
    return ReplayedTableComparison(
        model_hash=model_hash,
        content_tables=len(content),
        model_tables=len(model_tables),
        projection_root=projection_root,
    )


def _bbox_delta(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(abs(left[index] - right[index]) for index in range(4))


def _normalized_bbox(value: Any) -> tuple[float, float, float, float] | None:
    bbox = _bbox(value)
    if (
        bbox is None
        or min(bbox) < 0
        or max(bbox) > 1000
        or bbox[0] >= bbox[2]
        or bbox[1] >= bbox[3]
    ):
        return None
    return bbox


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(isinstance(part, bool) for part in value):
        return None
    try:
        parsed = tuple(float(part) for part in value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(part) for part in parsed):
        return None
    return parsed  # type: ignore[return-value]


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _page_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


__all__ = [
    "MODEL_BBOX_MAX_DELTA",
    "ComparableTable",
    "RawContentTable",
    "ReplayedTableComparison",
    "content_table_html",
    "raw_content_tables",
    "replay_page_local_table_comparison",
    "TableComparisonError",
    "comparable_table",
    "mineru_exported_table_html",
    "model_comparable_tables",
    "projection_receipt_root",
    "prove_unique_bijection",
]
