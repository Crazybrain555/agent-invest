"""Pure planning for gradual historical index coverage."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta


def oldest_missing_sync_slice(
    *,
    window_start: date,
    window_end: date,
    covered: Sequence[tuple[date, date]],
    max_days: int = 90,
) -> tuple[date, date] | None:
    """Return one closed missing interval, bounded by days and known coverage.

    Coverage is successful source query history, including empty results; it
    says nothing about filing availability or processing completion.
    """

    if max_days < 1:
        raise ValueError("history slice must contain at least one day")
    if window_start > window_end:
        return None
    cursor = window_start
    for start, end in sorted(covered):
        if start > end:
            raise ValueError("successful sync coverage has reversed dates")
        if end < cursor:
            continue
        if start > cursor:
            return cursor, min(
                window_end,
                start - timedelta(days=1),
                cursor + timedelta(days=max_days - 1),
            )
        cursor = max(cursor, end + timedelta(days=1))
        if cursor > window_end:
            return None
    return cursor, min(window_end, cursor + timedelta(days=max_days - 1))
