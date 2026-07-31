"""Prove page-local closure between MinerU content and model tables.

Production disables MinerU's cross-page table merge.  Each canonical
``content_list`` table must therefore describe exactly one physical page and
must have one uniquely equal table in ``*_model.json`` on that same page.
This module validates that closed relation; it never repairs or annotates the
provider payload.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
from html import unescape
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any

from disclosure_anchor.adapters.parsers.mineru.geometry import (
    bbox_delta,
    is_page_index,
)
from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    resolved_image_path,
    resolved_table_html,
)
from disclosure_anchor.adapters.parsers.mineru.table_html_structure import (
    HtmlTableMedia,
    ParsedHtmlTable,
    TableHtmlStructureError,
    parse_table_html_structure,
    table_media_artifact_role,
)
from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    TABLE_RECONCILIATION_ALGORITHM_VERSION,
    validate_table_reconciliation_diagnostics,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


MODEL_BBOX_MAX_DELTA = 3.0
_MediaIdentity = tuple[str, str | None, str | None]
_CellIdentity = tuple[
    int,
    int,
    str,
    bool,
    int,
    int,
    tuple[_MediaIdentity, ...],
]
_TableIdentity = tuple[_CellIdentity, ...]
_MINERU_INLINE_EQUATION_RE = re.compile(r"<eq>(.*?)</eq>", re.DOTALL)
_MINERU_EQ_LIKE_TAG_RE = re.compile(r"</?eq\b[^>]*(?:>|$)", re.IGNORECASE)
_SUPPORTED_MINERU_INLINE_DELIMITERS = ("$", "$")
_MODEL_IMAGE_DATA_URI_RE = re.compile(
    r"^data:image/[A-Za-z0-9.+-]+;base64,(?P<payload>[A-Za-z0-9+/]*={0,2})$"
)


@dataclass(frozen=True)
class TableReconciliationStats:
    model_hash: str
    content_tables: int
    model_tables: int
    matched_tables: int
    page_local_closed: bool = True
    algorithm_version: str = TABLE_RECONCILIATION_ALGORITHM_VERSION

    def __post_init__(self) -> None:
        if self.algorithm_version != TABLE_RECONCILIATION_ALGORITHM_VERSION:
            raise ValueError("unexpected table reconciliation algorithm version")
        validate_table_reconciliation_diagnostics(self.as_dict())

    def as_dict(self) -> dict[str, str | int | bool]:
        return {
            "algorithm_version": self.algorithm_version,
            "model_hash": self.model_hash,
            "content_tables": self.content_tables,
            "model_tables": self.model_tables,
            "matched_tables": self.matched_tables,
            "page_local_closed": self.page_local_closed,
        }


@dataclass(frozen=True)
class TableReconciliationResult:
    content_list: list[dict[str, Any]]
    stats: TableReconciliationStats


@dataclass(frozen=True)
class _Table:
    index: int
    page_idx: int
    bbox: tuple[float, float, float, float]
    identity: _TableIdentity


def reconcile_content_list_tables(
    content_list: list[dict[str, Any]],
    *,
    model_path: Path | None,
    registered_evidence_image_paths: Mapping[str, Path],
    content_table_structures: Mapping[int, ParsedHtmlTable] | None = None,
) -> TableReconciliationResult:
    """Validate a complete one-to-one page-local table relation."""

    _validate_registered_paths(registered_evidence_image_paths)
    content_tables = _content_tables(
        content_list,
        registered_evidence_image_paths=registered_evidence_image_paths,
        content_table_structures=content_table_structures or {},
    )
    model_tables, model_hash = _read_model_tables(model_path)
    _validate_unique_bijection(content_tables, model_tables)
    count = len(content_tables)
    return TableReconciliationResult(
        content_list=list(content_list),
        stats=TableReconciliationStats(
            model_hash=model_hash,
            content_tables=count,
            model_tables=len(model_tables),
            matched_tables=count,
        ),
    )


def _content_tables(
    content_list: list[dict[str, Any]],
    *,
    registered_evidence_image_paths: Mapping[str, Path],
    content_table_structures: Mapping[int, ParsedHtmlTable],
) -> list[_Table]:
    tables: list[_Table] = []
    for index, item in enumerate(content_list):
        if str(item.get("type") or "") != "table":
            continue
        page_idx = _page_index(item.get("page_idx"))
        bbox = _normalized_bbox(item.get("bbox"))
        html = resolved_table_html(item)
        image_path = resolved_image_path(item)
        if page_idx is None or bbox is None:
            raise ParserOutputContractError(
                f"MinerU content table {index} lacks a valid page-local bbox"
            )
        if not isinstance(html, str) or not html.strip():
            raise ParserOutputContractError(
                f"MinerU content table {index} has empty HTML"
            )
        if image_path is None or not _safe_relative_path(image_path):
            raise ParserOutputContractError(
                f"MinerU content table {index} lacks a valid image crop path"
            )
        structure = content_table_structures.get(index)
        if structure is None:
            raise ParserOutputContractError(
                "MinerU content table structure was not materialized from "
                f"the content artifact: {index}"
            )
        outer_role = f"evidence_image_{index:06d}"
        if outer_role not in registered_evidence_image_paths:
            raise ParserOutputContractError(
                f"MinerU content table {index} image crop is not registered"
            )
        tables.append(
            _Table(
                index=index,
                page_idx=page_idx,
                bbox=bbox,
                identity=_table_identity(
                    structure,
                    media_sha256=lambda media: _content_media_sha256(
                        media,
                        source_item_index=index,
                        registered_evidence_image_paths=(
                            registered_evidence_image_paths
                        ),
                    ),
                ),
            )
        )
    return tables


def _read_model_tables(path: Path | None) -> tuple[list[_Table], str]:
    if path is None:
        raise ParserOutputContractError(
            "MinerU model artifact is required for page-local table closure"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot read MinerU model artifact: {path}"
        ) from exc
    model_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParserOutputContractError(
            f"invalid MinerU model JSON: {path}"
        ) from exc
    if not isinstance(payload, list):
        raise ParserOutputContractError("unsupported MinerU model table schema")
    if all(isinstance(page, dict) for page in payload):
        return _pipeline_model_tables(payload), model_hash
    if all(isinstance(page, list) for page in payload):
        return _vlm_model_tables(payload), model_hash
    raise ParserOutputContractError("unsupported MinerU model table schema")


def _pipeline_model_tables(payload: list[Any]) -> list[_Table]:
    tables: list[_Table] = []
    for page in payload:
        if not isinstance(page, dict):
            raise ParserOutputContractError("unsupported MinerU pipeline model schema")
        page_info = page.get("page_info")
        detections = page.get("layout_dets")
        if not isinstance(page_info, dict) or not isinstance(detections, list):
            raise ParserOutputContractError("unsupported MinerU pipeline model schema")
        page_idx = _page_index(page_info.get("page_no"))
        width = _positive_float(page_info.get("width"))
        height = _positive_float(page_info.get("height"))
        if page_idx is None or width is None or height is None:
            raise ParserOutputContractError("unsupported MinerU pipeline page geometry")
        for detection in detections:
            if not isinstance(detection, dict):
                raise ParserOutputContractError(
                    "unsupported MinerU pipeline detection schema"
                )
            if detection.get("label") != "table":
                continue
            raw_bbox = _bbox(detection.get("bbox"))
            html = detection.get("html")
            if raw_bbox is None or not isinstance(html, str) or not html.strip():
                raise ParserOutputContractError(
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
                raise ParserOutputContractError(
                    "MinerU pipeline model table bbox is invalid"
                )
            index = len(tables)
            tables.append(
                _Table(
                    index=index,
                    page_idx=page_idx,
                    bbox=bbox,
                    identity=_model_table_identity(
                        html,
                        label=f"model table {index}",
                    ),
                )
            )
    return tables


def _vlm_model_tables(payload: list[Any]) -> list[_Table]:
    tables: list[_Table] = []
    for page_idx, page in enumerate(payload):
        if not isinstance(page, list):
            raise ParserOutputContractError("unsupported MinerU VLM model schema")
        for item in page:
            if not isinstance(item, dict):
                raise ParserOutputContractError("unsupported MinerU VLM model schema")
            if item.get("type") != "table":
                continue
            raw_bbox = _bbox(item.get("bbox"))
            html = item.get("content")
            if (
                raw_bbox is None
                or any(coordinate < 0 or coordinate > 1 for coordinate in raw_bbox)
                or not isinstance(html, str)
                or not html.strip()
            ):
                raise ParserOutputContractError(
                    "MinerU VLM model table lacks normalized bbox or HTML"
                )
            bbox = _normalized_bbox([coordinate * 1000 for coordinate in raw_bbox])
            if bbox is None:
                raise ParserOutputContractError(
                    "MinerU VLM model table bbox is invalid"
                )
            index = len(tables)
            tables.append(
                _Table(
                    index=index,
                    page_idx=page_idx,
                    bbox=bbox,
                    identity=_model_table_identity(
                        html,
                        label=f"model table {index}",
                    ),
                )
            )
    return tables


def _validate_unique_bijection(
    content_tables: list[_Table],
    model_tables: list[_Table],
) -> None:
    owners: dict[int, int] = {}
    for content in content_tables:
        candidates = [
            model
            for model in model_tables
            if model.page_idx == content.page_idx
            and bbox_delta(model.bbox, content.bbox) <= MODEL_BBOX_MAX_DELTA
            and model.identity == content.identity
        ]
        if len(candidates) != 1:
            raise ParserOutputContractError(
                "MinerU content table "
                f"{content.index} has {len(candidates)} exact page-local model matches"
            )
        model_index = candidates[0].index
        if model_index in owners:
            raise ParserOutputContractError(
                "MinerU model table "
                f"{model_index} ambiguously matches content tables "
                f"{owners[model_index]} and {content.index}"
            )
        owners[model_index] = content.index
    unmatched_model_indices = sorted(set(range(len(model_tables))) - owners.keys())
    if unmatched_model_indices:
        raise ParserOutputContractError(
            "MinerU model tables are not represented by content_list: "
            + ", ".join(str(index) for index in unmatched_model_indices)
        )


def _model_table_identity(html: str, *, label: str) -> _TableIdentity:
    exported_html = _mineru_exported_table_html(html, label=label)
    structure = _parsed_structure(exported_html, label=label)
    return _table_identity(
        structure,
        media_sha256=_model_media_sha256,
    )


def _parsed_structure(html: str, *, label: str) -> ParsedHtmlTable:
    try:
        return parse_table_html_structure(html)
    except TableHtmlStructureError as exc:
        raise ParserOutputContractError(
            f"MinerU {label} has invalid logical cells: {exc}"
        ) from exc


def _table_identity(
    structure: ParsedHtmlTable,
    *,
    media_sha256: Callable[[HtmlTableMedia], str],
) -> _TableIdentity:
    media_by_cell: dict[tuple[int, int], list[_MediaIdentity]] = {}
    for media in structure.embedded_media:
        media_by_cell.setdefault((media.row, media.col), []).append(
            (
                media_sha256(media),
                media.alt_text,
                media.title_text,
            )
        )
    return tuple(
        (
            cell.row,
            cell.col,
            cell.text,
            cell.is_header,
            cell.rowspan,
            cell.colspan,
            tuple(media_by_cell.get((cell.row, cell.col), ())),
        )
        for cell in structure.cells
    )


def _content_media_sha256(
    media: HtmlTableMedia,
    *,
    source_item_index: int,
    registered_evidence_image_paths: Mapping[str, Path],
) -> str:
    if not _safe_relative_path(media.image_path):
        raise ParserOutputContractError(
            "MinerU content table "
            f"{source_item_index} has an unsafe embedded image path"
        )
    role = table_media_artifact_role(
        source_item_index,
        media.occurrence_index,
    )
    path = registered_evidence_image_paths.get(role)
    if path is None:
        raise ParserOutputContractError(
            "MinerU content table "
            f"{source_item_index} embedded image {media.occurrence_index} "
            "is not registered"
        )
    return _file_sha256(
        path,
        label=(
            f"content table {source_item_index} embedded image "
            f"{media.occurrence_index}"
        ),
    )


def _model_media_sha256(media: HtmlTableMedia) -> str:
    match = _MODEL_IMAGE_DATA_URI_RE.fullmatch(media.image_path)
    if match is None:
        raise ParserOutputContractError(
            "MinerU model table embedded image is not a supported data URI"
        )
    try:
        payload = base64.b64decode(
            match.group("payload"),
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ParserOutputContractError(
            "MinerU model table embedded image has invalid base64 bytes"
        ) from exc
    if not payload:
        raise ParserOutputContractError(
            "MinerU model table embedded image has empty bytes"
        )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path, *, label: str) -> str:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot read MinerU {label}: {path}"
        ) from exc
    if not payload:
        raise ParserOutputContractError(
            f"MinerU {label} has empty bytes"
        )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _mineru_exported_table_html(html: str, *, label: str) -> str:
    """Apply MinerU 3.4's bounded model-to-content equation serialization.

    ``*_model.json`` keeps inline table formulas as ``<eq>...</eq>`` while
    the official content exporter writes the configured, registered ``$``
    delimiters.  This mirrors that provider stage exactly; it does not
    reinterpret dollar-delimited content or repair malformed model markup.
    """

    markers = tuple(_MINERU_EQ_LIKE_TAG_RE.finditer(html))
    depth = 0
    for marker in markers:
        token = marker.group(0)
        if token not in {"<eq>", "</eq>"}:
            raise ParserOutputContractError(
                f"MinerU {label} has malformed inline-equation markup"
            )
        if token == "<eq>":
            if depth:
                raise ParserOutputContractError(
                    f"MinerU {label} has nested inline-equation markup"
                )
            depth = 1
        else:
            if not depth:
                raise ParserOutputContractError(
                    f"MinerU {label} has malformed inline-equation markup"
                )
            depth = 0
    if depth:
        raise ParserOutputContractError(
            f"MinerU {label} has unclosed inline-equation markup"
        )
    left, right = _SUPPORTED_MINERU_INLINE_DELIMITERS
    return _MINERU_INLINE_EQUATION_RE.sub(
        lambda match: f" {left}{unescape(match.group(1))}{right} ",
        html,
    )


def _safe_relative_path(value: str) -> bool:
    if not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and "." not in path.parts
        and str(path) == value
    )


def _validate_registered_paths(paths: Mapping[str, Path]) -> None:
    for role, path in paths.items():
        if (
            not isinstance(role, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", role) is None
            or not isinstance(path, Path)
            or not path.is_file()
        ):
            raise ValueError("registered evidence image paths are invalid")


def _normalized_bbox(
    value: Any,
) -> tuple[float, float, float, float] | None:
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
    return value if is_page_index(value) else None
