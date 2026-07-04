"""Map MinerU content_list artifacts to parser-neutral NormalizedIR v2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from typing import Any


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
        if self._cell_attrs is None:
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


def _parse_table(html: str) -> tuple[dict[str, Any], bool]:
    if not html.strip():
        return {"headers": [], "rows": []}, False
    try:
        parser = _TableParser()
        parser.feed(html)
        parser.finish()
        return _table_grid(parser.rows), False
    except Exception:
        return {"headers": [], "rows": []}, True


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
    if max_row < 0 or max_col < 0:
        table = {"headers": [], "rows": []}
    else:
        grid = [
            [occupied.get((row_index, col_index), "") for col_index in range(max_col + 1)]
            for row_index in range(max_row + 1)
        ]
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
        if raw_kind == "table":
            element["table_caption"] = _string_list(item.get("table_caption"))
            element["table_footnote"] = _string_list(item.get("table_footnote"))
            table_html = _table_html(item) or ""
            element["table_html"] = table_html
            table, parse_failed = _parse_table(table_html)
            element["table"] = table
            if parse_failed:
                element["table_parse_failed"] = True
        if image_path := _image_path(item):
            element["image_path"] = image_path
        if "image" in item and "image_path" not in element:
            element["image_path"] = str(item["image"])
        element["document_id"] = document_id
        return element
