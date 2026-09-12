"""Bounded canonical JSON for private diagnostic evidence; no IO authority."""

from __future__ import annotations

import json
import math
from dataclasses import fields, is_dataclass
from typing import cast

from disclosure_anchor.application.contracts.strict_json import strict_json_loads


MAXIMUM_JSON_DEPTH = 64
_TEXT_CHUNK_CHARACTERS = 4096


def require_projection_budget(value: object, *, maximum_bytes: int) -> None:
    """Bound materialization before projecting an evidence DTO into JSON.

    Charge a conservative lower bound of the emitted values, visiting dataclass
    fields without asdict/deepcopy. Encoders may use this only when every visited
    value is retained in their wire shape. The final serializer still enforces
    the exact byte count, including keys and escaping. This preflight keeps
    container expansion proportional to the granted budget, even on failure.
    """
    _require_budget(maximum_bytes)
    remaining = maximum_bytes

    def charge(count: int) -> None:
        nonlocal remaining
        if count > remaining:
            raise ValueError("diagnostic JSON projection byte budget exceeded")
        remaining -= count

    def visit(item: object, depth: int) -> None:
        if item is None:
            charge(4)
        elif type(item) is bool:
            charge(4 if item else 5)
        elif type(item) is str:
            if len(item) + 2 > remaining:
                raise ValueError("diagnostic JSON projection byte budget exceeded")
            charge(2)
            for start in range(0, len(item), _TEXT_CHUNK_CHARACTERS):
                charge(len(item[start:start + _TEXT_CHUNK_CHARACTERS].encode("utf-8")))
        elif type(item) is int:
            if max(0, item.bit_length() - 1) // 4 > remaining:
                raise ValueError("diagnostic JSON projection byte budget exceeded")
            charge(len(str(item)))
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("diagnostic JSON number must be finite")
            charge(len(json.dumps(item, allow_nan=False)))
        elif type(item) in (dict, list, tuple) or (
            is_dataclass(item) and not isinstance(item, type)
        ):
            if depth >= MAXIMUM_JSON_DEPTH:
                raise ValueError("diagnostic JSON nesting budget exceeded")
            charge(1)
            if is_dataclass(item) and not isinstance(item, type):
                for field in fields(item):
                    visit(getattr(item, field.name), depth + 1)
            elif type(item) is dict:
                if len(item) > remaining:
                    raise ValueError("diagnostic JSON projection byte budget exceeded")
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError("diagnostic JSON object keys must be strings")
                    visit(key, depth + 1)
                    visit(child, depth + 1)
            else:
                sequence = cast(list[object] | tuple[object, ...], item)
                if len(sequence) > remaining:
                    raise ValueError("diagnostic JSON projection byte budget exceeded")
                for child in sequence:
                    visit(child, depth + 1)
        else:
            raise ValueError("diagnostic JSON projection has an unsupported type")

    visit(value, 0)


def bounded_json_bytes(value: object, *, maximum_bytes: int) -> bytes:
    """Serialize while charging each small UTF-8 chunk before retaining it.

    The root container has depth one. Scalar values add no container depth.
    Strings are escaped in bounded pieces, including when one scalar alone is
    larger than the evidence allocation. Unsupported Python objects are rejected.
    """
    _require_budget(maximum_bytes)
    output = bytearray()

    def append(data: bytes) -> None:
        if len(data) > maximum_bytes - len(output):
            raise ValueError("diagnostic JSON byte budget exceeded")
        output.extend(data)

    def text(value: str) -> None:
        append(b'"')
        for start in range(0, len(value), _TEXT_CHUNK_CHARACTERS):
            piece = json.dumps(
                value[start:start + _TEXT_CHUNK_CHARACTERS], ensure_ascii=False,
            )[1:-1]
            append(piece.encode("utf-8"))
        append(b'"')

    def emit(item: object, depth: int) -> None:
        if item is None:
            append(b"null")
        elif type(item) is bool:
            append(b"true" if item else b"false")
        elif type(item) is str:
            text(item)
        elif type(item) is int:
            append(str(item).encode("ascii"))
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("diagnostic JSON number must be finite")
            append(json.dumps(item, allow_nan=False).encode("ascii"))
        elif type(item) in (dict, list, tuple):
            container = cast(dict[str, object] | list[object] | tuple[object, ...], item)
            if depth >= MAXIMUM_JSON_DEPTH:
                raise ValueError("diagnostic JSON nesting budget exceeded")
            # Even an empty value plus its separator needs at least two bytes.
            # Reject huge containers before allocating a sorted key inventory.
            if len(container) > maximum_bytes - len(output):
                raise ValueError("diagnostic JSON byte budget exceeded")
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    raise ValueError("diagnostic JSON object keys must be strings")
                append(b"{")
                for index, key in enumerate(sorted(item)):
                    if index:
                        append(b",")
                    text(key)
                    append(b":")
                    emit(item[key], depth + 1)
                append(b"}")
            else:
                append(b"[")
                for index, child in enumerate(cast(list[object] | tuple[object, ...], item)):
                    if index:
                        append(b",")
                    emit(child, depth + 1)
                append(b"]")
        else:
            raise ValueError("diagnostic JSON value has an unsupported type")

    emit(value, 0)
    return bytes(output)


def bounded_json_value(raw: bytes, *, maximum_bytes: int) -> object:
    """Decode only bounded, strict, canonical bytes, before any record trust."""
    _require_budget(maximum_bytes)
    if type(raw) is not bytes:
        raise ValueError("diagnostic JSON input must be bytes")
    if len(raw) > maximum_bytes:
        raise ValueError("diagnostic JSON byte budget exceeded")
    # Scan ASCII structural bytes before the recursive decoder. Brackets inside
    # UTF-8 strings never count; malformed JSON still goes to the strict decoder.
    depth = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAXIMUM_JSON_DEPTH:
                raise ValueError("diagnostic JSON nesting budget exceeded")
        elif byte in (93, 125):
            depth -= 1
    value = strict_json_loads(raw)
    if bounded_json_bytes(value, maximum_bytes=maximum_bytes) != raw:
        raise ValueError("diagnostic JSON bytes are not canonical")
    return value


def _require_budget(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("diagnostic JSON byte budget must be a positive integer")
