"""Shared MinerU page-geometry primitives (bbox delta, bands, page guard).

Pure numeric helpers used by both the content_list->IR mapper and the table
reconciler.  Keeping one definition stops the two from drifting apart on the
top/bottom position bands or the page-index guard.
"""

from __future__ import annotations

from collections.abc import Sequence

from typing_extensions import TypeIs


# Top/bottom position bands in normalized 0..1000 page space used to classify a
# running header/footer candidate by where it sits vertically on the page.
PAGE_TOP_BAND_MAX = 180.0
PAGE_BOTTOM_BAND_MIN = 820.0


def bbox_delta(left: Sequence[float], right: Sequence[float]) -> float:
    """Return the maximum absolute per-coordinate delta of two 4-point bboxes."""

    return max(abs(float(left[index]) - float(right[index])) for index in range(4))


def is_page_index(value: object) -> TypeIs[int]:
    """True for a real 0-based page index (a non-negative, non-bool integer)."""

    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
