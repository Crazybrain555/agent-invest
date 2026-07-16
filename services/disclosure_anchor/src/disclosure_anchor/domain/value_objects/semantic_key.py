"""Controlled semantic-key syntax and new-unit invariants."""

from __future__ import annotations

import re
from typing import Any


SEMANTIC_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$", re.ASCII)


class SemanticKeyInvariantError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def is_valid_semantic_key(value: Any) -> bool:
    return isinstance(value, str) and SEMANTIC_KEY_RE.fullmatch(value) is not None


def validate_semantic_key_state(
    semantic_key: Any,
    semantic_keys: Any,
) -> None:
    """Require one non-empty, stable, internally consistent key set."""

    if not is_valid_semantic_key(semantic_key):
        raise SemanticKeyInvariantError(
            "scalar_invalid",
            "semantic_key must be a controlled non-empty key",
        )
    if not isinstance(semantic_keys, list) or not semantic_keys:
        raise SemanticKeyInvariantError(
            "array_empty",
            "semantic_keys must be a non-empty array",
        )
    if any(not is_valid_semantic_key(key) for key in semantic_keys):
        raise SemanticKeyInvariantError(
            "array_item_invalid",
            "semantic_keys contains an invalid key",
        )
    if len(semantic_keys) != len(set(semantic_keys)):
        raise SemanticKeyInvariantError(
            "array_duplicate",
            "semantic_keys must not contain duplicates",
        )
    if semantic_key not in semantic_keys:
        raise SemanticKeyInvariantError(
            "scalar_not_member",
            "semantic_key must be present in semantic_keys",
        )
