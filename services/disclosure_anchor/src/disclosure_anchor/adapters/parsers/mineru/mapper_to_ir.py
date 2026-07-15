"""Map MinerU content_list artifacts to parser-neutral NormalizedIR v2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import math
import re
from typing import Any

from disclosure_anchor.domain.errors import ParserOutputContractError


TABLE_RECONCILIATION_ALGORITHM_VERSION = "mineru-aggregate-table-restore.v3"
_PRIVATE_AGGREGATE_TABLE_LOCATOR_KEY = "_mineru_aggregate_table_locator"
_AGGREGATE_TABLE_LOCATOR_FIELDS = frozenset(
    {
        "algorithm_version",
        "page_span",
        "page_bboxes",
        "model_table_indices",
        "continuation_source_item_indices",
    }
)


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
    return page_idx + 1 if isinstance(page_idx, int) else None


def _parsed_pages(items: list[dict[str, Any]]) -> dict[str, Any]:
    page_numbers = [page for item in items if (page := _page_no(item)) is not None]
    if not page_numbers:
        return {"start_page_no": None, "end_page_no": None, "full_pdf": True}
    return {
        "start_page_no": min(page_numbers),
        "end_page_no": max(page_numbers),
        "full_pdf": True,
    }


def _table_html(item: dict[str, Any]) -> str | None:
    value = item.get("table_body")
    if value is None:
        value = item.get("table_html")
    return str(value) if value is not None else None


def _image_path(item: dict[str, Any]) -> str | None:
    value = item.get("img_path")
    if value is None:
        value = item.get("image_path")
    return str(value) if value else None


def _collapse_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _validated_aggregate_table_locator(
    locator: dict[str, Any], *, root_source_item_index: int
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
    return {
        "algorithm_version": TABLE_RECONCILIATION_ALGORITHM_VERSION,
        "page_span": list(page_span),
        "page_bboxes": normalized_page_bboxes,
        "model_table_indices": model_indices,
        "continuation_source_item_indices": continuation_indices,
    }


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
    heading_level = _int_or_none(item.get("text_level"))
    if raw_kind == "text":
        if heading_level is not None:
            return "heading", heading_level
        return "text", None
    if raw_kind in {"header", "page_number", "footer"}:
        return "page_furniture", heading_level
    if raw_kind in {"table", "image", "equation"}:
        return raw_kind, heading_level
    if raw_kind == "list" and _list_text(item) is not None:
        # MinerU 3.x emits prose lists as ordered string items.  NormalizedIR
        # has no parser-specific list kind, so preserve their exact order and
        # text as a neutral text element while retaining raw_kind="list".
        return "text", None
    return "unknown", heading_level


class MinerUToNormalizedIRMapper:
    """Convert the stable MinerU content_list shape into NormalizedIR."""

    def map_content_list(
        self,
        *,
        content_list: list[dict[str, Any]],
        parser_info: MinerUParserInfo,
        document_metadata: dict[str, Any],
        parser_artifacts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        document_id = str(document_metadata["document_id"])
        elements = [
            self._map_item(
                item=item,
                index=index,
                document_id=document_id,
            )
            for index, item in enumerate(content_list)
        ]
        return {
            "contract_version": "normalized_ir.v2",
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
            "parsed_pages": _parsed_pages(content_list),
            "elements": elements,
        }

    def _map_item(
        self,
        *,
        item: dict[str, Any],
        index: int,
        document_id: str,
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
        element: dict[str, Any] = {
            "ir_id": f"ir_{index:04d}",
            "kind": kind,
            "raw_kind": raw_kind,
            "order_index": index,
            "source_item_index": index,
            "heading_level": heading_level,
        }
        for key in ("page_idx", "bbox", "text_level"):
            if key in item:
                element[key] = item[key]
        if (page_no := _page_no(item)) is not None:
            element["page_no"] = page_no
        if "text" in item:
            element["text"] = str(item["text"])
        elif raw_kind == "list" and (list_text := _list_text(item)) is not None:
            element["text"] = list_text
        if raw_kind == "table":
            element["table_caption"] = _string_list(item.get("table_caption"))
            element["table_footnote"] = _string_list(item.get("table_footnote"))
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
                    locator, root_source_item_index=index
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
        if image_path := _image_path(item):
            element["image_path"] = image_path
        if "image" in item and "image_path" not in element:
            element["image_path"] = str(item["image"])
        element["document_id"] = document_id
        return element
