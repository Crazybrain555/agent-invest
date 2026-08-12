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


def validate_optional_semantic_key(semantic_key: Any) -> None:
    """Accept no semantic claim or one controlled scalar key."""

    if semantic_key is None:
        return
    if not is_valid_semantic_key(semantic_key):
        raise SemanticKeyInvariantError(
            "scalar_invalid",
            "semantic_key must be null or a controlled non-empty key",
        )
