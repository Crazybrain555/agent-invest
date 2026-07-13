"""Pure helpers for provider-category vocabulary coverage audits."""

from __future__ import annotations

from collections.abc import Collection, Mapping

from disclosure_anchor.adapters.sources.cninfo.mapper import (
    ACCEPTED_MISC_CATEGORY_CODES,
)


def unmapped_code_counts(
    counts: Mapping[str, int],
    *,
    class_prefixes: Collection[str],
    facet_prefixes: Collection[str],
) -> dict[str, int]:
    """Return true gaps, excluding provider buckets accepted as generic misc."""

    prefixes = tuple((*class_prefixes, *facet_prefixes))
    return {
        code: count
        for code, count in counts.items()
        if code not in ACCEPTED_MISC_CATEGORY_CODES
        and not any(code.startswith(prefix) for prefix in prefixes)
    }
