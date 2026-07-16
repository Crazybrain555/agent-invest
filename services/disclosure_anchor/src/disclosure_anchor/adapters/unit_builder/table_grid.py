"""Pure table-grid normalization for unit-builder S5."""

from __future__ import annotations

from typing import Any


def merge_table_grids_with_stats(
    tables: list[dict[str, Any]],
) -> tuple[list[str], list[list[str]], list[dict[str, int]], int]:
    """Merge grids without rewriting source row structure.

    Visually blank rows can still be covered by rowspan/colspan and therefore
    carry table meaning.  Normalized views may hide them at presentation time;
    the L1 evidence payload keeps the source grid and reports zero destructive
    row drops.
    """

    full_grid: list[list[str]] = []
    merged_cells: list[dict[str, int]] = []
    header_candidate: list[str] = []
    first_table_has_header = False
    dropped_blank_rows = 0

    for table in tables:
        next_headers = [str(item) for item in table.get("headers") or []]
        next_rows = [
            [str(cell) for cell in row] for row in table.get("rows") or []
        ]
        next_cells = [dict(cell) for cell in table.get("merged_cells") or []]
        next_full_grid = [next_headers, *next_rows] if next_headers else next_rows
        if not full_grid:
            full_grid.extend(next_full_grid)
            merged_cells.extend(next_cells)
            first_table_has_header = bool(next_headers)
            header_candidate = list(next_headers)
            continue
        if not next_full_grid:
            continue
        skipped_rows = int(
            bool(
                header_candidate
                and next_headers
                and next_full_grid
                and _same_cells(next_headers, header_candidate)
                and not _merge_crosses_row_boundary(next_cells, boundary=1)
            )
        )
        row_offset = len(full_grid)
        for merged_cell in next_cells:
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

    # Preserve the source role. A first data row is never promoted to a header
    # merely because the public table shape has a separate ``headers`` field.
    headers = full_grid[0] if first_table_has_header and full_grid else []
    rows = full_grid[1:] if first_table_has_header else full_grid
    return headers, rows, merged_cells, dropped_blank_rows


def _same_cells(left: list[str], right: list[str]) -> bool:
    return [cell.strip() for cell in left] == [cell.strip() for cell in right]


def _merge_crosses_row_boundary(
    merged_cells: list[dict[str, int]], *, boundary: int
) -> bool:
    return any(
        int(cell["row"]) < boundary
        < int(cell["row"]) + int(cell["rowspan"])
        for cell in merged_cells
    )
