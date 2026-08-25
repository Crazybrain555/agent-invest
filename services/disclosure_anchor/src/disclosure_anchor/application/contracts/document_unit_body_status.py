"""DB-free derivation of the public Unit body-content status."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Literal


DocumentUnitBodyStatus = Literal["content", "heading_only", "empty"]


def derive_document_unit_body_status(
    *,
    payload_kind: str,
    payload: Mapping[str, object],
    title: str | None,
) -> DocumentUnitBodyStatus:
    """Mirror the append-only 0043 public-view rule from source payload fields.

    This pure form is intentionally usable by source-replay evidence without
    reading the public view it is meant to verify.  The scratch-DB integration
    suite binds it to the PostgreSQL JSONB expression on representative edge
    cases.
    """

    if payload_kind not in {"text", "table", "qa", "mixed"}:
        raise ValueError("document Unit payload kind is unsupported")
    if payload_kind == "text" and dict(payload) == {"text": ""}:
        return "heading_only" if title is not None else "empty"
    if payload_kind == "mixed":
        parts = payload.get("parts")
        if isinstance(parts, list) and not _mixed_parts_have_body(parts):
            return "heading_only" if title is not None else "empty"
    return "content"


def _mixed_parts_have_body(parts: list[object]) -> bool:
    for part in parts:
        if not isinstance(part, Mapping):
            raise ValueError("mixed document Unit part must be an object")
        for key, value in part.items():
            if key == "content_artifacts":
                continue
            if isinstance(value, str) and value.strip(" "):
                return True
            if isinstance(value, list) and any(
                _json_array_element_has_text(item) for item in value
            ):
                return True
    return False


def _json_array_element_has_text(value: object) -> bool:
    # PostgreSQL jsonb_array_elements_text returns SQL NULL for JSON null and
    # a non-empty textual representation for every other non-string scalar or
    # nested JSON value.  Strings retain their own text before btrim().
    if value is None:
        return False
    if isinstance(value, str):
        # PostgreSQL btrim(text) defaults to U+0020 only.  Tabs, newlines and
        # other Unicode whitespace remain source content in the 0043 view.
        return bool(value.strip(" "))
    return bool(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).strip()
    )


__all__ = ["DocumentUnitBodyStatus", "derive_document_unit_body_status"]
