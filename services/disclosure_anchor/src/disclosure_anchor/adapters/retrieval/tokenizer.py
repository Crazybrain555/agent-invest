"""Deterministic Chinese analyzers for the derived search projection.

The index and query analyzers intentionally differ: jieba search mode emits
the exact term plus useful subterms at write time, while exact mode keeps the
query compact.  A pinned general-purpose tokenizer is an analyzer mechanism,
not an application phrase list; document taxonomy and filing-period aliases
remain outside content tokenization.
"""

from __future__ import annotations

import threading
import unicodedata

RETRIEVAL_RULES_VERSION = "rp-2026.08-provider-unit-v1"

_lock = threading.Lock()
_tokenizer = None


def _load_tokenizer():  # type: ignore[no-untyped-def]
    global _tokenizer
    if _tokenizer is not None:
        return _tokenizer
    with _lock:
        if _tokenizer is None:
            import jieba

            _tokenizer = jieba.Tokenizer()
    return _tokenizer


def normalize_search_text(text: str) -> str:
    """Return the sole Unicode normalization used by index and query lanes."""

    return unicodedata.normalize("NFKC", text).casefold()


def index_word_tokens(text: str) -> str:
    """Space-joined jieba search-mode tokens for a stored ``tsvector``."""

    normalized = normalize_search_text(text)
    if not normalized.strip():
        return ""
    tokens = (
        token.strip()
        for token in _load_tokenizer().cut_for_search(normalized, HMM=True)
    )
    return " ".join(token for token in tokens if token)


def query_word_tokens(text: str) -> tuple[str, ...]:
    """Ordered, exact-mode query lexemes without content-level aliases."""

    normalized = normalize_search_text(text)
    if not normalized.strip():
        return ()
    tokens = (
        token.strip()
        for token in _load_tokenizer().lcut(normalized, HMM=True)
    )
    return tuple(token for token in tokens if token)


def _quote_lexeme(lexeme: str) -> str:
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


def build_search_tsquery_groups(query: str) -> tuple[str, ...]:
    """Return exact word-token groups whose conjunction represents a query."""

    return tuple(
        _quote_lexeme(token)
        for token in dict.fromkeys(query_word_tokens(query))
    )


def build_search_tsquery(query: str) -> str:
    """Return a ``to_tsquery('simple', ...)`` string for the word channel."""

    return " & ".join(build_search_tsquery_groups(query))
