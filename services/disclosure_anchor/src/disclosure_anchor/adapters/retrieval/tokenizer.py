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
        tokenizer.load_userdict(str(_DICT_PATH))
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
