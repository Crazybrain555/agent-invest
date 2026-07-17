"""Deterministic write-time Chinese tokenizer for the 06R search projection.

Application-side jieba segmentation feeding PostgreSQL's built-in ``simple``
text-search configuration (milestone 06R §2): the local cluster offers no
Chinese parser extension, and pinning jieba + the domain dictionary in-repo
keeps the projection deterministically regenerable (U7 red line).

Any change to the jieba pin or the dictionary MUST bump
``RETRIEVAL_RULES_VERSION`` and trigger a full projection rebuild.
"""

from __future__ import annotations

import hashlib
import threading
import unicodedata
from pathlib import Path

RETRIEVAL_RULES_VERSION = "rp-2026.07-1"

_DICT_PATH = Path(__file__).with_name("domain_dict.txt")
_DICT_SHA256 = "e07c3d14e90c9e1bd2db08098722dff020d616e39cfef1b703ba6eb66f0ada8b"
_DICT_ENTRIES = 389

_lock = threading.Lock()
_tokenizer = None


class RetrievalDictionaryError(RuntimeError):
    """The pinned domain dictionary drifted from the recorded fingerprint."""


def _load_tokenizer():  # type: ignore[no-untyped-def]
    global _tokenizer
    if _tokenizer is not None:
        return _tokenizer
    with _lock:
        if _tokenizer is not None:
            return _tokenizer
        body = _DICT_PATH.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        entries = sum(1 for line in body.splitlines() if line.strip())
        if digest != _DICT_SHA256 or entries != _DICT_ENTRIES:
            raise RetrievalDictionaryError(
                "domain_dict.txt drifted: "
                f"sha256={digest} entries={entries}; bump "
                "RETRIEVAL_RULES_VERSION and refresh the pinned fingerprint"
            )
        import jieba

        tokenizer = jieba.Tokenizer()
        with _DICT_PATH.open("rb") as handle:
            tokenizer.load_userdict(handle)
        _tokenizer = tokenizer
    return _tokenizer


def tokenize(text: str) -> str:
    """NFKC-casefolded exact-mode segmentation, space-joined for tsvector."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    if not normalized.strip():
        return ""
    tokenizer = _load_tokenizer()
    tokens = [
        token.strip()
        for token in tokenizer.lcut(normalized, HMM=True)
        if token.strip()
    ]
    return " ".join(tokens)


# --- Query-side synonym expansion (queries only; never touches stored data) --

SEARCH_SYNONYMS_VERSION = "qs-2026.07-1"

_SYNONYMS_PATH = Path(__file__).with_name("synonyms.txt")
_MAX_SYNONYM_GROUPS = 40

# Parsing validates every term through tokenize(), which may itself have to
# build the jieba tokenizer under ``_lock`` — so synonym caching needs its
# own lock or a cold-start load deadlocks.
_synonyms_lock = threading.Lock()
_synonyms: dict[str, tuple[str, ...]] | None = None


class RetrievalSynonymError(RuntimeError):
    """The query-side synonym table is malformed or out of contract."""


def _synonym_terms(segment: str, *, line_no: int) -> list[str]:
    terms = [term.strip() for term in segment.split(",")]
    terms = [term for term in terms if term]
    for term in terms:
        if tokenize(term) != term:
            raise RetrievalSynonymError(
                f"synonyms.txt:{line_no}: {term!r} is not a single lexeme "
                "under the pinned tokenizer; shared-lexeme pairs match "
                "without an alias and multi-lexeme aliases never fire"
            )
    return terms


def parse_synonyms(text: str) -> dict[str, tuple[str, ...]]:
    """Parse the alias table into token -> extra-lexemes, failing closed."""

    expansion: dict[str, list[str]] = {}
    groups = 0
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        groups += 1
        if "=>" in line:
            left, _, right = line.partition("=>")
            sources = _synonym_terms(left, line_no=line_no)
            targets = _synonym_terms(right, line_no=line_no)
            if len(sources) != 1 or not targets:
                raise RetrievalSynonymError(
                    f"synonyms.txt:{line_no}: a directional rule needs "
                    "exactly one source and at least one target"
                )
            expansion.setdefault(sources[0], []).extend(targets)
        else:
            terms = _synonym_terms(line, line_no=line_no)
            if len(terms) < 2:
                raise RetrievalSynonymError(
                    f"synonyms.txt:{line_no}: an equivalence group needs "
                    "at least two terms"
                )
            for term in terms:
                expansion.setdefault(term, []).extend(
                    other for other in terms if other != term
                )
    if groups > _MAX_SYNONYM_GROUPS:
        raise RetrievalSynonymError(
            f"synonyms.txt exceeds the {_MAX_SYNONYM_GROUPS}-group cap; the "
            "alias table is deliberately bounded (see file header)"
        )
    return {
        token: tuple(dict.fromkeys(extras))
        for token, extras in expansion.items()
    }


def _load_synonyms() -> dict[str, tuple[str, ...]]:
    global _synonyms
    if _synonyms is not None:
        return _synonyms
    with _synonyms_lock:
        if _synonyms is None:
            _synonyms = parse_synonyms(
                _SYNONYMS_PATH.read_text(encoding="utf-8")
            )
    return _synonyms


def _quote_lexeme(lexeme: str) -> str:
    return "'" + lexeme.replace("'", "''") + "'"


def build_search_tsquery(query: str) -> str:
    """Expand a user query into a ``to_tsquery('simple', …)`` string.

    Each query lexeme becomes an OR-group of itself plus its curated
    aliases; groups are AND-combined. Returns an empty string when no
    lexeme survives normalization, so callers can skip the tsquery channel.
    """

    tokens = tokenize(query).split()
    if not tokens:
        return ""
    synonyms = _load_synonyms()
    groups: list[str] = []
    for token in dict.fromkeys(tokens):
        alternatives = dict.fromkeys([token, *synonyms.get(token, ())])
        quoted = [_quote_lexeme(alternative) for alternative in alternatives]
        groups.append(
            "(" + " | ".join(quoted) + ")" if len(quoted) > 1 else quoted[0]
        )
    return " & ".join(groups)
