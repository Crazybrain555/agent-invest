"""Map MinerU content-list artifacts to parser-neutral NormalizedIR v3."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import math
import re
from typing import Any

from disclosure_anchor.application.contracts.normalized_ir import (
    CURRENT_NORMALIZED_IR_VERSION,
)

from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    TABLE_RECONCILIATION_ALGORITHM_VERSION,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


_PRIVATE_AGGREGATE_TABLE_LOCATOR_KEY = "_mineru_aggregate_table_locator"
_AGGREGATE_TABLE_LOCATOR_BBOX_MAX_DELTA = 3.0
_AGGREGATE_TABLE_LOCATOR_FIELDS = frozenset(
    {
        "algorithm_version",
        "page_span",
        "page_bboxes",
        "model_table_indices",
        "continuation_source_item_indices",
    }
)
_PAGE_TOP_BAND_MAX = 180.0
_PAGE_BOTTOM_BAND_MIN = 820.0
_FURNITURE_POSITION_BUCKET = 25.0
_FURNITURE_MIN_PAGES = 3
_FURNITURE_MIN_CONTIGUOUS_DENSITY = 0.60


@dataclass(frozen=True)
class MinerUParserInfo:
    name: str
    package_version: str
    backend: str
    method: str
    language: str
    formula: bool
    table: bool


def _page_no(item: dict[str, Any]) -> int | None:
    page_idx = item.get("page_idx")
    return (
        page_idx + 1
        if isinstance(page_idx, int)
        and not isinstance(page_idx, bool)
        and page_idx >= 0
        else None
    )


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
        if page is not None and (
            not isinstance(page, int) or isinstance(page, bool) or page < 0
        ):
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


def resolved_table_html(item: dict[str, Any]) -> str | None:
    """Resolve MinerU's HTML aliases by content, never mere key presence."""

    present = [
        str(item[key])
        for key in ("table_body", "table_html")
        if item.get(key) is not None
    ]
    values = [value for value in present if value.strip()]
    if not values:
        return present[0] if present else None
    if any(value.strip() != values[0].strip() for value in values[1:]):
        raise ParserOutputContractError(
            "table_body and table_html contain conflicting non-empty HTML"
        )
    return values[0]


def _table_html(item: dict[str, Any]) -> str | None:
    return resolved_table_html(item)


def _image_path(item: dict[str, Any]) -> str | None:
    present: list[tuple[str, str]] = []
    for key in ("img_path", "image_path", "image"):
        if key not in item or item[key] is None:
            continue
        value = item[key]
        if not isinstance(value, str):
            raise ParserOutputContractError(
                f"MinerU {key} must be text when present"
            )
        if value.strip():
            present.append((key, value))
    if not present:
        return None
    canonical = present[0][1].strip()
    if any(value.strip() != canonical for _, value in present[1:]):
        raise ParserOutputContractError(
            "MinerU image path aliases contain conflicting non-empty values"
        )
    return canonical


