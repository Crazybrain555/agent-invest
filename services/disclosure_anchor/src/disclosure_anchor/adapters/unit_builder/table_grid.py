"""Pure table-grid normalization shared by S5 and parser reconciliation."""

from __future__ import annotations

from typing import Any


def merge_table_grids(
    tables: list[dict[str, Any]],
) -> tuple[list[str], list[list[str]], list[dict[str, int]]]:
    """Merge page grids using full ``[headers, *rows]`` merge coordinates.

    Mapper ``merged_cells`` coordinates refer to the complete expanded source
    grid.  S5 may promote a plain first row to ``headers`` and suppress a
    repeated continuation header, but neither operation may change that public
    coordinate convention.
    """

    if not tables:
        return [], [], []
    first = tables[0]
    first_headers = [str(item) for item in first.get("headers") or []]
    first_rows = [[str(cell) for cell in row] for row in first.get("rows") or []]
    full_grid = [first_headers, *first_rows] if first_headers else first_rows
    merged_cells = [dict(cell) for cell in first.get("merged_cells") or []]
    header_candidate = full_grid[0] if full_grid else []

    for table in tables[1:]:
        next_headers = [str(item) for item in table.get("headers") or []]
        next_rows = [
            [str(cell) for cell in row] for row in table.get("rows") or []
        ]
        next_full_grid = [next_headers, *next_rows] if next_headers else next_rows
        skipped_rows = int(
            bool(
                header_candidate
                and next_full_grid
                and _same_cells(next_full_grid[0], header_candidate)
            )
        )
        row_offset = len(full_grid)
        for merged_cell in table.get("merged_cells") or []:
            adjusted = dict(merged_cell)
            source_row = int(adjusted["row"])
            source_end = source_row + int(adjusted["rowspan"])
            kept_start = max(source_row, skipped_rows)
            if source_end <= kept_start:
                continue
            adjusted["row"] = row_offset + kept_start - skipped_rows
            adjusted["rowspan"] = source_end - kept_start
            merged_cells.append(adjusted)
        full_grid.extend(next_full_grid[skipped_rows:])

    headers = full_grid[0] if full_grid else []
    rows = full_grid[1:] if full_grid else []
    return headers, rows, merged_cells


def drop_blank_table_rows(
    *,
    headers: list[str],
    rows: list[list[str]],
    merged_cells: list[dict[str, int]],
) -> tuple[list[list[str]], list[dict[str, int]], int]:
    """Drop blank data rows and remap full-grid merge anchors/spans."""

    kept: list[list[str]] = []
    header_offset = 1 if headers else 0
    index_map: dict[int, int] = {0: 0} if headers else {}
    for index, grid_row in enumerate(rows):
        if any(str(cell).strip() for cell in grid_row):
            index_map[index + header_offset] = len(kept) + header_offset
            kept.append(grid_row)
    dropped = len(rows) - len(kept)
    if not dropped:
        return rows, merged_cells, 0

    adjusted: list[dict[str, int]] = []
    for cell in merged_cells:
        source_start = int(cell["row"])
        rowspan = int(cell["rowspan"])
        covered_rows = [
            index_map[source_row]
            for source_row in range(source_start, source_start + rowspan)
            if source_row in index_map
        ]
        if not covered_rows:
            continue
        adjusted.append(
            {
                **cell,
                "row": covered_rows[0],
                "rowspan": len(covered_rows),
            }
        )
    return kept, adjusted, dropped


def _same_cells(left: list[str], right: list[str]) -> bool:
    return [cell.strip() for cell in left] == [cell.strip() for cell in right]
