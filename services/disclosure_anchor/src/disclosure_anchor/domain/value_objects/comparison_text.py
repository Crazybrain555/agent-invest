"""Representation-level text normalization shared across builder and audit.

Two carriers are "the same" for exact-duplicate/projection comparison when they
differ only in Unicode representation, letter case, LaTeX tilde escaping, or
whitespace.  Keeping this normalization in the domain layer lets the unit
builder and its independent audit apply byte-identical rules without importing
each other.  Domain code stays free of IO/framework dependencies (stdlib only).
"""

from __future__ import annotations

import re
import unicodedata


def comparison_text(value: str) -> str:
    """Normalize only representation-level differences for exact comparison."""

    normalized = unicodedata.normalize("NFKC", value).casefold().replace(r"\~", "~")
    return re.sub(r"\s+", "", normalized)