def _collapse_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _text_list(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ParserOutputContractError(
        f"MinerU {field} must be text, an array of text, or null"
    )


def _resolved_text(item: dict[str, Any], *, include_content: bool) -> str | None:
    keys = ("text", "content") if include_content else ("text",)
    present: list[tuple[str, str]] = []
    for key in keys:
        if key not in item or item[key] is None:
            continue
        value = item[key]
        if not isinstance(value, str):
            raise ParserOutputContractError(
                f"MinerU {key} must be text when present"
            )
        if value:
            present.append((key, value))
    if not present:
        return "" if any(key in item for key in keys) else None
    canonical = present[0][1]
    if any(value != canonical for _, value in present[1:]):
        raise ParserOutputContractError(
            "MinerU text/content aliases contain conflicting non-empty values"
        )
    return canonical


def _validated_aggregate_table_locator(
    locator: dict[str, Any],
    *,
    root_source_item_index: int,
    content_list: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate the ordered, complete private locator before publishing it.

    JSON Schema can require the public bundle and unique array members, but it
    cannot compare two array positions or require page numbers to be ordered.
    The mapper is therefore the fail-loud semantic contract boundary for those
    cross-field invariants.
    """

    fields = set(locator)
    if fields != _AGGREGATE_TABLE_LOCATOR_FIELDS:
        missing = sorted(_AGGREGATE_TABLE_LOCATOR_FIELDS - fields)
        unexpected = sorted(fields - _AGGREGATE_TABLE_LOCATOR_FIELDS)
        raise ParserOutputContractError(
            "invalid aggregate table locator fields: "
            f"missing={missing}, unexpected={unexpected}"
        )
    if locator["algorithm_version"] != TABLE_RECONCILIATION_ALGORITHM_VERSION:
        raise ParserOutputContractError(
            "invalid aggregate table locator algorithm version"
        )

    page_span = locator["page_span"]
    if (
        not isinstance(page_span, list)
        or len(page_span) != 2
        or any(
            not isinstance(page, int) or isinstance(page, bool) or page < 1
            for page in page_span
        )
        or page_span[0] >= page_span[1]
    ):
        raise ParserOutputContractError(
            "aggregate table locator page_span must be strictly ascending"
        )

    page_bboxes = locator["page_bboxes"]
    if not isinstance(page_bboxes, list) or len(page_bboxes) < 2:
        raise ParserOutputContractError(
            "aggregate table locator page_bboxes must contain at least two pages"
        )
    expected_pages = list(range(page_span[0], page_span[1] + 1))
    actual_pages: list[int] = []
    normalized_page_bboxes: list[dict[str, Any]] = []
    for page_bbox in page_bboxes:
        if not isinstance(page_bbox, dict) or set(page_bbox) != {"page_no", "bbox"}:
            raise ParserOutputContractError(
                "aggregate table locator page bbox must contain page_no and bbox"
            )
        page_no = page_bbox["page_no"]
        bbox = page_bbox["bbox"]
        if (
            not isinstance(page_no, int)
            or isinstance(page_no, bool)
            or page_no < 1
            or not isinstance(bbox, list)
            or len(bbox) != 4
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                for value in bbox
            )
        ):
            raise ParserOutputContractError(
                "aggregate table locator contains an invalid page bbox"
            )
        x_min, y_min, x_max, y_max = (float(value) for value in bbox)
        if x_min >= x_max or y_min >= y_max:
            raise ParserOutputContractError(
                "aggregate table locator bbox must have positive width and height"
            )
        actual_pages.append(page_no)
        normalized_page_bboxes.append(
            {"page_no": page_no, "bbox": list(bbox)}
        )
    if actual_pages != expected_pages:
        raise ParserOutputContractError(
            "aggregate table locator page_bboxes must cover page_span in order"
        )

    model_indices = _validated_unique_nonnegative_indices(
        locator["model_table_indices"],
        label="model_table_indices",
        expected_length=len(page_bboxes),
    )
    continuation_indices = _validated_unique_nonnegative_indices(
        locator["continuation_source_item_indices"],
        label="continuation_source_item_indices",
        expected_length=len(page_bboxes) - 1,
    )
    if continuation_indices != sorted(continuation_indices):
        raise ParserOutputContractError(
            "aggregate table locator continuation indices must be ascending"
        )
    if any(index <= root_source_item_index for index in continuation_indices):
        raise ParserOutputContractError(
            "aggregate table locator continuation indices must follow the root"
        )

    root_item = content_list[root_source_item_index]
    root_page_no = _required_source_item_page_no(root_item, label="root")
    if root_page_no != page_span[0] or root_page_no != actual_pages[0]:
        raise ParserOutputContractError(
            "aggregate table locator root page must match page_span and page_bboxes"
        )
    if not (_table_html(root_item) or "").strip():
        raise ParserOutputContractError(
            "aggregate table locator root must carry non-empty aggregate table HTML"
        )
    root_bbox = _required_source_item_bbox(root_item, label="root")
    if _bbox_delta(root_bbox, normalized_page_bboxes[0]["bbox"]) > (
        _AGGREGATE_TABLE_LOCATOR_BBOX_MAX_DELTA
    ):
        raise ParserOutputContractError(
            "aggregate table locator root bbox must match page_bboxes"
        )
    for continuation_index, expected_page_bbox in zip(
        continuation_indices, normalized_page_bboxes[1:], strict=True
    ):
        if continuation_index >= len(content_list):
            raise ParserOutputContractError(
                "aggregate table locator continuation index is out of range"
            )
        continuation = content_list[continuation_index]
        if continuation.get("type") != "table":
            raise ParserOutputContractError(
                "aggregate table locator continuation must reference a table item"
            )
        if any(
            str(continuation[key]).strip()
            for key in ("table_body", "table_html")
            if key in continuation and continuation[key] is not None
        ):
            raise ParserOutputContractError(
                "aggregate table locator continuation must reference an empty table ghost"
            )
        continuation_page_no = _required_source_item_page_no(
            continuation, label="continuation"
        )
        expected_page_no = expected_page_bbox["page_no"]
        if continuation_page_no != expected_page_no:
            raise ParserOutputContractError(
                "aggregate table locator continuation page must match page_bboxes"
            )
        continuation_bbox = _required_source_item_bbox(
            continuation, label="continuation"
        )
        if _bbox_delta(continuation_bbox, expected_page_bbox["bbox"]) > (
            _AGGREGATE_TABLE_LOCATOR_BBOX_MAX_DELTA
        ):
            raise ParserOutputContractError(
                "aggregate table locator continuation bbox must match page_bboxes"
            )
    return {
        "algorithm_version": TABLE_RECONCILIATION_ALGORITHM_VERSION,
        "page_span": list(page_span),
        "page_bboxes": normalized_page_bboxes,
        "model_table_indices": model_indices,
        "continuation_source_item_indices": continuation_indices,
    }


def _required_source_item_page_no(item: dict[str, Any], *, label: str) -> int:
    page_idx = item.get("page_idx")
    if (
        not isinstance(page_idx, int)
        or isinstance(page_idx, bool)
        or page_idx < 0
    ):
        raise ParserOutputContractError(
            f"aggregate table locator {label} item requires a valid page_idx"
        )
    return page_idx + 1


def _required_source_item_bbox(
    item: dict[str, Any], *, label: str
) -> tuple[float, float, float, float]:
    bbox = item.get("bbox")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            for value in bbox
        )
    ):
        raise ParserOutputContractError(
            f"aggregate table locator {label} item requires a finite bbox"
        )
    normalized_bbox = tuple(float(value) for value in bbox)
    if normalized_bbox[0] >= normalized_bbox[2] or normalized_bbox[1] >= normalized_bbox[3]:
        raise ParserOutputContractError(
            f"aggregate table locator {label} item bbox must have positive dimensions"
        )
    return (
        normalized_bbox[0],
        normalized_bbox[1],
        normalized_bbox[2],
        normalized_bbox[3],
    )


def _bbox_delta(
    left: tuple[float, float, float, float], right: list[Any]
) -> float:
    return max(abs(left[index] - float(right[index])) for index in range(4))


def _validated_unique_nonnegative_indices(
    value: Any, *, label: str, expected_length: int
) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != expected_length
        or any(
            not isinstance(index, int) or isinstance(index, bool) or index < 0
            for index in value
        )
        or len(set(value)) != len(value)
    ):
        raise ParserOutputContractError(
            f"aggregate table locator {label} has invalid indices"
        )
    return list(value)


def _list_text(item: dict[str, Any]) -> str | None:
    """Preserve MinerU list prose without inventing a nested-list contract."""

    value = item.get("list_items")
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(list_item, str) for list_item in value):
        return None
    if not any(list_item.strip() for list_item in value):
        return None
    return "\n".join(value)


@dataclass(frozen=True)
class _TableCell:
    text: str
    rowspan: int
    colspan: int
    is_header: bool


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_TableCell]] = []
        self._current_row: list[_TableCell] | None = None
        self._cell_attrs: tuple[int, int, bool] | None = None
        self._cell_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            if self._current_row is not None:
                self.rows.append(self._current_row)
            self._current_row = []
            return
        if tag not in {"td", "th"} or self._current_row is None:
            return
        attr_map = {name.lower(): value for name, value in attrs}
        self._cell_attrs = (
            _span_value(attr_map.get("rowspan")),
            _span_value(attr_map.get("colspan")),
            tag == "th",
        )
        self._cell_text = []

    def handle_data(self, data: str) -> None:
        if self._cell_attrs is not None:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._current_row is not None:
            self._finish_cell()
            return
        if tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None

    def finish(self) -> None:
        if self._current_row is not None:
            self._finish_cell()
            self.rows.append(self._current_row)
            self._current_row = None

    def _finish_cell(self) -> None:
        if self._cell_attrs is None or self._current_row is None:
            return
        rowspan, colspan, is_header = self._cell_attrs
        self._current_row.append(
            _TableCell(
                text=_collapse_text("".join(self._cell_text)),
                rowspan=rowspan,
                colspan=colspan,
                is_header=is_header,
            )
        )
        self._cell_attrs = None
        self._cell_text = []


def _span_value(value: str | None) -> int:
    try:
        parsed = int(value) if value is not None else 1
    except ValueError:
        return 1
    return parsed if parsed >= 1 else 1


def parse_table_html(html: str) -> tuple[dict[str, Any], bool]:
    """Parse MinerU table HTML into the neutral expanded grid contract."""

    if not html.strip():
        return {"headers": [], "rows": []}, False
    try:
        parser = _TableParser()
        parser.feed(html)
        parser.finish()
        table = _table_grid(parser.rows)
    except Exception:
        return {"headers": [], "rows": []}, True
    # Non-empty HTML that yields no cells means the carrier was not a
    # recognizable table; flag it so the builder falls back to raw_html
    # instead of silently emitting an empty grid.
    if not table.get("headers") and not table.get("rows"):
        return table, True
    return table, False


def table_html_logical_rows(
    html: str,
) -> tuple[list[list[tuple[str, bool, int, int]]], bool]:
    """Return source ``tr`` cells without expanding row/column spans.

    Reconciliation compares text, ``th``/``td`` identity, rowspan and colspan.
    All four affect the expanded NormalizedIR grid, so omitting any of them can
    make a seemingly equal source-cell sequence add, remove or reclassify rows
    after page-local restoration. NormalizedIR itself continues to use the
    expanded grid from :func:`parse_table_html`.
    """

    if not html.strip():
        return [], False
    try:
        parser = _TableParser()
        parser.feed(html)
        parser.finish()
        rows = [
            [
                (cell.text, cell.is_header, cell.rowspan, cell.colspan)
                for cell in row
            ]
            for row in parser.rows
        ]
    except Exception:
        return [], True
    if not any(row for row in rows):
        return [], True
    return rows, False


def _table_grid(source_rows: list[list[_TableCell]]) -> dict[str, Any]:
    occupied: dict[tuple[int, int], str] = {}
    merged_cells: list[dict[str, int]] = []
    max_row = -1
    max_col = -1
    for row_index, row in enumerate(source_rows):
        col_index = 0
        for cell in row:
            while (row_index, col_index) in occupied:
                col_index += 1
            if cell.rowspan > 1 or cell.colspan > 1:
                merged_cells.append(
                    {
                        "row": row_index,
                        "col": col_index,
                        "rowspan": cell.rowspan,
                        "colspan": cell.colspan,
                    }
                )
            for row_offset in range(cell.rowspan):
                for col_offset in range(cell.colspan):
                    target = (row_index + row_offset, col_index + col_offset)
                    occupied[target] = cell.text
                    max_row = max(max_row, target[0])
                    max_col = max(max_col, target[1])
            col_index += cell.colspan
    table: dict[str, Any]
    if max_row < 0 or max_col < 0:
        table = {"headers": [], "rows": []}
    else:
        grid = [
            [occupied.get((row_index, col_index), "") for col_index in range(max_col + 1)]
            for row_index in range(max_row + 1)
        ]
        # Headers only on <th> evidence. MinerU emits plain <td> tables, so
        # headers is usually empty and the full grid stays in rows; promoting
        # the first row to a header is a business rule that belongs to the
        # unit builder (05-S5, after cross-page merge) — forcing it here
        # mislabels continuation pages and key-value tables (real-data audit:
        # 25/398 tables had data rows promoted to headers).
        first_row_is_header = bool(source_rows and any(cell.is_header for cell in source_rows[0]))
        if first_row_is_header:
            table = {"headers": grid[0], "rows": grid[1:]}
        else:
            table = {"headers": [], "rows": grid}
    if merged_cells:
        table["merged_cells"] = merged_cells
    return table


def _kind_and_heading(raw_kind: str, item: dict[str, Any]) -> tuple[str, int | None]:
    raw_heading_level = item.get("text_level")
    if raw_heading_level is None:
        heading_level = None
    elif (
        not isinstance(raw_heading_level, int)
        or isinstance(raw_heading_level, bool)
        or raw_heading_level < 1
    ):
        raise ParserOutputContractError(
            "MinerU text_level must be a positive integer or null"
        )
    else:
        heading_level = raw_heading_level
    if raw_kind == "text":
        if heading_level is not None:
            return "heading", heading_level
        return "text", None
    if raw_kind in {"aside_text", "page_footnote"}:
        # MinerU emits these from its discarded-layout lane, but both are
        # source text: aside_text is supplementary margin content and a page
        # footnote can define or qualify nearby facts.  ``raw_kind`` retains
        # the auxiliary role for later retrieval ranking.
        return "text", heading_level
    if raw_kind in {"header", "page_number", "footer"}:
        return "page_furniture", heading_level
    if raw_kind in {"table", "image", "equation"}:
        return raw_kind, heading_level
    if raw_kind == "chart":
        # NormalizedIR v3 has one parser-neutral image-backed visual kind;
        # ``raw_kind=chart`` preserves the provider distinction while keeping
        # the chart image and recognized data in the same source atom.
        return "image", heading_level
    if raw_kind == "list" and _list_text(item) is not None:
        # MinerU 3.x emits prose lists as ordered string items.  NormalizedIR
        # has no parser-specific list kind, so preserve their exact order and
        # text as a neutral text element while retaining raw_kind="list".
        return "text", None
    return "unknown", heading_level


def _inferred_page_furniture_indices(
    content_list: list[dict[str, Any]],
) -> frozenset[int]:
    """Infer running furniture from exact text + repeated page-edge geometry.

    MinerU occasionally labels running headers as level-1 text headings.  A
    title vocabulary cannot solve that safely: the same words may be a real
    body heading elsewhere.  We therefore require the same normalized carrier
    on at least three distinct, sufficiently contiguous pages and in the same
    top/bottom position band.  The signature must also occur only once on each
    candidate page: same-page ambiguity is business content until proven
    otherwise.  Only those physical occurrences are retyped; a body occurrence
    at another position remains content.
    """

    signature_page_counts: dict[tuple[str, int], int] = defaultdict(int)
    for item in content_list:
        if str(item.get("type") or "unknown") != "text":
            continue
        page_idx = item.get("page_idx")
        text = _layout_text(item.get("text"))
        if (
            text
            and isinstance(page_idx, int)
            and not isinstance(page_idx, bool)
            and page_idx >= 0
        ):
            signature_page_counts[(text, page_idx)] += 1

    groups: dict[tuple[str, str, int, int], list[tuple[int, int]]] = defaultdict(
        list
    )
    for index, item in enumerate(content_list):
        if str(item.get("type") or "unknown") != "text":
            continue
        text = _layout_text(item.get("text"))
        if not text or len(text) > 200:
            continue
        page_idx = item.get("page_idx")
        bbox = _finite_bbox(item.get("bbox"))
        if not isinstance(page_idx, int) or isinstance(page_idx, bool) or bbox is None:
            continue
        if signature_page_counts[(text, page_idx)] != 1:
            continue
        x1, y1, x2, y2 = bbox
        if min(x1, y1, x2, y2) < 0 or max(x1, y1, x2, y2) > 1000:
            continue
        if y2 <= _PAGE_TOP_BAND_MAX:
            band = "top"
        elif y1 >= _PAGE_BOTTOM_BAND_MIN:
            band = "bottom"
        else:
            continue
        center_bucket = round(((y1 + y2) / 2) / _FURNITURE_POSITION_BUCKET)
        height_bucket = round((y2 - y1) / _FURNITURE_POSITION_BUCKET)
        groups[(text, band, center_bucket, height_bucket)].append((index, page_idx))

    inferred: set[int] = set()
    for occurrences in groups.values():
        pages = sorted({page_idx for _, page_idx in occurrences})
        if len(pages) < _FURNITURE_MIN_PAGES:
            continue
        span = pages[-1] - pages[0] + 1
        if len(pages) / span < _FURNITURE_MIN_CONTIGUOUS_DENSITY:
            continue
        inferred.update(index for index, _ in occurrences)
    return frozenset(inferred)


def _layout_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


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
        parser_info: MinerUParserInfo,
        document_metadata: dict[str, Any],
        parser_artifacts: dict[str, str] | None = None,
        start_page: int | None = None,
        end_page: int | None = None,
    ) -> dict[str, Any]:
        document_id = str(document_metadata["document_id"])
        inferred_furniture = _inferred_page_furniture_indices(content_list)
        elements = [
            self._map_item(
                item=item,
                index=index,
                document_id=document_id,
                content_list=content_list,
                inferred_page_furniture=index in inferred_furniture,
            )
            for index, item in enumerate(content_list)
        ]
        return {
            "contract_version": CURRENT_NORMALIZED_IR_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "document_id": document_id,
            "source_pdf": str(document_metadata.get("source_pdf", "")),
            "title": document_metadata.get("title"),
            "parser": {
                "name": parser_info.name,
                "package_version": parser_info.package_version,
                "backend": parser_info.backend,
                "method": parser_info.method,
                "language": parser_info.language,
                "formula": parser_info.formula,
                "table": parser_info.table,
            },
            "parser_artifacts": parser_artifacts or {},
            "parsed_pages": _parsed_pages(
                content_list,
                start_page=start_page,
                end_page=end_page,
            ),
            "elements": elements,
        }

    def _map_item(
        self,
        *,
        item: dict[str, Any],
        index: int,
        document_id: str,
        content_list: list[dict[str, Any]],
        inferred_page_furniture: bool = False,
    ) -> dict[str, Any]:
        raw_kind = str(item.get("type") or "unknown")
        locator_present = _PRIVATE_AGGREGATE_TABLE_LOCATOR_KEY in item
        locator = item.get(_PRIVATE_AGGREGATE_TABLE_LOCATOR_KEY)
        if locator_present and (
            raw_kind != "table" or not isinstance(locator, dict)
        ):
            raise ParserOutputContractError(
                "aggregate table locator requires a table item and object bundle"
            )
        kind, heading_level = _kind_and_heading(raw_kind, item)
        if inferred_page_furniture:
            kind = "page_furniture"
        element: dict[str, Any] = {
            "ir_id": f"ir_{index:04d}",
            "kind": kind,
            "raw_kind": raw_kind,
            "order_index": index,
            "source_item_index": index,
            "heading_level": heading_level,
        }
        if "page_idx" in item:
            page_idx = item["page_idx"]
            if (
                not isinstance(page_idx, int)
                or isinstance(page_idx, bool)
                or page_idx < 0
            ):
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
        text = _resolved_text(
            item,
            include_content=raw_kind in {"image", "chart", "equation"},
        )
        if text is not None:
            element["text"] = text
        elif raw_kind == "list" and (list_text := _list_text(item)) is not None:
            element["text"] = list_text
        if raw_kind == "table":
            element["table_caption"] = _text_list(
                item.get("table_caption"), field="table_caption"
            )
            element["table_footnote"] = _text_list(
                item.get("table_footnote"), field="table_footnote"
            )
            table_html = _table_html(item) or ""
            element["table_html"] = table_html
            table, parse_failed = parse_table_html(table_html)
            element["table"] = table
            if parse_failed:
                element["table_parse_failed"] = True
            if locator is not None:
                assert isinstance(locator, dict)  # narrowed by the fail-loud guard
                # A proven group rejected for page-local restoration keeps its
                # aggregate HTML. These fields are source locators only and
                # therefore never enter the table payload. Restored groups use
                # the ordinary per-page fields above and carry no private tag.
                validated_locator = _validated_aggregate_table_locator(
                    locator,
                    root_source_item_index=index,
                    content_list=content_list,
                )
                for key in (
                    "page_span",
                    "page_bboxes",
                    "model_table_indices",
                    "continuation_source_item_indices",
                ):
                    element[key] = validated_locator[key]
                element["table_locator_algorithm"] = validated_locator[
                    "algorithm_version"
                ]
        if raw_kind in {"image", "chart"}:
            caption_field = (
                "chart_caption" if raw_kind == "chart" else "image_caption"
            )
            footnote_field = (
                "chart_footnote" if raw_kind == "chart" else "image_footnote"
            )
            element["image_caption"] = _text_list(
                item.get(caption_field), field=caption_field
            )
            element["image_footnote"] = _text_list(
                item.get(footnote_field), field=footnote_field
            )
            if "sub_type" in item:
                subtype = item["sub_type"]
                if subtype is not None and not isinstance(subtype, str):
                    raise ParserOutputContractError(
                        "MinerU visual sub_type must be text or null"
                    )
                if subtype:
                    element["visual_subtype"] = subtype
        if image_path := _image_path(item):
            element["image_path"] = image_path
        element["document_id"] = document_id
        return element
