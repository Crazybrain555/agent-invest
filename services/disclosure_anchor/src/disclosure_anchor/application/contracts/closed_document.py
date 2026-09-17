"""Shared helpers for small closed JSON contracts with canonical identity.

A closed document is a JSON object with an exact field set and exact scalar
types. Decoding accepts any strict JSON layout (unique keys, no NaN); identity
is the canonical compact sorted encoding, so a pretty tracked file and its
canonical package copy share one hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from disclosure_anchor.application.contracts.strict_json import strict_json_loads


SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_INT64 = (1 << 63) - 1


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def sha256_of(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def load_closed_object(payload: bytes, *, label: str, maximum_bytes: int) -> dict[str, Any]:
    if type(payload) is not bytes or not payload or len(payload) > maximum_bytes:
        raise ValueError(f"{label} bytes are outside the closed envelope")
    try:
        decoded = strict_json_loads(payload)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} is not strict UTF-8 JSON") from exc
    if type(decoded) is not dict:
        raise ValueError(f"{label} must be a JSON object")
    return decoded


def require_fields(value: dict[str, Any], names: frozenset[str] | set[str], *, label: str) -> None:
    if set(value) != set(names):
        raise ValueError(f"{label} fields are not closed")


def require_str(value: object, *, label: str, maximum: int = 4096) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def require_sha256(value: object, *, label: str) -> str:
    if type(value) is not str or SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is not a canonical sha256")
    return value


def require_int(value: object, *, label: str, minimum: int = 1, maximum: int = _MAX_INT64) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer within {minimum}..{maximum}")
    return value


def require_bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def require_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite positive number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


__all__ = [
    "SHA256_RE",
    "canonical_bytes",
    "load_closed_object",
    "require_bool",
    "require_fields",
    "require_int",
    "require_number",
    "require_sha256",
    "require_str",
    "sha256_of",
]
