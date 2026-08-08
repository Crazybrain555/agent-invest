"""Shared comparison space for provider/native text matching.

Payloads stay verbatim; every matching layer folds width/compatibility
and evidence-proven glyph variants here, in one place, so builder and
audit derive the same equivalences without importing each other.
"""

from __future__ import annotations

from disclosure_anchor.domain.value_objects.comparison_text import (
    fold_provider_markup as _fold_provider_markup,
    source_occurrence_comparison_text,
)


def fold_provider_markup(value: str) -> str:
    """Compatibility export for parser callers."""

    return _fold_provider_markup(value)


def comparison_text(value: str) -> str:
    return source_occurrence_comparison_text(value)
