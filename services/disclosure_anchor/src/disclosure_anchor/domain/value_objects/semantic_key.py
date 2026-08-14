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


def validate_optional_semantic_key_state(
    semantic_key: Any,
    semantic_keys: Any,
) -> None:
    """Accept no route claim or one ordered, internally consistent route set."""

    if semantic_key is None and semantic_keys is None:
        return
    if not is_valid_semantic_key(semantic_key):
        raise SemanticKeyInvariantError(
            "scalar_invalid",
            "semantic_key must be null or a controlled non-empty key",
        )
    if not isinstance(semantic_keys, list) or not semantic_keys:
        raise SemanticKeyInvariantError(
            "array_empty",
            "semantic_keys must be null or a non-empty array",
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
    if semantic_keys[0] != semantic_key:
        raise SemanticKeyInvariantError(
            "primary_not_first",
            "semantic_key must be the first semantic_keys item",
        )


def validate_optional_section_keys(section_keys: Any) -> None:
    """Accept no normalized section claim or one ordered unique key chain."""

    if section_keys is None:
        return
    if not isinstance(section_keys, list) or not section_keys:
        raise SemanticKeyInvariantError(
            "section_array_empty",
            "section_keys must be null or a non-empty array",
        )
    if any(not is_valid_semantic_key(key) for key in section_keys):
        raise SemanticKeyInvariantError(
            "section_item_invalid",
            "section_keys contains an invalid key",
        )
    if len(section_keys) != len(set(section_keys)):
        raise SemanticKeyInvariantError(
            "section_array_duplicate",
            "section_keys must not contain duplicates",
        )
