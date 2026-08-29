"""Small shared strict JSON decoder for security-sensitive evidence."""

from __future__ import annotations

import json
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def strict_json_loads(payload: str | bytes | bytearray) -> object:
    """Decode UTF-8 JSON while rejecting duplicate keys and non-finite numbers."""

    if not isinstance(payload, str):
        payload = bytes(payload).decode("utf-8")
    return json.loads(
        payload,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


__all__ = ["strict_json_loads"]
