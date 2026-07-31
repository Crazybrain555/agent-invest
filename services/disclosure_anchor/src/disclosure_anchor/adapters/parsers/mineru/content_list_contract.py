"""Single typed-field schema for MinerU legacy ``content_list`` items."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from disclosure_anchor.domain.errors import ParserOutputContractError

MinerUFieldSpec = tuple[str, tuple[str, ...], str]

_LOCATION_FIELDS = frozenset({"type", "page_idx", "bbox"})
_TEXT_ALIASES = ("text", "content")
_IMAGE_PATH_FIELDS = frozenset({"img_path", "image_path", "image"})
_TEXT_KINDS = frozenset(
    "aside_text equation footer header page_footnote page_number phonetic ref_text text".split()
)

MINERU_VISIBLE_FIELD_SPECS: dict[str, tuple[MinerUFieldSpec, ...]] = {
    kind: (("scalar", _TEXT_ALIASES, "text"),) for kind in _TEXT_KINDS
}
MINERU_VISIBLE_FIELD_SPECS.update(
    chart=(
        ("sequence", ("chart_caption",), "image_caption"),
        ("sequence", ("chart_footnote",), "image_footnote"),
    ),
    code=(
        ("sequence", ("code_caption",), "code_caption"),
        ("scalar", ("code_body",), "code_body"),
        ("sequence", ("code_footnote",), "code_footnote"),
    ),
    image=(
        ("sequence", ("image_caption",), "image_caption"),
        ("sequence", ("image_footnote",), "image_footnote"),
    ),
    list=(("strict_sequence", ("list_items",), "list_items"),),
    table=(
        ("sequence", ("table_caption",), "table_caption"),
        ("scalar", ("table_body", "table_html"), "table_html"),
        ("sequence", ("table_footnote",), "table_footnote"),
    ),
)

_EXTRA_FIELDS_BY_KIND = {
    # Chart text/content is visual recognition, not native source text.  It is
    # mapped, but intentionally excluded from the ordinary text-carrier lane.
    "chart": _IMAGE_PATH_FIELDS | frozenset(_TEXT_ALIASES) | {"sub_type"},
    "code": frozenset({"sub_type"}),
    "equation": _IMAGE_PATH_FIELDS | {"text_format"},
    # MinerU image text/content is a free-form generated description rather
    # than visual recognition, so it is not an exact source-text carrier.
    "image": _IMAGE_PATH_FIELDS | frozenset(_TEXT_ALIASES) | {"sub_type"},
    "list": frozenset({"sub_type"}),
    "table": _IMAGE_PATH_FIELDS,
    "text": frozenset({"text_level"}),
}

MINERU_CONTENT_FIELDS_BY_KIND = {
    kind: _LOCATION_FIELDS
    | frozenset(
        raw_field
        for _, raw_fields, _ in specs
        for raw_field in raw_fields
    )
    | _EXTRA_FIELDS_BY_KIND.get(kind, frozenset())
    for kind, specs in MINERU_VISIBLE_FIELD_SPECS.items()
}
MINERU_SUPPORTED_RAW_KINDS = frozenset(MINERU_CONTENT_FIELDS_BY_KIND)
MINERU_SCALAR_IR_FIELDS = frozenset(
    ir_field
    for specs in MINERU_VISIBLE_FIELD_SPECS.values()
    for shape, _, ir_field in specs
    if shape == "scalar"
)
MINERU_SEQUENCE_IR_FIELDS = frozenset(
    ir_field
    for specs in MINERU_VISIBLE_FIELD_SPECS.values()
    for shape, _, ir_field in specs
    if shape != "scalar"
)


class MinerUFieldContractError(ParserOutputContractError):
    """Typed provider-field violation with a stable source-evidence reason."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def canonical_mineru_item_sha256(item: Mapping[str, Any]) -> str:
    """Hash one complete provider item with the sole canonical encoding."""

    encoded = json.dumps(
        item,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def mineru_scalar_alias(
    item: Mapping[str, Any],
    fields: tuple[str, ...],
    *,
    strip_equivalent: bool = False,
) -> str | None:
    """Resolve one closed scalar alias family without choosing by length."""

    present: list[str] = []
    for field in fields:
        value = item.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise MinerUFieldContractError(
                "mineru_field_invalid",
                f"MinerU {field} must be text or null",
            )
        present.append(value)
    nonempty = [value for value in present if value]
    if nonempty:
        canonical = nonempty[0]
        comparable = canonical.strip() if strip_equivalent else canonical
        if any(
            (value.strip() if strip_equivalent else value) != comparable
            for value in nonempty[1:]
        ):
            raise MinerUFieldContractError(
                "mineru_alias_conflict",
                f"MinerU {'/'.join(fields)} aliases conflict",
            )
        return canonical
    return present[0] if present else None


def mineru_text_sequence(
    value: object,
    *,
    field: str,
    strict_array: bool = False,
) -> list[str]:
    """Decode MinerU's text-or-array fields under one provider contract."""

    if value is None and not strict_array:
        return []
    if isinstance(value, str) and not strict_array:
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    expected = "an array of text" if strict_array else "text, an array of text, or null"
    raise MinerUFieldContractError(
        "mineru_field_invalid",
        f"MinerU {field} must be {expected}",
    )


def resolved_table_html(item: Mapping[str, Any]) -> str | None:
    return mineru_scalar_alias(
        item,
        ("table_body", "table_html"),
        strip_equivalent=True,
    )


def resolved_image_path(item: Mapping[str, Any]) -> str | None:
    present: list[str] = []
    for field in ("img_path", "image_path", "image"):
        value = item.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise MinerUFieldContractError(
                "mineru_field_invalid",
                f"MinerU {field} must be text or null",
            )
        if value.strip():
            present.append(value.strip())
    if not present:
        return None
    if any(value != present[0] for value in present[1:]):
        raise MinerUFieldContractError(
            "mineru_alias_conflict",
            "MinerU image path aliases contain conflicting non-empty values",
        )
    return present[0]


def resolved_text(
    item: Mapping[str, Any],
    *,
    include_content: bool,
) -> str | None:
    fields = ("text", "content") if include_content else ("text",)
    return mineru_scalar_alias(item, fields)


def mineru_list_items(item: Mapping[str, Any]) -> list[str]:
    return mineru_text_sequence(
        item.get("list_items"),
        field="list_items",
        strict_array=True,
    )


def mineru_code_payload(
    item: Mapping[str, Any],
) -> tuple[str, list[str], list[str], str | None, str]:
    body = item.get("code_body")
    if not isinstance(body, str) or not body.strip():
        raise MinerUFieldContractError(
            "mineru_field_invalid",
            "MinerU code_body must be non-empty text for type=code",
        )
    captions = mineru_text_sequence(
        item.get("code_caption"),
        field="code_caption",
    )
    footnotes = mineru_text_sequence(
        item.get("code_footnote"),
        field="code_footnote",
    )
    raw_subtype = item.get("sub_type")
    if raw_subtype is not None and not isinstance(raw_subtype, str):
        raise MinerUFieldContractError(
            "mineru_field_invalid",
            "MinerU code sub_type must be text or null",
        )
    subtype = raw_subtype if isinstance(raw_subtype, str) and raw_subtype else None
    visible = "\n".join(
        value for value in [*captions, body, *footnotes] if value.strip()
    )
    return body, captions, footnotes, subtype, visible


def mineru_typed_values(
    item: Mapping[str, Any],
    kind: str,
) -> tuple[tuple[str, int | None, str], ...]:
    """Decode every visible field using the single typed MinerU schema."""

    result: list[tuple[str, int | None, str]] = []
    for shape, raw_fields, ir_field in MINERU_VISIBLE_FIELD_SPECS[kind]:
        if shape == "scalar":
            value = (
                resolved_table_html(item)
                if ir_field == "table_html"
                else mineru_scalar_alias(item, raw_fields)
            )
            if value is not None:
                result.append((ir_field, None, value))
            continue
        raw_field = raw_fields[0]
        values = mineru_text_sequence(
            item.get(raw_field),
            field=raw_field,
            strict_array=shape == "strict_sequence",
        )
        result.extend(
            (ir_field, index, value) for index, value in enumerate(values)
        )
    if kind == "code" and not any(
        field == "code_body" and value.strip()
        for field, _index, value in result
    ):
        raise MinerUFieldContractError(
            "mineru_field_invalid",
            "MinerU code_body must be non-empty text for type=code",
        )
    return tuple(result)


def mineru_provider_item_sha256(item: Mapping[str, Any]) -> str:
    try:
        return canonical_mineru_item_sha256(item)
    except (TypeError, ValueError) as exc:
        raise ParserOutputContractError(
            "MinerU content-list item is not canonical JSON"
        ) from exc
